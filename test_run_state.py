import json
from pathlib import Path

from meeting_assistant import ActionItem, Extraction, run_workflow, submit_review

TRANSCRIPT = "Priya will update API by Friday. Someone should clean notes."


def fake_extractor(_: str) -> Extraction:
    return Extraction(summary="Sprint follow-up", decisions=[], tasks=[
        ActionItem(title="Update API", owner_text="Priya", due_date_text="Friday", evidence="Priya will update API by Friday."),
        ActionItem(title="Clean notes", owner_text=None, due_date_text=None, evidence="Someone should clean notes."),
    ])


def read_events(run_id: str) -> list[dict]:
    lines = Path("logs/audit.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["run_id"] == run_id]


def test_run_pauses_for_review_without_approvals():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor)
    assert state["status"] == "awaiting_review"
    assert state["events"][-1]["event"] == "awaiting_review"
    snapshot = json.loads(Path(f"logs/runs/{state['run_id']}.json").read_text())
    assert snapshot["status"] == "awaiting_review"


def test_status_sequence_is_timestamped():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor, approve=set())
    assert list(state["timestamps"]) == ["started", "input_validated", "extracted", "awaiting_review",
                                         "completed_with_items_needing_review"]


def test_transcript_text_is_not_persisted():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor)
    assert "transcript" not in state
    assert len(state["transcript_sha256"]) == 64
    snapshot = Path(f"logs/runs/{state['run_id']}.json").read_text()
    assert TRANSCRIPT not in snapshot  # only per-candidate evidence quotes are kept
    assert TRANSCRIPT not in Path("logs/audit.jsonl").read_text()


def test_audit_log_reconstructs_run():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor, approve=set())
    events = read_events(state["run_id"])
    assert events == state["events"]
    assert [e["event"] for e in events] == [
        "run_started", "input_valid", "model_call_started", "schema_valid",
        "validated", "validated", "awaiting_review",
        "review_decision", "blocked", "review_decision", "blocked", "run_completed",
    ]


def test_schema_retry_is_counted_and_logged():
    calls = []

    def flaky(text: str) -> Extraction:
        calls.append(text)
        if len(calls) == 1:
            raise ValueError("bad json")
        return fake_extractor(text)

    state = run_workflow(TRANSCRIPT, "2026-10-08", flaky)
    assert state["retry_count"] == {"model_extraction": 2}
    assert state["errors"][0]["step"] == "model_extraction"
    assert "schema_invalid" in [e["event"] for e in state["events"]]
    assert state["status"] == "awaiting_review"


def test_second_schema_failure_fails_run_and_writes_snapshot():
    def broken(_: str) -> Extraction:
        raise ValueError("bad json")

    state = run_workflow(TRANSCRIPT, "2026-10-08", broken)
    assert state["status"] == "failed"
    assert "after one retry" in state["error"]
    snapshot = json.loads(Path(f"logs/runs/{state['run_id']}.json").read_text())
    assert snapshot["status"] == "failed"


def test_submit_review_logs_decision_guard_and_jira():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor)
    first = state["candidates"][0]["candidate_id"]
    state = submit_review(state, {first})
    assert state["candidates"][0]["jira_result"].startswith("SIM-")
    tail = [(e["step"], e["event"], e.get("result")) for e in read_events(state["run_id"])][-6:]
    assert tail == [("human_review", "review_decision", "approved"), ("approval_guard", "allowed", "ALLOWED"),
                    ("jira_handoff", "created", state["candidates"][0]["jira_result"]),
                    ("human_review", "review_decision", "needs_review"), ("approval_guard", "blocked", "BLOCKED"),
                    ("audit", "run_completed", "completed_with_items_needing_review")]


def test_rejected_item_is_blocked_and_counts_as_resolved():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor)
    first, second = (c["candidate_id"] for c in state["candidates"])
    state = submit_review(state, {first}, reject={second})
    assert state["candidates"][1]["review_status"] == "rejected"
    assert state["candidates"][1]["jira_result"] is None
    assert state["status"] == "completed"


def test_failed_log_write_is_recorded_and_does_not_stop_run():
    Path("not_a_dir").write_text("")
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor, log_dir="not_a_dir")
    assert state["status"] == "awaiting_review"
    assert state["candidates"][0]["validation"] == "PASS"
    assert any(e["step"] == "audit" for e in state["errors"])


def test_guard_blocks_approved_item_with_unresolved_fields():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor)
    flagged = state["candidates"][1]["candidate_id"]  # no owner, no due date
    state = submit_review(state, {flagged})
    assert state["candidates"][1]["jira_result"] is None
    assert state["candidates"][1]["review_status"] == "needs_review"
    blocked = [e for e in state["events"] if e["event"] == "blocked" and e["candidate_id"] == flagged]
    assert blocked[-1]["reason"] == "unresolved owner, due date"


def test_resubmitting_never_creates_a_second_issue():
    state = run_workflow(TRANSCRIPT, "2026-10-08", fake_extractor)
    first = state["candidates"][0]["candidate_id"]
    state = submit_review(state, {first})
    key = state["candidates"][0]["jira_result"]
    state = submit_review(state, set())  # reviewer unticks it and submits again
    assert state["candidates"][0]["jira_result"] == key
    assert state["candidates"][0]["review_status"] == "approved"
    assert sum(e["event"] == "created" for e in state["events"]) == 1
