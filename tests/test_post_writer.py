"""F8 — source_url escaping in the post's anchor tag."""
import pipeline.post_writer as pw


async def test_source_url_is_escaped(monkeypatch):
    async def fake_llm(system, user, model="post"):
        return "<b>Headline</b>\n\nBody text."
    monkeypatch.setattr(pw, "chat", fake_llm)

    hostile = 'https://x.com/a"b<c>&d'
    post = await pw.write_post("summary", title="T", source_url=hostile)

    assert '<a href="https://x.com/a&quot;b&lt;c&gt;&amp;d">Source</a>' in post
    # The raw quote must not appear inside the href attribute.
    assert f'href="{hostile}"' not in post


async def test_clean_url_passes_through(monkeypatch):
    async def fake_llm(system, user, model="post"):
        return "<b>Headline</b>\n\nBody."
    monkeypatch.setattr(pw, "chat", fake_llm)

    post = await pw.write_post("summary", source_url="https://x.com/story")
    assert '<a href="https://x.com/story">Source</a>' in post
