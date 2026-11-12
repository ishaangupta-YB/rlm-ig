"""A local OpenAI-compatible chat-completions server for tests.

``reply`` is a function of the decoded request body returning the assistant
text (or raising ``HTTPStatus`` to fail the request). Every request body is
kept in ``requests`` for assertions. Streaming and plain responses are both
implemented, since different clients ask for different ones.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HTTPStatus(Exception):
    def __init__(self, code: int, message: str = "error", headers: dict | None = None):
        self.code, self.message, self.headers = code, message, headers or {}


class OpenAIServer:
    def __init__(self, reply, usage=None):
        self.reply = reply
        self.usage = usage or {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128}
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._json(200, {"object": "list", "data": [{"id": "m1", "object": "model"}]})

            def _json(self, code, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                try:
                    text = outer.reply(body)
                except HTTPStatus as status:
                    self._json(status.code, {"error": {"message": status.message, "type": "test"}}, status.headers)
                    return
                usage = outer.usage(body) if callable(outer.usage) else outer.usage
                base = {"id": "chatcmpl-test", "created": int(time.time()), "model": body.get("model")}
                if not body.get("stream"):
                    choice = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                    self._json(200, {**base, "object": "chat.completion", "choices": [choice], "usage": usage})
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def event(obj):
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                    self.wfile.flush()

                chunk = {**base, "object": "chat.completion.chunk"}
                event(
                    {
                        **chunk,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                    }
                )
                for start in range(0, len(text), 16):
                    delta = {"content": text[start : start + 16]}
                    event({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                event({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                event({**chunk, "choices": [], "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def last_user_text(body: dict) -> str:
    content = body["messages"][-1]["content"]
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


def system_text(body: dict) -> str:
    out = []
    for message in body["messages"]:
        if message["role"] in ("system", "developer"):
            content = message["content"]
            out.append(content if isinstance(content, str) else "".join(p.get("text", "") for p in content))
    return "\n".join(out)
