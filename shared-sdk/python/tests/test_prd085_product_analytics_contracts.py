"""验证 PRD-085 产品分析的权限目录、错误模型与 Webhook 公共契约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 固定标识、时间和七阶段类型用于验证跨模块序列化结果保持字节级稳定。
PRODUCT_EVENT_ID = "pev_" + "a" * 32
WEBHOOK_EVENT_ID = "evt_" + PRODUCT_EVENT_ID
NOW = datetime(2026, 8, 17, 1, 2, 3, tzinfo=UTC)
ALLOWED_EVENT_TYPES = (
    "tenant.registered",
    "provider.created",
    "virtual_key.created",
    "gateway.first_call_succeeded",
    "budget.configured",
    "teammate.invited",
    "request_logs.viewed",
)


def _payload(**changes: object) -> dict[str, object]:
    """构造最小公开事件载荷，并允许测试用例只覆盖待验证字段。"""

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "product_event_id": PRODUCT_EVENT_ID,
        "event_type": "provider.created",
        "capture_mode": "live",
        "occurred_at": "2026-08-17T01:02:03Z",
    }
    payload.update(changes)
    return payload


def _build(payload: dict[str, object], **changes: object):
    """使用固定租户与追踪坐标构造 Webhook，避免各用例重复信封字段。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_prd085",
        "event_type": "product_event.recorded",
        "payload": payload,
        "actor_id": "user_prd085",
        "object_id": PRODUCT_EVENT_ID,
        "trace_id": "trc_prd085",
        "source_request_id": "req_prd085",
        "event_id": WEBHOOK_EVENT_ID,
        "timestamp": NOW,
    }
    values.update(changes)
    return build_webhook_event(**values)


def test_prd141_retires_product_analytics_from_the_active_rbac_catalog() -> None:
    """PRD-085 的事件模型保留，但 Workspace 读取权限已由 PRD-141 退役。"""

    assert CATALOG_REVISION >= 27
    assert "product_analytics.read" not in PERMISSION_BY_CODE
    assert all(item.code != "product_analytics.read" for item in PERMISSION_CATALOG)
    assert all(
        "product_analytics.read" not in permissions
        for permissions in ROLE_GRANTABLE_PERMISSIONS.values()
    )


def test_prd141_active_role_matrix_hashes_match_migration_0133() -> None:
    """共享授权代码必须与迁移 0133 写入的六个固定矩阵完全一致。"""

    assert {
        role: permission_matrix_sha256(permissions)
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
    } == {
        "admin": "eb48d3c3fddfa76477ae5af3943449253af0581da445e3d5695f893258dba6b8",
        "developer": "888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686",
        "viewer": "be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af",
        "finance_admin": "843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc",
        "security_admin": "7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb",
        "ops_admin": "bdab5bcae4741701f7171d1f08624f71306963dea15e99ccbde55028df2e6426",
    }


@pytest.mark.parametrize("product_event_type", ALLOWED_EVENT_TYPES)
def test_product_event_recorded_accepts_every_frozen_stage(
    product_event_type: str,
) -> None:
    """确认七类激活事实均使用同一个最小化实时公开事件契约。"""

    event = _build(_payload(event_type=product_event_type))
    assert "product_event.recorded" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == WEBHOOK_EVENT_ID
    assert event.object_id == PRODUCT_EVENT_ID
    assert event.timestamp == "2026-08-17T01:02:03Z"
    assert event.payload == _payload(event_type=product_event_type)


@pytest.mark.parametrize(
    "mutation",
    (
        {"metadata": {}},
        {"source_id": "audit_log_1"},
        {"request_id": "req_private"},
        {"name": "private"},
        {"email": "private@example.invalid"},
        {"endpoint": "https://private.invalid/v1"},
        {"filter": "private"},
        {"prompt": "private"},
        {"response": "private"},
        {"token": "private"},
        {"provider_key": "private"},
        {"virtual_key": "private"},
        {"authorization": "Bearer private"},
    ),
)
def test_product_event_recorded_rejects_extra_or_sensitive_fields(
    mutation: dict[str, object],
) -> None:
    """确认源身份、任意元数据、密钥和业务正文无法进入公开事件载荷。"""

    payload = _payload()
    payload.update(mutation)
    with pytest.raises(ValueError, match=r"^webhook\.payload_(?:invalid|sensitive)$"):
        _build(payload)


@pytest.mark.parametrize(
    "mutation",
    (
        {"schema_version": 1},
        {"schema_version": "2.0"},
        {"product_event_id": "pev_" + "A" * 32},
        {"product_event_id": "pev_" + "a" * 31},
        {"event_type": "tenant.created"},
        {"event_type": 1},
        {"capture_mode": "backfill"},
        {"capture_mode": True},
        {"occurred_at": "2026-08-17T01:02:03+00:00"},
        {"occurred_at": "2026-08-17T01:02:03.000Z"},
        {"occurred_at": "2026-13-17T01:02:03Z"},
    ),
)
def test_product_event_recorded_rejects_malformed_payload(
    mutation: dict[str, object],
) -> None:
    """确认线协议类型、标识、枚举、采集模式与整秒时间格式不可漂移。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(**mutation))


@pytest.mark.parametrize(
    "changes",
    (
        {"event_id": "pevo_" + "b" * 32},
        {"event_id": "evt_pev_" + "B" * 32},
        {"tenant_id": "tenant\nother"},
        {"actor_id": "user\nother"},
        {"object_id": "pev_" + "b" * 32},
        {"trace_id": "trace\nother"},
        {"source_request_id": "request\nother"},
        {"timestamp": NOW + timedelta(seconds=1)},
        {"timestamp": datetime(2026, 8, 17, 1, 2, 3)},
    ),
)
def test_product_event_recorded_rejects_envelope_drift(
    changes: dict[str, object],
) -> None:
    """确认恢复流程不能替换公开事件身份、租户归属、追踪链或时间戳。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _build(_payload(), **changes)


def test_product_event_recorded_accepts_nullable_source_actor_and_trace() -> None:
    """确认系统派生事实能够保留权威来源中为空的操作者和追踪坐标。"""

    event = _build(
        _payload(event_type="gateway.first_call_succeeded"),
        actor_id=None,
        trace_id=None,
        source_request_id=None,
    )
    assert event.actor_id is None
    assert event.trace_id is None


def test_product_event_recorded_payload_is_detached_from_caller_mutation() -> None:
    """确认已签名事件保留校验后的安全副本，不受调用方后续修改影响。"""

    payload = _payload()
    event = _build(payload)
    payload["event_type"] = "tenant.registered"
    assert event.payload["event_type"] == "provider.created"


def test_prd085_error_specs_are_exact_and_generic() -> None:
    """确认产品分析错误保持稳定状态码，同时不会泄露数据库或敏感实现细节。"""

    assert {
        code: (spec.http_status, spec.retryable, spec.owner)
        for code, spec in ERROR_SPECS.items()
        if code.startswith("product_analytics.")
    } == {
        "product_analytics.request_invalid": (422, False, "product_analytics"),
        "product_analytics.unavailable": (503, True, "product_analytics"),
    }
    public_text = " ".join(
        f"{ERROR_SPECS[code].message} {ERROR_SPECS[code].suggestion}".lower()
        for code in (
            "product_analytics.request_invalid",
            "product_analytics.unavailable",
        )
    )
    assert all(
        sensitive not in public_text
        for sensitive in ("sqlite", "table", "path", "token", "prompt", "response", "key")
    )
