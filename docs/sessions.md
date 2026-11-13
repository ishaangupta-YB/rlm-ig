# Sessions

A plain `rlm.run` starts a fresh REPL, answers one task, and throws the REPL away. A `Session` keeps one REPL alive across several tasks. Whatever one task builds (parsed data, an index, helper functions) is still there for the next. This pays off when the first step is expensive, such as parsing a large export, and later questions reuse it.

## Using a session

```python
import rlm_ig as rlm

with rlm.Session(model="claude-code") as session:
    session.run("Parse this export and index it by customer.", context=export)
    result = session.run("Which customers have more than three open tickets?")
    print(result.answer)
```

In async code:

```python
async with rlm.Session(model="claude-code") as session:
    await session.arun("Parse this export and index it by customer.", context=export)
    result = await session.arun("Which customers have more than three open tickets?")
```

`Session(*, config=None, tools=None, path=None, max_snapshot_bytes=50_000_000, **options)` takes the same `config` and keyword options as `rlm.run`, and `tools`. `session.run(task, context=None, *, schema=None, on_event=None)` returns a normal [`Result`](python-api.md#result).

- **`context`** replaces the REPL's `context` when you pass it. Leave it out (or pass `None`) and the REPL keeps the `context` it already has.
- **Each task is its own run.** It has its own budget, its own trace file, its own `Result` and usage figures, and a fresh model conversation. Limits such as `max_usd` apply per task, not per session.
- **Sub-agents** started during a task get their own throwaway REPLs as usual. Only the root agent's REPL lives on.
- **Tools and MCP servers** you configured are available to every task. MCP servers are started once and live until the session closes.
- **Closing** the session (leaving the `with` block, or `close()` / `aclose()`) stops the REPL worker and any MCP servers.

Other members:

| Member | Meaning |
| --- | --- |
| `session.history` | A list of completed tasks, oldest first: `{"task": ..., "status": ..., "answer": ...}`. The answer is text, cut to 2,000 characters, and `None` for a failed task. |
| `session.variables()` / `await session.avariables()` | `[{"name", "type", "preview"}]` for everything the REPL holds, except `context`. |
| `session.skipped` | Variables that could not be saved or restored, with the reason. |
| `session.save()` / `await session.asave()` | Write the session to `path` now. |

The blocking methods (`run`, `variables`, `save`, `close`) raise `RlmError` if called from a running event loop. Inside a loop, use the `a`-prefixed coroutines.

## What the next task is told

Every task starts a new model conversation, so the model does not remember the earlier ones. Instead, the first message gets an "Earlier in this session" section: the last 8 tasks with their answers, and an inventory of the variables that exist. The prompt stays small however long the session runs. For the example above, the second task's first message contains (real output):

```text
## Earlier in this session

This REPL has already been used in this session. Its variables are still defined; reuse them.

Earlier tasks and their answers:
  1. Parse the export and index it by customer.
     -> ["acme", "zed"]

Variables and functions defined so far:
  open_count (def): <function open_count at 0x7fcea61af380>
  rows (list): [{'customer': 'acme', 'open': True}, {'customer': 'acme', 'open': True}, {'customer': 'zed', 'open': False}]
  by_customer (dict): {'acme': [{'customer': 'acme', 'open': True}, {'customer': 'acme', 'open': True}], 'zed': [{'customer': 'zed', 'open': False}]}
  row (dict): {'customer': 'zed', 'open': False}
```

Each answer is shown as its first line, up to 300 characters. Older tasks are summarised as `(N older task(s) not shown)`. The inventory lists up to 60 names with a short preview each. The model is expected to inspect a variable itself if it needs more. If some variables could not be restored from a saved file, a line says `Not carried over from the saved session: ...`.

The `Data` section of the first message describes the current `context`.

## Saving to a path

```python
with rlm.Session(model="claude-code", path="tickets-session.json") as session:
    session.run("Parse this export and index it by customer.", context=export)

# later, perhaps in another process
with rlm.Session(model="claude-code", path="tickets-session.json") as session:
    session.run("How many open items does acme have?")
```

With a `path`, the REPL's namespace and the task history are written to that file after every task (atomically, through a temporary file and a rename), and restored the next time a session opens the same path. A leading `~` is expanded. Failed tasks are recorded too. The parent folders are created. Call `session.save()` to write it at other times.

### What is saved

| Saved | How |
| --- | --- |
| Variables, including `context` | Each is pickled (protocol 4) and stored as a base64 string. |
| Functions and classes defined in cells | As their source text. They are restored by running the source again. |
| Task history | Task text, status, and the answer text. |

### What is not saved

- **Imports and modules.** A function restored from a saved session that uses `re` or `json` fails with `NameError` until the code imports the module again. Import inside the function, or import again at the start of the next task.
- **Variables that cannot be pickled**, such as locks, open files, sockets and generators. They are listed in `session.skipped` with a reason like `not picklable (TypeError)`, and the model is told.
- **Variables whose pickle is larger than `max_snapshot_bytes`** (default 50 MB each). The reason is `too large (N bytes)`.
- **Names that start with an underscore.**
- **The tool stubs and built-ins**, which are recreated from the tools you pass to `Session`.
- **Files the code wrote** in its working directory. The directory is temporary and deleted when the session closes, unless you set `sandbox.workdir`.
- **The model conversation.** Each task starts a new one.

### The file

```text
{"version": 1, "history": [...], "state": {"vars": {"rows": "<base64>"}, "defs": {"open_count": "def open_count(name): ..."}, "skipped": {"lock": "not picklable (TypeError)"}}}
```

Because `context` is saved like any variable, the file contains your whole input data. It can be large, and it holds whatever is sensitive in the data.

The host never unpickles anything. The pickles are made by the worker, stored as opaque text, and given back to a worker when the session is restored. That means a session file is code, in the sense that restoring it unpickles its contents inside the worker. Do not open a session file that came from somewhere you do not trust. See [Security](security.md).

Opening a file that is not valid JSON, or that was written by a different state version, raises `RlmError`:

```text
could not read the session file /path/bad.json: Expecting value: line 1 column 1 (char 0)
/path/bad.json was written by an incompatible version (state version 9)
```

## One task at a time

A session has one REPL, so it runs one task at a time. Concurrent `arun` calls on the same session queue up and run in order:

```python
async with rlm.Session(model="claude-code") as session:
    a, b = await asyncio.gather(session.arun("First."), session.arun("Second."))   # runs one, then the other
```

For parallel work, use several sessions or plain `rlm.arun` calls.

## From the CLI

```bash
rlm "Parse this export and index it by customer." -f export.jsonl --session tickets
rlm "Which customers have more than three open tickets?" --session tickets
```

`--session NAME` stores the session at `.rlm/sessions/NAME.json` under the current directory, and every invocation with the same name resumes it. If `NAME` contains a path separator or ends in `.json`, it is used as the path itself. With `-f` or piped data the `context` is replaced. Without it the session keeps its `context`.

In a script, when you give no `-f` and stdin is not a terminal, `rlm` reads stdin. Redirect `< /dev/null` for follow-up tasks, otherwise the command waits for input.
