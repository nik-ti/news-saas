"""
Pipeline — Summarize.
Fetches an article's page and condenses it to a few sentences, which is what
both the relevance gate and the post writer consume.
"""
import logging

import config
from crawler.fetcher import fetch_page
from research.llm import chat_json

logger = logging.getLogger(__name__)

SKIP = "SKIP"

SYSTEM_PROMPT_SUMMARIZE = """\
You are a news summarizer. Given an article title and page content, produce a \
concise 2-4 sentence summary that captures the key information: what happened, \
who it affects, and when.

Preserve the source's certainty. "Proposed" is not "approved"; "could" is not "will".

If the content is not a news article — a paywall, a login page, a navigation or \
category listing, a cookie notice, or an empty page — output exactly:
{"summary": "SKIP"}

Otherwise output JSON: {"summary": "<2-4 sentence summary>"}
No markdown, no explanation."""


async def summarize_article(article: dict) -> tuple[str, str]:
    """
    Summarize one article. Returns (summary, title).

    `summary` is SKIP when the page isn't a usable news article.
    `title` may be corrected from the page's own <title> — link text on feed
    pages is often card text (category + date + headline + snippet concatenated).
    """
    title = (article.get("title") or "").strip()
    url = article.get("url", "")

    # Inline-extracted items (changelog-style sources) already carry a summary,
    # and their url points at the feed page, so re-fetching would be wrong.
    existing = (article.get("summary") or "").strip()
    if existing:
        return existing[:config.SUMMARY_CHAR_CAP], title

    page = await fetch_page(url)
    if not page["success"] or not page["content"]:
        logger.info("Can't fetch article %s: %s", url, page.get("error", "no content"))
        return SKIP, title

    if page["title"] and len(title) > 80:
        title = page["title"].strip()[:200]

    result = await chat_json(
        SYSTEM_PROMPT_SUMMARIZE,
        f"Title: {title}\n\nContent:\n{page['content'][:3000]}\n\nSummarize this article.",
    )
    summary = (result.get("summary") or "").strip()

    if not summary:
        return SKIP, title
    return summary[:config.SUMMARY_CHAR_CAP], title
