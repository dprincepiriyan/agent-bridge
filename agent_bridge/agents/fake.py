"""Offline adapter for tests and demos (AGENT_BRIDGE_FAKE=1). Spends no quota."""
from __future__ import annotations

import asyncio

from ..presets import NAMES
from .base import Adapter, AgentError, TurnResult


class FakeAdapter(Adapter):
    def __init__(self, name: str, replies: list[str] | None = None, fail_times: int = 0, delay: float = 0.0):
        self.name = name
        self.label = NAMES[name]
        self.replies = list(replies or [])
        self.fail_times = fail_times
        self.delay = delay
        self.prompts: list[str] = []
        self.calls: list[dict] = []

    async def send(self, agent, prompt, ctx, on_text, on_tool) -> TurnResult:
        self.prompts.append(prompt)
        self.calls.append({"model": agent.get("model"), "effort": agent.get("effort"), "perms": ctx.perms,
                           "started": agent.get("started")})
        if self.fail_times:
            self.fail_times -= 1
            raise AgentError("simulated failure")
        n = len(self.prompts)
        reply = self.replies.pop(0) if self.replies else (
            f"{self.label} here (reply {n}). I read your message and have thoughts.\n"
            f"Point one: this is a fake reply for testing.\nPoint two: no quota used.")
        for line in reply.splitlines(keepends=True):
            if self.delay:
                await asyncio.sleep(self.delay)
            on_text(line)
        agent["started"] = True
        agent["session"] = agent.get("session") or f"fake-{self.name}"
        return TurnResult(text=reply, meta="fake")

    async def list_models(self, refresh: bool = False):
        return [(f"{self.name}-fast", "Fake fast"), (f"{self.name}-smart", "Fake smart")]
