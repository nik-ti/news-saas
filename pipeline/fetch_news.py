"""
Pipeline — Fetch News.
Cron job: pulls latest articles from each active source in parallel.

Three extraction strategies per source, in order:
  1. RSS/Atom: if the feed_url looks like a feed, parse it directly (no browser).
  2. Link-based: extract article links from the crawled feed page.
  3. LLM fallback: for pages with inline content and no per-entry links
     (changelogs, update-card pages), extract items from the page text itself.
"""
import asyncio
import logging

import httpx

import config
from crawler.fetcher import fetch_page, extract_article_links, content_hash
from database import store
from research.llm import chat_json

logger = logging.getLogger(__name__)

RSS_URL_HINTS = ("/feed", "/rss", ".rss", ".xml", "/atom", "format=rss")


async def _fetch_rss_items(feed_url: str) -> list[dict]:
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


async def fetch_source_news(source: dict) -> list[dict]:
    """
    Fetch the latest articles from a single source.
    Returns list of new article dicts.
    """
    url = source["url"]
    source_id = source["id"]

    # Use feed_url if available, otherwise fall back to the main url
    feed_url = source.get("feed_url") or url

    # Strategy 0: RSS/Atom feed — parse directly, no browser needed
    if any(h in feed_url.lower() for h in RSS_URL_HINTS):
        rss_items = await _fetch_rss_items(feed_url)
        if rss_items:
            store.update_source_fetch_time(source_id)
            store.reset_fail_count(source_id)
            new_articles = []
            for item in rss_items:
                c_hash = content_hash(item["url"].split("?")[0].rstrip("/").lower())
                if store.article_exists(c_hash):
                    continue
                new_articles.append({
                    "source_id": source_id,
                    "title": item["title"],
                    "url": item["url"],
                    "summary": item.get("summary", ""),
                    "content_hash": c_hash,
                })
                if len(new_articles) >= config.MAX_ARTICLES_PER_FETCH:
                    break
            logger.info("Source %s (RSS): %d new articles", url, len(new_articles))
            return new_articles

    page = await fetch_page(feed_url)
    if not page["success"]:
        logger.warning("Fetch failed for source %s: %s", url, page["error"])
        # Tolerate transient failures; only deactivate after N consecutive ones
        fails = store.increment_fail_count(source_id)
        if fails >= config.MAX_CONSECUTIVE_FETCH_FAILURES:
            store.update_source_status(source_id, "error")
            logger.warning("Source %s marked as error after %d consecutive failures",
                           url, fails)
        return []

    # Mark source as successfully fetched
    store.update_source_fetch_time(source_id)
    store.reset_fail_count(source_id)

    # Strategy 1: extract article links from the feed page.
    # Keep only links that are plausibly articles: article-like URL slug, or a
    # headline-length title. Nav links ("Config Generator", "Setup Guide")
    # pass the generic extractor but not this bar. A real headline is never a
    # single word — that drops category links like "Alignment" whose URLs
    # (/research/alignment) are indistinguishable from article slugs.
    all_links = extract_article_links(page, base_url=feed_url,
                                      same_domain_only=True)
    article_links = [l for l in all_links
                     if (l.get("article_like") or len(l["title"]) >= 40)
                     and len(l["title"].split()) >= 3]

    new_articles = []
    for link in article_links:
        title = link["title"]
        article_url = link["url"]
        c_hash = content_hash(article_url.split("?")[0].rstrip("/").lower())

        # Skip duplicates
        if store.article_exists(c_hash):
            continue

        new_articles.append({
            "source_id": source_id,
            "title": title,
            "url": article_url,
            "summary": "",
            "content_hash": c_hash,
        })
        if len(new_articles) >= config.MAX_ARTICLES_PER_FETCH:
            break  # cap per cycle — avoids flooding summarize on first fetch

    # Strategy 2: no per-entry links found (changelog/update-card pages) —
    # extract items from the page content itself via LLM
    if not article_links:
        logger.info("No article links on %s — trying inline item extraction", feed_url)
        inline_items = await _extract_inline_items(feed_url, page)
        for item in inline_items:
            if store.article_exists(item["content_hash"]):
                continue
            item["source_id"] = source_id
            new_articles.append(item)
        if inline_items:
            logger.info("Inline extraction: %d items from %s", len(inline_items), feed_url)

    logger.info("Source %s: %d new articles", url, len(new_articles))
    return new_articles


async def fetch_all_news() -> dict:
    """
    Fetch news from all active sources in parallel.
    Returns summary: {total_sources, total_new, errors}
    """
    sources = store.get_active_sources()
    logger.info("Fetching news from %d active sources...", len(sources))

    if not sources:
        return {"total_sources": 0, "total_new": 0, "errors": 0}

    # Fetch in parallel (crawler handles its own semaphore)
    tasks = [fetch_source_news(s) for s in sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_new = 0
    errors = 0

    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            logger.error("Error fetching %s: %s", source["url"], result)
            errors += 1
            continue

        for article in result:
            store.add_article(
                source_id=article["source_id"],
                title=article["title"],
                url=article["url"],
                summary=article.get("summary", ""),
                content_hash=article["content_hash"],
            )
            total_new += 1

    logger.info("Fetch complete: %d new articles from %d sources (%d errors)",
                total_new, len(sources), errors)
    return {"total_sources": len(sources), "total_new": total_new, "errors": errors}
