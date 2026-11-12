"""Cost estimates for backends that report tokens but not money.

Prices are USD per million tokens. The built-in table covers current Claude
models and will age; anything in the ``[pricing]`` section of the config wins
over it, and a model found in neither simply has no cost estimate (token and
call limits still apply).
"""

from __future__ import annotations

# model id -> (input, output, cache read or None for the 0.1x default)
BUILTIN: dict[str, tuple[float, float, float | None]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, None),
    "claude-fable-5": (10.0, 50.0, None),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, None),
    "claude-opus-4-8": (5.0, 25.0, None),
    "claude-opus-4-7": (5.0, 25.0, None),
    "claude-opus-4-6": (5.0, 25.0, None),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, None),
    "claude-sonnet-4-6": (3.0, 15.0, None),
    "claude-haiku-5-5": (0.10, 0.50, None),
    "claude-haiku-4-5": (1.0, 5.0, None),
}

CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def lookup(model: str, table: dict[str, dict[str, float]] | None) -> tuple[float, float, float] | None:
    """(input, output, cached-input) prices for ``model``, if known."""
    name = model.split("/")[-1]
    for key in (model, name):
        entry = (table or {}).get(key)
        if entry and "input" in entry and "output" in entry:
            price_in, price_out = float(entry["input"]), float(entry["output"])
            return price_in, price_out, float(entry.get("cached", price_in * CACHE_READ_FACTOR))
    for key in (model, name):
        if key in BUILTIN:
            price_in, price_out, cached = BUILTIN[key]
            return price_in, price_out, price_in * CACHE_READ_FACTOR if cached is None else cached
    return None


def estimate(
    model: str,
    table: dict[str, dict[str, float]] | None,
    *,
    fresh_input: int,
    output: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float | None:
    """Estimated USD for one call, or ``None`` when the model has no price."""
    prices = lookup(model, table)
    if prices is None:
        return None
    price_in, price_out, price_cached = prices
    total = (
        fresh_input * price_in
        + cache_write * price_in * CACHE_WRITE_FACTOR
        + cache_read * price_cached
        + output * price_out
    )
    return total / 1_000_000
