"""
SocialProof — SQLAlchemy ORM Models v3.0
Mirrors the live socialproof_db schema exactly (all 15 tables).

v3.0 Changes vs v2:
  - Added MBFCDomainORM    → mbfc_domains table (MBFC/iffy.news domain credibility cache)
  - Added PhpInputLogORM   → php_input_log table (was in DB but missing from ORM)
  - Added FactcheckCacheORM (factcheck_cache) — caches Google Fact Check API results
    (24hr TTL per FACTCHECK_CACHE_TTL_HOURS). Prevents burning free 100/day quota
    on duplicate claim lookups. Note: a coworker flagged this as non-essential;
    discussed and reinstated — the cache is for rate-limit protection, not
    fact-checking. Source node would exhaust the free quota in hours without it.
  - init_mysql_schema() extended with mbfc_domains CREATE TABLE
  - All existing 13 tables (claims, evaluations, evidence, lesson_completions,
    lessons, lessons_triggered, pretest_results, quiz_attempts, quiz_questions,
    re_evaluations, user_evaluations, user_skill_progress, users) unchanged
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    Column, Integer, String, Text, Float,
    JSON, DateTime, SmallInteger,
    Enum as SAEnum,
    create_engine,
)
from sqlalchemy.orm import declarative_base

from config import DATABASE_URL, logger

Base   = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)


# ── Existing tables (unchanged) ───────────────────────────────────────────────

class UserORM(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(50),  nullable=False, unique=True)
    email         = Column(String(150), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    role          = Column(SAEnum("user", "admin", create_constraint=False, native_enum=False), nullable=False, default="user")
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EvaluationORM(Base):
    __tablename__ = "evaluations"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, nullable=True)
    session_token = Column(String(64), nullable=False)
    input_type    = Column(SAEnum("text", "url", "image", "pdf", create_constraint=False, native_enum=False), default="text")
    raw_content   = Column(Text, nullable=False)
    parsed_text   = Column(Text, nullable=True)
    system_label  = Column(String(30), nullable=True)
    analysis_json = Column(JSON, nullable=True)
    status        = Column(SAEnum("pending", "analyzed", "complete", create_constraint=False, native_enum=False), default="pending")
    created_at    = Column(DateTime, default=datetime.utcnow)


class ClaimORM(Base):
    __tablename__  = "claims"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    evaluation_id  = Column(Integer, nullable=False)
    claim_text     = Column(Text, nullable=False)
    sentence_index = Column(Integer, nullable=True)
    label          = Column(String(20), default="unverified")
    confidence     = Column(Float, nullable=True)


class EvidenceORM(Base):
    __tablename__    = "evidence"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    claim_id         = Column(Integer, nullable=False)
    evidence_text    = Column(Text, nullable=False)
    type             = Column(String(12), nullable=False)
    source_url       = Column(String(2083), nullable=True)
    source_label     = Column(String(255), nullable=True)
    similarity_score = Column(Float, nullable=True)
    retrieved_at     = Column(DateTime, default=datetime.utcnow)


class UserEvaluationORM(Base):
    __tablename__     = "user_evaluations"
    id                = Column(Integer, primary_key=True, autoincrement=True)
    evaluation_id     = Column(Integer, nullable=False)
    user_id           = Column(Integer, nullable=True)
    identified_claims = Column(JSON, nullable=True)
    source_credible   = Column(SAEnum("yes", "no", "unsure", create_constraint=False, native_enum=False), nullable=True)
    bias_detected     = Column(SmallInteger, nullable=True)
    evidence_assessed = Column(SmallInteger, nullable=True)
    user_score        = Column(Integer, nullable=True)
    user_label        = Column(String(30), nullable=True)
    confidence_level  = Column(SAEnum("low", "medium", "high", create_constraint=False, native_enum=False), nullable=True)
    skipped_steps     = Column(JSON, nullable=True)
    time_spent_seconds = Column(Integer, nullable=True)
    submitted_at      = Column(DateTime, default=datetime.utcnow)


class ReEvaluationORM(Base):
    __tablename__      = "re_evaluations"
    id                 = Column(Integer, primary_key=True, autoincrement=True)
    user_evaluation_id = Column(Integer, nullable=False)
    revised_score      = Column(Integer, nullable=True)
    revised_label      = Column(String(30), nullable=True)
    revised_confidence = Column(SAEnum("low", "medium", "high", create_constraint=False, native_enum=False), nullable=True)
    revision_notes     = Column(Text, nullable=True)
    revision_trigger   = Column(String(50), nullable=True)
    revised_at         = Column(DateTime, default=datetime.utcnow)


class LessonORM(Base):
    __tablename__ = "lessons"
    id                     = Column(Integer, primary_key=True, autoincrement=True)
    lesson_key             = Column(String(100), nullable=False, unique=True)
    title                  = Column(String(255), nullable=False)
    content                = Column(Text, nullable=False)
    topic                  = Column(
        SAEnum("claim_detection", "source_verification", "bias_detection",
               "evidence_evaluation", "general", create_constraint=False, native_enum=False),
        nullable=False,
    )
    difficulty             = Column(
        SAEnum("beginner", "intermediate", "advanced", create_constraint=False, native_enum=False),
        nullable=False, default="beginner",
    )
    mil_skill              = Column(String(50),  nullable=True)
    sort_order             = Column(Integer,      nullable=True)
    prerequisite_lesson_id = Column(Integer,      nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)


class LessonsTriggeredORM(Base):
    __tablename__      = "lessons_triggered"
    id                 = Column(Integer, primary_key=True, autoincrement=True)
    user_evaluation_id = Column(Integer, nullable=False)
    lesson_id          = Column(Integer, nullable=False)
    trigger_reason     = Column(String(255), nullable=True)
    was_read           = Column(SmallInteger, nullable=False, default=0)
    triggered_at       = Column(DateTime, default=datetime.utcnow)


class QuizQuestionORM(Base):
    __tablename__ = "quiz_questions"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    lesson_id     = Column(Integer, nullable=True)
    question_text = Column(Text, nullable=False)
    options       = Column(JSON, nullable=False)
    correct_index = Column(Integer, nullable=False)
    explanation   = Column(Text, nullable=True)
    topic         = Column(String(40), nullable=False)
    difficulty    = Column(
        SAEnum("beginner", "intermediate", "advanced", create_constraint=False, native_enum=False),
        nullable=True, default="beginner",
    )


class QuizAttemptORM(Base):
    __tablename__  = "quiz_attempts"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    user_id        = Column(Integer, nullable=True)
    question_id    = Column(Integer, nullable=False)
    selected_index = Column(Integer, nullable=False)
    is_correct     = Column(SmallInteger, nullable=False)
    attempted_at   = Column(DateTime, default=datetime.utcnow)


class UserSkillProgressORM(Base):
    """Per-user MIL skill level per topic. Updated after quiz attempts and lesson reads."""
    __tablename__ = "user_skill_progress"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    user_id          = Column(Integer, nullable=True)
    session_token    = Column(String(64), nullable=True)
    topic            = Column(
        SAEnum("claim_detection", "source_verification", "bias_detection",
               "evidence_evaluation", "general", create_constraint=False, native_enum=False),
        nullable=False,
    )
    current_level    = Column(
        SAEnum("beginner", "intermediate", "advanced", create_constraint=False, native_enum=False),
        nullable=False, default="beginner",
    )
    quiz_accuracy_pct = Column(Float, nullable=True)
    lessons_completed = Column(Integer, nullable=False, default=0)
    last_updated      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PretestResultORM(Base):
    """Pre/post test results. Replaces negative-ID hack in quiz_attempts."""
    __tablename__ = "pretest_results"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, nullable=True)
    session_token = Column(String(64), nullable=True)
    phase         = Column(SAEnum("pretest", "posttest", create_constraint=False, native_enum=False), nullable=False)
    score_pct     = Column(Integer, nullable=False)
    correct       = Column(Integer, nullable=False)
    total         = Column(Integer, nullable=False)
    submitted_at  = Column(DateTime, default=datetime.utcnow)


class LessonCompletionORM(Base):
    """Per-user lesson completion tracking."""
    __tablename__  = "lesson_completions"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    user_id        = Column(Integer, nullable=True)
    session_token  = Column(String(64), nullable=True)
    lesson_id      = Column(Integer, nullable=False)
    completed_at   = Column(DateTime, default=datetime.utcnow)


class PhpInputLogORM(Base):
    """
    PHP frontend input log — created by the frontend before FastAPI is called.
    evaluation_id is backfilled once FastAPI responds.
    This table is owned by the PHP layer; Python only reads it for admin/debug.
    """
    __tablename__ = "php_input_log"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    session_token = Column(String(64), nullable=False)
    input_type    = Column(SAEnum("text", "url", "image", "pdf", create_constraint=False, native_enum=False), nullable=False, default="text")
    raw_content   = Column(Text, nullable=False)
    user_id       = Column(Integer, nullable=True)
    evaluation_id = Column(Integer, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


# ── v3 New tables ─────────────────────────────────────────────────────────────

class FactcheckCacheORM(Base):
    """
    Caches Google Fact Check Tools API results to avoid re-querying
    for identical claims and to stay within the free 100/day quota.
    TTL is controlled by FACTCHECK_CACHE_TTL_HOURS (default: 24h).
    """
    __tablename__ = "factcheck_cache"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    claim_hash   = Column(String(64),  nullable=False, unique=True)  # SHA256 of normalised claim
    claim_text   = Column(Text,        nullable=False)
    results_json = Column(Text,        nullable=True)                 # JSON array of {publisher, url, rating}
    queried_at   = Column(DateTime,    default=datetime.utcnow)
    expires_at   = Column(DateTime,    nullable=True)


class MBFCDomainORM(Base):
    """
    MBFC / iffy.news domain credibility cache.
    Populated by scripts/sync_mbfc.py (monthly).
    Looked up in pipeline/source_credibility.py by domain extracted from a URL.
    Used as a non-judgmental signal in the Source node — never shown as a verdict.
    """
    __tablename__ = "mbfc_domains"
    id               = Column(Integer, primary_key=True, autoincrement=True)
    domain           = Column(String(255), nullable=False, unique=True)
    factual_reporting = Column(String(50), nullable=True)   # HIGH / MOSTLY_FACTUAL / MIXED / LOW / VERY_LOW
    bias_rating      = Column(String(50), nullable=True)    # LEFT-CENTER / CENTER / RIGHT / etc.
    credibility_rating = Column(String(50), nullable=True)
    country          = Column(String(10), nullable=True)
    notes_url        = Column(String(500), nullable=True)
    last_synced      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Auto-migration on startup ──────────────────────────────────────────────────

def init_mysql_schema():
    """
    Run safe migrations on startup. Every statement is idempotent.
    - PostgreSQL (Neon): uses Base.metadata.create_all() — dialect-safe, handles
      all 15 tables automatically via the ORM models defined above.
    - MySQL: uses the original raw DDL path (CREATE TABLE IF NOT EXISTS + ALTER TABLE).
    Column additions check information_schema before running ALTER TABLE.
    New tables use CREATE TABLE IF NOT EXISTS.
    """
    is_postgres = DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")

    if is_postgres:
        # ── PostgreSQL (Neon) — use SQLAlchemy ORM create_all ─────────────────
        # create_all() is idempotent (checkfirst=True by default).
        # It creates every table defined as a Base subclass above.
        # SAEnum columns: SQLAlchemy creates native PostgreSQL ENUM types.
        # We pass create_constraint=False so they behave like VARCHAR
        # and don't require separate CREATE TYPE statements.
        try:
            logger.info("[Migration] PostgreSQL detected — running Base.metadata.create_all()")
            Base.metadata.create_all(engine)
            logger.info("[Migration] All tables created/verified via ORM (PostgreSQL).")
        except Exception as e:
            logger.error(f"[Migration] create_all() failed: {e}")
            raise
        return  # Skip MySQL-specific DDL below

    # ── New tables ────────────────────────────────────────────────────────────
    new_tables_sql = [
        # v2 tables that were created via migration but need to exist
        """CREATE TABLE IF NOT EXISTS user_skill_progress (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NULL,
            session_token VARCHAR(64) NULL,
            topic ENUM('claim_detection','source_verification','bias_detection',
                       'evidence_evaluation','general') NOT NULL,
            current_level ENUM('beginner','intermediate','advanced') NOT NULL DEFAULT 'beginner',
            quiz_accuracy_pct FLOAT NULL,
            lessons_completed INT NOT NULL DEFAULT 0,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_usp_user (user_id),
            INDEX idx_usp_topic (topic)
        )""",
        """CREATE TABLE IF NOT EXISTS pretest_results (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NULL,
            session_token VARCHAR(64) NULL,
            phase ENUM('pretest','posttest') NOT NULL,
            score_pct INT NOT NULL,
            correct INT NOT NULL,
            total INT NOT NULL,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_pr_user (user_id),
            INDEX idx_pr_session (session_token)
        )""",
        """CREATE TABLE IF NOT EXISTS lesson_completions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NULL,
            session_token VARCHAR(64) NULL,
            lesson_id INT NOT NULL,
            completed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_lc_user (user_id),
            INDEX idx_lc_lesson (lesson_id)
        )""",
        # v3 new
        """CREATE TABLE IF NOT EXISTS mbfc_domains (
            id INT AUTO_INCREMENT PRIMARY KEY,
            domain VARCHAR(255) NOT NULL UNIQUE,
            factual_reporting VARCHAR(50) NULL,
            bias_rating VARCHAR(50) NULL,
            credibility_rating VARCHAR(50) NULL,
            country VARCHAR(10) NULL,
            notes_url VARCHAR(500) NULL,
            last_synced DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_mbfc_domain (domain)
        )""",
        """CREATE TABLE IF NOT EXISTS factcheck_cache (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            claim_hash   VARCHAR(64)  NOT NULL UNIQUE,
            claim_text   TEXT         NOT NULL,
            results_json TEXT         NULL,
            queried_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
            expires_at   DATETIME     NULL,
            INDEX idx_fc_hash (claim_hash)
        )""",
    ]

    with engine.connect() as conn:
        for sql in new_tables_sql:
            try:
                conn.execute(sa.text(sql))
                conn.commit()
            except Exception as e:
                logger.warning(f"[Migration] Table creation skipped: {e}")

    # ── Column additions ──────────────────────────────────────────────────────
    column_migrations = [
        (
            "re_evaluations", "revision_trigger",
            "ALTER TABLE re_evaluations ADD COLUMN revision_trigger VARCHAR(50) NULL"
        ),
        (
            "lessons", "mil_skill",
            "ALTER TABLE lessons ADD COLUMN mil_skill VARCHAR(50) NULL"
        ),
        (
            "lessons", "sort_order",
            "ALTER TABLE lessons ADD COLUMN sort_order INT NULL"
        ),
        (
            "lessons", "prerequisite_lesson_id",
            "ALTER TABLE lessons ADD COLUMN prerequisite_lesson_id INT NULL"
        ),
        (
            "quiz_questions", "difficulty",
            "ALTER TABLE quiz_questions ADD COLUMN difficulty "
            "ENUM('beginner','intermediate','advanced') NULL DEFAULT 'beginner'"
        ),
        (
            "user_evaluations", "time_spent_seconds",
            "ALTER TABLE user_evaluations ADD COLUMN time_spent_seconds INT NULL"
        ),
    ]

    with engine.connect() as conn:
        for table, column, sql in column_migrations:
            try:
                result = conn.execute(sa.text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    f"AND table_name = '{table}' AND column_name = '{column}'"
                ))
                if result.scalar() == 0:
                    conn.execute(sa.text(sql))
                    conn.commit()
                    logger.info(f"[Migration] Added column '{column}' to '{table}'")
            except Exception as e:
                logger.warning(f"[Migration] {table}.{column}: {e}")

    # ── Enum column expansions ─────────────────────────────────────────────────
    # MySQL ENUM columns require ALTER TABLE MODIFY to add new values.
    # Safe to re-run — MySQL no-ops if the column already has the target definition.
    enum_migrations = [
        """ALTER TABLE evaluations
           MODIFY COLUMN input_type ENUM('text','url','image','pdf') NOT NULL DEFAULT 'text'""",
        """ALTER TABLE php_input_log
           MODIFY COLUMN input_type ENUM('text','url','image','pdf') NOT NULL DEFAULT 'text'""",
    ]

    with engine.connect() as conn:
        for sql in enum_migrations:
            try:
                conn.execute(sa.text(sql))
                conn.commit()
            except Exception as e:
                logger.warning(f"[Migration] Enum expand skipped: {e}")
