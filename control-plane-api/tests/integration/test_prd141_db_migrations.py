"""PRD-141 platform-growth migration, source fidelity, and rollback guards."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.auth.service import _load_permission_snapshot
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SCHEDULER_JOBS = {
    "source_eligibility",
    "daily_funnel",
    "cohort_projection",
    "attribution",
    "experiment_guardrail",
    "export_build",
    "privacy_correction",
    "growth_event_outbox",
    "product_event_outbox",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _copy_migrations(target: Path, maximum: int) -> None:
    target.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= maximum:
            shutil.copy2(source, target / source.name)


def _clone(source: Path, tmp_path: Path) -> tuple[str, Path]:
    database = tmp_path / "prd141.sqlite3"
    migrations = tmp_path / "migrations"
    shutil.copy2(source, database)
    _copy_migrations(migrations, 133)
    return _url(database), migrations


def _digest_source(connection: object) -> str:
    event_rows = [
        dict(row)
        for row in connection.execute(  # type: ignore[attr-defined]
            "SELECT * FROM product_events ORDER BY product_event_id"
        ).fetchall()
    ]
    outbox_rows = [
        dict(row)
        for row in connection.execute(  # type: ignore[attr-defined]
            "SELECT * FROM product_event_outbox ORDER BY product_event_outbox_id"
        ).fetchall()
    ]
    return hashlib.sha256(
        json.dumps(
            [event_rows, outbox_rows],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def pristine_0132(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd141-pristine-0132")
    migrations = root / "migrations"
    _copy_migrations(migrations, 132)
    database = root / "base.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0132"
    return database


@pytest.fixture(scope="module")
def legacy_0132(
    pristine_0132: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    root = tmp_path_factory.mktemp("prd141-legacy-0132")
    database = root / "base.sqlite3"
    shutil.copy2(pristine_0132, database)
    database_url = _url(database)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}")
        seed = apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        event_id = connection.execute(
            "SELECT product_event_id FROM product_events WHERE tenant_id=? LIMIT 1",
            (seed.tenant_id,),
        ).fetchone()[0]
        owner_trigger = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='trigger' AND name='trg_product_event_outbox_owner_insert_prd085'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_product_event_outbox_owner_insert_prd085")
        connection.execute(
            """
            INSERT INTO product_event_outbox(
              product_event_outbox_id,tenant_id,product_event_id,status,attempt_count,
              generation,next_attempt_at,created_at,updated_at)
            VALUES('pevo_14100000000000000000000000000000',?,?,'pending',0,0,
              '2030-01-01T00:00:00Z','2029-12-31T23:59:00Z','2029-12-31T23:59:00Z')
            """,
            (seed.tenant_id, event_id),
        )
        connection.execute(owner_trigger)
        connection.commit()
    return database


def test_pristine_0132_to_0133_roundtrip(pristine_0132: Path, tmp_path: Path) -> None:
    database_url, migrations = _clone(pristine_0132, tmp_path)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0133"]
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_permission_extensions"
        ).fetchone()[0] == 8
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_role_permissions"
        ).fetchone()[0] == 17
        assert {
            row[0]
            for row in connection.execute(
                "SELECT job_code FROM platform_growth_scheduler_state"
            ).fetchall()
        } == SCHEDULER_JOBS
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_migration_cursors"
        ).fetchone()[0] == 5
        command_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='platform_growth_commands'"
        ).fetchone()[0]
        assert "expected_revision>=0" in command_sql
        assert connection.execute(
            "SELECT count(*) FROM rbac_permission_templates "
            "WHERE permission_code='product_analytics.read'"
        ).fetchone()[0] == 0
    _healthy(database_url)
    assert rollback_last_migration(
        database_url,
        migrations_dir=migrations,
        expected_version="0133",
    ).rolled_back == "0133"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM rbac_permission_templates "
            "WHERE permission_code='product_analytics.read'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM rbac_role_permission_templates "
            "WHERE permission_code='product_analytics.read'"
        ).fetchone()[0] == 2
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0133"]
    _healthy(database_url)


def test_legacy_product_event_fidelity_permission_contract_and_down_guard(
    legacy_0132: Path,
    tmp_path: Path,
) -> None:
    database_url, migrations = _clone(legacy_0132, tmp_path)
    with closing(connect_database(database_url)) as connection:
        source_digest = _digest_source(connection)
        event_count = connection.execute("SELECT count(*) FROM product_events").fetchone()[0]
        outbox_count = connection.execute(
            "SELECT count(*) FROM product_event_outbox"
        ).fetchone()[0]
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0133"]
    with closing(connect_database(database_url)) as connection:
        assert _digest_source(connection) == source_digest
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_event_eligibility"
        ).fetchone()[0] == event_count
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_legacy_mappings"
        ).fetchone()[0] == event_count + outbox_count
        assert connection.execute(
            "SELECT count(*) FROM platform_growth_event_eligibility "
            "WHERE status!='excluded' OR reason_code!='historical_consent_unproven'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM permissions WHERE code='product_analytics.read'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM role_permissions role_permission "
            "JOIN permissions permission ON permission.permission_id=role_permission.permission_id "
            "WHERE permission.code='product_analytics.read'"
        ).fetchone()[0] == 0
        user = connection.execute(
            "SELECT user_id,tenant_id,role FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()
        assert user is not None
        snapshot = _load_permission_snapshot(
            connection,
            tenant_id=user["tenant_id"],
            user_id=user["user_id"],
            role_code=user["role"],
            unavailable_error_code="rbac.unavailable",
        )
        assert "product_analytics.read" not in snapshot.permission_codes
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0133",
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_active_product_event_outbox_lease_blocks_cutover(
    legacy_0132: Path,
    tmp_path: Path,
) -> None:
    database_url, migrations = _clone(legacy_0132, tmp_path)
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            UPDATE product_event_outbox SET status='leased',attempt_count=1,generation=1,
              lease_owner_id='prd141-worker',lease_token=?,
              lease_expires_at='2031-01-01T00:05:00Z',next_attempt_at=NULL,
              last_error_code=NULL,updated_at='2031-01-01T00:00:00Z'
            WHERE status='pending'
            """,
            ("a" * 64,),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        connection.commit()
    with pytest.raises(DbError) as blocked:
        apply_migrations(database_url, migrations_dir=migrations)
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='platform_growth_permission_extensions'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT max(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0] == "0132"
    _healthy(database_url)


@pytest.mark.parametrize("fact_kind", ["cursor", "scheduler"])
def test_cursor_or_scheduler_fact_blocks_empty_schema_down(
    pristine_0132: Path,
    tmp_path: Path,
    fact_kind: str,
) -> None:
    database_url, migrations = _clone(pristine_0132, tmp_path)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0133"]
    with closing(connect_database(database_url)) as connection:
        if fact_kind == "cursor":
            connection.execute(
                """
                UPDATE platform_growth_migration_cursors
                SET generation=1,source_row_count=1,
                    source_sha256=?,updated_at='2030-01-01T00:00:00Z'
                WHERE source_code='billing'
                """,
                ("b" * 64,),
            )
        else:
            connection.execute(
                """
                UPDATE platform_growth_scheduler_state
                SET generation=1,cursor_json='{"offset":1}',
                    updated_at='2030-01-01T00:00:00Z'
                WHERE job_code='daily_funnel'
                """
            )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0133",
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)
