"""Slash commands and their autocompletion."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from rich.table import Table
from rich.text import Text

from .. import config, store
from ..engine import Engine, resolve_roles
from ..presets import AGENTS, CUSTOM, EFFORTS, MODES, NAMES, PERMS, ROLE_PRESETS
from .pickers import ask_text, confirm, pick

if TYPE_CHECKING:
    from .app import App

Handler = Callable[["App", list[str]], Awaitable[None]]


@dataclass
class Command:
    name: str
    help: str
    handler: Handler
    usage: str = ""
    aliases: tuple[str, ...] = ()
    args: Callable[["App", list[str]], Iterable[str]] | None = None  # completions for the next arg
    paths: bool = False  # complete the argument as a filesystem path


REGISTRY: dict[str, Command] = {}


def command(name: str, help: str, usage: str = "", aliases: tuple[str, ...] = (), args=None, paths=False):
    def deco(fn: Handler) -> Handler:
        cmd = Command(name, help, fn, usage, aliases, args, paths)
        REGISTRY[name] = cmd
        for al in aliases:
            REGISTRY.setdefault(al, cmd)
        return fn
    return deco


def unique_commands() -> list[Command]:
    seen, out = set(), []
    for c in REGISTRY.values():
        if c.name not in seen:
            seen.add(c.name)
            out.append(c)
    return out


# ---------------------------------------------------------------- completion
class SlashCompleter(Completer):
    def __init__(self, app: "App"):
        self.app = app
        self.paths = PathCompleter(expanduser=True)

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            word = text[1:].lower()
            for c in unique_commands():
                if c.name.startswith(word):
                    yield Completion("/" + c.name, start_position=-len(text),
                                     display=f"/{c.name} {c.usage}".rstrip(), display_meta=c.help)
            return
        name, _, rest = text[1:].partition(" ")
        cmd = REGISTRY.get(name.lower())
        if not cmd:
            return
        if cmd.paths:
            yield from self.paths.get_completions(Document(rest, len(rest)), complete_event)
            return
        if not cmd.args:
            return
        parts = rest.split(" ")
        done, current = parts[:-1], parts[-1]
        for opt in cmd.args(self.app, [p for p in done if p]):
            if opt.startswith(current):
                yield Completion(opt, start_position=-len(current))


# ------------------------------------------------------------------- helpers
def _agent_arg(arg: str | None) -> str | None:
    if not arg:
        return None
    arg = arg.lower()
    return {"claude": "claude", "c": "claude", "agy": "agy", "antigravity": "agy", "a": "agy"}.get(arg)


async def _choose_agent(app: "App", title: str) -> str | None:
    s = app.settings
    opts = [(a, NAMES[a], f"model: {getattr(s, f'{a}_model') or 'default'} · "
                          f"effort: {getattr(s, f'{a}_effort') or 'default'}") for a in AGENTS]
    return await pick(title, opts)


def _apply_agent_setting(app: "App", agent: str, key: str, value: str) -> None:
    setattr(app.settings, f"{agent}_{key}", value)
    if app.engine:
        app.engine.set_agent(agent, **{key: value})
    when = "from the next turn" if app.engine else "for new conversations"
    app.info(f"{NAMES[agent]} {key} → {value or 'default'} ({when})")


def _need_idle(app: "App", what: str) -> bool:
    if app.running():
        app.warn(f"Can't {what} while agents are working. /pause or /stop first.")
        return False
    return True


# ------------------------------------------------------------------ commands
@command("help", "Show all commands", aliases=("?",))
async def cmd_help(app: "App", args: list[str]) -> None:
    t = Table(show_header=False, box=None, padding=(0, 2))
    for c in unique_commands():
        t.add_row(Text(f"/{c.name} {c.usage}".rstrip(), style="bold"), c.help)
    app.console.print(t)
    app.console.print(Text("\nType a topic to start a conversation. While agents work: type to send them a "
                           "message, Esc or Ctrl+C to interrupt.", style="dim"))


def _model_args(app: "App", done: list[str]):
    if not done:
        return list(AGENTS)
    agent = _agent_arg(done[0])
    if len(done) == 1 and agent:
        return ["default"] + [m for m, _ in app.model_cache.get(agent, [])]
    return []


@command("model", "Change the model for Claude or Antigravity", "[agent] [model]", args=_model_args)
async def cmd_model(app: "App", args: list[str]) -> None:
    agent = _agent_arg(args[0]) if args else await _choose_agent(app, "Change the model for which agent?")
    if not agent:
        if args:
            app.warn("Agent must be claude or agy.")
        return
    if len(args) >= 2:
        value = "" if args[1] == "default" else args[1]
        _apply_agent_setting(app, agent, "model", value)
        return
    while True:
        if agent not in app.model_cache:
            app.info(f"Fetching {NAMES[agent]} models…")
        models = await app.models(agent)
        opts = [("", "default", "the CLI's own default")] + [(m, m, label) for m, label in models]
        opts += [("__custom__", "custom…", "type any model ID")]
        if agent == "agy":
            opts += [("__refresh__", "↻ refresh list", "re-run `agy models`")]
        choice = await pick(f"{NAMES[agent]} model", opts, getattr(app.settings, f"{agent}_model"))
        if choice is None:
            return
        if choice == "__refresh__":
            await app.models(agent, refresh=True)
            continue
        if choice == "__custom__":
            choice = await ask_text(f"{NAMES[agent]} model ID: ", getattr(app.settings, f"{agent}_model"))
            if choice is None:
                return
            choice = choice.strip()
        _apply_agent_setting(app, agent, "model", choice)
        return


def _effort_args(app, done):
    if not done:
        return list(AGENTS)
    return ["default", *EFFORTS] if len(done) == 1 else []


@command("effort", "Reasoning effort per agent", "[agent] [level]", args=_effort_args)
async def cmd_effort(app: "App", args: list[str]) -> None:
    agent = _agent_arg(args[0]) if args else await _choose_agent(app, "Change the effort for which agent?")
    if not agent:
        return
    if len(args) >= 2:
        lvl = args[1].lower()
        if lvl not in ("default", *EFFORTS):
            app.warn(f"Effort must be one of: default, {', '.join(EFFORTS)}")
            return
        _apply_agent_setting(app, agent, "effort", "" if lvl == "default" else lvl)
        return
    opts = [("", "default", "the CLI's own default")] + [(e, e, "") for e in EFFORTS]
    choice = await pick(f"{NAMES[agent]} effort", opts, getattr(app.settings, f"{agent}_effort"))
    if choice is not None:
        _apply_agent_setting(app, agent, "effort", choice)


@command("mode", "Type of work: build-review, plan, debate, brainstorm, custom", "[mode]",
         args=lambda app, done: list(MODES) if not done else [])
async def cmd_mode(app: "App", args: list[str]) -> None:
    if args:
        choice = args[0].lower()
        if choice not in MODES:
            app.warn(f"Mode must be one of: {', '.join(MODES)}")
            return
    else:
        opts = [(k, k, v[0]) for k, v in ROLE_PRESETS.items()] + [(CUSTOM, CUSTOM, "write your own roles")]
        choice = await pick("Type of work", opts, app.settings.preset)
        if choice is None:
            return
    app.settings.preset = choice
    if choice == CUSTOM:
        await cmd_roles(app, [])
    else:
        app.settings.role_a = app.settings.role_b = ""
    ra, rb = resolve_roles(app.settings)
    first, second = app.settings.first, "agy" if app.settings.first == "claude" else "claude"
    app.info(f"Mode → {choice}\n  {NAMES[first]}: {ra}\n  {NAMES[second]}: {rb}")
    if app.engine:
        app.info("Applies to new conversations (/new).")


@command("roles", "Write custom roles for each agent")
async def cmd_roles(app: "App", args: list[str]) -> None:
    ra, rb = resolve_roles(app.settings)
    first = app.settings.first
    second = "agy" if first == "claude" else "claude"
    new_a = await ask_text(f"Role for {NAMES[first]} (speaks first): ", ra)
    if new_a is None:
        return
    new_b = await ask_text(f"Role for {NAMES[second]}: ", rb)
    if new_b is None:
        return
    app.settings.preset = CUSTOM
    app.settings.role_a, app.settings.role_b = new_a.strip(), new_b.strip()
    app.info("Custom roles set." + (" Applies to new conversations (/new)." if app.engine else ""))


@command("turns", "Max turns (total for this conversation; default for new ones)", "[n]",
         args=lambda app, done: ["4", "6", "8", "12", "16", "20", "30"] if not done else [])
async def cmd_turns(app: "App", args: list[str]) -> None:
    if args:
        raw = args[0]
    else:
        opts = [(str(n), str(n), "") for n in (4, 6, 8, 12, 16, 20, 30)] + [("__custom__", "custom…", "")]
        raw = await pick("Max turns", opts, str(app.settings.turns))
        if raw == "__custom__":
            raw = await ask_text("Max turns: ", str(app.settings.turns))
        if raw is None:
            return
    try:
        n = int(raw)
        assert n >= 1
    except (ValueError, AssertionError):
        app.warn("Turns must be a positive whole number.")
        return
    app.settings.turns = n
    msg = f"Max turns → {n}"
    if app.engine:
        eff = app.engine.set_max_turns(n)
        msg += f" (this conversation: {app.engine.state['turn']}/{eff})"
    app.info(msg)


@command("first", "Who speaks first in new conversations", "[claude|agy]",
         args=lambda app, done: list(AGENTS) if not done else [])
async def cmd_first(app: "App", args: list[str]) -> None:
    agent = _agent_arg(args[0]) if args else await pick(
        "Who speaks first?", [(a, NAMES[a], "") for a in AGENTS], app.settings.first)
    if not agent:
        return
    app.settings.first = agent
    app.info(f"{NAMES[agent]} speaks first" + (" in new conversations (/new)." if app.engine else "."))


@command("perms", "What agents may do: none, edits, auto", "[level]",
         args=lambda app, done: list(PERMS) if not done else [])
async def cmd_perms(app: "App", args: list[str]) -> None:
    if args:
        lvl = args[0].lower()
        if lvl not in PERMS:
            app.warn(f"Permission level must be one of: {', '.join(PERMS)}")
            return
    else:
        lvl = await pick("Permissions", [(k, k, v) for k, v in PERMS.items()], app.settings.perms)
        if lvl is None:
            return
    if lvl == "auto" and not await confirm(
            "auto lets BOTH agents edit files and run commands in the working directory. Continue?"):
        return
    app.settings.perms = lvl
    if app.engine:
        app.engine.state["perms"] = lvl
        app.engine.save()
    app.info(f"Permissions → {lvl}" + (" (from the next turn)" if app.engine else ""))


@command("workdir", "Shared project folder for new conversations", "[path|default]", paths=True)
async def cmd_workdir(app: "App", args: list[str]) -> None:
    raw = " ".join(args).strip()
    if not raw:
        raw = await ask_text("Working directory (empty = isolated per-run workspace): ",
                             app.settings.workdir, PathCompleter(expanduser=True, only_directories=True))
        if raw is None:
            return
        raw = raw.strip()
    if raw in ("", "default"):
        app.settings.workdir = ""
        app.info("Workdir → isolated per-run workspace")
        return
    p = Path(raw).expanduser().resolve()
    if p.exists() and not p.is_dir():
        app.warn(f"{p} is a file, not a directory.")
        return
    if not p.exists():
        if not await confirm(f"{p} doesn't exist. Create it?", default_yes=True):
            return
        p.mkdir(parents=True)
    app.settings.workdir = str(p)
    if not (p / ".git").exists():
        app.warn("Tip: this folder isn't a git repo — `git init` it so you can review and undo agent changes.")
    app.info(f"Workdir → {p}" + (" (for new conversations: /new)" if app.engine else ""))


@command("spec", "Start a new conversation using a file as the topic", "<file>", paths=True)
async def cmd_spec(app: "App", args: list[str]) -> None:
    if not _need_idle(app, "start a new conversation"):
        return
    raw = " ".join(args).strip() or await ask_text("Spec file: ", "", PathCompleter(expanduser=True))
    if not raw:
        return
    p = Path(raw.strip()).expanduser()
    try:
        topic = p.read_text().strip()
    except OSError as e:
        app.warn(f"Can't read {p}: {e}")
        return
    if not topic:
        app.warn(f"{p} is empty.")
        return
    app.engine = None
    app.start_new(topic)


@command("new", "Start a fresh conversation (next message is the topic)", aliases=("reset",))
async def cmd_new(app: "App", args: list[str]) -> None:
    if not _need_idle(app, "start a new conversation"):
        return
    app.engine = None
    app.info("Ready for a new conversation. Type the topic or task.")


@command("pause", "Pause after the current turn")
async def cmd_pause(app: "App", args: list[str]) -> None:
    if not app.running():
        app.warn("Nothing is running.")
        return
    app.engine.request_pause()
    app.info("Will pause after the current turn. /continue or type a message to resume.")


@command("stop", "Interrupt the current turn now (same as Esc)", aliases=("interrupt",))
async def cmd_stop(app: "App", args: list[str]) -> None:
    if not app.running():
        app.warn("Nothing is running.")
        return
    app.interrupt()


@command("continue", "Continue the current conversation", "[turns]",
         aliases=("c",), args=lambda app, done: ["2", "4", "8"] if not done else [])
async def cmd_continue(app: "App", args: list[str]) -> None:
    if app.running():
        app.warn("Already running.")
        return
    if not app.engine:
        app.warn("No conversation yet. Type a topic, or /resume a past run.")
        return
    try:
        n = int(args[0]) if args else app.settings.turns
    except ValueError:
        app.warn("Usage: /continue [turns]")
        return
    st = app.engine.state
    if st["turn"] >= st["max_turns"] or st["finished"]:
        app.engine.extend(n)
    app.start_run()


def _runs_args(app, done):
    return [r["name"] for r in store.list_runs(30)] if not done else []


@command("resume", "Load a past conversation", "[run]", args=_runs_args)
async def cmd_resume(app: "App", args: list[str]) -> None:
    if not _need_idle(app, "switch conversations"):
        return
    runs = store.list_runs(40)
    if not runs:
        app.warn("No saved runs yet.")
        return
    if args:
        match = [r for r in runs if r["name"] == args[0]] or (
            [{"dir": Path(args[0]).expanduser()}] if Path(args[0]).expanduser().is_dir() else [])
        if not match:
            app.warn(f"No run named {args[0]}.")
            return
        run_dir = match[0]["dir"]
    else:
        opts = [(str(r["dir"]), r["name"],
                 f"{r['turn']}/{r['max_turns']}{' ✔' if r['finished'] else ''} · {_short(r['topic'], 60)}")
                for r in runs]
        choice = await pick("Resume which conversation?", opts)
        if choice is None:
            return
        run_dir = Path(choice)
    try:
        app.engine = Engine.load(run_dir, app.adapters, app.renderer)
    except (OSError, ValueError, KeyError) as e:
        app.warn(f"Couldn't load {run_dir}: {e}")
        return
    st = app.engine.state
    app.info(f"Loaded {Path(st['run_dir']).name} — turn {st['turn']}/{st['max_turns']}"
             f"{' (finished)' if st['finished'] else ''}\n  Topic: {_short(st['topic'], 200)}")
    if st.get("last_msg"):
        who = NAMES.get(st.get("last_speaker") or "", "Last")
        app.console.print(Text(f"  {who}: {_short(st['last_msg'], 300)}", style="dim"))
    app.info("Type a message to steer it, or /continue.")


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


@command("runs", "List past conversations")
async def cmd_runs(app: "App", args: list[str]) -> None:
    runs = store.list_runs(20)
    if not runs:
        app.info("No saved runs yet.")
        return
    t = Table(box=None, padding=(0, 2), header_style="bold")
    t.add_column("run")
    t.add_column("turns", justify="right")
    t.add_column("")
    t.add_column("topic")
    for r in runs:
        t.add_row(r["name"], f"{r['turn']}/{r['max_turns']}", "✔" if r["finished"] else "", _short(r["topic"], 70))
    app.console.print(t)
    app.console.print(Text(f"Stored in {store.runs_dir()} · /resume <run> to load one", style="dim"))


@command("transcript", "Open the conversation transcript in a pager", "[run]", args=_runs_args)
async def cmd_transcript(app: "App", args: list[str]) -> None:
    if args:
        path = store.runs_dir() / args[0] / "transcript.md"
    elif app.engine:
        path = store.transcript_path(app.engine.state)
    else:
        app.warn("No current conversation. Use /transcript <run> (see /runs).")
        return
    if not path.exists():
        app.warn(f"No transcript at {path}")
        return
    if app.running():
        app.info(f"Transcript: {path}\n  (agents are working; view it in another terminal with: less {path})")
        return
    pager = os.environ.get("PAGER") or ("less" if shutil.which("less") else "")
    if not pager:
        app.info(f"Transcript: {path}")
        return
    cmd = pager.split() + (["-R"] if pager.split()[0] == "less" else []) + [str(path)]
    await asyncio.to_thread(subprocess.run, cmd)


@command("status", "Current settings, conversation and cost")
async def cmd_status(app: "App", args: list[str]) -> None:
    s = app.settings
    t = Table(show_header=False, box=None, padding=(0, 2))
    ra, rb = resolve_roles(s)
    first, second = s.first, "agy" if s.first == "claude" else "claude"
    t.add_row("Claude", f"model {s.claude_model or 'default'} · effort {s.claude_effort or 'default'}")
    t.add_row("Antigravity", f"model {s.agy_model or 'default'} · effort {s.agy_effort or 'default'}")
    t.add_row("Mode", s.preset)
    t.add_row(f"  {NAMES[first]}", ra)
    t.add_row(f"  {NAMES[second]}", rb)
    t.add_row("First", NAMES[s.first])
    t.add_row("Turns", str(s.turns))
    t.add_row("Perms", f"{s.perms} — {PERMS[s.perms]}")
    t.add_row("Workdir", s.workdir or "isolated per-run workspace")
    if app.engine:
        st = app.engine.state
        t.add_row("", "")
        t.add_row("Conversation", Path(st["run_dir"]).name + (" (running)" if app.running() else ""))
        t.add_row("  Topic", _short(st["topic"], 120))
        t.add_row("  Progress", f"turn {st['turn']}/{st['max_turns']}" + (" · finished" if st["finished"] else ""))
        t.add_row("  Workdir", st["workdir"])
        for a in AGENTS:
            ag = st["agents"][a]
            t.add_row(f"  {NAMES[a]}", f"session {ag.get('session') or '—'} · tokens {ag.get('tokens', 0):,}")
        if app.engine.cost:
            t.add_row("  Claude cost", f"~${app.engine.cost:.3f} API-equivalent (covered by a subscription)")
    app.console.print(t)


@command("config", "Show, save or reset your defaults", "[show|save|reset]",
         args=lambda app, done: ["show", "save", "reset"] if not done else [])
async def cmd_config(app: "App", args: list[str]) -> None:
    sub = (args[0].lower() if args else "show")
    if sub == "save":
        path = config.save(app.settings)
        app.info(f"Saved current settings as defaults → {path}")
    elif sub == "reset":
        if await confirm("Reset all settings to built-in defaults (and save)?"):
            app.settings = config.Settings()
            config.save(app.settings)
            app.info("Settings reset.")
    elif sub == "show":
        path = config.config_file()
        app.console.print(Text(f"{path}{'' if path.exists() else ' (not saved yet — /config save)'}", style="dim"))
        app.console.print(Text(config.dumps(app.settings)))
    else:
        app.warn("Usage: /config [show|save|reset]")


@command("clear", "Clear the screen")
async def cmd_clear(app: "App", args: list[str]) -> None:
    app.console.clear()


@command("exit", "Quit (interrupts and saves any running conversation)", aliases=("quit", "q"))
async def cmd_exit(app: "App", args: list[str]) -> None:
    app.should_exit = True


async def dispatch(app: "App", line: str) -> None:
    name, _, rest = line[1:].partition(" ")
    cmd = REGISTRY.get(name.lower())
    if not cmd:
        close = [c.name for c in unique_commands() if c.name.startswith(name.lower()[:2])] if name else []
        app.warn(f"Unknown command /{name}." + (f" Did you mean /{close[0]}?" if close else " Try /help."))
        return
    args = rest.split() if not cmd.paths else ([rest.strip()] if rest.strip() else [])
    await cmd.handler(app, args)
