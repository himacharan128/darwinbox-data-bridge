"""Storage for the mock destination.

Its own database and its own credentials. The migration application cannot write
here directly; everything arrives over HTTP, because a destination you can reach
around is not a destination you have integrated with.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_records (
    id                TEXT PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE,
    run_id            TEXT NOT NULL,
    natural_key       TEXT NOT NULL,
    schema_version    INTEGER NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 1,
    payload           TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'accepted',
    received_at       TEXT NOT NULL,
    rolled_back_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_records_run ON target_records(run_id);
CREATE INDEX IF NOT EXISTS ix_records_natural ON target_records(natural_key);

CREATE TABLE IF NOT EXISTS registered_schemas (
    version     INTEGER PRIMARY KEY,
    body        TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    run_id      TEXT,
    natural_key TEXT,
    idempotency_key TEXT,
    outcome     TEXT NOT NULL,
    status      INTEGER NOT NULL,
    detail      TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def reset(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM target_records")
            conn.execute("DELETE FROM request_log")
            conn.execute("DELETE FROM registered_schemas")

    def log(self, **kw: object) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO request_log (at, run_id, natural_key, idempotency_key,"
                " outcome, status, detail) VALUES (:at,:run_id,:natural_key,"
                ":idempotency_key,:outcome,:status,:detail)",
                {"detail": json.dumps(kw.pop("detail", None)), **kw},
            )
