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


class Lookup(BaseModel):
    """One read-only thing the investigator is allowed to go and check."""

    name: str
    description: str
    input_schema: dict[str, Any]


class Step(BaseModel):
    """One look the agent took, kept so a person can see what it actually checked."""

    looked_at: str
    found: str


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

    def investigate(
        self,
        answer_model: type[T],
        *,
        system: str,
        user: str,
        answer_tool: str,
        lookups: list[Lookup],
        run_lookup: Any,
        prompt_version: str,
        max_steps: int = 4,
        reasoning_effort: str = "low",
        max_tokens: int = 2048,
    ) -> tuple[T | None, list[Step], ModelCall]:
        """Let the model go and look things up before it answers.

        The difference between an agent and a form-filler. Bounded: it gets
        `max_steps` looks, every one is read-only, and if it has not concluded by
        then the question goes to a person with whatever it did find attached.
        """
        raise NotImplementedError


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


    def investigate(
        self,
        answer_model: type[T],
        *,
        system: str,
        user: str,
        answer_tool: str,
        lookups: list[Lookup],
        run_lookup: Any,
        prompt_version: str,
        max_steps: int = 4,
        reasoning_effort: str = "low",
        max_tokens: int = 2048,
    ) -> tuple[T | None, list[Step], ModelCall]:
        reads = [
            {
                "toolSpec": {
                    "name": lk.name,
                    "description": lk.description,
                    "inputSchema": {"json": lk.input_schema},
                }
            }
            for lk in lookups
        ]
        answering = {
            "toolSpec": {
                "name": answer_tool,
                "description": answer_model.__doc__ or answer_tool,
                "inputSchema": {"json": _tool_schema(answer_model)},
            }
        }

        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": user}]}]
        steps: list[Step] = []
        started = time.monotonic()
        tokens_in = tokens_out = 0
        answer: T | None = None

        def turn(tools: list[dict[str, Any]], choice: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal tokens_in, tokens_out
            response = self._client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=messages,
                toolConfig={"tools": tools, "toolChoice": choice},
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
                additionalModelRequestFields={"reasoning_effort": reasoning_effort},
            )
            usage = response.get("usage", {})
            tokens_in += usage.get("inputTokens", 0)
            tokens_out += usage.get("outputTokens", 0)
            return response["output"]["message"]["content"]

        for step_no in range(max_steps):
            # The first turn offers only the lookups. Left free to answer straight
            # away it does, every time, and then reports having "checked the source
            # data" without having called anything. One real look is the minimum
            # that makes this an investigation rather than a guess.
            tools = [*reads] if step_no == 0 else [*reads, answering]
            content = turn(tools, {"any": {}})
            uses = [b["toolUse"] for b in content if "toolUse" in b]
            if not uses:
                break

            # reasoningContent is deliberately dropped here as everywhere else.
            replies: list[dict[str, Any]] = []
            for use in uses:
                if use["name"] == answer_tool:
                    answer = _validated(use["input"], answer_model)
                    if answer is not None:
                        break
                    continue
                found = run_lookup(use["name"], use["input"])
                steps.append(Step(
                    looked_at=_describe(use["name"], use["input"]),
                    found=str(found)[:400],
                ))
                replies.append({
                    "toolResult": {
                        "toolUseId": use["toolUseId"],
                        "content": [{"text": str(found)[:2000]}],
                    }
                })
            if answer is not None:
                break
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": replies})

        if answer is None:
            # Out of looks, or it stopped calling tools. Either way it has to commit
            # to something now, even if that something is "I could not settle it".
            # Saying so in words as well as in toolChoice matters: forced to answer
            # off the back of a tool conversation it will otherwise invent another
            # lookup to call, and the name it invents is not one that exists.
            messages.append({"role": "user", "content": [{
                "text": (
                    "Stop looking now and record your finding with "
                    f"`{answer_tool}`, using only what the lookups returned above. "
                    "If they did not settle it, say so and set settled to false."
                )
            }]})
            content = turn([answering], {"tool": {"name": answer_tool}})
            answer = _first_answer(content, answer_tool, answer_model)

        call = ModelCall(
            provider=self.name, model=self.model_id, prompt_version=prompt_version,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=tokens_in, output_tokens=tokens_out,
        )
        return answer, steps, call


def _validated[M: BaseModel](payload: Any, answer_model: type[M]) -> M | None:
    """A malformed answer is a failed investigation, not a crashed run."""
    try:
        return answer_model.model_validate(payload)
    except Exception:  # noqa: BLE001 - the case simply goes to a person unaided
        return None


def _first_answer[M: BaseModel](
    content: list[dict[str, Any]], answer_tool: str, answer_model: type[M]
) -> M | None:
    """Take the answer, and only the answer.

    Under a forced toolChoice the model still sometimes calls a tool it made up,
    so the name is checked rather than assumed.
    """
    for block in content:
        use = block.get("toolUse")
        if use and use.get("name") == answer_tool:
            return _validated(use["input"], answer_model)
    return None


def _describe(tool: str, args: dict[str, Any]) -> str:
    """The look, in words a consultant can read."""
    pretty = tool.replace("_", " ")
    if not args:
        return pretty
    return f"{pretty}: " + ", ".join(f"{k}={v!r}" for k, v in args.items())


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


    def investigate(
        self,
        answer_model: type[T],
        *,
        system: str,
        user: str,
        answer_tool: str,
        lookups: list[Lookup],
        run_lookup: Any,
        prompt_version: str,
        max_steps: int = 4,
        reasoning_effort: str = "low",
        max_tokens: int = 2048,
    ) -> tuple[T | None, list[Step], ModelCall]:
        """Cached whole, not turn by turn.

        What is worth replaying is the investigation - what it went and looked at,
        and what it concluded - not the individual exchanges that produced it.
        Keying on the opening question keeps a replay reproducible even though the
        path through the looks is the model's to choose.
        """
        key = self._key(answer_model, system, user, f"{prompt_version}/investigate")
        path = self.cache_dir / f"{key}.json"

        if path.exists():
            cached = json.loads(path.read_text())
            call = ModelCall.model_validate(cached["call"])
            call.cached = True
            answer = (
                answer_model.model_validate(cached["payload"])
                if cached.get("payload") is not None else None
            )
            return answer, [Step.model_validate(x) for x in cached.get("steps", [])], call

        if self.offline or self.inner is None:
            raise ProposalError(
                f"offline mode and no recorded investigation for {answer_tool} ({key}). "
                "Record a live run first."
            )

        answer, steps, call = self.inner.investigate(
            answer_model, system=system, user=user, answer_tool=answer_tool,
            lookups=lookups, run_lookup=run_lookup, prompt_version=prompt_version,
            max_steps=max_steps, reasoning_effort=reasoning_effort, max_tokens=max_tokens,
        )
        path.write_text(json.dumps({
            "payload": answer.model_dump(mode="json") if answer else None,
            "steps": [x.model_dump(mode="json") for x in steps],
            "call": call.model_dump(mode="json"),
        }, indent=2))
        return answer, steps, call


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
