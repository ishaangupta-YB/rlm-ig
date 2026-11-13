"""An offline demonstration: the real engine driven by a scripted "model".

Nothing here calls a language model. A small script plays the model's part so
you can watch the loop (REPL cells, a parallel fan-out, a sub-agent, the
answer) and confirm an installation works before any credentials exist.
"""

from __future__ import annotations

import json
import random
import re

from .models.scripted import Scripted, Turn

TASK = "Which service had the most payment failures in this log, and what was the most common failure reason for it?"

SERVICES = ["checkout", "billing", "ledger", "invoices", "refunds"]
REASONS = ["card_declined", "gateway_timeout", "insufficient_funds", "fraud_hold"]


def make_log(lines: int = 40_000, seed: int = 11) -> str:
    """A synthetic service log with a planted answer (billing, gateway_timeout)."""
    rng = random.Random(seed)
    rows = []
    for i in range(lines):
        service = rng.choice(SERVICES)
        stamp = f"2026-03-{1 + i * 28 // lines:02d}T{rng.randrange(24):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}Z"
        if rng.random() < (0.06 if service == "billing" else 0.02):
            weights = [1, 6, 1, 1] if service == "billing" else [3, 1, 2, 1]
            reason = rng.choices(REASONS, weights)[0]
            rows.append(f"{stamp} {service} ERROR payment failed reason={reason} order={rng.randrange(10**6)}")
        else:
            rows.append(f"{stamp} {service} INFO request ok latency_ms={rng.randrange(5, 900)}")
    return "\n".join(rows)


def _reply(note: str, code: str) -> str:
    return f"{note}\n\n```python\n{code.strip()}\n```"


def _script(turn: Turn) -> str:
    if turn.oneshot:
        # Plays the sub-model: count failure reasons in the slice it was given.
        data = re.search(r"<data>\n(.*)\n</data>", turn.last, re.S)
        counts: dict[str, int] = {}
        for reason in re.findall(r"reason=(\w+)", data.group(1) if data else ""):
            counts[reason] = counts.get(reason, 0) + 1
        return json.dumps(counts)
    if "Double-check" in turn.first:
        # Plays a sub-agent: it has its own REPL and its own `context`.
        return _reply(
            "I only need to count the reason fields in my slice.",
            "import collections\n"
            "reasons = collections.Counter(line.split('reason=')[1].split()[0] for line in context)\n"
            "submit(reasons.most_common(1)[0][0])",
        )
    steps = [
        _reply("First a look at the size and shape of the data.", "peek()\nprint(context[:300])"),
        _reply(
            "Lines are `timestamp service LEVEL message`. Counting failures per service is exact work, so code does it.",
            "import collections\n"
            "failed = [line for line in context.splitlines() if 'payment failed' in line]\n"
            "per_service = collections.Counter(line.split()[1] for line in failed)\n"
            "print(len(failed), per_service.most_common())",
        ),
        _reply(
            "Now the reasons for the worst service. I fan the lines out to sub-models in parallel and merge the tallies.",
            "worst = per_service.most_common(1)[0][0]\n"
            "lines = [line for line in failed if line.split()[1] == worst]\n"
            "parts = chunk(lines, size=30000)\n"
            "schema = {'type': 'object', 'additionalProperties': {'type': 'integer'}}\n"
            "tallies = ask_map('Count how often each failure reason occurs. Return {reason: count}.', parts, schema=schema)\n"
            "totals = collections.Counter()\n"
            "for tally in tallies:\n"
            "    totals.update(tally)\n"
            "print(worst, totals.most_common())",
        ),
        _reply(
            "A sub-agent double-checks the top reason independently, on a sample of the lines.",
            "check = delegate('Double-check: which failure reason is most common in these lines? Reply with its name.', lines[::2])\n"
            "print(check, totals.most_common(1)[0][0])",
        ),
        _reply(
            "Both agree, so I can answer.",
            "top_reason, count = totals.most_common(1)[0]\n"
            "submit({'service': worst, 'failures': per_service[worst], 'top_reason': top_reason, 'top_reason_count': count})",
        ),
    ]
    return steps[turn.index]


def demo_model(delay: float = 0.15) -> Scripted:
    return Scripted(_script, delay=delay, name="demo")
