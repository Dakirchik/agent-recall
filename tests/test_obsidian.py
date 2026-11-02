import json

from agent_recall import query
from agent_recall.collectors.obsidian import extract_embeds, parse_frontmatter


def test_frontmatter_and_tags(env):
    env.note("Ideas/Idea.md", "---\ntags: [pkm, idea]\ncreated: 2026-10-30\n---\n# Title here\nText #inline\n```\n#notatag\n```\n")
    env.sync()
    item = query.get_item(env.conn(), "obsidian:vault:Ideas/Idea.md")
    meta = json.loads(item["meta"])
    assert item["title"] == "Title here"
    assert item["created_at"].startswith("2026-10-30")
    assert set(meta["tags"]) == {"pkm", "idea", "inline"}


def test_frontmatter_fallback_parser():
    fm, body = parse_frontmatter("---\ntitle: 'Hi'\ntags:\n  - a\n  - b\n---\nbody")
    assert fm["title"] == "Hi" and fm["tags"] == ["a", "b"] and body == "body"


def test_adr_kind_and_deleted_notes_are_kept(env):
    env.note("ADR/ADR-001 storage.md", "# ADR-001\nUse SQLite FTS5")
    p = env.note("Daily/2026-11-01.md", "temporary")
    env.sync()
    c = env.conn()
    assert query.get_item(c, "obsidian:vault:ADR/ADR-001 storage.md")["kind"] == "adr"
    p.unlink()
    env.sync()
    gone = query.get_item(env.conn(), "obsidian:vault:Daily/2026-11-01.md")
    assert gone["deleted_at"] is not None
    assert query.search(env.conn(), "temporary") == []


def test_embed_context_uses_heading_and_nearby_text():
    body = ("## Login bug\nWhat the browser showed on prod:\n![[Pasted image 20261101143005.png]]\n\n"
            "Diagram: ![d](../Attachments/auth%20flow.png)\n```\n![[in-code.png]]\n```")
    embeds = extract_embeds(body)
    assert [e["name"] for e in embeds] == ["Pasted image 20261101143005.png", "auth flow.png"]
    assert "Login bug" in embeds[0]["context"] and "browser showed on prod" in embeds[0]["context"]
    assert "Diagram" in embeds[1]["context"] and "browser" not in embeds[1]["context"]


def test_attachments_are_searchable_copied_and_inherit_projects(env):
    env.projects = [{"id": "shop", "name": "Shop", "obsidian": ["work/shop/*"]}]
    env.file("Attachments/Pasted image 20261101143005.png")
    env.file("Attachments/orphan.jpg")
    env.note("Work/Shop/Bug.md", "## Checkout crash\nStack trace from staging\n![[Pasted image 20261101143005.png]]\n")
    env.sync()
    c = env.conn()
    hits = query.search(c, "checkout staging", kind="image")
    assert len(hits) == 1 and hits[0]["title"].startswith("Скриншот")
    item = query.get_item(c, hits[0]["id"])
    assert "shop" in item["projects"]
    assert (env.data / "attachments" / "vault" / "Attachments" / "Pasted image 20261101143005.png").exists()
    assert query.get_item(c, "obsidian:vault:Attachments/orphan.jpg")["projects"] == []


def test_second_sync_writes_nothing(env):
    env.note("a.md", "hello")
    env.file("img.png")
    env.sync()
    res = env.sync()
    assert res["sources"]["obsidian"]["written"] == 0
