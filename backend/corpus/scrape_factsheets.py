"""
corpus/scrape_factsheets.py — Targeted Evidence-Dense Source Scraper (v1.0)

Scrapes pages where NEARLY EVERY SENTENCE is a factual reference statement —
unlike news articles where factual sentences are mixed with narrative.

Target source categories:
  1. WHO fact sheets and Q&A pages (health misinformation ground truth)
  2. CDC health topic pages
  3. tsek.ph verdict paragraphs (PH-specific fact-checks)
  4. Vera Files fact-check verdicts
  5. AFP Fact Check PH verdicts

WHY these sources and not broad news scraping:
  These pages are written as factual reference material. Nearly every sentence
  is a claim-sized factual assertion that will pass the quality gate and
  produce high-quality FAISS matches. A single WHO fact sheet page gives
  10–30 usable evidence sentences vs ~2-3 from a typical news article.

USAGE:
  python -m corpus.scrape_factsheets           # scrape all sources
  python -m corpus.scrape_factsheets --who     # WHO only
  python -m corpus.scrape_factsheets --ph      # PH fact-checkers only
  python -m corpus.scrape_factsheets --dry-run # count without inserting

After running, rebuild FAISS:
  python scripts/rebuild_faiss.py

IMPORTANT: This scraper must be run AFTER running purge_narrative_sentences()
to clean pre-3.4 corpus noise. Both steps together dramatically improve
retrieval quality without requiring a corpus rebuild from scratch.
"""

import argparse
import time
import re
import urllib.request
import urllib.error
from typing import List, Tuple, Optional
from pathlib import Path

from corpus.db import insert_article, insert_pipeline_sentences, article_exists, get_corpus_stats

# ── HTTP config ────────────────────────────────────────────────────────────────
TIMEOUT    = 15   # seconds per request
USER_AGENT = (
    "Mozilla/5.0 (compatible; SocialProof-Corpus/1.0; "
    "Academic research — thesis project)"
)
DELAY_BETWEEN_REQUESTS = 1.5   # seconds between requests — be a polite scraper

# ── Source lists ──────────────────────────────────────────────────────────────
# WHO fact sheets — these are the highest-value single pages for health claims.
# Each page covers one topic and every paragraph is factual.
WHO_FACT_SHEETS = [
    "https://www.who.int/news-room/fact-sheets/detail/vaccines-and-immunization-what-is-vaccination",
    "https://www.who.int/news-room/fact-sheets/detail/autism-spectrum-disorders",
    "https://www.who.int/news-room/fact-sheets/detail/cancer",
    "https://www.who.int/news-room/fact-sheets/detail/diabetes",
    "https://www.who.int/news-room/fact-sheets/detail/cardiovascular-diseases-(cvds)",
    "https://www.who.int/news-room/fact-sheets/detail/hiv-aids",
    "https://www.who.int/news-room/fact-sheets/detail/tuberculosis",
    "https://www.who.int/news-room/fact-sheets/detail/dengue-and-severe-dengue",
    "https://www.who.int/news-room/fact-sheets/detail/malaria",
    "https://www.who.int/news-room/fact-sheets/detail/mental-health-strengthening-our-response",
    "https://www.who.int/news-room/fact-sheets/detail/depression",
    "https://www.who.int/news-room/fact-sheets/detail/obesity-and-overweight",
    "https://www.who.int/news-room/fact-sheets/detail/tobacco",
    "https://www.who.int/news-room/fact-sheets/detail/alcohol",
    "https://www.who.int/news-room/fact-sheets/detail/antibiotic-resistance",
    "https://www.who.int/news-room/fact-sheets/detail/coronaviruses",
    "https://www.who.int/news-room/fact-sheets/detail/influenza-(seasonal)",
    "https://www.who.int/news-room/fact-sheets/detail/food-safety",
    "https://www.who.int/news-room/fact-sheets/detail/air-pollution",
    "https://www.who.int/news-room/fact-sheets/detail/climate-change-and-health",
]

# WHO Q&A and myth-busting pages
WHO_QA_PAGES = [
    "https://www.who.int/emergencies/diseases/novel-coronavirus-2019/advice-for-public/myth-busters",
    "https://www.who.int/news-room/questions-and-answers/item/vaccines-and-immunization",
    "https://www.who.int/news-room/questions-and-answers/item/herd-immunity-lockdowns-and-covid-19",
]

# CDC health topic pages — similar format to WHO, English-primary
CDC_PAGES = [
    "https://www.cdc.gov/vaccines/vac-gen/howvacwork.htm",
    "https://www.cdc.gov/vaccinesafety/concerns/index.html",
    "https://www.cdc.gov/cancer/risk_factors.htm",
    "https://www.cdc.gov/diabetes/basics/diabetes.html",
    "https://www.cdc.gov/mentalhealth/learn/index.htm",
    "https://www.cdc.gov/tobacco/basic_information/index.htm",
    "https://www.cdc.gov/alcohol/fact-sheets/alcohol-use.htm",
    "https://www.cdc.gov/flu/about/keyfacts.htm",
]

# Philippine fact-checkers — verdict paragraphs only (high precision)
PH_FACTCHECK_PAGES = [
    # tsek.ph — official PH fact-check site
    "https://tsek.ph/category/fact-check/",
    "https://tsek.ph/fact-check/",
    # Vera Files
    "https://verafiles.org/category/vera-files-fact-check",
    "https://verafiles.org/fact-check",
    # AFP Fact Check PH
    "https://factcheck.afp.com/afp-philippines",
]

# ── Text extraction helpers ───────────────────────────────────────────────────

_STRIP_TAGS    = re.compile(r"<(script|style|nav|footer|header|aside|form|iframe|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ALL_TAGS      = re.compile(r"<[^>]+>")
_WHITESPACE    = re.compile(r"\s{2,}")
_CONTENT_TAGS  = re.compile(r"<(p|h[1-6]|li|blockquote|td|th)[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


def _fetch_html(url: str) -> Optional[str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":      USER_AGENT,
            "Accept":          "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9,fil;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "text" not in ct and "html" not in ct:
                return None
            return resp.read(500_000).decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"    ✗ Fetch error: {e}")
        return None
    except Exception as e:
        print(f"    ✗ Unexpected error: {e}")
        return None


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return _ALL_TAGS.sub("", m.group(1)).strip() if m else ""


def _extract_sentences(html: str) -> List[str]:
    """Extract clean text sentences from HTML content."""
    cleaned = _STRIP_TAGS.sub(" ", html)
    parts   = _CONTENT_TAGS.findall(cleaned)

    if parts:
        paragraphs = [_ALL_TAGS.sub("", p[1]).strip() for p in parts if len(p[1]) > 30]
    else:
        body_m     = re.search(r"<body[^>]*>(.*?)</body>", cleaned, re.IGNORECASE | re.DOTALL)
        body       = body_m.group(1) if body_m else cleaned
        paragraphs = [_ALL_TAGS.sub(" ", body).strip()]

    # Decode HTML entities
    def _decode(t):
        return (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                  .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))

    sentences = []
    for para in paragraphs:
        para = _WHITESPACE.sub(" ", _decode(para)).strip()
        # Split on sentence boundaries
        for raw_sent in re.split(r"(?<=[.!])\s+(?=[A-Z])", para):
            s = raw_sent.strip()
            if len(s) >= 40:
                sentences.append(s)

    return sentences


# ── Scrape a single page ──────────────────────────────────────────────────────

def scrape_page(
    url:          str,
    domain:       str,
    pipeline:     str,
    dry_run:      bool = False,
) -> Tuple[int, int]:
    """
    Scrape a single URL, extract sentences, insert into corpus DB.

    Returns:
        (sentences_found, sentences_inserted)
    """
    if not dry_run and article_exists(url):
        print(f"    ↷ Already scraped: {url[:70]}")
        return 0, 0

    html = _fetch_html(url)
    if html is None:
        return 0, 0

    title     = _extract_title(html)
    sentences = _extract_sentences(html)

    if dry_run:
        print(f"    [dry-run] {url[:70]} → {len(sentences)} candidate sentences")
        return len(sentences), 0

    from corpus.db import insert_article, insert_pipeline_sentences
    article_id = insert_article(
        source_domain  = domain,
        url            = url,
        title          = title,
        content        = " ".join(sentences[:50]),  # store first 50 for reference
        date_published = "",
        word_count     = sum(len(s.split()) for s in sentences),
    )

    inserted = insert_pipeline_sentences(
        article_id    = article_id,
        source_domain = domain,
        url           = url,
        sentences     = sentences,
        pipeline      = pipeline,
    )

    return len(sentences), inserted


# ── Source group scrapers ─────────────────────────────────────────────────────

def scrape_who(dry_run: bool = False) -> int:
    """Scrape WHO fact sheets and Q&A pages."""
    print("\n📋 WHO Fact Sheets")
    total = 0
    all_urls = WHO_FACT_SHEETS + WHO_QA_PAGES
    for i, url in enumerate(all_urls, 1):
        print(f"  [{i}/{len(all_urls)}] {url[-50:]}")
        found, inserted = scrape_page(url, "who.int", "factcheck", dry_run)
        print(f"    → {found} found, {inserted} inserted")
        total += inserted
        time.sleep(DELAY_BETWEEN_REQUESTS)
    print(f"  WHO total inserted: {total}")
    return total


def scrape_cdc(dry_run: bool = False) -> int:
    """Scrape CDC health topic pages."""
    print("\n🏥 CDC Health Topics")
    total = 0
    for i, url in enumerate(CDC_PAGES, 1):
        print(f"  [{i}/{len(CDC_PAGES)}] {url[-50:]}")
        found, inserted = scrape_page(url, "cdc.gov", "factcheck", dry_run)
        print(f"    → {found} found, {inserted} inserted")
        total += inserted
        time.sleep(DELAY_BETWEEN_REQUESTS)
    print(f"  CDC total inserted: {total}")
    return total


def scrape_ph_factcheckers(dry_run: bool = False) -> int:
    """Scrape PH fact-checker listing pages and follow article links."""
    print("\n🇵🇭 PH Fact-Checkers (tsek.ph, Vera Files, AFP PH)")
    total    = 0
    base_domains = {
        "tsek.ph":           "tsek.ph",
        "verafiles.org":     "verafiles.org",
        "factcheck.afp.com": "factcheck.afp.com",
    }

    for listing_url in PH_FACTCHECK_PAGES:
        domain = next((d for d in base_domains if d in listing_url), "factcheck")
        print(f"\n  Listing: {listing_url[-60:]}")

        html = _fetch_html(listing_url)
        if html is None:
            continue

        # Extract article links from listing page
        link_re  = re.compile(
            rf'href="(https?://{re.escape(domain)}/[^"]+)"',
            re.IGNORECASE,
        )
        article_links = list(dict.fromkeys(link_re.findall(html)))[:20]
        print(f"    Found {len(article_links)} article links")

        for link in article_links:
            # Skip non-article pages
            if any(x in link for x in ["/category/", "/tag/", "/page/", "/author/", "#"]):
                continue
            print(f"    → {link[-60:]}")
            found, inserted = scrape_page(link, domain, "factcheck", dry_run)
            print(f"      {found} found, {inserted} inserted")
            total += inserted
            time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"\n  PH fact-checkers total inserted: {total}")
    return total


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape evidence-dense factual sources for the FAISS corpus."
    )
    parser.add_argument("--who",     action="store_true", help="Scrape WHO only")
    parser.add_argument("--cdc",     action="store_true", help="Scrape CDC only")
    parser.add_argument("--ph",      action="store_true", help="Scrape PH fact-checkers only")
    parser.add_argument("--dry-run", action="store_true", help="Count without inserting")
    args = parser.parse_args()

    run_all = not (args.who or args.cdc or args.ph)

    print("=" * 60)
    print("SocialProof — Evidence-Dense Source Scraper v1.0")
    if args.dry_run:
        print("DRY RUN — no data will be inserted")
    print("=" * 60)

    before = get_corpus_stats()
    print(f"\nCorpus before: {before.get('total_sentences', 0):,} sentences")

    grand_total = 0
    if run_all or args.who:
        grand_total += scrape_who(args.dry_run)
    if run_all or args.cdc:
        grand_total += scrape_cdc(args.dry_run)
    if run_all or args.ph:
        grand_total += scrape_ph_factcheckers(args.dry_run)

    after = get_corpus_stats()
    print("\n" + "=" * 60)
    print(f"Scraping complete.")
    print(f"  Inserted this run:  {grand_total:,} sentences")
    print(f"  Corpus total now:   {after.get('total_sentences', 0):,} sentences")
    print("\nNext step: rebuild FAISS index to include new sentences:")
    print("  python scripts/rebuild_faiss.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
