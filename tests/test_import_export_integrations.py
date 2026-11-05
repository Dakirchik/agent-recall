import json
import sqlite3
import tomllib
import zipfile

from agent_recall import integrations, query
from agent_recall.export import backup_db, export_jsonl
from agent_recall.importers import import_export, load_conversations


def _claude_zip(path):
    convs = [{"uuid": "c1", "name": "Payments", "created_at": "2026-10-28T09:00:00Z", "chat_messages": [
        {"uuid": "m1", "sender": "human", "created_at": "2026-10-28T09:00:00Z", "text": "",
         "content": [{"type": "text", "text": "Why does SHOP-7 fail?"}],
         "attachments": [{"file_name": "log.txt", "extracted_content": "gateway timeout"}]},
        {"uuid": "m2", "sender": "assistant", "created_at": "2026-10-28T09:01:00Z", "text": "",
         "content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "The gateway times out."}]},
    ]}]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data/conversations.json", json.dumps(convs))
    return path


def test_claude_export_import_is_idempotent(env, tmp_path):
    c = env.conn()
    z = _claude_zip(tmp_path / "export.zip")
    first = import_export(c, z)
    assert first["format"] == "claude" and first["written"] == 3
    assert import_export(c, z)["written"] == 0
    assert query.search(c, "gateway timeout", source="claude_app")
    assert not query.search(c, "hidden")


def test_folder_of_export_zips_is_read_without_extraction(tmp_path):
    for n, cid in enumerate(("one", "two", "one")):
        with zipfile.ZipFile(tmp_path / f"conversations-{n:03d}.zip", "w") as z:
            z.writestr("conversations.json", json.dumps([{"uuid": cid, "chat_messages": []}]))
    assert [c["uuid"] for c in load_conversations(tmp_path)] == ["one", "two"]


def test_chatgpt_export_follows_current_branch(env, tmp_path):
    convs = [{"title": "SQLite", "conversation_id": "g1", "current_node": "n4", "mapping": {
        "n1": {"message": None, "parent": None},
        "n2": {"message": {"id": "n2", "author": {"role": "user"}, "create_time": 1761800010.0,
                           "content": {"content_type": "text", "parts": ["open sqlite in a browser?"]}}, "parent": "n1"},
        "n3": {"message": {"id": "n3", "author": {"role": "assistant"},
                           "content": {"content_type": "text", "parts": ["DISCARDED answer"]}}, "parent": "n2"},
        "n4": {"message": {"id": "n4", "author": {"role": "assistant"},
                           "content": {"content_type": "text", "parts": ["Use datasette."]}}, "parent": "n2"},
    }}]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(convs), encoding="utf-8")
    c = env.conn()
    assert import_export(c, p)["format"] == "chatgpt"
    assert query.search(c, "datasette") and not query.search(c, "DISCARDED")


def test_backup_and_jsonl(env, tmp_path):
    env.note("a.md", "hello world")
    env.sync()
    c = env.conn()
    dest = backup_db(c, str(tmp_path / "out") + "/")
    assert dest.name == "brain.db"
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    path, n = export_jsonl(c, tmp_path / "all.jsonl")
    assert n == 1 and json.loads(path.read_text(encoding="utf-8").splitlines()[0])["body"] == "hello world"


def test_codex_integration_keeps_foreign_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\nargs = ["a"]\n', encoding="utf-8")
    integrations.codex_add(r"C:\Users\me\AppData\Local\agent-recall\venv\Scripts\python.exe")
    integrations.codex_add(r"C:\other\python.exe")  # re-adding replaces, never duplicates
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["brain"]["command"] == r"C:\other\python.exe"
    assert data["mcp_servers"]["other"]["command"] == "x"
    integrations.codex_remove()
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert "brain" not in data["mcp_servers"] and data["model"] == "gpt-5"
