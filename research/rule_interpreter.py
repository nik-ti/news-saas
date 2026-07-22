"""
Stream tuning — the rule interpreter.

Turns ONE free-text user request ("stop sending me news about the Ukraine
war") into ONE structured action on the stream's rule list. The LLM never
edits prompt prose: it classifies the request and normalises the topic into a
short phrase, and CODE renders the rules into the relevance gate's prompt
(relevance_checker._rules_section). That split is what keeps the gate prompt
deterministic no matter how rambling the user's message is.

The interpreter doubles as the topic guardian: a request that would ADD
coverage outside the stream's beat comes back as "off_topic" so the bot can
suggest a separate stream instead of overloading this stream's gate.
"""
import json
import logging

from research.llm import chat_json

logger = logging.getLogger(__name__)

ACTIONS = ("add_exclude", "add_include", "remove_rule", "off_topic", "unclear")

SYSTEM_PROMPT_RULE_INTERPRETER = """\
You configure the relevance filter of ONE news stream in a personalised news
service. The stream has a FIXED beat, chosen when the user created it. The
user now sends a free-text request to change what the stream delivers.

Turn their request into exactly ONE structured action.

── Actions ──
- "add_exclude" — the user wants to STOP seeing some topic, angle, or content
  type. Narrowing the stream is ALWAYS allowed, whatever the topic.
- "add_include" — the user wants MORE of something that lies INSIDE the
  stream's existing beat.
- "remove_rule" — the user wants to undo one of the stream's existing rules.
  Set "matched_rule_id" to that rule's id.
- "off_topic" — the request would ADD coverage of a subject clearly OUTSIDE
  the stream's beat (e.g. asking a politics stream to also cover F1). This
  service keeps one topic per stream, so such requests are declined with a
  suggestion to create a separate stream. Note: excluding anything is never
  off-topic — only broadening can be.
- "unclear" — you cannot confidently map the request to one of the above.

── Output ──
Respond with ONLY valid JSON, nothing else:
{"action": "<one of the actions>", "rule_text": "<short topic phrase>",
 "matched_rule_id": <rule id or null>}

Rules:
- "rule_text" is a normalised topic phrase of 2-6 words ("Ukraine-Russia war",
  "US election polls"), NOT the user's raw sentence. Write it in the same
  language the user wrote in. Empty string when the action needs none.
- If the request duplicates an existing rule (same topic, same direction),
  set "matched_rule_id" to that rule's id — even for add_exclude/add_include.
- ONE action only. If the user lists several wishes, take the first one; they
  can send the rest as separate messages.
- "matched_rule_id" is null unless the request matches an existing rule."""


def _rules_brief(rules: list[dict]) -> str:
    """Render the stream's active rules for the interpreter prompt."""
    active = [r for r in rules if r.get("active") and r.get("text")]
    if not active:
        return "(none yet)"
    return "\n".join(
        f"  #{r['id']} {r.get('kind', '?')}: {r['text']}" for r in active)


async def interpret_rule_request(criteria: dict, user_text: str) -> dict:
    """
    Classify one tuning request against the stream's profile.

    Returns {"action": one of ACTIONS, "rule_text": str,
             "matched_rule_id": int | None}. Fails safe to "unclear" on any
    error or unparseable output — nothing is ever written on unclear.
    """
    criteria = criteria if isinstance(criteria, dict) else {}
    rules = criteria.get("rules") or []
    context = {
        "topic": criteria.get("topic") or criteria.get("description") or "",
        "broad_domain": criteria.get("broad_domain") or "",
        "specific_topics": criteria.get("specific_topics") or [],
    }

    user_prompt = (
        f"The stream's beat:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        f"The stream's current rules:\n{_rules_brief(rules)}\n\n"
        f"The user's request:\n\"{user_text.strip()}\"\n\n"
        "Give your action JSON."
    )

    try:
        result = await chat_json(SYSTEM_PROMPT_RULE_INTERPRETER, user_prompt,
                                 model="smart")
    except Exception as e:
        logger.error("Rule interpreter error: %s", e)
        return {"action": "unclear", "rule_text": "", "matched_rule_id": None}

    if not isinstance(result, dict) or result.get("action") not in ACTIONS:
        logger.warning("Rule interpreter returned unusable result: %s", result)
        return {"action": "unclear", "rule_text": "", "matched_rule_id": None}

    rule_id = result.get("matched_rule_id")
    try:
        rule_id = int(rule_id) if rule_id is not None else None
    except (TypeError, ValueError):
        rule_id = None

    return {
        "action": result["action"],
        "rule_text": str(result.get("rule_text") or "").strip()[:120],
        "matched_rule_id": rule_id,
    }
