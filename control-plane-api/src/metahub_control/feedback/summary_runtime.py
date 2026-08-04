"""PRD-049 反馈汇总的异步资源门禁与 endpoint deadline 执行器。

SQLite 汇总是同步 CPU/磁盘工作，不能直接运行在 ASGI 事件循环中。本模块把它放入
工作线程，并在单个 Control API 进程内限制为一个并发扫描，避免多个租户的报表请求
同时争用 SQLite、JSON 展开和默认线程池。门禁等待与工作线程共享调用方给出的绝对
单调时钟截止点，排队不会获得一份新的 250ms 预算。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import TypeVar

from fastapi import FastAPI
from metahub_shared.runtime.errors import ApiError

# SQLite 是当前控制面的单文件共享资源；单并发可防止多条 JSON 聚合互相放大尾延迟。
FEEDBACK_SUMMARY_MAX_CONCURRENCY = 1
_APP_STATE_ATTRIBUTE = "feedback_summary_runtime"

_ResultT = TypeVar("_ResultT")


class FeedbackSummaryRuntime:
    """在有界并发槽中把同步 Summary 操作移出事件循环。

    参数 ``max_concurrency`` 是进程内同时运行的 Summary 上限，当前生产合同固定为 1；
    ``monotonic`` 只供确定性测试注入。``run`` 返回同步操作结果，并把排队、线程调度和
    执行都纳入同一个绝对 deadline。请求取消或超时后，正在运行的线程不能被 Python
    强制终止，因此槽位会一直保留到线程真正退出，防止后续请求与遗留扫描重叠。
    """

    def __init__(
        self,
        *,
        max_concurrency: int = FEEDBACK_SUMMARY_MAX_CONCURRENCY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建不启动线程的惰性门禁；实例应绑定一个 FastAPI 应用和事件循环。"""

        # 并发上限属于进程资源边界，拒绝 bool、零值和负值，不能退化成无界或永久阻塞。
        if type(max_concurrency) is not int or max_concurrency < 1:
            # 构造参数只来自后端装配；使用固定异常文本，禁止携带请求或租户内容。
            raise ValueError("max_concurrency must be a positive integer")
        self._slots = asyncio.Semaphore(max_concurrency)
        self._monotonic = monotonic

    async def run(
        self,
        operation: Callable[[float], _ResultT],
        *,
        monotonic_deadline: float,
    ) -> _ResultT:
        """在 deadline 内取得槽位，并在线程中执行接收同一 deadline 的同步操作。

        参数 ``operation`` 只能接收一个绝对 monotonic deadline，负责把它继续传给
        SQLite progress handler；``monotonic_deadline`` 必须是有限实数。返回 operation
        的结果。排队或执行超时统一抛出 ``feedback.unavailable``，不暴露线程或数据库
        状态；外部取消继续传播，但仍等待后台线程完成后才归还槽位。
        """

        deadline = self._normalize_deadline(monotonic_deadline)
        acquired = False
        release_on_worker_completion = False
        worker: asyncio.Task[_ResultT] | None = None
        try:
            try:
                # wait_for 的时间也从 endpoint 绝对截止点扣除，排队不能刷新预算。
                await asyncio.wait_for(
                    self._slots.acquire(),
                    timeout=self._remaining_seconds(deadline),
                )
                acquired = True
            # 槽位排队超过 endpoint deadline 时不创建工作线程，直接返回统一依赖不可用。
            except TimeoutError as exc:
                # 只公开稳定错误码，不能暴露当前活跃租户、队列长度或线程状态。
                raise ApiError("feedback.unavailable") from exc

            # to_thread 只在取得唯一槽位后创建，避免超时 waiter 堆积默认线程池任务。
            worker = asyncio.create_task(
                asyncio.to_thread(operation, deadline),
                name="control-feedback-summary",
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=self._remaining_seconds(deadline),
                )
            # 工作线程未在剩余预算内退出时及时结束 HTTP waiter，但保留真实资源占用。
            except TimeoutError as exc:
                # shield 让 SQLite progress handler 自行按同一 deadline 中断；完成前不能
                # 释放槽位，否则遗留扫描会与下一条 Summary 并发争用数据库。
                worker.add_done_callback(self._release_after_worker)
                release_on_worker_completion = True
                raise ApiError("feedback.unavailable") from exc
            # 小结果仍可能在线程返回与事件循环恢复之间跨过 deadline，必须最后复核。
            if self._monotonic() >= deadline:
                raise ApiError("feedback.unavailable")
            return result
        # 创建 worker 后若在计算剩余时间时发现预算耗尽，也必须等线程退出再归还槽位。
        except ApiError:
            if (
                acquired
                and worker is not None
                and not worker.done()
                and not release_on_worker_completion
            ):
                # 此分支覆盖 to_thread 已排队、但 wait_for 尚未来得及接管的极窄竞态窗口。
                worker.add_done_callback(self._release_after_worker)
                release_on_worker_completion = True
            raise
        # 客户端取消与内部 deadline 不同：继续传播取消，同时维护槽位的真实占用周期。
        except asyncio.CancelledError:
            if acquired and worker is not None and not worker.done():
                # 客户端断开不能取消运行中的 Python 线程；由回调吞掉后台异常并延迟归还槽位。
                worker.add_done_callback(self._release_after_worker)
                release_on_worker_completion = True
            raise
        finally:
            if acquired and not release_on_worker_completion:
                self._slots.release()

    def _release_after_worker(self, worker: asyncio.Task[object]) -> None:
        """消费脱离请求生命周期的 worker 结果，并在真实退出后归还并发槽位。"""

        try:
            # 读取 exception 防止超时响应后出现“Task exception was never retrieved”告警；
            # 原始异常可能含数据库路径或 SQL，不能在这里写日志。
            worker.exception()
        except asyncio.CancelledError:
            pass
        finally:
            self._slots.release()

    def _normalize_deadline(self, value: float) -> float:
        """验证内部绝对 deadline，并把已耗尽预算折叠为统一不可用错误。"""

        # deadline 只由后端路由产生；异常类型仍要 fail closed，不能落入 asyncio 无界等待。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # 统一折叠为暂不可用，避免把内部调度参数误报成客户端 query 错误。
            raise ApiError("feedback.unavailable")
        deadline = float(value)
        # NaN/Infinity 会破坏大小比较，已耗尽的绝对时间也不能刷新为一份新预算。
        if not math.isfinite(deadline) or deadline <= self._monotonic():
            # 调用方只能重试完整 endpoint，不能获知进程单调时钟或剩余槽位信息。
            raise ApiError("feedback.unavailable")
        return deadline

    def _remaining_seconds(self, deadline: float) -> float:
        """返回严格为正的剩余秒数，避免 asyncio 把耗尽预算解释成无界等待。"""

        remaining = deadline - self._monotonic()
        # 每次 await 前重新计算；事件循环调度延迟同样属于 endpoint 预算。
        if remaining <= 0:
            # 禁止把零/负 timeout 传给后续资源操作后再重新计时。
            raise ApiError("feedback.unavailable")
        return remaining


def get_feedback_summary_runtime(app: FastAPI) -> FeedbackSummaryRuntime:
    """取得当前应用独享的 Summary 门禁，首次合法请求时惰性创建。

    FastAPI 应用可能在同一测试进程创建多个实例；把门禁放在 ``app.state`` 可避免不同
    应用/事件循环误共享 Semaphore。事件循环在两次读取之间不会切换任务，因此无需用
    线程锁保护惰性初始化。
    """

    runtime = getattr(app.state, _APP_STATE_ATTRIBUTE, None)
    if isinstance(runtime, FeedbackSummaryRuntime):
        return runtime
    runtime = FeedbackSummaryRuntime()
    setattr(app.state, _APP_STATE_ATTRIBUTE, runtime)
    return runtime


__all__ = [
    "FEEDBACK_SUMMARY_MAX_CONCURRENCY",
    "FeedbackSummaryRuntime",
    "get_feedback_summary_runtime",
]
