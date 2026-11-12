"""Any OpenAI-compatible chat-completions endpoint, with no dependencies.

Covers OpenAI itself, OpenRouter, Gemini's compatibility endpoint, Groq,
DeepSeek, Ollama and other local servers, and anything else you describe in a
``[providers.<name>]`` table with ``kind = "openai"``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any

from ..budget import Usage
from ..errors import ModelError
from . import pricing as pricing_table
from .base import Chat, Model, Reply, StatelessChat, TextSink

# provider -> (base URL, environment variable holding the key)
PRESETS: dict[str, tuple[str, str | None]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenAICompatible(Model):
    parallel = 8
    window_chars = 400_000

    def __init__(
        self,
        provider: str = "openai",
        model: str = "",
        settings: dict[str, Any] | None = None,
        pricing: dict | None = None,
    ):
        settings = dict(settings or {})
        self.provider = provider
        self.model_name = model or settings.get("model") or ""
        if not self.model_name:
            raise ModelError(f"{provider} needs a model id, for example {provider}:<model-id>")
        self.spec = f"{provider}:{self.model_name}"
        preset_url, preset_key = PRESETS.get(provider, (None, None))
        base = settings.get("base_url") or (os.environ.get("OPENAI_BASE_URL") if provider == "openai" else None)
        self.base_url = (base or preset_url or "").rstrip("/")
        if not self.base_url:
            raise ModelError(f"[providers.{provider}] needs a base_url")
        self.key_env = settings.get("api_key_env", preset_key)
        self._key = settings.get("api_key")
        self.headers = {str(k): str(v) for k, v in (settings.get("headers") or {}).items()}
        #: extra fields merged into every request body (temperature, max_tokens, reasoning_effort, ...)
        self.params = dict(settings.get("params") or {})
        if provider == "openrouter":
            self.params.setdefault("usage", {"include": True})  # ask OpenRouter to report cost
        self.timeout = float(settings.get("timeout", 600))
        self.retries = int(settings.get("retries", 4))
        self.compact_at = int(settings.get("compact_at", 300_000))
        self.window_chars = int(settings.get("window_chars", self.window_chars))
        self.parallel = int(settings.get("parallel", self.parallel))
        self.pricing = pricing or {}
        self.reports_cost = provider == "openrouter" or pricing_table.lookup(self.model_name, self.pricing) is not None
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    def api_key(self) -> str | None:
        if self._key:
            return str(self._key)
        if self.key_env:
            key = os.environ.get(self.key_env)
            if not key:
                raise ModelError(
                    f"{self.spec} needs an API key: set ${self.key_env} (or api_key in [providers.{self.provider}])"
                )
            return key
        return None

    def chat(self, system: str) -> Chat:
        return _OpenAIChat(self, system)

    def pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="rlm-http")
        return self._pool

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ http

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        """Blocking request with retries; run it through the thread pool."""
        headers = {"content-type": "application/json", "accept": "application/json", **self.headers}
        key = self.api_key()
        if key:
            headers.setdefault("authorization", f"Bearer {key}")
        data = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}/chat/completions"
        attempt = 0
        while True:
            request = urllib.request.Request(url, data=data, method="POST", headers=headers)
            wait: float | None = None
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = _error_text(exc.read())
                if exc.code not in _RETRY_STATUS or attempt >= self.retries:
                    hint = ""
                    if exc.code in (401, 403):
                        hint = f" Check the API key{f' in ${self.key_env}' if self.key_env else ''}."
                    elif exc.code == 404:
                        hint = " Check the model id and base_url."
                    raise ModelError(f"{self.spec}: HTTP {exc.code}: {detail.rstrip('.')}.{hint}") from None
                wait = _retry_after(exc.headers.get("retry-after"))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt >= self.retries:
                    reason = getattr(exc, "reason", exc)
                    raise ModelError(
                        f"{self.spec}: could not reach {self.base_url}: {reason}", retryable=True
                    ) from None
            except ValueError as exc:
                raise ModelError(f"{self.spec}: the endpoint did not return JSON ({exc})") from None
            attempt += 1
            time.sleep(wait if wait is not None else min(30.0, 0.5 * 2**attempt) * (0.5 + random.random() / 2))

    def usage(self, raw: Any) -> Usage:
        raw = raw if isinstance(raw, dict) else {}
        prompt = int(raw.get("prompt_tokens") or 0)
        output = int(raw.get("completion_tokens") or 0)
        details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
        cached = int(details.get("cached_tokens") or 0)
        cost = raw.get("cost") if isinstance(raw.get("cost"), (int, float)) else None
        if cost is None:
            cost = pricing_table.estimate(
                self.model_name, self.pricing, fresh_input=max(0, prompt - cached), output=output, cache_read=cached
            )
        return Usage(input_tokens=prompt, output_tokens=output, cached_tokens=cached, cost_usd=cost)


class _OpenAIChat(StatelessChat):
    def __init__(self, model: OpenAICompatible, system: str):
        super().__init__(system, compact_at=model.compact_at)
        self.model = model

    async def _request(self, on_text: TextSink | None) -> Reply:
        body = {
            "model": self.model.model_name,
            "messages": [{"role": "system", "content": self.system}, *self.messages],
            **self.model.params,
            "stream": False,
        }
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(self.model.pool(), self.model.post, body)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"{self.model.spec}: the response had no choices: {json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = content or ""
        if on_text and text:
            on_text(text)
        return Reply(
            text=text,
            usage=self.model.usage(data.get("usage")),
            model=data.get("model") or self.model.spec,
            stop=choices[0].get("finish_reason"),
        )


def _error_text(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:400] or "no details"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:400]
    return str(error or payload)[:400]


def _retry_after(value: str | None) -> float | None:
    try:
        return min(60.0, max(0.0, float(value))) if value else None
    except ValueError:
        return None
