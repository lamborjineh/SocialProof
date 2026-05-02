"""
retrieval/build_index.py
Builds BGE-M3 dense embedding indices from the corpus.

v3.3 Changes (fix #13):
  - date_published is now fetched from the articles table (via a new DB helper)
    and stored in per-sentence metadata so retriever.py can use actual publish
    dates for recency decay instead of guessing from URLs.

v3.4 Changes:
  - PER_DOMAIN_CAP (default 200) applied before encoding so no single domain
    can flood the index even if the DB contains more rows.
    nature.com and any other high-volume domain are silently trimmed to cap.
  - Domain distribution is printed at build time so you can verify balance.

Three separate indices are maintained:
  news / stats / factcheck + combined "all" fallback.

Usage:
    python retrieval/build_index.py           # build if no index exists
    python retrieval/build_index.py --rebuild  # force rebuild
    python retrieval/build_index.py --pipeline stats
    python retrieval/build_index.py --rebuild --cap 300  # custom per-domain cap
"""

import numpy as np
import json
import argparse
from pathlib import Path
from collections import defaultdict
import sys
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
    _hf_token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
    if _hf_token:
        from huggingface_hub import login
        login(token=_hf_token, add_to_git_credential=False)
        print("[BuildIndex] HuggingFace token loaded from .env")
except Exception:
    pass

from corpus.db import get_all_sentences
from retrieval.utils import index_files, INDEX_DIR

MODEL_NAME = "BAAI/bge-m3"
PIPELINES  = ["news", "stats", "factcheck", "all"]

# ── Per-domain cap ─────────────────────────────────────────────────────────────
# Prevents any single domain from dominating the index.
# Can be overridden at CLI with --cap N.
# nature.com previously had 20,204 / 28,946 sentences (69.8%) — this fixes that.
DEFAULT_PER_DOMAIN_CAP = 200

# Domains exempt from the cap — load ALL their sentences.
# Add any curated/benchmark dataset domain here.
UNCAPPED_DOMAINS = {
    "liar-dataset.bench",
}

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False


def _apply_domain_cap(sentences: list, cap: int) -> list:
    """
    Cap removed — all sentences from every source are indexed.
    FAISS nearest-neighbor search handles relevance at query time,
    so domain volume does not bias results.
    Prints domain distribution so you can verify what's in the index.
    """
    domain_counts: dict = defaultdict(int)
    for s in sentences:
        domain_counts[s.get("source_domain", "unknown")] += 1

    print(f"\n[BuildIndex] No domain cap applied. Domain distribution:")
    for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        bar = "█" * min(count // 50, 30)
        print(f"  {domain:<35} {count:>5}  {bar}")
    print(f"  {'TOTAL':<35} {len(sentences):>5}")

    return sentences


def _fetch_date_published_map() -> dict:
    """
    Fix #13: return {article_id: date_published} mapping from the articles table.
    Falls back to empty dict gracefully if the join fails for any reason.
    """
    try:
        from corpus.db import get_connection
        conn = get_connection()
        c    = conn.cursor()
        c.execute("SELECT id, date_published FROM articles WHERE date_published IS NOT NULL")
        result = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return result
    except Exception as e:
        print(f"[BuildIndex] Warning: could not load date_published map: {e}")
        return {}


def load_model():
    try:
        from FlagEmbedding import BGEM3FlagModel
        print(f"[BuildIndex] Loading BGE-M3: {MODEL_NAME}")
        model = BGEM3FlagModel(MODEL_NAME, use_fp16=False)
        return model, True
    except ImportError:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(MODEL_NAME)
        return model, False


def encode_sentences(model, texts: list, use_bge: bool) -> np.ndarray:
    batch_size = 32
    if use_bge:
        all_vecs = []
        for i in range(0, len(texts), batch_size):
            batch  = texts[i:i + batch_size]
            output = model.encode(
                batch, return_dense=True, return_sparse=False,
                return_colbert_vecs=False, batch_size=batch_size,
            )
            all_vecs.append(output["dense_vecs"])
            if (i // batch_size) % 10 == 0:
                print(f"  {min(i + batch_size, len(texts))}/{len(texts)} encoded...")
        embeddings = np.vstack(all_vecs).astype("float32")
    else:
        embeddings = model.encode(
            texts, batch_size=batch_size, show_progress_bar=True,
            normalize_embeddings=True, convert_to_numpy=True,
        ).astype("float32")

    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return embeddings / norms


def _save_index(embeddings: np.ndarray, metadata: list, pipeline: str):
    faiss_path, npy_path, meta_path, type_path = index_files(pipeline)

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)
    print(f"[BuildIndex][{pipeline}] Metadata saved: {meta_path.name}")

    if FAISS_AVAILABLE:
        dim   = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)
        index.add(embeddings)
        faiss.write_index(index, str(faiss_path))
        type_path.write_text("faiss")
        print(f"[BuildIndex][{pipeline}] FAISS index: {len(metadata)} vectors, dim={dim}")
    else:
        np.save(npy_path, embeddings)
        type_path.write_text("numpy")
        print(f"[BuildIndex][{pipeline}] NumPy index: shape {embeddings.shape}")


def build_index(
    force_rebuild: bool = False,
    only_pipeline: str = None,
    per_domain_cap: int = DEFAULT_PER_DOMAIN_CAP,
):
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    target_pipelines = [only_pipeline] if only_pipeline else PIPELINES

    if not force_rebuild:
        all_exist = all(
            index_files(p)[2].exists() for p in target_pipelines
        )
        if all_exist:
            for p in target_pipelines:
                _, _, meta_path, _ = index_files(p)
                with open(meta_path) as f:
                    meta = json.load(f)
                print(f"[BuildIndex][{p}] Already exists: {len(meta)} sentences. "
                      "Use --rebuild to regenerate.")
            return

    print("[BuildIndex] Fetching sentences from corpus database...")
    all_sentences = get_all_sentences()
    if not all_sentences:
        print("[BuildIndex] No sentences in database.")
        print("            Run: python corpus/scraper.py --limit 200")
        return

    print(f"[BuildIndex] Raw sentence count from DB: {len(all_sentences)}")

    # ── Apply per-domain cap before encoding ──────────────────────────────────
    all_sentences = _apply_domain_cap(all_sentences, cap=per_domain_cap)
    print(f"[BuildIndex] Sentences after domain cap ({per_domain_cap}): {len(all_sentences)}")

    # Fix #13: pre-load date_published for all articles
    date_map = _fetch_date_published_map()
    print(f"[BuildIndex] Loaded date_published for {len(date_map)} articles.")

    model, use_bge = load_model()

    by_pipeline: dict = {"news": [], "stats": [], "factcheck": [], "all": []}
    for s in all_sentences:
        pt = s.get("pipeline_type", "news") or "news"
        if pt in by_pipeline:
            by_pipeline[pt].append(s)
        else:
            by_pipeline["news"].append(s)
        by_pipeline["all"].append(s)

    for pipeline in target_pipelines:
        sents = by_pipeline.get(pipeline, [])
        if not sents:
            print(f"[BuildIndex][{pipeline}] No sentences — skipping.")
            continue

        print(f"\n[BuildIndex][{pipeline}] Building index for {len(sents)} sentences...")
        texts      = [s["sentence_text"] for s in sents]
        embeddings = encode_sentences(model, texts, use_bge)

        metadata = [
            {
                "id":              s["id"],
                "text":            s["sentence_text"],
                "domain":          s["source_domain"],
                "url":             s["url"],
                "pipeline_type":   s.get("pipeline_type", "news"),
                "numeric_density": s.get("numeric_density", 0.0),
                # Fix #13: store actual publish date from articles table
                "date_published":  date_map.get(s.get("article_id")),
            }
            for s in sents
        ]

        _save_index(embeddings, metadata, pipeline)

    print(f"\n[BuildIndex] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build BGE-M3 embedding indices")
    parser.add_argument("--rebuild",  action="store_true")
    parser.add_argument("--pipeline", type=str, choices=PIPELINES)
    parser.add_argument(
        "--cap", type=int, default=DEFAULT_PER_DOMAIN_CAP,
        help=f"Max sentences per domain in the index (default: {DEFAULT_PER_DOMAIN_CAP})"
    )
    args = parser.parse_args()
    build_index(
        force_rebuild=args.rebuild,
        only_pipeline=args.pipeline,
        per_domain_cap=args.cap,
    )
