"""Milestone 4 evidence: each control with a blocked case and a normal case.

    python m4_controls_demo.py        (no API key needed; writes into a temp folder)

Results are read where they land: the simulated Jira's database, the text given
to the model, and the files we store.
"""
import os
import tempfile

import test_m4_controls as t


def run_case(title, transcript, extractor, approve_all=True):
    print(f"\n--- {title} ---")
    wf = t.make_wf(extractor)
    run = t.ingest(wf, transcript)
    for review_id in run["reviews"]:
        packet = wf.review(review_id)["packet_json"]
        outcome = t.approve(wf, review_id)["outcome"] if approve_all else "-"
        print(f"  {packet['proposed_action']['fields']['summary'][:45]:45}  reviewer approves -> {outcome}")
    issues = wf.jira.issues()
    print(f"  Issues in simulated Jira: {len(issues)}  {[i['summary'] for i in issues]}")
    return wf


def main():
    os.chdir(tempfile.mkdtemp())
    print("CONTROL A - untrusted-content guard")
    run_case("A1 blocked: transcript with a hidden instruction, model obeys it", t.INJECTED, t.obedient_model)
    security = [e for e in t.audit_events() if e["step"] == "security"]
    for e in security:
        print(f"  audit: {e['event']}  {e.get('reasons') or e.get('sentences', '')}")
    os.chdir(tempfile.mkdtemp())
    run_case("A2 normal: ordinary meeting", t.CLEAN, t.obedient_model)

    print("\nCONTROL B - sensitive-data redaction")
    os.chdir(tempfile.mkdtemp())
    spy = t.SpyModel()
    run_case("B1 blocked: transcript with a fake SSN, e-mail, phone, API key, password", t.WITH_PII, spy)
    print("  Text sent to the model:", spy.inputs[0])
    leaked = [v for v in t.RAW_VALUES if v in t.stored_text()]
    print(f"  Raw values found in audit log, snapshots, workflow.db, jira_sim.db: {leaked or 'none'}")
    print("  Logged counts:", next(e for e in t.audit_events() if e["event"] == "sensitive_data_redacted")["counts"])
    os.chdir(tempfile.mkdtemp())
    spy = t.SpyModel()
    run_case("B2 normal: ordinary meeting", t.CLEAN, spy)
    print("  Text sent to the model is unchanged:", spy.inputs == [t.CLEAN])


if __name__ == "__main__":
    main()
