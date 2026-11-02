<#
.SYNOPSIS
  brainctl — installs and manages agent-recall on Windows.
  All data work happens in Python (the `brain` command); this script only installs and configures.
  Messages are in English or Russian: AGENT_RECALL_LANG, `language` in config.toml, or the Windows UI language.

.EXAMPLE
  .\brainctl.ps1 install                  # everything: Python, environment, config, first sync, scheduler, MCP
  brainctl status                         # after install the command works from any terminal
  brainctl task add -Interval 30
  brainctl mcp add claude
  brainctl uninstall -Purge
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Action = "",
    [Parameter(Position = 2)][string]$Target = "all",
    [int]$Interval = 15,
    [string]$Source = "",
    [string]$Vault = "",
    [switch]$Yes,
    [switch]$NoTask,
    [switch]$NoMcp,
    [switch]$NoSync,
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# --- Locations -------------------------------------------------------------------------------
$AppDir   = Join-Path $env:LOCALAPPDATA "agent-recall"     # program: venv, command shims, copy of this script
$VenvDir  = Join-Path $AppDir "venv"
$Py       = Join-Path $VenvDir "Scripts\python.exe"
$PyW      = Join-Path $VenvDir "Scripts\pythonw.exe"
$BinDir   = Join-Path $AppDir "bin"
$SrcFile  = Join-Path $AppDir "source.txt"
# data: config.toml, brain.db, brain.log. Same rule as in Python (config.data_home):
# AGENT_RECALL_HOME > the pre-rename folder .brainsync (if the new one does not exist yet) > .agent-recall
$NewData    = Join-Path $env:USERPROFILE ".agent-recall"
$LegacyData = Join-Path $env:USERPROFILE ".brainsync"
$DataDir = if ($env:AGENT_RECALL_HOME) { $env:AGENT_RECALL_HOME }
           elseif (-not (Test-Path $NewData) -and (Test-Path $LegacyData)) { $LegacyData }
           else { $NewData }
$Config   = Join-Path $DataDir "config.toml"
$TaskName = "AgentRecall"
# Install made before the project was renamed (brainsync) — removed by `install`
$LegacyAppDir = Join-Path $env:LOCALAPPDATA "brainsync"
$LegacyTask   = "BrainSync"
$MinPy    = [version]"3.11"

# --- Language: same priority as in Python (i18n.set_lang) ------------------------------------
function Get-Lang {
    foreach ($v in @($env:AGENT_RECALL_LANG)) {
        if ($v -and $v.Substring(0, [Math]::Min(2, $v.Length)).ToLower() -in @("en", "ru")) { return $v.Substring(0, 2).ToLower() }
    }
    if (Test-Path $Config) {
        $m = Select-String -Path $Config -Pattern '^\s*language\s*=\s*"(en|ru)' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    if ((Get-UICulture).TwoLetterISOLanguageName -eq "ru") { return "ru" }
    return "en"
}
$Lang = Get-Lang
function T([string]$en, [string]$ru) { if ($Lang -eq "ru") { return $ru } return $en }

# --- Output ----------------------------------------------------------------------------------
function Step($t)  { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "    [ok] $t" -ForegroundColor Green }
function Warn($t)  { Write-Host "    [!]  $t" -ForegroundColor Yellow }
function Info($t)  { Write-Host "         $t" }
function Ask($q) {
    if ($Yes) { return $true }
    $a = Read-Host "    $q [y/N]"
    return $a -match '^(y|yes|д|да)$'
}

# --- Python ----------------------------------------------------------------------------------
function Get-PyVersion([string]$exe, [string[]]$pre = @()) {
    $ErrorActionPreference = "Continue"
    try {
        $out = & $exe @pre -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $ver, $path = "$out".Trim().Split("|", 2)
        return @{ Version = [version]$ver; Path = $path }
    } catch { return $null }
}

function Find-Python {
    $candidates = @()
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($v in "3.13", "3.12", "3.11", "3") { $candidates += , @("py.exe", @("-$v")) }
    }
    foreach ($name in "python.exe", "python3.exe") {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        # the Microsoft Store stub in WindowsApps opens the Store instead of running Python — skip it
        if ($cmd -and $cmd.Source -notmatch "WindowsApps") { $candidates += , @($cmd.Source, @()) }
    }
    $roots = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")
    foreach ($r in $roots) {
        if ($r -and (Test-Path $r)) {
            Get-ChildItem $r -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { $exe = Join-Path $_.FullName "python.exe"; if (Test-Path $exe) { $candidates += , @($exe, @()) } }
        }
    }
    foreach ($c in $candidates) {
        $info = Get-PyVersion $c[0] $c[1]
        if ($info -and $info.Version -ge $MinPy) { return $info }
    }
    return $null
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-Python {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw (T "Python $MinPy+ not found and winget is unavailable. Install Python from https://www.python.org/downloads/ and run install again." `
                 "Python $MinPy+ не найден, а winget недоступен. Установите Python с https://www.python.org/downloads/ и запустите install ещё раз.")
    }
    if (-not (Ask (T "Python $MinPy+ not found. Install Python 3.12 via winget (current user only)?" `
                     "Python $MinPy+ не найден. Установить Python 3.12 через winget (только для текущего пользователя)?"))) {
        throw (T "Cannot continue without Python." "Без Python продолжить нельзя.")
    }
    Info (T "Downloading and installing Python via winget — usually 1–3 minutes, keep this window open..." `
            "Скачиваю и ставлю Python через winget — обычно 1–3 минуты, окно не закрывайте...")
    & winget.exe install --id Python.Python.3.12 -e --scope user --silent --disable-interactivity `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw (T "winget returned code $LASTEXITCODE" "winget вернул код $LASTEXITCODE") }
    Update-SessionPath
    $info = Find-Python
    if (-not $info) {
        throw (T "Python was installed but cannot be found. Open a new PowerShell window and run install again." `
                 "Python установлен, но не найден. Откройте новое окно PowerShell и повторите install.")
    }
    return $info
}

function Invoke-Py([string[]]$arguments) {
    if (-not (Test-Path $Py)) { throw (T "agent-recall is not installed. Run: brainctl install" "agent-recall не установлен. Запустите: brainctl install") }
    & $Py @arguments
    if ($LASTEXITCODE -ne 0) {
        throw (T "python $($arguments -join ' ') exited with code $LASTEXITCODE" "python $($arguments -join ' ') завершился с кодом $LASTEXITCODE")
    }
}

# --- Package ---------------------------------------------------------------------------------
function Resolve-Source {
    if ($Source) { return (Resolve-Path $Source).Path }
    if (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) { return $PSScriptRoot }
    if (Test-Path $SrcFile) {
        $s = (Get-Content $SrcFile -Raw).Trim()
        if (Test-Path (Join-Path $s "pyproject.toml")) { return $s }
    }
    throw (T "Source folder (pyproject.toml) not found. Specify it: brainctl update -Source <path to agent-recall>" `
             "Не найдена папка с исходниками (pyproject.toml). Укажите: brainctl update -Source <путь к agent-recall>")
}

function Install-Package([string]$src) {
    Invoke-Py @("-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check", "pip")
    Invoke-Py @("-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check", "${src}[mcp]")
    Set-Content -Path $SrcFile -Value $src -Encoding UTF8
}

function Install-Shims {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    # a copy of this script, so brainctl keeps working even if the source folder is deleted
    if ($PSCommandPath -ne (Join-Path $AppDir "brainctl.ps1")) {
        Copy-Item $PSCommandPath (Join-Path $AppDir "brainctl.ps1") -Force
    }
    # %LOCALAPPDATA% is expanded at run time — shims work with non-ASCII user names too
    Set-Content (Join-Path $BinDir "brain.cmd") -Encoding ASCII -Value `
        "@`"%LOCALAPPDATA%\agent-recall\venv\Scripts\python.exe`" -m agent_recall %*"
    Set-Content (Join-Path $BinDir "brainctl.cmd") -Encoding ASCII -Value `
        "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%LOCALAPPDATA%\agent-recall\brainctl.ps1`" %*"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { $_ })
    if ($parts -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $BinDir) -join ";"), "User")
        Ok (T "Commands brain and brainctl added to PATH (in new terminal windows)" "Команды brain и brainctl добавлены в PATH (в новых окнах терминала)")
    }
    if (($env:Path -split ";") -notcontains $BinDir) { $env:Path += ";$BinDir" }
}

function Remove-Shims {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { $_ -and $_ -ne $BinDir })
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

# --- Install made under the old name (brainsync) ---------------------------------------------
function Remove-LegacyInstall {
    $found = $false
    if (Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false
        $found = $true
    }
    if (Test-Path $LegacyAppDir) {
        $oldPy = Join-Path $LegacyAppDir "venv\Scripts\python.exe"
        if (Test-Path $oldPy) {
            # MCP registrations pointed at the old Python; the new install registers them again
            $ErrorActionPreference = "Continue"
            & $oldPy -m brainsync integrate remove all *> $null
            $ErrorActionPreference = "Stop"
        }
        $oldBin = Join-Path $LegacyAppDir "bin"
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $parts = @($userPath -split ";" | Where-Object { $_ -and $_ -ne $oldBin })
        [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
        try { Remove-Item $LegacyAppDir -Recurse -Force }
        catch { Warn (T "Could not remove $LegacyAppDir — close your agents and delete it manually" "Не удалось удалить $LegacyAppDir — закройте агентов и удалите вручную") }
        $found = $true
    }
    if ($found) { Ok (T "Old brainsync install removed. Your data stays where it was: $DataDir" "Старая установка brainsync убрана. Данные остались на месте: $DataDir") }
}

# --- Scheduler -------------------------------------------------------------------------------
function Add-SyncTask([int]$minutes) {
    if (-not (Test-Path $PyW)) { throw (T "agent-recall is not installed. Run: brainctl install" "agent-recall не установлен. Запустите: brainctl install") }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    # pythonw has no console window, so the task does not flash every N minutes
    $action = New-ScheduledTaskAction -Execute $PyW -Argument "-m agent_recall sync" -WorkingDirectory $DataDir
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
    # StartWhenAvailable: if the PC was off, the missed run happens when it is back on
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "agent-recall: shared memory sync for AI agents" -Force | Out-Null
    Ok (T "Task '$TaskName': every $minutes min." "Задача '$TaskName': каждые $minutes мин.")
}

function Remove-SyncTask {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Ok (T "Task '$TaskName' removed" "Задача '$TaskName' удалена")
    } else { Info (T "No task '$TaskName'" "Задачи '$TaskName' нет") }
}

function Show-SyncTask {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Warn (T "Scheduled task not created (brainctl task add)" "Задача планировщика не создана (brainctl task add)"); return }
    $i = $t | Get-ScheduledTaskInfo
    $interval = $t.Triggers[0].Repetition.Interval
    $result = switch ($i.LastTaskResult) {
        0       { T "success" "успешно" }
        267009  { T "running" "выполняется" }
        267011  { T "has not run yet" "ещё не запускалась" }
        default { T "code $($i.LastTaskResult)" "код $($i.LastTaskResult)" }
    }
    Ok (T "Task '$TaskName': $($t.State), interval $interval" "Задача '$TaskName': $($t.State), интервал $interval")
    Info (T "Last run: $($i.LastRunTime) ($result)" "Последний запуск: $($i.LastRunTime) ($result)")
    Info (T "Next run: $($i.NextRunTime)" "Следующий запуск: $($i.NextRunTime)")
}

# --- Commands --------------------------------------------------------------------------------
function Cmd-Install {
    if ((Test-Path $LegacyAppDir) -or (Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue)) {
        Step (T "Migrating from the old version (brainsync)" "Перенос со старой версии (brainsync)")
        Remove-LegacyInstall
    }
    Step "Python"
    $sys = Find-Python
    if (-not $sys) { $sys = Install-Python }
    Ok "Python $($sys.Version): $($sys.Path)"

    Step (T "agent-recall environment" "Окружение agent-recall")
    New-Item -ItemType Directory -Force -Path $AppDir, $DataDir | Out-Null
    if (-not (Test-Path $Py)) {
        & $sys.Path -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw (T "Could not create the venv" "Не удалось создать venv") }
    }
    $src = Resolve-Source
    Install-Package $src
    Install-Shims
    Ok (T "Installed to $AppDir" "Установлено в $AppDir")

    Step (T "Config" "Конфиг")
    if (Test-Path $Config) {
        Ok (T "Already exists: $Config" "Уже есть: $Config")
    } else {
        $initArgs = @("-m", "agent_recall", "init")
        if ($Vault) { $initArgs += @("--vault", $Vault) }
        Invoke-Py $initArgs
        if (-not $Yes -and (Ask (T "Open the config to set up projects? (later: brainctl config)" "Открыть конфиг, чтобы настроить проекты? (можно и позже: brainctl config)"))) {
            Start-Process notepad.exe $Config -Wait
        }
    }

    if (-not $NoSync) {
        Step (T "First sync (may take a couple of minutes)" "Первая синхронизация (может занять пару минут)")
        Invoke-Py @("-m", "agent_recall", "sync")
    }
    if (-not $NoTask) {
        Step (T "Schedule" "Автозапуск")
        Add-SyncTask $Interval
    }
    if (-not $NoMcp) {
        Step (T "Agent connections (MCP)" "Подключение к агентам (MCP)")
        & $Py -m agent_recall integrate add all
    }

    Step (T "Check" "Проверка")
    & $Py -m agent_recall doctor
    Write-Host ("`n" + (T "Done. In a new terminal window you have:" "Готово. В новом окне терминала доступны:")) -ForegroundColor Green
    Info (T "brain search <query>       — search" "brain search <запрос>      — поиск")
    Info (T "brain handoff --last -c    — continue in another AI" "brain handoff --last -c    — продолжить в другом ИИ")
    Info (T "brainctl status            — installation status" "brainctl status            — состояние установки")
    Info (T "brainctl help              — all management commands" "brainctl help              — все команды управления")
}

function Cmd-Update {
    Step (T "Updating agent-recall" "Обновление agent-recall")
    $src = Resolve-Source
    Install-Package $src
    Install-Shims
    Ok (T "Updated from $src" "Обновлено из $src")
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Info (T "The scheduled task picks up the new version by itself" "Задача планировщика подхватит новую версию сама")
    }
}

function Cmd-Uninstall {
    if (-not (Ask (T "Remove agent-recall (program, scheduled task, MCP connections)?" "Удалить agent-recall (программу, задачу планировщика, подключения MCP)?"))) { return }
    Step (T "Uninstalling" "Удаление")
    Remove-SyncTask
    if (Test-Path $Py) {
        $ErrorActionPreference = "Continue"
        & $Py -m agent_recall integrate remove all
        $ErrorActionPreference = "Stop"
    }
    Remove-Shims
    Ok (T "Commands removed from PATH" "Команды убраны из PATH")
    if (Test-Path $AppDir) {
        try {
            Remove-Item $AppDir -Recurse -Force
            Ok (T "Removed: $AppDir" "Удалено: $AppDir")
        } catch {
            Warn (T "Could not remove $AppDir — an agent is probably still running the MCP server." "Не удалось удалить $AppDir — вероятно, MCP-сервер ещё запущен агентом.")
            Info (T "Close Claude Code / VS Code / Codex and delete the folder manually." "Закройте Claude Code / VS Code / Codex и удалите папку вручную.")
        }
    }
    if ($Purge) {
        if (Ask (T "Also delete your DATA (${DataDir}: database, config, logs)? This cannot be undone." "Удалить и ДАННЫЕ (${DataDir}: база, конфиг, логи)? Это необратимо.")) {
            Remove-Item $DataDir -Recurse -Force
            Ok (T "Data deleted" "Данные удалены")
        }
    } else {
        Info (T "Data kept in $DataDir (to delete it too: brainctl uninstall -Purge)" "Данные сохранены в $DataDir (удалить вместе с ними: brainctl uninstall -Purge)")
    }
}

function Cmd-Status {
    Step (T "Installation" "Установка")
    if (Test-Path $Py) {
        $v = Get-PyVersion $Py
        Ok (T "Python $($v.Version) in $VenvDir" "Python $($v.Version) в $VenvDir")
        $src = if (Test-Path $SrcFile) { (Get-Content $SrcFile -Raw).Trim() } else { "?" }
        Info (T "Source: $src" "Исходники: $src")
    } else { Warn (T "Not installed (brainctl install)" "Не установлено (brainctl install)"); return }
    if (([Environment]::GetEnvironmentVariable("Path", "User") -split ";") -contains $BinDir) { Ok (T "brain and brainctl are in PATH" "brain и brainctl в PATH") }
    else { Warn (T "$BinDir is not in PATH" "$BinDir нет в PATH") }
    Step (T "Scheduler" "Планировщик")
    Show-SyncTask
    Step (T "Data and sources" "Данные и источники")
    & $Py -m agent_recall doctor
}

function Cmd-Task {
    switch ($Action) {
        "add"    { Add-SyncTask $Interval }
        "remove" { Remove-SyncTask }
        "run"    { Start-ScheduledTask -TaskName $TaskName; Ok (T "Started. Log: brainctl log" "Запущено. Лог: brainctl log") }
        "pause"  { Disable-ScheduledTask -TaskName $TaskName | Out-Null; Ok (T "Auto-sync paused" "Автосинхронизация приостановлена") }
        "resume" { Enable-ScheduledTask -TaskName $TaskName | Out-Null; Ok (T "Auto-sync resumed" "Автосинхронизация возобновлена") }
        default  { Show-SyncTask }
    }
}

function Cmd-Mcp {
    $agent = if ($Target) { $Target } else { "all" }
    switch ($Action) {
        "add"    { & $Py -m agent_recall integrate add $agent }
        "remove" { & $Py -m agent_recall integrate remove $agent }
        default  { & $Py -m agent_recall integrate status all }
    }
}

function Cmd-Log {
    $log = Join-Path $DataDir "brain.log"
    if (-not (Test-Path $log)) { Warn (T "No log yet" "Лога ещё нет"); return }
    if ($Action -eq "follow") { Get-Content $log -Tail 30 -Wait -Encoding UTF8 }
    else { Get-Content $log -Tail 40 -Encoding UTF8 }
}

function Cmd-Help {
    if ($Lang -eq "ru") {
        Write-Host @"
brainctl — установка и управление agent-recall

  install [-Interval 15] [-Vault <путь>] [-Yes] [-NoSync] [-NoTask] [-NoMcp]
                         Python (через winget, если нет) → окружение → конфиг →
                         первая синхронизация → планировщик → подключение к агентам
  update [-Source <папка>]  переустановить из исходников (после git pull / новой версии)
  uninstall [-Purge]     удалить программу; с -Purge ещё и базу с конфигом

  status                 что установлено, задача планировщика, источники, агенты
  paths                  все пути: конфиг, база, лог, Claude Code, Codex, Obsidian
  config                 открыть config.toml
  log [follow]           последние строки лога (follow — следить)

  task                   состояние задачи планировщика
  task add [-Interval N] создать / пересоздать (по умолчанию каждые 15 мин.)
  task remove | run | pause | resume

  mcp                    к каким агентам подключён
  mcp add [all|claude|codex|cline]
  mcp remove [all|claude|codex|cline]

  sync                   синхронизировать сейчас (то же, что brain sync -v)
  doctor                 проверка путей и токенов (то же, что brain doctor)

Работа с данными — команда brain: brain search, brain handoff, brain export ... (brain --help)
Язык: AGENT_RECALL_LANG=en|ru или language = "en" в config.toml
"@
        return
    }
    Write-Host @"
brainctl — install and manage agent-recall

  install [-Interval 15] [-Vault <path>] [-Yes] [-NoSync] [-NoTask] [-NoMcp]
                         Python (via winget if missing) → environment → config →
                         first sync → scheduler → agent connections
  update [-Source <folder>] reinstall from source (after git pull / a new version)
  uninstall [-Purge]     remove the program; -Purge also deletes the database and config

  status                 what is installed, scheduled task, sources, agents
  paths                  all paths: config, database, log, Claude Code, Codex, Obsidian
  config                 open config.toml
  log [follow]           last lines of the log (follow — keep watching)

  task                   scheduled task status
  task add [-Interval N] create / recreate (default every 15 min)
  task remove | run | pause | resume

  mcp                    which agents are connected
  mcp add [all|claude|codex|cline]
  mcp remove [all|claude|codex|cline]

  sync                   sync now (same as brain sync -v)
  doctor                 check paths and tokens (same as brain doctor)

Working with data is the brain command: brain search, brain handoff, brain export ... (brain --help)
Language: AGENT_RECALL_LANG=en|ru or language = "ru" in config.toml
"@
}

try {
    switch ($Command.ToLower()) {
        "install"   { Cmd-Install }
        "update"    { Cmd-Update }
        "uninstall" { Cmd-Uninstall }
        "status"    { Cmd-Status }
        "task"      { Cmd-Task }
        "mcp"       { Cmd-Mcp }
        "config"    { if (-not (Test-Path $Config)) { Invoke-Py @("-m", "agent_recall", "init") }; Start-Process notepad.exe $Config }
        "log"       { Cmd-Log }
        "sync"      { Invoke-Py @("-m", "agent_recall", "sync", "-v") }
        "doctor"    { Invoke-Py @("-m", "agent_recall", "doctor") }
        "paths"     { Invoke-Py @("-m", "agent_recall", "paths") }
        default     { Cmd-Help }
    }
} catch {
    Write-Host ("`n" + (T "Error: " "Ошибка: ") + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
