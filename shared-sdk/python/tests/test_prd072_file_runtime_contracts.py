"""PRD-072 文件权限、错误目录与 Webhook 安全合同专项测试。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from io import StringIO

import pytest
from metahub_control.auth import require_file_reader, require_file_writer
from metahub_control.auth.dependencies import (
    require_file_reader as dependency_file_reader,
)
from metahub_control.auth.dependencies import (
    require_file_writer as dependency_file_writer,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.logging import (
    DropUnsafeThirdPartyLoggerFilter,
    configure_json_logging,
)
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# revision 8 的六角色摘要是迁移和 Shared canonical JSON 共同冻结的发布合同。
REVISION_8_HASHES = {
    "admin": "6826d92219b488c5d6553147fbf59bb5a1ff514e7cb1812be724bb352017a052",
    "developer": "d1cc4e0cad810c33e57ca2061a2345ef9a7843b26d8af80cb218d4dbf34fae87",
    "viewer": "519abd5f24d6c0612727f010fc1eae824b8c05ff0b8a4b0ca32f5ee9c2d3ddb5",
    "finance_admin": "96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114",
    "security_admin": "6b090e14a83776cb75f9633a8cd49033b4b95fc79f18de6c33fbefd1f6f04ec0",
    "ops_admin": "6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b",
}


def test_file_permissions_are_revision_8_and_role_bounded() -> None:
    """权限主流程：目录、风险级别、默认角色与 canonical hash 必须精确一致。"""

    assert CATALOG_REVISION == 8
    assert len(PERMISSION_CODES) == 61
    assert PERMISSION_BY_CODE["file.read"].risk_level == "read"
    assert PERMISSION_BY_CODE["file.write"].risk_level == "write"
    for role_code in ("admin", "developer", "viewer", "security_admin"):
        assert "file.read" in ROLE_GRANTABLE_PERMISSIONS[role_code]
    for role_code in ("admin", "developer", "viewer"):
        assert "file.write" in ROLE_GRANTABLE_PERMISSIONS[role_code]
    for role_code in ("finance_admin", "ops_admin"):
        assert not {"file.read", "file.write"} & ROLE_GRANTABLE_PERMISSIONS[role_code]
    assert "file.write" not in ROLE_GRANTABLE_PERMISSIONS["security_admin"]
    assert {
        role_code: permission_matrix_sha256(permission_codes)
        for role_code, permission_codes in ROLE_GRANTABLE_PERMISSIONS.items()
    } == REVISION_8_HASHES


def test_auth_package_exports_file_dependencies() -> None:
    """后端权限依赖必须从认证包公开导出，业务路由不能退化为前端按钮判断。"""

    assert require_file_reader is dependency_file_reader
    assert require_file_writer is dependency_file_writer


def test_file_error_directory_is_complete_localized_and_diagnosable() -> None:
    """14 个文件错误固定 HTTP/retry/owner，并具备中英文标签和安全专项诊断。"""

    expected = {
        "file.multipart_invalid": (400, False),
        "file.name_invalid": (422, False),
        "file.unsupported_format": (415, False),
        "file.too_large": (413, False),
        "file.processing_busy": (429, True),
        "file.structure_invalid": (422, False),
        "file.no_extractable_text": (422, False),
        "file.extract_limit_exceeded": (413, False),
        "file.parse_failed": (422, False),
        "file.dlp_blocked": (403, False),
        "file.dlp_unavailable": (503, True),
        "file.storage_unavailable": (503, True),
        "file.integrity_failed": (500, False),
        "file.not_found": (404, False),
    }
    for code, (status, retryable) in expected.items():
        spec = ERROR_SPECS[code]
        assert (spec.http_status, spec.retryable, spec.owner) == (status, retryable, "file")
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
        diagnosis = diagnose_error(code, http_status=status, message=spec.message)
        assert diagnosis.matched_by == "error_code"
        assert diagnosis.rule_id != "unknown_error"
    rendered = " ".join(
        f"{ERROR_SPECS[code].message} {ERROR_SPECS[code].suggestion}" for code in expected
    ).lower()
    assert all(term not in rendered for term in ("filename", "object_key", "ciphertext", "pattern"))


def test_pypdf_free_text_logs_are_dropped_before_formatting() -> None:
    """日志隐私：pypdf 的用户控制告警正文必须在 root handler 边界被整体丢弃。"""

    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    pypdf_base_logger = logging.getLogger("pypdf")
    pypdf_logger = logging.getLogger("pypdf._reader")
    original_root_handlers = list(root.handlers)
    original_root_level = root.level
    original_states = {
        logger: (list(logger.handlers), logger.level, logger.propagate)
        for logger in (httpx_logger, httpcore_logger, pypdf_base_logger, pypdf_logger)
    }
    stream = StringIO()
    root.handlers = [logging.StreamHandler(stream)]
    try:
        configure_json_logging("INFO")
        configure_json_logging("INFO")
        root_handler = root.handlers[0]
        assert (
            sum(
                isinstance(item, DropUnsafeThirdPartyLoggerFilter)
                for item in root_handler.filters
            )
            == 1
        )

        # 模拟第三方子 logger 显式改级别；root Filter 仍必须拦截 PDF 正文金丝雀。
        pypdf_logger.setLevel(logging.DEBUG)
        pypdf_logger.warning("broken CMap line: %s", "PRD072_PDF_BODY_CANARY")
        logging.getLogger("metahub.test").warning(
            "file.parse_failed",
            extra={"request_id": "req_prd072_log", "error_code": "file.parse_failed"},
        )
        rendered = stream.getvalue()
        assert "PRD072_PDF_BODY_CANARY" not in rendered
        assert '"logger": "pypdf._reader"' not in rendered
        assert '"request_id": "req_prd072_log"' in rendered
        assert '"error_code": "file.parse_failed"' in rendered
    finally:
        root.handlers = original_root_handlers
        root.setLevel(original_root_level)
        for logger, (handlers, level, propagate) in original_states.items():
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate


def test_file_uploaded_webhook_accepts_exact_safe_payload() -> None:
    """uploaded 事件只接受 processing owner 的六字段安全投影和完整 source 信封。"""

    payload = _uploaded_payload()
    event = build_webhook_event(
        tenant_id="tenant_prd072",
        event_type="file.uploaded",
        payload=payload,
        actor_id="user_prd072",
        object_id=payload["file_id"],
        trace_id="trc_prd072",
        source_request_id="req_prd072",
        event_id="fevt_" + "b" * 32,
        timestamp=datetime(2026, 8, 5, 2, 0, tzinfo=UTC),
    )

    assert {"file.uploaded", "file.scanned"} <= WEBHOOK_EVENT_TYPES
    assert {"file.uploaded", "file.scanned"} <= WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.payload == payload


@pytest.mark.parametrize(
    ("event_type", "updates"),
    [
        ("file.uploaded", {"filename": "private.txt"}),
        ("file.uploaded", {"content": "private body"}),
        ("file.uploaded", {"size_bytes": True}),
        ("file.uploaded", {"virus_scan_status": "clean"}),
        ("file.scanned", {"object_key": "tenant/path"}),
        ("file.scanned", {"content_fingerprint": "a" * 64}),
        ("file.scanned", {"dlp_scan_status": "clean", "match_count": 1}),
        ("file.scanned", {"error_code": "file.parse_failed"}),
    ],
)
def test_file_webhook_rejects_extra_sensitive_or_inconsistent_fields(
    event_type: str,
    updates: dict[str, object],
) -> None:
    """Webhook 异常流程：额外敏感键、类型漂移和状态矛盾必须在签名前拒绝。"""

    payload = _uploaded_payload() if event_type == "file.uploaded" else _ready_payload()
    payload.update(updates)
    timestamp = datetime(2026, 8, 5, 2, 0 if event_type == "file.uploaded" else 1, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"webhook\.payload_(?:invalid|sensitive)"):
        build_webhook_event(
            tenant_id="tenant_prd072",
            event_type=event_type,
            payload=payload,
            actor_id="user_prd072",
            object_id="file_" + "a" * 32,
            trace_id="trc_prd072",
            source_request_id="req_prd072",
            event_id="fevt_" + "b" * 32,
            timestamp=timestamp,
        )


def test_file_scanned_webhook_accepts_ready_and_blocked_contracts() -> None:
    """扫描主/阻断流程：成功 error=null，阻断必须保持 DLP 决策、Token 和固定错误码。"""

    ready = _ready_payload()
    blocked = {
        **ready,
        "status": "blocked",
        "dlp_scan_status": "blocked",
        "dlp_action": "block",
        "match_count": 1,
        "error_code": "file.dlp_blocked",
    }
    for suffix, payload in (("c", ready), ("d", blocked)):
        event = build_webhook_event(
            tenant_id="tenant_prd072",
            event_type="file.scanned",
            payload=payload,
            actor_id="user_prd072",
            object_id=payload["file_id"],
            trace_id="trc_prd072",
            source_request_id="req_prd072",
            event_id="fevt_" + suffix * 32,
            timestamp=datetime(2026, 8, 5, 2, 1, tzinfo=UTC),
        )
        assert event.payload == payload


def test_file_webhook_rejects_envelope_drift() -> None:
    """权限/租户链路异常：缺 actor、跨对象或错误 source 时间都不能构造文件事件。"""

    payload = _uploaded_payload()
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd072",
            event_type="file.uploaded",
            payload=payload,
            actor_id=None,
            object_id="file_" + "f" * 32,
            trace_id="trc_prd072",
            source_request_id="req_prd072",
            event_id="fevt_" + "b" * 32,
            timestamp=datetime(2026, 8, 5, 2, 1, tzinfo=UTC),
        )


def _uploaded_payload() -> dict[str, object]:
    """构造不含文件名、正文、对象键、hash 或 DLP 证据的上传摘要。"""

    return {
        "file_id": "file_" + "a" * 32,
        "status": "processing",
        "media_type": "text/plain",
        "size_bytes": 18,
        "virus_scan_status": "not_configured",
        "created_at": "2026-08-05T02:00:00Z",
    }


def _ready_payload() -> dict[str, object]:
    """构造完成 DLP 和文本抽取后的安全 ready 摘要。"""

    return {
        "file_id": "file_" + "a" * 32,
        "status": "ready",
        "media_type": "text/plain",
        "size_bytes": 18,
        "virus_scan_status": "not_configured",
        "dlp_scan_status": "clean",
        "dlp_action": None,
        "match_count": 0,
        "estimated_tokens": 4,
        "extractor_version": "txt-v1",
        "completed_at": "2026-08-05T02:01:00Z",
        "error_code": None,
    }
