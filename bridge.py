#!/usr/bin/env python3
"""
agent-bridge: let Claude Code (`claude`) and Antigravity (`agy`) talk to each other.

Design choices (to avoid the usual pitfalls):
  * Sessions are pinned by exact ID (claude --session-id/--resume, agy --conversation).
    `-c/--continue` is never used, so the bridge can't hijack another conversation.
  * Each run gets its own directory (runs/<timestamp>/) with an isolated workspace
    used as the CWD for both CLIs, a live Markdown transcript, and a state.json.
  * Each agent keeps its own memory via its session, so only the *other* agent's
    latest message is sent each turn (prompts don't grow).
  * Stop protocol: when one agent emits the stop token, the other gets one turn
    to confirm (also emitting it) or object. Hard cap via --turns.
  * Timeouts + retries per call; Ctrl+C saves state; `--resume RUN_DIR` continues.
  * Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path(__file__).resolve().parent
RUNS = HOME / "runs"
STOP_DEFAULT = "[[DONE]]"

ROLE_PRESETS = {
    # name: (role for agent A, role for agent B)
    "debate": ("Argue FOR the proposition / the first position. Push hard, concede only to strong evidence.",
               "Argue AGAINST / the opposing position. Find weaknesses, counter-examples and hidden costs."),
    "build-review": ("Implementer: propose concrete designs and write the code/solution.",
                     "Reviewer: critique for correctness, security, edge cases and simplicity; demand fixes."),
    "plan": ("Architect: propose a concrete step-by-step plan with trade-offs.",
             "Skeptic: stress-test the plan, find risks and missing steps, propose improvements."),
    "brainstorm": ("Generate bold, unconventional ideas and build on the partner's ideas.",
                   "Generate practical ideas, filter and refine the strongest ones, keep a ranked shortlist."),
}

NAMES = {"claude": "Claude", "agy": "Antigravity"}

# permission level -> (claude flags, agy flags)
PERMS = {
    "none":  (["--tools", ""], ["--mode", "plan"]),
    "edits": (["--permission-mode", "acceptEdits"], ["--mode", "accept-edits"]),
    "auto":  (["--permission-mode", "auto"], ["--mode", "auto"]),
}


class AgentError(RuntimeError):
    pass


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def briefing(me: str, other: str, topic: str, role: str, stop: str, perms: str, max_words: int) -> str:
    tools = ("Do NOT use any tools; just think and write." if perms == "none"
             else "You may read/edit files in the current working directory (shared with your partner) if useful.")
    return f"""You are {NAMES[me]}, in a live conversation with another AI agent, {NAMES[other]}.
An automated bridge relays messages between you; no human is reading turn-by-turn.

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


def run_cli(cmd: list[str], cwd: Path, timeout: int) -> dict:
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # make nested `claude` behave like a fresh top-level run
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AgentError(f"timed out after {timeout}s")
    out = p.stdout.strip()
    try:
        # JSON is the last non-empty line (guards against stray banner output)
        data = json.loads(out.splitlines()[-1]) if out else None
    except (json.JSONDecodeError, IndexError):
        data = None
    if data is None:
        raise AgentError(f"exit {p.returncode}, no JSON output.\nstderr: {p.stderr.strip()[-1500:]}\nstdout: {out[-500:]}")
    return data


def call_claude(state: dict, prompt: str, sys_prompt: str) -> tuple[str, str]:
    a = state["agents"]["claude"]
    cmd = ["claude", "--output-format", "json"]
    cmd += ["--resume", a["session"]] if a["started"] else ["--session-id", a["session"]]
    cmd += PERMS[state["perms"]][0] + ["--append-system-prompt", sys_prompt]
    if state.get("claude_model"):
        cmd += ["--model", state["claude_model"]]
    cmd += ["-p", prompt]
    d = run_cli(cmd, Path(state["workdir"]), state["timeout"])
    if d.get("is_error") or d.get("subtype", "success") != "success" or "result" not in d:
        raise AgentError(f"claude error: {json.dumps(d)[:800]}")
    a["started"] = True
    cost = d.get("total_cost_usd")
    a["cost"] = a.get("cost", 0) + (cost or 0)
    return d["result"].strip(), (f"${cost:.3f}" if cost is not None else "")


def call_agy(state: dict, prompt: str, _sys_prompt: str) -> tuple[str, str]:
    a = state["agents"]["agy"]
    cmd = ["agy", "--output-format", "json"] + PERMS[state["perms"]][1]
    if a.get("session"):
        cmd += ["--conversation", a["session"]]
    if state.get("agy_model"):
        cmd += ["--model", state["agy_model"]]
    cmd += [f"--print={prompt}"]  # attached form: prompt can never be mistaken for a flag
    d = run_cli(cmd, Path(state["workdir"]), state["timeout"])
    if d.get("status") != "SUCCESS" or "response" not in d:
        raise AgentError(f"agy error: {json.dumps(d)[:800]}")
    if not d.get("conversation_id"):
        raise AgentError("agy returned no conversation_id; cannot keep context")
    a["session"] = d["conversation_id"]
    a["started"] = True
    toks = (d.get("usage") or {}).get("total_tokens")
    return d["response"].strip(), (f"{toks} tok" if toks else "")


CALLERS = {"claude": call_claude, "agy": call_agy}


def save(state: dict) -> None:
    p = Path(state["run_dir"]) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)  # atomic


def log(state: dict, text: str) -> None:
    with open(Path(state["run_dir"]) / "transcript.md", "a") as f:
        f.write(text)


def has_stop(msg: str, stop: str) -> bool:
    return any(line.strip() == stop for line in msg.splitlines()[-5:])


def take_turn(state: dict) -> str:
    me = state["next"]
    other = "agy" if me == "claude" else "claude"
    a = state["agents"][me]
    brief = briefing(me, other, state["topic"], a["role"], state["stop"], state["perms"], state["max_words"])
    if not a["started"]:
        if state["last_msg"] is None:
            prompt = brief + f"\nYou speak first. Open the conversation with {NAMES[other]}."
        else:
            prompt = brief + f"\n{NAMES[other]} opened with:\n\n{state['last_msg']}\n\nYour reply:"
    else:
        prompt = f"{NAMES[other]} said:\n\n{state['last_msg']}\n\nYour reply:"

    label = f"Turn {state['turn'] + 1}/{state['max_turns']} · {NAMES[me]}"
    for attempt in range(1, state["retries"] + 2):
        print(c(f"\n▶ {label}", "1;36" if me == "claude" else "1;35") + c("  (thinking…)", "2"), flush=True)
        t0 = time.time()
        try:
            reply, meta = CALLERS[me](state, prompt, brief)
            if not reply:
                raise AgentError("empty reply")
            break
        except AgentError as e:
            print(c(f"  ✗ attempt {attempt} failed: {e}", "31"), flush=True)
            if attempt > state["retries"]:
                raise
            time.sleep(5 * attempt)
    secs = time.time() - t0
    print(reply)
    print(c(f"  [{secs:.0f}s {meta}]", "2"), flush=True)
    log(state, f"\n## {label}\n\n{reply}\n")
    state["last_msg"] = reply
    state["turn"] += 1
    state["next"] = other
    save(state)
    return reply


def new_state(args) -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS / stamp
    workdir = Path(args.workdir).resolve() if args.workdir else run_dir / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.role_a or args.role_b:
        ra, rb = args.role_a or ROLE_PRESETS[args.preset][0], args.role_b or ROLE_PRESETS[args.preset][1]
    else:
        ra, rb = ROLE_PRESETS[args.preset]
    first = args.first
    second = "agy" if first == "claude" else "claude"
    state = {
        "version": 1, "run_dir": str(run_dir), "workdir": str(workdir),
        "topic": args.topic, "preset": args.preset, "perms": args.perms,
        "max_turns": args.turns, "turn": 0, "next": first, "last_msg": None,
        "stop": args.stop_token, "stop_pending": None, "finished": False,
        "timeout": args.timeout, "retries": args.retries, "max_words": args.max_words,
        "claude_model": args.claude_model, "agy_model": args.agy_model,
        "agents": {
            first: {"role": ra, "started": False, "session": None},
            second: {"role": rb, "started": False, "session": None},
        },
    }
    state["agents"]["claude"]["session"] = str(uuid.uuid4())  # claude accepts a preset ID
    log(state, f"# Agent Bridge — {stamp}\n\n**Topic:** {args.topic}\n\n"
               f"- {NAMES[first]}: {ra}\n- {NAMES[second]}: {rb}\n- permissions: {args.perms}\n")
    save(state)
    return state


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Let Claude Code and Antigravity talk to each other.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="presets: " + ", ".join(ROLE_PRESETS) + "\n"
               "perms:   none (talk only) | edits (auto-accept file edits in workdir) | auto (agents' auto mode)\n\n"
               "examples:\n"
               "  agent-bridge \"Should a 3-person startup use microservices?\" -P debate\n"
               "  agent-bridge \"Build a CLI todo app in Python\" -P build-review --perms edits -n 10\n"
               "  agent-bridge --resume ~/agent-bridge/runs/20261121-120000 -n 6")
    ap.add_argument("topic", nargs="?", help="topic or task for the conversation")
    ap.add_argument("-P", "--preset", choices=ROLE_PRESETS, default="build-review")
    ap.add_argument("--role-a", help="custom role for the agent that speaks first")
    ap.add_argument("--role-b", help="custom role for the agent that speaks second")
    ap.add_argument("-f", "--first", choices=["claude", "agy"], default="claude")
    ap.add_argument("-n", "--turns", type=int, default=8, help="max total turns (default 8)")
    ap.add_argument("--perms", choices=PERMS, default="none")
    ap.add_argument("--workdir", help="shared working dir (default: isolated per-run workspace)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per agent call (default 600)")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--max-words", type=int, default=350)
    ap.add_argument("--stop-token", default=STOP_DEFAULT)
    ap.add_argument("--claude-model")
    ap.add_argument("--agy-model")
    ap.add_argument("--resume", metavar="RUN_DIR", help="continue a previous run; -n sets the NEW total turn cap")
    args = ap.parse_args()

    for exe in ("claude", "agy"):
        if not shutil.which(exe):
            ap.error(f"`{exe}` not found on PATH")

    if args.resume:
        sp = Path(args.resume).expanduser() / "state.json"
        if not sp.exists():
            ap.error(f"no state.json in {args.resume}")
        state = json.loads(sp.read_text())
        state["run_dir"] = str(sp.parent)
        if "-n" in sys.argv or "--turns" in sys.argv:
            state["max_turns"] = max(args.turns, state["turn"] + 1)
        else:
            state["max_turns"] = max(state["max_turns"], state["turn"] + 2)
        state["finished"] = False
        state["stop_pending"] = None
    else:
        if not args.topic:
            ap.error("topic is required (or use --resume)")
        if args.workdir and args.perms != "none":
            print(c(f"⚠ agents may modify files in {Path(args.workdir).resolve()}", "33"))
        state = new_state(args)

    run_dir = state["run_dir"]
    print(c(f"Run dir: {run_dir}", "2"))
    print(c(f"Transcript: {run_dir}/transcript.md", "2"))

    def on_sigterm(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    try:
        while state["turn"] < state["max_turns"]:
            speaker = state["next"]
            reply = take_turn(state)
            if has_stop(reply, state["stop"]):
                if state["stop_pending"] and state["stop_pending"] != speaker:
                    state["finished"] = True
                    print(c("\n✔ Both agents agreed the task is done.", "1;32"))
                    break
                state["stop_pending"] = speaker
                if state["turn"] >= state["max_turns"]:
                    state["max_turns"] += 1  # always allow the confirmation turn
            else:
                state["stop_pending"] = None
            save(state)
        else:
            print(c(f"\n■ Reached turn limit ({state['max_turns']}).", "33"))
    except KeyboardInterrupt:
        save(state)
        print(c(f"\n■ Interrupted. Resume with: agent-bridge --resume {run_dir}", "33"))
        return 130
    except AgentError as e:
        save(state)
        print(c(f"\n✗ Stopped: {e}\nResume with: agent-bridge --resume {run_dir}", "31"))
        return 1

    save(state)
    log(state, f"\n---\n_Ended after {state['turn']} turns "
               f"({'agreed done' if state['finished'] else 'turn limit'})._\n")
    cost = state["agents"]["claude"].get("cost")
    print(c(f"Transcript: {run_dir}/transcript.md" + (f"  ·  Claude cost: ${cost:.3f}" if cost else ""), "2"))
    if not state["finished"]:
        print(c(f"Continue with: agent-bridge --resume {run_dir} -n {state['turn'] + 4}", "2"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
