# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Planned as 0.2.0.

### Added

- Docker images on GitHub Container Registry: `ghcr.io/ishaangupta-yb/rlm-ig`
  runs the `rlm` command (with a `-claude` variant that includes Claude Code),
  and `ghcr.io/ishaangupta-yb/rlm-ig-sandbox` is a locked-down Python with
  numpy, pandas, python-dateutil and regex for the REPL worker. Multi-arch,
  with provenance, SBOM and signed attestations.
- A one-flag container sandbox: `--sandbox docker[:IMAGE]`,
  `sandbox.preset = "docker"` or `RLM_SANDBOX=docker` runs each REPL worker
  with no network, a read-only root, no capabilities and an unprivileged
  user. `podman` is accepted too (untested).
- `RLM_SANDBOX_LAUNCHER`, `RLM_SANDBOX_PYTHON` and `RLM_SESSION_DIR`
  environment variables, and `OLLAMA_HOST` support for the `ollama` provider.
- `rlm doctor --sandbox`, and `--sandbox` on `rlm run`.
- Container tests against a real Docker daemon (`pytest -m docker`), run in CI.

### Fixed

- Replies with stray tool-call markup (such as `</parameter>`) or an unclosed code
  block followed by prose are repaired before running, instead of failing with a
  SyntaxError and costing a turn. Repairs are reported as a `note` in the trace.
- `result.parsed` builds nested dataclasses: fields typed `list[X]`,
  `tuple[X, ...]`, `dict[str, X]` or `X | None` hold `X` instances.
- `rlm doctor` checks the configured sandbox (and honours `--config`) instead
  of a default one, shows the launcher, and exits non-zero when it fails.
- A `launcher` written as a string keeps quoted arguments together.
- Auto-detection no longer picks the Anthropic backend when the Anthropic SDK
  is not installed, and the error says what to install.
- After Ctrl-C or SIGTERM, `rlm` exits at once instead of waiting for an
  HTTP request that is still in flight. `docker stop` no longer has to kill it.
- An operating system error, such as an unwritable session directory, is
  reported as `rlm: ...` instead of a traceback.
- When the REPL worker fails to start, the error now shows the launcher's real
  stderr instead of only its last line, which for Docker was always
  `Run 'docker run --help' for more information`, plus a hint for a missing
  image, a stopped Docker daemon, or an image without `python3`.
- A launcher that exits immediately is always reported as
  `sandbox failed to start: exit code N; ...`, never as a bare
  `sandbox pipe closed`.

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
