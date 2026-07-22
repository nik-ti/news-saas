"""Truncated-post guard (Part 0): the llm.py finish_reason retry plus the
post_writer mid-sentence check. A cut completion must never reach Telegram —
it goes down the same retry path as an empty completion."""
import pytest

import pipeline.post_writer as pw
import research.llm as llm_mod


class _Resp:
    """Minimal stand-in for a LangChain AIMessage."""
    def __init__(self, content, finish_reason):
        self.content = content
        self.response_metadata = {"finish_reason": finish_reason}


def _patch_llm(monkeypatch, responses):
    """Fake the tier cache: ainvoke returns the queued responses in order."""
    calls = {"n": 0}

    class FakeLLM:
        async def ainvoke(self, messages):
            resp = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return resp

    monkeypatch.setattr(llm_mod, "_get_llm", lambda kind: FakeLLM())
    return calls


# ── research.llm.chat: finish_reason retry ────────────────────────────────────

async def test_chat_retries_truncated_completion(monkeypatch):
    calls = _patch_llm(monkeypatch, [
        _Resp("The NIGHT token, associated with Midnight, saw", "length"),
        _Resp("The full post, complete.", "stop"),
    ])
    text = await llm_mod.chat("sys", "user", model="post")
    assert text == "The full post, complete."
    assert calls["n"] == 2                             # retried exactly once


async def test_chat_raises_when_truncated_twice(monkeypatch):
    calls = _patch_llm(monkeypatch, [
        _Resp("partial one", "length"),
        _Resp("partial two", "length"),
    ])
    with pytest.raises(RuntimeError, match="truncated"):
        await llm_mod.chat("sys", "user", model="post")
    assert calls["n"] == 2                             # tried, retried, gave up


async def test_chat_accepts_normal_completion(monkeypatch):
    calls = _patch_llm(monkeypatch, [_Resp("Fine post.", "stop")])
    assert await llm_mod.chat("sys", "user", model="post") == "Fine post."
    assert calls["n"] == 1                             # no wasted retry


def test_finish_reason_shapes():
    # OpenAI-compatible: a plain string…
    assert llm_mod._finish_reason(_Resp("x", "length")) == "length"
    assert llm_mod._finish_reason(_Resp("x", "MAX_TOKENS")) == "max_tokens"
    assert llm_mod._finish_reason(_Resp("x", None)) == ""
    # …but some providers nest a dict.
    r = _Resp("x", None)
    r.response_metadata = {"finish_reason": {"finish_reason": "length"}}
    assert llm_mod._finish_reason(r) == "length"
    r.response_metadata = {"finish_reason": {"reason": "max_tokens"}}
    assert llm_mod._finish_reason(r) == "max_tokens"
    # No metadata at all → not truncated.
    r.response_metadata = {}
    assert llm_mod._finish_reason(r) == ""


# ── pipeline.post_writer: belt-and-braces completeness check ─────────────────

async def test_write_post_returns_empty_when_chat_raises(monkeypatch):
    # chat() raising (double truncation, provider error) → "" → the delivery
    # stays queued for the next send tick instead of posting partial text.
    async def boom(system, user, model="post"):
        raise RuntimeError("LLM 'post' completion truncated twice")
    monkeypatch.setattr(pw, "chat", boom)
    assert await pw.write_post("summary", title="T") == ""


async def test_write_post_rejects_mid_sentence_body(monkeypatch):
    async def cut(system, user, model="post"):
        return "<b>NIGHT token recovers</b>\n\nThe NIGHT token, associated with Midnight, saw"
    monkeypatch.setattr(pw, "chat", cut)
    assert await pw.write_post("summary", title="T") == ""


async def test_write_post_accepts_sentence_endings(monkeypatch):
    for ending in ("text.", "text!", "text?", 'said "go."', "text (details).",
                   "<b>42%</b>"):
        async def ok(system, user, model="post", _e=ending):
            return f"<b>Headline</b>\n\nBody {_e}"
        monkeypatch.setattr(pw, "chat", ok)
        post = await pw.write_post("summary", title="T")
        assert post != "", f"ending {ending!r} must be accepted"


async def test_write_post_accepts_html_tag_ending_and_appends_source(monkeypatch):
    async def ok(system, user, model="post"):
        return "<b>Headline</b>\n\nGrowth hit <b>42%</b>"
    monkeypatch.setattr(pw, "chat", ok)
    post = await pw.write_post("summary", source_url="https://x.com/s")
    assert post.endswith('<a href="https://x.com/s">Source</a>')
    assert "Growth hit <b>42%</b>" in post
