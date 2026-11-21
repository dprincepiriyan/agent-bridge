from __future__ import annotations

import os
import shutil

from .base import Adapter, AgentError, TurnContext, TurnResult


def make_adapters() -> dict[str, Adapter]:
    """Real adapters, or offline fakes when AGENT_BRIDGE_FAKE is set."""
    if os.environ.get("AGENT_BRIDGE_FAKE"):
        from .fake import FakeAdapter
        delay = float(os.environ.get("AGENT_BRIDGE_FAKE_DELAY", "0.3"))
        return {"claude": FakeAdapter("claude", delay=delay), "agy": FakeAdapter("agy", delay=delay)}
    from .agy import AgyAdapter
    from .claude import ClaudeAdapter
    return {"claude": ClaudeAdapter(), "agy": AgyAdapter()}


def missing_executables(adapters: dict[str, Adapter]) -> list[str]:
    return [a.executable for a in adapters.values() if a.executable and not shutil.which(a.executable)]


__all__ = ["Adapter", "AgentError", "TurnContext", "TurnResult", "make_adapters", "missing_executables"]
