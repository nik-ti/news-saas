"""
OpenRouter LLM wrapper via LangChain.
Provides async chat completions for the research engine and pipeline.
"""
import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI

import config

logger = logging.getLogger(__name__)

# ── LLM instances (singleton) ─────────────────────────────────────────────────
_llm_fast: Optional[ChatOpenAI] = None
_llm_smart: Optional[ChatOpenAI] = None
_llm_post: Optional[ChatOpenAI] = None


def get_llm_fast() -> ChatOpenAI:
    """Fast/cheap LLM for simple tasks (query generation, summaries)."""
    global _llm_fast
    if _llm_fast is None:
        _llm_fast = ChatOpenAI(
            model=config.LLM_MODEL_FAST,
            openai_api_key=config.OPENROUTER_API_KEY,
            openai_api_base=config.OPENROUTER_BASE_URL,
            temperature=config.LLM_TEMPERATURE,
            timeout=30,
            max_retries=2,
        )
    return _llm_fast


def get_llm_smart() -> ChatOpenAI:
    """Smart LLM for complex reasoning (qualification, profile building)."""
    global _llm_smart
    if _llm_smart is None:
        _llm_smart = ChatOpenAI(
            model=config.LLM_MODEL_SMART,
            openai_api_key=config.OPENROUTER_API_KEY,
            openai_api_base=config.OPENROUTER_BASE_URL,
            temperature=config.LLM_TEMPERATURE,
            timeout=60,
            max_retries=2,
        )
    return _llm_smart


def get_llm_post() -> ChatOpenAI:
    """Cheap LLM for post writing (Gemini Flash)."""
    global _llm_post
    if _llm_post is None:
        _llm_post = ChatOpenAI(
            model=config.LLM_MODEL_POST,
            openai_api_key=config.OPENROUTER_API_KEY,
            openai_api_base=config.OPENROUTER_BASE_URL,
            temperature=0.2,
            timeout=30,
            max_retries=2,
        )
    return _llm_post


async def _openrouter_embeddings(model: str, inputs: list[str]) -> list[list[float]]:
    """
    Call OpenRouter's /embeddings endpoint directly (langchain's chat client
    doesn't cover embeddings). Returns one vector per input, in order.
    """
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{config.OPENROUTER_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            json={"model": model, "input": inputs},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    # Preserve request order (OpenAI-compatible responses carry an index).
    rows = sorted(data["data"], key=lambda d: d.get("index", 0))
    return [row["embedding"] for row in rows]


async def chat(system_prompt: str, user_prompt: str, smart: bool = False) -> str:
    """
    Simple async chat call. Returns the text response.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_llm_smart() if smart else get_llm_fast()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    response = await llm.ainvoke(messages)
    return response.content


async def chat_post(system_prompt: str, user_prompt: str) -> str:
    """
    Chat call using the post-writing LLM (cheap model, e.g. Gemini Flash).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_llm_post()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    response = await llm.ainvoke(messages)
    return response.content


async def chat_json(system_prompt: str, user_prompt: str, smart: bool = False) -> dict:
    """
    Async chat call that expects a JSON response.
    Strips markdown fences and parses.
    """
    raw = await chat(system_prompt, user_prompt, smart=smart)
    return parse_json_response(raw)


def parse_json_response(raw: str) -> dict:
    """Extract and parse JSON from an LLM response, handling fences and prose."""
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object boundaries
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    logger.error("Failed to parse JSON from LLM response: %s", raw[:500])
    return {}