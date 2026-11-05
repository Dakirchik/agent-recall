"""Loading config.toml with sane defaults."""
from __future__ import annotations

import os
import json
import re
import tomllib
from pathlib import Path
from typing import Any


def set_config_value(path: Path, key: str, value: str | list[str]) -> None:
    """Set a top-level TOML string/list while preserving existing tables."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    pat = re.compile(rf'(?m)^\s*{re.escape(key)}\s*=.*$')
    first_table = re.search(r"(?m)^\s*\[", text)
    head = text[: first_table.start()] if first_table else text
    head = pat.sub(lambda _match: line, head, count=1) if pat.search(head) else line + "\n" + head
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + (text[first_table.start():] if first_table else ""), encoding="utf-8")


def remember_import(cfg: dict, manifest: Path, *, archive_root: Path | None = None) -> None:
    """Remember archive locations so they remain visible after restarting the UI."""
    archive = str(manifest.resolve().parent)
    locations = [p for p in dict.fromkeys(cfg.get("import_archives", [])) if p != archive] + [archive]
    path = Path(cfg["_config_path"])
    if archive_root is not None:
        root = str(archive_root.resolve())
        set_config_value(path, "import_archive_dir", root)
        cfg["import_archive_dir"] = root
    set_config_value(path, "import_archives", locations)
    cfg["import_archives"] = locations

def data_home() -> Path:
    """Where config, database and log live. AGENT_RECALL_HOME overrides; an existing
    pre-rename folder (~/.brainsync) keeps being used so old installs need no migration."""
    env = os.environ.get("AGENT_RECALL_HOME")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    new, legacy = Path.home() / ".agent-recall", Path.home() / ".brainsync"
    if not new.exists() and legacy.exists():
        return legacy
    return new


DEFAULT_CONFIG_PATH = data_home() / "config.toml"

# Same env vars the tools themselves use to relocate their data folders
_CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
_CODEX_HOME = os.environ.get("CODEX_HOME") or "~/.codex"

DEFAULTS: dict[str, Any] = {
    "db_path": str(data_home() / "brain.db"),
    "obsidian": {"enabled": False, "vaults": [], "exclude": [".obsidian", ".trash", ".git"]},
    "claude_code": {"enabled": True, "dir": f"{_CLAUDE_HOME}/projects", "max_tool_chars": 1500},
    "codex": {"enabled": True, "dirs": [f"{_CODEX_HOME}/sessions", f"{_CODEX_HOME}/archived_sessions"],
              "max_tool_chars": 1500},
    "github": {
        "enabled": False, "token_env": "GITHUB_TOKEN", "repos": [], "include_forks": False,
        "since": "2025-01-01", "max_pages": 30,
    },
    "export": {"after_sync": [], "daily_snapshots_dir": "", "keep_daily": 14},
    "projects": [],
}


def expand(p: str | os.PathLike) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    path = path or os.environ.get("BRAIN_CONFIG") or DEFAULT_CONFIG_PATH
    path = expand(path)
    data: dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    cfg = _merge(DEFAULTS, data)
    cfg["_config_path"] = str(path)
    cfg["_config_found"] = path.exists()
    return cfg
