"""
corpus/purge_noise.py
Applies the v4.0 quality gate retroactively to existing corpus sentences.

The v4.0 gate now requires factual markers for BOTH news and factcheck
pipelines. This script scans existing news-pipeline sentences and removes
those that would fail the new gate. Run once after upgrading to v4.0,
then rebuild the FAISS index.

Usage:
    python corpus/purge_noise.py --dry-run     # show counts only
    python corpus/purge_noise.py               # execute purge
    python corpus/purge_noise.py --pipeline news
    python corpus/purge_noise.py --pipeline factcheck

After purging, always rebuild the index:
    python retrieval/build_index.py --rebuild
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import get_connection, _sentence_quality_gate


def purge(pipeline: str = None, dry_run: bool = False) -> dict:
    conn = get_connection()
    c    = conn.cursor()

    pipelines = [pipeline] if pipeline else ["news", "factcheck", "stats"]
    stats = {}

    for pl in pipelines:
        c.execute(
            "SELECT id, sentence_text FROM sentences WHERE pipeline_type = ?", (pl,)
        )
        rows = c.fetchall()
        total    = len(rows)
        to_purge = []

        for row in rows:
            sid   = row["id"]
            text  = row["sentence_text"]
            passes = _sentence_quality_gate(text, pl)
            if not passes:
                to_purge.append(sid)

        stats[pl] = {
            "total":        total,
            "will_purge":   len(to_purge),
            "will_keep":    total - len(to_purge),
            "purge_pct":    round(len(to_purge) / total * 100, 1) if total else 0,
        }

        if not dry_run and to_purge:
            # Delete in batches of 500
            for i in range(0, len(to_purge), 500):
                batch = to_purge[i:i + 500]
                placeholders = ",".join("?" * len(batch))
                c.execute(f"DELETE FROM sentences WHERE id IN ({placeholders})", batch)
            conn.commit()
            print(f"[{pl}] Purged {len(to_purge)}/{total} sentences ({stats[pl]['purge_pct']}%)")
        else:
            print(
                f"[{pl}] DRY RUN: would purge {len(to_purge)}/{total} sentences "
                f"({stats[pl]['purge_pct']}%) — {total - len(to_purge)} would remain"
            )

    conn.close()
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Purge low-quality corpus sentences")
    parser.add_argument("--dry-run",    action="store_true", help="Show counts only, do not delete")
    parser.add_argument("--pipeline",   default=None,        help="news | factcheck | stats (default: all)")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Corpus quality purge — {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)
    stats = purge(pipeline=args.pipeline, dry_run=args.dry_run)

    print()
    print("Summary:")
    total_purged = sum(s["will_purge"] for s in stats.values())
    total_kept   = sum(s["will_keep"]  for s in stats.values())
    print(f"  Total would be removed : {total_purged}")
    print(f"  Total would remain     : {total_kept}")

    if not args.dry_run:
        print()
        print("Index is now stale — rebuild it:")
        print("  python retrieval/build_index.py --rebuild")
