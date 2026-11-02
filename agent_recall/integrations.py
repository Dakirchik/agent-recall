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
        return "не найден (нет команды claude)"
    r = _run([cli, "mcp", "get", SERVER_NAME])
    return "подключён" if r.returncode == 0 else "не подключён"


def claude_add(python: str | None) -> str:
    cli = _claude_cli()
    cmd, args = _server_cmd(python)
    if not cli:
        return f"Claude Code не найден. Подключите вручную: claude mcp add {SERVER_NAME} --scope user -- \"{cmd}\" {' '.join(args)}"
    _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"])  # idempotent re-add with fresh paths
    r = _run([cli, "mcp", "add", SERVER_NAME, "--scope", "user", "--", cmd, *args])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return "Claude Code: подключён"


def claude_remove() -> str:
    cli = _claude_cli()
    if not cli:
        return "Claude Code: не найден, пропускаю"
    r = _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"])
    return "Claude Code: отключён" if r.returncode == 0 else "Claude Code: не был подключён"


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
        return "не найден (нет папки .codex)"
    return "подключён" if cfg.exists() and _CODEX_BLOCK.search(cfg.read_text(encoding="utf-8")) else "не подключён"


def codex_add(python: str | None) -> str:
    cfg = _codex_config()
    if not cfg.parent.exists():
        return "Codex: не найден, пропускаю"
    cmd, args = _server_cmd(python)
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    _backup(cfg)
    text = _strip_block(text).rstrip()
    # TOML literal strings ('...') keep Windows backslashes as-is
    block = (f"\n\n[mcp_servers.{SERVER_NAME}]\ncommand = '{cmd}'\n"
             f"args = [{', '.join(repr(a) for a in args)}]\n")
    cfg.write_text((text + block).lstrip("\n"), encoding="utf-8")
    return "Codex: подключён"


def codex_remove() -> str:
    cfg = _codex_config()
    if not cfg.exists():
        return "Codex: не найден, пропускаю"
    text = cfg.read_text(encoding="utf-8")
    if not _CODEX_BLOCK.search(text):
        return "Codex: не был подключён"
    _backup(cfg)
    cfg.write_text(_strip_block(text).rstrip() + "\n", encoding="utf-8")
    return "Codex: отключён"


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
        raise RuntimeError(f"{p} — невалидный JSON, не трогаю: {e}")


def cline_status() -> str:
    files = _cline_settings()
    if not files:
        return "не найден (нет расширения Cline)"
    ok = [f for f in files if SERVER_NAME in (_load_json(f).get("mcpServers") or {})]
    return "подключён" if ok else "не подключён"


def cline_add(python: str | None) -> str:
    files = _cline_settings()
    if not files:
        return "Cline: не найден, пропускаю"
    cmd, args = _server_cmd(python)
    for f in files:
        data = _load_json(f)
        _backup(f)
        data.setdefault("mcpServers", {})[SERVER_NAME] = {"command": cmd, "args": args, "disabled": False}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"Cline: подключён ({len(files)} редактор(а))"


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
    return "Cline: отключён" if done else "Cline: не был подключён"


ADD = {"claude": claude_add, "codex": codex_add, "cline": cline_add}
REMOVE = {"claude": claude_remove, "codex": codex_remove, "cline": cline_remove}
STATUS = {"claude": claude_status, "codex": codex_status, "cline": cline_status}
