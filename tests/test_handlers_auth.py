"""F2 — admin gate and per-stream ownership checks."""
from types import SimpleNamespace

import config
import bot.handlers as handlers
from database import store


def _update(user_id, chat_id=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id or user_id),
    )


def _capture_sends(monkeypatch):
    sent = []

    async def fake_send(chat_id, markdown, extra_html=""):
        sent.append((chat_id, markdown))
        return {"ok": True}

    monkeypatch.setattr(handlers, "send_rich_async", fake_send)
    return sent


# ── admin_only ────────────────────────────────────────────────────────────────

async def test_admin_only_blocks_other_users(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    sent = _capture_sends(monkeypatch)
    calls = []

    @handlers.admin_only
    async def secret(update, context):
        calls.append(update.effective_user.id)

    await secret(_update(222), SimpleNamespace(args=[]))
    assert calls == []
    assert "restricted" in sent[0][1]

    await secret(_update(111), SimpleNamespace(args=[]))
    assert calls == [111]


async def test_sensitive_commands_are_wrapped():
    # The decorator must actually be applied — a regression here silently
    # re-opens the whole cross-tenant DB to any Telegram user.
    for cmd in (handlers.cmd_sources_all, handlers.cmd_runpipeline,
                handlers.cmd_status, handlers.cmd_testsource):
        assert cmd.__wrapped__ is not None  # functools.wraps marker


# ── stream ownership ──────────────────────────────────────────────────────────

async def test_cmd_sources_rejects_non_owner(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={})
    store.add_source(stream_id=sid, url="https://a.com")

    await handlers.cmd_sources(_update(2), SimpleNamespace(args=[str(sid)]))

    assert len(sent) == 1
    assert "isn't yours" in sent[0][1]


async def test_cmd_sources_allows_owner(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={})
    store.add_source(stream_id=sid, url="https://a.com")

    # Owner path goes on to send the source table + a keyboard message.
    bot_msgs = []

    async def fake_bot_send(chat_id, text, **kw):
        bot_msgs.append(text)

    ctx = SimpleNamespace(args=[str(sid)],
                          bot=SimpleNamespace(send_message=fake_bot_send))
    await handlers.cmd_sources(_update(1), ctx)

    assert any("Sources for Stream" in m for _, m in sent)


async def test_cmd_research_rejects_non_owner(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={"topic": "x"})

    await handlers.cmd_research(_update(2), SimpleNamespace(args=[str(sid)]))

    assert "isn't yours" in sent[0][1]
    # Status untouched — research was NOT started on someone else's stream.
    assert store.get_stream(sid)["status"] == "active"


async def test_admin_bypasses_ownership(temp_db, monkeypatch):
    # The operator can inspect and manage ANY stream (auto-pause tells them to
    # /resumestream streams they don't own).
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    sid = store.create_stream(user_id=1, name="someone elses", criteria={})
    store.update_stream_status(sid, "paused")
    sent = _capture_sends(monkeypatch)

    await handlers.cmd_resumestream(_update(111), SimpleNamespace(args=[str(sid)]))

    assert store.get_stream(sid)["status"] == "active"
    assert any("active again" in m for _, m in sent)


async def test_non_admin_still_rejected_from_others_streams(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    sid = store.create_stream(user_id=1, name="mine", criteria={})
    sent = _capture_sends(monkeypatch)

    await handlers.cmd_pausestream(_update(2), SimpleNamespace(args=[str(sid)]))

    assert store.get_stream(sid)["status"] == "active"   # untouched
    assert "isn't yours" in sent[0][1]


async def test_admin_exempt_from_research_limit(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    monkeypatch.setattr(handlers.store, "get_usage", lambda uid, kind: 999)

    ok_admin, _ = handlers._research_allowed(111)
    ok_user, why = handlers._research_allowed(2)
    assert ok_admin is True
    assert ok_user is False and "research" in why


async def test_start_shows_operator_commands_only_to_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    sent = _capture_sends(monkeypatch)

    await handlers.cmd_start(_update(2), SimpleNamespace(args=[]))
    await handlers.cmd_start(_update(111), SimpleNamespace(args=[]))

    user_text, admin_text = sent[0][1], sent[1][1]
    assert "Operator commands" not in user_text
    assert "/runpipeline" not in user_text
    assert "/sources_all" not in user_text
    assert "Operator commands" in admin_text
    assert "/runpipeline" in admin_text
    assert "/newstream" in user_text            # normal features still listed


async def test_admin_notified_on_new_users_first_stream(temp_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USER_ID", 111)
    sent = _capture_sends(monkeypatch)

    async def no_research(*args, **kwargs):
        return None
    monkeypatch.setattr(handlers, "_run_research_background", no_research)

    async def fake_bot_send(chat_id, text, **kw):
        pass

    def _upd(user_id, name="Ada", username="ada"):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, full_name=name,
                                           username=username),
            effective_chat=SimpleNamespace(id=user_id),
        )

    def _ctx():
        return SimpleNamespace(
            args=[], user_data={"transcript": [
                {"role": "user", "content": "AI chip supply chains"}]},
            bot=SimpleNamespace(send_message=fake_bot_send),
        )

    # First stream by a stranger → operator hears about it.
    await handlers._start_research(_upd(2), _ctx())
    assert any(chat == 111 and "New user" in text for chat, text in sent)

    # Their second stream → no repeat notification.
    sent.clear()
    await handlers._start_research(_upd(2), _ctx())
    assert not any("New user" in text for _, text in sent)

    # The admin's own first stream → no self-notification.
    sent.clear()
    await handlers._start_research(_upd(111), _ctx())
    assert not any("New user" in text for _, text in sent)


async def test_cmd_latest_is_scoped_to_caller(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    s1 = store.create_stream(user_id=1, name="mine", criteria={})
    src1 = store.add_source(stream_id=s1, url="https://a.com")
    store.add_article(source_id=src1, title="Private headline", url="u",
                      content_hash="H")

    await handlers.cmd_latest(_update(2), SimpleNamespace(args=[]))
    assert "No articles" in sent[0][1]           # other user sees nothing

    await handlers.cmd_latest(_update(1), SimpleNamespace(args=[]))
    assert "Private headline" in sent[1][1]      # owner sees their own
