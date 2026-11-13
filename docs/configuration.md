# Configuration

Every option has a working default, so a run needs no configuration beyond a model backend. This page lists every option, where it can be set, and in what order the sources are read.

## Where settings come from

`rlm.Config.load()` builds a configuration from these sources. Later sources win:

1. Built-in defaults.
2. The user file: `$XDG_CONFIG_HOME/rlm-ig/config.toml`, or `~/.config/rlm-ig/config.toml`.
3. The project file: `rlm.toml` in the current directory or the nearest parent that has one. If you pass a path (`--config FILE`, or `rlm.Config.load("path.toml")`) or set `RLM_CONFIG`, that file is used instead of the search. The user file is still read.
4. Environment variables (`RLM_*`, listed below).
5. Overrides: keyword arguments to `rlm.run` and `rlm.Session`, or CLI flags.

Scalar options are replaced by the later source. Tables (`providers`, `pricing`, `mcp`, `sandbox`) are merged key by key, so a project file can add one provider setting without repeating the rest.

Notes that matter in practice:

- An override of `None` is ignored. It does not reset the option. This also means `rlm.run("...", max_usd=None)` leaves the default in place.
- To remove a limit, build the config yourself: `rlm.run("...", config=rlm.Config(max_usd=None))`. TOML has no null, so in a file use a very large number instead.
- When you pass `config=` to `rlm.run`, `rlm.arun` or `rlm.Session`, no files and no environment variables are read. Only that object and your keyword overrides apply.
- An unknown option name in a file, in `Config.load(...)`, or in `rlm.run(...)` raises `ConfigError: unknown option(s): ...`.
- A malformed environment variable raises `ConfigError`, for example `$RLM_MAX_DEPTH='x' is not a valid int`.

## Options

Names are the same in `rlm.toml`, `Config(...)`, and as keyword arguments.

### Models and limits

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `model` | `str` or `Model` | `None` | Root model, `provider:model`. `None` picks the first backend that looks ready. See [Backends](backends.md). |
| `sub_model` | `str` or `Model` | `None` | Model for `ask` calls and sub-agents. Defaults to `model`. |
| `max_depth` | `int` | `2` | Levels of sub-agents allowed below the root. `0` means no `delegate`. Cannot be negative. |
| `max_steps` | `int` | `20` | Turns each agent gets before the forced wrap-up turn. At least 1. |
| `max_calls` | `int` or `None` | `300` | Model calls for the whole run, all agents together. |
| `max_usd` | `float` or `None` | `5.0` | Spend ceiling in USD. Applies when the backend reports cost or can estimate it. |
| `max_tokens` | `int` or `None` | `None` | Input plus output tokens for the whole run. |
| `timeout` | `float` or `None` | `None` | Wall-clock seconds for the whole run. A hard stop at `timeout * 1.5 + 30` seconds ends the run as `failed`. |
| `cell_timeout` | `float` | `120.0` | Seconds one cell may compute. Time spent waiting on sub-calls is not counted. |
| `max_parallel` | `int` or `None` | `None` | Simultaneous model requests, and separately live sub-agents. `None` uses the smaller default of the root and sub-model backends. At least 1. |

`max_calls`, `max_usd`, `max_tokens` and `timeout` must be positive when set. What happens when a limit is reached is described in [Concepts](concepts.md#budgets).

### What the model sees

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `output_limit` | `int` | `8000` | Characters of cell output shown to the model per turn. At least 200. |
| `ask_limit` | `int` or `None` | `None` | Largest `ask` input in characters. `None` uses the sub-model's `window_chars`, 400,000 for the built-in backends. |
| `ask_batch` | `int` | `16` | Most items `ask_map` packs into one sub-model call. `1` turns batching off. At least 1. |
| `ask_batch_chars` | `int` | `24000` | Most characters of item data in one batched call. At least 1. |
| `inherit_tools` | `bool` | `True` | Sub-agents get the parent's host tools unless `delegate(tools=...)` says otherwise. |
| `passthrough_guard` | `bool` | `True` | Refuse `delegate` calls that hand a sub-agent nearly all of the parent's own context. |

### Tracing and output

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `trace` | `bool` | `True` | Write a JSONL trace of every run. |
| `trace_dir` | `str` | `$XDG_STATE_HOME/rlm-ig/traces`, or `~/.local/state/rlm-ig/traces` | Where traces go. |
| `verbosity` | `int` or `str` | `1` | `0` or `"quiet"` or `"silent"`: nothing. `1` or `"normal"`: a line or two per step. `2` or `"verbose"`: also the code and output. Progress goes to stderr, in Python as well as on the CLI. |

### Sandbox, providers, pricing, MCP

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `sandbox` | `SandboxOptions` or `dict` | defaults | How REPL workers are launched and limited. See below. |
| `providers` | `dict` | `{}` | Settings per provider name, and custom provider definitions. See [Backends](backends.md). |
| `pricing` | `dict` | `{}` | USD per million tokens: `{"model-id": {"input": 3.0, "output": 15.0, "cached": 0.3}}`. Used to estimate cost where a backend reports tokens but not money. |
| `mcp` | `dict` | `{}` | MCP servers: `{"name": {"command": ..., "args": [...]}}` or `{"name": {"url": ...}}`. See [Tools and MCP](tools-and-mcp.md). |

## The sandbox table

These are the fields of `[sandbox]` in a file, or of `rlm.SandboxOptions` in Python. An unknown name raises `ConfigError: unknown [sandbox] option(s): ...`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `python` | `str` or `None` | `None` | Interpreter for the worker. `None` means the interpreter running rlm-ig, or `python3` when a launcher is set. |
| `launcher` | `list[str]` or `str` | `[]` | A command prefix that wraps the worker, for example to run it in a container. A string is split on spaces. See [Security](security.md#real-isolation-with-a-launcher). |
| `env` | `dict[str, str]` | `{}` | Environment variables to set in the worker. |
| `pass_env` | `list[str]` | `[]` | Names of your environment variables to pass through to the worker, in addition to the built-in allowlist. |
| `inherit_env` | `bool` | `False` | Pass the whole environment, credentials included. |
| `memory_mb` | `int` or `None` | `None` | Address-space limit for the worker, in megabytes. |
| `cell_timeout` | `float` | `120.0` | Overwritten by the top-level `cell_timeout`. Set it there. |
| `capture_limit` | `int` | `200000` | Characters of a cell's output kept at each end. The middle is dropped. |
| `workdir` | `str` or `None` | `None` | A fixed working directory shared by all workers, not deleted afterwards. `None` gives each agent its own temporary directory. |

## Environment variables

Read by `Config.load` (so not when you pass `config=`):

| Variable | Option |
| --- | --- |
| `RLM_CONFIG` | Path of the config file to use instead of searching for `rlm.toml`. |
| `RLM_MODEL` | `model` |
| `RLM_SUB_MODEL` | `sub_model` |
| `RLM_MAX_DEPTH` | `max_depth` |
| `RLM_MAX_STEPS` | `max_steps` |
| `RLM_MAX_CALLS` | `max_calls` |
| `RLM_MAX_USD` | `max_usd` |
| `RLM_MAX_PARALLEL` | `max_parallel` |
| `RLM_TIMEOUT` | `timeout` |
| `RLM_TRACE_DIR` | `trace_dir` |

An empty variable is treated as unset. `max_tokens`, `cell_timeout`, `output_limit`, `ask_limit`, `ask_batch`, `ask_batch_chars`, `inherit_tools`, `passthrough_guard`, `trace`, `verbosity`, `sandbox`, `providers`, `pricing` and `mcp` have no environment variable.

Others that rlm-ig reads:

| Variable | Used for |
| --- | --- |
| `XDG_CONFIG_HOME`, `XDG_STATE_HOME` | Where the user file and the traces live by default. |
| `RLM_DEBUG_DIR` | An existing directory where the Claude Code backend keeps the raw event stream of each CLI process. See [Tracing](tracing.md#debugging-the-backend). |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` | The Anthropic backend, and default-backend detection. |
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY` | The OpenAI-compatible presets. |
| `OPENAI_BASE_URL` | Base URL for the `openai` provider when `base_url` is not set. |
| `NO_COLOR` | Turns off colour in progress output. |

## CLI flags

Flags are overrides, the last source in the order above:

| Flag | Option |
| --- | --- |
| `-m`, `--model` | `model` |
| `-s`, `--sub-model` | `sub_model` |
| `--max-usd`, `--max-calls`, `--max-steps`, `--max-depth` | the same names |
| `--timeout` | `timeout` |
| `--parallel` | `max_parallel` |
| `--no-trace` | `trace = false` |
| `-q`, `-v` | `verbosity` 0 and 2 |
| `--config FILE` | the config file to use |

## A complete `rlm.toml`

```toml
# Models. Claude Code for the root, a cheaper model for ask() and sub-agents.
model = "claude-code:sonnet"
sub_model = "claude-code:haiku"

# Limits for one run
max_depth = 2
max_steps = 20
max_calls = 300
max_usd = 5.0
max_tokens = 2_000_000
timeout = 900
cell_timeout = 120
max_parallel = 8

# What the model sees
output_limit = 8000
ask_limit = 300000
ask_batch = 16
ask_batch_chars = 24000
inherit_tools = true
passthrough_guard = true

# Tracing and progress
trace = true
trace_dir = "~/.local/state/rlm-ig/traces"
verbosity = "normal"

[sandbox]
memory_mb = 2048
pass_env = ["MY_DATA_TOKEN"]
env = { TZ = "UTC" }
# launcher = ["docker", "run", "--rm", "-i", "--network", "none", "python:3.12-slim"]

[providers.claude-code]
effort = "low"
strip_api_key = true
timeout = 1200

[providers.opencode]
mode = "server"

[providers.anthropic]
max_tokens = 16000
params = { output_config = { effort = "low" } }

[providers.local]
kind = "openai"
base_url = "http://localhost:8000/v1"
params = { temperature = 0 }

[pricing."my-model"]
input = 1.0
output = 3.0
cached = 0.1

[mcp.files]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]
timeout = 60

[mcp.search]
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ..." }
tools = ["query"]
```

`trace_dir` can start with `~`. Values under `[mcp.*]` are the interface described in [Tools and MCP](tools-and-mcp.md).

## The same in Python

```python
import rlm_ig as rlm

config = rlm.Config(
    model="claude-code:sonnet",
    sub_model="claude-code:haiku",
    max_steps=12,
    max_usd=2.0,
    verbosity=0,
    sandbox={"memory_mb": 2048, "pass_env": ["MY_DATA_TOKEN"]},
    providers={"claude-code": {"effort": "low"}},
)
config.validate()

# config= means no files and no environment variables are read.
result = rlm.run("...", context="...", config=config, max_steps=5)   # keyword overrides apply
```

`Config.replace(**changes)` returns a copy with some fields changed and ignores `None` values. `rlm.Config.load(path=None, **overrides)` does the full load described above.
