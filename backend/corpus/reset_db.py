"""
corpus/reset_db.py
Wipes corpus data (articles, sentences, structured_stats) and re-initialises
the empty tables so you can do a clean scrape + index rebuild.

Safe tables (NOT touched):
  users, sessions, analysis_log, feedback, shared_results,
  trusted_domains, system_logs

Usage:
    python corpus/reset_db.py            # asks for confirmation
    python corpus/reset_db.py --confirm  # skip the prompt (for scripts)
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import get_connection, init_db

CORPUS_TABLES = ["sentences", "structured_stats", "articles"]


def reset_corpus(confirm: bool = False) -> None:
    conn = get_connection()
    c    = conn.cursor()

    # Count rows before wipe so the user can see what gets deleted
    totals = {}
    for t in CORPUS_TABLES:
        c.execute(f"SELECT COUNT(*) FROM {t}")
        totals[t] = c.fetchone()[0]

    print("\n[ResetDB] The following corpus rows will be permanently deleted:")
    for t, n in totals.items():
        print(f"  {t:<22} {n:>7} rows")
    print()

    if not confirm:
        answer = input("Type YES to continue, anything else to cancel: ").strip()
        if answer != "YES":
            print("[ResetDB] Cancelled.")
            conn.close()
            return

    print("[ResetDB] Wiping corpus tables...")

    # Disable FK checks temporarily so we can drop in any order
    c.execute("PRAGMA foreign_keys = OFF")

    for t in CORPUS_TABLES:
        c.execute(f"DELETE FROM {t}")
        print(f"  ✓ {t} cleared")

    # Reset AUTOINCREMENT counters so IDs start from 1 again
    for t in CORPUS_TABLES:
        c.execute(
            "DELETE FROM sqlite_sequence WHERE name=?", (t,)
        )

    c.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()

    print("[ResetDB] Done. Corpus tables are empty.")
    print()
    print("─" * 60)
    print("NEXT STEPS — run these in order:")
    print()
    print("  1. Scrape Philippine government + factcheck sources first:")
    print("       python corpus/scraper.py --group ph_gov --limit 50")
    print("       python corpus/scraper.py --group ph_factcheck --limit 100")
    print("       python corpus/scraper.py --group ph_news --limit 60")
    print()
    print("  2. Scrape international sources:")
    print("       python corpus/scraper.py --group international --limit 30")
    print("       python corpus/scraper.py --group factcheck --limit 100")
    print()
    print("  3. Rebuild the FAISS index:")
    print("       python retrieval/build_index.py --rebuild")
    print()
    print("  4. Restart your server:")
    print("       uvicorn main:app --reload")
    print("─" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wipe SocialProof corpus tables")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Skip the interactive confirmation prompt"
    )
    args = parser.parse_args()
    reset_corpus(confirm=args.confirm)
