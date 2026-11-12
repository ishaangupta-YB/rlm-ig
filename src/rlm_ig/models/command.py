"""Any command-line program as a model.

For agents and tools with no dedicated backend. Each turn runs the command
once: the system prompt and the conversation so far go in on stdin, the reply
comes back on stdout. Define one in the config::

    [providers.mycli]
    kind = "command"
    command = ["mycli", "chat", "--model", "{model}"]
    json_path = "choices.0.text"     # optional: stdout is JSON, the reply is here

and select it with ``model = "mycli:some-model"``. The program is run as
given; rlm-ig adds no flags of its own, so put any "no tools" or read-only
options the program has into ``command``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from typing import Any

from .. import procs
from ..budget import Usage
from ..errors import ModelError
from ..text import approx_tokens
from .base import Chat, Model, Reply, StatelessChat, TextSink


class CommandModel(Model):
    parallel = 4

    def __init__(
        self,
        provider: str = "command",
        model: str = "",
        settings: dict[str, Any] | None = None,
        pricing: dict | None = None,
    ):
        settings = dict(settings or {})
        command = settings.get("command")
        if isinstance(command, str):
            command = command.split()
        if not command or not isinstance(command, list):
            raise ModelError(f'[providers.{provider}] needs command = ["program", "arg", ...]')
        self.model_name = model or settings.get("model") or ""
        self.spec = provider + (f":{self.model_name}" if self.model_name else "")
        self.command = [str(part).replace("{model}", self.model_name) for part in command]
        self.json_path = settings.get("json_path")
        self.env = {str(k): str(v) for k, v in (settings.get("env") or {}).items()}
        self.timeout = float(settings.get("timeout", 600))
        self.compact_at = int(settings.get("compact_at", 300_000))
        self.window_chars = int(settings.get("window_chars", self.window_chars))
        self.parallel = int(settings.get("parallel", self.parallel))

    def chat(self, system: str) -> Chat:
        return _CommandChat(self, system)


def render_transcript(system: str, messages: list[dict[str, str]]) -> str:
    parts = [system.rstrip(), ""]
    for message in messages:
        label = "ASSISTANT" if message["role"] == "assistant" else "USER"
        parts.append(f"### {label}\n{message['content']}\n")
    parts.append("### ASSISTANT")
    return "\n".join(parts)


class _CommandChat(StatelessChat):
    def __init__(self, model: CommandModel, system: str):
        super().__init__(system, compact_at=model.compact_at)
        self.model = model

    async def _request(self, on_text: TextSink | None) -> Reply:
        import os

        prompt = render_transcript(self.system, self.messages)
        executable = shutil.which(self.model.command[0])
        if executable is None:
            raise ModelError(f"{self.model.spec}: {self.model.command[0]!r} was not found on PATH")
        workdir = tempfile.mkdtemp(prefix="rlm-cmd-")
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                *self.model.command[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env={**os.environ, **self.model.env, "PWD": workdir},
                start_new_session=(os.name == "posix"),
            )
            procs.track(proc.pid, group=True)
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), self.model.timeout)
            except TimeoutError:
                raise ModelError(
                    f"{self.model.spec}: no reply within {self.model.timeout:g}s", retryable=True
                ) from None
            finally:
                procs.kill(proc.pid, group=True)
                procs.untrack(proc.pid)
                with contextlib.suppress(Exception):
                    await proc.wait()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        stdout = out.decode("utf-8", "replace")
        if proc.returncode != 0:
            tail = err.decode("utf-8", "replace").strip()[-500:] or stdout.strip()[-500:]
            raise ModelError(f"{self.model.spec}: exited with code {proc.returncode}: {tail or 'no output'}")
        text = _dig(stdout, self.model.json_path, self.model.spec) if self.model.json_path else stdout.strip()
        if on_text and text:
            on_text(text)
        usage = Usage(input_tokens=approx_tokens(len(prompt)), output_tokens=approx_tokens(len(text)))
        return Reply(text=text, usage=usage, model=self.model.spec)


def _dig(stdout: str, path: str, spec: str) -> str:
    try:
        node: Any = json.loads(stdout)
    except ValueError:
        raise ModelError(f"{spec}: json_path is set but the output is not JSON: {stdout.strip()[:200]}") from None
    for key in path.split("."):
        try:
            node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ModelError(f"{spec}: json_path {path!r} not found in the output") from None
    return node if isinstance(node, str) else json.dumps(node)
