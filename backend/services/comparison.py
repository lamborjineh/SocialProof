"""
SocialProof — Comparison Engine & Lesson Trigger Logic
v3.1 — Added score_diff_label (§5.3) using Korfiatis et al. (2012) scale.

score_diff_label scale (absolute value of user_score - system_score):
  |diff| <= 15  → "Low"      (close judgment)
  16–30         → "Moderate" (somewhat off)
  > 30          → "High"     (significantly off)

Source: Korfiatis, N., García-Bariocanal, E., & Sánchez-Alonso, S. (2012).
  Evaluating content quality and helpfulness of online product reviews.
  Decision Support Systems, 53(1), 219–228.
  Applied to assessment gap tercile splits on a 0–100 scale.
"""

from typing import Optional, List, Dict
from schemas import UserEvaluationRequest


# ── Score diff label (§5.3) ───────────────────────────────────────────────────

def _score_diff_label(diff: int) -> str:
    """
    Classify the absolute score difference between user and system.
    Based on Korfiatis et al. (2012) tercile split applied to 0-100 scale.
    """
    abs_diff = abs(diff)
    if abs_diff <= 15:
        return "Low"
    if abs_diff <= 30:
        return "Moderate"
    return "High"



# ── Comparison Engine ─────────────────────────────────────────────────────────

class ComparisonEngine:

    @staticmethod
    def compare(
        user_eval:     UserEvaluationRequest,
        system_result: Dict,
    ) -> Dict:
        system_score = system_result.get("score", 50)
        system_label = system_result.get("label", "Uncertain")
        bias_score   = system_result.get("bias_score", 0.0)
        evidence_was_missing = system_result.get("is_partial", False) or (
            system_result.get("evidence_coverage", 1.0) == 0.0
        )

        user_score    = user_eval.user_score or 50
        user_label    = user_eval.user_label
        skipped_steps = user_eval.skipped_steps or []
        confidence    = user_eval.confidence_level

        score_diff       = user_score - system_score
        label_match      = (user_label == system_label) if user_label else False
        missed_bias      = (bias_score > 0.45) and (not user_eval.bias_detected)
        user_claims_raw  = " ".join(user_eval.identified_claims or [])
        missed_claims    = (not user_claims_raw) or len(user_claims_raw.strip()) < 10
        source_mismatch  = (
            user_eval.source_credible == "yes"
            and system_result.get("source_score", 0.5) < 0.45
        )

        # Fix (audit §HIGH-3): determine_lessons() was generating Path A keys
        # ("identify_claims", "recognize_bias", …) that do NOT exist in the DB.
        # The DB was seeded with Path B keys ("claim_detection_beginner", …) from
        # compute_triggers() in lesson_trigger.py.  Having two parallel systems
        # caused the DB enrichment query to always return empty, so lessons were
        # never surfaced.  Solution: remove determine_lessons() entirely from this
        # path.  Lesson triggering is now done exclusively via compute_triggers()
        # in routers/user_evaluation.py after compare() returns, where the correct
        # Path B keys match the seeded DB rows.
        triggered_lessons:  list = []
        lesson_context_map: dict = {}

        parts: List[str] = []

        if evidence_was_missing:
            parts.append(
                "Note: no evidence was found in the corpus for the claims in this content. "
                "The analysis is based on source credibility and language patterns only."
            )

        if label_match:
            parts.append("Your overall verdict matched the system's analysis.")
        else:
            parts.append(
                f"Your verdict was '{user_label or 'not provided'}', "
                f"while the system rated it '{system_label}'."
            )

        abs_diff = abs(score_diff)
        if abs_diff <= 15:
            parts.append("Your credibility score was very close to the system's.")
        elif score_diff > 15:
            parts.append(
                f"You rated this content {score_diff} points higher than the system — "
                "you may have been more lenient."
            )
        else:
            parts.append(
                f"You rated this content {abs_diff} points lower than the system — "
                "you may have been more critical."
            )

        if missed_bias:
            parts.append(
                "The system detected high emotional/biased language that you did not flag."
            )
        if missed_claims:
            parts.append(
                "You did not identify specific claims — "
                "claim identification is an important MIL skill."
            )
        if source_mismatch:
            parts.append(
                "You rated the source as credible, but the system found it to be of low reliability."
            )

        return {
            "score_diff":           score_diff,
            "score_diff_label":     _score_diff_label(score_diff),   # §5.3 fix
            "user_label":           user_label,
            "system_label":         system_label,
            "label_match":          label_match,
            "missed_bias":          missed_bias,
            "missed_claims":        missed_claims,
            "source_mismatch":      source_mismatch,
            "confidence_level":     confidence,
            "triggered_lessons":    triggered_lessons,
            "feedback_summary":     " ".join(parts),
            "evidence_was_missing": evidence_was_missing,
            "lesson_context_map":   lesson_context_map,
            # Required by ComparisonResult schema
            "feedback_items":       [],
        }
