from __future__ import annotations

import dataclasses
from typing import Literal, Optional, TypedDict

import pytest

from rlm_ig.errors import SchemaError
from rlm_ig.schema import parse_json_reply, to_schema, validate
from rlm_ig.text import clip_middle, extract_code

# ------------------------------------------------------------- extract_code


def test_tagged_block_is_extracted():
    assert extract_code("I will look.\n```python\nprint(1)\n```\nthen more") == "print(1)"


def test_multiple_tagged_blocks_are_joined_in_order():
    reply = "```python\na = 1\n```\ntext\n```py\nb = 2\n```"
    assert extract_code(reply) == "a = 1\n\nb = 2"


def test_other_languages_are_ignored_when_python_is_present():
    reply = '```json\n{"a": 1}\n```\n```python\nx = 1\n```'
    assert extract_code(reply) == "x = 1"


def test_untagged_block_is_accepted_only_if_it_is_python():
    assert extract_code("```\nx = 1\n```") == "x = 1"
    assert extract_code("```\nthis is not python at all\n```") is None


def test_prose_only_reply_has_no_code():
    assert extract_code("The answer is 42.") is None
    assert extract_code("Use `x` inline, never a block.") is None


def test_unterminated_final_block_is_kept():
    assert extract_code("ok\n```python\nprint('cut off')") == "print('cut off')"


def test_fence_inside_a_string_does_not_end_the_block():
    reply = 'Building markdown.\n```python\ndoc = """\n```\ninner\n```\n"""\nprint(doc)\n```\nDone.'
    code = extract_code(reply)
    assert code.endswith("print(doc)") and code.count("```") == 2
    compile(code, "<t>", "exec")


def test_repl_tag_and_tilde_fences_work():
    assert extract_code("```repl\nx = 1\n```") == "x = 1"
    assert extract_code("~~~python\nx = 2\n~~~") == "x = 2"


def test_clip_middle_keeps_both_ends():
    text = "A" * 500 + "B" * 500
    clipped = clip_middle(text, 100)
    assert clipped.startswith("A" * 60) and clipped.endswith("B" * 40)
    assert "900 characters omitted" in clipped
    assert clip_middle("short", 100) == "short"


# ------------------------------------------------------------------ schemas


def test_type_hints_become_schemas():
    assert to_schema(int) == {"type": "integer"}
    assert to_schema(list[str]) == {"type": "array", "items": {"type": "string"}}
    assert to_schema(dict[str, float]) == {"type": "object", "additionalProperties": {"type": "number"}}
    legacy = Optional[int]  # noqa: UP045 - the pre-3.10 spelling must keep working
    assert to_schema(legacy) == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert to_schema(int | str) == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    assert to_schema(Literal["a", "b"]) == {"enum": ["a", "b"]}
    assert to_schema({"type": "string"}) == {"type": "string"}
    assert to_schema(None) is None


def test_dataclass_and_typeddict_schemas():
    @dataclasses.dataclass
    class Verdict:
        title: str
        score: float
        tags: list[str] = dataclasses.field(default_factory=list)

    class Row(TypedDict):
        name: str
        n: int

    schema = to_schema(Verdict)
    assert schema["required"] == ["title", "score"]
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert to_schema(list[Row])["items"]["properties"] == {"name": {"type": "string"}, "n": {"type": "integer"}}


def test_unsupported_type_is_a_schema_error():
    with pytest.raises(SchemaError):
        to_schema(object)


def test_validate_reports_paths():
    schema = {
        "type": "object",
        "required": ["name", "items"],
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "items": {"type": "array", "items": {"type": "integer", "minimum": 0}, "maxItems": 3},
        },
        "additionalProperties": False,
    }
    assert validate({"name": "ok", "items": [1, 2]}, schema) == []
    problems = validate({"name": "x", "items": [1, -2, "3"], "extra": 1}, schema)
    text = "\n".join(problems)
    assert "$.name: shorter than 2" in text
    assert "$.items[1]: below minimum 0" in text
    assert "$.items[2]: expected integer, got string" in text
    assert "unexpected key 'extra'" in text
    assert validate({"items": []}, schema) == ["$: missing required key 'name'"]


def test_validate_type_edge_cases():
    assert validate(True, {"type": "integer"}) != []
    assert validate(3.0, {"type": "integer"}) == []
    assert validate(3, {"type": "number"}) == []
    assert validate(None, {"type": ["string", "null"]}) == []
    assert validate("x", {"enum": ["a", "b"]}) != []
    assert validate("abc", {"type": "string", "pattern": "^a.c$"}) == []


def test_validate_combinators_and_refs():
    schema = {
        "$defs": {"id": {"type": "integer", "minimum": 1}},
        "type": "object",
        "properties": {
            "id": {"$ref": "#/$defs/id"},
            "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "kind": {"oneOf": [{"const": "a"}, {"const": "b"}]},
        },
    }
    assert validate({"id": 3, "value": None, "kind": "a"}, schema) == []
    problems = validate({"id": 0, "value": 5, "kind": "c"}, schema)
    assert len(problems) == 3


def test_pydantic_models_work_when_installed():
    pydantic = pytest.importorskip("pydantic")

    class Item(pydantic.BaseModel):
        name: str
        qty: int

    schema = to_schema(list[Item])
    assert validate([{"name": "a", "qty": 1}], schema) == []
    assert validate([{"name": "a"}], schema) != []


def test_parse_json_reply_variants():
    assert parse_json_reply('{"a": 1}') == {"a": 1}
    assert parse_json_reply("Here you go:\n```json\n[1, 2]\n```") == [1, 2]
    assert parse_json_reply('Sure. The result is {"ok": true} as requested.') == {"ok": True}
    with pytest.raises(ValueError):
        parse_json_reply("no json here")
