"""Usage accounting and run-wide limits."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .errors import BudgetExceeded


@dataclass
class Usage:
    """Token and cost totals.

    ``input_tokens`` counts every prompt token, cached or not, so the number
    means the same thing on every backend. ``cached_tokens`` is the subset
    that was served from a provider cache. ``cost_usd`` stays ``None`` until
    some backend reports or can estimate a price.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.calls += other.calls
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self, *, calls: bool = True) -> dict:
        out: dict = {"calls": self.calls} if calls else {}
        return {
            **out,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
        }

    def __str__(self) -> str:
        cost = "cost n/a" if self.cost_usd is None else f"${self.cost_usd:.4f}"
        cached = f" ({_short(self.cached_tokens)} cached)" if self.cached_tokens else ""
        return (
            f"{self.calls} call{'s' if self.calls != 1 else ''} · "
            f"{_short(self.input_tokens)} in{cached} / {_short(self.output_tokens)} out · {cost}"
        )


def _short(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


@dataclass
class Limits:
    """Ceilings for one run. ``None`` disables a limit."""

    max_calls: int | None = 300
    max_usd: float | None = 5.0
    max_tokens: int | None = None
    timeout: float | None = None


@dataclass
class Ledger:
    """Tracks spend for a run and refuses new model calls past a limit.

    A call is counted when it is *reserved*, before the request is made, so
    concurrent agents cannot all slip under the ceiling together.
    """

    limits: Limits = field(default_factory=Limits)
    usage: Usage = field(default_factory=Usage)
    by_model: dict[str, Usage] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    exhausted: str | None = None
    _grace: int = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def reason(self) -> str | None:
        """Why no further call may start, or ``None`` if one may."""
        if self.exhausted:
            return self.exhausted
        lim, use = self.limits, self.usage
        if lim.max_calls is not None and use.calls >= lim.max_calls:
            return f"call limit reached ({use.calls} of {lim.max_calls} model calls)"
        if lim.max_usd is not None and use.cost_usd is not None and use.cost_usd >= lim.max_usd:
            return f"cost limit reached (${use.cost_usd:.4f} of ${lim.max_usd:.2f})"
        if lim.max_tokens is not None and use.total_tokens >= lim.max_tokens:
            return f"token limit reached ({use.total_tokens:,} of {lim.max_tokens:,})"
        if lim.timeout is not None and self.elapsed >= lim.timeout:
            return f"time limit reached ({self.elapsed:.0f}s of {lim.timeout:g}s)"
        return None

    def reserve(self, model: str = "") -> None:
        """Claim one model call or raise :class:`BudgetExceeded`."""
        why = self.reason()
        if why:
            self.exhausted = why
            if self._grace <= 0:
                raise BudgetExceeded(why)
            self._grace -= 1
        self.usage.calls += 1
        self.by_model.setdefault(model, Usage()).calls += 1

    def record(self, usage: Usage, model: str = "") -> None:
        """Add the tokens and cost of a finished call (the call itself was counted at reserve)."""
        for bucket in (self.usage, self.by_model.setdefault(model, Usage())):
            bucket.input_tokens += usage.input_tokens
            bucket.output_tokens += usage.output_tokens
            bucket.cached_tokens += usage.cached_tokens
            if usage.cost_usd is not None:
                bucket.cost_usd = (bucket.cost_usd or 0.0) + usage.cost_usd

    def allow_grace(self, calls: int = 1) -> None:
        """Permit a few calls past the limit so the root agent can wrap up."""
        self._grace = calls
