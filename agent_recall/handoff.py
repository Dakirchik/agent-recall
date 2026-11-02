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

CHAT_SOURCES = ("claude_code", "codex", "claude_app", "chatgpt")
SOURCE_NAMES = {
    "claude_code": "Claude Code", "codex": "Codex CLI", "claude_app": "приложение Claude",
    "chatgpt": "ChatGPT", "obsidian": "Obsidian", "github": "GitHub",
}
SOURCE_IN = {"claude_code": "Claude Code", "codex": "Codex CLI", "claude_app": "приложении Claude",
             "chatgpt": "ChatGPT"}
_FILE_KEYS = ("file_path", "path", "notebook_path", "filePath")
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: (.+)")
_TOOL_RE = re.compile(r"^\[инструмент ([^\]]+)\] (.*)$", re.S)
_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "str_replace_editor", "create_file"}


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
    if row and row[0] and row[0] != "Без названия":
        return row[0]
    for (body,) in conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                   AND author IN ('user', 'human') ORDER BY created_at LIMIT 5""", (thread_id,)):
        t = clean(body)
        if t:
            return _clip(t.splitlines()[0], 90)
    return thread_id


def resolve_thread(conn: sqlite3.Connection, target: str) -> tuple[str, str]:
    """target = thread id | item id | free-text query. Returns (thread_id, how it was found)."""
    if conn.execute("SELECT 1 FROM items WHERE thread_id = ? LIMIT 1", (target,)).fetchone():
        return target, "по id сессии"
    row = conn.execute("SELECT thread_id FROM items WHERE id = ?", (target,)).fetchone()
    if row and row[0]:
        return row[0], "по id записи"
    found = find_threads(conn, target, limit=1)
    if found:
        return found[0]["thread_id"], f"поиском по «{target}»"
    raise ThreadNotFound(
        f"Не нашёл сессию, где есть все слова «{target}».\n"
        "Чаты из приложения Claude и ChatGPT появляются в базе только после brain import (экспорт);\n"
        "проверить, что есть в базе: brain search <слова> или brain recent --source claude_app")


def last_thread(conn: sqlite3.Connection, source: str | None = None) -> str:
    sources = (source,) if source else CHAT_SOURCES
    row = conn.execute(
        f"""SELECT thread_id FROM items
            WHERE source IN ({','.join('?' * len(sources))}) AND thread_id IS NOT NULL
              AND kind IN ('message', 'tool_call')
            ORDER BY created_at DESC LIMIT 1""", sources).fetchone()
    if not row:
        raise ThreadNotFound("Сессий ещё нет в базе.")
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
        if r["kind"] not in ("tool_call", "message") or "[инструмент " not in (r["body"] or ""):
            continue
        for line in (r["body"] or "").splitlines():
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
        raise ThreadNotFound(f"Сессия {thread_id} не найдена")
    source = rows[0]["source"]
    msgs = [r for r in rows if r["kind"] == "message" and r["author"] in ("user", "assistant", "human")]
    users = [r for r in msgs if r["author"] in ("user", "human")]
    assistants = [r for r in msgs if r["author"] == "assistant"]
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
        """Message text without tool-call lines — those are summarised in «Файлы и команды»."""
        lines = [l for l in clean(r["body"]).splitlines() if not _TOOL_RE.match(l.strip())]
        return "\n".join(lines).strip()

    talk = [r for r in msgs if spoken(r)]

    head = [f"# Контекст для продолжения работы",
            "",
            f"Это выжимка моей сессии в {SOURCE_IN.get(source, source)}. Прочитай её, коротко "
            f"перескажи, как ты понял задачу и где мы остановились, и продолжай с этого места. "
            f"Если чего-то не хватает — спроси, не додумывай.",
            "",
            "## Сессия"]
    if summary and summary != "Без названия":
        head.append(f"- Тема: {summary}")
    if dated:
        head.append(f"- Период: {_day(min(dated))} — {_day(max(dated))} UTC, сообщений: {len(talk)}")
    if cwd:
        head.append(f"- Рабочая папка: `{cwd}`" + (f", ветка `{branch}`" if branch else ""))
    if projects:
        head.append(f"- Проект: {', '.join(projects)}")

    sections: list[tuple[str, list[str]]] = []
    first_user = next((u for u in users if spoken(u)), None)
    if first_user:
        sections.append(("## С чего началось", [_clip(spoken(first_user), 2500)]))
    if edited or read or commands:
        lines = []
        if edited:
            lines.append("Изменялись файлы: " + ", ".join(f"`{f}`" for f in edited[:30]))
        if read:
            lines.append("Читались: " + ", ".join(f"`{f}`" for f in read[:20]))
        if commands:
            lines.append("Команды:")
            lines += [f"- `{c}`" for c in commands[-15:]]
        sections.append(("## Файлы и команды", lines))

    tail_rows = talk[-6:]
    tail_ids = {r["id"] for r in tail_rows}
    middle_users = [u for u in users if u is not first_user and u["id"] not in tail_ids and spoken(u)]
    if middle_users:
        sections.append(("## Мои следующие запросы", [f"- {_clip(spoken(u), 400)}" for u in middle_users]))

    tail: list[str] = []
    for r in tail_rows:
        who = "Я" if r["author"] in ("user", "human") else "Ассистент"
        tail.append(f"**{who}** ({_day(r['created_at'])}):\n{_clip(spoken(r), 1800)}")
    if tail:
        sections.append(("## Где остановились (последние реплики)", tail))
    footer = ["", f"_Полная сессия: `brain thread {thread_id}`_"]

    def render(secs):
        out = list(head)
        for title, body in secs:
            out += ["", title, *body]
        return "\n".join(out + footer)

    text = render(sections)
    # Over budget: drop the oldest intermediate requests first (the start and the end matter most),
    # then the oldest of the final replies, then shorten everything.
    mid = next((b for t, b in sections if t.startswith("## Мои следующие")), None)
    last = next((b for t, b in sections if t.startswith("## Где остановились")), None)
    dropped = 0
    while len(text) > max_chars:
        if mid and len(mid) > 1:
            if mid[0].startswith("- …"):
                mid.pop(1)
            else:
                mid.pop(0)
            dropped += 1
            mid[:] = [x for x in mid if not x.startswith("- …")]
            mid.insert(0, f"- … (ещё {dropped} более ранних запросов пропущено)")
        elif last and len(last) > 2:
            last.pop(0)
        else:
            shorter = [(t, [_clip(x, max(300, len(x) // 2)) for x in b]) for t, b in sections]
            if len(render(shorter)) >= len(text):
                break
            sections = shorter
        text = render(sections)
    return text


# --- a whole project -----------------------------------------------------------------------

def project_handoff(conn: sqlite3.Connection, project: str, days: int = 14, max_chars: int = 16000) -> str:
    p = conn.execute("SELECT id, name FROM projects WHERE id = ?", (project,)).fetchone()
    if not p:
        raise ThreadNotFound(f"Проект «{project}» не найден (brain projects)")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    in_project = "id IN (SELECT item_id FROM item_projects WHERE project_id = ?)"
    out = [f"# Контекст проекта «{p['name']}»",
           "",
           "Это выжимка из моей базы знаний: решения, заметки и последние рабочие сессии по проекту. "
           "Используй её как контекст; если чего-то не хватает — спроси.",
           ""]

    adrs = conn.execute(f"""SELECT title, body, created_at FROM items WHERE {in_project}
                            AND source = 'obsidian' AND kind = 'adr' AND deleted_at IS NULL
                            ORDER BY created_at DESC LIMIT 10""", (project,)).fetchall()
    if adrs:
        out.append("## Принятые решения (ADR)")
        for a in adrs:
            out += [f"### {a['title']}", _clip(a["body"], 1200), ""]

    notes = conn.execute(f"""SELECT title, body, updated_at FROM items WHERE {in_project}
                             AND source = 'obsidian' AND kind NOT IN ('adr', 'image', 'pdf', 'attachment')
                             AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 6""", (project,)).fetchall()
    if notes:
        out.append("## Последние заметки")
        for n in notes:
            out += [f"### {n['title']} ({_day(n['updated_at'])[:10]})", _clip(n["body"], 800), ""]

    threads = conn.execute(f"""SELECT thread_id, source, MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                                      COUNT(*) AS n
                               FROM items WHERE {in_project} AND source IN ({','.join('?' * len(CHAT_SOURCES))})
                                 AND kind = 'message' AND created_at >= ?
                               GROUP BY thread_id ORDER BY last_at DESC LIMIT 8""",
                           (project, *CHAT_SOURCES, since)).fetchall()
    if threads:
        out.append(f"## Рабочие сессии за {days} дн.")
        for t in threads:
            first = conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                    AND author IN ('user', 'human') ORDER BY created_at LIMIT 1""",
                                 (t["thread_id"],)).fetchone()
            last = conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                   AND author = 'assistant' ORDER BY created_at DESC LIMIT 1""",
                                (t["thread_id"],)).fetchone()
            out.append(f"### {SOURCE_NAMES.get(t['source'], t['source'])}, {_day(t['first_at'])} — "
                       f"{_day(t['last_at'])}, {t['n']} сообщ.")
            if first:
                out.append(f"Задача: {_clip(first[0], 500)}")
            if last:
                out.append(f"Итог: {_clip(last[0], 700)}")
            out.append(f"_Подробно: `brain handoff {t['thread_id']}`_\n")

    commits = conn.execute(f"""SELECT title, repo, created_at FROM items WHERE {in_project}
                               AND source = 'github' AND created_at >= ?
                               ORDER BY created_at DESC LIMIT 20""", (project, since)).fetchall()
    if commits:
        out.append(f"## GitHub за {days} дн.")
        out += [f"- {_day(c['created_at'])[:10]} {c['repo']}: {c['title']}" for c in commits]

    if len(out) <= 4:
        out.append(f"_За последние {days} дн. по проекту ничего нет. Увеличьте --days или проверьте правила проекта._")
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "\n\n…(обрезано по лимиту)"


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
