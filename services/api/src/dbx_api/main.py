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
from dbx_agent import build_provider, recommend_schema
from dbx_contracts import Action, Actor, MigrationSchema
from dbx_extraction import confidence_for, crop, is_sidecar, read
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .delivery import DestinationClient, Outcome
from .jobs import jobs
from .runtime import case_key, replay, source_profiles
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
    jobs.start(run_id, lambda report: replay(store, run_id, report=report)[0])
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
        if is_sidecar(path):
            # Copy it so OCR replays, but never offer it as employee data.
            shutil.copy(path, target / path.name)
            continue
        if path.suffix.lower() in (".csv", ".xlsx", ".json", ".yaml", ".yml", ".pdf"):
            shutil.copy(path, target / path.name)
            saved.append(str(target / path.name))
    with store.connect() as conn:
        conn.execute("UPDATE runs SET files_json = ? WHERE id = ?",
                     (json.dumps(saved), run_id))
    destination.register_schema(_schema_for_destination(schema))
    jobs.start(run_id, lambda report: replay(store, run_id, report=report)[0])
    return {"run_id": run_id}


class SchemaUpload(BaseModel):
    """A schema supplied as JSON or YAML text, or as an already-parsed object."""

    body: str | None = None
    schema_obj: dict[str, Any] | None = None
    origin: str = "supplied"


def _parse_schema(upload: SchemaUpload) -> tuple[MigrationSchema, str]:
    """Normalise a form, a JSON upload and a YAML upload into one representation.

    The original text is kept alongside, because a consultant who uploaded YAML
    should be able to see what they uploaded, not our re-rendering of it.
    """
    if upload.schema_obj is not None:
        raw = upload.schema_obj
    elif upload.body:
        try:
            raw = yaml.safe_load(upload.body)   # YAML is a superset of JSON
        except yaml.YAMLError as exc:
            raise HTTPException(422, f"could not parse the schema: {exc}") from exc
    else:
        raise HTTPException(422, "no schema supplied")

    try:
        return MigrationSchema.model_validate(raw), (upload.body or json.dumps(raw, indent=2))
    except Exception as exc:
        raise HTTPException(422, f"that is not a usable schema: {exc}") from exc


@app.get("/api/runs/{run_id}/schema")
def get_schema(run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    approved = store.approved_schema(run_id)
    active = json.loads(approved["body"]) if approved else json.loads(run["schema_json"])
    return {
        "active": active,
        "approved_version": approved["version"] if approved else None,
        "versions": store.schema_versions(run_id),
    }


@app.post("/api/runs/{run_id}/schema")
def put_schema(run_id: str, upload: Annotated[SchemaUpload, Body()]) -> dict[str, Any]:
    """Record a draft. Drafts do not affect processing until they are approved."""
    if store.get_run(run_id) is None:
        raise HTTPException(404, "no such run")
    schema, original = _parse_schema(upload)
    version = store.add_schema_version(
        run_id, schema.model_dump_json(), origin=upload.origin, original=original
    )
    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": "schema.drafted",
        "summary": f"Draft schema v{version} ({upload.origin}), {len(schema.fields)} fields",
        "at": now(),
    }])
    return {"version": version, "state": "draft", "fields": len(schema.fields)}


@app.post("/api/runs/{run_id}/schema/recommend")
def recommend(run_id: str) -> dict[str, Any]:
    """Mode B: propose a destination shape from the source data.

    The common engagement is a client with a pile of exports and no target defined.
    The result is a draft a human edits and approves, never a contract.
    """
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")

    profiles = source_profiles(store, run_id)
    if not profiles:
        raise HTTPException(422, "no readable source columns to learn from")

    provider = build_provider(Path("tests/fixtures/model-cache"))
    try:
        proposal, call = recommend_schema(provider, profiles)
    except Exception as exc:
        raise HTTPException(503, f"the model could not propose a schema: {exc}") from exc

    draft = {
        "schema_version": 1,
        "entity": proposal.entity,
        "description": "Proposed by the agent from the uploaded files. Edit before approving.",
        "fields": [
            {k: v for k, v in {
                "name": f.name, "type": f.type, "required": f.required,
                "unique": f.unique, "allowed": f.allowed or None, "format": f.format,
                "description": f.reason,
            }.items() if v not in (None, [], False) or k in ("required", "name", "type")}
            for f in proposal.fields
        ],
    }
    schema = MigrationSchema.model_validate(draft)
    version = store.add_schema_version(
        run_id, schema.model_dump_json(), origin="recommended",
        original=json.dumps(draft, indent=2),
    )
    store.append_audit(run_id, [{
        "actor": Actor.AGENT.value, "action": "schema.recommended",
        "summary": f"Proposed a {len(schema.fields)}-field schema for {schema.entity}",
        "at": now(), "provider": call.provider, "model": call.model,
        "prompt_version": call.prompt_version, "latency_ms": call.latency_ms,
    }])
    return {"version": version, "state": "draft", "schema": draft}


@app.post("/api/runs/{run_id}/schema/{version}/approve")
def approve_schema(run_id: str, version: int) -> dict[str, Any]:
    """Approval freezes a version and reprocesses everything not yet delivered."""
    body = store.schema_body(run_id, version)
    if body is None:
        raise HTTPException(404, "no such schema version")

    delivered = len(store.accepted_keys(run_id))
    store.approve_schema(run_id, version)
    with store.connect() as conn:
        conn.execute("UPDATE runs SET schema_json = ? WHERE id = ?", (body, run_id))

    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": "schema.approved",
        "summary": (
            f"Approved schema v{version}"
            + (f"; {delivered} already-delivered record(s) keep the version they were "
               "sent under" if delivered else "")
        ),
        "at": now(),
    }])
    jobs.invalidate(run_id)
    jobs.start(run_id, lambda report: replay(store, run_id, report=report)[0])
    return {"approved": version, "delivered_unchanged": delivered}


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


def _ensure_processing(run_id: str) -> Any | None:
    """Return the cached result, or start the work and let the caller poll."""
    cached = jobs.result(run_id)
    if cached is not None:
        return cached
    if not jobs.running(run_id):
        jobs.start(run_id, lambda report: replay(store, run_id, report=report)[0])
    return None


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, wait: Annotated[bool, Query()] = False) -> dict[str, Any]:
    if store.get_run(run_id) is None:
        raise HTTPException(404, "no such run")

    result = _ensure_processing(run_id)
    if result is None and wait:
        jobs.wait(run_id)
        result = jobs.result(run_id)

    progress = jobs.progress(run_id)
    if result is None:
        # Still working. Answer with what is known so the live view has something
        # to show rather than an empty screen.
        return {
            "run_id": run_id, "status": "processing", "progress": progress.as_dict(),
            "counts": {"records": 0, "ready": 0, "delivered": 0, "blocked": 0,
                       "excluded": 0, "open_cases": 0},
            "cases": [], "records": [], "mappings": [],
            "activity": [{"actor": "agent", "action": progress.stage,
                          "summary": progress.message, "reason": None,
                          "before": None, "after": None}],
        }

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
        "progress": progress.as_dict(),
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
    if deliverable:
        # Nothing blocked, nothing sent: the agent is finished and waiting on a click.
        return "ready_to_send"
    return "processing"


@app.post("/api/runs/{run_id}/cases/{key}/decide")
def decide(run_id: str, key: str, body: Annotated[Decide, Body()]) -> dict[str, Any]:
    jobs.wait(run_id, timeout=180)
    result = jobs.result(run_id)
    if result is None:
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

    jobs.invalidate(run_id)
    after, _ = replay(store, run_id)
    jobs.start(run_id, lambda report: replay(store, run_id, report=report)[0])
    jobs.wait(run_id, timeout=180)
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
    jobs.wait(run_id, timeout=180)
    result = jobs.result(run_id)
    if result is None:
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

    jobs.invalidate(run_id)
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
    jobs.invalidate(run_id)
    return {
        "attempted": len(results), "succeeded": succeeded,
        "partial": succeeded != len(results), "records": results,
    }


@app.get("/api/runs/{run_id}/destination")
def destination_state(run_id: str) -> dict[str, Any]:
    """What the destination actually holds. Read from the service, not from our guess."""
    try:
        with destination._client() as client:
            stored = client.get("/records", params={"run_id": run_id,
                                                    "include_rolled_back": True}).json()
    except Exception as exc:
        raise HTTPException(503, f"destination unreachable: {exc}") from exc
    return {"records": stored, "attempts": store.deliveries(run_id)}


@app.get("/api/runs/{run_id}/crop")
def crop_image(
    run_id: str,
    file: Annotated[str, Query()],
    page: Annotated[int, Query()],
    column: Annotated[str, Query()],
    row: Annotated[int, Query()],
) -> Response:
    """The picture of the thing the agent could not read.

    A confidence score tells a consultant nothing they can act on. The cropped scan
    region does: they can see the smudge and type what it says.
    """
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    path = next(
        (Path(p) for p in json.loads(run["files_json"]) if Path(p).name == file), None
    )
    if path is None or not path.exists():
        raise HTTPException(404, "no such source file in this run")

    records = read(path)
    target = next(
        (r for r in records if r.source.row == row and r.source.page == page), None
    )
    cells = confidence_for(target.id) if target else {}
    cell = cells.get(column)
    if cell is None or cell.bbox is None:
        raise HTTPException(404, "no image region recorded for that value")
    png = crop(path, cell.page, cell.bbox, normalized=True)
    return Response(content=png, media_type="image/png")


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
