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
    label        TEXT,
    -- Set by a rollback. Automatic delivery follows readiness, so without this an undo
    -- would be undone by the very next processing pass.
    delivery_paused INTEGER NOT NULL DEFAULT 0,
    -- Sending is a step the consultant takes. Off until they ask for it, because
    -- "push to target" is a decision somebody makes, not a thing that happens
    -- while they are reading the queue.
    auto_send       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS schema_versions (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    state       TEXT NOT NULL,          -- draft | approved | superseded
    origin      TEXT NOT NULL,          -- supplied | recommended | edited
    body        TEXT NOT NULL,
    original    TEXT,                   -- the upload exactly as received
    approved_by TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (run_id, version)
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

-- The answer a replay produced, kept against a fingerprint of everything that
-- could change it. A run nobody has touched is served from here rather than
-- recomputed, so opening an old migration does not redo its work.
CREATE TABLE IF NOT EXISTS snapshots (
    run_id      TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL,
    at          TEXT NOT NULL
);
"""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            if "auto_send" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN auto_send INTEGER NOT NULL DEFAULT 0")

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

    def pause_delivery(self, run_id: str, paused: bool = True) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE runs SET delivery_paused = ? WHERE id = ?",
                         (1 if paused else 0, run_id))

    def delivery_paused(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        return bool(run and run["delivery_paused"])

    def set_auto_send(self, run_id: str, on: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE runs SET auto_send = ? WHERE id = ?",
                         (1 if on else 0, run_id))

    def auto_send(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        try:
            return bool(run and run["auto_send"])
        except (IndexError, KeyError):
            return False        # a run created before the column existed

    def set_status(self, run_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def save_snapshot(self, run_id: str, fingerprint: str, payload: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO snapshots (run_id, fingerprint, payload, at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, payload = excluded.payload, "
                "at = excluded.at",
                (run_id, fingerprint, payload, now()),
            )

    def snapshot(self, run_id: str, fingerprint: str) -> str | None:
        """The stored answer, but only if nothing that feeds it has changed."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM snapshots WHERE run_id = ? AND fingerprint = ?",
                (run_id, fingerprint),
            ).fetchone()
        return row["payload"] if row else None

    def latest_snapshot(self, run_id: str) -> str | None:
        """The last computed view, fingerprint aside — good enough to label a list."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row["payload"] if row else None

    def clear_snapshot(self, run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM snapshots WHERE run_id = ?", (run_id,))

    def delete_run(self, run_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    # ---------------------------------------------------------- schema versions

    def add_schema_version(
        self, run_id: str, body: str, *, origin: str, state: str = "draft",
        original: str | None = None,
    ) -> int:
        """Append a version. Approved versions are never edited in place.

        A delivered record was shaped by a particular version of the schema, and
        rewriting that version afterwards would make the audit trail describe a
        payload that was never sent.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM schema_versions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            version = row["v"] + 1
            conn.execute(
                "INSERT INTO schema_versions (id, run_id, version, state, origin, body,"
                " original, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, version, state, origin, body, original, now()),
            )
        return version

    def approve_schema(self, run_id: str, version: int, actor: str = "consultant") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE schema_versions SET state = 'superseded'"
                " WHERE run_id = ? AND state = 'approved'",
                (run_id,),
            )
            conn.execute(
                "UPDATE schema_versions SET state = 'approved', approved_by = ?"
                " WHERE run_id = ? AND version = ?",
                (actor, run_id, version),
            )

    def approved_schema(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM schema_versions WHERE run_id = ? AND state = 'approved'"
                " ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_draft(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM schema_versions WHERE run_id = ? AND state = 'draft'"
                " ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def schema_versions(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, version, state, origin, approved_by, created_at"
                " FROM schema_versions WHERE run_id = ? ORDER BY version",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def schema_body(self, run_id: str, version: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT body FROM schema_versions WHERE run_id = ? AND version = ?",
                (run_id, version),
            ).fetchone()
        return row["body"] if row else None

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
