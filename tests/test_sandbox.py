from __future__ import annotations

import os
import sys

import pytest

from rlm_ig.errors import SandboxError
from rlm_ig.sandbox import Sandbox, SandboxOptions
from rlm_ig.sandbox.host import _startup_hint, _stderr_summary

from helpers import run_async


def session(fn, context=None, options=None, **start):
    """Start a sandbox, hand it to ``fn``, always close it."""

    async def go():
        sandbox = Sandbox(options or SandboxOptions(cell_timeout=20))
        try:
            await sandbox.start(context, **start)
            return await fn(sandbox)
        finally:
            await sandbox.close()

    return run_async(go())


def test_state_persists_and_last_expression_is_shown():
    async def body(sb):
        await sb.run("x = 20 + 1")
        return await sb.run("x * 2")

    result = session(body)
    assert result.stdout.strip() == "42"
    assert result.error is None and not result.submitted


def test_context_summary_and_size():
    async def body(sb):
        return sb.summary, sb.context_size, (await sb.run("print(type(context).__name__, len(context))")).stdout

    summary, size, out = session(body, context="hello world\n" * 1000)
    assert "str, 12,000 chars" in summary
    assert size == 12000
    assert out.strip() == "str 12000"


def test_structured_and_pickled_contexts_arrive_intact():
    async def body(sb):
        return (await sb.run("print(context['rows'][1]['n'], type(context['when']).__name__, context['pair'])")).stdout

    import datetime

    context = {"rows": [{"n": 1}, {"n": 2}], "when": datetime.date(2024, 1, 2), "pair": (1, 2)}
    assert session(body, context=context).strip() == "2 date (1, 2)"


def test_traceback_points_at_model_code_only():
    async def body(sb):
        return await sb.run("def f(a):\n    return 1 / a\n\nf(0)")

    result = session(body)
    assert "ZeroDivisionError" in result.error
    assert "<cell 1>, line 2, in f" in result.error
    assert "worker" not in result.error


def test_syntax_error_is_reported_not_raised():
    result = session(lambda sb: sb.run("x = = 1"))
    assert "SyntaxError" in result.error


def test_submit_stops_the_cell_and_returns_json_safe_value():
    result = session(lambda sb: sb.run("submit({'a': (1, 2), 'b': {3}})\nprint('unreachable')"))
    assert result.submitted and result.value == {"a": [1, 2], "b": [3]}
    assert "unreachable" not in result.stdout


def test_submit_rejects_functions():
    result = session(lambda sb: sb.run("submit(print)"))
    assert not result.submitted and "needs data" in result.error


def test_output_is_bounded_and_keeps_both_ends():
    options = SandboxOptions(capture_limit=2000, cell_timeout=20)
    result = session(lambda sb: sb.run("for i in range(50000): print('line', i)"), options=options)
    assert len(result.stdout) < 5000
    assert result.stdout.startswith("line 0\n")
    assert "line 49999" in result.stdout and "characters omitted" in result.stdout


def test_child_process_and_raw_fd_output_is_captured():
    code = "import os, subprocess, sys\nsubprocess.run([sys.executable, '-c', 'print(\"from child\")'])\nos.write(1, b'raw write\\n')"
    result = session(lambda sb: sb.run(code))
    assert "from child" in result.stdout and "raw write" in result.stdout


def test_stdin_is_not_the_protocol_stream():
    result = session(lambda sb: sb.run("input()"))
    assert "EOFError" in result.error


def test_top_level_await_works():
    result = session(lambda sb: sb.run("import asyncio\nawait asyncio.sleep(0)\n'awaited'"))
    assert result.stdout.strip() == "'awaited'"


@pytest.mark.skipif(os.name != "posix", reason="cells are interrupted with SIGINT")
def test_runaway_cell_is_interrupted_and_state_survives():
    async def body(sb):
        await sb.run("kept = 123")
        slow = await sb.run("import time\nwhile True:\n    time.sleep(0.05)")
        after = await sb.run("kept")
        return slow, after

    slow, after = session(body, options=SandboxOptions(cell_timeout=1))
    assert slow.interrupted and "time limit" in slow.error
    assert after.stdout.strip() == "123"


def test_crash_restarts_with_context_but_without_variables():
    async def body(sb):
        await sb.run("mine = 1")
        crashed = await sb.run("import os\nos._exit(7)")
        after = await sb.run("print('mine' in globals(), context)")
        return crashed, after

    crashed, after = session(body, context="ctx")
    assert crashed.restarted and "exit code 7" in crashed.error
    assert after.stdout.strip() == "False ctx"


def test_calls_reach_the_host_and_errors_map_to_exceptions():
    seen = []

    async def on_call(kind, msg):
        seen.append(kind)
        if kind == "ask":
            return {"ok": True, "value": {"j": f"{msg['prompt']}|{msg['data']}"}}
        if kind == "ask_map":
            return {"ok": True, "value": {"j": [1, None, 3]}, "failed": {"1": "broke"}}
        if kind == "tool":
            return {"ok": True, "value": {"j": [msg["name"], msg["args"], msg["kwargs"]]}}
        return {"ok": False, "etype": "BudgetExceeded", "error": "spent"}

    code = """
print(ask('q', {'k': 1}))
out = ask_map('p', ['a', 'b', 'c'])
print(out[0], bool(out[1]), out[1].error, out[2])
print(lookup('x', n=2))
try:
    delegate('t', 'd')
except BudgetExceeded as exc:
    print('budget:', exc)
"""
    tools = [{"name": "lookup", "signature": "(key, n=1)", "doc": "Look something up."}]
    result = session(lambda sb: sb.run(code, on_call), tools=tools)
    lines = result.stdout.strip().splitlines()
    assert lines[0] == 'q|{"k": 1}'
    assert "1 of 3 items failed" in lines[1]
    assert lines[2] == "1 False broke 3"
    assert lines[3] == "['lookup', ['x'], {'n': 2}]"
    assert lines[4] == "budget: spent"
    assert seen == ["ask", "ask_map", "tool", "delegate"]


def test_calls_from_threads_run_concurrently():
    import asyncio

    active = peak = 0

    async def on_call(kind, msg):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"ok": True, "value": {"j": msg["prompt"].upper()}}

    code = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "with ThreadPoolExecutor(8) as pool:\n"
        "    print(list(pool.map(ask, ['a', 'b', 'c', 'd', 'e', 'f'])))"
    )
    result = session(lambda sb: sb.run(code, on_call))
    assert result.stdout.strip() == "['A', 'B', 'C', 'D', 'E', 'F']"
    assert peak >= 4


def test_helpers_chunk_grep_peek():
    code = """
parts = chunk('para one.\\n\\npara two.\\n\\npara three.', size=14)
print(parts)
print([len(g) for g in chunk(['aa', 'bb', 'cc', 'dd'], size=4)])
hits = grep(r'needle-\\d+', 'hay needle-42 hay', window=4)
print(hits[0].match, hits[0].start, repr(hits[0].snippet))
peek({'a': [1, 2, 3], 'b': 'text'})
"""
    out = session(lambda sb: sb.run(code)).stdout
    assert "['para one.\\n\\n', 'para two.\\n\\n', 'para three.']" in out
    assert "[2, 2]" in out
    assert "needle-42 4 'hay needle-42 hay'" in out
    assert "value: dict, 2 keys" in out


def test_snapshot_and_restore_round_trip():
    async def go():
        first = Sandbox(SandboxOptions())
        second = Sandbox(SandboxOptions())
        try:
            await first.start("ctx")
            await first.run(
                "import threading\n"
                "class Point:\n    def __init__(self, x):\n        self.x = x\n"
                "def twice(n):\n    return n * 2\n"
                "p = Point(4)\nindex = {'a': [1, 2]}\nlock = threading.Lock()"
            )
            state = await first.snapshot()
            rows = await first.inventory()
            await second.start(None)
            failed = await second.restore(state)
            out = await second.run("print(twice(p.x), index, context)")
            return state, rows, failed, out
        finally:
            await first.close()
            await second.close()

    state, rows, failed, out = run_async(go())
    assert set(state["defs"]) == {"Point", "twice"}
    assert "lock" in state["skipped"]
    assert {r["name"] for r in rows} >= {"p", "index", "twice"}
    assert failed == {}
    assert out.stdout.strip() == "8 {'a': [1, 2]} ctx"


def test_environment_is_scrubbed_by_default(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_KEY", "hunter2")
    code = "import os\nprint(os.environ.get('SUPER_SECRET_KEY'), os.environ.get('EXTRA'))"
    assert session(lambda sb: sb.run(code)).stdout.split() == ["None", "None"]
    options = SandboxOptions(pass_env=["SUPER_SECRET_KEY"], env={"EXTRA": "1"})
    assert session(lambda sb: sb.run(code), options=options).stdout.split() == ["hunter2", "1"]


def test_cells_run_in_a_private_working_directory():
    out = session(lambda sb: sb.run("import os\nopen('note.txt', 'w').write('x')\nprint(os.getcwd())")).stdout.strip()
    assert out != os.getcwd() and not os.path.exists(out)


@pytest.mark.skipif(os.name != "posix", reason="uses /usr/bin/env as a stand-in launcher")
def test_launcher_prefix_wraps_the_worker():
    options = SandboxOptions(launcher=["env", "RLM_WRAPPED=yes"], python=sys.executable, pass_env=[])
    out = session(lambda sb: sb.run("import os\nos.environ['RLM_WRAPPED']"), options=options).stdout
    assert out.strip() == "'yes'"


def test_missing_interpreter_is_a_clear_error():
    with pytest.raises(SandboxError, match="could not start"):
        session(lambda sb: sb.run("1"), options=SandboxOptions(python="/no/such/python"))


# ------------------------------------------------------------ start-up failures

# Real stderr from Docker 29.7.2 for the failures a launcher hits most often.
MISSING_IMAGE = (
    "Unable to find image 'ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0' locally\n"
    "docker: Error response from daemon: failed to resolve reference "
    '"ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0": ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0: not found\n'
    "\n"
    "Run 'docker run --help' for more information\n"
)
NO_SUCH_REPO = (
    "Unable to find image 'rlm-no-such-image-xyz:1' locally\n"
    "docker: Error response from daemon: pull access denied for rlm-no-such-image-xyz, "
    "repository does not exist or may require 'docker login'\n"
    "\n"
    "Run 'docker run --help' for more information\n"
)
BAD_FLAG = (
    "unknown flag: --bogus\n"
    "\n"
    "Usage:  docker run [OPTIONS] IMAGE [COMMAND] [ARG...]\n"
    "\n"
    "Run 'docker run --help' for more information\n"
)
NO_PYTHON = (
    "docker: Error response from daemon: failed to create task for container: failed to create shim task: "
    "OCI runtime create failed: runc create failed: unable to start container process: error during container "
    'init: exec: "python3": executable file not found in $PATH\n'
    "\n"
    "Run 'docker run --help' for more information\n"
)
DAEMON_DOWN = (
    "failed to connect to the docker API at unix:///Users/ishaan/.docker/run/docker.sock; check if the path "
    "is correct and if the daemon is running: dial unix /Users/ishaan/.docker/run/docker.sock: connect: "
    "no such file or directory\n"
)
OLD_DAEMON_DOWN = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n"

DOCKER_DOWN_HINT = (
    "Docker is not running; start Docker Desktop or the docker service, then check that `docker info` works"
)


def _missing(image):
    return (
        f"{image} is not available locally or in its registry; pull it with `docker pull {image}`, "
        "or choose another image with --sandbox docker:IMAGE"
    )


@pytest.mark.parametrize(
    "stderr, summary, hint",
    [
        (
            MISSING_IMAGE,
            "Unable to find image 'ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0' locally | docker: Error response "
            'from daemon: failed to resolve reference "ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0": '
            "ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0: not found",
            _missing("ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0"),
        ),
        (
            NO_SUCH_REPO,
            "Unable to find image 'rlm-no-such-image-xyz:1' locally | docker: Error response from daemon: pull "
            "access denied for rlm-no-such-image-xyz, repository does not exist or may require 'docker login'",
            _missing("rlm-no-such-image-xyz:1"),
        ),
        (BAD_FLAG, "unknown flag: --bogus", ""),
        (
            NO_PYTHON,
            NO_PYTHON.splitlines()[0],
            "the image has no python3 on its PATH; use an image with Python 3, or set sandbox.python to its interpreter",
        ),
        (DAEMON_DOWN, DAEMON_DOWN.strip(), DOCKER_DOWN_HINT),
        (OLD_DAEMON_DOWN, OLD_DAEMON_DOWN.strip(), DOCKER_DOWN_HINT),
    ],
    ids=["missing-image", "no-such-repo", "bad-flag", "no-python", "daemon-down", "old-daemon-down"],
)
def test_stderr_summary_and_startup_hint_for_real_docker_errors(stderr, summary, hint):
    assert _stderr_summary(stderr) == summary
    assert _startup_hint(stderr) == hint


def test_the_no_python_line_is_kept_whole():
    line = NO_PYTHON.splitlines()[0]
    assert len(line) == 263 and "…" not in _stderr_summary(NO_PYTHON)


def test_stderr_summary_of_nothing_is_empty():
    assert _stderr_summary("") == ""
    assert _stderr_summary("  \n\t\n   \n") == ""


def test_stderr_summary_keeps_the_last_three_lines_of_a_traceback():
    traceback = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "<string>", line 1, in <module>',
            '  File "/app/a.py", line 2, in run',
            "    step()",
            '  File "/app/b.py", line 3, in step',
            "    load()",
            '  File "/app/c.py", line 4, in load',
            "    open(path)",
            '  File "/app/d.py", line 5, in open',
            "FileNotFoundError: [Errno 2] No such file or directory: 'x'",
        ]
    )
    assert len(traceback.splitlines()) == 10
    assert _stderr_summary(traceback) == (
        "open(path) | File \"/app/d.py\", line 5, in open | FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    )


def test_stderr_summary_shortens_long_lines():
    summary = _stderr_summary("x" * 500)
    assert len(summary) == 300 and summary == "x" * 299 + "…"


def test_stderr_summary_drops_see_help_boilerplate():
    assert _stderr_summary("docker: 'bogus' is not a docker command.\nSee 'docker run --help'.\n") == (
        "docker: 'bogus' is not a docker command."
    )


def _start_error(options, context=None):
    """Start a sandbox that is expected to fail; return the SandboxError's message."""

    async def go():
        sandbox = Sandbox(options)
        try:
            await sandbox.start(context)
        except SandboxError as exc:
            return str(exc)
        finally:
            await sandbox.close()
        raise AssertionError("the sandbox started")

    return run_async(go())


def _fake_launcher(stderr, code):
    """A launcher that prints ``stderr`` and exits with ``code`` without reading its input, as docker does."""
    return [sys.executable, "-c", f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})"]


def test_a_failing_launcher_reports_its_stderr_and_a_hint():
    message = _start_error(SandboxOptions(launcher=_fake_launcher(MISSING_IMAGE, 125)))
    assert message.startswith("sandbox failed to start: exit code 125; Unable to find image")
    assert "not found. Hint: ghcr.io/ishaangupta-yb/rlm-ig-sandbox:0.2.0 is not available" in message
    assert "Run 'docker run --help'" not in message


def test_a_launcher_that_exits_at_once_is_always_a_failed_start():
    # The launcher exits without reading stdin, so writing the worker source and the init
    # message races its exit. A context larger than any pipe buffer makes the write hit the
    # closed pipe every time (on Linux the 32 KB source alone often fits in the buffer).
    # Every attempt must report the exit code, never a closed pipe.
    options = SandboxOptions(launcher=[sys.executable, "-c", "import sys; sys.exit(3)"])
    context = "x" * 1_000_000
    for attempt in range(25):
        message = _start_error(options, context)
        assert message.startswith("sandbox failed to start: exit code 3"), (attempt, message)
        assert "pipe closed" not in message, (attempt, message)


@pytest.mark.skipif(os.name != "posix", reason="uses an executable script as the interpreter")
def test_a_failed_start_without_a_launcher_gets_no_hint(tmp_path):
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({MISSING_IMAGE!r})\nsys.exit(125)\n")
    fake.chmod(0o755)
    message = _start_error(SandboxOptions(python=str(fake)))
    assert message.startswith("sandbox failed to start: exit code 125; Unable to find image")
    assert "Hint:" not in message


def test_a_crash_mid_run_gets_no_hint_and_restarts():
    async def body(sb):
        crashed = await sb.run("import os\nos._exit(7)")
        after = await sb.run("print(context)")
        return crashed, after

    crashed, after = session(body, context="ctx")
    assert crashed.restarted and "exit code 7" in crashed.error
    assert "Hint:" not in crashed.error
    assert after.stdout.strip() == "ctx"
