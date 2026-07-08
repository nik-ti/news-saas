"""
Phase 3 — Source Qualification (Optimized for Speed).

Two-stage funnel:
  Stage 1 (FAST): Fetch ALL candidate homepages in parallel → batch LLM pre-filter.
  Stage 2 (DEEP): Top candidates get full treatment — article fetching, deep LLM eval,
                  AND identification of the correct feed_url (article list page).

This reduces total time from 10+ minutes to ~2-5 minutes for 60 candidates.
"""
import asyncio
import json
import logging
from typing import Optional
from urllib.parse import urlparse

import config
from crawler.fetcher import fetch_multiple, extract_article_links
from research.llm import chat_json
from research.urlutils import is_article_url, derive_source_url, registered_domain

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_BATCH_PREFILTER = """\
You are a fast source qualification agent. You are given multiple candidate sources \
at once. For each one, quickly assess whether it's worth deeper investigation based \
on the homepage content and the Source Criteria Profile.

For each source, output:
- id: the source number
- score: 0-100 (how likely this is a good match)
- verdict: "investigate" (worth deep checking) or "skip" (clearly irrelevant)

Be generous in Stage 1 — keep anything remotely relevant for Stage 2. \
Only skip sources that are clearly about a different topic, are spam, \
or have no readable content.

Output JSON: {"results": [{"id": 1, "score": 85, "verdict": "investigate"}, ...]}"""

SYSTEM_PROMPT_DEEP_QUALIFY = """\
You are a strict source qualification agent for a news aggregation service.

You are given:
1. A Source Criteria Profile (what the user wants)
2. Content from a candidate source (homepage + recent article excerpts)

Determine whether this source TRULY covers what the user wants. \
Do not be satisfied by bare-minimum relevance.

CRITICAL: You must also identify the correct "feed_url" — the page on this site \
that lists the most recent articles/news. This is the page we will crawl regularly \
to fetch new articles. Common patterns:
- /news, /blog, /articles, /feed, /latest
- A category-specific page like /crypto/news, /tech/news
- The homepage itself if it already shows recent articles
- NEVER a specific article page (e.g. /blog/specific-article-title)
- NEVER a tag page, author page, or single PDF

Output valid JSON:
{
  "covers_topic": true/false,
  "primary_focus": "<what this source mainly covers>",
  "matches_specific_topics": ["<specific topics from the profile it covers>"],
  "quality_assessment": "<'high', 'medium', or 'low'>",
  "frequency": "<'daily', 'weekly', 'monthly', 'rare'>",
  "match_score": <0-100 integer>,
  "evidence": ["<specific article titles or content snippets as proof>"],
  "recommendation": "<'accept', 'reject', or 'borderline'>",
  "source_name": "<the name of the site/publication>",
  "broad_category": "<the broad news category this belongs to>",
  "specific_keywords": ["<3-5 keywords describing what this source focuses on>"],
  "description": "<1-2 sentence description of this source>",
  "feed_url": "<the full URL of the article list/feed page we should crawl>"
}

Scoring: 90-100 perfect, 70-89 strong, 50-69 partial, 0-49 poor.
Be strict. No evidence = reject."""


async def qualify_all(candidates: list[str], profile: dict,
                      progress_callback=None) -> list[dict]:
    """
    Two-stage qualification pipeline.
    Stage 1: Quick parallel homepage fetch + batch LLM pre-filter.
    Stage 2: Deep qualification on survivors only — includes feed_url identification.
    """
    total = len(candidates)
    logger.info("Qualifying %d candidates (2-stage funnel)...", total)

    if total == 0:
        return []

    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 1: Fast pre-filter — fetch homepages + batch LLM
    # ═══════════════════════════════════════════════════════════════════════
    if progress_callback:
        await progress_callback(0, total)

    logger.info("Stage 1: Fetching %d homepages in parallel...", total)

    # Fetch ALL homepages in parallel (high concurrency for this stage)
    homepage_results = await fetch_multiple(candidates)

    # Build source summaries for batch LLM
    sources_for_llm = []
    url_to_index = {}
    for i, (url, result) in enumerate(zip(candidates, homepage_results), 1):
        if result["success"] and result["content"]:
            sources_for_llm.append({
                "id": i,
                "url": url,
                "title": result["title"][:100],
                "content_preview": result["content"][:800],
            })
            url_to_index[i] = url

    logger.info("Stage 1: %d/%d homepages fetched successfully",
                len(sources_for_llm), total)

    if progress_callback:
        await progress_callback(total, total)  # stage 1 fetching done

    if not sources_for_llm:
        logger.warning("No homepages could be fetched")
        return []

    # Batch LLM pre-filter — process in chunks of 15 to stay within token limits
    logger.info("Stage 1: Batch LLM pre-filtering %d sources...", len(sources_for_llm))

    promising = {}  # id -> score
    chunk_size = 15

    for chunk_start in range(0, len(sources_for_llm), chunk_size):
        chunk = sources_for_llm[chunk_start:chunk_start + chunk_size]
        profile_json = json.dumps(profile, indent=2)
        sources_json = json.dumps(chunk, indent=2)

        result = await chat_json(
            SYSTEM_PROMPT_BATCH_PREFILTER,
            f"## Source Criteria Profile\n{profile_json}\n\n"
            f"## Candidate Sources ({len(chunk)})\n{sources_json}\n\n"
            f"Evaluate each source. Output JSON.",
        )

        for r in result.get("results", []):
            src_id = r.get("id")
            score = r.get("score", 0)
            verdict = r.get("verdict", "skip")
            if verdict == "investigate" and src_id in url_to_index:
                promising[src_id] = score

    # Sort by score, take top candidates for deep dive
    sorted_promising = sorted(promising.items(), key=lambda x: x[1], reverse=True)
    # Keep top 15 or all if fewer
    deep_candidates = sorted_promising[:15]

    logger.info("Stage 1 complete: %d sources selected for deep qualification",
                len(deep_candidates))

    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 2: Deep qualification on survivors
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("Stage 2: Deep qualification on %d sources...", len(deep_candidates))

    # Get the homepage results we already fetched (reuse, don't re-fetch)
    deep_tasks = []
    for src_id, pre_score in deep_candidates:
        url = url_to_index[src_id]
        homepage = homepage_results[src_id - 1]  # 0-indexed
        deep_tasks.append(_deep_qualify_single(url, profile, homepage))

    deep_results = await asyncio.gather(*deep_tasks, return_exceptions=True)

    # Filter by threshold and sort
    threshold = config.QUALIFICATION_SCORE_THRESHOLD
    if profile.get("strictness") == "high":
        threshold = 75
    elif profile.get("strictness") == "low":
        threshold = 50

    qualified = []
    for result in deep_results:
        if isinstance(result, Exception):
            logger.error("Deep qualification error: %s", result)
            continue
        if result is None:
            continue
        if result.get("match_score", 0) >= threshold and \
           result.get("recommendation") in ("accept", "borderline"):
            qualified.append(result)

    qualified.sort(key=lambda x: x.get("match_score", 0), reverse=True)

    logger.info("Stage 2 complete: %d/%d sources qualified (threshold=%d)",
                len(qualified), len(deep_candidates), threshold)
    return qualified


async def _deep_qualify_single(url: str, profile: dict, homepage: dict) -> Optional[dict]:
    """
    Deep qualification of a single source.
    Fetches recent articles and does a thorough LLM evaluation.
    Also identifies the feed_url (article list page).
    Reuses the already-fetched homepage to avoid re-crawling.
    """
    try:
        if not homepage["success"]:
            return None

        # Extract article links from homepage — prefer plausible articles
        article_links = extract_article_links(homepage, base_url=url,
                                              same_domain_only=True)
        plausible = [a for a in article_links
                     if a.get("article_like") or len(a["title"]) >= 40]
        article_urls = [a["url"] for a in (plausible or article_links)[:config.ARTICLES_TO_EXAMINE]]

        # Fetch articles in parallel
        article_contents = []
        if article_urls:
            articles = await fetch_multiple(article_urls)
            for art in articles:
                if art["success"] and art["content"]:
                    article_contents.append({
                        "title": art["title"],
                        "url": art["url"],
                        "excerpt": art["content"][:1500],
                    })

        # LLM evaluation — includes feed_url identification
        profile_json = json.dumps(profile, indent=2)
        content_for_llm = _build_evaluation_content(url, homepage, article_contents)

        result = await chat_json(
            SYSTEM_PROMPT_DEEP_QUALIFY,
            f"## Source Criteria Profile\n\n{profile_json}\n\n"
            f"## Candidate Source Content\n\n{content_for_llm}\n\n"
            f"Evaluate this source and identify its feed_url. Output JSON.",
            smart=True,
        )

        if not result:
            logger.warning("LLM returned empty qualification for %s", url)
            return None

        # Validate / sanitize feed_url — fall back to the original URL if missing/invalid
        raw_feed = result.get("feed_url")
        if isinstance(raw_feed, str):
            feed_url = raw_feed.strip()
        else:
            feed_url = ""
        if not feed_url:
            feed_url = url
        elif not feed_url.startswith("http"):
            # Make it absolute
            parsed = urlparse(url)
            if feed_url.startswith("/"):
                feed_url = f"{parsed.scheme}://{parsed.netloc}{feed_url}"
            else:
                feed_url = url  # invalid, fall back

        # Deterministic guards (never trust the LLM alone):
        # feed_url must stay on the same domain and must not be a single article
        if registered_domain(feed_url) != registered_domain(url):
            logger.warning("feed_url %s is off-domain for %s — falling back", feed_url, url)
            feed_url = url
        elif is_article_url(feed_url):
            logger.warning("feed_url %s looks like an article page — collapsing", feed_url)
            feed_url = derive_source_url(feed_url)

        result["feed_url"] = feed_url
        result["url"] = url
        result["articles_examined"] = len(article_contents)
        return result

    except Exception as e:
        logger.error("Deep qualification failed for %s: %s", url, e)
        return None


def _build_evaluation_content(url: str, homepage: dict,
                               articles: list[dict]) -> str:
    """Build the text payload for the LLM evaluation."""
    parts = [f"**Source URL:** {url}"]

    if homepage["title"]:
        parts.append(f"**Site Title:** {homepage['title']}")

    parts.append(f"\n**Homepage Content (first 2000 chars):**\n{homepage['content'][:2000]}")

    # Include the links found on the page so the LLM can identify the feed_url
    article_links = extract_article_links(homepage, base_url=url)
    if article_links:
        parts.append(f"\n**Links found on this page ({len(article_links)} shown):**")
        for link in article_links[:20]:
            parts.append(f"- [{link['title'][:60]}]({link['url']})")

    if articles:
        parts.append(f"\n**Recent Articles Examined ({len(articles)}):**")
        for i, art in enumerate(articles, 1):
            parts.append(f"\n### Article {i}: {art['title']}")
            parts.append(f"URL: {art['url']}")
            parts.append(f"Excerpt:\n{art['excerpt'][:1000]}")
    else:
        parts.append("\n*No individual articles could be extracted. "
                      "Evaluate based on homepage content only.*")

    return "\n".join(parts)