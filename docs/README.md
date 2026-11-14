# rlm-ig guide

rlm-ig runs recursive language models. Instead of pasting a large document into a prompt, it loads the data into a Python REPL as the variable `context`. The model writes code to explore it, can call smaller models and sub-agents from that code, and finishes with `submit(value)`.

Developed and tested on Linux. macOS should behave the same. On Windows, use WSL.

## Pages

- [Quickstart](quickstart.md): install, check your setup, run the offline demo, make a first real run from the CLI and from Python.
- [Concepts](concepts.md): how a run works, step by step, with an annotated real transcript. Sub-calls versus sub-agents, depth, budgets, the forced wrap-up.
- [REPL API](repl-api.md): reference for everything inside the REPL: `context`, `peek`, `grep`, `chunk`, `ask`, `ask_map`, `delegate`, `delegate_map`, `submit`, `Failed`, exceptions, tools.
- [Backends](backends.md): every `provider:model` string, how the default is detected, and the settings for Claude Code, opencode, the Anthropic API, OpenAI-compatible endpoints, custom commands, and the scripted test model.
- [Configuration](configuration.md): every `Config` field, the load order, a complete `rlm.toml`, environment variables, and the sandbox options.
- [Python API](python-api.md): `run`, `arun`, `Result`, `schema=`, `tools=`, `on_event`, `Session`, `load`, and the errors you can get.
- [Tools and MCP](tools-and-mcp.md): giving the model host functions, and connecting MCP servers.
- [Sessions](sessions.md): keeping one live REPL across several tasks, and saving it to disk.
- [Tracing](tracing.md): the trace file format, `rlm trace`, HTML reports, event callbacks, verbosity.
- [Security](security.md): what the sandbox does and does not do, and how to isolate the worker properly.
- [Troubleshooting](troubleshooting.md): symptoms and fixes, and how to read a trace to see what went wrong.
- [Visual explainer](explainer.html): one self-contained HTML page with an animated replay of a real run, the architecture and data flow, the trade-offs against plain LLM calls, agents and RAG, and production guidance. Download it and open it in a browser; GitHub shows HTML as source.
