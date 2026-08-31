"""PRD-139 channel-domain migration, legacy fidelity, and guarded rollback."""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_control.auth.service import authenticate_bearer, login_admin
from metahub_control.partner_portal.commission import (
    claim_next_commission_cursor,
    materialize_claimed_commission,
)
from metahub_control.partner_portal.models import (
    CreatePartnerCustomerRequest,
    CreatePartnerRequest,
)
from metahub_control.partner_portal.service import create_partner, create_partner_customer
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PASSWORD = f"Aa9!{secrets.token_urlsafe(24)}"
STAMP = "2026-08-31T00:00:00Z"
STAMP_DT = datetime(2026, 8, 31, tzinfo=UTC)
PROJECT_ID = "prj_" + "1" * 32


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
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
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=["http://localhost:5173"],
        control_background_schedulers_enabled=False,
    )


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0130(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    root = tmp_path_factory.mktemp("prd139-0130-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 130:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0130.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0130"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", PASSWORD)
        seed = apply_seed_data(database_url)
    return database, migrations, seed.tenant_id


@pytest.fixture
def migrated_0130(
    base_0130: tuple[Path, Path, str],
    tmp_path: Path,
) -> tuple[Path, Path, str]:
    base, source_migrations, tenant_id = base_0130
    database = tmp_path / "prd139-0130.sqlite3"
    migrations = tmp_path / "migrations"
    shutil.copy2(base, database)
    shutil.copytree(source_migrations, migrations)
    for filename in (
        "0131_channel_partner_attribution_management.sql",
        "0131_channel_partner_attribution_management.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations, tenant_id


def _insert_project(connection: sqlite3.Connection, tenant_id: str) -> str:
    user_id = str(
        connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND role='admin'",
            (tenant_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO projects(
             project_id,tenant_id,project_code,name,owner_user_id,status,
             created_by,updated_by,created_at,updated_at)
           VALUES(?,?,'prd139-channel','PRD-139 Channel',?,'active',?,?,?,?)""",
        (PROJECT_ID, tenant_id, user_id, user_id, user_id, STAMP, STAMP),
    )
    connection.commit()
    return user_id


def _insert_usage_charge(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> sqlite3.Row:
    metadata = connection.execute(
        """SELECT model.model_id,provider.provider_id,provider.provider_code,
                  key.virtual_key_id,environment.environment_id,
                  environment.code environment_code
             FROM models model
             JOIN providers provider
               ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
             JOIN virtual_keys key ON key.tenant_id=model.tenant_id
             JOIN environments environment
               ON environment.tenant_id=model.tenant_id AND environment.code='dev'
            WHERE model.tenant_id=?
            ORDER BY model.model_id,key.virtual_key_id LIMIT 1""",
        (tenant_id,),
    ).fetchone()
    assert metadata is not None
    request_id = "req_prd139_legacy_charge"
    trace_id = "trc_prd139_legacy_charge"
    usage_id = "usg_prd139_legacy_charge"
    snapshot_id = "prs_prd139_legacy_charge"
    connection.execute(
        """INSERT INTO gateway_request_claims(
             tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
             policy_snapshot_status,claimed_at)
           VALUES(?,?,?,?,NULL,'disabled',?)""",
        (tenant_id, request_id, trace_id, metadata["virtual_key_id"], STAMP),
    )
    connection.execute(
        """INSERT INTO request_logs(
             request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
             status,created_at,updated_at,environment_id,environment_code,project_id)
           VALUES(?,?,?,?,?,?,'success',?,?,?,?,?)""",
        (
            request_id,
            tenant_id,
            trace_id,
            metadata["virtual_key_id"],
            metadata["provider_id"],
            metadata["model_id"],
            STAMP,
            STAMP,
            metadata["environment_id"],
            metadata["environment_code"],
            PROJECT_ID,
        ),
    )
    connection.execute(
        """INSERT INTO price_snapshots(
             price_snapshot_id,tenant_id,request_id,model_id,provider_id,provider_code,
             currency,input_price_per_1k,output_price_per_1k,price_schema_version,
             billing_dimensions,provider_raw_mapping,source,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,'CNY','0.001000','0.002000','price-v1','{}','{}',
             'model_catalog','active',?,?)""",
        (
            snapshot_id,
            tenant_id,
            request_id,
            metadata["model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            STAMP,
            STAMP,
        ),
    )
    normalized = json.dumps(
        {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "cache_tokens": 1,
            "cost_amount": "0.001000",
            "cost_cny": "0.001000",
            "cost_usd": "0",
            "currency": "CNY",
            "price_schema_version": "price-v1",
            "price_snapshot_id": snapshot_id,
            "posting_source": "direct",
        },
        separators=(",", ":"),
    )
    connection.execute(
        """INSERT INTO usage_ledger(
             usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
             cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,currency,
             price_schema_version,estimated,usage_source,price_snapshot_id,model_id,
             provider_id,virtual_key_id,environment_id,environment_code,provider_code,
             project_id,created_at,updated_at)
           VALUES(?,?,?,3,2,5,1,'{}',?,'0.001000','0.001000','0','CNY','price-v1',
             0,'upstream',?,?,?,?,?,?,?,?,?,?)""",
        (
            usage_id,
            tenant_id,
            request_id,
            normalized,
            snapshot_id,
            metadata["model_id"],
            metadata["provider_id"],
            metadata["virtual_key_id"],
            metadata["environment_id"],
            metadata["environment_code"],
            metadata["provider_code"],
            PROJECT_ID,
            STAMP,
            STAMP,
        ),
    )
    entry = connection.execute(
        """SELECT * FROM finops_ledger_entries
            WHERE tenant_id=? AND usage_ledger_id=? AND entry_type='usage_charge'""",
        (tenant_id, usage_id),
    ).fetchone()
    assert entry is not None
    connection.commit()
    return entry


def _seed_legacy(database_url: str, tenant_id: str) -> dict[str, int]:
    config = _config(database_url)
    with closing(connect_database(database_url)) as connection:
        _insert_project(connection, tenant_id)
    login = login_admin(
        config,
        tenant_slug="default",
        email="admin@metahub.local",
        password=PASSWORD,
        request_id="req_prd139_legacy_login",
        trace_id="trc_prd139_legacy_login",
        user_agent="pytest",
        client_host="127.0.0.1",
    )
    context = authenticate_bearer(config, f"Bearer {login.access_token}")
    partner = create_partner(
        config,
        context,
        CreatePartnerRequest(
            partner_code="prd139.channel",
            display_name="PRD-139 Channel",
            portal_name="Legacy Partner",
            brand_primary_color="#176B5B",
            support_text=None,
        ),
        idempotency_key="prd139-partner-legacy",
        request_id="req_prd139_partner",
        trace_id="trc_prd139_partner",
        now=STAMP_DT,
    )
    customer = create_partner_customer(
        config,
        context,
        CreatePartnerCustomerRequest(
            project_id=PROJECT_ID,
            customer_code="legacy.customer",
            display_name="Legacy Customer",
        ),
        idempotency_key="prd139-customer-legacy",
        request_id="req_prd139_customer",
        trace_id="trc_prd139_customer",
        now=STAMP_DT,
    )
    with closing(connect_database(database_url)) as connection:
        _insert_usage_charge(connection, tenant_id)
    claim = claim_next_commission_cursor(
        config,
        worker_id="prd139-migration-test",
        now=STAMP_DT + timedelta(seconds=1),
    )
    assert claim is not None
    assert materialize_claimed_commission(
        config,
        claim,
        now=STAMP_DT + timedelta(seconds=2),
    ) is not None
    assert partner.partner.partner_id.startswith("par_")
    assert customer.customer.partner_customer_id.startswith("pcu_")
    with closing(connect_database(database_url)) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "partners",
                "partner_customers",
                "partner_commission_cursors",
                "commission_records",
                "partner_portal_operations",
                "partner_customer_outbox",
            )
        }


def test_0130_to_0131_empty_roundtrip_and_scheduler_guard(
    migrated_0130: tuple[Path, Path, str],
) -> None:
    database, migrations, _tenant_id = migrated_0130
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0131"]
    _healthy(database_url)
    assert rollback_last_migration(
        database_url,
        migrations_dir=migrations,
        expected_version="0131",
    ).rolled_back == "0131"
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0131"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """UPDATE channel_scheduler_state
                  SET fencing_generation=1,revision=revision+1,updated_at=?
                WHERE singleton_id=1""",
            ("2030-01-01T00:00:01.000Z",),
        )
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0131",
        )
    _healthy(database_url)


def test_legacy_six_table_backfill_hash_fence_and_down_guard(
    migrated_0130: tuple[Path, Path, str],
) -> None:
    database, migrations, tenant_id = migrated_0130
    database_url = _url(database)
    source_counts = _seed_legacy(database_url, tenant_id)
    assert all(count > 0 for count in source_counts.values())
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0131"]
    with closing(connect_database(database_url)) as connection:
        cursor_counts = {
            str(row["source_table"]): int(row["source_row_count"])
            for row in connection.execute("SELECT * FROM channel_migration_cursors")
        }
        assert cursor_counts == source_counts
        assert connection.execute(
            "SELECT COUNT(*) FROM channel_legacy_object_mappings"
        ).fetchone()[0] == sum(source_counts.values())
        assert connection.execute("SELECT COUNT(*) FROM channel_partners").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM channel_memberships").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM channel_registered_leads WHERE stage='legacy_unresolved'"
        ).fetchone()[0] == 1
        statement = connection.execute(
            """SELECT statement.total_amount_micros,statement.total_commission_micros,
                      statement.line_count,statement.reconciliation_sha256
                 FROM channel_statements statement"""
        ).fetchone()
        assert statement is not None
        assert tuple(statement[:3]) == (1000, 100, 1)
        assert len(statement["reconciliation_sha256"]) == 64
        with pytest.raises(sqlite3.IntegrityError, match="legacy_partner_retired"):
            connection.execute(
                "UPDATE partners SET display_name=display_name WHERE tenant_id=?",
                (tenant_id,),
            )
        connection.rollback()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0131",
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in source_counts
        } == source_counts
    _healthy(database_url)


def test_fresh_schema_installs_prd139_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0131"
    _healthy(database_url)
