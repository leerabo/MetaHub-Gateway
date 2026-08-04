"""PRD-069 手工敏感意图分类 Control API 路由。

路由不声明 Pydantic body 参数：FastAPI 必须先完成 Session 与专用 RBAC dependency，之后
才手工读取敏感正文。分类和 SQLite 写事务在线程池执行，Webhook 在响应提交后执行。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from metahub_control.auth import AuthContext, require_intent_risk_classifier

from .models import IntentClassificationResponse
from .service import classify_manual_intent, dispatch_intent_event_safely
from .wire import INTENT_CLASSIFICATION_OPENAPI, read_intent_classification_text

router = APIRouter(tags=["intent-security"])


@router.post(
    "/security/classify-intent",
    response_model=IntentClassificationResponse,
    openapi_extra=INTENT_CLASSIFICATION_OPENAPI,
)
async def post_intent_classification(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    context: Annotated[AuthContext, Depends(require_intent_risk_classifier)],
) -> IntentClassificationResponse:
    """分类一次性文本并返回已提交安全摘要。

    参数：请求、响应头容器、响应后任务与实时权限上下文。返回：不含正文的分类结果。
    约束：拒绝全部 query；正文只在权限通过后读取，分类完成后立即清理局部引用。
    """

    if request.query_params:
        # 该命令没有租户、阈值或模式 query，拒绝全部参数避免旧服务静默忽略安全开关。
        from metahub_shared.runtime.errors import ApiError

        raise ApiError("intent_risk.payload_invalid")
    response.headers["Cache-Control"] = "no-store"
    sensitive_text = await read_intent_classification_text(request)
    request_id = _request_id(request)
    trace_id = _trace_id(request)
    try:
        result, event = await asyncio.to_thread(
            classify_manual_intent,
            request.app.state.config,
            context,
            (sensitive_text,),
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        # Python 字符串不可原位擦除；覆盖唯一业务局部引用，禁止传入日志、异常或后台任务。
        sensitive_text = ""
    if event is not None:
        # BackgroundTasks 只持有安全事件；Prompt 正文在创建任务前已从所有局部容器释放。
        background_tasks.add_task(
            dispatch_intent_event_safely,
            request.app.state.config,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    return result


def _request_id(request: Request) -> str:
    """读取共享 middleware 为敏感写入口新生成的内部 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取共享 middleware 为敏感写入口新生成的内部 trace_id。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
