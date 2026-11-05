"""Streamlit reviewer UI for the Milestone 3 workflow.

  streamlit run app.py

It drives the same Workflow Controller as cli.py: every button sends an event
(transcript.received, review.decision, timer.tick), state lives in SQLite under
data/ui/, and issues are created only in the simulated Jira after an approval.
Each page load calls resume(), as a restarted service would.
"""
from __future__ import annotations

import html
import json
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

# Slate + indigo palette (Tailwind slate/indigo/emerald/amber/red scales).
DOTS = ("url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18'%3E"
        "%3Ccircle cx='1.5' cy='1.5' r='1.2' fill='%236366F1'/%3E%3C/svg%3E\") 0 0/18px 18px")
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root {--ink:#0F172A; --muted:#64748B; --line:#E2E8F0; --card:#FFFFFF; --bg:#F8FAFC; --subtle:#F1F5F9;
       --primary:#4F46E5; --primary-dark:#4338CA; --primary-soft:#EEF2FF; --primary-line:#C7D2FE;
       --success:#047857; --success-soft:#D1FAE5; --warn:#92400E; --warn-soft:#FEF3C7; --warn-line:#FCD34D;
       --danger:#B91C1C; --danger-soft:#FEE2E2;}
html, body, [class*="css"], .stMarkdown, .stTextInput, .stSelectbox, button {font-family: Inter, -apple-system, sans-serif;}
.block-container {padding-top: 2.2rem; max-width: 1240px;}
h1, h2, h3 {font-family: Inter, -apple-system, sans-serif !important; font-weight: 700 !important; letter-spacing: -.025em;}

/* sidebar */
[data-testid="stSidebar"] {background: var(--card); border-right: 1px solid var(--line);}
.brand {display:flex; align-items:center; gap:10px; margin: 2px 0 6px;}
.brand-mark {display:flex; align-items:flex-end; gap:3px; height:24px;}
.brand-mark i {display:block; width:4px; border-radius:3px; background: var(--primary);}
.brand-mark i:nth-child(1){height:12px; opacity:.55} .brand-mark i:nth-child(2){height:24px} .brand-mark i:nth-child(3){height:18px; opacity:.8} .brand-mark i:nth-child(4){height:9px; opacity:.4}
.brand-name {font-size: 1.15rem; font-weight: 800; color: var(--ink); letter-spacing: -.03em;}
.brand-sub {font: 600 9px ui-monospace, Menlo, monospace; letter-spacing: 1.6px; color: var(--muted); margin-bottom: 4px;}
.side-h {font-size: .68rem; font-weight: 700; letter-spacing: .09em; color: var(--muted); text-transform: uppercase; margin: 18px 0 2px;}

/* overview card */
.overview {background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 18px 22px 14px; margin: 4px 0 14px;
           box-shadow: 0 1px 2px rgba(15,23,42,.04);}
.ov-top {display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 12px; margin-bottom: 14px;}
.ov-top > div {border-left: 2px solid var(--line); padding-left: 12px;}
.ov-top > div:first-child {border-left-color: var(--primary);}
.ov-n {font-size: 1.9rem; font-weight: 800; letter-spacing: -.03em; line-height: 1.05; color: var(--ink);}
.ov-n.warn {color: var(--danger);}
.ov-k {font-size: .76rem; color: var(--muted); margin-top: 2px;}
.ov-cell {display:flex; align-items:center; justify-content:space-between; gap: 10px; padding-right: 14px;}
.ov-tile {width: 44px; height: 44px; border-radius: 12px; display:grid; place-items:center; flex-shrink: 0;}
.ov-tile .ic {margin: 0; color: currentColor;}
.c-primary {background: var(--primary-soft); color: var(--primary);}
.c-danger {background: var(--danger-soft); color: #DC2626;}
.c-success {background: var(--success-soft); color: #059669;}
.c-violet {background: #F3E8FF; color: #7C3AED;}
.bar {display:flex; height: 8px; border-radius: 999px; overflow: hidden; background: var(--subtle); gap: 2px;}
.seg {display:block; min-width: 6px; background: var(--subtle);}
.legend {display:flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; font-size: .78rem; color: var(--muted);}
.legend b {color: var(--ink); font-weight: 600; margin-left: 2px;}
.legend .muted {margin-left: auto;}
.dot {display:inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; vertical-align: 0;}
.s-pending {background: #A5B4FC;} .s-escalated {background: #F87171;} .s-approved {background: #34D399;}
.s-rejected {background: #94A3B8;} .s-expired {background: #CBD5E1;}
.tag {font-size: .64rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; margin-right: 10px; opacity: .85;}
.notice {display:flex; align-items:baseline; border: 1px solid var(--warn-line); border-left: 4px solid #F59E0B; background: #FFFBEB;
         color: var(--warn); border-radius: 12px; padding: 11px 15px; margin-bottom: 14px; font-size: .9rem;}

/* plain icons */
.ic {display:inline-block; vertical-align: -2px; margin-right: 6px; flex-shrink: 0; color: #64748B;}
.notice .ic, .conflict .ic {color: currentColor; margin-right: 8px; vertical-align: -3px;}
.meta dt {display:flex; align-items:center;}
.stTabs [data-baseweb="tab"] [data-testid="stIconMaterial"] {color: #94A3B8;}
.stTabs [aria-selected="true"] [data-testid="stIconMaterial"] {color: var(--primary);}

/* white table card */
.stApp, [data-testid="stAppViewContainer"] {background: var(--bg);}
[data-testid="stHeader"] {background: transparent;}
.table-card {background: var(--card); border: 1px solid var(--line); border-radius: 18px; overflow: hidden; margin-top: 4px;
             box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 10px 26px rgba(15,23,42,.05);}
.tc-head {display:flex; align-items:center; justify-content:space-between; padding: 14px 18px; border-bottom: 1px solid var(--line);
          font-weight: 700; color: var(--ink);}
.tc-head .count {font-size: .75rem; font-weight: 700; color: var(--primary); background: var(--primary-soft); border-radius: 999px; padding: 2px 10px;}
table.issues {width: 100%; border-collapse: collapse; font-size: .9rem;}
table.issues th {text-align: left; font-size: .68rem; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; color: var(--muted);
                 background: var(--bg); padding: 10px 18px; border-bottom: 1px solid var(--line);}
table.issues td {padding: 12px 18px; border-bottom: 1px solid var(--subtle); color: var(--ink); vertical-align: middle;}
table.issues tr:last-child td {border-bottom: 0;}
table.issues th, table.issues td {border-left: 0 !important; border-right: 0 !important; border-top: 0 !important;}
table.issues tr:hover td {background: #FAFBFF;}
table.issues .key {font-weight: 700; color: var(--primary);}
table.issues code {background: var(--subtle); color: #475569; border-radius: 6px; padding: 2px 7px; font-size: .78rem;}
[data-testid="stDataFrame"] {background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 2px;}
.table-card {margin-bottom: 12px;}
[data-testid="stSidebar"] [data-baseweb="select"] > div, [data-testid="stSidebar"] [data-baseweb="input"],
[data-testid="stSidebar"] [data-baseweb="input"] input, [data-testid="stSidebar"] [data-testid="stNumberInputContainer"] button
    {background: var(--subtle) !important;}

/* nav bar */
.nav {display:flex; align-items:center; justify-content:space-between; gap: 16px; height: 58px; padding: 0 18px; margin-bottom: 18px;
      background: rgba(255,255,255,.82); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
      border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 1px 2px rgba(15,23,42,.04);}
.nav-l, .nav-r {display:flex; align-items:center; gap: 12px;}
.nav-brand {font-weight: 800; font-size: 1.02rem; color: var(--ink); letter-spacing: -.02em;}
.nav-sep {width: 1px; height: 20px; background: var(--line);}
.nav-team {font-size: .82rem; color: var(--muted);}
.nav-link {font-size: .84rem; font-weight: 600; color: #334155 !important; text-decoration: none !important; padding: 6px 10px; border-radius: 8px;}
.nav-link:hover {background: var(--subtle); color: var(--primary) !important;}
.avatar {width: 32px; height: 32px; border-radius: 50%; display:grid; place-items:center; font-size: .72rem; font-weight: 700;
         color: #fff; background: linear-gradient(135deg, #6366F1, #8B5CF6);}
.nav-user {display:flex; flex-direction:column; line-height: 1.15; font-size: .82rem; color: var(--ink);}
.nav-user small {font-size: .7rem; color: var(--muted);}
.side-title {font-size: 1rem; font-weight: 800; color: var(--ink); letter-spacing: -.02em; margin: 2px 0 2px;}
/* faded dot grid behind the top of the page */
[data-testid="stAppViewContainer"]::before {content:''; position: absolute; inset: 0 0 auto 0; height: 560px; pointer-events: none;
    background: DOTS; opacity: .22; z-index: 0;
    -webkit-mask-image: linear-gradient(180deg, #000 0%, rgba(0,0,0,.5) 45%, transparent 100%);
    mask-image: linear-gradient(180deg, #000 0%, rgba(0,0,0,.5) 45%, transparent 100%);}
[data-testid="stMain"] {position: relative; z-index: 1;}

/* console header */
.console {position: relative; isolation: isolate; overflow: hidden; border-radius: 16px; padding: 20px 24px 14px; margin-bottom: 14px;
          background: radial-gradient(circle at 95% 0%, #E0E7FF 0, rgba(224,231,255,0) 48%), var(--card);
          border: 1px solid var(--line); box-shadow: 0 1px 2px rgba(15,23,42,.04);}
.console:before {content:''; position:absolute; inset:0; z-index:-1; background: DOTS; opacity:.22;
                 -webkit-mask-image: linear-gradient(270deg, #000, transparent 60%); mask-image: linear-gradient(270deg, #000, transparent 60%);}
.console-top {display:flex; justify-content:space-between; align-items:flex-start; gap: 16px;}
.crumb {font-size: .75rem; color: var(--muted); font-weight: 500;} .crumb span {margin: 0 6px; color: #CBD5E1;}
.console-title {font-size: 1.55rem !important; margin: 0 0 4px !important; padding: 0 !important; color: var(--ink);}
.console-sub {font-size: .88rem; color: var(--muted); max-width: 640px;}
.chips {display:flex; gap: 8px; flex-wrap: wrap; justify-content:flex-end;}
.chip {font-size: .76rem; color: #334155; background: var(--bg); border: 1px solid var(--line); border-radius: 999px; padding: 5px 11px;
       white-space: nowrap;}
.chip b {color: var(--ink);}
.live {display:inline-block; width: 7px; height: 7px; border-radius: 50%; background: #10B981; margin-right: 6px; box-shadow: 0 0 0 3px #D1FAE5;}
.steps {display:flex; align-items:center; gap: 8px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--subtle); flex-wrap: wrap;}
.stp {font-size: .76rem; color: var(--muted); font-weight: 500; display:flex; align-items:center; gap: 6px;}
.stp b {display:grid; place-items:center; width: 18px; height: 18px; border-radius: 50%; font-size: .66rem; background: var(--subtle); color: #475569;}
.stp.human {color: var(--primary-dark);} .stp.human b {background: var(--primary); color: #fff;}
.stp.jira {color: var(--success);} .stp.jira b {background: #10B981; color: #fff;}
.sep {flex: 0 0 18px; height: 1px; background: var(--line);}
.policy {border-top: 1px solid var(--subtle); padding-top: 10px; margin-top: 2px;}
.policy-h {font-size: .66rem; font-weight: 700; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px;}
.rule {display:flex; gap: 8px; align-items:flex-start; font-size: .82rem; color: #475569; margin-bottom: 4px;}
.rid {flex-shrink:0; font: 700 .66rem ui-monospace, Menlo, monospace; color: var(--primary); background: var(--primary-soft);
      border-radius: 5px; padding: 1px 5px; margin-top: 1px;}
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], footer {display: none !important;}
[data-testid="stAlertContainer"] {padding: 10px 14px !important; font-size: .9rem;}
[data-testid="stAlert"] code {font-size: .72rem; opacity: .7; background: rgba(255,255,255,.6);}

/* segmented tabs */
.stTabs [data-baseweb="tab-list"] {background: var(--subtle); border: 1px solid var(--line); border-radius: 14px; padding: 5px; gap: 4px;}
.stTabs [data-baseweb="tab"] {border-radius: 10px; padding: 9px 16px; height: auto; color: var(--muted); font-weight: 500;}
.stTabs [aria-selected="true"] {background: var(--card); color: var(--primary); box-shadow: 0 1px 3px rgba(15,23,42,.1);}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {display: none;}

/* review cards */
[class*="st-key-card-"] {background: var(--card); border-radius: 18px !important; border-color: var(--line) !important;
                         box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 10px 26px rgba(15,23,42,.05); padding: 20px 22px 14px !important;}
.card-title {font-size: 1.22rem; font-weight: 700; color: var(--ink); margin: 0 0 8px; letter-spacing: -.02em;}
.pill {display:inline-block; padding: 3px 10px; border-radius: 999px; font-size: .68rem; font-weight: 700;
       letter-spacing: .06em; margin-right: 6px; text-transform: uppercase;}
.p-pass {background: var(--success-soft); color: var(--success);} .p-flag {background: var(--warn-soft); color: var(--warn);}
.p-pending {background: var(--subtle); color: #475569;} .p-escalated {background: var(--danger-soft); color: var(--danger);}
.grid {display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 8px; margin: 12px 0;}
.field {background: var(--bg); border: 1px solid var(--line); border-radius: 11px; padding: 8px 11px;}
.field .k {font-size: .66rem; font-weight: 700; letter-spacing: .07em; color: var(--muted); text-transform: uppercase;}
.field .v {font-size: .93rem; font-weight: 600; color: var(--ink); overflow-wrap: anywhere;}
.field .v.missing {color: var(--danger);}
.quote {font-style: italic; font-size: 1rem; color: #334155; border-left: 3px solid var(--primary);
        background: linear-gradient(90deg, var(--primary-soft), rgba(238,242,255,0) 70%); padding: 8px 12px; border-radius: 0 10px 10px 0; margin: 4px 0 12px;}
.conflict {background: var(--warn-soft); border: 1px solid var(--warn-line); border-left: 4px solid #F59E0B; color: var(--warn); border-radius: 10px; padding: 8px 12px;
           font-size: .86rem; margin-bottom: 10px;}
.rules {font-size: .82rem; color: var(--muted); margin: 0; padding-left: 1.1rem;}
.meta {display:grid; grid-template-columns: auto 1fr; gap: 4px 14px; margin: 0 0 10px; padding: 10px 14px; font-size: .84rem;
       background: var(--bg); border: 1px solid var(--line); border-radius: 12px;}
.meta dt {color: var(--muted); font-weight: 500;} .meta dd {margin: 0; color: var(--ink); font-weight: 600; overflow-wrap: anywhere;}
.meta code {background: transparent; color: var(--primary); font-size: .8rem; padding: 0;}

/* controls */
.stButton > button, .stFormSubmitButton > button {border-radius: 10px; font-weight: 600; min-height: 42px;}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primaryFormSubmit"] {background: var(--primary); border-color: var(--primary);
    box-shadow: 0 1px 2px rgba(79,70,229,.25);}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primaryFormSubmit"]:hover {background: var(--primary-dark); border-color: var(--primary-dark);}
.stButton > button[kind="secondary"], .stFormSubmitButton > button[kind="secondaryFormSubmit"] {background: var(--card); border-color: var(--line); color: #334155;}
.stButton > button[kind="secondary"]:hover, .stFormSubmitButton > button[kind="secondaryFormSubmit"]:hover {border-color: var(--primary-line); color: var(--primary);}
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] {border-radius: 10px !important;}
[data-testid="stForm"] {border-radius: 18px; border-color: var(--line); background: var(--card);}
[data-testid="stAlert"] {border-radius: 14px;}
[data-testid="stExpander"] details {border-radius: 14px; background: var(--card); border-color: var(--line);}
:focus-visible {outline: 2px solid var(--primary) !important; outline-offset: 3px;}
</style>
""".replace("DOTS", DOTS)


# Plain line icons (Lucide paths, 24px grid), drawn in the surrounding text colour.
ICONS = {
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "ticket": '<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2M13 17v2M13 11v2"/>',
    "history": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/>',
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "arrow-up": '<path d="M7 17 17 7M7 7h10v10"/>',
    "calendar": '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4M12 17h.01"/>',
}


def icon(name: str, size: int = 14) -> str:
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


st.set_page_config(page_title="Meeting-to-Action-Items Assistant", page_icon=":material/task_alt:", layout="wide")
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


MESSAGES = {
    "run_created": "Transcript received. {reviews} candidate task(s) are waiting for review.",
    "approved": "Approved. The Jira action is {action}.",
    "rejected": "Rejected. No Jira issue will be created for this item.",
    "tick": "Timer processed: {escalated} review(s) escalated, {expired} expired.",
    "duplicate_ignored": "Duplicate event ignored. This event ID was already processed, so nothing changed.",
    "duplicate_content": "This meeting was already processed under another event ID. No new run was created.",
    "needs_changes": "Needs changes. Set an owner and a due date before approving.",
    "unauthorized": "Not authorized. Only the assigned reviewer, or the project lead after escalation, can decide.",
    "stale_version": "The item changed after you opened it. Review the current version and decide again.",
    "already_decided": "This review was already decided. Nothing changed.",
    "late_ignored": "The review window has closed. The item was withheld and nothing was sent.",
    "process_stopped": "Process stopped before calling Jira (crash drill). The approval is saved and is completed on restart.",
    "malformed": "The event is missing required fields and was ignored.",
    "unsupported_type": "Unknown event type. It was recorded and ignored.",
    "run_failed": "Extraction failed. The run was stopped before review.",
}


def show_result(result: dict) -> None:
    outcome = result.get("outcome")
    actions = result.get("actions") or {}
    text = MESSAGES.get(outcome, "{outcome}").format(
        outcome=outcome, reviews=len(result.get("reviews") or []), action=", ".join(actions.values()) or "queued",
        escalated=len(result.get("escalated") or []), expired=len(result.get("expired") or []))
    text += f"  `{outcome}`"
    if outcome in ("run_created", "approved", "rejected", "tick"):
        st.success(text)
    else:
        st.warning(text)


def pill(text: str) -> str:
    return f'<span class="pill p-{text.lower()}">{html.escape(text)}</span>'


def field(label: str, value: str | None) -> str:
    shown = html.escape(value) if value else "Unresolved"
    return (f'<div class="field"><div class="k">{label}</div>'
            f'<div class="v{"" if value else " missing"}">{shown}</div></div>')


def overview(counts: dict[str, int], jira: int, runs: int) -> str:
    """One card: headline numbers and a stacked bar of every review by status."""
    order = [("pending", "Pending"), ("escalated", "Escalated"), ("approved", "Approved"),
             ("rejected", "Rejected"), ("expired", "Expired")]
    total = sum(counts.values())
    bar = "".join(f'<span class="seg s-{k}" style="flex:{counts.get(k, 0)}"></span>' for k, _ in order if counts.get(k))
    legend = "".join(f'<span class="lg"><i class="dot s-{k}"></i>{label} <b>{counts.get(k, 0)}</b></span>' for k, label in order)
    open_n = counts.get("pending", 0) + counts.get("escalated", 0)
    def cell(value: int, label: str, icon_name: str, tone: str, warn: bool = False) -> str:
        return (f'<div class="ov-cell"><div><div class="ov-n{" warn" if warn else ""}">{value}</div>'
                f'<div class="ov-k">{label}</div></div><div class="ov-tile c-{tone}">{icon(icon_name, 20)}</div></div>')
    return (f'<div class="overview"><div class="ov-top">'
            + cell(open_n, "open reviews", "inbox", "primary")
            + cell(counts.get("escalated", 0), "escalated", "clock", "danger", warn=bool(counts.get("escalated")))
            + cell(jira, "issues in Jira", "ticket", "success")
            + cell(runs, "runs", "history", "violet") + "</div>"
            f'<div class="bar">{bar or "<span class=seg style=flex:1></span>"}</div>'
            f'<div class="legend">{legend}<span class="lg muted">{total} reviews in total</span></div></div>')


def side_heading(text: str) -> None:
    st.markdown(f'<div class="side-h">{text}</div>', unsafe_allow_html=True)


# Every page load is a process start: finish or reconcile actions left open.
recovered = workflow().resume()
if recovered:
    st.toast(f"Restart recovery finished {len(recovered)} open action(s): {', '.join(recovered.values())}", icon=":material/sync:")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown('<div class="side-title">Controls</div>', unsafe_allow_html=True)
    side_heading("Reviewer")
    reviewer = st.selectbox("Signed in as", PEOPLE, index=PEOPLE.index("shubhada"), key="reviewer",
                            help=f"The organizer reviews first; {PROJECT_LEAD} (project lead) may decide after escalation.")
    side_heading("Extraction")
    st.radio("Extractor", ["Rule-based (no API key)", "Hosted model (OpenAI)"], key="extractor",
             disabled=not os.getenv("OPENAI_API_KEY"), help="The hosted option needs OPENAI_API_KEY in .env.",
             label_visibility="collapsed")

    side_heading("Simulation")
    st.caption("Demo controls: inject faults and move the clock.")
    st.selectbox("Next Jira call", ["none", "unavailable", "lose_confirmation"], key="fault_choice",
                 help="unavailable = 503, nothing created. lose_confirmation = issue created, reply lost.")
    st.checkbox("Stop the process before calling Jira (crash)", key="crash")

    now = datetime.now(timezone.utc) + timedelta(hours=ss["clock_offset_h"])
    st.caption(f"Simulated time **+{ss['clock_offset_h']} h** · {now:%a %d %b, %H:%M} UTC")
    hours = st.number_input("Advance by hours", min_value=1, max_value=200, value=25, step=1)
    c1, c2 = st.columns(2)
    if c1.button("Advance", icon=":material/fast_forward:", width="stretch"):
        ss["clock_offset_h"] += int(hours)
        st.rerun()
    if c2.button("Timer tick", key="tick", icon=":material/timer:", width="stretch"):
        send({"event_id": f"tick-{uuid.uuid4().hex[:10]}", "type": "timer.tick", "payload": {}})
        st.rerun()

    st.divider()
    if st.button("Reset demo data", icon=":material/restart_alt:", type="secondary", width="stretch"):
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        for key in ("clock_offset_h", "messages", "last_transcript_event"):
            ss.pop(key, None)
        st.rerun()

# ------------------------------------------------------------------ header
REPO = "https://github.com/Tejasvaidya10/AI-Meeting-to-Action-Items-Assistant/tree/shubhada/m3-hitl-events"
role = "Project lead" if reviewer == PROJECT_LEAD else "Reviewer"
st.markdown(
    '<div class="nav"><div class="nav-l"><div class="brand-mark"><i></i><i></i><i></i><i></i></div>'
    '<span class="nav-brand">Action Items</span><span class="nav-sep"></span><span class="nav-team">Team 8 · Milestone 3</span></div>'
    f'<div class="nav-r"><a class="nav-link" href="{REPO}" target="_blank">Repository</a>'
    '<span class="chip"><i class="live"></i>Jira: simulated</span>'
    f'<span class="avatar">{html.escape(reviewer[:2].upper())}</span>'
    f'<span class="nav-user"><b>{html.escape(reviewer)}</b><small>{role}</small></span></div></div>',
    unsafe_allow_html=True)

steps = [("Event", ""), ("Extract", ""), ("Validate", ""), ("Human review", "human"), ("Approval Guard", "human"),
         ("Jira", "jira")]
st.markdown(
    '<div class="console"><h1 class="console-title">Review console</h1>'
    '<div class="console-sub">Approve, edit or reject each proposed Jira issue. Nothing is created without an approval, '
    'and decisions survive restarts.</div>'
    '<div class="steps">' + '<span class="sep"></span>'.join(
        f'<span class="stp {kind}"><b>{n}</b>{label}</span>' for n, (label, kind) in enumerate(steps, 1)) + '</div></div>',
    unsafe_allow_html=True)

if ss["messages"]:
    show_result(ss["messages"][-1])

wf = workflow()
open_reviews = wf.open_reviews()
escalated = [r for r in open_reviews if r["status"] == "escalated"]
counts = {r["status"]: r["n"] for r in wf.store.all("SELECT status, COUNT(*) AS n FROM reviews GROUP BY status")}
st.markdown(overview(counts, wf.jira.count(), wf.store.one("SELECT COUNT(*) AS n FROM runs")["n"]), unsafe_allow_html=True)
if escalated:
    st.markdown(f'<div class="notice">{icon("clock", 16)}<span class="tag">Escalated</span><span><b>{len(escalated)} review(s) escalated to {html.escape(PROJECT_LEAD)}.</b> '
                "They expire 48 h after escalation, and the issue is then withheld.</span></div>", unsafe_allow_html=True)

tab_new, tab_queue, tab_jira, tab_runs = st.tabs([":material/edit_note: New meeting", f":material/fact_check: Review queue ({len(open_reviews)})",
                                                  f":material/confirmation_number: Simulated Jira ({wf.jira.count()})", ":material/history: Runs and audit"])

# ------------------------------------------------------------- new meeting
with tab_new:
    with st.form("new_meeting", border=True):
        c1, c2, c3 = st.columns(3)
        meeting_id = c1.text_input("Meeting ID", value="sprint-2026-10-08")
        meeting_date = c2.date_input("Meeting date", value=date(2026, 10, 8))
        organizer = c3.selectbox("Organizer (reviewer)", PEOPLE, index=PEOPLE.index("shubhada"))
        transcript = st.text_area("Transcript", value=SAMPLE, height=130)
        submitted = st.form_submit_button("Send transcript event", type="primary", icon=":material/send:")
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
        if c2.button("Deliver the same event again (webhook retry)", key="redeliver", icon=":material/replay:", width="stretch"):
            send(ss["last_transcript_event"])
            st.rerun()

# ------------------------------------------------------------ review queue
with tab_queue:
    if not open_reviews:
        st.info("No open reviews. Send a transcript event from **New meeting**.", )
    for review in open_reviews:
        p = review["packet_json"]
        fields = p["proposed_action"]["fields"]
        with st.container(border=True, key=f"card-{review['review_id']}"):
            left, right = st.columns([3, 2], gap="large")
            with left:
                st.markdown(
                    pill(p["validation"]) + pill(review["status"])
                    + f'<div class="card-title" style="margin-top:8px">{html.escape(fields["summary"])}</div>'
                    + f'<div class="quote">“{html.escape(p["evidence"]["quote"])}”</div>'
                    + '<div class="grid">' + field("Action", p["proposed_action"]["action"])
                    + field("Assignee", fields["assignee"]) + field("Due date", fields["due_date"]) + "</div>"
                    + ('<div class="conflict">' + icon("alert", 15) + '<span class="tag">Conflict</span>' + html.escape("; ".join(p["conflicts"])) + "</div>" if p["conflicts"] else "")
                    + '<div class="policy"><div class="policy-h">Policy checks</div>' + "".join(
                        f'<div class="rule"><span class="rid">{html.escape(r.split(" ", 1)[0])}</span>{html.escape(r.split(" ", 1)[1])}</div>'
                        for r in p["rules"]) + "</div>",
                    unsafe_allow_html=True)
            with right:
                st.markdown(
                    '<dl class="meta">'
                    f'<dt>{icon("user")}Reviewer</dt><dd>{html.escape(review["assigned_to"])}</dd>'
                    f'<dt>{icon("arrow-up")}Escalates to</dt><dd>{html.escape(review["escalation_to"])}</dd>'
                    f'<dt>{icon("calendar")}Deadline</dt><dd>{html.escape(review["deadline"].replace("T", " ").replace("Z", " UTC"))}</dd>'
                    f'<dt>{icon("lock")}Version</dt><dd><code>{html.escape(p["content_version"])}</code></dd></dl>',
                    unsafe_allow_html=True)
                with st.form(f"decide-{review['review_id']}", border=False):
                    c1, c2 = st.columns(2)
                    owner = c1.text_input("Owner", value=fields["assignee"] or "", placeholder="e.g. tejas")
                    due = c2.text_input("Due date", value=fields["due_date"] or "", placeholder="Friday or 2026-10-16")
                    comment = st.text_input("Comment (optional)")
                    b1, b2 = st.columns(2)
                    approve = b1.form_submit_button("Approve", type="primary", icon=":material/check:", width="stretch")
                    reject = b2.form_submit_button("Reject", icon=":material/close:", width="stretch")
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
        st.info("No issues yet. An issue is created only after a reviewer approves it.")
    else:
        rows = "".join(
            f'<tr><td><span class="key">{html.escape(i["key"])}</span></td><td>{html.escape(i["summary"])}</td>'
            f'<td>{html.escape(i["assignee"] or "")}</td><td>{html.escape(i["due_date"] or "")}</td>'
            f'<td><code>{html.escape(", ".join(json.loads(i["labels"]) if isinstance(i["labels"], str) else i["labels"]))}</code></td></tr>'
            for i in issues)
        st.markdown(f'<div class="table-card"><div class="tc-head"><span>{icon("ticket", 16)}Issues created</span>'
                    f'<span class="count">{len(issues)}</span></div>'
                    '<table class="issues"><thead><tr><th>Issue</th><th>Summary</th><th>Assignee</th><th>Due</th>'
                    f'<th>Idempotency label</th></tr></thead><tbody>{rows}</tbody></table></div>', unsafe_allow_html=True)
    st.caption("Counted in the receiver's own database (data/ui/jira_sim.db), not in the workflow's status fields.")

# ----------------------------------------------------------- runs and audit
with tab_runs:
    runs = wf.store.all("SELECT run_id, meeting_id, status, updated_at FROM runs ORDER BY updated_at DESC")
    if not runs:
        st.info("No runs yet.")
    for run in runs:
        with st.expander(f"{run['meeting_id']}  ·  {run['status'].replace('_', ' ').capitalize()}  ·  {run['run_id']}"):
            st.markdown("**Reviews**")
            st.dataframe(wf.store.all("SELECT review_id, status, assigned_to, deadline FROM reviews WHERE run_id = ?",
                                      run["run_id"]), width="stretch", hide_index=True,
                         column_config={"review_id": "Review", "status": "Status", "assigned_to": "Reviewer", "deadline": "Deadline"})
            actions = wf.store.all("SELECT idempotency_key, status, attempts, jira_key, last_error FROM actions "
                                   "WHERE run_id = ?", run["run_id"])
            if actions:
                st.markdown("**Outbox (external actions)**")
                st.dataframe(actions, width="stretch", hide_index=True,
                             column_config={"idempotency_key": "Idempotency key", "status": "Status", "attempts": "Attempts",
                                            "jira_key": "Jira issue", "last_error": "Last error"})
            events = wf.store.one("SELECT state_json FROM runs WHERE run_id = ?", run["run_id"])["state_json"]["events"]
            st.markdown("**Audit trail**")
            st.dataframe([{k: e.get(k) for k in ("timestamp", "step", "event", "candidate_id", "result", "reason", "error")}
                          for e in events], width="stretch", hide_index=True,
                         column_config={"timestamp": "Time", "step": "Step", "event": "Event", "candidate_id": "Candidate",
                                        "result": "Result", "reason": "Reason", "error": "Error"})
    with st.expander("Event inbox (every event_id and its outcome)"):
        st.dataframe(wf.store.all("SELECT event_id, type, received_at, outcome FROM inbox ORDER BY received_at DESC"),
                     width="stretch", hide_index=True,
                     column_config={"event_id": "Event ID", "type": "Type", "received_at": "Received", "outcome": "Outcome"})
