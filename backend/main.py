"""
SocialProof — FastAPI Application Entry Point  (merged v3.0)
Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Interactive docs:  http://localhost:8000/docs
Health check:      http://localhost:8000/health

Security fixes applied:
  C-5  — serve_page() path is sanitised; only plain identifiers allowed.
  H-4  — /health no longer exposes internal index_stale_msg detail publicly.
  M-1  — CORS narrowed to explicit methods and headers.
  L-2  — Dead docstring in health() removed.
"""

import json
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from config import logger, CORS_ORIGINS
from core.model_registry import ModelRegistry
from database.models import init_mysql_schema
from routers.dashboard   import router as dashboard_router
from routers.prebunking  import router as prebunking_router
from routers.calibration import router as calibration_router
from routers.comparison  import router as comparison_router
from routers import (
    analyze_router,
    user_eval_router,
    lessons_router,
    quiz_router,
    auth_router,
    admin_router,
)

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("SocialProof API v3.3 starting — running migrations and pre-loading models…")

    try:
        init_mysql_schema()
        logger.info("DB migrations complete.")
    except Exception as e:
        logger.error(f"DB migration error: {e}")

    try:
        ModelRegistry.nlp()
        ModelRegistry.embed()
        logger.info("spaCy + SentenceTransformer loaded.")
    except Exception as e:
        logger.error(f"Model pre-loading error (spaCy/embed): {e}")

    try:
        ModelRegistry.nli()
        logger.info("NLI model pre-loaded.")
    except Exception as e:
        logger.warning(f"NLI pre-warm skipped (will load on first request): {e}")

    try:
        from retrieval.retriever import get_retriever
        get_retriever()
        logger.info("BGE-M3 Retriever pre-loaded.")
    except Exception as e:
        logger.warning(f"Retriever pre-warm skipped (will load on first request): {e}")

    yield
    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("SocialProof API shutting down.")
    from routers.analyze import _executor
    _executor.shutdown(wait=True)
    logger.info("ThreadPoolExecutor shut down.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "SocialProof NLP Analysis API",
    description = "Media and Information Literacy credibility analysis pipeline",
    version     = "3.3.0",
    lifespan    = lifespan,
)

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title       = app.title,
        version     = app.version,
        description = app.description,
        routes      = app.routes,
    )
    schema.setdefault("components", {})
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type":         "http",
            "scheme":       "bearer",
            "bearerFormat": "JWT",
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

# M-1: Narrowed from ["*"] to explicit safe lists
app.add_middleware(
    CORSMiddleware,
    allow_origins     = CORS_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["GET", "POST", "PUT", "DELETE"],
    allow_headers     = ["Authorization", "Content-Type"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(analyze_router)
app.include_router(user_eval_router)
app.include_router(lessons_router)
app.include_router(quiz_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(comparison_router)
app.include_router(prebunking_router)
app.include_router(calibration_router)
app.include_router(dashboard_router)

# ── Frontend ──────────────────────────────────────────────────────────────────
# NOTE: Frontend is served by Vercel. This backend is API-only on Railway.
# Root endpoint returns API info.
@app.get("/")
async def root():
    return {"name": "SocialProof API", "version": "3.3.0", "docs": "/docs"}


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """
    Quick health probe. Reports Ollama and OCR availability.
    H-4 / L-2: Internal index staleness detail is logged server-side only,
    not exposed in the public response.
    """
    from retrieval.retriever import get_retriever
    try:
        stale_warning = getattr(get_retriever(), "stale_index_warning", None)
    except Exception:
        stale_warning = "Retriever not loaded yet"

    # H-4: Log stale warning internally; never expose the detail publicly
    if stale_warning:
        logger.warning(f"[Health] Index stale: {stale_warning}")

    ollama_ok = False
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            ollama_ok = True
    except Exception:
        pass

    from pipeline.image_input import is_ocr_available
    ocr_status = is_ocr_available()

    from pipeline.pdf_input import is_pdf_available
    pdf_status = is_pdf_available()

    return {
        "status":    "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "index_stale": stale_warning is not None,
        # H-4: index_stale_msg removed from public response
        "ollama":    "available" if ollama_ok else "not running (rule-based fallback active)",
        "ocr":       ocr_status,
        "pdf":       pdf_status,
    }


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
