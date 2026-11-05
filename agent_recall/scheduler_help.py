"""Read-only OS checks users can perform independently of agent-recall."""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from .i18n import T
from .tui import BOLD, DIM, Line


def current_os() -> str:
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"


def scheduler_guide(system: str) -> list[Line]:
    common = [
        (T("These checks only read the scheduler; they do not change settings.",
           "Эти проверки только читают состояние планировщика и не меняют настройки."), DIM),
        (T("Use your usual user account: agent-recall creates a per-user job.",
           "Проверяйте под своей обычной учётной записью: agent-recall создаёт задачу пользователя."), ""),
        ("", ""),
    ]
    if system == "windows":
        steps = [
            (T("1. Open Task Scheduler", "1. Откройте Планировщик задач"), BOLD),
            (T("Press Win+R, enter taskschd.msc and press Enter.",
               "Нажмите Win+R, введите taskschd.msc и нажмите Enter."), ""),
            (T("Select Task Scheduler Library on the left. Find AgentRecall in the central list.",
               "Слева выберите «Библиотека планировщика заданий». В центре найдите AgentRecall."), ""),
            (T("Older installations may also have BrainSync. Refresh the list with F5 after changes.",
               "У старых установок также может быть BrainSync. После изменений обновите список клавишей F5."), ""),
            ("", ""),
            (T("2. Check the job", "2. Проверьте задачу"), BOLD),
            (T("Triggers shows the interval. Actions should run Python with -m agent_recall and sync.",
               "Вкладка «Триггеры» показывает интервал. В «Действиях» должен запускаться Python с -m agent_recall и sync."), ""),
            (T("Legacy BrainSync jobs may use -m brainsync instead.",
               "В старых задачах BrainSync вместо этого может быть -m brainsync."), DIM),
            (T("Ready means waiting; Disabled means paused. Last Run Result 0x0 means the last run succeeded.",
               "«Готово» означает ожидание, «Отключено» — паузу. Результат последнего запуска 0x0 означает успешное завершение."), ""),
            (T("A job that has never run has no successful run to verify yet. Wait for the next scheduled run.",
               "Если задача ещё не запускалась, успешного результата пока нет. Дождитесь следующего запуска по расписанию."), DIM),
            ("", ""),
            (T("3. Optional: check in PowerShell", "3. Дополнительно: проверка в PowerShell"), BOLD),
            ("Get-ScheduledTask -TaskPath '\\' |", ""),
            ("  Where-Object { $_.TaskName -in @('AgentRecall', 'BrainSync') }", ""),
            (T("This lists matching jobs and their states. To see the last run of AgentRecall:",
               "Команда покажет найденные задачи и их состояние. Последний запуск AgentRecall:"), DIM),
            ("Get-ScheduledTaskInfo -TaskPath '\\' -TaskName AgentRecall", ""),
            ("", ""),
            (T("After disabling in brain", "После отключения в brain"), BOLD),
            (T("AgentRecall and recognized legacy BrainSync sync jobs should be absent, not merely Disabled.",
               "AgentRecall и распознанные старые задачи синхронизации BrainSync должны отсутствовать, а не просто иметь состояние «Отключено»."), ""),
            (T("The list command then prints no matching jobs; the last-run command reports that AgentRecall was not found.",
               "Первая команда тогда не выводит найденных задач; команда последнего запуска сообщает, что AgentRecall не найден."), DIM),
            ("https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/get-scheduledtaskinfo", DIM),
        ]
    elif system == "macos":
        steps = [
            (T("1. Find the LaunchAgent file", "1. Найдите файл LaunchAgent"), BOLD),
            (T("In Finder press Shift+Cmd+G and open:", "В Finder нажмите Shift+Cmd+G и откройте:"), ""),
            ("~/Library/LaunchAgents", ""),
            (T("The job file is com.agent-recall.sync.plist; a legacy file may be com.brainsync.sync.plist.",
               "Файл задачи — com.agent-recall.sync.plist; старый файл может называться com.brainsync.sync.plist."), ""),
            (T("A file on disk does not prove the job is loaded. Check it in Terminal too.",
               "Наличие файла ещё не означает, что задача загружена. Проверьте её также в Терминале."), DIM),
            ("", ""),
            (T("2. Check the loaded job in Terminal", "2. Проверьте загруженную задачу в Терминале"), BOLD),
            ('launchctl print "gui/$(id -u)/com.agent-recall.sync"', ""),
            (T("Details mean the job is loaded. It can be idle between runs; it does not need a permanent Python process.",
               "Если показаны сведения, задача загружена. Между запусками она может ожидать: постоянный процесс Python не требуется."), ""),
            (T("A last exit code of 0 means the last completed run succeeded. Check the sync log for details.",
               "Last exit code 0 означает успешное завершение последнего запуска. Подробности смотрите в журнале синхронизации."), ""),
            (T("For a legacy job, repeat with com.brainsync.sync instead of com.agent-recall.sync.",
               "Для старой задачи повторите команду, заменив com.agent-recall.sync на com.brainsync.sync."), DIM),
            ("", ""),
            (T("After disabling in brain", "После отключения в brain"), BOLD),
            (T("The plist file should be gone and launchctl should report that the service was not found.",
               "Файл plist должен исчезнуть, а launchctl — сообщить, что служба не найдена."), ""),
            (T("A permission error is not proof of removal. Use the same signed-in account as the installation.",
               "Ошибка доступа не подтверждает удаление. Используйте ту же учётную запись, под которой выполнена установка."), DIM),
            ("https://support.apple.com/guide/terminal/apdc6c1077b-5d5d-4d35-9c19-60f2397b2369/mac", DIM),
        ]
    elif system == "linux":
        # Respect the real per-user unit directory when reading this on Linux.
        if sys.platform.startswith("linux"):
            directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
            unit_files = f"ls -l {shlex.quote(str(directory))}/agent-recall.*"
        else:
            unit_files = 'ls -l "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"/agent-recall.*'
        steps = [
            (T("1. Check which scheduler is used", "1. Уточните, какой планировщик используется"), BOLD),
            ("brain auto-sync status", ""),
            (T("Linux uses a systemd user timer when available, otherwise cron. If neither is available, auto-sync cannot be enabled.",
               "На Linux используется таймер пользователя systemd, а при его недоступности — cron. Если нет обоих, включить автосинхронизацию нельзя."), ""),
            ("", ""),
            (T("2. For systemd, run in a terminal", "2. Для systemd выполните в терминале"), BOLD),
            ("systemctl --user status agent-recall.timer --no-pager", ""),
            ("systemctl --user list-timers --all agent-recall.timer", ""),
            (T("active (waiting) means the timer is waiting for its next run. NEXT shows when it is due.",
               "active (waiting) означает ожидание следующего запуска. В колонке NEXT указано его время."), ""),
            (T("Check the last sync separately; the service can be inactive after successful completion:",
               "Последнюю синхронизацию проверьте отдельно: после успешного завершения служба может быть inactive:"), ""),
            ("systemctl --user status agent-recall.service --no-pager", ""),
            ("journalctl --user -u agent-recall.service -n 30 --no-pager", ""),
            (T("Unit files are in the user configuration directory:", "Файлы задачи находятся в каталоге настроек пользователя:"), DIM),
            (unit_files, ""),
            (T("For a legacy installation, also check brainsync.timer and brainsync.service.",
               "Для старой установки также проверьте brainsync.timer и brainsync.service."), DIM),
            ("", ""),
            (T("3. For cron, inspect your crontab", "3. Для cron проверьте свой crontab"), BOLD),
            ("crontab -l", ""),
            (T("Find the line ending in # agent-recall sync (or # brainsync sync). It should run Python with -m agent_recall and sync.",
               "Найдите строку с окончанием # agent-recall sync (или # brainsync sync). Она должна запускать Python с -m agent_recall и sync."), ""),
            (T("Legacy BrainSync jobs may use -m brainsync instead.",
               "В старых задачах BrainSync вместо этого может быть -m brainsync."), DIM),
            (T("*/15 in the first field means every 15 minutes. A line starting with # is paused.",
               "*/15 в первом поле означает запуск каждые 15 минут. Строка, начинающаяся с #, находится на паузе."), ""),
            ("", ""),
            (T("After disabling in brain", "После отключения в brain"), BOLD),
            (T("The timer must be inactive and its timer/service files removed. Cron must have no matching sync lines, including commented ones.",
               "Таймер должен быть неактивен, а файлы timer/service — удалены. В cron не должно оставаться строк этой синхронизации, включая закомментированные."), ""),
            (T("Unit not found or no crontab can be expected after removal. Failed to connect to bus or Permission denied means the check failed.",
               "Unit not found или отсутствие crontab допустимы после удаления. Failed to connect to bus или Permission denied означают, что проверка не удалась."), DIM),
            ("https://www.freedesktop.org/software/systemd/man/latest/systemctl.html", DIM),
        ]
    else:
        raise ValueError(system)
    return common + steps + [
        ("", ""),
        (T("Check the sync result too", "Проверьте и результат синхронизации"), BOLD),
        (T("A registered job is not proof that data was collected successfully. See Status and paths for the brain.log location.",
           "Наличие задачи не подтверждает успешный сбор данных. Путь к brain.log указан в разделе «Состояние и пути»."), ""),
        (T("After the next scheduled run, check the log time and errors. Disabling auto-sync keeps the database and manual sync.",
           "После следующего запуска по расписанию проверьте время записей и ошибки в журнале. Отключение автосинхронизации сохраняет базу и ручную синхронизацию."), ""),
    ]
