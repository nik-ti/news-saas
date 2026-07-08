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
MAX_ARTICLES_PER_FETCH = 15      # cap new articles per source per fetch cycle

# ── Pipeline / Cron ───────────────────────────────────────────────────────────
STREAM_POST_INTERVAL_MINUTES = 15   # real-time article posting cron
FETCH_INTERVAL_MINUTES = 30         # legacy fetch-only cron
PROCESS_INTERVAL_MINUTES = 60
DELIVER_INTERVAL_HOURS = 6
HEALTH_CHECK_INTERVAL_HOURS = 24

# ── Telegram API base (for sendRichMessage) ──────────────────────────────────
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"