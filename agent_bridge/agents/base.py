"""Shared adapter interface and a safe streaming subprocess runner."""
from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class AgentError(RuntimeError):
    pass


class ConfigError(AgentError):
    """Bad settings (e.g. an unknown model). Retrying can't help; the user must change something."""


_BAD_MODEL_RE = re.compile(
    r"invalid model|model\b.{0,80}\b(not (recognized|found|available|supported)|does not exist|unknown)"
    r"|issue with the selected model|unknown model", re.I | re.S)


def looks_like_bad_model(text: str) -> bool:
    return bool(text and _BAD_MODEL_RE.search(text))


class UsageLimitError(AgentError):
    """The agent's subscription/quota limit was hit. Retrying won't help."""

    def __init__(self, msg: str, resets_at: float | None = None):
        super().__init__(msg)
        self.resets_at = resets_at


_LIMIT_RE = re.compile(
    r"(usage|rate)[ _-]?limit|limit (reached|exceeded)|hit your limit|out of (credits|quota)|quota"
    r"|resource[_ ]exhausted|too many requests|\b429\b", re.I)


def looks_like_limit(text: str) -> bool:
    return bool(text and _LIMIT_RE.search(text))


def parse_reset(text: str) -> float | None:
    """Find a unix-epoch reset time like 'limit reached|1795293600'."""
    m = re.search(r"\b(1[7-9]\d{8})\b", text or "")
    return float(m.group(1)) if m else None


@dataclass
class TurnContext:
    workdir: Path
    perms: str
    timeout: int                 # hard cap for the whole call
    system_prompt: str
    idle_timeout: int = 600      # kill if no output at all for this long
    ephemeral: bool = False      # one-off call (judge): don't create/resume a session


@dataclass
class TurnResult:
    text: str
    meta: str = ""
    cost: float | None = None
    tokens: int | None = None


TextCb = Callable[[str], None]

# tool name → verb shown to the user (Claude names first, then Antigravity's)
_TOOL_VERBS = {
    "edit": "Edit", "multiedit": "Edit", "write": "Write", "read": "Read", "bash": "Run",
    "glob": "Find", "grep": "Search", "notebookedit": "Edit", "webfetch": "Fetch", "websearch": "Search web",
    "read_file": "Read", "write_file": "Write", "write_to_file": "Write", "edit_file": "Edit",
    "replace_file_content": "Edit", "multi_replace_file_content": "Edit", "view_file": "Read",
    "run_command": "Run", "list_dir": "List", "find_by_name": "Find", "grep_search": "Search",
    "codebase_search": "Search", "read_url_content": "Fetch", "search_web": "Search web",
}
_TARGET_KEYS = ("file_path", "path", "Path", "TargetFile", "AbsolutePath", "command", "CommandLine",
                "pattern", "Pattern", "Query", "query", "url", "Url", "DirectoryPath", "SearchPath")


def describe_tool(name: str, params: dict | None, workdir: Path | None = None) -> str:
    """'Edit', {'file_path': '/w/src/a.py'} → 'Edit src/a.py'."""
    verb = _TOOL_VERBS.get((name or "").lower(), name or "tool")
    target = ""
    for k in _TARGET_KEYS:
        v = (params or {}).get(k)
        if isinstance(v, str) and v.strip():
            target = v.strip()
            break
    if target and workdir is not None:
        wd = str(workdir).rstrip("/")
        if target.startswith(wd + "/"):
            target = target[len(wd) + 1:]
        target = target.replace(wd + "/", "").replace(wd, ".")  # paths inside commands
    target = " ".join(target.split())
    if len(target) > 70:
        target = target[:69] + "…"
    return f"{verb} {target}".strip()


class Adapter:
    name = ""
    label = ""
    executable = ""
    limits: dict | None = None  # latest usage info the CLI reported, if any

    async def send(self, agent: dict, prompt: str, ctx: TurnContext,
                   on_text: TextCb, on_tool: TextCb) -> TurnResult:
        raise NotImplementedError

    async def list_models(self, refresh: bool = False) -> list[tuple[str, str]]:
        """[(model_id, label)], without the 'default' entry."""
        return []

    def cached_models(self) -> tuple[list[tuple[str, str]], bool]:
        """(models available instantly, whether they're fresh). Slow CLIs override this."""
        return [], False


def _kill(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def stream_process(cmd: list[str], cwd: Path, timeout: int, on_line: Callable[[str], None],
                         idle_timeout: int | None = None) -> tuple[int, str]:
    """Run cmd, feeding each stdout line to on_line. Kills the whole process group
    on timeout, prolonged silence, or cancellation. Returns (returncode, stderr)."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # nested `claude` should act as a fresh top-level run
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024, start_new_session=True)
    except FileNotFoundError:
        raise AgentError(f"`{cmd[0]}` not found on PATH")
    err_task = asyncio.create_task(proc.stderr.read())
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    raw = (await asyncio.wait_for(proc.stdout.readline(), idle_timeout) if idle_timeout
                           else await proc.stdout.readline())
                except TimeoutError:
                    raise AgentError(f"no output for {idle_timeout}s — the agent looks stuck")
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if line:
                    on_line(line)
            await proc.wait()
    except TimeoutError:
        raise AgentError(f"timed out after {timeout}s")
    finally:
        if proc.returncode is None:
            _kill(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), 3)
            except (TimeoutError, asyncio.CancelledError):
                _kill(proc, signal.SIGKILL)
        if not err_task.done():
            err_task.cancel()
    try:
        err = (await err_task).decode(errors="replace")
    except asyncio.CancelledError:
        err = ""
    return proc.returncode, err.strip()
