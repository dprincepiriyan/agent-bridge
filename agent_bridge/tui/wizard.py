"""/init: a short Q&A that writes SPEC.md, prepares the project folder and starts the build."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.completion import PathCompleter
from rich.text import Text

from .. import workspace
from ..engine import role_for
from ..presets import NAMES
from ..render import tilde
from ..roles import role_name
from .pickers import ask_text, confirm, pick

if TYPE_CHECKING:
    from .app import App

# (team:first speaker, label, description)
TEAM_CHOICES = [
    ("build-review:claude", "Claude codes · Antigravity reviews", "classic pair"),
    ("build-review:agy", "Antigravity codes · Claude reviews", ""),
    ("orchestrated:agy", "Antigravity orchestrates · Claude codes", "one plans and assigns tasks"),
    ("orchestrated:claude", "Claude orchestrates · Antigravity codes", ""),
    ("design-build:claude", "Claude architects · Antigravity codes", "design first, then build"),
    ("design-build:agy", "Antigravity architects · Claude codes", ""),
    ("build-test:claude", "Claude codes · Antigravity tests", "QA tries to break it"),
    ("build-test:agy", "Antigravity codes · Claude tests", ""),
]

DEFAULT_DONE = "All tests pass and the README explains how to build, run and test it."

# keyword in the stack answer → suggested verify commands (first = default)
TEST_COMMANDS = [
    (("python", "django", "flask", "fastapi", "pytest"), ["python -m pytest -q"]),
    (("typescript", "javascript", "node", "react", "next", "vue", "svelte", "npm"), ["npm test"]),
    (("rust", "cargo"), ["cargo test"]),
    (("go", "golang"), ["go test ./..."]),
    (("java", "kotlin", "spring", "maven", "gradle"), ["mvn -q test", "gradle test --quiet"]),
    (("c#", ".net", "dotnet"), ["dotnet test"]),
    (("c++", "cpp", "cmake", " c "), ["make test", "ctest --output-on-failure"]),
    (("ruby", "rails"), ["bundle exec rspec"]),
]
GENERIC = ["python -m pytest -q", "npm test", "cargo test", "go test ./...", "mvn -q test"]


def suggest_tests(stack: str) -> list[str]:
    s = f" {stack.lower()} "
    out: list[str] = []
    for keys, cmds in TEST_COMMANDS:
        if any(re.search(rf"(?<![a-z]){re.escape(k.strip())}(?![a-z])", s) for k in keys):
            out += [c for c in cmds if c not in out]
    return out or list(GENERIC)


def slugify(text: str, words: int = 4) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "for", "with", "and", "of", "to", "in", "build", "make", "create", "write", "app"}
    keep = [p for p in parts if p not in stop][:words] or parts[:words] or ["project"]
    return "-".join(keep)


def render_spec(goal: str, stack: str, features: list[str], done: str, verify: str) -> str:
    title = goal.strip().rstrip(".")
    title = title[0].upper() + title[1:] if title else "Project"
    reqs = "\n".join(f"- {f}" for f in features) or "- Agree on the essential features together; keep the scope small."
    done_lines = f"- {done.strip()}" + (f"\n- `{verify}` passes (the bridge checks this before the run counts as done)"
                                        if verify else "")
    return f"""# {title}

## Goal
{goal.strip()}

## Stack
{stack.strip() or "Your choice: pick something simple and well-suited, and say why."}

## Requirements
{reqs}

## Done when
{done_lines}

## Notes for the agents
- Work only inside this folder. Keep the README up to date.
- Write tests alongside the code and run them before declaring anything done.
- Prefer small, working increments over big rewrites.
"""


async def run_wizard(app: "App") -> None:
    say = lambda msg: app.console.print(Text(msg, style="cyan"))  # noqa: E731
    say("Let's set up a project. Esc cancels at any step.\n")

    goal = await ask_text("1/6 What should they build? ")
    if not goal or not goal.strip():
        app.warn("Cancelled.")
        return
    stack = await ask_text("2/6 Language / stack (empty = let them choose): ")
    if stack is None:
        return
    feats = await ask_text("3/6 Must-have features, comma-separated (optional): ")
    if feats is None:
        return
    features = [f.strip() for f in feats.split(",") if f.strip()]
    done = await ask_text("4/6 Done when… ", DEFAULT_DONE)
    if done is None:
        return

    sugg = suggest_tests(stack)
    opts = [(c, c, "suggested" if i == 0 else "") for i, c in enumerate(sugg)]
    opts += [("__custom__", "custom…", "type your own command"), ("", "no verification", "not recommended for builds")]
    verify = await pick("5/6 Command that proves it works (run when both agents say done)", opts)
    if verify is None:
        return
    if verify == "__custom__":
        verify = await ask_text("Verify command: ")
        if verify is None:
            return
        verify = verify.strip()

    default_dir = app.settings.workdir or str(Path.home() / "projects" / slugify(goal))
    folder = await ask_text("6/6 Project folder: ", tilde(default_dir) if default_dir.startswith(str(Path.home()))
                            else default_dir, PathCompleter(expanduser=True, only_directories=True))
    if not folder or not folder.strip():
        return
    wd = Path(folder.strip()).expanduser().resolve()
    if wd.exists() and not wd.is_dir():
        app.warn(f"{wd} is a file.")
        return
    wd.mkdir(parents=True, exist_ok=True)

    perms = await pick("Permissions", [
        ("auto", "auto", "edit files AND run commands like tests (recommended for builds)"),
        ("edits", "edits", "edit files only; they can't run the tests themselves"),
    ])
    if perms is None:
        return

    team = await pick("Who does what?", TEAM_CHOICES)
    if team is None:
        return

    spec_path = wd / "SPEC.md"
    if spec_path.exists() and not await confirm(f"{tilde(spec_path)} exists. Overwrite it?"):
        return
    spec = render_spec(goal, stack, features, done, verify)

    # git: make sure checkpoints can work (needs a clean repo)
    repo_note = ""
    if workspace.git_available():
        fresh = not await workspace.is_repo(wd)
        if fresh:
            await workspace.init_repo(wd)
            repo_note = "initialized git"
        # a brand-new repo only contains what we just created; an existing one may hold the user's work
        dirty_before = False if fresh else await workspace.is_dirty(wd)
        spec_path.write_text(spec)
        if not dirty_before:
            try:
                await workspace.checkpoint(wd, "Initial commit with SPEC.md (agent-bridge /init)" if fresh
                                           else "Add SPEC.md (agent-bridge /init)")
                repo_note = (repo_note + ", " if repo_note else "") + "committed SPEC.md"
            except RuntimeError as e:
                app.warn(f"Couldn't commit SPEC.md: {e}")
        else:
            app.warn("The folder has uncommitted changes, so per-turn checkpoints stay off until you commit them.")
    else:
        spec_path.write_text(spec)

    s = app.settings
    preset, first = team.split(":")
    s.workdir, s.verify, s.perms, s.preset, s.first = str(wd), verify, perms, preset, first
    s.role_a = s.role_b = ""
    s.claude_role = s.agy_role = ""  # follow the chosen team
    s.turns = max(s.turns, 16)
    app.engine = None
    say(f"\nWrote {tilde(spec_path)}" + (f" ({repo_note})" if repo_note else ""))
    roles = " · ".join(f"{NAMES[a]}: {role_name(role_for(s, a)[0])}" for a in ("claude", "agy"))
    say(f"  {roles}\n  mode {preset} · perms {perms} · turns {s.turns} · verify {verify or '(none)'}\n"
        f"  workdir {tilde(wd)}")
    if perms == "auto":
        app.warn("  auto lets both agents edit files and run commands in that folder.")

    if await confirm("Start the build now?", default_yes=True):
        topic = (f"Build the project described in SPEC.md in the working directory. "
                 f"Follow it closely.\n\n{spec}")
        app.start_new(topic)
    else:
        say("Ready. Type a message to start, or edit SPEC.md first and use /spec SPEC.md.")
