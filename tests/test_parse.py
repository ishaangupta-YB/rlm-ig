"""Building ``result.parsed`` from a validated answer, nested dataclasses included."""

from __future__ import annotations

import dataclasses
import enum
from typing import Literal, Optional, TypedDict

import pytest

from rlm_ig.schema import build

from helpers import block


class Severity(enum.Enum):
    LOW = "low"
    HIGH = "high"


@dataclasses.dataclass
class Incident:
    source_ip: str
    attempts: int
    level: Literal["low", "high"] = "low"


@dataclasses.dataclass
class Report:
    summary: str
    incidents: list[Incident]
    worst: Incident | None = None
    by_user: dict[str, Incident] = dataclasses.field(default_factory=dict)
    pair: tuple[Incident, ...] = ()


class Row(TypedDict):
    sku: str
    qty: int


@dataclasses.dataclass
class Order:
    rows: list[Row]
    note: Optional[str] = None  # noqa: UP045 - the pre-3.10 spelling must keep working


@dataclasses.dataclass
class Picky:
    n: int

    def __post_init__(self):
        if self.n < 0:
            raise ValueError("n must not be negative")


@dataclasses.dataclass
class Holder:
    picky: Picky


INCIDENT = {"source_ip": "10.0.0.1", "attempts": 7}
REPORT = {
    "summary": "two hosts",
    "incidents": [INCIDENT, {"source_ip": "10.0.0.2", "attempts": 3, "level": "high"}],
    "worst": INCIDENT,
    "by_user": {"root": INCIDENT},
    "pair": [INCIDENT],
}


# --------------------------------------------------------------- end to end


def test_nested_dataclasses_are_built_in_a_run(solve):
    result = solve([block(f"submit({REPORT!r})")], schema=Report)
    assert result.ok and result.answer == REPORT
    parsed = result.parsed
    assert isinstance(parsed, Report)
    assert isinstance(parsed.incidents[0], Incident)
    assert parsed.incidents[0].source_ip == "10.0.0.1"
    assert parsed.incidents[1].level == "high"
    assert isinstance(parsed.worst, Incident)
    assert isinstance(parsed.by_user["root"], Incident)
    assert parsed.pair == (Incident("10.0.0.1", 7),)


def test_optional_nested_dataclass_may_be_null(solve):
    answer = {"summary": "none", "incidents": [], "worst": None}
    result = solve([block(f"submit({answer!r})")], schema=Report)
    assert result.parsed == Report("none", [], None)


def test_a_failing_build_leaves_parsed_none_and_keeps_the_answer(solve):
    result = solve([block("submit({'picky': {'n': -1}})")], schema=Holder)
    assert result.ok and result.answer == {"picky": {"n": -1}}
    assert result.parsed is None


def test_typeddict_values_stay_dicts(solve):
    answer = {"rows": [{"sku": "A1", "qty": 2}]}
    result = solve([block(f"submit({answer!r})")], schema=Order)
    assert result.parsed == Order([{"sku": "A1", "qty": 2}])
    assert type(result.parsed.rows[0]) is dict


def test_parsed_stays_none_for_plain_type_hints(solve):
    assert solve([block("submit(['a', 'b'])")], schema=list[str]).parsed is None
    assert solve([block("submit({'sku': 'A1', 'qty': 2})")], schema=Row).parsed is None


# ------------------------------------------------------------------- build()


def test_build_containers():
    assert build(list[Incident], [INCIDENT]) == [Incident("10.0.0.1", 7)]
    assert build(tuple[int, str], [1, "a"]) == (1, "a")
    assert build(dict[str, list[int]], {"a": [1]}) == {"a": [1]}
    assert build(set[str], ["a", "a"]) == {"a"}
    assert build(Optional[Incident], None) is None  # noqa: UP045
    assert build(Incident | None, INCIDENT) == Incident("10.0.0.1", 7)


def test_build_passes_literal_enum_and_any_values_through():
    assert build(Literal["a", "b"], "a") == "a"
    assert build(Severity, "high") == "high"
    assert build(Row, {"sku": "x", "qty": 1}) == {"sku": "x", "qty": 1}


def test_build_picks_the_union_member_that_fits():
    assert build(str | Incident, INCIDENT) == Incident("10.0.0.1", 7)
    assert build(str | Incident, "plain") == "plain"
    assert build(int | float, 2.5) == 2.5


@pytest.mark.parametrize(
    ("hint", "value"),
    [
        (Incident, ["not", "an", "object"]),
        (Incident, {"source_ip": "x", "attempts": 1, "extra": True}),
        (list[Incident], {"source_ip": "x"}),
        (tuple[int, int], [1]),
        (dict[str, int], [1]),
        (int, "3"),
        (str | int, [1]),
    ],
)
def test_build_rejects_values_that_do_not_fit(hint, value):
    with pytest.raises((TypeError, ValueError)):
        build(hint, value)


def test_build_handles_pydantic_models_at_any_depth():
    pytest.importorskip("pydantic")
    from pydantic_models import Box, Item

    built = build(Box, {"items": [{"name": "a"}]})
    assert isinstance(built.items[0], Item) and built.items[0].name == "a"


def test_classes_defined_in_a_function_still_build_the_top_level():
    # with `from __future__ import annotations` the field types of a local class are strings that
    # cannot be resolved later; the top level is still built, as before nested building existed
    @dataclasses.dataclass
    class Local:
        name: str

    assert build(Local, {"name": "a"}) == Local("a")
