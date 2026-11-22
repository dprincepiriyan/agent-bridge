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

from . import store, workspace
from .agents.base import Adapter, AgentError, ConfigError, TurnContext, TurnResult, UsageLimitError
from .config import Settings
from .presets import AGENTS, CUSTOM, NAMES, ROLE_PRESETS


@dataclass
class Event:
    # run_start turn_start text tool turn_end retry user_msg notice error done
    # checkpoint verify_start verify_end summary_start summary_end
    kind: str
    agent: str | None = None
    text: str = ""
    data: dict = field(default_factory=dict)


def other_of(agent: str) -> str:
    return "agy" if agent == "claude" else "claude"


def resolve_roles(s: Settings) -> tuple[str, str]:
    base = ROLE_PRESETS.get(s.preset, ROLE_PRESETS["build-review"])
    return (s.role_a or base[1]), (s.role_b or base[2])


def briefing(me: str, topic: str, role: str, stop: str, perms: str, max_words: int, verify: str = "") -> str:
    other = other_of(me)
    tools = ("Do NOT use any tools; just think and write." if perms == "none" else
             "You may read/edit files in the current working directory (shared with your partner) when useful"
             + ("." if perms == "edits" else ", and run commands such as tests."))
    check = (f"\n- Before the run counts as done, the bridge runs `{verify}` in the working directory.\n"
             f"  It must succeed (exit 0); if it fails you'll both get its output and must fix it." if verify else "")
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
  and do NOT include the token.{check}
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
        "version": store.SCHEMA, "created": dt.datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir), "workdir": str(workdir), "topic": topic,
        "preset": s.preset, "perms": s.perms, "max_turns": s.turns, "turn": 0,
        "next": first, "last_msg": None, "last_speaker": None, "stop": s.stop_token,
        "stop_pending": None, "finished": False, "timeout": s.timeout,
        "retries": s.retries, "max_words": s.max_words, "pending_user": [], "agents": agents,
        "idle_timeout": s.idle_timeout, "verify": s.verify, "verify_timeout": s.verify_timeout,
        "checkpoints": s.checkpoints, "own_workspace": not s.workdir, "judge": s.judge,
        "auto_verdict": s.auto_verdict,
    }
    store.upgrade(state)
    store.save_state(state)
    store.append_transcript(state, (
        f"# Agent Bridge — {run_dir.name}\n\n**Topic:** {topic}\n\n"
        f"- {NAMES[first]}: {ra}\n- {NAMES[second]}: {rb}\n- mode: {s.preset} · permissions: {s.perms}"
        f" · workdir: {workdir}\n" + (f"- verify: `{s.verify}`\n" if s.verify else "")))
    return state


VERDICT_PROMPT = """You are an impartial judge. You did not take part in the debate below between Claude and
Antigravity. Judge only the quality of the arguments: evidence, logic, and how well each side
answered the other — not which position you personally prefer.

Write in Markdown, under 300 words:
## Verdict
Who argued better and why (2–3 sentences). Pick a winner, or call a draw and say why.
## Strongest arguments
- **Claude:** …
- **Antigravity:** …
## Where they agreed
## Weak spots
One line per side: the claim that was least supported or never answered."""

SUMMARY_PROMPT = """You are reviewing a conversation between two AI agents, Claude and Antigravity, who worked on
the task below. You did not take part. Write a concise Markdown summary, under 300 words:
## Outcome
What was produced or decided, and whether the task looks complete.
## Key decisions
## Open issues / next steps
Be concrete; mention file names, commands, or numbers where the transcript has them."""

JUDGE_SYSTEM = "You are a careful, impartial reviewer. Do not use tools. Answer only with the requested Markdown."
MAX_TRANSCRIPT = 120_000


class Engine:
    retry_backoff = 5.0

    def __init__(self, state: dict, adapters: dict[str, Adapter], emit: Callable[[Event], None] | None = None):
        self.state = state
        self.adapters = adapters
        self.emit = emit or (lambda e: None)
        self.current: tuple[str, float] | None = None  # (agent|"verify", start time) while something runs
        self.running = False
        self._pause = False
        self._in_turn = False
        self._turn_cut = False

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
    def workdir(self) -> Path:
        return Path(self.state["workdir"])

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

    def add_note(self, text: str) -> None:
        """A message from the bridge itself (verification failures, reverts) for the next agent."""
        self.state["pending_notes"].append(text)
        self.save()

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

    def set(self, **kw) -> None:
        self.state.update(kw)
        self.save()

    # ---- prompts ----------------------------------------------------------
    def _prompt(self, me: str) -> tuple[str, str]:
        s = self.state
        other = other_of(me)
        a = s["agents"][me]
        brief = briefing(me, s["topic"], a["role"], s["stop"], s["perms"], s["max_words"], s.get("verify", ""))
        notes = ""
        intr = s.get("interrupted")
        if intr and intr.get("agent") == me and a["started"]:
            notes += ("(Your previous turn was interrupted before you finished, so the message below is being "
                      "re-sent. If you already see it above, answer it once — don't treat it as a new request.)\n\n")
        notes += "".join(f"The bridge reports:\n{m}\n\n" for m in s["pending_notes"])
        notes += "".join(f"The user says:\n{m}\n\n" for m in s["pending_user"])
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

    def _ctx(self, brief: str, perms: str | None = None, ephemeral: bool = False) -> TurnContext:
        s = self.state
        return TurnContext(workdir=self.workdir, perms=perms or s["perms"], timeout=s["timeout"],
                           system_prompt=brief, idle_timeout=s.get("idle_timeout", 600), ephemeral=ephemeral)

    # ---- one turn ---------------------------------------------------------
    async def _take_turn(self) -> tuple[str, str]:
        self._in_turn = True
        try:
            return await self._do_turn()
        except asyncio.CancelledError:
            self._turn_cut = True
            raise
        finally:
            self._in_turn = False

    async def _do_turn(self) -> tuple[str, str]:
        s = self.state
        me = s["next"]
        a = s["agents"][me]
        used_user, used_notes = len(s["pending_user"]), len(s["pending_notes"])
        prompt, brief = self._prompt(me)
        label = f"Turn {s['turn'] + 1}/{s['max_turns']}"
        self.emit(Event("turn_start", me, label))
        ctx = self._ctx(brief)
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
            except UsageLimitError as e:
                e.agent = me  # type: ignore[attr-defined]
                raise  # retrying can't help
            except ConfigError:
                raise  # wrong settings: retrying can't help either
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
        s["pending_user"] = s["pending_user"][used_user:]
        s["pending_notes"] = s["pending_notes"][used_notes:]
        s["last_msg"], s["last_speaker"] = reply, me
        s["interrupted"] = None
        s["turn"] += 1
        s["next"] = other_of(me)
        self.save()
        return me, reply

    # ---- checkpoints --------------------------------------------------------
    async def _prepare_checkpoints(self) -> None:
        s = self.state
        if not s.get("checkpoints") or s["perms"] == "none" or not workspace.git_available():
            return
        wd = self.workdir
        if not await workspace.is_repo(wd):
            if s.get("own_workspace") and await workspace.init_repo(wd):
                s["checkpoint_ok"] = True
                try:  # commit the default .gitignore (and anything pre-existing) as setup, not as a turn
                    await workspace.checkpoint(wd, f"agent-bridge: set up workspace\n\nRun {self.run_dir.name}")
                except RuntimeError:
                    pass
            else:
                if s.get("checkpoint_ok") is None:
                    self.emit(Event("notice", text="Workdir isn't a git repo, so per-turn checkpoints are off. "
                                                   "Run `git init` there to enable them."))
                s["checkpoint_ok"] = False
                return
        if s.get("checkpoint_ok") is None:
            if await workspace.is_dirty(wd):
                s["checkpoint_ok"] = False
                self.emit(Event("notice", text="Workdir has uncommitted changes, so per-turn checkpoints are off "
                                               "for this conversation (they'd mix with your work). Commit or stash, "
                                               "then /checkpoints on."))
                return
            s["checkpoint_ok"] = True
        if s["checkpoint_ok"]:
            await self._checkpoint(s["turn"], None, "changes made outside the conversation")
        self.save()

    async def _checkpoint(self, turn: int, agent: str | None, note: str = "") -> None:
        s = self.state
        if s.get("checkpoint_ok") is not True:
            return
        who = f" · {NAMES[agent]}" if agent else ""
        msg = f"agent-bridge: turn {turn}{who}{f' ({note})' if note else ''}\n\nRun {self.run_dir.name}"
        try:
            r = await workspace.checkpoint(self.workdir, msg)
        except RuntimeError as e:
            self.emit(Event("notice", text=f"Checkpoint failed: {e}"))
            return
        if r:
            sha, stat = r
            s["checkpoint_log"].append({"turn": turn, "agent": agent, "sha": sha, "stat": stat,
                                        "note": note, "reverted": False})
            self.emit(Event("checkpoint", agent, sha, {"stat": stat, "turn": turn, "note": note}))
            self.save()

    async def undo(self, turn: int | None = None) -> tuple[bool, str]:
        """Revert the file changes of one checkpointed turn (the latest by default)."""
        s = self.state
        live = [e for e in s.get("checkpoint_log", []) if not e.get("reverted")]
        if turn is not None:
            live = [e for e in live if e["turn"] == turn]
        if not live:
            return False, "No checkpoint to undo" + (f" for turn {turn}." if turn is not None else ".")
        e = live[-1]
        ok, out = await workspace.revert(self.workdir, e["sha"])
        if not ok:
            return False, f"git revert {e['sha']} failed (later turns may have changed the same lines):\n{out[-800:]}"
        e["reverted"] = True
        who = NAMES.get(e.get("agent") or "", "someone")
        self.add_note(f"The user reverted the file changes from turn {e['turn']} by {who} (commit {e['sha']}). "
                      f"Re-read the affected files before continuing.")
        store.append_transcript(s, f"\n> **You:** reverted turn {e['turn']} ({who}, {e['sha']})\n")
        self.save()
        return True, f"Reverted turn {e['turn']} ({who}, {e['sha']}{', ' + e['stat'] if e.get('stat') else ''})."

    # ---- verification -------------------------------------------------------
    async def verify_now(self) -> tuple[bool, int, str]:
        s = self.state
        cmd = s["verify"]
        self.emit(Event("verify_start", text=cmd))
        self.current = ("verify", time.monotonic())
        try:
            code, out = await workspace.run_verify(cmd, self.workdir, s.get("verify_timeout", 900))
        finally:
            self.current = None
        ok = code == 0
        out = workspace.tail(out)
        self.emit(Event("verify_end", text=out, data={"ok": ok, "code": code, "cmd": cmd}))
        store.append_transcript(s, f"\n### Verification `{cmd}` → {'passed' if ok else f'FAILED (exit {code})'}\n\n"
                                   f"```\n{out}\n```\n")
        s["last_verify"] = {"ok": ok, "code": code, "turn": s["turn"]}
        self.save()
        return ok, code, out

    async def _verify_after_agreement(self) -> bool:
        s = self.state
        ok, code, out = await self.verify_now()
        if not ok:
            s["stop_pending"] = None
            self.add_note(f"You both said the task is done, but the verification command `{s['verify']}` FAILED "
                          f"(exit {code}). Last output:\n```\n{out}\n```\nFix the problem, and only send "
                          f"{s['stop']} again once it passes.")
        return ok

    # ---- verdict / summary --------------------------------------------------
    async def summarize(self, judge: str | None = None) -> str | None:
        s = self.state
        judge = judge or s.get("judge") or "claude"
        transcript = store.transcript_path(s).read_text()
        if len(transcript) > MAX_TRANSCRIPT:
            transcript = (transcript[:8000] + "\n\n[… middle of the transcript omitted for length …]\n\n"
                          + transcript[-(MAX_TRANSCRIPT - 8000):])
        debate = s.get("preset") == "debate"
        title = "Verdict" if debate else "Summary"
        prompt = (f"{VERDICT_PROMPT if debate else SUMMARY_PROMPT}\n\nTOPIC:\n{s['topic']}\n\n"
                  f"TRANSCRIPT:\n<transcript>\n{transcript}\n</transcript>")
        src = s["agents"][judge]
        agent = {"started": False, "session": None, "model": src.get("model", ""), "effort": src.get("effort", "")}
        self.emit(Event("summary_start", judge, title))
        self.current = (judge, time.monotonic())
        try:
            res = await self.adapters[judge].send(agent, prompt, self._ctx(JUDGE_SYSTEM, "none", ephemeral=True),
                                                  lambda t: self.emit(Event("text", judge, t)), lambda t: None)
        except AgentError as e:
            self.emit(Event("error", text=f"Couldn't write the {title.lower()}: {e}"))
            return None
        finally:
            self.current = None
        text = res.text.strip()
        src["cost"] = (src.get("cost") or 0) + (res.cost or 0)
        self.emit(Event("summary_end", judge, title, {"meta": res.meta}))
        (self.run_dir / "summary.md").write_text(f"# {title} (by {NAMES[judge]})\n\n{text}\n")
        store.append_transcript(s, f"\n---\n## ⚖ {title} · by {NAMES[judge]}\n\n{text}\n")
        self.save()
        return text

    # ---- the loop ---------------------------------------------------------
    async def run(self) -> str:
        """Run until agreement, turn limit, pause, error, usage limit or cancellation.
        Returns: agreed | limit | paused | verify_failed | usage_limit | error | interrupted."""
        s = self.state
        self._pause = False
        self._turn_cut = False
        self.running = True
        self.emit(Event("run_start", data={"run_dir": s["run_dir"], "workdir": s["workdir"]}))
        reason, detail = "limit", {}
        try:
            await self._prepare_checkpoints()
            while s["turn"] < s["max_turns"]:
                if self._pause:
                    reason = "paused"
                    break
                speaker, reply = await self._take_turn()
                await self._checkpoint(s["turn"], speaker)
                if has_stop(reply, s["stop"]):
                    if s["stop_pending"] and s["stop_pending"] != speaker:
                        if s.get("verify") and not await self._verify_after_agreement():
                            if s["turn"] >= s["max_turns"]:
                                reason = "verify_failed"
                                break
                            self.save()
                            continue
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
            if reason in ("agreed", "limit") and s.get("auto_verdict") and s.get("preset") == "debate" and s["turn"]:
                await self.summarize()
        except asyncio.CancelledError:
            if self._turn_cut:
                s["interrupted"] = {"agent": s["next"], "turn": s["turn"] + 1}
                if s["pending_user"]:
                    self.emit(Event("notice", text=f"Your message will go to {NAMES[s['next']]}, whose turn "
                                                   f"was interrupted, when you continue."))
            try:
                await self._checkpoint(s["turn"] + 1, s["next"], "interrupted, partial")
            except Exception:
                pass
            self._finish("interrupted")
            raise
        except UsageLimitError as e:
            reason = "usage_limit"
            detail = {"agent": getattr(e, "agent", None), "resets_at": e.resets_at, "message": str(e)}
        except AgentError as e:
            reason = "error"
            self.emit(Event("error", text=str(e)))
        self._finish(reason, detail)
        return reason

    def _finish(self, reason: str, detail: dict | None = None) -> None:
        self.running = False
        self.current = None
        self.save()
        if reason == "agreed":
            store.append_transcript(self.state, f"\n---\n_Both agents agreed the task is done "
                                                f"(turn {self.state['turn']})._\n")
        self.emit(Event("done", data={"reason": reason, "turn": self.state["turn"], "topic": self.state["topic"],
                                      "max_turns": self.state["max_turns"], "run_dir": self.state["run_dir"],
                                      "cost": self.cost, **(detail or {})}))
