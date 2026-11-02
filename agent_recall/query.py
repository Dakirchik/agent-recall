"""Read side: search, item/thread lookup, project timeline. Shared by CLI and MCP."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

_ADVANCED = re.compile(r'"|\b(?:AND|OR|NOT|NEAR)\b|\*|\^|:')
_WORD = re.compile(r"\w+", re.U)


def fts_query(q: str, mode: str = "and") -> str:
    """Turn free text into a safe FTS5 query. Advanced syntax is passed through as-is."""
    if _ADVANCED.search(q):
        return q
    words = _WORD.findall(q)
    if not words:
        return '""'
    # prefix match helps inflected languages: "certificat" finds "certificates", and Russian word forms too
    terms = [f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in words]
    return (" OR " if mode == "or" else " ").join(terms)


def _filters(source, project, since, until, kind, include_deleted, alias="i"):
    where, args = [], []
    if source:
        where.append(f"{alias}.source = ?"); args.append(source)
    if kind:
        where.append(f"{alias}.kind = ?"); args.append(kind)
    if since:
        where.append(f"{alias}.created_at >= ?"); args.append(since)
    if until:
        where.append(f"{alias}.created_at < ?"); args.append(until)
    if project:
        where.append(f"{alias}.id IN (SELECT item_id FROM item_projects WHERE project_id = ?)"); args.append(project)
    if not include_deleted:
        where.append(f"{alias}.deleted_at IS NULL")
    return where, args


def search(conn: sqlite3.Connection, q: str, source: str | None = None, project: str | None = None,
           since: str | None = None, until: str | None = None, kind: str | None = None,
           limit: int = 20, include_tool_output: bool = False, include_deleted: bool = False,
           fallback_or: bool = True) -> list[dict]:
    where, args = _filters(source, project, since, until, kind, include_deleted)
    if not include_tool_output and not kind:
        where.append("i.kind NOT IN ('tool_result', 'tool_call')")
    extra = (" AND " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT i.id, i.source, i.kind, i.thread_id, i.title, i.author, i.url, i.path, i.cwd, i.repo,
               i.created_at, snippet(items_fts, 1, '«', '»', '…', 16) AS snippet,
               bm25(items_fts, 4.0, 1.0) AS score
        FROM items_fts JOIN items i ON i.rid = items_fts.rowid
        WHERE items_fts MATCH ?{extra}
        ORDER BY score LIMIT ?"""
    advanced = bool(_ADVANCED.search(q))
    rows = []
    for mode in ("and", "or"):
        try:
            rows = conn.execute(sql, [fts_query(q, mode), *args, limit]).fetchall()
        except sqlite3.OperationalError:
            # broken advanced syntax (unbalanced quotes etc.) -> search the plain words instead
            words = [w for w in _WORD.findall(q) if w.upper() not in ("AND", "OR", "NOT", "NEAR")]
            if not words:
                return []
            q, advanced = " ".join(words), False
            rows = conn.execute(sql, [fts_query(q, mode), *args, limit]).fetchall()
        if rows or advanced or not fallback_or:
            break
    return [dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["projects"] = [r[0] for r in conn.execute(
        "SELECT project_id FROM item_projects WHERE item_id = ?", (item_id,))]
    return d


def get_thread(conn: sqlite3.Connection, thread_id: str, limit: int = 200,
               include_tool_output: bool = False) -> list[dict]:
    extra = "" if include_tool_output else " AND kind NOT IN ('tool_result', 'tool_call')"
    rows = conn.execute(f"""SELECT id, kind, author, title, body, created_at FROM items
                            WHERE thread_id = ?{extra} ORDER BY created_at, rid LIMIT ?""",
                        (thread_id, limit)).fetchall()
    return [dict(r) for r in rows]


def recent(conn: sqlite3.Connection, source: str | None = None, project: str | None = None,
           limit: int = 20) -> list[dict]:
    where, args = _filters(source, project, None, None, None, False)
    where.append("i.kind NOT IN ('tool_result', 'tool_call')")
    rows = conn.execute(f"""SELECT i.id, i.source, i.kind, i.thread_id, i.title, i.url, i.created_at
                            FROM items i WHERE {' AND '.join(where)}
                            ORDER BY i.created_at DESC LIMIT ?""", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def timeline(conn: sqlite3.Connection, project: str, since: str | None = None, until: str | None = None,
             limit: int = 500) -> list[dict]:
    """Project history collapsed to one entry per (day, thread): a 40-message chat is one line."""
    where, args = _filters(None, project, since, until, None, False)
    where.append("i.kind NOT IN ('tool_result', 'tool_call')")
    rows = conn.execute(f"""
        SELECT substr(i.created_at, 1, 10) AS day, i.source,
               COALESCE(i.thread_id, i.id) AS thread,
               COUNT(*) AS n,
               MIN(i.created_at) AS first_at,
               (SELECT t.title FROM items t
                 WHERE COALESCE(t.thread_id, t.id) = COALESCE(i.thread_id, i.id)
                   AND substr(t.created_at, 1, 10) = substr(i.created_at, 1, 10)
                   AND t.kind NOT IN ('tool_result', 'tool_call')
                   AND (t.author IS NULL OR t.author != 'assistant')
                 ORDER BY t.created_at LIMIT 1) AS title,
               GROUP_CONCAT(DISTINCT i.kind) AS kinds,
               MAX(i.url) AS url
        FROM items i WHERE {' AND '.join(where)}
        GROUP BY day, i.source, thread
        ORDER BY first_at LIMIT ?""", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id, p.name, COUNT(i.id) AS items,
               MIN(i.created_at) AS first_at, MAX(i.created_at) AS last_at
        FROM projects p
        LEFT JOIN item_projects ip ON ip.project_id = p.id
        LEFT JOIN items i ON i.id = ip.item_id AND i.deleted_at IS NULL
        GROUP BY p.id ORDER BY last_at DESC""").fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    by_source = [dict(r) for r in conn.execute(
        """SELECT source, COUNT(*) AS items, MIN(created_at) AS first_at, MAX(created_at) AS last_at
           FROM items WHERE deleted_at IS NULL GROUP BY source ORDER BY items DESC""")]
    last = [dict(r) for r in conn.execute(
        """SELECT source, MAX(ts) AS ts FROM sync_log GROUP BY source""")]
    errors = [dict(r) for r in conn.execute(
        """SELECT ts, source, error FROM sync_log WHERE error IS NOT NULL ORDER BY ts DESC LIMIT 5""")]
    return {"by_source": by_source, "last_sync": last, "recent_errors": errors}


CHAT_SOURCES = ("claude_code", "codex", "claude_app", "chatgpt")


def list_threads(conn: sqlite3.Connection, source: str | None = None, project: str | None = None,
                 limit: int = 500) -> list[dict]:
    """Agent sessions and imported chats, newest first: one row per thread."""
    where = ["i.thread_id IS NOT NULL", "i.kind IN ('message', 'tool_call')"]
    args: list = []
    if source:
        where.append("i.source = ?"); args.append(source)
    else:
        where.append(f"i.source IN ({','.join('?' * len(CHAT_SOURCES))})"); args += CHAT_SOURCES
    if project:
        where.append("i.thread_id IN (SELECT it.thread_id FROM item_projects ip JOIN items it ON it.id = ip.item_id "
                     "WHERE ip.project_id = ?)"); args.append(project)
    rows = conn.execute(f"""
        SELECT i.thread_id, i.source, MIN(i.created_at) AS first_at, MAX(i.created_at) AS last_at,
               SUM(i.kind = 'message') AS n, MAX(i.cwd) AS cwd
        FROM items i WHERE {' AND '.join(where)}
        GROUP BY i.thread_id ORDER BY last_at DESC LIMIT ?""", (*args, limit)).fetchall()
    return [dict(r) for r in rows]
