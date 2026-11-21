"""Turn what the user typed ("Gemini 4 Argon", "argon high", an exact ID) into model IDs."""
from __future__ import annotations

import re


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s()\-_/.,]+", text.lower()) if w]


def match_models(query: str, models: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the (id, label) pairs matching query, best match first.

    Exact ID or label → that one model. Otherwise every word of the query must appear
    in the model's ID or label ("argon high" → gemini-4-argon-high).
    """
    q = query.strip().lower()
    if not q:
        return []
    for mid, label in models:
        if q == mid.lower() or q == label.lower():
            return [(mid, label)]
    words = _words(q)
    hits = []
    for mid, label in models:
        hay = set(_words(mid)) | set(_words(label))
        if all(any(h == w or h.startswith(w) for h in hay) for w in words):
            hits.append((mid, label))
    return hits
