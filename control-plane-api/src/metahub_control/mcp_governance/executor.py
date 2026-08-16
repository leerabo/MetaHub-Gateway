"""Fail-closed PRD-082 MCP executor boundary.

Production execution remains disabled until PRD-083 supplies the network sandbox.  The only
network-capable implementation in this module is a test-only official MCP SDK adapter restricted
to literal loopback Streamable HTTP endpoints.  Endpoint, bearer, Schema, arguments, and result
values are excluded from dataclass representations and are never included in raised errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from metahub_shared.mcp import (
    McpCanonicalJson,
    McpContractError,
    McpSchemaBundle,
    canonical_json_bytes,
    canonicalize_and_harden_schemas,
    canonicalize_mcp_annotations,
    normalize_mcp_description,
    validate_mcp_endpoint_url,
    validate_mcp_tool_name,
    validate_server_schema_budget,
)
from metahub_shared.runtime.config import AppConfig

McpTransport = Literal["streamable_http"]

_PROTOCOL_VERSION = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", re.ASCII)
_MAX_TOOLS = 100
_MAX_TOOL_PAGES = 10
_MAX_CURSOR_BYTES = 256
_MAX_CAPABILITIES_BYTES = 65_536
_FIXED_ERROR_CODES = frozenset(
    {
        "mcp.executor_disabled",
        "mcp.sync_failed",
        "mcp.server_unavailable",
        "mcp.result_invalid",
        "mcp.outcome_unknown",
        "mcp.unavailable",
    }
)


class McpExecutorError(RuntimeError):
    """A fixed executor failure that never contains submitted or remote data."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _FIXED_ERROR_CODES else "mcp.unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class McpConnectionMaterial:
    """Ephemeral decrypted connection material owned by a trusted worker."""

    endpoint: str = field(repr=False)
    bearer_token: str | None = field(default=None, repr=False)
    transport: McpTransport = "streamable_http"


@dataclass(frozen=True, slots=True)
class McpDiscoverRequest:
    """One bounded initialize and tools/list discovery request."""

    connection: McpConnectionMaterial = field(repr=False)
    deadline_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class McpProbeRequest:
    """One bounded initialize and tools/list contract probe for a frozen Tool."""

    connection: McpConnectionMaterial = field(repr=False)
    tool_name: str
    deadline_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class McpCallRequest:
    """One bounded tools/call request made only after the durable call intent."""

    connection: McpConnectionMaterial = field(repr=False)
    tool_name: str
    arguments: Mapping[str, Any] = field(repr=False)
    deadline_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class McpDiscoveredTool:
    """Validated remote Tool metadata ready for a worker finalize transaction."""

    name: str
    description: str | None = field(repr=False)
    schema_bundle: McpSchemaBundle = field(repr=False)
    annotations: McpCanonicalJson = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpDiscoveryResult:
    """A complete, duplicate-free, bounded Server discovery result."""

    protocol_version: str
    capabilities_json: str = field(repr=False)
    capabilities_sha256: str
    tools: tuple[McpDiscoveredTool, ...]
    request_bytes: int
    response_bytes: int


@dataclass(frozen=True, slots=True)
class McpProbeResult:
    """The current remote contract for one frozen Tool."""

    protocol_version: str
    capabilities_json: str = field(repr=False)
    capabilities_sha256: str
    tool: McpDiscoveredTool
    request_bytes: int
    response_bytes: int


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """A known MCP CallToolResult; raw values remain hidden from representations."""

    is_error: bool
    content: list[dict[str, str]] = field(repr=False)
    structured_content: dict[str, Any] | None = field(repr=False)
    request_bytes: int
    response_bytes: int


@dataclass(frozen=True, slots=True)
class McpSdkInitialize:
    """Safe normalized initialize fields returned by an SDK session adapter."""

    protocol_version: str
    capabilities: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpSdkTool:
    """Raw JSON-compatible Tool fields returned by an SDK session adapter."""

    name: object
    description: object = field(repr=False)
    input_schema: object = field(repr=False)
    output_schema: object | None = field(repr=False)
    annotations: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpSdkToolPage:
    """One SDK tools/list page before executor validation."""

    tools: tuple[McpSdkTool, ...]
    next_cursor: object = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class McpSdkCallResult:
    """One normalized official SDK CallToolResult before result projection."""

    is_error: object
    content: object = field(repr=False)
    structured_content: object | None = field(repr=False)


class McpSdkSession(Protocol):
    """Minimal official SDK surface used by the executor orchestration."""

    @property
    def request_bytes(self) -> int: ...

    @property
    def response_bytes(self) -> int: ...

    async def initialize(self) -> McpSdkInitialize: ...

    async def list_tools(self, cursor: str | None) -> McpSdkToolPage: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> McpSdkCallResult: ...


McpSdkSessionFactory = Callable[
    [McpConnectionMaterial, int, int], AbstractAsyncContextManager[McpSdkSession]
]


class McpExecutor:
    """Synchronous worker entry points with an injectable async SDK session boundary."""

    def __init__(
        self,
        config: AppConfig,
        *,
        session_factory: McpSdkSessionFactory | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory or _official_session_factory

    def discover(self, request: McpDiscoverRequest) -> McpDiscoveryResult:
        """Discover all Tools while enforcing the ten-page and 100-Tool limits."""

        self._ensure_enabled()
        if not isinstance(request, McpDiscoverRequest):
            raise McpExecutorError("mcp.unavailable")
        connection = _connection(request.connection)
        deadline = _deadline(request.deadline_seconds, self._config.mcp_probe_deadline_seconds)
        return _run_sync(
            lambda: self._discover_async(
                connection,
                deadline=deadline,
                failure_code="mcp.sync_failed",
            )
        )

    def probe(self, request: McpProbeRequest) -> McpProbeResult:
        """Read and validate the current remote contract for one Tool."""

        self._ensure_enabled()
        if not isinstance(request, McpProbeRequest):
            raise McpExecutorError("mcp.unavailable")
        connection = _connection(request.connection)
        tool_name = _tool_name(request.tool_name)
        deadline = _deadline(request.deadline_seconds, self._config.mcp_probe_deadline_seconds)
        discovery = _run_sync(
            lambda: self._discover_async(
                connection,
                deadline=deadline,
                failure_code="mcp.server_unavailable",
            )
        )
        tool = next((item for item in discovery.tools if item.name == tool_name), None)
        if tool is None:
            raise McpExecutorError("mcp.server_unavailable")
        return McpProbeResult(
            protocol_version=discovery.protocol_version,
            capabilities_json=discovery.capabilities_json,
            capabilities_sha256=discovery.capabilities_sha256,
            tool=tool,
            request_bytes=discovery.request_bytes,
            response_bytes=discovery.response_bytes,
        )

    def call(self, request: McpCallRequest) -> McpCallResult:
        """Execute exactly one tools/call under the configured call deadline."""

        self._ensure_enabled()
        if not isinstance(request, McpCallRequest):
            raise McpExecutorError("mcp.unavailable")
        connection = _connection(request.connection)
        tool_name = _tool_name(request.tool_name)
        deadline = _deadline(request.deadline_seconds, self._config.mcp_call_deadline_seconds)
        try:
            arguments_bytes = canonical_json_bytes(
                request.arguments,
                maximum=self._config.mcp_arguments_max_bytes,
                code="mcp.arguments_invalid",
            )
            arguments = json.loads(arguments_bytes)
        except (McpContractError, TypeError, ValueError):
            raise McpExecutorError("mcp.unavailable") from None
        if type(arguments) is not dict:
            raise McpExecutorError("mcp.unavailable")
        return _run_sync(
            lambda: self._call_async(
                connection,
                tool_name=tool_name,
                arguments=arguments,
                deadline=deadline,
            )
        )

    def _ensure_enabled(self) -> None:
        # This gate intentionally precedes request parsing, SDK loading, and adapter construction.
        if (
            self._config.environment == "prod"
            or not self._config.mcp_enabled
            or self._config.mcp_executor_mode != "test_loopback"
            or not self._config.mcp_test_loopback_enabled
        ):
            raise McpExecutorError("mcp.executor_disabled")

    async def _discover_async(
        self,
        connection: McpConnectionMaterial,
        *,
        deadline: int,
        failure_code: str,
    ) -> McpDiscoveryResult:
        try:
            async with asyncio.timeout(deadline):
                async with self._session_factory(
                    connection,
                    deadline,
                    self._config.mcp_result_max_bytes,
                ) as session:
                    initialized = _initialize(await session.initialize())
                    raw_tools = await _collect_tool_pages(session)
                    tools = tuple(
                        sorted(
                            (
                                _discovered_tool(item, config=self._config)
                                for item in raw_tools
                            ),
                            key=lambda item: item.name,
                        )
                    )
                    validate_server_schema_budget(
                        [item.schema_bundle for item in tools],
                        maximum=self._config.mcp_server_schema_max_bytes,
                    )
                    capabilities_json, capabilities_sha256 = _capabilities(
                        initialized.capabilities
                    )
                    request_bytes, response_bytes = _metrics(
                        session,
                        response_maximum=self._config.mcp_result_max_bytes,
                    )
                    return McpDiscoveryResult(
                        protocol_version=initialized.protocol_version,
                        capabilities_json=capabilities_json,
                        capabilities_sha256=capabilities_sha256,
                        tools=tools,
                        request_bytes=request_bytes,
                        response_bytes=response_bytes,
                    )
        except (McpExecutorError, McpContractError, TimeoutError, Exception):
            raise McpExecutorError(failure_code) from None

    async def _call_async(
        self,
        connection: McpConnectionMaterial,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        deadline: int,
    ) -> McpCallResult:
        call_started = False
        call_returned = False
        try:
            async with asyncio.timeout(deadline):
                async with self._session_factory(
                    connection,
                    deadline,
                    self._config.mcp_result_max_bytes,
                ) as session:
                    _initialize(await session.initialize())
                    call_started = True
                    raw = await session.call_tool(tool_name, arguments)
                    call_returned = True
                    result = _call_result(raw, maximum=self._config.mcp_result_max_bytes)
                    request_bytes, response_bytes = _metrics(
                        session,
                        response_maximum=self._config.mcp_result_max_bytes,
                    )
                    return McpCallResult(
                        is_error=result.is_error,
                        content=cast(list[dict[str, str]], result.content),
                        structured_content=cast(dict[str, Any] | None, result.structured_content),
                        request_bytes=request_bytes,
                        response_bytes=response_bytes,
                    )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            if call_returned:
                raise McpExecutorError("mcp.result_invalid") from None
            if call_started:
                raise McpExecutorError("mcp.outcome_unknown") from None
            raise McpExecutorError("mcp.server_unavailable") from None


def discover_mcp_server(config: AppConfig, request: McpDiscoverRequest) -> McpDiscoveryResult:
    """Worker-friendly synchronous Server discovery entry point."""

    return McpExecutor(config).discover(request)


def probe_mcp_tool(config: AppConfig, request: McpProbeRequest) -> McpProbeResult:
    """Worker-friendly synchronous Tool probe entry point."""

    return McpExecutor(config).probe(request)


def call_mcp_tool(config: AppConfig, request: McpCallRequest) -> McpCallResult:
    """Worker-friendly synchronous Tool call entry point."""

    return McpExecutor(config).call(request)


async def _collect_tool_pages(session: McpSdkSession) -> tuple[McpSdkTool, ...]:
    tools: list[McpSdkTool] = []
    names: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for page_number in range(1, _MAX_TOOL_PAGES + 1):
        page = await session.list_tools(cursor)
        if not isinstance(page, McpSdkToolPage) or type(page.tools) is not tuple:
            raise McpContractError("mcp.schema_invalid")
        for item in page.tools:
            if not isinstance(item, McpSdkTool):
                raise McpContractError("mcp.schema_invalid")
            name = validate_mcp_tool_name(item.name)
            if name in names:
                raise McpContractError("mcp.schema_invalid")
            names.add(name)
            tools.append(item)
            if len(tools) > _MAX_TOOLS:
                raise McpContractError("mcp.schema_invalid")
        next_cursor = _cursor(page.next_cursor)
        if next_cursor is None:
            return tuple(tools)
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise McpContractError("mcp.schema_invalid")
        if page_number == _MAX_TOOL_PAGES:
            raise McpContractError("mcp.schema_invalid")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise McpContractError("mcp.schema_invalid")


def _discovered_tool(value: McpSdkTool, *, config: AppConfig) -> McpDiscoveredTool:
    name = validate_mcp_tool_name(value.name)
    description = normalize_mcp_description(value.description)
    schema_bundle = canonicalize_and_harden_schemas(
        value.input_schema,
        value.output_schema,
        limits=config.mcp_schema_limits,
    )
    annotations = canonicalize_mcp_annotations(value.annotations)
    return McpDiscoveredTool(
        name=name,
        description=description,
        schema_bundle=schema_bundle,
        annotations=annotations,
    )


def _initialize(value: object) -> McpSdkInitialize:
    if not isinstance(value, McpSdkInitialize):
        raise McpContractError("mcp.schema_invalid")
    if (
        type(value.protocol_version) is not str
        or _PROTOCOL_VERSION.fullmatch(value.protocol_version) is None
        or type(value.capabilities) is not dict
    ):
        raise McpContractError("mcp.schema_invalid")
    return value


def _capabilities(value: Mapping[str, Any]) -> tuple[str, str]:
    encoded = canonical_json_bytes(
        value,
        maximum=_MAX_CAPABILITIES_BYTES,
        code="mcp.schema_invalid",
    )
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _call_result(value: object, *, maximum: int) -> McpSdkCallResult:
    if not isinstance(value, McpSdkCallResult) or type(value.is_error) is not bool:
        raise McpContractError("mcp.result_invalid")
    if type(value.content) is not list or len(value.content) > _MAX_TOOLS:
        raise McpContractError("mcp.result_invalid")
    content: list[dict[str, str]] = []
    for item in value.content:
        if (
            type(item) is not dict
            or set(item) != {"type", "text"}
            or item.get("type") != "text"
            or type(item.get("text")) is not str
        ):
            raise McpContractError("mcp.result_invalid")
        content.append({"type": "text", "text": item["text"]})
    if value.structured_content is not None and type(value.structured_content) is not dict:
        raise McpContractError("mcp.result_invalid")
    projected: dict[str, object] = {"content": content}
    if value.structured_content is not None:
        projected["structuredContent"] = value.structured_content
    encoded = canonical_json_bytes(projected, maximum=maximum, code="mcp.result_invalid")
    detached = json.loads(encoded)
    return McpSdkCallResult(
        is_error=value.is_error,
        content=detached["content"],
        structured_content=detached.get("structuredContent"),
    )


def _connection(value: object) -> McpConnectionMaterial:
    if not isinstance(value, McpConnectionMaterial) or value.transport != "streamable_http":
        raise McpExecutorError("mcp.executor_disabled")
    try:
        endpoint = validate_mcp_endpoint_url(
            value.endpoint,
            production=False,
            allow_test_loopback=True,
        )
    except McpContractError:
        raise McpExecutorError("mcp.executor_disabled") from None
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise McpExecutorError("mcp.executor_disabled")
    token = value.bearer_token
    if token is not None:
        if type(token) is not str:
            raise McpExecutorError("mcp.executor_disabled")
        try:
            token_bytes = token.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise McpExecutorError("mcp.executor_disabled") from None
        if not 1 <= len(token_bytes) <= 8_192 or any(
            character in token for character in "\r\n"
        ):
            raise McpExecutorError("mcp.executor_disabled")
    return McpConnectionMaterial(
        endpoint=endpoint,
        bearer_token=token,
        transport="streamable_http",
    )


def _tool_name(value: object) -> str:
    try:
        return validate_mcp_tool_name(value)
    except McpContractError:
        raise McpExecutorError("mcp.unavailable") from None


def _deadline(value: object, configured: object) -> int:
    if type(configured) is not int or configured < 1:
        raise McpExecutorError("mcp.unavailable")
    if value is None:
        return configured
    if type(value) is not int or value < 1 or value > configured:
        raise McpExecutorError("mcp.unavailable")
    return value


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8", errors="strict")) > _MAX_CURSOR_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise McpContractError("mcp.schema_invalid")
    return value


def _metrics(session: McpSdkSession, *, response_maximum: int) -> tuple[int, int]:
    request_bytes = session.request_bytes
    response_bytes = session.response_bytes
    if (
        type(request_bytes) is not int
        or request_bytes < 0
        or type(response_bytes) is not int
        or response_bytes < 0
        or response_bytes > response_maximum
    ):
        raise McpContractError("mcp.result_invalid")
    return request_bytes, response_bytes


def _run_sync(factory: Callable[[], Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(factory())
        except McpExecutorError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise McpExecutorError("mcp.unavailable") from None
    raise McpExecutorError("mcp.unavailable")


@dataclass(slots=True)
class _ByteBudget:
    maximum: int
    request_bytes: int = 0
    response_bytes: int = 0

    def record_request(self, request: Any) -> None:
        raw = request.headers.get("content-length")
        if raw is None:
            return
        try:
            count = int(raw)
        except (TypeError, ValueError):
            return
        if count >= 0:
            self.request_bytes += count

    def record_response(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise _SdkFailure
        self.response_bytes += count
        if self.response_bytes > self.maximum:
            raise _SdkFailure


class _SdkFailure(Exception):
    """Internal fixed failure used only to abort the SDK transport."""


@asynccontextmanager
async def _official_session_factory(
    connection: McpConnectionMaterial,
    deadline_seconds: int,
    max_response_bytes: int,
) -> Any:
    client_session, streamable_http_client = _load_sdk_bindings()
    budget = _ByteBudget(maximum=max_response_bytes)
    headers = None
    if connection.bearer_token is not None:
        headers = {"Authorization": f"Bearer {connection.bearer_token}"}
    httpx_factory = _bounded_httpx_factory(budget, deadline_seconds=deadline_seconds)
    try:
        async with streamable_http_client(
            connection.endpoint,
            headers=headers,
            timeout=timedelta(seconds=deadline_seconds),
            sse_read_timeout=timedelta(seconds=deadline_seconds),
            terminate_on_close=False,
            httpx_client_factory=httpx_factory,
        ) as streams:
            if type(streams) not in {tuple, list} or len(streams) < 2:
                raise _SdkFailure
            async with client_session(streams[0], streams[1]) as session:
                yield _OfficialSdkSession(session, budget)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        raise _SdkFailure from None


def _load_sdk_bindings() -> tuple[Any, Any]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except (ImportError, AttributeError):
        raise _SdkFailure from None
    return ClientSession, streamablehttp_client


class _OfficialSdkSession:
    def __init__(self, session: Any, budget: _ByteBudget) -> None:
        self._session = session
        self._budget = budget

    @property
    def request_bytes(self) -> int:
        return self._budget.request_bytes

    @property
    def response_bytes(self) -> int:
        return self._budget.response_bytes

    async def initialize(self) -> McpSdkInitialize:
        result = await self._session.initialize()
        return McpSdkInitialize(
            protocol_version=_attribute(result, "protocolVersion", "protocol_version"),
            capabilities=_json_mapping(_attribute(result, "capabilities")),
        )

    async def list_tools(self, cursor: str | None) -> McpSdkToolPage:
        result = await self._session.list_tools(cursor=cursor)
        raw_tools = _attribute(result, "tools")
        if type(raw_tools) is not list:
            raise _SdkFailure
        tools: list[McpSdkTool] = []
        for tool in raw_tools:
            tools.append(
                McpSdkTool(
                    name=_attribute(tool, "name"),
                    description=_optional_attribute(tool, "description"),
                    input_schema=_json_value(_attribute(tool, "inputSchema", "input_schema")),
                    output_schema=_optional_json_attribute(
                        tool, "outputSchema", "output_schema"
                    ),
                    annotations=_optional_json_attribute(tool, "annotations"),
                )
            )
        return McpSdkToolPage(
            tools=tuple(tools),
            next_cursor=_optional_attribute(result, "nextCursor", "next_cursor"),
        )

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> McpSdkCallResult:
        result = await self._session.call_tool(name, arguments=dict(arguments))
        raw_content = _attribute(result, "content")
        if type(raw_content) is not list:
            raise _SdkFailure
        content: list[dict[str, str]] = []
        for block in raw_content:
            payload = _json_mapping(block)
            if (
                payload.get("type") != "text"
                or type(payload.get("text")) is not str
            ):
                raise _SdkFailure
            content.append({"type": "text", "text": payload["text"]})
        return McpSdkCallResult(
            is_error=_attribute(result, "isError", "is_error"),
            content=content,
            structured_content=_optional_json_attribute(
                result, "structuredContent", "structured_content"
            ),
        )


def _bounded_httpx_factory(
    budget: _ByteBudget,
    *,
    deadline_seconds: int,
) -> Callable[..., Any]:
    def create_client(headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
        del timeout
        try:
            import httpx
        except ImportError:
            raise _SdkFailure from None

        class BoundedStream(httpx.AsyncByteStream):
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def __aiter__(self) -> Any:
                async for chunk in self._inner:
                    budget.record_response(len(chunk))
                    yield chunk

            async def aclose(self) -> None:
                await self._inner.aclose()

        class BoundedTransport(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self._inner = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

            async def handle_async_request(self, request: Any) -> Any:
                budget.record_request(request)
                response = await self._inner.handle_async_request(request)
                safe_headers = [
                    (key, value)
                    for key, value in response.headers.raw
                    if key.lower() != b"set-cookie"
                ]
                return httpx.Response(
                    status_code=response.status_code,
                    headers=safe_headers,
                    stream=BoundedStream(response.stream),
                    extensions=response.extensions,
                )

            async def aclose(self) -> None:
                await self._inner.aclose()

        return httpx.AsyncClient(
            auth=auth,
            headers=headers,
            timeout=httpx.Timeout(float(deadline_seconds)),
            follow_redirects=False,
            transport=BoundedTransport(),
            trust_env=False,
        )

    return create_client


_MISSING = object()


def _attribute(value: object, *names: str) -> Any:
    result = _optional_attribute(value, *names, default=_MISSING)
    if result is _MISSING:
        raise _SdkFailure
    return result


def _optional_attribute(value: object, *names: str, default: object = None) -> Any:
    for name in names:
        if type(value) is dict and name in value:
            return value[name]
        try:
            result = inspect_attribute(value, name)
        except (AttributeError, TypeError):
            continue
        return result
    return default


def inspect_attribute(value: object, name: str) -> Any:
    """Read a declared SDK model field without ever formatting the model."""

    return object.__getattribute__(value, name)


def _optional_json_attribute(value: object, *names: str) -> Any:
    raw = _optional_attribute(value, *names)
    return None if raw is None else _json_value(raw)


def _json_mapping(value: object) -> dict[str, Any]:
    normalized = _json_value(value)
    if type(normalized) is not dict:
        raise _SdkFailure
    return normalized


def _json_value(value: object) -> Any:
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        except (TypeError, ValueError, AttributeError):
            raise _SdkFailure from None
    try:
        encoded = canonical_json_bytes(
            value,
            maximum=1_048_576,
            code="mcp.result_invalid",
        )
    except McpContractError:
        raise _SdkFailure from None
    return json.loads(encoded)


__all__ = [
    "McpCallRequest",
    "McpCallResult",
    "McpConnectionMaterial",
    "McpDiscoverRequest",
    "McpDiscoveredTool",
    "McpDiscoveryResult",
    "McpExecutor",
    "McpExecutorError",
    "McpProbeRequest",
    "McpProbeResult",
    "McpSdkCallResult",
    "McpSdkInitialize",
    "McpSdkSession",
    "McpSdkSessionFactory",
    "McpSdkTool",
    "McpSdkToolPage",
    "call_mcp_tool",
    "discover_mcp_server",
    "probe_mcp_tool",
]
