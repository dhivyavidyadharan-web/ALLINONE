"""All runtime settings, read from environment / .env (locally) or Vercel project env vars.

Core stack: Neon (DATABASE_URL) · Gemini (GEMINI_API_KEY) · Vercel (hosting + cron) · GitHub (code, CI, tick schedule).
Everything else (Portkey, Langfuse, live web scraping, alert webhook) is optional and switches on when its key is set.
Secrets are never logged, never sent to a model, never rendered on a page.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


ON_VERCEL = bool(os.getenv("VERCEL"))

# --- Neon (core) ---------------------------------------------------------
# Postgres connection string. Use Neon's *pooled* URL (host contains "-pooler").
# Empty locally = SQLite file for offline development/tests only.
# NEON / GEMINI are accepted too (names used when the Vercel project was first set up).
DATABASE_URL = (os.getenv("DATABASE_URL", "") or os.getenv("NEON", "")).strip()
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "allin.db"))
SAMPLES_DIR = ROOT / "data" / "samples"   # fictional data, used by the automated tests
# Seed data the app loads (applications, memos, portfolio, web fixtures). Real Case 03 data lives in data/seed.
DATA_DIR = Path(os.getenv("DATA_DIR", "") or (ROOT / "data" / "seed" if (ROOT / "data" / "seed" / "memos.json").exists() else SAMPLES_DIR))

# --- Gemini (core) -------------------------------------------------------
# LLM_MODE=mock -> deterministic offline model (no key, no network), still costed and traced.
# LLM_MODE=live -> Gemini via the google-genai Interactions API.
LLM_MODE = os.getenv("LLM_MODE", "mock").lower()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI", "")).strip()
MODEL_SCORE = os.getenv("MODEL_SCORE", "gemini-4-hydrogen")          # scoring, recommendation, backtest analysis
MODEL_EXTRACT = os.getenv("MODEL_EXTRACT", "gemini-3.5-flash-lite")  # claim extraction, reply parsing
MODEL_WRITE = os.getenv("MODEL_WRITE", "gemini-3.5-flash-lite")      # email drafting
# Fallback mechanism: if a model keeps failing (overload, 5xx, timeouts), try these in order.
FALLBACK_MODELS = {
    "MODEL_SCORE": _list("FALLBACK_SCORE", "gemini-3.8-flash"),
    "MODEL_EXTRACT": _list("FALLBACK_EXTRACT", "gemini-3.1-flash-lite"),
    "MODEL_WRITE": _list("FALLBACK_WRITE", "gemini-3.1-flash-lite"),
}
RETRIES_PER_MODEL = _i("RETRIES_PER_MODEL", 2)
MODEL_TIMEOUT_S = _f("MODEL_TIMEOUT_S", 40)

# USD per 1M tokens (input, output incl. thinking). From Google's pricing page on 2026-11-20;
# some change on 2026-12-01 / 2027-01-01 - update here when they do.
PRICES = {
    "gemini-4-carbon": (2.00, 10.00),
    "gemini-4-argon": (2.00, 10.00),
    "gemini-4-neon": (3.00, 15.00),
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}
MOCK_PRICE_AS = "gemini-4-hydrogen"  # mock calls are priced like the scoring model so the cost page is realistic

# --- Optional: Portkey gateway (Case 04 Task 3) ---------------------------
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY", "")
PORTKEY_BASE_URL = os.getenv("PORTKEY_BASE_URL", "https://api.portkey.ai/v1").rstrip("/")
PORTKEY_RETRY_ATTEMPTS = _i("PORTKEY_RETRY_ATTEMPTS", 3)

# --- Optional: Langfuse (Case 04 Task 4) ---------------------------------
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")

# --- Queue (Case 04 Task 2) ----------------------------------------------
WORKER_CONCURRENCY = _i("WORKER_CONCURRENCY", 3)      # applications processed in parallel per tick / worker
MAX_ATTEMPTS = _i("MAX_ATTEMPTS", 3)
TICK_BUDGET_S = _f("TICK_BUDGET_S", 150)              # stop claiming new work after this; leaves 150 s of the 300 s Vercel limit for the last app
STALE_AFTER_MIN = _f("STALE_AFTER_MIN", 6)            # 'processing' older than this = killed function, re-queue
RUN_BACKGROUND_WORKERS = os.getenv("RUN_BACKGROUND_WORKERS", "0" if ON_VERCEL else "1") == "1"
MOCK_FAIL_RATE = _f("MOCK_FAIL_RATE", 0.0)
MOCK_LATENCY_S = _f("MOCK_LATENCY_S", 0.15)

# --- Cost (Case 04 Task 5) -----------------------------------------------
DAILY_COST_LIMIT_USD = _f("DAILY_COST_LIMIT_USD", 5.00)
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")

# --- Security (required when deployed) -----------------------------------
APP_PASSWORD = os.getenv("APP_PASSWORD", "")      # dashboard login; empty = no login (local only)
SESSION_SECRET = os.getenv("SESSION_SECRET", "") or APP_PASSWORD or "dev-only-secret"
CRON_SECRET = os.getenv("CRON_SECRET", "")        # Vercel cron + GitHub Actions call /api/tick with this
INTAKE_TOKEN = os.getenv("INTAKE_TOKEN", "")      # for POST /api/applications from the public form

# --- Research ------------------------------------------------------------
LIVE_WEB = os.getenv("LIVE_WEB", "0") == "1"      # 1 = read real websites (else offline fixtures)
SCRAPER_MAX_PAGES = _i("SCRAPER_MAX_PAGES", 5)      # homepage + the 4 most useful pages
SCRAPER_BUDGET_S = _f("SCRAPER_BUDGET_S", 20)       # per site; keeps a tick inside Vercel's limit
SCRAPER_DELAY_S = _f("SCRAPER_DELAY_S", 0.5)        # pause between requests to the same site
SCRAPER_MAX_BYTES = _i("SCRAPER_MAX_BYTES", 2_000_000)
SCRAPER_PAGE_CHARS = _i("SCRAPER_PAGE_CHARS", 6000)
SCRAPER_CACHE_DAYS = _i("SCRAPER_CACHE_DAYS", 7)
SEARCH_MAX_RESULTS = _i("SEARCH_MAX_RESULTS", 5)    # independent sources kept per application
SEARCH_PAGE_BUDGET_S = _f("SEARCH_PAGE_BUDGET_S", 8)  # time to open each search result
SEARCH_FOUNDERS = os.getenv("SEARCH_FOUNDERS", "1") == "1"  # 1 = also search founder names (search step only)
SEARCH_CALL_USD = _f("SEARCH_CALL_USD", 0.0)          # Google's per-request fee for search grounding; set it from
                                                      # Gemini's current price list so /costs and the daily limit see it
SCRAPER_ALLOW_PRIVATE = os.getenv("SCRAPER_ALLOW_PRIVATE", "0") == "1"  # tests only: allow localhost

# --- Outreach ------------------------------------------------------------
FOLLOW_UP_AFTER_DAYS = _i("FOLLOW_UP_AFTER_DAYS", 3)
CLOSE_AFTER_FOLLOW_UPS = _i("CLOSE_AFTER_FOLLOW_UPS", 2)
REVIEWER_NAME = os.getenv("REVIEWER_NAME", "Riya")
FUND_NAME = "All In Capital"


def portkey_enabled() -> bool:
    return bool(PORTKEY_API_KEY)


def langfuse_enabled() -> bool:
    return bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)
