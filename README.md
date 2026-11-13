# rlm-ig

Ask questions over data that is far too large for a context window.

rlm-ig loads your data into a Python REPL and lets a language model explore it by writing code. The model never has to read the whole thing: it searches, slices and filters with code, fans the parts that need reading out to cheaper sub-models in parallel, starts sub-agents for pieces that are still too big, and returns an answer you can validate against a schema.

It runs on the coding agent you already have. **Claude Code** and **opencode** work out of the box with no API key, and any Anthropic or OpenAI-compatible API works too.

```bash
pip install rlm-ig
rlm "Was there a fatal error, and on which worker?" -f server.log
```

```python
import rlm_ig as rlm

result = rlm.run("Which customers churned twice?", context=open("events.log").read())
print(result.answer)
```

## Contents

- [Why this exists](#why-this-exists)
- [Install](#install)
- [First five minutes](#first-five-minutes)
- [How a run works](#how-a-run-works)
- [Backends](#backends)
- [Python API](#python-api)
- [Command line](#command-line)
- [Limits and cost control](#limits-and-cost-control)
- [What to expect](#what-to-expect)
- [System requirements](#system-requirements)
- [Security](#security)
- [Testing](#testing)
- [Known limits](#known-limits)
- [Documentation](#documentation)

## Why this exists

A model given a 2 MB log either cannot fit it or reads it badly. A model given a REPL that holds the log can do what a person would: check its shape, grep, count, read the three places that matter, and hand the bulk classification work to something cheaper.

This approach is known as a recursive language model (introduced in research by Zhang, Kraska and Khattab at MIT, 2025). rlm-ig is an engine for it built around a few decisions:

- **A real Python process, not a toy interpreter.** The REPL is a native subprocess that starts in about 50 ms, has the whole standard library and whatever packages you have installed, and keeps its state across turns. A runaway cell is interrupted without losing variables; a crashed worker is replaced.
- **Coding agents as first-class backends.** One long-lived Claude Code process (or one opencode server session) per agent, so each turn sends only the new message and reuses the prompt cache. Their own tools are switched off: every computation happens in the REPL, where you can see it.
- **Two kinds of sub-call.** `ask` is one cheap completion for work that only needs reading. `delegate` starts a full sub-agent with its own REPL for work that needs exploring. Both have parallel forms.
- **The engine does the batching.** `ask_map` over 300 short records becomes about 20 calls, not 300, and the answers come back per record.
- **Hard limits that degrade gracefully.** Calls, cost, tokens and wall-clock time are capped per run. When a cap is hit the root agent gets one last turn to submit what it has, and the result is marked `partial` rather than thrown away.
- **Everything is traceable.** Each run writes a JSONL trace you can replay in the terminal or as a self-contained HTML page.

## Install

Requires Python 3.11 or newer. The core has no dependencies.

```bash
pip install rlm-ig                 # Claude Code, opencode, OpenAI-compatible APIs
pip install "rlm-ig[anthropic]"    # adds the Anthropic API backend (official SDK)
```

From a checkout:

```bash
git clone https://github.com/ishaangupta-yb/rlm-ig && cd rlm-ig
pip install -e ".[dev]"
```

You also need at least one model backend:

| Backend | What to do |
| --- | --- |
| Claude Code | Install the `claude` CLI and sign in by running it once |
| opencode | Install the `opencode` CLI and run `opencode auth login` |
| Anthropic API | `pip install "rlm-ig[anthropic]"` and set `ANTHROPIC_API_KEY` |
| OpenAI-compatible | Set `OPENAI_API_KEY` (or `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`), or run Ollama locally |

## First five minutes

**1. Check the setup.** `rlm doctor` shows what is installed and which model would be used. Add `--live` to send one tiny request.

```text
$ rlm doctor --live
runtime
  + python         3.13.18
  + sandbox        REPL worker starts and runs code in 51 ms
backends
  + claude-code    2.1.322 (Claude Code)
  + opencode       1.19.5
  - anthropic      SDK 2.8.0, no ANTHROPIC_API_KEY set
configuration
  + default model  claude-code
live check
  + claude-code    answered 'ready' in 2.1s
```

**2. Watch a complete run offline.** `rlm demo` drives the real engine with a scripted stand-in for the model, so it needs no credentials and costs nothing.

```text
$ rlm demo
rlm  scripted:demo  · context: str, 2,432,029 chars
root   ▸ turn 1  0.2s  First a look at the size and shape of the data.
root     └ ran 0.00s, 1,283 chars of output
root   ▸ turn 2  0.2s  Lines are `timestamp service LEVEL message`. Counting failures per service is exact work…
root   ▸ turn 3  0.1s  Now the reasons for the worst service. I fan the lines out to sub-models in parallel…
root     ⇉ ask_map ×2  43,584 chars
root     ⇇ ask_map done in 0.2s
root   ▸ turn 4  0.2s  A sub-agent double-checks the top reason independently, on a sample of the lines.
root     ⇉ delegate  Double-check: which failure reason is most common in these lines?
  1      ◆ sub-agent Double-check: which failure reason is most common… (21,735 chars)
  1      ▸ turn 1  0.2s  I only need to count the reason fields in my slice.
  1      ✔ gateway_timeout
root   ▸ turn 5  0.2s  Both agree, so I can answer.
root     ✔ submitted
✔ ok  1.2s · 8 calls · 21k in / 453 out · $0.0000
{"service": "billing", "failures": 492, "top_reason": "gateway_timeout", "top_reason_count": 334}
```

**3. Run it on your own data.**

```bash
rlm "What changed between v1 and v2 of the pricing rules?" -f contracts/
cat access.log | rlm "Which IPs look like scanners? Top 10 with evidence." --max-usd 0.50
rlm trace            # replay the last run step by step
```

## How a run works

```text
                 ┌────────────────────────── host process ──────────────────────────┐
  task + data ──▶│  agent loop ◀──── text ────▶ model backend (Claude Code, API…)   │
                 │      │  code                                                      │
                 │      ▼                                                            │
                 │  REPL worker (subprocess)   context, variables, your code         │
                 │      │  ask / ask_map ─────▶ sub-model, one completion each       │
                 │      │  delegate[_map] ────▶ sub-agent: its own loop and REPL     │
                 │      │  tools ─────────────▶ your functions, run in the host      │
                 │      ▼                                                            │
                 │  submit(value) ──▶ schema check ──▶ Result                        │
                 └───────────────────────────────────────────────────────────────────┘
```

Each turn the model replies with a short note and one Python block. The block runs, and what it printed comes back (cut in the middle if long). This is what the model has available:

| In the REPL | What it does |
| --- | --- |
| `context` | Your data: a string of any size, or a dict, list, or any picklable object |
| `peek(x)`, `grep(pattern)`, `chunk(text, size)` | Look at structure, search with context, split on natural boundaries |
| `ask(prompt, data, schema=None)` | One sub-model completion. Returns text, or parsed JSON with a schema |
| `ask_map(prompt, items, schema=None)` | The same prompt over many items, in parallel, batched automatically |
| `delegate(task, data, schema=None)` | A sub-agent with its own REPL where `data` is its `context` |
| `delegate_map(task, items)` | One sub-agent per item, in parallel |
| your tools | Plain Python functions you pass in; they run in your process |
| `submit(value)` | Finish with this answer |

These are ordinary synchronous functions, so the model writes plain Python with no `await`. See [docs/concepts.md](docs/concepts.md) and [docs/repl-api.md](docs/repl-api.md).

## Backends

Models are named `provider:model`. With nothing configured, rlm-ig uses Claude Code if `claude` is on your PATH, then opencode, then the Anthropic API if a key is set.

| Model string | Backend | Notes |
| --- | --- | --- |
| `claude-code`, `claude-code:sonnet`, `claude-code:haiku` | Claude Code CLI | Uses your Claude Code login. Without a model name it uses Claude Code's own default |
| `opencode`, `opencode:anthropic/claude-sonnet-5-5` | opencode CLI | Uses your opencode login and any provider it supports |
| `anthropic:claude-opus-5-5` | Anthropic API | Official SDK, streaming, prompt caching |
| `openai:<model>`, `openrouter:<model>`, `gemini:<model>`, `groq:<model>`, `deepseek:<model>`, `ollama:<model>` | OpenAI-compatible | No extra dependency |
| `<name>:<model>` | Your own | Any endpoint or command-line program, defined in `rlm.toml` |

A cheaper model for the fan-out work is usually the best first optimisation:

```python
rlm.run(task, data, model="claude-code:sonnet", sub_model="claude-code:haiku")
```

Details, settings and billing notes for each backend: [docs/backends.md](docs/backends.md).

## Python API

```python
import rlm_ig as rlm
```

**Typed answers.** `schema` takes a JSON Schema dict, a type hint, a dataclass, a TypedDict or a Pydantic model. A value that does not fit is sent back to the model with the validation errors.

```python
from dataclasses import dataclass

@dataclass
class Summary:
    average_stars: float
    complaints: list[str]
    verdict: str

result = rlm.run("Summarise these reviews.", reviews, schema=Summary)
result.parsed.average_stars     # a Summary instance
```

**Your own tools.** Plain functions, sync or async. They run in your process, so they can use any library and any credentials; the model sees only the signature and docstring.

```python
def price_of(sku: str) -> float:
    """Current unit price of a SKU in USD."""
    return catalog[sku].price

rlm.run("Total this order.", order_text, tools=[price_of])
```

**MCP servers.** Each MCP tool becomes a function in the REPL named `<server>_<tool>`.

```python
rlm.run(task, mcp={"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]}})
```

**Sessions.** Follow-up questions reuse what earlier ones built, because the REPL stays alive. Pass `path=` to keep it between program runs.

```python
with rlm.Session(path="analysis.json") as session:
    session.run("Parse this export into `rows`.", context=export)
    session.run("Which region had the highest revenue?")     # uses rows, no re-parsing
```

**Results.** `run` returns a `Result` and does not raise for things that go wrong during a run.

```python
result.answer        # the submitted value
result.status        # "ok", "partial" (forced wrap-up), or "failed"
result.usage         # calls, input/output/cached tokens, cost
result.trace         # path of the JSONL trace
result.raise_for_status()
```

**Progress.** `on_event=` receives every event as it happens (`model.reply`, `cell`, `call.start`, `agent.end`, …). `arun` is the async form of `run`, and many runs can share one event loop.

Full reference: [docs/python-api.md](docs/python-api.md). Runnable scripts: [examples/](examples/).

## Command line

```text
rlm "task" [-f PATH ...] [-m MODEL] [-s SUB_MODEL] [--schema FILE|JSON] [--session NAME]
           [--json] [-o FILE] [--max-usd N] [--max-calls N] [--max-steps N] [--max-depth N]
           [--timeout S] [--parallel N] [-q | -v]
rlm doctor [--live]      rlm trace [FILE] [--full] [--html OUT]      rlm demo
```

- `-f` takes files or directories and can repeat. `.json`, `.jsonl`, `.csv` and `.tsv` are parsed; a directory becomes a dict of path to text. With no `-f`, piped stdin is the data.
- The answer goes to stdout and progress to stderr, so `rlm … > answer.txt` and pipelines behave.
- Exit code 0 for `ok`, 2 for `partial`, 1 for `failed`.

Project defaults live in `rlm.toml` (see [examples/rlm.toml](examples/rlm.toml) and [docs/configuration.md](docs/configuration.md)).

## Limits and cost control

| Option | Default | Meaning |
| --- | --- | --- |
| `max_usd` | 5.00 | Spend ceiling for the run |
| `max_calls` | 300 | Model calls, all agents combined |
| `max_steps` | 20 | Turns per agent |
| `max_depth` | 2 | Levels of sub-agents below the root |
| `max_tokens` | none | Total input plus output tokens |
| `timeout` | none | Wall-clock seconds |
| `cell_timeout` | 120 | Seconds one cell may compute (waiting on sub-calls is not counted) |

A call is reserved against the budget before it is made, so parallel agents cannot overshoot together. Cost is whatever the backend reports. Claude Code reports a figure even under a subscription: that is the API-equivalent price, not a charge to you. Backends that report only tokens get an estimate when the model has an entry in the price table, and are otherwise limited by calls and tokens.

## What to expect

Measured in the development sandbox (Linux, 4 vCPUs, Claude Code 2.1.322 serving Claude Haiku 5.5 as both root and sub-model, default settings). Your numbers depend mostly on the model's speed.

| Task | Data | Result | Time | Calls | Cost |
| --- | --- | --- | --- | --- | --- |
| Find one planted line in a log | 488 KB text | exact | 5 to 7 s | 2 to 4 | $0.0005 |
| Same question from the CLI over a file | 3.7 MB text | exact | 4.9 s | 2 to 3 | $0.0008 |
| Classify 300 support tickets into 4 categories (needs reading, not keywords) | 350 KB text | 296 of 300 correct | 23 s | 22 | $0.028 |
| Top expense category per department, one sub-agent each | 519 KB, 6 documents | exact | 18 s | 16 | $0.0065 |
| Order total using two host tools | 1 short order | exact | 4.3 s | 2 | $0.0006 |
| Three chained questions in one session | 5,000-row CSV | consistent | 15 s | 7 | $0.0025 |

Other things worth knowing before you rely on it:

- **A turn through Claude Code takes about 1 to 3 seconds** with Haiku, longer with larger models. The REPL itself is not the bottleneck: a cell that scans 100 MB of text takes well under a second.
- **Batching trades a little per-item attention for a lot of speed.** The 300-ticket run above made 22 calls. With `ask_batch=1` it makes one call per ticket, which took over three minutes and ran into the default 300-call limit, for roughly the same accuracy with this model.
- **Small models occasionally fumble a turn.** In testing Haiku sometimes replied without a code block or with a mangled one. The engine reminds it, salvages what it can, and the run continues; you will see this as an extra turn in the trace. Stronger root models do it less.
- **Upstream stalls happen.** A request that produces nothing but keep-alives for 90 seconds is abandoned and the conversation is rebuilt from its transcript, instead of waiting out the CLI's much longer internal timeout.
- **Answers can still be wrong.** The model decides what to read. Use `-v` or `rlm trace --full` to see exactly what it did, and prefer tasks where code can verify the result.

## System requirements

| | Minimum | Recommended |
| --- | --- | --- |
| OS | Linux, or macOS; Windows through WSL | Linux or macOS |
| Python | 3.11 | 3.12 or 3.13 |
| CPU | 2 cores | 4 or more |
| Memory | 2 GB free | 8 GB or more with Claude Code and parallel fan-out |
| Disk | under 1 MB for the package, plus traces | SSD |
| Network | Whatever your model backend needs | |
| Backend | One of: Claude Code CLI, opencode CLI, an API key, or a local Ollama | Claude Code with a strong root model and a cheap sub-model |

Where the memory goes, measured:

- rlm-ig itself: about 30 MB.
- Each REPL worker: about 20 MB plus the size of its context (a 100 MB context gives a 114 MB worker). The root has one worker and each live sub-agent has one.
- Each Claude Code process: about 200 MB. One exists per live agent and per in-flight `ask`. With the default parallelism of 4 that is roughly 1 to 2 GB at peak; `--parallel` moves it up or down.
- API backends add almost nothing per call, and default to 8 parallel requests.

To try it on the smallest possible setup, use an API backend with `--parallel 2`: a 2 GB machine is enough. To get the most out of it, use Claude Code or an API with a strong root model, a fast cheap sub-model, and `--parallel 8` on a machine with 8 GB or more.

## Security

The REPL worker is an ordinary subprocess running as you. **It is not a security boundary.** Model-written code can read your files and use your network. What rlm-ig does do:

- Credentials are not passed into the worker's environment unless you list them.
- Each worker runs in a throwaway directory, cells have a time limit, and memory can be capped.
- The host only ever parses JSON from a worker. It never unpickles anything a worker produced.
- Claude Code and opencode run with their own tools disabled, in a temporary directory.

For real isolation, wrap the worker with `sandbox.launcher` (a container or jail). The worker's source is shipped over stdin, so nothing needs to be mounted. See [docs/security.md](docs/security.md), including the note on prompt injection from the data you load.

## Testing

```bash
pip install -e ".[dev]"
pytest                      # 437 tests, about 2 minutes, no network or credentials
ruff check src tests
```

The suite runs the real engine and real REPL workers. Models are replaced by a scripted backend, and each real backend is exercised against a protocol-level fake: a stand-in `claude` CLI that speaks stream-json, local OpenAI and Anthropic API servers, and stdio and HTTP MCP servers. If the `opencode` binary is on your PATH, its tests drive the real binary against the local fake API.

To check a live backend yourself:

```bash
rlm doctor --live                          # one round trip to your default model
python examples/02_long_document.py        # a real question over a generated 4 MB log
RLM_MODEL=claude-code:haiku python examples/05_session.py
```

## Known limits

Stated plainly, so you know what has and has not been proven:

- Developed and tested on Linux with Python 3.11 and 3.13. macOS should behave the same. Native Windows is untested; use WSL.
- **Claude Code** was tested live, extensively (Claude Code 2.1.x).
- **opencode** was tested by driving the real binary (1.19.x) in both server and run modes against a local model endpoint. It was not run against a hosted provider in the development environment.
- The **Anthropic** and **OpenAI-compatible** backends were tested against local servers that implement the wire formats, not against the live services.
- The **MCP client** was tested against conformance fakes for both transports, not against third-party servers.
- The `sandbox.launcher` mechanism is tested; the Docker command line in the docs is an example that was not run.
- A sub-agent cannot see its parent's variables. Everything it needs has to be passed in the call.
- Sessions save picklable variables and REPL-defined functions. Open files, locks, generators and the like are skipped and reported.

## Documentation

The guide is in [docs/](docs/README.md):

[Quickstart](docs/quickstart.md) · [Concepts](docs/concepts.md) · [REPL reference](docs/repl-api.md) · [Backends](docs/backends.md) · [Configuration](docs/configuration.md) · [Python API](docs/python-api.md) · [Tools and MCP](docs/tools-and-mcp.md) · [Sessions](docs/sessions.md) · [Tracing](docs/tracing.md) · [Security](docs/security.md) · [Troubleshooting](docs/troubleshooting.md)

## License

MIT. See [LICENSE](LICENSE).
