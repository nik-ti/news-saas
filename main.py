"""
Main entry point — starts the Telegram bot and the APScheduler for cron jobs.
"""
import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import config
from database.models import init_db
from bot.handlers import (
    # Commands
    cmd_start,
    cmd_newstream,
    cmd_streams,
    cmd_sources,
    cmd_sources_all,
    cmd_addsource,
    cmd_deletesource,
    cmd_testsource,
    cmd_research,
    cmd_latest,
    cmd_runpipeline,
    cmd_status,
    # Conversation handlers
    handle_topic,
    handle_strictness,
    handle_exclusions,
    handle_followups,
    cancel_conversation,
    TOPIC,
    STRICTNESS,
    EXCLUSIONS,
    FOLLOWUPS,
)
from bot.messaging import send_rich

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Suppress noisy HTTP logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Cron job wrappers (run async pipeline in the bot's event loop)
# ═══════════════════════════════════════════════════════════════════════════════

async def cron_stream_post(context):
    """Cron: fetch articles → write posts → send to Telegram immediately."""
    from pipeline.stream_poster import process_and_post_articles
    logger.info("CRON: Stream poster running...")
    try:
        result = await process_and_post_articles()
        logger.info("CRON: Stream poster done — fetched %d, posted %d",
                    result["fetched"], result["posted"])
    except Exception as e:
        logger.error("CRON stream poster error: %s", e)


async def cron_fetch_news(context):
    """Cron: fetch news from all active sources (legacy, for digest mode)."""
    from pipeline.fetch_news import fetch_all_news
    logger.info("CRON: Fetching news...")
    try:
        result = await fetch_all_news()
        logger.info("CRON: Fetch complete — %d new articles", result["total_new"])
    except Exception as e:
        logger.error("CRON fetch error: %s", e)


async def cron_process_articles(context):
    """Cron: summarize and score new articles."""
    from pipeline.summarize import process_new_articles
    logger.info("CRON: Processing articles...")
    try:
        result = await process_new_articles()
        logger.info("CRON: Processed %d articles (%d relevant)",
                    result["processed"], result["relevant"])
    except Exception as e:
        logger.error("CRON process error: %s", e)


async def cron_deliver_digest(context):
    """Cron: deliver compiled digest."""
    from pipeline.deliver import deliver_digest_async
    logger.info("CRON: Delivering digest...")
    try:
        result = await deliver_digest_async(config.TELEGRAM_CHAT_ID)
        logger.info("CRON: Delivered %d articles", result["delivered"])
    except Exception as e:
        logger.error("CRON deliver error: %s", e)


async def cron_health_check(context):
    """Cron: re-test blocked/error sources."""
    from database import store
    from crawler.fetcher import test_source
    logger.info("CRON: Health check...")

    from database.models import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, url, feed_url FROM sources WHERE fetch_status IN ('blocked', 'error')"
    ).fetchall()
    conn.close()

    reactivated = 0
    for row in rows:
        # Test the page the pipeline actually crawls
        result = await test_source(row["feed_url"] or row["url"])
        if result["fetchable"]:
            store.reset_fail_count(row["id"])  # sets status back to active
            reactivated += 1

    if reactivated > 0:
        send_rich(config.TELEGRAM_CHAT_ID,
                  f"🔧 **Health Check:** Reactivated {reactivated} source(s).")

    logger.info("CRON: Health check — reactivated %d/%d", reactivated, len(rows))


# ═══════════════════════════════════════════════════════════════════════════════
# Application factory
# ═══════════════════════════════════════════════════════════════════════════════

def build_application() -> Application:
    """Build and configure the Telegram bot application."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # ── Conversation handler for /newstream ───────────────────────────────
    newstream_conv = ConversationHandler(
        entry_points=[CommandHandler("newstream", cmd_newstream)],
        states={
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic)],
            STRICTNESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_strictness)],
            EXCLUSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_exclusions)],
            FOLLOWUPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_followups)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )

    # ── Error handler ─────────────────────────────────────────────────────
    async def error_handler(update: object, context) -> None:
        logger.error("Unhandled exception: %s", context.error)

    # ── Register all command handlers ─────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(newstream_conv)
    app.add_handler(CommandHandler("streams", cmd_streams))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("sources_all", cmd_sources_all))
    app.add_handler(CommandHandler("addsource", cmd_addsource))
    app.add_handler(CommandHandler("deletesource", cmd_deletesource))
    app.add_handler(CommandHandler("testsource", cmd_testsource))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("runpipeline", cmd_runpipeline))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_error_handler(error_handler)

    return app


def setup_scheduler(app: Application) -> None:
    """Configure APScheduler cron jobs within the PTB application."""
    from telegram.ext import CallbackContext

    job_queue = app.job_queue

    if job_queue is None:
        logger.warning("JobQueue not available — cron jobs disabled. "
                       "Install with: pip install 'python-telegram-bot[job-queue]'")
        return

    # Real-time article posting every N minutes
    job_queue.run_repeating(
        cron_stream_post,
        interval=config.STREAM_POST_INTERVAL_MINUTES * 60,
        first=30,  # start 30 seconds after boot
        name="stream_post",
    )

    # Fetch news every N minutes (legacy)
    job_queue.run_repeating(
        cron_fetch_news,
        interval=config.FETCH_INTERVAL_MINUTES * 60,
        first=config.FETCH_INTERVAL_MINUTES * 60,
        name="fetch_news",
    )

    # Process articles every N minutes
    job_queue.run_repeating(
        cron_process_articles,
        interval=config.PROCESS_INTERVAL_MINUTES * 60,
        first=config.PROCESS_INTERVAL_MINUTES * 60,
        name="process_articles",
    )

    # Deliver digest every N hours
    job_queue.run_repeating(
        cron_deliver_digest,
        interval=config.DELIVER_INTERVAL_HOURS * 3600,
        first=config.DELIVER_INTERVAL_HOURS * 3600,
        name="deliver_digest",
    )

    # Health check every 24 hours
    job_queue.run_repeating(
        cron_health_check,
        interval=config.HEALTH_CHECK_INTERVAL_HOURS * 3600,
        first=config.HEALTH_CHECK_INTERVAL_HOURS * 3600,
        name="health_check",
    )

    logger.info("Scheduler configured: stream_post=%dmin, fetch=%dmin, process=%dmin, deliver=%dh, health=%dh",
                config.STREAM_POST_INTERVAL_MINUTES, config.FETCH_INTERVAL_MINUTES,
                config.PROCESS_INTERVAL_MINUTES, config.DELIVER_INTERVAL_HOURS,
                config.HEALTH_CHECK_INTERVAL_HOURS)


def main():
    """Initialize and run the bot + scheduler."""
    # Initialize database
    logger.info("Initializing database...")
    init_db()

    # Build application
    logger.info("Building Telegram bot application...")
    app = build_application()

    # Setup scheduler
    logger.info("Setting up cron jobs...")
    setup_scheduler(app)

    # Send startup message
    logger.info("Starting bot...")
    send_rich(
        config.TELEGRAM_CHAT_ID,
        """\
# 🟢 NewsStream Bot Online

The research engine and pipeline are running.

## Active Cron Jobs

| Job | Interval |
|-----|----------|
| **Stream Post** | **Every 15 min** |
| Fetch News | Every 30 min |
| Process Articles | Every 60 min |
| Deliver Digest | Every 6 hours |
| Health Check | Every 24 hours |

Use `/newstream` to create your first news stream.\
""",
    )

    # Run the bot
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()