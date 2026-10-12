from datetime import date

from meeting_assistant import Extraction, ActionItem, run_workflow, validate_candidates


def fake_extractor(_: str) -> Extraction:
    return Extraction(summary="Sprint follow-up", decisions=[], tasks=[
        ActionItem(title="Update API", owner_text="Priya", due_date_text="Friday", evidence="Priya will update API by Friday."),
        ActionItem(title="Clean notes", owner_text=None, due_date_text=None, evidence="Someone should clean notes."),
    ])


def test_validation_pass_and_flag():
    results = validate_candidates(fake_extractor("") , date(2026, 10, 8))
    assert results[0].validation == "PASS"
    assert results[0].due_date == "2026-10-09"
    assert results[1].validation == "FLAG"


def test_empty_input_stops_before_model():
    state = run_workflow("", "2026-10-08", lambda _: (_ for _ in ()).throw(AssertionError("model called")))
    assert state["status"] == "failed"
    assert "empty" in state["error"]


def test_approval_guard_blocks_unapproved_candidate():
    state = run_workflow("text", "2026-10-08", fake_extractor, approve=set())
    assert all(c["jira_result"] is None for c in state["candidates"])
    assert state["status"] == "completed_with_items_needing_review"
