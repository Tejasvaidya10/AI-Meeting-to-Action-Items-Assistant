# Milestone 3 design: human checkpoint, events, durable state

Architecture diagram: [`M3_Architecture.drawio`](M3_Architecture.drawio) (open in draw.io; PNG export alongside).

Code: `controller.py` (Workflow Controller, Approval Guard), `store.py` (durable state),
`jira_sim.py` (simulated receiver), `cli.py` (reviewer interface), `test_m3_workflow.py`.

## 1. Oversight per external action

| Action | Affects | Stakes / reversibility | Oversight |
|---|---|---|---|
| Create Jira issue (`jira.create_issue`) | Jira project; notifies the assignee | Visible to others, assigns work to a person; deletable but the notification cannot be recalled | **Approval before execution** (implemented) |
| Edit owner/due date during review | Content of the proposed issue | Changes who is committed to what | Part of the approval: the edit creates a new content version, and the approval binds to it (implemented) |
| Hosted model call | Spends money; sends transcript text to a provider | Small cost per call; data leaves the boundary | **No per-call review**; monitoring with intervention on token spend and failure rate (planned alerting) |
| Retry of an approved Jira create | Jira | Could duplicate an issue | **No human review**. Automatic and bounded (3 attempts), with a reconcile check before any resend. Exhausted retries raise an alert to operations (implemented) |
| Write run state and audit log | Internal SQLite and JSONL | Internal and append-only | **Later audit** (implemented) |
| Post meeting summary to Slack/email | Channel members | Hard to recall once read | Approval before execution (planned, not built) |
| Update or close an existing Jira issue | An existing record | Overwrites someone's work | Out of scope. The system only creates issues; R4 flags likely duplicates instead of editing them |

## 2. The checkpoint

- **Reviewer:** the meeting organizer (`organizer` on the transcript event).
- **Escalation:** if there is no decision in 24 h, the case goes to the project lead (`kriday`). Either of them can then decide. If there is still no decision 48 h later, the review **expires and the action is withheld**.
- **What the reviewer sees** (`python cli.py review <id> --as <name>`): the proposed action and target, the exact fields that will be sent, the evidence quote with the words the model read, the validation result, conflicts, the rules that apply, and the content version.
- **What approval authorizes** (stored in `reviews.decision_json.authorization`):
  `{action: jira.create_issue, target: {project: MTG, assignee}, content_version: v-…, approved_by, approved_at, expires_at: approved_at + 24h}`.
  Just before sending, the Approval Guard recomputes the content version and checks the expiry. If the content changed or the approval is too old, the action is **blocked**.
- **Approve:** an outbox row is committed, then Jira is called, and the issue key is written back to the run.
  **Reject:** no action row is created; the review is `rejected`.
  **Timeout:** the case goes `escalated` and then `expired`. Nothing is sent, and the run ends `completed_with_items_needing_review`.
- **FLAG items:** an approval without edits returns `needs_changes`, because the guard will not authorize an issue with no owner or due date. The reviewer edits, and the approval then binds to the edited version.

Example packet (FLAG case, from a CLI run):

```
Review rev-a8c47fb9  [pending]  run run-43ec2b17  meeting sprint-2026-10-08
  Assigned to: shubhada   escalates to: kriday   deadline: 2026-10-30T23:41:32Z
  Proposed action: jira.create_issue in project MTG
    summary  : Clean up the onboarding notes
    assignee : -- UNRESOLVED --
    due_date : -- UNRESOLVED --
  Evidence:  "Someone should clean up the onboarding notes soon."
  Validation: FLAG
  Conflicts: owner is missing; due date is missing or cannot be normalized
  Rules:
    - R1 No Jira issue is created without an explicit, unexpired approval of this exact content version.
    - R2 The assignee must resolve to a person in the project directory.
    - R3 The due date must normalize to a calendar date.
  Content version: v-5b3af3bc5425
```

## 3. State

Review (one per candidate):

| From | Event / condition | To | Effect |
|---|---|---|---|
| — | `transcript.received` (new meeting_id + transcript hash) | pending | packet stored, 24 h deadline |
| pending | `timer.tick` after deadline, or a decision arriving after it | escalated | project lead added, +48 h deadline |
| pending / escalated | `review.decision` approve, authorized reviewer, current version, fields resolved | approved | authorization + outbox row (`queued`) in one transaction |
| pending / escalated | approve with unresolved owner/date | (unchanged) | `needs_changes`, guard blocked |
| pending / escalated | `review.decision` reject | rejected | no action |
| escalated | `timer.tick` after deadline, or a decision arriving after it | expired | action withheld + `alert` |
| any terminal | further decisions | (unchanged) | `already_decided` / `late_ignored` |

Action (outbox, one per approved review):

| From | Event / condition | To |
|---|---|---|
| queued | Approval Guard passes, call starts | sending (attempts + 1) |
| sending | Jira returns a key | done |
| sending | 503 unavailable, attempts < 3 | queued (retried on next tick) |
| sending | 503 on 3rd attempt | failed + `alert` |
| sending | timeout after request sent | uncertain |
| sending / uncertain (on resume or tick) | Jira search by idempotency label finds the issue | done (no resend) |
| sending / uncertain | not found | guard re-check, then resend |
| queued | content hash ≠ approved version, or approval expired | blocked |

Run: `awaiting_review` stays open until every review is terminal and every action is done, failed or blocked. It then ends `completed` (everything approved or rejected and sent) or `completed_with_items_needing_review` (something expired, failed or was blocked).

## 4. Events and failure handling

- **Duplicate events:** each `event_id` is inserted into `inbox` in the same transaction as its state change. A redelivery returns `duplicate_ignored`. A new `event_id` carrying an already-seen meeting (same meeting_id + transcript SHA-256) returns `duplicate_content`. A second approval click with a new id returns `already_decided`. `actions.review_id` is UNIQUE, so one review can never produce two actions.
- **Late events:** a decision is judged by the review deadlines even if the timer has not fired yet. After 24 h the review is escalated first, so the project lead may decide. After 72 h it expires on the spot and the decision returns `late_ignored`.
- **Failed extraction:** if the model call errors or times out, nothing is recorded, so a redelivery of the same event is processed again. If the reply is still schema-invalid after one retry, the run is saved as `failed` and an `alert` is logged. Only live runs count for `duplicate_content`, so the organizer can resend the same transcript to start a new run.
- **Unexpected events:** an unknown type is recorded as `unsupported_type`. Missing fields return `malformed`. An unknown review returns `unknown_review`. None of these change state, and all are kept in `inbox` for inspection.
- **Safe retries:** the idempotency key is `review_id:content_version`. It is stored on the Jira issue as a label (`idem-…`), because Jira has no idempotency header.
- **Effect succeeded but confirmation lost:** the action is marked `uncertain`. Before any retry, the controller searches Jira for the label. If the issue is found, the action is marked done with that key; if not, it is resent. The same check runs for rows left in `sending` by a crash.
- **Interruption:** every command and service start calls `resume()`. Decisions are committed before Jira is called, so a crash between the two leaves a `queued` row, which is finished exactly once on restart.
