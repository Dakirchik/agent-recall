"""SQLite storage: one `items` table for every source + FTS5 index."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .redact import redact

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    rid         INTEGER PRIMARY KEY,           -- stable rowid for the FTS index
    id          TEXT NOT NULL UNIQUE,          -- source-specific stable id
    source      TEXT NOT NULL,                 -- obsidian | claude_code | codex | github
    kind        TEXT NOT NULL,                 -- note, adr, message, commit, issue, ...
    thread_id   TEXT,                          -- chat session / issue / note itself
    title       TEXT,
    body        TEXT,
    author      TEXT,                          -- user / assistant / github login
    url         TEXT,
    path        TEXT,                          -- file path / note path
    cwd         TEXT,                          -- working dir for agent sessions
    repo        TEXT,                          -- owner/name for GitHub
    created_at  TEXT,                          -- ISO-8601 UTC
    updated_at  TEXT,
    deleted_at  TEXT,                          -- kept, not removed: nothing is lost
    meta        TEXT,                          -- JSON with source-specific extras
    hash        TEXT,
    synced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_source_created ON items(source, created_at);
CREATE INDEX IF NOT EXISTS idx_items_thread ON items(thread_id);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_items_synced ON items(synced_at);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body,
    content='items', content_rowid='rid',
    tokenize='unicode61 remove_diacritics 2',
    prefix='2 3'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body) VALUES (new.rid, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body) VALUES ('delete', old.rid, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, body ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body) VALUES ('delete', old.rid, old.title, old.body);
    INSERT INTO items_fts(rowid, title, body) VALUES (new.rid, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS projects (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_projects (
    item_id    TEXT NOT NULL,
    project_id TEXT NOT NULL,
    method     TEXT NOT NULL,                  -- rule:<kind> | manual
    PRIMARY KEY (item_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_item_projects_project ON item_projects(project_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sync_log (
    ts       TEXT NOT NULL,
    source   TEXT NOT NULL,
    written  INTEGER NOT NULL,
    seconds  REAL,
    error    TEXT
);
"""

ITEM_FIELDS = ("id", "source", "kind", "thread_id", "title", "body", "author", "url",
               "path", "cwd", "repo", "created_at", "updated_at", "deleted_at", "meta")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_precise() -> str:
    """Microsecond timestamp for change tracking (synced_at), so runs in the same second don't overlap."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_iso(value: Any) -> str | None:
    """Normalise timestamps (epoch seconds/ms, ISO strings, datetimes) to ISO-8601 UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        dt = datetime.fromtimestamp(v, tz=timezone.utc)
    else:
        s = str(value).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO sync_state(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()  # an open write transaction would block backups from this connection
    conn.row_factory = sqlite3.Row
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return row[0]


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO sync_state(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def _hash(item: dict) -> str:
    h = hashlib.sha1()
    for f in ("title", "body", "meta", "deleted_at", "updated_at", "url"):
        h.update(str(item.get(f) or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def upsert_items(conn: sqlite3.Connection, items: Iterable[dict]) -> int:
    """Insert or update items. Returns number of rows actually written (new or changed)."""
    written = 0
    synced = now_precise()
    sql = (
        f"INSERT INTO items ({', '.join(ITEM_FIELDS)}, hash, synced_at) "
        f"VALUES ({', '.join('?' for _ in ITEM_FIELDS)}, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        + ", ".join(f"{f} = excluded.{f}" for f in ITEM_FIELDS if f != "id")
        + ", hash = excluded.hash, synced_at = excluded.synced_at "
        "WHERE items.hash IS NOT excluded.hash"
    )
    for item in items:
        item = dict(item)
        item["title"] = redact(item.get("title"))
        item["body"] = redact(item.get("body"))
        if isinstance(item.get("meta"), (dict, list)):
            item["meta"] = json.dumps(item["meta"], ensure_ascii=False, default=str)
        for f in ("created_at", "updated_at", "deleted_at"):
            item[f] = to_iso(item.get(f))
        h = _hash(item)
        cur = conn.execute(sql, [item.get(f) for f in ITEM_FIELDS] + [h, synced])
        written += cur.rowcount if cur.rowcount > 0 else 0
    return written


def mark_deleted(conn: sqlite3.Connection, ids: Iterable[str]) -> int:
    n = 0
    ts, synced = now_iso(), now_precise()
    for i in ids:
        cur = conn.execute(
            "UPDATE items SET deleted_at = ?, hash = NULL, synced_at = ? WHERE id = ? AND deleted_at IS NULL",
            (ts, synced, i))
        n += cur.rowcount
    return n


def log_sync(conn: sqlite3.Connection, source: str, written: int, seconds: float, error: str | None) -> None:
    conn.execute("INSERT INTO sync_log(ts, source, written, seconds, error) VALUES (?, ?, ?, ?, ?)",
                 (now_iso(), source, written, round(seconds, 2), error))
    conn.execute("DELETE FROM sync_log WHERE rowid NOT IN (SELECT rowid FROM sync_log ORDER BY ts DESC LIMIT 5000)")
