"""PRD-060 邀请错误码、Webhook 和链路凭据脱敏合同测试。

本文件只验证共享运行时的纯函数，不创建数据库、不访问网络，也不保存原始邀请 Token。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.request_id import resolve_request_id, resolve_trace_id
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)


# 固定 Invitation/Event ID 只验证严格 envelope，不对应真实租户业务记录。
INVITATION_ID = "inv_00000000000000000000000000000060"
EVENT_ID = "ivevt_00000000000000000000000000000060"
# 邮箱指纹夹具是规范小写 SHA-256 形状，不包含任何邮箱明文。
EMAIL_FINGERPRINT = "6" * 64
# Token canary 覆盖完整值与滑动 16 字符碰撞，不进入日志或测试证据文件。
TOKEN_CANARY = "A" * 14 + "0123456789abcdef" + "B" * 13
# 固定 UTC 秒保证 Webhook 时间合同与有效期上限断言可重复。
OCCURRED_AT = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def test_invitation_error_registry_keeps_public_failures_indistinguishable() -> None:
    """错误合同：公开状态只暴露统一 410，基础设施错误才允许 503 重试。"""

    expected = {
        "invitation.create_invalid": (422, False),
        "invitation.role_invalid": (422, False),
        "invitation.user_exists": (409, False),
        "invitation.pending_exists": (409, False),
        "invitation.not_available": (410, False),
        "invitation.accept_invalid": (422, False),
        "invitation.unavailable": (503, True),
    }
    actual = {
        code: (spec.http_status, spec.retryable)
        for code, spec in ERROR_SPECS.items()
        if code.startswith("invitation.")
    }

    assert actual == expected
    assert {ERROR_SPECS[code].owner for code in expected} == {"invitation"}


@pytest.mark.parametrize(
    ("event_type", "actor_id", "payload"),
    (
        (
            "user.invited",
            "user_admin_prd060",
            {
                "schema_version": 1,
                "invitation_id": INVITATION_ID,
                "email_fingerprint": EMAIL_FINGERPRINT,
                "role_code": "developer",
                "expires_at": "2026-08-03T12:00:00Z",
                "invited_at": "2026-07-31T12:00:00Z",
            },
        ),
        (
            "invitation.accepted",
            "user_invited_prd060",
            {
                "schema_version": 1,
                "invitation_id": INVITATION_ID,
                "user_id": "user_invited_prd060",
                "email_fingerprint": EMAIL_FINGERPRINT,
                "role_code": "developer",
                "accepted_at": "2026-07-31T12:00:00Z",
            },
        ),
    ),
)
def test_invitation_webhook_contract_accepts_only_persistent_safe_facts(
    event_type: str,
    actor_id: str,
    payload: dict[str, object],
) -> None:
    """主流程：两个事件绑定持久 ID、可信 actor、Invitation 和来源链路。"""

    event = build_webhook_event(
        tenant_id="tenant_prd060",
        event_type=event_type,
        payload=payload,
        actor_id=actor_id,
        object_id=INVITATION_ID,
        trace_id="trc_prd060_contract",
        source_request_id="req_prd060_contract",
        event_id=EVENT_ID,
        timestamp=OCCURRED_AT,
    )

    assert event.event_id == EVENT_ID
    assert event.event_type == event_type
    assert event.actor_id == actor_id
    assert event.object_id == INVITATION_ID
    assert event.payload == payload
    assert event_type in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES


@pytest.mark.parametrize(
    "mutation",
    (
        {"email": "private@example.com"},
        {"token": TOKEN_CANARY},
        {"password": "PrivatePassword!2026"},
        {"role_code": "unknown"},
        {"schema_version": True},
    ),
)
def test_invitation_webhook_contract_rejects_extra_sensitive_or_invalid_fields(
    mutation: dict[str, object],
) -> None:
    """安全流程：PII、凭据、未知角色、bool 版本和额外字段均整体失败关闭。"""

    payload: dict[str, object] = {
        "schema_version": 1,
        "invitation_id": INVITATION_ID,
        "email_fingerprint": EMAIL_FINGERPRINT,
        "role_code": "developer",
        "expires_at": "2026-08-03T12:00:00Z",
        "invited_at": "2026-07-31T12:00:00Z",
    }
    payload.update(mutation)

    with pytest.raises(ValueError, match=r"^webhook\.payload_(invalid|sensitive)$"):
        build_webhook_event(
            tenant_id="tenant_prd060",
            event_type="user.invited",
            payload=payload,
            actor_id="user_admin_prd060",
            object_id=INVITATION_ID,
            trace_id="trc_prd060_contract",
            source_request_id="req_prd060_contract",
            event_id=EVENT_ID,
            timestamp=OCCURRED_AT,
        )


@pytest.mark.parametrize(
    "incoming",
    (
        TOKEN_CANARY,
        f"req_{TOKEN_CANARY}",
        TOKEN_CANARY[:16],
        TOKEN_CANARY[14:30],
        TOKEN_CANARY[-16:],
    ),
)
def test_request_and_trace_ids_cannot_echo_invitation_token_fragments(incoming: str) -> None:
    """安全流程：完整 Token 及任意连续 16 字符都不能借链路 Header 回显。"""

    request_id = resolve_request_id(incoming, sensitive_values=(TOKEN_CANARY,))
    trace_id = resolve_trace_id(
        incoming,
        request_id,
        sensitive_values=(TOKEN_CANARY,),
    )

    assert request_id.startswith("req_")
    assert trace_id.startswith("trc_")
    assert incoming not in request_id
    assert incoming not in trace_id
    assert TOKEN_CANARY not in request_id
    assert TOKEN_CANARY not in trace_id
