"""PRD-142 brand-domain migration, legacy fidelity, and rollback guards."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from contextlib import closing
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SCHEDULER_JOBS = {
    "legacy_outbox",
    "asset_scan",
    "domain_verify",
    "public_publish",
    "deployment_bundle",
    "brand_event_outbox",
    "cleanup",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _copy_migrations(target: Path, maximum: int) -> None:
    target.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= maximum:
            shutil.copy2(source, target / source.name)


def _clone(source: Path, tmp_path: Path) -> tuple[str, Path]:
    database = tmp_path / "prd142.sqlite3"
    migrations = tmp_path / "migrations"
    shutil.copy2(source, database)
    _copy_migrations(migrations, 134)
    return _url(database), migrations


def _legacy_digest(connection: object) -> str:
    tables = ("branding_configs", "branding_events", "branding_event_outbox")
    rows = [
        [
            dict(row)
            for row in connection.execute(  # type: ignore[attr-defined]
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
        ]
        for table in tables
    ]
    return hashlib.sha256(
        json.dumps(
            rows,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("ascii")
    ).hexdigest()


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def pristine_0133(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd142-pristine-0133")
    migrations = root / "migrations"
    _copy_migrations(migrations, 133)
    database = root / "base.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0133"
    return database


@pytest.fixture(scope="module")
def legacy_0133(pristine_0133: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd142-legacy-0133")
    database = root / "base.sqlite3"
    shutil.copy2(pristine_0133, database)
    database_url = _url(database)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}")
        apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd142_nondefault','prd142-nondefault','PRD142 Nondefault',
              'internal_only','active','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')
            """
        )
        user_id = "user_prd142_nondefault"
        connection.execute(
            """
            INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at)
            VALUES(?, 'tenant_prd142_nondefault','prd142-nondefault@example.invalid',
              'PRD142 Nondefault',?,'admin','active',
              '2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')
            """,
            (
                user_id,
                hash_password(secrets.token_urlsafe(32), salt=secrets.token_bytes(16)),
            ),
        )
        connection.execute(
            """
            UPDATE branding_configs
            SET product_name='Legacy Assigned Later',revision=2,updated_by_user_id=?,
              updated_request_id='req_prd142_legacy',updated_trace_id='trace_prd142_legacy',
              updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','+1 second')
            WHERE tenant_id='tenant_prd142_nondefault'
            """,
            (user_id,),
        )
        config_id = connection.execute(
            "SELECT branding_config_id FROM branding_configs "
            "WHERE tenant_id='tenant_prd142_nondefault'"
        ).fetchone()[0]
        event_suffix = connection.execute(
            "SELECT substr(metahub_sha256(?||char(31)||2),1,32)", (config_id,)
        ).fetchone()[0]
        event_id = f"bev_{event_suffix}"
        payload = json.dumps(
            {
                "branding_config_id": config_id,
                "changed_fields": ["product_name"],
                "custom_domain_status": "not_configured",
                "logo_present": False,
                "logo_sha256": None,
                "revision": 2,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        stamp = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now','+1 second')"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
              object_type,object_id,object_name,before_summary_json,after_summary_json,
              changed_fields_json,request_id,trace_id,created_at)
            VALUES('aud_prd142_legacy','tenant_prd142_nondefault',?,
              'prd142-nondefault@example.invalid','admin','branding.updated',
              'branding_config',?,NULL,'{}','{}','["product_name"]',
              'req_prd142_legacy','trace_prd142_legacy',?)
            """,
            (user_id, config_id, stamp),
        )
        connection.execute(
            """
            INSERT INTO branding_events(
              branding_event_id,tenant_id,branding_config_id,audit_log_id,event_type,
              revision,actor_user_id,request_id,trace_id,changed_fields_json,payload_json,
              payload_sha256,created_at)
            VALUES(?,'tenant_prd142_nondefault',?,'aud_prd142_legacy','branding.updated',
              2,?,'req_prd142_legacy','trace_prd142_legacy','["product_name"]',?,
              metahub_sha256(?),?)
            """,
            (event_id, config_id, user_id, payload, payload, stamp),
        )
        connection.execute(
            """
            INSERT INTO branding_event_outbox(
              branding_event_outbox_id,tenant_id,branding_event_id,webhook_event_id,
              payload_json,payload_sha256,status,attempt_count,generation,next_attempt_at,
              subscription_snapshot_json,snapshot_frozen_at,created_at,updated_at)
            VALUES(?,'tenant_prd142_nondefault',?,? ,?,metahub_sha256(?),'pending',0,0,?,
              '[]',?,?,?)
            """,
            (
                f"bro_{event_suffix}",
                event_id,
                f"evt_brd_{event_suffix}",
                payload,
                payload,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
        connection.commit()
    return database


def test_pristine_0133_to_0134_roundtrip(pristine_0133: Path, tmp_path: Path) -> None:
    database_url, migrations = _clone(pristine_0133, tmp_path)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0134"]
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("SELECT count(*) FROM platform_brands").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM platform_brand_permission_extensions"
        ).fetchone()[0] == 6
        assert connection.execute(
            "SELECT count(*) FROM workspace_brand_permission_extensions"
        ).fetchone()[0] == 2
        assert {
            row[0] for row in connection.execute("SELECT job_code FROM brand_scheduler_state")
        } == SCHEDULER_JOBS
        assert connection.execute("SELECT count(*) FROM brand_migration_cursors").fetchone()[0] == 0
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0134"
    ).rolled_back == "0134"
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0134"]
    _healthy(database_url)


def test_legacy_inventory_fidelity_permission_cutover_and_down_guard(
    legacy_0133: Path, tmp_path: Path
) -> None:
    database_url, migrations = _clone(legacy_0133, tmp_path)
    with closing(connect_database(database_url)) as connection:
        source_digest = _legacy_digest(connection)
        source_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("branding_configs", "branding_events", "branding_event_outbox")
        }
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0134"]
    with closing(connect_database(database_url)) as connection:
        assert _legacy_digest(connection) == source_digest
        statuses = {
            row["tenant_id"]: row["status"]
            for row in connection.execute(
                """
                SELECT mapping.tenant_id,mapping.status
                FROM brand_legacy_object_mappings mapping
                WHERE mapping.source_kind='branding_config'
                """
            ).fetchall()
        }
        assert statuses["tenant_prd142_nondefault"] == "requires_assignment"
        assert list(statuses.values()).count("default_collapsed") == (
            source_counts["branding_configs"] - 1
        )
        assert connection.execute(
            "SELECT count(*) FROM permissions WHERE code IN('branding.read','branding.manage')"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM rbac_permission_templates "
            "WHERE permission_code IN('branding.read','branding.manage')"
        ).fetchone()[0] == 0
        with pytest.raises(Exception, match="branding_cutover_read_only"):
            connection.execute(
                "UPDATE branding_configs SET product_name='blocked' WHERE tenant_id=?",
                (next(iter(statuses)),),
            )
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0134")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_active_legacy_outbox_lease_blocks_cutover(legacy_0133: Path, tmp_path: Path) -> None:
    database_url, migrations = _clone(legacy_0133, tmp_path)
    with closing(connect_database(database_url)) as connection:
        event = connection.execute("SELECT * FROM branding_event_outbox LIMIT 1").fetchone()
        assert event is not None
        connection.execute(
            """
            UPDATE branding_event_outbox
            SET status='leased',attempt_count=1,generation=1,lease_owner_id='prd142-worker',
              lease_token=?,lease_expires_at='2031-01-01T00:05:00Z',next_attempt_at=NULL,
              updated_at='2031-01-01T00:00:00Z'
            WHERE branding_event_outbox_id=?
            """,
            ("a" * 64, event["branding_event_outbox_id"]),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        apply_migrations(database_url, migrations_dir=migrations)
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)
