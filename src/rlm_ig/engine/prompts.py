"""Every piece of text the engine shows a model.

The system prompt is one fixed string shared by the root agent and all
sub-agents. Anything that varies per agent (task, data summary, tools, answer
format, limits) goes in the first user message instead, so providers that
cache prompt prefixes can reuse the same cached system prompt across the
whole agent tree.
"""

from __future__ import annotations

import json

from ..text import clip_middle

SYSTEM = """\
You solve tasks by driving a Python REPL. You do not see the task's data directly: it lives in the REPL as the variable `context`, and it can be far larger than anything you could read in one go. You write code, the code runs, and you see what it printed. Repeat until you can call `submit(answer)`.

## Turn format

Reply with a short note on what you are about to do (a sentence or two), then exactly one fenced block:

```python
# your code
```

Only that block runs. A reply without a block does nothing. After each block you get back everything it printed, the value of its last expression (as in a notebook), and any traceback. Long output is cut in the middle, so print selectively.

State persists between turns: variables, imports and functions stay defined. Never recompute something you already stored in a variable.

## What the REPL provides

- `context`: the data for this task (a str, dict or list; None when the task needs no data).
- `peek(x)`: print a structural summary of any value.
- `grep(pattern, text=context)`: regex search. Returns hits with `.start`, `.match` and `.snippet` (the match plus surrounding text).
- `chunk(text_or_list, size=20000, overlap=0)`: split into pieces on natural boundaries.
- `ask(prompt, data=None, schema=None)`: one call to a language model. It sees only `prompt` and `data`, nothing else. Returns its text, or parsed JSON when you pass a JSON Schema dict as `schema`.
- `ask_map(prompt, items, schema=None)`: the same prompt applied to every item, in parallel. Returns a list in input order. Use it instead of a loop of `ask` whenever there are several pieces; it is many times faster. `ask_map([prompt1, prompt2, ...])` runs different prompts in parallel.
- `delegate(task, data=None, schema=None)`: start a sub-agent with its own REPL, where `data` becomes its `context`. Use it when a piece is still too large or too intricate for one `ask`. `delegate_map(task, items)` runs one sub-agent per item in parallel.
- `submit(value)`: finish with `value` as the answer. Nothing after it runs.
- Tools listed in the task message are plain Python functions. Call them directly.

These are ordinary synchronous functions. Do not `await` them.

## How to work

1. Look first. Check the size and shape of `context` with `peek`, slices or `grep` before deciding on an approach. Never print all of a large value.
2. Let code do what code does exactly and for free: counting, filtering, parsing, sorting, joining, arithmetic, exact matching.
3. Use models for what needs reading: judging relevance, extracting meaning, summarising. Narrow the text with code first and send only the relevant part. Split anything large with `chunk` and fan out with `ask_map`.
4. Sub-models and sub-agents know nothing about this conversation or your variables. Put everything they need into the call: the question, what the data is, and the exact form of answer you want. Ask for short, structured answers (pass `schema`) so you can combine them with code.
5. Keep results in variables and build the answer from them. Check before trusting: print a sample, look for empty or failed entries (failed `*_map` items are falsy `Failed(...)` objects), and redo only the pieces that failed.
6. Submit the variable that holds the answer, `submit(result)`, not a retyped copy of it. If the data does not contain the answer, say so in what you submit instead of guessing.

Be economical with turns: several steps can share one block when they do not depend on seeing each other's output. Do not skip the looking.
"""

ASK_SYSTEM = """\
You are a component inside a larger program. Answer the request using only the material you are given. Reply with the answer itself: no preamble, no restating the question, no remarks about the task. If the material does not contain what is asked for, reply exactly: NOT FOUND (unless the request tells you to do something else in that case).
"""

ASK_JSON = """
Reply with a single JSON value that satisfies the JSON Schema below, and nothing else: no prose and no code fence.

{schema}
"""


def ask_system(schema: dict | None) -> str:
    if not schema:
        return ASK_SYSTEM
    return ASK_SYSTEM + ASK_JSON.format(schema=json.dumps(schema, indent=1))


def ask_prompt(prompt: str, data: str | None) -> str:
    if data is None:
        return prompt
    return f"{prompt}\n\n<data>\n{data}\n</data>"


def ask_repair(reply: str, problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "Your previous reply was rejected:\n"
        f"{listed}\n\n"
        f"Previous reply:\n{clip_middle(reply, 2000)}\n\n"
        "Reply again with only the corrected JSON value."
    )


def opening(
    *,
    task: str,
    summary: str,
    tools: str,
    schema: dict | None,
    steps: int,
    output_limit: int,
    depth: int,
    max_depth: int,
    ask_chars: int,
    notes: str = "",
    problems: list[str] | None = None,
) -> str:
    parts = [f"## Task\n\n{task.strip()}", f"## Data\n\n{summary}"]
    if notes:
        parts.append(f"## Earlier in this session\n\n{notes}")
    if tools:
        parts.append(f"## Tools\n\nAvailable in the REPL in addition to the built-ins:\n\n{tools}")
    if schema:
        parts.append(
            "## Answer format\n\n"
            "The value you submit must satisfy this JSON Schema. It is checked when you call `submit`; "
            "if it fails you get the errors back and can fix the value and submit again.\n\n"
            f"{json.dumps(schema, indent=1)}"
        )
    limits = [
        f"- {steps} turns at most.",
        f"- Output shown per turn: {output_limit:,} characters.",
        f"- One `ask` handles about {ask_chars:,} characters of prompt plus data.",
    ]
    if depth >= max_depth:
        limits.append("- `delegate` is not available here (deepest level). Use code, `ask` and `ask_map`.")
    elif depth > 0:
        limits.append(f"- You are a sub-agent (level {depth} of {max_depth}). `delegate` is available.")
    for problem in problems or []:
        limits.append(f"- Note: {problem}")
    parts.append("## Limits\n\n" + "\n".join(limits))
    return "\n\n".join(parts)


def _footer(step: int, steps: int, seconds: float) -> str:
    left = steps - step
    note = f"[turn {step} of {steps} · {seconds:.1f}s]"
    if left <= 0:
        note += " No turns left after this."
    elif left <= 3:
        note += f" {left} turn{'s' if left != 1 else ''} left: finish up and call submit()."
    return note


def observation(text: str, *, step: int, steps: int, seconds: float, limit: int) -> str:
    body = clip_middle(text, limit) if text.strip() else "(no output; print what you need to see)"
    return f"{body}\n\n{_footer(step, steps, seconds)}"


def no_code(step: int, steps: int) -> str:
    return (
        "No code block was found in your reply, so nothing ran. Reply with one ```python block. "
        "If you already have the answer, call submit(answer) inside it.\n\n" + _footer(step, steps, 0.0)
    )


def schema_feedback(problems: list[str], schema: dict, *, step: int, steps: int) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "submit() was rejected: the value does not match the required answer format.\n"
        f"{listed}\n\n"
        f"Required JSON Schema:\n{json.dumps(schema, indent=1)}\n\n"
        "Your variables are intact. Fix the value and call submit() again.\n\n" + _footer(step, steps, 0.0)
    )


LAST_CALL = (
    "\n\nYou are out of turns. Reply with one final ```python block that calls submit(...) with the best answer "
    "you can build from the variables you already have. Do not start new work."
)

BUDGET_SPENT = (
    "\n\nThe run's budget is spent ({reason}). No further sub-calls will succeed. Reply with one final "
    "```python block that calls submit(...) with the best answer you can build from the variables you already "
    "have, and say in the answer if it is incomplete."
)
