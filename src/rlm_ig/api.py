"""The public entry points: :func:`run`, :func:`arun` and :class:`Result`."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .budget import Usage
from .config import Config
from .engine.agent import Agent
from .engine.events import Event, Events
from .engine.run import Run
from .errors import ConfigError, RlmError, RunFailed
from .models import NO_BACKEND, Model, detect, resolve
from .sandbox import Sandbox
from .schema import to_schema
from .text import clip_middle, first_line
from .tools import Toolbox
from .trace import TraceWriter, new_trace_path
from .trace.console import Console


@dataclass
class Result:
    """The outcome of a run.

    ``status`` is ``"ok"`` when the agent submitted an answer normally,
    ``"partial"`` when the answer came from a forced wrap-up (out of turns or
    budget; see ``note``), and ``"failed"`` when there is no answer (see
    ``error``).
    """

    answer: Any = None
    status: str = "ok"
    note: str | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    steps: int = 0
    agents: int = 0
    seconds: float = 0.0
    trace: str | None = None
    model: str = ""
    sub_model: str = ""
    #: the answer as an instance of the Pydantic model or dataclass given as ``schema``
    parsed: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def raise_for_status(self) -> Result:
        """Raise :class:`RunFailed` unless the run finished cleanly."""
        if self.status == "failed":
            raise RunFailed(self.error or "the run failed")
        if self.status == "partial":
            raise RunFailed(f"the run ended early: {self.note}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "status": self.status,
            "note": self.note,
            "error": self.error,
            "usage": self.usage.to_dict(),
            "usage_by_model": {k: v.to_dict() for k, v in self.usage_by_model.items()},
            "steps": self.steps,
            "agents": self.agents,
            "seconds": round(self.seconds, 3),
            "trace": self.trace,
            "model": self.model,
            "sub_model": self.sub_model,
        }

    def __str__(self) -> str:
        return self.answer if isinstance(self.answer, str) else repr(self.answer)


EventHandler = Callable[[Event], None]


class Engine:
    """Resolved models and tools, reusable across runs. Internal."""

    def __init__(self, config: Config, tools: Iterable[Any] | None = None):
        self.config = config
        spec = config.model or detect()
        if spec is None:
            raise ConfigError(NO_BACKEND)
        self.model: Model = resolve(spec, config)
        if config.sub_model is None or config.sub_model == spec:
            self.sub_model: Model = self.model
        else:
            self.sub_model = resolve(config.sub_model, config)
        self.toolbox = Toolbox(tools or ())
        self._mcp = None
        self._closed = False

    async def _connect_mcp(self) -> None:
        if self._mcp is not None or not self.config.mcp:
            return
        from .tools.mcp import connect  # deferred: most runs use no MCP server

        self._mcp = await connect(self.config.mcp)
        for mcp_tool in self._mcp.tools:
            self.toolbox.add(mcp_tool)

    async def solve(
        self,
        task: str,
        context: Any = None,
        *,
        schema: Any = None,
        on_event: EventHandler | None = None,
        sandbox: Sandbox | None = None,
        notes: str = "",
    ) -> Result:
        if not isinstance(task, str) or not task.strip():
            raise ConfigError("task must be a non-empty string")
        cfg = self.config
        json_schema = to_schema(schema)
        await self._connect_mcp()

        sinks: list[EventHandler] = []
        if cfg.verbosity > 0:
            sinks.append(Console(cfg.verbosity))
        writer: TraceWriter | None = None
        if cfg.trace:
            writer = TraceWriter(new_trace_path(cfg.trace_dir, task))
            sinks.append(writer)
        if on_event is not None:
            sinks.append(on_event)
        events = Events(sinks)
        run = Run(cfg, self.model, self.sub_model, toolbox=self.toolbox, events=events)
        root = Agent(run, task=task, context=context, schema=json_schema, sandbox=sandbox, notes=notes)

        result = Result(model=self.model.spec, sub_model=self.sub_model.spec)
        started = time.monotonic()
        events.emit(
            "run.start",
            task=task,
            model=self.model.spec,
            sub_model=self.sub_model.spec,
            summary=_context_line(context, sandbox),
            limits={
                "max_depth": cfg.max_depth,
                "max_steps": cfg.max_steps,
                "max_calls": cfg.max_calls,
                "max_usd": cfg.max_usd,
                "max_tokens": cfg.max_tokens,
                "timeout": cfg.timeout,
                "parallel": run.parallel,
            },
            tools=self.toolbox.names(),
        )
        try:
            hard_stop = None if cfg.timeout is None else cfg.timeout * 1.5 + 30
            result.answer = await asyncio.wait_for(root.execute(), hard_stop)
            if root.forced:
                result.status, result.note = "partial", f"answer produced by a forced wrap-up ({root.forced})"
            result.parsed = _parse(schema, result.answer)
        except TimeoutError:
            result.status, result.error = "failed", f"run exceeded its time limit of {cfg.timeout:g}s"
        except RlmError as exc:
            result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"
        except BaseException as exc:
            result.status, result.error = "failed", f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            raise
        finally:
            run.cleanup()
            result.usage = run.ledger.usage
            result.usage_by_model = dict(run.ledger.by_model)
            result.steps, result.agents = run.steps, run.agents
            result.seconds = time.monotonic() - started
            if writer is not None and writer.written:
                result.trace = str(writer.path)
            events.emit(
                "run.end",
                status=result.status,
                answer=clip_middle(str(result), 4000) if result.status != "failed" else None,
                error=result.error,
                note=result.note,
                usage=result.usage.to_dict(),
                usage_by_model={k: v.to_dict() for k, v in result.usage_by_model.items()},
                usage_text=str(result.usage),
                seconds=round(result.seconds, 3),
                steps=result.steps,
                agents=result.agents,
                trace=result.trace,
            )
            if writer is not None:
                writer.close()
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in {id(m): m for m in (self.model, self.sub_model)}.values():
            try:
                await closer.close()
            except Exception:  # noqa: BLE001 - shutting down
                pass
        if self._mcp is not None:
            try:
                await self._mcp.close()
            except Exception:  # noqa: BLE001
                pass


def _context_line(context: Any, sandbox: Sandbox | None) -> str:
    if sandbox is not None and sandbox.summary:
        return first_line(sandbox.summary, 100)
    if context is None:
        return "no context"
    if isinstance(context, str):
        return f"context: str, {len(context):,} chars"
    try:
        return f"context: {type(context).__name__}, {len(context):,} items"
    except TypeError:
        return f"context: {type(context).__name__}"


def _parse(schema: Any, answer: Any) -> Any:
    """Build an instance of the caller's schema class from a validated answer."""
    if schema is None or isinstance(schema, dict) or not isinstance(schema, type):
        return None
    try:
        if hasattr(schema, "model_validate"):
            return schema.model_validate(answer)
        if dataclasses.is_dataclass(schema) and isinstance(answer, dict):
            return schema(**answer)
    except Exception:  # noqa: BLE001 - the raw answer is still available
        return None
    return None


def build_config(config: Config | None, options: dict[str, Any]) -> Config:
    """``config`` (or the loaded defaults) with keyword overrides applied."""
    if config is None:
        return Config.load(**options)
    return config.replace(**options) if options else config


async def arun(
    task: str,
    context: Any = None,
    *,
    schema: Any = None,
    tools: Iterable[Any] | None = None,
    on_event: EventHandler | None = None,
    config: Config | None = None,
    **options: Any,
) -> Result:
    """Solve ``task`` over ``context`` and return a :class:`Result`.

    ``context`` is the data the model explores through its REPL: a string of
    any length, or a dict/list/any picklable object. ``schema`` constrains the
    answer (JSON Schema dict, type hint, dataclass or Pydantic model).
    ``tools`` are Python callables exposed in the REPL. Remaining keyword
    arguments override :class:`Config` fields, for example ``model=``,
    ``sub_model=``, ``max_usd=``, ``max_depth=``, ``verbosity=``.
    """
    engine = Engine(build_config(config, options), tools)
    try:
        return await engine.solve(task, context, schema=schema, on_event=on_event)
    finally:
        await engine.close()


def run(
    task: str,
    context: Any = None,
    *,
    schema: Any = None,
    tools: Iterable[Any] | None = None,
    on_event: EventHandler | None = None,
    config: Config | None = None,
    **options: Any,
) -> Result:
    """Blocking version of :func:`arun`. Use ``arun`` inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RlmError("run() cannot be called from a running event loop; use `await arun(...)` instead")
    return asyncio.run(arun(task, context, schema=schema, tools=tools, on_event=on_event, config=config, **options))
