"""A model whose replies come from your own code.

Useful for tests, offline demos, and for exercising the engine without
spending anything: the whole loop (REPL, sub-calls, budgets, tracing) runs for
real, only the "intelligence" is supplied by a function or a list.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..budget import Usage
from ..errors import ModelError
from ..text import approx_tokens
from .base import Chat, Model, Reply, TextSink


@dataclass
class Turn:
    """What a scripted model is asked to respond to."""

    system: str
    messages: list[dict[str, str]]
    #: 0 for the first reply in this conversation, 1 for the second, ...
    index: int
    #: True for a single-shot ``ask`` call, False for an agent conversation
    oneshot: bool

    @property
    def last(self) -> str:
        """The newest user message."""
        return self.messages[-1]["content"]

    @property
    def first(self) -> str:
        """The first user message (for an agent: the task and data summary)."""
        return self.messages[0]["content"]


Script = Callable[[Turn], Any]


class Scripted(Model):
    """Replies produced by ``script(turn)``, or taken in order from a list.

    With a list, every conversation walks the list from the start, and
    single-shot calls use ``asks`` (a function of the prompt, a list consumed
    in order, or a fixed string).
    """

    parallel = 16
    reports_cost = True

    def __init__(
        self,
        script: Script | Sequence[str],
        *,
        asks: Callable[[str], Any] | Sequence[str] | str | None = None,
        cost_per_call: float = 0.0,
        delay: float = 0.0,
        name: str = "scripted",
    ):
        self.spec = f"scripted:{name}"
        self._script = script
        self._asks = asks
        self._ask_index = 0
        self.cost_per_call = cost_per_call
        self.delay = delay
        self.turns: list[Turn] = []

    def chat(self, system: str) -> Chat:
        return _ScriptedChat(self, system, oneshot=False)

    async def complete(self, system: str, prompt: str) -> Reply:
        return await _ScriptedChat(self, system, oneshot=True).send(prompt)

    async def _produce(self, turn: Turn) -> str:
        self.turns.append(turn)
        if self.delay:
            await asyncio.sleep(self.delay)
        if turn.oneshot and self._asks is not None:
            asks = self._asks
            if isinstance(asks, str):
                return asks
            if callable(asks):
                out = asks(turn.last)
            else:
                if self._ask_index >= len(asks):
                    raise ModelError("scripted model ran out of ask replies")
                out = asks[self._ask_index]
                self._ask_index += 1
        elif callable(self._script):
            out = self._script(turn)
        else:
            if turn.index >= len(self._script):
                raise ModelError(f"scripted model has no reply for turn {turn.index + 1}")
            out = self._script[turn.index]
        if inspect.isawaitable(out):
            out = await out
        if isinstance(out, BaseException):
            raise out
        return str(out)


class _ScriptedChat(Chat):
    def __init__(self, model: Scripted, system: str, *, oneshot: bool):
        self.model = model
        self.system = system
        self.oneshot = oneshot
        self.messages: list[dict[str, str]] = []
        self.index = 0

    async def send(self, text: str, *, on_text: TextSink | None = None) -> Reply:
        self.messages.append({"role": "user", "content": text})
        turn = Turn(self.system, list(self.messages), self.index, self.oneshot)
        self.index += 1
        out = await self.model._produce(turn)
        self.messages.append({"role": "assistant", "content": out})
        if on_text:
            on_text(out)
        prompt_chars = len(self.system) + sum(len(m["content"]) for m in self.messages[:-1])
        usage = Usage(
            input_tokens=approx_tokens(prompt_chars),
            output_tokens=approx_tokens(len(out)),
            cost_usd=self.model.cost_per_call,
        )
        return Reply(out, usage, model=self.model.spec, stop="end_turn")
