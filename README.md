# agent-bridge

Let **Claude Code** (`claude`) and **Antigravity** (`agy`) talk to each other through their CLIs: debate a question, plan a design, or build and review a project together, taking turns until both agree it's done.

```
 you ──topic──► agent-bridge
                  ├─► claude -p  (session pinned by ID) ──reply──┐
                  ◄──────────────────────────────────────────────┘
                  │  relays it as "Claude said: …"
                  ├─► agy -p     (conversation pinned by ID) ──reply──┐
                  ◄────────────────────────────────────────────────────┘
                  └─ repeats until both say [[DONE]] or the turn limit is hit
```

## Features

- **Pinned sessions:** each agent's conversation is resumed by its exact ID. `-c/--continue` is never used, so the bridge can't attach to an unrelated conversation.
- **Isolated runs:** every run gets `runs/<timestamp>/` with its own workspace, a live `transcript.md`, and a `state.json`.
- **Small prompts:** each agent keeps its own history, so only the other agent's latest message is sent each turn.
- **Two-sided stop:** when one agent says it's done, the other gets one turn to confirm or object. A hard turn cap always applies.
- **Survives failures:** per-call timeouts and retries. Ctrl+C or an error saves state, and `--resume` continues where it stopped.
- **Role presets:** `debate`, `build-review`, `plan`, `brainstorm`, or your own roles.
- **No dependencies:** Python 3 standard library only.

## Requirements

- Python 3.9+
- [Claude Code](https://claude.com/claude-code) CLI (`claude`), logged in
- Antigravity CLI (`agy`), logged in

Both CLIs use your existing logins. With a Claude Pro/Max subscription and no `ANTHROPIC_API_KEY` set, Claude turns count against your subscription usage limits rather than being billed per token. The `$` figures the bridge prints are API-price estimates, not charges.

## Install

```bash
git clone git@github.com:dprincepiriyan/agent-bridge.git ~/agent-bridge
chmod +x ~/agent-bridge/bridge.py
ln -sf ~/agent-bridge/bridge.py ~/.local/bin/agent-bridge   # make sure ~/.local/bin is on PATH
agent-bridge --help
```

## Usage

```bash
# Debate (talk only, no tools)
agent-bridge "Should a 3-person startup use microservices?" -P debate

# Plan a design, Antigravity speaks first
agent-bridge "Design the data model for a habit tracker" -P plan -f agy -n 6

# Build and review in a real project folder
agent-bridge "$(cat SPEC.md)" -P build-review --perms auto --workdir ~/projects/myapp -n 20

# Continue a previous run (-n sets the new total turn cap)
agent-bridge --resume ~/agent-bridge/runs/20261121-113652 -n 12
```

Follow along in another terminal:

```bash
tail -f ~/agent-bridge/runs/<run>/transcript.md
```

### Options

| Option | Default | Description |
|---|---|---|
| `topic` | (required) | Topic or task. Use `"$(cat SPEC.md)"` for long specs. |
| `-P, --preset` | `build-review` | `debate`, `build-review`, `plan`, `brainstorm` |
| `--role-a`, `--role-b` | preset | Custom roles for the first and second speaker |
| `-f, --first` | `claude` | Who speaks first: `claude` or `agy` |
| `-n, --turns` | `8` | Maximum total turns |
| `--perms` | `none` | `none`: talk only · `edits`: auto-accept file edits · `auto`: each CLI's auto mode (can run commands) |
| `--workdir` | per-run workspace | Shared folder both agents work in |
| `--timeout` | `600` | Seconds per agent call |
| `--retries` | `2` | Retries per failed call |
| `--max-words` | `350` | Suggested length per message |
| `--stop-token` | `[[DONE]]` | Token both agents must emit to finish |
| `--claude-model`, `--agy-model` | CLI default | Model for each agent |
| `--resume RUN_DIR` | | Continue a saved run |

## Building complex projects

1. **Use git.** Point `--workdir` at a git repo so you can review and roll back whatever the agents do.
2. **Write a spec** (`SPEC.md`) with goals, requirements, and a clear **"Done when"** section. That section is what lets the agents agree to stop.
3. **Work in phases.** Run `-P plan` to agree on a design, then `-P build-review` per milestone, and commit between runs.
4. **Use `--perms auto`** if the agents need to run tests. With `edits` they can change files but can't run commands.
5. **Give the reviewer teeth.** For example: `--role-b "QA: run the tests, try to break it, list concrete bugs with file:line"`.
6. **If they loop without progress,** press Ctrl+C, tighten the spec, and resume or start the next phase.

## Safety

- `--perms edits` and `--perms auto` let **both** agents change files in the working directory, and `auto` also lets them run commands within each CLI's auto-mode safeguards. Only point `--workdir` at folders you're willing to have modified, ideally under version control.
- `runs/` contains transcripts and session IDs and is git-ignored. Don't commit it.

## Known quirk

`agy -p --output-format json "…"` fails because `agy` takes the token after `-p` as the prompt. The bridge uses the attached form `--print=<prompt>` to avoid this.

## License

[MIT](LICENSE)
