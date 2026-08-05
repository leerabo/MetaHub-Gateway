"""PRD-073 0065 文件安全事实、RBAC revision 9 与可逆迁移专项测试。"""

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

# 测试复制正式迁移而不维护私有 schema；哈希来自 Shared 的 canonical 权限集合算法。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
REVISION_8_HASHES = {
    "admin": "6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052",
    "developer": "d1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87",
    "viewer": "519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5",
    "finance_admin": "96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114",
    "security_admin": "6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0",
    "ops_admin": "6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b",
}
REVISION_9_HASHES = {
    "admin": "e098913e46c2f1512960a9ff91f8aec5f8ccd6267d414404a07c9f591079a319",
    "developer": "a55457236abcd09c3d42d57f50e636494c5d8ec7ddd92a33f2f94fb95f7ed15f",
    "viewer": "33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9",
    "finance_admin": REVISION_8_HASHES["finance_admin"],
    "security_admin": "28cc5f8be8a582475d14b9d68f72ba2d0284c0755fe4c43412c613d175c4ffa6",
    "ops_admin": REVISION_8_HASHES["ops_admin"],
}


def _url(path: Path) -> str:
    """返回当前测试独占 SQLite 文件的数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的正式迁移，避免后续 PRD 干扰本条边界测试。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd073_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """构建含真实 seed 的 0064 模板及只到 0065 的迁移目录。"""

    root = tmp_path_factory.mktemp("prd073-migration-templates")
    migrations_0064 = _migration_subset(root / "migrations-0064", 64)
    migrations_0065 = _migration_subset(root / "migrations-0065", 65)
    database_0064 = root / "template-0064.db"
    database_url = _url(database_0064)
    assert apply_migrations(database_url, migrations_dir=migrations_0064).applied[-1] == "0064"
    apply_seed_data(database_url)
    return {"database_0064": database_0064, "migrations_0065": migrations_0065}


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的 0064 模板并返回用例独占数据库 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _apply_0065(database_url: str, migrations: Path) -> None:
    """只应用 0065，使迁移失败能够直接归因于 PRD-073。"""

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0065"]


def _role_hashes(connection: sqlite3.Connection) -> dict[str, str]:
    """读取首个租户六角色矩阵哈希，验证迁移没有只更新目录计数。"""

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


def _matrix_hash(codes: list[str]) -> str:
    """按产品 canonical JSON 规则计算角色权限矩阵摘要。"""

    payload = json.dumps(sorted(codes), ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    return hashlib.sha256(payload).hexdigest()


def _insert_ready_file(connection: sqlite3.Connection) -> tuple[str, str, str, str, str]:
    """插入一个 0064 可验证的 ready TXT owner，供安全事实触发器测试复用。"""

    tenant_id, user_id = connection.execute(
        "SELECT tenant_id,user_id FROM users ORDER BY user_id LIMIT 1"
    ).fetchone()
    file_id = "file_" + "a" * 32
    extract_id = "fext_" + "b" * 32
    content_fingerprint = "hmac-sha256:" + "c" * 64
    extract_fingerprint = "hmac-sha256:" + "d" * 64
    created_at = "2026-08-05T02:00:00Z"
    connection.execute(
        """
        INSERT INTO uploaded_files(
          file_id,tenant_id,uploaded_by,source_request_id,source_trace_id,
          original_name_ciphertext,file_extension,media_type,size_bytes,
          content_fingerprint,status,virus_scan_status,dlp_scan_status,
          dlp_action,effective_dlp_rule_id,match_count,ruleset_fingerprint,
          storage_backend,original_object_key,failure_error_code,
          created_at,updated_at,completed_at
        ) VALUES(?,?,?,?,?,?,'txt','text/plain',64,?,'processing','not_configured',
                 'pending',NULL,NULL,0,NULL,NULL,NULL,NULL,?,?,NULL)
        """,
        (
            file_id,
            tenant_id,
            user_id,
            "req_prd073_file",
            "trc_prd073_file",
            b"x" * 64,
            content_fingerprint,
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        UPDATE uploaded_files SET status='ready',dlp_scan_status='clean',
          ruleset_fingerprint=?,storage_backend='local_encrypted_v1',
          original_object_key=?,updated_at=?,completed_at=?
        WHERE tenant_id=? AND file_id=?
        """,
        (
            "sha256:" + "e" * 64,
            "objects/prd073/original.bin",
            created_at,
            created_at,
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
        ) VALUES(?,?,?,'ready','txt-v1',64,64,16,'file-token-estimator-v1',?,
                 'local_encrypted_v1','objects/prd073/extract.bin',NULL,?,?)
        """,
        (extract_id, tenant_id, file_id, extract_fingerprint, created_at, created_at),
    )
    # 返回 owner 与固定时间，后续事实测试不得从请求自行构造 tenant 或 actor。
    return str(tenant_id), str(user_id), file_id, extract_id, created_at


def _insert_failed_scan(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    file_id: str,
    extract_id: str,
    attempt_no: int,
    risk_score: int | None,
    request_id: str = "req_prd073_failed",
) -> str:
    """插入无 finding 的 failed attempt，供 null 分数和链路边界合同测试复用。"""

    scan_id = f"fscn_{attempt_no:032x}"
    completed_at = "2026-08-05T02:30:00Z"
    connection.execute(
        """
        INSERT INTO file_security_scans(
          scan_id,tenant_id,file_id,file_extract_id,requested_by,
          source_request_id,source_trace_id,attempt_no,status,risk_level,
          risk_score,coverage_status,model_access_status,detector_version,
          ocr_status,ocr_engine_version,scanned_page_count,scanned_image_count,
          finding_count,high_finding_count,boundary_version,
          content_fingerprint,extract_fingerprint,report_sha256,
          failure_error_code,created_at,updated_at,completed_at
        ) VALUES(?,?,?,?,?,?,?,?,'failed','unknown',?,'unavailable','blocked',
                 'file-security-detector-v1','failed',NULL,0,0,0,0,
                 'file-context-boundary-v1',?,?,?,'file.security_scan_unavailable',?,?,?)
        """,
        (
            scan_id,
            tenant_id,
            file_id,
            extract_id,
            user_id,
            request_id,
            "trc_prd073_failed",
            attempt_no,
            risk_score,
            "hmac-sha256:" + "c" * 64,
            "hmac-sha256:" + "d" * 64,
            "sha256:" + f"{attempt_no:064x}",
            completed_at,
            completed_at,
            completed_at,
        ),
    )
    return scan_id


def test_prd073_up_down_restores_exact_catalog_and_keeps_prd072_objects(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """主流程：0064→0065→0064 精确恢复 RBAC，且 PRD-072 四表始终保留。"""

    database_url = _clone(prd073_templates["database_0064"], tmp_path / "roundtrip.db")
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 62
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 166
        assert _role_hashes(connection) == REVISION_9_HASHES
        assert {
            table: len(connection.execute(f"PRAGMA table_info('{table}')").fetchall())
            for table in (
                "file_security_scans",
                "file_security_findings",
                "file_security_events",
            )
        } == {
            "file_security_scans": 28,
            "file_security_findings": 15,
            "file_security_events": 14,
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert rollback_last_migration(
        database_url, migrations_dir=prd073_templates["migrations_0065"]
    ).rolled_back == "0065"
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 61
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 162
        assert _role_hashes(connection) == REVISION_8_HASHES
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'file_security_%'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='uploaded_files'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd073_finding_scan_event_owner_and_immutable_guards(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """安全主流程：Finding→Scan→Event 同事务可提交，伪造 owner/计数和改写均拒绝。"""

    database_url = _clone(prd073_templates["database_0064"], tmp_path / "facts.db")
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    with connect_database(database_url) as connection:
        tenant_id, user_id, file_id, extract_id, completed_at = _insert_ready_file(
            connection
        )
        scan_id = "fscn_" + "1" * 32
        finding_id = "fsfd_" + "2" * 32
        # deferred FK 允许先写 finding；Scan trigger 会反向复核完整集合与最高风险。
        connection.execute(
            """
            INSERT INTO file_security_findings(
              finding_id,tenant_id,scan_id,file_id,detector_code,category,severity,
              confidence_milli,source_kind,location_kind,location_index,
              evidence_fingerprint,status,created_at,updated_at
            ) VALUES(?,?,?,?,'prompt.visible_instruction','prompt_injection','high',
                     990,'visible_text','document',NULL,?,'detected',?,?)
            """,
            (
                finding_id,
                tenant_id,
                scan_id,
                file_id,
                "hmac-sha256:" + "3" * 64,
                completed_at,
                completed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO file_security_scans(
              scan_id,tenant_id,file_id,file_extract_id,requested_by,
              source_request_id,source_trace_id,attempt_no,status,risk_level,
              risk_score,coverage_status,model_access_status,detector_version,
              ocr_status,ocr_engine_version,scanned_page_count,scanned_image_count,
              finding_count,high_finding_count,boundary_version,
              content_fingerprint,extract_fingerprint,report_sha256,
              failure_error_code,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,'req_prd073_scan','trc_prd073_scan',1,'blocked','high',
                     90,'complete','blocked','file-security-detector-v1',
                     'not_required',NULL,0,0,1,1,'file-context-boundary-v1',?,?,?,
                     NULL,?,?,?)
            """,
            (
                scan_id,
                tenant_id,
                file_id,
                extract_id,
                user_id,
                "hmac-sha256:" + "c" * 64,
                "hmac-sha256:" + "d" * 64,
                "sha256:" + "4" * 64,
                completed_at,
                completed_at,
                completed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO file_security_events(
              file_security_event_id,tenant_id,scan_id,file_id,event_type,
              actor_user_id,source_request_id,source_trace_id,risk_level,
              finding_count,high_finding_count,model_access_status,
              detector_version,occurred_at
            ) VALUES(?,?,?,?,'file.prompt_injection_detected',?,
                     'req_prd073_scan','trc_prd073_scan','high',1,1,'blocked',
                     'file-security-detector-v1',?)
            """,
            ("fsev_" + "5" * 32, tenant_id, scan_id, file_id, user_id, completed_at),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="file_security_scan_immutable"):
            connection.execute(
                "UPDATE file_security_scans SET risk_score=1 WHERE scan_id=?", (scan_id,)
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="file_security_finding_owner_mismatch"
        ):
            connection.execute(
                """
                INSERT INTO file_security_findings(
                  finding_id,tenant_id,scan_id,file_id,detector_code,category,severity,
                  confidence_milli,source_kind,location_kind,location_index,
                  evidence_fingerprint,status,created_at,updated_at
                ) VALUES(?, 'tenant_missing', ?, ?, 'coverage.incomplete','coverage','high',
                         1000,'scan_coverage','document',NULL,?,'detected',?,?)
                """,
                (
                    "fsfd_" + "6" * 32,
                    "fscn_" + "7" * 32,
                    file_id,
                    "hmac-sha256:" + "8" * 64,
                    completed_at,
                    completed_at,
                ),
            )

    # 任一安全事实存在时 down 必须失败关闭，且迁移状态和证据保持原样。
    with pytest.raises(DbError, match="prd073_down_business_facts_present"):
        rollback_last_migration(
            database_url, migrations_dir=prd073_templates["migrations_0065"]
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0065'"
        ).fetchone()[0] == "applied"
        assert connection.execute("SELECT COUNT(*) FROM file_security_scans").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd073_failed_score_finding_limit_and_coverage_contracts(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """异常边界：failed 无安全分数、链路有界、coverage 为 high，且第 257 条 finding 被拒绝。"""

    database_url = _clone(prd073_templates["database_0064"], tmp_path / "limits.db")
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    with connect_database(database_url) as connection:
        tenant_id, user_id, file_id, extract_id, completed_at = _insert_ready_file(
            connection
        )
        scan_id = _insert_failed_scan(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            file_id=file_id,
            extract_id=extract_id,
            attempt_no=1,
            risk_score=None,
        )
        connection.commit()
        failed_row = connection.execute(
            "SELECT risk_level,risk_score,finding_count FROM file_security_scans WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
        assert tuple(failed_row) == ("unknown", None, 0)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_failed_scan(
                connection,
                tenant_id=tenant_id,
                user_id=user_id,
                file_id=file_id,
                extract_id=extract_id,
                attempt_no=2,
                risk_score=0,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_failed_scan(
                connection,
                tenant_id=tenant_id,
                user_id=user_id,
                file_id=file_id,
                extract_id=extract_id,
                attempt_no=2,
                risk_score=None,
                request_id="r" * 129,
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO file_security_findings(
                  finding_id,tenant_id,scan_id,file_id,detector_code,category,severity,
                  confidence_milli,source_kind,location_kind,location_index,
                  evidence_fingerprint,status,created_at,updated_at
                ) VALUES(?,?,?,?,'coverage.incomplete','coverage','low',1000,
                         'scan_coverage','document',NULL,?,'detected',?,?)
                """,
                (
                    "fsfd_" + "a" * 32,
                    tenant_id,
                    "fscn_" + "b" * 32,
                    file_id,
                    "hmac-sha256:" + "e" * 64,
                    completed_at,
                    completed_at,
                ),
            )

        pending_scan_id = "fscn_" + "f" * 32
        for index in range(256):
            connection.execute(
                """
                INSERT INTO file_security_findings(
                  finding_id,tenant_id,scan_id,file_id,detector_code,category,severity,
                  confidence_milli,source_kind,location_kind,location_index,
                  evidence_fingerprint,status,created_at,updated_at
                ) VALUES(?,?,?,?,'ocr.text_observed','ocr','low',900,'docx_image',
                         'image',?,?,'detected',?,?)
                """,
                (
                    f"fsfd_{index:032x}",
                    tenant_id,
                    pending_scan_id,
                    file_id,
                    index + 1,
                    "hmac-sha256:" + f"{index:064x}",
                    completed_at,
                    completed_at,
                ),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="file_security_finding_limit_exceeded",
        ):
            connection.execute(
                """
                INSERT INTO file_security_findings(
                  finding_id,tenant_id,scan_id,file_id,detector_code,category,severity,
                  confidence_milli,source_kind,location_kind,location_index,
                  evidence_fingerprint,status,created_at,updated_at
                ) VALUES(?,?,?,?,'ocr.text_observed','ocr','low',900,'docx_image',
                         'image',257,?,'detected',?,?)
                """,
                (
                    "fsfd_" + "f" * 32,
                    tenant_id,
                    pending_scan_id,
                    file_id,
                    "hmac-sha256:" + "f" * 64,
                    completed_at,
                    completed_at,
                ),
            )
        # 256 条孤立 finding 只用于触发容量门禁，回滚避免 deferred FK 在退出时提交。
        connection.rollback()


def test_prd073_custom_role_is_not_silently_expanded(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """权限流程：升级前定制 Viewer 不得按角色名称静默获得安全扫描命令。"""

    database_url = _clone(prd073_templates["database_0064"], tmp_path / "custom-role.db")
    with connect_database(database_url) as connection:
        tenant_id, role_id, revision = connection.execute(
            "SELECT tenant_id,role_id,permission_revision FROM roles "
            "WHERE code='viewer' ORDER BY tenant_id LIMIT 1"
        ).fetchone()
        relation_id = connection.execute(
            """
            SELECT relation.role_permission_id FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code='file.write'
            """,
            (tenant_id, role_id),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM role_permissions WHERE role_permission_id=?", (relation_id,)
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
            (int(revision) + 1, custom_hash, "2026-08-05T03:00:00Z", role_id),
        )
        connection.commit()

    _apply_0065(database_url, prd073_templates["migrations_0065"])
    with connect_database(database_url) as connection:
        custom_role_row = connection.execute(
            "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?", (role_id,)
        ).fetchone()
        assert tuple(custom_role_row) == (int(revision) + 1, custom_hash)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation
            JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                                       AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code='file.security.scan'
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 0
