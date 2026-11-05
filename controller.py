"""Workflow Controller (Milestone 3): events in, durable state, guarded Jira effects.

Events are dicts {"event_id", "type", "payload"}:
  transcript.received  {meeting_id, meeting_date, transcript, organizer, open_titles?}
  review.decision      {review_id, decision: approve|reject, reviewer, content_version, edits?}
  timer.tick           {now?}

Every event_id is recorded in the inbox in the same transaction as the state
change it causes, so a redelivered event is ignored. External effects go
through the actions outbox: the row is committed before Jira is called, and a
row left in "sending" or "uncertain" is checked against Jira before any retry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from audit import AuditLogger
from jira_sim import JiraConfirmationLost, JiraUnavailable, SimulatedJira
from meeting_assistant import DIRECTORY, Extraction, _parse_due_date, run_workflow
from mock_extractor import mock_extractor
from state import RunState
from store import Store

PROJECT_KEY = "MTG"
PROJECT_LEAD = "kriday"                  # escalation owner
REVIEW_SLA = timedelta(hours=24)         # organizer's window, then escalate
ESCALATION_SLA = timedelta(hours=48)     # project lead's window, then expire (withheld)
APPROVAL_TTL = timedelta(hours=24)       # an approval must be executed within this time
MAX_ATTEMPTS = 3                         # Jira create attempts per approved action

RULES = {
    "approval": "R1 No Jira issue is created without an explicit, unexpired approval of this exact content version.",
    "owner": "R2 The assignee must resolve to a person in the project directory.",
    "due date": "R3 The due date must normalize to a calendar date.",
    "duplicate": "R4 A likely duplicate of an open task needs the reviewer to confirm it is new.",
}
OPEN_REVIEW = ("pending", "escalated")
OPEN_ACTION = ("queued", "sending", "uncertain")


class SimulatedCrash(RuntimeError):
    """Raised by the crash hook to stand in for the process being killed."""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def content_version(proposed_action: dict[str, Any]) -> str:
    """Hash of exactly what would be sent; an approval is bound to this value."""
    canonical = json.dumps(proposed_action, sort_keys=True, separators=(",", ":"))
    return "v-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class Workflow:
    def __init__(self, db_path: str | Path = "data/workflow.db", jira: SimulatedJira | None = None,
                 log_dir: str | Path = "logs", extractor: Callable[[str], Extraction] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store = Store(db_path)
        self.jira = jira or SimulatedJira()
        self.audit = AuditLogger(log_dir)
        self.log_dir = log_dir
        self.extractor = extractor or mock_extractor
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ events
    def handle(self, event: dict[str, Any], crash_before_send: bool = False) -> dict[str, Any]:
        event_id, etype = event.get("event_id"), event.get("type")
        payload = event.get("payload") or {}
        if not event_id:
            return {"outcome": "malformed", "detail": "missing event_id (cannot be deduplicated)"}
        seen = self.store.one("SELECT * FROM inbox WHERE event_id = ?", event_id)
        if seen:
            return {"event_id": event_id, "outcome": "duplicate_ignored", "first_outcome": seen["outcome"]}

        now = parse_time(payload["now"]) if etype == "timer.tick" and payload.get("now") else self.clock()
        handler = {"transcript.received": self._on_transcript, "review.decision": self._on_decision,
                   "timer.tick": self._on_tick}.get(etype)
        to_execute: list[str] = []
        with self.store.transaction():
            if handler is None:
                result = {"outcome": "unsupported_type", "detail": f"unknown event type {etype!r}"}
            else:
                result = handler(payload, now, to_execute)
            self.store.execute("INSERT INTO inbox VALUES (?, ?, ?, ?, ?)",
                               event_id, etype, iso(now), result["outcome"], result)
        result["event_id"] = event_id
        if crash_before_send and to_execute:
            raise SimulatedCrash(f"process stopped after committing {event_id}, before calling Jira")
        result["actions"] = {key: self._execute(key, now) for key in to_execute}
        return result

    def resume(self, now: datetime | None = None) -> dict[str, str]:
        """Called on startup: finish or reconcile every action a previous process left open."""
        now = now or self.clock()
        keys = [a["idempotency_key"] for a in self.store.all(
            "SELECT idempotency_key FROM actions WHERE status IN (?, ?, ?)", *OPEN_ACTION)]
        return {key: self._execute(key, now) for key in keys}

    def _on_transcript(self, p: dict[str, Any], now: datetime, _: list[str]) -> dict[str, Any]:
        missing = [k for k in ("meeting_id", "meeting_date", "transcript", "organizer") if not p.get(k)]
        if missing:
            return {"outcome": "malformed", "detail": f"missing {', '.join(missing)}"}
        sha = hashlib.sha256(p["transcript"].encode("utf-8")).hexdigest()
        existing = self.store.one("SELECT run_id, status FROM runs WHERE meeting_id = ? AND transcript_sha256 = ?",
                                  p["meeting_id"], sha)
        if existing and existing["status"] != "failed":  # same meeting resent under a new event_id (e.g. webhook redelivery)
            return {"outcome": "duplicate_content", "run_id": existing["run_id"]}

        run = run_workflow(p["transcript"], p["meeting_date"], self.extractor,
                           open_titles=set(p.get("open_titles") or []), log_dir=self.log_dir)
        state = RunState.model_validate(run)
        if existing:  # a resend after a failed run replaces that row; its events stay in this run's history
            state.events = self._load_run(existing["run_id"]).events + state.events
            self.audit.log(state, "input_check", "resend_after_failed_run", previous_run_id=existing["run_id"])
            self.store.execute("DELETE FROM runs WHERE run_id = ?", existing["run_id"])
        if state.status == "failed":
            self.audit.log(state, "monitoring", "alert", reason=state.error, next="organizer resends the transcript")
        self.audit.snapshot(state)
        self.store.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)", state.run_id, p["meeting_id"],
                           sha, state.status, state.model_dump_json(), iso(now))
        if state.status == "failed":
            return {"outcome": "run_failed", "run_id": state.run_id, "detail": state.error}

        review_ids = []
        for cand in state.candidates:
            review_id = "rev-" + cand["candidate_id"].removeprefix("cand-")
            packet = self._packet(state, p["meeting_id"], cand)
            self.store.execute("INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               review_id, state.run_id, cand["candidate_id"], "pending", p["organizer"],
                               PROJECT_LEAD, iso(now + REVIEW_SLA), packet, None, iso(now), iso(now))
            self.audit.log(state, "human_review", "review_requested", candidate_id=cand["candidate_id"],
                           review_id=review_id, assigned_to=p["organizer"],
                           content_version=packet["content_version"], deadline=iso(now + REVIEW_SLA))
            review_ids.append(review_id)
        self._save_run(state, now)
        self._refresh_run_status(state.run_id, now)
        return {"outcome": "run_created", "run_id": state.run_id, "reviews": review_ids}

    def _on_decision(self, p: dict[str, Any], now: datetime, to_execute: list[str]) -> dict[str, Any]:
        missing = [k for k in ("review_id", "decision", "reviewer", "content_version") if not p.get(k)]
        if missing or p.get("decision") not in ("approve", "reject"):
            return {"outcome": "malformed", "detail": f"missing {', '.join(missing)}" if missing else "decision must be approve or reject"}
        review = self.store.one("SELECT * FROM reviews WHERE review_id = ?", p["review_id"])
        if review is None:
            return {"outcome": "unknown_review", "detail": p["review_id"]}
        if review["status"] in OPEN_REVIEW:
            self._apply_deadlines(review, now)  # a passed deadline counts even if the timer has not fired yet
        if review["status"] == "expired":
            return {"outcome": "late_ignored", "review_id": review["review_id"], "detail": "review expired; action withheld"}
        if review["status"] not in OPEN_REVIEW:
            return {"outcome": "already_decided", "review_id": review["review_id"], "status": review["status"]}
        allowed = {review["assigned_to"]} | ({review["escalation_to"]} if review["status"] == "escalated" else set())
        if p["reviewer"] not in allowed:
            return {"outcome": "unauthorized", "review_id": review["review_id"], "detail": f"{p['reviewer']} not in {sorted(allowed)}"}
        packet = review["packet_json"]
        if p["content_version"] != packet["content_version"]:
            return {"outcome": "stale_version", "review_id": review["review_id"],
                    "detail": f"reviewed {p['content_version']}, current is {packet['content_version']}"}

        state = self._load_run(review["run_id"])
        cand = next(c for c in state.candidates if c["candidate_id"] == review["candidate_id"])
        decision = {"decision": p["decision"], "reviewer": p["reviewer"], "decided_at": iso(now),
                    "reviewed_version": p["content_version"], "comment": p.get("comment")}
        if p["decision"] == "reject":
            cand["review_status"] = "rejected"
            self._set_review(review["review_id"], "rejected", now, decision=decision)
            self.audit.log(state, "human_review", "review_decision", candidate_id=cand["candidate_id"],
                           review_id=review["review_id"], result="rejected", reviewer=p["reviewer"])
            self._save_run(state, now)
            self._refresh_run_status(state.run_id, now)
            return {"outcome": "rejected", "review_id": review["review_id"]}

        if p.get("edits"):
            self._apply_edits(cand, p["edits"], state.meeting_date)
            packet = self._packet(state, packet["meeting_id"], cand, review_id=review["review_id"])
            decision["edits"] = p["edits"]
        fields = packet["proposed_action"]["fields"]
        unresolved = [name for name, key in (("owner", "assignee"), ("due date", "due_date")) if not fields[key]]
        if unresolved:  # approval guard: cannot authorize an incomplete issue
            self.audit.log(state, "approval_guard", "blocked", candidate_id=cand["candidate_id"], result="BLOCKED",
                           review_id=review["review_id"], reason="unresolved " + ", ".join(unresolved))
            self._save_run(state, now)
            return {"outcome": "needs_changes", "review_id": review["review_id"],
                    "detail": "approve with edits for: " + ", ".join(unresolved)}

        authorization = {
            "action": packet["proposed_action"]["action"],
            "target": {**packet["proposed_action"]["target"], "assignee": fields["assignee"]},
            "content_version": packet["content_version"],
            "approved_by": p["reviewer"], "approved_at": iso(now), "expires_at": iso(now + APPROVAL_TTL),
        }
        key = f"{review['review_id']}:{packet['content_version']}"
        cand["review_status"] = "approved"
        self._set_review(review["review_id"], "approved", now, decision={**decision, "authorization": authorization},
                         packet=packet)
        self.store.execute("INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           key, review["review_id"], state.run_id, cand["candidate_id"], authorization["action"],
                           packet["proposed_action"], authorization, "queued", 0, None, None, iso(now))
        self.audit.log(state, "human_review", "review_decision", candidate_id=cand["candidate_id"],
                       review_id=review["review_id"], result="approved", reviewer=p["reviewer"],
                       content_version=packet["content_version"], expires_at=authorization["expires_at"],
                       edited=bool(p.get("edits")))
        self._save_run(state, now)
        to_execute.append(key)
        return {"outcome": "approved", "review_id": review["review_id"], "idempotency_key": key}

    def _on_tick(self, _: dict[str, Any], now: datetime, to_execute: list[str]) -> dict[str, Any]:
        escalated, expired = [], []
        for review in self.store.all("SELECT * FROM reviews WHERE status IN (?, ?) AND deadline <= ?",
                                     *OPEN_REVIEW, iso(now)):
            changes = self._apply_deadlines(review, now)
            if "escalated" in changes:
                escalated.append(review["review_id"])
            if "expired" in changes:
                expired.append(review["review_id"])
        to_execute += [a["idempotency_key"] for a in self.store.all(
            "SELECT idempotency_key FROM actions WHERE status IN (?, ?, ?)", *OPEN_ACTION)]
        return {"outcome": "tick", "escalated": escalated, "expired": expired}

    # ----------------------------------------------------------- external effect
    def _execute(self, key: str, now: datetime) -> str:
        """Approval guard + Jira call for one outbox row. Safe to call any number of times."""
        action = self.store.one("SELECT * FROM actions WHERE idempotency_key = ?", key)
        if action["status"] not in OPEN_ACTION:
            return action["status"]
        if action["status"] in ("sending", "uncertain"):
            # A previous attempt may have reached Jira: look before sending again.
            found = self.jira.find_by_idempotency_key(key)
            self._log_action(action, now, "reconciled_found" if found else "reconciled_not_found", jira_key=found)
            if found:
                return self._finish(action, now, "done", jira_key=found)

        auth, proposed = action["authorization_json"], action["payload_json"]
        if content_version(proposed) != auth["content_version"]:
            return self._finish(action, now, "blocked", error="content changed after approval")
        if now > parse_time(auth["expires_at"]):
            return self._finish(action, now, "blocked", error="approval expired before execution; re-review required")
        if action["attempts"] >= MAX_ATTEMPTS:
            return self._finish(action, now, "failed", error=f"gave up after {MAX_ATTEMPTS} attempts")

        attempt = action["attempts"] + 1
        with self.store.transaction():
            self.store.execute("UPDATE actions SET status = 'sending', attempts = ?, updated_at = ? WHERE idempotency_key = ?",
                               attempt, iso(now), key)
        action["attempts"] = attempt
        self._log_action(action, now, "jira_call_started", attempt=attempt)
        try:
            jira_key = self.jira.create_issue(auth["target"]["project"], proposed["fields"], key)
        except JiraUnavailable as exc:
            status = "queued" if attempt < MAX_ATTEMPTS else "failed"
            if status == "failed":
                return self._finish(action, now, "failed", error=f"{exc}; gave up after {attempt} attempts")
            with self.store.transaction():
                self.store.execute("UPDATE actions SET status = ?, last_error = ?, updated_at = ? WHERE idempotency_key = ?",
                                   status, str(exc), iso(now), key)
            self._log_action(action, now, "jira_unavailable", attempt=attempt, error=str(exc), next="retry on next tick")
            return status
        except (JiraConfirmationLost, TimeoutError) as exc:
            with self.store.transaction():
                self.store.execute("UPDATE actions SET status = 'uncertain', last_error = ?, updated_at = ? WHERE idempotency_key = ?",
                                   str(exc), iso(now), key)
            self._log_action(action, now, "outcome_uncertain", attempt=attempt, error=str(exc), next="reconcile before retry")
            return "uncertain"
        return self._finish(action, now, "done", jira_key=jira_key)

    def _finish(self, action: dict[str, Any], now: datetime, status: str, jira_key: str | None = None,
                error: str | None = None) -> str:
        with self.store.transaction():
            self.store.execute("UPDATE actions SET status = ?, jira_key = ?, last_error = ?, updated_at = ? WHERE idempotency_key = ?",
                               status, jira_key, error, iso(now), action["idempotency_key"])
            state = self._load_run(action["run_id"])
            cand = next(c for c in state.candidates if c["candidate_id"] == action["candidate_id"])
            cand["jira_result"] = jira_key
            event = {"done": "created", "blocked": "blocked", "failed": "failed"}[status]
            self.audit.log(state, "jira_handoff", event, candidate_id=action["candidate_id"],
                           idempotency_key=action["idempotency_key"], result=jira_key or status.upper(),
                           attempts=action["attempts"], **({"error": error} if error else {}))
            if status == "failed":
                self.audit.log(state, "monitoring", "alert", candidate_id=action["candidate_id"], reason=error)
            self._save_run(state, now)
            self._refresh_run_status(action["run_id"], now)
        return status

    # ---------------------------------------------------------------- helpers
    def _packet(self, state: RunState, meeting_id: str, cand: dict[str, Any], review_id: str | None = None) -> dict[str, Any]:
        """Everything the reviewer needs: proposed action, evidence, rules and conflicts."""
        proposed = {
            "action": "jira.create_issue",
            "target": {"system": "jira", "project": PROJECT_KEY},
            "fields": {"summary": cand["title"], "assignee": cand["owner_id"], "due_date": cand["due_date"],
                       "description": f'From meeting {meeting_id} on {state.meeting_date}. Evidence: "{cand["evidence"]}"'},
        }
        rules = [RULES["approval"]] + [rule for word, rule in RULES.items()
                                       if word != "approval" and any(word in r for r in cand["reasons"])]
        return {
            "review_id": review_id or "rev-" + cand["candidate_id"].removeprefix("cand-"),
            "run_id": state.run_id, "candidate_id": cand["candidate_id"], "meeting_id": meeting_id,
            "proposed_action": proposed, "content_version": content_version(proposed),
            "evidence": {"quote": cand["evidence"], "owner_text": cand["owner_text"],
                         "due_date_text": cand["due_date_text"], "transcript_sha256": state.transcript_sha256},
            "validation": cand["validation"], "conflicts": cand["reasons"], "rules": rules,
        }

    @staticmethod
    def _apply_edits(cand: dict[str, Any], edits: dict[str, str], meeting_date: str) -> None:
        if "owner" in edits:
            person = DIRECTORY.get(edits["owner"].strip().casefold())
            cand["owner_text"], cand["owner_id"] = edits["owner"], person["jira_id"] if person else None
        if "due_date" in edits:
            cand["due_date_text"] = edits["due_date"]
            cand["due_date"] = _parse_due_date(edits["due_date"], date.fromisoformat(meeting_date))

    def _apply_deadlines(self, review: dict[str, Any], now: datetime) -> list[str]:
        """Apply every review deadline that has passed by `now`, as the timer would have.

        Shared by timer.tick and review.decision, so a decision is judged by the deadlines
        even when no tick has run since they passed. Updates `review` in place.
        """
        changes = []
        if review["status"] == "pending" and parse_time(review["deadline"]) <= now:
            deadline = iso(parse_time(review["deadline"]) + ESCALATION_SLA)
            self._set_review(review["review_id"], "escalated", now, deadline=deadline)
            state = self._load_run(review["run_id"])
            self.audit.log(state, "human_review", "escalated", candidate_id=review["candidate_id"],
                           review_id=review["review_id"], escalated_to=review["escalation_to"], deadline=deadline)
            self._save_run(state, now)
            review["status"], review["deadline"] = "escalated", deadline
            changes.append("escalated")
        if review["status"] == "escalated" and parse_time(review["deadline"]) <= now:
            self._expire(review, now)
            review["status"] = "expired"
            changes.append("expired")
        return changes

    def _expire(self, review: dict[str, Any], now: datetime) -> None:
        self._set_review(review["review_id"], "expired", now)
        state = self._load_run(review["run_id"])
        cand = next(c for c in state.candidates if c["candidate_id"] == review["candidate_id"])
        cand["review_status"] = "expired"
        self.audit.log(state, "human_review", "expired_withheld", candidate_id=review["candidate_id"],
                       review_id=review["review_id"], result="WITHHELD")
        self.audit.log(state, "monitoring", "alert", candidate_id=review["candidate_id"],
                       review_id=review["review_id"], reason="review expired with no decision; issue withheld")
        self._save_run(state, now)
        self._refresh_run_status(review["run_id"], now)

    def _set_review(self, review_id: str, status: str, now: datetime, decision: dict | None = None,
                    packet: dict | None = None, deadline: str | None = None) -> None:
        self.store.execute(
            "UPDATE reviews SET status = ?, decision_json = COALESCE(?, decision_json), packet_json = COALESCE(?, packet_json),"
            " deadline = COALESCE(?, deadline), updated_at = ? WHERE review_id = ?",
            status, decision, packet, deadline, iso(now), review_id)

    def _log_action(self, action: dict[str, Any], now: datetime, event: str, **details: Any) -> None:
        with self.store.transaction():
            state = self._load_run(action["run_id"])
            self.audit.log(state, "jira_handoff", event, candidate_id=action["candidate_id"],
                           idempotency_key=action["idempotency_key"], **details)
            self._save_run(state, now)

    def _refresh_run_status(self, run_id: str, now: datetime) -> None:
        """Close the run once every review is decided/expired and every action is finished."""
        reviews = self.store.all("SELECT status FROM reviews WHERE run_id = ?", run_id)
        actions = self.store.all("SELECT status FROM actions WHERE run_id = ?", run_id)
        if any(r["status"] in OPEN_REVIEW for r in reviews) or any(a["status"] in OPEN_ACTION for a in actions):
            return
        state = self._load_run(run_id)
        if state.status != "awaiting_review":
            return
        clean = all(r["status"] in ("approved", "rejected") for r in reviews) and all(a["status"] == "done" for a in actions)
        state.set_status("completed" if clean else "completed_with_items_needing_review")
        self.audit.log(state, "audit", "run_completed", result=state.status,
                       jira_created=sum(a["status"] == "done" for a in actions))
        self._save_run(state, now)

    def _load_run(self, run_id: str) -> RunState:
        return RunState.model_validate(self.store.one("SELECT state_json FROM runs WHERE run_id = ?", run_id)["state_json"])

    def _save_run(self, state: RunState, now: datetime) -> None:
        self.store.execute("UPDATE runs SET status = ?, state_json = ?, updated_at = ? WHERE run_id = ?",
                           state.status, state.model_dump_json(), iso(now), state.run_id)
        self.audit.snapshot(state)

    # ------------------------------------------------------------- read views
    def open_reviews(self) -> list[dict[str, Any]]:
        return self.store.all("SELECT * FROM reviews WHERE status IN (?, ?) ORDER BY created_at", *OPEN_REVIEW)

    def review(self, review_id: str) -> dict[str, Any] | None:
        return self.store.one("SELECT * FROM reviews WHERE review_id = ?", review_id)
