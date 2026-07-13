# Suggestions & Roadmap

Status after the July 13, 2026 implementation pass, which executed nearly the whole
roadmap that used to live in this file (see `PROJECT_LOG.md` for the full change
log). Only the genuinely remaining items are kept below, written so a future
session (human or AI) can pick any item and execute without re-deriving context.
Effort guesses are S (<half day), M (half–2 days), L (2+ days).

---

## 0. What's already done (context for this doc)

Everything from the July 2026 audit and its follow-ups EXCEPT the items in §1–§3
below. Highlights, all verified by the 118-test offline suite
(`python3 -m pytest tests/ -q`):

| Area | What shipped |
|---|---|
| **Schema v2 (the keystone)** | Canonical tenant-free `sources` (UNIQUE feed_url), `stream_sources` subscriptions (per-stream quality_score), `articles` without delivery state, per-(article, stream) `deliveries` with `post_html` + `verdict`. One crawl per distinct source per cycle, however many streams follow it. In-place v1 migration at boot; old tables kept as `*_v1`. |
| Data integrity | Per-stream dedup on the new schema, batched Phase-A inserts, nightly 30-day retention (posted/queued rows kept for provenance) |
| Delivery | Per-stream post budget (5/stream, 30 global), quiet hours (`/quiet`), story-level semantic dedup (0.85 cosine over the stream's last 72 h), 👍/👎 feedback buttons, exact sent post persisted |
| Lifecycle | `/pausestream` `/resumestream` `/deletestream`; paused streams' sources not crawled; auto-pause after 3 consecutive terminal send failures |
| Economics | RSS conditional GET (ETag/If-Modified-Since → 304), polling tiers from the qualifier's judged frequency, internal-DB matches ≥0.75 similarity skip Stage-1 qualification |
| Accounting | `usage` table + contextvar attribution of every LLM call/crawl/embed; caps: 3 research runs/day, 5 streams/user, 15 sources/stream |
| Quality | Research reconciliation (post-research snapshot of every stored source, honest live/pending report, all-aggregator warning), nightly quality-score fold from gate pass-rate + thumbs |
| Hardening | Untrusted-content clauses in all content-facing prompts, language field wired into the post writer, PicklePersistence for interviews, crawl4ai pinned (0.9.1), GitHub Actions CI |

Deliberately NOT done:

* **Summarize + relevance-gate merge (old §1.1)** — operator decision (July 13):
  keep them as two calls. Do not re-propose without asking.

---

## 1. Architecture work remaining

### 1.1 Split the pipeline out of the bot process (old §2.2) — M

Bot + crawler + LLM pipeline still share one event loop and one Chromium. A
research run (2–5 min, 60+ crawls) contends with the news cycle for crawl slots,
and heavy Phase A work delays webhook handling. A worker process reading the same
WAL-mode SQLite (or a simple job table) is enough — no queues/microservices
needed yet. This also absorbs the `_post_phase` serialization issue (§3.1 below).
Deployment note: this adds a second systemd unit — coordinate with the operator.

### 1.2 Premium / Firecrawl fetch ladder (old §3.4) — M/L

Blocked on an operator decision + a Firecrawl account/API key. "Blocked" becomes
a ladder, not a state: per-fetch attempt record (method tried, outcome, cost),
escalation `httpx → crawl4ai → Firecrawl`, entitlement check per stream/user
before rung 3, per-user Firecrawl budget. `fetch_method` + `fail_count` +
the new `usage` table are the right seeds; add a `fetch_attempts` log table.
Note Firecrawl solves *fetching*, not *baselining* — a source revived later
baselines late, and the user should be told "this source starts tomorrow".

---

## 2. Product polish backlog (all S/M, none blocking)

* **Digest mode** — quiet hours now hold posts; an optional "batch at 08:00"
  digest (one message summarising held posts) is the natural premium/free axis.
* **Auto-drop chronically bad sources** — the nightly score fold currently
  *reports* sources with terrible gate pass-rates to the admin; once trust is
  established, auto-unsubscribe below a score floor after N weeks.
* **Delivery preferences** — tone/format/headline-vs-full choices promised in
  OVERVIEW.md; today every post uses one house style (see TODO.md — the
  operator's list).
* **Timezone-aware quiet hours** — quiet hours are server-time today; ask for
  the user's timezone in the interview (or infer from Telegram locale).
* **Usage dashboards** — the `usage` table accumulates per-tenant LLM/crawl
  counts; an admin `/usage` command summarising cost per user is one query.

---

## 3. Documented tradeoffs (accepted, watch)

### 3.1 `_post_phase` is serial with a 2 s sleep per send

Fine at ≤30 posts/cycle. It becomes a latency wall as streams grow; fold into
the worker split (§1.1) rather than optimizing in place. Telegram's real limits
are ~30 msg/s global and ~1 msg/s per chat.

### 3.2 Flood-guard tradeoff

A source baselined while (nearly) empty that later publishes 8+ items at once is
silently re-baselined instead of posted (`REBASELINE_MIN_ITEMS/FRACTION`).
Correct for redesigns, wrong for genuine burst publishers. If a real source hits
it, add a per-source override.

### 3.3 Retention vs. re-listed old articles

The nightly prune deletes baseline/negative-outcome articles older than 30 days.
If a source re-lists an item older than that (a pinned evergreen link), it can
reappear as "new" — the flood guard catches the mass case; a single pinned item
may slip through once. Accepted.

### 3.4 Semantic dedup calls the embeddings endpoint once per candidate

~10–30 embed calls per cycle at current caps, attributed in `usage`. If the
endpoint degrades, dedup silently no-ops (fails open) — the self-check reports
endpoint health at boot.

---

## 4. Open questions for the operator

1. **Worker split (§1.1)**: green-light the second systemd unit?
2. **Firecrawl (§1.2)**: do we want the paid rung at all? Needs an account.
3. **ADMIN_USER_ID**: still defaults to `TELEGRAM_CHAT_ID`. If the alert chat
   isn't your personal account, set `ADMIN_USER_ID` in `.env` before sharing
   the bot.
4. **v1 tables**: `sources_v1`/`articles_v1` are kept for one release. Drop them
   once the v2 deploy has survived a week or two:
   `sqlite3 data/news.db "DROP TABLE sources_v1; DROP TABLE articles_v1;"`
   (a pre-migration backup exists at `data/news.db.pre-v2-backup`).
