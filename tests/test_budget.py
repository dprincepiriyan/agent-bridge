import asyncio

from agent_bridge import config
from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.config import Settings
from agent_bridge.engine import Engine
from agent_bridge.notify import Notifier
from agent_bridge.engine import Event


def new(settings, **ad):
    adapters = {"claude": ad.get("claude") or FakeAdapter("claude"), "agy": ad.get("agy") or FakeAdapter("agy")}
    ev = []
    eng = Engine.new(settings, "t", adapters, ev.append)
    eng.retry_backoff = 0
    return eng, adapters, ev


def test_turn_budget_is_a_hard_cap_across_continues():
    eng, _, ev = new(Settings(turns=4, budget_turns=5))
    assert asyncio.run(eng.run()) == "limit"
    eng.extend(10)  # like typing a message to continue
    assert asyncio.run(eng.run()) == "budget"
    assert eng.state["turn"] == 5
    done = ev[-1].data
    assert done["budget"] == "turns" and "5 turns used (budget 5)" in done["message"]
    assert asyncio.run(eng.run()) == "budget"  # /continue without raising it stops again immediately
    assert eng.state["turn"] == 5
    eng.state["budget"]["turns"] = 0  # /budget turns off
    eng.extend(1)
    assert asyncio.run(eng.run()) == "limit"


def test_time_budget_counts_active_time_only():
    eng, _, _ = new(Settings(turns=50, budget_minutes=1),
                    claude=FakeAdapter("claude", delay=0.05), agy=FakeAdapter("agy", delay=0.05))
    eng.state["active_secs"] = 59.8  # nearly used up from earlier runs
    assert asyncio.run(eng.run()) == "budget"
    assert 1 <= eng.state["turn"] < 50 and eng.state["active_secs"] >= 60


def test_cost_budget():
    class Pricey(FakeAdapter):
        async def send(self, *a, **k):
            r = await super().send(*a, **k)
            r.cost = 0.4
            return r
    eng, _, ev = new(Settings(turns=20, budget_cost=1.0), claude=Pricey("claude"))
    assert asyncio.run(eng.run()) == "budget"
    assert ev[-1].data["budget"] == "cost" and eng.cost >= 1.0


def test_usage_budget_reads_claude_window():
    claude = FakeAdapter("claude")
    claude.limits = {"five_hour": 0.5}
    eng, _, ev = new(Settings(turns=10, budget_usage=80), claude=claude)

    class Climb(FakeAdapter):
        async def send(self, *a, **k):
            claude.limits = {"five_hour": claude.limits["five_hour"] + 0.2}
            return await super().send(*a, **k)
    eng.adapters["agy"] = Climb("agy")
    assert asyncio.run(eng.run()) == "budget"
    assert ev[-1].data["budget"] == "usage" and "at 90%" in ev[-1].data["message"]
    assert eng.budget_status() == ["Claude 5h 90%/80%"]


def test_budget_from_settings_and_config_roundtrip():
    s = Settings(budget_minutes=30, budget_turns=50, budget_usage=80, budget_cost=2.5)
    config.save(s)
    loaded, warn = config.load()
    assert warn == [] and loaded.budget_cost == 2.5 and loaded.budget_usage == 80
    eng, _, _ = new(s)
    assert eng.state["budget"] == {"minutes": 30, "turns": 50, "usage": 80, "cost": 2.5}
    bad = Settings(budget_usage=150, budget_turns=-1)
    assert len(bad.validate()) == 2 and bad.budget_usage == 0


def test_budget_notifies_even_for_short_runs():
    sent = []
    n = Notifier(lambda: True, lambda: 30, sender=lambda *a: sent.append(a))
    n(Event("run_start"))
    n(Event("done", data={"reason": "budget", "message": "50 turns used (budget 50)", "topic": "pong"}))
    assert sent and sent[0][0] == "⏸ Budget reached" and "50 turns used" in sent[0][1]
