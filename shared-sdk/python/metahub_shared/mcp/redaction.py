"""Deterministic MCP result projection and exact sensitive-key redaction."""

from __future__ import annotations

import json
from typing import Any, Final

from .contracts import (
    MCP_SENSITIVE_RESULT_KEYS,
    McpContractError,
    McpRedactionResult,
    McpSchemaLimits,
    canonical_json_bytes,
    ensure_exact_string_sequence,
)

MCP_REDACTED_VALUE: Final[str] = "[REDACTED]"


def canonical_redaction_pointers(values: object) -> tuple[str, ...]:
    """Validate, deduplicate, and sort 0..32 non-root RFC 6901 pointers."""

    pointers = ensure_exact_string_sequence(values, maximum=32)
    for pointer in pointers:
        _pointer_tokens(pointer)
    return tuple(sorted(pointers))


def redact_result(
    result: object,
    pointers: object = (),
    *,
    limits: McpSchemaLimits | None = None,
) -> McpRedactionResult:
    """Return a detached canonical result after built-in and policy redaction.

    Missing exact pointers are safe no-ops.  A scalar encountered before the
    target, a malformed array index, or malformed escape fails closed.
    """

    budget = limits or McpSchemaLimits()
    if not isinstance(budget, McpSchemaLimits):
        raise McpContractError("mcp.result_invalid")
    raw = canonical_json_bytes(
        result,
        maximum=budget.max_result_bytes,
        code="mcp.result_invalid",
    )
    detached = json.loads(raw)
    if type(detached) not in {dict, list}:
        raise McpContractError("mcp.result_invalid")
    _validate_tree(detached, limits=budget, code="mcp.result_invalid")
    checked_pointers = canonical_redaction_pointers(pointers)

    replaced: set[tuple[str | int, ...]] = set()
    _redact_sensitive_keys(detached, path=(), replaced=replaced)
    for pointer in checked_pointers:
        _redact_pointer(detached, _pointer_tokens(pointer), replaced=replaced)

    _validate_tree(detached, limits=budget, code="mcp.result_invalid")
    encoded = canonical_json_bytes(
        detached,
        maximum=budget.max_result_bytes,
        code="mcp.result_invalid",
    )
    return McpRedactionResult(
        value=detached,
        canonical_json=encoded.decode("utf-8"),
        redaction_count=len(replaced),
        byte_count=len(encoded),
    )


def project_mcp_result(
    content: object,
    structured_content: object | None = None,
    pointers: object = (),
    *,
    limits: McpSchemaLimits | None = None,
) -> McpRedactionResult:
    """Project only MCP text content and optional structured JSON before redaction."""

    budget = limits or McpSchemaLimits()
    if type(content) is not list or len(content) > 100:
        raise McpContractError("mcp.result_invalid")
    projected_content: list[dict[str, str]] = []
    for item in content:
        if (
            type(item) is not dict
            or set(item) != {"type", "text"}
            or item["type"] != "text"
            or type(item["text"]) is not str
        ):
            raise McpContractError("mcp.result_invalid")
        projected_content.append({"type": "text", "text": item["text"]})
    if structured_content is not None and type(structured_content) is not dict:
        raise McpContractError("mcp.result_invalid")
    if not projected_content and structured_content is None:
        raise McpContractError("mcp.result_invalid")
    projected: dict[str, object] = {"content": projected_content}
    if structured_content is not None:
        projected["structuredContent"] = structured_content
    return redact_result(projected, pointers, limits=budget)


def validate_json_tree(
    value: object,
    *,
    limits: McpSchemaLimits,
    code: str,
) -> tuple[int, int]:
    """Validate the shared depth/node/string budgets and return node/keyword-like counts."""

    return _validate_tree(value, limits=limits, code=code)


def _validate_tree(
    value: object,
    *,
    limits: McpSchemaLimits,
    code: str,
) -> tuple[int, int]:
    nodes = 0
    string_code_points = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise McpContractError(code)
        if type(current) is str:
            string_code_points += len(current)
            if len(current) > limits.max_string_length:
                raise McpContractError(code)
        elif type(current) is dict:
            if len(current) > limits.max_properties or any(
                type(key) is not str for key in current
            ):
                raise McpContractError(code)
            for key, item in current.items():
                string_code_points += len(key)
                if len(key) > limits.max_string_length:
                    raise McpContractError(code)
                stack.append((item, depth + 1))
        elif type(current) is list:
            if len(current) > limits.max_array_items:
                raise McpContractError(code)
            stack.extend((item, depth + 1) for item in current)
        elif current is None or type(current) in {bool, int, float}:
            continue
        else:
            raise McpContractError(code)
    if string_code_points > limits.max_result_bytes:
        raise McpContractError(code)
    return nodes, string_code_points


def _redact_sensitive_keys(
    value: object,
    *,
    path: tuple[str | int, ...],
    replaced: set[tuple[str | int, ...]],
) -> None:
    if type(value) is dict:
        for key in tuple(value):
            child_path = (*path, key)
            if _normalized_sensitive_key(key) in MCP_SENSITIVE_RESULT_KEYS:
                if child_path not in replaced:
                    value[key] = MCP_REDACTED_VALUE
                    replaced.add(child_path)
                continue
            _redact_sensitive_keys(value[key], path=child_path, replaced=replaced)
    elif type(value) is list:
        for index, item in enumerate(value):
            _redact_sensitive_keys(item, path=(*path, index), replaced=replaced)


def _normalized_sensitive_key(key: str) -> str:
    if not key.isascii():
        return ""
    return key.lower().replace("-", "_")


def _redact_pointer(
    root: dict[str, Any] | list[Any],
    tokens: tuple[str, ...],
    *,
    replaced: set[tuple[str | int, ...]],
) -> None:
    current: object = root
    path: tuple[str | int, ...] = ()
    for offset, token in enumerate(tokens):
        is_target = offset == len(tokens) - 1
        if type(current) is dict:
            if token not in current:
                return
            segment: str | int = token
            path = (*path, segment)
            if is_target:
                if path not in replaced:
                    current[token] = MCP_REDACTED_VALUE
                    replaced.add(path)
                return
            current = current[token]
            continue
        if type(current) is list:
            if token == "-" or not token.isascii() or not token.isdigit():
                raise McpContractError("mcp.result_invalid")
            if len(token) > 1 and token.startswith("0"):
                raise McpContractError("mcp.result_invalid")
            index = int(token)
            if index >= len(current):
                return
            path = (*path, index)
            if is_target:
                if path not in replaced:
                    current[index] = MCP_REDACTED_VALUE
                    replaced.add(path)
                return
            current = current[index]
            continue
        raise McpContractError("mcp.result_invalid")


def _pointer_tokens(pointer: object) -> tuple[str, ...]:
    if type(pointer) is not str or not pointer or not pointer.startswith("/"):
        raise McpContractError("mcp.request_invalid")
    try:
        pointer_bytes = pointer.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise McpContractError("mcp.request_invalid") from None
    if len(pointer_bytes) > 512:
        raise McpContractError("mcp.request_invalid")
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        decoded: list[str] = []
        index = 0
        while index < len(raw):
            character = raw[index]
            if character != "~":
                decoded.append(character)
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                raise McpContractError("mcp.request_invalid")
            decoded.append("~" if raw[index + 1] == "0" else "/")
            index += 2
        tokens.append("".join(decoded))
    return tuple(tokens)
