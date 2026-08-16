"""PRD-084 durable local_model.called delivery crash-recovery tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Initialize the provider registry before importing routing attempts.
import metahub_gateway.providers  # noqa: F401
import pytest
from metahub_gateway import local_model_events
from metahub_gateway.local_model_events import (
    claim_local_model_events,
    dispatch_claimed_local_model_event,
)
from metahub_gateway.routing.attempts import (
    create_routing_attempt,
    mark_local_provider_call_started,
    safe_complete_routing_attempt,
)
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import encrypt_secret, secret_fingerprint, secret_prefix
from metahub_shared.runtime import webhook_delivery
from metahub_shared.runtime.config import load_app_config

NOW = datetime(2030, 8, 16, 12, tzinfo=UTC)
NOW_TEXT = "2026-08-16T12:00:00Z"
SENSITIVE_URL = "https://private-callback.invalid/secret-path"
SENSITIVE_SECRET = "whsec_prd084_must_never_appear_in_logs"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare_event_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = _sqlite_url(tmp_path / "prd084-delivery-reliability.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "prd084-test-only-encryption-secret-with-sufficient-length",
    )
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin' LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    assert admin is not None

    provider_id = "provider_prd084_delivery"
    model_id = "model_prd084_delivery"
    local_endpoint_id = "lme_" + "d" * 32
    connection.execute(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
          provider_type,auth_type,created_by
        ) VALUES(?,?,'prd084_delivery','Local delivery','http://local.invalid:8000/v1',
                 'active',?,?,'local_endpoint','none',?)
        """,
        (provider_id, seed.tenant_id, NOW_TEXT, NOW_TEXT, admin["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,created_at,updated_at,provider_model_id,display_name,
          context_window,input_price_per_1k,output_price_per_1k,currency,created_by
        ) VALUES(?,?,?,'prd084-delivery','served-model','{"chat":true}',
                 'active',?,?,'served-model','Local delivery',32768,0,0,'CNY',?)
        """,
        (model_id, seed.tenant_id, provider_id, NOW_TEXT, NOW_TEXT, admin["user_id"]),
    )
    connection.execute(
        """
        INSERT INTO local_model_endpoints(
          local_endpoint_id,tenant_id,provider_id,initial_model_id,engine_kind,
          revision,catalog_revision,catalog_status,created_by,status,created_at,updated_at
        ) VALUES(?,?,?,?, 'vllm',1,0,'unknown',?,'active',?,?)
        """,
        (
            local_endpoint_id,
            seed.tenant_id,
            provider_id,
            model_id,
            admin["user_id"],
            NOW_TEXT,
            NOW_TEXT,
        ),
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at,payload_format,
          security_event_rate_limit_per_minute
        ) VALUES(?,?,?,?,?, ?,?,?,'active',30000,3,?,?,?,?, 'json',60)
        """,
        (
            "wh_" + "e" * 32,
            seed.tenant_id,
            "Local model event receiver",
            SENSITIVE_URL,
            '["local_model.called"]',
            encrypt_secret(SENSITIVE_SECRET),
            secret_prefix(SENSITIVE_SECRET),
            secret_fingerprint(SENSITIVE_SECRET),
            admin["user_id"],
            admin["user_id"],
            NOW_TEXT,
            NOW_TEXT,
        ),
    )
    connection.commit()
    connection.close()

    config = load_app_config("gateway-data-plane")
    route = GatewayRoute(
        tenant_id=seed.tenant_id,
        model_id=model_id,
        model_alias="prd084-delivery",
        upstream_model_name="served-model",
        capabilities={"chat": True},
        provider_id=provider_id,
        provider_code="prd084_delivery",
        provider_type="local_endpoint",
        provider_auth_type="none",
        provider_name="Local delivery",
        provider_base_url="http://local.invalid:8000/v1",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    attempt = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd084_delivery",
        trace_id="trc_prd084_delivery",
        attempt_sequence=1,
        attempt_role="primary",
    )
    mark_local_provider_call_started(
        database_url,
        tenant_id=seed.tenant_id,
        routing_attempt_id=attempt.routing_attempt_id,
    )
    safe_complete_routing_attempt(
        config,
        attempt,
        succeeded=True,
        latency_ms=42,
        quality_observation_eligible=True,
        record_quality_observation=False,
    )
    return config


def _outbox_row(config):
    with connect_database(config.database_url) as connection:
        return connection.execute(
            "SELECT * FROM local_model_call_event_outbox"
        ).fetchone()


def _delivery_rows(config):
    with connect_database(config.database_url) as connection:
        deliveries = connection.execute(
            "SELECT * FROM webhook_deliveries ORDER BY webhook_delivery_id"
        ).fetchall()
        attempts = connection.execute(
            "SELECT * FROM webhook_delivery_attempts ORDER BY attempt_no"
        ).fetchall()
    return deliveries, attempts


def _success_result() -> dict[str, object]:
    return {
        "status": "success",
        "http_status": 204,
        "duration_ms": 1,
        "error_code": None,
        "error_summary": None,
    }


def test_pending_intent_cannot_mark_source_sent_and_resumes_same_event_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _prepare_event_database(monkeypatch, tmp_path)
    claim = claim_local_model_events(config, "worker-a", now=NOW, limit=16)[0]
    sent_event_ids: list[str] = []

    def successful_network(_config, _endpoint, body: bytes):
        sent_event_ids.append(str(json.loads(body)["event_id"]))
        return _success_result()

    monkeypatch.setattr(webhook_delivery, "_send_once", successful_network)
    original_record_attempt = webhook_delivery._record_attempt

    def crash_after_network(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("test-only post-network crash")

    monkeypatch.setattr(webhook_delivery, "_record_attempt", crash_after_network)
    assert not dispatch_claimed_local_model_event(config, claim, now=NOW)
    deliveries, attempts = _delivery_rows(config)
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "pending"
    assert deliveries[0]["attempt_count"] == 0
    assert attempts == []
    assert _outbox_row(config)["status"] == "pending"

    monkeypatch.setattr(webhook_delivery, "_record_attempt", original_record_attempt)
    second_at = NOW + timedelta(seconds=3)
    second = claim_local_model_events(config, "worker-b", now=second_at, limit=16)[0]
    original_dispatch = local_model_events.dispatch_runtime_webhook_event
    monkeypatch.setattr(
        local_model_events,
        "dispatch_runtime_webhook_event",
        lambda _config, _event: None,
    )
    assert not dispatch_claimed_local_model_event(config, second, now=second_at)
    assert _outbox_row(config)["status"] == "pending"
    assert _delivery_rows(config)[0][0]["status"] == "pending"

    monkeypatch.setattr(local_model_events, "dispatch_runtime_webhook_event", original_dispatch)
    third_at = second_at + timedelta(seconds=5)
    third = claim_local_model_events(config, "worker-c", now=third_at, limit=16)[0]
    assert dispatch_claimed_local_model_event(config, third, now=third_at)

    deliveries, attempts = _delivery_rows(config)
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "success"
    assert deliveries[0]["attempt_count"] == 1
    assert len(attempts) == 1
    assert _outbox_row(config)["status"] == "sent"
    assert len(sent_event_ids) == 2
    assert len(set(sent_event_ids)) == 1
    assert sent_event_ids[0] == f"evt_{claim.event_id}"

    log_text = caplog.text
    assert SENSITIVE_URL not in log_text
    assert SENSITIVE_SECRET not in log_text
    assert "prompt" not in log_text.lower()
    assert "response" not in log_text.lower()


def test_expired_source_lease_recovers_terminal_delivery_without_resending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _prepare_event_database(monkeypatch, tmp_path)
    assert local_model_events._BATCH_SIZE == 1
    assert local_model_events._LEASE_SECONDS >= (20 * 3 * 30) + 60

    first = claim_local_model_events(config, "worker-a", now=NOW, limit=16)[0]
    network_calls: list[str] = []

    def successful_network(_config, _endpoint, body: bytes):
        network_calls.append(str(json.loads(body)["event_id"]))
        return _success_result()

    monkeypatch.setattr(webhook_delivery, "_send_once", successful_network)
    original_settle_sent = local_model_events._settle_sent

    def crash_before_source_settle(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("test-only source settle crash")

    monkeypatch.setattr(local_model_events, "_settle_sent", crash_before_source_settle)
    with pytest.raises(RuntimeError, match="source settle crash"):
        dispatch_claimed_local_model_event(config, first, now=NOW)
    assert _outbox_row(config)["status"] == "leased"
    assert _delivery_rows(config)[0][0]["status"] == "success"

    monkeypatch.setattr(local_model_events, "_settle_sent", original_settle_sent)
    recovered_at = NOW + timedelta(seconds=local_model_events._LEASE_SECONDS + 1)
    recovered = claim_local_model_events(
        config,
        "worker-b",
        now=recovered_at,
        limit=16,
    )[0]
    assert recovered.lease_generation > first.lease_generation
    assert dispatch_claimed_local_model_event(config, recovered, now=recovered_at)
    assert _outbox_row(config)["status"] == "sent"
    assert network_calls == [f"evt_{first.event_id}"]

    deliveries, attempts = _delivery_rows(config)
    assert len(deliveries) == 1
    assert len(attempts) == 1
    with connect_database(config.database_url) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
