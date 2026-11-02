"""Full-screen terminal UI: `brain` with no arguments (or `brain ui`).

Everything here is a front end over the same functions the CLI uses (query, handoff,
sync, importers, integrations). Screens:

  Sessions  — every agent session / imported chat; copy a handoff, read the conversation
  Search    — full-text search over all sources
  Recent    — latest items
  Projects  — timelines and project handoffs
  Sync now, Import a chat export, Agents (MCP), Status & paths, Language
"""
from __future__ import annotations

import io
import re
import sqlite3
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from . import db as dbmod
from . import handoff as ho
from . import integrations, query
from .config import expand
from .i18n import T, lang, set_lang
from .tui import (BOLD, CYAN, DIM, GREEN, RED, YELLOW, Item, Line, Quit, Term, keys_hint, message,
                  select, view, ask_text)

SHORT = {"claude_code": "Claude", "codex": "Codex", "claude_app": "Claude app", "chatgpt": "ChatGPT",
         "obsidian": "Obsidian", "github": "GitHub"}


# ---- small helpers ----------------------------------------------------------------------

def local(ts: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except ValueError:
        return ts[:16].replace("T", " ")


def day_label(ts: str | None) -> str:
    d = local(ts, "%Y-%m-%d")
    if not d:
        return T("No date", "Без даты")
    today = datetime.now().date()
    if d == today.isoformat():
        return T("Today", "Сегодня")
    if d == (today - timedelta(days=1)).isoformat():
        return T("Yesterday", "Вчера")
    return d


def clip(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def body_lines(text: str | None, limit: int | None = None) -> list[Line]:
    lines = [(l, "") for l in (text or "").splitlines()]
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + [("…", DIM)]
    return lines


def nice_title(title: str) -> str:
    """Stored message titles start with the role ("user: …"); show it the human way."""
    for role, label in (("user: ", T("Me: ", "Я: ")), ("assistant: ", T("AI: ", "ИИ: ")), ("tool: ", "⚙ ")):
        if title.startswith(role):
            return label + title[len(role):]
    return title


class App:
    def __init__(self, cfg: dict, term: Term):
        self.cfg = cfg
        self.t = term
        self.db_path = expand(cfg["db_path"])
        self._titles: dict[str, str] = {}
        self._session_info: dict[str, list[Line]] = {}

    # -- database --
    def conn(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        return dbmod.connect(self.db_path, readonly=True)

    def title(self, conn: sqlite3.Connection, thread_id: str) -> str:
        if thread_id not in self._titles:
            self._titles[thread_id] = ho.thread_title(conn, thread_id)
        return self._titles[thread_id]

    # -- main menu --
    def run(self) -> None:
        try:
            while True:
                choice = self.main_menu()
                if choice in (None, "quit"):
                    return
                getattr(self, f"screen_{choice}")()
        except Quit:
            return

    def main_menu(self) -> str | None:
        conn = self.conn()
        no_db = T("no database yet — run Sync now first", "базы ещё нет — сначала «Синхронизировать»")
        n_threads = n_projects = 0
        last_sync = ""
        if conn:
            with closing(conn):
                n_threads = conn.execute(
                    f"SELECT COUNT(DISTINCT thread_id) FROM items WHERE source IN "
                    f"({','.join('?' * len(query.CHAT_SOURCES))})", query.CHAT_SOURCES).fetchone()[0]
                n_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                row = conn.execute("SELECT MAX(ts) FROM sync_log").fetchone()
                last_sync = local(row[0], "%d.%m %H:%M") if row and row[0] else ""
        dis = None if conn else no_db
        items = [
            Item("#work", T("Work with your memory", "Работа с памятью"), header=True),
            Item("sessions", T("Sessions — continue in another AI", "Сессии — продолжить в другом ИИ"),
                 str(n_threads) if conn else "", DIM, disabled=dis),
            Item("search", T("Search", "Поиск"), disabled=dis),
            Item("recent", T("Recent", "Последнее"), disabled=dis),
            Item("projects", T("Projects", "Проекты"), str(n_projects) if conn else "", DIM, disabled=dis),
            Item("#data", T("Data", "Данные"), header=True),
            Item("sync", T("Sync now", "Синхронизировать"), last_sync, DIM),
            Item("import", T("Import a chat export (Claude app, ChatGPT)…", "Импорт экспорта чатов (Claude, ChatGPT)…")),
            Item("#setup", T("Setup", "Настройка"), header=True),
            Item("agents", T("Agents (MCP)", "Агенты (MCP)")),
            Item("status", T("Status and paths", "Состояние и пути")),
            Item("language", T("Language: English", "Язык: русский")),
            Item("quit", T("Quit", "Выход")),
        ]
        about = {
            "sessions": [(T("Every Claude Code and Codex session, plus imported Claude app and ChatGPT chats.",
                            "Все сессии Claude Code и Codex, плюс импортированные чаты Claude и ChatGPT."), ""), ("", ""),
                         (T("Pick one to copy a handoff — a briefing another AI can continue from — or to read it.",
                            "Выберите сессию, чтобы скопировать handoff — выжимку, с которой другой ИИ продолжит работу, — или прочитать её."), "")],
            "search": [(T("Full-text search over notes, sessions, chats and GitHub.",
                          "Полнотекстовый поиск по заметкам, сессиям, чатам и GitHub."), ""), ("", ""),
                       (T("All words must match; word beginnings work too: “certif” finds “certificates”.",
                          "Совпасть должны все слова; можно начало слова: «сертиф» найдёт «сертификаты»."), DIM)],
            "recent": [(T("The latest notes, messages and commits.", "Последние заметки, сообщения и коммиты."), "")],
            "projects": [(T("Project timelines and project handoffs.", "Таймлайны проектов и handoff по проекту."), ""), ("", ""),
                         (T("Projects are rules in config.toml: working folder, repo, note path, tags, Jira keys.",
                            "Проекты — это правила в config.toml: рабочая папка, репозиторий, путь заметки, теги, ключи Jira."), DIM)],
            "sync": [(T("Pull new data from all sources right now.", "Забрать новые данные из всех источников прямо сейчас."), ""), ("", ""),
                     (T("It also runs on a schedule (brainctl task).", "Это же происходит по расписанию (brainctl task)."), DIM)],
            "import": [(T("Chats from the Claude app and ChatGPT live on their servers. Request a data export in their "
                          "settings, download the zip and import it here. Re-importing adds only new messages.",
                          "Чаты приложения Claude и ChatGPT хранятся на их серверах. Запросите экспорт данных в настройках, "
                          "скачайте zip и импортируйте здесь. Повторный импорт добавит только новое."), "")],
            "agents": [(T("Connect the memory to Claude Code, Codex and Cline over MCP, so they can search it themselves.",
                          "Подключить память к Claude Code, Codex и Cline через MCP, чтобы они сами искали в ней."), "")],
            "status": [(T("Where the config, database and logs live; what each source finds; which agents are connected.",
                          "Где лежат конфиг, база и логи; что видит каждый источник; какие агенты подключены."), "")],
            "language": [(T("Switch the interface and handoff language.", "Переключить язык интерфейса и выжимок handoff."), "")],
            "quit": [],
        }
        res = select(self.t, items, title=T("What do you want to do?", "Что сделать?"), crumbs=T("Home", "Главная"),
                     details=lambda it: about.get(it.key, []),
                     hint=keys_hint(details=False, back=T("Esc quit", "Esc выход"), search=False))
        if res.back:
            return "quit"
        return res.keys[0] if res.keys else None

    # -- sessions --------------------------------------------------------------------------
    def session_details(self, conn: sqlite3.Connection, tid: str, src: str, row: dict) -> list[Line]:
        if tid in self._session_info:
            return self._session_info[tid]
        first = conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                                AND author IN ('user', 'human') ORDER BY created_at, rid LIMIT 5""", (tid,)).fetchall()
        first_text = next((ho.clean(b[0]) for b in first if ho.clean(b[0])), "")
        last = conn.execute("""SELECT body FROM items WHERE thread_id = ? AND kind = 'message'
                               AND author = 'assistant' ORDER BY created_at DESC, rid DESC LIMIT 3""", (tid,)).fetchall()
        last_text = next((ho.clean(b[0]) for b in last if ho.clean(b[0]) and not b[0].startswith("[tool ")), "")
        projects = [p[0] for p in conn.execute(
            """SELECT DISTINCT ip.project_id FROM item_projects ip JOIN items i ON i.id = ip.item_id
               WHERE i.thread_id = ?""", (tid,))]
        info: list[Line] = [(self.title(conn, tid), BOLD),
                            (f"{ho.source_name(src)} · {local(row['first_at'])} — {local(row['last_at'], '%H:%M')} · "
                             + T(f"{row['n']} messages", f"сообщений: {row['n']}"), DIM)]
        if row.get("cwd"):
            info.append((T("Folder: ", "Папка: ") + row["cwd"], DIM))
        if projects:
            info.append((T("Project: ", "Проект: ") + ", ".join(projects), GREEN))
        if first_text:
            info += [("", ""), (T("How it started", "С чего началось"), CYAN), (clip(first_text, 500), "")]
        if last_text:
            info += [("", ""), (T("Last reply", "Последний ответ"), CYAN), (clip(last_text, 600), "")]
        info += [("", ""), (T("Enter — copy a handoff for another AI", "Enter — скопировать handoff для другого ИИ"), YELLOW)]
        self._session_info[tid] = info
        return info

    def screen_sessions(self, project: str | None = None) -> None:
        start = None
        while True:
            conn = self.conn()
            if not conn:
                return
            with closing(conn):
                rows = query.list_threads(conn, project=project, limit=400)
                if not rows:
                    message(self.t, [(T("No sessions yet. Run Sync now, or import a chat export.",
                                        "Сессий пока нет. Запустите синхронизацию или импортируйте экспорт чатов."), "")],
                            title=T("Sessions", "Сессии"))
                    return
                by_id = {r["thread_id"]: r for r in rows}
                items: list[Item] = []
                day = None
                for r in rows:
                    d = day_label(r["last_at"])
                    if d != day:
                        day = d
                        items.append(Item(f"#{d}", d, header=True))
                    items.append(Item(r["thread_id"], self.title(conn, r["thread_id"]),
                                      f"{SHORT.get(r['source'], r['source'])} · {local(r['last_at'], '%H:%M')}", DIM,
                                      search=f"{r['source']} {SHORT.get(r['source'], '')} {r.get('cwd') or ''}"))
                crumbs = T("Sessions", "Сессии") + (f" › {project}" if project else "")
                res = select(self.t, items, title=T("Which session to continue elsewhere?", "Какую сессию продолжить в другом месте?"),
                             crumbs=crumbs, start=start,
                             details=lambda it: self.session_details(conn, it.key, by_id[it.key]["source"], by_id[it.key]),
                             hint=keys_hint(enter=T("actions", "действия")))
                if res.back:
                    return
                start = res.keys[0]
                self.session_actions(conn, start, crumbs)

    def session_actions(self, conn: sqlite3.Connection, tid: str, crumbs: str) -> None:
        name = self.title(conn, tid)
        while True:
            res = select(self.t, [
                Item("copy", T("Copy handoff to clipboard", "Скопировать handoff в буфер обмена")),
                Item("save", T("Save handoff to a file…", "Сохранить handoff в файл…")),
                Item("preview", T("Preview the handoff", "Посмотреть handoff")),
                Item("read", T("Read the conversation", "Прочитать переписку")),
                Item("back", T("Back", "Назад")),
            ], title=name, crumbs=f"{crumbs} › {clip(name, 40)}",
                details=lambda it: [(T("A handoff is a Markdown briefing: the task, files and commands, where it stopped. "
                                       "Paste it into Codex, Claude, ChatGPT — any AI — to continue.",
                                       "Handoff — выжимка в Markdown: задача, файлы и команды, где остановились. "
                                       "Вставьте её в Codex, Claude, ChatGPT — любой ИИ — чтобы продолжить."), "")],
                hint=keys_hint(details=False, search=False))
            if res.back or res.keys == ["back"]:
                return
            act = res.keys[0]
            if act == "read":
                self.read_thread(conn, tid, name)
                continue
            text = ho.thread_handoff(conn, tid)
            if act == "preview":
                view(self.t, body_lines(text), title=T("Handoff", "Handoff"), crumbs=f"{crumbs} › handoff")
                continue
            if act == "copy":
                self.deliver(text, tid)
            elif act == "save":
                self.save(text, tid)
            return

    def deliver(self, text: str, name: str) -> None:
        if ho.copy_to_clipboard(text):
            message(self.t, [(T(f"Copied to the clipboard: {len(text)} characters.", f"Скопировано в буфер обмена: {len(text)} символов."), GREEN),
                             ("", ""),
                             (T("Paste it into any AI and it will restate the task and continue from where you stopped.",
                                "Вставьте в любой ИИ — он перескажет задачу и продолжит с того места, где вы остановились."), "")],
                    title=T("Handoff ready", "Handoff готов"))
        else:
            path = self.write_handoff(text, name)
            message(self.t, [(T("The clipboard is not available here, so the handoff was saved to a file:",
                                "Буфер обмена недоступен, поэтому handoff сохранён в файл:"), YELLOW), (str(path), BOLD)],
                    title=T("Handoff ready", "Handoff готов"))

    def default_handoff_path(self, name: str) -> Path:
        safe = re.sub(r"[^\w.-]+", "_", name)[:60] or "handoff"
        return self.db_path.parent / "handoffs" / f"handoff-{safe}.md"

    def write_handoff(self, text: str, name: str, path: Path | None = None) -> Path:
        path = path or self.default_handoff_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def save(self, text: str, name: str) -> None:
        default = str(self.default_handoff_path(name))
        target = ask_text(self.t, T("File to save the handoff to:", "Файл, куда сохранить handoff:"),
                          title=T("Save handoff", "Сохранить handoff"), default=default)
        if not target:
            return
        try:
            path = self.write_handoff(text, name, expand(target.strip().strip('"')))
        except OSError as e:
            message(self.t, [(str(e), RED)], title=T("Could not save", "Не удалось сохранить"))
            return
        message(self.t, [(T("Saved:", "Сохранено:"), GREEN), (str(path), BOLD)], title=T("Handoff saved", "Handoff сохранён"))

    def read_thread(self, conn: sqlite3.Connection, tid: str, name: str) -> None:
        lines: list[Line] = []
        for r in query.get_thread(conn, tid, limit=2000):
            text = ho.clean(r["body"])
            if not text:
                continue
            me = r["author"] in ("user", "human")
            who = T("Me", "Я") if me else (T("Assistant", "Ассистент") if r["author"] == "assistant" else r["author"] or r["kind"])
            lines += [(f"{who} · {local(r['created_at'])}", f"{BOLD};{CYAN if me else GREEN}")]
            lines += body_lines(text)
            lines.append(("", ""))
        view(self.t, lines or [(T("Empty session", "Пустая сессия"), DIM)], title=name,
             crumbs=T("Sessions › conversation", "Сессии › переписка"))

    # -- search and recent -----------------------------------------------------------------
    def item_details(self, conn: sqlite3.Connection, item_id: str) -> list[Line]:
        it = query.get_item(conn, item_id)
        if not it:
            return []
        where = it.get("repo") or it.get("cwd") or it.get("path") or ""
        info: list[Line] = [(nice_title(it.get("title") or ""), BOLD),
                            (f"{ho.source_name(it['source'])} · {it['kind']} · {local(it.get('created_at'))}", DIM)]
        if where:
            info.append((where, DIM))
        if it.get("projects"):
            info.append((T("Project: ", "Проект: ") + ", ".join(it["projects"]), GREEN))
        info.append(("", ""))
        info += body_lines(ho.clean(it.get("body")), limit=40)
        return info

    def results(self, conn: sqlite3.Connection, rows: list[dict], title: str, crumbs: str, group_by_day: bool) -> None:
        start = None
        while True:
            items: list[Item] = []
            day = None
            for r in rows:
                if group_by_day:
                    d = day_label(r["created_at"])
                    if d != day:
                        day = d
                        items.append(Item(f"#{d}", d, header=True))
                items.append(Item(r["id"], clip(nice_title(r.get("title") or r["id"]), 200),
                                  f"{SHORT.get(r['source'], r['source'])} · {local(r['created_at'], '%d.%m')}", DIM,
                                  search=r["source"]))
            res = select(self.t, items, title=title, crumbs=crumbs, start=start,
                         details=lambda it: self.item_details(conn, it.key), hint=keys_hint(enter=T("open", "открыть")))
            if res.back:
                return
            start = res.keys[0]
            self.open_item(conn, start, crumbs)

    def open_item(self, conn: sqlite3.Connection, item_id: str, crumbs: str) -> None:
        it = query.get_item(conn, item_id)
        if not it:
            return
        if it["source"] in query.CHAT_SOURCES and it.get("thread_id"):
            res = select(self.t, [
                Item("item", T("Read this message", "Прочитать это сообщение")),
                Item("session", T("Open the whole session…", "Открыть всю сессию…")),
                Item("back", T("Back", "Назад")),
            ], title=clip(it.get("title"), 80), crumbs=crumbs, hint=keys_hint(details=False, search=False))
            if res.back or res.keys == ["back"]:
                return
            if res.keys == ["session"]:
                self.session_actions(conn, it["thread_id"], crumbs)
                return
        view(self.t, self.item_details(conn, item_id)[:4] + [("", "")] + body_lines(ho.clean(it.get("body"))),
             title=clip(it.get("title"), 80), crumbs=crumbs)

    def screen_search(self) -> None:
        q = ""
        while True:
            q = ask_text(self.t, T("Search for:", "Что искать:"), title=T("Search", "Поиск"),
                         crumbs=T("Search", "Поиск"), default=q,
                         note=[(T("All words must match. Word beginnings work: “certif” finds “certificates”. "
                                  "Use \"quotes\" for an exact phrase.",
                                  "Совпасть должны все слова. Можно начало слова: «сертиф» найдёт «сертификаты». "
                                  "Точная фраза — в \"кавычках\"."), DIM)])
            if not q:
                return
            conn = self.conn()
            if not conn:
                return
            with closing(conn):
                rows = query.search(conn, q, limit=100, fallback_or=False)
                if not rows:
                    message(self.t, [(T(f"Nothing contains all of: {q}", f"Нет записей, где есть все слова: {q}"), YELLOW)],
                            title=T("Search", "Поиск"))
                    continue
                self.results(conn, rows, T(f"Found {len(rows)}: {q}", f"Найдено {len(rows)}: {q}"),
                             T("Search", "Поиск") + f" › {clip(q, 30)}", group_by_day=False)

    def screen_recent(self) -> None:
        conn = self.conn()
        if not conn:
            return
        with closing(conn):
            rows = query.recent(conn, limit=150)
            self.results(conn, rows, T("Latest notes, messages and commits", "Последние заметки, сообщения и коммиты"),
                         T("Recent", "Последнее"), group_by_day=True)

    # -- projects ------------------------------------------------------------------------------
    def screen_projects(self) -> None:
        while True:
            conn = self.conn()
            if not conn:
                return
            with closing(conn):
                rows = query.list_projects(conn)
            if not rows:
                message(self.t, [
                    (T("No projects yet.", "Проектов пока нет."), YELLOW), ("", ""),
                    (T("Add rules to the [[projects]] section of your config:", "Добавьте правила в раздел [[projects]] конфига:"), ""),
                    (str(self.cfg["_config_path"]), BOLD), ("", ""),
                    ("[[projects]]", CYAN), ('id = "shop"', CYAN), ('name = "Online shop"', CYAN),
                    ('cwd = ["*shop-frontend*"]', CYAN), ('jira = ["SHOP"]', CYAN), ("", ""),
                    (T("Then run Sync now (or: brain projects --rebuild).", "Затем «Синхронизировать» (или: brain projects --rebuild)."), DIM),
                ], title=T("Projects", "Проекты"))
                return
            rules = {p["id"]: p for p in self.cfg.get("projects", [])}

            def details(it: Item) -> list[Line]:
                r = next(x for x in rows if x["id"] == it.key)
                info: list[Line] = [(r["name"], BOLD),
                                    (T(f"{r['items']} items · {local(r['first_at'], '%Y-%m-%d')} — {local(r['last_at'], '%Y-%m-%d')}",
                                       f"записей: {r['items']} · {local(r['first_at'], '%Y-%m-%d')} — {local(r['last_at'], '%Y-%m-%d')}"), DIM),
                                    ("", "")]
                rule = rules.get(it.key)
                if rule:
                    info.append((T("Rules", "Правила"), CYAN))
                    for k, v in rule.items():
                        if k not in ("id", "name") and v:
                            info.append((f"  {k}: {', '.join(v) if isinstance(v, list) else v}", ""))
                return info

            res = select(self.t, [Item(r["id"], r["name"], str(r["items"]), DIM) for r in rows],
                         title=T("Projects", "Проекты"), crumbs=T("Projects", "Проекты"), details=details,
                         hint=keys_hint(enter=T("actions", "действия")))
            if res.back:
                return
            self.project_actions(res.keys[0])

    def project_actions(self, pid: str) -> None:
        while True:
            res = select(self.t, [
                Item("timeline", T("Timeline", "Таймлайн")),
                Item("sessions", T("Sessions of this project", "Сессии проекта")),
                Item("copy", T("Copy project handoff (last 30 days)", "Скопировать handoff проекта (30 дней)")),
                Item("back", T("Back", "Назад")),
            ], title=pid, crumbs=T("Projects", "Проекты") + f" › {pid}", hint=keys_hint(details=False, search=False))
            if res.back or res.keys == ["back"]:
                return
            act = res.keys[0]
            if act == "sessions":
                self.screen_sessions(project=pid)
                continue
            conn = self.conn()
            if not conn:
                return
            with closing(conn):
                if act == "timeline":
                    lines: list[Line] = []
                    day = None
                    for r in query.timeline(conn, pid, limit=2000):
                        if r["day"] != day:
                            day = r["day"]
                            lines += [("", ""), (day, f"{BOLD};{CYAN}")]
                        n = T(f" ({r['n']} msgs)", f" ({r['n']} сообщ.)") if r["n"] > 1 and r["source"] in query.CHAT_SOURCES else ""
                        lines.append((f"  {SHORT.get(r['source'], r['source']):9} {clip(r['title'] or '', 200)}{n}", ""))
                    view(self.t, lines or [(T("Nothing yet", "Пока ничего"), DIM)], title=T("Timeline", "Таймлайн"),
                         crumbs=T("Projects", "Проекты") + f" › {pid} › " + T("timeline", "таймлайн"))
                elif act == "copy":
                    self.deliver(ho.project_handoff(conn, pid, days=30), f"project-{pid}")

    # -- data ---------------------------------------------------------------------------------------
    def busy(self, text: str) -> None:
        cols, rows = self.t.size()
        from .tui import footer, header, style
        lines = header(T("Please wait", "Подождите"), text, cols) + ["", style("  " + text, BOLD, YELLOW)]
        lines += [""] * (rows - 1 - len(lines)) + [footer(T("Working…", "Работаю…"), cols)]
        self.t.draw(lines)

    def screen_sync(self) -> None:
        from .sync import run_sync
        self.busy(T("Syncing… the first run can take a few minutes", "Синхронизация… первый раз может занять несколько минут"))
        res = run_sync(self.cfg)
        self._titles.clear()
        self._session_info.clear()
        if res.get("skipped"):
            message(self.t, [(T("Another sync is running right now (probably the scheduled one). Try again in a minute.",
                                "Сейчас идёт другая синхронизация (скорее всего, по расписанию). Попробуйте через минуту."), YELLOW)],
                    title=T("Sync", "Синхронизация"))
            return
        lines: list[Line] = []
        for name, r in res["sources"].items():
            if r["error"]:
                lines.append((f"  {name:12} " + T("error: ", "ошибка: ") + r["error"], RED))
            else:
                lines.append((f"  {name:12} " + T(f"new or changed: {r['written']}", f"новых или изменённых: {r['written']}"), ""))
        lines += [("", ""), (T(f"Project links: {res.get('tagged', 0)}", f"Привязок к проектам: {res.get('tagged', 0)}"), DIM)]
        message(self.t, lines, title=T("Sync finished", "Синхронизация завершена"))

    def screen_import(self) -> None:
        from .importers import import_export
        from .projects import assign_projects
        path = ask_text(self.t, T("Path to the export (.zip, folder or conversations.json):",
                                  "Путь к экспорту (.zip, папка или conversations.json):"),
                        title=T("Import a chat export", "Импорт экспорта чатов"),
                        note=[(T("Claude: Settings → Privacy → Export data (web or desktop). The link in the email expires in 24 hours.",
                                 "Claude: Settings → Privacy → Export data (сайт или десктоп). Ссылка из письма действует 24 часа."), ""),
                              (T("ChatGPT: Settings → Data controls → Export data.", "ChatGPT: Settings → Data controls → Export data."), ""),
                              ("", ""),
                              (T("Tip: drag the file into this window to paste its path.",
                                 "Совет: перетащите файл в это окно — путь вставится сам."), DIM)])
        if not path:
            return
        p = expand(path.strip().strip('"').strip("'"))
        if not p.exists():
            message(self.t, [(T(f"Not found: {p}", f"Не найдено: {p}"), RED)], title=T("Import", "Импорт"))
            return
        self.busy(T("Importing…", "Импортирую…"))
        try:
            conn = dbmod.connect(self.db_path)
            with closing(conn):
                res = import_export(conn, p)
                src = {"claude": "claude_app", "chatgpt": "chatgpt"}[res["format"]]
                dbmod.log_sync(conn, src, res["written"], 0, None)
                tagged = assign_projects(conn, self.cfg)
                conn.commit()
        except Exception as e:
            message(self.t, [(str(e), RED)], title=T("Import failed", "Импорт не удался"))
            return
        self._titles.clear()
        self._session_info.clear()
        message(self.t, [(T(f"Chats: {res['conversations']}", f"Чатов: {res['conversations']}"), ""),
                         (T(f"New or changed messages: {res['written']}", f"Новых или изменённых сообщений: {res['written']}"), GREEN),
                         (T(f"Project links: {tagged}", f"Привязок к проектам: {tagged}"), DIM), ("", ""),
                         (T("They are now in Sessions and Search.", "Теперь они есть в «Сессиях» и «Поиске»."), "")],
                title=T("Import finished", "Импорт завершён"))

    # -- setup ----------------------------------------------------------------------------------------
    def screen_agents(self) -> None:
        start = None
        while True:
            self.busy(T("Checking agents…", "Проверяю агентов…"))
            states = {a: integrations.state(a) for a in integrations.AGENTS}
            label = {"on": (T("✓ connected", "✓ подключён"), GREEN), "off": (T("not connected", "не подключён"), YELLOW),
                     "missing": (T("not installed", "не установлен"), DIM)}
            items = [Item(a, integrations.AGENT_NAMES[a], label[s][0], label[s][1],
                          disabled=T("not installed on this computer", "не установлен на этом компьютере") if s == "missing" else None)
                     for a, s in states.items()]
            res = select(self.t, items, title=T("Which agents can search your memory?", "Каким агентам доступна память?"),
                         crumbs=T("Agents (MCP)", "Агенты (MCP)"), start=start,
                         details=lambda it: [(T("Enter connects or disconnects. After connecting, restart the agent; "
                                                "it gets the tools search, get_thread, timeline and handoff.",
                                                "Enter подключает или отключает. После подключения перезапустите агента; "
                                                "у него появятся инструменты search, get_thread, timeline и handoff."), ""),
                                             ("", ""),
                                             (T("Settings of the agent are backed up before any change (*.bak-date).",
                                                "Перед изменением настройки агента сохраняются в копию (*.bak-дата)."), DIM)],
                         hint=keys_hint(enter=T("switch", "переключить"), search=False))
            if res.back:
                return
            a = start = res.keys[0]
            try:
                out = integrations.REMOVE[a]() if states[a] == "on" else integrations.ADD[a](None)
                message(self.t, [(out, GREEN)], title=integrations.AGENT_NAMES[a])
            except Exception as e:
                message(self.t, [(str(e), RED)], title=integrations.AGENT_NAMES[a])

    def screen_status(self) -> None:
        from .cli import cmd_doctor, cmd_paths
        self.busy(T("Checking…", "Проверяю…"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                cmd_paths(self.cfg, SimpleNamespace(json=False))
                print()
                cmd_doctor(self.cfg, SimpleNamespace())
            except Exception as e:
                print(e)
        view(self.t, body_lines(buf.getvalue()), title=T("Status and paths", "Состояние и пути"),
             crumbs=T("Status", "Состояние"))

    def screen_language(self) -> None:
        res = select(self.t, [Item("en", "English", "✓" if lang() == "en" else "", GREEN),
                              Item("ru", "Русский", "✓" if lang() == "ru" else "", GREEN)],
                     title=T("Interface language", "Язык интерфейса"), crumbs=T("Language", "Язык"), start=lang(),
                     hint=keys_hint(details=False, search=False))
        if res.back or not res.keys:
            return
        new = res.keys[0]
        set_config_value(Path(self.cfg["_config_path"]), "language", new)
        self.cfg["language"] = new
        set_lang(new)


def set_config_value(path: Path, key: str, value: str) -> None:
    """Set a top-level `key = "value"` in config.toml, keeping everything else as is."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f'{key} = "{value}"'
    pat = re.compile(rf'(?m)^\s*{re.escape(key)}\s*=.*$')
    first_table = re.search(r"(?m)^\s*\[", text)
    head = text[: first_table.start()] if first_table else text
    if pat.search(head):
        head = pat.sub(line, head, count=1)
    else:
        head = line + "\n" + head
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(head + (text[first_table.start():] if first_table else ""), encoding="utf-8")


def run(cfg: dict, term: Term | None = None) -> int:
    with (term or Term()) as t:
        App(cfg, t).run()
    return 0
