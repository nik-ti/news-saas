"""
Usage accounting (§3.3).

Attributes expensive operations (LLM calls, crawls, embeddings, research runs)
to the tenant who caused them. The call sites — research/llm.py and
crawler/fetcher.py — don't know about Telegram users, so attribution flows
through a contextvar set at the entry points (research runs, /addsource).
Anything not attributed lands on user 0 (system: news cycle, health check).

Recording is fire-and-forget and must NEVER break the operation it measures.
"""
import contextvars
import logging

logger = logging.getLogger(__name__)

_current_user: contextvars.ContextVar[int] = contextvars.ContextVar(
    "usage_user", default=0
)

SYSTEM_USER = 0


def set_user(user_id: int) -> contextvars.Token:
    """Attribute subsequent usage in this context to a user. Returns a token
    for reset_user()."""
    return _current_user.set(user_id)


def reset_user(token: contextvars.Token) -> None:
    _current_user.reset(token)


def current_user() -> int:
    return _current_user.get()


def record(kind: str, n: int = 1) -> None:
    """Count n units of `kind` against the current context's user."""
    try:
        from database import store
        store.increment_usage(_current_user.get(), kind, n)
    except Exception:
        # Accounting must never take down the operation it measures.
        logger.debug("usage record failed for kind=%s", kind, exc_info=True)


def usage_today(user_id: int, kind: str) -> int:
    from database import store
    return store.get_usage(user_id, kind)
