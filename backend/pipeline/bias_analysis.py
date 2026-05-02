"""
SocialProof — Module 4: Language & Bias Analysis
Detects emotional language, exaggeration, and clickbait patterns.
Uses NRC Emotion Lexicon word lists + rule-based regex patterns.

v2.0 Changes:
  - Added Filipino/Tagalog emotional word sets and clickbait phrases.

v2.1 Fix:
  - Filipino hyphenated compound words (walang-hiya, nakaka-alarma, etc.) were
    silently not matching because re.findall(r"\b[a-z]+\b") splits on hyphens.
    Fixed by using _fil_word_match() which handles hyphens correctly.
    English detection is unchanged.
"""

import re
from typing import Dict

from config import logger


class BiasAnalysisModule:
    """
    Returns bias_score (0.0–1.0) and a list of detected signals.
    Higher score = more biased / emotional language detected.
    """

    FEAR_WORDS = {
        "dangerous", "deadly", "fatal", "kill", "death", "die", "terrifying",
        "horrifying", "catastrophic", "devastating", "crisis", "emergency",
        "urgent", "critical", "alarming", "disturbing", "horrific", "tragic",
    }
    ANGER_WORDS = {
        "outrageous", "disgusting", "unacceptable", "corrupt", "evil", "disgrace",
        "shameful", "despicable", "appalling", "atrocity", "betrayal", "scandal",
    }
    EXAGGERATION_WORDS = {
        "absolutely", "completely", "totally", "definitely", "certainly", "always",
        "never", "everyone", "nobody", "all", "none", "every single", "without exception",
        "proven", "fact", "truth", "lie", "hoax", "fake",
    }
    CLICKBAIT_PHRASES = [
        r"you won'?t believe",
        r"they (don'?t want|are hiding|are afraid)",
        r"doctors hate",
        r"this one (trick|secret|weird)",
        r"share (before|this) (they delete|it'?s removed)",
        r"act now",
        r"wake up",
        r"open your eyes",
        r"the truth about",
        r"what (they|the media|the government) (won'?t tell|is hiding|doesn'?t want)",
        r"shocking (truth|secret|revelation)",
    ]

    # ── Filipino / Tagalog emotional language ─────────────────────────────────
    # NOTE: hyphenated compounds are matched via _fil_word_match(), not standard
    # re.findall(r"\b[a-z]+\b") which splits on hyphens and breaks them.

    FEAR_WORDS_FIL = {
        "nakakatakot", "nakakagulat", "nakaka-alarma", "nakakaaalarma",
        "mapanganib", "delikado", "nakamamatay", "nakasisindak",
        "kahindik-hindik", "nakakakilig", "nakakabahalang",
        "krisis", "emerhensya", "alerto", "babala",
    }

    ANGER_WORDS_FIL = {
        "bulok", "corrupt", "kurap", "hayop", "demonyo", "kasamaan",
        "walang-hiya", "walang-kahihiyan", "kahihiyan",
        "nakakahiya", "nakakadiri", "karumal-dumal",
        "kataksilan", "traydor", "lokohin", "niloloko",
        "nilolooban", "pagtataksil",
    }

    EXAGGERATION_WORDS_FIL = {
        "lahat", "walang-sinuman", "palagi", "lagi",
        "hindi-kailanman", "tiyak", "sigurado",
        "totoong-totoo", "katotohanan",
        "itinago", "itinatago", "nagtatago", "nilulubos",
        "inililihim", "lihim", "sinasaklaw", "pinagtatakpan",
    }

    CLICKBAIT_PHRASES_FIL = [
        r"hindi mo maniwa(la)?",
        r"ayaw nilang malaman",
        r"itinago ng (gobyerno|media)",
        r"i-?share bago (i-?delete|tanggalin)",
        r"gising na",
        r"buksan ang (iyong )?mata",
        r"ang (tunay na )?katotohanan",
        r"hindi (nila )?gustong malaman mo",
        r"kumilos na",
        r"nakaka-(gulat|takot|alarma)",
        r"gawa-?gawa (lang )?ito",
    ]

    @classmethod
    def _fil_word_match(cls, text_lower: str, word_set: set) -> set:
        """
        Match Filipino words including hyphenated compounds.

        Standard re.findall(r"\\b[a-z]+\\b") splits 'walang-hiya' into
        ['walang', 'hiya'] so neither token matches the key 'walang-hiya'.

        This method:
          1. Checks direct substring for hyphenated words
          2. Uses standard word-boundary match for plain words
        """
        hits       = set()
        hyphenated = {w for w in word_set if "-" in w}
        plain      = word_set - hyphenated

        for word in hyphenated:
            if word in text_lower:
                hits.add(word)

        plain_tokens = set(re.findall(r"\b[a-z]+\b", text_lower))
        hits |= plain_tokens & plain

        return hits

    @classmethod
    def analyze(cls, text: str, doc) -> Dict:
        text_lower = text.lower()
        words      = set(re.findall(r"\b[a-z]+\b", text_lower))
        signals    = []
        score      = 0.0

        # ALL CAPS words (3+ chars)
        caps_words = re.findall(r"\b[A-Z]{3,}\b", text)
        if caps_words:
            score += min(0.15 * len(caps_words), 0.25)
            signals.extend([f"all_caps:{w}" for w in caps_words[:3]])

        # Excessive exclamation marks
        excl_count = text.count("!")
        if excl_count >= 2:
            score += min(0.05 * excl_count, 0.15)
            signals.append(f"excessive_exclamation:{excl_count}")

        # Emotion word groups — English
        fear_hits = words & cls.FEAR_WORDS
        if fear_hits:
            score += 0.08 * len(fear_hits)
            signals.extend([f"fear:{w}" for w in fear_hits])

        anger_hits = words & cls.ANGER_WORDS
        if anger_hits:
            score += 0.08 * len(anger_hits)
            signals.extend([f"anger:{w}" for w in anger_hits])

        exag_hits = words & cls.EXAGGERATION_WORDS
        if exag_hits:
            score += 0.05 * len(exag_hits)
            signals.extend([f"exaggeration:{w}" for w in list(exag_hits)[:4]])

        # Clickbait patterns — English
        for pattern in cls.CLICKBAIT_PHRASES:
            if re.search(pattern, text_lower):
                score += 0.20
                signals.append(f"clickbait_pattern:{pattern[:30]}")

        # Emotion word groups — Filipino (hyphen-aware)
        fear_fil = cls._fil_word_match(text_lower, cls.FEAR_WORDS_FIL)
        if fear_fil:
            score += 0.08 * len(fear_fil)
            signals.extend([f"fear_fil:{w}" for w in fear_fil])

        anger_fil = cls._fil_word_match(text_lower, cls.ANGER_WORDS_FIL)
        if anger_fil:
            score += 0.08 * len(anger_fil)
            signals.extend([f"anger_fil:{w}" for w in anger_fil])

        exag_fil = cls._fil_word_match(text_lower, cls.EXAGGERATION_WORDS_FIL)
        if exag_fil:
            score += 0.05 * len(exag_fil)
            signals.extend([f"exaggeration_fil:{w}" for w in list(exag_fil)[:4]])

        # Clickbait patterns — Filipino
        for pattern in cls.CLICKBAIT_PHRASES_FIL:
            if re.search(pattern, text_lower):
                score += 0.20
                signals.append(f"clickbait_fil:{pattern[:30]}")
                break

        # Superlatives via spaCy JJS tag
        superlatives = [t.text for t in doc if t.tag_ == "JJS"]
        if superlatives:
            score += 0.05 * min(len(superlatives), 3)
            signals.extend([f"superlative:{w}" for w in superlatives[:2]])

        score = max(0.0, min(1.0, score))
        label = (
            "High Bias"     if score > 0.55 else
            "Moderate Bias" if score > 0.25 else
            "Low Bias"
        )
        return {"score": round(score, 3), "label": label, "signals": signals[:12]}
