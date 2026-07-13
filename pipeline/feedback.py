"""
Feedback loop (§3.7) — nightly source score decay.

quality_score was write-once at research time; the gate's precision was
unmeasurable. Every post now carries 👍/👎 buttons (stored per delivery), and
this job folds each (stream, source) pair's observed performance — gate
pass-rate and thumb ratio — back into its subscription quality_score.

The fold is deliberately gentle (EMA): one bad day doesn't kill a source, but
a source that qualified well and then produces 90% `irrelevant` slides down
week after week. Sources that slide badly are REPORTED to the admin, not
auto-dropped — a human look costs nothing at this scale.
"""
import logging

import config
from database import store

logger = logging.getLogger(__name__)

MIN_OUTCOMES = 5        # don't judge a source on fewer gate decisions
KEEP_RATIO = 0.7        # how much of the old score survives each nightly fold
THUMB_WEIGHT = 0.4      # thumbs count this much of the observed component
REPORT_BELOW = 25       # scores that fold below this get reported


async def run_score_decay() -> list[dict]:
    """Fold the last 30 days of outcomes into quality scores.

    Returns the list of flagged (very low scoring) pairs for the caller to
    report."""
    stats = store.stream_source_stats(days=30)
    flagged = []

    for row in stats:
        posted = row["posted"] or 0
        irrelevant = row["irrelevant"] or 0
        outcomes = posted + irrelevant
        if outcomes < MIN_OUTCOMES:
            continue

        pass_rate = posted / outcomes
        ups, downs = row["ups"] or 0, row["downs"] or 0
        if ups + downs > 0:
            thumb_ratio = ups / (ups + downs)
            observed = (1 - THUMB_WEIGHT) * pass_rate + THUMB_WEIGHT * thumb_ratio
        else:
            observed = pass_rate

        srcs = store.get_sources_by_stream(row["stream_id"])
        current = next((s.get("quality_score", 0) for s in srcs
                        if s["id"] == row["source_id"]), None)
        if current is None:
            continue

        new_score = round(KEEP_RATIO * current + (1 - KEEP_RATIO) * observed * 100)
        new_score = max(0, min(100, new_score))
        if new_score != current:
            store.set_subscription_score(row["stream_id"], row["source_id"],
                                         new_score)
            logger.info(
                "Score fold: stream %d source %d %d→%d "
                "(pass %.0f%%, %d👍/%d👎 over %d outcomes)",
                row["stream_id"], row["source_id"], current, new_score,
                pass_rate * 100, ups, downs, outcomes,
            )

        if new_score < REPORT_BELOW and outcomes >= 4 * MIN_OUTCOMES:
            flagged.append({**row, "score": new_score, "pass_rate": pass_rate})

    return flagged


async def cron_score_decay(context=None) -> None:
    """Nightly job: fold scores; tell the admin about sources worth dropping."""
    from bot.messaging import send_rich_async

    try:
        flagged = await run_score_decay()
    except Exception:
        logger.exception("Score decay job failed")
        return

    if not flagged:
        return
    lines = ["# 📉 Sources performing badly", "",
             "These qualified well but almost everything they yield is gated "
             "out. Worth a look (`/deletesource <id>`):", ""]
    for f in flagged[:15]:
        lines.append(f"- source `{f['source_id']}` on stream `{f['stream_id']}` — "
                     f"score {f['score']}, gate pass {f['pass_rate']:.0%}")
    try:
        await send_rich_async(config.ADMIN_USER_ID, "\n".join(lines))
    except Exception:
        logger.exception("Admin report failed for score decay")
