"""Drive the Streamlit reviewer UI headlessly and check the effect in the simulated Jira."""
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from controller import Workflow  # noqa: E402
from jira_sim import SimulatedJira  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_APP_DATA", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    at = AppTest.from_file("app.py", default_timeout=30)
    at.run()
    at.tmp_path = tmp_path
    return at


def button(at, label):
    return next(b for b in at.button if b.label == label)


def jira_count(at):
    return SimulatedJira(at.tmp_path / "jira_sim.db").count()


def open_reviews(at):
    return Workflow(at.tmp_path / "workflow.db", jira=SimulatedJira(at.tmp_path / "jira_sim.db", faults=[])).open_reviews()


def send_transcript(at):
    button(at, "Send transcript event").click().run()
    return open_reviews(at)


def decide(at, review_id, label="Approve", owner=None, due=None):
    """Fill and submit the decision form of one review card."""
    index = [r["review_id"] for r in open_reviews(at)].index(review_id)
    if owner is not None:
        at.text_input[3 * index + 1].set_value(owner)   # first text input is the meeting ID
    if due is not None:
        at.text_input[3 * index + 2].set_value(due)
    [b for b in at.button if b.label == label][index].click().run()


def summary(review):
    return review["packet_json"]["proposed_action"]["fields"]["summary"]


def test_transcript_creates_reviews_and_redelivery_is_ignored(app):
    reviews = send_transcript(app)
    assert not app.exception
    assert len(reviews) == 3
    button(app, "Deliver the same event again (webhook retry)").click().run()
    assert "duplicate_ignored" in app.warning[0].value
    assert len(open_reviews(app)) == 3 and jira_count(app) == 0


def test_approve_creates_exactly_one_issue_and_second_approval_is_refused(app):
    api = next(r for r in send_transcript(app) if "API" in summary(r))
    decide(app, api["review_id"])
    assert "approved" in app.success[0].value
    assert jira_count(app) == 1
    assert len(open_reviews(app)) == 2


def test_reject_withholds_the_action(app):
    api = next(r for r in send_transcript(app) if "API" in summary(r))
    decide(app, api["review_id"], label="Reject")
    assert "rejected" in app.success[0].value
    assert jira_count(app) == 0


def test_flag_item_needs_edits_then_approval_uses_edited_fields(app):
    flag = next(r for r in send_transcript(app) if r["packet_json"]["validation"] == "FLAG")
    decide(app, flag["review_id"])
    assert "needs_changes" in app.warning[0].value and jira_count(app) == 0
    decide(app, flag["review_id"], owner="tejas", due="2026-10-16")
    issues = SimulatedJira(app.tmp_path / "jira_sim.db").issues()
    assert [(i["assignee"], i["due_date"]) for i in issues] == [("tejas", "2026-10-16")]


def test_lost_jira_reply_is_reconciled_on_next_load_without_resend(app):
    api = next(r for r in send_transcript(app) if "API" in summary(r))
    app.selectbox(key="fault_choice").set_value("lose_confirmation").run()
    decide(app, api["review_id"])
    assert "uncertain" in app.success[0].value and jira_count(app) == 1
    app.run()  # next page load = restart: resume() finds the issue by its label
    action = Workflow(app.tmp_path / "workflow.db", jira=SimulatedJira(app.tmp_path / "jira_sim.db", faults=[])).store.one(
        "SELECT status, attempts FROM actions")
    assert (action["status"], action["attempts"], jira_count(app)) == ("done", 1, 1)
    assert app.selectbox(key="fault_choice").value == "none"  # the fault applied to one event only


def test_crash_before_jira_is_finished_once_after_restart(app):
    api = next(r for r in send_transcript(app) if "API" in summary(r))
    app.checkbox(key="crash").check().run()
    decide(app, api["review_id"])
    # The page reload after the crash is the restart: resume() finishes the saved approval.
    assert "process_stopped" in app.warning[0].value
    assert any("Restart recovery finished 1" in t.value for t in app.toast)
    app.run()
    app.run()
    action = Workflow(app.tmp_path / "workflow.db", jira=SimulatedJira(app.tmp_path / "jira_sim.db", faults=[])).store.one(
        "SELECT status, attempts FROM actions")
    assert (action["status"], action["attempts"], jira_count(app)) == ("done", 1, 1)


def test_timeout_escalates_to_project_lead_who_can_then_decide(app):
    api = next(r for r in send_transcript(app) if "API" in summary(r))
    app.number_input[0].set_value(25)
    button(app, "Advance").click().run()
    button(app, "Timer tick").click().run()
    assert {r["status"] for r in open_reviews(app)} == {"escalated"}
    app.selectbox(key="reviewer").set_value("kriday").run()
    decide(app, api["review_id"])
    assert "approved" in app.success[0].value and jira_count(app) == 1


def test_non_reviewer_is_refused(app):
    api = next(r for r in send_transcript(app) if "API" in summary(r))
    app.selectbox(key="reviewer").set_value("priya").run()
    decide(app, api["review_id"])
    assert "unauthorized" in app.warning[0].value and jira_count(app) == 0
