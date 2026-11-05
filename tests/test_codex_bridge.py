import json
import os
import zipfile
from pathlib import Path

import pytest

from agent_recall.codex_bridge import AppServer, _safe_path, install, prepare, prepare_cached, rollout, verify


def sample():
    return {"uuid": "chat-one", "name": "Example chat", "created_at": "2026-10-01T10:00:00Z",
            "chat_messages": [
                {"uuid": "m1", "sender": "human", "text": "Please help", "created_at": "2026-10-01T10:00:00Z"},
                {"uuid": "m2", "sender": "assistant", "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "Here is artifact-1"},
                    {"type": "tool_use", "name": "shell", "input": {"command": "do not execute"}}],
                 "created_at": "2026-10-01T10:01:00Z"}]}


def make_bundle(root):
    root.mkdir()
    (root / "conversations.json").write_text(json.dumps([sample()]), encoding="utf-8")
    with zipfile.ZipFile(root / "projects-000.zip", "w") as z:
        z.writestr("projects/p1.json", json.dumps({"uuid": "p1", "name": "Project",
            "prompt_template": "Original project instructions", "docs": [
                {"filename": "../AGENTS.md", "content": "Project reference text"}]}))
    with zipfile.ZipFile(root / "frames-000.zip", "w") as z:
        z.writestr("artifacts/artifact-1/artifact.json", json.dumps({"id": "artifact-1", "kind": "code",
            "active_version": "v1", "versions": [{"id": "v1", "title": "Example"}]}))
        z.writestr("artifacts/artifact-1/versions/v1.html", "<html>Example</html>")
    return root


def test_prepare_preserves_assets_and_does_not_activate_instructions(tmp_path):
    source = make_bundle(tmp_path / "source")
    output = tmp_path / "prepared"
    m = prepare(source, output)
    assert len(m["threads"]) == len(m["projects"]) == len(m["artifacts"]) == 1
    assert m["threads"][0]["artifact_ids"] == ["artifact-1"]
    assert not list(output.rglob("AGENTS.md"))
    assert (output / "archive/artifacts/artifact-1/versions/v1.html").read_text() == "<html>Example</html>"
    original = json.loads((output / "conversations/chat-one/conversation.json").read_text())
    assert original == sample()
    rows = [json.loads(s) for s in Path(m["threads"][0]["rollout"]).read_text().splitlines()]
    response = [r["payload"] for r in rows if r["type"] == "response_item"]
    assert [r["role"] for r in response] == ["user", "assistant"]
    assert "do not execute" in response[1]["content"][0]["text"]
    assert "private reasoning" not in json.dumps(rows)
    assert all(r["type"] == "message" for r in response)
    assert len([r for r in rows if r["payload"].get("type") == "task_complete"]) == 1
    with pytest.raises(FileExistsError):
        prepare(source, output)


@pytest.mark.parametrize("name", ["../bad", "C:/bad", "\\\\host\\bad", "a/../../bad", "a\\..\\..\\bad"])
def test_archive_paths_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError):
        _safe_path(tmp_path, name)


def test_deterministic_ids_and_ordered_turns(tmp_path):
    c = sample()
    c["chat_messages"] += [{"sender": "human", "text": "Next"}, {"sender": "assistant", "text": "Done"}]
    rows = rollout(c, tmp_path, "id")
    assert [r["ordinal"] for r in rows] == list(range(len(rows)))
    assert len([r for r in rows if r["payload"].get("type") == "task_started"]) == 2
    assert "base_instructions" not in rows[0]["payload"]
    bundle = make_bundle(tmp_path / "source")
    conversation = sample()
    conversation["chat_messages"][0]["text"] += "\u2028Unicode line separator"
    (bundle / "conversations.json").write_text(json.dumps([conversation]), encoding="utf-8")
    a, b = prepare(bundle, tmp_path / "one"), prepare(bundle, tmp_path / "two")
    assert a["threads"][0]["thread_id"] == b["threads"][0]["thread_id"]


def test_installer_never_overwrites_existing_session(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path / "source")
    m = prepare(bundle, tmp_path / "prepared")
    home = tmp_path / "codex"
    dest = home / "sessions/2026/10/01" / Path(m["threads"][0]["rollout"]).name
    dest.parent.mkdir(parents=True)
    dest.write_text("existing continued conversation")

    class FakeServer:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def call(self, method, params):
            assert method == "thread/list"
            return {"data": [], "nextCursor": None}

    monkeypatch.setattr("agent_recall.codex_bridge.AppServer", FakeServer)
    with pytest.raises(FileExistsError):
        install(tmp_path / "prepared/manifest.json", "codex", home)
    assert dest.read_text() == "existing continued conversation"


def test_cached_preparation_reuses_archive_and_rejects_other_formats(tmp_path):
    source = make_bundle(tmp_path / "source")
    first = prepare_cached(source, tmp_path / "imports")
    assert prepare_cached(source, tmp_path / "imports") == first
    with zipfile.ZipFile(source / "frames-000.zip", "a") as z:
        z.writestr("artifacts/artifact-1/page.md", "Updated artifact")
    assert prepare_cached(source, tmp_path / "imports") != first
    other = tmp_path / "chatgpt.json"
    other.write_text(json.dumps([{"id": "other", "mapping": {}}]))
    with pytest.raises(ValueError, match="Claude"):
        prepare(other, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_json_extracted_one_folder_deeper_keeps_companion_archives(tmp_path):
    source = make_bundle(tmp_path / "source")
    nested = source / "conversations-000"
    nested.mkdir()
    (source / "conversations.json").rename(nested / "conversations.json")
    manifest = prepare(nested / "conversations.json", tmp_path / "prepared")
    assert len(manifest["artifacts"]) == 1 and len(manifest["projects"]) == 1


@pytest.mark.skipif(not os.environ.get("CODEX_BRIDGE_TEST_BINARY"), reason="requires local Codex binary")
def test_real_codex_indexes_and_reads_import(tmp_path):
    binary = os.environ["CODEX_BRIDGE_TEST_BINARY"]
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")
    desktop_state = home / ".codex-global-state.json"
    original_desktop_state = b'{"local-projects": {"existing": {"name": "Keep me"}}}'
    desktop_state.write_bytes(original_desktop_state)
    bundle = make_bundle(tmp_path / "source")
    conversation = sample()
    conversation["chat_messages"][0]["text"] += "\u2028Unicode line separator"
    (bundle / "conversations.json").write_text(json.dumps([conversation]), encoding="utf-8")
    m = prepare(bundle, tmp_path / "prepared")
    receipt = install(tmp_path / "prepared/manifest.json", binary, home)
    assert len(receipt["threads"]) == 1
    assert "projects" not in receipt and "archive_project_id" not in receipt
    assert desktop_state.read_bytes() == original_desktop_state
    tid = m["threads"][0]["thread_id"]
    # Re-preparing the same export elsewhere must reuse the native session.
    prepare(bundle, tmp_path / "other-preparation")
    reused = install(tmp_path / "other-preparation/manifest.json", binary, home)
    assert reused["threads"][0]["thread_id"] == tid
    assert Path(reused["threads"][0]["path"]).samefile(receipt["threads"][0]["path"])
    assert verify(tmp_path / "other-preparation/manifest.json", binary, home)["threads"] == 1
    with AppServer(binary, home) as server:
        turns = server.call("thread/turns/list", {"threadId": tid, "itemsView": "full"})["data"]
        serialized = json.dumps(turns)
        assert "Please help" in serialized and "Here is artifact-1" in serialized
        assert "private reasoning" not in serialized
        assert server.call("project/list", {})["data"] == []
        assert server.call("thread/read", {"threadId": tid})["thread"]["projectId"] is None
    again = install(tmp_path / "prepared/manifest.json", binary, home)
    assert again == receipt
    # Older receipts can contain now-removed project metadata. Even missing old
    # projects must not prevent reading or re-importing the saved conversations.
    receipt_path = next((tmp_path / "prepared").glob("installed-*.json"))
    legacy = dict(receipt, projects=[{"project_id": "deleted-project"}], archive_project_id="deleted-archive")
    receipt_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert install(tmp_path / "prepared/manifest.json", binary, home) == legacy
    report = verify(tmp_path / "prepared/manifest.json", binary, home)
    assert report["threads"] == 1 and report["messages"] == 2 and report["exact_transcripts"]
    assert "projects" not in report
    assert desktop_state.read_bytes() == original_desktop_state


@pytest.mark.skipif(not os.environ.get("CODEX_BRIDGE_TEST_BINARY"), reason="requires local Codex binary")
def test_incremental_install_keeps_chats_unassigned(tmp_path):
    binary = os.environ["CODEX_BRIDGE_TEST_BINARY"]
    source = make_bundle(tmp_path / "source")
    second = sample()
    second.update(uuid="chat-two", name="Second chat")
    (source / "conversations.json").write_text(json.dumps([sample(), second]))
    prepare(source, tmp_path / "prepared")
    path, home = tmp_path / "prepared/manifest.json", tmp_path / "codex"
    home.mkdir()
    first = install(path, binary, home, limit=1)
    both = install(path, binary, home)
    assert len(first["threads"]) == 1 and len(both["threads"]) == 2
    with AppServer(binary, home) as server:
        members = server.call("thread/list", {"projectId": None, "limit": 100})["data"]
        assert {t["id"] for t in members} == {t["thread_id"] for t in both["threads"]}
        assert all(t["projectId"] is None for t in members)
        assert server.call("project/list", {})["data"] == []
