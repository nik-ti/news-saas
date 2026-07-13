"""v1 → v2 migration: canonical collapse, subscriptions, delivery mapping."""
import sqlite3

import config
from database import store
from database.models import init_db


V1_SCHEMA = """
CREATE TABLE streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    name TEXT NOT NULL, criteria TEXT NOT NULL, status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    url TEXT NOT NULL, name TEXT, broad_category TEXT, site_type TEXT,
    specific_keywords TEXT, description TEXT, quality_score INTEGER DEFAULT 0,
    fetch_status TEXT DEFAULT 'active', feed_url TEXT, fetch_method TEXT,
    embedding BLOB, fail_count INTEGER DEFAULT 0, last_checked TEXT,
    last_fetched TEXT, baselined_at TEXT, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT, url TEXT, summary TEXT, relevance_score REAL DEFAULT 0,
    status TEXT DEFAULT 'new', fetched_at TEXT DEFAULT (datetime('now')),
    delivered_at TEXT, posted_at TEXT, attempts INTEGER DEFAULT 0,
    content_hash TEXT
);
"""


def _build_v1_db(path: str):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    # Two users; both follow TechCrunch (same feed_url) — the keystone case.
    conn.execute("INSERT INTO streams (user_id, name, criteria) VALUES (1,'crypto','{}')")
    conn.execute("INSERT INTO streams (user_id, name, criteria) VALUES (2,'ai','{}')")
    conn.execute(
        "INSERT INTO sources (stream_id, url, feed_url, quality_score, "
        "fetch_status, baselined_at, fetch_method) "
        "VALUES (1,'https://tc.com','https://tc.com/news',90,'active',"
        "'2026-07-01 00:00:00','links')")
    conn.execute(
        "INSERT INTO sources (stream_id, url, feed_url, quality_score, "
        "fetch_status, baselined_at, fetch_method) "
        "VALUES (2,'https://tc.com','https://tc.com/news',60,'blocked',NULL,'links')")
    conn.execute(
        "INSERT INTO sources (stream_id, url, feed_url, quality_score) "
        "VALUES (1,'https://solo.com','https://solo.com/feed',70)")
    # Articles: a baseline row, a posted row, and a queued row on source 1;
    # the SAME hash also exists under user 2's duplicate source row.
    conn.execute("INSERT INTO articles (source_id, title, url, status, content_hash) "
                 "VALUES (1,'base','u0','seen','H0')")
    conn.execute("INSERT INTO articles (source_id, title, url, status, content_hash, "
                 "posted_at) VALUES (1,'won','u1','posted','H1','2026-07-02 00:00:00')")
    conn.execute("INSERT INTO articles (source_id, title, url, status, content_hash, "
                 "attempts) VALUES (1,'queued','u2','new','H2',1)")
    conn.execute("INSERT INTO articles (source_id, title, url, status, content_hash) "
                 "VALUES (2,'won','u1','irrelevant','H1')")
    conn.commit()
    conn.close()


def _migrated(tmp_path, monkeypatch):
    db_path = str(tmp_path / "v1.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    _build_v1_db(db_path)
    init_db()
    return db_path


def test_duplicate_feed_rows_collapse(tmp_path, monkeypatch):
    _migrated(tmp_path, monkeypatch)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    sources = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    subs = conn.execute("SELECT * FROM stream_sources").fetchall()
    conn.close()

    assert len(sources) == 2                          # 3 rows → 2 canonical
    tc = next(s for s in sources if s["feed_url"] == "https://tc.com/news")
    assert tc["fetch_status"] == "active"             # healthiest status wins
    assert tc["baselined_at"] == "2026-07-01 00:00:00"
    assert len(subs) == 3                             # every old row → a sub
    scores = {(s["stream_id"], s["source_id"]): s["quality_score"] for s in subs}
    assert scores[(1, tc["id"])] == 90
    assert scores[(2, tc["id"])] == 60


def test_articles_dedupe_and_deliveries_map(tmp_path, monkeypatch):
    _migrated(tmp_path, monkeypatch)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    arts = conn.execute("SELECT * FROM articles ORDER BY id").fetchall()
    dels = conn.execute("SELECT * FROM deliveries").fetchall()
    conn.close()

    hashes = sorted(a["content_hash"] for a in arts)
    assert hashes == ["H0", "H1", "H2"]               # H1 deduped across old rows

    by = {(d["article_id"], d["stream_id"]): d for d in dels}
    h1 = next(a["id"] for a in arts if a["content_hash"] == "H1")
    h2 = next(a["id"] for a in arts if a["content_hash"] == "H2")
    assert by[(h1, 1)]["status"] == "posted"
    assert by[(h1, 1)]["posted_at"] == "2026-07-02 00:00:00"
    assert by[(h1, 2)]["status"] == "irrelevant"      # per-stream state preserved
    assert by[(h2, 1)]["status"] == "new"
    assert by[(h2, 1)]["attempts"] == 1
    assert len(dels) == 3                             # 'seen' rows create none


def test_baseline_hashes_still_seen_after_migration(tmp_path, monkeypatch):
    _migrated(tmp_path, monkeypatch)
    assert "H0" in store.stream_seen_hashes(1)
    assert "H1" in store.stream_seen_hashes(2)        # via the shared source


def test_old_tables_kept_and_second_boot_is_clean(tmp_path, monkeypatch):
    _migrated(tmp_path, monkeypatch)
    init_db()  # second boot must not re-migrate or crash
    conn = sqlite3.connect(config.DB_PATH)
    n_old_src = conn.execute("SELECT COUNT(*) FROM sources_v1").fetchone()[0]
    n_old_art = conn.execute("SELECT COUNT(*) FROM articles_v1").fetchone()[0]
    n_src = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()
    assert n_old_src == 3 and n_old_art == 4
    assert n_src == 2
