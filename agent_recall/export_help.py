"""Short, source-linked data-export instructions for the terminal import flow."""
from .i18n import T
import errno
import json
import zipfile

from .tui import BOLD, DIM, YELLOW, RED, Line

CLAUDE_SETTINGS = "https://claude.ai/settings/data-privacy-controls"
CLAUDE_HELP = "https://support.claude.com/en/articles/9450526-export-your-claude-data"
CHATGPT_SETTINGS = "https://chatgpt.com/#settings/DataControls"
CHATGPT_HELP = "https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data"


def claude_folder_guide() -> list[Line]:
    return [
        (T("Prepare one export folder:", "Подготовьте одну папку экспорта:"), BOLD),
        ("export/", DIM),
        ("  conversations.json", ""),
        ("  conversations-000.zip", DIM),
        ("  frames-000.zip", DIM), ("  projects-000.zip", DIM),
        ("  memories-000.zip", DIM), ("  feedback-000.zip", DIM), ("  light_metadata-000.zip", DIM),
        (T("Extract conversations.json from conversations-000.zip into this folder.",
           "Извлеките conversations.json из conversations-000.zip в эту папку."), ""),
        (T("Leave the other ZIP files compressed and choose the whole folder here.",
           "Остальные ZIP-файлы оставьте в исходном виде. Здесь выберите всю папку."), ""),
        (T("Extra archives are optional. A conversations ZIP can also be read directly.",
           "Дополнительные архивы необязательны. ZIP с перепиской также можно читать без распаковки."), DIM),
    ]


def import_problem(error: Exception) -> list[Line]:
    """Explain common input and filesystem failures without a traceback."""
    if isinstance(error, FileNotFoundError):
        return [(T("The chat export could not be found at this location.", "По этому пути не найден экспорт чатов."), RED),
                (T("Choose the whole export folder, conversations.json, or conversations-000.zip. Other ZIPs do not contain chats.",
                   "Выберите всю папку экспорта, conversations.json или conversations-000.zip. В остальных ZIP нет переписки."), "")]
    if isinstance(error, zipfile.BadZipFile):
        return [(T("The ZIP could not be opened. It may be incomplete or damaged.",
                   "Не удалось открыть ZIP. Возможно, он повреждён или скачан не полностью."), RED),
                (T("Download it again and keep all export files together.",
                   "Скачайте его заново и сохраните все файлы экспорта рядом."), "")]
    if isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return [(T("The conversation file could not be read.", "Не удалось прочитать файл переписки."), RED),
                (T("Use the original conversations.json from the export, without editing it.",
                   "Используйте исходный conversations.json из экспорта, не редактируя его."), "")]
    if isinstance(error, PermissionError):
        return [(T("The selected file or folder is not accessible.", "Нет доступа к выбранному файлу или папке."), RED),
                (T("Check access permissions or choose another local folder.",
                   "Проверьте права доступа или выберите другую локальную папку."), "")]
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return [(T("There is not enough free disk space.", "На диске недостаточно свободного места."), RED),
                (T("Free some space or choose an archive folder on another drive.",
                   "Освободите место или выберите папку архива на другом диске."), "")]
    if isinstance(error, TimeoutError):
        return [(T("Codex did not respond in time.", "Codex не ответил вовремя."), RED),
                (T("Close the Codex desktop app and retry. Already transferred chats are preserved.",
                   "Закройте приложение Codex и повторите попытку. Уже перенесённые чаты сохранятся."), "")]
    if isinstance(error, ValueError):
        return [(str(error), RED),
                (T("Check that you selected a data export from the correct service.",
                   "Проверьте, что выбрали экспорт данных нужного сервиса."), "")]
    return [(T("The import could not be completed.", "Не удалось завершить импорт."), RED), (str(error), DIM),
            (T("Check the paths and retry. Source files are preserved.",
               "Проверьте пути и повторите попытку. Исходные файлы сохраняются."), "")]


def export_guide(provider: str) -> list[Line]:
    if provider == "claude":
        return [
            (T("1. Open Claude on the web or desktop and sign in.",
               "1. Откройте Claude на сайте или компьютере и войдите в аккаунт."), BOLD),
            (CLAUDE_SETTINGS, ""),
            (T("2. Settings → Privacy → Export data.", "2. Settings → Privacy → Export data."), ""),
            (T("3. Wait for the email, then download the export while signed in.",
               "3. Дождитесь письма и скачайте экспорт, войдя в тот же аккаунт."), ""),
            (T("The link expires after 24 hours. Request another export if needed.",
               "Ссылка действует 24 часа. Если срок истёк — запросите экспорт заново."), DIM),
            (T("4. Keep the downloaded ZIP files together in one folder.",
               "4. Сохраните скачанные ZIP-файлы вместе в одной папке."), ""),
            *claude_folder_guide(),
            (T("5. Return here and choose the prepared folder.",
               "5. Вернитесь сюда и выберите подготовленную папку."), ""),
            (T("Before transferring to Codex, fully close its desktop app. Open it again after the transfer finishes.",
               "Перед переносом в Codex полностью закройте его приложение. После завершения переноса откройте его заново."), YELLOW),
            (T("Mobile apps cannot request this export.", "Запрос экспорта недоступен в мобильном приложении."), DIM),
            ("", ""), (T("Official instructions:", "Официальная инструкция:"), BOLD), (CLAUDE_HELP, ""),
        ]
    return [
        (T("1. Sign in to ChatGPT on the web.", "1. Войдите в ChatGPT на сайте."), BOLD),
        (CHATGPT_SETTINGS, ""),
        (T("2. Settings → Data controls → Export data → Confirm export.",
           "2. Settings → Data controls → Export data → Confirm export."), ""),
        (T("3. Wait for the email or SMS; delivery can take up to 7 days.",
           "3. Дождитесь письма или SMS; подготовка может занять до 7 дней."), ""),
        (T("4. Download the ZIP within 24 hours, signed in to the same account.",
           "4. Скачайте ZIP в течение 24 часов, войдя в тот же аккаунт."), ""),
        (T("5. Return here and paste the ZIP path. No extraction is needed.",
           "5. Вернитесь сюда и вставьте путь к ZIP. Распаковывать не нужно."), ""),
        ("", ""), (T("Official instructions:", "Официальная инструкция:"), BOLD), (CHATGPT_HELP, ""),
    ]
