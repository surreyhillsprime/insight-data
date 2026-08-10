"""Small offline Draft 2020-12 subset used only when jsonschema is unavailable."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime


class SchemaValidationError(ValueError):
    pass


def _matches_type(value, expected):
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
    }.get(expected, False)


def _resolve(root, reference):
    if not reference.startswith("#/"):
        raise SchemaValidationError(f"unsupported external $ref {reference}")
    value = root
    for part in reference[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def errors(instance, schema, root=None, path="$", schema_path="$schema"):
    root = root or schema
    result = []
    if schema is True:
        return result
    if schema is False:
        return [f"{path}: rejected by {schema_path}"]
    if "$ref" in schema:
        return errors(instance, _resolve(root, schema["$ref"]), root, path, schema["$ref"])
    if "oneOf" in schema:
        branches = [errors(instance, branch, root, path, schema_path + ".oneOf") for branch in schema["oneOf"]]
        if sum(not branch for branch in branches) != 1:
            result.append(f"{path}: must match exactly one oneOf branch")
        return result
    for child in schema.get("allOf", []):
        result.extend(errors(instance, child, root, path, schema_path + ".allOf"))
    if "if" in schema:
        condition_matches = not errors(instance, schema["if"], root, path, schema_path + ".if")
        branch = schema.get("then") if condition_matches else schema.get("else")
        if branch is not None:
            result.extend(errors(instance, branch, root, path, schema_path + (".then" if condition_matches else ".else")))
    if "const" in schema and instance != schema["const"]:
        result.append(f"{path}: value differs from const")
    if "enum" in schema and instance not in schema["enum"]:
        result.append(f"{path}: value is outside enum")
    expected_type = schema.get("type")
    if expected_type:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_type(instance, item) for item in allowed):
            return result + [f"{path}: expected type {allowed}"]
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                result.append(f"{path}: missing required property {key}")
        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        for key, value in instance.items():
            if key in properties:
                result.extend(errors(value, properties[key], root, f"{path}.{key}", schema_path + f".properties.{key}"))
                continue
            matched_patterns = [
                (pattern, child_schema)
                for pattern, child_schema in pattern_properties.items()
                if re.search(pattern, key)
            ]
            if matched_patterns:
                for pattern, child_schema in matched_patterns:
                    result.extend(errors(value, child_schema, root, f"{path}.{key}", schema_path + f".patternProperties.{pattern}"))
            elif schema.get("additionalProperties") is False:
                result.append(f"{path}: unexpected property {key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                result.extend(errors(value, schema["additionalProperties"], root, f"{path}.{key}", schema_path + ".additionalProperties"))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            result.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            result.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems"):
            serialised = [json.dumps(item, sort_keys=True) for item in instance]
            if len(serialised) != len(set(serialised)):
                result.append(f"{path}: items are not unique")
        prefix = schema.get("prefixItems", [])
        for index, child_schema in enumerate(prefix[:len(instance)]):
            result.extend(errors(instance[index], child_schema, root, f"{path}[{index}]", schema_path + ".prefixItems"))
        items_schema = schema.get("items")
        if items_schema is not None:
            start = len(prefix) if prefix else 0
            for index in range(start, len(instance)):
                result.extend(errors(instance[index], items_schema, root, f"{path}[{index}]", schema_path + ".items"))
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            result.append(f"{path}: shorter than minLength")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            result.append(f"{path}: does not match pattern")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(instance.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError:
                result.append(f"{path}: invalid date-time")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            result.append(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            result.append(f"{path}: above maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            result.append(f"{path}: not above exclusiveMinimum")
    return result


def validate(instance, schema):
    failures = errors(instance, schema)
    if failures:
        raise SchemaValidationError("; ".join(failures[:10]))
