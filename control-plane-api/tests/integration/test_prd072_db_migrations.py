"""PRD-072 0064 文件事实、RBAC revision 8 与可逆迁移专项测试。"""

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

# 正反迁移测试共享正式目录与两代精确 hash，禁止维护测试私有权限矩阵。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
REVISION_7_HASHES = {
    "admin": "792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f",
    "developer": "2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c",
    "viewer": "3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086",
    "finance_admin": "96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114",
    "security_admin": "de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c",
    "ops_admin": "6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b",
}
REVISION_8_HASHES = {
    "admin": "6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052",
    "developer": "d1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87",
    "viewer": "519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5",
    "finance_admin": "96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114",
    "security_admin": "6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0",
    "ops_admin": "6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b",
}


def _url(path: Path) -> str:
    """返回当前用例独占 SQLite 文件的数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于目标版本的正式迁移，测试不会维护另一份 schema。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd072_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """构建含真实 seed 的 0063 模板和只到 0064 的正式迁移目录。"""

    root = tmp_path_factory.mktemp("prd072-migration-templates")
    migrations_0063 = _migration_subset(root / "migrations-0063", 63)
    migrations_0064 = _migration_subset(root / "migrations-0064", 64)
    database_0063 = root / "template-0063.db"
    database_url = _url(database_0063)
    assert apply_migrations(
        database_url,
        migrations_dir=migrations_0063,
    ).applied[-1] == "0063"
    apply_seed_data(database_url)
    return {
        "database_0063": database_0063,
        "migrations_0064": migrations_0064,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的 0063 模板并返回独占数据库 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _apply_0064(database_url: str, migrations: Path) -> None:
    """只应用 0064，使失败能够直接定位到本 PRD。"""

    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied == ["0064"]


def _matrix_hash(codes: list[str]) -> str:
    """按 Shared canonical JSON 合同计算自定义角色矩阵摘要。"""

    payload = json.dumps(
        sorted(codes),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _customize_security_admin(connection: sqlite3.Connection) -> tuple[str, str, int, str]:
    """合法移除一项旧权限并推进 CAS/hash，构造真实租户自定义矩阵。"""

    tenant_id, role_id, revision = connection.execute(
        "SELECT tenant_id,role_id,permission_revision FROM roles "
        "WHERE code='security_admin' ORDER BY tenant_id LIMIT 1"
    ).fetchone()
    relation_id = connection.execute(
        """
        SELECT relation.role_permission_id FROM role_permissions relation
        JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                   AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=? AND relation.role_id=? AND permission.code='dlp.write'
        """,
        (tenant_id, role_id),
    ).fetchone()[0]
    connection.execute(
        "DELETE FROM role_permissions WHERE role_permission_id=?",
        (relation_id,),
    )
    codes = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
            """,
            (tenant_id, role_id),
        ).fetchall()
    ]
    custom_hash = _matrix_hash(codes)
    connection.execute(
        "UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=? WHERE role_id=?",
        (int(revision) + 1, custom_hash, "2026-08-05T00:01:00Z", role_id),
    )
    connection.commit()
    # 返回升级前的租户 owner 与 CAS 快照，后续断言迁移没有覆盖自定义矩阵。
    return str(tenant_id), str(role_id), int(revision) + 1, custom_hash


def _role_hashes(connection: sqlite3.Connection) -> dict[str, str]:
    """读取第一个真实 tenant 的六角色 hash，便于精确比较 revision 合同。"""

    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
    ).fetchone()[0]
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT code,matrix_sha256 FROM roles WHERE tenant_id=? ORDER BY code",
            (tenant_id,),
        ).fetchall()
    }


def test_prd072_up_down_reup_restores_exact_revision_and_schema(
    prd072_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """主流程：0063→0064→0063→0064 精确恢复目录、hash、表和完整性。"""

    database_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "round-trip.db",
    )
    _apply_0064(database_url, prd072_templates["migrations_0064"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 61
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 162
        assert _role_hashes(connection) == REVISION_8_HASHES
        expected_columns = {
            "uploaded_files": 23,
            "file_extracts": 15,
            "file_dlp_matches": 9,
            "file_events": 18,
        }
        assert {
            table: len(connection.execute(f"PRAGMA table_info('{table}')").fetchall())
            for table in expected_columns
        } == expected_columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    result = rollback_last_migration(
        database_url,
        migrations_dir=prd072_templates["migrations_0064"],
    )
    assert result.rolled_back == "0064"
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 59
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 155
        assert _role_hashes(connection) == REVISION_7_HASHES
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name IN "
            "('uploaded_files','file_extracts','file_dlp_matches','file_events')"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _apply_0064(database_url, prd072_templates["migrations_0064"])
    assert apply_migrations(
        database_url,
        migrations_dir=prd072_templates["migrations_0064"],
    ).applied == []


def test_prd072_new_tenant_gets_exact_revision_8_matrix(
    prd072_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """新租户流程：一个触发器事务内必须物化 61 权限、162 关系和六角色。"""

    database_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "new-tenant.db",
    )
    _apply_0064(database_url, prd072_templates["migrations_0064"])
    with connect_database(database_url) as connection:
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES('tenant_prd072_new','prd072-new','PRD072 New','internal_only',
                     'active','2026-08-05T00:00:00Z','2026-08-05T00:00:00Z')
            """
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd072_new'"
        ).fetchone()[0] == 61
        assert connection.execute(
            "SELECT COUNT(*) FROM role_permissions WHERE tenant_id='tenant_prd072_new'"
        ).fetchone()[0] == 162
        assert {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT code,matrix_sha256 FROM roles "
                "WHERE tenant_id='tenant_prd072_new' ORDER BY code"
            ).fetchall()
        } == REVISION_8_HASHES


def test_prd072_custom_security_admin_is_not_expanded(
    prd072_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """权限流程：升级前已定制 SecurityAdmin 不得被静默授予 file.read。"""

    database_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "custom-role.db",
    )
    with connect_database(database_url) as connection:
        tenant_id, role_id, revision, custom_hash = _customize_security_admin(connection)
    _apply_0064(database_url, prd072_templates["migrations_0064"])
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?",
                (role_id,),
            ).fetchone()
        ) == (revision, custom_hash)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code IN ('file.read','file.write')
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 0


def test_prd072_file_owner_state_event_and_down_fact_guard(
    prd072_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """事实主流程：tenant owner、单向终态、Extract 和两个不可变事件均由数据库复核。"""

    database_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "file-facts.db",
    )
    _apply_0064(database_url, prd072_templates["migrations_0064"])
    created_at = "2026-08-05T01:00:00Z"
    completed_at = "2026-08-05T01:01:00Z"
    with connect_database(database_url) as connection:
        tenant_id, user_id = connection.execute(
            "SELECT tenant_id,user_id FROM users ORDER BY user_id LIMIT 1"
        ).fetchone()
        file_id = "file_" + "a" * 32
        connection.execute(
            """
            INSERT INTO uploaded_files(
              file_id,tenant_id,uploaded_by,source_request_id,source_trace_id,
              original_name_ciphertext,file_extension,media_type,size_bytes,
              content_fingerprint,status,virus_scan_status,dlp_scan_status,
              dlp_action,effective_dlp_rule_id,match_count,ruleset_fingerprint,
              storage_backend,original_object_key,failure_error_code,
              created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,'txt','text/plain',18,?,'processing','not_configured',
                     'pending',NULL,NULL,0,NULL,NULL,NULL,NULL,?,?,NULL)
            """,
            (
                file_id,
                tenant_id,
                user_id,
                "req_prd072",
                "trc_prd072",
                b"x" * 64,
                "hmac-sha256:" + "b" * 64,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO file_events(
              file_event_id,tenant_id,file_id,event_type,actor_user_id,
              source_request_id,source_trace_id,file_status,media_type,size_bytes,
              virus_scan_status,dlp_scan_status,dlp_action,match_count,
              estimated_tokens,extractor_version,error_code,occurred_at
            ) VALUES(?,?,?,'file.uploaded',?,?,'trc_prd072','processing','text/plain',18,
                     'not_configured','pending',NULL,0,NULL,NULL,NULL,?)
            """,
            ("fevt_" + "c" * 32, tenant_id, file_id, user_id, "req_prd072", created_at),
        )
        connection.execute(
            """
            UPDATE uploaded_files SET status='ready',dlp_scan_status='clean',
              ruleset_fingerprint=?,storage_backend='local_encrypted_v1',
              original_object_key=?,updated_at=?,completed_at=?
            WHERE tenant_id=? AND file_id=?
            """,
            (
                "sha256:" + "d" * 64,
                f"objects/aa/{file_id}/original.bin",
                completed_at,
                completed_at,
                tenant_id,
                file_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO file_extracts(
              file_extract_id,tenant_id,file_id,status,extractor_version,
              text_size_bytes,text_code_points,estimated_tokens,estimator_version,
              text_fingerprint,storage_backend,extracted_object_key,
              failure_error_code,created_at,updated_at
            ) VALUES(?,?,?,'ready','txt-v1',18,18,5,'file-token-estimator-v1',?,
                     'local_encrypted_v1',?,NULL,?,?)
            """,
            (
                "fext_" + "e" * 32,
                tenant_id,
                file_id,
                "hmac-sha256:" + "f" * 64,
                f"objects/aa/{file_id}/extract.bin",
                completed_at,
                completed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO file_events(
              file_event_id,tenant_id,file_id,event_type,actor_user_id,
              source_request_id,source_trace_id,file_status,media_type,size_bytes,
              virus_scan_status,dlp_scan_status,dlp_action,match_count,
              estimated_tokens,extractor_version,error_code,occurred_at
            ) VALUES(?,?,?,'file.scanned',?,?,'trc_prd072','ready','text/plain',18,
                     'not_configured','clean',NULL,0,5,'txt-v1',NULL,?)
            """,
            ("fevt_" + "1" * 32, tenant_id, file_id, user_id, "req_prd072", completed_at),
        )
        with pytest.raises(sqlite3.IntegrityError, match="file_event_immutable"):
            connection.execute(
                "UPDATE file_events SET match_count=1 WHERE file_id=?",
                (file_id,),
            )
        connection.commit()

    with pytest.raises(DbError, match="prd072_down_business_facts_present"):
        rollback_last_migration(
            database_url,
            migrations_dir=prd072_templates["migrations_0064"],
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0064'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM file_events"
        ).fetchone()[0] == 2


def test_prd072_cross_tenant_user_is_rejected(
    prd072_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """租户异常：验证另一租户的用户绝不能成为当前租户文件 owner。"""

    database_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "cross-tenant.db",
    )
    _apply_0064(database_url, prd072_templates["migrations_0064"])
    with connect_database(database_url) as connection:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES('tenant_prd072_other','prd072-other','Other','internal_only','active',
                     '2026-08-05T00:00:00Z','2026-08-05T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd072_other','tenant_prd072_other','other@example.invalid','Other',
                     'hash','viewer','active','2026-08-05T00:00:00Z','2026-08-05T00:00:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="file_owner_mismatch"):
            connection.execute(
                """
                INSERT INTO uploaded_files(
                  file_id,tenant_id,uploaded_by,source_request_id,source_trace_id,
                  original_name_ciphertext,file_extension,media_type,size_bytes,
                  content_fingerprint,status,virus_scan_status,dlp_scan_status,
                  dlp_action,effective_dlp_rule_id,match_count,ruleset_fingerprint,
                  storage_backend,original_object_key,failure_error_code,
                  created_at,updated_at,completed_at
                ) VALUES(?,?,?,'req_cross','trc_cross',?,'txt','text/plain',1,?,
                         'processing','not_configured','pending',NULL,NULL,0,NULL,
                         NULL,NULL,NULL,?,?,NULL)
                """,
                (
                    "file_" + "9" * 32,
                    tenant_id,
                    "user_prd072_other",
                    b"x" * 64,
                    "hmac-sha256:" + "8" * 64,
                    "2026-08-05T00:00:00Z",
                    "2026-08-05T00:00:00Z",
                ),
            )


def test_prd072_down_rejects_webhook_subscription_and_future_dependency(
    prd072_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """降级异常：文件订阅或未知后续 view 均在首项持久 DDL 前原子阻断。"""

    subscription_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "subscription-guard.db",
    )
    _apply_0064(subscription_url, prd072_templates["migrations_0064"])
    with connect_database(subscription_url) as connection:
        tenant_id, user_id = connection.execute(
            "SELECT tenant_id,user_id FROM users ORDER BY user_id LIMIT 1"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
              max_attempts,created_by,updated_by,created_at,updated_at
            ) VALUES('whe_prd072_guard',?,'PRD072','https://example.invalid/hook',
                     '["file.scanned"]','cipher','prefix','fingerprint','active',5000,3,
                     ?,?,'2026-08-05T00:00:00Z','2026-08-05T00:00:00Z')
            """,
            (tenant_id, user_id, user_id),
        )
        connection.commit()
    with pytest.raises(DbError, match="prd072_down_business_facts_present"):
        rollback_last_migration(
            subscription_url,
            migrations_dir=prd072_templates["migrations_0064"],
        )

    dependency_url = _clone(
        prd072_templates["database_0063"],
        tmp_path / "future-dependency-guard.db",
    )
    _apply_0064(dependency_url, prd072_templates["migrations_0064"])
    with connect_database(dependency_url) as connection:
        connection.execute(
            "CREATE VIEW future_file_projection AS SELECT tenant_id,file_id FROM uploaded_files"
        )
    with pytest.raises(DbError):
        rollback_last_migration(
            dependency_url,
            migrations_dir=prd072_templates["migrations_0064"],
        )
    with connect_database(dependency_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='future_file_projection'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0064'"
        ).fetchone()[0] == "applied"
