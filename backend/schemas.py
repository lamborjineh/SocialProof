"""
SocialProof — Pydantic Schemas  v3.0
All API request/response models in one place.

v3.1 Changes vs v3.0:
  - Added FactCheckResult — Google Fact Check Tools API result item
  - SourceStepResponse extended: factcheck_results + claimbuster_score

v3.0 Changes vs v2:
  - input_type: added 'pdf' to the enum alongside text | url | image
  - Added MBFCRating        — domain credibility signal from mbfc_domains table
  - Added SourceStepResponse — extended Source node payload carrying mbfc field
  - Added QuizAttemptResponse, PretestSubmitRequest, PretestResultResponse
    (were being returned as raw dicts from quiz router — now typed)
  - Added LessonCompletionRequest, LessonCompletionResponse
  - AnalyzeRequest: image_data stays Optional[str] (base64); pdf_data added
    alongside it using the same pattern
  - All existing schemas preserved exactly
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, EmailStr, field_validator


# ── Analysis — Request ────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    text:          Optional[str] = Field(None, description="Raw text content")
    url:           Optional[str] = Field(None, description="URL to analyze")
    input_type:    str           = Field("text", description="text | url | image | pdf")
    session_token: str           = Field(..., description="Anonymous session token")
    user_id:       Optional[int] = Field(None, description="Authenticated user ID")
    # image input — base64-encoded, decoded with base64.b64decode() in the router
    image_data:    Optional[str] = Field(
        None,
        description="Base64-encoded image string (input_type=image)."
    )
    # pdf input — base64-encoded PDF bytes, text extracted via pdfplumber in the router
    pdf_data:      Optional[str] = Field(
        None,
        description="Base64-encoded PDF bytes (input_type=pdf). "
                    "Text extracted server-side before entering the pipeline."
    )


class UserClaimRequest(BaseModel):
    """
    When the system detects no claims, the user can type their own.
    Re-runs the full pipeline on the user-supplied claim text,
    stored as a child of the original evaluation.
    """
    evaluation_id: int
    claim_text:    str = Field(..., min_length=10, description="User-typed claim to verify")
    session_token: str
    user_id:       Optional[int] = None


class ValidateClaimRequest(BaseModel):
    """
    Ask Ollama whether a user-typed string is a checkable factual claim.
    Returns {is_claim: bool, reason: str}.
    """
    claim_text:    str = Field(..., min_length=3, description="User-typed text to validate")
    evaluation_id: int


# ── Analysis — Sub-models ─────────────────────────────────────────────────────

class ClaimResult(BaseModel):
    text:             str
    sentence_index:   int
    label:            str
    confidence:       float
    evidence_found:   bool          = False
    check_worthiness: Optional[float] = None


class EvidenceResult(BaseModel):
    evidence_text:    str
    type:             str
    source_label:     str
    source_url:       Optional[str]   = None
    similarity_score: float
    claim_text:       str
    nli_confidence:   Optional[float] = None
    source_type:      Optional[str]   = None   # faiss | live | hardcoded


class AnnotationSegment(BaseModel):
    text:   str
    type:   str
    status: Optional[str] = None


# ── Analysis — Response ───────────────────────────────────────────────────────

class AnalyzeResponse(BaseModel):
    evaluation_id:      int
    label:              str
    explanation:        str
    explanation_source: str = "rule_based"
    claims:             List[ClaimResult]
    evidence:           List[EvidenceResult]
    annotated:          List[AnnotationSegment]
    source_score:       float
    bias_score:         float
    sub_scores:         Dict[str, Any]
    processing_ms:      int
    is_partial:                  bool  = False
    is_inconclusive:             bool  = False
    no_claims_detected:          bool  = False
    live_search_used:            bool  = False
    evidence_coverage:           float = 1.0
    unverified_claims:           List[str] = Field(default_factory=list)
    suggest_secondary_retrieval: bool  = False
    mil_tip:                     str   = ""
    mil_tip_source:              str   = "rule_based"
    all_evidence_neutral:        bool  = False
    evidence_quality_note:       str   = ""
    url_fetch_failed:            bool  = False
    url_fetch_error:             str   = ""


# ── Source node — v3 extended response ───────────────────────────────────────

class FactCheckResult(BaseModel):
    """
    One result from the Google Fact Check Tools API.
    Surfaced in the Source node as clickable review links — never as a verdict.
    """
    publisher:      str
    url:            str
    textual_rating: str
    claim_date:     Optional[str] = None


class MBFCRating(BaseModel):
    """
    Domain credibility signal from the mbfc_domains table (iffy.news export).
    Surfaced in the Source node as context — never as a verdict.
    The frontend displays this alongside other source signals so the user
    can draw their own conclusions.
    """
    domain:            str
    factual_reporting: Optional[str] = None   # HIGH / MOSTLY_FACTUAL / MIXED / LOW / VERY_LOW
    bias_rating:       Optional[str] = None   # LEFT-CENTER / CENTER / RIGHT / etc.
    credibility_rating: Optional[str] = None
    country:           Optional[str] = None


class SourceStepResponse(BaseModel):
    """
    Returned when the Source node is triggered (on-demand, not on submission).
    v3 extends v2 with MBFC domain signal, Google Fact Check results, and
    ClaimBuster check-worthiness score.
    """
    domain:            str
    source_type:       str
    trust_signals:     List[str]
    source_score:      float
    source_label:      str
    mbfc:              Optional[MBFCRating]         = None   # None if domain not in MBFC dataset
    factcheck_results: List[FactCheckResult]        = Field(default_factory=list)  # Google Fact Check API
    claimbuster_score: Optional[float]              = None   # 0.0–1.0, stored but not shown as number


# ── User Evaluation ───────────────────────────────────────────────────────────

class UserEvaluationRequest(BaseModel):
    evaluation_id:     int
    user_id:           Optional[int]       = None
    session_token:     str
    identified_claims: Optional[List[str]] = None
    source_credible:   Optional[str]       = None
    bias_detected:     Optional[bool]      = None
    evidence_assessed: Optional[bool]      = None
    user_score:        Optional[int]       = Field(None, ge=0, le=100)
    user_label:        Optional[str]       = None
    confidence_level:  Optional[str]       = None
    skipped_steps:     Optional[List[str]] = Field(default_factory=list)


class FeedbackItem(BaseModel):
    type: str
    text: str


class TriggeredLesson(BaseModel):
    key:            str
    title:          str
    topic:          str
    trigger_reason: str


class ComparisonResult(BaseModel):
    score_diff:       int
    score_diff_label: str              # "Low" | "Moderate" | "High"
    user_label:       Optional[str]
    system_label:     str
    label_match:      bool
    missed_bias:      bool
    missed_claims:    bool
    source_mismatch:  bool
    confidence_level: Optional[str]
    triggered_lessons:    List[TriggeredLesson]
    feedback_items:       List[FeedbackItem]
    feedback_summary:     str
    evidence_was_missing: bool = False
    lesson_context_map:   Dict[str, str] = Field(default_factory=dict)


class UserEvaluationResponse(BaseModel):
    user_evaluation_id: int
    comparison:         ComparisonResult


# ── Re-Evaluation ─────────────────────────────────────────────────────────────

class ReEvaluationRequest(BaseModel):
    user_evaluation_id: int
    revised_score:      Optional[int] = Field(None, ge=0, le=100)
    revised_label:      Optional[str] = None
    revised_confidence: Optional[str] = None
    revision_notes:     Optional[str] = None
    revision_trigger:   Optional[str] = Field(
        None,
        description="system_feedback | own_research | community_review | lesson_learned"
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str      = Field(..., min_length=3, max_length=50)
    email:    EmailStr = Field(..., description="Valid email address")
    password: str      = Field(..., min_length=8)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v):
            raise ValueError("Password must contain at least one special character (!@#$%^&*...).")
        return v


class LoginRequest(BaseModel):
    identifier: str
    password:   str


class AuthResponse(BaseModel):
    token:    str
    user_id:  int
    username: str
    role:     str


# ── Quiz ──────────────────────────────────────────────────────────────────────

class QuizAttemptRequest(BaseModel):
    user_id:        Optional[int] = None
    question_id:    int
    selected_index: int


class QuizAttemptResponse(BaseModel):
    """Immediate feedback returned after a quiz attempt is recorded."""
    is_correct:     bool
    correct_index:  int
    explanation:    Optional[str] = None
    topic:          str
    difficulty:     Optional[str] = None


# ── Pretest / Posttest ────────────────────────────────────────────────────────

class PretestAnswerItem(BaseModel):
    claim_id:       int
    selected_label: str   # "supported" | "misleading" | "neutral" | "unverified"


class PretestSubmitRequest(BaseModel):
    session_token: str
    user_id:       Optional[int] = None
    answers:       List[PretestAnswerItem]


class PretestResultResponse(BaseModel):
    phase:        str    # "pretest" | "posttest"
    score_pct:    int
    correct:      int
    total:        int
    # posttest only — None on pretest
    delta:        Optional[int]   = None   # score_pct - pretest score_pct
    skill_gains:  Optional[Dict[str, Any]] = None


# ── Lessons ───────────────────────────────────────────────────────────────────

class LessonCompletionRequest(BaseModel):
    lesson_id:     int
    user_id:       Optional[int] = None
    session_token: str


class LessonCompletionResponse(BaseModel):
    lesson_id:    int
    completed_at: str
    message:      str = "Lesson marked complete."
