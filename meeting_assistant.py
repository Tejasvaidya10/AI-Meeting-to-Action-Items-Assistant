"""Milestone 2 prototype for transcript-to-action-item extraction.

The model only proposes structured candidates. Local validation and approval
logic decide whether a candidate can continue to a Jira handoff.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from audit import AuditLogger
from state import RunState


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3)
    owner_text: str | None = None
    due_date_text: str | None = None
    evidence: str = Field(min_length=3)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    decisions: list[str]
    tasks: list[ActionItem]


class CandidateResult(BaseModel):
    candidate_id: str
    title: str
    owner_text: str | None
    owner_id: str | None
    due_date_text: str | None
    due_date: str | None
    evidence: str
    validation: Literal["PASS", "FLAG"]
    reasons: list[str]
    review_status: Literal["needs_review", "approved", "rejected", "expired"] = "needs_review"
    jira_result: str | None = None


DIRECTORY = {
    "anusha": {"name": "Anusha", "jira_id": "anusha"},
    "gouri": {"name": "Gouri", "jira_id": "gouri"},
    "kriday": {"name": "Kriday", "jira_id": "kriday"},
    "tejas": {"name": "Tejas", "jira_id": "tejas"},
    "shubhada": {"name": "Shubhada", "jira_id": "shubhada"},
    "marco": {"name": "Marco", "jira_id": "marco"},
    "priya": {"name": "Priya", "jira_id": "priya"},
}


def _parse_due_date(text: str | None, meeting_date: date) -> str | None:
    if not text:
        return None
    value = text.strip().lower()
    weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6}
    if value in weekdays:
        delta = (weekdays[value] - meeting_date.weekday()) % 7
        return (meeting_date + timedelta(days=delta or 7)).isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def validate_candidates(extraction: Extraction, meeting_date: date, open_titles: set[str] | None = None) -> list[CandidateResult]:
    open_titles = {x.casefold() for x in (open_titles or set())}
    results = []
    for task in extraction.tasks:
        reasons: list[str] = []
        owner_id = None
        if not task.owner_text:
            reasons.append("owner is missing")
        else:
            owner = DIRECTORY.get(task.owner_text.strip().casefold())
            if owner:
                owner_id = owner["jira_id"]
            else:
                reasons.append("owner is not in the project directory")
        due_date = _parse_due_date(task.due_date_text, meeting_date)
        if not due_date:
            reasons.append("due date is missing or cannot be normalized")
        if task.title.casefold() in open_titles:
            reasons.append("likely duplicate open task")
        results.append(CandidateResult(
            candidate_id=f"cand-{uuid.uuid4().hex[:8]}", title=task.title,
            owner_text=task.owner_text, owner_id=owner_id,
            due_date_text=task.due_date_text, due_date=due_date,
            evidence=task.evidence, validation="PASS" if not reasons else "FLAG",
            reasons=reasons,
        ))
    return results


def extract_with_openai(transcript: str, model: str | None = None, client: Any = None) -> Extraction:
    """Call a hosted model and enforce the Pydantic schema on its JSON output."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # parse() converts the Pydantic model to a strict-mode schema (every key required,
    # no defaults); the raw model_json_schema() is rejected by strict mode.
    response = client.chat.completions.parse(
        model=model,
        temperature=0,
        response_format=Extraction,
        messages=[
            {"role": "system", "content": "Extract only explicit action items. Never invent owners or dates. Return the required JSON schema and include exact transcript evidence."},
            {"role": "user", "content": transcript},
        ],
    )
    raw = response.choices[0].message.content or ""
    return Extraction.model_validate_json(raw)


def run_workflow(transcript: str, meeting_date: str, extractor: Callable[[str], Extraction], approve: set[str] | None = None, open_titles: set[str] | None = None, log_dir: str | Path = "logs") -> dict[str, Any]:
    audit = AuditLogger(log_dir)
    state = RunState.start(transcript or "", meeting_date)
    audit.log(state, "input_check", "run_started", meeting_date=meeting_date, transcript_chars=state.transcript_chars)
    if not transcript or not transcript.strip():
        return _fail(state, audit, "input_check", "transcript is empty")
    state.set_status("input_validated")
    audit.log(state, "input_check", "input_valid")

    state.increment_retry("model_extraction")
    audit.log(state, "model_extraction", "model_call_started", attempt=1)
    try:
        extraction = extractor(transcript)
    except (ValidationError, ValueError, KeyError) as first_error:
        state.record_error("model_extraction", str(first_error))
        audit.log(state, "model_extraction", "schema_invalid", result="RETRY", attempt=1, error=str(first_error))
        state.increment_retry("model_extraction")
        audit.log(state, "model_extraction", "model_call_started", attempt=2)
        try:
            extraction = extractor(transcript)
        except Exception as second_error:
            return _fail(state, audit, "model_extraction", f"schema failure after one retry: {second_error}", attempt=2)
    except Exception as exc:
        _fail(state, audit, "model_extraction", f"model call failed: {exc}", attempt=1)
        raise
    state.extraction = extraction.model_dump()
    state.set_status("extracted")
    audit.log(state, "model_extraction", "schema_valid", attempt=state.retry_count["model_extraction"], tasks=len(extraction.tasks))

    candidates = validate_candidates(extraction, date.fromisoformat(meeting_date), open_titles)
    state.candidates = [c.model_dump() for c in candidates]
    for c in candidates:
        audit.log(state, "validation", "validated", candidate_id=c.candidate_id, result=c.validation, reasons=c.reasons)
    state.set_status("awaiting_review")
    audit.log(state, "human_review", "awaiting_review", candidates=len(candidates), flagged=sum(c.validation == "FLAG" for c in candidates))
    audit.snapshot(state)
    if approve is None:
        return state.model_dump()  # pause for human review; the UI calls submit_review()
    return submit_review(state.model_dump(), approve, log_dir)


def submit_review(run: dict[str, Any], approve: set[str], log_dir: str | Path = "logs", reject: set[str] | None = None) -> dict[str, Any]:
    """Apply the reviewer's decisions, run the approval guard and simulated Jira handoff, and close the run.

    The guard passes an item only if it is approved, its owner and due date are resolved, and it has
    no Jira key yet. Items in neither set stay unresolved (needs_review). Everything else is blocked.
    """
    audit = AuditLogger(log_dir)
    state = RunState.model_validate(run)
    reject = reject or set()
    for candidate in state.candidates:
        cid = candidate["candidate_id"]
        if candidate["jira_result"]:
            # Already sent on an earlier submission: keep the key and never create a second issue.
            audit.log(state, "approval_guard", "blocked", candidate_id=cid, result="BLOCKED", reason="already created", jira_result=candidate["jira_result"])
            continue
        if cid in approve:
            candidate["review_status"] = "approved"
        elif cid in reject:
            candidate["review_status"] = "rejected"
        else:
            candidate["review_status"] = "needs_review"
        audit.log(state, "human_review", "review_decision", candidate_id=cid, result=candidate["review_status"])
        missing = [field for field, key in (("owner", "owner_id"), ("due date", "due_date")) if not candidate[key]]
        if candidate["review_status"] != "approved":
            audit.log(state, "approval_guard", "blocked", candidate_id=cid, result="BLOCKED", reason=f"review_status={candidate['review_status']}")
        elif missing:
            candidate["review_status"] = "needs_review"
            audit.log(state, "approval_guard", "blocked", candidate_id=cid, result="BLOCKED", reason="unresolved " + ", ".join(missing))
        else:
            audit.log(state, "approval_guard", "allowed", candidate_id=cid, result="ALLOWED")
            candidate["jira_result"] = f"SIM-{cid[-4:]}"
            audit.log(state, "jira_handoff", "created", candidate_id=cid, result=candidate["jira_result"])
    state.set_status("completed" if all(c["review_status"] != "needs_review" for c in state.candidates) else "completed_with_items_needing_review")
    audit.log(state, "audit", "run_completed", result=state.status,
              approved=sum(c["review_status"] == "approved" for c in state.candidates),
              jira_created=sum(c["jira_result"] is not None for c in state.candidates))
    audit.snapshot(state)
    return state.model_dump()


def _fail(state: RunState, audit: AuditLogger, step: str, message: str, **details: Any) -> dict[str, Any]:
    state.error = message
    state.record_error(step, message)
    state.set_status("failed")
    audit.log(state, step, "run_failed", result="STOP", reason=message, **details)
    audit.snapshot(state)
    return state.model_dump()
