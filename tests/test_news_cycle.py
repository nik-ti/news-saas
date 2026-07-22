"""Phase A on schema v2: shared-source fan-out, per-stream dedup, baseline,
flood guard, circuit breaker, polling tiers, conditional GET."""
import pytest

import config
import pipeline.news_cycle as nc
from database import store
from database.models import get_connection
from pipeline.fetch_news import SourceFetchError, UNCHANGED


def _mk(user_id, url, baselined=True):
    """Create stream + source; optionally mark the source baselined."""
    sid = store.create_stream(user_id=user_id, name=url, criteria={"topic": url})
    src = store.add_source(stream_id=sid, url=url)
    if baselined:
        store.mark_source_baselined(src)
    return sid, src


def _snapshots(mapping):
    """snapshot_source stub: source url -> items list (or Exception to raise)."""
    async def fake(source):
        result = mapping[source["url"]]
        if isinstance(result, Exception):
            raise result
        return result
    return fake


def _item(h, title="Title long enough to pass"):
    return {"title": title, "url": f"https://x.com/{h}", "summary": "",
            "content_hash": h}


def _delivery_statuses(stream_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.content_hash, d.status FROM deliveries d
           JOIN articles a ON d.article_id = a.id WHERE d.stream_id = ?""",
        (stream_id,)).fetchall()
    conn.close()
    return {r["content_hash"]: r["status"] for r in rows}


def _article_hashes(source_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT content_hash FROM articles WHERE source_id = ?", (source_id,)
    ).fetchall()
    conn.close()
    return {r["content_hash"] for r in rows}


# ── Fan-out on the shared canonical source (the §2.1 keystone) ────────────────

async def test_shared_source_polled_once_delivered_to_both(temp_db, monkeypatch):
    s1 = store.create_stream(user_id=1, name="a", criteria={})
    s2 = store.create_stream(user_id=2, name="b", criteria={})
    src = store.add_source(stream_id=s1, url="https://tc.com")
    store.subscribe(s2, src)
    store.mark_source_baselined(src)

    polls = []
    async def fake_snap(source):
        polls.append(source["id"])
        return [_item("STORY")]
    monkeypatch.setattr(nc, "snapshot_source", fake_snap)

    baselined, queued = await nc._baseline_and_fetch_phase()
    assert polls == [src]                    # ONE crawl for two subscribers
    assert queued == 2
    assert _delivery_statuses(s1) == {"STORY": "new"}
    assert _delivery_statuses(s2) == {"STORY": "new"}


async def test_same_article_delivered_to_both_streams(temp_db, monkeypatch):
    s1, srcA = _mk(1, "https://a.com")
    s2, srcB = _mk(2, "https://b.com")
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("SHARED")],
        "https://b.com": [_item("SHARED")],
    }))

    baselined, queued = await nc._baseline_and_fetch_phase()
    assert baselined == 0
    assert queued == 2                       # ← the old global dedup gave 1
    assert _delivery_statuses(s1) == {"SHARED": "new"}
    assert _delivery_statuses(s2) == {"SHARED": "new"}


async def test_intra_stream_cross_source_dedup(temp_db, monkeypatch):
    # One stream, two sources listing the same story: exactly one queued.
    sid = store.create_stream(user_id=1, name="s", criteria={})
    srcA = store.add_source(stream_id=sid, url="https://a.com")
    srcB = store.add_source(stream_id=sid, url="https://b.com")
    store.mark_source_baselined(srcA)
    store.mark_source_baselined(srcB)
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("SAME")],
        "https://b.com": [_item("SAME")],
    }))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 1


async def test_already_seen_hash_not_requeued(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com")
    aid = store.add_article(source_id=src, title="t", url="u", content_hash="OLD")
    store.create_delivery(aid, sid)
    store.mark_delivery_posted(aid, sid, "<b>x</b>")
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("OLD"), _item("NEW")],
    }))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 1
    statuses = _delivery_statuses(sid)
    assert statuses["NEW"] == "new"
    assert statuses["OLD"] == "posted"  # untouched


async def test_first_poll_baselines_silently(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com", baselined=False)
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("H1"), _item("H2")],
    }))

    baselined, queued = await nc._baseline_and_fetch_phase()
    assert baselined == 1 and queued == 0
    assert _article_hashes(src) == {"H1", "H2"}      # recorded…
    assert _delivery_statuses(sid) == {}             # …but nothing queued
    assert store.get_source(src)["baselined_at"] is not None


async def test_late_subscriber_gets_no_backfill(temp_db, monkeypatch):
    # A stream subscribing to an established source must not receive that
    # source's history — only what appears after it joined.
    s1, src = _mk(1, "https://a.com")
    store.add_article(source_id=src, title="old", url="u", content_hash="OLD")

    s2 = store.create_stream(user_id=2, name="late", criteria={})
    store.subscribe(s2, src)
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("OLD"), _item("FRESH")],
    }))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 2                               # FRESH → both streams
    assert "OLD" not in _delivery_statuses(s2)
    assert _delivery_statuses(s2) == {"FRESH": "new"}


# ── Flood guard ───────────────────────────────────────────────────────────────

def test_looks_like_rebaseline_boundaries():
    assert nc._looks_like_rebaseline(8, 10) is True          # 80% of 10
    assert nc._looks_like_rebaseline(7, 10) is False         # below min items
    assert nc._looks_like_rebaseline(8, 20) is False         # only 40% fresh
    assert nc._looks_like_rebaseline(0, 0) is False
    assert nc._looks_like_rebaseline(50, 50) is True


async def test_wholesale_change_rebaselines_instead_of_posting(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com")
    items = [_item(f"H{i}") for i in range(10)]              # 10/10 suddenly new
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({"https://a.com": items}))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 0
    assert len(_article_hashes(src)) == 10                   # recorded silently
    assert _delivery_statuses(sid) == {}


async def test_normal_trickle_still_queued(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com")
    for i in range(8):                                       # 8 known, 2 new
        store.add_article(source_id=src, title="t", url=f"u{i}",
                          content_hash=f"K{i}")
    items = [_item(f"K{i}") for i in range(8)] + [_item("N1"), _item("N2")]
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({"https://a.com": items}))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 2


# ── Circuit breaker ───────────────────────────────────────────────────────────

async def test_systemic_failure_does_not_count_against_sources(temp_db, monkeypatch):
    urls = [f"https://s{i}.com" for i in range(4)]
    srcs = [_mk(1, u)[1] for u in urls]
    # 3 of 4 fail — that's systemic (dead browser), not the sources' fault.
    mapping = {u: SourceFetchError("boom") for u in urls[:3]}
    mapping[urls[3]] = [_item("OK")]
    monkeypatch.setattr(nc, "snapshot_source", _snapshots(mapping))

    resets = []
    async def fake_reset():
        resets.append(1)
    import crawler.fetcher as cf
    monkeypatch.setattr(cf, "_reset_crawler", fake_reset)

    await nc._baseline_and_fetch_phase()

    assert resets == [1]                                     # crawler was reset
    for src in srcs[:3]:
        assert (store.get_source(src)["fail_count"] or 0) == 0


async def test_isolated_failure_still_counts(temp_db, monkeypatch):
    urls = [f"https://s{i}.com" for i in range(4)]
    srcs = [_mk(1, u)[1] for u in urls]
    mapping = {u: [_item(f"H-{u}")] for u in urls[1:]}
    mapping[urls[0]] = SourceFetchError("just this one")
    monkeypatch.setattr(nc, "snapshot_source", _snapshots(mapping))

    await nc._baseline_and_fetch_phase()
    assert store.get_source(srcs[0])["fail_count"] == 1
    assert store.get_source(srcs[0])["fetch_status"] == "active"  # not yet 3


async def test_third_isolated_failure_deactivates(temp_db, monkeypatch):
    urls = [f"https://s{i}.com" for i in range(4)]
    srcs = [_mk(1, u)[1] for u in urls]
    mapping = {u: [_item(f"H-{u}")] for u in urls[1:]}
    mapping[urls[0]] = SourceFetchError("persistent")
    monkeypatch.setattr(nc, "snapshot_source", _snapshots(mapping))

    for _ in range(config.MAX_CONSECUTIVE_FETCH_FAILURES):
        await nc._baseline_and_fetch_phase()
    assert store.get_source(srcs[0])["fetch_status"] == "error"


# ── §2.6: conditional GET + polling tiers ─────────────────────────────────────

async def test_unchanged_snapshot_resets_failcount_and_stops(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com")
    conn = get_connection()
    conn.execute("UPDATE sources SET fail_count = 2 WHERE id = ?", (src,))
    conn.commit(); conn.close()

    async def fake_snap(source):
        return UNCHANGED
    monkeypatch.setattr(nc, "snapshot_source", fake_snap)

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 0
    s = store.get_source(src)
    assert s["fail_count"] == 0                        # a 304 is a healthy fetch
    assert s["last_fetched"] is not None


def test_due_for_poll_tiers():
    rss = {"fetch_method": "rss", "pub_frequency": "monthly",
           "last_fetched": "2026-07-13 00:00:00"}
    assert nc._due_for_poll(rss) is True               # RSS always polls (304s)

    from datetime import datetime, timezone
    now = datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc)
    recent = {"fetch_method": "links", "pub_frequency": "monthly",
              "last_fetched": "2026-07-13 00:00:00"}
    assert nc._due_for_poll(recent, now) is False      # 2h < 12h tier

    stale = {"fetch_method": "links", "pub_frequency": "monthly",
             "last_fetched": "2026-07-12 00:00:00"}
    assert nc._due_for_poll(stale, now) is True        # 26h > 12h tier

    daily = {"fetch_method": "links", "pub_frequency": "daily",
             "last_fetched": "2026-07-13 01:59:00"}
    assert nc._due_for_poll(daily, now) is True        # daily tier never waits

    never = {"fetch_method": "links", "pub_frequency": "weekly",
             "last_fetched": None}
    assert nc._due_for_poll(never, now) is True        # unbaselined: always due


def test_due_for_poll_slot_gate():
    # With the 10-min poll tick, browser sources are hash-slotted by id so
    # each is still crawled only every ~POLL_TICK_MINUTES * POLL_SLOTS min.
    daily = {"id": 6, "fetch_method": "links", "pub_frequency": "daily",
             "last_fetched": "2026-07-13 01:59:00"}
    assert daily["id"] % config.POLL_SLOTS == 0
    assert nc._due_for_poll(daily, slot=0) is True     # its slot: due
    assert nc._due_for_poll(daily, slot=1) is False    # not its slot: skipped
    assert nc._due_for_poll(daily, slot=None) is True  # manual run: no gate

    # RSS bypasses slotting — a conditional GET usually costs one 304.
    rss = {"id": 6, "fetch_method": "rss", "pub_frequency": "daily",
           "last_fetched": "2026-07-13 01:59:00"}
    assert nc._due_for_poll(rss, slot=1) is True

    # Tier gating still applies WITHIN the slot.
    from datetime import datetime, timezone
    now = datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc)
    weekly_recent = {"id": 6, "fetch_method": "links", "pub_frequency": "weekly",
                     "last_fetched": "2026-07-13 00:00:00"}
    assert nc._due_for_poll(weekly_recent, now, slot=0) is False


def test_current_slot_is_deterministic():
    # Slot derives from wall clock: stable within a tick, cycles through all
    # POLL_SLOTS buckets on successive ticks.
    base = 1_752_000_000                               # fixed epoch
    tick = config.POLL_TICK_MINUTES * 60
    assert nc._current_slot(base) == nc._current_slot(base + tick - 1)
    slots = {nc._current_slot(base + i * tick) for i in range(config.POLL_SLOTS)}
    assert slots == set(range(config.POLL_SLOTS))
    assert nc._current_slot(base + config.POLL_SLOTS * tick) == nc._current_slot(base)


async def test_every_source_polled_once_across_slots(temp_db, monkeypatch):
    # 3 daily sources, ids 1-3 → one slot each; over 3 ticks each is polled
    # exactly once.
    srcs = [_mk(1, f"https://s{i}.com")[1] for i in range(3)]
    assert {s % config.POLL_SLOTS for s in srcs} == {0, 1, 2}

    polls = []
    async def fake_snap(source):
        polls.append(source["id"])
        return []
    monkeypatch.setattr(nc, "snapshot_source", fake_snap)

    for slot in range(config.POLL_SLOTS):
        await nc._baseline_and_fetch_phase(slot=slot)
    assert sorted(polls) == sorted(srcs)               # each exactly once
