"""First names that skip titles (B8, Brian 2026-11-25: the talk bar said "Dr. 37%" for "Dr. ..."; titles in general).

first_name("Dr. Nina Okafor") -> "Nina";  first_name("Dr. Okafor") -> "Dr. Okafor" (a title and one word: keep both,
"Okafor" alone reads oddly);  first_name("Alex Rivera") -> "Alex". Used wherever a person's short name is shown or
matched (talk bar, People pages, duplicate suggestions, name patches in AI text), so past and future talks agree."""
from __future__ import annotations

TITLES = {"dr", "doctor", "mr", "mrs", "ms", "mx", "miss", "prof", "professor", "rev", "reverend", "pastor", "rabbi",
          "fr", "sir", "dame", "hon", "judge", "coach", "capt", "captain", "sgt", "lt", "col", "gen", "officer",
          "nurse", "dean", "sen", "rep", "gov"}


def is_title(word: str) -> bool:
    return word.rstrip(".").lower() in TITLES


def first_name(name: str | None) -> str:
    words = (name or "").split()
    if not words:
        return ""
    i = 0
    while i < len(words) - 1 and is_title(words[i]):
        i += 1
    if i == 0:
        return words[0]
    rest = words[i:]
    return rest[0] if len(rest) > 1 else " ".join(words[i - 1:])
