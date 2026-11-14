"""Model backends and the ``provider:model`` strings that select them."""

from __future__ import annotations

import importlib.util
import os
import shutil
from collections.abc import Callable
from typing import Any

from ..errors import ConfigError
from .base import Chat, Model, Reply, StatelessChat
from .scripted import Scripted, Turn

# provider name -> (module, class). Imported on first use so that a run never
# pays for (or requires the dependencies of) a backend it does not touch.
_BUILTIN: dict[str, tuple[str, str]] = {
    "claude-code": ("claude_code", "ClaudeCode"),
    "opencode": ("opencode", "OpenCode"),
    "anthropic": ("anthropic_api", "AnthropicAPI"),
    "openai": ("openai_compat", "OpenAICompatible"),
    "openrouter": ("openai_compat", "OpenAICompatible"),
    "gemini": ("openai_compat", "OpenAICompatible"),
    "groq": ("openai_compat", "OpenAICompatible"),
    "deepseek": ("openai_compat", "OpenAICompatible"),
    "ollama": ("openai_compat", "OpenAICompatible"),
    "command": ("command", "CommandModel"),
}

# How a custom [providers.<name>] entry picks its implementation via `kind`.
_KINDS = {
    "claude-code": "claude-code",
    "opencode": "opencode",
    "anthropic": "anthropic",
    "openai": "openai",
    "command": "command",
}

ALIASES = {"claude": "claude-code", "cc": "claude-code", "oc": "opencode"}


def providers() -> list[str]:
    return sorted(_BUILTIN)


def split_spec(spec: str) -> tuple[str, str]:
    """``"provider:model"`` -> ``(provider, model)``; the model may be empty."""
    spec = spec.strip()
    provider, sep, name = spec.partition(":")
    provider = ALIASES.get(provider, provider)
    if not sep:
        if provider in _BUILTIN:
            return provider, ""
        lowered = spec.lower()
        if lowered.startswith("claude-"):
            return "anthropic", spec
        if lowered.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
            return "openai", spec
        return "", spec
    return provider, name


def resolve(spec: Any, config: Any) -> Model:
    """Turn a spec string (or a ready ``Model``) into a ``Model`` instance."""
    if isinstance(spec, Model):
        return spec
    if not isinstance(spec, str) or not spec.strip():
        raise ConfigError(f"model must be a 'provider:model' string or a Model, got {spec!r}")
    provider, name = split_spec(spec)
    settings = dict((config.providers or {}).get(provider) or {})
    kind = settings.pop("kind", None)
    if provider in _BUILTIN:
        impl = provider if kind is None else _KINDS.get(kind, kind)
    elif kind is not None:
        impl = _KINDS.get(kind)
        if impl is None:
            raise ConfigError(f"[providers.{provider}] kind={kind!r} is not one of: {', '.join(sorted(_KINDS))}")
    else:
        known = ", ".join(providers())
        raise ConfigError(
            f"unknown model {spec!r}. Use 'provider:model' with a provider from: {known}; "
            f"or define [providers.{provider or 'NAME'}] with a kind in your config."
        )
    module_name, class_name = _BUILTIN[impl]
    module = __import__(f"{__name__}.{module_name}", fromlist=[class_name])
    factory: Callable[..., Model] = getattr(module, class_name)
    return factory(provider=provider, model=name, settings=settings, pricing=config.pricing or {})


def detect() -> str | None:
    """The first backend that looks ready on this machine, if any."""
    if shutil.which("claude"):
        return "claude-code"
    if shutil.which("opencode"):
        return "opencode"
    if anthropic_key_set() and anthropic_sdk_installed():
        return "anthropic:claude-opus-5-5"
    return None


def anthropic_key_set() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def anthropic_sdk_installed() -> bool:
    try:
        return importlib.util.find_spec("anthropic") is not None
    except (ImportError, ValueError):
        return False


def no_backend_message() -> str:
    """:data:`NO_BACKEND`, plus a line when an Anthropic key is set but its SDK is missing."""
    if anthropic_key_set() and not anthropic_sdk_installed():
        return NO_BACKEND + ANTHROPIC_SDK_MISSING
    return NO_BACKEND


NO_BACKEND = """\
No model is configured and none could be detected.

Pick one of:
  - Claude Code:  install the `claude` CLI and sign in, then use  model="claude-code"
  - opencode:     install the `opencode` CLI and sign in, then use  model="opencode"
  - Anthropic:    pip install "rlm-ig[anthropic]", set ANTHROPIC_API_KEY, use  model="anthropic:claude-opus-5-5"
  - OpenAI-style: set OPENAI_API_KEY (or OPENROUTER_API_KEY, ...), use  model="openai:<model-id>"

Set it per call (model=...), in rlm.toml (model = "..."), or with $RLM_MODEL. Run `rlm doctor` to see what is available.
"""

ANTHROPIC_SDK_MISSING = """
ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is set, but the Anthropic SDK is not installed, so the anthropic
backend was not picked. Install it with:  pip install "rlm-ig[anthropic]"
"""

__all__ = [
    "Chat", "Model", "Reply", "Scripted", "StatelessChat", "Turn",
    "detect", "no_backend_message", "providers", "resolve", "split_spec",
]  # fmt: skip
