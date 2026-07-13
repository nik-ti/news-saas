"""F10 — qualification must survive null scores from the LLM.
C6 — Stage-1 chunks run in parallel; one failed chunk loses only its own candidates."""
import research.qualification as qual


def _homepage(url):
    return {"url": url, "title": "Site", "content": "words " * 100,
            "html": "", "links": [], "success": True, "error": None}


async def test_null_scores_do_not_crash_qualification(monkeypatch):
    candidates = ["https://a.com", "https://b.com"]

    async def fake_fetch_multiple(urls):
        return [_homepage(u) for u in urls]

    async def fake_chat_json(system, user, model="fast"):
        if model == "fast":   # Stage 1 batch prefilter — emits null scores
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

    async def fake_chat_json(system, user, model="fast"):
        if model == "fast":
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

    async def fake_chat_json(system, user, model="fast"):
        if model == "fast":
            return {"results": ["garbage", None,
                                {"id": 1, "score": 50, "verdict": "skip"}]}
        raise AssertionError("nothing should reach stage 2")

    monkeypatch.setattr(qual, "fetch_multiple", fake_fetch_multiple)
    monkeypatch.setattr(qual, "chat_json", fake_chat_json)

    assert await qual.qualify_all(candidates, {}) == []


async def test_failed_prefilter_chunk_loses_only_its_candidates(monkeypatch):
    # 16 candidates → two chunks (size 15). The first chunk's LLM call blows up;
    # the second chunk's survivor must still be qualified.
    candidates = [f"https://s{i}.com" for i in range(16)]

    async def fake_fetch_multiple(urls):
        return [_homepage(u) for u in urls]

    async def fake_chat_json(system, user, model="fast"):
        if model == "fast":
            if "https://s0.com" in user:          # first chunk (ids 1-15)
                raise RuntimeError("LLM outage for this chunk")
            return {"results": [{"id": 16, "score": 88, "verdict": "investigate"}]}
        return {"match_score": 80, "recommendation": "accept",
                "source_name": "S16", "feed_url": ""}

    monkeypatch.setattr(qual, "fetch_multiple", fake_fetch_multiple)
    monkeypatch.setattr(qual, "chat_json", fake_chat_json)

    result = await qual.qualify_all(candidates, {})
    assert len(result) == 1
    assert result[0]["url"] == "https://s15.com"   # id 16 = 16th candidate


# ── §2.5: internal-DB matches skip the Stage-1 prefilter ─────────────────────

async def test_priority_urls_bypass_prefilter(monkeypatch):
    import json

    async def fake_fetch_multiple(urls):
        return [{"url": u, "title": "T", "content": "some content", "html": "",
                 "links": [], "success": True, "error": None} for u in urls]

    prefilter_calls = []

    async def fake_chat_json(system, user, model="fast"):
        if "fast source qualification" in system:
            prefilter_calls.append(user)
            return {"results": []}          # prefilter rejects EVERYTHING
        # deep qualification result
        return {"covers_topic": True, "match_score": 88,
                "recommendation": "accept", "source_name": "Cached",
                "feed_url": "https://cached.com/news", "frequency": "daily"}

    monkeypatch.setattr(qual, "fetch_multiple", fake_fetch_multiple)
    monkeypatch.setattr(qual, "chat_json", fake_chat_json)

    out = await qual.qualify_all(
        ["https://cached.com", "https://random.com"], {"strictness": "medium"},
        priority_urls={"https://cached.com"})

    # The prefilter rejected everything, but the cached source still made it
    # through deep qualification.
    assert [q["url"] for q in out] == ["https://cached.com"]
