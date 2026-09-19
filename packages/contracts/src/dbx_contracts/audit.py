"""Append-only audit. Corrections add events; nothing rewrites history."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

from .provenance import SourceRef


class Actor(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    MOCK_API = "mock_api"
    SYSTEM = "system"


class AuditEvent(BaseModel):
    id: str
    run_id: str
    actor: Actor
    action: str  # e.g. "mapping.auto_applied", "delivery.retried"
    summary: str  # plain language, shown first
    at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))

    record_id: str | None = None
    case_id: str | None = None
    target_field: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)

    before: object = None
    after: object = None
    reason: str | None = None
    detail: dict = Field(default_factory=dict)  # expandable technical payload

    # model call metadata (TD004). Chain-of-thought is never stored.
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int | None = None
    tokens: dict[str, int] | None = None
