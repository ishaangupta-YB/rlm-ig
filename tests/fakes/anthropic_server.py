"""A local stand-in for the Anthropic Messages API (streaming only)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class AnthropicServer:
    """``reply(body)`` returns the text, or a dict with any of:
    ``text``, ``stop_reason``, ``status`` (HTTP error code), ``message``, ``usage``.
    """

    def __init__(self, reply):
        self.reply = reply
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.headers: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                outer.paths.append(self.path)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                out = outer.reply(body)
                if isinstance(out, str):
                    out = {"text": out}
                if out.get("status"):
                    data = json.dumps(
                        {
                            "type": "error",
                            "error": {"type": "invalid_request_error", "message": out.get("message", "bad")},
                        }
                    ).encode()
                    self.send_response(out["status"])
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                text = out.get("text", "")
                usage = {
                    "input_tokens": 12,
                    "cache_creation_input_tokens": 300,
                    "cache_read_input_tokens": 1000,
                    "output_tokens": 0,
                }
                usage.update(out.get("usage") or {})
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def event(name, obj):
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
                    self.wfile.flush()

                message = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": usage,
                }
                event("message_start", {"type": "message_start", "message": message})
                event(
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                )
                for start in range(0, len(text), 20):
                    event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": text[start : start + 20]},
                        },
                    )
                event("content_block_stop", {"type": "content_block_stop", "index": 0})
                delta = {"stop_reason": out.get("stop_reason", "end_turn"), "stop_sequence": None}
                event(
                    "message_delta",
                    {"type": "message_delta", "delta": delta, "usage": {"output_tokens": max(1, len(text) // 4)}},
                )
                event("message_stop", {"type": "message_stop"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
