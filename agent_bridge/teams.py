"""Saved teams: a named snapshot of roles, models and run settings (~/.config/agent-bridge/teams/*.toml)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import fields
from pathlib import Path

from . import config
from .config import Settings
from .presets import AGENTS, NAMES
from .roles import CUSTOM, TEAMS, role_name

# What a team captures. Not the project folder, timeouts or notifications: a team should work anywhere.
TEAM_FIELDS = (
    "preset", "first", "role_a", "role_b",
    "claude_role", "claude_role_text", "agy_role", "agy_role_text",
    "claude_model", "claude_effort", "agy_model", "agy_effort",
    "perms", "turns", "max_words", "verify", "checkpoints",
    "progress", "progress_every", "progress_keeper", "judge", "auto_verdict",
)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RESERVED = {*TEAMS, CUSTOM, "save", "delete", "list"}


def teams_dir() -> Path:
    return config.config_dir() / "teams"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and name not in RESERVED


def list_teams() -> list[str]:
    d = teams_dir()
    return sorted(p.stem for p in d.glob("*.toml")) if d.is_dir() else []


def save_team(name: str, s: Settings) -> Path:
    d = teams_dir()
    d.mkdir(parents=True, exist_ok=True)
    snap = Settings()
    for f in TEAM_FIELDS:
        setattr(snap, f, getattr(s, f))
    body = config.dumps(snap).splitlines()
    keep = [ln for ln in body if ln.split(" = ", 1)[0] in TEAM_FIELDS]
    path = d / f"{name}.toml"
    path.write_text(f"# agent-bridge team '{name}': {describe(s)}\n# Load with /mode {name} or --team {name}\n\n"
                    + "\n".join(keep) + "\n")
    return path


def load_team(name: str) -> dict:
    path = teams_dir() / f"{name}.toml"
    data = tomllib.loads(path.read_text())
    return {k: v for k, v in data.items() if k in TEAM_FIELDS}


def apply_team(s: Settings, data: dict) -> list[str]:
    """Copy a saved team onto settings (type-checked); returns warnings."""
    types = {f.name: str(f.type) for f in fields(Settings)}
    warn = []
    for k, v in data.items():
        expected = {"int": int, "bool": bool}.get(types.get(k, "str"), str)
        if not isinstance(v, expected) or (expected is int and isinstance(v, bool)):
            warn.append(f"team: {k} has the wrong type; ignored")
            continue
        setattr(s, k, v)
    return warn + s.validate()


def delete_team(name: str) -> bool:
    path = teams_dir() / f"{name}.toml"
    if path.exists():
        path.unlink()
        return True
    return False


def describe(s: Settings) -> str:
    from .engine import role_for
    parts = []
    for a in AGENTS:
        model = getattr(s, f"{a}_model") or "default"
        parts.append(f"{NAMES[a]}: {role_name(role_for(s, a)[0])} ({model})")
    parts.append(f"perms {s.perms}")
    parts.append(f"{s.turns} turns")
    if s.verify:
        parts.append(f"verify `{s.verify}`")
    return " · ".join(parts)


def describe_saved(name: str) -> str:
    try:
        s = Settings()
        apply_team(s, load_team(name))
        return describe(s)
    except (OSError, tomllib.TOMLDecodeError):
        return "(unreadable file)"
