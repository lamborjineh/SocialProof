"""
SocialProof — Model Registry
Lazy singleton loader for heavy ML models.
Models are loaded once at startup and reused across all requests.

Change from v2.0:
  NLI model: MoritzLaurer/mDeBERTa-v3-base-mnli-xnli — multilingual (100 langs incl. Filipino).
  No translation required for Filipino input. Loaded as text-classification pipeline.
  with text-pair input (evidence as premise, claim as hypothesis). This is
  the semantically correct NLI setup and matches the architecture spec.
"""

import threading
from typing import Optional, Any

import torch
from transformers import pipeline as hf_pipeline

from config import NLI_MODEL, EMBED_MODEL, logger

_nlp_lock   = threading.Lock()
_embed_lock = threading.Lock()
_nli_lock   = threading.Lock()


class ModelRegistry:
    """
    Thread-safe lazy singleton loader for spaCy, SentenceTransformer, and NLI.
    All models are class-level attributes so they live for the process lifetime.
    """

    _nlp:   Optional[Any] = None
    _embed: Optional[Any] = None
    _nli:   Optional[Any] = None

    @classmethod
    def nlp(cls):
        """Return the loaded spaCy en_core_web_sm model."""
        with _nlp_lock:
            if cls._nlp is None:
                import spacy
                logger.info("Loading spaCy model en_core_web_sm…")
                cls._nlp = spacy.load("en_core_web_sm")
        return cls._nlp

    @classmethod
    def embed(cls):
        """Return the loaded SentenceTransformer model."""
        with _embed_lock:
            if cls._embed is None:
                from sentence_transformers import SentenceTransformer
                logger.info(f"Loading SentenceTransformer {EMBED_MODEL}…")
                cls._embed = SentenceTransformer(EMBED_MODEL)
        return cls._embed

    @classmethod
    def nli(cls):
        """
        Return the loaded NLI classification pipeline (thread-safe).

        mDeBERTa-v3-base-mnli-xnli (or configured NLI_MODEL) is loaded as text-classification rather than
        zero-shot-classification. Callers pass a dict:
            {"text": evidence_sentence, "text_pair": claim_sentence}
        and receive back {"label": "ENTAILMENT"|"CONTRADICTION"|"NEUTRAL",
                          "score": float}.

        This is the standard, correct way to use an MNLI model for
        premise–hypothesis NLI (evidence = premise, claim = hypothesis).
        """
        with _nli_lock:
            if cls._nli is None:
                logger.info(f"Loading NLI model {NLI_MODEL}…")
                cls._nli = hf_pipeline(
                    "text-classification",
                    model=NLI_MODEL,
                    device=0 if torch.cuda.is_available() else -1,
                )
        return cls._nli
