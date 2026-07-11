"""F8 — Telegram HTML sanitizer and safe truncation. C5 — unified transport."""
import bot.messaging as msg
from bot.messaging import sanitize_telegram_html, _safe_truncate


# ── C5: _api transport ────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        return _FakeResponse(self.data)


async def test_api_posts_method_and_returns_payload(monkeypatch):
    client = _FakeClient({"ok": True, "result": {"message_id": 7}})
    monkeypatch.setattr(msg, "_get_client", lambda: client)

    data = await msg._api("sendMessage", {"chat_id": 1, "text": "hi"})
    assert data["ok"] is True
    url, payload = client.calls[0]
    assert url.endswith("/sendMessage")
    assert payload == {"chat_id": 1, "text": "hi"}


async def test_api_returns_error_payload_without_raising(monkeypatch):
    client = _FakeClient({"ok": False, "error_code": 400, "description": "bad"})
    monkeypatch.setattr(msg, "_get_client", lambda: client)

    data = await msg._api("sendMessage", {"chat_id": 1, "text": "x"})
    assert data["error_code"] == 400   # caller decides; no exception


async def test_send_html_message_routes_through_api(monkeypatch):
    client = _FakeClient({"ok": True})
    monkeypatch.setattr(msg, "_get_client", lambda: client)

    await msg.send_html_message_async(5, "<b>post</b> & Yield <6%")
    url, payload = client.calls[0]
    assert url.endswith("/sendMessage")
    assert payload["text"] == "<b>post</b> & Yield &lt;6%"   # sanitized in transit
    assert payload["parse_mode"] == "HTML"


# ── sanitize_telegram_html ────────────────────────────────────────────────────

def test_br_becomes_newline():
    # Telegram has no <br>; passing one through means a 400 and a dropped article.
    assert sanitize_telegram_html("a<br>b") == "a\nb"
    assert sanitize_telegram_html("a<br/>b") == "a\nb"
    assert sanitize_telegram_html("a<BR />b") == "a\nb"


def test_spoiler_tag_is_escaped_tg_spoiler_passes():
    assert sanitize_telegram_html("<spoiler>x</spoiler>") == \
        "&lt;spoiler&gt;x&lt;/spoiler&gt;"
    assert sanitize_telegram_html("<tg-spoiler>x</tg-spoiler>") == \
        "<tg-spoiler>x</tg-spoiler>"


def test_blockquote_passes():
    assert sanitize_telegram_html("<blockquote>q</blockquote>") == \
        "<blockquote>q</blockquote>"


def test_stray_angle_brackets_escaped():
    assert sanitize_telegram_html("Yield <6% now") == "Yield &lt;6% now"
    assert sanitize_telegram_html("<script>bad()</script>") == \
        "&lt;script&gt;bad()&lt;/script&gt;"


def test_allowed_tags_untouched():
    html = '<b>Bold</b> <i>it</i> <a href="https://x.com">link</a> <code>c</code>'
    assert sanitize_telegram_html(html) == html


def test_lone_open_bracket_escaped():
    assert sanitize_telegram_html("price < value") == "price &lt; value"


# ── _safe_truncate ────────────────────────────────────────────────────────────

def test_short_text_unchanged():
    assert _safe_truncate("<b>x</b>", 4096) == "<b>x</b>"


def test_never_cuts_mid_tag():
    # Build a string whose blind slice would land inside the <a> tag.
    long_url = "https://example.com/" + "x" * 200
    html = "y" * 90 + f'<a href="{long_url}">link</a>'
    out = _safe_truncate(html, 120)
    assert len(out) <= 120
    # No unterminated '<' at the end
    assert out.count("<") == out.count(">") or out.rfind("<") < out.rfind(">")


def test_closes_dangling_bold():
    html = "<b>" + "word " * 2000  # opened, never closed, way over limit
    out = _safe_truncate(html, 4096)
    assert len(out) <= 4096
    assert out.count("<b>") == out.count("</b>")


def test_closes_dangling_anchor():
    html = '<a href="https://x.com">' + "t" * 5000
    out = _safe_truncate(html, 4096)
    assert len(out) <= 4096
    assert out.count("<a") == out.count("</a>")


def test_result_within_limit():
    out = _safe_truncate("z" * 10000, 4096)
    assert len(out) <= 4096
