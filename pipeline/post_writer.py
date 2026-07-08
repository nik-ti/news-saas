"""
Pipeline — Post Writer.
Takes article content and writes a short Telegram news post via LLM.
Adapted from telegram-news-channel template.
"""
import logging

from research.llm import chat_post

logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = """\
You write short, punchy Telegram news posts from article content.

## Core Rule

You MUST write the post. ALWAYS. NO EXCEPTIONS.
Your output is ONLY the post itself. Nothing else. No explanations, \
no rejections, no meta-commentary. Start IMMEDIATELY with the post.

---

## Style & Format

**Writing:**
* Natural English. Professional, calm tone. No sensationalism.
* Short paragraphs (2-3 lines max), line breaks for readability.
* One post = one main point. Pick the most important news, explain it clearly.

**Length:** 300-600 characters.

**Emojis:** 1-3, used naturally.

**HTML formatting:** Use `<b>`, `<i>`, `<a href="">`, `<code>` only.
Bold key dates, names, numbers. Link to source if available.

---

## Context

Briefly explain specialized terms on first mention.

---

## Example Output

<b>EU finalises MiCA stablecoin rules</b>

The European Commission has approved the technical standards for MiCA's \
stablecoin provisions, setting strict reserve and transparency requirements.

📅 In effect: June 2026
🏛️ Affects: all stablecoin issuers operating in the EU
<a href="SOURCE_URL">Read more →</a>

---

**Input:** Article text (may include title, summary, and full content).
**Output:** Short Telegram news post, HTML format."""


async def write_post(article_text: str, source_url: str = "") -> str:
    """
    Write a short Telegram post from article content.
    Returns HTML string ready to send.
    """
    prompt = f"Article text:\n\n{article_text[:4000]}"
    if source_url:
        prompt += f"\n\nSource URL: {source_url}"

    try:
        raw = await chat_post(SYSTEM_MESSAGE, prompt)
        post = _strip_code_blocks(raw)
        post = _strip_preamble(post)
        return post
    except Exception as e:
        logger.error("Post writer error: %s", e)
        return ""


def _strip_code_blocks(text: str) -> str:
    """Remove markdown code block wrappers from LLM output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl != -1:
            cleaned = cleaned[first_nl + 1:]
        else:
            cleaned = cleaned[3:]
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