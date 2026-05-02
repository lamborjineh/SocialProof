"""
SocialProof — Module 5: Evidence Retrieval
v3.1 — ClaimBuster integration + FAISS primary + live search fallback
       + hardcoded corpus emergency fallback

Retrieval priority:
  1. FAISS retriever (BGE-M3, built corpus)
     → if evidence_coverage == 0, escalate to live search
  2. Live search (Google News RSS + Google Fact Check API)
     → only fires when FAISS finds nothing for ALL claims
  3. Hardcoded corpus (~40 entries, always available, no setup required)
     → emergency fallback when FAISS index files are missing entirely

ClaimBuster (optional pre-retrieval step):
  Scores each detected claim for check-worthiness (0.0–1.0).
  Claims below CHECK_WORTHY_THRESHOLD are flagged as low-priority but
  still processed — this never blocks retrieval, only annotates claims.
  API key: set CLAIMBUSTER_API_KEY in .env
  Register: https://idir.uta.edu/claimbuster/api/

  Academic reference:
    Hassan, N., Arslan, F., Li, C., & Tremayne, M. (2017).
    Toward automated fact-checking: Detecting check-worthy factual claims
    by claimbuster. KDD 2017. https://doi.org/10.1145/3097983.3098131
"""

import hashlib
import os
import re
import requests as _requests
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

from sentence_transformers import util

from config import logger
from core.model_registry import ModelRegistry

# ── ClaimBuster config ────────────────────────────────────────────────────────
# Register at: https://idir.uta.edu/claimbuster/api/
# Then add CLAIMBUSTER_API_KEY=<your_key> to your .env file
CLAIMBUSTER_API_KEY: str = os.getenv("CLAIMBUSTER_API_KEY", "API_KEY_HERE")
CLAIMBUSTER_URL     = "https://idir.uta.edu/claimbuster/api/v2/score/text/"
CHECK_WORTHY_THRESHOLD = 0.5   # claims below this are flagged low-priority

# Cosine-similarity floor for the hardcoded-corpus fallback
SIM_THRESHOLD = 0.10

# ── Unverified-claim tracking (corpus gap analysis) ──────────────────────────
_unverified_counter: Dict[str, int] = defaultdict(int)
_unverified_texts:   Dict[str, str] = {}
_faiss_retriever = None


def record_unverified(claim_text: str) -> None:
    h = hashlib.md5(claim_text.encode()).hexdigest()
    _unverified_counter[h] += 1
    _unverified_texts[h] = claim_text
    if _unverified_counter[h] >= 3:
        logger.warning(
            f"[CORPUS GAP] Claim seen {_unverified_counter[h]}x with no evidence — "
            f"add to corpus: '{claim_text[:80]}…'"
        )


def get_unverified_log() -> List[Dict]:
    return sorted(
        [{"claim": _unverified_texts[h], "count": c}
         for h, c in _unverified_counter.items()],
        key=lambda x: x["count"],
        reverse=True,
    )


# ── ClaimBuster ───────────────────────────────────────────────────────────────

def score_claim_worthiness(claim_text: str) -> Dict:
    """
    Improved local check-worthiness heuristic (Fix 4a).

    Signals based on Hassan et al. (2017) — KDD check-worthiness research:
      - Attributed assertion (has a named source/speaker)    +0.25
      - Specific statistic with unit                         +0.25
      - Superlative or absolute word                         +0.15
      - Causal language                                      +0.15
      - Specific number (year or decimal)                    +0.10
      - Appropriate length (6–80 words)                      +0.10
      - Penalty: too short or too long                       -0.20

    Fix 4b: if CLAIMBUSTER_API_KEY is set to a real key (not "API_KEY_HERE"),
    uses the ClaimBuster API instead and falls back to this heuristic on error.
    """
    # Fix 4b: use real ClaimBuster if key is configured
    if CLAIMBUSTER_API_KEY and CLAIMBUSTER_API_KEY != "API_KEY_HERE":
        try:
            resp = _requests.get(
                f"{CLAIMBUSTER_URL}{_requests.utils.quote(claim_text)}",
                headers={"x-api-key": CLAIMBUSTER_API_KEY},
                timeout=3.0,
            )
            if resp.status_code == 200:
                data    = resp.json()
                results = data.get("results", [{}])
                cb_score = float(results[0].get("score", 0.5)) if results else 0.5
                return {
                    "score":        round(cb_score, 4),
                    "check_worthy": cb_score >= CHECK_WORTHY_THRESHOLD,
                    "source":       "claimbuster",
                    "signals":      [],
                }
        except Exception as e:
            logger.debug(f"[ClaimBuster] API unavailable ({e}), using local heuristic.")

    # Local heuristic (always used when API unavailable or key not set)
    _ATTRIBUTED_RE   = re.compile(
        r"\b(according to|said|announced|reported|confirmed|stated|claimed|"
        r"showed?|found|revealed|declared|warned)\b", re.I
    )
    _STATISTIC_RE    = re.compile(
        r"\b\d[\d,.]*\s*(%|percent|million|billion|trillion|thousand)\b", re.I
    )
    _SUPERLATIVE_RE  = re.compile(
        r"\b(first|last|only|never|always|highest|lowest|largest|smallest|"
        r"most|least|best|worst|all|none|every)\b", re.I
    )
    _CAUSAL_RE       = re.compile(
        r"\b(cause[sd]?|led to|resulted in|linked to|associated with|"
        r"responsible for|due to|leads to)\b", re.I
    )
    _SPECIFIC_NUM_RE = re.compile(r"\b\d{4}\b|\b\d+\.\d+\b")

    text    = claim_text.strip()
    words   = text.split()
    score   = 0.0
    signals = []

    if _ATTRIBUTED_RE.search(text):
        score += 0.25; signals.append("attributed_assertion")
    if _STATISTIC_RE.search(text):
        score += 0.25; signals.append("statistic_with_unit")
    if _SUPERLATIVE_RE.search(text):
        score += 0.15; signals.append("superlative_absolute")
    if _CAUSAL_RE.search(text):
        score += 0.15; signals.append("causal_language")
    if _SPECIFIC_NUM_RE.search(text):
        score += 0.10; signals.append("specific_number")
    if 6 <= len(words) <= 80:
        score += 0.10
    elif len(words) < 5 or len(words) > 100:
        score -= 0.20

    score = max(0.0, min(1.0, score))
    return {
        "score":        round(score, 4),
        "check_worthy": score >= CHECK_WORTHY_THRESHOLD,
        "source":       "local_heuristic_v2",
        "signals":      signals,
    }


def _try_load_faiss_retriever():
    global _faiss_retriever
    if _faiss_retriever is not None:
        return _faiss_retriever

    try:
        from retrieval.retriever import get_retriever
        r = get_retriever()
        if r.has_index():
            _faiss_retriever = r
            logger.info(
                "[EvidenceRetrieval] FAISS retriever loaded — "
                "using real corpus for evidence retrieval."
            )
        else:
            _faiss_retriever = False
            logger.warning("[EvidenceRetrieval] FAISS retriever has no index.")
    except FileNotFoundError as e:
        _faiss_retriever = False
        logger.warning(f"[EvidenceRetrieval] FAISS index files not found: {e}")
    except ImportError as e:
        _faiss_retriever = False
        logger.warning(f"[EvidenceRetrieval] FAISS dependencies missing: {e}")
    except Exception as e:
        _faiss_retriever = False
        logger.warning(f"[EvidenceRetrieval] FAISS retriever unavailable: {e}")

    return _faiss_retriever


class EvidenceRetrievalModule:
    """
    Evidence retrieval:
      Primary   — FAISS (BGE-M3 corpus)
      Fallback1 — Live search (when FAISS coverage == 0)
      Fallback2 — Hardcoded corpus (when FAISS index missing entirely)

    Pre-retrieval:
      ClaimBuster scores each claim for check-worthiness.
      Low-worthiness claims are annotated but still processed.
    """

    # ── Hardcoded test corpus (emergency fallback only) ───────────────────────
    CORPUS: List[Dict] = [
        {
            "text": "The World Health Organization has not classified coffee as a Group 1 carcinogen.",
            "label": "contradict",
            "source_label": "WHO / IARC Monographs",
            "source_url": "https://www.who.int",
        },
        {
            "text": "Vaccines do not cause autism. The original 1998 study claiming this link was retracted.",
            "label": "contradict",
            "source_label": "CDC — Vaccine Safety",
            "source_url": "https://www.cdc.gov/vaccinesafety/concerns/autism.html",
        },
        {
            "text": "Vaccines are safe and effective. The scientific consensus from WHO, CDC confirms benefits.",
            "label": "support",
            "source_label": "WHO — Vaccine Safety",
            "source_url": "https://www.who.int",
        },
        {
            "text": "5G technology does not spread COVID-19. Viruses cannot travel on radio waves.",
            "label": "contradict",
            "source_label": "WHO — Mythbusters",
            "source_url": "https://www.who.int/emergencies/diseases/novel-coronavirus-2019/advice-for-public/myth-busters",
        },
        {
            "text": "mRNA COVID-19 vaccines do not alter human DNA. mRNA never enters the cell nucleus.",
            "label": "contradict",
            "source_label": "CDC — How mRNA Vaccines Work",
            "source_url": "https://www.cdc.gov",
        },
        {
            "text": "Climate change is real and primarily driven by human activities, according to the IPCC.",
            "label": "support",
            "source_label": "IPCC Sixth Assessment Report",
            "source_url": "https://www.ipcc.ch/assessment-report/ar6/",
        },
        {
            "text": "The Philippine Statistics Authority publishes official monthly inflation statistics.",
            "label": "neutral",
            "source_label": "Philippine Statistics Authority",
            "source_url": "https://psa.gov.ph",
        },
        {
            "text": "VERA Files is an independent fact-checking organization accredited by the IFCN.",
            "label": "neutral",
            "source_label": "VERA Files",
            "source_url": "https://verafiles.org",
        },
        {
            "text": "Historical records confirm that Martial Law in the Philippines (1972-1981) involved "
                    "systematic human rights violations.",
            "label": "support",
            "source_label": "Amnesty International",
            "source_url": "https://www.amnesty.org",
        },
        {
            "text": "Natural does not mean safe. Many natural substances are toxic (arsenic, cyanide).",
            "label": "neutral",
            "source_label": "Science-Based Medicine",
            "source_url": "https://sciencebasedmedicine.org",
        },
    ]

    def __init__(self):
        self._embeddings = None
        self._texts      = [c["text"] for c in self.CORPUS]

    # ── Public interface ──────────────────────────────────────────────────────

    def retrieve(
        self,
        claim_text: str,
        top_k: int = 3,
        check_worthiness: Optional[Dict] = None,
    ) -> Tuple[List[Dict], bool]:
        """
        Return (results, any_found).

        check_worthiness: optional pre-scored ClaimBuster result.
        If provided and score < threshold, logs a warning but still retrieves.
        """
        if check_worthiness and not check_worthiness.get("check_worthy", True):
            logger.info(
                f"[EvidenceRetrieval] Low check-worthiness "
                f"({check_worthiness['score']:.2f}) for: '{claim_text[:60]}' — "
                "processing anyway."
            )

        retriever = _try_load_faiss_retriever()
        if retriever:
            return self._retrieve_faiss(claim_text, top_k, retriever)
        return self._retrieve_hardcoded(claim_text, top_k)

    def retrieve_live(self, claim_text: str, model, top_k: int = 5) -> Tuple[List[Dict], bool]:
        """
        Live search fallback — called by orchestrator when FAISS coverage == 0.
        Requires the embedding model to be passed in (no second model load).
        """
        try:
            from retrieval.live_search import live_search
            raw = live_search(claim_text, model, k=top_k)
            if not raw:
                return [], False

            mapped = []
            for r in raw[:top_k]:
                mapped.append({
                    "text":             r.get("text", ""),
                    "source_label":     r.get("source_label") or r.get("domain", ""),
                    "source_url":       r.get("url", ""),
                    "similarity_score": float(r.get("similarity", 0.35)),
                    "nli_confidence":   float(r.get("nli_confidence", 0.5)),
                    "found":            True,
                    "source_type":      r.get("source_type", "live"),
                })
            return mapped, True
        except Exception as e:
            logger.warning(f"[EvidenceRetrieval] Live search failed: {e}")
            return [], False

    # ── FAISS retrieval ───────────────────────────────────────────────────────

    def _retrieve_faiss(
        self, claim_text: str, top_k: int, retriever
    ) -> Tuple[List[Dict], bool]:
        try:
            raw_results = retriever.search(claim_text, k=top_k * 2)
        except Exception as e:
            logger.warning(f"[EvidenceRetrieval] FAISS search failed: {e}. Falling back.")
            return self._retrieve_hardcoded(claim_text, top_k)

        if not raw_results:
            return [], False

        mapped = []
        for r in raw_results[:top_k]:
            mapped.append({
                "text":             r["text"],
                "source_label":     r["domain"],
                "source_url":       r.get("url", ""),
                "similarity_score": float(r["similarity"]),
                "found":            True,
                "source_type":      "faiss",
            })

        return mapped, True

    # ── Hardcoded corpus retrieval ────────────────────────────────────────────

    def _get_hardcoded_embeddings(self):
        if self._embeddings is None:
            logger.info(f"Pre-computing {len(self._texts)} hardcoded corpus embeddings…")
            self._embeddings = ModelRegistry.embed().encode(
                self._texts, convert_to_tensor=True, show_progress_bar=False
            )
        return self._embeddings

    def _retrieve_hardcoded(
        self, claim_text: str, top_k: int
    ) -> Tuple[List[Dict], bool]:
        claim_emb  = ModelRegistry.embed().encode(claim_text, convert_to_tensor=True)
        corpus_emb = self._get_hardcoded_embeddings()
        scores     = util.cos_sim(claim_emb, corpus_emb)[0].tolist()

        ranked  = sorted(zip(scores, self.CORPUS), key=lambda x: x[0], reverse=True)
        results = []
        for sim_score, entry in ranked[:top_k]:
            if sim_score < SIM_THRESHOLD:
                break
            results.append({
                "text":             entry["text"],
                "source_label":     entry["source_label"],
                "source_url":       entry.get("source_url", ""),
                "similarity_score": round(float(sim_score), 4),
                "found":            True,
                "source_type":      "hardcoded",
            })

        return results, len(results) > 0