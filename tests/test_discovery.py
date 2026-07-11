"""F5 (loud Brave failure) and F10 (null keywords fallback)."""
import pytest

import research.discovery as disc


async def test_all_queries_failing_raises(monkeypatch):
    async def broken(query, count=None):
        return None                      # every request errored (bad key/quota)
    monkeypatch.setattr(disc, "_brave_search", broken)

    with pytest.raises(RuntimeError, match="Brave Search failed"):
        await disc.search_parallel(["q1", "q2"])


async def test_partial_failure_merges_survivors(monkeypatch):
    async def flaky(query, count=None):
        if query == "bad":
            return None
        return [{"title": "T", "url": "https://site.com/news", "description": ""}]
    monkeypatch.setattr(disc, "_brave_search", flaky)

    candidates = await disc.search_parallel(["bad", "good"])
    assert len(candidates) == 1
    assert "site.com" in candidates[0]


async def test_genuinely_empty_results_do_not_raise(monkeypatch):
    async def empty(query, count=None):
        return []                        # API worked, nothing matched
    monkeypatch.setattr(disc, "_brave_search", empty)

    assert await disc.search_parallel(["q"]) == []


async def test_query_fallback_survives_null_keywords(monkeypatch):
    # The LLM can emit {"queries": null}; the profile can carry
    # {"keywords": null}. Neither may crash query generation.
    async def null_llm(system, user):
        return {"queries": None}
    monkeypatch.setattr(disc, "chat_json", null_llm)

    queries = await disc.generate_search_queries(
        {"keywords": None, "broad_domain": None})
    assert queries == ["news"]

    queries = await disc.generate_search_queries(
        {"keywords": ["crypto reg"], "broad_domain": "crypto"})
    assert queries == ["crypto reg"]
