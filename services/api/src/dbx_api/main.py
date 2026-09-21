"""The migration API.

Backend state is authoritative: nothing here reports success optimistically. A record
is delivered when the destination says it holds it, never when a request was sent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Any

import yaml
from dbx_agent import build_provider, recommend_schema, vote_on_column
from dbx_contracts import Action, Actor, EscalationClass, MigrationSchema
from dbx_extraction import (
    UnsupportedInput,
    confidence_for,
    crop,
    is_sidecar,
    read,
    sniff,
)
from dbx_migration_core import mask_to_pattern
from dbx_migration_core.scoring import score_pair
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .delivery import DestinationClient, Outcome
from .jobs import jobs
from .runtime import case_key, load_lookups, lookup_columns, replay, source_profiles
from .store import Store, now

log = logging.getLogger("dbx.api")

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


def _case_payload(case: Any, names: dict[str, str] | None = None) -> dict[str, Any]:
    names = names or {}
    return {
        # Who this is about, as a person rather than a key.
        "who": names.get(case.record_key or "", ""),
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
        # What the agent went and checked before asking, so the question arrives
        # with its legwork attached rather than as a bare request for help.
        "checked": [c.model_dump() for c in case.checked],
        "found": case.found,
        "blocks": case.waiting_count,
        "children": len(case.child_records),
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
) -> dict[str, Any]:
    # A run starts with files and no schema. Processing cannot begin until a human
    # has supplied or approved one — mapping against a schema nobody agreed to would
    # make every downstream decision unaccountable.
    run_id = store.create_run("", [], label)

    folder = UPLOADS / run_id
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for upload in files:
        target = folder / Path(upload.filename or "upload.csv").name
        with target.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        saved.append(str(target))

    with store.connect() as conn:
        conn.execute("UPDATE runs SET files_json = ?, status = 'awaiting_schema'"
                     " WHERE id = ?", (json.dumps(saved), run_id))
    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": "run.created",
        "summary": f"Uploaded {len(saved)} file(s)", "at": now(),
    }])
    return {"run_id": run_id, "files": len(saved), "status": "awaiting_schema"}


SAMPLE_BLURB = {
    "01-clean": "Four employees, correct formats. Nothing should need your attention.",
    "02-messy": "Seven files in five naming conventions, including a scanned page. The real one.",
    "03-conflicts": "The same people in two systems that disagree, plus a namesake.",
    "04-edge-cases": "Empty, malformed, disguised and oversized input. Should not crash.",
    "05-adversarial": "A prompt injection, a SQL fragment, and a file of pure noise.",
}


@app.get("/api/samples")
def list_samples() -> list[dict[str, Any]]:
    """Sample sets bundled with the app, so a reviewer needs no files of their own."""
    root = Path("samples")
    if not root.is_dir():
        return []
    out = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        usable = [
            f for f in folder.iterdir()
            if f.is_file() and not is_sidecar(f) and f.suffix.lower() in
            (".csv", ".xlsx", ".json", ".yaml", ".yml", ".pdf")
        ]
        out.append({
            "name": folder.name,
            "files": len(usable),
            "description": SAMPLE_BLURB.get(folder.name, ""),
        })
    return out


@app.post("/api/runs/from-sample")
def create_run_from_sample(name: Annotated[str, Body(embed=True)]) -> dict[str, Any]:
    source = Path("samples") / name
    if not source.is_dir():
        raise HTTPException(404, f"no sample set named {name!r}")
    return _stage_run(source, label=f"sample/{name}")


@app.post("/api/runs/from-fixtures")
def create_run_from_fixtures(
    folder: Annotated[str, Body(embed=True)] = "run1",
) -> dict[str, Any]:
    """Start a run from the bundled fixtures, so the demo needs no file picker."""
    source = Path("tests/fixtures") / folder
    if not source.is_dir():
        raise HTTPException(404, f"no fixture folder named {folder!r}")
    return _stage_run(source, label=f"fixtures/{folder}")


def _stage_run(source: Path, *, label: str) -> dict[str, Any]:
    """Copy a bundled folder into a new run. No schema, so nothing starts."""
    run_id = store.create_run("", [], label)
    target = UPLOADS / run_id
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        if is_sidecar(path):
            # Copy it so OCR replays, but never offer it as employee data.
            shutil.copy(path, target / path.name)
            continue
        if path.suffix.lower() in (".csv", ".xlsx", ".json", ".yaml", ".yml", ".pdf"):
            shutil.copy(path, target / path.name)
            saved.append(str(target / path.name))
    with store.connect() as conn:
        conn.execute("UPDATE runs SET files_json = ?, status = 'awaiting_schema'"
                     " WHERE id = ?", (json.dumps(saved), run_id))
    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": "run.created",
        "summary": f"Loaded {len(saved)} file(s) from {label}", "at": now(),
    }])
    return {"run_id": run_id, "files": len(saved), "status": "awaiting_schema"}


@app.get("/api/runs/{run_id}/files")
def run_files(run_id: str) -> dict[str, Any]:
    """What was uploaded and what was found in it, before any schema is chosen.

    A consultant should see that their files were read — and which ones could not be —
    before being asked to make decisions about them.
    """
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")

    out = []
    for raw in json.loads(run["files_json"]):
        path = Path(raw)
        detected = sniff(path)
        entry: dict[str, Any] = {
            "name": path.name,
            "kind": detected.kind.value,
            "supported": detected.supported,
            "note": detected.note,
            "rows": 0,
            "columns": [],
        }
        if detected.supported:
            try:
                records = read(path)
                entry["rows"] = len(records)
                entry["columns"] = list(records[0].values) if records else []
            except UnsupportedInput as exc:
                entry["supported"] = False
                entry["note"] = str(exc)
        out.append(entry)
    return {
        "run_id": run_id,
        "files": out,
        "total_rows": sum(f["rows"] for f in out),
        "total_columns": sum(len(f["columns"]) for f in out),
    }


@app.get("/api/runs/{run_id}/schema/starter")
def schema_starter(run_id: str) -> dict[str, Any]:
    """A worked example to edit, for someone who wants to write their own."""
    return {"body": DEFAULT_SCHEMA.read_text()}


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
    draft = store.latest_draft(run_id)

    # Show the newest draft when nothing is approved yet. Without this a proposal the
    # agent just made is invisible — the version exists but nothing renders it, which
    # looks exactly like the agent having done nothing.
    showing = approved or draft
    return {
        "active": json.loads(showing["body"]) if showing else None,
        "showing_version": showing["version"] if showing else None,
        "showing_state": showing["state"] if showing else None,
        "origin": showing["origin"] if showing else None,
        "approved_version": approved["version"] if approved else None,
        "draft_version": draft["version"] if draft else None,
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

    # The model reliably names the fields and rarely marks any of them unique — and a
    # schema with nothing unique has no blocking keys, so reconciliation never runs and
    # the same person arrives several times. The data already says which columns are
    # unique, so say it in the schema rather than hoping the model does.
    #: A column of values that happen to be distinct is not an identifier. In a
    #: 24-row sample every first name is different too, and marking that unique would
    #: reject two people called Priya. So a field qualifies only if it also *looks*
    #: like an identifier: an email, a coded value, or a name that says so.
    id_name = re.compile(r"(^|_)(id|code|ref|reference|number|no|key|uid)$")
    profiles_by_key: dict[str, Any] = {}
    for profile in profiles:
        # A four-row file makes anything look distinct. Judge on a real sample.
        if profile.non_null >= 8:
            key = re.sub(r"[^a-z0-9]+", "", profile.raw_name.casefold())
            best = profiles_by_key.get(key)
            if best is None or profile.cardinality_ratio > best.cardinality_ratio:
                profiles_by_key[key] = profile

    def _looks_like_a_lookup_value(name: str, by_key: dict[str, Any]) -> bool:
        """Does a small set of values repeat across the run? Then it is a category."""
        key = re.sub(r"[^a-z0-9]+", "", name.casefold())
        ratios = [
            p.cardinality_ratio for column, p in by_key.items()
            if (column == key or column.endswith(key) or key.endswith(column))
        ]
        return bool(ratios) and min(ratios) < 0.9

    def identifier_like(name: str, field_type: str) -> bool:
        key = re.sub(r"[^a-z0-9]+", "", name.casefold())
        for column, profile in profiles_by_key.items():
            if not (column == key or column.endswith(key) or key.endswith(column)):
                continue
            if profile.cardinality_ratio < 0.99:
                continue
            if field_type == "email" or id_name.search(name):
                return True
            mask = profile.dominant_mask()
            # A coded value: one consistent shape, with both letters and digits in it.
            if mask and mask.coverage >= 0.9 and "#" in mask.mask and (
                "A" in mask.mask or "a" in mask.mask
            ):
                return True
        return False

    #: Columns that are plumbing rather than facts about a person. The model is told
    #: to skip them and mostly does not, so they are dropped here.
    plumbing = re.compile(
        r"(^|_)(row|record|seq)(_?(id|no|num|number))?$|checksum|hash|"
        r"^(created|updated|modified|changed)_?(by|at|on|ts|time)?$|"
        r"sync|etl|batch|source_system|legacy|version|is_deleted|deleted|"
        r"remark|comment|note|bank|acct|account_(no|number)|audit"
    )

    lookups = load_lookups(json.loads(run["files_json"]))
    lookup_cols = lookup_columns(json.loads(run["files_json"]))

    kept, dropped = [], []
    for field in proposal.fields:
        (dropped if plumbing.search(field.name.casefold()) else kept).append(field)
    if kept:
        proposal.fields = kept

    by_raw = {p.raw_name.strip().casefold(): p for p in profiles}

    def _aliases_for(field: Any, known: dict[str, Any]) -> list[str]:
        """The columns the agent says fed this field become the field's aliases.

        The agent named 'job_title' only because it read a column called
        'Designation'. Without writing that down, mapping has to rediscover a
        synonym it cannot measure, and scores it 0.31. Recording it makes the
        match exact and deterministic — and it lands in the draft schema, which
        a human reads and edits before anything runs.
        """
        out: list[str] = []
        for raw in field.sources:
            name = raw.strip()
            if not name or name.casefold() not in known:
                continue
            if _key(name) == _key(field.name) or name in out:
                continue
            out.append(name)
        return out

    def _key(text: str) -> str:
        return _norm(text)

    def feeding(field: Any) -> list[Any]:
        """The profiled columns a proposed field says it came from."""
        found = [by_raw[c.strip().casefold()] for c in field.sources
                 if c.strip().casefold() in by_raw]
        if found:
            return found
        # The model named no column, or named one that is not in the profiles.
        # Fall back to the field's own name, which is right for files that already
        # use destination spelling.
        key = re.sub(r"[^a-z0-9]+", "", field.name.casefold())
        hit = next((p for column, p in profiles_by_key.items()
                    if column == key or column.endswith(key) or key.endswith(column)), None)
        return [hit] if hit else []

    marked = 0
    for field in proposal.fields:
        columns = feeding(field)
        if not columns:
            continue
        source = max(columns, key=lambda p: p.non_null)

        # A field whose values all live in an uploaded lookup gets that rule, which is
        # the single strongest signal mapping has.
        for table, values in lookups.items():
            sampled = [v.value for v in source.top_values] or source.sample
            if sampled and sum(1 for v in sampled if v in values) / len(sampled) >= 0.9:
                field.reference = f"{table}.code"
                # The lookup file is the real universe. Keeping the model's list of
                # values-it-happened-to-see would reject every code outside this batch.
                field.allowed = []
                # An enum is defined by its allowed list, so one with the list taken
                # away is not a valid field at all. The reference now says what the
                # values may be, which is what `string` plus a reference means.
                if field.type == "enum":
                    field.type = "string"
                break

        if field.unique or field.reference:
            continue      # a lookup value identifies the lookup row, not the person
        if _looks_like_a_lookup_value(field.name, profiles_by_key):
            continue
        if not identifier_like(field.name, field.type):
            continue
        # Every column feeding it has to be an identifier, or it is not one.
        if min(p.cardinality_ratio for p in columns) < 0.99:
            continue
        field.unique = True
        marked += 1

    # Give identifiers the shape their values actually have, so mapping has evidence
    # beyond the column's name. Only where every source column agrees on the shape.
    patterned = 0
    for field in proposal.fields:
        if not field.unique or field.type != "string" or field.pattern:
            continue
        masks = [p.dominant_mask() for p in feeding(field)]
        if not masks or any(m is None or m.coverage < 0.95 for m in masks):
            continue
        shapes = {m.mask for m in masks if m}
        if len(shapes) != 1:
            continue
        field.pattern = mask_to_pattern(shapes.pop())
        patterned += 1 if field.pattern else 0

    draft = {
        "schema_version": 1,
        "entity": proposal.entity,
        "description": "Proposed by the agent from the uploaded files. Edit before approving.",
        "fields": [
            {k: v for k, v in {
                "name": f.name, "type": f.type, "required": f.required,
                "aliases": _aliases_for(f, by_raw),
                "unique": f.unique, "allowed": f.allowed or None, "format": f.format,
                "pattern": getattr(f, "pattern", None),
                "reference": getattr(f, "reference", None),
                "description": f.reason,
            }.items() if v not in (None, [], False) or k in ("required", "name", "type")}
            for f in proposal.fields
        ],
    }
    # A reference is only legal against a declared lookup. The uploaded file that
    # supplied the values is that declaration.
    referenced = {f.reference.split(".")[0] for f in proposal.fields if f.reference}
    if referenced:
        draft["lookups"] = [
            {"name": name, "key": "code", "fields": sorted(lookup_cols.get(name, ["code"]))}
            for name in sorted(referenced)
        ]
    schema = MigrationSchema.model_validate(draft)
    version = store.add_schema_version(
        run_id, schema.model_dump_json(), origin="recommended",
        original=json.dumps(draft, indent=2),
    )
    store.append_audit(run_id, [{
        "actor": Actor.AGENT.value, "action": "schema.recommended",
        "summary": (
            f"Proposed a {len(schema.fields)}-field schema for {schema.entity}"
            + (f"; left out {len(dropped)} system column(s)" if dropped else "")
            + (f"; {marked} field(s) identify a person" if marked else "")
            + (f"; {patterned} got the value shape seen in the data" if patterned else "")
        ),
        "at": now(), "provider": call.provider, "model": call.model,
        "prompt_version": call.prompt_version, "latency_ms": call.latency_ms,
    }])
    return {"version": version, "state": "draft", "schema": draft}


#: The model has to be fairly sure before a column is worth showing at all.
#:
#: Nothing stronger is available, and that is the finding rather than a gap. On
#: the labelled corpus the model votes 1.00 on `strAuditUser` meaning `designation`
#: and 1.00 on `dob` meaning `date_of_birth`, so its confidence separates nothing.
#: Measured fit does not separate them either: `role -> designation` is correct at
#: 0.047 and `strAuditUser -> designation` is wrong at 0.061, while the wrong
#: `dtProbationEnd -> date_of_joining` sits at 0.613, above most correct ones.
#: Filtering on either number would hide the two suggestions worth having. So the
#: list is shown with values attached, and a person decides.
ALIAS_VOTE_FLOOR = 0.80


@app.post("/api/runs/{run_id}/schema/{version}/aliases")
def suggest_aliases(run_id: str, version: int) -> dict[str, Any]:
    """Offer the source columns that mean the same as each field.

    'Designation' and 'job_title' are the same thing and nothing measurable says
    so - no shared tokens, no shared shape. Only meaning connects them, and the
    model is the only thing here that holds meaning.

    Nothing is applied. On this corpus the model votes 1.00 on suggestions that
    are flatly wrong - `dtProbationEnd` is not `date_of_joining` - and raising the
    vote threshold does not separate them, because it votes 1.00 on the correct
    ones too. So its confidence cannot be the gate. What can: the data must agree
    the column could hold that field, and then a person ticks the ones that are
    right.
    """
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    body = store.schema_body(run_id, version)
    if body is None:
        raise HTTPException(404, f"no schema version {version}")

    schema = MigrationSchema.model_validate(json.loads(body))
    by_name = {f.name: f for f in schema.fields}
    spoken_for = {
        _norm(name) for field in schema.fields for name in (field.name, *field.aliases)
    }

    provider = build_provider(Path("tests/fixtures/model-cache"))
    offers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for profile in source_profiles(store, run_id):
        raw = profile.raw_name.strip()
        key = _norm(raw)
        if not raw or key in spoken_for or key in seen:
            continue
        seen.add(key)
        try:
            votes, _ = vote_on_column(provider, profile, schema)
        except Exception as exc:  # noqa: BLE001 - one bad column must not lose the rest
            log.warning("alias vote failed for %s: %s", raw, exc)
            continue
        best = max(votes.items(), key=lambda kv: kv[1], default=None)
        if not best or best[1] < ALIAS_VOTE_FLOOR:
            continue
        field = by_name.get(best[0])
        if field is None:
            continue
        # A veto is a hard impossibility rather than a low opinion - the column
        # cannot hold this field's type at all - so it is the one automatic filter
        # that is safe here.
        evidence = score_pair(profile, field, llm_vote=None)
        if evidence.vetoes:
            continue
        offers.append({
            "field": field.name,
            "column": raw,
            "seen_in": profile.source.label(),
            "samples": [v.value for v in profile.top_values[:3]] or profile.sample[:3],
            "why": evidence.explain()[:2],
        })

    store.append_audit(run_id, [{
        "actor": Actor.AGENT.value, "action": "schema.aliases",
        "summary": (
            f"Found {len(offers)} column(s) that may be other names for a field "
            "already in the schema. None applied - each needs a person to confirm."
        ),
        "at": now(),
    }])
    return {"version": version, "suggestions": offers}


@app.post("/api/runs/{run_id}/schema/{version}/approve")
def approve_schema(run_id: str, version: int) -> dict[str, Any]:
    """Approval freezes a version and reprocesses everything not yet delivered."""
    body = store.schema_body(run_id, version)
    if body is None:
        raise HTTPException(404, "no such schema version")

    delivered = len(store.accepted_keys(run_id))
    first = store.approved_schema(run_id) is None
    store.approve_schema(run_id, version)
    with store.connect() as conn:
        conn.execute("UPDATE runs SET schema_json = ?, status = 'processing'"
                     " WHERE id = ?", (body, run_id))
    destination.register_schema(
        _schema_for_destination(MigrationSchema.model_validate(json.loads(body)))
    )

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
    store.clear_snapshot(run_id)
    jobs.start(run_id, lambda report: _process_run(run_id, report))
    return {
        "approved": version,
        "started": first,
        "delivered_unchanged": delivered,
    }


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    out = []
    for run in store.list_runs():
        run_id = run["id"]
        delivered = len(store.accepted_keys(run_id))
        # The runs table records what a run was asked to do. The last computed view
        # records where it actually got to, which is what a list should say.
        status, counts = run["status"], None
        if store.approved_schema(run_id) is None:
            status = "awaiting_schema"
        else:
            stored = store.latest_snapshot(run_id)
            if stored:
                view = json.loads(stored)
                status, counts = view.get("status", status), view.get("counts")
            elif jobs.running(run_id):
                status = "processing"
        out.append({
            "id": run_id, "created_at": run["created_at"], "label": run["label"],
            "status": status, "files": len(json.loads(run["files_json"])),
            "delivered": delivered, "counts": counts,
        })
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _fingerprint(run_id: str, run: Any) -> str:
    """Everything a replay's answer depends on, reduced to one string.

    If this is unchanged, replaying would produce the same view, so the stored
    one is served instead. Files are identified by name and size rather than
    content: they are write-once uploads under a run's own directory.
    """
    approved = store.approved_schema(run_id)
    files = []
    for raw in json.loads(run["files_json"]):
        path = Path(raw)
        files.append(f"{path.name}:{path.stat().st_size if path.exists() else '-'}")
    parts = [
        run_id,
        str(approved["version"] if approved else None),
        *sorted(files),
        *sorted(f"{d['case_key']}={d['action']}={d['value']}"
                for d in store.decisions(run_id)),
        *sorted(f"{row['natural_key']}={row['outcome']}={row['generation']}"
                for row in store.deliveries(run_id)),
        f"paused={store.delivery_paused(run_id)}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _ensure_processing(run_id: str) -> Any | None:
    """Return the cached result, or start the work and let the caller poll."""
    cached = jobs.result(run_id)
    if cached is not None:
        return cached
    if not jobs.running(run_id):
        jobs.start(run_id, lambda report: _process_run(run_id, report))
    return None


def _process_run(run_id: str, report: Any) -> Any:
    """Work the files. Sending is a separate step somebody takes.

    Pushing to the target is the one action with a consequence outside this tool,
    so it stays a decision rather than something that happens while the consultant
    is still reading the queue. Once they have seen a push land they can turn on
    auto-send, and from then on readiness is enough.
    """
    result, _ = replay(store, run_id, report=report)
    ready = len(result.ready)
    if ready and store.auto_send(run_id):
        report("delivering", f"Sending {ready} record(s) to the destination", 0, ready)
        deliver_ready(run_id, result)
    return result


EMPTY_COUNTS = {"records": 0, "ready": 0, "delivered": 0, "blocked": 0,
                "excluded": 0, "open_cases": 0}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, wait: Annotated[bool, Query()] = False) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")

    if store.approved_schema(run_id) is None:
        # Nothing is mapped, cleaned or delivered until a schema is agreed.
        return {
            "run_id": run_id, "status": "awaiting_schema", "progress": None,
            "counts": dict(EMPTY_COUNTS), "cases": [], "records": [], "mappings": [],
            "activity": [{"actor": "system", "action": "awaiting_schema",
                          "summary": "Waiting for a target schema to be approved",
                          "reason": None, "before": None, "after": None}],
        }

    # A run nobody has touched since it was last computed is served from store,
    # so opening an old migration never re-runs it.
    fingerprint = _fingerprint(run_id, run)
    if not jobs.running(run_id):
        stored = store.snapshot(run_id, fingerprint)
        if stored is not None:
            return json.loads(stored)

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

    children = {rid for c in result.cases for rid in c.child_records}
    names = {
        (r.natural_key or r.id): " ".join(
            str(r.values.get(f) or "") for f in ("first_name", "last_name")
        ).strip()
        for r in result.records
    }
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
            # Only records genuinely riding on a neighbour's case, not everything
            # held by a file-level question.
            "waiting_on_another": record.id in children,
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

    # A destination rejection is the receiver refusing, not the agent asking. Different
    # cause, different action, so it does not belong in the review queue.
    failures = [
        c for c in open_cases
        if c.klass is EscalationClass.DELIVERY_PERMANENT_FAILURE
    ]
    review = [c for c in open_cases if c not in failures]
    failed_keys = _failed_keys(run_id)

    payload = {
        "run_id": run_id,
        "status": _status(result, records, accepted),
        "progress": progress.as_dict(),
        "delivery_paused": store.delivery_paused(run_id),
        "auto_send": store.auto_send(run_id),
        "counts": {
            # Raw rows before reconciliation, beside the employees they became. Showing
            # both is what makes reconciliation legible.
            "rows_read": result.rows_read,
            "records": len(records),
            "delivered": len([r for r in records if r["state"] == "delivered"]),
            "needs_review": len([r for r in records if r["state"] == "blocked"]),
            "excluded": len([r for r in records if r["state"] == "excluded"]),
            "failed": len(failed_keys),
            "open_cases": len(review),
            # kept for the API's older callers
            "ready": len([r for r in records if r["state"] == "ready"]),
            "blocked": len([r for r in records if r["state"] == "blocked"]),
        },
        "cases": [_case_payload(c, names) for c in review],
        "failures": [
            *({"record": k, "reason": v} for k, v in failed_keys.items()),
        ],
        "records": records,
        "mappings": [
            {
                "file": m.source_file, "column": m.source_column,
                "decision": m.decision.value, "field": m.chosen_field,
                "score": m.best.score if m.best else 0.0, "gap": m.gap,
                "evidence": m.best.evidence.explain() if m.best else [],
            } for m in result.mappings
        ],
        # Read the durable history rather than only this replay's, so "what the agent
        # did" includes what it sent — not just what it mapped and cleaned.
        "activity": [
            {"actor": e.get("actor", "agent"), "action": e.get("action", ""),
             "summary": e.get("summary", ""), "reason": e.get("reason"),
             "before": e.get("before"), "after": e.get("after"), "at": e.get("at")}
            for e in reversed(store.audit(run_id, limit=80))
        ],
    }
    store.save_snapshot(run_id, fingerprint, json.dumps(payload))
    return payload


def _failed_keys(run_id: str) -> dict[str, str]:
    """Records the destination permanently refused, with what it said."""
    out: dict[str, str] = {}
    for row in store.deliveries(run_id):
        key = row["natural_key"]
        if row["outcome"] == "rejected":
            errors = (row["response"] or {}).get("errors") or []
            out[key] = "; ".join(errors) or "the destination refused it"
        elif row["outcome"] in ("accepted", "duplicate"):
            out.pop(key, None)
    return out


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
        result, _ = replay(store, run_id, investigate=False)
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
    store.clear_snapshot(run_id)
    after, _ = replay(store, run_id, investigate=False)
    jobs.start(run_id, lambda report: _process_run(run_id, report))
    jobs.wait(run_id, timeout=180)
    remaining = [c for c in after.cases
                 if case_key(c) not in {d["case_key"] for d in store.decisions(run_id)}]
    return {
        "resolved": key,
        "open_cases": len(remaining),
        "ready": len(after.ready),
        "blocked": len(after.blocked),
    }


def deliver_ready(run_id: str, result: Any) -> dict[str, int]:
    """Send everything that is ready, as soon as it is ready.

    Waiting for a click only ever delayed work the agent had already decided was safe,
    and it made "ready to send" a state records sat in for no reason. Rollback is the
    undo; a gate before the fact is not.
    """
    run = store.get_run(run_id)
    if run is None or not run["schema_json"]:
        return {}
    if store.delivery_paused(run_id):
        # A human undid a delivery. Sending it straight back would make the undo a
        # no-op, so it stays paused until they say otherwise.
        return {}
    schema = MigrationSchema.model_validate(json.loads(run["schema_json"]))
    try:
        destination.register_schema(_schema_for_destination(schema))
    except Exception:  # noqa: BLE001 - a destination outage is reported, not fatal
        return {}
    return _push(run_id, result, schema)


@app.post("/api/runs/{run_id}/deliver")
def deliver(
    run_id: str,
    keep_sending: Annotated[bool, Body(embed=True)] = False,
    simulate: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Push to the target: the step the consultant takes.

    Sends everything currently ready, reports what the destination said about each
    record, and is also the way back after a rollback or a refusal. `keep_sending`
    turns on automatic delivery from here on, for a consultant who has watched one
    push land and does not want to press it again.
    """
    if store.approved_schema(run_id) is None:
        # Without one there is nothing to send and nothing to validate against.
        # Left to fall through it surfaced as a JSON parse error on an empty string.
        raise HTTPException(409, "this run has no approved schema yet")

    store.pause_delivery(run_id, False)
    if keep_sending:
        store.set_auto_send(run_id, True)

    # A rehearsed failure belongs to this push and nothing else. The destination's
    # failure mode is global, so arming it from a panel meant the next few
    # deliveries of any migration wore it. Armed here and cleared in `finally`, it
    # cannot outlive the push that asked for it.
    if simulate and simulate != "none":
        if simulate not in {"transient", "timeout", "uncertain"}:
            raise HTTPException(422, f"unknown failure mode {simulate!r}")
        try:
            destination.set_failure_mode(simulate, 3)
        except Exception as exc:
            raise HTTPException(503, f"destination unreachable: {exc}") from exc
    jobs.wait(run_id, timeout=180)
    result = jobs.result(run_id)
    if result is None:
        # Only the records are needed to send them. Investigating the open cases is
        # what the review queue is for, and doing it here put half a minute between
        # pressing Push and anything happening.
        result, _ = replay(store, run_id, investigate=False)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    schema = MigrationSchema.model_validate(json.loads(run["schema_json"]))
    destination.register_schema(_schema_for_destination(schema))

    try:
        sent = _push(run_id, result, schema)
    finally:
        # Whatever happened, the destination goes back to behaving normally.
        if simulate and simulate != "none":
            try:
                destination.set_failure_mode("none", 0)
            except Exception as exc:  # noqa: BLE001 - reported, never raised over the push
                log.warning("could not clear the rehearsed failure: %s", exc)
    jobs.invalidate(run_id)
    store.clear_snapshot(run_id)
    return {"sent": sent}


def _push(run_id: str, result: Any, schema: MigrationSchema) -> dict[str, int]:
    already = store.accepted_keys(run_id)
    sent = {"accepted": 0, "duplicate": 0, "rejected": 0, "failed": 0, "uncertain": 0}

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
        # What the agent did includes what it sent, not only what it mapped.
        summary = {
            "accepted": f"Sent {key} to the destination",
            "duplicate": f"{key} was already at the destination",
            "rejected": f"The destination refused {key}",
        }.get(bucket, f"Could not send {key} ({final.outcome.value})")
        store.append_audit(run_id, [{
            "actor": Actor.MOCK_API.value, "action": f"delivery.{final.outcome.value}",
            "summary": summary + (
                f" after {len(attempts)} attempts" if len(attempts) > 1 else ""
            ),
            "at": now(), "after": final.target_record_id,
            "reason": "; ".join((final.response or {}).get("errors", []))
            if final.response else None,
        }])
    return sent


class Rehearsal(BaseModel):
    """How the destination should misbehave, and for how many requests."""

    mode: str = "none"          # none | transient | timeout | uncertain
    remaining: int = 3


@app.post("/api/destination/rehearse")
def rehearse_failure(body: Annotated[Rehearsal, Body()]) -> dict[str, Any]:
    """Arrange for the next few deliveries to fail, so the recovery can be seen.

    Retry, reconciliation and rollback are the part of delivery that only exists
    when something goes wrong. A destination that always accepts cannot show any of
    it, which left a named acceptance criterion working but undemonstrable.
    """
    if body.mode not in {"none", "transient", "timeout", "uncertain"}:
        raise HTTPException(422, f"unknown failure mode {body.mode!r}")
    try:
        state = destination.set_failure_mode(body.mode, max(0, body.remaining))
    except Exception as exc:
        raise HTTPException(503, f"destination unreachable: {exc}") from exc
    return {"destination": state}


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
    store.pause_delivery(run_id, True)
    store.append_audit(run_id, [{
        "actor": Actor.HUMAN.value, "action": "delivery.rollback",
        "summary": (
            f"Rolled back {succeeded} of {len(results)} delivered record(s). "
            "Automatic sending is paused until you resume it."
        ),
        "at": now(),
    }])
    jobs.invalidate(run_id)
    store.clear_snapshot(run_id)
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
