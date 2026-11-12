#!/usr/bin/env python3
"""A stand-in for the `claude` CLI that speaks its stream-json protocol.

Replies come from the JSON list in $FAKE_SCRIPT, one per user message. The
position is kept in $FAKE_STATE so it survives the process being restarted.
A reply may be a directive instead of text:

    @@crash          exit without answering
    @@stall          send keep-alive pings forever
    @@runaway        stream the same fragment forever
    @@error:<text>   finish the turn with an error result
"""

import json
import os
import sys
import time

script = json.load(open(os.environ["FAKE_SCRIPT"]))
state_path = os.environ["FAKE_STATE"]
log_path = os.environ.get("FAKE_LOG")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def stream(inner):
    emit({"type": "stream_event", "event": inner})


def log(record):
    if log_path:
        with open(log_path, "a") as handle:
            handle.write(json.dumps(record) + "\n")


def next_index():
    try:
        index = int(open(state_path).read())
    except (OSError, ValueError):
        index = 0
    with open(state_path, "w") as handle:
        handle.write(str(index + 1))
    return index


argv = sys.argv[1:]
system = ""
if "--system-prompt-file" in argv:
    system = open(argv[argv.index("--system-prompt-file") + 1]).read()
log({"argv": argv, "system": system, "cwd": os.getcwd(), "env_entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT")})

total_cost = 0.0
first = True
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    text = "".join(block.get("text", "") for block in message["message"]["content"])
    log({"user": text})
    index = next_index()
    reply = script[index] if index < len(script) else "@@error:script exhausted"
    if first:
        emit(
            {
                "type": "system",
                "subtype": "init",
                "model": "fake-model-1",
                "apiKeySource": "none",
                "claude_code_version": "0.0.0-fake",
                "tools": [],
            }
        )
        first = False
    if reply == "@@crash":
        sys.stderr.write("fake claude: simulated crash\n")
        sys.exit(3)
    stream({"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
    if reply == "@@stall":
        while True:
            stream({"type": "ping"})
            time.sleep(0.05)
    if reply == "@@runaway":
        stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Let me think.\n"}})
        while True:
            stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "\n\n\n<br>" * 40}})
            time.sleep(0.001)
    if reply.startswith("@@error:"):
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": reply[8:],
                "total_cost_usd": total_cost,
            }
        )
        continue
    for start in range(0, len(reply), 7):
        stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply[start : start + 7]}})
    usage = {
        "input_tokens": 3,
        "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 100 * index,
        "output_tokens": len(reply) // 4,
    }
    stream({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage})
    stream({"type": "message_stop"})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
    total_cost += 0.01
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": reply,
            "total_cost_usd": total_cost,
            "usage": usage,
            "stop_reason": "end_turn",
        }
    )
