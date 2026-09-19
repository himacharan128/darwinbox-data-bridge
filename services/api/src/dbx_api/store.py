"""Durable state for migration runs.

Only two things are truly durable: what a human decided, and what the destination
accepted. Everything else — mappings, cleaned values, the case queue — is derived by
replaying the pipeline over the uploaded files plus those decisions.

That is what makes a run resumable. Reopening it recomputes the same queue rather
than restoring a snapshot that could disagree with the files on disk, and a decision
can never leave the dataset half-processed.

DATABASE_URL is honoured when set (Postgres in the deployed environment); the local
default is a SQLite file so the repository runs with no services to start.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    schema_json  TEXT NOT NULL,
    files_json   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'processing',
    label        TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    case_key    TEXT NOT NULL,
    klass       TEXT NOT NULL,
    action      TEXT NOT NULL,
    value       TEXT,
    reason      TEXT,
    actor       TEXT NOT NULL DEFAULT 'consultant',
    decided_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    natural_key   TEXT NOT NULL,
    generation    INTEGER NOT NULL DEFAULT 1,
    attempt       INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    status_code   INTEGER,
    target_record_id TEXT,
    payload       TEXT NOT NULL,
    response      TEXT,
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_run ON deliveries(run_id, natural_key);
CREATE TABLE IF NOT EXISTS audit (
    id        TEXT PRIMARY KEY,
    run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    summary   TEXT NOT NULL,
    body      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit(run_id, at);
"""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ runs

    def create_run(self, schema_json: str, files: list[str], label: str | None) -> str:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, created_at, schema_json, files_json, label)"
                " VALUES (?,?,?,?,?)",
                (run_id, now(), schema_json, json.dumps(files), label),
            )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def set_status(self, run_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def delete_run(self, run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    # -------------------------------------------------------------- decisions

    def record_decision(
        self, run_id: str, case_key: str, klass: str, action: str,
        value: str | None, reason: str | None, payload: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO decisions (id, run_id, case_key, klass, action, value,"
                " reason, decided_at, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, case_key, klass, action, value, reason,
                 now(), json.dumps(payload)),
            )

    def decisions(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE run_id = ? ORDER BY decided_at", (run_id,)
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # ------------------------------------------------------------- deliveries

    def record_delivery(self, run_id: str, **kw: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO deliveries (id, run_id, natural_key, generation, attempt,"
                " outcome, status_code, target_record_id, payload, response, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, kw["natural_key"], kw.get("generation", 1),
                 kw["attempt"], kw["outcome"], kw.get("status_code"),
                 kw.get("target_record_id"), json.dumps(kw.get("payload", {})),
                 json.dumps(kw.get("response")), now()),
            )

    def deliveries(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE run_id = ? ORDER BY at", (run_id,)
            ).fetchall()
        return [
            {**dict(r), "payload": json.loads(r["payload"]),
             "response": json.loads(r["response"]) if r["response"] else None}
            for r in rows
        ]

    def accepted_keys(self, run_id: str) -> dict[str, int]:
        """Natural keys currently accepted by the destination, with their generation."""
        out: dict[str, int] = {}
        for row in self.deliveries(run_id):
            if row["outcome"] in ("accepted", "duplicate"):
                out[row["natural_key"]] = row["generation"]
            elif row["outcome"] == "rolled_back":
                out.pop(row["natural_key"], None)
        return out

    # ------------------------------------------------------------------ audit

    def append_audit(self, run_id: str, events: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO audit (id, run_id, at, actor, action, summary, body)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (e.get("id") or str(uuid.uuid4()), run_id, e.get("at") or now(),
                     e["actor"], e["action"], e["summary"], json.dumps(e))
                    for e in events
                ],
            )

    def audit(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit WHERE run_id = ? ORDER BY at DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [json.loads(r["body"]) | {"at": r["at"]} for r in rows]
