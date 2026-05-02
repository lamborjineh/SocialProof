"""
SocialProof — Router: User Dashboard  (v3 rebuild)
GET /dashboard/{user_id}

Returns:
  stats           — total evaluations, lessons completed, challenge streak
  skill_progress  — per-topic MIL skill level + quiz accuracy (from user_skill_progress)
  behavior_cards  — derived behavior insight cards from lesson_triggers patterns
  lesson_triggers — top weakness topics (for bar chart)
  history         — recent evaluations (user's own judgments only — no AI scores shown)

v3 Design Rule enforced: system_score is NEVER returned to the frontend.
"""

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session

from config import logger
from database.models import engine
from routers.auth import get_current_user

router = APIRouter()


# ── Behaviour insight card templates ─────────────────────────────────────────
# Derived from lesson_triggers patterns — no behaviour_tracker.py required.
_BEHAVIOR_INSIGHTS = {
    "source_verification": {
        "flag":    "trusts_sources_unchecked",
        "title":   "Source checking gap",
        "body":    "You've been prompted to re-examine sources several times. Practise running a quick domain check before trusting a claim.",
        "mil_skill": "Evaluate & Assess",
        "action":  "lessons.html#evaluating-sources",
        "action_label": "Review: Evaluating Sources →",
    },
    "bias_detection": {
        "flag":    "trusts_emotional",
        "title":   "Emotional language slips through",
        "body":    "Emotionally charged framing is appearing in content you've marked as credible. Recognising loaded language is one of the highest-leverage MIL skills.",
        "mil_skill": "Evaluate & Assess",
        "action":  "lessons.html#bias-emotional-language",
        "action_label": "Review: Bias & Emotional Language →",
    },
    "claim_detection": {
        "flag":    "skips_claims",
        "title":   "Claim identification step skipped",
        "body":    "Isolating exactly what is being claimed is the first step in any fact-check. Skipping it means reasoning on the wrong thing.",
        "mil_skill": "Understand",
        "action":  "lessons.html#what-is-a-claim",
        "action_label": "Review: What Is a Claim? →",
    },
    "evidence_evaluation": {
        "flag":    "skips_evidence",
        "title":   "Evidence step skipped",
        "body":    "You've bypassed the evidence step in several sessions. Evidence quality — not just presence — determines how much weight a claim deserves.",
        "mil_skill": "Evaluate & Assess",
        "action":  "lessons.html#reading-evidence",
        "action_label": "Review: Reading Evidence →",
    },
    "general": {
        "flag":    "overconfident",
        "title":   "High confidence, varied outcomes",
        "body":    "Your confidence level is often high — but your verdicts don't always align with the evidence signals. Calibrated scepticism is the goal, not certainty.",
        "mil_skill": "Reflect",
        "action":  "lessons.html#putting-it-together",
        "action_label": "Review: Putting It Together →",
    },
}

_TOPIC_DISPLAY = {
    "claim_detection":    "Claim Detection",
    "source_verification": "Source Verification",
    "bias_detection":     "Bias Detection",
    "evidence_evaluation": "Evidence Evaluation",
    "general":            "General MIL",
}

_LEVEL_ORDER = {"beginner": 0, "intermediate": 1, "advanced": 2}


@router.get("/dashboard/{user_id}")
async def get_dashboard(user_id: int, request: Request):
    try:
        payload = get_current_user(request, request.headers.get("authorization"))
        if payload["sub"] != user_id and payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Forbidden.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    db = Session(engine)
    try:

        # ── 1. Stats ──────────────────────────────────────────────────────────
        total_evals = db.execute(sa.text(
            "SELECT COUNT(*) FROM user_evaluations WHERE user_id = :uid"
        ), {"uid": user_id}).scalar() or 0

        lessons_done = db.execute(sa.text(
            "SELECT COUNT(*) FROM lesson_completions WHERE user_id = :uid"
        ), {"uid": user_id}).scalar() or 0

        # Challenge streak: consecutive days with a quiz_attempt (best-effort)
        streak = 0
        try:
            streak_rows = db.execute(sa.text("""
                SELECT DATE(attempted_at) AS d
                FROM quiz_attempts
                WHERE user_id = :uid
                GROUP BY DATE(attempted_at)
                ORDER BY d DESC
                LIMIT 30
            """), {"uid": user_id}).fetchall()
            if streak_rows:
                from datetime import date, timedelta
                today = date.today()
                streak = 0
                for i, row in enumerate(streak_rows):
                    expected = today - timedelta(days=i)
                    if row.d == expected:
                        streak += 1
                    else:
                        break
        except Exception:
            streak = 0

        stats = {
            "total_evaluations": total_evals,
            "lessons_completed": lessons_done,
            "challenge_streak":  streak,
        }

        # ── 2. Skill progress (from user_skill_progress) ──────────────────────
        skill_rows = db.execute(sa.text("""
            SELECT topic, current_level, quiz_accuracy_pct, lessons_completed
            FROM user_skill_progress
            WHERE user_id = :uid
        """), {"uid": user_id}).fetchall()

        skill_map = {r.topic: r for r in skill_rows}
        skill_progress = []
        for topic in ["claim_detection", "source_verification", "bias_detection",
                      "evidence_evaluation", "general"]:
            row = skill_map.get(topic)
            skill_progress.append({
                "topic":            topic,
                "display_name":     _TOPIC_DISPLAY.get(topic, topic),
                "current_level":    row.current_level if row else "beginner",
                "level_index":      _LEVEL_ORDER.get(row.current_level if row else "beginner", 0),
                "quiz_accuracy_pct": round(row.quiz_accuracy_pct) if (row and row.quiz_accuracy_pct) else None,
                "lessons_completed": row.lessons_completed if row else 0,
            })

        # ── 3. Lesson triggers (weakness bars + behavior card derivation) ──────
        trigger_rows = db.execute(sa.text("""
            SELECT l.topic, COUNT(*) AS trigger_count
            FROM lessons_triggered lt
            JOIN user_evaluations ue ON ue.id = lt.user_evaluation_id
            JOIN lessons l ON l.id = lt.lesson_id
            WHERE ue.user_id = :uid
            GROUP BY l.topic
            ORDER BY trigger_count DESC
            LIMIT 5
        """), {"uid": user_id}).fetchall()

        lesson_triggers = [
            {"topic": r.topic, "trigger_count": r.trigger_count,
             "display_name": _TOPIC_DISPLAY.get(r.topic, r.topic)}
            for r in trigger_rows
        ]

        # ── 4. Behavior insight cards ─────────────────────────────────────────
        # Surface a card for any topic triggered 2+ times, up to 3 cards.
        behavior_cards = []
        for tr in trigger_rows:
            if tr.trigger_count >= 2 and tr.topic in _BEHAVIOR_INSIGHTS:
                tmpl = _BEHAVIOR_INSIGHTS[tr.topic]
                behavior_cards.append({
                    "flag":         tmpl["flag"],
                    "title":        tmpl["title"],
                    "body":         tmpl["body"],
                    "mil_skill":    tmpl["mil_skill"],
                    "action":       tmpl["action"],
                    "action_label": tmpl["action_label"],
                    "trigger_count": tr.trigger_count,
                })
            if len(behavior_cards) >= 3:
                break

        # ── 5. Evaluation history (user's own judgments — no AI scores) ───────
        history_rows = db.execute(sa.text("""
            SELECT e.id          AS eval_id,
                   e.raw_content AS content_preview,
                   e.created_at,
                   ue.user_score,
                   ue.user_label,
                   ue.confidence_level
            FROM user_evaluations ue
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE ue.user_id = :uid
            ORDER BY e.created_at DESC
            LIMIT 20
        """), {"uid": user_id}).fetchall()

        history = []
        for r in history_rows:
            preview = (r.content_preview or "")[:80]
            if len(r.content_preview or "") > 80:
                preview += "…"
            history.append({
                "eval_id":         r.eval_id,
                "content_preview": preview,
                "user_score":      r.user_score,
                "user_label":      r.user_label,
                "confidence_level": r.confidence_level,
                "created_at":      r.created_at.isoformat() if r.created_at else None,
            })

        return {
            "stats":           stats,
            "skill_progress":  skill_progress,
            "behavior_cards":  behavior_cards,
            "lesson_triggers": lesson_triggers,
            "history":         history,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[Dashboard] Error for user {user_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Dashboard query failed.")
    finally:
        db.close()
