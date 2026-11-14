# Plan: bug fixes and Docker support

Hand-off plan for an implementation session. Read it fully before you start.
Delete this file in the last commit once everything here is done.

## Ground rules

- Work on a feature branch and open a pull request against `main`. Do not push to `main` directly.
- Keep the core package free of third-party dependencies. Docker support must not add any to `pyproject.toml` `dependencies`.
- Every change needs a test, or a written reason why it cannot be tested.
- Before every commit: `ruff check .`, `ruff format --check src tests examples`, `pytest`. All must pass (434 passed and 6 skipped at the time of writing).
- Do not change how `.github/workflows/release.yml` publishes to PyPI (Trusted Publishing, `pypi` and `testpypi` environments). Adding jobs next to it is fine.
- Pin every GitHub Action by full commit SHA with a `# vX.Y.Z` comment, exactly like `ci.yml`. Resolve SHAs with `git ls-remote --tags https://github.com/OWNER/REPO` (use the `^{}` dereferenced commit for annotated tags). Only pick releases that are at least two weeks old.
- Pin Docker base images by digest (`python:3.13-slim@sha256:...`) and let Dependabot update them.
- Docs must say only what was verified. If you did not run something, say so in the doc, as the current docs do.
- Version: Part A can ship in 0.1.x. Part B is new functionality and should ship as 0.2.0. Bump `__version__` in `src/rlm_ig/__init__.py` only, and update `CHANGELOG.md`.

## Part A: bugs and shortcomings found during review

Each item lists the evidence, the fix and the test. Priority P0 = fix before Docker work, P1 = needed for Docker, P2 = nice to have.

### A1 (P0) Nested dataclasses in `schema=` are not built

- **Evidence:** `src/rlm_ig/api.py:239` `_parse` builds dataclasses with `schema(**answer)`. With `schema=Report` where `Report.incidents: list[Incident]`, `result.parsed.incidents[0]` is a `dict`, and `.source_ip` raises `AttributeError`. Reproduced with a real run. Documented as a limitation in `docs/python-api.md:155`, README "Known limits" and `GUIDE.md` section 8.
- **Fix:** replace the one-level build with a recursive builder driven by `typing.get_type_hints`. Handle: nested dataclasses, `list[X]`, `tuple[X, ...]`, `dict[str, X]`, `X | None` / `Optional[X]`, and `Literal`/enum values passed through unchanged. Leave TypedDict values as dicts (they are dicts). If anything fails, keep today's behaviour: `parsed = None`, `answer` still set.
- **Test:** in `tests/test_text_schema.py` or a new `tests/test_parse.py`, use `Scripted` to submit a nested value and assert `isinstance(result.parsed.incidents[0], Incident)`. Add a case where the build fails and `parsed is None`.
- **Docs:** remove the limitation from `docs/python-api.md`, README "Known limits" and `GUIDE.md` section 8. In the GUIDE example, switch the loop back to `result.parsed.incidents` and `i.source_ip`.

### A2 (P0) `rlm doctor` ignores the configured sandbox

- **Evidence:** `src/rlm_ig/cli.py:248` builds `Sandbox()` with default options. A broken `sandbox.launcher` in `rlm.toml` (for example a Docker image that does not exist) still shows `+ sandbox` in `rlm doctor`.
- **Fix:** load the config the same way `run` does (honour `--config`), pass `config.sandbox` to `Sandbox`, and print the launcher in the row, e.g. `+ sandbox  REPL worker starts and runs code in 410 ms (launcher: docker run ...)`. On failure print `x` and the error, and make `doctor` exit non-zero.
- **Test:** `tests/test_cli.py`: a config with a launcher that fails (`["false"]`) shows `x sandbox`, and the stand-in launcher already used by the sandbox tests shows `+`.

### A3 (P1) A `launcher` written as a string is split on spaces

- **Evidence:** `src/rlm_ig/config.py:167` uses `launcher.split()`, so quoted arguments break.
- **Fix:** `shlex.split(launcher)`.
- **Test:** `launcher = "docker run --label 'a b' img"` gives `["docker", "run", "--label", "a b", "img"]`.

### A4 (P1) No way to set the sandbox launcher, session directory or Ollama address from the environment

These are what make a container image configurable without mounting a config file.

- **Evidence:** `_ENV` in `src/rlm_ig/config.py:192` has no sandbox entry. The Ollama URL is hard-coded to `http://localhost:11434/v1` (`src/rlm_ig/models/openai_compat.py:32`), which is unreachable from inside a container. CLI sessions are always written to `./.rlm/sessions` (`src/rlm_ig/cli.py:198`), which fails on a read-only mount.
- **Fix:**
  - `RLM_SANDBOX_LAUNCHER` (parsed with `shlex.split`) and `RLM_SANDBOX_PYTHON`, merged into the `sandbox` table.
  - `OLLAMA_HOST`, Ollama's own variable: when `[providers.ollama] base_url` is not set, build the URL from it. Accept `host:port`, `http://host:port` and `https://...`, and append `/v1`.
  - `RLM_SESSION_DIR`: the directory for `--session NAME` files. The default stays `./.rlm/sessions`.
- **Test:** one test per variable in `tests/test_config.py`, plus the Ollama URL derivation in `tests/test_api_backends.py`.
- **Docs:** add them to the environment variable table in `docs/configuration.md`.

### A5 (P2) Auto-detection picks the Anthropic backend when the SDK is missing

- **Evidence:** `src/rlm_ig/models/__init__.py:95` returns `anthropic:claude-opus-5-5` whenever `ANTHROPIC_API_KEY` is set. Without the `anthropic` extra, the run then fails with "the anthropic backend needs the Anthropic SDK". The message is clear, but detection should not choose a backend it knows cannot work.
- **Fix:** also require `importlib.util.find_spec("anthropic")`. If the key is set and the SDK is missing, extend `NO_BACKEND` with a line saying exactly that.
- **Test:** monkeypatch `find_spec` to return `None`.

### A6 (P1) A one-flag Docker sandbox

The hardened Docker launcher works (verified by hand, see `docs/security.md`), but it is a 20-word command line people will get wrong.

- **Fix:** add a preset. `sandbox.preset = "docker"` in config, `--sandbox docker[:IMAGE]` on the CLI, `RLM_SANDBOX=docker[:IMAGE]` in the environment. It expands to exactly:
  ```text
  docker run --rm -i --network none --read-only --tmpfs /tmp -w /tmp
    --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256
    --user 65534:65534 IMAGE
  ```
  The default `IMAGE` is `python:3.12-slim` until the B2 sandbox image exists, then that image with a pinned tag. An explicit `launcher` wins over a preset. Accept `podman` as well (`--sandbox podman[:IMAGE]`) with the same flags, but mark it untested in the docs unless you test it.
- **Note:** `-w /tmp` is new compared to the verified command. It gives the model's code a writable working directory, because `/` is read-only. Check it by hand.
- **Test:** unit-test the expansion. Add a `docker`-marked integration test (see B5).

### A7 (P2) Decide the development status classifier

`pyproject.toml` says `Development Status :: 4 - Beta`. For a first 0.x release whose API may still change, `3 - Alpha` is more accurate. This is the maintainer's decision. Ask; do not change it silently.

## Part B: Docker support

### What Docker is for here, and what it is not

Users should be able to use rlm-ig in three ways: from PyPI, from a clone, or with Docker. Docker serves two separate needs. Keep them separate in the code and in the docs:

1. **Isolating the model's code** (the security win). `rlm` runs on the host and only the REPL worker runs in a locked-down container with no network. This already works through `sandbox.launcher`. B2 and A6 make it easy.
2. **Running the whole tool without installing Python** (the convenience win). `rlm` itself runs in a container. B1.

The weak points of (2), which the docs must state plainly:

- Inside the app container, the REPL worker is an ordinary subprocess of the same container. The container shields your host files (only what you mount is visible), but **the model's code has the same network access as `rlm`**, because `rlm` needs the network to reach the model. For network isolation of the model's code, use (1).
- **Claude Code and opencode in a container are the awkward case.** Their sign-in lives in the user's home directory. Mounting `~/.claude` into a container is fragile and gives the container your whole Claude Code state. Support Claude Code through an environment variable instead: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, which Claude Code's docs describe for CI and headless use, or `ANTHROPIC_API_KEY`. Check the current wording at https://code.claude.com/docs/en/authentication before you document it.
- **Never mount `/var/run/docker.sock` into the app container** to combine (1) and (2). That gives the container root on the host. Document this as unsupported.

### B1 App image: `ghcr.io/ishaangupta-yb/rlm-ig`

Files: `Dockerfile` and `.dockerignore` at the repo root.

Requirements:

- **Multi-stage build.** Builder stage: `python:3.13-slim` (by digest) with `pip install build`, then `python -m build --wheel` from the source tree. Runtime stage: `python:3.13-slim` (same digest) with `pip install --no-cache-dir "/tmp/dist/rlm_ig-*.whl[anthropic]"`, then delete the wheel. Do not copy the source tree into the runtime stage.
- **Non-root:** create user `rlm` with uid/gid 10001, home `/home/rlm`, and switch to it with `USER 10001:10001`.
- `WORKDIR /work`, which is where users mount their data.
- `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_STATE_HOME=/home/rlm/.local/state RLM_SESSION_DIR=/home/rlm/.local/state/rlm-ig/sessions`. The last one needs A4.
- **PID 1:** `rlm` starts worker subprocesses, and Claude Code processes in the claude variant. Install `tini` from apt and use `ENTRYPOINT ["tini", "--", "rlm"]`, with `CMD ["--help"]`.
- OCI labels: `org.opencontainers.image.source=https://github.com/ishaangupta-yb/rlm-ig`, `licenses=MIT`, plus `version` and `revision` from build args. `docker/metadata-action` fills these in CI.
- `.dockerignore`: `.git`, `.github`, `.venv`, `dist`, `build`, `**/__pycache__`, `.pytest_cache`, `.ruff_cache`, `.rlm`, `*.jsonl`, `plans`, `tests`, `docs`, `examples`. The build needs only `pyproject.toml`, `README.md`, `LICENSE` and `src/`. Hatchling reads `README.md` for metadata, so it must not be ignored.
- **Variant `claude`** (tag suffix `-claude`, build arg `WITH_CLAUDE_CODE=1`, or a second target `FROM runtime AS claude`): add Node.js 22 and Claude Code at a pinned version. Use the install method Claude Code's docs recommend at the time, from https://code.claude.com/docs/en/setup. If that is the npm package, `npm install -g @anthropic-ai/claude-code@X.Y.Z`. If it is the native installer, pin the version and verify its checksum. Never `curl | bash` an unpinned script.
- Skip an opencode variant in this pass. Document how to extend the image `FROM ghcr.io/ishaangupta-yb/rlm-ig` instead.
- Target size: the default image under 200 MB. Report the real size in the PR.

Usage to document (README "With Docker" and a new GUIDE section). Test every line before you document it:

```bash
# API backend, data in the current directory, traces kept in a named volume
docker run --rm -e ANTHROPIC_API_KEY -v "$PWD:/work:ro" -v rlm-state:/home/rlm/.local/state \
  ghcr.io/ishaangupta-yb/rlm-ig "Which IPs failed to log in most?" -f OpenSSH_2k.log -m anthropic:claude-haiku-5-5

# stdin needs -i (not -t)
cat server.log | docker run --rm -i -e OPENAI_API_KEY ghcr.io/ishaangupta-yb/rlm-ig "Summarise the outage" -m openai:<model>

# Claude Code, headless token
docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN -v "$PWD:/work:ro" ghcr.io/ishaangupta-yb/rlm-ig:claude "..." -f data.log -m claude-code:haiku

# Ollama on the host (needs A4)
docker run --rm --add-host=host.docker.internal:host-gateway -e OLLAMA_HOST=http://host.docker.internal:11434 \
  -v "$PWD:/work:ro" ghcr.io/ishaangupta-yb/rlm-ig "..." -f data.log -m ollama:<model>

# Writing output into the mounted directory: match your uid
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e ANTHROPIC_API_KEY -v "$PWD:/work" \
  ghcr.io/ishaangupta-yb/rlm-ig "..." -f data.log -o answer.txt
```

Check that the last form works with an arbitrary uid: `XDG_STATE_HOME` must be writable or the run must fall back cleanly. If it fails, fix the image (for example by making the state directory world-writable with the sticky bit) and test again.

### B2 Sandbox image: `ghcr.io/ishaangupta-yb/rlm-ig-sandbox`

The image the REPL worker runs in under `sandbox.launcher` / `--sandbox docker`.

- File: `docker/sandbox.Dockerfile`. Base `python:3.12-slim` by digest.
- Install the libraries the model's code most often wants, at pinned versions: `numpy`, `pandas`, `python-dateutil`, `regex`. Keep it small. List them in the docs so people know what is importable.
- No rlm-ig inside: the worker source is sent over stdin, which is the whole design.
- Run as uid 65534 (`nobody`), with no extra packages, shell tools or compilers beyond what the base has.
- Make A6's default image this one, pinned to the matching release tag, and fall back to `python:3.12-slim` when a user asks for it explicitly.

### B3 Image CI and publishing: `.github/workflows/docker.yml`

- **Triggers:** `pull_request` (build only, amd64, no push), `push` to `main` (push tag `edge`), tags `v*` (push `X.Y.Z`, `X.Y` and `latest`, plus `-claude` equivalents for the claude variant).
- **Permissions:** top level `contents: read`. The publish job adds `packages: write`, `id-token: write`, `attestations: write`. Nothing else.
- **Steps:** `actions/checkout` (with `persist-credentials: false`), `docker/setup-qemu-action`, `docker/setup-buildx-action`, `docker/login-action` to `ghcr.io` with `${{ github.actor }}` / `${{ secrets.GITHUB_TOKEN }}` (no personal token), `docker/metadata-action` for tags and labels, `docker/build-push-action` with `platforms: linux/amd64,linux/arm64`, `provenance: mode=max`, `sbom: true`, and GitHub Actions cache. Then `actions/attest-build-provenance` with `push-to-registry: true` for each pushed digest.
- **Tag check:** on a tag, fail if the tag is not `v` + `__version__`, using the same check as `release.yml`.
- **Smoke test before push** (amd64): load the image and run `--version`, `demo`, `docker run --rm --entrypoint id IMAGE -u` (must print `10001`), and a stdin pipe test with `-i`.
- **Vulnerability scan:** optional. If you add a scanner action, pin it by SHA and check its recent security advisories before adopting it. Scanner actions have been targets of supply-chain attacks. Fail only on fixable critical findings.
- **Dependabot:** add `package-ecosystem: docker` for `/` and `/docker` to `.github/dependabot.yml`, so base image digests get updated.
- **Manual step for the maintainer, to put in RELEASING.md:** after the first push, open the package on GitHub (profile > Packages > rlm-ig), set its visibility to **Public** and link it to the repository. GHCR packages start private.

### B4 Optional: `compose.yaml` with Ollama

Only if time allows: a `compose.yaml` with an `ollama` service and an `rlm` service (`profiles: [cli]`) wired through `OLLAMA_HOST=http://ollama:11434`, for a fully local setup. Document the first `ollama pull`. Mark it untested in the docs unless you run it.

### B5 Tests

- `tests/test_docker.py`, marked `@pytest.mark.docker` and skipped unless `docker info` succeeds. Register the marker in `pyproject.toml`. With `Scripted` models and the A6 preset on `python:3.12-slim`, assert that:
  1. a run completes and `ask_map` works through the pipe;
  2. `socket.create_connection(("1.1.1.1", 53), timeout=2)` raises inside the worker;
  3. a cell past `cell_timeout` reports that the worker restarted, and `docker ps -q --filter ancestor=python:3.12-slim` is empty afterwards (allow a few seconds);
  4. writing to `/tmp` works and writing to `/` fails.
- In `ci.yml`, run `pytest -m docker` in the ubuntu job. Docker is available on `ubuntu-latest`. Pull the image first so the pull time does not count against cell timeouts.
- Image smoke tests as in B3.

### B6 Documentation to update

- `README.md`: the "With Docker" paragraph under Install becomes a short section with the B1 commands. Add the sandbox preset to Security. Update "Known limits".
- `GUIDE.md`: section 1 option D "Docker", a new section "Run everything in Docker", and section 9 switched to `--sandbox docker`. Re-run every command you change and update its **real output** block. Never invent output.
- `docs/security.md`: the preset, the sandbox image and its library list, the network caveat of the app image, and the docker.sock warning.
- `docs/configuration.md`: `preset`, the new environment variables, `RLM_SESSION_DIR`.
- `RELEASING.md`: the GHCR visibility step, and how image tags relate to PyPI versions.
- `CHANGELOG.md`: entries under `Unreleased`.

## Acceptance checklist

- [ ] A1 to A5 fixed with tests. A6 implemented. A7 asked.
- [ ] `docker build .` works from a clean clone; image under 200 MB; runs as uid 10001; `demo` passes inside it.
- [ ] Every documented `docker run` line was run and works.
- [ ] `docker.yml` builds on PRs without pushing, and pushes signed, attested multi-arch images only from `main` and `v*` tags.
- [ ] All actions pinned by SHA; base images pinned by digest; Dependabot covers both.
- [ ] `pytest` and `pytest -m docker` pass in CI on the PR.
- [ ] README, GUIDE, docs and CHANGELOG updated, with real outputs only.
- [ ] This plan file deleted.
