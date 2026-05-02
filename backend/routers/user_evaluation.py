"""
SocialProof — Router: User Evaluation & Re-Evaluation v3.2

Changes:
  - user_label normalisation (same ENUM fix as revised_label)
  - score_diff_label now populated in ComparisonResult
  - revision_trigger stored in re_evaluations
  - v3.2 FIX: triggered_lessons batch-enriched from DB before ComparisonResult
    construction. ComparisonEngine previously returned List[str] (lesson keys),
    which Pydantic rejected because ComparisonResult.triggered_lessons expects
    List[TriggeredLesson]. This caused a 422/500 on every /user-evaluation call,
    meaning lessons NEVER rendered and always showed "Great job — no gaps."
    Fix: single batch SELECT after compare(), build TriggeredLesson objects
    (key, title, topic, trigger_reason) before passing to ComparisonResult(**).
    _save_lessons() updated to extract keys from the enriched objects.
"""

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from config import logger
from database.models import engine, EvaluationORM, UserEvaluationORM, ReEvaluationORM
from schemas import (
    UserEvaluationRequest, UserEvaluationResponse,
    ReEvaluationRequest, ComparisonResult, TriggeredLesson,
)
from services.comparison import ComparisonEngine
from services.lesson_trigger import compute_triggers
from services.behavior_tracker import get_behavior_triggers

router = APIRouter()

# ── Label normalisation (MySQL ENUM enforcement) ──────────────────────────────
_LABEL_MAP = {
    "likely credible":   "Likely Credible",
    "credible":          "Likely Credible",
    "uncertain":         "Uncertain",
    "unsure":            "Uncertain",
    "likely misleading": "Likely Misleading",
    "misleading":        "Likely Misleading",
    "false":             "Likely Misleading",
}


def _normalise_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    normalised = _LABEL_MAP.get(raw.strip().lower())
    if normalised is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid label '{raw}'. "
                f"Accepted: 'Likely Credible', 'Uncertain', 'Likely Misleading' "
                f"(or shorthand: 'credible', 'uncertain', 'misleading')."
            ),
        )
    return normalised


# ── POST /user-evaluation ─────────────────────────────────────────────────────
@router.post("/user-evaluation", response_model=UserEvaluationResponse)
async def submit_user_evaluation(
    request: UserEvaluationRequest,
    background_tasks: BackgroundTasks,
):
    """
    Saves the user's guided evaluation answers, runs the ComparisonEngine,
    and returns triggered lessons + plain-language feedback.
    """
    db = Session(engine)
    try:
        eval_orm = db.get(EvaluationORM, request.evaluation_id)
        if not eval_orm:
            raise HTTPException(status_code=404, detail="Evaluation not found.")
        if eval_orm.analysis_json is None:
            raise HTTPException(
                status_code=409,
                detail="System analysis not yet complete for this evaluation.",
            )
        system_result = eval_orm.analysis_json
    finally:
        db.close()

    # Derive and normalise user_label
    user_label = _normalise_label(request.user_label)
    if not user_label and request.user_score is not None:
        user_label = (
            "Likely Credible"   if request.user_score >= 60 else
            "Uncertain"         if request.user_score >= 40 else
            "Likely Misleading"
        )

    db2 = Session(engine)
    user_eval_orm = UserEvaluationORM(
        evaluation_id     = request.evaluation_id,
        user_id           = request.user_id,
        identified_claims = request.identified_claims,
        source_credible   = request.source_credible,
        bias_detected     = 1 if request.bias_detected else 0,
        evidence_assessed = 1 if request.evidence_assessed else 0,
        user_score        = request.user_score,
        user_label        = user_label,
        confidence_level  = request.confidence_level,
        skipped_steps     = request.skipped_steps,
    )
    try:
        db2.add(user_eval_orm)
        db2.commit()
        db2.refresh(user_eval_orm)
        user_eval_id = user_eval_orm.id
    except Exception as e:
        logger.warning(f"User evaluation DB save failed: {e}")
        user_eval_id = 0
    finally:
        db2.close()

    comparison_data = ComparisonEngine.compare(request, system_result)

    # ── Fix (audit §HIGH-3): route all lesson triggering through compute_triggers() ──
    # ComparisonEngine.compare() no longer calls determine_lessons() (Path A keys).
    # We call compute_triggers() here with the full user_eval_data so it produces
    # Path B keys ("claim_detection_beginner", "source_verification_beginner", …)
    # that match the rows seeded in the lessons table.
    user_eval_data = {
        "skipped_steps":    request.skipped_steps or [],
        "confidence_level": request.confidence_level,
        "source_credible":  request.source_credible,
        "bias_detected":    request.bias_detected,
        "user_label":       user_label,
        "user_score":       request.user_score or 50,
        "user_id":          request.user_id,
    }
    db_for_skills = Session(engine)
    try:
        raw_triggers = compute_triggers(comparison_data, user_eval_data, db_session=db_for_skills)
    finally:
        db_for_skills.close()

    # v3: append behavioral pattern triggers (Modules 6/7/8)
    # These run on the same user_eval_data dict; history is fetched for always_unsure flag.
    history_rows: list = []
    if request.user_id:
        try:
            db_hist = Session(engine)
            hist_result = db_hist.execute(
                sa.text(
                    "SELECT confidence_level FROM user_evaluations "
                    "WHERE user_id = :uid ORDER BY submitted_at DESC LIMIT 10"
                ),
                {"uid": request.user_id},
            ).fetchall()
            history_rows = [dict(r._mapping) for r in hist_result]
            db_hist.close()
        except Exception as hist_err:
            logger.debug(f"[BehaviorTracker] History fetch skipped: {hist_err}")

    behavior_triggers = get_behavior_triggers(
        user_eval   = {
            **user_eval_data,
            "evidence_assessed":  request.evidence_assessed,
            "identified_claims":  request.identified_claims or [],
            "time_spent_seconds": getattr(request, "time_spent_seconds", None),
        },
        system_data = system_result,
        history     = history_rows or None,
    )
    raw_triggers = raw_triggers + behavior_triggers

    # raw_triggers is List[{lesson_key, trigger_reason, difficulty}] — Path B keys.
    raw_lesson_keys: list = [t["lesson_key"] for t in raw_triggers]
    # Build a trigger_reason lookup so enrichment can attach the right context text.
    trigger_reason_map: dict = {t["lesson_key"]: t["trigger_reason"] for t in raw_triggers}

    enriched_lessons: list = []
    if raw_lesson_keys:
        db_enrich = Session(engine)
        try:
            # Batch fetch — avoids N round-trips
            rows = db_enrich.execute(
                sa.text(
                    "SELECT lesson_key, title, topic "
                    "FROM lessons "
                    "WHERE lesson_key IN :keys"
                ).bindparams(sa.bindparam("keys", expanding=True)),
                {"keys": raw_lesson_keys},
            ).fetchall()
            lookup = {row.lesson_key: row for row in rows}
        except Exception as exc:
            logger.warning(f"Lesson DB enrichment fetch failed: {exc}")
            lookup = {}
        finally:
            db_enrich.close()

        for key in raw_lesson_keys:
            row = lookup.get(key)
            enriched_lessons.append(
                TriggeredLesson(
                    key=key,
                    title=row.title if row else key.replace("_", " ").title(),
                    topic=row.topic if row else "general",
                    trigger_reason=trigger_reason_map.get(key, ""),
                )
            )

    comparison_data["triggered_lessons"] = enriched_lessons

    # ── Background: persist triggered lessons to DB ───────────────────────────
    # Keys extracted from the already-enriched objects so _save_lessons stays
    # in sync with what was returned to the client.
    def _save_lessons():
        try:
            db3 = Session(engine)
            for lesson in enriched_lessons:
                row = db3.execute(
                    sa.text("SELECT id FROM lessons WHERE lesson_key = :key"),
                    {"key": lesson.key},
                ).fetchone()
                if row:
                    db3.execute(
                        sa.text("""
                            INSERT INTO lessons_triggered
                                (user_evaluation_id, lesson_id, trigger_reason, was_read)
                            VALUES (:uid, :lid, :reason, 0)
                        """),
                        {
                            "uid":    user_eval_id,
                            "lid":    row.id,
                            "reason": f"auto:{lesson.key}",
                        },
                    )
            db3.commit()
        except Exception as exc:
            logger.warning(f"Lesson trigger DB save failed: {exc}")
        finally:
            db3.close()

    background_tasks.add_task(_save_lessons)

    return UserEvaluationResponse(
        user_evaluation_id=user_eval_id,
        comparison=ComparisonResult(**comparison_data),
    )


# ── POST /re-evaluation ───────────────────────────────────────────────────────
@router.post("/re-evaluation")
async def submit_re_evaluation(request: ReEvaluationRequest):
    """
    Saves the user's revised judgment after seeing system feedback.
    Stores revision_trigger for §9 research metrics (why did they change?).
    """
    db = Session(engine)
    try:
        orm = ReEvaluationORM(
            user_evaluation_id = request.user_evaluation_id,
            revised_score      = request.revised_score,
            revised_label      = _normalise_label(request.revised_label),
            revised_confidence = request.revised_confidence,
            revision_notes     = request.revision_notes,
            revision_trigger   = request.revision_trigger,   # §5.6 — now stored
        )
        db.add(orm)
        db.commit()
        db.refresh(orm)
        return {"re_evaluation_id": orm.id, "ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
