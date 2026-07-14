"""
CRUD operations for streams, canonical sources, subscriptions, articles,
deliveries, and usage accounting.

Every function goes through db(): one place that opens, commits, and closes.
Row decoding for sources lives in _source_row(): one place that parses the
specific_keywords JSON.

Schema v2: sources are canonical (tenant-free, UNIQUE(feed_url)); streams
follow them through stream_sources; per-(article, stream) delivery state —
including the exact post_html that was sent — lives in deliveries.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
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


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def get_all_streams() -> list[dict]:
    """Every stream across all users (operator view). Newest first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM streams ORDER BY created_at DESC"
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"])
        results.append(d)
    return results


def count_streams(user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM streams WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["c"]


def update_stream_status(stream_id: int, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE streams SET status = ? WHERE id = ?",
                     (status, stream_id))


def update_stream_criteria(stream_id: int, criteria: dict) -> None:
    """Persist a (re)generated profile back onto the stream."""
    with db() as conn:
        conn.execute("UPDATE streams SET criteria = ? WHERE id = ?",
                     (json.dumps(criteria), stream_id))


def set_stream_criteria_field(stream_id: int, key: str, value) -> bool:
    """Set one key inside a stream's criteria JSON (post_length, quiet_hours…)."""
    stream = get_stream(stream_id)
    if not stream:
        return False
    criteria = stream.get("criteria") or {}
    if not isinstance(criteria, dict):
        criteria = {}
    criteria[key] = value
    update_stream_criteria(stream_id, criteria)
    return True


def set_post_length(stream_id: int, length: str) -> bool:
    """Set a stream's post-length preference ('standard' | 'compact')."""
    return set_stream_criteria_field(stream_id, "post_length", length)


# ── Users (interface preferences) ────────────────────────────────────────────

def get_ui_lang(user_id: int) -> str:
    """The user's bot-interface language ('en' | 'ru'). Default: English."""
    with db() as conn:
        row = conn.execute(
            "SELECT ui_lang FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return (row["ui_lang"] if row and row["ui_lang"] else "en")


def set_ui_lang(user_id: int, lang: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, ui_lang) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET ui_lang = excluded.ui_lang",
            (user_id, lang),
        )


def record_send_result(stream_id: int, ok: bool) -> int:
    """
    Track consecutive terminal send failures per stream (for auto-pause).
    Returns the streak after this result.
    """
    with db() as conn:
        if ok:
            conn.execute("UPDATE streams SET send_fail_streak = 0 WHERE id = ?",
                         (stream_id,))
            return 0
        conn.execute(
            "UPDATE streams SET send_fail_streak = "
            "COALESCE(send_fail_streak, 0) + 1 WHERE id = ?", (stream_id,))
        row = conn.execute("SELECT send_fail_streak FROM streams WHERE id = ?",
                           (stream_id,)).fetchone()
    return row["send_fail_streak"] if row else 0


def delete_stream(stream_id: int) -> None:
    """Delete a stream, its subscriptions and deliveries; then drop any
    canonical source nobody follows anymore (its articles cascade with it)."""
    with db() as conn:
        conn.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
        conn.execute(
            "DELETE FROM sources WHERE id NOT IN "
            "(SELECT DISTINCT source_id FROM stream_sources)"
        )


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


# ── Sources (canonical) + subscriptions ──────────────────────────────────────

def upsert_source(
    url: str,
    feed_url: str = "",
    name: str = "",
    broad_category: str = "",
    specific_keywords: list = None,
    description: str = "",
    fetch_status: str = "active",
    site_type: str = "",
    fetch_method: str = "",
    pub_frequency: str = "",
) -> int:
    """
    Insert or find the canonical source for this feed page. Returns its id.
    An existing row keeps its fetch state; empty metadata fields are filled in.
    """
    key = feed_url or url
    with db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE feed_url = ?",
                           (key,)).fetchone()
        if row:
            d = dict(row)
            # Fill blanks only — never clobber a proven fetch_method or state.
            updates, args = [], []
            for col, val in (("name", name), ("broad_category", broad_category),
                             ("site_type", site_type), ("description", description),
                             ("fetch_method", fetch_method),
                             ("pub_frequency", pub_frequency)):
                if val and not d.get(col):
                    updates.append(f"{col} = ?")
                    args.append(val)
            if specific_keywords and not json.loads(d.get("specific_keywords") or "[]"):
                updates.append("specific_keywords = ?")
                args.append(json.dumps(specific_keywords))
            if updates:
                conn.execute(f"UPDATE sources SET {', '.join(updates)} WHERE id = ?",
                             (*args, d["id"]))
            return d["id"]

        cur = conn.execute(
            """INSERT INTO sources
               (url, feed_url, fetch_method, name, broad_category, site_type,
                specific_keywords, description, fetch_status, pub_frequency,
                last_checked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (url, key, fetch_method or None, name, broad_category,
             site_type or None, json.dumps(specific_keywords or []),
             description, fetch_status, pub_frequency or None),
        )
        return cur.lastrowid


def subscribe(stream_id: int, source_id: int, quality_score: int = 0) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO stream_sources "
            "(stream_id, source_id, quality_score) VALUES (?, ?, ?)",
            (stream_id, source_id, quality_score),
        )


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
    pub_frequency: str = "",
) -> int:
    """Upsert the canonical source and subscribe the stream to it.

    Returns the canonical source id. (Kept as the one-call convenience the
    research engine and handlers use — quality_score lands on the subscription.)
    """
    source_id = upsert_source(
        url=url, feed_url=feed_url or url, name=name,
        broad_category=broad_category, specific_keywords=specific_keywords,
        description=description, fetch_status=fetch_status,
        site_type=site_type, fetch_method=fetch_method,
        pub_frequency=pub_frequency,
    )
    subscribe(stream_id, source_id, quality_score)
    return source_id


def unsubscribe(stream_id: int, source_id: int) -> None:
    """Remove a stream's subscription; drop the source if nobody else follows it."""
    with db() as conn:
        conn.execute(
            "DELETE FROM stream_sources WHERE stream_id = ? AND source_id = ?",
            (stream_id, source_id),
        )
        conn.execute(
            "DELETE FROM sources WHERE id = ? AND id NOT IN "
            "(SELECT DISTINCT source_id FROM stream_sources)", (source_id,)
        )


def get_sources_by_stream(stream_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, ss.quality_score AS quality_score
               FROM sources s JOIN stream_sources ss ON s.id = ss.source_id
               WHERE ss.stream_id = ? ORDER BY ss.quality_score DESC""",
            (stream_id,),
        ).fetchall()
    return [_source_row(r) for r in rows]


def get_all_sources() -> list[dict]:
    """All canonical sources with their subscriber count (for /sources_all)."""
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, COUNT(ss.stream_id) AS subscribers,
                      MAX(ss.quality_score) AS quality_score
               FROM sources s LEFT JOIN stream_sources ss ON s.id = ss.source_id
               GROUP BY s.id ORDER BY s.created_at DESC"""
        ).fetchall()
    return [_source_row(r) for r in rows]


def get_active_sources() -> list[dict]:
    """
    Canonical sources worth polling: active themselves AND followed by at
    least one active stream. A source only paused streams follow is skipped
    entirely — no crawl, no LLM, no cost.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT s.* FROM sources s
               JOIN stream_sources ss ON s.id = ss.source_id
               JOIN streams st ON ss.stream_id = st.id
               WHERE s.fetch_status = 'active' AND st.status = 'active'"""
        ).fetchall()
    return [_source_row(r) for r in rows]


def active_subscriber_ids(source_id: int) -> list[int]:
    """IDs of ACTIVE streams subscribed to this source."""
    with db() as conn:
        rows = conn.execute(
            """SELECT ss.stream_id FROM stream_sources ss
               JOIN streams st ON ss.stream_id = st.id
               WHERE ss.source_id = ? AND st.status = 'active'""",
            (source_id,),
        ).fetchall()
    return [r["stream_id"] for r in rows]


def subscriber_user_ids(source_id: int) -> list[int]:
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT st.user_id FROM stream_sources ss
               JOIN streams st ON ss.stream_id = st.id WHERE ss.source_id = ?""",
            (source_id,),
        ).fetchall()
    return [r["user_id"] for r in rows]


def get_source(source_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    return _source_row(row) if row else None


def get_source_by_url(stream_id: int, url: str) -> Optional[dict]:
    """A source this STREAM follows whose site or feed URL matches."""
    with db() as conn:
        row = conn.execute(
            """SELECT s.* FROM sources s
               JOIN stream_sources ss ON s.id = ss.source_id
               WHERE ss.stream_id = ? AND (s.url = ? OR s.feed_url = ?)""",
            (stream_id, url, url),
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


def set_source_conditional(source_id: int, etag: str | None,
                           last_modified: str | None) -> None:
    """Remember the feed's ETag / Last-Modified for conditional GET."""
    with db() as conn:
        conn.execute(
            "UPDATE sources SET etag = ?, http_last_modified = ? WHERE id = ?",
            (etag, last_modified, source_id),
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
    """Hard-delete a canonical source (admin path — takes it from EVERY stream)."""
    with db() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


# ── Articles ──────────────────────────────────────────────────────────────────

def add_article(
    source_id: int,
    title: str,
    url: str,
    summary: str = "",
    content_hash: str = "",
) -> int:
    """Insert one article. Returns its id, or the EXISTING id on a hash dup."""
    with db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (source_id, title, url, summary, content_hash)
               VALUES (?, ?, ?, ?, ?)""",
            (source_id, title, url, summary, content_hash),
        )
        if cur.rowcount:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM articles WHERE source_id = ? AND content_hash = ?",
            (source_id, content_hash),
        ).fetchone()
    return row["id"] if row else 0


def add_articles_batch(source_id: int, items: list[dict]) -> list[int]:
    """
    Insert many articles for one source in ONE transaction (Phase A used to
    commit per row). Returns the article id for each item, dup-safe.
    """
    ids = []
    with db() as conn:
        for item in items:
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles
                   (source_id, title, url, summary, content_hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (source_id, item.get("title", ""), item.get("url", ""),
                 item.get("summary", ""), item.get("content_hash", "")),
            )
            if cur.rowcount:
                ids.append(cur.lastrowid)
            else:
                row = conn.execute(
                    "SELECT id FROM articles WHERE source_id = ? AND content_hash = ?",
                    (source_id, item.get("content_hash", "")),
                ).fetchone()
                ids.append(row["id"] if row else 0)
    return ids


def get_article(article_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
    return dict(row) if row else None


def source_seen_hashes(source_id: int) -> set[str]:
    """All content hashes ever recorded for one canonical source."""
    with db() as conn:
        rows = conn.execute(
            "SELECT content_hash FROM articles WHERE source_id = ?", (source_id,)
        ).fetchall()
    return {r["content_hash"] for r in rows if r["content_hash"]}


def stream_seen_hashes(stream_id: int) -> set[str]:
    """
    All content hashes this STREAM has seen, via any source it subscribes to.

    Dedup is per stream: two streams following overlapping sources must EACH
    receive an article, while two sources inside one stream (e.g. Google News +
    the publisher directly) must not deliver it twice.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT a.content_hash FROM articles a
               JOIN stream_sources ss ON a.source_id = ss.source_id
               WHERE ss.stream_id = ?""",
            (stream_id,),
        ).fetchall()
    return {r["content_hash"] for r in rows if r["content_hash"]}


def set_article_summary(article_id: int, summary: str) -> None:
    """Persist a computed summary so retries don't re-crawl and re-summarize."""
    with db() as conn:
        conn.execute(
            "UPDATE articles SET summary = ? WHERE id = ?", (summary, article_id)
        )


def set_article_embedding(article_id: int, blob: bytes) -> None:
    with db() as conn:
        conn.execute("UPDATE articles SET embedding = ? WHERE id = ?",
                     (blob, article_id))


def get_latest_articles_for_user(user_id: int, limit: int = 20) -> list[dict]:
    """Latest articles across THIS user's streams only (tenant-scoped)."""
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT a.*, s.name AS source_name, s.url AS source_url
               FROM articles a
               JOIN sources s ON a.source_id = s.id
               JOIN stream_sources ss ON a.source_id = ss.source_id
               JOIN streams st ON ss.stream_id = st.id
               WHERE st.user_id = ?
               ORDER BY a.fetched_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Deliveries ────────────────────────────────────────────────────────────────

def create_delivery(article_id: int, stream_id: int, status: str = "new") -> bool:
    """Queue one article for one stream. Returns False if it already existed."""
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO deliveries (article_id, stream_id, status) "
            "VALUES (?, ?, ?)",
            (article_id, stream_id, status),
        )
        return bool(cur.rowcount)


def get_queued_deliveries(per_stream_limit: int, global_limit: int) -> list[dict]:
    """
    Deliveries awaiting processing, oldest first, for active streams only —
    capped per stream so one noisy stream can't starve the others, and capped
    globally as a safety ceiling.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT d.article_id, d.stream_id, d.attempts,
                      a.title, a.url, a.summary, a.content_hash, a.embedding,
                      a.source_id, s.name AS source_name,
                      s.feed_url AS source_feed_url, st.user_id
               FROM deliveries d
               JOIN articles a ON d.article_id = a.id
               JOIN sources s ON a.source_id = s.id
               JOIN streams st ON d.stream_id = st.id
               WHERE d.status = 'new' AND st.status = 'active'
               ORDER BY a.fetched_at ASC""",
        ).fetchall()

    out, per_stream = [], {}
    for row in rows:
        d = dict(row)
        sid = d["stream_id"]
        if per_stream.get(sid, 0) >= per_stream_limit:
            continue
        per_stream[sid] = per_stream.get(sid, 0) + 1
        out.append(d)
        if len(out) >= global_limit:
            break
    return out


def update_delivery_status(article_id: int, stream_id: int, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE deliveries SET status = ? WHERE article_id = ? AND stream_id = ?",
            (status, article_id, stream_id),
        )


def mark_delivery_posted(article_id: int, stream_id: int, post_html: str) -> None:
    """Terminal success — and store EXACTLY what was sent (provenance)."""
    with db() as conn:
        conn.execute(
            "UPDATE deliveries SET status = 'posted', post_html = ?, "
            "posted_at = datetime('now') WHERE article_id = ? AND stream_id = ?",
            (post_html, article_id, stream_id),
        )


def increment_delivery_attempts(article_id: int, stream_id: int) -> int:
    """Count a transient processing failure; returns the new attempt count."""
    with db() as conn:
        conn.execute(
            "UPDATE deliveries SET attempts = COALESCE(attempts, 0) + 1 "
            "WHERE article_id = ? AND stream_id = ?",
            (article_id, stream_id),
        )
        row = conn.execute(
            "SELECT attempts FROM deliveries WHERE article_id = ? AND stream_id = ?",
            (article_id, stream_id),
        ).fetchone()
    return row["attempts"] if row else 0


def set_delivery_verdict(article_id: int, stream_id: int, verdict: str) -> bool:
    """Record the user's 👍/👎 on a delivered post."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE deliveries SET verdict = ? WHERE article_id = ? AND stream_id = ?",
            (verdict, article_id, stream_id),
        )
        return bool(cur.rowcount)


def get_delivery(article_id: int, stream_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE article_id = ? AND stream_id = ?",
            (article_id, stream_id),
        ).fetchone()
    return dict(row) if row else None


def recent_posted_embeddings(stream_id: int, hours: int = 72,
                             exclude_article_id: int = 0) -> list[tuple[int, bytes]]:
    """
    Embeddings of articles POSTED to this stream in the last N hours — the
    comparison set for story-level semantic dedup.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT a.id, a.embedding FROM deliveries d
               JOIN articles a ON d.article_id = a.id
               WHERE d.stream_id = ? AND d.status = 'posted'
                 AND d.posted_at >= datetime('now', ?)
                 AND a.embedding IS NOT NULL AND a.id != ?""",
            (stream_id, f"-{int(hours)} hours", exclude_article_id),
        ).fetchall()
    return [(r["id"], r["embedding"]) for r in rows]


# ── Retention (§2.3) ─────────────────────────────────────────────────────────

def prune_old_articles(days: int = 30) -> int:
    """
    Delete articles older than N days that nothing will ever read again:
    baseline rows and articles whose deliveries all ended negative. Articles
    with a 'posted' delivery are KEPT (provenance / "why did I get this?"),
    as is anything still queued. Returns how many articles were deleted.
    """
    with db() as conn:
        cur = conn.execute(
            """DELETE FROM articles WHERE fetched_at < datetime('now', ?)
               AND id NOT IN (
                   SELECT article_id FROM deliveries
                   WHERE status IN ('new', 'posted')
               )""",
            (f"-{int(days)} days",),
        )
        return cur.rowcount


# ── Usage accounting (§3.3) ──────────────────────────────────────────────────

def increment_usage(user_id: int, kind: str, n: int = 1, day: str = "") -> None:
    day = day or _today()
    with db() as conn:
        conn.execute(
            """INSERT INTO usage (user_id, day, kind, n) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, day, kind) DO UPDATE SET n = n + excluded.n""",
            (user_id, day, kind, n),
        )


def get_usage(user_id: int, kind: str, day: str = "") -> int:
    day = day or _today()
    with db() as conn:
        row = conn.execute(
            "SELECT n FROM usage WHERE user_id = ? AND day = ? AND kind = ?",
            (user_id, day, kind),
        ).fetchone()
    return row["n"] if row else 0


# ── Feedback / source score decay (§3.7) ─────────────────────────────────────

def stream_source_stats(days: int = 30) -> list[dict]:
    """
    Per (stream, source): delivery outcomes and thumb counts over the last N
    days — the input to the nightly quality_score fold.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT d.stream_id, a.source_id,
                      SUM(CASE WHEN d.status = 'posted' THEN 1 ELSE 0 END) AS posted,
                      SUM(CASE WHEN d.status = 'irrelevant' THEN 1 ELSE 0 END) AS irrelevant,
                      SUM(CASE WHEN d.verdict = 'up' THEN 1 ELSE 0 END) AS ups,
                      SUM(CASE WHEN d.verdict = 'down' THEN 1 ELSE 0 END) AS downs
               FROM deliveries d JOIN articles a ON d.article_id = a.id
               WHERE d.created_at >= datetime('now', ?)
               GROUP BY d.stream_id, a.source_id""",
            (f"-{int(days)} days",),
        ).fetchall()
    return [dict(r) for r in rows]


def set_subscription_score(stream_id: int, source_id: int, score: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE stream_sources SET quality_score = ? "
            "WHERE stream_id = ? AND source_id = ?",
            (score, stream_id, source_id),
        )


# ── Internal Source DB (cross-stream) ────────────────────────────────────────

def set_source_embedding(source_id: int, blob: bytes) -> None:
    with db() as conn:
        conn.execute("UPDATE sources SET embedding = ? WHERE id = ?",
                     (blob, source_id))


def sources_missing_embedding(stream_id: int = None) -> list[dict]:
    """Sources with no embedding yet (optionally scoped to one stream's subs)."""
    if stream_id is None:
        sql = "SELECT * FROM sources WHERE embedding IS NULL"
        args = ()
    else:
        sql = ("SELECT s.* FROM sources s "
               "JOIN stream_sources ss ON s.id = ss.source_id "
               "WHERE s.embedding IS NULL AND ss.stream_id = ?")
        args = (stream_id,)
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_source_row(r) for r in rows]


def get_embedded_sources(exclude_stream_id: int = None) -> list[dict]:
    """
    Every canonical source that has an embedding. Optionally exclude sources
    the given stream already follows, so research doesn't 'reuse' the sources
    it just added.
    """
    sql = ("SELECT id, url, name, broad_category, site_type, specific_keywords, "
           "description, feed_url, fetch_method, embedding "
           "FROM sources WHERE embedding IS NOT NULL AND fetch_status = 'active'")
    args = ()
    if exclude_stream_id is not None:
        sql += (" AND id NOT IN (SELECT source_id FROM stream_sources "
                "WHERE stream_id = ?)")
        args = (exclude_stream_id,)
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
