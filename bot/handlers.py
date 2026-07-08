"""
Telegram bot handlers — all commands and conversation flow.
Uses python-telegram-bot ConversationHandler for the multi-step /newstream flow.
"""
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import config
from database import store
from database.models import init_db, get_connection
from bot.messaging import send_rich_async, send_rich_html_async
from bot.keyboards import (
    stream_management_keyboard,
    source_management_keyboard,
    main_menu_keyboard,
)
from research.engine import run_research
from research.profile_builder import generate_followup_questions, PREMADE_QUESTIONS
from crawler.fetcher import test_source
from pipeline.fetch_news import fetch_all_news
from pipeline.summarize import process_new_articles
from pipeline.deliver import deliver_digest_async

logger = logging.getLogger(__name__)

# ── Conversation states for /newstream ───────────────────────────────────────
TOPIC, STRICTNESS, EXCLUSIONS, FOLLOWUPS, RESEARCHING = range(5)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /start
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with rich command table."""
    await send_rich_async(update.effective_chat.id, """\
# 🗞️ NewsStream Bot

*Your autonomous news research engine.*

Tell me what you want, and I'll find the best sources, monitor them, and \
deliver relevant news directly to you.

## Commands

| Command | Description |
|---------|-------------|
| `/newstream` | Create a new news stream (guided Q&A) |
| `/streams` | List all your news streams |
| `/sources <stream_id>` | View sources for a stream |
| `/sources_all` | View the entire source database |
| `/addsource <stream_id> <url>` | Manually add a source |
| `/deletesource <source_id>` | Delete a source |
| `/testsource <url>` | Test if a URL is fetchable |
| `/research <stream_id>` | Re-run research for a stream |
| `/latest` | Show latest fetched articles |
| `/runpipeline` | Run fetch → summarize → deliver now |
| `/status` | Show system status |

---
*Powered by autonomous AI research.*\
""")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /newstream — Conversation flow
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_newstream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the new stream conversation."""
    context.user_data["answers"] = {}
    context.user_data["premade_idx"] = 0

    await send_rich_async(update.effective_chat.id, """\
# ➕ New News Stream

Let's set up your personalised news feed. I'll ask a few questions, \
then our AI research engine will find the best sources for you.

---
""")

    # Ask the first premade question
    q = PREMADE_QUESTIONS[0]
    await send_rich_async(
        update.effective_chat.id,
        f"**Q1: {q['question']}**\n\n_{q['placeholder']}_",
    )
    return TOPIC


async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the topic answer, ask about strictness."""
    context.user_data["answers"]["topic"] = update.message.text
    context.user_data["premade_idx"] = 1

    q = PREMADE_QUESTIONS[1]
    await send_rich_async(
        update.effective_chat.id,
        f"Got it.\n\n**Q2: {q['question']}**\n\n_{q['placeholder']}_",
    )
    return STRICTNESS


async def handle_strictness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the strictness answer, ask about exclusions."""
    context.user_data["answers"]["strictness"] = update.message.text
    context.user_data["premade_idx"] = 2

    q = PREMADE_QUESTIONS[2]
    await send_rich_async(
        update.effective_chat.id,
        f"**Q3: {q['question']}**\n\n_{q['placeholder']}_",
    )
    return EXCLUSIONS


async def handle_exclusions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle exclusions, generate dynamic follow-up questions."""
    context.user_data["answers"]["exclusions"] = update.message.text

    await send_rich_async(update.effective_chat.id, "🧠 Generating follow-up questions...")

    # Generate dynamic follow-ups
    followups = await generate_followup_questions(context.user_data["answers"])
    context.user_data["followup_questions"] = followups
    context.user_data["followup_answers"] = []
    context.user_data["followup_idx"] = 0

    if followups:
        await send_rich_async(
            update.effective_chat.id,
            f"**Follow-up: {followups[0]}**",
        )
        return FOLLOWUPS
    else:
        # No follow-ups needed, proceed to research
        return await _start_research(update, context)


async def handle_followups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle follow-up question answers."""
    context.user_data["followup_answers"].append(update.message.text)
    context.user_data["followup_idx"] += 1

    followups = context.user_data["followup_questions"]
    idx = context.user_data["followup_idx"]

    if idx < len(followups):
        await send_rich_async(
            update.effective_chat.id,
            f"**Follow-up: {followups[idx]}**",
        )
        return FOLLOWUPS
    else:
        # All follow-ups answered, compile answers and start research
        return await _start_research(update, context)


async def _start_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compile answers and kick off research."""
    # Merge follow-up answers into the answers dict
    answers = context.user_data["answers"]
    followup_qs = context.user_data.get("followup_questions", [])
    followup_as = context.user_data.get("followup_answers", [])
    for q, a in zip(followup_qs, followup_as):
        answers[f"followup_{len(answers)}"] = f"Q: {q}\nA: {a}"

    # Create stream in DB
    stream_name = answers.get("topic", "New Stream")[:50]
    stream_id = store.create_stream(
        user_id=update.effective_user.id,
        name=stream_name,
        criteria=answers,  # temporary, will be replaced with profile after research
    )
    store.update_stream_status(stream_id, "researching")
    context.user_data["stream_id"] = stream_id

    await send_rich_async(update.effective_chat.id, f"""\
# 🔬 Research Starting

**Stream:** {stream_name}
**Stream ID:** `{stream_id}`

The research engine is now running:
1. Build criteria profile
2. Search for candidates (parallel)
3. Qualify sources (parallel sub-agents)
4. Validate fetchability

This usually takes 30-90 seconds. I'll keep you updated.

---\
""")

    # Run research in background with progress callbacks
    chat_id = update.effective_chat.id

    async def progress(msg: str):
        await send_rich_async(chat_id, msg)

    # Run research asynchronously
    asyncio.create_task(_run_research_background(stream_id, answers, chat_id, context))

    return ConversationHandler.END


async def _run_research_background(stream_id: int, answers: dict, chat_id: int,
                                     context: ContextTypes.DEFAULT_TYPE):
    """Run research in background, send progress updates."""
    try:
        state = await run_research(answers, stream_id, progress=None)

        # Build the criteria profile into the stream
        if state.get("profile"):
            conn = get_connection()
            import json
            conn.execute(
                "UPDATE streams SET criteria = ? WHERE id = ?",
                (json.dumps(state["profile"]), stream_id),
            )
            conn.commit()
            conn.close()

        store.update_stream_status(stream_id, "active")

        # Report results
        final_sources = state.get("final_sources", [])
        profile = state.get("profile", {})

        if final_sources:
            markdown = f"""\
# ✅ Research Complete!

**Stream ID:** `{stream_id}`
**Domain:** {profile.get('broad_domain', 'N/A')}
**Sources Found:** {len(final_sources)}

## Top Sources

| # | Source | Score | Status |
|---|--------|-------|--------|
"""
            for i, src in enumerate(final_sources, 1):
                name = src.get('name', 'Unknown')[:25]
                score = src.get('quality_score', 0)
                markdown += f"| {i} | {name} | {score} | ✅ |\n"

            markdown += f"\nUse `/sources {stream_id}` to see full details."

            await send_rich_async(chat_id, markdown)
        else:
            await send_rich_async(chat_id, f"""\
# ⚠️ Research Complete (No Sources)

The research engine didn't find enough qualifying sources. \
This can happen with very narrow topics.

Try `/research {stream_id}` to try again, or `/addsource {stream_id} <url>` \
to add a source manually.
""")

    except Exception as e:
        logger.exception("Background research failed")
        store.update_stream_status(stream_id, "active")
        await send_rich_async(chat_id, f"❌ Research error: {e}")


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await send_rich_async(update.effective_chat.id, "❌ Stream creation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /streams
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_streams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all streams."""
    streams = store.get_streams_by_user(update.effective_user.id)

    if not streams:
        await send_rich_async(update.effective_chat.id, "📭 You have no streams yet. Use `/newstream` to create one.")
        return

    markdown = "# 📋 Your News Streams\n\n"
    markdown += "| ID | Name | Status | Sources |\n|----|------|--------|---------|\n"

    for s in streams:
        sources = store.get_sources_by_stream(s["id"])
        active = len([src for src in sources if src["fetch_status"] == "active"])
        name = s["name"][:30]
        status_emoji = {"active": "✅", "researching": "🔬", "paused": "⏸️"}.get(s["status"], "❓")
        markdown += f"| {s['id']} | {name} | {status_emoji} {s['status']} | {active} |\n"

    markdown += "\nUse `/sources <id>` to view sources for a stream."

    await send_rich_async(update.effective_chat.id, markdown)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /sources <stream_id>
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View sources for a stream."""
    if not context.args:
        await send_rich_async(update.effective_chat.id, "Usage: `/sources <stream_id>`")
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, "Invalid stream ID. Usage: `/sources <stream_id>`")
        return

    sources = store.get_sources_by_stream(stream_id)

    if not sources:
        await send_rich_async(update.effective_chat.id, f"📭 No sources found for stream `{stream_id}`.")
        return

    # Build rich source table
    markdown = f"# 📰 Sources for Stream `{stream_id}`\n\n"
    markdown += "| # | Source | Score | Status | Keywords |\n|---|--------|-------|--------|----------|\n"

    for i, src in enumerate(sources, 1):
        name = (src.get("name") or src["url"])[:25]
        score = src.get("quality_score", 0)
        status_icon = {"active": "✅", "blocked": "🚫", "error": "⚠️"}.get(src["fetch_status"], "❓")
        keywords = ", ".join(src.get("specific_keywords", [])[:3])
        markdown += f"| {i} | [{name}]({src['url']}) | {score} | {status_icon} | {keywords} |\n"

    # Collapsible details for each source
    detail_parts = ["\n---\n## Source Details\n"]
    for i, src in enumerate(sources, 1):
        detail_parts.append(f"\n**{i}. {src.get('name', src['url'])}**")
        detail_parts.append(f"- URL: {src['url']}")
        detail_parts.append(f"- Score: {src.get('quality_score', 0)}/100")
        detail_parts.append(f"- Status: {src['fetch_status']}")
        detail_parts.append(f"- Category: {src.get('broad_category', 'N/A')}")
        detail_parts.append(f"- Keywords: {', '.join(src.get('specific_keywords', []))}")
        if src.get("description"):
            detail_parts.append(f"- Description: {src['description']}")
        detail_parts.append(f"- ID: `{src['id']}` (use with /deletesource)")

    markdown += "\n".join(detail_parts)

    await send_rich_async(update.effective_chat.id, markdown)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /sources_all
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_sources_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View the entire source database."""
    sources = store.get_all_sources()

    if not sources:
        await send_rich_async(update.effective_chat.id, "🗃️ The source database is empty.")
        return

    markdown = f"# 🗃️ Source Database ({len(sources)} total)\n\n"
    markdown += "| # | Source | Stream | Score | Status |\n|---|--------|--------|-------|--------|\n"

    for i, src in enumerate(sources[:30], 1):  # cap at 30 for message size
        name = (src.get("name") or src["url"])[:25]
        stream_name = (src.get("stream_name") or "?")[:15]
        score = src.get("quality_score", 0)
        status_icon = {"active": "✅", "blocked": "🚫", "error": "⚠️"}.get(src["fetch_status"], "❓")
        markdown += f"| {i} | [{name}]({src['url']}) | {stream_name} | {score} | {status_icon} |\n"

    if len(sources) > 30:
        markdown += f"\n*...and {len(sources) - 30} more.*"

    await send_rich_async(update.effective_chat.id, markdown)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /addsource <stream_id> <url>
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_addsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually add a source."""
    if len(context.args) < 2:
        await send_rich_async(update.effective_chat.id, "Usage: `/addsource <stream_id> <url>`")
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, "Invalid stream ID.")
        return

    url = context.args[1]

    # Check if source already exists
    existing = store.get_source_by_url(stream_id, url)
    if existing:
        await send_rich_async(update.effective_chat.id, f"⚠️ Source already exists: `{existing['id']}`")
        return

    await send_rich_async(update.effective_chat.id, f"🧪 Testing `{url}` with web crawler...")

    # Test the source
    result = await test_source(url)

    if result["fetchable"]:
        source_id = store.add_source(
            stream_id=stream_id,
            url=url,
            name=result["title"][:100],
            fetch_status="active",
        )
        await send_rich_async(update.effective_chat.id, f"""\
✅ Source added successfully!

**ID:** `{source_id}`
**URL:** {url}
**Title:** {result['title']}

The crawler can fetch this source. News will be included in future fetches.\
""")
    else:
        # Add as blocked so the user can see it
        source_id = store.add_source(
            stream_id=stream_id,
            url=url,
            fetch_status="blocked",
        )
        await send_rich_async(update.effective_chat.id, f"""\
⚠️ Source added but **blocked**.

**ID:** `{source_id}`
**URL:** {url}
**Error:** {result.get('error', 'Unknown')}

The crawler couldn't fetch this source. It's saved as blocked for reference.\
""")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /deletesource <source_id>
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_deletesource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a source."""
    if not context.args:
        await send_rich_async(update.effective_chat.id, "Usage: `/deletesource <source_id>`")
        return

    try:
        source_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, "Invalid source ID.")
        return

    src = store.get_source(source_id)
    if not src:
        await send_rich_async(update.effective_chat.id, "❌ Source not found.")
        return

    store.delete_source(source_id)
    await send_rich_async(update.effective_chat.id, f"🗑️ Deleted source `{source_id}` ({src.get('name', src['url'])})")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /testsource <url>
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_testsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test if a URL is fetchable."""
    if not context.args:
        await send_rich_async(update.effective_chat.id, "Usage: `/testsource <url>`")
        return

    url = context.args[0]
    await send_rich_async(update.effective_chat.id, f"🧪 Testing `{url}`...")

    result = await test_source(url)

    if result["fetchable"]:
        await send_rich_async(update.effective_chat.id, f"""\
✅ **Fetchable!**

**URL:** {url}
**Title:** {result['title']}

*Preview (first 200 chars):*
> {result['content_preview'][:200]}\
""")
    else:
        await send_rich_async(update.effective_chat.id, f"""\
❌ **Not Fetchable**

**URL:** {url}
**Error:** {result.get('error', 'Unknown')}
""")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /research <stream_id>
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run research for a stream."""
    if not context.args:
        await send_rich_async(update.effective_chat.id, "Usage: `/research <stream_id>`")
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, "Invalid stream ID.")
        return

    stream = store.get_stream(stream_id)
    if not stream:
        await send_rich_async(update.effective_chat.id, "❌ Stream not found.")
        return

    store.update_stream_status(stream_id, "researching")
    await send_rich_async(update.effective_chat.id, f"🔬 Re-running research for stream `{stream_id}`...")

    chat_id = update.effective_chat.id

    async def progress(msg: str):
        await send_rich_async(chat_id, msg)

    asyncio.create_task(_run_research_background(stream_id, stream["criteria"], chat_id, context))

    await send_rich_async(update.effective_chat.id, "Research started in background. I'll update you with results.")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /latest
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show latest fetched articles."""
    articles = store.get_latest_articles(limit=15)

    if not articles:
        await send_rich_async(update.effective_chat.id, "📭 No articles fetched yet.")
        return

    markdown = "# 📰 Latest Articles\n\n"
    markdown += "| # | Title | Source | Relevance |\n|---|-------|--------|----------|\n"

    for i, art in enumerate(articles, 1):
        title = (art.get("title") or "Untitled")[:40]
        source = (art.get("source_name") or art.get("source_url", "?"))[:15]
        score = art.get("relevance_score", 0)
        markdown += f"| {i} | [{title}]({art.get('url', '#')}) | {source} | {score:.1f} |\n"

    await send_rich_async(update.effective_chat.id, markdown)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /runpipeline
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_runpipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the full pipeline."""
    chat_id = update.effective_chat.id

    await send_rich_async(chat_id, "▶️ Running pipeline...\n\n**Step 1/3:** Fetching news...")

    # Run pipeline steps
    fetch_result = await fetch_all_news()
    await send_rich_async(chat_id, f"✅ Fetched {fetch_result['total_new']} new articles from {fetch_result['total_sources']} sources.")

    await send_rich_async(chat_id, "**Step 2/3:** Summarizing & scoring relevance...")
    process_result = await process_new_articles()
    await send_rich_async(chat_id, f"✅ Processed {process_result['processed']} articles ({process_result['relevant']} relevant).")

    await send_rich_async(chat_id, "**Step 3/3:** Delivering digest...")
    deliver_result = await deliver_digest_async(chat_id)

    if deliver_result["delivered"] > 0:
        await send_rich_async(chat_id, f"✅ Digest delivered: {deliver_result['delivered']} articles.")
    else:
        await send_rich_async(chat_id, "📭 No relevant articles to deliver.")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /status
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show system status."""
    from database.models import get_connection
    conn = get_connection()

    stream_count = conn.execute("SELECT COUNT(*) as c FROM streams").fetchone()["c"]
    source_count = conn.execute("SELECT COUNT(*) as c FROM sources").fetchone()["c"]
    active_sources = conn.execute("SELECT COUNT(*) as c FROM sources WHERE fetch_status = 'active'").fetchone()["c"]
    article_count = conn.execute("SELECT COUNT(*) as c FROM articles").fetchone()["c"]
    new_articles = conn.execute("SELECT COUNT(*) as c FROM articles WHERE status = 'new'").fetchone()["c"]

    conn.close()

    await send_rich_async(update.effective_chat.id, f"""\
# 📊 System Status

| Metric | Value |
|--------|-------|
| Streams | {stream_count} |
| Total Sources | {source_count} |
| Active Sources | {active_sources} |
| Total Articles | {article_count} |
| New (Unprocessed) | {new_articles} |

---
*Scheduler runs fetch every {config.FETCH_INTERVAL_MINUTES} min, process every {config.PROCESS_INTERVAL_MINUTES} min, deliver every {config.DELIVER_INTERVAL_HOURS} hours.*\
""")