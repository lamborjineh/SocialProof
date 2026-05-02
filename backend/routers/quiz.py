"""
SocialProof — Router: Quiz & Practice
Endpoints:
  GET  /quiz                    — fetch randomised questions (filterable by topic)
  POST /quiz/attempt            — record a quiz attempt + return immediate feedback
  GET  /quiz/stats/{user_id}    — per-topic accuracy summary for a logged-in user

Implements System_Requirements §5.7 Quiz and Practice Module.
"""
from config import logger, QUIZ_QUESTIONS_PER_SESSION

from typing import Optional, Dict, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from database.models import engine, QuizQuestionORM, QuizAttemptORM, UserSkillProgressORM
from schemas import QuizAttemptRequest, QuizAttemptResponse

router = APIRouter()


@router.get("/quiz/settings")
async def get_quiz_settings():
    """
    Return public quiz configuration so the frontend can display
    the correct session limit without hard-coding it.
    Admins change QUIZ_QUESTIONS_PER_SESSION in .env to adjust.
    """
    return {"questions_per_session": QUIZ_QUESTIONS_PER_SESSION}


@router.get("/quiz")
async def get_quiz_questions(
    topic: Optional[str] = Query(None, description="Filter by topic"),
    limit: int           = Query(QUIZ_QUESTIONS_PER_SESSION, ge=1, le=50, description="Number of questions"),
):
    """
    Return randomly sampled quiz questions.
    Questions are shuffled each time for variety.
    """
    db = Session(engine)
    try:
        query  = "SELECT * FROM quiz_questions"
        params: Dict[str, Any] = {}
        if topic:
            query += " WHERE topic = :topic"
            params["topic"] = topic
        query += " ORDER BY RAND() LIMIT :limit"
        params["limit"] = limit
        rows = db.execute(sa.text(query), params).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error(f"[Quiz] error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred.")
    finally:
        db.close()


@router.post("/quiz/attempt", response_model=QuizAttemptResponse)
async def submit_quiz_attempt(request: QuizAttemptRequest):
    """
    Record a quiz attempt, return immediate correctness feedback,
    and update user_skill_progress if the user has hit the advancement threshold.

    Skill advancement rules (per topic):
      beginner     → intermediate : accuracy_pct >= 70 over last 10 attempts
      intermediate → advanced     : accuracy_pct >= 80 over last 10 attempts
    """
    db = Session(engine)
    try:
        question = db.get(QuizQuestionORM, request.question_id)
        if not question:
            raise HTTPException(status_code=404, detail="Question not found.")

        is_correct = int(request.selected_index == question.correct_index)
        db.add(QuizAttemptORM(
            user_id        = request.user_id,
            question_id    = request.question_id,
            selected_index = request.selected_index,
            is_correct     = is_correct,
        ))
        db.commit()

        # ── Skill progress update (authenticated users only) ──────────────────
        if request.user_id:
            _maybe_advance_skill(db, request.user_id, question.topic)

        return QuizAttemptResponse(
            is_correct    = bool(is_correct),
            correct_index = question.correct_index,
            explanation   = question.explanation,
            topic         = question.topic,
            difficulty    = question.difficulty,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Quiz] error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred.")
    finally:
        db.close()


# ── Skill advancement helper ──────────────────────────────────────────────────

_LEVEL_ORDER = ["beginner", "intermediate", "advanced"]
_ADVANCE_THRESHOLD = {
    "beginner":     70,   # accuracy_pct needed to move to intermediate
    "intermediate": 80,   # accuracy_pct needed to move to advanced
}
_WINDOW = 10   # rolling window of recent attempts per topic


def _maybe_advance_skill(db: Session, user_id: int, topic: str) -> None:
    """
    Recalculate rolling accuracy for this user+topic and advance
    current_level in user_skill_progress if the threshold is met.
    Creates the row if it doesn't exist yet.
    """
    try:
        # Rolling accuracy over last _WINDOW attempts for this topic
        rows = db.execute(
            sa.text("""
                SELECT qa.is_correct
                FROM quiz_attempts qa
                JOIN quiz_questions qq ON qq.id = qa.question_id
                WHERE qa.user_id = :uid AND qq.topic = :topic
                  AND qa.question_id > 0
                ORDER BY qa.attempted_at DESC
                LIMIT :window
            """),
            {"uid": user_id, "topic": topic, "window": _WINDOW},
        ).fetchall()

        if not rows:
            return

        accuracy = round(sum(r.is_correct for r in rows) / len(rows) * 100)

        # Get or create skill progress row
        prog = db.execute(
            sa.text(
                "SELECT id, current_level, lessons_completed FROM user_skill_progress "
                "WHERE user_id = :uid AND topic = :topic LIMIT 1"
            ),
            {"uid": user_id, "topic": topic},
        ).fetchone()

        if prog is None:
            db.execute(
                sa.text("""
                    INSERT INTO user_skill_progress
                        (user_id, topic, current_level, quiz_accuracy_pct, lessons_completed)
                    VALUES (:uid, :topic, 'beginner', :acc, 0)
                """),
                {"uid": user_id, "topic": topic, "acc": accuracy},
            )
            db.commit()
            return

        current_level = prog.current_level
        threshold     = _ADVANCE_THRESHOLD.get(current_level)

        # Always update accuracy regardless of advancement
        new_level = current_level
        if threshold is not None and accuracy >= threshold:
            idx = _LEVEL_ORDER.index(current_level)
            if idx < len(_LEVEL_ORDER) - 1:
                new_level = _LEVEL_ORDER[idx + 1]
                logger.info(
                    f"[SkillProgress] user={user_id} topic={topic} "
                    f"{current_level} → {new_level} (accuracy={accuracy}%)"
                )

        db.execute(
            sa.text("""
                UPDATE user_skill_progress
                SET current_level = :level, quiz_accuracy_pct = :acc
                WHERE user_id = :uid AND topic = :topic
            """),
            {"level": new_level, "acc": accuracy, "uid": user_id, "topic": topic},
        )
        db.commit()

    except Exception as e:
        logger.warning(f"[SkillProgress] Update failed for user={user_id} topic={topic}: {e}")


@router.get("/quiz/stats/{user_id}")
async def get_quiz_stats(user_id: int):
    """Return per-topic quiz performance summary for a logged-in user."""
    db = Session(engine)
    try:
        rows = db.execute(
            sa.text("""
                SELECT
                    qq.topic,
                    COUNT(*)                     AS topic_attempts,
                    SUM(qa.is_correct)           AS topic_correct,
                    ROUND(AVG(qa.is_correct)*100) AS accuracy_pct
                FROM quiz_attempts qa
                JOIN quiz_questions qq ON qq.id = qa.question_id
                WHERE qa.user_id = :uid
                GROUP BY qq.topic
            """),
            {"uid": user_id},
        ).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error(f"[Quiz] error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred.")
    finally:
        db.close()


# ── Pre-test / Post-test (Fix #11 — thesis baseline measurement) ──────────────
# These 5 claims span the three FEVER-style labels and are intentionally
# ambiguous enough to reveal pre/post accuracy changes.
_PRETEST_CLAIMS = [
    {"id": 1, "text": "The Philippines is the most populous country in Southeast Asia.",        "answer": "False"},
    {"id": 2, "text": "The COVID-19 vaccine was developed in under one year.",                   "answer": "True"},
    {"id": 3, "text": "Facebook was founded in 2004 by Mark Zuckerberg.",                       "answer": "True"},
    {"id": 4, "text": "The Great Wall of China is visible from space with the naked eye.",      "answer": "False"},
    {"id": 5, "text": "Telemundo is an English-language television network.",                   "answer": "False"},
]


@router.get("/quiz/pretest")
async def get_pretest():
    """
    Return the 5-item pre-test claim set.
    Used to measure baseline media literacy before system interaction.
    Pair with POST /quiz/pretest/submit to record answers,
    then compare against POST /quiz/posttest/submit after system use.
    """
    return {"claims": [{"id": c["id"], "text": c["text"]} for c in _PRETEST_CLAIMS]}


@router.post("/quiz/pretest/submit")
async def submit_pretest(payload: dict):
    """
    Record pre-test answers and return the score.

    Body: {"user_id": int (optional), "session_token": str, "answers": {"1": "True", ...}}

    Writes to pretest_results table (not quiz_attempts — the negative-ID hack is retired).
    """
    answers     = payload.get("answers", {})
    session_tok = payload.get("session_token", "anonymous")
    user_id     = payload.get("user_id")

    correct = 0
    results = []
    for claim in _PRETEST_CLAIMS:
        user_ans   = answers.get(str(claim["id"]), "").strip()
        is_correct = user_ans.lower() == claim["answer"].lower()
        if is_correct:
            correct += 1
        results.append({
            "id":             claim["id"],
            "text":           claim["text"],
            "your_answer":    user_ans,
            "correct_answer": claim["answer"],
            "is_correct":     is_correct,
        })

    score_pct = round(correct / len(_PRETEST_CLAIMS) * 100)

    try:
        db = Session(engine)
        db.execute(
            sa.text("""
                INSERT INTO pretest_results
                    (user_id, session_token, phase, score_pct, correct, total)
                VALUES (:uid, :tok, 'pretest', :score, :correct, :total)
            """),
            {
                "uid":     user_id,
                "tok":     session_tok,
                "score":   score_pct,
                "correct": correct,
                "total":   len(_PRETEST_CLAIMS),
            },
        )
        db.commit()
    except Exception as e:
        logger.warning(f"[Pretest] DB write failed: {e}")
    finally:
        try:
            db.close()
        except Exception:
            pass

    return {
        "score_pct":     score_pct,
        "correct":       correct,
        "total":         len(_PRETEST_CLAIMS),
        "results":       results,
        "session_token": session_tok,
        "phase":         "pretest",
    }


@router.post("/quiz/posttest/submit")
async def submit_posttest(payload: dict):
    """
    Record post-test answers using the same 5 claims and return score + delta.

    Body: {"user_id": int (optional), "session_token": str, "answers": {"1": "True", ...},
           "pretest_score": int (optional, for inline delta calculation)}

    Writes to pretest_results (phase='posttest'). Delta is calculated against
    the most recent pretest_results row for this session/user if pretest_score
    is not supplied inline.
    """
    answers     = payload.get("answers", {})
    session_tok = payload.get("session_token", "anonymous")
    user_id     = payload.get("user_id")
    pre_score   = payload.get("pretest_score")

    correct = 0
    results = []
    for claim in _PRETEST_CLAIMS:
        user_ans   = answers.get(str(claim["id"]), "").strip()
        is_correct = user_ans.lower() == claim["answer"].lower()
        if is_correct:
            correct += 1
        results.append({
            "id":             claim["id"],
            "text":           claim["text"],
            "your_answer":    user_ans,
            "correct_answer": claim["answer"],
            "is_correct":     is_correct,
        })

    score_pct = round(correct / len(_PRETEST_CLAIMS) * 100)

    # Look up pretest score from DB if not supplied inline
    if pre_score is None:
        try:
            db = Session(engine)
            row = db.execute(
                sa.text("""
                    SELECT score_pct FROM pretest_results
                    WHERE phase = 'pretest'
                    AND (user_id = :uid OR session_token = :tok)
                    ORDER BY submitted_at DESC LIMIT 1
                """),
                {"uid": user_id, "tok": session_tok},
            ).fetchone()
            if row:
                pre_score = row.score_pct
            db.close()
        except Exception:
            pass

    delta = (score_pct - pre_score) if pre_score is not None else None

    # Persist posttest result
    try:
        db = Session(engine)
        db.execute(
            sa.text("""
                INSERT INTO pretest_results
                    (user_id, session_token, phase, score_pct, correct, total)
                VALUES (:uid, :tok, 'posttest', :score, :correct, :total)
            """),
            {
                "uid":     user_id,
                "tok":     session_tok,
                "score":   score_pct,
                "correct": correct,
                "total":   len(_PRETEST_CLAIMS),
            },
        )
        db.commit()
    except Exception as e:
        logger.warning(f"[Posttest] DB write failed: {e}")
    finally:
        try:
            db.close()
        except Exception:
            pass

    return {
        "score_pct": score_pct,
        "correct":   correct,
        "total":     len(_PRETEST_CLAIMS),
        "results":   results,
        "phase":     "posttest",
        "delta":     delta,
        "improved":  (delta > 0) if delta is not None else None,
    }
