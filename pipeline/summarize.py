"""
Pipeline — Summarize & Relevance Check.
Processes new articles: summarizes and scores relevance in parallel.
"""
import asyncio
import logging

from database import store
from database.models import get_connection
from research.llm import chat_json
from crawler.fetcher import fetch_page

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_SUMMARIZE = """\
You are a news summarizer. Given an article title and content, produce a \
concise 2-3 sentence summary that captures the key information.

Output JSON: {"summary": "<2-3 sentence summary>"}
No markdown, no explanation."""


SYSTEM_PROMPT_RELEVANCE = """\
You are a relevance evaluator for a personalised news service.

Given:
1. A Source Criteria Profile (what the user wants)
2. An article title and summary

Score how relevant this article is to the user's interests on a scale of 0.0 to 1.0:
- 1.0: Perfectly on-topic, covers the specific sub-topics
- 0.7-0.9: Clearly relevant
- 0.4-0.6: Tangentially relevant (broad domain but not specifics)
- 0.0-0.3: Not relevant

Also check if it matches any exclusion criteria.

Output JSON:
{
  "relevance_score": <0.0-1.0>,
  "reasoning": "<one sentence>",
  "is_relevant": true/false,
  "matches_exclusion": true/false
}
No markdown, no explanation."""


async def process_article(article: dict, profile: dict) -> dict:
    """
    Process a single article: fetch content, summarize, score relevance.
    """
    article_id = article["id"]
    title = article.get("title", "")
    url = article.get("url", "")

    # Step 1: Get a summary.
    # Inline-extracted items (changelog-style sources) already carry one —
    # their url points at the feed page, so re-fetching would be wrong anyway.
    summary = (article.get("summary") or "").strip()
    new_title = None
    if not summary:
        page = await fetch_page(url)
        if page["success"] and page["content"]:
            # Link-text "titles" are often card text (category+date+headline+snippet
            # concatenated). The page's own title is the real headline.
            if page["title"] and len(title) > 80:
                new_title = page["title"].strip()[:200]
                title = new_title
            result = await chat_json(
                SYSTEM_PROMPT_SUMMARIZE,
                f"Title: {title}\n\nContent:\n{page['content'][:3000]}\n\nSummarize this article.",
            )
            summary = result.get("summary", title)
        else:
            summary = title  # fallback to title only

    # Step 2: Relevance check
    import json
    profile_json = json.dumps(profile, indent=2)
    relevance = await chat_json(
        SYSTEM_PROMPT_RELEVANCE,
        f"## Source Criteria Profile\n{profile_json}\n\n"
        f"## Article\nTitle: {title}\nSummary: {summary}\n\n"
        f"Score relevance.",
    )

    score = relevance.get("relevance_score", 0.5)
    is_relevant = relevance.get("is_relevant", score >= 0.5)
    matches_exclusion = relevance.get("matches_exclusion", False)

    if matches_exclusion or not is_relevant:
        status = "irrelevant"
    else:
        status = "processed"

    store.update_article_status(article_id, status, relevance_score=score)

    # Update summary (and corrected title) in DB
    conn = get_connection()
    if new_title:
        conn.execute("UPDATE articles SET summary = ?, title = ? WHERE id = ?",
                     (summary, new_title, article_id))
    else:
        conn.execute("UPDATE articles SET summary = ? WHERE id = ?",
                     (summary, article_id))
    conn.commit()
    conn.close()

    return {
        "article_id": article_id,
        "title": title,
        "summary": summary,
        "relevance_score": score,
        "status": status,
    }


async def process_new_articles() -> dict:
    """
    Process all new articles: summarize + relevance check in parallel.
    """
    new_articles = store.get_new_articles()
    logger.info("Processing %d new articles...", len(new_articles))

    if not new_articles:
        return {"processed": 0, "relevant": 0, "irrelevant": 0}

    # Group articles by stream to get the right profile
    relevant_count = 0
    irrelevant_count = 0

    # Process in batches of 10 for LLM rate limits
    batch_size = 10
    for i in range(0, len(new_articles), batch_size):
        batch = new_articles[i : i + batch_size]
        tasks = []
        for article in batch:
            # Get the stream profile
            stream = store.get_stream(article["stream_id"])
            profile = stream["criteria"] if stream else {}
            tasks.append(process_article(article, profile))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("Article processing error: %s", result)
                continue
            if result["status"] == "processed":
                relevant_count += 1
            else:
                irrelevant_count += 1

    logger.info("Processing complete: %d relevant, %d irrelevant",
                relevant_count, irrelevant_count)
    return {
        "processed": len(new_articles),
        "relevant": relevant_count,
        "irrelevant": irrelevant_count,
    }