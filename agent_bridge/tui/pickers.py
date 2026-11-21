"""Small inline widgets: arrow-key picker and text input."""
from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

STYLE = Style.from_dict({
    "title": "bold",
    "sel": "bold #7fb2ff",
    "cur": "#6cc070",
    "desc": "#8a8a8a",
    "hint": "#8a8a8a italic",
})

Option = tuple[str, str, str]  # (value, label, description)


async def pick(title: str, options: list[Option], current: str | None = None) -> str | None:
    """Arrow keys / j k / number to choose, Enter to confirm, Esc to cancel."""
    if not options:
        return None
    idx = next((i for i, o in enumerate(options) if o[0] == current), 0)
    st = {"i": idx}
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    @kb.add("s-tab")
    def _up(e):
        st["i"] = (st["i"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    @kb.add("tab")
    def _down(e):
        st["i"] = (st["i"] + 1) % len(options)

    @kb.add("enter")
    def _ok(e):
        e.app.exit(result=options[st["i"]][0])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("q")
    def _cancel(e):
        e.app.exit(result=None)

    for n in range(1, min(len(options), 9) + 1):
        @kb.add(str(n))
        def _num(e, n=n):
            e.app.exit(result=options[n - 1][0])

    def render():
        out = [("class:title", f" {title}\n")]
        for i, (value, label, desc) in enumerate(options):
            sel = i == st["i"]
            num = f"{i + 1}." if i < 9 else "  "
            out.append(("class:sel" if sel else "", f" {'❯' if sel else ' '} {num} {label}"))
            if value == current:
                out.append(("class:cur", "  ✓ current"))
            if desc:
                out.append(("class:desc", f"  — {desc}"))
            out.append(("", "\n"))
        out.append(("class:hint", " ↑/↓ move · enter select · esc cancel"))
        return out

    app = Application(layout=Layout(Window(FormattedTextControl(render, show_cursor=False))),
                      key_bindings=kb, style=STYLE, full_screen=False, erase_when_done=True)
    return await app.run_async()


async def ask_text(message: str, default: str = "", completer: Completer | None = None) -> str | None:
    """Single-line input; None if cancelled (Ctrl+C / Ctrl+D / Esc)."""
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _cancel(e):
        e.app.exit(result=None)

    try:
        return await PromptSession(key_bindings=kb).prompt_async(
            message, default=default, completer=completer, complete_while_typing=completer is not None)
    except (KeyboardInterrupt, EOFError):
        return None


async def confirm(question: str, default_yes: bool = False) -> bool:
    opts = [("yes", "Yes", ""), ("no", "No", "")]
    if not default_yes:
        opts.reverse()  # the highlighted (first) option is the safe default
    return await pick(question, opts) == "yes"
