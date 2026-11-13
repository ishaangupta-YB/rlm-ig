# Concepts

## The idea

A language model reads a fixed amount of text, and answers get worse as the text gets long. A recursive language model does not read the data at all at first. The data is loaded into a Python REPL as the variable `context`. The model sees only a short description of it, writes code to look at it, and sees what the code printed. Code does the exact work (counting, filtering, parsing). Smaller model calls do the reading work on pieces the code selected. When a piece is still too big, the model can start a sub-agent with its own REPL.

The idea comes from research on recursive language models by Zhang, Kraska and Khattab (MIT, 2025). This page describes how rlm-ig does it.

## A run, step by step

1. **Start.** rlm-ig starts a REPL worker, a Python subprocess, and loads `context` into it. `context` can be a string, a dict, a list, or any picklable object. It also starts a model conversation with a fixed system prompt.
2. **First message.** The model gets one message with the task, a short structural summary of `context` (never the data itself), the list of host tools, the answer format if you gave a schema, and the limits.
3. **Turn.** The model replies with a sentence or two and one fenced `python` block.
4. **Run.** The block runs in the REPL. Variables, imports and functions persist from turn to turn.
5. **Observe.** The model gets back what the code printed, the value of the last expression (as in a notebook), and any traceback, followed by a footer such as `[turn 2 of 20 · 0.0s]`.
6. **Repeat** from step 3 until the code calls `submit(value)`. That value is the answer.

Inside a block the model can call `ask` and `ask_map` (one-shot sub-model calls), `delegate` and `delegate_map` (sub-agents), and host tools you supply. See the [REPL API](repl-api.md).

## What the model sees

### The system prompt

One fixed system prompt is shared by the root agent and every sub-agent. Everything that varies goes in the first user message instead, so providers that cache prompt prefixes can reuse the same cached prompt across the whole agent tree. The prompt tells the model:

- It drives a Python REPL and cannot see the data directly.
- Reply with a short note and exactly one fenced `python` block. It has no other tools, whatever a surrounding environment note might suggest.
- State persists. Long output is cut in the middle, so print selectively.
- The functions available in the REPL, and that they are synchronous (no `await`).
- How to work: look first, let code do exact work, use models for reading, give sub-models everything they need because they know nothing else, check results before trusting them, submit the variable that holds the answer.

### The first message

For a dict with two keys, a tool, and `schema=int`, the first message is (real output):

```text
## Task

How many users are in the export?

## Data

context: dict, 2 keys
  ['users'] (list, len 1,000): [{'id': 0, 'name': 'user0'}, {'id': 1, 'name': 'user1'}, {'id': 2, 'name': 'user2'}, {'id': 3, 'name': 'user3'}, {'id': 4, 'name': 'user4'},...[+745 chars]
  ['meta'] (dict, len 1): {'source': 'export'}

## Tools

Available in the REPL in addition to the built-ins:

def price(item: str) -> float
    Unit price of an item.

## Answer format

The value you submit must satisfy this JSON Schema. It is checked when you call `submit`; if it fails you get the errors back and can fix the value and submit again.

{
 "type": "integer"
}

## Limits

- 20 turns at most.
- Output shown per turn: 8,000 characters.
- One `ask` handles about 400,000 characters of prompt plus data.
```

The Tools and Answer format sections appear only when there are tools or a schema. In a [session](sessions.md), an "Earlier in this session" section appears too. A sub-agent's Limits section also says which level it is on, and that `delegate` is unavailable at the deepest level.

### Each turn after that

The next message is the cell's output plus the footer:

```text
context: dict, 2 keys
  ['users'] (list, len 1,000): ...
{'id': 0, 'name': 'user0'}

[turn 1 of 20 · 0.0s]
```

When a cell prints nothing, the message says `(no output; print what you need to see)`. Near the end the footer adds `N turns left: finish up and call submit().`, and on the last turn it says `No turns left after this.`

### Truncation

Output is bounded twice.

- In the worker, a cell's captured output keeps the first and last 200,000 characters (`sandbox.capture_limit`) and drops the middle with a `[N characters omitted]` marker.
- Before it goes to the model, the text is cut to `output_limit` characters (default 8,000). The cut keeps about 60% from the start and 40% from the end and says `... [N characters omitted; print a slice or filter instead of the whole value] ...`.

The value of the last expression is shown with a compact `repr`: long lists show their first 30 items and long strings show their start and end. The model is expected to print what it needs.

### What counts as a reply

The engine takes the fenced block tagged `python` (also `py`, `python3`, `repl`, `ipython`, `pycon`). If a reply has several tagged blocks they are joined in order. If none is tagged, untagged blocks that parse as Python are used. An unterminated final block is kept, because a cut-off reply is common. As a last resort, if a reply has exactly one closing fence and the lines above it parse as Python once a stray opening tag (such as `<invoke ...`, left over from native tool-call habits) is stripped, that code is used.

A reply with no usable block gets a message saying nothing ran, and the turn is spent. If the model answers in prose twice in a row, the engine accepts the latest prose as the answer and records a `note` event. Prose is never accepted when you gave a `schema`.

If the cell raises, the model sees the traceback and the variables are intact. If `submit(value)` is called with a value that fails the schema, the model gets the validation errors and can submit again.

## Sub-calls and sub-agents

| | `ask` / `ask_map` | `delegate` / `delegate_map` |
| --- | --- | --- |
| What it is | One request to the sub-model. | A new agent with its own REPL and its own turn loop. |
| What it sees | Only the prompt and the data you pass. | Only its task and `data`, which becomes its `context`. |
| What it can do | Read and answer. No code, no state. | Explore with code, call `ask`, call tools, delegate further. |
| Returns | Text, or parsed JSON when you pass a schema. | Whatever the sub-agent passes to `submit`. |
| Cost | One model call. | Several: one per turn, plus any calls it makes. |

Use `ask` or `ask_map` when the answer is a function of a piece of text you can hand over: classify this record, extract these fields from this chunk, summarise this paragraph. Narrow the text with code first, split large text with `chunk`, and fan out with `ask_map`, which also packs small items into shared calls (see [REPL API](repl-api.md#batching)). Use `delegate` when a piece is still too large for one call, or needs looking at before it can be answered, or needs several steps and its own variables. Sub-agents are the expensive choice. A sub-agent that is asked to do what one `ask` could do wastes turns.

Neither kind of sub-model knows anything about the parent conversation or its variables. The task has to say what the data is and what form of answer is wanted.

The root agent uses `model`. Sub-agents and every `ask` use `sub_model`, which defaults to the same model. A cheaper `sub_model` is the usual way to cut cost.

### Depth

`max_depth` (default 2) is how many levels of sub-agents may exist below the root. The root is level 0. At the deepest level `delegate` raises `DelegationRefused`, and that agent's Limits section says so. Sub-agents can still use `ask` and `ask_map` there.

### Parallelism

`ask_map` and `delegate_map` run their items concurrently. The number of simultaneous model requests, and separately of live sub-agents, is `max_parallel`, or a default that depends on the backend. An agent that is only waiting on its children gives its slot back, so a deep tree cannot starve itself.

### The pass-through guard

If a parent holds at least 20,000 characters of context and calls `delegate` with data that is 80% or more of that, the call is refused with a message that the work is only moving down a level. The model is told to narrow the data first, or to pass `whole=True` if one pass over everything is really what it wants. The guard applies to `delegate`, not to `delegate_map`. Turn it off with `passthrough_guard = false`.

## The forced wrap-up turn and `partial`

Every agent gets `max_steps` turns (default 20). If it has not submitted by then, it gets one more turn, in which the message ends with an instruction to call `submit(...)` with the best answer it can build from the variables it already has and not to start new work. That is a request, not a restriction: the cell runs normally, and if it does not submit, the agent fails with `StepsExhausted`. A prose reply in that turn is accepted as the answer, unless you gave a schema.

When the root agent's answer comes from a forced wrap-up, the run's status is `partial` and `Result.note` says why:

```text
answer produced by a forced wrap-up (out of turns)
```

The same happens when a budget is spent (below), with the note `answer produced by a forced wrap-up (budget spent: call limit reached (4 of 4 model calls))` for a call limit of 4. A `partial` answer is real output built from real work, but the model was cut short, so check it. A sub-agent that is forced to wrap up returns its value to its parent normally. The `agent.end` event in the trace has status `partial` for it, but `Result.status` reflects only the root.

## Budgets

These limits apply to the whole run, all agents together:

| Option | Default | Counts |
| --- | --- | --- |
| `max_calls` | 300 | Model calls: every agent turn, every `ask`, every call `ask_map` makes (one per batch of small items), and every repair retry. |
| `max_usd` | 5.0 | Cost, when the backend reports or can estimate one. |
| `max_tokens` | none | Input plus output tokens. |
| `timeout` | none | Wall-clock seconds. |

A call is counted when it is reserved, before the request, so parallel agents cannot all slip under the ceiling together. Once any limit is reached:

- New `ask`, `ask_map` and `delegate` calls raise `BudgetExceeded` inside the REPL. The model can catch it, since it is a `SubcallError`.
- A sub-agent whose own turn hits the limit fails, and its parent sees the exception.
- When the root agent's next turn hits the limit, the root is told that the budget is spent and gets one final turn to submit. The result is `partial`, and the note names the limit. The wrap-up call is allowed one call past the limit, so `usage.calls` can be one more than `max_calls`.

A single cell is limited separately by `cell_timeout` (default 120 seconds). Time spent waiting on sub-calls does not count against it. `timeout` is checked between model calls, so one long cell can overshoot it. A hard stop at `timeout * 1.5 + 30` seconds ends the run as `failed`.

If a backend does not report cost, `max_usd` cannot be enforced for it. rlm-ig emits a `note` saying so. `max_calls` and `max_tokens` still apply, and a `[pricing]` entry in the config lets rlm-ig estimate cost. See [Configuration](configuration.md).

## An annotated transcript

This is `rlm demo -v`, shortened only where noted. The data is a synthetic service log of 2,432,029 characters and 40,000 lines. A script plays the model, so the replies are canned, but everything else is the real engine. The task:

> Which service had the most payment failures in this log, and what was the most common failure reason for it?

```text
rlm  scripted:demo  · context: str, 2,432,029 chars
root   ▸ turn 1  0.2s  First a look at the size and shape of the data.
root     │ peek()
root     │ print(context[:300])
root     └ ran 0.00s, 1,283 chars of output
root     │ context: str, 2,432,029 chars, 40,000 lines
root     │   starts: '2026-03-01T17:54:59Z invoices INFO request ok latency_ms=467\n2026-03-01T18:12:11Z refunds ...' (shortened)
root     │   ends:   '2026-03-28T09:24:24Z ledger INFO request ok latency_ms=654\n2026-03-28T01:16:14Z ledger ...' (shortened)
root     │ 2026-03-01T17:54:59Z invoices INFO request ok latency_ms=467
root     │ 2026-03-01T18:12:11Z refunds INFO request ok latency_ms=492
root     │ ... (three more log lines)
```

**Turn 1: look first.** The model never loads the 2.4 million characters. `peek()` prints a structural summary and `print(context[:300])` shows the first lines. The whole turn costs about 1,300 characters of output.

```text
root   ▸ turn 2  0.2s  Lines are `timestamp service LEVEL message`. Counting failures per service is exact work, so co…
root     │ import collections
root     │ failed = [line for line in context.splitlines() if 'payment failed' in line]
root     │ per_service = collections.Counter(line.split()[1] for line in failed)
root     │ print(len(failed), per_service.most_common())
root     └ ran 0.01s, 96 chars of output
root     │ 1125 [('billing', 492), ('invoices', 171), ('ledger', 161), ('checkout', 160), ('refunds', 141)]
```

**Turn 2: code for exact work.** Counting over 40,000 lines is free and exact in Python. No model call is made. `failed` and `per_service` stay defined for later turns.

```text
root   ▸ turn 3  0.2s  Now the reasons for the worst service. I fan the lines out to sub-models in parallel and merge …
root     │ worst = per_service.most_common(1)[0][0]
root     │ lines = [line for line in failed if line.split()[1] == worst]
root     │ parts = chunk(lines, size=30000)
root     │ schema = {'type': 'object', 'additionalProperties': {'type': 'integer'}}
root     │ tallies = ask_map('Count how often each failure reason occurs. Return {reason: count}.', parts, schema=schema)
root     │ totals = collections.Counter()
root     │ for tally in tallies:
root     │     totals.update(tally)
root     │ print(worst, totals.most_common())
root     ⇉ ask_map ×2  43,584 chars
root     ⇇ ask_map done in 0.2s
root     └ ran 0.16s, 105 chars of output
root     │ billing [('gateway_timeout', 334), ('fraud_hold', 59), ('card_declined', 57), ('insufficient_funds', 42)]
```

**Turn 3: sub-calls.** Code narrows the log to the 492 billing failures. `chunk` splits that list into two pieces of at most 30,000 characters. `ask_map` sends both to the sub-model in parallel and gets two parsed dicts back, because a JSON Schema was passed. Each piece is large, so each gets its own call. Small items would be packed together. Code merges the tallies. The `⇉` and `⇇` lines are the `call.start` and `call.end` events: two items, 43,584 characters in total.

```text
root   ▸ turn 4  0.2s  A sub-agent double-checks the top reason independently, on a sample of the lines.
root     │ check = delegate('Double-check: which failure reason is most common in these lines? Reply with its name.', lines[::2])
root     │ print(check, totals.most_common(1)[0][0])
root     ⇉ delegate  Double-check: which failure reason is most common in these lines? Reply with it…
  1      ◆ sub-agent Double-check: which failure reason is most common in these lines? Rep… (21,735 chars)
  1      ▸ turn 1  0.2s  I only need to count the reason fields in my slice.
  1        │ import collections
  1        │ reasons = collections.Counter(line.split('reason=')[1].split()[0] for line in context)
  1        │ submit(reasons.most_common(1)[0][0])
  1        ✔ submitted
  1      ✔ gateway_timeout
root     ⇇ delegate done in 0.2s
root     └ ran 0.23s, 31 chars of output
root     │ gateway_timeout gateway_timeout
```

**Turn 4: a sub-agent.** `delegate` starts agent `1` (its id is `0.1` in the trace, shown as `1` in the console) with half of the billing lines as its own `context` of 21,735 characters. It has its own REPL and its own turn loop, and here it finishes in one turn. Its answer comes back as the return value of `delegate`. Its lines are indented and prefixed with its id.

```text
root   ▸ turn 5  0.1s  Both agree, so I can answer.
root     │ top_reason, count = totals.most_common(1)[0]
root     │ submit({'service': worst, 'failures': per_service[worst], 'top_reason': top_reason, 'top_reason_count': count})
root     ✔ submitted
✔ ok  1.3s · 8 calls · 21k in / 453 out · $0.0000
  trace: ~/.local/state/rlm-ig/traces/20261113-073205-which-service-had-the-most-payment-failu-2883.jsonl
{
  "service": "billing",
  "failures": 492,
  "top_reason": "gateway_timeout",
  "top_reason_count": 334
}
```

**Turn 5: submit.** The answer is built from variables, not retyped. The status is `ok`. The 8 calls are 5 root turns, 2 `ask_map` items, and 1 sub-agent turn. Token counts in the demo are estimates made by the scripted model, and the cost is zero because the script charges nothing.

You can replay any run later with `rlm trace <file> --full`. See [Tracing](tracing.md).
