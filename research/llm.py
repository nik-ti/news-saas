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

# kind -> (model id, temperature, timeout seconds, max tokens)
_LLM_SPECS = {
    "fast": (config.LLM_MODEL_FAST, config.LLM_TEMPERATURE, 30, None),
    "smart": (config.LLM_MODEL_SMART, config.LLM_TEMPERATURE, 60, None),
    # Explicit token cap on the post writer: a cut completion is deterministic
    # (finish_reason="length") instead of provider-dependent luck.
    "post": (config.LLM_MODEL_POST, 0.2, 30, 1024),
}
_llms: dict[str, ChatOpenAI] = {}


def _get_llm(kind: str) -> ChatOpenAI:
    if kind not in _llms:
        model, temperature, timeout, max_tokens = _LLM_SPECS[kind]
        kwargs = dict(
            model=model,
            openai_api_key=config.OPENROUTER_API_KEY,
            openai_api_base=config.OPENROUTER_BASE_URL,
            temperature=temperature,
            timeout=timeout,
            max_retries=2,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        _llms[kind] = ChatOpenAI(**kwargs)
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


# finish_reason values meaning the provider cut the completion off. LangChain
# returns the partial text WITHOUT raising, so a truncated post would be sent
# mid-sentence unless we check this ourselves.
_TRUNCATING_FINISH_REASONS = {"length", "max_tokens"}


def _finish_reason(response) -> str:
    """
    finish_reason as a lowercase string. Providers disagree on the shape:
    usually a plain string in response_metadata (OpenAI-compatible), but some
    nest a dict (e.g. {"finish_reason": "length"}). Handle both.
    """
    meta = getattr(response, "response_metadata", None) or {}
    reason = meta.get("finish_reason")
    if isinstance(reason, dict):
        reason = reason.get("finish_reason") or reason.get("reason") or ""
    return str(reason or "").lower()


async def chat(system_prompt: str, user_prompt: str, model: str = "fast") -> str:
    """
    Simple async chat call. Returns the text response.
    `model` picks the tier: "fast" | "smart" | "post".
    A truncated completion (finish_reason "length"/"max_tokens") is retried
    once, then raises so the caller's error path runs instead of shipping
    partial text.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from pipeline import usage
    usage.record("llm_call")

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    reason = ""
    for attempt in (1, 2):
        response = await _get_llm(model).ainvoke(messages)
        reason = _finish_reason(response)
        if reason not in _TRUNCATING_FINISH_REASONS:
            return response.content
        logger.warning("LLM '%s' completion truncated (finish_reason=%s, "
                       "attempt %d/2)", model, reason, attempt)
    raise RuntimeError(
        f"LLM '{model}' completion truncated twice (finish_reason={reason})")


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