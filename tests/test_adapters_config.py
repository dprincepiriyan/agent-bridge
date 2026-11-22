import asyncio
import json
import sys
from pathlib import Path

from agent_bridge import config, store
from agent_bridge.agents.agy import AgyAdapter
from agent_bridge.agents.base import AgentError, TurnContext, stream_process
from agent_bridge.agents.claude import ClaudeAdapter

CTX = TurnContext(workdir=Path("."), perms="none", timeout=30, system_prompt="SYS")


def test_claude_cmd_new_vs_resume():
    a = ClaudeAdapter()
    cmd = a.build_cmd({"started": False, "session": "abc", "model": "opus", "effort": "high"}, "hi", CTX)
    assert cmd[cmd.index("--session-id") + 1] == "abc"
    assert "--resume" not in cmd and "-c" not in cmd and "--continue" not in cmd
    assert cmd[-2:] == ["-p", "hi"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "opus"
    cmd = a.build_cmd({"started": True, "session": "abc"}, "hi", CTX)
    assert cmd[cmd.index("--resume") + 1] == "abc"


def test_agy_cmd_uses_attached_print_and_conversation():
    a = AgyAdapter()
    cmd = a.build_cmd({"started": False, "session": None}, "-p tricky", CTX)
    assert cmd[-1] == "--print=-p tricky"
    assert "--conversation" not in cmd
    cmd = a.build_cmd({"started": True, "session": "conv1", "model": "m"}, "x", CTX)
    assert cmd[cmd.index("--conversation") + 1] == "conv1"
    assert "-c" not in cmd


def _fake_cli(tmp_path, name, lines, code=0):
    script = tmp_path / name
    body = "\n".join(f"print({json.dumps(l)}, flush=True)" for l in lines)
    script.write_text(f"#!{sys.executable}\nimport sys\n{body}\nsys.exit({code})\n")
    script.chmod(0o755)
    return script


def test_claude_stream_parsing(tmp_path, monkeypatch):
    lines = [json.dumps(x) for x in [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Edit"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"file_path\": \"WD/src/"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "app.py\"}"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Hello", "total_cost_usd": 0.01,
         "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]]
    lines = [l.replace("WD", str(tmp_path)) for l in lines]
    _fake_cli(tmp_path, "claude", lines)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    texts, tools = [], []
    agent = {"started": False, "session": "abc"}
    res = asyncio.run(ClaudeAdapter().send(agent, "hi", TurnContext(tmp_path, "none", 30, "S"), texts.append, tools.append))
    assert "".join(texts) == "Hello" and tools == ["Edit src/app.py"]
    assert res.text == "Hello" and res.cost == 0.01 and res.tokens == 7
    assert agent["started"] and agent["session"] == "abc"


def test_claude_error_rotates_unstarted_session(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "claude", [json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "bad"})], 1)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    agent = {"started": False, "session": "abc"}
    try:
        asyncio.run(ClaudeAdapter().send(agent, "hi", TurnContext(tmp_path, "none", 30, "S"), print, print))
        assert False
    except AgentError as e:
        assert "bad" in str(e)
    assert agent["session"] != "abc" and not agent["started"]


def test_agy_stream_parsing(tmp_path, monkeypatch):
    lines = [json.dumps(x) for x in [
        {"event": "init", "conversation_id": "c1"},
        {"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}},
        {"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "A"}},
        {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
                                                 "tool_info": {"name": "run_command", "parameters": {"CommandLine": "mvn -q test"}}}},
        {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command"}},
        {"event": "step_update", "step_update": {"step_index": 4, "state": "ACTIVE", "step_type": "system_message"}},
        {"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "B"}},
        {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "B\n", "usage": {"total_tokens": 9}}},
    ]]
    _fake_cli(tmp_path, "agy", lines)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    texts, tools = [], []
    agent = {"started": False, "session": None}
    res = asyncio.run(AgyAdapter().send(agent, "hi", TurnContext(tmp_path, "none", 30, "S"), texts.append, tools.append))
    assert "".join(texts) == "A\n\nB" and tools == ["Run mvn -q test"]
    assert res.text == "B" and agent["session"] == "c1" and agent["started"]


def test_stream_process_timeout_kills(tmp_path):
    async def go():
        try:
            await stream_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1, print)
        except AgentError as e:
            return str(e)
    assert "timed out" in asyncio.run(go())


def test_config_roundtrip_and_validation():
    s = config.Settings(turns=12, claude_model="opus", role_a='say "hi"\nthere')
    config.save(s)
    loaded, warn = config.load()
    assert warn == [] and loaded == s
    config.config_file().write_text('turns = -3\nperms = "yolo"\nbogus = 1\n')
    loaded, warn = config.load()
    assert loaded.turns == 8 and loaded.perms == "none" and len(warn) == 3


def test_config_corrupt_file_falls_back():
    config.config_file().parent.mkdir(parents=True, exist_ok=True)
    config.config_file().write_text("this is = = not toml")
    s, warn = config.load()
    assert s == config.Settings() and warn


def test_legacy_migration(tmp_path, monkeypatch):
    old = tmp_path / "oldrepo" / "runs"
    d = old / "20250101-000000"
    (d / "workspace").mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"run_dir": str(d), "workdir": str(d / "workspace")}))
    monkeypatch.setattr(store, "legacy_dirs", lambda: [old])
    assert store.migrate_legacy() == 1
    new = store.runs_dir() / d.name
    st = json.loads((new / "state.json").read_text())
    assert st["workdir"] == str(new / "workspace")
    assert not old.exists()


def test_agy_empty_reply_gets_nudged_in_same_conversation(tmp_path, monkeypatch):
    # 1st call: tool use, SUCCESS, empty response. 2nd call (nudge): real reply. Count calls via a file.
    first = [json.dumps({"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "tool",
                                                                 "tool_name": "read_file", "tool_info": {"parameters": {"Path": "x.py"}}}}),
             json.dumps({"event": "result", "result": {"conversation_id": "c9", "status": "SUCCESS", "response": ""}})]
    second = [json.dumps({"event": "result", "result": {"conversation_id": "c9", "status": "SUCCESS", "response": "Looks good."}})]
    script = tmp_path / "agy"
    script.write_text(f"""#!{sys.executable}
import pathlib, sys
c = pathlib.Path({str(tmp_path / 'count')!r})
n = int(c.read_text()) if c.exists() else 0
c.write_text(str(n + 1))
pathlib.Path({str(tmp_path / 'args')!r} + str(n)).write_text(" ".join(sys.argv[1:]))
for l in ({first!r} if n == 0 else {second!r}):
    print(l)
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    agent = {"started": False, "session": None}
    res = asyncio.run(AgyAdapter().send(agent, "review it", TurnContext(tmp_path, "edits", 30, "S"), print, print))
    assert res.text == "Looks good."
    assert "--conversation c9" in (tmp_path / "args1").read_text()  # nudge continued the same conversation
    assert "without writing a message" in (tmp_path / "args1").read_text()


def test_describe_tool_shortens_workdir_in_commands():
    from agent_bridge.agents.base import describe_tool
    wd = Path("/home/u/proj")
    assert describe_tool("Bash", {"command": "cd /home/u/proj && pytest -q"}, wd) == "Run cd . && pytest -q"
    assert describe_tool("run_command", {"CommandLine": "ls /home/u/proj/src"}, wd) == "Run ls src"
