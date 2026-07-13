"""§3.1 lifecycle commands, §3.3 usage caps, §3.7 feedback, §2.3 retention,
§3.2 semantic dedup, §3.9 language."""
from types import SimpleNamespace

import numpy as np

import config
import bot.handlers as handlers
import pipeline.news_cycle as nc
from database import store
from database.models import get_connection


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


# ── §3.1 stream lifecycle commands ────────────────────────────────────────────

async def test_pausestream_and_resumestream(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={})

    await handlers.cmd_pausestream(_update(1), SimpleNamespace(args=[str(sid)]))
    assert store.get_stream(sid)["status"] == "paused"

    await handlers.cmd_resumestream(_update(1), SimpleNamespace(args=[str(sid)]))
    assert store.get_stream(sid)["status"] == "active"
    assert any("paused" in m for _, m in sent)


async def test_pausestream_rejects_non_owner(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={})

    await handlers.cmd_pausestream(_update(2), SimpleNamespace(args=[str(sid)]))
    assert store.get_stream(sid)["status"] == "active"
    assert "isn't yours" in sent[0][1]


async def test_deletestream_asks_then_callback_deletes(temp_db, monkeypatch):
    _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={})
    store.add_source(stream_id=sid, url="https://a.com")

    asked = []

    async def fake_bot_send(chat_id, text, **kw):
        asked.append((text, kw.get("reply_markup")))

    ctx = SimpleNamespace(args=[str(sid)],
                          bot=SimpleNamespace(send_message=fake_bot_send))
    await handlers.cmd_deletestream(_update(1), ctx)
    assert store.get_stream(sid) is not None           # ask first, delete later
    assert asked and "delete" in asked[0][0].lower()

    # Confirmation tap:
    edits = []

    async def fake_answer():
        pass

    async def fake_edit(text=None, **kw):
        edits.append(text)

    query = SimpleNamespace(
        data=f"del_stream:{sid}", from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat_id=1), answer=fake_answer,
        edit_message_text=fake_edit)
    upd = SimpleNamespace(callback_query=query)
    await handlers.handle_callback(upd, SimpleNamespace(user_data={}))

    assert store.get_stream(sid) is None
    assert store.get_active_sources() == []            # orphan source cleaned up


async def test_deletestream_callback_rejects_non_owner(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="mine", criteria={})

    async def fake_answer():
        pass
    edits = []

    async def fake_edit(text=None, **kw):
        edits.append(text)

    query = SimpleNamespace(
        data=f"del_stream:{sid}", from_user=SimpleNamespace(id=99),
        message=SimpleNamespace(chat_id=99), answer=fake_answer,
        edit_message_text=fake_edit)
    await handlers.handle_callback(SimpleNamespace(callback_query=query),
                                   SimpleNamespace(user_data={}))
    assert store.get_stream(sid) is not None
    assert edits and "isn't yours" in edits[0]


async def test_quiet_command_sets_and_clears(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={"topic": "x"})

    await handlers.cmd_quiet(_update(1), SimpleNamespace(args=[str(sid), "23-8"]))
    assert store.get_stream(sid)["criteria"]["quiet_hours"] == "23-8"

    await handlers.cmd_quiet(_update(1), SimpleNamespace(args=[str(sid), "off"]))
    assert store.get_stream(sid)["criteria"]["quiet_hours"] == ""

    await handlers.cmd_quiet(_update(1), SimpleNamespace(args=[str(sid), "25-9"]))
    assert any("doesn't parse" in m for _, m in sent)


# ── §3.3 usage accounting + caps ──────────────────────────────────────────────

def test_usage_increment_and_readback(temp_db):
    store.increment_usage(7, "llm_call")
    store.increment_usage(7, "llm_call", n=4)
    assert store.get_usage(7, "llm_call") == 5
    assert store.get_usage(7, "crawl") == 0
    assert store.get_usage(8, "llm_call") == 0


def test_usage_contextvar_attribution(temp_db):
    from pipeline import usage
    token = usage.set_user(42)
    try:
        usage.record("crawl")
    finally:
        usage.reset_user(token)
    usage.record("crawl")                              # unattributed → system
    assert store.get_usage(42, "crawl") == 1
    assert store.get_usage(usage.SYSTEM_USER, "crawl") == 1


async def test_research_rate_limit_blocks(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=5, name="mine", criteria={"topic": "x"})
    store.increment_usage(5, "research_run", n=config.RESEARCH_RUNS_PER_DAY)

    await handlers.cmd_research(_update(5), SimpleNamespace(args=[str(sid)]))
    assert store.get_stream(sid)["status"] == "active"  # research NOT started
    assert any("research" in m and "today" in m for _, m in sent)


async def test_sources_per_stream_cap(temp_db, monkeypatch):
    sent = _capture_sends(monkeypatch)
    sid = store.create_stream(user_id=1, name="mine", criteria={})
    monkeypatch.setattr(config, "MAX_SOURCES_PER_STREAM", 2)
    store.add_source(stream_id=sid, url="https://a.com")
    store.add_source(stream_id=sid, url="https://b.com")

    called = []

    async def no_find(url):
        called.append(url)
        return []
    monkeypatch.setattr(handlers, "find_news_pages", no_find)

    await handlers.cmd_addsource(
        _update(1), SimpleNamespace(args=[str(sid), "https://c.com"],
                                    user_data={}))
    assert called == []                                # discovery never ran
    assert any("cap" in m for _, m in sent)


# ── §3.7 feedback ─────────────────────────────────────────────────────────────

async def test_feedback_callback_stores_verdict(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com")
    aid = store.add_article(source_id=src, title="t", url="u", content_hash="H")
    store.create_delivery(aid, sid)
    store.mark_delivery_posted(aid, sid, "<b>x</b>")

    async def fake_answer(*a, **kw):
        pass

    async def fake_edit_markup(**kw):
        pass

    query = SimpleNamespace(
        data=f"fb:{aid}:{sid}:down", from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(chat_id=1), answer=fake_answer,
        edit_message_reply_markup=fake_edit_markup)
    await handlers.handle_callback(SimpleNamespace(callback_query=query),
                                   SimpleNamespace(user_data={}))
    assert store.get_delivery(aid, sid)["verdict"] == "down"


async def test_feedback_from_non_owner_ignored(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com")
    aid = store.add_article(source_id=src, title="t", url="u", content_hash="H")
    store.create_delivery(aid, sid)

    async def fake_answer(*a, **kw):
        pass

    query = SimpleNamespace(
        data=f"fb:{aid}:{sid}:up", from_user=SimpleNamespace(id=999),
        message=SimpleNamespace(chat_id=999), answer=fake_answer)
    await handlers.handle_callback(SimpleNamespace(callback_query=query),
                                   SimpleNamespace(user_data={}))
    assert store.get_delivery(aid, sid)["verdict"] is None


async def test_score_decay_folds_outcomes(temp_db):
    from pipeline.feedback import run_score_decay
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com", quality_score=90)
    # 1 posted, 5 irrelevant → pass rate ~17%; one 👎 on the posted item.
    for i in range(6):
        aid = store.add_article(source_id=src, title=f"t{i}", url=f"u{i}",
                                content_hash=f"H{i}")
        store.create_delivery(aid, sid)
        if i == 0:
            store.mark_delivery_posted(aid, sid, "<b>x</b>")
            store.set_delivery_verdict(aid, sid, "down")
        else:
            store.update_delivery_status(aid, sid, "irrelevant")

    await run_score_decay()
    new_score = store.get_sources_by_stream(sid)[0]["quality_score"]
    assert new_score < 90                              # slid down…
    assert new_score >= 60                             # …gently (EMA)


# ── §2.3 retention ────────────────────────────────────────────────────────────

def test_retention_prunes_dead_rows_keeps_posted_and_queued(temp_db):
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com")

    old_seen = store.add_article(source_id=src, title="b", url="u1",
                                 content_hash="OLD_SEEN")
    old_irr = store.add_article(source_id=src, title="i", url="u2",
                                content_hash="OLD_IRR")
    old_posted = store.add_article(source_id=src, title="p", url="u3",
                                   content_hash="OLD_POSTED")
    old_queued = store.add_article(source_id=src, title="q", url="u4",
                                   content_hash="OLD_QUEUED")
    fresh = store.add_article(source_id=src, title="f", url="u5",
                              content_hash="FRESH")

    store.create_delivery(old_irr, sid)
    store.update_delivery_status(old_irr, sid, "irrelevant")
    store.create_delivery(old_posted, sid)
    store.mark_delivery_posted(old_posted, sid, "<b>x</b>")
    store.create_delivery(old_queued, sid)

    conn = get_connection()
    conn.execute("UPDATE articles SET fetched_at = datetime('now', '-60 days') "
                 "WHERE id != ?", (fresh,))
    conn.commit(); conn.close()

    n = store.prune_old_articles(days=30)
    remaining = {a["content_hash"] for a in
                 store.get_latest_articles_for_user(1, limit=50)}
    assert n == 2
    assert "OLD_SEEN" not in remaining                 # baseline row pruned
    assert "OLD_IRR" not in remaining                  # negative outcome pruned
    assert "OLD_POSTED" in remaining                   # provenance kept
    assert "OLD_QUEUED" in remaining                   # still queued — kept
    assert "FRESH" in remaining


# ── §3.2 story-level semantic dedup ──────────────────────────────────────────

async def test_semantic_duplicate_detection(temp_db, monkeypatch):
    from research import embeddings
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com")

    # Already-posted story with a known vector.
    posted = store.add_article(source_id=src, title="orig", url="u1",
                               content_hash="A")
    vec = np.zeros(8, dtype=np.float32); vec[0] = 1.0
    store.set_article_embedding(posted, vec.tobytes())
    store.create_delivery(posted, sid)
    store.mark_delivery_posted(posted, sid, "<b>x</b>")

    candidate = store.add_article(source_id=src, title="same story", url="u2",
                                  content_hash="B")

    async def near_identical(text):
        v = np.zeros(8, dtype=np.float32); v[0] = 0.99; v[1] = 0.05
        return v.tolist()
    monkeypatch.setattr(embeddings, "embed", near_identical)

    assert await nc._is_semantic_duplicate(candidate, sid, "same story", "s") is True

    async def unrelated(text):
        v = np.zeros(8, dtype=np.float32); v[3] = 1.0
        return v.tolist()
    monkeypatch.setattr(embeddings, "embed", unrelated)
    other = store.add_article(source_id=src, title="different", url="u3",
                              content_hash="C")
    assert await nc._is_semantic_duplicate(other, sid, "different", "s") is False


async def test_semantic_dedup_degrades_when_embeddings_dead(temp_db, monkeypatch):
    from research import embeddings
    sid = store.create_stream(user_id=1, name="s", criteria={})
    src = store.add_source(stream_id=sid, url="https://a.com")
    aid = store.add_article(source_id=src, title="t", url="u", content_hash="H")

    async def dead(text):
        return None
    monkeypatch.setattr(embeddings, "embed", dead)
    assert await nc._is_semantic_duplicate(aid, sid, "t", "s") is False


# ── §3.9 language ─────────────────────────────────────────────────────────────

async def test_post_writer_honours_language(monkeypatch):
    import pipeline.post_writer as pw
    systems = []

    async def fake_llm(system, user, model="post"):
        systems.append(system)
        return "<b>Titel</b>\n\nText."
    monkeypatch.setattr(pw, "chat", fake_llm)

    await pw.write_post("summary", language="German")
    assert "German" in systems[0]
    assert "English-language" not in systems[0]

    await pw.write_post("summary")                     # default stays English
    assert "English-language" in systems[1]


async def test_post_phase_passes_stream_language(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="s",
                              criteria={"topic": "x", "language": "de"})
    src = store.add_source(stream_id=sid, url="https://a.com")
    aid = store.add_article(source_id=src, title="t", url="u", content_hash="H")
    store.create_delivery(aid, sid)

    langs = []

    async def fake_summarize(article):
        return "summary", "t"

    async def fake_gate(title, summ, profile):
        return True, "ok"

    async def fake_write(summ, title="", source_url="", length="standard",
                         language=""):
        langs.append(language)
        return "<b>Post</b> long enough to pass the length check."

    async def fake_send(chat_id, html, reply_markup=None):
        return {"ok": True}

    async def fake_dup(*a):
        return False

    async def no_sleep(_):
        return None

    monkeypatch.setattr(nc, "summarize_article", fake_summarize)
    monkeypatch.setattr(nc, "check_relevance", fake_gate)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "send_html_message_async", fake_send)
    monkeypatch.setattr(nc, "_is_semantic_duplicate", fake_dup)
    monkeypatch.setattr(nc.asyncio, "sleep", no_sleep)

    await nc._post_phase()
    assert langs == ["de"]
