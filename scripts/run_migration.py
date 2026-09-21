#!/usr/bin/env python
"""Run one migration over the fixtures and print what the agent decided.

    uv run python scripts/run_migration.py           # with the model vote
    uv run python scripts/run_migration.py --no-llm  # deterministic only
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbx_agent import assign_file_columns, build_provider
from dbx_contracts import MigrationSchema
from dbx_extraction import confidence_for, read
from dbx_migration_core import Pipeline

FIX = ROOT / "tests" / "fixtures"
EMPLOYEE_FILES = [
    "hrms_employees_export.csv",
    "payroll_staff.xlsx",
    "contractors_2024.csv",
    "workday_extract.csv",
    "legacy_hrms_dump.csv",
    "roster_export.pdf",
    "scanned_roster.pdf",
]
LOOKUP_FILES = {"department": "departments.csv", "location": "locations.csv"}


def load_lookups(run_dir: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, fname in LOOKUP_FILES.items():
        path = run_dir / fname
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            out[name] = {row["code"] for row in csv.DictReader(fh)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--run", default="run1")
    args = ap.parse_args()

    schema = MigrationSchema.model_validate(
        yaml.safe_load((FIX / "schemas" / "target_schema.yaml").read_text())
    )
    run_dir = FIX / args.run
    sources = {
        f: read(run_dir / f) for f in EMPLOYEE_FILES if (run_dir / f).exists()
    }
    lookups = load_lookups(run_dir)

    votes = None
    if not args.no_llm:
        provider = build_provider(FIX / "model-cache")

        def votes(file_name, profiles, sch):
            # One call per file, as the console makes it. A model that is unreachable
            # leaves the deterministic evidence to decide, which is what it is for.
            try:
                return assign_file_columns(provider, file_name, profiles, sch)[0]
            except Exception:  # noqa: BLE001
                return {}

    confidence = {
        record.id: {k: c.confidence for k, c in confidence_for(record.id).items()}
        for records in sources.values()
        for record in records
        if confidence_for(record.id)
    }
    result = Pipeline(
        run_id="run-demo", schema=schema, votes=votes, confidence=confidence
    ).run(sources, lookups)

    print("\n" + "=" * 78)
    print(f"{'AGENT ACTIVITY':^78}")
    print("=" * 78)
    for event in result.audit:
        print(f"  [{event.actor.value:5s}] {event.summary}")

    print("\n" + "=" * 78)
    print(f"{'NEEDS YOUR DECISION':^78}")
    print("=" * 78)
    for case in result.open_cases:
        print(f"\n  {case.id}  {case.klass.value}")
        print(f"     {case.headline}")
        print(f"     {case.detail}")
        if case.record_key:
            print(f"     record : {case.record_key}")
        if case.source_refs:
            print(f"     source : {case.source_refs[0].label()}")
        if case.raw_values:
            print(f"     value  : {', '.join(repr(v) for v in case.raw_values[:3])}")
        if case.options:
            print(f"     options: {' | '.join(o.label for o in case.options[:4])}")

    print("\n" + "=" * 78)
    print(f"  records ready to send : {len(result.ready)}")
    print(f"  records awaiting you  : {len(result.blocked)}")
    print(f"  questions raised      : {len(result.open_cases)}")
    print(f"  audit events          : {len(result.audit)}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
