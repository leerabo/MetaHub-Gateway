"""Recovery ordering for provider failures across successive requests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from metahub_gateway.cache_affinity import routing as affinity_routing
from metahub_gateway.routing import models
from metahub_gateway.routing.cooldown import RouteCooldownRegistry
from metahub_shared.runtime.routing_policy import (
    RoutingCandidateInput,
    RoutingCandidateResult,
    RoutingSelectionResult,
)


def _selection() -> RoutingSelectionResult:
    results = []
    for rank, model_id in enumerate(("primary", "backup-a", "backup-b"), start=1):
        candidate = RoutingCandidateInput(
            candidate_role="default" if rank == 1 else "fallback",
            configured_priority=rank - 1,
            model_id=model_id,
            provider_id=f"provider-{model_id}",
            model_status="active",
            provider_status="active",
            health_state="unknown",
            health_fresh=False,
        )
        results.append(
            RoutingCandidateResult(
                candidate=candidate,
                rank=rank,
                status="selected" if rank == 1 else "standby",
                reason_code="routing.fixed_priority" if rank == 1 else "routing.candidate.standby",
            )
        )
    return RoutingSelectionResult(
        routing_mode="fixed",
        status="selected",
        selection_reason="routing.fixed_priority",
        selected_model_id="primary",
        selected_provider_id="provider-primary",
        candidate_results=tuple(results),
        eligible_candidate_count=3,
    )


def _ranks(selection: RoutingSelectionResult) -> list[str]:
    return [
        item.candidate.model_id
        for item in sorted(selection.candidate_results, key=lambda item: item.rank or 99)
    ]


def test_failed_primary_is_deferred_but_remains_last_chance(monkeypatch) -> None:
    now = [100.0]
    registry = RouteCooldownRegistry(lambda: now[0])
    monkeypatch.setattr(models, "route_cooldowns", registry)
    primary = ("tenant-a", "primary", "provider-primary")
    registry.failed(primary, request_id="first")

    selected, deferred, probe = models._apply_route_cooldowns(
        _selection(), tenant_id="tenant-a", request_id="next"
    )
    assert _ranks(selected) == ["backup-a", "backup-b", "primary"]
    assert selected.selected_model_id == "backup-a"
    assert deferred == ("primary",)
    assert probe is None
    other_tenant, _, _ = models._apply_route_cooldowns(
        _selection(), tenant_id="tenant-b", request_id="independent"
    )
    assert other_tenant.selected_model_id == "primary"


def test_one_recovery_probe_then_success_restores_priority(monkeypatch) -> None:
    now = [100.0]
    registry = RouteCooldownRegistry(lambda: now[0])
    monkeypatch.setattr(models, "route_cooldowns", registry)
    primary = ("tenant-a", "primary", "provider-primary")
    registry.failed(primary, request_id="first")
    now[0] = 131.0

    first, _, probe = models._apply_route_cooldowns(
        _selection(), tenant_id="tenant-a", request_id="probe"
    )
    parallel, _, parallel_probe = models._apply_route_cooldowns(
        _selection(), tenant_id="tenant-a", request_id="parallel"
    )
    assert first.selected_model_id == "primary"
    assert probe == primary
    assert parallel.selected_model_id == "backup-a"
    assert parallel_probe is None

    registry.succeeded(primary)
    restored, _, _ = models._apply_route_cooldowns(
        _selection(), tenant_id="tenant-a", request_id="restored"
    )
    assert restored.selected_model_id == "primary"


def test_failed_probe_extends_cooldown_and_inconclusive_does_not_restore() -> None:
    now = [100.0]
    registry = RouteCooldownRegistry(lambda: now[0])
    primary = ("tenant-a", "primary", "provider-primary")
    backup = ("tenant-a", "backup-a", "provider-backup-a")
    registry.failed(primary, request_id="first")
    now[0] = 131.0
    assert registry.order((primary, backup), "probe")[1] == primary
    registry.inconclusive(primary, "probe")
    assert registry.order((primary, backup), "next")[0][0] == backup
    now[0] = 162.0
    assert registry.order((primary, backup), "probe-2")[1] == primary
    registry.failed(primary, request_id="probe-2")
    now[0] = 220.0
    assert registry.order((primary, backup), "before-60s")[0][0] == backup
    now[0] = 223.0
    assert registry.order((primary, backup), "after-60s")[1] == primary


def test_late_failure_cannot_override_newer_success() -> None:
    now = [100.0]
    registry = RouteCooldownRegistry(lambda: now[0])
    primary = ("tenant-a", "primary", "provider-primary")
    backup = ("tenant-a", "backup-a", "provider-backup-a")
    registry.failed(primary, request_id="old", attempt_started=99.0)
    now[0] = 140.0
    registry.succeeded(primary, attempt_started=139.0)
    now[0] = 141.0
    registry.failed(primary, request_id="late", attempt_started=100.0)
    assert registry.order((primary, backup), "next")[0][0] == primary


def test_late_success_cannot_clear_newer_failure() -> None:
    now = [100.0]
    registry = RouteCooldownRegistry(lambda: now[0])
    primary = ("tenant-a", "primary", "provider-primary")
    backup = ("tenant-a", "backup-a", "provider-backup-a")
    registry.failed(primary, request_id="new", attempt_started=100.0)
    now[0] = 101.0
    registry.succeeded(primary, attempt_started=99.0)
    assert registry.order((primary, backup), "next")[0][0] == backup


def test_all_32_cooled_routes_remain_available_as_last_chances() -> None:
    registry = RouteCooldownRegistry(lambda: 100.0)
    routes = tuple(("tenant-a", f"model-{index}", f"provider-{index}") for index in range(32))
    for route in routes:
        registry.failed(route, request_id="previous")
    ordered, probe, deferred = registry.order(routes, "next")
    assert ordered == routes
    assert deferred == routes
    assert probe is None


def test_cache_affinity_does_not_restore_cooling_model(monkeypatch) -> None:
    monkeypatch.setattr(
        affinity_routing, "_binding_state_for_request", lambda *args, **kwargs: "warm"
    )
    bindings = tuple(
        {
            "model_endpoint_id": model_id,
            "endpoint_revision_digest": model_id,
            "cache_affinity_binding_id": model_id,
            "pending_lease_generation": 1,
            "provider_id": f"provider-{model_id}",
            "last_success_at": None,
        }
        for model_id in ("primary", "backup-a")
    )
    preferred = affinity_routing._choose_endpoint_preference(
        None, None,
        scope_row={"preferred_binding_id": "primary", "preferred_lease_generation": 1},
        binding_rows=bindings,
        selection=_selection(),
        candidate_rows={},
        endpoint_revisions={"primary": "primary", "backup-a": "backup-a"},
        ttl_seconds=60,
        as_of=datetime.now(UTC),
        deferred_model_ids=("primary",),
    )
    assert preferred == ("backup-a", "provider-backup-a", "warm")


def test_frozen_fallback_chain_keeps_deferred_primary_last() -> None:
    selection = _selection()
    primary = ("tenant-a", "primary", "provider-primary")
    registry = RouteCooldownRegistry(lambda: 100.0)
    registry.failed(primary, request_id="first")
    original = models.route_cooldowns
    models.route_cooldowns = registry
    try:
        reordered, _, _ = models._apply_route_cooldowns(
            selection, tenant_id="tenant-a", request_id="next"
        )
    finally:
        models.route_cooldowns = original
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fallback_policies (tenant_id TEXT, model_alias_id TEXT, "
        "status TEXT, fallback_policy_id TEXT, version INTEGER, condition_expr TEXT)"
    )
    connection.execute(
        "INSERT INTO fallback_policies VALUES (?, ?, ?, ?, ?, ?)",
        ("tenant-a", "alias-a", "enabled", "policy-a", 1, '["upstream_any_error"]'),
    )
    rows = {
        model_id: {
            "tenant_id": "tenant-a",
            "model_id": model_id,
            "model_alias": model_id,
            "upstream_model_name": model_id,
            "capabilities": '{"chat":true,"stream":true}',
            "provider_id": f"provider-{model_id}",
            "provider_code": model_id,
            "provider_type": "openai_compatible",
            "auth_type": "bearer",
            "provider_name": model_id,
            "base_url": "https://example.invalid/v1",
        }
        for model_id in ("primary", "backup-a", "backup-b")
    }
    initial = models._route_from_row(
        rows["backup-a"], requested_model="alias-a", environment="dev",
        environment_id="env-a", model_alias_id="alias-a", public_model_alias="alias-a",
        routing_reason="alias.fallback",
    )
    routes, *_ = models._load_frozen_runtime_fallback(
        connection, tenant_id="tenant-a", alias_policy={"model_alias_id": "alias-a"},
        selection=reordered, candidate_rows=rows, initial_route=initial,
    )
    assert [route.model_id for route in routes] == ["backup-b", "primary"]
    connection.close()
