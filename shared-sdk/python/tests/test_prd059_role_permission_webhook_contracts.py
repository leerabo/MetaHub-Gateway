"""PRD-059 权限矩阵 Webhook 对新旧角色身份的精确兼容合同测试。

测试只构造固定租户、角色和权限差异摘要，不访问数据库、网络或运行服务；Provider Key、
Token、Prompt、Response 及用户邮箱均不进入事件或断言输出。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.webhooks import build_webhook_event

# 当前角色由数据库触发器生成带前缀随机 ID；旧三角色由 0050 迁移原样保留 UUID 主键。
CURRENT_ROLE_ID = "role_" + "1" * 32
LEGACY_DEVELOPER_ROLE_ID = "706c98f5-0edb-5e48-ab68-4e03cf91eab6"


def _payload(role_id: str) -> dict[str, object]:
    """构造一次仅撤销 ``chat.use`` 的安全权限差异摘要。

    参数：待验证的新式或历史角色 ID。返回：完整九字段事件 payload。
    约束：权限列表保持规范排序，revision 相邻且前后哈希不同，不携带完整权限矩阵。
    """

    return {
        "schema_version": 1,
        "role_id": role_id,
        "role_code": "developer",
        "before_revision": 2,
        "after_revision": 3,
        "before_sha256": "a" * 64,
        "after_sha256": "b" * 64,
        "added_permissions": [],
        "removed_permissions": ["chat.use"],
    }


def _event(role_id: str, *, object_id: str | None = None):
    """构造绑定固定租户、操作者、请求链路和目标角色的持久事件。

    参数：payload 角色 ID，以及可选的信封对象 ID。返回：已校验 WebhookEvent。
    约束：未覆盖对象时逐字绑定同一角色；覆盖参数仅用于验证跨对象信封会失败关闭。
    """

    return build_webhook_event(
        tenant_id="tenant_prd059",
        event_type="role.permission_changed",
        payload=_payload(role_id),
        actor_id="user_prd059_admin",
        object_id=role_id if object_id is None else object_id,
        trace_id="trc_prd059_role_patch",
        source_request_id="req_prd059_role_patch",
        event_id="rpevt_" + "2" * 32,
        timestamp=datetime(2026, 8, 2, 8, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize("role_id", (CURRENT_ROLE_ID, LEGACY_DEVELOPER_ROLE_ID))
def test_role_permission_event_accepts_current_and_canonical_legacy_role_ids(
    role_id: str,
) -> None:
    """兼容主流程：新角色 ID 与迁移保留的规范小写 UUID 都能形成事件。"""

    event = _event(role_id)

    assert event.payload["role_id"] == role_id
    assert event.object_id == role_id
    assert event.payload["removed_permissions"] == ["chat.use"]


@pytest.mark.parametrize(
    "role_id",
    (
        LEGACY_DEVELOPER_ROLE_ID.upper(),
        LEGACY_DEVELOPER_ROLE_ID.replace("-", ""),
        "developer",
        "role_" + "A" * 32,
    ),
)
def test_role_permission_event_rejects_noncanonical_or_arbitrary_role_ids(
    role_id: str,
) -> None:
    """异常流程：大写、无连字符、自由文本和非小写新格式不能产生多种签名表示。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _event(role_id)


def test_legacy_role_permission_event_keeps_exact_object_binding() -> None:
    """租户归属：兼容历史 UUID 不得放松 payload 与信封 ``object_id`` 的逐字绑定。"""

    with pytest.raises(ValueError, match=r"^webhook\.payload_invalid$"):
        _event(LEGACY_DEVELOPER_ROLE_ID, object_id=CURRENT_ROLE_ID)
