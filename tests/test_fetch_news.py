"""F7 (RSS fail-hard + XML guard), F11 (query-string permalink dedup), and
§2.6 conditional GET."""
import pytest

import pipeline.fetch_news as fn
from pipeline.fetch_news import (SourceFetchError, UNCHANGED, _dedup_key,
                                 _item, snapshot_source)


def _conditional(items_or_unchanged, meta=None):
    """Stub for fetch_rss_conditional returning fixed items/meta."""
    async def fake(feed_url, etag="", last_modified=""):
        return items_or_unchanged, (meta or {})
    return fake


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
    fetch_calls = []

    async def no_fetch(url):
        fetch_calls.append(url)
        raise AssertionError("browser must not be used for a proven-RSS source")

    monkeypatch.setattr(fn, "fetch_rss_conditional", _conditional([]))
    monkeypatch.setattr(fn, "fetch_page", no_fetch)

    source = {"url": "https://ex.com", "feed_url": "https://ex.com/feed",
              "fetch_method": "rss"}
    with pytest.raises(SourceFetchError):
        await snapshot_source(source)
    assert fetch_calls == []


async def test_sniffed_rss_source_still_falls_through(monkeypatch):
    # Legacy source (no stored method): URL sniff says RSS, feed empty →
    # crawler fallthrough is preserved.
    async def fake_fetch(url):
        return {"success": True, "content": "x" * 300, "title": "t",
                "html": "", "links": [], "url": url, "error": None}

    async def no_items(feed_url, page):
        return []

    monkeypatch.setattr(fn, "fetch_rss_conditional", _conditional([]))
    monkeypatch.setattr(fn, "fetch_page", fake_fetch)
    monkeypatch.setattr(fn, "_extract_inline_items", no_items)

    source = {"url": "https://ex.com", "feed_url": "https://ex.com/feed",
              "fetch_method": None}
    items = await snapshot_source(source)
    assert items == []


# ── §2.6: conditional GET ─────────────────────────────────────────────────────

async def test_304_returns_unchanged_sentinel(monkeypatch):
    monkeypatch.setattr(fn, "fetch_rss_conditional",
                        _conditional(UNCHANGED, {"etag": "abc"}))
    source = {"url": "https://ex.com", "feed_url": "https://ex.com/feed",
              "fetch_method": "rss", "etag": "abc"}
    assert await snapshot_source(source) is UNCHANGED


async def test_fresh_read_hands_back_new_validators(monkeypatch):
    monkeypatch.setattr(fn, "fetch_rss_conditional", _conditional(
        [{"title": "Long enough headline here", "url": "https://ex.com/1",
          "summary": ""}],
        {"etag": 'W/"new"', "last_modified": "Sun, 13 Jul 2026 00:00:00 GMT"}))
    source = {"url": "https://ex.com", "feed_url": "https://ex.com/feed",
              "fetch_method": "rss"}
    items = await snapshot_source(source)
    assert len(items) == 1
    assert source["_new_etag"] == 'W/"new"'
    assert source["_new_last_modified"] == "Sun, 13 Jul 2026 00:00:00 GMT"


async def test_conditional_get_sends_validators_and_handles_304(monkeypatch):
    """fetch_rss_conditional itself: request headers + 304 handling."""
    seen_headers = {}

    class FakeResp:
        status_code = 304
        headers = {}
        text = ""

    class FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, timeout=None, headers=None):
            seen_headers.update(headers or {})
            return FakeResp()

    monkeypatch.setattr(fn.httpx, "AsyncClient", FakeClient)
    items, meta = await fn.fetch_rss_conditional(
        "https://ex.com/feed", etag='W/"x"',
        last_modified="Sat, 12 Jul 2026 00:00:00 GMT")
    assert items is UNCHANGED
    assert seen_headers["If-None-Match"] == 'W/"x"'
    assert seen_headers["If-Modified-Since"] == "Sat, 12 Jul 2026 00:00:00 GMT"
    assert meta["etag"] == 'W/"x"'                      # validators preserved


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
