"""
Pipeline — Relevance Gate.

A binary yes/no decision on whether ONE article belongs in a given stream,
made against a rubric written from that user's own intake conversation.

This gate is what stands between the user and a wall of academic paper titles.
It runs BEFORE the post writer, so nothing irrelevant is ever written or sent.
"""
import json
import logging

from research.llm import chat_json

logger = logging.getLogger(__name__)

GATE_INSTRUCTIONS = """\
You are a strict relevance classifier for one person's personalised news feed.

You are given that person's relevance rubric, plus a single article's title and
summary. Decide whether this article should be sent to them.

Judge ONLY against the rubric. When the rubric names an exclusion, honour it even
if the article is otherwise on-topic. When genuinely uncertain, answer false —
a missed article costs less than a wasted notification.

Return ONLY this JSON object, nothing else:
{"is_relevant": true or false, "reason": "<one short sentence>"}"""


def _rubric_for(profile: dict) -> str:
    """
    Resolve the rubric body for a stream profile.

    Three tiers, because stored profiles vary in age:
      1. A bespoke rubric generated at research time.
      2. Synthesised from the structured profile fields.
      3. Last resort — the raw topic the user typed.
    """
    if not isinstance(profile, dict):
        return "Send the article only if it is clearly newsworthy and on-topic."

    rubric = (profile.get("relevance_rubric") or "").strip()
    if rubric:
        return rubric

    # Tier 2 — synthesise from whatever structured fields exist.
    lines = []
    if profile.get("hit_criteria"):
        lines.append(f"An article is a HIT when: {profile['hit_criteria']}")
    if profile.get("broad_domain"):
        lines.append(f"Broad domain: {profile['broad_domain']}.")
    topics = profile.get("specific_topics") or []
    if topics:
        lines.append("Specific topics that matter: " + "; ".join(map(str, topics)) + ".")
    excludes = profile.get("exclude") or []
    if excludes:
        lines.append("NEVER send articles about: " + "; ".join(map(str, excludes)) + ".")
    if lines:
        return "\n".join(lines)

    # Tier 3 — pre-research profile: only the raw conversation/topic exists.
    topic = (profile.get("topic") or profile.get("description") or "").strip()
    if topic:
        return (
            f"The user asked for news about: {topic}\n"
            "Send the article only if it directly covers that subject as a news "
            "development. Exclude tangential, promotional, or reference material."
        )

    return "Send the article only if it is clearly newsworthy and on-topic."


async def check_relevance(title: str, summary: str, profile: dict) -> tuple[bool, str]:
    """
    Returns (is_relevant, reason). Fails closed: any error → not relevant.
    """
    rubric = _rubric_for(profile)

    try:
        result = await chat_json(
            f"{GATE_INSTRUCTIONS}\n\n## The user's relevance rubric\n{rubric}",
            f"## Article\nTitle: {title}\n\nSummary: {summary}\n\nIs this relevant?",
        )
    except Exception as e:
        logger.error("Relevance gate error for %r: %s", title[:60], e)
        return False, "gate error"

    if not isinstance(result, dict) or "is_relevant" not in result:
        logger.warning("Relevance gate returned unusable result for %r: %s",
                       title[:60], json.dumps(result)[:200])
        return False, "unparseable gate response"

    return bool(result["is_relevant"]), str(result.get("reason", ""))[:200]
