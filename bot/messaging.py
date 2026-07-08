"""
Telegram messaging — four sending functions (sync + async pairs).
Follows the telegram-bot skill: always uses sendRichMessage.
"""
import logging

import httpx
from telegramify_markdown import richify

import config

logger = logging.getLogger(__name__)

TOKEN = config.TELEGRAM_BOT_TOKEN
API_BASE = config.API_BASE


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