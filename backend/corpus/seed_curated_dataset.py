"""
corpus/seed_curated_dataset.py
Seeds the curated_dataset_300.json into the corpus SQLite DB.

Usage:
    python corpus/seed_curated_dataset.py
    python corpus/seed_curated_dataset.py --dataset data/curated_dataset_300.json
    python corpus/seed_curated_dataset.py --dry-run

After running this script you MUST rebuild the FAISS index:
    python retrieval/build_index.py --rebuild
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import get_connection

DEFAULT_DATASET = Path(__file__).parent.parent / "data" / "curated_dataset_300.json"
PIPELINE_TYPE   = "factcheck"


def _hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()


def _get_or_create_article_id(cursor, source_domain: str, source_url: str) -> int:
    """
    Return the article id for a synthetic placeholder article representing this
    source domain. Creates one if it doesn't exist yet. This satisfies the
    NOT NULL constraint on sentences.article_id without needing real scraped content.
    """
    cursor.execute("SELECT id FROM articles WHERE url = ?", (source_url,))
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute(
        """
        INSERT INTO articles (source_domain, url, title, content, word_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            source_domain,
            source_url,
            f"[Curated dataset — {source_domain}]",
            "",
            0,
        ),
    )
    return cursor.lastrowid


def seed(dataset_path: Path, dry_run: bool = False):
    with open(dataset_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    conn   = get_connection()
    cursor = conn.cursor()

    # Verify correct columns exist
    cursor.execute("PRAGMA table_info(sentences)")
    cols = {row[1] for row in cursor.fetchall()}
    required = {"sentence_text", "source_domain", "url", "pipeline_type", "article_id"}
    missing  = required - cols
    if missing:
        print(f"[seed] ERROR: sentences table missing columns: {missing}")
        conn.close()
        sys.exit(1)

    # Fetch existing hashes to avoid duplicates
    cursor.execute("SELECT sentence_text FROM sentences")
    existing_hashes = {_hash(row[0]) for row in cursor.fetchall()}
    print(f"[seed] Existing sentences in DB: {len(existing_hashes)}")

    inserted   = 0
    skipped    = 0
    label_dist = {"SUPPORTED": 0, "REFUTED": 0, "NEI": 0}

    for entry in entries:
        claim      = entry.get("claim", "").strip()
        label      = entry.get("label", "NEI").upper()
        evidences  = entry.get("evidence", [])
        source_dom = entry.get("source", "unknown").strip()
        source_url = f"https://{source_dom}"

        label_dist[label] = label_dist.get(label, 0) + 1

        sentences_to_add = [ev.strip() for ev in evidences if ev.strip()]
        if claim:
            sentences_to_add.append(f"Claim: {claim}")

        # Get or create a placeholder article row for this source
        article_id = None
        if not dry_run and sentences_to_add:
            article_id = _get_or_create_article_id(cursor, source_dom, source_url)

        for text in sentences_to_add:
            h = _hash(text)
            if h in existing_hashes:
                skipped += 1
                continue

            if not dry_run:
                try:
                    cursor.execute(
                        """
                        INSERT INTO sentences
                            (article_id, sentence_text, source_domain, url,
                             pipeline_type, numeric_density)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            article_id,
                            text,
                            source_dom,
                            source_url,
                            PIPELINE_TYPE,
                            sum(ch.isdigit() for ch in text) / max(len(text), 1),
                        ),
                    )
                    existing_hashes.add(h)
                    inserted += 1
                except Exception as e:
                    print(f"[seed] Insert error for '{text[:60]}': {e}")
            else:
                inserted += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n[seed] {'DRY RUN — ' if dry_run else ''}Results:")
    print(f"  Dataset entries : {len(entries)}")
    print(f"  Label dist      : {label_dist}")
    print(f"  Sentences added : {inserted}")
    print(f"  Skipped (dupes) : {skipped}")
    if not dry_run:
        print(f"\n[seed] Done. Now rebuild the FAISS index:")
        print(f"       python retrieval/build_index.py --rebuild")
    else:
        print(f"\n[seed] Dry run complete — no changes written.")


def main():
    parser = argparse.ArgumentParser(description="Seed curated dataset into corpus DB")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.dataset)
    if not path.exists():
        print(f"[seed] ERROR: Dataset not found: {path}")
        sys.exit(1)

    print(f"[seed] Seeding from: {path}")
    seed(path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()