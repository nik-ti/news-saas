"""F10 — qualification must survive null scores from the LLM."""
import research.qualification as qual


def _homepage(url):
    return {"url": url, "title": "Site", "content": "words " * 100,
            "html": "", "links": [], "success": True, "error": None}


async def test_null_scores_do_not_crash_qualification(monkeypatch):
    candidates = ["https://a.com", "https://b.com"]

    async def fake_fetch_multiple(urls):
        return [_homepage(u) for u in urls]

    async def fake_chat_json(system, user, smart=False):
        if not smart:   # Stage 1 batch prefilter — emits null scores
            return {"results": [
                {"id": 1, "score": None, "verdict": "investigate"},
                {"id": 2, "score": None, "verdict": "investigate"},
            ]}
        # Stage 2 deep qualify — null match_score
        return {"match_score": None, "recommendation": "accept",
                "source_name": "X", "feed_url": ""}

    monkeypatch.setattr(qual, "fetch_multiple", fake_fetch_multiple)
    monkeypatch.setattr(qual, "chat_json", fake_chat_json)

    # Old code: TypeError ('<' between NoneType and int) killed the whole run.
    result = await qual.qualify_all(candidates, {"strictness": "medium"})
    assert result == []   # null score coalesces to 0 → below threshold


async def test_valid_scores_still_qualify(monkeypatch):
    candidates = ["https://a.com"]

    async def fake_fetch_multiple(urls):
        return [_homepage(u) for u in urls]

    async def fake_chat_json(system, user, smart=False):
        if not smart:
            return {"results": [{"id": 1, "score": 90, "verdict": "investigate"}]}
        return {"match_score": 85, "recommendation": "accept",
                "source_name": "A", "feed_url": "https://a.com/news"}

    monkeypatch.setattr(qual, "fetch_multiple", fake_fetch_multiple)
    monkeypatch.setattr(qual, "chat_json", fake_chat_json)

    result = await qual.qualify_all(candidates, {})
    assert len(result) == 1
    assert result[0]["url"] == "https://a.com"
    assert result[0]["feed_url"] == "https://a.com/news"


async def test_malformed_result_entries_skipped(monkeypatch):
    candidates = ["https://a.com"]

    async def fake_fetch_multiple(urls):
        return [_homepage(u) for u in urls]

    async def fake_chat_json(system, user, smart=False):
        if not smart:
            return {"results": ["garbage", None,
                                {"id": 1, "score": 50, "verdict": "skip"}]}
        raise AssertionError("nothing should reach stage 2")

    monkeypatch.setattr(qual, "fetch_multiple", fake_fetch_multiple)
    monkeypatch.setattr(qual, "chat_json", fake_chat_json)

    assert await qual.qualify_all(candidates, {}) == []
