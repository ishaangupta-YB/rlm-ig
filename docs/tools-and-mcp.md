# Tools and MCP

A tool is a function of yours that the model can call from its REPL. Use tools for what the model cannot or should not do from inside the sandbox: query a database, call an internal API, use a credential the sandbox must never see.

## Host tools

Any Python callable is a tool. Pass them with `tools=`:

```python
import rlm_ig as rlm
from rlm_ig import Scripted

PRICES = {"apple": 3, "pear": 5}

def price(item: str, qty: int = 1) -> float:
    """Look up the unit price of an item and multiply by qty."""
    return PRICES[item] * qty

def reply(code):
    return f"Working.\n\n```python\n{code}\n```"

model = Scripted([reply("submit([price('apple', qty=2), price('pear')])")])
result = rlm.run("What do 2 apples and a pear cost?", model=model, tools=[price], verbosity=0, trace=False)
print(result.answer)   # [6, 5]
```

With a real model, drop the `Scripted` line and pass `model="claude-code"` or any [model string](backends.md).

### Where a tool runs

A tool runs in your process, not in the sandbox. The REPL worker only holds a stub with the same name, signature and docstring. Calling the stub sends the arguments to the host and waits for the result. That is why a tool can use any library, hold open connections, and read credentials the sandbox never sees.

Synchronous tools run in a worker thread, so a slow one does not block the run. Calls made from several threads in one cell, or by several agents at once, run concurrently, so make shared state thread-safe. Async tools (`async def`) are awaited on the run's event loop. rlm-ig puts no time limit on a tool call, and the cell timeout does not count the time spent waiting for one, so give slow tools their own timeouts.

### Arguments and results

Arguments come from model-written code and arrive as JSON-like values: strings, numbers, booleans, `None`, lists and dicts. A tuple arrives as a list. The model can call a tool with positional or keyword arguments.

A result can be any picklable object. Plain JSON values (including nested dicts and lists) travel as JSON. Anything else is pickled by the host and unpickled inside the worker, so the class of the returned object must be importable in the worker:

- Standard library types, and classes from packages installed in the same Python environment, work: `datetime.date`, `decimal.Decimal`, and so on. The worker runs under the same interpreter unless you set `sandbox.python` or a launcher.
- A class defined in your script's `__main__` does not work. The cell fails with `AttributeError: Can't get attribute 'Local' on <module '__main__'>`.
- A class in a module that is only on your script's `sys.path` does not work either, because the worker does not share it. Put the module on `PYTHONPATH` (that variable is passed to the worker), or install it.

When in doubt, return plain dicts, lists and strings. They are also the cheapest thing for the model to read.

### How the model sees a tool

The first message lists each tool as a stub, never the body:

```text
## Tools

Available in the REPL in addition to the built-ins:

def price(item: str, qty: int = 1) -> float
    Look up the unit price of an item and multiply by qty.
```

The signature comes from `inspect.signature`, with annotations unquoted, and the description is the docstring. Write both for the model: a clear name, type hints, and a docstring that says what it returns. A tool with no docstring is listed as `(no description)`.

With more than 12 tools, the list switches to one line per tool, with the first line of each docstring, and ends with `(call help(name) in the REPL for a tool's full description)`. `help(name)` then prints the full docstring. It shows the stub's `(*args, **kwargs)` instead of the real signature, which is why the one-line list includes the signature.

### The `@tool` decorator

Plain functions work without it. Use `@tool` to rename a tool or replace its description:

```python
from rlm_ig import tool

@tool(name="search", doc="Search the knowledge base. Returns a list of hits, best first.")
def kb_search(query: str, k: int = 5) -> list[dict]:
    ...
```

`name` and `doc` are both optional. The model calls `search(...)`. The decorator also works on a `functools.partial` or an object with `__call__`, where you need a name, and often a description, because a partial's own docstring is generic:

```python
import functools
from rlm_ig import tool

def add(a, b):
    return a + b

add_one = tool(name="add_one", doc="Add 1 to b.")(functools.partial(add, 1))
```

Rules:

- The name must be a valid Python identifier and not a keyword.
- It must not collide with a REPL name: `context`, `ask`, `ask_map`, `delegate`, `delegate_map`, `submit`, `peek`, `chunk`, `grep`, `Failed`, `SubcallError`, `BudgetExceeded`, `DelegationRefused`, `ToolError`.
- Two different tools cannot share a name.

Breaking a rule raises `ConfigError` when the run starts.

### Errors

If a tool raises, the model's code gets a `ToolError` whose message names the tool and the original exception:

```text
ToolError: price failed: KeyError: 'kiwi'
```

The model can catch it with `except ToolError`. It is a `SubcallError`.

### Tools and sub-agents

Sub-agents inherit the parent's host tools by default (`inherit_tools = true`). Tool calls from a sub-agent run in your process like any other. To control what a sub-agent gets:

```python
delegate("Look it up.", item)                    # parent's tools, if inherit_tools is true
delegate("Think about it.", item, tools=[])      # none
delegate("Look it up.", item, tools=["price"])   # exactly these, by name
```

Set `inherit_tools = false` to make `delegate` give a sub-agent nothing unless `tools=` says so.

A function the model defined itself in a cell can also be handed to a sub-agent through `tools=[fn]`. Its source is copied into the sub-agent's REPL, so it has to be self-contained: no references to the parent's variables, and imports inside the function body. See [REPL API](repl-api.md#tools-in-sub-agents).

Tool calls appear in the trace as `call.start` and `call.end` events with `op` set to `tool` and the tool's name. In the console they are shown at verbosity 2 only.

### Sessions

`rlm.Session(tools=[...])` gives the tools to every task in the session. See [Sessions](sessions.md).

## MCP servers

[Model Context Protocol](https://modelcontextprotocol.io) servers expose tools over JSON-RPC. rlm-ig has a small built-in client, with no dependencies, that turns each server tool into a host tool.

Configure servers in `rlm.toml`, or with the `mcp=` option. A server is either a local process (stdio) or a remote one (streamable HTTP):

```toml
# stdio: rlm-ig starts the process and talks to it over stdin and stdout
[mcp.files]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]
env = { LOG_LEVEL = "warn" }
cwd = "/srv/data"

# streamable HTTP: every message is a POST
[mcp.search]
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ..." }
timeout = 30          # seconds per request, default 60 (either kind)
tools = ["query"]     # expose only these tools, default all (either kind)
```

From Python:

```python
result = rlm.run(
    "List the files that mention invoices.",
    model="claude-code",
    mcp={"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]}},
)
```

A server table has exactly one of `command` (stdio) or `url` (HTTP). Otherwise rlm-ig raises `ConfigError: MCP server 'NAME' needs either "command" (stdio) or "url" (HTTP)`.

### What the model gets

Each MCP tool becomes a function named `<server>_<tool>`. Characters that are not valid in a Python name become `_`, so the tool `list-items` on server `calc` is `calc_list_items`. The function takes keyword arguments only. Its description is the tool's description followed by the parameter descriptions, and its signature is built from the tool's input schema. For a server named `calc` with an `add` tool, the model sees:

```text
def calc_add(*, a: int, b: int) -> Any
    Add two integers.

    Args:
        a: first addend
```

JSON Schema types are shown as `str`, `int`, `float`, `bool`, `list`, `dict` or `Any`. Optional parameters are shown with `= None`, and a parameter set to `None` is left out of the request. Calling with positional arguments raises `ToolError: calc_add takes keyword arguments only: calc_add(*, a: int, b: int) -> Any`.

The result is converted for Python:

- If the server reports an error, the call raises `ToolError: calc_fail failed: <the server's text>`.
- Structured content, when the server returns it, is returned as the parsed value (for example `{'sum': 42}`).
- Otherwise the text content is returned: one text block as a string, several joined with newlines.
- Anything else is returned as the raw content list.

Only tools are used. Resources, prompts and server-initiated requests such as sampling are not supported.

### When servers start

Servers are started and initialised when a run starts, all at once, and stopped when it ends. A `Session` keeps them for its whole life. If any server cannot be started or initialised, if a name in `tools` does not exist on the server, or if two servers produce a tool with the same name, `rlm.run` raises `ConfigError` before the model is called. Examples of the messages:

```text
MCP server 'calc': could not start 'nonexistent-binary': [Errno 2] No such file or directory: 'nonexistent-binary'
MCP server 'calc' has no tool named nope
MCP server 'calc' could not be initialised: [Errno 111] Connection refused
```

A request that times out or loses its server at call time raises `ToolError` in the model's code: `calc_add: MCP server 'calc' did not answer (...)`.

MCP tools are ordinary host tools. They count toward the compact listing after 12, they are inherited by sub-agents, they can be restricted with `delegate(tools=[...])`, and they appear in traces like any tool.

### Trust

A stdio server is a child process of your process. It gets your full environment, plus anything in `env`, and runs with your privileges. An HTTP server receives whatever the model sends it. Connecting to a server hands the model that server's capabilities, and a server's replies are data that can contain instructions aimed at the model. Only connect servers you trust. See [Security](security.md).

For an HTTP server on `localhost`, `127.0.0.1` or `::1`, proxy environment variables are ignored. Other hosts use them.
