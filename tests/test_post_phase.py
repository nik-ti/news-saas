"""Phase B on schema v2: summary persistence, terminal statuses, post_html
provenance, quiet hours, semantic dedup, per-stream budget, auto-pause."""
import config
import pipeline.news_cycle as nc
from database import store
from database.models import get_connection
from pipeline.summarize import SKIP


def _queued_delivery(summary="", user_id=42, criteria=None):
    """Stream + source + one queued delivery; returns (article_id, stream_id)."""
    sid = store.create_stream(user_id=user_id, name="s",
                              criteria=criteria or {"topic": "x"})
    src = store.add_source(stream_id=sid, url=f"https://a{sid}.com")
    aid = store.add_article(source_id=src, title="Headline",
                            url=f"https://a{sid}.com/1", summary=summary,
                            content_hash="H1")
    store.create_delivery(aid, sid)
    return aid, sid


def _status(article_id, stream_id):
    conn = get_connection()
    row = conn.execute(
        """SELECT d.status, d.attempts, d.post_html, a.summary
           FROM deliveries d JOIN articles a ON d.article_id = a.id
           WHERE d.article_id = ? AND d.stream_id = ?""",
        (article_id, stream_id)).fetchone()
    conn.close()
    return dict(row)


def _stub_pipeline(monkeypatch, *, summary="A fine summary.", relevant=True,
                   post="<b>Post</b> long enough to pass the length check.",
                   send_result=None, duplicate=False):
    async def fake_summarize(article):
        return summary, article.get("title") or ""

    async def fake_gate(title, summ, profile):
        return relevant, "stub"

    async def fake_write(summ, title="", source_url="", length="standard",
                         language=""):
        return post

    sent = []

    async def fake_send(chat_id, html, reply_markup=None):
        sent.append({"chat_id": chat_id, "html": html,
                     "reply_markup": reply_markup})
        return send_result or {"ok": True}

    async def fake_dup(article_id, stream_id, title, summ):
        return duplicate

    monkeypatch.setattr(nc, "summarize_article", fake_summarize)
    monkeypatch.setattr(nc, "check_relevance", fake_gate)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "send_html_message_async", fake_send)
    monkeypatch.setattr(nc, "_is_semantic_duplicate", fake_dup)
    return sent


async def test_posted_delivery_persists_summary_and_post_html(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    _stub_pipeline(monkeypatch, summary="Computed summary text.",
                   post="<b>The exact post</b> that went out to the user.")

    stats = await nc._post_phase()
    row = _status(aid, sid)
    assert stats["posted"] == 1
    assert row["status"] == "posted"
    assert row["summary"] == "Computed summary text."          # persisted
    assert row["post_html"].startswith("<b>The exact post</b>")  # §3.5


async def test_posts_carry_feedback_buttons(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    sent = _stub_pipeline(monkeypatch)

    await nc._post_phase()
    markup = sent[0]["reply_markup"]
    callbacks = [b["callback_data"] for row in markup["inline_keyboard"]
                 for b in row]
    assert f"fb:{aid}:{sid}:up" in callbacks
    assert f"fb:{aid}:{sid}:down" in callbacks


async def test_unusable_page_gets_unusable_status(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    _stub_pipeline(monkeypatch, summary=SKIP)

    stats = await nc._post_phase()
    assert stats["dropped"] == 1
    assert _status(aid, sid)["status"] == "unusable"


async def test_irrelevant_delivery_status(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    _stub_pipeline(monkeypatch, relevant=False)

    stats = await nc._post_phase()
    assert stats["irrelevant"] == 1
    assert _status(aid, sid)["status"] == "irrelevant"


async def test_semantic_duplicate_status(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    sent = _stub_pipeline(monkeypatch, duplicate=True)

    stats = await nc._post_phase()
    assert stats["duplicate"] == 1
    assert _status(aid, sid)["status"] == "duplicate"
    assert sent == []                                  # nothing was sent


async def test_terminal_send_error_marks_send_failed(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    _stub_pipeline(monkeypatch,
                   send_result={"ok": False, "error_code": 403,
                                "description": "bot was blocked"})

    async def no_admin_msg(chat_id, md, extra_html=""):
        return {"ok": True}
    monkeypatch.setattr(nc, "send_rich_async", no_admin_msg)

    stats = await nc._post_phase()
    assert stats["dropped"] == 1
    assert _status(aid, sid)["status"] == "send_failed"


async def test_auto_pause_after_repeated_send_failures(temp_db, monkeypatch):
    # §3.1: a user who blocked the bot must stop costing crawls + LLM calls.
    sid = store.create_stream(user_id=7, name="s", criteria={"topic": "x"})
    src = store.add_source(stream_id=sid, url="https://a.com")
    for i in range(config.AUTO_PAUSE_SEND_FAILURES):
        aid = store.add_article(source_id=src, title=f"t{i}", url=f"u{i}",
                                content_hash=f"H{i}")
        store.create_delivery(aid, sid)

    _stub_pipeline(monkeypatch,
                   send_result={"ok": False, "error_code": 403,
                                "description": "blocked"})
    admin_notes = []

    async def fake_admin(chat_id, md, extra_html=""):
        admin_notes.append((chat_id, md))
        return {"ok": True}
    monkeypatch.setattr(nc, "send_rich_async", fake_admin)

    await nc._post_phase()                               # tick 1: 2 failures
    await nc._post_phase()                               # tick 2: 3rd failure
    stream = store.get_stream(sid)
    assert stream["status"] == "paused"
    assert any("auto-paused" in md for _, md in admin_notes)
    # …and its (now orphan-subscribed) source is no longer polled:
    assert store.get_active_sources() == []


async def test_quiet_hours_hold_without_burning_attempts(temp_db, monkeypatch):
    # 0-23 covers every hour of the day — the post must be held whatever the
    # wall clock says. (start != end is required by the parser.)
    from datetime import datetime
    hour = datetime.now().hour
    window = f"{hour}-{(hour + 2) % 24}"
    aid, sid = _queued_delivery(criteria={"topic": "x", "quiet_hours": window})
    sent = _stub_pipeline(monkeypatch)

    stats = await nc._post_phase()
    assert stats["held"] == 1
    row = _status(aid, sid)
    assert row["status"] == "new"                      # still queued
    assert row["attempts"] == 0                        # not charged
    assert sent == []


def test_quiet_hours_parser():
    assert nc._parse_quiet_hours({"quiet_hours": "23-8"}) == (23, 8)
    assert nc._parse_quiet_hours({"quiet_hours": "9-17"}) == (9, 17)
    assert nc._parse_quiet_hours({"quiet_hours": ""}) is None
    assert nc._parse_quiet_hours({"quiet_hours": "25-9"}) is None
    assert nc._parse_quiet_hours({"quiet_hours": "9-9"}) is None
    assert nc._parse_quiet_hours({}) is None

    from datetime import datetime
    night = datetime(2026, 7, 13, 3, 0)
    day = datetime(2026, 7, 13, 12, 0)
    assert nc._in_quiet_hours({"quiet_hours": "23-8"}, night) is True
    assert nc._in_quiet_hours({"quiet_hours": "23-8"}, day) is False
    assert nc._in_quiet_hours({}, night) is False


async def test_per_stream_budget_stops_starvation(temp_db, monkeypatch):
    # One noisy stream with 10 queued items, one quiet stream with 1 —
    # the quiet stream must still get its post in the same send tick.
    noisy = store.create_stream(user_id=1, name="noisy", criteria={"topic": "x"})
    src_n = store.add_source(stream_id=noisy, url="https://n.com")
    for i in range(10):
        aid = store.add_article(source_id=src_n, title=f"t{i}", url=f"un{i}",
                                content_hash=f"N{i}")
        store.create_delivery(aid, noisy)
    quiet = store.create_stream(user_id=2, name="quiet", criteria={"topic": "y"})
    src_q = store.add_source(stream_id=quiet, url="https://q.com")
    aid_q = store.add_article(source_id=src_q, title="q", url="uq",
                              content_hash="Q1")
    store.create_delivery(aid_q, quiet)

    sent = _stub_pipeline(monkeypatch)
    monkeypatch.setattr(config, "MAX_POSTS_PER_STREAM_PER_TICK", 5)
    monkeypatch.setattr(config, "MAX_POSTS_PER_TICK", 30)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(nc.asyncio, "sleep", no_sleep)

    stats = await nc._post_phase()
    assert stats["posted"] == 6                        # 5 noisy + 1 quiet
    assert {m["chat_id"] for m in sent} == {1, 2}      # quiet user WAS served


def _queued_count(stream_id):
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM deliveries WHERE stream_id = ? "
        "AND status = 'new'", (stream_id,)).fetchone()["c"]
    conn.close()
    return n


async def test_per_tick_budget_leaves_remainder_queued(temp_db, monkeypatch):
    # 5 queued for one stream, per-tick cap 2: exactly 2 sent this tick,
    # 3 stay 'new', and the next tick sends 2 more.
    sid = store.create_stream(user_id=1, name="s", criteria={"topic": "x"})
    src = store.add_source(stream_id=sid, url="https://s.com")
    for i in range(5):
        aid = store.add_article(source_id=src, title=f"t{i}", url=f"us{i}",
                                content_hash=f"S{i}")
        store.create_delivery(aid, sid)

    sent = _stub_pipeline(monkeypatch)
    monkeypatch.setattr(config, "MAX_POSTS_PER_STREAM_PER_TICK", 2)
    monkeypatch.setattr(config, "MAX_POSTS_PER_TICK", 50)

    stats1 = await nc._post_phase()
    assert stats1["posted"] == 2
    assert _queued_count(sid) == 3

    stats2 = await nc._post_phase()
    assert stats2["posted"] == 2
    assert _queued_count(sid) == 1
    assert len(sent) == 4


async def test_clump_drains_over_ticks_oldest_first(temp_db, monkeypatch):
    # A 6-article clump on one stream drains 2-per-tick over 3 ticks,
    # oldest first (get_queued_deliveries ORDER BY a.fetched_at ASC).
    sid = store.create_stream(user_id=1, name="s", criteria={"topic": "x"})
    src = store.add_source(stream_id=sid, url="https://s.com")
    aids = []
    for i in range(6):
        aid = store.add_article(source_id=src, title=f"t{i}", url=f"uc{i}",
                                summary=f"MARKER{i}", content_hash=f"C{i}")
        store.create_delivery(aid, sid)
        aids.append(aid)
    # fetched_at has second resolution — stagger explicitly so the
    # oldest-first ordering is deterministic.
    conn = get_connection()
    for i, aid in enumerate(aids):
        conn.execute("UPDATE articles SET fetched_at = ? WHERE id = ?",
                     (f"2026-07-0{i + 1} 00:00:00", aid))
    conn.commit(); conn.close()

    async def fake_write(summ, title="", source_url="", length="standard",
                         language=""):
        return f"<b>Post</b> {summ} — long enough to pass the check."

    async def fake_summarize(article):
        # Trust the stored per-article summary so each post carries its marker.
        return article.get("summary") or "", article.get("title") or ""

    sent = _stub_pipeline(monkeypatch)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "summarize_article", fake_summarize)
    monkeypatch.setattr(config, "MAX_POSTS_PER_STREAM_PER_TICK", 2)
    monkeypatch.setattr(config, "MAX_POSTS_PER_TICK", 50)

    for tick in range(3):
        stats = await nc._post_phase()
        assert stats["posted"] == 2, f"tick {tick}"
    markers = [m["html"].split()[1] for m in sent]
    assert markers == [f"MARKER{i}" for i in range(6)]


async def test_global_per_tick_ceiling_across_streams(temp_db, monkeypatch):
    # 3 streams x 2 queued, global per-tick cap 3 → only 3 sent this tick.
    for u in (1, 2, 3):
        sid = store.create_stream(user_id=u, name=f"s{u}", criteria={"topic": "x"})
        src = store.add_source(stream_id=sid, url=f"https://g{u}.com")
        for i in range(2):
            aid = store.add_article(source_id=src, title=f"t{i}",
                                    url=f"ug{u}{i}", content_hash=f"G{u}{i}")
            store.create_delivery(aid, sid)

    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(config, "MAX_POSTS_PER_STREAM_PER_TICK", 2)
    monkeypatch.setattr(config, "MAX_POSTS_PER_TICK", 3)

    stats = await nc._post_phase()
    assert stats["candidates"] == 3
    assert stats["posted"] == 3


async def test_run_news_cycle_is_poll_only(temp_db, monkeypatch):
    # run_news_cycle polls and queues but NEVER sends — delivery is the
    # separate run_send_phase tick's job.
    aid, sid = _queued_delivery()
    sent = _stub_pipeline(monkeypatch)

    async def fake_snap(source):
        return []
    monkeypatch.setattr(nc, "snapshot_source", fake_snap)

    result = await nc.run_news_cycle(force_all_slots=True)
    assert result["skipped"] is False
    assert sent == []                                  # nothing sent
    assert _status(aid, sid)["status"] == "new"        # still queued

    send = await nc.run_send_phase()
    assert send["posted"] == 1                         # the send tick delivers


async def test_retry_budget_exhaustion_marks_dropped(temp_db, monkeypatch):
    aid, sid = _queued_delivery()
    _stub_pipeline(monkeypatch, post="")                 # writer returns nothing

    for _ in range(config.MAX_ARTICLE_ATTEMPTS):
        await nc._post_phase()

    row = _status(aid, sid)
    assert row["status"] == "dropped"
    assert row["attempts"] == config.MAX_ARTICLE_ATTEMPTS


async def test_retry_uses_persisted_summary_without_refetch(temp_db, monkeypatch):
    # Cycle 1: real summarize path persists the summary but the send 5xxes.
    # Cycle 2: summarize_article (the REAL one) must trust the stored summary —
    # no page fetch — and the send succeeds.
    aid, sid = _queued_delivery()
    fetches = []

    async def fake_fetch(url):
        fetches.append(url)
        return {"success": True, "content": "Article body. " * 60,
                "title": "T", "html": "", "links": [], "error": None}

    async def fake_llm(system, user):
        return {"summary": "Computed once, reused on retry. " * 12}  # > 300 chars

    async def fake_gate(title, summ, profile):
        return True, "ok"

    async def fake_write(summ, title="", source_url="", length="standard",
                         language=""):
        return "<b>Post</b> long enough to pass the length check."

    async def fake_dup(article_id, stream_id, title, summ):
        return False

    sends = {"n": 0}

    async def flaky_send(chat_id, html, reply_markup=None):
        sends["n"] += 1
        if sends["n"] == 1:
            return {"ok": False, "error_code": 500, "description": "gateway"}
        return {"ok": True}

    import pipeline.summarize as summ_mod
    monkeypatch.setattr(summ_mod, "fetch_page", fake_fetch)
    monkeypatch.setattr(summ_mod, "chat_json", fake_llm)
    monkeypatch.setattr(nc, "check_relevance", fake_gate)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "send_html_message_async", flaky_send)
    monkeypatch.setattr(nc, "_is_semantic_duplicate", fake_dup)

    stats1 = await nc._post_phase()
    assert stats1["retry"] == 1
    assert len(fetches) == 1                             # crawled once

    stats2 = await nc._post_phase()
    assert stats2["posted"] == 1
    assert len(fetches) == 1                             # NOT crawled again
    assert _status(aid, sid)["status"] == "posted"
