# Backends

A backend is what answers the model calls: the root agent's turns, sub-agent turns, and every `ask`. You pick one with a `provider:model` string, as `model` for the root and optionally `sub_model` for sub-agents and `ask`:

```bash
rlm "..." -f data.json --model claude-code:sonnet --sub-model claude-code:haiku
```

```python
rlm.run("...", context=data, model="claude-code:sonnet", sub_model="claude-code:haiku")
```

```toml
# rlm.toml
model = "claude-code:sonnet"
sub_model = "claude-code:haiku"
```

Or set `RLM_MODEL` and `RLM_SUB_MODEL`. Settings for each provider go in a `[providers.NAME]` table. See [Configuration](configuration.md).

## Provider strings

| String | Backend | Notes |
| --- | --- | --- |
| `claude-code` | Claude Code CLI | Model optional: `claude-code`, `claude-code:sonnet`, `claude-code:haiku`. Aliases `claude` and `cc`. |
| `opencode` | opencode CLI | Model optional, written `provider/model`: `opencode`, `opencode:anthropic/claude-sonnet-5-5`. Alias `oc`. |
| `anthropic:MODEL` | Anthropic Messages API | Needs `pip install "rlm-ig[anthropic]"`. Model required: `anthropic:claude-opus-5-5`. |
| `openai:MODEL` | OpenAI chat completions | `openai:<model-id>`. |
| `openrouter:MODEL` | OpenRouter | `openrouter:<model-id>`. |
| `gemini:MODEL` | Gemini's OpenAI-compatible endpoint | `gemini:<model-id>`. |
| `groq:MODEL` | Groq | `groq:<model-id>`. |
| `deepseek:MODEL` | DeepSeek | `deepseek:<model-id>`. |
| `ollama:MODEL` | A local Ollama server | `ollama:<model-id>`. No key. |
| `NAME:MODEL` | Your own `[providers.NAME]` entry | `kind = "openai"`, `"command"`, `"claude-code"`, `"opencode"` or `"anthropic"`. |
| `command` | Any command-line program | Defined in `[providers.command]`. |

Minimal examples:

```bash
rlm "..." -f data.json -m claude-code
rlm "..." -f data.json -m claude-code:haiku
rlm "..." -f data.json -m opencode
rlm "..." -f data.json -m opencode:anthropic/claude-sonnet-5-5
rlm "..." -f data.json -m anthropic:claude-opus-5-5
rlm "..." -f data.json -m openai:<model-id>
rlm "..." -f data.json -m openrouter:<model-id>
rlm "..." -f data.json -m ollama:<model-id>
```

A few shorthands also work without a provider prefix: a name starting with `claude-` means `anthropic:` (`claude-opus-5-5` is `anthropic:claude-opus-5-5`), and names starting with `gpt-`, `o1`, `o3`, `o4` or `chatgpt-` mean `openai:`. Any other bare string is an error, so write `provider:model`.

A custom provider name always needs a colon in the spec, even when the model part is irrelevant: use `mycli:` or `mycli:anything`, not `mycli`. The model part replaces `{model}` in a command provider and is otherwise sent as the model id.

### The default backend

With no `model` set, rlm-ig picks the first of these that looks ready:

1. `claude` found on `PATH`: `claude-code`.
2. `opencode` found on `PATH`: `opencode`.
3. `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` set: `anthropic:claude-opus-5-5`.

Detection only looks for the binary or the variable. It does not check that you are signed in or that the Anthropic SDK is installed. OpenAI-style keys are never used for auto-detection: set `model` explicitly. If nothing is found, rlm-ig stops with a message that lists the options. `rlm doctor` shows what it found.

### Which account pays

| Backend | Billed to | Cost shown by rlm-ig |
| --- | --- | --- |
| Claude Code | Whatever the `claude` CLI is signed in with: a subscription login, or an API key if the CLI uses one. | The `total_cost_usd` the CLI reports. Under a subscription this is an API-equivalent figure, not a charge. |
| opencode | Whatever providers `opencode` is signed in to (`opencode auth login`). | The cost opencode reports. |
| `anthropic` | The API key you provide. | An estimate from token counts and a price table. |
| OpenAI-compatible | The API key you provide. | OpenRouter's reported cost; otherwise an estimate if the model is in your `[pricing]` table, else none. |
| `command` | Whatever your program uses. | None. |

rlm-ig does not log in for you and does not hold credentials for Claude Code or opencode. `max_usd` is enforced against the cost figure shown above, so on a subscription it limits an API-equivalent number, not money.

## Claude Code

`claude-code` drives the Claude Code CLI. Each agent gets one long-lived `claude -p` process speaking stream-json in both directions. The process holds the conversation, so each turn sends only the new message, later turns hit the prompt cache, and the CLI's start-up cost is paid once per agent.

Claude Code is used as a plain text model. rlm-ig starts it with its own tools turned off (`--tools ""`), its system prompt replaced by rlm-ig's, `--strict-mcp-config` so no MCP servers load, slash commands disabled, and no session persistence. All computation happens in the rlm-ig REPL, where it shows up in the trace. The process runs in a throwaway working directory and gets your environment, minus one variable (`CLAUDE_CODE_ENTRYPOINT`, which would make the CLI add a status summary after every turn).

Install it from [claude.com/claude-code](https://claude.com/claude-code) and sign in by running `claude` once.

### Settings

Put these in `[providers.claude-code]`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `command` | `"claude"` | The executable, looked up on `PATH`. |
| `model` | none | Model, if you did not give one in the spec. |
| `setting_sources` | `"user"` | Which Claude Code settings files apply (`--setting-sources`). `"user"` keeps your login and provider configuration. `""` loads none, which suits API-key setups. |
| `effort` | none | Passed as `--effort`. |
| `args` | `[]` | Extra command-line arguments, appended. |
| `env` | `{}` | Extra environment variables for the process. |
| `strip_api_key` | `false` | Remove `ANTHROPIC_API_KEY` from the process environment so a subscription login is used even when a key is exported. |
| `timeout` | `900` | Seconds a whole turn may take. |
| `stall_timeout` | `90` | Seconds with no progress before the session is treated as stalled. |
| `retries` | `2` | How many times a lost session is rebuilt. |
| `max_output_tokens` | `32000` | Sets `CLAUDE_CODE_MAX_OUTPUT_TOKENS` for the process, unless that variable is already set in your environment. |
| `max_reply_chars` | `80000` | Backstop on the length of one reply for the runaway guard. |
| `window_chars` | `400000` | Largest `ask` input this model should be given. |
| `parallel` | `4` | Default number of simultaneous requests. |

Unknown setting names are ignored, so check spelling.

```toml
[providers.claude-code]
effort = "low"
strip_api_key = true
timeout = 1200
```

Or from Python:

```python
rlm.run("...", context=data, model="claude-code", providers={"claude-code": {"effort": "low"}})
```

### Keeping long runs alive

Three mechanisms protect a run from a bad session:

- **Stall watchdog.** The reader waits in 5 second slices. Keep-alive pings do not count as progress. If nothing but pings arrives for `stall_timeout` seconds, the session is treated as lost, even though the CLI's own retry would wait far longer.
- **Runaway guard.** While a reply streams, rlm-ig checks it every 1,000 characters. A reply longer than `max_reply_chars`, or whose last 600 or so characters are one short fragment repeated, is cut off and the process is killed so it stops generating. The turn returns the text so far, and a `note` event records `the model's output ran away and was cut off; its session was restarted`.
- **Transcript replay.** After a lost session (the process exited, a turn exceeded `timeout`, a stall, or a runaway), the next attempt starts a fresh process and sends the whole exchange so far as one message, with each earlier reply over 6,000 characters shortened. The model continues as if nothing happened. A lost session is retried up to `retries` times before the turn fails.

Errors you may meet:

- `Claude Code was not found (looked for 'claude' on PATH)...`: install it, or set `command`.
- `Claude Code reported an error: ...`: the CLI returned an error result. If the message mentions login or authentication, run `claude` once to sign in.
- `Claude Code did not finish a turn within 900s`: raise `timeout` if the model really needs longer.
- `the model declined to answer (stop_reason: refusal)`.

`rlm doctor --live` prints the model the CLI served, its auth source, and its version. Set `RLM_DEBUG_DIR` to an existing directory to keep the raw event stream of every CLI process, one file per process. See [Tracing](tracing.md#debugging-the-backend).

## opencode

`opencode` drives the opencode CLI. Install it from [opencode.ai](https://opencode.ai) and sign in with `opencode auth login`. List the available models with `opencode models`. The model is written `provider/model`:

```bash
rlm "..." -f data.json -m opencode:anthropic/claude-sonnet-5-5
```

A model without a slash is rejected before anything starts.

Like Claude Code, opencode is used as a text model only. rlm-ig writes an `opencode.json` into a throwaway working directory that denies every permission (`"permission": {"*": "deny"}`) and defines a bare agent named `rlm` whose prompt is rlm-ig's. opencode's own coding-agent prompt and tool definitions never reach the model.

### Server mode and run mode

| `mode` | How it works |
| --- | --- |
| `server` | One headless `opencode serve` process for the whole run, bound to `127.0.0.1` on a free port and protected by a one-off password. Each conversation is a server-side session, so a turn is one HTTP request. Preferred. |
| `run` | One `opencode run` process per turn, continuing a session by id. Slower, because every turn pays a start-up, but it needs nothing beyond the basic CLI. |
| `auto` (default) | Try the server. If it cannot start, fall back to `run` and remember that. |

### Settings

Put these in `[providers.opencode]`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `command` | `"opencode"` | The executable. |
| `model` | none | Model, if not in the spec. |
| `mode` | `"auto"` | `auto`, `server` or `run`. |
| `args` | `[]` | Extra arguments, appended to `opencode serve` or `opencode run`. |
| `env` | `{}` | Extra environment variables. |
| `timeout` | `600` | Seconds a turn may take. |
| `start_timeout` | `45` | Seconds to wait for the server to come up. |
| `retries` | `1` | Retries after a retryable failure (a timeout or a connection error). The new session replays the transcript. |
| `error_grace` | `30` | Seconds opencode may keep retrying a failing upstream request by itself before the turn is given up. Quota, billing and authentication errors fail immediately. |
| `window_chars` | `400000` | Largest `ask` input. |
| `parallel` | `4` | Default simultaneous requests. |

The process gets your environment. Billing is whatever your opencode sign-in uses. Errors that mention credentials include a hint to run `opencode auth list` and `opencode auth login`.

opencode retries a failed upstream request on its own and reports it only in its log, sometimes for a long time (a quota error can carry a retry delay of hours). rlm-ig runs it with error logging on and watches for those lines, so a quota or credential problem ends the turn within seconds with the provider's message instead of looking like a hang.

## Anthropic API

```bash
pip install "rlm-ig[anthropic]"
export ANTHROPIC_API_KEY=...
rlm "..." -f data.json -m anthropic:claude-opus-5-5
```

The SDK finds credentials itself: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or a profile from `ant auth login`. A model id is required. Requests stream, so long replies do not hit HTTP timeouts.

Prompt caching is on. One cache breakpoint sits on the system prompt, which every agent in a run shares, and one on the newest message, so each turn re-reads the growing conversation from cache instead of paying for it again. Cached tokens appear in `usage.cached_tokens`. When a conversation gets long (over `compact_at` characters), the bodies of older REPL outputs are shortened in place, in strides, so the cache is invalidated rarely.

### Settings

Put these in `[providers.anthropic]`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `model` | none | Model, if not in the spec. |
| `max_tokens` | `32000` | Output ceiling per request. |
| `params` | `{}` | Extra request fields, merged into every request, for example `{ output_config = { effort = "low" } }`. |
| `refusal_fallback` | on for supported models | See below. |
| `api_key` | none | An explicit key. Prefer the environment. |
| `api_key_env` | none | Name of an environment variable that holds the key. |
| `base_url` | SDK default | Another endpoint. |
| `retries` | `4` | SDK retry count. |
| `timeout` | `600` | Request timeout in seconds. |
| `compact_at` | `600000` | Conversation size, in characters, at which older output is shortened. |
| `max_reply_chars` | `80000` | Runaway guard: a reply longer than this, or ending in a long repeated fragment, is cut off. |
| `window_chars` | `400000` | Largest `ask` input. |
| `parallel` | `8` | Default simultaneous requests. |

### Refusal fallback

For some models, a request that a safety classifier declines can be retried server-side on a fallback model that Anthropic recommends for that refusal category, inside the same call. rlm-ig turns this on by default for `claude-fable-5-2`, `claude-fable-5-1`, `claude-opus-5-5`, `claude-opus-5` and `claude-sonnet-5-5`. It uses the SDK's beta endpoint with the `server-side-fallback-2026-07-01` beta. Set `refusal_fallback = false` to turn it off, or `true` to ask for it on another model. If the SDK or endpoint does not know the option, rlm-ig switches it off and carries on. When a request is refused with no fallback, the call fails with `the model declined the request (refusal category: ...)`.

### Pricing and cost

Cost is estimated from the token counts (fresh input, cache writes, cache reads, output) and a price table. The built-in table covers these model ids: `claude-fable-5-2`, `claude-fable-5-1`, `claude-fable-5`, `claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-5-5`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-5-5` and `claude-haiku-4-5`. It will age. Anything in your config wins:

```toml
[pricing."claude-haiku-5-5"]
input = 0.10     # USD per million input tokens
output = 0.50    # USD per million output tokens
cached = 0.01    # USD per million cache-read tokens; default is 0.1 x input
```

A model that is in neither table has no cost estimate. Token and call limits still apply, `max_usd` cannot be enforced for it, and rlm-ig emits a `note` saying so. Cache writes are priced at 1.25 times the input price.

A cheaper sub-model is an easy saving, because every `ask` and sub-agent turn uses it:

```python
rlm.run(
    "...", context=data,
    model="anthropic:claude-opus-5-5",
    sub_model="anthropic:claude-haiku-5-5",
)
```

## OpenAI-compatible endpoints

One backend, with no dependencies, speaks the chat-completions protocol. These presets are built in:

| Provider | Base URL | Key variable |
| --- | --- | --- |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | none |

For `openai`, the `OPENAI_BASE_URL` environment variable overrides the base URL when `base_url` is not set.

### Settings

Put these in `[providers.NAME]`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `model` | none | Model, if not in the spec. |
| `base_url` | preset | Endpoint root. The request goes to `<base_url>/chat/completions`. |
| `api_key` | none | An explicit key. |
| `api_key_env` | preset | Name of the variable holding the key. |
| `headers` | `{}` | Extra HTTP headers. |
| `params` | `{}` | Extra fields merged into every request body: `temperature`, `max_tokens`, `reasoning_effort`, and so on. |
| `timeout` | `600` | Seconds per request. |
| `retries` | `4` | Retries on HTTP 408, 409, 425, 429, 500, 502, 503, 504 and 529, and on connection errors, with backoff and honouring `Retry-After`. |
| `compact_at` | `300000` | Conversation size at which older output is shortened. |
| `window_chars` | `400000` | Largest `ask` input. |
| `parallel` | `8` | Default simultaneous requests. |

Requests are not streamed. For OpenRouter, rlm-ig asks for usage accounting so the reported cost is available. For the others, cost is estimated only when the model is in your `[pricing]` table.

### A custom endpoint

Any server that speaks the same protocol (a local inference server, a gateway, a hosted service) can be defined with `kind = "openai"`:

```toml
[providers.local]
kind = "openai"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"      # optional
params = { temperature = 0 }
```

Select it with a spec that carries any model id the server accepts:

```bash
rlm "..." -f data.json -m local:<model-id>
```

`kind` can also change what a built-in name does. Valid kinds are `openai`, `command`, `claude-code`, `opencode` and `anthropic`, so you can define several named variants of one backend:

```toml
[providers.cc-fast]
kind = "claude-code"
model = "haiku"
effort = "low"
```

Then `-m cc-fast:` selects it.

## Any command: `kind = "command"`

For a tool with no dedicated backend, define a program that reads a prompt on stdin and writes a reply on stdout:

```toml
[providers.mycli]
kind = "command"
command = ["/usr/local/bin/mycli", "chat", "--model", "{model}"]
json_path = "choices.0.text"     # optional
```

Select it with `-m mycli:some-model`. Each turn runs the command once. rlm-ig adds no flags of its own, so put any "no tools" or read-only options the program has into `command`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `command` | required | A list, or a string that is split on spaces. `{model}` is replaced by the model part of the spec. |
| `json_path` | none | If set, stdout is JSON and the reply is at this dotted path (`choices.0.text`). Otherwise stdout, stripped, is the reply. |
| `env` | `{}` | Extra environment variables, added to your environment. |
| `timeout` | `600` | Seconds per turn. |
| `compact_at` | `300000` | Size at which older output is shortened. |
| `window_chars`, `parallel` | `400000`, `4` | As above. |

The program receives the system prompt and the conversation on stdin in this layout, and the reply is whatever it prints:

```text
<system prompt>

### USER
<first message>

### ASSISTANT
<reply>

### USER
<next message>

### ASSISTANT
```

The program runs in a throwaway working directory, so use absolute paths for scripts and files. The first element of `command` is looked up on `PATH`. A non-zero exit becomes an error carrying the tail of stderr. Token counts are estimated from character counts and no cost is reported.

## Scripted, for tests

`Scripted` is a model whose replies come from your own code. The whole loop (REPL, sub-calls, budgets, traces) runs for real. Only the intelligence is supplied by you, so it is the way to test code that uses rlm-ig, and to learn how a cell behaves. It has no string form: pass the object as `model`.

```python
import rlm_ig as rlm
from rlm_ig import Scripted

def reply(code):
    return f"Counting.\n\n```python\n{code}\n```"

model = Scripted([reply("submit(len(context.split()))")])
result = rlm.run("Count the words.", context="one two three", model=model, verbosity=0)
print(result.answer)   # 3
```

`Scripted(script, *, asks=None, cost_per_call=0.0, delay=0.0, name="scripted")`:

- `script` is a list of replies, one per turn (every conversation starts from the top), or a function `script(turn)` that returns the reply. The function can be `async` and can raise an exception to simulate a backend error.
- `asks` supplies replies to one-shot calls (`ask`, `ask_map`): a function of the prompt, a list consumed in order, or a fixed string. When omitted, one-shot calls use `script`. Note that `ask_map` packs small items into one batched call. The prompt your `asks` function receives for a batch contains `<item index="1">...</item>` blocks, and the reply it must give is a JSON array with one element per item. If it gives anything else, rlm-ig falls back to one call per item. Set `ask_batch=1` to get one plain prompt per item.
- `cost_per_call` is the cost reported for every call, to test `max_usd`.
- `delay` sleeps before each reply, to test parallelism and timeouts.
- `name` becomes the spec, `scripted:NAME`.

A `Turn` has `system`, `messages`, `index` (0 for the first reply in this conversation), `oneshot`, and the properties `first` (the first user message: task and data summary) and `last` (the newest user message). The model records every turn in `model.turns`, which is how you assert on what the engine sent.

A function script can play different roles by looking at the turn:

```python
import re
import rlm_ig as rlm
from rlm_ig import Scripted

def script(turn):
    if turn.oneshot:                       # an ask() call
        data = re.search(r"<data>\n(.*)\n</data>", turn.last, re.S).group(1)
        return data.upper()
    if "Shout" in turn.first:              # a sub-agent
        return "```python\nsubmit(context.upper())\n```"
    return "```python\nsubmit(delegate('Shout it.', 'hello'))\n```"   # the root

result = rlm.run("Say hello.", model=Scripted(script), verbosity=0)
print(result.answer)   # HELLO
print(result.agents)   # 2
```

Any object that implements the `rlm_ig.Model` interface can also be passed as `model`.
