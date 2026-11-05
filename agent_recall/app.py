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
import json
import os
import re
import sqlite3
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from . import db as dbmod
from . import handoff as ho
from . import integrations, query
from .config import expand, remember_import, set_config_value
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
        self._import_archive_root = expand(cfg.get("import_archive_dir") or self.db_path.parent / "imports")
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
            Item("auto_sync", T("Auto-sync", "Автосинхронизация")),
            Item("agents", T("Agents (MCP)", "Агенты (MCP)")),
            Item("status", T("Status and paths", "Состояние и пути")),
            Item("language", T("Language: English", "Язык: русский")),
            Item("help", T("Help", "Помощь")),
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
                     (T("Optional scheduling is available under Auto-sync.", "Расписание можно включить в разделе «Автосинхронизация»."), DIM)],
            "import": [(T("Add a Claude or ChatGPT export to shared memory, or transfer Claude chats into Codex history.",
                          "Добавить экспорт Claude или ChatGPT в общую память или перенести чаты Claude в историю Codex."), ""),
                       ("", ""), (T("Before importing, read Prepare Claude export or Prepare ChatGPT export in this menu.",
                                        "Перед импортом прочитайте «Подготовка экспорта Claude» или «Подготовка экспорта ChatGPT» в этом меню."), DIM)],
            "auto_sync": [(T("Choose whether to run sync automatically, and how often.",
                              "Выберите, запускать ли синхронизацию автоматически и как часто."), ""),
                          (T("Disabling removes the OS scheduler job. Your data and manual sync remain available.",
                             "При отключении задача удаляется из планировщика ОС. Данные и ручная синхронизация остаются доступны."), DIM)],
            "agents": [(T("Connect the memory to Claude Code, Codex and Cline over MCP, so they can search it themselves.",
                          "Подключить память к Claude Code, Codex и Cline через MCP, чтобы они сами искали в ней."), "")],
            "status": [(T("Where the config, database and logs live; what each source finds; which agents are connected.",
                          "Где лежат конфиг, база и логи; что видит каждый источник; какие агенты подключены."), "")],
            "language": [(T("Switch the interface and handoff language.", "Переключить язык интерфейса и выжимок handoff."), "")],
            "help": [(T("Find and independently check scheduled sync on Windows, macOS or Linux.",
                        "Как найти и самостоятельно проверить задачу автосинхронизации в Windows, macOS или Linux."), ""),
                     (T("Includes expected results when enabled and after removal, plus checks for older installations.",
                        "Ожидаемые результаты при включении и после удаления, а также проверка старых установок."), DIM)],
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
        from .export_help import export_guide
        details = {
            "memory": [(T("Before you start, read Prepare Claude export or Prepare ChatGPT export below.",
                           "Перед началом прочитайте «Подготовка экспорта Claude» или «Подготовка экспорта ChatGPT» ниже."), YELLOW),
                       (T("Chats become available in Sessions, Search and your connected AI agents.",
                           "Чаты появятся в «Сессиях», «Поиске» и станут доступны подключённым ИИ-агентам."), ""),
                       (T("Accepts Claude and ChatGPT exports. Re-importing adds new or changed messages.",
                          "Поддерживает экспорт Claude и ChatGPT. Повторный импорт добавляет новые и изменённые сообщения."), DIM)],
            "codex": [(T("Before you start, read Prepare Claude export below.",
                          "Перед началом прочитайте «Подготовка экспорта Claude» ниже."), YELLOW),
                      ("", ""),
                      (T("1. Request and download your Claude data export.",
                         "1. Запросите и скачайте экспорт данных Claude."), ""),
                      (T("2. Keep all files together. Extract conversations.json; leave the other ZIPs compressed.",
                         "2. Сохраните все файлы рядом. Извлеките conversations.json; остальные ZIP оставьте как есть."), ""),
                      (T("3. Fully close Codex, choose the export folder and where to save the archive.",
                         "3. Полностью закройте Codex, выберите папку экспорта и место для архива."), ""),
                      (T("4. Confirm the import. Open Codex again after it finishes.",
                         "4. Подтвердите импорт. После его завершения откройте Codex заново."), ""),
                      ("", ""),
                      (T("Chats appear in Codex history with the [Claude] title prefix.",
                          "Чаты появятся в истории Codex с префиксом [Claude] в названиях."), ""),
                      (T("Experimental. Projects are not created; artifact files are saved in a local archive.",
                         "Экспериментально. Проекты не создаются; файлы артефактов сохраняются в локальном архиве."), DIM)],
            "claude": export_guide("claude"), "chatgpt": export_guide("chatgpt"),
        }
        start = "memory"
        while True:
            res = select(self.t, [
                Item("#where", T("Where should the chats appear?", "Где должны появиться чаты?"), header=True),
                Item("memory", T("Add to shared memory", "Добавить в общую память"), "Claude, ChatGPT", DIM),
                Item("codex", T("Import into Codex history", "Импорт в историю Codex"), "Claude", DIM),
                Item("#help", T("Read before you start", "Прочитайте перед началом"), header=True),
                Item("claude", T("Prepare Claude export", "Подготовка экспорта Claude")),
                Item("chatgpt", T("Prepare ChatGPT export", "Подготовка экспорта ChatGPT")),
            ], title=T("Import chats", "Импорт чатов"), crumbs=T("Home › Import", "Главная › Импорт"),
                start=start, details=lambda it: details[it.key], hint=keys_hint(search=False))
            if res.back:
                return
            start = res.keys[0]
            if start in ("claude", "chatgpt"):
                view(self.t, details[start], title=T("Prepare export", "Подготовка экспорта") + " · "
                     + {"claude": "Claude", "chatgpt": "ChatGPT"}[start],
                     crumbs=T("Import", "Импорт") + " › " + start)
            elif start == "memory":
                self.import_memory()
            else:
                self.import_codex()

    def ask_export(self, title: str, *, claude_only: bool = False) -> Path | None:
        from .export_help import CLAUDE_SETTINGS, CHATGPT_SETTINGS, import_problem
        from .importers import detect_format, load_conversations
        notes = [(T("Already downloaded? Paste the ZIP or folder path, or drag it into this terminal.",
                    "Уже скачали? Вставьте путь к ZIP или папке либо перетащите их в терминал."), ""),
                 (T("Keep companion export ZIPs in the same folder to preserve documents and artifacts.",
                    "Храните остальные ZIP-файлы экспорта в той же папке, чтобы сохранить документы и артефакты."), DIM),
                 ("", ""), (T("Claude: Settings → Privacy → Export data", "Claude: Settings → Privacy → Export data"), ""),
                 (CLAUDE_SETTINGS, CYAN)]
        if claude_only:
            notes = [(T("Fully close the Codex desktop app before transferring chats.",
                        "Перед переносом чатов полностью закройте приложение Codex."), YELLOW),
                     (T("Choose the whole export folder. Extract conversations.json from conversations-000.zip; leave other ZIPs compressed.",
                        "Выберите всю папку экспорта. Извлеките conversations.json из conversations-000.zip; остальные ZIP оставьте как есть."), ""),
                     (T("Expected: conversations.json, frames-000.zip, projects-000.zip, and any other export ZIPs.",
                        "Пример: conversations.json, frames-000.zip, projects-000.zip и остальные ZIP экспорта."), DIM),
                     (T("Paste or drag the folder path into this terminal.", "Вставьте путь к папке или перетащите её в терминал."), "")]
        if not claude_only:
            notes += [(T("ChatGPT: Settings → Data controls → Export data", "ChatGPT: Settings → Data controls → Export data"), ""),
                      (CHATGPT_SETTINGS, CYAN)]
        notes += [(T("Need the full instructions? Esc returns to the import menu.",
                     "Нужна полная инструкция? Esc вернёт в меню импорта."), DIM)]
        while True:
            path = ask_text(self.t, T("Export ZIP, folder or conversations.json:", "ZIP экспорта, папка или conversations.json:"),
                            title=title, note=notes)
            if not path:
                return None
            p = expand(path.strip().strip('"').strip("'"))
            if p.exists():
                try:
                    data = load_conversations(p)
                    fmt = detect_format(data)
                    if fmt is None:
                        raise ValueError(T("This file does not contain a supported chat export.",
                                           "Этот файл не содержит поддерживаемого экспорта чатов."))
                    if claude_only and fmt != "claude":
                        raise ValueError(T("Transferring to Codex requires a Claude export. Choose shared memory for a ChatGPT export.",
                                           "Для переноса в Codex нужен экспорт Claude. Для экспорта ChatGPT выберите общую память."))
                    return p
                except Exception as e:
                    message(self.t, import_problem(e), title=T("Check your export", "Проверьте экспорт"))
                    continue
            message(self.t, [(T(f"Not found: {p}", f"Не найдено: {p}"), RED),
                             (T("Copy the full path from your file manager and try again.",
                                "Скопируйте полный путь из файлового менеджера и попробуйте ещё раз."), "")], title=title)

    def import_memory(self) -> None:
        from .importers import import_export
        from .projects import assign_projects
        p = self.ask_export(T("Add chats to shared memory", "Добавить чаты в общую память"))
        if p is None:
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
            from .export_help import import_problem
            message(self.t, import_problem(e), title=T("Import failed", "Импорт не удался"))
            return
        self._titles.clear()
        self._session_info.clear()
        message(self.t, [(T(f"Chats: {res['conversations']}", f"Чатов: {res['conversations']}"), ""),
                         (T(f"New or changed messages: {res['written']}", f"Новых или изменённых сообщений: {res['written']}"), GREEN),
                         (T(f"Project links: {tagged}", f"Привязок к проектам: {tagged}"), DIM), ("", ""),
                         (T("They are now in Sessions and Search.", "Теперь они есть в «Сессиях» и «Поиске»."), "")],
                title=T("Import finished", "Импорт завершён"))

    def import_codex(self) -> None:
        from . import codex_bridge
        from .environment import detect_codex
        title = T("Import into Codex history", "Импорт в историю Codex")
        source = self.ask_export(title, claude_only=True)
        if source is None:
            return
        binary = detect_codex()
        if binary is None:
            path = ask_text(self.t, T("Codex was not found. Path to its executable:", "Codex не найден. Путь к исполняемому файлу:"),
                            title=title, note=[(T("Install Codex or open its desktop app once, then try again.",
                                                                "Установите Codex или один раз откройте его приложение, затем повторите попытку."), "")])
            if not path:
                return
            binary = expand(path.strip().strip('"').strip("'"))
            if not binary.is_file():
                message(self.t, [(T(f"Not found: {binary}", f"Не найдено: {binary}"), RED)], title=title)
                return
        archive_root = self.ask_import_folder()
        if archive_root is None:
            return
        codex_home = expand(os.environ.get("CODEX_HOME") or "~/.codex")
        manifest_path = None
        try:
            self.busy(T("Reading your export…", "Читаю экспорт…"))
            manifest_path = codex_bridge.prepare_cached(source, archive_root)
            remember_import(self.cfg, manifest_path, archive_root=archive_root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            threads = [t for t in manifest["threads"] if t["messages"]]
            if not threads:
                message(self.t, [(T("No chats with messages were found in this export.",
                                    "В экспорте не найдено чатов с сообщениями."), YELLOW)], title=title)
                return
            summary = [(T("Keep the Codex desktop app closed until the transfer finishes.",
                          "Не открывайте приложение Codex до завершения переноса."), YELLOW),
                       (T(f"Chats: {len(threads)}", f"Чатов: {len(threads)}"), BOLD),
                       (T(f"Messages: {sum(t['messages'] for t in threads)}",
                          f"Сообщений: {sum(t['messages'] for t in threads)}"), ""),
                       (T("New chats appear in Codex history as [Claude] …, without project assignments.",
                          "Новые чаты появятся в истории Codex с названиями [Claude] …, без привязки к проектам."), ""),
                       (T("Existing chats are preserved. Previously transferred chats are reused.",
                          "Существующие чаты сохраняются. Уже перенесённые чаты используются повторно."), DIM),
                       (T("Source files and artifacts are saved here:", "Исходные файлы и артефакты сохранены здесь:"), ""),
                       (str(manifest_path.parent), DIM)]
            confirm = select(self.t, [Item("transfer", T("Transfer chats", "Перенести чаты")),
                                      Item("cancel", T("Cancel", "Отмена"))], title=title,
                             details=lambda it: summary, hint=keys_hint(details=False, search=False))
            if confirm.back or confirm.keys == ["cancel"]:
                return
            self.busy(T("Transferring chats into Codex…", "Переношу чаты в Codex…"))
            codex_bridge.install(manifest_path, binary, codex_home)
            self.busy(T("Checking the transferred messages…", "Проверяю перенесённые сообщения…"))
            report = codex_bridge.verify(manifest_path, binary, codex_home)
        except Exception as e:
            from .export_help import import_problem
            lines = import_problem(e)
            if manifest_path:
                lines += [(T("Your prepared archive is saved here; retrying will reuse it:",
                             "Подготовленный архив сохранён здесь; повторная попытка использует его:"), ""),
                          (str(manifest_path.parent), DIM)]
            message(self.t, lines, title=T("Transfer failed", "Перенос не удался"))
            return
        message(self.t, [(T("The transfer is complete. Open the Codex desktop app again.",
                            "Перенос завершён. Теперь откройте приложение Codex заново."), GREEN),
                         (T(f"Chats: {report['threads']}", f"Чатов: {report['threads']}"), ""),
                         (T(f"Messages verified: {report['messages']}", f"Сообщений проверено: {report['messages']}"), GREEN),
                         (T("Open Codex chat history and search for [Claude].", "Откройте историю чатов Codex и найдите [Claude]."), ""),
                         (T("Original dates are preserved, so older chats may be lower in the list.",
                            "Исходные даты сохранены, поэтому старые чаты могут быть ниже в списке."), DIM),
                         ("", ""), (T("Local archive:", "Локальный архив:"), ""), (str(manifest_path.parent), DIM)],
                title=T("Transfer finished", "Перенос завершён"))

    def ask_import_folder(self) -> Path | None:
        title = T("Save the import archive", "Сохранить архив импорта")
        while True:
            path = ask_text(self.t, T("Where should the local import archive be saved?",
                                      "Где сохранить локальный архив импорта?"),
                            title=title, default=str(self._import_archive_root), note=[
                                (T("Press Enter to use the suggested folder, or Del to enter another path.",
                                   "Нажмите Enter для предложенной папки или Del, чтобы указать другой путь."), ""),
                                (T("Source messages, documents and artifacts will be saved in a separate subfolder for each export.",
                                   "Исходные сообщения, документы и артефакты сохранятся в отдельной подпапке для каждого экспорта."), ""),
                                (T("Codex sessions are stored separately in its own history folder.",
                                   "Сессии Codex хранятся отдельно, в его собственной папке истории."), DIM),
                                (T("Choose a permanent location: this archive is also the chats' working folder.",
                                   "Выберите постоянное место: этот архив также служит рабочей папкой чатов."), DIM)])
            if path is None:
                return None
            root = expand((path.strip().strip('"').strip("'") or str(self._import_archive_root))).resolve()
            if root.exists() and not root.is_dir():
                message(self.t, [(T("This path is a file. Choose a folder or enter a new folder path.",
                                    "По этому пути находится файл. Выберите папку или укажите путь к новой папке."), RED)], title=title)
                continue
            self._import_archive_root = root
            return root

    # -- setup ----------------------------------------------------------------------------------------
    def screen_help(self) -> None:
        from .scheduler_help import current_os, scheduler_guide
        start = current_os()
        while True:
            items = [Item(system, name, T("this OS", "эта ОС") if system == current_os() else "", DIM)
                     for system, name in (("windows", "Windows"), ("macos", "macOS"), ("linux", "Linux"))]
            res = select(self.t, items, title=T("How to find and check the scheduler", "Как найти и проверить планировщик"),
                         crumbs=T("Help", "Помощь"), start=start,
                         details=lambda it: [(T("Choose an OS for step-by-step checks, commands and expected results.",
                                                "Выберите ОС: откроются пошаговая инструкция, команды и ожидаемые результаты."), ""),
                                             (T("These instructions do not change scheduled jobs.",
                                                "Эти инструкции не изменяют задачи планировщика."), DIM)],
                         hint=keys_hint(details=False, search=False))
            if res.back:
                return
            start = res.keys[0]
            view(self.t, scheduler_guide(start), title=T("Check the scheduler: ", "Проверка планировщика: ")
                 + {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[start], crumbs=T("Help", "Помощь"))

    def screen_auto_sync(self) -> None:
        from .scheduler import Scheduler, ScheduleError
        scheduler = Scheduler(self.cfg)
        title = T("Auto-sync", "Автосинхронизация")
        while True:
            try:
                self.busy(T("Checking the scheduler…", "Проверяю планировщик…"))
                state = scheduler.status()
                detail = [
                    (T("Enabled", "Включена") if state.enabled else T("Disabled", "Выключена"), GREEN if state.enabled else DIM),
                    (T(f"Scheduler: {state.backend}", f"Планировщик: {state.backend}"), DIM),
                    (T(f"Interval: {state.interval} minutes", f"Интервал: {state.interval} мин."), DIM),
                    ("", ""),
                    (T("Runs brain sync on the configured sources in the background. GitHub is contacted if configured.",
                       "Запускает brain sync для настроенных источников в фоне. Если настроен GitHub, обращается к нему."), ""),
                    (T("Export ZIPs are imported manually. No AI model calls are made by the scheduler.",
                       "ZIP-файлы экспорта импортируются вручную. Планировщик не вызывает ИИ-модели."), ""),
                    (T("Disable removes current and recognized legacy sync jobs from the OS scheduler. Data and manual sync are kept.",
                       "Отключение удаляет из планировщика текущие и распознанные старые задачи синхронизации. Данные и ручная синхронизация сохраняются."), ""),
                ]
                res = select(self.t, [
                    Item("enable", T("Change interval", "Изменить интервал") if state.enabled else T("Enable…", "Включить…")),
                    Item("disable", T("Disable and remove scheduled jobs", "Выключить и удалить задачи планировщика")),
                    Item("help", T("Help: find and check the scheduler", "Помощь: как найти и проверить планировщик")),
                ], title=title, details=lambda it: detail, hint=keys_hint(details=False, search=False))
                if res.back:
                    return
                if res.keys == ["help"]:
                    self.screen_help()
                    continue
                if res.keys == ["enable"]:
                    value = ask_text(self.t, T("Interval in minutes (1–1440)", "Интервал в минутах (1–1440)"),
                                     title=title, default=str(state.interval), note=detail[3:])
                    if value is None:
                        continue
                    try:
                        interval = int(value)
                    except ValueError:
                        raise ScheduleError(T("Enter a whole number of minutes.", "Введите целое число минут."))
                    scheduler.enable(interval)
                elif res.keys == ["disable"]:
                    scheduler.disable()
                message(self.t, [(T("Schedule updated.", "Расписание обновлено."), GREEN)], title=title)
            except (ScheduleError, OSError, ValueError) as e:
                message(self.t, [(T("Could not read or change the schedule.", "Не удалось прочитать или изменить расписание."), RED),
                                 (str(e), "")], title=title)
                return

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


def run(cfg: dict, term: Term | None = None) -> int:
    try:
        with (term or Term()) as t:
            App(cfg, t).run()
    except KeyboardInterrupt:
        # Leave the context first so Ctrl+C restores the cursor and normal screen.
        return 130
    return 0
