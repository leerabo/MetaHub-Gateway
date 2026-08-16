"""PRD-084 local Endpoint network, call fence, stream, and durable event tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from metahub_gateway import local_model_events
from metahub_gateway.api import openai_compatible as openai_api
from metahub_gateway.local_model_events import (
    claim_local_model_events,
    dispatch_claimed_local_model_event,
    recover_stale_local_attempts,
)
from metahub_gateway.providers import local_network
from metahub_gateway.providers import openai_compatible as provider_api
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    ProviderRequest,
    ProviderStreamResponse,
)
from metahub_gateway.providers.local_network import (
    LocalEndpointNetworkError,
    LocalEndpointTarget,
    PinnedLocalEndpointTransport,
    resolve_local_endpoint_target,
)
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing import attempts as routing_attempts
from metahub_gateway.routing.attempts import (
    RoutingAttempt,
    create_routing_attempt,
    mark_local_provider_call_started,
    safe_complete_routing_attempt,
)
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.streaming.sessions import StreamSession
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError

NOW = "2026-08-16T00:00:00Z"


def _resolver_answers(*addresses: str):
    def getaddrinfo(_host: str, port: int, *_args: object):
        return [
            (2, 1, 6, "", (address, port))
            for address in addresses
        ]

    return getaddrinfo


@pytest.mark.anyio
async def test_local_network_validates_all_dns_answers_ports_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        local_network.socket,
        "getaddrinfo",
        _resolver_answers("10.20.0.9"),
    )
    target = await resolve_local_endpoint_target(
        "https://models.internal:8443/v1",
        private_cidrs=["10.20.0.0/16"],
        allowed_ports=[8443],
        allow_loopback=False,
    )
    assert target == LocalEndpointTarget(
        scheme="https",
        hostname="models.internal",
        port=8443,
        pinned_ip="10.20.0.9",
    )

    monkeypatch.setattr(
        local_network.socket,
        "getaddrinfo",
        _resolver_answers("10.20.0.9", "8.8.8.8"),
    )
    with pytest.raises(LocalEndpointNetworkError) as mixed:
        await resolve_local_endpoint_target(
            "https://models.internal:8443/v1",
            private_cidrs=["10.20.0.0/16"],
            allowed_ports=[8443],
            allow_loopback=False,
        )
    assert mixed.value.code == "gateway.local_endpoint_blocked"

    for metadata_address in (
        "169.254.169.254",
        "100.100.100.200",
        "::ffff:169.254.169.254",
        "::ffff:100.100.100.200",
        "fd00:ec2::254",
        "fd20:ce::254",
    ):
        monkeypatch.setattr(
            local_network.socket,
            "getaddrinfo",
            _resolver_answers(metadata_address),
        )
        with pytest.raises(LocalEndpointNetworkError) as ipv6_metadata:
            await resolve_local_endpoint_target(
                "http://metadata.internal:8000/v1",
                private_cidrs=["0.0.0.0/0", "::/0"],
                allowed_ports=[8000],
                allow_loopback=False,
            )
        assert ipv6_metadata.value.code == "gateway.local_endpoint_blocked"

    monkeypatch.setattr(
        local_network.socket,
        "getaddrinfo",
        _resolver_answers("127.0.0.1"),
    )
    with pytest.raises(LocalEndpointNetworkError):
        await resolve_local_endpoint_target(
            "http://localhost:11434/v1",
            private_cidrs=[],
            allowed_ports=[11434],
            allow_loopback=False,
        )
    loopback = await resolve_local_endpoint_target(
        "http://localhost:11434/v1",
        private_cidrs=[],
        allowed_ports=[11434],
        allow_loopback=True,
    )
    assert loopback.pinned_ip == "127.0.0.1"

    with pytest.raises(LocalEndpointNetworkError) as port:
        await resolve_local_endpoint_target(
            "http://localhost:9999/v1",
            private_cidrs=[],
            allowed_ports=[11434],
            allow_loopback=True,
        )
    assert port.value.code == "gateway.local_endpoint_blocked"


def _local_request() -> ProviderRequest:
    return ProviderRequest(
        provider_id="provider_local",
        provider_code="local",
        provider_type="local_endpoint",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="http://models.internal:8000/v1/chat/completions",
        headers={},
        payload={"model": "served-model", "messages": [], "stream": True},
        tenant_id="tenant_local",
        model_id="model_local",
        database_url="sqlite:///unused.db",
        routing_attempt_id="rta_" + "1" * 32,
        local_endpoint_private_cidrs=("10.20.0.0/16",),
        local_endpoint_allowed_ports=(8000,),
    )


@pytest.mark.anyio
async def test_local_send_fence_runs_only_after_network_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def approved(*_args: object, **_kwargs: object) -> LocalEndpointTarget:
        order.append("preflight")
        return LocalEndpointTarget("http", "models.internal", 8000, "10.20.0.9")

    def fenced(*_args: object, **_kwargs: object) -> None:
        order.append("fence")

    monkeypatch.setattr(
        "metahub_gateway.providers.openai_compatible.resolve_local_endpoint_target",
        approved,
    )
    monkeypatch.setattr(
        "metahub_gateway.providers.openai_compatible.mark_local_provider_call_started",
        fenced,
    )
    request = _local_request()
    transport = await provider_api._prepare_local_endpoint_transport(request)
    assert isinstance(transport, PinnedLocalEndpointTransport)
    assert order == ["preflight", "fence"]
    assert request.local_provider_call_started is True
    await transport.aclose()

    async def blocked(*_args: object, **_kwargs: object) -> LocalEndpointTarget:
        order.append("blocked")
        raise LocalEndpointNetworkError("gateway.local_endpoint_blocked")

    monkeypatch.setattr(
        "metahub_gateway.providers.openai_compatible.resolve_local_endpoint_target",
        blocked,
    )
    order.clear()
    with pytest.raises(LocalEndpointNetworkError):
        await provider_api._prepare_local_endpoint_transport(_local_request())
    assert order == ["blocked"]


@pytest.mark.anyio
async def test_local_overall_deadline_includes_network_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_preflight(*_args: object, **_kwargs: object) -> LocalEndpointTarget:
        await asyncio.sleep(0.05)
        return LocalEndpointTarget("http", "models.internal", 8000, "10.20.0.9")

    monkeypatch.setattr(provider_api, "_prepare_local_endpoint_transport", slow_preflight)
    request = _local_request()
    request.local_endpoint_overall_timeout_seconds = 0.01
    adapter = OpenAICompatibleAdapter()
    for send in (adapter.send_non_stream, adapter.send_stream):
        with pytest.raises(ApiError) as raised:
            await send(request)
        assert raised.value.code == "gateway.local_endpoint_unavailable"


class _DoneStream:
    def __init__(self) -> None:
        self.closed = False

    async def iter_bytes(self):
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


class _LocalStreamAdapter(OpenAICompatibleAdapter):
    def __init__(self, stream: _DoneStream) -> None:
        self.stream = stream
        self.request: ProviderRequest | None = None

    async def send_stream(self, request: ProviderRequest):
        self.request = request
        return self.stream


@pytest.mark.anyio
async def test_local_stream_uses_routing_attempt_without_double_quality_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = GatewayRoute(
        tenant_id="tenant_local",
        model_id="model_local",
        model_alias="local-chat",
        upstream_model_name="served-model",
        capabilities={"chat": True, "stream": True},
        provider_id="provider_local",
        provider_code="local",
        provider_type="local_endpoint",
        provider_auth_type="none",
        provider_name="Local",
        provider_base_url="http://models.internal:8000/v1",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    attempt = RoutingAttempt(
        routing_attempt_id="rta_" + "2" * 32,
        tenant_id=route.tenant_id,
        request_id="req_local_stream",
        trace_id="trc_local_stream",
        attempt_sequence=1,
        attempt_role="primary",
        route=route,
    )
    completed: list[dict[str, Any]] = []
    captured_attempt_ids: list[str | None] = []
    stream = _DoneStream()
    adapter = _LocalStreamAdapter(stream)

    monkeypatch.setattr(openai_api, "create_routing_attempt", lambda *_a, **_k: attempt)
    monkeypatch.setattr(
        openai_api,
        "safe_complete_routing_attempt",
        lambda *_a, **kwargs: completed.append(kwargs),
    )

    def build_request(*_args: object, **kwargs: object) -> ProviderRequest:
        captured_attempt_ids.append(kwargs["routing_attempt_id"])
        request = _local_request()
        request.routing_attempt_id = kwargs["routing_attempt_id"]
        return request

    monkeypatch.setattr(openai_api, "_provider_request_with_affinity", build_request)

    def create_session(
        _config: object,
        context: RequestLogContext,
        *,
        inter_token_timeout_ms: int,
    ) -> StreamSession:
        return StreamSession(
            stream_session_id="str_local_stream",
            tenant_id=context.tenant_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            model_id=context.model_id,
            provider_id=context.provider_id,
            provider_code=context.provider_code,
            inter_token_timeout_ms=inter_token_timeout_ms,
            started_at=datetime.now(UTC),
        )

    monkeypatch.setattr(openai_api, "create_stream_session", create_session)
    monkeypatch.setattr(
        openai_api,
        "mark_stream_session_active",
        lambda _config, session: setattr(session, "status", "active"),
    )
    monkeypatch.setattr(
        openai_api,
        "safe_finish_stream_session",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(openai_api, "safe_record_request_log", lambda *_a, **_k: None)
    monkeypatch.setattr(openai_api, "safe_record_usage_ledger", lambda *_a, **_k: None)

    context = RequestLogContext(
        tenant_id=route.tenant_id,
        request_id=attempt.request_id,
        trace_id=attempt.trace_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        model_id=route.model_id,
    )
    client_request = SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    response = await openai_api._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        adapter,
        None,
        0.0,
        None,
        client_request=client_request,
        route=route,
        route_plan=plan,
        endpoint="chat/completions",
        payload={"model": "local-chat", "messages": [], "stream": True},
    )
    assert [chunk async for chunk in response.body_iterator] == [b"data: [DONE]\n\n"]
    assert captured_attempt_ids == [attempt.routing_attempt_id]
    assert adapter.request is not None
    assert completed == [
        {
            "succeeded": True,
            "latency_ms": completed[0]["latency_ms"],
            "quality_observation_eligible": True,
            "record_quality_observation": False,
        }
    ]
    assert stream.closed is True


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare_local_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    database_url = _sqlite_url(tmp_path / "prd084-events.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin' LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    assert admin is not None
    provider_id = "provider_prd084_runtime"
    model_id = "model_prd084_runtime"
    endpoint_id = "lme_" + "8" * 32
    connection.execute(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
          provider_type,auth_type,created_by
        ) VALUES(?,?,'prd084_runtime','Local runtime','http://local.invalid:8000/v1',
                 'active',?,?,'local_endpoint','none',?)
        """,
        (provider_id, seed.tenant_id, NOW, NOW, admin["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,created_at,updated_at,provider_model_id,display_name,
          context_window,input_price_per_1k,output_price_per_1k,currency,created_by
        ) VALUES(?,?,?,'prd084-runtime','served-model','{"chat":true,"stream":true}',
                 'active',?,?,'served-model','Local runtime',32768,0,0,'CNY',?)
        """,
        (model_id, seed.tenant_id, provider_id, NOW, NOW, admin["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO local_model_endpoints(
          local_endpoint_id,tenant_id,provider_id,initial_model_id,engine_kind,
          revision,catalog_revision,catalog_status,created_by,status,created_at,updated_at
        ) VALUES(?,?,?,?, 'vllm',1,0,'unknown',?,'active',?,?)
        """,
        (endpoint_id, seed.tenant_id, provider_id, model_id, admin["user_id"], NOW, NOW),
    )
    connection.commit()
    connection.close()
    config = load_app_config("gateway-data-plane")
    route = GatewayRoute(
        tenant_id=seed.tenant_id,
        model_id=model_id,
        model_alias="prd084-runtime",
        upstream_model_name="served-model",
        capabilities={"chat": True, "stream": True},
        provider_id=provider_id,
        provider_code="prd084_runtime",
        provider_type="local_endpoint",
        provider_auth_type="none",
        provider_name="Local runtime",
        provider_base_url="http://local.invalid:8000/v1",
    )
    return config, route, GatewayRoutePlan(initial_route=route, initial_role="primary")


def _create_attempt(config, route: GatewayRoute, plan: GatewayRoutePlan, marker: str):
    return create_routing_attempt(
        config,
        plan,
        route,
        request_id=f"req_prd084_{marker}",
        trace_id=f"trc_prd084_{marker}",
        attempt_sequence=1,
        attempt_role="primary",
    )


def test_local_attempt_recovery_and_outbox_delivery_are_durable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, route, plan = _prepare_local_database(monkeypatch, tmp_path)
    stale_started_at = datetime(2026, 8, 16, tzinfo=UTC)
    monkeypatch.setattr(routing_attempts, "_utcnow", lambda: stale_started_at)
    called = _create_attempt(config, route, plan, "called_stale")
    mark_local_provider_call_started(
        config.database_url,
        tenant_id=route.tenant_id,
        routing_attempt_id=called.routing_attempt_id,
    )
    not_called = _create_attempt(config, route, plan, "not_called_stale")

    recovered_at = datetime(2026, 8, 16, 12, tzinfo=UTC)
    assert recover_stale_local_attempts(config, now=recovered_at) == 2
    connection = connect_database(config.database_url)
    called_event = connection.execute(
        "SELECT outcome,error_code FROM local_model_call_events WHERE routing_attempt_id=?",
        (called.routing_attempt_id,),
    ).fetchone()
    assert tuple(called_event) == (
        "outcome_unknown",
        "gateway.local_endpoint_outcome_unknown",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM local_model_call_events WHERE routing_attempt_id=?",
        (not_called.routing_attempt_id,),
    ).fetchone()[0] == 0
    connection.close()
    succeeded = _create_attempt(config, route, plan, "success")
    mark_local_provider_call_started(
        config.database_url,
        tenant_id=route.tenant_id,
        routing_attempt_id=succeeded.routing_attempt_id,
    )
    safe_complete_routing_attempt(
        config,
        succeeded,
        succeeded=True,
        latency_ms=42,
        quality_observation_eligible=True,
        record_quality_observation=False,
    )

    claim_time = datetime(2026, 8, 17, tzinfo=UTC)
    first_batch = claim_local_model_events(config, "worker-a", now=claim_time, limit=16)
    assert len(first_batch) == 1
    first_event = local_model_events._load_source_event(config, first_batch[0])
    replay = local_model_events._load_source_event(config, first_batch[0])
    assert first_event.event_id == replay.event_id
    assert first_event.payload == replay.payload

    delivered: list[str] = []
    monkeypatch.setattr(
        local_model_events,
        "dispatch_runtime_webhook_event",
        lambda _config, event: delivered.append(event.event_id),
    )
    assert dispatch_claimed_local_model_event(
        config,
        first_batch[0],
        now=claim_time,
    )
    assert delivered == [first_event.event_id]

    second_batch = claim_local_model_events(config, "worker-b", now=claim_time, limit=16)
    assert len(second_batch) == 1
    original_generation = second_batch[0].lease_generation
    original_event_id = second_batch[0].event_id

    def fail_delivery(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("test-only delivery failure")

    monkeypatch.setattr(local_model_events, "dispatch_runtime_webhook_event", fail_delivery)
    assert not dispatch_claimed_local_model_event(
        config,
        second_batch[0],
        now=claim_time,
    )
    retried = claim_local_model_events(
        config,
        "worker-c",
        now=claim_time + timedelta(seconds=3),
        limit=16,
    )
    assert len(retried) == 1
    assert retried[0].event_id == original_event_id
    assert retried[0].lease_generation > original_generation
    assert retried[0].attempt_count == 2

    connection = connect_database(config.database_url)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_called_cancellation_preserves_unavailable_usage_without_quality_penalty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, route, plan = _prepare_local_database(monkeypatch, tmp_path)
    invalid = _create_attempt(config, route, plan, "called_cancelled_invalid")
    mark_local_provider_call_started(
        config.database_url,
        tenant_id=route.tenant_id,
        routing_attempt_id=invalid.routing_attempt_id,
    )
    cancellation_error = NormalizedProviderError(
        code="gateway.client_disconnected",
        message="",
        http_status=None,
        retryable=False,
        provider_error_type="gateway",
        error_category="other",
    )
    safe_complete_routing_attempt(
        config,
        invalid,
        succeeded=False,
        latency_ms=11,
        error=cancellation_error,
        usage_status="not_applicable",
        quality_observation_eligible=False,
    )
    connection = connect_database(config.database_url)
    invalid_row = connection.execute(
        "SELECT status,usage_status FROM routing_attempts WHERE routing_attempt_id=?",
        (invalid.routing_attempt_id,),
    ).fetchone()
    assert tuple(invalid_row) == ("started", "pending")
    connection.close()

    attempt = _create_attempt(config, route, plan, "called_cancelled")
    mark_local_provider_call_started(
        config.database_url,
        tenant_id=route.tenant_id,
        routing_attempt_id=attempt.routing_attempt_id,
    )
    safe_complete_routing_attempt(
        config,
        attempt,
        succeeded=False,
        latency_ms=17,
        error=cancellation_error,
        usage_status="unavailable",
        usage_exposure_possible=True,
        quality_observation_eligible=False,
    )
    connection = connect_database(config.database_url)
    attempt_row = connection.execute(
        "SELECT status,usage_status FROM routing_attempts WHERE routing_attempt_id=?",
        (attempt.routing_attempt_id,),
    ).fetchone()
    event_row = connection.execute(
        "SELECT outcome,usage_status FROM local_model_call_events WHERE routing_attempt_id=?",
        (attempt.routing_attempt_id,),
    ).fetchone()
    quality_row = connection.execute(
        "SELECT sample_count,error_count FROM model_quality_scores "
        "WHERE tenant_id=? AND model_id=?",
        (route.tenant_id, route.model_id),
    ).fetchone()
    assert tuple(attempt_row) == ("failed", "unavailable")
    assert tuple(event_row) == ("failed", "unavailable")
    assert tuple(quality_row) == (0, 0)
    connection.close()


@pytest.mark.anyio
async def test_local_stream_byte_limit_uses_frozen_public_error() -> None:
    class Response:
        async def aiter_bytes(self):
            yield b"123"
            yield b"45"

    class StreamContext:
        async def __aexit__(self, *_args: object) -> None:
            return None

    class Client:
        async def aclose(self) -> None:
            return None

    stream = ProviderStreamResponse(
        client=Client(),
        response=Response(),
        stream_context=StreamContext(),
        max_bytes=4,
    )
    with pytest.raises(ApiError) as raised:
        _ = [chunk async for chunk in stream.iter_bytes()]
    assert raised.value.code == "gateway.local_endpoint_unavailable"
