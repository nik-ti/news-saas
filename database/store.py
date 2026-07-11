"""
CRUD operations for streams, sources, and articles.
"""
import json
from typing import Optional
from database.models import get_connection


# ── Streams ───────────────────────────────────────────────────────────────────

def create_stream(user_id: int, name: str, criteria: dict) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO streams (user_id, name, criteria) VALUES (?, ?, ?)",
        (user_id, name, json.dumps(criteria)),
    )
    conn.commit()
    stream_id = cur.lastrowid
    conn.close()
    return stream_id


def get_stream(stream_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"])
        return d
    return None


def get_streams_by_user(user_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM streams WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"])
        results.append(d)
    return results


def update_stream_status(stream_id: int, status: str) -> None:
    conn = get_connection()
    conn.execute("UPDATE streams SET status = ? WHERE id = ?", (status, stream_id))
    conn.commit()
    conn.close()


def delete_stream(stream_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
    conn.commit()
    conn.close()


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
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
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
    conn.commit()
    source_id = cur.lastrowid
    conn.close()
    return source_id


def get_sources_by_stream(stream_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sources WHERE stream_id = ? ORDER BY quality_score DESC",
        (stream_id,),
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
        results.append(d)
    return results


def get_all_sources() -> list[dict]:
    """Return all sources across all streams (for /sources_all)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT s.*, st.name as stream_name FROM sources s
           JOIN streams st ON s.stream_id = st.id
           ORDER BY s.created_at DESC"""
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
        results.append(d)
    return results


def get_active_sources() -> list[dict]:
    """All sources with fetch_status='active' — used by the fetch cron."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sources WHERE fetch_status = 'active'"
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
        results.append(d)
    return results


def get_source(source_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
        return d
    return None


def get_source_by_url(stream_id: int, url: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sources WHERE stream_id = ? AND url = ?",
        (stream_id, url),
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def update_source_status(source_id: int, status: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE sources SET fetch_status = ?, last_checked = datetime('now') WHERE id = ?",
        (status, source_id),
    )
    conn.commit()
    conn.close()


def update_source_fetch_time(source_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE sources SET last_fetched = datetime('now') WHERE id = ?", (source_id,)
    )
    conn.commit()
    conn.close()


def increment_fail_count(source_id: int) -> int:
    """Increment consecutive fetch failure count; returns the new count."""
    conn = get_connection()
    conn.execute(
        "UPDATE sources SET fail_count = COALESCE(fail_count, 0) + 1, "
        "last_checked = datetime('now') WHERE id = ?",
        (source_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT fail_count FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    conn.close()
    return row["fail_count"] if row else 0


def reset_fail_count(source_id: int) -> None:
    """Clear the consecutive-failure counter after a successful fetch.

    Deliberately does NOT touch fetch_status — a source the user blocked stays
    blocked. Use reactivate_source() to bring an errored source back.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE sources SET fail_count = 0, last_checked = datetime('now') WHERE id = ?",
        (source_id,),
    )
    conn.commit()
    conn.close()


def reactivate_source(source_id: int) -> None:
    """Bring an errored source back to active (health check only)."""
    conn = get_connection()
    conn.execute(
        "UPDATE sources SET fetch_status = 'active', fail_count = 0, "
        "last_checked = datetime('now') WHERE id = ?",
        (source_id,),
    )
    conn.commit()
    conn.close()


def reset_stuck_research() -> int:
    """
    Research runs in a background task and is not resumable. If the process dies
    mid-run, its stream is stranded in 'researching' and the news cycle — which
    only serves active streams — ignores it forever. Reconcile on boot.
    Returns how many streams were freed.
    """
    conn = get_connection()
    cur = conn.execute(
        "UPDATE streams SET status = 'active' WHERE status = 'researching'"
    )
    conn.commit()
    freed = cur.rowcount
    conn.close()
    return freed


def mark_source_baselined(source_id: int) -> None:
    """Record that this source has had its first snapshot taken."""
    conn = get_connection()
    conn.execute(
        "UPDATE sources SET baselined_at = datetime('now') WHERE id = ?", (source_id,)
    )
    conn.commit()
    conn.close()


def delete_source(source_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()


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
    conn = get_connection()
    cur = conn.cursor()
    # OR IGNORE: UNIQUE(source_id, content_hash) makes a duplicate insert a no-op
    # instead of a crash, so racing writers can't double-record an article.
    cur.execute(
        """INSERT OR IGNORE INTO articles
           (source_id, title, url, summary, relevance_score, status, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source_id, title, url, summary, relevance_score, status, content_hash),
    )
    conn.commit()
    article_id = cur.lastrowid if cur.rowcount else 0
    conn.close()
    return article_id


def stream_seen_hashes(stream_id: int) -> set[str]:
    """
    All content hashes this STREAM has ever recorded, from any of its sources.

    Dedup is per stream: two streams following overlapping sources must EACH
    receive an article, while two sources inside one stream (e.g. Google News +
    the publisher directly) must not deliver it twice.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.content_hash FROM articles a
           JOIN sources s ON a.source_id = s.id
           WHERE s.stream_id = ?""",
        (stream_id,),
    ).fetchall()
    conn.close()
    return {r["content_hash"] for r in rows if r["content_hash"]}


def get_new_articles() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.*, s.stream_id FROM articles a
           JOIN sources s ON a.source_id = s.id
           WHERE a.status = 'new'
           ORDER BY a.fetched_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_articles_by_stream(stream_id: int, limit: int = 20) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.* FROM articles a
           JOIN sources s ON a.source_id = s.id
           WHERE s.stream_id = ?
           ORDER BY a.fetched_at DESC LIMIT ?""",
        (stream_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_article_status(article_id: int, status: str, relevance_score: float = None) -> None:
    conn = get_connection()
    if relevance_score is not None:
        conn.execute(
            "UPDATE articles SET status = ?, relevance_score = ? WHERE id = ?",
            (status, relevance_score, article_id),
        )
    else:
        conn.execute(
            "UPDATE articles SET status = ? WHERE id = ?", (status, article_id)
        )
    conn.commit()
    conn.close()


def mark_posted(article_id: int) -> None:
    """Terminal status for an article that was successfully sent."""
    conn = get_connection()
    conn.execute(
        "UPDATE articles SET status = 'posted', posted_at = datetime('now') WHERE id = ?",
        (article_id,),
    )
    conn.commit()
    conn.close()


def increment_article_attempts(article_id: int) -> int:
    """Count a transient processing failure; returns the new attempt count."""
    conn = get_connection()
    conn.execute(
        "UPDATE articles SET attempts = COALESCE(attempts, 0) + 1 WHERE id = ?",
        (article_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    conn.close()
    return row["attempts"] if row else 0


def get_queued_articles(limit: int) -> list[dict]:
    """Articles awaiting processing, oldest first, for active streams only."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.*, s.stream_id, s.name AS source_name, st.user_id
           FROM articles a
           JOIN sources s ON a.source_id = s.id
           JOIN streams st ON s.stream_id = st.id
           WHERE a.status = 'new' AND st.status = 'active'
           ORDER BY a.fetched_at ASC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_stream_criteria(stream_id: int, criteria: dict) -> None:
    """Persist a (re)generated profile back onto the stream."""
    conn = get_connection()
    conn.execute(
        "UPDATE streams SET criteria = ? WHERE id = ?",
        (json.dumps(criteria), stream_id),
    )
    conn.commit()
    conn.close()


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


def mark_articles_delivered(article_ids: list[int]) -> None:
    conn = get_connection()
    conn.executemany(
        "UPDATE articles SET delivered_at = datetime('now'), status = 'processed' WHERE id = ?",
        [(aid,) for aid in article_ids],
    )
    conn.commit()
    conn.close()


def get_latest_articles_for_user(user_id: int, limit: int = 20) -> list[dict]:
    """Latest articles across THIS user's streams only (tenant-scoped)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT a.*, s.name as source_name, s.url as source_url FROM articles a
           JOIN sources s ON a.source_id = s.id
           JOIN streams st ON s.stream_id = st.id
           WHERE st.user_id = ?
           ORDER BY a.fetched_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Internal Source DB (cross-stream) ────────────────────────────────────────

def set_source_embedding(source_id: int, blob: bytes) -> None:
    conn = get_connection()
    conn.execute("UPDATE sources SET embedding = ? WHERE id = ?", (blob, source_id))
    conn.commit()
    conn.close()


def sources_missing_embedding(stream_id: int = None) -> list[dict]:
    """Sources with no embedding yet (optionally scoped to one stream)."""
    conn = get_connection()
    sql = "SELECT * FROM sources WHERE embedding IS NULL"
    args = ()
    if stream_id is not None:
        sql += " AND stream_id = ?"
        args = (stream_id,)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
        results.append(d)
    return results


def get_embedded_sources(exclude_stream_id: int = None) -> list[dict]:
    """
    Every source that has an embedding, one row per distinct feed page (the
    internal DB is cross-stream). Optionally exclude the stream we're building,
    so research doesn't 'reuse' the sources it just added.
    """
    conn = get_connection()
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
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["specific_keywords"] = json.loads(d.get("specific_keywords") or "[]")
        results.append(d)
    return results


def find_internal_sources(broad_category: str, keywords: list) -> list[dict]:
    """Find existing sources in DB matching a broad category + any keyword."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT url, name, broad_category, specific_keywords, description "
        "FROM sources WHERE broad_category = ? AND fetch_status = 'active'",
        (broad_category,),
    ).fetchall()
    conn.close()
    results = []
    kw_lower = [k.lower() for k in keywords]
    for row in rows:
        d = dict(row)
        src_keywords = json.loads(d.get("specific_keywords") or "[]")
        src_kw_lower = [k.lower() for k in src_keywords]
        # match if any keyword overlaps
        if any(kw in src_kw_lower for kw in kw_lower):
            results.append(d)
    return results