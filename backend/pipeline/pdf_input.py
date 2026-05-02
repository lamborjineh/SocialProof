"""
SocialProof — Module 0c: PDF Input
Extracts plain text from uploaded PDF files so the normal analysis pipeline
can process PDF-based articles, reports, and documents.

Priority chain:
  1. pdfplumber — layout-aware, handles columns and tables well
                  pip install pdfplumber
  2. Error       — returns empty string with a clear message if extraction fails

Usage (called by routers/analyze.py):
    from pipeline.pdf_input import extract_text_from_pdf
    text = extract_text_from_pdf(pdf_bytes)

Notes:
  - Scanned/image-only PDFs will return empty string (no embedded text layer).
    The router should surface this to the user so they can paste the text manually
    or re-submit as input_type=image via OCR.
  - Password-protected PDFs will fail gracefully and return empty string.
  - Very large PDFs (> ~50 pages) are capped at MAX_PAGES to keep latency sane.
"""

import io
import logging
from typing import Optional

logger = logging.getLogger("socialproof")

MAX_PAGES = 50   # Hard cap — analysis targets social-media-length content


# ── pdfplumber extraction ─────────────────────────────────────────────────────

def _extract_pdfplumber(pdf_bytes: bytes) -> Optional[str]:
    try:
        import pdfplumber

        pages_text: list[str] = []

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total = len(pdf.pages)
            if total > MAX_PAGES:
                logger.warning(
                    f"[PDF] Document has {total} pages — capping at {MAX_PAGES}."
                )

            for page in pdf.pages[:MAX_PAGES]:
                try:
                    text = page.extract_text()
                    if text and text.strip():
                        pages_text.append(text.strip())
                except Exception as page_err:
                    logger.debug(f"[PDF] Page extraction failed: {page_err}")
                    continue

        combined = "\n\n".join(pages_text).strip()

        if combined:
            logger.info(
                f"[PDF] pdfplumber extracted {len(combined)} chars "
                f"from {min(total, MAX_PAGES)} page(s)."
            )
            return combined

        # File opened but no text — likely a scanned/image-only PDF
        logger.warning(
            "[PDF] pdfplumber found no text layer. "
            "Document may be a scanned image-only PDF."
        )
        return None

    except ImportError:
        logger.warning("[PDF] pdfplumber not installed. Run: pip install pdfplumber")
        return None
    except Exception as e:
        logger.warning(f"[PDF] pdfplumber extraction failed: {e}")
        return None


# ── Public interface ──────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extract text from PDF bytes using pdfplumber.

    Args:
        pdf_bytes: Raw bytes of the uploaded PDF file.

    Returns:
        Extracted text string, or empty string if extraction fails.
        Caller should check len(result) > 0 before proceeding — an empty
        string means the PDF has no text layer (scanned) or pdfplumber failed.
    """
    if not pdf_bytes:
        return ""

    text = _extract_pdfplumber(pdf_bytes)
    if text:
        return text

    logger.warning(
        "[PDF] Text extraction returned nothing. "
        "If this is a scanned PDF, re-submit as input_type=image."
    )
    return ""


def is_pdf_available() -> dict:
    """
    Health check — reports whether pdfplumber is importable.
    Called by GET /health to surface PDF support status.
    """
    try:
        import pdfplumber  # noqa: F401
        return {"pdfplumber": True, "any": True}
    except ImportError:
        return {"pdfplumber": False, "any": False}
