#!/usr/bin/env python3
"""A minimal MCP server for tests.

Run as a script it speaks the stdio transport. ``HttpMcpServer`` serves the
same tools over streamable HTTP: plain JSON for most methods and an SSE
stream for ``tools/call``, with a session id that later requests must carry.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer", "description": "first addend"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "echo",
        "description": "Repeat the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "shout": {"type": "boolean", "description": "upper-case it"}},
            "required": ["text"],
        },
    },
    {"name": "fail", "description": "Always reports an error.", "inputSchema": {"type": "object", "properties": {}}},
    {
        "name": "slow",
        "description": "Sleep, then answer.",
        "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]},
    },
    {"name": "list-items", "description": "A tool whose name is not an identifier.", "inputSchema": {"type": "object"}},
]


def handle(message: dict) -> dict | None:
    """Answer one JSON-RPC message; ``None`` for notifications."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        )
    if method == "tools/list":
        if params.get("cursor") == "page-2":
            return ok({"tools": TOOLS[3:]})
        return ok({"tools": TOOLS[:3], "nextCursor": "page-2"})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "add":
            total = args["a"] + args["b"]
            return ok({"content": [{"type": "text", "text": str(total)}], "structuredContent": {"sum": total}})
        if name == "echo":
            text = args["text"].upper() if args.get("shout") else args["text"]
            return ok({"content": [{"type": "text", "text": text}]})
        if name == "fail":
            return ok({"content": [{"type": "text", "text": "it broke"}], "isError": True})
        if name == "slow":
            time.sleep(float(args["seconds"]))
            return ok({"content": [{"type": "text", "text": "done"}, {"type": "text", "text": "really"}]})
        if name == "list-items":
            return ok({"content": [{"type": "resource", "uri": "x://1"}]})
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": f"unknown tool {name}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"no method {method}"}}


def serve_stdio() -> None:
    lock = threading.Lock()

    def reply(message: dict) -> None:
        answer = handle(message)
        if answer is not None:
            with lock:
                sys.stdout.write(json.dumps(answer) + "\n")
                sys.stdout.flush()

    print("fake mcp server starting (not JSON, must be ignored)", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "method" not in message:
            continue  # a response to one of our own requests
        if message.get("method") == "tools/call":
            # Calls run on threads so that slow ones overlap.
            threading.Thread(target=reply, args=(message,), daemon=True).start()
        else:
            reply(message)


class HttpMcpServer:
    SESSION = "session-abc"

    def __init__(self):
        self.seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_DELETE(self):
                outer.seen.append({"method": "DELETE", "session": self.headers.get("mcp-session-id")})
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                message = json.loads(self.rfile.read(length))
                outer.seen.append(
                    {
                        "method": message.get("method"),
                        "session": self.headers.get("mcp-session-id"),
                        "protocol": self.headers.get("mcp-protocol-version"),
                        "auth": self.headers.get("authorization"),
                    }
                )
                if message.get("method") != "initialize" and self.headers.get("mcp-session-id") != outer.SESSION:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"missing session id")
                    return
                answer = handle(message)
                if answer is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                if message.get("method") == "tools/call":
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    notice = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
                    for event in (notice, answer):
                        self.wfile.write(b"event: message\ndata: " + json.dumps(event).encode() + b"\n\n")
                        self.wfile.flush()
                    return
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                if message.get("method") == "initialize":
                    self.send_header("mcp-session-id", outer.SESSION)
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


if __name__ == "__main__":
    serve_stdio()
