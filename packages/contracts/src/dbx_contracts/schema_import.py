"""Read a target schema the way a client is likely to hand one over.

The schema language has one shape - a list of fields - but a client's spec arrives
in whatever shape their team writes. The two worth meeting halfway:

* **JSON Schema**, the usual answer to "send us the spec". `properties` become
  fields, `required` marks them, `format` and `enum` set the type, and the usual
  constraints carry over. It has no word for "unique", "also called" or "points
  at", so `unique` / `x-unique`, `aliases` / `x-aliases` and `reference` /
  `x-reference` are read on a property when present.
* **A map of fields** - `fields: {employee_id: {type: string}}` - which is how most
  people write YAML by hand. A bare type (`name: string`) is accepted too.

Everything is converted by rule, not guessed: a shape that cannot be read says why,
naming the field, instead of being coerced into something nobody wrote.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

#: The spellings people use for the types the schema language has.
_TYPE_WORDS = {
    "string": "string", "str": "string", "text": "string", "varchar": "string",
    "char": "string",
    "integer": "integer", "int": "integer", "long": "integer", "bigint": "integer",
    "number": "number", "float": "number", "double": "number", "decimal": "number",
    "numeric": "number",
    "boolean": "boolean", "bool": "boolean",
    "date": "date",
    "datetime": "datetime", "date-time": "datetime", "timestamp": "datetime",
    "email": "email",
    "enum": "enum",
}

#: JSON Schema formats that are really types here.
_FORMAT_TYPES = {"date": "date", "date-time": "datetime", "email": "email"}

#: JSON Schema keyword -> schema-language key, for constraints that mean the same thing.
_CONSTRAINTS = {
    "pattern": "pattern", "maxLength": "max_length", "minLength": "min_length",
    "minimum": "minimum", "maximum": "maximum", "description": "description",
}

#: Extension keys a JSON Schema property may carry, for what JSON Schema cannot say.
_EXTENSIONS = {
    "unique": "unique", "x-unique": "unique",
    "aliases": "aliases", "x-aliases": "aliases",
    "reference": "reference", "x-reference": "reference",
    "case_sensitive": "case_sensitive", "x-case-sensitive": "case_sensitive",
}


class SchemaShapeError(ValueError):
    """The input is not a schema this can read. The message says which part and why."""


def normalise_schema(raw: Any) -> dict[str, Any]:
    """Any accepted shape in, the schema language's own shape out."""
    if isinstance(raw, list):
        # Just the fields, with nothing around them.
        raw = {"fields": raw}
    if not isinstance(raw, dict):
        raise SchemaShapeError(
            "expected a schema with fields; this is a single value, not a list of fields"
        )

    # A JSON Schema for "a list of employees" describes one employee in `items`.
    if raw.get("type") == "array" and isinstance(raw.get("items"), dict):
        raw = {**raw["items"], "title": raw["items"].get("title") or raw.get("title")}

    if "fields" not in raw and isinstance(raw.get("properties"), dict):
        return _from_json_schema(raw)

    out = dict(raw)
    if "entity" not in out:
        out["entity"] = _entity_name(out.get("title"))
    out.pop("title", None)

    fields = out.get("fields")
    if isinstance(fields, dict):
        fields = [_named(name, spec) for name, spec in fields.items()]
    if isinstance(fields, list):
        out["fields"] = [_tidy_field(f) if isinstance(f, dict) else f for f in fields]
    return out


def explain_schema_errors(exc: ValidationError, raw: Any) -> str:
    """Pydantic's report, rewritten for the person who wrote the schema."""
    names: list[str] = []
    if isinstance(raw, dict) and isinstance(raw.get("fields"), list):
        names = [f.get("name", "?") if isinstance(f, dict) else "?" for f in raw["fields"]]

    lines = []
    for err in exc.errors():
        loc = list(err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        if loc == ["fields"] and err.get("type") == "missing":
            lines.append("there is no list of fields")
            continue
        where = ""
        if len(loc) >= 2 and loc[0] == "fields" and isinstance(loc[1], int):
            name = names[loc[1]] if loc[1] < len(names) else f"#{loc[1] + 1}"
            rest = ".".join(str(p) for p in loc[2:])
            where = f"field {name!r}" + (f" ({rest})" if rest else "")
        elif loc:
            where = ".".join(str(p) for p in loc)
        # The schema's own checks already name the field they are about.
        lines.append(f"{where}: {msg}" if where and not msg.startswith("field ") else msg)
    return "; ".join(dict.fromkeys(lines)) or "that is not a usable schema"


# ------------------------------------------------------------------ internals


def _entity_name(title: Any) -> str:
    if isinstance(title, str) and title.strip():
        return re.sub(r"[^a-z0-9]+", "_", title.strip().casefold()).strip("_") or "record"
    return "record"


def _named(name: str, spec: Any) -> dict[str, Any]:
    """One entry of a field map as a field."""
    if isinstance(spec, str):
        return {"name": name, "type": spec}
    if spec is None:
        return {"name": name, "type": "string"}
    if isinstance(spec, dict):
        return {"name": name, **spec}
    raise SchemaShapeError(
        f"field {name!r}: expected a type or a set of settings, got {type(spec).__name__}"
    )


def _tidy_field(field: dict[str, Any]) -> dict[str, Any]:
    """Accept the common spellings of a type inside the schema language's own shape."""
    out = dict(field)
    kind = out.get("type")
    if isinstance(kind, str):
        word = _TYPE_WORDS.get(kind.strip().casefold())
        if word:
            out["type"] = word
        fmt = out.get("format")
        # `type: string, format: email` is JSON Schema's way of saying "an email".
        # Only those words: a date format such as YYYY-MM-DD stays a date format.
        if out["type"] == "string" and isinstance(fmt, str) and fmt in _FORMAT_TYPES:
            out["type"] = _FORMAT_TYPES[fmt]
            out.pop("format")
    if "enum" in out and "allowed" not in out:
        out["allowed"] = [str(v) for v in out.pop("enum") if v is not None]
        out.setdefault("type", "enum")
        if out["type"] == "string":
            out["type"] = "enum"
    return out


def _from_json_schema(doc: dict[str, Any]) -> dict[str, Any]:
    required = set(doc.get("required") or [])
    defs = {**(doc.get("definitions") or {}), **(doc.get("$defs") or {})}
    fields: list[dict[str, Any]] = []
    for name, node in doc["properties"].items():
        fields.extend(_properties(name, node, name in required, defs))
    out: dict[str, Any] = {
        "entity": _entity_name(doc.get("title")),
        "fields": fields,
    }
    if isinstance(doc.get("description"), str):
        out["description"] = doc["description"]
    return out


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    ref = node.get("$ref") if isinstance(node, dict) else None
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        if key in defs:
            return {**defs[key], **{k: v for k, v in node.items() if k != "$ref"}}
        raise SchemaShapeError(f"{ref!r} points at a definition this schema does not include")
    return node


def _properties(
    name: str, node: Any, required: bool, defs: dict[str, Any]
) -> list[dict[str, Any]]:
    node = _resolve(node, defs)
    if not isinstance(node, dict):
        raise SchemaShapeError(f"field {name!r}: expected a description of the field")

    nullable = False
    # `anyOf: [{...}, {type: null}]` and `type: [string, null]` both mean "may be empty".
    for key in ("anyOf", "oneOf"):
        options = node.get(key)
        if isinstance(options, list):
            concrete = [_resolve(o, defs) for o in options
                        if not (isinstance(o, dict) and o.get("type") == "null")]
            nullable = len(concrete) < len(options)
            if len(concrete) != 1:
                raise SchemaShapeError(
                    f"field {name!r}: can be one of several shapes; a migration field "
                    "needs one"
                )
            node = {**{k: v for k, v in node.items() if k != key}, **concrete[0]}
    kind = node.get("type")
    if isinstance(kind, list):
        nullable = nullable or "null" in kind
        kinds = [k for k in kind if k != "null"]
        if len(kinds) != 1:
            raise SchemaShapeError(f"field {name!r}: can be {kinds}; a migration field needs one type")
        kind = kinds[0]

    if kind == "object" or (kind is None and isinstance(node.get("properties"), dict)):
        # One level of nesting reads naturally as prefixed fields: address.city
        # becomes address_city. Deeper than that is a different record, not a field.
        inner_required = set(node.get("required") or [])
        out = []
        for inner, child in (node.get("properties") or {}).items():
            child = _resolve(child, defs)
            if isinstance(child, dict) and (child.get("type") == "object"
                                            or isinstance(child.get("properties"), dict)):
                raise SchemaShapeError(
                    f"field {name}.{inner!s}: nested more than one level deep; flatten it "
                    "or leave it out"
                )
            out.extend(_properties(f"{name}_{inner}", child,
                                   required and inner in inner_required, defs))
        return out
    if kind == "array":
        raise SchemaShapeError(
            f"field {name!r} holds a list, and a migration field holds one value. "
            "Leave it out, or describe it as text"
        )

    field: dict[str, Any] = {"name": name, "required": required}
    fmt = node.get("format")
    if "enum" in node:
        field["type"] = "enum"
        field["allowed"] = [str(v) for v in node["enum"] if v is not None]
    elif kind == "string" and fmt in _FORMAT_TYPES:
        field["type"] = _FORMAT_TYPES[fmt]
    elif isinstance(kind, str) and kind in _TYPE_WORDS:
        field["type"] = _TYPE_WORDS[kind]
    elif kind is None:
        field["type"] = "string"
    else:
        raise SchemaShapeError(f"field {name!r}: type {kind!r} is not one this can hold")

    for key, ours in _CONSTRAINTS.items():
        if node.get(key) is not None:
            field[ours] = node[key]
    for key, ours in _EXTENSIONS.items():
        if node.get(key) is not None:
            field[ours] = node[key]
    if nullable:
        field["nullable"] = True
    return [field]
