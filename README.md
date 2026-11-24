# agent-bridge

Let **Claude Code** (`claude`) and **Antigravity** (`agy`) talk to each other through their CLIs: debate a question, plan a design, or build and review a project together, taking turns until both agree it's done.

Run `agent-bridge` (or just **`abr`**) for an interactive app in the style of Claude Code, with `/` commands, model pickers, live streaming and a status bar. You can also pass a topic for a one-shot run from scripts.

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

The installer uses `uv` if you have it. Otherwise it creates a private virtual environment in `~/.local/share/agent-bridge/venv`. Either way it puts `agent-bridge` and its short alias **`abr`** in `~/.local/bin`. The install is editable, so after a `git pull` the command uses the new code immediately.

With a Claude Pro/Max subscription and no `ANTHROPIC_API_KEY` set, Claude turns count against your subscription limits and aren't billed per token. The `~$` figures are API-price estimates, not charges.

## Interactive app

```
$ agent-bridge
╭─ agent-bridge v0.8.0 ──────────────────────────────────────────────╮
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
| `/model [agent] [model]` | Pick a model per agent. Accepts IDs or names, e.g. `/model agy Gemini 4 Argon (High)` or `/model agy argon high`; ambiguous names open a picker |
| `/effort [agent] [level]` | Reasoning effort: low, medium, high, xhigh, max |
| `/role [agent] [role]` | Give an agent a role (`/role agy orchestrator`), `/role swap`, `/role create` (your own, saved as a file), `/role reload`; works mid-conversation too |
| `/roles` | Show who does what and every available role |
| `/mode [mode]` | Team preset: `build-review`, `orchestrated`, `design-build`, `build-test`, `secure-build`, `plan`, `debate`, `brainstorm`, `custom`, or one of your saved teams. `/mode save <name>` · `/mode delete <name>` |
| `/budget [limits]` | Hard caps per conversation: `30m`, `2h`, `50 turns`, `usage 80%` (Claude 5-hour window), `cost $5`, `off` |
| `/turns [n]` | Max turns for this conversation and the default for new ones |
| `/first [agent]` | Who speaks first |
| `/perms [level]` | `none` (talk only) · `edits` (auto-accept file edits) · `auto` (edits + commands) |
| `/workdir [path]` | Shared project folder (`default` = isolated per-run workspace) |
| `/init` | Project wizard: 6 questions → `SPEC.md`, verify command, git repo, then starts the build |
| `/spec <file>` | Start a conversation using a file (e.g. `SPEC.md`) as the task |
| `/verify [cmd\|off\|run]` | A command that must pass before "done" counts, e.g. `/verify pytest -q` |
| `/checkpoints [on\|off]` | Git-commit the workdir after every turn; with no argument, list the commits |
| `/diff [turn\|all]` | Show the code changes of the latest turn, a given turn, or the whole conversation |
| `/undo [turn]` | Revert the file changes of the last (or a given) turn; the agents are told |
| `/progress [on\|off\|every N\|keeper …]` | Show the shared `PROGRESS.md`, or configure how often it's shown and who keeps it |
| `/summary [agent]` | Verdict (debates) or summary of the conversation, saved to `summary.md` |
| `/judge [agent\|auto on\|auto off]` | Who judges, and whether debates get a verdict automatically |
| `/pause` · `/stop` · `/continue [n]` | Pause after this turn · interrupt now · run more turns |
| `/publish [folder]` | Put the project on GitHub: creates a repo (private by default) and pushes. Only when you run it; never force-pushes |
| `/new` | Start a fresh conversation |
| `/resume [words]` · `/runs` | Load a past conversation; the menu shows date, mode, progress and topic, and typing words filters by topic (`/resume pizza`) · list them all |
| `/transcript [run]` | Open a transcript in your pager |
| `/status` | Settings, sessions, tokens, cost and Claude usage |
| `/config [show\|save\|reset]` | Save the current settings as your defaults |
| `/notify [on\|off\|test]` | Desktop notification when a run ends on its own |
| `/clear` · `/exit` | Clear the screen · quit |

Defaults live in `~/.config/agent-bridge/config.toml`. Use `/config save`, or edit the file by hand.

## One-shot mode (scripts)

```bash
agent-bridge "Should a 3-person startup use microservices?" -P debate
agent-bridge "$(cat SPEC.md)" -P build-review --perms auto --workdir ~/projects/myapp -n 20 --verify "pytest -q"
agent-bridge "…" --claude-model opus --agy-model gemini-3.8-flash-high --agy-effort high
agent-bridge --resume ~/.local/share/agent-bridge/runs/20261121-113652 -n 12
agent-bridge --help
```

Command-line flags override your saved defaults.

## Roles: who does what

Each agent gets a role from a built-in library, and you choose which agent gets which:

| Role | What it does |
|---|---|
| `orchestrator` | Plans the work, gives one assignment per turn, tracks a checklist, decides when it's done |
| `architect` | Designs modules, interfaces, data model and tech choices (in `ARCHITECTURE.md`) |
| `coder` | Writes the code and tests, and runs them |
| `reviewer` | Reviews for bugs, security, edge cases and simplicity |
| `tester` | Writes and runs tests, tries to break it, reports bugs with steps |
| `debugger` | Reproduces problems and finds root causes |
| `security` | Threat-models and hunts for vulnerabilities |
| `designer` | UI/UX: layout, flows, accessibility |
| `docs` · `devops` · `product` | README and docs · build, scripts and CI · requirements and scope |
| `skeptic` · `advocate` · `critic` · `ideator` · `refiner` | Planning, debating and brainstorming roles |
| `custom` | Write your own |

```
/role agy orchestrator     # Antigravity leads; Claude gets a fitting partner role (coder)
/role claude tester        # explicit roles are never overridden
/role swap                 # trade roles
/mode orchestrated         # or pick a ready-made team
abr "Build a ping pong game" --agy-role orchestrator --claude-role coder --perms auto
```

- **Briefings:** each agent is told its own role and its partner's.
- **Who opens:** lead roles (orchestrator, architect, product manager) speak first automatically.
- **Mid-conversation changes:** both agents are told about the switch on their next turn.
- **`/init`:** asks "who does what?" (e.g. *Antigravity orchestrates · Claude codes*).

### Your own roles

Write a role once and it shows up everywhere next to the built-ins (★ in `/role`, "(yours)" in `/roles`, in completions, and accepted by `--claude-role` / `--agy-role`). Use `/role create`, or drop a Markdown file in `~/.config/agent-bridge/roles/`:

```markdown
---
name: Game developer
summary: Pygame, 60 fps, simple art
---
You build games in Python with Pygame. Keep a steady 60 fps, separate game state from rendering,
and make it playable with the keyboard. Run the game loop headless in tests.
```

The file name is the role id (`game-dev.md` → `/role claude game-dev`). A file named after a built-in role (e.g. `coder.md`) replaces it. Run `/role reload` after editing.

### Saved teams

Found a setup you like? `/mode save my-java-team` stores the roles, who opens, models, efforts, permissions, turns, verify command, progress settings and budgets in `~/.config/agent-bridge/teams/my-java-team.toml`. It doesn't store the project folder, so a team works on any project. Load it with `/mode my-java-team` (it's also listed in the `/mode` menu) or `abr --team my-java-team`; flags still override it.

## Safety nets for long runs

- **Verification.** Set a command with `/verify pytest -q` or `--verify "pytest -q"`. When both agents say `[[DONE]]`, the bridge runs it in the workdir. If it fails, its output goes back to the agents and the conversation continues until it passes or the turns run out. Both agents are told about the check up front.
- **Git checkpoints.** After every turn that changed files, the workdir is committed as `agent-bridge: turn N · Claude`, so `git log` shows who changed what. `/undo` reverts a turn with a revert commit (nothing is deleted) and tells the agents. Per-run workspaces get their own repo automatically. In your own project, checkpoints only start if the repo is clean, so your uncommitted work is never swept into an agent commit. Turn them off with `/checkpoints off` or `--no-checkpoints`.
- **Stuck detection.** An agent is stopped after 10 minutes without any output (`--idle-timeout`) or 60 minutes in total (`--timeout`), then retried.
- **Usage limits.** When Claude or Antigravity reports a usage limit, the run pauses immediately with the reset time, without wasting retries. `/continue` picks up exactly there. The status bar shows Claude's 5-hour window usage.
- **Shared progress memory.** In runs where agents can edit files, the bridge creates `PROGRESS.md` (Goal, Done, In progress, Next, Decisions, Open issues). One agent keeps it updated: the lead role (orchestrator, architect or product manager), otherwise the first speaker. Every 4 turns, and whenever a run resumes, the bridge shows the current file to the agent about to speak, so the plan survives the CLIs compressing their own history. The keeper is nudged if the file goes missing or stale. `/summary` reads it too. Configure it with `/progress every 2`, `/progress keeper claude` or `/progress off` (`--no-progress`).
- **Budgets.** `/budget 2h 50 turns usage 80%` puts hard caps on a conversation: agent time, total turns (even when you keep continuing), Claude's 5-hour usage window, and API-equivalent cost. They're checked between turns, so a run never stops mid-edit. When one is reached it pauses with a message and a notification, and you can raise it (or `/budget off`) and `/continue`. The status bar shows used/limit.
- **Notifications.** When a run that took more than 30 seconds ends on its own (agreed, turn limit, verification failed, usage limit, error), you get a desktop notification via `notify-send`, so you can walk away from long builds. Your own Esc or `/pause` never notifies. Use `/notify off` or `--no-notify` to turn them off.
- **Visible work.** Tool steps name their target ("⚙ Edit src/Bank.java", "⚙ Run mvn -q test"), and `/diff` shows each turn's code changes from its checkpoint.
- **Verdicts.** When a debate ends, a fresh judge call (Claude by default; `/judge agy` to switch) reads the transcript and writes a verdict: winner, strongest arguments, common ground and weak spots. `/summary` works for any conversation, at any time.

## Building real projects

1. **Use git.** Point the workdir (`/workdir` or `--workdir`) at a clean git repo. Every turn becomes a commit you can inspect or `/undo`.
2. **Start with `/init`.** It asks what to build, the stack, the must-haves, "done when" and the test command, then writes `SPEC.md`, sets up git and `/verify`, and starts the build. Or **write a spec** (`SPEC.md`) with goals, requirements, and a clear **"Done when"** section, and back it with `/verify` so "done" is actually checked.
3. **Work in phases.** Use `/mode plan` to agree on a design, then `/mode build-review` per milestone, and commit between runs.
4. **Use `/perms auto`** if the agents need to run tests. With `edits` they can change files but can't run commands.
5. **Give the reviewer teeth** with `/roles`, e.g. *"QA: run the tests, try to break it, list concrete bugs with file:line"*.
6. **Steer as you go.** Type into the running conversation, and press Esc if they loop.

## Publishing to GitHub

The agents never push. They commit locally every turn, and you decide when the result goes to GitHub:

```
/diff all        # review everything they changed
/publish         # create the GitHub repo (private by default) and push
```

- **Already has a remote:** `/publish` shows how many commits are new and pushes after you confirm.
- **No remote, `gh` logged in:** asks for a name (default: the folder name) and visibility, then creates the repo and pushes. Log in once with `gh auth login`.
- **No remote, `gh` not logged in:** create an empty repo at github.com/new and paste its URL. It's pushed over SSH.
- Uncommitted leftovers are committed only if you agree. It never force-pushes: if GitHub has commits you don't (e.g. a README made on the website), it tells you to pull first.

## Where things are stored

| What | Where |
|---|---|
| Runs (transcripts, `state.json`, `summary.md`, workspaces) | `~/.local/share/agent-bridge/runs/` |
| Defaults | `~/.config/agent-bridge/config.toml` |
| Input history | `~/.local/state/agent-bridge/history` |
| Antigravity model list cache (24h) | `~/.cache/agent-bridge/` |

Runs from v0.1 (`~/agent-bridge/runs`) are moved there automatically.

## How it works

- **Pinned sessions:** Claude runs with `--session-id`/`--resume <id>` and Antigravity with `--conversation <id>`. `-c/--continue` is never used, so the bridge can't attach to an unrelated conversation.
- **Small prompts:** each agent keeps its own history, so only the partner's latest message (plus any of yours) is sent each turn.
- **Two-sided stop:** when one agent ends with `[[DONE]]`, the other gets one turn to confirm or object.
- **Survives failures:** idle and total timeouts kill the whole process tree, failed calls are retried (usage limits aren't), and state is saved atomically after every turn.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                  # offline: uses fake agents
AGENT_BRIDGE_FAKE=1 .venv/bin/agent-bridge        # try the UI without spending quota
```

Code layout: `engine.py` (turn loop, verification, checkpoints, verdicts; UI-agnostic), `agents/` (one adapter per CLI), `workspace.py` (git and verify helpers), `tui/` (interactive app), `render.py` (output), `store.py` (runs), `config.py` (defaults). See [PLAN.md](PLAN.md) for the design.

## Safety

`edits` and `auto` let **both** agents change files in the working directory, and `auto` also lets them run commands within each CLI's auto-mode safeguards. Only point the workdir at folders you're willing to have modified, ideally under version control.

## Known quirk

`agy -p --output-format json "…"` fails because `agy` takes the token after `-p` as the prompt. The bridge uses the attached form `--print=<prompt>`.

## License

[MIT](LICENSE)
