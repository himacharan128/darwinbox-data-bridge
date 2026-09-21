"""Replay a run from its uploaded files plus the decisions taken so far.

Case identifiers are positional, so they renumber whenever the pipeline replays.
Decisions are therefore keyed by content — what the case is ABOUT — which survives
a replay and makes a decision reapplicable to the case it actually answered.
"""
from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dbx_agent import assign_file_columns, build_provider
from dbx_contracts import Action, Actor, CaseState, MigrationSchema, ReviewCase
from dbx_extraction import UnsupportedInput, confidence_for, is_sidecar, read
from dbx_migration_core import Overrides, Pipeline, RunResult, apply_decision

from .investigate import enrich
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
        if not path.exists() or is_sidecar(path) or path.suffix.lower() != ".csv":
            continue
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if rows and "code" in rows[0] and len(rows[0]) <= 4:
            out[stem] = {r["code"] for r in rows}
    return out


def lookup_columns(paths: list[str]) -> dict[str, list[str]]:
    """The column names each lookup file actually has, for declaring it in a schema."""
    out: dict[str, list[str]] = {}
    for raw in paths:
        path = Path(raw)
        stem = path.stem.rstrip("s")
        if not path.exists() or is_sidecar(path) or path.suffix.lower() != ".csv":
            continue
        with path.open(encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if "code" in header and len(header) <= 4:
            out[stem] = [h.strip() for h in header]
    return out


def _is_lookup(path: Path, lookups: dict[str, set[str]]) -> bool:
    return path.stem.rstrip("s") in lookups


def replay(
    store: Store,
    run_id: str,
    *,
    offline: bool = False,
    report: Callable[..., None] | None = None,
    investigate: bool = True,
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
        if not path.exists() or is_sidecar(path) or _is_lookup(path, lookups):
            continue
        try:
            sources[path.name] = read(path)
        except UnsupportedInput:
            continue

    confidence: dict[str, dict[str, float]] = {}
    for records in sources.values():
        for record in records:
            cells = confidence_for(record.id)
            if cells:
                confidence[record.id] = {k: c.confidence for k, c in cells.items()}

    provider = build_provider(Path("tests/fixtures/model-cache"), offline=offline)
    seen = 0
    expected = sum(len(records[0].values) for records in sources.values() if records)

    def votes(
        file_name: str, profiles: list[Any], sch: MigrationSchema
    ) -> dict[str, dict[str, float]]:
        nonlocal seen
        seen += len(profiles)
        if report:
            report("mapping", f"Working out where the columns of {file_name} belong",
                   min(seen, expected), expected)
        try:
            return assign_file_columns(provider, file_name, profiles, sch)[0]
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
        result = Pipeline(
            run_id, schema, votes=votes, overrides=overrides, confidence=confidence
        ).run(sources, lookups)
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
    pipeline = Pipeline(
        run_id, schema, votes=votes, overrides=overrides, confidence=confidence
    )
    result = pipeline.run(sources, lookups)
    # An answer whose question never came up is one the schema has moved away from -
    # a field renamed or removed since. It is not applied, and not silently lost.
    for decision in decisions:
        if decision["id"] not in applied:
            about = (decision.get("payload") or {}).get("headline") or decision["case_key"]
            pipeline._log(
                "decision.no_longer_applies",
                f"An earlier answer no longer matches anything in this schema: {about}",
                actor=Actor.HUMAN,
            )
    answered = {d["case_key"] for d in decisions}
    for case in result.cases:
        if case_key(case) in answered:
            case.state = CaseState.RESOLVED

    # Everything above is deterministic. Only now, with the queue settled, does the
    # agent go and look at the data behind the questions it is about to ask - and
    # all it may do with what it finds is suggest an answer on a case that still
    # needs a person.
    if investigate and result.open_cases and not offline:
        if report:
            report("investigating", "Looking into what it could not settle",
                   0, len(result.open_cases))
        try:
            looked = enrich(result, schema, pipeline.profiles, provider)
            if looked and report:
                report("investigating", f"Checked the data behind {looked} question(s)",
                       looked, looked)
        except Exception as exc:  # noqa: BLE001 - never lose a queue over this
            logging.getLogger("dbx.api").warning("investigation pass failed: %s", exc)

    return result, overrides


def source_profiles(store: Store, run_id: str) -> list:
    """Profile every source column in a run, for the schema recommender."""
    from collections import defaultdict

    from dbx_contracts import SourceRef
    from dbx_migration_core.profiling import profile_column

    run = store.get_run(run_id)
    if run is None:
        return []
    paths = json.loads(run["files_json"])
    lookups = load_lookups(paths)

    out = []
    for raw in paths:
        path = Path(raw)
        if not path.exists() or is_sidecar(path) or _is_lookup(path, lookups):
            continue
        try:
            records = read(path)
        except UnsupportedInput:
            continue
        if not records:
            continue
        columns: dict[str, list] = defaultdict(list)
        for record in records:
            for key, value in record.values.items():
                columns[key].append(value)
        src = records[0].source
        out.extend(
            profile_column(
                SourceRef(file=src.file, sheet=src.sheet, column=header), header, values
            )
            for header, values in columns.items()
        )
    return out
