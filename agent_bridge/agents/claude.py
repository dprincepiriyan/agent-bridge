"""Claude Code adapter (`claude -p --output-format stream-json`)."""
from __future__ import annotations

import asyncio
import json
import uuid

from .base import (Adapter, AgentError, ConfigError, TurnContext, TurnResult, UsageLimitError,
                   describe_tool, looks_like_bad_model, looks_like_limit, parse_reset, stream_process)

MODELS = [
    ("opus", "Opus (latest)"),
    ("sonnet", "Sonnet (latest)"),
    ("haiku", "Haiku (latest)"),
    ("fable", "Fable (latest)"),
]

PERM_FLAGS = {
    "none": ["--tools", ""],
    "edits": ["--permission-mode", "acceptEdits"],
    "auto": ["--permission-mode", "auto"],
}


class ClaudeAdapter(Adapter):
    name = "claude"
    label = "Claude"
    executable = "claude"

    def __init__(self):
        self.limits = None

    def build_cmd(self, agent: dict, prompt: str, ctx: TurnContext) -> list[str]:
        cmd = ["claude", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if ctx.ephemeral:
            cmd += ["--no-session-persistence"]
        elif agent.get("started"):
            cmd += ["--resume", agent["session"]]
        else:
            cmd += ["--session-id", agent["session"]]
        cmd += PERM_FLAGS[ctx.perms]
        cmd += ["--append-system-prompt", ctx.system_prompt]
        if agent.get("model"):
            cmd += ["--model", agent["model"]]
        if agent.get("effort"):
            cmd += ["--effort", agent["effort"]]
        return cmd + ["-p", prompt]

    def _note_limits(self, info: dict) -> None:
        windows = info.get("unifiedWindows") or {}
        self.limits = {
            "status": info.get("status"),
            "resets_at": info.get("resetsAt"),
            "five_hour": (windows.get("five_hour") or {}).get("utilization"),
            "seven_day": (windows.get("seven_day") or {}).get("utilization"),
        }

    async def send(self, agent, prompt, ctx, on_text, on_tool) -> TurnResult:
        if not agent.get("session") and not ctx.ephemeral:
            agent["session"] = str(uuid.uuid4())
        result: dict | None = None
        had_text = False
        junk: list[str] = []
        tools: dict = {}  # content-block index → [tool name, streamed JSON input]

        def on_line(line: str) -> None:
            nonlocal result, had_text
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                junk.append(line)
                return
            t = d.get("type")
            if t == "stream_event":
                ev = d.get("event") or {}
                et = ev.get("type")
                if et == "content_block_start":
                    block = ev.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tools[ev.get("index")] = [block.get("name", "tool"), ""]
                    elif block.get("type") == "text" and had_text:
                        on_text("\n\n")
                elif et == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        had_text = True
                        on_text(delta["text"])
                    elif delta.get("type") == "input_json_delta" and ev.get("index") in tools:
                        tools[ev.get("index")][1] += delta.get("partial_json") or ""
                elif et == "content_block_stop" and ev.get("index") in tools:
                    name, raw = tools.pop(ev.get("index"))
                    try:
                        params = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        params = {}
                    on_tool(describe_tool(name, params, ctx.workdir))
            elif t == "rate_limit_event":
                self._note_limits(d.get("rate_limit_info") or {})
            elif t == "result":
                result = d

        def rotate():
            if not agent.get("started") and not ctx.ephemeral:
                agent["session"] = str(uuid.uuid4())  # never reuse a half-created session ID

        try:
            code, err = await stream_process(self.build_cmd(agent, prompt, ctx), ctx.workdir, ctx.timeout,
                                             on_line, ctx.idle_timeout)
        except (AgentError, asyncio.CancelledError):
            rotate()  # an interrupted first call may have claimed the session ID already
            raise
        if result is None or result.get("is_error") or result.get("subtype", "success") != "success":
            rotate()
            detail = (result or {}).get("result") or err[-1500:] or "\n".join(junk[-5:])
            if looks_like_bad_model(str(detail)):
                raise ConfigError(f"Claude doesn't recognise the model \"{agent.get('model') or 'default'}\" "
                                  f"({str(detail)[:200]}). Pick a valid one with /model claude.")
            rejected = (self.limits or {}).get("status") == "rejected"
            if rejected or looks_like_limit(str(detail)):
                resets = (self.limits or {}).get("resets_at") if rejected else None
                raise UsageLimitError(f"Claude usage limit reached: {str(detail)[:300]}",
                                      resets or parse_reset(str(detail)))
            raise AgentError(f"claude failed (exit {code}): {detail}")
        if not ctx.ephemeral:
            agent["started"] = True
        cost = result.get("total_cost_usd")
        usage = result.get("usage") or {}
        tokens = sum(usage.get(k, 0) or 0 for k in ("input_tokens", "output_tokens",
                                                   "cache_creation_input_tokens", "cache_read_input_tokens"))
        meta = f"~${cost:.3f} API-equiv" if cost is not None else ""
        return TurnResult(text=(result.get("result") or "").strip(), meta=meta, cost=cost, tokens=tokens)

    async def list_models(self, refresh: bool = False):
        return list(MODELS)

    def cached_models(self):
        return list(MODELS), True
