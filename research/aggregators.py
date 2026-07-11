"""
Aggregator sources.

Some of the best topic coverage isn't a single site — it's a query across every
site. Google News exposes exactly that as an RSS feed: one URL per topic that
returns the latest matching headlines from hundreds of publications.

Two things make it a special kind of source:
  * The item links are Google redirect URLs that resolve to an interstitial, not
    the article — so we can't crawl the body. We treat these as HEADLINE items:
    the title (which Google formats as "Headline - Publication") is the content,
    the relevance gate judges the headline, and the post links out to Google's
    redirect (which a real browser follows to the article).
  * The feed itself is always on-topic by construction, so it skips source
    qualification. Per-article relevance still applies.
"""
from urllib.parse import quote_plus, urlparse

GOOGLE_NEWS_HOST = "news.google.com"


def google_news_feed_url(query: str, lang: str = "en", country: str = "US") -> str:
    """Build a Google News RSS search feed for a topic query."""
    q = quote_plus(query.strip())
    ceid = f"{country}:{lang}"
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={lang}-{country}&gl={country}&ceid={ceid}")


def is_google_news_url(url: str) -> bool:
    """True for Google News redirect/article links, which must not be crawled."""
    try:
        return urlparse(url).netloc.lower().endswith(GOOGLE_NEWS_HOST)
    except Exception:
        return False


def news_query_for(profile: dict) -> str:
    """
    The single best Google News query for a stream's profile.

    Google News search is plain keywords, so we keep it short and concrete.
    Always anchor on the broad domain so "market-moving news" (a topic
    description) doesn't become a standalone query that returns cattle futures.
    """
    domain = (profile.get("broad_domain") or "").strip()
    topics = profile.get("specific_topics") or []
    keywords = profile.get("keywords") or []

    # Domain + first topic is specific without being too long
    if domain and topics:
        return f"{domain} {topics[0]}"
    if domain:
        return domain
    # keywords are often already search-ready ("crypto market news")
    if keywords:
        return keywords[0]
    for key in ("description", "topic"):
        val = profile.get(key)
        if val:
            return str(val)[:80]
    return "top news"
