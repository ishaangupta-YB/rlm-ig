"""The ``docker`` sandbox preset against a real Docker daemon.

Skipped unless ``docker info`` succeeds. CI runs these with ``pytest -m docker``
after pulling the images, so pull time never counts against a cell timeout.
Locally, a missing image is pulled once; if that fails the tests are skipped,
except under CI, where they fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

import rlm_ig as rlm
from rlm_ig.models.scripted import Scripted
from rlm_ig.sandbox import Sandbox, SandboxOptions
from rlm_ig.sandbox.host import PLAIN_IMAGE, sandbox_image

from helpers import block, run_async

pytestmark = pytest.mark.docker

PRESET = f"docker:{PLAIN_IMAGE}"


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


if not _docker_works():
    pytest.skip("needs a working Docker daemon (`docker info` failed)", allow_module_level=True)


def _have(image: str, *, pull: bool) -> bool:
    if _docker("image", "inspect", image).returncode == 0:
        return True
    return pull and _docker("pull", image, timeout=600).returncode == 0


@pytest.fixture(scope="module")
def plain_image():
    if not _have(PLAIN_IMAGE, pull=True):
        message = f"could not pull {PLAIN_IMAGE}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return PLAIN_IMAGE


@pytest.fixture
def docker_cfg(cfg, plain_image):
    return cfg.replace(sandbox={"preset": PRESET}, cell_timeout=60)


def _containers(image: str) -> list[str]:
    return _docker("ps", "-q", "--filter", f"ancestor={image}").stdout.split()


def _wait_for(predicate, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


# ------------------------------------------------------------------ the preset


def test_run_completes_in_the_container_and_ask_map_crosses_the_pipe(docker_cfg):
    code = (
        "import os\n"
        "answers = ask_map('Name the letter.', ['a', 'b', 'c'])\n"
        "submit({'answers': answers, 'uid': os.getuid(), 'cwd': os.getcwd(), 'size': len(context)})"
    )
    model = Scripted([block(code)], asks=lambda prompt: "letter")
    result = rlm.run("Do the task.", "x" * 5000, model=model, config=docker_cfg)
    assert result.ok, result.error
    assert result.answer == {"answers": ["letter"] * 3, "uid": 65534, "cwd": "/tmp", "size": 5000}


def test_the_container_has_no_network(docker_cfg):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "    outcome = 'connected'\n"
        "except OSError as exc:\n"
        "    outcome = 'blocked: ' + type(exc).__name__\n"
        "submit(outcome)"
    )
    result = rlm.run("Do the task.", model=Scripted([block(code)]), config=docker_cfg)
    assert result.ok and result.answer.startswith("blocked: "), result.answer


def test_tmp_is_writable_and_the_root_filesystem_is_not(docker_cfg):
    code = (
        "with open('/tmp/probe.txt', 'w') as handle:\n"
        "    handle.write('ok')\n"
        "try:\n"
        "    open('/probe.txt', 'w')\n"
        "    root = 'writable'\n"
        "except OSError as exc:\n"
        "    root = type(exc).__name__\n"
        "with open('probe2.txt', 'w') as handle:\n"  # relative to the working directory, /tmp
        "    handle.write('ok')\n"
        "submit({'tmp': open('/tmp/probe.txt').read(), 'cwd_file': open('/tmp/probe2.txt').read(), 'root': root})"
    )
    result = rlm.run("Do the task.", model=Scripted([block(code)]), config=docker_cfg)
    assert result.ok, result.error
    assert result.answer["tmp"] == "ok" and result.answer["cwd_file"] == "ok"
    assert result.answer["root"] in ("OSError", "PermissionError")


def test_a_runaway_cell_restarts_the_worker_and_leaves_no_container_behind(plain_image):
    before = set(_containers(plain_image))

    async def go():
        sandbox = Sandbox(SandboxOptions(preset=PRESET, cell_timeout=2))
        try:
            await sandbox.start("ctx")
            first = await sandbox.run("marker = 1\nwhile True:\n    pass")
            # the worker was replaced: one live container of ours, the new one
            live = set(_containers(plain_image)) - before
            second = await sandbox.run("print(context, 'marker' in globals())")
            return first, live, second
        finally:
            await sandbox.close()

    first, live, second = run_async(go())
    assert first.restarted and "It was restarted" in first.error
    assert len(live) <= 1
    assert second.stdout.strip() == "ctx False"
    assert _wait_for(lambda: not (set(_containers(plain_image)) - before)), "a container was left running"


def test_closing_the_sandbox_removes_its_container(plain_image):
    before = set(_containers(plain_image))

    async def go():
        sandbox = Sandbox(SandboxOptions(preset=PRESET))
        await sandbox.start(None)
        running = set(_containers(plain_image)) - before
        await sandbox.close()
        return running

    assert len(run_async(go())) == 1
    assert _wait_for(lambda: not (set(_containers(plain_image)) - before))


# ------------------------------------------------------------ the sandbox image


def test_default_preset_image_has_the_documented_libraries(cfg):
    image = sandbox_image()
    if not _have(image, pull=False):
        pytest.skip(f"{image} is not present locally (CI builds it from docker/sandbox.Dockerfile first)")
    code = (
        "import os, numpy, pandas, dateutil, regex\n"
        "submit({'uid': os.getuid(), 'libs': sorted(m.__name__ for m in (numpy, pandas, dateutil, regex))})"
    )
    config = cfg.replace(sandbox={"preset": "docker"}, cell_timeout=60)
    result = rlm.run("Do the task.", model=Scripted([block(code)]), config=config)
    assert result.ok, result.error
    assert result.answer == {"uid": 65534, "libs": ["dateutil", "numpy", "pandas", "regex"]}
