"""
SocialProof — LessonTriggerService
services/lesson_trigger.py

Separated from services/comparison.py per docx §5.2.
Converts ComparisonEngine output into a list of lesson triggers with
the correct difficulty level for the user's current skill progress.

compute_triggers() is the single public entry point.
It applies the 7 trigger rules from docx §4.1 Step 6:

  Rule 1 — Skipped claim identification step
  Rule 2 — Rated source differently from system (medium/high confidence)
  Rule 3 — Did not detect bias that system found
  Rule 4 — Skipped evidence assessment step
  Rule 5 — Score diverged by 30+ points from system score
  Rule 6 — High confidence + wrong label (overconfidence)
  Rule 7 — Any re-evaluation requested
  Rule 8 — behavior_tracker flag: skips_source_check  → Module 8 (Responsible Sharing)
  Rule 9 — behavior_tracker flag: trusts_emotional     → Module 6 (How Fake News is Made)
  Rule 10 — behavior_tracker flag: headline_only       → Module 7 (Types of Misinformation)

Called from routers/user_evaluation.py — behavior_tracker.get_behavior_triggers() feeds
flags into this function via user_eval_data["behavior_flags"].

Called from routers/user_evaluation.py after ComparisonEngine.compare().
Results are inserted into lessons_triggered.
"""

from __future__ import annotations

from typing import Optional, List, Dict, Any

import sqlalchemy as sa
from sqlalchemy.orm import Session


# ── Trigger rules ─────────────────────────────────────────────────────────────

def compute_triggers(
    comparison_result: Dict[str, Any],
    user_eval_data:    Dict[str, Any],
    db_session:        Optional[Session] = None,
) -> List[Dict[str, str]]:
    """
    Convert a ComparisonEngine result into a list of lesson trigger objects.

    Args:
        comparison_result: Output from ComparisonEngine.compare().
        user_eval_data:    Dict with keys:
            - skipped_steps     List[str]
            - confidence_level  str | None
            - source_credible   str | None   ("yes" | "no" | "unsure")
            - bias_detected     bool | None
            - user_label        str | None
            - user_score        int
        db_session:        Optional open SQLAlchemy Session for reading
                           user_skill_progress to pick the right difficulty.
                           If None, defaults to "beginner".

    Returns:
        List of dicts: [{lesson_key, trigger_reason, difficulty}]
    """
    triggers: List[Dict[str, str]] = []

    skipped_steps    = user_eval_data.get("skipped_steps") or []
    confidence       = user_eval_data.get("confidence_level")
    source_credible  = user_eval_data.get("source_credible")
    bias_detected    = user_eval_data.get("bias_detected")
    user_label       = user_eval_data.get("user_label")
    user_score       = user_eval_data.get("user_score", 50)
    user_id          = user_eval_data.get("user_id")

    system_label     = comparison_result.get("system_label", "Uncertain")
    missed_bias      = comparison_result.get("missed_bias", False)
    missed_claims    = comparison_result.get("missed_claims", False)
    source_mismatch  = comparison_result.get("source_mismatch", False)
    score_diff       = comparison_result.get("score_diff", 0)
    label_match      = comparison_result.get("label_match", True)

    # Build per-topic skill level lookup (defaults to beginner if no DB data)
    skill_levels: Dict[str, str] = _get_skill_levels(db_session, user_id)

    # ── Rule 1: Skipped claim identification step ─────────────────────────────
    if missed_claims or "claims" in skipped_steps:
        topic = "claim_detection"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                "You skipped the claim identification step. "
                "Isolating what is being claimed is the first step in any fact-check."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 2: Source rated differently from system (medium/high confidence) ─
    if source_mismatch and confidence in ("medium", "high"):
        topic = "source_verification"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                f"You rated the source as credible (with {confidence} confidence), "
                "but the system found it to be of low or unknown reliability."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 3: Did not detect bias that system found ─────────────────────────
    if missed_bias:
        topic = "bias_detection"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                "The system detected significant emotional or manipulative language "
                "that you did not flag. Recognising emotional language is one of the "
                "most important media literacy skills."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 4: Skipped evidence assessment step ──────────────────────────────
    if "evidence" in skipped_steps:
        topic = "evidence_evaluation"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                "You skipped the evidence assessment step. "
                "Checking whether claims are backed by verifiable evidence — "
                "not just asserted — separates fact from opinion."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 5: Score diverged by 30+ points ─────────────────────────────────
    if abs(score_diff) >= 30:
        direction = "more lenient" if score_diff > 0 else "more critical"
        topic = "general"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                f"Your credibility score was {abs(score_diff)} points "
                f"{'higher' if score_diff > 0 else 'lower'} than the system's — "
                f"you were significantly {direction}. "
                "This lesson covers calibrated skepticism."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 6: High confidence + wrong label (overconfidence) ───────────────
    if confidence == "high" and not label_match and user_label is not None:
        topic = "general"
        # Avoid duplicate with Rule 5
        already_added = any(t["lesson_key"].startswith("general_") for t in triggers)
        if not already_added:
            triggers.append({
                "lesson_key":     "general_overconfidence",
                "trigger_reason": (
                    f"You were highly confident in your verdict ('{user_label}'), "
                    f"but the system reached a different conclusion ('{system_label}'). "
                    "High confidence + wrong label is a sign of confirmation bias."
                ),
                "difficulty":     skill_levels.get(topic, "beginner"),
            })

    # ── Deduplicate (keep first occurrence per lesson_key) ────────────────────
    # ── Rule 8: behavior flag — skips_source_check ───────────────────────────
    # Fired when behavior_tracker detects the user consistently skips source
    # verification. → Module 8: Responsible Sharing / Pre-Share Check habit.
    behavior_flags = user_eval_data.get("behavior_flags") or []
    if "skips_source" in behavior_flags or "skips_source_check" in behavior_flags:
        topic = "responsible_sharing"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                "You skipped checking the source. Verifying sources before "
                "sharing is a core responsible sharing habit — Module 8 covers this."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 9: behavior flag — trusts_emotional ──────────────────────────────
    # Fired when behavior_tracker detects the user regularly accepts emotionally
    # framed content without scrutiny. → Module 6: How Fake News is Made.
    if "trusts_emotional" in behavior_flags:
        topic = "fake_news_creation"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                "Emotionally charged language is a common tactic in fake news. "
                "Module 6 explains how misleading content is engineered."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    # ── Rule 10: behavior flag — headline_only ────────────────────────────────
    # Fired when behavior_tracker detects the user judges content based on
    # the headline alone without reading evidence. → Module 7: Types of Misinformation.
    if "headline_only" in behavior_flags:
        topic = "misinformation_types"
        triggers.append({
            "lesson_key":     _pick_lesson_key(topic, skill_levels),
            "trigger_reason": (
                "Clickbait and misleading headlines are a core misinformation tactic. "
                "Module 7 explains the different types of mis- and disinformation."
            ),
            "difficulty":     skill_levels.get(topic, "beginner"),
        })

    seen: set = set()
    deduped: List[Dict[str, str]] = []
    for t in triggers:
        if t["lesson_key"] not in seen:
            seen.add(t["lesson_key"])
            deduped.append(t)

    return deduped


# ── Helpers ───────────────────────────────────────────────────────────────────

_TOPIC_LESSON_KEYS = {
    "claim_detection": {
        "beginner":     "claim_detection_beginner",
        "intermediate": "claim_detection_intermediate",
        "advanced":     "claim_detection_advanced",
    },
    "source_verification": {
        "beginner":     "source_verification_beginner",
        "intermediate": "source_verification_intermediate",
        "advanced":     "source_verification_advanced",
    },
    "bias_detection": {
        "beginner":     "bias_detection_beginner",
        "intermediate": "bias_detection_intermediate",
        "advanced":     "bias_detection_advanced",
    },
    "evidence_evaluation": {
        "beginner":     "evidence_evaluation_beginner",
        "intermediate": "evidence_evaluation_intermediate",
        "advanced":     "evidence_evaluation_advanced",
    },
    "general": {
        "beginner":     "general_mil_beginner",
        "intermediate": "general_mil_intermediate",
        "advanced":     "general_mil_advanced",
    },
    # v3 — Modules 6/7/8 triggered by behavior_tracker flags
    "fake_news_creation": {
        "beginner":     "fake_news_creation_beginner",
        "intermediate": "fake_news_creation_intermediate",
        "advanced":     "fake_news_creation_advanced",
    },
    "misinformation_types": {
        "beginner":     "misinformation_types_beginner",
        "intermediate": "misinformation_types_intermediate",
        "advanced":     "misinformation_types_advanced",
    },
    "responsible_sharing": {
        "beginner":     "responsible_sharing_beginner",
        "intermediate": "responsible_sharing_intermediate",
        "advanced":     "responsible_sharing_advanced",
    },
}


def _pick_lesson_key(topic: str, skill_levels: Dict[str, str]) -> str:
    """Return the lesson key for the user's current skill level in a topic."""
    level = skill_levels.get(topic, "beginner")
    return _TOPIC_LESSON_KEYS.get(topic, {}).get(level, f"{topic}_beginner")


def _get_skill_levels(
    db_session: Optional[Session],
    user_id: Optional[int],
) -> Dict[str, str]:
    """
    Look up the user's current skill level per topic from user_skill_progress.
    Returns a dict {topic: level}. Defaults to 'beginner' for missing topics.
    """
    defaults = {
        "claim_detection":    "beginner",
        "source_verification": "beginner",
        "bias_detection":     "beginner",
        "evidence_evaluation": "beginner",
        "general":            "beginner",
    }

    if db_session is None or not user_id:
        return defaults

    try:
        rows = db_session.execute(
            sa.text(
                "SELECT topic, current_level FROM user_skill_progress "
                "WHERE user_id = :uid"
            ),
            {"uid": user_id},
        ).fetchall()
        for row in rows:
            defaults[row.topic] = row.current_level
    except Exception:
        pass  # Return defaults if table doesn't exist yet

    return defaults
