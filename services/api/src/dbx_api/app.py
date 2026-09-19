"""The migration API.

Backend state is authoritative: nothing here reports success optimistically. A record
is delivered when the destination says it holds it, never when a request was sent.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated, Any

import yaml
from dbx_contracts import Action, Actor, MigrationSchema
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .delivery import DestinationClient, Outcome
from .runtime import case_key, replay
from .store import Store, now

DATA = Path(os.environ.get("DBX_DATA_DIR", ".artifacts"))
UPLOADS = DATA / "uploads"
store = Store(DATA / "migration.db")
destination = DestinationClient(os.environ.get("MOCK_TARGET_URL", "http://localhost:8081"))

app = FastAPI(title="Darwinbox data bridge", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

DEFAULT_SCHEMA = Path("tests/fixtures/schemas/target_schema.yaml")


class Decide(BaseModel):
    action: str
    value: str | None = None
    reason: str | None = None


def _schema_for_destination(schema: MigrationSchema) -> dict[str, Any]:
    return {
        "version": schema.schema_version,
        "required": [f.name for f in schema.required_fields],
        "allowed": {f.name: f.allowed for f in schema.fields if f.allowed},
        "patterns": {f.name: f.pattern for f in schema.fields if f.pattern},
    }


def _case_payload(case: Any) -> dict[str, Any]:
    return {
        "key": case_key(case),
        "id": case.id,
        "class": case.klass.value,
        "headline": case.headline,
        "detail": case.detail,
        "record": case.record_key,
        "field": case.target_field,
        "sources": [r.label() for r in case.source_refs],
        "values": case.raw_values,
        "evidence": case.evidence,
        "rule": case.rule,
        "attempts": case.attempts,
        "actions": [a.value for a in case.actions],
        "options": [o.model_dump() for o in case.options],
        "blocks": len(case.blocks_records),
        "state": case.state.value,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    ok = True
    try:
        destination.reconcile("health", "none")
    except Exception:  # noqa: BLE001
        ok = False
    return {"status": "ok", "destination_reachable": ok}


@app.post("/api/runs")
async def create_run(
    files: Annotated[list[UploadFile], File()],
    label: Annotated[str | None, Form()] = None,
) -> dict[str, str]:
    schema = MigrationSchema.model_validate(yaml.safe_load(DEFAULT_SCHEMA.read_text()))
    run_id = store.create_run(schema.model_dump_json(), [], label)

    folder = UPLOADS / run_id
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for upload in files:
        target = folder / Path(upload.filename or "upload.csv").name
        with target.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        saved.append(str(target))

    with store.connect() as conn:
        conn.execute("UPDATE runs SET files_json = ? WHERE id = ?",
                     (json.dumps(saved), run_id))

    destination.register_schema(_schema_for_destination(schema))
    return {"run_id": run_id}


@app.post("/api/runs/from-fixtures")
def create_run_from_fixtures(
    folder: Annotated[str, Body(embed=True)] = "run1",
) -> dict[str, str]:
    """Start a run from the bundled fixtures, so the demo needs no file picker."""
    source = Path("tests/fixtures") / folder
    if not source.is_dir():
        raise HTTPException(404, f"no fixture folder named {folder!r}")
    schema = MigrationSchema.model_validate(yaml.safe_load(DEFAULT_SCHEMA.read_text()))
    run_id = store.create_run(schema.model_dump_json(), [], f"fixtures/{folder}")
    target = UPLOADS / run_id
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for path in sorted(source.iterdir()):
        if path.suffix.lower() in (".csv", ".xlsx"):
            shutil.copy(path, target / path.name)
            saved.append(str(target / path.name))
    with store.connect() as conn:
        conn.execute("UPDATE runs SET files_json = ? WHERE id = ?",
                     (json.dumps(saved), run_id))
    destination.register_schema(_schema_for_destination(schema))
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    out = []
    for run in store.list_runs():
        delivered = len(store.accepted_keys(run["id"]))
        out.append({
            "id": run["id"], "created_at": run["created_at"], "label": run["label"],
            "status": run["status"], "files": len(json.loads(run["files_json"])),
            "delivered": delivered,
        })
    return out


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        result, _ = replay(store, run_id)
    except KeyError as exc:
        raise HTTPException(404, "no such run") from exc

    store.append_audit(run_id, [e.model_dump(mode="json") for e in result.audit])
    accepted = store.accepted_keys(run_id)
    decided = {d["case_key"] for d in store.decisions(run_id)}

    records = []
    for record in result.records:
        key = record.natural_key or record.id
        records.append({
            "key": key,
            "state": "delivered" if key in accepted else record.state.value,
            "values": {k: str(v) for k, v in record.values.items()},
            "sources": [r.label() for r in record.contributing],
            "issues": [i.message for i in record.validation.errors],
            "cases": record.open_cases,
            "provenance": {
                name: {
                    "raw": p.raw_value, "value": str(p.value),
                    "from": p.source.label(),
                    "changes": [
                        {"rule": t.rule, "before": t.before, "after": t.after,
                         "why": t.reason} for t in p.transformations
                    ],
                } for name, p in record.provenance.items()
            },
        })

    open_cases = [c for c in result.cases if case_key(c) not in decided]
    ready = [r for r in records if r["state"] == "ready"]
    return {
        "run_id": run_id,
        "status": _status(result, records, accepted),
        "counts": {
            "records": len(records),
            "ready": len(ready),
            "delivered": len([r for r in records if r["state"] == "delivered"]),
            "blocked": len([r for r in records if r["state"] == "blocked"]),
            "excluded": len([r for r in records if r["state"] == "excluded"]),
            "open_cases": len(open_cases),
        },
        "cases": [_case_payload(c) for c in open_cases],
        "records": records,
        "mappings": [
            {
                "file": m.source_file, "column": m.source_column,
                "decision": m.decision.value, "field": m.chosen_field,
                "score": m.best.score if m.best else 0.0, "gap": m.gap,
                "evidence": m.best.evidence.explain() if m.best else [],
            } for m in result.mappings
        ],
        "activity": [
            {"actor": e.actor.value, "action": e.action, "summary": e.summary,
             "reason": e.reason, "before": e.before, "after": e.after}
            for e in result.audit
        ][-60:],
    }


def _status(result: Any, records: list[dict], accepted: dict[str, int]) -> str:
    """Derived from record state, never chosen. A run cannot be complete by neglect."""
    blocked = [r for r in records if r["state"] == "blocked"]
    delivered = [r for r in records if r["state"] == "delivered"]
    excluded = [r for r in records if r["state"] == "excluded"]
    deliverable = [r for r in records if r["state"] in ("ready", "delivered")]

    if blocked:
        return "partially_delivered" if delivered else "awaiting_review"
    if delivered and len(delivered) == len(deliverable):
        return "completed_with_exclusions" if excluded else "completed"
    if delivered:
        return "partially_delivered"
    return "processing"


@app.post("/api/runs/{run_id}/cases/{key}/decide")
def decide(run_id: str, key: str, body: Annotated[Decide, Body()]) -> dict[str, Any]:
    result, _ = replay(store, run_id)
    case = next((c for c in result.cases if case_key(c) == key), None)
    if case is None:
        raise HTTPException(404, "no such case")

    action = Action(body.action)
    if action is Action.APPROVE and not case.has_proposal:
        # Approval must never be a way past a failed check.
        raise HTTPException(422, "this case has no proposal to approve")

    store.record_decision(
        run_id, key, case.klass.value, action.value, body.value, body.reason,
        {"headline": case.headline, "record": case.record_key, "field": case.target_field},
    )
    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": f"review.{action.value}",
        "summary": f"{action.value.title()}d: {case.headline}",
        "reason": body.reason, "after": body.value, "at": now(),
    }])

    after, _ = replay(store, run_id)
    remaining = [c for c in after.cases
                 if case_key(c) not in {d["case_key"] for d in store.decisions(run_id)}]
    return {
        "resolved": key,
        "open_cases": len(remaining),
        "ready": len(after.ready),
        "blocked": len(after.blocked),
    }


@app.post("/api/runs/{run_id}/deliver")
def deliver(run_id: str) -> dict[str, Any]:
    result, _ = replay(store, run_id)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    schema = MigrationSchema.model_validate(json.loads(run["schema_json"]))
    destination.register_schema(_schema_for_destination(schema))

    already = store.accepted_keys(run_id)
    sent = {"accepted": 0, "duplicate": 0, "rejected": 0, "failed": 0, "uncertain": 0}
    per_record = []

    for record in result.ready:
        key = record.natural_key or record.id
        if key in already:
            continue  # resuming a run must not resend what the destination has
        generation = already.get(key, 1)
        payload = {k: (v if not hasattr(v, "isoformat") else v.isoformat())
                   for k, v in record.values.items()}
        attempts = destination.deliver(
            run_id, key, payload, schema_version=schema.schema_version,
            generation=generation,
        )
        for attempt in attempts:
            store.record_delivery(
                run_id, natural_key=key, generation=generation, attempt=attempt.attempt,
                outcome=attempt.outcome.value, status_code=attempt.status_code,
                target_record_id=attempt.target_record_id, payload=payload,
                response=attempt.response or {"error": attempt.error},
            )
        final = attempts[-1]
        bucket = {
            Outcome.ACCEPTED: "accepted", Outcome.DUPLICATE: "duplicate",
            Outcome.REJECTED: "rejected", Outcome.UNCERTAIN: "uncertain",
        }.get(final.outcome, "failed")
        sent[bucket] += 1
        per_record.append({
            "record": key, "outcome": final.outcome.value,
            "attempts": len(attempts),
            "errors": (final.response or {}).get("errors", []) if final.response else [],
        })
        store.append_audit(run_id, [{
            "actor": Actor.MOCK_API.value, "action": f"delivery.{final.outcome.value}",
            "summary": f"{key}: {final.outcome.value} after {len(attempts)} attempt(s)",
            "at": now(), "after": final.target_record_id,
        }])

    return {"sent": sent, "records": per_record}


@app.post("/api/runs/{run_id}/rollback")
def rollback(run_id: str) -> dict[str, Any]:
    rows = [d for d in store.deliveries(run_id)
            if d["outcome"] in ("accepted", "duplicate") and d["target_record_id"]]
    accepted = store.accepted_keys(run_id)
    results = []
    for row in rows:
        if row["natural_key"] not in accepted:
            continue
        ok, response = destination.rollback(row["target_record_id"])
        store.record_delivery(
            run_id, natural_key=row["natural_key"],
            generation=row["generation"] + 1, attempt=1,
            outcome="rolled_back" if ok else "rollback_failed",
            target_record_id=row["target_record_id"], payload=row["payload"],
            response=response,
        )
        results.append({"record": row["natural_key"], "ok": ok})
    succeeded = sum(1 for r in results if r["ok"])
    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": "delivery.rollback",
        "summary": f"Rolled back {succeeded} of {len(results)} delivered record(s)",
        "at": now(),
    }])
    return {
        "attempted": len(results), "succeeded": succeeded,
        "partial": succeeded != len(results), "records": results,
    }


@app.get("/api/runs/{run_id}/destination")
def destination_state(run_id: str) -> dict[str, Any]:
    """What the destination actually holds. Read from the service, not from our guess."""
    try:
        with __import__("httpx").Client(base_url=destination.base_url, timeout=5) as client:
            stored = client.get("/records", params={"run_id": run_id,
                                                    "include_rolled_back": True}).json()
    except Exception as exc:
        raise HTTPException(503, f"destination unreachable: {exc}") from exc
    return {"records": stored, "attempts": store.deliveries(run_id)}


@app.get("/api/runs/{run_id}/audit")
def audit(run_id: str) -> list[dict[str, Any]]:
    return store.audit(run_id)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict[str, str]:
    store.delete_run(run_id)
    shutil.rmtree(UPLOADS / run_id, ignore_errors=True)
    return {"deleted": run_id}


WEB = Path("apps/web/dist")
if WEB.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        return FileResponse(WEB / "index.html")
