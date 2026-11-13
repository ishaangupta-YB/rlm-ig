"""A small Model Context Protocol client, standard library only.

MCP servers expose tools over JSON-RPC 2.0. Two transports are supported:

stdio
    ``{"command": "npx", "args": ["-y", "some-server"], "env": {...}, "cwd": "..."}``
    The server is started as a child process and spoken to with one JSON
    message per line on its stdin and stdout.

streamable HTTP
    ``{"url": "https://host/mcp", "headers": {...}}``
    Every message is a POST. The reply is either a JSON body or a
    server-sent-event stream that carries the response.

Optional keys for both: ``timeout`` (seconds per request, default 60) and
``tools`` (names to expose; default all).

Each MCP tool becomes an ordinary host tool named ``<server>_<tool>``, so in
the REPL it is just a function that takes keyword arguments.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import __version__, procs
from ..errors import ConfigError, ToolError
from . import Tool, safe_name

PROTOCOL_VERSION = "2025-06-18"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "array": "list", "object": "dict"}


class _RpcError(Exception):
    """The server answered a request with a JSON-RPC error."""


class _Server:
    """Shared request logic; subclasses provide the transport."""

    def __init__(self, name: str, spec: dict[str, Any]):
        self.name = name
        self.spec = spec
        self.timeout = float(spec.get("timeout", 60))
        self._ids = 0

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    async def notify(self, method: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def handshake(self) -> list[dict[str, Any]]:
        """Initialise the connection and return every tool the server lists."""
        hello = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "rlm-ig", "version": __version__},
            },
        )
        self.protocol = (hello or {}).get("protocolVersion") or PROTOCOL_VERSION
        await self.notify("notifications/initialized")
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend((page or {}).get("tools") or [])
            cursor = (page or {}).get("nextCursor")
            if not cursor:
                return tools


class _Stdio(_Server):
    def __init__(self, name: str, spec: dict[str, Any]):
        super().__init__(name, spec)
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._errors: asyncio.Task | None = None
        self._stderr = b""
        self._write = asyncio.Lock()

    async def start(self) -> None:
        argv = [str(self.spec["command"]), *(str(a) for a in self.spec.get("args") or [])]
        env = {**os.environ, **{str(k): str(v) for k, v in (self.spec.get("env") or {}).items()}}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.spec.get("cwd"),
                env=env,
                limit=64 * 1024 * 1024,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ConfigError(f"MCP server {self.name!r}: could not start {argv[0]!r}: {exc}") from exc
        procs.track(self._proc.pid, group=True)
        self._reader = asyncio.ensure_future(self._read())
        self._errors = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        with contextlib.suppress(Exception):
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr = (self._stderr + chunk)[-2000:]

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # servers sometimes log to stdout; skip anything that is not JSON
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    if "id" in message:  # a request from the server to us
                        await self._answer(message)
                    continue
                future = self._pending.pop(message.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            tail = self._stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            why = "the server exited" + (": " + " | ".join(tail) if tail else "")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(why))
            self._pending.clear()

    async def _answer(self, message: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if message.get("method") == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": f"{message.get('method')} is not supported by this client"}
        with contextlib.suppress(Exception):
            await self._send(reply)

    async def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            raise ConnectionError("the server is not running")
        async with self._write:
            self._proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            await self._proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            message = await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            raise TimeoutError(f"no reply to {method} within {self.timeout:g}s") from None
        finally:
            self._pending.pop(request_id, None)
        return _result(message)

    async def notify(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method})

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 1)
            if proc.returncode is None:
                procs.kill(proc.pid, group=True, sig=signal.SIGTERM)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 2)
            procs.kill(proc.pid, group=True)  # anything the launcher left behind
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)
            procs.untrack(proc.pid)
        for task in (self._reader, self._errors):
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._reader = self._errors = None


class _Http(_Server):
    def __init__(self, name: str, spec: dict[str, Any]):
        super().__init__(name, spec)
        self.url = str(spec["url"])
        self.headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        self.session: str | None = None
        self.protocol: str | None = None
        host = urllib.parse.urlsplit(self.url).hostname or ""
        self._opener = _DIRECT if host in _LOOPBACK else urllib.request.build_opener()

    def _post(self, message: dict[str, Any]) -> Any:
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", **self.headers}
        if self.session:
            headers["mcp-session-id"] = self.session
        if self.protocol:
            headers["mcp-protocol-version"] = self.protocol
        request = urllib.request.Request(self.url, data=json.dumps(message).encode(), method="POST", headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                self.session = response.headers.get("mcp-session-id") or self.session
                if "id" not in message:
                    return None
                if "text/event-stream" in (response.headers.get("content-type") or ""):
                    return _from_sse(response, message["id"])
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
            raise ConnectionError(f"HTTP {exc.code}{': ' + detail if detail else ''}") from None
        except urllib.error.URLError as exc:
            raise ConnectionError(str(exc.reason)) from None
        return json.loads(body) if body.strip() else None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        message = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params or {}}
        reply = await asyncio.to_thread(self._post, message)
        if reply is None:
            raise ConnectionError(f"the server sent no response to {method}")
        return _result(reply)

    async def notify(self, method: str) -> None:
        await asyncio.to_thread(self._post, {"jsonrpc": "2.0", "method": method})

    async def close(self) -> None:
        if not self.session:
            return
        headers = {**self.headers, "mcp-session-id": self.session}
        request = urllib.request.Request(self.url, method="DELETE", headers=headers)
        with contextlib.suppress(Exception):  # ending the session is a courtesy; many servers do not implement it
            await asyncio.to_thread(lambda: self._opener.open(request, timeout=5).close())


def _from_sse(response: Any, request_id: int) -> Any:
    """Read server-sent events until the JSON-RPC response for ``request_id``."""
    data: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line or not data:
            continue
        payload, data = "\n".join(data), []
        try:
            message = json.loads(payload)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
            return message
    return None


def _result(message: dict[str, Any]) -> Any:
    error = message.get("error")
    if error:
        text = error.get("message") if isinstance(error, dict) else str(error)
        raise _RpcError(text or "unknown error")
    return message.get("result")


# --------------------------------------------------------------- tool wrappers


def _signature(schema: dict[str, Any]) -> str:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [name for name in schema.get("required") or [] if name in properties]
    optional = [name for name in properties if name not in required]

    def kind(name: str) -> str:
        declared = (properties[name] or {}).get("type")
        return _TYPES.get(declared, "Any") if isinstance(declared, str) else "Any"

    parts = [f"{name}: {kind(name)}" for name in required] + [f"{name}: {kind(name)} = None" for name in optional]
    return f"(*, {', '.join(parts)}) -> Any" if parts else "() -> Any"


def _doc(spec: dict[str, Any]) -> str:
    lines = [str(spec.get("description") or "").strip() or f"MCP tool {spec.get('name')}."]
    properties = (spec.get("inputSchema") or {}).get("properties") or {}
    described = [
        (name, str(info.get("description")).strip()) for name, info in properties.items() if info.get("description")
    ]
    if described:
        lines += ["", "Args:"] + [f"    {name}: {text}" for name, text in described]
    return "\n".join(lines)


def _convert(result: dict[str, Any], label: str) -> Any:
    content = result.get("content") or []
    texts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
    if result.get("isError"):
        raise ToolError(f"{label} failed: {' '.join(texts).strip() or 'the server reported an error'}")
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    if texts:
        return texts[0] if len(texts) == 1 else "\n".join(texts)
    return content


def _wrap(server: _Server, spec: dict[str, Any]) -> Tool:
    remote = str(spec["name"])
    local = safe_name(f"{server.name}_{remote}")
    signature = _signature(spec.get("inputSchema") or {})

    async def call(*args: Any, **kwargs: Any) -> Any:
        if args:
            raise ToolError(f"{local} takes keyword arguments only: {local}{signature}")
        arguments = {key: value for key, value in kwargs.items() if value is not None}
        try:
            result = await server.request("tools/call", {"name": remote, "arguments": arguments})
        except _RpcError as exc:
            raise ToolError(f"{local} failed: {exc}") from None
        except (TimeoutError, ConnectionError, OSError, ValueError) as exc:
            raise ToolError(f"{local}: MCP server {server.name!r} did not answer ({exc})") from None
        return _convert(result or {}, local)

    call.__name__ = local
    return Tool(name=local, fn=call, signature=signature, doc=_doc(spec), is_async=True)


class McpHub:
    """Every connected server and the tools they provide."""

    def __init__(self) -> None:
        self.servers: list[_Server] = []
        self.tools: list[Tool] = []

    async def close(self) -> None:
        servers, self.servers = self.servers, []
        await asyncio.gather(*(server.close() for server in servers), return_exceptions=True)


async def _open(name: str, spec: dict[str, Any]) -> tuple[_Server, list[Tool]]:
    if not isinstance(spec, dict) or ("command" in spec) == ("url" in spec):
        raise ConfigError(f'MCP server {name!r} needs either "command" (stdio) or "url" (HTTP)')
    server: _Server = _Stdio(name, spec) if "command" in spec else _Http(name, spec)
    try:
        if isinstance(server, _Stdio):
            await server.start()
        listed = await server.handshake()
    except ConfigError:
        await server.close()
        raise
    except (_RpcError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
        await server.close()
        raise ConfigError(f"MCP server {name!r} could not be initialised: {exc}") from None
    wanted = spec.get("tools")
    if wanted is not None:
        missing = sorted(set(wanted) - {tool.get("name") for tool in listed})
        if missing:
            await server.close()
            raise ConfigError(f"MCP server {name!r} has no tool named {', '.join(missing)}")
        listed = [tool for tool in listed if tool.get("name") in wanted]
    return server, [_wrap(server, tool) for tool in listed if tool.get("name")]


async def connect(specs: dict[str, dict[str, Any]]) -> McpHub:
    """Connect to every server in ``specs`` at once and collect their tools."""
    hub = McpHub()
    outcomes = await asyncio.gather(*(_open(name, spec) for name, spec in specs.items()), return_exceptions=True)
    failure: BaseException | None = None
    seen: dict[str, str] = {}
    for name, outcome in zip(specs, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failure = failure or outcome
            continue
        server, tools = outcome
        hub.servers.append(server)
        for tool in tools:
            if tool.name in seen and failure is None:
                failure = ConfigError(
                    f"MCP servers {seen[tool.name]!r} and {name!r} both produce a tool named {tool.name!r}"
                )
            seen[tool.name] = name
            hub.tools.append(tool)
    if failure is not None:
        await hub.close()
        raise failure
    return hub
