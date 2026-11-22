"""Antigravity adapter (`agy --output-format stream-json --print=...`)."""
from __future__ import annotations

import asyncio
import json
import time

from .. import config
from .base import (Adapter, AgentError, ConfigError, TurnContext, TurnResult, UsageLimitError,
                   describe_tool, looks_like_bad_model, looks_like_limit, parse_reset, stream_process)

PERM_FLAGS = {
    "none": ["--mode", "plan"],
    "edits": ["--mode", "accept-edits"],
    "auto": ["--mode", "auto"],
}
CACHE_TTL = 24 * 3600
QUIET_STEPS = {"user_input", "system_message", "ephemeral_message", "checkpoint", "planner_response"}


class AgyAdapter(Adapter):
    name = "agy"
    label = "Antigravity"
    executable = "agy"

    def build_cmd(self, agent: dict, prompt: str, ctx: TurnContext) -> list[str]:
        cmd = ["agy", "--output-format", "stream-json"] + PERM_FLAGS[ctx.perms]
        if agent.get("started") and agent.get("session") and not ctx.ephemeral:
            cmd += ["--conversation", agent["session"]]
        if agent.get("model"):
            cmd += ["--model", agent["model"]]
        if agent.get("effort"):
            cmd += ["--effort", agent["effort"]]
        # Attached form: `-p` would swallow the next token, so the prompt can never
        # be confused with a flag this way.
        return cmd + [f"--print={prompt}"]

    async def send(self, agent, prompt, ctx, on_text, on_tool) -> TurnResult:
        res, streamed = await self._send_once(agent, prompt, ctx, on_text, on_tool)
        if res.text or ctx.ephemeral:
            return res
        if streamed.strip():  # the final 'response' field can be empty even when text was streamed
            res.text = streamed.strip()
            return res
        # It used tools and then stopped without a message. Ask for the reply in the same
        # conversation (keeps its work) instead of redoing the whole turn.
        on_text("")
        nudge = "You ended your turn without writing a message. Write your reply to your partner now."
        res2, streamed2 = await self._send_once(agent, nudge, ctx, on_text, on_tool)
        res2.text = res2.text or streamed2.strip()
        return res2

    async def _send_once(self, agent, prompt, ctx, on_text, on_tool) -> tuple[TurnResult, str]:
        result: dict | None = None
        seen_steps: set[int] = set()
        streamed: list[str] = []
        last_text_step: int | None = None
        junk: list[str] = []

        def on_line(line: str) -> None:
            nonlocal result, last_text_step
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                junk.append(line)
                return
            ev = d.get("event")
            if ev == "step_update":
                st = d.get("step_update") or {}
                idx, kind = st.get("step_index"), st.get("step_type", "")
                if kind == "agent_response":
                    if st.get("text_delta"):
                        streamed.append(st["text_delta"])
                        if last_text_step is not None and idx != last_text_step:
                            on_text("\n\n")
                        last_text_step = idx
                        on_text(st["text_delta"])
                elif kind and kind not in QUIET_STEPS and idx not in seen_steps:
                    seen_steps.add(idx)
                    info = st.get("tool_info") or {}
                    on_tool(describe_tool(st.get("tool_name") or info.get("name") or kind,
                                          info.get("parameters"), ctx.workdir))
            elif ev == "result":
                result = d.get("result") or {}

        code, err = await stream_process(self.build_cmd(agent, prompt, ctx), ctx.workdir, ctx.timeout,
                                         on_line, ctx.idle_timeout)
        if result is None or result.get("status") != "SUCCESS":
            detail = (json.dumps(result)[:800] if result else "") or err[-1500:] or "\n".join(junk[-5:])
            if looks_like_bad_model(detail):
                model = agent.get("model") or "default"
                raise ConfigError(f"Antigravity doesn't recognise the model \"{model}\". "
                                  f"Pick a valid one with /model agy (or --agy-model <id>).")
            if looks_like_limit(detail):
                raise UsageLimitError(f"Antigravity quota/limit reached: {detail[:300]}", parse_reset(detail))
            raise AgentError(f"agy failed (exit {code}): {detail}")
        if ctx.ephemeral:
            tokens = (result.get("usage") or {}).get("total_tokens")
            return TurnResult(text=(result.get("response") or "").strip(), meta=f"{tokens} tok" if tokens else "",
                              tokens=tokens), "".join(streamed)
        if not result.get("conversation_id"):
            raise AgentError("agy returned no conversation_id; cannot keep context")
        agent["session"] = result["conversation_id"]
        agent["started"] = True
        tokens = (result.get("usage") or {}).get("total_tokens")
        return TurnResult(text=(result.get("response") or "").strip(),
                          meta=f"{tokens} tok" if tokens else "", tokens=tokens), "".join(streamed)

    def _cache_file(self):
        return config.cache_dir() / "agy_models.json"

    def cached_models(self):
        """Whatever is on disk, even if old (shown instantly; refreshed in the background)."""
        try:
            data = json.loads(self._cache_file().read_text())
            models = [tuple(m) for m in data["models"]]
            return models, time.time() - data["at"] < CACHE_TTL
        except (OSError, ValueError, KeyError, TypeError):
            return [], False

    async def list_models(self, refresh: bool = False):
        if not refresh:
            models, fresh = self.cached_models()
            if models and fresh:
                return models
        try:
            proc = await asyncio.create_subprocess_exec(
                "agy", "models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except (FileNotFoundError, TimeoutError):
            return self.cached_models()[0]
        models = []
        for line in out.decode(errors="replace").splitlines():
            if "\t" in line:
                mid, label = line.split("\t", 1)
                models.append((mid.strip(), label.strip()))
        if models:
            cache = self._cache_file()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"at": time.time(), "models": models}))
            return models
        return self.cached_models()[0]
