"""Optional per-user scheduled sync. The OS, including pre-rename jobs, is the source of truth.

No shell is used for commands. Only recognized sync jobs at our exact names are changed.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .i18n import _


class ScheduleError(RuntimeError):
    pass


@dataclass
class State:
    backend: str
    enabled: bool = False
    installed: bool = False
    interval: int = 15

    def to_dict(self) -> dict:
        return asdict(self)


def _run(args: list[str], *, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(args, input=input, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScheduleError(str(e)) from e
    if check and p.returncode:
        raise ScheduleError((p.stderr or p.stdout).strip() or f"{args[0]}: exit {p.returncode}")
    return p


def _owned(command: str) -> bool:
    return bool(re.search(r"(?<!\S)['\"]?-m['\"]?\s+['\"]?(?:agent_recall|brainsync)['\"]?\s+", command)
                and re.search(r"\bsync\b", command))


def _unit_quote(value: str) -> str:
    # systemd expands specifiers and environment variables even inside double quotes.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"' + escaped.replace("\n", r"\n").replace("\r", r"\r").replace("\t", r"\t") + '"'


class Scheduler:
    def __init__(self, cfg: dict, *, platform: str | None = None, home: Path | None = None,
                 python: str | None = None):
        self.platform = platform or sys.platform
        self.home = home or Path.home()
        # Keep venv symlinks: resolving them would select the base Python without our package.
        self.python = os.path.abspath(python or sys.executable)
        self.config = str(Path(cfg["_config_path"]).resolve())
        self.directory = Path(self.config).parent
        self.launch_dir = self.home / "Library/LaunchAgents"
        self.unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", self.home / ".config")) / "systemd/user"

    def _args(self) -> list[str]:
        return [self.python, "-X", "utf8", "-m", "agent_recall", "--config", self.config, "sync"]

    def _ps(self, action: str, interval: int = 15) -> list[dict]:
        # Pass paths as JSON data rather than interpolating them into PowerShell source.
        payload = json.dumps({"action": action, "interval": interval, "python": self.python,
                              "args": subprocess.list2cmdline(self._args()[1:]),
                              "directory": str(self.directory)}, ensure_ascii=True)
        script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = [Console]::In.ReadToEnd() | ConvertFrom-Json
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$jobs = @()
foreach ($name in @('AgentRecall', 'BrainSync')) {
    # SilentlyContinue would also hide a broken scheduler or permission error.
    $task = Get-ScheduledTask -TaskPath '\' | Where-Object { $_.TaskName -eq $name }
    if (-not $task) { continue }
    $user = $task.Principal.UserId
    $ours = @($task.Actions | Where-Object {
        $_.Arguments -match '(?<!\S)-m\s+(agent_recall|brainsync)\s+' -and $_.Arguments -match '\bsync\b'
    }).Count -gt 0
    if (-not $ours -or $user -notin @($identity.Name, $identity.User.Value, $env:USERNAME)) {
        if ($name -eq 'AgentRecall' -and $p.action -eq 'enable') { throw 'AgentRecall task belongs to another application or user' }
        continue
    }
    $minutes = 15
    if ($task.Triggers[0].Repetition.Interval) {
        $minutes = [int][System.Xml.XmlConvert]::ToTimeSpan($task.Triggers[0].Repetition.Interval).TotalMinutes
    }
    $jobs += @{ name=$name; enabled=($task.State -ne 'Disabled'); interval=$minutes }
    if ($p.action -eq 'disable') { Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false }
}
if ($p.action -eq 'enable') {
    $exe = $p.python
    $windowless = Join-Path (Split-Path $exe) 'pythonw.exe'
    if (Test-Path -LiteralPath $windowless) { $exe = $windowless }
    $action = New-ScheduledTaskAction -Execute $exe -Argument $p.args -WorkingDirectory $p.directory
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $p.interval) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskPath '\' -TaskName 'AgentRecall' -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description 'agent-recall: optional shared memory sync' -Force | Out-Null
    foreach ($job in $jobs) {
        if ($job.name -eq 'BrainSync') { Unregister-ScheduledTask -TaskPath '\' -TaskName $job.name -Confirm:$false }
    }
}
ConvertTo-Json -InputObject @($jobs) -Compress
'''
        p = _run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=payload)
        return json.loads(p.stdout.strip() or "[]")

    def _launch_jobs(self) -> list[tuple[Path, dict]]:
        jobs = []
        for label in ("com.agent-recall.sync", "com.brainsync.sync"):
            path = self.launch_dir / f"{label}.plist"
            if not path.exists():
                continue
            try:
                data = plistlib.loads(path.read_bytes())
            except (ValueError, plistlib.InvalidFileException) as e:
                raise ScheduleError(f"Cannot read {path}: {e}") from e
            args = data.get("ProgramArguments", [])
            if (data.get("Label") == label and isinstance(args, list)
                    and all(isinstance(a, str) for a in args) and _owned(" ".join(args))):
                jobs.append((path, data))
        return jobs

    def _units(self) -> list[tuple[str, str]]:
        jobs = []
        for name in ("agent-recall", "brainsync"):
            service = self.unit_dir / f"{name}.service"
            if service.exists():
                text = service.read_text(encoding="utf-8")
                if any(_owned(line.partition("=")[2]) for line in text.splitlines()
                       if line.strip().startswith("ExecStart=")):
                    jobs.append((name, text))
        return jobs

    def _launch_loaded(self, label: str) -> bool:
        p = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False)
        if p.returncode and p.stderr.strip() and not any(s in p.stderr.lower() for s in (
                "could not find service", "could not find domain", "no such process")):
            raise ScheduleError(p.stderr.strip())
        return p.returncode == 0

    def _cron(self) -> str:
        if not shutil.which("crontab"):
            return ""
        p = _run(["crontab", "-l"], check=False)
        if p.returncode and (p.returncode != 1 or (p.stderr.strip() and "no crontab" not in p.stderr.lower())):
            raise ScheduleError(p.stderr.strip() or "Cannot read crontab")
        return p.stdout if not p.returncode else ""

    @staticmethod
    def _cron_ours(line: str) -> bool:
        return any(marker in line for marker in ("# agent-recall sync", "# brainsync sync")) and _owned(line)

    def backend(self) -> str:
        if self.platform == "win32":
            return "Windows Task Scheduler"
        if self.platform == "darwin":
            return "launchd"
        if self.platform.startswith("linux"):
            if shutil.which("systemctl") and not _run(["systemctl", "--user", "show-environment"], check=False).returncode:
                return "systemd"
            return "cron" if shutil.which("crontab") else "unavailable"
        return "unavailable"

    def status(self) -> State:
        backend = self.backend()
        if self.platform == "win32":
            jobs = self._ps("status")
            active = next((j for j in jobs if j["enabled"]), jobs[0] if jobs else {})
            return State(backend, bool(active.get("enabled")), bool(jobs), active.get("interval", 15))
        if self.platform == "darwin":
            jobs = self._launch_jobs()
            for _, data in jobs:
                if self._launch_loaded(data["Label"]):
                    return State(backend, True, True, max(1, data.get("StartInterval", 900) // 60))
            return State(backend, installed=bool(jobs), interval=max(1, jobs[0][1].get("StartInterval", 900) // 60) if jobs else 15)
        if self.platform.startswith("linux"):
            units = self._units()
            interval = 15
            for name, _ in units:
                timer = self.unit_dir / f"{name}.timer"
                if timer.exists():
                    text = timer.read_text(encoding="utf-8")
                    m = re.search(r"OnUnitActiveSec=(\d+)min", text)
                    interval = int(m[1]) if m else 15
                if shutil.which("systemctl"):
                    p = _run(["systemctl", "--user", "is-active", f"{name}.timer"], check=False)
                    if p.returncode not in (0, 3, 4):
                        raise ScheduleError(p.stderr.strip() or "Cannot read systemd timer state")
                    if p.returncode:
                        continue
                    return State("systemd", True, True, interval)
            cron_jobs = [s for s in self._cron().splitlines() if self._cron_ours(s)]
            for line in cron_jobs:
                spec = line.lstrip("# ")
                m = re.match(r"\*/(\d+) ", spec)
                hours = re.match(r"0 \*/(\d+) ", spec)
                interval = int(m[1]) if m else int(hours[1]) * 60 if hours else 15
                if not line.lstrip().startswith("#"):
                    return State("cron", True, True, interval)
            return State(backend, installed=bool(units or cron_jobs), interval=interval)
        return State(backend)

    def disable(self) -> None:
        if self.platform == "win32":
            self._ps("disable")
            return
        if self.platform == "darwin":
            for path, data in self._launch_jobs():
                target = f"gui/{os.getuid()}/{data['Label']}"
                # Already unloaded jobs can still leave files after a previous pause.
                if self._launch_loaded(data["Label"]):
                    _run(["launchctl", "bootout", target])
                path.unlink()
            return
        if self.platform.startswith("linux"):
            units = self._units()
            for name, _ in units:
                # Do not remove files while a timer may still be running.
                _run(["systemctl", "--user", "disable", "--now", f"{name}.timer"])
                for suffix in ("timer", "service"):
                    (self.unit_dir / f"{name}.{suffix}").unlink(missing_ok=True)
            if units:
                _run(["systemctl", "--user", "daemon-reload"])
            cron = self._cron()
            kept = "".join(s for s in cron.splitlines(keepends=True) if not self._cron_ours(s))
            if kept != cron:
                _run(["crontab", "-"], input=kept)

    def enable(self, interval: int = 15) -> None:
        if not 1 <= interval <= 1440:
            raise ScheduleError(_("Interval must be between 1 and 1440 minutes"))
        backend = self.backend()
        if backend == "unavailable":
            raise ScheduleError(_("No per-user scheduler is available (systemd or crontab required on Linux)"))
        if backend == "cron" and not (interval <= 60 and 60 % interval == 0 or interval % 60 == 0 and 1440 % interval == 0):
            raise ScheduleError(_("Cron supports these minute intervals: 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440"))
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.platform == "win32":
            self._ps("enable", interval)
            return
        # Detect conflicting files before touching any existing schedule.
        if backend == "launchd":
            path = self.launch_dir / "com.agent-recall.sync.plist"
            if path.exists() and path not in [p for p, _ in self._launch_jobs()]:
                raise ScheduleError(f"Unrecognized scheduler file: {path}")
        if backend == "systemd":
            recognized = {name for name, _ in self._units()}
            for suffix in ("service", "timer"):
                path = self.unit_dir / f"agent-recall.{suffix}"
                if path.exists() and "agent-recall" not in recognized:
                    raise ScheduleError(f"Unrecognized scheduler file: {path}")
        self.disable()
        if backend == "launchd":
            label = "com.agent-recall.sync"
            self.launch_dir.mkdir(parents=True, exist_ok=True)
            path = self.launch_dir / f"{label}.plist"
            path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": self._args(),
                                            "StartInterval": interval * 60, "RunAtLoad": True,
                                            "WorkingDirectory": str(self.directory),
                                            "StandardOutPath": str(self.directory / "launchd.log"),
                                            "StandardErrorPath": str(self.directory / "launchd.log")}))
            _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        elif backend == "systemd":
            self.unit_dir.mkdir(parents=True, exist_ok=True)
            (self.unit_dir / "agent-recall.service").write_text(
                "[Unit]\nDescription=agent-recall sync\n[Service]\nType=oneshot\nExecStart="
                + " ".join(_unit_quote(a) for a in self._args()) + "\n", encoding="utf-8")
            (self.unit_dir / "agent-recall.timer").write_text(
                f"[Unit]\nDescription=agent-recall sync\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec={interval}min\n"
                "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", "agent-recall.timer"])
        else:
            cron = self._cron()
            spec = f"*/{interval} * * * *" if interval < 60 else f"0 */{interval // 60} * * *"
            command = shlex.join(self._args()).replace("%", r"\%")
            _run(["crontab", "-"], input=cron.rstrip("\n") + ("\n" if cron else "")
                 + f"{spec} {command} >/dev/null 2>&1 # agent-recall sync\n")
