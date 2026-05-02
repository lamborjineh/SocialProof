"""
SocialProof — Module 0b: Image Input / OCR
Extracts plain text from uploaded images so the normal analysis pipeline
can process image-based social media posts.

Priority chain:
  1. EasyOCR  — handles Tagalog/Filipino characters, no system install needed
                 pip install easyocr pillow numpy
  2. Tesseract — fallback, requires system install:
                 Windows: https://github.com/UB-Mannheim/tesseract/wiki
                 Linux:   sudo apt install tesseract-ocr
                 pip install pytesseract
  3. Error    — returns empty string with a clear message if both fail

Changes vs. original:
  - preload_ocr() called at app startup to eliminate first-request cold-start
  - Image preprocessing (grayscale + contrast boost) before OCR for accuracy
  - detail=0 kept for EasyOCR (confidence scores skipped — needs downstream changes)

Usage (called by routers/analyze.py):
    from pipeline.image_input import extract_text_from_image, preload_ocr
    preload_ocr()   # call once at startup in main.py
    text = extract_text_from_image(image_bytes)
"""

import io
import logging
from typing import Optional

logger = logging.getLogger("socialproof")

# ── EasyOCR singleton (load once, reuse) ─────────────────────────────────────
_easyocr_reader    = None
_easyocr_available: Optional[bool] = None   # None = not yet checked


def _get_easyocr_reader():
    global _easyocr_reader, _easyocr_available
    if _easyocr_available is False:
        return None
    if _easyocr_reader is not None:
        return _easyocr_reader
    try:
        import easyocr
        # English + Filipino (Tagalog uses Latin script so "en" covers it)
        # gpu=False keeps it CPU-safe; set True if you have a CUDA GPU
        _easyocr_reader    = easyocr.Reader(["en"], gpu=False, verbose=False)
        _easyocr_available = True
        logger.info("[OCR] EasyOCR reader loaded (en).")
        return _easyocr_reader
    except ImportError:
        _easyocr_available = False
        logger.warning("[OCR] EasyOCR not installed. Run: pip install easyocr")
        return None
    except Exception as e:
        _easyocr_available = False
        logger.warning(f"[OCR] EasyOCR failed to load: {e}")
        return None


def preload_ocr() -> None:
    """
    Warm up the EasyOCR model at startup.
    Call this once in main.py (e.g. in a startup event or top-level) so the
    first user request doesn't pay the 10-30s model-load penalty.

    Example in main.py:
        from pipeline.image_input import preload_ocr
        preload_ocr()
    """
    logger.info("[OCR] Preloading EasyOCR model...")
    reader = _get_easyocr_reader()
    if reader:
        logger.info("[OCR] EasyOCR preload complete.")
    else:
        logger.warning("[OCR] EasyOCR preload failed — will try Tesseract at runtime.")


# ── Image preprocessing ───────────────────────────────────────────────────────

def _preprocess_image(image_bytes: bytes):
    """
    Prepare image for OCR:
      1. Convert to grayscale  — removes colour noise, most text is B&W anyway
      2. Boost contrast        — makes faint text pop against backgrounds
      3. Upscale if tiny       — OCR accuracy drops sharply on small images

    Returns a PIL Image ready to pass to an OCR engine.
    Returns None if PIL is unavailable or image loading fails.
    """
    try:
        from PIL import Image, ImageEnhance, ImageOps

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Upscale very small images (shorter side < 300px hurts OCR a lot)
        min_side = min(img.size)
        if min_side < 300:
            scale = 300 / min_side
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)

        # Grayscale
        img = ImageOps.grayscale(img)

        # Contrast boost (factor 2.0 is a reliable default for screenshots/photos)
        img = ImageEnhance.Contrast(img).enhance(2.0)

        # Convert back to RGB so both EasyOCR and Tesseract accept it
        img = img.convert("RGB")

        return img
    except ImportError:
        logger.warning("[OCR] PIL not available — skipping preprocessing.")
        return None
    except Exception as e:
        logger.warning(f"[OCR] Image preprocessing failed: {e}")
        return None


# ── EasyOCR extraction ────────────────────────────────────────────────────────

def _extract_easyocr(image_bytes: bytes) -> Optional[str]:
    reader = _get_easyocr_reader()
    if reader is None:
        return None
    try:
        import numpy as np
        from PIL import Image

        img = _preprocess_image(image_bytes)
        if img is None:
            # Preprocessing failed — fall back to raw load
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        arr   = np.array(img)
        lines = reader.readtext(arr, detail=0, paragraph=True)
        text  = "\n".join(line.strip() for line in lines if line.strip())
        if text:
            logger.info(f"[OCR] EasyOCR extracted {len(text)} chars.")
        return text or None
    except Exception as e:
        logger.warning(f"[OCR] EasyOCR extraction failed: {e}")
        return None


# ── Tesseract fallback ────────────────────────────────────────────────────────

def _extract_tesseract(image_bytes: bytes) -> Optional[str]:
    try:
        import pytesseract
        from PIL import Image

        img = _preprocess_image(image_bytes)
        if img is None:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # oem 3 = default LSTM engine, psm 3 = fully automatic page segmentation
        text = pytesseract.image_to_string(img, config="--oem 3 --psm 3")
        text = text.strip()
        if text:
            logger.info(f"[OCR] Tesseract extracted {len(text)} chars.")
        return text or None
    except ImportError:
        logger.warning("[OCR] pytesseract not installed. Run: pip install pytesseract")
        return None
    except Exception as e:
        logger.warning(f"[OCR] Tesseract extraction failed: {e}")
        return None


# ── Public interface ──────────────────────────────────────────────────────────

def extract_text_from_image(image_bytes: bytes) -> str:
    """
    Extract text from image bytes using EasyOCR (primary) or Tesseract (fallback).
    Images are preprocessed (grayscale + contrast boost) before OCR.

    Args:
        image_bytes: Raw bytes of the uploaded image (JPEG, PNG, WEBP, etc.)

    Returns:
        Extracted text string, or empty string if both engines fail.
        Caller should check len(result) > 0 before proceeding.
    """
    if not image_bytes:
        return ""

    # Primary: EasyOCR
    text = _extract_easyocr(image_bytes)
    if text:
        return text

    # Fallback: Tesseract
    logger.info("[OCR] Falling back to Tesseract.")
    text = _extract_tesseract(image_bytes)
    if text:
        return text

    logger.warning(
        "[OCR] Both EasyOCR and Tesseract failed or are not installed. "
        "Install with: pip install easyocr pillow"
    )
    return ""


def is_ocr_available() -> dict:
    """
    Health check — reports which OCR engines are available.
    Called by GET /health to surface OCR status.
    """
    easyocr_ok = _get_easyocr_reader() is not None

    tesseract_ok = False
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        tesseract_ok = True
    except Exception:
        pass

    return {
        "easyocr":   easyocr_ok,
        "tesseract": tesseract_ok,
        "any":       easyocr_ok or tesseract_ok,
    }
