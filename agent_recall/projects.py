"""Attach items to projects using rules from config.toml.

Rules run incrementally on items written since the last pass; `rebuild=True`
re-evaluates everything (use after editing rules). Manual tags are never touched.
"""
from __future__ import annotations

import fnmatch
import json
import re
import sqlite3

from .db import get_state, now_precise, set_state
from .i18n import _


def _norm(p: str | None) -> str:
    return (p or "").replace("\\", "/").lower().rstrip("/")


class Rule:
    def __init__(self, p: dict):
        self.id = p["id"]
        self.name = p.get("name") or p["id"]
        low = lambda k: [str(x).lower().replace("\\", "/") for x in p.get(k, [])]
        self.cwd, self.repo, self.obsidian = low("cwd"), low("repo"), low("obsidian")
        self.tags = [t.lstrip("#") for t in low("tags")]
        self.keywords = low("keywords")
        rx = [re.escape(k.upper()) + r"-\d+" for k in p.get("jira", [])]
        rx += list(p.get("regex", []))
        self.regex = re.compile(r"\b(?:" + "|".join(rx) + r")\b", re.I) if rx else None

    def match(self, row: sqlite3.Row, meta: dict) -> str | None:
        cwd = _norm(row["cwd"])
        if cwd and any(fnmatch.fnmatchcase(cwd, pat) for pat in self.cwd):
            return "rule:cwd"
        repo = (row["repo"] or "").lower()
        if repo and any(fnmatch.fnmatchcase(repo, pat) for pat in self.repo):
            return "rule:repo"
        if row["source"] == "obsidian":
            path = _norm(row["path"])
            if any(fnmatch.fnmatchcase(path, pat) for pat in self.obsidian):
                return "rule:obsidian"
            tags = {t.lower() for t in meta.get("tags", [])}
            if any(t in tags or any(x.startswith(t + "/") for x in tags) for t in self.tags):
                return "rule:tag"
        text = f"{row['title'] or ''}\n{row['body'] or ''}"
        if self.keywords:
            low_text = text.lower()
            if any(k in low_text for k in self.keywords):
                return "rule:keyword"
        if self.regex and self.regex.search(text):
            return "rule:regex"
        return None


def assign_projects(conn: sqlite3.Connection, cfg: dict, rebuild: bool = False, log=None) -> int:
    rules = [Rule(p) for p in cfg.get("projects", [])]
    for r in rules:
        conn.execute("INSERT INTO projects(id, name) VALUES (?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET name = excluded.name", (r.id, r.name))
    started = now_precise()
    last = None if rebuild else get_state(conn, "projects:last_pass")
    if rebuild:
        conn.execute("DELETE FROM item_projects WHERE method LIKE 'rule:%'")
    if not rules:
        set_state(conn, "projects:last_pass", started)
        conn.commit()
        return 0
    sql = "SELECT id, source, title, body, path, cwd, repo, meta FROM items"
    args: tuple = ()
    if last:
        sql += " WHERE synced_at >= ?"
        args = (last,)
    added = 0
    rows = conn.execute(sql, args).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except json.JSONDecodeError:
            meta = {}
        if last:  # item changed since last pass: re-evaluate its rule-based tags
            conn.execute("DELETE FROM item_projects WHERE item_id = ? AND method LIKE 'rule:%'", (row["id"],))
        for r in rules:
            how = r.match(row, meta)
            if how:
                cur = conn.execute("INSERT OR IGNORE INTO item_projects(item_id, project_id, method) "
                                   "VALUES (?, ?, ?)", (row["id"], r.id, how))
                added += cur.rowcount
    # A chat session belongs to a project as a whole: if any message matched, tag the session.
    added += conn.execute("""
        INSERT OR IGNORE INTO item_projects(item_id, project_id, method)
        SELECT i.id, ip.project_id, 'rule:thread'
        FROM items i
        JOIN (SELECT DISTINCT it.thread_id, ip.project_id
              FROM item_projects ip JOIN items it ON it.id = ip.item_id
              WHERE it.source IN ('claude_code', 'codex', 'claude_app', 'chatgpt') AND it.thread_id IS NOT NULL) ip
          ON ip.thread_id = i.thread_id
        WHERE i.source IN ('claude_code', 'codex', 'claude_app', 'chatgpt')
    """).rowcount
    # An image/PDF belongs to the projects of the notes it is embedded in.
    added += conn.execute("""
        INSERT OR IGNORE INTO item_projects(item_id, project_id, method)
        SELECT a.id, ip.project_id, 'rule:embed'
        FROM items a, json_each(a.meta, '$.notes') j
        JOIN item_projects ip ON ip.item_id = j.value
        WHERE a.source = 'obsidian' AND a.kind IN ('image', 'pdf', 'attachment')
    """).rowcount
    set_state(conn, "projects:last_pass", started)
    conn.commit()
    if log:
        log.info(_("projects: checked %d items, new links %d"), len(rows), added)
    return added


def tag(conn: sqlite3.Connection, item_id: str, project_id: str, remove: bool = False) -> None:
    if remove:
        conn.execute("DELETE FROM item_projects WHERE item_id = ? AND project_id = ?", (item_id, project_id))
    else:
        conn.execute("INSERT OR IGNORE INTO projects(id, name) VALUES (?, ?)", (project_id, project_id))
        conn.execute("INSERT INTO item_projects(item_id, project_id, method) VALUES (?, ?, 'manual') "
                     "ON CONFLICT(item_id, project_id) DO UPDATE SET method = 'manual'", (item_id, project_id))
    conn.commit()
