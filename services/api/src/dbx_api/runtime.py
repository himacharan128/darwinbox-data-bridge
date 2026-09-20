"""Replay a run from its uploaded files plus the decisions taken so far.

Case identifiers are positional, so they renumber whenever the pipeline replays.
Decisions are therefore keyed by content — what the case is ABOUT — which survives
a replay and makes a decision reapplicable to the case it actually answered.
"""
from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dbx_agent import build_provider, vote_on_column
from dbx_contracts import Action, CaseState, MigrationSchema, ReviewCase
from dbx_extraction import UnsupportedInput, read
from dbx_migration_core import Overrides, Pipeline, RunResult, apply_decision

from .store import Store


def case_key(case: ReviewCase) -> str:
    """A stable identity for a case, independent of its position in the queue."""
    column = ""
    for ref in case.source_refs:
        if ref.column:
            column = f"{ref.file}:{ref.column}"
            break
    return "|".join([
        case.klass.value, case.record_key or "", case.target_field or "",
        column, case.headline if not case.record_key and not column else "",
    ])


def load_schema(schema_json: str) -> MigrationSchema:
    return MigrationSchema.model_validate(json.loads(schema_json))


def load_lookups(paths: list[str]) -> dict[str, set[str]]:
    """Any uploaded file whose name matches a lookup becomes that lookup's universe."""
    out: dict[str, set[str]] = {}
    for raw in paths:
        path = Path(raw)
        stem = path.stem.rstrip("s")
        if not path.exists() or path.suffix.lower() != ".csv":
            continue
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if rows and "code" in rows[0] and len(rows[0]) <= 4:
            out[stem] = {r["code"] for r in rows}
    return out


def _is_lookup(path: Path, lookups: dict[str, set[str]]) -> bool:
    return path.stem.rstrip("s") in lookups


def replay(
    store: Store,
    run_id: str,
    *,
    offline: bool = False,
    report: Callable[..., None] | None = None,
) -> tuple[RunResult, Overrides]:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)

    schema = load_schema(run["schema_json"])
    paths = json.loads(run["files_json"])
    lookups = load_lookups(paths)

    sources: dict[str, list] = {}
    for raw in paths:
        path = Path(raw)
        if not path.exists() or _is_lookup(path, lookups):
            continue
        try:
            sources[path.name] = read(path)
        except UnsupportedInput:
            continue

    provider = build_provider(Path("tests/fixtures/model-cache"), offline=offline)
    seen = 0
    expected = sum(len(records[0].values) for records in sources.values() if records)

    def votes(profile: Any, sch: MigrationSchema) -> dict[str, float]:
        nonlocal seen
        seen += 1
        if report:
            report("mapping", f"Working out where {profile.raw_name!r} belongs",
                   min(seen, expected), expected)
        try:
            return vote_on_column(provider, profile, sch)[0]
        except Exception:  # noqa: BLE001 - a model outage must not stop the run
            return {}

    # A decision can only be matched to its case once the decisions before it have
    # been applied — answering "which column is designation?" is what makes the next
    # case appear. So replay, apply whatever now matches, and repeat until nothing
    # new matches. Each decision is applied at most once, which is what keeps the
    # result stable across replays.
    overrides = Overrides()
    decisions = store.decisions(run_id)
    applied: set[str] = set()

    for _ in range(len(decisions) + 1):
        result = Pipeline(run_id, schema, votes=votes, overrides=overrides).run(sources, lookups)
        by_key = {case_key(c): c for c in result.cases}
        progressed = False
        for decision in decisions:
            if decision["id"] in applied:
                continue
            case = by_key.get(decision["case_key"])
            if case is None:
                continue
            apply_decision(overrides, case, Action(decision["action"]), decision["value"])
            applied.add(decision["id"])
            progressed = True
        if not progressed:
            break

    if report:
        report("validating", "Checking records against the target schema")
    result = Pipeline(run_id, schema, votes=votes, overrides=overrides).run(sources, lookups)
    answered = {d["case_key"] for d in decisions}
    for case in result.cases:
        if case_key(case) in answered:
            case.state = CaseState.RESOLVED
    return result, overrides
