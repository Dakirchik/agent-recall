"""Codex CLI rollouts (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl) -> items.

Handles both the current format ({"type": "...", "payload": {...}}) and the early
one where response items are written at the top level.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import expand
from ..db import get_state, set_state, upsert_items
from ._jsonl import clip, first_line, iter_files, read_new_lines

# Injected context that Codex sends as "user" messages — not something you wrote.
_INJECTED_PREFIXES = ("<environment_context", "<user_instructions", "# agents.md", "<permissions",
                      "<user_shell_command", "<turn_aborted")


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def sync(conn, cfg: dict, log) -> int:
    ccfg = cfg["codex"]
    max_tool = int(ccfg.get("max_tool_chars", 1500))
    state: dict[str, dict] = get_state(conn, "codex:files", {}) or {}
    written = files = 0
    for d in ccfg.get("dirs", []):
        root = expand(d)
        if not root.exists():
            continue
        for path in iter_files(root, "rollout-*.jsonl"):
            key = path.as_posix()
            st = state.get(key) or {"offset": 0, "session": None, "cwd": None}
            lines, new_off, restarted = read_new_lines(path, int(st.get("offset", 0)))
            items: list[dict] = []
            for off, obj in lines:
                typ, payload = obj.get("type"), obj.get("payload")
                if payload is None:  # early format: the object is the payload
                    payload, typ = obj, ("session_meta" if "instructions" in obj and "id" in obj else "response_item")
                if typ == "session_meta":
                    st["session"] = payload.get("id") or st.get("session")
                    st["cwd"] = payload.get("cwd") or st.get("cwd")
                    continue
                if typ == "turn_context" and isinstance(payload, dict) and payload.get("cwd"):
                    st["cwd"] = payload["cwd"]
                    continue
                if typ != "response_item" or not isinstance(payload, dict):
                    continue  # event_msg duplicates response items
                session = st.get("session") or path.stem
                ptype = payload.get("type")
                role, kind = None, None
                if ptype == "message":
                    role = payload.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    text = _text_from_content(payload.get("content")).strip()
                    if not text or text.lower().startswith(_INJECTED_PREFIXES):
                        continue
                    kind = "message"
                elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                    role, kind = "assistant", "tool_call"
                    args = payload.get("arguments") or payload.get("input") or payload.get("action") or ""
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    text = f"[инструмент {payload.get('name') or ptype}] {clip(args, 600)}"
                elif ptype in ("function_call_output", "custom_tool_call_output"):
                    role, kind = "tool", "tool_result"
                    out = payload.get("output")
                    if isinstance(out, dict):
                        out = out.get("content") or json.dumps(out, ensure_ascii=False)
                    text = f"[результат] {clip(out, max_tool)}"
                else:
                    continue
                items.append({
                    "id": f"codex:{session}:{off}",
                    "source": "codex",
                    "kind": kind,
                    "thread_id": session,
                    "title": f"{role}: {first_line(text)}",
                    "body": text,
                    "author": role,
                    "path": key,
                    "cwd": st.get("cwd"),
                    "created_at": obj.get("timestamp"),
                    "meta": {"file": path.name},
                })
            if items:
                files += 1
                written += upsert_items(conn, items)
            st["offset"] = new_off
            state[key] = st
    set_state(conn, "codex:files", state)
    conn.commit()
    log.info("codex: файлов с новыми строками %d, записано %d", files, written)
    return written
