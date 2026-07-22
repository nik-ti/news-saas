"""Stream tuning (Part 3): natural-language rules on the relevance gate.

Covers the gate rendering (_rubric_for + _rules_section), the rule
interpreter's output contract, the store helpers, and the bot flow:
menu arm → free text → ✅/❌ confirmation → criteria write.
"""
from types import SimpleNamespace

import config
import bot.handlers as handlers
import research.rule_interpreter as ri
from database import store
from pipeline.relevance_checker import _rubric_for, _rules_section


# ── gate rendering ────────────────────────────────────────────────────────────

def test_rules_section_appended_to_bespoke_rubric():
    profile = {
        "relevance_rubric": "Send only EU crypto regulation news.",
        "rules": [
            {"id": 1, "kind": "exclude", "text": "Ukraine-Russia war",
             "active": True},
            {"id": 2, "kind": "include", "text": "MiCA enforcement",
             "active": True},
        ],
    }
    rubric = _rubric_for(profile)
    assert rubric.startswith("Send only EU crypto regulation news.")
    assert "## Hard user rules" in rubric
    assert "NEVER send articles about: Ukraine-Russia war" in rubric
    assert "ALWAYS send articles about: MiCA enforcement" in rubric


def test_rules_section_appended_even_to_topic_only_profile():
    # Tier-3 profile (no rubric, no structured fields) still gets the rules.
    profile = {"topic": "politics",
               "rules": [{"id": 1, "kind": "exclude", "text": "local sports",
                          "active": True}]}
    rubric = _rubric_for(profile)
    assert "politics" in rubric
    assert "NEVER send articles about: local sports" in rubric


def test_rules_section_skips_inactive_and_empty():
    profile = {"rules": [
        {"id": 1, "kind": "exclude", "text": "old rule", "active": False},
        {"id": 2, "kind": "include", "text": "", "active": True},
    ]}
    assert _rules_section(profile) == ""
    assert _rules_section({}) == ""
    assert _rules_section(None) == ""


# ── store helpers ─────────────────────────────────────────────────────────────

def test_rule_lifecycle(temp_db):
    sid = store.create_stream(user_id=1, name="politics",
                              criteria={"topic": "politics"})
    r1 = store.add_stream_rule(sid, "exclude", "Ukraine-Russia war")
    r2 = store.add_stream_rule(sid, "include", "EU AI regulation")
    assert (r1["id"], r2["id"]) == (1, 2)
    assert r1["active"] and r1["created_at"]

    rules = store.get_stream_rules(sid)
    assert [r["text"] for r in rules] == ["Ukraine-Russia war",
                                          "EU AI regulation"]

    assert store.deactivate_stream_rule(sid, r1["id"]) is True
    assert not store.get_stream_rules(sid)[0]["active"]

    # Re-adding the same rule reactivates it instead of duplicating.
    again = store.add_stream_rule(sid, "exclude", "  ukraine-russia WAR ")
    assert again["id"] == r1["id"]
    assert again["active"] is True
    assert len(store.get_stream_rules(sid)) == 2

    assert store.deactivate_stream_rule(sid, 999) is False


# ── interpreter output contract ───────────────────────────────────────────────

def _patch_interpreter(monkeypatch, payload):
    async def fake_chat_json(system, user, model="fast"):
        return payload
    monkeypatch.setattr(ri, "chat_json", fake_chat_json)


async def test_interpreter_add_exclude(monkeypatch):
    _patch_interpreter(monkeypatch, {"action": "add_exclude",
                                     "rule_text": "Ukraine-Russia war",
                                     "matched_rule_id": None})
    result = await ri.interpret_rule_request(
        {"topic": "politics", "rules": []}, "no more ukraine war news please")
    assert result == {"action": "add_exclude", "rule_text": "Ukraine-Russia war",
                      "matched_rule_id": None}


async def test_interpreter_guardian_off_topic(monkeypatch):
    _patch_interpreter(monkeypatch, {"action": "off_topic", "rule_text": "F1",
                                     "matched_rule_id": None})
    result = await ri.interpret_rule_request(
        {"topic": "politics", "broad_domain": "geopolitics", "rules": []},
        "also send me formula 1 news")
    assert result["action"] == "off_topic"


async def test_interpreter_garbage_fails_safe(monkeypatch):
    _patch_interpreter(monkeypatch, {"nonsense": True})
    result = await ri.interpret_rule_request({"topic": "x"}, "blah")
    assert result["action"] == "unclear"

    async def boom(system, user, model="fast"):
        raise RuntimeError("provider down")
    monkeypatch.setattr(ri, "chat_json", boom)
    result = await ri.interpret_rule_request({"topic": "x"}, "blah")
    assert result["action"] == "unclear"


async def test_interpreter_coerces_bad_rule_id(monkeypatch):
    _patch_interpreter(monkeypatch, {"action": "remove_rule", "rule_text": "",
                                     "matched_rule_id": "abc"})
    result = await ri.interpret_rule_request({"topic": "x"}, "drop that rule")
    assert result["matched_rule_id"] is None


# ── bot flow ──────────────────────────────────────────────────────────────────

class _Bot:
    """Fake context.bot recording send_message calls."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class _Query:
    """Fake CallbackQuery recording edits (mirrors tests/test_menu.py)."""
    def __init__(self, user_id, data):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat_id=user_id)
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


def _tune_update(user_id, text):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=user_id),
        effective_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(text=text),
    )


def _patch_sends(monkeypatch):
    rich = []

    async def fake_rich(chat_id, text, *a, **k):
        rich.append((chat_id, text))
        return {"ok": True}
    monkeypatch.setattr(handlers, "send_rich_async", fake_rich)
    return rich


def _patch_interpret(monkeypatch, result):
    async def fake(criteria, user_text):
        return result
    monkeypatch.setattr(handlers, "interpret_rule_request", fake)


async def test_tune_add_rule_confirm_yes(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="Politics",
                              criteria={"topic": "politics"})
    _patch_sends(monkeypatch)
    _patch_interpret(monkeypatch, {"action": "add_exclude",
                                   "rule_text": "Ukraine-Russia war",
                                   "matched_rule_id": None})
    bot = _Bot()
    ctx = SimpleNamespace(user_data={"tune_stream": sid}, bot=bot)

    await handlers.handle_free_text(_tune_update(1, "stop ukraine war news"), ctx)
    # Nothing written yet — a confirmation prompt with ✅/❌ went out.
    assert store.get_stream_rules(sid) == []
    assert "Ukraine-Russia war" in bot.sent[-1][1]
    assert ctx.user_data["pending_rule"]["kind"] == "exclude"

    q = _Query(1, f"rule_ok:{sid}")
    await handlers.handle_callback(SimpleNamespace(callback_query=q), ctx)
    rules = store.get_stream_rules(sid)
    assert len(rules) == 1 and rules[0]["kind"] == "exclude"
    assert rules[0]["text"] == "Ukraine-Russia war"
    assert "pending_rule" not in ctx.user_data


async def test_tune_confirm_no_writes_nothing(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="P", criteria={"topic": "p"})
    _patch_sends(monkeypatch)
    _patch_interpret(monkeypatch, {"action": "add_include",
                                   "rule_text": "EU AI regulation",
                                   "matched_rule_id": None})
    ctx = SimpleNamespace(user_data={"tune_stream": sid}, bot=_Bot())
    await handlers.handle_free_text(_tune_update(1, "more AI regulation"), ctx)

    q = _Query(1, f"rule_no:{sid}")
    await handlers.handle_callback(SimpleNamespace(callback_query=q), ctx)
    assert store.get_stream_rules(sid) == []


async def test_tune_off_topic_guardian(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="Politics",
                              criteria={"topic": "politics"})
    _patch_sends(monkeypatch)
    _patch_interpret(monkeypatch, {"action": "off_topic", "rule_text": "F1",
                                   "matched_rule_id": None})
    bot = _Bot()
    ctx = SimpleNamespace(user_data={"tune_stream": sid}, bot=bot)
    await handlers.handle_free_text(_tune_update(1, "also send me F1 news"), ctx)
    assert store.get_stream_rules(sid) == []
    assert "pending_rule" not in ctx.user_data
    assert "/newstream" in bot.sent[-1][1]


async def test_tune_duplicate_detected(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="P", criteria={"topic": "p"})
    existing = store.add_stream_rule(sid, "exclude", "Ukraine-Russia war")
    rich = _patch_sends(monkeypatch)
    _patch_interpret(monkeypatch, {"action": "add_exclude",
                                   "rule_text": "Ukraine-Russia war",
                                   "matched_rule_id": existing["id"]})
    ctx = SimpleNamespace(user_data={"tune_stream": sid}, bot=_Bot())
    await handlers.handle_free_text(_tune_update(1, "no ukraine news"), ctx)
    assert "pending_rule" not in ctx.user_data
    assert len(store.get_stream_rules(sid)) == 1
    assert rich  # the "already covered" reply


async def test_tune_rule_cap(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="P", criteria={"topic": "p"})
    for i in range(config.MAX_STREAM_RULES):
        store.add_stream_rule(sid, "exclude", f"topic {i}")
    rich = _patch_sends(monkeypatch)
    _patch_interpret(monkeypatch, {"action": "add_exclude",
                                   "rule_text": "one more thing",
                                   "matched_rule_id": None})
    ctx = SimpleNamespace(user_data={"tune_stream": sid}, bot=_Bot())
    await handlers.handle_free_text(_tune_update(1, "also exclude X"), ctx)
    assert "pending_rule" not in ctx.user_data
    assert len(store.get_stream_rules(sid)) == config.MAX_STREAM_RULES
    assert rich  # the cap message


async def test_tune_remove_rule_flow(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="P", criteria={"topic": "p"})
    rule = store.add_stream_rule(sid, "exclude", "Ukraine-Russia war")
    _patch_sends(monkeypatch)
    _patch_interpret(monkeypatch, {"action": "remove_rule", "rule_text": "",
                                   "matched_rule_id": rule["id"]})
    ctx = SimpleNamespace(user_data={"tune_stream": sid}, bot=_Bot())
    await handlers.handle_free_text(_tune_update(1, "actually, ukraine news is fine"), ctx)
    assert ctx.user_data["pending_rule"]["op"] == "remove"

    q = _Query(1, f"rule_ok:{sid}")
    await handlers.handle_callback(SimpleNamespace(callback_query=q), ctx)
    assert not store.get_stream_rules(sid)[0]["active"]


async def test_tune_delete_button_and_rearm(temp_db, monkeypatch):
    sid = store.create_stream(user_id=1, name="P", criteria={"topic": "p"})
    rule = store.add_stream_rule(sid, "exclude", "local sports")
    ctx = SimpleNamespace(user_data={})

    q = _Query(1, f"rule_del:{sid}:{rule['id']}")
    q.edit_message_text = _safe_edit_recorder(q)
    await handlers.handle_callback(SimpleNamespace(callback_query=q), ctx)
    assert not store.get_stream_rules(sid)[0]["active"]
    assert ctx.user_data["tune_stream"] == sid   # prompt stays armed


def _safe_edit_recorder(q):
    """rule_del goes through _safe_edit → edit_message_text with markup."""
    async def rec(text, reply_markup=None, **kw):
        q.edits.append(text)
    return rec


async def test_menu_tune_button_arms_and_lists_rules(temp_db):
    sid = store.create_stream(user_id=1, name="P", criteria={"topic": "p"})
    store.add_stream_rule(sid, "exclude", "local sports")
    ctx = SimpleNamespace(user_data={})

    q = _Query(1, f"menu:tune:{sid}")
    q.edit_message_text = _safe_edit_recorder(q)
    await handlers.handle_callback(SimpleNamespace(callback_query=q), ctx)
    assert ctx.user_data["tune_stream"] == sid
    assert "local sports" in q.edits[-1]


async def test_free_text_ignores_unarmed_chatter(temp_db, monkeypatch):
    rich = _patch_sends(monkeypatch)
    ctx = SimpleNamespace(user_data={}, bot=_Bot())
    await handlers.handle_free_text(_tune_update(1, "hello bot"), ctx)
    assert rich == []
