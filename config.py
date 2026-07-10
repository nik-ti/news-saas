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
MAX_CONCURRENT_CRAWLS = 15       # crawl4ai parallel semaphore (Stage 1 fetches all at once)
MAX_CONCURRENT_SEARCHES = 3      # Brave Search parallel semaphore
QUALIFICATION_SCORE_THRESHOLD = 70  # min score to accept a source
DESIRED_SOURCES_MIN = 3
DESIRED_SOURCES_MAX = 8
ARTICLES_TO_EXAMINE = 3          # articles to fetch per candidate source (deep dive)
MAX_CONSECUTIVE_FETCH_FAILURES = 3  # deactivate a source only after N failed cycles

# ── Pipeline / Cron ───────────────────────────────────────────────────────────
NEWS_CYCLE_MINUTES = 30          # the one cron: poll sources → gate → post
MAX_NEW_PER_SOURCE = 3           # new articles queued per source per cycle
MAX_POSTS_PER_CYCLE = 10         # global cap on messages sent per cycle
MAX_ARTICLE_ATTEMPTS = 3         # transient failures before an article is dropped
HEALTH_CHECK_INTERVAL_HOURS = 24

# ── Text budgets ──────────────────────────────────────────────────────────────
SUMMARY_CHAR_CAP = 1500          # summary handed to the gate and the post writer
POST_INPUT_CHAR_CAP = 2000       # cap on what the post writer reads

# ── Telegram API base (for sendRichMessage) ──────────────────────────────────
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Webhook (served behind nginx at bot.simple-flow.co) ───────────────────────
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://bot.simple-flow.co")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/test-news-saas")
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "3010"))