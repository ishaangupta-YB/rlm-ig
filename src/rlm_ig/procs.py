"""Track child processes so none outlive the interpreter.

Model CLIs keep generating (and billing) after their parent disappears unless
someone stops them. Every long-lived child is registered here and killed at
interpreter exit if it is still around; :func:`cancel_on_signals` makes a
polite ``SIGTERM`` take the same clean path as Ctrl-C.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import signal

_live: set[int] = set()


def track(pid: int) -> None:
    _live.add(pid)


def untrack(pid: int) -> None:
    _live.discard(pid)


@atexit.register
def _reap() -> None:
    for pid in list(_live):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
    _live.clear()


def cancel_on_signals(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    """Cancel ``task`` on SIGTERM/SIGHUP so its cleanup handlers run."""
    if os.name != "posix":
        return
    for name in ("SIGTERM", "SIGHUP"):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(getattr(signal, name), task.cancel)
