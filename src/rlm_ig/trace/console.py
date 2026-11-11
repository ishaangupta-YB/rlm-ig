"""Live progress on the terminal.

Everything goes to stderr, so stdout carries only the answer and a run can be
piped into another program at any verbosity.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from ..engine.events import Event
from ..text import first_line

_COLORS = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36"}


class Console:
    """An event sink that prints one or two lines per step."""

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
        self._width = 0

    # ------------------------------------------------------------- plumbing

    def _c(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return f"\x1b[{';'.join(_COLORS[s] for s in styles)}m{text}\x1b[0m"

    def _sym(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    def _print(self, text: str = "") -> None:
        try:
            print(text, file=self.stream, flush=True)
        except (OSError, ValueError):
            pass

    def _tag(self, event: Event) -> str:
        label = "root" if event.agent in ("", "0") else event.agent[2:]
        return self._c(f"{'  ' * event.depth}{label:<5}", "cyan" if event.depth == 0 else "blue")

    def _block(self, text: str, depth: int, style: str) -> None:
        pad = "  " * depth + "      " + self._sym("│ ", "| ")
        for line in text.rstrip().splitlines() or [""]:
            self._print(self._c(pad, "dim") + self._c(line, style))

    # --------------------------------------------------------------- events

    def __call__(self, event: Event) -> None:
        if self.verbosity <= 0:
            return
        handler = getattr(self, "_on_" + event.kind.replace(".", "_"), None)
        if handler:
            handler(event)

    def _on_run_start(self, e: Event) -> None:
        models = e["model"] if e.get("sub_model") in (None, e["model"]) else f"{e['model']}  sub {e['sub_model']}"
        self._print(
            f"{self._c('rlm', 'bold')}  {models}  {self._c(self._sym('·', '-') + ' ' + e.get('summary', ''), 'dim')}"
        )

    def _on_agent_start(self, e: Event) -> None:
        if e.depth == 0:
            return
        size = f"{e.get('context_size', 0):,} chars"
        self._print(
            f"{self._tag(e)} {self._c(self._sym('◆', '*') + ' sub-agent', 'blue')} {first_line(e['task'], 70)} {self._c('(' + size + ')', 'dim')}"
        )

    def _on_model_reply(self, e: Event) -> None:
        note = first_line(_prose(e["text"]), 90)
        took = self._c(f"{e['seconds']:.1f}s", "dim")
        head = f"{self._tag(e)} {self._sym('▸', '>')} turn {e['step']}  {took}"
        self._print(f"{head}  {note}" if note else head)

    def _on_cell(self, e: Event) -> None:
        pad = "  " * e.depth + "      "
        if self.verbosity >= 2:
            self._block(e["code"], e.depth, "dim")
        if e.get("submitted"):
            self._print(f"{pad}{self._c(self._sym('✔', 'ok') + ' submitted', 'green')}")
        elif e.get("error"):
            last = (e["output"].strip().splitlines() or ["error"])[-1]
            self._print(f"{pad}{self._c(self._sym('✗', 'x') + ' ' + first_line(last, 100), 'red')}")
        else:
            size = len(e["output"])
            self._print(self._c(f"{pad}{self._sym('└', '-')} {e['seconds']:.2f}s  {size:,} chars out", "dim"))
        if self.verbosity >= 2 and e["output"].strip() and not e.get("submitted"):
            self._block(e["output"], e.depth, "red" if e.get("error") else "")

    def _on_call_start(self, e: Event) -> None:
        kind = e["op"]
        if kind == "tool" and self.verbosity < 2:
            return
        pad = "  " * e.depth + "      "
        count = f" ×{e['n']}" if e.get("n", 1) != 1 else ""
        if not self.unicode:
            count = count.replace("×", "x")
        detail = e.get("name") or (f"{e.get('chars', 0):,} chars" if e.get("chars") else "")
        self._print(f"{pad}{self._c(self._sym('⇉', '>>') + ' ' + kind + count, 'yellow')}  {self._c(detail, 'dim')}")

    def _on_call_end(self, e: Event) -> None:
        if e["op"] == "tool" and self.verbosity < 2:
            return
        pad = "  " * e.depth + "      "
        if not e.get("ok"):
            self._print(
                f"{pad}{self._c(self._sym('✗', 'x') + ' ' + e['op'] + ': ' + first_line(e.get('error', ''), 110), 'red')}"
            )
        elif e["op"] in ("ask_map", "delegate_map", "delegate") or self.verbosity >= 2:
            failed = f", {e['failed']} failed" if e.get("failed") else ""
            self._print(self._c(f"{pad}{self._sym('⇇', '<<')} {e['op']} done in {e['seconds']:.1f}s{failed}", "dim"))

    def _on_agent_end(self, e: Event) -> None:
        if e.depth == 0:
            return
        if e["status"] in ("ok", "partial"):
            self._print(
                f"{self._tag(e)} {self._c(self._sym('✔', 'ok'), 'green')} {first_line(e.get('answer', ''), 90)}"
            )
        else:
            self._print(
                f"{self._tag(e)} {self._c(self._sym('✗', 'x') + ' ' + first_line(e.get('error', ''), 100), 'red')}"
            )

    def _on_note(self, e: Event) -> None:
        self._print(self._c(f"note: {e['message']}", "yellow"))

    def _on_run_end(self, e: Event) -> None:
        status = e["status"]
        style = {"ok": "green", "partial": "yellow"}.get(status, "red")
        mark = self._sym({"ok": "✔", "partial": "◐"}.get(status, "✗"), status)
        line = f"{self._c(mark + ' ' + status, style, 'bold')}  {e['seconds']:.1f}s {self._sym('·', '-')} {e['usage_text']}"
        self._print(line)
        if e.get("error"):
            self._print(self._c(f"  {e['error']}", "red"))
        if e.get("note"):
            self._print(self._c(f"  {e['note']}", "yellow"))
        if e.get("trace") and self.verbosity >= 1:
            self._print(self._c(f"  trace: {e['trace']}", "dim"))


def _prose(reply: str) -> str:
    """The part of a model reply that precedes its code block."""
    cut = reply.find("```")
    return reply if cut == -1 else reply[:cut]
