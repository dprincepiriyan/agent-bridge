"""The interactive app: prompt, slash commands, status bar, background runs."""
from __future__ import annotations

import asyncio
import time
import traceback

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app_or_none
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.panel import Panel
from rich.text import Text

from .. import __version__, config
from ..agents import make_adapters, missing_executables
from ..config import Settings
from ..engine import Engine, Event
from ..presets import AGENTS, NAMES
from ..render import COLORS, Renderer, make_console, tilde
from . import commands

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

STYLE = Style.from_dict({
    "bottom-toolbar": "noreverse bg:default #9a9a9a",
    "tb.claude": "#e08a63",
    "tb.agy": "#7fb2ff",
    "tb.sep": "#5a5a5a",
    "tb.busy": "bold #e5c07b",
    "tb.hint": "#7a7a7a italic",
    "prompt": "bold #7fb2ff",
    "prompt.busy": "bold #e5c07b",
    "completion-menu.completion": "bg:#2a2a2a #d0d0d0",
    "completion-menu.completion.current": "bg:#3d5a80 #ffffff bold",
    "completion-menu.meta.completion": "bg:#2a2a2a #8a8a8a",
    "completion-menu.meta.completion.current": "bg:#3d5a80 #e0e0e0",
})


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.console = make_console()
        self.renderer = Renderer(self.console)
        self.adapters = make_adapters()
        self.engine: Engine | None = None
        self.task: asyncio.Task | None = None
        self.should_exit = False
        self.model_cache: dict[str, list[tuple[str, str]]] = {}
        self._last_ctrl_c = 0.0
        config.state_dir().mkdir(parents=True, exist_ok=True)
        self.session: PromptSession = PromptSession(
            history=FileHistory(str(config.state_dir() / "history")),
            completer=commands.SlashCompleter(self),
            complete_while_typing=True,
            bottom_toolbar=self.toolbar,
            refresh_interval=0.25,
            key_bindings=self._bindings(),
            style=STYLE,
            reserve_space_for_menu=8,
        )

    # ---- small output helpers --------------------------------------------
    def info(self, msg: str) -> None:
        self.console.print(Text(msg, style="cyan"))

    def warn(self, msg: str) -> None:
        self.console.print(Text(msg, style="yellow"))

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def models(self, agent: str, refresh: bool = False) -> list[tuple[str, str]]:
        if refresh or agent not in self.model_cache:
            self.model_cache[agent] = await self.adapters[agent].list_models(refresh=refresh)
        return self.model_cache[agent]

    # ---- key bindings & toolbar ------------------------------------------
    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        idle_buffer = Condition(lambda: self.running() and not self.session.default_buffer.text)

        @kb.add("escape", filter=idle_buffer, eager=True)
        def _esc(event):
            self.interrupt()

        return kb

    def _model_of(self, agent: str) -> str:
        if self.engine:
            m = self.engine.state["agents"][agent].get("model")
        else:
            m = getattr(self.settings, f"{agent}_model")
        return m or "default"

    def toolbar(self):
        sep = ("class:tb.sep", " │ ")
        parts = [("", " "), ("class:tb.claude", f"claude:{self._model_of('claude')}"), sep,
                 ("class:tb.agy", f"agy:{self._model_of('agy')}"), sep,
                 ("", self.settings.preset), sep, ("", f"perms:{self.engine.state['perms'] if self.engine else self.settings.perms}")]
        if self.engine:
            st = self.engine.state
            parts += [sep, ("", f"turn {st['turn']}/{st['max_turns']}")]
            if self.engine.cost:
                parts += [sep, ("", f"~${self.engine.cost:.2f}")]
        if self.running():
            cur = self.engine.current if self.engine else None
            frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            if cur:
                agent, t0 = cur
                busy = f"{frame} {NAMES[agent]} working {int(time.monotonic() - t0)}s"
            else:
                busy = f"{frame} working"
            parts += [sep, ("class:tb.busy", busy), ("class:tb.hint", "  esc stop · type to steer")]
        else:
            parts += [("class:tb.hint", "   / commands · ctrl+d quit")]
        return FormattedText(parts)

    def _prompt_message(self):
        if self.running():
            return FormattedText([("class:prompt.busy", "✎ ")])
        return FormattedText([("class:prompt", "❯ ")])

    # ---- runs --------------------------------------------------------------
    def start_new(self, topic: str) -> None:
        s = self.settings
        if s.workdir and s.perms != "none":
            self.warn(f"⚠ agents may modify files in {s.workdir}")
        self.engine = Engine.new(s, topic, self.adapters, self.renderer)
        self.start_run()

    def start_run(self) -> None:
        assert self.engine is not None
        self.engine.emit = self.renderer
        self.task = asyncio.create_task(self.engine.run())
        self.task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            pass
        elif (exc := task.exception()) is not None:
            self.console.print(Text("".join(traceback.format_exception(exc)), style="red"))
            self.warn("Unexpected error — progress up to the last completed turn is saved.")
        else:
            reason = task.result()
            if reason == "agreed":
                self.console.print(Text("Type a message to keep going in this conversation, or /new for a new topic.",
                                        style="dim"))
            elif reason in ("limit", "paused", "error"):
                self.console.print(Text("Type a message to steer, /continue for more turns, or /new.", style="dim"))
        if self.engine:
            self.console.print(Text(f"Transcript: {tilde(self.engine.run_dir / 'transcript.md')}", style="dim"), soft_wrap=True)
        if (app := get_app_or_none()) is not None:
            app.invalidate()

    def interrupt(self) -> None:
        if self.running():
            self.task.cancel()

    async def handle_text(self, text: str) -> None:
        if self.running():
            self.engine.add_user_message(text)
            return
        if self.engine is None:
            self.start_new(text)
            return
        self.engine.add_user_message(text)
        self.engine.extend(self.settings.turns)
        self.start_run()

    # ---- main loop -----------------------------------------------------------
    def banner(self) -> None:
        s = self.settings
        missing = missing_executables(self.adapters)
        body = Text.assemble(
            ("Claude", f"bold {COLORS['claude']}"), f": {s.claude_model or 'default'}   ",
            ("Antigravity", f"bold {COLORS['agy']}"), f": {s.agy_model or 'default'}\n",
            ("Mode", "bold"), f": {s.preset} · ", ("Turns", "bold"), f": {s.turns} · ",
            ("Perms", "bold"), f": {s.perms} · ", ("Dir", "bold"), f": {tilde(s.workdir) if s.workdir else 'per-run workspace'}",
        )
        self.console.print(Panel(body, title=f"[bold]agent-bridge[/] v{__version__}", title_align="left",
                                 border_style="#5a5a5a", expand=False))
        if missing:
            self.warn(f"Not found on PATH: {', '.join(missing)} — runs will fail until installed.")
        self.console.print(Text(" Type a topic or task to start, or / for commands.\n", style="dim"))

    async def main(self) -> int:
        self.banner()
        with patch_stdout(raw=True):
            while not self.should_exit:
                try:
                    line = await self.session.prompt_async(self._prompt_message)
                except KeyboardInterrupt:
                    if self.running():
                        self.interrupt()
                    elif time.monotonic() - self._last_ctrl_c < 2:
                        break
                    else:
                        self._last_ctrl_c = time.monotonic()
                        self.console.print(Text("Press Ctrl+C again to quit (or /exit).", style="dim"))
                    continue
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    if line.startswith("/") and not line.startswith("//"):
                        await commands.dispatch(self, line)
                    else:
                        await self.handle_text(line[1:] if line.startswith("//") else line)
                except Exception as e:  # a bad command must never kill the app
                    self.console.print(Text(f"Error: {e}", style="red"))
            if self.running():
                self.interrupt()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.engine:
            self.console.print(Text(f"Saved. Resume later with /resume {self.engine.run_dir.name}", style="dim"))
        return 0


def run_tui(settings: Settings) -> int:
    try:
        return asyncio.run(App(settings).main())
    except KeyboardInterrupt:
        return 130
