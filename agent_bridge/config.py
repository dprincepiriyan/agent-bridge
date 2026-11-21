"""User defaults (~/.config/agent-bridge/config.toml) and standard paths."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .presets import AGENTS, EFFORTS, MODES, PERMS, STOP_DEFAULT


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "agent-bridge"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "agent-bridge"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "agent-bridge"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "agent-bridge"


def config_file() -> Path:
    return config_dir() / "config.toml"


@dataclass
class Settings:
    preset: str = "build-review"
    role_a: str = ""          # custom role for the first speaker (overrides preset)
    role_b: str = ""          # custom role for the second speaker
    first: str = "claude"
    turns: int = 8
    perms: str = "none"
    workdir: str = ""         # "" = isolated per-run workspace
    timeout: int = 600
    retries: int = 2
    max_words: int = 350
    stop_token: str = STOP_DEFAULT
    claude_model: str = ""    # "" = CLI default
    claude_effort: str = ""
    agy_model: str = ""
    agy_effort: str = ""

    def validate(self) -> list[str]:
        """Fix invalid values in place; return warnings."""
        warn = []
        defaults = Settings()

        def reset(name: str, why: str):
            warn.append(f"config: {name}={getattr(self, name)!r} {why}; using {getattr(defaults, name)!r}")
            setattr(self, name, getattr(defaults, name))

        if self.preset not in MODES:
            reset("preset", "is not a known mode")
        if self.first not in AGENTS:
            reset("first", "must be claude or agy")
        if self.perms not in PERMS:
            reset("perms", "is not a permission level")
        for a in AGENTS:
            if getattr(self, f"{a}_effort") not in ("",) + EFFORTS:
                reset(f"{a}_effort", "is not an effort level")
        for name in ("turns", "timeout", "max_words"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                reset(name, "must be a positive integer")
        if not isinstance(self.retries, int) or self.retries < 0:
            reset("retries", "must be >= 0")
        if not self.stop_token.strip():
            reset("stop_token", "must not be empty")
        return warn


def load() -> tuple[Settings, list[str]]:
    s = Settings()
    path = config_file()
    if not path.exists():
        return s, []
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as e:
        return s, [f"config: could not read {path}: {e}; using defaults"]
    warn = []
    types = {f.name: f.type for f in fields(Settings)}
    for k, v in data.items():
        if k not in types:
            warn.append(f"config: unknown key {k!r} ignored")
            continue
        expected = int if types[k] in ("int", int) else str
        if not isinstance(v, expected) or isinstance(v, bool):
            warn.append(f"config: {k} should be {expected.__name__}; ignored")
            continue
        setattr(s, k, v)
    warn += s.validate()
    return s, warn


def dumps(s: Settings) -> str:
    lines = ["# agent-bridge defaults. Edit freely or use /config save.", ""]
    for k, v in asdict(s).items():
        lines.append(f"{k} = {v if isinstance(v, int) else json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def save(s: Settings) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps(s))
    tmp.replace(path)
    return path
