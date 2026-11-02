"""Discovering what is installed on this machine: Obsidian vaults, agent data folders, GitHub auth."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .config import expand


def _appdata_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support"]
    return [Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")),
            home / ".var" / "app" / "md.obsidian.Obsidian" / "config"]  # flatpak


def detect_obsidian_vaults() -> list[Path]:
    """Obsidian keeps the list of opened vaults in obsidian.json in its app-data folder."""
    found: list[tuple[int, Path]] = []
    for base in _appdata_dirs():
        f = base / "obsidian" / "obsidian.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for v in (data.get("vaults") or {}).values():
            p = Path(v.get("path", ""))
            if p.is_dir():
                found.append((int(v.get("ts") or 0), p))
    found.sort(key=lambda x: -x[0])  # most recently opened first
    seen, out = set(), []
    for _, p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def count_files(root: Path, pattern: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob(pattern))


def github_auth_source(cfg: dict) -> str | None:
    env = cfg["github"].get("token_env") or "GITHUB_TOKEN"
    if os.environ.get(env):
        return f"переменная {env}"
    from .collectors.github import _token
    if _token({"token_env": "__none__"}):
        return "gh auth"
    return None


def source_paths(cfg: dict) -> dict[str, list[Path]]:
    return {
        "claude_code": [expand(cfg["claude_code"]["dir"])],
        "codex": [expand(d) for d in cfg["codex"].get("dirs", [])],
        "obsidian": [expand(v) for v in cfg["obsidian"].get("vaults", [])],
    }
