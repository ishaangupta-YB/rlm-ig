"""The model interface the engine talks to.

A :class:`Model` hands out :class:`Chat` objects. A chat is one multi-turn
conversation with a fixed system prompt; the engine sends one user message per
turn and gets one reply back. Whether the conversation history lives in this
process (HTTP APIs) or inside an external agent's session (Claude Code,
opencode) is the backend's business.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..budget import Usage
from ..text import clip_middle

TextSink = Callable[[str], None]


@dataclass
class Reply:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    stop: str | None = None


def replay_message(history: list[tuple[str, str]], text: str) -> str:
    """One message that re-creates a conversation in a fresh backend session.

    Used when an external agent's session is lost mid-run (it crashed, stalled,
    or was stopped because its output ran away). Over-long assistant turns are
    shortened; they are the usual reason a session had to be replaced.
    """
    parts = [
        "[This conversation was interrupted and is being resumed in a new session. The exchange so far is "
        "reproduced below. Continue from the last message exactly as if nothing had happened.]"
    ]
    for sent, received in history:
        parts.append(f"=== message to you ===\n{sent}")
        parts.append(f"=== your reply ===\n{clip_middle(received, 6000, hint='reply shortened')}")
    parts.append(f"=== message to you ===\n{text}")
    return "\n\n".join(parts)


class Chat(abc.ABC):
    """One conversation. Not safe for concurrent ``send`` calls."""

    @abc.abstractmethod
    async def send(self, text: str, *, on_text: TextSink | None = None) -> Reply:
        """Send one user message and wait for the assistant's reply."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release whatever the conversation holds (process, remote session)."""


class Model(abc.ABC):
    """A configured model backend."""

    #: canonical ``provider:model`` string
    spec: str = ""
    #: sensible number of simultaneous requests for this backend
    parallel: int = 8
    #: largest ``ask`` payload, in characters, this model should be handed
    window_chars: int = 400_000
    #: whether the provider reports (or we can estimate) cost
    reports_cost: bool = False

    @abc.abstractmethod
    def chat(self, system: str) -> Chat:
        """Start a conversation with ``system`` as its system prompt."""

    async def complete(self, system: str, prompt: str) -> Reply:
        """A single-turn request. Backends may override with a cheaper path."""
        chat = self.chat(system)
        try:
            return await chat.send(prompt)
        finally:
            await chat.close()

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release run-wide resources (servers, clients)."""

    def describe(self) -> dict[str, Any]:
        return {"spec": self.spec}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.spec}>"


class StatelessChat(Chat):
    """A chat for request/response APIs: history is kept here and re-sent.

    Subclasses implement :meth:`_request`. When the transcript grows past
    ``compact_at`` characters, the bodies of older REPL outputs are shortened
    in place. The first message (task and data summary) and the most recent
    exchanges are never touched, and trimming happens in strides so a provider
    prompt cache is invalidated rarely rather than on every turn.
    """

    keep_recent = 6

    def __init__(self, system: str, *, compact_at: int = 300_000):
        self.system = system
        self.messages: list[dict[str, str]] = []
        self.compact_at = compact_at

    @abc.abstractmethod
    async def _request(self, on_text: TextSink | None) -> Reply:
        """Call the API with ``self.system`` and ``self.messages``."""

    async def send(self, text: str, *, on_text: TextSink | None = None) -> Reply:
        self.messages.append({"role": "user", "content": text})
        self._compact()
        try:
            reply = await self._request(on_text)
        except BaseException:
            self.messages.pop()
            raise
        self.messages.append({"role": "assistant", "content": reply.text or "(empty reply)"})
        return reply

    def _size(self) -> int:
        return len(self.system) + sum(len(m["content"]) for m in self.messages)

    def _compact(self) -> None:
        if self._size() <= self.compact_at:
            return
        target = self.compact_at // 2
        stop = len(self.messages) - self.keep_recent
        for i in range(1, max(stop, 1)):
            msg = self.messages[i]
            if msg["role"] != "user" or len(msg["content"]) <= 700:
                continue
            body = msg["content"]
            msg["content"] = (
                f"{body[:400]}\n... [{len(body) - 600:,} characters of earlier output trimmed to save context] ...\n{body[-200:]}"
            )
            if self._size() <= target:
                return
