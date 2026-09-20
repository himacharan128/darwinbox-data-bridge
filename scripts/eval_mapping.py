#!/usr/bin/env python
"""Measure the mapping boundary against the labelled corpus.

This is the Phase 2 harness in embryo. It answers the question criterion 3 actually
asks — *why is the line there?* — with precision and recall rather than an assertion.

    uv run python scripts/eval_mapping.py            # deterministic + model vote
    uv run python scripts/eval_mapping.py --no-llm   # deterministic only
    uv run python scripts/eval_mapping.py --sweep    # threshold sweep
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbx_agent import assign_file_columns, build_provider
from dbx_contracts import Decision, MigrationSchema, SourceRef, Thresholds
from dbx_extraction import read
from dbx_migration_core.pipeline import _resolve_within_file
from dbx_migration_core.profiling import profile_column
from dbx_migration_core.scoring import rank_column

FIXTURES = ROOT / "tests" / "fixtures"
CORPUS = ROOT / "tests" / "evaluations" / "corpus"
FILES = [
    "hrms_employees_export.csv",
    "payroll_staff.xlsx",
    "contractors_2024.csv",
    "workday_extract.csv",
    "legacy_hrms_dump.csv",
]


@dataclass
class Row:
    file: str
    column: str
    expected_target: str | None
    expected_outcome: str
    difficulty: str
    profile: object
    votes: dict[str, float]
    lookups: dict[str, set[str]]


def load_schema() -> MigrationSchema:
    return MigrationSchema.model_validate(
        yaml.safe_load((FIXTURES / "schemas" / "target_schema.yaml").read_text())
    )


def load_labels() -> dict[tuple[str, str], tuple[str | None, str, str]]:
    doc = yaml.safe_load((CORPUS / "mapping_decisions.yaml").read_text())
    return {
        (c["source"]["file"].split("/")[-1], c["source"]["column"]): (
            c.get("expected_target"),
            c["expected_outcome"],
            c.get("difficulty", "?"),
        )
        for c in doc["cases"]
        if c["source"]["column"]
    }


def load_lookups() -> dict[str, set[str]]:
    import csv as _csv

    out: dict[str, set[str]] = {}
    for name, fname in (("department", "departments.csv"), ("location", "locations.csv")):
        path = FIXTURES / "run1" / fname
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                out[name] = {r["code"] for r in _csv.DictReader(fh)}
    return out


def collect(schema: MigrationSchema, *, use_llm: bool) -> list[Row]:
    labels = load_labels()
    lookups = load_lookups()
    provider = build_provider(ROOT / "tests" / "fixtures" / "model-cache") if use_llm else None
    rows: list[Row] = []

    for fname in FILES:
        records = read(FIXTURES / "run1" / fname)
        columns: dict[str, list[str | None]] = defaultdict(list)
        for r in records:
            for key, value in r.values.items():
                columns[key].append(value)
        src = records[0].source

        # Profile the whole file, then ask about it in one go — the same way the
        # pipeline does it, so the corpus measures what actually runs.
        profiles = [
            profile_column(
                SourceRef(file=src.file, sheet=src.sheet, column=header), header, values
            )
            for header, values in columns.items()
        ]
        by_column: dict[str, dict[str, float]] = {}
        if provider is not None:
            by_column, _ = assign_file_columns(provider, fname, profiles, schema)

        for profile in profiles:
            header = profile.raw_name
            target, outcome, difficulty = labels.get((fname, header), (None, "?", "?"))
            rows.append(
                Row(fname, header, target, outcome, difficulty, profile,
                    by_column.get(header, {}), lookups)
            )
    return rows


def evaluate(rows: list[Row], schema: MigrationSchema, thresholds: Thresholds) -> dict:
    """Score the boundary three ways, because there are three kinds of mistake.

    Mapping a column that is not entity data at all (a checksum, an audit timestamp)
    is the worst of them: it silently corrupts the dataset with no case raised. It is
    tracked separately from applying the wrong field.
    """
    auto_correct = auto_wrong = false_positive = 0
    over = under = 0
    escalated = needed = correctly_held = 0
    details = []

    # Rank per file and resolve within it, exactly as the pipeline does: one field
    # cannot be fed by two columns of the same file, and the loser is decided again
    # rather than escalated.
    ranked_by_row: dict[int, Any] = {}
    by_file: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for i, row in enumerate(rows):
        mapping = rank_column(
            row.profile, schema, llm_votes=row.votes or None, thresholds=thresholds,
            lookups=row.lookups,
        )
        ranked_by_row[i] = mapping
        by_file[row.file].append((row.column, mapping))
    for pairs in by_file.values():
        _resolve_within_file(pairs)

    for i, row in enumerate(rows):
        mapping = ranked_by_row[i]
        auto = mapping.decision is Decision.AUTO_APPLY
        reviewed = mapping.decision is Decision.REVIEW
        want = row.expected_outcome

        if want == "no_map":
            if auto:
                false_positive += 1
            elif reviewed:
                over += 1
        elif want == "escalate":
            needed += 1
            if auto:
                under += 1
            else:
                correctly_held += 1
        else:  # auto
            if auto:
                if mapping.chosen_field == row.expected_target:
                    auto_correct += 1
                else:
                    auto_wrong += 1
            else:
                over += 1

        if reviewed:
            escalated += 1
        ok = (
            (want == "auto" and auto and mapping.chosen_field == row.expected_target)
            or (want == "escalate" and not auto)
            or (want == "no_map" and not auto and not reviewed)
        )
        details.append((row, mapping, auto, want, ok))

    return {
        "precision": correctly_held / escalated if escalated else 1.0,
        "recall": correctly_held / needed if needed else 1.0,
        "auto_correct": auto_correct,
        "auto_wrong": auto_wrong,
        "false_positive": false_positive,
        "over_escalated": over,
        "under_escalated": under,
        "agreement": sum(1 for d in details if d[4]) / len(details),
        "total": len(rows),
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="deterministic signals only")
    ap.add_argument("--sweep", action="store_true", help="sweep auto-apply and gap thresholds")
    args = ap.parse_args()

    schema = load_schema()
    rows = collect(schema, use_llm=not args.no_llm)
    mode = "deterministic only" if args.no_llm else "deterministic + capped model vote"

    if args.sweep:
        print(f"\nThreshold sweep ({mode})\n" + "=" * 78)
        print(
            f"{'T_auto':>7} {'T_gap':>6} {'auto ok':>8} {'wrong':>6} {'noise':>6}"
            f" {'over-esc':>9} {'under-esc':>10} {'agree':>7}"
        )
        for auto in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
            for gap in (0.05, 0.10, 0.15, 0.20):
                m = evaluate(rows, schema, Thresholds(auto_apply=auto, gap=gap, review_floor=0.50))
                print(
                    f"{auto:>7.2f} {gap:>6.2f} {m['auto_correct']:>8} {m['auto_wrong']:>6}"
                    f" {m['false_positive']:>6} {m['over_escalated']:>9}"
                    f" {m['under_escalated']:>10} {m['agreement']:>7.2f}"
                )
        return 0

    thresholds = Thresholds()
    m = evaluate(rows, schema, thresholds)
    print(f"\nMapping boundary ({mode})\n" + "=" * 78)
    for row, mapping, auto, want, ok in m["details"]:
        best = mapping.best
        label = f"{best.target_field}={best.score:.2f}" if best else "(nothing)"
        mark = "OK " if ok else "XX "
        print(
            f"  {mark}{mapping.decision.value:10s} {row.column:18s} -> {label:28s}"
            f" gap={mapping.gap:.2f}  want={want}"
        )
    print("=" * 78)
    print(f"  labelled decisions     : {m['total']}")
    print(f"  auto-applied correctly : {m['auto_correct']}")
    print(f"  noise columns mapped   : {m['false_positive']}   <- must be 0")
    print(f"  auto-applied WRONGLY   : {m['auto_wrong']}   <- must be 0")
    print(f"  over-escalated         : {m['over_escalated']}   (safe cases sent to a human)")
    print(f"  under-escalated        : {m['under_escalated']}   <- must be 0")
    print(f"  escalation precision   : {m['precision']:.2f}")
    print(f"  escalation recall      : {m['recall']:.2f}")
    print(f"  agreement with corpus  : {m['agreement']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
