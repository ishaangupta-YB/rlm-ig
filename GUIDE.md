# rlm-ig step-by-step guide

This guide goes from nothing installed to real answers over real data. Every `rlm` command and Python snippet in it was run, except the `pipx` and `uv` installs and the suggestions in [section 17](#17-more-data-to-try). The `docker run` commands in [section 10](#10-run-everything-in-docker) were run against local stand-in API servers rather than live services, as that section explains. Every output block marked **real output** was copied from an actual run with Claude Code 2.1.325 and Claude Haiku on Linux in November 2026. Your wording will differ because models are not deterministic. Counts that code can verify should not differ.

- [1. Install](#1-install)
- [2. Connect a model backend](#2-connect-a-model-backend)
- [3. Check your setup](#3-check-your-setup)
- [4. Watch a run with no model](#4-watch-a-run-with-no-model)
- [5. Get the example data](#5-get-the-example-data)
- [6. Use case: rank attackers in an SSH log (CLI)](#6-use-case-rank-attackers-in-an-ssh-log-cli)
- [7. Use case: several files, one structured answer](#7-use-case-several-files-one-structured-answer)
- [8. Use case: a typed report from Python](#8-use-case-a-typed-report-from-python)
- [9. Run model code in a Docker container](#9-run-model-code-in-a-docker-container)
- [10. Run everything in Docker](#10-run-everything-in-docker)
- [11. Use case: follow-up questions with a session](#11-use-case-follow-up-questions-with-a-session)
- [12. Give the model your own functions](#12-give-the-model-your-own-functions)
- [13. Control cost and time](#13-control-cost-and-time)
- [14. See exactly what happened](#14-see-exactly-what-happened)
- [15. Save your settings in rlm.toml](#15-save-your-settings-in-rlmtoml)
- [16. When something goes wrong](#16-when-something-goes-wrong)
- [17. More data to try](#17-more-data-to-try)
- [18. Sources](#18-sources)

## 1. Install

You need Python 3.11 or newer on Linux or macOS. On Windows, use WSL. Check with `python3 --version`.

Pick one way to install.

**A. From PyPI, as a command-line tool.** This is the simplest if you mostly use the `rlm` command. `pipx` and `uv tool` give it its own environment, so it cannot clash with other packages.

```bash
pipx install rlm-ig
```

```bash
uv tool install rlm-ig
```

**B. From PyPI, into a project.** Do this when you will `import rlm_ig` from your own code.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install rlm-ig
```

Add the Anthropic API backend if you want it: `pip install "rlm-ig[anthropic]"`.

**C. From a clone.** Do this to read or change the code, or to run the tests.

```bash
git clone https://github.com/ishaangupta-yb/rlm-ig.git
cd rlm-ig
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The tests take about two minutes and need no network or credentials.

**Check it worked**, whichever way you chose:

```bash
rlm --version
```

**Real output:**

```text
rlm-ig 0.2.0
```

**D. With Docker.** Nothing to install but Docker. The image runs `rlm` itself:

```bash
docker run --rm ghcr.io/ishaangupta-yb/rlm-ig --version
```

Docker is used in two separate ways, and it helps to keep them apart. Running `rlm` in a container saves you installing Python ([section 10](#10-run-everything-in-docker)). Running only the model's code in a container, while `rlm` runs on your machine, is the safety measure ([section 9](#9-run-model-code-in-a-docker-container)).

## 2. Connect a model backend

rlm-ig does not include a model. Set up at least one of these.

| Backend | Setup | Model string | Who pays |
| --- | --- | --- | --- |
| Claude Code | Install the CLI from [code.claude.com](https://code.claude.com/docs/en/setup), run `claude` once and sign in | `claude-code`, `claude-code:sonnet`, `claude-code:haiku` | Your Claude subscription, or your API key if one is set (see the warning below) |
| opencode | Install from [opencode.ai/docs](https://opencode.ai/docs), run `opencode auth login` | `opencode`, `opencode:<provider>/<model>` | Whichever provider you signed in with |
| Anthropic API | `pip install "rlm-ig[anthropic]"` and `export ANTHROPIC_API_KEY=...` | `anthropic:claude-opus-5-5`, `anthropic:claude-haiku-5-5` | Your API account |
| OpenAI-compatible | `export OPENAI_API_KEY=...` (or `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`) | `openai:<model>`, `openrouter:<model>`, … | That provider |
| Ollama | Run Ollama locally and pull a model | `ollama:<model>` | Nobody (your hardware) |

> **Warning for Claude Code users.** If `ANTHROPIC_API_KEY` is set in your shell, the Claude Code CLI uses that key and bills your API account instead of your subscription. To always use the subscription login, put this in `rlm.toml`:
>
> ```toml
> [providers.claude-code]
> strip_api_key = true
> ```

If you set nothing, rlm-ig uses Claude Code if `claude` is on your PATH, then opencode, then the Anthropic API if a key is set. An OpenAI-compatible key is never picked automatically, because a model name is needed. Pass `-m openai:<model>` or set `RLM_MODEL`.

## 3. Check your setup

```bash
rlm doctor --live
```

`--live` sends one tiny request to the default model. **Real output** (the end of it):

```text
configuration
  - user config    /root/.config/rlm-ig/config.toml (not present)
  + default model  claude-code
    limits         max_usd=5.0 max_calls=300 max_steps=20 max_depth=2
    traces         /root/.local/state/rlm-ig/traces

live check
  + claude-code    answered 'Ready' in 4.6s (810 in / 118 out)
```

A `-` means "not set up", which is fine for backends you do not use. An `x` is an error to fix.

## 4. Watch a run with no model

```bash
rlm demo
```

This drives the real engine with a scripted stand-in for the model over a generated 2.4 MB log. It costs nothing and shows every part of a run: the model looking at the data, counting with code, fanning work out with `ask_map`, a sub-agent checking the result, and a typed answer. Add `-v` to see the code of every step.

## 5. Get the example data

The examples use [Loghub](https://github.com/logpai/loghub), a public collection of real system logs from production systems, published for research ([Zhu et al., ISSRE 2023](#18-sources)). Each log has a 2,000-line sample in the repository, small enough to download in a second and check by hand.

```bash
mkdir rlm-examples && cd rlm-examples
curl -LO https://raw.githubusercontent.com/logpai/loghub/master/OpenSSH/OpenSSH_2k.log
curl -LO https://raw.githubusercontent.com/logpai/loghub/master/OpenSSH/OpenSSH_2k.log_structured.csv
curl -LO https://raw.githubusercontent.com/logpai/loghub/master/HDFS/HDFS_2k.log
mkdir logs
curl -L -o logs/Apache_2k.log https://raw.githubusercontent.com/logpai/loghub/master/Apache/Apache_2k.log
curl -L -o logs/Linux_2k.log https://raw.githubusercontent.com/logpai/loghub/master/Linux/Linux_2k.log
```

| File | What it is | Size |
| --- | --- | --- |
| `OpenSSH_2k.log` | An SSH server's auth log during brute-force attacks | 225 KB |
| `OpenSSH_2k.log_structured.csv` | The same lines parsed into columns (date, time, event id, template) | |
| `HDFS_2k.log` | Hadoop file system block operations | 288 KB |
| `logs/Apache_2k.log` | Apache web server error log | 171 KB |
| `logs/Linux_2k.log` | Linux syslog | 216 KB |

These samples fit in a large context window. They are used here because the answers can be checked with `grep`. rlm-ig does the same thing on files hundreds of times larger, because the model never reads the whole file; see [section 17](#17-more-data-to-try).

## 6. Use case: rank attackers in an SSH log (CLI)

**Goal:** find which IP addresses are trying hardest to break into the server.

```bash
rlm "Which 5 IP addresses have the most failed password attempts? Give each IP with its count." \
    -f OpenSSH_2k.log -m claude-code:haiku --max-usd 0.50
```

**Real output:**

```text
183.62.140.253: 286
187.141.143.180: 80
103.99.0.122: 46
112.95.230.3: 26
5.188.10.180: 18
✔ ok  5.9s · 3 calls · 11k in (6.8k cached) / 665 out · $0.0013
```

**Check it yourself:**

```bash
grep "Failed password" OpenSSH_2k.log | grep -o 'from [0-9.]*' | sort | uniq -c | sort -rn | head -5
```

The counts match exactly. The model wrote a regex and counted in Python. It did not read 2,000 lines and guess.

What to notice:

- The answer goes to **stdout** and the progress line to **stderr**, so `rlm ... > answer.txt` saves only the answer.
- `--max-usd 0.50` caps the spend. The reported cost under a Claude subscription is the API-equivalent price, not a charge.
- The exit code is 0 for `ok`, 2 for `partial` (a limit was hit and the model wrapped up), and 1 for `failed`.
- Piping works too: `cat OpenSSH_2k.log | rlm "..."`.

## 7. Use case: several files, one structured answer

**Goal:** a machine-readable health summary of several logs at once.

`-f` with a directory loads every file in it, as a dict of file name to text. `--schema` makes the answer follow a JSON Schema; if the model's answer does not fit, it is sent back with the errors.

Save this as `schema.json`:

```json
{
  "type": "object",
  "properties": {
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "file": {"type": "string"},
          "error_lines": {"type": "integer"},
          "top_problem": {"type": "string"}
        },
        "required": ["file", "error_lines", "top_problem"]
      }
    }
  },
  "required": ["files"]
}
```

```bash
rlm "For each file, count the error lines and name the most common problem in plain words." \
    -f logs/ --schema schema.json -m claude-code:haiku --max-usd 0.50
```

**Real output:**

```json
{
  "files": [
    {
      "file": "Apache_2k.log",
      "error_lines": 595,
      "top_problem": "The mod_jk connector's worker child processes are stuck in an error state (mostly state 6), so requests to the backend (Tomcat) fail."
    },
    {
      "file": "Linux_2k.log",
      "error_lines": 539,
      "top_problem": "Failed login attempts: SSH/PAM authentication failures, mostly for unknown users, likely from repeated attempts by outside hosts."
    }
  ]
}
```

`grep -c "\[error\]" logs/Apache_2k.log` gives 595, an exact match. The Linux log has no `error` keyword, so the model chose its own definition (lines mentioning failures). `grep -ciE "fail|error"` gives 538, close but not equal. **Lesson: when you need an exact number, say exactly what to count.** "Lines containing `authentication failure`" gets an exact answer. "Error lines" leaves the model to decide what counts.

Add `--json` to get the full result (answer, status, usage, trace path) as one JSON object for scripts.

## 8. Use case: a typed report from Python

**Goal:** turn the SSH log into a report your own code can use.

Save as `report.py` next to `OpenSSH_2k.log`:

```python
from dataclasses import dataclass

import rlm_ig as rlm


@dataclass
class Incident:
    kind: str  # e.g. "brute force", "break-in attempt"
    source_ip: str
    attempts: int
    usernames_tried: list[str]


@dataclass
class Report:
    incidents: list[Incident]
    summary: str


log = rlm.load("OpenSSH_2k.log")
result = rlm.run(
    "Find the 3 most serious attack sources in this SSH log. For each, give the kind of attack, "
    "the source IP, how many attempts, and up to 5 usernames it tried.",
    context=log,
    schema=Report,
    model="claude-code:haiku",
    max_usd=0.50,
)
print(result.status)
for i in result.parsed.incidents:  # Incident instances
    print(f"{i.source_ip:16} {i.attempts:4}  {i.kind}  {i.usernames_tried[:5]}")
print(result.parsed.summary)
print(result.usage)
```

```bash
python report.py
```

**Real output:**

```text
ok
183.62.140.253    286  SSH password brute-force (dictionary attack, mostly against root)  ['root', 'oracle', 'zhangyan', 'dff', 'test']
187.141.143.180    80  SSH password brute-force (credential guessing against root and service accounts)  ['root', 'oracle', 'git', 'deploy', 'mysql']
103.99.0.122       46  SSH password brute-force with invalid-user probing (default and generic account names)  ['admin', 'root', 'user', 'support', '1234']
The three most serious sources, ranked by count of 'Failed password' events, are 183.62.140.253 (286 attempts, mostly root), ...
4 calls · 14k in (5.9k cached) / 2.5k out · $0.0028
```

Check: `grep -c "Failed password.*183.62.140.253" OpenSSH_2k.log` prints 286.

Things to know:

- `result.answer` is always the validated plain value. `result.parsed` is an instance of your class, built at every level: `result.parsed.incidents` holds `Incident` objects. If building it fails, `parsed` is `None` and `answer` is still there.
- `run` does not raise when a run fails. Check `result.status`, or call `result.raise_for_status()`.
- `rlm.load(path)` reads a file or directory the same way `-f` does. You can pass any string, list, dict or picklable object as `context`.
- For a cheaper fan-out, keep a strong root model and a small sub-model: `model="claude-code:sonnet", sub_model="claude-code:haiku"`.

## 9. Run model code in a Docker container

By default the model's code runs as an ordinary subprocess with your user's permissions. It can read your files and use the network. **For data you do not trust, or runs nobody watches, run that code in a container.** You need Docker installed and running. `rlm` itself keeps running on your machine.

Add `--sandbox docker`:

```bash
rlm "How many distinct block IDs (blk_...) appear in this HDFS log, and which block appears most often?" -f HDFS_2k.log --sandbox docker
```

**Real output:**

```text
Distinct block IDs: 2200 (2469 total occurrences). Most frequent block: blk_-8775602795571523802, appearing 4 times.
✔ ok  4.3s · 2 calls · 5.8k in (2.7k cached) / 347 out · $0.0008
```

Check: `grep -o "blk_-\?[0-9]*" HDFS_2k.log | sort -u | wc -l` prints 2200.

To see the container from the model's side, ask it to look:

```bash
rlm "Use code to report: the user id your Python runs as, the current directory, whether pandas imports, and whether a TCP connection to 1.1.1.1 port 53 succeeds." --sandbox docker -q
```

**Real output:**

```text
{
  "uid": 65534,
  "cwd": "/tmp",
  "pandas_import": "ok (version 3.1.0)",
  "tcp_1.1.1.1:53": "failed: OSError: [Errno 101] Network is unreachable"
}
```

`--sandbox docker` starts each REPL worker with:

```text
docker run --rm -i --network none --read-only --tmpfs /tmp -w /tmp
  --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256
  --user 65534:65534 ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0
```

`rlm doctor --sandbox docker` checks that this works and prints the command. To make it the default, put `preset = "docker"` under `[sandbox]` in `rlm.toml`, or set `RLM_SANDBOX=docker`.

How it works and what to know:

- rlm-ig sends the REPL worker's source code into the container over stdin, so **nothing needs to be mounted or installed** in the image except Python. Your data also arrives over the pipe.
- The default image is `ghcr.io/ishaangupta-yb/rlm-ig-sandbox` at the version of rlm-ig you installed. It holds Python 3.12 with numpy, pandas, python-dateutil and regex. For other packages, build your own image and name it: `--sandbox docker:my-image`. `--sandbox docker:python:3.12-slim` uses plain Python.
- `--network none` means the model's code cannot reach the internet or your network. `ask`, `delegate` and your tools still work, because they run on the host and only their results cross into the container.
- The container's filesystem is read-only except `/tmp`, which is where the code starts.
- A cell that runs past `cell_timeout` cannot be interrupted inside a container, so the container is killed and replaced. The model is told that its variables are gone.
- Only the worker is isolated. The model CLIs, your tools and MCP servers still run on your machine.
- `--sandbox podman` uses the same flags with Podman. It has not been tested.

The real output above came from the sandbox image built from `docker/sandbox.Dockerfile` in this repository, at this version, before it was published. The same works from Python with `sandbox={"preset": "docker"}`, and any other command prefix with `sandbox={"launcher": [...]}`. Details: [docs/security.md](docs/security.md#real-isolation-with-a-launcher).

## 10. Run everything in Docker

The image `ghcr.io/ishaangupta-yb/rlm-ig` contains Python, rlm-ig and the Anthropic SDK. Its entry point is `rlm`, so anything after the image name is an `rlm` command line. It runs as an unprivileged user (uid 10001) in `/work`, where you mount your data.

```bash
docker run --rm ghcr.io/ishaangupta-yb/rlm-ig demo
```

That runs the offline demo from [section 4](#4-watch-a-run-with-no-model) and needs nothing else.

**With an API key**, mount the folder with your data read-only and pass the key through by name. A named volume keeps the traces between runs:

```bash
docker run --rm -e ANTHROPIC_API_KEY -v "$PWD:/work:ro" -v rlm-state:/home/rlm/.local/state \
  ghcr.io/ishaangupta-yb/rlm-ig "Which IPs failed to log in most?" -f OpenSSH_2k.log -m anthropic:claude-haiku-5-5
```

```bash
docker run --rm -v rlm-state:/home/rlm/.local/state ghcr.io/ishaangupta-yb/rlm-ig trace --list
```

**Piping data in** needs `-i`, and not `-t`. Without `-i` the container gets no stdin and the run sees no data:

```bash
cat server.log | docker run --rm -i -e OPENAI_API_KEY ghcr.io/ishaangupta-yb/rlm-ig "Summarise the outage" -m openai:<model>
```

**Ollama on your machine.** Inside a container, `localhost` is the container. Point rlm-ig at the host with `OLLAMA_HOST`:

```bash
docker run --rm --add-host=host.docker.internal:host-gateway -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v "$PWD:/work:ro" ghcr.io/ishaangupta-yb/rlm-ig "..." -f data.log -m ollama:<model>
```

**Writing files into the mounted folder**, such as `-o answer.txt`, needs the container to run as your user. Set `HOME` to somewhere writable:

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e ANTHROPIC_API_KEY -v "$PWD:/work" \
  ghcr.io/ishaangupta-yb/rlm-ig "..." -f data.log -o answer.txt
```

**Claude Code** is in the `:claude` variant. Sign in with a token instead of mounting `~/.claude`: run `claude setup-token` on your machine, which prints a long-lived token for scripts and CI that uses your Claude subscription, and pass it as `CLAUDE_CODE_OAUTH_TOKEN`:

```bash
docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN -v "$PWD:/work:ro" ghcr.io/ishaangupta-yb/rlm-ig:claude "..." -f data.log -m claude-code:haiku
```

`ANTHROPIC_API_KEY` works there as well, and takes precedence over the token if both are set.

What was checked, and how: every command above was run with images built from this repository's `Dockerfile`. A real model key was not available where they were run, so `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` and `OLLAMA_HOST` pointed the runs at local stand-in servers that speak each API. The image, the mounts, the named volume, stdin with `-i`, a run as another uid writing `-o answer.txt`, and Claude Code 2.1.315 inside the `:claude` image all worked against them. `CLAUDE_CODE_OAUTH_TOKEN` itself was not tried, because that needs a real token.

Things to know:

- **The model's code is not network-isolated here.** In this image the REPL worker is an ordinary process inside the same container as `rlm`, and `rlm` needs the network to reach the model. The container keeps the model's code away from your files (it sees only what you mount), but not away from the network. When that matters, run `rlm` on your machine with `--sandbox docker` as in [section 9](#9-run-model-code-in-a-docker-container).
- **Do not mount `/var/run/docker.sock`** into the container to combine the two. That gives the container root on your machine. It is not supported.
- Variables you pass with `-e` are visible to `rlm` and the model CLIs, not to the model's code, which gets the [scrubbed environment](docs/security.md#the-environment-allowlist).
- To add opencode or other tools, build your own image `FROM ghcr.io/ishaangupta-yb/rlm-ig` and install them.

## 11. Use case: follow-up questions with a session

**Goal:** parse once, then ask several questions without re-parsing.

A session keeps the REPL alive between runs. From the CLI, `--session NAME` saves it to `.rlm/sessions/NAME.json` in the current directory.

```bash
rlm "Load the rows into a variable called rows. How many distinct EventIds are there?" \
    -f OpenSSH_2k.log_structured.csv --session ssh -m claude-code:haiku -q
```

**Real output:**

```text
rows is loaded with 2000 records. There are 27 distinct EventIds (E1 through E27), and no record is missing an EventId.
```

```bash
rlm "Using rows, which hour of the day (Time column) has the most events?" --session ssh -m claude-code:haiku -q
```

**Real output:**

```text
The 09 hour (09:00–09:59) has the most events: 676 of 2,000 rows. Next are 10 (554) and 11 (476).
```

The second question needed no `-f`: the parsed `rows` were still there. A `.csv` file is parsed into a list of dicts on load. `-q` prints only the answer.

In Python:

```python
import rlm_ig as rlm

with rlm.Session(path="analysis.json") as session:
    session.run("Parse this into `rows`.", context=rlm.load("OpenSSH_2k.log_structured.csv"))
    print(session.run("Using rows, which EventId is most common?").answer)
```

**Real output:** `E24`, which is correct (413 of 2,000 rows).

A session file contains pickles that the worker restores, so **treat it like code**: never load a session file someone else gave you.

## 12. Give the model your own functions

Tools are plain Python functions. They run in your process with your credentials, and the model sees only the name, signature and docstring. This is how you let the model look things up in a database or API without giving its code the keys.

```python
import rlm_ig as rlm

PRICES = {"SKU-104": 19.99, "SKU-221": 4.50}

def price_of(sku: str) -> float:
    """Current unit price of a SKU in USD."""
    return PRICES[sku]

result = rlm.run("Total this order: 3 x SKU-104, 10 x SKU-221", tools=[price_of])
print(result.answer)
```

**Real output:** `104.97`, which is 3 × 19.99 + 10 × 4.50. The model called `price_of` for each SKU instead of guessing prices.

A complete version is in [examples/04_tools.py](examples/04_tools.py). MCP servers work too: each tool appears in the REPL as `<server>_<tool>(...)`; see [docs/tools-and-mcp.md](docs/tools-and-mcp.md).

**Tools run with your permissions on arguments the model chose.** Keep them few, narrow and read-only.

## 13. Control cost and time

Every run has hard limits. When one is hit, the root agent gets one last turn to submit what it has, and the result is marked `partial` instead of being lost.

| CLI flag | Python / rlm.toml | Default | Meaning |
| --- | --- | --- | --- |
| `--max-usd N` | `max_usd` | 5.00 | Spend ceiling |
| `--max-calls N` | `max_calls` | 300 | Model calls, all agents together |
| `--max-steps N` | `max_steps` | 20 | Turns per agent |
| `--max-depth N` | `max_depth` | 2 | Levels of sub-agents |
| `--timeout S` | `timeout` | none | Wall-clock seconds |
| `--parallel N` | `max_parallel` | 4 for CLIs, 8 for APIs | Simultaneous requests |

Rules of thumb:

- Start with `--max-usd 0.50` while you learn what a task costs. The runs in this guide each cost under half a cent with Haiku.
- A cheap `sub_model` is the biggest saving on tasks that read many items.
- Each Claude Code process uses about 200 MB of memory. On a small machine use `--parallel 2`.

## 14. See exactly what happened

Every run writes a JSONL trace (unless you pass `--no-trace`).

```bash
rlm trace                      # the last run, step by step
rlm trace --full               # with the code and output of every step
rlm trace --list               # recent runs
rlm trace --html report.html   # a self-contained page to share
```

Use `-v` during a run to watch the code as it runs. **Read the trace before you act on an answer that matters.** It shows exactly which parts of the data the model looked at. Traces contain slices of your data, so treat them as sensitive.

## 15. Save your settings in rlm.toml

rlm-ig looks for `rlm.toml` in the current directory and its parents. Every key is optional.

```toml
model = "claude-code:sonnet"
sub_model = "claude-code:haiku"
max_usd = 2.00

[sandbox]
cell_timeout = 120
# memory_mb = 4096

[providers.claude-code]
strip_api_key = true
```

Later sources win: built-in defaults, then `~/.config/rlm-ig/config.toml`, then `rlm.toml`, then `RLM_*` environment variables (`RLM_MODEL`, `RLM_SUB_MODEL`, `RLM_MAX_USD`, …), then CLI flags. A fully commented example is [examples/rlm.toml](examples/rlm.toml); every option is in [docs/configuration.md](docs/configuration.md).

## 16. When something goes wrong

| Symptom | Fix |
| --- | --- |
| `No model is configured and none could be detected` | Set up a backend ([section 2](#2-connect-a-model-backend)) or pass `-m provider:model` |
| `the anthropic backend needs the Anthropic SDK` | `pip install "rlm-ig[anthropic]"`, or unset `ANTHROPIC_API_KEY` if you meant to use Claude Code |
| Claude Code bills your API account | Unset `ANTHROPIC_API_KEY` or set `strip_api_key = true` |
| Result is `partial` | A limit was hit. Raise `--max-steps`, `--max-calls` or `--max-usd`, or make the task narrower |
| A count is slightly off | Say exactly what to count, and ask the model to verify with code |
| Your task is the word `doctor`, `trace`, `demo` or `version` | Write `rlm run "doctor"`, because a bare first word is read as a command |
| Docker launcher: the run hangs or fails at start | Make sure `-i` is in the launcher and the image has `python3`; check `docker run --rm -i python:3.12-slim python3 -V` |
| `ModuleNotFoundError` inside a Docker sandbox | Build an image with the package installed and use it in the launcher |

More in [docs/troubleshooting.md](docs/troubleshooting.md).

## 17. More data to try

These were not run for this guide. They are good next steps because they are far larger than a context window.

- **Full Loghub logs.** The complete datasets (for example `HDFS_v1` with about 11 million lines, or `OpenSSH` with about 650,000) are on Zenodo: [zenodo.org/records/8196385](https://zenodo.org/records/8196385). Download one, then try `rlm "Which hours had the most anomalies, and what were they?" -f OpenSSH.log --max-usd 1`.
- **A whole novel.** Project Gutenberg's *War and Peace* is about 3.3 MB of plain text: [gutenberg.org/ebooks/2600](https://www.gutenberg.org/ebooks/2600). Try "List every character who dies, with the book and chapter", which needs reading across the whole book and suits `ask_map` fan-out.
- **Your own code.** `rlm "List every HTTP endpoint and the auth it requires" -f ./src` loads a source tree as a dict of path to text.

## 18. Sources

- Alex L. Zhang, Tim Kraska, Omar Khattab. *Recursive Language Models*. arXiv:2512.24601, 2025. [arxiv.org/abs/2512.24601](https://arxiv.org/abs/2512.24601). The method rlm-ig implements. rlm-ig is independent and not affiliated with the authors.
- Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. *Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics*. IEEE ISSRE 2023, pp. 355–366. [github.com/logpai/loghub](https://github.com/logpai/loghub), data on [Zenodo](https://zenodo.org/records/8196385). Source of every log used in this guide. Loghub's datasets are free for research and academic use.
- Leo Tolstoy, *War and Peace*, Project Gutenberg eBook #2600. [gutenberg.org/ebooks/2600](https://www.gutenberg.org/ebooks/2600).
