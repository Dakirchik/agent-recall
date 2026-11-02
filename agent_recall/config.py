"""Loading config.toml with sane defaults."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

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
