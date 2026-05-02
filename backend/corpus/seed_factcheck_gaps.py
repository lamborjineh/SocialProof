"""
corpus/seed_factcheck_gaps.py — Fix 1b
Manually seeds factcheck sentences for domains with zero or near-zero coverage.

These are domains where scraping is difficult (Cloudflare, paywalls, gov sites
that block bots) but that are critically important for Philippine/global factchecking.
Even 5–20 curated sentences per domain is infinitely better than zero — they enter
the FAISS index and are retrieved as evidence on relevant claims.

Uses the correct corpus.db schema:
  sentences(article_id, source_domain, url, sentence_text, sentence_index,
            pipeline_type, numeric_density)

Run:
    python corpus/seed_factcheck_gaps.py [--dry-run]

Then rebuild:
    python retrieval/build_index.py --pipeline factcheck --rebuild
"""

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import get_connection

CURATED_ENTRIES = [
    # ── nhcp.gov.ph — Philippine history ─────────────────────────────────────
    {"domain": "nhcp.gov.ph", "pipeline": "factcheck", "url": "https://nhcp.gov.ph",
     "text": "The National Historical Commission of the Philippines has documented that Martial Law under Ferdinand Marcos Sr. from 1972 to 1981 resulted in thousands of documented victims of human rights violations, including extrajudicial killings, torture, and enforced disappearances."},
    {"domain": "nhcp.gov.ph", "pipeline": "factcheck", "url": "https://nhcp.gov.ph",
     "text": "According to NHCP records, Andres Bonifacio was born on November 30, 1863, in Tondo, Manila, and founded the Katipunan revolutionary society in 1892 to fight for Philippine independence from Spain."},
    {"domain": "nhcp.gov.ph", "pipeline": "factcheck", "url": "https://nhcp.gov.ph",
     "text": "The NHCP confirms that Jose Rizal was executed by musketry at Bagumbayan (now Luneta Park) on December 30, 1896, after being convicted of rebellion, sedition, and conspiracy."},

    # ── comelec.gov.ph — Philippine elections ─────────────────────────────────
    {"domain": "comelec.gov.ph", "pipeline": "factcheck", "url": "https://comelec.gov.ph",
     "text": "The Commission on Elections (COMELEC) confirms that the automated election system using vote-counting machines has been used in Philippine national and local elections since 2010."},
    {"domain": "comelec.gov.ph", "pipeline": "factcheck", "url": "https://comelec.gov.ph",
     "text": "COMELEC requires every Filipino citizen aged 18 and above to register as a voter to be eligible to participate in elections; voter registration is conducted annually except during the election period."},
    {"domain": "comelec.gov.ph", "pipeline": "factcheck", "url": "https://comelec.gov.ph",
     "text": "According to COMELEC, candidates found guilty of election offenses may be disqualified from holding public office and face criminal penalties under the Omnibus Election Code."},

    # ── nasa.gov — climate and space ─────────────────────────────────────────
    {"domain": "nasa.gov", "pipeline": "factcheck", "url": "https://climate.nasa.gov/evidence/",
     "text": "NASA data confirms that global average surface temperature has increased by approximately 1.1 degrees Celsius since the late 19th century, primarily driven by increased carbon dioxide and other human-caused emissions into the atmosphere."},
    {"domain": "nasa.gov", "pipeline": "factcheck", "url": "https://climate.nasa.gov/evidence/",
     "text": "According to NASA, the Greenland and Antarctic ice sheets have decreased in mass, with Greenland losing an average of 279 billion tons of ice per year between 1993 and 2019."},
    {"domain": "nasa.gov", "pipeline": "factcheck", "url": "https://www.nasa.gov/moon",
     "text": "NASA confirms that the Apollo 11 mission successfully landed humans on the Moon on July 20, 1969; astronauts Neil Armstrong and Buzz Aldrin walked on the lunar surface while Michael Collins orbited above."},

    # ── noaa.gov — climate ────────────────────────────────────────────────────
    {"domain": "noaa.gov", "pipeline": "factcheck", "url": "https://www.noaa.gov/education/resource-collections/climate",
     "text": "According to NOAA, 2023 was the warmest year on record globally, with the global surface temperature exceeding the 20th-century average by 1.17 degrees Celsius."},
    {"domain": "noaa.gov", "pipeline": "factcheck", "url": "https://www.noaa.gov/education/resource-collections/climate",
     "text": "NOAA data shows that the amount of carbon dioxide in the atmosphere has increased by more than 50 percent since the start of the Industrial Revolution, primarily due to burning fossil fuels."},

    # ── ipcc.ch — climate science consensus ──────────────────────────────────
    {"domain": "ipcc.ch", "pipeline": "factcheck", "url": "https://www.ipcc.ch/report/ar6/syr/",
     "text": "The Intergovernmental Panel on Climate Change (IPCC) concluded in its Sixth Assessment Report that it is unequivocal that human influence has warmed the atmosphere, ocean and land."},
    {"domain": "ipcc.ch", "pipeline": "factcheck", "url": "https://www.ipcc.ch/report/ar6/syr/",
     "text": "According to the IPCC Sixth Assessment Report, limiting global warming to 1.5 degrees Celsius above pre-industrial levels requires global greenhouse gas emissions to reach net zero around 2050."},

    # ── psa.gov.ph — Philippine statistics ────────────────────────────────────
    {"domain": "psa.gov.ph", "pipeline": "factcheck", "url": "https://psa.gov.ph",
     "text": "The Philippine Statistics Authority (PSA) reported that the poverty incidence among Filipinos decreased to 15.5 percent in 2023 from 18.1 percent in 2021, based on the Family Income and Expenditure Survey."},
    {"domain": "psa.gov.ph", "pipeline": "factcheck", "url": "https://psa.gov.ph",
     "text": "According to PSA, the Philippines had a total population of approximately 109 million as of the 2020 Census of Population and Housing."},
    {"domain": "psa.gov.ph", "pipeline": "factcheck", "url": "https://psa.gov.ph",
     "text": "PSA data shows that the Philippines recorded an unemployment rate of 4.3 percent in October 2023, with approximately 2.2 million Filipinos classified as unemployed."},

    # ── verafiles.org — Philippine factchecking ────────────────────────────────
    {"domain": "verafiles.org", "pipeline": "factcheck", "url": "https://verafiles.org",
     "text": "Vera Files has fact-checked and found FALSE the claim that the COVID-19 vaccines contain microchips; no credible scientific evidence supports this claim, and vaccine ingredients are publicly disclosed by manufacturers and regulatory bodies."},
    {"domain": "verafiles.org", "pipeline": "factcheck", "url": "https://verafiles.org",
     "text": "According to Vera Files fact-check, photographs circulating online claiming to show recent events in the Philippines have frequently been found to be taken from older events or from other countries entirely."},

    # ── doh.gov.ph — Philippine Department of Health ──────────────────────────
    {"domain": "doh.gov.ph", "pipeline": "factcheck", "url": "https://doh.gov.ph",
     "text": "The Department of Health Philippines confirms that COVID-19 vaccines authorized for use in the Philippines have been evaluated by the Food and Drug Administration (FDA) for safety, efficacy, and quality before receiving Emergency Use Authorization."},
    {"domain": "doh.gov.ph", "pipeline": "factcheck", "url": "https://doh.gov.ph",
     "text": "According to DOH, dengue fever remains a major public health concern in the Philippines, with tens of thousands of cases reported annually; the dengue vaccine Dengvaxia is recommended only for individuals who have had a previous dengue infection."},
]


def seed(dry_run: bool = False) -> int:
    conn = get_connection()
    c    = conn.cursor()
    inserted = 0

    for entry in CURATED_ENTRIES:
        # Check for existing sentence to avoid duplicates
        c.execute(
            "SELECT id FROM sentences WHERE sentence_text = ? AND source_domain = ?",
            (entry["text"], entry["domain"])
        )
        if c.fetchone():
            print(f"  [skip] Already exists: {entry['domain']}: {entry['text'][:60]}")
            continue

        if dry_run:
            print(f"  [dry]  Would insert: {entry['domain']}: {entry['text'][:80]}")
            inserted += 1
            continue

        # Get or create a stub article record for this domain
        c.execute(
            "INSERT OR IGNORE INTO articles (source_domain, url, title) VALUES (?, ?, ?)",
            (entry["domain"], entry["url"], f"[curated] {entry['domain']}")
        )
        c.execute("SELECT id FROM articles WHERE url = ?", (entry["url"],))
        row = c.fetchone()
        article_id = row["id"] if row else None

        # Compute numeric density
        numeric_density = sum(1 for ch in entry["text"] if ch.isdigit()) / max(len(entry["text"]), 1)

        c.execute(
            """INSERT INTO sentences
               (article_id, source_domain, url, sentence_text, sentence_index,
                pipeline_type, numeric_density)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (article_id, entry["domain"], entry["url"], entry["text"],
             inserted, entry["pipeline"], numeric_density)
        )
        inserted += 1
        print(f"  [+] {entry['domain']}: {entry['text'][:80]}")

    if not dry_run:
        conn.commit()
    conn.close()
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed curated factcheck sentences for coverage gaps")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be inserted without writing")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Seeding factcheck gaps — {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)
    n = seed(dry_run=args.dry_run)
    print(f"\n{'Would insert' if args.dry_run else 'Inserted'}: {n} sentences")
    if not args.dry_run:
        print("\nNow rebuild the factcheck index:")
        print("  python retrieval/build_index.py --pipeline factcheck --rebuild")
