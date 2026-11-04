# Sample events

Deliver with `python cli.py emit sample_events/<file>`. Review decisions are created
interactively by `python cli.py review <review_id> --as <reviewer>` because they must
quote the review's current content version.

| File | Shows |
|---|---|
| 01_transcript_success.json | New meeting: 3 candidates (2 PASS, 1 FLAG) -> 3 open reviews |
| 02_transcript_redelivered.json | Same event_id again -> `duplicate_ignored` |
| 03_transcript_same_content_new_id.json | Same meeting + transcript under a new event_id -> `duplicate_content`, no second run |
| 04_transcript_duplicate_task.json | Candidate matches an open task -> FLAG with rule R4 conflict |
| 05_unknown_event_type.json | Unsupported type -> recorded as `unsupported_type`, no state change |
| 06_malformed_transcript.json | Missing transcript -> `malformed`, no run created |
