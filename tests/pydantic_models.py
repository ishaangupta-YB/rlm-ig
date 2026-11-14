"""Pydantic models at module level, so their annotations resolve (imported only when Pydantic is installed)."""

from __future__ import annotations

import dataclasses

import pydantic


class Item(pydantic.BaseModel):
    name: str


@dataclasses.dataclass
class Box:
    items: list[Item]
