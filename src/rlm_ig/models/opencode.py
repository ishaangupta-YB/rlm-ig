"""opencode as a model backend.

Two ways of driving it, chosen by ``mode``:

``server`` (preferred)
    One headless ``opencode serve`` process for the whole run. Each
    conversation is a server-side session, so a turn is a single HTTP request
    with no process start-up, and many conversations share the server.

``run``
    One ``opencode run`` process per turn, continuing a session by id. Slower
    (a start-up per turn) but needs nothing beyond the basic CLI.

``auto`` tries the server and falls back to ``run`` if it cannot start.

Either way opencode is used purely as a text model: the working directory
holds a generated ``opencode.json`` that denies every tool and defines a bare
agent whose prompt is ours, so opencode's own coding-agent prompt and tool
definitions never reach the model.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import tempfile
import urllib.error
import urllib.request
from typing import Any

from .. import procs
from ..budget import Usage
from ..errors import ModelError
from .base import Chat, Model, Reply, TextSink, replay_message

AGENT = "rlm"
PREFACE = "You are a language model used as a component by a host program. Follow the instructions below exactly."
_LISTENING = re.compile(rb"listening on (https?://[^\s]+)")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def project_config(prompt: str) -> dict[str, Any]:
    """The opencode.json written into the throwaway working directory."""
    deny_all = {"*": "deny"}
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": deny_all,
        "agent": {AGENT: {"mode": "primary", "prompt": prompt, "permission": deny_all}},
    }


class OpenCode(Model):
    parallel = 4
    window_chars = 400_000
    reports_cost = True

    def __init__(
        self,
        provider: str = "opencode",
        model: str = "",
        settings: dict[str, Any] | None = None,
        pricing: dict | None = None,
    ):
        settings = dict(settings or {})
        self.model_name = model or settings.get("model") or ""
        if self.model_name and "/" not in self.model_name:
            raise ModelError(
                f"opencode models are written provider/model (for example opencode:anthropic/claude-sonnet-5-5), "
                f"got {self.model_name!r}. Run `opencode models` to list them."
            )
        self.spec = provider + (f":{self.model_name}" if self.model_name else "")
        self.command = settings.get("command", "opencode")
        self.mode = settings.get("mode", "auto")
        if self.mode not in ("auto", "server", "run"):
            raise ModelError(f"[providers.opencode] mode must be auto, server or run, got {self.mode!r}")
        self.extra_args = [str(a) for a in settings.get("args", [])]
        self.extra_env = {str(k): str(v) for k, v in (settings.get("env") or {}).items()}
        self.turn_timeout = float(settings.get("timeout", 600))
        self.start_timeout = float(settings.get("start_timeout", 45))
        self.retries = int(settings.get("retries", 1))
        self.window_chars = int(settings.get("window_chars", self.window_chars))
        self.parallel = int(settings.get("parallel", self.parallel))
        self.info: dict[str, Any] = {}
        self._server: _Server | None = None
        self._server_lock = asyncio.Lock()
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    # ------------------------------------------------------------- plumbing

    def executable(self) -> str:
        found = shutil.which(self.command)
        if found is None:
            raise ModelError(
                f"opencode was not found (looked for {self.command!r} on PATH). Install it from https://opencode.ai "
                "and sign in with `opencode auth login`, or pick another model."
            )
        return found

    def environment(self, cwd: str) -> dict[str, str]:
        # PWD matters: opencode locates its project config from it, so a stale
        # value would make it read the caller's project instead of ours.
        return {**os.environ, **self.extra_env, "PWD": cwd}

    def model_ref(self) -> dict[str, str] | None:
        if not self.model_name:
            return None
        provider_id, _, model_id = self.model_name.partition("/")
        return {"providerID": provider_id, "modelID": model_id}

    async def server(self) -> _Server | None:
        """The shared server, started on first use. ``None`` means use run mode."""
        if self.mode == "run":
            self.info.setdefault("mode", "run")
            return None
        async with self._server_lock:
            if self._server is None:
                candidate = _Server(self)
                try:
                    await candidate.start()
                except ModelError:
                    await candidate.stop()
                    if self.mode == "server":
                        raise
                    self.mode = "run"  # auto: remember the fallback
                    self.info["mode"] = "run"
                    return None
                self._server = candidate
                self.info["mode"] = "server"
            return self._server

    def chat(self, system: str) -> Chat:
        return _OpenCodeChat(self, system)

    def pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="rlm-opencode")
        return self._pool

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.stop()
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if self.mode == "run" and self.info.get("mode") == "run":
            self.mode = "auto"  # let a later run try the server again

    def describe(self) -> dict[str, Any]:
        return {"spec": self.spec, "command": self.command, **self.info}


class _Server:
    """A headless opencode server bound to localhost with a one-off password."""

    def __init__(self, model: OpenCode):
        self.model = model
        self.url = ""
        self.password = secrets.token_urlsafe(24)
        self.dir = tempfile.mkdtemp(prefix="rlm-opencode-")
        self._proc: asyncio.subprocess.Process | None = None
        self._log = b""
        self._pump: asyncio.Task | None = None

    async def start(self) -> None:
        with open(os.path.join(self.dir, "opencode.json"), "w", encoding="utf-8") as handle:
            json.dump(project_config(PREFACE), handle)
        argv = [self.model.executable(), "serve", "--hostname", "127.0.0.1", "--port", "0", *self.model.extra_args]
        env = self.model.environment(self.dir)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.dir,
                env=env,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ModelError(f"could not start the opencode server: {exc}") from exc
        procs.track(self._proc.pid, group=True)
        try:
            self.url = await asyncio.wait_for(self._wait_for_url(), self.model.start_timeout)
        except TimeoutError:
            tail = self._log.decode("utf-8", "replace").strip()[-400:]
            raise ModelError(f"the opencode server did not come up in {self.model.start_timeout:g}s: {tail}") from None
        self._pump = asyncio.ensure_future(self._drain())

    async def _wait_for_url(self) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                tail = self._log.decode("utf-8", "replace").strip()[-400:]
                raise ModelError(f"the opencode server exited during start-up: {tail}")
            self._log = (self._log + line)[-4000:]
            found = _LISTENING.search(line)
            if found:
                return found.group(1).decode().rstrip("/")

    async def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        with contextlib.suppress(Exception):
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    return
                self._log = (self._log + chunk)[-4000:]

    def request(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        """Blocking JSON request; call it through the model's thread pool."""
        token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        headers = {"authorization": f"Basic {token}", "content-type": "application/json", "accept": "application/json"}
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with _NO_PROXY.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ModelError(f"opencode server returned HTTP {exc.code} for {method} {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ModelError(f"opencode server request failed ({method} {path}): {exc}", retryable=True) from None
        return json.loads(raw) if raw.strip() else None

    async def call(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.model.pool(), self.request, method, path, body, timeout)

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            # The npm-installed `opencode` is a launcher whose child is the real
            # server, so signal the whole group, politely first.
            procs.kill(proc.pid, group=True, sig=signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:  # noqa: BLE001
                pass
            procs.kill(proc.pid, group=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 3)
            procs.untrack(proc.pid)
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(BaseException):
                await self._pump
            self._pump = None
        shutil.rmtree(self.dir, ignore_errors=True)


class _OpenCodeChat(Chat):
    def __init__(self, model: OpenCode, system: str):
        self.model = model
        self.system = system
        self._session: str | None = None
        self._server: _Server | None = None
        self._dir: str | None = None  # run mode only
        self._history: list[tuple[str, str]] = []

    async def send(self, text: str, *, on_text: TextSink | None = None) -> Reply:
        last: ModelError | None = None
        for _attempt in range(1 + max(0, self.model.retries)):
            payload = text if self._session is not None or not self._history else replay_message(self._history, text)
            try:
                server = await self.model.server()
                reply = await (self._via_server(server, payload) if server else self._via_run(payload))
            except ModelError as exc:
                if not exc.retryable:
                    raise
                last = exc
                self._session = None  # start a new session and replay
                continue
            self._history.append((text, reply.text))
            if on_text:
                on_text(reply.text)
            return reply
        assert last is not None
        raise ModelError(str(last))

    # ---------------------------------------------------------- server mode

    async def _via_server(self, server: _Server, payload: str) -> Reply:
        self._server = server
        if self._session is None:
            created = await server.call("POST", "/session", {"title": "rlm"})
            self._session = created["id"]
        body: dict[str, Any] = {
            "agent": AGENT,
            "system": self.system,
            "parts": [{"type": "text", "text": payload}],
        }
        ref = self.model.model_ref()
        if ref:
            body["model"] = ref
        out = await server.call("POST", f"/session/{self._session}/message", body, timeout=self.model.turn_timeout)
        info = (out or {}).get("info") or {}
        _raise_for(info.get("error"))
        text = "".join(p.get("text", "") for p in (out or {}).get("parts") or [] if p.get("type") == "text")
        if not self.model.info.get("model") and info.get("modelID"):
            self.model.info["model"] = f"{info.get('providerID')}/{info.get('modelID')}"
        return Reply(text=text, usage=_usage(info.get("tokens"), info.get("cost")), model=self.model.info.get("model"))

    # ------------------------------------------------------------- run mode

    async def _via_run(self, payload: str) -> Reply:
        if self._dir is None:
            self._dir = tempfile.mkdtemp(prefix="rlm-opencode-")
            with open(os.path.join(self._dir, "opencode.json"), "w", encoding="utf-8") as handle:
                json.dump(project_config(self.system), handle)
        argv = [self.model.executable(), "run", "--format", "json", "--pure", "--agent", AGENT, "--title", "rlm"]
        if self.model.model_name:
            argv += ["-m", self.model.model_name]
        if self._session:
            argv += ["--session", self._session]
        argv += self.model.extra_args
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._dir,
                env=self.model.environment(self._dir),
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ModelError(f"could not start opencode: {exc}") from exc
        procs.track(proc.pid, group=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload.encode("utf-8")), self.model.turn_timeout)
        except TimeoutError:
            raise ModelError(f"opencode did not answer within {self.model.turn_timeout:g}s", retryable=True) from None
        finally:
            # Covers a timeout, a cancelled run, and any child the launcher left.
            procs.kill(proc.pid, group=True)
            procs.untrack(proc.pid)
        texts: list[str] = []
        usage = Usage()
        for line in out.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self._session = event.get("sessionID") or self._session
            kind, part = event.get("type"), event.get("part") or {}
            if kind == "text":
                texts.append(part.get("text", ""))
            elif kind == "step_finish":
                usage.add(_usage(part.get("tokens"), part.get("cost")))
            elif kind == "error":
                _raise_for(event.get("error"))
        if proc.returncode != 0 and not texts:
            tail = err.decode("utf-8", "replace").strip()[-500:]
            raise ModelError(f"opencode exited with code {proc.returncode}: {tail or 'no output'}")
        usage.calls = 0
        return Reply(text="".join(texts), usage=usage, model=self.model.model_name or None)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session and self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.call("DELETE", f"/session/{session}", timeout=10)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


def _raise_for(error: Any) -> None:
    if not error:
        return
    if isinstance(error, dict):
        data = error.get("data") or {}
        message = data.get("message") if isinstance(data, dict) else None
        detail = f"{error.get('name', 'error')}: {message or json.dumps(data)[:300]}"
    else:
        detail = str(error)
    hint = ""
    lowered = detail.lower()
    if any(word in lowered for word in ("api key", "unauthorized", "credential", "auth")):
        hint = " Check `opencode auth list` and sign in with `opencode auth login`."
    raise ModelError(f"opencode reported an error: {detail}.{hint}")


def _usage(tokens: Any, cost: Any) -> Usage:
    tokens = tokens if isinstance(tokens, dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cached = int(cache.get("read") or 0)
    return Usage(
        input_tokens=int(tokens.get("input") or 0) + cached + int(cache.get("write") or 0),
        output_tokens=int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0),
        cached_tokens=cached,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )
