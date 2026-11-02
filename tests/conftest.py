"""Shared fixtures: a throwaway home with an Obsidian vault and agent session folders."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_recall import db as dbmod
from agent_recall.config import load_config

LOG = logging.getLogger("tests")


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.vault = root / "vault"
        self.claude = root / "claude" / "projects"
        self.codex = root / "codex" / "sessions"
        self.data = root / "data"
        for d in (self.vault, self.claude, self.codex, self.data):
            d.mkdir(parents=True, exist_ok=True)
        self.projects: list[dict] = []
        self.extra: dict = {}

    # --- writing fake sources ---
    def note(self, rel: str, text: str) -> Path:
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def file(self, rel: str, data: bytes = b"\x89PNG fake") -> Path:
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def claude_session(self, project: str, session: str, lines: list[dict], mode: str = "a") -> Path:
        p = self.claude / project / f"{session}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, mode, encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        return p

    def codex_rollout(self, name: str, lines: list[dict]) -> Path:
        p = self.codex / "2026" / "11" / "01" / f"rollout-{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        return p

    # --- config / db ---
    def config(self) -> dict:
        def toml_list(xs):
            return "[" + ", ".join(json.dumps(x) for x in xs) + "]"
        text = f"""
db_path = {json.dumps((self.data / "brain.db").as_posix())}
[obsidian]
enabled = true
vaults = {toml_list([self.vault.as_posix()])}
[claude_code]
enabled = true
dir = {json.dumps(self.claude.as_posix())}
[codex]
enabled = true
dirs = {toml_list([self.codex.as_posix()])}
[github]
enabled = false
"""
        for p in self.projects:
            text += "\n[[projects]]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in p.items()) + "\n"
        path = self.root / "config.toml"
        path.write_text(text, encoding="utf-8")
        cfg = load_config(path)
        for k, v in self.extra.items():
            cfg.setdefault(k, {}).update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
        return cfg

    def sync(self, only=None) -> dict:
        from agent_recall.sync import run_sync
        return run_sync(self.config(), only=only)

    def conn(self):
        return dbmod.connect(self.data / "brain.db")


@pytest.fixture(autouse=True)
def english(monkeypatch):
    """Tests run in English unless they switch the language themselves."""
    from agent_recall import i18n
    monkeypatch.setenv("AGENT_RECALL_LANG", "en")
    i18n.set_lang()
    yield
    i18n._lang = None


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def cc_line(uuid: str, role: str, content, ts: str, session: str = "s1", cwd: str = r"C:\dev\shop") -> dict:
    return {"type": role, "uuid": uuid, "sessionId": session, "cwd": cwd, "timestamp": ts,
            "gitBranch": "main", "message": {"role": role, "content": content}}
