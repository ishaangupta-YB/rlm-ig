"""Small text utilities shared by the engine and the backends."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _Block:
    """One fenced block of a reply."""

    lang: str
    body: str
    #: no closing fence was found for it
    unterminated: bool = False
    #: unterminated, and ended at the next fence opener with a language tag
    cut: bool = False

    @property
    def is_code(self) -> bool:
        return self.lang in _CODE_LANGS


def _blocks(lines: list[str], *, cut_at_tagged_fence: bool = False) -> list[_Block]:
    """Split a reply into its fenced blocks.

    With ``cut_at_tagged_fence``, a Python block ends at the next line that
    opens a fence with a language tag when no closing fence comes before it.
    The block is then unterminated and ``cut``, and the opener starts a block
    of its own. Otherwise an unterminated block runs to the end of the reply.
    """
    blocks: list[_Block] = []
    i = 0
    while i < len(lines):
        opened = _FENCE_OPEN.match(lines[i])
        if not opened:
            i += 1
            continue
        fence, lang = opened.group(1), opened.group(2).lower()
        is_code = lang in _CODE_LANGS
        limit = len(lines)
        if cut_at_tagged_fence and is_code:
            for j in range(i + 1, len(lines)):
                nxt = _FENCE_OPEN.match(lines[j])
                if nxt and nxt.group(2):
                    limit = j
                    break
        closers = [
            j
            for j in range(i + 1, limit)
            if lines[j].strip().startswith(fence) and lines[j].strip(fence[0] + " \t") == ""
        ]
        unterminated = cut = False
        if not closers:
            end = limit
            unterminated = True
            cut = limit < len(lines)
            next_i = end if cut else end + 1
        else:
            end = closers[0]
            if is_code and len(closers) > 1:
                for candidate in closers:
                    if _parses("\n".join(lines[i + 1 : candidate])):
                        end = candidate
                        break
            next_i = end + 1
        body = "\n".join(lines[i + 1 : end]).strip("\n")
        blocks.append(_Block(lang, body, unterminated, cut))
        i = next_i
    return blocks


def extract_code(reply: str) -> str | None:
    """Return the Python a model reply asks to run, or ``None``.

    Fenced blocks tagged as Python are joined in order. If no block is tagged,
    untagged blocks that parse as Python are accepted. A fence is recognised
    only at the start of a line, and when a block contains fence-like lines of
    its own the closing fence is the first one that leaves valid Python
    behind. A final block left unterminated (the reply was cut off) is kept.
    Code that does not compile is repaired when the damage is unambiguous;
    see :func:`extract_code_with_repairs`.
    """
    return extract_code_with_repairs(reply)[0]


def extract_code_with_repairs(reply: str) -> tuple[str | None, list[str]]:
    """:func:`extract_code`, plus a description of each repair it made.

    The repair pass runs only when the code would not compile. It removes
    lines that hold nothing but a markup tag (``</parameter>``, left over from
    native tool-call habits), and drops a short prose paragraph that sits
    between an unclosed block and the next fence. A repair is used only when
    the result compiles; otherwise the original code comes back unchanged, so
    a genuine syntax error still reaches the model.
    """
    lines = reply.split("\n")
    blocks = _blocks(lines)
    tagged = [b.body for b in blocks if b.is_code and b.body.strip()]
    untagged = [b.body for b in blocks if b.lang == "" and b.body.strip() and _parses(b.body)]
    chosen = tagged or untagged
    if not chosen:
        return _salvage(lines), []
    code = "\n\n".join(chosen)
    if _parses(code):
        return code, []
    repaired = _repair(lines)
    if repaired is None:
        return code, []
    return repaired


_MARKUP_LINE = re.compile(r"^\s*</?[A-Za-z_][\w:.-]*(?:\s[^<>\n]*)?/?>\s*$")
_CODE_CHARS = frozenset("=()[]{}")


def _repair(lines: list[str]) -> tuple[str, list[str]] | None:
    """Repaired code and what was done, or ``None`` when no safe repair exists."""
    parts: list[str] = []
    removed = dropped = closed = 0
    for block in _blocks(lines, cut_at_tagged_fence=True):
        if not block.is_code:
            continue
        if not block.body.strip():
            closed += block.cut
            continue
        body = block.body
        if _parses(body):
            closed += block.cut
            parts.append(body)
            continue
        kept = [line for line in body.split("\n") if not _MARKUP_LINE.match(line)]
        stripped = "\n".join(kept).strip("\n")
        if stripped.strip() and _parses(stripped):
            closed += block.cut
            removed += len(body.split("\n")) - len(kept)
            parts.append(stripped)
            continue
        trimmed = _drop_prose_paragraph(stripped) if block.cut else None
        if trimmed is None:
            return None
        removed += len(body.split("\n")) - len(kept)
        dropped += 1
        parts.append(trimmed)
    if not parts:
        return None
    code = "\n\n".join(parts)
    if not _parses(code):
        return None
    repairs = []
    if removed:
        repairs.append(f"removed {removed} stray markup line{'s' if removed != 1 else ''}")
    if dropped:
        repairs.append("dropped prose after an unclosed block" + (f" ({dropped} times)" if dropped > 1 else ""))
    if closed:
        repairs.append("ended an unclosed block at the next fence" + (f" ({closed} times)" if closed > 1 else ""))
    return code, repairs


def _drop_prose_paragraph(body: str) -> str | None:
    """``body`` without its last paragraph, if that paragraph is prose and the rest compiles."""
    lines = body.split("\n")
    blank = [k for k, line in enumerate(lines) if not line.strip()]
    if not blank:
        return None
    paragraph = lines[blank[-1] + 1 :]
    if not 1 <= len(paragraph) <= 3:
        return None
    for line in paragraph:
        if not line or line[0].isspace() or _CODE_CHARS.intersection(line):
            return None
    rest = "\n".join(lines[: blank[-1]]).strip("\n")
    if rest.strip() and _parses(rest):
        return rest
    return None


_STRAY_TAG = re.compile(r"^\s*<[/\w][^>\n]*?(?:>|(?=\s))\s*")


def _salvage(lines: list[str]) -> str | None:
    """Recover code from a reply whose opening fence was mangled.

    Models used to native tool calls sometimes open a block with a stray tag
    (``<invoke ...``) instead of a fence, then close it with a normal fence.
    If the reply has exactly one fence line, the longest run of lines directly
    above it that parses as Python is taken to be the code.
    """
    fences = [i for i, line in enumerate(lines) if line.strip().startswith(("```", "~~~"))]
    if len(fences) != 1 or fences[0] == 0:
        return None
    body = lines[: fences[0]]
    for start in range(len(body)):
        first = _STRAY_TAG.sub("", body[start], count=1)
        candidate = "\n".join([first, *body[start + 1 :]]).strip("\n")
        if not candidate.strip():
            return None
        # Require real code, not a stray word that happens to be an expression.
        if _parses(candidate) and re.search(r"[=(.\[]|^\s*(import|from|def|for|if|print)\b", candidate, re.M):
            return candidate
    return None


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
