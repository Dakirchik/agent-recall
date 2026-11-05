"""Experimental Claude web export -> native Codex rollouts.

Preparation never modifies Codex. Installation uses app-server rather than editing
its databases. Original exports are data, never executable project instructions.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .importers import CLAUDE_COMPANION_ARCHIVES, detect_format, export_bundle, load_conversations
from .i18n import _

NAMESPACE = uuid.UUID("eb52a138-e086-4c21-9334-bac3a178902b")


def _json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _slug(value: str) -> str:
    return re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-")[:80] or "untitled"


def _date(value) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _iso(value) -> str:
    return _date(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_path(root: Path, name: str) -> Path:
    # ZIP names are portable, even when read on Windows. Reject drive paths too.
    rel = PurePosixPath(name.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or any(":" in p for p in rel.parts):
        raise ValueError(_("Unsafe archive entry: {name}", name=repr(name)))
    dest = root.joinpath(*rel.parts).resolve()
    if not dest.is_relative_to(root.resolve()):
        raise ValueError(_("Unsafe archive entry: {name}", name=repr(name)))
    return dest


def _text(message: dict) -> str:
    parts = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind in ("tool_use", "tool_result"):
            # Text only: never replay foreign tool calls as executable Codex calls.
            parts.append("[Claude " + kind + "]\n" + json.dumps(block, ensure_ascii=False))
        elif kind in ("image", "document"):
            parts.append("[Claude " + kind + "; original metadata in source conversation.json]")
    if not parts and message.get("text"):
        parts.append(message["text"])
    for attachment in message.get("attachments") or []:
        parts.append("[attachment: " + str(attachment.get("file_name", "attachment")) + "]\n"
                     + str(attachment.get("extracted_content") or ""))
    for file in message.get("files") or []:
        parts.append("[file metadata] " + json.dumps(file, ensure_ascii=False))
    return "\n\n".join(parts).strip()


def rollout(conversation: dict, cwd: Path, thread_id: str) -> list[dict]:
    """Mirror the paginated rollout emitted by Codex's external-agent importer."""
    ts = _iso(conversation.get("created_at"))
    rows = []

    def add(typ, payload, stamp=ts):
        rows.append({"timestamp": stamp, "ordinal": len(rows), "type": typ, "payload": payload})

    add("session_meta", {
        "id": thread_id, "session_id": thread_id, "timestamp": ts,
        "cwd": str(cwd.resolve()), "originator": "agent-recall Claude export import",
        "cli_version": "0.166.0", "source": "vscode", "model_provider": "openai",
        "history_mode": "paginated", "multi_agent_version": "v1",
        "context_window": {"window_id": str(uuid.uuid5(NAMESPACE, thread_id + ":window"))},
        # Omit base_instructions so Codex uses its own default system prompt.
        # Never promote instructions from the exported conversation to system rules.
    })
    turn_id = None
    started = None
    last_ts = ts

    def finish():
        if turn_id:
            add("event_msg", {"type": "task_complete", "turn_id": turn_id,
                              "last_agent_message": None, "started_at": started}, last_ts)

    for i, message in enumerate(conversation.get("chat_messages") or []):
        if message.get("sender") not in ("human", "assistant"):
            continue
        text = _text(message)
        if not text:
            continue
        stamp = _iso(message.get("created_at") or conversation.get("created_at"))
        role = "user" if message["sender"] == "human" else "assistant"
        if role == "user" or turn_id is None:
            finish()
            turn_id = f"claude-import-turn-{i}"
            started = int(_date(stamp).timestamp())
            add("event_msg", {"type": "task_started", "turn_id": turn_id,
                              "started_at": started, "model_context_window": None,
                              "collaboration_mode_kind": "default"}, stamp)
        item_id = f"claude-import-item-{i}"
        item = {"type": "UserMessage" if role == "user" else "AgentMessage", "id": item_id,
                "content": [{"type": "text", "text": text, "text_elements": []}]
                if role == "user" else [{"type": "Text", "text": text}]}
        add("event_msg", {"type": "item_completed", "thread_id": thread_id, "turn_id": turn_id,
                          "item": item, "completed_at_ms": int(_date(stamp).timestamp() * 1000)}, stamp)
        add("response_item", {"type": "message", "role": role,
                              "content": [{"type": "input_text" if role == "user" else "output_text",
                                           "text": text}]}, stamp)
        last_ts = stamp
    finish()
    return rows


def prepare(source: str | Path, output: str | Path) -> dict:
    """Preserve archive assets and generate importable threads, without installing."""
    source, output = Path(source).resolve(), Path(output).resolve()
    # No overwrites: re-import is explicit, installed conversations stay immutable.
    conversations = load_conversations(source)
    if detect_format(conversations) != "claude":
        raise ValueError(_("Choose a Claude data export to transfer chats into Codex"))
    output.mkdir(parents=True, exist_ok=False)
    bundle = export_bundle(source)
    projects, artifacts, preserved = [], [], []
    assets = output / "archive"
    for pattern in CLAUDE_COMPANION_ARCHIVES:
        for archive in sorted(bundle.glob(pattern)):
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    dest = _safe_path(assets, info.filename)
                    data = z.read(info)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() and dest.read_bytes() != data:
                        raise ValueError(_("Conflicting archive entry: {name}", name=info.filename))
                    dest.write_bytes(data)
                    preserved.append(info.filename)
                    if info.filename.startswith("projects/") and dest.suffix == ".json":
                        project = json.loads(data)
                        pid = str(project["uuid"])
                        folder = output / "projects" / (_slug(project.get("name") or "Untitled") + "-" + _slug(pid))
                        folder.mkdir(parents=True, exist_ok=True)
                        _json(folder / "source-project.json", project)
                        docs = []
                        for i, doc in enumerate(project.get("docs") or []):
                            name = f"{i + 1:03d}-" + _slug(doc.get("filename") or "document") + ".txt"
                            (folder / name).write_text(str(doc.get("content") or ""), encoding="utf-8")
                            docs.append(name)
                        # Deliberately not AGENTS.md: imported prompts need human review.
                        (folder / "README.md").write_text(
                            "# " + (project.get("name") or "Untitled") + "\n\n"
                            + (project.get("description") or "") + "\n\n"
                            + "Imported Claude project. Original prompt and metadata: source-project.json.\n"
                            + "No conversation-to-project links were inferred.\n", encoding="utf-8")
                        projects.append({"id": pid, "name": project.get("name") or "Untitled",
                                         "folder": str(folder), "documents": docs})
                    if info.filename.endswith("/artifact.json"):
                        artifact = json.loads(data)
                        artifacts.append({"id": artifact.get("id"), "kind": artifact.get("kind"),
                                          "folder": str(dest.parent), "versions": artifact.get("versions") or [],
                                          "active_version": artifact.get("active_version")})
    threads = []
    for c in conversations:
        if not c.get("uuid"):
            raise ValueError(_("Conversation without uuid"))
        cid = str(c["uuid"])
        thread_id = str(uuid.uuid5(NAMESPACE, "claude.ai:" + cid))
        folder = output / "conversations" / _slug(cid)
        folder.mkdir(parents=True)
        _json(folder / "conversation.json", c)
        serialized = json.dumps(c, ensure_ascii=False)
        links = [a["id"] for a in artifacts if a["id"] and a["id"] in serialized]
        rows = rollout(c, output, thread_id)
        dest = folder / f"rollout-{_date(c.get('created_at')).strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
        dest.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        messages = [r["payload"] for r in rows if r["type"] == "response_item"]
        (folder / "transcript.md").write_text(
            "# " + (c.get("name") or "Untitled") + "\n\n" + "\n\n".join(
                "## " + m["role"] + "\n\n" + m["content"][0]["text"] for m in messages), encoding="utf-8")
        threads.append({"claude_id": cid, "thread_id": thread_id,
                        "title": "[Claude] " + (c.get("name") or "Untitled"),
                        "rollout": str(dest), "cwd": str(output), "messages": len(messages),
                        "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                        "artifact_ids": links, "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
    manifest = {"version": 1, "source": str(source), "prepared_at": datetime.now(timezone.utc).isoformat(),
                "threads": threads, "projects": projects, "artifacts": artifacts,
                "preserved_files": sorted(set(preserved)),
                "limitations": ["Project membership absent from conversations.json; no membership inferred.",
                                "Images are placeholders unless their files exist in the export.",
                                "Claude thinking stays in source JSON only; tools are inert transcript text.",
                                "Memories and project prompts are archived, not activated as instructions."]}
    _json(output / "manifest.json", manifest)
    (output / "README.md").write_text(
        "# Claude export\n\n## Conversations\n\n" + "\n".join(
            f"- [{t['title']}](conversations/{_slug(t['claude_id'])}/transcript.md)" for t in threads)
        + "\n\n## Projects\n\n" + "\n".join(
            f"- [{p['name']}]({Path(p['folder']).relative_to(output).as_posix()}/README.md)" for p in projects)
        + "\n\n## Artifacts\n\n" + "\n".join(
            f"- [{a['id']}]({Path(a['folder']).relative_to(output).as_posix()}/artifact.json)" for a in artifacts)
        + "\n", encoding="utf-8")
    return manifest


def prepare_cached(source: str | Path, archive_root: str | Path) -> Path:
    """Reuse the same permanent preparation when the user selects an export again."""
    source = Path(source).resolve()
    conversations = load_conversations(source)
    if detect_format(conversations) != "claude":
        raise ValueError(_("Choose a Claude data export to transfer chats into Codex"))
    digest = hashlib.sha256(json.dumps(conversations, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    bundle = export_bundle(source)
    for pattern in CLAUDE_COMPANION_ARCHIVES:
        for archive in sorted(bundle.glob(pattern)):
            digest.update(archive.name.encode("utf-8"))
            with archive.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
    folder = Path(archive_root).resolve() / ("claude-" + digest.hexdigest()[:20])
    manifest = folder / "manifest.json"
    if not manifest.exists():
        prepare(source, folder)
    return manifest


class AppServer:
    """Small bounded stdio JSON-RPC client. Never calls turn/start or an LLM."""

    def __init__(self, executable: str | Path, home: str | Path, timeout=45):
        self.timeout, self.counter = timeout, 0
        env = dict(os.environ, CODEX_HOME=str(Path(home).resolve()))
        self.process = subprocess.Popen(
            [str(executable), "app-server", "--listen", "stdio://"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.responses = queue.Queue()

        def read():
            try:
                for line in self.process.stdout:
                    self.responses.put(json.loads(line))
            finally:
                self.responses.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def __enter__(self):
        try:
            self.call("initialize", {"clientInfo": {"name": "agent_recall_import", "version": "0.4.0"},
                                     "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _send(self, obj):
        self.process.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.counter += 1
        request_id = self.counter
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                obj = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(_("Codex app-server timeout: {method}", method=method)) from None
            if obj is None:
                raise RuntimeError(_("Codex app-server exited during {method}", method=method))
            if obj.get("id") == request_id:
                if "error" in obj:
                    raise RuntimeError(f"{method}: {obj['error']}")
                return obj.get("result")
            if "id" in obj and "method" in obj:
                self._send({"id": obj["id"], "error": {"code": -32601, "message": "Import client cannot run tools"}})

    def __exit__(self, *args):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()
        self.reader.join(timeout=2)


def install(manifest_path: str | Path, executable: str | Path, home: str | Path,
            limit: int | None = None) -> dict:
    """Copy new rollouts only, let Codex index them, and verify visible history."""
    manifest_path, home = Path(manifest_path).resolve(), Path(home).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if limit is not None and limit < 1:
        raise ValueError(_("limit must be positive"))
    selected = [t for t in manifest["threads"] if t["messages"]][:limit]
    receipt_path = manifest_path.parent / ("installed-" + hashlib.sha256(str(home).encode()).hexdigest()[:12] + ".json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {
        "codex_home": str(home), "threads": []}
    known = {t["thread_id"] for t in receipt["threads"]}
    # Preflight before starting the server or writing into the destination.
    for thread in selected:
        if thread["thread_id"] in known:
            continue
        src = Path(thread["rollout"]).resolve()
        if not src.is_relative_to(manifest_path.parent):
            raise ValueError(_("Rollout must be inside the prepared archive"))
        if hashlib.sha256(src.read_bytes()).hexdigest() != thread["sha256"]:
            raise ValueError(_("Prepared rollout changed: {name}", name=src.name))
    backup = manifest_path.parent / ("backup-" + hashlib.sha256(str(home).encode()).hexdigest()[:12])
    backup.mkdir(exist_ok=True)
    for pattern in ("state_*.sqlite", "thread_history_*.sqlite"):
        for src in home.glob(pattern):
            target = backup / src.name
            if target.exists():
                continue
            # SQLite backup sees committed WAL changes; copying .sqlite alone does not.
            deadline = time.monotonic() + 30

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError(_("Codex database backup timed out"))

            with sqlite3.connect(src.as_uri() + "?mode=ro", uri=True) as original:
                with sqlite3.connect(target) as snapshot:
                    original.backup(snapshot, pages=200, progress=progress)
    src, target = home / "session_index.jsonl", backup / "session_index.jsonl"
    if src.exists() and not target.exists():
        shutil.copy2(src, target)
    with AppServer(executable, home) as server:
        existing = {}
        pending = {t["thread_id"] for t in selected} - known
        if pending:
            # An export may have been prepared in a different folder before.
            # Reuse those sessions, including archived ones, without moving them.
            for archived in (False, True):
                cursor = None
                while True:
                    page = server.call("thread/list", {"archived": archived, "sourceKinds": ["vscode"],
                                                       "limit": 100, "cursor": cursor, "useStateDbOnly": True})
                    existing.update((t["id"], t) for t in page["data"] if t["id"] in pending)
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
        for thread in selected:
            tid = thread["thread_id"]
            if tid in known:
                server.call("thread/read", {"threadId": tid})
                continue
            if tid in existing:
                native = existing[tid]
                receipt["threads"].append({"thread_id": tid, "title": native.get("name") or thread["title"],
                                            "path": native.get("path")})
                known.add(tid)
                _json(receipt_path, receipt)
                continue
            src = Path(thread["rollout"])
            if hashlib.sha256(src.read_bytes()).hexdigest() != thread["sha256"]:
                raise ValueError(_("Prepared rollout changed: {name}", name=src.name))
            day = _date(thread["created_at"]).strftime("%Y/%m/%d")
            dest = home / "sessions" / day / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Never overwrite any existing native or previously continued session.
            try:
                with dest.open("xb") as f:
                    f.write(src.read_bytes())
            except FileExistsError:
                if dest.read_bytes() != src.read_bytes():
                    raise FileExistsError(_("Existing Codex session differs: {path}", path=dest)) from None
            result = server.call("thread/resume", {"threadId": tid, "path": str(dest), "excludeTurns": True})
            if result["thread"]["id"] != tid:
                raise RuntimeError(_("Codex returned an unexpected thread id"))
            server.call("thread/name/set", {"threadId": tid, "name": thread["title"]})
            turns = server.call("thread/turns/list", {"threadId": tid, "limit": 1, "itemsView": "full"})
            if not turns.get("data"):
                raise RuntimeError(_("Codex has no visible turns for {id}", id=tid))
            visible = server.call("thread/list", {"searchTerm": thread["title"], "limit": 100,
                                                   "sourceKinds": ["vscode"], "useStateDbOnly": True})
            if not any(t["id"] == tid for t in visible["data"]):
                raise RuntimeError(_("Codex did not index {id}", id=tid))
            receipt["threads"].append({"thread_id": tid, "title": thread["title"], "path": str(dest)})
            known.add(tid)
            _json(receipt_path, receipt)
    return receipt


def verify(manifest_path: str | Path, executable: str | Path, home: str | Path) -> dict:
    """Read every installed UI turn and compare its text with the prepared source."""
    manifest_path, home = Path(manifest_path).resolve(), Path(home).resolve()
    key = hashlib.sha256(str(home).encode()).hexdigest()[:12]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads((manifest_path.parent / f"installed-{key}.json").read_text(encoding="utf-8"))
    expected = {t["thread_id"]: t for t in manifest["threads"]}
    count = 0
    checked = []
    with AppServer(executable, home) as server:
        for entry in receipt["threads"]:
            tid = entry["thread_id"]
            prepared = Path(expected[tid]["rollout"])
            with prepared.open(encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            original = [r["payload"] for r in rows if r["type"] == "response_item"]
            original = [(m["role"], m["content"][0]["text"]) for m in original]
            actual = []
            cursor = None
            while True:
                page = server.call("thread/turns/list", {
                    "threadId": tid, "limit": 50, "itemsView": "full", "sortDirection": "asc", "cursor": cursor})
                for turn in page["data"]:
                    for item in turn["items"]:
                        if item["type"] == "userMessage":
                            actual.append(("user", "\n".join(c["text"] for c in item["content"] if c["type"] == "text")))
                        elif item["type"] == "agentMessage":
                            actual.append(("assistant", item["text"]))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if actual != original:
                raise RuntimeError(_("Codex transcript differs from prepared source: {id}", id=tid))
            count += len(actual)
            checked.append(tid)
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "threads": len(checked),
              "messages": count, "exact_transcripts": True}
    _json(manifest_path.parent / f"verification-{key}.json", report)
    return report
