"""Role library and team presets.

A *role* is what one agent does (orchestrator, coder, reviewer…). Each agent gets its own.
A *team* (the /mode) is a ready-made pair: the first speaker's role + the second's.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    name: str
    summary: str
    text: str


ROLES: dict[str, Role] = {
    # ---- building software
    "orchestrator": Role(
        "Orchestrator", "plans the work, assigns tasks, tracks progress, decides when it's done",
        "Orchestrator / tech lead. Break the task into small, concrete steps and give your partner ONE clear "
        "assignment per turn (what to build, which files, acceptance criteria). Check what they delivered "
        "(read the files, run the tests if you can), give precise feedback, then assign the next step. Keep a "
        "short checklist of done / next in your messages. Write little code yourself; you own scope, order "
        "and the final call on whether the task is complete."),
    "architect": Role(
        "Architect", "designs structure, modules, interfaces and tech choices",
        "Software architect. Decide the structure before code is written: modules and their "
        "responsibilities, interfaces and data model, key libraries, and how it will be tested. Record the "
        "design in ARCHITECTURE.md (if you can edit files) and keep it current. Review your partner's work for "
        "fit with the design and push back on accidental complexity."),
    "coder": Role(
        "Coder", "writes the code and tests, and runs them",
        "Implementer. Write clean, working code in small increments, with tests alongside it. Run the tests "
        "(if you can run commands) before saying something works. Report exactly what you changed (files, "
        "functions) and anything left unfinished. Ask for clarification only when truly blocked."),
    "reviewer": Role(
        "Reviewer", "reviews code for bugs, security, edge cases and simplicity",
        "Code reviewer. Read the actual changes and critique them for correctness, edge cases, security, "
        "error handling, naming and simplicity. Point to specific files and lines and say what to change. "
        "Approve only when it's genuinely solid; don't nitpick style that doesn't matter."),
    "tester": Role(
        "Tester / QA", "writes and runs tests, tries to break it, reports bugs",
        "QA engineer. Write and run tests, including edge cases and failure paths; try hard to break the "
        "software. Report each bug with steps to reproduce, expected vs actual, and file:line where "
        "possible. Re-test fixes and confirm when they hold."),
    "debugger": Role(
        "Debugger", "reproduces problems and finds root causes",
        "Debugger. For each problem: reproduce it, narrow it down, find the root cause (not just the "
        "symptom), and propose or apply the smallest correct fix. Explain the cause in one or two sentences."),
    "security": Role(
        "Security reviewer", "threat-models and hunts for vulnerabilities",
        "Security engineer. Threat-model the system and review for vulnerabilities: input validation, "
        "injection, authN/authZ, secrets handling, unsafe defaults, dependency risks. Rate each finding by "
        "severity with a concrete fix."),
    "designer": Role(
        "UI/UX designer", "shapes the interface, layout, usability and accessibility",
        "UI/UX designer. Define how it looks and feels: layout, flows, wording, visual hierarchy, "
        "accessibility and responsiveness. Give concrete, implementable specs (components, spacing, states) "
        "and review the result against them."),
    "docs": Role(
        "Docs writer", "writes the README and user/developer docs",
        "Technical writer. Keep the README and docs accurate and useful: what it is, how to install, run and "
        "test it, with examples. Verify instructions against the code, and ask your partner when behavior "
        "is unclear."),
    "devops": Role(
        "DevOps", "build setup, scripts, packaging, CI",
        "DevOps engineer. Own the build and run experience: project setup, dependency management, scripts, "
        "packaging, configuration and CI. Make it reproducible from a clean checkout with one or two commands."),
    "product": Role(
        "Product manager", "sharpens requirements and acceptance criteria, guards scope",
        "Product manager. Turn the goal into clear user stories with acceptance criteria, decide priorities, "
        "and guard the scope against creep. Judge deliverables from the user's point of view."),
    # ---- thinking & discussion
    "skeptic": Role(
        "Skeptic", "stress-tests plans, finds risks and missing steps",
        "Skeptic. Stress-test your partner's proposals: find risks, hidden assumptions, missing steps and "
        "cheaper alternatives. Be constructive: every objection comes with a suggested improvement."),
    "advocate": Role(
        "Advocate (for)", "argues FOR the proposition",
        "Argue FOR the proposition / the first position. Push hard with evidence and examples; concede only "
        "to strong arguments."),
    "critic": Role(
        "Critic (against)", "argues AGAINST the proposition",
        "Argue AGAINST / the opposing position. Find weaknesses, counter-examples and hidden costs; concede "
        "only to strong arguments."),
    "ideator": Role(
        "Ideator", "generates bold ideas and builds on the partner's",
        "Generate bold, unconventional ideas and build on your partner's ideas."),
    "refiner": Role(
        "Refiner", "filters and sharpens ideas into a ranked shortlist",
        "Generate practical ideas, filter and refine the strongest ones, and keep a ranked shortlist."),
}

CUSTOM = "custom"


@dataclass(frozen=True)
class Team:
    summary: str
    first: str   # role key for the agent that speaks first
    second: str  # role key for the other agent


TEAMS: dict[str, Team] = {
    "build-review": Team("one codes, the other reviews", "coder", "reviewer"),
    "orchestrated": Team("one orchestrates and assigns tasks, the other codes", "orchestrator", "coder"),
    "design-build": Team("one designs the architecture, the other builds it", "architect", "coder"),
    "build-test": Team("one codes, the other tests and tries to break it", "coder", "tester"),
    "secure-build": Team("one codes, the other reviews for security", "coder", "security"),
    "plan": Team("one designs a plan, the other stress-tests it", "architect", "skeptic"),
    "debate": Team("argue opposite sides", "advocate", "critic"),
    "brainstorm": Team("generate and refine ideas", "ideator", "refiner"),
}

# a sensible partner for each role (used when only one agent's role is chosen)
PARTNER = {
    "orchestrator": "coder", "architect": "coder", "product": "coder", "coder": "reviewer",
    "reviewer": "coder", "tester": "coder", "security": "coder", "debugger": "coder", "designer": "coder",
    "docs": "coder", "devops": "coder", "skeptic": "architect", "advocate": "critic", "critic": "advocate",
    "ideator": "refiner", "refiner": "ideator",
}

# roles that should open the conversation
LEADS = {"orchestrator", "architect", "product"}


def role_name(key: str) -> str:
    return ROLES[key].name if key in ROLES else ("Custom" if key == CUSTOM else key)
