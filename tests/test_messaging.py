"""F8 — Telegram HTML sanitizer and safe truncation."""
from bot.messaging import sanitize_telegram_html, _safe_truncate


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
