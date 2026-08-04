"""PRD-057 Webhook 死信归档审计白名单专项测试。"""

from __future__ import annotations

from metahub_control.audit.service import ACTION_OBJECT_TYPES, SUMMARY_FIELDS


def test_dead_letter_archive_audit_contract_is_minimal_and_stable() -> None:
    """归档动作只允许定位根死信和前后状态，不允许正文、URL、幂等键或 lease 入库。"""

    assert ACTION_OBJECT_TYPES["webhook_dead_letter.archived"] == "webhook_dead_letter"
    assert SUMMARY_FIELDS["webhook_dead_letter"] == frozenset(
        {
            "dead_letter_event_id",
            "webhook_delivery_id",
            "webhook_endpoint_id",
            "event_type",
            "status",
            "failed_at",
            "archived_at",
        }
    )
    forbidden = {
        "payload",
        "endpoint_url",
        "idempotency_key",
        "replay_claim_token",
        "replay_lease_owner_id",
        "secret",
    }
    assert SUMMARY_FIELDS["webhook_dead_letter"].isdisjoint(forbidden)
