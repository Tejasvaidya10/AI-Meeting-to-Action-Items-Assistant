"""Simulated Jira Cloud receiver (the external system).

It keeps its own SQLite file so tests can count the issues it actually holds.
Like real Jira it has no idempotency header: the caller's idempotency key is
stored as a label and can be searched afterwards (JQL `labels = idem-...`).
Creating the same payload twice therefore creates two issues, which is why
the workflow checks before it retries.

Faults can be injected for testing, one per call, in order:
  "unavailable"        the call fails and nothing is created (HTTP 503)
  "lose_confirmation"  the issue IS created, then the response is lost (timeout)
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from state import utc_now


class JiraUnavailable(Exception):
    """Nothing was created; safe to retry later."""


class JiraConfirmationLost(Exception):
    """The request may or may not have been applied; check before retrying."""


class SimulatedJira:
    def __init__(self, db_path: str | Path = "data/jira_sim.db", faults: list[str] | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        env_fault = os.getenv("JIRA_FAULT")
        self.faults = list(faults) if faults is not None else ([env_fault] if env_fault else [])
        self.calls = 0
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS issues (key TEXT PRIMARY KEY, project TEXT, summary TEXT,"
                " assignee TEXT, due_date TEXT, description TEXT, labels TEXT, created_at TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_issue(self, project: str, fields: dict[str, Any], idempotency_key: str) -> str:
        self.calls += 1
        fault = self.faults.pop(0) if self.faults else None
        if fault == "unavailable":
            raise JiraUnavailable("503 Service Unavailable")
        with self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM issues WHERE project = ?", (project,)).fetchone()[0]
            key = f"{project}-{n + 1}"
            conn.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, project, fields["summary"], fields["assignee"], fields["due_date"],
                 fields["description"], json.dumps([f"idem-{idempotency_key}"]), utc_now()),
            )
        if fault == "lose_confirmation":
            raise JiraConfirmationLost("read timeout after request was sent")
        return key

    def find_by_idempotency_key(self, idempotency_key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key FROM issues WHERE labels LIKE ?", (f'%"idem-{idempotency_key}"%',)
            ).fetchone()
        return row["key"] if row else None

    def issues(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM issues ORDER BY created_at")]

    def count(self) -> int:
        return len(self.issues())
