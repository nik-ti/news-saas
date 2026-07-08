"""
Phase 2 — Candidate Discovery.
Uses Brave Search API directly (httpx) for parallel web searches.
"""
import asyncio
import logging
from urllib.parse import urlparse

import httpx

import config
from research.llm import chat_json
from research.urlutils import derive_source_url, registered_domain, path_segments

logger = logging.getLogger(__name__)

# ── Concurrency control for Brave Search ─────────────────────────────────────
_search_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SEARCHES)

SYSTEM_PROMPT_QUERIES = """\
You are a search strategist for a news source discovery system.
Given a Source Criteria Profile, generate varied search queries that would \
help find websites and news sources covering these specific topics.

Generate exactly {n} search queries. Each query should approach the topic from \
a different angle:
- Some should be specific (exact sub-topic names)
- Some should be broader (to find general sites that cover this area)
- Some should include terms like "news", "analysis", "blog", "update"
- Vary phrasings and synonyms

Output ONLY a JSON object: {{"queries": ["query1", "query2", ...]}}
No markdown, no explanation."""


async def generate_search_queries(profile: dict, n: int = None) -> list[str]:
    """
    Generate varied search queries from the criteria profile using the LLM.
    """
    n = n or config.MAX_SEARCH_QUERIES
    prompt = SYSTEM_PROMPT_QUERIES.format(n=n)
    import json
    profile_text = json.dumps(profile, indent=2)

    result = await chat_json(
        prompt,
        f"Source Criteria Profile:\n\n{profile_text}\n\n"
        f"Generate {n} varied search queries as JSON.",
    )

    queries = result.get("queries", [])
    if not queries:
        # Fallback: use keywords directly
        queries = profile.get("keywords", [profile.get("broad_domain", "news")])[:n]

    logger.info("Generated %d search queries", len(queries))
    return queries[:n]


async def _brave_search(query: str, count: int = None) -> list[dict]:
    """
    Execute a single Brave Search query using direct API.
    Returns list of {title, url, description}.
    """
    count = count or config.MAX_CANDIDATES_PER_QUERY
    async with _search_semaphore:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={
                        "X-Subscription-Token": config.BRAVE_SEARCH_API_KEY,
                        "Accept": "application/json",
                    },
                    params={"q": query, "count": count},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()

                # Brave API returns results under web.results
                raw_results = data.get("web", {}).get("results", [])

                candidates = []
                for r in raw_results:
                    url = r.get("url", "")
                    if not url:
                        continue
                    candidates.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "description": r.get("description", ""),
                    })

                return candidates
        except Exception as e:
            logger.error("Brave search failed for '%s': %s", query, e)
            return []


async def search_parallel(queries: list[str]) -> list[str]:
    """
    Run all search queries in parallel, merge and deduplicate candidate URLs.

    Search results are frequently individual articles (site.com/blog/post-title).
    Each result is collapsed to its source URL (section page or domain root)
    so we qualify PUBLICATIONS, never single articles. One candidate per domain
    (the shallowest URL wins) — the engine dedupes by domain later anyway,
    so doing it here saves an entire crawl per duplicate.
    """
    tasks = [_brave_search(q) for q in queries]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # domain -> best candidate URL (prefer shallowest path)
    by_domain: dict[str, str] = {}

    for result in all_results:
        if isinstance(result, Exception):
            continue
        for item in result:
            raw_url = item["url"]
            if not _is_valid_candidate(raw_url):
                continue
            source_url = derive_source_url(raw_url)
            domain = registered_domain(source_url)
            if not domain:
                continue
            existing = by_domain.get(domain)
            if existing is None or len(path_segments(source_url)) < len(path_segments(existing)):
                by_domain[domain] = source_url

    candidates = list(by_domain.values())
    logger.info("Discovery: %d unique source candidates from %d queries",
                len(candidates), len(queries))
    return candidates


# Skip social media, aggregators, etc. — matched as exact domain or subdomain
SKIP_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "tiktok.com", "reddit.com",
    "wikipedia.org", "google.com", "bing.com", "medium.com",
    "amazon.com", "play.google.com", "apps.apple.com",
    "quora.com", "pinterest.com", "github.com",
}


def _is_valid_candidate(url: str) -> bool:
    """Filter out obviously non-useful URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    netloc = parsed.netloc.lower().split(":")[0]

    # Exact domain or subdomain match (substring matching would make
    # "x.com" wrongly block e.g. netflix.com)
    for d in SKIP_DOMAINS:
        if netloc == d or netloc.endswith("." + d):
            return False

    # Skip if no proper TLD
    if "." not in netloc:
        return False

    return True