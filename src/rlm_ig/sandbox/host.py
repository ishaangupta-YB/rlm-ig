"""Host side of the REPL sandbox: start a worker, run cells, serve its calls."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import pickle
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import SandboxError

WORKER_SOURCE = (Path(__file__).with_name("worker.py")).read_bytes()

# The worker receives its own source as the first bytes on stdin. Nothing has
# to exist on the worker's filesystem, which is what lets a launcher prefix
# (container, remote shell, jail) wrap it without any mounts.
# The read is unbuffered and exact on purpose: a buffered reader would pull the
# first protocol frames into its own buffer, where the worker never looks.
BOOTSTRAP = (
    "import os,sys\n"
    "n=int(sys.argv[1]);b=b''\n"
    "while len(b)<n:\n"
    " c=os.read(0,n-len(b))\n"
    " if not c:break\n"
    " b+=c\n"
    "exec(compile(b,'<rlm-worker>','exec'))"
)

# Environment the worker inherits by default. Credentials are intentionally
# absent: model-written code has no business reading the host's API keys.
PASSTHROUGH_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)  # fmt: skip

CallHandler = Callable[[str, dict], Awaitable[dict]]


@dataclass
class SandboxOptions:
    """How REPL workers are launched and limited."""

    python: str | None = None
    launcher: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    pass_env: list[str] = field(default_factory=list)
    inherit_env: bool = False
    memory_mb: int | None = None
    cell_timeout: float = 120.0
    capture_limit: int = 200_000
    workdir: str | None = None

    def command(self, source_len: int) -> list[str]:
        python = self.python or ("python3" if self.launcher else sys.executable)
        return [*self.launcher, python, "-u", "-c", BOOTSTRAP, str(source_len)]

    def environment(self) -> dict[str, str]:
        if self.inherit_env:
            env = dict(os.environ)
        else:
            env = {k: os.environ[k] for k in (*PASSTHROUGH_ENV, *self.pass_env) if k in os.environ}
        env.update(self.env)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env


@dataclass
class CellResult:
    stdout: str = ""
    error: str | None = None
    submitted: bool = False
    value: Any = None
    seconds: float = 0.0
    interrupted: bool = False
    restarted: bool = False

    @property
    def text(self) -> str:
        """What the model should see: output followed by any error."""
        parts = [self.stdout.rstrip()] if self.stdout.strip() else []
        if self.error:
            parts.append(self.error)
        return "\n".join(parts)


def encode(value: Any) -> dict:
    """Package a host value for the worker: JSON when lossless, else a pickle."""
    try:
        json.dumps(value, allow_nan=False)
        if _is_plain(value):
            return {"j": value}
    except (TypeError, ValueError):
        pass
    try:
        return {"p": base64.b64encode(pickle.dumps(value, protocol=4)).decode("ascii")}
    except Exception as exc:
        raise SandboxError(f"value of type {type(value).__name__} cannot be sent to the sandbox: {exc}") from exc


def _is_plain(value: Any, depth: int = 0) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if depth > 60:
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_plain(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return all(_is_plain(v, depth + 1) for v in value)
    return False


class Sandbox:
    """One worker process holding one persistent Python namespace."""

    def __init__(self, options: SandboxOptions | None = None, *, name: str = "sandbox"):
        self.options = options or SandboxOptions()
        self.name = name
        self.summary = ""
        self.context_size = 0
        self.python_version = ""
        self.helpers: list[str] = []
        self.problems: list[str] = []
        self._proc: asyncio.subprocess.Process | None = None
        self._init: dict | None = None
        self._write_lock = asyncio.Lock()
        self._busy = asyncio.Lock()
        self._ids = 0
        self._stderr_tail = b""
        self._stderr_task: asyncio.Task | None = None
        self._cwd: str | None = None
        self._own_cwd = False

    # ------------------------------------------------------------------ life

    async def start(
        self,
        context: Any = None,
        *,
        tools: list[dict] | None = None,
        helpers: list[str] | None = None,
    ) -> str:
        """Launch the worker and load ``context``. Returns a summary of it."""
        self._init = {
            "op": "init",
            "context": encode(context),
            "tools": tools or [],
            "helpers": helpers or [],
            "config": {"capture_limit": self.options.capture_limit, "memory_mb": self.options.memory_mb},
        }
        await self._spawn()
        return self.summary

    async def _spawn(self) -> None:
        opts = self.options
        if self._cwd is None:
            if opts.workdir:
                Path(opts.workdir).mkdir(parents=True, exist_ok=True)
                self._cwd = opts.workdir
            else:
                self._cwd = tempfile.mkdtemp(prefix="rlm-")
                self._own_cwd = True
        cmd = opts.command(len(WORKER_SOURCE))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=opts.environment(),
                start_new_session=(os.name == "posix"),
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise SandboxError(f"could not start the sandbox ({cmd[0]!r}): {exc}") from exc
        self._stderr_tail = b""
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(self._proc))
        assert self._proc.stdin is not None
        self._proc.stdin.write(WORKER_SOURCE)
        await self._send(self._init or {"op": "init"})
        ready = await self._recv()
        if ready is None or ready.get("op") != "ready":
            detail = await self._death_note()
            raise SandboxError(f"sandbox failed to start: {detail}")
        self.summary = ready.get("summary", "")
        self.context_size = int(ready.get("size") or 0)
        self.python_version = ready.get("python", "")
        self.helpers = list(ready.get("helpers") or [])
        self.problems = list(ready.get("problems") or [])

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.returncode is None:
                    self._kill(proc)
                await asyncio.wait_for(proc.wait(), 5)
            with contextlib.suppress(Exception):
                proc._transport.close()  # type: ignore[attr-defined]
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stderr_task
            self._stderr_task = None
        if self._own_cwd and self._cwd:
            shutil.rmtree(self._cwd, ignore_errors=True)
            self._cwd = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ------------------------------------------------------------------ cells

    async def run(self, code: str, on_call: CallHandler | None = None) -> CellResult:
        """Execute one cell, serving any calls the code makes back to the host."""
        async with self._busy:
            if not self.alive:
                await self._spawn()
            self._ids += 1
            cell_id = self._ids
            started = time.monotonic()
            await self._send({"op": "exec", "id": cell_id, "code": code})
            calls: set[asyncio.Task] = set()
            active = 0
            idle = 0.0
            interrupted_at: float | None = None

            async def serve(msg: dict) -> None:
                nonlocal active
                active += 1
                try:
                    if on_call is None:
                        reply = {"ok": False, "etype": "SubcallError", "error": "no host is serving calls"}
                    else:
                        try:
                            reply = await on_call(msg.get("kind", ""), msg)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # the handler is expected to catch its own errors
                            reply = {"ok": False, "etype": type(exc).__name__, "error": str(exc)}
                    reply = dict(reply, op="reply", id=msg.get("id"))
                    with contextlib.suppress(Exception):
                        await self._send(reply)
                finally:
                    active -= 1

            reader = asyncio.ensure_future(self._recv())
            try:
                while True:
                    done, _ = await asyncio.wait({reader}, timeout=0.25)
                    if not done:
                        if interrupted_at is not None:
                            if time.monotonic() - interrupted_at > 5:
                                return await self._crashed("the cell ignored its time limit and the worker was killed")
                        elif active == 0:
                            idle += 0.25
                            if idle >= self.options.cell_timeout:
                                interrupted_at = time.monotonic()
                                if not self._interrupt():
                                    return await self._crashed(
                                        f"the cell ran past {self.options.cell_timeout:g}s and the worker was killed"
                                    )
                        continue
                    msg = reader.result()
                    if msg is None:
                        return await self._crashed(await self._death_note())
                    op = msg.get("op")
                    if op == "call":
                        task = asyncio.ensure_future(serve(msg))
                        calls.add(task)
                        task.add_done_callback(calls.discard)
                    elif op == "done" and msg.get("id") == cell_id:
                        return CellResult(
                            stdout=msg.get("stdout") or "",
                            error=msg.get("error"),
                            submitted=bool(msg.get("submitted")),
                            value=msg.get("value"),
                            seconds=time.monotonic() - started,
                            interrupted=bool(msg.get("interrupted")),
                        )
                    elif op == "fault":
                        return CellResult(
                            error=f"sandbox fault: {msg.get('error')}", seconds=time.monotonic() - started
                        )
                    reader = asyncio.ensure_future(self._recv())
            finally:
                if not reader.done():
                    reader.cancel()
                    with contextlib.suppress(BaseException):
                        await reader
                for task in list(calls):
                    task.cancel()
                if calls:
                    await asyncio.gather(*calls, return_exceptions=True)

    async def _crashed(self, why: str) -> CellResult:
        """Replace a dead or stuck worker and report the loss to the model."""
        await self._terminate()
        note = (
            f"The REPL process stopped ({why}). It was restarted: `context` is back, but every other variable is gone."
        )
        try:
            await self._spawn()
        except SandboxError as exc:
            note = f"The REPL process stopped ({why}) and could not be restarted: {exc}"
        return CellResult(error=note, restarted=True)

    # --------------------------------------------------------------- requests

    async def _request(self, msg: dict, expect: str) -> dict:
        async with self._busy:
            if not self.alive:
                await self._spawn()
            self._ids += 1
            msg = dict(msg, id=self._ids)
            await self._send(msg)
            while True:
                reply = await self._recv()
                if reply is None:
                    raise SandboxError(f"sandbox died: {await self._death_note()}")
                if reply.get("op") == "fault":
                    raise SandboxError(f"sandbox fault: {reply.get('error')}")
                if reply.get("op") == expect and reply.get("id") == self._ids:
                    return reply

    async def set_context(self, context: Any) -> str:
        if self._init is not None:
            self._init["context"] = encode(context)
        reply = await self._request({"op": "set_context", "context": encode(context)}, "context")
        self.summary = reply.get("summary", "")
        self.context_size = int(reply.get("size") or 0)
        return self.summary

    async def inventory(self) -> list[dict]:
        return (await self._request({"op": "inventory"}, "inventory")).get("rows") or []

    async def snapshot(self, max_bytes: int = 50_000_000) -> dict:
        return (await self._request({"op": "snapshot", "max_bytes": max_bytes}, "snapshot")).get("state") or {}

    async def restore(self, state: dict) -> dict:
        return (await self._request({"op": "restore", "state": state}, "restored")).get("failed") or {}

    # ------------------------------------------------------------------- wire

    async def _send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise SandboxError("sandbox is not running")
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8", "surrogatepass")
        async with self._write_lock:
            try:
                proc.stdin.write(b"%d\n" % len(data))
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise SandboxError("sandbox pipe closed") from exc

    async def _recv(self) -> dict | None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        try:
            header = await proc.stdout.readline()
            if not header:
                return None
            data = await proc.stdout.readexactly(int(header))
        except (asyncio.IncompleteReadError, ValueError, ConnectionResetError):
            return None
        return json.loads(data.decode("utf-8", "surrogatepass"))

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_tail = (self._stderr_tail + chunk)[-4000:]

    async def _death_note(self) -> str:
        proc = self._proc
        code = None
        if proc is not None:
            with contextlib.suppress(Exception):
                code = await asyncio.wait_for(proc.wait(), 2)
        await asyncio.sleep(0.05)
        tail = self._stderr_tail.decode("utf-8", "replace").strip()
        last = tail.splitlines()[-1] if tail else ""
        if code is not None and code < 0:
            with contextlib.suppress(ValueError):
                return f"killed by {signal.Signals(-code).name}" + (f"; {last}" if last else "")
        return f"exit code {code}" + (f"; {last}" if last else "")

    def _interrupt(self) -> bool:
        """Ask the running cell to stop. False means it cannot be interrupted."""
        proc = self._proc
        if proc is None or os.name != "posix" or self.options.launcher:
            return False
        try:
            proc.send_signal(signal.SIGINT)
            return True
        except (ProcessLookupError, OSError):
            return False

    def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
                return
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            self._kill(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        with contextlib.suppress(Exception):
            proc._transport.close()  # type: ignore[attr-defined]
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
