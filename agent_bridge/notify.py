"""Desktop notifications when a run ends or needs attention (via notify-send)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Callable

from .engine import Event
from .presets import NAMES
from .render import fmt_reset


def available() -> bool:
    return bool(shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def send(title: str, body: str, urgency: str = "normal") -> bool:
    if not available():
        return False
    try:
        subprocess.Popen(["notify-send", "-a", "agent-bridge", "-u", urgency, title, body],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


def _short(text: str, n: int = 90) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


class Notifier:
    """Watches engine events; notifies when a run that took a while ends on its own.
    User-initiated stops (Esc, /pause) never notify."""

    def __init__(self, enabled: Callable[[], bool], min_secs: Callable[[], int], sender=send):
        self.enabled = enabled
        self.min_secs = min_secs
        self.sender = sender
        self.started = time.monotonic()

    def __call__(self, ev: Event) -> None:
        if ev.kind == "run_start":
            self.started = time.monotonic()
            return
        if ev.kind != "done" or not self.enabled():
            return
        d = ev.data
        reason = d.get("reason")
        if reason in ("interrupted", "paused"):
            return
        if time.monotonic() - self.started < self.min_secs() and reason not in ("usage_limit", "error", "budget"):
            return
        topic = _short(d.get("topic", ""))
        title, urgency = {
            "agreed": ("✔ Agents agreed the task is done", "normal"),
            "limit": (f"■ Turn limit reached ({d.get('max_turns')})", "normal"),
            "verify_failed": ("✗ Verification failed — no turns left", "critical"),
            "usage_limit": (f"⏸ {NAMES.get(d.get('agent') or '', 'An agent')} hit its usage limit", "critical"),
            "error": ("✗ Run stopped with an error", "critical"),
            "budget": ("⏸ Budget reached", "normal"),
        }.get(reason, (f"Run ended ({reason})", "normal"))
        body = topic
        if reason == "budget":
            body = f"{d.get('message', '')}. {topic}"
        if reason == "usage_limit" and (when := fmt_reset(d.get("resets_at"))):
            body = f"Resets at {when}. {topic}"
        self.sender(title, body, urgency)
