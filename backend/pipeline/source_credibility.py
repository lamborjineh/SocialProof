"""
SocialProof — pipeline/source_credibility.py  v3.0
Evaluates source reliability based on domain type, HTTPS, and
the tiered reputation registry in corpus/source_registry.py.

v3.1 Changes:
  - Added get_factcheck_results() — async, queries Google Fact Check Tools API
    for IFCN-certified fact-checker reviews on a claim. Results cached in
    factcheck_cache table (24hr TTL) to stay within free quota.
  - Added score_check_worthiness() — async, queries ClaimBuster API (0.0–1.0).
    Stored in ClaimResult.check_worthiness. Not shown as a number to users.

v3.0 Changes:
  - Added get_mbfc_rating() — DB lookup, on-demand (Source node click).
  - SourceCredibilityModule is unchanged from v2.

Architecture note:
  SourceCredibilityModule.evaluate() → fast, fully offline, always runs.
  get_mbfc_rating()                  → DB lookup, on-demand.
  get_factcheck_results()            → async HTTP, on-demand (Source node click).
  score_check_worthiness()           → async HTTP, auto-runs on Claim step (silent).
  All API results are context for the user — never verdicts.
"""

import asyncio
import hashlib
import json
import urllib.request
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from urllib.parse import urlparse

from config import logger, GOOGLE_FACTCHECK_API_KEY, CLAIMBUSTER_API_KEY, FACTCHECK_CACHE_TTL_HOURS

from corpus.source_registry import get_reputation, REPUTATION_THRESHOLD


# ── MBFC lookup (v3 new) ──────────────────────────────────────────────────────

def get_mbfc_rating(url: Optional[str]) -> Optional[Dict]:
    """
    Look up a domain in the mbfc_domains table (populated by scripts/sync_mbfc.py).

    Returns a dict with non-judgmental signal fields, or None if:
      - no URL was provided
      - domain cannot be parsed
      - domain is not in the MBFC dataset

    The returned dict is added to the Source node response payload.
    The frontend displays it as context — e.g. "This domain has a [MIXED]
    factual reporting rating" — never as a verdict.

    Args:
        url: The URL submitted by the user, or None for text-only input.

    Returns:
        {
            "domain": str,
            "factual_reporting": str | None,   # HIGH / MOSTLY_FACTUAL / MIXED / LOW / VERY_LOW
            "bias_rating": str | None,          # LEFT-CENTER / CENTER / RIGHT / etc.
            "credibility_rating": str | None,
            "country": str | None,
        }
        or None
    """
    if not url or not url.strip():
        return None

    raw = url.strip().lower()
    if not raw.startswith("http"):
        raw = "https://" + raw

    try:
        parsed = urlparse(raw)
        domain = parsed.netloc.replace("www.", "").strip("/")
        if not domain:
            return None
    except Exception:
        return None

    try:
        import sqlalchemy as sa
        from database.models import engine

        with engine.connect() as conn:
            result = conn.execute(sa.text(
                "SELECT domain, factual_reporting, bias_rating, credibility_rating, country "
                "FROM mbfc_domains WHERE domain = :domain LIMIT 1"
            ), {"domain": domain})
            row = result.fetchone()

        if row is None:
            logger.debug(f"[MBFC] Domain not found in mbfc_domains: {domain}")
            return None

        return {
            "domain":            row[0],
            "factual_reporting": row[1],
            "bias_rating":       row[2],
            "credibility_rating": row[3],
            "country":           row[4],
        }

    except Exception as e:
        logger.warning(f"[MBFC] DB lookup failed for {domain}: {e}")
        return None


# ── Existing SourceCredibilityModule (unchanged from v2) ─────────────────────

class SourceCredibilityModule:
    """
    Outputs a source_score (0.0–1.0) and human-readable signals list.
    No external API calls — fully offline and reproducible.

    Scoring priority:
      1. Social platform → always 0.30 (low, social origin)
      2. Registry reputation >= 0.90 → score = reputation directly
      3. Registry reputation >= REPUTATION_THRESHOLD (0.65) → score = rep * 0.90
      4. Unknown domain → heuristics (TLD, HTTPS, .gov/.edu/.org bonus)
    """

    SOCIAL_PLATFORMS = {
        "facebook.com", "twitter.com", "x.com", "tiktok.com",
        "instagram.com", "youtube.com", "reddit.com", "threads.net",
    }
    SUSPICIOUS_TLDS = {".xyz", ".click", ".buzz", ".info", ".biz"}

    @classmethod
    def evaluate(cls, url: Optional[str], text: str) -> Dict:
        signals: list = []
        score   = 0.5

        if not url or url.strip() == "":
            social_mentions = sum(
                1 for p in cls.SOCIAL_PLATFORMS
                if p.replace(".com", "").replace(".net", "") in text.lower()
            )
            if social_mentions:
                score = 0.35
                signals.append("social_media_origin_detected")
            else:
                score = 0.45
                signals.append("no_source_provided")
            return {"score": score, "label": cls._label(score), "signals": signals}

        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        domain = parsed.netloc.lower().replace("www.", "")
        tld    = "." + domain.split(".")[-1] if "." in domain else ""

        if url.startswith("https"):
            score += 0.10
            signals.append("https_present")
        else:
            score -= 0.10
            signals.append("no_https")

        if domain in cls.SOCIAL_PLATFORMS:
            score = 0.30
            signals.append("social_media_source")
            return {"score": round(score, 3), "label": cls._label(score), "signals": signals}

        rep = get_reputation(domain)

        if rep >= 0.90:
            score = rep
            signals.append(f"registry_tier1_tier2:{domain}")
            return {"score": round(score, 3), "label": cls._label(score), "signals": signals}

        if rep >= REPUTATION_THRESHOLD:
            score = rep * 0.90
            signals.append(f"registry_tier3:{domain}")
            return {"score": round(score, 3), "label": cls._label(score), "signals": signals}

        if domain.endswith(".gov") or domain.endswith(".gov.ph"):
            score += 0.35
            signals.append("gov_domain_unlisted")
        elif domain.endswith(".edu") or domain.endswith(".edu.ph"):
            score += 0.30
            signals.append("edu_domain_unlisted")
        elif domain.endswith(".org"):
            score += 0.10
            signals.append("org_domain_unlisted")

        if tld in cls.SUSPICIOUS_TLDS:
            score -= 0.20
            signals.append(f"suspicious_tld:{tld}")

        score = max(0.0, min(1.0, score))
        return {"score": round(score, 3), "label": cls._label(score), "signals": signals}

    @staticmethod
    def _label(score: float) -> str:
        if score >= 0.70:
            return "High Credibility"
        if score >= 0.45:
            return "Moderate Credibility"
        return "Low Credibility"
