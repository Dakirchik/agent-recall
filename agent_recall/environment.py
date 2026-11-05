"""Discovering what is installed on this machine: Obsidian vaults, agent data folders, GitHub auth."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .config import expand
from .i18n import _


def detect_codex() -> Path | None:
    """Find a native CLI on PATH or in the desktop/VS Code installations."""
    command = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if command:
        return Path(command)
    home = Path.home()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        patterns = [(local / "OpenAI/Codex/bin", "*/codex.exe"),
                    (home / ".vscode/extensions", "openai.chatgpt-*/bin/windows-x86_64/codex.exe")]
    elif sys.platform == "darwin":
        patterns = [(Path("/Applications"), "Codex.app/Contents/Resources/codex"),
                    (home / "Applications", "Codex.app/Contents/Resources/codex")]
    else:
        patterns = [(home / ".local/bin", "codex")]
    for root, pattern in patterns:
        try:
            found = [p for p in root.glob(pattern) if p.is_file()]
            if found:
                return max(found, key=lambda p: p.stat().st_mtime)
        except OSError:
            continue
    return None


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
    for _ts, p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def count_files(root: Path, pattern: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _f in root.rglob(pattern))


def github_auth_source(cfg: dict) -> str | None:
    env = cfg["github"].get("token_env") or "GITHUB_TOKEN"
    if os.environ.get(env):
        return _("variable {name}", name=env)
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
