"""MCP server (stdio): gives any agent read-only access to the brain.

Register in Claude Code:  claude mcp add brain -- python -m agent_recall mcp
"""
from __future__ import annotations

import json
from contextlib import closing

from . import db as dbmod
from . import query
from .config import expand
from .i18n import _


def serve(cfg: dict) -> None:
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        try:  # mcp 1.x
            from mcp.server.fastmcp import FastMCP
        except ImportError as e:
            raise SystemExit(_("The mcp package is required: pip install \"mcp>=1.2\"")) from e

    from .i18n import set_lang
    set_lang(config_value=cfg.get("language"))  # language of handoff briefings
    db_path = expand(cfg["db_path"])
    mcp = FastMCP("brain", instructions=(
        "Personal knowledge base of the user: Obsidian notes (incl. ADRs, session logs, screenshots), "
        "past Claude Code and Codex sessions, imported Claude app and ChatGPT chats, GitHub "
        "commits/issues/PRs. To continue work started in another AI, call `handoff`. Use `search` before asking "
        "the user for context they may have already written down or discussed. Search is keyword-based "
        "(BM25): try several phrasings, synonyms, and both Russian and English terms. Then open "
        "results with `get_item` or the whole conversation with `get_thread`."))

    def conn():
        return dbmod.connect(db_path, readonly=True)

    def _q(fn, *a, **kw):
        with closing(conn()) as c:
            return fn(c, *a, **kw)

    def dump(x) -> str:
        return json.dumps(x, ensure_ascii=False, default=str)

    @mcp.tool()
    def search(query_text: str, source: str | None = None, project: str | None = None,
               since: str | None = None, until: str | None = None, limit: int = 15) -> str:
        """Full-text search across all synced data.
        source: obsidian | claude_code | codex | github | claude_app | chatgpt. project: project id (see list_projects).
        since/until: YYYY-MM-DD. Supports FTS5 syntax: "exact phrase", OR, NOT, prefix*."""
        return dump(_q(query.search, query_text, source=source, project=project,
                                 since=since, until=until, limit=min(limit, 50)))

    @mcp.tool()
    def get_item(id: str, max_chars: int = 12000) -> str:
        """Full content of one item by id (from search results)."""
        item = _q(query.get_item, id)
        if item and item.get("body") and len(item["body"]) > max_chars:
            item["body"] = item["body"][:max_chars] + "\n…[truncated]"
        return dump(item or {"error": "not found"})

    @mcp.tool()
    def get_thread(thread_id: str, limit: int = 120, max_chars_per_message: int = 3000) -> str:
        """All messages of a chat session / issue thread in order (thread_id from search results)."""
        rows = _q(query.get_thread, thread_id, limit=limit)
        for r in rows:
            if r.get("body") and len(r["body"]) > max_chars_per_message:
                r["body"] = r["body"][:max_chars_per_message] + "…"
        return dump(rows)

    @mcp.tool()
    def timeline(project: str, since: str | None = None, until: str | None = None, limit: int = 200) -> str:
        """Chronological history of a project, one line per day per thread/note/commit."""
        return dump(_q(query.timeline, project, since=since, until=until, limit=limit))

    @mcp.tool()
    def recent(source: str | None = None, project: str | None = None, limit: int = 20) -> str:
        """Most recent items, optionally filtered by source or project."""
        return dump(_q(query.recent, source=source, project=project, limit=limit))

    @mcp.tool()
    def list_projects() -> str:
        """Known projects with item counts and activity range."""
        return dump(_q(query.list_projects))

    @mcp.tool()
    def handoff(target: str | None = None, last_source: str | None = None, project: str | None = None,
                days: int = 14, max_chars: int = 14000) -> str:
        """Briefing to continue work started elsewhere (another agent or chat).
        target: session/thread id, item id, or words describing the session (searched).
        No target: the most recent session, optionally from last_source
        (claude_code | codex | claude_app | chatgpt). project: briefing for a whole project instead."""
        from . import handoff as ho
        with closing(conn()) as c:
            try:
                if project:
                    return ho.project_handoff(c, project, days=days, max_chars=max_chars)
                tid = ho.resolve_thread(c, target)[0] if target else ho.last_thread(c, last_source)
                return ho.thread_handoff(c, tid, max_chars=max_chars)
            except ho.ThreadNotFound as e:
                return str(e)

    mcp.run()
