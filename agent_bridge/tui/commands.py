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
from ..agents.models import match_models
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
    search: bool = False  # complete using the whole argument text as a search query


REGISTRY: dict[str, Command] = {}


def command(name: str, help: str, usage: str = "", aliases: tuple[str, ...] = (), args=None, paths=False,
            search=False):
    def deco(fn: Handler) -> Handler:
        cmd = Command(name, help, fn, usage, aliases, args, paths, search)
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
        if cmd.search:  # e.g. /resume pizza debate → runs matching those words
            for value, display, meta in cmd.args(self.app, rest):
                yield Completion(value, start_position=-len(rest), display=display, display_meta=meta)
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


def _model_args(app: "App", rest: str):
    """Completions for '/model <agent> <search words>' as (inserted text, shown name, detail)."""
    first, sep, query = rest.partition(" ")
    if not sep:
        return [(a, a, NAMES[a]) for a in AGENTS if a.startswith(first.lower())]
    agent = _agent_arg(first)
    if not agent:
        return []
    if agent not in app.model_cache:
        app.prefetch_models(agent)  # fills instantly from the on-disk cache when there is one
    models = app.model_cache.get(agent)
    if models is None:
        return [(f"{first} ", "loading models…", "they'll appear in a moment — keep typing or press Tab")]
    q = query.strip()
    out = [(f"{first} default", "default", "the CLI's own default")] if "default".startswith(q.lower()) else []
    current = getattr(app.settings, f"{agent}_model", "")
    for mid, label in (match_models(q, models) if q else models):
        out.append((f"{first} {mid}", label + ("  ✓" if mid == current else ""), mid))
    return out


@command("model", "Change the model for Claude or Antigravity", "[agent] [model]", args=_model_args, search=True)
async def cmd_model(app: "App", args: list[str]) -> None:
    agent = _agent_arg(args[0]) if args else await _choose_agent(app, "Change the model for which agent?")
    if not agent:
        if args:
            app.warn("Agent must be claude or agy.")
        return
    if len(args) >= 2:
        query = " ".join(args[1:]).strip()  # model names can contain spaces
        if query.lower() == "default":
            _apply_agent_setting(app, agent, "model", "")
            return
        if agent not in app.model_cache:
            app.info(f"Fetching {NAMES[agent]} models…")
        models = await app.models(agent)
        hits = match_models(query, models)
        if len(hits) == 1:
            _apply_agent_setting(app, agent, "model", hits[0][0])
            return
        if len(hits) > 1:
            choice = await pick(f"{len(hits)} {NAMES[agent]} models match \"{query}\"",
                                [(m, m, label) for m, label in hits])
            if choice:
                _apply_agent_setting(app, agent, "model", choice)
            return
        if agent == "claude" and " " not in query:
            # claude accepts aliases and full IDs beyond our short list (e.g. claude-opus-5-5)
            _apply_agent_setting(app, agent, "model", query)
            return
        app.warn(f"No {NAMES[agent]} model matches \"{query}\". Run /model {agent} to pick from the list"
                 + (" (or choose \"custom…\" for an unlisted ID)." if agent == "agy" else "."))
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


def _runs_args(app, query: str):
    runs = store.search_runs(query, store.list_runs(60)) if query.strip() else store.list_runs(30)
    return [(r["name"], store.run_when(r["name"]), store.run_summary(r)) for r in runs[:30]]


def _run_options(runs: list[dict]):
    return [(str(r["dir"]), store.run_when(r["name"]), store.run_summary(r, 80)) for r in runs]


@command("resume", "Load a past conversation (type words to search topics)", "[run|search]",
         args=_runs_args, search=True)
async def cmd_resume(app: "App", args: list[str]) -> None:
    if not _need_idle(app, "switch conversations"):
        return
    runs = store.list_runs(60)
    if not runs:
        app.warn("No saved runs yet.")
        return
    query = " ".join(args).strip()
    exact = [r for r in runs if r["name"] == query]
    if exact:
        run_dir = exact[0]["dir"]
    elif query and Path(query).expanduser().is_dir():
        run_dir = Path(query).expanduser()
    else:
        matches = store.search_runs(query, runs) if query else runs
        if not matches:
            app.warn(f"No conversation matches \"{query}\". /runs lists them all.")
            return
        if len(matches) == 1 and query:
            run_dir = matches[0]["dir"]
        else:
            title = f"Resume which conversation? ({len(matches)} match \"{query}\")" if query else \
                "Resume which conversation?"
            choice = await pick(title, _run_options(matches[:30]))
            if choice is None:
                return
            run_dir = Path(choice)
    try:
        app.engine = Engine.load(run_dir, app.adapters, app.renderer)
    except (OSError, ValueError, KeyError) as e:
        app.warn(f"Couldn't load {run_dir}: {e}")
        return
    st = app.engine.state
    name = Path(st["run_dir"]).name
    app.info(f"Loaded: {store.run_when(name)} · {st.get('preset', '')} · turn {st['turn']}/{st['max_turns']}"
             f"{' · finished' if st['finished'] else ''}\n  Topic: {_short(st['topic'], 200)}")
    summary = Path(st["run_dir"]) / "summary.md"
    if summary.exists():
        lines = [l for l in summary.read_text().splitlines()[1:] if l.strip() and not l.startswith("#")]
        if lines:
            app.console.print(Text(f"  {summary.name}: {_short(' '.join(lines[:3]), 300)}", style="dim"))
    if st.get("last_msg"):
        who = NAMES.get(st.get("last_speaker") or "", "Last")
        app.console.print(Text(f"  Last — {who}: {_short(st['last_msg'], 300)}", style="dim"))
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
    t.add_column("when")
    t.add_column("mode")
    t.add_column("turns", justify="right")
    t.add_column("")
    t.add_column("topic")
    for r in runs:
        t.add_row(store.run_when(r["name"]), r.get("preset", ""), f"{r['turn']}/{r['max_turns']}",
                  "✔" if r["finished"] else "", _short(r["topic"], 60))
    app.console.print(t)
    app.console.print(Text(f"Stored in {store.runs_dir()} · /resume <words from the topic> to load one",
                           style="dim"))


@command("transcript", "Open the conversation transcript in a pager", "[run|search]", args=_runs_args,
         search=True)
async def cmd_transcript(app: "App", args: list[str]) -> None:
    if args:
        query = " ".join(args)
        runs = store.list_runs(60)
        hit = [r for r in runs if r["name"] == query] or store.search_runs(query, runs)
        if not hit:
            app.warn(f"No conversation matches \"{query}\".")
            return
        if len(hit) > 1:
            choice = await pick("Which transcript?", _run_options(hit[:30]))
            if choice is None:
                return
            path = Path(choice) / "transcript.md"
        else:
            path = hit[0]["dir"] / "transcript.md"
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
    t.add_row("Verify", s.verify or "(none)")
    t.add_row("Checkpoints", "on" if s.checkpoints else "off")
    t.add_row("Judge", f"{NAMES[s.judge]} · auto verdict {'on' if s.auto_verdict else 'off'}")
    t.add_row("Timeouts", f"{s.idle_timeout}s without output · {s.timeout}s per call")
    lim = app.adapters["claude"].limits
    if lim and lim.get("five_hour") is not None:
        t.add_row("Claude usage", f"5-hour window {lim['five_hour']:.0%} · 7-day {lim.get('seven_day') or 0:.0%}")
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
        if st.get("last_verify"):
            lv = st["last_verify"]
            t.add_row("  Last verify", f"{'passed' if lv['ok'] else 'failed (exit ' + str(lv['code']) + ')'} "
                                       f"at turn {lv['turn']}")
        n_cp = len(st.get("checkpoint_log", []))
        if n_cp:
            t.add_row("  Checkpoints", f"{n_cp} commit(s) · /checkpoints to list · /undo to revert")
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


# ------------------------------------------------- verify / checkpoints / verdict
@command("verify", "Command that must pass before 'done' counts (e.g. pytest -q)", "[cmd|off|run]",
         args=lambda app, done: ["off", "run", "pytest -q", "npm test", "cargo test", "go test ./..."] if not done else [])
async def cmd_verify(app: "App", args: list[str]) -> None:
    raw = " ".join(args).strip()
    current = app.engine.state.get("verify") if app.engine else app.settings.verify
    if not raw:
        app.info(f"Verify command: {current or '(none)'}\n  /verify <cmd> to set · /verify off · /verify run to run it now")
        return
    if raw == "run":
        if not app.engine:
            app.warn("No conversation yet — the command runs in the conversation's workdir.")
            return
        if not current:
            app.warn("No verify command set. Example: /verify pytest -q")
            return
        if not _need_idle(app, "run verification"):
            return
        app.start_task(app.engine.verify_now())
        return
    value = "" if raw in ("off", "none") else raw
    app.settings.verify = value
    if app.engine:
        app.engine.set(verify=value)
    app.info(f"Verify → {value or 'off'}" + (" (applies to this conversation too)" if app.engine else ""))


@command("checkpoints", "Git-commit the workdir after every turn", "[on|off]",
         args=lambda app, done: ["on", "off"] if not done else [])
async def cmd_checkpoints(app: "App", args: list[str]) -> None:
    if not args:
        on = app.engine.state.get("checkpoints") if app.engine else app.settings.checkpoints
        msg = f"Checkpoints: {'on' if on else 'off'}"
        if app.engine:
            log = app.engine.state.get("checkpoint_log", [])
            ok = app.engine.state.get("checkpoint_ok")
            if on and ok is False:
                msg += " (inactive: workdir isn't a clean git repo)"
            for e in log[-8:]:
                who = NAMES.get(e.get("agent") or "", "—")
                msg += (f"\n  turn {e['turn']:>3} · {who:<11} {e['sha']}  {e.get('stat', '')}"
                        f"{'  (reverted)' if e.get('reverted') else ''}")
        app.info(msg)
        return
    val = args[0].lower()
    if val not in ("on", "off"):
        app.warn("Usage: /checkpoints [on|off]")
        return
    on = val == "on"
    app.settings.checkpoints = on
    if app.engine:
        app.engine.set(checkpoints=on, checkpoint_ok=None)  # re-checked at the next run start
    app.info(f"Checkpoints → {val}")


@command("undo", "Revert the file changes of the last turn (or a given turn)", "[turn]")
async def cmd_undo(app: "App", args: list[str]) -> None:
    if not app.engine:
        app.warn("No conversation.")
        return
    if not _need_idle(app, "undo"):
        return
    turn = None
    if args:
        try:
            turn = int(args[0])
        except ValueError:
            app.warn("Usage: /undo [turn]")
            return
    live = [e for e in app.engine.state.get("checkpoint_log", []) if not e.get("reverted")]
    if turn is not None:
        live = [e for e in live if e["turn"] == turn]
    if not live:
        app.warn("Nothing to undo — no checkpoints" + (f" for turn {turn}." if turn else " yet."))
        return
    e = live[-1]
    who = NAMES.get(e.get("agent") or "", "—")
    if not await confirm(f"Revert turn {e['turn']} ({who}, {e['sha']}: {e.get('stat') or 'changes'})? "
                         "This adds a revert commit."):
        return
    ok, msg = await app.engine.undo(e["turn"])
    (app.info if ok else app.warn)(msg + (" The agents will be told on their next turn." if ok else ""))


@command("summary", "Write a verdict (debates) or summary of this conversation", "[claude|agy]",
         aliases=("verdict",), args=lambda app, done: list(AGENTS) if not done else [])
async def cmd_summary(app: "App", args: list[str]) -> None:
    if not app.engine or not app.engine.state["turn"]:
        app.warn("Nothing to summarize yet.")
        return
    if not _need_idle(app, "summarize"):
        return
    judge = _agent_arg(args[0]) if args else app.settings.judge
    if not judge:
        app.warn("Usage: /summary [claude|agy]")
        return
    app.start_task(app.engine.summarize(judge))


@command("judge", "Who writes verdicts, and whether debates get one automatically", "[claude|agy|auto on|auto off]",
         args=lambda app, done: ["claude", "agy", "auto"] if not done else (["on", "off"] if done == ["auto"] else []))
async def cmd_judge(app: "App", args: list[str]) -> None:
    if args and args[0] == "auto":
        on = len(args) > 1 and args[1] == "on"
        app.settings.auto_verdict = on
        if app.engine:
            app.engine.set(auto_verdict=on)
        app.info(f"Automatic debate verdict → {'on' if on else 'off'}")
        return
    agent = _agent_arg(args[0]) if args else await pick(
        "Who should judge?", [(a, NAMES[a], "") for a in AGENTS], app.settings.judge)
    if not agent:
        return
    app.settings.judge = agent
    if app.engine:
        app.engine.set(judge=agent)
    app.info(f"Judge → {NAMES[agent]} (automatic debate verdict: {'on' if app.settings.auto_verdict else 'off'})")


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
