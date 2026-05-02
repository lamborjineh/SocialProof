"""
SocialProof — services/behavior_tracker.py  v3.0
Detects behavioral reasoning patterns from a submitted user evaluation
and returns additional lesson triggers for Modules 6, 7, and 8.

Called by routers/user_evaluation.py AFTER the user_evaluation row is saved
and AFTER lesson_trigger.py runs its 7 content-gap rules. The flags produced
here extend — never replace — the existing trigger list.

Behavioral flags detected:
  skips_source       → user never assessed source credibility (skipped Step 2)
  trusts_emotional   → user gave high score to heavily biased content
  headline_only      → user submitted with very low time spent and no claims
  overconfident      → user is "high" confidence but score diverges badly from system
  always_unsure      → user is always "low" confidence regardless of content clarity

Each flag maps to a lesson key seeded in scripts/seed_lessons.py (Modules 6–8).
If the lessons table doesn't have the key yet, the trigger is silently skipped
by the JOIN in routers/lessons.py — seed the lessons first.

Usage:
    from services.behavior_tracker import get_behavior_triggers
    extra = get_behavior_triggers(user_eval_data, system_data)
    # extra is a list of {lesson_key, trigger_reason, difficulty} dicts
    # append to existing triggers before saving to lessons_triggered
"""

from typing import List, Dict, Any, Optional


# ── Thresholds (adjust without touching logic) ────────────────────────────────

# Minimum time (seconds) expected for a considered evaluation
_MIN_THOUGHTFUL_TIME = 20

# Score gap between user and system that flags overconfidence
_OVERCONFIDENCE_SCORE_GAP = 30

# Bias score above which emotional framing is considered significant
_EMOTIONAL_BIAS_THRESHOLD = 0.65

# How many evaluations in history before we flag "always_unsure"
_ALWAYS_UNSURE_MIN_EVALS = 3


def get_behavior_triggers(
    user_eval: Dict[str, Any],
    system_data: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """
    Detect behavioral reasoning patterns and return lesson triggers.

    Args:
        user_eval: Dict of fields from the just-saved UserEvaluationORM row.
            Expected keys: skipped_steps, source_credible, bias_detected,
            evidence_assessed, user_score, confidence_level, time_spent_seconds,
            identified_claims.
        system_data: Dict from the evaluation's analysis_json.
            Expected keys: bias_score, score, label, claims.
        history: Optional list of recent user_evaluations (for pattern flags).
            Each dict should have: confidence_level.

    Returns:
        List of trigger dicts: [{lesson_key, trigger_reason, difficulty}]
        Empty list if no behavioral flags fire.
    """
    triggers: List[Dict[str, str]] = []
    seen_keys: set = set()

    def _add(lesson_key: str, reason: str, difficulty: str = "beginner"):
        if lesson_key not in seen_keys:
            seen_keys.add(lesson_key)
            triggers.append({
                "lesson_key":     lesson_key,
                "trigger_reason": reason,
                "difficulty":     difficulty,
            })

    skipped_steps     = user_eval.get("skipped_steps") or []
    source_credible   = user_eval.get("source_credible")
    bias_detected     = user_eval.get("bias_detected", 0)
    evidence_assessed = user_eval.get("evidence_assessed", 0)
    user_score        = user_eval.get("user_score") or 50
    confidence_level  = user_eval.get("confidence_level", "medium")
    time_spent        = user_eval.get("time_spent_seconds") or 0
    identified_claims = user_eval.get("identified_claims") or []

    bias_score   = system_data.get("bias_score", 0.0)
    system_score = system_data.get("score", 50)
    system_claims = system_data.get("claims") or []

    # ── Flag 1: skips_source (Module 6) ──────────────────────────────────────
    # User skipped Step 2 (source check) entirely.
    # "source" in skipped_steps covers both "source_step" and "source" variants.
    skipped_source = (
        source_credible is None
        or any("source" in str(s).lower() for s in skipped_steps)
    )
    if skipped_source:
        _add(
            lesson_key     = "behavior_skips_source",
            trigger_reason = (
                "You did not assess the source before forming a judgment. "
                "Source verification is a key step in evaluating information."
            ),
            difficulty     = "beginner",
        )

    # ── Flag 2: trusts_emotional (Module 7) ──────────────────────────────────
    # User gave a credible/high score to content with strong emotional framing.
    content_biased   = bias_score >= _EMOTIONAL_BIAS_THRESHOLD
    user_trusted_it  = user_score >= 60
    bias_undetected  = not bias_detected

    if content_biased and user_trusted_it and bias_undetected:
        _add(
            lesson_key     = "behavior_trusts_emotional",
            trigger_reason = (
                "This content had strong emotional or biased framing, "
                "but you rated it as credible without flagging bias. "
                "Emotional language can influence how trustworthy content feels."
            ),
            difficulty     = "intermediate",
        )

    # ── Flag 3: headline_only (Module 6) ─────────────────────────────────────
    # User submitted very quickly with no claims identified — likely didn't
    # read past the headline.
    rushed          = time_spent > 0 and time_spent < _MIN_THOUGHTFUL_TIME
    no_claims       = len(identified_claims) == 0
    system_has_claims = len(system_claims) > 0

    if rushed and no_claims and system_has_claims:
        _add(
            lesson_key     = "behavior_headline_only",
            trigger_reason = (
                "Your evaluation was submitted very quickly and identified no claims, "
                "even though the system detected verifiable statements. "
                "Reading beyond the headline helps you evaluate content more accurately."
            ),
            difficulty     = "beginner",
        )

    # ── Flag 4: overconfident (Module 8) ─────────────────────────────────────
    # User reports "high" confidence but their score diverges significantly
    # from the system's assessment.
    score_gap = abs(user_score - system_score)
    if confidence_level == "high" and score_gap >= _OVERCONFIDENCE_SCORE_GAP:
        _add(
            lesson_key     = "behavior_overconfident",
            trigger_reason = (
                f"You indicated high confidence, but your assessment differed "
                f"from the system's by {score_gap} points. "
                "Calibrated confidence means matching certainty to the strength of evidence."
            ),
            difficulty     = "intermediate",
        )

    # ── Flag 5: always_unsure (Module 8) ─────────────────────────────────────
    # User consistently rates their own confidence as "low" across multiple
    # evaluations — may indicate avoidance rather than genuine uncertainty.
    if history and len(history) >= _ALWAYS_UNSURE_MIN_EVALS:
        low_confidence_count = sum(
            1 for h in history
            if (h.get("confidence_level") or "").lower() == "low"
        )
        if low_confidence_count >= len(history):
            _add(
                lesson_key     = "behavior_always_unsure",
                trigger_reason = (
                    "You have consistently rated your confidence as low across "
                    "multiple evaluations. Building a systematic approach to "
                    "evidence assessment can help you trust your own reasoning."
                ),
                difficulty     = "intermediate",
            )

    return triggers
