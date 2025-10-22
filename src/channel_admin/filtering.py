"""Content filtering utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(slots=True)
class WordFilter:
    """Simple word-based post filter."""

    banned_words: set[str]

    @classmethod
    def from_iterable(cls, words: Iterable[str]) -> "WordFilter":
        return cls({word.lower() for word in words})

    def is_allowed(self, text: str) -> bool:
        words = {token.strip(".,!?\"'\n\r\t ").lower() for token in text.split()}
        return not any(word in words for word in self.banned_words)

    def assert_allowed(self, text: str) -> None:
        if not self.is_allowed(text):
            raise ValueError("Post contains banned words and cannot be submitted")
