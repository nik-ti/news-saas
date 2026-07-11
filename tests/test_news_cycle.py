"""F1 (per-stream delivery), F3 (circuit breaker), F6 (flood guard) — integration
against a real temp SQLite DB with only snapshot_source stubbed."""
import pytest

import config
import pipeline.news_cycle as nc
from database import store
from database.models import get_connection
from pipeline.fetch_news import SourceFetchError


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


def _statuses(source_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT content_hash, status FROM articles WHERE source_id = ?", (source_id,)
    ).fetchall()
    conn.close()
    return {r["content_hash"]: r["status"] for r in rows}


# ── F1: per-stream delivery ───────────────────────────────────────────────────

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
    assert _statuses(srcA) == {"SHARED": "new"}
    assert _statuses(srcB) == {"SHARED": "new"}


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
    store.add_article(source_id=src, title="t", url="u",
                      content_hash="OLD", status="posted")
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("OLD"), _item("NEW")],
    }))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 1
    assert _statuses(src)["NEW"] == "new"
    assert _statuses(src)["OLD"] == "posted"  # untouched


async def test_first_poll_baselines_silently(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com", baselined=False)
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({
        "https://a.com": [_item("H1"), _item("H2")],
    }))

    baselined, queued = await nc._baseline_and_fetch_phase()
    assert baselined == 1 and queued == 0
    assert set(_statuses(src).values()) == {"seen"}
    assert store.get_source(src)["baselined_at"] is not None


# ── F6: flood guard ───────────────────────────────────────────────────────────

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
    statuses = _statuses(src)
    assert len(statuses) == 10
    assert set(statuses.values()) == {"seen"}


async def test_normal_trickle_still_queued(temp_db, monkeypatch):
    sid, src = _mk(1, "https://a.com")
    for i in range(8):                                       # 8 known, 2 new
        store.add_article(source_id=src, title="t", url=f"u{i}",
                          content_hash=f"K{i}", status="seen")
    items = [_item(f"K{i}") for i in range(8)] + [_item("N1"), _item("N2")]
    monkeypatch.setattr(nc, "snapshot_source", _snapshots({"https://a.com": items}))

    _, queued = await nc._baseline_and_fetch_phase()
    assert queued == 2


# ── F3: circuit breaker ───────────────────────────────────────────────────────

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
