"""Live progress on the terminal.

Everything goes to stderr, so stdout carries only the answer and a run can be
piped into another program at any verbosity. Every line starts with the agent
it belongs to, which keeps the output readable when sub-agents run in
parallel and their lines interleave.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from ..engine.events import Event
from ..text import first_line

_COLORS = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36"}


def agent_label(agent: str) -> str:
    """``"0"`` is the root; ``"0.2.1"`` is shown as ``2.1``."""
    return "root" if agent in ("", "0") else agent[2:]


class Console:
    """An event sink that prints a line or two per step."""

    def __init__(self, verbosity: int = 1, stream: TextIO | None = None, color: bool | None = None):
        self.verbosity = verbosity
        self.stream = stream or sys.stderr
        if color is None:
            color = (
                hasattr(self.stream, "isatty")
                and self.stream.isatty()
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM") != "dumb"
            )
        self.color = color
        encoding = (getattr(self.stream, "encoding", None) or "ascii").lower()
        self.unicode = "utf" in encoding

    # ------------------------------------------------------------- plumbing

    def _c(self, text: str, *styles: str) -> str:
        if not self.color or not styles or not text:
            return text
        return f"\x1b[{';'.join(_COLORS[s] for s in styles)}m{text}\x1b[0m"

    def _sym(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    def _print(self, text: str = "") -> None:
        try:
            print(text, file=self.stream, flush=True)
        except (OSError, ValueError):
            pass

    def _line(self, event: Event, text: str) -> None:
        indent = "  " * event.depth
        tag = self._c(f"{agent_label(event.agent):<6}", "cyan" if event.depth == 0 else "blue")
        self._print(f"{indent}{tag} {text}")

    def _block(self, event: Event, text: str, style: str = "") -> None:
        bar = self._c(self._sym("│", "|"), "dim")
        for line in text.rstrip().splitlines() or [""]:
            self._line(event, f"  {bar} {self._c(line, style)}")

    # --------------------------------------------------------------- events

    def __call__(self, event: Event) -> None:
        if self.verbosity <= 0:
            return
        handler = getattr(self, "_on_" + event.kind.replace(".", "_"), None)
        if handler:
            handler(event)

    def _on_run_start(self, e: Event) -> None:
        models = e["model"] if e.get("sub_model") in (None, e["model"]) else f"{e['model']}  (sub: {e['sub_model']})"
        dot = self._sym("·", "-")
        self._print(f"{self._c('rlm', 'bold')}  {models}  {self._c(f'{dot} ' + e.get('summary', ''), 'dim')}")

    def _on_agent_start(self, e: Event) -> None:
        if e.depth == 0:
            return
        size = self._c(f"({e.get('context_size', 0):,} chars)", "dim")
        self._line(e, f"{self._c(self._sym('◆', '*') + ' sub-agent', 'blue')} {first_line(e['task'], 70)} {size}")

    def _on_model_reply(self, e: Event) -> None:
        note = first_line(_prose(e["text"]), 96)
        took = self._c(f"{e['seconds']:.1f}s", "dim")
        self._line(e, f"{self._sym('▸', '>')} turn {e['step']}  {took}  {note}".rstrip())

    def _on_cell_start(self, e: Event) -> None:
        if self.verbosity >= 2:
            self._block(e, e["code"], "dim")

    def _on_cell(self, e: Event) -> None:
        if e.get("submitted"):
            self._line(e, f"  {self._c(self._sym('✔', 'ok') + ' submitted', 'green')}")
            return
        if e.get("error"):
            last = (e["output"].strip().splitlines() or ["error"])[-1]
            self._line(e, f"  {self._c(self._sym('✗', 'x') + ' ' + first_line(last, 100), 'red')}")
        else:
            corner = self._sym("└", "-")
            self._line(e, self._c(f"  {corner} ran {e['seconds']:.2f}s, {len(e['output']):,} chars of output", "dim"))
        if self.verbosity >= 2 and e["output"].strip():
            self._block(e, e["output"], "red" if e.get("error") else "")

    def _on_call_start(self, e: Event) -> None:
        op = e["op"]
        if op == "tool" and self.verbosity < 2:
            return
        times = self._sym("×", "x")
        count = f" {times}{e['n']}" if e.get("n", 1) != 1 else ""
        detail = e.get("name") or (f"{e.get('chars', 0):,} chars" if e.get("chars") else "")
        self._line(
            e, f"  {self._c(self._sym('⇉', '>>') + ' ' + op + count, 'yellow')}  {self._c(detail, 'dim')}".rstrip()
        )

    def _on_call_end(self, e: Event) -> None:
        op = e["op"]
        if op == "tool" and self.verbosity < 2:
            return
        if not e.get("ok"):
            text = f"{self._sym('✗', 'x')} {op}: {first_line(e.get('error', ''), 110)}"
            self._line(e, f"  {self._c(text, 'red')}")
        elif op != "ask" or self.verbosity >= 2:
            failed = f", {e['failed']} failed" if e.get("failed") else ""
            self._line(e, self._c(f"  {self._sym('⇇', '<<')} {op} done in {e['seconds']:.1f}s{failed}", "dim"))

    def _on_agent_end(self, e: Event) -> None:
        if e.depth == 0:
            return
        if e["status"] in ("ok", "partial"):
            self._line(e, f"{self._c(self._sym('✔', 'ok'), 'green')} {first_line(e.get('answer', ''), 90)}")
        else:
            self._line(e, self._c(f"{self._sym('✗', 'x')} {first_line(e.get('error', ''), 100)}", "red"))

    def _on_note(self, e: Event) -> None:
        self._print(self._c(f"note: {e['message']}", "yellow"))

    def _on_run_end(self, e: Event) -> None:
        status = e["status"]
        style = {"ok": "green", "partial": "yellow"}.get(status, "red")
        mark = self._sym({"ok": "✔", "partial": "◐"}.get(status, "✗"), "*")
        dot = self._sym("·", "-")
        self._print(f"{self._c(f'{mark} {status}', style, 'bold')}  {e['seconds']:.1f}s {dot} {e['usage_text']}")
        if e.get("error"):
            self._print(self._c(f"  {e['error']}", "red"))
        if e.get("note"):
            self._print(self._c(f"  {e['note']}", "yellow"))
        if e.get("trace"):
            self._print(self._c(f"  trace: {e['trace']}", "dim"))


def _prose(reply: str) -> str:
    """The part of a model reply that precedes its code block."""
    cut = reply.find("```")
    return reply if cut == -1 else reply[:cut]
