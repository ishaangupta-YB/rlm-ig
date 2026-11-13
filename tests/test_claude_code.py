from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rlm_ig as rlm
from rlm_ig.models.claude_code import ClaudeCode

from helpers import block, run_async

FAKE = str(Path(__file__).parent / "fakes" / "fake_claude.py")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the fake CLI is a POSIX executable script")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Point the backend at the fake CLI; returns (make_model, read_log)."""
    script, state, log = tmp_path / "script.json", tmp_path / "state", tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_SCRIPT", str(script))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("FAKE_LOG", str(log))

    def make(replies, **settings):
        script.write_text(json.dumps(replies))
        return ClaudeCode(model="sonnet", settings={"command": FAKE, **settings})

    def read_log():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return make, read_log


def test_multi_turn_conversation_in_one_process(fake):
    make, read_log = fake
    model = make(["first reply", "second reply"])

    async def go():
        chat = model.chat("SYSTEM TEXT")
        try:
            return await chat.send("one"), await chat.send("two")
        finally:
            await chat.close()

    first, second = run_async(go())
    assert (first.text, second.text) == ("first reply", "second reply")
    log = read_log()
    assert [e["user"] for e in log if "user" in e] == ["one", "two"]
    assert sum("argv" in e for e in log) == 1  # one process served both turns
    assert log[0]["system"] == "SYSTEM TEXT"


def test_flags_isolate_the_cli(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")
    make, read_log = fake
    model = make(["ok"], effort="low", args=["--fallback-model", "haiku"])
    run_async(model.complete("sys", "hi"))
    start = read_log()[0]
    argv = start["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--fallback-model"):
        assert flag in argv
    assert start["env_entrypoint"] is None  # not inherited from a parent Claude Code session
    assert start["cwd"] != os.getcwd()  # runs in a throwaway directory


def test_usage_and_cost_are_per_turn(fake):
    make, _ = fake
    model = make(["a" * 40, "b" * 80])

    async def go():
        chat = model.chat("s")
        try:
            return await chat.send("1"), await chat.send("2")
        finally:
            await chat.close()

    first, second = run_async(go())
    assert first.usage.input_tokens == 43 and first.usage.cached_tokens == 0 and first.usage.output_tokens == 10
    assert second.usage.input_tokens == 143 and second.usage.cached_tokens == 100 and second.usage.output_tokens == 20
    assert first.usage.cost_usd == pytest.approx(0.01) and second.usage.cost_usd == pytest.approx(0.01)
    assert model.info == {"model": "fake-model-1", "auth": "none", "version": "0.0.0-fake"}


def test_text_is_streamed_to_the_sink(fake):
    make, _ = fake
    model = make(["streamed in several pieces"])
    pieces: list[str] = []

    async def go():
        chat = model.chat("s")
        try:
            return await chat.send("go", on_text=pieces.append)
        finally:
            await chat.close()

    assert run_async(go()).text == "streamed in several pieces"
    assert len(pieces) > 1 and "".join(pieces) == "streamed in several pieces"


def test_crash_is_recovered_by_replaying_the_transcript(fake):
    make, read_log = fake
    model = make(["reply one", "@@crash", "reply after restart"])

    async def go():
        chat = model.chat("s")
        try:
            return await chat.send("message one"), await chat.send("message two")
        finally:
            await chat.close()

    first, second = run_async(go())
    assert second.text == "reply after restart"
    users = [e["user"] for e in read_log() if "user" in e]
    assert users[:2] == ["message one", "message two"]
    replay = users[2]
    assert "being resumed in a new session" in replay
    assert "message one" in replay and "reply one" in replay and replay.rstrip().endswith("message two")


def test_stalled_turn_is_abandoned_and_retried(fake):
    make, _ = fake
    model = make(["@@stall", "recovered"], stall_timeout=0.5)
    reply = run_async(model.complete("s", "hello"))
    assert reply.text == "recovered"


def test_retries_are_bounded(fake):
    make, _ = fake
    model = make(["@@crash", "@@crash", "@@crash", "never reached"], retries=2)
    with pytest.raises(rlm.ModelError, match="simulated crash"):
        run_async(model.complete("s", "hello"))


def test_runaway_output_is_cut_off_and_the_run_recovers(fake, cfg):
    make, read_log = fake
    model = make(["@@runaway", block("submit('recovered')")])
    notes: list[str] = []
    result = rlm.run(
        "t", model=model, config=cfg, on_event=lambda e: notes.append(e["message"]) if e.kind == "note" else None
    )
    assert result.ok and result.answer == "recovered"
    assert any("ran away" in n for n in notes)
    replay = [e["user"] for e in read_log() if "user" in e][-1]
    assert "No code block was found" in replay and len(replay) < 20000  # the runaway reply was shortened


def test_error_result_is_a_model_error(fake):
    make, _ = fake
    model = make(["@@error:Invalid API key · Please run /login"])
    with pytest.raises(rlm.ModelError, match="Invalid API key"):
        run_async(model.complete("s", "hello"))


def test_missing_binary_is_explained():
    model = ClaudeCode(settings={"command": "definitely-not-claude"})
    with pytest.raises(rlm.ModelError, match="was not found"):
        run_async(model.complete("s", "hello"))


def test_full_run_through_the_cli_backend(fake, cfg):
    make, _ = fake
    model = make([block("print(len(context))"), block("submit(context.upper())")])
    result = rlm.run("Shout it.", "quiet", model=model, config=cfg)
    assert result.ok and result.answer == "QUIET"
    assert result.usage.calls == 2 and result.usage.cost_usd == pytest.approx(0.02)
    assert result.model == "claude-code:sonnet"


def test_no_cli_processes_are_left_behind(fake, cfg):
    def running() -> int:
        listing = subprocess.run(["ps", "-ewwo", "args"], capture_output=True, text=True).stdout
        return sum(FAKE in line.split()[:2] for line in listing.splitlines())

    make, _ = fake
    model = make([block("submit(1)")])
    before = running()
    rlm.run("t", model=model, config=cfg)
    assert running() == before


def test_spec_strings_resolve_to_this_backend(cfg):
    from rlm_ig.models import resolve

    assert resolve("claude-code", cfg).spec == "claude-code"
    assert resolve("claude-code:opus", cfg).model_name == "opus"
    assert resolve("claude:haiku", cfg).spec == "claude-code:haiku"
    assert sys.executable  # keep the import used on every platform
