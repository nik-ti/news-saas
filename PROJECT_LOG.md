# 📋 Project Log — News SaaS MVP

> What's been done. Updated as work progresses.

---

## Concept

Self-serve news service: user describes a topic → AI research engine finds the best sources → pipeline monitors and delivers a personalised news feed via Telegram.

---

## What's Built

### Intake (understands the user)
- Single natural interview loop — no fixed form, no visible scaffolding
- Interviewer LLM carries a research protocol: beat + angle, what counts as a hit, hard exclusions
- Asks only what is still unclear *and* would change source selection; hard cap of 4 answers
- Produces the Source Criteria Profile **and** a bespoke relevance rubric written from the user's own words

### Research Engine (finds sources)
- 4-phase pipeline: profile builder → Brave Search discovery → 2-stage qualification → fetch validation
- LLM: DeepSeek V4 Flash via OpenRouter
- Crawler: crawl4ai (headless Chromium, memory-saving mode)
- Deterministic URL heuristics prevent article URLs from being stored as sources
- Domain-level dedup ensures no duplicate sources
- Feed URL identification — the LLM's guess is *verified*, then repaired by the deterministic finder
- Internal source DB — sources found for one user benefit future users

### News Page Discovery (`research/feed_finder.py`)
- Crawler-driven and language-agnostic; no path guessing
- RSS/Atom autodiscovery first (the site declaring its own feed)
- Otherwise: crawl the homepage, infer sections from where article-shaped URLs actually live
- Every candidate proved by counting article links with the same filter the poller uses
- Polite: sequential verification, 1s delay, one retry on a bot challenge, gives up on hostile hosts
- Never re-crawls the homepage it already fetched

### News Cycle (one cron, every 30 min)
- Phase A — snapshot each source; **a source's first poll is a silent baseline** (records everything, sends nothing)
- Phase B — summarize → binary relevance gate (per-stream rubric) → write post → send to the stream's owner
- Caps: 3 new articles per source per cycle, 10 posts per cycle globally
- 3-strategy article extraction: RSS feeds → link extraction → LLM inline extraction
- Transient failures (LLM outage, network) retry up to 3 times; permanent ones (Telegram 400/403) are terminal
- Computed summaries are persisted, so a retry costs one LLM call, not a re-crawl
- Overlap guard: a slow cycle causes the next tick to skip
- Circuit breaker: if most sources fail in one cycle (dead browser, outage), nobody's
  strike counter is charged and the crawler is reset
- Flood guard: a known source whose page is suddenly ~all-new (redesign) is silently
  re-baselined instead of spamming stale articles
- Health check: re-tests **errored and blocked** sources every 24 hours, RSS-aware

### Telegram Bot
- `/newstream` — natural interview → AI research → sources found
- `/streams`, `/sources`, `/sources_all` — view streams and sources
- `/addsource` — discovers the site's news page(s); asks if there are several
- `/deletesource`, `/testsource` — manual source management (ownership-checked)
- `/research` — re-run research for a stream, regenerating profile + rubric (ownership-checked)
- `/latest` — show the caller's latest articles (tenant-scoped)
- `/runpipeline` — manually run the same news cycle the cron runs
- `/status` — system stats, including sources awaiting baseline
- Authorization: `/sources_all`, `/runpipeline`, `/status`, `/testsource` are
  admin-only (`ADMIN_USER_ID`); every stream-taking command checks ownership
- Inline keyboards wired through a `CallbackQueryHandler`
- `sendRichMessage` for bot chrome; plain `sendMessage` + sanitized Telegram HTML for news posts

### Database
- SQLite: streams, sources (feed_url, fail_count, baselined_at), articles (status, attempts, posted_at)
- Article states: `new` | `seen` (baseline) | `posted` | `irrelevant` | `unusable` |
  `dropped` | `send_failed` — every article ends terminal, and the status says *why*
- Dedup is **per stream** (overlapping streams each receive an article) and enforced
  at the DB level: `UNIQUE(source_id, content_hash)` + a `content_hash` index
- Auto-migrations for schema changes (sources and articles)

### Tests
- 74 offline tests in `tests/` (`python3 -m pytest tests/ -q`, ~7 s)
- No network, no browser: temp SQLite per test, external calls stubbed
- Regression-proven: the suite was run against the pre-fix code and failed on
  exactly the bugs it targets (17 behavioral failures), then passed on the fixes

---

## Timeline

### Jul 2, 2026 — Initial Build
- Built entire research engine, pipeline, Telegram bot, scheduler
- Tested with 3 topics: broad crypto (3 sources), EU DeFi regulation (6 sources), geopolitics (8 sources)
- Fixed bugs: wrong LLM model name, broken BraveSearchWrapper API, stale bot processes

### Jul 4, 2026 — Hardening
- Switched LLM to DeepSeek V4 Flash
- Added domain-level dedup to prevent duplicate sources
- Added feed_url identification (finds the correct article-list page)
- Fixed NoneType crashes, empty-list access errors
- Added URL utilities module (`urlutils.py`) with deterministic article/section detection
- Fixed discovery to collapse article URLs to publication URLs before qualification
- Added RSS feed parsing, link plausibility filtering, LLM inline-item extraction
- Added failure tolerance (3 consecutive fails before deactivation)
- Fixed validation to test feed_url (not homepage)
- Fixed DB migrations (ALTER before CREATE crash on fresh DB)

### Jul 7, 2026 — Real-Time Posting
- Added post_writer pipeline (Gemini Flash LLM writes short news posts from article content)
- Added stream_poster orchestrator (fetch → write post → send to Telegram immediately)
- Added `cron_stream_post` job running every 15 minutes
- Removed batch digest notifications from fetch cron
- Added `LLM_MODEL_POST` config for separate post-writing model

### Jul 9–10, 2026 — Deployment, and the Great De-Spamming

**Deployed to the server.** Runs under `systemd` (`test-news-saas.service`) in webhook mode
behind nginx at `bot.simple-flow.co/test-news-saas` → `127.0.0.1:3010`.

**Fixed: research found no sources.** Playwright's Chromium was never installed — discovery
found 39 candidates and the crawler fetched 0 of 39 homepages, so qualification had nothing to
read. The crawler singleton also cached its own half-started instance, so it could never recover.

**Fixed: 268 spam messages.** Three compounding bugs:
- No first-run baseline — every link on a new source's page counted as "new".
- `cron_stream_post` (15 min) raced ahead of `cron_process_articles` (60 min) and won, so every
  posted article had `relevance_score = 0.0`. The relevance check was never consulted.
- `MAX_ARTICLES_PER_FETCH = 15` per source with no global ceiling.

**Rebuilt the delivery path.** Removed the legacy digest (`deliver.py`) and the racing poster
(`stream_poster.py`); consolidated four crons into one `pipeline/news_cycle.py`. Added the silent
baseline, the per-stream relevance gate, per-source and global caps, and owner-routed delivery.

**Rewrote intake.** Replaced the fixed `Q1/Q2/Q3` form and its generic LLM follow-ups with a single
natural interview driven by a real research protocol. Removed the visible
"🧠 Generating follow-up questions…" scaffolding in favour of a typing indicator.

**Rewrote post formatting.** `write_post()` was being fed raw crawled page HTML; it now reads the
summary. Prompt ported from `re_news_channel` (English), with its factual-accuracy rules intact.
News posts now go out via `sendMessage` + `parse_mode=HTML` with a `sanitize_telegram_html()` pass.

**Added deterministic news-page discovery.** `/addsource` now finds the page that actually lists a
site's articles, by crawling rather than guessing paths. Verified on `coindesk.com` (homepage, 33),
`anthropic.com` (`/policy` 25, `/research` 18, `/news` 15), `tagesschau.de` (RSS, German), and
`example.com` (correctly nothing).

**Fixed: "deleting sources doesn't work".** The SQL was always fine. `/sources` showed a row number
where `/deletesource` expected the database id, and every inline button was inert because no
`CallbackQueryHandler` was registered. Added an ownership check while there.

**Other fixes:** `reset_fail_count()` no longer resurrects `blocked` sources; articles can no
longer be retried forever, nor silently discarded by a transient LLM outage; `fetch_page()` no
longer returns `title=None`; streams stranded in `researching` by a restart are reconciled on boot;
speculative feed probing no longer trips Cloudflare and locks the crawler out.

### Jul 10, 2026 (later) — Semantic source DB, aggregators, post length, stealth

- **Semantic internal source DB.** Every qualified source is embedded
  (OpenRouter `text-embedding-3-small`, 1536-dim, stored as a BLOB). New research
  does a cosine-similarity lookup of the internal DB first, so "EU crypto
  regulation" reuses a source tagged "European digital-asset law" — no shared
  words needed. Matches are seeds; they still re-qualify for the new user.
  (`research/embeddings.py`.)
- **Google News aggregator feeds** per stream (`research/aggregators.py`),
  handled as headline items (their links are redirects, not articles).
- **Richer source metadata**: `site_type`, meaningful descriptions, stored
  `fetch_method` so the poller doesn't re-guess each cycle.
- **Post length** per stream (Standard ~100 words / Compact) via `/postsize`.
- **Crawler stealth mode** (`enable_stealth=True`, random UA) to get past bot
  walls — openai.com now returns real content instead of a Cloudflare block.
- **Research reporting** no longer says "found nothing" when sources were stored
  but temporarily unreachable; validation retries rate-limited hosts.

### Jul 11, 2026 — Full audit + hardening (phases 1–2)

A single-pass technical audit of the whole codebase, then two implementation phases on
branch `claude/news-saas-audit-ib1yqa` (5 + 3 commits, plus docs). Everything verified
by a new 74-test offline suite, including a regression proof against the pre-fix code.

**Phase 1 — all 14 critical issues + a startup self-check:**
- **Per-stream article dedup.** Dedup was global, so an article shared by two streams
  was delivered to only one — a hard multi-tenant blocker. Now scoped per stream, with
  `UNIQUE(source_id, content_hash)` + a hash index at the DB level.
- **Crawler lifecycle.** A crashed headless browser stayed cached forever: every fetch
  failed, every source got blamed, and the whole system mass-deactivated within ~90 min
  (the health check used the same dead browser, so nothing recovered). Now: dead-browser
  detection + reset, a >50%-failures circuit breaker, a shutdown hook, crawl
  concurrency 15 → 8.
- **Command authorization.** Any Telegram user could read every tenant's data and
  trigger paid research runs. Added `ADMIN_USER_ID` gate + ownership checks + scoped `/latest`.
- **Telegram HTML safety.** `<br>`/bad truncation/unescaped URLs caused 400s that
  permanently dropped articles. Valid tag whitelist, tag-safe truncation, escaped links.
- **Silent failures made loud.** Total Brave Search failure (dead key) was
  indistinguishable from "no results"; proven-RSS feeds falling back to browser+LLM
  produced hallucinated posts from raw XML; LLM `null` fields crashed whole research
  runs. All fixed; plus a 🩺 startup self-check that pings both LLMs, embeddings, and
  Brave at boot and messages the admin about anything dead.
- Also: feed repair now runs *before* validation (good sources were being wrongly
  discarded), flood/re-baseline guard, WordPress `?p=` permalink hashing, Google News
  dup guard on re-research, intake conversation preserved for `/research`, RSS-aware
  health check, `feed:force` dup guard.

**Phase 2 — code quality (audit §2, minus the summarize+gate merge):**
- **Real summaries for RSS items.** RSS teaser descriptions no longer masquerade as
  summaries — thin ones trigger a real article fetch; teasers remain a graceful
  fallback for paywalls/fetch failures. Computed summaries are persisted (cheap retries).
- **Distinct terminal statuses** (`dropped`/`unusable`/`send_failed`) replace the
  four-way overload of `seen`.
- **Refactors:** store.py around one `db()` context manager (~25 copies of
  connect/commit/close removed), one shared Telegram transport (`_api` + reused
  AsyncClient), one model-keyed LLM client (`chat(..., model="fast"|"smart"|"post")`),
  Stage-1 prefilter chunks parallelized.
- **Pruning:** dead `bot/keyboards.py`, dead store functions, unused deps
  (`langgraph`, `langchain-community`, `rank-bm25`, `langchain` meta); `numpy` added
  (was imported but never declared).

**Docs:** `SUGGESTIONS.md` added — the full remaining roadmap (schema split to shared
sources, worker split, semantic story dedup, usage limits, Firecrawl ladder, quiet
hours, feedback loop, CI) written so any future session can execute it.

**Operator notes:** set `ADMIN_USER_ID` in `.env` if the alert chat isn't your personal
account; pin `crawl4ai` to the server's deployed version; watch the 🩺 self-check
message on first boot (it will reveal whether the embeddings endpoint actually works).

---

## Tech Stack

| Component | Technology |
|---|---|
| Bot | python-telegram-bot 21.10 |
| LLM (research) | DeepSeek V4 Flash via OpenRouter |
| LLM (post writing) | Gemini 2.5 Flash via OpenRouter |
| Search | Brave Search API |
| Crawler | crawl4ai (headless Chromium) |
| Database | SQLite |
| Scheduler | PTB JobQueue (APScheduler) |

---

## How to Run

Deployed on the server as a systemd unit, in webhook mode:

```bash
systemctl status  test-news-saas.service
systemctl restart test-news-saas.service
journalctl -u     test-news-saas.service -f
```

First-time setup:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # crawl4ai needs the browser
cp .env.example .env                    # then fill in the four keys
python main.py
```

The webhook is registered automatically on boot to `$WEBHOOK_HOST$WEBHOOK_PATH`
(default `https://bot.simple-flow.co/test-news-saas`), served by nginx on `127.0.0.1:3010`.

Run the test suite (offline — no keys, no browser needed):

```bash
python3 -m pytest tests/ -q
```