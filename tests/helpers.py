from __future__ import annotations

import asyncio
import textwrap


def block(code: str, note: str = "Next step.") -> str:
    """A model reply: a line of prose and one fenced Python block."""
    return f"{note}\n```python\n{textwrap.dedent(code).strip()}\n```"


def run_async(coro):
    return asyncio.run(coro)
