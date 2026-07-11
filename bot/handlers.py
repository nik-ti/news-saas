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
| `/deletesource <source_id>` | Delete a source |
| `/testsource <url>` | Test if a URL is fetchable |
| `/research <stream_id>` | Re-run research for a stream |
| `/latest` | Show latest fetched articles |
| `/runpipeline` | Run fetch → summarize → deliver now |
| `/postsize <stream_id>` | Choose post length (standard / compact) |
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


async def _start_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compile the conversation and kick off research."""
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
        user_id=update.effective_user.id,
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

    asyncio.create_task(_run_research_background(stream_id, answers, chat_id, context))

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


async def _run_research_background(stream_id: int, answers: dict, chat_id: int,
                                     context: ContextTypes.DEFAULT_TYPE):
    """Run research in background, send progress updates."""
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

        if active or blocked:
            lines = [f"# ✅ Found {len(stored)} sources for you", "",
                     f"Here's what I'll be watching for **{stream_name}**:", "",
                     "| # | Source | Match | Status |",
                     "|---|--------|-------|--------|"]
            for i, src in enumerate(active + blocked, 1):
                name = (src.get("name") or src["url"])[:25]
                score = src.get("quality_score", 0)
                icon = "✅" if src["fetch_status"] == "active" else "🔄"
                lines.append(f"| {i} | {name} | {score}/100 | {icon} |")

            if active:
                lines.append("\nI'll start pulling relevant stories from these shortly.")
            if blocked:
                lines.append(
                    f"\n🔄 {len(blocked)} were busy when I checked — I'll keep "
                    f"retrying them automatically and they'll switch on once reachable."
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
        "cands": [(c.url, c.kind, c.item_count) for c in candidates],
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
    source_id = store.add_source(
        stream_id=stream_id, url=site_url, name=name,
        feed_url=cand.url, fetch_status="active",
        fetch_method="rss" if cand.kind == "feed" else "links",
    )
    kind = "RSS feed" if cand.kind == "feed" else "article page"

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
    """Delete a source if the caller owns its stream. Returns (ok, message)."""
    src = store.get_source(source_id)
    if not src:
        return False, (f"❌ No source with ID `{source_id}`.\n\n"
                       f"Use `/sources <stream_id>` — the **ID** column is what "
                       f"you pass here, not the row position.")

    owns, _ = await _owns_stream(user_id, src["stream_id"])
    if not owns:
        return False, "❌ That source belongs to someone else's stream."

    store.delete_source(source_id)
    name = src.get("name") or src["url"]
    return True, f"🗑️ Deleted source `{source_id}` — {name}"


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

        url, kind, count = picked["cands"][idx]
        from types import SimpleNamespace
        cand = SimpleNamespace(url=url, kind=kind, item_count=count, title="")
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

    asyncio.create_task(_run_research_background(stream_id, answers, chat_id, context))

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
    source_count = conn.execute("SELECT COUNT(*) as c FROM sources").fetchone()["c"]
    active_sources = conn.execute("SELECT COUNT(*) as c FROM sources WHERE fetch_status = 'active'").fetchone()["c"]
    pending_baseline = conn.execute("SELECT COUNT(*) as c FROM sources WHERE baselined_at IS NULL").fetchone()["c"]
    article_count = conn.execute("SELECT COUNT(*) as c FROM articles").fetchone()["c"]
    queued = conn.execute("SELECT COUNT(*) as c FROM articles WHERE status = 'new'").fetchone()["c"]
    posted = conn.execute("SELECT COUNT(*) as c FROM articles WHERE status = 'posted'").fetchone()["c"]

    conn.close()

    await send_rich_async(update.effective_chat.id, f"""\
# 📊 System Status

| Metric | Value |
|--------|-------|
| Streams | {stream_count} |
| Total Sources | {source_count} |
| Active Sources | {active_sources} |
| Awaiting Baseline | {pending_baseline} |
| Articles Tracked | {article_count} |
| Queued to Post | {queued} |
| Posted | {posted} |

---
*The news cycle runs every {config.NEWS_CYCLE_MINUTES} min — up to {config.MAX_NEW_PER_SOURCE} new \
articles per source, {config.MAX_POSTS_PER_CYCLE} posts per cycle.*\
""")