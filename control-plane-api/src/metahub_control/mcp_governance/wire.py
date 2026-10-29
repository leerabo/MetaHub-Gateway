"""Exact HTTP wire parsing for PRD-082 MCP endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError


class DuplicateJsonKey(ValueError):
    """Raised without retaining a duplicate value that may be sensitive."""


class McpRoute(APIRoute):
    """Collapse framework path validation without echoing MCP values."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise ApiError("mcp.request_invalid") from None

        return handler


async def parse_json[PayloadT: BaseModel](
    request: Request, model: type[PayloadT], maximum: int
) -> PayloadT:
    values = raw_headers(request, b"content-type")
    if len(values) != 1 or values[0].lower() != "application/json":
        raise ApiError("mcp.request_invalid")
    try:
        body = await read_body(request, maximum=maximum, allow_empty=False)
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        return model.model_validate(value)
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ApiError("mcp.request_invalid") from None


async def read_body(request: Request, *, maximum: int, allow_empty: bool) -> bytes:
    if raw_headers(request, b"content-encoding") or raw_headers(request, b"transfer-encoding"):
        raise ApiError("mcp.request_invalid")
    declared = content_length(request)
    if declared is not None and (declared > maximum or (declared == 0 and not allow_empty)):
        raise ApiError("mcp.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                raise ApiError("mcp.request_invalid")
            body.extend(chunk)
    except ApiError:
        raise
    except Exception:
        raise ApiError("mcp.request_invalid") from None
    if (not body and not allow_empty) or (declared is not None and declared != len(body)):
        raise ApiError("mcp.request_invalid")
    return bytes(body)


def content_length(request: Request) -> int | None:
    values = raw_headers(request, b"content-length")
    if not values:
        return None
    value = values[0]
    if (
        len(values) != 1
        or not value
        or len(value) > 12
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ApiError("mcp.request_invalid")
    return int(value)


def require_idempotency_key(request: Request) -> str:
    values = raw_headers(request, b"idempotency-key")
    if len(values) != 1:
        raise ApiError("mcp.idempotency_required")
    value = values[0]
    if (
        not 8 <= len(value) <= 128
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        or "," in value
    ):
        raise ApiError("mcp.idempotency_required")
    return value


def strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            raise ApiError("mcp.request_invalid")
        result[key] = value
    return result


def reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("mcp.request_invalid")


def raw_headers(request: Request, name: bytes) -> list[str]:
    result: list[str] = []
    for key, value in request.scope.get("headers", ()):
        if key.lower() != name:
            continue
        try:
            result.append(value.decode("ascii", errors="strict"))
        except UnicodeDecodeError:
            raise ApiError("mcp.request_invalid") from None
    return result


def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKey
        value[key] = item
    return value


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def trace_id(request: Request) -> str:
    return str(getattr(request.state, "trace_id", ""))


def bounded_config_int(config: object, name: str, default: int, maximum: int) -> int:
    value = getattr(config, name, default)
    if type(value) is not int or value < 1:
        return default
    return min(value, maximum)


__all__ = [
    "McpRoute",
    "bounded_config_int",
    "no_store",
    "parse_json",
    "read_body",
    "reject_query",
    "request_id",
    "require_idempotency_key",
    "strict_query",
    "trace_id",
]
