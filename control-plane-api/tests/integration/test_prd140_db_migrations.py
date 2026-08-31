"""PRD-140 customer-success migration, legacy fidelity, and rollback guards."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_control.customer_health.service import (
    collect_source_snapshot,
    materialize_customer_health,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _copy_migrations(target: Path, maximum: int) -> None:
    target.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= maximum:
            shutil.copy2(source, target / source.name)


def _clone_with_0132(source: Path, tmp_path: Path) -> tuple[str, Path]:
    database = tmp_path / "prd140.sqlite3"
    migrations = tmp_path / "migrations"
    shutil.copy2(source, database)
    _copy_migrations(migrations, 132)
    return _url(database), migrations


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def pristine_0131(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd140-pristine-0131")
    migrations = root / "migrations"
    _copy_migrations(migrations, 131)
    database = root / "base.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0131"
    return database


@pytest.fixture(scope="module")
def legacy_0131(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd140-legacy-0131")
    migrations = root / "migrations"
    _copy_migrations(migrations, 131)
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0131"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", f"Aa9!{secrets.token_urlsafe(24)}")
        seed = apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        evaluated_at = (datetime.now(UTC) + timedelta(minutes=1)).replace(second=0, microsecond=0)
        evaluated_text = evaluated_at.isoformat(timespec="seconds").replace("+00:00", "Z")
        lease_expires = (
            (evaluated_at + timedelta(minutes=5))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        connection.execute(
            """UPDATE customer_health_scheduler_tenants
                  SET status='leased',generation=generation+1,lease_owner_id='prd140-test-worker',
                      lease_token=?,lease_expires_at=?,last_error_code=NULL,updated_at=?
                WHERE tenant_id=? AND status='pending'""",
            ("a" * 64, lease_expires, evaluated_text, seed.tenant_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        source = collect_source_snapshot(
            connection,
            seed.tenant_id,
            evaluated_at=evaluated_at,
        )
        score_id, inserted, outbox_created = materialize_customer_health(connection, source)
        assert inserted and outbox_created and score_id.startswith("chs_")
        next_evaluation = (
            (evaluated_at + timedelta(minutes=10))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        connection.execute(
            """UPDATE customer_health_scheduler_tenants
                  SET status='pending',generation=generation+1,next_evaluation_at=?,
                      consecutive_failure_count=0,lease_owner_id=NULL,lease_token=NULL,
                      lease_expires_at=NULL,last_completed_window_end=?,last_error_code=NULL,
                      updated_at=? WHERE tenant_id=? AND status='leased'""",
            (next_evaluation, source.window_end, evaluated_text, seed.tenant_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        connection.commit()
        assert connection.execute("SELECT count(*) FROM customer_health_scores").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM customer_health_score_outbox").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM workspace_health_projections").fetchone()[0]
            == 1
        )
    return database


def test_pristine_0131_to_0132_roundtrip(pristine_0131: Path, tmp_path: Path) -> None:
    database_url, migrations = _clone_with_0132(pristine_0131, tmp_path)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0132"]
    with closing(connect_database(database_url)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_success_permission_extensions"
            ).fetchone()[0]
            == 8
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_success_role_permissions"
            ).fetchone()[0]
            == 16
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_success_migration_cursors"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_account_projections"
            ).fetchone()[0]
            == 0
        )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0132",
        ).rolled_back
        == "0132"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0132"]
    _healthy(database_url)


def test_legacy_health_fidelity_write_guard_and_down_guard(
    legacy_0131: Path,
    tmp_path: Path,
) -> None:
    database_url, migrations = _clone_with_0132(legacy_0131, tmp_path)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0132"]
    with closing(connect_database(database_url)) as connection:
        source = connection.execute("SELECT * FROM customer_health_scores").fetchone()
        target = connection.execute(
            """SELECT * FROM platform_customer_health_snapshots
                WHERE customer_health_snapshot_id=?""",
            (source["customer_health_score_id"],),
        ).fetchone()
        assert target is not None
        preserved = {
            "formula_version": "score_version",
            "window_start": "window_start",
            "window_end": "window_end",
            "comparison_start": "comparison_start",
            "comparison_end": "comparison_end",
            "source_fingerprint": "source_fingerprint",
            "health_projection_sha256": "health_projection_sha256",
            "score_bps": "score_bps",
            "risk_level": "risk_level",
            "operation_request_id": "operation_request_id",
            "operation_trace_id": "operation_trace_id",
            "evaluated_at": "evaluated_at",
        }
        assert all(target[new] == source[old] for new, old in preserved.items())
        assert target["formula_version"] == "customer-health-v1"
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_account_projections"
            ).fetchone()[0]
            == connection.execute(
                "SELECT count(*) FROM workspace_tenant_bindings WHERE status='active'"
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_success_scheduler_state"
            ).fetchone()[0]
            == 6
            * connection.execute(
                "SELECT count(*) FROM platform_customer_account_projections"
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM platform_customer_success_legacy_mappings"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM permissions WHERE code='customer_health.read'"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(sqlite3.IntegrityError, match="legacy_customer_health_retired"):
            connection.execute(
                "UPDATE customer_health_scores SET updated_at=updated_at WHERE rowid=1"
            )
        connection.rollback()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0132",
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_active_legacy_lease_blocks_cutover(legacy_0131: Path, tmp_path: Path) -> None:
    database_url, migrations = _clone_with_0132(legacy_0131, tmp_path)
    with closing(connect_database(database_url)) as connection:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM customer_health_scheduler_tenants LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """UPDATE customer_health_scheduler_tenants
                  SET status='leased',generation=generation+1,lease_owner_id='prd140-test-worker',
                      lease_token=?,lease_expires_at='2030-01-01T00:02:00Z',
                      last_error_code=NULL,updated_at='2030-01-01T00:00:05Z'
                WHERE tenant_id=? AND status='pending'""",
            ("a" * 64, tenant_id),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        apply_migrations(database_url, migrations_dir=migrations)
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert (
            connection.execute(
                "SELECT max(version) FROM schema_migrations WHERE status='applied'"
            ).fetchone()[0]
            == "0131"
        )


def test_workspace_health_projection_drift_blocks_cutover(
    legacy_0131: Path,
    tmp_path: Path,
) -> None:
    database_url, migrations = _clone_with_0132(legacy_0131, tmp_path)
    with closing(connect_database(database_url)) as connection:
        connection.execute("DROP TRIGGER trg_workspace_health_transition_prd096")
        current = connection.execute(
            "SELECT risk_level FROM workspace_health_projections LIMIT 1"
        ).fetchone()[0]
        replacement = "critical" if current != "critical" else "healthy"
        connection.execute("UPDATE workspace_health_projections SET risk_level=?", (replacement,))
        connection.commit()
    with pytest.raises(DbError) as blocked:
        apply_migrations(database_url, migrations_dir=migrations)
    assert blocked.value.code == "db.migration_failed"


def test_fresh_schema_installs_prd140_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0132"
    _healthy(database_url)
