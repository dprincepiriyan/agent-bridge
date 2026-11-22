"""Drive the interactive app with simulated keystrokes (fake agents, no quota)."""
import asyncio
import subprocess

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_bridge import config, store
from agent_bridge.config import Settings
from agent_bridge.tui import commands


async def wait_for(cond, timeout=10):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def run_script(monkeypatch, script):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE", "1")
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.01")

    async def go():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            from agent_bridge.tui.app import App
            app = App(Settings())
            app_task = asyncio.create_task(app.main())
            await asyncio.sleep(0.2)
            await script(app, inp)
            inp.send_text("/exit\r")
            await asyncio.wait_for(app_task, 10)
            return app

    return asyncio.run(go())


def test_settings_commands_and_a_run(monkeypatch):
    async def script(app, inp):
        inp.send_text("/turns 3\r")
        await wait_for(lambda: app.settings.turns == 3)
        inp.send_text("/model claude opus\r")
        await wait_for(lambda: app.settings.claude_model == "opus")
        inp.send_text("/effort agy high\r")
        await wait_for(lambda: app.settings.agy_effort == "high")
        inp.send_text("/mode debate\r")
        await wait_for(lambda: app.settings.preset == "debate")
        inp.send_text("/first agy\r")
        await wait_for(lambda: app.settings.first == "agy")
        inp.send_text("Is pineapple on pizza fine?\r")
        await wait_for(lambda: app.engine is not None and app.task is not None)
        await wait_for(lambda: not app.running())

    app = run_script(monkeypatch, script)
    st = app.engine.state
    assert st["turn"] == 3 and st["next"] == "claude"  # agy, claude, agy
    assert st["agents"]["claude"]["model"] == "opus"
    assert "YOUR ROLE: Argue FOR" in app.adapters["agy"].prompts[0]
    assert app.adapters["agy"].calls[0]["effort"] == "high"


def test_picker_selection_and_config_save(monkeypatch):
    async def script(app, inp):
        inp.send_text("/mode\r")
        await asyncio.sleep(0.3)
        inp.send_text("3")  # 3rd option in the picker = debate
        await wait_for(lambda: app.settings.preset == "debate")
        inp.send_text("/perms\r")
        await asyncio.sleep(0.3)
        inp.send_text("2")  # edits
        await wait_for(lambda: app.settings.perms == "edits")
        inp.send_text("/config save\r")
        await wait_for(lambda: config.config_file().exists())

    run_script(monkeypatch, script)
    saved, _ = config.load()
    assert saved.preset == "debate" and saved.perms == "edits"


def test_interject_interrupt_continue_and_resume(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE_DELAY", "0.15")

    async def script(app, inp):
        inp.send_text("/turns 6\r")
        inp.send_text("Plan a picnic\r")
        await wait_for(lambda: app.running())
        inp.send_text("keep it vegetarian\r")  # typed while running → queued for next agent
        await wait_for(lambda: any("keep it vegetarian" in p for p in app.adapters["agy"].prompts))
        inp.send_text("/stop\r")
        await wait_for(lambda: not app.running())
        turn = app.engine.state["turn"]
        assert turn < 6
        inp.send_text("/continue\r")
        await wait_for(lambda: not app.running() and app.engine.state["turn"] == 6, timeout=20)
        name = app.engine.run_dir.name
        inp.send_text("/new\r")
        await wait_for(lambda: app.engine is None)
        inp.send_text(f"/resume {name}\r")
        await wait_for(lambda: app.engine is not None)
        inp.send_text("now add dessert\r")  # continues the loaded conversation
        await wait_for(lambda: app.engine.state["turn"] > 6)
        await wait_for(lambda: not app.running(), timeout=20)

    app = run_script(monkeypatch, script)
    assert "now add dessert" in store.transcript_path(app.engine.state).read_text()


def test_completer_lists_commands_and_args(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE", "1")
    from prompt_toolkit.document import Document

    class Stub:
        model_cache = {"agy": [("gemini-4-argon-high", "Gemini 4 Argon (High)"),
                               ("gemini-4-neon-low", "Gemini 4 Neon (Low)")]}
        settings = Settings(agy_model="gemini-4-neon-low")
        fetched = []

        def prefetch_models(self, agent=None):
            self.fetched.append(agent)

    stub = Stub()
    comp = commands.SlashCompleter(stub)

    def names(text):
        return [c.text for c in comp.get_completions(Document(text), None)]

    assert "/model" in names("/mo") and "/mode" in names("/mo")
    assert names("/model ") == ["claude", "agy"]
    assert names("/model agy ") == ["agy default", "agy gemini-4-argon-high", "agy gemini-4-neon-low"]
    assert names("/model agy argon") == ["agy gemini-4-argon-high"]
    shown = {c.text: (c.display_text, c.display_meta_text) for c in comp.get_completions(Document("/model agy "), None)}
    assert shown["agy gemini-4-neon-low"][0] == "Gemini 4 Neon (Low)  ✓"
    assert names("/model claude ")[0].startswith("claude ")  # not loaded yet → loading hint + prefetch
    assert stub.fetched == ["claude"]

    assert names("/perms a") == ["auto"]
    assert names("hello") == []


def test_unknown_command_does_not_crash(monkeypatch):
    async def script(app, inp):
        inp.send_text("/frobnicate\r")
        inp.send_text("/turns abc\r")
        await asyncio.sleep(0.3)

    app = run_script(monkeypatch, script)
    assert app.settings.turns == 8


def test_v03_commands(monkeypatch):
    async def script(app, inp):
        inp.send_text("/verify true\r")
        await wait_for(lambda: app.settings.verify == "true")
        inp.send_text("/judge agy\r")
        await wait_for(lambda: app.settings.judge == "agy")
        inp.send_text("/judge auto off\r")
        await wait_for(lambda: app.settings.auto_verdict is False)
        inp.send_text("/checkpoints off\r")
        await wait_for(lambda: app.settings.checkpoints is False)
        inp.send_text("/checkpoints on\r")
        await wait_for(lambda: app.settings.checkpoints is True)
        inp.send_text("/turns 2\r")
        inp.send_text("/mode debate\r")
        await wait_for(lambda: app.settings.preset == "debate")
        inp.send_text("Cats or dogs?\r")
        await wait_for(lambda: app.engine is not None)
        await wait_for(lambda: not app.running())
        assert not (app.engine.run_dir / "summary.md").exists()  # auto verdict was turned off
        inp.send_text("/verify run\r")
        await wait_for(lambda: app.engine.state.get("last_verify") is not None)
        await wait_for(lambda: not app.running())
        inp.send_text("/summary\r")
        await wait_for(lambda: (app.engine.run_dir / "summary.md").exists())
        await wait_for(lambda: not app.running())
        inp.send_text("/undo\r")  # nothing to undo in a talk-only debate → warning, no crash
        inp.send_text("/status\r")
        await asyncio.sleep(0.3)

    app = run_script(monkeypatch, script)
    assert app.engine.state["last_verify"]["ok"] is True
    assert "(by Antigravity)" in (app.engine.run_dir / "summary.md").read_text()


def test_model_name_with_spaces(monkeypatch):
    async def script(app, inp):
        app.model_cache["agy"] = [("gemini-4-argon-high", "Gemini 4 Argon (High)"),
                                  ("gemini-4-argon-low", "Gemini 4 Argon (Low)")]
        inp.send_text("/model agy Gemini 4 Argon (High)\r")
        await wait_for(lambda: app.settings.agy_model == "gemini-4-argon-high")
        inp.send_text("/model agy Gemini 4 Argon\r")  # ambiguous → picker; pick #2
        await asyncio.sleep(0.4)
        inp.send_text("2")
        await wait_for(lambda: app.settings.agy_model == "gemini-4-argon-low")
        inp.send_text("/model agy Nonexistent Model\r")  # rejected, setting unchanged
        await asyncio.sleep(0.3)
        inp.send_text("/model claude claude-opus-5-5\r")  # claude accepts unlisted IDs
        await wait_for(lambda: app.settings.claude_model == "claude-opus-5-5")

    app = run_script(monkeypatch, script)
    assert app.settings.agy_model == "gemini-4-argon-low"


def _make_runs():
    from agent_bridge.agents.fake import FakeAdapter
    from agent_bridge.engine import Engine
    ad = {"claude": FakeAdapter("claude"), "agy": FakeAdapter("agy")}
    names = []
    for topic, preset in (("Tabs vs spaces forever", "debate"), ("Build a pizza ordering CLI", "build-review"),
                          ("Plan a pizza party", "plan")):
        eng = Engine.new(Settings(turns=2, preset=preset), topic, ad)
        asyncio.run(eng.run())
        names.append(eng.run_dir.name)
    return names


def test_resume_completion_shows_topics(monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE", "1")
    from prompt_toolkit.document import Document
    _make_runs()
    comp = commands.SlashCompleter(object())

    def comps(text):
        return list(comp.get_completions(Document(text), None))

    all_runs = comps("/resume ")
    assert len(all_runs) == 3
    metas = [c.display_meta_text for c in all_runs]
    assert any("Tabs vs spaces forever" in m and "debate" in m for m in metas)
    assert all(c.display_text.startswith("Today ") for c in all_runs)
    pizza = comps("/resume pizza")
    assert len(pizza) == 2
    assert len(comps("/resume pizza party")) == 1
    assert comps("/resume pizza party")[0].start_position == -len("pizza party")


def test_resume_by_search_words(monkeypatch):
    _make_runs()

    async def script(app, inp):
        inp.send_text("/resume tabs\r")  # single match → loads directly
        await wait_for(lambda: app.engine is not None)
        assert app.engine.state["topic"] == "Tabs vs spaces forever"
        inp.send_text("/new\r")
        await wait_for(lambda: app.engine is None)
        inp.send_text("/resume pizza\r")  # two matches → picker
        await asyncio.sleep(0.4)
        inp.send_text("1")
        await wait_for(lambda: app.engine is not None)

    app = run_script(monkeypatch, script)
    assert "pizza" in app.engine.state["topic"].lower()


def test_agy_models_cache_is_instant_and_refreshes(monkeypatch, tmp_path):
    import json, time
    from agent_bridge import config
    from agent_bridge.agents.agy import AgyAdapter
    cache = config.cache_dir() / "agy_models.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"at": time.time() - 9 * 86400, "models": [["old-model", "Old Model"]]}))
    ad = AgyAdapter()
    assert ad.cached_models() == ([("old-model", "Old Model")], False)  # stale but usable instantly
    script = tmp_path / "agy"
    script.write_text("#!/bin/sh\nprintf 'new-model\\tNew Model\\n'\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    assert asyncio.run(ad.list_models()) == [("new-model", "New Model")]
    assert ad.cached_models() == ([("new-model", "New Model")], True)


def test_init_wizard_writes_spec_and_starts(monkeypatch, tmp_path):
    proj = tmp_path / "proj"

    async def script(app, inp):
        inp.send_text("/init\r")
        await asyncio.sleep(0.3)
        inp.send_text("a tiny calculator\r")
        await asyncio.sleep(0.2)
        inp.send_text("Python\r")
        await asyncio.sleep(0.2)
        inp.send_text("add, subtract\r")
        await asyncio.sleep(0.2)
        inp.send_text("\r")  # accept default 'done when'
        await asyncio.sleep(0.3)
        inp.send_text("\r")  # first suggestion: python -m pytest -q
        await asyncio.sleep(0.3)
        inp.send_text("\x15" + str(proj) + "\r")  # ctrl-u clears the default folder
        await asyncio.sleep(0.3)
        inp.send_text("2")  # perms: edits
        await asyncio.sleep(0.3)
        inp.send_text("1")  # start now: yes
        await wait_for(lambda: app.engine is not None)
        await wait_for(lambda: not app.running(), timeout=20)

    app = run_script(monkeypatch, script)
    spec = (proj / "SPEC.md").read_text()
    assert "a tiny calculator" in spec and "- add\n- subtract" in spec
    assert app.settings.verify == "python -m pytest -q" and app.settings.perms == "edits"
    assert app.engine.state["workdir"] == str(proj.resolve())
    log = subprocess.run(["git", "log", "--format=%s"], cwd=proj, capture_output=True, text=True).stdout
    assert "Initial commit with SPEC.md" in log
    assert "SPEC.md" in app.engine.state["topic"]


def test_diff_and_notify_commands(monkeypatch):
    sent = []
    monkeypatch.setattr("agent_bridge.notify.send", lambda *a: sent.append(a) or True)

    async def script(app, inp):
        def writer(n, ctx):
            (ctx.workdir / f"file{n}.txt").write_text("x\n" * n)
        app.adapters["claude"].on_send = writer
        inp.send_text("/perms edits\r")
        await wait_for(lambda: app.settings.perms == "edits")
        inp.send_text("/turns 2\r")
        inp.send_text("make files\r")
        await wait_for(lambda: app.engine is not None)
        await wait_for(lambda: not app.running())
        inp.send_text("/diff\r")
        inp.send_text("/diff 1\r")
        inp.send_text("/diff all\r")
        inp.send_text("/diff 99\r")
        inp.send_text("/notify off\r")
        await wait_for(lambda: app.settings.notify is False)
        inp.send_text("/notify test\r")
        await asyncio.sleep(0.4)

    app = run_script(monkeypatch, script)
    assert len(app.engine.state["checkpoint_log"]) == 1
    assert sent and sent[-1][1] == "Notifications work 🎉"
