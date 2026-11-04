"""Command-line front end for the Milestone 3 workflow (the human checkpoint).

  python cli.py emit sample_events/01_transcript_success.json   # deliver any event (simulated webhook)
  python cli.py reviews                                           # open review queue
  python cli.py review rev-xxxxxxxx --as shubhada                 # show packet, prompt for a decision
  python cli.py tick [--now 2026-10-10T09:00:00Z]                 # timer: escalate / expire / retry
  python cli.py resume                                            # reconcile actions left open by a crash
  python cli.py jira                                              # what the (simulated) receiver holds
  python cli.py run run-xxxxxxxx                                  # run status and audit trail

Every command first calls resume(), as a restarted service would.
Set JIRA_FAULT=unavailable or JIRA_FAULT=lose_confirmation to inject one fault.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from controller import SimulatedCrash, Workflow
from jira_sim import SimulatedJira


def show_packet(review: dict) -> None:
    p = review["packet_json"]
    fields = p["proposed_action"]["fields"]
    print(f"\nReview {review['review_id']}  [{review['status']}]  run {p['run_id']}  meeting {p['meeting_id']}")
    print(f"  Assigned to: {review['assigned_to']}   escalates to: {review['escalation_to']}   deadline: {review['deadline']}")
    print(f"  Proposed action: {p['proposed_action']['action']} in project {p['proposed_action']['target']['project']}")
    for name in ("summary", "assignee", "due_date"):
        print(f"    {name:9}: {fields[name] or '-- UNRESOLVED --'}")
    print(f"  Evidence:  \"{p['evidence']['quote']}\"  (owner said: {p['evidence']['owner_text']!r}, due said: {p['evidence']['due_date_text']!r})")
    print(f"  Validation: {p['validation']}")
    print("  Conflicts: " + ("; ".join(p["conflicts"]) or "none"))
    print("  Rules:")
    for rule in p["rules"]:
        print(f"    - {rule}")
    print(f"  Content version: {p['content_version']}")


def prompt_decision(review: dict, reviewer: str) -> dict:
    choice = input("\nApprove, reject, or edit then approve? [a/r/e/q] ").strip().lower()
    if choice not in ("a", "r", "e"):
        raise SystemExit("No decision recorded.")
    payload = {"review_id": review["review_id"], "reviewer": reviewer,
               "content_version": review["packet_json"]["content_version"],
               "decision": "reject" if choice == "r" else "approve"}
    if choice == "e":
        edits = {k: v for k, v in (("owner", input("  owner (blank = keep): ").strip()),
                                   ("due_date", input("  due date, e.g. Friday or 2026-10-16 (blank = keep): ").strip())) if v}
        payload["edits"] = edits
    payload["comment"] = input("  comment (optional): ").strip() or None
    return {"event_id": f"evt-{uuid.uuid4().hex[:10]}", "type": "review.decision", "payload": payload}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/workflow.db")
    parser.add_argument("--jira-db", default="data/jira_sim.db")
    sub = parser.add_subparsers(dest="cmd", required=True)
    emit = sub.add_parser("emit")
    emit.add_argument("event_file")
    emit.add_argument("--crash-before-send", action="store_true", help="stop after the decision is saved, before Jira")
    sub.add_parser("reviews")
    rev = sub.add_parser("review")
    rev.add_argument("review_id")
    rev.add_argument("--as", dest="reviewer", required=True)
    rev.add_argument("--crash-before-send", action="store_true")
    tick = sub.add_parser("tick")
    tick.add_argument("--now")
    sub.add_parser("resume")
    sub.add_parser("jira")
    run = sub.add_parser("run")
    run.add_argument("run_id")
    args = parser.parse_args()

    wf = Workflow(args.db, jira=SimulatedJira(args.jira_db))
    recovered = wf.resume()
    if recovered or args.cmd == "resume":
        print(f"resume: {recovered or 'nothing left open'}")

    if args.cmd in ("emit", "review"):
        if args.cmd == "emit":
            event = json.loads(Path(args.event_file).read_text())
        else:
            review = wf.review(args.review_id)
            if review is None:
                raise SystemExit(f"No review {args.review_id}")
            show_packet(review)
            event = prompt_decision(review, args.reviewer)
        try:
            print(json.dumps(wf.handle(event, crash_before_send=args.crash_before_send), indent=2))
        except SimulatedCrash as exc:
            raise SystemExit(f"SIMULATED CRASH: {exc}")
    elif args.cmd == "reviews":
        for review in wf.open_reviews():
            show_packet(review)
        print(f"\n{len(wf.open_reviews())} open review(s)")
    elif args.cmd == "tick":
        event = {"event_id": f"tick-{uuid.uuid4().hex[:10]}", "type": "timer.tick",
                 "payload": {"now": args.now} if args.now else {}}
        print(json.dumps(wf.handle(event), indent=2))
    elif args.cmd == "jira":
        issues = wf.jira.issues()
        for issue in issues:
            print(f"{issue['key']}: {issue['summary']} -> {issue['assignee']} due {issue['due_date']}  {issue['labels']}")
        print(f"{len(issues)} issue(s) in simulated Jira")
    elif args.cmd == "run":
        row = wf.store.one("SELECT * FROM runs WHERE run_id = ?", args.run_id)
        if row is None:
            raise SystemExit(f"No run {args.run_id}")
        print(f"{args.run_id}: {row['status']}")
        for event in row["state_json"]["events"]:
            extra = " ".join(f"{k}={event[k]}" for k in ("candidate_id", "result", "reason", "error") if event.get(k))
            print(f"  {event['timestamp']}  {event['step']:15} {event['event']:22} {extra}")


if __name__ == "__main__":
    main()
