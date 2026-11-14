# Python API

```python
import rlm_ig as rlm
```

Exported names: `run`, `arun`, `Session`, `Result`, `Config`, `SandboxOptions`, `Usage`, `Event`, `tool`, `load`, `Model`, `Scripted`, and the exceptions `RlmError`, `ConfigError`, `ModelError`, `BudgetExceeded`, `StepsExhausted`, `SandboxError`, `SchemaError`, `ToolError`, `RunFailed`.

## run and arun

```python
result = rlm.run(task, context=None, *, schema=None, tools=None, on_event=None, config=None, **options)
result = await rlm.arun(task, context=None, *, schema=None, tools=None, on_event=None, config=None, **options)
```

| Argument | Meaning |
| --- | --- |
| `task` | What to find out or produce. A non-empty string. |
| `context` | The data the model explores. A string of any length, or a dict, list or any picklable object. Use [`load`](#load) to read files. `None` for a task that needs no data. |
| `schema` | Constrains the answer. See [schema](#schema). |
| `tools` | Python callables exposed in the REPL. See [Tools and MCP](tools-and-mcp.md). |
| `on_event` | A function called with every [event](#on_event). |
| `config` | A `Config` to use as is. When given, no config files and no environment variables are read. |
| `**options` | Any `Config` field as a keyword: `model=`, `sub_model=`, `max_usd=`, `max_depth=`, `max_steps=`, `timeout=`, `verbosity=`, `trace=`, `providers=`, and so on. See [Configuration](configuration.md). `None` values are ignored. |

`run` blocks and is safe to call from ordinary code and scripts. Called from inside a running event loop it raises `RlmError`, so use `await rlm.arun(...)` there. `arun` calls can run concurrently with `asyncio.gather`.

By default a run prints progress to stderr (`verbosity=1`). Pass `verbosity=0` to silence it.

A minimal offline example, using the scripted model from [Backends](backends.md#scripted-for-tests):

```python
import rlm_ig as rlm
from rlm_ig import Scripted

def reply(code):
    return f"Working.\n\n```python\n{code}\n```"

model = Scripted([reply("submit(len(context.split()))")])
result = rlm.run("Count the words.", "one two three", model=model, verbosity=0, trace=False)
print(result.status, result.answer, result.steps, result.agents)
print(result.usage)
print(repr(str(result)))
```

```text
ok 3 1 1
1 call · 1.1k in / 13 out · $0.0000
'3'
```

The scripted model estimates tokens from character counts, so the token figures depend on the system prompt and change between versions.

## Result

| Field | Type | Meaning |
| --- | --- | --- |
| `answer` | any | What was passed to `submit`: a string, number, bool, list, dict or `None`. `None` when the run failed. |
| `status` | `str` | `"ok"`, `"partial"` or `"failed"`. |
| `note` | `str` or `None` | For `partial`: why. For example `answer produced by a forced wrap-up (out of turns)`. |
| `error` | `str` or `None` | For `failed`: `ErrorType: message`. |
| `usage` | `Usage` | Totals for the whole run: `calls`, `input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd` (`None` until some backend reports or estimates a price), and `total_tokens`. |
| `usage_by_model` | `dict[str, Usage]` | The same, per model spec. |
| `steps` | `int` | Model turns taken by agents. |
| `agents` | `int` | Agents started, root included. |
| `seconds` | `float` | Wall-clock time. |
| `trace` | `str` or `None` | Path of the trace file, `None` if tracing is off or nothing was written. |
| `model`, `sub_model` | `str` | The resolved specs, for example `claude-code:sonnet`. |
| `parsed` | any | The answer as an instance of your dataclass or Pydantic model. See [schema](#schema). |

`input_tokens` counts every prompt token, cached or not, so the number means the same on every backend. `cached_tokens` is the part that was served from a provider cache.

Properties and methods:

- `result.ok` is `status == "ok"`.
- `str(result)` is the answer as text: a string as is, anything else as JSON.
- `result.to_dict()` is a JSON-friendly dict of everything above except `parsed`. The CLI's `--json` prints it.
- `result.raise_for_status()` returns the result when `status` is `ok`, and raises `RunFailed` otherwise.

### Statuses

| Status | When | What you have |
| --- | --- | --- |
| `ok` | The root agent called `submit` normally, and the value passed the schema. | `answer`. |
| `partial` | The answer came from a forced wrap-up turn: the root ran out of turns, or a budget was spent. | `answer` and `note`. It is real, but built in one last turn, so check it. |
| `failed` | There is no answer: the backend failed, the sandbox failed, the wrap-up turn did not submit, or the time limit passed. | `error`. |

```python
result = rlm.run("...", context=data, verbosity=0)
result.raise_for_status()       # RunFailed unless status == "ok"
```

`RunFailed` carries `the run ended early: <note>` for a partial result, and the `error` text for a failed one. To accept partial answers, check the status yourself:

```python
if result.status == "failed":
    raise SystemExit(result.error)
if result.status == "partial":
    print("warning:", result.note)
```

## schema

`schema=` constrains the answer. The model is shown the JSON Schema, and `submit` is checked against it. A value that does not match is rejected with the errors, and the model can fix it and submit again. A prose answer is never accepted when a schema is set.

`schema` can be:

**A JSON Schema dict.**

```python
schema = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}
```

**A type hint.** `int`, `float`, `str`, `bool`, `list[str]`, `dict[str, int]`, `tuple[int, int]`, `Literal["yes", "no"]`, unions such as `int | None`, and combinations of these.

**A dataclass.**

```python
import dataclasses

@dataclasses.dataclass
class Person:
    name: str
    age: int

result = rlm.run("...", context=text, schema=Person)
result.answer    # {'name': 'Ada', 'age': 36}
result.parsed    # Person(name='Ada', age=36)
```

**A `TypedDict`.**

```python
import typing

class Row(typing.TypedDict):
    sku: str
    qty: int
```

**A Pydantic v2 model.**

```python
import pydantic

class Invoice(pydantic.BaseModel):
    number: str
    total: float
```

`result.answer` is always the plain validated value (a dict for objects). `result.parsed` is an instance of the class for dataclasses and Pydantic v2 models, and `None` for everything else, or if building the instance fails. Dataclasses are built at every level: a field typed `list[Incident]`, `tuple[Incident, ...]`, `dict[str, Incident]` or `Incident | None` holds `Incident` instances. `TypedDict`, `Literal` and enum-typed values stay as the plain JSON values. With a scripted model that submits a matching value, the results are:

```text
{'type': 'object', 'properties': {'n': {'type': 'integer'}}, ...} -> ok {'n': 3} None
Person -> ok {'name': 'Ada', 'age': 36} Person(name='Ada', age=36)
Row -> ok {'sku': 'A1', 'qty': 2} None
Invoice -> ok {'number': 'INV-1', 'total': 12.5} Invoice(number='INV-1', total=12.5)
list[str] -> ok ['a', 'b'] None
```

The schemas that dataclasses, `TypedDict` and type hints produce are, for example:

```json
{"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}, "required": ["name", "age"]}
{"type": "array", "items": {"type": "string"}}
{"type": "object", "additionalProperties": {"type": "integer"}}
```

An unsupported type raises `SchemaError` from `run` before anything starts.

The checker is built in and has no dependencies. It covers `type`, `enum`, `const`, string length and `pattern`, numeric bounds and `multipleOf`, array `items`, `prefixItems`, size and uniqueness, object `properties`, `required`, `additionalProperties` and size, local `$ref`, and `allOf`, `anyOf`, `oneOf` and `not`. Unknown keywords are ignored rather than rejected.

A schema also applies to `ask`, `ask_map`, `delegate` and `delegate_map` calls that pass one. See [REPL API](repl-api.md).

## tools

```python
def price(item: str, qty: int = 1) -> float:
    """Look up the unit price of an item and multiply by qty."""
    return PRICES[item] * qty

result = rlm.run("What do 3 apples cost?", tools=[price], model="claude-code")
```

Tools run in your process and appear in the REPL as functions. Everything about them is in [Tools and MCP](tools-and-mcp.md).

## on_event

`on_event(event)` is called for every event of the run, in order. Events are also what the console output and the trace file are built from.

```python
import rlm_ig as rlm
from rlm_ig import Scripted

def reply(code):
    return f"Working.\n\n```python\n{code}\n```"

counts = {}

def on_event(event):
    counts[event.kind] = counts.get(event.kind, 0) + 1
    if event.kind == "cell":
        print(f"agent {event.agent} step {event['step']}: error={event['error']} output={event['output']!r}")

model = Scripted([reply("r = ask('q')\nprint(r)"), reply("submit(r)")], asks="a")
result = rlm.run("t", model=model, on_event=on_event, verbosity=0, trace=False)
print(counts)
```

```text
agent 0 step 1: error=False output='a'
agent 0 step 2: error=False output=''
{'run.start': 1, 'agent.start': 1, 'model.reply': 2, 'cell.start': 2, 'call.start': 1, 'call.end': 1, 'cell': 2, 'agent.end': 1, 'run.end': 1}
```

An `Event` has `kind`, `agent`, `depth`, `t` (seconds since the run started) and a `data` dict. `event["field"]` and `event.get("field", default)` read from `data`. `event.to_dict()` is the flat form written to the trace.

`agent` is `"0"` for the root, `"0.1"` for its first sub-agent, `"0.1.2"` for that agent's second, and `""` for run-level events. Events from parallel sub-agents interleave, so use `agent` to tell them apart.

The handler runs inline, in the run's own thread, so a slow handler slows the run. If it raises, rlm-ig prints one line to stderr (`rlm: an event handler failed (...); continuing`) and keeps going. It reports a given handler only once, but still calls it for later events.

### Event kinds

| Kind | Fields |
| --- | --- |
| `run.start` | `task`, `model`, `sub_model`, `summary` (a line describing the context), `limits` (`max_depth`, `max_steps`, `max_calls`, `max_usd`, `max_tokens`, `timeout`, `parallel`), `tools` (names). |
| `agent.start` | `task`, `parent` (the parent agent id or `None`), `context_size` (characters), `summary`. |
| `model.reply` | `step`, `text` (the model's full reply), `seconds`, `usage` (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`), `model`. |
| `cell.start` | `step`, `code`. |
| `cell` | `step`, `code`, `output` (cut to `output_limit`), `error` (bool), `seconds`, `submitted` (bool). |
| `call.start` | `call` (id), `op` (`ask`, `ask_map`, `delegate`, `delegate_map` or `tool`), `n` (items), `chars`, `name` (tool name, or the first line of a delegate task). |
| `call.end` | The same fields, plus `seconds`, `ok`, `failed` (count of failed items), and `error` when `ok` is false. |
| `agent.end` | `status` (`ok`, `partial`, `failed` or `cancelled`), `steps`, and `answer` (cut to 2,000 characters) or `error`. |
| `note` | `message`. For example a prose reply accepted as the answer, a runaway reply cut off, a backend that reports no cost. Run-level notes have an empty `agent`. |
| `run.end` | `status`, `answer` (cut to 4,000 characters, `None` on failure), `error`, `note`, `usage`, `usage_by_model`, `usage_text`, `seconds`, `steps`, `agents`, `trace`. |

A turn produces, in order: `model.reply`, `cell.start`, any `call.start` and `call.end` pairs the code makes, then `cell`. The same events are in the [trace file](tracing.md).

## Session

```python
with rlm.Session(model="claude-code") as session:
    session.run("Parse this export and index it by customer.", context=export)
    result = session.run("Which customers have more than three open tickets?")
```

One live REPL across several tasks. Everything about it is in [Sessions](sessions.md).

## load

```python
rlm.load(path) -> Any
```

Reads one file or directory into a value suitable as `context`:

| Path | Result |
| --- | --- |
| `.json` | The decoded value. |
| `.jsonl`, `.ndjson` | A list of decoded records, one per non-empty line. |
| `.csv`, `.tsv` | A list of dicts, one per row, keyed by the header. All values are strings. |
| A directory | A dict of relative path (with `/`) to text, for the text files inside. |
| Anything else | The file's text. |

Files are read as UTF-8, with undecodable bytes replaced. For a directory, hidden files and folders and common clutter (`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build`, and so on) are skipped, as are binary files and files over 5 MB. A file you name directly is always loaded. A path that does not exist, a file that does not parse, or a directory with no readable text files raises `ConfigError`.

```python
context = {"orders": rlm.load("orders.csv"), "notes": rlm.load("notes.md")}
```

The CLI does the same for each `-f`, and for several `-f` it builds a dict keyed by file name, adding `(2)` to repeated names.

## Errors

All errors rlm-ig raises on purpose derive from `RlmError`:

```text
RlmError
  ConfigError       invalid or incomplete configuration
  ModelError        a backend failed: missing binary, bad credentials, API error. Has .retryable
  BudgetExceeded    a run-wide limit was reached
  StepsExhausted    an agent used all its steps without submitting
  DelegationRefused a sub-agent was not started (depth limit or guard)
  SandboxError      the REPL worker could not be started or stopped responding
  SchemaError       a schema is not usable, or a value does not satisfy it
  ToolError         a host tool raised
  RunFailed         raised by Result.raise_for_status
```

`DelegationRefused` is importable from `rlm_ig.errors`; the others are also on the top-level package.

What is raised, and what is returned as a `failed` result:

- **Raised from `run`, before the run starts:** an empty task, no model and none detected, an unknown provider, an invalid option, a tool name that collides with a REPL name, an unsupported `schema`, and an MCP server that cannot be started or initialised. These are `ConfigError` or `SchemaError`.
- **Returned as `status="failed"`:** any `RlmError` during the run, with `error` set to `ErrorType: message`. That covers backend errors (`ModelError`), sandbox failures, an agent that never submitted (`StepsExhausted`), and the overall time limit.
- **Raised from `run`, after cleanup:** anything that is not an `RlmError`, such as `KeyboardInterrupt`, task cancellation, or an unexpected exception from a library.

So a loop over many tasks should check `result.status`, and should also expect an occasional exception from `run` for problems outside rlm-ig's control.

The `BudgetExceeded`, `DelegationRefused`, `ToolError` and `SubcallError` that model-written code sees are separate classes of the same names inside the REPL. See [REPL API](repl-api.md#exceptions).
