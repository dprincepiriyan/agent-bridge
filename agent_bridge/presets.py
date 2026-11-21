"""Static choices: agents, role presets, permission levels, efforts."""

AGENTS = ("claude", "agy")
NAMES = {"claude": "Claude", "agy": "Antigravity"}

STOP_DEFAULT = "[[DONE]]"

# name: (description, role for the first speaker, role for the second speaker)
ROLE_PRESETS = {
    "build-review": (
        "One builds, the other reviews",
        "Implementer: propose concrete designs and write the code/solution.",
        "Reviewer: critique for correctness, security, edge cases and simplicity; demand fixes.",
    ),
    "plan": (
        "One designs, the other stress-tests",
        "Architect: propose a concrete step-by-step plan with trade-offs.",
        "Skeptic: stress-test the plan, find risks and missing steps, propose improvements.",
    ),
    "debate": (
        "Argue opposite sides",
        "Argue FOR the proposition / the first position. Push hard, concede only to strong evidence.",
        "Argue AGAINST / the opposing position. Find weaknesses, counter-examples and hidden costs.",
    ),
    "brainstorm": (
        "Generate and refine ideas",
        "Generate bold, unconventional ideas and build on the partner's ideas.",
        "Generate practical ideas, filter and refine the strongest ones, keep a ranked shortlist.",
    ),
}
CUSTOM = "custom"
MODES = tuple(ROLE_PRESETS) + (CUSTOM,)

PERMS = {
    "none": "Talk only, no tools",
    "edits": "Auto-accept file edits in the working directory (no commands)",
    "auto": "Each CLI's auto mode: edits and commands, with the CLI's safety checks",
}

EFFORTS = ("low", "medium", "high", "xhigh", "max")
