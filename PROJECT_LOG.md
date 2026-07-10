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
- Overlap guard: a slow cycle causes the next tick to skip
- Health check: re-tests **errored** sources every 24 hours (never revives `blocked` ones)

### Telegram Bot
- `/newstream` — natural interview → AI research → sources found
- `/streams`, `/sources`, `/sources_all` — view streams and sources
- `/addsource` — discovers the site's news page(s); asks if there are several
- `/deletesource`, `/testsource` — manual source management (ownership-checked)
- `/research` — re-run research for a stream, regenerating profile + rubric
- `/latest` — show latest articles
- `/runpipeline` — manually run the same news cycle the cron runs
- `/status` — system stats, including sources awaiting baseline
- Inline keyboards wired through a `CallbackQueryHandler`
- `sendRichMessage` for bot chrome; plain `sendMessage` + sanitized Telegram HTML for news posts

### Database
- SQLite: streams, sources (feed_url, fail_count, baselined_at), articles (status, attempts, posted_at)
- Article states: `seen` | `new` | `posted` | `irrelevant` — every article ends terminal
- Auto-migrations for schema changes (sources and articles)

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