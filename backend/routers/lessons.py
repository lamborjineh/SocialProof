"""
SocialProof — Router: Lessons
Endpoints:
  GET  /lessons                                — list all lessons (filterable)
  GET  /lessons/triggered/{user_evaluation_id} — lessons triggered for a user eval
  POST /lessons/mark-read/{lesson_trigger_id}  — mark a triggered lesson as read
  POST /lessons/{lesson_id}/read               — Fix #18: server-side read sync
  POST /lessons/complete                       — v3: write to lesson_completions table
"""

from datetime import datetime
from typing import Optional, Dict, Any

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Header
from sqlalchemy.orm import Session

from database.models import engine
from schemas import LessonCompletionRequest, LessonCompletionResponse

router = APIRouter()


@router.get("/lessons")
async def get_lessons(
    topic:      Optional[str] = Query(None, description="Filter by topic"),
    difficulty: Optional[str] = Query(None, description="beginner | intermediate | advanced"),
):
    """
    Return all lessons with optional filtering by topic and difficulty.
    Powers the Learn page (learn.html).
    """
    db = Session(engine)
    try:
        query  = "SELECT * FROM lessons"
        params: Dict[str, Any] = {}
        wheres = []
        if topic:
            wheres.append("topic = :topic")
            params["topic"] = topic
        if difficulty:
            wheres.append("difficulty = :difficulty")
            params["difficulty"] = difficulty
        if wheres:
            query += " WHERE " + " AND ".join(wheres)
        query += " ORDER BY topic, difficulty"
        rows = db.execute(sa.text(query), params).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/lessons/triggered/{user_evaluation_id}")
async def get_triggered_lessons(user_evaluation_id: int):
    """Return all lessons triggered for a specific user evaluation."""
    db = Session(engine)
    try:
        rows = db.execute(
            sa.text("""
                SELECT l.*, lt.id AS trigger_id, lt.trigger_reason, lt.was_read
                FROM lessons_triggered lt
                JOIN lessons l ON l.id = lt.lesson_id
                WHERE lt.user_evaluation_id = :uid
            """),
            {"uid": user_evaluation_id},
        ).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.post("/lessons/mark-read/{lesson_trigger_id}")
async def mark_lesson_read(lesson_trigger_id: int):
    """Mark a triggered lesson as read (for completion tracking)."""
    db = Session(engine)
    try:
        db.execute(
            sa.text("UPDATE lessons_triggered SET was_read = 1 WHERE id = :id"),
            {"id": lesson_trigger_id},
        )
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/lessons/{lesson_id}/read")
async def mark_lesson_read_by_id(lesson_id: int, authorization: str = Header(None)):
    """Fix #18 — server-side read sync by lesson_id."""
    if not authorization or not authorization.startswith("Bearer "):
        return {"status": "skipped", "reason": "unauthenticated"}
    db = Session(engine)
    try:
        db.execute(
            sa.text("UPDATE lessons_triggered SET was_read = 1 WHERE lesson_id = :lid"),
            {"lid": lesson_id},
        )
        db.commit()
        return {"status": "ok", "lesson_id": lesson_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@router.post("/lessons/complete", response_model=LessonCompletionResponse)
async def complete_lesson(request: LessonCompletionRequest):
    """
    v3 — Record that a user has completed a lesson.
    Writes to lesson_completions (the permanent per-user record) and also
    marks the most recent lessons_triggered row for this lesson as read.

    Both user_id (authenticated) and session_token (anonymous) are accepted.
    At least one must be provided.
    """
    if not request.user_id and not request.session_token:
        raise HTTPException(
            status_code=422,
            detail="Either user_id or session_token is required."
        )

    db = Session(engine)
    try:
        # Verify lesson exists
        lesson = db.execute(
            sa.text("SELECT id FROM lessons WHERE id = :lid"),
            {"lid": request.lesson_id},
        ).fetchone()
        if not lesson:
            raise HTTPException(status_code=404, detail="Lesson not found.")

        now = datetime.utcnow()

        # Write to lesson_completions
        db.execute(
            sa.text("""
                INSERT INTO lesson_completions (user_id, session_token, lesson_id, completed_at)
                VALUES (:uid, :tok, :lid, :now)
            """),
            {
                "uid": request.user_id,
                "tok": request.session_token,
                "lid": request.lesson_id,
                "now": now,
            },
        )

        # Also mark was_read on any open lessons_triggered rows for this lesson
        if request.user_id:
            db.execute(
                sa.text("""
                    UPDATE lessons_triggered lt
                    JOIN user_evaluations ue ON ue.id = lt.user_evaluation_id
                    SET lt.was_read = 1
                    WHERE lt.lesson_id = :lid AND ue.user_id = :uid AND lt.was_read = 0
                """),
                {"lid": request.lesson_id, "uid": request.user_id},
            )
        else:
            db.execute(
                sa.text("""
                    UPDATE lessons_triggered lt
                    JOIN user_evaluations ue ON ue.id = lt.user_evaluation_id
                    JOIN evaluations e ON e.id = ue.evaluation_id
                    SET lt.was_read = 1
                    WHERE lt.lesson_id = :lid AND e.session_token = :tok AND lt.was_read = 0
                """),
                {"lid": request.lesson_id, "tok": request.session_token},
            )

        db.commit()

        return LessonCompletionResponse(
            lesson_id    = request.lesson_id,
            completed_at = now.isoformat(),
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
