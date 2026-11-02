"""Mask secrets before anything is written to the database.

Agent logs and notes routinely contain tokens pasted into a terminal or .env files
read by a tool. Once they are in a file you copy to cloud storage, they are leaked.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str], str | None]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), None),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"), None),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"), None),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), None),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), None),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), None),
    ("bearer", re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._\-~+/=]{20,}"), r"\1[REDACTED:bearer]"),
    ("url_password", re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1[REDACTED]\2"),
    # key = value / key: value with a secret-looking name
    ("assignment", re.compile(
        r"(?i)(\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|client[_\-]?secret)"
        r"[\w.\-]*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"
    ), r"\1[REDACTED]"),
]


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for name, pat, repl in _PATTERNS:
        text = pat.sub(repl if repl is not None else f"[REDACTED:{name}]", text)
    return text
