import asyncio

from agent_bridge.agents.fake import FakeAdapter
from agent_bridge.cli import apply_args, build_parser
from agent_bridge.config import Settings
from agent_bridge.engine import Engine, resolve_roles, role_for
from agent_bridge.roles import ROLES, TEAMS


def test_mode_decides_roles_by_speaking_order():
    s = Settings(preset="orchestrated", first="agy")
    assert role_for(s, "agy")[0] == "orchestrator" and role_for(s, "claude")[0] == "coder"
    s.first = "claude"
    assert role_for(s, "claude")[0] == "orchestrator"


def test_explicit_role_overrides_mode_and_custom_text():
    s = Settings(preset="build-review", claude_role="architect")
    assert role_for(s, "claude") == ("architect", ROLES["architect"].text)
    assert role_for(s, "agy")[0] == "reviewer"  # still from the mode
    s.agy_role, s.agy_role_text = "custom", "Only write haiku."
    assert role_for(s, "agy") == ("custom", "Only write haiku.")
    s.agy_role_text = ""  # custom without text falls back to the mode
    assert role_for(s, "agy")[0] == "reviewer"


def test_legacy_role_a_b_still_work():
    s = Settings(preset="custom", role_a="Do A", role_b="Do B")
    assert resolve_roles(s) == ("Do A", "Do B")


def test_every_team_uses_known_roles():
    for t in TEAMS.values():
        assert t.first in ROLES and t.second in ROLES


def test_briefings_include_both_roles():
    ad = {"claude": FakeAdapter("claude"), "agy": FakeAdapter("agy")}
    eng = Engine.new(Settings(turns=2, claude_role="coder", agy_role="orchestrator", first="agy"), "t", ad)
    asyncio.run(eng.run())
    p = ad["agy"].prompts[0]
    assert "YOUR ROLE — Orchestrator:" in p and "YOUR PARTNER'S ROLE (Claude — Coder)" in p
    assert "Orchestrator" in (eng.run_dir / "transcript.md").read_text()


def test_role_change_mid_conversation_notifies_both_once():
    ad = {"claude": FakeAdapter("claude"), "agy": FakeAdapter("agy")}
    eng = Engine.new(Settings(turns=2), "t", ad)
    asyncio.run(eng.run())
    eng.set_role("claude", "tester", ROLES["tester"].text)
    eng.extend(3)
    asyncio.run(eng.run())
    assert "changed your role from Coder to Tester / QA" in ad["claude"].prompts[1]
    assert "changed Claude's role from Coder to Tester / QA" in ad["agy"].prompts[1]
    assert "changed your role" not in ad["claude"].prompts[2]  # delivered once
    assert eng.state["agents"]["claude"]["role_key"] == "tester"
    assert eng.state["pending_notes"] == []


def test_old_string_notes_still_delivered():
    ad = {"claude": FakeAdapter("claude"), "agy": FakeAdapter("agy")}
    eng = Engine.new(Settings(turns=1), "t", ad)
    eng.state["pending_notes"] = ["legacy note"]
    asyncio.run(eng.run())
    assert "legacy note" in ad["claude"].prompts[0] and eng.state["pending_notes"] == []


def test_cli_role_flags_and_lead_opens():
    args = build_parser().parse_args(["x", "--agy-role", "orchestrator", "--claude-role", "coder"])
    s = Settings()
    apply_args(s, args)
    assert s.agy_role == "orchestrator" and s.first == "agy"
    args = build_parser().parse_args(["x", "--agy-role", "orchestrator", "-f", "claude"])
    s = Settings()
    apply_args(s, args)
    assert s.first == "claude"  # explicit --first wins


def test_single_role_gets_a_fitting_partner():
    args = build_parser().parse_args(["x", "--agy-role", "orchestrator"])
    s = Settings()
    apply_args(s, args)
    assert s.claude_role == "coder"
    args = build_parser().parse_args(["x", "--claude-role", "advocate"])
    s = Settings()
    apply_args(s, args)
    assert s.agy_role == "critic"
