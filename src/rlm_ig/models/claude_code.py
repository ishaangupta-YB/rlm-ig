"""Claude Code as a model backend.

Each conversation is one long-lived ``claude -p`` process speaking
stream-json in both directions. The process keeps the conversation, so a turn
sends only the new message, every turn after the first hits the prompt cache,
and the CLI's start-up cost is paid once per agent instead of once per step.

Claude Code is used purely as a text model here: its own tools are switched
off (``--tools ""``), its system prompt is replaced, and no MCP servers or
slash commands are loaded. All computation happens in the rlm-ig sandbox,
where it is visible in the trace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from typing import Any

from .. import procs
from ..budget import Usage
from ..errors import ModelError
from ..text import approx_tokens, looks_runaway
from .base import Chat, Model, Reply, TextSink, replay_message

# Set by a parent Claude Code session. Inherited by a child, it makes the
# child produce a status summary after every turn: an extra model call and a
# few seconds of latency per step that an rlm run has no use for.
_DROP_ENV = ("CLAUDE_CODE_ENTRYPOINT",)

_STREAM_LIMIT = 256 * 1024 * 1024  # one event is one line; replies can be long

# Set RLM_DEBUG_DIR to a directory to keep the raw event stream of every CLI process.
_DEBUG_DIR = os.environ.get("RLM_DEBUG_DIR")


class ClaudeCode(Model):
    parallel = 4
    window_chars = 400_000
    reports_cost = True

    def __init__(
        self,
        provider: str = "claude-code",
        model: str = "",
        settings: dict[str, Any] | None = None,
        pricing: dict | None = None,
    ):
        settings = dict(settings or {})
        self.model_name = model or settings.get("model") or ""
        self.spec = provider + (f":{self.model_name}" if self.model_name else "")
        self.command = settings.get("command", "claude")
        #: which Claude Code settings files apply: "user" keeps your login and
        #: provider configuration; "" loads none (API-key setups only)
        self.setting_sources = settings.get("setting_sources", "user")
        self.effort = settings.get("effort")
        self.extra_args = [str(a) for a in settings.get("args", [])]
        self.extra_env = {str(k): str(v) for k, v in (settings.get("env") or {}).items()}
        #: remove ANTHROPIC_API_KEY from the CLI's environment so a
        #: subscription login is used even when a key is exported
        self.strip_api_key = bool(settings.get("strip_api_key", False))
        #: longest a whole turn may take
        self.turn_timeout = float(settings.get("timeout", 900))
        #: seconds with nothing but keep-alive pings before the session is
        #: treated as stalled and rebuilt (the CLI's own retry waits far longer)
        self.stall_timeout = float(settings.get("stall_timeout", 90))
        #: how many times a lost session is rebuilt from the transcript
        self.retries = int(settings.get("retries", 2))
        #: backstop on a single reply; the stream guard usually acts first
        self.max_output_tokens = int(settings.get("max_output_tokens", 32000))
        self.max_reply_chars = int(settings.get("max_reply_chars", 80_000))
        self.window_chars = int(settings.get("window_chars", self.window_chars))
        self.parallel = int(settings.get("parallel", self.parallel))
        #: filled from the CLI's init event: served model, auth source, version
        self.info: dict[str, Any] = {}

    def chat(self, system: str) -> Chat:
        return _ClaudeChat(self, system)

    def argv(self, system_file: str) -> list[str]:
        executable = shutil.which(self.command)
        if executable is None:
            raise ModelError(
                f"Claude Code was not found (looked for {self.command!r} on PATH). Install it from "
                "https://claude.com/claude-code and sign in by running `claude` once, or pick another model."
            )
        args = [
            executable, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools", "",
            "--system-prompt-file", system_file,
            "--strict-mcp-config",
            "--setting-sources", self.setting_sources,
            "--disable-slash-commands",
            "--no-session-persistence",
        ]  # fmt: skip
        if self.model_name:
            args += ["--model", self.model_name]
        if self.effort:
            args += ["--effort", str(self.effort)]
        return args + self.extra_args

    def environment(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        if self.strip_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", str(self.max_output_tokens))
        env.update(self.extra_env)
        return env

    def describe(self) -> dict[str, Any]:
        return {"spec": self.spec, "command": self.command, **self.info}


class _Lost(ModelError):
    """The CLI session is gone; the conversation can be rebuilt and retried."""

    def __init__(self, message: str):
        super().__init__(message, retryable=True)


class _ClaudeChat(Chat):
    def __init__(self, model: ClaudeCode, system: str):
        self.model = model
        self.system = system
        self._proc: asyncio.subprocess.Process | None = None
        self._dir: str | None = None
        self._stderr = b""
        self._stderr_task: asyncio.Task | None = None
        self._billed = 0.0
        self._history: list[tuple[str, str]] = []

    # ------------------------------------------------------------- process

    async def _start(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="rlm-claude-")
        system_file = os.path.join(self._dir, "system.md")
        with open(system_file, "w", encoding="utf-8") as handle:
            handle.write(self.system)
        argv = self.model.argv(system_file)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._dir,
                env={**self.model.environment(), "PWD": self._dir},
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            raise ModelError(f"could not start Claude Code: {exc}") from exc
        procs.track(self._proc.pid)
        self._billed = 0.0
        self._stderr = b""
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(self._proc))

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr = (self._stderr + chunk)[-4000:]

    async def _stop(self, *, graceful: bool) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if graceful:
                with contextlib.suppress(Exception):
                    if proc.stdin is not None:
                        proc.stdin.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 3)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 3)
            procs.untrack(proc.pid)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stderr_task
            self._stderr_task = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    async def close(self) -> None:
        await self._stop(graceful=True)

    # ---------------------------------------------------------------- turns

    async def send(self, text: str, *, on_text: TextSink | None = None) -> Reply:
        last: ModelError | None = None
        for _attempt in range(1 + max(0, self.model.retries)):
            # A fresh process knows nothing: hand it the transcript so far.
            payload = text if self._proc is not None or not self._history else replay_message(self._history, text)
            try:
                reply = await self._turn(payload, on_text)
            except _Lost as exc:
                last = exc
                await self._stop(graceful=False)
                continue
            except BaseException:
                await self._stop(graceful=False)
                raise
            self._history.append((text, reply.text))
            return reply
        assert last is not None
        raise ModelError(str(last))

    async def _turn(self, payload: str, on_text: TextSink | None) -> Reply:
        if self._proc is None:
            await self._start()
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        message = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": payload}]}}
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise _Lost(await self._failure("Claude Code exited before the message was sent")) from None
        try:
            return await asyncio.wait_for(self._read_turn(on_text), self.model.turn_timeout)
        except TimeoutError:
            raise _Lost(
                f"Claude Code did not finish a turn within {self.model.turn_timeout:g}s "
                "(raise [providers.claude-code] timeout if the model needs longer)"
            ) from None

    async def _read_turn(self, on_text: TextSink | None) -> Reply:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        streamed: list[str] = []  # text deltas, when the CLI streams them
        size = 0
        checked = 0
        whole: list[str] = []  # text blocks from complete assistant messages
        usage: dict[str, Any] = {}
        stop: str | None = None
        loop = asyncio.get_running_loop()
        progress = loop.time()
        pending: asyncio.Future | None = None
        while True:
            # Poll in short slices so a stalled upstream request is noticed even
            # while keep-alive pings keep the pipe busy.
            if pending is None:
                pending = asyncio.ensure_future(proc.stdout.readline())
            done, _ = await asyncio.wait({pending}, timeout=5)
            if loop.time() - progress > self.model.stall_timeout:
                pending.cancel()
                raise _Lost(f"Claude Code made no progress for {self.model.stall_timeout:g}s (upstream stall)")
            if not done:
                continue
            line, pending = pending.result(), None
            if not line:
                raise _Lost(await self._failure("Claude Code exited unexpectedly"))
            if _DEBUG_DIR:
                with open(os.path.join(_DEBUG_DIR, f"claude-{proc.pid}.jsonl"), "ab") as dump:
                    dump.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if not (kind == "stream_event" and (event.get("event") or {}).get("type") == "ping"):
                progress = loop.time()
            if kind == "system" and event.get("subtype") == "init":
                self.model.info.update(
                    model=event.get("model"),
                    auth=event.get("apiKeySource"),
                    version=event.get("claude_code_version"),
                )
            elif kind == "stream_event":
                inner = event.get("event") or {}
                inner_kind = inner.get("type")
                if inner_kind == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        streamed.append(delta["text"])
                        size += len(delta["text"])
                        if on_text:
                            on_text(delta["text"])
                        if size - checked >= 1000:
                            checked = size
                            text = "".join(streamed)
                            if looks_runaway(text, max_chars=self.model.max_reply_chars):
                                # Stop paying for garbage. The next turn starts a new
                                # session from the transcript, with this reply shortened.
                                await self._stop(graceful=False)
                                return Reply(
                                    text=text,
                                    usage=Usage(output_tokens=approx_tokens(len(text))),
                                    model=self.model.info.get("model") or self.model.spec,
                                    stop="runaway",
                                )
                elif inner_kind == "message_delta":
                    usage = inner.get("usage") or usage
                    stop = (inner.get("delta") or {}).get("stop_reason") or stop
            elif kind == "assistant":
                content = (event.get("message") or {}).get("content") or []
                whole.extend(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            elif kind == "result":
                text = "".join(streamed) or "".join(whole) or str(event.get("result") or "")
                if event.get("is_error"):
                    detail = str(event.get("result") or event.get("subtype") or "unknown error")
                    raise ModelError(f"Claude Code reported an error: {detail}")
                if not text.strip() and stop == "refusal":
                    raise ModelError("the model declined to answer (stop_reason: refusal)")
                return Reply(
                    text=text,
                    usage=self._usage(usage or event.get("usage") or {}, event.get("total_cost_usd")),
                    model=self.model.info.get("model") or self.model.spec,
                    stop=stop or event.get("stop_reason"),
                )

    def _usage(self, raw: dict[str, Any], total_cost: Any) -> Usage:
        cached = int(raw.get("cache_read_input_tokens") or 0)
        prompt = int(raw.get("input_tokens") or 0) + int(raw.get("cache_creation_input_tokens") or 0) + cached
        cost = None
        if isinstance(total_cost, (int, float)):
            # The CLI reports a running total for the session.
            cost = max(0.0, float(total_cost) - self._billed)
            self._billed = float(total_cost)
        return Usage(
            input_tokens=prompt,
            output_tokens=int(raw.get("output_tokens") or 0),
            cached_tokens=cached,
            cost_usd=cost,
        )

    async def _failure(self, what: str) -> str:
        proc = self._proc
        code = None
        if proc is not None:
            with contextlib.suppress(Exception):
                code = await asyncio.wait_for(proc.wait(), 2)
        await asyncio.sleep(0.05)
        tail = self._stderr.decode("utf-8", "replace").strip()
        detail = f" (exit code {code})" if code is not None else ""
        hint = ""
        if any(word in tail.lower() for word in ("login", "api key", "authenticat", "unauthorized")):
            hint = " Run `claude` once to sign in."
        return f"{what}{detail}{': ' + tail[-600:] if tail else ''}{hint}"
