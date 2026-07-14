"""Clean stream titles via a tiny model call, and the admin all-streams view."""
from types import SimpleNamespace

import config
import bot.handlers as handlers
import research.profile_builder as pb
from bot.i18n import t
from database import store


# ── generate_stream_name ──────────────────────────────────────────────────────

def test_clean_stream_name_strips_noise():
    assert pb._clean_stream_name('"EU Crypto Regulation"') == "EU Crypto Regulation"
    assert pb._clean_stream_name("Title: AI Startups") == "AI Startups"
    assert pb._clean_stream_name("Название: ИИ и стартапы") == "ИИ и стартапы"
    assert pb._clean_stream_name("`Space Launches`\nextra line") == "Space Launches"
    # A rambly sentence isn't a title.
    assert pb._clean_stream_name(
        "Here is a good title that goes on and on and on and on forever") == ""
    assert pb._clean_stream_name("") == ""


async def test_generate_stream_name_uses_model(monkeypatch):
    async def fake_chat(system, user, model="fast"):
        return "  EU Crypto Regulation  "
    monkeypatch.setattr(pb, "chat", fake_chat)
    assert await pb.generate_stream_name(
        "keep me posted on EU crypto rules, MiCA etc") == "EU Crypto Regulation"


async def test_generate_stream_name_falls_back_on_error(monkeypatch):
    async def boom(system, user, model="fast"):
        raise RuntimeError("model down")
    monkeypatch.setattr(pb, "chat", boom)
    topic = "every 100 years the calendar shifts and I want to follow it closely"
    name = await pb.generate_stream_name(topic)
    assert name == topic[:50].rstrip()   # trimmed fallback, never blank
    assert name and len(name) <= 50


async def test_generate_stream_name_falls_back_on_junk(monkeypatch):
    async def junk(system, user, model="fast"):
        return "Sure! Here is a very long rambling answer that is clearly not a title at all"
    monkeypatch.setattr(pb, "chat", junk)
    assert await pb.generate_stream_name("crypto news") == "crypto news"


async def test_newstream_stores_the_generated_name(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 999)

    async def fake_name(topic):
        return "Clean Title"
    monkeypatch.setattr(handlers, "generate_stream_name", fake_name)

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(handlers, "_run_research_background", noop)

    async def cap(chat_id, markdown, extra_html=""):
        return {"ok": True}
    monkeypatch.setattr(handlers, "send_rich_async", cap)

    async def bot_send(chat_id, text, **kw):
        pass

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=5, full_name="A", username="a"),
        effective_chat=SimpleNamespace(id=5))
    ctx = SimpleNamespace(
        user_data={"transcript": [
            {"role": "user", "content": "a long rambling request about space"}]},
        bot=SimpleNamespace(send_message=bot_send))

    await handlers._start_research(upd, ctx)
    assert store.get_streams_by_user(5)[0]["name"] == "Clean Title"


# ── admin sees all streams ────────────────────────────────────────────────────

async def test_admin_streams_screen_shows_all_users(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    store.create_stream(user_id=1, name="Alice topic", criteria={})
    store.create_stream(user_id=2, name="Bob topic", criteria={})

    admin_text, admin_kb = handlers._screen_streams(111, "en")
    cbs = [b.callback_data for row in admin_kb.inline_keyboard for b in row]
    # Both users' streams are reachable, tagged with owner ids.
    labels = " ".join(b.text for row in admin_kb.inline_keyboard for b in row)
    assert "Alice topic" in labels and "Bob topic" in labels
    assert "u1" in labels and "u2" in labels
    assert any(cb and cb.startswith("menu:stream:") for cb in cbs)

    # A normal user sees only their own.
    _, user_kb = handlers._screen_streams(2, "en")
    ulabels = " ".join(b.text for row in user_kb.inline_keyboard for b in row)
    assert "Bob topic" in ulabels
    assert "Alice topic" not in ulabels


async def test_admin_can_open_any_users_stream(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    sid = store.create_stream(user_id=1, name="Alice topic", criteria={})

    edits = []

    async def _answer(*a, **k):
        pass

    async def _edit(text, **kw):
        edits.append(text)

    # The admin taps user 1's stream in the menu — must open, not be rejected.
    query = SimpleNamespace(
        data=f"menu:stream:{sid}", from_user=SimpleNamespace(id=111),
        message=SimpleNamespace(chat_id=111),
        answer=_answer, edit_message_text=_edit)
    await handlers.handle_callback(SimpleNamespace(callback_query=query),
                                   SimpleNamespace(user_data={}))
    assert edits and "Alice topic" in edits[-1]


async def test_cmd_streams_admin_lists_all(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    store.create_stream(user_id=1, name="Alice topic", criteria={})
    store.create_stream(user_id=2, name="Bob topic", criteria={})
    sent = []

    async def cap(chat_id, markdown, extra_html=""):
        sent.append(markdown)
    monkeypatch.setattr(handlers, "send_rich_async", cap)

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=111))
    await handlers.cmd_streams(upd, SimpleNamespace(args=[]))
    assert "Alice topic" in sent[0] and "Bob topic" in sent[0]
    assert "Owner" in sent[0]                                # admin header
