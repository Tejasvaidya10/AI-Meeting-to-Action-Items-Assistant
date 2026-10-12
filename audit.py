"""Audit Logger: one JSON line per event plus a JSON snapshot per run.

logs/audit.jsonl          every event from every run (filter by run_id)
logs/runs/<run_id>.json   latest full RunState for that run

A failed log write is recorded in state.errors and never stops the run or
changes candidate status.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state import RunState, utc_now


class AuditLogger:
    def __init__(self, log_dir: str | Path = "logs") -> None:
        self.log_dir = Path(log_dir)
        self.events_path = self.log_dir / "audit.jsonl"
        self.runs_dir = self.log_dir / "runs"

    def log(self, state: RunState, step: str, event: str, **details: Any) -> None:
        entry = {
            "timestamp": utc_now(),
            "run_id": state.run_id,
            "step": step,
            "event": event,
            "status": state.status,
            "retry_count": dict(state.retry_count),
            **details,
        }
        state.events.append(entry)
        self._write(state, self.events_path, json.dumps(entry, ensure_ascii=False) + "\n", mode="a")

    def snapshot(self, state: RunState) -> None:
        path = self.runs_dir / f"{state.run_id}.json"
        self._write(state, path, state.model_dump_json(indent=2), mode="w")

    def _write(self, state: RunState, path: Path, text: str, mode: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open(mode, encoding="utf-8") as f:
                f.write(text)
        except OSError as exc:
            state.record_error("audit", f"log write failed for {path}: {exc}")
