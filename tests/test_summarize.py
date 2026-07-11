"""C1 — RSS teasers no longer masquerade as summaries; graceful degradation."""
import pipeline.summarize as summ
from pipeline.summarize import SKIP, summarize_article


def _article(summary="", url="https://ex.com/story", feed_url="https://ex.com/feed"):
    return {"title": "A headline long enough", "url": url,
            "summary": summary, "source_feed_url": feed_url}


def _page(content="Article body. " * 50, success=True):
    return {"success": success, "content": content if success else "",
            "title": "Page Title", "html": "", "links": [], "error": None}


async def test_thin_rss_teaser_triggers_real_summarization(monkeypatch):
    fetches = []

    async def fake_fetch(url):
        fetches.append(url)
        return _page()

    async def fake_llm(system, user):
        return {"summary": "A real, full summary of the article body."}

    monkeypatch.setattr(summ, "fetch_page", fake_fetch)
    monkeypatch.setattr(summ, "chat_json", fake_llm)

    summary, _ = await summarize_article(_article(summary="Read more…"))
    assert fetches == ["https://ex.com/story"]          # page WAS fetched
    assert summary == "A real, full summary of the article body."


async def test_inline_item_uses_stored_summary_without_fetch(monkeypatch):
    async def no_fetch(url):
        raise AssertionError("inline items must never be re-fetched")
    monkeypatch.setattr(summ, "fetch_page", no_fetch)

    # url == source_feed_url marks an inline-extracted (changelog) item.
    art = _article(summary="Changelog entry text.",
                   url="https://ex.com/feed", feed_url="https://ex.com/feed")
    summary, _ = await summarize_article(art)
    assert summary == "Changelog entry text."


async def test_substantial_stored_summary_trusted_without_fetch(monkeypatch):
    async def no_fetch(url):
        raise AssertionError("a substantial summary must not trigger a fetch")
    monkeypatch.setattr(summ, "fetch_page", no_fetch)

    big = "Detailed persisted summary. " * 20          # >= 300 chars (retry path)
    summary, _ = await summarize_article(_article(summary=big))
    assert summary.startswith("Detailed persisted summary.")


async def test_thin_teaser_degrades_when_fetch_fails(monkeypatch):
    async def dead_fetch(url):
        return _page(success=False)
    monkeypatch.setattr(summ, "fetch_page", dead_fetch)

    summary, _ = await summarize_article(_article(summary="Teaser only."))
    assert summary == "Teaser only."                    # degraded, NOT dropped


async def test_thin_teaser_degrades_when_page_is_paywall(monkeypatch):
    async def fake_fetch(url):
        return _page()

    async def paywall_llm(system, user):
        return {"summary": "SKIP"}

    monkeypatch.setattr(summ, "fetch_page", fake_fetch)
    monkeypatch.setattr(summ, "chat_json", paywall_llm)

    summary, _ = await summarize_article(_article(summary="Teaser only."))
    assert summary == "Teaser only."


async def test_no_summary_and_dead_page_skips(monkeypatch):
    async def dead_fetch(url):
        return _page(success=False)
    monkeypatch.setattr(summ, "fetch_page", dead_fetch)

    summary, _ = await summarize_article(_article(summary=""))
    assert summary == SKIP
