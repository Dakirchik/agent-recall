"""The full-screen UI driven by scripted keys (no real terminal)."""
import io
import json
import errno
import zipfile
import tomllib
from types import SimpleNamespace

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


@pytest.mark.parametrize("prefix", [[], ["down", "enter"]])
def test_ctrl_c_exits_cleanly_and_restores_terminal(filled, prefix):
    def interrupted_keys():
        yield from prefix
        raise KeyboardInterrupt

    t = term(interrupted_keys())
    assert appmod.run(filled.config(), t) == 130
    assert not t.active
    output = t.out.getvalue()
    assert output.endswith("\x1b[0m\x1b[?25h\x1b[?1049l")
    assert "Traceback" not in output


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
    keys = ["down"] * 7 + ["enter", "enter", "esc", "esc", "q"]
    run(filled, keys)
    assert calls == ["add"]


def test_auto_sync_enable_and_remove(filled, monkeypatch):
    from agent_recall import scheduler
    state = scheduler.State("test scheduler")
    calls = []
    monkeypatch.setattr(scheduler.Scheduler, "status", lambda self: state)
    def enable(self, interval):
        calls.append(("enable", interval))
        state.enabled, state.installed, state.interval = True, True, interval
    def disable(self):
        calls.append(("disable",))
        state.enabled = state.installed = False
    monkeypatch.setattr(scheduler.Scheduler, "enable", enable)
    monkeypatch.setattr(scheduler.Scheduler, "disable", disable)
    # Home → Auto-sync → Enable → replace interval → message → Disable → message → back.
    keys = ["down"] * 6 + ["enter", "enter", "delete"] + list("30") + [
        "enter", "esc", "down", "enter", "esc", "esc", "q"]
    t = run(filled, keys)
    assert calls == [("enable", 30), ("disable",)]
    text = "".join(frames(t))
    assert "Disabled" in text and "Enabled" in text
    assert "removes" in text and "Export ZIPs" in text


def test_auto_sync_error_does_not_crash_ui(filled, monkeypatch):
    from agent_recall import scheduler
    def unavailable(self):
        raise scheduler.ScheduleError("permission denied")
    monkeypatch.setattr(scheduler.Scheduler, "status", unavailable)
    t = run(filled, ["down"] * 6 + ["enter", "esc", "q"])
    assert "permission denied" in "".join(frames(t))


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("system,command", [("windows", "taskschd.msc"),
                                           ("macos", "launchctl print"), ("linux", "crontab -l")])
def test_scheduler_help_from_home_is_read_only_and_selects_current_os(env, monkeypatch, language, system, command):
    from agent_recall import scheduler, scheduler_help
    from agent_recall.i18n import set_lang
    set_lang(language)
    monkeypatch.setattr(scheduler_help, "current_os", lambda: system)
    monkeypatch.setattr(scheduler.Scheduler, "status", lambda self: pytest.fail("Help must not query the scheduler"))
    monkeypatch.setattr(scheduler.Scheduler, "enable", lambda *args: pytest.fail("Help must not enable tasks"))
    monkeypatch.setattr(scheduler.Scheduler, "disable", lambda *args: pytest.fail("Help must not disable tasks"))
    # Home → Help → the already selected current OS → read all pages → back to Home.
    keys = ["down"] * 10 + ["enter", "enter"] + ["pgdn"] * 8 + ["esc", "esc", "q"]
    t = term(keys, size=(80, 24))
    appmod.App(env.config(), t).run()
    text = "".join(frames(t))
    assert command in text
    assert ("this OS" if language == "en" else "эта ОС") in text
    assert ("After disabling in brain" if language == "en" else "После отключения в brain") in text
    assert "brain.log" in text


def test_scheduler_help_can_show_another_os(env, monkeypatch):
    from agent_recall import scheduler_help
    monkeypatch.setattr(scheduler_help, "current_os", lambda: "windows")
    t = term(["down"] * 10 + ["enter", "down", "enter", "esc", "esc", "q"])
    appmod.App(env.config(), t).run()
    text = "".join(frames(t))
    assert "Check the scheduler: macOS" in text
    assert "~/Library/LaunchAgents" in text


def test_scheduler_help_from_auto_sync_does_not_change_schedule(env, monkeypatch):
    from agent_recall import scheduler
    monkeypatch.setattr(scheduler.Scheduler, "status", lambda self: scheduler.State("test"))
    monkeypatch.setattr(scheduler.Scheduler, "enable", lambda *args: pytest.fail("Unexpected enable"))
    monkeypatch.setattr(scheduler.Scheduler, "disable", lambda *args: pytest.fail("Unexpected disable"))
    keys = ["down"] * 6 + ["enter", "down", "down", "enter", "enter", "esc", "esc", "esc", "q"]
    t = run(env, keys)
    text = "".join(frames(t))
    assert "Check the scheduler:" in text
    assert "Schedule updated" not in text


def test_language_switch_writes_config(filled):
    keys = ["down"] * 9 + ["enter", "down", "enter", "q"]
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


def export_file(env):
    path = env.root / "export.json"
    path.write_text(json.dumps([{"uuid": "imported-chat", "name": "Imported example",
        "created_at": "2026-10-01T10:00:00Z", "chat_messages": [
            {"uuid": "imported-message", "sender": "human", "text": "A useful imported question"}]}]), encoding="utf-8")
    return path


@pytest.mark.parametrize("language,provider,steps,link", [
    ("en", "claude", "Settings → Privacy", "https://claude.ai/settings/data-privacy-controls"),
    ("ru", "claude", "Дождитесь письма", "https://claude.ai/settings/data-privacy-controls"),
    ("en", "chatgpt", "Data controls", "https://chatgpt.com/#settings/DataControls"),
])
def test_import_guides_are_available_before_choosing_a_file(env, language, provider, steps, link):
    from agent_recall.i18n import set_lang
    set_lang(language)
    keys = ["down"] * 5 + ["enter"] + ["down"] * (2 if provider == "claude" else 3) + ["enter", "enter", "esc", "q"]
    t = term(keys, size=(80, 24))
    appmod.App(env.config(), t).run()
    text = "".join(frames(t))
    assert steps in text and link in text
    assert "24" in text


def test_import_memory_still_populates_sessions_and_search(env):
    path = export_file(env)
    keys = ["down"] * 5 + ["enter", "enter"] + list(str(path)) + ["enter", "enter", "esc", "q"]
    t = run(env, keys)
    assert "They are now in Sessions and Search" in "".join(frames(t))
    with env.conn() as conn:
        assert appmod.query.search(conn, "useful imported question", source="claude_app")


def test_codex_import_preview_can_be_cancelled(env, monkeypatch):
    from agent_recall import codex_bridge, environment
    path = export_file(env)
    monkeypatch.setattr(environment, "detect_codex", lambda: env.root / "codex.exe")
    monkeypatch.setattr(codex_bridge, "install", lambda *args: pytest.fail("Cancelled import must not touch Codex"))
    keys = ["down"] * 5 + ["enter", "down", "enter"] + list(str(path)) + ["enter", "enter", "esc", "esc", "q"]
    t = run(env, keys)
    preview = next(f for f in frames(t) if "Chats: 1" in f)
    text = " ".join(" ".join(line.split("│", 1)[-1] for line in preview.splitlines()).split())
    assert "without project assignments" in text
    assert list((env.data / "imports").glob("*/manifest.json"))
    assert not list((env.data / "imports").glob("*/installed-*.json"))


@pytest.mark.parametrize("custom_folder", [False, True])
def test_codex_import_detects_paths_and_checks_result(env, monkeypatch, custom_folder):
    from agent_recall import codex_bridge, environment
    path = export_file(env)
    native_home = env.root / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(native_home))
    binary = env.root / "codex.exe"
    monkeypatch.setattr(environment, "detect_codex", lambda: binary)
    calls = []
    archive_root = env.root / "chosen archive" if custom_folder else env.data / "imports"

    def install(manifest, executable, home):
        assert manifest.is_file() and executable == binary and home == native_home
        assert manifest.parent.parent == archive_root
        calls.append("install")

    def verify(*args):
        calls.append("verify")
        return {"threads": 1, "messages": 1}

    monkeypatch.setattr(codex_bridge, "install", install)
    monkeypatch.setattr(codex_bridge, "verify", verify)
    keys = ["down"] * 5 + ["enter", "down", "enter"] + list(str(path)) + ["enter"]
    if custom_folder:
        keys += ["delete"] + list(str(archive_root))
    keys += ["enter", "enter", "enter", "esc", "q"]
    t = run(env, keys)
    assert calls == ["install", "verify"]
    text = "".join(frames(t))
    assert "Messages verified: 1" in text and "Open the Codex desktop app again" in text
    from agent_recall.config import load_config
    saved = load_config(env.root / "config.toml")
    assert saved["import_archive_dir"] == str(archive_root)
    assert len(saved["import_archives"]) == 1
    assert appmod.App(saved, term(["esc"]))._import_archive_root == archive_root


def test_cancel_archive_folder_does_not_prepare_or_import(env, monkeypatch):
    from agent_recall import codex_bridge, environment
    path = export_file(env)
    monkeypatch.setattr(environment, "detect_codex", lambda: env.root / "codex.exe")
    monkeypatch.setattr(codex_bridge, "prepare_cached", lambda *args: pytest.fail("Cancelled folder choice must not write an archive"))
    keys = ["down"] * 5 + ["enter", "down", "enter"] + list(str(path)) + ["enter", "esc", "esc", "q"]
    text = "".join(frames(run(env, keys)))
    assert "Save the import archive" in text
    assert not (env.data / "imports").exists()


def test_archive_folder_rejects_a_file_and_allows_retry(env, monkeypatch):
    from agent_recall import environment
    path = export_file(env)
    monkeypatch.setattr(environment, "detect_codex", lambda: env.root / "codex.exe")
    keys = ["down"] * 5 + ["enter", "down", "enter"] + list(str(path)) + ["enter", "delete"]
    keys += list(str(path)) + ["enter", "enter", "enter", "esc", "esc", "q"]
    text = "".join(frames(run(env, keys)))
    assert "This path is a file" in text
    assert list((env.data / "imports").glob("*/manifest.json"))


def test_import_missing_file_allows_retry(env):
    path = export_file(env)
    keys = ["down"] * 5 + ["enter", "enter"] + list(str(env.root / "missing.zip")) + ["enter", "enter"]
    keys += list(str(path)) + ["enter", "enter", "esc", "q"]
    text = "".join(frames(run(env, keys)))
    assert "Not found" in text and "Import finished" in text


def test_import_paths_are_visible_after_restarting(env, monkeypatch, capsys):
    from agent_recall.config import load_config, remember_import
    from agent_recall.cli import cmd_paths
    monkeypatch.setenv("CODEX_HOME", str(env.root / "codex-home"))
    cfg = env.config()
    archive_root = env.root / "custom archives"
    archive = archive_root / "claude-example"
    archive.mkdir(parents=True)
    remember_import(cfg, archive / "manifest.json", archive_root=archive_root)
    loaded = load_config(env.root / "config.toml")
    cmd_paths(loaded, SimpleNamespace(json=True))
    rows = json.loads(capsys.readouterr().out)
    paths = {r["name"]: r["path"] for r in rows}
    assert paths["New imports"] == str(archive_root)
    assert paths["Latest import"] == str(archive)
    assert paths["Codex chat history"] == str(env.root / "codex-home" / "sessions")
    cmd_paths(loaded, SimpleNamespace(json=False))
    assert str(archive) in capsys.readouterr().out


def test_remember_import_does_not_change_preferred_folder_when_only_recording(env):
    from agent_recall.config import remember_import
    cfg = env.config()
    root = env.root / "chosen"
    remember_import(cfg, root / "one/manifest.json", archive_root=root)
    remember_import(cfg, env.root / "old/another/manifest.json")
    assert cfg["import_archive_dir"] == str(root)
    assert len(cfg["import_archives"]) == 2
    remember_import(cfg, root / "one/manifest.json")
    assert len(cfg["import_archives"]) == 2
    assert cfg["import_archives"][-1] == str(root / "one")


def test_paths_distinguish_existing_archive_from_future_import_folder(env, capsys):
    from agent_recall.config import remember_import
    from agent_recall.cli import cmd_paths
    cfg = env.config()
    archive = env.root / "actual-export"
    archive.mkdir()
    remember_import(cfg, archive / "manifest.json")
    cmd_paths(cfg, SimpleNamespace(json=False))
    lines = capsys.readouterr().out.splitlines()
    current = next(line for line in lines if line.startswith("Latest import"))
    future = next(line for line in lines if line.startswith("New imports"))
    assert str(archive) in current and "(missing)" not in current
    assert str(env.data / "imports") in future and "(created on import)" in future
    assert "(missing)" not in future
    assert not (env.data / "imports").exists()


def test_paths_still_warn_about_a_missing_recorded_archive(env, capsys):
    from agent_recall.cli import cmd_paths
    cfg = env.config()
    cfg["import_archives"] = [str(env.root / "deleted-export")]
    cmd_paths(cfg, SimpleNamespace(json=False))
    current = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Latest import"))
    assert "(missing)" in current and "(created on import)" not in current


def test_config_values_escape_windows_paths_and_quotes(tmp_path):
    p = tmp_path / "config.toml"
    value = 'D:\\new\\imports "example"'
    appmod.set_config_value(p, "import_archive_dir", value)
    appmod.set_config_value(p, "import_archive_dir", value + "\\next")
    appmod.set_config_value(p, "import_archives", [value])
    saved = tomllib.loads(p.read_text(encoding="utf-8"))
    assert saved["import_archive_dir"] == value + "\\next"
    assert saved["import_archives"] == [value]


@pytest.mark.parametrize("error,expected", [
    (zipfile.BadZipFile(), "Download it again"),
    (json.JSONDecodeError("broken", "", 0), "original conversations.json"),
    (PermissionError(), "Check access permissions"),
    (OSError(errno.ENOSPC, "full"), "free disk space"),
    (TimeoutError(), "did not respond in time"),
])
def test_import_errors_include_recovery_steps(error, expected):
    from agent_recall.export_help import import_problem
    assert expected in " ".join(line for line, _ in import_problem(error))


def test_wrong_export_file_can_be_corrected_without_leaving_the_wizard(env):
    good = export_file(env)
    wrong = env.root / "projects-000.zip"
    with zipfile.ZipFile(wrong, "w") as z:
        z.writestr("projects/example.json", "{}")
    keys = ["down"] * 5 + ["enter", "enter"] + list(str(wrong)) + ["enter", "enter"]
    keys += list(str(good)) + ["enter", "enter", "esc", "q"]
    text = "".join(frames(run(env, keys)))
    assert "Other ZIPs do not contain chats" in text and "Import finished" in text


def test_claude_guide_shows_folder_layout_and_codex_restart():
    from agent_recall.export_help import export_guide
    text = " ".join(line for line, _ in export_guide("claude"))
    assert "conversations.json" in text and "conversations-000.zip" in text and "frames-000.zip" in text
    assert "Leave the other ZIP" in text and "fully close" in text and "Open it again" in text


@pytest.mark.parametrize("language,action,guide,read_first", [
    ("en", "Import into Codex history", "Prepare Claude export", "Before you start"),
    ("ru", "Импорт в историю Codex", "Подготовка экспорта Claude", "Перед началом"),
])
def test_codex_import_menu_recommends_preparation_and_summarizes_steps(env, language, action, guide, read_first):
    from agent_recall.i18n import set_lang
    set_lang(language)
    t = term(["down", "esc"], size=(120, 34))
    appmod.App(env.config(), t).screen_import()
    frame = frames(t)[-1]
    assert action in frame and guide in frame and read_first in frame
    assert "conversations.json" in frame
    assert "1." in frame and "4." in frame
