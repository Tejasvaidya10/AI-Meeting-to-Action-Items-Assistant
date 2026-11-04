"""Print the evidence for the four Milestone 3 report cases.

  python m3_scenarios.py

Each case runs in a fresh temporary directory. "Jira issues" is counted in the
simulated receiver's own database, not taken from workflow status.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from controller import SimulatedCrash, Workflow
from jira_sim import SimulatedJira

TRANSCRIPT = ("Priya will update the API error messages by Friday. "
              "Someone should clean up the onboarding notes soon.")
START = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def fresh(clock: Clock, faults: list[str] | None = None) -> Workflow:
    return Workflow("data/workflow.db", jira=SimulatedJira("data/jira_sim.db", faults=faults or []), clock=clock)


def ingest(wf: Workflow) -> dict:
    return wf.handle({"event_id": "evt-t1", "type": "transcript.received", "payload": {
        "meeting_id": "sprint-2026-10-08", "meeting_date": "2026-10-08", "organizer": "shubhada", "transcript": TRANSCRIPT}})


def decision(wf: Workflow, review_id: str, choice: str = "approve", event_id: str = "evt-d1", reviewer: str = "shubhada") -> dict:
    version = wf.review(review_id)["packet_json"]["content_version"]
    return {"event_id": event_id, "type": "review.decision", "payload": {
        "review_id": review_id, "decision": choice, "reviewer": reviewer, "content_version": version}}


def tick(wf: Workflow, n: int) -> dict:
    return wf.handle({"event_id": f"tick-{n}", "type": "timer.tick", "payload": {}})


def case(title: str) -> Clock:
    os.chdir(tempfile.mkdtemp())
    print(f"\n=== {title}")
    return Clock()


def main() -> None:
    clock = case("1. Approved run")
    wf = fresh(clock)
    run = ingest(wf)
    result = wf.handle(decision(wf, run["reviews"][0]))
    wf.handle(decision(wf, run["reviews"][1], "reject", "evt-d2"))
    action = wf.store.one("SELECT * FROM actions")
    print(f"run {run['run_id']}: decision={result['outcome']} action={action['status']} key={action['idempotency_key']}")
    print(f"run status={wf.store.one('SELECT status FROM runs')['status']}  Jira issues={wf.jira.count()}  {wf.jira.issues()[0]['key']} -> {wf.jira.issues()[0]['assignee']} due {wf.jira.issues()[0]['due_date']}")

    clock = case("2a. Rejection")
    wf = fresh(clock)
    run = ingest(wf)
    print(f"run {run['run_id']}: decision={wf.handle(decision(wf, run['reviews'][0], 'reject'))['outcome']}  actions={len(wf.store.all('SELECT * FROM actions'))}  Jira issues={wf.jira.count()}")

    clock = case("2b. Timeout: no decision")
    wf = fresh(clock)
    run = ingest(wf)
    clock.now = START + timedelta(hours=25)
    t1 = tick(wf, 1)
    clock.now = START + timedelta(hours=73)
    wf = fresh(clock)  # restarted between ticks
    t2 = tick(wf, 2)
    late = wf.handle(decision(wf, run["reviews"][0], event_id="evt-late", reviewer="kriday"))
    print(f"run {run['run_id']}: +25h escalated={len(t1['escalated'])}  +73h expired={len(t2['expired'])}  late decision={late['outcome']}")
    print(f"run status={wf.store.one('SELECT status FROM runs')['status']}  actions={len(wf.store.all('SELECT * FROM actions'))}  Jira issues={wf.jira.count()}")

    clock = case("3. Duplicate events and retry")
    wf = fresh(clock, faults=["unavailable"])
    run = ingest(wf)
    redelivered = ingest(wf)["outcome"]
    event = decision(wf, run["reviews"][0])
    first = wf.handle(event)
    again = wf.handle(event)["outcome"]
    second_click = wf.handle(decision(wf, run["reviews"][0], event_id="evt-d-click2"))["outcome"]
    print(f"transcript redelivered={redelivered}  first decision={first['outcome']} -> action {list(first['actions'].values())[0]} (Jira 503)")
    print(f"same decision event={again}  second click={second_click}  Jira issues before retry={wf.jira.count()}")
    clock.now += timedelta(minutes=5)
    tick(wf, 1)
    action = wf.store.one("SELECT * FROM actions")
    print(f"after tick: action={action['status']} attempts={action['attempts']}  runs={len(wf.store.all('SELECT * FROM runs'))}  Jira issues={wf.jira.count()}")

    clock = case("4a. Interruption: crash after approval, before Jira")
    wf = fresh(clock)
    run = ingest(wf)
    try:
        wf.handle(decision(wf, run["reviews"][0]), crash_before_send=True)
    except SimulatedCrash as exc:
        print(f"crash: {exc}")
    print(f"saved action={wf.store.one('SELECT status FROM actions')['status']}  Jira issues={wf.jira.count()}")
    wf = fresh(clock)
    print(f"restart resume={wf.resume()}  second resume={wf.resume()}  Jira issues={wf.jira.count()}")

    clock = case("4b. Uncertain outcome: issue created, reply lost")
    wf = fresh(clock, faults=["lose_confirmation"])
    run = ingest(wf)
    first = wf.handle(decision(wf, run["reviews"][0]))
    print(f"decision -> action {list(first['actions'].values())[0]}  Jira issues={wf.jira.count()} (it was created)")
    wf = fresh(clock)
    print(f"restart resume={wf.resume()}")
    action = wf.store.one("SELECT * FROM actions")
    events = [e["event"] for e in wf.store.one("SELECT state_json FROM runs")["state_json"]["events"] if e["step"] == "jira_handoff"]
    print(f"action={action['status']} attempts={action['attempts']} jira_key={action['jira_key']}  Jira issues={wf.jira.count()}")
    print(f"audit: {' -> '.join(events)}")


if __name__ == "__main__":
    main()
