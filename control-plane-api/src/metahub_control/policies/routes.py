"""策略管理 HTTP 路由。

五个入口统一依赖后端 admin 守卫，并把 request_id/trace_id、审计来源和认证租户
交给服务层。路由不会接受 tenant_id、AST、hash 或服务端版本字段。
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_policy_reader,
    require_policy_simulator,
    require_policy_writer,
)

from .dry_run_service import recover_completed_dry_run, run_policy_dry_run
from .models import (
    CreatePolicyRequest,
    PolicyDryRunRequest,
    PolicyDryRunResponse,
    PolicyListResponse,
    PolicyResponse,
    PolicyValidationResponse,
    UpdatePolicyRequest,
    ValidatePolicyRequest,
)
from .service import create_policy, list_policies, update_policy, validate_policy

router = APIRouter(prefix="/policies", tags=["policies"])

# 正文、滑动窗口和 key 表硬上限共同保护单进程 CPU，状态中不会保存 Prompt。
MAX_DRY_RUN_BODY_BYTES = 256 * 1024
DRY_RUN_WINDOW_SECONDS = 60.0
DRY_RUN_ACTOR_LIMIT = 30
DRY_RUN_TENANT_LIMIT = 120
DRY_RUN_MAX_ACTOR_KEYS = 4096
DRY_RUN_MAX_TENANT_KEYS = 1024


class _DuplicateJsonKey(ValueError):
    """JSON object 出现重复键；异常不保存具体 key，避免正文进入日志或错误信息。"""


class _DryRunLimiter:
    """进程内有界滑动窗口限流器，状态只含可信 tenant/actor ID 和单调时钟。"""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        """初始化两个独立有界 key 表；锁保证并发检查与计数是一个原子步骤。"""

        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._actors: OrderedDict[str, deque[float]] = OrderedDict()
        self._tenants: OrderedDict[str, deque[float]] = OrderedDict()

    def acquire(self, tenant_id: str, actor_id: str) -> bool:
        """同时满足 actor 30 RPM 与 tenant 120 RPM 时登记一次 CPU 额度。"""

        now = self._monotonic()
        cutoff = now - DRY_RUN_WINDOW_SECONDS
        with self._lock:
            actor_events = self._window(
                self._actors,
                actor_id,
                cutoff=cutoff,
                maximum_keys=DRY_RUN_MAX_ACTOR_KEYS,
            )
            tenant_events = self._window(
                self._tenants,
                tenant_id,
                cutoff=cutoff,
                maximum_keys=DRY_RUN_MAX_TENANT_KEYS,
            )
            if (
                actor_events is None
                or tenant_events is None
                or len(actor_events) >= DRY_RUN_ACTOR_LIMIT
                or len(tenant_events) >= DRY_RUN_TENANT_LIMIT
            ):
                return False
            actor_events.append(now)
            tenant_events.append(now)
            return True

    def clear(self) -> None:
        """只供隔离测试清理进程状态，不改变生产请求路径。"""

        with self._lock:
            self._actors.clear()
            self._tenants.clear()

    @staticmethod
    def _window(
        windows: OrderedDict[str, deque[float]],
        key: str,
        *,
        cutoff: float,
        maximum_keys: int,
    ) -> deque[float] | None:
        """清理当前 key 与过期 LRU key，并在硬上限耗尽时失败关闭。"""

        events = windows.get(key)
        if events is None:
            # 从最旧 key 开始只删除窗口已完全过期的状态，不能驱逐仍在计数的调用者。
            for old_key in tuple(windows):
                old_events = windows[old_key]
                while old_events and old_events[0] <= cutoff:
                    old_events.popleft()
                if old_events:
                    continue
                del windows[old_key]
                if len(windows) < maximum_keys:
                    break
            if len(windows) >= maximum_keys:
                return None
            events = deque()
            windows[key] = events
        else:
            windows.move_to_end(key)
        while events and events[0] <= cutoff:
            events.popleft()
        return events


_dry_run_limiter = _DryRunLimiter()


@router.get("", response_model=PolicyListResponse)
async def get_policies(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_reader)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    action: Annotated[str | None, Query()] = None,
) -> PolicyListResponse:
    """读取当前租户策略，显式把原始 query key 交给服务层做 allowlist 校验。"""

    _set_no_store(response)
    return list_policies(
        request.app.state.config,
        context,
        status=status_filter,
        action=action,
        query_keys=tuple(request.query_params.keys()),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/validate",
    response_model=PolicyValidationResponse,
    response_model_exclude_none=True,
)
async def post_policy_validation(
    payload: ValidatePolicyRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_writer)],
) -> PolicyValidationResponse:
    """校验一个无副作用草稿，并在领域失败时记录最小化失败事件。"""

    _set_no_store(response)
    return validate_policy(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/dry-run",
    response_model=PolicyDryRunResponse,
    response_model_exclude_none=False,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": PolicyDryRunRequest.model_json_schema(),
                }
            },
        }
    },
)
async def post_policy_dry_run(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_simulator)],
) -> PolicyDryRunResponse:
    """鉴权后有界读取单条当前请求上下文，并返回不产生真实模型成本的完整预演。"""

    _set_no_store(response)
    # 恢复优先于本地 CPU 限流和正文读取：旧事实即使当前配置损坏也必须能够重派稳定事件。
    recover_completed_dry_run(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    # actor/tenant 任一窗口耗尽都必须在读取 Prompt 前拒绝，且不写成功事实。
    if not _dry_run_limiter.acquire(context.user.tenant_id, context.user.user_id):
        raise ApiError("policy.dry_run_rate_limited")
    payload = await _read_dry_run_payload(request)
    return run_policy_dry_run(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        recovery_checked=True,
    )


@router.post("", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def post_policy(
    payload: CreatePolicyRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_writer)],
) -> PolicyResponse:
    """创建当前租户 Policy/Rule/Action，并原子写入审计与 created 事件。"""

    _set_no_store(response)
    result = create_policy(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.policy.version}"'
    return result


@router.patch("/{policy_id}", response_model=PolicyResponse)
async def patch_policy(
    policy_id: str,
    payload: UpdatePolicyRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_policy_writer)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> PolicyResponse:
    """按 If-Match 更新策略，事务内重新权威回读以阻止并发静默覆盖。"""

    _set_no_store(response)
    result = update_policy(
        request.app.state.config,
        context,
        policy_id,
        payload,
        if_match=if_match,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.policy.version}"'
    return result


def _set_no_store(response: Response) -> None:
    """禁止浏览器或代理缓存包含策略正文、诊断或版本信息的响应。"""

    response.headers["Cache-Control"] = "no-store"


def _request_id(request: Request) -> str:
    """读取共享中间件生成的 request_id，供响应、事件、审计和日志串联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取共享中间件生成的 trace_id，供跨服务排障使用。"""

    return getattr(request.state, "trace_id", "")


async def _read_dry_run_payload(request: Request) -> PolicyDryRunRequest:
    """流式读取最多 256 KiB 标准 JSON，并拒绝重复 key、常量、尾随内容和未知字段。

    该函数只在 ``require_policy_admin`` 成功后调用。任何解析/DTO 异常都折叠为固定错误码，
    不把 Pydantic input、JSON key、字节正文或 Unicode 解码片段带入错误响应和日志。
    """

    content_type = request.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    # 非 JSON 媒体类型不能进入宽松框架解析，统一按不含正文的 payload 错误处理。
    if media_type != "application/json":
        raise ApiError("policy.dry_run_payload_invalid")
    raw_length = request.headers.get("Content-Length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        # 非法 Content-Length 不应把框架或整数解析异常暴露给未可信正文。
        except ValueError:
            raise ApiError("policy.dry_run_payload_invalid") from None
        # 负长度没有合法 HTTP 正文语义，必须在开始流式读取前拒绝。
        if declared_length < 0:
            raise ApiError("policy.dry_run_payload_invalid")
        # 已声明超限时无需消费任何 Prompt 字节，立即返回专用 413。
        if declared_length > MAX_DRY_RUN_BODY_BYTES:
            raise ApiError("policy.dry_run_payload_too_large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_DRY_RUN_BODY_BYTES:
            # 超限后立即停止消费后续 ASGI chunk，限制恶意无界正文的内存和 CPU 占用。
            raise ApiError("policy.dry_run_payload_too_large")
        body.extend(chunk)
    try:
        text = bytes(body).decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        ValueError,
    ):
        # 解码、结构、递归或非标准 JSON 异常全部折叠，绝不拼接原始正文片段。
        raise ApiError("policy.dry_run_payload_invalid") from None
    # 顶层数组或标量无法表达严格 DTO，不能依赖 Pydantic 做含糊转换。
    if type(parsed) is not dict:
        raise ApiError("policy.dry_run_payload_invalid")
    try:
        return PolicyDryRunRequest.model_validate(parsed, strict=True)
    # Pydantic 原始 input 可能含 Prompt，异常只能转换为固定领域错误码。
    except ValidationError:
        raise ApiError("policy.dry_run_payload_invalid") from None


def _json_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """构造 JSON object；任何层级重复 key 都拒绝且不在异常中保存 key。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    """拒绝 Python JSON 解码器默认接受的 NaN/Infinity 非标准常量。"""

    # 异常只保留固定文本，不保存 NaN/Infinity 所在字段或周边正文。
    raise ValueError("non-standard JSON constant")


def _reset_dry_run_limiter_for_tests() -> None:
    """清理测试进程内限流状态；生产代码不调用该入口。"""

    _dry_run_limiter.clear()
