"""
Phase 1 — Query Understanding.

Two responsibilities:
  1. Run a short, NATURAL intake conversation with the user (interview_turn).
     No rigid "Q1/Q2/Q3" form, no visible "generating questions" scaffolding —
     one sharp, human follow-up at a time, driven by a real research protocol
     that only probes what actually changes which sources we'd pick.
  2. Compile the whole conversation into a structured Source Criteria Profile
     that guides the autonomous source-discovery system (build_profile).
"""
import json
import logging

from research.llm import chat, chat_json

logger = logging.getLogger(__name__)

# ── Conversation opener ───────────────────────────────────────────────────────
# Static, warm, human. The interviewer LLM takes over from the user's reply.
OPENER = (
    "What would you like me to keep you in the loop on?\n\n"
    "Tell me about the news you're after — a topic, a beat, a question you're "
    "trying to stay ahead of. As specific or as loose as you like, in your own words."
)

# Hard cap on how many times we'll ask the user something. The opener is free;
# after this many *answers* we always wrap up and start researching.
MAX_INTERVIEW_ANSWERS = 4


# ── The intake specialist (research protocol lives here) ─────────────────────
SYSTEM_PROMPT_INTERVIEW = """\
You are the intake specialist for a personalised news service. Through a short,
natural conversation you work out exactly what news one person wants, so that a
downstream research engine can go and find the best sources for them — news
sites, blogs, expert feeds, and so on.

You are talking to the user directly. Sound like a sharp, friendly human who
knows the beat — never like a form or a survey. One message at a time. Never
number your questions, never present a list of questions, never narrate your
own process ("let me now ask...", "generating questions").

── What you must understand before handing off (the research protocol) ──
1. THE BEAT + ANGLE. The core subject AND the specific slice they care about.
   "Crypto" is not enough — "EU crypto regulation, especially MiCA enforcement"
   is. Gently pull them toward the specific angle if they start broad.
2. WHAT COUNTS AS A HIT. What a genuinely relevant story looks like to them, and
   what a near-miss looks like. This is what lets the engine judge a source's
   articles rather than just its topic.
3. HARD EXCLUSIONS. Anything they never want to see — angles, sub-topics, or
   formats (price-prediction spam, clickbait, press releases, etc.).
4. ONLY IF IT GENUINELY MATTERS: geographic/language focus, and whether they
   want fast headlines or deep analysis (this changes what kind of source fits).

── How to run the conversation ──
- Ask only about things that are still genuinely unclear AND would change which
  sources you'd choose. Never ask filler. Never re-ask what they've told you.
- One focused question per turn, phrased warmly, that clearly builds on what
  they just said (reference their actual words).
- If their answers already cover the essentials, STOP — do not drag it out.
  A rich first answer can mean zero follow-ups. Most need one or two.
- Never ask more than a few questions total.

── Output ──
Respond with ONLY valid JSON, nothing else:
{"enough": <true or false>, "message": "<your next message to the user>"}

- If "enough" is false: "message" is your next question.
- If "enough" is true: "message" is a short, natural, ONE-sentence sign-off that
  tells them you've got what you need and you're going to find their sources now.
  Do not ask anything when enough is true."""


def _format_transcript(transcript: list[dict]) -> str:
    """Render the running conversation for the LLM prompt."""
    lines = []
    for turn in transcript:
        who = "User" if turn["role"] == "user" else "You"
        lines.append(f"{who}: {turn['content']}")
    return "\n".join(lines)


async def interview_turn(transcript: list[dict]) -> dict:
    """
    Given the conversation so far, decide the next natural move.

    Returns {"enough": bool, "message": str}:
      - enough=False → message is the next question to ask.
      - enough=True  → message is a short sign-off; research should begin.
    """
    answers_given = sum(1 for t in transcript if t["role"] == "user")
    force_wrap = answers_given >= MAX_INTERVIEW_ANSWERS

    convo = _format_transcript(transcript)
    if force_wrap:
        instruction = (
            "You now have plenty to work with. Set \"enough\" to true and give "
            "your one-sentence sign-off."
        )
    else:
        instruction = "Give your next-turn JSON."

    result = await chat_json(
        SYSTEM_PROMPT_INTERVIEW,
        f"Conversation so far:\n\n{convo}\n\n{instruction}",
        model="smart",
    )

    # Robust fallbacks — never leave the user stuck mid-conversation.
    if not isinstance(result, dict) or not result.get("message"):
        logger.warning("Interview turn returned unusable result: %s", result)
        return {
            "enough": True,
            "message": "Got it — that's enough to go on. Let me find your sources.",
        }

    enough = bool(result.get("enough")) or force_wrap
    return {"enough": enough, "message": str(result["message"]).strip()}


# ── Profile compilation ───────────────────────────────────────────────────────
SYSTEM_PROMPT_PROFILE = """\
You are a research strategist for a news aggregation service. You are given the
full intake conversation between our service and a user. Turn it into a
structured "Source Criteria Profile" that will guide an autonomous
source-discovery system.

Read the WHOLE conversation and infer intent — including things the user implied
but did not state outright. Output valid JSON with EXACTLY these fields:
{
  "broad_domain": "<the broad news category, e.g. 'cryptocurrency', 'geopolitics'>",
  "specific_topics": ["<the focused sub-topics / angles the user actually cares about>"],
  "hit_criteria": "<one sentence: what makes an article a genuine match for this user>",
  "exclude": ["<topics, angles, or content types to exclude>"],
  "geography": "<geographic focus or 'global'>",
  "language": "<preferred language, default 'en'>",
  "strictness": "<'low', 'medium', or 'high'>",
  "min_frequency": "<'daily', 'weekly', or 'monthly' — how often new content should appear>",
  "source_type": "<'news_site', 'blog', 'analysis', or 'mixed'>",
  "keywords": ["<5-10 search keywords/phrases that would help FIND these sources>"],
  "description": "<1-2 sentence summary of exactly what kind of sources we're looking for>"
}

Be precise. If the user wants "DeFi regulation in Europe", broad_domain is
'cryptocurrency' but specific_topics should be ['DeFi regulation', 'EU regulatory
policy']. Keywords should include varied phrasings a search engine understands.
Infer strictness from how narrow and exclusive the user was."""


# ── Per-stream relevance rubric ───────────────────────────────────────────────
# The gate that decides whether an individual article is worth sending is only as
# good as its rubric. A generic one lets academic paper listings through. So we
# write a bespoke rubric from THIS user's own words, once, at research time.

RUBRIC_VERSION = 1

SYSTEM_PROMPT_RUBRIC = """\
You are configuring a binary relevance gate for ONE person's personalised news feed.
From their own intake conversation, write the BODY of a classifier instruction that
decides whether a single article should be sent to THIS user.

Be concrete, and use THEIR language and THEIR examples. Cover:
- What ALWAYS counts as a hit.
- Near-misses that should still pass.
- Hard EXCLUSIONS — topics, angles, or formats they never want.

120-200 words. Imperative voice. No preamble, no headings, no JSON — just the
rubric text itself, ready to be dropped into a classifier prompt."""


async def build_relevance_rubric(answers: dict, profile: dict) -> str:
    """Write a bespoke relevance rubric from the user's intake conversation."""
    payload = json.dumps(
        {"conversation": answers, "profile": profile}, indent=2, ensure_ascii=False
    )
    try:
        rubric = await chat(
            SYSTEM_PROMPT_RUBRIC,
            f"Intake conversation and derived profile:\n\n{payload}\n\n"
            "Write the relevance rubric.",
            model="smart",
        )
    except Exception as e:
        logger.error("Rubric generation failed: %s", e)
        return ""
    return (rubric or "").strip()


async def build_profile(answers: dict) -> dict:
    """
    Turn the intake conversation into a structured Source Criteria Profile,
    including a bespoke relevance rubric used later to gate individual articles.

    `answers` carries a "conversation" transcript (and a "topic" seed). We hand
    the whole thing to the LLM so it can reason over the full exchange.
    """
    answers_text = json.dumps(answers, indent=2, ensure_ascii=False)
    profile = await chat_json(
        SYSTEM_PROMPT_PROFILE,
        f"Here is the full intake conversation:\n\n{answers_text}\n\n"
        "Generate the Source Criteria Profile as JSON.",
        model="smart",
    )

    if not profile:
        logger.error("Profile generation returned empty. Answers: %s", answers)
        seed = answers.get("topic") or answers.get("conversation") or "general"
        if not isinstance(seed, str):
            seed = "general news"
        profile = {
            "broad_domain": seed[:60],
            "specific_topics": [seed[:80]],
            "hit_criteria": f"Articles about {seed[:80]}",
            "exclude": [],
            "geography": "global",
            "language": "en",
            "strictness": "medium",
            "min_frequency": "daily",
            "source_type": "mixed",
            "keywords": [seed[:60]],
            "description": seed[:120],
        }

    rubric = await build_relevance_rubric(answers, profile)
    if rubric:
        profile["relevance_rubric"] = rubric
        profile["rubric_version"] = RUBRIC_VERSION

    logger.info("Built profile: %s (rubric: %s)",
                profile.get("broad_domain"), "yes" if rubric else "no")
    return profile
