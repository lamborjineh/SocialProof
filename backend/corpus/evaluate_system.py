"""
corpus/evaluate_system.py
§9 Goal 5 — System accuracy against LIAR + FEVER ground truth.

What this does:
  1. Loads labeled claims from LIAR and FEVER (already in corpus.db)
  2. Runs each claim through the SocialProof evidence retrieval + NLI pipeline
  3. Maps the dataset label to your 3-class system label:
       LIAR:  true/mostly-true       → Likely Credible
              half-true/barely-true  → Uncertain
              false/pants-fire       → Likely Misleading
       FEVER: SUPPORTS               → Likely Credible
              NOT ENOUGH INFO        → Uncertain
              REFUTES                → Likely Misleading
  4. Stores results in corpus.db → system_predictions table
  5. Prints accuracy report by dataset and overall

The /admin/research-metrics endpoint reads system_predictions to serve §9 Goal 5.

Usage:
    python corpus/evaluate_system.py                    # run all (LIAR + FEVER)
    python corpus/evaluate_system.py --dataset liar     # LIAR only
    python corpus/evaluate_system.py --dataset fever    # FEVER only
    python corpus/evaluate_system.py --limit 200        # sample 200 per dataset
    python corpus/evaluate_system.py --limit 200 --dry-run  # preview only

Performance note:
  Each claim runs evidence retrieval + NLI. On CPU with FAISS:
    ~0.5–2s per claim → 200 claims ≈ 5–10 min
  Run with --limit 200 for a fast representative sample.
  Full LIAR (12.8k) will take several hours on CPU.

Academic references:
  LIAR: Wang, W. Y. (2017). "Liar, Liar Pants on Fire": A New Benchmark
        Dataset for Fake News Detection. ACL 2017.
        https://aclanthology.org/P17-2067/

  FEVER: Thorne, J., Vlachos, A., Christodouloupoulos, C., & Mittal, A.
         (2018). FEVER: a Large-scale Dataset for Fact Extraction and
         VERification. NAACL 2018.
         https://aclanthology.org/N18-1074/
"""

import sys
import json
import time
import argparse
import sqlite3
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Label mappings (dataset → system 3-class) ─────────────────────────────────

LIAR_TO_SYSTEM = {
    "true":        "Likely Credible",
    "mostly-true": "Likely Credible",
    "half-true":   "Uncertain",
    "barely-true": "Uncertain",
    "false":       "Likely Misleading",
    "pants-fire":  "Likely Misleading",
    # Mapped labels already in DB (from load_liar.py)
    "support":    "Likely Credible",
    "neutral":    "Uncertain",
    "contradict": "Likely Misleading",
}

FEVER_TO_SYSTEM = {
    "SUPPORTS":       "Likely Credible",
    "NOT ENOUGH INFO": "Uncertain",
    "REFUTES":        "Likely Misleading",
}

# ── Corpus DB ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent.parent / "data" / "corpus.db"
FEVER_PATH = Path(__file__).parent.parent / "data" / "fever_dev.jsonl"


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_predictions_table(conn):
    """Create system_predictions table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_predictions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset          TEXT    NOT NULL,      -- 'liar' or 'fever'
            claim_text       TEXT    NOT NULL,
            ground_truth_label TEXT  NOT NULL,      -- Likely Credible / Uncertain / Likely Misleading
            predicted_label  TEXT,                  -- system output, NULL if pipeline failed
            evidence_found   INTEGER DEFAULT 0,     -- 1 if FAISS found evidence
            nli_type         TEXT,                  -- support / contradict / neutral
            score            INTEGER,               -- system credibility score 0-100
            processing_ms    INTEGER,
            evaluated_at     TEXT    DEFAULT (datetime('now')),
            UNIQUE(dataset, claim_text)
        )
    """)
    conn.commit()


def _already_evaluated(conn, dataset: str, claim_text: str) -> bool:
    row = conn.execute(
        "SELECT id FROM system_predictions WHERE dataset=? AND claim_text=?",
        (dataset, claim_text[:500])
    ).fetchone()
    return row is not None


# ── Load ground-truth claims ──────────────────────────────────────────────────

def _load_liar_claims(conn, limit: Optional[int]) -> List[Dict]:
    """
    Load LIAR claims from corpus.db sentences table.
    domain = liar-dataset.bench (set by load_liar.py)
    The pipeline_type = 'factcheck' and numeric_density carries the label
    indirectly through the article title (we stored raw_label there).
    """
    # We stored the statement as sentence_text and the label mapping as:
    #   support    → Likely Credible
    #   neutral    → Uncertain
    #   contradict → Likely Misleading
    # We recover this by joining on the article title which stores the raw label.
    query = """
        SELECT s.sentence_text AS claim, a.title AS raw_label
        FROM sentences s
        JOIN articles a ON a.id = s.article_id
        WHERE s.source_domain = 'liar-dataset.bench'
        ORDER BY RANDOM()
    """
    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    claims = []
    for row in rows:
        # Title stored as first 120 chars of statement; we need the label
        # which is encoded in the URL: liar://liar-dataset.bench/statement/NNNNN
        # We recover it from LIAR_TO_SYSTEM via the pipeline_type label
        # stored in sentences. The actual label was stored as pipeline_type='factcheck'
        # but the ground truth label must come from re-loading the raw_label.
        # Since we stored statement as title[:120], use article content for exact match.
        claim_text = row["claim"].strip()
        if not claim_text or len(claim_text) < 10:
            continue

        # Try to determine ground truth from the article title (which is statement[:120])
        # The raw_label we stored was the first 120 chars of statement — not the label.
        # We need a different approach: read the label from the URL pattern.
        claims.append({
            "claim":       claim_text,
            "ground_truth": None,   # will be resolved below
            "dataset":     "liar",
        })

    # Better approach: re-read from the articles table where we can join on URL
    # The URL encodes the row index: liar://liar-dataset.bench/statement/NNNNN
    # We need to re-join with the original label. Since we didn't store raw_label
    # in the DB, we fall back to using pipeline_type + sentence content to infer.
    # For evaluation purposes: use LIAR_TO_SYSTEM applied to the NLI output as proxy.
    # This is still valid — we're measuring: given this claim, does the system label it correctly?
    # Ground truth here = the label stored in the article title field.

    # Re-query with the actual label from the article content (we stored raw statement there)
    rows2 = conn.execute("""
        SELECT s.sentence_text AS claim, a.url
        FROM sentences s
        JOIN articles a ON a.id = s.article_id
        WHERE s.source_domain = 'liar-dataset.bench'
        ORDER BY RANDOM()
    """ + (f" LIMIT {limit}" if limit else "")).fetchall()

    # We can't recover the original LIAR label from the DB without storing it separately.
    # We'll use the pipeline_type-based approach:
    # Claim labeled 'support' in LIAR maps to Likely Credible, etc.
    # Since we only stored statement text and lost the label, we need to load LIAR again.
    # The cleanest fix: load directly from FEVER (which has labels in the jsonl)
    # and for LIAR, re-download a sample.
    print("[Evaluate] Note: LIAR ground truth requires re-loading from source.")
    print("[Evaluate] Loading LIAR sample from HuggingFace for accurate ground truth...")

    return _load_liar_from_source(limit)


def _load_liar_from_source(limit: Optional[int]) -> List[Dict]:
    """Download LIAR TSV directly for accurate ground-truth labels."""
    try:
        import requests
        url  = "https://raw.githubusercontent.com/thiagorainmaker77/liar_dataset/master/test.tsv"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        import csv, io
        reader = csv.reader(io.StringIO(resp.text), delimiter="\t")
        claims = []
        for row in reader:
            if len(row) < 3:
                continue
            raw_label = row[1].strip().lower()
            system_label = LIAR_TO_SYSTEM.get(raw_label)
            if not system_label:
                continue
            statement = row[2].strip()
            if len(statement) < 15:
                continue
            claims.append({
                "claim":        statement,
                "ground_truth": system_label,
                "dataset":      "liar",
            })
            if limit and len(claims) >= limit:
                break

        print(f"[Evaluate] Loaded {len(claims)} LIAR claims from source.")
        return claims

    except Exception as e:
        print(f"[Evaluate] Could not load LIAR from source: {e}")
        return []


def _load_fever_claims(limit: Optional[int]) -> List[Dict]:
    """Load FEVER claims from the local fever_dev.jsonl file."""
    if not FEVER_PATH.exists():
        print(f"[Evaluate] FEVER file not found: {FEVER_PATH}")
        return []

    claims = []
    with open(FEVER_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line.strip())
            except Exception:
                continue

            label_raw    = row.get("label", "")
            system_label = FEVER_TO_SYSTEM.get(label_raw)
            if not system_label:
                continue

            claim_text = row.get("claim", "").strip()
            if len(claim_text) < 10:
                continue

            claims.append({
                "claim":        claim_text,
                "ground_truth": system_label,
                "dataset":      "fever",
            })

            if limit and len(claims) >= limit:
                break

    print(f"[Evaluate] Loaded {len(claims)} FEVER claims.")
    return claims


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _run_pipeline_on_claim(claim_text: str) -> Dict:
    """
    Run evidence retrieval + NLI on a single claim.
    Returns predicted label, score, evidence_found, nli_type, processing_ms.
    """
    from pipeline.evidence_retrieval import EvidenceRetrievalModule
    from pipeline.nli import NLIModule

    retriever = EvidenceRetrievalModule()
    t0 = time.time()

    try:
        results, found = retriever.retrieve(claim_text, top_k=3)

        if not found or not results:
            ms = int((time.time() - t0) * 1000)
            return {
                "predicted_label": "Uncertain",
                "evidence_found":  0,
                "nli_type":        None,
                "score":           50,
                "processing_ms":   ms,
            }

        # Run NLI on top evidence
        best_type = "neutral"
        best_conf = 0.0
        evidence_items = []

        for ev in results:
            nli = NLIModule.classify(claim_text, ev["text"])
            if nli["nli_confidence"] > best_conf:
                best_conf = nli["nli_confidence"]
                best_type = nli["type"]

            evidence_items.append({
                "type":             nli["type"],
                "similarity_score": ev["similarity_score"],
                "nli_confidence":   nli["nli_confidence"],
            })

        # Derive predicted label from NLI result
        predicted = {
            "support":    "Likely Credible",
            "contradict": "Likely Misleading",
            "neutral":    "Uncertain",
        }.get(best_type, "Uncertain")

        # Compute a credibility score for the record
        scoring = CredibilityScoringEngine.compute(
            source_score     = 0.5,   # neutral (no URL provided)
            bias_score       = 0.0,
            evidence_items   = [
                {"type": e["type"], "similarity_score": e["similarity_score"]}
                for e in evidence_items
            ],
            claim_results    = [{"label": predicted.lower().replace(" ", "_"), "evidence_found": True}],
            evidence_coverage = 1.0,
        )

        ms = int((time.time() - t0) * 1000)
        return {
            "predicted_label": predicted,
            "evidence_found":  1,
            "nli_type":        best_type,
            "score":           scoring["score"],
            "processing_ms":   ms,
        }

    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        print(f"  [Pipeline error] {e}")
        return {
            "predicted_label": None,
            "evidence_found":  0,
            "nli_type":        None,
            "score":           None,
            "processing_ms":   ms,
        }


# ── Accuracy report ───────────────────────────────────────────────────────────

def _print_report(results: List[Dict]):
    by_dataset = defaultdict(lambda: {"total": 0, "correct": 0, "failed": 0})
    by_class   = defaultdict(lambda: {"total": 0, "correct": 0})

    for r in results:
        ds = r["dataset"]
        by_dataset[ds]["total"] += 1

        if r["predicted_label"] is None:
            by_dataset[ds]["failed"] += 1
            continue

        if r["predicted_label"] == r["ground_truth"]:
            by_dataset[ds]["correct"] += 1
            by_class[r["ground_truth"]]["correct"] += 1

        by_class[r["ground_truth"]]["total"] += 1

    print("\n" + "="*60)
    print("SYSTEM ACCURACY REPORT")
    print("="*60)

    overall_total   = 0
    overall_correct = 0

    for ds, stats in sorted(by_dataset.items()):
        total   = stats["total"]
        correct = stats["correct"]
        failed  = stats["failed"]
        acc     = round(correct / (total - failed) * 100, 1) if (total - failed) > 0 else 0
        print(f"\n  Dataset : {ds.upper()}")
        print(f"  Total   : {total}")
        print(f"  Correct : {correct}")
        print(f"  Failed  : {failed}")
        print(f"  Accuracy: {acc}%")
        overall_total   += (total - failed)
        overall_correct += correct

    print(f"\n  {'─'*40}")
    overall_acc = round(overall_correct / overall_total * 100, 1) if overall_total > 0 else 0
    print(f"  OVERALL ACCURACY: {overall_acc}% ({overall_correct}/{overall_total})")

    print(f"\n  Per-class accuracy:")
    for label, stats in sorted(by_class.items()):
        t = stats["total"]
        c = stats["correct"]
        a = round(c / t * 100, 1) if t > 0 else 0
        print(f"    {label:<22} {c}/{t} ({a}%)")

    print("="*60)
    print("\nResults stored in corpus.db → system_predictions table.")
    print("Accessible via GET /admin/research-metrics (system_accuracy field).")


# ── Main ──────────────────────────────────────────────────────────────────────

def evaluate(
    datasets: List[str],
    limit: Optional[int],
    dry_run: bool,
):
    conn = _get_db()
    _init_predictions_table(conn)

    all_claims: List[Dict] = []

    if "liar" in datasets:
        liar_claims = _load_liar_from_source(limit)
        all_claims.extend(liar_claims)

    if "fever" in datasets:
        fever_claims = _load_fever_claims(limit)
        all_claims.extend(fever_claims)

    if not all_claims:
        print("[Evaluate] No claims loaded. Check data files and network access.")
        return

    print(f"\n[Evaluate] Total claims to evaluate: {len(all_claims)}")
    print(f"[Evaluate] Dry run: {dry_run}")

    if dry_run:
        print("\n[Evaluate] DRY RUN — sample claims:")
        for c in all_claims[:5]:
            print(f"  [{c['dataset']:5s}] [{c['ground_truth']:20s}] {c['claim'][:80]}")
        print("  ...")
        return

    results = []
    skipped = 0

    for i, item in enumerate(all_claims):
        claim_text   = item["claim"]
        ground_truth = item["ground_truth"]
        dataset      = item["dataset"]

        # Skip already evaluated claims (idempotent)
        if _already_evaluated(conn, dataset, claim_text):
            skipped += 1
            continue

        print(f"  [{i+1}/{len(all_claims)}] [{dataset}] {claim_text[:70]}...")

        prediction = _run_pipeline_on_claim(claim_text)

        # Store result
        try:
            conn.execute("""
                INSERT OR IGNORE INTO system_predictions
                    (dataset, claim_text, ground_truth_label, predicted_label,
                     evidence_found, nli_type, score, processing_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                dataset,
                claim_text[:500],
                ground_truth,
                prediction["predicted_label"],
                prediction["evidence_found"],
                prediction["nli_type"],
                prediction["score"],
                prediction["processing_ms"],
            ))
            conn.commit()
        except Exception as e:
            print(f"    [DB error] {e}")
            continue

        results.append({
            "dataset":        dataset,
            "claim":          claim_text,
            "ground_truth":   ground_truth,
            "predicted_label": prediction["predicted_label"],
        })

        match = "✓" if prediction["predicted_label"] == ground_truth else "✗"
        print(
            f"    {match} GT={ground_truth:20s} "
            f"PRED={prediction['predicted_label'] or 'FAILED':20s} "
            f"({prediction['processing_ms']}ms)"
        )

    if skipped:
        print(f"\n[Evaluate] Skipped {skipped} already-evaluated claims.")

    conn.close()

    if results:
        _print_report(results)
    else:
        print("\n[Evaluate] All claims were already evaluated. "
              "Delete system_predictions rows to re-run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate SocialProof system accuracy against LIAR + FEVER ground truth."
    )
    parser.add_argument(
        "--dataset", type=str, default="all",
        choices=["liar", "fever", "all"],
        help="Which dataset to evaluate against (default: all)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max claims to evaluate per dataset (default: all). "
             "Recommended: --limit 200 for a fast representative sample.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview claims without running the pipeline",
    )
    args = parser.parse_args()

    target_datasets = ["liar", "fever"] if args.dataset == "all" else [args.dataset]

    print("="*60)
    print("SocialProof — System Accuracy Evaluation")
    print(f"Datasets : {', '.join(target_datasets)}")
    print(f"Limit    : {args.limit or 'all'} claims per dataset")
    print(f"Dry run  : {args.dry_run}")
    print("="*60)

    evaluate(
        datasets  = target_datasets,
        limit     = args.limit,
        dry_run   = args.dry_run,
    )