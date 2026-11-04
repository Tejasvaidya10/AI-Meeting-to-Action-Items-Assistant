"""Durable workflow state (SQLite), so a pending review survives a restart.

runs      one row per run: status plus the full RunState JSON
reviews   one row per candidate awaiting a human: the review packet, its
          deadline, who may decide, and the recorded decision/authorization
actions   outbox of external effects keyed by idempotency key; a row is
          written BEFORE Jira is called, so a crash leaves evidence to reconcile
inbox     every event_id ever received and what happened to it (dedup)

The transcript text is never stored here, only its hash (see state.py).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, meeting_id TEXT, transcript_sha256 TEXT, status TEXT,
    state_json TEXT, updated_at TEXT, UNIQUE (meeting_id, transcript_sha256));
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY, run_id TEXT, candidate_id TEXT, status TEXT,
    assigned_to TEXT, escalation_to TEXT, deadline TEXT, packet_json TEXT,
    decision_json TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS actions (
    idempotency_key TEXT PRIMARY KEY, review_id TEXT UNIQUE, run_id TEXT, candidate_id TEXT,
    action TEXT, payload_json TEXT, authorization_json TEXT, status TEXT,
    attempts INTEGER DEFAULT 0, jira_key TEXT, last_error TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS inbox (
    event_id TEXT PRIMARY KEY, type TEXT, received_at TEXT, outcome TEXT, detail TEXT);
"""

JSON_COLUMNS = ("state_json", "packet_json", "decision_json", "payload_json", "authorization_json", "detail")


class Store:
    def __init__(self, db_path: str | Path = "data/workflow.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)  # explicit transactions only
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def transaction(self) -> "Transaction":
        return Transaction(self.conn)

    def one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        row = self.conn.execute(sql, args).fetchone()
        return _decode(row) if row else None

    def all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [_decode(r) for r in self.conn.execute(sql, args).fetchall()]

    def execute(self, sql: str, *args: Any) -> None:
        self.conn.execute(sql, tuple(json.dumps(a) if isinstance(a, (dict, list)) else a for a in args))

    def close(self) -> None:
        self.conn.close()


class Transaction:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for col in JSON_COLUMNS:
        if out.get(col):
            out[col] = json.loads(out[col])
    return out
