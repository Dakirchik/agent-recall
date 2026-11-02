from agent_recall import query

from conftest import cc_line


def test_claude_code_incremental_partial_line_and_rewrite(env):
    p = env.claude_session("C--dev-shop", "s1", [
        cc_line("u1", "user", "How do I fix the checkout bug?", "2026-11-01T10:00:00Z"),
        cc_line("u2", "assistant", [{"type": "thinking", "thinking": "secret"},
                                    {"type": "text", "text": "Patch the validator."}], "2026-11-01T10:00:05Z"),
    ])
    assert env.sync()["sources"]["claude_code"]["written"] == 2
    c = env.conn()
    assert query.search(c, "secret") == []  # thinking is not stored

    with open(p, "a", encoding="utf-8") as f:  # a line still being written
        f.write('{"type":"user","uuid":"u3","sessionId":"s1"')
    assert env.sync()["sources"]["claude_code"]["written"] == 0
    with open(p, "a", encoding="utf-8") as f:
        f.write(',"timestamp":"2026-11-01T10:01:00Z","message":{"role":"user","content":"thanks, works"}}\n')
    assert env.sync()["sources"]["claude_code"]["written"] == 1

    env.claude_session("C--dev-shop", "s1", [cc_line("u9", "user", "rewritten", "2026-11-01T11:00:00Z")], mode="w")
    assert env.sync()["sources"]["claude_code"]["written"] == 1


def test_claude_code_tool_output_is_clipped_and_hidden_from_default_search(env):
    env.claude_session("C--dev-shop", "s1", [
        cc_line("t1", "user", [{"type": "tool_result", "content": "needle " + "x" * 5000}], "2026-11-01T10:00:00Z"),
    ])
    env.sync()
    c = env.conn()
    assert query.search(c, "needle") == []
    hit = query.search(c, "needle", include_tool_output=True)[0]
    assert len(query.get_item(c, hit["id"])["body"]) < 1700


def test_codex_formats_and_injected_context(env):
    env.codex_rollout("new", [
        {"timestamp": "2026-11-01T12:00:00Z", "type": "session_meta", "payload": {"id": "cx-1", "cwd": r"C:\dev\shop"}},
        {"timestamp": "2026-11-01T12:00:01Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
        {"timestamp": "2026-11-01T12:00:02Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Refactor the payment module"}]}},
        {"timestamp": "2026-11-01T12:00:03Z", "type": "event_msg", "payload": {"type": "user_message", "message": "dup"}},
        {"timestamp": "2026-11-01T12:00:04Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "arguments": '{"command":["bash","-lc","npm test"]}'}},
    ])
    env.codex_rollout("old", [
        {"id": "old-1", "timestamp": "2025-04-01T10:00:00Z", "instructions": ""},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "early format works"}]},
    ])
    env.sync()
    c = env.conn()
    rows = c.execute("SELECT kind, body, cwd, thread_id FROM items WHERE source='codex' ORDER BY rid").fetchall()
    bodies = [r["body"] for r in rows]
    assert not any("environment_context" in b or b == "dup" for b in bodies)
    assert any("Refactor the payment module" == b for b in bodies)
    assert any(r["kind"] == "tool_call" and "npm test" in r["body"] for r in rows)
    assert {r["thread_id"] for r in rows} == {"cx-1", "old-1"}
    assert all(r["cwd"] == r"C:\dev\shop" for r in rows if r["thread_id"] == "cx-1")


def test_project_rules_cwd_jira_and_whole_session(env):
    env.projects = [{"id": "shop", "name": "Shop", "cwd": ["*\\dev\\shop*"], "jira": ["SHOP"]}]
    env.claude_session("C--dev-shop", "s1", [cc_line("u1", "user", "hello", "2026-11-01T10:00:00Z")])
    env.claude_session("C--dev-other", "s2", [
        cc_line("v1", "user", "unrelated start", "2026-11-01T10:00:00Z", session="s2", cwd=r"C:\dev\other"),
        cc_line("v2", "user", "this is about SHOP-123", "2026-11-01T10:01:00Z", session="s2", cwd=r"C:\dev\other"),
    ])
    env.sync()
    c = env.conn()
    tagged = {r[0] for r in c.execute("SELECT item_id FROM item_projects WHERE project_id='shop'")}
    assert {"claude_code:u1", "claude_code:v1", "claude_code:v2"} <= tagged
