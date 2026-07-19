"""Shared secret-pattern gate for public scans and private feedback text."""

from __future__ import annotations

import re
from typing import Pattern


SENSITIVE_PATTERNS: tuple[tuple[str, Pattern[str]], ...] = (
    ("OpenAI-style secret", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("Stripe live secret", re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b")),
    (
        "Bearer credential",
        re.compile(
            r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+"
            r"[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
    (
        "private key material",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+(?: [A-Z0-9]+)* )?PRIVATE KEY-----"),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?i)(?:api[_-]?key|access[_-]?token|token|secret|password)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{24,}"
        ),
    ),
)


def sensitive_text_label(text: str) -> str | None:
    """Return only a classification label; never return or interpolate a secret."""
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            return label
    return None
