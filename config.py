"""
Central configuration — loads environment variables and defines constants.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────────────
BRAVE_SEARCH_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("MVP_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# The operator's Telegram user id — admin-only commands and system alerts go
# here. Defaults to the alert chat so existing deployments need no new env var.
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", str(TELEGRAM_CHAT_ID)))

# ── OpenRouter / LLM ──────────────────────────────────────────────────────────
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL_FAST = "deepseek/deepseek-v4-flash"            # speed tasks
LLM_MODEL_SMART = "deepseek/deepseek-v4-flash"           # qualification
LLM_MODEL_POST = "google/gemini-2.5-flash"              # post writing (cheap, fast)
LLM_TEMPERATURE = 0.3

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "news.db")

# ── Research Engine ───────────────────────────────────────────────────────────
MAX_SEARCH_QUERIES = 6           # how many varied queries to generate
MAX_CANDIDATES_PER_QUERY = 8     # top results per Brave query
MAX_CONCURRENT_CRAWLS = 8        # crawl4ai parallel semaphore — 15 concurrent pages
                                 # in one Chromium was the likeliest OOM trigger
MAX_CONCURRENT_SEARCHES = 3      # Brave Search parallel semaphore
QUALIFICATION_SCORE_THRESHOLD = 70  # min score to accept a source
DESIRED_SOURCES_MIN = 3
DESIRED_SOURCES_MAX = 8
ARTICLES_TO_EXAMINE = 3          # articles to fetch per candidate source (deep dive)
MAX_CONSECUTIVE_FETCH_FAILURES = 3  # deactivate a source only after N failed cycles

# ── Pipeline / Cron ───────────────────────────────────────────────────────────
NEWS_CYCLE_MINUTES = 30          # the one cron: poll sources → gate → post
MAX_NEW_PER_SOURCE = 3           # new articles queued per source per cycle
MAX_POSTS_PER_CYCLE = 30         # global safety ceiling on messages per cycle
MAX_POSTS_PER_STREAM_PER_CYCLE = 5  # per-stream budget — one noisy stream can't
                                    # starve every other tenant anymore
MAX_ARTICLE_ATTEMPTS = 3         # transient failures before an article is dropped
HEALTH_CHECK_INTERVAL_HOURS = 24
RETENTION_DAYS = 30              # nightly prune of dead article rows
AUTO_PAUSE_SEND_FAILURES = 3     # consecutive terminal send failures → stream paused

# Polling tiers (§2.6): non-RSS sources whose proven publishing frequency is low
# skip ticks. Hours a source must wait between browser crawls, by frequency.
POLL_TIER_HOURS = {"daily": 0, "weekly": 4, "monthly": 12, "rare": 24}

# Story-level semantic dedup (§3.2): a candidate whose embedding is closer than
# this to something already posted to the same stream in the window is a dup.
STORY_DEDUP_THRESHOLD = 0.85
STORY_DEDUP_HOURS = 72

# ── Per-user limits (§3.3) ───────────────────────────────────────────────────
RESEARCH_RUNS_PER_DAY = 3        # /newstream + /research runs per user per day
MAX_STREAMS_PER_USER = 5
MAX_SOURCES_PER_STREAM = 15

# Internal source-DB cache (§2.5): a semantic match at least this similar skips
# the Stage-1 prefilter and goes straight to deep qualification.
CACHE_SKIP_STAGE1_SIMILARITY = 0.75

# Re-baseline guard: if a KNOWN source suddenly shows this many "new" items and
# they make up at least this fraction of its page, the page structure changed
# (redesign, URL scheme change) — re-baseline silently instead of posting stale
# articles as news.
REBASELINE_MIN_ITEMS = 8
REBASELINE_FRACTION = 0.8

# ── Text budgets ──────────────────────────────────────────────────────────────
SUMMARY_CHAR_CAP = 1500          # summary handed to the gate and the post writer
MIN_TRUSTED_SUMMARY_CHARS = 300  # a stored summary shorter than this is an RSS
                                 # teaser — fetch the real article instead
POST_INPUT_CHAR_CAP = 2000       # cap on what the post writer reads

# ── Telegram API base (for sendRichMessage) ──────────────────────────────────
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Webhook (served behind nginx at bot.simple-flow.co) ───────────────────────
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://bot.simple-flow.co")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/test-news-saas")
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "3010"))