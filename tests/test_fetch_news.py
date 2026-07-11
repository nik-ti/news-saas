"""F7 (RSS fail-hard + XML guard) and F11 (query-string permalink dedup)."""
import pytest

import pipeline.fetch_news as fn
from pipeline.fetch_news import SourceFetchError, _dedup_key, _item, snapshot_source


# ── F11: dedup key ────────────────────────────────────────────────────────────

def test_wordpress_permalinks_stay_distinct():
    a = _dedup_key("https://example.com/?p=123")
    b = _dedup_key("https://example.com/?p=124")
    root = _dedup_key("https://example.com/")
    assert a != b
    assert a != root and b != root


def test_id_query_on_path_kept():
    a = _dedup_key("https://example.com/news?id=77")
    b = _dedup_key("https://example.com/news?id=78")
    assert a != b


def test_tracking_params_still_stripped():
    assert _dedup_key("https://example.com/story?utm_source=x") == \
        _dedup_key("https://example.com/story")


def test_legacy_key_shape_preserved():
    # Existing DBs hashed scheme://host/path lowercased, no trailing slash.
    # Path-based URLs must produce the IDENTICAL key after the upgrade,
    # or every stored hash invalidates on deploy.
    url = "https://Example.com/News/Story-One/"
    assert _dedup_key(url) == "https://example.com/news/story-one"


def test_item_hash_uses_dedup_key():
    x = _item("t", "https://example.com/?p=1")
    y = _item("t", "https://example.com/?p=2")
    assert x["content_hash"] != y["content_hash"]


# ── F7: RSS fail-hard ─────────────────────────────────────────────────────────

async def test_proven_rss_source_raises_on_empty_feed(monkeypatch):
    async def empty_rss(url):
        return []
    fetch_calls = []

    async def no_fetch(url):
        fetch_calls.append(url)
        raise AssertionError("browser must not be used for a proven-RSS source")

    monkeypatch.setattr(fn, "fetch_rss_items", empty_rss)
    monkeypatch.setattr(fn, "fetch_page", no_fetch)

    source = {"url": "https://ex.com", "feed_url": "https://ex.com/feed",
              "fetch_method": "rss"}
    with pytest.raises(SourceFetchError):
        await snapshot_source(source)
    assert fetch_calls == []


async def test_sniffed_rss_source_still_falls_through(monkeypatch):
    # Legacy source (no stored method): URL sniff says RSS, feed empty →
    # crawler fallthrough is preserved.
    async def empty_rss(url):
        return []

    async def fake_fetch(url):
        return {"success": True, "content": "x" * 300, "title": "t",
                "html": "", "links": [], "url": url, "error": None}

    async def no_items(feed_url, page):
        return []

    monkeypatch.setattr(fn, "fetch_rss_items", empty_rss)
    monkeypatch.setattr(fn, "fetch_page", fake_fetch)
    monkeypatch.setattr(fn, "_extract_inline_items", no_items)

    source = {"url": "https://ex.com", "feed_url": "https://ex.com/feed",
              "fetch_method": None}
    items = await snapshot_source(source)
    assert items == []


# ── F7: XML guard on inline extraction ───────────────────────────────────────

async def test_inline_extraction_refuses_raw_xml(monkeypatch):
    llm_calls = []

    async def fake_llm(system, user):
        llm_calls.append(user)
        return {"items": [{"title": "hallucinated", "content": "junk"}]}

    monkeypatch.setattr(fn, "chat_json", fake_llm)
    page = {"content": '<?xml version="1.0"?><rss><channel>' + "x" * 300}
    items = await fn._extract_inline_items("https://ex.com/feed", page)
    assert items == []
    assert llm_calls == []  # the LLM was never asked


async def test_inline_extraction_still_works_on_text(monkeypatch):
    async def fake_llm(system, user):
        return {"items": [{"title": "Real update", "content": "Something shipped."}]}

    monkeypatch.setattr(fn, "chat_json", fake_llm)
    page = {"content": "Changelog\n\nReal update — something shipped. " + "y" * 300}
    items = await fn._extract_inline_items("https://ex.com/changelog", page)
    assert len(items) == 1
    assert items[0]["title"] == "Real update"
