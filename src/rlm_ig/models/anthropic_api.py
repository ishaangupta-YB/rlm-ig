"""The Anthropic Messages API, through the official SDK.

Needs the optional dependency: ``pip install "rlm-ig[anthropic]"``.

Requests stream (long replies would otherwise run into HTTP timeouts) and use
prompt caching: one breakpoint on the system prompt, which every agent in a
run shares, and one on the newest message, so each turn re-reads the growing
conversation from cache instead of paying for it again.
"""

from __future__ import annotations

from typing import Any

from ..budget import Usage
from ..errors import ModelError
from ..text import looks_runaway
from . import pricing as pricing_table
from .base import Chat, Model, Reply, StatelessChat, TextSink

# Models that support server-side refusal fallbacks. With the option on, a
# request a safety classifier declines is retried on the fallback model
# Anthropic recommends for that refusal category, inside the same call.
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

CACHE = {"type": "ephemeral"}


def _sdk():
    try:
        import anthropic
    except ImportError:
        raise ModelError(
            'the anthropic backend needs the Anthropic SDK: pip install "rlm-ig[anthropic]" (or pip install anthropic)'
        ) from None
    return anthropic


class AnthropicAPI(Model):
    parallel = 8
    window_chars = 400_000
    reports_cost = True

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "",
        settings: dict[str, Any] | None = None,
        pricing: dict | None = None,
    ):
        settings = dict(settings or {})
        self.model_name = model or settings.get("model") or ""
        if not self.model_name:
            raise ModelError("anthropic needs a model id, for example anthropic:claude-opus-5-5")
        self.spec = f"{provider}:{self.model_name}"
        self.max_tokens = int(settings.get("max_tokens", 32000))
        #: extra request fields, e.g. {"output_config": {"effort": "low"}} or {"thinking": {...}}
        self.params = dict(settings.get("params") or {})
        fallback = settings.get("refusal_fallback")
        self.refusal_fallback = (self.model_name in FALLBACK_MODELS) if fallback is None else bool(fallback)
        self.compact_at = int(settings.get("compact_at", 600_000))
        self.max_reply_chars = int(settings.get("max_reply_chars", 80_000))
        self.window_chars = int(settings.get("window_chars", self.window_chars))
        self.parallel = int(settings.get("parallel", self.parallel))
        self.pricing = pricing or {}
        self.reports_cost = pricing_table.lookup(self.model_name, self.pricing) is not None
        self._client_options: dict[str, Any] = {
            "max_retries": int(settings.get("retries", 4)),
            "timeout": float(settings.get("timeout", 600)),
        }
        # Leave credentials to the SDK (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
        # or an `ant auth login` profile) unless the config names something.
        if settings.get("api_key"):
            self._client_options["api_key"] = settings["api_key"]
        elif settings.get("api_key_env"):
            import os

            key = os.environ.get(settings["api_key_env"])
            if not key:
                raise ModelError(f"{self.spec} needs an API key in ${settings['api_key_env']}")
            self._client_options["api_key"] = key
        if settings.get("base_url"):
            self._client_options["base_url"] = settings["base_url"]
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None:
            anthropic = _sdk()
            try:
                self._client = anthropic.AsyncAnthropic(**self._client_options)
            except anthropic.AnthropicError as exc:
                raise ModelError(
                    f"{self.spec}: no usable Anthropic credentials ({exc}). Set ANTHROPIC_API_KEY or run `ant auth login`."
                ) from None
        return self._client

    def chat(self, system: str) -> Chat:
        return _AnthropicChat(self, system)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - shutting down
                pass

    def usage(self, raw: Any) -> Usage:
        fresh = int(getattr(raw, "input_tokens", 0) or 0)
        written = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
        read = int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        output = int(getattr(raw, "output_tokens", 0) or 0)
        cost = pricing_table.estimate(
            self.model_name, self.pricing, fresh_input=fresh, output=output, cache_read=read, cache_write=written
        )
        return Usage(input_tokens=fresh + written + read, output_tokens=output, cached_tokens=read, cost_usd=cost)


class _AnthropicChat(StatelessChat):
    def __init__(self, model: AnthropicAPI, system: str):
        super().__init__(system, compact_at=model.compact_at)
        self.model = model

    def _payload(self) -> dict[str, Any]:
        # History is text only, so no thinking blocks are replayed and trimming
        # old output never invalidates anything. The cache breakpoint rides on
        # the newest message.
        messages: list[dict[str, Any]] = [dict(m) for m in self.messages]
        last = messages[-1]
        messages[-1] = {
            "role": last["role"],
            "content": [{"type": "text", "text": last["content"], "cache_control": CACHE}],
        }
        return {
            "model": self.model.model_name,
            "max_tokens": self.model.max_tokens,
            "system": [{"type": "text", "text": self.system, "cache_control": CACHE}],
            "messages": messages,
            **self.model.params,
        }

    async def _request(self, on_text: TextSink | None) -> Reply:
        anthropic = _sdk()
        client = self.model.client()
        payload = self._payload()
        try:
            if self.model.refusal_fallback:
                try:
                    return await self._stream(
                        client.beta.messages, {**payload, "betas": [FALLBACK_BETA], "fallbacks": "default"}, on_text
                    )
                except (TypeError, anthropic.BadRequestError) as exc:
                    # An SDK or endpoint that does not know the option: carry on without it.
                    if isinstance(exc, anthropic.BadRequestError) and "fallback" not in str(exc).lower():
                        raise
                    self.model.refusal_fallback = False
            return await self._stream(client.messages, payload, on_text)
        except anthropic.AuthenticationError as exc:
            raise ModelError(
                f"{self.model.spec}: authentication failed ({exc.message}). Check ANTHROPIC_API_KEY."
            ) from None
        except anthropic.PermissionDeniedError as exc:
            raise ModelError(f"{self.model.spec}: permission denied ({exc.message})") from None
        except anthropic.NotFoundError as exc:
            raise ModelError(f"{self.model.spec}: not found ({exc.message}). Check the model id.") from None
        except anthropic.BadRequestError as exc:
            raise ModelError(f"{self.model.spec}: the request was rejected ({exc.message})") from None
        except anthropic.RateLimitError as exc:
            raise ModelError(f"{self.model.spec}: rate limited after retries ({exc.message})", retryable=True) from None
        except anthropic.APIStatusError as exc:
            raise ModelError(
                f"{self.model.spec}: API error {exc.status_code} ({exc.message})", retryable=exc.status_code >= 500
            ) from None
        except anthropic.APIConnectionError as exc:
            raise ModelError(f"{self.model.spec}: could not reach the API ({exc})", retryable=True) from None

    async def _stream(self, endpoint: Any, payload: dict[str, Any], on_text: TextSink | None) -> Reply:
        pieces: list[str] = []
        size = checked = 0
        async with endpoint.stream(**payload) as stream:
            async for text in stream.text_stream:
                pieces.append(text)
                size += len(text)
                if on_text:
                    on_text(text)
                if size - checked >= 1000:
                    checked = size
                    so_far = "".join(pieces)
                    if looks_runaway(so_far, max_chars=self.model.max_reply_chars):
                        # Leaving the block closes the connection, which stops generation.
                        return Reply(
                            text=so_far, usage=Usage(output_tokens=size // 4), model=self.model.spec, stop="runaway"
                        )
            message = await stream.get_final_message()
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ModelError(f"{self.model.spec}: the model declined the request (refusal category: {category})")
        text = "".join(block.text for block in message.content if block.type == "text")
        return Reply(
            text=text,
            usage=self.model.usage(message.usage),
            model=getattr(message, "model", None) or self.model.spec,
            stop=message.stop_reason,
        )
