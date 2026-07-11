"""
SQLite database schema initialization and connection helper.
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


def init_db() -> None:
    """Create all tables if they don't exist."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS streams (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        name        TEXT    NOT NULL,
        criteria    TEXT    NOT NULL,    -- JSON SourceCriteriaProfile
        status      TEXT    DEFAULT 'active',  -- active | paused | researching
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sources (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        stream_id       INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
        url             TEXT    NOT NULL,
        name            TEXT,
        broad_category  TEXT,
        site_type       TEXT,            -- news_site | company_blog | aggregator | analysis | other
        specific_keywords TEXT,          -- JSON array
        description     TEXT,
        quality_score   INTEGER DEFAULT 0,
        fetch_status    TEXT    DEFAULT 'active',  -- active | blocked | error
        feed_url        TEXT,
        fetch_method    TEXT,            -- rss | links | inline — the proven way to read this source
        embedding       BLOB,            -- float32 vector of what this source covers (semantic reuse)
        fail_count      INTEGER DEFAULT 0,
        last_checked    TEXT,
        last_fetched    TEXT,
        baselined_at    TEXT,   -- set after the first snapshot; NULL = never polled
        created_at      TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS articles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        title           TEXT,
        url             TEXT,
        summary         TEXT,
        relevance_score REAL    DEFAULT 0,
        status          TEXT    DEFAULT 'new',   -- new | seen (baselined) | posted |
                                                 -- irrelevant | unusable | dropped | send_failed
        fetched_at      TEXT    DEFAULT (datetime('now')),
        delivered_at    TEXT,
        posted_at       TEXT,
        attempts        INTEGER DEFAULT 0,  -- transient-failure retries
        content_hash    TEXT    -- for dedup
    );

    CREATE INDEX IF NOT EXISTS idx_sources_stream ON sources(stream_id);
    CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
    CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
    CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
    """)

    # Dedup is enforced at the DB level so racing writers can't double-insert.
    # A legacy DB may already hold per-source duplicates; fall back to the plain
    # content_hash index above rather than failing startup.
    try:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_src_hash "
            "ON articles(source_id, content_hash)"
        )
    except sqlite3.IntegrityError:
        logger.warning(
            "Could not create UNIQUE(source_id, content_hash) index — existing "
            "duplicate articles present; dedup stays best-effort in code"
        )

    # Migrations for databases created before these columns existed
    # (must run AFTER table creation, or a fresh DB has no sources table to alter)
    migrations = {
        "sources": [
            ("feed_url", "ALTER TABLE sources ADD COLUMN feed_url TEXT"),
            ("fail_count", "ALTER TABLE sources ADD COLUMN fail_count INTEGER DEFAULT 0"),
            ("baselined_at", "ALTER TABLE sources ADD COLUMN baselined_at TEXT"),
            ("site_type", "ALTER TABLE sources ADD COLUMN site_type TEXT"),
            ("fetch_method", "ALTER TABLE sources ADD COLUMN fetch_method TEXT"),
            ("embedding", "ALTER TABLE sources ADD COLUMN embedding BLOB"),
        ],
        "articles": [
            ("posted_at", "ALTER TABLE articles ADD COLUMN posted_at TEXT"),
            ("attempts", "ALTER TABLE articles ADD COLUMN attempts INTEGER DEFAULT 0"),
        ],
    }
    for table, cols in migrations.items():
        existing_cols = {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols:
            if col not in existing_cols:
                cur.execute(ddl)
                logger.info("Migration: added %s column to %s table", col, table)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {config.DB_PATH}")