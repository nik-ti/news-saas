"""
Pipeline — Deliver Digest.
Sends compiled news digests to Telegram using rich messages.
"""
import logging
from datetime import datetime

import config
from database import store
from database.models import get_connection
from bot.messaging import send_rich, send_rich_async

logger = logging.getLogger(__name__)


def build_digest_markdown(articles: list[dict]) -> str:
    """Build a rich markdown digest from relevant articles."""
    if not articles:
        return "📭 *No new relevant articles since last digest.*\n"

    now = datetime.now().strftime("%b %d, %H:%M")
    parts = [f"# 📰 News Digest\n_{now}_\n"]

    # Group by source
    by_source = {}
    for art in articles:
        source_name = art.get("source_name", art.get("source_url", "Unknown"))
        by_source.setdefault(source_name, []).append(art)

    parts.append(f"**{len(articles)} articles** from **{len(by_source)} sources**\n")

    for source_name, arts in by_source.items():
        parts.append(f"\n## {source_name}\n")
        for art in arts[:3]:  # max 3 per source
            score = art.get("relevance_score", 0)
            score_emoji = "🔥" if score >= 0.8 else "✅" if score >= 0.5 else "ℹ️"
            title = art.get("title", "Untitled")
            url = art.get("url", "")
            summary = art.get("summary", "")

            parts.append(f"### {score_emoji} {title}")
            if summary:
                parts.append(f">{summary}\n")
            if url:
                parts.append(f"[Read →]({url})")
            parts.append("")

    return "\n".join(parts)


def _get_undelivered_articles() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.*, s.name as source_name, s.url as source_url FROM articles a
           JOIN sources s ON a.source_id = s.id
           WHERE a.status = 'processed' AND a.delivered_at IS NULL
           ORDER BY a.relevance_score DESC LIMIT 20"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def deliver_digest_sync(chat_id: int = None) -> dict:
    """
    Sync version: deliver digest of processed articles.
    Used by cron jobs.
    """
    chat_id = chat_id or config.TELEGRAM_CHAT_ID

    articles = _get_undelivered_articles()
    if not articles:
        logger.info("No articles to deliver")
        return {"delivered": 0}

    markdown = build_digest_markdown(articles)
    result = send_rich(chat_id, markdown)

    if result.get("ok"):
        article_ids = [a["id"] for a in articles]
        store.mark_articles_delivered(article_ids)
        logger.info("Digest delivered: %d articles", len(articles))
        return {"delivered": len(articles)}
    else:
        logger.error("Digest delivery failed: %s", result)
        return {"delivered": 0, "error": result}


async def deliver_digest_async(chat_id: int = None) -> dict:
    """Async version for use inside PTB handlers."""
    chat_id = chat_id or config.TELEGRAM_CHAT_ID

    articles = _get_undelivered_articles()
    if not articles:
        await send_rich_async(chat_id, "📭 No new articles to deliver yet.")
        return {"delivered": 0}

    markdown = build_digest_markdown(articles)
    result = await send_rich_async(chat_id, markdown)

    if result.get("ok"):
        article_ids = [a["id"] for a in articles]
        store.mark_articles_delivered(article_ids)
        return {"delivered": len(articles)}
    else:
        return {"delivered": 0, "error": result}