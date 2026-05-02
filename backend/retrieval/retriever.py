"""
retrieval/retriever.py
Evidence retrieval using BGE-M3 hybrid search (dense + sparse).

v3.5 Changes:
  DOMAIN_DIVERSE — Replaces flat MAX_PER_DOMAIN cap with domain-diversified
          retrieval. _search_index() now returns all candidates above threshold
          without a per-domain cut. search() applies _diversify() which groups
          results by domain, keeps the top DOMAIN_DIVERSE_K (default 5) per
          domain, then pools and reranks the full set.

          Why this is better than a flat cap:
            - Every domain gets a fair shot at contributing evidence regardless
              of how many sentences it has in the index.
            - Small PH gov sources (PSA, PHIVOLCS, BSP) — which might only have
              3-10 relevant sentences total — are no longer starved out by LIAR
              or large news domains at the retrieval stage.
            - The reranker then decides the final ranking from a balanced pool,
              rather than inheriting FAISS volume bias.
            - MAX_PER_DOMAIN is kept as a safety fallback for the "all" index
              pipeline only (legacy path), where domain diversity is not enforced
              by the per-pipeline split.

v3.6 Changes:
  PER_DOMAIN_PER_PIPELINE — Fixes the remaining volume-bias problem in v3.5.

          The v3.5 gap: _diversify() applied domain caps AFTER FAISS already
          returned its top-N. A large domain (e.g. rappler.com with 200 indexed
          sentences) would occupy most of FAISS's top fetch_k slots, leaving
          small domains (psa.gov.ph, 20 sentences) with zero representation in
          the raw result set — so _diversify() had nothing to cap.

          The fix: _search_index_per_domain() replaces the single FAISS search
          per pipeline with a per-domain guaranteed query. For each unique domain
          present in a pipeline's metadata, it identifies that domain's row
          indices, runs a targeted search over only those rows, and keeps the
          top DOMAIN_DIVERSE_K. All domain buckets are then pooled and merged —
          so every domain that clears RELEVANCE_THRESHOLD is guaranteed a seat
          in the reranker pool, regardless of how large or small it is.

          Flow (per pipeline):
            for each domain in pipeline_index:
                fetch top DOMAIN_DIVERSE_K sentences for that domain
            pool all domain results → deduplicate → reranker → top-k

          _search_index() is kept intact for the "all" fallback pipeline
          (legacy path) which still uses apply_domain_cap=True.

Model: BAAI/bge-m3 (Chen et al., 2024)
"""

import numpy as np
import json
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.source_registry import STATS_DOMAINS
from retrieval.utils import (
    index_files, recency_boost, trust_normalised,
    hybrid_score, is_numeric_query, pipeline_timer,
)
from retrieval.reranker import rerank as _rerank

MODEL_NAME_BASE  = "BAAI/bge-m3"
_FINETUNED_PATH  = Path(__file__).parent.parent / "models" / "bge_ph_finetuned"
MODEL_NAME       = str(_FINETUNED_PATH) if _FINETUNED_PATH.exists() else MODEL_NAME_BASE

# ── Retrieval config ──────────────────────────────────────────────────────────
# PATCH: Lowered from 0.40 → 0.30 to reduce false "no evidence found" failures.
# With a ~4k-sentence corpus, 0.40 was too aggressive and filtered out valid matches.
RELEVANCE_THRESHOLD = 0.30

# DOMAIN_DIVERSE_K: how many sentences per domain to keep after pooling.
# Every domain that clears RELEVANCE_THRESHOLD gets up to this many slots
# in the reranker pool, regardless of how large the domain is in the index.
# 5 is chosen to be generous enough to capture nuance within a domain
# (e.g. a topic with 3 angles needs 3 sentences) without letting one domain
# flood the reranker with redundant near-duplicates.
DOMAIN_DIVERSE_K             = 5  # news + stats pipelines
DOMAIN_DIVERSE_K_FACTCHECK   = 8  # factcheck: richer reranker pool for nuanced claims

# MAX_PER_DOMAIN: legacy cap used ONLY in the "all" fallback index pipeline.
# The per-pipeline paths (news/stats/factcheck) use _diversify() instead.
MAX_PER_DOMAIN      = 3

SPARSE_WEIGHT       = 0.3

try:
    import faiss as _faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False


def _load_one_index(pipeline: str):
    faiss_path, npy_path, meta_path, type_path = index_files(pipeline)

    if not meta_path.exists():
        return None

    with open(meta_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    index_type = type_path.read_text().strip() if type_path.exists() else "numpy"

    if index_type == "faiss" and FAISS_AVAILABLE and faiss_path.exists():
        faiss_index = _faiss.read_index(str(faiss_path))
        return faiss_index, None, metadata, "faiss"
    elif npy_path.exists():
        embeddings = np.load(npy_path)
        return None, embeddings, metadata, "numpy"
    else:
        return None


class Retriever:
    """
    Loads pre-built BGE-M3 embedding indices and performs hybrid search.
    """

    def __init__(self):
        self.model       = None
        self._use_bge    = False
        self._indices: dict = {}
        self.stale_index_warning: Optional[str] = None
        self._load()

    def _load(self):
        try:
            from FlagEmbedding import BGEM3FlagModel
            print(f"[Retriever] Loading BGE-M3: {MODEL_NAME}")
            self.model    = BGEM3FlagModel(MODEL_NAME, use_fp16=False)
            self._use_bge = True
            print("[Retriever] BGE-M3 loaded — hybrid dense+sparse mode active.")
        except ImportError:
            print("[Retriever] FlagEmbedding not installed. Using SentenceTransformer.")
            from sentence_transformers import SentenceTransformer
            self.model    = SentenceTransformer(MODEL_NAME_BASE)
            self._use_bge = False

        loaded_any = False
        for pipeline in ["news", "stats", "factcheck"]:
            result = _load_one_index(pipeline)
            if result is not None:
                self._indices[pipeline] = result
                fi, _, meta, itype = result
                count = fi.ntotal if fi else len(meta)
                print(f"[Retriever] [{pipeline}] {count} vectors ({itype})")
                loaded_any = True

        result = _load_one_index("all")
        if result is not None:
            self._indices["all"] = result
            fi, _, meta, itype = result
            count = fi.ntotal if fi else len(meta)
            print(f"[Retriever] [all] {count} vectors ({itype}) — fallback index")
            loaded_any = True

        if not loaded_any:
            raise FileNotFoundError(
                "No embedding index found.\n"
                "Run: python retrieval/build_index.py"
            )

        # BUG 9 FIX: Stale index detection.
        self._check_index_staleness()

    def _check_index_staleness(self):
        """
        BUG 9: Compare FAISS index file mtime against newest sentence in the DB.
        Sets self.stale_index_warning if the index is more than 24h stale.
        Safe to fail silently — just logs a warning, never raises.
        """
        import os
        try:
            oldest_index_mtime = float("inf")
            for pipeline in ["news", "stats", "factcheck", "all"]:
                faiss_path, npy_path, meta_path, _ = index_files(pipeline)
                for p in (faiss_path, npy_path):
                    if p.exists():
                        mtime = p.stat().st_mtime
                        if mtime < oldest_index_mtime:
                            oldest_index_mtime = mtime

            if oldest_index_mtime == float("inf"):
                return

            from corpus.db import get_connection
            conn = get_connection()
            c    = conn.cursor()
            try:
                c.execute("SELECT MAX(created_at) FROM sentences")
                row = c.fetchone()
                newest_db_ts = row[0] if row and row[0] else None
            except Exception:
                newest_db_ts = None
            conn.close()

            if newest_db_ts is None:
                return

            from datetime import datetime as _dt
            try:
                if isinstance(newest_db_ts, (int, float)):
                    newest_db_dt = _dt.fromtimestamp(newest_db_ts)
                else:
                    newest_db_dt = _dt.fromisoformat(str(newest_db_ts)[:19])
            except Exception:
                return

            import time as _time
            index_dt    = _dt.fromtimestamp(oldest_index_mtime)
            stale_hours = (newest_db_dt - index_dt).total_seconds() / 3600.0

            if stale_hours > 24:
                msg = (
                    f"[Retriever] ⚠ STALE INDEX WARNING: The embedding index "
                    f"is ~{stale_hours:.0f}h behind the corpus DB. "
                    f"New sentences (seeded after {index_dt.strftime('%Y-%m-%d %H:%M')}) "
                    f"will NOT be retrieved until you rebuild.\n"
                    f"  Run: python retrieval/build_index.py --rebuild"
                )
                print(msg)
                self.stale_index_warning = msg
        except Exception as _e:
            print(f"[Retriever] Staleness check skipped: {_e}")

    def _encode_query(self, claim: str) -> Tuple[np.ndarray, Optional[dict]]:
        if self._use_bge:
            output = self.model.encode(
                [claim.strip()],
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
                batch_size=1,
            )
            dense_vec       = np.array(output["dense_vecs"][0], dtype="float32")
            lexical_weights = output["lexical_weights"][0]
        else:
            dense_vec = self.model.encode(
                [claim.strip()],
                normalize_embeddings=True,
                convert_to_numpy=True,
            )[0].astype("float32")
            lexical_weights = None

        norm = np.linalg.norm(dense_vec)
        if norm > 0:
            dense_vec = dense_vec / norm

        return dense_vec, lexical_weights

    def _sparse_boost(self, lexical_weights: dict, sentence_text: str) -> float:
        if not lexical_weights:
            return 0.0
        sentence_lower = sentence_text.lower()
        raw = sum(
            float(weight)
            for token, weight in lexical_weights.items()
            if isinstance(token, str) and token.lower() in sentence_lower
        )
        return min(raw * 0.05, 0.15)

    def _search_index(self, pipeline: str, dense_vec: np.ndarray,
                      lexical_weights: Optional[dict],
                      claim: str, k: int,
                      numeric_boost: bool = False,
                      apply_domain_cap: bool = False) -> List[Dict]:
        """
        Search one pipeline index. Returns ALL candidates above RELEVANCE_THRESHOLD,
        scored and sorted — domain diversification is handled upstream by _diversify().

        apply_domain_cap=True activates the legacy MAX_PER_DOMAIN hard cap, used
        only for the "all" fallback index where the per-pipeline split isn't available.

        Fix #6: dense_score < RELEVANCE_THRESHOLD → skip immediately, before
        sparse boost. This prevents low-quality matches from being inflated.

        Fix #13: date_published is included in metadata so hybrid_score()
        can use actual publish date for recency decay.
        """
        if pipeline not in self._indices:
            return []

        faiss_index, embeddings, metadata, itype = self._indices[pipeline]
        # Fetch a generous pool: 10× k so domain diversification has material to work with.
        fetch_k = min(k * 10, len(metadata))

        if faiss_index is not None:
            scores, idxs = faiss_index.search(dense_vec.reshape(1, -1), fetch_k)
            scored_pairs = list(zip(idxs[0], scores[0]))
        else:
            sims     = np.dot(embeddings, dense_vec)
            top_idxs = np.argsort(sims)[::-1][:fetch_k]
            scored_pairs = [(int(i), float(sims[i])) for i in top_idxs]

        candidates    = []
        domain_counts = {}

        for idx, dense_score in scored_pairs:
            # Fix #6: strict threshold check BEFORE any boosting
            if dense_score < RELEVANCE_THRESHOLD:
                break
            if idx < 0 or idx >= len(metadata):
                continue

            meta   = metadata[idx]
            domain = meta["domain"]

            # Legacy cap: only active for the "all" fallback pipeline
            if apply_domain_cap and domain_counts.get(domain, 0) >= MAX_PER_DOMAIN:
                continue

            sparse   = self._sparse_boost(lexical_weights, meta["text"])
            semantic = min(1.0, max(0.0, float(dense_score) + SPARSE_WEIGHT * sparse))

            # Fix #13: pass date_published (may be None if metadata predates v3.3)
            final = hybrid_score(
                semantic, domain, meta["url"],
                numeric_boost=numeric_boost,
                date_published=meta.get("date_published"),
            )

            if apply_domain_cap:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

            candidates.append({
                "sentence_id":     meta["id"],
                "text":            meta["text"],
                "domain":          domain,
                "url":             meta["url"],
                "similarity":      round(final, 4),
                "pipeline_type":   meta.get("pipeline_type", pipeline),
                "numeric_density": meta.get("numeric_density", 0.0),
                "date_published":  meta.get("date_published"),
            })

        candidates.sort(key=lambda x: x["similarity"], reverse=True)
        return candidates

    def _diversify(self, candidates: List[Dict], per_domain_k: int = DOMAIN_DIVERSE_K, pipeline: str = None) -> List[Dict]:
        if pipeline == "factcheck" and per_domain_k == DOMAIN_DIVERSE_K:
            per_domain_k = DOMAIN_DIVERSE_K_FACTCHECK
        """
        Domain-diversified pooling: for each domain, keep the top per_domain_k
        candidates (already sorted by similarity). Pool all domain buckets and
        return sorted by similarity descending.

        This ensures every domain that has relevant evidence gets equal
        representation in the reranker pool, regardless of domain size in the
        FAISS index. A domain with 10k sentences and a domain with 50 sentences
        both contribute up to per_domain_k candidates if they clear the threshold.

        The reranker then decides final ordering from this balanced pool.
        """
        buckets: Dict[str, List[Dict]] = defaultdict(list)

        # Input is sorted by similarity desc — first N per domain are the best N
        for c in candidates:
            domain = c["domain"]
            if len(buckets[domain]) < per_domain_k:
                buckets[domain].append(c)

        pool = [c for bucket in buckets.values() for c in bucket]
        pool.sort(key=lambda x: x["similarity"], reverse=True)

        domain_summary = {d: len(b) for d, b in buckets.items()}
        print(
            f"[Retriever] _diversify: {len(candidates)} candidates → "
            f"{len(pool)} pooled across {len(buckets)} domains "
            f"(top-{per_domain_k} each) | {domain_summary}"
        )

        return pool

    def _search_index_per_domain(
        self,
        pipeline: str,
        dense_vec: np.ndarray,
        lexical_weights: Optional[dict],
        claim: str,
        numeric_boost: bool = False,
        per_domain_k: int = DOMAIN_DIVERSE_K,
    ) -> List[Dict]:
        """
        v3.6: Per-domain guaranteed retrieval for one pipeline index.

        Problem this solves:
            The v3.5 approach queried FAISS once per pipeline with a large
            fetch_k, then applied _diversify() to cap per domain. But FAISS
            returns the globally nearest vectors — a large domain (e.g.
            rappler.com, 200 sentences) fills most of the top-N slots, leaving
            small domains (psa.gov.ph, 20 sentences) with zero rows in the raw
            result. _diversify() had nothing to cap for the small domain.

        This method:
            1. Builds a per-domain index map from the pipeline's metadata
               (done once per pipeline, O(N) scan).
            2. For each domain, scores only its rows against the query vector
               and keeps the top per_domain_k above RELEVANCE_THRESHOLD.
            3. Returns the flat list of all domain buckets combined.

        Every domain is guaranteed up to per_domain_k slots if it has any
        sentences above threshold. Volume no longer determines eligibility.

        Note on FAISS sub-search:
            FAISS IndexFlatIP does not support row-subset search natively.
            For the per-domain case we use numpy dot-product over the domain's
            row subset — the index is still used for the "all" fallback which
            is volume-dominant and benefits from FAISS speed. Per-pipeline
            indices are small enough (thousands of rows) that numpy is fast.
        """
        if pipeline not in self._indices:
            return []

        faiss_index, embeddings, metadata, itype = self._indices[pipeline]

        # ── Step 1: build domain → [row_indices] map ─────────────────────────
        # O(N) over metadata; result is cached on the index tuple for reuse
        # within the same Retriever lifetime via a side-dict on self.
        cache_key = f"_domain_map_{pipeline}"
        domain_map: Dict[str, List[int]] = getattr(self, cache_key, None)
        if domain_map is None:
            domain_map = defaultdict(list)
            for row_idx, meta in enumerate(metadata):
                domain_map[meta["domain"]].append(row_idx)
            setattr(self, cache_key, domain_map)

        # ── Step 2: For each domain, score its rows and keep top per_domain_k ─
        all_domain_results: List[Dict] = []

        # We need a dense embedding matrix for numpy dot-product sub-search.
        # If the index was loaded as FAISS (no embeddings matrix in memory),
        # reconstruct it once per pipeline and cache it.
        embed_cache_key = f"_embed_cache_{pipeline}"
        embed_matrix: Optional[np.ndarray] = getattr(self, embed_cache_key, None)
        if embed_matrix is None and faiss_index is not None:
            try:
                n = faiss_index.ntotal
                d = faiss_index.d
                embed_matrix = np.zeros((n, d), dtype="float32")
                faiss_index.reconstruct_n(0, n, embed_matrix)
                setattr(self, embed_cache_key, embed_matrix)
                print(f"[Retriever] [{pipeline}] Reconstructed {n}×{d} embed matrix for per-domain search.")
            except Exception as e:
                # reconstruct_n not available on all index types (e.g. IVF).
                # Fall back to the original _search_index for this pipeline.
                print(f"[Retriever] [{pipeline}] reconstruct_n failed ({e}); falling back to _search_index.")
                return self._search_index(
                    pipeline, dense_vec, lexical_weights, claim,
                    k=per_domain_k * len(domain_map),
                    numeric_boost=numeric_boost,
                    apply_domain_cap=False,
                )
        elif embed_matrix is None:
            # numpy path — embeddings already in memory
            embed_matrix = embeddings

        for domain, row_indices in domain_map.items():
            if not row_indices:
                continue

            # Score this domain's rows
            rows       = embed_matrix[row_indices]          # shape (D_size, dim)
            sims       = np.dot(rows, dense_vec)            # shape (D_size,)
            top_local  = np.argsort(sims)[::-1][:per_domain_k * 2]  # extra headroom

            domain_hits: List[Dict] = []
            for local_i in top_local:
                dense_score = float(sims[local_i])
                if dense_score < RELEVANCE_THRESHOLD:
                    break  # sorted desc — no point continuing
                global_idx = row_indices[local_i]
                meta       = metadata[global_idx]

                sparse   = self._sparse_boost(lexical_weights, meta["text"])
                semantic = min(1.0, max(0.0, dense_score + SPARSE_WEIGHT * sparse))
                final    = hybrid_score(
                    semantic, domain, meta["url"],
                    numeric_boost=numeric_boost,
                    date_published=meta.get("date_published"),
                )

                domain_hits.append({
                    "sentence_id":     meta["id"],
                    "text":            meta["text"],
                    "domain":          domain,
                    "url":             meta["url"],
                    "similarity":      round(final, 4),
                    "pipeline_type":   meta.get("pipeline_type", pipeline),
                    "numeric_density": meta.get("numeric_density", 0.0),
                    "date_published":  meta.get("date_published"),
                })
                if len(domain_hits) >= per_domain_k:
                    break

            all_domain_results.extend(domain_hits)

        all_domain_results.sort(key=lambda x: x["similarity"], reverse=True)

        domain_summary = defaultdict(int)
        for r in all_domain_results:
            domain_summary[r["domain"]] += 1
        print(
            f"[Retriever] [{pipeline}] per-domain search: "
            f"{len(domain_map)} domains, {len(all_domain_results)} candidates "
            f"(top-{per_domain_k} each) | {dict(domain_summary)}"
        )

        return all_domain_results

    def search(self, claim: str, k: int = 7) -> List[Dict]:
        """
        Find top-k relevant evidence sentences for a claim.

        v3.6 flow (per-domain-per-pipeline):
          1. For each pipeline (news, stats, factcheck), call
             _search_index_per_domain() which scores every domain's rows
             independently and guarantees up to DOMAIN_DIVERSE_K candidates
             per domain — regardless of how large or small the domain is.
          2. Merge + deduplicate results across all three pipelines.
          3. Pass the balanced pool (up to k*3) to the cross-encoder reranker,
             which returns the final top-k.

        The key difference from v3.5: domain guarantee happens AT fetch time,
        not as a post-hoc cap. Small domains (psa.gov.ph, 20 sentences) are
        no longer crowded out of the raw FAISS result set by large domains
        (rappler.com, 200 sentences) before _diversify() ever runs.

        Fix #9: wrapped in pipeline_timer for latency logging.
        """
        if not claim or len(claim.strip()) < 5:
            return []

        try:
            from corpus.db import log_event as _log
        except Exception:
            _log = None

        with pipeline_timer("retrieval", log_fn=_log) as t:
            dense_vec, lexical_weights = self._encode_query(claim)
            numeric_q = is_numeric_query(claim)

            per_pipeline_available = any(
                p in self._indices for p in ["news", "stats", "factcheck"]
            )

            if per_pipeline_available:
                # v3.6: query each pipeline with guaranteed per-domain slots.
                # Numeric queries weight stats higher; otherwise favour news.
                # per_domain_k is the same DOMAIN_DIVERSE_K constant (default 5).
                results_news  = self._search_index_per_domain(
                    "news",      dense_vec, lexical_weights, claim,
                    numeric_boost=numeric_q,
                    per_domain_k=DOMAIN_DIVERSE_K,
                )
                results_stats = self._search_index_per_domain(
                    "stats",     dense_vec, lexical_weights, claim,
                    numeric_boost=numeric_q,
                    per_domain_k=DOMAIN_DIVERSE_K,
                )
                results_fact  = self._search_index_per_domain(
                    "factcheck", dense_vec, lexical_weights, claim,
                    numeric_boost=numeric_q,
                    per_domain_k=DOMAIN_DIVERSE_K,
                )

                # Merge and deduplicate across all three pipelines.
                # Numeric queries: stats results rank first in the merge sort
                # so that stat sentences win ties against news/factcheck.
                if numeric_q:
                    merge_order = results_stats + results_news + results_fact
                else:
                    merge_order = results_news + results_stats + results_fact

                seen           = set()
                all_candidates = []
                for r in sorted(merge_order, key=lambda x: x["similarity"], reverse=True):
                    key = r["text"][:80]
                    if key not in seen:
                        seen.add(key)
                        all_candidates.append(r)

                # all_candidates is already domain-balanced (guaranteed per-domain
                # slots were applied at fetch time). Pass up to k*3 to the reranker.
                result = all_candidates[: k * 3]

                print(
                    f"[Retriever] search pool: {len(all_candidates)} unique candidates "
                    f"across {len({r['domain'] for r in all_candidates})} domains → "
                    f"passing top {len(result)} to reranker."
                )

            else:
                # Fallback "all" index: use legacy MAX_PER_DOMAIN cap
                result = self._search_index(
                    "all", dense_vec, lexical_weights, claim, k, numeric_q,
                    apply_domain_cap=True,
                )

        # Stage 2: Cross-encoder reranking — always enforced.
        if result:
            result = _rerank(claim, result, top_k=k)

        return result

    def has_index(self) -> bool:
        return bool(self._indices)


# ── Singleton accessor ────────────────────────────────────────────────────────
_retriever_instance = None

def get_retriever() -> Retriever:
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = Retriever()
    return _retriever_instance


if __name__ == "__main__":
    r = Retriever()
    test_claim = "The Bangko Sentral ng Pilipinas raised interest rates in 2024"
    print(f"\nSearching for: '{test_claim}'\n")
    results = r.search(test_claim, k=5)
    if not results:
        print("No results. Scrape corpus first: python corpus/scraper.py --limit 200")
        print("Then build index: python retrieval/build_index.py")
    else:
        for i, res in enumerate(results, 1):
            print(f"[{i}] Score: {res['similarity']:.4f} | {res['domain']} | {res['pipeline_type']}")
            print(f"     {res['text'][:120]}...")
            print(f"     URL: {res['url']}")
            if res.get("date_published"):
                print(f"     Published: {res['date_published']}")
            print()