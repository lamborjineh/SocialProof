"""
retrieval/utils.py — Shared retrieval helpers
SocialProof v3.3

Fixes applied in this version:
  #5  — trust_normalised() now uses the full REPUTATION registry properly;
         hybrid_score() exposes domain_tier so callers can boost Tier-1 on
         all queries, not just numeric ones.
  #9  — pipeline_timer() context manager added for latency logging.
  #13 — recency_boost() updated: evidence older than 2 yr gets 0.5x weight,
         older than 5 yr gets 0.3x. date_published preferred over URL parsing
         when available.

Import from here in every retrieval module. Do NOT redefine these locally.
"""
from __future__ import annotations

import re
import time
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional

from corpus.source_registry import get_reputation, STATS_DOMAINS, TIER_MAP

# ── Index directory ───────────────────────────────────────────────────────────

INDEX_DIR = Path(__file__).parent.parent / "data"

# ── Hybrid ranking weights ────────────────────────────────────────────────────
W_SEMANTIC = 0.6
W_TRUST    = 0.2
W_RECENCY  = 0.2

# ── Numeric query detection ───────────────────────────────────────────────────

_NUMERIC_QUERY_RE = re.compile(
    r"\b(rate|percent|%|gdp|inflation|unemployment|poverty|population|"
    r"statistics?|data|figure|number|growth|index|ratio|income|wage|"
    r"production|export|import|revenue|budget|deficit|surplus)\b",
    re.I,
)


def is_numeric_query(claim: str) -> bool:
    """Return True if the claim appears to ask about statistics or numbers."""
    return bool(_NUMERIC_QUERY_RE.search(claim))


# ── Per-pipeline index file resolution ───────────────────────────────────────

def index_files(pipeline: str) -> Tuple[Path, Path, Path, Path]:
    """
    Return (faiss_path, npy_path, meta_path, type_path) for a given pipeline.
    Pipeline 'all' omits the suffix (backward-compatible combined index).
    """
    suffix = f"_{pipeline}" if pipeline != "all" else ""
    return (
        INDEX_DIR / f"embeddings{suffix}.faiss",
        INDEX_DIR / f"embeddings{suffix}.npy",
        INDEX_DIR / f"sentences_meta{suffix}.json",
        INDEX_DIR / f"index_type{suffix}.txt",
    )


# ── Scoring helpers ───────────────────────────────────────────────────────────

def recency_boost(url: str, date_published: Optional[str] = None) -> float:
    """
    Score recency in [0, 1].

    Fix #13: prefer date_published (ISO YYYY-MM-DD) over URL-pattern guessing.
    Decay schedule (linear):
      ≤ 30 days  → 1.0
      ≤ 730 days → linear 1.0 → 0.5
      ≤ 1825 days (5 yr) → linear 0.5 → 0.3
      > 5 yr     → 0.3
    Falls back to 0.5 (neutral) when no date is parseable.
    """
    days_old: Optional[int] = None

    # Prefer explicit date_published field
    if date_published:
        try:
            pub = datetime.fromisoformat(date_published[:10])
            days_old = (datetime.now() - pub).days
        except Exception:
            pass

    # Fallback: extract year/month from URL
    if days_old is None:
        m = re.search(r"/(20\d{2})(?:/(\d{2}))?", url)
        if m:
            try:
                year  = int(m.group(1))
                month = int(m.group(2)) if m.group(2) else 6
                days_old = (datetime.now() - datetime(year, month, 1)).days
            except Exception:
                pass

    if days_old is None:
        return 0.5

    if days_old <= 30:
        return 1.0
    if days_old <= 730:
        return 1.0 - 0.5 * (days_old - 30) / 700.0
    if days_old <= 1825:
        return 0.5 - 0.2 * (days_old - 730) / 1095.0
    return 0.3


def trust_normalised(domain: str) -> float:
    """
    Normalise a domain's reputation score to [0, 1].

    Fix (audit §MED): unknown domains previously returned 0.0 because
    get_reputation() defaults to 0.5 and the normalization range starts at 0.5,
    mapping exactly to 0.0.  Unknown domains should be neutral (0.5), not
    penalised.  We detect unknowns by checking REPUTATION directly and short-
    circuit before the normalization formula.
    """
    from corpus.source_registry import REPUTATION
    if domain not in REPUTATION:
        return 0.5   # neutral — don't penalise evidence from unknown sources
    rep = REPUTATION[domain]
    lo, hi = 0.50, 1.0
    return max(0.0, min(1.0, (rep - lo) / (hi - lo)))


def hybrid_score(
    semantic: float,
    domain: str,
    url: str,
    numeric_boost: bool = False,
    date_published: Optional[str] = None,
) -> float:
    """
    Weighted hybrid ranking score:
        final = 0.6 × semantic + 0.2 × domain_trust + 0.2 × recency

    Fix #5: numeric_boost now applies to ALL Tier-1 sources (was STATS_DOMAINS
    only). Tier-1 covers PSA, BSP, WHO, World Bank, etc.

    Fix #13: date_published passed through to recency_boost().
    """
    semantic = min(1.0, max(0.0, semantic))
    score = (
        W_SEMANTIC * semantic
        + W_TRUST   * trust_normalised(domain)
        + W_RECENCY * recency_boost(url, date_published=date_published)
    )
    tier = TIER_MAP.get(domain, 3)
    if numeric_boost and tier == 1:
        score = min(1.0, score * 1.15)
    return score


# ── Sentence splitting ────────────────────────────────────────────────────────

def split_sentences(
    text: str,
    min_len: int = 20,
    max_len: int = 500,
) -> list[str]:
    """
    Split text into sentences on .!? followed by a capital letter, digit, or
    opening quote. Filters by length.
    """
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z0-9"\'])', text)
    return [s.strip() for s in parts if min_len <= len(s.strip()) <= max_len]


# ── Fix #9: Pipeline timing context manager ───────────────────────────────────

@contextlib.contextmanager
def pipeline_timer(stage: str, log_fn=None):
    """
    Context manager that measures wall-clock time for a pipeline stage.

    Usage:
        with pipeline_timer("retrieval", log_fn=db.log_event) as t:
            results = retriever.search(claim)
        # t.elapsed_ms is available after the block exits

    log_fn, if provided, is called as log_fn("INFO", "pipeline", message, details).
    Suitable for passing corpus.db.log_event directly.
    """
    class _Timer:
        elapsed_ms: float = 0.0

    t = _Timer()
    start = time.perf_counter()
    try:
        yield t
    finally:
        t.elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        msg = f"[pipeline] {stage} completed in {t.elapsed_ms} ms"
        if log_fn:
            try:
                log_fn("INFO", "pipeline_timer", msg,
                       f'{{"stage": "{stage}", "elapsed_ms": {t.elapsed_ms}}}')
            except Exception:
                pass
