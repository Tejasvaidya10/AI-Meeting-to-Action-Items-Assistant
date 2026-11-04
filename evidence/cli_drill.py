"""Run the README walk-through as separate CLI processes and print a transcript.

  python evidence/cli_drill.py > evidence/cli_drill.txt

Each step is a new `python cli.py ...` process against fresh databases in a
temporary directory, so state only survives through SQLite, as after a restart.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
DBS = ["--db", str(TMP / "workflow.db"), "--jira-db", str(TMP / "jira_sim.db")]


def cli(*args: str, answers: str = "", fault: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "JIRA_FAULT"}
    if fault:
        env["JIRA_FAULT"] = fault
    shown = " ".join(args).replace(str(REPO) + "/", "")
    print(f"\n$ {'JIRA_FAULT=' + fault + ' ' if fault else ''}python cli.py {shown}")
    if answers:
        print(f"  (reviewer types: {answers.strip().splitlines()})")
    out = subprocess.run([sys.executable, "cli.py", *DBS, *args], cwd=REPO, env=env,
                         input=answers, capture_output=True, text=True)
    text = (out.stdout + out.stderr).rstrip()
    print(text)
    return text


def peek() -> None:
    """Read both databases directly, without starting the service (so no resume() runs)."""
    with sqlite3.connect(TMP / "workflow.db") as wf, sqlite3.connect(TMP / "jira_sim.db") as jira:
        actions = wf.execute("SELECT idempotency_key, status, attempts FROM actions").fetchall()
        issues = jira.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    print(f"\n[inspect databases, no process started] actions={actions}  Jira issues={issues}")


def review_ids() -> dict[str, str]:
    ids, current = {}, None
    for line in cli("reviews").splitlines():
        if m := re.match(r"Review (rev-\w+)", line):
            current = m.group(1)
        elif current and (m := re.match(r"\s+summary\s+: (.+)", line)):
            ids[m.group(1)] = current
    return ids


def main() -> None:
    print("# Milestone 3 CLI drill: every command below is a separate process")
    events = REPO / "sample_events"
    run_id = re.search(r'"run_id": "(run-\w+)"', cli("emit", str(events / "01_transcript_success.json"))).group(1)
    cli("emit", str(events / "02_transcript_redelivered.json"))
    cli("emit", str(events / "03_transcript_same_content_new_id.json"))
    cli("emit", str(events / "05_unknown_event_type.json"))
    cli("emit", str(events / "06_malformed_transcript.json"))
    ids = review_ids()
    api, flag, release = (next(v for k, v in ids.items() if word in k.lower())
                          for word in ("api", "onboarding", "release"))

    print("\n## Interruption: approval saved, process stops before Jira")
    cli("review", api, "--as", "shubhada", "--crash-before-send", answers="a\n\n")
    peek()
    cli("resume")
    cli("resume")
    cli("jira")

    print("\n## FLAG item: approve as-is is refused, then edit + lost Jira reply")
    cli("review", flag, "--as", "shubhada", answers="a\n\n")
    cli("review", flag, "--as", "shubhada", answers="e\ntejas\n2026-10-16\n\n", fault="lose_confirmation")
    peek()
    cli("resume")
    cli("jira")

    print("\n## Jira unavailable (503), retried by the next process start or tick")
    cli("review", release, "--as", "shubhada", answers="a\n\n", fault="unavailable")
    peek()
    cli("tick")
    cli("jira")

    print("\n## Repeated approval of an already-decided review")
    cli("review", api, "--as", "shubhada", answers="a\n\n")
    cli("jira")

    print("\n## Audit trail")
    cli("run", run_id)


if __name__ == "__main__":
    main()
