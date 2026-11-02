"""The full-screen UI driven by scripted keys (no real terminal)."""
import io

import pytest

from agent_recall import app as appmod
from agent_recall import handoff, integrations
from agent_recall.tui import Term, strip_ansi

from conftest import cc_line


def term(keys, size=(120, 34)):
    return Term(keys=keys, out=io.StringIO(), size=size)


def frames(t: Term) -> list[str]:
    return [strip_ansi(f).replace("\r\n", "\n") for f in t.out.getvalue().split("\x1b[H")]


@pytest.fixture
def filled(env):
    env.projects = [{"id": "shop", "name": "Shop", "cwd": ["*shop*"]}]
    env.claude_session("C--dev-shop", "s1", [
        {"type": "summary", "summary": "Checkout validation fix", "leafUuid": "x"},
        cc_line("u1", "user", "Payments fail at checkout, fix it", "2026-11-01T10:00:00Z"),
        cc_line("a1", "assistant", [{"type": "text", "text": "Fixed the validator; e2e left."}], "2026-11-01T10:05:00Z"),
    ])
    env.note("Ideas.md", "# Ideas\nA payments dashboard")
    env.sync()
    return env


def run(env, keys):
    t = term(keys)
    appmod.App(env.config(), t).run()
    return t


def test_home_screen_lists_sections_and_quits(filled):
    t = run(filled, ["q"])
    first = frames(t)[-1]
    assert "Sessions" in first and "Projects" in first and "Sync now" in first


def test_without_database_only_setup_is_available(env):
    t = run(env, ["enter", "q"])  # cursor starts on Sessions, which is disabled
    assert "no database yet" in frames(t)[-1]


def test_session_handoff_is_copied(filled, monkeypatch):
    copied = {}
    monkeypatch.setattr(handoff, "copy_to_clipboard", lambda text: copied.setdefault("text", text) or True)
    # Home → Sessions → the session → Copy → message → back, back, quit
    t = run(filled, ["enter", "enter", "enter", "esc", "esc", "q"])
    assert "Checkout validation fix" in "".join(frames(t))
    assert "## How it started" in copied["text"] and "Fixed the validator" in copied["text"]


def test_handoff_falls_back_to_a_file(filled, monkeypatch):
    monkeypatch.setattr(handoff, "copy_to_clipboard", lambda text: False)
    run(filled, ["enter", "enter", "enter", "esc", "esc", "q"])
    saved = list((filled.data / "handoffs").glob("*.md"))
    assert saved and "Payments fail at checkout" in saved[0].read_text(encoding="utf-8")


def test_search_finds_and_shows_details(filled):
    keys = ["down", "enter"] + list("payments") + ["enter", "down", "esc", "esc", "q"]
    t = run(filled, keys)
    text = "".join(frames(t))
    assert "Found 2" in text and "Me: Payments fail at checkout" in text and "A payments dashboard" in text


def test_project_timeline(filled):
    # Home → Projects → shop → Timeline (view) → back…
    keys = ["down", "down", "down", "enter", "enter", "enter", "esc", "esc", "esc", "q"]
    t = run(filled, keys)
    assert "Payments fail at checkout" in "".join(frames(t))


def test_agents_toggle(filled, monkeypatch):
    state = {"claude": "off", "codex": "missing", "cline": "on"}
    calls = []
    monkeypatch.setattr(integrations, "state", lambda a: state[a])
    monkeypatch.setitem(integrations.ADD, "claude", lambda py: calls.append("add") or "Claude Code: connected")
    keys = ["down"] * 6 + ["enter", "enter", "esc", "esc", "q"]
    run(filled, keys)
    assert calls == ["add"]


def test_language_switch_writes_config(filled):
    keys = ["down"] * 8 + ["enter", "down", "enter", "q"]
    t = run(filled, keys)
    cfg_text = (filled.root / "config.toml").read_text(encoding="utf-8")
    assert 'language = "ru"' in cfg_text and "Сессии" in frames(t)[-1]


def test_set_config_value_keeps_tables(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('db_path = "x"\n[obsidian]\nlanguage = "nope"\n', encoding="utf-8")
    appmod.set_config_value(p, "language", "en")
    text = p.read_text(encoding="utf-8")
    assert text.startswith('language = "en"\n') and 'language = "nope"' in text
    appmod.set_config_value(p, "language", "ru")
    assert p.read_text(encoding="utf-8").count('language = "ru"') == 1
