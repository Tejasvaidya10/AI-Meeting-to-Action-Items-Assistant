"""One-command classroom demonstration of the Milestone 2 workflow."""
from meeting_assistant import ActionItem, Extraction, run_workflow


def demo_extractor(_: str) -> Extraction:
    return Extraction(
        summary="Sprint follow-up",
        decisions=["Keep the current release date"],
        tasks=[
            ActionItem(
                title="Update API error messages",
                owner_text="Priya",
                due_date_text="Friday",
                evidence="Priya will update the API error messages by Friday.",
            ),
            ActionItem(
                title="Clean onboarding notes",
                owner_text=None,
                due_date_text=None,
                evidence="Someone should clean up the onboarding notes soon.",
            ),
        ],
    )


state = run_workflow(
    "Priya will update the API error messages by Friday. "
    "Someone should clean up the onboarding notes soon.",
    "2026-10-08",
    demo_extractor,
    approve=set(),  # reviewer approves nothing yet, so the guard blocks every item
)

print(f"Run: {state['run_id']}")
print(f"Status: {state['status']}")
print("\nCandidates:")
for candidate in state["candidates"]:
    print(
        f"- {candidate['title']} | validation={candidate['validation']} "
        f"| owner={candidate['owner_id']} | due={candidate['due_date']} "
        f"| review={candidate['review_status']}"
    )
    if candidate["reasons"]:
        print(f"  reasons: {', '.join(candidate['reasons'])}")
print("\nAudit trail:")
for event in state["events"]:
    extra = " ".join(str(event[k]) for k in ("candidate_id", "result") if k in event)
    print(f"- {event['step']}: {event['event']} {extra}".rstrip())
print(f"\nLogs: logs/audit.jsonl and logs/runs/{state['run_id']}.json")
