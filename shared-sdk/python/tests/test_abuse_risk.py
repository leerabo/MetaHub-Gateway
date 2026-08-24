"""PRD-110 privacy-bounded abuse-risk runtime contracts."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

import pytest
from metahub_shared.abuse_risk import (
    DegradedFact,
    InMemoryRiskCounterStore,
    RiskCounterUnavailable,
    RiskRuntime,
    persistence_sink,
)
from metahub_shared.db import apply_migrations, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo


def _config(database_url: str = "sqlite:///:memory:") -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        allowed_origins=["http://localhost:5173"],
        control_background_schedulers_enabled=False,
    )


@pytest.mark.anyio
async def test_atomic_dimensions_threshold_and_subject_hmac() -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
    store = InMemoryRiskCounterStore()
    facts = []

    async def sink(batch) -> None:
        facts.extend(batch)

    runtime = RiskRuntime(_config(), store=store, sink=sink, now=lambda: now)
    await runtime.start()
    subjects = {
        "ip_prefix": "203.0.113.42",
        "device": "local-device-secret",
        "account": "person@example.invalid",
    }
    try:
        for expected in range(1, 52):
            result = await runtime.assess(
                "login",
                subjects,
                request_id=f"request-{expected}",
                trace_id=f"trace-{expected}",
            )
            assert result.max_count == expected
            assert result.outcome == ("challenge" if expected > 50 else "allow")
        await runtime._queue.join()
    finally:
        await runtime.stop()

    counter_facts = [fact for fact in facts if hasattr(fact, "subject_hmac")]
    assert len(counter_facts) == 51 * 3
    assert {fact.subject_kind for fact in counter_facts} == {
        "account",
        "device",
        "ip_prefix",
    }
    serialized = repr(facts)
    assert "203.0.113.42" not in serialized
    assert "local-device-secret" not in serialized
    assert "person@example.invalid" not in serialized
    assert all(len(fact.subject_hmac) == 64 for fact in counter_facts)


class _UnavailableStore:
    async def increment_many(self, claims):
        raise RiskCounterUnavailable("test unavailable")


@pytest.mark.anyio
async def test_backend_failure_split_and_queue_backpressure() -> None:
    runtime = RiskRuntime(_config(), store=_UnavailableStore(), queue_size=1)
    privilege = await runtime.assess(
        "registration",
        {"ip_prefix": "203.0.113.7"},
        request_id="request-privilege",
        trace_id="trace-privilege",
    )
    gateway = await runtime.assess(
        "gateway_api",
        {"workspace": "wsp_" + "1" * 32},
        request_id="request-gateway",
        trace_id="trace-gateway",
    )

    assert (privilege.outcome, privilege.degraded) == ("challenge", True)
    assert (gateway.outcome, gateway.degraded) == ("allow", True)
    assert runtime.fixed_counters == {
        "backend_failure": 2,
        "queue_full": 1,
        "privilege_fail_closed": 1,
        "gateway_fail_open": 1,
    }


@pytest.mark.anyio
async def test_degraded_counters_are_persisted(tmp_path) -> None:
    database = tmp_path / "risk-runtime.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    assert apply_migrations(database_url).applied[-1] == "0102"
    sink = persistence_sink(_config(database_url))

    await sink(
        (
            DegradedFact(kind="privilege_fail_closed"),
            DegradedFact(kind="privilege_fail_closed"),
            DegradedFact(kind="gateway_fail_open"),
        )
    )

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT privilege_fail_closed_count,gateway_fail_open_count,revision "
            "FROM risk_degraded_counters"
        ).fetchone()
        assert tuple(row) == (2, 1, 2)
    finally:
        connection.close()
