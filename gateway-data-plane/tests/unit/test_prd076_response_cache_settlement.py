"""PRD-076 Response Cache atomic settlement against the real 0069 schema.

该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from metahub_gateway.output_safety.models import OutputSafetyTerminalState
from metahub_gateway.output_safety.service import prepare_output_safety_scan
from metahub_gateway.providers.base import (
    NormalizedResponse,
    NormalizedUsage,
    ProviderRequest,
)
from metahub_gateway.quota import enforce_quota_guard
from metahub_gateway.response_cache import (
    PreparedResponseCacheSettlement,
    commit_response_cache_fill,
    fail_response_cache_candidate,
    freeze_response_cache_reference,
    prepare_response_cache,
    read_response_cache_settlement,
    recover_absent_response_cache_settlement,
    settle_response_cache_candidate,
)
from metahub_gateway.response_cache.models import ResponseCacheCandidate
from metahub_gateway.routing.models import GatewayRoute
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    claim_gateway_request,
    record_request_log,
    safe_record_usage_ledger,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.output_safety import OutputScanDecision
from metahub_shared.response_cache import VersionedResponseCacheSecret

# 该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。
POLICY_SNAPSHOT_ID = "psnap_prd076_settlement_v1"
POLICY_SNAPSHOT_VERSION = 1
POLICY_SNAPSHOT_STATUS = "current"
INPUT_TOKENS = 12
OUTPUT_TOKENS = 8
TOTAL_TOKENS = INPUT_TOKENS + OUTPUT_TOKENS
REFERENCE_COST = Decimal("0.002800")


@dataclass(frozen=True, slots=True)
class SettlementScenario:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 SettlementScenario 对应步骤。"""
    config: SimpleNamespace
    route: GatewayRoute
    payload: dict[str, object]
    cached_payload: dict[str, object]
    tenant_id: str
    actor_user_id: str
    virtual_key_id: str
    environment_id: str
    environment_code: str
    cache_policy_id: str


@pytest.fixture(scope="module")
def migrated_database_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create one closed 0069 seed database and clone it per test.

    该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 migrated_database_template 对应步骤。"""

    root = tmp_path_factory.mktemp("prd076-settlement-template")
    database_path = root / "template.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    result = apply_migrations(database_url)
    # The shared fixture runs the complete current SQLite migration chain.
    assert result.applied[-1] == "0164"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0] == "0164"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    return database_path


def _scenario(
    template: Path,
    tmp_path: Path,
    *,
    charge_cache_hits: bool,
) -> SettlementScenario:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _scenario 对应步骤。"""
    database_path = tmp_path / "settlement.sqlite3"
    shutil.copy2(template, database_path)
    database_url = f"sqlite:///{database_path.as_posix()}"
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT tenant.tenant_id,tenant.slug,user.user_id,
                   key.virtual_key_id,environment.environment_id,
                   environment.code AS environment_code,
                   model.model_id,model.model_alias,
                   COALESCE(model.provider_model_id,model.upstream_model_name)
                     AS upstream_model_name,
                   model.capabilities,provider.provider_id,provider.provider_code,
                   provider.provider_type,provider.auth_type,provider.name,
                   provider.base_url
            FROM tenants tenant
            JOIN users user
              ON user.tenant_id=tenant.tenant_id AND user.role='admin'
            JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
            JOIN environments environment
              ON environment.tenant_id=tenant.tenant_id AND environment.code='dev'
            JOIN models model
              ON model.tenant_id=tenant.tenant_id AND model.status='active'
            JOIN providers provider
              ON provider.tenant_id=model.tenant_id
             AND provider.provider_id=model.provider_id
            WHERE tenant.status='active' AND user.status='active'
            ORDER BY tenant.slug='default' DESC,model.model_id,key.virtual_key_id
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        alias_id = "mal_prd076_" + uuid4().hex
        alias = "prd076-settlement-" + uuid4().hex[:12]
        policy_id = "rcp_" + uuid4().hex
        now = "2026-08-10T00:00:00Z"
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,default_model_id,
              fallback_model_id,status,description,created_by,disabled_at,
              disabled_by,created_at,updated_at,environment_id
            ) VALUES(?,?,?,'dev',?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
            """,
            (
                alias_id,
                row["tenant_id"],
                alias,
                row["model_id"],
                row["user_id"],
                now,
                now,
                row["environment_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO response_cache_policies(
              cache_policy_id,tenant_id,model_alias_id,status,data_class,
              ttl_seconds,charge_cache_hits,tags_json,
              single_data_class_confirmed,data_class_confirmed_by,
              data_class_confirmed_version,version,invalidation_generation,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,'enabled','fixture.public',900,?,'["settlement"]',
                     1,?,1,1,1,?,?,?,?)
            """,
            (
                policy_id,
                row["tenant_id"],
                alias_id,
                1 if charge_cache_hits else 0,
                row["user_id"],
                row["user_id"],
                row["user_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k='100.000000',
                output_price_per_1k='200.000000',currency='CNY',
                price_schema_version='prd076-settlement-price-v1'
            WHERE tenant_id=? AND model_id=?
            """,
            (row["tenant_id"], row["model_id"]),
        )
        # Exercise the legacy model-catalog fallback explicitly; current
        # workspace rates are authoritative when enabled.
        connection.execute(
            """
            UPDATE workspace_provider_cost_rate_heads
            SET adoption_state='disabled',head_revision=head_revision+1
            WHERE workspace_provider_cost_rate_id IN(
              SELECT workspace_provider_cost_rate_id FROM workspace_provider_cost_rates
              WHERE tenant_id=? AND model_id=?
            )
            """,
            (row["tenant_id"], row["model_id"]),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM model_price_plans WHERE tenant_id=? AND model_id=?",
            (row["tenant_id"], row["model_id"]),
        ).fetchone()[0] == 0
        connection.commit()
        metadata = {name: row[name] for name in row.keys()}
        metadata["model_alias_id"] = alias_id
        metadata["public_alias"] = alias
        metadata["cache_policy_id"] = policy_id
    finally:
        connection.close()

    raw_capabilities = json.loads(str(metadata["capabilities"] or "{}"))
    capabilities = (
        raw_capabilities if isinstance(raw_capabilities, dict) else {"chat": True}
    )
    route = GatewayRoute(
        tenant_id=str(metadata["tenant_id"]),
        model_id=str(metadata["model_id"]),
        model_alias=str(metadata["model_alias"]),
        upstream_model_name=str(metadata["upstream_model_name"]),
        capabilities=capabilities,
        provider_id=str(metadata["provider_id"]),
        provider_code=str(metadata["provider_code"]),
        provider_type=str(metadata["provider_type"]),
        provider_auth_type=str(metadata["auth_type"]),
        provider_name=str(metadata["name"]),
        provider_base_url=str(metadata["base_url"]),
        requested_model=str(metadata["public_alias"]),
        environment="dev",
        environment_id=str(metadata["environment_id"]),
        model_alias_id=str(metadata["model_alias_id"]),
        public_model_alias=str(metadata["public_alias"]),
        routing_reason="alias.default",
    )
    config = SimpleNamespace(
        database_url=database_url,
        environment="dev",
        default_tenant_slug=str(metadata["slug"]),
        provider_base_urls={route.provider_code: route.provider_base_url},
        provider_api_keys={route.provider_code: "x" * 48},
        provider_health_stale_seconds=180,
        response_cache_enabled=True,
        response_cache_hmac_key=VersionedResponseCacheSecret(1, "h" * 48),
        response_cache_encryption_key=VersionedResponseCacheSecret(1, "e" * 48),
        response_cache_fill_lease_seconds=90,
        response_cache_max_ready_entries_per_tenant=100,
        response_cache_max_plaintext_bytes=1_048_576,
        response_cache_failure_spool_root=str(tmp_path / "failure-spool"),
        finops_usage_spool_root=str(tmp_path / "usage-spool"),
        auth_token_secret="a" * 48,
    )
    payload: dict[str, object] = {
        "model": route.public_model_alias or route.requested_model,
        "messages": [],
        "stream": False,
    }
    cached_payload: dict[str, object] = {
        "id": "fixture-cache-response",
        "object": "chat.completion",
        "choices": [],
    }
    scenario = SettlementScenario(
        config=config,
        route=route,
        payload=payload,
        cached_payload=cached_payload,
        tenant_id=route.tenant_id,
        actor_user_id=str(metadata["user_id"]),
        virtual_key_id=str(metadata["virtual_key_id"]),
        environment_id=str(metadata["environment_id"]),
        environment_code=str(metadata["environment_code"]),
        cache_policy_id=str(metadata["cache_policy_id"]),
    )
    _store_source_entry(scenario)
    return scenario


def _install_prd079_quota_plan(scenario: SettlementScenario) -> None:
    """给缓存测试租户安装一个仅含 free 余额的 PRD-079 套餐。"""

    plan_id = f"pln_{uuid4().hex}"
    now = "2026-08-10T00:00:00Z"
    connection = connect_database(scenario.config.database_url)
    try:
        connection.execute(
            """
            INSERT INTO plans(
              plan_id,tenant_id,display_name,base_tier,status,trial_status,
              trial_started_at,trial_ends_at,trial_cycle,state_revision,
              quota_generation,created_at,updated_at
            ) VALUES(?,?,?,'free','active','not_started',NULL,NULL,0,1,1,?,?)
            """,
            (plan_id, scenario.tenant_id, "PRD-079 Cache Plan", now, now),
        )
        for quota_type in ("trial", "free", "gift", "paid"):
            granted = 10_000 if quota_type == "free" else 0
            connection.execute(
                """
                INSERT INTO quota_buckets(
                  quota_bucket_id,tenant_id,plan_id,quota_type,granted_micros,
                  consumed_micros,reserved_micros,status,expires_at,state_revision,
                  created_at,updated_at
                ) VALUES(?,?,?,?,?,0,0,?,NULL,1,?,?)
                """,
                (
                    f"qbk_{uuid4().hex}",
                    scenario.tenant_id,
                    plan_id,
                    quota_type,
                    granted,
                    "active" if granted else "exhausted",
                    now,
                    now,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _make_route_unpriced(scenario: SettlementScenario) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _make_route_unpriced 对应步骤。"""
    connection = connect_database(scenario.config.database_url)
    try:
        connection.execute(
            """
            UPDATE models SET price_schema_version=NULL
            WHERE tenant_id=? AND model_id=?
            """,
            (scenario.tenant_id, scenario.route.model_id),
        )
        connection.commit()
    finally:
        connection.close()


def _context(scenario: SettlementScenario, suffix: str) -> RequestLogContext:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _context 对应步骤。"""
    return RequestLogContext(
        tenant_id=scenario.tenant_id,
        request_id=f"req-prd076-{suffix}",
        trace_id=f"trc-prd076-{suffix}",
        virtual_key_id=scenario.virtual_key_id,
        environment_id=scenario.environment_id,
        environment_code=scenario.environment_code,
        provider_id=scenario.route.provider_id,
        model_id=scenario.route.model_id,
        endpoint="chat/completions",
        request_model=scenario.route.requested_model,
        resolved_model=scenario.route.model_alias,
        provider_code=scenario.route.provider_code,
        snapshot_version=None,
        policy_snapshot_status="disabled",
    )


def _prepare(
    scenario: SettlementScenario,
    context: RequestLogContext,
):
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _prepare 对应步骤。"""
    return prepare_response_cache(
        scenario.config,
        context=context,
        route=scenario.route,
        payload=scenario.payload,
        request_kind="chat",
        public_virtual_key=True,
        internal_invocation=False,
        policy_decision="allow",
        experiment_disposition="not_in_scope",
        policy_snapshot_id=POLICY_SNAPSHOT_ID,
        policy_snapshot_version=POLICY_SNAPSHOT_VERSION,
        policy_snapshot_status=POLICY_SNAPSHOT_STATUS,
    )


def _store_source_entry(scenario: SettlementScenario) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _store_source_entry 对应步骤。"""
    context = _context(scenario, "source")
    claim_gateway_request(scenario.config, context)
    prepared = _prepare(scenario, context)
    assert prepared.status == "miss_owner" and prepared.fill_lease is not None
    record_request_log(
        scenario.config,
        context,
        status="success",
        error_code=None,
        latency_ms=9,
    )
    usage = NormalizedUsage(
        input_tokens=INPUT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
        total_tokens=TOTAL_TOKENS,
        raw_usage={},
    )
    committed = safe_record_usage_ledger(
        scenario.config,
        context,
        usage.as_ledger_payload(),
        require_durable=True,
    )
    assert committed is not None
    provider_request = ProviderRequest(
        provider_id=scenario.route.provider_id,
        provider_code=scenario.route.provider_code,
        provider_type=scenario.route.provider_type,
        adapter_name="prd076-settlement-fixture",
        endpoint="chat/completions",
        url=scenario.route.provider_base_url,
        headers={},
        payload={},
        tenant_id=scenario.tenant_id,
        model_id=scenario.route.model_id,
        provider_key_source=(
            None if scenario.route.provider_auth_type == "none" else "environment"
        ),
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    response = NormalizedResponse(
        payload=scenario.cached_payload,
        usage=usage,
        status_code=200,
    )
    result = commit_response_cache_fill(
        scenario.config,
        lease=prepared.fill_lease,
        context=context,
        route=scenario.route,
        provider_request=provider_request,
        response=response,
    )
    assert result.status == "stored"


def _freeze(
    scenario: SettlementScenario,
    suffix: str,
) -> tuple[RequestLogContext, ResponseCacheCandidate, PreparedResponseCacheSettlement]:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _freeze 对应步骤。"""
    context = _context(scenario, suffix)
    claim_gateway_request(scenario.config, context)
    lookup = _prepare(scenario, context)
    assert lookup.status == "hit_candidate" and lookup.candidate is not None
    prepared = freeze_response_cache_reference(
        scenario.config,
        context=context,
        route=scenario.route,
        candidate=lookup.candidate,
        policy_snapshot_id=POLICY_SNAPSHOT_ID,
        policy_snapshot_version=POLICY_SNAPSHOT_VERSION,
        policy_snapshot_status=POLICY_SNAPSHOT_STATUS,
    )
    assert prepared is not None
    return context, lookup.candidate, prepared


def _readback(
    scenario: SettlementScenario,
    context: RequestLogContext,
    prepared: PreparedResponseCacheSettlement,
    *,
    policy_version: int = 1,
):
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _readback 对应步骤。"""
    return read_response_cache_settlement(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        cache_policy_id=scenario.cache_policy_id,
        cache_policy_version=policy_version,
        ids=prepared.ids,
    )


def _insert_budget_reservation(
    scenario: SettlementScenario,
    context: RequestLogContext,
    amount: Decimal,
) -> RequestLogContext:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _insert_budget_reservation 对应步骤。"""
    connection = connect_database(scenario.config.database_url)
    try:
        rule_id = "br_prd076_" + uuid4().hex
        now = "2026-08-10T00:00:00Z"
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
              daily_budget_cny,warning_threshold_percent,
              exhausted_threshold_percent,exhausted_action,status,
              created_at,updated_at
            ) VALUES(?,?,?,'tenant',?,'PRD076 settlement fixture',100,80,100,
                     'block','active',?,?)
            """,
            (
                rule_id,
                scenario.tenant_id,
                scenario.environment_id,
                scenario.tenant_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO budget_reservations(
              budget_reservation_id,tenant_id,environment_id,budget_rule_id,
              scope_type,scope_id,budget_date,request_id,trace_id,
              reserved_cost_cny,outstanding_cost_cny,created_at
            ) VALUES(?,?,?,?,'tenant',?,'2026-08-10',?,?,?,NULL,?)
            """,
            (
                "bres_prd076_" + uuid4().hex,
                scenario.tenant_id,
                scenario.environment_id,
                rule_id,
                scenario.tenant_id,
                context.request_id,
                context.trace_id,
                format(amount, "f"),
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return replace(context, budget_reservation_outstanding_cny="0")


def _insert_output_block_rule(scenario: SettlementScenario) -> str:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _insert_output_block_rule 对应步骤。"""
    rule_id = "dlp_prd076_" + uuid4().hex
    pattern = "fixture-output-block-marker"
    digest = hashlib.sha256(f"keyword:{pattern}".encode("ascii")).hexdigest()
    connection = connect_database(scenario.config.database_url)
    try:
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
              action,status,priority,scan_scope,created_at,updated_at
            ) VALUES(?,?,?,'keyword',?,?,'block','active',10,'output',?,?)
            """,
            (
                rule_id,
                scenario.tenant_id,
                "PRD076 output fixture",
                pattern,
                f"sha256:{digest}",
                "2026-08-10T00:00:00Z",
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return rule_id


def _blocked_terminal(rule_id: str) -> OutputSafetyTerminalState:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 _blocked_terminal 对应步骤。"""
    decision = OutputScanDecision(
        decision="blocked",
        action="block",
        dlp_rule_id=rule_id,
        rule_type="keyword",
        match_count=1,
        checkpoint_count=1,
        scanned_token_count=5,
    )
    return OutputSafetyTerminalState.from_response_cache_decision(
        decision,
        observed_output_token_count=5,
    )


def test_freeze_generates_all_ids_and_trusted_reference_estimate(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_freeze_generates_all_ids_and_trusted_reference_estimate 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=False)
    context, _candidate, prepared = _freeze(scenario, "freeze")

    prefixes = {
        prepared.ids.cache_hit_id: "rch_",
        prepared.ids.usage_ledger_id: "usg_",
        prepared.ids.finops_ledger_entry_id: "fle_",
        prepared.ids.finops_event_id: "flevt_",
        prepared.ids.reference_price_snapshot_id: "prs_",
        prepared.ids.billed_price_snapshot_id: "prs_",
        prepared.ids.response_cache_event_id: "rcev_",
    }
    for value, prefix in prefixes.items():
        assert value.startswith(prefix) and len(value) == len(prefix) + 32
    assert prepared.ids.reference_price_snapshot_id != prepared.ids.billed_price_snapshot_id
    assert prepared.reference_estimate.cost_cny == REFERENCE_COST
    assert prepared.trusted_estimate.input_tokens == INPUT_TOKENS
    assert prepared.trusted_estimate.output_tokens == OUTPUT_TOKENS
    assert prepared.trusted_estimate.cost_cny == Decimal("0")
    assert prepared.trusted_estimate.price_available is True

    connection = connect_database(scenario.config.database_url)
    try:
        snapshot = connection.execute(
            """
            SELECT request_id,source,price_schema_version
            FROM price_snapshots
            WHERE tenant_id=? AND price_snapshot_id=?
            """,
            (
                scenario.tenant_id,
                prepared.ids.reference_price_snapshot_id,
            ),
        ).fetchone()
        assert snapshot is not None
        assert tuple(snapshot) == (
            context.request_id,
            "response_cache_reference",
            "prd076-settlement-price-v1",
        )
    finally:
        connection.close()


def test_charge_off_hit_commits_complete_atomic_graph_and_budget_zero(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_charge_off_hit_commits_complete_atomic_graph_and_budget_zero 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=False)
    context, _candidate, prepared = _freeze(scenario, "charge-off")
    context = _insert_budget_reservation(scenario, context, Decimal("0"))
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    assert scan.scan_status == "not_configured"

    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=None,
        latency_ms=11,
    )
    assert result.status == "hit"
    assert result.payload == scenario.cached_payload
    assert result.settlement_owned is True
    assert result.ownership_unknown is False
    assert _readback(scenario, context, prepared).status == "complete"

    connection = connect_database(scenario.config.database_url)
    try:
        hit = connection.execute(
            "SELECT * FROM response_cache_hits WHERE response_cache_hit_id=?",
            (prepared.ids.cache_hit_id,),
        ).fetchone()
        usage = connection.execute(
            "SELECT * FROM usage_ledger WHERE usage_ledger_id=?",
            (prepared.ids.usage_ledger_id,),
        ).fetchone()
        charge = connection.execute(
            "SELECT * FROM finops_ledger_entries WHERE finops_ledger_entry_id=?",
            (prepared.ids.finops_ledger_entry_id,),
        ).fetchone()
        fact = connection.execute(
            "SELECT * FROM response_cache_request_facts WHERE tenant_id=? AND request_id=?",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        event = connection.execute(
            "SELECT * FROM response_cache_events WHERE response_cache_event_id=?",
            (prepared.ids.response_cache_event_id,),
        ).fetchone()
        outbox = connection.execute(
            """SELECT * FROM response_cache_event_outbox
               WHERE tenant_id=? AND response_cache_event_id=?""",
            (scenario.tenant_id, prepared.ids.response_cache_event_id),
        ).fetchone()
        budget = connection.execute(
            """SELECT outstanding_cost_cny FROM budget_reservations
               WHERE tenant_id=? AND request_id=?""",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        assert all(item is not None for item in (hit, usage, charge, fact, event, outbox, budget))
        assert hit["billed_cost_status"] == "known"
        assert hit["billed_cost_cny_micros"] == 0
        assert hit["saved_cost_cny_micros"] == 2800
        assert usage["usage_source"] == "response_cache"
        assert usage["price_snapshot_id"] == prepared.ids.billed_price_snapshot_id
        assert Decimal(str(usage["cost_cny"])) == 0
        assert charge["event_id"] == prepared.ids.finops_event_id
        assert charge["price_evidence_status"] == "priced"
        assert charge["price_snapshot_source"] == "response_cache_no_charge"
        assert charge["price_schema_version"] == "response-cache-no-charge-v1"
        assert charge["cny_normalization_status"] == "normalized"
        assert charge["normalized_cny_micros"] == 0
        assert fact["status"] == "hit" and fact["hit"] == 1
        assert event["event_type"] == "cache.hit"
        assert outbox["status"] == "pending"
        assert Decimal(str(budget["outstanding_cost_cny"])) == 0
        billed = connection.execute(
            "SELECT source,price_schema_version FROM price_snapshots WHERE price_snapshot_id=?",
            (prepared.ids.billed_price_snapshot_id,),
        ).fetchone()
        assert tuple(billed) == (
            "response_cache_no_charge",
            "response-cache-no-charge-v1",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_charge_off_unpriced_reference_preserves_explicit_no_charge_evidence(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_charge_off_unpriced_reference_preserves_explicit_no_charge_evidence 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=False)
    _make_route_unpriced(scenario)
    context, _candidate, prepared = _freeze(scenario, "charge-off-unpriced")
    assert prepared.reference_estimate.price_schema_version == "unpriced-v1"
    assert prepared.trusted_estimate.price_available is True
    assert prepared.trusted_estimate.cost_cny == Decimal("0")
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=None,
        latency_ms=9,
    )
    assert result.status == "hit"

    connection = connect_database(scenario.config.database_url)
    try:
        snapshots = connection.execute(
            """
            SELECT price_snapshot_id,source,price_schema_version
            FROM price_snapshots
            WHERE tenant_id=? AND request_id=?
            ORDER BY price_snapshot_id
            """,
            (scenario.tenant_id, context.request_id),
        ).fetchall()
        assert {
            (
                row["price_snapshot_id"],
                row["source"],
                row["price_schema_version"],
            )
            for row in snapshots
        } == {
            (
                prepared.ids.reference_price_snapshot_id,
                "response_cache_reference",
                "unpriced-v1",
            ),
            (
                prepared.ids.billed_price_snapshot_id,
                "response_cache_no_charge",
                "response-cache-no-charge-v1",
            ),
        }
        charge = connection.execute(
            """
            SELECT price_evidence_status,price_snapshot_source,
                   price_schema_version,cny_normalization_status,
                   normalized_cny_micros
            FROM finops_ledger_entries WHERE finops_ledger_entry_id=?
            """,
            (prepared.ids.finops_ledger_entry_id,),
        ).fetchone()
        hit = connection.execute(
            """
            SELECT current_reference_cost_status,saved_cost_status,
                   billed_cost_status,billed_cost_cny_micros
            FROM response_cache_hits WHERE response_cache_hit_id=?
            """,
            (prepared.ids.cache_hit_id,),
        ).fetchone()
        assert tuple(charge) == (
            "priced",
            "response_cache_no_charge",
            "response-cache-no-charge-v1",
            "normalized",
            0,
        )
        assert tuple(hit) == ("unavailable", "unavailable", "known", 0)
    finally:
        connection.close()


def test_charge_on_reuses_frozen_reference_snapshot(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_charge_on_reuses_frozen_reference_snapshot 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=True)
    context, _candidate, prepared = _freeze(scenario, "charge-on")
    assert prepared.ids.billed_price_snapshot_id == prepared.ids.reference_price_snapshot_id
    assert prepared.trusted_estimate.cost_cny == REFERENCE_COST
    assert prepared.trusted_estimate.price_available is True
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=None,
        latency_ms=7,
    )
    assert result.status == "hit"

    connection = connect_database(scenario.config.database_url)
    try:
        rows = connection.execute(
            """SELECT price_snapshot_id,source FROM price_snapshots
               WHERE tenant_id=? AND request_id=?""",
            (scenario.tenant_id, context.request_id),
        ).fetchall()
        assert [(row["price_snapshot_id"], row["source"]) for row in rows] == [
            (prepared.ids.reference_price_snapshot_id, "response_cache_reference")
        ]
        usage = connection.execute(
            "SELECT price_snapshot_id,cost_cny FROM usage_ledger WHERE usage_ledger_id=?",
            (prepared.ids.usage_ledger_id,),
        ).fetchone()
        hit = connection.execute(
            """SELECT billed_cost_cny_micros,saved_cost_cny_micros
               FROM response_cache_hits WHERE response_cache_hit_id=?""",
            (prepared.ids.cache_hit_id,),
        ).fetchone()
        charge = connection.execute(
            """
            SELECT price_evidence_status,price_snapshot_source,
                   price_schema_version,cny_normalization_status,
                   normalized_cny_micros
            FROM finops_ledger_entries WHERE finops_ledger_entry_id=?
            """,
            (prepared.ids.finops_ledger_entry_id,),
        ).fetchone()
        assert usage["price_snapshot_id"] == prepared.ids.reference_price_snapshot_id
        assert Decimal(str(usage["cost_cny"])) == REFERENCE_COST
        assert tuple(hit) == (2800, 0)
        assert tuple(charge) == (
            "priced",
            "response_cache_reference",
            "prd076-settlement-price-v1",
            "normalized",
            2800,
        )
    finally:
        connection.close()


def test_charge_on_unpriced_reference_preserves_unavailable_finops_evidence(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_charge_on_unpriced_reference_preserves_unavailable_finops_evidence 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=True)
    _make_route_unpriced(scenario)

    context, _candidate, prepared = _freeze(scenario, "charge-on-unpriced")
    assert prepared.reference_estimate.price_schema_version == "unpriced-v1"
    assert prepared.trusted_estimate.cost_cny == Decimal("0")
    assert prepared.trusted_estimate.price_available is False
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=None,
        latency_ms=8,
    )
    assert result.status == "hit"

    connection = connect_database(scenario.config.database_url)
    try:
        charge = connection.execute(
            """SELECT price_evidence_status,price_snapshot_source,
                      price_schema_version,cny_normalization_status,
                      normalized_cny_micros,price_snapshot_id,amount_micros
               FROM finops_ledger_entries WHERE finops_ledger_entry_id=?""",
            (prepared.ids.finops_ledger_entry_id,),
        ).fetchone()
        hit = connection.execute(
            """SELECT current_reference_cost_status,saved_cost_status,
                      billed_cost_status
               FROM response_cache_hits WHERE response_cache_hit_id=?""",
            (prepared.ids.cache_hit_id,),
        ).fetchone()
        usage = connection.execute(
            """SELECT cost_cny,price_schema_version,price_snapshot_id
               FROM usage_ledger WHERE usage_ledger_id=?""",
            (prepared.ids.usage_ledger_id,),
        ).fetchone()
        assert tuple(charge) == (
            "unpriced",
            "response_cache_reference",
            "unpriced-v1",
            "unavailable",
            None,
            prepared.ids.reference_price_snapshot_id,
            0,
        )
        assert tuple(hit) == ("unavailable", "unavailable", "unavailable")
        assert Decimal(str(usage["cost_cny"])) == 0
        assert usage["price_schema_version"] == "unpriced-v1"
        assert usage["price_snapshot_id"] == prepared.ids.reference_price_snapshot_id
    finally:
        connection.close()


def test_stale_candidate_does_not_finalize_pending_output_safety(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_stale_candidate_does_not_finalize_pending_output_safety 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=False)
    context, candidate, prepared = _freeze(scenario, "stale")
    _insert_output_block_rule(scenario)
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    assert scan.scan_status == "pending"
    connection = connect_database(scenario.config.database_url)
    try:
        connection.execute(
            """
            UPDATE response_cache_key_generations
            SET generation=generation+1,updated_at=datetime('now')
            WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
            """,
            (
                scenario.tenant_id,
                candidate.owner.hmac_key_version,
                candidate.owner.cache_identity_digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=_blocked_terminal(scan.snapshot.rules[0].dlp_rule_id),
        latency_ms=5,
    )
    assert result.status == "stale"
    assert result.settlement_owned is False
    connection = connect_database(scenario.config.database_url)
    try:
        row = connection.execute(
            "SELECT scan_status,decision FROM output_safety_scans WHERE output_safety_scan_id=?",
            (scan.output_safety_scan_id,),
        ).fetchone()
        fact = connection.execute(
            "SELECT status FROM response_cache_request_facts WHERE tenant_id=? AND request_id=?",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        assert tuple(row) == ("pending", "none")
        assert fact["status"] == "hit_candidate"
    finally:
        connection.close()


def test_blocked_candidate_has_no_usage_charge_and_fences_entry(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_blocked_candidate_has_no_usage_charge_and_fences_entry 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=False)
    context, candidate, prepared = _freeze(scenario, "blocked")
    context = _insert_budget_reservation(scenario, context, Decimal("0"))
    rule_id = _insert_output_block_rule(scenario)
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    terminal = _blocked_terminal(rule_id)
    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=terminal,
        latency_ms=6,
    )
    assert result.status == "hit_blocked"
    assert result.error_code == "content_policy_violation"
    assert result.settlement_owned is True
    assert _readback(scenario, context, prepared).outcome == "hit_blocked"

    connection = connect_database(scenario.config.database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=? AND request_id=?",
            (scenario.tenant_id, context.request_id),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM finops_ledger_entries WHERE tenant_id=? AND source_request_id=?",
            (scenario.tenant_id, context.request_id),
        ).fetchone()[0] == 0
        assert connection.execute(
            """SELECT COUNT(*) FROM response_cache_entries
               WHERE tenant_id=? AND response_cache_entry_id=?""",
            (scenario.tenant_id, candidate.owner.entry_id),
        ).fetchone()[0] == 0
        fact = connection.execute(
            """SELECT status,failure_code FROM response_cache_request_facts
               WHERE tenant_id=? AND request_id=?""",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        invalidation = connection.execute(
            """
            SELECT reason_code FROM response_cache_invalidations
            WHERE tenant_id=? AND operation_request_id=? AND operation_trace_id=?
            """,
            (scenario.tenant_id, context.request_id, context.trace_id),
        ).fetchone()
        output = connection.execute(
            """
            SELECT scan_status,decision,billing_disposition
            FROM output_safety_scans WHERE output_safety_scan_id=?
            """,
            (scan.output_safety_scan_id,),
        ).fetchone()
        budget = connection.execute(
            """SELECT outstanding_cost_cny FROM budget_reservations
               WHERE tenant_id=? AND request_id=?""",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        assert tuple(fact) == ("hit_blocked", "content_policy_violation")
        assert invalidation["reason_code"] == "system_output_blocked"
        assert tuple(output) == (
            "completed",
            "blocked",
            "not_billable_response_cache_candidate",
        )
        assert Decimal(str(budget["outstanding_cost_cny"])) == 0
    finally:
        connection.close()


def test_readback_complete_absent_partial_and_unreadable(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_readback_complete_absent_partial_and_unreadable 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=True)
    complete_context, _candidate, complete_prepared = _freeze(scenario, "read-complete")
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=complete_context.tenant_id,
        request_id=complete_context.request_id,
        trace_id=complete_context.trace_id,
        scan_mode="non_stream_full",
    )
    settled = settle_response_cache_candidate(
        scenario.config,
        context=complete_context,
        route=scenario.route,
        prepared=complete_prepared,
        output_safety_scan=scan,
        output_safety_terminal=None,
        latency_ms=3,
    )
    assert settled.status == "hit"
    assert _readback(scenario, complete_context, complete_prepared).status == "complete"
    assert _readback(
        scenario,
        complete_context,
        complete_prepared,
        policy_version=2,
    ).status == "partial"

    absent_context, _candidate, absent_prepared = _freeze(scenario, "read-absent")
    assert _readback(scenario, absent_context, absent_prepared).status == "absent"
    unreadable_config = SimpleNamespace(
        **{
            **vars(scenario.config),
            "database_url": f"sqlite:///{(tmp_path / 'empty.sqlite3').as_posix()}",
        }
    )
    unreadable = read_response_cache_settlement(
        unreadable_config,
        tenant_id=absent_context.tenant_id,
        request_id=absent_context.request_id,
        trace_id=absent_context.trace_id,
        cache_policy_id=scenario.cache_policy_id,
        cache_policy_version=1,
        ids=absent_prepared.ids,
    )
    assert unreadable.status == "unreadable"


def test_absent_recovery_closes_failed_terminal_once(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_absent_recovery_closes_failed_terminal_once 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=True)
    context, _candidate, prepared = _freeze(scenario, "recover")
    assert _readback(scenario, context, prepared).status == "absent"
    recovered = recover_absent_response_cache_settlement(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        cache_policy_id=scenario.cache_policy_id,
        cache_policy_version=1,
        ids=prepared.ids,
        error_code="gateway.response_cache_settlement_unavailable",
    )
    assert recovered.status == "recovered"
    replay = recover_absent_response_cache_settlement(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        cache_policy_id=scenario.cache_policy_id,
        cache_policy_version=1,
        ids=prepared.ids,
        error_code="gateway.response_cache_settlement_unavailable",
    )
    assert replay.status == "already_complete"
    readback = _readback(scenario, context, prepared)
    assert readback.status == "complete"
    assert readback.outcome == "settlement_failed"


def test_budget_failure_before_scan_closes_request_and_reservation(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 test_budget_failure_before_scan_closes_request_and_reservation 对应步骤。"""
    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=True)
    context, _candidate, prepared = _freeze(scenario, "budget-fail")
    context = _insert_budget_reservation(scenario, context, REFERENCE_COST)
    result = fail_response_cache_candidate(
        scenario.config,
        context=context,
        prepared=prepared,
        output_safety_scan=None,
        output_safety_terminal=None,
        latency_ms=4,
        error_code="gateway.budget_unavailable",
    )
    assert result.status == "unavailable"
    assert result.error_code == "gateway.budget_unavailable"
    assert result.settlement_owned is True
    assert result.ownership_unknown is False

    connection = connect_database(scenario.config.database_url)
    try:
        request = connection.execute(
            "SELECT status,error_code FROM request_logs WHERE tenant_id=? AND request_id=?",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        fact = connection.execute(
            """SELECT status,failure_code FROM response_cache_request_facts
               WHERE tenant_id=? AND request_id=?""",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        budget = connection.execute(
            """SELECT outstanding_cost_cny FROM budget_reservations
               WHERE tenant_id=? AND request_id=?""",
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        assert tuple(request) == ("failed", "gateway.budget_unavailable")
        assert tuple(fact) == ("settlement_failed", "gateway.budget_unavailable")
        assert Decimal(str(budget["outstanding_cost_cny"])) == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM output_safety_scans WHERE tenant_id=? AND request_id=?",
            (scenario.tenant_id, context.request_id),
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("charge_cache_hits", "actual_micros", "allocation_class", "consumed_micros"),
    [
        (False, 0, "unallocated", 0),
        (True, 2_800, "free", 2_800),
    ],
)
def test_prd079_cache_hit_settles_conservative_quota_reservation(
    migrated_database_template: Path,
    tmp_path: Path,
    charge_cache_hits: bool,
    actual_micros: int,
    allocation_class: str,
    consumed_micros: int,
) -> None:
    """缓存命中在同一事务按正式 charge 结算额度，零费用释放全部预留。"""

    scenario = _scenario(
        migrated_database_template,
        tmp_path,
        charge_cache_hits=charge_cache_hits,
    )
    _install_prd079_quota_plan(scenario)
    context, _candidate, prepared = _freeze(scenario, "prd079-quota-hit")
    guard = enforce_quota_guard(
        scenario.config,
        tenant_id=scenario.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        route=scenario.route,
        payload=scenario.payload,
        request_kind="chat",
        trusted_cost_cny=REFERENCE_COST,
    )
    assert guard.reservation_id is not None
    context = replace(context, quota_reservation_outstanding_cny="0")
    scan = prepare_output_safety_scan(
        scenario.config,
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        scan_mode="non_stream_full",
    )
    result = settle_response_cache_candidate(
        scenario.config,
        context=context,
        route=scenario.route,
        prepared=prepared,
        output_safety_scan=scan,
        output_safety_terminal=None,
        latency_ms=2,
    )
    assert result.status == "hit"

    connection = connect_database(scenario.config.database_url)
    try:
        reservation = connection.execute(
            """
            SELECT status,actual_micros,outstanding_micros
            FROM quota_reservations WHERE tenant_id=? AND request_id=?
            """,
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT amount_micros,allocation_class,allocation_count,status
            FROM quota_usage_settlements WHERE tenant_id=? AND request_id=?
            """,
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        free_bucket = connection.execute(
            """
            SELECT consumed_micros,reserved_micros FROM quota_buckets
            WHERE tenant_id=? AND quota_type='free'
            """,
            (scenario.tenant_id,),
        ).fetchone()
        assert tuple(reservation) == ("settled", actual_micros, 0)
        assert tuple(settlement) == (
            actual_micros,
            allocation_class,
            0 if actual_micros == 0 else 1,
            "settled",
        )
        assert tuple(free_bucket) == (consumed_micros, 0)
    finally:
        connection.close()


def test_prd079_cache_failure_releases_quota_in_terminal_transaction(
    migrated_database_template: Path,
    tmp_path: Path,
) -> None:
    """缓存失败终态与额度释放一起提交，不能留下并发占用。"""

    scenario = _scenario(migrated_database_template, tmp_path, charge_cache_hits=True)
    _install_prd079_quota_plan(scenario)
    context, _candidate, prepared = _freeze(scenario, "prd079-quota-failure")
    guard = enforce_quota_guard(
        scenario.config,
        tenant_id=scenario.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        route=scenario.route,
        payload=scenario.payload,
        request_kind="chat",
        trusted_cost_cny=REFERENCE_COST,
    )
    assert guard.reservation_id is not None
    context = replace(context, quota_reservation_outstanding_cny="0")
    result = fail_response_cache_candidate(
        scenario.config,
        context=context,
        prepared=prepared,
        output_safety_scan=None,
        output_safety_terminal=None,
        latency_ms=2,
        error_code="gateway.output_safety_unavailable",
    )
    assert result.status == "unavailable"

    connection = connect_database(scenario.config.database_url)
    try:
        reservation = connection.execute(
            """
            SELECT status,actual_micros FROM quota_reservations
            WHERE tenant_id=? AND request_id=?
            """,
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        assert tuple(reservation) == ("released", 0)
        assert connection.execute(
            "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
            (scenario.tenant_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
