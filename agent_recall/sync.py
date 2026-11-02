"""Run all collectors once: lock, collect, tag projects, export copies."""
from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import db as dbmod
from .collectors import COLLECTORS
from .config import expand
from .export import backup_db, daily_snapshot, mirror_dir
from .projects import assign_projects

LOCK_STALE_SECONDS = 3600


def setup_logging(db_path: Path, verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("agent_recall")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(db_path.with_name("brain.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


class Lock:
    """Prevents two scheduled runs from writing at once."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        try:
            if self.path.exists() and time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                self.path.unlink(missing_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
        except FileExistsError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.path.unlink(missing_ok=True)


def run_sync(cfg: dict, only: list[str] | None = None, verbose: bool = False) -> dict:
    db_path = expand(cfg["db_path"])
    log = setup_logging(db_path, verbose)
    result: dict = {"sources": {}, "skipped": False}
    with Lock(db_path.with_name("brain.lock")) as lock:
        if not lock.acquired:
            log.info("sync: другая синхронизация ещё идёт — пропускаю запуск")
            result["skipped"] = True
            return result
        conn = dbmod.connect(db_path)
        total = 0
        try:
            for name, fn in COLLECTORS.items():
                if only and name not in only:
                    continue
                if not cfg.get(name, {}).get("enabled", False):
                    continue
                t0 = time.time()
                err = None
                n = 0
                try:
                    n = fn(conn, cfg, log)
                except Exception as e:  # a failing source must not block the others
                    conn.rollback()
                    err = f"{type(e).__name__}: {e}"
                    log.exception("%s: ошибка", name)
                dbmod.log_sync(conn, name, n, time.time() - t0, err)
                conn.commit()
                result["sources"][name] = {"written": n, "error": err}
                total += n
            result["tagged"] = assign_projects(conn, cfg, log=log)
            result["written"] = total
            exp = cfg.get("export", {})
            if total or result["tagged"]:
                for dest in exp.get("after_sync", []) or []:
                    try:
                        p = backup_db(conn, dest)
                        log.info("export: копия базы -> %s", p)
                        att = expand(cfg["obsidian"].get("attachments_dir") or (db_path.parent / "attachments"))
                        if att.is_dir():
                            n = mirror_dir(att, p.parent / "attachments")
                            if n:
                                log.info("export: вложений скопировано %d -> %s", n, p.parent / "attachments")
                    except Exception as e:
                        log.warning("export: не удалось скопировать в %s: %s", dest, e)
            if exp.get("daily_snapshots_dir"):
                try:
                    p = daily_snapshot(conn, exp["daily_snapshots_dir"], int(exp.get("keep_daily", 14)))
                    if p:
                        log.info("export: дневной снимок -> %s", p)
                except Exception as e:
                    log.warning("export: снимок не создан: %s", e)
        finally:
            conn.close()
    log.info("sync: готово, записано %d", result.get("written", 0))
    return result
