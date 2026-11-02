"""Getting the database out: consistent file copies and JSONL dumps.

The copy uses SQLite's online backup API, so it is safe while a sync is writing
(a plain file copy of a WAL database can produce a corrupt file).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from .config import expand


def backup_db(src_conn: sqlite3.Connection, dest: str | os.PathLike) -> Path:
    looks_like_dir = str(dest).endswith(("/", "\\"))
    dest = expand(dest)
    if dest.is_dir() or looks_like_dir:
        dest = dest / "brain.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(tmp)
    try:
        src_conn.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")  # single self-contained file
    finally:
        out.close()
    os.replace(tmp, dest)
    return dest


def daily_snapshot(src_conn: sqlite3.Connection, folder: str | os.PathLike, keep: int = 14) -> Path | None:
    folder = expand(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"brain-{date.today().isoformat()}.db"
    if target.exists():
        return None
    backup_db(src_conn, target)
    snaps = sorted(folder.glob("brain-????-??-??.db"))
    for old in snaps[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return target


def export_jsonl(conn: sqlite3.Connection, dest: str | os.PathLike, source: str | None = None,
                 since: str | None = None, project: str | None = None,
                 include_tool_output: bool = False) -> tuple[Path, int]:
    """One JSON object per line — the format most RAG / fine-tuning / warehouse loaders accept."""
    dest = expand(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    where, args = ["deleted_at IS NULL"], []
    if source:
        where.append("source = ?"); args.append(source)
    if since:
        where.append("created_at >= ?"); args.append(since)
    if project:
        where.append("id IN (SELECT item_id FROM item_projects WHERE project_id = ?)"); args.append(project)
    if not include_tool_output:
        where.append("kind NOT IN ('tool_result', 'tool_call')")
    projects: dict[str, list[str]] = {}
    for item_id, pid in conn.execute("SELECT item_id, project_id FROM item_projects"):
        projects.setdefault(item_id, []).append(pid)
    n = 0
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        for row in conn.execute(f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY created_at", args):
            d = dict(row)
            d.pop("rid", None); d.pop("hash", None)
            try:
                d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
            except json.JSONDecodeError:
                pass
            d["projects"] = projects.get(d["id"], [])
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            n += 1
    return dest, n


def mirror_dir(src: str | os.PathLike, dst: str | os.PathLike) -> int:
    """Copy new/changed files from src to dst (nothing is deleted at the destination)."""
    src, dst = expand(src), expand(dst)
    if not src.is_dir():
        return 0
    n = 0
    for root, _dirs, files in os.walk(src):
        for fn in files:
            s = os.path.join(root, fn)
            d = dst / os.path.relpath(s, src)
            st = os.stat(s)
            if d.exists():
                dt = d.stat()
                if dt.st_size == st.st_size and int(dt.st_mtime) >= int(st.st_mtime):
                    continue
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            n += 1
    return n
