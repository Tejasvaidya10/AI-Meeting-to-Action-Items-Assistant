"""Milestone 4 controls. Both run in code, outside the model prompt.

Control A - untrusted-content guard (prompt injection, tool misuse)
    The transcript and the model's output are untrusted. A candidate task is
    blocked from Jira when
      * its evidence quote does not appear in the transcript (the model made it up), or
      * its title, evidence, or Jira fields contain instruction-like text
        ("ignore previous instructions", "auto-approve", "system:", ...).
    A blocked candidate cannot be approved, not even with edits, and the final
    Jira call re-checks the exact fields before anything is sent.

Control B - sensitive-data redaction
    Before the transcript leaves our system (model call) or is stored (audit log,
    run snapshots, workflow.db, Jira), e-mail addresses, phone numbers, SSN-like
    numbers, card numbers, API keys, and spoken passwords are replaced with
    [REDACTED_<TYPE>]. Only counts per type are logged, never the values.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ---------------------------------------------------------------- Control B
SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SECRET", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})\b")),
    ("PASSWORD", re.compile(r"(?i)\b(password|passcode|pwd)(\s+(?:is|=|:)\s*|\s*[:=]\s*)\S+")),
    ("SSN", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("CARD", re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{1,4}(?!\d)")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return the text with sensitive values masked, and a count per type."""
    counts: dict[str, int] = {}
    for label, pattern in SENSITIVE_PATTERNS:
        def _mask(match: re.Match[str], label: str = label) -> str:
            counts[label] = counts.get(label, 0) + 1
            if label == "PASSWORD":  # keep the word "password", mask the value
                return f"{match.group(1)}{match.group(2)}[REDACTED_PASSWORD]"
            return f"[REDACTED_{label}]"
        text = pattern.sub(_mask, text)
    return text, counts


def find_sensitive(text: str) -> list[str]:
    """Types of sensitive value still present (used as a last check before Jira)."""
    return [label for label, pattern in SENSITIVE_PATTERNS
            if pattern.search(text) and not (label == "PASSWORD" and "[REDACTED_PASSWORD]" in text)]


# ---------------------------------------------------------------- Control A
INJECTION_PATTERNS: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules?|prompt|guidelines?|policy|policies)\b",
    r"\byou are now\b",
    r"(^|[\s.])(system|assistant|developer)\s*:",
    r"\bnew instructions?\b",
    r"\b(auto[- ]?approve|approve (it|this|them|all)\b.{0,20}\b(automatically|without review))",
    r"\b(skip|bypass|disable)\b.{0,20}\b(review|approval|validation|checks?)\b",
    r"\b(do not|don't|never)\s+(flag|review|validate)\b",
    r"\b(admin|administrator|owner)\s+(access|rights|permissions?|role)\b",
    r"https?://",
)]

_QUOTES = "\"'“”‘’"


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().strip(_QUOTES).strip().rstrip(".").casefold()


def injection_hits(text: str | None) -> list[str]:
    """The instruction-like phrases found in a piece of untrusted text."""
    return [m.group(0).strip() for p in INJECTION_PATTERNS for m in [p.search(text or "")] if m]


def suspicious_sentences(transcript: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", transcript) if injection_hits(s)]


def check_candidate(candidate: dict[str, Any], transcript: str) -> list[str]:
    """Policy violations that block this candidate from ever reaching Jira."""
    blocks = []
    evidence = _norm(candidate.get("evidence"))
    if not evidence or evidence not in _norm(transcript):
        blocks.append("evidence not found in transcript")
    for field in ("title", "evidence", "owner_text", "due_date_text"):
        hits = injection_hits(candidate.get(field))
        if hits:
            blocks.append(f"instruction-like text in {field}: {hits[0]!r}")
    return blocks


def check_outbound(fields: dict[str, Any]) -> list[str]:
    """Last check on the exact fields about to be sent to Jira."""
    problems = []
    for name, value in fields.items():
        if not isinstance(value, str):
            continue
        # The description quotes the meeting evidence; check it the same way.
        hits = injection_hits(value)
        if hits:
            problems.append(f"instruction-like text in {name}: {hits[0]!r}")
        leaked = find_sensitive(value)
        if leaked:
            problems.append(f"unredacted {', '.join(leaked)} in {name}")
    return problems


def guard_candidates(candidates: Iterable[dict[str, Any]], transcript: str) -> int:
    """Mark each candidate with its policy blocks (in place). Returns how many were blocked."""
    blocked = 0
    for cand in candidates:
        blocks = check_candidate(cand, transcript)
        cand["policy_blocks"] = blocks
        if blocks:
            blocked += 1
            cand["validation"] = "FLAG"
            cand["reasons"] = list(cand.get("reasons") or []) + [f"blocked by policy: {b}" for b in blocks]
    return blocked
