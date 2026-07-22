# Implementation Plan — news-saas

Date: 2026-07-22
Status: approved for implementation, not yet started

Three workstreams, in the order they should ship:

- **Part 0** — fix the truncated-post bug (found while investigating the broken NIGHT-token article)
- **Part 1** — paced sending (kills the "big batch" feeling)
- **Part 2** — staggered polling (spreads discovery and crawler load)
- **Part 3** — natural-language stream tuning (user refines accept/reject rules via bot chat, with a topic guardian)

Verification for every part: `python3 -m pytest tests/ -q` (asyncio_mode=auto, offline, `temp_db` fixture in `tests/conftest.py`), then restart the systemd service `test-news-saas.service` and observe one live cycle.

---

## Part 0 — Truncated-post bug fix

### Root cause (confirmed against the DB)

The broken post (`Midnight's NIGHT token recovers after Wanchain bridge hack … "The NIGHT token, associated with Midnight, saw"`) is article `id=3185` in `data/news.db`, delivery posted 2026-07-22 12:05:46 to stream 13.

- The article's stored `summary` is complete and coherent (~350 chars). No pipeline stage truncates it: `SUMMARY_CHAR_CAP`/`POST_INPUT_CHAR_CAP` slices, `_strip_preamble`, and `_safe_truncate` were all ruled out — the message is ~200 chars.
- The truncation happened **inside the LLM completion itself**: `write_post()` uses model tier `"post"` (`google/gemini-2.5-flash` via OpenRouter). `research/llm.py:73-77` (`chat()`) returns `response.content` **without inspecting `finish_reason`**. When the provider cuts the completion (`finish_reason: "length"`), LangChain silently returns partial text — no exception, so the retry path never fires.
- The only existing guard is `pipeline/news_cycle.py:419` (`len(post_html) < 20`), a length floor, not a completeness check. The truncated post passes it.
- Systematic exposure, edge-case frequency: only this one delivery in the DB is genuinely cut mid-sentence, but there is zero defense, so it will recur randomly.

### Changes

1. `research/llm.py` — in `chat()` after `ainvoke`: read `response.response_metadata.get("finish_reason")`. If it is `"length"` or `"max_tokens"`, retry once; on second truncated completion, raise so the caller's `except` path runs (`write_post` returns `""` and `news_cycle.py:419-422` retries the article next cycle instead of posting garbage).
2. `pipeline/post_writer.py` (after the post body is built, ~line 192) — belt-and-braces completeness check: if the body (before the source link is appended) does not end with sentence-ending punctuation (`. ! ? " ” ) >`), return `""` exactly like the empty-completion case. Same retry path handles it.
3. Optionally set an explicit `max_tokens` (e.g. 1024) in the `"post"` spec in `research/llm.py:19-23` so truncation is deterministic rather than provider-dependent.

### Tests

- New `tests/test_post_writer_truncation.py`:
  - fake `chat()` raising/returning metadata with `finish_reason="length"` → `write_post` returns `""`, delivery left for retry.
  - body ending mid-sentence → returns `""`; body ending with `.`/`!`/`>` (HTML tag) → accepted.
- `research/llm.py` retry logic: first call truncated, second complete → returns text; both truncated → raises.

---

## Part 1 — Paced sending

Today `_post_phase()` (`pipeline/news_cycle.py:349-450`) drains the queue immediately after polling in the same tick: up to `MAX_POSTS_PER_STREAM_PER_CYCLE = 5` posts per stream, back to back, `asyncio.sleep(2)` apart (`news_cycle.py:434`). Result: 30 min of silence, then a clump.

Goal: send phase becomes its own job on a 5-minute tick, max 2 posts per stream per tick. Same news, trickling in like a live feed. Worst case the 3rd article of a clump arrives ~10-15 min later.

### Changes

1. `config.py`:
   - Add `SEND_TICK_MINUTES = 5`.
   - Add `MAX_POSTS_PER_STREAM_PER_TICK = 2` and `MAX_POSTS_PER_TICK = 5` (re-derived from the old per-30-min caps of 5/stream and 30 global ÷ 6 ticks). Keep the old keys for one release or rename with a comment; all usages are in `news_cycle.py` and tests.
2. `pipeline/news_cycle.py`:
   - `run_news_cycle()` (line 37-56): remove the `_post_phase()` call — poll phase only.
   - `_post_phase()` reads the new per-tick config keys instead of the per-cycle ones.
   - Locking: `_cycle_lock` currently covers both phases. Keep it on the poll phase; add a separate `_send_lock` for the send phase so a slow crawl never blocks send ticks (they run on the same bot event loop).
   - Retry semantics are unchanged in code but improve in practice: `_retry_or_drop()` leaves `status='new'` and the next send tick (5 min, not 30) picks it up. `MAX_ARTICLE_ATTEMPTS = 3` still bounds attempts.
   - Quiet-hours behavior unchanged (`_in_quiet_hours` already holds without charging an attempt).
3. `main.py` `setup_scheduler()` (line 278): register a second `run_repeating` job — `cron_send_phase` every `SEND_TICK_MINUTES * 60`, `first=60`, calling `_post_phase()` with the same exception-logging wrapper pattern as `cron_news_cycle` (line 69-80).
4. `bot/handlers.py` `/runpipeline` (line 1464-1485): decide and document — simplest is: manual run executes poll (all slots, see Part 2) **and then** one send pass, so admins still get the full cycle on demand. Update its stats output accordingly.

### Tests (`tests/test_post_phase.py` extends naturally — it already monkeypatches `config.MAX_POSTS_PER_STREAM_PER_CYCLE`)

- Per-tick budget: 5 queued deliveries for one stream, `MAX_POSTS_PER_STREAM_PER_TICK=2` → exactly 2 sent, 3 remain `status='new'`; next invocation sends 2 more.
- Clump spreading: queue of 6 for one stream drains over 3 successive `_post_phase()` calls, oldest-first ordering preserved (`ORDER BY a.fetched_at ASC` at `database/store.py:594`).
- `run_news_cycle()` no longer sends anything (poll-only).
- Global per-tick ceiling respected across multiple streams.

---

## Part 2 — Staggered polling

Today every due source is polled every 30 minutes in one `asyncio.gather` burst (`news_cycle.py:98-105`). Note: `_due_for_poll()` (line 72-92) has **no 30-min gate for `daily`/unknown-tier sources** — so naively shortening the tick would poll them 3× more often. Slotting is mandatory, not optional.

Goal: poll tick every 10 minutes; each source assigned to one of 3 slots by its stable integer id; each source still polled every ~30 min, discoveries spread across the half hour. Side benefit: Chromium crawl load spreads out too.

### Changes

1. `config.py`: rename/split `NEWS_CYCLE_MINUTES = 30` → `POLL_TICK_MINUTES = 10`; add `POLL_SLOTS = 3`. (Update references in `main.py:290-295, 328, 363`.)
2. `pipeline/news_cycle.py`:
   - Current slot: `slot = int(time.time() // (POLL_TICK_MINUTES * 60)) % POLL_SLOTS` — deterministic across restarts, no state to persist.
   - `_due_for_poll(source, now, slot)`: add `source.id % POLL_SLOTS == slot` to the gate. `POLL_TIER_HOURS` logic untouched (weekly/monthly/rare sources keep their longer intervals on top of slotting).
   - `/runpipeline` (manual) passes a flag to skip the slot check so admins can force a full poll.
3. No literal per-source cron entries — hash-bucketing gives the same effect with zero config management.

### Tests (`tests/test_news_cycle.py` — `test_due_for_poll_tiers` at line 254 already calls `_due_for_poll` directly)

- A source is polled only when `source.id % 3 == current_slot`; over 3 consecutive ticks every due daily source is polled exactly once.
- Tier gating still applies within a slot (a `weekly` source due in 4h is skipped even in its slot).
- Slot calculation is stable for a fixed mocked timestamp.

---

## Part 3 — Natural-language stream tuning ("Tune my stream")

User story: a subscriber with a broad "politics" stream types "I don't want news about the Ukraine-Russia war anymore" and the stream stops delivering those articles — without hand-editing prompts, without the prompt degrading as requests pile up, and with a guardian that redirects off-topic requests to a new stream.

### Design: structured rules, not prompt editing

The accept/reject gate is `pipeline/relevance_checker.py` — `GATE_INSTRUCTIONS` + a per-stream rubric resolved by `_rubric_for(profile)` (line 31-71) from the stream's `criteria` JSON. Key constraint discovered: `criteria["relevance_rubric"]` takes **precedence** over synthesized fields, so editing `criteria["exclude"]` alone would be silently ignored on researched streams.

Instead of letting an LLM rewrite the rubric prose on every request (non-deterministic, prompt rots as unstructured requests accumulate), store **atomic rules** and render them into the gate prompt with a fixed, code-owned template:

1. **Data model** — new key in the `streams.criteria` JSON (no schema migration needed; `criteria` is a JSON blob, `update_stream_criteria`/`set_stream_criteria_field` at `database/store.py:93,100` are the write path):
   ```json
   "rules": [
     {"id": 1, "kind": "exclude", "text": "Ukraine-Russia war", "created_at": "...", "active": true}
   ]
   ```
   Append-only with soft-deactivation; `kind` is `exclude` or `include`. Rule text is a short normalized topic phrase produced by the interpreter LLM, not the user's raw rambling — this is what keeps the prompt clean.
2. **Gate rendering** — `_rubric_for()` gains a step: if active `rules` exist, append a deterministic section after the rubric:
   ```
   ## Hard user rules (override everything above)
   - ALWAYS reject articles about: Ukraine-Russia war; US midterm horse-race polls
   - ALWAYS accept articles about: EU AI regulation
   ```
   Rendering is pure string joining in code — the LLM never edits prose, so the protocol can't be broken by a weird request. Rules are one-liners; cap at ~20 active rules per stream, beyond that ask the user to merge/remove (surfaced in the rules list UI).
3. **Interpreter** (new, modeled on `build_relevance_rubric` at `research/profile_builder.py:183-198`, tier `"smart"`, `chat_json`): input = stream topic/domain + current rule texts + the user's message; output contract:
   ```json
   {"action": "add_exclude" | "add_include" | "remove_rule" | "off_topic" | "unclear",
    "rule_text": "Ukraine-Russia war",
    "matched_rule_id": 3,
    "reply": "one-sentence explanation to the user"}
   ```
   Fails safe to `unclear` on unparseable output (same idiom as `interview_turn`'s fallback, `profile_builder.py:121-127`).
4. **Guardian** — the same interpreter prompt carries the stream's `broad_domain`/`topic` and is instructed: if the request would pull *coverage* toward a topic outside the domain (e.g. "also send me crypto news" into a politics stream), return `off_topic`. The bot then replies suggesting `/newstream` for that topic and adds nothing. (An *exclusion* of anything is always in-scope — narrowing never overloads the gate.)
5. **Confirmation before writing** — the bot shows the parsed rule ("Got it: exclude **Ukraine-Russia war** from *Politics*.") with ✅ / ❌ inline buttons. Nothing is persisted on `unclear` or ❌. This is the deterministic checkpoint that absorbs disorganized user input.
6. **Rules management UI** — menu entry "Tune stream" per stream + a rules list with per-rule delete buttons (callback prefix `rule:`), following the existing `handle_callback` routing pattern (`bot/handlers.py:1158-1341`).
7. **Bot intake plumbing** — clone the armed-state pattern of `handle_pending_source` (`bot/handlers.py:1035-1063`): "Tune stream" button sets `context.user_data["tune_stream"] = stream_id`; the existing catch-all text handler (registered at `main.py:269-270`) routes the next message to the interpreter. Works in webhook mode; `PicklePersistence` keeps the armed state across restarts.

### What this deliberately avoids

- No LLM rewriting of `relevance_rubric` prose per request (non-deterministic, unverifiable).
- No per-article extra LLM calls — rules ride inside the existing single gate call.
- No freeform "memory" appended to prompts — the rule list is the memory, and it's user-visible and editable.

### Changes by file

- `pipeline/relevance_checker.py` — `_rubric_for()` renders the rules section; unit-testable pure function.
- `research/profile_builder.py` (or new `research/rule_interpreter.py`) — interpreter prompt constant + `interpret_rule_request()` returning the parsed contract.
- `bot/handlers.py` — menu button, armed state, interpreter call, confirmation keyboard, rules list, `rule:` callbacks. Follow the merge-don't-clobber precedent at `handlers.py:683-698` when writing `criteria`.
- `bot/i18n.py` — en/ru strings for the new screens.
- `database/store.py` — small helpers: `add_stream_rule`, `deactivate_stream_rule` (or inline via `set_stream_criteria_field`).

### Tests

- `_rubric_for` with rules → gate prompt contains the exact hard-rules section; inactive rules omitted; precedence over rubric text verified.
- Interpreter: parse each `action` variant from canned LLM JSON; garbage JSON → `unclear`, nothing written.
- Guardian: "also send me F1 news" on a politics stream → `off_topic`, criteria unchanged.
- Bot flow (pattern after `tests/test_menu.py`'s fake `_Query`): tune → message → confirm ✅ → rule stored; ❌ → not stored; duplicate request → interpreter returns `matched_rule_id`, bot replies "already covered" instead of adding a second rule.
- Rule cap: 20th rule accepted, 21st refused with a merge suggestion.

---

## Rollout order

1. **Part 0** — tiny, fixes a live bug. Ship immediately.
2. **Part 1** — the symptom-killer for batching. Independent of Part 2; ship next.
3. **Part 2** — smoothing; requires the slot gate so sources aren't polled 3× more often.
4. **Part 3** — the feature; largest piece, ships last.

Each part lands with its tests green, then `systemctl restart test-news-saas.service` and a live observation window (one full cycle for 0-2; a scripted tune-session for 3).
