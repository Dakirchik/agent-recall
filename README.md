# agent-recall

**Shared memory for your AI agents.** Every 15 minutes it collects your Claude Code and Codex sessions, Obsidian notes (with screenshots and PDFs), GitHub activity and exported Claude / ChatGPT chats into one SQLite file — and lets any agent search it over MCP or pick up where another one left off.

[Русская версия](README.ru.md)

> No embeddings, no vector DB, no server, no cloud. Python standard library + SQLite full-text search. One file you own.

## Why

You start a task in Claude Code, continue in Codex, discuss the design in a chat app, and write the decision down in Obsidian. A week later none of the tools remembers the others. agent-recall keeps one local, searchable record of all of it:

- **"What did we decide about auth last week?"** — any agent can search your past sessions, notes and commits instead of asking you again.
- **Switch tools without re-explaining.** `brain handoff --last -c` builds a briefing from a session (the task, files changed, commands run, where it stopped) and copies it to the clipboard for any AI.
- **Project timelines.** Notes, commits and agent sessions on one time axis per project.
- **Your data stays yours.** Tools delete old sessions (Claude Code: after 30 days by default); the archive keeps them.

## What gets collected

| Source | What | How "new" is detected |
|---|---|---|
| **Claude Code** | user and assistant messages, tool calls (clipped), working dir, git branch, session summaries — from `~/.claude/projects` | byte offset per file |
| **Codex CLI** | messages and tool calls from `~/.codex/sessions` and `archived_sessions` | byte offset per file |
| **Obsidian** | notes: text, frontmatter, tags, links; ADRs and session logs get their own kinds | mtime + size; deleted notes are flagged, never erased |
| **Obsidian attachments** | screenshots, images, PDFs: the file is copied next to the DB; searchable by the heading and text around where it's embedded, PDFs also by content | same |
| **GitHub** | your commits, issues, PRs and comments in your repos | `since` per repo |
| **Claude app, ChatGPT** | chats from the official data export (`brain import`), manual | message ids — re-import adds only new messages |

Not stored: model "thinking", images inside agent chats, context the IDE injects (`<ide_opened_file>` etc.). Secrets that look like tokens, keys and passwords are masked before anything is written.

## Install

### Windows (one click)

Download the repo, double-click **`install.cmd`**. Or in PowerShell:

```powershell
.\brainctl.ps1 install
```

The installer:
1. finds Python 3.11+, or offers to install Python 3.12 via winget (current user, no admin);
2. creates an isolated environment in `%LOCALAPPDATA%\agent-recall` — your system Python is untouched;
3. adds two commands to PATH: `brain` (your data) and `brainctl` (management);
4. writes `%USERPROFILE%\.agent-recall\config.toml`, auto-detecting the Obsidian vaults you've opened;
5. auto-detects Claude Code and Codex session folders;
6. runs the first sync, schedules one every 15 minutes, and registers the MCP server in Claude Code, Codex and Cline (whichever are installed).

Options: `-Interval 30`, `-Vault "D:\Notes"`, `-Yes` (no questions), `-NoSync`, `-NoTask`, `-NoMcp`. Behind a corporate proxy set `$env:HTTPS_PROXY` first.

### macOS / Linux

```bash
pip install ".[mcp]"
brain init              # finds your Obsidian vaults
brain sync -v
brain integrate add all # MCP for Claude Code, Codex, Cline
brain doctor
```
Schedule `python -m agent_recall sync` with cron or launchd.

### GitHub (optional)

`gh auth login`, or set `GITHUB_TOKEN` (a read-only fine-grained token: Contents, Issues, Pull requests, Metadata).

## Use it

```powershell
brain search checkout bug                     # full-text search, all sources
brain search "exact phrase" --source obsidian
brain search auth --project shop --since 2026-09-01
brain show <id>                               # one item in full
brain thread <thread_id>                      # a whole agent session
brain recent --source codex -n 10
brain timeline shop --since 2026-10-01        # project history by day
brain stats                                   # what's in the database
brain paths                                   # where everything lives
```

Search is SQLite FTS5 (BM25) with prefix matching, so word forms match too. Tool output (file reads, command logs) is excluded by default; add `--tools` to include it.

### Continue in another AI: handoff

```powershell
brain handoff --last -c                  # latest session of any agent → clipboard
brain handoff --last claude_code -c
brain handoff payment validator -c       # find the session by words (all must match)
brain handoff payment --list             # show matching sessions, pick one
brain handoff --project shop --days 30 -c  # ADRs, notes, sessions and commits for a project
```

The briefing asks the receiving AI to restate the task and where things stopped, and to ask rather than guess. Agents with MCP have it as the `handoff` tool: *"take the handoff of my last Claude Code session and continue"*.

### Import chats from the Claude app and ChatGPT

Those chats live on the providers' servers, so they come in through the official data export:

- **Claude**: Settings → Privacy → Export data (web or desktop). The emailed link expires after 24 hours.
- **ChatGPT**: Settings → Data controls → Export data.

```powershell
brain import auto Downloads\export.zip   # format is detected automatically
```

## Projects

Rules in `config.toml`; an item joins a project if any pattern matches:

```toml
[[projects]]
id = "shop"
name = "Online shop"
cwd = ["*shop-frontend*"]   # agent session working directory
repo = ["me/shop-*"]        # GitHub repo
obsidian = ["Work/Shop/*"]  # note path in the vault
tags = ["shop"]             # Obsidian tag (and nested: shop/api)
jira = ["SHOP"]             # finds SHOP-123 anywhere in text
keywords = ["checkout-service"]
```

If one message of an agent session matches, the whole session joins the project. Screenshots join the projects of the notes they're embedded in. After editing rules: `brain projects --rebuild`. Manual: `brain tag <id> <project>`.

## MCP tools

`search`, `get_item`, `get_thread`, `timeline`, `recent`, `list_projects`, `handoff` — read-only.

`brainctl mcp add` (or `brain integrate add all`) registers them; manual setup:

```powershell
claude mcp add brain --scope user -- python -m agent_recall mcp
```
```toml
# ~/.codex/config.toml
[mcp_servers.brain]
command = "python"
args = ["-m", "agent_recall", "mcp"]
```

Tip: add to your `CLAUDE.md` / `AGENTS.md`: *"Before asking me about past decisions or context, search the brain MCP."*

## Managing it on Windows: brainctl

```powershell
brainctl status                  # install, scheduler, sources, agents
brainctl paths                   # all paths
brainctl config                  # open config.toml
brainctl log follow              # tail the sync log
brainctl task add -Interval 30   # change the schedule
brainctl task pause | resume | run | remove
brainctl mcp add claude          # all | claude | codex | cline
brainctl update                  # reinstall from the source folder
brainctl uninstall [-Purge]      # -Purge also deletes the database and config
```

| Folder | Contents | On `uninstall` |
|---|---|---|
| `%LOCALAPPDATA%\agent-recall` | Python environment, commands, copy of brainctl | removed |
| `%USERPROFILE%\.agent-recall` | `config.toml`, `brain.db`, `brain.log`, `attachments\` | kept (`-Purge` removes) |

Settings of other tools (`.codex\config.toml`, Cline) are backed up as `*.bak-<date>` before any change.

## Export

```powershell
brain export db D:\Backup\brain.db     # consistent copy, safe during a sync
brain export jsonl D:\datasets\all.jsonl   # one JSON object per line: RAG, fine-tuning, any warehouse
```
```toml
[export]
after_sync = ["D:/YandexDisk/agent-recall/brain.db"]  # copy (with attachments) after each sync
daily_snapshots_dir = "D:/Backup/agent-recall"       # brain-YYYY-MM-DD.db, keep_daily kept
```

Don't copy `brain.db` by hand while a sync runs (WAL mode) — use the commands above. To browse the database: [DB Browser for SQLite](https://sqlitebrowser.org/) (open read-only) or `pip install datasette && datasette brain.db`.

## Privacy and security

- Everything stays on your machine unless you configure an export target.
- Secret-looking strings are masked before writing. This is a heuristic, not a guarantee.
- The database contains your full conversations with agents. Keep it on an encrypted disk.
- Don't mix employer data into a personal archive: use a separate config and database on a work machine.

## Why no vector database?

Agents are good at keyword search: they rephrase, try synonyms and both languages, and read what they find. For a personal archive BM25 over SQLite is fast, free, and transparent. If semantic search ever becomes necessary, [sqlite-vec](https://github.com/asg017/sqlite-vec) with a local embedding model fits into the same file.

## Status and limitations

- The CLI speaks **Russian** for now; English messages are on the roadmap (PRs welcome).
- Windows is the primary platform (installer, scheduler); macOS/Linux work via pip.
- Claude Code / Codex log formats are undocumented and change; collectors are defensive, but expect occasional fixes.
- No OCR for screenshots yet (Windows has a built-in OCR engine — a natural next step).

## Development

```bash
pip install -e ".[mcp]" pytest
pytest
```

Adding a source = one file in `agent_recall/collectors/` with `sync(conn, cfg, log)` that calls `upsert_items()` with dicts like `{id, source, kind, thread_id, title, body, created_at, ...}`, plus one line in `collectors/__init__.py`.

```
agent_recall/
  collectors/      obsidian, claude_code, codex, github
  db.py            schema: items (+ FTS5), projects, item_projects, sync_state, sync_log
  sync.py          lock → collectors → project rules → export
  query.py         search, threads, timelines (shared by CLI and MCP)
  handoff.py       briefings for continuing in another AI
  importers.py     Claude app / ChatGPT export import
  projects.py      project rules
  export.py        .db copies, snapshots, JSONL
  redact.py        secret masking
  integrations.py  MCP registration in Claude Code, Codex, Cline
  environment.py   detection of vaults, folders, tokens (brain doctor)
  mcp_server.py    MCP server
brainctl.ps1       Windows installer and manager
install.cmd        double-click installer
```

## License

[MIT](LICENSE)
