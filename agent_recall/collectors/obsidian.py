"""Obsidian vault -> items. One item per note; deleted notes are kept and flagged."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from ..config import expand
from ..db import get_state, mark_deleted, set_state, upsert_items
from ..i18n import _

PARSER_VERSION = 3  # bump to force re-reading every note after the parser learns something new

DEFAULT_ATTACHMENT_EXT = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".pdf"]
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic"}
EMBED_WIKI_RE = re.compile(r"!\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
EMBED_MD_RE = re.compile(r"!\[[^\]]*\]\(<?([^)>]+?)>?(?:\s+\"[^\"]*\")?\)")
# "Pasted image 20261101123456.png" — Obsidian puts the paste time into the name
PASTED_TS_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")

TAG_RE = re.compile(r"(?<![\w/#&])#([^\s#\[\](){}.,;:!?\"'`]*[^\W\d][^\s#\[\](){}.,;:!?\"'`]*)")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
H1_RE = re.compile(r"^#\s+(.+)$", re.M)


def _parse_scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [_parse_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    try:  # use PyYAML when available, it handles everything
        import yaml  # type: ignore
        data = yaml.safe_load(raw) or {}
        return (data if isinstance(data, dict) else {}), body
    except ImportError:
        pass
    except Exception:
        return {}, body
    data: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\s+-\s", line) and key:
            data.setdefault(key, [])
            if not isinstance(data[key], list):
                data[key] = [data[key]] if data[key] else []
            data[key].append(_parse_scalar(line.split("-", 1)[1]))
            continue
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            key = key.strip()
            data[key] = _parse_scalar(val) if val.strip() else []
    return data, body


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def _file_created(st: os.stat_result) -> float:
    bt = getattr(st, "st_birthtime", None)  # Windows / macOS
    if bt:
        return bt
    return st.st_mtime


def extract_embeds(body: str, limit: int = 50) -> list[dict]:
    """Images/PDFs embedded in a note, with the surrounding text as their description:
    the nearest heading + the text on the same line (or the line right above)."""
    out: list[dict] = []
    heading, prev = "", ""
    in_code = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("#") and not stripped.startswith("#!"):
            heading, prev = stripped.lstrip("#").strip(), ""
            continue
        targets = EMBED_WIKI_RE.findall(line)
        targets += [urllib.parse.unquote(t) for t in EMBED_MD_RE.findall(line) if "://" not in t]
        if not targets:
            if stripped:
                prev = stripped
            continue
        rest = EMBED_MD_RE.sub(" ", EMBED_WIKI_RE.sub(" ", line)).strip(" -*>:\t")
        text = rest if len(rest) >= 15 else " ".join(x for x in (prev.rstrip(":"), rest) if x)
        context = " — ".join(x for x in (heading, text) if x)[:300]
        for t in targets:
            name = t.strip().replace("\\", "/").rsplit("/", 1)[-1]
            suffix = Path(name).suffix.lower()
            if suffix in DEFAULT_ATTACHMENT_EXT or not suffix:
                out.append({"name": name, "context": context})
        prev = ""  # the text above belonged to this embed, not to the next one
        if len(out) >= limit:
            break
    return out


def _pdf_text(path: Path, limit: int = 20000) -> str:
    try:
        from pypdf import PdfReader  # optional: pip install pypdf
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        parts, total = [], 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= limit:
                break
        return "\n".join(parts)[:limit]
    except Exception:
        return ""


def _attachment_created(path: Path, st: os.stat_result):
    m = PASTED_TS_RE.search(path.stem)
    if m:
        try:
            return datetime(*map(int, m.groups())).astimezone()  # local time of the paste
        except ValueError:
            pass
    return _file_created(st)


def attachment_to_item(vault: Path, vault_name: str, rel: str, path: Path, st: os.stat_result,
                       refs: list[dict], pdf_text: str, copy_path: Path | None) -> dict:
    ext = path.suffix.lower()
    kind = "image" if ext in IMAGE_EXT else ("pdf" if ext == ".pdf" else "attachment")
    label = {"image": _("Image"), "pdf": "PDF"}.get(kind, _("File"))
    auto_named = path.stem.lower().startswith(("pasted image", "screenshot", "снимок экрана", "скриншот", "image"))
    if kind == "image" and auto_named:
        label = _("Screenshot")
    lines = [f"{label}: {path.name}", _("Path in vault: {rel}", rel=rel)]
    if refs:
        lines.append(_("Embedded in notes:"))
        for r in refs:
            lines.append(f"- {r['title']}" + (f" — {r['context']}" if r.get("context") else ""))
    else:
        lines.append(_("Not embedded in any note."))
    if pdf_text:
        lines += ["", pdf_text]
    return {
        "id": f"obsidian:{vault_name}:{rel}",
        "source": "obsidian",
        "kind": kind,
        "thread_id": refs[0]["note_id"] if refs else f"obsidian:{vault_name}:{rel}",
        # an auto-generated name says nothing; then the note it lives in is the better title
        "title": _("{label} in “{note}”", label=label, note=refs[0]["title"]) if (auto_named and refs) else f"{label}: {path.stem}",
        "body": "\n".join(lines),
        "url": f"obsidian://open?vault={vault_name}&file={urllib.parse.quote(rel)}",
        "path": rel,
        "created_at": _attachment_created(path, st),
        "updated_at": st.st_mtime,
        "meta": {
            "vault": vault_name, "size": st.st_size, "ext": ext,
            "notes": [r["note_id"] for r in refs],
            "original": str(path),
            "copy": str(copy_path) if copy_path else None,
            "pdf_text": bool(pdf_text),
        },
    }


def _embed_index(conn, vault_name: str) -> dict[str, list[dict]]:
    """attachment file name (lower case) -> notes that embed it."""
    idx: dict[str, list[dict]] = {}
    rows = conn.execute(
        """SELECT id, title, meta FROM items
           WHERE source = 'obsidian' AND deleted_at IS NULL
             AND kind NOT IN ('image', 'pdf', 'attachment')
             AND json_extract(meta, '$.vault') = ?
             AND json_array_length(json_extract(meta, '$.embeds')) > 0""", (vault_name,))
    for note_id, title, meta in rows:
        for e in json.loads(meta).get("embeds", []):
            idx.setdefault(e["name"].lower(), []).append(
                {"note_id": note_id, "title": title, "context": e.get("context", "")})
    return idx


def note_to_item(vault: Path, vault_name: str, path: Path, st: os.stat_result) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(text)
    rel = path.relative_to(vault).as_posix()
    plain = CODE_RE.sub(" ", body)
    tags = {t.lstrip("#").lower() for t in _as_list(fm.get("tags") or fm.get("tag"))}
    tags |= {t.lower() for t in TAG_RE.findall(plain)}
    links = sorted({l.strip() for l in LINK_RE.findall(body)})
    embeds = extract_embeds(body)
    h1 = H1_RE.search(body)
    title = str(fm.get("title") or (h1.group(1).strip() if h1 else path.stem))

    kind = str(fm.get("type") or fm.get("kind") or "").strip().lower()
    if not kind:
        low = rel.lower()
        if re.search(r"(^|/)(adr|decisions?)(/|-)", low) or re.match(r"adr[-_ ]?\d+", path.stem.lower()):
            kind = "adr"
        elif re.search(r"(^|/)(sessions?|session-logs?|logs?|daily|journal)(/)", low):
            kind = "log"
        else:
            kind = "note"

    created = fm.get("created") or fm.get("date") or _file_created(st)
    if hasattr(created, "isoformat") and not isinstance(created, datetime):  # yaml date
        created = datetime(created.year, created.month, created.day, tzinfo=timezone.utc)
    fm_clean = {k: (v if isinstance(v, (str, int, float, bool, list, dict, type(None))) else str(v))
                for k, v in fm.items()}
    return {
        "id": f"obsidian:{vault_name}:{rel}",
        "source": "obsidian",
        "kind": kind,
        "thread_id": f"obsidian:{vault_name}:{rel}",
        "title": title,
        "body": body.strip(),
        "author": None,
        "url": f"obsidian://open?vault={vault_name}&file={rel}",
        "path": rel,
        "created_at": created,
        "updated_at": st.st_mtime,
        "deleted_at": None,
        "meta": {"vault": vault_name, "tags": sorted(tags), "links": links, "embeds": embeds,
                 "frontmatter": fm_clean,
                 "sha1": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()},
    }


def sync(conn, cfg: dict, log) -> int:
    ocfg = cfg["obsidian"]
    exclude = {e.lower() for e in ocfg.get("exclude", [])}
    with_att = ocfg.get("attachments", True)
    att_ext = {e.lower() if e.startswith(".") else "." + e.lower()
               for e in ocfg.get("attachment_ext", DEFAULT_ATTACHMENT_EXT)}
    copy_att = ocfg.get("copy_attachments", True)
    max_bytes = float(ocfg.get("max_attachment_mb", 25)) * 1024 * 1024
    att_root = expand(ocfg.get("attachments_dir") or (expand(cfg["db_path"]).parent / "attachments"))
    written = 0
    for vault_str in ocfg.get("vaults", []):
        vault = expand(vault_str)
        if not vault.is_dir():
            log.warning(_("obsidian: vault not found: %s"), vault)
            continue
        name = vault.name
        state_key = f"obsidian:{vault.as_posix()}"
        seen: dict[str, list] = get_state(conn, state_key, {}) or {}
        if get_state(conn, f"{state_key}:parser", 1) < PARSER_VERSION:
            seen = {}  # parser got smarter — re-read every note once
        seen_att: dict[str, list] = get_state(conn, f"{state_key}:attachments", {}) or {}
        current: dict[str, list] = {}
        current_att: dict[str, tuple[Path, os.stat_result]] = {}
        batch: list[dict] = []
        for root, dirs, files in os.walk(vault):
            dirs[:] = [d for d in dirs if d.lower() not in exclude and not d.startswith(".")]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                is_note = ext == ".md"
                if not is_note and not (with_att and ext in att_ext):
                    continue
                p = Path(root) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(vault).as_posix()
                if not is_note:
                    current_att[rel] = (p, st)
                    continue
                sig = [st.st_mtime_ns, st.st_size]
                current[rel] = sig
                if seen.get(rel) == sig:
                    continue
                try:
                    batch.append(note_to_item(vault, name, p, st))
                except Exception as e:  # one broken note must not stop the sync
                    log.warning(_("obsidian: could not read %s: %s"), p, e)
                    current.pop(rel, None)
                if len(batch) >= 200:
                    written += upsert_items(conn, batch)
                    batch.clear()
        written += upsert_items(conn, batch)
        gone = [f"obsidian:{name}:{rel}" for rel in seen if rel not in current]
        written += mark_deleted(conn, gone)
        set_state(conn, state_key, current)
        set_state(conn, f"{state_key}:parser", PARSER_VERSION)
        conn.commit()
        n_notes = written

        # Attachments are re-described every run (cheap, no file reads) because the notes
        # embedding them may have changed; unchanged rows are skipped by the hash check.
        att_written = copied = 0
        if with_att:
            index = _embed_index(conn, name)
            new_seen_att: dict[str, list] = {}
            att_batch: list[dict] = []
            for rel, (p, st) in current_att.items():
                sig = [st.st_mtime_ns, st.st_size]
                new_seen_att[rel] = sig
                changed = seen_att.get(rel) != sig
                item_id = f"obsidian:{name}:{rel}"
                copy_path = att_root / name / rel if copy_att and st.st_size <= max_bytes else None
                if copy_path and (changed or not copy_path.exists()):
                    try:
                        copy_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, copy_path)
                        copied += 1
                    except OSError as e:
                        log.warning(_("obsidian: could not copy %s: %s"), p, e)
                        copy_path = None
                pdf_text = ""
                if p.suffix.lower() == ".pdf":
                    if changed:
                        pdf_text = _pdf_text(p)
                    else:  # reuse text extracted earlier
                        row = conn.execute("SELECT body FROM items WHERE id = ?", (item_id,)).fetchone()
                        if row and row[0] and "\n\n" in row[0]:
                            pdf_text = row[0].split("\n\n", 1)[1]
                refs = index.get(p.name.lower(), [])
                att_batch.append(attachment_to_item(vault, name, rel, p, st, refs, pdf_text, copy_path))
                if len(att_batch) >= 200:
                    att_written += upsert_items(conn, att_batch)
                    att_batch.clear()
            att_written += upsert_items(conn, att_batch)
            # deleted from the vault: flagged, the copy next to the database is kept
            att_written += mark_deleted(conn, [f"obsidian:{name}:{rel}" for rel in seen_att if rel not in current_att])
            set_state(conn, f"{state_key}:attachments", new_seen_att)
            conn.commit()
            written += att_written
        log.info(_("obsidian: %s — notes %d (written %d, deleted %d), attachments %d (written %d, copied %d)"),
                 name, len(current), n_notes, len(gone), len(current_att), att_written, copied)
    return written
