"""
corpus/load_liar.py
Downloads the LIAR dataset and inserts it into corpus.db as factcheck-pipeline
sentences. Run this AFTER reset_db.py and BEFORE build_index.py.

LIAR (Wang, 2017 / ACL) — 12.8k labeled political statements from PolitiFact.
Each row is a self-contained {statement, speaker, label} — no external corpus
needed, unlike FEVER. This makes it a clean drop-in for your factcheck index.

Label mapping (6-class → your 3-class schema):
  true / mostly-true          → "support"    (statement is verifiable/true)
  false / pants-fire          → "contradict" (statement is false/debunked)
  half-true / barely-true     → "neutral"    (mixed or unverifiable)

Usage:
    python corpus/load_liar.py               # downloads + inserts all splits
    python corpus/load_liar.py --split train # only train split
    python corpus/load_liar.py --dry-run     # preview without inserting

Source: https://huggingface.co/datasets/liar
Paper:  Wang (2017). "Liar, Liar Pants on Fire": A New Benchmark Dataset for
        Fake News Detection. ACL 2017. https://aclanthology.org/P17-2067/
"""

import sys
import csv
import argparse
import io
from pathlib import Path
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import get_connection, init_db

# ── Label mapping ─────────────────────────────────────────────────────────────
LABEL_MAP = {
    "true":        "support",
    "mostly-true": "support",
    "half-true":   "neutral",
    "barely-true": "neutral",
    "false":       "contradict",
    "pants-fire":  "contradict",
}

# ── LIAR TSV column indices ───────────────────────────────────────────────────
# Format: id | label | statement | subject | speaker | job | state | party |
#         barely_true | false | half_true | mostly_true | pants_fire | context
COL_LABEL     = 1
COL_STATEMENT = 2
COL_SPEAKER   = 4
COL_JOB       = 5
COL_CONTEXT   = 13

# Minimum statement length to skip noise
MIN_LENGTH = 20

# HuggingFace raw file URLs for each split
HF_BASE = "https://huggingface.co/datasets/liar/resolve/main/data"
SPLITS  = {
    "train": f"{HF_BASE}/train.jsonl",
    "valid": f"{HF_BASE}/validation.jsonl",
    "test":  f"{HF_BASE}/test.jsonl",
}

# Fallback: direct TSV from original paper repo
PAPER_BASE = "https://raw.githubusercontent.com/thiagorainmaker77/liar_dataset/master"
SPLITS_TSV = {
    "train": f"{PAPER_BASE}/train.tsv",
    "valid": f"{PAPER_BASE}/valid.tsv",
    "test":  f"{PAPER_BASE}/test.tsv",
}


def _download_tsv(split: str) -> List[List[str]]:
    """Download LIAR TSV for the given split. Returns list of rows."""
    import requests

    url = SPLITS_TSV[split]
    print(f"[LIAR] Downloading {split} split: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text), delimiter="\t")
    rows   = list(reader)
    print(f"[LIAR] Downloaded {len(rows)} rows")
    return rows


def _parse_rows(rows: List[List[str]]) -> List[Dict]:
    """Parse TSV rows into statement dicts."""
    parsed = []
    for row in rows:
        if len(row) < 3:
            continue

        raw_label = row[COL_LABEL].strip().lower()
        label     = LABEL_MAP.get(raw_label)
        if label is None:
            continue   # unknown label — skip

        statement = row[COL_STATEMENT].strip() if len(row) > COL_STATEMENT else ""
        if len(statement) < MIN_LENGTH:
            continue

        # Build a readable source attribution
        speaker = row[COL_SPEAKER].strip() if len(row) > COL_SPEAKER else ""
        job     = row[COL_JOB].strip()     if len(row) > COL_JOB     else ""
        context = row[COL_CONTEXT].strip() if len(row) > COL_CONTEXT else ""
        source_note = ", ".join(filter(None, [speaker, job, context]))

        parsed.append({
            "statement":   statement,
            "label":       label,
            "raw_label":   raw_label,
            "source_note": source_note,
        })

    return parsed


def _insert_statements(statements: List[Dict], dry_run: bool = False) -> int:
    """
    Insert parsed LIAR statements into corpus.db.

    Each statement is inserted as:
      - One article row  (url = synthetic liar://id, domain = politifact.com)
      - One sentence row (the statement text, pipeline_type = factcheck)

    politifact.com has reputation 0.90 in source_registry so it will pass
    the REPUTATION_THRESHOLD filter in live_search.py.
    """
    if dry_run:
        print(f"[LIAR] DRY RUN — would insert {len(statements)} statements")
        for s in statements[:5]:
            print(f"  [{s['raw_label']:12s} → {s['label']:10s}] {s['statement'][:80]}")
        print("  ...")
        return 0

    conn      = get_connection()
    c         = conn.cursor()
    inserted  = 0
    skipped   = 0

    for i, s in enumerate(statements):
        # Synthetic URL so we can use INSERT OR IGNORE for idempotency
        synthetic_url = f"liar://liar-dataset.bench/statement/{i:05d}"

        try:
            c.execute(
                "INSERT OR IGNORE INTO articles "
                "(source_domain, url, title, content, date_published, word_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "liar-dataset.bench",
                    synthetic_url,
                    s["statement"][:120],
                    s["statement"],
                    None,
                    len(s["statement"].split()),
                ),
            )
            if c.rowcount == 0:
                skipped += 1
                continue

            article_id = c.lastrowid

            c.execute(
                "INSERT INTO sentences "
                "(article_id, source_domain, url, sentence_text, sentence_index, "
                " pipeline_type, numeric_density) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    article_id,
                    "liar-dataset.bench",
                    synthetic_url,
                    s["statement"],
                    0,
                    "factcheck",
                    0.0,
                ),
            )
            inserted += 1

        except Exception as e:
            print(f"[LIAR] Warning: insert failed for row {i}: {e}")
            continue

    conn.commit()
    conn.close()
    return inserted


def load_liar(splits: List[str], dry_run: bool = False) -> None:
    init_db()   # no-op if tables already exist

    label_counts: Dict[str, int] = {}
    total_inserted = 0

    for split in splits:
        print(f"\n[LIAR] === Processing split: {split} ===")
        try:
            rows = _download_tsv(split)
        except Exception as e:
            print(f"[LIAR] Failed to download {split}: {e}")
            continue

        statements = _parse_rows(rows)
        print(f"[LIAR] Parsed {len(statements)} valid statements from {len(rows)} rows")

        # Report label distribution
        for s in statements:
            label_counts[s["label"]] = label_counts.get(s["label"], 0) + 1

        n = _insert_statements(statements, dry_run=dry_run)
        if not dry_run:
            print(f"[LIAR] Inserted {n} new statements ({len(statements) - n} already existed)")
            total_inserted += n

    print(f"\n[LIAR] Label distribution across all splits:")
    for label, count in sorted(label_counts.items()):
        print(f"  {label:10s}: {count}")

    if not dry_run:
        print(f"\n[LIAR] Total inserted into corpus.db: {total_inserted}")
        print("[LIAR] Done. These statements are now in the 'factcheck' pipeline.")
        print("[LIAR] Run: python retrieval/build_index.py --rebuild --pipeline factcheck")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load LIAR dataset into corpus.db")
    parser.add_argument(
        "--split", type=str, default="all",
        choices=["train", "valid", "test", "all"],
        help="Which dataset split to load (default: all)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without inserting into the database",
    )
    args = parser.parse_args()

    target_splits = list(SPLITS_TSV.keys()) if args.split == "all" else [args.split]
    load_liar(splits=target_splits, dry_run=args.dry_run)
