"""Control Session 的反馈创建与满意度汇总 HTTP 路由。

路由不声明 Pydantic Body/Query：FastAPI 必须先完成 Session RBAC，再读取不可信正文
或解析查询参数。双 POST 入口的 JSON、幂等头规则由 shared feedback 包统一实现。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.db.connection import database_request_scope, release_idle_request_connections
from metahub_shared.feedback import (
    FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS,
    MAX_FEEDBACK_BODY_BYTES,
    parse_feedback_json_bytes,
    resolve_feedback_operation_request_id,
)
from metahub_shared.runtime.errors import ApiError
from starlette.concurrency import run_in_threadpool

from metahub_control.auth import (
    AuthContext,
    require_feedback_summary_user,
)
from metahub_control.auth.dependencies import require_workspace_or_legacy_permissions

from .models import FeedbackMutationResponse, FeedbackSummaryResponse
from .service import create_session_feedback, summarize_session_feedback
from .summary_runtime import get_feedback_summary_runtime

router = APIRouter(prefix="/feedback", tags=["feedback"])

# Summary 仅接受四个筛选/分页字段；tenant 与 actor 永远来自认证上下文。
_SUMMARY_QUERY_FIELDS = frozenset({"model_id", "from_time", "to_time", "limit", "offset"})
# 秒精度 canonical UTC 文本用于 SQLite TEXT 闭开区间比较，禁止 offset 与小数秒歧义。
_CANONICAL_UTC_PATTERN = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z", re.ASCII)
# 稳定模型 ID 只允许有界 ASCII 标识字符，禁止把正文或控制字符带入查询与日志。
_MODEL_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)
# 默认三十天与最大三百六十六天窗口冻结报表资源边界。
_DEFAULT_WINDOW = timedelta(days=30)
_MAX_WINDOW = timedelta(days=366)
@dataclass(frozen=True)
class _SummaryQuery:
    """认证后解析出的租户反馈汇总查询。

    时间均为秒精度 UTC；模型 ID 可空；分页在进入 shared 单快照查询前已经完成
    ASCII 整数和范围校验，避免数据库层接受框架自动转换的宽松表示。
    """

    model_id: str | None
    from_time: datetime
    to_time: datetime
    limit: int
    offset: int


@router.post("", response_model=FeedbackMutationResponse, status_code=201)
async def post_feedback(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext, Depends(require_workspace_or_legacy_permissions("feedback.use"))
    ],
) -> FeedbackMutationResponse:
    """由 admin/developer 对当前租户的一次成功模型调用提交不可变反馈。

    权限依赖先于本函数执行；随后先校验 raw ``X-Request-ID``，再有界读取正文并交给
    shared 规范化。首次创建返回 201，同 actor/操作/意图恢复返回 200。
    """

    operation_request_id = resolve_feedback_operation_request_id(
        request.scope.get("headers", ()),
        _request_id(request),
    )
    body = await _read_feedback_body(request)
    intent = parse_feedback_json_bytes(
        body,
        content_type=request.headers.get("content-type"),
        content_length=request.headers.get("content-length"),
    )
    result = await run_in_threadpool(
        create_session_feedback,
        request.app.state.config,
        context,
        operation_request_id=operation_request_id,
        trace_id=_trace_id(request),
        intent=intent,
    )
    # 幂等恢复不创建第二条事实，HTTP 200 显式区别于首次提交的 201。
    response.status_code = 200 if result.idempotent_replay else 201
    response.headers["Cache-Control"] = "no-store, private"
    return result


@router.get("/summary", response_model=FeedbackSummaryResponse)
async def get_feedback_summary(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_feedback_summary_user)],
) -> FeedbackSummaryResponse:
    """查询当前 Session 租户在确定 UTC 窗口内的满意度与采纳率。

    viewer/匿名请求在解析畸形 query 前即被依赖拒绝；合法查询委托 shared 在单连接、
    单只读快照和 endpoint-wide 250ms deadline 内完成全部总计、标签与模型分页。
    """

    # 专用认证依赖从中间件进入时刻冻结 deadline；这里仅恢复同一个值，不能在完成
    # Session/RBAC 后重新获得 250ms。fallback 只服务绕过 ASGI 依赖的直接函数测试。
    monotonic_deadline = getattr(
        request.state,
        "feedback_summary_deadline",
        time.monotonic() + FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS,
    )
    query = _read_summary_query(request)
    response.headers["Cache-Control"] = "no-store, private"

    def build_summary(worker_deadline: float) -> FeedbackSummaryResponse:
        """在线程内完成 tenant-scoped SQLite 查询和 DTO 映射，并沿用 endpoint deadline。"""

        # DTO 构造和 tenant scope 均由 service 完成，路由不得从 query 补入租户或 actor。
        # HTTP timeout can precede worker exit. Only the worker may return its
        # connection to the pool; never inherit the request's shorter lifetime.
        with database_request_scope(reuse_connections=True):
            return summarize_session_feedback(
                request.app.state.config,
                context,
                model_id=query.model_id,
                from_time=query.from_time,
                to_time=query.to_time,
                limit=query.limit,
                offset=query.offset,
                request_id=_request_id(request),
                trace_id=_trace_id(request),
                monotonic_deadline=worker_deadline,
            )

    # Authentication has committed; make its warm handle available without
    # coupling the independently owned worker to the HTTP request's teardown.
    release_idle_request_connections()
    result = await get_feedback_summary_runtime(request.app).run(
        build_summary,
        monotonic_deadline=monotonic_deadline,
    )
    # Header 设置也属于 endpoint 处理；超时后不能因为查询刚好返回而误发成功响应。
    if time.monotonic() >= monotonic_deadline:
        raise ApiError("feedback.unavailable")
    return result


async def _read_feedback_body(request: Request) -> bytes:
    """有界读取反馈正文，防止伪造或缺失 Content-Length 绕过 4096 字节硬上限。

    参数 ``request`` 为已通过认证/RBAC 的 ASGI 请求；返回原始 bytes 供 shared 严格
    UTF-8/JSON 解析。越界立即中止且不保存、记录或回显任何正文片段。
    """

    body = bytearray()
    async for chunk in request.stream():
        # 实际流才是资源硬边界；超过上限时不保存或回显已接收的正文片段。
        if len(body) + len(chunk) > MAX_FEEDBACK_BODY_BYTES:
            raise ApiError("feedback.input_invalid")
        body.extend(chunk)
    return bytes(body)


def _read_summary_query(request: Request) -> _SummaryQuery:
    """严格解析白名单 summary query，拒绝未知/重复字段与宽松数字时间表示。"""

    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        # first/last 参数语义在代理间不一致；未知和重复参数必须整体拒绝。
        if key not in _SUMMARY_QUERY_FIELDS or key in values:
            raise ApiError("feedback.query_invalid")
        values[key] = value
    model_id = values.get("model_id")
    # model_id 必须按原文校验，禁止 strip/替换后意外命中另一个稳定资源。
    if model_id is not None and _MODEL_ID_PATTERN.fullmatch(model_id) is None:
        raise ApiError("feedback.query_invalid")
    raw_from = values.get("from_time")
    raw_to = values.get("to_time")
    # 显式窗口必须成对出现，不能用服务端 now 猜测缺失的一侧。
    if (raw_from is None) != (raw_to is None):
        raise ApiError("feedback.query_invalid")
    if raw_from is None:
        to_time = datetime.now(UTC).replace(microsecond=0)
        from_time = to_time - _DEFAULT_WINDOW
    else:
        from_time = _parse_canonical_utc(raw_from)
        to_time = _parse_canonical_utc(raw_to or "")
        # 闭开窗口必须正向且有硬上限，避免无界扫描争用控制面数据库。
        if from_time >= to_time or to_time - from_time > _MAX_WINDOW:
            raise ApiError("feedback.query_invalid")
    return _SummaryQuery(
        model_id=model_id,
        from_time=from_time,
        to_time=to_time,
        limit=_query_integer(values.get("limit"), default=20, minimum=1, maximum=100),
        offset=_query_integer(values.get("offset"), default=0, minimum=0, maximum=10_000),
    )


def _parse_canonical_utc(value: str) -> datetime:
    """解析秒精度 ``YYYY-MM-DDTHH:MM:SSZ``，拒绝 offset、小数秒和无效日期。"""

    # 先检查机器格式，再调用日期解析器校验闰年、月份和时分秒实际范围。
    if _CANONICAL_UTC_PATTERN.fullmatch(value) is None:
        raise ApiError("feedback.query_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # 底层日期异常不得回显原始 query，统一折叠为公开查询错误。
    except ValueError as exc:
        raise ApiError("feedback.query_invalid") from exc


def _query_integer(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    """解析有界 ASCII 十进制分页值，不接受符号、空白、指数或 Unicode 数字。"""

    if value is None:
        return default
    # API wire 固定 ASCII 十进制，拒绝全角数字、符号、空白和指数表示。
    if not value or any(character < "0" or character > "9" for character in value):
        raise ApiError("feedback.query_invalid")
    parsed = int(value)
    # 分页界限限制模型行扫描和 offset 跳过成本，防止报表拖慢控制面。
    if parsed < minimum or parsed > maximum:
        raise ApiError("feedback.query_invalid")
    return parsed


def _request_id(request: Request) -> str:
    """读取统一中间件生成或验证后的本次操作 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的本次 HTTP trace_id；重放响应仍使用该值。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
