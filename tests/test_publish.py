"""/publish against local bare repos standing in for GitHub (no network, no real account)."""
import asyncio
import subprocess

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_bridge.config import Settings


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def make_project(tmp_path, name="ping-pong"):
    wd = tmp_path / name
    wd.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=wd)
    (wd / "game.py").write_text("print('pong')\n")
    sh("git", "add", "-A", cwd=wd)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "turn 1", cwd=wd)
    return wd


def fake_gh(tmp_path, logged_in=True):
    remotes = tmp_path / "remotes"
    remotes.mkdir(exist_ok=True)
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(exist_ok=True)
    gh.write_text(f"""#!/bin/sh
echo "$@" >> {tmp_path}/gh_calls
case "$1 $2" in
  "auth status") exit {0 if logged_in else 1} ;;
  "api user") echo tester ;;
  "repo create")
    git init -q --bare -b main {remotes}/$3.git
    git remote add origin {remotes}/$3.git
    git push -q -u origin HEAD 2>&1 ;;
esac
""")
    gh.chmod(0o755)
    return remotes


def drive(monkeypatch, tmp_path, script, settings):
    monkeypatch.setenv("AGENT_BRIDGE_FAKE", "1")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")

    async def go():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            from agent_bridge.tui.app import App
            app = App(settings)
            t = asyncio.create_task(app.main())
            await asyncio.sleep(0.2)
            await script(app, inp)
            inp.send_text("/exit\r")
            await asyncio.wait_for(t, 10)
            return app
    return asyncio.run(go())


async def answer(inp, *keys, pause=0.35):
    for k in keys:
        inp.send_text(k)
        await asyncio.sleep(pause)


def test_publish_creates_private_repo_with_gh(monkeypatch, tmp_path):
    wd = make_project(tmp_path)
    remotes = fake_gh(tmp_path, logged_in=True)

    async def script(app, inp):
        # /publish → name (accept default) → visibility (enter = private) → confirm (enter = yes)
        await answer(inp, "/publish\r", "\r", "\r", "\r", pause=0.6)

    drive(monkeypatch, tmp_path, script, Settings(workdir=str(wd)))
    calls = (tmp_path / "gh_calls").read_text()
    assert "repo create ping-pong --private --source . --remote origin --push" in calls
    assert sh("git", "log", "--format=%s", cwd=remotes / "ping-pong.git").strip() == "turn 1"


def test_publish_existing_remote_pushes_then_up_to_date(monkeypatch, tmp_path):
    wd = make_project(tmp_path)
    bare = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    sh("git", "remote", "add", "origin", str(bare), cwd=wd)
    sh("git", "push", "-q", "-u", "origin", "main", cwd=wd)
    (wd / "game.py").write_text("print('pong v2')\n")  # leftover, uncommitted
    fake_gh(tmp_path)

    async def script(app, inp):
        await answer(inp, "/publish\r", "\r", "\r", pause=0.6)   # commit leftovers? yes · push? yes
        await answer(inp, "/publish\r", pause=0.6)               # nothing new → up to date

    drive(monkeypatch, tmp_path, script, Settings(workdir=str(wd)))
    log = sh("git", "log", "--format=%s", cwd=bare).splitlines()
    assert log[0] == "agent-bridge: changes before publishing"
    assert not (tmp_path / "gh_calls").exists() or "repo create" not in (tmp_path / "gh_calls").read_text()


def test_publish_without_gh_login_uses_pasted_url(monkeypatch, tmp_path):
    wd = make_project(tmp_path)
    fake_gh(tmp_path, logged_in=False)
    remotes = tmp_path / "remotes"
    sh("git", "init", "-q", "--bare", "-b", "main", str(remotes / "me" / "ping-pong.git"), cwd=tmp_path)
    # route git@github.com: to the local stand-in, for this repo only
    sh("git", "config", f"url.{remotes}/.insteadOf", "git@github.com:", cwd=wd)

    async def script(app, inp):
        await answer(inp, "/publish\r", "\r", "\r",                                     # name, private
                     "https://github.com/me/ping-pong\r", "\r", pause=0.6)           # url (https→ssh), confirm

    drive(monkeypatch, tmp_path, script, Settings(workdir=str(wd)))
    assert sh("git", "config", "--get", "remote.origin.url", cwd=wd).strip() == "git@github.com:me/ping-pong.git"
    assert sh("git", "log", "--format=%s", cwd=remotes / "me" / "ping-pong.git").strip() == "turn 1"


def test_publish_never_force_pushes(monkeypatch, tmp_path):
    wd = make_project(tmp_path)
    bare = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    other = tmp_path / "other"
    sh("git", "clone", "-q", str(bare), str(other), cwd=tmp_path)
    (other / "README.md").write_text("made on github\n")
    sh("git", "add", "-A", cwd=other)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "remote readme", cwd=other)
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=other)
    sh("git", "remote", "add", "origin", str(bare), cwd=wd)
    fake_gh(tmp_path)
    out = []

    async def script(app, inp):
        app.warn = lambda m: out.append(m)
        await answer(inp, "/publish\r", "\r", pause=0.6)

    drive(monkeypatch, tmp_path, script, Settings(workdir=str(wd)))
    assert any("never force-push" in m for m in out)
    assert sh("git", "log", "--format=%s", cwd=bare).strip() == "remote readme"  # untouched


def test_publish_refuses_non_repo_and_bad_names(monkeypatch, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = []

    async def script(app, inp):
        app.warn = lambda m: out.append(m)
        await answer(inp, f"/publish {plain}\r", pause=0.5)

    drive(monkeypatch, tmp_path, script, Settings())
    assert any("isn't a git repository" in m for m in out)
    from agent_bridge.github import valid_repo_name, parse_github_url
    assert not valid_repo_name("ping pong") and valid_repo_name("ping-pong_2.0")
    assert parse_github_url("https://gitlab.com/a/b") is None
