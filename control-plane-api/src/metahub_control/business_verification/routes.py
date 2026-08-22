"""提供隔离 Workspace/PlatformSession 的企业认证、开票资料和审核 HTTP 边界。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    BillingProfileResponse,
    BillingProfileUpdateRequest,
    BusinessVerificationSubmitRequest,
    EvidenceDownloadGrantResponse,
    PlatformVerificationListResponse,
    VerificationReviewRequest,
    VerificationReviewResponse,
    WorkspaceBusinessVerificationResponse,
)
from .service import (
    get_workspace_business_verification,
    issue_evidence_download_token,
    list_platform_business_verifications,
    review_business_verification,
    submit_business_verification,
    update_workspace_billing_profile,
)

router = APIRouter(tags=["business-verification"])
# 强 ETag 和幂等键使用单一规范，原始值不写响应、日志或审计。
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_ETAG_RE = re.compile(r'"(?:0|[1-9][0-9]*)"')
_QUEUE_STATUSES = frozenset({"submitted", "in_review", "approved", "rejected", "expired"})


def require_workspace_business_finance(request: Request) -> WorkspaceSessionContext:
    """在正文解析前恢复 WorkspaceSession，并实时要求 owner 或 finance_admin。"""

    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        # Workspace 法定资料必须保持单一浏览器凭据域，不能混入 Bearer、Platform 或 Support 身份。
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "business_verification.request_invalid")
        csrf = _required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        )
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    # admin/developer 角色名称不能隐式获得法定主体或开票资料权限。
    if context.membership_role not in {"owner", "finance_admin"}:
        raise ApiError("workspace_session.permission_denied")
    # 路径 owner 必须与会话恢复的 Workspace 完全一致，跨空间请求统一表现为不存在。
    if context.workspace_id != request.path_params.get("workspace_id"):
        raise ApiError("business_verification.not_found")
    return context


def require_platform_verification(request: Request) -> PlatformContext:
    """在正文解析前恢复 PlatformSession，并按读写方法复核 verification 权限。"""

    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        # reviewer 不能借 Workspace 或 Support Cookie 扩大客户数据可见范围。
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    permission = (
        "platform.verification.read"
        if request.method in {"GET", "HEAD", "OPTIONS"}
        else "platform.verification.review"
    )
    # 页面隐藏控件不构成授权，读写方法必须再次校验当前 PlatformSession 动态权限。
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.post(
    "/workspaces/{workspace_id}/business-verifications",
    response_model=WorkspaceBusinessVerificationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_business_verification(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_business_finance)],
) -> WorkspaceBusinessVerificationResponse:
    """解析严格提交 DTO 后，原子冻结主体 revision 与证据 manifest。"""

    _reject_query(request)
    try:
        payload = BusinessVerificationSubmitRequest.model_validate(
            await _strict_json(request, "business_verification.request_invalid")
        )
    # DTO 校验错误只返回固定码，不能回显法定名称、证号或对象引用。
    except ValueError:
        raise ApiError("business_verification.request_invalid") from None
    result = submit_business_verification(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.verification is not None:
        response.headers["ETag"] = f'"{result.verification.revision}"'
    _private(response)
    return result


@router.get(
    "/workspaces/{workspace_id}/business-verification",
    response_model=WorkspaceBusinessVerificationResponse,
)
async def get_business_verification(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_business_finance)],
) -> WorkspaceBusinessVerificationResponse:
    """返回当前主体、认证、合同和开票资料的脱敏 Workspace 摘要。"""

    _reject_query(request)
    result = get_workspace_business_verification(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.verification is not None:
        response.headers["ETag"] = f'"{result.verification.revision}"'
    _private(response)
    return result


@router.patch(
    "/workspaces/{workspace_id}/billing-profile",
    response_model=BillingProfileResponse,
)
async def patch_billing_profile(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_business_finance)],
) -> BillingProfileResponse:
    """以强 CAS 和幂等键替换开票资料密文，并返回字段级掩码状态。"""

    _reject_query(request)
    expected_revision = _if_match(request, allow_zero=True)
    try:
        payload = BillingProfileUpdateRequest.model_validate(
            await _strict_json(request, "business_verification.request_invalid")
        )
    # DTO 错误不能回显税号、银行账号或地址等开票明文。
    except ValueError:
        raise ApiError("business_verification.request_invalid") from None
    result = update_workspace_billing_profile(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.billing_profile.revision}"'
    _private(response)
    return result


@router.get(
    "/platform/business-verifications",
    response_model=PlatformVerificationListResponse,
)
async def get_platform_business_verifications(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_verification)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlatformVerificationListResponse:
    """返回审核最小队列，不读取 Workspace 成员或通用客户资料。"""

    _exact_queue_query(request)
    # 队列状态只能使用后端冻结枚举，未知文本不得进入查询或错误详情。
    if status_filter is not None and status_filter not in _QUEUE_STATUSES:
        raise ApiError("business_verification.request_invalid")
    result = list_platform_business_verifications(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        status=status_filter,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/business-verifications/{intent_id}/review",
    response_model=VerificationReviewResponse,
)
async def post_platform_business_verification_review(
    intent_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_verification)],
) -> VerificationReviewResponse:
    """追加固定审核决定，并原子创建合同与安全平台审计。"""

    _reject_query(request)
    expected_revision = _if_match(request, allow_zero=False)
    try:
        payload = VerificationReviewRequest.model_validate(
            await _strict_json(request, "business_verification.request_invalid")
        )
    # 审核决定校验失败只返回固定码，禁止形成自由原因或客户资料回显通道。
    except ValueError:
        raise ApiError("business_verification.request_invalid") from None
    result = review_business_verification(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        intent_id=intent_id,
        expected_revision=expected_revision,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.case.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/business-verifications/{intent_id}/evidence/{evidence_id}/download-token",
    response_model=EvidenceDownloadGrantResponse,
)
async def post_evidence_download_token(
    intent_id: str,
    evidence_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_verification)],
) -> EvidenceDownloadGrantResponse:
    """为 active review 签发一次性短 TTL 证据授权，不返回对象存储地址。"""

    _reject_query(request)
    # 空对象保持严格 JSON/Content-Type 与 Origin/CSRF 语义，禁止通过 query 传 owner。
    payload = await _strict_json(request, "business_verification.request_invalid")
    # 下载授权端点不接受任何客户端 owner 或对象存储参数，非空正文一律拒绝。
    if payload:
        raise ApiError("business_verification.request_invalid")
    result = issue_evidence_download_token(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        intent_id=intent_id,
        evidence_id=evidence_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.intent_revision}"'
    _private(response)
    return result


def _idempotency_key(request: Request) -> str:
    """只接受一个规范幂等键；错误时不回显原值。"""

    values = _raw_headers(request, b"idempotency-key")
    # 重复、缺失或非规范幂等键统一失败，原值不能进入响应和审计摘要。
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("business_verification.request_invalid")
    return values[0]


def _if_match(request: Request, *, allow_zero: bool) -> int:
    """解析单一强十进制 ETag，拒绝弱 ETag、列表和非规范数字。"""

    values = _raw_headers(request, b"if-match")
    # 只接受一个强 ETag，避免代理合并列表或弱校验造成 CAS 语义歧义。
    if len(values) != 1 or _ETAG_RE.fullmatch(values[0]) is None:
        raise ApiError("business_verification.request_invalid")
    revision = int(values[0][1:-1])
    # 既有审核 intent 不存在 revision 零，防止把创建语义误用于审核更新。
    if not allow_zero and revision == 0:
        raise ApiError("business_verification.request_invalid")
    return revision


def _reject_query(request: Request) -> None:
    """拒绝所有查询参数，owner 只能由路径、会话与数据库恢复。"""

    # 写入 owner 只能来自会话和路径，任何查询参数都可能制造第二事实来源。
    if request.query_params.multi_items():
        raise ApiError("business_verification.request_invalid")


def _exact_queue_query(request: Request) -> None:
    """审核列表只允许 status/limit/offset 各出现一次。"""

    keys = [key for key, _value in request.query_params.multi_items()]
    # 未知或重复查询键会产生解析差异，必须在业务查询前统一拒绝。
    if any(key not in {"status", "limit", "offset"} for key in keys) or len(keys) != len(
        set(keys)
    ):
        # 查询键集合不规范时固定拒绝，不把未知键名回显到错误响应。
        raise ApiError("business_verification.request_invalid")


__all__ = ["router"]
