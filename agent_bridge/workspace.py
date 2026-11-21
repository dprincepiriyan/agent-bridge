"""Workdir helpers: git checkpoints and the verification command."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

FALLBACK_IDENTITY = ["-c", "user.name=agent-bridge", "-c", "user.email=agent-bridge@localhost"]


async def _run(args: list[str], cwd: Path, timeout: float = 60) -> tuple[int, str]:
    """Run a command, merging stderr into stdout. Kills its process group on timeout/cancel."""
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        _killpg(proc)
        await proc.wait()
        return 124, f"(timed out after {timeout:.0f}s)"
    except asyncio.CancelledError:
        _killpg(proc)
        raise
    return proc.returncode, out.decode(errors="replace")


def _killpg(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------- verification
async def run_verify(command: str, cwd: Path, timeout: int) -> tuple[int, str]:
    shell = shutil.which("bash") or "/bin/sh"
    return await _run([shell, "-c", command], cwd, timeout)


def tail(text: str, lines: int = 40, chars: int = 6000) -> str:
    t = "\n".join(text.rstrip().splitlines()[-lines:])
    return t[-chars:]


# ------------------------------------------------------------------------ git
def git_available() -> bool:
    return shutil.which("git") is not None


async def git(cwd: Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    return await _run(["git", *args], cwd, timeout)


async def is_repo(cwd: Path) -> bool:
    if not git_available():
        return False
    code, out = await git(cwd, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


async def is_dirty(cwd: Path) -> bool:
    code, out = await git(cwd, "status", "--porcelain", "--", ".")
    return code == 0 and bool(out.strip())


async def has_identity(cwd: Path) -> bool:
    code, out = await git(cwd, "config", "user.email")
    return code == 0 and bool(out.strip())


DEFAULT_GITIGNORE = """# added by agent-bridge for its own workspace
__pycache__/
*.py[cod]
.venv/
venv/
node_modules/
target/
dist/
build/
.pytest_cache/
.DS_Store
"""


async def init_repo(cwd: Path) -> bool:
    if not git_available():
        return False
    code, _ = await git(cwd, "init", "-q")
    gi = cwd / ".gitignore"
    if code == 0 and not gi.exists():
        gi.write_text(DEFAULT_GITIGNORE)
    return code == 0


async def checkpoint(cwd: Path, message: str) -> tuple[str, str] | None:
    """Commit all changes. Returns (short sha, shortstat) or None if nothing changed."""
    if not await is_dirty(cwd):
        return None
    code, out = await git(cwd, "add", "-A", "--", ".")
    if code != 0:
        raise RuntimeError(f"git add failed: {out.strip()[:300]}")
    _, stat = await git(cwd, "diff", "--cached", "--shortstat", "--", ".")
    ident = [] if await has_identity(cwd) else FALLBACK_IDENTITY
    code, out = await git(cwd, *ident, "commit", "-q", "--no-verify", "-m", message, "--", ".")
    if code != 0:
        raise RuntimeError(f"git commit failed: {out.strip()[:300]}")
    _, sha = await git(cwd, "rev-parse", "--short", "HEAD")
    return sha.strip(), stat.strip()


async def revert(cwd: Path, sha: str) -> tuple[bool, str]:
    ident = [] if await has_identity(cwd) else FALLBACK_IDENTITY
    code, out = await git(cwd, *ident, "revert", "--no-edit", sha)
    if code != 0:
        await git(cwd, "revert", "--abort")
    return code == 0, out.strip()
