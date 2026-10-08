"""Milestone 2 prototype for transcript-to-action-item extraction.

The model only proposes structured candidates. Local validation and approval
logic decide whether a candidate can continue to a Jira handoff.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
    review_status: Literal["needs_review", "approved", "rejected"] = "needs_review"
    jira_result: str | None = None


DIRECTORY = {
    "anusha": {"name": "Anusha", "jira_id": "anusha"},
    "gouri": {"name": "Gouri", "jira_id": "gouri"},
    "kriday": {"name": "Kriday", "jira_id": "kriday"},
    "mourya": {"name": "Mourya", "jira_id": "mourya"},
    "tejas": {"name": "Tejas", "jira_id": "tejas"},
    "shubhada": {"name": "Shubhada", "jira_id": "shubhada"},
    "marco": {"name": "Marco", "jira_id": "marco"},
    "priya": {"name": "Priya", "jira_id": "priya"},
}


def _log(state: dict[str, Any], step: str, **details: Any) -> None:
    state.setdefault("events", []).append({
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "step": step,
        **details,
    })


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
    schema = Extraction.model_json_schema()
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_schema", "json_schema": {"name": "meeting_extraction", "strict": True, "schema": schema}},
        messages=[
            {"role": "system", "content": "Extract only explicit action items. Never invent owners or dates. Return the required JSON schema and include exact transcript evidence."},
            {"role": "user", "content": transcript},
        ],
    )
    raw = response.choices[0].message.content or ""
    return Extraction.model_validate_json(raw)


def run_workflow(transcript: str, meeting_date: str, extractor: Callable[[str], Extraction], approve: set[str] | None = None, open_titles: set[str] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"run_id": f"run-{uuid.uuid4().hex[:8]}", "status": "started", "transcript": transcript, "events": []}
    _log(state, "input_check")
    if not transcript or not transcript.strip():
        state.update(status="failed", error="transcript is empty")
        _log(state, "input_check", result="STOP", reason=state["error"])
        return state
    _log(state, "model_extraction", attempt=1)
    try:
        extraction = extractor(transcript)
    except (ValidationError, ValueError, KeyError) as first_error:
        _log(state, "model_extraction", result="RETRY", error=str(first_error))
        try:
            extraction = extractor(transcript)
        except Exception as second_error:
            state.update(status="failed", error=f"schema failure after one retry: {second_error}")
            _log(state, "model_extraction", result="STOP", attempt=2, error=str(second_error))
            return state
    state["extraction"] = extraction.model_dump()
    candidates = validate_candidates(extraction, date.fromisoformat(meeting_date), open_titles)
    state["candidates"] = [c.model_dump() for c in candidates]
    _log(state, "validation", result="PASS_OR_FLAG", flagged=sum(c.validation == "FLAG" for c in candidates))
    approve = approve or set()
    for candidate in candidates:
        if candidate.candidate_id in approve:
            candidate.review_status = "approved"
            candidate.jira_result = f"SIM-{candidate.candidate_id[-4:]}"
            _log(state, "jira_handoff", candidate_id=candidate.candidate_id, result=candidate.jira_result)
        else:
            candidate.review_status = "needs_review"
            _log(state, "approval_guard", candidate_id=candidate.candidate_id, result="BLOCKED")
    state["candidates"] = [c.model_dump() for c in candidates]
    state["status"] = "completed" if all(c.review_status != "needs_review" for c in candidates) else "completed_with_items_needing_review"
    _log(state, "audit", result=state["status"])
    return state


def write_jsonl(state: dict[str, Any], path: str | Path = "execution_log.jsonl") -> None:
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False) + "\n")
