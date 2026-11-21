"""Antigravity adapter (`agy --output-format stream-json --print=...`)."""
from __future__ import annotations

import asyncio
import json
import time

from .. import config
from .base import Adapter, AgentError, TurnContext, TurnResult, stream_process

PERM_FLAGS = {
    "none": ["--mode", "plan"],
    "edits": ["--mode", "accept-edits"],
    "auto": ["--mode", "auto"],
}
CACHE_TTL = 24 * 3600


class AgyAdapter(Adapter):
    name = "agy"
    label = "Antigravity"
    executable = "agy"

    def build_cmd(self, agent: dict, prompt: str, ctx: TurnContext) -> list[str]:
        cmd = ["agy", "--output-format", "stream-json"] + PERM_FLAGS[ctx.perms]
        if agent.get("started") and agent.get("session"):
            cmd += ["--conversation", agent["session"]]
        if agent.get("model"):
            cmd += ["--model", agent["model"]]
        if agent.get("effort"):
            cmd += ["--effort", agent["effort"]]
        # Attached form: `-p` would swallow the next token, so the prompt can never
        # be confused with a flag this way.
        return cmd + [f"--print={prompt}"]

    async def send(self, agent, prompt, ctx, on_text, on_tool) -> TurnResult:
        result: dict | None = None
        seen_steps: set[int] = set()
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
                        if last_text_step is not None and idx != last_text_step:
                            on_text("\n\n")
                        last_text_step = idx
                        on_text(st["text_delta"])
                elif kind and kind != "user_input" and idx not in seen_steps:
                    seen_steps.add(idx)
                    on_tool(kind)
            elif ev == "result":
                result = d.get("result") or {}

        code, err = await stream_process(self.build_cmd(agent, prompt, ctx), ctx.workdir, ctx.timeout, on_line)
        if result is None or result.get("status") != "SUCCESS":
            detail = (json.dumps(result)[:800] if result else "") or err[-1500:] or "\n".join(junk[-5:])
            raise AgentError(f"agy failed (exit {code}): {detail}")
        if not result.get("conversation_id"):
            raise AgentError("agy returned no conversation_id; cannot keep context")
        agent["session"] = result["conversation_id"]
        agent["started"] = True
        tokens = (result.get("usage") or {}).get("total_tokens")
        return TurnResult(text=(result.get("response") or "").strip(),
                          meta=f"{tokens} tok" if tokens else "", tokens=tokens)

    async def list_models(self, refresh: bool = False):
        cache = config.cache_dir() / "agy_models.json"
        if not refresh and cache.exists():
            try:
                data = json.loads(cache.read_text())
                if time.time() - data["at"] < CACHE_TTL and data["models"]:
                    return [tuple(m) for m in data["models"]]
            except (OSError, ValueError, KeyError):
                pass
        try:
            proc = await asyncio.create_subprocess_exec(
                "agy", "models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except (FileNotFoundError, TimeoutError):
            return []
        models = []
        for line in out.decode(errors="replace").splitlines():
            if "\t" in line:
                mid, label = line.split("\t", 1)
                models.append((mid.strip(), label.strip()))
        if models:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"at": time.time(), "models": models}))
        return models
