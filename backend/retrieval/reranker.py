"""
retrieval/reranker.py
Cross-encoder reranking for evidence relevance — upgraded to L-12.

Problem with bi-encoder (BGE-M3 dense) alone:
  Even with hybrid scoring, the initial retrieval ranks by approximate
  semantic + lexical overlap. A cross-encoder is more accurate because
  it reads the claim and evidence TOGETHER (not independently encoded)
  and scores their specific logical relationship.

Solution — two-stage retrieval:
  Stage 1: BGE-M3 hybrid retrieval → top-30 candidates (fast, ~ms)
  Stage 2: Cross-encoder → re-score each (claim, sentence) pair (accurate, ~1-2s)
  Result:  Keep top-k by cross-encoder score

Model: cross-encoder/ms-marco-MiniLM-L-12-v2
  Upgraded from L-6 to L-12 (12 transformer layers vs 6).
  More accurate relevance scoring for the same MS MARCO passage retrieval task.
  Speed difference on CPU: ~0.5s per query (7 pairs) — acceptable for a web app.
  Size: ~133MB

Model priority (auto-detected):
  1. models/crossencoder_ph_finetuned  — fine-tuned on PH claim-evidence pairs
  2. cross-encoder/ms-marco-MiniLM-L-12-v2  — base L-12 model (default)

Academic basis:
  Nogueira, R., & Cho, K. (2019). Passage re-ranking with BERT.
  arXiv:1901.04085. https://arxiv.org/abs/1901.04085

  Bajaj, P., Campos, D., Craswell, N., Deng, L., Gao, J., Liu, X., ...
  & Mitra, B. (2016). MS MARCO: A human generated machine reading
  comprehension dataset. arXiv:1611.09268.
  https://arxiv.org/abs/1611.09268
"""

from typing import List, Dict
from pathlib import Path

# ── Model auto-detection ──────────────────────────────────────────────────────
_ROOT           = Path(__file__).parent.parent
_FINETUNED_PATH = _ROOT / "models" / "crossencoder_ph_finetuned"
_BASE_MODEL     = "cross-encoder/ms-marco-MiniLM-L-12-v2"
RERANKER_MODEL  = str(_FINETUNED_PATH) if _FINETUNED_PATH.exists() else _BASE_MODEL

_reranker = None


def get_reranker():
    """Lazy-load the cross-encoder. Called on first rerank() invocation."""
    global _reranker
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            print(f"[Reranker] Loading cross-encoder: {RERANKER_MODEL}")
            if RERANKER_MODEL != _BASE_MODEL:
                print("[Reranker] Using fine-tuned Philippine cross-encoder.")
            # max_length=512 — truncates very long evidence sentences safely
            _reranker = CrossEncoder(RERANKER_MODEL, max_length=512)
            print("[Reranker] Cross-encoder loaded (L-12 — higher accuracy than L-6).")
        except Exception as e:
            print(f"[Reranker] Could not load cross-encoder: {e}")
            print("[Reranker] Falling back to BGE-M3 ordering.")
            _reranker = None
    return _reranker


def rerank(claim: str, candidates: List[Dict], top_k: int = 7) -> List[Dict]:
    """
    Rerank a list of candidate evidence sentences using the cross-encoder.

    The cross-encoder reads (claim, evidence) as a single concatenated input
    and outputs a relevance score. This is slower than bi-encoder cosine
    similarity but significantly more accurate for passage relevance.

    Args:
        claim:      The submitted claim text
        candidates: Evidence dicts from BGE-M3 retrieval (already threshold-filtered)
        top_k:      How many to return after reranking

    Returns:
        Top-k candidates sorted by cross-encoder rerank_score (descending).
        Each dict gains a new field: rerank_score (float).
        Falls back to original BGE-M3 ordering if cross-encoder unavailable.

    Performance note (CPU, 7 pairs):
        L-6 model: ~200ms
        L-12 model: ~400ms
        Acceptable for a web application; not a bottleneck vs DeBERTa NLI.

    Academic basis:
        Nogueira & Cho (2019) — Passage Re-ranking with BERT
        MS MARCO cross-encoder: trained on ~500K human-labeled query-passage pairs
    """
    if not candidates:
        return candidates

    reranker = get_reranker()

    if reranker is None:
        # No cross-encoder available — return BGE-M3 ranked order
        return candidates[:top_k]

    pairs = [(claim, ev["text"]) for ev in candidates]

    try:
        scores = reranker.predict(pairs)

        for ev, score in zip(candidates, scores):
            ev["rerank_score"] = round(float(score), 4)

        reranked = sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)
        return reranked[:top_k]

    except Exception as e:
        print(f"[Reranker] Prediction failed: {e}. Using BGE-M3 order.")
        return candidates[:top_k]
