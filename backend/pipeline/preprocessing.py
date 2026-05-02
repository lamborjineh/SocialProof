"""
SocialProof — Module 1: Preprocessing
Cleans raw input and extracts keywords for downstream modules.
"""

import re
from typing import List

from config import logger


class PreprocessingModule:
    """
    Cleans raw input text:
      - Strips HTML tags
      - Replaces URLs with [URL] placeholder
      - Normalises whitespace and zero-width characters
      - Normalises curly quotes to straight quotes
    """

    @staticmethod
    def clean(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"http\S+|www\.\S+", "[URL]", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
        text = text.replace("\u201c", '"').replace("\u201d", '"')
        text = text.replace("\u2018", "'").replace("\u2019", "'")
        return text.strip()

    @staticmethod
    def extract_keywords(doc) -> List[str]:
        """Extract noun phrases and named entities as keywords (max 20)."""
        keywords: set = set()
        for chunk in doc.noun_chunks:
            if len(chunk.text.split()) <= 4:
                keywords.add(chunk.text.lower())
        for ent in doc.ents:
            keywords.add(ent.text.lower())
        return list(keywords)[:20]
