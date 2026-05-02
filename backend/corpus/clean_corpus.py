"""
corpus/clean_corpus.py
One-time corpus cleanup script — removes contaminated data from corpus.db.

Problems fixed:
  1. politifact.com sentences (10,221 rows) — the scraper fetched PolitiFact
     article pages and stored the CLAIM TEXT as evidence sentences.
     These are political statements being fact-checked, NOT fact-check verdicts.
     They will confuse NLI (claim vs claim, not evidence vs claim).
     Note: LIAR evaluation data is correctly stored under 'liar-dataset.bench'
     by load_liar.py — that is separate and not touched here.

  2. rappler.com sentences — general RSS scrape included lifestyle articles,
     advice columns, and boilerplate. Only factually dense sentences are kept.

  3. factcheck.org fragment sentences — mid-sentence cuts without terminal
     punctuation provide no signal to NLI.

  4. General fragment / boilerplate filter applied to all other domains.

Usage:
    python corpus/clean_corpus.py           # dry run — shows what would change
    python corpus/clean_corpus.py --apply   # apply deletions

After running with --apply, always rebuild the index:
    python retrieval/build_index.py --rebuild
"""

import re
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from corpus.db import get_connection

# ── Quality thresholds ────────────────────────────────────────────────────────
MIN_LEN_DEFAULT    = 40   # chars — minimum for any sentence
MIN_LEN_RAPPLER    = 60   # stricter: news pipeline needs more context
MIN_LEN_FACTCHECK  = 50   # factcheck sentences must be complete thoughts
MIN_WORDS          = 8    # word count minimum

# News domains that are general news publications — narrative-heavy by nature.
# These get the same factual marker requirement as rappler: only sentences that
# assert something concrete (statistic, attribution, official body reference)
# survive. Boilerplate and narrative sentences are dropped.
_NEWS_PIPELINE_DOMAINS = {
    "bbc.com", "bbc.co.uk",
    "theguardian.com", "guardian.com",
    "npr.org",
    "inquirer.net",
    "philstar.com",
    "abs-cbn.com",
    "gmanetwork.com", "gmanews.tv",
    "nytimes.com",
    "apnews.com",
    "reuters.com",
    "theatlantic.com",
    "time.com",
    "vox.com",
}

# Factual markers — at least one required for news pipeline sentences
_FACTUAL_PATTERNS = [
    r"\b\d+\s*(%|percent|million|billion|thousand|trillion)\b",
    r"\baccording to\b",
    r"\b(said|confirmed|announced|reported|stated|declared)\b",
    r"\b(study|research|report|survey|data|statistics?)\b",
    r"\b(WHO|CDC|DOH|PSA|BSP|NEDA|UN|IMF|World Bank|IPCC|FDA)\b",
    r"\b(law|bill|policy|ordinance|executive order|resolution)\b",
    r"\b(president|secretary|senator|mayor|governor|minister|official)\b",
    r"\bPhilippines?\b|\bManila\b|\bFilipino\b",
]
_FACTUAL_RE = re.compile("|".join(_FACTUAL_PATTERNS), re.IGNORECASE)

# Boilerplate phrases to reject
_BOILERPLATE = [
    "subscribe to", "sign up for", "read more:", "also read:",
    "follow us on", "advertisement", "click here to", "all rights reserved",
    "terms of service", "privacy policy", "this is ai generated",
    "for context, always refer", "you may also like", "load more",
    "share this article", "in photos:", "watch:", "look:", "just in:",
    "developing story", "rappler's people section", "advice column",
    "note to editors", "for media inquiries", "for immediate release",
    "download pdf", "download full report", "media contact",
    "press release",
]


def _is_boilerplate(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _BOILERPLATE)


def _is_fragment(text: str, min_len: int = MIN_LEN_DEFAULT) -> bool:
    """True if the sentence is too short, too few words, or lacks terminal punctuation."""
    s = text.strip()
    if len(s) < min_len:
        return True
    if len(s.split()) < MIN_WORDS:
        return True
    # Must end with terminal punctuation — questions excluded (not useful as evidence)
    if s[-1] not in ".!\"'":
        return True
    return False


def _rappler_passes(text: str) -> bool:
    """Only keep Rappler sentences that are factually informative."""
    if _is_boilerplate(text):
        return False
    if _is_fragment(text, min_len=MIN_LEN_RAPPLER):
        return False
    return bool(_FACTUAL_RE.search(text))


def _news_pipeline_passes(text: str) -> bool:
    """
    Quality gate for all general news domains (BBC, Guardian, NPR, etc.).

    General news articles are narrative-heavy — story colour, scene-setting,
    quotes of emotion, and transition sentences all pass the basic length check
    but provide no useful evidence to the NLI model.  The same factual-marker
    requirement used for Rappler is applied here.  A sentence must contain at
    least one concrete factual signal (statistic, attribution phrase, official
    body name, legislation reference) to be kept.
    """
    if _is_boilerplate(text):
        return False
    if _is_fragment(text, min_len=MIN_LEN_RAPPLER):   # same 60-char min as rappler
        return False
    return bool(_FACTUAL_RE.search(text))


def _factcheck_passes(text: str) -> bool:
    """Factcheck sentences must be complete and non-boilerplate."""
    if _is_boilerplate(text):
        return False
    return not _is_fragment(text, min_len=MIN_LEN_FACTCHECK)


def _default_passes(text: str) -> bool:
    """General quality gate for all other domains."""
    if _is_boilerplate(text):
        return False
    return not _is_fragment(text, min_len=MIN_LEN_DEFAULT)


# ── Main cleanup ──────────────────────────────────────────────────────────────

def run_cleanup(apply: bool = False) -> None:
    conn = get_connection()
    c    = conn.cursor()

    label = "APPLYING" if apply else "DRY RUN"
    print("=" * 65)
    print(f"SocialProof Corpus Cleanup — {label}")
    print("=" * 65)

    c.execute("SELECT COUNT(*) FROM sentences")
    total_before = c.fetchone()[0]
    print(f"\nTotal sentences before: {total_before:,}")

    removed_total = 0

    # ── 1. politifact.com — remove entirely ──────────────────────────────────
    c.execute("SELECT COUNT(*) FROM sentences WHERE source_domain='politifact.com'")
    n_politi = c.fetchone()[0]
    print(f"\n[REMOVE] politifact.com: {n_politi:,} sentences")
    print("  Reason: scraper stored claim headlines as evidence (not fact-check verdicts)")
    print("  LIAR evaluation data is kept — it lives under 'liar-dataset.bench'")
    if apply and n_politi > 0:
        c.execute("DELETE FROM sentences WHERE source_domain='politifact.com'")
        c.execute(
            "DELETE FROM articles WHERE source_domain='politifact.com' AND url LIKE 'liar://%'"
        )
        removed_total += n_politi
        print(f"  ✓ Removed {n_politi:,} rows")

    # ── 2. rappler.com — filter to factual sentences only ────────────────────
    c.execute("SELECT id, sentence_text FROM sentences WHERE source_domain='rappler.com'")
    rappler_rows = c.fetchall()
    rappler_drop_ids = [r[0] for r in rappler_rows if not _rappler_passes(r[1])]
    rappler_keep_n   = len(rappler_rows) - len(rappler_drop_ids)
    print(f"\n[FILTER] rappler.com: {len(rappler_rows):,} sentences")
    print(f"  Keep: {rappler_keep_n:,}  |  Drop: {len(rappler_drop_ids):,}")
    # Print 3 drop examples so the user can verify
    drop_examples = [r[1] for r in rappler_rows if r[0] in set(rappler_drop_ids[:5])]
    for ex in drop_examples[:3]:
        print(f"  DROP: {ex[:95]!r}")
    if apply and rappler_drop_ids:
        _bulk_delete(c, rappler_drop_ids)
        removed_total += len(rappler_drop_ids)
        print(f"  ✓ Removed {len(rappler_drop_ids):,} low-quality sentences")

    # ── 3. factcheck.org — remove fragments ──────────────────────────────────
    c.execute("SELECT id, sentence_text FROM sentences WHERE source_domain='factcheck.org'")
    fc_rows    = c.fetchall()
    fc_drop_ids = [r[0] for r in fc_rows if not _factcheck_passes(r[1])]
    print(f"\n[FILTER] factcheck.org: {len(fc_rows):,} sentences")
    print(f"  Drop fragments: {len(fc_drop_ids):,}")
    if apply and fc_drop_ids:
        _bulk_delete(c, fc_drop_ids)
        removed_total += len(fc_drop_ids)
        print(f"  ✓ Removed {len(fc_drop_ids):,} fragments")

    # ── 3b. nature.com — remove entirely (job ads, paywall notices, nav text) ─
    # The scraper hit nature.com's paywalled article pages and collected only
    # institutional hiring posts, "you have full access via your institution"
    # notices, and journal navigation text. Zero usable evidence sentences.
    # Source config has been fixed to use gnews instead — re-scrape after purge.
    c.execute("SELECT COUNT(*) FROM sentences WHERE source_domain='nature.com'")
    n_nature = c.fetchone()[0]
    print(f"\n[REMOVE] nature.com: {n_nature:,} sentences")
    print("  Reason: scraper hit paywalled pages — only job ads and nav text in DB")
    print("  Fix: source_registry.py updated to use gnews; re-scrape after rebuild")
    if apply and n_nature > 0:
        c.execute("DELETE FROM sentences WHERE source_domain='nature.com'")
        c.execute("DELETE FROM articles WHERE source_domain='nature.com'")
        removed_total += n_nature
        print(f"  ✓ Removed {n_nature:,} rows (articles table also cleared)")

    # ── 3c. cdc.gov — remove entirely (product recall notices, pet food alerts) ─
    # The old CDC RSS feed (id 316422) was an animal food recall feed.
    # Scraped content: dog milk replacer recalls, pet food vitamin D warnings.
    # Not a single sentence about vaccines, disease prevention, or public health.
    # Source config fixed to target /vaccines/, /flu/, /covid-19/ — re-scrape.
    c.execute("SELECT COUNT(*) FROM sentences WHERE source_domain='cdc.gov'")
    n_cdc = c.fetchone()[0]
    print(f"\n[REMOVE] cdc.gov: {n_cdc:,} sentences")
    print("  Reason: old RSS feed was a product recall/pet food alert feed")
    print("  Fix: source_registry.py updated to target health/vaccine pages")
    if apply and n_cdc > 0:
        c.execute("DELETE FROM sentences WHERE source_domain='cdc.gov'")
        c.execute("DELETE FROM articles WHERE source_domain='cdc.gov'")
        removed_total += n_cdc
        print(f"  ✓ Removed {n_cdc:,} rows (articles table also cleared)")

    # ── 4. General news domains — apply factual-marker quality gate ───────────
    # These are general news publications whose articles are narrative-heavy.
    # A sentence from BBC or Guardian about a person's emotional reaction or a
    # scene description will pass the basic length/boilerplate check but is
    # useless as NLI evidence.  Apply the same factual-marker filter used for
    # Rappler to all news pipeline domains.
    excluded_domains = (
        "'politifact.com','rappler.com','factcheck.org','nature.com','cdc.gov'"
    )
    news_domain_list = ",".join(f"'{d}'" for d in _NEWS_PIPELINE_DOMAINS)
    c.execute(
        f"SELECT id, sentence_text FROM sentences "
        f"WHERE source_domain IN ({news_domain_list})"
    )
    news_rows     = c.fetchall()
    news_drop_ids = [r[0] for r in news_rows if not _news_pipeline_passes(r[1])]
    news_keep_n   = len(news_rows) - len(news_drop_ids)
    print(f"\n[FILTER] General news domains (BBC, Guardian, NPR, etc.): {len(news_rows):,} sentences")
    print(f"  Keep (factual sentences): {news_keep_n:,}  |  Drop (narrative/boilerplate): {len(news_drop_ids):,}")
    drop_news_examples = [r[1] for r in news_rows if r[0] in set(news_drop_ids[:5])]
    for ex in drop_news_examples[:3]:
        print(f"  DROP: {ex[:95]!r}")
    if apply and news_drop_ids:
        _bulk_delete(c, news_drop_ids)
        removed_total += len(news_drop_ids)
        print(f"  ✓ Removed {len(news_drop_ids):,} narrative/boilerplate sentences")

    # ── 5. All remaining domains — general quality gate ───────────────────────
    c.execute(
        "SELECT id, sentence_text FROM sentences "
        f"WHERE source_domain NOT IN ({excluded_domains}) "
        f"AND source_domain NOT IN ({news_domain_list})"
    )
    other_rows    = c.fetchall()
    other_drop_ids = [r[0] for r in other_rows if not _default_passes(r[1])]
    print(f"\n[FILTER] All remaining domains: {len(other_rows):,} sentences")
    print(f"  Drop fragments/boilerplate: {len(other_drop_ids):,}")
    if apply and other_drop_ids:
        _bulk_delete(c, other_drop_ids)
        removed_total += len(other_drop_ids)
        print(f"  ✓ Removed {len(other_drop_ids):,} fragments/boilerplate")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    if apply:
        conn.commit()
        c.execute("SELECT COUNT(*) FROM sentences")
        total_after = c.fetchone()[0]
        print("CLEANUP COMPLETE")
        print(f"  Before : {total_before:,} sentences")
        print(f"  Removed: {removed_total:,}")
        print(f"  After  : {total_after:,} sentences")
        print(f"\nDomain distribution after cleanup:")
        c.execute(
            "SELECT source_domain, COUNT(*) FROM sentences "
            "GROUP BY source_domain ORDER BY COUNT(*) DESC"
        )
        for row in c.fetchall():
            print(f"  {row[0]:30s} {row[1]:>6,}")
        print(f"\nNEXT STEP — rebuild the FAISS index:")
        print(f"  python retrieval/build_index.py --rebuild")
    else:
        print("DRY RUN — no changes made")
        print(f"  Would remove : {removed_total:,} sentences")
        print(f"  Would keep   : {total_before - removed_total:,} sentences")
        politi_n   = n_politi
        rappler_n  = len(rappler_drop_ids)
        fc_n       = len(fc_drop_ids)
        nature_n   = n_nature
        cdc_n      = n_cdc
        news_n     = len(news_drop_ids)
        other_n    = len(other_drop_ids)
        print(f"\n  Breakdown:")
        print(f"    politifact.com      {politi_n:>6,}  (scraped claims)")
        print(f"    rappler.com         {rappler_n:>6,}  (lifestyle/boilerplate)")
        print(f"    factcheck.org       {fc_n:>6,}  (fragments)")
        print(f"    nature.com          {nature_n:>6,}  (job ads / paywall noise)")
        print(f"    cdc.gov             {cdc_n:>6,}  (pet food recall feed)")
        print(f"    news domains        {news_n:>6,}  (narrative/non-factual sentences)")
        print(f"    other domains       {other_n:>6,}  (fragments/boilerplate)")
        print(f"\nRe-run with --apply to apply changes.")

    conn.close()


def _bulk_delete(cursor, ids: list) -> None:
    """Delete rows in batches of 500 to avoid SQLite parameter limits."""
    BATCH = 500
    for i in range(0, len(ids), BATCH):
        batch = ids[i : i + BATCH]
        placeholders = ",".join("?" * len(batch))
        cursor.execute(f"DELETE FROM sentences WHERE id IN ({placeholders})", batch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean SocialProof corpus.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions (default: dry run only)",
    )
    args = parser.parse_args()
    run_cleanup(apply=args.apply)
