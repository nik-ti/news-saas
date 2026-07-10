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
# so "strike" isn't shadowed by "s", "strong" by "s", etc.
_ALLOWED_TAG_RE = re.compile(
    r"</?(?:strike|spoiler|strong|code|pre|br|em|b|i|u|s|a)(?:\s[^<>]*)?/?>",
    re.IGNORECASE,
)


def _escape_angles(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def sanitize_telegram_html(text: str) -> str:
    """Escape every angle bracket that isn't part of a valid Telegram HTML tag.

    Article text routinely contains things like "Yield <6%" or "<script".
    Telegram rejects those with 'Unsupported start tag' or 'unexpected end of
    input' — including a lone "<" with no closing bracket. Recognized tags pass
    through untouched; everything else gets escaped.
    """
    out = []
    pos = 0
    for m in _ALLOWED_TAG_RE.finditer(text):
        out.append(_escape_angles(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_escape_angles(text[pos:]))
    return "".join(out)


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
        "text": sanitize_telegram_html(html)[:4096],
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