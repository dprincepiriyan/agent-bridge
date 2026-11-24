from agent_bridge import config
from agent_bridge.config import Settings
from agent_bridge.engine import role_for
from agent_bridge.roles import BUILTIN_ROLES, ROLES, USER_ROLES, load_user_roles, parse_role_file, roles_dir


def write(name, text):
    d = roles_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def test_parse_formats():
    r = parse_role_file("game-dev", "---\nname: Game developer\nsummary: Pygame, 60 fps\n---\nYou build games.\n")
    assert (r.name, r.summary, r.text) == ("Game developer", "Pygame, 60 fps", "You build games.")
    r = parse_role_file("ux-writer", "# UX writer\n\nWrite microcopy. Keep it short.")
    assert r.name == "UX writer" and r.summary == "Write microcopy." and r.text == "Write microcopy. Keep it short."
    r = parse_role_file("data-sci", "Analyse data with pandas.")
    assert r.name == "Data Sci"
    assert parse_role_file("empty", "---\nname: X\n---\n") is None


def test_load_reload_and_validation():
    write("game-dev.md", "---\nname: Game developer\nsummary: Pygame\n---\nBuild games with Pygame at 60 fps.")
    write("Bad Name.md", "x")
    write("empty.md", "# Nothing\n")
    warn = load_user_roles()
    assert "game-dev" in ROLES and USER_ROLES == {"game-dev"}
    assert any("Bad Name.md skipped" in w for w in warn) and any("empty.md has no instructions" in w for w in warn)
    s = Settings(claude_role="game-dev")
    assert s.validate() == [] and role_for(s, "claude")[1].startswith("Build games")
    (roles_dir() / "game-dev.md").unlink()
    load_user_roles()
    assert "game-dev" not in ROLES and ROLES.keys() == BUILTIN_ROLES.keys()


def test_override_builtin_and_restore():
    write("coder.md", "Write only Rust.")
    warn = load_user_roles()
    assert ROLES["coder"].text == "Write only Rust." and any("replaces the built-in" in w for w in warn)
    (roles_dir() / "coder.md").unlink()
    load_user_roles()
    assert ROLES["coder"] is BUILTIN_ROLES["coder"]


def test_config_load_knows_user_roles():
    write("game-dev.md", "Build games.")
    config.config_file().parent.mkdir(parents=True, exist_ok=True)
    config.config_file().write_text('claude_role = "game-dev"\n')
    s, warn = config.load()
    assert s.claude_role == "game-dev" and warn == []


def test_cli_accepts_user_roles(monkeypatch):
    write("game-dev.md", "Build games.")
    import agent_bridge.cli as cli
    seen = {}
    monkeypatch.setattr(cli, "run_headless", lambda s, args: seen.update(s=s) or 0)
    assert cli.main(["make pong", "--claude-role", "game-dev"]) == 0
    assert seen["s"].claude_role == "game-dev" and seen["s"].agy_role == ""  # no partner mapping for unknown roles
