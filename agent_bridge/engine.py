"""The conversation engine: turn loop, stop handshake, interjections, resume.

UI-agnostic: it reports progress through `emit(Event)` callbacks, which both the
interactive app and the one-shot mode render.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import store
from .agents.base import Adapter, AgentError, TurnContext, TurnResult
from .config import Settings
from .presets import AGENTS, CUSTOM, NAMES, ROLE_PRESETS


@dataclass
class Event:
    kind: str  # run_start turn_start text tool turn_end retry user_msg notice done error
    agent: str | None = None
    text: str = ""
    data: dict = field(default_factory=dict)


def other_of(agent: str) -> str:
    return "agy" if agent == "claude" else "claude"


def resolve_roles(s: Settings) -> tuple[str, str]:
    base = ROLE_PRESETS.get(s.preset, ROLE_PRESETS["build-review"])
    return (s.role_a or base[1]), (s.role_b or base[2])


def briefing(me: str, topic: str, role: str, stop: str, perms: str, max_words: int) -> str:
    other = other_of(me)
    tools = ("Do NOT use any tools; just think and write." if perms == "none" else
             "You may read/edit files in the current working directory (shared with your partner) when useful"
             + ("." if perms == "edits" else ", and run commands such as tests."))
    return f"""You are {NAMES[me]}, in a live conversation with another AI agent, {NAMES[other]}.
An automated bridge relays messages between you. The human running it may occasionally
add messages marked "The user says:" — treat those as priority instructions for both of you.

TOPIC / TASK:
{topic}

YOUR ROLE: {role}

RULES:
- Address {NAMES[other]} directly. Be substantive and specific; don't just agree or flatter.
- Keep each message under ~{max_words} words.
- {tools}
- When the task is genuinely complete and you both agree, end your message with the
  token {stop} on its own line. If your partner sent {stop} but you disagree, say why
  and do NOT include the token.
"""


def has_stop(msg: str, stop: str) -> bool:
    return any(line.strip() == stop for line in msg.strip().splitlines()[-5:])


def new_state(s: Settings, topic: str) -> dict:
    run_dir = store.new_run_dir()
    workdir = Path(s.workdir).expanduser().resolve() if s.workdir else run_dir / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    ra, rb = resolve_roles(s)
    first, second = s.first, other_of(s.first)
    agents = {first: {"role": ra}, second: {"role": rb}}
    for a in AGENTS:
        agents[a].update(started=False, session=None, model=getattr(s, f"{a}_model"),
                         effort=getattr(s, f"{a}_effort"), cost=0.0, tokens=0)
    agents["claude"]["session"] = str(uuid.uuid4())  # claude accepts a preset ID
    state = {
        "version": 2, "created": dt.datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir), "workdir": str(workdir), "topic": topic,
        "preset": s.preset, "perms": s.perms, "max_turns": s.turns, "turn": 0,
        "next": first, "last_msg": None, "last_speaker": None, "stop": s.stop_token,
        "stop_pending": None, "finished": False, "timeout": s.timeout,
        "retries": s.retries, "max_words": s.max_words, "pending_user": [], "agents": agents,
    }
    store.save_state(state)
    store.append_transcript(state, (
        f"# Agent Bridge — {run_dir.name}\n\n**Topic:** {topic}\n\n"
        f"- {NAMES[first]}: {ra}\n- {NAMES[second]}: {rb}\n- mode: {s.preset} · permissions: {s.perms}"
        f" · workdir: {workdir}\n"))
    return state


class Engine:
    retry_backoff = 5.0

    def __init__(self, state: dict, adapters: dict[str, Adapter], emit: Callable[[Event], None] | None = None):
        self.state = state
        self.adapters = adapters
        self.emit = emit or (lambda e: None)
        self.current: tuple[str, float] | None = None  # (agent, start time) while a call runs
        self.running = False
        self._pause = False
        self._in_turn = False

    @classmethod
    def new(cls, s: Settings, topic: str, adapters, emit=None) -> "Engine":
        return cls(new_state(s, topic), adapters, emit)

    @classmethod
    def load(cls, run_dir, adapters, emit=None) -> "Engine":
        return cls(store.load_state(run_dir), adapters, emit)

    # ---- properties -------------------------------------------------------
    @property
    def run_dir(self) -> Path:
        return Path(self.state["run_dir"])

    @property
    def cost(self) -> float:
        return sum(self.state["agents"][a].get("cost", 0) or 0 for a in AGENTS)

    def save(self) -> None:
        store.save_state(self.state)

    # ---- controls (safe to call while running) -----------------------------
    def add_user_message(self, text: str) -> None:
        self.state["pending_user"].append(text)
        self.state["stop_pending"] = None
        store.append_transcript(self.state, f"\n> **You:** {text}\n")
        self.save()
        nxt = self.state["next"]
        to = other_of(nxt) if self._in_turn else nxt  # mid-turn notes go to the partner who replies next
        self.emit(Event("user_msg", text=text, data={"to": to}))

    def request_pause(self) -> None:
        self._pause = True

    def extend(self, n: int) -> None:
        """Allow n more turns from now and clear any 'finished' state."""
        s = self.state
        s["max_turns"] = max(s["max_turns"], s["turn"] + n)
        s["finished"] = False
        s["stop_pending"] = None
        self.save()

    def set_max_turns(self, n: int) -> int:
        s = self.state
        floor = s["turn"] + (1 if self.running else 0)
        s["max_turns"] = max(n, floor)
        self.save()
        return s["max_turns"]

    def set_agent(self, agent: str, **kw) -> None:
        self.state["agents"][agent].update(kw)
        self.save()

    # ---- the loop ---------------------------------------------------------
    def _prompt(self, me: str) -> tuple[str, str]:
        s = self.state
        other = other_of(me)
        a = s["agents"][me]
        brief = briefing(me, s["topic"], a["role"], s["stop"], s["perms"], s["max_words"])
        notes = "".join(f"The user says:\n{m}\n\n" for m in s["pending_user"])
        last = s["last_msg"]
        if not a["started"]:
            body = (f"You speak first. Open the conversation with {NAMES[other]}." if last is None
                    else f"{NAMES[other]} said:\n\n{last}\n\nYour reply:")
            return brief + "\n" + notes + body, brief
        if last is not None and s.get("last_speaker") in (None, other):
            body = f"{NAMES[other]} said:\n\n{last}\n\nYour reply:"
        else:
            body = f"Continue the conversation with {NAMES[other]}."
        return notes + body, brief

    async def _take_turn(self) -> tuple[str, str]:
        self._in_turn = True
        try:
            return await self._do_turn()
        finally:
            self._in_turn = False

    async def _do_turn(self) -> tuple[str, str]:
        s = self.state
        me = s["next"]
        a = s["agents"][me]
        used = len(s["pending_user"])  # messages typed during this turn go to the next one
        prompt, brief = self._prompt(me)
        label = f"Turn {s['turn'] + 1}/{s['max_turns']}"
        self.emit(Event("turn_start", me, label))
        ctx = TurnContext(workdir=Path(s["workdir"]), perms=s["perms"], timeout=s["timeout"], system_prompt=brief)
        t0 = time.monotonic()
        res: TurnResult | None = None
        for attempt in range(1, s["retries"] + 2):
            self.current = (me, time.monotonic())
            try:
                res = await self.adapters[me].send(
                    a, prompt, ctx,
                    lambda t: self.emit(Event("text", me, t)),
                    lambda t: self.emit(Event("tool", me, t)))
                if not res.text.strip():
                    raise AgentError("empty reply")
                break
            except AgentError as e:
                if attempt > s["retries"]:
                    raise
                self.emit(Event("retry", me, str(e), {"attempt": attempt}))
                await asyncio.sleep(self.retry_backoff * attempt)
            finally:
                self.current = None
        assert res is not None
        reply = res.text.strip()
        a["cost"] = (a.get("cost") or 0) + (res.cost or 0)
        a["tokens"] = (a.get("tokens") or 0) + (res.tokens or 0)
        secs = time.monotonic() - t0
        self.emit(Event("turn_end", me, data={"secs": secs, "meta": res.meta, "reply": reply}))
        store.append_transcript(s, f"\n## {label} · {NAMES[me]}\n\n{reply}\n")
        s["pending_user"] = s["pending_user"][used:]
        s["last_msg"], s["last_speaker"] = reply, me
        s["turn"] += 1
        s["next"] = other_of(me)
        self.save()
        return me, reply

    async def run(self) -> str:
        """Run until agreement, turn limit, pause, error or cancellation.
        Returns the reason: agreed | limit | paused | error | interrupted."""
        s = self.state
        self._pause = False
        self.running = True
        self.emit(Event("run_start", data={"run_dir": s["run_dir"], "workdir": s["workdir"]}))
        reason = "limit"
        try:
            while s["turn"] < s["max_turns"]:
                if self._pause:
                    reason = "paused"
                    break
                speaker, reply = await self._take_turn()
                if has_stop(reply, s["stop"]):
                    if s["stop_pending"] and s["stop_pending"] != speaker:
                        s["finished"] = True
                        reason = "agreed"
                        break
                    s["stop_pending"] = speaker
                    if s["turn"] >= s["max_turns"]:
                        s["max_turns"] += 1
                        self.emit(Event("notice", text="Granting one extra turn so the partner can confirm."))
                else:
                    s["stop_pending"] = None
                self.save()
        except asyncio.CancelledError:
            reason = "interrupted"
            self._finish(reason)
            raise
        except AgentError as e:
            reason = "error"
            self.emit(Event("error", text=str(e)))
        self._finish(reason)
        return reason

    def _finish(self, reason: str) -> None:
        self.running = False
        self.current = None
        self.save()
        if reason == "agreed":
            store.append_transcript(self.state, f"\n---\n_Both agents agreed the task is done "
                                                f"(turn {self.state['turn']})._\n")
        self.emit(Event("done", data={"reason": reason, "turn": self.state["turn"],
                                      "max_turns": self.state["max_turns"], "run_dir": self.state["run_dir"],
                                      "cost": self.cost}))
