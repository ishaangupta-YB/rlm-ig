"""JSON Schema support without dependencies.

Two jobs: turn a Python type into a schema (:func:`to_schema`), and check a
value against a schema (:func:`validate`). The validator covers the subset of
JSON Schema that typed Python and Pydantic actually emit; unknown keywords are
ignored rather than rejected.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import json
import re
import types
import typing
from typing import Any

from .errors import SchemaError

_PRIMITIVES: dict[Any, dict] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    type(None): {"type": "null"},
    list: {"type": "array"},
    tuple: {"type": "array"},
    set: {"type": "array"},
    dict: {"type": "object"},
}


def to_schema(spec: Any) -> dict | None:
    """Convert ``spec`` to a JSON Schema dict.

    Accepts a schema dict (returned unchanged), a Pydantic model class, a
    dataclass, a ``TypedDict``, or an ordinary type hint such as ``int``,
    ``list[str]`` or ``dict[str, float] | None``.
    """
    if spec is None:
        return None
    if isinstance(spec, dict):
        return spec
    try:
        return _from_hint(spec)
    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(f"cannot build a JSON Schema from {spec!r}: {exc}") from exc


def _from_hint(hint: Any) -> dict:
    if hint is Any:
        return {}
    if hint in _PRIMITIVES:
        return dict(_PRIMITIVES[hint])
    if hasattr(hint, "model_json_schema"):  # Pydantic v2 model
        return hint.model_json_schema()
    if hasattr(hint, "schema") and hasattr(hint, "__fields__"):  # Pydantic v1 model
        return hint.schema()
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in (list, set, frozenset, cabc.Sequence, cabc.Iterable, cabc.Collection):
        return {"type": "array", "items": _from_hint(args[0])} if args else {"type": "array"}
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": _from_hint(args[0])}
        if args:
            return {
                "type": "array",
                "prefixItems": [_from_hint(a) for a in args],
                "minItems": len(args),
                "maxItems": len(args),
            }
        return {"type": "array"}
    if origin is dict:
        if len(args) == 2:
            return {"type": "object", "additionalProperties": _from_hint(args[1])}
        return {"type": "object"}
    if origin is typing.Literal:
        return {"enum": list(args)}
    if origin is typing.Union or origin is types.UnionType:
        return {"anyOf": [_from_hint(a) for a in args]}
    if origin is typing.Annotated:
        return _from_hint(args[0])
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        hints = typing.get_type_hints(hint)
        props, required = {}, []
        for f in dataclasses.fields(hint):
            props[f.name] = _from_hint(hints.get(f.name, Any))
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                required.append(f.name)
        return {"type": "object", "properties": props, "required": required}
    if isinstance(hint, type) and issubclass(hint, dict) and hasattr(hint, "__annotations__"):  # TypedDict
        hints = typing.get_type_hints(hint)
        required = sorted(getattr(hint, "__required_keys__", hints.keys()))
        return {"type": "object", "properties": {k: _from_hint(v) for k, v in hints.items()}, "required": required}
    raise SchemaError(
        f"unsupported output type {hint!r}; use a JSON Schema dict, a basic type hint, a dataclass, or a Pydantic model"
    )


# ---------------------------------------------------------------- validation

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "array": lambda v: isinstance(v, (list, tuple)),
    "object": lambda v: isinstance(v, dict),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer()),
}


def validate(value: Any, schema: dict | None, *, limit: int = 12) -> list[str]:
    """Return human-readable problems (empty when ``value`` conforms)."""
    if not schema:
        return []
    errors: list[str] = []
    _check(value, schema, schema, "$", errors, limit)
    return errors[:limit]


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#"):
        raise SchemaError(f"only local $ref values are supported, got {ref!r}")
    node: Any = root
    for part in ref[1:].strip("/").split("/"):
        if not part:
            continue
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"$ref {ref!r} does not resolve")
        node = node[part]
    return node


def _matches(value: Any, schema: Any, root: dict) -> bool:
    probe: list[str] = []
    _check(value, schema, root, "$", probe, 1)
    return not probe


def _check(value: Any, schema: Any, root: dict, path: str, errors: list[str], limit: int) -> None:
    if len(errors) >= limit or schema is True or schema == {}:
        return
    if schema is False:
        errors.append(f"{path}: no value is allowed here")
        return
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), root, path, errors, limit)
        return

    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_TYPE_CHECKS.get(k, lambda v: True)(value) for k in kinds):
            errors.append(f"{path}: expected {' or '.join(kinds)}, got {_kind(value)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {json.dumps(schema['enum'])[:200]}, got {_show(value)}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {_show(schema['const'])}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be greater than {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: must be less than {schema['exclusiveMaximum']}")
        if (
            schema.get("multipleOf")
            and abs((value / schema["multipleOf"]) - round(value / schema["multipleOf"])) > 1e-9
        ):
            errors.append(f"{path}: not a multiple of {schema['multipleOf']}")
    elif isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen = set()
            for item in value:
                key = json.dumps(item, sort_keys=True, default=str)
                if key in seen:
                    errors.append(f"{path}: items are not unique")
                    break
                seen.add(key)
        prefix = schema.get("prefixItems") or []
        for i, sub in enumerate(prefix[: len(value)]):
            _check(value[i], sub, root, f"{path}[{i}]", errors, limit)
        items = schema.get("items")
        if items is not None:
            for i in range(len(prefix), len(value)):
                if len(errors) >= limit:
                    break
                _check(value[i], items, root, f"{path}[{i}]", errors, limit)
    elif isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: missing required key {name!r}")
        for name, sub in props.items():
            if name in value:
                _check(value[name], sub, root, f"{path}.{name}", errors, limit)
        extra = schema.get("additionalProperties")
        if extra is not None and extra is not True:
            for name in value:
                if name in props:
                    continue
                if extra is False:
                    errors.append(f"{path}: unexpected key {name!r}")
                else:
                    _check(value[name], extra, root, f"{path}.{name}", errors, limit)
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            errors.append(f"{path}: fewer than {schema['minProperties']} keys")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append(f"{path}: more than {schema['maxProperties']} keys")

    for sub in schema.get("allOf") or []:
        _check(value, sub, root, path, errors, limit)
    if "anyOf" in schema and not any(_matches(value, sub, root) for sub in schema["anyOf"]):
        errors.append(f"{path}: {_show(value)} matches none of the allowed alternatives")
    if "oneOf" in schema:
        hits = sum(1 for sub in schema["oneOf"] if _matches(value, sub, root))
        if hits != 1:
            errors.append(f"{path}: must match exactly one alternative, matched {hits}")
    if "not" in schema and _matches(value, schema["not"], root):
        errors.append(f"{path}: matches a schema it must not match")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _show(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


# -------------------------------------------------------------- JSON replies


def parse_json_reply(text: str) -> Any:
    """Pull one JSON value out of a model reply.

    Tries the whole reply, then fenced blocks, then the first position where a
    JSON object or array decodes. Raises ``ValueError`` when nothing parses.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    for match in re.finditer(r"```(?:json|JSON)?\s*\n(.*?)```", text, re.S):
        try:
            return json.loads(match.group(1).strip())
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
            return value
        except ValueError:
            continue
    raise ValueError("no JSON value found in the reply")
