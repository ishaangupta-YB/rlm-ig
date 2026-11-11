"""State shared by every agent in one run."""

from __future__ import annotations

import asyncio
import itertools
import tempfile
import time
from pathlib import Path
from typing import Any

from ..budget import Ledger, Limits
from ..config import Config
from ..errors import ModelError
from ..models.base import Chat, Model, Reply
from ..sandbox import Sandbox, SandboxOptions
from ..schema import parse_json_reply, validate
from ..tools import Toolbox
from . import prompts
from .events import Events


class Run:
    """Models, budget, concurrency limits and bookkeeping for one run."""

    def __init__(
        self,
        config: Config,
        model: Model,
        sub_model: Model,
        *,
        toolbox: Toolbox | None = None,
        events: Events | None = None,
    ):
        self.config = config
        self.model = model
        self.sub_model = sub_model
        self.toolbox = toolbox or Toolbox()
        self.events = events or Events()
        self.ledger = Ledger(
            Limits(
                max_calls=config.max_calls,
                max_usd=config.max_usd,
                max_tokens=config.max_tokens,
                timeout=config.timeout,
            )
        )
        parallel = config.max_parallel or min(model.parallel, sub_model.parallel)
        self.parallel = max(1, parallel)
        # Requests in flight, and sub-agents alive and working. Two separate
        # pools: an agent gives its slot back while it waits on children, so a
        # deep tree can never starve itself.
        self.request_slots = asyncio.Semaphore(self.parallel)
        self.agent_slots = asyncio.Semaphore(self.parallel)
        self.ask_limit = config.ask_limit or sub_model.window_chars
        self.agents = 0
        self.steps = 0
        self._calls = itertools.count(1)
        self._workdir: tempfile.TemporaryDirectory | None = None
        self._warned_cost = False

    # ------------------------------------------------------------- sandboxes

    def new_sandbox(self, agent_id: str) -> Sandbox:
        base = self.config.sandbox
        options = SandboxOptions(**{**base.__dict__})
        if base.workdir is None:
            if self._workdir is None:
                self._workdir = tempfile.TemporaryDirectory(prefix="rlm-run-")
            options.workdir = str(Path(self._workdir.name) / f"agent-{agent_id}")
        return Sandbox(options, name=agent_id)

    def cleanup(self) -> None:
        if self._workdir is not None:
            self._workdir.cleanup()
            self._workdir = None

    # ----------------------------------------------------------- model calls

    def next_call_id(self) -> int:
        return next(self._calls)

    async def turn(self, chat: Chat, model: Model, message: str, *, agent: str, depth: int, step: int) -> Reply:
        """One agent turn: reserve budget, wait for a slot, send, account."""
        self.ledger.reserve(model.spec)
        started = time.monotonic()
        async with self.request_slots:
            waited = time.monotonic() - started
            reply = await chat.send(message)
        self._account(reply, model)
        self.steps += 1
        self.events.emit(
            "model.reply",
            agent,
            depth,
            step=step,
            text=reply.text,
            seconds=round(time.monotonic() - started - waited, 3),
            usage=reply.usage.to_dict(),
            model=reply.model or model.spec,
        )
        return reply

    async def ask(self, prompt: str, data: str | None, schema: dict | None) -> Any:
        """A single-shot sub-model call, with JSON parsing and one repair try."""
        model = self.sub_model
        text = prompts.ask_prompt(prompt, data)
        if len(text) > self.ask_limit:
            raise ValueError(
                f"ask() input is {len(text):,} characters; one call handles about {self.ask_limit:,}. "
                "Split it with chunk() and use ask_map(), or hand it to delegate()."
            )
        system = prompts.ask_system(schema)
        reply = await self._complete(model, system, text)
        if not schema:
            return reply.text.strip()
        problems, value = _parse(reply.text, schema)
        if problems:
            reply = await self._complete(model, system, text + "\n\n" + prompts.ask_repair(reply.text, problems))
            problems, value = _parse(reply.text, schema)
            if problems:
                raise ModelError("the sub-model did not return valid JSON for the schema: " + "; ".join(problems[:3]))
        return value

    async def _complete(self, model: Model, system: str, prompt: str) -> Reply:
        self.ledger.reserve(model.spec)
        async with self.request_slots:
            reply = await model.complete(system, prompt)
        self._account(reply, model)
        return reply

    def _account(self, reply: Reply, model: Model) -> None:
        self.ledger.record(reply.usage, model.spec)
        if reply.usage.cost_usd is None and self.config.max_usd is not None and not self._warned_cost:
            self._warned_cost = True
            self.events.emit(
                "note",
                message=(
                    f"{model.spec} does not report cost, so max_usd cannot be enforced for it; "
                    "max_calls and max_tokens still apply (add a [pricing] entry to estimate cost)"
                ),
            )


def _parse(text: str, schema: dict) -> tuple[list[str], Any]:
    try:
        value = parse_json_reply(text)
    except ValueError as exc:
        return [str(exc)], None
    return validate(value, schema), value
