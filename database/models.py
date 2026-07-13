"""
SQLite database schema initialization, migration, and connection helper.

Schema v2 (the §2.1 split):
  * sources are CANONICAL and tenant-free — one row per distinct feed page,
    however many streams follow it. Polling cost scales with distinct sources.
  * stream_sources is the subscription table (per-stream quality_score lives
    here — fit is per-user, not per-site).
  * articles hold one row per (source, story), no delivery state.
  * deliveries hold the per-(article, stream) delivery state, including the
    exact post_html that was sent (provenance).

A v1 database (sources.stream_id) is migrated in place on startup; the old
tables are kept as sources_v1 / articles_v1 for one release.
"""
import sqlite3
import os
import logging
import config

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with row factory."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    criteria    TEXT    NOT NULL,    -- JSON SourceCriteriaProfile
    status      TEXT    DEFAULT 'active',  -- active | paused | researching
    send_fail_streak INTEGER DEFAULT 0,    -- consecutive terminal send failures
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL,
    feed_url        TEXT,            -- the page we actually poll
    fetch_method    TEXT,            -- rss | links | inline — the proven way to read it
    name            TEXT,
    broad_category  TEXT,
    site_type       TEXT,            -- news_site | company_blog | aggregator | analysis | other
    specific_keywords TEXT,          -- JSON array
    description     TEXT,
    embedding       BLOB,            -- float32 vector of what this source covers
    fetch_status    TEXT    DEFAULT 'active',  -- active | blocked | error
    fail_count      INTEGER DEFAULT 0,
    pub_frequency   TEXT,            -- daily | weekly | monthly | rare (polling tier)
    etag            TEXT,            -- conditional GET: last ETag seen
    http_last_modified TEXT,         -- conditional GET: last Last-Modified seen
    last_checked    TEXT,
    last_fetched    TEXT,
    baselined_at    TEXT,   -- set after the first snapshot; NULL = never polled
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(feed_url)
);

CREATE TABLE IF NOT EXISTS stream_sources (
    stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    quality_score INTEGER DEFAULT 0,   -- fit is per-user, not per-site
    added_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (stream_id, source_id)
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title           TEXT,
    url             TEXT,
    summary         TEXT,
    content_hash    TEXT,
    embedding       BLOB,            -- story vector for semantic dedup
    fetched_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(source_id, content_hash)
);

CREATE TABLE IF NOT EXISTS deliveries (
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    status      TEXT DEFAULT 'new',  -- new | posted | irrelevant | dropped |
                                     -- unusable | send_failed | duplicate | stale
    post_html   TEXT,                -- the exact post that was sent (provenance)
    verdict     TEXT,                -- user feedback: up | down
    posted_at   TEXT,
    attempts    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (article_id, stream_id)
);

CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,        -- YYYY-MM-DD (UTC)
    kind    TEXT    NOT NULL,        -- research_run | llm_call | crawl | embed_call
    n       INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, day, kind)
);

CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
CREATE INDEX IF NOT EXISTS idx_deliveries_stream ON deliveries(stream_id);
CREATE INDEX IF NOT EXISTS idx_stream_sources_source ON stream_sources(source_id);
"""

# Columns added to v2 tables after their initial release — same ALTER-if-missing
# pattern v1 used, so older v2 databases upgrade in place.
V2_MIGRATIONS = {
    "streams": [
        ("send_fail_streak",
         "ALTER TABLE streams ADD COLUMN send_fail_streak INTEGER DEFAULT 0"),
    ],
    "sources": [
        ("pub_frequency", "ALTER TABLE sources ADD COLUMN pub_frequency TEXT"),
        ("etag", "ALTER TABLE sources ADD COLUMN etag TEXT"),
        ("http_last_modified",
         "ALTER TABLE sources ADD COLUMN http_last_modified TEXT"),
    ],
    "articles": [
        ("embedding", "ALTER TABLE articles ADD COLUMN embedding BLOB"),
    ],
    "deliveries": [
        ("post_html", "ALTER TABLE deliveries ADD COLUMN post_html TEXT"),
        ("verdict", "ALTER TABLE deliveries ADD COLUMN verdict TEXT"),
    ],
}


def _table_columns(cur, table: str) -> set[str]:
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def _needs_v1_migration(cur) -> bool:
    """A v1 database is one whose sources table carries stream_id."""
    cols = _table_columns(cur, "sources")
    return bool(cols) and "stream_id" in cols


def init_db() -> None:
    """Create all tables if they don't exist; migrate a v1 database in place."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = get_connection()
    cur = conn.cursor()

    if _needs_v1_migration(cur):
        _migrate_v1_to_v2(conn)
        cur = conn.cursor()

    cur.executescript(SCHEMA)

    for table, cols in V2_MIGRATIONS.items():
        existing = _table_columns(cur, table)
        for col, ddl in cols:
            if col not in existing:
                cur.execute(ddl)
                logger.info("Migration: added %s column to %s table", col, table)

    conn.commit()
    conn.close()


# ── v1 → v2 migration ─────────────────────────────────────────────────────────

def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Convert a per-stream-sources database to the canonical-sources schema.

    * Distinct feed pages collapse to one canonical source (best-scored row's
      metadata wins; a source is active if ANY of its old rows was active).
    * Every old row becomes a stream_sources subscription.
    * Old articles map onto the canonical source, deduped on (source, hash).
    * Old delivery state (everything except baseline 'seen') becomes a
      deliveries row for the owning stream.
    * The old tables survive as sources_v1 / articles_v1 for one release.
    """
    logger.warning("v1 database detected — migrating to canonical-sources schema")
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=OFF;")

    cur.execute("ALTER TABLE sources RENAME TO sources_v1")
    cur.execute("ALTER TABLE articles RENAME TO articles_v1")
    # The old indexes ride along with the renamed tables; drop them so the new
    # tables can claim the names.
    for idx in ("idx_sources_stream", "idx_articles_source",
                "idx_articles_status", "idx_articles_hash",
                "uq_articles_src_hash"):
        cur.execute(f"DROP INDEX IF EXISTS {idx}")

    cur.executescript(SCHEMA)

    # ── Canonical sources: group old rows by the page we poll ────────────
    old_sources = cur.execute(
        "SELECT * FROM sources_v1 ORDER BY quality_score DESC, id ASC"
    ).fetchall()

    canon_ids: dict[str, int] = {}       # feed key -> new source id
    old_to_new: dict[int, int] = {}      # old source id -> new source id

    fetch_rank = {"active": 0, "blocked": 1, "error": 2}

    for row in old_sources:
        d = dict(row)
        key = d.get("feed_url") or d["url"]
        if key not in canon_ids:
            cur.execute(
                """INSERT INTO sources
                   (url, feed_url, fetch_method, name, broad_category, site_type,
                    specific_keywords, description, embedding, fetch_status,
                    fail_count, last_checked, last_fetched, baselined_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["url"], key, d.get("fetch_method"), d.get("name"),
                 d.get("broad_category"), d.get("site_type"),
                 d.get("specific_keywords"), d.get("description"),
                 d.get("embedding"), d.get("fetch_status") or "active",
                 d.get("fail_count") or 0, d.get("last_checked"),
                 d.get("last_fetched"), d.get("baselined_at"), d.get("created_at")),
            )
            canon_ids[key] = cur.lastrowid
        else:
            # Merge state: the healthiest status and the earliest baseline win.
            new_id = canon_ids[key]
            existing = cur.execute("SELECT fetch_status, baselined_at FROM sources "
                                   "WHERE id = ?", (new_id,)).fetchone()
            status = d.get("fetch_status") or "active"
            if fetch_rank.get(status, 9) < fetch_rank.get(existing["fetch_status"], 9):
                cur.execute("UPDATE sources SET fetch_status = ? WHERE id = ?",
                            (status, new_id))
            if d.get("baselined_at") and (
                    existing["baselined_at"] is None
                    or d["baselined_at"] < existing["baselined_at"]):
                cur.execute("UPDATE sources SET baselined_at = ? WHERE id = ?",
                            (d["baselined_at"], new_id))

        new_id = canon_ids[key]
        old_to_new[d["id"]] = new_id
        cur.execute(
            "INSERT OR IGNORE INTO stream_sources "
            "(stream_id, source_id, quality_score) VALUES (?, ?, ?)",
            (d["stream_id"], new_id, d.get("quality_score") or 0),
        )

    # ── Articles + deliveries ─────────────────────────────────────────────
    old_articles = cur.execute(
        "SELECT a.*, s.stream_id FROM articles_v1 a "
        "JOIN sources_v1 s ON a.source_id = s.id ORDER BY a.id ASC"
    ).fetchall()

    n_deliveries = 0
    for row in old_articles:
        d = dict(row)
        new_src = old_to_new.get(d["source_id"])
        if new_src is None:
            continue
        c_hash = d.get("content_hash") or ""

        article_id = None
        if c_hash:
            existing = cur.execute(
                "SELECT id FROM articles WHERE source_id = ? AND content_hash = ?",
                (new_src, c_hash),
            ).fetchone()
            if existing:
                article_id = existing["id"]
        if article_id is None:
            cur.execute(
                """INSERT INTO articles
                   (source_id, title, url, summary, content_hash, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (new_src, d.get("title"), d.get("url"), d.get("summary"),
                 c_hash, d.get("fetched_at")),
            )
            article_id = cur.lastrowid

        # Baseline rows carry no delivery state — the article row itself is
        # what makes the hash "seen" for every subscribed stream.
        status = d.get("status") or "new"
        if status == "seen":
            continue
        cur.execute(
            """INSERT OR IGNORE INTO deliveries
               (article_id, stream_id, status, posted_at, attempts, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (article_id, d["stream_id"], status, d.get("posted_at"),
             d.get("attempts") or 0, d.get("fetched_at")),
        )
        n_deliveries += cur.rowcount

    conn.commit()
    cur.execute("PRAGMA foreign_keys=ON;")
    logger.warning(
        "Migration complete: %d source rows → %d canonical sources, "
        "%d subscriptions, %d articles, %d deliveries "
        "(old tables kept as sources_v1/articles_v1)",
        len(old_sources), len(canon_ids),
        cur.execute("SELECT COUNT(*) FROM stream_sources").fetchone()[0],
        cur.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        n_deliveries,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(f"Database initialized at {config.DB_PATH}")
