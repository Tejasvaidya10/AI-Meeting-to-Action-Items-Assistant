"""Deterministic stand-in for the hosted model (no API key needed).

Recognises two sentence shapes, which is enough for the synthetic transcripts:
  "<Name> will <task> [by|on <day>]."   -> owner and optional due date
  "Someone/We should <task> [soon]." -> no owner, no due date (FLAG)
"""
from __future__ import annotations

import re

from meeting_assistant import ActionItem, Extraction

WILL = re.compile(r"^(?P<owner>[A-Z]\w+) will (?P<task>.+?)(?: (?:by|on) (?P<due>[\w-]+))?$")
SHOULD = re.compile(r"^(?:someone|we) should (?P<task>.+?)(?: soon)?$", re.IGNORECASE)


def mock_extractor(transcript: str) -> Extraction:
    tasks = []
    for sentence in (s.strip() for s in transcript.split(".")):
        match = WILL.match(sentence) or SHOULD.match(sentence)
        if not match:
            continue
        groups = match.groupdict()
        task = groups["task"]
        tasks.append(ActionItem(
            title=task[0].upper() + task[1:],
            owner_text=groups.get("owner"),
            due_date_text=groups.get("due"),
            evidence=sentence + ".",
        ))
    return Extraction(summary=f"{len(tasks)} action item(s) found", decisions=[], tasks=tasks)
