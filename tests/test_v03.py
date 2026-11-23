"""Verification, git checkpoints, idle timeout, usage limits, verdicts."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_bridge import store
from agent_bridge.agents.base import AgentError, TurnContext, UsageLimitError, looks_like_limit, stream_process
from agent_bridge.agents.claude import ClaudeAdapter
from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.config import Settings
from agent_bridge.engine import Engine

DONE = "[[DONE]]"


def make(settings, claude=None, agy=None, topic="task"):
    events = []
    ad = {"claude": claude or FakeAdapter("claude"), "agy": agy or FakeAdapter("agy")}
    eng = Engine.new(settings, topic, ad, events.append)
    eng.retry_backoff = 0
    return eng, ad, events


def git_log(wd):
    return subprocess.run(["git", "log", "--format=%s"], cwd=wd, capture_output=True, text=True).stdout.splitlines()


# ------------------------------------------------------------------ verification
def test_verify_pass_finishes():
    eng, _, ev = make(Settings(turns=6, verify="true"),
                      claude=FakeAdapter("claude", [f"done\n{DONE}"]), agy=FakeAdapter("agy", [f"ok\n{DONE}"]))
    assert asyncio.run(eng.run()) == "agreed"
    assert [e.data["ok"] for e in ev if e.kind == "verify_end"] == [True]


def test_verify_failure_feeds_back_then_passes():
    def make_file(n, ctx):  # claude "fixes" the problem on its 2nd turn
        if n == 2:
            (ctx.workdir / "fixed.txt").write_text("ok")

    eng, ad, ev = make(Settings(turns=10, verify="test -f fixed.txt", perms="edits"),
                       claude=FakeAdapter("claude", [f"done\n{DONE}", f"fixed\n{DONE}"], on_send=make_file),
                       agy=FakeAdapter("agy", [f"agree\n{DONE}", f"agree again\n{DONE}"]))
    assert asyncio.run(eng.run()) == "agreed"
    results = [e.data["ok"] for e in ev if e.kind == "verify_end"]
    assert results == [False, True]
    # the failure output went to the next agent as a bridge note
    assert "verification command `test -f fixed.txt` FAILED" in ad["claude"].prompts[1]
    assert "Verification `test -f fixed.txt` → FAILED" in store.transcript_path(eng.state).read_text()


def test_verify_failed_with_no_turns_left():
    eng, _, _ = make(Settings(turns=2, verify="false"),
                     claude=FakeAdapter("claude", [f"x\n{DONE}"]), agy=FakeAdapter("agy", [f"y\n{DONE}"]))
    assert asyncio.run(eng.run()) == "verify_failed"
    assert not eng.state["finished"]


def test_verify_in_briefing():
    eng, ad, _ = make(Settings(turns=1, verify="pytest -q"))
    asyncio.run(eng.run())
    assert "the bridge runs `pytest -q`" in ad["claude"].prompts[0]


# ------------------------------------------------------------------- checkpoints
def write_on_turn(name):
    def f(n, ctx):
        (ctx.workdir / f"{name}{n}.txt").write_text(f"{name} {n}")
    return f


def test_checkpoints_in_own_workspace_and_undo():
    eng, ad, ev = make(Settings(turns=3, perms="edits"),
                       claude=FakeAdapter("claude", on_send=write_on_turn("c")),
                       agy=FakeAdapter("agy", on_send=write_on_turn("a")))
    asyncio.run(eng.run())
    wd = eng.workdir
    log = git_log(wd)
    assert log[0].startswith("agent-bridge: turn 3 · Claude")
    assert len(eng.state["checkpoint_log"]) == 3
    assert sum(e.kind == "checkpoint" for e in ev) == 3
    ok, msg = asyncio.run(eng.undo())
    assert ok and "turn 3" in msg
    assert not (wd / "c2.txt").exists() and (wd / "a1.txt").exists()
    assert "reverted the file changes from turn 3" in eng.state["pending_notes"][0]["text"]


def test_no_checkpoints_when_perms_none():
    eng, _, _ = make(Settings(turns=2))
    asyncio.run(eng.run())
    assert eng.state["checkpoint_log"] == []
    assert not (eng.workdir / ".git").exists()


def test_dirty_user_repo_disables_checkpoints(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "mine.txt").write_text("user work in progress")
    eng, _, ev = make(Settings(turns=2, perms="edits", workdir=str(repo)),
                      claude=FakeAdapter("claude", on_send=write_on_turn("c")))
    asyncio.run(eng.run())
    assert eng.state["checkpoint_ok"] is False
    assert any("uncommitted changes" in e.text for e in ev if e.kind == "notice")
    assert git_log(repo) == []  # nothing committed on the user's behalf


def test_clean_user_repo_gets_checkpoints(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    eng, _, _ = make(Settings(turns=2, perms="edits", workdir=str(repo)),
                     claude=FakeAdapter("claude", on_send=write_on_turn("c")))
    asyncio.run(eng.run())
    assert git_log(repo) == ["agent-bridge: turn 1 · Claude"]


def test_checkpoints_off(tmp_path):
    eng, _, _ = make(Settings(turns=2, perms="edits", checkpoints=False),
                     claude=FakeAdapter("claude", on_send=write_on_turn("c")))
    asyncio.run(eng.run())
    assert eng.state["checkpoint_log"] == []


# ------------------------------------------------------------------ timeouts
def test_idle_timeout_kills_silent_process(tmp_path):
    async def go():
        with pytest.raises(AgentError, match="no output for 1s"):
            await stream_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 60, print, 1)
    asyncio.run(go())


def test_idle_timeout_resets_on_output(tmp_path):
    code = "import time\nfor i in range(4):\n    print(i, flush=True); time.sleep(0.6)"
    lines = []
    rc, _ = asyncio.run(stream_process([sys.executable, "-c", code], tmp_path, 60, lines.append, 1))
    assert rc == 0 and lines == ["0", "1", "2", "3"]


# ------------------------------------------------------------------ usage limits
def test_usage_limit_stops_without_retry():
    eng, ad, ev = make(Settings(turns=6, retries=3), agy=FakeAdapter("agy", limit_after=0))
    assert asyncio.run(eng.run()) == "usage_limit"
    assert len(ad["agy"].prompts) == 1  # no retries
    done = ev[-1].data
    assert done["agent"] == "agy" and done["resets_at"] == 1795293600.0
    assert eng.state["turn"] == 1 and eng.state["next"] == "agy"  # resumable at the right place


def test_claude_rejected_rate_limit_event(tmp_path, monkeypatch):
    lines = [json.dumps(x) for x in [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1795293600,
                                                         "unifiedWindows": {"five_hour": {"utilization": 1.0}}}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your limit"},
    ]]
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + "\n".join(f"print({json.dumps(l)})" for l in lines) + "\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    ad = ClaudeAdapter()
    with pytest.raises(UsageLimitError) as ei:
        asyncio.run(ad.send({"started": False, "session": "s"}, "hi", TurnContext(tmp_path, "none", 30, "S"),
                            lambda t: None, lambda t: None))
    assert ei.value.resets_at == 1795293600
    assert ad.limits["five_hour"] == 1.0


def test_limit_detection_text():
    assert looks_like_limit("Claude AI usage limit reached|1795293600")
    assert looks_like_limit("RESOURCE_EXHAUSTED: quota exceeded")
    assert not looks_like_limit("file not found")


# ------------------------------------------------------------------ verdicts
def test_debate_gets_automatic_verdict():
    judge_reply = "## Verdict\nClaude wins on evidence."
    claude = FakeAdapter("claude", ["for", "for again", judge_reply])
    eng, ad, ev = make(Settings(turns=4, preset="debate"), claude=claude)
    asyncio.run(eng.run())
    kinds = [e.kind for e in ev]
    assert "summary_start" in kinds and "summary_end" in kinds
    assert ad["claude"].calls[-1]["ephemeral"] and ad["claude"].calls[-1]["perms"] == "none"
    assert "impartial judge" in ad["claude"].prompts[-1] and "<transcript>" in ad["claude"].prompts[-1]
    assert (eng.run_dir / "summary.md").read_text().startswith("# Verdict (by Claude)")
    assert "⚖ Verdict" in store.transcript_path(eng.state).read_text()


def test_no_auto_verdict_outside_debate_or_when_off():
    eng, _, ev = make(Settings(turns=2, preset="plan"))
    asyncio.run(eng.run())
    assert "summary_start" not in [e.kind for e in ev]
    eng, _, ev = make(Settings(turns=2, preset="debate", auto_verdict=False))
    asyncio.run(eng.run())
    assert "summary_start" not in [e.kind for e in ev]


def test_manual_summary_with_other_judge():
    eng, ad, _ = make(Settings(turns=2, preset="build-review"))
    asyncio.run(eng.run())
    text = asyncio.run(eng.summarize("agy"))
    assert text and "## Outcome" in ad["agy"].prompts[-1]
    assert (eng.run_dir / "summary.md").read_text().startswith("# Summary (by Antigravity)")
