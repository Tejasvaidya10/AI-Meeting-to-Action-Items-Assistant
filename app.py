"""Streamlit reviewer UI for the Milestone 3 workflow.

  streamlit run app.py

It drives the same Workflow Controller as cli.py: every button sends an event
(transcript.received, review.decision, timer.tick), state lives in SQLite under
data/ui/, and issues are created only in the simulated Jira after an approval.
Each page load calls resume(), as a restarted service would.
"""
from __future__ import annotations

import html
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
PEOPLE = sorted(DIRECTORY)

# Colours match the architecture diagram: people/approval amber, AI blue, code grey, Jira green.
CSS = """
<style>
.block-container {padding-top: 3.6rem; max-width: 1250px;}
.hero {background: linear-gradient(120deg, #F4F7FC 0%, #EEF3FB 100%); border: 1px solid #DCE6F5;
       border-radius: 14px; padding: 18px 22px 16px; margin-bottom: 14px;}
.hero h1 {font-size: 1.65rem; margin: 0 0 2px; color: #1F2D3D; padding: 0;}
.hero p {margin: 0 0 12px; color: #5A6B80; font-size: 0.95rem;}
.flow {display: flex; flex-wrap: wrap; gap: 6px; align-items: center;}
.step {padding: 4px 11px; border-radius: 999px; font-size: 0.8rem; font-weight: 600; border: 1px solid;}
.arrow {color: #9AA7B8; font-size: 0.85rem;}
.s-event {background:#F7F7F7; border-color:#C9C9C9; color:#444;}
.s-ai {background:#DCE8FA; border-color:#5B84C4; color:#24467A;}
.s-code {background:#F7F7F7; border-color:#999; color:#333;}
.s-human {background:#FFF0C2; border-color:#D9A400; color:#7A5B00;}
.s-jira {background:#DCEFDC; border-color:#4C9A5B; color:#245C31;}
.pill {display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 700;
       letter-spacing: .02em; margin-right: 6px; vertical-align: middle;}
.p-pass {background:#E3F4E5; color:#1E6B2E;} .p-flag {background:#FDEBD3; color:#8A4B00;}
.p-pending {background:#E7EEF9; color:#2F5590;} .p-escalated {background:#FDE2E1; color:#9B2C2C;}
.card-title {font-size: 1.12rem; font-weight: 700; color:#1F2D3D; margin: 0 0 8px;}
.grid {display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 10px 0;}
.field {background:#F8FAFD; border:1px solid #E3E9F2; border-radius: 9px; padding: 7px 10px;}
.field .k {font-size: 0.7rem; text-transform: uppercase; letter-spacing: .05em; color:#7A8799;}
.field .v {font-size: 0.9rem; font-weight: 600; color:#1F2D3D; font-family: ui-monospace, Menlo, monospace;
           overflow-wrap: anywhere;}
.field .v.missing {color:#B54708;}
.quote {border-left: 3px solid #5B84C4; background:#F4F7FC; padding: 8px 12px; border-radius: 0 8px 8px 0;
        color:#2B3A4D; font-style: italic; margin-bottom: 10px;}
.conflict {background:#FFF6E5; border:1px solid #F5D08A; color:#7A4E00; border-radius: 8px; padding: 7px 11px;
           font-size: 0.88rem; margin-bottom: 10px;}
.rules {font-size: 0.83rem; color:#4A5A6E; margin: 0; padding-left: 1.1rem;}
.meta {font-size: 0.86rem; color:#4A5A6E; line-height: 1.7; margin-bottom: 4px;}
.side-h {font-size: 0.74rem; text-transform: uppercase; letter-spacing: .08em; color:#7A8799; font-weight: 700;
         margin: 16px 0 2px;}
div[data-testid="stMetric"] {background:#FFFFFF; border:1px solid #E3E9F2; border-radius: 12px; padding: 10px 14px;
                             box-shadow: 0 1px 2px rgba(31,45,61,.05);}
</style>
"""

st.set_page_config(page_title="Meeting-to-Action-Items Assistant", page_icon="✅", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
ss = st.session_state
ss.setdefault("clock_offset_h", 0)
ss.setdefault("messages", [])
if ss.pop("drill_used", False):  # a fault or crash applies to one event only
    ss["fault_choice"], ss["crash"] = "none", False


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
    if outcome in ("run_created", "approved", "rejected", "tick"):
        st.success(text, icon="✅")
    else:
        st.warning(text, icon="⚠️")


def pill(text: str) -> str:
    return f'<span class="pill p-{text.lower()}">{html.escape(text)}</span>'


def field(label: str, value: str | None) -> str:
    shown = html.escape(value) if value else "UNRESOLVED"
    return (f'<div class="field"><div class="k">{label}</div>'
            f'<div class="v{"" if value else " missing"}">{shown}</div></div>')


def side_heading(text: str) -> None:
    st.markdown(f'<div class="side-h">{text}</div>', unsafe_allow_html=True)


# Every page load is a process start: finish or reconcile actions left open.
recovered = workflow().resume()
if recovered:
    st.toast(f"Restart recovery finished {len(recovered)} open action(s): {', '.join(recovered.values())}", icon="🔁")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### ✅ Reviewer console")
    side_heading("Reviewer")
    reviewer = st.selectbox("Signed in as", PEOPLE, index=PEOPLE.index("shubhada"), key="reviewer",
                            help=f"The organizer reviews first; {PROJECT_LEAD} (project lead) may decide after escalation.")
    side_heading("Extraction")
    st.radio("Extractor", ["Rule-based (no API key)", "Hosted model (OpenAI)"], key="extractor",
             disabled=not os.getenv("OPENAI_API_KEY"), help="The hosted option needs OPENAI_API_KEY in .env.",
             label_visibility="collapsed")

    side_heading("Failure drills")
    st.selectbox("Next Jira call", ["none", "unavailable", "lose_confirmation"], key="fault_choice",
                 help="unavailable = 503, nothing created. lose_confirmation = issue created, reply lost.")
    st.checkbox("Stop the process before calling Jira (crash)", key="crash")

    side_heading("Clock")
    now = datetime.now(timezone.utc) + timedelta(hours=ss["clock_offset_h"])
    st.caption(f"Simulated time **+{ss['clock_offset_h']} h** · {now:%a %d %b, %H:%M} UTC")
    hours = st.number_input("Advance by hours", min_value=1, max_value=200, value=25, step=1)
    c1, c2 = st.columns(2)
    if c1.button("Advance", icon="⏩", width="stretch"):
        ss["clock_offset_h"] += int(hours)
        st.rerun()
    if c2.button("Timer tick", key="tick", icon="⏱️", width="stretch"):
        send({"event_id": f"tick-{uuid.uuid4().hex[:10]}", "type": "timer.tick", "payload": {}})
        st.rerun()

    st.divider()
    if st.button("Reset demo data", icon="🗑️", type="secondary", width="stretch"):
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        for key in ("clock_offset_h", "messages", "last_transcript_event"):
            ss.pop(key, None)
        st.rerun()

# ------------------------------------------------------------------ header
st.markdown("""
<div class="hero">
  <h1>Meeting-to-Action-Items Assistant</h1>
  <p>Every Jira issue waits for a named reviewer. State is saved in SQLite, so pending reviews survive a restart.</p>
  <div class="flow">
    <span class="step s-event">Event</span><span class="arrow">→</span>
    <span class="step s-ai">Extract (LLM)</span><span class="arrow">→</span>
    <span class="step s-code">Validate</span><span class="arrow">→</span>
    <span class="step s-human">Human review</span><span class="arrow">→</span>
    <span class="step s-human">Approval Guard</span><span class="arrow">→</span>
    <span class="step s-jira">Jira (simulated)</span>
  </div>
</div>""", unsafe_allow_html=True)

if ss["messages"]:
    show_result(ss["messages"][-1])

wf = workflow()
open_reviews = wf.open_reviews()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Open reviews", len(open_reviews))
m2.metric("Escalated", sum(r["status"] == "escalated" for r in open_reviews))
m3.metric("Issues in Jira", wf.jira.count())
m4.metric("Runs", wf.store.one("SELECT COUNT(*) AS n FROM runs")["n"])

tab_new, tab_queue, tab_jira, tab_runs = st.tabs(["📝 New meeting", f"🧑‍⚖️ Review queue ({len(open_reviews)})",
                                                  f"🎫 Simulated Jira ({wf.jira.count()})", "📜 Runs and audit"])

# ------------------------------------------------------------- new meeting
with tab_new:
    with st.form("new_meeting", border=True):
        c1, c2, c3 = st.columns(3)
        meeting_id = c1.text_input("Meeting ID", value="sprint-2026-10-08")
        meeting_date = c2.date_input("Meeting date", value=date(2026, 10, 8))
        organizer = c3.selectbox("Organizer (reviewer)", PEOPLE, index=PEOPLE.index("shubhada"))
        transcript = st.text_area("Transcript", value=SAMPLE, height=130)
        submitted = st.form_submit_button("Send transcript event", type="primary", icon="📨")
    if submitted:
        event = {"event_id": f"evt-{uuid.uuid4().hex[:10]}", "type": "transcript.received",
                 "payload": {"meeting_id": meeting_id, "meeting_date": meeting_date.isoformat(),
                             "organizer": organizer, "transcript": transcript}}
        ss["last_transcript_event"] = event
        send(event)
        st.rerun()
    if ss.get("last_transcript_event"):
        c1, c2 = st.columns([3, 2], vertical_alignment="center")
        c1.caption(f"Last event sent: `{ss['last_transcript_event']['event_id']}`. "
                   "A webhook may deliver it twice; the inbox ignores the repeat.")
        if c2.button("Deliver the same event again (webhook retry)", key="redeliver", icon="🔁", width="stretch"):
            send(ss["last_transcript_event"])
            st.rerun()

# ------------------------------------------------------------ review queue
with tab_queue:
    if not open_reviews:
        st.info("No open reviews. Send a transcript event from **New meeting**.", icon="📭")
    for review in open_reviews:
        p = review["packet_json"]
        fields = p["proposed_action"]["fields"]
        with st.container(border=True):
            left, right = st.columns([3, 2], gap="large")
            with left:
                st.markdown(
                    f'<div class="card-title">{html.escape(fields["summary"])}</div>'
                    + pill(p["validation"]) + pill(review["status"])
                    + '<div class="grid">' + field("Action", p["proposed_action"]["action"])
                    + field("Assignee", fields["assignee"]) + field("Due date", fields["due_date"]) + "</div>"
                    + f'<div class="quote">“{html.escape(p["evidence"]["quote"])}”</div>'
                    + ('<div class="conflict">⚠️ ' + html.escape("; ".join(p["conflicts"])) + "</div>" if p["conflicts"] else "")
                    + '<ul class="rules">' + "".join(f"<li>{html.escape(r)}</li>" for r in p["rules"]) + "</ul>",
                    unsafe_allow_html=True)
            with right:
                st.markdown(
                    f'<div class="meta">👤 <b>Assigned to</b> {html.escape(review["assigned_to"])} · '
                    f'escalates to {html.escape(review["escalation_to"])}<br>'
                    f'⏰ <b>Deadline</b> {html.escape(review["deadline"])}<br>'
                    f'🔒 <b>Content version</b> <code>{html.escape(p["content_version"])}</code></div>',
                    unsafe_allow_html=True)
                with st.form(f"decide-{review['review_id']}", border=False):
                    c1, c2 = st.columns(2)
                    owner = c1.text_input("Owner", value=fields["assignee"] or "", placeholder="e.g. tejas")
                    due = c2.text_input("Due date", value=fields["due_date"] or "", placeholder="Friday or 2026-10-16")
                    comment = st.text_input("Comment (optional)")
                    b1, b2 = st.columns(2)
                    approve = b1.form_submit_button("Approve", type="primary", icon="✔️", width="stretch")
                    reject = b2.form_submit_button("Reject", icon="✖️", width="stretch")
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
    if not issues:
        st.info("No issues yet. An issue is created only after a reviewer approves it.", icon="🎫")
    else:
        st.dataframe([{k: i[k] for k in ("key", "summary", "assignee", "due_date", "labels")} for i in issues],
                     width="stretch", hide_index=True,
                     column_config={"key": "Issue", "summary": "Summary", "assignee": "Assignee", "due_date": "Due",
                                    "labels": st.column_config.TextColumn("Idempotency label", width="large")})
    st.caption("Counted in the receiver's own database (data/ui/jira_sim.db), not in the workflow's status fields.")

# ----------------------------------------------------------- runs and audit
with tab_runs:
    runs = wf.store.all("SELECT run_id, meeting_id, status, updated_at FROM runs ORDER BY updated_at DESC")
    if not runs:
        st.info("No runs yet.", icon="📜")
    icons = {"completed": "🟢", "completed_with_items_needing_review": "🟠", "awaiting_review": "🔵", "failed": "🔴"}
    for run in runs:
        with st.expander(f"{icons.get(run['status'], '⚪')} {run['run_id']} · {run['meeting_id']} · {run['status']}"):
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
    with st.expander("📥 Event inbox (every event_id and its outcome)"):
        st.dataframe(wf.store.all("SELECT event_id, type, received_at, outcome FROM inbox ORDER BY received_at DESC"),
                     width="stretch", hide_index=True)
