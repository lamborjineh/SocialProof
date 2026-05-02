"""
corpus/stat_extractor.py — Structured Data Extraction for Tier 1 / Tier 2 Sources
SocialProof v3.0

Extracts numeric/statistical fields from government press releases and
international organisation reports and stores them in the `structured_stats`
table alongside the normal sentences pipeline.

Fields extracted per record:
  source_domain — e.g. "psa.gov.ph"
  url           — article URL
  year          — calendar year (int) if found, else NULL
  metric        — descriptor string, e.g. "inflation rate", "GDP growth"
  value         — numeric value as float
  unit          — unit string if detected (%, PHP, USD, pp, ...)
  sentence      — the full sentence the triple was extracted from

Extraction strategy (in priority order):
  1. HTML <table> cells — best for PSA/BSP/NEDA statistical release pages.
  2. Numeric sentence pattern — regex for "metric was X%" / "X% metric" forms.

Only sentences/rows with a detected numeric value are stored here.
The normal sentences pipeline still stores ALL sentences regardless.
"""

from __future__ import annotations
import re
import sqlite3
from typing import Optional
from pathlib import Path
from bs4 import BeautifulSoup

from corpus.db import get_connection

# ── Metric keyword patterns ───────────────────────────────────────────────────
# Ordered from more specific to more general so the first match wins.
_METRIC_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("inflation rate",      re.compile(r"\binflation\b", re.I)),
    ("GDP growth",          re.compile(r"\bgdp\b.*\bgrowth\b|\bgrowth\b.*\bgdp\b", re.I)),
    ("unemployment rate",   re.compile(r"\bunemployment\b", re.I)),
    ("poverty incidence",   re.compile(r"\bpoverty\b", re.I)),
    ("interest rate",       re.compile(r"\binterest\s+rate\b", re.I)),
    ("population",          re.compile(r"\bpopulation\b", re.I)),
    ("literacy rate",       re.compile(r"\bliteracy\b", re.I)),
    ("malnutrition rate",   re.compile(r"\bmalnutrition\b|\bundernutrition\b", re.I)),
    ("enrollment rate",     re.compile(r"\benrollment\b|\benrolment\b", re.I)),
    ("rice production",     re.compile(r"\brice\s+production\b|\bpalay\b", re.I)),
    ("exports",             re.compile(r"\bexports?\b", re.I)),
    ("imports",             re.compile(r"\bimports?\b", re.I)),
    ("trade balance",       re.compile(r"\btrade\s+balance\b|\btrade\s+deficit\b", re.I)),
    ("remittances",         re.compile(r"\bremittances?\b", re.I)),
    ("foreign reserves",    re.compile(r"\bforeign\s+(?:exchange\s+)?reserves?\b", re.I)),
]

_YEAR_RE      = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
_NUMBER_RE    = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?")
_UNIT_RE      = re.compile(r"\b(%|percent|PHP|USD|billion|million|trillion|pp|percentage\s+points?)\b", re.I)


def _detect_year(text: str) -> Optional[int]:
    m = _YEAR_RE.search(text)
    return int(m.group(1)) if m else None


def _detect_value(text: str) -> Optional[float]:
    """Return the first numeric value found in text, or None."""
    m = _NUMBER_RE.search(text)
    if m:
        try:
            return float(m.group(0).replace(",", ""))
        except ValueError:
            pass
    return None


def _detect_unit(text: str) -> str:
    m = _UNIT_RE.search(text)
    return m.group(1) if m else ""


def _detect_metric(text: str) -> str:
    for label, pat in _METRIC_PATTERNS:
        if pat.search(text):
            return label
    return "statistic"


# ── Table extraction ──────────────────────────────────────────────────────────

def _extract_from_tables(html: str, domain: str, url: str) -> list[dict]:
    """
    Parse <table> elements on a page.
    Looks for rows where at least one cell contains a numeric value.
    Returns list of {domain, url, year, metric, value, unit, sentence}.
    """
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []

    # Try to find a page-level year (useful when table header omits year)
    body_text = soup.get_text(" ")
    page_year = _detect_year(body_text)

    for table in soup.find_all("table"):
        headers: list[str] = []
        header_row = table.find("tr")
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]

        for row in table.find_all("tr")[1:]:  # skip header row
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells:
                continue

            row_text = " ".join(cells)
            value    = _detect_value(row_text)
            if value is None:
                continue

            year   = _detect_year(row_text) or page_year
            unit   = _detect_unit(row_text)
            metric = _detect_metric(row_text)

            # Use column header as metric if available and more descriptive
            for i, cell in enumerate(cells):
                if _NUMBER_RE.fullmatch(cell.replace(",", "")):
                    if i < len(headers) and len(headers[i]) > 3:
                        metric = headers[i]
                    break

            records.append({
                "source_domain": domain,
                "url":           url,
                "year":          year,
                "metric":        metric,
                "value":         value,
                "unit":          unit,
                "sentence":      row_text[:500],
            })

    return records


# ── Sentence-level extraction ─────────────────────────────────────────────────

def _extract_from_sentences(sentences: list[str], domain: str, url: str) -> list[dict]:
    """
    Scan text sentences for numeric statements.
    A sentence qualifies if it contains a numeric value AND a metric keyword.
    """
    records: list[dict] = []

    for s in sentences:
        value = _detect_value(s)
        if value is None:
            continue
        # Must also contain a metric keyword to avoid bare numbers
        metric = _detect_metric(s)
        if metric == "statistic" and not _UNIT_RE.search(s):
            continue  # no unit and no metric keyword — too ambiguous

        year = _detect_year(s)
        unit = _detect_unit(s)

        records.append({
            "source_domain": domain,
            "url":           url,
            "year":          year,
            "metric":        metric,
            "value":         value,
            "unit":          unit,
            "sentence":      s[:500],
        })

    return records


# ── Public API ────────────────────────────────────────────────────────────────

def extract_stats(html: str, sentences: list[str], domain: str, url: str) -> list[dict]:
    """
    Run both extraction strategies and return merged deduplicated records.
    Table extraction is tried first; sentence extraction fills in the rest.
    """
    table_recs = _extract_from_tables(html, domain, url)
    sent_recs  = _extract_from_sentences(sentences, domain, url)

    # Deduplicate by (metric, value) keeping table records preferred
    seen: set[tuple] = set()
    merged: list[dict] = []

    for r in table_recs + sent_recs:
        key = (r["metric"].lower()[:40], r["value"])
        if key not in seen:
            seen.add(key)
            merged.append(r)

    return merged


def insert_stats(records: list[dict]) -> int:
    """
    Upsert extracted stat records into the structured_stats table.
    Returns number of rows inserted/replaced.
    """
    if not records:
        return 0

    conn = get_connection()
    c    = conn.cursor()
    count = 0
    for r in records:
        try:
            c.execute(
                """INSERT OR IGNORE INTO structured_stats
                   (source_domain, url, year, metric, value, unit, sentence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["source_domain"], r["url"], r.get("year"), r["metric"],
                 r["value"], r.get("unit", ""), r.get("sentence", "")),
            )
            count += c.rowcount
        except sqlite3.Error:
            pass
    conn.commit()
    conn.close()
    return count
