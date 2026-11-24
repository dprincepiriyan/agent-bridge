"""Parse budget phrases like '30m', '2h', '1h30m', '50 turns', 'usage 80%', 'cost $5', 'off', 'time off'."""
from __future__ import annotations

import re

KINDS = ("minutes", "turns", "usage", "cost")
ALIASES = {"time": "minutes", "minutes": "minutes", "min": "minutes", "turns": "turns", "turn": "turns",
           "usage": "usage", "claude": "usage", "cost": "cost", "money": "cost"}


def _minutes(tok: str) -> int | None:
    m = re.fullmatch(r"(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?|s)?)?)?", tok)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def parse(text: str) -> dict[str, float | int]:
    """Return {kind: value} updates; 0 means off. Raises ValueError with a helpful message."""
    t = " ".join(text.lower().replace(",", " ").split())
    if t in ("off", "none", "clear", "reset"):
        return {k: 0 for k in KINDS}
    t = re.sub(r"(\d)\s+(h|hr|hrs|hours?|m|min|mins|minutes?)\b", r"\1\2", t)
    t = re.sub(r"(\d+h)\s+(\d+m)", r"\1\2", t)
    out: dict[str, float | int] = {}
    toks = t.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok in ALIASES:  # 'turns 50', 'usage 80%', 'cost $5', 'time 30m', 'time off'
            kind = ALIASES[tok]
            if nxt in ("off", "0", "none"):
                out[kind] = 0
                i += 2
                continue
            if not nxt:
                raise ValueError(f"'{tok}' needs a value, e.g. /budget {tok} " +
                                 {"minutes": "30m", "turns": "50", "usage": "80%", "cost": "5"}[kind])
            out.update(_value(kind, nxt))
            i += 2
            continue
        if nxt in ("turns", "turn", "t"):  # '50 turns'
            out.update(_value("turns", tok))
            i += 2
            continue
        if tok.endswith("%"):
            out.update(_value("usage", tok))
        elif tok.startswith("$"):
            out.update(_value("cost", tok))
        elif re.fullmatch(r"\d+t", tok):
            out.update(_value("turns", tok[:-1]))
        elif _minutes(tok) is not None:
            out["minutes"] = _minutes(tok)
        else:
            raise ValueError(f"Didn't understand '{tok}'. Examples: 30m · 2h · 50 turns · usage 80% · cost $5 · off")
        i += 1
    if not out:
        raise ValueError("Examples: /budget 30m · /budget 50 turns · /budget usage 80% · /budget cost $5 · /budget off")
    return out


def _value(kind: str, raw: str) -> dict[str, float | int]:
    try:
        if kind == "minutes":
            m = _minutes(raw) if not raw.isdigit() else int(raw)
            if m is None:
                raise ValueError
            return {"minutes": m}
        if kind == "turns":
            n = int(raw)
            if n < 0:
                raise ValueError
            return {"turns": n}
        if kind == "usage":
            n = int(raw.rstrip("%"))
            if not 0 <= n <= 100:
                raise ValueError
            return {"usage": n}
        v = float(raw.lstrip("$"))
        if v < 0:
            raise ValueError
        return {"cost": v}
    except ValueError:
        raise ValueError(f"Bad value '{raw}' for {kind}.") from None


def describe(b: dict) -> str:
    parts = []
    if b.get("minutes"):
        h, m = divmod(int(b["minutes"]), 60)
        parts.append(f"time {f'{h}h' if h else ''}{f'{m}m' if m or not h else ''}")
    if b.get("turns"):
        parts.append(f"{b['turns']} turns")
    if b.get("usage"):
        parts.append(f"Claude 5h usage {b['usage']}%")
    if b.get("cost"):
        parts.append(f"cost ${b['cost']:g}")
    return " · ".join(parts) or "no limits"
