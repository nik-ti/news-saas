"""
CRUD operations for streams, sources, and articles.

Every function goes through db(): one place that opens, commits, and closes.
Row decoding for sources lives in _source_row(): one place that parses the
specific_keywords JSON.
"""
import json
from contextlib import contextmanager
from typing import Optional

from database.models import get_connection


@contextmanager
def db():
    """Connection scope: commit on success, always close."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _source_row(row) -> dict:
    d = dict(row)
    d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
    return d


# ── Streams ───────────────────────────────────────────────────────────────────

def create_stream(user_id: int, name: str, criteria: dict) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO streams (user_id, name, criteria) VALUES (?, ?, ?)",
            (user_id, name, json.dumps(criteria)),
        )
        return cur.lastrowid


def get_stream(stream_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM streams WHERE id = ?", (stream_id,)
        ).fetchone()
    if row:
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"])
        return d
    return None


def get_streams_by_user(user_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM streams WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"])
        results.append(d)
    return results


def update_stream_status(stream_id: int, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE streams SET status = ? WHERE id = ?",
                     (status, stream_id))


def update_stream_criteria(stream_id: int, criteria: dict) -> None:
    """Persist a (re)generated profile back onto the stream."""
    with db() as conn:
        conn.execute("UPDATE streams SET criteria = ? WHERE id = ?",
                     (json.dumps(criteria), stream_id))


def set_post_length(stream_id: int, length: str) -> bool:
    """Set a stream's post-length preference ('standard' | 'compact')."""
    stream = get_stream(stream_id)
    if not stream:
        return False
    criteria = stream.get("criteria") or {}
    if not isinstance(criteria, dict):
        criteria = {}
    criteria["post_length"] = length
    update_stream_criteria(stream_id, criteria)
    return True


def delete_stream(stream_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM streams WHERE id = ?", (stream_id,))


def reset_stuck_research() -> int:
    """
    Research runs in a background task and is not resumable. If the process dies
    mid-run, its stream is stranded in 'researching' and the news cycle — which
    only serves active streams — ignores it forever. Reconcile on boot.
    Returns how many streams were freed.
    """
    with db() as conn:
        cur = conn.execute(
            "UPDATE streams SET status = 'active' WHERE status = 'researching'"
        )
        return cur.rowcount


# ── Sources ───────────────────────────────────────────────────────────────────

def add_source(
    stream_id: int,
    url: str,
    name: str = "",
    broad_category: str = "",
    specific_keywords: list = None,
    description: str = "",
    quality_score: int = 0,
    fetch_status: str = "active",
    feed_url: str = "",
    site_type: str = "",
    fetch_method: str = "",
) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO sources
               (stream_id, url, name, broad_category, site_type, specific_keywords,
                description, quality_score, fetch_status, last_checked, feed_url,
                fetch_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
            (
                stream_id, url, name, broad_category, site_type or None,
                json.dumps(specific_keywords or []),
                description, quality_score, fetch_status, feed_url or url,
                fetch_method or None,
            ),
        )
        return cur.lastrowid


def get_sources_by_stream(stream_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sources WHERE stream_id = ? ORDER BY quality_score DESC",
            (stream_id,),
        ).fetchall()
    return [_source_row(r) for r in rows]


def get_all_sources() -> list[dict]:
    """Return all sources across all streams (for /sources_all)."""
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, st.name as stream_name FROM sources s
               JOIN streams st ON s.stream_id = st.id
               ORDER BY s.created_at DESC"""
        ).fetchall()
    return [_source_row(r) for r in rows]


def get_active_sources() -> list[dict]:
    """All sources with fetch_status='active' — used by the fetch cron."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sources WHERE fetch_status = 'active'"
        ).fetchall()
    return [_source_row(r) for r in rows]


def get_source(source_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    return _source_row(row) if row else None


def get_source_by_url(stream_id: int, url: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE stream_id = ? AND url = ?",
            (stream_id, url),
        ).fetchone()
    return dict(row) if row else None


def update_source_status(source_id: int, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE sources SET fetch_status = ?, last_checked = datetime('now') "
            "WHERE id = ?",
            (status, source_id),
        )


def update_source_fetch_time(source_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE sources SET last_fetched = datetime('now') WHERE id = ?",
            (source_id,),
        )


def increment_fail_count(source_id: int) -> int:
    """Increment consecutive fetch failure count; returns the new count."""
    with db() as conn:
        conn.execute(
            "UPDATE sources SET fail_count = COALESCE(fail_count, 0) + 1, "
            "last_checked = datetime('now') WHERE id = ?",
            (source_id,),
        )
        row = conn.execute(
            "SELECT fail_count FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    return row["fail_count"] if row else 0


def reset_fail_count(source_id: int) -> None:
    """Clear the consecutive-failure counter after a successful fetch.

    Deliberately does NOT touch fetch_status — a source the user blocked stays
    blocked. Use reactivate_source() to bring an errored source back.
    """
    with db() as conn:
        conn.execute(
            "UPDATE sources SET fail_count = 0, last_checked = datetime('now') "
            "WHERE id = ?",
            (source_id,),
        )


def reactivate_source(source_id: int) -> None:
    """Bring an errored source back to active (health check only)."""
    with db() as conn:
        conn.execute(
            "UPDATE sources SET fetch_status = 'active', fail_count = 0, "
            "last_checked = datetime('now') WHERE id = ?",
            (source_id,),
        )


def mark_source_baselined(source_id: int) -> None:
    """Record that this source has had its first snapshot taken."""
    with db() as conn:
        conn.execute(
            "UPDATE sources SET baselined_at = datetime('now') WHERE id = ?",
            (source_id,),
        )


def delete_source(source_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# ── Articles ──────────────────────────────────────────────────────────────────

def add_article(
    source_id: int,
    title: str,
    url: str,
    summary: str = "",
    relevance_score: float = 0,
    status: str = "new",
    content_hash: str = "",
) -> int:
    with db() as conn:
        # OR IGNORE: UNIQUE(source_id, content_hash) makes a duplicate insert a
        # no-op instead of a crash, so racing writers can't double-record.
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (source_id, title, url, summary, relevance_score, status, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_id, title, url, summary, relevance_score, status, content_hash),
        )
        return cur.lastrowid if cur.rowcount else 0


def stream_seen_hashes(stream_id: int) -> set[str]:
    """
    All content hashes this STREAM has ever recorded, from any of its sources.

    Dedup is per stream: two streams following overlapping sources must EACH
    receive an article, while two sources inside one stream (e.g. Google News +
    the publisher directly) must not deliver it twice.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT a.content_hash FROM articles a
               JOIN sources s ON a.source_id = s.id
               WHERE s.stream_id = ?""",
            (stream_id,),
        ).fetchall()
    return {r["content_hash"] for r in rows if r["content_hash"]}


def get_articles_by_stream(stream_id: int, limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT a.* FROM articles a
               JOIN sources s ON a.source_id = s.id
               WHERE s.stream_id = ?
               ORDER BY a.fetched_at DESC LIMIT ?""",
            (stream_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def update_article_status(article_id: int, status: str,
                          relevance_score: float = None) -> None:
    with db() as conn:
        if relevance_score is not None:
            conn.execute(
                "UPDATE articles SET status = ?, relevance_score = ? WHERE id = ?",
                (status, relevance_score, article_id),
            )
        else:
            conn.execute(
                "UPDATE articles SET status = ? WHERE id = ?", (status, article_id)
            )


def mark_posted(article_id: int) -> None:
    """Terminal status for an article that was successfully sent."""
    with db() as conn:
        conn.execute(
            "UPDATE articles SET status = 'posted', posted_at = datetime('now') "
            "WHERE id = ?",
            (article_id,),
        )


def increment_article_attempts(article_id: int) -> int:
    """Count a transient processing failure; returns the new attempt count."""
    with db() as conn:
        conn.execute(
            "UPDATE articles SET attempts = COALESCE(attempts, 0) + 1 WHERE id = ?",
            (article_id,),
        )
        row = conn.execute(
            "SELECT attempts FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
    return row["attempts"] if row else 0


def get_queued_articles(limit: int) -> list[dict]:
    """Articles awaiting processing, oldest first, for active streams only."""
    with db() as conn:
        rows = conn.execute(
            """SELECT a.*, s.stream_id, s.name AS source_name,
                      s.feed_url AS source_feed_url, st.user_id
               FROM articles a
               JOIN sources s ON a.source_id = s.id
               JOIN streams st ON s.stream_id = st.id
               WHERE a.status = 'new' AND st.status = 'active'
               ORDER BY a.fetched_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_article_summary(article_id: int, summary: str) -> None:
    """Persist a computed summary so retries don't re-crawl and re-summarize."""
    with db() as conn:
        conn.execute(
            "UPDATE articles SET summary = ? WHERE id = ?", (summary, article_id)
        )


def get_latest_articles_for_user(user_id: int, limit: int = 20) -> list[dict]:
    """Latest articles across THIS user's streams only (tenant-scoped)."""
    with db() as conn:
        rows = conn.execute(
            """SELECT a.*, s.name as source_name, s.url as source_url FROM articles a
               JOIN sources s ON a.source_id = s.id
               JOIN streams st ON s.stream_id = st.id
               WHERE st.user_id = ?
               ORDER BY a.fetched_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Internal Source DB (cross-stream) ────────────────────────────────────────

def set_source_embedding(source_id: int, blob: bytes) -> None:
    with db() as conn:
        conn.execute("UPDATE sources SET embedding = ? WHERE id = ?",
                     (blob, source_id))


def sources_missing_embedding(stream_id: int = None) -> list[dict]:
    """Sources with no embedding yet (optionally scoped to one stream)."""
    sql = "SELECT * FROM sources WHERE embedding IS NULL"
    args = ()
    if stream_id is not None:
        sql += " AND stream_id = ?"
        args = (stream_id,)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_source_row(r) for r in rows]


def get_embedded_sources(exclude_stream_id: int = None) -> list[dict]:
    """
    Every source that has an embedding, one row per distinct feed page (the
    internal DB is cross-stream). Optionally exclude the stream we're building,
    so research doesn't 'reuse' the sources it just added.
    """
    sql = (
        "SELECT id, url, name, broad_category, site_type, specific_keywords, "
        "description, feed_url, fetch_method, quality_score, embedding, "
        "MIN(stream_id) AS stream_id "
        "FROM sources WHERE embedding IS NOT NULL AND fetch_status = 'active'"
    )
    args = ()
    if exclude_stream_id is not None:
        sql += " AND stream_id != ?"
        args = (exclude_stream_id,)
    sql += " GROUP BY feed_url"   # dedupe the same page discovered for many users
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_source_row(r) for r in rows]


def find_internal_sources(broad_category: str, keywords: list) -> list[dict]:
    """Find existing sources in DB matching a broad category + any keyword."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT url, name, broad_category, specific_keywords, description "
            "FROM sources WHERE broad_category = ? AND fetch_status = 'active'",
            (broad_category,),
        ).fetchall()
    results = []
    kw_lower = [k.lower() for k in keywords]
    for row in rows:
        d = _source_row(row)
        src_kw_lower = [k.lower() for k in d["specific_keywords"]]
        # match if any keyword overlaps
        if any(kw in src_kw_lower for kw in kw_lower):
            results.append(d)
    return results
