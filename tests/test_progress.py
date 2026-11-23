"""Shared PROGRESS.md memory."""
import asyncio

from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.config import Settings
from agent_bridge.engine import Engine
from agent_bridge.roles import progress_keeper


def new(settings, **ad):
    adapters = {"claude": ad.get("claude") or FakeAdapter("claude"), "agy": ad.get("agy") or FakeAdapter("agy")}
    eng = Engine.new(settings, "Build a ping pong game", adapters)
    eng.retry_backoff = 0
    return eng, adapters


def test_keeper_choice():
    assert progress_keeper("agy", {"claude": "orchestrator", "agy": "coder"}, "claude") == "agy"
    assert progress_keeper("auto", {"claude": "coder", "agy": "orchestrator"}, "claude") == "agy"
    assert progress_keeper("auto", {"claude": "coder", "agy": "reviewer"}, "claude") == "claude"
    assert progress_keeper("auto", {"claude": "architect", "agy": "orchestrator"}, "agy") == "agy"  # 2 leads


def test_state_fields():
    eng, _ = new(Settings(perms="edits", agy_role="orchestrator", claude_role="coder", first="agy"))
    st = eng.state
    assert st["progress"] is True and st["progress_keeper"] == "agy" and st["progress_every"] == 4
    eng, _ = new(Settings(perms="none"))
    assert eng.state["progress"] is False  # talk-only runs can't keep a file
    eng, _ = new(Settings(perms="auto", progress=False))
    assert eng.state["progress"] is False
