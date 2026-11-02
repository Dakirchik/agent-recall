"""Command line: brain init | doctor | paths | sync | search | show | thread | timeline | recent |
projects | tag | export | stats | handoff | import | integrate | mcp."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import db as dbmod
from . import query
from .config import DEFAULT_CONFIG_PATH, expand, load_config
from .i18n import _, set_lang

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
        sys.exit(_("Database not found: {path}\nRun first: brain sync", path=path))
    return dbmod.connect(path, readonly=True)


def cmd_init(cfg, args):
    import re
    from .environment import detect_obsidian_vaults
    target = expand(args.config or DEFAULT_CONFIG_PATH)
    if target.exists() and not args.force:
        print(_("Config already exists: {path} (overwrite: --force)", path=target))
        return
    from .i18n import lang
    here = Path(__file__).resolve().parent
    example = here / ("config.example.ru.toml" if lang() == "ru" else "config.example.toml")
    text = example.read_text(encoding="utf-8") if example.exists() else ''
    vaults = [Path(v) for v in (args.vault or [])]
    if not vaults and not args.no_detect:
        vaults = detect_obsidian_vaults()
    if vaults:
        listed = ", ".join(f'"{v.as_posix()}"' for v in vaults)
        text = re.sub(r"(?m)^vaults = \[.*\]$", f"vaults = [{listed}]", text, count=1)
    else:
        text = re.sub(r"(?m)^vaults = \[.*\]$", "vaults = []", text, count=1)
        text = re.sub(r"(?ms)^(\[obsidian\]\s*\nenabled = )true", r"\1false", text, count=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(_("Config created: {path}", path=target))
    if vaults:
        print(_("Obsidian vaults:") + "".join(f"\n  {v}" for v in vaults))
    else:
        print(_("No Obsidian vaults found — the source is disabled. Set a path in the config or run: brain init --force --vault <path>"))
    print(_("Projects are configured in the [[projects]] section of the same file."))


def cmd_doctor(cfg, args):
    from . import integrations
    from .environment import count_files, detect_obsidian_vaults, github_auth_source, source_paths
    ok = lambda b: "OK " if b else "-- "
    print(f"{_('Config'):9}{ok(cfg['_config_found'])}{cfg['_config_path']}")
    dbp = expand(cfg["db_path"])
    print(f"{_('Database'):9}{ok(dbp.exists())}{dbp}" + (f"  ({dbp.stat().st_size // 1024} KB)" if dbp.exists() else ""))
    print("\n" + _("Sources:"))
    paths = source_paths(cfg)
    on, off = _("on "), _("off")
    for name, pattern in (("claude_code", "*.jsonl"), ("codex", "rollout-*.jsonl"), ("obsidian", "*.md")):
        enabled = cfg[name].get("enabled", False)
        for p in paths[name] or [None]:
            if p is None:
                print(f"  {name:12} {on if enabled else off}  " + _("path not set"))
                continue
            print(f"  {name:12} {on if enabled else off}  {ok(p.exists())}{p}  " + _("files: {n}", n=count_files(p, pattern)))
    gh = github_auth_source(cfg)
    print(f"  {'github':12} {on if cfg['github'].get('enabled') else off}  {ok(bool(gh))}"
          + _("token: {src}", src=gh or _("not found (setx GITHUB_TOKEN ... or gh auth login)")))
    found = [v for v in detect_obsidian_vaults() if v not in paths["obsidian"]]
    if found:
        print("\n" + _("Obsidian vaults found that are not in the config:") + "".join(f"\n  {v}" for v in found))
    print("\n" + _("Agent connections (MCP):"))
    for agent, fn in integrations.STATUS.items():
        try:
            print(f"  {agent:8} {fn()}")
        except Exception as e:
            print(f"  {agent:8} " + _("error: {e}", e=e))
    if dbp.exists():
        conn = dbmod.connect(dbp, readonly=True)
        s = query.stats(conn)
        if s["last_sync"]:
            print("\n" + _("Last sync: {ts} UTC", ts=max(r["ts"] for r in s["last_sync"])[:16].replace("T", " ")))
        for e in s["recent_errors"][:3]:
            print(f"  ! {e['ts'][:16]} {e['source']}: {e['error']}")


def cmd_paths(cfg, args):
    from .environment import source_paths
    db = expand(cfg["db_path"])
    att = expand(cfg["obsidian"].get("attachments_dir") or (db.parent / "attachments"))
    src = source_paths(cfg)
    not_set = _("not configured")
    rows = [(_("Config"), cfg["_config_path"]), (_("Database"), db), (_("Log"), db.with_name("brain.log")),
            (_("Attachments"), att)]
    rows += [("Claude Code", p) for p in src["claude_code"]]
    rows += [("Codex", p) for p in src["codex"]]
    rows += [("Obsidian", p) for p in src["obsidian"]] or [("Obsidian", not_set)]
    exp = cfg.get("export", {})
    rows += [(_("Copy after sync"), expand(d)) for d in exp.get("after_sync") or []]
    if exp.get("daily_snapshots_dir"):
        rows.append((_("Snapshots"), expand(exp["daily_snapshots_dir"])))
    rows.append((_("Program"), sys.prefix))
    if args.json:
        print(json.dumps([{"name": n, "path": str(p) if p == not_set else str(expand(p)),
                           "exists": Path(str(p)).exists()} for n, p in rows], ensure_ascii=False, indent=2))
        return
    for name, p in rows:
        mark = "" if p == not_set or Path(str(p)).exists() else "   " + _("(missing)")
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
            print(f"{a}: " + _("error: {e}", e=e))
            rc = 1
    return rc


def cmd_sync(cfg, args):
    from .sync import run_sync
    if not cfg["_config_found"]:
        print(_("Config not found ({path}), using defaults. Create one: brain init",
                path=cfg["_config_path"]), file=sys.stderr)
    res = run_sync(cfg, only=args.only, verbose=args.verbose)
    if res.get("skipped"):
        print(_("Another sync is still running — skipped."))
        return 0
    for name, r in res["sources"].items():
        status = _("error: {e}", e=r["error"]) if r["error"] else _("written {n}", n=r["written"])
        print(f"  {name:12} {status}")
    print(_("Project links: {n}", n=res.get("tagged", 0)))
    return 1 if any(r["error"] for r in res["sources"].values()) else 0


def cmd_search(cfg, args):
    conn = _open_ro(cfg)
    rows = query.search(conn, " ".join(args.query), source=args.source, project=args.project,
                        since=args.since, until=args.until, kind=args.kind, limit=args.limit,
                        include_tool_output=args.tools)
    if _out(rows, args.json):
        return
    if not rows:
        print(_("Nothing found."))
    for r in rows:
        where = r["repo"] or r["cwd"] or r["path"] or ""
        print(f"[{_short(r['created_at'])}] {r['source']}/{r['kind']}  {r['title'] or ''}")
        print(f"    {r['snippet'].replace(chr(10), ' ')}")
        print(f"    id: {r['id']}" + (f"   ({where})" if where else ""))


def cmd_show(cfg, args):
    conn = _open_ro(cfg)
    item = query.get_item(conn, args.id)
    if not item:
        sys.exit(_("Item not found"))
    if _out(item, args.json):
        return
    for k in ("id", "source", "kind", "title", "author", "created_at", "url", "path", "cwd", "repo", "projects"):
        if item.get(k):
            print(f"{k:11}: {item[k]}")
    try:
        meta = json.loads(item.get("meta") or "{}")
    except json.JSONDecodeError:
        meta = {}
    for k, label in (("original", _("file")), ("copy", _("copy"))):
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
        print(_("No items for this project. Check the id (brain projects) and the rules in the config."))
    day = None
    for r in rows:
        if r["day"] != day:
            day = r["day"]
            print(f"\n{day}")
        count = " " + _("({n} msgs)", n=r["n"]) if r["source"] in ("claude_code", "codex", "claude_app", "chatgpt") and r["n"] > 1 else ""
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
        print(_("Rules re-applied, links: {n}", n=n))
    conn = _open_ro(cfg)
    rows = query.list_projects(conn)
    if _out(rows, args.json):
        return
    for r in rows:
        print(f"{r['id']:20} {r['name']:30} " + _("items") + f" {r['items']:6}  "
              f"{_short(r['first_at'])[:10]} — {_short(r['last_at'])[:10]}")


def cmd_tag(cfg, args):
    from .projects import tag
    conn = dbmod.connect(expand(cfg["db_path"]))
    tag(conn, args.id, args.project, remove=args.remove)
    print(_("Done."))


def cmd_export(cfg, args):
    from .export import backup_db, export_jsonl
    conn = dbmod.connect(expand(cfg["db_path"]))
    if args.format == "db":
        print(_("Database copy: {path}", path=backup_db(conn, args.dest)))
    else:
        path, n = export_jsonl(conn, args.dest, source=args.source, since=args.since,
                               project=args.project, include_tool_output=args.tools)
        print(_("Exported {n} items to {path}", n=n, path=path))


def cmd_stats(cfg, args):
    conn = _open_ro(cfg)
    s = query.stats(conn)
    if _out(s, args.json):
        return
    print(_("Database: {path}", path=expand(cfg["db_path"])))
    for r in s["by_source"]:
        print(f"  {r['source']:12} {r['items']:8}  {_short(r['first_at'])[:10]} — {_short(r['last_at'])[:10]}")
    for r in s["last_sync"]:
        print("  " + _("last sync") + f" {r['source']:12} {_short(r['ts'])} UTC")
    for e in s["recent_errors"]:
        print(f"  ! {_short(e['ts'])} {e['source']}: {e['error']}")


def cmd_handoff(cfg, args):
    from . import handoff as ho
    conn = _open_ro(cfg)
    try:
        if args.project:
            text = ho.project_handoff(conn, args.project, days=args.days, max_chars=args.max_chars)
            what = _("project {p}", p=args.project)
        else:
            if args.last is not None or not args.target:
                tid = ho.last_thread(conn, args.last or None)
                how = _("latest session")
            else:
                q = " ".join(args.target)
                if args.list:
                    found = ho.find_threads(conn, q, limit=10)
                    if not found:
                        sys.exit(_("No sessions contain all the words “{q}”.", q=q))
                    for f in found:
                        print(f"{(f['created_at'] or '')[:10]}  {ho.source_name(f['source']):18} "
                              f"{f['title']}\n            brain handoff {f['thread_id']} -c")
                    return
                tid, how = ho.resolve_thread(conn, q)
                if how == "search":
                    others = [f for f in ho.find_threads(conn, q, limit=4) if f["thread_id"] != tid]
                    if others:
                        print(_("Several sessions match; took the best one. Others: brain handoff {q} --list", q=q),
                              file=sys.stderr)
            text = ho.thread_handoff(conn, tid, max_chars=args.max_chars)
            src = conn.execute("SELECT source FROM items WHERE thread_id = ? LIMIT 1", (tid,)).fetchone()[0]
            what = f"{ho.source_name(src)}, “{ho.thread_title(conn, tid)}” ({ho.how_found(how, args.target)})"
    except ho.ThreadNotFound as e:
        sys.exit(str(e))
    if args.output:
        out = expand(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(_("Saved: {path} — {what}, {n} chars", path=out, what=what, n=len(text)), file=sys.stderr)
    if args.copy:
        if ho.copy_to_clipboard(text):
            print(_("Copied to clipboard: {what}, {n} chars. Paste it into any AI.", what=what, n=len(text)), file=sys.stderr)
        else:
            print(_("Could not copy to the clipboard — printing instead."), file=sys.stderr)
            print(text)
    elif not args.output:
        print(_("Session: {what}", what=what), file=sys.stderr)
        print(text)


def cmd_import(cfg, args):
    from .importers import import_export
    from .projects import assign_projects
    import time
    path = expand(args.path)
    if not path.exists():
        sys.exit(_("File not found: {path}", path=path))
    conn = dbmod.connect(expand(cfg["db_path"]))
    t0 = time.time()
    res = import_export(conn, path, fmt=args.format)
    src = {"claude": "claude_app", "chatgpt": "chatgpt"}[res["format"]]
    dbmod.log_sync(conn, src, res["written"], time.time() - t0, None)
    tagged = assign_projects(conn, cfg)
    conn.commit()
    name = {"claude": _("Claude app"), "chatgpt": "ChatGPT"}[res["format"]]
    print(_("Import ({name}): chats {c}, items {i}, new or changed {w}, project links {t}",
            name=name, c=res["conversations"], i=res["items"], w=res["written"], t=tagged))


def cmd_ui(cfg, args):
    from . import app, tui
    if not tui.supported():
        sys.exit(_("This console cannot show the full-screen UI. Use the commands: brain --help"))
    return app.run(cfg)


def interactive_terminal() -> bool:
    import os
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def cmd_mcp(cfg, args):
    from .mcp_server import serve
    serve(cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain", description=_("agent-recall: shared memory for AI agents — sync, search, handoff"))
    p.add_argument("--config", help=_("path to config.toml (default {path})", path=DEFAULT_CONFIG_PATH))
    sub = p.add_subparsers(dest="cmd")

    def filters(sp, project=True, dates=True):
        sp.add_argument("--source", choices=SOURCES)
        if project:
            sp.add_argument("--project")
        if dates:
            sp.add_argument("--since", help="YYYY-MM-DD")
            sp.add_argument("--until", help="YYYY-MM-DD")

    sp = sub.add_parser("init", help=_("create config.toml (Obsidian vaults are detected automatically)"))
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--vault", action="append", help=_("path to an Obsidian vault (repeatable)"))
    sp.add_argument("--no-detect", action="store_true", help=_("do not detect vaults automatically"))
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("doctor", help=_("check paths, tokens and agent connections"))
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("paths", help=_("show all paths: config, database, log, Claude Code, Codex, Obsidian"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_paths)

    sp = sub.add_parser("integrate", help=_("register/unregister MCP in Claude Code, Codex, Cline"))
    sp.add_argument("action", choices=["add", "remove", "status"])
    sp.add_argument("agent", nargs="?", default="all", choices=["all", "claude", "codex", "cline"])
    sp.add_argument("--python", help=_("python executable the agent will run the server with (default: current)"))
    sp.set_defaults(fn=cmd_integrate)

    sp = sub.add_parser("sync", help=_("pull new data from all sources"))
    sp.add_argument("--only", nargs="+", choices=["obsidian", "claude_code", "codex", "github"])
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("search", help=_("full-text search"))
    sp.add_argument("query", nargs="+")
    filters(sp)
    sp.add_argument("--kind")
    sp.add_argument("--tools", action="store_true", help=_("also search agent tool output"))
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("show", help=_("show an item in full"))
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("thread", help=_("a whole session / thread"))
    sp.add_argument("thread_id")
    sp.add_argument("--tools", action="store_true")
    sp.add_argument("-n", "--limit", type=int, default=500)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_thread)

    sp = sub.add_parser("timeline", help=_("project history by day"))
    sp.add_argument("project")
    sp.add_argument("--since"); sp.add_argument("--until")
    sp.add_argument("-n", "--limit", type=int, default=500)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_timeline)

    sp = sub.add_parser("recent", help=_("latest items"))
    filters(sp, dates=False)
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_recent)

    sp = sub.add_parser("projects", help=_("list projects"))
    sp.add_argument("--rebuild", action="store_true", help=_("re-apply rules after editing the config"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_projects)

    sp = sub.add_parser("tag", help=_("manually link an item to a project"))
    sp.add_argument("id"); sp.add_argument("project")
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(fn=cmd_tag)

    sp = sub.add_parser("export", help=_("export the database"))
    sp.add_argument("format", choices=["db", "jsonl"])
    sp.add_argument("dest")
    filters(sp, dates=False)
    sp.add_argument("--since")
    sp.add_argument("--tools", action="store_true")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("stats", help=_("what is in the database and when it last synced"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("handoff", help=_("briefing of a session or project to continue in another AI"))
    sp.add_argument("target", nargs="*", help=_("session id, item id, or words to find the session"))
    sp.add_argument("--last", nargs="?", const="", choices=["", "claude_code", "codex", "claude_app", "chatgpt"],
                    help=_("latest session (optionally of a given source)"))
    sp.add_argument("--project", help=_("briefing for a project instead of one session"))
    sp.add_argument("--days", type=int, default=14, help=_("with --project: how many days of sessions to include"))
    sp.add_argument("--max-chars", type=int, default=14000, help=_("size limit (default 14000)"))
    sp.add_argument("-c", "--copy", action="store_true", help=_("copy to the clipboard"))
    sp.add_argument("-o", "--output", help=_("save to a .md file"))
    sp.add_argument("--list", action="store_true", help=_("only list the sessions that match the words"))
    sp.set_defaults(fn=cmd_handoff)

    sp = sub.add_parser("import", help=_("import a chat export from the Claude app or ChatGPT"))
    sp.add_argument("format", choices=["claude", "chatgpt", "auto"], help=_("whose export (auto — detect)"))
    sp.add_argument("path", help=_("export zip, unpacked folder or conversations.json"))
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("ui", help=_("full-screen UI (also: brain with no arguments)"))
    sp.set_defaults(fn=cmd_ui)

    sp = sub.add_parser("mcp", help=_("run the MCP server (stdio) for Claude Code / Codex / Cline"))
    sp.set_defaults(fn=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows console: print non-ASCII correctly
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # The language must be known before the parser builds its help texts, so peek at --config.
    argv = sys.argv[1:] if argv is None else argv
    cfg_path = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--config"), None)
    try:
        set_lang(config_value=load_config(cfg_path).get("language"))
    except Exception:
        set_lang()
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if not args.cmd:
        from . import tui
        if interactive_terminal() and tui.supported():
            return cmd_ui(cfg, args)
        parser.print_help()
        return 0
    try:
        rc = args.fn(cfg, args)
    except BrokenPipeError:  # output piped into `head`, `Select-Object -First` etc.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
