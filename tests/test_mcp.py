from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

import rlm_ig as rlm
from fakes.mcp_server import HttpMcpServer
from rlm_ig.tools.mcp import connect

from helpers import block, run_async

SERVER = str(Path(__file__).parent / "fakes" / "mcp_server.py")
STDIO = {"command": sys.executable, "args": [SERVER]}


def with_hub(specs, body):
    async def go():
        hub = await connect(specs)
        try:
            return await body({tool.name: tool for tool in hub.tools})
        finally:
            await hub.close()

    return run_async(go())


@pytest.fixture(params=["stdio", "http"])
def spec(request):
    if request.param == "stdio":
        yield dict(STDIO)
    else:
        with HttpMcpServer() as server:
            yield {"url": server.url, "headers": {"Authorization": "Bearer t0ken"}}


def test_tools_are_discovered_across_pages_with_python_signatures(spec):
    async def body(tools):
        return tools

    tools = with_hub({"calc": spec}, body)
    assert list(tools) == ["calc_add", "calc_echo", "calc_fail", "calc_slow", "calc_list_items"]
    assert tools["calc_add"].signature == "(*, a: int, b: int) -> Any"
    assert tools["calc_echo"].signature == "(*, text: str, shout: bool = None) -> Any"
    assert tools["calc_fail"].signature == "() -> Any"
    assert tools["calc_add"].doc == "Add two integers.\n\nArgs:\n    a: first addend"
    assert tools["calc_add"].is_async


def test_results_are_converted(spec):
    async def body(tools):
        return (
            await tools["calc_add"].fn(a=2, b=40),
            await tools["calc_echo"].fn(text="hi", shout=True),
            await tools["calc_echo"].fn(text="quiet", shout=None),
            await tools["calc_slow"].fn(seconds=0),
            await tools["calc_list_items"].fn(),
        )

    structured, shouted, plain, joined, raw = with_hub({"calc": spec}, body)
    assert structured == {"sum": 42}
    assert (shouted, plain) == ("HI", "quiet")
    assert joined == "done\nreally"
    assert raw == [{"type": "resource", "uri": "x://1"}]


def test_errors_become_tool_errors(spec):
    async def body(tools):
        with pytest.raises(rlm.ToolError, match="calc_fail failed: it broke"):
            await tools["calc_fail"].fn()
        with pytest.raises(rlm.ToolError, match=r"keyword arguments only: calc_add\(\*, a: int, b: int\)"):
            await tools["calc_add"].fn(1, 2)
        with pytest.raises(rlm.ToolError, match="calc_add"):
            await tools["calc_add"].fn(a=1)  # the server raises on the missing argument

    with_hub({"calc": spec}, body)


def test_allowlist_limits_what_is_exposed(spec):
    async def body(tools):
        return list(tools)

    assert with_hub({"m": {**spec, "tools": ["echo", "add"]}}, body) == ["m_add", "m_echo"]
    with pytest.raises(rlm.ConfigError, match="'m' has no tool named nope"):
        with_hub({"m": {**spec, "tools": ["nope"]}}, body)


def test_stdio_calls_overlap_and_time_out():
    async def body(tools):
        started = time.monotonic()
        await asyncio.gather(*(tools["s_slow"].fn(seconds=0.4) for _ in range(5)))
        elapsed = time.monotonic() - started
        with pytest.raises(rlm.ToolError, match="did not answer"):
            await tools["s_slow"].fn(seconds=3)
        return elapsed

    assert with_hub({"s": {**STDIO, "timeout": 1.5}}, body) < 1.2


def test_http_session_and_headers_are_carried():
    with HttpMcpServer() as server:

        async def body(tools):
            return await tools["web_add"].fn(a=1, b=2)

        assert with_hub({"web": {"url": server.url, "headers": {"Authorization": "Bearer t0ken"}}}, body) == {"sum": 3}
        methods = [entry["method"] for entry in server.seen]
        assert methods == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/list",
            "tools/call",
            "DELETE",
        ]
        assert server.seen[0]["session"] is None and all(e["session"] == server.SESSION for e in server.seen[1:])
        assert server.seen[2]["protocol"] == "2025-06-18" and server.seen[2]["auth"] == "Bearer t0ken"


def test_connection_problems_name_the_server():
    async def body(tools):
        return tools

    with pytest.raises(rlm.ConfigError, match="'gone': could not start 'no-such-mcp-binary'"):
        with_hub({"gone": {"command": "no-such-mcp-binary"}}, body)
    with pytest.raises(rlm.ConfigError, match="'dead' could not be initialised"):
        with_hub({"dead": {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]}}, body)
    with pytest.raises(rlm.ConfigError, match="'web' could not be initialised"):
        with_hub({"web": {"url": "http://127.0.0.1:9/mcp", "timeout": 2}}, body)
    with pytest.raises(rlm.ConfigError, match='needs either "command"'):
        with_hub({"odd": {}}, body)


def test_a_failing_server_does_not_leave_the_others_running():
    def running() -> int:
        listing = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        return sum(SERVER in line.split()[:3] for line in listing.splitlines())

    before = running()

    async def body(tools):
        return tools

    with pytest.raises(rlm.ConfigError):
        with_hub({"good": dict(STDIO), "bad": {"command": "no-such-mcp-binary"}}, body)
    with_hub({"good": dict(STDIO)}, body)
    assert running() == before


def test_name_collisions_between_servers_are_rejected():
    async def body(tools):
        return tools

    # Both names become the identifier prefix "my_srv".
    specs = {"my-srv": {**STDIO, "tools": ["add"]}, "my_srv": {**STDIO, "tools": ["add"]}}
    with pytest.raises(rlm.ConfigError, match="both produce a tool named 'my_srv_add'"):
        with_hub(specs, body)


def test_mcp_tools_are_callable_from_the_repl(cfg):
    from rlm_ig.models.scripted import Scripted

    model = Scripted([block("print(calc_echo(text='ping', shout=True))\nsubmit(calc_add(a=20, b=22)['sum'])")])
    result = rlm.run("Add.", model=model, config=cfg.replace(mcp={"calc": dict(STDIO)}))
    assert result.ok and result.answer == 42
    first = model.turns[0].first
    assert "def calc_add(*, a: int, b: int) -> Any" in first and "Add two integers." in first
