"""Turns engine events into terminal output (shared by one-shot and interactive modes)."""
from __future__ import annotations

import re
import sys

from rich.console import Console
from rich.text import Text

from .engine import Event
from .presets import NAMES

COLORS = {"claude": "#e08a63", "agy": "#7fb2ff"}
TOOL_LABELS = {"run_command": "running a command"}


def tilde(path) -> str:
    """Show paths under $HOME as ~/…"""
    from pathlib import Path
    p, home = str(path), str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def make_console() -> Console:
    # file=None → rich resolves sys.stdout on every write, so prompt_toolkit's
    # patch_stdout proxy is picked up automatically in interactive mode.
    return Console(highlight=False, force_terminal=sys.stdout.isatty() or None)


_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)")


def md_line(line: str, in_code: bool) -> tuple[Text, bool]:
    """Light Markdown for one streamed line: headings, **bold**, `code`, fenced blocks."""
    if line.lstrip().startswith("```"):
        return Text(line, style="dim"), not in_code
    if in_code:
        return Text(line, style="#c3d08b"), True
    if m := re.match(r"^(#{1,6})\s+(.*)", line):
        return Text(m.group(2), style="bold underline"), False
    t = Text()
    for part in _INLINE.split(line):
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            t.append(part[2:-2], style="bold")
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            t.append(part[1:-1], style="#c3d08b")
        else:
            t.append(part)
    return t, False


class Renderer:
    """Streams agent text line by line (keeps output clean above an active prompt)."""

    def __init__(self, console: Console):
        self.c = console
        self.buf = ""
        self.agent: str | None = None
        self.in_code = False

    def _line(self, line: str) -> None:
        text, self.in_code = md_line(line, self.in_code)
        self.c.print(text)

    def _flush(self) -> None:
        if self.buf:
            self._line(self.buf)
            self.buf = ""

    def __call__(self, ev: Event) -> None:
        k = ev.kind
        if k == "text":
            self.buf += ev.text
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                self._line(line)
        elif k == "turn_start":
            self._flush()
            self.in_code = False
            self.agent = ev.agent
            color = COLORS.get(ev.agent, "white")
            self.c.print()
            self.c.rule(Text.assemble(("▶ ", color), (f"{ev.text} · {NAMES[ev.agent]}", f"bold {color}")),
                        align="left", style=color)
        elif k == "tool":
            self._flush()
            self.c.print(Text(f"  ⚙ {TOOL_LABELS.get(ev.text, ev.text)}", style="dim"))
        elif k == "turn_end":
            self._flush()
            meta = ev.data.get("meta") or ""
            self.c.print(Text(f"  [{ev.data.get('secs', 0):.0f}s{' · ' + meta if meta else ''}]", style="dim"))
        elif k == "retry":
            self._flush()
            self.c.print(Text(f"  ✗ attempt {ev.data.get('attempt')} failed: {ev.text} — retrying", style="red"))
        elif k == "user_msg":
            self._flush()
            self.c.print(Text.assemble(("  ✎ you → ", "bold green"),
                                       (f"{NAMES[ev.data.get('to', 'claude')]} (next turn): ", "green"),
                                       (ev.text, "")))
        elif k == "notice":
            self._flush()
            self.c.print(Text(f"  {ev.text}", style="yellow"))
        elif k == "error":
            self._flush()
            self.c.print(Text(f"\n✗ {ev.text}", style="bold red"))
        elif k == "done":
            self._flush()
            r = ev.data["reason"]
            msg, style = {
                "agreed": ("✔ Both agents agreed the task is done.", "bold green"),
                "limit": (f"■ Reached the turn limit ({ev.data['max_turns']}).", "yellow"),
                "paused": ("‖ Paused.", "yellow"),
                "interrupted": ("■ Interrupted. Progress saved.", "yellow"),
                "error": ("✗ Stopped after repeated failures. Progress saved.", "red"),
            }[r]
            self.c.print()
            self.c.print(Text(msg, style=style))
