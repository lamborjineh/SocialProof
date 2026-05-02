"""
SocialProof — Router: Comparison  v3.0
routers/comparison.py

Endpoints:
  GET  /comparison/{user_evaluation_id}   — fetch stored comparison for a user evaluation
  POST /comparison/preview                — run compare() without persisting (for UI preview)

Implements System_Requirements §5.3 Comparison Step.
The comparison result is already computed and stored by routers/user_evaluation.py
when the user submits their evaluation.  This router only reads it back + offers
a preview mode for the step-by-step UI to show diff before final submission.
"""

from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Header, Request
from sqlalchemy.orm import Session

from config import logger
from database.models import engine, UserEvaluationORM, EvaluationORM
from routers.auth import get_current_user
from services.comparison import ComparisonEngine
from schemas import UserEvaluationRequest

router = APIRouter()


# ── GET /comparison/{user_evaluation_id} ─────────────────────────────────────

@router.get("/comparison/{user_evaluation_id}")
async def get_comparison_result(
    user_evaluation_id: int,
    req:                Request,
    session_token:      Optional[str] = None,
    authorization:      Optional[str] = Header(None),
):
    """
    Fetch the stored comparison result for a submitted user evaluation.

    Ownership guard: logged-in user must match evaluation's user_id, OR
    anonymous caller must supply the matching session_token.
    """
    db = Session(engine)
    try:
        ue_row = db.get(UserEvaluationORM, user_evaluation_id)
        if not ue_row:
            raise HTTPException(status_code=404, detail="User evaluation not found.")

        # Load parent evaluation for ownership check
        eval_row = db.get(EvaluationORM, ue_row.evaluation_id)
        if not eval_row:
            raise HTTPException(status_code=404, detail="Parent evaluation not found.")

        # Ownership check
        if authorization and authorization.startswith("Bearer "):
            current_user = get_current_user(req, authorization)
            if eval_row.user_id is not None and eval_row.user_id != current_user["sub"]:
                raise HTTPException(status_code=403, detail="Access denied.")
        elif session_token:
            if eval_row.session_token != session_token:
                raise HTTPException(status_code=403, detail="Access denied.")
        else:
            raise HTTPException(status_code=401, detail="Authentication required.")

        if not ue_row.comparison_json:
            raise HTTPException(
                status_code=404,
                detail="Comparison result not yet available for this evaluation.",
            )

        return {
            "user_evaluation_id": user_evaluation_id,
            "evaluation_id":      ue_row.evaluation_id,
            "comparison":         ue_row.comparison_json,
        }

    finally:
        db.close()


# ── POST /comparison/preview ──────────────────────────────────────────────────

@router.post("/comparison/preview")
async def preview_comparison(body: dict):
    """
    Run ComparisonEngine.compare() without persisting the result.

    Used by the step-by-step UI to show the user a live diff of their
    assessment vs the system before they confirm submission.

    Body:
        evaluation_id: int
        session_token:  str
        user_score:     int        (0–100)
        user_label:     str | None
        confidence_level: str | None
        bias_detected:  bool | None
        source_credible: str | None  ("yes" | "no" | "unsure")
        identified_claims: list[str] | None
        skipped_steps:  list[str] | None
    """
    evaluation_id = body.get("evaluation_id")
    session_token = body.get("session_token", "")

    if not evaluation_id:
        raise HTTPException(status_code=422, detail="evaluation_id is required.")

    db = Session(engine)
    try:
        eval_row = db.get(EvaluationORM, evaluation_id)
        if not eval_row:
            raise HTTPException(status_code=404, detail="Evaluation not found.")

        # Lightweight ownership — only session_token for preview (no auth required)
        if eval_row.session_token and eval_row.session_token != session_token:
            raise HTTPException(status_code=403, detail="Access denied.")

        system_result = eval_row.analysis_json or {}
        if not system_result:
            raise HTTPException(
                status_code=422,
                detail="Analysis not yet complete for this evaluation.",
            )

        # Build a temporary UserEvaluationRequest from the preview body
        try:
            user_eval = UserEvaluationRequest(
                evaluation_id      = evaluation_id,
                session_token      = session_token,
                user_score         = body.get("user_score"),
                user_label         = body.get("user_label"),
                confidence_level   = body.get("confidence_level"),
                bias_detected      = body.get("bias_detected"),
                source_credible    = body.get("source_credible"),
                identified_claims  = body.get("identified_claims"),
                skipped_steps      = body.get("skipped_steps") or [],
            )
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid preview body: {e}")

        comparison = ComparisonEngine.compare(user_eval, system_result)

        return {
            "evaluation_id": evaluation_id,
            "preview":       True,
            "comparison":    comparison,
        }

    finally:
        db.close()
