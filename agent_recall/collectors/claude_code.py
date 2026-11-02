"""Claude Code sessions (~/.claude/projects/**/*.jsonl) -> one item per message."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import expand
from ..db import get_state, set_state, upsert_items
from ._jsonl import clip, first_line, iter_files, read_new_lines


def _content_text(content, max_tool: int) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "tool_use":
            args = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[инструмент {block.get('name')}] {clip(args, 600)}")
        elif t == "tool_result":
            c = block.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            parts.append(f"[результат] {clip(c, max_tool)}")
        # thinking / images are skipped
    return "\n".join(p for p in parts if p)


def line_to_item(obj: dict, path: Path, line_off: int, max_tool: int) -> dict | None:
    typ = obj.get("type")
    session = obj.get("sessionId") or path.stem
    if typ == "summary" and obj.get("summary"):
        return {
            "id": f"claude_code:summary:{obj.get('leafUuid') or session}",
            "source": "claude_code", "kind": "session_summary", "thread_id": session,
            "title": obj["summary"], "body": obj["summary"], "author": "system",
            "path": path.as_posix(), "created_at": path.stat().st_mtime,
            "meta": {"file": path.name},
        }
    if typ not in ("user", "assistant"):
        return None
    msg = obj.get("message") or {}
    text = _content_text(msg.get("content"), max_tool).strip()
    if not text:
        return None
    role = msg.get("role") or typ
    is_tool_result = text.startswith("[результат]")
    kind = "tool_result" if is_tool_result else "message"
    return {
        "id": f"claude_code:{obj.get('uuid') or f'{session}:{line_off}'}",
        "source": "claude_code",
        "kind": kind,
        "thread_id": session,
        "title": f"{role}: {first_line(text)}",
        "body": text,
        "author": role,
        "path": path.as_posix(),
        "cwd": obj.get("cwd"),
        "created_at": obj.get("timestamp"),
        "meta": {k: v for k, v in {
            "git_branch": obj.get("gitBranch"),
            "model": msg.get("model"),
            "sidechain": obj.get("isSidechain") or None,
            "version": obj.get("version"),
        }.items() if v},
    }


def sync(conn, cfg: dict, log) -> int:
    ccfg = cfg["claude_code"]
    root = expand(ccfg["dir"])
    if not root.exists():
        log.info("claude_code: папка не найдена: %s", root)
        return 0
    max_tool = int(ccfg.get("max_tool_chars", 1500))
    offsets: dict[str, int] = get_state(conn, "claude_code:offsets", {}) or {}
    written = files = 0
    for path in iter_files(root, "*.jsonl"):
        key = path.as_posix()
        lines, new_off, restarted = read_new_lines(path, int(offsets.get(key, 0)))
        if restarted:
            log.info("claude_code: файл перезаписан, читаю заново: %s", path.name)
        if lines:
            files += 1
            items = [it for off, obj in lines if (it := line_to_item(obj, path, off, max_tool))]
            written += upsert_items(conn, items)
        offsets[key] = new_off
    set_state(conn, "claude_code:offsets", offsets)
    conn.commit()
    log.info("claude_code: файлов с новыми строками %d, записано %d", files, written)
    return written
