import asyncio
import re
"""
SocialProof — Pipeline Orchestrator v3.3

Pipeline order
  0a URL Fetching
  0b OCR
  1  Preprocessing
  2  Claim Detection
  3  Source Credibility
  4  Language & Bias Analysis
  5  ClaimBuster pre-scoring (optional, non-blocking)
  6  Evidence Retrieval (FAISS → live search fallback)
  7  NLI (multi-evidence weighted voting)
  8  Credibility Scoring
  9  Annotation Engine
  10 Explainability Engine
"""

import time
from typing import Dict, Optional

from config import MAX_EVIDENCE, logger
from core.model_registry import ModelRegistry
from schemas import AnalyzeRequest

from .preprocessing      import PreprocessingModule
from .url_fetcher        import URLFetcher
from .image_input        import extract_text_from_image
from .claim_detection    import ClaimDetectionModule
from .source_credibility import SourceCredibilityModule
from .bias_analysis      import BiasAnalysisModule
from .evidence_retrieval import (
    EvidenceRetrievalModule, record_unverified,
    score_claim_worthiness,
)
from .nli                import NLIModule
from .annotation         import AnnotationEngine
from .explainability     import ExplainabilityEngine, OllamaExplainer

def _timeout_fallback(claim_text: str = '', timeout_seconds: float = 5.0) -> dict:
    return {
        'label': 'Uncertain',
        'score': 50,
        'note': 'pipeline_timeout',
        'is_partial': True,
        'evidence_coverage': 0.0,
        'live_search_used': False,
        'timeout': True,
        'timeout_seconds': timeout_seconds,
    }


# ── Evidence retrieval config ─────────────────────────────────────────────────
# More candidates retrieved so multi-evidence voting has more material to work
# with. The quality gate inside NLIModule.classify_multi filters out weak ones.
_RETRIEVAL_TOP_K = 5


def _compute_score(source_score, bias_score, evidence_items, claim_results,
                   evidence_coverage, all_evidence_neutral=False):
    """Inline credibility scoring — replaces the removed CredibilityScoringEngine."""
    WEIGHTS = {"source": 0.25, "bias": 0.20, "evidence": 0.40, "claims": 0.15}
    PARTIAL = {"source": 0.45, "bias": 0.45, "claims": 0.10}
    STRONG_THRESH = 0.40

    is_partial = evidence_coverage < 0.0
    s_source = source_score
    s_bias   = 1.0 - bias_score

    s_evidence = None
    if evidence_items and not is_partial:
        def cw(e):
            return float(e.get("similarity_score", 0.0)) * float(e.get("nli_confidence", 0.5))
        sup = sum(cw(e) for e in evidence_items if e.get("type") == "support")
        con = sum(cw(e) for e in evidence_items if e.get("type") == "contradict")
        neu = sum(cw(e) for e in evidence_items if e.get("type") == "neutral")
        dec = sup + con
        if dec < 1e-6:
            s_evidence = 0.5
        else:
            dr = sup / (dec + 1e-9)
            tw = dec + neu + 1e-9
            nr = neu / tw
            s_evidence = dr * (1.0 - nr * 0.5) + 0.5 * (nr * 0.5)
        has_strong = any(e.get("similarity_score", 0) >= STRONG_THRESH for e in evidence_items)
        if not has_strong:
            s_evidence = 0.5 + (s_evidence - 0.5) * 0.6

    vote_scores = []
    for c in (claim_results or []):
        bd = c.get("vote_breakdown")
        if bd and isinstance(bd, dict):
            vote_scores.append(bd.get("support", 0.0) - bd.get("contradict", 0.0))
        else:
            lbl = c.get("label", "unverified")
            vote_scores.append(1.0 if lbl == "supported" else (-0.5 if lbl == "unverified" else 0.0))
    if vote_scores:
        avg = sum(vote_scores) / len(vote_scores)
        s_claims = max(0.0, min(1.0, (avg + 1.0) / 2.0))
    else:
        s_claims = 0.5

    if is_partial:
        final_score = 0
        label = "Inconclusive"
    else:
        assert s_evidence is not None
        w = WEIGHTS
        raw = s_source * w["source"] + s_bias * w["bias"] + s_evidence * w["evidence"] + s_claims * w["claims"]
        final_score = max(0, min(100, int(round(raw * 100))))
        label = "Likely Credible" if final_score >= 60 else ("Uncertain" if final_score >= 40 else "Likely Misleading")

    def _sub(score, weight_pct, lbl):
        return {"score": round(score, 3) if score is not None else None, "weight_pct": weight_pct, "label": lbl}

    sub_scores = {
        "source":   _sub(s_source, 25, "Credible source" if s_source >= 0.7 else ("Mixed reliability" if s_source >= 0.5 else "Low credibility")),
        "bias":     _sub(s_bias,   20, "Low bias" if s_bias >= 0.7 else ("Moderate bias" if s_bias >= 0.5 else "High bias")),
        "evidence": _sub(s_evidence, 40, "No evidence" if s_evidence is None else ("Supports" if s_evidence >= 0.65 else ("Mixed" if s_evidence >= 0.4 else "Contradicts"))),
        "claims":   _sub(s_claims, 15, "Verified" if s_claims >= 0.65 else ("Partial" if s_claims >= 0.5 else "Unverified")),
        "evidence_coverage": round(evidence_coverage, 3),
    }

    return {
        "score": final_score,
        "label": label,
        "is_partial": is_partial,
        "is_inconclusive": is_partial,
        "all_evidence_neutral": all_evidence_neutral,
        "sub_scores": sub_scores,
    }


class AnalysisPipeline:
    """
    Stateless orchestrator. Instantiated once at startup and reused per request.
    """

    def __init__(self):
        self.preprocessor       = PreprocessingModule()
        self.evidence_retriever = EvidenceRetrievalModule()

    def run(
        self,
        request:               AnalyzeRequest,
        use_llm:               bool = True,
        user_submitted_claim:  Optional[str] = None,
    ) -> Dict:
        """
        Run the full analysis pipeline.

        Args:
            request:             Standard AnalyzeRequest.
            use_llm:             Whether to try Ollama for explanation.
            user_submitted_claim: If set, bypass claim detection and use this
                                  single claim (§4.3 user-typed claim).
        """
        t0 = time.time()

        # ── 0a. URL fetching ──────────────────────────────────────────────────
        raw_text         = request.text or ""
        url_fetch_failed = False
        url_fetch_error  = ""
        if request.input_type == "url" and request.url:
            fetch_result = URLFetcher.fetch(request.url)
            if fetch_result["error"]:
                logger.warning(f"URL fetch failed: {fetch_result['error']}")
                raw_text         = request.url
                url_fetch_failed = True
                url_fetch_error  = fetch_result["error"]
                # Fix 3b: abort early if URL string itself is too short to analyse usefully
                url_words = [w for w in re.split(r"[/\-_?=&.]", request.url) if len(w) > 3]
                if len(url_words) < 4:
                    return {
                        "score": None, "label": "Inconclusive", "is_inconclusive": True,
                        "is_partial": True, "url_fetch_failed": True,
                        "url_fetch_error": f"Could not access URL: {fetch_result['error']}. Please paste the article text instead.",
                        "explanation": f"The URL could not be loaded ({fetch_result['error']}). Please copy and paste the article text directly.",
                        "claims": [], "evidence": [], "annotated": [],
                        "source_score": 0.0, "bias_score": 0.0, "sub_scores": {},
                        "processing_ms": int((time.time() - t0) * 1000),
                        "evidence_coverage": 0.0, "unverified_claims": [],
                        "suggest_secondary_retrieval": True, "live_search_used": False,
                        "all_evidence_neutral": False, "no_claims_detected": False,
                        "stage_outputs": {}, "mil_tip": "", "mil_tip_source": "rule_based",
                        "evidence_quality_note": "The URL could not be accessed. No content was analysed.",
                    }
            else:
                raw_text = fetch_result["text"] or request.url
                logger.info(f"[0a] URL fetched: {len(raw_text)} chars")

        # ── 0b. OCR (image input) ─────────────────────────────────────────────
        # image_data is a base64-encoded string on the schema.
        # Decode it here to bytes before passing to OCR.
        if request.input_type == "image" and getattr(request, 'image_data', None):
            import base64 as _b64
            try:
                _image_bytes = _b64.b64decode(request.image_data)
            except Exception:
                _image_bytes = None
            if _image_bytes:
                ocr_text = extract_text_from_image(_image_bytes)
                if ocr_text:
                    raw_text = ocr_text
                    logger.info(f"[0b] OCR extracted: {len(raw_text)} chars")
                else:
                    logger.warning("[0b] OCR returned empty — image may be unreadable")

        # ── 1. Preprocessing ──────────────────────────────────────────────────
        clean_text = self.preprocessor.clean(raw_text)
        doc        = ModelRegistry.nlp()(clean_text)
        keywords   = self.preprocessor.extract_keywords(doc)
        logger.info(f"[1] Preprocessed. Keywords: {keywords[:5]}")

        # ── 2. Claim Detection ────────────────────────────────────────────────
        if user_submitted_claim:
            raw_claims = [{
                "text":           user_submitted_claim.strip(),
                "sentence_index": 0,
                "confidence":     1.0,
                "reasons":        ["user_submitted"],
            }]
            no_claims_detected = False
            logger.info("[2] Using user-submitted claim (no auto-detection)")
        else:
            raw_claims         = ClaimDetectionModule.detect(doc, clean_text, article_keywords=keywords)
            no_claims_detected = len(raw_claims) == 0
            logger.info(f"[2] Claim detection: {len(raw_claims)} claim(s) found")

        # ── 3. Source Credibility ─────────────────────────────────────────────
        source_result = SourceCredibilityModule.evaluate(request.url, clean_text)
        logger.info(f"[3] Source score: {source_result['score']}")

        # ── 4. Language & Bias Analysis ───────────────────────────────────────
        bias_result = BiasAnalysisModule.analyze(clean_text, doc)
        logger.info(f"[4] Bias score: {bias_result['score']}")

        # ── 5. ClaimBuster pre-scoring (non-blocking) ─────────────────────────
        claim_worthiness: Dict = {}
        for claim in raw_claims:
            cw = score_claim_worthiness(claim["text"])
            claim_worthiness[claim["text"][:80]] = cw
            if cw["source"] == "claimbuster":
                logger.info(
                    f"[5] ClaimBuster: '{claim['text'][:50]}' → "
                    f"{cw['score']:.2f} "
                    f"({'check-worthy' if cw['check_worthy'] else 'low priority'})"
                )

        # ── 6 + 7. Evidence Retrieval + NLI (per claim) ───────────────────────
        # v3.2: uses classify_multi() instead of per-evidence winner-takes-all.
        all_evidence:   list = []
        evidence_map:   Dict = {}
        claim_results:  list = []
        claims_with_evidence = 0

        for claim in raw_claims:
            # Fix #1 — augment only with named entities from the claim's own spaCy span,
            # NOT article-level keywords.  Article keywords contaminate unrelated claims
            # (e.g. a "slider" claim in a UI article picks up "CAPTCHA" and "volume ring"
            # from article keywords, pulling those exact corpus documents through FAISS).
            # Claim-level entities are semantically tethered to the claim itself.
            claim_lower = claim["text"].lower()
            claim_span  = next(
                (s for s in doc.sents if s.text.strip() == claim["text"]), None
            )
            claim_ents = (
                [e.text for e in claim_span.ents if len(e.text) > 3]
                if claim_span else []
            )
            # Only augment short claims that lack their own strong entity signal
            retrieval_query = claim["text"]
            if claim_ents and len(claim["text"]) < 80:
                extra = [e for e in claim_ents[:2] if e.lower() not in claim_lower]
                if extra:
                    retrieval_query = claim["text"] + " " + " ".join(extra)
                    logger.debug(f"[6] Entity-augmented query: '{retrieval_query[:100]}'")

            cw        = claim_worthiness.get(claim["text"][:80], {})
            retrieved, any_found = self.evidence_retriever.retrieve(
                retrieval_query,
                top_k=_RETRIEVAL_TOP_K,
                check_worthiness=cw,
            )

            # ── No evidence found at all ──────────────────────────────────────
            if not any_found:
                record_unverified(claim["text"])
                logger.info(
                    f"[6] stage_failed=retrieval — no evidence for: "
                    f"'{claim['text'][:60]}'"
                )
                claim_results.append({
                    "text":             claim["text"],
                    "sentence_index":   claim["sentence_index"],
                    "label":            "unverified",
                    "confidence":       claim["confidence"],
                    "evidence_found":   False,
                    "stage_failed":     "retrieval",
                    "check_worthiness": cw.get("score"),
                    "vote_breakdown":   None,
                })
                continue

            # ── Multi-evidence NLI ────────────────────────────────────────────
            multi = NLIModule.classify_multi(claim["text"], retrieved)

            # Collect per-evidence results for the response
            for ev_result in multi["evidence_results"]:
                all_evidence.append({
                    "evidence_text":    ev_result["text"],
                    "type":             ev_result["type"],
                    "source_label":     ev_result.get("source_label", ""),
                    "source_url":       ev_result.get("source_url"),
                    "similarity_score": ev_result["similarity_score"],
                    "nli_confidence":   ev_result["nli_confidence"],
                    "weight":           ev_result.get("weight", 0.0),
                    "claim_text":       claim["text"],
                    "source_type":      ev_result.get("source_type", "faiss"),
                })
                if ev_result.get("source_type") != "filtered":
                    if ev_result["similarity_score"] >= NLIModule.MIN_EVIDENCE_SIMILARITY:
                        pass  # already tracked inside classify_multi

            # Determine stage failure
            if multi["evidence_count"] == 0:
                # Evidence was found but all fell below similarity threshold
                record_unverified(claim["text"])
                logger.info(
                    f"[7] stage_failed=retrieval_weak — evidence found but all "
                    f"sim < {NLIModule.MIN_EVIDENCE_SIMILARITY} for: "
                    f"'{claim['text'][:60]}'"
                )
                claim_results.append({
                    "text":             claim["text"],
                    "sentence_index":   claim["sentence_index"],
                    "label":            "unverified",
                    "confidence":       claim["confidence"],
                    "evidence_found":   True,
                    "stage_failed":     "retrieval_weak",
                    "check_worthiness": cw.get("score"),
                    "vote_breakdown":   multi["vote_breakdown"],
                })
                continue

            claims_with_evidence += 1

            claim_label = {
                "support":    "supported",
                "contradict": "unverified",
                "neutral":    "neutral",
            }.get(multi["type"], "unverified")

            logger.info(
                f"[7] Claim '{claim['text'][:50]}' → "
                f"{claim_label} "
                f"(support={multi['vote_breakdown']['support']:.3f}, "
                f"contra={multi['vote_breakdown']['contradict']:.3f}, "
                f"neutral={multi['vote_breakdown']['neutral']:.3f}, "
                f"n={multi['evidence_count']})"
            )

            claim_results.append({
                "text":             claim["text"],
                "sentence_index":   claim["sentence_index"],
                "label":            claim_label,
                "confidence":       claim["confidence"],
                "nli_confidence":   multi["nli_confidence"],
                "evidence_found":   True,
                "stage_failed":     "none",
                "check_worthiness": cw.get("score"),
                "vote_breakdown":   multi["vote_breakdown"],
            })

            if multi["type"] in ("support", "contradict"):
                evidence_map[claim["sentence_index"]] = multi["type"]

        # ── Live search fallback (§4.6) ───────────────────────────────────────
        # PATCH: Previously only fired when FAISS found zero evidence for ALL claims.
        # This was a false-confidence trap: FAISS returning weak/irrelevant matches
        # blocked the fallback entirely. Now also fires when evidence quality is low.
        live_search_used = False

        # Compute evidence quality metrics to decide if live search is warranted.
        #
        # `all_evidence_neutral` is computed here for UX signalling (sent to the
        # frontend so it can show "Insufficient evidence" instead of "50/100"),
        # but is intentionally NOT used as a live-search trigger. When FAISS
        # returns high-similarity-but-neutral sentences (sim 0.65+), triggering
        # live search causes HTTP calls that routinely exceed the 5-second pipeline
        # SLA, returning a timeout fallback with all-zero scores — worse than the
        # neutral result. Live search stays gated on cosine sim thresholds only.
        _ev_sims = [e["similarity_score"] for e in all_evidence if "similarity_score" in e]
        _avg_sim = sum(_ev_sims) / len(_ev_sims) if _ev_sims else 0.0
        _max_sim = max(_ev_sims) if _ev_sims else 0.0

        decisive_evidence_count = sum(
            1 for e in all_evidence if e.get("type") in ("support", "contradict")
        )
        all_evidence_neutral = (
            len(all_evidence) > 0 and decisive_evidence_count == 0
        )

        # _max_sim intentionally excluded from trigger:
        # It is computed pre-NLI and includes evidence that NLI later rejects
        # as neutral or filtered. A reputation-boosted result at sim=0.41 can
        # block live search even when claims_with_evidence == 0 (NLI confirmed
        # nothing useful). The remaining conditions are NLI-verified:
        #   claims_with_evidence == 0  → NLI found nothing decisive
        #   _avg_sim < 0.45            → FAISS retrieval quality is weak overall
        # NOTE: all_evidence_neutral excluded — would cause HTTP timeouts without
        #       a per-claim timeout guard around the live search loop.
        _needs_live = (
            claims_with_evidence == 0          # strongest signal: NLI found nothing
            or _avg_sim < 0.45                 # FAISS retrieval quality is weak
        )

        logger.info(
            f"[LiveSearch gate] avg_sim={_avg_sim:.3f}, max_sim={_max_sim:.3f}, "
            f"claims_with_evidence={claims_with_evidence}/{len(raw_claims) if raw_claims else 0}, "
            f"all_evidence_neutral={all_evidence_neutral}, "
            f"_needs_live={_needs_live}"
        )

        if raw_claims and _needs_live:
            logger.info(
                f"[6] Escalating to live search "
                f"(coverage={claims_with_evidence}/{len(raw_claims)}, "
                f"avg_sim={_avg_sim:.3f})"
            )
            model = ModelRegistry.embed()

            for claim in raw_claims:
                live_results, live_found = self.evidence_retriever.retrieve_live(
                    claim["text"], model, top_k=_RETRIEVAL_TOP_K
                )
                if not live_found:
                    continue

                multi = NLIModule.classify_multi(claim["text"], live_results)

                # Collect live evidence
                for ev_result in multi["evidence_results"]:
                    all_evidence.append({
                        "evidence_text":    ev_result["text"],
                        "type":             ev_result["type"],
                        "source_label":     ev_result.get("source_label", ""),
                        "source_url":       ev_result.get("source_url"),
                        "similarity_score": ev_result["similarity_score"],
                        "nli_confidence":   ev_result["nli_confidence"],
                        "weight":           ev_result.get("weight", 0.0),
                        "claim_text":       claim["text"],
                        "source_type":      "live",
                    })

                if multi["evidence_count"] == 0:
                    continue

                live_search_used      = True
                claims_with_evidence += 1

                # Remove stale FAISS evidence for this claim now that live
                # evidence supersedes it. Without this, all_evidence contains
                # both the weak FAISS hits that *triggered* the live fallback
                # and the stronger live results, with no frontend indicator of
                # which is which — misleading for users and for scoring.
                all_evidence = [
                    e for e in all_evidence
                    if not (
                        e.get("claim_text") == claim["text"]
                        and e.get("source_type", "faiss") != "live"
                    )
                ]

                # Replace the previously added unverified entry for this claim
                claim_results = [
                    c for c in claim_results if c["text"] != claim["text"]
                ]

                claim_label = {
                    "support":    "supported",
                    "contradict": "unverified",
                    "neutral":    "neutral",
                }.get(multi["type"], "unverified")

                claim_results.append({
                    "text":             claim["text"],
                    "sentence_index":   claim["sentence_index"],
                    "label":            claim_label,
                    "confidence":       claim["confidence"],
                    "nli_confidence":   multi["nli_confidence"],
                    "evidence_found":   True,
                    "stage_failed":     "none",
                    "check_worthiness": claim_worthiness.get(
                        claim["text"][:80], {}
                    ).get("score"),
                    "vote_breakdown":   multi["vote_breakdown"],
                    "source_type":      "live",
                })

                if multi["type"] in ("support", "contradict"):
                    evidence_map[claim["sentence_index"]] = multi["type"]

            if live_search_used:
                logger.info("[6] Live search provided usable evidence.")

        # ── Coverage metrics ──────────────────────────────────────────────────
        total_claims      = len(raw_claims)
        evidence_coverage = (
            round(claims_with_evidence / total_claims, 3)
            if total_claims > 0 else 0.0
        )
        is_partial   = (total_claims > 0 and evidence_coverage == 0.0)
        unverified_claims = [
            c["text"] for c in claim_results
            if not c.get("evidence_found", True)
            or c.get("stage_failed") not in ("none", None)
        ]
        suggest_secondary_retrieval = evidence_coverage < 0.5

        logger.info(
            f"[6+7] Coverage: {claims_with_evidence}/{total_claims} "
            f"({evidence_coverage:.0%}) — partial={is_partial} "
            f"live_search_used={live_search_used}"
        )

        # Fix 9a — deduplicate evidence by full-text hash (+ fuzzy domain prefix)
        import hashlib as _hashlib
        seen_hashes: set = set()
        seen_fuzzy:  set = set()
        dedup_evidence_list = []
        for ev in sorted(all_evidence, key=lambda x: x["similarity_score"], reverse=True):
            full_hash  = _hashlib.md5(ev["evidence_text"].encode()).hexdigest()
            fuzzy_key  = ev["evidence_text"][:80].strip().lower() + "|" + ev.get("source_label", "")
            if full_hash in seen_hashes or fuzzy_key in seen_fuzzy:
                continue
            seen_hashes.add(full_hash)
            seen_fuzzy.add(fuzzy_key)
            dedup_evidence_list.append(ev)
        dedup_evidence = dedup_evidence_list[:MAX_EVIDENCE]

        # Recompute after dedup — dedup may have removed some decisive pieces,
        # though in practice dedup only removes exact duplicates.
        decisive_evidence_count = sum(
            1 for e in dedup_evidence if e.get("type") in ("support", "contradict")
        )
        all_evidence_neutral = (
            len(dedup_evidence) > 0 and decisive_evidence_count == 0
        )

        # ── 8. Credibility Scoring ────────────────────────────────────────────
        scoring = _compute_score(
            source_score=source_result["score"],
            bias_score=bias_result["score"],
            evidence_items=dedup_evidence,
            claim_results=claim_results,
            evidence_coverage=evidence_coverage,
            all_evidence_neutral=all_evidence_neutral,
        )
        logger.info(f"[8] Final score: {scoring['score']} ({scoring['label']})")

        # ── 9. Annotation ─────────────────────────────────────────────────────
        annotated = AnnotationEngine.annotate(doc, raw_claims, evidence_map)

        # ── 10. Explanation + MIL tip ─────────────────────────────────────────
        explain_kwargs = dict(
            score=scoring["score"],
            label=scoring["label"],
            source_result=source_result,
            bias_result=bias_result,
            evidence_items=dedup_evidence,
            claim_results=claim_results,
            sub_scores=scoring["sub_scores"],
            is_partial=is_partial,
            unverified_claims=unverified_claims,
        )
        mil_tip_kwargs = dict(
            score=scoring["score"],
            label=scoring["label"],
            bias_result=bias_result,
            source_result=source_result,
            claim_results=claim_results,
            evidence_items=dedup_evidence,   # actual evidence sentences + types
            is_partial=is_partial,
        )
        if use_llm:
            explanation, explanation_source = OllamaExplainer.explain_with_source(
                **explain_kwargs
            )
            mil_tip, mil_tip_source = OllamaExplainer.generate_mil_tip(**mil_tip_kwargs)
        else:
            explanation        = ExplainabilityEngine.explain(**explain_kwargs)
            explanation_source = "rule_based"
            mil_tip            = ExplainabilityEngine.mil_tip(**mil_tip_kwargs)
            mil_tip_source     = "rule_based"

        processing_ms = int((time.time() - t0) * 1000)
        logger.info(f"Pipeline complete in {processing_ms} ms")

        return {
            "score":                       scoring["score"],
            "label":                       scoring["label"],
            "is_inconclusive":             scoring.get("is_inconclusive", False),
            "explanation":                 explanation,
            "explanation_source":          explanation_source,
            "claims":                      claim_results,
            "evidence":                    dedup_evidence,
            "annotated":                   annotated,
            "source_score":                source_result["score"],
            "source_result":               source_result,
            "bias_score":                  bias_result["score"],
            "bias_result":                 bias_result,
            "sub_scores":                  scoring["sub_scores"],
            "processing_ms":               processing_ms,
            "is_partial":                  is_partial,
            "no_claims_detected":          no_claims_detected,
            "evidence_coverage":           evidence_coverage,
            "unverified_claims":           unverified_claims,
            "suggest_secondary_retrieval": suggest_secondary_retrieval,
            "live_search_used":            live_search_used,
            "all_evidence_neutral":        all_evidence_neutral,
            "mil_tip":                         mil_tip,
            "mil_tip_source":                  mil_tip_source,
        }
