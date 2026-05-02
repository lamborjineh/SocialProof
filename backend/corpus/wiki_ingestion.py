"""
corpus/wiki_ingestion.py
Fetches Wikipedia article summaries via the REST API and seeds them
into the corpus DB as factcheck-pipeline sentences.

Usage:
    python corpus/wiki_ingestion.py
    python corpus/wiki_ingestion.py --topics "climate change" "vaccine"
    python corpus/wiki_ingestion.py --dry-run
    python corpus/wiki_ingestion.py --limit 100

After running, rebuild the index:
    python retrieval/build_index.py --rebuild
"""

import argparse
import hashlib
import sys
import time
from pathlib import Path
from typing import List, Optional

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

DEFAULT_TOPICS = [
    # ── Philippines — politics & history ─────────────────────────────────────
    "Philippines",
    "History of the Philippines",
    "Ferdinand Marcos",
    "Imelda Marcos",
    "Rodrigo Duterte",
    "Bongbong Marcos",
    "Emilio Aguinaldo",
    "People Power Revolution",
    "Philippine independence",
    "Bangsamoro",
    "Jose Rizal",
    "Andres Bonifacio",
    "Corazon Aquino",
    "Benigno Aquino III",
    "Leni Robredo",
    "Philippine Constitution",
    "COMELEC",
    "Martial law in the Philippines",
    "Philippine–American War",
    "Moro Islamic Liberation Front",
    "Abu Sayyaf",
    "New People's Army Philippines",
    "Duterte drug war",
    "International Criminal Court Philippines",
    "EDSA Revolution",
    "Ferdinand Marcos human rights violations",

    # ── Philippines — geography & economy ────────────────────────────────────
    "Philippine peso",
    "Bangko Sentral ng Pilipinas",
    "Philippine Statistics Authority",
    "Overseas Filipino Worker",
    "Manila",
    "Quezon City",
    "Davao City",
    "Cebu",
    "Luzon",
    "Mindanao",
    "Visayas",
    "Mount Apo",
    "Laguna de Bay",
    "Typhoon Haiyan",
    "Typhoon Odette",
    "Mount Pinatubo",
    "NEDA Philippines",
    "Philippine economy",
    "Inflation Philippines",
    "Philippine poverty",
    "Jeepney",
    "Maguindanao massacre",

    # ── Philippines — health & science ───────────────────────────────────────
    "Department of Health Philippines",
    "PhilHealth",
    "Dengue fever Philippines",
    "Tuberculosis in the Philippines",
    "Leptospirosis",
    "Rabies Philippines",
    "Philippine measles outbreak",
    "Dengvaxia controversy",
    "COVID-19 Philippines",
    "COVID-19 vaccination Philippines",
    "Philippine Red Cross",
    "National Kidney and Transplant Institute",
    "PAGASA",
    "PHIVOLCS",
    "Philippine Institute of Volcanology and Seismology",

    # ── Global health — vaccines & immunization ───────────────────────────────
    "COVID-19 pandemic",
    "COVID-19 vaccine",
    "Vaccination",
    "Vaccine safety",
    "Herd immunity",
    "mRNA vaccine",
    "AstraZeneca COVID-19 vaccine",
    "Pfizer–BioNTech COVID-19 vaccine",
    "Sinovac COVID-19 vaccine",
    "Ivermectin",
    "Hydroxychloroquine",
    "Vaccines and autism",
    "MMR vaccine",
    "Polio vaccine",
    "HPV vaccine",
    "Influenza vaccine",
    "Vaccine hesitancy",
    "Anti-vaccination movement",
    "VAERS",
    "COVID-19 misinformation",

    # ── Global health — diseases ──────────────────────────────────────────────
    "Antibiotic resistance",
    "HIV/AIDS",
    "Malaria",
    "Tuberculosis",
    "Ebola virus disease",
    "Monkeypox",
    "Influenza",
    "Cancer",
    "Breast cancer",
    "Lung cancer",
    "Colorectal cancer",
    "Diabetes",
    "Type 2 diabetes",
    "Hypertension",
    "Cardiovascular disease",
    "Stroke",
    "Alzheimer's disease",
    "Mental health",
    "Depression",
    "Anxiety disorder",
    "Autism spectrum disorder",
    "Obesity",

    # ── Global health — nutrition & lifestyle ─────────────────────────────────
    "Vitamin D",
    "Vitamin C",
    "Zinc",
    "Sugar",
    "Saturated fat",
    "Trans fat",
    "Processed food",
    "Organic food",
    "Genetically modified organism",
    "GMO safety",
    "Fluoride",
    "Fluoridation of water",
    "Aspartame",
    "MSG food additive",
    "Intermittent fasting",
    "Detox diet",
    "Alkaline diet",
    "Raw water",
    "Essential oil",
    "Homeopathy",
    "Alternative medicine",
    "Traditional Chinese medicine",
    "Colloidal silver",
    "Bleach therapy",
    "Cancer cure misinformation",

    # ── Climate & environment ─────────────────────────────────────────────────
    "Climate change",
    "Global warming",
    "IPCC",
    "Paris Agreement",
    "Carbon dioxide in Earth's atmosphere",
    "Greenhouse effect",
    "Ozone layer",
    "Ozone depletion",
    "Sea level rise",
    "Arctic ice",
    "Coral reef",
    "Amazon rainforest",
    "Deforestation",
    "Renewable energy",
    "Solar power",
    "Wind power",
    "Electric vehicle",
    "Nuclear power",
    "Fossil fuel",
    "Carbon footprint",
    "Great Barrier Reef",
    "Ocean acidification",
    "Extreme weather event",
    "Climate change denial",

    # ── Science consensus topics ──────────────────────────────────────────────
    "Evolution",
    "Natural selection",
    "Age of the universe",
    "Big Bang",
    "DNA",
    "Human genome",
    "Photosynthesis",
    "Germ theory of disease",
    "Speed of light",
    "Black hole",
    "Tectonic plates",
    "Antibiotic",
    "Penicillin",
    "Stem cell",
    "CRISPR",
    "Artificial intelligence",
    "5G technology",
    "Electromagnetic radiation",
    "Microwave oven",
    "Wi-Fi health effects",

    # ── Common misinformation targets ─────────────────────────────────────────
    "Flat Earth",
    "Moon landing conspiracy theory",
    "Chemtrails",
    "Microchip implant conspiracy",
    "QAnon",
    "COVID-19 conspiracy theories",
    "Bill Gates conspiracy theories",
    "Pizzagate",
    "Deep state",
    "Great Reset",
    "Agenda 21",
    "Population control conspiracy",
    "Vaccine microchip conspiracy",
    "5G COVID conspiracy",
    "Soros conspiracy theories",

    # ── Media & information literacy ──────────────────────────────────────────
    "Misinformation",
    "Disinformation",
    "Fake news",
    "Media literacy",
    "Fact-checking",
    "Propaganda",
    "Social media and misinformation",
    "Deepfake",
    "Confirmation bias",
    "Echo chamber (media)",
    "Filter bubble",
    "Clickbait",
    "Satire",
    "Parody news",
    "Source credibility",

    # ── History ───────────────────────────────────────────────────────────────
    "World War II",
    "World War I",
    "Holocaust",
    "United Nations",
    "Moon landing",
    "Cold War",
    "Berlin Wall",
    "French Revolution",
    "Ferdinand Magellan",
    "Spanish-American War",
    "Battle of Mactan",
    "Katipunan",
    "American Civil War",
    "Hiroshima and Nagasaki atomic bombings",
    "Nuremberg trials",

    # ── Global institutions & organizations ───────────────────────────────────
    "World Health Organization",
    "Centers for Disease Control and Prevention",
    "United Nations Children's Fund",
    "International Monetary Fund",
    "World Bank",
    "Amnesty International",
    "Human Rights Watch",
    "International Criminal Court",
    "ASEAN",
    "European Union",
    "NATO",

    # ── Drug policy (PH-relevant) ─────────────────────────────────────────────
    "War on drugs Philippines",
    "Illegal drug trade in the Philippines",
    "Philippine Drug Enforcement Agency",
    "Shabu drug Philippines",
    "Drug-related killings Philippines",
]

WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
DOMAIN   = "en.wikipedia.org"
PIPELINE = "factcheck"


def _hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()


def fetch_summary(topic: str) -> Optional[str]:
    url = WIKI_API.format(requests.utils.quote(topic.replace(" ", "_")))
    try:
        resp = requests.get(url, timeout=10,
                            headers={"User-Agent": "SocialProof/1.0 wiki_ingestion.py"})
        if resp.status_code == 200:
            return resp.json().get("extract", "")
        elif resp.status_code == 404:
            print(f"  [wiki] Not found: '{topic}'")
        else:
            print(f"  [wiki] HTTP {resp.status_code} for '{topic}'")
    except Exception as e:
        print(f"  [wiki] Request failed for '{topic}': {e}")
    return None


def split_sentences(text: str) -> List[str]:
    import re
    raw = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in raw if len(s.strip()) >= 30]


def _get_or_create_article_id(cursor, topic: str, domain: str) -> int:
    """
    Return id of a placeholder article row for this Wikipedia topic.
    Creates one if it doesn't exist. Satisfies the NOT NULL constraint
    on sentences.article_id.
    """
    url = f"https://{domain}/wiki/{topic.replace(' ', '_')}"
    cursor.execute("SELECT id FROM articles WHERE url = ?", (url,))
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute(
        """
        INSERT INTO articles (source_domain, url, title, content, word_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (domain, url, f"[Wikipedia] {topic}", "", 0),
    )
    return cursor.lastrowid


def seed_to_db(sentences: List[str], topic: str, domain: str = DOMAIN,
               dry_run: bool = False) -> int:
    from corpus.db import get_connection
    conn   = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT sentence_text FROM sentences WHERE source_domain = ?", (domain,)
    )
    existing = {_hash(row[0]) for row in cursor.fetchall()}

    # Create placeholder article row once per topic
    article_id = None
    if not dry_run:
        article_id = _get_or_create_article_id(cursor, topic, domain)

    added = 0
    for text in sentences:
        h = _hash(text)
        if h in existing:
            continue
        if not dry_run:
            try:
                cursor.execute(
                    """INSERT INTO sentences
                       (article_id, sentence_text, source_domain, url,
                        pipeline_type, numeric_density)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        article_id,
                        text,
                        domain,
                        f"https://{domain}/wiki/{topic.replace(' ', '_')}",
                        PIPELINE,
                        sum(ch.isdigit() for ch in text) / max(len(text), 1),
                    ),
                )
                existing.add(h)
            except Exception as e:
                print(f"  [wiki] DB insert error: {e}")
        added += 1

    if not dry_run:
        conn.commit()
    conn.close()
    return added


def run(topics: List[str], dry_run: bool = False, limit: int = 0):
    total_added   = 0
    total_fetched = 0
    i = 0

    for i, topic in enumerate(topics, 1):
        print(f"[{i}/{len(topics)}] Fetching: '{topic}' ...", end=" ", flush=True)
        extract = fetch_summary(topic)
        if not extract:
            print("skipped.")
            time.sleep(0.3)
            continue

        sentences = split_sentences(extract)
        total_fetched += len(sentences)
        print(f"{len(sentences)} sentences", end=" ")

        added = seed_to_db(sentences, topic=topic, dry_run=dry_run)
        total_added += added
        print(f"(+{added} new)")

        if limit and total_added >= limit:
            print(f"[wiki] Hit --limit {limit}. Stopping early.")
            break

        time.sleep(0.4)

    print(f"\n[wiki] {'DRY RUN — ' if dry_run else ''}Summary:")
    print(f"  Topics processed   : {i}")
    print(f"  Sentences fetched  : {total_fetched}")
    print(f"  New sentences added: {total_added}")
    if not dry_run and total_added > 0:
        print(f"\n[wiki] Done. Rebuild the index to activate new sentences:")
        print(f"       python retrieval/build_index.py --rebuild")


def main():
    parser = argparse.ArgumentParser(description="Ingest Wikipedia summaries into corpus")
    parser.add_argument("--topics", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    topics = args.topics if args.topics else DEFAULT_TOPICS
    print(f"[wiki] Starting ingestion of {len(topics)} topics "
          f"({'DRY RUN' if args.dry_run else 'LIVE'})\n")
    run(topics, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()