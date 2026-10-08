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

This prints one valid candidate, one FLAG candidate, the approval guard blocking both until review, and the audit trail. It also appends the run to `execution_log.jsonl`.

## Streamlit UI demo

```bash
python3 -m pip install -r requirements.txt
streamlit run app.py
```

Open the local URL shown by Streamlit, keep `Demo (no API key)` selected, click **Extract and validate**, review the PASS/FLAG results, select an item, and click **Run approval guard and Jira handoff**. Choose `Hosted model (OpenAI)` only after setting `OPENAI_API_KEY` in `.env`.

## Use the hosted model path

```python
from dotenv import load_dotenv
from meeting_assistant import extract_with_openai, run_workflow, write_jsonl

load_dotenv()
state = run_workflow(
    transcript="Priya will update the API error messages by Friday.",
    meeting_date="2026-10-08",
    extractor=extract_with_openai,
)
write_jsonl(state)
print(state)
```

The model returns only candidates. Candidates with missing or unresolved owners/dates are marked `FLAG`. A candidate reaches the simulated Jira handoff only after an explicit approval set is supplied; `PASS` does not imply approval.

## Synthetic data

`synthetic_transcripts.json` contains representative success, ambiguous/FLAG, and empty-input cases. Extend it with hand-labeled transcripts for the team's larger evaluation set.

## Milestone 2 evidence to capture

Run the tests and one keyed hosted-model run, then copy the run ID, candidate validation results, retry/stop event (if exercised), and simulated Jira result into Sections 4 and 5 of the working draft. The JSONL log is the execution record; it intentionally excludes credentials.
