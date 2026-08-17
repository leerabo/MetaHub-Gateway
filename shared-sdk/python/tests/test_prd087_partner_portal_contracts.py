"""验证 PRD-087 伙伴门户的 RBAC、错误与 EVT-099 公共合同。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CATALOG,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    PARTNER_CUSTOMER_CREATED_EVENT_TYPE,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 这些固定标识只用于 exact wire/租户 owner 测试，不能被测试输出或事件正文当成真实凭据。
PARTNER_ID = "par_" + "a" * 32
CUSTOMER_ID = "pcu_" + "b" * 32
PROJECT_ID = "prj_" + "c" * 32
EVENT_SUFFIX = hashlib.sha256(CUSTOMER_ID.encode("ascii")).hexdigest()[:32]
OCCURRED_AT = datetime(2026, 8, 17, 12, 34, 56, tzinfo=UTC)
PARTNER_PERMISSIONS = {
    "partner_portal.read",
    "partner_portal.configure",
    "partner_customer.manage",
    "partner_branding.manage",
    "partner_commission.export",
    "partner_delivery.download",
}


def _payload(**changes: object) -> dict[str, object]:
    """构造 EVT-099 七字段安全投影，并允许用例覆盖一个字段。"""

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "partner_id": PARTNER_ID,
        "partner_customer_id": CUSTOMER_ID,
        "customer_code": "customer-001",
        "project_id": PROJECT_ID,
        "status": "active",
        "commission_rate_bps": 1_000,
    }
    payload.update(changes)
    return payload


def _build(payload: dict[str, object], **changes: object):
    """使用固定创建链路构造可恢复的伙伴客户事件。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_prd087",
        "event_type": PARTNER_CUSTOMER_CREATED_EVENT_TYPE,
        "payload": payload,
        "actor_id": "user_prd087",
        "object_id": CUSTOMER_ID,
        "trace_id": "trc_prd087",
        "source_request_id": "req_prd087",
        "event_id": f"evt_pcu_{EVENT_SUFFIX}",
        "timestamp": OCCURRED_AT,
    }
    values.update(changes)
    return build_webhook_event(**values)


def test_prd087_rbac_revision_catalog_and_role_matrices_are_exact() -> None:
    """确认 revision 22 的六权限目录和六个角色矩阵没有静默漂移。"""

    assert CATALOG_REVISION == 22
    assert len(PERMISSION_CATALOG) == 96
    assert PARTNER_PERMISSIONS <= set(PERMISSION_BY_CODE)
    assert PARTNER_PERMISSIONS <= ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert ROLE_GRANTABLE_PERMISSIONS["viewer"] & PARTNER_PERMISSIONS == (
        PARTNER_PERMISSIONS - {"partner_portal.configure"}
    )
    for role in ("developer", "finance_admin", "security_admin", "ops_admin"):
        assert ROLE_GRANTABLE_PERMISSIONS[role].isdisjoint(PARTNER_PERMISSIONS)
    assert {
        role: permission_matrix_sha256(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    } == {
        "admin": "0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a",
        "developer": "ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1",
        "viewer": "7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3",
        "finance_admin": "86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c",
        "security_admin": "aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df",
        "ops_admin": "1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783",
    }


def test_partner_customer_created_accepts_exact_safe_projection() -> None:
    """确认 EVT-099 的 payload、对象、创建链路和稳定事件身份可签名。"""

    event = _build(_payload())
    assert PARTNER_CUSTOMER_CREATED_EVENT_TYPE in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == f"evt_pcu_{EVENT_SUFFIX}"
    assert event.object_id == CUSTOMER_ID
    assert event.timestamp == "2026-08-17T12:34:56Z"
    assert event.payload == _payload()


@pytest.mark.parametrize(
    "mutation",
    (
        {"schema_version": 1},
        {"partner_id": "par_" + "A" * 32},
        {"partner_customer_id": "pcu_" + "b" * 31},
        {"customer_code": ".customer"},
        {"customer_code": "CUSTOMER"},
        {"project_id": "prj_" + "C" * 32},
        {"status": "disabled"},
        {"commission_rate_bps": True},
        {"commission_rate_bps": 999},
        {"display_name": "private"},
        {"support_text": "private"},
        {"prompt": "private"},
        {"response": "private"},
        {"token": "private"},
        {"provider_key": "private"},
    ),
)
def test_partner_customer_created_rejects_drift_or_sensitive_payload(
    mutation: dict[str, object],
) -> None:
    """确认额外字段、敏感正文、弱 ID、状态和费率漂移都在签名前拒绝。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_(?:invalid|sensitive)$"):
        _build(_payload(**mutation))


@pytest.mark.parametrize(
    "changes",
    (
        {"event_id": "evt_pcu_" + "d" * 32},
        {"tenant_id": "tenant\nother"},
        {"actor_id": None},
        {"object_id": "pcu_" + "d" * 32},
        {"trace_id": "trace\nother"},
        {"source_request_id": "request\nother"},
        {"timestamp": datetime(2026, 8, 17, 12, 34, 56)},
    ),
)
def test_partner_customer_created_rejects_envelope_drift(changes: dict[str, object]) -> None:
    """确认恢复流程不能替换 tenant、actor、对象、事件身份或请求链路。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(), **changes)


def test_partner_customer_created_payload_is_detached() -> None:
    """确认已校验的事件副本不受调用方后续修改。"""

    payload = _payload()
    event = _build(payload)
    payload["customer_code"] = "changed"
    assert event.payload["customer_code"] == "customer-001"


def test_prd087_error_specs_are_exact_and_generic() -> None:
    """确认伙伴错误状态、重试语义和公开文本不会泄露内部实现。"""

    assert {
        code: (spec.http_status, spec.retryable, spec.owner)
        for code, spec in ERROR_SPECS.items()
        if code.startswith("partner.")
    } == {
        "partner.request_invalid": (422, False, "partner"),
        "partner.not_configured": (404, False, "partner"),
        "partner.already_configured": (409, False, "partner"),
        "partner.revision_conflict": (409, False, "partner"),
        "partner.project_not_found": (404, False, "partner"),
        "partner.customer_not_found": (404, False, "partner"),
        "partner.customer_conflict": (409, False, "partner"),
        "partner.commission_unavailable": (503, True, "partner"),
        "partner.export_too_large": (413, False, "partner"),
        "partner.export_unavailable": (503, True, "partner"),
        "partner.event_unavailable": (503, True, "partner"),
        "partner.unavailable": (503, True, "partner"),
    }
    public_text = " ".join(
        f"{spec.message} {spec.suggestion}".lower()
        for code, spec in ERROR_SPECS.items()
        if code.startswith("partner.")
    )
    assert all(
        sensitive not in public_text
        for sensitive in ("sqlite", "table", "path", "token", "prompt", "response", "key")
    )
