# 🔬 How Source Research Works — Complete Breakdown

> This document explains, step by step and in plain language, exactly how the system finds news sources for a user's topic. No jargon. Read this to understand the full flow and identify where you can improve things.

---

## The Big Picture

When a user says "I want news about X," the system needs to find **websites** that regularly publish articles about X. Not just any websites — ones that genuinely focus on what the user asked for, and that our crawler can actually read.

This happens in **4 phases**, one after the other:

```
User answers questions
        ↓
Phase 1: Understand what they want (build a "profile")
        ↓
Phase 2: Search the web to find candidate websites
        ↓
Phase 3: Evaluate each candidate — is it really about the topic?
        ↓
Phase 4: Verify we can actually crawl each accepted source
        ↓
Sources saved to database
```

Total time: about **3–5 minutes** for a typical stream.

---

## Phase 1 — Understanding What the User Wants

**File:** `research/profile_builder.py`
**Goal:** Turn the user's free-text answers into a structured profile the rest of the system can use.

### What happens

The user answers **3 premade questions** in Telegram:

1. **"What topic are you interested in?"** — they describe it in their own words
2. **"How strict should matching be?"** — exclusive focus, or broader coverage okay?
3. **"What do you NOT want?"** — topics/angles to exclude

Then the LLM generates **1–2 dynamic follow-up questions** based on those answers. For example, if the user said "crypto regulation," the LLM might ask "Are you interested in a specific region (US, EU, global)?" or "Do you want deep legal analysis or quick news updates?"

### What comes out

All answers are sent to the LLM with a system prompt that says: "Turn this into a structured Source Criteria Profile." The output is a JSON object:

```json
{
  "broad_domain": "cryptocurrency",
  "specific_topics": ["DeFi regulation", "EU MiCA framework"],
  "exclude": ["price predictions", "memecoins"],
  "geography": "Europe",
  "language": "en",
  "strictness": "high",
  "min_frequency": "daily",
  "source_type": "news_site",
  "keywords": ["DeFi regulation EU", "MiCA crypto compliance", ...],
  "description": "Sources covering EU DeFi regulation and MiCA framework updates"
}
```

This profile is the **north star** — every later phase checks against it.

### Where you could improve

- The premade questions are fixed. You could make them more dynamic or topic-aware.
- The follow-up question generation is limited to 2. More rounds might produce a sharper profile.
- The LLM sometimes interprets "strictness" inconsistently. You could define it more concretely (e.g. "what % of articles must be about this topic").

---

## Phase 2 — Finding Candidate Websites

**File:** `research/discovery.py`
**Goal:** Cast a wide net across the web to find websites that *might* be good sources.

### What happens

#### Step 1: Generate search queries

The LLM takes the profile and generates **6 varied search queries** (configurable via `MAX_SEARCH_QUERIES`). Each query approaches the topic from a different angle:

- Some are specific: `"MiCA framework EU crypto regulation news"`
- Some are broader: `"cryptocurrency news site"`
- Some use synonyms: `"digital asset compliance updates Europe"`

#### Step 2: Run searches in parallel

All 6 queries are sent to the **Brave Search API** simultaneously (up to 3 concurrent API calls). Each query returns up to 8 results (`MAX_CANDIDATES_PER_QUERY`). That gives us roughly 40–50 raw URLs.

#### Step 3: Collapse article URLs to source URLs

This is a crucial step. Search results often point to **individual articles**, not publications:

- ❌ `beincrypto.com/institutional-digital-asset-adoption-firms-2026/` — this is one article
- ✅ `beincrypto.com` — this is a publication that publishes many articles

The system uses `derive_source_url()` from `urlutils.py` to collapse article URLs to their parent section. It checks:

- Is the URL a date-based path? (`/2026/07/01/...`) → article, collapse it
- Is it a slug? (`/blog/some-long-title-here`) → article, collapse it
- Is it nested under a known section? (`/blog/specific-post`) → article, collapse to `/blog`
- Is it already a section page? (`/news`, `/blog`) → keep as-is

#### Step 4: One candidate per domain

Even after collapsing, the same domain might appear from multiple search queries. The system keeps only **one URL per domain** — the one with the shallowest path (closest to the homepage). This prevents wasting time crawling the same site twice.

#### Step 5: Seed with internal DB matches

If previous research runs already found sources matching this profile's broad category and keywords, those URLs are added to the candidate pool. This implements the "check our internal DB first" idea from the overview — every user's research benefits from previous users' research.

#### What comes out

A list of **unique publication URLs** (typically 30–50), with no duplicates and no individual articles.

### Where you could improve

- **Only one search engine.** Adding Google, Bing, or a news-specific search would cast a wider net.
- **No RSS directory search.** Many sites have discoverable RSS feeds that would be more reliable to crawl.
- **No social media search.** X.com accounts, Reddit communities, and YouTube channels are excluded entirely (filtered out in `_is_valid_candidate`).
- **Query generation is one-shot.** The LLM generates all queries upfront. A "search → analyze results → generate better queries" loop could find sources the initial queries missed.
- **The skip-domain list is hardcoded.** `github.com` is skipped, but some projects publish news on their GitHub pages (changelogs, release notes).

---

## Phase 3 — Evaluating Each Candidate

**File:** `research/qualification.py`
**Goal:** Determine whether each candidate website *genuinely covers* the user's topic, and find the correct page to crawl for articles.

This is the hardest and most important phase. It uses a **two-stage funnel** for speed.

### Stage 1: Fast pre-filter

**Goal:** Quickly eliminate obviously irrelevant candidates so we don't waste time deep-diving 50 sites.

#### What happens

1. **Fetch all candidate homepages in parallel** — up to 15 concurrent crawls (`MAX_CONCURRENT_CRAWLS`). The crawler (crawl4ai) loads each page in a headless browser, extracts the text content and links.

2. **Batch LLM evaluation** — the fetched homepages are sent to the LLM in chunks of 15. For each one, the LLM quickly scores it 0–100 and says "investigate" or "skip." The LLM sees:
   - The user's profile
   - Each site's URL, title, and first 800 characters of content

3. **Select top 15** — candidates marked "investigate" are sorted by score, and the top 15 move to Stage 2.

This stage typically takes **30–90 seconds** for 40+ candidates.

### Stage 2: Deep qualification

**Goal:** Thoroughly evaluate the survivors — read their actual articles, score them strictly, and identify the correct `feed_url`.

#### What happens (per candidate, all in parallel)

1. **Extract plausible article links** from the already-fetched homepage. Links are filtered to:
   - Same domain only (no off-site links)
   - Article-like URLs (slug patterns) OR titles with 3+ words and 40+ characters
   - This filters out navigation links ("Home", "About", "Config Generator")

2. **Fetch up to 3 articles** (`ARTICLES_TO_EXAMINE`) in parallel — the system reads the actual article content, not just headlines.

3. **LLM deep evaluation** — the LLM receives:
   - The user's profile
   - The homepage content (first 2000 chars)
   - The links found on the page
   - The full text of 3 recent articles

   The LLM then outputs:
   - Does this source cover the topic? (true/false)
   - What does it primarily focus on?
   - Match score (0–100)
   - Evidence (specific article titles as proof)
   - Quality assessment (high/medium/low)
   - Publishing frequency (daily/weekly/monthly/rare)
   - **feed_url** — the page we should crawl to get future articles

4. **feed_url validation** — the LLM's suggested `feed_url` is checked deterministically:
   - Must be on the **same domain** as the source (LLMs sometimes hallucinate URLs)
   - Must **not** be an individual article page (checked via `is_article_url()`)
   - If it fails either check, it's collapsed to a safe section URL or the homepage

#### What comes out

A list of qualified sources, each with: match score, description, keywords, quality assessment, and a verified `feed_url`. Only sources scoring above the threshold (70 by default, 75 for "high" strictness, 50 for "low") pass.

### Where you could improve

- **The LLM sees only 3 articles.** Reading more would give a better picture, but costs time and tokens.
- **No historical analysis.** The system evaluates a snapshot of the site *right now*. A site that posted about the topic 6 months ago but stopped wouldn't be caught.
- **Language detection is implicit.** If the user wants English news but a source publishes in German, the LLM might not catch it consistently.
- **Bias detection is absent.** The system doesn't evaluate whether a source has a strong editorial bias, which may matter to some users.
- **The feed_url LLM identification is a single shot.** The LLM might pick a category page that has fewer articles than the homepage. A "crawl both, count articles, pick the better one" approach would be more reliable.
- **No RSS discovery.** Many sites have RSS feeds at predictable URLs (`/feed`, `/rss`, `/atom.xml`). The system could probe these automatically during qualification instead of relying on the LLM to mention them.

---

## Phase 3.5 — Domain Deduplication

**File:** `research/engine.py` → `dedup_by_domain()`
**Goal:** Ensure no duplicate sources (same website appearing twice).

### What happens

After qualification, the system groups results by domain and keeps only the **highest-scoring source per domain**. This is deterministic — it always produces the same result regardless of order.

This runs as a safety net even though discovery already deduplicates by domain, because:
- Internal DB matches might introduce duplicates
- The LLM might slightly alter URLs during qualification

---

## Phase 4 — Validation

**File:** `research/validator.py`
**Goal:** Verify that we can actually crawl each accepted source's `feed_url`.

### What happens

For each of the top sources (up to 8, `DESIRED_SOURCES_MAX`):

1. **If the feed_url looks like an RSS feed** (contains `/feed`, `/rss`, `.xml`, etc.):
   - Parse it directly over HTTP (no browser needed)
   - If it returns items, mark as active
   - This is important because the browser crawler can false-flag raw XML as "blocked" (it sees minimal visible text)

2. **Otherwise, test with crawl4ai**:
   - Load the page in the headless browser
   - Check if content is extractable
   - If blocked (Cloudflare, paywall, etc.), mark as "blocked"

Sources that pass validation are marked "active." Sources that fail are still saved to the database but marked "blocked" — they show up in the bot so the user can see what was found but couldn't be crawled.

### Fallback logic

If fewer than 3 sources (`DESIRED_SOURCES_MIN`) pass validation, the system goes back to lower-ranked qualified sources, validates their feed_urls, and includes any that pass — until it reaches the minimum of 3.

### Where you could improve

- **Validation is binary.** A source is either "fetchable" or "blocked." There's no middle ground for "fetchable but returns very little content" or "fetchable but very slow."
- **No retry with different crawler settings.** If Cloudflare blocks the default headless browser, the system doesn't try a stealth mode, different user agent, or proxy.
- **RSS detection is URL-pattern-based.** The system checks if the URL *looks like* a feed. It doesn't probe common feed paths (`/feed`, `/rss`) for sources where the feed_url wasn't identified.

---

## After Research: Storing Sources

**File:** `research/engine.py` → `run_research()`

When research completes, each source is saved to the SQLite database with:
- `url` — the publication URL
- `feed_url` — the specific page to crawl for articles
- `name`, `description`, `broad_category`, `specific_keywords` — from LLM evaluation
- `quality_score` — the match score (0–100)
- `fetch_status` — "active" or "blocked"
- `fail_count` — 0 (tracked over time by the fetch pipeline)

Blocked sources are also stored (with `fetch_status = "blocked"`) so the user can see them in the bot and decide whether to try again later.

---

## The Fetch Pipeline (Ongoing Article Collection)

**File:** `pipeline/fetch_news.py`
**Goal:** Periodically crawl each source's `feed_url` and extract new articles.

This runs every 30 minutes via cron job. For each active source, it tries **three strategies** in order:

### Strategy 1: RSS/Atom feed

If the `feed_url` looks like a feed (contains `/feed`, `/rss`, `.xml`, etc.), the system fetches it directly over HTTP — **no browser needed**. This is fast and reliable. The feed is parsed with BeautifulSoup, extracting title, link, and summary for each item.

### Strategy 2: Link extraction

If it's not a feed, the page is loaded in the headless browser. The system extracts all links and filters them:
- Same domain only
- Article-like URL slugs OR titles with 3+ words
- Excludes navigation, social media, utility pages
- Deduplicates by normalized URL

Up to 15 new articles (`MAX_ARTICLES_PER_FETCH`) are saved per source per cycle.

### Strategy 3: LLM inline extraction (fallback)

If **no article links** are found on the page (common with changelogs, update cards, or JS-heavy sites), the LLM reads the page content and extracts individual news/update items directly. Each item gets:
- A title
- A 2–3 sentence summary
- A hash based on `source::title` to prevent re-adding the same item

### Failure tolerance

A source is only deactivated after **3 consecutive failed fetches** (`MAX_CONSECUTIVE_FETCH_FAILURES`). A single transient error doesn't kill the source. The health check cron job (every 24h) re-tests blocked/error sources and resets their fail count if they're working again.

### Where you could improve

- **No JavaScript rendering check.** Some sites load articles dynamically via JS. The crawler waits for `domcontentloaded`, but some sites need more time or interaction.
- **No content diffing.** The system checks URL-based dedup but not content-based. If a source edits an article and changes its URL, it could be added twice.
- **The LLM fallback is expensive.** Every fetch cycle with no links triggers an LLM call. For changelog sources, caching the extraction pattern could save tokens.
- **No scheduled fetch timing.** All sources are fetched every 30 minutes regardless of how often they publish. A source that publishes weekly doesn't need 48 checks per day.

---

## Key Configuration Values

All of these live in `config.py` and can be tuned:

| Setting | Current | What it controls |
|---|---|---|
| `MAX_SEARCH_QUERIES` | 6 | How many Brave searches per stream |
| `MAX_CANDIDATES_PER_QUERY` | 8 | Results per search query |
| `MAX_CONCURRENT_CRAWLS` | 15 | Parallel browser tabs during Stage 1 |
| `QUALIFICATION_SCORE_THRESHOLD` | 70 | Minimum score to accept a source |
| `DESIRED_SOURCES_MIN` | 3 | If fewer pass, relax and try lower-ranked |
| `DESIRED_SOURCES_MAX` | 8 | Cap on final sources per stream |
| `ARTICLES_TO_EXAMINE` | 3 | Articles read per source during deep qualification |
| `MAX_CONSECUTIVE_FETCH_FAILURES` | 3 | Deactivate source only after N fails |
| `MAX_ARTICLES_PER_FETCH` | 15 | Cap new articles per source per cycle |

---

## URL Utilities — The Rules Engine

**File:** `research/urlutils.py`

This module contains **deterministic rules** (no LLM) for classifying and transforming URLs. It's used across discovery, qualification, and fetching.

### `is_article_url(url)` — Is this a single article?

Returns `True` if the URL points to a specific article, not a list/section page. Rules:
- Date in path (`/2026/07/01/...`) → article
- Numeric ID (`/12345`) → article
- File extension (`.html`, `.pdf`) → article
- Nested under a section (`/blog/some-post`) → article
- Hyphenated slug with 3+ dashes → article
- 3+ path segments → likely article
- Known section name as last segment (`/news`, `/blog`) → NOT an article

### `derive_source_url(url)` — Collapse to publication

Takes an article URL and walks up the path until it finds a non-article-looking page. For example:
- `site.com/blog/specific-post` → `site.com/blog`
- `site.com/2026/07/01/news-story` → `site.com` (root)

### `registered_domain(url)` — Extract domain

Returns the clean domain (`coindesk.com` from `https://www.coindesk.com/path`).

### `normalise_url(url)` — For deduplication

Strips query strings, fragments, and trailing slashes. Two URLs that differ only in `?utm_source=...` are treated as the same article.

---

## Summary: Where the Biggest Improvements Are Possible

| Area | Current State | Improvement Potential |
|---|---|---|
| **Discovery breadth** | Single search engine (Brave) | Add Google, RSS directories, Reddit, X.com |
| **Query strategy** | One-shot generation | Iterative: search → analyze → refine queries |
| **Source evaluation depth** | 3 articles examined | More articles, historical archive check |
| **feed_url identification** | LLM single guess + deterministic guard | Probe common feed paths automatically |
| **Anti-bot handling** | Give up on first block | Stealth mode, proxy rotation, Firecrawl fallback |
| **Language/region filtering** | LLM's implicit judgment | Explicit language detection on page content |
| **Content quality scoring** | LLM holistic judgment | Add readability scores, source reputation DB |
| **Fetch efficiency** | All sources every 30 min | Adaptive scheduling based on publishing frequency |
| **Dedup across streams** | Per-stream only | Global article dedup (same article from 2 sources) |
| **Historical learning** | Internal DB match by keywords | Vector similarity: "sources similar to ones that worked for similar profiles" |