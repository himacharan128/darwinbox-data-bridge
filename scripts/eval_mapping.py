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

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbx_agent import build_provider, vote_on_column  # noqa: E402
from dbx_contracts import Decision, MigrationSchema, SourceRef, Thresholds  # noqa: E402
from dbx_extraction import read  # noqa: E402
from dbx_migration_core.profiling import profile_column  # noqa: E402
from dbx_migration_core.scoring import rank_column  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
CORPUS = ROOT / "tests" / "evaluations" / "corpus"
FILES = ["hrms_employees_export.csv", "payroll_staff.xlsx", "contractors_2024.csv"]


@dataclass
class Row:
    file: str
    column: str
    expected_target: str | None
    expected_outcome: str
    difficulty: str
    profile: object
    votes: dict[str, float]


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
            c["difficulty"],
        )
        for c in doc["cases"]
        if c["source"]["column"]
    }


def collect(schema: MigrationSchema, *, use_llm: bool) -> list[Row]:
    labels = load_labels()
    provider = build_provider(ROOT / "tests" / "fixtures" / "model-cache") if use_llm else None
    rows: list[Row] = []

    for fname in FILES:
        records = read(FIXTURES / "run1" / fname)
        columns: dict[str, list[str | None]] = defaultdict(list)
        for r in records:
            for key, value in r.values.items():
                columns[key].append(value)
        src = records[0].source

        for header, values in columns.items():
            profile = profile_column(
                SourceRef(file=src.file, sheet=src.sheet, column=header), header, values
            )
            votes: dict[str, float] = {}
            if provider is not None:
                votes, _ = vote_on_column(provider, profile, schema)
            target, outcome, difficulty = labels.get((fname, header), (None, "?", "?"))
            rows.append(Row(fname, header, target, outcome, difficulty, profile, votes))
    return rows


def evaluate(rows: list[Row], schema: MigrationSchema, thresholds: Thresholds) -> dict:
    tp = fp = fn = correct = wrong = 0
    details = []
    for row in rows:
        mapping = rank_column(
            row.profile, schema, llm_votes=row.votes or None, thresholds=thresholds
        )
        auto = mapping.decision is Decision.AUTO_APPLY
        should_escalate = row.expected_outcome == "escalate"

        if should_escalate and not auto:
            tp += 1  # correctly held back
        elif not should_escalate and not auto:
            fp += 1  # over-escalation: a safe case sent to a human
        elif should_escalate and auto:
            fn += 1  # under-escalation: guessed at something uncertain
        if not should_escalate and auto:
            if mapping.chosen_field == row.expected_target:
                correct += 1
            else:
                wrong += 1
        details.append((row, mapping, auto, should_escalate))

    escalated = tp + fp
    needed = tp + fn
    return {
        "precision": tp / escalated if escalated else 1.0,
        "recall": tp / needed if needed else 1.0,
        "auto_correct": correct,
        "auto_wrong": wrong,
        "over_escalated": fp,
        "under_escalated": fn,
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
            f"{'T_auto':>7} {'T_gap':>6} {'auto ok':>8} {'auto wrong':>11}"
            f" {'over-esc':>9} {'under-esc':>10} {'precision':>10} {'recall':>8}"
        )
        for auto in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
            for gap in (0.05, 0.10, 0.15, 0.20):
                m = evaluate(rows, schema, Thresholds(auto_apply=auto, gap=gap, review_floor=0.50))
                print(
                    f"{auto:>7.2f} {gap:>6.2f} {m['auto_correct']:>8} {m['auto_wrong']:>11}"
                    f" {m['over_escalated']:>9} {m['under_escalated']:>10}"
                    f" {m['precision']:>10.2f} {m['recall']:>8.2f}"
                )
        return 0

    thresholds = Thresholds()
    m = evaluate(rows, schema, thresholds)
    print(f"\nMapping boundary ({mode})\n" + "=" * 78)
    for row, mapping, auto, should in m["details"]:
        best = mapping.best
        ok = (auto and mapping.chosen_field == row.expected_target) or (should and not auto)
        label = f"{best.target_field}={best.score:.2f}" if best else "-"
        print(
            f"  {'OK ' if ok else 'XX '}{mapping.decision.value:10s} "
            f"{row.column:18s} -> {label:28s} gap={mapping.gap:.2f} [{row.difficulty}]"
        )
    print("=" * 78)
    print(f"  auto-applied correctly : {m['auto_correct']}")
    print(f"  auto-applied WRONGLY   : {m['auto_wrong']}   <- must be 0")
    print(f"  over-escalated         : {m['over_escalated']}   (safe cases sent to a human)")
    print(f"  under-escalated        : {m['under_escalated']}   <- must be 0")
    print(f"  escalation precision   : {m['precision']:.2f}")
    print(f"  escalation recall      : {m['recall']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
