"""Register / unregister the brain MCP server in agents: Claude Code, Codex, Cline (VS Code)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from .i18n import _

SERVER_NAME = "brain"
AGENTS = ("claude", "codex", "cline")


def _server_cmd(python: str | None) -> tuple[str, list[str]]:
    return (python or sys.executable), ["-m", "agent_recall", "mcp"]


def _backup(path: Path) -> None:
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


# --- Claude Code: through its own CLI, which owns ~/.claude.json -------------------------

def _claude_cli() -> str | None:
    return shutil.which("claude")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")


def claude_status() -> str:
    cli = _claude_cli()
    if not cli:
        return _("not found (no `claude` command)")
    r = _run([cli, "mcp", "get", SERVER_NAME])
    return _("connected") if r.returncode == 0 else _("not connected")


def claude_add(python: str | None) -> str:
    cli = _claude_cli()
    cmd, args = _server_cmd(python)
    if not cli:
        return _("Claude Code not found. Register manually: {cmd}",
                 cmd=f"claude mcp add {SERVER_NAME} --scope user -- \"{cmd}\" {' '.join(args)}")
    _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"])  # idempotent re-add with fresh paths
    r = _run([cli, "mcp", "add", SERVER_NAME, "--scope", "user", "--", cmd, *args])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return "Claude Code: " + _("connected")


def claude_remove() -> str:
    cli = _claude_cli()
    if not cli:
        return "Claude Code: " + _("not found, skipping")
    r = _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"])
    return "Claude Code: " + (_("disconnected") if r.returncode == 0 else _("was not connected"))


# --- Codex: [mcp_servers.brain] block in config.toml -----------------------------------------

def _codex_config() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "config.toml"


# The [mcp_servers.brain] table and any sub-tables like [mcp_servers.brain.env], up to the next header.
_CODEX_BLOCK = re.compile(r"(?ms)^\[mcp_servers\.brain(?:\.[^\]\n]*)?\][^\n]*\n?.*?(?=^\[|\Z)")


def _strip_block(text: str) -> str:
    prev = None
    while prev != text:
        prev, text = text, _CODEX_BLOCK.sub("", text, count=1)
    return text


def codex_status() -> str:
    cfg = _codex_config()
    if not cfg.parent.exists():
        return _("not found (no .codex folder)")
    return _("connected") if cfg.exists() and _CODEX_BLOCK.search(cfg.read_text(encoding="utf-8")) else _("not connected")


def codex_add(python: str | None) -> str:
    cfg = _codex_config()
    if not cfg.parent.exists():
        return "Codex: " + _("not found, skipping")
    cmd, args = _server_cmd(python)
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    _backup(cfg)
    text = _strip_block(text).rstrip()
    # TOML literal strings ('...') keep Windows backslashes as-is
    block = (f"\n\n[mcp_servers.{SERVER_NAME}]\ncommand = '{cmd}'\n"
             f"args = [{', '.join(repr(a) for a in args)}]\n")
    cfg.write_text((text + block).lstrip("\n"), encoding="utf-8")
    return "Codex: " + _("connected")


def codex_remove() -> str:
    cfg = _codex_config()
    if not cfg.exists():
        return "Codex: " + _("not found, skipping")
    text = cfg.read_text(encoding="utf-8")
    if not _CODEX_BLOCK.search(text):
        return "Codex: " + _("was not connected")
    _backup(cfg)
    cfg.write_text(_strip_block(text).rstrip() + "\n", encoding="utf-8")
    return "Codex: " + _("disconnected")


# --- Cline (VS Code extension): cline_mcp_settings.json ------------------------------------

def _cline_settings() -> list[Path]:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    out = []
    for editor in ("Code", "Code - Insiders", "Cursor", "Windsurf"):
        d = base / editor / "User" / "globalStorage" / "saoudrizwan.claude-dev"
        if d.exists():
            out.append(d / "settings" / "cline_mcp_settings.json")
    return out


def _load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(_("{path} is not valid JSON, leaving it untouched: {e}", path=p, e=e))


def cline_status() -> str:
    files = _cline_settings()
    if not files:
        return _("not found (Cline extension not installed)")
    ok = [f for f in files if SERVER_NAME in (_load_json(f).get("mcpServers") or {})]
    return _("connected") if ok else _("not connected")


def cline_add(python: str | None) -> str:
    files = _cline_settings()
    if not files:
        return "Cline: " + _("not found, skipping")
    cmd, args = _server_cmd(python)
    for f in files:
        data = _load_json(f)
        _backup(f)
        data.setdefault("mcpServers", {})[SERVER_NAME] = {"command": cmd, "args": args, "disabled": False}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "Cline: " + _("connected ({n} editor(s))", n=len(files))


def cline_remove() -> str:
    files = [f for f in _cline_settings() if f.exists()]
    done = 0
    for f in files:
        data = _load_json(f)
        if SERVER_NAME in (data.get("mcpServers") or {}):
            _backup(f)
            del data["mcpServers"][SERVER_NAME]
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            done += 1
    return "Cline: " + (_("disconnected") if done else _("was not connected"))


def state(agent: str) -> str:
    """"on", "off" or "missing" (the agent is not installed) — for the UI."""
    try:
        if agent == "claude":
            cli = _claude_cli()
            if not cli:
                return "missing"
            return "on" if _run([cli, "mcp", "get", SERVER_NAME]).returncode == 0 else "off"
        if agent == "codex":
            cfg = _codex_config()
            if not cfg.parent.exists():
                return "missing"
            return "on" if cfg.exists() and _CODEX_BLOCK.search(cfg.read_text(encoding="utf-8")) else "off"
        if agent == "cline":
            files = _cline_settings()
            if not files:
                return "missing"
            return "on" if any(SERVER_NAME in (_load_json(f).get("mcpServers") or {}) for f in files) else "off"
    except Exception:
        return "off"
    return "missing"


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "cline": "Cline (VS Code)"}
ADD = {"claude": claude_add, "codex": codex_add, "cline": cline_add}
REMOVE = {"claude": claude_remove, "codex": codex_remove, "cline": cline_remove}
STATUS = {"claude": claude_status, "codex": codex_status, "cline": cline_status}
