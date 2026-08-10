"""验证 PRD-075 source outbox 只终结白名单事件的 pending Webhook intent。

测试边界：所有事件均为合成租户事实，不保存签名密钥、客户 Endpoint 或回调正文。
"""

from __future__ import annotations

import sqlite3

import pytest
from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries
from metahub_shared.runtime.errors import ApiError


def _connection() -> sqlite3.Connection:
    """创建只包含 cleanup helper 所需列的内存数据库。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE webhook_deliveries(
          tenant_id TEXT NOT NULL,
          event_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          delivery_kind TEXT NOT NULL,
          status TEXT NOT NULL,
          error_code TEXT,
          error_summary TEXT,
          next_retry_at TEXT,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO webhook_deliveries(
          tenant_id,event_id,event_type,delivery_kind,status,updated_at
        ) VALUES(?,?,?,'original','pending','2026-08-10T00:00:00Z')
        """,
        (
            ("tenant-prd075", "event-file", "file.prompt_injection_detected"),
            ("tenant-prd075", "event-experiment", "experiment.completed"),
            ("tenant-prd075", "event-other", "audit.recorded"),
        ),
    )
    connection.commit()
    return connection


@pytest.mark.parametrize(
    ("event_id", "event_type"),
    (
        ("event-file", "file.prompt_injection_detected"),
        ("event-experiment", "experiment.completed"),
    ),
)
def test_pending_delivery_cleanup_accepts_only_supported_source_events(
    event_id: str,
    event_type: str,
) -> None:
    """两个持久 source 类型都只终结自己的 original pending delivery。"""

    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert fail_pending_webhook_event_deliveries(
            connection,
            tenant_id="tenant-prd075",
            event_id=event_id,
            event_type=event_type,
            updated_at="2026-08-10T00:00:01Z",
            failure_code="webhook.delivery_unavailable",
        ) == 1
        connection.commit()
        rows = {
            str(row["event_id"]): str(row["status"])
            for row in connection.execute(
                "SELECT event_id,status FROM webhook_deliveries ORDER BY event_id"
            ).fetchall()
        }
        assert rows[event_id] == "failed"
        assert rows["event-other"] == "pending"
    finally:
        connection.close()


def test_pending_delivery_cleanup_rejects_unapproved_event_type() -> None:
    """任意其他事件类型必须在执行 UPDATE 前失败关闭。"""

    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ApiError) as invalid:
            fail_pending_webhook_event_deliveries(
                connection,
                tenant_id="tenant-prd075",
                event_id="event-other",
                event_type="audit.recorded",
                updated_at="2026-08-10T00:00:01Z",
                failure_code="webhook.delivery_unavailable",
            )
        assert invalid.value.code == "webhook.delivery_unavailable"
        assert connection.execute(
            "SELECT status FROM webhook_deliveries WHERE event_id='event-other'"
        ).fetchone()[0] == "pending"
        connection.rollback()
    finally:
        connection.close()
