"""Drive the interactive app with simulated keystrokes (fake agents, no quota)."""
import asyncio

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
        model_cache = {"agy": [("gemini-x", "Gemini X")]}

    comp = commands.SlashCompleter(Stub())

    def names(text):
        return [c.text for c in comp.get_completions(Document(text), None)]

    assert "/model" in names("/mo") and "/mode" in names("/mo")
    assert names("/model ") == ["claude", "agy"]
    assert "gemini-x" in names("/model agy ")
    assert names("/perms a") == ["auto"]
    assert names("hello") == []


def test_unknown_command_does_not_crash(monkeypatch):
    async def script(app, inp):
        inp.send_text("/frobnicate\r")
        inp.send_text("/turns abc\r")
        await asyncio.sleep(0.3)

    app = run_script(monkeypatch, script)
    assert app.settings.turns == 8
