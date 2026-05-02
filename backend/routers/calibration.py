"""
SocialProof — Router: Calibration  v1.0
routers/calibration.py

Endpoints:
  GET  /calibration/data        — full calibration dataset for a user/session
                                  (evaluations with confidence levels + peer comparisons)
  GET  /calibration/summary     — lightweight summary for dashboard widget
  GET  /calibration/peer/{id}   — peer agreement breakdown for one evaluation

Calibration score formula:
  - 50% weight: raw label-match accuracy (user_label == system_label)
  - 50% weight: confidence ordering (very_high most accurate → low least accurate)
  A perfectly calibrated user scores 100%.

Peer comparison uses anonymous aggregate from user_evaluations where
content_hash matches (same URL or text hash). Never exposes individual data.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException

from config import logger
from database.models import engine

router = APIRouter()

# ── Confidence score mapping ───────────────────────────────────────────────────
CONF_SCORE = {
    "very_high": 1.0,
    "high":      0.75,
    "medium":    0.50,
    "low":       0.25,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_user_evaluations(session_token: str, user_id: Optional[int], limit: int = 50) -> List[Dict]:
    """
    Pull user_evaluations joined to evaluations for label + content data.
    Returns only rows that have a confidence_level set.
    """
    try:
        with engine.connect() as conn:
            if user_id:
                rows = conn.execute(sa.text("""
                    SELECT
                        ue.id                AS user_evaluation_id,
                        ue.user_label,
                        ue.confidence_level,
                        ue.created_at,
                        e.label              AS system_label,
                        e.input_text,
                        e.url,
                        e.content_hash,
                        CASE WHEN ue.user_label = e.label THEN 1 ELSE 0 END AS label_match
                    FROM user_evaluations ue
                    JOIN evaluations e ON e.id = ue.evaluation_id
                    WHERE ue.user_id = :uid
                      AND ue.confidence_level IS NOT NULL
                    ORDER BY ue.created_at DESC
                    LIMIT :lim
                """), {"uid": user_id, "lim": limit}).fetchall()
            else:
                rows = conn.execute(sa.text("""
                    SELECT
                        ue.id                AS user_evaluation_id,
                        ue.user_label,
                        ue.confidence_level,
                        ue.created_at,
                        e.label              AS system_label,
                        e.input_text,
                        e.url,
                        e.content_hash,
                        CASE WHEN ue.user_label = e.label THEN 1 ELSE 0 END AS label_match
                    FROM user_evaluations ue
                    JOIN evaluations e ON e.id = ue.evaluation_id
                    WHERE e.session_token = :tok
                      AND ue.confidence_level IS NOT NULL
                    ORDER BY ue.created_at DESC
                    LIMIT :lim
                """), {"tok": session_token, "lim": limit}).fetchall()

        results = []
        for r in rows:
            # Build a short content snippet for display
            raw = r.input_text or r.url or ""
            snippet = (raw[:80] + "…") if len(raw) > 80 else raw

            results.append({
                "id":               r.user_evaluation_id,
                "user_label":       r.user_label,
                "confidence_level": r.confidence_level,
                "created_at":       str(r.created_at),
                "system_label":     r.system_label,
                "content_snippet":  snippet,
                "content_hash":     r.content_hash,
                "label_match":      bool(r.label_match),
            })
        return results

    except Exception as e:
        logger.warning(f"[Calibration] _fetch_user_evaluations error: {e}")
        return []


def _compute_calibration(evals: List[Dict]) -> Dict:
    """
    Compute calibration score and per-confidence breakdown.
    Returns dict matching what calibration.html computeStats() expects.
    """
    if not evals:
        return {"calibration_score": None, "by_confidence": {}, "accuracy": None}

    total   = len(evals)
    correct = sum(1 for e in evals if e["label_match"])
    accuracy = correct / total

    by_conf: Dict[str, Dict] = {}
    for level in ["very_high", "high", "medium", "low"]:
        grp = [e for e in evals if e["confidence_level"] == level]
        if grp:
            grp_correct = sum(1 for e in grp if e["label_match"])
            by_conf[level] = {
                "n":       len(grp),
                "correct": grp_correct,
                "rate":    grp_correct / len(grp),
            }
        else:
            by_conf[level] = {"n": 0, "correct": 0, "rate": None}

    # Calibration ordering score
    order_pairs = [
        ("very_high", "high"),
        ("high",      "medium"),
        ("medium",    "low"),
    ]
    ok = 0
    checks = 0
    for a, b in order_pairs:
        ra = by_conf[a]["rate"]
        rb = by_conf[b]["rate"]
        if ra is not None and rb is not None:
            checks += 1
            if ra >= rb:
                ok += 1

    order_score = (ok / checks) if checks else 0.5
    calib_score = round((accuracy * 0.5 + order_score * 0.5) * 100)

    # Flags
    vh = by_conf.get("very_high", {})
    lo = by_conf.get("low", {})
    overconfident  = (vh.get("n", 0) >= 3 and vh.get("rate", 1) is not None and vh["rate"] < 0.50)
    underconfident = (lo.get("n", 0) >= 3 and lo.get("rate", 0) is not None and lo["rate"] > 0.70)

    return {
        "calibration_score": calib_score,
        "accuracy":          round(accuracy * 100, 1),
        "total":             total,
        "correct":           correct,
        "by_confidence":     by_conf,
        "overconfident":     overconfident,
        "underconfident":    underconfident,
    }


def _fetch_peer_comparisons(content_hashes: List[str]) -> List[Dict]:
    """
    For each content_hash, fetch aggregate label distribution from all
    user_evaluations where confidence_level IS NOT NULL.
    Returns the top-3 most contested evaluations for the peer comparison panel.
    """
    if not content_hashes:
        return []

    results = []
    try:
        with engine.connect() as conn:
            for ch in content_hashes[:10]:   # cap to avoid N+1 overload
                if not ch:
                    continue
                rows = conn.execute(sa.text("""
                    SELECT
                        ue.user_label,
                        COUNT(*) AS cnt,
                        e.label AS system_label,
                        e.input_text,
                        e.url
                    FROM user_evaluations ue
                    JOIN evaluations e ON e.id = ue.evaluation_id
                    WHERE e.content_hash = :ch
                      AND ue.confidence_level IS NOT NULL
                    GROUP BY ue.user_label, e.label, e.input_text, e.url
                    ORDER BY cnt DESC
                """), {"ch": ch}).fetchall()

                if not rows or sum(r.cnt for r in rows) < 5:
                    continue   # not enough raters for meaningful comparison

                total_raters   = sum(r.cnt for r in rows)
                system_label   = rows[0].system_label
                raw_text       = rows[0].input_text or rows[0].url or ""
                snippet        = (raw_text[:75] + "…") if len(raw_text) > 75 else raw_text
                agree_rows     = [r for r in rows if r.user_label == system_label]
                agree_pct      = round(sum(r.cnt for r in agree_rows) / total_raters * 100)
                peer_label     = rows[0].user_label   # most common user label

                results.append({
                    "content_hash":  ch,
                    "snippet":       snippet,
                    "system_label":  system_label,
                    "peer_label":    peer_label,
                    "agree_pct":     agree_pct,
                    "total_raters":  total_raters,
                    "label_dist":    [{"label": r.user_label, "count": r.cnt} for r in rows],
                })
    except Exception as e:
        logger.warning(f"[Calibration] _fetch_peer_comparisons error: {e}")

    # Sort by contestedness (agree_pct closest to 50%)
    results.sort(key=lambda x: abs(x["agree_pct"] - 50))
    return results[:4]


# ── GET /calibration/data ─────────────────────────────────────────────────────

@router.get("/calibration/data")
async def get_calibration_data(
    session_token: str,
    user_id:       Optional[int] = None,
):
    """
    Full calibration dataset consumed by calibration.html.
    Returns evaluations list + calibration stats + peer comparisons.
    """
    evals = _fetch_user_evaluations(session_token, user_id)
    stats = _compute_calibration(evals)

    # Peer comparison — only for evaluations that have a content_hash
    hashes = list({e["content_hash"] for e in evals if e.get("content_hash")})
    peer   = _fetch_peer_comparisons(hashes)

    return {
        "evaluations":       evals,
        "calibration":       stats,
        "peer_comparisons":  peer,
        "generated_at":      datetime.utcnow().isoformat(),
    }


# ── GET /calibration/summary ──────────────────────────────────────────────────

@router.get("/calibration/summary")
async def get_calibration_summary(
    session_token: str,
    user_id:       Optional[int] = None,
):
    """
    Lightweight endpoint for the dashboard widget.
    Returns just calibration_score, accuracy, total, overconfident flag.
    """
    evals = _fetch_user_evaluations(session_token, user_id, limit=30)
    stats = _compute_calibration(evals)

    # Also pull prebunking inoculation score if available
    inoculation_score = None
    try:
        with engine.connect() as conn:
            q = {"tok": session_token}
            clause = "WHERE session_token = :tok"
            if user_id:
                q = {"uid": user_id}
                clause = "WHERE user_id = :uid"
            row = conn.execute(sa.text(f"""
                SELECT
                    COUNT(*)                              AS total,
                    SUM(CASE WHEN correct=1 THEN 1 END)  AS correct_n
                FROM prebunking_attempts
                {clause}
            """), q).fetchone()
            if row and row.total:
                inoculation_score = round((row.correct_n or 0) / row.total * 100, 1)
    except Exception:
        pass   # prebunking_attempts table may not exist yet — graceful

    return {
        "calibration_score":  stats.get("calibration_score"),
        "accuracy":           stats.get("accuracy"),
        "total_evaluations":  stats.get("total", 0),
        "overconfident":      stats.get("overconfident", False),
        "underconfident":     stats.get("underconfident", False),
        "inoculation_score":  inoculation_score,
    }


# ── GET /calibration/peer/{evaluation_id} ────────────────────────────────────

@router.get("/calibration/peer/{evaluation_id}")
async def get_peer_breakdown(evaluation_id: int):
    """
    Peer label distribution for one specific evaluation.
    Exposed for the step-by-step comparison view (after user submits).
    """
    try:
        with engine.connect() as conn:
            # Get content_hash for this evaluation
            eval_row = conn.execute(sa.text(
                "SELECT content_hash, label FROM evaluations WHERE id = :eid"
            ), {"eid": evaluation_id}).fetchone()

            if not eval_row or not eval_row.content_hash:
                return {"peer_data": None, "reason": "No content hash for this evaluation."}

            rows = conn.execute(sa.text("""
                SELECT ue.user_label, COUNT(*) AS cnt
                FROM user_evaluations ue
                JOIN evaluations e ON e.id = ue.evaluation_id
                WHERE e.content_hash = :ch
                  AND ue.confidence_level IS NOT NULL
                GROUP BY ue.user_label
                ORDER BY cnt DESC
            """), {"ch": eval_row.content_hash}).fetchall()

            total = sum(r.cnt for r in rows)
            if total < 5:
                return {"peer_data": None, "reason": "Not enough raters yet for this content."}

            return {
                "evaluation_id": evaluation_id,
                "system_label":  eval_row.label,
                "total_raters":  total,
                "distribution":  [
                    {
                        "label":   r.user_label,
                        "count":   r.cnt,
                        "pct":     round(r.cnt / total * 100),
                    }
                    for r in rows
                ],
            }
    except Exception as e:
        logger.error(f"[Calibration] peer breakdown error: {e}")
        raise HTTPException(status_code=500, detail="Could not fetch peer data.")
