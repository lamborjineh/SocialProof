"""
SocialProof — Router: Admin / Research Metrics v3.1

New endpoints (this revision):
  POST /admin/corpus/ingest  — save manually curated sentences to corpus.db
  GET  /admin/corpus/stats   — sentence count, sources, pipeline breakdown

New v3 endpoint:
  GET /admin/api-usage          — Fact Check API call counts, cache hit rate, ClaimBuster usage

Existing endpoints:
  GET /admin/research-metrics
  GET /admin/stats
  GET /admin/evaluations
  GET /admin/lessons/impact
  GET /admin/users
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import logger
from database.models import engine
from routers.auth import _verify

# Path to the SQLite corpus database (same location the scraper uses)
_CORPUS_DB = Path(__file__).resolve().parent.parent / "data" / "corpus.db"


class CorpusIngestRequest(BaseModel):
    sentences:     List[str]
    source_domain: str
    source_name:   str
    url:           Optional[str] = ""
    pipeline:      str = "stats"
    reputation:    float = 0.95


router = APIRouter(prefix="/admin")


def _require_admin(authorization: str, request: Request = None):
    """Accept JWT from Authorization: Bearer header OR HttpOnly sp_jwt cookie."""
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    elif request is not None:
        token = request.cookies.get("sp_jwt")
    if not token:
        raise HTTPException(status_code=401, detail="Authorization required.")
    payload = _verify(token)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return payload


# ── GET /admin/research-metrics ───────────────────────────────────────────────
@router.get("/research-metrics")
async def get_research_metrics(request: Request, authorization: str = Header(None)):
    """
    §9 — Thesis research metrics.

    Returns all five measurement goals:
      1. Did users get better?       (accuracy_before, accuracy_after, improvement)
      2. Did they correct themselves? (correction_rate)
      3. Are they confidently correct? (high_confidence_correct_pct, high_confidence_wrong_pct)
      4. Do they understand key skills? (per-skill accuracy: claims, bias, source)
      5. Does your system work?       (system_accuracy vs LIAR/FEVER ground truth)

    Accuracy = label agreement rate (user_label vs system_label),
    NOT numeric score difference. Based on category match:
      Likely Credible / Uncertain / Likely Misleading
    """
    _require_admin(authorization, request)
    db = Session(engine)
    try:

        # ── 1. Did users get better? ──────────────────────────────────────────
        # accuracy_before = user label vs system label on first submission
        # accuracy_after  = revised label vs system label after re-evaluation
        # improvement     = accuracy_after - accuracy_before

        accuracy_before_row = db.execute(sa.text("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN ue.user_label = e.system_label THEN 1 ELSE 0 END) AS correct
            FROM user_evaluations ue
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE ue.user_label IS NOT NULL AND e.system_label IS NOT NULL
        """)).fetchone()

        accuracy_after_row = db.execute(sa.text("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN re.revised_label = e.system_label THEN 1 ELSE 0 END) AS correct
            FROM re_evaluations re
            JOIN user_evaluations ue ON ue.id = re.user_evaluation_id
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE re.revised_label IS NOT NULL AND e.system_label IS NOT NULL
        """)).fetchone()

        total_before  = accuracy_before_row.total   or 0
        correct_before = accuracy_before_row.correct or 0
        total_after   = accuracy_after_row.total    or 0
        correct_after  = accuracy_after_row.correct  or 0

        acc_before = round(correct_before / total_before * 100, 1) if total_before > 0 else None
        acc_after  = round(correct_after  / total_after  * 100, 1) if total_after  > 0 else None
        improvement = round(acc_after - acc_before, 1) if (acc_before is not None and acc_after is not None) else None

        # ── 2. Did they correct themselves? ───────────────────────────────────
        # correction_rate = % of users who had wrong label before but correct after
        correction_row = db.execute(sa.text("""
            SELECT
                COUNT(*) AS re_eval_count,
                SUM(CASE
                    WHEN ue.user_label  != e.system_label
                     AND re.revised_label = e.system_label
                    THEN 1 ELSE 0
                END) AS corrected
            FROM re_evaluations re
            JOIN user_evaluations ue ON ue.id = re.user_evaluation_id
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE re.revised_label IS NOT NULL
        """)).fetchone()

        re_eval_total = correction_row.re_eval_count or 0
        corrected     = correction_row.corrected      or 0
        correction_rate = round(corrected / re_eval_total * 100, 1) if re_eval_total > 0 else None

        # Breakdown by revision_trigger (why did they change their mind?)
        trigger_rows = db.execute(sa.text("""
            SELECT
                COALESCE(revision_trigger, 'not_specified') AS trigger,
                COUNT(*) AS count
            FROM re_evaluations
            GROUP BY revision_trigger
            ORDER BY count DESC
        """)).fetchall()
        correction_by_trigger = [dict(r._mapping) for r in trigger_rows]

        # ── 3. Confidence calibration ─────────────────────────────────────────
        # High confidence + correct vs high confidence + wrong
        confidence_rows = db.execute(sa.text("""
            SELECT
                ue.confidence_level,
                COUNT(*) AS total,
                SUM(CASE WHEN ue.user_label = e.system_label THEN 1 ELSE 0 END) AS correct,
                SUM(CASE WHEN ue.user_label != e.system_label THEN 1 ELSE 0 END) AS wrong
            FROM user_evaluations ue
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE ue.confidence_level IS NOT NULL
              AND ue.user_label IS NOT NULL
              AND e.system_label IS NOT NULL
            GROUP BY ue.confidence_level
        """)).fetchall()

        confidence_calibration = []
        high_conf_correct_pct  = None
        high_conf_wrong_pct    = None

        for row in confidence_rows:
            total   = row.total   or 1
            correct = row.correct or 0
            wrong   = row.wrong   or 0
            entry   = {
                "confidence_level": row.confidence_level,
                "total":            total,
                "correct_pct":      round(correct / total * 100, 1),
                "wrong_pct":        round(wrong   / total * 100, 1),
            }
            confidence_calibration.append(entry)
            if row.confidence_level == "high":
                high_conf_correct_pct = entry["correct_pct"]
                high_conf_wrong_pct   = entry["wrong_pct"]

        # ── 4. Per-skill accuracy ─────────────────────────────────────────────
        # Claim detection: did user identify at least one claim?
        claim_skill_row = db.execute(sa.text("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE
                    WHEN JSON_LENGTH(ue.identified_claims) > 0
                     AND e.system_label != 'Incomplete Analysis'
                    THEN 1 ELSE 0
                END) AS detected
            FROM user_evaluations ue
            JOIN evaluations e ON e.id = ue.evaluation_id
        """)).fetchone()

        # Bias detection: did user flag bias when system detected high bias?
        bias_skill_row = db.execute(sa.text("""
            SELECT
                COUNT(*) AS total_high_bias,
                SUM(CASE WHEN ue.bias_detected = 1 THEN 1 ELSE 0 END) AS user_detected
            FROM user_evaluations ue
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE e.analysis_json IS NOT NULL
              AND JSON_EXTRACT(e.analysis_json, '$.bias_score') > 0.45
        """)).fetchone()

        # Source evaluation: did user correctly identify low-credibility sources?
        source_skill_row = db.execute(sa.text("""
            SELECT
                COUNT(*) AS total_low_cred,
                SUM(CASE WHEN ue.source_credible = 'no' THEN 1 ELSE 0 END) AS user_flagged
            FROM user_evaluations ue
            JOIN evaluations e ON e.id = ue.evaluation_id
            WHERE e.analysis_json IS NOT NULL
              AND JSON_EXTRACT(e.analysis_json, '$.source_score') < 0.45
        """)).fetchone()

        cs_total    = claim_skill_row.total    or 1
        bs_total    = bias_skill_row.total_high_bias  or 0
        ss_total    = source_skill_row.total_low_cred or 0

        skill_accuracy = {
            "claim_detection": {
                "total":    cs_total,
                "detected": claim_skill_row.detected or 0,
                "accuracy_pct": round((claim_skill_row.detected or 0) / cs_total * 100, 1),
            },
            "bias_detection": {
                "total_high_bias_cases": bs_total,
                "user_detected":         bias_skill_row.user_detected or 0,
                "accuracy_pct": round((bias_skill_row.user_detected or 0) / bs_total * 100, 1)
                                 if bs_total > 0 else None,
            },
            "source_evaluation": {
                "total_low_cred_cases": ss_total,
                "user_flagged":         source_skill_row.user_flagged or 0,
                "accuracy_pct": round((source_skill_row.user_flagged or 0) / ss_total * 100, 1)
                                 if ss_total > 0 else None,
            },
        }

        # ── 5. System accuracy vs LIAR / FEVER ground truth ───────────────────
        # Ground truth is stored in corpus.db (SQLite), not MySQL.
        # We query it directly using the corpus DB connection.
        system_accuracy = _compute_system_accuracy()

        return {
            "meta": {
                "note": (
                    "Accuracy = label agreement (Likely Credible / Uncertain / Likely Misleading). "
                    "NOT numeric score difference. "
                    "Source: Korfiatis et al. (2012) for score_diff_label scale."
                ),
            },
            # §9 Goal 1
            "accuracy_before":    acc_before,
            "accuracy_after":     acc_after,
            "improvement":        improvement,
            "total_evaluated":    total_before,
            "total_re_evaluated": total_after,
            # §9 Goal 2
            "correction_rate":        correction_rate,
            "corrections_total":      corrected,
            "correction_by_trigger":  correction_by_trigger,
            # §9 Goal 3
            "high_confidence_correct_pct": high_conf_correct_pct,
            "high_confidence_wrong_pct":   high_conf_wrong_pct,
            "confidence_calibration":      confidence_calibration,
            # §9 Goal 4
            "skill_accuracy": skill_accuracy,
            # §9 Goal 5
            "system_accuracy": system_accuracy,
        }
    finally:
        db.close()


def _compute_system_accuracy() -> dict:
    """
    §9 Goal 5 — System accuracy against LIAR/FEVER ground truth.

    Label mapping (dataset → system):
      LIAR:  true/mostly-true     → Likely Credible
             half-true/barely-true → Uncertain
             false/pants-fire      → Likely Misleading
      FEVER: SUPPORTS             → Likely Credible
             REFUTES              → Likely Misleading
             NOT ENOUGH INFO      → Uncertain

    Only evaluations where the raw_content matches a known dataset claim
    are included. This requires the analysis pipeline to have been run on
    LIAR/FEVER content — run corpus/evaluate_system.py to populate these.

    Falls back gracefully if corpus.db is unreachable.
    """
    try:
        import sqlite3
        from pathlib import Path

        db_path = Path(__file__).parent.parent / "data" / "corpus.db"
        if not db_path.exists():
            return {"error": "corpus.db not found", "total": 0}

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Check if system_predictions table exists (populated by evaluate_system.py)
        c.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='system_predictions'
        """)
        if not c.fetchone():
            conn.close()
            return {
                "error": "system_predictions table not found. "
                         "Run: python corpus/evaluate_system.py",
                "total": 0,
            }

        c.execute("""
            SELECT
                COUNT(*)                                                         AS total,
                SUM(CASE WHEN predicted_label = ground_truth_label THEN 1 END)  AS correct,
                dataset
            FROM system_predictions
            GROUP BY dataset
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()

        overall_total   = sum(r["total"]   for r in rows)
        overall_correct = sum(r["correct"] or 0 for r in rows)

        return {
            "overall_accuracy_pct": round(overall_correct / overall_total * 100, 1)
                                     if overall_total > 0 else None,
            "total":   overall_total,
            "correct": overall_correct,
            "by_dataset": rows,
            "note": (
                "Run python corpus/evaluate_system.py to populate ground-truth predictions. "
                "Uses LIAR + FEVER datasets already in corpus.db."
            ),
        }

    except Exception as e:
        return {"error": str(e), "total": 0}


# ── GET /admin/stats ──────────────────────────────────────────────────────────
@router.get("/stats")
async def get_admin_stats(request: Request, authorization: str = Header(None)):
    """Aggregate research metrics across all users."""
    _require_admin(authorization, request)
    db = Session(engine)
    try:
        overview = db.execute(sa.text("""
            SELECT
                COUNT(DISTINCT e.id)                                      AS total_evaluations,
                COUNT(DISTINCT e.user_id)                                 AS total_users,
                COUNT(DISTINCT CASE WHEN e.user_id IS NULL THEN e.session_token END)
                                                                          AS anonymous_sessions,
                ROUND(AVG(e.system_score), 1)                            AS avg_system_score,
                ROUND(AVG(ue.user_score), 1)                             AS avg_user_score,
                ROUND(AVG(ABS(ue.user_score - e.system_score)), 1)       AS avg_abs_score_diff,
                ROUND(AVG(CASE WHEN ue.user_label = e.system_label THEN 1.0 ELSE 0.0 END) * 100, 1)
                                                                          AS label_accuracy_pct,
                COUNT(DISTINCT ue.id)                                     AS total_user_evaluations,
                COUNT(DISTINCT re.id)                                     AS total_re_evaluations,
                ROUND(AVG(CASE WHEN re.revised_score IS NOT NULL
                    THEN re.revised_score - ue.user_score END), 1)       AS avg_score_shift,
                COUNT(DISTINCT lt.id)                                     AS total_lesson_triggers,
                ROUND(AVG(lt.was_read) * 100, 1)                         AS lesson_read_rate_pct
            FROM evaluations e
            LEFT JOIN user_evaluations  ue ON ue.evaluation_id     = e.id
            LEFT JOIN re_evaluations    re ON re.user_evaluation_id = ue.id
            LEFT JOIN lessons_triggered lt ON lt.user_evaluation_id = ue.id
        """)).fetchone()

        confidence_dist = db.execute(sa.text("""
            SELECT confidence_level, COUNT(*) AS count
            FROM user_evaluations
            WHERE confidence_level IS NOT NULL
            GROUP BY confidence_level ORDER BY count DESC
        """)).fetchall()

        label_dist = db.execute(sa.text("""
            SELECT system_label, COUNT(*) AS count
            FROM evaluations
            WHERE system_label IS NOT NULL
            GROUP BY system_label ORDER BY count DESC
        """)).fetchall()

        skipped_steps_raw = db.execute(sa.text("""
            SELECT skipped_steps FROM user_evaluations
            WHERE skipped_steps IS NOT NULL AND skipped_steps != 'null'
        """)).fetchall()

        step_counts: dict = {}
        for row in skipped_steps_raw:
            try:
                steps = json.loads(row.skipped_steps) if isinstance(row.skipped_steps, str) else (row.skipped_steps or [])
                for s in steps:
                    step_counts[s] = step_counts.get(s, 0) + 1
            except Exception:
                pass

        return {
            "overview":        dict(overview._mapping) if overview else {},
            "confidence_dist": [dict(r._mapping) for r in confidence_dist],
            "label_dist":      [dict(r._mapping) for r in label_dist],
            "skipped_steps":   step_counts,
        }
    finally:
        db.close()


# ── GET /admin/evaluations ────────────────────────────────────────────────────
@router.get("/evaluations")
async def list_all_evaluations(
    page: int = 1, per_page: int = 20, request: Request = None, authorization: str = Header(None),
):
    _require_admin(authorization, request)
    db = Session(engine)
    try:
        offset = (page - 1) * per_page
        rows = db.execute(sa.text("""
            SELECT
                e.id, e.system_score, e.system_label,
                SUBSTRING(e.raw_content, 1, 150) AS content_preview,
                e.input_type, e.status, e.created_at,
                e.user_id, e.session_token,
                ue.user_score, ue.user_label, ue.confidence_level,
                ue.bias_detected, ue.evidence_assessed,
                JSON_LENGTH(ue.skipped_steps) AS steps_skipped,
                re.revised_score, re.revision_trigger
            FROM evaluations e
            LEFT JOIN user_evaluations  ue ON ue.evaluation_id     = e.id
            LEFT JOIN re_evaluations    re ON re.user_evaluation_id = ue.id
            ORDER BY e.created_at DESC
            LIMIT :lim OFFSET :off
        """), {"lim": per_page, "off": offset}).fetchall()

        total = db.execute(sa.text("SELECT COUNT(*) FROM evaluations")).scalar()

        return {
            "page":        page,
            "per_page":    per_page,
            "total":       total,
            "total_pages": -(-total // per_page),
            "data":        [dict(r._mapping) for r in rows],
        }
    finally:
        db.close()


# ── GET /admin/lessons/impact ─────────────────────────────────────────────────
@router.get("/lessons/impact")
async def get_lesson_impact(request: Request, authorization: str = Header(None)):
    _require_admin(authorization, request)
    db = Session(engine)
    try:
        rows = db.execute(sa.text("""
            SELECT
                l.lesson_key, l.title, l.topic,
                COUNT(lt.id)                        AS trigger_count,
                SUM(lt.was_read)                    AS read_count,
                ROUND(AVG(lt.was_read) * 100, 1)    AS read_rate_pct,
                ROUND(AVG(
                    CASE WHEN re.revised_score IS NOT NULL
                    THEN re.revised_score - ue.user_score END
                ), 1)                               AS avg_score_shift_after
            FROM lessons l
            LEFT JOIN lessons_triggered lt ON lt.lesson_id          = l.id
            LEFT JOIN user_evaluations  ue ON ue.id                 = lt.user_evaluation_id
            LEFT JOIN re_evaluations    re ON re.user_evaluation_id = ue.id
            GROUP BY l.id, l.lesson_key, l.title, l.topic
            ORDER BY trigger_count DESC
        """)).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


# ── GET /admin/users ──────────────────────────────────────────────────────────
@router.get("/users")
async def list_users(request: Request, authorization: str = Header(None)):
    _require_admin(authorization, request)
    db = Session(engine)
    try:
        rows = db.execute(sa.text("""
            SELECT
                u.id, u.username, u.email, u.role, u.created_at,
                COUNT(DISTINCT e.id) AS eval_count,
                ROUND(AVG(CASE WHEN ue.user_label = e.system_label THEN 1.0 ELSE 0.0 END) * 100, 1)
                                     AS label_accuracy_pct,
                ROUND(AVG(ABS(ue.user_score - e.system_score)), 1) AS avg_score_diff
            FROM users u
            LEFT JOIN evaluations       e  ON e.user_id       = u.id
            LEFT JOIN user_evaluations  ue ON ue.evaluation_id = e.id
            GROUP BY u.id, u.username, u.email, u.role, u.created_at
            ORDER BY eval_count DESC
        """)).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


# ── POST /admin/corpus/ingest ─────────────────────────────────────────────────
@router.post("/corpus/ingest")
async def corpus_ingest(req: CorpusIngestRequest,
                        request: Request, authorization: str = Header(None)):
    """
    Save manually curated sentences from the admin Corpus Builder UI
    directly into corpus.db (SQLite).

    The table schema mirrors what scraper.py writes:
        CREATE TABLE IF NOT EXISTS sentences (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            text          TEXT NOT NULL,
            source_domain TEXT,
            source_name   TEXT,
            url           TEXT,
            pipeline      TEXT,
            reputation    REAL,
            date_added    TEXT
        )

    If the table does not exist yet it is created here.
    Duplicate text (exact match) is silently skipped — idempotent.
    """
    _require_admin(authorization, request)

    if not req.sentences:
        raise HTTPException(status_code=422, detail="No sentences provided.")

    # Clamp reputation
    rep = max(0.0, min(1.0, req.reputation))
    now = datetime.utcnow().strftime("%Y-%m-%d")

    if not _CORPUS_DB.exists():
        # Corpus DB hasn't been initialised yet — create parent dir + file
        _CORPUS_DB.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[CorpusIngest] Creating new corpus.db at {_CORPUS_DB}")

    try:
        con = sqlite3.connect(str(_CORPUS_DB))
        cur = con.cursor()

        # Ensure table exists (safe to run even if already present)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sentences (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                text          TEXT    NOT NULL,
                source_domain TEXT,
                source_name   TEXT,
                url           TEXT,
                pipeline      TEXT,
                reputation    REAL,
                date_added    TEXT
            )
        """)

        inserted = 0
        skipped  = 0
        for text in req.sentences:
            text = text.strip()
            if not text:
                continue
            # Skip exact duplicates
            exists = cur.execute(
                "SELECT 1 FROM sentences WHERE text = ? LIMIT 1", (text,)
            ).fetchone()
            if exists:
                skipped += 1
                continue
            cur.execute(
                "INSERT INTO sentences (text, source_domain, source_name, url, pipeline, reputation, date_added) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (text, req.source_domain, req.source_name,
                 req.url or "", req.pipeline, rep, now),
            )
            inserted += 1

        con.commit()
        con.close()

        logger.info(
            f"[CorpusIngest] {inserted} inserted, {skipped} skipped "
            f"(source={req.source_domain}, pipeline={req.pipeline})"
        )
        return {"inserted": inserted, "skipped": skipped,
                "source_domain": req.source_domain}

    except Exception as exc:
        logger.error(f"[CorpusIngest] DB error: {exc}")
        raise HTTPException(status_code=500, detail=f"corpus.db write failed: {exc}")


# ── GET /admin/corpus/stats ───────────────────────────────────────────────────
@router.get("/corpus/stats")
async def corpus_stats(request: Request, authorization: str = Header(None)):
    """
    Return a quick summary of what is in corpus.db:
      total_sentences, sources (count), pipeline breakdown string.
    Returns zeros gracefully if corpus.db does not exist yet.
    """
    _require_admin(authorization, request)

    if not _CORPUS_DB.exists():
        return {"total_sentences": 0, "sources": 0, "pipelines": ""}

    try:
        con = sqlite3.connect(str(_CORPUS_DB))
        cur = con.cursor()

        total = cur.execute("SELECT COUNT(*) FROM sentences").fetchone()[0]
        sources = cur.execute(
            "SELECT COUNT(DISTINCT source_domain) FROM sentences"
        ).fetchone()[0]
        pip_rows = cur.execute(
            "SELECT pipeline, COUNT(*) AS n FROM sentences GROUP BY pipeline ORDER BY n DESC"
        ).fetchall()
        con.close()

        pip_str = " · ".join(f"{p}:{n}" for p, n in pip_rows) if pip_rows else ""
        return {"total_sentences": total, "sources": sources, "pipelines": pip_str}

    except Exception as exc:
        logger.warning(f"[CorpusStats] {exc}")
        return {"total_sentences": 0, "sources": 0, "pipelines": ""}


# ── GET /admin/api-usage ──────────────────────────────────────────────────────

@router.get("/api-usage")
async def get_api_usage(request: Request, authorization: str = Header(None)):
    """
    v3 — Returns usage statistics for the external API layer:
      - Google Fact Check Tools API: total calls, cache hits, cache hit rate
      - ClaimBuster API: total calls made (inferred from check_worthiness values)
      - MBFC domain coverage: total domains cached, last sync time
    """
    _require_admin(authorization, request)
    db = Session(engine)
    try:
        # ── Google Fact Check cache stats ─────────────────────────────────────
        factcheck_stats = {"total_cached": 0, "expired": 0, "active": 0, "error": None}
        try:
            fc_row = db.execute(sa.text("""
                SELECT
                    COUNT(*)                                              AS total_cached,
                    SUM(CASE WHEN expires_at < NOW() THEN 1 ELSE 0 END)  AS expired,
                    SUM(CASE WHEN expires_at >= NOW() THEN 1 ELSE 0 END) AS active
                FROM factcheck_cache
            """)).fetchone()
            if fc_row:
                factcheck_stats = {
                    "total_cached": fc_row.total_cached or 0,
                    "expired":      fc_row.expired      or 0,
                    "active":       fc_row.active       or 0,
                    "note":         "Each row = 1 unique claim queried. Active = within TTL window.",
                }
        except Exception as e:
            factcheck_stats["error"] = f"factcheck_cache table unavailable: {e}"

        # ── ClaimBuster usage — inferred from claim check_worthiness values ───
        claimbuster_stats = {"total_scored": 0, "error": None}
        try:
            cb_row = db.execute(sa.text("""
                SELECT COUNT(*) AS scored
                FROM claims
                WHERE check_worthiness IS NOT NULL
            """)).fetchone()
            claimbuster_stats = {
                "total_scored": cb_row.scored if cb_row else 0,
                "note": "Claims with a check_worthiness score (populated by ClaimBuster API).",
            }
        except Exception as e:
            claimbuster_stats["error"] = f"claims table check_worthiness column unavailable: {e}"

        # ── MBFC domain coverage ──────────────────────────────────────────────
        mbfc_stats = {"total_domains": 0, "last_synced": None, "error": None}
        try:
            mbfc_row = db.execute(sa.text("""
                SELECT COUNT(*) AS total, MAX(last_synced) AS last_synced
                FROM mbfc_domains
            """)).fetchone()
            mbfc_stats = {
                "total_domains": mbfc_row.total       if mbfc_row else 0,
                "last_synced":   str(mbfc_row.last_synced) if mbfc_row and mbfc_row.last_synced else None,
                "note":          "Run scripts/sync_mbfc.py to refresh. Recommended: monthly.",
            }
        except Exception as e:
            mbfc_stats["error"] = f"mbfc_domains table unavailable: {e}"

        return {
            "google_factcheck_api": factcheck_stats,
            "claimbuster_api":      claimbuster_stats,
            "mbfc_coverage":        mbfc_stats,
        }

    finally:
        db.close()
