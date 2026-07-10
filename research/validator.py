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
    from pipeline.fetch_news import RSS_URL_HINTS, fetch_rss_items
    if any(h in url.lower() for h in RSS_URL_HINTS):
        items = await fetch_rss_items(url)
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


def _failed(url: str, error: str) -> dict:
    return {"url": url, "fetchable": False, "title": "", "content_preview": "",
            "status": "blocked", "error": error}


async def validate_sources(urls: list[str]) -> list[dict]:
    """
    Validate multiple sources, then give every failure a second, unhurried try.

    Validation runs immediately after qualification has hammered these same
    hosts with dozens of parallel crawls. A source that fails here is usually
    rate-limited, not broken — and condemning it means the user never sees that
    publication again. So failures are retried one at a time, with a pause.
    """
    import asyncio

    results = await asyncio.gather(*(validate_source(u) for u in urls),
                                   return_exceptions=True)

    final: list[dict] = []
    retry: list[int] = []
    for i, (url, result) in enumerate(zip(urls, results)):
        if isinstance(result, Exception):
            final.append(_failed(url, str(result)))
            retry.append(i)
        else:
            final.append(result)
            if not result["fetchable"]:
                retry.append(i)

    if retry:
        logger.info("Validation: retrying %d failed source(s) sequentially", len(retry))
    for i in retry:
        await asyncio.sleep(1.5)
        try:
            second = await validate_source(urls[i])
        except Exception as e:
            logger.warning("Validation retry crashed for %s: %s", urls[i], e)
            continue
        if second["fetchable"]:
            logger.info("Validation: %s passed on retry", urls[i])
            final[i] = second

    return final