"""Exception types raised by rlm-ig."""

from __future__ import annotations


class RlmError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(RlmError):
    """The configuration is invalid or incomplete."""


class ModelError(RlmError):
    """A model backend failed: missing binary, bad credentials, API error."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BudgetExceeded(RlmError):
    """A run-wide limit (calls, cost, tokens, or time) was reached."""


class StepsExhausted(RlmError):
    """An agent used all of its steps without submitting an answer."""


class DelegationRefused(RlmError):
    """A sub-agent was not started (depth limit or pass-through guard)."""


class SandboxError(RlmError):
    """The REPL worker could not be started or stopped responding."""


class SchemaError(RlmError):
    """A schema is not usable, or a value does not satisfy it."""


class ToolError(RlmError):
    """A host tool raised while being called from the REPL."""


class RunFailed(RlmError):
    """Raised by ``Result.raise_for_status`` when a run did not finish cleanly."""
