"""Model access behind one interface.

Typed proposals use FORCED TOOL USE, never `response_format`. Verified against
gpt-oss-120b on Bedrock: `response_format: json_schema, strict: true` came back as
`finish_reason: stop` with reasoning tags, a markdown preamble and truncated JSON,
while `toolChoice: {tool: {name}}` returned `stopReason: tool_use` with pre-parsed,
schema-conformant input.

It also gives chain-of-thought separation for free. Converse returns reasoning as its
own content block, so dropping it is structural rather than a regex over
`<reasoning>` tags — which is how TD004's "do not store or present hidden
chain-of-thought" stays reliable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "openai.gpt-oss-120b-1:0"
DEFAULT_REGION = "ap-south-1"


class ModelCall(BaseModel):
    """Everything auditable about one model call. Reasoning text is never kept."""

    provider: str
    model: str
    prompt_version: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False


class ProposalError(RuntimeError):
    """The model did not return a usable typed proposal."""


class Provider(ABC):
    name: str

    @abstractmethod
    def propose(
        self,
        schema_model: type[T],
        *,
        system: str,
        user: str,
        tool_name: str,
        prompt_version: str,
        reasoning_effort: str = "low",
        max_tokens: int = 2048,
    ) -> tuple[T, ModelCall]:
        """Force one typed proposal out of the model."""


def _tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic -> Bedrock toolSpec inputSchema. $defs are inlined; Bedrock rejects refs."""
    js = model.model_json_schema()
    defs = js.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                key = node["$ref"].rsplit("/", 1)[-1]
                return inline(defs.get(key, {}))
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(js)


class BedrockProvider(Provider):
    """Bedrock Converse with a forced tool call."""

    name = "bedrock"

    def __init__(self, model_id: str | None = None, region: str | None = None) -> None:
        import boto3

        self.model_id = model_id or os.environ.get("MODEL_ID", DEFAULT_MODEL)
        self.region = region or os.environ.get("AWS_REGION", DEFAULT_REGION)
        self._client = boto3.client("bedrock-runtime", region_name=self.region)

    def propose(
        self,
        schema_model: type[T],
        *,
        system: str,
        user: str,
        tool_name: str,
        prompt_version: str,
        reasoning_effort: str = "low",
        max_tokens: int = 2048,
    ) -> tuple[T, ModelCall]:
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool_name,
                        "description": schema_model.__doc__ or tool_name,
                        "inputSchema": {"json": _tool_schema(schema_model)},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": tool_name}},
        }
        started = time.monotonic()
        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
            additionalModelRequestFields={"reasoning_effort": reasoning_effort},
        )
        latency = int((time.monotonic() - started) * 1000)

        payload = None
        for block in response["output"]["message"]["content"]:
            # reasoningContent is deliberately ignored, never stored.
            if "toolUse" in block:
                payload = block["toolUse"]["input"]
        if payload is None:
            raise ProposalError(f"model returned no tool call (stop={response.get('stopReason')})")

        usage = response.get("usage", {})
        call = ModelCall(
            provider=self.name,
            model=self.model_id,
            prompt_version=prompt_version,
            latency_ms=latency,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )
        return schema_model.model_validate(payload), call


class CachingProvider(Provider):
    """Replay identical requests from disk.

    Does double duty. It makes the audit trail reproducible — gpt-oss-120b is
    Mixture-of-Experts and provider-side batching means temperature=0 is not a
    determinism guarantee — and it is the offline demo path, so a rate limit or a
    dead network never takes the demo down.

    A replay of a recorded real call is not a hand-written fixture, and the UI
    labels it as a replay.
    """

    def __init__(self, inner: Provider | None, cache_dir: Path, *, offline: bool = False) -> None:
        self.inner = inner
        self.cache_dir = cache_dir
        self.offline = offline
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.name = f"cached:{inner.name if inner else 'offline'}"

    def _key(self, schema_model: type[BaseModel], system: str, user: str, version: str) -> str:
        model_id = getattr(self.inner, "model_id", "offline")
        blob = json.dumps([model_id, version, schema_model.__name__, system, user], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def propose(
        self,
        schema_model: type[T],
        *,
        system: str,
        user: str,
        tool_name: str,
        prompt_version: str,
        reasoning_effort: str = "low",
        max_tokens: int = 2048,
    ) -> tuple[T, ModelCall]:
        key = self._key(schema_model, system, user, prompt_version)
        path = self.cache_dir / f"{key}.json"

        if path.exists():
            cached = json.loads(path.read_text())
            call = ModelCall.model_validate(cached["call"])
            call.cached = True
            return schema_model.model_validate(cached["payload"]), call

        if self.offline or self.inner is None:
            raise ProposalError(
                f"offline mode and no cached response for {tool_name} ({key}). "
                "Record a live run first."
            )

        result, call = self.inner.propose(
            schema_model,
            system=system,
            user=user,
            tool_name=tool_name,
            prompt_version=prompt_version,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        path.write_text(
            json.dumps(
                {"payload": result.model_dump(mode="json"), "call": call.model_dump(mode="json")},
                indent=2,
            )
        )
        return result, call


def build_provider(cache_dir: Path | None = None, *, offline: bool = False) -> Provider:
    """Wire the configured provider, wrapped in the cache."""
    cache = cache_dir or Path(".artifacts/model-cache")
    inner: Provider | None = None
    if not offline:
        try:
            inner = BedrockProvider()
        except Exception:  # noqa: BLE001 - absence of credentials is a normal offline case
            inner = None
    return CachingProvider(inner, cache, offline=offline or inner is None)
