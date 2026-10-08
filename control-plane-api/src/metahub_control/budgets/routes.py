"""预算 API 路由、后端 RBAC 与严格 wire 解析。

所有路由都先完成认证依赖，再读取 query 或 body。这样无权限用户即使提交畸形 JSON、
重复字段或超限正文，也只会得到稳定 403，不能利用解析差异探测预算配置合同。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .models import (
    BudgetPeopleResponse,
    BudgetRuleListResponse,
    BudgetRuleResponse,
    BudgetStatusResponse,
    CreateBudgetRuleRequest,
    UpdateBudgetModeRequest,
    UpdateBudgetRuleRequest,
)
from .service import (
    create_budget_rule,
    get_budget_status,
    list_budget_rules,
    list_budget_people,
    update_budget_mode,
    upsert_budget_rule,
)

router = APIRouter(tags=["budgets"])
BudgetReader = Annotated[
    AuthContext, Depends(require_workspace_or_legacy_permissions("billing.budget.read"))
]
BudgetWriter = Annotated[
    AuthContext, Depends(require_workspace_or_legacy_permissions("billing.budget.write"))
]

# 预算查询和写请求采用显式白名单与硬上限，避免未知字段进入 SQL、审计或内存消耗面。
ALLOWED_BUDGET_LIST_QUERY_FIELDS = frozenset(
    {"scope_type", "environment_id", "environment", "status"}
)
ALLOWED_BUDGET_STATUS_QUERY_FIELDS = frozenset(
    {"virtual_key_id", "environment_id", "project_id", "application_id"}
)
ALLOWED_BUDGET_RULE_BODY_FIELDS = frozenset(
    {
        "scope_type",
        "scope_id",
        "environment_id",
        "environment",
        "name",
        "daily_budget_cny",
        "budget_period",
        "warning_threshold_percent",
        "exhausted_action",
        "status",
    }
)
ALLOWED_BUDGET_MODE_BODY_FIELDS = frozenset({"mode"})
MAX_BUDGET_BODY_BYTES = 16 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 64
BUDGET_RULE_REQUEST_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": UpdateBudgetRuleRequest.model_json_schema(),
            }
        },
    }
}
CREATE_BUDGET_RULE_REQUEST_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": CreateBudgetRuleRequest.model_json_schema(),
            }
        },
    }
}
BUDGET_MODE_REQUEST_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": UpdateBudgetModeRequest.model_json_schema(),
            }
        },
    }
}
PayloadModel = TypeVar("PayloadModel", bound=BaseModel)


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，防止不同解析器采用 first/last 不同语义。"""


@router.get("/budget-people", response_model=BudgetPeopleResponse)
def get_budget_people_route(
    request: Request,
    response: Response,
    context: BudgetReader,
) -> BudgetPeopleResponse:
    """按姓名或邮箱分页查员工；选定员工后读取其调用凭证安全元数据。"""

    query = _read_query(request, frozenset({"search", "user_id", "offset"}))
    try:
        offset = int(query.get("offset", "0"))
    except ValueError as exc:
        raise ApiError("budget.rule_invalid") from exc
    if offset < 0 or offset > 1_000_000:
        raise ApiError("budget.rule_invalid")
    response.headers["Cache-Control"] = "no-store"
    return list_budget_people(
        request.app.state.config,
        context,
        search=query.get("search"),
        user_id=query.get("user_id"),
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/budgets", response_model=BudgetRuleListResponse)
def get_budgets_route(
    request: Request,
    response: Response,
    context: BudgetReader,
) -> BudgetRuleListResponse:
    """读取当前租户预算规则与同快照今日状态。"""

    query = _read_query(request, ALLOWED_BUDGET_LIST_QUERY_FIELDS)
    response.headers["Cache-Control"] = "no-store"
    return list_budget_rules(
        request.app.state.config,
        context,
        scope_type=query.get("scope_type"),
        environment_id=query.get("environment_id"),
        environment=query.get("environment"),
        status=query.get("status"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/budgets",
    response_model=BudgetRuleResponse,
    status_code=201,
    openapi_extra=CREATE_BUDGET_RULE_REQUEST_OPENAPI,
)
async def post_budget_route(
    request: Request,
    response: Response,
    context: BudgetWriter,
) -> BudgetRuleResponse:
    """仅管理员 create-only 创建预算规则，重复 scope 返回稳定 409。"""

    payload = await _read_payload(
        request,
        CreateBudgetRuleRequest,
        allowed_fields=ALLOWED_BUDGET_RULE_BODY_FIELDS,
        error_code="budget.rule_invalid",
    )
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        create_budget_rule,
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/budgets",
    response_model=BudgetRuleResponse,
    openapi_extra=BUDGET_RULE_REQUEST_OPENAPI,
)
async def patch_budget_route(
    request: Request,
    response: Response,
    context: BudgetWriter,
) -> BudgetRuleResponse:
    """兼容既有 upsert 语义更新或创建当前租户预算规则。"""

    payload = await _read_payload(
        request,
        UpdateBudgetRuleRequest,
        allowed_fields=ALLOWED_BUDGET_RULE_BODY_FIELDS,
        error_code="budget.rule_invalid",
    )
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        upsert_budget_rule,
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/budgets/{budget_rule_id}/mode",
    response_model=BudgetRuleResponse,
    openapi_extra=BUDGET_MODE_REQUEST_OPENAPI,
)
async def patch_budget_mode_route(
    budget_rule_id: str,
    request: Request,
    response: Response,
    context: BudgetWriter,
) -> BudgetRuleResponse:
    """仅管理员可按当前租户规则 ID 切换硬阻断或仅告警模式。"""

    payload = await _read_payload(
        request,
        UpdateBudgetModeRequest,
        allowed_fields=ALLOWED_BUDGET_MODE_BODY_FIELDS,
        error_code="budget.mode_invalid",
    )
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        update_budget_mode,
        request.app.state.config,
        context,
        budget_rule_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/budget-status", response_model=BudgetStatusResponse)
def get_budget_status_route(
    request: Request,
    response: Response,
    context: BudgetReader,
) -> BudgetStatusResponse:
    """读取可信 Project/Application/Key 归属链对应的今日预算聚合状态。"""

    query = _read_query(request, ALLOWED_BUDGET_STATUS_QUERY_FIELDS)
    response.headers["Cache-Control"] = "no-store"
    return get_budget_status(
        request.app.state.config,
        context,
        virtual_key_id=query.get("virtual_key_id"),
        environment_id=query.get("environment_id"),
        project_id=query.get("project_id"),
        application_id=query.get("application_id"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _read_query(request: Request, allowed_fields: frozenset[str]) -> dict[str, str]:
    """严格读取唯一白名单查询参数，拒绝未知、重复和客户端 tenant_id。"""

    seen: set[str] = set()
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        # 重复参数会在代理和框架间产生 first/last 歧义，未知字段也不能静默忽略。
        if key not in allowed_fields or key in seen:
            raise ApiError("budget.rule_invalid")
        seen.add(key)
        values[key] = value
    return values


async def _read_payload(
    request: Request,
    model: type[PayloadModel],
    *,
    allowed_fields: frozenset[str],
    error_code: str,
) -> PayloadModel:
    """在 RBAC 通过后有界读取 JSON object，并转换为 extra-forbid DTO。"""

    parsed = await _read_json_object(request, allowed_fields=allowed_fields, error_code=error_code)
    try:
        return model.model_validate(parsed)
    # DTO 类型异常属于外部输入问题，不能暴露 Pydantic 的字段路径或内部模型。
    except (ValidationError, TypeError, ValueError) as exc:
        # Pydantic 只负责字段类型的第二道校验，对外仍统一成预算域错误码。
        raise ApiError(error_code) from exc


async def _read_json_object(
    request: Request,
    *,
    allowed_fields: frozenset[str],
    error_code: str,
) -> dict[str, Any]:
    """严格读取 UTF-8 JSON object，拒绝重复键、未知字段和资源攻击结构。"""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    # 只接受明确 JSON 媒体类型，避免代理与框架对表单或文本产生不同解释。
    if content_type != "application/json":
        # 媒体类型差异统一收敛为预算域错误，不能成为解析器探针。
        raise ApiError(error_code)
    declared = request.headers.get("content-length")
    if declared is not None:
        # Content-Length 只接受规范 ASCII 无符号整数，符号、空白和 Unicode 数字均失败关闭。
        if not declared or any(character < "0" or character > "9" for character in declared):
            # 畸形长度不能进入 int 宽松解析或绕过正文限额。
            raise ApiError(error_code)
        if int(declared) < 1 or int(declared) > MAX_BUDGET_BODY_BYTES:
            # 空声明和超限声明都必须在读取正文前拒绝，限制内存占用。
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BUDGET_BODY_BYTES:
            # 实际分块流量仍需独立限额，不能信任客户端 Content-Length。
            raise ApiError(error_code)
        body.extend(chunk)
    if not body:
        # 空正文没有合法规则语义，不能补默认 scope 或金额。
        raise ApiError(error_code)
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    # 编码、重复键和递归异常都属于同一外部合同，禁止返回解析器细节。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # 对外只返回稳定预算错误码，原始正文和异常文本不得进入响应。
        raise ApiError(error_code) from exc
    if not isinstance(parsed, dict) or _has_unsafe_json_structure(parsed):
        # 顶层非对象或结构超限会产生歧义与资源风险，统一失败关闭。
        raise ApiError(error_code)
    # tenant_id、actor 和服务端派生字段都不在白名单内，因此不会进入 DTO 或服务层。
    if any(key not in allowed_fields for key in parsed):
        raise ApiError(error_code)
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object，并在任意层发现重复字段时失败关闭。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _has_unsafe_json_structure(value: object) -> bool:
    """迭代限制 JSON 深度、节点数和字符串控制字符。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            return True
        if isinstance(current, str):
            if any(ord(character) < 32 for character in current):
                return True
            continue
        if isinstance(current, dict):
            for key, child in current.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        if isinstance(current, list):
            for child in current:
                stack.append((child, depth + 1))
    return False


def _request_id(request: Request) -> str:
    """从请求上下文读取统一 request_id，保证预算 API 响应和日志可串联。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """从请求上下文读取统一 trace_id，保证预算 API 响应和跨服务排障链路可串联。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
