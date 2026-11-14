# Quickstart

This page takes you from install to a first real run. It assumes Python 3.11 or newer and a Linux or macOS shell. On Windows, use WSL.

## Install

```bash
pip install rlm-ig
```

To use the Anthropic API as a backend, add the extra:

```bash
pip install "rlm-ig[anthropic]"
```

From a source checkout:

```bash
pip install -e ".[dev]"
```

This installs the `rlm` command and the `rlm_ig` package. The core package has no dependencies. The `anthropic` extra adds the Anthropic SDK. The `dev` extra adds pytest, ruff, pydantic and the Anthropic SDK.

## You need one model backend

rlm-ig does not bundle a model. It needs one of these:

- The Claude Code CLI (`claude`), signed in.
- The opencode CLI (`opencode`), signed in.
- An API key: `ANTHROPIC_API_KEY` (with the `anthropic` extra), or `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`. A local Ollama server also works.

See [Backends](backends.md) for the details and for which account pays for what.

## Check your setup

```bash
rlm doctor
```

It starts a REPL worker, looks for each backend, shows where your config and traces live, and says which model it would use by default. Abridged output from a machine with Claude Code installed:

```text
rlm-ig 0.2.0

runtime
  + python         3.13.18 (/path/to/.venv/bin/python)
  + sandbox        REPL worker starts and runs code in 75 ms

backends
  + claude-code    2.1.323 (Claude Code)
  - opencode       `opencode` not found on PATH
  - anthropic      SDK 2.8.0, no ANTHROPIC_API_KEY set
  - openai         $OPENAI_API_KEY not set
  ...

configuration
  - user config    ~/.config/rlm-ig/config.toml (not present)
  + default model  claude-code
    limits         max_usd=5.0 max_calls=300 max_steps=20 max_depth=2
    traces         ~/.local/state/rlm-ig/traces

  run `rlm doctor --live` to send one tiny request to the default model
```

`rlm doctor --live` also sends one small request to the default model and prints the model that answered. Use `-m` to check a specific one, for example `rlm doctor --live -m claude-code:haiku`.

If doctor says no model could be detected, see [Troubleshooting](troubleshooting.md).

## Watch a run offline

```bash
rlm demo
```

The demo runs the real engine, REPL, parallel fan-out and a sub-agent, but a script plays the model, so it needs no credentials and costs nothing. The data is a synthetic 40,000-line service log. Add `-v` to see the code and output of every step. [Concepts](concepts.md) walks through that transcript.

## First run from the CLI

Make a small data file. This one is 5,000 order rows:

```bash
python - <<'EOF'
import csv, random
rng = random.Random(3)
with open("orders.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["order_id", "region", "amount", "status"])
    for i in range(1, 5001):
        w.writerow([
            i,
            rng.choice(["north", "south", "east", "west"]),
            round(rng.uniform(5, 400), 2),
            rng.choice(["paid", "paid", "paid", "refunded", "failed"]),
        ])
EOF
```

Ask a question about it:

```bash
rlm "Which region has the highest total of paid orders? Answer with the region and the total." -f orders.csv
```

Add `-m claude-code:haiku` (or another [model string](backends.md)) if you do not want the default. Real output from a run with `-m claude-code:haiku`:

```text
rlm  claude-code:haiku  · context: list, 5,000 items
root   ▸ turn 1  3.9s  I'll check the distinct status and region values for messy data, then total the paid amounts pe…
root     └ ran 0.01s, 319 chars of output
root   ▸ turn 2  0.7s  The data is clean: statuses and regions are consistent, every amount parsed, and all 5,000 reco…
root     ✔ submitted
✔ ok  5.2s · 2 calls · 5.7k in (4.4k cached) / 645 out · $0.0006
  trace: ~/.local/state/rlm-ig/traces/20261113-073935-which-region-has-the-highest-total-of-pa-3658.jsonl
south: 157215.69
```

The model's wording, the number of turns and the cost will differ from run to run. What does not change:

- Progress goes to stderr. The answer is the only thing on stdout, so `rlm ... > answer.txt` and pipes work.
- `-q` prints only the answer. `-v` also shows the code and output of every step.
- The exit code is 0 for `ok`, 2 for `partial`, 1 for `failed` or an error, and 130 after Ctrl-C.

`-f` takes a file or a directory and can be repeated. `.json`, `.jsonl`, `.csv` and `.tsv` files become structured values, other files become text, and a directory becomes a dict of relative path to text. Without `-f`, `rlm` reads stdin if it is piped:

```bash
cat server.log | rlm "Summarise the outage"
```

If stdin is not a terminal and you give no `-f`, `rlm` reads stdin until it closes. In a script or CI job where stdin is open but nothing is piped in, that looks like a hang. Redirect `< /dev/null` when the task needs no data.

To get a machine-readable result, use `--json`. It prints the answer, status, usage, step count, trace path and models.

## First run from Python

```python
import rlm_ig as rlm

result = rlm.run(
    "Which region has the highest total of paid orders? Answer with the region and the total.",
    context=rlm.load("orders.csv"),
    model="claude-code:haiku",
    verbosity=0,
)

print(result.status)   # "ok", "partial" or "failed"
print(result.answer)
print(result.usage)
print(result.trace)
```

`verbosity=0` silences the progress lines. The default is 1, so a Python call prints progress to stderr just like the CLI unless you turn it off.

Output of that script:

```text
ok
south: 157215.69 (766 paid orders)
2 calls · 5.8k in (3.9k cached) / 731 out · $0.0008
~/.local/state/rlm-ig/traces/20261113-073853-which-region-has-the-highest-total-of-pa-c647.jsonl
```

Inside an async program use `await rlm.arun(...)`. `rlm.run` raises if it is called from a running event loop.

## Reading the result

`rlm.run` returns a `Result`:

| Field | Meaning |
| --- | --- |
| `answer` | What the model passed to `submit`. A string, number, list or dict. `None` when the run failed. |
| `status` | `"ok"`, `"partial"` or `"failed"`. |
| `note` | For `partial`: why the answer came from a forced wrap-up. |
| `error` | For `failed`: what went wrong. |
| `usage` | Calls, input and output tokens, cached tokens, and cost in USD when the backend reports or can estimate one. |
| `steps`, `agents` | Model turns and agents used. |
| `trace` | Path of the trace file, or `None`. |

`partial` means the model ran out of turns or the budget ran out, and the answer was built in one last turn from what it already had. Treat it as possibly incomplete. Use `result.raise_for_status()` to turn `partial` and `failed` into an exception. See [Python API](python-api.md).

On Claude Code under a subscription, the cost shown is an API-equivalent figure reported by the CLI, not a charge. See [Backends](backends.md).

## Where traces go

Every run writes a trace, one JSON event per line, to `~/.local/state/rlm-ig/traces/` (or `$XDG_STATE_HOME/rlm-ig/traces/`). Change the directory with `trace_dir` or `RLM_TRACE_DIR`, and turn tracing off with `--no-trace` or `trace=False`.

```bash
rlm trace            # the most recent run, step by step
rlm trace --full     # include the code and output of every step
rlm trace --list     # recent runs
rlm trace --html report.html
```

See [Tracing](tracing.md).

## Next

- [Concepts](concepts.md) explains what happens inside a run.
- [Configuration](configuration.md) covers limits, `rlm.toml` and environment variables.
- [Tools and MCP](tools-and-mcp.md) shows how to give the model your own functions.
