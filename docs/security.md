# Security

This page says what rlm-ig does and does not protect, so you can decide what to add.

## The short version

- In a run, a model writes Python and rlm-ig executes it. That code runs in a **subprocess of your user. It is not a security boundary.** It can read your files and use the network.
- A few things reduce accidents: a scrubbed environment, a throwaway working directory, cell timeouts, an optional memory limit. They do not stop code that is trying to do harm.
- The host never unpickles anything a worker produced. Workers only ever send it JSON.
- For real isolation, run the worker in a container: `--sandbox docker` on the command line, or `preset = "docker"` under `[sandbox]`. Anything else that can wrap a process works through `sandbox.launcher`. This is the recommended setup for untrusted data or an unattended run. Tools and MCP servers still run on the host.
- Running `rlm` itself in the Docker image is a convenience, not this isolation: there the model's code shares the network that `rlm` uses. See [Running rlm in a container](#running-rlm-in-a-container).
- Text in your data can try to steer the model. Treat the data as untrusted input and limit what the model can reach.

## The REPL worker is an ordinary subprocess

Each agent's code runs in a Python process started by rlm-ig, with your user id, in its own process group. Model-written code in it can:

- read any file your user can read, including `~/.ssh`, `~/.aws`, browser profiles, and the project you are in;
- write and delete files anywhere your user can;
- open network connections, including to hosts that are not the model's;
- start other programs;
- use any library installed in the Python environment;
- interfere with the worker's own bookkeeping.

The throwaway working directory is only a place to start. Code can `cd` out of it or use absolute paths. Nothing prevents it.

Do not run rlm-ig on data and tasks you would not be comfortable letting a script written by a stranger run against your account, unless you isolate the worker as described below.

## What does exist

| Measure | What it does | What it does not do |
| --- | --- | --- |
| Scrubbed environment | The worker gets a short allowlist of variables (below), not your environment. API keys and tokens in your environment are not passed. | It does not hide credentials stored in files under `HOME`, in a keychain, or in a proxy URL. |
| Throwaway working directory | Each agent starts in its own empty temporary directory, deleted when the run ends. Stray files do not land in your project. | It is not a jail. |
| Cell timeout | A cell that computes for more than `cell_timeout` (default 120 s) is interrupted, and killed if it ignores that. | It does not limit what the cell does before then. |
| `sandbox.memory_mb` | Sets `RLIMIT_AS` on the worker. An allocation over the limit usually raises `MemoryError` in the cell. | Needs a POSIX system with the `resource` module. If the limit cannot be set, it is skipped silently. It limits address space, not resident memory. |
| Process cleanup | A worker starts in its own process group and is killed when its agent ends. A worker also exits on its own when its pipe to the host closes, for instance if the host process dies. | It cannot clean up what a cell started in a different session or on another machine. |
| The trust rule | The host only parses JSON from a worker and never unpickles anything a worker produced. | It does not protect the worker from what the host sends it. |

### The environment allowlist

Unless you change it, the worker's environment holds only these variables, and only those that are set in your environment:

```text
PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TZ TERM
TMPDIR TEMP TMP SYSTEMROOT COMSPEC PATHEXT USERPROFILE APPDATA LOCALAPPDATA
VIRTUAL_ENV CONDA_PREFIX PYTHONPATH PYTHONHOME
HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY http_proxy https_proxy no_proxy all_proxy
SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE NODE_EXTRA_CA_CERTS
```

rlm-ig adds `PYTHONIOENCODING=utf-8` and `PYTHONUNBUFFERED=1`. Notes:

- `HOME` is passed, so programs the worker runs can find your dotfiles.
- Proxy variables are passed so the worker can reach the network where you need a proxy. A proxy URL can contain a username and password, and they are visible to the worker.
- `PYTHONPATH` is passed so modules you rely on are importable in the worker.

Three settings change the allowlist, in `[sandbox]`:

| Setting | Effect |
| --- | --- |
| `pass_env = ["MY_TOKEN"]` | Also pass these variables from your environment. |
| `env = { KEY = "value" }` | Set these in the worker, whatever your environment holds. |
| `inherit_env = true` | Pass everything, credentials included. Do this only for a worker you have isolated. |

```toml
[sandbox]
pass_env = ["MY_DATA_TOKEN"]
env = { TZ = "UTC" }
```

Variables in `env` and `pass_env` are visible to model-written code. Pass only what the task needs.

### The trust rule

Two directions, two rules:

- **Host to worker.** The host may send the worker pickles: a `context` that is not plain JSON, and a tool result that is not plain JSON. The worker unpickles them. The host is the trusted side. Plain values (strings, numbers, lists and dicts of those) go as JSON.
- **Worker to host.** Everything a worker sends is JSON. Values are reduced to JSON before they leave the worker, and anything that cannot be reduced degrades to its `repr`. The host parses that JSON and never calls `pickle.loads` or `eval` on it.

The consequence: a compromised or misbehaving worker cannot get code executed in the host process by returning a crafted object. It can still ask the host to do things through the functions the REPL exposes, which is the next point.

A saved [session file](sessions.md) holds pickles made by the worker. The host stores them as opaque text and gives them back to a worker on restore. It never reads them. Restoring a session file unpickles it inside a worker, so treat a session file as code and do not restore one from an untrusted source.

### What the model can make the host do

Model-written code can call `ask`, `ask_map`, `delegate`, `delegate_map` and your tools. These run on the host.

- `ask` and `delegate` spend your model budget. `max_calls`, `max_usd`, `max_tokens`, `max_depth` and `timeout` bound that. See [Concepts](concepts.md#budgets).
- **Tools run in your process, with your privileges, with arguments the model chose.** A tool that runs a shell command, a SQL query, or an HTTP request on the model's say-so is as dangerous as the model's code is. Validate arguments, make tools read-only where you can, and do not expose a tool you would not want a hostile caller to use. See [Tools and MCP](tools-and-mcp.md).
- **MCP servers** are host processes with your full environment, or remote services that receive what the model sends.

Putting the worker in a container does not change this. The tool call crosses the container boundary by design.

## Real isolation with a launcher

### The Docker preset

The short way to run the worker in a locked-down container:

```bash
rlm "..." -f data.log --sandbox docker
```

or in `rlm.toml`:

```toml
[sandbox]
preset = "docker"
```

or `RLM_SANDBOX=docker` in the environment, or `sandbox={"preset": "docker"}` in Python. It expands to exactly this launcher:

```text
docker run --rm -i --network none --read-only --tmpfs /tmp -w /tmp
  --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256
  --user 65534:65534 ghcr.io/ishaangupta-yb/rlm-ig-sandbox:<version>
```

If the container cannot start, the error shows Docker's own message plus a hint for a missing image, a stopped Docker daemon, or an image without `python3`, and `rlm doctor --sandbox docker` is the quickest way to check (see [Troubleshooting](troubleshooting.md#sandbox-failed-to-start)).

| Flag | Effect |
| --- | --- |
| `--network none` | No network at all inside the container. |
| `--read-only --tmpfs /tmp -w /tmp` | The image's filesystem cannot be changed. `/tmp` is a writable in-memory directory and the working directory, so code that writes a scratch file still works. |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | No Linux capabilities, and no way to gain privileges through setuid programs. |
| `--memory 1g --pids-limit 256` | Bounded memory and number of processes. |
| `--user 65534:65534` | Runs as `nobody`, not root. |

`<version>` is the installed rlm-ig version, so the sandbox image always comes from the same release as the package. Name another image with `--sandbox docker:IMAGE`, for example `docker:python:3.12-slim` for plain Python or an image of your own with the libraries you need. An explicit `launcher` in the same config source wins over a preset. `--sandbox podman[:IMAGE]` uses the same flags with Podman; **it has not been tested.**

**The sandbox image**, `ghcr.io/ishaangupta-yb/rlm-ig-sandbox`, is built from [`docker/sandbox.Dockerfile`](../docker/sandbox.Dockerfile): `python:3.12-slim` pinned by digest, plus these libraries at pinned versions, so the model's code can import them:

| Library | Version |
| --- | --- |
| numpy | 2.5.4 |
| pandas | 3.1.0 |
| python-dateutil | 2.9.0.post0 |
| regex | 2026.9.29 |

It contains no rlm-ig: the worker's source arrives over stdin. It adds no shell tools or compilers to what the base image has, and runs as uid 65534. It is published from version 0.2.0 on; see [RELEASING.md](../RELEASING.md).

**What is tested.** `tests/test_docker.py` runs the preset against a real Docker daemon, in CI on every push and pull request: a run completes and `ask_map` works through the pipe; a connection to `1.1.1.1:53` fails inside the worker; `/tmp` and the working directory are writable and `/` is not; the worker runs as uid 65534 in `/tmp`; a cell past `cell_timeout` gets its container killed and replaced, with no container left behind; closing a sandbox removes its container; and the libraries above import in the sandbox image. These tests use `python:3.12-slim` and an image built from `docker/sandbox.Dockerfile`. A real run through `--sandbox docker` with Claude Code and Claude Haiku is in the [guide](../GUIDE.md#9-run-model-code-in-a-docker-container).

### Any launcher

`sandbox.launcher` is a command prefix. rlm-ig starts the worker as the launcher followed by `python3 -u -c <bootstrap> <size>`. The worker's own source is sent over stdin as the first bytes, then the normal protocol follows on the same pipes. Nothing has to exist on the other side: no mounted directory, no installed rlm-ig. Anything that can start a program and connect its stdin and stdout can wrap it.

The preset above is one such prefix. Written out as a launcher, a minimal Docker example is:

```toml
[sandbox]
launcher = ["docker", "run", "--rm", "-i", "--network", "none", "python:3.12-slim"]
```

A launcher written as a string is split into words the way a shell would: `launcher = "docker run --label 'a b' img"` gives `["docker", "run", "--label", "a b", "img"]`. `RLM_SANDBOX_LAUNCHER` takes the same string form.

Or in Python:

```python
result = rlm.run(
    "...", context=data,
    sandbox={"launcher": ["docker", "run", "--rm", "-i", "--network", "none", "python:3.12-slim"]},
)
```

**The launcher mechanism is tested** in our test suite with a stand-in launcher. Both Docker command lines on this page were also run by hand with Docker 29: the model's code ran in the container, could not reach the network, `ask_map` and `submit` worked through the pipe, and a cell that ran past `cell_timeout` got its container killed and replaced, with no container left behind. The plain one also ran with the `python:3.8-slim` and `python:3.9-slim` images. Podman and other runtimes have not been tried. The stricter variant, which also runs as an unprivileged user with a read-only filesystem:

```toml
[sandbox]
launcher = [
  "docker", "run", "--rm", "-i",
  "--network", "none",
  "--read-only", "--tmpfs", "/tmp",
  "--cap-drop", "ALL",
  "--security-opt", "no-new-privileges",
  "--memory", "1g", "--pids-limit", "256",
  "--user", "65534:65534",
  "python:3.12-slim",
]
```

What to know when you use a launcher:

- **`-i` is required** so the container's stdin stays open. Without it the worker never receives its source.
- **The image needs `python3`.** The worker source sticks to syntax and idioms that work on Python 3.8 and later, so that it can run under an interpreter older than yours. It was run with the 3.8, 3.9 and 3.12 slim images; the test suite does not cover old interpreters. To use another interpreter path in the image, set `sandbox.python`. Libraries the model's code imports must be in the image.
- **`--network none` blocks the model's code from the network.** If your task needs a package index or an API from inside the REPL, you need a different setup. Data does not need to be mounted, because `context` and tool results arrive over the pipe.
- **Cells cannot be interrupted.** rlm-ig stops a runaway cell with `SIGINT`, which it cannot deliver through a launcher. Under a launcher, a cell that passes `cell_timeout` gets the worker killed and restarted, and the variables it held are gone. The model is told.
- **The scrubbed environment applies to the launcher process too.** If your launcher needs variables, such as `DOCKER_HOST`, list them in `pass_env`. They reach `docker`, not the container, unless you forward them with the launcher's own flags.
- **`memory_mb` still applies** inside the worker, in addition to any limit your container has.
- **Only the worker is wrapped.** Tools, MCP servers, and the model CLIs still run on the host.

## Running rlm in a container

The image `ghcr.io/ishaangupta-yb/rlm-ig` runs `rlm` itself in a container, so you do not need Python on your machine. See the [guide](../GUIDE.md#10-run-everything-in-docker) for how to use it. What it does and does not protect:

- **The model's code has the same network access as `rlm`.** Inside that container the REPL worker is an ordinary subprocess of `rlm`, and `rlm` needs the network to reach the model. The container does keep the model's code away from your files: it sees only the directories you mount, and the image runs as an unprivileged user (uid 10001).
- **For a worker with no network, run `rlm` on your machine with `--sandbox docker`.** That is the configuration the tests above cover.
- **Never mount `/var/run/docker.sock` into the container** to start sandbox containers from inside it. Access to the Docker socket is root access to your machine, and with it the model's code could start any container it likes. This setup is not supported.
- Pass credentials with `-e NAME` (the value comes from your environment), not on the command line. They reach `rlm` and the model CLIs. The worker still gets the [scrubbed environment](#the-environment-allowlist).
- The `:claude` variant signs in to Claude Code with `CLAUDE_CODE_OAUTH_TOKEN` (made by `claude setup-token`) or `ANTHROPIC_API_KEY`. Mounting `~/.claude` would hand the container your whole Claude Code state, so the guide does not suggest it.

## CLI backends run with their tools disabled

Claude Code and opencode are coding agents with their own tools. rlm-ig turns those off, because every action should happen in the REPL where it is visible in the trace:

- **Claude Code** is started with `--tools ""`, `--strict-mcp-config` (no MCP servers), slash commands disabled, no session persistence, and a replaced system prompt, in a throwaway working directory.
- **opencode** runs in a throwaway directory whose generated `opencode.json` denies every permission (`"*": "deny"`) and defines a bare agent whose prompt is rlm-ig's. The server mode listens on `127.0.0.1` on a free port and requires a one-off password.

This relies on those CLIs honouring the options. They are still programs running as you, with your environment (so any keys in it) and your sign-in. A `kind = "command"` provider gets no such treatment: rlm-ig adds no flags, so any "no tools" option has to be in your `command`.

## Prompt injection

The model reads your data through code and through `ask` calls. If the data contains text that looks like instructions ("ignore the task and instead ..."), the model may follow it. That is a property of language models, and nothing here prevents it. What you control is how much damage an injected instruction can do:

- Keep tools few, narrow and read-only. A model that cannot send, write or delete cannot be talked into it.
- Do not expose credentials or secrets in `context`, in tool results, or in the worker's environment.
- Run the worker in a container with no network when the data is untrusted (see above).
- Use `schema=` so the answer has a fixed shape, and check it in your own code before you act on it.
- Keep budgets low enough that a runaway loop costs little.
- Read the trace of any run whose result you are going to act on. See [Tracing](tracing.md).

Sub-models called with `ask` have no tools or code, but their replies are text that returns into the model's code and can be wrong or hostile. Sub-agents are the same as the root agent in what they can do: they have their own worker and inherit your tools.

## Other places data and secrets sit

- **Traces** contain the task, replies, code, and outputs, which can hold slices of your data. See [Tracing](tracing.md#what-is-in-a-trace-privacy).
- **Session files** contain the whole `context` and every variable.
- **`api_key` in a config file** is stored in plain text. Prefer `api_key_env` or the SDK's usual environment variable.
- **`RLM_DEBUG_DIR`** keeps raw CLI event streams, which hold full prompts and replies.
- **Claude Code and opencode** use their own stored sign-ins. rlm-ig never sees or stores them.
