"""Collectors. Each exposes `sync(conn, cfg, log) -> int` (rows written)."""
from . import claude_code, codex, github, obsidian

COLLECTORS = {
    "obsidian": obsidian.sync,
    "claude_code": claude_code.sync,
    "codex": codex.sync,
    "github": github.sync,
}
