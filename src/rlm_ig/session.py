"""Sessions: several tasks sharing one live REPL.

Within a session the root agent's sandbox is never torn down, so whatever one
task built (parsed data, indexes, helper functions) is simply still there for
the next. Each task starts a fresh model conversation, seeded with a short
record of the earlier tasks and an inventory of the variables that exist, so
the prompt stays small no matter how long the session runs.

Give the session a ``path`` and the namespace is also written to disk after
every task and restored the next time the same path is opened.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .api import Engine, EventHandler, Result, _answer_text, build_config
from .config import Config
from .errors import RlmError
from .sandbox import Sandbox, SandboxOptions
from .text import clip_middle, first_line

STATE_VERSION = 1
HISTORY_SHOWN = 8


class Session:
    """A sequence of related runs over one persistent REPL.

    Use it as a context manager, sync or async::

        with rlm.Session(model="claude-code") as session:
            session.run("Parse this export and index it by customer.", context=export)
            answer = session.run("Which customers have more than three open tickets?")
    """

    def __init__(
        self,
        *,
        config: Config | None = None,
        tools: Iterable[Any] | None = None,
        path: str | os.PathLike | None = None,
        max_snapshot_bytes: int = 50_000_000,
        **options: Any,
    ):
        self.config = build_config(config, options)
        self.path = Path(path).expanduser() if path else None
        self.max_snapshot_bytes = max_snapshot_bytes
        #: completed tasks, oldest first: ``{"task": ..., "answer": ..., "status": ...}``
        self.history: list[dict[str, Any]] = []
        #: variables that could not be saved or restored, with the reason
        self.skipped: dict[str, str] = {}
        self._tools = list(tools or ())
        self._engine: Engine | None = None
        self._sandbox: Sandbox | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None

    # ------------------------------------------------------------ lifecycle

    async def _ensure(self) -> Sandbox:
        if self._engine is None:
            self._engine = Engine(self.config, self._tools)
        if self._sandbox is None:
            await self._engine._connect_mcp()
            base = self.config.sandbox
            sandbox = Sandbox(SandboxOptions(**{**base.__dict__}), name="session")
            await sandbox.start(None, tools=self._engine.toolbox.meta())
            self._sandbox = sandbox
            if self.path and self.path.is_file():
                await self._load(sandbox)
        return self._sandbox

    async def _load(self, sandbox: Sandbox) -> None:
        assert self.path is not None
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RlmError(f"could not read the session file {self.path}: {exc}") from exc
        if saved.get("version") != STATE_VERSION:
            raise RlmError(f"{self.path} was written by an incompatible version (state version {saved.get('version')})")
        self.history = list(saved.get("history") or [])
        state = saved.get("state") or {}
        failed = await sandbox.restore(state)
        self.skipped = {**(state.get("skipped") or {}), **failed}
        await sandbox.describe()

    async def asave(self) -> None:
        """Write the namespace and history to ``path`` (atomic replace)."""
        if self.path is None or self._sandbox is None:
            return
        state = await self._sandbox.snapshot(self.max_snapshot_bytes)
        self.skipped = dict(state.get("skipped") or {})
        payload = {"version": STATE_VERSION, "history": self.history, "state": state}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, self.path)

    async def aclose(self) -> None:
        sandbox, self._sandbox = self._sandbox, None
        if sandbox is not None:
            await sandbox.close()
        engine, self._engine = self._engine, None
        if engine is not None:
            await engine.close()

    # ------------------------------------------------------------------ runs

    async def arun(
        self,
        task: str,
        context: Any = None,
        *,
        schema: Any = None,
        on_event: EventHandler | None = None,
    ) -> Result:
        """Run one task. ``context`` replaces the REPL's ``context`` when given."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:  # one task at a time per REPL
            sandbox = await self._ensure()
            assert self._engine is not None
            if context is not None:
                await sandbox.set_context(context)
            notes = await self._notes(sandbox)
            result = await self._engine.solve(
                task, context, schema=schema, on_event=on_event, sandbox=sandbox, notes=notes
            )
            self.history.append(
                {
                    "task": task,
                    "status": result.status,
                    "answer": clip_middle(_answer_text(result.answer), 2000) if result.status != "failed" else None,
                }
            )
            if self.path is not None:
                await self.asave()
            return result

    async def _notes(self, sandbox: Sandbox) -> str:
        rows = await sandbox.inventory()
        if not self.history and not rows:
            return ""
        lines = ["This REPL has already been used in this session. Its variables are still defined; reuse them."]
        if self.history:
            lines.append("")
            lines.append("Earlier tasks and their answers:")
            start = max(0, len(self.history) - HISTORY_SHOWN)
            if start:
                lines.append(f"  ({start} older task(s) not shown)")
            for number, item in enumerate(self.history[start:], start + 1):
                answer = first_line(item.get("answer") or f"({item.get('status')})", 300)
                lines.append(f"  {number}. {first_line(item['task'], 200)}")
                lines.append(f"     -> {answer}")
        if rows:
            lines.append("")
            lines.append("Variables and functions defined so far:")
            for row in rows[:60]:
                lines.append(f"  {row['name']} ({row['type']}): {row['preview']}")
            if len(rows) > 60:
                lines.append(f"  ... and {len(rows) - 60} more")
        if self.skipped:
            lines.append("")
            lines.append("Not carried over from the saved session: " + ", ".join(sorted(self.skipped)))
        return "\n".join(lines)

    async def avariables(self) -> list[dict[str, str]]:
        """``[{"name", "type", "preview"}]`` for everything the REPL holds."""
        return await (await self._ensure()).inventory()

    # ------------------------------------------------------ async and sync use

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _sync(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise RlmError("this Session method blocks; inside an event loop use the a-prefixed coroutine instead")
        if self._loop is None:
            # A private loop that lives as long as the session: the REPL worker
            # and backend processes are bound to it.
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def run(
        self, task: str, context: Any = None, *, schema: Any = None, on_event: EventHandler | None = None
    ) -> Result:
        return self._sync(self.arun(task, context, schema=schema, on_event=on_event))

    def variables(self) -> list[dict[str, str]]:
        return self._sync(self.avariables())

    def save(self) -> None:
        self._sync(self.asave())

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            self._loop.run_until_complete(self.aclose())
        finally:
            self._loop.close()
            self._loop = None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
