"""
SocialProof — Centralised Configuration  (merged)
Loads from .env file when present; falls back to safe defaults.
All values are overridable via environment variables — never commit secrets.
"""
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Database ──────────────────────────────────────────────────────────────────
# Railway injects DATABASE_URL automatically when you add a MySQL/PostgreSQL plugin.
# For PlanetScale: use mysql+pymysql:// driver with SSL params appended.
# For Neon (PostgreSQL): use postgresql+psycopg2:// driver.
_raw_db_url = os.getenv("DATABASE_URL", "mysql+pymysql://user:password@localhost/socialproof_db")

# Railway PostgreSQL URLs come as postgres:// — SQLAlchemy needs postgresql://
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql+psycopg2://", 1)

DATABASE_URL = _raw_db_url

# ── Auth / JWT ────────────────────────────────────────────────────────────────
_SECRET_KEY_DEFAULT = "change-this-in-production-please"
SECRET_KEY          = os.getenv("SECRET_KEY", _SECRET_KEY_DEFAULT)
if SECRET_KEY == _SECRET_KEY_DEFAULT:
    import logging as _logging
    _logging.getLogger("socialproof").critical(
        "SECRET_KEY is using the insecure default value! "
        "Set SECRET_KEY in your .env file before deploying to production."
    )
JWT_ALGORITHM   = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "7"))

# ── ML Models ─────────────────────────────────────────────────────────────────
# Switched from roberta-large-mnli to MoritzLaurer/mDeBERTa-v3-base-mnli-xnli.
# mDeBERTa natively supports 100 languages including Filipino — zero translation needed.
# roberta-large-mnli is English-only; mDeBERTa removes the Google Translate dependency
# and the 6-second translation timeout entirely for Filipino input.
# Model size: ~280MB (smaller than roberta-large at 1.4GB). Performance on English
# is comparable; Filipino/multilingual performance is significantly better.
NLI_MODEL   = os.getenv("NLI_MODEL",   "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")

# ── Pipeline limits ───────────────────────────────────────────────────────────
MAX_CLAIMS                 = int(os.getenv("MAX_CLAIMS",                  "8"))
MAX_EVIDENCE               = int(os.getenv("MAX_EVIDENCE",                "5"))
QUIZ_QUESTIONS_PER_SESSION = int(os.getenv("QUIZ_QUESTIONS_PER_SESSION", "10"))
# REMOVED: RESPONSE_TARGET_SECONDS — global 5-second pipeline timeout deleted.
# Replaced with per-step timeouts below. Each step has its own fallback.
# See routers/analyze.py — asyncio.wait_for() has been removed.

# ── Per-step timeouts (seconds) ───────────────────────────────────────────────
TIMEOUT_URL_FETCH       = float(os.getenv("TIMEOUT_URL_FETCH",       "15.0"))  # fallback: use raw input text
TIMEOUT_TRANSLATION     = float(os.getenv("TIMEOUT_TRANSLATION",      "6.0"))  # fallback: use original text
TIMEOUT_LIVE_SEARCH     = float(os.getenv("TIMEOUT_LIVE_SEARCH",     "20.0"))  # fallback: partial results
TIMEOUT_OLLAMA_EXPLAIN  = float(os.getenv("TIMEOUT_OLLAMA_EXPLAIN",  "30.0"))  # fallback: rule-based explanation
TIMEOUT_OLLAMA_TIP      = float(os.getenv("TIMEOUT_OLLAMA_TIP",      "25.0"))  # fallback: rule-based MIL tip
TIMEOUT_OLLAMA_VALIDATE = float(os.getenv("TIMEOUT_OLLAMA_VALIDATE", "15.0"))  # fallback: skip validation

# ── Ollama (optional local LLM for explanation generation) ───────────────────
# Install: https://ollama.com  |  Pull: ollama pull llama3.2
# Falls back to rule-based explanations if Ollama is not running.
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# ── CORS origins ──────────────────────────────────────────────────────────────
# In production: set CORS_ORIGINS to your Vercel URL e.g. https://your-app.vercel.app
_cors_raw    = os.getenv(
    "CORS_ORIGINS",
    "http://localhost,http://127.0.0.1,http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080",
)
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# ── Fact-Check API Layer (v3 new) ────────────────────────────────────────────
# Google Fact Check Tools API — free (100 queries/day)
# Get key: https://console.cloud.google.com → Enable Fact Check Tools API
GOOGLE_FACTCHECK_API_KEY   = os.getenv("GOOGLE_FACTCHECK_API_KEY", "")

# ClaimBuster API — free for research/academic use
# Register at: https://idir.uta.edu/claimbuster
CLAIMBUSTER_API_KEY        = os.getenv("CLAIMBUSTER_API_KEY", "")

# iffy.news MBFC export URL (re-downloaded monthly by scripts/sync_mbfc.py)
MBFC_SYNC_URL              = os.getenv(
    "MBFC_SYNC_URL",
    "https://iffy.news/iffy-plus.csv",
)

# Hours to cache Google Fact Check results in factcheck_cache table
FACTCHECK_CACHE_TTL_HOURS  = int(os.getenv("FACTCHECK_CACHE_TTL_HOURS", "24"))


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("socialproof")
