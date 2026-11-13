"""The agent loop: one model conversation driving one REPL."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..errors import BudgetExceeded, DelegationRefused, RlmError, StepsExhausted, ToolError
from ..models.base import Chat, Model
from ..sandbox import Sandbox, encode
from ..schema import validate
from ..text import clip_middle, extract_code, first_line
from ..tools import Toolbox
from . import prompts
from .run import Run

# A parent must hold at least this much context before the pass-through guard
# cares; forwarding a small context whole is harmless.
GUARD_MIN_CHARS = 20_000
GUARD_RATIO = 0.8


class Agent:
    """Solves one task over one context, possibly spawning sub-agents."""

    def __init__(
        self,
        run: Run,
        *,
        task: str,
        context: Any = None,
        depth: int = 0,
        agent_id: str = "0",
        parent: str | None = None,
        schema: dict | None = None,
        toolbox: Toolbox | None = None,
        helpers: list[str] | None = None,
        sandbox: Sandbox | None = None,
        notes: str = "",
    ):
        self.run = run
        self.task = task
        self.context = context
        self.depth = depth
        self.id = agent_id
        self.parent = parent
        self.schema = schema
        self.toolbox = toolbox if toolbox is not None else run.toolbox
        self.helpers = list(helpers or [])
        self.sandbox = sandbox
        self.notes = notes
        self.model: Model = run.model if depth == 0 else run.sub_model
        #: True when the answer came from a forced wrap-up turn
        self.forced: str | None = None
        self.steps = 0
        self._children = 0
        self._holds_slot = False
        self._graced = False

    # ------------------------------------------------------------------ run

    async def execute(self) -> Any:
        run, events = self.run, self.run.events
        run.agents += 1
        owns_sandbox = self.sandbox is None
        chat: Chat | None = None
        try:
            if owns_sandbox:
                self.sandbox = run.new_sandbox(self.id)
                await self.sandbox.start(self.context, tools=self.toolbox.meta(), helpers=self.helpers)
            assert self.sandbox is not None
            events.emit(
                "agent.start",
                self.id,
                self.depth,
                task=self.task,
                parent=self.parent,
                context_size=self.sandbox.context_size,
                summary=first_line(self.sandbox.summary, 160),
            )
            chat = self.model.chat(prompts.SYSTEM)
            value = await self._loop(chat)
            events.emit(
                "agent.end",
                self.id,
                self.depth,
                status="partial" if self.forced else "ok",
                steps=self.steps,
                answer=clip_middle(_show(value), 2000),
            )
            return value
        except asyncio.CancelledError:
            events.emit("agent.end", self.id, self.depth, status="cancelled", steps=self.steps, error="cancelled")
            raise
        except Exception as exc:
            events.emit(
                "agent.end",
                self.id,
                self.depth,
                status="failed",
                steps=self.steps,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if chat is not None:
                await _quietly(chat.close())
            if owns_sandbox and self.sandbox is not None:
                await _quietly(self.sandbox.close())

    async def _loop(self, chat: Chat) -> Any:
        cfg = self.run.config
        assert self.sandbox is not None
        message = prompts.opening(
            task=self.task,
            summary=self.sandbox.summary,
            tools=self.toolbox.catalog(),
            schema=self.schema,
            steps=cfg.max_steps,
            output_limit=cfg.output_limit,
            depth=self.depth,
            max_depth=cfg.max_depth,
            ask_chars=self.run.ask_limit,
            notes=self.notes,
            problems=self.sandbox.problems,
        )
        prose_replies = 0
        last_prose = ""
        final = False
        step = 0
        while True:
            step += 1
            if step > cfg.max_steps and not final:
                final = True
                self.forced = "out of turns"
                message += prompts.LAST_CALL
            try:
                reply = await self.run.turn(chat, self.model, message, agent=self.id, depth=self.depth, step=step)
            except BudgetExceeded as exc:
                # Only the root gets to wrap up on a spent budget; a sub-agent
                # fails upward so its parent can decide what to do.
                if self.depth != 0 or self._graced:
                    raise
                self._graced = True
                final = True
                self.forced = f"budget spent: {exc}"
                self.run.ledger.allow_grace(1)
                message += prompts.BUDGET_SPENT.format(reason=exc)
                reply = await self.run.turn(chat, self.model, message, agent=self.id, depth=self.depth, step=step)
            self.steps = step

            code = extract_code(reply.text)
            if code is None:
                prose_replies += 1
                last_prose = reply.text.strip() or last_prose
                # A model that keeps answering in prose has answered. Take it,
                # unless a schema says the answer must be structured.
                if (prose_replies >= 2 or final) and self.schema is None and last_prose:
                    self.run.events.emit("note", self.id, self.depth, message="accepted a prose reply as the answer")
                    return last_prose
                if final:
                    raise StepsExhausted(f"agent {self.id} did not submit an answer ({self.forced})")
                message = prompts.no_code(step, cfg.max_steps)
                continue
            prose_replies = 0

            self.run.events.emit("cell.start", self.id, self.depth, step=step, code=code)
            result = await self.sandbox.run(code, self._on_call)
            shown = clip_middle(result.text, cfg.output_limit)
            self.run.events.emit(
                "cell",
                self.id,
                self.depth,
                step=step,
                code=code,
                output=shown,
                error=bool(result.error),
                seconds=round(result.seconds, 3),
                submitted=result.submitted,
            )
            if result.submitted:
                problems = validate(result.value, self.schema)
                if not problems:
                    return result.value
                if final:
                    raise StepsExhausted(f"agent {self.id}: final answer did not match the schema ({problems[0]})")
                message = prompts.schema_feedback(problems, self.schema or {}, step=step, steps=cfg.max_steps)
                continue
            if final:
                raise StepsExhausted(f"agent {self.id} did not submit an answer ({self.forced})")
            message = prompts.observation(
                result.text, step=step, steps=cfg.max_steps, seconds=result.seconds, limit=cfg.output_limit
            )

    # ------------------------------------------------------ calls from the REPL

    async def _on_call(self, kind: str, msg: dict) -> dict:
        events = self.run.events
        call = self.run.next_call_id()
        info = _call_info(kind, msg)
        events.emit("call.start", self.id, self.depth, call=call, op=kind, **info)
        started = time.monotonic()
        failed: dict[str, str] = {}
        try:
            if kind == "ask":
                value = await self.run.ask(msg.get("prompt", ""), msg.get("data"), msg.get("schema"))
            elif kind == "ask_map":
                value, failed = await self._ask_map(msg.get("jobs") or [], msg.get("schema"))
            elif kind == "delegate":
                value = await self._delegate(msg)
            elif kind == "delegate_map":
                value, failed = await self._delegate_map(msg)
            elif kind == "tool":
                value = await self.toolbox.call(msg.get("name", ""), msg.get("args") or [], msg.get("kwargs") or {})
            else:
                raise RlmError(f"unknown call kind {kind!r}")
            reply: dict = {"ok": True, "value": encode(value)}
            if failed:
                reply["failed"] = failed
            events.emit(
                "call.end", self.id, self.depth, call=call, op=kind, ok=True,
                seconds=round(time.monotonic() - started, 3), failed=len(failed), **info,
            )  # fmt: skip
            return reply
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            etype = type(exc).__name__ if isinstance(exc, _PASSED_THROUGH) else "SubcallError"
            text = str(exc) if isinstance(exc, _PASSED_THROUGH) else f"{type(exc).__name__}: {exc}"
            events.emit(
                "call.end", self.id, self.depth, call=call, op=kind, ok=False,
                seconds=round(time.monotonic() - started, 3), error=text, failed=0, **info,
            )  # fmt: skip
            return {"ok": False, "etype": etype, "error": text}

    async def _ask_map(self, jobs: list[dict], schema: dict | None) -> tuple[list, dict[str, str]]:
        results: list[Any] = [None] * len(jobs)

        async def serve(indices: list[int]) -> None:
            if len(indices) > 1:
                try:
                    values = await self.run.ask_batch(
                        jobs[indices[0]]["prompt"], [jobs[i]["data"] for i in indices], schema
                    )
                except BudgetExceeded as exc:
                    for i in indices:
                        results[i] = exc
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # The batched call was unusable: ask for each item separately.
                    self.run.events.emit(
                        "note",
                        self.id,
                        self.depth,
                        message=f"ask_map: a batch of {len(indices)} items fell back to single calls ({exc})",
                    )
                else:
                    for i, value in zip(indices, values, strict=True):
                        results[i] = value
                    return
            answers = await asyncio.gather(
                *(self.run.ask(jobs[i].get("prompt", ""), jobs[i].get("data"), schema) for i in indices),
                return_exceptions=True,
            )
            for i, answer in zip(indices, answers, strict=True):
                results[i] = answer

        await asyncio.gather(*(serve(group) for group in self._batches(jobs)))
        return self._collect(results)

    def _batches(self, jobs: list[dict]) -> list[list[int]]:
        """Group ``ask_map`` jobs so that small items share a sub-model call.

        Only the ``ask_map(prompt, items)`` form is grouped (one prompt, every
        job carrying data). An item that is large on its own keeps a call to
        itself, so batching never dilutes attention on substantial inputs.
        """
        cfg = self.run.config
        prompt = jobs[0].get("prompt") if jobs else None
        if cfg.ask_batch <= 1 or len(jobs) < 2 or any(j.get("data") is None or j.get("prompt") != prompt for j in jobs):
            return [[i] for i in range(len(jobs))]
        budget = min(cfg.ask_batch_chars, max(1, self.run.ask_limit - len(prompt or "") - 2000))
        groups: list[list[int]] = []
        current: list[int] = []
        used = 0
        for i, job in enumerate(jobs):
            size = len(job["data"]) + 40
            if size > budget // 2:
                if current:
                    groups.append(current)
                    current, used = [], 0
                groups.append([i])
                continue
            if current and (len(current) >= cfg.ask_batch or used + size > budget):
                groups.append(current)
                current, used = [], 0
            current.append(i)
            used += size
        if current:
            groups.append(current)
        return groups

    def _collect(self, results: list) -> tuple[list, dict[str, str]]:
        values: list = []
        failed: dict[str, str] = {}
        for i, item in enumerate(results):
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, BaseException):
                failed[str(i)] = f"{type(item).__name__}: {item}"
                values.append(None)
            else:
                values.append(item)
        if failed and len(failed) == len(results):
            # Nothing worked: raise instead of returning a list of failures, so
            # a spent budget or a dead backend is impossible to overlook.
            first = next(r for r in results if isinstance(r, BaseException))
            raise first
        return values, failed

    def _child_tools(self, names: list[str] | None) -> Toolbox:
        if names is None:
            return self.toolbox if self.run.config.inherit_tools else Toolbox()
        return self.toolbox.subset(names)

    def _check_depth(self) -> None:
        if self.depth >= self.run.config.max_depth:
            raise DelegationRefused(
                "sub-agents are not available at this level. Do the work here with code, ask() and ask_map()."
            )

    async def _delegate(self, msg: dict) -> Any:
        self._check_depth()
        data = msg.get("data")
        assert self.sandbox is not None
        own = self.sandbox.context_size
        if self.run.config.passthrough_guard and not msg.get("whole") and own >= GUARD_MIN_CHARS:
            size = _size(data)
            if size >= GUARD_RATIO * own:
                raise DelegationRefused(
                    f"this would hand a sub-agent {size:,} characters, {round(100 * size / own)}% of your own context, "
                    "which only moves the problem down a level. Narrow it first (filter, slice, or chunk and use "
                    "delegate_map / ask_map). If one pass over everything really is what you want, pass whole=True."
                )
        tools = self._child_tools(msg.get("tools"))
        results = await self._spawn([(msg.get("task", ""), data)], msg.get("schema"), tools, msg.get("helpers") or [])
        if isinstance(results[0], BaseException):
            raise results[0]
        return results[0]

    async def _delegate_map(self, msg: dict) -> tuple[list, dict[str, str]]:
        self._check_depth()
        tools = self._child_tools(msg.get("tools"))
        task = msg.get("task", "")
        jobs = [(task, item) for item in msg.get("items") or []]
        results = await self._spawn(jobs, msg.get("schema"), tools, msg.get("helpers") or [])
        return self._collect(results)

    async def _spawn(
        self, jobs: list[tuple[str, Any]], schema: dict | None, tools: Toolbox, helpers: list[str]
    ) -> list:
        """Run child agents concurrently; exceptions come back as list items."""

        async def one(task: str, data: Any) -> Any:
            self._children += 1
            child = Agent(
                self.run,
                task=task,
                context=data,
                depth=self.depth + 1,
                agent_id=f"{self.id}.{self._children}",
                parent=self.id,
                schema=schema,
                toolbox=tools,
                helpers=helpers,
            )
            async with self.run.agent_slots:
                child._holds_slot = True
                try:
                    return await child.execute()
                finally:
                    child._holds_slot = False

        # While this agent only waits, its slot is better used by a child.
        held = self._holds_slot
        if held:
            self.run.agent_slots.release()
            self._holds_slot = False
        try:
            return await asyncio.gather(*(one(task, data) for task, data in jobs), return_exceptions=True)
        finally:
            if held:
                await self.run.agent_slots.acquire()
                self._holds_slot = True


_PASSED_THROUGH = (BudgetExceeded, DelegationRefused, ToolError, ValueError, TypeError)


def _size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(repr(value))


def _show(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _call_info(kind: str, msg: dict) -> dict:
    if kind == "ask":
        return {"n": 1, "chars": len(msg.get("prompt") or "") + len(msg.get("data") or ""), "name": ""}
    if kind == "ask_map":
        jobs = msg.get("jobs") or []
        return {
            "n": len(jobs),
            "chars": sum(len(j.get("prompt") or "") + len(j.get("data") or "") for j in jobs),
            "name": "",
        }
    if kind == "delegate":
        return {"n": 1, "chars": _size(msg.get("data")), "name": first_line(msg.get("task") or "", 80)}
    if kind == "delegate_map":
        items = msg.get("items") or []
        return {"n": len(items), "chars": sum(_size(i) for i in items), "name": first_line(msg.get("task") or "", 80)}
    return {"n": 1, "chars": 0, "name": msg.get("name") or ""}


async def _quietly(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
        pass
