"""
Phase 1 — Query Understanding.
Builds a structured Source Criteria Profile from the user's answers.
"""
import json
import logging

from research.llm import chat_json, chat

logger = logging.getLogger(__name__)

# ── Premade questions ────────────────────────────────────────────────────────
PREMADE_QUESTIONS = [
    {
        "key": "topic",
        "question": "What topic or kind of news are you interested in? "
                    "Describe it as specifically as you can.",
        "placeholder": "e.g. EU crypto regulation, specifically MiCA framework updates",
    },
    {
        "key": "strictness",
        "question": "How strict should the source matching be? "
                    "Should sources focus *exclusively* on this topic, or is broader coverage okay?",
        "placeholder": "e.g. very strict — only sources that focus primarily on this",
    },
    {
        "key": "exclusions",
        "question": "What do you NOT want to see? Any sub-topics, angles, or content types to exclude?",
        "placeholder": "e.g. no price predictions, no memecoins, no clickbait",
    },
]

SYSTEM_PROMPT_PROFILE = """\
You are a research strategist for a news aggregation service.
Your job is to take a user's free-form answers about what news they want and \
turn them into a structured "Source Criteria Profile" that will guide an \
autonomous source-discovery system.

You must output valid JSON with EXACTLY these fields:
{
  "broad_domain": "<the broad news category, e.g. 'cryptocurrency', 'geopolitics'>",
  "specific_topics": ["<focused sub-topics the user cares about>"],
  "exclude": ["<topics/angles/content types to exclude>"],
  "geography": "<geographic focus or 'global'>",
  "language": "<preferred language, default 'en'>",
  "strictness": "<'low', 'medium', or 'high'>",
  "min_frequency": "<'daily', 'weekly', or 'monthly' — how often new content appears>",
  "source_type": "<'news_site', 'blog', 'analysis', or 'mixed'>",
  "keywords": ["<5-10 search keywords/phrases that would help find these sources>"],
  "description": "<1-2 sentence summary of exactly what kind of sources we're looking for>"
}

Be precise. If the user says "DeFi regulation in Europe", the broad_domain is \
'cryptocurrency' but specific_topics should be ['DeFi regulation', 'EU regulatory policy']. \
Keywords should include varied phrasings a search engine would understand."""


SYSTEM_PROMPT_FOLLOWUP = """\
You are a research strategist helping to gather information from a user \
who wants a personalised news feed.

Based on the user's answers so far, generate 1-2 SHORT follow-up questions \
that will help the research system find the best sources. Ask about things \
that are still ambiguous and would affect source selection — e.g.:
- Specific sub-topics or angles they care about
- Preferred depth (quick headlines vs. deep analysis)
- Quality bar (mainstream only, or niche expert sources too?)
- Format preferences

Output ONLY the questions, one per line. No numbering, no prefixes.
Maximum 2 questions. Each question should be under 20 words."""


async def build_profile(answers: dict) -> dict:
    """
    Turn the user's answers (topic, strictness, exclusions, follow-ups) into
    a structured Source Criteria Profile.
    """
    answers_text = json.dumps(answers, indent=2)
    profile = await chat_json(
        SYSTEM_PROMPT_PROFILE,
        f"Here are the user's answers:\n\n{answers_text}\n\n"
        "Generate the Source Criteria Profile as JSON.",
        smart=True,
    )

    if not profile:
        logger.error("Profile generation returned empty. Answers: %s", answers)
        # Fallback minimal profile
        profile = {
            "broad_domain": answers.get("topic", "general"),
            "specific_topics": [answers.get("topic", "")],
            "exclude": [],
            "geography": "global",
            "language": "en",
            "strictness": "medium",
            "min_frequency": "daily",
            "source_type": "news_site",
            "keywords": [answers.get("topic", "")],
            "description": answers.get("topic", "General news"),
        }

    logger.info("Built profile: %s", profile.get("broad_domain"))
    return profile


async def generate_followup_questions(answers: dict) -> list[str]:
    """
    Generate dynamic follow-up questions based on the user's initial answers.
    """
    answers_text = json.dumps(answers, indent=2)
    raw = await chat(
        SYSTEM_PROMPT_FOLLOWUP,
        f"User's answers so far:\n\n{answers_text}\n\n"
        "Generate 1-2 follow-up questions.",
    )

    questions = [
        line.strip()
        for line in raw.strip().split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    return questions[:2]  # max 2