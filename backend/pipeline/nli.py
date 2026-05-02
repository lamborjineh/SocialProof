"""
SocialProof — Module 6: NLI (Contradiction Detection)  v4.0

v4.0 Changes:
  - Model switched to MoritzLaurer/mDeBERTa-v3-base-mnli-xnli.
    Handles 100 languages including Filipino natively — no Google Translate needed.
    Translation path kept for backward compatibility if NLI_MODEL is switched back to roberta.
  - _MULTILINGUAL_NLI flag: evaluated once at import from NLI_MODEL env var.
    When active, _is_tagalog() and _translate_with_timeout() are both bypassed entirely.
  - MIN_EVIDENCE_SIMILARITY restored to 0.30 (changed_files raised it to 0.50 which
    silently discarded valid evidence — the reranker already ensures quality).
  - MIN_NLI_CONFIDENCE_FOR_DECISIVE lowered from 0.45 → 0.35.
    mDeBERTa and roberta-large-mnli both return 0.38–0.44 confidence for valid
    Philippine content entailments. 0.45 was throwing away real signals.
  - _TAGALOG_MARKERS cleaned: removed "at", "ng", "sa", "na", "ay", "ang", "ito"
    which all appear in English text (especially with PH proper nouns like
    "Bangko Sentral ng Pilipinas"), causing false Tagalog detection on English input.
  - classify_multi() added in v3.2: weighted multi-evidence NLI aggregation.
    Each evidence piece weighted by similarity_score × nli_confidence.
    The label with the highest accumulated weight wins.

  Academic references:
    Laurer et al. (2022). Less Annotating, More Classifying.
    https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli
    Liu et al. (2019). RoBERTa: A Robustly Optimized BERT Pretraining Approach.
    https://arxiv.org/abs/1907.11692
"""

import re
import math
from typing import Dict, List

from config import logger, TIMEOUT_TRANSLATION
from core.model_registry import ModelRegistry


# ── Reranker gate ─────────────────────────────────────────────────────────────

# Cross-encoder logit floor. Candidates scoring below this are irrelevant even
# if their BGE-M3 cosine score passed the similarity threshold.
# ms-marco-MiniLM logits typically range -8 to +8; -1.0 is safely below any
# legitimate relevant passage.
MIN_RERANK_SCORE: float = -1.0


def _sigmoid(x: float, scale: float = 3.0) -> float:
    """Map cross-encoder logits to [0, 1] for use as relevance weights."""
    return 1.0 / (1.0 + math.exp(-x / scale))


# ── Token overlap guard ───────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "in", "to",
    "and", "or", "that", "this", "it", "be", "has", "have", "for",
}


def _token_overlap(claim: str, evidence: str) -> float:
    """
    Fraction of content words in `claim` that also appear in `evidence`.
    Only words ≥3 characters that are not stop words are counted.
    Returns 0.0 when `claim` has no content words.
    """
    c = {w.lower() for w in re.findall(r"\b\w{3,}\b", claim)}   - _STOPWORDS
    e = {w.lower() for w in re.findall(r"\b\w{3,}\b", evidence)} - _STOPWORDS
    if not c:
        return 0.0
    return len(c & e) / len(c)


class NLIModule:
    """
    Single-evidence NLI (classify) and multi-evidence aggregation (classify_multi).

    classify_multi() is the preferred entry point — it aggregates evidence
    using weighted voting to avoid the single-piece winner-takes-all failure mode.
    """

    LABEL_MAP = {
        # roberta-large-mnli labels
        "entailment":    "support",
        "contradiction": "contradict",
        "neutral":       "neutral",
        "ENTAILMENT":    "support",
        "CONTRADICTION": "contradict",
        "NEUTRAL":       "neutral",
        # mDeBERTa-v3-base-mnli-xnli labels (same format, kept for explicitness)
        "entailment_en": "support",
    }

    # mDeBERTa handles 100 languages natively — translation not needed when active.
    # This flag is evaluated once at import; model changes require process restart.
    _MULTILINGUAL_NLI: bool = (
        "deberta" in __import__("os").getenv(
            "NLI_MODEL", "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
        ).lower()
    )

    # Minimum reranker similarity score for evidence to enter NLI.
    # 0.30 matches the retriever threshold — the reranker already guarantees
    # relevance, so discarding anything above 0.30 removes valid signals.
    # Do NOT raise this without first rebuilding the FAISS index to full size.
    MIN_EVIDENCE_SIMILARITY: float = 0.30

    # Minimum NLI confidence for a support/contradict label to be treated as decisive.
    # Lowered from 0.45 → 0.35: both mDeBERTa and roberta-large-mnli return
    # 0.38–0.44 confidence for valid Philippine content entailments.
    # At 0.45 those were being downgraded to neutral, killing the evidence sub-score.
    MIN_NLI_CONFIDENCE_FOR_DECISIVE: float = 0.35

    # Tagalog function words used for lightweight language detection.
    # IMPORTANT: "at", "ng", "sa", "na", "ay", "ang", "ito" removed — these appear
    # in English text (especially Philippine proper nouns) and caused false positives
    # that sent English sentences through the 6-second Google Translate path.
    _TAGALOG_MARKERS = {
        "ayon", "mga", "para",
        "siya", "niya", "nila", "kami", "kaya",
        "pero", "dahil", "kung", "hindi", "oo", "po", "rin",
    }

    @classmethod
    def _is_tagalog(cls, text: str) -> bool:
        """
        Lightweight Tagalog detector.
        Returns True when ≥3 Tagalog marker words are present.
        Only called when _MULTILINGUAL_NLI is False (i.e. roberta is active).
        """
        words = set(text.lower().split())
        hits  = words & cls._TAGALOG_MARKERS
        return len(hits) >= 3

    @classmethod
    def _translate_with_timeout(cls, text: str, timeout: float = None) -> str:
        """
        Translates Tagalog text to English using GoogleTranslator, enforcing a
        hard timeout via ThreadPoolExecutor so a slow network call cannot blow
        the pipeline SLA.

        Returns the translated string on success, or the ORIGINAL TEXT on
        timeout/error — always returns a string, never None.
        Only called when _MULTILINGUAL_NLI is False (roberta mode).
        """
        if timeout is None:
            timeout = TIMEOUT_TRANSLATION

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

        def _do_translate():
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source="tl", target="en").translate(text)

        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_translate)
            try:
                return future.result(timeout=timeout)
            except FutureTimeout:
                logger.warning(
                    f"[NLI] GoogleTranslator exceeded {timeout}s timeout — using original text"
                )
                return text  # always a string, never None
            except Exception as exc:
                logger.warning(f"[NLI] GoogleTranslator error: {exc} — using original text")
                return text  # always a string, never None

    @classmethod
    def classify(cls, claim: str, evidence: str) -> Dict:
        """
        Single-evidence NLI.

        Returns:
            {"type": "support"|"contradict"|"neutral", "nli_confidence": float}

        Falls back to neutral/0.5 on model failure.
        """
        # mDeBERTa handles Filipino natively — skip translation entirely.
        # Translation path kept for backward compat if NLI_MODEL switches to roberta.
        if not cls._MULTILINGUAL_NLI and cls._is_tagalog(claim):
            translated = cls._translate_with_timeout(claim)
            if translated != claim:
                logger.debug(f"[NLI] Tagalog → EN (roberta mode): '{translated[:80]}'")
            claim = translated

        try:
            nli    = ModelRegistry.nli()
            result = nli({"text": evidence, "text_pair": claim})

            if isinstance(result, list):
                result = result[0]

            best_label = result.get("label", "NEUTRAL")
            best_score = float(result.get("score", 0.5))

            mapped = cls.LABEL_MAP.get(best_label, "neutral")

            # Low-confidence decisive labels are downgraded to neutral.
            # Threshold lowered to 0.35 — PH content regularly scores 0.38–0.44
            # for valid entailments; 0.45 was discarding real signals.
            if mapped in ("support", "contradict") and best_score < cls.MIN_NLI_CONFIDENCE_FOR_DECISIVE:
                logger.debug(
                    f"[NLI] {mapped} confidence {best_score:.3f} < "
                    f"{cls.MIN_NLI_CONFIDENCE_FOR_DECISIVE} — downgraded to neutral"
                )
                mapped = "neutral"

            return {
                "type":           mapped,
                "nli_confidence": round(best_score, 4),
            }
        except Exception as e:
            logger.warning(f"NLI classification failed: {e}. Using neutral fallback.")
            return {"type": "neutral", "nli_confidence": 0.5}

    @classmethod
    def classify_multi(
        cls,
        claim: str,
        evidence_list: List[Dict],
    ) -> Dict:
        """
        Multi-evidence NLI with weighted voting.

        Each evidence piece is classified individually, then weighted by:
            weight = similarity_score × nli_confidence

        The label that accumulates the most weight wins.
        This prevents a single high-confidence but irrelevant piece from
        overriding a consensus of lower-confidence but more relevant evidence.

        Args:
            claim:         The claim text (NLI hypothesis).
            evidence_list: List of dicts, each with at minimum:
                           {"text": str, "similarity_score": float}

        Returns:
            {
                "type":            "support"|"contradict"|"neutral",
                "nli_confidence":  float,   # winning label's normalised vote share
                "vote_breakdown":  {"support": float, "contradict": float, "neutral": float},
                "evidence_results": [        # per-evidence NLI results with weights
                    {
                        "text": str,
                        "type": str,
                        "nli_confidence": float,
                        "similarity_score": float,
                        "weight": float,
                        ...original evidence fields...
                    }
                ],
                "evidence_count": int,       # number of pieces that participated
            }
        """
        if not evidence_list:
            return {
                "type":             "neutral",
                "nli_confidence":   0.5,
                "vote_breakdown":   {"support": 0.0, "contradict": 0.0, "neutral": 0.0},
                "evidence_results": [],
                "evidence_count":   0,
            }

        votes:            Dict[str, float] = {"support": 0.0, "contradict": 0.0, "neutral": 0.0}
        evidence_results: List[Dict]       = []
        participated      = 0

        # Fix (audit): process up to 5 candidates to match _RETRIEVAL_TOP_K.
        # Raising quality gates (reranker + overlap) without also raising this cap
        # means fewer pieces survive to vote when the top-2 fail the gates.
        MAX_NLI_EVIDENCE = 5
        for ev in evidence_list[:MAX_NLI_EVIDENCE]:
            sim_score    = float(ev.get("similarity_score", 0.0))
            rerank_score = ev.get("rerank_score")   # None if reranker unavailable

            # Gate 1 — similarity threshold
            if sim_score < cls.MIN_EVIDENCE_SIMILARITY:
                logger.debug(
                    f"[NLI] Skipping low-sim evidence "
                    f"(sim={sim_score:.3f} < {cls.MIN_EVIDENCE_SIMILARITY}): "
                    f"{ev.get('text','')[:60]}"
                )
                continue

            # Gate 2 — reranker gate (Fix #2, audit).
            # The cross-encoder already scored this (claim, evidence) pair; a
            # very negative logit means the reranker considers it irrelevant even
            # if the BGE-M3 cosine score passed the sim threshold.
            if rerank_score is not None and rerank_score < MIN_RERANK_SCORE:
                logger.debug(
                    f"[NLI] Skipping low-rerank evidence "
                    f"(rerank={rerank_score:.2f}): {ev.get('text','')[:60]}"
                )
                continue

            # Gate 3 — topic coherence guard (Fix #3, audit).
            # A cheap pre-check before any model inference: skip truly unrelated
            # evidence (zero content-word overlap AND low cosine score).
            if _token_overlap(claim, ev["text"]) < 0.10 and sim_score < 0.70:
                logger.debug(
                    "[NLI] Skipping — zero topic overlap with claim: "
                    f"{ev.get('text','')[:60]}"
                )
                continue

            nli_result  = cls.classify(claim, ev["text"])
            ev_type     = nli_result["type"]
            nli_conf    = nli_result["nli_confidence"]

            # Fix #2 (audit): weight by sigmoid(rerank_score) when available, so
            # the cross-encoder's joint relevance signal drives the vote, not just
            # the BGE-M3 cosine score. Falls back to sim_score × nli_conf when the
            # reranker was unavailable (e.g. cross-encoder failed to load).
            relevance = _sigmoid(rerank_score) if rerank_score is not None else sim_score
            weight = relevance * nli_conf
            votes[ev_type] += weight
            participated   += 1

            evidence_results.append({
                **ev,
                "type":            ev_type,
                "nli_confidence":  nli_conf,
                "weight":          round(weight, 4),
            })

        # If all evidence was filtered out, fall back to neutral
        if participated == 0:
            logger.info(
                "[NLI] All evidence filtered by similarity threshold — "
                "returning neutral/0.5"
            )
            return {
                "type":             "neutral",
                "nli_confidence":   0.5,
                "vote_breakdown":   {"support": 0.0, "contradict": 0.0, "neutral": 0.0},
                "evidence_results": evidence_results,
                "evidence_count":   0,
            }

        # Normalise vote shares
        total_votes = sum(votes.values()) + 1e-9
        vote_shares = {k: round(v / total_votes, 4) for k, v in votes.items()}

        # Winning label = highest accumulated weight
        winning_label = max(votes, key=votes.get)
        winning_share = vote_shares[winning_label]

        logger.debug(
            f"[NLI] Multi-evidence ({participated} pieces): "
            f"support={vote_shares['support']:.3f} "
            f"contradict={vote_shares['contradict']:.3f} "
            f"neutral={vote_shares['neutral']:.3f} "
            f"→ {winning_label} ({winning_share:.3f})"
        )

        return {
            "type":             winning_label,
            "nli_confidence":   winning_share,
            "vote_breakdown":   vote_shares,
            "evidence_results": evidence_results,
            "evidence_count":   participated,
        }
