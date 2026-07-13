"""
Shared URL heuristics for the research engine.

Search results are often individual article pages (e.g. /blog/some-post-title),
but a *source* must be a site or section that keeps publishing. These helpers
deterministically collapse article URLs to their section root and detect
article-like URLs so they never end up stored as sources or feed_urls.
"""
import calendar
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

# Path segments that indicate an article LIST page (good feed candidates)
SECTION_SEGMENTS = {
    "news", "blog", "articles", "posts", "stories", "latest", "updates",
    "changelog", "insights", "research", "analysis", "press", "feed",
    "publications", "briefs", "reports", "newsroom", "media",
}

# Last segments that always mean "this is a listing", never a single article.
# /research/index/ and /blog/archive are indexes, not stories.
INDEX_SEGMENTS = {"index", "archive", "archives", "all", "page", "home"}

_DATE_PATTERN = re.compile(r"/(19|20)\d{2}([/-]\d{1,2})?([/-]\d{1,2})?(/|$)")
_NUMERIC_ID_PATTERN = re.compile(r"/\d{4,}(/|$)")

# A full Y-M-D datestamp may sit right against a slug (/2026-07-10-story) —
# three date parts in a row are unambiguous. A bare year or year-month is NOT:
# "/2008-financial-crisis-lessons" is a topic, not a dateline, so partial dates
# must end cleanly at a path boundary.
_FULL_DATE_CAPTURE = re.compile(
    r"/((?:19|20)\d{2})[/-](\d{1,2})[/-](\d{1,2})(?=[/-]|$)")
_PARTIAL_DATE_CAPTURE = re.compile(
    r"/((?:19|20)\d{2})(?:[/-](\d{1,2}))?(?=/|$)")


def date_from_url(url: str) -> datetime | None:
    """
    Publication date implied by a URL's path (/2024/03/15/story, /news/2023-05-02-x).
    Partial dates resolve to their LATEST possible moment (end of month / Dec 31)
    so only clearly-old articles are ever judged stale from a URL alone.
    Returns None when the path carries no date — absence of a date proves nothing.
    """
    path = urlparse(url).path
    try:
        m = _FULL_DATE_CAPTURE.search(path)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        m = _PARTIAL_DATE_CAPTURE.search(path)
        if not m:
            return None
        year, month = int(m.group(1)), m.group(2)
        if month:
            last = calendar.monthrange(year, int(month))[1]
            return datetime(year, int(month), last, 23, 59, tzinfo=timezone.utc)
        return datetime(year, 12, 31, 23, 59, tzinfo=timezone.utc)
    except ValueError:
        return None  # "/2024/99/" — a number that isn't really a date


def registered_domain(url: str) -> str:
    """coindesk.com from https://www.coindesk.com/path"""
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def path_segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


def is_article_url(url: str) -> bool:
    """
    Heuristic: does this URL point to a single article rather than a site/section?
    True for e.g. /blog/openclaw-rough-week, /2026/07/01/some-story, /news/12345.
    False for roots and section pages like /blog, /news, /crypto/news.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return False

    segments = path_segments(url)
    last = segments[-1].lower()

    # Date-based paths are almost always articles
    if _DATE_PATTERN.search(path) or _NUMERIC_ID_PATTERN.search(path):
        return True

    # A known section name as the last segment → list page, not an article
    if last in SECTION_SEGMENTS:
        return False

    # An explicit index/archive segment → list page (e.g. /research/index/)
    if last in INDEX_SEGMENTS:
        return False

    # File extensions → article/document
    if re.search(r"\.(html?|php|aspx?|pdf)$", last):
        return True

    # Anything nested directly under a section (/blog/<slug>, /news/<slug>)
    # is an individual article
    if len(segments) >= 2 and segments[-2].lower() in SECTION_SEGMENTS:
        return True

    # Hyphenated slug (words-glued-together) → article title slug
    if last.count("-") >= 3 or (last.count("-") >= 1 and len(last) > 25):
        return True

    # Deep paths (3+ segments) that don't end in a section name → likely article
    if len(segments) >= 3:
        return True

    return False


def derive_source_url(url: str) -> str:
    """
    Collapse an article URL to the most plausible source URL.
    - Article page → its parent section if that looks like a list page,
      otherwise the domain root.
    - Section pages and roots pass through unchanged (normalised).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    root = f"{scheme}://{parsed.netloc}"

    if not is_article_url(url):
        path = parsed.path.rstrip("/")
        return f"{root}{path}" if path else root

    segments = path_segments(url)
    # Walk up the path until we hit a non-article-looking prefix
    for depth in range(len(segments) - 1, 0, -1):
        candidate = f"{root}/" + "/".join(segments[:depth])
        if not is_article_url(candidate):
            # Only keep the parent if it's a recognisable section; otherwise root
            if segments[depth - 1].lower() in SECTION_SEGMENTS or depth == 1:
                return candidate
    return root


def normalise_url(url: str) -> str:
    """Normalise a URL for deduplication (scheme+host+path, no query/fragment)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}".lower()
