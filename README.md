# Milestone 2 Prototype

This prototype implements Tejas Vaidya's Milestone 2 work: a hosted-model extraction path, strict structured output, deterministic owner/date/duplicate validation, synthetic inputs, bounded schema retry, approval gating, simulated Jira handoff, and JSONL execution logging.

## Setup

```bash
cd Project
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env` (or the shell) and optionally change `OPENAI_MODEL`. Never commit `.env` or an API key.

## Run the deterministic tests

```bash
pytest -q
```

The tests exercise a successful owner/date validation, a FLAG branch for unresolved fields, empty-input stopping, and the approval guard. They do not call the hosted model.

## Professor demo (no API key required)

```bash
python demo.py
```

This prints one valid candidate, one FLAG candidate, the approval guard blocking both until review, and the audit trail. Every step is written to `logs/audit.jsonl` and a snapshot of the run is saved to `logs/runs/<run_id>.json` (see **Run state and audit log** below).

## Streamlit UI demo

```bash
python3 -m pip install -r requirements.txt
streamlit run app.py
```

Open the local URL shown by Streamlit, keep `Demo (no API key)` selected, click **Extract and validate**, review the PASS/FLAG results, select an item, and click **Run approval guard and Jira handoff**. Choose `Hosted model (OpenAI)` only after setting `OPENAI_API_KEY` in `.env`.

## Use the hosted model path

```python
from dotenv import load_dotenv
from meeting_assistant import extract_with_openai, run_workflow

load_dotenv()
state = run_workflow(
    transcript="Priya will update the API error messages by Friday.",
    meeting_date="2026-10-08",
    extractor=extract_with_openai,
)
print(state)
```

The model returns only candidates. Candidates with missing or unresolved owners/dates are marked `FLAG`. A candidate reaches the simulated Jira handoff only after an explicit approval set is supplied; `PASS` does not imply approval.

## Run state and audit log

- `state.py` (Task Status Tracker): one `RunState` per run. It holds `run_id`, `status`, the meeting date, the transcript's SHA-256 hash and length (never the text), the extraction, candidates, `retry_count` per step, `errors`, `events`, and a timestamp for each status. Status flow: `started → input_validated → extracted → awaiting_review → completed | completed_with_items_needing_review`, or `failed`.
- `audit.py` (Audit Logger): appends one JSON line per event to `logs/audit.jsonl` (time, run_id, step, event, status, retry counts, details) and writes the full state to `logs/runs/<run_id>.json` after each phase. A failed log write is recorded in `errors` and never stops the run.
- `submit_review(state, approved_ids)` applies reviewer approvals, runs the approval guard and simulated Jira handoff, and closes the run. The Streamlit UI uses it, so its approvals are logged too.

Reconstruct one run: `grep '"run_id": "run-xxxxxxxx"' logs/audit.jsonl`.

## Synthetic data

`synthetic_transcripts.json` contains representative success, ambiguous/FLAG, and empty-input cases. Extend it with hand-labeled transcripts for the team's larger evaluation set.

## Milestone 2 evidence to capture

Run the tests and one keyed hosted-model run, then copy the run ID, candidate validation results, retry/stop event (if exercised), and simulated Jira result into Sections 4 and 5 of the working draft. `logs/audit.jsonl` and `logs/runs/` are the execution record; it intentionally excludes credentials.
