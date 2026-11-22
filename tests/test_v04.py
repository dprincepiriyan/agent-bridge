"""Notifications, /diff, /init wizard, interrupted-turn handling."""
import asyncio
import subprocess

import pytest

from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.config import Settings
from agent_bridge.engine import Engine, Event
from agent_bridge.notify import Notifier
from agent_bridge.tui.wizard import render_spec, slugify, suggest_tests


# ------------------------------------------------------------------ notifications
def run_notifier(reason, secs_ago=100, enabled=True, **data):
    sent = []
    n = Notifier(lambda: enabled, lambda: 30, sender=lambda *a: sent.append(a))
    n(Event("run_start"))
    n.started -= secs_ago
    n(Event("done", data={"reason": reason, "topic": "Build a bank app", "max_turns": 8, **data}))
    return sent


def test_notifies_on_natural_endings():
    assert run_notifier("agreed")[0][0].startswith("✔")
    assert run_notifier("limit")[0][1] == "Build a bank app"
    assert run_notifier("verify_failed")[0][2] == "critical"


def test_no_notification_for_user_stops_short_runs_or_when_off():
    assert run_notifier("interrupted") == []
    assert run_notifier("paused") == []
    assert run_notifier("agreed", secs_ago=5) == []
    assert run_notifier("agreed", enabled=False) == []


def test_usage_limit_always_notifies_with_reset():
    sent = run_notifier("usage_limit", secs_ago=1, agent="claude", resets_at=1795293600)
    assert "Claude hit its usage limit" in sent[0][0] and "Resets at" in sent[0][1]


# ------------------------------------------------------------------ interrupted turns
def test_interrupted_turn_is_flagged_and_resent_with_note():
    async def go():
        ad = {"claude": FakeAdapter("claude", delay=0.05), "agy": FakeAdapter("agy", delay=0.3)}
        ev = []
        eng = Engine.new(Settings(turns=4), "t", ad, ev.append)
        task = asyncio.create_task(eng.run())
        while eng.state["turn"] < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # agy is mid-turn now
        eng.add_user_message("typed mid-turn")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return eng, ad, ev

    eng, ad, ev = asyncio.run(go())
    assert eng.state["interrupted"] == {"agent": "agy", "turn": 2}
    assert any("will go to Antigravity" in e.text for e in ev if e.kind == "notice")
    # agy's first call never completed, so it isn't 'started' → no duplicate note needed yet;
    # simulate an agent that had started and was cut off on a later turn:
    eng.state["agents"]["agy"]["started"] = True
    eng.retry_backoff = 0
    asyncio.run(eng.run())
    assert "re-sent" in ad["agy"].prompts[1] and "typed mid-turn" in ad["agy"].prompts[1]
    assert eng.state["interrupted"] is None


def test_claude_rotates_session_when_first_call_cancelled(tmp_path, monkeypatch):
    import sys
    from agent_bridge.agents.base import TurnContext
    from agent_bridge.agents.claude import ClaudeAdapter
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    agent = {"started": False, "session": "first-id"}

    async def go():
        t = asyncio.create_task(ClaudeAdapter().send(agent, "hi", TurnContext(tmp_path, "none", 60, "S"),
                                                     print, print))
        await asyncio.sleep(0.3)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert agent["session"] != "first-id"


# ------------------------------------------------------------------ wizard helpers
def test_wizard_helpers():
    assert suggest_tests("Java + Maven")[0] == "mvn -q test"
    assert suggest_tests("Django")[0] == "python -m pytest -q"  # 'go' inside 'django' must not match
    assert "go test ./..." not in suggest_tests("Django")
    assert suggest_tests("")[0] == "python -m pytest -q"
    assert slugify("Build a bank system application in Java") == "bank-system-application-java"
    spec = render_spec("a todo CLI", "Python", ["add", "list"], "tests pass", "python -m pytest -q")
    assert "## Done when" in spec and "`python -m pytest -q` passes" in spec and "- add\n- list" in spec
