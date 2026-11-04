"""Evaluate extraction + validation on the hand-labeled transcripts.

  python evaluate_m3.py --extractor openai     # needs OPENAI_API_KEY (in .env or the shell)
  python evaluate_m3.py --extractor mock       # plumbing check, no key

For each transcript it keeps the run ID, the model's raw JSON output, the
validation result per candidate, token usage and latency, and writes them to
eval_results/<extractor>-<timestamp>.json. Per-run logs go to logs/ as usual.

Matching: a predicted task matches the unused expected task with the highest
title word overlap (Jaccard >= 0.3).
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from meeting_assistant import extract_with_openai, run_workflow
from mock_extractor import mock_extractor

STOP = {"the", "a", "an", "to", "of", "on", "for", "and"}


class RecordingClient:
    """Wraps the OpenAI client to keep the raw output, token usage and latency of each call."""

    def __init__(self, client: Any) -> None:
        self.client, self.calls = client, []
        self.chat = self.completions = self

    def parse(self, **kwargs: Any) -> Any:
        start = time.perf_counter()
        response = self.client.chat.completions.parse(**kwargs)
        self.calls.append({
            "model": response.model, "raw_output": response.choices[0].message.content,
            "input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens,
            "latency_s": round(time.perf_counter() - start, 2),
        })
        return response


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in STOP}


def match(predicted: list[dict], expected: list[dict]) -> list[tuple[dict, dict | None]]:
    unused, pairs = list(expected), []
    for cand in predicted:
        scored = [(len(words(cand["title"]) & words(e["title"])) / len(words(cand["title"]) | words(e["title"]) or {1}), e)
                  for e in unused]
        best = max(scored, key=lambda s: s[0], default=(0, None))
        if best[0] >= 0.3:
            unused.remove(best[1])
            pairs.append((cand, best[1]))
        else:
            pairs.append((cand, None))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extractor", choices=["openai", "mock"], default="openai")
    parser.add_argument("--data", default="labeled_transcripts.json")
    args = parser.parse_args()
    load_dotenv()

    recorder = None
    if args.extractor == "openai":
        from openai import OpenAI
        recorder = RecordingClient(OpenAI())
        extractor = lambda text: extract_with_openai(text, client=recorder)  # noqa: E731
    else:
        extractor = mock_extractor

    totals = {k: 0 for k in ("expected", "predicted", "matched", "owner_ok", "date_ok", "validation_ok",
                             "false_pass", "input_tokens", "output_tokens")}
    rows = []
    for case in json.loads(Path(args.data).read_text())["cases"]:
        before = len(recorder.calls) if recorder else 0
        state = run_workflow(case["transcript"], case["meeting_date"], extractor,
                             open_titles=set(case.get("open_titles", [])))
        calls = recorder.calls[before:] if recorder else []
        pairs = match(state["candidates"], case["expected_tasks"])
        checks = []
        for cand, exp in pairs:
            check = {"title": cand["title"], "owner_id": cand["owner_id"], "due_date": cand["due_date"],
                     "validation": cand["validation"], "reasons": cand["reasons"], "expected": exp}
            if exp:
                check |= {"owner_ok": cand["owner_id"] == exp["owner"], "date_ok": cand["due_date"] == exp["due_date"],
                          "validation_ok": cand["validation"] == exp["validation"]}
                for key in ("owner_ok", "date_ok", "validation_ok"):
                    totals[key] += check[key]
            # The unsafe error: something marked PASS that a reviewer should have been warned about.
            check["false_pass"] = cand["validation"] == "PASS" and (exp is None or exp["validation"] == "FLAG")
            totals["false_pass"] += check["false_pass"]
            checks.append(check)
        totals["expected"] += len(case["expected_tasks"])
        totals["predicted"] += len(state["candidates"])
        totals["matched"] += sum(exp is not None for _, exp in pairs)
        for call in calls:
            totals["input_tokens"] += call["input_tokens"]
            totals["output_tokens"] += call["output_tokens"]
        rows.append({"id": case["id"], "kind": case["kind"], "run_id": state["run_id"], "status": state["status"],
                     "model_calls": calls, "candidates": checks,
                     "missed": [e for e in case["expected_tasks"] if e not in [x for _, x in pairs]]})
        flags = " ".join(f"{c['validation']}{'!' if c['false_pass'] else ''}" for c in checks)
        print(f"{case['id']:18} {state['run_id']}  expected={len(case['expected_tasks'])} "
              f"predicted={len(state['candidates'])} [{flags}]  status={state['status']}")

    m = totals["matched"] or 1
    summary = {
        "extractor": args.extractor, "model": next((c["model"] for r in rows for c in r["model_calls"]), None),
        "cases": len(rows), "task_recall": round(totals["matched"] / (totals["expected"] or 1), 2),
        "task_precision": round(totals["matched"] / (totals["predicted"] or 1), 2),
        "owner_accuracy": round(totals["owner_ok"] / m, 2), "due_date_accuracy": round(totals["date_ok"] / m, 2),
        "validation_agreement": round(totals["validation_ok"] / m, 2), "false_pass": totals["false_pass"],
        "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
        "avg_input_tokens_per_transcript": round(totals["input_tokens"] / len(rows)),
        "avg_output_tokens_per_transcript": round(totals["output_tokens"] / len(rows)),
    }
    print(json.dumps(summary, indent=2))
    out = Path("eval_results") / f"{args.extractor}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
