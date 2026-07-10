"""
Semantic memory for the internal source database.

Every source we qualify is a small, hard-won asset: we crawled it, judged it, and
found its news page. The point of the internal DB is that the NEXT person asking
about a related topic starts warm instead of rediscovering it from scratch.

The old lookup matched on an exact broad_category string plus literal keyword
overlap, so "EU crypto regulation" never found a source tagged "European
digital-asset law" — same meaning, zero shared words. Embeddings fix that: each
source gets a vector fingerprint of its meaning, and a new query finds the
sources nearest to it in meaning-space, words be damned.

Vectors come from OpenRouter's embeddings endpoint (no extra dependency, no model
download). Similarity is brute-force cosine in numpy — fine for thousands of
sources; revisit only if the DB grows into the tens of thousands.
"""
import logging

import numpy as np

import config
from research.llm import _openrouter_embeddings

logger = logging.getLogger(__name__)

EMBED_MODEL = "openai/text-embedding-3-small"   # 1536-dim, cheap, strong
EMBED_DIM = 1536
SIMILARITY_THRESHOLD = 0.45   # below this, a "match" is just noise
DTYPE = np.float32


def source_text(src: dict) -> str:
    """The text we embed for a source — everything that describes what it covers."""
    parts = [
        src.get("name") or "",
        src.get("broad_category") or "",
        src.get("site_type") or "",
        " ".join(src.get("specific_keywords") or []),
        src.get("description") or "",
    ]
    return " — ".join(p for p in parts if p).strip()


def profile_text(profile: dict) -> str:
    """The text we embed for a research query."""
    parts = [
        profile.get("broad_domain") or "",
        " ".join(profile.get("specific_topics") or []),
        " ".join(profile.get("keywords") or []),
        profile.get("description") or "",
    ]
    return " — ".join(p for p in parts if p).strip()


async def embed(text: str) -> list[float] | None:
    """Embed one string. Returns None on failure (callers degrade gracefully)."""
    if not text.strip():
        return None
    vecs = await embed_batch([text])
    return vecs[0] if vecs else None


async def embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Embed many strings in one request. Returns None on failure."""
    clean = [t if t.strip() else " " for t in texts]
    try:
        return await _openrouter_embeddings(EMBED_MODEL, clean)
    except Exception as e:
        logger.error("Embedding request failed: %s", e)
        return None


def to_blob(vec: list[float]) -> bytes:
    return np.asarray(vec, dtype=DTYPE).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=DTYPE)


def cosine_top(query: list[float], candidates: list[tuple[int, bytes]],
               top_k: int, threshold: float) -> list[tuple[int, float]]:
    """
    Return (row_id, score) for the top_k candidates most similar to `query`,
    above `threshold`. `candidates` is [(row_id, embedding_blob), ...].
    """
    if not candidates:
        return []
    q = np.asarray(query, dtype=DTYPE)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    q = q / qn

    mat = np.stack([from_blob(b) for _, b in candidates])
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1e-9
    sims = (mat @ q) / norms

    order = np.argsort(-sims)
    out = []
    for idx in order[:top_k]:
        score = float(sims[idx])
        if score < threshold:
            break
        out.append((candidates[idx][0], score))
    return out


# ── The two entry points the engine uses ──────────────────────────────────────

async def backfill_stream_embeddings(stream_id: int) -> int:
    """
    Embed every source in this stream that doesn't have a vector yet, so it
    becomes findable by future research. Returns how many were embedded.
    """
    from database import store

    pending = store.sources_missing_embedding(stream_id)
    if not pending:
        return 0

    vecs = await embed_batch([source_text(s) for s in pending])
    if not vecs:
        return 0

    n = 0
    for src, vec in zip(pending, vecs):
        if vec:
            store.set_source_embedding(src["id"], to_blob(vec))
            n += 1
    logger.info("Embedded %d source(s) for stream %d", n, stream_id)
    return n


async def find_internal_semantic(profile: dict, exclude_stream_id: int = None,
                                  top_k: int = 8) -> list[dict]:
    """
    Semantic search of the internal source DB for a new research query.

    Returns source dicts (url, feed_url, name, …) ranked by similarity, each
    tagged with a `similarity` score. These are seeds — they still go through
    qualification against THIS user's profile before being kept.
    """
    from database import store

    pool = store.get_embedded_sources(exclude_stream_id=exclude_stream_id)
    if not pool:
        return []

    query_vec = await embed(profile_text(profile))
    if not query_vec:
        return []

    ranked = cosine_top(
        query_vec,
        [(i, s["embedding"]) for i, s in enumerate(pool)],
        top_k=top_k,
        threshold=SIMILARITY_THRESHOLD,
    )

    matches = []
    for idx, score in ranked:
        src = dict(pool[idx])
        src.pop("embedding", None)      # don't ship the raw vector around
        src["similarity"] = round(score, 3)
        matches.append(src)

    if matches:
        logger.info("Internal DB: %d semantic match(es) (top %.2f)",
                    len(matches), matches[0]["similarity"])
    return matches
