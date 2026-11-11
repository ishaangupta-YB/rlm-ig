"""Host tools: ordinary Python callables the model can call from its REPL.

A tool runs in the *host* process, not in the sandbox. The worker only holds
a stub with the same name, signature and docstring; calling the stub sends
the arguments to the host and blocks for the result. That is why a tool may
use any library, hold open connections, or read credentials the sandbox
never sees.
"""

from __future__ import annotations

import asyncio
import inspect
import keyword
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigError, ToolError


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    signature: str
    doc: str
    is_async: bool = False

    @classmethod
    def wrap(cls, fn: Callable[..., Any], *, name: str | None = None, doc: str | None = None) -> Tool:
        if isinstance(fn, Tool):
            return fn
        if not callable(fn):
            raise ConfigError(f"a tool must be callable, got {type(fn).__name__}")
        tool_name = name or getattr(fn, "__rlm_tool_name__", None) or getattr(fn, "__name__", None)
        if not tool_name or not tool_name.isidentifier() or keyword.iskeyword(tool_name):
            raise ConfigError(f"tool name {tool_name!r} is not a valid Python identifier; pass name=...")
        signature = _signature_text(fn)
        text = doc or getattr(fn, "__rlm_tool_doc__", None) or inspect.getdoc(fn) or ""
        is_async = inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(type(fn).__call__)
        return cls(tool_name, fn, signature, text.strip(), is_async)

    def render(self) -> str:
        """How the tool is shown to the model: a stub, never the body."""
        lines = [f"def {self.name}{self.signature}"]
        for line in self.doc.splitlines() or ["(no description)"]:
            lines.append(f"    {line}".rstrip())
        return "\n".join(lines)


def _signature_text(fn: Callable[..., Any]) -> str:
    """The signature as the model should read it, with annotations unquoted."""
    try:
        return str(inspect.signature(fn, eval_str=True))
    except Exception:  # noqa: BLE001 - unresolvable forward references, builtins, ...
        pass
    try:
        text = str(inspect.signature(fn))
    except (TypeError, ValueError):
        return "(*args, **kwargs)"
    # Postponed annotations show up as quoted strings; the quotes are noise.
    return re.sub(r"(:|->) '([^']*)'", r"\1 \2", text)


def tool(fn: Callable[..., Any] | None = None, *, name: str | None = None, doc: str | None = None):
    """Optional decorator to rename a tool or override its description.

    Plain functions work as tools without it::

        @tool(name="search", doc="Search the knowledge base. Returns a list of hits.")
        def kb_search(query: str, k: int = 5) -> list[dict]: ...
    """

    def mark(f: Callable[..., Any]) -> Callable[..., Any]:
        if name:
            f.__rlm_tool_name__ = name  # type: ignore[attr-defined]
        if doc:
            f.__rlm_tool_doc__ = doc  # type: ignore[attr-defined]
        return f

    return mark(fn) if fn is not None else mark


class Toolbox:
    """An ordered set of tools with unique names."""

    def __init__(self, tools: Iterable[Any] = ()):
        self._tools: dict[str, Tool] = {}
        for item in tools or ():
            self.add(item)

    def add(self, item: Any) -> Tool:
        wrapped = Tool.wrap(item)
        if wrapped.name in RESERVED:
            raise ConfigError(f"tool name {wrapped.name!r} collides with a built-in REPL name")
        if wrapped.name in self._tools and self._tools[wrapped.name].fn is not wrapped.fn:
            raise ConfigError(f"two tools are both named {wrapped.name!r}")
        self._tools[wrapped.name] = wrapped
        return wrapped

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: Iterable[str]) -> Toolbox:
        """A toolbox restricted to ``names``; unknown names raise ``ToolError``."""
        picked = Toolbox()
        for name in names:
            if name not in self._tools:
                raise ToolError(f"unknown tool {name!r}; available: {', '.join(self._tools) or 'none'}")
            picked._tools[name] = self._tools[name]
        return picked

    def meta(self) -> list[dict]:
        return [{"name": t.name, "signature": t.signature, "doc": t.doc} for t in self._tools.values()]

    def catalog(self, *, compact_after: int = 12) -> str:
        """Text for the task message. Large toolboxes list one line per tool."""
        tools = list(self._tools.values())
        if not tools:
            return ""
        if len(tools) <= compact_after:
            return "\n\n".join(t.render() for t in tools)
        lines = [f"{t.name}{t.signature}  -  {(t.doc.splitlines() or [''])[0]}" for t in tools]
        lines.append("(call help(name) in the REPL for a tool's full description)")
        return "\n".join(lines)

    async def call(self, name: str, args: list, kwargs: dict) -> Any:
        if name not in self._tools:
            raise ToolError(f"unknown tool {name!r}")
        target = self._tools[name]
        try:
            if target.is_async:
                return await target.fn(*args, **kwargs)
            result = await asyncio.to_thread(target.fn, *args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{name} failed: {type(exc).__name__}: {exc}") from exc


RESERVED = frozenset(
    "context ask ask_map delegate delegate_map submit peek chunk grep Failed "
    "SubcallError BudgetExceeded DelegationRefused ToolError".split()
)


def safe_name(text: str) -> str:
    """Turn arbitrary text into a usable Python identifier."""
    name = re.sub(r"\W+", "_", text).strip("_") or "tool"
    if name[0].isdigit() or keyword.iskeyword(name):
        name = "_" + name
    return name


__all__ = ["Tool", "Toolbox", "tool", "safe_name", "RESERVED"]
