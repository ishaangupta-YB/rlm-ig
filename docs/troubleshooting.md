# Troubleshooting

Find the symptom, read the cause, apply the fix. The messages quoted here are the ones rlm-ig prints. For a run that finished but gave a bad or incomplete answer, skip to [Reading a trace](#reading-a-trace).

The first command to run is always:

```bash
rlm doctor
rlm doctor --live      # also sends one tiny request to the default model
```

## Setup and backends

### `No model is configured and none could be detected.`

No `model` is set, and rlm-ig found no `claude` or `opencode` on `PATH` and no `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`. The message lists the options. Pick one:

- Install and sign in to Claude Code or opencode, and the default is found automatically.
- Name a model: `-m claude-code`, `model = "..."` in `rlm.toml`, or `RLM_MODEL`.
- For an OpenAI-style key, name the model yourself: `-m openai:<model-id>`. These keys are never used for auto-detection.

### `Claude Code was not found (looked for 'claude' on PATH)`

The `claude` binary is not on `PATH` in the environment `rlm` runs in. Install Claude Code and run `claude` once to sign in. If it has another name or location, set `[providers.claude-code] command = "/path/to/claude"`. The same applies to `opencode was not found ...` (install from opencode.ai, sign in with `opencode auth login`, or set `[providers.opencode] command`).

### Authentication errors

| Message | Meaning and fix |
| --- | --- |
| `Claude Code reported an error: ...` followed by `Run `claude` once to sign in.` | The CLI is not signed in, or its login expired. Run `claude` yourself and sign in. |
| `opencode reported an error: ... Check `opencode auth list` and sign in with `opencode auth login`.` | Same for opencode, for the provider your model belongs to. |
| `anthropic:...: authentication failed (...). Check ANTHROPIC_API_KEY.` | The key was rejected. Check it and its account. |
| `openai:...: needs an API key: set $OPENAI_API_KEY` | The preset's key variable is not set in the environment `rlm` runs in. |
| `...: HTTP 401: ... Check the API key in $OPENAI_API_KEY.` | The key was sent and rejected. |
| `...: HTTP 404: ... Check the model id and base_url.` | The endpoint does not know that model, or `base_url` is wrong. |
| `... not found (...). Check the model id.` (Anthropic) | The model id does not exist for your account. |

If you use Claude Code and an `ANTHROPIC_API_KEY` is exported, the CLI may use the key rather than your subscription login. `rlm doctor --live` shows the auth source the CLI reports. To use the login, set `[providers.claude-code] strip_api_key = true`.

### `the anthropic backend needs the Anthropic SDK`

Run `pip install "rlm-ig[anthropic]"`. Default-backend detection only checks that `ANTHROPIC_API_KEY` is set, not that the SDK is installed.

### A `TypeError: Could not resolve authentication method` traceback from the Anthropic backend

With the SDK version we tested, running `anthropic:...` with no credentials raises this `TypeError` from inside the SDK, and the CLI prints a Python traceback instead of a one-line message. It means no API key was found. Set `ANTHROPIC_API_KEY`, or `api_key_env` in `[providers.anthropic]`.

### `unknown model 'x'. Use 'provider:model' with a provider from: ...`

The string is not `provider:model` with a known provider. Examples that work: `claude-code`, `claude-code:sonnet`, `opencode:anthropic/claude-sonnet-5-5`, `anthropic:claude-opus-5-5`, `openai:<model-id>`. A provider you defined yourself needs a colon even when the model part is empty: write `mycli:`, not `mycli`. For a custom provider, check the table name and `kind` in `[providers.NAME]`.

### `opencode models are written provider/model`

Use `opencode:anthropic/claude-sonnet-5-5`, not `opencode:claude-sonnet-5-5`. `opencode models` lists what you have.

### `[providers.NAME] needs command = ["program", "arg", ...]`

A `kind = "command"` provider has no `command`. For a script, give the absolute path: the program runs in a throwaway working directory, so a relative path will not be found.

### Claude Code turns time out or stall

| Message | Fix |
| --- | --- |
| `Claude Code did not finish a turn within 900s` | The model needs longer than `timeout`. Raise `[providers.claude-code] timeout`. |
| `Claude Code made no progress for 90s (upstream stall)` | The upstream request hung. rlm-ig rebuilds the session and replays the conversation, up to `retries` times. Raise `stall_timeout` only if your model is slow to start answering. |
| `Claude Code exited unexpectedly (exit code N): ...` | The CLI died. The tail of its stderr is included. If it mentions login, sign in. |

If the CLI keeps misbehaving, set `RLM_DEBUG_DIR` to an existing directory to capture its raw event stream. See [Tracing](tracing.md#debugging-the-backend). A missing directory crashes the run with `FileNotFoundError`.

### `the model declined to answer` or `the model declined the request (refusal category: ...)`

The backend refused. For the Anthropic API, refusal fallback is on by default for supported models and retries on the fallback model Anthropic recommends. Check the trace for what was sent.

### Rate limits and server errors

OpenAI-compatible endpoints retry on 408, 409, 425, 429, 5xx and 529 with backoff, up to `retries` (default 4). The Anthropic SDK retries on its own, also `retries` (default 4). After that you get `rate limited after retries` or `HTTP N`. Lower `max_parallel` (or `--parallel`) so fewer requests are in flight, or use a cheaper `sub_model`.

## Results and limits

### The result is `partial`

The answer was built in one forced wrap-up turn because the root agent ran out of turns or a budget ran out. `result.note` says which:

```text
answer produced by a forced wrap-up (out of turns)
answer produced by a forced wrap-up (budget spent: call limit reached (300 of 300 model calls))
answer produced by a forced wrap-up (budget spent: cost limit reached ($5.0012 of $5.00))
answer produced by a forced wrap-up (budget spent: token limit reached (...))
answer produced by a forced wrap-up (budget spent: time limit reached (...))
```

On the CLI the exit code is 2. The answer is real but may be incomplete, and the model was told to say so. To fix it:

- **Out of turns.** Raise `max_steps`. Or find out why it used so many (see [Reading a trace](#reading-a-trace)): repeated errors, or a model that explores in tiny steps.
- **Call limit.** Raise `max_calls`, or reduce calls. A loop of `ask` over many items should be one `ask_map`, which also batches small items.
- **Cost limit.** Raise `max_usd`, use a cheaper `sub_model`, or narrow the data with code before it reaches a model. On Claude Code with a subscription, the figure is an API-equivalent one and nothing is charged, but `max_usd` still limits it.
- **Time limit.** Raise `timeout`. A hard stop at `timeout * 1.5 + 30` seconds ends the run as `failed` with `run exceeded its time limit of Ns`.

### The result is `failed`

`result.error` says why, as `ErrorType: message`. Common ones:

- `StepsExhausted: agent 0 did not submit an answer (out of turns)`: the forced wrap-up turn did not call `submit`. Raise `max_steps`, or use a stronger model.
- `StepsExhausted: agent 0: final answer did not match the schema (...)`: the model could not produce a value that satisfies your `schema`, even after being shown the errors. Loosen the schema or check that it is satisfiable from the data.
- `ModelError: ...`: the backend failed. The message names it.
- `SandboxError: sandbox failed to start: ...`: the REPL worker could not start. See [sandbox failed to start](#sandbox-failed-to-start).

### `max_usd` or another limit seems to be ignored

- `rlm.run("...", max_usd=None)` does nothing: `None` overrides are ignored. To remove a limit, pass `config=rlm.Config(max_usd=None)`.
- A backend that reports no cost cannot be limited by `max_usd`. rlm-ig prints `note: ... does not report cost, so max_usd cannot be enforced for it; max_calls and max_tokens still apply (add a [pricing] entry to estimate cost)`. Add a `[pricing."model-id"]` entry. See [Backends](backends.md#pricing-and-cost).
- A run using `config=` reads no `rlm.toml` and no environment variables.
- `[sandbox] cell_timeout` in a file has no effect. Set `cell_timeout` at the top level.

### The `rlm trace` of a run ends with `result  (trace ends without a result...)`

The run was killed or interrupted before it finished, or it is still going. The trace is written as the run happens, so everything up to that point is there.

## Model replies

### `No code block was found in your reply, so nothing ran.`

The model replied without a fenced ```` ```python ```` block. It is told so and the turn is spent. In the trace, `rlm trace` shows `(no code block in this reply)` for that turn. Causes and fixes:

- The model answered in prose. After two prose replies in a row, rlm-ig accepts the latest as the answer and records the note `accepted a prose reply as the answer`. When a `schema` is set, prose is never accepted and the run ends in `StepsExhausted`.
- The model used another format, such as tool-call syntax or XML. The message tells it this does not work. A stronger or more instruction-following model fixes it.
- The reply was cut off before the code block started. Look at `model.reply` in the trace, and check for `note: the model's output ran away and was cut off`.

### `ask()` or `ask_map()` fails

| Message | Meaning and fix |
| --- | --- |
| `ValueError: ask() input is N characters; one call handles about M. Split it with chunk() and use ask_map(), or hand it to delegate().` | Prompt plus data is larger than `ask_limit` (default: the sub-model's window, 400,000 characters). Split with `chunk` and use `ask_map`, narrow with code first, or use `delegate`. Raise `ask_limit` only if the sub-model can really take more. |
| `SubcallError: ModelError: the sub-model did not return valid JSON for the schema: ...` | The sub-model answered with something that does not parse or satisfy the `schema`, twice. Simplify the schema, give a clearer prompt, or use a stronger `sub_model`. |
| `[ask_map] 2 of 10 items failed; failed entries are falsy Failed(...) objects` | Some items failed. The model's code gets `Failed` objects for them, and should redo only those. The error text is in `item.error`. |
| `SubcallError: ModelError: ...` from `ask_map` | Every item failed, so rlm-ig raised. Usually the backend is down, a limit was hit, or credentials are wrong. |
| `note: ask_map: a batch of N items fell back to single calls (...)` | The sub-model did not return a usable JSON array for a batched call, so the items were asked one by one. Output is still correct but costs more calls. If it happens often, use a stronger `sub_model` or set `ask_batch = 1`. |

### `this would hand a sub-agent N characters, 100% of your own context`

This is the pass-through guard. The model called `delegate` with almost all of its own context, which only moves the problem one level down. The model sees the message and should narrow the data (filter, slice, or `chunk` and `delegate_map` or `ask_map`). If a single pass over everything is really wanted, the model passes `whole=True`. To turn the guard off, set `passthrough_guard = false`.

### `sub-agents are not available at this level`

`delegate` was called at the deepest level. Raise `max_depth` if you want more levels. The default is 2. The model is told at the start which level it is on.

### `StepsExhausted: agent 0.2 did not submit an answer` inside a cell

A sub-agent ran out of turns, and its parent got a `SubcallError`. A sub-agent's turns are `max_steps` each. Look at that agent's section of the trace. Often the task given to it was too vague, or too large for one agent.

## Slow runs

A run takes as long as its model calls take, plus code. To see where the time goes, run `rlm trace` and look at the seconds on each turn, and at `call` lines for sub-calls. Usual causes:

- **Many turns.** Every turn is a model round trip. Look for repeated errors, or exploration in tiny steps. A stronger model usually takes fewer turns.
- **Sequential sub-calls.** A Python `for` loop that calls `ask` once per item runs them one after another. `ask_map` runs them in parallel and batches small items. The model is told this, but weaker models forget.
- **Sub-agents.** Each `delegate` is a whole conversation. See whether an `ask` would do.
- **Parallelism is capped.** The default is small for the CLI backends (4). Raise `max_parallel` or `--parallel` if your account allows more concurrent requests.
- **Expensive turns.** Use a cheaper `sub_model` for `ask` and sub-agents, and `[providers.claude-code] effort = "low"` where the CLI supports it.
- **The cell itself.** `ran 12.40s` on a cell line means the code was slow. The model may be doing something heavy over the whole `context`.

## The command line

### `rlm` hangs with no output

When you give no `-f` and stdin is not a terminal, `rlm` reads stdin until it closes. In a script, a CI job, or a tool runner, stdin may be open and silent. Redirect it: `rlm "..." < /dev/null`, or pass `-f`. This applies to `--session` follow-ups too.

### `rlm: no traces found in ...` or `no traces in ...`

Nothing has run since the trace directory was set, or tracing is off. `rlm doctor` shows the directory. Check `RLM_TRACE_DIR`, `trace_dir`, and `--no-trace`.

### Exit codes

0 for `ok`, 2 for `partial`, 1 for `failed` or an error before the run, and 130 after Ctrl-C. A `failed` result prints no answer on stdout, and `-o FILE` does not write the file. The error is shown in the progress output, and with `-q` or `--json` also on stderr as `rlm: <error>`. With `--json`, stdout still carries the JSON result, with `status` and `error` set.

## The REPL

### sandbox failed to start

The REPL worker, or the launcher wrapped around it, exited before the worker was ready. The message gives the exit code and the last few meaningful lines of its stderr, joined with ` | `, with boilerplate such as `Run 'docker run --help' for more information` left out. When a launcher or the Docker preset is in use, a hint for the common causes follows. `rlm doctor` shows the same message in its sandbox row, with the launcher it tried. This is the row for a sandbox image that has not been pulled:

```text
  x sandbox        sandbox failed to start: exit code 125; Unable to find image 'ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0' locally | docker: Error response from daemon: failed to resolve reference "ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0": ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0: not found. Hint: ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0 is not available locally or in its registry; pull it with `docker pull ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0`, or choose another image with --sandbox docker:IMAGE (launcher: docker run --rm -i --network none --read-only --tmpfs /tmp -w /tmp --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256 --user 65534:65534 ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0)
```

The hints cover three causes:

| Cause | Fix |
| --- | --- |
| The image is not pulled, or not published (`Unable to find image`, `pull access denied`, `manifest unknown`, `failed to resolve reference`) | `docker pull IMAGE`, or pick another image with `--sandbox docker:IMAGE`, for example `--sandbox docker:python:3.12-slim` |
| Docker is not running (`failed to connect to the docker API`, `Cannot connect to the Docker daemon`) | Start Docker Desktop or the docker service, then check that `docker info` works |
| The image has no `python3` (`executable file not found`) | Use an image with Python 3, or set `sandbox.python` to the interpreter's path in the image |

Any other failure, such as a mistyped flag in a custom `sandbox.launcher`, shows the launcher's own error without a hint: `exit code 125; unknown flag: --bogus`. Run the launcher command by hand to see all of its output. Without a launcher, check `sandbox.python`.

### `The REPL process stopped (...). It was restarted: `context` is back, but every other variable is gone.`

The worker died (a crash, `os._exit`, the memory limit, a kill), or a cell ignored its time limit, or a cell ran past `cell_timeout` under a [launcher](security.md#real-isolation-with-a-launcher), where it cannot be interrupted. The model is told and normally recomputes. If this happens on legitimate work, raise `cell_timeout` or `sandbox.memory_mb`, or have the model do less per cell.

### `Interrupted: the cell ran past its time limit and was stopped. Variables are intact.`

A cell computed for longer than `cell_timeout` (default 120 s). Time spent waiting on `ask`, `delegate` or a tool is not counted. Raise the limit if the work is legitimate.

### `NameError` after restoring a saved session

Imports are not saved. A function restored from a session file that uses `re`, `json` or another module needs the import again. See [Sessions](sessions.md#what-is-not-saved).

### A tool returns an object and the cell fails with `AttributeError` or `ModuleNotFoundError`

A non-JSON return value is pickled in your process and unpickled in the worker, so its class must be importable there. A class defined in your `__main__` is not. Return plain data, or put the module on `PYTHONPATH`. See [Tools and MCP](tools-and-mcp.md#arguments-and-results).

### `ToolError: ... failed: ...`

A tool raised. The message names it and the original exception. For MCP tools, `calc_add takes keyword arguments only: ...` means the model called it with positional arguments, and `MCP server 'x' did not answer (...)` means a timeout or a dead server.

## Configuration

| Message | Fix |
| --- | --- |
| `unknown option(s): foo` | A name in `rlm.toml`, `Config.load`, or a keyword to `rlm.run` is not a `Config` field. See [Configuration](configuration.md). |
| `unknown [sandbox] option(s): foo` | Same, in the `[sandbox]` table. |
| `config file not found: PATH` | `--config` or `$RLM_CONFIG` points at a file that is not there. |
| `PATH: Expected ...` (a TOML parse error) | Fix the syntax in that file. |
| `$RLM_MAX_DEPTH='x' is not a valid int` | An environment variable has the wrong type. |
| `max_steps must be at least 1`, `output_limit must be at least 200 characters`, `max_usd must be positive (or None to disable it)` | A value is out of range. |
| `tool name 'x' is not a valid Python identifier; pass name=...` | Give the tool a name with `@tool(name="...")`. |
| `tool name 'ask' collides with a built-in REPL name` | Rename it. |
| `MCP server 'x' ...` | See [Tools and MCP](tools-and-mcp.md#when-servers-start). |

## Python API

### `run() cannot be called from a running event loop; use await arun(...) instead`

You called `rlm.run` inside async code, a notebook cell, or a framework with a running loop. Use `await rlm.arun(...)`.

### `this Session method blocks; inside an event loop use the a-prefixed coroutine instead`

The same for `Session.run`, `variables`, `save` and `close`. Use `arun`, `avariables`, `asave` and `aclose`.

### Session file errors

```text
could not read the session file PATH: ...
PATH was written by an incompatible version (state version N)
```

The file is not valid JSON (a half-written or foreign file), or it was written by a different version of the state format. Delete it, or point `path` somewhere else. Variables that could not be restored are listed in `session.skipped`.

## Reading a trace

When a result is wrong, slow, or incomplete, the trace shows what the model actually did. Start with the outline, then look closer.

```bash
rlm trace            # the outline: result, notes, turns, calls
rlm trace --full     # plus code and output for every turn
rlm trace --html t.html
```

This is a real `rlm trace --full` of a run that came back `partial` (shortened paths, scripted model):

```text
trace   ~/.local/state/rlm-ig/traces/20261113-075016-how-many-refund-requests-are-there-ccf3.jsonl
task    How many refund requests are there?
model   scripted:scripted
data    context: str, 26 chars
result  partial in 0.1s, 8 calls · 4.9k in / 75 out · $0.0000
        1 agent(s), 4 turn(s)
note    answer produced by a forced wrap-up (out of turns)
note    ask_map: a batch of 3 items fell back to single calls (upstream 500)
note    accepted a prose reply as the answer

root  [26 chars of context]
  turn 1 (0.0s)  Look for refunds.
      > hits = grep('refund')
      > print(len(hits))
      | 3
  turn 2 (0.0s)  Count them.
      > totals = {}
      > for h in hits:
      >     totals[h.match] = totals.get(h.mach, 0) + 1
      > print(totals)
      error: AttributeError: 'Hit' object has no attribute 'mach'. Did you mean: 'match'?
      | Traceback (most recent call last):
      |   <cell 2>, line 3, in <module>
      |     totals[h.match] = totals.get(h.mach, 0) + 1
      | AttributeError: 'Hit' object has no attribute 'mach'. Did you mean: 'match'?
  turn 3 (0.0s)  Classify the hits.
      > r = ask_map('Classify.', [1, 2, 3])
      > print(r)
      call  ask_map x3 0.0s  30 chars  (1 failed)
      | [ask_map] 1 of 3 items failed; failed entries are falsy Failed(...) objects
      | ['ok', Failed('ModelError: upstream 500'), 'ok']
  turn 4 (0.0s)  I could not finish; there were 3 hits.
      (no code block in this reply)
  -> I could not finish; there were 3 hits.

answer
I could not finish; there were 3 hits.
```

How to read it:

1. **The `result`, `error` and `note` lines at the top** say how it ended. `partial` plus the note tells you whether it was turns or a budget.
2. **The `note` lines** are things the engine decided or noticed. A fall-back, a prose reply accepted as the answer, a runaway reply cut off, or a missing cost figure all show up here.
3. **Each `turn` line** has the model's own words before its code. A model that says what it intends is easier to follow. Read them in order and find where it went off track.
4. **`error:` lines** are cells that raised. Several in a row with the same message means the model is stuck on a mistake, which is a prompt or model-strength problem rather than a rlm-ig problem. In the example, a typo (`h.mach`) cost a turn.
5. **`call` lines** show sub-calls: how many items, how long, `(N failed)` for failed items, and `FAILED: ...` for a call that raised. Failed calls point at the backend or at a limit.
6. **`(no code block in this reply)`** is a turn spent on a reply that had nothing to run.
7. **Sub-agents** are nested under the turn that started them, each with its own turns. A sub-agent that ends with `-> failed: ...` tells you why the `delegate` raised.
8. **The seconds on each turn and call** show where the time went.

For more detail than the outline, read the raw trace. Each `model.reply` has the model's whole reply, each `cell` the code and the exact output the model saw, and `run.end` the usage. `jq` makes quick work of it:

```bash
jq -c 'select(.kind == "cell" and .error) | {agent, step, output}' trace.jsonl
jq -c 'select(.kind == "note") | .message' trace.jsonl
jq -c 'select(.kind == "call.end" and (.ok | not)) | {agent, op, error}' trace.jsonl
```

If the trace shows the engine doing something unexpected rather than the model, a trace file plus the HTML report is what to attach to a bug report. Trim anything sensitive first. See [Tracing](tracing.md).
