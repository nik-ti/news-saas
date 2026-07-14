"""Interface language + per-stream post language (EN default / RU)."""
import re
from types import SimpleNamespace

import config
import bot.handlers as handlers
import bot.i18n as i18n
import pipeline.news_cycle as nc
import pipeline.post_writer as pw
import research.profile_builder as pb
from bot.i18n import t
from database import store


# ── i18n mechanical guarantees ────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def test_every_key_exists_in_both_languages():
    for key, entry in i18n.STRINGS.items():
        assert "en" in entry and entry["en"], f"{key} missing English"
        assert "ru" in entry and entry["ru"], f"{key} missing Russian"


def test_placeholders_match_across_languages():
    # A translation with a missing/renamed {placeholder} breaks at runtime —
    # catch it forever, mechanically.
    for key, entry in i18n.STRINGS.items():
        en_ph = set(_PLACEHOLDER_RE.findall(entry["en"]))
        ru_ph = set(_PLACEHOLDER_RE.findall(entry["ru"]))
        assert en_ph == ru_ph, f"{key}: en={en_ph} ru={ru_ph}"


def test_t_falls_back_and_formats():
    assert t("ru", "limit_streams", max=5) != t("en", "limit_streams", max=5)
    assert "5" in t("ru", "limit_streams", max=5)
    assert t("de", "cancelled") == t("en", "cancelled")   # unknown lang → en
    assert t("en", "no_such_key_ever") == "no_such_key_ever"


# ── storage ───────────────────────────────────────────────────────────────────

def test_ui_lang_default_and_roundtrip(temp_db):
    assert store.get_ui_lang(42) == "en"          # never seen → default
    store.set_ui_lang(42, "ru")
    assert store.get_ui_lang(42) == "ru"
    store.set_ui_lang(42, "en")                   # upsert, not duplicate insert
    assert store.get_ui_lang(42) == "en"


# ── /language command + callbacks ─────────────────────────────────────────────

def _update(user_id, chat_id=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id or user_id),
    )


def _query(user_id, data):
    edits = []

    async def edit_message_text(text, **kw):
        edits.append(text)

    async def answer(*a, **kw):
        pass

    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id),
                        message=SimpleNamespace(chat_id=user_id),
                        edit_message_text=edit_message_text, answer=answer)
    return SimpleNamespace(callback_query=q), edits


async def test_ulang_callback_sets_interface_language(temp_db, monkeypatch):
    update, edits = _query(7, "ulang:ru")
    await handlers.handle_callback(update, SimpleNamespace(user_data={}))
    assert store.get_ui_lang(7) == "ru"
    # The menu re-renders in the newly chosen language.
    assert edits and edits[0] == t("ru", "menu_main")

    update, edits = _query(7, "ulang:en")
    await handlers.handle_callback(update, SimpleNamespace(user_data={}))
    assert store.get_ui_lang(7) == "en"
    assert edits and edits[0] == t("en", "menu_main")


async def test_slang_callback_sets_stream_post_language(temp_db, monkeypatch):
    sid = store.create_stream(user_id=7, name="s", criteria={"topic": "x"})
    update, edits = _query(7, f"slang:{sid}:ru")
    await handlers.handle_callback(update, SimpleNamespace(user_data={}))
    criteria = store.get_stream(sid)["criteria"]
    assert criteria["post_language"] == "ru"
    assert len(edits) == 1


async def test_slang_callback_rejects_non_owner(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="theirs", criteria={"topic": "x"})
    update, edits = _query(2, f"slang:{sid}:ru")
    await handlers.handle_callback(update, SimpleNamespace(user_data={}))
    assert "post_language" not in (store.get_stream(sid)["criteria"] or {})
    assert edits == [t("en", "not_your_stream")]


async def test_cmd_language_stream_path_ownership(temp_db, monkeypatch):
    sent = []

    async def fake_send(chat_id, markdown, extra_html=""):
        sent.append(markdown)
        return {"ok": True}
    monkeypatch.setattr(handlers, "send_rich_async", fake_send)

    sid = store.create_stream(user_id=1, name="theirs", criteria={})
    ctx = SimpleNamespace(args=[str(sid)], bot=None)
    await handlers.cmd_language(_update(2), ctx)
    assert sent == [t("en", "not_your_stream")]


# ── interface language actually changes the replies ──────────────────────────

async def test_russian_user_gets_russian_interface(temp_db, monkeypatch):
    sent = []

    async def fake_send(chat_id, markdown, extra_html=""):
        sent.append(markdown)
        return {"ok": True}
    monkeypatch.setattr(handlers, "send_rich_async", fake_send)

    store.set_ui_lang(9, "ru")
    await handlers.cmd_streams(_update(9), SimpleNamespace(args=[]))
    assert sent[-1] == t("ru", "streams_none")

    await handlers.cmd_help(_update(9), SimpleNamespace(args=[]))
    assert "Команды" in sent[-1]
    assert "/language" in sent[-1]


# ── post language: writer behavior ────────────────────────────────────────────

async def test_russian_stream_gets_russian_prompt_and_source_label(monkeypatch):
    captured = {}

    async def spy_llm(system, user, model="post"):
        captured["system"] = system
        return "<b>Заголовок</b>\n\nТекст поста."
    monkeypatch.setattr(pw, "chat", spy_llm)

    post = await pw.write_post("summary", title="T",
                               source_url="https://x.com/a", language="ru")
    assert "native Russian" in captured["system"]
    assert post.endswith('>Источник</a>')

    post_en = await pw.write_post("summary", title="T",
                                  source_url="https://x.com/a", language="")
    assert post_en.endswith('>Source</a>')


def test_language_rule_normalization():
    russian = {"ru", "Russian", "русский", "RUS"}
    for v in russian:
        assert pw._language_rule(v) == pw._RUSSIAN_RULE
    for v in ("", "en", "English", "ENG"):
        assert pw._language_rule(v) == "English-language"
    # Some other inferred language keeps the generic pass-through.
    assert "German" in pw._language_rule("German")


def test_russian_preamble_stripped():
    raw = "Вот пост:\n<b>Заголовок</b>\n\nТело."
    assert pw._strip_preamble(raw).startswith("<b>Заголовок</b>")
    raw2 = "Конечно! Держите:\n<b>З</b>\n\nТ."
    assert pw._strip_preamble(raw2).startswith("<b>З</b>")


async def test_post_language_beats_inferred_language(temp_db, monkeypatch):
    # Stream profile says inferred "language": "en" but the user explicitly
    # chose Russian posts — Russian must win.
    sid = store.create_stream(user_id=1, name="s", criteria={
        "language": "en", "post_language": "ru"})
    src = store.add_source(stream_id=sid, url="https://a.com")
    store.mark_source_baselined(src)
    aid = store.add_articles_batch(src, [{
        "title": "T", "url": "https://a.com/1", "summary": "",
        "content_hash": "H"}])[0]
    store.create_delivery(aid, sid)

    langs = []

    async def fake_summarize(d):
        return "A fine summary long enough to pass everything.", "T"

    async def fake_gate(title, s, p):
        return True, "ok"

    async def fake_write(s, title="", source_url="", length="standard",
                         language=""):
        langs.append(language)
        return "<b>Post</b> long enough to pass the length check."

    async def fake_send(chat_id, html, reply_markup=None):
        return {"ok": True}

    async def not_dup(*a, **kw):
        return False

    monkeypatch.setattr(nc, "summarize_article", fake_summarize)
    monkeypatch.setattr(nc, "check_relevance", fake_gate)
    monkeypatch.setattr(nc, "write_post", fake_write)
    monkeypatch.setattr(nc, "send_html_message_async", fake_send)
    monkeypatch.setattr(nc, "_is_semantic_duplicate", not_dup)

    await nc._post_phase()
    assert langs == ["ru"]


# ── interview language ────────────────────────────────────────────────────────

async def test_interview_converses_in_russian_for_ru_ui(monkeypatch):
    captured = {}

    async def spy_llm(system, user, model="smart"):
        captured["system"] = system
        return {"enough": False, "message": "Какая именно тема вам ближе?"}
    monkeypatch.setattr(pb, "chat_json", spy_llm)

    turn = await pb.interview_turn(
        [{"role": "user", "content": "новости про ИИ"}], ui_lang="ru")
    assert "in natural, native Russian" in captured["system"]
    assert turn["message"].startswith("Какая")

    await pb.interview_turn([{"role": "user", "content": "AI news"}],
                            ui_lang="en")
    assert "native Russian" not in captured["system"]


# ── preferences survive re-research (pre-existing bug for length/quiet) ──────

async def test_user_prefs_survive_reresearch(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="s",
                              criteria={"topic": "x", "conversation": "c"})
    store.set_stream_criteria_field(sid, "post_language", "ru")
    store.set_stream_criteria_field(sid, "post_length", "compact")
    store.set_stream_criteria_field(sid, "quiet_hours", "23-8")

    async def fake_research(answers, stream_id, progress=None):
        # A fresh profile, as the research engine would return — none of the
        # user-preference keys are in it.
        return {"profile": {"broad_domain": "ai", "language": "en",
                            "keywords": ["ai"]}}

    async def no_send(chat_id, markdown, extra_html=""):
        return {"ok": True}

    async def no_reconcile(sources):
        return {}

    monkeypatch.setattr(handlers, "run_research", fake_research)
    monkeypatch.setattr(handlers, "send_rich_async", no_send)
    monkeypatch.setattr(handlers, "_reconcile_sources", no_reconcile)

    await handlers._run_research_background(sid, {"conversation": "c"},
                                            chat_id=1, context=None, user_id=1)

    criteria = store.get_stream(sid)["criteria"]
    assert criteria["post_language"] == "ru"      # ← lost before this fix
    assert criteria["post_length"] == "compact"   # ← lost before this fix
    assert criteria["quiet_hours"] == "23-8"      # ← lost before this fix
    assert criteria["broad_domain"] == "ai"       # new profile still applied


# ── seeding at stream creation ────────────────────────────────────────────────

async def test_ru_interface_seeds_stream_post_language(temp_db, monkeypatch):
    sent = []

    async def fake_send(chat_id, markdown, extra_html=""):
        sent.append(markdown)
        return {"ok": True}

    async def no_research(*args, **kwargs):
        return None

    async def fake_bot_send(chat_id, text, **kw):
        sent.append(text)

    monkeypatch.setattr(handlers, "send_rich_async", fake_send)
    monkeypatch.setattr(handlers, "_run_research_background", no_research)
    monkeypatch.setattr(config, "ADMIN_USER_ID", 999)

    def _ctx():
        return SimpleNamespace(
            args=[], user_data={"transcript": [
                {"role": "user", "content": "новости про ИИ"}]},
            bot=SimpleNamespace(send_message=fake_bot_send),
        )

    store.set_ui_lang(5, "ru")
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=5, full_name="A", username="a"),
        effective_chat=SimpleNamespace(id=5))
    await handlers._start_research(upd, _ctx())

    streams = store.get_streams_by_user(5)
    assert streams[0]["criteria"]["post_language"] == "ru"

    # English interface seeds nothing — inference stays in charge.
    store.set_ui_lang(6, "en")
    upd6 = SimpleNamespace(
        effective_user=SimpleNamespace(id=6, full_name="B", username="b"),
        effective_chat=SimpleNamespace(id=6))
    await handlers._start_research(upd6, _ctx())
    assert "post_language" not in store.get_streams_by_user(6)[0]["criteria"]
