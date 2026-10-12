"""Run state for one transcript (Task Status Tracker).

One RunState is created per run and carried through every step. The
transcript text itself is never stored: only its SHA-256 hash and length.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal[
    "started",
    "input_validated",
    "extracted",
    "awaiting_review",
    "failed",
    "completed",
    "completed_with_items_needing_review",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run-{uuid.uuid4().hex[:8]}")
    status: RunStatus = "started"
    meeting_date: str
    transcript_sha256: str
    transcript_chars: int
    extraction: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    retry_count: dict[str, int] = Field(default_factory=dict)
    errors: list[dict[str, str]] = Field(default_factory=list)
    error: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    timestamps: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def start(cls, transcript: str, meeting_date: str) -> "RunState":
        state = cls(
            meeting_date=meeting_date,
            transcript_sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            transcript_chars=len(transcript),
        )
        state.timestamps["started"] = utc_now()
        return state

    def set_status(self, status: RunStatus) -> None:
        """Move the run to a new stage and record when it was entered."""
        self.status = status
        self.timestamps[status] = utc_now()

    def increment_retry(self, step: str) -> int:
        """Count one more attempt of a bounded step and return the new count."""
        self.retry_count[step] = self.retry_count.get(step, 0) + 1
        return self.retry_count[step]

    def record_error(self, step: str, message: str) -> None:
        self.errors.append({"step": step, "message": message, "timestamp": utc_now()})
