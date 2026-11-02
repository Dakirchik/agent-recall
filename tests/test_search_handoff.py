import pytest

from agent_recall import handoff, query
from agent_recall.redact import redact

from conftest import cc_line


@pytest.fixture
def session(env):
    env.claude_session("C--dev-shop", "s1", [
        {"type": "summary", "summary": "Checkout validation fix", "leafUuid": "x"},
        cc_line("u1", "user", "<ide_opened_file>The user opened package.json</ide_opened_file>\n"
                              "Сертификаты падают при оплате, почини", "2026-11-01T10:00:00Z"),
        cc_line("a1", "assistant", [{"type": "tool_use", "name": "Edit",
                                     "input": {"file_path": "src/pay.ts", "old_string": "a", "new_string": "b"}},
                                    {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
                                    {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}],
                "2026-11-01T10:01:00Z"),
        cc_line("u2", "user", "<task-notification><status>completed</status></task-notification>", "2026-11-01T10:02:00Z"),
        cc_line("a2", "assistant", [{"type": "text", "text": "Fixed; remaining: e2e on staging."}], "2026-11-01T10:03:00Z"),
    ])
    env.sync()
    return env.conn()


def test_prefix_search_handles_russian_word_forms(session):
    assert query.search(session, "сертификат")  # finds "Сертификаты"


def test_broken_fts_syntax_does_not_crash(session):
    assert query.search(session, 'foo"bar AND') == []


def test_strict_search_has_no_or_fallback(session):
    assert query.search(session, "сертификаты marsupial", fallback_or=False) == []
    assert query.search(session, "сертификаты marsupial")  # default keeps the OR fallback


def test_handoff_contents(session):
    text = handoff.thread_handoff(session, "s1")
    assert "Checkout validation fix" in text
    assert "`src/pay.ts`" in text and "`README.md`" in text and "`npm test`" in text
    assert "ide_opened_file" not in text and "task-notification" not in text
    assert "Fixed; remaining: e2e on staging." in text
    assert "## С чего началось\nСертификаты падают при оплате" in text


def test_handoff_resolves_by_words_and_refuses_partial_matches(session):
    assert handoff.resolve_thread(session, "сертификаты оплате")[0] == "s1"
    with pytest.raises(handoff.ThreadNotFound):
        handoff.resolve_thread(session, "сертификаты батарею")


def test_handoff_respects_budget(session):
    assert len(handoff.thread_handoff(session, "s1", max_chars=700)) <= 900


def test_redaction():
    text = redact("token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB and password=SuperSecret123 "
                  "key sk-ant-api03-abcdefghijklmnopqrstuvwx https://user:pa55w0rd@host/x")
    assert "ghp_abc" not in text and "SuperSecret123" not in text and "sk-ant" not in text and "pa55w0rd" not in text
