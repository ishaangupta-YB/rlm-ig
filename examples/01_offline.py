"""The whole loop with no model and no credentials.

A scripted stand-in plays the model, so this shows exactly what a run does:
REPL turns, a parallel fan-out to "sub-models", and a typed answer.

    python examples/01_offline.py
"""

import json
import re

import rlm_ig as rlm

log = "\n".join(f"req {i} status={'500' if i % 37 == 0 else '200'} ms={(i * 7) % 900}" for i in range(20_000))


def script(turn: rlm.models.Turn) -> str:
    if turn.oneshot:
        # A sub-model call from ask_map: answer for the batch of items it was given.
        items = re.findall(r'<item index="\d+">\n(.*?)\n</item>', turn.last, re.S)
        return json.dumps([str(item.count("status=500")) for item in items])
    replies = [
        "Look at the data first.\n```python\npeek()\n```",
        "Count failures per chunk in parallel, then add them up.\n```python\n"
        "counts = ask_map('How many lines have status=500? Reply with the number.', chunk(context, 4000))\n"
        "total = sum(int(c) for c in counts)\nprint(len(counts), total)\n```",
        "Cross-check with plain code and submit.\n```python\n"
        "assert total == context.count('status=500')\nsubmit({'failures': total})\n```",
    ]
    return replies[turn.index]


result = rlm.run("How many requests failed?", log, model=rlm.Scripted(script), schema={"type": "object"})
print(result.answer, "|", result.status, "|", result.usage)
