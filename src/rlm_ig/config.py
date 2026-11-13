"""Run configuration: defaults, TOML files, environment, keyword overrides."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .sandbox import SandboxOptions

PROJECT_FILE = "rlm.toml"

QUIET, NORMAL, VERBOSE = 0, 1, 2
_VERBOSITY = {"quiet": QUIET, "silent": QUIET, "normal": NORMAL, "verbose": VERBOSE}


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rlm-ig" / "config.toml"


def default_trace_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(base) / "rlm-ig" / "traces")


@dataclass
class Config:
    """Everything that shapes a run. Every field has a working default."""

    #: root model, ``provider:model``. ``None`` picks the first backend that is set up.
    model: Any = None
    #: model for ``ask`` calls and sub-agents. Defaults to ``model``.
    sub_model: Any = None

    #: how many levels of sub-agents may exist below the root
    max_depth: int = 2
    #: turns each agent gets before it must submit
    max_steps: int = 20
    #: model calls for the whole run, all agents combined
    max_calls: int | None = 300
    #: spend ceiling in USD (applies when the backend reports or can estimate cost)
    max_usd: float | None = 5.0
    #: total tokens (input + output) for the whole run
    max_tokens: int | None = None
    #: wall-clock seconds for the whole run
    timeout: float | None = None
    #: seconds a single cell may compute (time spent waiting on sub-calls is not counted)
    cell_timeout: float = 120.0
    #: simultaneous model requests and live sub-agents; ``None`` uses the backend's default
    max_parallel: int | None = None

    #: characters of cell output shown to the model per turn
    output_limit: int = 8000
    #: largest ``ask`` input in characters; ``None`` uses the sub-model's default
    ask_limit: int | None = None
    #: most items ``ask_map`` packs into one sub-model call (1 turns batching off)
    ask_batch: int = 16
    #: most characters of item data per batched call
    ask_batch_chars: int = 24_000
    #: sub-agents get the parent's tools unless ``delegate(tools=...)`` says otherwise
    inherit_tools: bool = True
    #: refuse to hand a sub-agent nearly all of the parent's own context
    passthrough_guard: bool = True

    #: write a JSONL trace of every run
    trace: bool = True
    trace_dir: str = field(default_factory=default_trace_dir)
    #: 0 quiet, 1 normal, 2 verbose (or the names)
    verbosity: int = NORMAL

    sandbox: SandboxOptions = field(default_factory=SandboxOptions)
    #: per-provider settings and custom provider definitions
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: USD per million tokens: ``{"model-id": {"input": 3.0, "output": 15.0, "cached": 0.3}}``
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    #: MCP servers: ``{"name": {"command": ..., "args": [...]}}`` or ``{"name": {"url": ...}}``
    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.verbosity, str):
            if self.verbosity not in _VERBOSITY:
                raise ConfigError(f"verbosity must be one of {', '.join(_VERBOSITY)} or 0-2")
            self.verbosity = _VERBOSITY[self.verbosity]
        if isinstance(self.sandbox, dict):
            self.sandbox = _sandbox_from(self.sandbox)
        # copy, so `replace()` never edits the sandbox options of the config it was called on
        self.sandbox = dataclasses.replace(self.sandbox, cell_timeout=float(self.cell_timeout))
        self.validate()

    def validate(self) -> None:
        if self.max_depth < 0:
            raise ConfigError("max_depth cannot be negative")
        if self.max_steps < 1:
            raise ConfigError("max_steps must be at least 1")
        if self.output_limit < 200:
            raise ConfigError("output_limit must be at least 200 characters")
        if self.ask_batch < 1 or self.ask_batch_chars < 1:
            raise ConfigError("ask_batch and ask_batch_chars must be at least 1")
        if self.max_parallel is not None and self.max_parallel < 1:
            raise ConfigError("max_parallel must be at least 1")
        for name in ("max_calls", "max_usd", "max_tokens", "timeout"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ConfigError(f"{name} must be positive (or None to disable it)")

    def replace(self, **changes: Any) -> Config:
        """A copy with some fields changed. ``None`` values are ignored."""
        changes = {k: v for k, v in changes.items() if v is not None}
        unknown = set(changes) - {f.name for f in dataclasses.fields(self)}
        if unknown:
            raise ConfigError(f"unknown option(s): {', '.join(sorted(unknown))}")
        return dataclasses.replace(self, **changes)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, **overrides: Any) -> Config:
        """Build a config from files, the environment and keyword overrides.

        Later sources win: built-in defaults, then the user file
        (``~/.config/rlm-ig/config.toml``), then the project file (``rlm.toml``
        in the working directory or a parent; or ``path`` / ``$RLM_CONFIG``
        instead), then ``RLM_*`` environment variables, then ``overrides``.
        """
        values: dict[str, Any] = {}
        explicit = path or os.environ.get("RLM_CONFIG")
        files = [user_config_path()]
        if explicit:
            if not Path(explicit).is_file():
                raise ConfigError(f"config file not found: {explicit}")
            files.append(Path(explicit))
        else:
            found = _find_up(PROJECT_FILE)
            if found:
                files.append(found)
        for file in files:
            if file.is_file():
                _merge(values, _read_toml(file))
        _merge(values, _from_env())
        _merge(values, {k: v for k, v in overrides.items() if v is not None})
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ConfigError(f"unknown option(s): {', '.join(sorted(unknown))}")
        try:
            return cls(**values)
        except TypeError as exc:
            raise ConfigError(str(exc)) from exc


def _sandbox_from(raw: dict[str, Any]) -> SandboxOptions:
    known = {f.name for f in dataclasses.fields(SandboxOptions)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown [sandbox] option(s): {', '.join(sorted(unknown))}")
    launcher = raw.get("launcher")
    if isinstance(launcher, str):
        raw = dict(raw, launcher=launcher.split())
    return SandboxOptions(**raw)


def _find_up(name: str) -> Path | None:
    here = Path.cwd()
    for folder in (here, *here.parents):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _merge(into: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            merged = dict(into[key])
            _merge(merged, value)
            into[key] = merged
        else:
            into[key] = value


_ENV = {
    "RLM_MODEL": ("model", str),
    "RLM_SUB_MODEL": ("sub_model", str),
    "RLM_MAX_DEPTH": ("max_depth", int),
    "RLM_MAX_STEPS": ("max_steps", int),
    "RLM_MAX_CALLS": ("max_calls", int),
    "RLM_MAX_USD": ("max_usd", float),
    "RLM_MAX_PARALLEL": ("max_parallel", int),
    "RLM_TIMEOUT": ("timeout", float),
    "RLM_TRACE_DIR": ("trace_dir", str),
}


def _from_env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for var, (key, cast) in _ENV.items():
        raw = os.environ.get(var)
        if raw is None or raw == "":
            continue
        try:
            out[key] = cast(raw)
        except ValueError as exc:
            raise ConfigError(f"${var}={raw!r} is not a valid {cast.__name__}") from exc
    return out
