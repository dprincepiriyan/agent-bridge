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


def test_parse_phrases():
    from agent_bridge.budget import parse, describe
    assert parse("30m") == {"minutes": 30}
    assert parse("2h") == {"minutes": 120}
    assert parse("1h30m") == parse("1h 30m") == parse("1 h 30 min") == {"minutes": 90}
    assert parse("50 turns") == parse("turns 50") == parse("50t") == {"turns": 50}
    assert parse("usage 80%") == parse("80%") == {"usage": 80}
    assert parse("cost $5") == {"cost": 5.0} and parse("$2.5") == {"cost": 2.5}
    assert parse("cost 2.5") == {"cost": 2.5}
    assert parse("2h 50 turns usage 80%") == {"minutes": 120, "turns": 50, "usage": 80}
    assert parse("off") == {"minutes": 0, "turns": 0, "usage": 0, "cost": 0}
    assert parse("time off") == {"minutes": 0} and parse("turns off") == {"turns": 0}
    assert describe({"minutes": 90, "turns": 50, "usage": 80, "cost": 5.0}) == \
        "time 1h30m · 50 turns · Claude 5h usage 80% · cost $5"
    import pytest
    for bad in ("banana", "usage 150%", "cost", "turns -3"):
        with pytest.raises(ValueError):
            parse(bad)


def test_cli_budget_flags_and_resume(monkeypatch):
    import agent_bridge.cli as cli
    seen = {}
    monkeypatch.setattr(cli, "run_headless", lambda s, args: seen.update(s=s) or 0)
    assert cli.main(["x", "--budget-minutes", "30", "--budget-usage", "80", "--budget-cost", "2.5"]) == 0
    s = seen["s"]
    assert (s.budget_minutes, s.budget_usage, s.budget_cost) == (30, 80, 2.5)


def test_teams_carry_budgets():
    from agent_bridge import teams
    teams.save_team("night", Settings(budget_minutes=120, budget_cost=3.0))
    t = Settings()
    teams.apply_team(t, teams.load_team("night"))
    assert t.budget_minutes == 120 and t.budget_cost == 3.0
