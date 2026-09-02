"""Safety helpers for educational vocabulary video generation."""

from __future__ import annotations

import re

# Sensitive vocabulary may still exist in a general language-learning source.
# For those entries we avoid turning the word into a stock-media search query.
_SENSITIVE_VISUAL_WORDS = {
    "alcohol", "beer", "casino", "cigarette", "drug", "firearm", "gambling",
    "gun", "knife", "lottery", "rifle", "tobacco", "weapon", "wine",
}


def safe_visual_term(word: str, proposed: str = "") -> str:
    tokens = set(re.findall(r"[a-z]+", str(word or "").casefold()))
    if tokens.intersection(_SENSITIVE_VISUAL_WORDS):
        return "English vocabulary lesson neutral background"
    return str(proposed or word or "English vocabulary lesson").strip()
