"""One-off imports of chat exports: the Claude app (claude.ai) and ChatGPT.

Both services export a zip with conversations.json. Re-importing a newer export is
safe: message ids are stable, unchanged rows are skipped, new ones are added.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterator

from .collectors._jsonl import clip, first_line
from .db import upsert_items
from .i18n import _

MAX_ATTACHMENT_CHARS = 4000
MAX_TOOL_CHARS = 1500


def load_conversations(path: str | Path) -> list[dict]:
    """Accepts the export .zip, the unpacked folder, or conversations.json itself."""
    path = Path(path)
    if path.is_dir():
        files = sorted(path.rglob("conversations.json"), key=lambda p: len(p.parts))
        if not files:
            raise FileNotFoundError(_("No conversations.json in folder {path}", path=path))
        path = files[0]
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if n.endswith("conversations.json")), key=len)
            if not names:
                raise FileNotFoundError(_("No conversations.json in archive {name}", name=path.name))
            with z.open(names[0]) as f:
                data = json.load(io.TextIOWrapper(f, encoding="utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # some exports wrap the list
        data = data.get("conversations") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError(_("Unexpected conversations.json format"))
    return data


def detect_format(convs: list[dict]) -> str | None:
    for c in convs[:20]:
        if "chat_messages" in c:
            return "claude"
        if "mapping" in c:
            return "chatgpt"
    return None


# --- Claude app ----------------------------------------------------------------------------

def _claude_message_text(m: dict) -> str:
    parts: list[str] = []
    content = m.get("content")
    if isinstance(content, list) and content:
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text"):
                parts.append(b["text"])
            elif t == "tool_use":
                args = json.dumps(b.get("input", {}), ensure_ascii=False)
                parts.append(f"[tool {b.get('name')}] {clip(args, 600)}")
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                if c:
                    parts.append(f"[result] {clip(str(c), MAX_TOOL_CHARS)}")
            # thinking blocks are not kept, same as for Claude Code
    elif m.get("text"):
        parts.append(m["text"])
    for a in m.get("attachments") or []:
        name = a.get("file_name") or "attachment"
        extracted = a.get("extracted_content")
        parts.append(f"[attachment: {name}]" + (f"\n{clip(extracted, MAX_ATTACHMENT_CHARS)}" if extracted else ""))
    for f in m.get("files") or []:
        parts.append(f"[file: {f.get('file_name') or f.get('file_uuid') or '?'}]")
    return "\n".join(p for p in parts if p).strip()


def claude_items(convs: list[dict]) -> Iterator[dict]:
    for c in convs:
        cid = c.get("uuid")
        if not cid:
            continue
        thread = f"claude_app:{cid}"
        name = (c.get("name") or "").strip() or "Untitled"
        url = f"https://claude.ai/chat/{cid}"
        msgs = c.get("chat_messages") or []
        yield {
            "id": f"claude_app:conv:{cid}", "source": "claude_app", "kind": "conversation",
            "thread_id": thread, "title": name, "body": "\n".join(x for x in (name, c.get("summary") or "") if x),
            "url": url, "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
            "meta": {"messages": len(msgs), "project": (c.get("project") or {}).get("name")
                     if isinstance(c.get("project"), dict) else None},
        }
        for m in msgs:
            text = _claude_message_text(m)
            if not text or not m.get("uuid"):
                continue
            role = "user" if m.get("sender") == "human" else "assistant"
            yield {
                "id": f"claude_app:{m['uuid']}", "source": "claude_app", "kind": "message",
                "thread_id": thread, "title": f"{role}: {first_line(text)}", "body": text,
                "author": role, "url": url, "created_at": m.get("created_at"),
                "meta": {"conversation": name},
            }


# --- ChatGPT -------------------------------------------------------------------------------

def _chatgpt_text(msg: dict) -> str:
    content = msg.get("content") or {}
    ctype = content.get("content_type")
    if ctype in ("text", "multimodal_text"):
        out = []
        for p in content.get("parts") or []:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                out.append("[image]" if "image" in str(p.get("content_type", "")) else "")
        return "\n".join(x for x in out if x).strip()
    if ctype == "code":
        return f"```\n{content.get('text', '')}\n```"
    if ctype in ("execution_output", "tether_quote", "tether_browsing_display"):
        return clip(content.get("text") or content.get("result") or "", MAX_TOOL_CHARS)
    return ""


def _main_branch(c: dict) -> list[dict]:
    """ChatGPT stores a tree (edits/regenerations); follow the branch that ends at current_node."""
    mapping = c.get("mapping") or {}
    node_id = c.get("current_node")
    chain = []
    seen = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if node.get("message"):
            chain.append(node["message"])
        node_id = node.get("parent")
    if not chain:  # no current_node: take everything in creation order
        chain = [n["message"] for n in mapping.values() if n.get("message")]
        chain.sort(key=lambda m: m.get("create_time") or 0)
        return chain
    return list(reversed(chain))


def chatgpt_items(convs: list[dict]) -> Iterator[dict]:
    for c in convs:
        cid = c.get("conversation_id") or c.get("id")
        if not cid:
            continue
        thread = f"chatgpt:{cid}"
        title = (c.get("title") or "").strip() or "Untitled"
        url = f"https://chatgpt.com/c/{cid}"
        yield {
            "id": f"chatgpt:conv:{cid}", "source": "chatgpt", "kind": "conversation",
            "thread_id": thread, "title": title, "body": title, "url": url,
            "created_at": c.get("create_time"), "updated_at": c.get("update_time"),
            "meta": {"model": c.get("default_model_slug")},
        }
        for m in _main_branch(c):
            role = (m.get("author") or {}).get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            if (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                continue
            text = _chatgpt_text(m)
            if not text or not m.get("id"):
                continue
            kind = "tool_result" if role == "tool" else "message"
            yield {
                "id": f"chatgpt:{m['id']}", "source": "chatgpt", "kind": kind,
                "thread_id": thread, "title": f"{role}: {first_line(text)}", "body": text,
                "author": role, "url": url, "created_at": m.get("create_time"),
                "meta": {"conversation": title, "model": (m.get("metadata") or {}).get("model_slug")},
            }


def import_export(conn, path: str | Path, fmt: str = "auto") -> dict[str, Any]:
    convs = load_conversations(path)
    if fmt == "auto":
        fmt = detect_format(convs) or ""
    gen = {"claude": claude_items, "chatgpt": chatgpt_items}.get(fmt)
    if not gen:
        raise ValueError(_("Could not tell whose export this is. Specify it: brain import claude ... or brain import chatgpt ..."))
    written = total = 0
    batch: list[dict] = []
    for item in gen(convs):
        batch.append(item)
        total += 1
        if len(batch) >= 500:
            written += upsert_items(conn, batch)
            batch.clear()
    written += upsert_items(conn, batch)
    conn.commit()
    return {"format": fmt, "conversations": len(convs), "items": total, "written": written}
