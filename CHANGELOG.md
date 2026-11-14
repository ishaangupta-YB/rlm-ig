# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-14

First public release.

- Recursive agent engine over a persistent native Python REPL, with `ask`,
  `ask_map`, `delegate` and `delegate_map` sub-calls and automatic batching.
- Backends: Claude Code, opencode, Anthropic API, OpenAI-compatible APIs and
  generic commands.
- Per-run limits on calls, cost, tokens and wall-clock time, with partial
  results when a limit is hit.
- Schema-validated answers, tools, MCP client, sessions.
- The `rlm` command with `run`, `doctor`, `trace` and `demo`; JSONL traces
  with terminal and HTML viewers.
- A step-by-step guide with worked examples on public Loghub datasets.

[Unreleased]: https://github.com/ishaangupta-yb/rlm-ig/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ishaangupta-yb/rlm-ig/releases/tag/v0.1.0
