# agent-bridge

Let **Claude Code** (`claude`) and **Antigravity** (`agy`) talk to each other through their CLIs: debate a question, plan a design, or build and review a project together, taking turns until both agree it's done.

Run `agent-bridge` for an interactive app in the style of Claude Code, with `/` commands, model pickers, live streaming and a status bar. You can also pass a topic for a one-shot run from scripts.

```
 you ──topic──► agent-bridge
                  ├─► claude  (session pinned by ID) ──reply──┐
                  ◄────────────────────────────────────────────┘
                  │  relays it as "Claude said: …"
                  ├─► agy     (conversation pinned by ID) ──reply──┐
                  ◄─────────────────────────────────────────────────┘
                  └─ repeats until both say [[DONE]] or the turn limit is hit
```

## Install

Requirements: Python 3.11+, and the [Claude Code](https://claude.com/claude-code) and Antigravity CLIs, both logged in.

```bash
git clone git@github.com:dprincepiriyan/agent-bridge.git ~/agent-bridge
~/agent-bridge/install.sh
```

The installer uses `uv` if you have it. Otherwise it creates a private virtual environment in `~/.local/share/agent-bridge/venv`. Either way it puts `agent-bridge` in `~/.local/bin`. The install is editable, so after a `git pull` the command uses the new code immediately.

With a Claude Pro/Max subscription and no `ANTHROPIC_API_KEY` set, Claude turns count against your subscription limits and aren't billed per token. The `~$` figures are API-price estimates, not charges.

## Interactive app

```
$ agent-bridge
╭─ agent-bridge v0.2.0 ──────────────────────────────────────────────╮
│ Claude: default   Antigravity: gemini-3.8-flash-low                │
│ Mode: debate · Turns: 8 · Perms: none · Dir: per-run workspace     │
╰────────────────────────────────────────────────────────────────────╯
❯ Is Go or Rust better for a first CLI tool?
▶ Turn 1/8 · Claude ──────────────────────────────────────────────────
Antigravity, I'll take the first position: Go is the better choice…
 claude:default │ agy:gemini-3.8-flash-low │ debate │ turn 0/8 │ ⠦ Claude working 3s
```

- **Type a topic** to start a conversation.
- **Type while they work** to steer. Your message goes to the agent that replies next, marked "The user says:".
- **Esc** or **Ctrl+C** interrupts the current turn. Progress is saved.
- **After a run ends,** typing continues the same conversation, with both agents keeping their context. `/new` starts a fresh topic.
- **Tab / ↑↓** complete commands and arguments. History persists across sessions (Ctrl+R searches it).

### Commands

| Command | What it does |
|---|---|
| `/help` | List all commands |
| `/model [agent] [model]` | Pick a model per agent (Antigravity's list comes from `agy models`) |
| `/effort [agent] [level]` | Reasoning effort: low, medium, high, xhigh, max |
| `/mode [mode]` | Type of work: `build-review`, `plan`, `debate`, `brainstorm`, `custom` |
| `/roles` | Write custom roles for each agent |
| `/turns [n]` | Max turns for this conversation and the default for new ones |
| `/first [agent]` | Who speaks first |
| `/perms [level]` | `none` (talk only) · `edits` (auto-accept file edits) · `auto` (edits + commands) |
| `/workdir [path]` | Shared project folder (`default` = isolated per-run workspace) |
| `/spec <file>` | Start a conversation using a file (e.g. `SPEC.md`) as the task |
| `/pause` · `/stop` · `/continue [n]` | Pause after this turn · interrupt now · run more turns |
| `/new` | Start a fresh conversation |
| `/resume [run]` · `/runs` | Load a past conversation · list them |
| `/transcript [run]` | Open a transcript in your pager |
| `/status` | Settings, sessions, tokens and cost |
| `/config [show\|save\|reset]` | Save the current settings as your defaults |
| `/clear` · `/exit` | Clear the screen · quit |

Defaults live in `~/.config/agent-bridge/config.toml`. Use `/config save`, or edit the file by hand.

## One-shot mode (scripts)

```bash
agent-bridge "Should a 3-person startup use microservices?" -P debate
agent-bridge "$(cat SPEC.md)" -P build-review --perms auto --workdir ~/projects/myapp -n 20
agent-bridge "…" --claude-model opus --agy-model gemini-3.8-flash-high --agy-effort high
agent-bridge --resume ~/.local/share/agent-bridge/runs/20261121-113652 -n 12
agent-bridge --help
```

Command-line flags override your saved defaults.

## Building real projects

1. **Use git.** Point the workdir (`/workdir` or `--workdir`) at a git repo so you can review and roll back whatever the agents do.
2. **Write a spec** (`SPEC.md`) with goals, requirements, and a clear **"Done when"** section. That section is what lets the agents agree to stop.
3. **Work in phases.** Use `/mode plan` to agree on a design, then `/mode build-review` per milestone, and commit between runs.
4. **Use `/perms auto`** if the agents need to run tests. With `edits` they can change files but can't run commands.
5. **Give the reviewer teeth** with `/roles`, e.g. *"QA: run the tests, try to break it, list concrete bugs with file:line"*.
6. **Steer as you go.** Type into the running conversation, and press Esc if they loop.

## Where things are stored

| What | Where |
|---|---|
| Runs (transcripts, `state.json`, workspaces) | `~/.local/share/agent-bridge/runs/` |
| Defaults | `~/.config/agent-bridge/config.toml` |
| Input history | `~/.local/state/agent-bridge/history` |
| Antigravity model list cache (24h) | `~/.cache/agent-bridge/` |

Runs from v0.1 (`~/agent-bridge/runs`) are moved there automatically.

## How it works

- **Pinned sessions:** Claude runs with `--session-id`/`--resume <id>` and Antigravity with `--conversation <id>`. `-c/--continue` is never used, so the bridge can't attach to an unrelated conversation.
- **Small prompts:** each agent keeps its own history, so only the partner's latest message (plus any of yours) is sent each turn.
- **Two-sided stop:** when one agent ends with `[[DONE]]`, the other gets one turn to confirm or object.
- **Survives failures:** per-call timeouts kill the whole process tree, failed calls are retried, and state is saved atomically after every turn.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                  # offline: uses fake agents
AGENT_BRIDGE_FAKE=1 .venv/bin/agent-bridge        # try the UI without spending quota
```

Code layout: `engine.py` (turn loop, UI-agnostic), `agents/` (one adapter per CLI), `tui/` (interactive app), `render.py` (output), `store.py` (runs), `config.py` (defaults). See [PLAN.md](PLAN.md) for the design.

## Safety

`edits` and `auto` let **both** agents change files in the working directory, and `auto` also lets them run commands within each CLI's auto-mode safeguards. Only point the workdir at folders you're willing to have modified, ideally under version control.

## Known quirk

`agy -p --output-format json "…"` fails because `agy` takes the token after `-p` as the prompt. The bridge uses the attached form `--print=<prompt>`.

## License

[MIT](LICENSE)
