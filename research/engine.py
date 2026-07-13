"""
Research Engine Orchestrator.
Coordinates the 4-phase research pipeline:
  1. Query Understanding (profile building)
  2. Candidate Discovery (parallel Brave Search)
  3. Source Qualification (parallel sub-agents) — includes feed_url identification
  4. Fetch Validation (crawl4ai test)

Includes deterministic domain-level dedup to prevent duplicate sources.
"""
import asyncio
import logging
from typing import TypedDict, Optional, Callable, Awaitable
from urllib.parse import urlparse

import config
from database import store
from research.profile_builder import build_profile
from research.discovery import generate_search_queries, search_parallel
from research.qualification import qualify_all
from research.validator import validate_sources

logger = logging.getLogger(__name__)


# ── LangGraph State ───────────────────────────────────────────────────────────

class ResearchState(TypedDict, total=False):
    answers: dict                      # raw user answers
    profile: dict                      # Source Criteria Profile
    db_matches: list[dict]             # internal DB hits
    search_queries: list[str]          # generated queries
    candidates: list[str]              # raw URLs from discovery
    qualified: list[dict]              # scored qualification results
    validated: list[dict]              # fetch validation results
    final_sources: list[dict]          # sources to store
    log: list[str]                     # progress messages
    stream_id: int                     # DB stream ID
    error: Optional[str]


# ── Progress callback type ───────────────────────────────────────────────────
ProgressCallback = Optional[Callable[[str], Awaitable[None]]]


async def _noop(msg: str) -> None:
    pass


# ── Deduplication ────────────────────────────────────────────────────────────

def _get_domain(url: str) -> str:
    """Extract the registered domain from a URL (e.g. coindesk.com from www.coindesk.com/path)."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    # Strip leading www.
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def dedup_by_domain(sources: list[dict]) -> list[dict]:
    """
    Deterministic domain-level dedup.
    Keeps the highest-scoring source per domain, removes all others.
    """
    seen_domains = {}
    for src in sources:
        domain = _get_domain(src.get("url", ""))
        if not domain:
            continue
        if domain not in seen_domains:
            seen_domains[domain] = src
        else:
            # Keep the one with the higher score
            existing = seen_domains[domain]
            if src.get("match_score", 0) > existing.get("match_score", 0):
                seen_domains[domain] = src

    result = list(seen_domains.values())
    result.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    return result


# ── Node functions ───────────────────────────────────────────────────────────

async def node_build_profile(state: ResearchState,
                              progress: ProgressCallback = None) -> ResearchState:
    """Phase 1: Build Source Criteria Profile from answers."""
    progress = progress or _noop
    await progress("🧠 Building source criteria profile...")
    state["log"].append("Phase 1: Building profile")

    profile = await build_profile(state["answers"])
    state["profile"] = profile

    await progress(
        f"✅ Profile ready: **{profile.get('broad_domain') or '?'}** — "
        f"{len(profile.get('keywords') or [])} keywords"
    )
    return state


async def node_check_db(state: ResearchState,
                         progress: ProgressCallback = None) -> ResearchState:
    """
    Check the internal DB for sources that already cover this topic — by MEANING,
    so 'EU crypto regulation' finds a source tagged 'European digital-asset law'.
    Matches are seeds; they still go through qualification for this user.
    """
    progress = progress or _noop
    state["log"].append("Phase 2: Checking internal DB")

    profile = state["profile"]
    from research import embeddings
    db_matches = await embeddings.find_internal_semantic(
        profile, exclude_stream_id=state.get("stream_id")
    )

    # Fall back to the old literal match if embeddings are unavailable.
    if not db_matches:
        db_matches = store.find_internal_sources(
            broad_category=profile.get("broad_domain", ""),
            keywords=profile.get("keywords", []),
        )

    state["db_matches"] = db_matches
    if db_matches:
        await progress(f"🗄️ Reusing {len(db_matches)} known source(s) from past research")

    return state


async def node_generate_queries(state: ResearchState,
                                 progress: ProgressCallback = None) -> ResearchState:
    """Generate varied search queries from the profile."""
    progress = progress or _noop
    await progress("🔍 Generating search queries...")
    state["log"].append("Phase 2: Generating queries")

    queries = await generate_search_queries(state["profile"])
    state["search_queries"] = queries

    await progress(f"✅ Generated {len(queries)} search queries")
    return state


async def node_discover(state: ResearchState,
                         progress: ProgressCallback = None) -> ResearchState:
    """Phase 2: Run parallel search across all queries."""
    progress = progress or _noop
    await progress(f"🔎 Searching {len(state['search_queries'])} queries in parallel...")
    state["log"].append("Phase 2: Discovery (parallel)")

    candidates = await search_parallel(state["search_queries"])

    # Seed with internal-DB matches from previous research runs (the overview's
    # "check our internal db first") — they still go through qualification so
    # they're scored against THIS user's profile
    seen_domains = {_get_domain(c) for c in candidates}
    for match in state.get("db_matches", []):
        url = match.get("url", "")
        if url and _get_domain(url) not in seen_domains:
            candidates.append(url)
            seen_domains.add(_get_domain(url))

    state["candidates"] = candidates

    await progress(f"✅ Discovery: {len(candidates)} unique candidate URLs")
    return state


async def node_qualify(state: ResearchState,
                        progress: ProgressCallback = None) -> ResearchState:
    """Phase 3: Qualify all candidates in parallel."""
    progress = progress or _noop
    candidates = state["candidates"]
    await progress(f"🔬 Qualifying {len(candidates)} candidates (parallel sub-agents)...")
    state["log"].append("Phase 3: Qualification (parallel)")

    async def progress_cb(done: int, total: int):
        await progress(f"   ⏳ Qualified {done}/{total}...")

    # §2.5: internal-DB matches similar enough to trust skip the Stage-1
    # prefilter and go straight to deep qualification.
    priority = {
        m["url"] for m in state.get("db_matches", [])
        if (m.get("similarity") or 0) >= config.CACHE_SKIP_STAGE1_SIMILARITY
    }
    if priority:
        logger.info("Internal-DB fast path: %d cached source(s) skip Stage 1",
                    len(priority))

    qualified = await qualify_all(candidates, state["profile"],
                                  progress_callback=progress_cb,
                                  priority_urls=priority)

    # ── Deterministic domain-level dedup ────────────────────────────────
    before_dedup = len(qualified)
    qualified = dedup_by_domain(qualified)
    after_dedup = len(qualified)

    if before_dedup != after_dedup:
        logger.info("Dedup removed %d duplicate(s) (%d → %d)",
                    before_dedup - after_dedup, before_dedup, after_dedup)
        await progress(f"🔀 Dedup: removed {before_dedup - after_dedup} duplicate source(s)")

    state["qualified"] = qualified

    top_score = qualified[0].get('match_score', 0) if qualified else 0
    await progress(
        f"✅ Qualification complete: {len(qualified)} sources passed "
        f"(top score: {top_score})"
    )
    return state


def _feed_url_of(q: dict) -> str:
    """The URL we will actually crawl for this qualified source."""
    return q.get("feed_url") or q["url"]


def _fetch_method_of(q: dict) -> str:
    """The proven way to read this source, so the poller needn't re-guess."""
    # Feed repair may have already determined the method from a verified page.
    if q.get("fetch_method"):
        return q["fetch_method"]
    from pipeline.fetch_news import RSS_URL_HINTS
    feed = _feed_url_of(q).lower()
    if any(h in feed for h in RSS_URL_HINTS):
        return "rss"
    return "links"  # a crawlable article-list page


def _to_source_dict(q: dict, fetch_status: str = "active") -> dict:
    return {
        "url": q["url"],
        "name": q.get("source_name", ""),
        "broad_category": q.get("broad_category", ""),
        "site_type": q.get("site_type", ""),
        "specific_keywords": q.get("specific_keywords", []),
        "description": q.get("description", ""),
        "quality_score": q.get("match_score", 0),
        "fetch_status": fetch_status,
        "feed_url": _feed_url_of(q),
        "fetch_method": _fetch_method_of(q),
        # The qualifier's judged publishing frequency drives the polling tier
        # (§2.6) — a monthly blog doesn't need 48 crawls a day.
        "pub_frequency": q.get("frequency") or "",
    }


async def node_validate(state: ResearchState,
                         progress: ProgressCallback = None) -> ResearchState:
    """Phase 4: Repair each top source's feed_url, then validate what's left.

    Repair must run BEFORE the validation filter: a good publication whose
    qualifier-LLM named a wrong feed_url would otherwise fail validation and be
    discarded — the exact sources the repair exists to save. A repair that
    proves a page (crawls it and counts its articles) IS a validation, so those
    sources skip the second crawl entirely.
    """
    progress = progress or _noop
    state["log"].append("Phase 4: Feed repair + validation")

    top = state["qualified"][:config.DESIRED_SOURCES_MAX]
    if top:
        await progress(f"🔧 Confirming each of {len(top)} sources' news pages...")
        top = list(await asyncio.gather(*(_repair_feed_url(q) for q in top)))
        state["qualified"][:config.DESIRED_SOURCES_MAX] = top

    proven = {_feed_url_of(q) for q in top if q.get("_feed_verified")}
    to_check = list({_feed_url_of(q) for q in top} - proven)

    validated = []
    if to_check:
        await progress(f"🌐 Testing {len(to_check)} feed pages with web crawler...")
        validated = await validate_sources(to_check)
    validated += [{"url": u, "fetchable": True, "status": "active",
                   "title": "", "content_preview": "", "error": None}
                  for u in proven]
    state["validated"] = validated

    fetchable = [v for v in validated if v["fetchable"]]
    await progress(f"✅ Validation: {len(fetchable)}/{len(validated)} sources are fetchable")
    return state


async def _repair_feed_url(src: dict) -> dict:
    """
    The qualifier asks an LLM to name each source's article-list page. Trust but
    verify: if that page doesn't actually expose articles, fall back to the
    deterministic finder. A source whose feed_url is a marketing homepage would
    silently never produce news.

    Sets src["_feed_verified"] = True when the page was PROVEN to list articles
    (either the LLM's URL checked out, or the finder returned a verified page),
    so validation doesn't have to crawl it again.
    """
    from crawler.fetcher import fetch_page
    from pipeline.fetch_news import (RSS_URL_HINTS, article_links_on_page,
                                     fetch_rss_items)
    from research.feed_finder import find_news_pages, MIN_ARTICLE_LINKS

    feed_url = src.get("feed_url") or src["url"]

    # An RSS feed_url proves itself with one cheap HTTP GET — pointing the
    # browser at raw XML would find zero "article links" and wrongly repair it.
    if any(h in feed_url.lower() for h in RSS_URL_HINTS):
        if await fetch_rss_items(feed_url):
            src["_feed_verified"] = True
            src["fetch_method"] = "rss"
            return src

    page = await fetch_page(feed_url)
    if page["success"] and len(article_links_on_page(page, feed_url)) >= MIN_ARTICLE_LINKS:
        src["_feed_verified"] = True
        src.setdefault("fetch_method", "links")
        return src  # the LLM was right

    logger.info("feed_url %s exposes too few articles — rediscovering", feed_url)
    try:
        candidates = await find_news_pages(src["url"])
    except Exception as e:
        logger.warning("Feed rediscovery failed for %s: %s", src["url"], e)
        return src

    if candidates:
        best = candidates[0]
        logger.info("Repaired feed_url for %s: %s → %s",
                    src["url"], feed_url, best.url)
        src["feed_url"] = best.url
        if best.kind == "feed":
            src["fetch_method"] = "rss"
        elif getattr(best, "scope", "internal") == "external":
            src["fetch_method"] = "links_ext"
        else:
            src["fetch_method"] = "links"
        src["_feed_verified"] = True
    return src


async def node_finalize(state: ResearchState,
                         progress: ProgressCallback = None) -> ResearchState:
    """Merge qualification + validation results, prepare for storage."""
    progress = progress or _noop
    state["log"].append("Phase 4: Finalizing")

    # Map validation results by feed URL
    val_map = {v["url"]: v for v in state.get("validated", [])}

    def _is_fetchable(q: dict) -> bool:
        return val_map.get(_feed_url_of(q), {}).get("fetchable", False)

    final = [_to_source_dict(q)
             for q in state["qualified"][:config.DESIRED_SOURCES_MAX]
             if _is_fetchable(q)]

    # If too few fetchable, relax to include lower-ranked qualified sources
    if len(final) < config.DESIRED_SOURCES_MIN:
        extra = state["qualified"][config.DESIRED_SOURCES_MAX:]
        extra_urls = [_feed_url_of(q) for q in extra
                      if _feed_url_of(q) not in val_map]
        if extra_urls:
            for v in await validate_sources(extra_urls):
                val_map[v["url"]] = v
        for q in extra:
            if _is_fetchable(q):
                final.append(_to_source_dict(q))
            if len(final) >= config.DESIRED_SOURCES_MIN:
                break

    state["final_sources"] = final
    await progress(f"🎯 Final result: {len(final)} validated sources ready")
    return state


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def run_research(answers: dict, stream_id: int,
                        progress: ProgressCallback = None) -> dict:
    """
    Run the full 4-phase research pipeline.
    Returns the final ResearchState with all sources found.
    """
    progress = progress or _noop
    logger.info("Starting research for stream %d", stream_id)

    state: ResearchState = {
        "answers": answers,
        "stream_id": stream_id,
        "log": [],
        "db_matches": [],
        "candidates": [],
        "qualified": [],
        "validated": [],
        "final_sources": [],
    }

    try:
        state = await node_build_profile(state, progress)
        state = await node_check_db(state, progress)
        state = await node_generate_queries(state, progress)
        state = await node_discover(state, progress)
        state = await node_qualify(state, progress)
        state = await node_validate(state, progress)
        state = await node_finalize(state, progress)

    except Exception as e:
        logger.exception("Research pipeline failed")
        state["error"] = str(e)
        await progress(f"❌ Research error: {e}")

    # Store final sources in DB (with feed_url)
    stored_urls = set()
    for src in state.get("final_sources", []):
        stored_urls.add(src["url"])
        if not store.get_source_by_url(stream_id, src["url"]):
            store.add_source(stream_id=stream_id, **src)

    # Also store blocked sources (marked as blocked) for reference
    val_map = {v["url"]: v for v in state.get("validated", [])}
    for q in state["qualified"][:config.DESIRED_SOURCES_MAX]:
        if q["url"] in stored_urls:
            continue
        val = val_map.get(_feed_url_of(q), {})
        if not val.get("fetchable", False):
            if not store.get_source_by_url(stream_id, q["url"]):
                store.add_source(stream_id=stream_id,
                                 **_to_source_dict(q, fetch_status="blocked"))

    # Always give the stream a Google News feed for its topic — an aggregator
    # across every publication, on-topic by construction. Per-article relevance
    # and caps keep it in check.
    await _add_google_news_source(stream_id, state.get("profile", {}), progress)

    # Embed the sources we just stored so future research can find them by meaning.
    try:
        from research import embeddings
        await embeddings.backfill_stream_embeddings(stream_id)
    except Exception:
        logger.exception("Embedding backfill failed (non-fatal)")

    logger.info("Research complete: %d sources stored for stream %d",
                len(state["final_sources"]), stream_id)
    return state


async def _add_google_news_source(stream_id: int, profile: dict,
                                   progress: ProgressCallback = None) -> None:
    """Attach a verified Google News topic feed to the stream (idempotent)."""
    from research.aggregators import google_news_feed_url, news_query_for
    from pipeline.fetch_news import fetch_rss_items

    if not profile:
        return
    query = news_query_for(profile)
    feed_url = google_news_feed_url(query)

    # One Google News feed per stream, ever. Re-research regenerates the profile
    # and shifts the query text, so keying on the query-bearing URL would add a
    # duplicate aggregator on every re-run.
    if any("news.google.com" in ((s.get("feed_url") or "").lower())
           for s in store.get_sources_by_stream(stream_id)):
        return
    site_url = f"https://news.google.com/search?q={query}"

    items = await fetch_rss_items(feed_url)
    if not items:
        logger.info("Google News feed empty for %r — skipping", query)
        return

    store.add_source(
        stream_id=stream_id,
        url=site_url,
        name=f"Google News: {query}"[:100],
        broad_category=profile.get("broad_domain", ""),
        site_type="aggregator",
        description=(f"Google News aggregator feed for '{query}'. Pulls the latest "
                     f"matching headlines from across many publications."),
        feed_url=feed_url,
        fetch_method="rss",
        fetch_status="active",
        quality_score=60,
    )
    if progress:
        await progress(f"📰 Added a Google News feed for **{query}**")
    logger.info("Added Google News source for stream %d (%r)", stream_id, query)