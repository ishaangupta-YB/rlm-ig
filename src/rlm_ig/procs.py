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

_live: dict[int, bool] = {}  # pid -> whether it leads its own process group


def track(pid: int, *, group: bool = False) -> None:
    _live[pid] = group


def untrack(pid: int) -> None:
    _live.pop(pid, None)


def kill(pid: int, *, group: bool = False, sig: int | None = None) -> None:
    """Signal a process, or its whole group when it was started as a group leader.

    Some CLIs are thin launchers that spawn the real program as a child; only
    a group signal reaches both.
    """
    if sig is None:
        sig = signal.SIGKILL if os.name == "posix" else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if group and os.name == "posix":
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)


@atexit.register
def _reap() -> None:
    for pid, group in list(_live.items()):
        kill(pid, group=group)
    _live.clear()


def cancel_on_signals(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    """Cancel ``task`` on SIGTERM/SIGHUP so its cleanup handlers run."""
    if os.name != "posix":
        return
    for name in ("SIGTERM", "SIGHUP"):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(getattr(signal, name), task.cancel)
