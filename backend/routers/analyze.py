import re
import os
import asyncio
import base64
import functools
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends, Request, Header

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config import logger
from database.models import engine, EvaluationORM, ClaimORM
from pipeline import AnalysisPipeline
from pipeline.preprocessing import PreprocessingModule
from pipeline.evidence_retrieval import get_unverified_log
from pipeline.pdf_input import extract_text_from_pdf
import json
import urllib.request

from schemas import (
    AnalyzeRequest, AnalyzeResponse, UserClaimRequest, ValidateClaimRequest,
    ClaimResult, EvidenceResult, AnnotationSegment,
    SourceStepResponse, FactCheckResult, MBFCRating,
)
from config import OLLAMA_URL, OLLAMA_MODEL
from routers.auth import get_current_user

router    = APIRouter()
_pipeline = AnalysisPipeline()

# H-6: configurable worker count; default cpu_count * 2, min 2
_PIPELINE_WORKERS = int(os.environ.get("PIPELINE_WORKERS", max(2, (os.cpu_count() or 2) * 2)))
_executor = ThreadPoolExecutor(max_workers=_PIPELINE_WORKERS)

# H-6: overall pipeline timeout in seconds (configurable)
_PIPELINE_TIMEOUT = float(os.environ.get("PIPELINE_TIMEOUT_SECONDS", "120"))

# C-2 / H-5: session token format
_SESSION_TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")


# ── POST /analyze ─────────────────────────────────────────────────────────────
@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest, background_tasks: BackgroundTasks):
    """
    Main analysis endpoint.
    Supports input_type: text | url | image.
    Requires a valid session_token (from GET /auth/session).
    """
    # C-2 / H-5: Hard-reject missing or malformed session_token
    if not request.session_token or not _SESSION_TOKEN_RE.match(request.session_token):
        raise HTTPException(
            status_code=422,
            detail=(
                "A valid session_token is required. "
                "Obtain one from GET /auth/session before calling this endpoint."
            ),
        )

    # ── Input validation ──────────────────────────────────────────────────────
    if request.input_type == "image":
        if not request.image_data:
            raise HTTPException(
                status_code=422,
                detail="image_data is required when input_type='image'. Send a base64-encoded string."
            )
        try:
            request_image_bytes = base64.b64decode(request.image_data)
        except Exception:
            raise HTTPException(status_code=422, detail="image_data is not valid base64.")

    elif request.input_type == "pdf":
        if not request.pdf_data:
            raise HTTPException(
                status_code=422,
                detail="pdf_data is required when input_type='pdf'. Send a base64-encoded PDF."
            )
        try:
            pdf_bytes = base64.b64decode(request.pdf_data)
        except Exception:
            raise HTTPException(status_code=422, detail="pdf_data is not valid base64.")

        extracted = extract_text_from_pdf(pdf_bytes)
        if not extracted or len(extracted.strip()) < 15:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Could not extract text from this PDF. "
                    "It may be a scanned image-only document. "
                    "Try re-submitting the page as input_type='image'."
                )
            )
        # Inject extracted text so the pipeline treats it as input_type=text
        request.text = extracted

    elif not request.text and not request.url:
        raise HTTPException(status_code=422, detail="Either 'text' or 'url' must be provided.")
    elif request.text and len(request.text.strip()) < 15:
        raise HTTPException(status_code=422, detail="Text content is too short to analyze.")

    # ── Save pending evaluation ───────────────────────────────────────────────
    db       = Session(engine)
    eval_orm = EvaluationORM(
        user_id       = request.user_id,
        session_token = request.session_token,
        input_type    = request.input_type,
        raw_content   = (
            request.text or request.url
            or ("[pdf]" if request.input_type == "pdf" else "[image]")
        ),
        status        = "pending",
    )
    try:
        db.add(eval_orm)
        db.commit()
        db.refresh(eval_orm)
        eval_id = eval_orm.id
    except Exception as e:
        logger.warning(f"DB save (pending) failed: {e}")
        eval_id = 0
    finally:
        db.close()

    # ── Run pipeline (H-6: with executor + timeout) ───────────────────────────
    try:
        loop   = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, functools.partial(_pipeline.run, request)),
            timeout=_PIPELINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Pipeline timeout after {_PIPELINE_TIMEOUT}s for eval {eval_id}")
        raise HTTPException(status_code=504, detail="Analysis timed out. Please try again.")
    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred during analysis.")

    # ── Persist results in background ─────────────────────────────────────────
    def _save_results():
        db2 = None
        try:
            db2 = Session(engine)
            orm = db2.get(EvaluationORM, eval_id)
            if orm:
                orm.parsed_text   = PreprocessingModule.clean(request.text or "")
                orm.system_score  = None if result.get("is_inconclusive") else result["score"]
                orm.analysis_json = result
                orm.status        = "analyzed"
                db2.commit()

                # Backfill evaluation_id into php_input_log (v3)
                _backfill_php_input_log(request.session_token, eval_id)

                saved_claim_ids: dict = {}
                for c in result["claims"]:
                    claim_orm = ClaimORM(
                        evaluation_id  = eval_id,
                        claim_text     = c["text"],
                        sentence_index = c["sentence_index"],
                        label          = c["label"],
                        confidence     = c["confidence"],
                    )
                    db2.add(claim_orm)
                    db2.flush()
                    saved_claim_ids[c["text"][:80]] = claim_orm.id
                db2.commit()
        except Exception as exc:
            logger.warning(f"Background DB save failed: {exc}")
            try:
                if db2: db2.rollback()
            except Exception:
                pass
        finally:
            if db2: db2.close()

    background_tasks.add_task(_save_results)

    return AnalyzeResponse(
        evaluation_id               = eval_id,
        score                       = result["score"],
        label                       = result["label"],
        is_inconclusive             = result.get("is_inconclusive", False),
        explanation                 = result["explanation"],
        explanation_source          = result.get("explanation_source", "rule_based"),
        claims                      = [ClaimResult(**c) for c in result["claims"]],
        evidence                    = [EvidenceResult(**e) for e in result["evidence"]],
        annotated                   = [AnnotationSegment(**s) for s in result["annotated"]],
        source_score                = result["source_score"],
        bias_score                  = result["bias_score"],
        processing_ms               = result["processing_ms"],
        is_partial                  = result["is_partial"],
        no_claims_detected          = result.get("no_claims_detected", False),
        live_search_used            = result.get("live_search_used", False),
        evidence_coverage           = result["evidence_coverage"],
        unverified_claims           = result["unverified_claims"],
        suggest_secondary_retrieval = result["suggest_secondary_retrieval"],
        sub_scores                  = result["sub_scores"],
        mil_tip                     = result.get("mil_tip", ""),
        mil_tip_source              = result.get("mil_tip_source", "rule_based"),
        all_evidence_neutral        = result.get("all_evidence_neutral", False),
        url_fetch_failed            = result.get("url_fetch_failed", False),
        url_fetch_error             = result.get("url_fetch_error", ""),
        evidence_quality_note       = result.get("evidence_quality_note", ""),
    )


# ── POST /analyze/validate-claim ─────────────────────────────────────────────
@router.post("/analyze/validate-claim")
async def validate_claim(request: ValidateClaimRequest):
    """
    Ask Ollama whether the user-typed text is a checkable factual claim.
    Returns {is_claim: bool, reason: str}.
    Raises 503 if Ollama is unavailable.
    """
    prompt = (
        "You are a claim detection assistant. "
        "Determine whether the following user input is a checkable factual claim — "
        "a statement that asserts something as fact and could be verified with evidence. "
        "Opinions, questions, greetings, and vague statements are NOT claims.\n\n"
        f"User input: \"{request.claim_text}\"\n\n"
        "Respond ONLY with valid JSON in this exact format, no other text:\n"
        "{\"is_claim\": true, \"reason\": \"brief one-sentence explanation\"}"
    )

    try:
        payload = json.dumps({
            "model":  OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data     = json.loads(resp.read().decode())
            raw_text = data.get("response", "").strip()

        raw_text = raw_text.replace("```json", "").replace("```", "").strip()

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(f"[ValidateClaim] Could not parse Ollama JSON: {raw_text!r}")
            return {"is_claim": True, "reason": "Validation parsing failed — proceeding."}

        return {
            "is_claim": bool(parsed.get("is_claim", False)),
            "reason":   parsed.get("reason", ""),
        }

    except Exception as e:
        logger.warning(f"[ValidateClaim] Ollama unavailable: {e}")
        raise HTTPException(
            status_code=503,
            detail="Ollama validation service is offline. Skipping claim check.",
        )


# ── POST /analyze/user-claim ──────────────────────────────────────────────────
@router.post("/analyze/user-claim")
async def analyze_user_claim(
    request: UserClaimRequest,
    background_tasks: BackgroundTasks,
    req: Request,
    authorization: str = Header(None),
):
    """
    §4.3 — Re-analyze with a user-typed claim when the system detected none.
    C-4: Requires auth for logged-in users; anonymous users verified by session_token.
    H-3: Pipeline runs in executor — does not block the event loop.
    """
    # C-4: Load original evaluation first
    db = Session(engine)
    try:
        eval_orm = db.get(EvaluationORM, request.evaluation_id)
        if not eval_orm:
            raise HTTPException(status_code=404, detail="Evaluation not found.")

        # C-4: Ownership check
        # Logged-in user: verify JWT sub matches evaluation user_id
        # Anonymous user: verify session_token matches
        if authorization and authorization.startswith("Bearer "):
            try:
                current_user = get_current_user(req, authorization)
                if eval_orm.user_id is not None and eval_orm.user_id != current_user["sub"]:
                    raise HTTPException(status_code=403, detail="Access denied.")
            except HTTPException as e:
                if e.status_code == 401:
                    # No valid token — fall through to session_token check
                    if eval_orm.session_token != request.session_token:
                        raise HTTPException(status_code=403, detail="Access denied.")
                else:
                    raise
        else:
            # No auth header — verify by session_token
            if eval_orm.session_token != request.session_token:
                raise HTTPException(status_code=403, detail="Access denied.")

        original_text = eval_orm.parsed_text or eval_orm.raw_content
    finally:
        db.close()

    synthetic_request = AnalyzeRequest(
        text          = original_text,
        input_type    = "text",
        session_token = request.session_token,
        user_id       = request.user_id,
    )

    # H-3: Run in executor — does NOT block the event loop
    try:
        loop   = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                _executor,
                functools.partial(_pipeline.run, synthetic_request, user_submitted_claim=request.claim_text),
            ),
            timeout=_PIPELINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Analysis timed out. Please try again.")
    except Exception as e:
        logger.error(f"User claim pipeline error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred during analysis.")

    def _save_user_claim_results():
        db2 = None
        try:
            db2 = Session(engine)
            claim_orm = ClaimORM(
                evaluation_id  = request.evaluation_id,
                claim_text     = request.claim_text,
                sentence_index = -1,
                label          = result["claims"][0]["label"] if result["claims"] else "unverified",
                confidence     = 1.0,
            )
            db2.add(claim_orm)
            db2.flush()
            claim_id = claim_orm.id

            orm = db2.get(EvaluationORM, request.evaluation_id)
            if orm:
                orm.system_score  = result["score"]
                orm.analysis_json = {
                    **(orm.analysis_json or {}),
                    **result,
                    "user_submitted_claim": request.claim_text,
                }
                orm.status = "analyzed"

            db2.commit()
        except Exception as exc:
            logger.warning(f"User claim DB save failed: {exc}")
            try:
                if db2: db2.rollback()
            except Exception:
                pass
        finally:
            if db2: db2.close()

    background_tasks.add_task(_save_user_claim_results)

    return {
        "evaluation_id":    request.evaluation_id,
        "claim_text":       request.claim_text,
        "score":            result["score"],
        "label":            result["label"],
        "explanation":      result["explanation"],
        "claims":           result["claims"],
        "evidence":         result["evidence"],
        "is_partial":       result["is_partial"],
        "live_search_used": result.get("live_search_used", False),
        "processing_ms":    result["processing_ms"],
    }


# ── GET /evaluations/{id} ─────────────────────────────────────────────────────
# C-1: Requires auth; verifies ownership by user_id or session_token
@router.get("/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: int,
    req: Request,
    session_token: str = None,
    authorization: str = Header(None),
):
    db = Session(engine)
    try:
        row = db.get(EvaluationORM, evaluation_id)
        if not row:
            raise HTTPException(status_code=404, detail="Evaluation not found.")

        # C-1: Ownership check — logged-in user OR matching session_token
        if authorization and authorization.startswith("Bearer "):
            current_user = get_current_user(req, authorization)
            if row.user_id is not None and row.user_id != current_user["sub"]:
                raise HTTPException(status_code=403, detail="Access denied.")
        elif session_token:
            if row.session_token != session_token:
                raise HTTPException(status_code=403, detail="Access denied.")
        else:
            raise HTTPException(status_code=401, detail="Authentication required.")

        return {
            "id":           row.id,
            "system_score": row.system_score,
            "status":       row.status,
            "analysis":     row.analysis_json,
            "created_at":   row.created_at.isoformat(),
        }
    finally:
        db.close()


# ── GET /evaluations/{id}/comparison ─────────────────────────────────────────
# C-1: Same ownership guard as above
@router.get("/evaluations/{evaluation_id}/comparison")
async def get_comparison(
    evaluation_id: int,
    req: Request,
    session_token: str = None,
    authorization: str = Header(None),
):
    db = Session(engine)
    try:
        # Ownership check on the parent evaluation
        row_check = db.get(EvaluationORM, evaluation_id)
        if not row_check:
            raise HTTPException(status_code=404, detail="Evaluation not found.")

        if authorization and authorization.startswith("Bearer "):
            current_user = get_current_user(req, authorization)
            if row_check.user_id is not None and row_check.user_id != current_user["sub"]:
                raise HTTPException(status_code=403, detail="Access denied.")
        elif session_token:
            if row_check.session_token != session_token:
                raise HTTPException(status_code=403, detail="Access denied.")
        else:
            raise HTTPException(status_code=401, detail="Authentication required.")

        row = db.execute(
            sa.text("""
                SELECT ue.*, e.system_score, e.analysis_json
                FROM user_evaluations ue
                JOIN evaluations e ON e.id = ue.evaluation_id
                WHERE ue.evaluation_id = :eid
                ORDER BY ue.submitted_at DESC
                LIMIT 1
            """),
            {"eid": evaluation_id},
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404, detail="No user evaluation found for this evaluation."
            )
        return dict(row._mapping)
    finally:
        db.close()


# ── GET /evaluations ──────────────────────────────────────────────────────────
# H-1: user_id is now derived from JWT — never accepted from query string
@router.get("/evaluations")
async def list_evaluations(
    req: Request,
    session_token: str = None,
    limit: int = 20,
    authorization: str = Header(None),
):
    db = Session(engine)
    try:
        # Logged-in user: derive user_id from JWT
        if authorization and authorization.startswith("Bearer "):
            current_user = get_current_user(req, authorization)
            rows = db.execute(
                sa.text("""
                    SELECT e.id, e.system_score, e.raw_content,
                           e.status, e.created_at,
                           ue.user_score, ue.confidence_level, ue.submitted_at,
                           re.revised_score
                    FROM evaluations e
                    LEFT JOIN user_evaluations ue ON ue.evaluation_id = e.id
                    LEFT JOIN re_evaluations re ON re.user_evaluation_id = ue.id
                    WHERE e.user_id = :uid
                    ORDER BY e.created_at DESC LIMIT :lim
                """),
                {"uid": current_user["sub"], "lim": limit},
            ).fetchall()
        elif session_token:
            rows = db.execute(
                sa.text("""
                    SELECT e.id, e.system_score, e.raw_content,
                           e.status, e.created_at,
                           ue.user_score, ue.confidence_level, ue.submitted_at,
                           re.revised_score
                    FROM evaluations e
                    LEFT JOIN user_evaluations ue ON ue.evaluation_id = e.id
                    LEFT JOIN re_evaluations re ON re.user_evaluation_id = ue.id
                    WHERE e.session_token = :tok
                    ORDER BY e.created_at DESC LIMIT :lim
                """),
                {"tok": session_token, "lim": limit},
            ).fetchall()
        else:
            raise HTTPException(status_code=422, detail="Authorization header or session_token required.")
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


# ── GET /corpus-gaps ──────────────────────────────────────────────────────────
# H-4: Requires authentication (admin or any logged-in user)
@router.get("/corpus-gaps")
async def corpus_gaps(
    req: Request,
    authorization: str = Header(None),
):
    get_current_user(req, authorization)   # H-4: auth required
    return {"gaps": get_unverified_log()}


# ── php_input_log backfill helper ─────────────────────────────────────────────

def _backfill_php_input_log(session_token: str, evaluation_id: int) -> None:
    """
    After analysis completes, backfill evaluation_id into php_input_log
    so the orchestrator can trace a PHP log entry back to an evaluation.
    Safe to call even if the row doesn't exist or php_input_log is absent.
    """
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("""
                UPDATE php_input_log
                SET evaluation_id = :eid
                WHERE session_token = :tok
                  AND evaluation_id IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            """), {"eid": evaluation_id, "tok": session_token})
    except Exception as e:
        logger.debug(f"[PhpInputLog] Backfill skipped (non-fatal): {e}")


# ── POST /analyze/pre-share-check ────────────────────────────────────────────

@router.post("/analyze/pre-share-check", response_model=SourceStepResponse)
async def pre_share_check(body: dict):
    """
    Lightweight source check surfaced before the user shares content.

    Per guide §7 / §8:
      - Runs Source metadata + Google Fact Check API + MBFC lookup only.
      - Does NOT run NLI, bias analysis, or full evidence retrieval.
      - Optional and non-blocking — prompts the user to verify before sharing.
      - Accepts: { url: str, claim_text?: str, session_token: str }

    Returns SourceStepResponse with factcheck_results and mbfc populated.
    """
    from pydantic import ValidationError

    url         = (body.get("url") or "").strip()
    claim_text  = (body.get("claim_text") or "").strip()
    session_tok = (body.get("session_token") or "").strip()

    if not url and not claim_text:
        raise HTTPException(
            status_code=422,
            detail="Either 'url' or 'claim_text' must be provided."
        )

    # ── Source metadata (offline, fast) ──────────────────────────────────────
    from pipeline.source_credibility import (
        SourceCredibilityModule,
        get_mbfc_rating,
        get_factcheck_results,
    )

    source_result = SourceCredibilityModule.evaluate(url or None, claim_text)

    # ── MBFC lookup (DB, fast) ────────────────────────────────────────────────
    mbfc_raw = get_mbfc_rating(url or None)
    mbfc     = MBFCRating(**mbfc_raw) if mbfc_raw else None

    # ── Google Fact Check (async HTTP, cached) ────────────────────────────────
    factcheck_raw: list = []
    if claim_text:
        try:
            factcheck_raw = await asyncio.wait_for(
                get_factcheck_results(claim_text), timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning("[PreShareCheck] Fact Check API timed out — returning empty")
        except Exception as e:
            logger.warning(f"[PreShareCheck] Fact Check API error: {e}")

    from urllib.parse import urlparse as _urlparse
    parsed_domain = ""
    if url:
        try:
            parsed_domain = _urlparse(url if url.startswith("http") else "https://" + url).netloc.replace("www.", "")
        except Exception:
            parsed_domain = url

    return SourceStepResponse(
        domain        = parsed_domain or "(no domain)",
        source_type   = "url" if url else "text",
        trust_signals = source_result.get("signals", []),
        source_score  = source_result.get("score", 0.5),
        source_label  = source_result.get("label", "Unknown"),
        mbfc          = mbfc,
        factcheck_results = [FactCheckResult(**r) for r in factcheck_raw],
        claimbuster_score = None,   # pre-share check skips ClaimBuster
    )