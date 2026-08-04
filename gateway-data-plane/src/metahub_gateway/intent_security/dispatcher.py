"""Gateway 敏感意图分类的有界进程内 worker 生命周期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from metahub_gateway.events import enqueue_runtime_event

from .models import IntentClassificationJob
from .service import process_intent_job

logger = logging.getLogger("metahub.gateway.intent_security.dispatcher")

# 固定容量限制企业 Prompt 在单进程内的最坏内存占用，禁止按租户或输入大小动态扩容。
QUEUE_MAX_SIZE = 128
# 滚动退出最多等待五秒；超时取消只丢失 best-effort 旁路，不改变已返回的模型响应。
SHUTDOWN_DRAIN_SECONDS = 5.0


def install_intent_security_dispatcher(app: FastAPI) -> None:
    """把 intent worker 组合在 runtime event dispatcher 外层。

    参数：已安装 Webhook event dispatcher 的 Gateway 应用。返回值：无。关键约束：启动时
    Webhook queue 先可用；退出时先排空 intent job，再由内层生命周期排空新产生的事件。
    """

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """在内层生命周期启动后创建队列，并在退出时有界排空或清理未完成任务。"""

        async with original_lifespan(current_app):
            queue: asyncio.Queue[IntentClassificationJob | None] = asyncio.Queue(
                maxsize=QUEUE_MAX_SIZE
            )
            current_app.state.intent_classification_queue = queue
            worker = asyncio.create_task(
                _intent_worker(current_app, queue),
                name="gateway-intent-classifier",
            )
            try:
                yield
            finally:
                try:
                    # 哨兵严格排在已接收 job 后；队列满时等待 worker 腾位，但退出仍有硬上限。
                    await asyncio.wait_for(queue.put(None), timeout=1.0)
                    await asyncio.wait_for(worker, timeout=SHUTDOWN_DRAIN_SECONDS)
                except TimeoutError:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    _discard_pending_jobs(queue)
                finally:
                    # 应用状态先断开入口，再退出内层 Webhook 生命周期，禁止停机后继续接收 job。
                    current_app.state.intent_classification_queue = None

    app.router.lifespan_context = combined_lifespan


async def _intent_worker(
    app: FastAPI,
    queue: asyncio.Queue[IntentClassificationJob | None],
) -> None:
    """串行消费任务，把 CPU 分类和 SQLite 事务放到线程，并在事件循环中投递 Webhook。"""

    while True:
        job = await queue.get()
        try:
            if job is None:
                return
            event = await asyncio.to_thread(process_intent_job, app.state.config, job)
            if event is not None:
                # asyncio.Queue 只在所属事件循环线程操作；Webhook 失败不反压已完成的主请求。
                enqueue_runtime_event(app.state, event)
        except asyncio.CancelledError:
            # 取消必须保留给 lifespan；finally 仍会删除当前局部 job 引用并维护 queue 计数。
            raise
        except Exception as exc:
            # worker 编排异常只记录固定码和类型，不能输出可能含正文的 job 或 traceback。
            logger.warning(
                "security.intent_worker_failed",
                extra={
                    "event": "security.intent_worker_failed",
                    "object_id": getattr(job, "intent_risk_result_id", None),
                    "error_code": "intent_risk.worker_failed",
                    "payload": {"exception_type": exc.__class__.__name__},
                },
            )
        finally:
            queue.task_done()
            # 显式删除局部强引用，避免 worker 循环在空闲期继续持有上一条正文。
            del job


def _discard_pending_jobs(queue: asyncio.Queue[IntentClassificationJob | None]) -> None:
    """停机超时后清空队列引用，使尚未执行任务的正文可以立即释放。"""

    while True:
        try:
            # 返回值不绑定到局部变量，使尚未执行任务的正文引用在本语句后立即释放。
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        # 每次成功取走一个任务都必须平衡 unfinished_tasks，确保有界排空可以结束。
        queue.task_done()


__all__ = ["QUEUE_MAX_SIZE", "SHUTDOWN_DRAIN_SECONDS", "install_intent_security_dispatcher"]
