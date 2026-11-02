"""Command line: brain sync | search | show | thread | timeline | recent | projects | tag | export | stats | mcp."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import db as dbmod
from . import query
from .config import DEFAULT_CONFIG_PATH, expand, load_config

SOURCES = ["obsidian", "claude_code", "codex", "github", "claude_app", "chatgpt"]


def _out(data, as_json: bool) -> bool:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return True
    return False


def _short(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def _open_ro(cfg):
    path = expand(cfg["db_path"])
    if not path.exists():
        sys.exit(f"База не найдена: {path}\nСначала запустите: brain sync")
    return dbmod.connect(path, readonly=True)


def cmd_init(cfg, args):
    import re
    from .environment import detect_obsidian_vaults
    target = expand(args.config or DEFAULT_CONFIG_PATH)
    if target.exists() and not args.force:
        print(f"Конфиг уже есть: {target} (перезаписать: --force)")
        return
    example = Path(__file__).resolve().parent / "config.example.toml"
    text = example.read_text(encoding="utf-8") if example.exists() else ''
    vaults = [Path(v) for v in (args.vault or [])]
    if not vaults and not args.no_detect:
        vaults = detect_obsidian_vaults()
    if vaults:
        listed = ", ".join(f'"{v.as_posix()}"' for v in vaults)
        text = re.sub(r"(?m)^vaults = \[.*\]$", f"vaults = [{listed}]", text, count=1)
    else:
        text = re.sub(r"(?ms)^(\[obsidian\]\s*\nenabled = )true", r"\1false", text, count=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"Создан конфиг: {target}")
    if vaults:
        print("Хранилища Obsidian:" + "".join(f"\n  {v}" for v in vaults))
    else:
        print("Хранилища Obsidian не найдены — источник выключен. Укажите путь в конфиге или: brain init --force --vault <путь>")
    print("Проекты настраиваются в разделе [[projects]] того же файла.")


def cmd_doctor(cfg, args):
    from . import integrations
    from .environment import count_files, detect_obsidian_vaults, github_auth_source, source_paths
    ok = lambda b: "OK " if b else "-- "
    print(f"Конфиг:  {ok(cfg['_config_found'])}{cfg['_config_path']}")
    dbp = expand(cfg["db_path"])
    print(f"База:    {ok(dbp.exists())}{dbp}" + (f"  ({dbp.stat().st_size // 1024} КБ)" if dbp.exists() else ""))
    print("\nИсточники:")
    paths = source_paths(cfg)
    for name, pattern in (("claude_code", "*.jsonl"), ("codex", "rollout-*.jsonl"), ("obsidian", "*.md")):
        enabled = cfg[name].get("enabled", False)
        for p in paths[name] or [None]:
            if p is None:
                print(f"  {name:12} {'вкл ' if enabled else 'выкл'}  путь не задан")
                continue
            print(f"  {name:12} {'вкл ' if enabled else 'выкл'}  {ok(p.exists())}{p}  файлов: {count_files(p, pattern)}")
    gh = github_auth_source(cfg)
    print(f"  {'github':12} {'вкл ' if cfg['github'].get('enabled') else 'выкл'}  "
          f"{ok(bool(gh))}токен: {gh or 'не найден (setx GITHUB_TOKEN ... или gh auth login)'}")
    found = [v for v in detect_obsidian_vaults() if v not in paths["obsidian"]]
    if found:
        print("\nНайдены хранилища Obsidian, которых нет в конфиге:" + "".join(f"\n  {v}" for v in found))
    print("\nПодключение к агентам (MCP):")
    for agent, fn in integrations.STATUS.items():
        try:
            print(f"  {agent:8} {fn()}")
        except Exception as e:
            print(f"  {agent:8} ошибка: {e}")
    if dbp.exists():
        conn = dbmod.connect(dbp, readonly=True)
        s = query.stats(conn)
        if s["last_sync"]:
            print("\nПоследняя синхронизация: " + max(r["ts"] for r in s["last_sync"])[:16].replace("T", " ") + " UTC")
        for e in s["recent_errors"][:3]:
            print(f"  ! {e['ts'][:16]} {e['source']}: {e['error']}")


def cmd_paths(cfg, args):
    from .environment import source_paths
    db = expand(cfg["db_path"])
    att = expand(cfg["obsidian"].get("attachments_dir") or (db.parent / "attachments"))
    src = source_paths(cfg)
    rows = [("Конфиг", cfg["_config_path"]), ("База", db), ("Лог", db.with_name("brain.log")),
            ("Вложения", att)]
    rows += [("Claude Code", p) for p in src["claude_code"]]
    rows += [("Codex", p) for p in src["codex"]]
    rows += [("Obsidian", p) for p in src["obsidian"]] or [("Obsidian", "не настроен")]
    exp = cfg.get("export", {})
    rows += [("Копия после синхр.", expand(d)) for d in exp.get("after_sync") or []]
    if exp.get("daily_snapshots_dir"):
        rows.append(("Снимки", expand(exp["daily_snapshots_dir"])))
    rows.append(("Программа", sys.prefix))
    if args.json:
        print(json.dumps([{"name": n, "path": str(expand(p)) if isinstance(p, (str, Path)) and "не " not in str(p) else str(p),
                           "exists": Path(str(p)).exists()} for n, p in rows], ensure_ascii=False, indent=2))
        return
    for name, p in rows:
        mark = "" if Path(str(p)).exists() or str(p) == "не настроен" else "   (нет)"
        print(f"{name:19} {p}{mark}")


def cmd_integrate(cfg, args):
    from . import integrations
    agents = list(integrations.AGENTS) if args.agent == "all" else [args.agent]
    rc = 0
    for a in agents:
        try:
            if args.action == "add":
                print(integrations.ADD[a](args.python))
            elif args.action == "remove":
                print(integrations.REMOVE[a]())
            else:
                print(f"{a:8} {integrations.STATUS[a]()}")
        except Exception as e:
            print(f"{a}: ошибка — {e}")
            rc = 1
    return rc


def cmd_sync(cfg, args):
    from .sync import run_sync
    if not cfg["_config_found"]:
        print(f"Конфиг не найден ({cfg['_config_path']}), работаю с настройками по умолчанию. "
              f"Создать: brain init", file=sys.stderr)
    res = run_sync(cfg, only=args.only, verbose=args.verbose)
    if res.get("skipped"):
        print("Другая синхронизация ещё идёт — пропуск.")
        return 0
    for name, r in res["sources"].items():
        status = f"ошибка: {r['error']}" if r["error"] else f"записано {r['written']}"
        print(f"  {name:12} {status}")
    print(f"Привязок к проектам: {res.get('tagged', 0)}")
    return 1 if any(r["error"] for r in res["sources"].values()) else 0


def cmd_search(cfg, args):
    conn = _open_ro(cfg)
    rows = query.search(conn, " ".join(args.query), source=args.source, project=args.project,
                        since=args.since, until=args.until, kind=args.kind, limit=args.limit,
                        include_tool_output=args.tools)
    if _out(rows, args.json):
        return
    if not rows:
        print("Ничего не найдено.")
    for r in rows:
        where = r["repo"] or r["cwd"] or r["path"] or ""
        print(f"[{_short(r['created_at'])}] {r['source']}/{r['kind']}  {r['title'] or ''}")
        print(f"    {r['snippet'].replace(chr(10), ' ')}")
        print(f"    id: {r['id']}" + (f"   ({where})" if where else ""))


def cmd_show(cfg, args):
    conn = _open_ro(cfg)
    item = query.get_item(conn, args.id)
    if not item:
        sys.exit("Запись не найдена")
    if _out(item, args.json):
        return
    for k in ("id", "source", "kind", "title", "author", "created_at", "url", "path", "cwd", "repo", "projects"):
        if item.get(k):
            print(f"{k:11}: {item[k]}")
    try:
        meta = json.loads(item.get("meta") or "{}")
    except json.JSONDecodeError:
        meta = {}
    for k, label in (("original", "файл"), ("copy", "копия")):
        if meta.get(k):
            print(f"{label:11}: {meta[k]}")
    print("-" * 60)
    print(item.get("body") or "")


def cmd_thread(cfg, args):
    conn = _open_ro(cfg)
    rows = query.get_thread(conn, args.thread_id, limit=args.limit, include_tool_output=args.tools)
    if _out(rows, args.json):
        return
    for r in rows:
        print(f"--- {r['author'] or r['kind']} · {_short(r['created_at'])}")
        print(r["body"])


def cmd_timeline(cfg, args):
    conn = _open_ro(cfg)
    rows = query.timeline(conn, args.project, since=args.since, until=args.until, limit=args.limit)
    if _out(rows, args.json):
        return
    if not rows:
        print("Для проекта нет записей. Проверьте id (brain projects) и правила в конфиге.")
    day = None
    for r in rows:
        if r["day"] != day:
            day = r["day"]
            print(f"\n{day}")
        count = f" ({r['n']} сообщ.)" if r["source"] in ("claude_code", "codex") and r["n"] > 1 else ""
        print(f"  {r['source']:11} {r['kinds']:14} {r['title'] or ''}{count}")


def cmd_recent(cfg, args):
    conn = _open_ro(cfg)
    rows = query.recent(conn, source=args.source, project=args.project, limit=args.limit)
    if _out(rows, args.json):
        return
    for r in rows:
        print(f"[{_short(r['created_at'])}] {r['source']}/{r['kind']}  {r['title'] or ''}")


def cmd_projects(cfg, args):
    if args.rebuild:
        from .projects import assign_projects
        conn = dbmod.connect(expand(cfg["db_path"]))
        n = assign_projects(conn, cfg, rebuild=True)
        print(f"Правила пересчитаны, привязок: {n}")
    conn = _open_ro(cfg)
    rows = query.list_projects(conn)
    if _out(rows, args.json):
        return
    for r in rows:
        print(f"{r['id']:20} {r['name']:30} записей {r['items']:6}  "
              f"{_short(r['first_at'])[:10]} — {_short(r['last_at'])[:10]}")


def cmd_tag(cfg, args):
    from .projects import tag
    conn = dbmod.connect(expand(cfg["db_path"]))
    tag(conn, args.id, args.project, remove=args.remove)
    print("Готово.")


def cmd_export(cfg, args):
    from .export import backup_db, export_jsonl
    conn = dbmod.connect(expand(cfg["db_path"]))
    if args.format == "db":
        print(f"Копия базы: {backup_db(conn, args.dest)}")
    else:
        path, n = export_jsonl(conn, args.dest, source=args.source, since=args.since,
                               project=args.project, include_tool_output=args.tools)
        print(f"Выгружено {n} записей в {path}")


def cmd_stats(cfg, args):
    conn = _open_ro(cfg)
    s = query.stats(conn)
    if _out(s, args.json):
        return
    print(f"База: {expand(cfg['db_path'])}")
    for r in s["by_source"]:
        print(f"  {r['source']:12} {r['items']:8}  {_short(r['first_at'])[:10]} — {_short(r['last_at'])[:10]}")
    for r in s["last_sync"]:
        print(f"  последняя синхронизация {r['source']:12} {_short(r['ts'])} UTC")
    for e in s["recent_errors"]:
        print(f"  ! {_short(e['ts'])} {e['source']}: {e['error']}")


def cmd_handoff(cfg, args):
    from . import handoff as ho
    conn = _open_ro(cfg)
    try:
        if args.project:
            text = ho.project_handoff(conn, args.project, days=args.days, max_chars=args.max_chars)
            what = f"проект {args.project}"
        else:
            if args.last is not None or not args.target:
                tid = ho.last_thread(conn, args.last or None)
                how = "последняя сессия"
            else:
                q = " ".join(args.target)
                if args.list:
                    found = ho.find_threads(conn, q, limit=10)
                    if not found:
                        sys.exit(f"Сессий со всеми словами «{q}» нет.")
                    for f in found:
                        print(f"{(f['created_at'] or '')[:10]}  {ho.SOURCE_NAMES.get(f['source'], f['source']):18} "
                              f"{f['title']}\n            brain handoff {f['thread_id']} -c")
                    return
                tid, how = ho.resolve_thread(conn, q)
                if how.startswith("поиском"):
                    others = [f for f in ho.find_threads(conn, q, limit=4) if f["thread_id"] != tid]
                    if others:
                        print("Нашлось несколько сессий, взял самую подходящую. Другие: brain handoff "
                              f"{q} --list", file=sys.stderr)
            text = ho.thread_handoff(conn, tid, max_chars=args.max_chars)
            src = conn.execute("SELECT source FROM items WHERE thread_id = ? LIMIT 1", (tid,)).fetchone()[0]
            what = f"{ho.SOURCE_NAMES.get(src, src)}, «{ho.thread_title(conn, tid)}» ({how})"
    except ho.ThreadNotFound as e:
        sys.exit(str(e))
    if args.output:
        out = expand(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"Сохранено: {out} — {what}, {len(text)} символов", file=sys.stderr)
    if args.copy:
        if ho.copy_to_clipboard(text):
            print(f"Скопировано в буфер обмена: {what}, {len(text)} символов. Вставьте в любой ИИ.", file=sys.stderr)
        else:
            print("Не удалось скопировать в буфер — вывожу текстом.", file=sys.stderr)
            print(text)
    elif not args.output:
        print(f"Сессия: {what}", file=sys.stderr)
        print(text)


def cmd_import(cfg, args):
    from .importers import import_export
    from .projects import assign_projects
    import time
    path = expand(args.path)
    if not path.exists():
        sys.exit(f"Не найден файл: {path}")
    conn = dbmod.connect(expand(cfg["db_path"]))
    t0 = time.time()
    res = import_export(conn, path, fmt=args.format)
    src = {"claude": "claude_app", "chatgpt": "chatgpt"}[res["format"]]
    dbmod.log_sync(conn, src, res["written"], time.time() - t0, None)
    tagged = assign_projects(conn, cfg)
    conn.commit()
    name = {"claude": "приложение Claude", "chatgpt": "ChatGPT"}[res["format"]]
    print(f"Импорт ({name}): чатов {res['conversations']}, записей {res['items']}, "
          f"новых или изменённых {res['written']}, привязок к проектам {tagged}")


def cmd_mcp(cfg, args):
    from .mcp_server import serve
    serve(cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain", description="agent-recall: общая память для ИИ-агентов — синхронизация, поиск, handoff")
    p.add_argument("--config", help=f"путь к config.toml (по умолчанию {DEFAULT_CONFIG_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    def filters(sp, project=True, dates=True):
        sp.add_argument("--source", choices=SOURCES)
        if project:
            sp.add_argument("--project")
        if dates:
            sp.add_argument("--since", help="ГГГГ-ММ-ДД")
            sp.add_argument("--until", help="ГГГГ-ММ-ДД")

    sp = sub.add_parser("init", help="создать config.toml (хранилища Obsidian находятся автоматически)")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--vault", action="append", help="путь к хранилищу Obsidian (можно несколько раз)")
    sp.add_argument("--no-detect", action="store_true", help="не искать хранилища автоматически")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("doctor", help="проверить пути, токены, подключение к агентам")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("paths", help="показать все пути: конфиг, база, лог, Claude Code, Codex, Obsidian")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_paths)

    sp = sub.add_parser("integrate", help="подключить/отключить MCP в Claude Code, Codex, Cline")
    sp.add_argument("action", choices=["add", "remove", "status"])
    sp.add_argument("agent", nargs="?", default="all", choices=["all", "claude", "codex", "cline"])
    sp.add_argument("--python", help="python.exe, которым агент будет запускать сервер (по умолчанию текущий)")
    sp.set_defaults(fn=cmd_integrate)

    sp = sub.add_parser("sync", help="забрать новые данные из всех источников")
    sp.add_argument("--only", nargs="+", choices=["obsidian", "claude_code", "codex", "github"])
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("search", help="полнотекстовый поиск")
    sp.add_argument("query", nargs="+")
    filters(sp)
    sp.add_argument("--kind")
    sp.add_argument("--tools", action="store_true", help="искать и в выводе инструментов агентов")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("show", help="показать запись целиком")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("thread", help="вся сессия / тред")
    sp.add_argument("thread_id")
    sp.add_argument("--tools", action="store_true")
    sp.add_argument("-n", "--limit", type=int, default=500)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_thread)

    sp = sub.add_parser("timeline", help="история проекта по дням")
    sp.add_argument("project")
    sp.add_argument("--since"); sp.add_argument("--until")
    sp.add_argument("-n", "--limit", type=int, default=500)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_timeline)

    sp = sub.add_parser("recent", help="последние записи")
    filters(sp, dates=False)
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_recent)

    sp = sub.add_parser("projects", help="список проектов")
    sp.add_argument("--rebuild", action="store_true", help="пересчитать правила после правки конфига")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_projects)

    sp = sub.add_parser("tag", help="вручную привязать запись к проекту")
    sp.add_argument("id"); sp.add_argument("project")
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(fn=cmd_tag)

    sp = sub.add_parser("export", help="выгрузить базу")
    sp.add_argument("format", choices=["db", "jsonl"])
    sp.add_argument("dest")
    filters(sp, dates=False)
    sp.add_argument("--since")
    sp.add_argument("--tools", action="store_true")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("stats", help="что лежит в базе и когда синхронизировалось")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("handoff", help="выжимка сессии или проекта, чтобы продолжить в другом ИИ")
    sp.add_argument("target", nargs="*", help="id сессии, id записи или слова для поиска сессии")
    sp.add_argument("--last", nargs="?", const="", choices=["", "claude_code", "codex", "claude_app", "chatgpt"],
                    help="последняя сессия (можно указать источник)")
    sp.add_argument("--project", help="выжимка по проекту вместо одной сессии")
    sp.add_argument("--days", type=int, default=14, help="для --project: за сколько дней брать сессии")
    sp.add_argument("--max-chars", type=int, default=14000, help="ограничение размера (по умолчанию 14000)")
    sp.add_argument("-c", "--copy", action="store_true", help="скопировать в буфер обмена")
    sp.add_argument("-o", "--output", help="сохранить в файл .md")
    sp.add_argument("--list", action="store_true", help="только показать, какие сессии подходят под слова")
    sp.set_defaults(fn=cmd_handoff)

    sp = sub.add_parser("import", help="импорт экспорта чатов из приложения Claude или ChatGPT")
    sp.add_argument("format", choices=["claude", "chatgpt", "auto"], help="чей экспорт (auto — определить самому)")
    sp.add_argument("path", help="zip-архив экспорта, распакованная папка или conversations.json")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("mcp", help="запустить MCP-сервер (stdio) для Claude Code / Codex / Cline")
    sp.set_defaults(fn=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows console: print Cyrillic correctly
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    rc = args.fn(cfg, args)
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
