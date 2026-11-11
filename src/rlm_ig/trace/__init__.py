"""Traces: one JSON line per event, written as the run happens."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..engine.events import Event


def new_trace_path(trace_dir: str, task: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:40].strip("-") or "run"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(trace_dir).expanduser() / f"{stamp}-{slug}-{secrets.token_hex(2)}.jsonl"


class TraceWriter:
    """An event sink that appends to a JSONL file, flushing every line."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._file = None
        self._failed = False

    def __call__(self, event: Event) -> None:
        if self._failed:
            return
        try:
            if self._file is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(self.path, "a", encoding="utf-8")
            self._file.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
            self._file.flush()
        except OSError:
            self._failed = True  # an unwritable trace must not take the run down

    @property
    def written(self) -> bool:
        return self._file is not None and not self._failed

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def read_trace(path: str | os.PathLike) -> list[dict[str, Any]]:
    """Load a trace file. Truncated trailing lines (a killed run) are skipped."""
    return list(iter_trace(path))


def iter_trace(path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def latest_trace(trace_dir: str) -> Path | None:
    folder = Path(trace_dir).expanduser()
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


__all__ = ["TraceWriter", "read_trace", "iter_trace", "latest_trace", "new_trace_path"]
