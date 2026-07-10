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
    cmd_postsize,
    # Conversation handler (single natural interview loop)
    handle_interview,
    cancel_conversation,
    INTERVIEW,
    # Inline button router
    handle_callback,
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

async def cron_news_cycle(context):
    """The one cron: poll sources → gate → post."""
    from pipeline.news_cycle import run_news_cycle
    logger.info("CRON: News cycle running...")
    try:
        result = await run_news_cycle()
        if result.get("skipped"):
            return
        logger.info("CRON: News cycle done — posted %d of %d candidates",
                    result["posted"], result["candidates"])
    except Exception:
        logger.exception("CRON news cycle error")


async def cron_health_check(context):
    """Cron: re-test sidelined sources and bring the healthy ones back.

    Covers both 'error' (repeated fetch failures) and 'blocked' (the crawler was
    refused). Neither is set by the user — they only ever mean "we couldn't read
    it that day", and a site that rate-limited us during research is usually
    perfectly readable later. Leaving 'blocked' out would strand it forever.
    """
    from database import store
    from crawler.fetcher import test_source
    logger.info("CRON: Health check...")

    from database.models import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, url, feed_url FROM sources "
        "WHERE fetch_status IN ('error', 'blocked')"
    ).fetchall()
    conn.close()

    reactivated = 0
    for row in rows:
        # Test the page the pipeline actually crawls, one at a time.
        result = await test_source(row["feed_url"] or row["url"])
        if result["fetchable"]:
            store.reactivate_source(row["id"])
            reactivated += 1
        await asyncio.sleep(1)

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
            INTERVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interview)],
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
    app.add_handler(CommandHandler("postsize", cmd_postsize))
    # Inline keyboards are inert without this.
    app.add_handler(CallbackQueryHandler(handle_callback))
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

    # The one news cycle: poll sources → gate → post
    job_queue.run_repeating(
        cron_news_cycle,
        interval=config.NEWS_CYCLE_MINUTES * 60,
        first=120,  # let the bot settle before the first poll
        name="news_cycle",
    )

    # Health check every 24 hours
    job_queue.run_repeating(
        cron_health_check,
        interval=config.HEALTH_CHECK_INTERVAL_HOURS * 3600,
        first=config.HEALTH_CHECK_INTERVAL_HOURS * 3600,
        name="health_check",
    )

    logger.info("Scheduler configured: news_cycle=%dmin (max %d/source, %d posts/cycle), health=%dh",
                config.NEWS_CYCLE_MINUTES, config.MAX_NEW_PER_SOURCE,
                config.MAX_POSTS_PER_CYCLE, config.HEALTH_CHECK_INTERVAL_HOURS)


def main():
    """Initialize and run the bot + scheduler."""
    # Initialize database
    logger.info("Initializing database...")
    init_db()

    # A restart during research strands that stream in 'researching' forever.
    from database import store
    freed = store.reset_stuck_research()
    if freed:
        logger.warning("Reset %d stream(s) stranded mid-research — "
                       "re-run /research <id> to populate them", freed)

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
        f"""\
# 🟢 NewsStream Bot Online

The research engine and news cycle are running.

I check every source's article list every **{config.NEWS_CYCLE_MINUTES} minutes**. \
A newly added source is baselined on its first check — I record what's already \
published and only send you what appears *after* that.

Use `/newstream` to create your first news stream.\
""",
    )

    # Run the bot (webhook mode, served behind nginx at bot.simple-flow.co)
    logger.info("Starting webhook: %s (listening on %s:%d)",
                config.WEBHOOK_URL, config.LISTEN_HOST, config.LISTEN_PORT)
    app.run_webhook(
        listen=config.LISTEN_HOST,
        port=config.LISTEN_PORT,
        url_path=config.WEBHOOK_PATH,
        webhook_url=config.WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()