"""Button menu: navigation, back button, source add/remove, quiet, actions."""
from types import SimpleNamespace

import config
import bot.handlers as handlers
from bot.i18n import t
from database import store


class _Query:
    """Fake CallbackQuery that records the latest edited text + keyboard."""
    def __init__(self, user_id, data, chat_id=None):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat_id=chat_id or user_id)
        self.edits = []
        self.markups = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append(text)
        self.markups.append(reply_markup)

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.markups.append(reply_markup)

    @property
    def last_text(self):
        return self.edits[-1] if self.edits else None

    def buttons(self):
        """Flatten the last keyboard's (text, callback_data) pairs."""
        kb = self.markups[-1]
        if kb is None:
            return []
        return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


async def _press(user_id, data, ctx=None):
    q = _Query(user_id, data)
    update = SimpleNamespace(callback_query=q)
    await handlers.handle_callback(update, ctx or SimpleNamespace(user_data={}))
    return q


def _cbs(q):
    return [cb for _, cb in q.buttons()]


# ── navigation + back ─────────────────────────────────────────────────────────

async def test_menu_main_has_core_entries(temp_db):
    q = await _press(1, "menu:main")
    cbs = _cbs(q)
    assert "menu:newstream" in cbs
    assert "menu:streams" in cbs
    assert "menu:language" in cbs


async def test_streams_list_and_drill_in_and_back(temp_db):
    sid = store.create_stream(user_id=1, name="Crypto", criteria={"topic": "x"})

    q = await _press(1, "menu:streams")
    assert f"menu:stream:{sid}" in _cbs(q)

    q = await _press(1, f"menu:stream:{sid}")
    # Stream screen exposes the management actions + a Back to the list.
    cbs = _cbs(q)
    assert f"menu:sources:{sid}" in cbs
    assert f"menu:addsrc:{sid}" in cbs
    assert f"menu:pause:{sid}" in cbs
    assert "menu:streams" in cbs          # Back

    # Back returns to the stream list.
    q = await _press(1, "menu:streams")
    assert f"menu:stream:{sid}" in _cbs(q)


async def test_menu_rejects_non_owner(temp_db):
    sid = store.create_stream(user_id=1, name="mine", criteria={})
    q = await _press(2, f"menu:stream:{sid}")
    assert q.last_text == t("en", "not_your_stream")


# ── pause / resume from the menu ──────────────────────────────────────────────

async def test_pause_and_resume_toggle(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})

    q = await _press(1, f"menu:pause:{sid}")
    assert store.get_stream(sid)["status"] == "paused"
    # Now the stream screen offers Resume, not Pause.
    assert f"menu:resume:{sid}" in _cbs(q)

    q = await _press(1, f"menu:resume:{sid}")
    assert store.get_stream(sid)["status"] == "active"
    assert f"menu:pause:{sid}" in _cbs(q)


# ── sources screen: remove a source ───────────────────────────────────────────

async def test_sources_screen_lists_and_removes(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com", name="Site A")

    q = await _press(1, f"menu:sources:{sid}")
    assert f"msrc_del:{sid}:{src}" in _cbs(q)

    q = await _press(1, f"msrc_del:{sid}:{src}")
    # Source gone; the screen re-renders without it.
    assert store.get_sources_by_stream(sid) == []
    assert not any(cb.startswith("msrc_del:") for cb in _cbs(q))


# ── add source via the menu (armed flag → next message is the URL) ────────────

async def test_add_source_flow_arms_and_consumes_message(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    ctx = SimpleNamespace(user_data={})

    await _press(1, f"menu:addsrc:{sid}", ctx)
    assert ctx.user_data.get("addsrc_stream") == sid

    called = {}

    async def fake_discover(chat_id, stream_id, raw_url, lang, context):
        called["args"] = (stream_id, raw_url)

    monkeypatch.setattr(handlers, "_discover_and_add", fake_discover)

    async def noop(chat_id, markdown, extra_html=""):
        return {"ok": True}
    monkeypatch.setattr(handlers, "send_rich_async", noop)

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        message=SimpleNamespace(text="techcrunch.com"))
    await handlers.handle_free_text(upd, ctx)

    assert called["args"] == (sid, "techcrunch.com")
    assert "addsrc_stream" not in ctx.user_data       # flag consumed


async def test_pending_source_ignores_unarmed_messages(temp_db, monkeypatch):
    # A normal message with no armed flag must be ignored entirely.
    called = {}

    async def fake_discover(*a, **k):
        called["hit"] = True
    monkeypatch.setattr(handlers, "_discover_and_add", fake_discover)

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        message=SimpleNamespace(text="just chatting"))
    await handlers.handle_free_text(upd, SimpleNamespace(user_data={}))
    assert "hit" not in called


async def test_add_source_rejects_non_url_text(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    ctx = SimpleNamespace(user_data={"addsrc_stream": sid})
    sent = []

    async def cap(chat_id, markdown, extra_html=""):
        sent.append(markdown)
    monkeypatch.setattr(handlers, "send_rich_async", cap)

    called = {}

    async def fake_discover(*a, **k):
        called["hit"] = True
    monkeypatch.setattr(handlers, "_discover_and_add", fake_discover)

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        message=SimpleNamespace(text="please add some tech news"))
    await handlers.handle_free_text(upd, ctx)
    assert "hit" not in called
    assert sent and sent[0] == t("en", "addsrc_not_a_url")


# ── quiet hours from the menu ─────────────────────────────────────────────────

async def test_quiet_hours_set_from_menu(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})

    q = await _press(1, f"menu:quiet:{sid}")
    assert f"mquiet:{sid}:23-8" in _cbs(q)

    await _press(1, f"mquiet:{sid}:23-8")
    assert store.get_stream(sid)["criteria"]["quiet_hours"] == "23-8"

    await _press(1, f"mquiet:{sid}:off")
    assert store.get_stream(sid)["criteria"]["quiet_hours"] == ""


# ── post length / language from the menu return to the stream screen ──────────

async def test_post_length_from_menu(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    await _press(1, f"plen:{sid}:compact")
    assert store.get_stream(sid)["criteria"]["post_length"] == "compact"


async def test_post_language_from_menu(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    q = await _press(1, f"slang:{sid}:ru")
    assert store.get_stream(sid)["criteria"]["post_language"] == "ru"
    # Returns to the stream screen (Back to the list present).
    assert "menu:streams" in _cbs(q)


# ── delete stream from the menu (confirm → delete) ────────────────────────────

async def test_delete_stream_confirm_then_delete(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})

    q = await _press(1, f"menu:delstream:{sid}")
    cbs = _cbs(q)
    assert f"del_stream:{sid}" in cbs              # confirm
    assert f"menu:stream:{sid}" in cbs             # keep (back)

    await _press(1, f"del_stream:{sid}")
    assert store.get_stream(sid) is None


# ── the Russian interface renders the menu in Russian ─────────────────────────

async def test_menu_renders_in_russian(temp_db):
    store.set_ui_lang(5, "ru")
    q = await _press(5, "menu:main")
    assert q.last_text == t("ru", "menu_main")
