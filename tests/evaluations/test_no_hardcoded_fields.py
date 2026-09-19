"""Guard: the migration engine must be schema-driven, with no domain field names.

MASTER_PLAN.md §2.0. The runtime target schema is whatever the user supplies or
approves; the fixture schema is one instance. If any engine package starts
special-casing a field from that fixture, this fails.

It is the difference between claiming the engine is generic and proving it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "tests" / "fixtures" / "schemas" / "target_schema.yaml"

# Engine packages: must know nothing about employees.
GUARDED = ["packages/migration-core", "packages/agent"]

# Generic schema-language terms that are legitimately part of the engine vocabulary.
ALLOWED = {"string", "integer", "number", "boolean", "date", "datetime", "email", "enum"}


def _banned_terms() -> set[str]:
    doc = yaml.safe_load(SCHEMA.read_text())
    terms = {f["name"] for f in doc["fields"]} - ALLOWED
    terms |= {lk["name"] for lk in doc.get("lookups", [])}
    terms |= {doc["entity"]}
    return terms


def _sources() -> list[Path]:
    out: list[Path] = []
    for pkg in GUARDED:
        out.extend((ROOT / pkg).rglob("*.py"))
    return out


def test_engine_contains_no_domain_field_names() -> None:
    banned = _banned_terms()
    assert banned, "fixture schema produced no terms — guard would be vacuous"

    patterns = {t: re.compile(rf"\b{re.escape(t)}\b") for t in banned}
    violations: list[str] = []

    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for term, pat in patterns.items():
                if pat.search(line):
                    rel = path.relative_to(ROOT)
                    violations.append(f"{rel}:{lineno}: '{term}' -> {line.strip()[:70]}")

    assert not violations, (
        "Engine packages must be schema-driven and contain no domain field names.\n"
        "Read the field from the supplied schema instead of naming it.\n\n"
        + "\n".join(violations[:20])
    )


def test_guard_is_watching_real_paths() -> None:
    """The guard must fail loudly if the package layout moves."""
    missing = [p for p in GUARDED if not (ROOT / p).is_dir()]
    assert not missing, f"guarded packages not found: {missing}"
