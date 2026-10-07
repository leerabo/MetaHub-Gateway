"""PRD-144 Workspace ProviderCostRate consumers and owner bindings."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from metahub_control.batch_jobs.service import _priced_candidate
from metahub_gateway.budgets.service import _estimate_request_cost
from metahub_gateway.pricing import resolve_workspace_cost_rate
from metahub_gateway.response_cache.settlement import _freeze_reference_estimate
from metahub_gateway.routing.models import GatewayRoute, _load_routing_prices
from metahub_gateway.usage import ledger as ledger_module
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    claim_gateway_request,
    record_request_log,
    safe_record_usage_ledger,
)
from metahub_gateway.usage.spool import UsageSpool, replay_usage_spool_batch
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import TokenEstimate

OBSERVED = "2026-09-03T00:00:00.000Z"
OBSERVED_AT = datetime(2026, 9, 3, tzinfo=UTC)
FUTURE = "2101-01-01T00:00:00.000Z"
MODEL_LKG = "model_prd144_lkg"
MODEL_CURRENT = "model_prd144_current"
MODEL_USD = "model_prd144_usd"
MODEL_UNPRICED = "model_prd144_unpriced"
RATE_LKG = "pcr_" + "a" * 32
RATE_CURRENT = "pcr_" + "b" * 32
RATE_USD = "pcr_" + "c" * 32
VERSION_LKG = "pcrv_" + "1" * 32
VERSION_FUTURE = "pcrv_" + "2" * 32
VERSION_CURRENT = "pcrv_" + "3" * 32
VERSION_USD = "pcrv_" + "4" * 32


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd144-gateway-template")
    database = root / "template.sqlite3"
    database_url = _url(database)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    return database


def _scenario(template: Path, tmp_path: Path) -> SimpleNamespace:
    database = tmp_path / "prd144-gateway.sqlite3"
    shutil.copy2(template, database)
    database_url = _url(database)
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            """
            SELECT provider.provider_id,provider.provider_code,binding.workspace_id,
              membership.workspace_membership_id,key.virtual_key_id,
              environment.environment_id,environment.code AS environment_code
            FROM providers provider
            JOIN workspace_tenant_bindings binding
              ON binding.tenant_id=provider.tenant_id AND binding.status='active'
            JOIN workspace_memberships membership
              ON membership.workspace_id=binding.workspace_id
             AND membership.tenant_id=binding.tenant_id
             AND membership.role_code='owner' AND membership.status='active'
            JOIN virtual_keys key
              ON key.tenant_id=provider.tenant_id AND key.status='active'
            JOIN environments environment
              ON environment.tenant_id=provider.tenant_id AND environment.code='dev'
            WHERE provider.status='active'
            ORDER BY provider.provider_id,key.virtual_key_id
            LIMIT 1
            """
        ).fetchone()
        assert owner is not None
        tenant_id = str(
            connection.execute(
                "SELECT tenant_id FROM workspace_tenant_bindings "
                "WHERE workspace_id=? AND status='active'",
                (owner["workspace_id"],),
            ).fetchone()[0]
        )
        for model_id, alias, currency in (
            (MODEL_LKG, "prd144-lkg", "CNY"),
            (MODEL_CURRENT, "prd144-current", "CNY"),
            (MODEL_USD, "prd144-usd", "USD"),
            (MODEL_UNPRICED, "prd144-unpriced", "CNY"),
        ):
            _insert_model(
                connection,
                tenant_id=tenant_id,
                provider_id=str(owner["provider_id"]),
                model_id=model_id,
                alias=alias,
                currency=currency,
            )
        _insert_rate(
            connection,
            rate_id=RATE_LKG,
            workspace_id=str(owner["workspace_id"]),
            tenant_id=tenant_id,
            provider_id=str(owner["provider_id"]),
            model_id=MODEL_LKG,
            currency="CNY",
            membership_id=str(owner["workspace_membership_id"]),
            versions=(
                (VERSION_LKG, 1, 1_000_000, 2_000_000, 500_000, "2020-01-01T00:00:00Z",
                 "2100-01-01T00:00:00Z", "superseded"),
                (VERSION_FUTURE, 2, 3_000_000, 4_000_000, 1_500_000, "2100-01-01T00:00:00Z",
                 None, "active"),
            ),
            head_version=VERSION_FUTURE,
        )
        _insert_rate(
            connection,
            rate_id=RATE_CURRENT,
            workspace_id=str(owner["workspace_id"]),
            tenant_id=tenant_id,
            provider_id=str(owner["provider_id"]),
            model_id=MODEL_CURRENT,
            currency="CNY",
            membership_id=str(owner["workspace_membership_id"]),
            versions=(
                (VERSION_CURRENT, 1, 500_000, 1_000_000, 250_000,
                 "2020-01-01T00:00:00Z", None, "active"),
            ),
            head_version=VERSION_CURRENT,
        )
        _insert_rate(
            connection,
            rate_id=RATE_USD,
            workspace_id=str(owner["workspace_id"]),
            tenant_id=tenant_id,
            provider_id=str(owner["provider_id"]),
            model_id=MODEL_USD,
            currency="USD",
            membership_id=str(owner["workspace_membership_id"]),
            versions=(
                (VERSION_USD, 1, 1_500_000, 2_500_000, 750_000,
                 "2020-01-01T00:00:00Z", None, "active"),
            ),
            head_version=VERSION_USD,
        )
        connection.commit()
    config = SimpleNamespace(
        database_url=database_url,
        tenant_id=tenant_id,
        finops_usage_spool_root=str(tmp_path / "spool"),
        auth_token_secret="prd144-gateway-test-hmac-secret-32-characters",
        finops_usage_spool_claim_lease_seconds=60,
        finops_usage_spool_poll_interval_seconds=1,
    )
    return SimpleNamespace(
        config=config,
        tenant_id=tenant_id,
        workspace_id=str(owner["workspace_id"]),
        provider_id=str(owner["provider_id"]),
        provider_code=str(owner["provider_code"]),
        virtual_key_id=str(owner["virtual_key_id"]),
        environment_id=str(owner["environment_id"]),
        environment_code=str(owner["environment_code"]),
    )


def _insert_model(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    provider_id: str,
    model_id: str,
    alias: str,
    currency: str,
) -> None:
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,created_at,updated_at,input_price_per_1k,
          output_price_per_1k,cache_price_per_1k,currency,price_schema_version)
        VALUES(?,?,?,?,?,'{}','active',?,?,?,?,?,?,?)
        """,
        (
            model_id,
            tenant_id,
            provider_id,
            alias,
            f"{alias}-upstream",
            OBSERVED,
            OBSERVED,
            "99.000000",
            "99.000000",
            "49.500000",
            currency,
            "legacy-price-must-not-resolve",
        ),
    )


def _insert_rate(
    connection: sqlite3.Connection,
    *,
    rate_id: str,
    workspace_id: str,
    tenant_id: str,
    provider_id: str,
    model_id: str,
    currency: str,
    membership_id: str,
    versions: tuple[tuple[str, int, int, int, int, str, str | None, str], ...],
    head_version: str,
) -> None:
    connection.execute(
        """
        INSERT INTO workspace_provider_cost_rates(
          workspace_provider_cost_rate_id,workspace_id,tenant_id,provider_id,
          model_id,currency,created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (rate_id, workspace_id, tenant_id, provider_id, model_id, currency, OBSERVED),
    )
    for version_id, number, input_rate, output_rate, cache_rate, start, end, status in versions:
        connection.execute(
            """
            INSERT INTO workspace_provider_cost_rate_versions(
              workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,
              version_number,input_rate_micros_per_1k,output_rate_micros_per_1k,
              cache_rate_micros_per_1k,
              effective_from,effective_until,source_type,source_id,source_sha256,
              status,revision,content_sha256,created_by_membership_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,'manual',?,?,?,?,?,?,?)
            """,
            (
                version_id,
                rate_id,
                number,
                input_rate,
                output_rate,
                cache_rate,
                start,
                end,
                f"manual-{version_id}",
                "a" * 64,
                status,
                number,
                "b" * 64,
                membership_id,
                OBSERVED,
            ),
        )
    connection.execute(
        """
        INSERT INTO workspace_provider_cost_rate_heads(
          workspace_provider_cost_rate_id,workspace_provider_cost_rate_version_id,
          head_revision,adoption_state,updated_at)
        VALUES(?,?,?,'active',?)
        """,
        (rate_id, head_version, len(versions), OBSERVED),
    )


def _route(scenario: SimpleNamespace, model_id: str, alias: str) -> GatewayRoute:
    return GatewayRoute(
        tenant_id=scenario.tenant_id,
        model_id=model_id,
        model_alias=alias,
        upstream_model_name=f"{alias}-upstream",
        capabilities={},
        provider_id=scenario.provider_id,
        provider_code=scenario.provider_code,
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="PRD-144 test provider",
        provider_base_url="https://provider.invalid/v1",
        requested_model=alias,
        environment=scenario.environment_code,
        environment_id=scenario.environment_id,
    )


def _context(
    scenario: SimpleNamespace,
    suffix: str,
    model_id: str = MODEL_LKG,
) -> RequestLogContext:
    alias = {
        MODEL_LKG: "prd144-lkg",
        MODEL_CURRENT: "prd144-current",
        MODEL_USD: "prd144-usd",
    }.get(model_id, "prd144-unpriced")
    context = RequestLogContext(
        tenant_id=scenario.tenant_id,
        request_id=f"req-prd144-{suffix}",
        trace_id=f"trc-prd144-{suffix}",
        virtual_key_id=scenario.virtual_key_id,
        environment_id=scenario.environment_id,
        environment_code=scenario.environment_code,
        provider_id=scenario.provider_id,
        model_id=model_id,
        endpoint="chat/completions",
        request_model=alias,
        resolved_model=alias,
        provider_code=scenario.provider_code,
    )
    claim_gateway_request(scenario.config, context)
    record_request_log(
        scenario.config,
        context,
        status="success",
        error_code=None,
        latency_ms=5,
    )
    return context


def _usage() -> dict[str, object]:
    return {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cache_tokens": 0,
        "estimated": False,
        "usage_source": "upstream",
        "raw_usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def test_resolver_selects_lkg_under_future_head_and_never_uses_legacy_fallback(
    migrated_template: Path,
    tmp_path: Path,
) -> None:
    scenario = _scenario(migrated_template, tmp_path)
    with closing(connect_database(scenario.config.database_url)) as connection:
        lkg = resolve_workspace_cost_rate(
            connection,
            tenant_id=scenario.tenant_id,
            model_id=MODEL_LKG,
            observed_at=OBSERVED,
        )
        assert lkg is not None
        assert lkg.source_id == VERSION_LKG
        assert lkg.price_schema_version == f"provider-cost-v1:{VERSION_LKG}"
        assert (lkg.currency, lkg.input_price_per_1k, lkg.output_price_per_1k) == (
            "CNY",
            Decimal("1"),
            Decimal("2"),
        )
        future = resolve_workspace_cost_rate(
            connection,
            tenant_id=scenario.tenant_id,
            model_id=MODEL_LKG,
            observed_at=FUTURE,
        )
        assert future is not None and future.source_id == VERSION_FUTURE
        usd = resolve_workspace_cost_rate(
            connection,
            tenant_id=scenario.tenant_id,
            model_id=MODEL_USD,
            observed_at=OBSERVED,
        )
        assert usd is not None and usd.currency == "USD"
        assert usd.source_id == VERSION_USD
        assert resolve_workspace_cost_rate(
            connection,
            tenant_id=scenario.tenant_id,
            model_id=MODEL_UNPRICED,
            observed_at=OBSERVED,
        ) is None
        connection.execute(
            "UPDATE workspace_provider_cost_rate_heads "
            "SET adoption_state='disabled',head_revision=head_revision+1 "
            "WHERE workspace_provider_cost_rate_id=?",
            (RATE_LKG,),
        )
        assert resolve_workspace_cost_rate(
            connection,
            tenant_id=scenario.tenant_id,
            model_id=MODEL_LKG,
            observed_at=OBSERVED,
        ) is None


def test_budget_routing_batch_and_cache_share_exact_workspace_rate(
    migrated_template: Path,
    tmp_path: Path,
) -> None:
    scenario = _scenario(migrated_template, tmp_path)
    route = _route(scenario, MODEL_LKG, "prd144-lkg")
    usd_route = _route(scenario, MODEL_USD, "prd144-usd")
    tokens = TokenEstimate(input_tokens=100, output_tokens=50, truncated=False)
    context = _context(scenario, "cache")
    with closing(connect_database(scenario.config.database_url)) as connection:
        budget = _estimate_request_cost(
            connection,
            route,
            "chat",
            {},
            token_estimate=tokens,
            price_as_of=OBSERVED_AT,
        )
        assert budget.price_available is True
        # Prices use the current per-million-token contract. 100 input and 50
        # output tokens at 99/99 per million cost 0.000200 CNY.
        assert budget.cost_cny == Decimal("0.000200")
        usd_budget = _estimate_request_cost(
            connection,
            usd_route,
            "chat",
            {},
            token_estimate=tokens,
            price_as_of=OBSERVED_AT,
        )
        assert usd_budget.price_available is False
        facts = _load_routing_prices(
            connection,
            scenario.tenant_id,
            (MODEL_LKG, MODEL_CURRENT),
            OBSERVED_AT,
        )
        assert [(fact.source, fact.price_schema_version) for fact in facts] == [
            ("workspace_provider_cost_rate", f"provider-cost-v1:{VERSION_LKG}"),
            ("workspace_provider_cost_rate", f"provider-cost-v1:{VERSION_CURRENT}"),
        ]
        batch = _priced_candidate(
            connection,
            scenario.tenant_id,
            role="default",
            model_id=MODEL_LKG,
            as_of=OBSERVED_AT,
        )
        assert batch.price_source == "workspace_provider_cost_rate"
        assert batch.price_version == f"provider-cost-v1:{VERSION_LKG}"
        with pytest.raises(ApiError):
            _priced_candidate(
                connection,
                scenario.tenant_id,
                role="default",
                model_id=MODEL_USD,
                as_of=OBSERVED_AT,
            )
        candidate = SimpleNamespace(
            owner=SimpleNamespace(input_tokens=100, output_tokens=50)
        )
        estimate, available = _freeze_reference_estimate(
            connection,
            context,
            route,
            candidate,
            snapshot_id="prs_" + "5" * 32,
            observed_at=OBSERVED,
        )
        connection.commit()
        assert available is True
        assert estimate.amount == Decimal("0.000200")
        assert estimate.price_schema_version == f"provider-cost-v1:{VERSION_LKG}"
        assert estimate.price_snapshot_evidence is not None
        assert estimate.price_snapshot_evidence.source == "response_cache_reference"
        binding = connection.execute(
            """
            SELECT workspace_provider_cost_rate_version_id,workspace_id,tenant_id
            FROM price_snapshot_provider_cost_rate_bindings
            WHERE price_snapshot_id=?
            """,
            (estimate.price_snapshot_id,),
        ).fetchone()
        assert tuple(binding) == (
            VERSION_LKG,
            scenario.workspace_id,
            scenario.tenant_id,
        )


def test_usage_writes_exact_rate_snapshot_and_all_internal_finops_owners(
    migrated_template: Path,
    tmp_path: Path,
) -> None:
    scenario = _scenario(migrated_template, tmp_path)
    context = _context(scenario, "usage")
    committed = safe_record_usage_ledger(
        scenario.config,
        context,
        _usage(),
        require_durable=True,
    )
    assert committed is not None and committed.created is True
    assert committed.amount == "0.000200"
    assert committed.price_snapshot_source == "workspace_provider_cost_rate"
    assert committed.price_schema_version == f"provider-cost-v1:{VERSION_LKG}"
    with closing(connect_database(scenario.config.database_url)) as connection:
        rate_binding = connection.execute(
            """
            SELECT workspace_provider_cost_rate_version_id,workspace_id,tenant_id
            FROM price_snapshot_provider_cost_rate_bindings
            WHERE price_snapshot_id=?
            """,
            (committed.price_snapshot_id,),
        ).fetchone()
        assert tuple(rate_binding) == (
            VERSION_LKG,
            scenario.workspace_id,
            scenario.tenant_id,
        )
        usage_ledger_id = connection.execute(
            "SELECT usage_ledger_id FROM finops_ledger_entries "
            "WHERE tenant_id=? AND finops_ledger_entry_id=?",
            (scenario.tenant_id, committed.ledger_entry_id),
        ).fetchone()[0]
        owners = connection.execute(
            """
            SELECT source_kind,source_id,workspace_id,tenant_id
            FROM internal_finops_ledger_owner_bindings
            WHERE source_id IN(?,?,?)
            ORDER BY source_kind
            """,
                (
                    committed.ledger_entry_id,
                    committed.price_snapshot_id,
                    usage_ledger_id,
                ),
        ).fetchall()
        assert {(row["source_kind"], row["source_id"]) for row in owners} == {
            ("ledger_entry", committed.ledger_entry_id),
            ("price_snapshot", committed.price_snapshot_id),
            ("usage_ledger", usage_ledger_id),
        }
        assert {
            (row["workspace_id"], row["tenant_id"]) for row in owners
        } == {(scenario.workspace_id, scenario.tenant_id)}


class _FailUsageInsertConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
        if "INSERT INTO usage_ledger" in statement:
            raise sqlite3.OperationalError("injected_usage_insert_failure")
        return self.connection.execute(statement, parameters)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def test_spool_replay_preserves_provider_rate_and_owner_bindings(
    migrated_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(migrated_template, tmp_path)
    context = _context(scenario, "spool")
    original_connect = ledger_module._connect
    monkeypatch.setattr(
        ledger_module,
        "_connect",
        lambda config: _FailUsageInsertConnection(
            connect_database(config.database_url)
        ),
    )
    assert safe_record_usage_ledger(
        scenario.config,
        context,
        _usage(),
        require_durable=True,
    ) is None
    monkeypatch.setattr(ledger_module, "_connect", original_connect)
    spool = UsageSpool(scenario.config)
    claims = spool.claim_batch()
    assert len(claims) == 1
    frozen = claims[0].payload["estimate"]
    assert frozen["evidence"]["source"] == "workspace_provider_cost_rate"
    assert frozen["price_schema_version"] == f"provider-cost-v1:{VERSION_LKG}"
    spool.release(claims[0])

    async def replay() -> tuple[object, object]:
        state = SimpleNamespace(runtime_event_queue=asyncio.Queue())
        first = await replay_usage_spool_batch(scenario.config, state)
        second = await replay_usage_spool_batch(scenario.config, state)
        return first, second

    first, second = asyncio.run(replay())
    assert first.committed == first.events_enqueued == 1
    assert second.claimed == second.committed == second.existing == 0
    with closing(connect_database(scenario.config.database_url)) as connection:
        row = connection.execute(
            """
            SELECT entry.price_snapshot_id,entry.usage_ledger_id,
              entry.finops_ledger_entry_id,entry.posting_source,
              binding.workspace_provider_cost_rate_version_id
            FROM finops_ledger_entries entry
            JOIN price_snapshot_provider_cost_rate_bindings binding
              ON binding.price_snapshot_id=entry.price_snapshot_id
            WHERE entry.tenant_id=? AND entry.source_request_id=?
            """,
            (scenario.tenant_id, context.request_id),
        ).fetchone()
        assert row["posting_source"] == "spool_replay"
        assert row["workspace_provider_cost_rate_version_id"] == VERSION_LKG
        owner_kinds = {
            item[0]
            for item in connection.execute(
                """
                SELECT source_kind FROM internal_finops_ledger_owner_bindings
                WHERE source_id IN(?,?,?)
                """,
                (
                    row["price_snapshot_id"],
                    row["usage_ledger_id"],
                    row["finops_ledger_entry_id"],
                ),
            ).fetchall()
        }
        assert owner_kinds == {"price_snapshot", "usage_ledger", "ledger_entry"}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"
