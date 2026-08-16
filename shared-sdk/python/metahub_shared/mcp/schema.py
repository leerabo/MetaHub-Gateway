"""Draft 2020-12 MCP Schema profile with deterministic MetaHub hardening."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex

from .contracts import (
    McpContractError,
    McpSchemaArtifact,
    McpSchemaBundle,
    McpSchemaLimits,
    canonical_json_bytes,
)
from .redaction import validate_json_tree

MCP_DRAFT_2020_12_SCHEMA: Final[str] = "https://json-schema.org/draft/2020-12/schema"
MCP_MAX_SERVER_SCHEMA_BYTES: Final[int] = 1_048_576

_ALLOWED_FORMATS: Final[frozenset[str]] = frozenset(
    {"date-time", "date", "time", "duration", "email", "hostname", "ipv4", "ipv6", "uuid", "uri"}
)
_REFERENCE_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"$ref", "$dynamicRef", "$recursiveRef", "$id", "$anchor", "$dynamicAnchor"}
)
_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "$defs",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "type",
        "enum",
        "const",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "format",
        "properties",
        "patternProperties",
        "additionalProperties",
        "unevaluatedProperties",
        "required",
        "propertyNames",
        "maxProperties",
        "minProperties",
        "dependentRequired",
        "dependentSchemas",
        "items",
        "prefixItems",
        "contains",
        "minContains",
        "maxContains",
        "maxItems",
        "minItems",
        "uniqueItems",
        "unevaluatedItems",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
    }
)
_MAPPING_OF_SCHEMAS: Final[frozenset[str]] = frozenset(
    {"$defs", "properties", "patternProperties", "dependentSchemas"}
)
_LIST_OF_SCHEMAS: Final[frozenset[str]] = frozenset(
    {"prefixItems", "allOf", "anyOf", "oneOf"}
)
_COMBINATOR_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"allOf", "anyOf", "oneOf"}
)
_SINGLE_SCHEMA: Final[frozenset[str]] = frozenset(
    {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "contains",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
    }
)
_OBJECT_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "properties",
        "patternProperties",
        "additionalProperties",
        "unevaluatedProperties",
        "required",
        "propertyNames",
        "maxProperties",
        "minProperties",
        "dependentRequired",
        "dependentSchemas",
    }
)
_STRING_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"maxLength", "minLength", "pattern", "format"}
)
_ARRAY_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "items",
        "prefixItems",
        "contains",
        "minContains",
        "maxContains",
        "maxItems",
        "minItems",
        "uniqueItems",
        "unevaluatedItems",
    }
)


@dataclass(slots=True)
class _ProfileState:
    keyword_count: int = 0
    combinator_branches: int = 0
    enum_items: int = 0


def canonicalize_and_harden_schemas(
    input_schema: object,
    output_schema: object | None = None,
    *,
    limits: McpSchemaLimits | None = None,
) -> McpSchemaBundle:
    """Validate original input/output Schemas and produce deterministic effective copies."""

    budget = _limits(limits)
    input_artifact = _artifact(input_schema, limits=budget)
    output_artifact = None if output_schema is None else _artifact(output_schema, limits=budget)
    bundle = McpSchemaBundle(input=input_artifact, output=output_artifact)
    if bundle.total_byte_count > MCP_MAX_SERVER_SCHEMA_BYTES:
        raise McpContractError("mcp.schema_invalid")
    return bundle


def validate_arguments(
    arguments: object,
    effective_schema: object,
    *,
    limits: McpSchemaLimits | None = None,
) -> bytes:
    """Validate and return canonical argument bytes using only an effective Schema."""

    budget = _limits(limits)
    if type(arguments) is not dict:
        raise McpContractError("mcp.arguments_invalid")
    return _validate_instance(
        arguments,
        effective_schema,
        limits=budget,
        maximum=budget.max_instance_bytes,
        code="mcp.arguments_invalid",
    )


def validate_result(
    result: object,
    effective_schema: object,
    *,
    limits: McpSchemaLimits | None = None,
) -> bytes:
    """Validate and return canonical structured-result bytes using an effective output Schema."""

    budget = _limits(limits)
    if type(result) is not dict:
        raise McpContractError("mcp.result_invalid")
    return _validate_instance(
        result,
        effective_schema,
        limits=budget,
        maximum=budget.max_result_bytes,
        code="mcp.result_invalid",
    )


def validate_server_schema_budget(
    bundles: object,
    *,
    maximum: int = MCP_MAX_SERVER_SCHEMA_BYTES,
) -> int:
    """Bound the aggregate original+effective Schema bytes for one discovered Server."""

    if type(bundles) not in {list, tuple} or type(maximum) is not int or maximum < 1:
        raise McpContractError("mcp.schema_invalid")
    total = 0
    for bundle in bundles:
        if not isinstance(bundle, McpSchemaBundle):
            raise McpContractError("mcp.schema_invalid")
        total += bundle.total_byte_count
        if total > maximum:
            raise McpContractError("mcp.schema_invalid")
    return total


def _artifact(value: object, *, limits: McpSchemaLimits) -> McpSchemaArtifact:
    original, original_bytes = _parse_schema(value, limits=limits)
    _check_schema(original, limits=limits)
    effective = deepcopy(original)
    _harden_schema(effective, limits=limits)
    _check_schema(effective, limits=limits)
    effective_bytes = canonical_json_bytes(
        effective,
        maximum=limits.max_schema_bytes,
        code="mcp.schema_invalid",
    )
    return McpSchemaArtifact(
        original=original,
        effective=effective,
        original_json=original_bytes.decode("utf-8"),
        effective_json=effective_bytes.decode("utf-8"),
        original_sha256=hashlib.sha256(original_bytes).hexdigest(),
        effective_sha256=hashlib.sha256(effective_bytes).hexdigest(),
        original_byte_count=len(original_bytes),
        effective_byte_count=len(effective_bytes),
    )


def _parse_schema(value: object, *, limits: McpSchemaLimits) -> tuple[dict[str, Any], bytes]:
    if type(value) is bytes:
        raw = value
    elif type(value) is str:
        try:
            raw = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise McpContractError("mcp.schema_invalid") from None
    elif type(value) is dict:
        raw = canonical_json_bytes(
            value,
            maximum=limits.max_schema_bytes,
            code="mcp.schema_invalid",
        )
    else:
        raise McpContractError("mcp.schema_invalid")
    if not raw or len(raw) > limits.max_schema_bytes:
        raise McpContractError("mcp.schema_invalid")
    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise McpContractError("mcp.schema_invalid") from None
    if type(parsed) is not dict or parsed.get("type") != "object":
        raise McpContractError("mcp.schema_invalid")
    validate_json_tree(parsed, limits=limits, code="mcp.schema_invalid")
    canonical = canonical_json_bytes(
        parsed,
        maximum=limits.max_schema_bytes,
        code="mcp.schema_invalid",
    )
    return parsed, canonical


def _check_schema(schema: dict[str, Any], *, limits: McpSchemaLimits) -> _ProfileState:
    state = _ProfileState()
    _profile_schema(schema, limits=limits, state=state, root=True)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise McpContractError("mcp.schema_invalid") from None
    return state


def _profile_schema(
    schema: object,
    *,
    limits: McpSchemaLimits,
    state: _ProfileState,
    root: bool = False,
) -> None:
    if type(schema) is not dict:
        raise McpContractError("mcp.schema_invalid")
    if any(type(key) is not str for key in schema):
        raise McpContractError("mcp.schema_invalid")
    keys = set(schema)
    if keys & _REFERENCE_KEYWORDS or keys - _SCHEMA_KEYWORDS:
        raise McpContractError("mcp.schema_invalid")
    state.keyword_count += len(schema)
    if state.keyword_count > limits.max_keywords:
        raise McpContractError("mcp.schema_invalid")
    if "$schema" in schema:
        if not root or schema["$schema"] not in {
            MCP_DRAFT_2020_12_SCHEMA,
            MCP_DRAFT_2020_12_SCHEMA + "#",
        }:
            raise McpContractError("mcp.schema_invalid")
    if "format" in schema and schema["format"] not in _ALLOWED_FORMATS:
        raise McpContractError("mcp.schema_invalid")
    if "pattern" in schema:
        _safe_pattern(schema["pattern"], limits=limits)
    pattern_properties = schema.get("patternProperties")
    if pattern_properties is not None:
        if type(pattern_properties) is not dict:
            raise McpContractError("mcp.schema_invalid")
        for pattern in pattern_properties:
            _safe_pattern(pattern, limits=limits)
    if _object_schema_without_composition_carrier(schema, keys=keys):
        raise McpContractError("mcp.schema_invalid")
    if "enum" in schema:
        enum = schema["enum"]
        if type(enum) is not list or not enum:
            raise McpContractError("mcp.schema_invalid")
        state.enum_items += len(enum)
        if state.enum_items > limits.max_enum_items:
            raise McpContractError("mcp.schema_invalid")
        for item in enum:
            canonical_json_bytes(
                item,
                maximum=limits.max_enum_item_bytes,
                code="mcp.schema_invalid",
            )
    for keyword in _LIST_OF_SCHEMAS:
        if keyword not in schema:
            continue
        children = schema[keyword]
        if type(children) is not list or not children:
            raise McpContractError("mcp.schema_invalid")
        if keyword in _COMBINATOR_KEYWORDS:
            state.combinator_branches += len(children)
            if state.combinator_branches > limits.max_combinator_branches:
                raise McpContractError("mcp.schema_invalid")
        for child in children:
            _profile_schema(child, limits=limits, state=state)
    for keyword in _MAPPING_OF_SCHEMAS:
        if keyword not in schema:
            continue
        children = schema[keyword]
        if type(children) is not dict or any(type(key) is not str for key in children):
            raise McpContractError("mcp.schema_invalid")
        if keyword == "properties" and len(children) > limits.max_properties:
            raise McpContractError("mcp.schema_invalid")
        for child in children.values():
            _profile_schema(child, limits=limits, state=state)
    for keyword in _SINGLE_SCHEMA:
        if keyword not in schema:
            continue
        child = schema[keyword]
        if type(child) is bool:
            if keyword not in {"additionalProperties", "unevaluatedProperties", "unevaluatedItems"}:
                raise McpContractError("mcp.schema_invalid")
            continue
        _profile_schema(child, limits=limits, state=state)


def _object_schema_without_composition_carrier(
    schema: dict[str, Any],
    *,
    keys: set[str],
) -> bool:
    """Reject branch-declared fields that local ``additionalProperties=false`` would block."""

    declared_type = schema.get("type")
    is_object = (
        declared_type == "object"
        or (type(declared_type) is list and "object" in declared_type)
        or bool(keys & _OBJECT_KEYWORDS)
    )
    if not is_object or any(
        type(schema.get(keyword)) is dict and bool(schema[keyword])
        for keyword in ("properties", "patternProperties")
    ):
        return False
    return _composition_declares_object_members(schema)


def _composition_declares_object_members(schema: dict[str, Any]) -> bool:
    for keyword in _COMBINATOR_KEYWORDS:
        children = schema.get(keyword)
        if type(children) is not list:
            continue
        for child in children:
            if type(child) is not dict:
                continue
            if any(
                type(child.get(member_keyword)) is dict
                and bool(child[member_keyword])
                for member_keyword in ("properties", "patternProperties")
            ) or _composition_declares_object_members(child):
                return True
    return False


def _harden_schema(schema: dict[str, Any], *, limits: McpSchemaLimits) -> None:
    declared_type = schema.get("type")
    type_names = {declared_type} if type(declared_type) is str else set(declared_type or ())
    if "object" in type_names or bool(set(schema) & _OBJECT_KEYWORDS):
        schema["additionalProperties"] = False
        if "unevaluatedProperties" in schema:
            schema["unevaluatedProperties"] = False
        schema["maxProperties"] = _tightened_limit(
            schema.get("maxProperties"), limits.max_properties
        )
    if "string" in type_names or bool(set(schema) & _STRING_KEYWORDS):
        schema["maxLength"] = _tightened_limit(
            schema.get("maxLength"), limits.max_string_length
        )
    if "array" in type_names or bool(set(schema) & _ARRAY_KEYWORDS):
        schema["maxItems"] = _tightened_limit(
            schema.get("maxItems"), limits.max_array_items
        )
    for keyword in _LIST_OF_SCHEMAS:
        children = schema.get(keyword)
        if type(children) is list:
            for child in children:
                _harden_schema(child, limits=limits)
    for keyword in _MAPPING_OF_SCHEMAS:
        children = schema.get(keyword)
        if type(children) is dict:
            for child in children.values():
                _harden_schema(child, limits=limits)
    for keyword in _SINGLE_SCHEMA:
        child = schema.get(keyword)
        if type(child) is dict:
            _harden_schema(child, limits=limits)


def _validate_instance(
    value: object,
    schema_value: object,
    *,
    limits: McpSchemaLimits,
    maximum: int,
    code: str,
) -> bytes:
    schema, _ = _parse_schema(schema_value, limits=limits)
    # Even an internal caller that accidentally passes the original Schema cannot reopen
    # unknown object fields or deployment maxima; validation always uses the effective form.
    _harden_schema(schema, limits=limits)
    state = _check_schema(schema, limits=limits)
    encoded = canonical_json_bytes(value, maximum=maximum, code=code)
    detached = json.loads(encoded)
    nodes, _ = validate_json_tree(detached, limits=limits, code=code)
    estimated_steps = max(1, state.keyword_count + state.combinator_branches) * max(1, nodes)
    if estimated_steps > limits.max_evaluation_steps:
        raise McpContractError(code)
    try:
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(formats=tuple(sorted(_ALLOWED_FORMATS))),
        )
        error = next(validator.iter_errors(detached), None)
    except Exception:
        raise McpContractError(code) from None
    if error is not None:
        raise McpContractError(code)
    return encoded


def _safe_pattern(value: object, *, limits: McpSchemaLimits) -> None:
    if type(value) is not str or len(value) > limits.max_pattern_length:
        raise McpContractError("mcp.schema_invalid")
    try:
        compile_safe_dlp_regex(value)
    except UnsafeRegexError:
        raise McpContractError("mcp.schema_invalid") from None


def _tightened_limit(value: object, maximum: int) -> int:
    if value is None:
        return maximum
    if type(value) is not int or value < 0:
        raise McpContractError("mcp.schema_invalid")
    return min(value, maximum)


def _limits(value: McpSchemaLimits | None) -> McpSchemaLimits:
    if value is None:
        return McpSchemaLimits()
    if not isinstance(value, McpSchemaLimits):
        raise McpContractError("mcp.schema_invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("constant")
