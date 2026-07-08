"""
Phase 4 — Fetch Validation.
Tests that accepted sources are actually fetchable with crawl4ai.
Sources that block the crawler or return paywall stubs are flagged.
"""
import logging
from typing import Optional

from crawler.fetcher import test_source

logger = logging.getLogger(__name__)


async def validate_source(url: str) -> dict:
    """
    Test-fetch a source URL.
    Returns: {url, fetchable, title, content_preview, error}
    """
    # RSS/Atom feeds: validate with the direct feed parser — the browser
    # crawler can false-flag raw XML as "blocked" (minimal visible text)
    from pipeline.fetch_news import RSS_URL_HINTS, _fetch_rss_items
    if any(h in url.lower() for h in RSS_URL_HINTS):
        items = await _fetch_rss_items(url)
        if items:
            logger.info("Validation: %s → active (RSS feed, %d items)", url, len(items))
            return {
                "url": url,
                "fetchable": True,
                "title": "RSS feed",
                "content_preview": items[0]["title"],
                "status": "active",
                "error": None,
            }

    result = await test_source(url)

    status = "active" if result["fetchable"] else "blocked"
    logger.info("Validation: %s → %s", url, status)

    return {
        "url": url,
        "fetchable": result["fetchable"],
        "title": result["title"],
        "content_preview": result["content_preview"],
        "status": status,
        "error": result["error"],
    }


async def validate_sources(urls: list[str]) -> list[dict]:
    """Validate multiple sources in parallel."""
    import asyncio
    tasks = [validate_source(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    final = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            final.append({
                "url": url,
                "fetchable": False,
                "title": "",
                "content_preview": "",
                "status": "error",
                "error": str(result),
            })
        else:
            final.append(result)

    return final