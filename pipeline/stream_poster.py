"""
Pipeline — Stream Poster (Real-time article posting).

Every 15 minutes:
  1. Fetch new articles from all active sources (reuses fetch_news)
  2. For each new article:
     a. Fetch full article content via crawler
     b. Write a short post via post_writer LLM
     c. Send immediately to Telegram
  3. Mark article as 'posted'

This replaces the old batch digest model with real-time per-article posting.
"""
import asyncio
import logging

import config
from database import store
from database.models import get_connection
from crawler.fetcher import fetch_page
from pipeline.fetch_news import fetch_all_news
from pipeline.post_writer import write_post
from bot.messaging import send_rich, send_rich_html_async

logger = logging.getLogger(__name__)


async def process_and_post_articles(chat_id: int = None) -> dict:
    """
    Main entry point for the real-time posting pipeline.
    1. Fetch new articles from all sources
    2. For each: fetch content → write post → send to Telegram
    Returns summary stats.
    """
    chat_id = chat_id or config.TELEGRAM_CHAT_ID

    # Step 1: Fetch new articles
    logger.info("Stream poster: fetching new articles...")
    fetch_result = await fetch_all_news()
    new_count = fetch_result["total_new"]

    if new_count == 0:
        logger.info("Stream poster: no new articles")
        return {"fetched": 0, "posted": 0, "errors": 0}

    logger.info("Stream poster: %d new articles to process", new_count)

    # Step 2: Get all unposted articles
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.*, s.name as source_name, s.url as source_url, s.stream_id
           FROM articles a
           JOIN sources s ON a.source_id = s.id
           WHERE a.status = 'new'
           ORDER BY a.fetched_at ASC"""
    ).fetchall()
    conn.close()

    articles = [dict(r) for r in rows]
    posted = 0
    errors = 0

    # Step 3: Process each article sequentially (avoid flooding Telegram)
    for article in articles:
        try:
            success = await _process_single_article(article, chat_id)
            if success:
                posted += 1
                # Small delay between posts to avoid rate limiting
                await asyncio.sleep(2)
            else:
                errors += 1
        except Exception as e:
            logger.error("Error processing article %d: %s", article["id"], e)
            # Mark as irrelevant so it doesn't get retried forever
            store.update_article_status(article["id"], "irrelevant")
            errors += 1

    logger.info("Stream poster: posted %d/%d articles (%d errors)",
                posted, new_count, errors)

    return {"fetched": new_count, "posted": posted, "errors": errors}


async def _process_single_article(article: dict, chat_id: int) -> bool:
    """
    Process a single article: fetch content → write post → send to Telegram.
    Returns True if posted successfully.
    """
    article_id = article["id"]
    article_url = article.get("url", "")
    article_title = article.get("title", "")
    source_name = article.get("source_name", "")

    # If article already has a summary (from RSS), use it directly
    # Otherwise, fetch the full article content
    if article.get("summary") and len(article["summary"]) > 100:
        article_text = f"Title: {article_title}\n\n{article['summary']}"
    else:
        # Fetch full article content
        page = await fetch_page(article_url)
        if not page["success"] or not page["content"]:
            logger.warning("Can't fetch article %s: %s", article_url,
                           page.get("error", "no content"))
            store.update_article_status(article_id, "irrelevant")
            return False

        article_text = f"Title: {article_title}\n\n{page['content'][:3000]}"

    # Write the post
    post_html = await write_post(article_text, source_url=article_url)

    if not post_html or len(post_html) < 20:
        logger.warning("Post writer returned empty for article %d", article_id)
        store.update_article_status(article_id, "irrelevant")
        return False

    # Send to Telegram as raw HTML (post_writer outputs HTML, not markdown)
    result = await send_rich_html_async(chat_id, post_html)

    if result.get("ok"):
        # Mark as posted
        store.update_article_status(article_id, "posted")
        logger.info("Posted article %d: %s", article_id, article_title[:60])
        return True
    else:
        logger.error("Failed to send post for article %d: %s", article_id, result)
        return False