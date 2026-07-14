"""
Telegram bot handlers — all commands and conversation flow.
Uses python-telegram-bot ConversationHandler for the multi-step /newstream flow.
"""
import asyncio
import functools
import html as html_mod
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
from bot.i18n import t
from bot.messaging import send_rich_async
from research.engine import run_research
from research.feed_finder import find_news_pages
from research.profile_builder import interview_turn, generate_stream_name
from crawler.fetcher import test_source
from pipeline import usage
from pipeline.news_cycle import run_news_cycle

logger = logging.getLogger(__name__)


def _lang(user_id: int) -> str:
    """The user's interface language ('en' | 'ru')."""
    return store.get_ui_lang(user_id)

# ── Conversation state for /newstream ─────────────────────────────────────────
# A single natural interview loop replaces the old fixed-form states.
INTERVIEW = 0


# ── Authorization ──────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID


def admin_only(handler):
    """Restrict a command to the operator (config.ADMIN_USER_ID).

    Commands that expose the whole cross-tenant database or drive the pipeline
    must not be callable by any Telegram user who finds the bot.
    """
    @functools.wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not _is_admin(user.id):
            await send_rich_async(update.effective_chat.id,
                                  "❌ This command is restricted to the operator.")
            return
        return await handler(update, context)
    return wrapped


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /start
# ═══════════════════════════════════════════════════════════════════════════════

_START_ADMIN_EXTRA = """


## Operator commands (only you see these)

| Command | Description |
|---------|-------------|
| `/status` | System stats across all users |
| `/sources_all` | The entire source database |
| `/runpipeline` | Run the news cycle now |
| `/testsource <url>` | Test if a URL is fetchable |

You also bypass the per-user limits and can manage any user's stream by id.\
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Short welcome + guide link + the button menu."""
    user_id = update.effective_user.id
    lang = _lang(user_id)
    _, keyboard = _screen_main(lang)
    await context.bot.send_message(
        update.effective_chat.id,
        t(lang, "start_user", guide=t(lang, "guide_url")),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    if _is_admin(user_id):
        # Operator strings stay English by design.
        await send_rich_async(update.effective_chat.id,
                              "# Operator" + _START_ADMIN_EXTRA)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The full command table (everything is also in /menu as buttons)."""
    user_id = update.effective_user.id
    text = t(_lang(user_id), "help_user", guide=t(_lang(user_id), "guide_url"))
    if _is_admin(user_id):
        text += _START_ADMIN_EXTRA
    await send_rich_async(update.effective_chat.id, text)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The button menu — every common action without typing a command."""
    lang = _lang(update.effective_user.id)
    text, keyboard = _screen_main(lang)
    await context.bot.send_message(update.effective_chat.id, text,
                                   parse_mode="HTML", reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════════════════════
# Menu screens — each builder returns (text, keyboard); navigation edits the
# same message in place, so Back always works.
# ═══════════════════════════════════════════════════════════════════════════════

def _btn(text: str, cb: str = None, url: str = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text, url=url)
    return InlineKeyboardButton(text, callback_data=cb)


def _screen_main(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_btn(t(lang, "btn_menu_newstream"), "menu:newstream")],
        [_btn(t(lang, "btn_menu_streams"), "menu:streams")],
        [_btn(t(lang, "btn_menu_language"), "menu:language"),
         _btn(t(lang, "btn_menu_guide"), url=t(lang, "guide_url"))],
    ]
    return t(lang, "menu_main"), InlineKeyboardMarkup(rows)


_STATUS_ICONS = {"active": "✅", "paused": "⏸", "researching": "🔬"}


def _screen_streams(user_id: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    # The operator sees every user's streams (each tagged with the owner id);
    # everyone else sees only their own.
    admin = _is_admin(user_id)
    streams = store.get_all_streams() if admin else store.get_streams_by_user(user_id)
    rows = []
    for s in streams[:30 if admin else 20]:
        label = f"{_STATUS_ICONS.get(s['status'], '❓')} {s['name'][:38]}"
        if admin:
            label += f" · u{s['user_id']}"
        rows.append([_btn(label, f"menu:stream:{s['id']}")])
    if not streams:
        rows.append([_btn(t(lang, "btn_menu_newstream"), "menu:newstream")])
    rows.append([_btn(t(lang, "btn_back"), "menu:main")])
    if not streams:
        text = t(lang, "menu_streams_empty")
    elif admin:
        text = t(lang, "menu_streams_title_admin", n=len(streams))
    else:
        text = t(lang, "menu_streams_title")
    return text, InlineKeyboardMarkup(rows)


def _post_lang_code(criteria: dict) -> str:
    raw = str((criteria or {}).get("post_language")
              or (criteria or {}).get("language") or "").lower()
    return "ru" if raw.startswith(("ru", "рус")) else "en"


def _screen_stream(stream: dict, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    sid = stream["id"]
    criteria = stream.get("criteria") or {}
    if not isinstance(criteria, dict):
        criteria = {}
    n_sources = len(store.get_sources_by_stream(sid))
    status = (t(lang, f"status_{stream['status']}")
              if stream["status"] in ("active", "paused", "researching")
              else stream["status"])
    length_key = ("word_compact" if criteria.get("post_length") == "compact"
                  else "word_standard")
    text = t(lang, "scr_stream",
             name=html_mod.escape(stream["name"]),
             status=f"{_STATUS_ICONS.get(stream['status'], '')} {status}",
             n_sources=n_sources,
             length=t(lang, length_key),
             language=t(lang, f"lang_name_{_post_lang_code(criteria)}"),
             quiet=criteria.get("quiet_hours") or t(lang, "word_off"))

    toggle = (_btn(t(lang, "btn_resume"), f"menu:resume:{sid}")
              if stream["status"] == "paused"
              else _btn(t(lang, "btn_pause"), f"menu:pause:{sid}"))
    rows = [
        [_btn(t(lang, "btn_sources"), f"menu:sources:{sid}"),
         _btn(t(lang, "btn_add_source"), f"menu:addsrc:{sid}")],
        [toggle, _btn(t(lang, "btn_research"), f"menu:research:{sid}")],
        [_btn(t(lang, "btn_postlen"), f"menu:plen:{sid}"),
         _btn(t(lang, "btn_postlang"), f"menu:slang:{sid}")],
        [_btn(t(lang, "btn_quiet"), f"menu:quiet:{sid}"),
         _btn(t(lang, "btn_delete_stream"), f"menu:delstream:{sid}")],
        [_btn(t(lang, "btn_back"), "menu:streams")],
    ]
    return text, InlineKeyboardMarkup(rows)


def _screen_sources(stream: dict, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    sid = stream["id"]
    sources = store.get_sources_by_stream(sid)
    src_icons = {"active": "✅", "blocked": "🚫", "error": "⚠️"}
    if sources:
        text = t(lang, "menu_sources_title",
                 name=html_mod.escape(stream["name"]))
        rows = [[_btn(f"🗑 {src_icons.get(s['fetch_status'], '❓')} "
                      f"{(s.get('name') or s['url'])[:36]}",
                      f"msrc_del:{sid}:{s['id']}")] for s in sources[:15]]
    else:
        text = t(lang, "menu_sources_empty")
        rows = []
    rows.append([_btn(t(lang, "btn_add_source"), f"menu:addsrc:{sid}")])
    rows.append([_btn(t(lang, "btn_back"), f"menu:stream:{sid}")])
    return text, InlineKeyboardMarkup(rows)


def _screen_quiet(stream: dict, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    sid = stream["id"]
    rows = [
        [_btn("23:00–08:00", f"mquiet:{sid}:23-8"),
         _btn("22:00–09:00", f"mquiet:{sid}:22-9")],
        [_btn("00:00–08:00", f"mquiet:{sid}:0-8"),
         _btn(t(lang, "btn_quiet_off"), f"mquiet:{sid}:off")],
        [_btn(t(lang, "btn_back"), f"menu:stream:{sid}")],
    ]
    return (t(lang, "menu_quiet_title", name=html_mod.escape(stream["name"])),
            InlineKeyboardMarkup(rows))


def _screen_plen(stream: dict, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    sid = stream["id"]
    rows = [
        [_btn(t(lang, "btn_standard"), f"plen:{sid}:standard"),
         _btn(t(lang, "btn_compact"), f"plen:{sid}:compact")],
        [_btn(t(lang, "btn_back"), f"menu:stream:{sid}")],
    ]
    return (t(lang, "menu_plen_title", name=html_mod.escape(stream["name"])),
            InlineKeyboardMarkup(rows))


def _screen_slang(stream: dict, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    sid = stream["id"]
    rows = [
        [_btn(t(lang, "btn_lang_en"), f"slang:{sid}:en"),
         _btn(t(lang, "btn_lang_ru"), f"slang:{sid}:ru")],
        [_btn(t(lang, "btn_back"), f"menu:stream:{sid}")],
    ]
    return (t(lang, "menu_slang_title", name=html_mod.escape(stream["name"])),
            InlineKeyboardMarkup(rows))


def _back_kb(lang: str, cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(t(lang, "btn_back"), cb)]])


def _screen_language(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_btn(t(lang, "btn_lang_en"), "ulang:en"),
         _btn(t(lang, "btn_lang_ru"), "ulang:ru")],
        [_btn(t(lang, "btn_back"), "menu:main")],
    ]
    return t(lang, "lang_pick_ui"), InlineKeyboardMarkup(rows)


async def _safe_edit(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Edit the menu message in place; ignore Telegram's 'not modified' noise."""
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                      disable_web_page_preview=True,
                                      reply_markup=keyboard)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.debug("menu edit failed: %s", e)


async def _owned_stream_for_menu(query, user_id: int, lang: str, sid: str):
    """Parse + ownership-check a stream id from menu callback data."""
    try:
        owns, stream = await _owns_stream(user_id, int(sid))
    except ValueError:
        return None
    if stream is None or not owns:
        await query.edit_message_text(t(lang, "not_your_stream"))
        return None
    return stream


async def _handle_menu_nav(query, context, user_id: int, lang: str,
                           action: str) -> None:
    """Route a `menu:*` button. Each branch edits the message to a new screen."""
    # ── top-level ──────────────────────────────────────────────────────
    if action == "main":
        await _safe_edit(query, *_screen_main(lang))
        return
    if action == "streams":
        await _safe_edit(query, *_screen_streams(user_id, lang))
        return
    if action == "language":
        await _safe_edit(query, *_screen_language(lang))
        return
    # "newstream" is handled by the conversation's callback entry point.

    verb, _, sid = action.partition(":")
    if not sid:
        return
    stream = await _owned_stream_for_menu(query, user_id, lang, sid)
    if stream is None:
        return
    sid_int = stream["id"]

    if verb == "stream":
        await _safe_edit(query, *_screen_stream(stream, lang))
    elif verb == "sources":
        await _safe_edit(query, *_screen_sources(stream, lang))
    elif verb == "quiet":
        await _safe_edit(query, *_screen_quiet(stream, lang))
    elif verb == "plen":
        await _safe_edit(query, *_screen_plen(stream, lang))
    elif verb == "slang":
        await _safe_edit(query, *_screen_slang(stream, lang))
    elif verb == "pause":
        store.update_stream_status(sid_int, "paused")
        await _safe_edit(query, *_screen_stream(store.get_stream(sid_int), lang))
    elif verb == "resume":
        store.update_stream_status(sid_int, "active")
        store.record_send_result(sid_int, ok=True)   # clear auto-pause streak
        await _safe_edit(query, *_screen_stream(store.get_stream(sid_int), lang))
    elif verb == "addsrc":
        if _source_cap_reached(user_id, sid_int):
            await query.edit_message_text(
                t(lang, "limit_sources", max=config.MAX_SOURCES_PER_STREAM))
            return
        context.user_data["addsrc_stream"] = sid_int
        await query.edit_message_text(t(lang, "addsrc_prompt"), parse_mode="HTML")
    elif verb == "research":
        allowed, why = _research_allowed(user_id)
        if not allowed:
            await query.edit_message_text(why)
            return
        _kick_reresearch(user_id, stream, query.message.chat_id, context)
        await _safe_edit(query, *_screen_stream(store.get_stream(sid_int), lang))
    elif verb == "delstream":
        rows = [[_btn(t(lang, "btn_delete_yes"), f"del_stream:{sid_int}"),
                 _btn(t(lang, "btn_delete_keep"), f"menu:stream:{sid_int}")]]
        n = len(store.get_sources_by_stream(sid_int))
        await _safe_edit(query,
                         t(lang, "delete_confirm",
                           name=html_mod.escape(stream["name"]), n=n),
                         InlineKeyboardMarkup(rows))


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /newstream — Conversation flow
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_newstream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the natural intake conversation, in the user's interface language.

    Reachable as a command AND as the menu's "New stream" button — when it's the
    button, clear its loading spinner first.
    """
    if update.callback_query is not None:
        await update.callback_query.answer()
    opener = t(_lang(update.effective_user.id), "interview_opener")
    context.user_data["transcript"] = [{"role": "assistant", "content": opener}]

    await send_rich_async(update.effective_chat.id, opener)
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

    turn = await interview_turn(transcript,
                                ui_lang=_lang(update.effective_user.id))
    transcript.append({"role": "assistant", "content": turn["message"]})

    await send_rich_async(update.effective_chat.id, turn["message"])

    if not turn["enough"]:
        return INTERVIEW

    return await _start_research(update, context)


async def _notify_admin_new_user(user, stream_name: str) -> None:
    """Best-effort heads-up to the operator — must never break stream creation."""
    try:
        handle = f"@{user.username}" if getattr(user, "username", None) else "no username"
        await send_rich_async(
            config.ADMIN_USER_ID,
            f"👋 **New user:** {getattr(user, 'full_name', '') or 'unknown'} "
            f"({handle}, id `{user.id}`) just created their first stream: "
            f"_{stream_name}_",
        )
    except Exception:
        logger.exception("New-user notification failed (non-fatal)")


def _research_allowed(user_id: int) -> tuple[bool, str]:
    """Per-user limits (§3.3): research is a ~100-crawl, ~40-LLM-call operation.
    The admin runs the system and pays its bills — limits don't apply to them."""
    if _is_admin(user_id):
        return True, ""
    if store.get_usage(user_id, "research_run") >= config.RESEARCH_RUNS_PER_DAY:
        return False, t(_lang(user_id), "limit_research",
                        max=config.RESEARCH_RUNS_PER_DAY)
    return True, ""


async def _start_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Compile the conversation and kick off research."""
    user_id = update.effective_user.id
    ui_lang = _lang(user_id)
    if not _is_admin(user_id) and \
            store.count_streams(user_id) >= config.MAX_STREAMS_PER_USER:
        await send_rich_async(
            update.effective_chat.id,
            t(ui_lang, "limit_streams", max=config.MAX_STREAMS_PER_USER))
        return ConversationHandler.END
    allowed, why = _research_allowed(user_id)
    if not allowed:
        await send_rich_async(update.effective_chat.id, why)
        return ConversationHandler.END

    transcript = context.user_data.get("transcript", [])

    # Seed the stream name from the user's first answer; hand the full
    # conversation to the research engine as the criteria.
    first_answer = next(
        (x["content"] for x in transcript if x["role"] == "user"), "New Stream"
    )
    convo_text = "\n".join(
        f"{'User' if x['role'] == 'user' else 'Service'}: {x['content']}"
        for x in transcript
    )
    answers = {"topic": first_answer, "conversation": convo_text}

    # A non-English interface seeds the stream's POST language explicitly
    # (predictable, announced below, overridable via /language <id>). English
    # seeds nothing so the interview-inferred language still applies.
    if ui_lang != "en":
        answers["post_language"] = ui_lang

    is_first_stream = store.count_streams(user_id) == 0

    # A tiny fast-model call turns the user's raw first message into a clean
    # short title (falls back to a trim if the model is unavailable).
    usage.set_user(user_id)
    stream_name = await generate_stream_name(first_answer)
    stream_id = store.create_stream(
        user_id=user_id,
        name=stream_name,
        criteria=answers,  # temporary, replaced with the profile after research
    )
    store.update_stream_status(stream_id, "researching")
    context.user_data["stream_id"] = stream_id

    # The bot is open to anyone on Telegram — tell the operator when a NEW
    # person actually starts using it (first stream, not every stream).
    if is_first_stream and not _is_admin(user_id):
        await _notify_admin_new_user(update.effective_user, stream_name)

    chat_id = update.effective_chat.id
    lang_note = (t(ui_lang, "research_kickoff_lang_note", stream_id=stream_id)
                 if ui_lang == "ru" else "")
    await context.bot.send_message(
        chat_id,
        t(ui_lang, "research_kickoff", lang_note=lang_note),
        reply_markup=_post_length_keyboard(stream_id, ui_lang),
    )

    asyncio.create_task(_run_research_background(stream_id, answers, chat_id,
                                                 context, user_id=user_id))

    return ConversationHandler.END


def _post_length_keyboard(stream_id: int, lang: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "btn_standard"),
                             callback_data=f"plen:{stream_id}:standard"),
        InlineKeyboardButton(t(lang, "btn_compact"),
                             callback_data=f"plen:{stream_id}:compact"),
    ]])


async def cmd_postsize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set how long a stream's posts are."""
    chat_id = update.effective_chat.id
    lang = _lang(update.effective_user.id)
    if not context.args:
        streams = store.get_streams_by_user(update.effective_user.id)
        if not streams:
            await send_rich_async(chat_id, t(lang, "no_streams_yet"))
            return
        await send_rich_async(chat_id, t(lang, "postsize_usage"))
        return
    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, t(lang, "invalid_stream_id"))
        return
    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(chat_id, t(lang, "not_your_stream"))
        return
    current = (stream.get("criteria") or {}).get("post_length", "standard")
    await context.bot.send_message(
        chat_id,
        t(lang, "postsize_pick", name=stream["name"], current=current),
        reply_markup=_post_length_keyboard(stream_id, lang),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /language — interface language, or a stream's post language
# ═══════════════════════════════════════════════════════════════════════════════

def _ui_lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t("en", "btn_lang_en"), callback_data="ulang:en"),
        InlineKeyboardButton(t("en", "btn_lang_ru"), callback_data="ulang:ru"),
    ]])


def _stream_lang_keyboard(stream_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t("en", "btn_lang_en"),
                             callback_data=f"slang:{stream_id}:en"),
        InlineKeyboardButton(t("en", "btn_lang_ru"),
                             callback_data=f"slang:{stream_id}:ru"),
    ]])


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/language` → the bot's interface language for this user.
    `/language <stream_id>` → the POST language of one stream."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    lang = _lang(user_id)

    if not context.args:
        await context.bot.send_message(
            chat_id, t(lang, "lang_pick_ui"), reply_markup=_ui_lang_keyboard())
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, t(lang, "invalid_stream_id"))
        return
    owns, stream = await _owns_stream(user_id, stream_id)
    if stream is None or not owns:
        await send_rich_async(chat_id, t(lang, "not_your_stream"))
        return

    criteria = stream.get("criteria") or {}
    current_code = "ru" if (criteria.get("post_language")
                            or criteria.get("language") or "").lower().startswith(
                                ("ru", "рус")) else "en"
    await context.bot.send_message(
        chat_id,
        t(lang, "lang_pick_stream", name=stream["name"],
          current=t(lang, f"lang_name_{current_code}")),
        reply_markup=_stream_lang_keyboard(stream_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Stream lifecycle: /pausestream, /resumestream, /deletestream, /quiet  (§3.1, §3.6)
# ═══════════════════════════════════════════════════════════════════════════════

async def _owned_stream_from_args(update, context) -> tuple[int | None, dict | None]:
    """Parse `<stream_id>` from args and verify ownership. Replies on failure."""
    chat_id = update.effective_chat.id
    lang = _lang(update.effective_user.id)
    if not context.args:
        await send_rich_async(chat_id, t(lang, "lifecycle_usage"))
        return None, None
    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, t(lang, "invalid_stream_id"))
        return None, None
    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(chat_id, t(lang, "not_your_stream"))
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
        t(_lang(update.effective_user.id), "stream_paused",
          name=stream["name"], stream_id=stream_id))


async def cmd_resumestream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume a paused stream."""
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return
    store.update_stream_status(stream_id, "active")
    store.record_send_result(stream_id, ok=True)  # clear the auto-pause streak
    await send_rich_async(
        update.effective_chat.id,
        t(_lang(update.effective_user.id), "stream_resumed", name=stream["name"]))


async def cmd_deletestream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a stream — destructive, so it asks for one confirmation tap."""
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return
    lang = _lang(update.effective_user.id)
    n_sources = len(store.get_sources_by_stream(stream_id))
    await context.bot.send_message(
        update.effective_chat.id,
        t(lang, "delete_confirm", name=stream["name"], n=n_sources),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_delete_yes"),
                                 callback_data=f"del_stream:{stream_id}"),
            InlineKeyboardButton(t(lang, "btn_delete_keep"),
                                 callback_data="del_stream:cancel"),
        ]]),
    )


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a stream's quiet hours: posts inside the window are held, not lost."""
    chat_id = update.effective_chat.id
    lang = _lang(update.effective_user.id)
    if len(context.args) < 2:
        await send_rich_async(chat_id, t(lang, "quiet_usage"))
        return
    stream_id, stream = await _owned_stream_from_args(update, context)
    if stream_id is None:
        return

    spec = context.args[1].strip().lower()
    if spec in ("off", "none", "clear"):
        store.set_stream_criteria_field(stream_id, "quiet_hours", "")
        await send_rich_async(chat_id, t(lang, "quiet_cleared", name=stream["name"]))
        return

    from pipeline.news_cycle import _parse_quiet_hours
    if _parse_quiet_hours({"quiet_hours": spec}) is None:
        await send_rich_async(chat_id, t(lang, "quiet_bad_spec"))
        return
    store.set_stream_criteria_field(stream_id, "quiet_hours", spec)
    start, end = spec.split("-", 1)
    await send_rich_async(
        chat_id,
        t(lang, "quiet_set", name=stream["name"],
          start=f"{int(start):02d}", end=f"{int(end):02d}"))


async def _run_research_background(stream_id: int, answers: dict, chat_id: int,
                                     context: ContextTypes.DEFAULT_TYPE,
                                     user_id: int = 0):
    """Run research in background, send progress updates."""
    # Attribute every crawl/LLM call in this task to the requesting tenant.
    usage.set_user(user_id)
    store.increment_usage(user_id, "research_run")
    lang = _lang(user_id)
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
            # USER PREFERENCES must survive re-research: they live in criteria,
            # which this replaces. These keys are only ever user-set — the
            # profile builder can't emit them, so nothing is clobbered.
            old = (store.get_stream(stream_id) or {}).get("criteria") or {}
            if isinstance(old, dict):
                for pref in ("post_length", "quiet_hours", "post_language"):
                    if old.get(pref) and not profile.get(pref):
                        profile[pref] = old[pref]
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

            lines = [t(lang, "res_header", n=len(stored), name=stream_name),
                     t(lang, "res_table_header")]
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
                lines.append(t(lang, "res_live", n=len(live)))
            if pending:
                lines.append(t(lang, "res_pending", n=len(pending)))
            if blocked:
                lines.append(t(lang, "res_blocked", n=len(blocked)))
            if active and all((s.get("site_type") == "aggregator") for s in active):
                lines.append(t(lang, "res_all_aggregators"))
            lines.append(t(lang, "res_finetune", stream_id=stream_id))
            await send_rich_async(chat_id, "\n".join(lines))
        else:
            await send_rich_async(chat_id, t(lang, "res_none", stream_id=stream_id))

    except Exception as e:
        logger.exception("Background research failed")
        store.update_stream_status(stream_id, "active")
        await send_rich_async(chat_id, t(lang, "research_error", e=e))


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
    await send_rich_async(update.effective_chat.id,
                          t(_lang(update.effective_user.id), "cancelled"))
    context.user_data.clear()
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /streams
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_streams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List streams. The operator sees everyone's; users see their own."""
    user_id = update.effective_user.id
    lang = _lang(user_id)
    admin = _is_admin(user_id)
    streams = store.get_all_streams() if admin else store.get_streams_by_user(user_id)

    if not streams:
        await send_rich_async(update.effective_chat.id, t(lang, "streams_none"))
        return

    markdown = t(lang, "streams_header_admin") if admin else t(lang, "streams_header")

    for s in streams:
        sources = store.get_sources_by_stream(s["id"])
        active = len([src for src in sources if src["fetch_status"] == "active"])
        name = s["name"][:30]
        status_emoji = {"active": "✅", "researching": "🔬", "paused": "⏸️"}.get(s["status"], "❓")
        status_word = t(lang, f"status_{s['status']}") \
            if s["status"] in ("active", "paused", "researching") else s["status"]
        if admin:
            markdown += (f"| {s['id']} | {name} | u{s['user_id']} | "
                         f"{status_emoji} {status_word} | {active} |\n")
        else:
            markdown += f"| {s['id']} | {name} | {status_emoji} {status_word} | {active} |\n"

    markdown += t(lang, "streams_footer")

    await send_rich_async(update.effective_chat.id, markdown)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /sources <stream_id>
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View sources for a stream."""
    lang = _lang(update.effective_user.id)
    if not context.args:
        await send_rich_async(update.effective_chat.id, t(lang, "sources_usage"))
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, t(lang, "invalid_stream_id"))
        return

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(update.effective_chat.id, t(lang, "not_your_stream"))
        return

    sources = store.get_sources_by_stream(stream_id)

    if not sources:
        await send_rich_async(update.effective_chat.id,
                              t(lang, "sources_none", stream_id=stream_id))
        return

    # The ID column IS the id you pass to /deletesource — never a row number.
    markdown = t(lang, "sources_header", stream_id=stream_id)

    for src in sources:
        name = (src.get("name") or src["url"])[:28]
        score = src.get("quality_score", 0)
        status_icon = {"active": "✅", "blocked": "🚫", "error": "⚠️"}.get(src["fetch_status"], "❓")
        markdown += f"| `{src['id']}` | [{name}]({src['url']}) | {score} | {status_icon} |\n"

    detail_parts = [t(lang, "sources_details")]
    for src in sources:
        site_type = src.get("site_type")
        type_label = (t(lang, f"type_{site_type}")
                      if site_type in ("news_site", "company_blog",
                                       "aggregator", "analysis") else "")
        header = f"\n**`{src['id']}` — {src.get('name') or src['url']}**"
        if type_label:
            header += f"  · {type_label}"
        detail_parts.append(header)
        detail_parts.append(t(lang, "sources_site", url=src["url"]))
        if src.get("feed_url") and src["feed_url"] != src["url"]:
            detail_parts.append(t(lang, "sources_polling", url=src["feed_url"]))
        detail_parts.append(t(lang, "sources_score_status",
                              score=src.get("quality_score", 0),
                              status=src["fetch_status"]))
        if src.get("specific_keywords"):
            detail_parts.append(t(lang, "sources_keywords",
                                  kw=", ".join(src["specific_keywords"])))
        if src.get("description"):
            detail_parts.append(f"- {src['description']}")

    markdown += "\n".join(detail_parts)
    markdown += t(lang, "sources_delete_hint")

    await send_rich_async(update.effective_chat.id, markdown)

    rows = [[InlineKeyboardButton(
        f"🗑 {src['id']} · {_short(src.get('name') or src['url'], 26)}",
        callback_data=f"del_src:{src['id']}")] for src in sources[:10]]
    await context.bot.send_message(
        update.effective_chat.id, t(lang, "sources_tap_delete"),
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
    """Ownership check. The admin passes for EVERY stream — they operate the
    system (and auto-pause tells them to /resumestream streams they don't own)."""
    stream = store.get_stream(stream_id)
    if not stream:
        return False, None
    return _is_admin(user_id) or stream["user_id"] == user_id, stream


async def cmd_addsource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add a source. The user usually pastes a site's front door, so we go and find
    the page that actually lists its articles rather than polling a homepage.
    """
    chat_id = update.effective_chat.id
    lang = _lang(update.effective_user.id)

    if len(context.args) < 2:
        await send_rich_async(chat_id, t(lang, "addsource_usage"))
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(chat_id, t(lang, "invalid_stream_id"))
        return

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None:
        await send_rich_async(chat_id, t(lang, "stream_not_found", stream_id=stream_id))
        return
    if not owns:
        await send_rich_async(chat_id, t(lang, "not_your_stream"))
        return

    if _source_cap_reached(update.effective_user.id, stream_id):
        await send_rich_async(
            chat_id, t(lang, "limit_sources", max=config.MAX_SOURCES_PER_STREAM))
        return

    await _discover_and_add(chat_id, stream_id, context.args[1], lang, context)


def _source_cap_reached(user_id: int, stream_id: int) -> bool:
    return (not _is_admin(user_id)
            and len(store.get_sources_by_stream(stream_id))
            >= config.MAX_SOURCES_PER_STREAM)


async def _discover_and_add(chat_id: int, stream_id: int, raw_url: str,
                            lang: str, context) -> None:
    """Find a site's news page and add it (shared by /addsource and the menu)."""
    url = raw_url.strip()
    if "://" not in url:
        url = f"https://{url}"

    await send_rich_async(chat_id, t(lang, "addsource_looking", url=url))

    try:
        candidates = await find_news_pages(url)
    except Exception:
        logger.exception("Feed discovery failed for %s", url)
        await send_rich_async(chat_id, t(lang, "addsource_inspect_error"))
        return

    # ── Nothing publishable ───────────────────────────────────────────
    if not candidates:
        context.user_data["pending_source"] = {"stream_id": stream_id, "url": url}
        await context.bot.send_message(
            chat_id,
            t(lang, "addsource_none_found", url=url),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(lang, "btn_add_anyway"),
                                     callback_data="feed:force"),
                InlineKeyboardButton(t(lang, "btn_cancel"),
                                     callback_data="feed:cancel"),
            ]]),
        )
        return

    # ── Exactly one → just add it ─────────────────────────────────────
    if len(candidates) == 1:
        await _store_discovered_source(chat_id, stream_id, url, candidates[0],
                                       lang=lang)
        return

    # ── Several → let the user choose ─────────────────────────────────
    context.user_data["feed_candidates"] = {
        "stream_id": stream_id, "site_url": url,
        "cands": [(c.url, c.kind, c.item_count, c.scope) for c in candidates],
    }
    rows = [[InlineKeyboardButton(
        t(lang, "btn_feed_option",
          icon="📡" if c.kind == "feed" else "📄",
          url=_short(c.url), n=c.item_count),
        callback_data=f"feed:{i}")] for i, c in enumerate(candidates)]
    rows.append([InlineKeyboardButton(t(lang, "btn_cancel"),
                                      callback_data="feed:cancel")])

    await context.bot.send_message(
        chat_id,
        t(lang, "addsource_multi", url=url),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_pending_source(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message after tapping “Add source” in the menu is the site URL.

    Only acts when the menu armed it (addsrc_stream set); otherwise ignores the
    message so ordinary chatter isn't captured.
    """
    stream_id = context.user_data.get("addsrc_stream")
    if not stream_id:
        return
    context.user_data.pop("addsrc_stream", None)
    chat_id = update.effective_chat.id
    lang = _lang(update.effective_user.id)
    text = (update.message.text or "").strip()

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(chat_id, t(lang, "not_your_stream"))
        return
    if _source_cap_reached(update.effective_user.id, stream_id):
        await send_rich_async(
            chat_id, t(lang, "limit_sources", max=config.MAX_SOURCES_PER_STREAM))
        return
    # A sanity check so a stray sentence isn't treated as a site.
    if not text or " " in text or "." not in text:
        await send_rich_async(chat_id, t(lang, "addsrc_not_a_url"))
        return

    await _discover_and_add(chat_id, stream_id, text, lang, context)


def _short(url: str, n: int = 34) -> str:
    trimmed = url.replace("https://", "").replace("http://", "").rstrip("/")
    return trimmed if len(trimmed) <= n else trimmed[: n - 1] + "…"


async def _store_discovered_source(chat_id: int, stream_id: int, site_url: str,
                                   cand, lang: str = "en") -> None:
    """Persist a verified news page as a source, keyed on the site's root URL."""
    if store.get_source_by_url(stream_id, site_url):
        await send_rich_async(chat_id, t(lang, "source_dup"))
        return

    name = (cand.title or _short(site_url, 60))[:100]
    if cand.kind == "feed":
        method, kind_key = "rss", "kind_feed"
    elif getattr(cand, "scope", "internal") == "external":
        # Outbound aggregator: the page's headlines link to other domains, so
        # the poller must keep off-domain links for this source.
        method, kind_key = "links_ext", "kind_page_ext"
    else:
        method, kind_key = "links", "kind_page"
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

    await send_rich_async(chat_id, t(
        lang, "source_added", name=name, site=site_url,
        poll=cand.url, kind=t(lang, kind_key), n=cand.item_count))


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /deletesource <source_id>
# ═══════════════════════════════════════════════════════════════════════════════

async def _delete_source_for(user_id: int, source_id: int,
                             lang: str = "en") -> tuple[bool, str]:
    """
    Remove a source from the caller's stream(s). Sources are canonical now —
    "deleting" unsubscribes THIS user's streams; the row itself only goes away
    once nobody follows it anymore.
    """
    src = store.get_source(source_id)
    if not src:
        return False, t(lang, "source_not_found", source_id=source_id)

    # Which of the caller's streams actually follow it?
    subscribed = [
        s["id"] for s in store.get_streams_by_user(user_id)
        if any(x["id"] == source_id for x in store.get_sources_by_stream(s["id"]))
    ]
    if not subscribed:
        return False, t(lang, "source_not_on_streams")

    for sid in subscribed:
        store.unsubscribe(sid, source_id)
    name = src.get("name") or src["url"]
    return True, t(lang, "source_removed", source_id=source_id, name=name)


async def cmd_deletesource(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a source by its database ID."""
    lang = _lang(update.effective_user.id)
    if not context.args:
        await send_rich_async(update.effective_chat.id,
                              t(lang, "deletesource_usage"))
        return

    try:
        source_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id,
                              t(lang, "invalid_source_id"))
        return

    _, msg = await _delete_source_for(update.effective_user.id, source_id,
                                      lang=lang)
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
    lang = _lang(user_id)

    # ── Menu navigation (edits the same message so Back always works) ──
    if data.startswith("menu:"):
        await _handle_menu_nav(query, context, user_id, lang, data[len("menu:"):])
        return

    # ── Remove a source from the menu's Sources screen ────────────────
    if data.startswith("msrc_del:"):
        _, sid, source_id = data.split(":", 2)
        owns, stream = await _owns_stream(user_id, int(sid))
        if stream is None or not owns:
            await query.edit_message_text(t(lang, "not_your_stream"))
            return
        store.unsubscribe(int(sid), int(source_id))
        text, kb = _screen_sources(stream, lang)
        await _safe_edit(query, text, kb)
        return

    # ── Set quiet hours from the menu ─────────────────────────────────
    if data.startswith("mquiet:"):
        _, sid, spec = data.split(":", 2)
        owns, stream = await _owns_stream(user_id, int(sid))
        if stream is None or not owns:
            await query.edit_message_text(t(lang, "not_your_stream"))
            return
        store.set_stream_criteria_field(
            int(sid), "quiet_hours", "" if spec == "off" else spec)
        stream = store.get_stream(int(sid))          # reflect the change
        text, kb = _screen_stream(stream, lang)
        await _safe_edit(query, text, kb)
        return

    # ── Interface language ────────────────────────────────────────────
    if data.startswith("ulang:"):
        choice = data.split(":", 1)[1]
        if choice in ("en", "ru"):
            store.set_ui_lang(user_id, choice)
            # Re-render the menu in the language they just chose.
            await _safe_edit(query, *_screen_main(choice))
        return

    # ── A stream's post language ──────────────────────────────────────
    if data.startswith("slang:"):
        _, sid, choice = data.split(":", 2)
        owns, stream = await _owns_stream(user_id, int(sid))
        if stream is None or not owns:
            await query.edit_message_text(t(lang, "not_your_stream"))
            return
        if choice in ("en", "ru"):
            store.set_stream_criteria_field(int(sid), "post_language", choice)
            await _safe_edit(query, *_screen_stream(store.get_stream(int(sid)), lang))
        return

    # ── Delete a source ───────────────────────────────────────────────
    if data.startswith("del_src:"):
        source_id = int(data.split(":", 1)[1])
        ok, msg = await _delete_source_for(user_id, source_id, lang=lang)
        await query.edit_message_text(
            t(lang, "deleted_short") if ok else t(lang, "delete_failed_short"))
        await send_rich_async(chat_id, msg)
        return

    # ── Delete a stream (confirmation tap from /deletestream) ────────
    if data.startswith("del_stream:"):
        arg = data.split(":", 1)[1]
        if arg == "cancel":
            await query.edit_message_text(t(lang, "kept_nothing_deleted"))
            return
        stream_id = int(arg)
        owns, stream = await _owns_stream(user_id, stream_id)
        if stream is None or not owns:
            await query.edit_message_text(t(lang, "not_your_stream"))
            return
        store.delete_stream(stream_id)
        await query.edit_message_text(t(lang, "stream_deleted", name=stream["name"]))
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
            await query.edit_message_text(t(lang, "not_your_stream"))
            return
        store.set_post_length(int(sid), length)
        await _safe_edit(query, *_screen_stream(store.get_stream(int(sid)), lang))
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
        await query.edit_message_text(t(lang, "cancelled_nothing_added"))
        return

    if data == "feed:force":
        pending = context.user_data.pop("pending_source", None)
        if not pending:
            await query.edit_message_text(t(lang, "choice_expired"))
            return
        # Telegram can redeliver a callback on lag — don't add the source twice.
        if store.get_source_by_url(pending["stream_id"], pending["url"]):
            await query.edit_message_text(t(lang, "source_dup"))
            return
        source_id = store.add_source(
            stream_id=pending["stream_id"], url=pending["url"],
            name=_short(pending["url"], 60), feed_url=pending["url"],
            fetch_status="active",
        )
        await query.edit_message_text(t(lang, "added_as_is"))
        await send_rich_async(chat_id, t(lang, "added_anyway",
                                         source_id=source_id,
                                         url=pending["url"]))
        return

    if data.startswith("feed:"):
        picked = context.user_data.get("feed_candidates")
        if not picked:
            await query.edit_message_text(t(lang, "choice_expired"))
            return
        idx = int(data.split(":", 1)[1])
        if idx >= len(picked["cands"]):
            await query.edit_message_text(t(lang, "option_invalid"))
            return

        chosen = picked["cands"][idx]
        url, kind, count = chosen[0], chosen[1], chosen[2]
        scope = chosen[3] if len(chosen) > 3 else "internal"
        from types import SimpleNamespace
        cand = SimpleNamespace(url=url, kind=kind, item_count=count, title="",
                               scope=scope)
        await query.edit_message_text(t(lang, "following_page", url=_short(url, 48)))
        await _store_discovered_source(chat_id, picked["stream_id"],
                                       picked["site_url"], cand, lang=lang)
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
    lang = _lang(update.effective_user.id)
    if not context.args:
        await send_rich_async(update.effective_chat.id, t(lang, "research_usage"))
        return

    try:
        stream_id = int(context.args[0])
    except ValueError:
        await send_rich_async(update.effective_chat.id, t(lang, "invalid_stream_id"))
        return

    owns, stream = await _owns_stream(update.effective_user.id, stream_id)
    if stream is None or not owns:
        await send_rich_async(update.effective_chat.id, t(lang, "not_your_stream"))
        return

    allowed, why = _research_allowed(update.effective_user.id)
    if not allowed:
        await send_rich_async(update.effective_chat.id, why)
        return

    await send_rich_async(update.effective_chat.id,
                          t(lang, "research_rerun", stream_id=stream_id))
    _kick_reresearch(update.effective_user.id, stream,
                     update.effective_chat.id, context)
    await send_rich_async(update.effective_chat.id, t(lang, "research_started"))


def _kick_reresearch(user_id: int, stream: dict, chat_id: int, context) -> None:
    """Re-run source research for a stream (shared by /research and the menu)."""
    stream_id = stream["id"]
    store.update_stream_status(stream_id, "researching")

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
        stream_id, answers, chat_id, context, user_id=user_id))


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND: /latest
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's latest fetched articles (their streams only)."""
    lang = _lang(update.effective_user.id)
    articles = store.get_latest_articles_for_user(update.effective_user.id, limit=15)

    if not articles:
        await send_rich_async(update.effective_chat.id, t(lang, "latest_none"))
        return

    markdown = t(lang, "latest_header")

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