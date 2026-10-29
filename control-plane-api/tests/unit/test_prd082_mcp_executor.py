from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
from metahub_control.mcp_governance import executor as subject
from metahub_shared.mcp import McpSchemaArtifact, McpSchemaBundle, McpSchemaLimits


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "environment": "dev",
        "mcp_enabled": True,
        "mcp_executor_mode": "test_loopback",
        "mcp_test_loopback_enabled": True,
        "mcp_probe_deadline_seconds": 10,
        "mcp_call_deadline_seconds": 30,
        "mcp_arguments_max_bytes": 65_536,
        "mcp_result_max_bytes": 1_048_576,
        "mcp_server_schema_max_bytes": 1_048_576,
        "mcp_schema_limits": McpSchemaLimits(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _connection(endpoint: str = "http://127.0.0.1:8765/mcp") -> subject.McpConnectionMaterial:
    return subject.McpConnectionMaterial(
        endpoint=endpoint,
        bearer_token="executor-token-canary",
    )


def _tool(name: str) -> subject.McpSdkTool:
    return subject.McpSdkTool(
        name=name,
        description=f"description-{name}",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        annotations={"readOnlyHint": True},
    )


def _schema_bundle() -> McpSchemaBundle:
    raw = '{"type":"object"}'
    digest = sha256(raw.encode()).hexdigest()
    artifact = McpSchemaArtifact(
        original={"type": "object"},
        effective={"type": "object"},
        original_json=raw,
        effective_json=raw,
        original_sha256=digest,
        effective_sha256=digest,
        original_byte_count=len(raw),
        effective_byte_count=len(raw),
    )
    return McpSchemaBundle(input=artifact, output=None)


@pytest.fixture(autouse=True)
def _stub_optional_jsonschema(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _schema_bundle()
    monkeypatch.setattr(
        subject,
        "canonicalize_and_harden_schemas",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        subject,
        "validate_server_schema_budget",
        lambda bundles, *, maximum: sum(item.total_byte_count for item in bundles),
    )


class FakeSession:
    def __init__(
        self,
        *,
        pages: dict[str | None, subject.McpSdkToolPage] | None = None,
        call_result: subject.McpSdkCallResult | None = None,
        initialize_error: Exception | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self.pages = pages or {None: subject.McpSdkToolPage(())}
        self.call_result = call_result or subject.McpSdkCallResult(False, [], {"ok": True})
        self.initialize_error = initialize_error
        self.call_error = call_error
        self.request_bytes = 113
        self.response_bytes = 257
        self.cursors: list[str | None] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> subject.McpSdkInitialize:
        if self.initialize_error is not None:
            raise self.initialize_error
        return subject.McpSdkInitialize(
            protocol_version="2025-06-18",
            capabilities={"tools": {"listChanged": True}},
        )

    async def list_tools(self, cursor: str | None) -> subject.McpSdkToolPage:
        self.cursors.append(cursor)
        return self.pages[cursor]

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> subject.McpSdkCallResult:
        self.calls.append((name, dict(arguments)))
        if self.call_error is not None:
            raise self.call_error
        return self.call_result


class FakeFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.connections: list[tuple[subject.McpConnectionMaterial, int, int]] = []

    def __call__(
        self,
        connection: subject.McpConnectionMaterial,
        deadline_seconds: int,
        max_response_bytes: int,
    ) -> Any:
        self.connections.append((connection, deadline_seconds, max_response_bytes))

        @asynccontextmanager
        async def context() -> Any:
            yield self.session

        return context()


def test_disabled_mode_precedes_sdk_loading_and_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeFactory(FakeSession())

    def sdk_bomb() -> None:
        raise AssertionError("SDK loader must not run")

    monkeypatch.setattr(subject, "_load_sdk_bindings", sdk_bomb)
    executor = subject.McpExecutor(
        _config(mcp_executor_mode="disabled"),  # type: ignore[arg-type]
        session_factory=factory,
    )

    with pytest.raises(subject.McpExecutorError, match="^mcp.executor_disabled$"):
        executor.discover(subject.McpDiscoverRequest(_connection()))

    assert factory.connections == []


def test_production_profile_performs_zero_dns_or_socket_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeFactory(FakeSession())

    def network_bomb(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("production executor attempted network work")

    monkeypatch.setattr("socket.getaddrinfo", network_bomb)
    monkeypatch.setattr("socket.socket", network_bomb)
    executor = subject.McpExecutor(  # type: ignore[arg-type]
        _config(environment="prod"),
        session_factory=factory,
    )

    with pytest.raises(subject.McpExecutorError, match="^mcp.executor_disabled$"):
        executor.discover(subject.McpDiscoverRequest(_connection()))

    assert factory.connections == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8765/mcp",
        "http://127.0.0.2:8765/mcp",
        "http://10.0.0.1:8765/mcp",
        "https://127.0.0.1:8765/mcp",
        "http://[::2]:8765/mcp",
        "http://127.0.0.1:8765/mcp?token=value",
    ],
)
def test_test_adapter_rejects_every_non_literal_http_loopback_endpoint(endpoint: str) -> None:
    factory = FakeFactory(FakeSession())
    executor = subject.McpExecutor(_config(), session_factory=factory)  # type: ignore[arg-type]

    with pytest.raises(subject.McpExecutorError, match="^mcp.executor_disabled$"):
        executor.discover(subject.McpDiscoverRequest(_connection(endpoint)))

    assert factory.connections == []


def test_discovery_collects_bounded_pages_and_returns_safe_contract() -> None:
    session = FakeSession(
        pages={
            None: subject.McpSdkToolPage((_tool("search"),), "cursor-1"),
            "cursor-1": subject.McpSdkToolPage((_tool("lookup"),), None),
        }
    )
    factory = FakeFactory(session)
    request = subject.McpDiscoverRequest(_connection(), deadline_seconds=7)

    result = subject.McpExecutor(  # type: ignore[arg-type]
        _config(), session_factory=factory
    ).discover(request)

    assert [item.name for item in result.tools] == ["lookup", "search"]
    assert result.protocol_version == "2025-06-18"
    assert result.capabilities_sha256 == sha256(result.capabilities_json.encode()).hexdigest()
    assert (result.request_bytes, result.response_bytes) == (113, 257)
    assert session.cursors == [None, "cursor-1"]
    assert factory.connections[0][1:] == (7, 1_048_576)
    representation = repr((request, result))
    assert "executor-token-canary" not in representation
    assert "description-search" not in representation
    assert "listChanged" not in representation


def test_literal_ipv6_loopback_is_accepted() -> None:
    factory = FakeFactory(FakeSession())
    executor = subject.McpExecutor(_config(), session_factory=factory)  # type: ignore[arg-type]

    result = executor.discover(
        subject.McpDiscoverRequest(_connection("http://[::1]:8765/mcp"))
    )

    assert result.tools == ()
    assert len(factory.connections) == 1


@pytest.mark.parametrize("failure", ["duplicate_name", "duplicate_cursor", "page_limit"])
def test_discovery_fails_closed_for_duplicate_and_page_limits(failure: str) -> None:
    if failure == "duplicate_name":
        pages = {
            None: subject.McpSdkToolPage((_tool("same"),), "next"),
            "next": subject.McpSdkToolPage((_tool("same"),), None),
        }
    elif failure == "duplicate_cursor":
        pages = {
            None: subject.McpSdkToolPage((_tool("one"),), "same-cursor"),
            "same-cursor": subject.McpSdkToolPage((_tool("two"),), "same-cursor"),
        }
    else:
        pages = {
            None: subject.McpSdkToolPage((_tool("tool-0"),), "cursor-1"),
            **{
                f"cursor-{index}": subject.McpSdkToolPage(
                    (_tool(f"tool-{index}"),),
                    f"cursor-{index + 1}",
                )
                for index in range(1, 10)
            },
        }
    factory = FakeFactory(FakeSession(pages=pages))

    with pytest.raises(subject.McpExecutorError, match="^mcp.sync_failed$"):
        subject.McpExecutor(_config(), session_factory=factory).discover(  # type: ignore[arg-type]
            subject.McpDiscoverRequest(_connection())
        )


def test_discovery_rejects_more_than_one_hundred_tools() -> None:
    page = subject.McpSdkToolPage(tuple(_tool(f"tool-{index}") for index in range(101)))
    factory = FakeFactory(FakeSession(pages={None: page}))

    with pytest.raises(subject.McpExecutorError, match="^mcp.sync_failed$"):
        subject.McpExecutor(_config(), session_factory=factory).discover(  # type: ignore[arg-type]
            subject.McpDiscoverRequest(_connection())
        )


def test_probe_returns_only_the_requested_remote_tool() -> None:
    session = FakeSession(
        pages={None: subject.McpSdkToolPage((_tool("one"), _tool("target")))}
    )
    executor = subject.McpExecutor(  # type: ignore[arg-type]
        _config(), session_factory=FakeFactory(session)
    )

    result = executor.probe(subject.McpProbeRequest(_connection(), "target"))

    assert result.tool.name == "target"
    with pytest.raises(subject.McpExecutorError, match="^mcp.server_unavailable$"):
        executor.probe(subject.McpProbeRequest(_connection(), "missing"))


def test_call_returns_only_text_and_structured_json_with_metrics() -> None:
    session = FakeSession(
        call_result=subject.McpSdkCallResult(
            False,
            [{"type": "text", "text": "safe result"}],
            {"count": 2},
        )
    )
    factory = FakeFactory(session)
    request = subject.McpCallRequest(
        _connection(),
        "search",
        {"query": "argument-canary"},
        deadline_seconds=12,
    )

    result = subject.McpExecutor(  # type: ignore[arg-type]
        _config(), session_factory=factory
    ).call(request)

    assert result.is_error is False
    assert result.content == [{"type": "text", "text": "safe result"}]
    assert result.structured_content == {"count": 2}
    assert session.calls == [("search", {"query": "argument-canary"})]
    assert factory.connections[0][1] == 12
    representation = repr((request, result))
    assert "argument-canary" not in representation
    assert "safe result" not in representation


def test_call_maps_pre_call_call_unknown_and_known_invalid_result() -> None:
    cases = [
        (
            FakeSession(initialize_error=RuntimeError("endpoint-and-token-canary")),
            "mcp.server_unavailable",
        ),
        (
            FakeSession(call_error=RuntimeError("endpoint-and-token-canary")),
            "mcp.outcome_unknown",
        ),
        (
            FakeSession(
                call_result=subject.McpSdkCallResult(
                    False,
                    [{"type": "image", "data": "result-canary"}],
                    None,
                )
            ),
            "mcp.result_invalid",
        ),
    ]

    for session, expected in cases:
        executor = subject.McpExecutor(  # type: ignore[arg-type]
            _config(), session_factory=FakeFactory(session)
        )
        with pytest.raises(subject.McpExecutorError) as captured:
            executor.call(subject.McpCallRequest(_connection(), "search", {}))
        assert captured.value.code == expected
        assert str(captured.value) == expected
        assert "canary" not in repr(captured.value)


def test_deadline_cannot_expand_the_deployment_limit() -> None:
    factory = FakeFactory(FakeSession())
    executor = subject.McpExecutor(  # type: ignore[arg-type]
        _config(mcp_probe_deadline_seconds=10), session_factory=factory
    )

    with pytest.raises(subject.McpExecutorError, match="^mcp.unavailable$"):
        executor.discover(subject.McpDiscoverRequest(_connection(), deadline_seconds=11))

    assert factory.connections == []


def test_stream_budget_aborts_on_the_chunk_that_crosses_the_limit() -> None:
    budget = subject._ByteBudget(maximum=10)

    budget.record_response(6)
    with pytest.raises(subject._SdkFailure):
        budget.record_response(5)

    assert budget.response_bytes == 11
