"""数据面有界非阻塞 Webhook 事件队列。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from metahub_shared.runtime.webhook_delivery import dispatch_runtime_webhook_event
from metahub_shared.runtime.webhooks import WebhookEvent

logger = logging.getLogger("metahub.gateway.events")
# 队列容量限制单实例内存占用；满载时宁可丢事件告警，也不能反压模型请求主链路。
QUEUE_MAX_SIZE = 128
# 进程退出最多等待五秒排空，超过上限取消 worker，避免部署滚动升级长期卡住。
SHUTDOWN_DRAIN_SECONDS = 5.0


def install_runtime_event_dispatcher(app: FastAPI) -> None:
    """把事件 worker 组合进 Gateway 现有 lifespan，不覆盖配置加载与其他生命周期。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原应用启动完成后创建队列，退出时有界排空已接收事件。"""

        async with original_lifespan(current_app):
            queue: asyncio.Queue[WebhookEvent | None] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
            current_app.state.runtime_event_queue = queue
            worker = asyncio.create_task(_event_worker(current_app, queue), name="gateway-runtime-events")
            try:
                yield
            finally:
                # 先放入哨兵停止接收循环；队列满时等待已有事件腾出一个槽，但总退出仍有硬上限。
                try:
                    await asyncio.wait_for(queue.put(None), timeout=1.0)
                    await asyncio.wait_for(worker, timeout=SHUTDOWN_DRAIN_SECONDS)
                # 排空超过硬上限时主动取消后台任务，保证 Gateway 能按时退出。
                except TimeoutError:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                finally:
                    current_app.state.runtime_event_queue = None

    app.router.lifespan_context = combined_lifespan


def enqueue_runtime_event(app_state: object, event: WebhookEvent) -> bool:
    """用 put_nowait 投递事件；队列缺失或已满时只告警，不反压模型调用。"""

    queue = getattr(app_state, "runtime_event_queue", None)
    if not isinstance(queue, asyncio.Queue):
        _log_enqueue_drop(event, "worker_unavailable")
        return False
    try:
        queue.put_nowait(event)
        return True
    except asyncio.QueueFull:
        _log_enqueue_drop(event, "queue_full")
        return False


async def _event_worker(app: FastAPI, queue: asyncio.Queue[WebhookEvent | None]) -> None:
    """串行消费事件并把同步数据库/HTTP 投递放入线程，避免阻塞 ASGI 事件循环。"""

    while True:
        event = await queue.get()
        try:
            if event is None:
                return
            try:
                await asyncio.to_thread(dispatch_runtime_webhook_event, app.state.config, event)
            except Exception:
                # 原始异常可能包含 URL 或请求对象，只记录固定代码和事件 ID。
                logger.warning(
                    "route.runtime_webhook_failed",
                    extra={
                        "event": "route.runtime_webhook_failed",
                        "tenant_id": event.tenant_id,
                        "trace_id": event.trace_id,
                        "object_id": event.event_id,
                        "payload": {
                            "event_type": event.event_type,
                            "error_code": "webhook.delivery_unavailable",
                        },
                    },
                )
        finally:
            queue.task_done()


def _log_enqueue_drop(event: WebhookEvent, reason: str) -> None:
    """记录安全丢弃原因，不输出事件 payload 以降低未来字段扩展的泄露风险。"""

    logger.warning(
        "route.runtime_event_dropped",
        extra={
            "event": "route.runtime_event_dropped",
            "tenant_id": event.tenant_id,
            "trace_id": event.trace_id,
            "object_id": event.event_id,
            "payload": {"event_type": event.event_type, "reason": reason},
        },
    )
