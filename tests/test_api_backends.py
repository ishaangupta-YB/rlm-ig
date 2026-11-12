from __future__ import annotations

import sys

import pytest

import rlm_ig as rlm
from fakes.anthropic_server import AnthropicServer
from fakes.openai_server import HTTPStatus, OpenAIServer, last_user_text, system_text
from rlm_ig.models import pricing, resolve
from rlm_ig.models.base import StatelessChat
from rlm_ig.models.command import CommandModel
from rlm_ig.models.openai_compat import OpenAICompatible

from helpers import block, run_async


def converse(model, *messages, system="SYS"):
    async def go():
        chat = model.chat(system)
        try:
            return [await chat.send(m) for m in messages]
        finally:
            await chat.close()
            await model.close()

    return run_async(go())


# ----------------------------------------------------------- OpenAI-compatible


def local(server, **settings):
    return OpenAICompatible("local", "test-model", {"base_url": server.url, "api_key": "k-123", **settings})


def test_openai_chat_resends_history_and_reads_usage():
    usage = {"prompt_tokens": 50, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}}
    with OpenAIServer(lambda body: f"echo:{last_user_text(body)}", usage) as api:
        first, second = converse(local(api), "one", "two")
    assert (first.text, second.text) == ("echo:one", "echo:two")
    assert [m["role"] for m in api.requests[1]["messages"]] == ["system", "user", "assistant", "user"]
    assert system_text(api.requests[0]) == "SYS" and api.requests[0]["model"] == "test-model"
    assert api.headers[0]["authorization"] == "Bearer k-123"
    assert (second.usage.input_tokens, second.usage.output_tokens, second.usage.cached_tokens) == (50, 7, 40)
    assert second.usage.cost_usd is None  # unknown model, no price table entry


def test_openai_params_headers_and_reported_cost():
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0123}
    with OpenAIServer(lambda body: "ok", usage) as api:
        model = local(api, params={"temperature": 0, "max_tokens": 99}, headers={"X-Title": "rlm"})
        (reply,) = converse(model, "hi")
    assert api.requests[0]["temperature"] == 0 and api.requests[0]["max_tokens"] == 99
    assert api.headers[0]["x-title"] == "rlm"
    assert reply.usage.cost_usd == pytest.approx(0.0123)


def test_openai_cost_is_estimated_from_a_price_table():
    usage = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 100_000,
        "prompt_tokens_details": {"cached_tokens": 400_000},
    }
    with OpenAIServer(lambda body: "ok", usage) as api:
        model = OpenAICompatible(
            "local", "test-model", {"base_url": api.url, "api_key": "k"},
            pricing={"test-model": {"input": 2.0, "output": 10.0, "cached": 0.5}},
        )  # fmt: skip
        (reply,) = converse(model, "hi")
    assert reply.usage.cost_usd == pytest.approx(0.6 * 2.0 + 0.4 * 0.5 + 0.1 * 10.0)
    assert model.reports_cost


def test_openai_retries_transient_errors_then_succeeds():
    calls = {"n": 0}

    def reply(body):
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPStatus(503, "overloaded", {"retry-after": "0"})
        return "third time lucky"

    with OpenAIServer(reply) as api:
        (out,) = converse(local(api), "hi")
    assert out.text == "third time lucky" and calls["n"] == 3


def test_openai_auth_errors_fail_fast_with_a_hint():
    calls = {"n": 0}

    def reply(body):
        calls["n"] += 1
        raise HTTPStatus(401, "Incorrect API key provided")

    with OpenAIServer(reply) as api:
        with pytest.raises(rlm.ModelError, match=r"HTTP 401: Incorrect API key provided\. Check the API key"):
            converse(local(api), "hi")
    assert calls["n"] == 1


def test_failed_request_does_not_poison_the_history():
    state = {"fail": True}

    def reply(body):
        if state["fail"]:
            raise HTTPStatus(400, "bad request")
        return f"{len(body['messages'])} messages"

    with OpenAIServer(reply) as api:
        model = local(api)

        async def go():
            chat = model.chat("s")
            with pytest.raises(rlm.ModelError):
                await chat.send("first try")
            state["fail"] = False
            out = await chat.send("second try")
            await model.close()
            return out

        assert run_async(go()).text == "2 messages"  # system + the one user message that succeeded


def test_presets_and_key_handling(monkeypatch, cfg):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    model = resolve("openai:gpt-test", cfg)
    assert model.base_url == "https://api.openai.com/v1"
    with pytest.raises(rlm.ModelError, match=r"\$OPENAI_API_KEY"):
        model.api_key()
    assert resolve("openrouter:vendor/model", cfg).params == {"usage": {"include": True}}
    assert resolve("ollama:qwen3:8b", cfg).model_name == "qwen3:8b" and resolve("ollama:x", cfg).api_key() is None
    assert resolve("gpt-test-mini", cfg).spec == "openai:gpt-test-mini"
    with pytest.raises(rlm.ModelError, match="needs a model id"):
        resolve("openai", cfg)


def test_custom_provider_from_config(cfg):
    with OpenAIServer(lambda body: block("submit(context[::-1])")) as api:
        config = cfg.replace(providers={"lab": {"kind": "openai", "base_url": api.url, "api_key": "x"}})
        result = rlm.run("Reverse it.", "abc", model="lab:tiny", config=config)
    assert result.answer == "cba" and result.model == "lab:tiny"


def test_unknown_provider_is_a_config_error(cfg):
    with pytest.raises(rlm.ConfigError, match="unknown model"):
        resolve("nonsense:model", cfg)
    with pytest.raises(rlm.ConfigError, match="kind"):
        resolve("lab:m", cfg.replace(providers={"lab": {"kind": "wrong"}}))


# --------------------------------------------------------------- compaction


def test_old_output_is_trimmed_once_the_transcript_grows():
    class Recorder(StatelessChat):
        async def _request(self, on_text):
            from rlm_ig.models.base import Reply

            return Reply("ok")

    chat = Recorder("system", compact_at=20_000)

    async def go():
        for i in range(12):
            await chat.send(f"output {i}: " + "x" * 3000)

    run_async(go())
    users = [m["content"] for m in chat.messages if m["role"] == "user"]
    assert users[0].startswith("output 0") and len(users[0]) > 3000  # the task message is never touched
    assert "trimmed to save context" in users[1] and len(users[1]) < 800
    assert all(len(u) > 3000 for u in users[-3:])  # recent turns stay whole
    assert sum(len(m["content"]) for m in chat.messages) < 20_000 + 3200


# ------------------------------------------------------------------ pricing


def test_builtin_prices_and_overrides():
    assert pricing.lookup("claude-opus-5-5", None) == (4.0, 20.0, 0.20)
    assert pricing.lookup("claude-haiku-5-5", None) == (0.10, 0.50, pytest.approx(0.01))
    assert pricing.lookup("anthropic/claude-sonnet-5-5", None) == (2.0, 10.0, 0.10)
    assert pricing.lookup("claude-opus-5-5", {"claude-opus-5-5": {"input": 1, "output": 2}}) == (1.0, 2.0, 0.1)
    assert pricing.lookup("mystery-model", None) is None
    cost = pricing.estimate(
        "claude-opus-5-5", None, fresh_input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write=1_000_000
    )
    assert cost == pytest.approx(4.0 + 20.0 + 0.20 + 5.0)


# ---------------------------------------------------------------- Anthropic

anthropic = pytest.importorskip("anthropic")


def claude(server, model="claude-haiku-5-5", **settings):
    from rlm_ig.models.anthropic_api import AnthropicAPI

    return AnthropicAPI("anthropic", model, {"base_url": server.url, "api_key": "sk-test", "retries": 0, **settings})


def test_anthropic_streams_caches_and_accounts():
    with AnthropicServer(lambda body: f"reply to {body['messages'][-1]['content'][0]['text']}") as api:
        pieces: list[str] = []
        model = claude(api)

        async def go():
            chat = model.chat("SYSTEM PROMPT")
            try:
                return await chat.send("first", on_text=pieces.append), await chat.send("second")
            finally:
                await model.close()

        first, second = run_async(go())
    assert first.text == "reply to first" and "".join(pieces) == "reply to first"
    body = api.requests[1]
    assert body["system"] == [{"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}]
    assert (
        body["stream"] is True and body["max_tokens"] == 32000 and "thinking" not in body and "temperature" not in body
    )
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][0]["content"] == "first" and body["messages"][1]["content"] == "reply to first"
    assert body["messages"][2]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }  # breakpoint on the newest message only
    assert api.paths[0] == "/v1/messages" and "fallbacks" not in body  # Haiku 5.5 has no server-side fallback
    assert (second.usage.input_tokens, second.usage.cached_tokens) == (1312, 1000)
    expected = (12 * 0.10 + 300 * 0.10 * 1.25 + 1000 * 0.01 + second.usage.output_tokens * 0.50) / 1e6
    assert second.usage.cost_usd == pytest.approx(expected)


def test_anthropic_refusal_fallback_is_on_for_supporting_models_and_degrades_cleanly():
    with AnthropicServer(lambda body: "fine") as api:
        (reply,) = converse(claude(api, model="claude-opus-5-5"), "hi")
    assert reply.text == "fine"
    assert api.requests[0]["fallbacks"] == "default" and api.paths[0].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in api.headers[0]["anthropic-beta"]

    def picky(body):
        if "fallbacks" in body:
            return {"status": 400, "message": "fallbacks: Extra inputs are not permitted"}
        return "plain worked"

    with AnthropicServer(picky) as api:
        model = claude(api, model="claude-opus-5-5")
        first, second = converse(model, "a", "b")
    assert (first.text, second.text) == ("plain worked", "plain worked")
    assert len(api.requests) == 3  # one rejected attempt, then the option stays off

    with AnthropicServer(lambda body: "ok") as api:
        converse(claude(api, model="claude-opus-5-5", refusal_fallback=False), "hi")
    assert "fallbacks" not in api.requests[0]


def test_anthropic_refusals_and_errors_are_model_errors():
    with AnthropicServer(lambda body: {"text": "", "stop_reason": "refusal"}) as api:
        with pytest.raises(rlm.ModelError, match="declined the request"):
            converse(claude(api), "hi")
    with AnthropicServer(lambda body: {"status": 401, "message": "invalid x-api-key"}) as api:
        with pytest.raises(rlm.ModelError, match="authentication failed"):
            converse(claude(api), "hi")
    with AnthropicServer(lambda body: {"status": 404, "message": "model: nope"}) as api:
        with pytest.raises(rlm.ModelError, match="Check the model id"):
            converse(claude(api), "hi")


def test_anthropic_params_pass_through_and_runaway_is_cut():
    with AnthropicServer(lambda body: "ok") as api:
        converse(claude(api, params={"output_config": {"effort": "low"}}, max_tokens=5000), "hi")
    assert api.requests[0]["output_config"] == {"effort": "low"} and api.requests[0]["max_tokens"] == 5000
    with AnthropicServer(lambda body: "start " + "<br>\n" * 4000) as api:
        (reply,) = converse(claude(api), "hi")
    assert reply.stop == "runaway" and len(reply.text) < 20000


def test_full_run_on_the_anthropic_backend(cfg):
    def reply(body):
        text = body["messages"][-1]["content"][0]["text"]
        system = body["system"][0]["text"]
        if "component inside a larger program" in system:
            return "4"
        return block("print(ask('2+2?'))") if "## Task" in text else block("submit(int(ask('2+2?')))")

    with AnthropicServer(reply) as api:
        config = cfg.replace(providers={"anthropic": {"base_url": api.url, "api_key": "sk-test"}})
        result = rlm.run("Add.", model="anthropic:claude-haiku-5-5", config=config)
    assert result.ok and result.answer == 4 and result.usage.calls == 4 and result.usage.cost_usd > 0
    assert resolve("claude-opus-5-5", cfg).spec == "anthropic:claude-opus-5-5"


# -------------------------------------------------------------- command model


def test_command_model_runs_a_program_per_turn(tmp_path):
    script = tmp_path / "echo_model.py"
    script.write_text(
        "import json, sys\n"
        "prompt = sys.stdin.read()\n"
        "users = prompt.count('### USER')\n"
        "print(json.dumps({'out': {'text': f'model={sys.argv[1]} users={users} sys={prompt.splitlines()[0]}'}}))\n"
    )
    model = CommandModel("mycli", "m9", {"command": [sys.executable, str(script), "{model}"], "json_path": "out.text"})
    first, second = converse(model, "a", "b", system="SYSLINE")
    assert first.text == "model=m9 users=1 sys=SYSLINE" and second.text == "model=m9 users=2 sys=SYSLINE"
    assert model.spec == "mycli:m9" and first.usage.cost_usd is None


def test_command_model_errors(tmp_path):
    bad = CommandModel(
        "x", "", {"command": [sys.executable, "-c", "import sys; sys.stderr.write('kaput'); sys.exit(4)"]}
    )
    with pytest.raises(rlm.ModelError, match="exited with code 4: kaput"):
        converse(bad, "hi")
    with pytest.raises(rlm.ModelError, match="needs command"):
        CommandModel("x", "", {})
    missing = CommandModel("x", "", {"command": ["no-such-program-xyz"]})
    with pytest.raises(rlm.ModelError, match="not found on PATH"):
        converse(missing, "hi")
