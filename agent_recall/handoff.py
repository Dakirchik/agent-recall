"""Handoff: a compact Markdown briefing to continue work in another AI.

Built purely from the database (no LLM, no API keys): what the task was, what was
decided, which files and commands were touched, where things stopped. Paste it into
any chat, or let an agent fetch it over MCP.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import query
from .i18n import _

CHAT_SOURCES = ("claude_code", "codex", "claude_app", "chatgpt")
UNTITLED = {"", "Untitled", "Без названия"}
_FILE_KEYS = ("file_path", "path", "notebook_path", "filePath")
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: (.+)")
# tool-call lines as stored by the collectors (Russian markers come from databases made by early versions)
_TOOL_RE = re.compile(r"^\[(?:tool|инструмент) ([^\]]+)\] (.*)$", re.S)
_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "str_replace_editor", "create_file"}


def source_name(source: str) -> str:
    return {"claude_code": "Claude Code", "codex": "Codex CLI", "claude_app": _("Claude app"),
            "chatgpt": "ChatGPT", "obsidian": "Obsidian", "github": "GitHub"}.get(source, source)


def _source_in(source: str) -> str:
    """Source name as it reads after "in" (Russian needs a different case)."""
    return _("the Claude app") if source == "claude_app" else source_name(source)


def how_found(code: str, target=None) -> str:
    q = " ".join(target) if isinstance(target, (list, tuple)) else (target or "")
    return {"id": _("by session id"), "item": _("by item id"),
            "search": _("by search “{q}”", q=q)}.get(code, code)


# Context the IDE / agent injects into "user" messages: useless for a human or another AI.
_INJECTED_RE = re.compile(
    r"<(ide_opened_file|ide_selection|system-reminder|task-notification|command-message|command-name|"
    r"command-args|local-command-stdout|user-prompt-submit-hook)>.*?</\1>", re.S)


def clean(text: str | None) -> str:
    text = _INJECTED_RE.sub("", text or "")
    text = re.sub(r"</?pasted_content[^>]*>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + " …"


def _day(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


class ThreadNotFound(LookupError):
    pass


# --- choosing what to hand off -----------------------------------------------------------

def find_threads(conn: sqlite3.Connection, text: str, limit: int = 5) -> list[dict]:
    """Chat sessions matching ALL words of the query, best first (no loose OR matching —
    a handoff of the wrong session is worse than none)."""
    out, seen = [], set()
    for r in query.search(conn, text, limit=200, fallback_or=False):
        if r["source"] in CHAT_SOURCES and r["thread_id"] and r["thread_id"] not in seen:
            seen.add(r["thread_id"])
            out.append({"thread_id": r["thread_id"], "source": r["source"], "created_at": r["created_at"],
                        "title": thread_title(conn, r["thread_id"])})
            if len(out) >= limit:
                break
    return out


def thread_title(conn: sqlite3.Connection, thread_id: str) -> str:
    row = conn.execute("""SELECT title FROM items WHERE thread_id = ? AND kind IN ('session_summary', 'conversation')
                          LIMIT 1""", (thread_id,)).fetchone()
    if row and row[0] and row[0] not in UNTITLED:
        return row[0]
    for (body,) in conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                   AND author IN ('user', 'human') ORDER BY created_at LIMIT 5""", (thread_id,)):
        t = clean(body)
        if t:
            return _clip(t.splitlines()[0], 90)
    return thread_id


def resolve_thread(conn: sqlite3.Connection, target: str) -> tuple[str, str]:
    """target = thread id | item id | free-text query.
    Returns (thread_id, how): how is "id", "item" or "search" (see how_found)."""
    if conn.execute("SELECT 1 FROM items WHERE thread_id = ? LIMIT 1", (target,)).fetchone():
        return target, "id"
    row = conn.execute("SELECT thread_id FROM items WHERE id = ?", (target,)).fetchone()
    if row and row[0]:
        return row[0], "item"
    found = find_threads(conn, target, limit=1)
    if found:
        return found[0]["thread_id"], "search"
    raise ThreadNotFound(_(
        "No session contains all the words “{q}”.\n"
        "Claude app and ChatGPT chats appear only after brain import (data export);\n"
        "see what is in the database: brain search <words> or brain recent --source claude_app", q=target))


def last_thread(conn: sqlite3.Connection, source: str | None = None) -> str:
    sources = (source,) if source else CHAT_SOURCES
    row = conn.execute(
        f"""SELECT thread_id FROM items
            WHERE source IN ({','.join('?' * len(sources))}) AND thread_id IS NOT NULL
              AND kind IN ('message', 'tool_call')
            ORDER BY created_at DESC LIMIT 1""", sources).fetchone()
    if not row:
        raise ThreadNotFound(_("There are no sessions in the database yet."))
    return row[0]


# --- one session ---------------------------------------------------------------------------

def _touched(rows: list[sqlite3.Row]) -> tuple[list[str], list[str], list[str]]:
    """(edited files, read files, commands) from tool calls."""
    edited, read, commands = [], [], []

    def add(lst, v):
        v = str(v).strip()
        if v and v not in lst:
            lst.append(v)

    for r in rows:
        body = r["body"] or ""
        if r["kind"] not in ("tool_call", "message") or ("[tool " not in body and "[инструмент " not in body):
            continue
        for line in body.splitlines():
            m = _TOOL_RE.match(line.strip())
            if not m:
                continue
            tool, raw = m.group(1).strip(), m.group(2).strip().rstrip("…").strip()
            for f in _PATCH_FILE_RE.findall(raw):
                add(edited, f)
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = None
            if isinstance(args, dict):
                f = next((args[k] for k in _FILE_KEYS if args.get(k)), None)
                if f:
                    add(edited if tool.lower() in _EDIT_TOOLS else read, f)
                cmd = args.get("command") or args.get("cmd")
                if isinstance(cmd, list):  # codex: ["bash", "-lc", "npm test"]
                    cmd = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-lc", "-c", "/c", "-Command") else " ".join(cmd)
                if isinstance(cmd, str) and cmd.strip() and "*** Begin Patch" not in cmd:
                    add(commands, _clip(cmd.splitlines()[0], 160))
    read = [f for f in read if f not in edited]
    return edited, read, commands


def thread_handoff(conn: sqlite3.Connection, thread_id: str, max_chars: int = 14000) -> str:
    rows = conn.execute(
        """SELECT id, source, kind, author, title, body, cwd, created_at, meta FROM items
           WHERE thread_id = ? ORDER BY created_at, rid""", (thread_id,)).fetchall()
    if not rows:
        raise ThreadNotFound(_("Session {id} not found", id=thread_id))
    source = rows[0]["source"]
    msgs = [r for r in rows if r["kind"] == "message" and r["author"] in ("user", "assistant", "human")]
    users = [r for r in msgs if r["author"] in ("user", "human")]
    summary = next((r["title"] for r in rows if r["kind"] in ("session_summary", "conversation")), None)
    cwd = next((r["cwd"] for r in rows if r["cwd"]), None)
    branch = None
    for r in rows:
        try:
            branch = json.loads(r["meta"] or "{}").get("git_branch")
        except json.JSONDecodeError:
            pass
        if branch:
            break
    projects = [p[0] for p in conn.execute(
        """SELECT DISTINCT ip.project_id FROM item_projects ip JOIN items i ON i.id = ip.item_id
           WHERE i.thread_id = ?""", (thread_id,))]
    edited, read, commands = _touched(rows)
    dated = [r["created_at"] for r in msgs if r["created_at"]]

    def spoken(r) -> str:
        """Message text without tool-call lines — those are summarised under "Files and commands"."""
        lines = [l for l in clean(r["body"]).splitlines() if not _TOOL_RE.match(l.strip())]
        return "\n".join(lines).strip()

    talk = [r for r in msgs if spoken(r)]

    head = ["# " + _("Context to continue the work"),
            "",
            _("This is a summary of my session in {where}. Read it, briefly restate how you understand "
              "the task and where we stopped, then continue from there. If something is missing, ask "
              "instead of guessing.", where=_source_in(source)),
            "",
            "## " + _("Session")]
    if summary and summary not in UNTITLED:
        head.append("- " + _("Topic: {t}", t=summary))
    if dated:
        head.append("- " + _("Period: {a} — {b} UTC, messages: {n}", a=_day(min(dated)), b=_day(max(dated)), n=len(talk)))
    if cwd:
        head.append("- " + _("Working directory: `{cwd}`", cwd=cwd) + (", " + _("branch `{b}`", b=branch) if branch else ""))
    if projects:
        head.append("- " + _("Project: {p}", p=", ".join(projects)))

    # sections: (key, title, lines); keys drive the trimming below
    sections: list[tuple[str, str, list[str]]] = []
    first_user = next((u for u in users if spoken(u)), None)
    if first_user:
        sections.append(("start", "## " + _("How it started"), [_clip(spoken(first_user), 2500)]))
    if edited or read or commands:
        lines = []
        if edited:
            lines.append(_("Files changed:") + " " + ", ".join(f"`{f}`" for f in edited[:30]))
        if read:
            lines.append(_("Files read:") + " " + ", ".join(f"`{f}`" for f in read[:20]))
        if commands:
            lines.append(_("Commands:"))
            lines += [f"- `{c}`" for c in commands[-15:]]
        sections.append(("files", "## " + _("Files and commands"), lines))

    tail_rows = talk[-6:]
    tail_ids = {r["id"] for r in tail_rows}
    middle_users = [u for u in users if u is not first_user and u["id"] not in tail_ids and spoken(u)]
    if middle_users:
        sections.append(("middle", "## " + _("My follow-up requests"),
                         [f"- {_clip(spoken(u), 400)}" for u in middle_users]))

    tail: list[str] = []
    for r in tail_rows:
        who = _("Me") if r["author"] in ("user", "human") else _("Assistant")
        tail.append(f"**{who}** ({_day(r['created_at'])}):\n{_clip(spoken(r), 1800)}")
    if tail:
        sections.append(("tail", "## " + _("Where we stopped (last messages)"), tail))
    footer = ["", "_" + _("Full session: `brain thread {id}`", id=thread_id) + "_"]

    def render(secs):
        out = list(head)
        for _key, title, body in secs:
            out += ["", title, *body]
        return "\n".join(out + footer)

    text = render(sections)
    # Over budget: drop the oldest intermediate requests first (the start and the end matter most),
    # then the oldest of the final replies, then shorten everything.
    mid = next((b for k, _t, b in sections if k == "middle"), None)
    last = next((b for k, _t, b in sections if k == "tail"), None)
    dropped = 0
    while len(text) > max_chars:
        if mid and len(mid) > 1:
            if mid[0].startswith("- …"):
                mid.pop(1)
            else:
                mid.pop(0)
            dropped += 1
            mid[:] = [x for x in mid if not x.startswith("- …")]
            mid.insert(0, "- … " + _("({n} earlier requests omitted)", n=dropped))
        elif last and len(last) > 2:
            last.pop(0)
        else:
            shorter = [(k, t, [_clip(x, max(300, len(x) // 2)) for x in b]) for k, t, b in sections]
            if len(render(shorter)) >= len(text):
                break
            sections = shorter
        text = render(sections)
    return text


# --- a whole project -----------------------------------------------------------------------

def project_handoff(conn: sqlite3.Connection, project: str, days: int = 14, max_chars: int = 16000) -> str:
    p = conn.execute("SELECT id, name FROM projects WHERE id = ?", (project,)).fetchone()
    if not p:
        raise ThreadNotFound(_("Project “{p}” not found (brain projects)", p=project))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    in_project = "id IN (SELECT item_id FROM item_projects WHERE project_id = ?)"
    out = ["# " + _("Project context: “{name}”", name=p["name"]),
           "",
           _("This is a summary from my knowledge base: decisions, notes and recent work sessions on the "
             "project. Use it as context; if something is missing, ask."),
           ""]

    adrs = conn.execute(f"""SELECT title, body, created_at FROM items WHERE {in_project}
                            AND source = 'obsidian' AND kind = 'adr' AND deleted_at IS NULL
                            ORDER BY created_at DESC LIMIT 10""", (project,)).fetchall()
    if adrs:
        out.append("## " + _("Decisions (ADR)"))
        for a in adrs:
            out += [f"### {a['title']}", _clip(a["body"], 1200), ""]

    notes = conn.execute(f"""SELECT title, body, updated_at FROM items WHERE {in_project}
                             AND source = 'obsidian' AND kind NOT IN ('adr', 'image', 'pdf', 'attachment')
                             AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 6""", (project,)).fetchall()
    if notes:
        out.append("## " + _("Recent notes"))
        for n in notes:
            out += [f"### {n['title']} ({_day(n['updated_at'])[:10]})", _clip(n["body"], 800), ""]

    threads = conn.execute(f"""SELECT thread_id, source, MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                                      COUNT(*) AS n
                               FROM items WHERE {in_project} AND source IN ({','.join('?' * len(CHAT_SOURCES))})
                                 AND kind = 'message' AND created_at >= ?
                               GROUP BY thread_id ORDER BY last_at DESC LIMIT 8""",
                           (project, *CHAT_SOURCES, since)).fetchall()
    if threads:
        out.append("## " + _("Work sessions, last {d} days", d=days))
        for t in threads:
            first = conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                    AND author IN ('user', 'human') ORDER BY created_at LIMIT 1""",
                                 (t["thread_id"],)).fetchone()
            last = conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                   AND author = 'assistant' ORDER BY created_at DESC LIMIT 1""",
                                (t["thread_id"],)).fetchone()
            out.append(f"### {source_name(t['source'])}, {_day(t['first_at'])} — {_day(t['last_at'])}, "
                       + _("{n} msgs", n=t["n"]))
            if first:
                out.append(_("Task: {t}", t=_clip(clean(first[0]), 500)))
            if last:
                out.append(_("Outcome: {t}", t=_clip(clean(last[0]), 700)))
            out.append("_" + _("Details: `brain handoff {id}`", id=t["thread_id"]) + "_\n")

    commits = conn.execute(f"""SELECT title, repo, created_at FROM items WHERE {in_project}
                               AND source = 'github' AND created_at >= ?
                               ORDER BY created_at DESC LIMIT 20""", (project, since)).fetchall()
    if commits:
        out.append("## " + _("GitHub, last {d} days", d=days))
        out += [f"- {_day(c['created_at'])[:10]} {c['repo']}: {c['title']}" for c in commits]

    if len(out) <= 4:
        out.append("_" + _("Nothing on this project in the last {d} days. Increase --days or check the project rules.", d=days) + "_")
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "\n\n…" + _("(cut to the size limit)")


# --- clipboard -----------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    import shutil
    import subprocess
    import sys
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u32.SetClipboardData.restype = wintypes.HANDLE
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not u32.OpenClipboard(None):
            return False
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            ptr = k32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            k32.GlobalUnlock(h)
            return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            u32.CloseClipboard()
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, input=text.encode("utf-8")).returncode == 0
    return False
