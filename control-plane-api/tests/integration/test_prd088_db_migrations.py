"""PRD-088 migration round-trip, RBAC preservation, and rollback guard tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.rbac import permission_matrix_sha256

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
CATALOG_HASH_22 = "b44391603c39067c55f6d32742e2cb3daa9833721ab2ed4657c2c50db4dca125"
ROLE_HASH_22 = "0ebf053173c2a5b1acc10143f092753ef2dafe1d63f2a4ba8398ff49599b4379"
CATALOG_HASH_23 = "fcda2ee936d9a04c1f8f469ef204ac9fdf04aa7c646a895e93d8b5d7dd2f0350"
ROLE_HASH_23 = "07ff7275f31fa5cdd03a9f7f80e36692a71fca5e2af8022d0e765940dcc758f8"
STAMP = "2030-01-01T00:00:00Z"
STAMP_2 = "2030-01-01T00:00:01Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _clone(source: Path, destination: Path) -> str:
    shutil.copy2(source, destination)
    return _url(destination)


def _healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _hashes(connection: sqlite3.Connection) -> tuple[str, str]:
    catalog = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT permission_code,category,name,description,risk_level,catalog_revision
            FROM rbac_permission_templates ORDER BY permission_code
            """
        )
    ]
    roles = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT role_code,permission_code,default_granted
            FROM rbac_role_permission_templates ORDER BY role_code,permission_code
            """
        )
    ]
    return (
        hashlib.sha256("\x1e".join(catalog).encode()).hexdigest(),
        hashlib.sha256("\x1e".join(roles).encode()).hexdigest(),
    )


def _version(connection: sqlite3.Connection) -> str:
    return str(
        connection.execute(
            """
            SELECT version FROM schema_migrations
            WHERE status='applied' ORDER BY version DESC LIMIT 1
            """
        ).fetchone()[0]
    )


def _ids(connection: sqlite3.Connection) -> tuple[str, str, str]:
    tenant_id = str(connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()[0])
    user = connection.execute(
        """
        SELECT user_id,email FROM users
        WHERE tenant_id=? AND role='admin' ORDER BY user_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    config_id = str(
        connection.execute(
            "SELECT branding_config_id FROM branding_configs WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0]
    )
    return tenant_id, str(user["user_id"]), config_id


@pytest.fixture(scope="module")
def empty_0081(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd088-empty-0081")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 81:
            shutil.copy2(source, migrations / source.name)
    database = root / "empty.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied == [f"{version:04d}" for version in range(1, 82)]
    connection = connect_database(_url(database))
    try:
        assert _version(connection) == "0081"
        _healthy(connection)
    finally:
        connection.close()
    return database


@pytest.fixture(scope="module")
def seeded_0081(empty_0081: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd088-seeded-0081")
    database = root / "seeded.sqlite3"
    shutil.copy2(empty_0081, database)
    apply_seed_data(_url(database))
    connection = connect_database(_url(database))
    try:
        assert connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM branding_configs").fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()
    return database


def test_fresh_repeat_pristine_down_and_reapply(empty_0081: Path, tmp_path: Path) -> None:
    database_url = _clone(empty_0081, tmp_path / "roundtrip.sqlite3")
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_23, ROLE_HASH_23)
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM rbac_permission_templates),
                  (SELECT COUNT(*) FROM rbac_role_permission_templates)
                """
            ).fetchone()
        ) == (98, 254)
        assert [
            tuple(row)
            for row in connection.execute(
                """
            SELECT rowid,permission_code FROM rbac_permission_templates
            WHERE permission_code LIKE 'branding.%' ORDER BY rowid
            """
            )
        ] == [(97, "branding.read"), (98, "branding.manage")]
        audit_schema = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'"
            ).fetchone()[0]
        )
        assert "'branding.updated'" in audit_schema
        assert "'branding_config'" in audit_schema
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE '%_prd088'"
            ).fetchone()[0]
            == 10
        )
        _healthy(connection)
    finally:
        connection.close()

    repeated = apply_migrations(database_url, migrations_dir=empty_0081.parent / "migrations")
    assert repeated.applied == [] and repeated.skipped[-1] == "0081"
    assert rollback_last_migration(database_url, expected_version="0081").rolled_back == "0081"
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0080"
        assert _hashes(connection) == (CATALOG_HASH_22, ROLE_HASH_22)
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM rbac_permission_templates),
                  (SELECT COUNT(*) FROM rbac_role_permission_templates)
                """
            ).fetchone()
        ) == (96, 247)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'branding_%'"
            ).fetchone()[0]
            == 0
        )
        audit_schema = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'"
            ).fetchone()[0]
        )
        assert "branding.updated" not in audit_schema
        assert "branding_config" not in audit_schema
        _healthy(connection)
    finally:
        connection.close()

    assert apply_migrations(
        database_url, migrations_dir=empty_0081.parent / "migrations"
    ).applied == ["0081"]
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_23, ROLE_HASH_23)
        _healthy(connection)
    finally:
        connection.close()


def test_existing_custom_role_is_preserved_and_new_tenant_gets_defaults(
    empty_0081: Path,
    tmp_path: Path,
) -> None:
    database_url = _clone(empty_0081, tmp_path / "existing-and-new.sqlite3")
    assert rollback_last_migration(database_url, expected_version="0081").rolled_back == "0081"
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        old_rowids = {
            str(row["code"]): int(row["rowid"])
            for row in connection.execute(
                "SELECT rowid,code FROM permissions WHERE tenant_id=?", (seed.tenant_id,)
            )
        }
        developer = connection.execute(
            """
            SELECT role_id,permission_revision FROM roles
            WHERE tenant_id=? AND code='developer'
            """,
            (seed.tenant_id,),
        ).fetchone()
        removed = connection.execute(
            """
            SELECT permission_id FROM permissions
            WHERE tenant_id=? AND code='debug_console.use'
            """,
            (seed.tenant_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
            (seed.tenant_id, developer["role_id"], removed),
        )
        remaining = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                """,
                (seed.tenant_id, developer["role_id"]),
            )
        }
        custom_hash = permission_matrix_sha256(remaining)
        custom_revision = int(developer["permission_revision"]) + 1
        connection.execute(
            """
            UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (custom_revision, custom_hash, STAMP, seed.tenant_id, developer["role_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    assert apply_migrations(
        database_url, migrations_dir=empty_0081.parent / "migrations"
    ).applied == ["0081"]
    connection = connect_database(database_url)
    try:
        assert {
            str(row["code"]): int(row["rowid"])
            for row in connection.execute(
                """
                SELECT rowid,code FROM permissions
                WHERE tenant_id=? AND code NOT LIKE 'branding.%'
                """,
                (seed.tenant_id,),
            )
        } == old_rowids
        developer_after = connection.execute(
            """
            SELECT role_id,permission_revision,matrix_sha256 FROM roles
            WHERE tenant_id=? AND code='developer'
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert tuple(developer_after)[1:] == (custom_revision, custom_hash)
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code LIKE 'branding.%'
            """,
                (seed.tenant_id, developer_after["role_id"]),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM permissions WHERE tenant_id=?", (seed.tenant_id,)
            ).fetchone()[0]
            == 98
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (seed.tenant_id,)
            ).fetchone()[0]
            == 252
        )

        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd088_new','prd088-new','PRD088 New','internal_only','active',?,?)
            """,
            (STAMP, STAMP),
        )
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM branding_configs WHERE tenant_id='tenant_prd088_new'),
                  (SELECT COUNT(*) FROM roles WHERE tenant_id='tenant_prd088_new'),
                  (SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd088_new'),
                  (SELECT COUNT(*) FROM role_permissions WHERE tenant_id='tenant_prd088_new')
                """
            ).fetchone()
        ) == (1, 6, 98, 254)
        connection.commit()
        _healthy(connection)
    finally:
        connection.close()

    assert rollback_last_migration(database_url, expected_version="0081").rolled_back == "0081"
    connection = connect_database(database_url)
    try:
        developer_down = connection.execute(
            """
            SELECT permission_revision,matrix_sha256 FROM roles
            WHERE tenant_id=? AND code='developer'
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert tuple(developer_down) == (custom_revision, custom_hash)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM permissions WHERE tenant_id=?", (seed.tenant_id,)
            ).fetchone()[0]
            == 96
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (seed.tenant_id,)
            ).fetchone()[0]
            == 246
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM role_permissions WHERE tenant_id='tenant_prd088_new'"
            ).fetchone()[0]
            == 247
        )
        _healthy(connection)
    finally:
        connection.close()


def _insert_audit(connection: sqlite3.Connection, *, audit_id: str) -> None:
    tenant_id, user_id, config_id = _ids(connection)
    email = str(
        connection.execute(
            "SELECT email FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at)
        VALUES(?,?,?,?,'admin','branding.updated','branding_config',?,NULL,
          '{"revision":1}','{"revision":2}','["revision"]',NULL,NULL,
          'req-prd088-guard','trace-prd088-guard',?)
        """,
        (audit_id, tenant_id, user_id, email, config_id, STAMP_2),
    )


def _update_config(connection: sqlite3.Connection) -> tuple[str, str, str]:
    tenant_id, user_id, config_id = _ids(connection)
    connection.execute(
        """
        UPDATE branding_configs SET product_name='Guard Fact',revision=2,
          updated_by_user_id=?,updated_request_id='req-prd088-guard',
          updated_trace_id='trace-prd088-guard',updated_at=?
        WHERE tenant_id=?
        """,
        (user_id, STAMP_2, tenant_id),
    )
    return tenant_id, user_id, config_id


def _insert_event_chain(connection: sqlite3.Connection, *, include_outbox: bool) -> None:
    tenant_id, user_id, config_id = _update_config(connection)
    audit_id = "aud_" + "8" * 32
    _insert_audit(connection, audit_id=audit_id)
    suffix = hashlib.sha256(f"{config_id}\x1f2".encode("ascii")).hexdigest()[:32]
    payload = json.dumps(
        {
            "branding_config_id": config_id,
            "changed_fields": ["product_name"],
            "custom_domain_status": "not_configured",
            "logo_present": False,
            "logo_sha256": None,
            "revision": 2,
            "schema_version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO branding_events(
          branding_event_id,tenant_id,branding_config_id,audit_log_id,event_type,revision,
          actor_user_id,request_id,trace_id,changed_fields_json,payload_json,payload_sha256,
          created_at)
        VALUES(?,?,?,?,'branding.updated',2,?,'req-prd088-guard','trace-prd088-guard',
          '["product_name"]',?,?,?)
        """,
        ("bev_" + suffix, tenant_id, config_id, audit_id, user_id, payload, payload_hash, STAMP_2),
    )
    if include_outbox:
        connection.execute(
            """
            INSERT INTO branding_event_outbox(
              branding_event_outbox_id,tenant_id,branding_event_id,webhook_event_id,
              payload_json,payload_sha256,status,attempt_count,generation,lease_owner_id,
              lease_token,lease_expires_at,next_attempt_at,last_error_code,
              subscription_snapshot_json,snapshot_frozen_at,delivered_at,created_at,updated_at)
            VALUES(?,?,? ,?,?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,'[]',?,NULL,?,?)
            """,
            (
                "bro_" + suffix,
                tenant_id,
                "bev_" + suffix,
                "evt_brd_" + suffix,
                payload,
                payload_hash,
                STAMP_2,
                STAMP_2,
                STAMP_2,
                STAMP_2,
            ),
        )


def _insert_subscription(connection: sqlite3.Connection) -> None:
    tenant_id, user_id, _ = _ids(connection)
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
          secret_version,created_by,updated_by,created_at,updated_at)
        VALUES('whe_prd088_guard',?,'PRD088 Guard','https://example.invalid/prd088',
          '["branding.updated"]','enc:v1:test-fixture','test',?,'active',5000,3,1,?,?,?,?)
        """,
        (tenant_id, "f" * 64, user_id, user_id, STAMP, STAMP),
    )


def _customize_branding_reader(connection: sqlite3.Connection) -> None:
    tenant_id, _, _ = _ids(connection)
    role = connection.execute(
        """
        SELECT role_id,permission_revision FROM roles
        WHERE tenant_id=? AND code='developer'
        """,
        (tenant_id,),
    ).fetchone()
    removed = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='debug_console.use'",
        (tenant_id,),
    ).fetchone()[0]
    connection.execute(
        "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
        (tenant_id, role["role_id"], removed),
    )
    remaining = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
            """,
            (tenant_id, role["role_id"]),
        )
    }
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (
            int(role["permission_revision"]) + 1,
            permission_matrix_sha256(remaining),
            STAMP,
            tenant_id,
            role["role_id"],
        ),
    )


@pytest.mark.parametrize(
    "fact_kind", ("config", "audit", "event", "outbox", "subscription", "rbac")
)
def test_down_guards_are_atomic(
    fact_kind: str,
    seeded_0081: Path,
    tmp_path: Path,
) -> None:
    database_url = _clone(seeded_0081, tmp_path / f"down-{fact_kind}.sqlite3")
    connection = connect_database(database_url)
    try:
        if fact_kind == "config":
            _update_config(connection)
        elif fact_kind == "audit":
            _insert_audit(connection, audit_id="aud_" + "7" * 32)
        elif fact_kind in {"event", "outbox"}:
            _insert_event_chain(connection, include_outbox=fact_kind == "outbox")
        elif fact_kind == "subscription":
            _insert_subscription(connection)
        else:
            _customize_branding_reader(connection)
        connection.commit()
        _healthy(connection)
    finally:
        connection.close()

    with pytest.raises(DbError, match="prd088_down_guard_failed"):
        rollback_last_migration(database_url, expected_version="0081")
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0081"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name='branding_configs'"
            ).fetchone()[0]
            == 1
        )
        _healthy(connection)
    finally:
        connection.close()


def test_down_guard_precedes_destructive_ddl() -> None:
    source = (MIGRATIONS_DIR / "0081_tenant_white_label_branding.down.sql").read_text(
        encoding="utf-8"
    )
    guard = source.index("prd088_down_guard_failed")
    first_ddl = source.index("DROP TRIGGER trg_tenants_branding_initialize_prd088")
    assert guard < first_ddl
    for marker in (
        "branding_events",
        "branding_event_outbox",
        "branding.updated",
        "branding_config",
        "webhook_endpoints",
        "webhook_delivery_attempts",
        "webhook_dead_letter_events",
        "branding.read",
        "branding.manage",
        CATALOG_HASH_23,
        ROLE_HASH_23,
    ):
        assert marker in source[guard:first_ddl]
