"""
SocialProof — Module 8: Annotation Engine  v2.1

v2.1 Changes:
  - Opinion/context classification now uses the same compiled patterns as
    ClaimDetectionModule (imported directly) instead of a separate, weaker
    OPINION_MARKERS set that only had ~10 words.

    Previously annotation.py ran its own check on non-claim sentences using
    a small set like {"think", "believe", "shocking", "absolutely"}.
    This caused two problems:
      1. Sentences correctly penalised by claim_detection (e.g. "Some argue
         that climate change is natural") were not in claim_indices, but
         annotation then labelled them "context" instead of "opinion" because
         its own check was too narrow.
      2. The two modules could disagree about what constitutes an opinion,
         which is confusing for the frontend and wrong for MIL teaching.

    The fix: annotation.py imports CONTEXT_OPENER_RE, OPINION_OPENER_RE,
    OPINION_CONTENT_RE, ABSOLUTISM_RE, MPQA_STRONG_SUBJ etc. directly from
    claim_detection.py. Both modules now share a single source of truth.

  - Classification priority for non-claim sentences:
      1. OPINION_CONTENT_RE (mid-sentence hedging) → "opinion"
      2. OPINION_OPENER_RE (start-of-sentence hedging) → "opinion"
      3. CONTEXT_OPENER_RE (attribution/transition, no opinion content) → "context"
      4. EVALUATIVE_OPENER_RE → "opinion"
      5. Question → "context"
      6. Filipino opinion markers → "opinion"
      7. ABSOLUTISM_RE without specifics → "opinion"
      8. MPQA_STRONG_SUBJ ≥ 2 hits → "opinion"
      9. Default → "context"

  - Evidence status tags unchanged from v2.0:
      support    → green
      contradict → red
      unverified → gray (explicit, was None before v2.0)

Content type tags (unchanged):
  claim   → blue
  opinion → orange
  context → gray
"""

import re
from typing import List, Dict

# Import shared compiled patterns — single source of truth for both modules.
# No circular import: claim_detection imports only from config.
from pipeline.claim_detection import (
    CONTEXT_OPENER_RE,
    OPINION_OPENER_RE,
    OPINION_CONTENT_RE,
    EVALUATIVE_OPENER_RE,
    ABSOLUTISM_RE,
    MPQA_STRONG_SUBJ,
    QUESTION_RE,
    QUESTION_OPENER_RE,
    _SPECIFICS_RE,
    ClaimDetectionModule,
)


class AnnotationEngine:

    @classmethod
    def _classify_non_claim(cls, sent_text: str) -> str:
        """
        Classify a sentence that was NOT flagged as a claim.
        Returns "opinion" or "context".

        Uses the same pattern hierarchy as ClaimDetectionModule._opinion_penalty()
        so the two modules are always in agreement.
        """
        sent_lower = sent_text.lower()

        # 1. Mid-sentence hedging — takes priority over context opener
        if OPINION_CONTENT_RE.search(sent_text):
            return "opinion"

        # 2. Opinion opener (first + third person)
        if OPINION_OPENER_RE.match(sent_text):
            return "opinion"

        # 3. Context opener — attribution / transition (no opinion content inside)
        if CONTEXT_OPENER_RE.match(sent_text):
            return "context"

        # 4. Evaluative opener
        if EVALUATIVE_OPENER_RE.match(sent_text):
            return "opinion"

        # 5. Question → context (carries no assertion)
        if QUESTION_RE.search(sent_text) or QUESTION_OPENER_RE.match(sent_text):
            return "context"

        # 6. Filipino opinion / hedging markers
        if any(m in sent_lower for m in ClaimDetectionModule.OPINION_MARKERS_FIL):
            return "opinion"

        # 7. Absolutism without numeric specifics
        if ABSOLUTISM_RE.search(sent_text) and not _SPECIFICS_RE.search(sent_text):
            return "opinion"

        # 8. MPQA strong subjectivity: 2+ hits
        words     = set(re.findall(r"\b\w+\b", sent_lower))
        mpqa_hits = words & MPQA_STRONG_SUBJ
        if len(mpqa_hits) >= 2:
            return "opinion"

        # 9. Default
        return "context"

    @classmethod
    def annotate(
        cls,
        doc,
        claim_list:   List[Dict],
        evidence_map: Dict[int, str],   # sentence_index → "support" | "contradict"
    ) -> List[Dict]:
        """
        Walk every sentence in `doc` and assign:
          - type   : claim | opinion | context
          - status : support | contradict | unverified | None

        Claims with no evidence hit are explicitly marked "unverified" so the
        frontend renders a visible gray badge rather than omitting any indicator.
        """
        segments: List[Dict] = []
        claim_indices = {c["sentence_index"]: c for c in claim_list}

        for i, sent in enumerate(doc.sents):
            sent_text = sent.text.strip()
            if not sent_text:
                continue

            if i in claim_indices:
                claim_entry = claim_indices[i]
                ev_status   = evidence_map.get(i)

                # Explicit "unverified" when no support/contradict result available
                if ev_status is None and not claim_entry.get("evidence_found", True):
                    ev_status = "unverified"

                segments.append({
                    "text":   sent_text,
                    "type":   "claim",
                    "status": ev_status,
                })
            else:
                seg_type = cls._classify_non_claim(sent_text)
                segments.append({
                    "text":   sent_text,
                    "type":   seg_type,
                    "status": None,
                })

        return segments
