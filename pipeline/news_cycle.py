"""
Pipeline — the news cycle.

The single scheduled job. Every run:

  Phase A — poll each active source's article-list page.
            A source seen for the first time is BASELINED: everything currently
            on its page is recorded as 'seen' and nothing is sent. Only articles
            that appear on later cycles are queued as 'new'.
  Phase B — drain the queue serially: summarize → relevance gate → write post →
            send to the stream's owner.

Every article leaves Phase B with a terminal status, so nothing is retried forever.
"""
import asyncio
import logging

import config
from bot.messaging import send_html_message_async
from database import store
from pipeline.fetch_news import snapshot_source, SourceFetchError
from pipeline.post_writer import write_post
from pipeline.relevance_checker import check_relevance
from pipeline.summarize import summarize_article, SKIP

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
            "irrelevant=%d dropped=%d retry=%d",
            baselined, queued, stats["candidates"], stats["posted"],
            stats["irrelevant"], stats["dropped"], stats["retry"],
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


async def _baseline_and_fetch_phase() -> tuple[int, int]:
    """Snapshot every active source; baseline new ones, queue genuinely new articles."""
    sources = store.get_active_sources()
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

        stream_id = source["stream_id"]
        if stream_id not in seen_by_stream:
            seen_by_stream[stream_id] = store.stream_seen_hashes(stream_id)
        seen = seen_by_stream[stream_id]

        fresh = [i for i in snapshot if i["content_hash"] not in seen]
        seen.update(i["content_hash"] for i in fresh)  # intra-cycle dedup too

        # First time we've ever polled this source: record what's already there
        # so it is never mistaken for news, and send nothing.
        if source["baselined_at"] is None:
            for item in fresh:
                store.add_article(
                    source_id=source_id, title=item["title"], url=item["url"],
                    summary=item["summary"], content_hash=item["content_hash"],
                    status="seen",
                )
            store.mark_source_baselined(source_id)
            baselined += 1
            logger.info("Baselined source %s — %d existing articles marked seen, "
                        "0 posted", source["url"], len(fresh))
            continue

        # A known source where (nearly) everything is suddenly "new" changed
        # its page structure, not its news. Re-baseline silently.
        if _looks_like_rebaseline(len(fresh), len(snapshot)):
            for item in fresh:
                store.add_article(
                    source_id=source_id, title=item["title"], url=item["url"],
                    summary=item["summary"], content_hash=item["content_hash"],
                    status="seen",
                )
            logger.warning(
                "Source %s: %d/%d items suddenly new — page structure changed, "
                "re-baselined instead of posting", source["url"],
                len(fresh), len(snapshot),
            )
            continue

        for item in fresh[:config.MAX_NEW_PER_SOURCE]:
            store.add_article(
                source_id=source_id, title=item["title"], url=item["url"],
                summary=item["summary"], content_hash=item["content_hash"],
                status="new",
            )
            queued += 1

        # Anything beyond the per-source cap is left undiscovered; it will be
        # picked up next cycle (it stays absent from the dedup set).
        if len(fresh) > config.MAX_NEW_PER_SOURCE:
            logger.info("Source %s: %d new, capped to %d this cycle",
                        source["url"], len(fresh), config.MAX_NEW_PER_SOURCE)

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

def _retry_or_drop(article_id: int, why: str) -> str:
    """
    A transient failure (LLM outage, network blip, Telegram 5xx) must not cost
    the user an article. Leave it queued and try again next cycle, until it has
    burned through its attempt budget.
    """
    attempts = store.increment_article_attempts(article_id)
    if attempts >= config.MAX_ARTICLE_ATTEMPTS:
        logger.warning("Article %d dropped after %d attempts (%s)",
                       article_id, attempts, why)
        store.update_article_status(article_id, "seen")
        return "dropped"
    logger.info("Article %d left queued for retry %d/%d (%s)",
                article_id, attempts, config.MAX_ARTICLE_ATTEMPTS, why)
    return "retry"


async def _post_phase() -> dict:
    """Drain the queue serially. Global cap enforced by the SELECT's LIMIT."""
    queue = store.get_queued_articles(config.MAX_POSTS_PER_CYCLE)
    stats = {"candidates": len(queue), "posted": 0, "irrelevant": 0,
             "dropped": 0, "retry": 0}
    if not queue:
        return stats

    logger.info("news_cycle: processing %d queued articles", len(queue))

    # One profile lookup per stream, not per article.
    profiles: dict[int, dict] = {}

    for article in queue:
        article_id = article["id"]
        stream_id = article["stream_id"]
        try:
            if stream_id not in profiles:
                stream = store.get_stream(stream_id)
                profiles[stream_id] = (stream or {}).get("criteria") or {}
            profile = profiles[stream_id]

            summary, title = await summarize_article(article)
            if summary == SKIP:
                logger.info("Article %d not a usable news page — dropped", article_id)
                store.update_article_status(article_id, "seen")
                stats["dropped"] += 1
                continue

            is_relevant, reason = await check_relevance(title, summary, profile)
            if not is_relevant:
                logger.info("Article %d gated out (%s): %s", article_id, reason,
                            title[:60])
                store.update_article_status(article_id, "irrelevant")
                stats["irrelevant"] += 1
                continue

            post_html = await write_post(
                summary, title=title, source_url=article.get("url", ""),
                length=profile.get("post_length", "standard"),
            )
            if not post_html or len(post_html) < 20:
                stats[_retry_or_drop(article_id, "post writer returned nothing")] += 1
                continue

            result = await send_html_message_async(article["user_id"], post_html)
            if result.get("ok"):
                store.mark_posted(article_id)
                stats["posted"] += 1
                logger.info("Posted article %d: %s", article_id, title[:60])
                await asyncio.sleep(2)  # stay clear of Telegram rate limits
            elif _is_terminal_send_error(result):
                logger.error("Permanent send failure for article %d: %s",
                             article_id, result.get("description"))
                store.update_article_status(article_id, "seen")
                stats["dropped"] += 1
            else:
                stats[_retry_or_drop(article_id, "send failed")] += 1

        except Exception as e:
            logger.exception("Error processing article %d", article_id)
            stats[_retry_or_drop(article_id, f"{type(e).__name__}")] += 1

    return stats


def _is_terminal_send_error(result: dict) -> bool:
    """403 (bot blocked) and 400 (bad chat / unparseable HTML) will never succeed."""
    return result.get("error_code") in (400, 403)
