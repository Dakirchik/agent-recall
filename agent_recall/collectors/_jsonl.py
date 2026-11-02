"""Incremental reading of append-only JSONL logs (Claude Code, Codex).

For each file we remember the byte offset of the last complete line. A file that
got shorter than the remembered offset was rewritten (compaction) and is re-read
from the start; ids are offset/uuid based, so re-reading only refreshes rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def read_new_lines(path: Path, offset: int) -> tuple[list[tuple[int, dict]], int, bool]:
    """Return ([(line_offset, obj), ...], new_offset, restarted)."""
    size = path.stat().st_size
    restarted = False
    if size < offset:
        offset, restarted = 0, True
    if size == offset:
        return [], offset, restarted
    out: list[tuple[int, dict]] = []
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:  # no complete line yet (file being written)
        return [], offset, restarted
    pos = 0
    chunk = data[: end + 1]
    for raw in chunk.split(b"\n")[:-1]:
        line_off = offset + pos
        pos += len(raw) + 1
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append((line_off, obj))
    return out, offset + end + 1, restarted


def iter_files(root: Path, pattern: str) -> Iterator[Path]:
    if root.exists():
        yield from sorted(root.rglob(pattern))


def clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = str(text)
    if limit and len(text) > limit:
        return text[:limit] + f"\n…[truncated, {len(text)} chars total]"
    return text


def first_line(text: str, n: int = 100) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:n]
    return ""
