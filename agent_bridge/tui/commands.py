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

from .. import budget, config, store, teams
from ..config import Settings
from ..render import tilde
from ..engine import Engine, resolve_roles, role_for
from ..agents.models import match_models
from ..presets import AGENTS, EFFORTS, MODES, NAMES, PERMS
from ..roles import (BUILTIN_ROLES, CUSTOM, LEADS, PARTNER, ROLE_KEY_RE, ROLES, TEAMS, USER_ROLES,
                     load_user_roles, role_name, roles_dir, save_user_role)
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


def _other(agent: str) -> str:
    return "agy" if agent == "claude" else "claude"


def assignment(app: "App") -> str:
    """'Claude: Coder · Antigravity: Reviewer' for the settings (new conversations)."""
    return " · ".join(f"{NAMES[a]}: {role_name(role_for(app.settings, a)[0])}" for a in AGENTS)


def _live_roles(app: "App") -> str:
    st = app.engine.state
    return " · ".join(f"{NAMES[a]}: {role_name(st['agents'][a].get('role_key') or CUSTOM)}" for a in AGENTS)


def _mode_args(app, done):
    saved = teams.list_teams()
    if not done:
        return [*MODES, *saved, "save", "delete"]
    if done == ["delete"]:
        return saved
    return []


@command("mode", "Team preset: who does what (build-review, orchestrated, design-build, debate…)", "[mode]",
         args=_mode_args)
async def cmd_mode(app: "App", args: list[str]) -> None:
    s = app.settings
    saved = teams.list_teams()
    if args and args[0].lower() == "save":
        await _save_team(app, " ".join(args[1:]))  # whole rest, so "My Team" is rejected, not cut to "my"
        return
    if args and args[0].lower() == "delete":
        name = args[1].lower() if len(args) > 1 else await pick(
            "Delete which saved team?", [(n, n, teams.describe_saved(n)) for n in saved])
        if not name:
            return
        if name not in saved:
            app.warn(f"No saved team named {name}.")
            return
        if await confirm(f"Delete the saved team '{name}'?"):
            teams.delete_team(name)
            app.info(f"Deleted team {name}.")
        return
    if args:
        choice = args[0].lower()
        if choice not in MODES and choice not in saved:
            app.warn(f"Unknown mode or team. Built-in: {', '.join(MODES)}"
                     + (f" · yours: {', '.join(saved)}" if saved else "") + " · /mode save <name> to save one")
            return
    else:
        first, second = s.first, _other(s.first)
        opts = [(k, k, f"{t.summary} — {NAMES[first]}: {ROLES[t.first].name}, {NAMES[second]}: "
                       f"{ROLES[t.second].name}") for k, t in TEAMS.items()]
        opts.append((CUSTOM, CUSTOM, "pick each agent's role yourself (/role)"))
        opts += [(f"__team__{n}", f"★ {n}", teams.describe_saved(n)) for n in saved]
        opts.append(("__save__", "＋ save the current setup as a team…", teams.describe(s)))
        choice = await pick("Team preset (roles follow who speaks first; /first to flip)", opts, s.preset)
        if choice is None:
            return
        if choice == "__save__":
            await _save_team(app, "")
            return
        if choice.startswith("__team__"):
            choice = choice[len("__team__"):]
    if choice in saved and choice not in MODES:
        try:
            warn = teams.apply_team(s, teams.load_team(choice))
        except (OSError, ValueError) as e:
            app.warn(f"Couldn't load team {choice}: {e}")
            return
        for w in warn:
            app.warn(w)
        app.info(f"Team {choice} loaded → {teams.describe(s)}")
        if app.engine:
            app.info("Applies to new conversations (/new). To change roles in this conversation, use /role.")
        return
    s.preset = choice
    s.role_a = s.role_b = ""
    if choice == CUSTOM:
        for a in AGENTS:
            if not await _choose_role(app, a):
                break
    else:
        s.claude_role = s.agy_role = ""  # follow the mode
    app.info(f"Mode → {choice}  ({assignment(app)})")
    if app.engine:
        app.info("Applies to new conversations (/new). To change roles in this conversation, use /role.")


async def _save_team(app: "App", name: str) -> None:
    name = (name or await ask_text("Team name (e.g. my-java-team): ") or "").strip()
    if not name:
        return
    if not teams.valid_name(name):
        app.warn("Use lowercase letters, digits, '-' or '_', and not a built-in mode name.")
        return
    if name in teams.list_teams() and not await confirm(f"Team '{name}' exists. Overwrite it?"):
        return
    path = teams.save_team(name, app.settings)
    app.info(f"Saved team {name} → {tilde(path)}\n  {teams.describe(app.settings)}\n"
             f"  Load it with /mode {name}  (or abr --team {name})")


async def _choose_role(app: "App", agent: str) -> bool:
    s = app.settings
    current = getattr(s, f"{agent}_role")
    follow_key = role_for(Settings(**{**s.__dict__, f"{agent}_role": ""}), agent)[0]
    opts = [("__mode__", "follow the mode", f"{s.preset} gives {NAMES[agent]}: {role_name(follow_key)}")]
    opts += [(k, r.name + ("  ★" if k in USER_ROLES else ""), r.summary) for k, r in ROLES.items()]
    opts.append((CUSTOM, "custom…", "one-off text for this conversation"))
    opts.append(("__create__", "＋ create a reusable role…", f"saved in {tilde(roles_dir())}"))
    choice = await pick(f"Role for {NAMES[agent]}", opts, current or "__mode__")
    if choice is None:
        return False
    if choice == "__create__":
        await _create_role(app)
        return True
    await _set_role(app, agent, "" if choice == "__mode__" else choice)
    return True


async def _set_role(app: "App", agent: str, key: str) -> None:
    s = app.settings
    text = ""
    if key == CUSTOM:
        text = await ask_text(f"{NAMES[agent]}'s role: ", getattr(s, f"{agent}_role_text") or role_for(s, agent)[1])
        if text is None or not text.strip():
            return
        text = text.strip()
        setattr(s, f"{agent}_role_text", text)
    setattr(s, f"{agent}_role", key)
    rkey, rtext = role_for(s, agent)
    msg = f"{NAMES[agent]} → {role_name(rkey)}"
    other = _other(agent)
    if key and key in PARTNER and not getattr(s, f"{other}_role"):
        # the partner was following the mode; give it a role that fits this one
        setattr(s, f"{other}_role", PARTNER[key])
        msg += f" · {NAMES[other]} → {role_name(PARTNER[key])} to pair with it (/role {other} … to change)"
        if app.engine and app.engine.state["agents"][other].get("role_key") != PARTNER[key]:
            app.engine.set_role(other, PARTNER[key], ROLES[PARTNER[key]].text)
    if rkey in LEADS and s.first != agent and getattr(s, f"{_other(agent)}_role") not in LEADS:
        s.first = agent
        msg += f" ({NAMES[agent]} will open new conversations)"
    if app.engine:
        cur = app.engine.state["agents"][agent]
        if cur.get("role") != rtext:
            app.engine.set_role(agent, rkey, rtext)
            msg += " — applies from the next turn; both agents will be told"
    app.info(msg)


def _role_args(app, done):
    if not done:
        return [*AGENTS, "swap", "create", "reload"]
    if len(done) == 1 and _agent_arg(done[0]):
        return ["mode", *ROLES, CUSTOM]
    return []


@command("role", "Give an agent a role: orchestrator, architect, coder, reviewer, tester…",
         "[agent] [role|mode|custom] · swap · create · reload", args=_role_args)
async def cmd_role(app: "App", args: list[str]) -> None:
    s = app.settings
    if args and args[0].lower() in ("create", "new"):
        await _create_role(app, " ".join(args[1:]))
        return
    if args and args[0].lower() == "reload":
        warn = load_user_roles()
        for w in warn:
            app.warn(w)
        app.info(f"Loaded {len(USER_ROLES)} role(s) of your own from {tilde(roles_dir())}")
        return
    if args and args[0].lower() == "swap":
        keys = {a: role_for(s, a) for a in AGENTS}
        for a in AGENTS:
            k, t = keys[_other(a)]
            setattr(s, f"{a}_role", k)
            if k == CUSTOM:
                setattr(s, f"{a}_role_text", t)
        if app.engine:
            live = {a: (app.engine.state["agents"][a].get("role_key") or CUSTOM, app.engine.state["agents"][a]["role"])
                    for a in AGENTS}
            for a in AGENTS:
                app.engine.set_role(a, *live[_other(a)])
        app.info(f"Swapped roles → {assignment(app)}" + (" (from the next turn)" if app.engine else ""))
        return
    agent = _agent_arg(args[0]) if args else await _choose_agent_role(app)
    if not agent:
        if args:
            app.warn("Usage: /role <claude|agy> <role>   ·   /role swap   ·   /roles to see them all")
        return
    if len(args) < 2:
        await _choose_role(app, agent)
        return
    key = args[1].lower()
    if key == "mode":
        key = ""
    elif key not in ROLES and key != CUSTOM:
        close = [k for k in ROLES if k.startswith(key[:3])]
        app.warn(f"Unknown role \"{key}\"." + (f" Did you mean {close[0]}?" if close else "") + " /roles lists them.")
        return
    await _set_role(app, agent, key)


async def _create_role(app: "App", key: str = "") -> None:
    import re
    say = app.info
    say(f"New role — saved as a Markdown file in {tilde(roles_dir())} (edit it any time). Esc cancels.")
    key = (key or await ask_text("Role id (e.g. game-dev): ") or "").strip()
    if not key:
        return
    if not re.match(ROLE_KEY_RE, key) or key in (CUSTOM, "create", "new", "reload", "swap", "mode"):
        app.warn("Use lowercase letters, digits, '-' or '_' (e.g. game-dev).")
        return
    path = roles_dir() / f"{key}.md"
    if path.exists() and not await confirm(f"{tilde(path)} exists. Overwrite it?"):
        return
    if key in BUILTIN_ROLES and not path.exists() and not await confirm(
            f"'{key}' is a built-in role. Replace it with yours?"):
        return
    name = await ask_text("Display name: ", key.replace("-", " ").replace("_", " ").title())
    if name is None:
        return
    summary = await ask_text("One-line summary: ")
    if summary is None:
        return
    text = await ask_text("Instructions (what this role does and how; one paragraph is fine): ")
    if not text or not text.strip():
        app.warn("A role needs instructions.")
        return
    path = save_user_role(key, name or key, summary or "", text)
    for w in load_user_roles():
        app.warn(w)
    say(f"Saved {tilde(path)} → /role claude {key}  or  /role agy {key}")
    who = await pick("Give it to an agent now?", [(a, NAMES[a], "") for a in AGENTS] + [("", "not now", "")])
    if who:
        await _set_role(app, who, key)


async def _choose_agent_role(app: "App") -> str | None:
    opts = [(a, NAMES[a], f"now: {role_name(role_for(app.settings, a)[0])}") for a in AGENTS]
    opts.append(("__swap__", "swap roles", assignment(app)))
    choice = await pick("Change whose role?", opts)
    if choice == "__swap__":
        await cmd_role(app, ["swap"])
        return None
    return choice


@command("roles", "Show who does what, and every available role")
async def cmd_roles(app: "App", args: list[str]) -> None:
    app.console.print(Text("New conversations: " + assignment(app) + f"  (mode {app.settings.preset}, "
                           f"{NAMES[app.settings.first]} opens)", style="bold"))
    if app.engine:
        app.console.print(Text("This conversation: " + _live_roles(app), style="bold"))
    t = Table(show_header=False, box=None, padding=(0, 2))
    for k, r in ROLES.items():
        mine = k in USER_ROLES
        t.add_row(Text(k, style="bold green" if mine else "bold"), r.name + ("  (yours)" if mine else ""),
                  Text(r.summary, style="dim"))
    t.add_row(Text(CUSTOM, style="bold"), "Custom", Text("one-off text for one conversation", style="dim"))
    app.console.print(t)
    app.console.print(Text("Assign with /role <claude|agy> <role> · /role swap · /role create to add your own "
                           f"(files in {tilde(roles_dir())}) · /role reload after editing them", style="dim"))


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


@command("init", "Set up a project: answer 6 questions → SPEC.md, verify command, git, start")
async def cmd_init(app: "App", args: list[str]) -> None:
    if not _need_idle(app, "set up a new project"):
        return
    from .wizard import run_wizard
    await run_wizard(app)


@command("publish", "Put the project on GitHub: create a repo (private by default) and push", "[folder]",
         paths=True)
async def cmd_publish(app: "App", args: list[str]) -> None:
    if not _need_idle(app, "publish"):
        return
    from .publish import run_publish
    await run_publish(app, " ".join(args).strip())


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
        app.engine = Engine.load(run_dir, app.adapters, app.emit)
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
    for a in AGENTS:
        rk = role_for(s, a)[0]
        t.add_row(NAMES[a], f"{role_name(rk)} · model {getattr(s, f'{a}_model') or 'default'} · "
                            f"effort {getattr(s, f'{a}_effort') or 'default'}")
    t.add_row("Mode", s.preset)
    t.add_row("First", NAMES[s.first])
    t.add_row("Turns", str(s.turns))
    t.add_row("Perms", f"{s.perms} — {PERMS[s.perms]}")
    t.add_row("Workdir", s.workdir or "isolated per-run workspace")
    t.add_row("Verify", s.verify or "(none)")
    t.add_row("Checkpoints", "on" if s.checkpoints else "off")
    t.add_row("Judge", f"{NAMES[s.judge]} · auto verdict {'on' if s.auto_verdict else 'off'}")
    t.add_row("Progress", f"{'on' if s.progress else 'off'} · every {s.progress_every} turns · keeper "
                          f"{s.progress_keeper}" + ("" if s.perms != "none" else " (needs /perms edits or auto)"))
    t.add_row("Budget", budget.describe(_settings_budget(s)))
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
            t.add_row(f"  {NAMES[a]}", f"{role_name(ag.get('role_key') or CUSTOM)} · session "
                                       f"{ag.get('session') or '—'} · tokens {ag.get('tokens', 0):,}")
        if st.get("last_verify"):
            lv = st["last_verify"]
            t.add_row("  Last verify", f"{'passed' if lv['ok'] else 'failed (exit ' + str(lv['code']) + ')'} "
                                       f"at turn {lv['turn']}")
        used = app.engine.budget_status()
        if used:
            t.add_row("  Budget", ", ".join(used))
        if st.get("progress"):
            t.add_row("  Progress", f"PROGRESS.md kept by {NAMES.get(st.get('progress_keeper') or '', '—')} · "
                                    f"last changed at turn {st.get('progress_hash_turn', 0)}")
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


def _diff_args(app, done):
    if done or not app.engine:
        return []
    turns = sorted({str(e["turn"]) for e in app.engine.state.get("checkpoint_log", [])}, key=int)
    return ["all", *turns]


@command("diff", "Show the code changes from a turn (default: the latest)", "[turn|all]", args=_diff_args)
async def cmd_diff(app: "App", args: list[str]) -> None:
    from .. import workspace
    if not app.engine:
        app.warn("No conversation.")
        return
    log = app.engine.state.get("checkpoint_log", [])
    if not log:
        app.warn("No changes recorded yet. Checkpoints need file permissions (/perms edits or auto) and a "
                 "git repo workdir; see /checkpoints.")
        return
    wd = app.engine.workdir
    arg = args[0].lower() if args else ""
    if arg == "all":
        spec = [f"{log[0]['sha']}~1", log[-1]["sha"]]
        title = f"All changes in this conversation (turns {log[0]['turn']}–{log[-1]['turn']})"
        cmd = ["diff", "--stat", "--patch", *spec]
    else:
        if arg:
            try:
                t = int(arg)
            except ValueError:
                app.warn("Usage: /diff [turn|all]")
                return
            entries = [e for e in log if e["turn"] == t]
            if not entries:
                app.warn(f"Turn {t} didn't change any files. Turns with changes: "
                         + ", ".join(sorted({str(e['turn']) for e in log}, key=int)))
                return
            e = entries[-1]
        else:
            e = log[-1]
        who = NAMES.get(e.get("agent") or "", "")
        title = f"Turn {e['turn']}{' · ' + who if who else ''} · {e['sha']}{' (reverted)' if e.get('reverted') else ''}"
        cmd = ["show", "--stat", "--patch", "--format=", e["sha"]]
    code, out = await workspace.git(wd, "-c", "color.ui=always", *cmd)
    if code != 0:
        app.warn(f"git failed: {out.strip()[:300]}")
        return
    if not out.strip():
        app.info("No changes.")
        return
    pager = os.environ.get("PAGER") or ("less" if shutil.which("less") else "")
    long = out.count("\n") > 40
    if app.running() or not pager or not long:
        app.console.print(Text(title, style="bold"))
        body = out if not long else out.split("\n\n", 1)[0] + "\n… (long diff — run /diff again when idle to page it)"
        app.console.print(Text.from_ansi(body.rstrip()))
        return
    argv = pager.split() + (["-R"] if pager.split()[0] == "less" else [])
    await asyncio.to_thread(subprocess.run, argv, input=(f"{title}\n\n" + out).encode())


def _progress_args(app, done):
    if not done:
        return ["on", "off", "every", "keeper"]
    if done == ["every"]:
        return ["2", "3", "4", "6", "8"]
    if done == ["keeper"]:
        return ["auto", *AGENTS]
    return []


@command("progress", "Shared PROGRESS.md memory: show it, or configure it",
         "[on|off|every N|keeper claude|agy|auto]", args=_progress_args)
async def cmd_progress(app: "App", args: list[str]) -> None:
    from rich.markdown import Markdown
    from ..engine import PROGRESS_FILE
    from ..roles import progress_keeper
    s, eng = app.settings, app.engine
    sub = args[0].lower() if args else ""
    if sub in ("on", "off"):
        on = sub == "on"
        s.progress = on
        if eng:
            if on and eng.state["perms"] == "none":
                app.warn("This conversation is talk-only (/perms none), so the agents can't keep a file. "
                         "Set /perms edits or auto first.")
                return
            eng.set(progress=on)
            if on and not eng.state.get("progress_keeper"):
                keys = {a: eng.state["agents"][a].get("role_key", "") for a in AGENTS}
                eng.set(progress_keeper=progress_keeper(s.progress_keeper, keys, eng.state["next"]))
        app.info(f"Progress memory → {sub}" + (" (created at the next run start)" if on and eng else ""))
        return
    if sub == "every":
        try:
            n = int(args[1])
            assert n >= 1
        except (IndexError, ValueError, AssertionError):
            app.warn("Usage: /progress every <turns>")
            return
        s.progress_every = n
        if eng:
            eng.set(progress_every=n)
        app.info(f"PROGRESS.md is shown to the speaking agent every {n} turn(s).")
        return
    if sub == "keeper":
        who = args[1].lower() if len(args) > 1 else ""
        if who not in ("auto", *AGENTS):
            app.warn("Usage: /progress keeper <claude|agy|auto>")
            return
        s.progress_keeper = who
        if eng:
            keys = {a: eng.state["agents"][a].get("role_key", "") for a in AGENTS}
            new = progress_keeper(who, keys, eng.state["next"])
            if new != eng.state.get("progress_keeper"):
                eng.set(progress_keeper=new)
                eng.add_note(f"The user made you the keeper of {PROGRESS_FILE}: keep it up to date from now on.",
                             to=new)
                eng.add_note(f"{NAMES[new]} now keeps {PROGRESS_FILE}.", to=_other(new))
        app.info(f"PROGRESS.md keeper → {who}")
        return
    if sub:
        app.warn("Usage: /progress [on|off|every N|keeper claude|agy|auto]")
        return
    # show it
    if not eng:
        state = "on" if s.progress else "off"
        app.info(f"Progress memory: {state} · shown every {s.progress_every} turns · keeper {s.progress_keeper}\n"
                 "  It's created when a conversation with file permissions (/perms edits or auto) starts.")
        return
    st = eng.state
    if not st.get("progress"):
        app.info("Progress memory is off for this conversation." + (
            " It needs file permissions (/perms edits or auto)." if st["perms"] == "none" else " /progress on"))
        return
    text = eng._progress_text()
    keeper = NAMES.get(st.get("progress_keeper") or "", "—")
    meta = (f"{PROGRESS_FILE} · kept by {keeper} · shown every {st.get('progress_every', 4)} turns · "
            f"last changed at turn {st.get('progress_hash_turn', 0)}")
    if text is None:
        app.warn(f"{eng.progress_path} doesn't exist yet (it's created when the run starts).")
        return
    app.console.print(Text(meta, style="dim"))
    app.console.print(Markdown(text))


def _budget_args(app, done):
    if not done:
        return ["30m", "1h", "2h", "50", "usage", "cost", "time", "turns", "off"]
    if done[-1] in ("usage",):
        return ["70%", "80%", "90%", "off"]
    if done[-1] in ("cost",):
        return ["$2", "$5", "$10", "off"]
    if done[-1] in ("time",):
        return ["30m", "1h", "2h", "off"]
    if done[-1].isdigit():
        return ["turns"]
    return []


@command("budget", "Limits for a conversation: time, turns, Claude usage, cost (pauses cleanly)",
         "[30m|2h|50 turns|usage 80%|cost $5|off]", args=_budget_args)
async def cmd_budget(app: "App", args: list[str]) -> None:
    s, eng = app.settings, app.engine
    if not args:
        app.info(f"Budget for new conversations: {budget.describe(_settings_budget(s))}")
        if eng:
            used = eng.budget_status()
            app.info("This conversation: " + (budget.describe(eng.state["budget"])
                     + ("  ·  used: " + ", ".join(used) if used else "")))
        app.console.print(Text("Examples: /budget 30m · /budget 2h 50 turns · /budget usage 80% · "
                               "/budget cost $5 · /budget turns off · /budget off", style="dim"))
        return
    try:
        changes = budget.parse(" ".join(args))
    except ValueError as e:
        app.warn(str(e))
        return
    for k, v in changes.items():
        setattr(s, f"budget_{k}", float(v) if k == "cost" else int(v))
    msg = f"Budget → {budget.describe(_settings_budget(s))}"
    if eng:
        eng.state["budget"].update({k: (float(v) if k == "cost" else int(v)) for k, v in changes.items()})
        eng.save()
        msg += " (this conversation too; checked between turns)"
        if not app.running() and (hit := eng.budget_hit()):
            msg += f"\n  Already reached: {hit[1]}."
    app.info(msg)


def _settings_budget(s) -> dict:
    return {"minutes": s.budget_minutes, "turns": s.budget_turns, "usage": s.budget_usage, "cost": s.budget_cost}


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


@command("notify", "Desktop notification when a run ends on its own", "[on|off|test]",
         args=lambda app, done: ["on", "off", "test"] if not done else [])
async def cmd_notify(app: "App", args: list[str]) -> None:
    from .. import notify
    sub = args[0].lower() if args else ""
    if sub in ("on", "off"):
        app.settings.notify = sub == "on"
        app.info(f"Notifications → {sub}" + (f" (for runs longer than {app.settings.notify_after}s)" if sub == "on" else ""))
    elif sub == "test":
        if notify.send("agent-bridge", "Notifications work 🎉"):
            app.info("Sent a test notification.")
        else:
            app.warn("Couldn't send: notify-send isn't installed or there's no desktop session "
                     "(Arch: sudo pacman -S libnotify).")
    elif not sub:
        state = "on" if app.settings.notify else "off"
        app.info(f"Notifications: {state} · runs longer than {app.settings.notify_after}s · "
                 f"{'notify-send found' if notify.available() else 'notify-send unavailable'}\n"
                 "  /notify on · /notify off · /notify test")
    else:
        app.warn("Usage: /notify [on|off|test]")


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
