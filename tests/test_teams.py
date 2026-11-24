import tomllib

from agent_bridge import teams
from agent_bridge.config import Settings


def test_save_load_roundtrip_excludes_project_specific():
    s = Settings(preset="orchestrated", first="agy", claude_model="opus", agy_model="gemini-4-argon-high",
                 agy_effort="high", perms="auto", turns=20, verify="mvn -q test", progress_every=2,
                 workdir="/somewhere/else", timeout=99, claude_role="custom", claude_role_text="Write Java 21.")
    path = teams.save_team("my-java-team", s)
    raw = tomllib.loads(path.read_text())
    assert "workdir" not in raw and "timeout" not in raw
    assert path.read_text().startswith("# agent-bridge team 'my-java-team':")
    t = Settings()
    assert teams.apply_team(t, teams.load_team("my-java-team")) == []
    for f in teams.TEAM_FIELDS:
        assert getattr(t, f) == getattr(s, f), f
    assert t.workdir == "" and t.timeout == Settings().timeout


def test_names_list_delete_and_describe():
    assert teams.valid_name("my-java-team") and not teams.valid_name("debate") and not teams.valid_name("My Team")
    teams.save_team("b-team", Settings())
    teams.save_team("a-team", Settings(claude_role="architect"))
    assert teams.list_teams() == ["a-team", "b-team"]
    assert "Claude: Architect (default)" in teams.describe_saved("a-team")
    assert teams.delete_team("a-team") and teams.list_teams() == ["b-team"]


def test_bad_values_in_team_file_are_ignored():
    teams.teams_dir().mkdir(parents=True, exist_ok=True)
    (teams.teams_dir() / "weird.toml").write_text('turns = "lots"\nperms = "yolo"\nclaude_model = "opus"\n')
    s = Settings()
    warn = teams.apply_team(s, teams.load_team("weird"))
    assert s.turns == 8 and s.perms == "none" and s.claude_model == "opus" and len(warn) == 2


def test_cli_team_flag(monkeypatch):
    import agent_bridge.cli as cli
    teams.save_team("pong-crew", Settings(agy_role="orchestrator", claude_role="coder", first="agy", turns=24))
    seen = {}
    monkeypatch.setattr(cli, "run_headless", lambda s, args: seen.update(s=s) or 0)
    assert cli.main(["make pong", "--team", "pong-crew", "-n", "5"]) == 0
    s = seen["s"]
    assert s.agy_role == "orchestrator" and s.first == "agy" and s.turns == 5  # flags win over the team
    assert cli.main(["x", "--team", "nope"]) == 2
