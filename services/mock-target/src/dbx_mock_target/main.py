"""A working stub destination.

Deliberately not a simulation. It validates against a registered schema, stores what
it accepts, and the migration UI reads its state back over HTTP — so "delivered"
means this service holds the record, not that a request was attempted.

The failure simulator only produces transport-level trouble (timeouts, 503s,
uncertain outcomes). Permanent failures come from real schema validation, because a
rejection the destination did not actually make proves nothing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .store import Store

DB = Path(os.environ.get("MOCK_TARGET_DB", ".artifacts/mock-target.db"))
store = Store(DB)
app = FastAPI(title="Mock destination HRMS", version="1.0.0")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class RegisteredSchema(BaseModel):
    version: int
    required: list[str] = Field(default_factory=list)
    allowed: dict[str, list[str]] = Field(default_factory=dict)
    patterns: dict[str, str] = Field(default_factory=dict)


class DeliveryRequest(BaseModel):
    run_id: str
    natural_key: str
    schema_version: int
    generation: int = 1
    payload: dict[str, Any]


class DeliveryOutcome(BaseModel):
    outcome: str                      # accepted | duplicate | rejected | conflict
    record_id: str | None = None
    received_at: str | None = None
    errors: list[str] = Field(default_factory=list)


class FailureMode(BaseModel):
    """Transport trouble on demand, for demonstrating retry and recovery."""

    mode: str = "none"                # none | transient | timeout | uncertain
    remaining: int = 0


_failure = FailureMode()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/failure-mode")
def set_failure(mode: Annotated[FailureMode, Body()]) -> FailureMode:
    global _failure
    _failure = mode
    return _failure


@app.post("/admin/reset")
def reset() -> dict[str, str]:
    store.reset()
    return {"status": "reset"}


@app.post("/schemas")
def register_schema(spec: Annotated[RegisteredSchema, Body()]) -> dict[str, int]:
    with store.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO registered_schemas (version, body, registered_at)"
            " VALUES (?,?,?)",
            (spec.version, spec.model_dump_json(), _now()),
        )
    return {"version": spec.version}


def _load_schema(version: int) -> RegisteredSchema | None:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT body FROM registered_schemas WHERE version = ?", (version,)
        ).fetchone()
    return RegisteredSchema.model_validate_json(row["body"]) if row else None


def _validate(payload: dict[str, Any], spec: RegisteredSchema) -> list[str]:
    import re

    errors: list[str] = []
    for name in spec.required:
        if payload.get(name) in (None, ""):
            errors.append(f"{name} is required")
    for name, allowed in spec.allowed.items():
        value = payload.get(name)
        if value not in (None, "") and str(value) not in allowed:
            errors.append(f"{name}={value!r} is not permitted")
    for name, pattern in spec.patterns.items():
        value = payload.get(name)
        if value not in (None, "") and not re.match(pattern, str(value)):
            errors.append(f"{name}={value!r} does not match {pattern}")
    return errors


def _maybe_fail(idempotency_key: str, body: DeliveryRequest) -> None:
    """Transport failures, injected before any state change except the uncertain case."""
    if _failure.mode == "none" or _failure.remaining <= 0:
        return
    _failure.remaining -= 1
    mode = _failure.mode

    if mode == "transient":
        store.log(at=_now(), run_id=body.run_id, natural_key=body.natural_key,
                  idempotency_key=idempotency_key, outcome="transient_failure",
                  status=503, detail={"simulated": True})
        raise HTTPException(503, "destination temporarily unavailable")
    if mode == "timeout":
        time.sleep(float(os.environ.get("MOCK_TARGET_TIMEOUT_SECONDS", "8")))
        raise HTTPException(504, "gateway timeout")
    if mode == "uncertain":
        # The dangerous one: the write lands, the caller never learns. Only a
        # reconcile-by-identity lookup can tell the difference from a lost request.
        _store_record(idempotency_key, body)
        store.log(at=_now(), run_id=body.run_id, natural_key=body.natural_key,
                  idempotency_key=idempotency_key, outcome="uncertain",
                  status=500, detail={"simulated": True, "stored": True})
        raise HTTPException(500, "internal error after write")


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _store_record(idempotency_key: str, body: DeliveryRequest) -> str:
    record_id = str(uuid.uuid4())
    with store.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO target_records (id, idempotency_key, run_id,"
            " natural_key, schema_version, generation, payload, payload_hash, state,"
            " received_at) VALUES (?,?,?,?,?,?,?,?, 'accepted', ?)",
            (record_id, idempotency_key, body.run_id, body.natural_key,
             body.schema_version, body.generation, json.dumps(body.payload),
             _hash(body.payload), _now()),
        )
    return record_id


@app.post("/records", response_model=DeliveryOutcome)
def create_record(
    body: Annotated[DeliveryRequest, Body()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> DeliveryOutcome:
    with store.connect() as conn:
        existing = conn.execute(
            "SELECT * FROM target_records WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()

    if existing:
        # Same key, same payload: hand back the original result. Same key, different
        # payload: that is a caller bug, and silently overwriting would hide it.
        if existing["payload_hash"] != _hash(body.payload):
            store.log(at=_now(), run_id=body.run_id, natural_key=body.natural_key,
                      idempotency_key=idempotency_key, outcome="conflict", status=409)
            raise HTTPException(409, "idempotency key reused with a different payload")
        store.log(at=_now(), run_id=body.run_id, natural_key=body.natural_key,
                  idempotency_key=idempotency_key, outcome="duplicate", status=200)
        return DeliveryOutcome(outcome="duplicate", record_id=existing["id"],
                               received_at=existing["received_at"])

    _maybe_fail(idempotency_key, body)

    spec = _load_schema(body.schema_version)
    if spec is None:
        raise HTTPException(400, f"schema version {body.schema_version} is not registered")
    errors = _validate(body.payload, spec)
    if errors:
        store.log(at=_now(), run_id=body.run_id, natural_key=body.natural_key,
                  idempotency_key=idempotency_key, outcome="rejected", status=422,
                  detail={"errors": errors})
        return DeliveryOutcome(outcome="rejected", errors=errors)

    record_id = _store_record(idempotency_key, body)
    store.log(at=_now(), run_id=body.run_id, natural_key=body.natural_key,
              idempotency_key=idempotency_key, outcome="accepted", status=201)
    return DeliveryOutcome(outcome="accepted", record_id=record_id, received_at=_now())


@app.get("/records")
def list_records(
    run_id: Annotated[str | None, Query()] = None,
    natural_key: Annotated[str | None, Query()] = None,
    include_rolled_back: Annotated[bool, Query()] = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM target_records WHERE 1=1"
    params: list[object] = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    if natural_key:
        sql += " AND natural_key = ?"
        params.append(natural_key)
    if not include_rolled_back:
        sql += " AND state = 'accepted'"
    sql += " ORDER BY received_at"
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {**dict(r), "payload": json.loads(r["payload"])} for r in rows
    ]


@app.post("/records/{record_id}/rollback")
def rollback(record_id: str) -> dict[str, str]:
    """Tombstone, never delete. History is the point of an audit trail."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT state FROM target_records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no such record")
        if row["state"] != "accepted":
            return {"outcome": "already_rolled_back", "record_id": record_id}
        conn.execute(
            "UPDATE target_records SET state='rolled_back', rolled_back_at=? WHERE id=?",
            (_now(), record_id),
        )
    store.log(at=_now(), run_id=None, natural_key=None, idempotency_key=None,
              outcome="rolled_back", status=200, detail={"record_id": record_id})
    return {"outcome": "rolled_back", "record_id": record_id}


@app.get("/requests")
def request_log(limit: Annotated[int, Query(le=500)] = 100) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
