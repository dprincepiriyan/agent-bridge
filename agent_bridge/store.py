"""Run folders: state.json, transcript.md, migration of v0.1 runs."""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from . import config
from .presets import AGENTS


def runs_dir() -> Path:
    return config.data_dir() / "runs"


def new_run_dir() -> Path:
    base = runs_dir()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d, n = base / stamp, 2
    while d.exists():
        d, n = base / f"{stamp}-{n}", n + 1
    d.mkdir(parents=True)
    return d


def save_state(state: dict) -> None:
    p = Path(state["run_dir"]) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def append_transcript(state: dict, text: str) -> None:
    with open(Path(state["run_dir"]) / "transcript.md", "a") as f:
        f.write(text)


def transcript_path(state: dict) -> Path:
    return Path(state["run_dir"]) / "transcript.md"


SCHEMA = 3

V3_DEFAULTS = {
    "pending_notes": [], "idle_timeout": 600, "verify": "", "verify_timeout": 900, "checkpoints": True,
    "checkpoint_ok": None, "checkpoint_log": [], "own_workspace": False, "judge": "claude",
    "auto_verdict": True, "last_verify": None, "interrupted": None,
    # shared progress memory; off for runs created before it existed
    "progress": False, "progress_every": 4, "progress_keeper": "auto", "progress_last_shown": None,
    "progress_hash": None, "progress_hash_turn": 0,
    "budget": {"minutes": 0, "turns": 0, "usage": 0, "cost": 0.0}, "active_secs": 0.0,
}


def upgrade(state: dict) -> dict:
    """Bring an older state.json up to the current schema."""
    if state.get("version", 1) < 2:
        for a in AGENTS:
            ag = state["agents"][a]
            ag.setdefault("model", state.pop(f"{a}_model", None) or "")
            ag.setdefault("effort", "")
            ag.setdefault("cost", 0.0)
            ag.setdefault("tokens", 0)
        state.setdefault("pending_user", [])
        state.setdefault("last_speaker", None)
        state["version"] = 2
    for k, v in V3_DEFAULTS.items():  # always: fills fields for new and old states alike
        state.setdefault(k, list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
    if state["version"] < SCHEMA:
        # v0.1/0.2 per-run workspaces live inside the run dir
        state["own_workspace"] = state["own_workspace"] or str(state.get("workdir", "")).startswith(
            str(state.get("run_dir", "\0")))
        state["version"] = SCHEMA
    return state


def load_state(run_dir: Path) -> dict:
    run_dir = Path(run_dir).expanduser().resolve()
    sp = run_dir / "state.json"
    if not sp.exists():
        raise FileNotFoundError(f"no state.json in {run_dir}")
    state = upgrade(json.loads(sp.read_text()))
    state["run_dir"] = str(run_dir)
    return state


def list_runs(limit: int | None = None) -> list[dict]:
    out = []
    base = runs_dir()
    if not base.exists():
        return out
    for d in sorted(base.iterdir(), reverse=True):
        sp = d / "state.json"
        if not sp.is_file():
            continue
        try:
            s = json.loads(sp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "dir": d, "name": d.name, "topic": s.get("topic", ""),
            "turn": s.get("turn", 0), "max_turns": s.get("max_turns", 0),
            "finished": s.get("finished", False), "preset": s.get("preset", ""),
            "last_speaker": s.get("last_speaker"), "last_msg": s.get("last_msg") or "",
        })
        if limit and len(out) >= limit:
            break
    return out


def run_when(name: str) -> str:
    """'20261121-212534' → 'Today 21:25' / 'Yesterday 21:25' / 'Sat 21 Nov 21:25'."""
    try:
        t = dt.datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return name
    days = (dt.date.today() - t.date()).days
    day = "Today" if days == 0 else "Yesterday" if days == 1 else t.strftime("%a %d %b" if days < 300 else "%d %b %Y")
    return f"{day} {t:%H:%M}"


def run_summary(r: dict, width: int = 70) -> str:
    """One line: mode · progress · topic."""
    topic = " ".join((r.get("topic") or "").split())
    topic = topic if len(topic) <= width else topic[: width - 1] + "…"
    status = "✔ done" if r.get("finished") else f"{r.get('turn', 0)}/{r.get('max_turns', 0)} turns"
    return " · ".join(x for x in (r.get("preset"), status, topic) if x)


def search_runs(query: str, runs: list[dict]) -> list[dict]:
    """Runs whose name, topic or mode contain every word of query."""
    words = query.lower().split()
    out = []
    for r in runs:
        hay = f"{r['name']} {r.get('preset', '')} {r.get('topic', '')}".lower()
        if all(w in hay for w in words):
            out.append(r)
    return out


def legacy_dirs() -> list[Path]:
    cands = {Path(__file__).resolve().parent.parent / "runs", Path.home() / "agent-bridge" / "runs"}
    return [p for p in cands if p.is_dir() and p.resolve() != runs_dir().resolve()]


def migrate_legacy() -> int:
    """Move v0.1 runs (<repo>/runs) into the data dir. Returns number moved."""
    moved = 0
    for old in legacy_dirs():
        for d in sorted(old.iterdir()):
            if not (d / "state.json").is_file():
                continue
            target = runs_dir() / d.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(target))
            sp = target / "state.json"
            try:
                raw = sp.read_text().replace(str(d), str(target))
                sp.write_text(raw)
            except OSError:
                pass
            moved += 1
        try:
            old.rmdir()  # only succeeds if empty
        except OSError:
            pass
    return moved
