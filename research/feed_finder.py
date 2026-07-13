"""
News-page discovery.

A user who adds a source will almost always paste the site's front door
("https://acme.com"), not the page that actually lists its articles. Polling a
marketing homepage yields nothing, forever. This module finds the page(s) worth
polling.

The method, in order of how much it trusts the site:

  1. ASK THE SITE. RSS/Atom autodiscovery — <link rel="alternate"> in the head.
     Not a guess: the site is declaring its feed. One HTTP GET, no browser, and
     entries arrive with summaries already attached.

  2. LET THE CRAWLER LOOK. Crawl the homepage and read the links it really has.
     Where do the article-shaped URLs live? If twenty of them sit under
     /policy/..., then /policy is a news section — and we learned that from the
     site's own structure, not from a list of English words. Non-article links
     shallow enough to be sections are candidates too.

  3. PROVE EVERY CANDIDATE. Crawl it, count the article links it exposes, using
     the exact filter the poller uses. A page that scores here is a page that
     will produce articles later. Nothing is accepted on a hunch.

Vocabulary ("news", "blog") only ever breaks ties in step 2's ordering. It never
decides what is or isn't a candidate, because that would quietly fail on every
site that isn't in English.
"""
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from crawler.fetcher import fetch_page
from pipeline.fetch_news import article_links_on_page, fetch_rss_items
from research.urlutils import (
    derive_source_url, is_article_url, normalise_url, path_segments,
    registered_domain,
)

logger = logging.getLogger(__name__)

# A page must expose at least this many article links to count as a news page.
MIN_ARTICLE_LINKS = 5

# Feeds are cheapest to poll; a feed with this many entries is proof enough.
MIN_FEED_ITEMS = 3

# How many candidate pages we'll spend a browser crawl on.
MAX_VERIFY = 6

# Discovery aims every request at ONE host, so it must behave like a crawler,
# not a load test. Requests go out one at a time with a pause between them.
POLITE_DELAY_SECONDS = 1.0

# Substrings marking a refusal we should back off from and retry once, alone.
_ANTI_BOT = ("anti-bot", "cloudflare", "challenge", "403", "429", "captcha")

# Feeds are frequently unlinked from the head, but a hit here is *parsed* before
# it's believed — so this is verification, not guessing.
COMMON_FEED_PATHS = [
    "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml",
]

# Sections that are never news. Used to skip pointless crawls, never to choose.
UTILITY_SEGMENTS = {
    "about", "contact", "privacy", "terms", "legal", "cookie", "cookies",
    "login", "signin", "sign-in", "signup", "register", "account", "profile",
    "pricing", "plans", "careers", "jobs", "support", "help", "faq", "docs",
    "documentation", "advertise", "subscribe", "cart", "checkout", "shop",
    "store", "search", "sitemap", "team", "impressum", "datenschutz",
}

# Ordering hints only. A site whose section is /actualites still gets found —
# it just doesn't get to jump the queue.
NEWSY_HINTS = {
    "news", "blog", "article", "articles", "latest", "update", "updates",
    "insight", "insights", "press", "newsroom", "stories", "story", "posts",
    "research", "analysis", "publications", "media", "journal", "magazine",
    # a few non-English section names, so the ranking isn't purely anglocentric
    "noticias", "nachrichten", "nouvelles", "actualites", "notizie", "nyheter",
    "novosti", "blogg", "aktuelles",
}

_UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsStreamBot/1.0)"}


@dataclass
class Candidate:
    url: str
    kind: str          # "feed" | "page"
    item_count: int    # articles/entries actually found
    title: str = ""
    scope: str = "internal"   # "internal" | "external" — where its article
                              # links point. External = an outbound aggregator
                              # page whose headlines link to other domains.

    @property
    def label(self) -> str:
        tag = "RSS feed" if self.kind == "feed" else "page"
        return f"{self.url} ({tag}, {self.item_count} articles)"


def _root_of(url: str) -> str:
    p = urlparse(url if "://" in url else f"https://{url}")
    return f"{p.scheme or 'https'}://{p.netloc}"


async def _get(url: str, timeout: int = 12) -> tuple[int, str]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=_UA) as c:
            r = await c.get(url, timeout=timeout)
            return r.status_code, r.text
    except Exception as e:
        logger.debug("GET failed %s: %s", url, e)
        return 0, ""


# ── Step 1: ask the site ──────────────────────────────────────────────────────

async def _autodiscover_feeds(root: str, html: str) -> list[str]:
    """<link rel="alternate" type="application/rss+xml" href="..."> — the correct way."""
    if not html:
        return []
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    found = []
    for tag in soup.find_all("link"):
        rel = " ".join(tag.get("rel") or []).lower()
        typ = (tag.get("type") or "").lower()
        href = tag.get("href") or ""
        if not href or "alternate" not in rel:
            continue
        if "rss" in typ or "atom" in typ or "xml" in typ:
            found.append(urljoin(root, href))
    return found


# ── Step 2: let the crawler look ──────────────────────────────────────────────

def _internal_links(page: dict, root: str) -> list[tuple[str, str]]:
    """Every distinct same-domain link the crawler actually found, as (url, anchor text)."""
    domain = registered_domain(root)
    out, seen = [], set()
    for link in page.get("links", []):
        if not isinstance(link, dict):
            continue
        href = link.get("href") or link.get("url") or ""
        text = (link.get("text") or link.get("title") or "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        abs_url = urljoin(root, href)
        if not abs_url.startswith("http") or registered_domain(abs_url) != domain:
            continue
        key = normalise_url(abs_url)
        if key in seen:
            continue
        seen.add(key)
        out.append((abs_url, text))
    return out


def _hint_score(url: str, anchor: str) -> float:
    """Tiebreaker only. Nudges obvious section names up; never excludes anything."""
    segs = path_segments(url)
    last = segs[-1].lower() if segs else ""
    text = anchor.lower().strip()
    score = 0.0
    if last in NEWSY_HINTS:
        score += 2
    if text in NEWSY_HINTS:
        score += 2
    elif any(w in text for w in NEWSY_HINTS):
        score += 1
    score -= 0.1 * len(segs)          # prefer shallower sections
    return score


def _structural_candidates(home: dict, root: str, given: str) -> list[str]:
    """
    Infer this site's news sections from where its article links actually live.

    Two signals, both structural:
      * Article-shaped URLs on the homepage → their shared parent is a section.
        Twenty links under /policy/2026/... means /policy publishes articles.
      * Shallow non-article links → plausible section indexes to go look at.
    """
    links = _internal_links(home, root)
    root_key = normalise_url(root)

    section_hits: Counter[str] = Counter()
    shallow: dict[str, tuple[str, str]] = {}

    for url, anchor in links:
        segs = path_segments(url)
        if not segs:
            continue
        if segs[0].lower() in UTILITY_SEGMENTS:
            continue

        if is_article_url(url):
            parent = derive_source_url(url)
            if normalise_url(parent) != root_key:
                section_hits[parent] += 1
        elif len(segs) <= 2:
            shallow.setdefault(normalise_url(url), (url, anchor))

    # Sections proven by the articles hanging off them, most articles first.
    inferred = [u for u, _ in section_hits.most_common()]

    # Then everything shallow enough to be an index, best hints first.
    ranked_shallow = [
        url for url, _ in sorted(
            shallow.values(),
            key=lambda pair: _hint_score(pair[0], pair[1]),
            reverse=True,
        )
    ]

    logger.info("feed_finder: %d inferred section(s), %d shallow link(s)",
                len(inferred), len(ranked_shallow))
    if inferred:
        logger.info("feed_finder: top sections %s",
                    [f"{u} ({section_hits[u]} articles)" for u in inferred[:3]])

    # The page the user gave us, and the homepage, are candidates like any other.
    ordered = [given, root] + inferred + ranked_shallow
    return _dedupe(ordered)


# ── Step 3: prove every candidate ─────────────────────────────────────────────

async def _score_feed(url: str) -> Candidate | None:
    items = await fetch_rss_items(url)
    if len(items) >= MIN_FEED_ITEMS:
        return Candidate(url=url, kind="feed", item_count=len(items))
    return None


def _score_crawled(page: dict, url: str) -> Candidate | None:
    """Does this already-crawled page expose enough articles to be worth polling?

    Same-domain links first (the normal publication case). A page that fails
    that bar but is rich in OFF-domain headlines is an outbound aggregator
    (futuretools.io/news links out to TechCrunch etc.) — a perfectly good
    source that the internal-only filter used to reject outright.
    """
    if not page["success"]:
        return None
    links = article_links_on_page(page, url)
    if len(links) >= MIN_ARTICLE_LINKS:
        return Candidate(url=url, kind="page", item_count=len(links),
                         title=page.get("title") or "")
    ext_links = article_links_on_page(page, url, external=True)
    if len(ext_links) >= MIN_ARTICLE_LINKS:
        return Candidate(url=url, kind="page", item_count=len(ext_links),
                         title=page.get("title") or "", scope="external")
    return None


def _looks_like_anti_bot(error) -> bool:
    err = str(error or "").lower()
    return any(m in err for m in _ANTI_BOT)


async def _verify_pages(candidates: list[str], home: dict | None,
                        root: str) -> list[Candidate]:
    """
    Prove candidates one at a time. Discovery aims every request at a single
    host, so parallelism here buys a few seconds and costs you the whole site:
    two concurrent crawls are enough to trip Cloudflare on openai.com. We go
    sequentially, pause between requests, and stop if the host starts refusing.
    """
    verified: list[Candidate] = []
    consecutive_blocks = 0
    root_key = normalise_url(root)

    for i, url in enumerate(candidates):
        # We already crawled the homepage to read its structure. Score it from
        # that, rather than paying for it twice (and risking a challenge).
        if home is not None and normalise_url(url) == root_key:
            if c := _score_crawled(home, url):
                verified.append(c)
            continue

        if i:
            await asyncio.sleep(POLITE_DELAY_SECONDS)

        page = await fetch_page(url)
        if not page["success"] and _looks_like_anti_bot(page.get("error")):
            logger.info("feed_finder: %s challenged us — backing off, one retry", url)
            await asyncio.sleep(3)
            page = await fetch_page(url)

        if not page["success"]:
            if _looks_like_anti_bot(page.get("error")):
                consecutive_blocks += 1
                if consecutive_blocks >= 2:
                    logger.warning("feed_finder: %s is refusing crawlers — stopping "
                                   "verification with %d page(s) confirmed",
                                   root, len(verified))
                    break
            continue

        consecutive_blocks = 0
        if c := _score_crawled(page, url):
            verified.append(c)

    return verified


async def _verify_feeds(urls: list[str]) -> list[Candidate]:
    feeds = [c for c in await asyncio.gather(*(_score_feed(u) for u in urls)) if c]
    feeds.sort(key=lambda c: c.item_count, reverse=True)
    return feeds


async def find_news_pages(site_url: str, max_candidates: int = 5) -> list[Candidate]:
    """
    Find the page(s) on this site that list its articles.
    Returns candidates ranked best-first; empty means the site has no news page.
    """
    if "://" not in site_url:
        site_url = f"https://{site_url}"
    root = _root_of(site_url)
    logger.info("feed_finder: discovering news pages for %s", site_url)

    # ── 1. Ask the site for its feed, over plain HTTP (fast path) ────────
    #
    # If the root is behind a WAF, do NOT re-probe it with more requests —
    # a burst trips the bot challenge and locks the crawler out afterwards.
    # BUT: common feed paths (/rss.xml, /feed) often have different access
    # rules than the root. telegraph.co.uk returns 402 on its root but serves
    # 120 items on /rss.xml via plain httpx. So:
    #   * Root 200 → autodiscover from its HTML + probe feed paths
    #   * Root non-200 → skip HTML autodiscovery, but STILL probe feed paths
    #     (they may respond even when the root doesn't)
    status, html = await _get(root)
    http_usable = status == 200

    tried: list[str] = []
    # Always probe common feed paths — they can be accessible even when root isn't
    feed_path_urls = [urljoin(root, p) for p in COMMON_FEED_PATHS]
    if http_usable:
        autodiscovered = await _autodiscover_feeds(root, html)
        tried = _dedupe(autodiscovered + feed_path_urls)
    else:
        logger.info("feed_finder: %s root returned %s — skipping HTML autodiscovery, "
                    "probing feed paths directly", root, status or "no response")
        tried = _dedupe(feed_path_urls)

    feeds = await _verify_feeds(tried)
    if feeds:
        logger.info("feed_finder: %d verified feed(s) via HTTP — done", len(feeds))
        return feeds[:max_candidates]

    # ── 2. Crawl the homepage. We need its links for structure anyway, and
    #      its HTML lets us autodiscover on hosts that 403 a plain client. ──
    home = await fetch_page(root)
    if not home["success"]:
        logger.warning("feed_finder: homepage crawl failed for %s: %s",
                       root, home.get("error"))
        home = None
        candidates = _dedupe([site_url, root])
    else:
        crawled_feeds = [u for u in await _autodiscover_feeds(root, home.get("html", ""))
                         if normalise_url(u) not in {normalise_url(t) for t in tried}]
        if crawled_feeds:
            logger.info("feed_finder: %d feed(s) declared in crawled HTML",
                        len(crawled_feeds))
            feeds = await _verify_feeds(crawled_feeds)
            if feeds:
                logger.info("feed_finder: %d verified feed(s) via crawler — done",
                            len(feeds))
                return feeds[:max_candidates]
        candidates = _structural_candidates(home, root, site_url)

    # ── 3. Prove them ────────────────────────────────────────────────────
    to_verify = candidates[:MAX_VERIFY]
    logger.info("feed_finder: verifying %d candidate page(s)", len(to_verify))
    pages = await _verify_pages(to_verify, home, root)

    pages.sort(key=lambda c: (c.item_count, -len(path_segments(c.url))), reverse=True)

    # The page the user explicitly gave beats any score, as long as it proved
    # itself. They said "poll THIS page" — honour it. (Stable sort: everything
    # else keeps its score order behind it.)
    given_key = normalise_url(site_url)
    pages.sort(key=lambda c: normalise_url(c.url) != given_key)

    logger.info("feed_finder: %d verified page(s)", len(pages))
    return pages[:max_candidates]


def _dedupe(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        key = normalise_url(u)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out
