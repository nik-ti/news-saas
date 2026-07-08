# 📋 Project Log — News SaaS MVP

> What's been done. Updated as work progresses.

---

## Concept

Self-serve news service: user describes a topic → AI research engine finds the best sources → pipeline monitors and delivers a personalised news feed via Telegram.

---

## What's Built

### Research Engine (finds sources)
- 4-phase pipeline: profile builder → Brave Search discovery → 2-stage qualification → fetch validation
- LLM: DeepSeek V4 Flash via OpenRouter
- Crawler: crawl4ai (headless Chromium, memory-saving mode)
- Deterministic URL heuristics prevent article URLs from being stored as sources
- Domain-level dedup ensures no duplicate sources
- Feed URL identification — finds the correct article-list page for each source
- Internal source DB — sources found for one user benefit future users

### News Pipeline (monitors sources)
- **Real-time posting** (every 15 min): fetch new articles → write short post via Gemini Flash → send immediately to Telegram
- 3-strategy article extraction: RSS feeds → link extraction → LLM inline extraction
- Failure tolerance: sources deactivated only after 3 consecutive fetch failures
- Health check: re-tests blocked sources every 24 hours
- Article dedup via normalized URL hashing

### Telegram Bot
- `/newstream` — guided Q&A → AI research → sources found
- `/streams`, `/sources`, `/sources_all` — view streams and sources
- `/addsource`, `/deletesource`, `/testsource` — manual source management
- `/research` — re-run research for a stream
- `/latest` — show latest articles
- `/runpipeline` — manual trigger
- `/status` — system stats
- Rich messages throughout (tables, headings, links via `sendRichMessage`)

### Database
- SQLite: streams, sources (with feed_url, fail_count), articles
- Auto-migrations for schema changes

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

```bash
cd /Users/nikti/Documents/News\ SaaS
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 main.py
```

Note: must use Framework Python (not Homebrew) — dependencies are installed there.