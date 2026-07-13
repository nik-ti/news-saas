"""
Pipeline — the news cycle.

The single scheduled job. Every run:

  Phase A — poll each DISTINCT active source once (however many streams follow
            it). A source seen for the first time is BASELINED: everything
            currently on its page is recorded and nothing is sent. Fresh
            articles fan out as one `deliveries` row per subscribed active
            stream.
  Phase B — drain the deliveries queue serially: summarize → semantic dedup →
            relevance gate (per-stream rubric) → write post → send to the
            stream's owner, with a per-stream budget so one noisy stream can't
            starve the others.

Every delivery leaves Phase B with a terminal status, so nothing is retried
forever.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import config
from bot.messaging import send_html_message_async, send_rich_async
from database import store
from pipeline.fetch_news import snapshot_source, SourceFetchError, UNCHANGED
from pipeline.post_writer import write_post
from pipeline.relevance_checker import check_relevance
from pipeline.summarize import summarize_article, SKIP, STALE

logger = logging.getLogger(__name__)

# A cycle can outlive its 30-minute interval on a slow crawl. Never run two.
_cycle_lock = asyncio.Lock()


async def run_news_cycle() -> dict:
    """Run one full cycle. Returns stats; skips if a cycle is already running."""
    if _cycle_lock.locked():
        logger.warning("news_cycle: previous cycle still running — skipping this tick")
        return {"skipped": True, "posted": 0, "candidates": 0}

    async with _cycle_lock:
        baselined, queued = await _baseline_and_fetch_phase()
        stats = await _post_phase()
        stats["baselined_sources"] = baselined
        stats["queued"] = queued
        logger.info(
            "news_cycle done: baselined=%d queued=%d candidates=%d posted=%d "
            "irrelevant=%d duplicate=%d dropped=%d retry=%d",
            baselined, queued, stats["candidates"], stats["posted"],
            stats["irrelevant"], stats["duplicate"], stats["dropped"],
            stats["retry"],
        )
        return stats


# ── Phase A ───────────────────────────────────────────────────────────────────

def _looks_like_rebaseline(n_fresh: int, n_total: int) -> bool:
    """
    A wholesale change — site redesign, URL scheme change, feed regenerated —
    makes (nearly) EVERY item on a known source look new at once. Delivering
    those would spam the user with stale articles for days. Treat it as a
    re-baseline, not news.
    """
    if n_total <= 0 or n_fresh < config.REBASELINE_MIN_ITEMS:
        return False
    return n_fresh / n_total >= config.REBASELINE_FRACTION


def _due_for_poll(source: dict, now: datetime = None) -> bool:
    """
    Polling tiers (§2.6): every browser crawl costs a full Chromium render, so
    a source whose PROVEN publishing frequency is low skips ticks. RSS sources
    are always polled — their conditional GET usually costs one 304.
    """
    if (source.get("fetch_method") or "").lower() == "rss":
        return True
    wait_hours = config.POLL_TIER_HOURS.get(source.get("pub_frequency") or "", 0)
    if wait_hours <= 0:
        return True
    last = source.get("last_fetched")
    if not last:
        return True  # never fetched — always due
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last_dt).total_seconds() >= wait_hours * 3600


async def _baseline_and_fetch_phase() -> tuple[int, int]:
    """Snapshot every distinct active source once; baseline new ones, then fan
    fresh articles out as deliveries to every subscribed active stream."""
    sources = [s for s in store.get_active_sources() if _due_for_poll(s)]
    if not sources:
        return 0, 0

    logger.info("news_cycle: snapshotting %d active sources", len(sources))
    snapshots = await asyncio.gather(
        *(snapshot_source(s) for s in sources), return_exceptions=True
    )

    # Circuit breaker: when most sources fail in the same cycle the cause is
    # ours (dead browser, network outage), not theirs. Counting it against
    # individual sources would mass-deactivate the whole system in 3 cycles.
    failures = sum(1 for s in snapshots if isinstance(s, Exception))
    systemic = len(sources) >= 4 and failures > len(sources) // 2
    if systemic:
        logger.error(
            "news_cycle: %d/%d sources failed — systemic failure, resetting the "
            "crawler and NOT counting this cycle against individual sources",
            failures, len(sources),
        )
        from crawler.fetcher import _reset_crawler
        await _reset_crawler()

    baselined = 0
    queued = 0
    # Dedup is per stream (see store.stream_seen_hashes) — one query per
    # stream per cycle instead of one per link.
    seen_by_stream: dict[int, set[str]] = {}

    for source, snapshot in zip(sources, snapshots):
        source_id = source["id"]

        if isinstance(snapshot, Exception):
            if not systemic:
                _record_fetch_failure(source, snapshot)
            continue

        store.update_source_fetch_time(source_id)
        store.reset_fail_count(source_id)

        if snapshot is UNCHANGED:
            continue  # 304 — the feed itself says nothing changed

        # Persist fresh conditional-GET validators handed back by the fetcher.
        if source.get("_new_etag") or source.get("_new_last_modified"):
            store.set_source_conditional(
                source_id, source.get("_new_etag") or None,
                source.get("_new_last_modified") or None,
            )

        source_seen = store.source_seen_hashes(source_id)
        fresh, fresh_hashes = [], set()
        for item in snapshot:
            h = item["content_hash"]
            if h in source_seen or h in fresh_hashes:
                continue
            fresh.append(item)
            fresh_hashes.add(h)

        # Materialize each subscriber stream's seen-set BEFORE inserting this
        # source's articles — computed after, the fresh hashes would look
        # "already seen" and nothing would ever be delivered.
        subscribers = store.active_subscriber_ids(source_id)
        for stream_id in subscribers:
            if stream_id not in seen_by_stream:
                seen_by_stream[stream_id] = store.stream_seen_hashes(stream_id)

        def _mark_seen(hashes):
            for sid in subscribers:
                seen_by_stream[sid].update(hashes)

        # First time we've ever polled this source: record what's already there
        # so it is never mistaken for news, and send nothing.
        if source["baselined_at"] is None:
            store.add_articles_batch(source_id, fresh)
            store.mark_source_baselined(source_id)
            _mark_seen(fresh_hashes)
            baselined += 1
            logger.info("Baselined source %s — %d existing articles recorded, "
                        "0 posted", source["url"], len(fresh))
            continue

        # A known source where (nearly) everything is suddenly "new" changed
        # its page structure, not its news. Re-baseline silently.
        if _looks_like_rebaseline(len(fresh), len(snapshot)):
            store.add_articles_batch(source_id, fresh)
            _mark_seen(fresh_hashes)
            logger.warning(
                "Source %s: %d/%d items suddenly new — page structure changed, "
                "re-baselined instead of posting", source["url"],
                len(fresh), len(snapshot),
            )
            continue

        # Age guard: "new to us" is not "news". An item whose known publication
        # date (feed pubDate / URL date) is past the cutoff — a resurfaced old
        # link, a stale Google News result, a reactivated source's backlog —
        # is recorded so it's never re-examined, but never delivered.
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=config.MAX_ARTICLE_AGE_DAYS))
        deliverable, stale = [], []
        for item in fresh:
            ts = item.get("published_at")
            (stale if ts and ts < cutoff else deliverable).append(item)
        if stale:
            store.add_articles_batch(source_id, stale)
            _mark_seen({i["content_hash"] for i in stale})
            logger.info("Source %s: %d stale item(s) recorded, not delivered",
                        source["url"], len(stale))

        to_queue = deliverable[:config.MAX_NEW_PER_SOURCE]
        article_ids = store.add_articles_batch(source_id, to_queue)

        # Fan out: one delivery per subscribed ACTIVE stream, unless that
        # stream already saw this hash via another of its sources.
        for item, article_id in zip(to_queue, article_ids):
            if not article_id:
                continue
            for stream_id in subscribers:
                if item["content_hash"] in seen_by_stream[stream_id]:
                    continue
                if store.create_delivery(article_id, stream_id):
                    queued += 1
            _mark_seen({item["content_hash"]})

        # Anything beyond the per-source cap is left undiscovered; it will be
        # picked up next cycle (it stays absent from the dedup set).
        if len(deliverable) > config.MAX_NEW_PER_SOURCE:
            logger.info("Source %s: %d new, capped to %d this cycle",
                        source["url"], len(deliverable), config.MAX_NEW_PER_SOURCE)

    return baselined, queued


def _record_fetch_failure(source: dict, err: Exception) -> None:
    """Tolerate transient failures; only mark 'error' after N consecutive ones."""
    if not isinstance(err, SourceFetchError):
        logger.error("Snapshot crashed for %s: %s", source["url"], err)
    else:
        logger.warning("Fetch failed for source %s: %s", source["url"], err)

    fails = store.increment_fail_count(source["id"])
    if fails >= config.MAX_CONSECUTIVE_FETCH_FAILURES:
        store.update_source_status(source["id"], "error")
        logger.warning("Source %s marked as error after %d consecutive failures",
                       source["url"], fails)


# ── Phase B ───────────────────────────────────────────────────────────────────

def _retry_or_drop(article_id: int, stream_id: int, why: str) -> str:
    """
    A transient failure (LLM outage, network blip, Telegram 5xx) must not cost
    the user an article. Leave it queued and try again next cycle, until it has
    burned through its attempt budget.
    """
    attempts = store.increment_delivery_attempts(article_id, stream_id)
    if attempts >= config.MAX_ARTICLE_ATTEMPTS:
        logger.warning("Delivery %d/%d dropped after %d attempts (%s)",
                       article_id, stream_id, attempts, why)
        store.update_delivery_status(article_id, stream_id, "dropped")
        return "dropped"
    logger.info("Delivery %d/%d left queued for retry %d/%d (%s)",
                article_id, stream_id, attempts, config.MAX_ARTICLE_ATTEMPTS, why)
    return "retry"


def _parse_quiet_hours(profile: dict) -> tuple[int, int] | None:
    """criteria["quiet_hours"] = "23-8" (server-local hours). None = no quiet."""
    raw = (profile or {}).get("quiet_hours") or ""
    if not raw or not isinstance(raw, str) or "-" not in raw:
        return None
    try:
        start_s, end_s = raw.split("-", 1)
        start, end = int(start_s), int(end_s)
        if 0 <= start <= 23 and 0 <= end <= 23 and start != end:
            return start, end
    except ValueError:
        pass
    return None


def _in_quiet_hours(profile: dict, now: datetime = None) -> bool:
    """Quiet hours (§3.6): posts are held — not dropped — inside the window."""
    window = _parse_quiet_hours(profile)
    if window is None:
        return False
    start, end = window
    hour = (now or datetime.now()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight (e.g. 23-8)


async def _is_semantic_duplicate(article_id: int, stream_id: int,
                                 title: str, summary: str) -> bool:
    """
    Story-level semantic dedup (§3.2): the same story arrives as a Google News
    redirect AND the publisher's URL — different hashes, both queued. Embed the
    candidate and compare against what this stream ALREADY had posted in the
    last window. Degrades to False when embeddings are unavailable.
    """
    try:
        from research import embeddings
        vec = await embeddings.embed(f"{title}\n{summary}"[:2000])
        if not vec:
            return False
        store.set_article_embedding(article_id, embeddings.to_blob(vec))
        recent = store.recent_posted_embeddings(
            stream_id, hours=config.STORY_DEDUP_HOURS,
            exclude_article_id=article_id,
        )
        hits = embeddings.cosine_top(vec, recent, top_k=1,
                                     threshold=config.STORY_DEDUP_THRESHOLD)
        return bool(hits)
    except Exception:
        logger.exception("Semantic dedup failed (treated as not-duplicate)")
        return False


async def _auto_pause_if_unreachable(stream_id: int, user_id: int) -> None:
    """
    §3.1: a stream whose owner blocked the bot keeps burning crawls and LLM
    calls forever. After N consecutive terminal send failures, pause it and
    tell the admin (the owner is unreachable by definition).
    """
    streak = store.record_send_result(stream_id, ok=False)
    if streak < config.AUTO_PAUSE_SEND_FAILURES:
        return
    store.update_stream_status(stream_id, "paused")
    logger.warning("Stream %d auto-paused after %d consecutive send failures",
                   stream_id, streak)
    try:
        await send_rich_async(
            config.ADMIN_USER_ID,
            f"⏸️ Stream `{stream_id}` (user `{user_id}`) auto-paused after "
            f"{streak} consecutive failed sends — the user has likely blocked "
            f"the bot. `/resumestream {stream_id}` re-enables it.",
        )
    except Exception:
        logger.exception("Admin notify failed for auto-pause")


def _feedback_keyboard(article_id: int, stream_id: int) -> dict:
    """Inline 👍/👎 on every post (§3.7) — the cheapest data asset there is."""
    return {"inline_keyboard": [[
        {"text": "👍", "callback_data": f"fb:{article_id}:{stream_id}:up"},
        {"text": "👎", "callback_data": f"fb:{article_id}:{stream_id}:down"},
    ]]}


async def _post_phase() -> dict:
    """Drain the deliveries queue serially, budgeted per stream."""
    queue = store.get_queued_deliveries(
        per_stream_limit=config.MAX_POSTS_PER_STREAM_PER_CYCLE,
        global_limit=config.MAX_POSTS_PER_CYCLE,
    )
    stats = {"candidates": len(queue), "posted": 0, "irrelevant": 0,
             "duplicate": 0, "dropped": 0, "retry": 0, "held": 0, "stale": 0}
    if not queue:
        return stats

    logger.info("news_cycle: processing %d queued deliveries", len(queue))

    # One profile lookup per stream, not per delivery.
    profiles: dict[int, dict] = {}

    for delivery in queue:
        article_id = delivery["article_id"]
        stream_id = delivery["stream_id"]
        try:
            if stream_id not in profiles:
                stream = store.get_stream(stream_id)
                profiles[stream_id] = (stream or {}).get("criteria") or {}
            profile = profiles[stream_id]

            # Quiet hours: hold — no status change, no attempt charged.
            if _in_quiet_hours(profile):
                stats["held"] += 1
                continue

            summary, title = await summarize_article(delivery)
            if summary == SKIP:
                logger.info("Article %d not a usable news page — dropped", article_id)
                store.update_delivery_status(article_id, stream_id, "unusable")
                stats["dropped"] += 1
                continue
            if summary == STALE:
                logger.info("Article %d is old news per its own dateline — "
                            "not delivered", article_id)
                store.update_delivery_status(article_id, stream_id, "stale")
                stats["stale"] += 1
                continue

            # Persist the summary: a retry (LLM outage, Telegram 5xx) then costs
            # one LLM call instead of a crawl + summarize per attempt.
            if summary != (delivery.get("summary") or "").strip():
                store.set_article_summary(article_id, summary)

            if await _is_semantic_duplicate(article_id, stream_id, title, summary):
                logger.info("Article %d is a semantic duplicate for stream %d: %s",
                            article_id, stream_id, title[:60])
                store.update_delivery_status(article_id, stream_id, "duplicate")
                stats["duplicate"] += 1
                continue

            is_relevant, reason = await check_relevance(title, summary, profile)
            if not is_relevant:
                logger.info("Article %d gated out (%s): %s", article_id, reason,
                            title[:60])
                store.update_delivery_status(article_id, stream_id, "irrelevant")
                stats["irrelevant"] += 1
                continue

            post_html = await write_post(
                summary, title=title, source_url=delivery.get("url", ""),
                length=profile.get("post_length", "standard"),
                language=profile.get("language", ""),
            )
            if not post_html or len(post_html) < 20:
                stats[_retry_or_drop(article_id, stream_id,
                                     "post writer returned nothing")] += 1
                continue

            result = await send_html_message_async(
                delivery["user_id"], post_html,
                reply_markup=_feedback_keyboard(article_id, stream_id),
            )
            if result.get("ok"):
                store.mark_delivery_posted(article_id, stream_id, post_html)
                store.record_send_result(stream_id, ok=True)
                stats["posted"] += 1
                logger.info("Posted article %d to stream %d: %s",
                            article_id, stream_id, title[:60])
                await asyncio.sleep(2)  # stay clear of Telegram rate limits
            elif _is_terminal_send_error(result):
                logger.error("Permanent send failure for delivery %d/%d: %s",
                             article_id, stream_id, result.get("description"))
                store.update_delivery_status(article_id, stream_id, "send_failed")
                stats["dropped"] += 1
                await _auto_pause_if_unreachable(stream_id, delivery["user_id"])
            else:
                stats[_retry_or_drop(article_id, stream_id, "send failed")] += 1

        except Exception as e:
            logger.exception("Error processing delivery %d/%d",
                             article_id, stream_id)
            stats[_retry_or_drop(article_id, stream_id,
                                 f"{type(e).__name__}")] += 1

    return stats


def _is_terminal_send_error(result: dict) -> bool:
    """403 (bot blocked) and 400 (bad chat / unparseable HTML) will never succeed."""
    return result.get("error_code") in (400, 403)
