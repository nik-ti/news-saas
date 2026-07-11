"""
Pipeline — Post Writer.
Turns an article SUMMARY into a short, clean Telegram news post (Telegram HTML).
Adapted from the re_news_channel post writer.
"""
import html as html_mod
import logging

import config
from research.llm import chat

logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = """\
You write short English-language Telegram news posts.

## Core Rule

You MUST write the post. ALWAYS. NO EXCEPTIONS.
Relevance is checked by another system — your job is ONLY to write.
If the article seems off-topic or unclear, write about whatever news IS in it anyway.

Your output is ONLY the post itself. Nothing else. No explanations, no rejections, \
no meta-commentary, no preamble. Start IMMEDIATELY with the post title.

---

## Factual Accuracy

Preserve the precise meaning of the source. Never strengthen language for impact, \
and never make uncertain things sound certain:

* "projected growth" → projected, NOT guaranteed
* "under consideration" / "under review" → being considered, NOT decided
* "could lead to" → could, NOT will
* "proposed" → proposed, NOT launched
* Delayed ≠ Cancelled ≠ Approved.

If the source hedges, you hedge.

---

## One Post = One Main Point

Focus on ONE main news item. Don't cover multiple developments or summarise an \
entire long article. Before writing, identify: (1) the ONE main piece of news, \
(2) who it affects, (3) when it takes effect, (4) what context the reader needs.

---

## Style & Format

**Writing:**
* Natural English. Professional, calm tone. No sensationalism, no hype.
* No first-person, no rhetorical questions.
* Short paragraphs (2-3 lines max), blank line between them for readability.
* Use 🔹 bullet points for listing related facts.

**Structure:**
* First line: the headline, wrapped in <b>...</b>, optionally ending with one emoji.
* Blank line, then the body.

**Length:** {length_rule}

**Emojis:** 1-3, used naturally. Common: 📊 📌 ⚠️ ✅ 🔹 📎 ➡️ 🏛 💼 🔬

**HTML only:** <b>, <i>, <code>, <a href="">. Bold key numbers, dates, names.
Never use <p>, <ul>, <li>, <h1> or any other tag — Telegram rejects them.

---

## Context

Briefly explain specialised terms, acronyms, or jargon on first mention.

---

## Example Output

<b>EU finalises MiCA stablecoin rules 🏛</b>

The European Commission has approved the technical standards for MiCA's \
stablecoin provisions, setting reserve and transparency requirements for issuers.

🔹 In effect: <b>June 2026</b>
🔹 Affects: all stablecoin issuers operating in the EU

MiCA (Markets in Crypto-Assets) is the EU's unified crypto framework, phased in since 2024.

---

**Input:** An article summary (and its title).
**Output:** A short Telegram news post in English, HTML format."""


# The one line that changes with the user's chosen post length.
_LENGTH_RULES = {
    "standard": "80-100 words. Give the reader the full picture — the what, "
                "who, when, and why it matters — in tight prose.",
    "compact": "2-3 sentences, ~40 words max. Just the essential news, nothing more.",
}


def _length_rule(length: str) -> str:
    return _LENGTH_RULES.get(length, _LENGTH_RULES["standard"])


async def write_post(summary_text: str, title: str = "", source_url: str = "",
                     length: str = "standard") -> str:
    """
    Write a Telegram post from an article summary, at the stream's chosen length.
    Returns Telegram-HTML ready to send, with a source link appended.
    """
    system = SYSTEM_MESSAGE.replace("{length_rule}", _length_rule(length))

    parts = []
    if title:
        parts.append(f"Title: {title}")
    parts.append(f"Summary:\n{summary_text[:config.POST_INPUT_CHAR_CAP]}")
    prompt = "\n\n".join(parts)

    try:
        raw = await chat(system, prompt, model="post")
    except Exception as e:
        logger.error("Post writer error: %s", e)
        return ""

    post = _strip_preamble(_strip_code_blocks(raw))
    if not post:
        return ""

    if source_url:
        # Crawled URLs can carry quotes/angle brackets; unescaped they break the
        # anchor and Telegram rejects the whole message with a 400.
        safe_url = html_mod.escape(source_url, quote=True)
        post = f'{post}\n\n🔗 <a href="{safe_url}">Source</a>'
    return post


def _strip_code_blocks(text: str) -> str:
    """Remove markdown code block wrappers from LLM output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        cleaned = cleaned[first_nl + 1:] if first_nl != -1 else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _strip_preamble(text: str) -> str:
    """Remove LLM meta-commentary before the actual post."""
    for delimiter in ("\n---\n", "\n---", "\n\n---\n\n"):
        if delimiter in text:
            parts = text.split(delimiter, 1)
            if len(parts) == 2:
                return parts[1].strip()
    lines = text.strip().split("\n")
    skip_patterns = (
        "this article", "the article", "following your", "however",
        "in accordance", "based on", "sure here", "here is", "here's",
    )
    while lines and lines[0].strip().lower().startswith(skip_patterns):
        lines.pop(0)
    return "\n".join(lines).strip()
