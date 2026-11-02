# Changelog

## 0.1.0 — 2026-11-02

First public release.

- Collectors: Claude Code, Codex CLI, Obsidian (notes, screenshots, PDFs), GitHub.
- Import of Claude app and ChatGPT data exports.
- SQLite FTS5 search with prefix matching; project rules (cwd, repo, note path, tags, Jira keys, keywords).
- `brain handoff`: briefing to continue a session or project in another AI; clipboard copy.
- MCP server: search, get_item, get_thread, timeline, recent, list_projects, handoff.
- Export: consistent `.db` copies, daily snapshots, JSONL.
- Secret masking before anything is written.
- Windows: one-click installer and manager (`brainctl`): Python via winget, isolated venv, Task Scheduler, MCP registration in Claude Code / Codex / Cline. Migrates installs made under the earlier name *brainsync*.
