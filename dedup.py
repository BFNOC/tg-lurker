from __future__ import annotations

import hashlib
import re
import unicodedata


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w]", "", text, flags=re.UNICODE)
    return text.lower()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def trigrams(text: str) -> set[str]:
    normalized = normalize(text)
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[i : i + 3] for i in range(len(normalized) - 2)}


def similarity(text_a: str, text_b: str) -> float:
    tg_a = trigrams(text_a)
    tg_b = trigrams(text_b)
    if not tg_a or not tg_b:
        return 0.0
    intersection = len(tg_a & tg_b)
    union = len(tg_a | tg_b)
    return intersection / union if union > 0 else 0.0


class DedupEngine:
    SIMILARITY_THRESHOLD = 0.85

    def __init__(self) -> None:
        self._hashes: set[str] = set()
        self._recent_texts: list[str] = []
        self._max_recent = 200
        self._keyword_blocklist: list[str] = []

    def set_blocklist(self, keywords: list[str]) -> None:
        self._keyword_blocklist = [k.lower().strip() for k in keywords if k.strip()]

    def _is_blocked_by_keyword(self, text: str) -> bool:
        if not self._keyword_blocklist:
            return False
        text_lower = text.lower()
        matched = sum(1 for kw in self._keyword_blocklist if kw in text_lower)
        return matched >= 2

    def rebuild_from_texts(self, texts: list[str]) -> None:
        self._hashes.clear()
        self._recent_texts.clear()
        for t in texts:
            h = text_hash(t)
            self._hashes.add(h)
        self._recent_texts = texts[-self._max_recent:]

    def check_and_add(self, text: str) -> bool:
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

    def reset(self) -> None:
        self._hashes.clear()
        self._recent_texts.clear()
