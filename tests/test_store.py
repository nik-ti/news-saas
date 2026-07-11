"""F1 — per-stream dedup primitives and DB-level uniqueness. F2 — tenant scoping."""
from database import store
from database.models import get_connection


def _mk_stream(user_id=1, name="s"):
    return store.create_stream(user_id=user_id, name=name, criteria={"topic": name})


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


def test_unique_index_makes_duplicate_insert_noop(temp_db):
    s1 = _mk_stream()
    src = store.add_source(stream_id=s1, url="https://one.com")

    first = store.add_article(source_id=src, title="t", url="u", content_hash="H1")
    second = store.add_article(source_id=src, title="t", url="u", content_hash="H1")
    assert first > 0
    assert second == 0  # ignored, no crash

    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM articles WHERE source_id = ?", (src,)
    ).fetchone()["c"]
    conn.close()
    assert n == 1


def test_same_hash_allowed_on_different_sources(temp_db):
    # Cross-stream delivery relies on the SAME article existing under
    # each stream's own source row.
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


def test_init_db_survives_legacy_duplicate_articles(tmp_path, monkeypatch):
    # A DB created before the unique index can hold per-source duplicates.
    # init_db must fall back gracefully, not crash the bot at startup.
    import sqlite3
    import config
    from database.models import init_db

    db_path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    init_db()

    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX IF EXISTS uq_articles_src_hash")
    conn.execute("INSERT INTO streams (user_id, name, criteria) VALUES (1,'s','{}')")
    conn.execute("INSERT INTO sources (stream_id, url) VALUES (1,'https://a.com')")
    for _ in range(2):  # the duplicate pair the index would reject
        conn.execute(
            "INSERT INTO articles (source_id, title, url, content_hash) "
            "VALUES (1,'t','u','DUP')")
    conn.commit()
    conn.close()

    init_db()  # must not raise

    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert n == 2  # data untouched


def test_init_db_is_idempotent(temp_db):
    # Re-running init_db on an existing DB (with data) must not fail —
    # this is the startup path of every deploy.
    from database.models import init_db
    s1 = _mk_stream()
    src = store.add_source(stream_id=s1, url="https://one.com")
    store.add_article(source_id=src, title="t", url="u", content_hash="H1")
    init_db()
    init_db()
