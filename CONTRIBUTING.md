# Contributing

Bug reports, new sources and translations are welcome.

## Setup

```bash
git clone https://github.com/Dakirchik/agent-recall && cd agent-recall
pip install -e ".[mcp]" pytest
python -m pytest
```

CI runs the tests on Windows, macOS and Linux (Python 3.11 and 3.13), checks the shell scripts with
shellcheck, parses `brainctl.ps1` with Windows PowerShell 5.1, and installs/uninstalls on Linux and
macOS in an isolated home.

## Rules of the house

- **No new runtime dependencies** without a strong reason. The core is the standard library; `mcp` and
  `pypdf` are optional extras.
- **Never lose user data.** Collectors read; they never modify agent folders or the vault. Deleted notes
  are flagged, not erased. Other tools' configs are backed up before any change.
- **User-facing text in two languages.** In the CLI and collectors wrap strings in `_("English")` and add
  the Russian to `agent_recall/locales.py` (`test_i18n.py` fails on anything missing). In the full-screen
  UI and shell scripts write `T("English", "Русский")` / `t "English" "Русский"`.
- **Tests for every behaviour change.** UI screens are tested with scripted keys (`tests/test_app.py`).
- **No real personal data** in tests, fixtures or screenshots.

## Adding a source

One file in `agent_recall/collectors/` with `sync(conn, cfg, log) -> int` that reads only what is new
(keep a cursor in `sync_state`) and calls `db.upsert_items()` with dicts like
`{id, source, kind, thread_id, title, body, author, created_at, cwd, repo, path, meta}`. Register it in
`collectors/__init__.py`, add defaults to `config.py` and both config templates, and a test in `tests/`.

## Pull requests

Keep them focused, describe what changed and how you tested it, and tick the checklist in the template.
