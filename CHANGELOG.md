# Changelog

## 0.4.0 — 2026-11-02

- **macOS and Linux installer**: `./install.sh` (or double-click `install.command` on macOS) and
  `brainctl.sh` with the same commands as on Windows — a private environment, `brain` / `brainctl` in
  `~/.local/bin` with a PATH line in your shell profile, background sync via launchd (macOS), a systemd
  user timer or cron (Linux), MCP registration, `status`, `task`, `mcp`, `log`, `update`, `uninstall`.
- `brainctl update` also does `git pull` on Windows.
- SECURITY.md, CONTRIBUTING.md, issue and PR templates; CI installs and uninstalls on Ubuntu and macOS
  and runs shellcheck.
- `brain init` with no vault found leaves `vaults = []` instead of a placeholder path.

## 0.3.0 — 2026-11-02

- **Full-screen terminal UI**: run `brain` with no arguments (or `brain ui`). Sessions grouped by day with
  a details panel (how it started, last reply, folder, project) and one-key handoff to the clipboard;
  search, recent items, project timelines and handoffs, sync, chat-export import, MCP agent connections,
  status and paths, language switch. The UI engine is shared with skillpantry.
- README rewritten with a screenshot, features and FAQ.

## 0.2.0 — 2026-11-02

- **English interface.** The CLI, `brainctl`, logs, handoff briefings and the config template are now in English and Russian. The language follows the OS; override with `language` in config.toml or `AGENT_RECALL_LANG`.
- Tool calls are stored with language-neutral markers (`[tool …]`, `[result]`); databases created by 0.1.0 keep working.
- `brain` exits quietly when its output is piped into `head` / `Select-Object -First`.

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
