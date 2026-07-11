"""F4 (repair before validation) and F13 (Google News dup guard)."""
import pipeline.fetch_news
import crawler.fetcher
import research.feed_finder
import research.engine as eng
from database import store
from research.feed_finder import Candidate


# ── F13: one Google News feed per stream, ever ───────────────────────────────

async def test_google_news_not_duplicated_on_requery(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="s", criteria={})

    async def fake_rss(url):
        return [{"title": "Headline - Pub", "url": "https://news.google.com/x",
                 "summary": ""}]
    monkeypatch.setattr(pipeline.fetch_news, "fetch_rss_items", fake_rss)

    await eng._add_google_news_source(sid, {"broad_domain": "crypto",
                                            "specific_topics": ["MiCA"]})
    assert len(store.get_sources_by_stream(sid)) == 1

    # Re-research shifts the profile → different query text. Still no second feed.
    await eng._add_google_news_source(sid, {"broad_domain": "cryptocurrency",
                                            "specific_topics": ["EU regulation"]})
    sources = store.get_sources_by_stream(sid)
    assert len(sources) == 1
    assert sources[0]["fetch_method"] == "rss"
    assert "news.google.com" in sources[0]["feed_url"]


async def test_google_news_skipped_when_feed_empty(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="s", criteria={})

    async def empty_rss(url):
        return []
    monkeypatch.setattr(pipeline.fetch_news, "fetch_rss_items", empty_rss)

    await eng._add_google_news_source(sid, {"broad_domain": "crypto"})
    assert store.get_sources_by_stream(sid) == []


# ── F4: feed repair ───────────────────────────────────────────────────────────

def _page(success=True, links=None):
    return {"url": "u", "title": "", "content": "", "html": "",
            "links": links or [], "success": success, "error": None}


async def test_repair_marks_proven_llm_feed_verified(monkeypatch):
    async def good_page(url):
        return _page(success=True)

    def six_links(page, feed_url):
        return [{"title": f"Headline number {i} is long", "url": f"https://a.com/p{i}",
                 "article_like": True} for i in range(6)]

    monkeypatch.setattr(crawler.fetcher, "fetch_page", good_page)
    monkeypatch.setattr(pipeline.fetch_news, "article_links_on_page", six_links)

    src = {"url": "https://a.com", "feed_url": "https://a.com/news"}
    out = await eng._repair_feed_url(src)
    assert out["_feed_verified"] is True
    assert out["feed_url"] == "https://a.com/news"      # LLM was right — kept
    assert out["fetch_method"] == "links"


async def test_repair_rss_hint_fast_path(monkeypatch):
    crawls = []

    async def no_crawl(url):
        crawls.append(url)
        return _page(success=False)

    async def fake_rss(url):
        return [{"title": "t", "url": "https://a.com/1", "summary": ""}] * 3

    monkeypatch.setattr(crawler.fetcher, "fetch_page", no_crawl)
    monkeypatch.setattr(pipeline.fetch_news, "fetch_rss_items", fake_rss)

    src = {"url": "https://a.com", "feed_url": "https://a.com/feed"}
    out = await eng._repair_feed_url(src)
    assert out["_feed_verified"] is True
    assert out["fetch_method"] == "rss"
    assert crawls == []                                  # no browser needed


async def test_repair_replaces_bad_feed_url(monkeypatch):
    async def dead_page(url):
        return _page(success=False)

    async def finder(url):
        return [Candidate(url="https://a.com/blog", kind="page", item_count=12)]

    monkeypatch.setattr(crawler.fetcher, "fetch_page", dead_page)
    monkeypatch.setattr(research.feed_finder, "find_news_pages", finder)

    src = {"url": "https://a.com", "feed_url": "https://a.com/broken-404"}
    out = await eng._repair_feed_url(src)
    assert out["feed_url"] == "https://a.com/blog"
    assert out["_feed_verified"] is True
    assert out["fetch_method"] == "links"


async def test_repair_failure_leaves_source_unverified(monkeypatch):
    async def dead_page(url):
        return _page(success=False)

    async def finder_boom(url):
        raise RuntimeError("site refused")

    monkeypatch.setattr(crawler.fetcher, "fetch_page", dead_page)
    monkeypatch.setattr(research.feed_finder, "find_news_pages", finder_boom)

    src = {"url": "https://a.com", "feed_url": "https://a.com/broken"}
    out = await eng._repair_feed_url(src)
    assert not out.get("_feed_verified")
    assert out["feed_url"] == "https://a.com/broken"


# ── F4: node_validate runs repair FIRST and skips re-validating proven pages ──

async def test_node_validate_repairs_before_validating(monkeypatch):
    validated_urls = []

    async def fake_repair(q):
        # Simulate: the repair fixes a broken feed_url and proves the new one.
        if q["url"] == "https://fixed.com":
            q["feed_url"] = "https://fixed.com/blog"
            q["_feed_verified"] = True
        return q

    async def fake_validate(urls):
        validated_urls.extend(urls)
        return [{"url": u, "fetchable": True, "status": "active",
                 "title": "", "content_preview": "", "error": None} for u in urls]

    monkeypatch.setattr(eng, "_repair_feed_url", fake_repair)
    monkeypatch.setattr(eng, "validate_sources", fake_validate)

    state = {"qualified": [
        {"url": "https://fixed.com", "feed_url": "https://fixed.com/broken",
         "match_score": 90},
        {"url": "https://other.com", "feed_url": "https://other.com/news",
         "match_score": 80},
    ], "log": []}

    state = await eng.node_validate(state)

    # The proven (repaired) page was NOT re-crawled by validation…
    assert validated_urls == ["https://other.com/news"]
    # …but IS present as fetchable in the validation map, under the REPAIRED url.
    val_map = {v["url"]: v for v in state["validated"]}
    assert val_map["https://fixed.com/blog"]["fetchable"] is True
    assert val_map["https://other.com/news"]["fetchable"] is True
