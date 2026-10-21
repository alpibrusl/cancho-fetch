#!/usr/bin/env python3
"""Write schemas/fetch.v1.json, the JSON Schema (Draft 2020-12) of `fetch`: the
status, headers and body of one HTTP response.

Adapted from cancho-tools' scripts/schemas.py: the parts every tool shares --
the envelope, the error object, the repair kinds, `text_or_bytes` -- are
written here once, and the file is self-contained because it is embedded in
the binary by scripts/manifest.py and printed by `table introspect`.

    python3 scripts/schemas.py           # write
    python3 scripts/schemas.py --check   # exit 1 if a committed file differs
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIALECT = "https://json-schema.org/draft/2020-12/schema"

NAT = {"type": "integer", "minimum": 0}
CODES = ["GENERAL_ERROR", "INVALID_ARGS", "NOT_FOUND", "PERMISSION_DENIED",
         "CONFLICT", "PRECONDITION_FAILED"]


def obj(props, required=None):
    return {
        "type": "object",
        "properties": props,
        "required": list(props) if required is None else required,
        "additionalProperties": False,
    }


def obj_optional(optional, props):
    """An object whose properties are all required but those named in `optional`."""
    return obj(props, required=[k for k in props if k not in optional])


def common_defs():
    return {
        "text_or_bytes": {
            "description": "UTF-8 text as a string; other bytes as base64 (RFC 4648 section 4, padded).",
            "oneOf": [
                {"type": "string"},
                obj({"b64": {"type": "string", "pattern": "^[A-Za-z0-9+/]*={0,2}$"}}),
            ],
        },
        "repair": {
            "oneOf": [
                {"type": "null"},
                obj({"kind": {"const": "retry"},
                     "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}}),
                obj({"kind": {"const": "none"}, "reason": {"type": "string"}}),
                obj({"kind": {"const": "choose"},
                     "options": {"type": "array", "items": obj(
                         {"argv": {"type": "array", "items": {"type": "string"}}})}}),
            ]
        },
        "error": obj({
            "code": {"enum": CODES},
            "rule": {"type": "string", "pattern": "^[a-z]+\\.[a-z0-9-]+$"},
            "message": {"type": "string"},
            "hint": {"type": ["string", "null"]},
            "repair": {"$ref": "#/$defs/repair"},
            "detail": {"type": "object"},
        }),
        "meta": obj({"version": {"type": "string"}}),
    }


def document(tool, data):
    props = {
        "ok": {"type": "boolean"},
        "command": {"const": tool},
        "schema": {"const": tool + ".v1"},
        "data": {"$ref": "#/$defs/data"},
        "error": {"$ref": "#/$defs/error"},
        "errors": {"type": "array", "items": {"$ref": "#/$defs/error"}, "minItems": 1},
        "meta": {"$ref": "#/$defs/meta"},
    }
    defs = common_defs()
    defs["data"] = data
    return {
        "$schema": DIALECT,
        "$id": "https://github.com/alpibrusl/cancho-fetch/schemas/%s.v1.json" % tool,
        "title": "%s.v1" % tool,
        "description": "One JSON object on one line. ok is false exactly when errors is present; error is its first element.",
        "type": "object",
        "properties": props,
        "required": ["ok", "command", "schema", "meta"],
        "additionalProperties": False,
        "allOf": [
            {"if": {"properties": {"ok": {"const": True}}},
             "then": {"not": {"anyOf": [{"required": ["error"]}, {"required": ["errors"]}]}},
             "else": {"required": ["error", "errors"]}},
        ],
        "$defs": defs,
    }


TB = {"$ref": "#/$defs/text_or_bytes"}

SCHEMAS = {
    "fetch": document("fetch", obj({
        "method": {"enum": ["get", "head"]},
        "host": {"type": "string"},
        "path": {"type": "string"},
        "status": {"type": "integer", "minimum": 100, "maximum": 599},
        "reason": {"type": "string"},
        "headers": {"type": "array", "items": obj({"name": {"type": "string"}, "value": {"type": "string"}})},
        "body": TB,
        "body_bytes": NAT,
        "peer": {"type": "string"},
        "milliseconds": NAT,
    })),
}


def render(schema):
    return json.dumps(schema, indent=2, sort_keys=False) + "\n"


def main():
    check = "--check" in sys.argv[1:]
    out = ROOT / "schemas"
    out.mkdir(exist_ok=True)
    stale = []
    for tool, schema in SCHEMAS.items():
        path = out / ("%s.v1.json" % tool)
        text = render(schema)
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(text)
    if stale:
        print("stale schemas (run scripts/schemas.py): " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
