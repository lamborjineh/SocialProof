"""
SocialProof — scripts/sync_mbfc.py
Downloads the iffy.news MBFC export and upserts domain credibility ratings
into the mbfc_domains table.

Run manually:
    python scripts/sync_mbfc.py
    python scripts/sync_mbfc.py --force        # re-sync even if recently updated
    python scripts/sync_mbfc.py --dry-run      # preview without writing to DB

Schedule monthly via cron or a task scheduler.

The MBFC data is surfaced in the Source node as a non-judgmental signal:
"This domain has a [MIXED] factual reporting rating."
It is never shown as a verdict and does not override user judgment.
"""

import argparse
import io
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests
import sqlalchemy as sa

# Add project root to path so config imports work when run from scripts/
sys.path.insert(0, ".")

from config import logger

MBFC_URL = "https://iffy.news/iffy-plus.csv"
STALE_DAYS = 30  # re-sync if last_synced is older than this


def get_engine():
    from config import DATABASE_URL
    from sqlalchemy import create_engine
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def is_stale(engine) -> bool:
    """Returns True if mbfc_domains has no rows or the oldest sync is > STALE_DAYS."""
    try:
        with engine.connect() as conn:
            result = conn.execute(sa.text(
                "SELECT MIN(last_synced) FROM mbfc_domains"
            ))
            oldest = result.scalar()
            if oldest is None:
                return True
            return datetime.utcnow() - oldest > timedelta(days=STALE_DAYS)
    except Exception as e:
        logger.warning(f"[sync_mbfc] Stale check failed: {e}")
        return True


def fetch_csv(url: str) -> pd.DataFrame:
    logger.info(f"[sync_mbfc] Downloading from {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    logger.info(f"[sync_mbfc] Downloaded {len(df)} rows")
    return df


def normalize_domain(raw: str) -> str | None:
    """
    Extract bare domain from a URL or domain string.
    Strips www., trailing slashes, and paths.
    Returns None if unparseable.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip().lower()
    if not raw.startswith("http"):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        domain = parsed.netloc.replace("www.", "").strip("/")
        return domain if domain else None
    except Exception:
        return None


# iffy.news column names can vary between exports — map known variants
COLUMN_MAP = {
    "domain":           ["domain", "url", "source_url", "website"],
    "factual_reporting": ["factual_reporting", "factual", "fact_reporting"],
    "bias_rating":      ["bias_rating", "bias", "media_bias"],
    "credibility_rating": ["credibility_rating", "credibility"],
    "country":          ["country"],
    "notes_url":        ["notes_url", "mbfc_url", "source"],
}


def resolve_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def build_records(df: pd.DataFrame) -> list[dict]:
    col = {
        field: resolve_column(df, candidates)
        for field, candidates in COLUMN_MAP.items()
    }

    if col["domain"] is None:
        raise ValueError(
            f"[sync_mbfc] Cannot find domain column. Available: {list(df.columns)}"
        )

    records = []
    for _, row in df.iterrows():
        domain = normalize_domain(row.get(col["domain"], ""))
        if not domain:
            continue

        records.append({
            "domain":            domain,
            "factual_reporting": row.get(col["factual_reporting"]) if col["factual_reporting"] else None,
            "bias_rating":       row.get(col["bias_rating"])       if col["bias_rating"]       else None,
            "credibility_rating": row.get(col["credibility_rating"]) if col["credibility_rating"] else None,
            "country":           row.get(col["country"])           if col["country"]           else None,
            "notes_url":         row.get(col["notes_url"])         if col["notes_url"]         else None,
            "last_synced":       datetime.utcnow(),
        })

    # Deduplicate by domain (keep last)
    seen: dict[str, dict] = {}
    for r in records:
        seen[r["domain"]] = r
    return list(seen.values())


def upsert_records(engine, records: list[dict], dry_run: bool = False) -> int:
    if dry_run:
        logger.info(f"[sync_mbfc] DRY RUN — would upsert {len(records)} domains")
        for r in records[:5]:
            logger.info(f"  Sample: {r}")
        return 0

    upsert_sql = sa.text("""
        INSERT INTO mbfc_domains
            (domain, factual_reporting, bias_rating, credibility_rating, country, notes_url, last_synced)
        VALUES
            (:domain, :factual_reporting, :bias_rating, :credibility_rating, :country, :notes_url, :last_synced)
        ON DUPLICATE KEY UPDATE
            factual_reporting  = VALUES(factual_reporting),
            bias_rating        = VALUES(bias_rating),
            credibility_rating = VALUES(credibility_rating),
            country            = VALUES(country),
            notes_url          = VALUES(notes_url),
            last_synced        = VALUES(last_synced)
    """)

    batch_size = 500
    total = 0
    with engine.connect() as conn:
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            conn.execute(upsert_sql, batch)
            conn.commit()
            total += len(batch)
            logger.info(f"[sync_mbfc] Upserted {total}/{len(records)}")

    return total


def main():
    parser = argparse.ArgumentParser(description="Sync iffy.news MBFC data into mbfc_domains")
    parser.add_argument("--force",   action="store_true", help="Sync even if data is fresh")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--url",     default=MBFC_URL,    help="Override iffy.news CSV URL")
    args = parser.parse_args()

    engine = get_engine()

    if not args.force and not is_stale(engine):
        logger.info("[sync_mbfc] Data is fresh (< 30 days old). Use --force to re-sync.")
        return

    try:
        df = fetch_csv(args.url)
        records = build_records(df)
        logger.info(f"[sync_mbfc] Parsed {len(records)} unique domains")

        total = upsert_records(engine, records, dry_run=args.dry_run)

        if not args.dry_run:
            logger.info(f"[sync_mbfc] Done. {total} domains upserted into mbfc_domains.")
        else:
            logger.info("[sync_mbfc] Dry run complete. No rows written.")

    except Exception as e:
        logger.error(f"[sync_mbfc] Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
