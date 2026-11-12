from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

import rlm_ig as rlm
from fakes.openai_server import OpenAIServer, last_user_text, system_text
from helpers import block, run_async
from rlm_ig.models.opencode import OpenCode, _usage, project_config

OPENCODE = shutil.which("opencode")
needs_opencode = pytest.mark.skipif(OPENCODE is None, reason="the opencode CLI is not installed")


def test_generated_config_denies_every_tool():
    config = project_config("PROMPT")
    assert config["permission"] == {"*": "deny"}
    assert config["agent"]["rlm"] == {"mode": "primary", "prompt": "PROMPT", "permission": {"*": "deny"}}


def test_usage_mapping_counts_cached_tokens_as_input():
    usage = _usage({"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 100, "write": 30}}, 0.25)
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cost_usd) == (140, 7, 100, 0.25)
    assert _usage(None, None).cost_usd is None


def test_model_names_need_a_provider_prefix():
    with pytest.raises(rlm.ModelError, match="provider/model"):
        OpenCode(model="sonnet")
    assert OpenCode(model="anthropic/claude-sonnet-5-5").model_ref() == {
        "providerID": "anthropic",
        "modelID": "claude-sonnet-5-5",
    }
    assert OpenCode().model_ref() is None


def test_missing_binary_is_explained():
    model = OpenCode(settings={"command": "definitely-not-opencode"})
    with pytest.raises(rlm.ModelError, match="was not found"):
        run_async(model.complete("s", "hi"))


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A fake model API plus an opencode user config that routes ``mock/m1`` to it."""

    def start(reply):
        server = OpenAIServer(reply)
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Mock",
            "options": {"baseURL": server.url, "apiKey": "test-key"},
            "models": {"m1": {"name": "m1"}},
        }
        config = tmp_path / "opencode-user.json"
        config.write_text(json.dumps({"$schema": "https://opencode.ai/config.json", "provider": {"mock": provider}}))
        monkeypatch.setenv("OPENCODE_CONFIG", str(config))
        return server

    return start


@needs_opencode
@pytest.mark.parametrize("mode", ["server", "run"])
def test_conversation_through_real_opencode(upstream, mode):
    def reply(body):
        turns = sum(1 for m in body["messages"] if m["role"] == "user")
        return f"turn {turns}: {last_user_text(body).strip()}"

    with upstream(reply) as api:
        model = OpenCode(model="mock/m1", settings={"mode": mode})

        async def go():
            chat = model.chat("RLM-SYSTEM-MARKER follow the protocol.")
            try:
                return await chat.send("alpha"), await chat.send("beta")
            finally:
                await chat.close()
                await model.close()

        first, second = run_async(go())
    assert first.text == "turn 1: alpha" and second.text == "turn 2: beta"
    assert second.usage.input_tokens == 120 and second.usage.output_tokens == 8
    chat_requests = [b for b in api.requests if "RLM-SYSTEM-MARKER" in system_text(b)]
    assert len(chat_requests) == 2 and len(api.requests) == 2  # no title-generation or other side calls
    for body in chat_requests:
        assert not body.get("tools")  # every opencode tool is denied
        assert "You are opencode" not in system_text(body)  # its own agent prompt is replaced
    assert [m["role"] for m in chat_requests[1]["messages"]] == ["system", "user", "assistant", "user"]
    assert model.info["mode"] == mode


@needs_opencode
def test_full_run_with_sub_calls_through_the_server(upstream, cfg):
    def reply(body):
        text = last_user_text(body)
        if "You are a component inside a larger program" in system_text(body):
            return "PONG" if "ping" in text else "NOT FOUND"
        if "## Task" in text:
            return block("answers = ask_map(['ping one', 'ping two', 'other'])\nprint(answers)")
        return block("submit(answers)")

    with upstream(reply):
        result = rlm.run("Ping twice.", model="opencode:mock/m1", config=cfg)
    assert result.ok and result.answer == ["PONG", "PONG", "NOT FOUND"]
    assert result.usage.calls == 5 and result.usage.input_tokens == 600
    listing = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert not any(" serve --hostname 127.0.0.1" in line and "opencode" in line for line in listing.splitlines())


@needs_opencode
def test_upstream_errors_surface_as_model_errors(upstream):
    from fakes.openai_server import HTTPStatus

    def reply(body):
        raise HTTPStatus(401, "Incorrect API key provided")

    with upstream(reply):
        model = OpenCode(model="mock/m1", settings={"mode": "server"})

        async def go():
            try:
                return await model.complete("s", "hi")
            finally:
                await model.close()

        with pytest.raises(rlm.ModelError, match="opencode reported an error"):
            run_async(go())


def test_spec_strings_resolve_to_this_backend(cfg):
    from rlm_ig.models import resolve

    assert resolve("opencode", cfg).spec == "opencode"
    assert resolve("opencode:anthropic/claude-haiku-5-5", cfg).model_name == "anthropic/claude-haiku-5-5"
    assert os.name  # keep the import used on every platform
