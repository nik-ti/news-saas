"""
OpenRouter LLM wrapper via LangChain.
Provides async chat completions for the research engine and pipeline.
"""
import json
import logging

from langchain_openai import ChatOpenAI

import config

logger = logging.getLogger(__name__)

# ── LLM instances ─────────────────────────────────────────────────────────────
# One spec table, one lazy cache. Adding a model (fact-check tier, premium
# crawler…) is a dict entry, not a fourth copy-pasted singleton.

# kind -> (model id, temperature, timeout seconds)
_LLM_SPECS = {
    "fast": (config.LLM_MODEL_FAST, config.LLM_TEMPERATURE, 30),
    "smart": (config.LLM_MODEL_SMART, config.LLM_TEMPERATURE, 60),
    "post": (config.LLM_MODEL_POST, 0.2, 30),
}
_llms: dict[str, ChatOpenAI] = {}


def _get_llm(kind: str) -> ChatOpenAI:
    if kind not in _llms:
        model, temperature, timeout = _LLM_SPECS[kind]
        _llms[kind] = ChatOpenAI(
            model=model,
            openai_api_key=config.OPENROUTER_API_KEY,
            openai_api_base=config.OPENROUTER_BASE_URL,
            temperature=temperature,
            timeout=timeout,
            max_retries=2,
        )
    return _llms[kind]


async def _openrouter_embeddings(model: str, inputs: list[str]) -> list[list[float]]:
    """
    Call OpenRouter's /embeddings endpoint directly (langchain's chat client
    doesn't cover embeddings). Returns one vector per input, in order.
    """
    import httpx
    from pipeline import usage
    usage.record("embed_call")

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


async def chat(system_prompt: str, user_prompt: str, model: str = "fast") -> str:
    """
    Simple async chat call. Returns the text response.
    `model` picks the tier: "fast" | "smart" | "post".
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from pipeline import usage
    usage.record("llm_call")

    response = await _get_llm(model).ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ])
    return response.content


async def chat_json(system_prompt: str, user_prompt: str, model: str = "fast") -> dict:
    """
    Async chat call that expects a JSON response.
    Strips markdown fences and parses.
    """
    raw = await chat(system_prompt, user_prompt, model=model)
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