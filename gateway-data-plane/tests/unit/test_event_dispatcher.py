"""PRD-047 数据面运行事件队列的非阻塞与安全日志测试。"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from metahub_gateway.events import dispatcher
from metahub_shared.runtime.webhooks import build_webhook_event


def _policy_event():
    """构造仅含安全路由元数据的 request_selected 事件，明确排除 Prompt、Key 和 Endpoint。"""

    return build_webhook_event(
        tenant_id="tenant_prd047",
        event_type="route.policy_selected",
        event_id="rpe_0123456789abcdef0123456789abcdef",
        object_id="rpol_0123456789abcdef0123456789abcdef",
        trace_id="trace_prd047",
        payload={
            "trigger": "request_selected",
            "request_id": "req_prd047",
            "routing_decision_id": "rdec_prd047",
            "model_alias_id": "alias_prd047",
            "routing_policy_id": "rpol_0123456789abcdef0123456789abcdef",
            "routing_policy_version": 1,
            "routing_mode": "fixed",
            "selected_model_id": "model_prd047",
            "selected_provider_id": "provider_prd047",
            "candidate_count": 1,
            "eligible_candidate_count": 1,
            "selection_reason": "routing.fixed_priority",
            "project_id": None,
        },
    )


def test_policy_event_queue_full_is_non_blocking_and_uses_generic_log(caplog) -> None:
    """队列满立即返回 false，并按真实事件类型记录通用丢弃日志。"""

    event = _policy_event()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(event)
    state = SimpleNamespace(runtime_event_queue=queue)

    with caplog.at_level(logging.WARNING, logger="metahub.gateway.events"):
        accepted = dispatcher.enqueue_runtime_event(state, event)

    assert accepted is False
    record = caplog.records[-1]
    assert record.event == "route.runtime_event_dropped"
    assert record.payload == {
        "event_type": "route.policy_selected",
        "reason": "queue_full",
    }


@pytest.mark.anyio
async def test_policy_event_worker_failure_uses_generic_safe_log(monkeypatch, caplog) -> None:
    """接收端异常不终止 worker，日志只保留事件类型、稳定 ID 和统一错误码。"""

    event = _policy_event()

    def fail_delivery(_config, _event):
        """模拟投递层抛出可能含外部细节的异常。"""

        # 异常文本刻意包含敏感接收端细节，用于证明 worker 日志会统一脱敏。
        raise RuntimeError("sensitive receiver detail")

    monkeypatch.setattr(dispatcher, "dispatch_runtime_webhook_event", fail_delivery)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(event)
    queue.put_nowait(None)
    app = SimpleNamespace(state=SimpleNamespace(config=object()))

    with caplog.at_level(logging.WARNING, logger="metahub.gateway.events"):
        await dispatcher._event_worker(app, queue)

    record = caplog.records[-1]
    assert record.event == "route.runtime_webhook_failed"
    assert record.payload == {
        "event_type": "route.policy_selected",
        "error_code": "webhook.delivery_unavailable",
    }
    assert "sensitive receiver detail" not in record.getMessage()
