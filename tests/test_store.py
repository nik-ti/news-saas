"""Schema v2 primitives: canonical sources, subscriptions, per-stream dedup,
tenant scoping, and lifecycle cleanup."""
from database import store
from database.models import get_connection


def _mk_stream(user_id=1, name="s"):
    return store.create_stream(user_id=user_id, name=name, criteria={"topic": name})


# ── Canonical sources + subscriptions ─────────────────────────────────────────

def test_same_feed_collapses_to_one_canonical_source(temp_db):
    s1 = _mk_stream(1, "a")
    s2 = _mk_stream(2, "b")
    src1 = store.add_source(stream_id=s1, url="https://tc.com",
                            feed_url="https://tc.com/news", quality_score=80)
    src2 = store.add_source(stream_id=s2, url="https://tc.com",
                            feed_url="https://tc.com/news", quality_score=40)

    assert src1 == src2  # ONE canonical row, not two

    conn = get_connection()
    n_sources = conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    n_subs = conn.execute("SELECT COUNT(*) c FROM stream_sources").fetchone()["c"]
    conn.close()
    assert n_sources == 1
    assert n_subs == 2

    # quality_score is per subscription, not per site.
    assert store.get_sources_by_stream(s1)[0]["quality_score"] == 80
    assert store.get_sources_by_stream(s2)[0]["quality_score"] == 40


def test_upsert_fills_blank_metadata_only(temp_db):
    s1 = _mk_stream()
    store.add_source(stream_id=s1, url="https://a.com", feed_url="https://a.com/f",
                     fetch_method="rss")
    # Second add must not clobber the proven fetch_method.
    store.add_source(stream_id=s1, url="https://a.com", feed_url="https://a.com/f",
                     fetch_method="links", name="Named now")
    src = store.get_sources_by_stream(s1)[0]
    assert src["fetch_method"] == "rss"
    assert src["name"] == "Named now"   # blank was filled


def test_unsubscribe_drops_orphaned_source(temp_db):
    s1, s2 = _mk_stream(1), _mk_stream(2)
    src = store.add_source(stream_id=s1, url="https://a.com")
    store.subscribe(s2, src)

    store.unsubscribe(s1, src)
    assert store.get_source(src) is not None       # s2 still follows it

    store.unsubscribe(s2, src)
    assert store.get_source(src) is None           # orphaned → gone


def test_delete_stream_cleans_up_orphaned_sources(temp_db):
    s1, s2 = _mk_stream(1), _mk_stream(2)
    only_mine = store.add_source(stream_id=s1, url="https://mine.com")
    shared = store.add_source(stream_id=s1, url="https://shared.com")
    store.subscribe(s2, shared)

    store.delete_stream(s1)
    assert store.get_source(only_mine) is None     # nobody follows it → gone
    assert store.get_source(shared) is not None    # s2 still needs it


def test_get_active_sources_skips_paused_streams_sources(temp_db):
    # §3.1: a paused stream's sources were still CRAWLED before, just not posted.
    s1 = _mk_stream(1)
    store.add_source(stream_id=s1, url="https://a.com")
    assert len(store.get_active_sources()) == 1

    store.update_stream_status(s1, "paused")
    assert store.get_active_sources() == []        # no subscriber → no crawl

    store.update_stream_status(s1, "active")
    assert len(store.get_active_sources()) == 1


def test_get_active_sources_returns_distinct_rows(temp_db):
    # The keystone: ten streams following one feed = ONE row to poll.
    src_ids = set()
    for u in range(5):
        sid = _mk_stream(user_id=u)
        src_ids.add(store.add_source(stream_id=sid, url="https://tc.com",
                                     feed_url="https://tc.com/news"))
    assert len(src_ids) == 1
    assert len(store.get_active_sources()) == 1


# ── Per-stream dedup primitives ───────────────────────────────────────────────

def test_stream_seen_hashes_scoped_per_stream(temp_db):
    s1 = _mk_stream(user_id=1, name="a")
    s2 = _mk_stream(user_id=2, name="b")
    src1 = store.add_source(stream_id=s1, url="https://one.com")
    src2 = store.add_source(stream_id=s2, url="https://two.com")

    store.add_article(source_id=src1, title="t", url="u", content_hash="H1")

    assert "H1" in store.stream_seen_hashes(s1)
    # Stream 2 has NOT seen it — it must still be deliverable there.
    assert "H1" not in store.stream_seen_hashes(s2)

    store.add_article(source_id=src2, title="t", url="u", content_hash="H1")
    assert "H1" in store.stream_seen_hashes(s2)


def test_shared_source_hash_seen_by_all_subscribers(temp_db):
    s1, s2 = _mk_stream(1), _mk_stream(2)
    src = store.add_source(stream_id=s1, url="https://one.com")
    store.subscribe(s2, src)
    store.add_article(source_id=src, title="t", url="u", content_hash="H1")

    assert "H1" in store.stream_seen_hashes(s1)
    assert "H1" in store.stream_seen_hashes(s2)


def test_duplicate_insert_returns_existing_id(temp_db):
    s1 = _mk_stream()
    src = store.add_source(stream_id=s1, url="https://one.com")

    first = store.add_article(source_id=src, title="t", url="u", content_hash="H1")
    second = store.add_article(source_id=src, title="t", url="u", content_hash="H1")
    assert first > 0
    assert second == first  # dup insert is a no-op that finds the original

    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM articles WHERE source_id = ?", (src,)
    ).fetchone()["c"]
    conn.close()
    assert n == 1


def test_batch_insert_is_dup_safe(temp_db):
    s1 = _mk_stream()
    src = store.add_source(stream_id=s1, url="https://one.com")
    items = [{"title": "a", "url": "u1", "content_hash": "H1"},
             {"title": "b", "url": "u2", "content_hash": "H2"}]
    ids1 = store.add_articles_batch(src, items)
    ids2 = store.add_articles_batch(src, items)
    assert len(ids1) == 2 and all(i > 0 for i in ids1)
    assert ids2 == ids1                              # second pass finds them


def test_same_hash_allowed_on_different_sources(temp_db):
    s1, s2 = _mk_stream(1), _mk_stream(2)
    src1 = store.add_source(stream_id=s1, url="https://one.com")
    src2 = store.add_source(stream_id=s2, url="https://two.com")
    assert store.add_article(source_id=src1, title="t", url="u", content_hash="H") > 0
    assert store.add_article(source_id=src2, title="t", url="u", content_hash="H") > 0


def test_get_latest_articles_for_user_is_tenant_scoped(temp_db):
    s1 = _mk_stream(user_id=10)
    s2 = _mk_stream(user_id=20)
    src1 = store.add_source(stream_id=s1, url="https://one.com")
    src2 = store.add_source(stream_id=s2, url="https://two.com")
    store.add_article(source_id=src1, title="mine", url="u1", content_hash="A")
    store.add_article(source_id=src2, title="theirs", url="u2", content_hash="B")

    mine = store.get_latest_articles_for_user(10)
    assert [a["title"] for a in mine] == ["mine"]
    theirs = store.get_latest_articles_for_user(20)
    assert [a["title"] for a in theirs] == ["theirs"]
    assert store.get_latest_articles_for_user(99) == []


# ── Deliveries ────────────────────────────────────────────────────────────────

def test_delivery_lifecycle(temp_db):
    s1 = _mk_stream()
    src = store.add_source(stream_id=s1, url="https://a.com")
    aid = store.add_article(source_id=src, title="t", url="u", content_hash="H")

    assert store.create_delivery(aid, s1) is True
    assert store.create_delivery(aid, s1) is False   # idempotent

    store.mark_delivery_posted(aid, s1, "<b>the post</b>")
    d = store.get_delivery(aid, s1)
    assert d["status"] == "posted"
    assert d["post_html"] == "<b>the post</b>"       # §3.5 provenance
    assert d["posted_at"] is not None


def test_send_fail_streak(temp_db):
    s1 = _mk_stream()
    assert store.record_send_result(s1, ok=False) == 1
    assert store.record_send_result(s1, ok=False) == 2
    assert store.record_send_result(s1, ok=True) == 0
    assert store.record_send_result(s1, ok=False) == 1


# ── init_db robustness ────────────────────────────────────────────────────────

def test_init_db_is_idempotent(temp_db):
    # Re-running init_db on an existing DB (with data) must not fail —
    # this is the startup path of every deploy.
    from database.models import init_db
    s1 = _mk_stream()
    src = store.add_source(stream_id=s1, url="https://one.com")
    store.add_article(source_id=src, title="t", url="u", content_hash="H1")
    init_db()
    init_db()
