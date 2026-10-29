"""PRD-073 文件安全扫描、报告读取与 CSV 导出路由。

所有入口先执行后端 Session/RBAC dependency，再解析 path/query/body。扫描容量在 worker 读取并
解密企业 Blob 前取得；tenant 和 actor 始终来自 AuthContext，客户端没有 owner 覆盖字段。
"""

from __future__ import annotations

from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import (
    AuthContext,
    require_file_reader,
    require_file_security_scanner,
)

from .models import FileSecurityReportResponse
from .scheduler import wake_file_security_event_scheduler
from .service import (
    OcrBackendPool,
    export_security_report_csv,
    get_security_report,
    scan_security_report,
)

router = APIRouter(prefix="/files", tags=["file-security"])


@router.post(
    "/{file_id}/security-scan",
    response_model=FileSecurityReportResponse,
)
async def post_file_security_scan(
    file_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_file_security_scanner)],
) -> FileSecurityReportResponse:
    """扫描当前 tenant 的 ready 文件，或返回同 detector 权威报告的幂等复用结果。"""

    _reject_query(request)
    await _require_empty_body(request)
    limiter = _scan_limiter(request)
    acquired = False
    try:
        try:
            # 容量在 worker 解密原文件前取得；排队超时不会接触其他 tenant 的正文或 OCR 模型。
            with anyio.fail_after(2.0):
                await limiter.acquire()
                acquired = True
        # 扫描容量耗尽必须在解密前返回固定限流错误，不能继续排队并接触租户正文。
        except TimeoutError:
            raise ApiError("file.security_scan_busy") from None
        request_id = _request_id(request)
        trace_id = _trace_id(request)
        # 池的首次创建必须在事件循环 task 内完成。若放进 worker lambda，两个冷启动请求可能
        # 在不同线程同时观察到空 state，并各自加载一套离线 OCR 模型，突破部署容量与内存预算。
        ocr_backend = _ocr_pool(request)
        result, event = await anyio.to_thread.run_sync(
            lambda: scan_security_report(
                request.app.state.config,
                context,
                file_id,
                request_id=request_id,
                trace_id=trace_id,
                ocr_backend=ocr_backend,
            )
        )
        if event is not None:
            # 0066 trigger 已与 source 同事务写入 pending intent；路由只唤醒持久 worker，
            # 即使进程在此处退出，重启后的启动 drain 仍会恢复同一个 fsev_ 事件。
            wake_file_security_event_scheduler(request.app)
        _no_store(response)
        return result
    finally:
        if acquired:
            # anyio limiter 必须由取得 token 的同一 ASGI task 释放，worker 不操作 event-loop 容量。
            limiter.release()


@router.get(
    "/{file_id}/security-report",
    response_model=FileSecurityReportResponse,
)
async def get_file_security_report(
    file_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_file_reader)],
) -> FileSecurityReportResponse:
    """读取当前 detector 权威报告，或没有权威结果时读取最新 failed attempt。"""

    _reject_query(request)
    await _require_empty_body(request)
    result = await anyio.to_thread.run_sync(
        lambda: get_security_report(
            request.app.state.config,
            context,
            file_id,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    )
    _no_store(response)
    return result


@router.get("/{file_id}/security-report.csv")
async def get_file_security_report_csv(
    file_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_file_reader)],
) -> Response:
    """导出与 JSON 同一 canonical 报告的固定列 UTF-8 CSV，并禁止缓存敏感报告。"""

    _reject_query(request)
    await _require_empty_body(request)
    filename, content = await anyio.to_thread.run_sync(
        lambda: export_security_report_csv(
            request.app.state.config,
            context,
            file_id,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    )
    return Response(
        content=content,
        status_code=200,
        headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _scan_limiter(request: Request) -> anyio.CapacityLimiter:
    """为应用实例惰性创建部署固定容量的安全扫描 limiter。"""

    configured = request.app.state.config.file_security_scan_concurrency
    existing = getattr(request.app.state, "file_security_scan_limiter", None)
    if existing is None:
        existing = anyio.CapacityLimiter(configured)
        request.app.state.file_security_scan_limiter = existing
    # limiter 类型或容量漂移意味着运行期预算被改写，必须失败关闭并要求重启生效。
    if not isinstance(existing, anyio.CapacityLimiter) or existing.total_tokens != configured:
        # 运行期拆分 limiter 会使租户并发预算失真，配置变化必须通过重启应用生效。
        raise ApiError("file.security_scan_unavailable")
    return existing


def _ocr_pool(request: Request) -> OcrBackendPool:
    """惰性创建进程级 OCR 后端池；纯文本扫描不会调用池内模型。"""

    configured = request.app.state.config.file_security_ocr_concurrency
    existing = getattr(request.app.state, "file_security_ocr_pool", None)
    if existing is None:
        existing = OcrBackendPool(configured)
        request.app.state.file_security_ocr_pool = existing
        request.app.state.file_security_ocr_pool_capacity = configured
    if (
        not isinstance(existing, OcrBackendPool)
        or getattr(request.app.state, "file_security_ocr_pool_capacity", None) != configured
    ):
        # 后端池容量漂移会混合不同部署语义，不能在请求期动态替换已加载模型。
        raise ApiError("file.security_scan_unavailable")
    return existing


def _reject_query(request: Request) -> None:
    """扫描、报告和导出均无 query 合同，尤其禁止 tenant/actor/阈值注入。"""

    # 任意 query 都可能试图覆盖 tenant、actor 或检测阈值，因此合同外字段一律拒绝。
    if request.query_params.multi_items():
        raise ApiError("infra.validation_failed")


async def _require_empty_body(request: Request) -> None:
    """拒绝所有请求正文；首个非空 chunk 即停止，避免无业务语义的敏感输入。"""

    try:
        async for chunk in request.stream():
            # 正文没有业务语义，首个非空或非 bytes chunk 即按统一输入错误停止消费。
            if type(chunk) is not bytes or chunk:
                raise ApiError("infra.validation_failed")
    # 已归类的输入错误必须原样交给统一中间件，不能被底层断流分支重新包装。
    except ApiError:
        raise
    except Exception:
        # ASGI 断流或类型异常不回显底层对象，统一按输入合同失败。
        raise ApiError("infra.validation_failed") from None


def _no_store(response: Response) -> None:
    """禁止浏览器或共享代理缓存租户文件安全报告。"""

    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """读取统一中间件生成的 request_id，不接受 path/body/query 覆盖。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的 trace_id，供报告与 source event 共享。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
