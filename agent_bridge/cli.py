"""Entry point. No arguments → interactive app. A topic or --resume → one-shot run."""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from . import __version__, config, store
from .agents import make_adapters, missing_executables
from .engine import Engine
from .presets import AGENTS, EFFORTS, MODES, PERMS
from .render import Renderer, make_console, tilde

EPILOG = """\
Run with no arguments for the interactive app (type / for commands).

modes:  build-review · plan · debate · brainstorm · custom (with --role-a/--role-b)
perms:  none (talk only) · edits (auto-accept file edits) · auto (edits + commands)

examples:
  agent-bridge
  agent-bridge "Should a 3-person startup use microservices?" -P debate
  agent-bridge "$(cat SPEC.md)" -P build-review --perms auto --workdir ~/projects/app -n 20 --verify "pytest -q"
  agent-bridge --resume ~/.local/share/agent-bridge/runs/20261121-120000 -n 12
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent-bridge", description="Let Claude Code and Antigravity talk to each other.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    ap.add_argument("topic", nargs="?", help="topic or task (omit for the interactive app)")
    ap.add_argument("-P", "--preset", "--mode", dest="preset", choices=MODES, help="type of work")
    ap.add_argument("--role-a", help="custom role for the agent that speaks first")
    ap.add_argument("--role-b", help="custom role for the agent that speaks second")
    ap.add_argument("-f", "--first", choices=AGENTS)
    ap.add_argument("-n", "--turns", type=int, help="max total turns")
    ap.add_argument("--perms", choices=PERMS)
    ap.add_argument("--workdir", help="shared working dir (default: isolated per-run workspace)")
    ap.add_argument("--timeout", type=int, help="hard cap in seconds per agent call (default 3600)")
    ap.add_argument("--idle-timeout", type=int, help="stop an agent after this many seconds without output (default 600)")
    ap.add_argument("--verify", metavar="CMD", help='command that must pass before "done" counts, e.g. "pytest -q"')
    ap.add_argument("--verify-timeout", type=int, help="seconds for the verify command (default 900)")
    ap.add_argument("--no-checkpoints", dest="checkpoints", action="store_false", default=None,
                    help="don't git-commit the workdir after each turn")
    ap.add_argument("--judge", choices=AGENTS, help="who writes the debate verdict (default claude)")
    ap.add_argument("--no-verdict", dest="auto_verdict", action="store_false", default=None,
                    help="skip the automatic verdict when a debate ends")
    ap.add_argument("--retries", type=int)
    ap.add_argument("--max-words", type=int)
    ap.add_argument("--stop-token")
    for a in AGENTS:
        ap.add_argument(f"--{a}-model")
        ap.add_argument(f"--{a}-effort", choices=EFFORTS)
    ap.add_argument("--resume", metavar="RUN_DIR", help="continue a saved run; -n sets the new total turn cap")
    ap.add_argument("-V", "--version", action="version", version=f"agent-bridge {__version__}")
    return ap


def apply_args(s: config.Settings, args: argparse.Namespace) -> None:
    for key in ("preset", "role_a", "role_b", "first", "turns", "perms", "workdir", "timeout", "retries",
                "max_words", "stop_token", "claude_model", "claude_effort", "agy_model", "agy_effort",
                "idle_timeout", "verify", "verify_timeout", "checkpoints", "judge", "auto_verdict"):
        v = getattr(args, key, None)
        if v is not None:
            setattr(s, key, v)
    if (args.role_a or args.role_b) and args.preset is None:
        s.preset = "custom"


async def _headless(engine: Engine, console) -> str:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    console.print(f"[dim]Transcript: {tilde(engine.run_dir / 'transcript.md')}[/]", soft_wrap=True)
    return await engine.run()


def run_headless(s: config.Settings, args: argparse.Namespace) -> int:
    console = make_console()
    adapters = make_adapters()
    if missing := missing_executables(adapters):
        console.print(f"[red]Not found on PATH: {', '.join(missing)}[/]")
        return 2
    renderer = Renderer(console)
    if args.resume:
        try:
            engine = Engine.load(args.resume, adapters, renderer)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]{e}[/]")
            return 2
        st = engine.state
        if args.turns is not None:
            st["max_turns"] = max(args.turns, st["turn"] + 1)
        else:
            st["max_turns"] = max(st["max_turns"], st["turn"] + 2)
        st["finished"], st["stop_pending"] = False, None
        for a in AGENTS:  # explicit model/effort flags override the saved ones
            for k in ("model", "effort"):
                if (v := getattr(args, f"{a}_{k}")) is not None:
                    st["agents"][a][k] = v
        for key in ("perms", "verify", "verify_timeout", "idle_timeout", "timeout", "judge",
                    "checkpoints", "auto_verdict"):
            if (v := getattr(args, key, None)) is not None:
                st[key] = v
    else:
        if s.workdir and s.perms != "none":
            console.print(f"[yellow]⚠ agents may modify files in {s.workdir}[/]")
        engine = Engine.new(s, args.topic, adapters, renderer)

    try:
        reason = asyncio.run(_headless(engine, console))
    except KeyboardInterrupt:
        console.print(f"[yellow]Resume with: agent-bridge --resume {tilde(engine.run_dir)}[/]", soft_wrap=True)
        return 130
    except asyncio.CancelledError:
        console.print(f"[yellow]Resume with: agent-bridge --resume {tilde(engine.run_dir)}[/]", soft_wrap=True)
        return 143
    cost = engine.cost
    console.print(f"[dim]Transcript: {tilde(engine.run_dir / 'transcript.md')}"
                  + (f"  ·  Claude ~${cost:.3f} API-equivalent (covered by a subscription)" if cost else "") + "[/]",
                  soft_wrap=True)
    if reason in ("limit", "paused", "error", "verify_failed", "usage_limit"):
        console.print(f"[dim]Continue with: agent-bridge --resume {tilde(engine.run_dir)} -n {engine.state['turn'] + 4}[/]",
                      soft_wrap=True)
    return {"error": 1, "verify_failed": 1, "usage_limit": 3}.get(reason, 0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    s, warnings = config.load()
    apply_args(s, args)
    warnings += s.validate()
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    moved = store.migrate_legacy()
    if moved:
        print(f"Moved {moved} old run(s) to {store.runs_dir()}", file=sys.stderr)

    if args.topic or args.resume:
        return run_headless(s, args)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("agent-bridge: the interactive app needs a terminal. Pass a topic for one-shot mode "
              "(see --help).", file=sys.stderr)
        return 2
    from .tui.app import run_tui
    return run_tui(s)
