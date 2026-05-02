"""
corpus/simhash.py — Lightweight SimHash for near-duplicate detection.

Replaces the simple hash(text[:120]) deduplication in scraper.py with
SimHash, which catches syndicated near-duplicates (same story, slightly
different wording) that slip through exact-hash deduplication.

Algorithm (Charikar, 2002)
--------------------------
1. Tokenise the text into n-grams (bigrams by default).
2. Hash each token with a standard hash to a 64-bit integer.
3. For each bit position b (0..63):
     if bit b of hash(token) is 1  → add +token_weight to column b
     else                          → add -token_weight to column b
4. Collapse: bit b of SimHash = 1 if column b > 0 else 0.
5. Two texts are near-duplicates if hamming(simhash_a, simhash_b) <= threshold.

Typical threshold: 3 bits out of 64 → catches ~95 % of syndicated copies.
"""

from __future__ import annotations
import re
import hashlib
from typing import Set


# ── Tokeniser ─────────────────────────────────────────────────────────────────

def _tokenize(text: str, ngram: int = 2) -> list[str]:
    """
    Lowercase, strip non-alphanumeric, split into word tokens,
    then produce character n-grams from each word for better resilience
    to minor wording changes (word insertions, paraphrase).
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    tokens: list[str] = []
    for w in words:
        if len(w) <= ngram:
            tokens.append(w)
        else:
            tokens.extend(w[i:i + ngram] for i in range(len(w) - ngram + 1))
    return tokens


# ── SimHash core ──────────────────────────────────────────────────────────────

BITS = 64


def simhash(text: str) -> int:
    """
    Compute a 64-bit SimHash fingerprint for *text*.
    Returns an integer in [0, 2^64).
    """
    if not text:
        return 0

    tokens = _tokenize(text)
    if not tokens:
        return 0

    # 64-column vector, integer arithmetic
    v = [0] * BITS

    for token in tokens:
        # Use MD5 first 8 bytes → 64-bit hash (faster than sha256 for this)
        h = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
        for b in range(BITS):
            if (h >> b) & 1:
                v[b] += 1
            else:
                v[b] -= 1

    result = 0
    for b in range(BITS):
        if v[b] > 0:
            result |= (1 << b)
    return result


def hamming(a: int, b: int) -> int:
    """Count differing bits between two 64-bit SimHash values."""
    return bin(a ^ b).count("1")


# ── Near-duplicate store ──────────────────────────────────────────────────────

class SimHashStore:
    """
    Thread-unsafe in-process deduplication store.
    Suitable for single-threaded scraper runs.

    Usage:
        store = SimHashStore(threshold=3)
        for sentence in sentences:
            if store.is_duplicate(sentence):
                continue          # skip near-duplicate
            store.add(sentence)   # first occurrence — keep
    """

    def __init__(self, threshold: int = 3):
        """
        threshold — max Hamming distance to consider two texts near-duplicate.
        3 bits (default) catches ~95 % of syndicated near-copies in practice.
        Raise to 5 for looser matching; lower to 1 for stricter.
        """
        self.threshold = threshold
        self._hashes: list[int] = []

    def is_duplicate(self, text: str) -> bool:
        sh = simhash(text)
        for stored in self._hashes:
            if hamming(sh, stored) <= self.threshold:
                return True
        return False

    def add(self, text: str) -> None:
        self._hashes.append(simhash(text))

    def add_if_unique(self, text: str) -> bool:
        """
        Add text if it is not a near-duplicate of any stored text.
        Returns True if added (unique), False if rejected (duplicate).
        """
        sh = simhash(text)
        for stored in self._hashes:
            if hamming(sh, stored) <= self.threshold:
                return False
        self._hashes.append(sh)
        return True

    def __len__(self) -> int:
        return len(self._hashes)


# ── Convenience function ──────────────────────────────────────────────────────

def deduplicate_sentences(sentences: list[str],
                          store: SimHashStore | None = None,
                          threshold: int = 3) -> list[str]:
    """
    Filter *sentences* removing near-duplicates.
    If *store* is provided, it is updated in-place (shared state across calls).
    Returns the unique sentences in order.
    """
    if store is None:
        store = SimHashStore(threshold=threshold)
    unique = []
    for s in sentences:
        if store.add_if_unique(s):
            unique.append(s)
    return unique
