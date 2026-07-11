"""
Telegram messaging.

Two families:
  * send_rich* — markdown → Rich HTML → sendRichMessage. For bot chrome:
    /start, /status, /streams tables, research progress.
  * send_html_message_async — hand-written Telegram HTML → sendMessage.
    For news posts, which the post writer emits as standard Telegram HTML.
"""
import logging
import re

import httpx
from telegramify_markdown import richify

import config

logger = logging.getLogger(__name__)

TOKEN = config.TELEGRAM_BOT_TOKEN
API_BASE = config.API_BASE

# Telegram's HTML parse_mode accepts only these tags. Longest alternatives first
# so "strike" isn't shadowed by "s", "strong" by "s", etc. Note: <br> and
# <spoiler> are NOT valid Telegram HTML — one <br> from an LLM means a 400 and
# a permanently dropped article. <br> is normalised to a newline instead; the
# spoiler tag Telegram actually accepts is <tg-spoiler>.
_ALLOWED_TAG_RE = re.compile(
    r"</?(?:tg-spoiler|blockquote|strike|strong|code|pre|del|ins|em|b|i|u|s|a)"
    r"(?:\s[^<>]*)?>",
    re.IGNORECASE,
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

# Tags a truncation could leave dangling. Order matters: close inner-most last.
_CLOSEABLE_TAGS = ("a", "code", "pre", "i", "b")


def _escape_angles(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def sanitize_telegram_html(text: str) -> str:
    """Escape every angle bracket that isn't part of a valid Telegram HTML tag.

    Article text routinely contains things like "Yield <6%" or "<script".
    Telegram rejects those with 'Unsupported start tag' or 'unexpected end of
    input' — including a lone "<" with no closing bracket. Recognized tags pass
    through untouched; everything else gets escaped.
    """
    text = _BR_RE.sub("\n", text)
    out = []
    pos = 0
    for m in _ALLOWED_TAG_RE.finditer(text):
        out.append(_escape_angles(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_escape_angles(text[pos:]))
    return "".join(out)


def _safe_truncate(html: str, limit: int = 4096) -> str:
    """Truncate to Telegram's message limit without producing invalid HTML.

    A blind slice can cut inside a tag or leave <b>/<a> unclosed — Telegram
    rejects both, which permanently drops the article. Cut at a tag boundary
    and close anything left dangling.
    """
    if len(html) <= limit:
        return html
    # Leave room for the closing tags we may need to append.
    budget = limit - sum(len(f"</{t}>") for t in _CLOSEABLE_TAGS)
    cut = html[:budget]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:                       # sliced mid-tag
        cut = cut[:lt]
    for tag in _CLOSEABLE_TAGS:
        opens = len(re.findall(rf"<{tag}[\s>]", cut, re.IGNORECASE))
        closes = len(re.findall(rf"</{tag}>", cut, re.IGNORECASE))
        if opens > closes:
            cut += f"</{tag}>"
    return cut


# ── Sync versions ─────────────────────────────────────────────────────────────
# Use ONLY outside PTB async handlers (standalone alerters, background threads, cron).

def send_rich(chat_id: int, markdown: str, extra_html: str = "") -> dict:
    """Markdown → Rich HTML → sendRichMessage. For outbound alerts."""
    base_html = richify(markdown).to_dict().get("html", "")
    resp = httpx.post(
        f"{API_BASE}/sendRichMessage",
        json={"chat_id": chat_id, "rich_message": {"html": base_html + extra_html}},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error("sendRichMessage failed: %s", data)
    return data


def send_rich_html(chat_id: int, html: str) -> dict:
    """Raw Rich HTML → sendRichMessage. For <details>, <sub>, <sup>."""
    resp = httpx.post(
        f"{API_BASE}/sendRichMessage",
        json={"chat_id": chat_id, "rich_message": {"html": html}},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        logger.error("sendRichMessage failed: %s", data)
    return data


# ── Async versions ────────────────────────────────────────────────────────────
# Use inside PTB async handlers (cmd_*, handle_callback, etc.).

async def send_rich_async(chat_id: int, markdown: str, extra_html: str = "") -> dict:
    """Async markdown → Rich HTML → sendRichMessage. For use inside PTB handlers."""
    base_html = richify(markdown).to_dict().get("html", "")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE}/sendRichMessage",
            json={"chat_id": chat_id, "rich_message": {"html": base_html + extra_html}},
            timeout=30,
        )
    data = resp.json()
    if not data.get("ok"):
        logger.error("sendRichMessage failed: %s", data)
    return data


async def send_rich_html_async(chat_id: int, html: str) -> dict:
    """Async raw Rich HTML → sendRichMessage. For PTB handlers with <details> etc."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE}/sendRichMessage",
            json={"chat_id": chat_id, "rich_message": {"html": html}},
            timeout=30,
        )
    data = resp.json()
    if not data.get("ok"):
        logger.error("sendRichMessage failed: %s", data)
    return data


# ── News posts ────────────────────────────────────────────────────────────────

async def send_html_message_async(chat_id: int, html: str,
                                  reply_markup: dict | None = None) -> dict:
    """Send a news post written in Telegram HTML via plain sendMessage.

    Link previews are disabled so the post reads as written, and the text is
    sanitised + truncated to Telegram's 4096-char limit.
    """
    payload = {
        "chat_id": chat_id,
        "text": _safe_truncate(sanitize_telegram_html(html)),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{API_BASE}/sendMessage", json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        logger.error("sendMessage failed: %s", data)
    return data