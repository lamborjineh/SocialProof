"""
diagnose.py — SocialProof Corpus Viewer & Retrieval Tester
===========================================================
Run from your project root (same folder as main.py):

    python diagnose.py               # full report + default test claims
    python diagnose.py --view        # corpus health report only
    python diagnose.py --test        # retrieval tests only
    python diagnose.py --claim "your claim here"   # test one specific claim
    python diagnose.py --domain who.int            # inspect one domain's sentences
    python diagnose.py --pipeline factcheck        # inspect one pipeline's sentences

What this script does
─────────────────────
SECTION 1 — CORPUS HEALTH
  • Total sentences + breakdown by pipeline and domain
  • Per-pipeline coverage vs. thesis targets
  • Per-domain sentence count + reputation score
  • Index freshness check (are your FAISS files stale?)
  • Top 5 missing / underweight sources

SECTION 2 — RETRIEVAL TESTS
  • Tests a set of representative claims against the live index
  • Shows retrieved evidence: score, domain, pipeline, snippet
  • Flags if a claim retrieves nothing (coverage gap)
  • Tests across all three pipelines (news, stats, factcheck)

SECTION 3 — SINGLE CLAIM MODE
  • Deep-dive on one claim you provide with --claim

SECTION 4 — DOMAIN INSPECTOR
  • Shows sample sentences from a specific domain with --domain
"""

import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

# ── colour helpers (no external deps) ────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(s):    return f"{GREEN}✓ {s}{RESET}"
def warn(s):  return f"{YELLOW}⚠ {s}{RESET}"
def err(s):   return f"{RED}✗ {s}{RESET}"
def hdr(s):   return f"\n{BOLD}{CYAN}{'─'*60}\n  {s}\n{'─'*60}{RESET}"
def bar(n, total, width=30):
    filled = int(width * n / max(total, 1))
    colour = GREEN if n >= total * 0.8 else (YELLOW if n >= total * 0.4 else RED)
    return f"{colour}{'█' * filled}{'░' * (width - filled)}{RESET} {n:>5}/{total}"


# ── SECTION 1: Corpus Health ──────────────────────────────────────────────────

def section_corpus_health():
    print(hdr("SECTION 1 — CORPUS HEALTH"))

    try:
        from corpus.db import get_connection
        conn = get_connection()
        c    = conn.cursor()
    except Exception as e:
        print(err(f"Cannot open corpus.db: {e}"))
        return

    # ── Total counts ──────────────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM sentences")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM articles")
    total_articles = c.fetchone()[0]
    print(f"\n  Total sentences : {BOLD}{total:,}{RESET}")
    print(f"  Total articles  : {BOLD}{total_articles:,}{RESET}")

    # ── By pipeline ───────────────────────────────────────────────────────────
    TARGETS = {"news": 6000, "stats": 4500, "factcheck": 1500}
    c.execute("SELECT pipeline_type, COUNT(*) FROM sentences GROUP BY pipeline_type")
    by_pipeline = dict(c.fetchall())

    print(f"\n  {'Pipeline':<12} {'Count':>6}   {'vs Target':>10}   Progress")
    print(f"  {'─'*12} {'─'*6}   {'─'*10}   {'─'*35}")
    for pipeline, target in TARGETS.items():
        count = by_pipeline.get(pipeline, 0)
        status = ok("") if count >= target else (warn("") if count >= target * 0.5 else err(""))
        print(f"  {pipeline:<12} {count:>6}   target {target:>5}   {bar(count, target)}")
        if count < target * 0.5:
            deficit = target - count
            print(f"  {' '*12}  → {RED}Need ~{deficit:,} more sentences. "
                  f"Run: python corpus/scraper.py --group {'ph_gov intl_org' if pipeline=='stats' else pipeline} --limit 500{RESET}")

    # ── By domain ─────────────────────────────────────────────────────────────
    c.execute("""
        SELECT source_domain, pipeline_type, COUNT(*)
        FROM sentences
        GROUP BY source_domain, pipeline_type
        ORDER BY COUNT(*) DESC
    """)
    rows = c.fetchall()

    try:
        from corpus.source_registry import REPUTATION
    except Exception:
        REPUTATION = {}

    print(f"\n  {'Domain':<35} {'Pipeline':<10} {'Sentences':>9}  {'Rep':>5}  Health")
    print(f"  {'─'*35} {'─'*10} {'─'*9}  {'─'*5}  {'─'*20}")

    domain_totals = defaultdict(int)
    for domain, pipeline, count in rows:
        domain_totals[domain] += count

    MIN_GOOD = {"stats": 200, "factcheck": 300, "news": 200}
    problems = []
    for domain, pipeline, count in rows:
        rep   = REPUTATION.get(domain, 0.0)
        floor = MIN_GOOD.get(pipeline, 200)
        if count >= floor:
            health = ok("Good")
        elif count >= floor // 2:
            health = warn(f"Low ({floor - count} more needed)")
            problems.append((domain, pipeline, count, floor))
        else:
            health = err(f"Critical ({floor - count} more needed)")
            problems.append((domain, pipeline, count, floor))
        print(f"  {domain:<35} {pipeline:<10} {count:>9}  {rep:>5.2f}  {health}")

    # ── Sources in registry but with 0 sentences ─────────────────────────────
    from corpus.source_registry import SOURCES
    all_db_domains = {r[0] for r in rows}
    zero_domains = [(key, cfg) for key, cfg in SOURCES.items()
                    if cfg["domain"] not in all_db_domains]
    if zero_domains:
        print(f"\n  {YELLOW}Sources registered but not yet scraped ({len(zero_domains)}):{RESET}")
        for key, cfg in sorted(zero_domains, key=lambda x: -x[1]["reputation"]):
            print(f"    {cfg['domain']:<35} pipeline={cfg['pipeline']} rep={cfg['reputation']}")

    # ── Index freshness ───────────────────────────────────────────────────────
    print(f"\n  {'Index Files':<40} {'Size':>8}  {'Last Built':>20}  Status")
    print(f"  {'─'*40} {'─'*8}  {'─'*20}  {'─'*15}")

    data_dir = Path(__file__).parent / "data"
    try:
        c.execute("SELECT MAX(created_at) FROM sentences")
        newest_row = c.fetchone()[0]
        newest_db  = datetime.fromisoformat(str(newest_row)[:19]) if newest_row else None
    except Exception:
        newest_db = None

    for pipeline in ["news", "stats", "factcheck", "all"]:
        suffix    = f"_{pipeline}" if pipeline != "all" else ""
        faiss_f   = data_dir / f"embeddings{suffix}.faiss"
        meta_f    = data_dir / f"sentences_meta{suffix}.json"
        index_f   = faiss_f if faiss_f.exists() else meta_f

        if not index_f.exists():
            print(f"  {str(index_f.name):<40} {'—':>8}  {'not found':>20}  {err('Missing')}")
            continue

        size_mb  = index_f.stat().st_size / 1_048_576
        mtime    = datetime.fromtimestamp(index_f.stat().st_mtime)
        age_h    = (datetime.now() - mtime).total_seconds() / 3600

        if newest_db and newest_db > mtime:
            stale_h = (newest_db - mtime).total_seconds() / 3600
            status  = warn(f"Stale ~{stale_h:.0f}h") if stale_h > 24 else ok("Fresh")
        else:
            status = ok("Fresh")

        print(f"  {str(index_f.name):<40} {size_mb:>7.1f}M  {mtime.strftime('%Y-%m-%d %H:%M'):>20}  {status}")

    conn.close()

    # ── Scrape recommendations ────────────────────────────────────────────────
    print(f"\n  {BOLD}Recommended next scrape commands:{RESET}")
    low_pipelines = [p for p, t in TARGETS.items() if by_pipeline.get(p, 0) < t * 0.8]
    if not low_pipelines:
        print(f"  {ok('All pipelines are above 80% of target. Consider scraping new sources.')}")
    else:
        cmds = {
            "factcheck": "python corpus/scraper.py --group factcheck --limit 500",
            "stats":     "python corpus/scraper.py --group ph_gov --limit 400\n  "
                         "python corpus/scraper.py --group intl_org --limit 300\n  "
                         "python corpus/scraper.py --group science --limit 300",
            "news":      "python corpus/scraper.py --group ph_news --limit 200",
        }
        for p in low_pipelines:
            print(f"  {YELLOW}{cmds.get(p, '')}{RESET}")
        print(f"\n  After scraping, always rebuild the index:")
        print(f"  {CYAN}python retrieval/build_index.py --rebuild{RESET}")


# ── SECTION 2: Retrieval Tests ────────────────────────────────────────────────

# Representative test claims — one per pipeline type, mix of PH and global
DEFAULT_CLAIMS = [
    # Stats / numeric claims
    ("The Philippine unemployment rate increased in 2023.",               "stats"),
    ("WHO recommends vaccination coverage of at least 95 percent.",       "stats"),
    ("The Bangko Sentral ng Pilipinas raised interest rates.",            "stats"),
    ("The Philippine inflation rate exceeded 8 percent.",                 "stats"),
    # Factcheck claims (typical misinformation patterns)
    ("A viral video shows soldiers committing war crimes.",               "factcheck"),
    ("The COVID-19 vaccine contains microchips.",                         "factcheck"),
    ("Climate change is not caused by human activity.",                   "factcheck"),
    # News / event claims
    ("Typhoon caused widespread flooding in the Philippines.",            "news"),
    ("The Supreme Court ruled on election protest case.",                 "news"),
    ("The United Nations condemned the attack on civilians.",             "news"),
]

def _load_retriever():
    try:
        from retrieval.retriever import Retriever
        print(f"  {YELLOW}Loading embedding model (may take 10-30s on first run)...{RESET}")
        return Retriever()
    except FileNotFoundError as e:
        print(err(f"No index found: {e}"))
        print(f"  Run: python retrieval/build_index.py")
        return None
    except Exception as e:
        print(err(f"Retriever failed to load: {e}"))
        return None

def _print_result(i, res):
    score   = res["similarity"]
    domain  = res["domain"]
    pipe    = res.get("pipeline_type", "?")
    text    = res["text"]
    url     = res["url"]
    date    = res.get("date_published", "")

    score_col = GREEN if score >= 0.65 else (YELLOW if score >= 0.50 else RED)
    print(f"    [{i}] {score_col}score={score:.4f}{RESET}  {CYAN}{domain}{RESET}  [{pipe}]")
    print(f"         {text[:110]}{'...' if len(text)>110 else ''}")
    print(f"         {url[:90]}" + (f"  ({date[:10]})" if date else ""))

def section_retrieval_tests(claims=None, retriever=None):
    print(hdr("SECTION 2 — RETRIEVAL TESTS"))

    if retriever is None:
        retriever = _load_retriever()
    if retriever is None:
        return

    test_claims = claims or DEFAULT_CLAIMS
    gaps = []

    for claim, expected_pipeline in test_claims:
        print(f"\n  {BOLD}Claim:{RESET} {claim}")
        print(f"  {BOLD}Expected pipeline:{RESET} {expected_pipeline}")

        results = retriever.search(claim, k=5)

        if not results:
            print(f"  {err('NO RESULTS — corpus gap. This topic has no coverage.')}")
            gaps.append(claim)
            continue

        # Check if expected pipeline is represented
        pipelines_found = Counter(r.get("pipeline_type", "?") for r in results)
        if expected_pipeline not in pipelines_found:
            print(f"  {warn(f'Expected pipeline [{expected_pipeline}] not in top results. Found: {dict(pipelines_found)}')}")
        else:
            print(f"  {ok(f'Expected pipeline [{expected_pipeline}] present in results.')}")

        for i, res in enumerate(results, 1):
            _print_result(i, res)

    if gaps:
        print(f"\n  {RED}{BOLD}Coverage gaps — {len(gaps)} claim(s) returned no results:{RESET}")
        for g in gaps:
            print(f"    • {g}")
        print(f"\n  {YELLOW}Fix: scrape more sources for these topics, then rebuild the index.{RESET}")
    else:
        print(f"\n  {ok('All test claims returned at least one result.')}")

    return retriever  # return so caller can reuse


# ── SECTION 3: Single Claim Deep Dive ────────────────────────────────────────

def section_single_claim(claim: str, retriever=None):
    print(hdr(f"SECTION 3 — CLAIM DEEP DIVE"))
    print(f"  Claim: {BOLD}{claim}{RESET}\n")

    if retriever is None:
        retriever = _load_retriever()
    if retriever is None:
        return

    results = retriever.search(claim, k=10)

    if not results:
        print(err("No evidence found for this claim."))
        print("This is a corpus gap — scrape more sources on this topic.")
        return

    print(f"  Found {len(results)} evidence sentences:\n")
    by_pipeline = defaultdict(list)
    for r in results:
        by_pipeline[r.get("pipeline_type","?")].append(r)

    for pipeline in ["stats", "factcheck", "news"]:
        prs = by_pipeline.get(pipeline, [])
        if not prs:
            print(f"  {YELLOW}[{pipeline}] No results{RESET}")
            continue
        print(f"  {BOLD}[{pipeline}]{RESET}")
        for i, res in enumerate(prs, 1):
            _print_result(i, res)
        print()

    # score distribution
    scores = [r["similarity"] for r in results]
    print(f"\n  Score range: {min(scores):.4f} – {max(scores):.4f}  "
          f"(avg {sum(scores)/len(scores):.4f})")
    strong = sum(1 for s in scores if s >= 0.65)
    print(f"  Strong evidence (≥0.65): {strong}/{len(scores)}")
    if strong == 0:
        print(warn("No strong evidence. Results are weak matches — the claim may lack corpus support."))


# ── SECTION 4: Domain Inspector ───────────────────────────────────────────────

def section_domain_inspector(domain: str = None, pipeline: str = None):
    title = f"SECTION 4 — DOMAIN INSPECTOR"
    if domain:
        title += f": {domain}"
    if pipeline:
        title += f" [{pipeline}]"
    print(hdr(title))

    try:
        from corpus.db import get_connection
        conn = get_connection()
        c    = conn.cursor()
    except Exception as e:
        print(err(f"Cannot open corpus.db: {e}"))
        return

    if domain:
        c.execute("""
            SELECT s.sentence_text, s.pipeline_type, s.numeric_density,
                   s.url, a.date_published
            FROM sentences s
            LEFT JOIN articles a ON s.article_id = a.id
            WHERE s.source_domain = ?
            ORDER BY s.numeric_density DESC
            LIMIT 30
        """, (domain,))
    elif pipeline:
        c.execute("""
            SELECT s.sentence_text, s.pipeline_type, s.numeric_density,
                   s.url, a.date_published
            FROM sentences s
            LEFT JOIN articles a ON s.article_id = a.id
            WHERE s.pipeline_type = ?
            ORDER BY s.numeric_density DESC
            LIMIT 30
        """, (pipeline,))
    else:
        print(err("Provide --domain <domain> or --pipeline <pipeline>"))
        conn.close()
        return

    rows = c.fetchall()
    conn.close()

    if not rows:
        print(err(f"No sentences found for {'domain: ' + domain if domain else 'pipeline: ' + pipeline}"))
        print("Run the scraper first for this source.")
        return

    print(f"\n  Showing top {len(rows)} sentences (sorted by numeric density):\n")
    for i, (text, pipe, nd, url, date) in enumerate(rows, 1):
        nd_col = GREEN if nd > 0.3 else (YELLOW if nd > 0.1 else RESET)
        print(f"  [{i:>2}] {nd_col}density={nd:.3f}{RESET}  [{pipe}]  {(date or '')[:10]}")
        print(f"        {text[:120]}{'...' if len(text)>120 else ''}")
        print(f"        {url[:80]}")
        print()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SocialProof corpus viewer and retrieval tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python diagnose.py                          # full report
  python diagnose.py --view                   # corpus health only
  python diagnose.py --test                   # retrieval tests only
  python diagnose.py --claim "inflation rose" # test one claim
  python diagnose.py --domain who.int         # inspect WHO sentences
  python diagnose.py --pipeline factcheck     # inspect factcheck pipeline
        """
    )
    parser.add_argument("--view",     action="store_true", help="Corpus health report only")
    parser.add_argument("--test",     action="store_true", help="Retrieval tests only")
    parser.add_argument("--claim",    type=str,            help="Test a specific claim")
    parser.add_argument("--domain",   type=str,            help="Inspect sentences from a domain")
    parser.add_argument("--pipeline", type=str,            help="Inspect sentences from a pipeline (news/stats/factcheck)")
    args = parser.parse_args()

    print(f"\n{BOLD}{'='*60}")
    print(f"  SocialProof — Corpus & Retrieval Diagnostics")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}{RESET}")

    retriever = None

    if args.claim:
        section_corpus_health()
        retriever = section_single_claim(args.claim)
    elif args.domain or args.pipeline:
        section_domain_inspector(args.domain, args.pipeline)
    elif args.view:
        section_corpus_health()
    elif args.test:
        retriever = section_retrieval_tests()
    else:
        # Full report
        section_corpus_health()
        retriever = section_retrieval_tests(retriever=retriever)

    print(f"\n{BOLD}{'='*60}{RESET}\n")
