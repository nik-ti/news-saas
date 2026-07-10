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

    Prefer the sharpest specific topic; fall back to the domain. Google News
    search is plain keywords, so we keep it short and concrete.
    """
    topics = profile.get("specific_topics") or []
    if topics:
        return str(topics[0])
    for key in ("broad_domain", "description", "topic"):
        val = profile.get(key)
        if val:
            return str(val)
    return "top news"
