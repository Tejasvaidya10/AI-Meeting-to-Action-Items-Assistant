"""Streamlit reviewer UI for the Milestone 3 workflow.

  streamlit run app.py

It drives the same Workflow Controller as cli.py: every button sends an event
(transcript.received, review.decision, timer.tick), state lives in SQLite under
data/ui/, and issues are created only in the simulated Jira after an approval.
Each page load calls resume(), as a restarted service would.
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from controller import PROJECT_LEAD, SimulatedCrash, Workflow
from jira_sim import SimulatedJira
from meeting_assistant import DIRECTORY, extract_with_openai
from mock_extractor import mock_extractor

load_dotenv()
DATA_DIR = Path(os.getenv("MEETING_APP_DATA", "data/ui"))
SAMPLE = ("Priya will update the API error messages by Friday. Someone should clean up the onboarding notes soon. "
          "Marco will review the release checklist on Monday.")

st.set_page_config(page_title="Meeting-to-Action-Items Assistant", page_icon="✅", layout="wide")
ss = st.session_state
ss.setdefault("clock_offset_h", 0)
if ss.pop("drill_used", False):  # a fault or crash applies to one event only
    ss["fault_choice"], ss["crash"] = "none", False
ss.setdefault("messages", [])


def workflow(fault: str | None = None) -> Workflow:
    """A fresh controller per call, as each request in a real service would get."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    extractor = extract_with_openai if ss.get("extractor") == "Hosted model (OpenAI)" else mock_extractor
    clock = lambda: datetime.now(timezone.utc) + timedelta(hours=ss["clock_offset_h"])  # noqa: E731
    return Workflow(DATA_DIR / "workflow.db", jira=SimulatedJira(DATA_DIR / "jira_sim.db", faults=[fault] if fault else []),
                    log_dir=DATA_DIR / "logs", extractor=extractor, clock=clock)


def send(event: dict, crash: bool = False) -> dict:
    """Deliver one event. A Jira fault chosen in the sidebar applies to this event only."""
    fault = None if ss.get("fault_choice", "none") == "none" else ss["fault_choice"]
    try:
        result = workflow(fault).handle(event, crash_before_send=crash)
    except SimulatedCrash as exc:
        result = {"outcome": "process_stopped", "detail": str(exc)}
    ss["drill_used"] = bool(fault or crash)
    ss["messages"].append(result)
    return result


def show_result(result: dict) -> None:
    outcome = result.get("outcome")
    actions = result.get("actions") or {}
    text = f"**{outcome}**" + (f" — {result['detail']}" if result.get("detail") else "")
    if actions:
        text += "  ·  Jira action: " + ", ".join(f"`{status}`" for status in actions.values())
    good = outcome in ("run_created", "approved", "rejected", "tick")
    (st.success if good else st.warning)(text)


# Every page load is a process start: finish or reconcile actions left open.
recovered = workflow().resume()
if recovered:
    st.toast(f"Restart recovery finished {len(recovered)} open action(s): {', '.join(recovered.values())}")

st.title("Meeting-to-Action-Items Assistant")
st.caption("Event → extract → validate → **human review** → Approval Guard → simulated Jira. "
           "State is saved in SQLite, so pending reviews survive a restart.")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Reviewer")
    reviewer = st.selectbox("Signed in as", sorted(DIRECTORY), index=sorted(DIRECTORY).index("shubhada"), key="reviewer",
                            help=f"Organizer reviews first; {PROJECT_LEAD} (project lead) may decide after escalation.")
    st.header("Extraction")
    st.radio("Extractor", ["Rule-based (no API key)", "Hosted model (OpenAI)"], key="extractor",
             disabled=not os.getenv("OPENAI_API_KEY"), help="The hosted option needs OPENAI_API_KEY in .env.")

    st.header("Failure drills")
    st.selectbox("Next Jira call", ["none", "unavailable", "lose_confirmation"], key="fault_choice",
                 help="unavailable = 503, nothing created. lose_confirmation = issue created, reply lost.")
    st.checkbox("Stop the process before calling Jira (crash)", key="crash")

    st.header("Clock")
    st.write(f"Simulated time: **+{ss['clock_offset_h']} h**")
    hours = st.number_input("Advance by hours", min_value=1, max_value=200, value=25, step=1)
    c1, c2 = st.columns(2)
    if c1.button("Advance", width="stretch"):
        ss["clock_offset_h"] += int(hours)
        st.rerun()
    if c2.button("Timer tick", key="tick", width="stretch"):
        send({"event_id": f"tick-{uuid.uuid4().hex[:10]}", "type": "timer.tick", "payload": {}})
        st.rerun()

    st.divider()
    if st.button("Reset demo data", type="secondary"):
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        for key in ("clock_offset_h", "messages", "last_transcript_event"):
            ss.pop(key, None)
        st.rerun()

if ss["messages"]:
    show_result(ss["messages"][-1])

wf = workflow()
tab_new, tab_queue, tab_jira, tab_runs = st.tabs(["New meeting", f"Review queue ({len(wf.open_reviews())})",
                                                  f"Simulated Jira ({wf.jira.count()})", "Runs and audit"])

# ------------------------------------------------------------- new meeting
with tab_new:
    with st.form("new_meeting"):
        c1, c2, c3 = st.columns(3)
        meeting_id = c1.text_input("Meeting ID", value="sprint-2026-10-08")
        meeting_date = c2.date_input("Meeting date", value=date(2026, 10, 8))
        organizer = c3.selectbox("Organizer (reviewer)", sorted(DIRECTORY), index=sorted(DIRECTORY).index("shubhada"))
        transcript = st.text_area("Transcript", value=SAMPLE, height=140)
        submitted = st.form_submit_button("Send transcript event", type="primary")
    if submitted:
        event = {"event_id": f"evt-{uuid.uuid4().hex[:10]}", "type": "transcript.received",
                 "payload": {"meeting_id": meeting_id, "meeting_date": meeting_date.isoformat(),
                             "organizer": organizer, "transcript": transcript}}
        ss["last_transcript_event"] = event
        send(event)
        st.rerun()
    if ss.get("last_transcript_event"):
        st.caption(f"Last event: `{ss['last_transcript_event']['event_id']}`")
        if st.button("Deliver the same event again (webhook retry)", key="redeliver"):
            send(ss["last_transcript_event"])
            st.rerun()

# ------------------------------------------------------------ review queue
with tab_queue:
    reviews = wf.open_reviews()
    if not reviews:
        st.info("No open reviews. Send a transcript event first.")
    for review in reviews:
        p = review["packet_json"]
        fields = p["proposed_action"]["fields"]
        with st.container(border=True):
            badge = "🟢 PASS" if p["validation"] == "PASS" else "🟠 FLAG"
            st.markdown(f"#### {fields['summary']}  ·  {badge}  ·  `{review['status']}`")
            left, right = st.columns([3, 2])
            with left:
                st.markdown(f"**Proposed action:** `{p['proposed_action']['action']}` in project "
                            f"`{p['proposed_action']['target']['project']}`")
                st.markdown(f"**Assignee:** `{fields['assignee'] or 'UNRESOLVED'}`  ·  "
                            f"**Due date:** `{fields['due_date'] or 'UNRESOLVED'}`")
                st.markdown(f"**Evidence:** “{p['evidence']['quote']}”")
                if p["conflicts"]:
                    st.warning("Conflicts: " + "; ".join(p["conflicts"]))
                st.markdown("**Rules:**\n" + "\n".join(f"- {rule}" for rule in p["rules"]))
            with right:
                st.markdown(f"**Assigned to:** {review['assigned_to']}  \n**Escalates to:** {review['escalation_to']}  \n"
                            f"**Deadline:** {review['deadline']}  \n**Content version:** `{p['content_version']}`")
                with st.form(f"decide-{review['review_id']}"):
                    owner = st.text_input("Owner", value=fields["assignee"] or "")
                    due = st.text_input("Due date (e.g. Friday or 2026-10-16)", value=fields["due_date"] or "")
                    comment = st.text_input("Comment (optional)")
                    b1, b2 = st.columns(2)
                    approve = b1.form_submit_button("Approve", type="primary", width="stretch")
                    reject = b2.form_submit_button("Reject", width="stretch")
                if approve or reject:
                    edits = {k: v.strip() for k, v, old in (("owner", owner, fields["assignee"]), ("due_date", due, fields["due_date"]))
                             if v.strip() and v.strip() != (old or "")}
                    payload = {"review_id": review["review_id"], "decision": "approve" if approve else "reject",
                               "reviewer": reviewer, "content_version": p["content_version"], "comment": comment or None}
                    if edits and approve:
                        payload["edits"] = edits
                    send({"event_id": f"evt-{uuid.uuid4().hex[:10]}", "type": "review.decision", "payload": payload},
                         crash=ss.get("crash", False) and approve)
                    st.rerun()

# ------------------------------------------------------------ simulated jira
with tab_jira:
    issues = wf.jira.issues()
    st.metric("Issues in the simulated Jira database", len(issues))
    if issues:
        st.dataframe([{k: i[k] for k in ("key", "summary", "assignee", "due_date", "labels")} for i in issues],
                     width="stretch", hide_index=True)
    st.caption("Counted in the receiver's own database (data/ui/jira_sim.db), not in the workflow's status fields.")

# ----------------------------------------------------------- runs and audit
with tab_runs:
    runs = wf.store.all("SELECT run_id, meeting_id, status, updated_at FROM runs ORDER BY updated_at DESC")
    if not runs:
        st.info("No runs yet.")
    for run in runs:
        with st.expander(f"{run['run_id']} · {run['meeting_id']} · {run['status']}"):
            st.markdown("**Reviews**")
            st.dataframe(wf.store.all("SELECT review_id, status, assigned_to, deadline FROM reviews WHERE run_id = ?",
                                      run["run_id"]), width="stretch", hide_index=True)
            actions = wf.store.all("SELECT idempotency_key, status, attempts, jira_key, last_error FROM actions "
                                   "WHERE run_id = ?", run["run_id"])
            if actions:
                st.markdown("**Outbox (external actions)**")
                st.dataframe(actions, width="stretch", hide_index=True)
            events = wf.store.one("SELECT state_json FROM runs WHERE run_id = ?", run["run_id"])["state_json"]["events"]
            st.markdown("**Audit trail**")
            st.dataframe([{k: e.get(k) for k in ("timestamp", "step", "event", "candidate_id", "result", "reason", "error")}
                          for e in events], width="stretch", hide_index=True)
    with st.expander("Event inbox (every event_id and its outcome)"):
        st.dataframe(wf.store.all("SELECT event_id, type, received_at, outcome FROM inbox ORDER BY received_at DESC"),
                     width="stretch", hide_index=True)
