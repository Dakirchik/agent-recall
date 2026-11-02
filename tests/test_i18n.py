import ast
from pathlib import Path

from agent_recall import handoff, i18n
from agent_recall.locales import RU

from conftest import cc_line

SRC = Path(__file__).resolve().parent.parent / "agent_recall"


def _strings():
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                yield f.name, node.args[0].value


def test_every_string_has_a_russian_translation():
    missing = sorted({f"{f}: {s!r}" for f, s in _strings() if s not in RU})
    assert not missing, "Add to locales.RU:\n" + "\n".join(missing)


def test_placeholders_match():
    import string
    fields = lambda s: {f for _, f, _s, _c in string.Formatter().parse(s) if f}
    bad = [en for en, ru in RU.items() if fields(en) != fields(ru) or en.count("%") != ru.count("%")]
    assert not bad, bad


def test_language_switch(env, monkeypatch):
    env.claude_session("p", "s1", [cc_line("u1", "user", "fix the build", "2026-11-01T10:00:00Z")])
    env.sync()
    assert "## How it started" in handoff.thread_handoff(env.conn(), "s1")
    monkeypatch.setenv("AGENT_RECALL_LANG", "ru")
    i18n.set_lang()
    assert "## С чего началось" in handoff.thread_handoff(env.conn(), "s1")


def test_config_language_is_used_when_env_is_absent(monkeypatch):
    monkeypatch.delenv("AGENT_RECALL_LANG", raising=False)
    assert i18n.set_lang(config_value="ru") == "ru"
    assert i18n.set_lang(config_value="en") == "en"
    assert i18n.set_lang("ru", config_value="en") == "ru"
