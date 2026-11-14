from __future__ import annotations

import dataclasses
from typing import Literal, Optional, TypedDict

import pytest

from rlm_ig.errors import SchemaError
from rlm_ig.schema import parse_json_reply, to_schema, validate
from rlm_ig.text import clip_middle, extract_code, extract_code_with_repairs

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


# ------------------------------------------------- repairing mangled replies

# A reply seen live from a small model: stray tool-call markup before the
# closing fence, and an unclosed block followed by prose and another block.
MANGLED_REPLY = (
    "I'll count the failed passwords per IP.\n"
    "```python\n"
    "lines = context.splitlines()\n"
    "print(len(lines))\n"
    "</parameter>\n"
    "```\n"
    "\n"
    "Now the counts:\n"
    "```python\n"
    "from collections import Counter\n"
    "top = Counter(lines).most_common(5)\n"
    "</parameter>\n"
    "\n"
    "Now I'll submit the answer:\n"
    "```python\n"
    "submit(top)\n"
    "</parameter>"
)
REPAIRED = "lines = context.splitlines()\nprint(len(lines))\n\nfrom collections import Counter\ntop = Counter(lines).most_common(5)\n\nsubmit(top)"


def test_mangled_reply_is_repaired_and_the_repairs_are_reported():
    code, repairs = extract_code_with_repairs(MANGLED_REPLY)
    assert code == REPAIRED
    assert extract_code(MANGLED_REPLY) == REPAIRED
    text = ", ".join(repairs)
    assert "removed 3 stray markup lines" in text and "dropped prose after an unclosed block" in text


def test_unclosed_block_before_a_closed_one_is_repaired_too():
    # the same reply, but the last block is closed: its fence must not count as the earlier block's closer
    code, repairs = extract_code_with_repairs(MANGLED_REPLY + "\n```")
    assert code == REPAIRED and repairs


def test_a_genuine_syntax_error_is_left_alone():
    assert extract_code("```python\nx = (1,\nprint(x)\n```") == "x = (1,\nprint(x)"
    assert extract_code_with_repairs("```python\nx = (1,\nprint(x)\n```") == ("x = (1,\nprint(x)", [])


def test_a_tag_line_inside_a_string_is_never_stripped():
    reply = '```python\nhtml = """\n</parameter>\n"""\nprint(html)\n```'
    code, repairs = extract_code_with_repairs(reply)
    assert code == 'html = """\n</parameter>\n"""\nprint(html)' and repairs == []


def test_stray_markup_in_a_terminated_block_is_removed():
    code, repairs = extract_code_with_repairs("```python\nx = 1\nprint(x)\n</parameter>\n```")
    assert code == "x = 1\nprint(x)" and repairs == ["removed 1 stray markup line"]


@pytest.mark.parametrize(
    "markup", ["</parameter>", "</invoke>", '<parameter name="code">', "</function_calls>", "  <br/>  "]
)
def test_every_kind_of_lone_markup_line_is_removed(markup):
    assert extract_code(f"```python\n{markup}\nx = 1\n{markup}\n```") == "x = 1"


@pytest.mark.parametrize(
    ("reply", "today"),
    [
        # unterminated, runs to the end of the reply
        ("```python\nx = 1\n\ny = (", "x = 1\n\ny = ("),
        # cut at a following fence, but `y = (` is code, not prose
        ("```python\nx = 1\n\ny = (\n```python\nz = 2\n```", "x = 1\n\ny = (\n```python\nz = 2"),
        # a terminated block is never paragraph-trimmed
        ("```python\nx = 1\n\nNow I'll go:\n```", "x = 1\n\nNow I'll go:"),
    ],
)
def test_broken_code_in_a_last_paragraph_is_never_dropped(reply, today):
    assert extract_code_with_repairs(reply) == (today, [])
    assert extract_code(reply) == today


def test_prose_is_dropped_only_when_it_looks_like_prose():
    # more than three lines, an indented line, or code characters: not prose, so no repair
    for paragraph in ("One.\nTwo.\nThree.\nFour.", "Now:\n  indented", "Then call f() on it:"):
        reply = f"```python\nx = 1\n\n{paragraph}\n```python\nprint(x)\n```"
        code, repairs = extract_code_with_repairs(reply)
        assert repairs == [] and paragraph in code
    code, repairs = extract_code_with_repairs("```python\nx = 1\n\nNext I'll print it.\n```python\nprint(x)\n```")
    assert code == "x = 1\n\nprint(x)" and repairs == ["dropped prose after an unclosed block"]


def test_valid_replies_never_report_repairs():
    for reply in (
        "```python\nprint(1)\n```",
        "```python\na = 1\n```\ntext\n```py\nb = 2\n```",
        "ok\n```python\nprint('cut off')",
        "The answer is 42.",
        'Plan.\n\n<invoke name="python">\nx = 1\nprint(x)\n```',
    ):
        assert extract_code_with_repairs(reply) == (extract_code(reply), [])


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


def test_code_with_a_mangled_opening_fence_is_salvaged():
    reply = "The grep is capped, so I will split it myself.\n\n\n<invoke import re\nparts = re.split(r'---', context)\nprint(len(parts))\n```"
    assert extract_code(reply) == "import re\nparts = re.split(r'---', context)\nprint(len(parts))"
    tagged = 'Plan.\n\n<invoke name="python">\nx = 1\nprint(x)\n```'
    assert extract_code(tagged) == "x = 1\nprint(x)"


def test_salvage_does_not_invent_code_from_prose():
    assert extract_code("I think the answer is 42.\n```") is None
    assert extract_code("Done\n```") is None
