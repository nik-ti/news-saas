"""Age guard (no old articles, ever) + outbound-aggregator link scope."""
from datetime import datetime, timedelta, timezone

import config
import pipeline.fetch_news as fn
import pipeline.news_cycle as nc
import pipeline.summarize as summ
import research.feed_finder as ff
from database import store
from database.models import get_connection
from pipeline.fetch_news import (_item, _parse_feed_body, article_links_on_page,
                                 parse_feed_date, snapshot_source)
from research.urlutils import date_from_url


def _days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


# ── A1: feed date parsing ─────────────────────────────────────────────────────

def test_parse_feed_date_rfc822_and_iso():
    rfc = parse_feed_date("Sat, 05 Jul 2026 09:30:00 GMT")
    assert rfc.year == 2026 and rfc.month == 7 and rfc.day == 5
    iso = parse_feed_date("2026-07-05T09:30:00Z")
    assert iso == rfc.replace(tzinfo=timezone.utc)
    assert parse_feed_date("next Tuesday-ish") is None
    assert parse_feed_date("") is None
    assert parse_feed_date(None) is None


def test_parse_feed_body_extracts_pubdate():
    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>Old story</title><link>https://x.com/old</link>
        <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
      <item><title>Undated story</title><link>https://x.com/undated</link></item>
    </channel></rss>"""
    items = _parse_feed_body(rss)
    assert items[0]["published_at"].year == 2024
    assert items[1]["published_at"] is None


def test_parse_feed_body_extracts_atom_published():
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Fresh</title><link href="https://x.com/fresh"/>
        <published>2026-07-10T08:00:00Z</published></entry>
    </feed>"""
    items = _parse_feed_body(atom)
    assert items[0]["published_at"].month == 7


# ── A2: URL date heuristic ────────────────────────────────────────────────────

def test_date_from_url():
    old = date_from_url("https://x.com/2023/05/02/story-title")
    assert old.year == 2023 and old.month == 5 and old.day == 2
    fresh = date_from_url("https://x.com/news/2026-07-10-story")
    assert fresh.day == 10
    # Year-only resolves to Dec 31 — only clearly-old years trip the guard.
    year_only = date_from_url("https://x.com/2024/story")
    assert (year_only.month, year_only.day) == (12, 31)
    assert date_from_url("https://x.com/blog/some-story") is None
    # A year-like TOPIC slug is not a dateline — must not be judged stale.
    assert date_from_url("https://x.com/2008-financial-crisis-lessons") is None
    # A number that isn't a real month fails open.
    assert date_from_url("https://x.com/2024/99/nonsense") is None


def test_item_attaches_url_date_when_feed_gave_none():
    item = _item("A headline long enough here", "https://x.com/2023/01/05/story")
    assert item["published_at"].year == 2023
    explicit = _days_ago(1)
    item2 = _item("t", "https://x.com/2023/01/05/story", published_at=explicit)
    assert item2["published_at"] == explicit  # explicit feed date wins


# ── A3: Phase A never delivers stale items ────────────────────────────────────

def _mk_source(url, baselined=True, method=""):
    sid = store.create_stream(user_id=1, name=url, criteria={"topic": "x"})
    src = store.add_source(stream_id=sid, url=url, fetch_method=method)
    if baselined:
        store.mark_source_baselined(src)
    return sid, src


def _deliveries(stream_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT d.status, a.content_hash FROM deliveries d "
        "JOIN articles a ON d.article_id = a.id WHERE d.stream_id = ?",
        (stream_id,)).fetchall()
    conn.close()
    return {r["content_hash"]: r["status"] for r in rows}


def _snap(mapping):
    async def fake(source):
        return mapping[source["url"]]
    return fake


def _aged_item(h, days_old):
    return {"title": f"Headline number {h} words", "url": f"https://x.com/{h}",
            "summary": "", "content_hash": h, "published_at": _days_ago(days_old)}


async def test_stale_items_recorded_but_never_delivered(temp_db, monkeypatch):
    sid, src = _mk_source("https://a.com")
    items = [_aged_item("FRESH", 1), _aged_item("STALE",
             config.MAX_ARTICLE_AGE_DAYS + 5)]
    monkeypatch.setattr(nc, "snapshot_source", _snap({"https://a.com": items}))

    _, queued = await nc._baseline_and_fetch_phase()

    assert queued == 1
    d = _deliveries(sid)
    assert d.get("FRESH") == "new"
    assert "STALE" not in d                       # no delivery row at all
    # …but the stale item IS recorded, so it never comes back next cycle:
    assert "STALE" in store.source_seen_hashes(src)


async def test_undated_items_still_delivered(temp_db, monkeypatch):
    sid, src = _mk_source("https://a.com")
    item = {"title": "Headline of many words", "url": "https://a.com/story",
            "summary": "", "content_hash": "H", "published_at": None}
    monkeypatch.setattr(nc, "snapshot_source", _snap({"https://a.com": [item]}))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 1                            # unknown date ≠ stale


async def test_stale_item_not_requeued_next_cycle(temp_db, monkeypatch):
    sid, src = _mk_source("https://a.com")
    items = [_aged_item("OLD", config.MAX_ARTICLE_AGE_DAYS + 30)]
    monkeypatch.setattr(nc, "snapshot_source", _snap({"https://a.com": items}))

    await nc._baseline_and_fetch_phase()
    _, queued2 = await nc._baseline_and_fetch_phase()   # same page again
    assert queued2 == 0
    assert _deliveries(sid) == {}


# ── A4: LLM dateline backstop in Phase B ─────────────────────────────────────

def test_published_is_stale_parsing():
    old = (_days_ago(config.MAX_ARTICLE_AGE_DAYS + 10)).strftime("%Y-%m-%d")
    recent = (_days_ago(1)).strftime("%Y-%m-%d")
    assert summ._published_is_stale(old) is True
    assert summ._published_is_stale(recent) is False
    assert summ._published_is_stale(None) is False
    assert summ._published_is_stale("no idea") is False   # fail open


async def test_summarizer_dateline_marks_delivery_stale(temp_db, monkeypatch):
    sid, src = _mk_source("https://a.com")
    item = {"title": "Headline of many words", "url": "https://a.com/story",
            "summary": "", "content_hash": "H", "published_at": None}
    monkeypatch.setattr(nc, "snapshot_source", _snap({"https://a.com": [item]}))
    await nc._baseline_and_fetch_phase()

    async def fake_fetch(url):
        return {"success": True, "content": "Article body. " * 60,
                "title": "T", "html": "", "links": [], "error": None}

    old = (_days_ago(config.MAX_ARTICLE_AGE_DAYS + 10)).strftime("%Y-%m-%d")

    async def dated_llm(system, user):
        return {"summary": "A fine full summary of an old article.",
                "published": old}

    sends = []

    async def no_send(chat_id, html, reply_markup=None):
        sends.append(html)
        return {"ok": True}

    monkeypatch.setattr(summ, "fetch_page", fake_fetch)
    monkeypatch.setattr(summ, "chat_json", dated_llm)
    monkeypatch.setattr(nc, "send_html_message_async", no_send)

    stats = await nc._post_phase()
    assert stats["stale"] == 1
    assert stats["posted"] == 0
    assert sends == []
    assert _deliveries(sid)["H"] == "stale"


async def test_recent_dateline_flows_through(monkeypatch):
    async def fake_fetch(url):
        return {"success": True, "content": "Article body. " * 60,
                "title": "T", "html": "", "links": [], "error": None}

    async def dated_llm(system, user):
        return {"summary": "A fine full summary.",
                "published": _days_ago(1).strftime("%Y-%m-%d")}

    monkeypatch.setattr(summ, "fetch_page", fake_fetch)
    monkeypatch.setattr(summ, "chat_json", dated_llm)

    summary, _ = await summ.summarize_article(
        {"title": "T", "url": "https://a.com/x", "summary": ""})
    assert summary == "A fine full summary."


# ── B: outbound aggregator scope ─────────────────────────────────────────────

def _page_with_links(links):
    return {"url": "https://agg.com/news", "title": "Agg", "content": "x",
            "html": "", "links": links, "success": True, "error": None}


def _ext_links(n, domain="elsewhere.com"):
    return [{"href": f"https://{domain}/2026/07/story-{i}",
             "text": f"A proper headline with several words {i}"}
            for i in range(n)]


def test_external_scope_keeps_offdomain_headlines():
    page = _page_with_links(_ext_links(6))
    assert article_links_on_page(page, "https://agg.com/news") == []
    ext = article_links_on_page(page, "https://agg.com/news", external=True)
    assert len(ext) == 6


def test_external_scope_still_drops_social_domains():
    links = _ext_links(3) + [
        {"href": "https://twitter.com/someone/status/123456789",
         "text": "A long enough social media caption here"}]
    ext = article_links_on_page(_page_with_links(links),
                                "https://agg.com/news", external=True)
    assert all("twitter.com" not in l["url"] for l in ext)


def test_score_crawled_flags_external_aggregator():
    cand = ff._score_crawled(_page_with_links(_ext_links(8)),
                             "https://agg.com/news")
    assert cand is not None
    assert cand.scope == "external"
    assert cand.item_count == 8


def test_score_crawled_internal_stays_internal():
    links = [{"href": f"https://agg.com/2026/07/story-{i}",
              "text": f"A proper internal headline number {i}"} for i in range(7)]
    cand = ff._score_crawled(_page_with_links(links), "https://agg.com/news")
    assert cand.scope == "internal"


async def test_find_news_pages_prefers_user_given_page(monkeypatch):
    """The homepage may score higher, but the page the user gave verifies too —
    it must come first. (Old code returned the homepage.)"""
    async def no_feeds(urls):
        return []

    async def fake_get(url, timeout=12):
        return 200, "<html></html>"

    home_links = [{"href": f"https://site.com/tools/thing-number-{i}",
                   "text": f"An internal tool card title {i}"} for i in range(20)]

    async def fake_fetch(url):
        if url.rstrip("/") == "https://site.com":
            return _page_with_links(home_links) | {"url": url}
        if url.rstrip("/") == "https://site.com/news":
            return _page_with_links(_ext_links(9)) | {"url": url}
        return {"success": False, "content": "", "title": "", "html": "",
                "links": [], "url": url, "error": "404"}

    monkeypatch.setattr(ff, "_verify_feeds", no_feeds)
    monkeypatch.setattr(ff, "_get", fake_get)
    monkeypatch.setattr(ff, "fetch_page", fake_fetch)
    monkeypatch.setattr(ff, "POLITE_DELAY_SECONDS", 0)

    pages = await ff.find_news_pages("https://site.com/news")
    assert pages, "the given outbound-aggregator page must verify"
    assert pages[0].url.rstrip("/") == "https://site.com/news"
    assert pages[0].scope == "external"


async def test_snapshot_polls_external_links_for_links_ext(monkeypatch):
    async def fake_fetch(url):
        return _page_with_links(_ext_links(4))

    monkeypatch.setattr(fn, "fetch_page", fake_fetch)

    source = {"url": "https://agg.com", "feed_url": "https://agg.com/news",
              "fetch_method": "links_ext"}
    items = await snapshot_source(source)
    assert len(items) == 4
    assert all("elsewhere.com" in i["url"] for i in items)

    # An ordinary links source on the same page sees nothing (and falls to
    # inline extraction, stubbed empty here).
    async def no_inline(feed_url, page):
        return []
    monkeypatch.setattr(fn, "_extract_inline_items", no_inline)
    source2 = {"url": "https://agg.com", "feed_url": "https://agg.com/news",
               "fetch_method": "links"}
    assert await snapshot_source(source2) == []
