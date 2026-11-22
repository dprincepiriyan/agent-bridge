"""/publish: put the project on GitHub. Only ever runs when the user asks; never force-pushes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from .. import github, workspace
from ..render import tilde
from .pickers import ask_text, confirm, pick

if TYPE_CHECKING:
    from .app import App


def _spec_description(wd: Path) -> str:
    spec = wd / "SPEC.md"
    if spec.exists():
        for line in spec.read_text().splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return ""


def _project_dir(app: "App", arg: str) -> Path | None:
    if arg:
        return Path(arg).expanduser().resolve()
    if app.engine:
        return app.engine.workdir
    if app.settings.workdir:
        return Path(app.settings.workdir)
    return None


async def run_publish(app: "App", arg: str = "") -> None:
    say = lambda m, style="cyan": app.console.print(Text(m, style=style))  # noqa: E731
    wd = _project_dir(app, arg)
    if wd is None:
        app.warn("No project yet. Start or /resume a conversation, set /workdir, or use /publish <folder>.")
        return
    if not wd.is_dir():
        app.warn(f"{wd} doesn't exist.")
        return
    if not workspace.git_available() or not await workspace.is_repo(wd):
        app.warn(f"{tilde(wd)} isn't a git repository, so there's nothing to publish. "
                 "(Projects from /init and per-run workspaces are git repos automatically.)")
        return

    # 1. leftovers → commit (only with consent)
    if await workspace.is_dirty(wd):
        _, status = await workspace.git(wd, "status", "--short", "--", ".")
        files = status.strip().splitlines()
        say(f"{len(files)} uncommitted change(s) in {tilde(wd)}:", "yellow")
        for f in files[:10]:
            app.console.print(Text(f"  {f}", style="dim"))
        if not await confirm("Commit them before publishing?", default_yes=True):
            say("Publishing only what's already committed.")
        else:
            try:
                await workspace.checkpoint(wd, "agent-bridge: changes before publishing")
            except RuntimeError as e:
                app.warn(str(e))
                return
    if not await github.has_commits(wd):
        app.warn("No commits yet — nothing to publish.")
        return
    branch = await github.current_branch(wd)
    if not branch:
        app.warn("HEAD is detached; check out a branch first.")
        return

    # 2. existing remote → just push
    remote = await github.remote_url(wd)
    if remote:
        pending = await github.unpushed_count(wd)
        if pending == 0:
            say(f"Already up to date: {github.web_url(remote)}")
            return
        what = f"{pending} new commit(s)" if pending is not None else f"branch {branch}"
        if not await confirm(f"Push {what} to {github.web_url(remote)}?", default_yes=True):
            return
        say("Pushing…")
        ok, out = await github.push(wd, branch)
        if ok:
            say(f"✔ Pushed. {github.web_url(remote)}", "bold green")
        else:
            app.warn(_push_help(out))
        return

    # 3. no remote → create one
    name = await ask_text("Repository name: ", github.suggest_name(wd))
    if name is None:
        return
    name = name.strip()
    if not github.valid_repo_name(name):
        app.warn("Repository names may only contain letters, numbers, '.', '-' and '_'.")
        return
    vis = await pick("Visibility", [("private", "private", "only you (recommended)"),
                                    ("public", "public", "anyone can see it")])
    if vis is None:
        return
    commits = await github.commit_count(wd)

    if await github.gh_logged_in():
        user = await github.gh_user() or "your account"
        if not await confirm(f"Create {vis} repo {user}/{name} and push {commits} commit(s) on {branch}?",
                             default_yes=True):
            return
        say("Creating the repository and pushing…")
        ok, out = await github.gh_create_and_push(wd, name, vis == "private", _spec_description(wd))
        if not ok:
            app.warn(f"gh failed: {out[-600:]}")
            return
        remote = await github.remote_url(wd) or ""
        say(f"✔ Published ({vis}). {github.web_url(remote) if remote else ''}", "bold green")
        return

    # 4. no gh login → paste the URL of an empty repo
    say(("The GitHub CLI isn't logged in, so I can't create the repo for you.\n"
         "  Either run  gh auth login  in another terminal and /publish again (one-time setup), or create an EMPTY repo now at "
         f"https://github.com/new (name: {name}, {vis}, no README) and paste its URL."), "yellow")
    url = await ask_text("Repo URL (git@github.com:you/name.git): ")
    if not url or not url.strip():
        return
    url = url.strip()
    parsed = github.parse_github_url(url)
    if not parsed:
        app.warn("That doesn't look like a GitHub repo URL.")
        return
    if url.startswith("https://"):  # your SSH key works; https would ask for a password
        url = f"git@github.com:{parsed[0]}/{re.sub(r'.git$', '', parsed[1])}.git"
    if not await confirm(f"Push {commits} commit(s) on {branch} to {github.web_url(url)}?", default_yes=True):
        return
    ok, out = await github.add_remote(wd, url)
    if not ok:
        app.warn(f"Couldn't add the remote: {out}")
        return
    say("Pushing…")
    ok, out = await github.push(wd, branch)
    if ok:
        say(f"✔ Published. {github.web_url(url)}", "bold green")
    else:
        app.warn(_push_help(out))


def _push_help(out: str) -> str:
    low = out.lower()
    if "rejected" in low or "fetch first" in low or "non-fast-forward" in low:
        hint = ("The GitHub repo has commits this folder doesn't (e.g. a README created on github.com). "
                "Pull them first (git pull --rebase origin <branch>) — I never force-push.")
    elif "permission denied" in low or "could not read from remote" in low:
        hint = "GitHub refused access. Check the URL and that your SSH key is added to your account."
    elif "repository not found" in low:
        hint = "That repository doesn't exist (or you don't have access). Create it first, empty."
    else:
        hint = "Push failed."
    return f"{hint}\n  git said: {out[-400:]}"
