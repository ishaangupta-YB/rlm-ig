"""Sandbox worker: a persistent Python namespace driven over a pipe.

This file is deliberately self-contained (standard library only, no imports
from the rest of the package). The host never imports it into the worker
process; it ships the *source text* down the worker's stdin, so the same code
runs unchanged under a local interpreter, a container, or anything else that
can start ``python`` and connect its standard streams.

Wire format, both directions: an ASCII decimal length, a newline, then that
many bytes of UTF-8 JSON.

Trust boundary: everything this process sends to the host is plain JSON. The
host may send pickles down (it is the trusted side); it never unpickles
anything that comes back up.
"""

import ast
import asyncio
import base64
import builtins
import collections
import inspect
import io
import itertools
import json
import linecache
import os
import pickle
import re
import reprlib
import signal
import sys
import threading
import time
import traceback
import types

# --------------------------------------------------------------------------
# wire
# --------------------------------------------------------------------------


class Wire:
    def __init__(self, rfile, wfile):
        self._r = rfile
        self._w = wfile
        self._lock = threading.Lock()

    def send(self, msg):
        data = json.dumps(msg, ensure_ascii=False, default=_json_fallback).encode("utf-8", "surrogatepass")
        with self._lock:
            self._w.write(b"%d\n" % len(data))
            self._w.write(data)
            self._w.flush()

    def recv(self):
        header = self._r.readline()
        if not header:
            return None
        size = int(header)
        data = self._r.read(size)
        if len(data) < size:
            return None
        return json.loads(data.decode("utf-8", "surrogatepass"))


def _json_fallback(obj):
    return repr(obj)


# --------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------


def jsonable(value, _depth=0):
    """Reduce a Python value to something JSON can carry without losing shape.

    Containers are converted recursively, common value objects are unwrapped,
    and anything else degrades to its repr rather than failing.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else repr(value)
    if _depth > 60:
        return repr(value)
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else str(k)): jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset, collections.deque)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [jsonable(v, _depth + 1) for v in items]
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(bytes(value)).decode("ascii")
    for attr in ("model_dump", "_asdict", "to_dict", "tolist"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return jsonable(method(), _depth + 1)
            except Exception:
                pass
    if hasattr(value, "__dataclass_fields__"):
        return {name: jsonable(getattr(value, name), _depth + 1) for name in value.__dataclass_fields__}
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return repr(value)


def is_plain(value, _depth=0):
    """True when ``jsonable(value)`` is lossless (only JSON-native types)."""
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if _depth > 60:
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_plain(v, _depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(is_plain(v, _depth + 1) for v in value)
    return False


def decode(blob):
    if blob is None:
        return None
    if "p" in blob:
        return pickle.loads(base64.b64decode(blob["p"]))
    return blob.get("j")


def as_text(value):
    """Render a value for inclusion in a prompt."""
    if isinstance(value, str):
        return value
    return json.dumps(jsonable(value), ensure_ascii=False)


# --------------------------------------------------------------------------
# output capture
# --------------------------------------------------------------------------


class Capture(io.TextIOBase):
    """Bounded text sink that keeps the beginning and the end of the stream."""

    def __init__(self, limit):
        self._limit = max(int(limit), 1000)
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._head = []
            self._head_len = 0
            self._tail = collections.deque()
            self._tail_len = 0
            self._dropped = 0

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        written = len(text)
        with self._lock:
            room = self._limit - self._head_len
            if room > 0:
                part = text[:room]
                self._head.append(part)
                self._head_len += len(part)
                text = text[room:]
            if text:
                self._tail.append(text)
                self._tail_len += len(text)
                while self._tail_len - len(self._tail[0]) >= self._limit:
                    gone = self._tail.popleft()
                    self._tail_len -= len(gone)
                    self._dropped += len(gone)
        return written

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        with self._lock:
            head = "".join(self._head)
            tail = "".join(self._tail)
            dropped = self._dropped
            if len(tail) > self._limit:
                dropped += len(tail) - self._limit
                tail = tail[-self._limit :]
            if dropped:
                return "%s\n... [%s characters omitted] ...\n%s" % (head, format(dropped, ","), tail)
            return head + tail


# --------------------------------------------------------------------------
# errors visible to model-written code
# --------------------------------------------------------------------------


class SubcallError(RuntimeError):
    """A sub-model call, sub-agent, or tool failed."""


class BudgetExceeded(SubcallError):
    """The run's budget is spent; no further sub-calls are possible."""


class DelegationRefused(SubcallError):
    """The host declined to start a sub-agent for the reason given."""


class ToolError(SubcallError):
    """A host tool raised."""


class _Submit(BaseException):
    def __init__(self, value):
        self.value = value


class Failed:
    """Placeholder for one failed item in a ``*_map`` result. Falsy."""

    __slots__ = ("error",)

    def __init__(self, error):
        self.error = error

    def __bool__(self):
        return False

    def __repr__(self):
        return "Failed(%r)" % (self.error,)

    __str__ = __repr__


_ERRORS = {
    "BudgetExceeded": BudgetExceeded,
    "DelegationRefused": DelegationRefused,
    "ToolError": ToolError,
    "ValueError": ValueError,
    "TypeError": TypeError,
}

# --------------------------------------------------------------------------
# helpers exposed in the namespace
# --------------------------------------------------------------------------

_brief = reprlib.Repr()
_brief.maxlevel = 4
_brief.maxlist = _brief.maxtuple = _brief.maxset = _brief.maxfrozenset = _brief.maxdeque = 30
_brief.maxdict = 30
_brief.maxstring = _brief.maxother = 600
_brief.maxlong = 80


def brief(value, limit=2000):
    try:
        text = _brief.repr(value)
    except Exception as exc:  # a broken __repr__ must not break the cell
        text = "<%s: repr failed: %s>" % (type(value).__name__, exc)
    if len(text) > limit:
        text = text[: limit - 20] + "...[+%d chars]" % (len(text) - limit + 20)
    return text


def _clip(text, n):
    text = text.replace("\n", "\\n")
    return text if len(text) <= n else text[:n] + "..."


def _size(value):
    try:
        return len(value)
    except Exception:
        return None


def describe(value, name="context"):
    """A compact structural summary of a value, safe for any size."""
    lines = []
    kind = type(value).__name__
    if value is None:
        return "%s is None (no data was supplied)" % name
    if isinstance(value, str):
        n = len(value)
        lines.append("%s: str, %s chars, %s lines" % (name, format(n, ","), format(value.count("\n") + 1, ",")))
        hints = []
        probe = value[:4000].lstrip()
        if probe[:1] in "{[":
            hints.append("looks like JSON (try json.loads)")
        if re.search(r"^#{1,6} ", value[:20000], re.M):
            hints.append("has markdown headings")
        first = value[:2000].split("\n", 1)[0]
        if first.count(",") >= 3 or first.count("\t") >= 3:
            hints.append("first line looks like a delimited header")
        if hints:
            lines.append("  hints: " + "; ".join(hints))
        if n <= 1200:
            lines.append("  full text: " + repr(value))
        else:
            lines.append("  starts: " + repr(value[:600]))
            lines.append("  ends:   " + repr(value[-300:]))
        return "\n".join(lines)
    if isinstance(value, dict):
        lines.append("%s: dict, %d keys" % (name, len(value)))
        for i, (k, v) in enumerate(value.items()):
            if i >= 40:
                lines.append("  ... %d more keys" % (len(value) - 40))
                break
            size = _size(v)
            meta = type(v).__name__ + ("" if size is None else ", len %s" % format(size, ","))
            lines.append("  [%r] (%s): %s" % (k, meta, _clip(brief(v, 160), 160)))
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        lines.append("%s: %s, %s items" % (name, kind, format(len(value), ",")))
        kinds = collections.Counter(type(v).__name__ for v in itertools.islice(value, 2000))
        if kinds:
            lines.append("  item types: " + ", ".join("%s x%d" % kv for kv in kinds.most_common(5)))
        sizes = [s for s in (_size(v) for v in itertools.islice(value, 2000)) if s is not None]
        if sizes:
            lines.append("  item sizes: min %d, max %d, total %s" % (min(sizes), max(sizes), format(sum(sizes), ",")))
        for i, v in enumerate(value[:3]):
            lines.append("  [%d]: %s" % (i, _clip(brief(v, 300), 300)))
        if len(value) > 4:
            lines.append("  ...")
        if len(value) > 3:
            lines.append("  [%d]: %s" % (len(value) - 1, _clip(brief(value[-1], 300), 300)))
        return "\n".join(lines)
    size = _size(value)
    shape = getattr(value, "shape", None)
    lines.append(
        "%s: %s%s%s"
        % (
            name,
            type(value).__module__ + "." + kind,
            "" if size is None else ", len %s" % format(size, ","),
            "" if shape is None else ", shape %s" % (shape,),
        )
    )
    lines.append("  " + _clip(brief(value, 800), 800))
    return "\n".join(lines)


def chunk(text, size=20000, overlap=0, sep=None):
    """Split text into pieces of at most ``size`` characters.

    Pieces end on a natural boundary when one exists in the last quarter of
    the window: ``sep`` if given, otherwise a blank line, then a newline, then
    a space. ``overlap`` repeats that many trailing characters at the start of
    the next piece. Lists are grouped into sub-lists whose total length stays
    under ``size``.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if isinstance(text, (list, tuple)):
        groups, current, used = [], [], 0
        for item in text:
            cost = len(item) if hasattr(item, "__len__") else 1
            if current and used + cost > size:
                groups.append(current)
                current, used = [], 0
            current.append(item)
            used += cost
        if current:
            groups.append(current)
        return groups
    if not isinstance(text, str):
        raise TypeError("chunk() takes a str or a list, got %s" % type(text).__name__)
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be >= 0 and smaller than size")
    seps = [sep] if sep else ["\n\n", "\n", " "]
    pieces, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            floor = start + (size * 3) // 4
            for s in seps:
                cut = text.rfind(s, floor, end)
                if cut != -1:
                    end = cut + len(s)
                    break
        pieces.append(text[start:end])
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return pieces


Hit = collections.namedtuple("Hit", "start end match snippet")


def grep(pattern, text=None, window=120, limit=50, flags=re.IGNORECASE):
    """Regex search returning up to ``limit`` hits with surrounding text.

    Each hit is ``Hit(start, end, match, snippet)`` where ``snippet`` includes
    ``window`` characters on each side. Searches ``context`` when ``text`` is
    omitted.
    """
    if text is None:
        text = _NS.get("context")
    if not isinstance(text, str):
        raise TypeError("grep() searches a str; got %s. Pass text=... explicitly." % type(text).__name__)
    hits = []
    for m in re.finditer(pattern, text, flags):
        a, b = max(0, m.start() - window), min(len(text), m.end() + window)
        hits.append(Hit(m.start(), m.end(), m.group(0), text[a:b]))
        if len(hits) >= limit:
            break
    return hits


def peek(value=None, name=None):
    """Print a structural summary of a value (``context`` by default)."""
    if value is None and name is None:
        value, name = _NS.get("context"), "context"
    print(describe(value, name or "value"))


# --------------------------------------------------------------------------
# host calls
# --------------------------------------------------------------------------

_WIRE = None
_NS = {}
_PENDING = {}
_PENDING_LOCK = threading.Lock()
_IDS = itertools.count(1)
_TOOL_NAMES = set()


class _Slot:
    __slots__ = ("event", "reply")

    def __init__(self):
        self.event = threading.Event()
        self.reply = None


def host_call(kind, **payload):
    slot = _Slot()
    cid = next(_IDS)
    with _PENDING_LOCK:
        _PENDING[cid] = slot
    try:
        _WIRE.send(dict(payload, op="call", id=cid, kind=kind))
        slot.event.wait()
    finally:
        with _PENDING_LOCK:
            _PENDING.pop(cid, None)
    reply = slot.reply
    if reply is None:
        raise SubcallError("host went away")
    if not reply.get("ok"):
        raise _ERRORS.get(reply.get("etype"), SubcallError)(reply.get("error", "call failed"))
    value = decode(reply.get("value"))
    failed = reply.get("failed")
    if failed:
        for index, message in failed.items():
            value[int(index)] = Failed(message)
        print(
            "[%s] %d of %d items failed; failed entries are falsy Failed(...) objects" % (kind, len(failed), len(value))
        )
    return value


def _check_schema(schema):
    if schema is not None and not isinstance(schema, dict):
        raise TypeError("schema must be a JSON Schema dict, e.g. {'type': 'array', 'items': {'type': 'string'}}")
    return schema


def ask(prompt, data=None, *, schema=None):
    """One sub-model completion. Returns text, or parsed JSON when ``schema`` is given."""
    return host_call(
        "ask",
        prompt=as_text(prompt),
        data=None if data is None else as_text(data),
        schema=_check_schema(schema),
    )


def ask_map(prompt, items=None, *, schema=None):
    """Run ``ask`` over many items in parallel; results keep the input order.

    ``ask_map(prompt, items)`` applies one prompt to every item.
    ``ask_map([p1, p2, ...])`` runs a list of independent prompts.
    """
    if items is None:
        if isinstance(prompt, str):
            raise TypeError("ask_map(prompt, items) needs items, or pass a list of prompts")
        jobs = [{"prompt": as_text(p), "data": None} for p in prompt]
    else:
        text = as_text(prompt)
        jobs = [{"prompt": text, "data": as_text(item)} for item in items]
    if not jobs:
        return []
    return host_call("ask_map", jobs=jobs, schema=_check_schema(schema))


def _split_tools(tools):
    if tools is None:
        return None, []
    names, sources = [], []
    for t in tools:
        if isinstance(t, str):
            names.append(t)
        elif callable(t) and getattr(t, "__name__", None) in _TOOL_NAMES:
            names.append(t.__name__)
        elif callable(t):
            try:
                sources.append(inspect.getsource(t))
            except (OSError, TypeError):
                raise TypeError("cannot pass %r to a sub-agent: its source is unavailable" % (t,)) from None
        else:
            raise TypeError("tools must be tool names or functions, got %r" % (t,))
    return names, sources


def delegate(task, data=None, *, schema=None, tools=None, whole=False):
    """Hand a sub-task to a sub-agent that has its own REPL. Returns its answer."""
    names, sources = _split_tools(tools)
    return host_call(
        "delegate",
        task=as_text(task),
        data=jsonable(data),
        schema=_check_schema(schema),
        tools=names,
        helpers=sources,
        whole=bool(whole),
    )


def delegate_map(task, items, *, schema=None, tools=None):
    """Run one sub-agent per item in parallel; results keep the input order."""
    names, sources = _split_tools(tools)
    items = [jsonable(item) for item in items]
    if not items:
        return []
    return host_call(
        "delegate_map", task=as_text(task), items=items, schema=_check_schema(schema), tools=names, helpers=sources
    )


def submit(value=None):
    """Finish the task with ``value`` as the answer. Nothing after it runs."""
    raise _Submit(value)


def _make_tool(meta):
    name = meta["name"]

    def tool(*args, **kwargs):
        return host_call("tool", name=name, args=jsonable(list(args)), kwargs=jsonable(kwargs))

    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = meta.get("doc") or None
    tool.__rlm_signature__ = meta.get("signature") or "(...)"
    return tool


# --------------------------------------------------------------------------
# cell execution
# --------------------------------------------------------------------------

_CELLS = itertools.count(1)
_DEFS = {}
_LOOP = None
_BASELINE = set()


def _show(value):
    if value is not None:
        print(brief(value))


def _format_error(exc):
    if isinstance(exc, SyntaxError):
        return "".join(traceback.format_exception_only(type(exc), exc)).rstrip()
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith("<cell ")]
    parts = []
    if frames:
        parts.append("Traceback (most recent call last):\n")
        for frame in frames[-8:]:
            parts.append("  %s, line %s, in %s\n" % (frame.filename, frame.lineno, frame.name))
            if frame.line:
                parts.append("    %s\n" % frame.line.strip())
    parts.extend(traceback.format_exception_only(type(exc), exc))
    return "".join(parts).rstrip()


def _remember_defs(tree, code):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            source = ast.get_source_segment(code, node)
            if source:
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                lines = code.splitlines()
                _DEFS[node.name] = "\n".join(lines[first - 1 : node.end_lineno])


def run_cell(code, capture):
    global _LOOP
    number = next(_CELLS)
    filename = "<cell %d>" % number
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    outcome = {"error": None, "submitted": False, "value": None, "interrupted": False}
    try:
        tree = ast.parse(code, filename)
        _remember_defs(tree, code)
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = tree.body[-1]
            shown = ast.Expr(ast.Call(ast.Name("__rlm_show__", ast.Load()), [last.value], []))
            tree.body[-1] = ast.copy_location(shown, last)
            ast.fix_missing_locations(tree)
        compiled = compile(tree, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        if compiled.co_flags & inspect.CO_COROUTINE:
            if _LOOP is None:
                _LOOP = asyncio.new_event_loop()
            _LOOP.run_until_complete(eval(compiled, _NS))
        else:
            exec(compiled, _NS)
    except _Submit as done:
        value = done.value
        if callable(value) or isinstance(value, types.ModuleType):
            outcome["error"] = (
                "submit() needs data, not %s. Pass the value itself (str, number, list, dict...)."
                % type(value).__name__
            )
        else:
            outcome["submitted"] = True
            outcome["value"] = jsonable(value)
    except KeyboardInterrupt:
        outcome["interrupted"] = True
        outcome["error"] = "Interrupted: the cell ran past its time limit and was stopped. Variables are intact."
    except SystemExit:
        outcome["error"] = "SystemExit ignored: use submit(value) to finish."
    except BaseException as exc:  # noqa: BLE001 - everything is reported to the model
        outcome["error"] = _format_error(exc)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    _drain_fd_output()
    outcome["stdout"] = capture.getvalue()
    return outcome


# --------------------------------------------------------------------------
# sessions: snapshot, restore, inventory
# --------------------------------------------------------------------------


def _user_names():
    for name, value in list(_NS.items()):
        if name.startswith("_") or isinstance(value, types.ModuleType):
            continue
        if name in _BASELINE and name != "context":
            continue
        yield name, value


def inventory():
    rows = []
    for name, value in _user_names():
        if name == "context":
            continue
        kind = "def" if name in _DEFS and callable(value) else type(value).__name__
        rows.append({"name": name, "type": kind, "preview": _clip(brief(value, 200), 200)})
    return rows


def snapshot(max_bytes):
    saved, skipped, defs = {}, {}, {}
    for name, value in _user_names():
        if name in _DEFS and callable(value):
            defs[name] = _DEFS[name]
            continue
        try:
            blob = pickle.dumps(value, protocol=4)
        except Exception as exc:
            skipped[name] = "not picklable (%s)" % type(exc).__name__
            continue
        if len(blob) > max_bytes:
            skipped[name] = "too large (%s bytes)" % format(len(blob), ",")
            continue
        saved[name] = base64.b64encode(blob).decode("ascii")
    return {"vars": saved, "defs": defs, "skipped": skipped}


def restore(state):
    failed = {}
    for name, source in (state.get("defs") or {}).items():
        try:
            filename = "<restored %s>" % name
            linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
            exec(compile(source, filename, "exec"), _NS)
            _DEFS[name] = source
        except Exception as exc:
            failed[name] = "%s: %s" % (type(exc).__name__, exc)
    for name, blob in (state.get("vars") or {}).items():
        try:
            _NS[name] = pickle.loads(base64.b64decode(blob))
        except Exception as exc:
            failed[name] = "%s: %s" % (type(exc).__name__, exc)
    return failed


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


_SYNC_MARK = b"\x00\x01rlm-sync\x01\x00"
_SYNCED = threading.Event()


def _pump(fd, capture):
    """Copy raw bytes written to the process-level stdout/stderr into the capture."""
    decoder = __import__("codecs").getincrementaldecoder("utf-8")("replace")
    held = b""
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:
            return
        if not data:
            return
        data = held + data
        held = b""
        seen = _SYNC_MARK in data
        if seen:
            data = data.replace(_SYNC_MARK, b"")
        else:
            # keep back a possible partial marker at the end of this read
            for k in range(len(_SYNC_MARK) - 1, 0, -1):
                if data.endswith(_SYNC_MARK[:k]):
                    held, data = data[-k:], data[:-k]
                    break
        if data:
            capture.write(decoder.decode(data))
        if seen:
            _SYNCED.set()


def _drain_fd_output():
    """Wait until everything already written to fd 1/2 has reached the capture."""
    if not _PUMPING:
        return
    _SYNCED.clear()
    try:
        os.write(1, _SYNC_MARK)
    except OSError:
        return
    _SYNCED.wait(1.0)


_PUMPING = []


def _apply_limits(config):
    memory_mb = config.get("memory_mb")
    if memory_mb:
        try:
            import resource

            limit = int(memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass


def main():
    global _WIRE, _NS

    # Claim private copies of the protocol streams, then point the process's
    # real stdin at nothing and its stdout/stderr at the capture buffer, so
    # neither model code nor a child process can read or corrupt the protocol.
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdin = open(os.devnull, "r")

    _WIRE = Wire(proto_in, proto_out)
    init = _WIRE.recv()
    if not init or init.get("op") != "init":
        os._exit(2)
    config = init.get("config") or {}
    capture = Capture(config.get("capture_limit", 200000))

    try:
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        threading.Thread(target=_pump, args=(read_fd, capture), daemon=True).start()
        _PUMPING.append(True)
    except OSError:
        pass
    sys.stdout = sys.stderr = capture

    _apply_limits(config)

    module = types.ModuleType("__main__")
    sys.modules["__main__"] = module
    _NS = module.__dict__
    _NS.update(
        __builtins__=builtins,
        __rlm_show__=_show,
        context=decode(init.get("context")),
        ask=ask,
        ask_map=ask_map,
        delegate=delegate,
        delegate_map=delegate_map,
        submit=submit,
        peek=peek,
        chunk=chunk,
        grep=grep,
        Failed=Failed,
        SubcallError=SubcallError,
        BudgetExceeded=BudgetExceeded,
        DelegationRefused=DelegationRefused,
        ToolError=ToolError,
    )
    for meta in init.get("tools") or []:
        _NS[meta["name"]] = _make_tool(meta)
        _TOOL_NAMES.add(meta["name"])
    problems = []
    for i, source in enumerate(init.get("helpers") or []):
        try:
            filename = "<helper %d>" % (i + 1)
            linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
            tree = ast.parse(source, filename)
            _remember_defs(tree, source)
            exec(compile(tree, filename, "exec"), _NS)
        except Exception as exc:
            problems.append("helper %d failed to load: %s: %s" % (i + 1, type(exc).__name__, exc))
    _BASELINE.update(_NS.keys())
    _BASELINE.difference_update(_DEFS.keys())

    inbox = collections.deque()
    ready = threading.Condition()

    def reader():
        while True:
            try:
                msg = _WIRE.recv()
            except Exception:
                msg = None
            if msg is None:
                os._exit(0)
            if msg.get("op") == "reply":
                with _PENDING_LOCK:
                    slot = _PENDING.get(msg.get("id"))
                if slot is not None:
                    slot.reply = msg
                    slot.event.set()
                continue
            with ready:
                inbox.append(msg)
                ready.notify()

    threading.Thread(target=reader, daemon=True).start()

    helper_names = sorted(n for n in _DEFS if n in _NS)
    _WIRE.send(
        {
            "op": "ready",
            "summary": describe(_NS.get("context")),
            "size": len(as_text(_NS.get("context"))) if _NS.get("context") is not None else 0,
            "python": "%d.%d.%d" % sys.version_info[:3],
            "helpers": helper_names,
            "problems": problems,
        }
    )

    while True:
        with ready:
            while not inbox:
                try:
                    ready.wait()
                except KeyboardInterrupt:
                    pass  # a late interrupt for a cell that already finished
            msg = inbox.popleft()
        op = msg.get("op")
        try:
            if op == "exec":
                capture.reset()
                started = time.monotonic()
                outcome = run_cell(msg.get("code", ""), capture)
                outcome.update(op="done", id=msg.get("id"), seconds=round(time.monotonic() - started, 4))
                _WIRE.send(outcome)
            elif op == "set_context":
                _NS["context"] = decode(msg.get("context"))
                value = _NS["context"]
                _WIRE.send(
                    {
                        "op": "context",
                        "id": msg.get("id"),
                        "summary": describe(value),
                        "size": len(as_text(value)) if value is not None else 0,
                    }
                )
            elif op == "inventory":
                _WIRE.send({"op": "inventory", "id": msg.get("id"), "rows": inventory()})
            elif op == "snapshot":
                state = snapshot(int(msg.get("max_bytes") or 50_000_000))
                _WIRE.send({"op": "snapshot", "id": msg.get("id"), "state": state})
            elif op == "restore":
                failed = restore(msg.get("state") or {})
                _WIRE.send({"op": "restored", "id": msg.get("id"), "failed": failed})
            elif op == "shutdown":
                os._exit(0)
        except KeyboardInterrupt:
            continue
        except BaseException as exc:  # noqa: BLE001
            _WIRE.send({"op": "fault", "id": msg.get("id"), "error": "%s: %s" % (type(exc).__name__, exc)})


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except Exception:
        pass
    main()
