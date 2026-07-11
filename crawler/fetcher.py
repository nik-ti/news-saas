"""
Crawl4AI wrapper — async web fetching with memory-conscious browser management.
Uses the crawl4ai 0.4+ API with BrowserConfig and CrawlerRunConfig.
"""
import asyncio
import hashlib
import logging
from typing import Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

# ── Concurrency control ───────────────────────────────────────────────────────
_crawl_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_CRAWLS)
_crawler = None  # lazily initialised
_crawler_lock = asyncio.Lock()  # guards create/reset so racers can't double-start

# Error text that means the shared browser itself is gone (crashed, OOM-killed),
# not that one page failed. Without a reset, a cached dead browser fails every
# fetch forever and the whole system silently deactivates.
_BROWSER_DEAD_MARKERS = (
    "browser has been closed",
    "browser closed",
    "target closed",
    "connection closed",
    "pipe closed",
    "not connected",
)


async def _get_crawler():
    """Lazily initialise the AsyncWebCrawler (singleton)."""
    global _crawler
    async with _crawler_lock:
        if _crawler is None:
            from crawl4ai import AsyncWebCrawler, BrowserConfig

            browser_config = BrowserConfig(
                headless=True,
                verbose=False,
                text_mode=True,          # disable images/media for speed + memory
                light_mode=True,         # minimal browser setup
                memory_saving_mode=True, # aggressive memory optimization
                enable_stealth=True,     # patch the automation fingerprints bot-walls look for
                user_agent_mode="random",  # rotate a realistic UA instead of HeadlessChrome
            )
            crawler = AsyncWebCrawler(config=browser_config)
            # Don't cache a half-initialised browser — a raise here leaves
            # _crawler None so the next fetch retries from scratch.
            await crawler.start()
            _crawler = crawler
        return _crawler


async def _reset_crawler():
    """Drop the cached browser so the next fetch starts a fresh one."""
    global _crawler
    async with _crawler_lock:
        crawler, _crawler = _crawler, None
    if crawler is not None:
        try:
            await crawler.close()
        except Exception:
            pass  # it was likely already dead — that's why we're here
    logger.warning("Crawler reset — a fresh browser will start on the next fetch")


async def fetch_page(url: str) -> Optional[dict]:
    """
    Fetch a single URL with crawl4ai.
    Returns dict with keys: url, title, content, links, success, error.
    """
    async with _crawl_semaphore:
        try:
            from crawl4ai import CrawlerRunConfig, CacheMode

            crawler = await _get_crawler()
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                page_timeout=30000,
                word_count_threshold=1,
                verbose=False,
            )
            result = await crawler.arun(url=url, config=run_config)

            if not result.success:
                logger.warning("Crawl failed for %s: %s", url, result.error_message)
                return {
                    "url": url,
                    "title": "",
                    "content": "",
                    "html": "",
                    "links": [],
                    "success": False,
                    "error": result.error_message or "Unknown error",
                }

            # Extract content from markdown (crawl4ai's primary output)
            content_text = ""
            if hasattr(result, "markdown") and result.markdown:
                content_text = result.markdown

            # Extract links — crawl4ai returns dict with 'internal' and 'external' keys
            links = []
            if hasattr(result, "links") and result.links:
                raw_links = result.links
                if isinstance(raw_links, dict):
                    for category in ("internal", "external"):
                        for link in raw_links.get(category, []):
                            links.append(link)
                elif isinstance(raw_links, list):
                    links = raw_links

            # Extract title from metadata. The key can be present but null,
            # so coerce — callers slice this string.
            title = ""
            if hasattr(result, "metadata") and result.metadata:
                title = result.metadata.get("title") or ""

            # Raw HTML for feed autodiscovery. Sites behind Cloudflare answer the
            # browser but 403 a plain HTTP client, so the crawler is often the only
            # way to see their <link> tags. Keep the whole <head> — some sites bury
            # megabytes of inline JSON in it, and a blind cap would slice off the
            # feed declaration that lives at the end.
            raw_html = ""
            if hasattr(result, "html") and result.html:
                head_end = result.html.lower().find("</head>")
                raw_html = (result.html[:head_end + 7] if head_end != -1
                            else result.html[:150_000])

            return {
                "url": url,
                "title": title,
                "content": content_text[:8000],  # cap to avoid huge payloads
                "html": raw_html,
                "links": links[:300],  # cap links
                "success": True,
                "error": None,
            }
        except Exception as e:
            if any(m in str(e).lower() for m in _BROWSER_DEAD_MARKERS):
                await _reset_crawler()
            logger.error("Exception crawling %s: %s", url, e)
            return {
                "url": url,
                "title": "",
                "content": "",
                "html": "",
                "links": [],
                "success": False,
                "error": str(e),
            }


async def fetch_multiple(urls: list[str]) -> list[dict]:
    """Fetch multiple URLs in parallel (bounded by semaphore)."""
    tasks = [fetch_page(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Convert exceptions to error dicts
    final = []
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            final.append({
                "url": url, "title": "", "content": "", "html": "", "links": [],
                "success": False, "error": str(result),
            })
        else:
            final.append(result)
    return final


async def test_source(url: str) -> dict:
    """
    Test whether a source URL is fetchable.
    Returns dict: fetchable (bool), title, content_preview, error.
    """
    result = await fetch_page(url)
    if not result["success"]:
        return {
            "fetchable": False,
            "title": "",
            "content_preview": "",
            "error": result["error"],
        }
    return {
        "fetchable": True,
        "title": result["title"],
        "content_preview": result["content"][:500],
        "error": None,
    }


def extract_article_links(page_result: dict, base_url: str = "",
                          same_domain_only: bool = False) -> list[dict]:
    """
    From a crawled page result, extract likely article links.
    crawl4ai returns links as dicts with keys like: href, text.
    Returns list of {title, url}, deduped, article-like links first.
    """
    from research.urlutils import is_article_url, registered_domain

    page_url = base_url or page_result.get("url", "")
    page_domain = registered_domain(page_url) if page_url else ""

    articles = []
    seen = set()
    for link in page_result.get("links", []):
        if isinstance(link, dict):
            title = link.get("text", link.get("title", ""))
            url = link.get("href", link.get("url", ""))
        elif isinstance(link, (list, tuple)) and len(link) >= 2:
            title, url = link[0], link[1]
        else:
            continue

        if not url or not title:
            continue
        title = " ".join(title.split())  # collapse whitespace/newlines

        # Build absolute URL
        if url.startswith("/"):
            parsed = urlparse(page_url)
            url = f"{parsed.scheme}://{parsed.netloc}{url}"
        elif not url.startswith("http"):
            continue

        # Skip social/utility domains (exact or subdomain match)
        social_domains = {"facebook.com", "twitter.com", "x.com", "instagram.com",
                          "linkedin.com", "youtube.com", "t.me", "discord.gg"}
        link_domain = registered_domain(url)
        if link_domain in social_domains or \
           any(link_domain.endswith("." + d) for d in social_domains):
            continue

        # Skip nav/utility paths
        skip_patterns = ["/about", "/contact", "/privacy", "/terms",
                         "/login", "/signin", "/signup", "/subscribe",
                         "/advertis", "/careers", "/cookie",
                         "/tag/", "/tags/", "/author/", "/category/", "/page/",
                         "#", "mailto:", "javascript:"]
        lowered = url.lower()
        if any(p in lowered for p in skip_patterns):
            continue

        if same_domain_only and page_domain and registered_domain(url) != page_domain:
            continue

        # Dedupe (query strings / trailing slashes vary)
        key = lowered.split("?")[0].rstrip("/")
        if key in seen or key == page_url.lower().rstrip("/"):
            continue
        seen.add(key)

        # Nav links have short labels ("Home", "Markets"); real article links
        # carry the headline. Keep it if the title is sentence-like OR the URL
        # itself looks like an article.
        looks_article = is_article_url(url)
        if len(title) < 15 and not looks_article:
            continue

        articles.append({"title": title, "url": url, "article_like": looks_article})

    # Article-like URLs first — they're the real content
    articles.sort(key=lambda a: a["article_like"], reverse=True)
    return articles


def content_hash(text: str) -> str:
    """Generate a hash for dedup."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


async def shutdown_crawler():
    """Clean shutdown of the browser (called from the app's post_shutdown hook)."""
    global _crawler
    async with _crawler_lock:
        crawler, _crawler = _crawler, None
    if crawler is not None:
        try:
            await crawler.close()
        except Exception:
            logger.exception("Crawler shutdown failed (browser may already be gone)")