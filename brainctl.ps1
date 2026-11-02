<#
.SYNOPSIS
  brainctl — установка и управление agent-recall на Windows.
  Вся работа с данными — в Python (команда `brain`), этот скрипт только ставит и настраивает.

.EXAMPLE
  .\brainctl.ps1 install                  # всё сразу: Python, окружение, конфиг, синхронизация, планировщик, MCP
  brainctl status                         # после установки команда доступна из любого терминала
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

# --- Где что лежит ---------------------------------------------------------------------------
$AppDir   = Join-Path $env:LOCALAPPDATA "agent-recall"     # программа: venv, шимы, копия этого скрипта
$VenvDir  = Join-Path $AppDir "venv"
$Py       = Join-Path $VenvDir "Scripts\python.exe"
$PyW      = Join-Path $VenvDir "Scripts\pythonw.exe"
$BinDir   = Join-Path $AppDir "bin"
$SrcFile  = Join-Path $AppDir "source.txt"
# данные: config.toml, brain.db, brain.log. Та же логика, что в Python (config.data_home):
# AGENT_RECALL_HOME > старая папка .brainsync (если новой ещё нет) > .agent-recall
$NewData    = Join-Path $env:USERPROFILE ".agent-recall"
$LegacyData = Join-Path $env:USERPROFILE ".brainsync"
$DataDir = if ($env:AGENT_RECALL_HOME) { $env:AGENT_RECALL_HOME }
           elseif (-not (Test-Path $NewData) -and (Test-Path $LegacyData)) { $LegacyData }
           else { $NewData }
$Config   = Join-Path $DataDir "config.toml"
$TaskName = "AgentRecall"
# Установка до переименования проекта (brainsync) — убирается при install
$LegacyAppDir = Join-Path $env:LOCALAPPDATA "brainsync"
$LegacyTask   = "BrainSync"
$MinPy    = [version]"3.11"

# --- Вывод -----------------------------------------------------------------------------------
function Step($t)  { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "    [ok] $t" -ForegroundColor Green }
function Warn($t)  { Write-Host "    [!]  $t" -ForegroundColor Yellow }
function Info($t)  { Write-Host "         $t" }
function Ask($q) {
    if ($Yes) { return $true }
    $a = Read-Host "    $q [y/N]"
    return $a -match '^(y|д|yes|да)$'
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
        # заглушка из Microsoft Store в WindowsApps открывает магазин вместо запуска — пропускаем
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
        throw "Python $MinPy+ не найден, а winget недоступен. Установите Python с https://www.python.org/downloads/ и запустите install ещё раз."
    }
    if (-not (Ask "Python $MinPy+ не найден. Установить Python 3.12 через winget (только для текущего пользователя)?")) {
        throw "Без Python продолжить нельзя."
    }
    Info "Скачиваю и ставлю Python через winget — обычно 1–3 минуты, окно не закрывайте..."
    & winget.exe install --id Python.Python.3.12 -e --scope user --silent --disable-interactivity `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget вернул код $LASTEXITCODE" }
    Update-SessionPath
    $info = Find-Python
    if (-not $info) { throw "Python установлен, но не найден. Откройте новое окно PowerShell и повторите install." }
    return $info
}

function Invoke-Py([string[]]$arguments) {
    if (-not (Test-Path $Py)) { throw "agent-recall не установлен. Запустите: brainctl install" }
    & $Py @arguments
    if ($LASTEXITCODE -ne 0) { throw "python $($arguments -join ' ') завершился с кодом $LASTEXITCODE" }
}

# --- Установка пакета ------------------------------------------------------------------------
function Resolve-Source {
    if ($Source) { return (Resolve-Path $Source).Path }
    if (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) { return $PSScriptRoot }
    if (Test-Path $SrcFile) {
        $s = (Get-Content $SrcFile -Raw).Trim()
        if (Test-Path (Join-Path $s "pyproject.toml")) { return $s }
    }
    throw "Не найдена папка с исходниками (pyproject.toml). Укажите: brainctl update -Source <путь к brain-sync>"
}

function Install-Package([string]$src) {
    Invoke-Py @("-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check", "pip")
    Invoke-Py @("-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check", "${src}[mcp]")
    Set-Content -Path $SrcFile -Value $src -Encoding UTF8
}

function Install-Shims {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    # Копия скрипта управления — чтобы brainctl работал, даже если папку с исходниками удалить
    if ($PSCommandPath -ne (Join-Path $AppDir "brainctl.ps1")) {
        Copy-Item $PSCommandPath (Join-Path $AppDir "brainctl.ps1") -Force
    }
    # %LOCALAPPDATA% раскрывается при запуске — так шимы работают и с кириллицей в имени пользователя
    Set-Content (Join-Path $BinDir "brain.cmd") -Encoding ASCII -Value `
        "@`"%LOCALAPPDATA%\agent-recall\venv\Scripts\python.exe`" -m agent_recall %*"
    Set-Content (Join-Path $BinDir "brainctl.cmd") -Encoding ASCII -Value `
        "@powershell -NoProfile -ExecutionPolicy Bypass -File `"%LOCALAPPDATA%\agent-recall\brainctl.ps1`" %*"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { $_ })
    if ($parts -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $BinDir) -join ";"), "User")
        Ok "Команды brain и brainctl добавлены в PATH (в новых окнах терминала)"
    }
    if (($env:Path -split ";") -notcontains $BinDir) { $env:Path += ";$BinDir" }
}

function Remove-Shims {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { $_ -and $_ -ne $BinDir })
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

# --- Старая установка (до переименования в agent-recall) ------------------------------------
function Remove-LegacyInstall {
    $found = $false
    if (Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false
        $found = $true
    }
    if (Test-Path $LegacyAppDir) {
        $oldPy = Join-Path $LegacyAppDir "venv\Scripts\python.exe"
        if (Test-Path $oldPy) {
            # подключения MCP указывали на старый Python; новая установка подключит заново
            $ErrorActionPreference = "Continue"
            & $oldPy -m brainsync integrate remove all *> $null
            $ErrorActionPreference = "Stop"
        }
        $oldBin = Join-Path $LegacyAppDir "bin"
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $parts = @($userPath -split ";" | Where-Object { $_ -and $_ -ne $oldBin })
        [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
        try { Remove-Item $LegacyAppDir -Recurse -Force } catch { Warn "Не удалось удалить $LegacyAppDir — закройте агентов и удалите вручную" }
        $found = $true
    }
    if ($found) { Ok "Старая установка brainsync убрана. Данные остались на месте: $DataDir" }
}

# --- Планировщик -----------------------------------------------------------------------------
function Add-SyncTask([int]$minutes) {
    if (-not (Test-Path $PyW)) { throw "agent-recall не установлен. Запустите: brainctl install" }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    # pythonw — без консольного окна, задача не мигает каждые N минут
    $action = New-ScheduledTaskAction -Execute $PyW -Argument "-m agent_recall sync" -WorkingDirectory $DataDir
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
    # StartWhenAvailable: если ПК был выключен, пропущенный запуск выполнится при включении
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "agent-recall: синхронизация общей памяти ИИ-агентов" -Force | Out-Null
    Ok "Задача '$TaskName': каждые $minutes мин."
}

function Remove-SyncTask {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Ok "Задача '$TaskName' удалена"
    } else { Info "Задачи '$TaskName' нет" }
}

function Show-SyncTask {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Warn "Задача планировщика не создана (brainctl task add)"; return }
    $i = $t | Get-ScheduledTaskInfo
    $interval = $t.Triggers[0].Repetition.Interval
    $result = switch ($i.LastTaskResult) {
        0       { "успешно" }
        267009  { "выполняется" }
        267011  { "ещё не запускалась" }
        default { "код $($i.LastTaskResult)" }
    }
    Ok "Задача '$TaskName': $($t.State), интервал $interval"
    Info "Последний запуск: $($i.LastRunTime) ($result)"
    Info "Следующий запуск: $($i.NextRunTime)"
}

# --- Команды ---------------------------------------------------------------------------------
function Cmd-Install {
    if ((Test-Path $LegacyAppDir) -or (Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue)) {
        Step "Перенос со старой версии (brainsync)"
        Remove-LegacyInstall
    }
    Step "Python"
    $sys = Find-Python
    if (-not $sys) { $sys = Install-Python }
    Ok "Python $($sys.Version): $($sys.Path)"

    Step "Окружение agent-recall"
    New-Item -ItemType Directory -Force -Path $AppDir, $DataDir | Out-Null
    if (-not (Test-Path $Py)) {
        & $sys.Path -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Не удалось создать venv" }
    }
    $src = Resolve-Source
    Install-Package $src
    Install-Shims
    Ok "Установлено в $AppDir"

    Step "Конфиг"
    if (Test-Path $Config) {
        Ok "Уже есть: $Config"
    } else {
        $initArgs = @("-m", "agent_recall", "init")
        if ($Vault) { $initArgs += @("--vault", $Vault) }
        Invoke-Py $initArgs
        if (-not $Yes -and (Ask "Открыть конфиг, чтобы настроить проекты? (можно и позже: brainctl config)")) {
            Start-Process notepad.exe $Config -Wait
        }
    }

    if (-not $NoSync) {
        Step "Первая синхронизация (может занять пару минут)"
        Invoke-Py @("-m", "agent_recall", "sync")
    }
    if (-not $NoTask) {
        Step "Автозапуск"
        Add-SyncTask $Interval
    }
    if (-not $NoMcp) {
        Step "Подключение к агентам (MCP)"
        & $Py -m agent_recall integrate add all
    }

    Step "Проверка"
    & $Py -m agent_recall doctor
    Write-Host "`nГотово. В новом окне терминала доступны:" -ForegroundColor Green
    Info "brain search <запрос>      — поиск"
    Info "brain timeline <проект>    — история проекта"
    Info "brainctl status            — состояние установки"
    Info "brainctl help              — все команды управления"
}

function Cmd-Update {
    Step "Обновление agent-recall"
    $src = Resolve-Source
    Install-Package $src
    Install-Shims
    Ok "Обновлено из $src"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Info "Задача планировщика подхватит новую версию сама" }
}

function Cmd-Uninstall {
    if (-not (Ask "Удалить agent-recall (программу, задачу планировщика, подключения MCP)?")) { return }
    Step "Удаление"
    Remove-SyncTask
    if (Test-Path $Py) {
        $ErrorActionPreference = "Continue"
        & $Py -m agent_recall integrate remove all
        $ErrorActionPreference = "Stop"
    }
    Remove-Shims
    Ok "Команды убраны из PATH"
    if (Test-Path $AppDir) {
        try {
            Remove-Item $AppDir -Recurse -Force
            Ok "Удалено: $AppDir"
        } catch {
            Warn "Не удалось удалить $AppDir — вероятно, MCP-сервер ещё запущен агентом."
            Info "Закройте Claude Code / VS Code / Codex и удалите папку вручную."
        }
    }
    if ($Purge) {
        if (Ask "Удалить и ДАННЫЕ (${DataDir}: база, конфиг, логи)? Это необратимо.") {
            Remove-Item $DataDir -Recurse -Force
            Ok "Данные удалены"
        }
    } else {
        Info "Данные сохранены в $DataDir (удалить вместе с ними: brainctl uninstall -Purge)"
    }
}

function Cmd-Status {
    Step "Установка"
    if (Test-Path $Py) {
        $v = Get-PyVersion $Py
        Ok "Python $($v.Version) в $VenvDir"
        $src = if (Test-Path $SrcFile) { (Get-Content $SrcFile -Raw).Trim() } else { "?" }
        Info "Исходники: $src"
    } else { Warn "Не установлено (brainctl install)"; return }
    if (([Environment]::GetEnvironmentVariable("Path", "User") -split ";") -contains $BinDir) { Ok "brain и brainctl в PATH" }
    else { Warn "$BinDir нет в PATH" }
    Step "Планировщик"
    Show-SyncTask
    Step "Данные и источники"
    & $Py -m agent_recall doctor
}

function Cmd-Task {
    switch ($Action) {
        "add"    { Add-SyncTask $Interval }
        "remove" { Remove-SyncTask }
        "run"    { Start-ScheduledTask -TaskName $TaskName; Ok "Запущено. Лог: brainctl log" }
        "pause"  { Disable-ScheduledTask -TaskName $TaskName | Out-Null; Ok "Автосинхронизация приостановлена" }
        "resume" { Enable-ScheduledTask -TaskName $TaskName | Out-Null; Ok "Автосинхронизация возобновлена" }
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
    if (-not (Test-Path $log)) { Warn "Лога ещё нет"; return }
    if ($Action -eq "follow") { Get-Content $log -Tail 30 -Wait -Encoding UTF8 }
    else { Get-Content $log -Tail 40 -Encoding UTF8 }
}

function Cmd-Help {
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

Работа с данными — команда brain: brain search, brain timeline, brain export ... (brain --help)
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
    Write-Host "`nОшибка: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
