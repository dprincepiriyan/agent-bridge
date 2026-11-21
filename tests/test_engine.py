import asyncio
import json

import pytest

from agent_bridge import store
from agent_bridge.agents.base import AgentError
from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.config import Settings
from agent_bridge.engine import Engine, has_stop


def make(settings=None, topic="test topic", claude=None, agy=None):
    events = []
    adapters = {"claude": claude or FakeAdapter("claude"), "agy": agy or FakeAdapter("agy")}
    eng = Engine.new(settings or Settings(turns=4), topic, adapters, events.append)
    eng.retry_backoff = 0
    return eng, adapters, events


def kinds(events):
    return [e.kind for e in events]


def test_runs_to_turn_limit_alternating():
    eng, ad, ev = make()
    assert asyncio.run(eng.run()) == "limit"
    assert eng.state["turn"] == 4
    assert len(ad["claude"].prompts) == 2 and len(ad["agy"].prompts) == 2
    # first prompts carry the briefing; later ones only the partner's message
    assert "YOUR ROLE" in ad["claude"].prompts[0] and "You speak first" in ad["claude"].prompts[0]
    assert "YOUR ROLE" in ad["agy"].prompts[0] and "Claude said:" in ad["agy"].prompts[0]
    assert ad["claude"].prompts[1].startswith("Antigravity said:")
    assert "YOUR ROLE" not in ad["claude"].prompts[1]
    assert kinds(ev)[0] == "run_start" and kinds(ev)[-1] == "done"


def test_first_speaker_agy():
    eng, ad, _ = make(Settings(turns=2, first="agy"))
    asyncio.run(eng.run())
    assert "You speak first" in ad["agy"].prompts[0]
    assert "Antigravity said:" in ad["claude"].prompts[0]


def test_stop_handshake_agreed():
    eng, _, _ = make(Settings(turns=10), claude=FakeAdapter("claude", ["done\n[[DONE]]"]),
                     agy=FakeAdapter("agy", ["agreed\n[[DONE]]"]))
    assert asyncio.run(eng.run()) == "agreed"
    assert eng.state["finished"] and eng.state["turn"] == 2


def test_stop_objection_resets():
    eng, _, _ = make(Settings(turns=4), claude=FakeAdapter("claude", ["done\n[[DONE]]", "ok more"]),
                     agy=FakeAdapter("agy", ["no, not done", "x"]))
    assert asyncio.run(eng.run()) == "limit"
    assert not eng.state["finished"]


def test_confirmation_turn_granted_at_limit():
    eng, _, ev = make(Settings(turns=1), claude=FakeAdapter("claude", ["[[DONE]]"]),
                      agy=FakeAdapter("agy", ["[[DONE]]"]))
    assert asyncio.run(eng.run()) == "agreed"
    assert eng.state["turn"] == 2
    assert "notice" in kinds(ev)


def test_retry_then_success():
    eng, ad, ev = make(Settings(turns=1, retries=2), claude=FakeAdapter("claude", fail_times=2))
    assert asyncio.run(eng.run()) == "limit"
    assert kinds(ev).count("retry") == 2


def test_retries_exhausted_is_error_and_resumable():
    eng, ad, ev = make(Settings(turns=2, retries=1), claude=FakeAdapter("claude", fail_times=5))
    assert asyncio.run(eng.run()) == "error"
    assert eng.state["turn"] == 0
    assert "error" in kinds(ev)


def test_user_message_goes_to_next_agent_once():
    eng, ad, _ = make(Settings(turns=3))
    eng.add_user_message("please use Rust")
    asyncio.run(eng.run())
    assert "The user says:\nplease use Rust" in ad["claude"].prompts[0]
    assert "please use Rust" not in ad["agy"].prompts[0]
    assert "please use Rust" in store.transcript_path(eng.state).read_text()


def test_message_typed_during_turn_is_not_lost():
    class Hook(FakeAdapter):
        async def send(self, agent, prompt, ctx, on_text, on_tool):
            if len(self.prompts) == 0:
                eng.add_user_message("mid-turn note")
            return await super().send(agent, prompt, ctx, on_text, on_tool)

    global eng
    eng, ad, _ = make(Settings(turns=2), claude=Hook("claude"))
    asyncio.run(eng.run())
    assert "mid-turn note" not in ad["claude"].prompts[0]
    assert "mid-turn note" in ad["agy"].prompts[0]


def test_pause_after_current_turn():
    class PauseHook(FakeAdapter):
        async def send(self, agent, prompt, ctx, on_text, on_tool):
            peng.request_pause()
            return await super().send(agent, prompt, ctx, on_text, on_tool)

    global peng
    peng, _, _ = make(Settings(turns=6), claude=PauseHook("claude"))
    assert asyncio.run(peng.run()) == "paused"
    assert peng.state["turn"] == 1


def test_cancel_saves_and_resume_continues():
    async def go():
        eng, ad, ev = make(Settings(turns=6), claude=FakeAdapter("claude", delay=0.05),
                           agy=FakeAdapter("agy", delay=0.05))
        task = asyncio.create_task(eng.run())
        while eng.state["turn"] < 1:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return eng, ev

    eng, ev = asyncio.run(go())
    assert ev[-1].data["reason"] == "interrupted"
    turn = eng.state["turn"]
    # reload from disk and continue
    ad2 = {"claude": FakeAdapter("claude"), "agy": FakeAdapter("agy")}
    eng2 = Engine.load(eng.run_dir, ad2)
    eng2.retry_backoff = 0
    assert eng2.state["turn"] == turn
    asyncio.run(eng2.run())
    assert eng2.state["turn"] == 6


def test_extend_and_set_max_turns():
    eng, _, _ = make(Settings(turns=2))
    asyncio.run(eng.run())
    eng.extend(3)
    assert eng.state["max_turns"] == 5
    assert eng.set_max_turns(1) == 2  # can't go below turns already taken


def test_model_and_effort_passed_through():
    eng, ad, _ = make(Settings(turns=2, claude_model="opus", agy_effort="high", perms="edits"))
    asyncio.run(eng.run())
    assert ad["claude"].calls[0]["model"] == "opus"
    assert ad["agy"].calls[0]["effort"] == "high"
    assert ad["claude"].calls[0]["perms"] == "edits"


def test_v1_state_upgrade(tmp_path):
    d = store.runs_dir() / "20261121-000000"
    d.mkdir(parents=True)
    v1 = {"version": 1, "run_dir": str(d), "workdir": str(d / "workspace"), "topic": "t", "preset": "debate",
          "perms": "none", "max_turns": 3, "turn": 3, "next": "agy", "last_msg": "hi", "stop": "[[DONE]]",
          "stop_pending": None, "finished": False, "timeout": 600, "retries": 2, "max_words": 350,
          "claude_model": "sonnet", "agy_model": None,
          "agents": {"claude": {"role": "a", "started": True, "session": "s1", "cost": 0.1},
                     "agy": {"role": "b", "started": True, "session": "s2"}}}
    (d / "state.json").write_text(json.dumps(v1))
    st = store.load_state(d)
    assert st["version"] == store.SCHEMA
    assert st["own_workspace"] is True and st["checkpoint_log"] == []
    assert st["agents"]["claude"]["model"] == "sonnet"
    assert st["agents"]["agy"]["model"] == ""
    assert st["pending_user"] == []


def test_has_stop():
    assert has_stop("hello\n[[DONE]]", "[[DONE]]")
    assert has_stop("hello\n  [[DONE]]  \n", "[[DONE]]")
    assert not has_stop("I won't say [[DONE]] yet", "[[DONE]]")
