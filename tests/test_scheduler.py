"""Schedulers are simulated: tests never change the developer's OS jobs."""
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_recall import scheduler as sm
from agent_recall.cli import main


@pytest.fixture
def native(tmp_path, monkeypatch):
    """A fake OS with working launchctl/systemctl/crontab and an existing user crontab."""
    state = SimpleNamespace(loaded=set(), units=set(), cron="0 0 * * * echo keep-me\n", calls=[], fail=None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(sm.os, "getuid", lambda: 123, raising=False)
    monkeypatch.setattr(sm.shutil, "which", lambda name: f"/bin/{name}")

    def run(args, *, input=None, check=True):
        state.calls.append((args, input))
        rc, out, err = 0, "", ""
        if state.fail and state.fail in args:
            rc, err = 1, "permission denied"
        elif args[0] == "crontab":
            if args[1] == "-l":
                out = state.cron
            else:
                state.cron = input
        elif args[0] == "launchctl":
            if args[1] == "print":
                rc = 0 if args[2] in state.loaded else 1
            elif args[1] == "bootstrap":
                data = plistlib.loads(Path(args[3]).read_bytes())
                state.loaded.add(f"{args[2]}/{data['Label']}")
            elif args[1] == "bootout":
                state.loaded.discard(args[2])
        elif args[0] == "systemctl":
            if args[2] == "is-active":
                rc = 0 if args[3] in state.units else 3
            elif args[2] == "enable":
                state.units.add(args[-1])
            elif args[2] == "disable":
                state.units.discard(args[-1])
        if check and rc:
            raise sm.ScheduleError(err)
        return subprocess.CompletedProcess(args, rc, out, err)

    monkeypatch.setattr(sm, "_run", run)
    return state


def manager(tmp_path, platform):
    return sm.Scheduler({"_config_path": str(tmp_path / "data & spaces" / "config.toml")},
                        platform=platform, home=tmp_path, python=str(tmp_path / "venv $50%" / "python"))


@pytest.mark.parametrize("platform,backend", [("darwin", "launchd"), ("linux", "systemd"), ("win32", "Windows Task Scheduler")])
def test_new_install_is_off(tmp_path, native, monkeypatch, platform, backend):
    monkeypatch.setattr(sm.Scheduler, "_ps", lambda *a: [])
    state = manager(tmp_path, platform).status()
    assert not state.enabled and not state.installed and state.backend == backend


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_enable_update_disable_preserves_other_jobs_and_data(tmp_path, native, platform):
    m = manager(tmp_path, platform)
    m.directory.mkdir()
    data = m.directory / "brain.db"
    data.write_text("precious", encoding="utf-8")
    m.enable(30)
    assert m.status().enabled and m.status().interval == 30
    m.enable(60)
    assert m.status().interval == 60
    m.disable()
    m.disable()  # safe to repeat, also used by noninteractive installers
    assert not m.status().installed
    assert data.read_text(encoding="utf-8") == "precious"
    assert native.cron == "0 0 * * * echo keep-me\n"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_migrate_legacy_enabled_and_paused_jobs(tmp_path, native, platform):
    m = manager(tmp_path, platform)
    if platform == "darwin":
        m.launch_dir.mkdir(parents=True)
        path = m.launch_dir / "com.brainsync.sync.plist"
        path.write_bytes(plistlib.dumps({"Label": "com.brainsync.sync", "StartInterval": 1800,
                                        "ProgramArguments": ["/old/python", "-m", "brainsync", "sync"]}))
        native.loaded.add("gui/123/com.brainsync.sync")
    else:
        m.unit_dir.mkdir(parents=True)
        path = m.unit_dir / "brainsync.service"
        path.write_text('[Service]\nExecStart="/old/python" -X utf8 -m brainsync sync\n', encoding="utf-8")
        (m.unit_dir / "brainsync.timer").write_text("[Timer]\nOnUnitActiveSec=30min\n", encoding="utf-8")
        native.units.add("brainsync.timer")
    assert m.status().enabled and m.status().interval == 30
    m.enable(m.status().interval)
    assert m.status().enabled and not path.exists()
    assert not any("brainsync" in j for j in native.loaded | native.units)
    # An older paused schedule is still found and removed on disable.
    native.loaded.clear()
    native.units.clear()
    assert m.status().installed and not m.status().enabled
    m.disable()
    assert not m.status().installed


def test_cron_migration_keeps_unrelated_entries_and_validates_interval(tmp_path, native, monkeypatch):
    monkeypatch.setattr(sm.shutil, "which", lambda name: "/bin/crontab" if name == "crontab" else None)
    native.cron += '*/20 * * * * /old/python -m brainsync sync # brainsync sync\n'
    native.cron += '#*/15 * * * * /old/python -m agent_recall sync # agent-recall sync\n'
    m = manager(tmp_path, "linux")
    assert m.status().enabled and m.status().interval == 20
    old = native.cron
    with pytest.raises(sm.ScheduleError, match="Cron supports"):
        m.enable(90)
    assert native.cron == old  # validate before removing old tasks
    m.enable(120)
    assert native.cron.startswith("0 0 * * * echo keep-me\n0 */2 ")
    assert "brainsync" not in native.cron and "\\%" in native.cron
    assert m.status().interval == 120
    m.disable()
    assert native.cron == "0 0 * * * echo keep-me\n"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_unrecognized_files_are_not_changed(tmp_path, native, platform):
    m = manager(tmp_path, platform)
    if platform == "darwin":
        m.launch_dir.mkdir(parents=True)
        path = m.launch_dir / "com.agent-recall.sync.plist"
        content = plistlib.dumps({"Label": "com.agent-recall.sync", "ProgramArguments": ["unrelated"]})
    else:
        m.unit_dir.mkdir(parents=True)
        path = m.unit_dir / "agent-recall.service"
        content = b"[Service]\nExecStart=unrelated\n"
    path.write_bytes(content)
    m.disable()
    with pytest.raises(sm.ScheduleError, match="Unrecognized"):
        m.enable()
    assert path.read_bytes() == content


def test_failure_stopping_timer_keeps_files(tmp_path, native):
    m = manager(tmp_path, "linux")
    m.enable()
    native.fail = "disable"
    with pytest.raises(sm.ScheduleError, match="permission denied"):
        m.disable()
    assert (m.unit_dir / "agent-recall.timer").exists()
    assert "agent-recall.timer" in native.units


def test_systemd_quoting_and_venv_symlinks(tmp_path, native):
    m = manager(tmp_path, "linux")
    m.enable()
    service = (m.unit_dir / "agent-recall.service").read_text(encoding="utf-8")
    assert '"-m" "agent_recall"' in service
    assert "$$50%%" in service
    assert '"--config"' in service
    if os.name != "nt":
        link = tmp_path / "venv-python"
        link.symlink_to(Path(os.sys.executable))
        assert sm.Scheduler({"_config_path": m.config}, python=str(link)).python == str(link)


def test_windows_paths_are_data_and_native_commands_are_checked(tmp_path, native, monkeypatch):
    calls = []
    def ps(args, **kw):
        calls.append((args, kw))
        return subprocess.CompletedProcess(args, 0, '[{"name":"BrainSync","enabled":true,"interval":30}]', "")
    monkeypatch.setattr(sm, "_run", ps)
    m = manager(tmp_path, "win32")
    assert m.status().interval == 30
    m.enable(20)
    m.disable()
    payloads = [json.loads(kw["input"]) for _, kw in calls]
    assert [p["action"] for p in payloads] == ["status", "enable", "disable"]
    assert payloads[1]["interval"] == 20 and payloads[1]["python"] == m.python
    assert "--config" in payloads[1]["args"]
    script = calls[0][0][-1]
    assert m.config not in script
    assert "Unregister-ScheduledTask" in script and "BrainSync" in script
    assert "-Confirm:$false" in script and "-TaskPath '\\'" in script


@pytest.mark.parametrize("enabled,mode,answer,expected", [
    (False, "ask", "", "disable"), (False, "ask", "yes", "enable"),
    (True, "ask", "", "enable"), (True, "ask", "нет", "disable"),
    (False, "preserve", None, "disable"), (True, "preserve", None, "enable"),
    (True, "off", None, "disable"), (False, "on", None, "enable"),
])
def test_install_choice_defaults_and_upgrade(env, monkeypatch, enabled, mode, answer, expected, capsys):
    calls = []
    state = sm.State("fake", enabled, enabled, 30)
    monkeypatch.setattr(sm.Scheduler, "status", lambda self: state)
    def enable(self, interval):
        calls.append(("enable", interval))
        state.enabled = True
    def disable(self):
        calls.append(("disable",))
        state.enabled = False
    monkeypatch.setattr(sm.Scheduler, "enable", enable)
    monkeypatch.setattr(sm.Scheduler, "disable", disable)
    def respond(prompt):
        assert mode == "ask" and ("Y/n" if enabled else "y/N") in prompt
        return answer
    monkeypatch.setattr("builtins.input", respond)
    assert main(["--config", str(env.root / "config.toml"), "auto-sync", "setup", "--mode", mode]) == 0
    assert calls[0][0] == expected
    if expected == "enable":
        assert calls[0][1] == 30  # keep the old interval unless explicitly changed


def test_invalid_answer_does_not_change_schedule(env, monkeypatch):
    monkeypatch.setattr(sm.Scheduler, "status", lambda self: sm.State("fake"))
    monkeypatch.setattr(sm.Scheduler, "enable", lambda *a: pytest.fail("unexpected mutation"))
    monkeypatch.setattr(sm.Scheduler, "disable", lambda *a: pytest.fail("unexpected mutation"))
    monkeypatch.setattr("builtins.input", lambda _: "perhaps")
    assert main(["--config", str(env.root / "config.toml"), "auto-sync", "setup"]) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")
@pytest.mark.parametrize("action", ["status", "enable", "disable"])
def test_windows_native_script_with_mocked_task_cmdlets(tmp_path, monkeypatch, action):
    """Run the real PowerShell code and JSON pipe, substituting all scheduler cmdlets."""
    original_run = sm._run
    prefix = r'''
$script:created = 0
$script:removed = @()
function Get-ScheduledTask {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    foreach ($name in @('AgentRecall','BrainSync')) {
        [pscustomobject]@{TaskName=$name; State='Ready'; Principal=[pscustomobject]@{UserId=$user};
            Actions=@([pscustomobject]@{Arguments='-X utf8 -m agent_recall sync'});
            Triggers=@([pscustomobject]@{Repetition=[pscustomobject]@{Interval='PT30M'}})}
    }
}
function Unregister-ScheduledTask { param($TaskPath, $TaskName, [switch]$Confirm)
    if ($TaskPath -ne '\' -or $Confirm) { throw 'Unsafe unregister' }
    $script:removed += $TaskName
}
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory)
    if ($Argument -notmatch '--config' -or $WorkingDirectory -ne $p.directory) { throw 'Lost config path' }
    return @{}
}
function New-ScheduledTaskTrigger { return @{} }
function New-ScheduledTaskSettingsSet { return @{} }
function New-ScheduledTaskPrincipal { return @{} }
function Register-ScheduledTask { param($TaskPath,$TaskName,$Action,$Trigger,$Settings,$Principal,$Description,[switch]$Force)
    if ($TaskName -ne 'AgentRecall' -or $TaskPath -ne '\' -or -not $Force) { throw 'Wrong registration' }
    $script:created++
}
'''
    suffix = r'''
if ($p.action -eq 'enable' -and ($script:created -ne 1 -or ($script:removed -join ',') -ne 'BrainSync')) { throw 'Migration failed' }
if ($p.action -eq 'disable' -and ($script:removed -join ',') -ne 'AgentRecall,BrainSync') { throw 'Removal failed' }
if ($p.action -eq 'status' -and ($script:created -ne 0 -or $script:removed.Count -ne 0)) { throw 'Status changed jobs' }
'''
    def run(args, **kwargs):
        script = prefix + args[-1].replace("ConvertTo-Json -InputObject", suffix + "\nConvertTo-Json -InputObject")
        return original_run(args[:-1] + [script], **kwargs)
    monkeypatch.setattr(sm, "_run", run)
    m = manager(tmp_path, "win32")
    if action == "status":
        assert m.status().interval == 30
    elif action == "enable":
        m.enable(20)
    else:
        m.disable()


def _bash():
    found = shutil.which("bash")
    if found:
        return found
    git = shutil.which("git")
    candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
    return str(candidate) if candidate and candidate.exists() else None


@pytest.mark.parametrize("flags,mode", [([], "ask"), (["-y"], "preserve"),
    (["-y", "--auto-sync"], "on"), (["--no-task"], "off"), (["--no-auto-sync"], "off")])
def test_unix_installer_schedule_choice(tmp_path, flags, mode):
    bash = _bash()
    if not bash:
        pytest.skip("bash unavailable")
    root = Path(__file__).resolve().parents[1]
    source = (root / "brainctl.sh").read_text(encoding="utf-8")
    prefix, rest = source.split("# ---- arguments", 1)
    # Replace installation effects; execute the actual argument parser and cmd_install.
    stubs = r'''
ensure_python() { :; }
ensure_venv() { :; }
write_shims() { :; }
add_to_path() { :; }
save_state() { :; }
os_name() { echo linux; }
brain() { printf 'BRAIN %s\n' "$*"; }
CONFIG_FILE="$HOME/config.toml"
SOURCE="$HOME"
'''
    script = tmp_path / "installer.sh"
    script.write_text(prefix + stubs + "\n# ---- arguments" + rest, encoding="utf-8", newline="\n")
    proc = subprocess.run([bash, script.as_posix(), "install", "--no-sync", "--no-mcp", *flags],
                          env={**os.environ, "HOME": tmp_path.as_posix(), "AGENT_RECALL_LANG": "en"},
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert f"BRAIN auto-sync setup --mode {mode}" in proc.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows installer")
@pytest.mark.parametrize("flags,mode", [([], "ask"), (["-Yes"], "preserve"),
    (["-Yes", "-AutoSync"], "on"), (["-NoTask"], "off")])
def test_windows_installer_schedule_choice(tmp_path, flags, mode):
    source = (Path(__file__).resolve().parents[1] / "brainctl.ps1").read_text(encoding="utf-8-sig")
    parameters = source[:source.index('$ErrorActionPreference = "Stop"')]
    install = source[source.index("function Cmd-Install {"):source.index("function Cmd-Update {")]
    stubs = r'''
$IntervalSpecified = $PSBoundParameters.ContainsKey('Interval')
$AppDir='app'; $DataDir='data'; $Config='config'; $VenvDir='venv'; $LegacyAppDir='old'
$Py='Test-Python'
function T($en,$ru) { return $en }
function Step($x) {}
function Ok($x) {}
function Info($x) {}
function Find-Python { return @{Version='3.12'; Path='unused'} }
function Test-Path($Path) { return ($Path -ne 'old') }
function New-Item {}
function Resolve-Source { return 'source' }
function Install-Package {}
function Install-Shims {}
function Invoke-Py([string[]]$arguments) { Write-Output ('PY: '+($arguments -join ' ')) }
function Test-Python {}
'''
    script = tmp_path / "installer.ps1"
    script.write_text(parameters + stubs + install + "\nCmd-Install\n", encoding="utf-8-sig")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                           "install", "-NoSync", "-NoMcp", *flags], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=20)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert f"auto-sync setup --mode {mode}" in proc.stdout
