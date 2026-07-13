"""
Telegram bot handlers — all commands and conversation flow.
Uses python-telegram-bot ConversationHandler for the multi-step /newstream flow.
"""
import asyncio
import functools
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
from bot.messaging import send_rich_async
from research.engine import run_research
from research.feed_finder import find_news_pages
from research.profile_builder import interview_turn, OPENER
from crawler.fetcher import test_source
from pipeline import usage
from pipeline.news_cycle import run_news_cycle

logger = logging.getLogger(__name__)

# ── Conversation state for /newstream ─────────────────────────────────────────
# A single natural interview loop replaces the old fixed-form states.
INTERVIEW = 0


# ── Authorization ──────────────────────────────────────────────────────────────

def admin_only(handler):
    """Restrict a command to the operator (config.ADMIN_USER_ID).

    Commands that expose the whole cross-tenant database or drive the pipeline
    must not be callable by any Telegram user who finds the bot.
    """
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != config.ADMIN_USER_ID:
            await send_rich_async(update.effective_chat.id,
                                  "❌ This command is restricted to the operator.")
            return
        return await handler(update, context)
    return wrapped


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
| `/newstream` | Set up a new news stream (just tell me what you want) |
| `/streams` | List all your news streams |
| `/sources <stream_id>` | View sources for a stream |
| `/sources_all` | View the entire source database |
| `/addsource <stream_id> <url>` | Manually add a source |
| `/deletesource <source_id>` | Remove a source from your stream |
| `/testsource <url>` | Test if a URL is fetchable |
| `/research <stream_id>` | Re-run research for a stream |
| `/latest` | Show latest fetched articles |
| `/runpipeline` | Run fetch → summarize → deliver now |
| `/postsize <stream_id>` | Choose post length (standard / compact) |
| `/pausestream <stream_id>` | Pause a stream (no posts, no crawling) |
| `/resumestream <stream_id>` | Resume a paused stream |
| `/deletestream <stream_id>` | Delete a stream and its sources |
| `/quiet <stream_id> 23-8` | No posts between those hours (`off` to clear) |
| `/status` | Show system status |

---
*Powered by autonomous AI research.*\
""")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /newstream — Conversation flow
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_newstream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the natural intake conversation."""
    context.user_data["transcript"] = [{"role": "assistant", "content": OPENER}]

    await send_rich_async(update.effective_chat.id, OPENER)
    return INTERVIEW


async def handle_interview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    One turn of the natural intake conversation.
    The interviewer decides whether to ask another question or start research.
    """
    transcript = context.user_data.setdefault("transcript", [])
    transcript.append({"role": "user", "content": update.message.text})

    # Show a genuine "typing…" indicator instead of mechanical status text.
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    turn = await interview_turn(transcript)
    transcript.append({"role": "assistant", "content": turn["message"]})

    await send_rich_async(update.effective_chat.id, turn["message"])

    if not turn["enough"]:
        return INTERVIEW

    return await _start_research(update, context)


def _research_allowed(user_id: int) -> tuple[bool, str]:
    """Per-user limits (§3.3): research is a ~100-crawl, ~40-LLM-call operation."""
    if store.get_usage(user_id, "research_run") >= config.RESEARCH_RUNS_PER_DAY:
        return False, (f"You've used your {config.RESEARCH_RUNS_PER_DAY} research "
                       f"runs for today — try again tomorrow.")
    return True, ""


async def _start_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compile the conversation and kick off research."""
    user_id = update.effective_user.id
    if store.count_streams(user_id) >= config.MAX_STREAMS_PER_USER:
        await send_rich_async(
            update.effective_chat.id,
            f"You already have {config.MAX_STREAMS_PER_USER} streams — that's the "
            f"limit for now. `/deletestream <id>` frees a slot.")
        return ConversationHandler.END
    allowed, why = _research_allowed(user_id)
    if not allowed:
        await send_rich_async(update.effective_chat.id, why)
        return ConversationHandler.END

    transcript = context.user_data.get("transcript", [])

    # Seed the stream name from the user's first answer; hand the full
    # conversation to the research engine as the criteria.
    first_answer = next(
        (t["content"] for t in transcript if t["role"] == "user"), "New Stream"
    )
    convo_text = "\n".join(
        f"{'User' if t['role'] == 'user' else 'Service'}: {t['content']}"
        for t in transcript
    )
    answers = {"topic": first_answer, "conversation": convo_text}

    stream_name = first_answer[:50]
    stream_id = store.create_stream(
        user_id=user_id,
        name=stream_name,
        criteria=answers,  # temporary, replaced with the profile after research
    )
    store.update_stream_status(stream_id, "researching")
    context.user_data["stream_id"] = stream_id

    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id,
        "🔎 On it — I'm reading through the best sources on this now. This usually "
        "takes a minute or two.\n\nWhile I work: how long should each post be? "
        "(Standard by default — change anytime with /postsize.)",
        reply_markup=_post_length_keyboard(stream_id),
    )

    asyncio.create_task(_run_research_background(stream_id, answers, chat_id,
                                                 context, user_id=user_id))

    return ConversationHandler.END


def _post_length_keyboard(stream_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Standard (~100 words)", callback_data=f"plen:{stream_id}:standard"),
        InlineKeyboardButton("⚡ Compact", callback_data=f"plen:{stream_id}:compact"),
    ]])


async def cmd_postsize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set how long a stream's posts are."""
    chat_id = update.effective_chat.id
    if not context.args:
        streams = store.get_streams_by_user(update.effective_user.id)
        if not streams:
            await send_rich_async(chat_id, "You have no streams yet. Use `/newstream`.")
            return
        await send_rich_async(chat_id, "Usage: `/postsize <stream_id>` — I'll show the options.")
        return
    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, "Invalid stream ID.")
        return
    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(chat_id, "❌ That stream isn't yours.")
        return
    current = (stream.get("criteria") or {}).get("post_length", "standard")
    await context.bot.send_message(
        chat_id,
        f"Posts for *{stream['name']}* are currently *{current}*. Pick a size:".replace("*", ""),
        reply_markup=_post_length_keyboard(stream_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Stream lifecycle: /pausestream, /resumestream, /deletestream, /quiet  (§3.1, §3.6)
# ═══════════════════════════════════════════════════════════════════════════════

async def _owned_stream_from_args(update, context) -> tuple[int | None, dict | None]:
    """Parse `<stream_id>` from args and verify ownership. Replies on failure."""
    chat_id = update.effective_chat.id
    if not context.args:
        await send_rich_async(chat_id, "Usage: give me a stream id — "
                                       "`/streams` lists yours.")
        return None, None
    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, "Invalid stream ID.")
        return None, None
    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(chat_id, "❌ That stream isn't yours.")
        return None, None
    return stream_id, stream


async def cmd_pausestream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause a stream: no posts AND no crawling for sources only it follows."""
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return
    store.update_stream_status(stream_id, "paused")
    await send_rich_async(
        update.effective_chat.id,
        f"⏸️ **{stream['name']}** is paused — nothing will be posted and its "
        f"sources stop being crawled. `/resumestream {stream_id}` brings it back.")


async def cmd_resumestream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume a paused stream."""
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return
    store.update_stream_status(stream_id, "active")
    store.record_send_result(stream_id, ok=True)  # clear the auto-pause streak
    await send_rich_async(
        update.effective_chat.id,
        f"▶️ **{stream['name']}** is active again. Sources resume on the next cycle.")


async def cmd_deletestream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a stream — destructive, so it asks for one confirmation tap."""
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return
    n_sources = len(store.get_sources_by_stream(stream_id))
    await context.bot.send_message(
        update.effective_chat.id,
        f"Delete \"{stream['name']}\" and its {n_sources} source "
        f"subscription(s)? This can't be undone.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Yes, delete it",
                                 callback_data=f"del_stream:{stream_id}"),
            InlineKeyboardButton("✖️ Keep it", callback_data="del_stream:cancel"),
        ]]),
    )


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a stream's quiet hours: posts inside the window are held, not lost."""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await send_rich_async(
            chat_id,
            "Usage: `/quiet <stream_id> 23-8` — no posts from 23:00 to 08:00 "
            "(server time). `/quiet <stream_id> off` clears it.")
        return
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return

    spec = context.args[1].strip().lower()
    if spec in ("off", "none", "clear"):
        store.set_stream_criteria_field(stream_id, "quiet_hours", "")
        await send_rich_async(chat_id, f"🔔 Quiet hours cleared for "
                                       f"**{stream['name']}** — posts flow 24/7.")
        return

    from pipeline.news_cycle import _parse_quiet_hours
    if _parse_quiet_hours({"quiet_hours": spec}) is None:
        await send_rich_async(chat_id, "That doesn't parse — use e.g. `23-8` "
                                       "(hours 0–23, start ≠ end).")
        return
    store.set_stream_criteria_field(stream_id, "quiet_hours", spec)
    start, end = spec.split("-", 1)
    await send_rich_async(
        chat_id,
        f"🔕 Quiet hours set for **{stream['name']}**: nothing between "
        f"{int(start):02d}:00 and {int(end):02d}:00 (server time). Held posts "
        f"go out on the first cycle after the window ends.")


async def _run_research_background(stream_id: int, answers: dict, chat_id: int,
                                     context: ContextTypes.DEFAULT_TYPE,
                                     user_id: int = 0):
    """Run research in background, send progress updates."""
    # Attribute every crawl/LLM call in this task to the requesting tenant.
    usage.set_user(user_id)
    store.increment_usage(user_id, "research_run")
    try:
        state = await run_research(answers, stream_id, progress=None)

        # Persist the profile as the stream's criteria — but keep the user's
        # own intake conversation inside it. Re-research needs their actual
        # words; feeding a profile back in as if it were the interview degrades
        # the rubric a little more on every run.
        if state.get("profile"):
            profile = state["profile"]
            if not profile.get("intake_conversation"):
                profile["intake_conversation"] = (
                    answers.get("conversation") or ""
                ) if isinstance(answers, dict) else ""
            store.update_stream_criteria(stream_id, profile)

        store.update_stream_status(stream_id, "active")

        # Report from what was actually STORED, not just the fetchable subset.
        # Qualification can find great sources that validation can't reach on the
        # first try (they rate-limit us right after the qualification crawl). Those
        # are stored 'blocked' and the daily health check revives them — so
        # "found nothing" is only true when qualification itself came up empty.
        stream = store.get_stream(stream_id)
        stream_name = (stream or {}).get("name", "your stream")
        stored = store.get_sources_by_stream(stream_id)
        active = [s for s in stored if s["fetch_status"] == "active"]
        blocked = [s for s in stored if s["fetch_status"] == "blocked"]

        # §2.4 reconciliation: "research succeeded" must mean the sources
        # actually YIELD articles, not just that their pages loaded once.
        # Snapshot each stored active source and report honestly.
        item_counts = await _reconcile_sources(active)

        if active or blocked:
            live = [s for s in active if item_counts.get(s["id"], 0) > 0]
            pending = [s for s in active if item_counts.get(s["id"], 0) == 0]

            lines = [f"# ✅ Found {len(stored)} sources for you", "",
                     f"Here's what I'll be watching for **{stream_name}**:", "",
                     "| # | Source | Match | Articles | Status |",
                     "|---|--------|-------|----------|--------|"]
            for i, src in enumerate(active + blocked, 1):
                name = (src.get("name") or src["url"])[:25]
                score = src.get("quality_score", 0)
                if src["fetch_status"] != "active":
                    icon, n_items = "🔄", "—"
                else:
                    n = item_counts.get(src["id"], 0)
                    icon = "✅" if n else "⏳"
                    n_items = str(n) if n else "0"
                lines.append(f"| {i} | {name} | {score}/100 | {n_items} | {icon} |")

            if live:
                lines.append(f"\n{len(live)} source(s) are live and already "
                             f"listing articles — I'll start pulling relevant "
                             f"stories shortly.")
            if pending:
                lines.append(
                    f"\n⏳ {len(pending)} listed no articles on the first read — "
                    f"they stay on watch and count as live the moment they yield."
                )
            if blocked:
                lines.append(
                    f"\n🔄 {len(blocked)} were busy when I checked — I'll keep "
                    f"retrying them automatically and they'll switch on once reachable."
                )
            if active and all((s.get("site_type") == "aggregator") for s in active):
                lines.append(
                    "\n⚠️ Everything live right now is an aggregator feed — "
                    "I'd suggest `/addsource` with a publication you trust "
                    "for first-party coverage."
                )
            lines.append(
                f"\nWant to fine-tune? `/sources {stream_id}` shows the full list — "
                f"drop any with `/deletesource <id>` or add your own with "
                f"`/addsource {stream_id} <url>`."
            )
            await send_rich_async(chat_id, "\n".join(lines))
        else:
            await send_rich_async(chat_id, f"""\
I dug through a lot of sites but couldn't find sources solid enough to trust for this one yet — often that means the topic is very narrow, or worth phrasing a little differently.

A couple of options:
• `/research {stream_id}` — I'll take another pass at it.
• `/addsource {stream_id} <url>` — point me at a site you already like and I'll build from there.\
""")

    except Exception as e:
        logger.exception("Background research failed")
        store.update_stream_status(stream_id, "active")
        await send_rich_async(chat_id, f"❌ Research error: {e}")


async def _reconcile_sources(sources: list[dict]) -> dict[int, int]:
    """
    §2.4: snapshot each freshly stored source once and count what it lists.
    Catches whole classes of feed-selection bugs at research time instead of
    silently, days later. Pure reads — baselining still happens on the first
    news cycle. Failures count as 0 items (worth flagging, not crashing).
    """
    from pipeline.fetch_news import snapshot_source, UNCHANGED

    async def _count(src: dict) -> tuple[int, int]:
        try:
            snap = await snapshot_source(dict(src))
            n = 0 if snap is UNCHANGED else len(snap)
        except Exception:
            n = 0
        return src["id"], n

    if not sources:
        return {}
    results = await asyncio.gather(*(_count(s) for s in sources),
                                   return_exceptions=True)
    counts = {}
    for r in results:
        if not isinstance(r, Exception):
            sid, n = r
            counts[sid] = n
    return counts


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

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(update.effective_chat.id, "❌ That stream isn't yours.")
        return

    sources = store.get_sources_by_stream(stream_id)

    if not sources:
        await send_rich_async(update.effective_chat.id, f"📭 No sources found for stream `{stream_id}`.")
        return

    # The ID column IS the id you pass to /deletesource — never a row number.
    markdown = f"# 📰 Sources for Stream `{stream_id}`\n\n"
    markdown += "| ID | Source | Score | Status |\n|----|--------|-------|--------|\n"

    for src in sources:
        name = (src.get("name") or src["url"])[:28]
        score = src.get("quality_score", 0)
        status_icon = {"active": "✅", "blocked": "🚫", "error": "⚠️"}.get(src["fetch_status"], "❓")
        markdown += f"| `{src['id']}` | [{name}]({src['url']}) | {score} | {status_icon} |\n"

    detail_parts = ["\n---\n## Details\n"]
    for src in sources:
        type_label = {"news_site": "📰 News site", "company_blog": "🏢 Company blog",
                      "aggregator": "🔗 Aggregator", "analysis": "🔬 Analysis"}.get(
                          src.get("site_type"), "")
        header = f"\n**`{src['id']}` — {src.get('name') or src['url']}**"
        if type_label:
            header += f"  · {type_label}"
        detail_parts.append(header)
        detail_parts.append(f"- Site: {src['url']}")
        if src.get("feed_url") and src["feed_url"] != src["url"]:
            detail_parts.append(f"- Polling: {src['feed_url']}")
        detail_parts.append(f"- Score: {src.get('quality_score', 0)}/100 · Status: {src['fetch_status']}")
        if src.get("specific_keywords"):
            detail_parts.append(f"- Keywords: {', '.join(src['specific_keywords'])}")
        if src.get("description"):
            detail_parts.append(f"- {src['description']}")

    markdown += "\n".join(detail_parts)
    markdown += "\n\n_Delete with_ `/deletesource <ID>` _— or tap below._"

    await send_rich_async(update.effective_chat.id, markdown)

    rows = [[InlineKeyboardButton(
        f"🗑 {src['id']} · {_short(src.get('name') or src['url'], 26)}",
        callback_data=f"del_src:{src['id']}")] for src in sources[:10]]
    await context.bot.send_message(
        update.effective_chat.id, "Tap a source to delete it:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /sources_all
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def cmd_sources_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View the entire source database (operator only — spans all tenants)."""
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

async def _owns_stream(user_id: int, stream_id: int) -> tuple[bool, dict | None]:
    stream = store.get_stream(stream_id)
    if not stream:
        return False, None
    return stream["user_id"] == user_id, stream


async def cmd_addsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add a source. The user usually pastes a site's front door, so we go and find
    the page that actually lists its articles rather than polling a homepage.
    """
    chat_id = update.effective_chat.id

    if len(context.args) < 2:
        await send_rich_async(chat_id, "Usage: `/addsource <stream_id> <url>`")
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, "Invalid stream ID.")
        return

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None:
        await send_rich_async(chat_id, f"❌ Stream `{stream_id}` not found.")
        return
    if not owns:
        await send_rich_async(chat_id, "❌ That stream isn't yours.")
        return

    if len(store.get_sources_by_stream(stream_id)) >= config.MAX_SOURCES_PER_STREAM:
        await send_rich_async(
            chat_id,
            f"This stream already follows {config.MAX_SOURCES_PER_STREAM} sources "
            f"— that's the cap. Drop one with `/deletesource <id>` first.")
        return

    url = context.args[1]
    if "://" not in url:
        url = f"https://{url}"

    await send_rich_async(
        chat_id,
        f"🔍 Looking for the news page on `{url}`...\n\n_Checking for feeds, "
        f"section pages, and site navigation. This takes a moment._",
    )

    try:
        candidates = await find_news_pages(url)
    except Exception as e:
        logger.exception("Feed discovery failed for %s", url)
        await send_rich_async(chat_id, f"❌ Couldn't inspect that site: {e}")
        return

    # ── Nothing publishable ───────────────────────────────────────────
    if not candidates:
        context.user_data["pending_source"] = {"stream_id": stream_id, "url": url}
        await context.bot.send_message(
            chat_id,
            f"I crawled <b>{url}</b> — its feeds, common news paths, and its own "
            f"navigation — and couldn't find any page that lists articles.\n\n"
            f"That usually means the site doesn't publish a news feed, or it "
            f"blocks crawlers.\n\nAdd it anyway and poll the URL as given?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add anyway", callback_data="feed:force"),
                InlineKeyboardButton("✖️ Cancel", callback_data="feed:cancel"),
            ]]),
        )
        return

    # ── Exactly one → just add it ─────────────────────────────────────
    if len(candidates) == 1:
        await _store_discovered_source(chat_id, stream_id, url, candidates[0])
        return

    # ── Several → let the user choose ─────────────────────────────────
    context.user_data["feed_candidates"] = {
        "stream_id": stream_id, "site_url": url,
        "cands": [(c.url, c.kind, c.item_count, c.scope) for c in candidates],
    }
    rows = [[InlineKeyboardButton(
        f"{'📡' if c.kind == 'feed' else '📄'} {_short(c.url)} · {c.item_count} articles",
        callback_data=f"feed:{i}")] for i, c in enumerate(candidates)]
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="feed:cancel")])

    await context.bot.send_message(
        chat_id,
        f"<b>{url}</b> has more than one page that publishes articles.\n\n"
        f"Which should I follow?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _short(url: str, n: int = 34) -> str:
    trimmed = url.replace("https://", "").replace("http://", "").rstrip("/")
    return trimmed if len(trimmed) <= n else trimmed[: n - 1] + "…"


async def _store_discovered_source(chat_id: int, stream_id: int, site_url: str,
                                   cand) -> None:
    """Persist a verified news page as a source, keyed on the site's root URL."""
    if store.get_source_by_url(stream_id, site_url):
        await send_rich_async(chat_id, "⚠️ That source is already on this stream.")
        return

    name = (cand.title or _short(site_url, 60))[:100]
    if cand.kind == "feed":
        method, kind = "rss", "RSS feed"
    elif getattr(cand, "scope", "internal") == "external":
        # Outbound aggregator: the page's headlines link to other domains, so
        # the poller must keep off-domain links for this source.
        method, kind = "links_ext", "page linking out to articles"
    else:
        method, kind = "links", "article page"
    source_id = store.add_source(
        stream_id=stream_id, url=site_url, name=name,
        feed_url=cand.url, fetch_status="active",
        fetch_method=method,
    )

    # Add it to the semantic internal DB too (best-effort).
    try:
        from research import embeddings
        await embeddings.backfill_stream_embeddings(stream_id)
    except Exception:
        logger.exception("Embedding a manually-added source failed (non-fatal)")

    await send_rich_async(chat_id, f"""\
✅ **Source added** — `{source_id}`

**Site:** {site_url}
**Polling:** {cand.url}
_Found via {kind} — {cand.item_count} articles detected._

I'll baseline it on the next cycle: everything published so far is recorded \
silently, and you'll only hear about what appears *after* that.\
""")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /deletesource <source_id>
# ═══════════════════════════════════════════════════════════════════════════════

async def _delete_source_for(user_id: int, source_id: int) -> tuple[bool, str]:
    """
    Remove a source from the caller's stream(s). Sources are canonical now —
    "deleting" unsubscribes THIS user's streams; the row itself only goes away
    once nobody follows it anymore.
    """
    src = store.get_source(source_id)
    if not src:
        return False, (f"❌ No source with ID `{source_id}`.\n\n"
                       f"Use `/sources <stream_id>` — the **ID** column is what "
                       f"you pass here, not the row position.")

    # Which of the caller's streams actually follow it?
    subscribed = [
        s["id"] for s in store.get_streams_by_user(user_id)
        if any(x["id"] == source_id for x in store.get_sources_by_stream(s["id"]))
    ]
    if not subscribed:
        return False, "❌ That source isn't on any of your streams."

    for sid in subscribed:
        store.unsubscribe(sid, source_id)
    name = src.get("name") or src["url"]
    return True, f"🗑️ Removed source `{source_id}` — {name}"


async def cmd_deletesource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a source by its database ID."""
    if not context.args:
        await send_rich_async(update.effective_chat.id,
                              "Usage: `/deletesource <source_id>`")
        return

    try:
        source_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, "Invalid source ID.")
        return

    _, msg = await _delete_source_for(update.effective_user.id, source_id)
    await send_rich_async(update.effective_chat.id, msg)


# ═══════════════════════════════════════════════════════════════════════════════
# Inline button callbacks
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route every inline-keyboard press. Without this, all buttons are dead."""
    query = update.callback_query
    await query.answer()  # stop Telegram's spinner

    data = query.data or ""
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # ── Delete a source ───────────────────────────────────────────────
    if data.startswith("del_src:"):
        source_id = int(data.split(":", 1)[1])
        ok, msg = await _delete_source_for(user_id, source_id)
        await query.edit_message_text(
            ("🗑️ Deleted." if ok else "Couldn't delete that one."))
        await send_rich_async(chat_id, msg)
        return

    # ── Delete a stream (confirmation tap from /deletestream) ────────
    if data.startswith("del_stream:"):
        arg = data.split(":", 1)[1]
        if arg == "cancel":
            await query.edit_message_text("Kept — nothing was deleted.")
            return
        stream_id = int(arg)
        owns, stream = await _owns_stream(user_id, stream_id)
        if stream is None or not owns:
            await query.edit_message_text("That stream isn't yours.")
            return
        store.delete_stream(stream_id)
        await query.edit_message_text(f"🗑 Deleted \"{stream['name']}\" and "
                                      f"unsubscribed its sources.")
        return

    # ── 👍/👎 feedback on a delivered post (§3.7) ─────────────────────
    if data.startswith("fb:"):
        try:
            _, aid, sid, verdict = data.split(":", 3)
            article_id, stream_id = int(aid), int(sid)
        except ValueError:
            await query.answer("Malformed feedback.")
            return
        owns, _stream = await _owns_stream(user_id, stream_id)
        if not owns:
            return  # feedback only counts from the stream's owner
        if verdict in ("up", "down"):
            store.set_delivery_verdict(article_id, stream_id, verdict)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass  # markup already gone (double tap) — the verdict stuck
        return

    # ── Post length ───────────────────────────────────────────────────
    if data.startswith("plen:"):
        _, sid, length = data.split(":", 2)
        owns, _stream = await _owns_stream(user_id, int(sid))
        if not owns:
            await query.edit_message_text("That stream isn't yours.")
            return
        store.set_post_length(int(sid), length)
        pretty = "Standard (~100 words)" if length == "standard" else "Compact"
        await query.edit_message_text(f"✅ Posts set to {pretty}.")
        return

    # ── Re-test a source ──────────────────────────────────────────────
    if data.startswith("test_src:"):
        source_id = int(data.split(":", 1)[1])
        src = store.get_source(source_id)
        if not src:
            await send_rich_async(chat_id, "❌ Source not found.")
            return
        # RSS-aware: the browser can false-flag a healthy feed's raw XML.
        from research.validator import validate_source
        result = await validate_source(src.get("feed_url") or src["url"])
        if result["fetchable"]:
            store.reactivate_source(source_id)
            await send_rich_async(chat_id, f"✅ `{source_id}` is reachable — reactivated.")
        else:
            await send_rich_async(chat_id,
                                  f"❌ `{source_id}` still unreachable: {result.get('error')}")
        return

    # ── Feed discovery choices ────────────────────────────────────────
    if data == "feed:cancel":
        context.user_data.pop("feed_candidates", None)
        context.user_data.pop("pending_source", None)
        await query.edit_message_text("Cancelled — nothing was added.")
        return

    if data == "feed:force":
        pending = context.user_data.pop("pending_source", None)
        if not pending:
            await query.edit_message_text("That request expired. Run /addsource again.")
            return
        # Telegram can redeliver a callback on lag — don't add the source twice.
        if store.get_source_by_url(pending["stream_id"], pending["url"]):
            await query.edit_message_text("That source is already on this stream.")
            return
        source_id = store.add_source(
            stream_id=pending["stream_id"], url=pending["url"],
            name=_short(pending["url"], 60), feed_url=pending["url"],
            fetch_status="active",
        )
        await query.edit_message_text("Added as-is.")
        await send_rich_async(chat_id, f"""\
➕ **Added anyway** — `{source_id}`

I'll poll {pending['url']} directly. If it never yields articles it will be \
marked as errored after a few cycles.\
""")
        return

    if data.startswith("feed:"):
        picked = context.user_data.get("feed_candidates")
        if not picked:
            await query.edit_message_text("That choice expired. Run /addsource again.")
            return
        idx = int(data.split(":", 1)[1])
        if idx >= len(picked["cands"]):
            await query.edit_message_text("That option is no longer valid.")
            return

        chosen = picked["cands"][idx]
        url, kind, count = chosen[0], chosen[1], chosen[2]
        scope = chosen[3] if len(chosen) > 3 else "internal"
        from types import SimpleNamespace
        cand = SimpleNamespace(url=url, kind=kind, item_count=count, title="",
                               scope=scope)
        await query.edit_message_text(f"Following {_short(url, 48)}.")
        await _store_discovered_source(chat_id, picked["stream_id"],
                                       picked["site_url"], cand)
        context.user_data.pop("feed_candidates", None)
        return

    logger.warning("Unhandled callback data: %s", data)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /testsource <url>
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def cmd_testsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test if a URL is fetchable (operator only — drives the crawler at will)."""
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

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(update.effective_chat.id, "❌ That stream isn't yours.")
        return

    allowed, why = _research_allowed(update.effective_user.id)
    if not allowed:
        await send_rich_async(update.effective_chat.id, why)
        return

    store.update_stream_status(stream_id, "researching")
    await send_rich_async(update.effective_chat.id, f"🔬 Re-running research for stream `{stream_id}`...")

    chat_id = update.effective_chat.id

    # Rebuild the research input from the user's ORIGINAL intake conversation
    # when we have it. criteria is a generated profile after the first run —
    # passing that back in as if it were the interview compounds drift.
    criteria = stream.get("criteria") or {}
    convo = ""
    if isinstance(criteria, dict):
        convo = (criteria.get("intake_conversation")
                 or criteria.get("conversation") or "")
    if convo:
        answers = {"topic": stream.get("name") or "", "conversation": convo}
    else:
        answers = criteria  # legacy stream from before intake preservation

    asyncio.create_task(_run_research_background(
        stream_id, answers, chat_id, context,
        user_id=update.effective_user.id))

    await send_rich_async(update.effective_chat.id, "Research started in background. I'll update you with results.")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /latest
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's latest fetched articles (their streams only)."""
    articles = store.get_latest_articles_for_user(update.effective_user.id, limit=15)

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

@admin_only
async def cmd_runpipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually run the same news cycle the cron runs (operator only)."""
    chat_id = update.effective_chat.id

    await send_rich_async(chat_id, "▶️ Running the news cycle...")

    result = await run_news_cycle()

    if result.get("skipped"):
        await send_rich_async(chat_id, "⏳ A cycle is already running — try again shortly.")
        return

    lines = [f"✅ Cycle complete — **{result['posted']}** posted "
             f"from {result['candidates']} candidate(s)."]
    if result.get("baselined_sources"):
        lines.append(f"\n🆕 Baselined {result['baselined_sources']} new source(s) — "
                     f"their existing articles were recorded, not sent.")
    if result.get("irrelevant"):
        lines.append(f"\n🚫 {result['irrelevant']} filtered out as not relevant.")

    await send_rich_async(chat_id, "".join(lines))


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /status
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show system status (operator only — spans all tenants)."""
    from database.models import get_connection
    conn = get_connection()

    stream_count = conn.execute("SELECT COUNT(*) as c FROM streams").fetchone()["c"]
    paused_streams = conn.execute("SELECT COUNT(*) as c FROM streams WHERE status = 'paused'").fetchone()["c"]
    source_count = conn.execute("SELECT COUNT(*) as c FROM sources").fetchone()["c"]
    active_sources = conn.execute("SELECT COUNT(*) as c FROM sources WHERE fetch_status = 'active'").fetchone()["c"]
    pending_baseline = conn.execute("SELECT COUNT(*) as c FROM sources WHERE baselined_at IS NULL").fetchone()["c"]
    subscriptions = conn.execute("SELECT COUNT(*) as c FROM stream_sources").fetchone()["c"]
    article_count = conn.execute("SELECT COUNT(*) as c FROM articles").fetchone()["c"]
    queued = conn.execute("SELECT COUNT(*) as c FROM deliveries WHERE status = 'new'").fetchone()["c"]
    posted = conn.execute("SELECT COUNT(*) as c FROM deliveries WHERE status = 'posted'").fetchone()["c"]
    duplicates = conn.execute("SELECT COUNT(*) as c FROM deliveries WHERE status = 'duplicate'").fetchone()["c"]

    conn.close()

    await send_rich_async(update.effective_chat.id, f"""\
# 📊 System Status

| Metric | Value |
|--------|-------|
| Streams | {stream_count} ({paused_streams} paused) |
| Canonical Sources | {source_count} |
| Active Sources | {active_sources} |
| Subscriptions | {subscriptions} |
| Awaiting Baseline | {pending_baseline} |
| Articles Tracked | {article_count} |
| Queued to Post | {queued} |
| Posted | {posted} |
| Dup-suppressed | {duplicates} |

---
*The news cycle runs every {config.NEWS_CYCLE_MINUTES} min — up to {config.MAX_NEW_PER_SOURCE} new \
articles per source, {config.MAX_POSTS_PER_STREAM_PER_CYCLE} posts per stream \
({config.MAX_POSTS_PER_CYCLE} global) per cycle.*\
""")