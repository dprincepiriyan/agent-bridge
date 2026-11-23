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


def run(eng):
    return asyncio.run(eng.run())


def test_seeded_file_and_briefing_lines():
    eng, ad = new(Settings(turns=2, perms="edits", agy_role="orchestrator", claude_role="coder", first="agy"))
    run(eng)
    text = eng.progress_path.read_text()
    assert "## Goal\nBuild a ping pong game" in text and "## Open issues" in text
    assert "You keep PROGRESS.md" in ad["agy"].prompts[0]
    assert "kept by Antigravity" in ad["claude"].prompts[0]


def test_shown_every_n_turns_to_the_speaker():
    eng, ad = new(Settings(turns=7, perms="edits", progress_every=2))
    run(eng)
    shown = [p for a in ("claude", "agy") for p in ad[a].prompts if "current PROGRESS.md" in p]
    # seeded at turn 0 → shown on turns 3, 5 and 7 (every 2 turns)
    assert len(shown) == 3
    assert all("```markdown\n# Progress" in p for p in shown)


def test_resume_shows_it_immediately():
    eng, ad = new(Settings(turns=2, perms="edits", progress_every=50))
    run(eng)
    eng.extend(1)
    run(eng)
    assert "You're resuming after a pause" in ad["claude"].prompts[-1]


def test_keeper_nudged_when_missing_or_stale():
    def delete(n, ctx):  # agy's first turn (turn 2) deletes it; claude's turn 3 should be nudged
        if n == 1:
            (ctx.workdir / "PROGRESS.md").unlink()
    eng, ad = new(Settings(turns=4, perms="edits", first="claude"), agy=FakeAdapter("agy", on_send=delete))
    run(eng)
    assert "PROGRESS.md is missing" in ad["claude"].prompts[-1]

    eng, ad = new(Settings(turns=6, perms="edits", progress_every=1))
    run(eng)  # nobody edits the file → keeper (claude) gets a stale nudge
    assert any("hasn't changed in" in p for p in ad["claude"].prompts)
    assert not any("hasn't changed in" in p for p in ad["agy"].prompts)


def test_keeper_updates_reset_staleness():
    def update(n, ctx):
        (ctx.workdir / "PROGRESS.md").write_text(f"# Progress\n## Done\n- step {n}\n")
    eng, ad = new(Settings(turns=6, perms="edits", progress_every=1), claude=FakeAdapter("claude", on_send=update))
    run(eng)
    assert not any("hasn't changed in" in p for p in ad["claude"].prompts)
    assert "- step 3" in ad["agy"].prompts[-1]  # partner sees the keeper's latest version


def test_off_for_talk_only_and_when_disabled():
    eng, ad = new(Settings(turns=3, perms="none"))
    run(eng)
    assert not eng.progress_path.exists() and not any("PROGRESS.md" in p for p in ad["claude"].prompts)


def test_seed_does_not_disable_checkpoints_in_clean_user_repo(tmp_path):
    import subprocess
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    eng, _ = new(Settings(turns=1, perms="edits", workdir=str(repo)))
    run(eng)
    assert eng.state["checkpoint_ok"] is True
    log = subprocess.run(["git", "log", "--format=%s", "--name-only"], cwd=repo, capture_output=True, text=True).stdout
    assert "PROGRESS.md" in log  # committed with turn 1


def test_summary_includes_progress_for_builds():
    eng, ad = new(Settings(turns=2, perms="edits", preset="build-review"))
    run(eng)
    asyncio.run(eng.summarize("claude"))
    assert "<progress>" in ad["claude"].prompts[-1]
