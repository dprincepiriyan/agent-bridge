"""Static choices: agents, modes, permission levels, efforts."""

AGENTS = ("claude", "agy")
NAMES = {"claude": "Claude", "agy": "Antigravity"}

STOP_DEFAULT = "[[DONE]]"

from .roles import CUSTOM, ROLES, TEAMS  # noqa: E402

# mode name: (description, first speaker's role text, second speaker's role text)
ROLE_PRESETS = {k: (t.summary, ROLES[t.first].text, ROLES[t.second].text) for k, t in TEAMS.items()}
MODES = tuple(TEAMS) + (CUSTOM,)

PERMS = {
    "none": "Talk only, no tools",
    "edits": "Auto-accept file edits in the working directory (no commands)",
    "auto": "Each CLI's auto mode: edits and commands, with the CLI's safety checks",
}

EFFORTS = ("low", "medium", "high", "xhigh", "max")
