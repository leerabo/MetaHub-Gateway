"""PRD-073 0065 安全事实、0066 source outbox 恢复与可逆迁移专项测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import metahub_control.file_security.outbox as security_outbox
import metahub_control.webhooks.service as webhook_service
import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

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
OUTBOX_RECOVERY_TRIGGERS = {
    "trg_file_security_event_outbox_validate_insert_prd073_recovery",
    "trg_file_security_event_outbox_conflict_insert_prd073_recovery",
    "trg_file_security_event_outbox_state_update_prd073_recovery",
    "trg_file_security_event_outbox_delete_prd073_recovery",
    "trg_file_security_event_outbox_rowid_prd073_recovery",
    "trg_file_security_events_create_outbox_prd073_recovery",
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
    """构建含真实 seed 的 0064 模板及分别截止 0065/0066 的迁移目录。"""

    root = tmp_path_factory.mktemp("prd073-migration-templates")
    migrations_0064 = _migration_subset(root / "migrations-0064", 64)
    migrations_0065 = _migration_subset(root / "migrations-0065", 65)
    migrations_0066 = _migration_subset(root / "migrations-0066", 66)
    database_0064 = root / "template-0064.db"
    database_url = _url(database_0064)
    assert apply_migrations(database_url, migrations_dir=migrations_0064).applied[-1] == "0064"
    apply_seed_data(database_url)
    return {
        "database_0064": database_0064,
        "migrations_0065": migrations_0065,
        "migrations_0066": migrations_0066,
    }


def _clone(template: Path, destination: Path) -> str:
    """复制已关闭的 0064 模板并返回用例独占数据库 URL。"""

    shutil.copy2(template, destination)
    return _url(destination)


def _apply_0065(database_url: str, migrations: Path) -> None:
    """只应用 0065，使迁移失败能够直接归因于 PRD-073。"""

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0065"]


def _apply_0066(database_url: str, migrations: Path) -> None:
    """只应用 0066，使 source-outbox 失败能够直接归因于终审修复。"""

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0066"]


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


def _insert_ready_file(
    connection: sqlite3.Connection,
    *,
    file_marker: str = "a",
    extract_marker: str = "b",
) -> tuple[str, str, str, str, str]:
    """插入一个 0064 可验证的 ready TXT owner。

    ``file_marker`` 与 ``extract_marker`` 用于构造测试独占 ID；内容和抽取指纹保持固定，
    便于安全事实测试准确验证 owner 绑定而不复制正文或文件名。
    """

    tenant_id, user_id = connection.execute(
        "SELECT tenant_id,user_id FROM users ORDER BY user_id LIMIT 1"
    ).fetchone()
    assert len(file_marker) == 1 and file_marker in "0123456789abcdef"
    assert len(extract_marker) == 1 and extract_marker in "0123456789abcdef"
    file_id = "file_" + file_marker * 32
    extract_id = "fext_" + extract_marker * 32
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
            f"req_prd073_file_{file_marker}",
            f"trc_prd073_file_{file_marker}",
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
            f"objects/prd073/{file_marker}/original.bin",
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
                 'local_encrypted_v1',?,NULL,?,?)
        """,
        (
            extract_id,
            tenant_id,
            file_id,
            extract_fingerprint,
            f"objects/prd073/{extract_marker}/extract.bin",
            created_at,
            created_at,
        ),
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


def _insert_blocked_scan(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    file_id: str,
    extract_id: str,
    completed_at: str,
    marker: str,
) -> tuple[str, str, str, str]:
    """按 Finding→blocked Scan 顺序写入一组可验证安全事实。

    参数均来自已落库 owner；``marker`` 只生成当前用例内的固定 ID、HMAC 和报告摘要。
    返回扫描 ID、Finding ID、请求 ID和 Trace ID，供 Event 及冲突测试复用。
    """

    assert len(marker) == 1 and marker in "0123456789abcdef"
    scan_id = "fscn_" + marker * 32
    finding_id = "fsfd_" + marker * 32
    request_id = f"req_prd073_replace_{marker}"
    trace_id = f"trc_prd073_replace_{marker}"
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
            "hmac-sha256:" + marker * 64,
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
        ) VALUES(?,?,?,?,?,?,?,1,'blocked','high',90,'complete','blocked',
                 'file-security-detector-v1','not_required',NULL,0,0,1,1,
                 'file-context-boundary-v1',?,?,?,NULL,?,?,?)
        """,
        (
            scan_id,
            tenant_id,
            file_id,
            extract_id,
            user_id,
            request_id,
            trace_id,
            "hmac-sha256:" + "c" * 64,
            "hmac-sha256:" + "d" * 64,
            "sha256:" + marker * 64,
            completed_at,
            completed_at,
            completed_at,
        ),
    )
    return scan_id, finding_id, request_id, trace_id


def _insert_security_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    file_id: str,
    scan_id: str,
    request_id: str,
    trace_id: str,
    occurred_at: str,
    marker: str,
) -> str:
    """为已验证的 blocked Scan 写入一条字段完全一致的安全事件并返回事件 ID。"""

    assert len(marker) == 1 and marker in "0123456789abcdef"
    event_id = "fsev_" + marker * 32
    connection.execute(
        """
        INSERT INTO file_security_events(
          file_security_event_id,tenant_id,scan_id,file_id,event_type,
          actor_user_id,source_request_id,source_trace_id,risk_level,
          finding_count,high_finding_count,model_access_status,
          detector_version,occurred_at
        ) VALUES(?,?,?,?,'file.prompt_injection_detected',?,?,?,'high',1,1,
                 'blocked','file-security-detector-v1',?)
        """,
        (
            event_id,
            tenant_id,
            scan_id,
            file_id,
            user_id,
            request_id,
            trace_id,
            occurred_at,
        ),
    )
    return event_id


def _insert_prompt_source(
    connection: sqlite3.Connection,
    *,
    file_marker: str,
    extract_marker: str,
    event_marker: str,
) -> tuple[str, str, str, str]:
    """写入 ready owner、blocked Scan、Prompt finding 与 source event，返回恢复断言所需身份。"""

    tenant_id, user_id, file_id, extract_id, completed_at = _insert_ready_file(
        connection,
        file_marker=file_marker,
        extract_marker=extract_marker,
    )
    scan_id, _finding_id, request_id, trace_id = _insert_blocked_scan(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        file_id=file_id,
        extract_id=extract_id,
        completed_at=completed_at,
        marker=event_marker,
    )
    event_id = _insert_security_event(
        connection,
        tenant_id=tenant_id,
        user_id=user_id,
        file_id=file_id,
        scan_id=scan_id,
        request_id=request_id,
        trace_id=trace_id,
        occurred_at=completed_at,
        marker=event_marker,
    )
    # 返回固定身份供 lease、tenant 和 pending resume 断言，正文证据不离开数据库构造栈。
    return event_id, tenant_id, request_id, trace_id


def _prompt_event_from_source(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    event_id: str,
) -> WebhookEvent:
    """从 0065 不可变 source 行重建正式七字段 Webhook 事件，不在测试中复制宽松合同。"""

    source = connection.execute(
        """
        SELECT * FROM file_security_events
        WHERE tenant_id=? AND file_security_event_id=?
        """,
        (tenant_id, event_id),
    ).fetchone()
    assert source is not None
    occurred_at = datetime.fromisoformat(str(source["occurred_at"]).replace("Z", "+00:00"))
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type=str(source["event_type"]),
        event_id=event_id,
        timestamp=occurred_at,
        actor_id=str(source["actor_user_id"]),
        object_id=str(source["file_id"]),
        trace_id=str(source["source_trace_id"]),
        source_request_id=str(source["source_request_id"]),
        payload={
            "scan_id": str(source["scan_id"]),
            "file_id": str(source["file_id"]),
            "risk_level": str(source["risk_level"]),
            "finding_count": int(source["finding_count"]),
            "high_finding_count": int(source["high_finding_count"]),
            "model_access_status": str(source["model_access_status"]),
            "detector_version": str(source["detector_version"]),
        },
    )


def _insert_file_security_webhook_endpoint(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    marker: str,
    created_at: str,
) -> str:
    """插入一个同租户 active 文件安全订阅；密钥列只放无业务含义的测试占位值。"""

    endpoint_id = f"whe_prd073_snapshot_{marker}"
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY user_id LIMIT 1",
        (tenant_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,'["file.prompt_injection_detected"]',
                 'enc:v1:prd073-fixture','whsec_test','fpr_prd073_fixture',
                 'active',1000,3,?,?,?,?)
        """,
        (
            endpoint_id,
            tenant_id,
            f"PRD073 snapshot {marker}",
            f"https://example.invalid/prd073/{marker}",
            user_id,
            user_id,
            created_at,
            created_at,
        ),
    )
    return endpoint_id


def _assert_outbox_state_update_rejected(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    set_clause: str,
    values: tuple[object, ...],
) -> None:
    """执行固定测试语句并证明 0066 状态触发器原子拒绝，失败后清理当前事务。"""

    with pytest.raises(
        sqlite3.IntegrityError,
        match="file_security_event_outbox_state_invalid",
    ):
        connection.execute(
            f"UPDATE file_security_event_outbox SET {set_clause} "
            "WHERE file_security_event_id=?",
            (*values, event_id),
        )
    connection.rollback()


def _assert_insert_or_replace_rejected(
    connection: sqlite3.Connection,
    *,
    table: str,
    source_id: str,
    changes: dict[str, object],
    expected_error: str,
    preserve_source_rowid: bool = False,
) -> None:
    """复制一条真实事实执行 ``INSERT OR REPLACE`` 并断言不可变错误。

    ``table`` 仅接受三张固定事实表；``changes`` 用于精确制造某个唯一键冲突。
    ``preserve_source_rowid`` 为真时显式携带旧 rowid，以隔离验证物理行号覆盖门禁。
    """

    identifier_columns = {
        "file_security_scans": "scan_id",
        "file_security_findings": "finding_id",
        "file_security_events": "file_security_event_id",
    }
    identifier_column = identifier_columns[table]
    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    ]
    source = connection.execute(
        f"SELECT rowid AS source_rowid,* FROM {table} WHERE {identifier_column}=?",
        (source_id,),
    ).fetchone()
    assert source is not None
    assert set(changes).issubset(columns)
    payload = {column: source[column] for column in columns}
    payload.update(changes)
    insert_columns = list(columns)
    values = [payload[column] for column in columns]
    if preserve_source_rowid:
        insert_columns.insert(0, "rowid")
        values.insert(0, source["source_rowid"])
    placeholders = ",".join("?" for _ in insert_columns)
    with pytest.raises(sqlite3.IntegrityError, match=expected_error):
        connection.execute(
            f"INSERT OR REPLACE INTO {table}({','.join(insert_columns)}) "
            f"VALUES({placeholders})",
            values,
        )


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


def test_prd073_insert_or_replace_cannot_replace_append_only_facts(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """异常流程：关闭递归触发器后，REPLACE 仍不能覆盖三类 append-only 事实。"""

    database_url = _clone(prd073_templates["database_0064"], tmp_path / "replace.db")
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    with connect_database(database_url) as connection:
        # 生产连接不能假设 recursive_triggers 已开启；复现最弱 SQLite 触发器环境。
        connection.execute("PRAGMA recursive_triggers=OFF")
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0

        tenant_id, user_id, file_id, extract_id, completed_at = _insert_ready_file(
            connection
        )
        scan_id, finding_id, request_id, trace_id = _insert_blocked_scan(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            file_id=file_id,
            extract_id=extract_id,
            completed_at=completed_at,
            marker="1",
        )
        event_id = _insert_security_event(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            file_id=file_id,
            scan_id=scan_id,
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=completed_at,
            marker="2",
        )

        # 第二条 blocked Scan 不创建 Event，使 Event rowid 用例只命中物理行号而非业务唯一键。
        (
            second_tenant_id,
            second_user_id,
            second_file_id,
            second_extract_id,
            second_completed_at,
        ) = _insert_ready_file(connection, file_marker="9", extract_marker="8")
        assert (second_tenant_id, second_user_id) == (tenant_id, user_id)
        second_scan_id, _, second_request_id, second_trace_id = _insert_blocked_scan(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            file_id=second_file_id,
            extract_id=second_extract_id,
            completed_at=second_completed_at,
            marker="6",
        )
        connection.commit()

        original_scan = dict(
            connection.execute(
                "SELECT * FROM file_security_scans WHERE scan_id=?", (scan_id,)
            ).fetchone()
        )
        original_finding = dict(
            connection.execute(
                "SELECT * FROM file_security_findings WHERE finding_id=?", (finding_id,)
            ).fetchone()
        )
        original_event = dict(
            connection.execute(
                "SELECT * FROM file_security_events WHERE file_security_event_id=?",
                (event_id,),
            ).fetchone()
        )

        # scan_id 主键及两个包含 scan_id 的复合 UNIQUE 在表结构上重叠，仍逐项发起覆盖语句。
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_scans",
            source_id=scan_id,
            changes={},
            expected_error="file_security_scan_immutable",
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_scans",
            source_id=scan_id,
            changes={"report_sha256": "sha256:" + "7" * 64},
            expected_error="file_security_scan_immutable",
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_scans",
            source_id=scan_id,
            changes={"report_sha256": "sha256:" + "8" * 64},
            expected_error="file_security_scan_immutable",
        )

        # 新 scan_id 仅复用 attempt 唯一键；failed 终态不需要 Finding，避免其他键遮蔽目标。
        failed_scan = {
            "scan_id": "fscn_" + "a" * 32,
            "status": "failed",
            "risk_level": "unknown",
            "risk_score": None,
            "coverage_status": "unavailable",
            "model_access_status": "blocked",
            "ocr_status": "failed",
            "ocr_engine_version": None,
            "scanned_page_count": 0,
            "scanned_image_count": 0,
            "finding_count": 0,
            "high_finding_count": 0,
            "report_sha256": "sha256:" + "a" * 64,
            "failure_error_code": "file.security_scan_unavailable",
        }
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_scans",
            source_id=scan_id,
            changes=failed_scan,
            expected_error="file_security_scan_immutable",
        )

        # passed attempt_no=2 不碰 attempt 唯一键，只命中 passed/blocked 权威部分唯一键。
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_scans",
            source_id=scan_id,
            changes={
                "scan_id": "fscn_" + "b" * 32,
                "attempt_no": 2,
                "status": "passed",
                "risk_level": "none",
                "risk_score": 0,
                "coverage_status": "complete",
                "model_access_status": "allowed",
                "ocr_status": "not_required",
                "ocr_engine_version": None,
                "scanned_page_count": 0,
                "scanned_image_count": 0,
                "finding_count": 0,
                "high_finding_count": 0,
                "report_sha256": "sha256:" + "b" * 64,
                "failure_error_code": None,
            },
            expected_error="file_security_scan_immutable",
        )

        rowid_scan = dict(failed_scan)
        rowid_scan.update(
            {
                "scan_id": "fscn_" + "c" * 32,
                "attempt_no": 2,
                "report_sha256": "sha256:" + "c" * 64,
            }
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_scans",
            source_id=scan_id,
            changes=rowid_scan,
            expected_error="file_security_scan_immutable",
            preserve_source_rowid=True,
        )

        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_findings",
            source_id=finding_id,
            changes={},
            expected_error="file_security_finding_immutable",
        )
        # location_index=NULL 必须按 IS 视为相等；SQLite 原生 UNIQUE 本身不会拒绝两个 NULL。
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_findings",
            source_id=finding_id,
            changes={"finding_id": "fsfd_" + "3" * 32},
            expected_error="file_security_finding_immutable",
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_findings",
            source_id=finding_id,
            changes={
                "finding_id": "fsfd_" + "4" * 32,
                "evidence_fingerprint": "hmac-sha256:" + "4" * 64,
            },
            expected_error="file_security_finding_immutable",
            preserve_source_rowid=True,
        )

        # Event 主键与 tenant 复合主键重叠，也分别验证；第三条只命中 scan 事件幂等键。
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_events",
            source_id=event_id,
            changes={},
            expected_error="file_security_event_immutable",
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_events",
            source_id=event_id,
            changes={"occurred_at": completed_at},
            expected_error="file_security_event_immutable",
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_events",
            source_id=event_id,
            changes={"file_security_event_id": "fsev_" + "3" * 32},
            expected_error="file_security_event_immutable",
        )
        _assert_insert_or_replace_rejected(
            connection,
            table="file_security_events",
            source_id=event_id,
            changes={
                "file_security_event_id": "fsev_" + "4" * 32,
                "scan_id": second_scan_id,
                "file_id": second_file_id,
                "source_request_id": second_request_id,
                "source_trace_id": second_trace_id,
                "occurred_at": second_completed_at,
            },
            expected_error="file_security_event_immutable",
            preserve_source_rowid=True,
        )

        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM file_security_scans"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM file_security_findings"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM file_security_events"
        ).fetchone()[0] == 1
        assert dict(
            connection.execute(
                "SELECT * FROM file_security_scans WHERE scan_id=?", (scan_id,)
            ).fetchone()
        ) == original_scan
        assert dict(
            connection.execute(
                "SELECT * FROM file_security_findings WHERE finding_id=?", (finding_id,)
            ).fetchone()
        ) == original_finding
        assert dict(
            connection.execute(
                "SELECT * FROM file_security_events WHERE file_security_event_id=?",
                (event_id,),
            ).fetchone()
        ) == original_event
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
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


def test_prd073_outbox_0066_empty_roundtrip(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """迁移主流程：空事实库 0065→0066→0065→0066 可逆且保持完整性。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-empty-roundtrip.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('file_security_event_outbox')"
            ).fetchall()
        }
        assert len(columns) == 13
        assert "delivery_snapshot_frozen_at" in columns
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        assert OUTBOX_RECOVERY_TRIGGERS <= triggers
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name=?",
                ("file_security_event_outbox",),
            ).fetchall()
        }
        assert {
            "idx_file_security_event_outbox_due_prd073_recovery",
            "idx_file_security_event_outbox_tenant_status_prd073_recovery",
        } <= indexes
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0066'"
        ).fetchone()[0] == "applied"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert rollback_last_migration(
        database_url,
        migrations_dir=prd073_templates["migrations_0066"],
    ).rolled_back == "0066"
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='file_security_event_outbox'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0066'"
        ).fetchone()[0] == "rolled_back"
        remaining_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        assert OUTBOX_RECOVERY_TRIGGERS.isdisjoint(remaining_triggers)
        assert len(connection.execute("PRAGMA table_info('file_security_events')").fetchall()) == 14
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM file_security_event_outbox"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0066'"
        ).fetchone()[0] == "applied"
        restored_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        assert OUTBOX_RECOVERY_TRIGGERS <= restored_triggers
        assert "delivery_snapshot_frozen_at" in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('file_security_event_outbox')"
            ).fetchall()
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd073_outbox_0066_backfill_trigger_guards_and_down_refusal(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """迁移异常流程：存量回填、新事件同事务建 intent，且事实存在时拒绝删除或降级。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-backfill.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    with connect_database(database_url) as connection:
        first_event, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="1",
            extract_marker="2",
            event_marker="3",
        )
        connection.commit()

    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        backfilled = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (first_event,),
        ).fetchone()
        assert backfilled is not None
        assert (
            backfilled["dispatch_status"],
            backfilled["dispatch_attempts"],
            backfilled["next_attempt_at"],
            backfilled["created_at"],
        ) == ("pending", 0, "2026-08-05T02:00:00Z", "2026-08-05T02:00:00Z")

        second_event, _tenant, _request, _trace = _insert_prompt_source(
            connection,
            file_marker="4",
            extract_marker="5",
            event_marker="6",
        )
        connection.commit()
        assert connection.execute(
            "SELECT COUNT(*) FROM file_security_event_outbox WHERE file_security_event_id=?",
            (second_event,),
        ).fetchone()[0] == 1

        with pytest.raises(sqlite3.IntegrityError, match="file_security_event_outbox_invalid"):
            connection.execute(
                """
                INSERT INTO file_security_event_outbox(
                  file_security_event_id,tenant_id,dispatch_status,dispatch_attempts,
                  next_attempt_at,dispatched_at,claim_token,lease_owner_id,
                  lease_expires_at,last_error_code,created_at,updated_at
                ) VALUES(?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
                """,
                (
                    "fsev_" + "a" * 32,
                    tenant_id,
                    "2026-08-05T02:00:00Z",
                    "2026-08-05T02:00:00Z",
                    "2026-08-05T02:00:00Z",
                ),
            )
        connection.rollback()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="file_security_event_outbox_state_invalid",
        ):
            connection.execute(
                "UPDATE file_security_event_outbox SET tenant_id=? WHERE file_security_event_id=?",
                ("tenant_prd073_cross_owner", first_event),
            )
        connection.rollback()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="file_security_event_outbox_immutable",
        ):
            connection.execute(
                "DELETE FROM file_security_event_outbox WHERE file_security_event_id=?",
                (first_event,),
            )
        connection.rollback()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            migrations_dir=prd073_templates["migrations_0066"],
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0066'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT COUNT(*) FROM file_security_event_outbox"
        ).fetchone()[0] == 2


def test_prd073_outbox_lease_renewal_state_guards_and_empty_snapshot(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """租约与空快照：合法续租延长 90 秒，非法身份/时间/marker 更新全部失败关闭。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-lease-state-guards.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        event_id, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="1",
            extract_marker="2",
            event_marker="3",
        )
        event = _prompt_event_from_source(
            connection,
            tenant_id=tenant_id,
            event_id=event_id,
        )
        connection.commit()

    config = SimpleNamespace(database_url=database_url)
    due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    claim = security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-lease-owner",
        now=due,
    )
    assert claim is not None
    with connect_database(database_url) as connection:
        claimed = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
    claimed_at = datetime.fromisoformat(str(claimed["updated_at"]).replace("Z", "+00:00"))
    original_expiry = datetime.fromisoformat(
        str(claimed["lease_expires_at"]).replace("Z", "+00:00")
    )
    assert original_expiry - claimed_at == timedelta(seconds=90)
    assert claim.lease_expires_at == original_expiry

    renewed_at = claimed_at + timedelta(seconds=30)
    renewed_expiry = security_outbox._renew_source_lease(
        config,
        claim,
        expected_lease_expires_at=original_expiry,
        current=renewed_at,
    )
    with connect_database(database_url) as connection:
        renewed = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
    stored_renewed_expiry = datetime.fromisoformat(
        str(renewed["lease_expires_at"]).replace("Z", "+00:00")
    )
    assert renewed["updated_at"] == security_outbox._db_time(renewed_at)
    assert stored_renewed_expiry == renewed_expiry
    assert renewed_expiry - renewed_at == timedelta(seconds=90)
    assert renewed_expiry > original_expiry
    assert security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-too-early-reclaimer",
        now=original_expiry,
    ) is None

    # marker 尚为空时，变更 owner 的伪冻结即使时间和 90 秒 lease 合法也必须被触发器拒绝。
    forged_at = renewed_at + timedelta(seconds=1)
    with connect_database(database_url) as connection:
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause=(
                "lease_owner_id=?,delivery_snapshot_frozen_at=?,"
                "lease_expires_at=?,updated_at=?"
            ),
            values=(
                "prd073-not-owner",
                security_outbox._db_time(forged_at),
                security_outbox._db_time(forged_at + timedelta(seconds=90)),
                security_outbox._db_time(forged_at),
            ),
        )

    # 当时没有订阅者也必须冻结空集合；marker 形成后新增 Endpoint 不得追溯旧 source。
    frozen_at = renewed_at + timedelta(seconds=10)
    frozen_expiry = security_outbox._freeze_delivery_snapshot(
        config,
        claim,
        event,
        expected_lease_expires_at=renewed_expiry,
        current=frozen_at,
    )
    with connect_database(database_url) as connection:
        frozen = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE tenant_id=? AND event_id=?",
            (tenant_id, event_id),
        ).fetchone()[0] == 0
        late_endpoint = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="empty-late",
            created_at=security_outbox._db_time(frozen_at + timedelta(seconds=1)),
        )
        connection.commit()
    stored_frozen_expiry = datetime.fromisoformat(
        str(frozen["lease_expires_at"]).replace("Z", "+00:00")
    )
    assert frozen["delivery_snapshot_frozen_at"] == security_outbox._db_time(frozen_at)
    assert stored_frozen_expiry == frozen_expiry
    assert frozen_expiry - frozen_at == timedelta(seconds=90)
    security_outbox._freeze_delivery_snapshot(
        config,
        claim,
        event,
        expected_lease_expires_at=frozen_expiry,
        current=frozen_at + timedelta(seconds=1),
    )
    with connect_database(database_url) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND webhook_endpoint_id=?
            """,
            (tenant_id, event_id, late_endpoint),
        ).fetchone()[0] == 0
        state = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
        state_updated = datetime.fromisoformat(
            str(state["updated_at"]).replace("Z", "+00:00")
        )
        state_expiry = datetime.fromisoformat(
            str(state["lease_expires_at"]).replace("Z", "+00:00")
        )
        later = state_updated + timedelta(seconds=1)

        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="claim_token=?",
            values=("f" * 32,),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="lease_owner_id=?",
            values=("prd073-other-owner",),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="dispatch_attempts=?",
            values=(2,),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="tenant_id=?",
            values=("tenant_prd073_cross_owner",),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="lease_expires_at=?",
            values=(security_outbox._db_time(state_expiry - timedelta(seconds=1)),),
        )
        for lease_seconds in (89, 91):
            _assert_outbox_state_update_rejected(
                connection,
                event_id=event_id,
                set_clause="lease_expires_at=?,updated_at=?",
                values=(
                    security_outbox._db_time(later + timedelta(seconds=lease_seconds)),
                    security_outbox._db_time(later),
                ),
            )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="lease_expires_at=?,updated_at=?",
            values=(
                security_outbox._db_time(state_expiry + timedelta(seconds=90)),
                security_outbox._db_time(state_expiry),
            ),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause=(
                "delivery_snapshot_frozen_at=?,lease_expires_at=?,updated_at=?"
            ),
            values=(
                security_outbox._db_time(later),
                security_outbox._db_time(later + timedelta(seconds=90)),
                security_outbox._db_time(later),
            ),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause="delivery_snapshot_frozen_at=NULL",
            values=(),
        )
        _assert_outbox_state_update_rejected(
            connection,
            event_id=event_id,
            set_clause=(
                "dispatch_status='dispatched',next_attempt_at=NULL,dispatched_at=?,"
                "claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,"
                "delivery_snapshot_frozen_at=NULL,last_error_code=NULL,updated_at=?"
            ),
            values=(security_outbox._db_time(later), security_outbox._db_time(later)),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with pytest.raises(ApiError) as expired_renewal:
        security_outbox._renew_source_lease(
            config,
            claim,
            expected_lease_expires_at=frozen_expiry,
            current=frozen_expiry,
        )
    assert expired_renewal.value.code == "file.security_scan_unavailable"


def test_prd073_outbox_legacy_partial_delivery_snapshot_is_completed_once(
    prd073_templates: dict[str, Path], tmp_path: Path
) -> None:
    """升级兼容：0065 已投递子集原样保留，缺失订阅补齐后冻结且不吸收后来 Endpoint。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-legacy-partial-snapshot.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    legacy_delivery_id = "whd_" + "a" * 32
    legacy_created_at = "2026-08-05T01:30:00Z"
    with connect_database(database_url) as connection:
        event_id, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="4",
            extract_marker="5",
            event_marker="6",
        )
        first_endpoint = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="legacy-first",
            created_at="2026-08-05T01:00:00Z",
        )
        second_endpoint = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="legacy-second",
            created_at="2026-08-05T01:01:00Z",
        )
        event = _prompt_event_from_source(
            connection,
            tenant_id=tenant_id,
            event_id=event_id,
        )
        canonical_payload = webhook_service._json_dumps(event.to_payload())
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,
              delivery_kind,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'pending',1,3,'original',1,?,?)
            """,
            (
                legacy_delivery_id,
                tenant_id,
                first_endpoint,
                event_id,
                event.event_type,
                event.timestamp,
                canonical_payload,
                legacy_created_at,
                legacy_created_at,
            ),
        )
        connection.commit()

    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        assert connection.execute(
            """
            SELECT delivery_snapshot_frozen_at FROM file_security_event_outbox
            WHERE tenant_id=? AND file_security_event_id=?
            """,
            (tenant_id, event_id),
        ).fetchone()[0] is None

    config = SimpleNamespace(database_url=database_url)
    claim = security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-legacy-freezer",
        now=datetime(2026, 8, 5, 2, tzinfo=UTC),
    )
    assert claim is not None
    frozen_at = claim.lease_expires_at - timedelta(seconds=60)
    frozen_expiry = security_outbox._freeze_delivery_snapshot(
        config,
        claim,
        event,
        expected_lease_expires_at=claim.lease_expires_at,
        current=frozen_at,
    )
    with connect_database(database_url) as connection:
        deliveries = connection.execute(
            """
            SELECT * FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            ORDER BY webhook_endpoint_id
            """,
            (tenant_id, event_id),
        ).fetchall()
        assert len(deliveries) == 2
        by_endpoint = {row["webhook_endpoint_id"]: row for row in deliveries}
        legacy = by_endpoint[first_endpoint]
        assert (
            legacy["webhook_delivery_id"],
            legacy["status"],
            legacy["attempt_count"],
            legacy["created_at"],
            legacy["updated_at"],
        ) == (legacy_delivery_id, "pending", 1, legacy_created_at, legacy_created_at)
        completed = by_endpoint[second_endpoint]
        assert completed["status"] == "pending"
        assert completed["attempt_count"] == 0
        assert completed["webhook_delivery_id"] != legacy_delivery_id
        assert all(row["tenant_id"] == tenant_id for row in deliveries)
        assert all(row["event_id"] == event_id for row in deliveries)
        assert all(row["event_type"] == event.event_type for row in deliveries)
        assert all(row["event_timestamp"] == event.timestamp for row in deliveries)
        assert all(row["payload_json"] == canonical_payload for row in deliveries)
        outbox = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
        marker = datetime.fromisoformat(
            str(outbox["delivery_snapshot_frozen_at"]).replace("Z", "+00:00")
        )
        lease_expiry = datetime.fromisoformat(
            str(outbox["lease_expires_at"]).replace("Z", "+00:00")
        )
        assert marker == frozen_at
        assert lease_expiry - marker == timedelta(seconds=90)
        third_endpoint = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="legacy-late",
            created_at=security_outbox._db_time(frozen_at + timedelta(seconds=1)),
        )
        connection.commit()

    security_outbox._freeze_delivery_snapshot(
        config,
        claim,
        event,
        expected_lease_expires_at=frozen_expiry,
        current=frozen_at + timedelta(seconds=1),
    )
    with connect_database(database_url) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            """,
            (tenant_id, event_id),
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND webhook_endpoint_id=?
            """,
            (tenant_id, event_id, third_endpoint),
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT delivery_snapshot_frozen_at FROM file_security_event_outbox
            WHERE file_security_event_id=?
            """,
            (event_id,),
        ).fetchone()[0] == security_outbox._db_time(frozen_at)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd073_outbox_crash_recovery_stale_worker_and_pending_body(
    prd073_templates: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """崩溃恢复：首次 task 不运行仍可领取，过期接管后旧 worker 零出网且不能回写。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-crash-recovery.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        event_id, tenant_id, source_request_id, source_trace_id = _insert_prompt_source(
            connection,
            file_marker="7",
            extract_marker="8",
            event_marker="9",
        )
        connection.commit()

    config = SimpleNamespace(database_url=database_url)
    due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    first = security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-crashed-worker",
        now=due,
    )
    assert first is not None and first.dispatch_attempts == 1
    assert first.event_id == event_id
    assert security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-concurrent-worker",
        now=first.lease_expires_at - timedelta(seconds=1),
    ) is None

    recovered = security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-restarted-worker",
        now=first.lease_expires_at,
    )
    assert recovered is not None
    assert recovered.event_id == event_id
    assert recovered.dispatch_attempts == 2
    captured: list[tuple[str, object, dict[str, object]]] = []

    def capture_dispatch(
        _config,
        outbound_tenant: str,
        event,
        **kwargs: object,
    ) -> list[object]:
        """捕获唯一恢复出网，验证稳定事件 ID 和显式 pending resume。"""

        captured.append((outbound_tenant, event, kwargs))
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_dispatch)
    assert security_outbox.dispatch_claimed_file_security_event(
        config,
        first,
        now=first.lease_expires_at + timedelta(seconds=1),
    ) is False
    assert captured == []
    assert security_outbox.dispatch_claimed_file_security_event(
        config,
        recovered,
        now=recovered.lease_expires_at - timedelta(seconds=29),
    ) is True
    assert len(captured) == 1
    outbound_tenant, event, kwargs = captured[0]
    assert outbound_tenant == tenant_id
    assert kwargs["resume_existing_pending"] is True
    assert kwargs["create_snapshot_if_missing"] is False
    assert event.event_id == event_id
    assert event.trace_id == source_trace_id
    assert event.payload == {
        "scan_id": recovered.scan_id,
        "file_id": recovered.file_id,
        "risk_level": "high",
        "finding_count": 1,
        "high_finding_count": 1,
        "model_access_status": "blocked",
        "detector_version": "file-security-detector-v1",
    }

    with connect_database(database_url) as connection:
        state = connection.execute(
            """
            SELECT dispatch_status,dispatch_attempts,last_error_code
            FROM file_security_event_outbox WHERE file_security_event_id=?
            """,
            (event_id,),
        ).fetchone()
        pending_delivery = connection.execute(
            """
            SELECT ? AS payload_json,? AS tenant_id,? AS event_id,
                   ? AS event_type,? AS event_timestamp
            """,
            (
                json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True),
                tenant_id,
                event.event_id,
                event.event_type,
                event.timestamp,
            ),
        ).fetchone()
        resumed = webhook_service._validated_pending_delivery_body(
            connection,
            pending_delivery,
        )
        stored_request_id = connection.execute(
            """
            SELECT source_request_id FROM file_security_events
            WHERE tenant_id=? AND file_security_event_id=?
            """,
            (tenant_id, event_id),
        ).fetchone()[0]
    assert tuple(state) == ("dispatched", 2, None)
    assert stored_request_id == source_request_id
    assert json.loads(resumed) == event.to_payload()
    assert b"source_request_id" not in resumed


def test_prd073_transient_claim_error_rolls_back_and_lone_quarantine_progresses(
    prd073_templates: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """瞬时 claim 错误必须回滚；孤立坏 source 被隔离时 drain 仍报告已处理。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-transient-and-lone-quarantine.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        event_id, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="7",
            extract_marker="8",
            event_marker="9",
        )
        connection.commit()

    config = SimpleNamespace(database_url=database_url)
    due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    original_claim_builder = security_outbox._claim_from_row

    def raise_transient_sqlite_error(*_args: object, **_kwargs: object) -> object:
        """在 processing UPDATE 后模拟连接瞬断，正文只作不应泄漏的测试 canary。"""

        # 这个异常只模拟瞬时数据库故障；生产边界必须回滚 claim，不能把它当作坏 source 隔离。
        raise sqlite3.OperationalError("prd073-sensitive-transient-canary")

    monkeypatch.setattr(
        security_outbox,
        "_claim_from_row",
        raise_transient_sqlite_error,
    )
    with pytest.raises(ApiError) as transient_error:
        security_outbox.claim_next_file_security_event(
            config,
            worker_id="prd073-transient-claim-owner",
            now=due,
        )
    assert transient_error.value.code == "file.security_scan_unavailable"
    assert "sensitive-transient-canary" not in str(transient_error.value)
    with connect_database(database_url) as connection:
        rolled_back = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
    assert rolled_back["dispatch_status"] == "pending"
    assert rolled_back["dispatch_attempts"] == 0
    assert rolled_back["claim_token"] is None
    assert rolled_back["lease_owner_id"] is None
    assert rolled_back["lease_expires_at"] is None

    monkeypatch.setattr(security_outbox, "_claim_from_row", original_claim_builder)
    with connect_database(database_url) as connection:
        # 只在测试库模拟介质损坏；生产事实 trigger 会拒绝这类不可变字段漂移。
        connection.execute("DROP TRIGGER trg_file_security_events_update_prd073")
        connection.execute(
            """
            UPDATE file_security_events SET source_trace_id='trc_prd073_lone_corrupt'
            WHERE tenant_id=? AND file_security_event_id=?
            """,
            (tenant_id, event_id),
        )
        connection.commit()

    assert security_outbox.drain_file_security_event_once(
        config,
        worker_id="prd073-lone-quarantine-owner",
        now=due,
    ) is True
    # 隔离动作已经消费唯一队首；下一轮才是真正空队列。
    assert security_outbox.drain_file_security_event_once(
        config,
        worker_id="prd073-after-lone-quarantine",
        now=due,
    ) is False
    with connect_database(database_url) as connection:
        quarantined = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE tenant_id=? AND event_id=?",
            (tenant_id, event_id),
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert quarantined["dispatch_status"] == "failed"
    assert quarantined["dispatch_attempts"] == 1
    assert quarantined["last_error_code"] == "webhook.payload_invalid"
    assert quarantined["claim_token"] is None
    assert quarantined["lease_owner_id"] is None
    assert quarantined["lease_expires_at"] is None


@pytest.mark.parametrize("terminal_attempt", range(1, 11))
def test_prd073_payload_invalid_terminates_at_real_attempt_with_consistent_delivery(
    prd073_templates: dict[str, Path],
    tmp_path: Path,
    terminal_attempt: int,
) -> None:
    """attempt 1..10 的确定性 payload 损坏均立即终结，且 source/delivery 归因一致。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / f"outbox-payload-invalid-attempt-{terminal_attempt}.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        event_id, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="a",
            extract_marker="b",
            event_marker="c",
        )
        endpoint_id = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker=f"payload-invalid-{terminal_attempt}",
            created_at="2026-08-05T02:00:00Z",
        )
        event = _prompt_event_from_source(
            connection,
            tenant_id=tenant_id,
            event_id=event_id,
        )
        connection.commit()

        if terminal_attempt == 1:
            # CHECK 允许 failed(payload_invalid, attempt=1)，但状态 trigger 仍必须禁止
            # pending(0) 绕过真实 owner 直接跳终态。
            _assert_outbox_state_update_rejected(
                connection,
                event_id=event_id,
                set_clause=(
                    "dispatch_status='failed',dispatch_attempts=1,next_attempt_at=NULL,"
                    "last_error_code='webhook.payload_invalid',updated_at=?"
                ),
                values=("2026-08-05T02:00:01Z",),
            )

    config = SimpleNamespace(database_url=database_url)
    due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    for expected_attempt in range(1, terminal_attempt + 1):
        claim = security_outbox.claim_next_file_security_event(
            config,
            worker_id=f"prd073-payload-invalid-{terminal_attempt}-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.event_id == event_id
        assert claim.dispatch_attempts == expected_attempt
        active_expiry = claim.lease_expires_at
        if expected_attempt == 1:
            # 冻结一个真实 original intent，使终态错误码必须同步进入 delivery 和死信。
            active_expiry = security_outbox._freeze_delivery_snapshot(
                config,
                claim,
                event,
                expected_lease_expires_at=claim.lease_expires_at,
                current=claim.lease_expires_at - timedelta(seconds=30),
            )
        completed_at = active_expiry - timedelta(seconds=29)
        if expected_attempt < terminal_attempt:
            assert security_outbox._finish_failure(
                config,
                claim,
                active_expiry,
                completed_at,
                "webhook.delivery_unavailable",
            ) is True
            with connect_database(database_url) as connection:
                retry = connection.execute(
                    "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
                    (event_id,),
                ).fetchone()
                # pending 状态只允许瞬时 delivery_unavailable；确定性 payload 错误不能等待重试。
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        UPDATE file_security_event_outbox
                        SET last_error_code='webhook.payload_invalid'
                        WHERE file_security_event_id=?
                        """,
                        (event_id,),
                    )
                connection.rollback()
            assert retry["dispatch_status"] == "pending"
            assert retry["dispatch_attempts"] == expected_attempt
            assert retry["last_error_code"] == "webhook.delivery_unavailable"
            due = datetime.fromisoformat(str(retry["next_attempt_at"]).replace("Z", "+00:00"))
            continue

        assert security_outbox._finish_failure(
            config,
            claim,
            active_expiry,
            completed_at,
            "webhook.payload_invalid",
        ) is True

    with connect_database(database_url) as connection:
        terminal = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (event_id,),
        ).fetchone()
        delivery = connection.execute(
            """
            SELECT status,error_code,error_summary,next_retry_at
            FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND webhook_endpoint_id=?
            """,
            (tenant_id, event_id, endpoint_id),
        ).fetchone()
        dead_letter = connection.execute(
            """
            SELECT failure_error_code,status
            FROM webhook_dead_letter_events
            WHERE tenant_id=? AND event_id=? AND webhook_endpoint_id=?
            """,
            (tenant_id, event_id, endpoint_id),
        ).fetchone()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert terminal["dispatch_status"] == "failed"
    assert terminal["dispatch_attempts"] == terminal_attempt
    assert terminal["last_error_code"] == "webhook.payload_invalid"
    assert terminal["next_attempt_at"] is None
    assert terminal["claim_token"] is None
    assert terminal["lease_owner_id"] is None
    assert terminal["lease_expires_at"] is None
    assert tuple(delivery) == (
        "failed",
        "webhook.payload_invalid",
        "Source payload validation failed.",
        None,
    )
    assert tuple(dead_letter) == (
        "webhook.payload_invalid",
        "dead_letter",
    )


def test_prd073_invalid_source_is_quarantined_without_blocking_later_event(
    prd073_templates: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """损坏队首 source 必须固定失败关闭，同一事务继续领取后续合法事件。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-invalid-source-quarantine.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        invalid_event, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="1",
            extract_marker="2",
            event_marker="1",
        )
        valid_event, valid_tenant, _valid_request, _valid_trace = _insert_prompt_source(
            connection,
            file_marker="3",
            extract_marker="4",
            event_marker="2",
        )
        assert valid_tenant == tenant_id
        endpoint_id = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="quarantine",
            created_at="2026-08-05T02:00:00Z",
        )
        invalid_webhook_event = _prompt_event_from_source(
            connection,
            tenant_id=tenant_id,
            event_id=invalid_event,
        )
        connection.commit()

    config = SimpleNamespace(database_url=database_url)
    due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    invalid_claim = security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-invalid-before-corruption",
        now=due,
    )
    assert invalid_claim is not None and invalid_claim.event_id == invalid_event
    invalid_due = security_outbox._freeze_delivery_snapshot(
        config,
        invalid_claim,
        invalid_webhook_event,
        expected_lease_expires_at=invalid_claim.lease_expires_at,
        current=invalid_claim.lease_expires_at - timedelta(seconds=30),
    )
    with connect_database(database_url) as connection:
        # 只在测试库模拟介质损坏；生产 trigger 正常禁止修改不可变 source。
        connection.execute("DROP TRIGGER trg_file_security_events_update_prd073")
        connection.execute(
            """
            UPDATE file_security_events SET source_trace_id='trc_prd073_corrupted_source'
            WHERE tenant_id=? AND file_security_event_id=?
            """,
            (tenant_id, invalid_event),
        )
        connection.commit()

    claim = security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-valid-after-quarantine",
        now=invalid_due,
    )
    assert claim is not None
    assert claim.event_id == valid_event
    assert claim.dispatch_attempts == 1
    with connect_database(database_url) as connection:
        invalid_state = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (invalid_event,),
        ).fetchone()
        valid_state = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (valid_event,),
        ).fetchone()
        invalid_delivery = connection.execute(
            """
            SELECT status,error_code,error_summary,next_retry_at
            FROM webhook_deliveries WHERE tenant_id=? AND event_id=?
            """,
            (tenant_id, invalid_event),
        ).fetchone()
        invalid_dead_letters = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_dead_letter_events
            WHERE tenant_id=? AND event_id=? AND status='dead_letter'
            """,
            (tenant_id, invalid_event),
        ).fetchone()[0]
    assert invalid_state["dispatch_status"] == "failed"
    # 首次 worker 已占用 attempt 1；损坏后由第二次真实接管 owner 在 attempt 2 隔离。
    assert invalid_state["dispatch_attempts"] == 2
    assert invalid_state["last_error_code"] == "webhook.payload_invalid"
    assert invalid_state["claim_token"] is None
    assert invalid_state["lease_owner_id"] is None
    assert invalid_state["lease_expires_at"] is None
    assert tuple(invalid_delivery) == (
        "failed",
        "webhook.payload_invalid",
        "Source payload validation failed.",
        None,
    )
    assert invalid_dead_letters == 1
    assert valid_state["dispatch_status"] == "processing"
    assert valid_state["dispatch_attempts"] == 1

    def successful_empty_dispatch(*_args: object, **_kwargs: object) -> list[object]:
        """合法后续事件使用空订阅快照完成，证明损坏队首没有形成全局阻塞。"""

        return []

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        successful_empty_dispatch,
    )
    with connect_database(database_url) as connection:
        # 后续合法 source 使用空快照完成，避免测试桩留下未发送的 delivery。
        connection.execute(
            "UPDATE webhook_endpoints SET status='disabled' WHERE webhook_endpoint_id=?",
            (endpoint_id,),
        )
        connection.commit()
    assert security_outbox.dispatch_claimed_file_security_event(
        config,
        claim,
        now=claim.lease_expires_at - timedelta(seconds=29),
    ) is True
    with connect_database(database_url) as connection:
        states = {
            str(row["file_security_event_id"]): str(row["dispatch_status"])
            for row in connection.execute(
                """
                SELECT file_security_event_id,dispatch_status
                FROM file_security_event_outbox ORDER BY file_security_event_id
                """
            ).fetchall()
        }
        assert states == {invalid_event: "failed", valid_event: "dispatched"}
        assert connection.execute(
            """
            SELECT COUNT(*) FROM file_security_event_outbox
            WHERE dispatch_status IN ('pending','processing')
            """
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_prd073_outbox_backoff_terminal_and_tenth_orphan_reaper(
    prd073_templates: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：失败分级退避并在十次终结；第十次崩溃也不得产生第十一次出网。"""

    database_url = _clone(
        prd073_templates["database_0064"],
        tmp_path / "outbox-terminal.db",
    )
    _apply_0065(database_url, prd073_templates["migrations_0065"])
    _apply_0066(database_url, prd073_templates["migrations_0066"])
    with connect_database(database_url) as connection:
        event_id, tenant_id, _request_id, _trace_id = _insert_prompt_source(
            connection,
            file_marker="a",
            extract_marker="b",
            event_marker="c",
        )
        successful_endpoint = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="terminal-success",
            created_at="2026-08-05T02:00:00Z",
        )
        pending_endpoint = _insert_file_security_webhook_endpoint(
            connection,
            tenant_id=tenant_id,
            marker="terminal-pending",
            created_at="2026-08-05T02:00:00Z",
        )
        connection.commit()

    config = SimpleNamespace(database_url=database_url)

    def failed_dispatch(*_args: object, **_kwargs: object) -> list[object]:
        """模拟公共 dispatcher 的固定领域失败，不携带 Endpoint 或远端正文。"""

        # 只抛冻结错误码，证明退避状态不会复制自由异常或目标 URL。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", failed_dispatch)
    due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    snapshot_marker: str | None = None
    for expected_attempt in range(1, 11):
        claim = security_outbox.claim_next_file_security_event(
            config,
            worker_id=f"prd073-retry-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.event_id == event_id
        assert claim.dispatch_attempts == expected_attempt
        if snapshot_marker is not None:
            with connect_database(database_url) as connection:
                assert connection.execute(
                    """
                    SELECT delivery_snapshot_frozen_at
                    FROM file_security_event_outbox WHERE file_security_event_id=?
                    """,
                    (event_id,),
                ).fetchone()[0] == snapshot_marker
        completed_at = claim.lease_expires_at - timedelta(seconds=29)
        assert security_outbox.dispatch_claimed_file_security_event(
            config,
            claim,
            now=completed_at,
        ) is True
        with connect_database(database_url) as connection:
            state = connection.execute(
                "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
                (event_id,),
            ).fetchone()
        if snapshot_marker is None:
            snapshot_marker = str(state["delivery_snapshot_frozen_at"])
            assert snapshot_marker not in {"", "None"}
        else:
            assert state["delivery_snapshot_frozen_at"] == snapshot_marker
        if expected_attempt < 10:
            if expected_attempt == 1:
                with connect_database(database_url) as connection:
                    endpoint = connection.execute(
                        "SELECT * FROM webhook_endpoints WHERE webhook_endpoint_id=?",
                        (successful_endpoint,),
                    ).fetchone()
                    delivery_id = connection.execute(
                        """
                        SELECT webhook_delivery_id FROM webhook_deliveries
                        WHERE tenant_id=? AND event_id=? AND webhook_endpoint_id=?
                        """,
                        (tenant_id, event_id, successful_endpoint),
                    ).fetchone()[0]
                    success_result = {
                        "status": "success",
                        "http_status": 204,
                        "duration_ms": 1,
                        "error_code": None,
                        "error_summary": None,
                    }
                    webhook_service._record_attempt(
                        connection,
                        endpoint,
                        delivery_id,
                        1,
                        success_result,
                    )
                    webhook_service._update_delivery_after_attempt(
                        connection,
                        endpoint,
                        delivery_id,
                        1,
                        success_result,
                        final=True,
                    )
            expected_delay = security_outbox.EVENT_BACKOFF_SECONDS[
                min(
                    expected_attempt - 1,
                    len(security_outbox.EVENT_BACKOFF_SECONDS) - 1,
                )
            ]
            assert state["dispatch_status"] == "pending"
            due = datetime.fromisoformat(state["next_attempt_at"].replace("Z", "+00:00"))
            assert due - completed_at == timedelta(seconds=expected_delay)
        else:
            assert state["dispatch_status"] == "failed"
            assert state["dispatch_attempts"] == 10
            assert state["next_attempt_at"] is None
            assert state["last_error_code"] == "webhook.delivery_unavailable"
    with connect_database(database_url) as connection:
        terminal_deliveries = {
            str(row["webhook_endpoint_id"]): tuple(row)[1:]
            for row in connection.execute(
            """
            SELECT webhook_endpoint_id,status,error_code,error_summary,next_retry_at
            FROM webhook_deliveries WHERE tenant_id=? AND event_id=?
            """,
            (tenant_id, event_id),
            ).fetchall()
        }
        assert terminal_deliveries == {
            successful_endpoint: ("success", None, None, None),
            pending_endpoint: (
                "failed",
                "webhook.delivery_unavailable",
                "Source delivery attempts are exhausted.",
                None,
            ),
        }
        assert connection.execute(
            """
            SELECT COUNT(*) FROM webhook_dead_letter_events
            WHERE tenant_id=? AND event_id=? AND status='dead_letter'
            """,
            (tenant_id, event_id),
        ).fetchone()[0] == 1
    assert security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-after-terminal",
        now=due + timedelta(days=1),
    ) is None

    # 新建第二条 source，连续模拟十次 worker 在 dispatcher/CAS 前崩溃。
    with connect_database(database_url) as connection:
        orphan_event, _tenant, _request, _trace = _insert_prompt_source(
            connection,
            file_marker="d",
            extract_marker="e",
            event_marker="f",
        )
        connection.commit()
    orphan_due = datetime(2026, 8, 5, 2, tzinfo=UTC)
    for expected_attempt in range(1, 11):
        orphan_claim = security_outbox.claim_next_file_security_event(
            config,
            worker_id=f"prd073-orphan-{expected_attempt}",
            now=orphan_due,
        )
        assert orphan_claim is not None
        assert orphan_claim.event_id == orphan_event
        assert orphan_claim.dispatch_attempts == expected_attempt
        if expected_attempt == 1:
            with connect_database(database_url) as connection:
                orphan_webhook_event = _prompt_event_from_source(
                    connection,
                    tenant_id=tenant_id,
                    event_id=orphan_event,
                )
            # 模拟首次 worker 已冻结真实 Endpoint intent，但在调用 dispatcher 前崩溃。
            orphan_due = security_outbox._freeze_delivery_snapshot(
                config,
                orphan_claim,
                orphan_webhook_event,
                expected_lease_expires_at=orphan_claim.lease_expires_at,
                current=orphan_claim.lease_expires_at - timedelta(seconds=30),
            )
            continue
        if expected_attempt < 10:
            orphan_due = orphan_claim.lease_expires_at
            continue
        # 第十次外呼前续租后，旧到期点不能让 reaper 提前终结正在处理的 source。
        original_tenth_expiry = orphan_claim.lease_expires_at
        renewed_tenth_expiry = security_outbox._renew_source_lease(
            config,
            orphan_claim,
            expected_lease_expires_at=original_tenth_expiry,
            current=original_tenth_expiry - timedelta(seconds=30),
        )
        with connect_database(database_url) as connection:
            stored_tenth_expiry = datetime.fromisoformat(
                connection.execute(
                    """
                    SELECT lease_expires_at FROM file_security_event_outbox
                    WHERE file_security_event_id=?
                    """,
                    (orphan_event,),
                ).fetchone()[0].replace("Z", "+00:00")
            )
        assert stored_tenth_expiry == renewed_tenth_expiry
        assert renewed_tenth_expiry > original_tenth_expiry
        assert security_outbox.claim_next_file_security_event(
            config,
            worker_id="prd073-premature-orphan-reaper",
            now=original_tenth_expiry,
        ) is None
        orphan_due = renewed_tenth_expiry
    assert security_outbox.claim_next_file_security_event(
        config,
        worker_id="prd073-orphan-reaper",
        now=orphan_due,
    ) is None
    with connect_database(database_url) as connection:
        orphan_state = connection.execute(
            "SELECT * FROM file_security_event_outbox WHERE file_security_event_id=?",
            (orphan_event,),
        ).fetchone()
        orphan_deliveries = connection.execute(
            """
            SELECT status,error_code,error_summary,next_retry_at
            FROM webhook_deliveries WHERE tenant_id=? AND event_id=?
            ORDER BY webhook_endpoint_id
            """,
            (tenant_id, orphan_event),
        ).fetchall()
        orphan_dead_letters = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_dead_letter_events
            WHERE tenant_id=? AND event_id=? AND status='dead_letter'
            """,
            (tenant_id, orphan_event),
        ).fetchone()[0]
    assert orphan_state["dispatch_status"] == "failed"
    assert orphan_state["dispatch_attempts"] == 10
    assert orphan_state["claim_token"] is None
    assert orphan_state["lease_owner_id"] is None
    assert orphan_state["lease_expires_at"] is None
    assert len(orphan_deliveries) == 2
    assert all(
        tuple(delivery)
        == (
            "failed",
            "webhook.delivery_unavailable",
            "Source delivery attempts are exhausted.",
            None,
        )
        for delivery in orphan_deliveries
    )
    assert orphan_dead_letters == 2

    # 已过期 worker 网络返回后即使尝试落库，也不能把 reaper 的 failed 终态改回 success。
    with connect_database(database_url) as connection:
        endpoint = connection.execute(
            "SELECT * FROM webhook_endpoints WHERE webhook_endpoint_id=?",
            (successful_endpoint,),
        ).fetchone()
        late_delivery_id = connection.execute(
            """
            SELECT webhook_delivery_id FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND webhook_endpoint_id=?
            """,
            (tenant_id, orphan_event, successful_endpoint),
        ).fetchone()[0]
        late_result = {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }
        webhook_service._record_attempt(
            connection,
            endpoint,
            late_delivery_id,
            1,
            late_result,
        )
        with pytest.raises(ApiError) as late_worker:
            webhook_service._update_delivery_after_attempt(
                connection,
                endpoint,
                late_delivery_id,
                1,
                late_result,
                final=True,
            )
        assert late_worker.value.code == "webhook.delivery_unavailable"
        connection.rollback()
        assert connection.execute(
            """
            SELECT status FROM webhook_deliveries WHERE webhook_delivery_id=?
            """,
            (late_delivery_id,),
        ).fetchone()[0] == "failed"
        assert connection.execute(
            """
            SELECT COUNT(*) FROM webhook_delivery_attempts WHERE webhook_delivery_id=?
            """,
            (late_delivery_id,),
        ).fetchone()[0] == 0
