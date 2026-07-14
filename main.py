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
    PicklePersistence,
    filters,
)

import config
from database.models import init_db
from bot.handlers import (
    # Commands
    cmd_start,
    cmd_help,
    cmd_menu,
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
    cmd_language,
    cmd_pausestream,
    cmd_resumestream,
    cmd_deletestream,
    cmd_quiet,
    # Conversation handler (single natural interview loop)
    handle_interview,
    cancel_conversation,
    handle_pending_source,
    INTERVIEW,
    # Inline button router
    handle_callback,
)
from bot.messaging import send_rich, send_rich_async

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
    # validate_source is RSS-aware: raw XML fed to the browser can false-flag a
    # perfectly healthy feed as blocked, stranding it in 'error' forever.
    from research.validator import validate_source
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
        result = await validate_source(row["feed_url"] or row["url"])
        if result["fetchable"]:
            store.reactivate_source(row["id"])
            reactivated += 1
        await asyncio.sleep(1)

    if reactivated > 0:
        await send_rich_async(config.TELEGRAM_CHAT_ID,
                              f"🔧 **Health Check:** Reactivated {reactivated} source(s).")

    logger.info("CRON: Health check — reactivated %d/%d", reactivated, len(rows))


async def startup_selfcheck(context):
    """
    Ping every external dependency once at boot and tell the admin what's broken.

    A wrong model name, an expired search key, or a dead embeddings endpoint
    otherwise degrades silently — research 'finds nothing' and nobody knows why.
    """
    import config as cfg
    from research.llm import chat
    from research.embeddings import embed
    from research.discovery import _brave_search

    problems = []

    try:
        await chat("Reply with the word OK.", "ping")
    except Exception as e:
        problems.append(f"❌ fast LLM (`{cfg.LLM_MODEL_FAST}`): {e}")
    try:
        await chat("Reply with the word OK.", "ping", model="post")
    except Exception as e:
        problems.append(f"❌ post LLM (`{cfg.LLM_MODEL_POST}`): {e}")

    if await embed("selfcheck") is None:
        problems.append("⚠️ embeddings endpoint failing — semantic source DB inactive")

    brave = await _brave_search("news", count=1)
    if brave is None:
        problems.append("❌ Brave Search errored on a trivial query — check key/quota")
    elif not brave:
        problems.append("⚠️ Brave Search returned zero results for 'news'")

    if problems:
        logger.error("Startup self-check found problems: %s", problems)
        await send_rich_async(config.ADMIN_USER_ID,
                              "# 🩺 Startup self-check\n\n" + "\n".join(problems))
    else:
        logger.info("Startup self-check: all external dependencies OK")


# ═══════════════════════════════════════════════════════════════════════════════
# Application factory
# ═══════════════════════════════════════════════════════════════════════════════

async def _post_shutdown(app: Application) -> None:
    """Close the shared headless browser so restarts don't orphan Chromium."""
    from crawler.fetcher import shutdown_crawler
    await shutdown_crawler()


# The commands Telegram shows in the "/" hint list and the chat Menu button.
# Localized so Russian clients see Russian descriptions.
_MENU_COMMANDS = {
    "en": [
        ("newstream", "Set up a news stream"),
        ("menu", "Open the button menu"),
        ("streams", "My streams"),
        ("latest", "Latest articles"),
        ("language", "Bot / post language"),
        ("help", "All commands"),
    ],
    "ru": [
        ("newstream", "Создать новостной поток"),
        ("menu", "Открыть меню с кнопками"),
        ("streams", "Мои потоки"),
        ("latest", "Последние статьи"),
        ("language", "Язык бота и постов"),
        ("help", "Все команды"),
    ],
}


async def _post_init(app: Application) -> None:
    """Register the native command menu (the '/' list + Menu button)."""
    from telegram import BotCommand, MenuButtonCommands
    try:
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in _MENU_COMMANDS["en"]])
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in _MENU_COMMANDS["ru"]],
            language_code="ru")
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Registered bot command menu (en, ru)")
    except Exception:
        logger.exception("Setting the command menu failed (non-fatal)")


def build_application() -> Application:
    """Build and configure the Telegram bot application."""
    # §3.10: user_data (the running interview transcript) survives restarts —
    # a deploy mid-interview no longer silently eats the conversation.
    import os
    persistence = PicklePersistence(
        filepath=os.path.join(os.path.dirname(config.DB_PATH), "bot_state.pickle")
    )

    app = (Application.builder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .persistence(persistence)
           .post_init(_post_init)
           .post_shutdown(_post_shutdown)
           .build())

    # ── Conversation handler for /newstream ───────────────────────────────
    # The menu's "New stream" button is a second entry point, so the button
    # starts the same interview the command does.
    newstream_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newstream", cmd_newstream),
            CallbackQueryHandler(cmd_newstream, pattern="^menu:newstream$"),
        ],
        states={
            INTERVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interview)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
        name="newstream_interview",
        persistent=True,
    )

    # ── Error handler ─────────────────────────────────────────────────────
    async def error_handler(update: object, context) -> None:
        logger.error("Unhandled exception: %s", context.error)

    # ── Register all command handlers ─────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
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
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("pausestream", cmd_pausestream))
    app.add_handler(CommandHandler("resumestream", cmd_resumestream))
    app.add_handler(CommandHandler("deletestream", cmd_deletestream))
    app.add_handler(CommandHandler("quiet", cmd_quiet))
    # A plain message after tapping the menu's "Add source" is the site URL.
    # Registered after the interview conversation so it only fires when no
    # conversation is active; it no-ops unless the menu armed it.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handle_pending_source))
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

    # Nightly retention (§2.3): the articles table grew unboundedly and
    # nothing ever read the dead rows again.
    async def cron_retention(context):
        from database import store
        try:
            n = store.prune_old_articles(config.RETENTION_DAYS)
            if n:
                logger.info("Retention: pruned %d article(s) older than %d days",
                            n, config.RETENTION_DAYS)
        except Exception:
            logger.exception("Retention job failed")

    job_queue.run_repeating(cron_retention, interval=24 * 3600, first=3600,
                            name="retention")

    # Nightly feedback fold (§3.7): 👍/👎 + gate pass-rate → quality_score.
    from pipeline.feedback import cron_score_decay
    job_queue.run_repeating(cron_score_decay, interval=24 * 3600, first=2 * 3600,
                            name="score_decay")

    # One-shot dependency self-check shortly after boot
    job_queue.run_once(startup_selfcheck, when=15, name="startup_selfcheck")

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