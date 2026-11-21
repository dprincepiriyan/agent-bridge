"""Shared adapter interface and a safe streaming subprocess runner."""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class AgentError(RuntimeError):
    pass


@dataclass
class TurnContext:
    workdir: Path
    perms: str
    timeout: int
    system_prompt: str


@dataclass
class TurnResult:
    text: str
    meta: str = ""
    cost: float | None = None
    tokens: int | None = None


TextCb = Callable[[str], None]


class Adapter:
    name = ""
    label = ""
    executable = ""

    async def send(self, agent: dict, prompt: str, ctx: TurnContext,
                   on_text: TextCb, on_tool: TextCb) -> TurnResult:
        raise NotImplementedError

    async def list_models(self, refresh: bool = False) -> list[tuple[str, str]]:
        """[(model_id, label)], without the 'default' entry."""
        return []


def _kill(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def stream_process(cmd: list[str], cwd: Path, timeout: int,
                         on_line: Callable[[str], None]) -> tuple[int, str]:
    """Run cmd, feeding each stdout line to on_line. Kills the whole process group
    on timeout or cancellation. Returns (returncode, stderr)."""
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
            async for raw in proc.stdout:
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
