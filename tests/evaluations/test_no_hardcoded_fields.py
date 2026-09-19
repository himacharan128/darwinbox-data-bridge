"""Guard: the migration engine must be schema-driven, with no domain field names.

MASTER_PLAN.md §2.0. The runtime target schema is whatever the user supplies or
approves; the fixture schema is one instance. If any engine package starts
special-casing a field from that fixture, this fails.

It is the difference between claiming the engine is generic and proving it.
"""

from __future__ import annotations

from pathlib import Path

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


def _executable_source(path: Path) -> list[tuple[int, str]]:
    """Strip `#` comments and docstrings, keep everything that can affect behaviour.

    A comment naming a field cannot special-case it, and worked examples in prose are
    worth keeping. A string literal still can, so only docstrings are removed — a
    field name anywhere else, including a live string, still fails the guard.
    """
    import ast
    import io
    import tokenize

    text = path.read_text(encoding="utf-8")
    docstring_lines: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return list(enumerate(text.splitlines(), 1))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0].lineno
            last = body[0].end_lineno or first
            docstring_lines.update(range(first, last + 1))

    comment_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])

    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if lineno in docstring_lines:
            continue
        if lineno in comment_lines:
            line = line.split("#", 1)[0]
        if line.strip():
            out.append((lineno, line))
    return out


def test_guard_is_watching_real_paths() -> None:
    """The guard must fail loudly if the package layout moves."""
    missing = [p for p in GUARDED if not (ROOT / p).is_dir()]
    assert not missing, f"guarded packages not found: {missing}"
