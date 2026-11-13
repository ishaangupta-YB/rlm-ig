# REPL API

This page is the reference for what exists inside the REPL: the names the model's code can use and how a cell behaves. It is written for people who build on rlm-ig, debug a trace, or want to know exactly what the model was told. The model itself gets a shorter version of this in its system prompt.

Every agent, root or sub-agent, has its own REPL worker with its own namespace.

## Names

| Name | What it is |
| --- | --- |
| `context` | The task's data. |
| `peek(value=None, name=None)` | Print a structural summary of a value. |
| `grep(pattern, text=None, window=120, limit=50, flags=re.IGNORECASE)` | Regex search with surrounding text. |
| `chunk(text, size=20000, overlap=0, sep=None)` | Split a string or a list into pieces. |
| `ask(prompt, data=None, *, schema=None)` | One sub-model call. |
| `ask_map(prompt, items=None, *, schema=None)` | Many sub-model calls in parallel. |
| `delegate(task, data=None, *, schema=None, tools=None, whole=False)` | Run a sub-agent. |
| `delegate_map(task, items, *, schema=None, tools=None)` | Run one sub-agent per item in parallel. |
| `submit(value=None)` | Finish with `value` as the answer. |
| `Failed` | Placeholder for a failed item in a `*_map` result. |
| `SubcallError`, `BudgetExceeded`, `DelegationRefused`, `ToolError` | Exceptions. |
| Your tools | Host functions you passed with `tools=`. See [Tools and MCP](tools-and-mcp.md). |

All of these are ordinary synchronous functions. Do not `await` them. In a given cell, `ask`, `ask_map`, `delegate` and `delegate_map` block until the host answers.

`schema` is keyword-only in `ask`, `ask_map`, `delegate` and `delegate_map`. `ask('q', data, {...})` raises `TypeError`. (The system prompt writes these signatures with `schema` as a plain parameter. Passing it by keyword is the form that works.)

## context

The value you passed as `context`, loaded once when the REPL starts. It can be a string, dict, list, or any picklable object. It is a normal variable: the model can reassign it, though there is rarely a reason to. If you passed no context, it is `None`.

From the CLI, `-f` files are loaded with the same rules as `rlm_ig.load`: see [Python API](python-api.md#load). Data piped on stdin is a string.

A sub-agent's `context` is the `data` its parent passed to `delegate`.

## peek

```python
peek()                    # summary of context
peek(value)               # summary of any value
peek(value, name="rows")  # use a label in the first line
```

Prints, and returns nothing. Output by type:

```text
>>> peek({'a': [1, 2, 3], 'b': 'x'})
value: dict, 2 keys
  ['a'] (list, len 3): [1, 2, 3]
  ['b'] (str, len 1): 'x'

>>> peek([1, 2, 3])
value: list, 3 items
  item types: int x3
  [0]: 1
  [1]: 2
  [2]: 3
```

A string shows its length, line count, hints (looks like JSON, has markdown headings, first line looks like a delimited header), its first 600 and last 300 characters, or the whole text when it is 1,200 characters or shorter. A dict shows up to 40 keys with type and length. A list or tuple shows item types, item size range, and its first three and last item. Anything else shows its type, length and shape if it has them, and a brief `repr`.

`peek(None)` is the same as `peek()`: it summarises `context`. There is no way to peek at a literal `None`.

## grep

```python
hits = grep(r"payment failed", window=60, limit=5)
hits[0].start, hits[0].end, hits[0].match, hits[0].snippet
```

Returns a list of up to `limit` (default 50) `Hit(start, end, match, snippet)` named tuples. `start` and `end` are character offsets in the searched text. `snippet` is the match plus `window` (default 120) characters on each side. The search is case-insensitive by default; pass `flags=0` or other `re` flags to change that. With `text` omitted it searches `context`.

`grep` needs a string. On a dict or list it raises `TypeError: grep() searches a str; got dict. Pass text=... explicitly.`

## chunk

```python
chunk("a b c d e f", size=4)          # ['a b ', 'c d ', 'e f']
chunk(["aa", "bb", "cc"], size=4)     # [['aa', 'bb'], ['cc']]
chunk(text, size=20000, overlap=200)  # overlapping pieces
chunk(text, size=5000, sep="\n---\n") # prefer cutting at this separator
```

Strings: pieces of at most `size` characters. A piece ends at a natural boundary when one exists in the last quarter of the window: `sep` if given, otherwise a blank line, then a newline, then a space. `overlap` repeats that many trailing characters at the start of the next piece and must be smaller than `size`.

Lists and tuples: items are grouped into sub-lists whose total length stays under `size`, where an item's length is `len(item)` if it has one and 1 otherwise. A single item longer than `size` gets its own group.

`size <= 0` raises `ValueError`. Anything other than a str, list or tuple raises `TypeError`.

## ask

```python
text = ask("Summarise this paragraph in one sentence.", paragraph)
data = ask("Extract name and date.", record, schema={
    "type": "object",
    "properties": {"name": {"type": "string"}, "date": {"type": "string"}},
    "required": ["name", "date"],
})
```

One call to the sub-model (`sub_model`, which defaults to `model`). The sub-model sees only your prompt and the data, nothing else, in this form:

```text
your prompt

<data>
your data
</data>
```

Its system prompt tells it that it is a component inside a larger program, to answer from the given material only, to reply with the answer alone, and to reply exactly `NOT FOUND` if the material does not contain what was asked (unless your prompt says what to do instead). So `ask` can return the string `NOT FOUND`; check for it when it matters.

Without `schema`, `ask` returns the reply text, stripped. With `schema` (a JSON Schema dict), the sub-model is told to reply with one JSON value that satisfies it. rlm-ig extracts the value, validates it, and returns the parsed Python object. If the reply is not valid, it makes one repair attempt that shows the model its previous reply and the problems. If that also fails, `ask` raises `SubcallError`:

```text
SubcallError: ModelError: the sub-model did not return valid JSON for the schema: ...
```

A repair attempt is an extra model call and counts against `max_calls`.

`prompt` and `data` can be non-strings. A string is used as is. Anything else is converted to JSON text (tuples and sets become lists, dataclasses and Pydantic models become dicts, dates become ISO strings, other objects become their `repr`). `ask('Echo.', (1, 2))` sends `[1, 2]`.

The size of prompt plus data must fit `ask_limit` (default: the sub-model's window, 400,000 characters for the built-in backends). Over that, `ask` raises `ValueError`:

```text
ValueError: ask() input is 5,018 characters; one call handles about 1,000. Split it with chunk() and use ask_map(), or hand it to delegate().
```

## ask_map

```python
tallies = ask_map("Count failures per service. Return {service: count}.", parts, schema=counts_schema)
answers = ask_map(["Question one", "Question two"])   # different prompts
```

`ask_map(prompt, items)` applies one prompt to every item, as if by `ask(prompt, item)` for each. `ask_map([p1, p2, ...])` runs a list of independent prompts. The calls run in parallel up to `max_parallel`, and results come back in input order. An empty list returns `[]`. `ask_map("a string")` with no `items` raises `TypeError`. A `schema` applies to every item.

### Batching

`ask_map(prompt, items)` packs small items into shared sub-model calls on its own, so a list of 40 short records costs 3 calls rather than 40. Pass it the natural units (one record, one paragraph, one chunk) and let it do the batching.

- A call holds at most `ask_batch` items (default 16) and about `ask_batch_chars` characters of item data (default 24,000), and always fits within `ask_limit`.
- An item larger than half of that budget gets a call to itself, so a substantial input is never diluted.
- The sub-model is told to handle each item separately and to reply with a JSON array with one element per item, in order. Without a `schema` each element is a string. With a `schema` each element must satisfy it.
- If a batched reply is unusable (not JSON, wrong length, an element that fails the schema), the items of that batch are asked one by one, with the usual repair attempt, and a `note` event records `ask_map: a batch of N items fell back to single calls (...)`. A batch that fails because the budget is spent fails all of its items with `BudgetExceeded`.
- Only the form with one prompt and a list of items is batched. A list of different prompts, as in `ask_map([p1, p2])`, is not.
- A batch is one model call as far as `max_calls` and `usage` go. The trace's `call` events show the number of items (`n`), not the number of model calls.

Set `ask_batch = 1` to turn batching off. See [Configuration](configuration.md).

If some calls fail, their entries are `Failed` objects and the cell prints a line like:

```text
[ask_map] 1 of 3 items failed; failed entries are falsy Failed(...) objects
['OK', Failed('ModelError: upstream 500'), 'FINE']
```

If every call fails, `ask_map` raises `SubcallError` instead, so a spent budget or a dead backend cannot hide.

## delegate

```python
answer = delegate("Find every clause about termination and list them.", contract_text)
answer = delegate("Total the amounts.", rows, schema={"type": "number"})
```

Starts a sub-agent with its own REPL, in which `data` is `context` and the task is its task. It runs the same loop as the root and returns what it submits. Use it when a piece is too large or too intricate for one `ask`. See [Concepts](concepts.md#sub-calls-and-sub-agents).

- `data`: reduced to JSON-like values before it is sent: tuples and sets become lists, bytes become text, dataclasses and Pydantic models become dicts, dates become ISO strings, other objects become their `repr`. Pass plain strings, lists and dicts.
- `schema`: enforced inside the sub-agent. Its `submit` is checked, and it gets the errors back and can fix them, exactly as the root does.
- `tools`: which tools the sub-agent gets. See below.
- `whole=True`: skip the pass-through guard.

If the sub-agent fails, for instance because it ran out of turns, `delegate` raises `SubcallError`:

```text
SubcallError: StepsExhausted: agent 0.2 did not submit an answer (out of turns)
```

Beyond `max_depth`, `delegate` raises `DelegationRefused`: `sub-agents are not available at this level. Do the work here with code, ask() and ask_map().` If the pass-through guard refuses, the message is:

```text
this would hand a sub-agent 30,000 characters, 100% of your own context, which only moves the problem down a level. Narrow it first (filter, slice, or chunk and use delegate_map / ask_map). If one pass over everything really is what you want, pass whole=True.
```

### Tools in sub-agents

| `tools=` | The sub-agent gets |
| --- | --- |
| not given | The parent's host tools, if `inherit_tools` is true (the default). None if it is false. |
| `[]` | No host tools. |
| `["lookup", price]` | Exactly those. Names of existing tools, or functions. |

A function that is not a host tool, typically one the model defined in a cell, is sent as source code. It is run in the sub-agent's namespace with `exec`. That means it must be self-contained: it cannot see the parent's variables, and imports from the parent's cell are not there. Import inside the function body. The function must have been defined with `def` in a cell, because the source is read back from the cell; a lambda cannot be passed. If a helper fails to load in the sub-agent, the sub-agent's first message carries a note saying so.

```python
def shout(s):
    return s.upper() + "!"

delegate("Use the helper on your context.", "hey", tools=[shout])
```

An unknown tool name raises `ToolError: unknown tool 'nope'; available: none`.

## delegate_map

```python
results = delegate_map("Summarise this section.", sections, schema=summary_schema)
```

One sub-agent per item, in parallel, results in input order. Failed items are `Failed` objects. If every item fails, it raises `SubcallError`. An empty list returns `[]`. The pass-through guard does not apply, because each agent gets one item rather than the whole.

## submit

```python
submit(result)
```

Finishes the agent. The value is converted to JSON-like data (tuples and sets become lists, and so on), checked against the schema if there is one, and returned as the answer. Nothing after `submit` in that cell runs. If the value fails the schema, the cell ends and the model gets the errors in its next message, with its variables intact.

`submit()` with no argument submits `None`. Submitting a function or a module is refused with `submit() needs data, not function. Pass the value itself (str, number, list, dict...).`

`submit` works by raising an exception that derives from `BaseException`. `except Exception:` does not catch it. A bare `except:` or `except BaseException:` does.

## Failed

```python
out = ask_map("Shout.", ["ok", "boom", "fine"])
good = [x for x in out if x]     # Failed is falsy
out[1].error                     # 'ModelError: upstream 500'
```

`Failed` has one attribute, `error`, a string. It is falsy, so `if x` and `filter(None, ...)` drop failures. `repr` is `Failed('...')`. The model's instructions tell it to check for failed items and redo only those.

## Exceptions

All four derive from `RuntimeError` through `SubcallError`:

```text
RuntimeError
  SubcallError         a sub-model call, sub-agent, or tool failed
    BudgetExceeded     the run's budget is spent
    DelegationRefused  depth limit or pass-through guard
    ToolError          a host tool raised
```

`ask`, `ask_map`, `delegate` and `delegate_map` also raise plain `ValueError` (input too large) and `TypeError` (bad arguments, a non-dict schema). Other host-side failures, such as a model error from the sub-model, arrive as `SubcallError` whose message starts with the original type name, for example `ModelError: upstream 500`.

Tool errors read `lookup failed: KeyError: 'kiwi'`. The tool's own exception type is named in the message, not re-raised, because the tool runs in the host process.

A sub-call that raises is a normal Python exception in the cell. If the model does not catch it, the cell ends with a traceback and the model sees it.

## How a cell behaves

### Display of the last expression

If the last statement of a cell is an expression, its value is printed, as in a notebook. The printed form is a compact `repr`: containers show up to 30 items and then `...`, strings show a head and tail with `...` between, and the whole is capped at about 2,000 characters. `None` is not shown. An assignment shows nothing.

```text
>>> list(range(100))
[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, ...]
```

### Top-level await

A cell may use `await` at the top level, for example `await asyncio.sleep(1)`. rlm-ig's own functions are not awaitable. `asyncio.run(...)` also works inside a cell.

### State

Variables, imports, functions and classes persist from cell to cell for the life of the worker. The worker lives as long as the agent does, or as long as the [session](sessions.md).

### Output

Anything written to stdout or stderr during the cell is captured, including output from threads and from child processes the cell starts (`subprocess.run(["echo", "hi"])` shows `hi`). The worker's stdin is `/dev/null`, so `input()` raises `EOFError`.

### Errors

An uncaught exception ends the cell and the model sees a traceback that lists only the frames in cell code:

```text
Traceback (most recent call last):
  <cell 3>, line 3, in <module>
    g()
  <cell 3>, line 2, in g
    raise ValueError('bad')
ValueError: bad
```

A `SyntaxError` is shown without a traceback. `SystemExit` is ignored with the message `SystemExit ignored: use submit(value) to finish.`

### Cell timeout

A cell may compute for `cell_timeout` seconds (default 120). Time spent waiting for `ask`, `delegate` or a tool does not count. When a cell runs past the limit:

1. rlm-ig sends the worker `SIGINT`. The cell gets a `KeyboardInterrupt`, ends, and the model sees `Interrupted: the cell ran past its time limit and was stopped. Variables are intact.`
2. If the cell does not stop within 5 more seconds (for example it catches `KeyboardInterrupt` in a loop), the worker is killed and restarted.

Under a [launcher](security.md#real-isolation-with-a-launcher), cells cannot be interrupted, so step 2 happens immediately.

### Crash and restart

If the worker dies (it called `os._exit`, hit the memory limit and was killed, crashed, or was killed for ignoring the timeout), rlm-ig starts a new one and tells the model:

```text
The REPL process stopped (exit code 3). It was restarted: `context` is back, but every other variable is gone.
```

The new worker has the original `context` and the host tools. Everything else the model defined is gone, and cell numbers start again from 1. The run continues.

`memory_mb` sets an address-space limit (`RLIMIT_AS`) on the worker. Exceeding it usually shows up as an ordinary `MemoryError` in the cell, and the worker stays alive.

### Working directory

Each agent's worker starts in its own empty temporary directory, for example `/tmp/rlm-run-xxxx/agent-0`, which is removed when the run ends. Files the code writes there are gone afterwards. Set `sandbox.workdir` to use one fixed directory instead. It is then shared by every agent in the run and is not removed.

### Environment

The worker does not inherit your environment. It gets a short allowlist: `PATH`, `HOME`, `USER`, `LANG` and locale variables, `TZ`, `TERM`, temp directories, virtualenv variables, `PYTHONPATH`, proxy variables and certificate-bundle variables. API keys are not passed. `sandbox.pass_env` adds names, `sandbox.env` sets values, and `sandbox.inherit_env` passes everything. The exact list and the reasoning are in [Security](security.md#the-environment-allowlist).

### Concurrency

Calls to `ask` and friends can be made from several threads in one cell, and they run concurrently on the host:

```python
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(4) as pool:
    results = list(pool.map(ask, ["a", "b", "c"]))
```

`ask_map` does the same job with less code and respects `max_parallel`.

## Try it yourself

You can run any cell through the real REPL, offline, by scripting the model. `verbosity=2` prints each cell's code and output:

```python
import rlm_ig as rlm
from rlm_ig import Scripted

cell = """
rows = chunk(context, size=12)
print(rows)
print(grep('beta')[0].start)
print(ask_map('Shout.', rows))
"""

model = Scripted(
    [f"Trying a few things.\n\n```python\n{cell}\n```", "```python\nsubmit(len(rows))\n```"],
    asks=lambda prompt: prompt.split("<data>\n")[1].split("\n</data>")[0].upper(),
)
result = rlm.run(
    "Explore.", context="alpha beta gamma delta", model=model,
    ask_batch=1, verbosity=2, trace=False,
)
print(result.answer)
```

`Scripted` takes a list of replies, one per turn, or a function. `asks` supplies the sub-model's replies. `ask_batch=1` turns off `ask_map` batching, so that the `asks` function sees one plain prompt per item. See [Backends](backends.md#scripted-for-tests).
