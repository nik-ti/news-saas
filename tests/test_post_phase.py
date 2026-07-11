"""C2 (summary persistence) and C3 (distinct terminal statuses) — _post_phase
integration against a temp DB with the LLM/send layers stubbed."""
import config
import pipeline.news_cycle as nc
from database import store
from database.models import get_connection
from pipeline.summarize import SKIP


def _queued_article(summary=""):
    """Stream + source + one queued article; returns article_id."""
    sid = store.create_stream(user_id=42, name="s", criteria={"topic": "x"})
    src = store.add_source(stream_id=sid, url="https://a.com")
    aid = store.add_article(source_id=src, title="Headline", url="https://a.com/1",
                            summary=summary, content_hash="H1")
    return aid


def _status(article_id):
    conn = get_connection()
    row = conn.execute("SELECT status, summary, attempts FROM articles WHERE id = ?",
                       (article_id,)).fetchone()
    conn.close()
    return dict(row)


def _stub_pipeline(monkeypatch, *, summary="A fine summary.", relevant=True,
                   post="<b>Post</b> long enough to pass the length check.",
                   send_result=None):
    async def fake_summarize(article):
        return summary, article.get("title") or ""

    async def fake_gate(title, summ, profile):
        return relevant, "stub"

    async def fake_write(summ, title="", source_url="", length="standard"):
        return post

    async def fake_send(chat_id, html):
        return send_result or {"ok": True}

    monkeypatch.setattr(nc, "summarize_article", fake_summarize)
    monkeypatch.setattr(nc, "check_relevance", fake_gate)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "send_html_message_async", fake_send)


async def test_posted_article_persists_summary(temp_db, monkeypatch):
    aid = _queued_article()
    _stub_pipeline(monkeypatch, summary="Computed summary text.")

    stats = await nc._post_phase()
    row = _status(aid)
    assert stats["posted"] == 1
    assert row["status"] == "posted"
    assert row["summary"] == "Computed summary text."   # C2: persisted


async def test_unusable_page_gets_unusable_status(temp_db, monkeypatch):
    aid = _queued_article()
    _stub_pipeline(monkeypatch, summary=SKIP)

    stats = await nc._post_phase()
    assert stats["dropped"] == 1
    assert _status(aid)["status"] == "unusable"          # C3: not 'seen'


async def test_irrelevant_article_status(temp_db, monkeypatch):
    aid = _queued_article()
    _stub_pipeline(monkeypatch, relevant=False)

    stats = await nc._post_phase()
    assert stats["irrelevant"] == 1
    assert _status(aid)["status"] == "irrelevant"


async def test_terminal_send_error_marks_send_failed(temp_db, monkeypatch):
    aid = _queued_article()
    _stub_pipeline(monkeypatch,
                   send_result={"ok": False, "error_code": 403,
                                "description": "bot was blocked"})

    stats = await nc._post_phase()
    assert stats["dropped"] == 1
    assert _status(aid)["status"] == "send_failed"       # C3: not 'seen'


async def test_retry_budget_exhaustion_marks_dropped(temp_db, monkeypatch):
    aid = _queued_article()
    _stub_pipeline(monkeypatch, post="")                 # writer returns nothing

    for _ in range(config.MAX_ARTICLE_ATTEMPTS):
        await nc._post_phase()

    row = _status(aid)
    assert row["status"] == "dropped"                    # C3: not 'seen'
    assert row["attempts"] == config.MAX_ARTICLE_ATTEMPTS


async def test_retry_uses_persisted_summary_without_refetch(temp_db, monkeypatch):
    # Cycle 1: real summarize path persists the summary but the send 5xxes.
    # Cycle 2: summarize_article (the REAL one) must trust the stored summary —
    # no page fetch — and the send succeeds.
    aid = _queued_article()
    fetches = []

    async def fake_fetch(url):
        fetches.append(url)
        return {"success": True, "content": "Article body. " * 60,
                "title": "T", "html": "", "links": [], "error": None}

    async def fake_llm(system, user):
        return {"summary": "Computed once, reused on retry. " * 12}  # > 300 chars

    async def fake_gate(title, summ, profile):
        return True, "ok"

    async def fake_write(summ, title="", source_url="", length="standard"):
        return "<b>Post</b> long enough to pass the length check."

    sends = {"n": 0}

    async def flaky_send(chat_id, html):
        sends["n"] += 1
        if sends["n"] == 1:
            return {"ok": False, "error_code": 500, "description": "gateway"}
        return {"ok": True}

    import pipeline.summarize as summ_mod
    monkeypatch.setattr(summ_mod, "fetch_page", fake_fetch)
    monkeypatch.setattr(summ_mod, "chat_json", fake_llm)
    monkeypatch.setattr(nc, "check_relevance", fake_gate)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "send_html_message_async", flaky_send)

    stats1 = await nc._post_phase()
    assert stats1["retry"] == 1
    assert len(fetches) == 1                             # crawled once

    stats2 = await nc._post_phase()
    assert stats2["posted"] == 1
    assert len(fetches) == 1                             # NOT crawled again (C1+C2)
    assert _status(aid)["status"] == "posted"
