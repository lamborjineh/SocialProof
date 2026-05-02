"""
corpus/db.py  — quality gate added to insert_pipeline_sentences (v3.2)

Change summary (v3.2):
  - insert_pipeline_sentences now runs every sentence through
    _sentence_quality_gate() before INSERT.
    Rejects: too short, too few words, questions, boilerplate, fragments.
    Factcheck pipeline applies a stricter factual-marker check.
  - All other functions are unchanged from v3.1.
"""

import re
import sqlite3
from pathlib import Path
from typing import List

# ── Path ──────────────────────────────────────────────────────────────────────
_DB_PATH = Path(__file__).parent.parent / "data" / "corpus.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ── Quality gate ──────────────────────────────────────────────────────────────

_MIN_LEN         = 40    # chars
_MIN_LEN_FC      = 50    # stricter for factcheck pipeline
_MIN_WORDS       = 8
_MAX_LEN         = 500   # same as scraper split_sentences upper bound

_BOILERPLATE_RE = re.compile(
    r"subscribe to|sign up for|read more:|also read:|follow us on|"
    r"advertisement|click here|all rights reserved|terms of service|"
    r"privacy policy|this is ai generated|for context, always refer|"
    r"you may also like|share this article|in photos:|watch:|look:|"
    r"just in:|developing story|note to editors|press release|"
    r"for media inquiries|media contact|for immediate release",
    re.IGNORECASE,
)

# Factual markers required for factcheck-pipeline sentences
_FACTUAL_RE = re.compile(
    r"\b\d+\s*(%|percent|million|billion|thousand|trillion)\b|"
    r"\baccording to\b|"
    r"\b(said|confirmed|announced|reported|stated|declared|showed?|found|revealed)\b|"
    r"\b(study|research|report|survey|data|analysis|evidence)\b|"
    r"\b(WHO|CDC|DOH|PSA|BSP|NEDA|UN|IMF|World Bank|IPCC|FDA|PNP|AFP)\b|"
    r"\b(law|bill|policy|ordinance|executive order|resolution|verdict|ruling)\b|"
    r"\b(true|false|misleading|unsubstantiated|debunked|verified|fact.?check)\b",
    re.IGNORECASE,
)


def _sentence_quality_gate(text: str, pipeline: str) -> bool:
    """
    Return True if the sentence is worth indexing; False to reject.

    Applied before every INSERT in insert_pipeline_sentences.
    Stricter rules for 'factcheck' pipeline because bad evidence
    directly misleads NLI decisions.
    """
    s = text.strip()

    # Length and word-count gates
    min_len = _MIN_LEN_FC if pipeline == "factcheck" else _MIN_LEN
    if len(s) < min_len or len(s) > _MAX_LEN:
        return False
    if len(s.split()) < _MIN_WORDS:
        return False

    # Terminal punctuation required — avoids mid-sentence fragments
    if s[-1] not in ".!\"'":
        return False

    # Questions are rarely useful as evidence
    if s.endswith("?"):
        return False

    # Boilerplate rejection
    if _BOILERPLATE_RE.search(s):
        return False

    # Factcheck pipeline: must contain at least one factual marker
    if pipeline == "factcheck" and not _FACTUAL_RE.search(s):
        return False

    return True


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    conn = get_connection()
    c    = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source_domain  TEXT    NOT NULL,
            url            TEXT    NOT NULL UNIQUE,
            title          TEXT,
            content        TEXT,
            date_published TEXT,
            date_scraped   TEXT    DEFAULT (date('now')),
            word_count     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sentences (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id      INTEGER REFERENCES articles(id),
            source_domain   TEXT    NOT NULL,
            url             TEXT,
            sentence_text   TEXT    NOT NULL,
            sentence_index  INTEGER DEFAULT 0,
            pipeline_type   TEXT    DEFAULT 'news',
            numeric_density REAL    DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS structured_stats (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source_domain  TEXT,
            url            TEXT,
            year           INTEGER,
            metric         TEXT,
            value          REAL,
            unit           TEXT,
            sentence       TEXT,
            date_extracted TEXT    DEFAULT (date('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT    NOT NULL UNIQUE,
            email        TEXT    NOT NULL UNIQUE,
            password     TEXT    NOT NULL,
            created_at   TEXT    DEFAULT (datetime('now')),
            is_admin     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER REFERENCES users(id),
            token        TEXT    NOT NULL UNIQUE,
            created_at   TEXT    DEFAULT (datetime('now')),
            expires_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS analysis_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT,
            input_text   TEXT,
            result_json  TEXT,
            is_flagged   INTEGER DEFAULT 0,
            created_at   TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id  INTEGER REFERENCES analysis_log(id),
            rating       INTEGER,
            comment      TEXT,
            created_at   TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS shared_results (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            share_token  TEXT    NOT NULL UNIQUE,
            result_json  TEXT    NOT NULL,
            created_at   TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trusted_domains (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            domain       TEXT    NOT NULL UNIQUE,
            tier         INTEGER DEFAULT 3,
            reputation   REAL    DEFAULT 0.75
        );

        CREATE TABLE IF NOT EXISTS system_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            level        TEXT,
            message      TEXT,
            created_at   TEXT    DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


# ── Article helpers ───────────────────────────────────────────────────────────

def insert_article(source_domain: str, url: str, title: str,
                   content: str, date_published: str, word_count: int = 0) -> int:
    conn = get_connection()
    c    = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO articles "
            "(source_domain, url, title, content, date_published, word_count) "
            "VALUES (?,?,?,?,?,?)",
            (source_domain, url, title, content, date_published, word_count),
        )
        conn.commit()
        c.execute("SELECT id FROM articles WHERE url=?", (url,))
        row = c.fetchone()
        return row[0] if row else -1
    finally:
        conn.close()


def article_exists(url: str) -> bool:
    conn = get_connection()
    c    = conn.cursor()
    c.execute("SELECT 1 FROM articles WHERE url=?", (url,))
    result = c.fetchone() is not None
    conn.close()
    return result


# ── Sentence helpers ──────────────────────────────────────────────────────────

def insert_pipeline_sentences(
    article_id:      int,
    source_domain:   str,
    url:             str,
    sentences:       List[str],
    pipeline:        str  = "news",
    numeric_density: float = 0.0,
) -> int:
    """
    Insert sentences into the corpus.

    v3.2: Every sentence passes through _sentence_quality_gate() first.
    Sentences that fail the gate are silently skipped.

    Returns the number of sentences actually inserted.
    """
    conn     = get_connection()
    c        = conn.cursor()
    inserted = 0
    try:
        for i, s in enumerate(sentences):
            s = s.strip()
            if not _sentence_quality_gate(s, pipeline):
                continue
            c.execute(
                "INSERT INTO sentences "
                "(article_id, source_domain, url, sentence_text, "
                " sentence_index, pipeline_type, numeric_density) "
                "VALUES (?,?,?,?,?,?,?)",
                (article_id, source_domain, url, s, i, pipeline, numeric_density),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def get_all_sentences(pipeline: str = None) -> List[dict]:
    conn = get_connection()
    c    = conn.cursor()
    if pipeline:
        c.execute(
            "SELECT s.id, s.sentence_text, s.source_domain, s.url, "
            "s.pipeline_type, s.numeric_density, s.article_id "
            "FROM sentences s WHERE s.pipeline_type=?",
            (pipeline,),
        )
    else:
        c.execute(
            "SELECT s.id, s.sentence_text, s.source_domain, s.url, "
            "COALESCE(s.pipeline_type,'news') AS pipeline_type, "
            "COALESCE(s.numeric_density,0.0) AS numeric_density, "
            "s.article_id "
            "FROM sentences s"
        )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_corpus_stats() -> dict:
    conn  = get_connection()
    c     = conn.cursor()
    stats = {}
    for key, table in [
        ("total_articles",   "articles"),
        ("total_sentences",  "sentences"),
        ("total_users",      "users"),
        ("total_analyses",   "analysis_log"),
        ("total_feedback",   "feedback"),
        ("flagged_analyses", "analysis_log WHERE is_flagged=1"),
        ("total_stats",      "structured_stats"),
    ]:
        try:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            stats[key] = c.fetchone()[0]
        except Exception:
            stats[key] = 0

    c.execute(
        "SELECT pipeline_type, COUNT(*) FROM sentences GROUP BY pipeline_type"
    )
    stats["pipeline_breakdown"] = {r[0]: r[1] for r in c.fetchall()}

    c.execute(
        "SELECT source_domain, COUNT(*) FROM sentences "
        "GROUP BY source_domain ORDER BY COUNT(*) DESC LIMIT 20"
    )
    stats["domain_breakdown"] = {r[0]: r[1] for r in c.fetchall()}

    conn.close()
    return stats
