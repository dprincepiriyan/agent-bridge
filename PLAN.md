# Plan: agent-bridge as an interactive CLI (v0.2)

Goal: running `agent-bridge` with no arguments opens an interactive app in the style of Claude Code or `agy`: a prompt, a `/` command menu with autocomplete, model pickers, a status bar, and the conversation streaming live. The one-shot flag-based mode keeps working for scripts.

## What it looks like

```
$ agent-bridge
╭─ agent-bridge v0.2 ───────────────────────────────────────────╮
│ Claude: opus · Antigravity: gemini-4-neon-high               │
│ Mode: build-review · Turns: 8 · Perms: none · Dir: ~/projects/app │
╰──────────────────────────────────────────────────────────────╯
 Type a topic to start, or / for commands.

> /mo
  /model     Change the model for Claude or Antigravity
  /mode      Pick the type of work (debate, plan, build-review…)

> Build a CLI todo app with tests
▶ Turn 1/8 · Claude ─────────────────────────────────────────
Antigravity, here's my plan… (text streams in live)
─────────────────────────────────────────────────────────────
 claude:opus │ agy:gemini-4-neon-high │ build-review │ 2/8 │ $0.06
```

## Slash commands

| Command | What it does |
|---|---|
| `/help` | List all commands |
| `/model` | Picker: choose the agent, then its model |
| `/effort` | Reasoning effort per agent |
| `/mode` | Type of work: debate, plan, build-review, brainstorm, custom |
| `/roles` | Edit each agent's role text |
| `/turns` | Max turns |
| `/first` | Who speaks first |
| `/perms` | none / edits / auto |
| `/workdir` | Shared project folder |
| `/spec` | Load the topic from a file |
| `/pause`, `/continue`, `/stop` | Control a running conversation |
| `/resume` | Pick a past run to continue |
| `/runs`, `/transcript` | Browse past runs and transcripts |
| `/status` | Current settings, session IDs, cost |
| `/config` | Save current settings as defaults |
| `/clear`, `/exit` | Clear the screen, quit |

## Behavior

- Live streaming of each agent's reply.
- Typing while agents work queues a message that the next agent receives as "The user says: …".
- Esc interrupts the current turn and saves state.
- Status bar with models, mode, turn count and cost.
- Persistent input history (up/down arrows, Ctrl+R).
- Antigravity models come from `agy models` (cached for a day). Claude models are the aliases plus a custom ID option.
- Defaults are saved in `~/.config/agent-bridge/config.toml`. Flags override them.
- `agent-bridge "topic" -P debate -n 4` still runs non-interactively.

## Architecture

```
agent_bridge/
  cli.py          entry point: flags → one-shot, no args → interactive
  engine.py       turn loop, stop handshake, resume; emits events
  agents/         one adapter per CLI (command building, stream parsing, models)
  store.py        run folders, state.json, transcripts
  config.py       defaults
  tui/            prompt loop, slash commands, pickers, rendering
tests/
pyproject.toml
```

- The engine knows nothing about the UI. It emits events that both the interactive app and the one-shot mode consume.
- Agents run in the background (asyncio), so the prompt stays responsive.
- Each CLI's quirks live in its adapter. A new agent means one new adapter file.
- Runs live in `~/.local/share/agent-bridge/runs`. Old runs are migrated automatically.
- A fake echo adapter lets tests run without spending quota.

## Phases

1. Restructure into the package/engine/adapter layout with unchanged behavior, plus tests.
2. Live streaming via stream-json.
3. Basic interactive app: prompt, `/` autocomplete, `/help`, status bar, start a run.
4. Settings commands and the config file.
5. Run control: background runs, Esc, interjecting, pause/continue/stop, resume picker, runs and transcripts.
6. Polish: installation, run migration, README, version, final tests.

## Status

All six phases were implemented in v0.2.0:

- [x] 1. Package, engine and adapter layout. One-shot mode kept.
- [x] 2. Live streaming (stream-json for both CLIs).
- [x] 3. Interactive app: prompt, `/` autocomplete, `/help`, status bar.
- [x] 4. Settings commands (`/model`, `/effort`, `/mode`, `/roles`, `/turns`, `/first`, `/perms`, `/workdir`) and `config.toml`.
- [x] 5. Run control: background runs, Esc/Ctrl+C, steering mid-run, `/pause`, `/stop`, `/continue`, `/resume`, `/runs`, `/transcript`.
- [x] 6. `install.sh` (uv or private venv), v0.1 run migration, README, 29 offline tests.

Deviation: `install.sh` falls back to a private virtual environment when `uv` isn't installed, so `uv` is optional.
