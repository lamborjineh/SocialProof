"""
SocialProof — Router: Prebunking Lab  v1.0
routers/prebunking.py

Endpoints:
  GET  /prebunking/modules         — list all techniques + user completion state
  POST /prebunking/attempt         — record a technique attempt (correct/incorrect)
  GET  /prebunking/stats           — aggregated inoculation score for a user/session
  GET  /prebunking/techniques      — static technique definitions (for SPA hydration)

Implements MIL Layer: Inoculation Training (prebunking.html)
Technique completions are stored in prebunking_attempts table.
Inoculation score = (correct / total_attempted) × 100, shown on calibration dashboard.
"""

from typing import Optional, List
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import logger
from database.models import engine

router = APIRouter()

# ── Technique registry (mirrors frontend TECHNIQUES array) ────────────────────
# Keep in sync with prebunking.html TECHNIQUES ids
TECHNIQUE_IDS = [
    "emotional_override",
    "false_authority",
    "cherry_pick",
    "false_dichotomy",
    "conspiracy_framing",
    "impersonation",
]

# ── Pydantic models ───────────────────────────────────────────────────────────

class PrebunkingAttemptRequest(BaseModel):
    session_token: str
    user_id:       Optional[int] = None
    technique_id:  str
    correct:       bool


class PrebunkingAttemptResponse(BaseModel):
    technique_id:       str
    correct:            bool
    inoculation_score:  Optional[float] = None   # updated score after this attempt
    techniques_done:    int
    techniques_total:   int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_table():
    """
    Create prebunking_attempts table if it doesn't exist yet.
    Called lazily on first request so it doesn't block startup.
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS prebunking_attempts (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        session_token   VARCHAR(128) NOT NULL,
        user_id         INT          NULL,
        technique_id    VARCHAR(64)  NOT NULL,
        correct         TINYINT(1)   NOT NULL DEFAULT 0,
        attempted_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_session_tech (session_token, technique_id),
        INDEX idx_pb_user (user_id),
        INDEX idx_pb_session (session_token)
    )
    """
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(ddl))
    except Exception as e:
        logger.warning(f"[Prebunking] Could not ensure table: {e}")


def _get_completions(session_token: str, user_id: Optional[int]):
    """Return all attempt rows for a session/user."""
    try:
        with engine.connect() as conn:
            if user_id:
                rows = conn.execute(sa.text("""
                    SELECT technique_id, correct, attempted_at
                    FROM prebunking_attempts
                    WHERE user_id = :uid
                    ORDER BY attempted_at DESC
                """), {"uid": user_id}).fetchall()
            else:
                rows = conn.execute(sa.text("""
                    SELECT technique_id, correct, attempted_at
                    FROM prebunking_attempts
                    WHERE session_token = :tok
                    ORDER BY attempted_at DESC
                """), {"tok": session_token}).fetchall()
        return [{"technique_id": r[0], "correct": bool(r[1]), "attempted_at": str(r[2])} for r in rows]
    except Exception as e:
        logger.debug(f"[Prebunking] _get_completions error: {e}")
        return []


def _compute_score(completions: list) -> Optional[float]:
    if not completions:
        return None
    correct = sum(1 for c in completions if c["correct"])
    return round((correct / len(completions)) * 100, 1)


# ── GET /prebunking/modules ───────────────────────────────────────────────────

@router.get("/prebunking/modules")
async def get_prebunking_modules(
    session_token: str,
    user_id:       Optional[int] = None,
):
    """
    Returns the list of technique IDs with the user's completion state for each.
    The frontend uses this to restore progress across sessions.
    """
    _ensure_table()
    completions = _get_completions(session_token, user_id)
    completed_map = {c["technique_id"]: c for c in completions}

    modules = []
    for tid in TECHNIQUE_IDS:
        comp = completed_map.get(tid)
        modules.append({
            "technique_id": tid,
            "phase":        "done" if comp else "vaccine",
            "correct":      comp["correct"] if comp else None,
            "attempted_at": comp["attempted_at"] if comp else None,
        })

    return {
        "modules":          modules,
        "completions":      completions,
        "inoculation_score": _compute_score(completions),
        "techniques_done":  len(completed_map),
        "techniques_total": len(TECHNIQUE_IDS),
    }


# ── POST /prebunking/attempt ──────────────────────────────────────────────────

@router.post("/prebunking/attempt", response_model=PrebunkingAttemptResponse)
async def record_prebunking_attempt(body: PrebunkingAttemptRequest):
    """
    Record that a user completed the exercise phase for a technique.
    Uses INSERT … ON DUPLICATE KEY UPDATE so re-attempts overwrite the previous.
    Returns the updated inoculation score.
    """
    _ensure_table()

    if body.technique_id not in TECHNIQUE_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown technique_id '{body.technique_id}'. "
                   f"Valid: {TECHNIQUE_IDS}"
        )

    try:
        with engine.begin() as conn:
            conn.execute(sa.text("""
                INSERT INTO prebunking_attempts
                    (session_token, user_id, technique_id, correct, attempted_at)
                VALUES
                    (:tok, :uid, :tid, :correct, :now)
                ON DUPLICATE KEY UPDATE
                    correct      = VALUES(correct),
                    attempted_at = VALUES(attempted_at),
                    user_id      = COALESCE(VALUES(user_id), user_id)
            """), {
                "tok":     body.session_token,
                "uid":     body.user_id,
                "tid":     body.technique_id,
                "correct": int(body.correct),
                "now":     datetime.utcnow(),
            })
    except Exception as e:
        logger.error(f"[Prebunking] Failed to record attempt: {e}")
        raise HTTPException(status_code=500, detail="Could not record attempt.")

    completions = _get_completions(body.session_token, body.user_id)
    score       = _compute_score(completions)
    done        = len({c["technique_id"] for c in completions})

    logger.info(
        f"[Prebunking] technique={body.technique_id} correct={body.correct} "
        f"session={body.session_token[:8]}… score={score}"
    )

    return PrebunkingAttemptResponse(
        technique_id      = body.technique_id,
        correct           = body.correct,
        inoculation_score = score,
        techniques_done   = done,
        techniques_total  = len(TECHNIQUE_IDS),
    )


# ── GET /prebunking/stats ─────────────────────────────────────────────────────

@router.get("/prebunking/stats")
async def get_prebunking_stats(
    session_token: str,
    user_id:       Optional[int] = None,
):
    """
    Aggregated prebunking performance.
    Used by dashboard.html to show the inoculation score widget.
    """
    _ensure_table()
    completions = _get_completions(session_token, user_id)
    correct = [c for c in completions if c["correct"]]
    wrong   = [c for c in completions if not c["correct"]]

    return {
        "inoculation_score": _compute_score(completions),
        "techniques_done":   len(completions),
        "techniques_total":  len(TECHNIQUE_IDS),
        "correct":           len(correct),
        "incorrect":         len(wrong),
        "completed_ids":     [c["technique_id"] for c in completions],
        "weakest_technique": wrong[-1]["technique_id"] if wrong else None,
    }


# ── GET /prebunking/techniques ────────────────────────────────────────────────

@router.get("/prebunking/techniques")
async def list_techniques():
    """
    Static list of technique metadata for frontend hydration.
    Kept server-side so the list can be extended without a frontend deploy.
    """
    return {
        "techniques": [
            {"id": "emotional_override",  "name": "Emotional Override",         "module": 6},
            {"id": "false_authority",     "name": "False Authority",            "module": 6},
            {"id": "cherry_pick",         "name": "Cherry-Picked Statistics",   "module": 7},
            {"id": "false_dichotomy",     "name": "False Dichotomy",            "module": 7},
            {"id": "conspiracy_framing",  "name": "Conspiracy Framing",         "module": 8},
            {"id": "impersonation",       "name": "Source Impersonation",       "module": 8},
        ]
    }
