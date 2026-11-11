"""rlm-ig: recursive language models over a real Python REPL.

import rlm_ig as rlm

result = rlm.run("Which customers churned twice?", context=open("events.log").read())
print(result.answer)
"""

from .api import Result, arun, run
from .budget import Usage
from .config import Config
from .engine.events import Event
from .errors import (
    BudgetExceeded,
    ConfigError,
    ModelError,
    RlmError,
    RunFailed,
    SandboxError,
    SchemaError,
    StepsExhausted,
    ToolError,
)
from .models import Model, Scripted
from .sandbox import SandboxOptions
from .tools import tool

__version__ = "0.1.0"

__all__ = [
    "run", "arun", "Result", "Config", "SandboxOptions", "Usage", "Event", "tool",
    "Model", "Scripted",
    "RlmError", "ConfigError", "ModelError", "BudgetExceeded", "StepsExhausted",
    "SandboxError", "SchemaError", "ToolError", "RunFailed",
    "__version__",
]  # fmt: skip
