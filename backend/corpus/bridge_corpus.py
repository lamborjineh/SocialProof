"""
corpus/bridge_corpus.py
Reads sentences from corpus.db and exports them as Python CORPUS entries
that can be pasted directly into pipeline/evidence_retrieval.py

Run from your_project/ folder:
    python corpus/bridge_corpus.py

Output: corpus/corpus_entries.py  — ready to copy-paste into evidence_retrieval.py
"""

import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from corpus.db import get_all_sentences, get_connection
from corpus.source_registry import REPUTATION, get_pipeline

# How many entries to export (keep it manageable — 150 is enough)
EXPORT_LIMIT = 150

# Minimum sentence length — short sentences are useless as evidence
MIN_LENGTH = 60

# Only export sentences from sources above this reputation floor
MIN_REPUTATION = 0.70


def get_sentences_with_source_info():
    """Pull sentences with their source domain and reputation."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT s.sentence_text, s.source_domain, s.url,
               s.pipeline_type, s.numeric_density,
               a.date_published
        FROM sentences s
        LEFT JOIN articles a ON s.article_id = a.id
        WHERE length(s.sentence_text) >= ?
        ORDER BY s.numeric_density DESC, s.id DESC
    """, (MIN_LENGTH,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def sentence_to_label(pipeline_type: str, sentence: str) -> str:
    """
    Assign a default label based on pipeline type.
    Fact-check sources = contradict (they debunk things).
    Stats sources = support (they report verified numbers).
    News = neutral (reporting, not confirming/denying).
    """
    if pipeline_type == "factcheck":
        # Fact-checkers mostly debunk — default contradict, NLI will refine
        text_lower = sentence.lower()
        if any(w in text_lower for w in ["false", "misleading", "no evidence", "debunked",
                                          "not true", "inaccurate", "fabricated", "satire"]):
            return "contradict"
        return "neutral"
    elif pipeline_type == "stats":
        return "support"
    return "neutral"


def build_source_label(domain: str) -> str:
    """Human-readable source label from domain."""
    labels = {
        "psa.gov.ph":          "Philippine Statistics Authority",
        "bsp.gov.ph":          "Bangko Sentral ng Pilipinas",
        "doh.gov.ph":          "Department of Health Philippines",
        "neda.gov.ph":         "NEDA Philippines",
        "dole.gov.ph":         "DOLE Philippines",
        "deped.gov.ph":        "DepEd Philippines",
        "dof.gov.ph":          "Department of Finance Philippines",
        "pna.gov.ph":          "Philippine News Agency",
        "who.int":             "World Health Organization",
        "worldbank.org":       "World Bank",
        "un.org":              "United Nations",
        "unicef.org":          "UNICEF",
        "fao.org":             "FAO",
        "reuters.com":         "Reuters",
        "apnews.com":          "AP News",
        "bbc.com":             "BBC News",
        "rappler.com":         "Rappler",
        "verafiles.org":       "VERA Files (Fact Check)",
        "tsek.ph":             "Tsek.ph (Fact Check)",
        "snopes.com":          "Snopes (Fact Check)",
        "politifact.com":      "PolitiFact (Fact Check)",
        "factcheck.org":       "FactCheck.org (Fact Check)",
        "fullfact.org":        "Full Fact (Fact Check)",
        "inquirer.net":        "Philippine Daily Inquirer",
        "philstar.com":        "PhilStar",
        "gmanetwork.com":      "GMA News",
        "cnnphilippines.com":  "CNN Philippines",
        "news.abs-cbn.com":    "ABS-CBN News",
        "aljazeera.com":       "Al Jazeera",
        "comelec.gov.ph":      "Commission on Elections Philippines",
        "nih.gov":             "National Institutes of Health",
        "cdc.gov":             "Centers for Disease Control (CDC)",
        "nature.com":          "Nature",
        "thelancet.com":       "The Lancet",
        "bmj.com":             "BMJ",
        "ourworldindata.org":  "Our World in Data",
        "britannica.com":      "Encyclopaedia Britannica",
    }
    return labels.get(domain, domain)


def main():
    print("[Bridge] Reading from corpus.db...")
    rows = get_sentences_with_source_info()
    print(f"[Bridge] Found {len(rows)} total sentences")

    # Filter by reputation
    filtered = [r for r in rows if REPUTATION.get(r["source_domain"], 0) >= MIN_REPUTATION]
    print(f"[Bridge] {len(filtered)} sentences meet reputation threshold ({MIN_REPUTATION})")

    # ── Balanced export: enforce pipeline and per-domain quotas ───────────────
    # Target distribution: 40% news, 35% stats, 25% factcheck
    # This inverts the raw corpus bias (news-heavy) toward a truth-first design.
    pipeline_targets = {
        "stats":     int(EXPORT_LIMIT * 0.35),   # 52 of 150
        "factcheck": int(EXPORT_LIMIT * 0.25),   # 37 of 150
        "news":      int(EXPORT_LIMIT * 0.40),   # 60 of 150 (remainder)
    }
    # Per-domain cap: no single domain can exceed this share of its pipeline bucket
    # Prevents NPR/WHO/etc. from swamping their respective pipeline slots
    PER_DOMAIN_CAP = max(5, EXPORT_LIMIT // 15)  # ~10 per domain at limit=150

    pipeline_counts: dict = {p: 0 for p in pipeline_targets}
    domain_counts:   dict = {}
    selected = []

    for row in filtered:
        pipeline = row.get("pipeline_type") or get_pipeline(row["source_domain"])
        domain   = row["source_domain"]

        # Skip if this pipeline bucket is full
        if pipeline_counts.get(pipeline, 0) >= pipeline_targets.get(pipeline, 0):
            continue
        # Skip if this domain has hit its per-domain cap
        if domain_counts.get(domain, 0) >= PER_DOMAIN_CAP:
            continue

        selected.append(row)
        pipeline_counts[pipeline] = pipeline_counts.get(pipeline, 0) + 1
        domain_counts[domain]     = domain_counts.get(domain, 0) + 1

        if len(selected) >= EXPORT_LIMIT:
            break

    # Report what we actually got
    print(f"[Bridge] Balanced selection: {len(selected)} sentences")
    for p, count in pipeline_counts.items():
        target = pipeline_targets.get(p, 0)
        print(f"  {p:12s}: {count:3d} / {target} target")
    print(f"  Per-domain cap: {PER_DOMAIN_CAP}")
    by_domain = {}
    for row in selected:
        d = row["source_domain"]
        by_domain[d] = by_domain.get(d, 0) + 1
    for domain, count in sorted(by_domain.items(), key=lambda x: -x[1])[:10]:
        print(f"    {domain}: {count}")

    entries = []
    for r in selected:
        pipeline = r.get("pipeline_type") or get_pipeline(r["source_domain"])
        label    = sentence_to_label(pipeline, r["sentence_text"])
        domain   = r["source_domain"]
        entry = {
            "text":         r["sentence_text"].strip(),
            "label":        label,
            "source_label": build_source_label(domain),
            "source_url":   r["url"] or f"https://{domain}",
        }
        entries.append(entry)

    # Write as Python that can be copy-pasted into evidence_retrieval.py
    out_path = Path(__file__).parent / "corpus_entries.py"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# Auto-generated by bridge_corpus.py\n")
        f.write("# Copy the entries list below and APPEND it to the CORPUS list\n")
        f.write("# in Umamusume/pipeline/evidence_retrieval.py\n\n")
        f.write("SCRAPED_ENTRIES = [\n")
        for e in entries:
            f.write("    {\n")
            f.write(f'        "text": {json.dumps(e["text"])},\n')
            f.write(f'        "label": {json.dumps(e["label"])},\n')
            f.write(f'        "source_label": {json.dumps(e["source_label"])},\n')
            f.write(f'        "source_url": {json.dumps(e["source_url"])},\n')
            f.write("    },\n")
        f.write("]\n")

    print(f"[Bridge] Exported {len(entries)} entries to: {out_path}")
    print("[Bridge] Next: copy SCRAPED_ENTRIES list into evidence_retrieval.py CORPUS list")
    print("[Bridge] Then restart uvicorn — embeddings rebuild automatically on startup")


if __name__ == "__main__":
    main()