"""Publishing a project to GitHub: git remote/push helpers and `gh` integration."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .workspace import git, _run

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
URL_RE = re.compile(r"^(git@github\.com:|https://github\.com/)([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(\.git)?/?$")


def valid_repo_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and name not in (".", "..")


def suggest_name(folder: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", folder.name).strip("-.") or "project"
    return name[:100]


def parse_github_url(url: str) -> tuple[str, str] | None:
    """'git@github.com:me/app.git' or 'https://github.com/me/app' → ('me', 'app')."""
    m = URL_RE.match(url.strip())
    return (m.group(2), m.group(3)) if m else None


def web_url(remote: str) -> str:
    parsed = parse_github_url(remote)
    return f"https://github.com/{parsed[0]}/{parsed[1]}" if parsed else remote


# ---------------------------------------------------------------- git state
async def remote_url(cwd: Path, name: str = "origin") -> str | None:
    code, out = await git(cwd, "remote", "get-url", name)
    return out.strip() if code == 0 and out.strip() else None


async def current_branch(cwd: Path) -> str | None:
    code, out = await git(cwd, "symbolic-ref", "--short", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


async def has_commits(cwd: Path) -> bool:
    code, _ = await git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    return code == 0


async def commit_count(cwd: Path) -> int:
    code, out = await git(cwd, "rev-list", "--count", "HEAD")
    return int(out.strip()) if code == 0 and out.strip().isdigit() else 0


async def unpushed_count(cwd: Path) -> int | None:
    """Commits not on the upstream branch; None if there's no upstream yet."""
    code, out = await git(cwd, "rev-list", "--count", "@{upstream}..HEAD")
    return int(out.strip()) if code == 0 and out.strip().isdigit() else None


async def add_remote(cwd: Path, url: str) -> tuple[bool, str]:
    code, out = await git(cwd, "remote", "add", "origin", url)
    return code == 0, out.strip()


async def push(cwd: Path, branch: str) -> tuple[bool, str]:
    """Plain push (never forced), setting upstream."""
    code, out = await git(cwd, "push", "-u", "origin", branch, timeout=300)
    return code == 0, out.strip()


# ---------------------------------------------------------------------- gh
def gh_installed() -> bool:
    return shutil.which("gh") is not None


async def gh_logged_in() -> bool:
    if not gh_installed():
        return False
    code, _ = await _run(["gh", "auth", "status", "--hostname", "github.com"], Path.cwd(), 30)
    return code == 0


async def gh_user() -> str | None:
    code, out = await _run(["gh", "api", "user", "--jq", ".login"], Path.cwd(), 30)
    return out.strip() if code == 0 and out.strip() else None


async def gh_create_and_push(cwd: Path, name: str, private: bool, description: str = "") -> tuple[bool, str]:
    args = ["gh", "repo", "create", name, "--private" if private else "--public",
            "--source", ".", "--remote", "origin", "--push"]
    if description:
        args += ["--description", description[:300]]
    code, out = await _run(args, cwd, 300)
    return code == 0, out.strip()
