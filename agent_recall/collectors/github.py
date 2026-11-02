"""GitHub: your commits, issues/PRs and comments in your repositories (REST API, stdlib only)."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from ..db import get_state, now_iso, set_state, upsert_items
from ..i18n import _

API = "https://api.github.com"


def _token(gcfg: dict) -> str | None:
    tok = os.environ.get(gcfg.get("token_env") or "GITHUB_TOKEN")
    if tok:
        return tok.strip()
    gh = shutil.which("gh")
    if gh:
        try:
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class GitHub:
    def __init__(self, token: str, max_pages: int):
        self.token, self.max_pages = token, max_pages

    def _get(self, url: str):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-recall",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            link = r.headers.get("Link", "")
            return json.loads(r.read().decode("utf-8")), link

    def get(self, path: str):
        return self._get(API + path)[0]

    def paged(self, path: str, params: dict):
        url = f"{API}{path}?{urllib.parse.urlencode({**params, 'per_page': 100})}"
        for _page in range(self.max_pages):
            try:
                data, link = self._get(url)
            except urllib.error.HTTPError as e:
                if e.code in (404, 409):  # repo not found / empty repo
                    return
                raise
            yield from data
            m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            if not m:
                return
            url = m.group(1)


def sync(conn, cfg: dict, log) -> int:
    gcfg = cfg["github"]
    token = _token(gcfg)
    if not token:
        log.warning(_("github: no token (variable %s or `gh auth login`) — skipping"),
                    gcfg.get("token_env"))
        return 0
    gh = GitHub(token, int(gcfg.get("max_pages", 30)))
    me = gh.get("/user")["login"]
    patterns = gcfg.get("repos") or []
    default_since = gcfg.get("since") or "2025-01-01"
    if len(default_since) == 10:
        default_since += "T00:00:00Z"

    repos = []
    for r in gh.paged("/user/repos", {"affiliation": "owner,collaborator", "sort": "pushed"}):
        if r.get("fork") and not gcfg.get("include_forks"):
            continue
        if patterns and not any(fnmatch.fnmatch(r["full_name"].lower(), p.lower()) for p in patterns):
            continue
        repos.append(r)

    written = 0
    for r in repos:
        full = r["full_name"]
        key = f"github:{full}:since"
        since = get_state(conn, key) or default_since
        started = now_iso()
        items: list[dict] = []
        try:
            for c in gh.paged(f"/repos/{full}/commits", {"since": since, "author": me}):
                msg = c["commit"]["message"]
                items.append({
                    "id": f"github:commit:{full}:{c['sha']}",
                    "source": "github", "kind": "commit", "thread_id": f"github:{full}",
                    "title": msg.splitlines()[0][:200], "body": msg,
                    "author": (c.get("author") or {}).get("login") or c["commit"]["author"].get("name"),
                    "url": c.get("html_url"), "repo": full,
                    "created_at": c["commit"]["author"].get("date"),
                    "meta": {"sha": c["sha"]},
                })
            for i in gh.paged(f"/repos/{full}/issues", {"state": "all", "since": since}):
                kind = "pull_request" if "pull_request" in i else "issue"
                items.append({
                    "id": f"github:{kind}:{full}#{i['number']}",
                    "source": "github", "kind": kind, "thread_id": f"github:{full}#{i['number']}",
                    "title": f"#{i['number']} {i['title']}", "body": i.get("body") or "",
                    "author": (i.get("user") or {}).get("login"), "url": i.get("html_url"), "repo": full,
                    "created_at": i.get("created_at"), "updated_at": i.get("updated_at"),
                    "meta": {"state": i.get("state"), "labels": [l["name"] for l in i.get("labels", [])],
                             "closed_at": i.get("closed_at")},
                })
            for c in gh.paged(f"/repos/{full}/issues/comments", {"since": since}):
                num = (c.get("issue_url") or "").rsplit("/", 1)[-1]
                items.append({
                    "id": f"github:comment:{full}:{c['id']}",
                    "source": "github", "kind": "comment", "thread_id": f"github:{full}#{num}",
                    "title": f"#{num} " + _("comment"), "body": c.get("body") or "",
                    "author": (c.get("user") or {}).get("login"), "url": c.get("html_url"), "repo": full,
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
        except urllib.error.HTTPError as e:
            log.warning(_("github: %s — API error %s, will retry next time"), full, e.code)
            continue
        written += upsert_items(conn, items)
        set_state(conn, key, started)
        conn.commit()
        if items:
            log.info(_("github: %s — fetched %d"), full, len(items))
    log.info(_("github: repositories %d, written %d"), len(repos), written)
    return written
