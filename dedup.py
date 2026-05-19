from __future__ import annotations

import hashlib
import re
import unicodedata


def normalize(text: str) -> str:
    """Strip non-word characters, apply NFKC normalization, and lowercase."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w]", "", text, flags=re.UNICODE)
    return text.lower()


def text_hash(text: str) -> str:
    """Return the SHA-256 hex digest of normalized text."""
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def trigrams(text: str) -> set[str]:
    """Extract the set of character trigrams from normalized text."""
    normalized = normalize(text)
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[i : i + 3] for i in range(len(normalized) - 2)}


def similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts using trigram sets."""
    tg_a = trigrams(text_a)
    tg_b = trigrams(text_b)
    if not tg_a or not tg_b:
        return 0.0
    intersection = len(tg_a & tg_b)
    union = len(tg_a | tg_b)
    return intersection / union if union > 0 else 0.0


class DedupEngine:
    """Detect duplicate and near-duplicate messages via hash matching and trigram similarity."""

    SIMILARITY_THRESHOLD = 0.85

    def __init__(self) -> None:
        """Initialize empty hash set, recent text buffer, and keyword blocklist."""
        self._hashes: set[str] = set()
        self._recent_texts: list[str] = []
        self._max_recent = 200
        self._keyword_blocklist: list[str] = []

    def set_blocklist(self, keywords: list[str]) -> None:
        """Replace the keyword blocklist with normalized, non-empty entries."""
        self._keyword_blocklist = [k.lower().strip() for k in keywords if k.strip()]

    def _is_blocked_by_keyword(self, text: str) -> bool:
        """Return True if text contains two or more blocklist keywords."""
        if not self._keyword_blocklist:
            return False
        text_lower = text.lower()
        matched = sum(1 for kw in self._keyword_blocklist if kw in text_lower)
        return matched >= 2

    def rebuild_from_texts(self, texts: list[str]) -> None:
        """Reconstruct the hash set and recent text buffer from existing texts."""
        self._hashes.clear()
        self._recent_texts.clear()
        for t in texts:
            h = text_hash(t)
            self._hashes.add(h)
        self._recent_texts = texts[-self._max_recent:]

    def check_and_add(self, text: str) -> bool:
        """Check whether text is unique and record it if so.

        Rejects the text (returns False) if it matches the keyword blocklist,
        has an identical hash to a previously seen message, or exceeds the
        trigram similarity threshold against any recent text.
        """
        if self._is_blocked_by_keyword(text):
            return False

        h = text_hash(text)
        if h in self._hashes:
            return False

        for recent in self._recent_texts:
            if similarity(text, recent) >= self.SIMILARITY_THRESHOLD:
                return False

        self._hashes.add(h)
        self._recent_texts.append(text)
        if len(self._recent_texts) > self._max_recent:
            self._recent_texts.pop(0)
        return True

    def remove_hash(self, text: str) -> None:
        """Remove the hash for a given text from the dedup set."""
        self._hashes.discard(text_hash(text))

    def reset(self) -> None:
        """Clear all hashes and recent texts."""
        self._hashes.clear()
        self._recent_texts.clear()
