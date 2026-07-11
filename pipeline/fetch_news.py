"""
Pipeline — Source snapshotting.

Reads a source's article-list page and reports EVERYTHING currently on it.
Deciding what is new, what to baseline, and what to post is the caller's job
(see pipeline/news_cycle.py) — this module performs no DB writes.

Three extraction strategies per source, in order:
  1. RSS/Atom: if the feed_url looks like a feed, parse it directly (no browser).
  2. Link-based: extract article links from the crawled feed page.
  3. LLM fallback: for pages with inline content and no per-entry links
     (changelogs, update-card pages), extract items from the page text itself.
"""
import logging
import re
from urllib.parse import urlparse

import httpx

from crawler.fetcher import fetch_page, extract_article_links, content_hash
from research.llm import chat_json

logger = logging.getLogger(__name__)

RSS_URL_HINTS = ("/feed", "/rss", ".rss", ".xml", "/atom", "format=rss")


def article_links_on_page(page: dict, feed_url: str) -> list[dict]:
    """
    The article links we would actually poll from this page.

    Keep only links that are plausibly articles: an article-like URL slug, or a
    headline-length title. Nav links ("Config Generator", "Setup Guide") pass the
    generic extractor but not this bar. A real headline is never a single word —
    that drops category links like "Alignment" whose URLs (/research/alignment)
    are indistinguishable from article slugs.

    Shared by the poller and by feed discovery, so "a page worth polling" means
    exactly the same thing in both.
    """
    all_links = extract_article_links(page, base_url=feed_url, same_domain_only=True)
    return [l for l in all_links
            if (l.get("article_like") or len(l["title"]) >= 40)
            and len(l["title"].split()) >= 3]


async def fetch_rss_items(feed_url: str) -> list[dict]:
    """
    Fetch and parse an RSS/Atom feed directly over HTTP (no headless browser).
    Returns [] if the URL isn't actually a parseable feed.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(feed_url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsStreamBot/1.0)",
            })
            resp.raise_for_status()
            body = resp.text
    except Exception as e:
        logger.info("RSS fetch failed for %s: %s", feed_url, e)
        return []

    stripped = body.lstrip()[:300].lower()
    if not ("<rss" in stripped or "<feed" in stripped or "<?xml" in stripped):
        return []  # not a feed — caller falls through to the crawler

    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(body, "xml")
    except Exception:
        soup = BeautifulSoup(body, "html.parser")

    items = []
    # RSS: <item><title>..<link>text</link>  |  Atom: <entry><title>..<link href=".."/>
    for node in soup.find_all(["item", "entry"]):
        title_tag = node.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        link = ""
        link_tag = node.find("link")
        if link_tag:
            link = (link_tag.get("href") or link_tag.get_text(strip=True) or "").strip()
        desc_tag = node.find(["description", "summary", "content"])
        desc = desc_tag.get_text(" ", strip=True)[:500] if desc_tag else ""

        if not title or not link:
            continue
        items.append({"title": title, "url": link, "summary": desc})

    return items

SYSTEM_PROMPT_EXTRACT_ITEMS = """\
You are a news item extractor. You are given the text content of a page that \
publishes updates/news INLINE (e.g. a changelog or update-card page) without \
linking to separate article pages.

Extract the individual news/update items from the text. For each item output:
- "title": a short headline for the item (use the item's own heading if present)
- "date": the publication date if visible, else null
- "content": the item's text, condensed to 2-3 sentences

Only extract REAL dated update/news entries. Skip navigation, marketing copy, \
footers. Newest items first. Maximum 10 items.

Output JSON: {"items": [{"title": "...", "date": "...", "content": "..."}]}
If the page contains no update entries, output {"items": []}."""


async def _extract_inline_items(feed_url: str, page: dict) -> list[dict]:
    """LLM fallback: extract news items from pages without article links."""
    content = page.get("content", "")
    if len(content) < 200:  # nothing meaningful to extract
        return []

    # Raw XML means we crawled a feed with the browser (transient RSS failure
    # upstream). Asking an LLM to "extract news" from feed markup produces
    # hallucinated items that would be posted to the user.
    if content.lstrip()[:200].lower().startswith(("<?xml", "<rss", "<feed")):
        logger.warning("Inline extraction skipped for %s — page is raw XML", feed_url)
        return []

    result = await chat_json(
        SYSTEM_PROMPT_EXTRACT_ITEMS,
        f"Page URL: {feed_url}\n\nPage content:\n{content[:6000]}\n\n"
        f"Extract the news/update items as JSON.",
    )
    items = result.get("items", [])
    if not isinstance(items, list):
        return []

    extracted = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        body = (item.get("content") or "").strip()
        if not title or not body:
            continue
        extracted.append({
            "title": title,
            "url": feed_url,  # no dedicated page — link to the feed itself
            "summary": body,
            # Hash on source+title so the same entry isn't re-added every cycle
            "content_hash": content_hash(f"{feed_url}::{title.lower()}"),
        })
    return extracted


class SourceFetchError(Exception):
    """The source's feed page could not be read this cycle."""


_ID_QUERY_RE = re.compile(r"(?:^|&)(?:p|id|post|article)=\d+")


def _dedup_key(url: str) -> str:
    """
    Canonical form of an article URL for dedup hashing.

    Tracking params are stripped, BUT on sites with query-string permalinks
    (WordPress /?p=123 and friends) the query IS the article identity — blindly
    dropping it collapses every post to the homepage, so the source only ever
    delivers its first article.

    For URLs without an identity query this is EXACTLY the legacy key
    (scheme://host/path, no query, no trailing slash, lowercased) so existing
    stored hashes stay valid across the upgrade.
    """
    base = url.split("?")[0].rstrip("/").lower()
    p = urlparse(url)
    query = (p.query or "").lower()
    if query and (not p.path.strip("/") or _ID_QUERY_RE.search(query)):
        return f"{base}?{query}"
    return base


def _item(title: str, url: str, summary: str = "", c_hash: str = "") -> dict:
    return {
        "title": title,
        "url": url,
        "summary": summary,
        "content_hash": c_hash or content_hash(_dedup_key(url)),
    }


async def snapshot_source(source: dict) -> list[dict]:
    """
    Return EVERY article currently listed on this source's feed page.

    Pure read: no DB writes, no dedup, no caps. Raises SourceFetchError if the
    page can't be read, so the caller can decide how to count the failure.
    """
    url = source["url"]
    feed_url = source.get("feed_url") or url

    # Strategy 0: RSS/Atom feed — parse directly, no browser needed.
    # Prefer the method proven at discovery time; fall back to a URL sniff so
    # legacy sources (no stored method) still work. This is what lets a feed at
    # a clean URL like /index~atom.xml be read as RSS even without an obvious hint.
    method = (source.get("fetch_method") or "").lower()
    looks_rss = method == "rss" or any(h in feed_url.lower() for h in RSS_URL_HINTS)
    if looks_rss:
        rss_items = await fetch_rss_items(feed_url)
        if rss_items:
            items = [_item(i["title"], i["url"], i.get("summary", "")) for i in rss_items]
            logger.info("Source %s (RSS): %d items on page", url, len(items))
            return items
        if method == "rss":
            # This source is PROVEN to be a feed. An empty read is a fetch
            # failure to be counted, not a reason to point a browser (and then
            # an LLM) at raw XML and post whatever it hallucinates.
            raise SourceFetchError("RSS feed returned no items")

    page = await fetch_page(feed_url)
    if not page["success"]:
        raise SourceFetchError(page.get("error") or "unknown fetch error")

    # Strategy 1: extract article links from the feed page.
    article_links = article_links_on_page(page, feed_url)

    if article_links:
        items = [_item(l["title"], l["url"]) for l in article_links]
        logger.info("Source %s: %d items on page", url, len(items))
        return items

    # Strategy 2: no per-entry links (changelog/update-card pages) —
    # extract items from the page content itself via LLM
    logger.info("No article links on %s — trying inline item extraction", feed_url)
    inline_items = await _extract_inline_items(feed_url, page)
    items = [_item(i["title"], i["url"], i["summary"], i["content_hash"])
             for i in inline_items]
    logger.info("Source %s: %d inline items on page", url, len(items))
    return items
