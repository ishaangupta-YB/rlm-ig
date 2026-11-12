"""Small text utilities shared by the engine and the backends."""

from __future__ import annotations

import ast
import re

_CODE_LANGS = {"python", "py", "python3", "repl", "ipython", "pycon"}
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+.-]*)[^\n]*$")


def clip_middle(text: str, limit: int, *, hint: str = "print a slice or filter instead of the whole value") -> str:
    """Shorten ``text`` to about ``limit`` characters, keeping both ends."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = (limit * 3) // 5
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n... [{omitted:,} characters omitted; {hint}] ...\n{text[-tail:]}"


def _parses(code: str) -> bool:
    try:
        compile(code, "<reply>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        return True
    except (SyntaxError, ValueError):
        return False


def extract_code(reply: str) -> str | None:
    """Return the Python a model reply asks to run, or ``None``.

    Fenced blocks tagged as Python are joined in order. If no block is tagged,
    untagged blocks that parse as Python are accepted. A fence is recognised
    only at the start of a line, and when a block contains fence-like lines of
    its own the closing fence is the first one that leaves valid Python
    behind. A final block left unterminated (the reply was cut off) is kept.
    """
    lines = reply.split("\n")
    tagged: list[str] = []
    untagged: list[str] = []
    i = 0
    while i < len(lines):
        opened = _FENCE_OPEN.match(lines[i])
        if not opened:
            i += 1
            continue
        fence, lang = opened.group(1), opened.group(2).lower()
        closers = [
            j
            for j in range(i + 1, len(lines))
            if lines[j].strip().startswith(fence) and lines[j].strip(fence[0] + " \t") == ""
        ]
        is_code = lang in _CODE_LANGS
        if not closers:
            body = "\n".join(lines[i + 1 :])
            end = len(lines)
        else:
            end = closers[0]
            if is_code and len(closers) > 1:
                for candidate in closers:
                    if _parses("\n".join(lines[i + 1 : candidate])):
                        end = candidate
                        break
            body = "\n".join(lines[i + 1 : end])
        body = body.strip("\n")
        if body.strip():
            if is_code:
                tagged.append(body)
            elif lang == "" and _parses(body):
                untagged.append(body)
        i = end + 1
    chosen = tagged or untagged
    return "\n\n".join(chosen) if chosen else None


def first_line(text: str, width: int = 100) -> str:
    """The first non-empty line of ``text``, shortened for one-line display."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= width else line[: width - 1] + "…"
    return ""


def approx_tokens(chars: int) -> int:
    """A rough character-to-token conversion for estimates and guards."""
    return max(1, chars // 4)


def looks_runaway(text: str, *, max_chars: int = 80_000) -> bool:
    """True when a streamed reply has clearly degenerated.

    Models occasionally fall into a loop and emit the same short fragment
    until they hit their output ceiling. Two signs: the reply is far longer
    than any sensible REPL step, or its tail is one short unit repeated many
    times over.
    """
    if len(text) > max_chars:
        return True
    if len(text) < 1500:
        return False
    tail = text[-1200:]
    for period in range(1, 61):
        unit = tail[-period:]
        if not unit.strip():
            continue
        # At least 600 characters of pure repetition: long enough that a ruler
        # line or a padded table in a legitimate reply never trips it.
        need = max(12, -(-600 // period))
        if tail.endswith(unit * need):
            return True
    return False
