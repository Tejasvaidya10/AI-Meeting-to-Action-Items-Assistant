"""Milestone 3: human checkpoint, event handling, durable state, recovery.

Every effect is verified in the receiver: SimulatedJira's own SQLite file.
"""
from datetime import datetime, timedelta, timezone

import pytest

from controller import SimulatedCrash, Workflow
from jira_sim import SimulatedJira
from meeting_assistant import Extraction

TRANSCRIPT = ("Priya will update the API error messages by Friday. "
              "Someone should clean up the onboarding notes soon.")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


def make_wf(clock, faults=None):
    """A fresh Workflow on the same files: what a restarted process would build."""
    return Workflow("data/workflow.db", jira=SimulatedJira("data/jira_sim.db", faults=faults or []), clock=clock)


def ingest(wf, event_id="evt-t1", meeting_id="m-1", **extra):
    payload = {"meeting_id": meeting_id, "meeting_date": "2026-10-08", "organizer": "shubhada",
               "transcript": TRANSCRIPT, **extra}
    return wf.handle({"event_id": event_id, "type": "transcript.received", "payload": payload})


def decide(wf, review_id, decision="approve", reviewer="shubhada", event_id=None, **extra):
    review = wf.review(review_id)
    payload = {"review_id": review_id, "decision": decision, "reviewer": reviewer,
               "content_version": review["packet_json"]["content_version"], **extra}
    return {"event_id": event_id or f"evt-{decision}-{review_id}", "type": "review.decision", "payload": payload}


def action_status(wf, review_id):
    return wf.store.one("SELECT * FROM actions WHERE review_id = ?", review_id)


# ---- Case 1: approved run -------------------------------------------------
def test_approved_run_creates_exactly_the_approved_issue(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    pass_review, flag_review = run["reviews"]
    packet = wf.review(pass_review)["packet_json"]
    assert packet["validation"] == "PASS" and packet["rules"][0].startswith("R1")

    result = wf.handle(decide(wf, pass_review))
    wf.handle(decide(wf, flag_review, "reject"))

    assert result["outcome"] == "approved" and list(result["actions"].values()) == ["done"]
    issues = wf.jira.issues()
    assert len(issues) == 1
    assert (issues[0]["summary"], issues[0]["assignee"], issues[0]["due_date"]) == \
        tuple(packet["proposed_action"]["fields"][k] for k in ("summary", "assignee", "due_date"))
    run_row = wf.store.one("SELECT * FROM runs WHERE run_id = ?", run["run_id"])
    assert run_row["status"] == "completed"
    assert run_row["state_json"]["candidates"][0]["jira_result"] == issues[0]["key"]


# ---- Case 2: rejection and timeout ---------------------------------------
def test_rejection_withholds_action(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    assert wf.handle(decide(wf, run["reviews"][0], "reject"))["outcome"] == "rejected"
    assert wf.jira.count() == 0
    assert action_status(wf, run["reviews"][0]) is None
    assert wf.review(run["reviews"][0])["status"] == "rejected"


def test_timeout_escalates_then_expires_and_late_decision_is_ignored(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    review_id = run["reviews"][0]

    clock.advance(hours=25)
    tick = wf.handle({"event_id": "tick-1", "type": "timer.tick", "payload": {}})
    assert set(tick["escalated"]) == set(run["reviews"])
    assert wf.review(review_id)["status"] == "escalated"
    # after escalation the organizer or project lead may decide; nobody else
    assert wf.handle(decide(wf, review_id, reviewer="marco", event_id="e-x"))["outcome"] == "unauthorized"

    clock.advance(hours=48)
    tick = wf.handle({"event_id": "tick-2", "type": "timer.tick", "payload": {}})
    assert set(tick["expired"]) == set(run["reviews"])

    late = wf.handle(decide(wf, review_id, reviewer="kriday", event_id="e-late"))
    assert late["outcome"] == "late_ignored"
    assert wf.jira.count() == 0
    run_row = wf.store.one("SELECT * FROM runs")
    assert run_row["status"] == "completed_with_items_needing_review"
    alerts = [e for e in run_row["state_json"]["events"] if e["event"] == "alert"]
    assert {a["review_id"] for a in alerts} == set(run["reviews"])  # each expiry raises an alert


def test_project_lead_can_decide_after_escalation(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    clock.advance(hours=25)
    wf.handle({"event_id": "tick-1", "type": "timer.tick", "payload": {}})
    assert wf.handle(decide(wf, run["reviews"][0], reviewer="kriday"))["outcome"] == "approved"
    assert wf.jira.count() == 1

    # The deadlines also apply when no tick has run since they passed.
    lead_review, organizer_review = ingest(wf, event_id="evt-t2", meeting_id="m-2")["reviews"]
    clock.advance(hours=25)
    assert wf.handle(decide(wf, lead_review, reviewer="kriday"))["outcome"] == "approved"
    clock.advance(hours=48)  # 73 h: the review window has closed
    assert wf.handle(decide(wf, organizer_review))["outcome"] == "late_ignored"
    assert wf.review(organizer_review)["status"] == "expired"
    assert action_status(wf, organizer_review) is None
    assert wf.jira.count() == 2


def test_missing_decision_never_creates_a_task(clock):
    wf = make_wf(clock)
    ingest(wf)
    for hour in range(0, 80, 4):  # ticks, restarts and resumes, but no reviewer ever answers
        clock.now = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc) + timedelta(hours=hour)
        wf = make_wf(clock)
        wf.resume()
        wf.handle({"event_id": f"tick-{hour}", "type": "timer.tick", "payload": {}})
    assert wf.store.all("SELECT * FROM actions") == []
    assert {r["status"] for r in wf.store.all("SELECT status FROM reviews")} == {"expired"}
    assert wf.jira.count() == 0


# ---- Case 3: duplicate events and retries ---------------------------------
def test_duplicate_decision_event_creates_one_issue(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    event = decide(wf, run["reviews"][0])
    first, second = wf.handle(event), wf.handle(event)
    again = wf.handle(decide(wf, run["reviews"][0], event_id="evt-second-click"))
    assert first["outcome"] == "approved"
    assert second["outcome"] == "duplicate_ignored"
    assert again["outcome"] == "already_decided"
    assert wf.jira.count() == 1


def test_redelivered_transcript_does_not_start_second_run(clock):
    wf = make_wf(clock)
    first = ingest(wf)
    assert ingest(wf)["outcome"] == "duplicate_ignored"
    same_content = ingest(wf, event_id="evt-t2")
    assert same_content == {"outcome": "duplicate_content", "run_id": first["run_id"], "event_id": "evt-t2", "actions": {}}
    assert len(wf.store.all("SELECT * FROM runs")) == 1
    assert len(wf.open_reviews()) == 2


def test_unavailable_jira_is_retried_on_tick_without_duplicate(clock):
    wf = make_wf(clock, faults=["unavailable"])
    run = ingest(wf)
    assert list(wf.handle(decide(wf, run["reviews"][0]))["actions"].values()) == ["queued"]
    assert wf.jira.count() == 0
    clock.advance(minutes=5)
    wf.handle({"event_id": "tick-1", "type": "timer.tick", "payload": {}})
    assert action_status(wf, run["reviews"][0])["status"] == "done"
    assert action_status(wf, run["reviews"][0])["attempts"] == 2
    assert wf.jira.count() == 1


def test_retry_limit_fails_and_alerts(clock):
    wf = make_wf(clock, faults=["unavailable"] * 3)
    run = ingest(wf)
    wf.handle(decide(wf, run["reviews"][0]))
    for i in range(3):
        wf.handle({"event_id": f"tick-{i}", "type": "timer.tick", "payload": {}})
    action = action_status(wf, run["reviews"][0])
    assert (action["status"], action["attempts"]) == ("failed", 3)
    events = wf.store.one("SELECT state_json FROM runs")["state_json"]["events"]
    assert any(e["event"] == "alert" for e in events)
    assert wf.jira.count() == 0

    # The model step has its own limit: one schema retry, then the run fails and alerts.
    def outage(_):  # API error or timeout: nothing is recorded, so the same delivery can be retried
        raise ConnectionError("model API unreachable")

    def invalid_reply(_):  # a schema-invalid reply, on the first try and on the retry
        return Extraction.model_validate_json('{"summary": 1}')

    wf.extractor = outage
    with pytest.raises(ConnectionError):
        ingest(wf, event_id="evt-m2", meeting_id="m-2")
    assert wf.store.one("SELECT * FROM inbox WHERE event_id = 'evt-m2'") is None
    wf.extractor = invalid_reply
    failed = ingest(wf, event_id="evt-m2", meeting_id="m-2")
    assert failed["outcome"] == "run_failed"
    assert any(e["event"] == "alert" for e in wf._load_run(failed["run_id"]).events)

    wf = make_wf(clock)  # model healthy again; the organizer resends the same transcript
    resent = ingest(wf, event_id="evt-m2-resend", meeting_id="m-2")
    assert resent["outcome"] == "run_created"
    rows = wf.store.all("SELECT * FROM runs WHERE meeting_id = 'm-2'")
    assert [r["run_id"] for r in rows] == [resent["run_id"]]  # still one run per meeting + transcript
    assert any(e["event"] == "alert" for e in rows[0]["state_json"]["events"])  # failed attempt kept in history
    assert ingest(wf, event_id="evt-m2-again", meeting_id="m-2")["outcome"] == "duplicate_content"


# ---- Case 4: interruption/resume and uncertain outcome ---------------------
def test_pending_review_survives_restart(clock):
    run = ingest(make_wf(clock))
    restarted = make_wf(clock)  # new objects, nothing shared but the files
    assert [r["review_id"] for r in restarted.open_reviews()] == run["reviews"]
    assert restarted.handle(decide(restarted, run["reviews"][0]))["outcome"] == "approved"
    assert restarted.jira.count() == 1


def test_crash_after_approval_is_resumed_once(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    with pytest.raises(SimulatedCrash):
        wf.handle(decide(wf, run["reviews"][0]), crash_before_send=True)
    assert wf.jira.count() == 0
    assert action_status(wf, run["reviews"][0])["status"] == "queued"

    restarted = make_wf(clock)
    assert list(restarted.resume().values()) == ["done"]
    assert restarted.resume() == {}
    assert restarted.handle(decide(restarted, run["reviews"][0]))["outcome"] == "duplicate_ignored"
    assert restarted.jira.count() == 1


def test_lost_confirmation_is_reconciled_not_resent(clock):
    wf = make_wf(clock, faults=["lose_confirmation"])
    run = ingest(wf)
    result = wf.handle(decide(wf, run["reviews"][0]))
    assert list(result["actions"].values()) == ["uncertain"]
    assert wf.jira.count() == 1  # it really was created

    restarted = make_wf(clock)
    assert list(restarted.resume().values()) == ["done"]
    action = action_status(restarted, run["reviews"][0])
    assert action["attempts"] == 1 and action["jira_key"] == restarted.jira.issues()[0]["key"]
    assert restarted.jira.count() == 1
    events = [e["event"] for e in restarted.store.one("SELECT state_json FROM runs")["state_json"]["events"]]
    assert "outcome_uncertain" in events and "reconciled_found" in events


def test_crash_mid_send_with_nothing_created_retries(clock):
    wf = make_wf(clock)
    run = ingest(wf)
    with pytest.raises(SimulatedCrash):
        wf.handle(decide(wf, run["reviews"][0]), crash_before_send=True)
    wf.store.execute("UPDATE actions SET status = 'sending', attempts = 1")  # died while the call was in flight
    restarted = make_wf(clock)
    assert list(restarted.resume().values()) == ["done"]
    assert restarted.jira.count() == 1


# ---- Checkpoint rules -------------------------------------------------------
def test_flag_item_needs_edits_before_approval(clock):
    wf = make_wf(clock)
    flag_review = ingest(wf)["reviews"][1]
    packet = wf.review(flag_review)["packet_json"]
    assert packet["validation"] == "FLAG" and len(packet["rules"]) == 3

    assert wf.handle(decide(wf, flag_review))["outcome"] == "needs_changes"
    assert wf.jira.count() == 0
    edited = wf.handle(decide(wf, flag_review, event_id="e-edit", edits={"owner": "Tejas", "due_date": "2026-10-16"}))
    assert edited["outcome"] == "approved"
    issue = wf.jira.issues()[0]
    assert (issue["assignee"], issue["due_date"]) == ("tejas", "2026-10-16")
    auth = wf.review(flag_review)["decision_json"]["authorization"]
    assert auth["content_version"] != packet["content_version"]  # approval binds to the edited version


def test_stale_content_version_is_refused(clock):
    wf = make_wf(clock)
    review_id = ingest(wf)["reviews"][0]
    event = decide(wf, review_id)
    event["payload"]["content_version"] = "v-000000000000"
    assert wf.handle(event)["outcome"] == "stale_version"
    assert wf.jira.count() == 0


def test_approval_expires_if_not_executed_in_time(clock):
    wf = make_wf(clock, faults=["unavailable"])
    review_id = ingest(wf)["reviews"][0]
    wf.handle(decide(wf, review_id))
    clock.advance(hours=25)
    wf.handle({"event_id": "tick-1", "type": "timer.tick", "payload": {}})
    assert action_status(wf, review_id)["status"] == "blocked"
    assert wf.jira.count() == 0


def test_unexpected_and_malformed_events_change_nothing(clock):
    wf = make_wf(clock)
    assert wf.handle({"event_id": "e-1", "type": "calendar.updated", "payload": {}})["outcome"] == "unsupported_type"
    assert wf.handle({"event_id": "e-2", "type": "transcript.received", "payload": {"meeting_id": "x"}})["outcome"] == "malformed"
    assert wf.handle({"event_id": "e-3", "type": "review.decision", "payload": {
        "review_id": "rev-nope", "decision": "approve", "reviewer": "shubhada", "content_version": "v-1"}})["outcome"] == "unknown_review"
    assert wf.handle({"type": "timer.tick"})["outcome"] == "malformed"
    assert wf.store.all("SELECT * FROM runs") == [] and wf.jira.count() == 0
    assert [r["outcome"] for r in wf.store.all("SELECT outcome FROM inbox")] == ["unsupported_type", "malformed", "unknown_review"]
