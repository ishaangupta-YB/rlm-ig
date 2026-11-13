# Tracing

Every run records what happened: each model reply, each cell of code with its output, each sub-call, and the outcome. The same stream of events feeds the progress output on your terminal, the trace file on disk, and your own `on_event` callback.

## The trace file

By default a trace is written to `$XDG_STATE_HOME/rlm-ig/traces/`, or `~/.local/state/rlm-ig/traces/`. The file name is `YYYYmmdd-HHMMSS-<task slug>-<4 hex>.jsonl`, for example `20261113-073205-which-service-had-the-most-payment-failu-2883.jsonl`. Change the directory with `trace_dir` or `RLM_TRACE_DIR`. Turn tracing off with `trace = false`, `trace=False` or `--no-trace`.

The format is JSON Lines: one JSON object per line, one line per event, written and flushed as the run happens. A run that is killed leaves a valid file with no `run.end` line, and readers skip a truncated last line. If the trace directory cannot be written, the trace is silently dropped and the run carries on. `Result.trace` is then `None`.

Every line has these keys:

| Key | Meaning |
| --- | --- |
| `kind` | The event kind, listed below. |
| `agent` | The agent id: `"0"` for the root, `"0.1"` for its first sub-agent, `"0.1.2"` for that agent's second, and `""` for run-level events. |
| `depth` | 0 for the root, 1 for its sub-agents, and so on. |
| `t` | Seconds since the run started. |

The rest of the keys depend on the kind.

### Event kinds

| Kind | When | Fields |
| --- | --- | --- |
| `run.start` | Once, first. | `task`, `model`, `sub_model`, `summary`, `limits` (`max_depth`, `max_steps`, `max_calls`, `max_usd`, `max_tokens`, `timeout`, `parallel`; `null` where unlimited), `tools` (names). |
| `agent.start` | An agent's REPL is ready. | `task`, `parent` (id or `null`), `context_size` (characters), `summary` (first line of the data summary). |
| `model.reply` | An agent's turn was answered. | `step`, `text` (the whole reply), `seconds`, `usage` (`input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`), `model`. |
| `cell.start` | Before a cell runs. | `step`, `code`. |
| `cell` | After it ran. | `step`, `code`, `output` (cut to `output_limit` as the model saw it), `error` (bool), `seconds`, `submitted` (bool). |
| `call.start` | The code called `ask`, `ask_map`, `delegate`, `delegate_map` or a tool. | `call` (number), `op`, `n` (items), `chars` (size of the input), `name` (tool name, or the first line of a delegate task). |
| `call.end` | That call finished. | The same fields, plus `seconds`, `ok`, `failed` (items that failed), and `error` when `ok` is false. |
| `agent.end` | An agent finished. | `status` (`ok`, `partial`, `failed`, `cancelled`), `steps`, and `answer` (cut to 2,000 characters) or `error`. |
| `note` | Something worth knowing happened. | `message`. For example `accepted a prose reply as the answer`, `the model's output ran away and was cut off; its session was restarted`, a notice that the backend reports no cost, or `ask_map: a batch of 3 items fell back to single calls (...)`. |
| `run.end` | Once, last. | `status`, `answer` (cut to 4,000 characters, `null` on failure), `error`, `note`, `usage` (`calls`, `input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`), `usage_by_model`, `usage_text`, `seconds`, `steps`, `agents`, `trace` (the file's path). |

A turn produces `model.reply`, then `cell.start`, then the `call.start` and `call.end` pairs the code makes, then `cell`. Sub-agents' events are interleaved with their parent's, in time order, distinguished by `agent`.

Real lines from a run (long fields shortened with `...`):

```json
{"kind": "run.start", "agent": "", "depth": 0, "t": 0.0, "task": "Which service had the most payment failures ...", "model": "scripted:demo", "sub_model": "scripted:demo", "summary": "context: str, 2,432,029 chars", "limits": {"max_depth": 2, "max_steps": 20, "max_calls": 300, "max_usd": null, "max_tokens": null, "timeout": null, "parallel": 16}, "tools": []}
{"kind": "agent.start", "agent": "0", "depth": 0, "t": 0.129, "task": "Which service had the most payment failures ...", "parent": null, "context_size": 2432029, "summary": "context: str, 2,432,029 chars, 40,000 lines"}
{"kind": "model.reply", "agent": "0", "depth": 0, "t": 0.28, "step": 1, "text": "First a look at the size and shape of the data.\n\n```python\npeek()\nprint(context[:300])\n```", "seconds": 0.151, "usage": {"input_tokens": 1291, "output_tokens": 22, "cached_tokens": 0, "cost_usd": 0.0}, "model": "scripted:demo"}
{"kind": "cell", "agent": "0", "depth": 0, "t": 0.284, "step": 1, "code": "peek()\nprint(context[:300])", "output": "context: str, 2,432,029 chars, 40,000 lines\n  starts: ...", "error": false, "seconds": 0.003, "submitted": false}
{"kind": "call.start", "agent": "0", "depth": 0, "t": 0.6, "call": 1, "op": "ask_map", "n": 2, "chars": 43584, "name": ""}
{"kind": "call.end", "agent": "0", "depth": 0, "t": 0.752, "call": 1, "op": "ask_map", "ok": true, "seconds": 0.151, "failed": 0, "n": 2, "chars": 43584, "name": ""}
{"kind": "agent.end", "agent": "0.1", "depth": 1, "t": 1.13, "status": "ok", "steps": 1, "answer": "gateway_timeout"}
{"kind": "run.end", "agent": "", "depth": 0, "t": 1.29, "status": "ok", "answer": "{\"service\": \"billing\", ...}", "error": null, "note": null, "usage": {"calls": 8, "input_tokens": 20956, "output_tokens": 453, "cached_tokens": 0, "cost_usd": 0.0}, "usage_by_model": {"scripted:demo": {"calls": 8, "...": "..."}}, "usage_text": "8 calls · 21k in / 453 out · $0.0000", "seconds": 1.289, "steps": 6, "agents": 2, "trace": "~/.local/state/rlm-ig/traces/20261113-073205-which-service-had-the-most-payment-failu-2883.jsonl"}
```

Reading a trace with plain tools works too:

```bash
jq -c 'select(.kind == "cell" and .error) | {agent, step, output}' trace.jsonl   # failed cells
jq -r 'select(.kind == "model.reply") | .text' trace.jsonl                      # everything the model said
```

## `rlm trace`

```bash
rlm trace                  # the most recent run
rlm trace PATH             # a specific file
rlm trace --full           # include the code and output of every step
rlm trace --list           # recent traces
rlm trace --html out.html  # a standalone HTML report
```

The default view is an outline. This is `rlm trace` for the demo run:

```text
trace   ~/.local/state/rlm-ig/traces/20261113-073205-which-service-had-the-most-payment-failu-2883.jsonl
task    Which service had the most payment failures in this log, and what was the most common failure reaso…
model   scripted:demo
data    context: str, 2,432,029 chars
result  ok in 1.3s, 8 calls · 21k in / 453 out · $0.0000
        2 agent(s), 6 turn(s)

root  [2,432,029 chars of context]
  turn 1 (0.2s)  First a look at the size and shape of the data.
  turn 2 (0.2s)  Lines are `timestamp service LEVEL message`. Counting failures per service …
  turn 3 (0.2s)  Now the reasons for the worst service. I fan the lines out to sub-models in…
      call  ask_map x2 0.2s  43,584 chars
  turn 4 (0.2s)  A sub-agent double-checks the top reason independently, on a sample of the …
      call  delegate 0.2s  Double-check: which failure reason is most common in these lines? Reply with it…
      sub-agent 1: Double-check: which failure reason is most common in these …  [21,735 chars of context]
        turn 1 (0.2s)  I only need to count the reason fields in my slice.
            submitted
        -> gateway_timeout
  turn 5 (0.1s)  Both agree, so I can answer.
      submitted
  -> {"service": "billing", "failures": 492, "top_reason": "gateway_timeout", "top_reason_count": 334}

answer
{"service": "billing", "failures": 492, "top_reason": "gateway_timeout", "top_reason_count": 334}
```

Each `turn` line shows the model's note before its code block. Under a turn: `call` lines for sub-calls (with count, time, size, and `(N failed)` or `FAILED: ...` when something went wrong), sub-agents nested at the turn that started them, and then `submitted`, `error: <last line of the traceback>`, or `(no code block in this reply)`. Agent lines end with `-> answer` or `-> failed: <reason>`. A trace that stops before `run.end` prints `result  (trace ends without a result: the run was interrupted or is still going)`.

`--full` adds the code of each turn, each line prefixed with `>`, and the cell's output prefixed with `|` (cut to 3,000 characters). That is the form to read when you want to see what the model did. The [concepts page](concepts.md#an-annotated-transcript) walks through one.

`--list` shows up to 20 recent traces, newest first: modification time, status (`unfinished` when there is no `run.end`), duration, and path.

`--html FILE` writes one self-contained page, with no network access and no dependencies. It has a summary of status, time, calls, tokens, cost and models, then an expandable tree of agents and turns showing each reply, call, output and error, with Expand all and Collapse all buttons. It follows your light or dark setting. The data is embedded, so you can attach the file to a bug report.

## Progress on the terminal: verbosity

| Level | CLI | Python | Shows |
| --- | --- | --- | --- |
| 0 | `-q`, `--quiet` | `verbosity=0` or `"quiet"` | Nothing but the answer (CLI) or nothing (Python). |
| 1 | default | `verbosity=1` or `"normal"` | One line per turn, sub-calls (but not individual tool calls), sub-agents, notes, and a final summary with the trace path. |
| 2 | `-v`, `--verbose` | `verbosity=2` or `"verbose"` | Also the code of each cell, its output, and tool calls. |

Progress goes to stderr. Stdout carries only the answer on the CLI, so a run can be piped at any verbosity. Every line starts with the agent it belongs to, indented by depth, which keeps parallel sub-agents readable. Colour is used on a terminal, and turned off by `NO_COLOR` or `TERM=dumb`.

The final line is `✔ ok`, `◐ partial` or `✗ failed`, with time, call count, tokens and cost. `partial` and `failed` are followed by the note or error text.

## Event callbacks

`on_event` receives the same events as the trace file, live, as `Event` objects. See [Python API](python-api.md#on_event) for the interface and the field list. Use it to stream progress into your own UI, collect costs, or stop watching a run that has gone wrong.

## Debugging the backend

The trace shows what the engine saw. To see what a CLI backend said, set `RLM_DEBUG_DIR` to a directory that already exists:

```bash
mkdir -p ./claude-debug
RLM_DEBUG_DIR=./claude-debug rlm "..." -f data.json -m claude-code
```

The Claude Code backend then writes the raw stream-json events of each CLI process to `claude-<pid>.jsonl` there. The variable is read when rlm-ig starts, only the Claude Code backend uses it, and the directory is not created for you: if it does not exist, the run fails with a `FileNotFoundError`. Files accumulate until you delete them. They contain full prompts and replies, so treat them like the trace.

## What is in a trace, privacy

A trace holds the task, the model's replies, every cell's code and output, and the final answer. Output can include slices of your data. Traces are plain files in your home directory. Delete them, turn tracing off, or point `trace_dir` somewhere appropriate if the data is sensitive. A [session file](sessions.md) is a separate file that holds the entire context.
