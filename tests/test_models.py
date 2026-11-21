import asyncio
import json
import sys

import pytest

from agent_bridge.agents.agy import AgyAdapter
from agent_bridge.agents.base import AgentError, ConfigError, TurnContext, looks_like_bad_model
from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.agents.models import match_models
from agent_bridge.config import Settings
from agent_bridge.engine import Engine

AGY = [
    ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
    ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
    ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
]


def ids(q):
    return [m for m, _ in match_models(q, AGY)]


def test_exact_label_and_id():
    assert ids("Gemini 4 Argon (High)") == ["gemini-4-argon-high"]
    assert ids("gemini-4-argon-high") == ["gemini-4-argon-high"]
    assert ids("GEMINI-3.8-FLASH-LOW") == ["gemini-3.8-flash-low"]


def test_partial_names():
    assert ids("Gemini 4 Argon") == ["gemini-4-argon-medium", "gemini-4-argon-high", "gemini-4-argon-low"]
    assert ids("argon high") == ["gemini-4-argon-high"]
    assert ids("flash") == ["gemini-3.8-flash-low"]
    assert ids("sonnet") == ["claude-sonnet-5-5-high"]
    assert ids("Gemini") == [m for m, _ in AGY if m.startswith("gemini")]
    assert ids("gpt") == [] and ids("") == []


def test_bad_model_detection():
    msg = ('invalid model selection (--model "Gemini" --effort ""): model Gemini is not recognized as a known model')
    assert looks_like_bad_model(msg)
    assert looks_like_bad_model("There's an issue with the selected model (foo)")
    assert not looks_like_bad_model("file not found")


def test_agy_invalid_model_is_config_error(tmp_path, monkeypatch):
    line = json.dumps({"event": "result", "result": {"conversation_id": "", "status": "ERROR", "response": "",
                                                      "error": 'invalid model selection (--model "Gemini")'}})
    script = tmp_path / "agy"
    script.write_text(f"#!{sys.executable}\nprint({json.dumps(line)})\nraise SystemExit(1)\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(ConfigError, match='model "Gemini"'):
        asyncio.run(AgyAdapter().send({"started": False, "model": "Gemini"}, "hi",
                                       TurnContext(tmp_path, "none", 30, "S"), print, print))


def test_config_error_is_not_retried():
    class BadModel(FakeAdapter):
        async def send(self, *a, **k):
            self.prompts.append("x")
            raise ConfigError("bad model")

    agy = BadModel("agy")
    eng = Engine.new(Settings(turns=4, retries=3), "t", {"claude": FakeAdapter("claude"), "agy": agy})
    eng.retry_backoff = 0
    assert asyncio.run(eng.run()) == "error"
    assert len(agy.prompts) == 1  # stopped immediately
    assert eng.state["turn"] == 1 and eng.state["next"] == "agy"  # fix the model, then /continue
