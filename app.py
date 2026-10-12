"""Streamlit demo UI for the Milestone 2 meeting assistant."""
from datetime import date

import streamlit as st
from dotenv import load_dotenv

from meeting_assistant import ActionItem, Extraction, extract_with_openai, run_workflow, submit_review

load_dotenv()
st.set_page_config(page_title="Meeting-to-Action-Items Assistant", page_icon="✅", layout="wide")


def demo_extractor(_: str) -> Extraction:
    return Extraction(
        summary="Sprint follow-up",
        decisions=["Keep the current release date"],
        tasks=[
            ActionItem(title="Update API error messages", owner_text="Priya", due_date_text="Friday", evidence="Priya will update the API error messages by Friday."),
            ActionItem(title="Clean onboarding notes", owner_text=None, due_date_text=None, evidence="Someone should clean up the onboarding notes soon."),
        ],
    )


st.title("Meeting-to-Action-Items Assistant")
st.caption("AI proposes candidates → deterministic validation checks them → a human approves → Jira handoff is simulated")

with st.sidebar:
    st.header("Run settings")
    mode = st.radio("Extraction mode", ["Demo (no API key)", "Hosted model (OpenAI)"])
    meeting_date = st.date_input("Meeting date", value=date(2026, 10, 8))
    open_titles = st.text_input("Existing open task title (optional)", value="")

default_text = "Priya will update the API error messages by Friday. Someone should clean up the onboarding notes soon."
transcript = st.text_area("Meeting transcript", value=default_text, height=180)

if st.button("Extract and validate", type="primary"):
    extractor = demo_extractor if mode.startswith("Demo") else extract_with_openai
    try:
        state = run_workflow(transcript, meeting_date.isoformat(), extractor, open_titles={open_titles} if open_titles else None)
        st.session_state["state"] = state
    except Exception as exc:
        st.error(f"Run failed: {exc}")

state = st.session_state.get("state")
if state:
    st.divider()
    st.subheader("Validation results")
    if state.get("error"):
        st.error(state["error"])
    else:
        st.info(f"Run `{state['run_id']}` — {state['status']}")
        for err in state.get("errors", []):
            if err["step"] == "audit":
                st.warning(f"Logging error (run continued): {err['message']}")
        approved_ids = []
        for candidate in state["candidates"]:
            label = f"{candidate['title']} — {candidate['validation']}"
            with st.container(border=True):
                left, right = st.columns([3, 2])
                with left:
                    st.markdown(f"**{label}**")
                    st.write(f"Evidence: {candidate['evidence']}")
                with right:
                    st.write(f"Owner: `{candidate['owner_id'] or 'unresolved'}`")
                    st.write(f"Due date: `{candidate['due_date'] or 'unresolved'}`")
                    if candidate["reasons"]:
                        st.warning("; ".join(candidate["reasons"]))
                    if st.checkbox("Approve for simulated Jira", key=candidate["candidate_id"]):
                        approved_ids.append(candidate["candidate_id"])
        if st.button("Run approval guard and Jira handoff", type="secondary"):
            if not approved_ids:
                st.warning("Select at least one candidate with the checkbox above before running the handoff.")
                st.stop()
            st.session_state["state"] = submit_review(state, set(approved_ids))
            st.success("Approval guard completed. Approved items received simulated Jira keys.")
            st.rerun()

        for candidate in state["candidates"]:
            if candidate.get("jira_result"):
                st.success(f"{candidate['title']} → simulated Jira issue `{candidate['jira_result']}`")

    with st.expander("Audit trail"):
        st.json(state.get("events", []))
