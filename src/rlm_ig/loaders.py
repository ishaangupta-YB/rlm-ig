"""Turn files into context values."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from .errors import ConfigError

#: files larger than this inside a directory are skipped (a single file given
#: directly is always loaded)
DIR_FILE_LIMIT = 5_000_000
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}


def load(path: str | os.PathLike) -> Any:
    """Load one path as a context value.

    ``.json`` becomes the decoded value, ``.jsonl``/``.ndjson`` a list of
    records, ``.csv``/``.tsv`` a list of row dicts, a directory a dict of
    relative path to text, and anything else its text.
    """
    target = Path(path).expanduser()
    if not target.exists():
        raise ConfigError(f"no such file or directory: {path}")
    if target.is_dir():
        return _load_dir(target)
    suffix = target.suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(_read(target))
        if suffix in (".jsonl", ".ndjson"):
            return [json.loads(line) for line in _read(target).splitlines() if line.strip()]
        if suffix in (".csv", ".tsv"):
            with open(target, newline="", encoding="utf-8", errors="replace") as handle:
                return list(csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ","))
    except ValueError as exc:
        raise ConfigError(f"{path}: could not parse as {suffix[1:]}: {exc}") from exc
    return _read(target)


def load_many(paths: list[str]) -> Any:
    """One path loads as itself; several become a dict keyed by file name."""
    if not paths:
        return None
    if len(paths) == 1:
        return load(paths[0])
    out: dict[str, Any] = {}
    for path in paths:
        name = Path(path).name or str(path)
        key, n = name, 2
        while key in out:
            key, n = f"{name} ({n})", n + 1
        out[key] = load(path)
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"\0" in handle.read(2048)
    except OSError:
        return True


def _load_dir(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            file = Path(folder) / name
            if name.startswith(".") or not file.is_file():
                continue
            try:
                if file.stat().st_size > DIR_FILE_LIMIT or _looks_binary(file):
                    continue
                out[file.relative_to(root).as_posix()] = _read(file)
            except OSError:
                continue
    if not out:
        raise ConfigError(f"{root}: no readable text files found")
    return out
