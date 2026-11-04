# AI Meeting-to-Action-Items Assistant

Team 8, Enterprise AI: Anusha Bellavara, Gouri Sharma, Kriday Kallalathil, Shubhada Mandala, Tejas Vaidya.

The assistant turns a meeting transcript into proposed Jira issues. A model extracts candidate tasks, local rules check each one (PASS or FLAG), and **a named person must approve each issue before it is created**. Milestone 3 makes this event-driven and durable: a run can wait days for a reviewer, survive a restart, and never create the same issue twice.

```
transcript event ─► extract (LLM) ─► validate (owner, date, duplicate) ─► review checkpoint ─► Approval Guard ─► Jira
                                                                       organizer, then lead     exact version,
                                                                       24h + 48h, then expire   24h expiry, ≤3 tries
```

| Status | Component |
|---|---|
| Implemented | Event handler with inbox dedup, review checkpoint with escalation and expiry, Approval Guard, SQLite state, outbox with reconcile-before-retry, Streamlit reviewer app and CLI reviewer (same events) |
| Simulated | Jira (`jira_sim.py`, its own database plus fault injection); meeting webhook and timer (CLI commands) |
| Planned | Live Jira, Slack/email summary, cloud deployment (PostgreSQL, containers) |

## Quick start

Requires Python 3.11+ (tested on 3.13). No API key is needed for the tests or the demos.

```bash
git clone https://github.com/Tejasvaidya10/AI-Meeting-to-Action-Items-Assistant.git
cd AI-Meeting-to-Action-Items-Assistant
git checkout shubhada/m3-hitl-events
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt

pytest -q                       # 40 tests, all passing
python m3_scenarios.py          # the four Milestone 3 report cases
python evidence/cli_drill.py    # full CLI walk-through as separate processes
```

## Milestone 3: reviewer app (Streamlit)

```bash
streamlit run app.py            # opens http://localhost:8501
```

The app drives the same Workflow Controller as the CLI, with its own data in `data/ui/`:

- **New meeting**: send a `transcript.received` event; "Deliver the same event again" shows `duplicate_ignored`.
- **Review queue**: the full review packet (proposed action, fields, evidence, conflicts, rules, content version), with Approve, Reject, or edit owner/due date then Approve. "Signed in as" picks the reviewer; anyone other than the organizer (or the project lead after escalation) gets `unauthorized`.
- **Simulated Jira**: the issues actually in the receiver's database.
- **Runs and audit**: run status, reviews, the outbox (attempts, Jira key) and the audit trail.
- **Sidebar drills**: inject a Jira 503 or a lost reply for the next call, stop the process before the Jira call, advance the clock and run the timer (escalation after 24 h, expiry after 72 h), or reset the demo data.

Every page load calls `resume()`, so a stopped or uncertain action is finished or reconciled on the next load. `test_app.py` drives the app headlessly (8 tests); screenshots are in `evidence/ui/`.

## Milestone 3: try the event path from the command line

Each command is a separate process. State survives between them in `data/workflow.db`, and the simulated Jira keeps its own `data/jira_sim.db`. Every command first runs `resume()`, as a restarted service would.

```bash
python cli.py emit sample_events/01_transcript_success.json      # new meeting -> 3 open reviews
python cli.py emit sample_events/02_transcript_redelivered.json  # same event_id -> duplicate_ignored
python cli.py reviews                                            # the review queue, with full packets
python cli.py review <review_id> --as shubhada                   # approve (a), reject (r) or edit then approve (e)
python cli.py jira                                               # issues actually in the simulated Jira
python cli.py tick --now 2026-11-10T00:00:00Z                    # timer: escalate, expire, retry
python cli.py run <run_id>                                       # full audit trail of one run
```

Failure drills:

```bash
python cli.py review <id> --as shubhada --crash-before-send            # approval saved, process stops before Jira
python cli.py resume                                                   # restart finishes it exactly once
JIRA_FAULT=lose_confirmation python cli.py review <id> --as shubhada   # issue created, reply lost -> uncertain
python cli.py resume                                                   # finds the issue by its label, no resend
JIRA_FAULT=unavailable python cli.py review <id> --as shubhada         # 503 -> queued, retried on next start or tick
```

To start over, delete the `data/` folder.

### What the reviewer approves

The review packet shows the proposed action and target, the exact fields to send, the transcript quote, the validation result, any conflicts, the rules that apply and a **content version** (SHA-256 of the fields). An approval authorizes one action (`jira.create_issue`), one target (project MTG, assignee), that content version, by that approver, until approval time + 24 h. The Approval Guard re-checks the version and expiry just before calling Jira. A FLAG item with no owner or date cannot be approved as-is (`needs_changes`); the reviewer edits it and the approval binds to the edited version.

### Events and outcomes

| Situation | Outcome | State change |
|---|---|---|
| Same `event_id` delivered again | `duplicate_ignored` | none |
| Same meeting and transcript, new `event_id` | `duplicate_content` | none, returns the existing run |
| Second approval click | `already_decided` | none (`actions.review_id` is unique) |
| Decision after expiry | `late_ignored` | review expired, nothing sent |
| Old content version or non-reviewer | `stale_version` / `unauthorized` | none |
| Unknown type, missing fields, unknown review | `unsupported_type` / `malformed` / `unknown_review` | none, kept in the inbox |
| No decision in 24 h, then 48 h more | escalated to the project lead, then `expired` | action withheld |
| Jira 503 | action `queued`, retried; `failed` + alert after 3 attempts | |
| Jira created the issue but the reply was lost | action `uncertain`; Jira searched by label before any retry | `done`, no duplicate |

Design details, state tables and the oversight approach are in [`docs/M3_design.md`](docs/M3_design.md).

## Evidence

Saved outputs quoted in the Milestone 3 report:

| File | Contents |
|---|---|
| `evidence/pytest.txt` | All 40 tests, verbose |
| `evidence/ui/` | Screenshots of the reviewer app after a transcript and one approval |
| `evidence/m3_scenarios.txt` | The four required cases, with run IDs and Jira issue counts |
| `evidence/cli_drill.txt` | CLI walk-through as separate processes, with database reads between steps (3 approvals → 3 issues) |
| `evidence/eval_mock.txt` | Labeled-set evaluation of the rule-based baseline |
| `evidence/eval_openai.txt` | Labeled-set evaluation of gpt-4o-mini (full record in `eval_results/`) |

Run IDs change on every run; the outcomes and counts do not.

## Evaluation on labeled transcripts

`labeled_transcripts.json` holds 8 hand-labeled transcripts (11 expected tasks: 3 clear cases, 5 ambiguous).

```bash
python evaluate_m3.py --extractor mock      # rule-based baseline, no key
python evaluate_m3.py --extractor openai    # hosted model; needs OPENAI_API_KEY
```

Each run saves the run ID, the model's raw output, validation per candidate, tokens and latency to `eval_results/`. Baseline: recall 0.55, precision 0.86, owner accuracy 1.00, date accuracy 0.83, PASS/FLAG agreement 1.00, 1 false PASS. gpt-4o-mini (`eval_results/openai-20261104-183902.json`): recall 1.00, precision 0.92, owner accuracy 0.91, date accuracy 0.91, PASS/FLAG agreement 1.00, 0 false PASS, 1,742 input / 590 output tokens for all 8 transcripts.

For the hosted path, copy `.env.example` to `.env` and set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`, default `gpt-4o-mini`). `.env` is git-ignored; never commit a key.

## Milestone 2 path (still works)

```bash
python demo.py          # one PASS and one FLAG candidate, guard and audit trail, no key
```

`state.py` keeps one `RunState` per run (status per step, transcript SHA-256 hash rather than text, retries, errors). `audit.py` appends events to `logs/audit.jsonl` and snapshots to `logs/runs/<run_id>.json`.

## Repository layout

| File | Role |
|---|---|
| `controller.py` | Workflow controller: events, review checkpoint, Approval Guard, outbox execution, reconcile |
| `store.py` | SQLite state: `runs`, `reviews`, `actions` (outbox), `inbox` (event dedup) |
| `jira_sim.py` | Simulated Jira receiver with fault injection (`JIRA_FAULT`) |
| `cli.py` | Reviewer and operator command line |
| `meeting_assistant.py` | Extraction (OpenAI structured output), validation, Milestone 2 workflow |
| `mock_extractor.py` | Deterministic rule-based extractor for keyless runs |
| `evaluate_m3.py`, `labeled_transcripts.json` | Labeled-set evaluation |
| `m3_scenarios.py`, `evidence/` | Report cases and saved outputs |
| `sample_events/` | Example events for the CLI |
| `test_m3_workflow.py`, `test_app.py`, `test_run_state.py`, `test_meeting_assistant.py` | 17 + 8 + 11 + 4 tests |
| `app.py` | Streamlit reviewer app (Milestone 3 workflow) |
| `demo.py`, `state.py`, `audit.py` | Milestone 2 demo, run state, audit log |
| `docs/M3_design.md` | Milestone 3 design notes |

Only synthetic transcripts are used. No credentials or private data are in this repository.
