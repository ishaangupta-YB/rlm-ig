"""Run events: the single stream that the console, traces and callbacks consume."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Sink = Callable[["Event"], None]

# Event kinds, in the order a run produces them:
#   run.start     task, model, sub_model, summary, limits
#   agent.start   task, context_size, parent
#   model.reply   step, text, seconds, usage, model
#   cell          step, code, output, error, seconds, submitted
#   call.start    call, op, name, n, chars
#   call.end      call, op, name, n, seconds, ok, error, failed
#   agent.end     status, steps, answer, error
#   note          message
#   run.end       status, answer, usage, seconds, error


@dataclass
class Event:
    kind: str
    agent: str = ""
    depth: int = 0
    t: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "agent": self.agent, "depth": self.depth, "t": round(self.t, 3), **self.data}

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class Events:
    """Fan one event out to every sink. A failing sink never breaks a run."""

    def __init__(self, sinks: list[Sink] | None = None):
        self.sinks: list[Sink] = list(sinks or [])
        self.started = time.monotonic()
        self._complained: set[int] = set()

    def emit(self, kind: str, agent: str = "", depth: int = 0, **data: Any) -> None:
        event = Event(kind, agent, depth, time.monotonic() - self.started, data)
        for sink in self.sinks:
            try:
                sink(event)
            except Exception as exc:  # noqa: BLE001
                if id(sink) not in self._complained:
                    self._complained.add(id(sink))
                    print(f"rlm: an event handler failed ({type(exc).__name__}: {exc}); continuing", file=sys.stderr)
