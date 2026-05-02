
import re
from typing import List, Dict

from config import MAX_CLAIMS, logger

# ─────────────────────────────────────────────────────────────────────────────
# Module-level compiled patterns
# Defined here so annotation.py can import them directly — both modules must
# use identical logic. No circular import risk: claim_detection imports only
# from config.
# ─────────────────────────────────────────────────────────────────────────────

# Sentences starting with these are attribution/transition context — not claims.
CONTEXT_OPENER_RE = re.compile(
    r"^(According to|As reported by|As stated by|Based on|"
    r"The report(s)?|The study|The data|In a statement|"
    r"Officials? said|The government|Authorities said|"
    r"Meanwhile|However|Furthermore|Moreover|In addition|"
    r"On the other hand|As of|As a result|In response|"
    r"While|Although|Whereas|Even though|Despite|"
    r"Ayon sa|Batay sa|Sinabi ng|Iniulat ng)",
    re.IGNORECASE,
)

# Sentence-start hedging — first AND third person openers.
OPINION_OPENER_RE = re.compile(
    r"^(I think|I believe|I feel|I consider|In my opinion|In my view|"
    r"Personally|From my perspective|"
    r"It seems|It appears|It looks like|"
    r"Some (argue|say|claim|believe|suggest|think|feel|contend)|"
    r"Many (argue|say|claim|believe|suggest|think|feel|contend)|"
    r"Others (argue|say|claim|believe|suggest|think)|"
    r"Critics (say|argue|claim|contend)|"
    r"Skeptics (say|argue|claim|believe)|"
    r"Some people (say|argue|claim|believe|think|feel)|"
    r"Many people (say|argue|claim|believe|think)|"
    r"People (argue|say|claim|believe|think)|"
    r"They (claim|argue|say|believe|think|contend|suggest)|"
    r"He (claims?|argues?|says?|believes?|thinks?|suggests?)|"
    r"She (claims?|argues?|says?|believes?|thinks?|suggests?)|"
    r"We (think|believe|feel|should|must)|"
    r"There are (those who|people who|those that)|"
    r"Those who (believe|argue|claim)|"
    r"Proponents (say|argue|claim|believe)|"
    r"Opponents (say|argue|claim|believe)|"
    r"Sa tingin ng ilan|Naniniwala ang ilan|"
    r"Iginigiit ng ilan|May nagsasabing|"
    r"Ayon sa ilan|Ayon sa mga nagtatalo)",
    re.IGNORECASE,
)
# if this matches, it overrides CONTEXT_OPENER_RE.
OPINION_CONTENT_RE = re.compile(
    r"\b(some people (believe|claim|argue|think|say)|"
    r"many (people )?(believe|claim|argue|think|say)|"
    r"they believe|they claim|they argue|they think|"
    r"others believe|others claim|others argue|"
    r"people believe|people claim|people argue|"
    r"individuals (remain|are|were|feel|believe)|"
    r"skeptics (say|claim|argue|believe)|"
    r"critics (say|claim|argue|believe)|"
    r"mga naniniwala|ilan ay naniniwala)\b",
    re.IGNORECASE,
)

# Evaluative sentence openers → opinion.
EVALUATIVE_OPENER_RE = re.compile(
    r"^(This is|That is|These are|It is|He is|She is|They are|It was)\s+"
    r"(a |an )?(great|terrible|awful|good|bad|wrong|right|horrible|excellent|"
    r"unfair|unjust|corrupt|stupid|smart|ridiculous|outrageous)",
    re.IGNORECASE,
)

# Sweeping absolutist generalisations without specifics → opinion.
ABSOLUTISM_RE = re.compile(
    r"\b(all\s+(?:\w+\s+){1,5}(will|are|were|have been|must|shall|"
    r"support|oppose|want|need|agree|reject)|"
    r"every\s+(single\s+)?(?:\w+\s+){0,3}\w+|"
    r"no\s+(one|human|worker|person|job|industry|child|student|"
    r"driver|official|school|election|government)\b|"
    r"\b(never|always|inevitable|inevitably|completely|entirely|"
    r"end\s+of|obsolete|no\s+longer\s+necessary|proves?\s+that|"
    r"clearly\s+(proves?|shows?|signals?)|"
    r"fully\s+support|fully\s+agree|fully\s+endorse|"
    r"dominate\s+the|replace\s+all|misguided))\b",
    re.IGNORECASE,
)

# Numeric/proper-name specifics — used to override absolutism penalty when
# the sentence contains concrete data (years, percentages, proper name pairs).
_SPECIFICS_RE = re.compile(
    r"\b\d{4}\b|\b\d+(\.\d+)?%|\b[A-Z][a-z]+\s+[A-Z][a-z]+\b"
)

# Questions are neither claim nor opinion → context.
QUESTION_RE = re.compile(r"\?$")
QUESTION_OPENER_RE = re.compile(
    r"^(What|Who|When|Where|Why|How|Is|Are|Was|Were|Do|Does|Did|"
    r"Can|Could|Should|Would|Will|Has|Have|Had)\b",
    re.IGNORECASE,
)

# MPQA strong subjectivity lexicon (Wilson et al., 2005).
# Two or more hits in a sentence → strong opinion signal.
MPQA_STRONG_SUBJ = {
    # Evaluative adjectives
    "terrible", "awful", "horrible", "disgusting", "outrageous", "ridiculous",
    "excellent", "wonderful", "amazing", "fantastic", "brilliant",
    "stupid", "idiotic", "corrupt", "evil", "dishonest", "incompetent",
    "unfair", "unjust", "immoral", "wrong", "right",
    "best", "worst", "greatest", "weakest",
    # Emotion verbs
    "love", "hate", "despise", "admire", "fear", "hope", "wish",
    # Epistemic / hedging verbs
    "think", "feel", "seem", "appear", "believe", "consider",
    "should", "must", "ought", "deserve", "want", "need",
    # Certainty adverbs (over-confidence = opinion signal)
    "clearly", "obviously", "certainly", "definitely", "absolutely",
    "unfortunately", "thankfully", "sadly", "happily", "surprisingly",
    # Absolutist quantifiers
    "always", "never", "everyone", "nobody", "everything", "nothing",
}

# Personal pronoun — used with single MPQA hit for softer penalty.
_PRONOUN_RE = re.compile(
    r"\b(I|me|my|mine|myself|we|us|our|ours|you|your|yours)\b",
    re.IGNORECASE,
)



class ClaimDetectionModule:
    """
    Detects claims using:
      1. Assertion verb heuristics (spaCy POS) — English only
      2. Named-entity presence
      3. Regex patterns (statistics, attribution phrases) — EN + FIL
      4. Subject-verb-object dependency structure — English only
      5. Opinion-marker penalty — EN + FIL
      6. Filipino raw-text verb scan (bypasses spaCy) — FIL only
      7. Falsifiable predicate gate (v2.2) — prevents entity-only false positives
    """

    # PATCH: Lowered from 0.40 → 0.30. At 0.40, valid factual claims were
    # silently dropped before retrieval ran (no claim = no evidence = Unverified).
    # v2.2 raised it to 0.40 to reduce false positives, but with a small corpus
    # the false-negative cost is higher. 0.30 restores sensitivity.
    CLAIM_THRESHOLD = 0.40  # v3.4: raised back from 0.30. At 0.30 this was compensating for corpus noise.
    # The v3.4 quality gate purge now removes narrative noise from the index, so
    # 0.40 can be restored for better precision without dropping real claims.

    # Minimum sentence length to even consider
    MIN_SENTENCE_CHARS = 15  # Lowered: short factual claims like 'X died in 2025' must pass

    # ── English ───────────────────────────────────────────────────────────────
    ASSERTION_VERBS = {
        "show", "prove", "confirm", "reveal", "demonstrate", "establish",
        "find", "discover", "indicate", "suggest", "report", "state",
        "announce", "claim", "say", "allege", "cause", "lead", "result",
        "increase", "decrease", "reduce", "prevent", "cure",
        # Death/birth/appointment — common short factual claim verbs
        "die", "born", "appoint", "elect", "win", "lose", "pass", "sign",
        "arrest", "resign", "kill", "sentence", "convict", "acquit",
    }

    CLAIM_PATTERNS = [
        r"\b\d+\s*%",
        r"\b\d+\s*(million|billion|thousand)",
        r"\baccording to\b",
        r"\bstudies?\s+show\b",
        r"\bscientists?\s+(say|find|confirm)\b",
        r"\bexperts?\s+(say|warn|confirm)\b",
        r"\bwho\s+(confirm|say|announce)\b",
        r"\bhas\s+been\s+(proven|confirmed|established)\b",
        r"\bcauses?\b.{0,50}\b(cancer|disease|death|harm)\b",
    ]

    OPINION_MARKERS_EN = [
        "i think", "i believe", "in my opinion", "i feel", "personally",
    ]

    # v2.2: falsifiable-predicate patterns — at least one of these must be
    # present for a sentence to qualify as a factual claim (vs. descriptive text).
    # Applied as an additional gate when no explicit CLAIM_PATTERNS match.
    _FALSIFIABLE_RE = re.compile(
        r"\b(increase[sd]?|decrease[sd]?|rose|fell|grew|declined|drop+ed?|"
        r"cause[sd]?|lead|led|result(?:ed)?|prove[sd]?|confirm(?:ed)?|"
        r"reveal(?:ed)?|show[sn]?|found|discover(?:ed)?|establish(?:ed)?|"
        r"announce[sd]?|report(?:ed)?|state[sd]?|said|claims?|allege[sd]?|"
        r"prevent(?:ed)?|cure[sd]?|reduce[sd]?|raise[sd]?|"
        r"die[sd]?|died|born|won|lost|appointed?|elected?|arrested?|"
        r"resigned?|killed?|convicted?|acquitted?|sentenced?|signed?)\b",
        re.IGNORECASE,
    )

    # ── Filipino / Tagalog ────────────────────────────────────────────────────
    ASSERTION_VERBS_FIL = {
        "napatunayan", "sinabi", "inihayag", "ipinahayag", "kinumpirma",
        "nagpapakita", "nagpapatunay", "idineklara", "nagbalita",
        "iniulat", "nagpaliwanag", "ipinagpaalam", "ibinahagi",
        "nagdudulot", "nagdulot", "nagbunga", "nagpapalakas",
        "nagpapahina", "nagpapataas", "nagpapababa",
        "ayon", "batay", "sang-ayon",
    }

    CLAIM_PATTERNS_FIL = [
        r"\bayon sa\b",
        r"\bbatay sa\b",
        r"\bsinabi ng\b",
        r"\bpinakita ng\b",
        r"\bkumpirmado ng\b",
        r"\bipinahayag ng\b",
        r"\bnapatunayan ng\b",
        r"\biniulat ng\b",
        r"\b\d+\s*(porsyento|porsiento|%)\b",
        r"\bmilyon\b",
        r"\bbilyon\b",
        r"\blibo\b",
        r"\bdaw\b",
        r"\bdaw ay\b",
        r"\bumano\b",
        r"\bumano'y\b",
    ]

    OPINION_MARKERS_FIL = [
        "sa tingin ko", "sa palagay ko", "sa akin", "sa aking palagay",
        "naniniwala ako", "sa aking opinyon", "personal na palagay",
    ]

    @classmethod
    def _has_filipino_verb(cls, text_lower: str) -> bool:
        for verb in cls.ASSERTION_VERBS_FIL:
            if verb in text_lower:
                return True
        return False

    @classmethod
    def _has_falsifiable_predicate(cls, text: str) -> bool:
        """
        v2.2: Return True if the sentence contains a verb that implies a
        verifiable state of the world. Used to gate sentences that would
        otherwise qualify via named entities + SVO structure only.
        """
        return bool(cls._FALSIFIABLE_RE.search(text))

    @classmethod
    def detect(cls, doc, text: str, article_keywords: list = None) -> List[Dict]:
        """
        article_keywords: optional list from PreprocessingModule.extract_keywords().
        Sentences referencing known article entities get a small context boost
        (+0.10), improving recall for short or Filipino claim sentences.
        """
        article_keywords = [kw.lower() for kw in (article_keywords or [])]
        claims    = []
        sentences = list(doc.sents)

        for i, sent in enumerate(sentences):
            score      = 0.0
            reasons    = []
            sent_text  = sent.text.strip()
            sent_lower = sent_text.lower()

            # v2.2: minimum character length
            if len(sent_text) < cls.MIN_SENTENCE_CHARS:
                continue

            if len(sent_text.split()) < 3:
                continue

            has_en_pattern  = False
            has_fil_pattern = False

            # ── Rule 1: English assertion verbs (spaCy POS) ──────────────────
            for token in sent:
                if token.lemma_.lower() in cls.ASSERTION_VERBS and token.pos_ == "VERB":
                    score += 0.25
                    reasons.append("assertion_verb_en")
                    break

            # ── Rule 2: Named entities ────────────────────────────────────────
            ents = list(sent.ents)
            if ents:
                score += 0.15 * min(len(ents), 2)
                reasons.append("named_entity")

            # ── Rule 3a: English regex patterns ──────────────────────────────
            for pattern in cls.CLAIM_PATTERNS:
                if re.search(pattern, sent_text, re.IGNORECASE):
                    score          += 0.30
                    has_en_pattern  = True
                    reasons.append("pattern_match_en")
                    break

            # ── Rule 3b: Filipino regex patterns ─────────────────────────────
            for pattern in cls.CLAIM_PATTERNS_FIL:
                if re.search(pattern, sent_lower, re.IGNORECASE):
                    score           += 0.30
                    has_fil_pattern  = True
                    reasons.append("pattern_match_fil")
                    break

            # ── Rule 4: English SVO structure (spaCy dependency) ─────────────
            has_nsubj = any(t.dep_ in ("nsubj", "nsubjpass") for t in sent)
            has_obj   = any(t.dep_ in ("dobj", "attr", "pobj") for t in sent)
            if has_nsubj and has_obj:
                score += 0.20
                reasons.append("svo_structure")

            # ── Rule 6: Filipino raw-text verb scan ───────────────────────────
            if cls._has_filipino_verb(sent_lower):
                score += 0.25
                reasons.append("assertion_verb_fil")

            # ── Rule 5: Opinion marker penalty (EN + FIL) ────────────────────
            all_opinion_markers = cls.OPINION_MARKERS_EN + cls.OPINION_MARKERS_FIL
            if any(m in sent_lower for m in all_opinion_markers):
                score -= 0.40
                reasons.append("opinion_marker")

            score = max(0.0, min(1.0, score))

            # v2.2: apply falsifiable-predicate gate.
            # A sentence that qualifies purely via named entities + SVO
            # (no explicit pattern match, no Filipino verb) must also contain
            # a falsifiable predicate — otherwise it is likely descriptive text,
            # not a factual claim.
            if (
                score >= cls.CLAIM_THRESHOLD
                and not has_en_pattern
                and not has_fil_pattern
                and "assertion_verb_en" not in reasons
                and "assertion_verb_fil" not in reasons
                and not cls._has_falsifiable_predicate(sent_text)
            ):
                score = max(0.0, score - 0.20)
                reasons.append("falsifiable_penalty")

            if score >= cls.CLAIM_THRESHOLD:
                claims.append({
                    "text":           sent_text,
                    "sentence_index": i,
                    "confidence":     round(score, 3),
                    "reasons":        reasons,
                })

        claims.sort(key=lambda x: x["confidence"], reverse=True)
        logger.debug(
            f"ClaimDetection: {len(claims)} claim(s) found "
            f"(threshold={cls.CLAIM_THRESHOLD}, cap={MAX_CLAIMS})"
        )
        return claims[:MAX_CLAIMS]