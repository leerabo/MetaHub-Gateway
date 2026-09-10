"""控制面认证依赖：在解析业务参数前校验当前 Session 与数据库实时权限矩阵。

所有守卫均从认证上下文取得 tenant_id，并由后端执行 RBAC；前端可见性不能补权，
认证令牌和敏感业务字段不得进入日志或错误响应。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from fastapi import Header, Request
from metahub_shared.feedback import FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS
from metahub_shared.observability import bind_authenticated_telemetry_tenant
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .service import (
    AuthContext,
    authenticate_bearer,
    require_any_permission,
    require_permission,
)

if TYPE_CHECKING:
    from metahub_control.machine_identity.service import MachineAuthContext


def _require_request_permissions(
    request: Request,
    authorization: str | None,
    permission_codes: tuple[str, ...],
) -> AuthContext:
    """认证当前请求并要求给定 permission code 全部命中。

    参数：请求对象提供可信服务配置，Bearer Header 只定位 Session，权限集合来自数据库快照。
    返回：带当前 tenant_id、revision 和授权指纹的上下文；任一权限缺失即统一 403。
    """

    _reject_mixed_workspace_credential(request, authorization)
    context = authenticate_bearer(request.app.state.config, authorization)
    for permission_code in permission_codes:
        # 复合旧守卫采用全部命中语义，避免读写尚未拆路由时把已撤销的写权限重新放大。
        require_permission(context, permission_code)
    bind_authenticated_telemetry_tenant(request, context.user.tenant_id)
    return context


def require_current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """恢复当前登录用户。

    后续控制面管理 API 必须依赖这个函数拿租户上下文，不能只靠前端隐藏按钮。
    """

    # 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
    _reject_mixed_workspace_credential(request, authorization)
    context = authenticate_bearer(request.app.state.config, authorization)
    bind_authenticated_telemetry_tenant(request, context.user.tenant_id)
    return context


def require_console_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Authenticate Console navigation with exactly one legacy or Workspace credential."""

    workspace_token = _workspace_cookie(request)
    if workspace_token is not None:
        if (
            authorization is not None
            or _raw_authorization_present(request)
            or _cookie_present(request, PLATFORM_SESSION_COOKIE)
            or _cookie_present(request, SUPPORT_SESSION_COOKIE)
        ):
            raise ApiError("workspace_session.credential_conflict")
        clear_workspace_cookies_on_error(request)
        # Delay the cross-domain adapter import so auth package initialization remains acyclic.
        from metahub_control.workspace_access.service import (
            authenticate_workspace_session_as_auth_context,
        )

        context = authenticate_workspace_session_as_auth_context(
            request.app.state.config,
            workspace_token,
        )
        request.state.workspace_session_authenticated = True
        bind_authenticated_telemetry_tenant(request, context.user.tenant_id)
        return context
    context = authenticate_bearer(request.app.state.config, authorization)
    bind_authenticated_telemetry_tenant(request, context.user.tenant_id)
    return context


def require_workspace_or_legacy_permissions(
    *permission_codes: str,
    csrf_required: bool = False,
    monotonic_deadline: float | None = None,
    deadline_error_code: str = "infra.service_unavailable",
) -> Callable[..., AuthContext]:
    """Build an explicit resource-route guard for one Workspace or Legacy credential."""

    if not permission_codes:
        raise ValueError("permission codes are required")

    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AuthContext:
        workspace_token = _workspace_cookie(request)
        foreign_session = _cookie_present(request, PLATFORM_SESSION_COOKIE) or _cookie_present(
            request, SUPPORT_SESSION_COOKIE
        )
        if foreign_session:
            raise ApiError("workspace_session.credential_conflict")
        if workspace_token is not None:
            if authorization is not None or _raw_authorization_present(request):
                raise ApiError("workspace_session.credential_conflict")
            clear_workspace_cookies_on_error(request)
            csrf_token = None
            if csrf_required or request.method not in {"GET", "HEAD", "OPTIONS"}:
                _require_workspace_origin(request)
                csrf_token = _workspace_csrf(request)
            from metahub_control.workspace_access.service import (
                authenticate_workspace_session_as_auth_context,
            )

            context = authenticate_workspace_session_as_auth_context(
                request.app.state.config,
                workspace_token,
                csrf_token=csrf_token,
                monotonic_deadline=monotonic_deadline,
                deadline_error_code=deadline_error_code,
            )
            request.state.workspace_session_authenticated = True
            request.state.credential_domain = "workspace"
        else:
            context = authenticate_bearer(
                request.app.state.config, authorization,
                monotonic_deadline=monotonic_deadline, deadline_error_code=deadline_error_code,
            )
            request.state.credential_domain = "legacy"
        for permission_code in permission_codes:
            require_permission(context, permission_code)
        bind_authenticated_telemetry_tenant(request, context.user.tenant_id)
        return context

    return dependency


def require_provider_reader_or_machine(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext | MachineAuthContext:
    """Allow the Provider collection to one human domain or one machine Bearer."""

    return _require_human_or_machine(
        request,
        authorization,
        permission_code="provider.read",
    )


def require_request_log_reader_or_machine(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext | MachineAuthContext:
    """Allow only the request-log collection, never detail or SavedFilter routes."""

    return _require_human_or_machine(
        request,
        authorization,
        permission_code="request_log.read",
    )


def _require_human_or_machine(
    request: Request,
    authorization: str | None,
    *,
    permission_code: str,
) -> AuthContext | MachineAuthContext:
    from metahub_control.machine_identity.service import authenticate_machine_request
    from metahub_control.machine_identity.transport import (
        direct_peer_ip,
        optional_machine_bearer,
        reject_machine_cookies,
        verified_mtls_fingerprint,
    )

    access_token = optional_machine_bearer(request)
    if access_token is None:
        human_dependency = require_workspace_or_legacy_permissions(permission_code)
        return human_dependency(request, authorization)
    reject_machine_cookies(request)
    context = authenticate_machine_request(
        request.app.state.config,
        access_token=access_token,
        method=request.method,
        path=request.url.path,
        peer_ip=direct_peer_ip(request),
        verified_mtls_fingerprint=verified_mtls_fingerprint(request),
        request_id=getattr(request.state, "request_id", ""),
        trace_id=getattr(request.state, "trace_id", ""),
    )
    if permission_code not in context.permission_codes:
        raise ApiError("machine_auth.invalid")
    request.state.credential_domain = "machine"
    bind_authenticated_telemetry_tenant(request, context.tenant_id)
    return context


def _reject_mixed_workspace_credential(
    request: Request,
    authorization: str | None,
) -> None:
    if _workspace_cookie(request) is not None and (
        authorization is not None or _raw_authorization_present(request)
    ):
        raise ApiError("workspace_session.credential_conflict")


def _workspace_cookie(request: Request) -> str | None:
    values: list[str] = []
    for raw_name, raw_value in request.scope.get("headers", ()):
        if raw_name.lower() != b"cookie":
            continue
        for segment in raw_value.decode("latin-1").split(";"):
            name, separator, value = segment.strip().partition("=")
            if separator and name == WORKSPACE_SESSION_COOKIE:
                values.append(value)
    if not values:
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("workspace_session.invalid")
    return values[0]


def _cookie_present(request: Request, cookie_name: str) -> bool:
    return any(
        segment.strip().partition("=")[0] == cookie_name
        for raw_name, raw_value in request.scope.get("headers", ())
        if raw_name.lower() == b"cookie"
        for segment in raw_value.decode("latin-1").split(";")
    )


def _workspace_csrf(request: Request) -> str:
    cookie_values: list[str] = []
    for raw_name, raw_value in request.scope.get("headers", ()):
        if raw_name.lower() != b"cookie":
            continue
        for segment in raw_value.decode("latin-1").split(";"):
            name, separator, value = segment.strip().partition("=")
            if separator and name == WORKSPACE_CSRF_COOKIE:
                cookie_values.append(value)
    header_values = [
        raw_value.decode("latin-1")
        for raw_name, raw_value in request.scope.get("headers", ())
        if raw_name.lower() == b"x-csrf-token"
    ]
    if (
        len(cookie_values) != 1
        or len(header_values) != 1
        or not valid_opaque_value(cookie_values[0])
        or not valid_opaque_value(header_values[0])
        or not secrets.compare_digest(cookie_values[0], header_values[0])
    ):
        raise ApiError("workspace_session.csrf_invalid")
    return cookie_values[0]


def _require_workspace_origin(request: Request) -> None:
    origins = [
        raw_value.decode("latin-1")
        for raw_name, raw_value in request.scope.get("headers", ())
        if raw_name.lower() == b"origin"
    ]
    sites = [
        raw_value.decode("latin-1")
        for raw_name, raw_value in request.scope.get("headers", ())
        if raw_name.lower() == b"sec-fetch-site"
    ]
    # Browsers omit Origin for same-origin GET exports; CSRF remains mandatory.
    if not origins and request.method == "GET" and sites == ["same-origin"]:
        referrers = [
            raw_value.decode("latin-1")
            for raw_name, raw_value in request.scope.get("headers", ())
            if raw_name.lower() == b"referer"
        ]
        if len(referrers) == 1:
            referrer = referrers[0]
            try:
                parsed = urlsplit(referrer)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                if (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.fragment
                    and not any(ord(char) <= 32 or ord(char) == 127 for char in referrer)
                    and origin in request.app.state.config.allowed_origins
                ):
                    return
            except ValueError:
                pass
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError("onboarding.origin_invalid")


def _raw_authorization_present(request: Request) -> bool:
    return any(
        name.lower() == b"authorization" for name, _value in request.scope.get("headers", ())
    )


async def require_admin_assistant_authentication(request: Request) -> AsyncIterator[AuthContext]:
    """不读取正文，只为 PAGE-107 恢复可信认证上下文。

    管理助手必须审计“认证成立但缺少 base permission”的 actor，因此本依赖只认证，业务权限
    留给 endpoint 在正文前校验。首先读取 raw ASGI Header，避免框架把重复 Bearer 合并后任选
    一个凭据，造成身份歧义或让未授权正文进入 parser。
    """

    # 延迟导入避免 auth 包初始化与 PAGE-107 route/service 形成模块环；请求执行时模块已完整装配。
    from metahub_control.admin_assistant.service import (
        admin_assistant_request_owner,
        admin_assistant_runtime_read_connection,
        prepare_admin_assistant_request,
        release_admin_assistant_request,
        retire_admin_assistant_runtime_read_connection,
        run_admin_assistant_worker,
    )

    budget, runtime = prepare_admin_assistant_request(request)
    try:
        values = [
            value
            for key, value in request.scope.get("headers", ())
            if key.lower() == b"authorization"
        ]
        # 重复或逗号折叠的 Authorization 身份有歧义，正文解析前即按固定请求错误拒绝。
        if len(values) > 1 or (values and b"," in values[0]):
            raise ApiError("admin_assistant.request_invalid")
        if values:
            try:
                authorization = values[0].decode("ascii", errors="strict")
            # Bearer wire 只允许 ASCII；解码失败不保留原始 Token 字节或其表示。
            except UnicodeDecodeError as exc:
                raise ApiError("auth.token_invalid") from exc
        else:
            # 无凭据仍交由统一认证器产生标准错误信封和 request_id，不在依赖层自造响应。
            authorization = None
        _reject_mixed_workspace_credential(request, authorization)
        attempt_owner = admin_assistant_request_owner(request)

        # Session SELECT、权限扫描和 admission 排队共用 middleware 入口的 source deadline；
        # last_seen 延迟到最终治理事务。未建立可信 actor 前预算耗尽时不写 tenant 业务审计。
        def authenticate_on_runtime_connection() -> AuthContext:
            """在唯一 read-stage owner 内认证，并让后续来源读取复用同一连接。"""

            connection = admin_assistant_runtime_read_connection(
                runtime,
                request.app.state.config,
                deadline=budget.source_deadline,
                attempt_owner=attempt_owner,
            )
            # 管理助手入口认证保持只读，Session 活跃时间由最终治理事务原子更新。
            return authenticate_bearer(
                request.app.state.config,
                authorization,
                monotonic_deadline=budget.source_deadline,
                deadline_error_code="admin_assistant.source_unavailable",
                connection=connection,
                update_last_seen=False,
                invalidate_connection=lambda current: (
                    retire_admin_assistant_runtime_read_connection(runtime, current)
                ),
            )

        context = await run_admin_assistant_worker(
            authenticate_on_runtime_connection,
            slots=runtime.auth_slots,
            dispatcher=runtime.dispatcher,
            deadline=budget.source_deadline,
            error_code="admin_assistant.source_unavailable",
        )
    except BaseException:
        # 认证未建立可信 actor 时路由不会执行，必须在依赖边界释放已登记 gate owner。
        release_admin_assistant_request(request)
        raise
    try:
        # yield dependency 把 owner 生命周期覆盖到 endpoint 完整退出，关闭认证→路由取消缝隙。
        yield context
    finally:
        release_admin_assistant_request(request)


def _response_cache_authorization(request: Request) -> str | None:
    """从 raw ASGI Header 恢复唯一 Bearer 值，拒绝框架可能折叠的重复凭据。"""

    values = [
        value for key, value in request.scope.get("headers", ()) if key.lower() == b"authorization"
    ]
    if not values:
        return None
    if len(values) != 1:
        # 重复 Authorization 属于固定 HTTP 合同错误，绝不能任选一个 Token 继续认证。
        raise ApiError("response_cache.input_invalid")
    try:
        authorization = values[0].decode("ascii", errors="strict")
    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    except UnicodeError:
        # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
        raise ApiError("response_cache.input_invalid") from None
    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    if request.headers.get("authorization") != authorization or "," in authorization:
        # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
        raise ApiError("response_cache.input_invalid")
    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    return authorization


def _require_response_cache_permissions(
    request: Request,
    permission_codes: tuple[str, ...],
) -> AuthContext:
    """在读取 query/body 前完成 Response Cache 的实时 Session 与复合 RBAC 校验。"""

    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    return _require_request_permissions(
        request,
        _response_cache_authorization(request),
        permission_codes,
    )


def require_response_cache_reader(request: Request) -> AuthContext:
    """要求当前 Session 具有 Response Cache 读取权限。

    控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    当前定义负责 require_response_cache_reader 对应步骤。
    """

    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    return _require_response_cache_permissions(request, ("response_cache.read",))


def require_response_cache_writer(request: Request) -> AuthContext:
    """要求当前 Session 同时具有 Response Cache 读取与策略写权限。"""

    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    return _require_response_cache_permissions(
        request,
        ("response_cache.read", "response_cache.write"),
    )


def require_response_cache_invalidator(request: Request) -> AuthContext:
    """要求当前 Session 同时具有读取与危险失效权限。"""

    # 控制面关键分支必须维持租户、权限、审计和统一错误边界，禁止信任客户端归属。
    return _require_response_cache_permissions(
        request,
        ("response_cache.read", "response_cache.invalidate"),
    )


def require_current_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户为管理员角色。"""

    # 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
    return _require_request_permissions(
        request,
        authorization,
        ("user.manage",),
    )


def require_tenant_settings_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取或维护当前租户的基础设置。

    PRD-059 v1 仅定义 ``tenant.settings.read``，且该权限只属于不可编辑权限矩阵的
    admin 角色；因此租户资料 GET/PATCH 共同复用这一精确权限点。后续若产品拆出写权限，
    必须先通过新迁移扩展权限目录，不能继续借用 ``user.manage`` 或角色名隐式放权。
    """

    # tenant_id 只能由 Bearer Session 恢复，客户端不得通过路径、查询或正文选择其他租户。
    return _require_request_permissions(request, authorization, ("tenant.settings.read",))


def require_tenant_data_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """在解析 PAGE-121 业务输入前实时校验租户数据只读权限。"""

    return _require_request_permissions(request, authorization, ("tenant_data.read",))


def require_tenant_data_exporter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """导出命令必须由后端独立 critical 权限授权，不能由页面可见性推导。"""

    return _require_request_permissions(request, authorization, ("tenant_data.export",))


def require_tenant_data_downloader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """下载制品使用独立权限，避免只读页面权限扩大到敏感附件。"""

    return _require_request_permissions(request, authorization, ("tenant_data.download",))


def require_tenant_data_deleter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """注销命令在正文解析前完成独立后端危险权限校验。"""

    return _require_request_permissions(request, authorization, ("tenant_data.delete",))


def require_virtual_key_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可写 Virtual Key。

    PRD-008 明确 viewer 不能创建 Virtual Key；后续 Virtual Key 正式接口必须复用
    这个依赖，而不是只在控制台隐藏“创建”按钮。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("virtual_key.write",),
    )


def require_model_catalog_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取模型目录。

    模型目录对开发者开放，用于发现可调用的内部标准模型 ID；viewer 在 M0
    不开放，避免扩大企业内可用模型范围信息的暴露面。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("model.read",),
    )


def require_request_log_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取调用日志。

    调用日志包含模型、状态、延迟、成本估算和错误码，面向管理员和开发者开放；
    viewer 在 M0 不开放，避免扩大企业内部模型与调用行为的可见范围。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("request_log.read",),
    )


def require_request_explanation_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取请求解释中心。

    请求解释会聚合鉴权、预算、DLP、路由、成本与 Webhook 安全证据，因此只允许
    admin/developer。该依赖由 FastAPI 在业务 wire 解析前执行，viewer 不能借畸形
    path/query/header/body 探测请求或字段是否存在。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("request_explanation.read",),
    )


def require_error_map_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取 Provider 错误归一规则。

    规则 pattern 和 Provider 范围属于租户治理配置，只向 admin/developer 开放。viewer 即使
    直接请求或提交畸形 query/body，也必须在业务 wire 解析前得到统一权限错误。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("provider_error_map.read",),
    )


def require_error_map_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可修改 Provider 错误归一规则。

    规则会改变 Gateway 对上游失败的公开分类，因此写权限固定为 admin；前端隐藏编辑按钮
    不能替代该后端门禁，tenant_id 与 actor 仍只从已验证 Session 恢复。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("provider_error_map.write",),
    )


def require_usage_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取用量与成本账本。
    用量 API 会暴露模型、Key、Token 和成本维度，M0 仅开放给管理员和开发者，
    避免 viewer 扩大企业内部调用可见范围。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("billing.usage.read",),
    )


def require_dashboard_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取租户 Dashboard。

    PRD-026 旧总览和 PRD-065 角色首页都先经过本守卫；六个固定角色是否还能进入
    具体角色视图，随后继续以同一次请求的 permission 快照与 tenant-scoped 配置求交。
    前端隐藏入口、角色名或历史 Session 均不能替代该后端权限校验。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("dashboard.read",),
    )


def require_product_analytics_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 实时具备本租户产品激活聚合读取权限。"""

    return _require_request_permissions(
        request,
        authorization,
        ("product_analytics.read",),
    )


def require_customer_health_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 实时具备本租户客户健康评分读取权限。"""

    # PAGE-118 的 persona 和前端导航都不能补权；每次请求只信任当前 tenant 的动态矩阵。
    return _require_request_permissions(
        request,
        authorization,
        ("customer_health.read",),
    )


def require_partner_portal_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 实时具备伙伴门户读取权限。"""

    return _require_request_permissions(request, authorization, ("partner_portal.read",))


def require_partner_portal_configurer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 具备首次创建当前租户 Partner 的关键权限。"""

    return _require_request_permissions(request, authorization, ("partner_portal.configure",))


def require_partner_customer_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求同时具备伙伴读取与客户映射管理权限。"""

    return _require_request_permissions(
        request,
        authorization,
        ("partner_portal.read", "partner_customer.manage"),
    )


def require_partner_branding_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求同时具备伙伴读取与白标配置权限。"""

    return _require_request_permissions(
        request,
        authorization,
        ("partner_portal.read", "partner_branding.manage"),
    )


def require_branding_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 具备租户级 BrandingConfig 读取权限。"""

    return _require_branding_permissions(request, authorization, ("branding.read",))


def require_branding_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求同时具备白标读取和管理权限，避免撤销读取后仍可写入品牌配置。"""

    return _require_branding_permissions(
        request,
        authorization,
        ("branding.read", "branding.manage"),
    )


def _require_branding_permissions(
    request: Request,
    authorization: str | None,
    permission_codes: tuple[str, ...],
) -> AuthContext:
    """Map live RBAC denial to Branding while preserving authentication errors."""

    try:
        return _require_request_permissions(request, authorization, permission_codes)
    except ApiError as exc:
        if exc.code == "auth.permission_denied":
            raise ApiError("branding.forbidden") from None
        raise


def require_partner_commission_exporter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求同时具备伙伴读取与分润导出权限。"""

    return _require_request_permissions(
        request,
        authorization,
        ("partner_portal.read", "partner_commission.export"),
    )


def require_partner_delivery_downloader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求同时具备伙伴读取与安全交付包下载权限。"""

    return _require_request_permissions(
        request,
        authorization,
        ("partner_portal.read", "partner_delivery.download"),
    )


def require_project_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取项目与应用归属信息。

    Project/Application 会暴露业务系统归属、Key 绑定数量和成本聚合入口，
    M0.5 只开放给管理员和开发者。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("project.read",),
    )


def require_feedback_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 用户可以提交反馈并读取反馈汇总。

    参数 ``request`` 提供控制面配置，``authorization`` 是 Session Bearer Token；
    返回已经校验的用户、租户和会话上下文。逐请求反馈会暴露调用归属和模型满意度，
    因而仅向 admin/developer 开放，viewer 即使绕过前端直接调用也必须在读取正文或
    查询参数前由本依赖拒绝，避免利用校验差异探测反馈合同。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("feedback.use",),
    )


def require_feedback_summary_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """在整个 250ms endpoint 预算内认证 Feedback Summary 读者。

    起点由共享中间件在任何 FastAPI dependency 前写入；本依赖把同一绝对 deadline
    继续传给 Session 查询/last_seen 锁等待，再保存给路由和 Summary worker。匿名、
    无效 Token 与 viewer 仍先得到稳定认证/RBAC 错误，不解析报表 query。
    """

    started = getattr(request.state, "request_started_monotonic", time.monotonic())
    deadline = started + FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS
    # deadline 只驻留当前请求，路由不得重新用当前时刻生成另一份 250ms 预算。
    request.state.feedback_summary_deadline = deadline
    return require_workspace_or_legacy_permissions(
        "feedback.use",
        monotonic_deadline=deadline,
        deadline_error_code="feedback.unavailable",
    )(request, authorization)


def require_usage_exporter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可批量导出当前租户的用量与成本账本。

    PRD-043 的财务角色在当前 RBAC 尚未独立建模，因此批量导出仅允许 admin；
    developer 仍可使用分页 Usage API，但不能借导出绕过后续 PRD-059 的账单权限矩阵。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("billing.usage.export",),
    )


def require_finops_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取正式账本并创建财务调整。

    当前 RBAC 尚未建模独立 finance 角色，因此只允许 admin。返回统一认证产生的
    AuthContext；developer/viewer 即使手写请求也会在解析财务查询或正文前被拒绝。
    """

    # 复用统一管理员守卫，保持认证、租户恢复和固定权限错误完全一致。
    return _require_request_permissions(
        request,
        authorization,
        (
            "billing.finops.read",
            "billing.finops.write",
        ),
    )


def require_cost_anomaly_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取成本异常与检测器健康状态。

    PRD-053 尚未引入独立 finance/security 角色，因此读权限固定映射到 admin。
    该依赖必须在路由解析 query、header 和 body 之前执行，确保 developer/viewer
    无法借畸形输入探测治理接口合同。
    """

    # 统一管理员守卫同时恢复可信 tenant_id 和 actor，读接口不得自行解析客户端身份。
    return _require_request_permissions(
        request,
        authorization,
        ("billing.anomaly.read",),
    )


def require_cost_anomaly_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可处置成本异常并创建或释放临时限流覆盖。

    返回的 tenant_id 与 actor_user_id 都来自已验证 Session，动作服务不得接受客户端
    覆盖这两个安全边界字段。当前写权限同样仅映射 admin。
    """

    # 写动作必须复用同一后端管理员守卫，前端隐藏按钮不能替代角色和租户校验。
    return _require_request_permissions(
        request,
        authorization,
        ("billing.anomaly.write",),
    )


def require_provider_health_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备 Provider 健康状态读取权限。

    读取与主动探测必须拆分授权，否则只读运维角色会被无关的写操作权限阻断；
    探测接口另由 ``require_provider_health_prober`` 在后端独立校验。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.health.read",))


def require_provider_status_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户同时可读 Provider 健康与请求级调用证据。

    状态页会展示逐 Provider 的真实调用错误和 request_id，因此只具备健康读取权限并不足够。
    两项权限必须在每次请求从数据库快照共同校验，任一项被撤销后旧 Session 也会立即失败关闭。
    """

    # 双权限必须作为同一次实时快照校验，不能让健康权限替代请求级故障证据权限。
    return _require_request_permissions(
        request,
        authorization,
        ("provider.health.read", "request_log.read"),
    )


def require_audit_log_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备读取管理审计日志的权限。

    M0.5 尚未提供独立 security 角色，因此安全负责人映射为 admin。developer/viewer 即使绕过
    控制台导航直接调用接口，也会在该后端依赖中返回统一 `auth.permission_denied`。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("audit.read",),
    )


def require_audit_exporter(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可导出当前租户的不可变管理审计事实。

    PRD-059 前没有独立 SecurityAdmin 角色，因此只允许 admin。单独保留该依赖是为了
    后续权限矩阵演进时不扩大普通审计列表权限，也防止前端隐藏按钮被当成授权边界。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("audit.export",),
    )


def require_model_price_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取模型价格时间线。

    价格会暴露企业模型成本口径，当前基础角色中仅 admin/developer 可读；
    viewer 手写请求也必须由后端拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("model_price.read",),
    )


def require_model_price_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建不可变模型价格计划。

    当前 RBAC 尚未拆出 finance 角色，因此 M0.5 由 admin 承担财务写入职责，
    developer/viewer 均由后端拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("model_price.write",),
    )


def require_model_quality_score_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取模型质量与延迟聚合。

    模型成功率、错误率和人工评分会暴露企业内部模型选型依据，因此只开放给
    admin/developer；viewer 直接调用时也必须由后端返回统一权限错误。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("model_quality.read",),
    )


def require_model_quality_score_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可修改模型人工质量评分。

    当前 RBAC 没有独立产品负责人角色，PRD-048 将“产品/管理员”映射为 admin；
    developer 仍可查看统计，但不能通过手写请求改变线上路由质量先验。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("model_quality.write",),
    )


def require_project_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可管理项目、应用和 Virtual Key 归属绑定。

    写入归属会影响预算和审计解释，不能只依赖控制台隐藏按钮，必须在后端限制为 admin。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("project.write",),
    )


def require_environment_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取租户 Environment。

    环境会暴露 Key、别名和预算的配置边界，M0.5 只开放给 admin/developer，
    viewer 不能通过手写请求绕过导航隐藏。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("environment.read",),
    )


def require_environment_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建、启停 Environment。

    环境状态会直接阻断数据面调用并改变预算边界，因此写权限必须在后端限制为 admin。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("environment.write",),
    )


def require_budget_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取预算规则和预算状态。

    预算会暴露当前租户成本阈值和 Key 级治理策略，M0 暂开放给管理员和开发者；
    viewer 不能通过手写请求绕过页面隐藏。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("billing.budget.read",),
    )


def require_budget_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可修改预算规则。

    PRD-023 的“财务”角色在 M0 尚未独立建模，因此写权限先限定为 admin，
    避免 developer 私自提高或关闭预算。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("billing.budget.write",),
    )


def require_dlp_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以管理 DLP 规则。
    DLP 规则会直接影响用户输入是否进入模型上游，M0 尚未拆出 security role，因此只能由 admin
    在后端写入和读取，developer/viewer 即使手写 HTTP 请求也必须被拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        (
            "dlp.read",
            "dlp.write",
        ),
    )


def require_policy_admin(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以读取、校验和修改租户策略。

    策略会影响后续路由、预算与安全决策。当前 RBAC 尚未拆出安全/财务角色，
    因此 PRD-044 委托既有 admin 守卫，避免复制角色判断后与认证主链路漂移。
    """

    # 直接委托统一管理员依赖，保证认证、租户恢复和固定权限错误完全复用现有边界。
    return _require_request_permissions(
        request,
        authorization,
        (
            "policy.read",
            "policy.write",
            "policy.simulate",
            "policy.publish",
        ),
    )


def require_virtual_key_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取脱敏 Virtual Key 元数据。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("virtual_key.read",))


def require_provider_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取当前租户 Provider 与 KeyPool 脱敏配置。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.read",))


def require_provider_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建、更新和测试 Provider 或 KeyPool。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.write",))


def require_local_endpoint_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Require the independent local Endpoint read permission."""

    return _require_request_permissions(request, authorization, ("local_endpoint.read",))


def require_local_endpoint_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Require local Endpoint aggregate creation authority."""

    return _require_request_permissions(request, authorization, ("local_endpoint.manage",))


def require_local_endpoint_tester(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Require permission to run the bounded local Endpoint probe."""

    return _require_request_permissions(request, authorization, ("local_endpoint.test",))


def require_model_catalog_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可维护当前租户模型目录。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("model.write",))


def require_routing_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取别名、回退和路由策略。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("routing.read",))


def require_routing_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可维护别名、回退和路由策略。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("routing.write",))


def require_finops_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取正式 FinOps 账本。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("billing.finops.read",))


def require_finops_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建财务调整和冲正。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("billing.finops.write",))


def require_billing_dispute_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Require current-tenant billing dispute read permission before parsing input."""

    return _require_request_permissions(request, authorization, ("billing.dispute.read",))


def require_billing_dispute_creator(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Require both read and create authority for a dispute creation response."""

    return _require_request_permissions(
        request,
        authorization,
        ("billing.dispute.read", "billing.dispute.create"),
    )


def require_billing_dispute_mutator(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Admit dispute mutations with read plus at least one action permission."""

    context = _require_request_permissions(request, authorization, ("billing.dispute.read",))
    return require_any_permission(
        context,
        ("billing.dispute.create", "billing.dispute.review"),
    )


def require_plan_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取本租户套餐；先授权再解析任何业务输入。"""

    # 套餐读取只认当前 Session 的实时权限快照，前端导航可见性不能补权。
    return _require_request_permissions(request, authorization, ("billing.plan.read",))


def require_plan_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建或 CAS 修改本租户套餐。"""

    # 套餐写入只认当前 Session 的实时权限快照，角色名称不能替代权限码。
    return _require_request_permissions(request, authorization, ("billing.plan.write",))


def require_quota_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取本租户四分类额度和分摊报表。"""

    # 额度读取从认证上下文恢复 tenant，客户端参数和页面状态都不能改变归属。
    return _require_request_permissions(request, authorization, ("billing.quota.read",))


def require_quota_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建本租户 append-only 额度调整。"""

    # 额度写入要求后端实时权限，隐藏按钮不能替代服务端授权与租户隔离。
    return _require_request_permissions(request, authorization, ("billing.quota.write",))


def require_provider_health_prober(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可主动触发 Provider 健康探测。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("provider.health.probe",))


def require_dlp_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取脱敏 DLP 规则。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("dlp.read",))


def require_dlp_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建和更新 DLP 规则。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("dlp.write",))


def require_intent_risk_classifier(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备一次性敏感意图分类权限。

    该入口会处理短生命周期企业正文并持久化安全摘要，因此只能校验独立
    ``intent_risk.classify`` 权限；不得复用 DLP 管理权限或按角色名称推导授权。
    """

    # 每次请求都从当前租户数据库矩阵恢复权限，角色降权无需重新登录即可生效。
    return _require_request_permissions(request, authorization, ("intent_risk.classify",))


def require_session_risk_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取本租户的高危会话报表。

    参数：请求与 Bearer Session Header。返回：数据库实时复核后的 tenant/actor 权限上下文。
    关键约束：本依赖先于 query 解析执行，客户端 tenant_id 和前端导航状态都不参与授权。
    """

    # 每次请求都读取当前租户的实时权限矩阵，避免撤权后旧 Session 继续看到高危会话。
    return _require_request_permissions(request, authorization, ("session_risk.read",))


def _approval_authorization(request: Request) -> str | None:
    """从 ASGI 原始 Header 恢复唯一审批 Bearer，不让框架折叠身份。"""

    values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"authorization"
    ]
    if not values:
        # 真正缺失的凭据继续交给统一认证器映射为 auth.token_missing。
        return None
    if len(values) != 1 or not values[0] or b"," in values[0]:
        # 重复、空值或代理逗号折叠都没有唯一用户身份，必须在 query/body 前拒绝。
        raise ApiError("auth.token_invalid")
    try:
        return values[0].decode("ascii", errors="strict")
    # 非 ASCII Authorization 不得进入 Bearer parser、异常链或任何认证日志。
    except UnicodeDecodeError:
        # 原始非法字节统一映射为固定认证错误，避免回显或编码差异泄露。
        raise ApiError("auth.token_invalid") from None


def require_approval_reader(request: Request) -> AuthContext:
    """要求当前 Session 具备审批入口读取权限。

    该基础权限只允许进入审批域；服务层仍按每条任务的 target type 实时复核预算或
    会话风险读权限，不能把 ``approval.read`` 当成跨领域数据读取通行证。
    """

    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return _require_request_permissions(
        request,
        _approval_authorization(request),
        ("approval.read",),
    )


def require_approval_decider(request: Request) -> AuthContext:
    """要求审批基础读写权限，目标领域写权限由决策事务继续复核。"""

    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return _require_request_permissions(
        request,
        _approval_authorization(request),
        ("approval.read", "approval.decide"),
    )


def require_approval_internal_producer(
    request: Request,
) -> None:
    """验证审批内部生产者，并拒绝服务身份与用户 Bearer 混用。

    v1 使用 Control 已有独立内部配置 Token。比较采用常量时间；路径租户和目标 owner
    仍由审批服务查询，外部平台通知或请求正文永远不是身份来源。
    """

    internal_values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"x-internal-token"
    ]
    authorization_values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"authorization"
    ]
    # 服务身份与用户身份互斥；重复 Header 也必须失败，不能由框架任选或逗号合并。
    if authorization_values:
        raise ApiError("auth.internal_token_invalid")
    configured = request.app.state.config.internal_config_token
    if not internal_values:
        # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
        raise ApiError("auth.internal_token_required")
    if len(internal_values) != 1 or not internal_values[0] or b"," in internal_values[0]:
        # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
        raise ApiError("auth.internal_token_invalid")
    try:
        internal_token = internal_values[0].decode("ascii", errors="strict")
    # 服务 Token 的 wire 合同只允许 ASCII，非法字节不得参与密钥比较或错误记录。
    except UnicodeDecodeError:
        # 原始非法 Token 只映射固定错误码，不得进入响应、日志或异常链。
        raise ApiError("auth.internal_token_invalid") from None
    expected = configured or ""
    # 该治理分支必须保持租户隔离和失败关闭，并通过固定错误语义保留请求链路可追踪性。
    if (
        not expected
        or not expected.isascii()
        or not secrets.compare_digest(internal_token, expected)
    ):
        # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
        raise ApiError("auth.internal_token_invalid")


def require_session_risk_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户同时具备会话风险读取和处置权限。

    ``session_risk.manage`` 不隐含 read；自定义 manage-only 角色必须在正文和幂等 Header
    解析前得到统一 403。服务事务内还会再次复核两个权限，关闭路由通过后撤权竞态。
    """

    # manage 不隐含 read，两个权限必须同时由后端实时矩阵授予，不能依赖前端按钮可见性。
    return _require_request_permissions(
        request,
        authorization,
        ("session_risk.read", "session_risk.manage"),
    )


def require_policy_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取治理策略和版本摘要。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.read",))


def require_policy_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可创建和更新治理策略。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.write",))


def require_policy_simulator(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户具备执行无副作用策略模拟的独立后端权限。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.simulate",))


def require_policy_publisher(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可发布或回滚策略快照。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("policy.publish",))


def require_policy_template_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取内置策略模板目录和租户内影响预览。

    该权限只开放模板目录与服务端规划结果，不能隐式获得 Policy、路由、预算或 DLP
    的原始读取权限；运行时始终使用当前数据库权限快照，避免角色名硬编码造成越权。
    """

    # 模板目录是独立治理资源，只校验专用读权限，不能因用户拥有其他 Policy 权限而旁路放行。
    return _require_request_permissions(request, authorization, ("policy_template.read",))


def require_policy_template_applier(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可提交一次服务端冻结的策略模板应用命令。

    本依赖只完成 HTTP 入口的粗粒度权限门禁。服务层仍必须在 ``BEGIN IMMEDIATE``
    内重新读取权限矩阵并按模板类别复核复合领域权限，防止预览后撤权或跨域提权。
    """

    # 入口先拒绝无模板应用权的请求；租户归属与复合领域权限仍由事务内服务校验负责。
    return _require_request_permissions(request, authorization, ("policy_template.apply",))


def require_permission_matrix_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取和修改租户角色权限矩阵。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("rbac.matrix.manage",))


def require_system_config_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取脱敏运行配置摘要。"""

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(request, authorization, ("system.config.read",))


def require_config_transfer_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可导出严格白名单租户配置。

    该独立权限不隐含 Provider、Model、路由或 Virtual Key 普通读取权限；后端仍以本次请求
    从数据库加载的矩阵快照为唯一依据，不能按 Admin/Developer/OpsAdmin 角色名推导放行。
    """

    # 导出会暴露租户治理结构，必须逐请求读取数据库权限快照，不能信任 Token 内旧角色声明。
    return _require_request_permissions(request, authorization, ("config.transfer.read",))


def require_config_transfer_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可执行 create-only 原子配置导入。

    写权限包含生成全新 Virtual Key 的高风险副作用，因此必须使用专用 critical 权限；普通
    Provider/Model/路由/Virtual Key 写权限的任意组合都不能替代该后端检查。
    """

    # 导入会原子写入多类租户资源，必须在解析正文前完成专用 critical 权限校验。
    return _require_request_permissions(request, authorization, ("config.transfer.write",))


def require_internal_service_token(
    request: Request,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    """要求调用方提供控制面配置的内部服务 Token。

    与早期配置摘要接口不同，策略快照会暴露可执行 canonical expression，因此 dev/test
    也不得免认证。比较使用常量时间算法，错误响应和日志均不能泄露配置是否存在或 Token 内容。
    """

    configured = request.app.state.config.internal_config_token
    # 内部服务 Token 缺失时必须在读取任何租户快照前拒绝，且返回统一认证错误。
    if internal_token is None or not internal_token:
        raise ApiError("auth.internal_token_required")
    # 缺少服务端配置与 Token 不匹配共享固定错误，避免未授权调用者探测部署状态。
    expected = configured or ""
    if not expected or not secrets.compare_digest(internal_token, expected):
        raise ApiError("auth.internal_token_invalid")


def require_webhook_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以管理 Webhook Endpoint。

    Webhook 会把预算、DLP 和失败事件推送到企业系统；M0 尚未拆出 ops/security 角色，
    因此先开放给 admin/developer，viewer 即使手写 HTTP 请求也必须被后端拒绝。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("webhook.manage",),
    )


def require_developer_portal_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可读取开发者门户内容。

    快速开始会暴露模型别名、Gateway base_url 和 Key 前缀等调用准备信息，M0 只开放给管理员和开发者。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("developer_portal.read",),
    )


def require_debug_console_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前用户可以使用内置 API 调试台。

    调试台会触发真实上游模型调用并写入用量账本，因此只开放给管理员和开发者；
    viewer 即使前端看不到入口，也必须由后端依赖再次拦截，避免通过手写 HTTP 请求
    绕过控制台按钮隐藏。
    """

    # 当前后端 permission 快照是唯一授权依据，禁止按角色名或前端可见性补权。
    return _require_request_permissions(
        request,
        authorization,
        ("debug_console.use",),
    )


def require_enterprise_chat_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有企业 Chat 独立发送权限。

    参数：Control 请求和 Session Bearer Token。返回：从当前租户数据库实时复核的认证上下文。
    关键约束：``chat.use`` 不能由 project/model/debug 权限或角色名推导；撤权后的下一次
    options、建会话、发送和清空必须立即得到统一 403，前端隐藏入口不参与授权结论。
    """

    # 企业 Chat 会产生真实模型调用和成本，必须在正文解析前完成独立后端权限检查。
    return _require_request_permissions(request, authorization, ("chat.use",))


def require_prompt_template_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有模板定义和不可变历史读取权限。

    参数：Control 请求与 Bearer Session。返回：实时数据库矩阵复核后的当前租户上下文。
    关键约束：``prompt_template.use`` 只授权 Chat 最小投影和本次渲染结果，不能替代定义读取；
    Viewer 手写管理 API 也必须在解析 path/query/body 之前得到统一 403。
    """

    # 管理定义包含可解密 Prompt，必须逐请求读取实时矩阵，不能由 use 权限或前端入口可见性补权。
    return _require_request_permissions(request, authorization, ("prompt_template.read",))


def require_prompt_template_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有模板创建、启停和版本发布权限。

    参数：Control 请求与 Bearer Session。返回：实时权限上下文。关键约束：该 critical 权限
    必须独立于 read/use；业务服务仍需在 ``BEGIN IMMEDIATE`` 内再次复核，关闭鉴权后撤权竞态。
    """

    # 创建、启停和发布会产生不可变版本及审计事实，因此只接受数据库实时 write 授权。
    return _require_request_permissions(request, authorization, ("prompt_template.write",))


def require_prompt_template_catalog_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """允许具有模板 read 或 write 任一权限的用户读取表单最小候选目录。

    参数：Control 请求与 Bearer Session。返回：实时数据库矩阵上下文。该 any-of 只服务
    ``/prompt-templates/options``，不能授予 Project/路由管理权限，也不能按角色名或共享默认值补权。
    """

    _reject_mixed_workspace_credential(request, authorization)
    context = authenticate_bearer(request.app.state.config, authorization)
    # options 只允许管理侧 read/write 用户，缺权时统一拒绝，避免借候选目录枚举项目或模型。
    if not {"prompt_template.read", "prompt_template.write"}.intersection(context.permission_codes):
        # 候选目录也属于受保护资源投影，缺少两项管理权限时不能仅凭登录态访问。
        raise ApiError("auth.permission_denied")
    return context


def require_file_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有 tenant 文件安全投影读取权限。

    参数：Control 请求与 Workspace Cookie 或 Legacy Bearer。返回实时复核后的 AuthContext。
    关键约束：file.write 不隐含 read，自定义 write-only 角色不能借详情/列表枚举文件名。
    """

    # 文件名虽以密文落库，但授权 API 会短暂解密展示，因此必须在 path/query 解析前逐请求校验。
    return require_workspace_or_legacy_permissions("file.read")(request, authorization)


def require_file_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前登录 Session 具有文件上传、解析和 DLP 预扫权限。

    参数：Control 请求与 Workspace Cookie 或 Legacy Bearer。返回实时复核后的 AuthContext。
    关键约束：依赖必须先于 multipart 正文读取；服务事务还会二次复核，关闭撤权竞态。
    """

    # 上传会接触企业正文并产生持久对象，前端按钮、角色名或 chat.use 都不能替代独立权限。
    return require_workspace_or_legacy_permissions("file.write")(request, authorization)


def require_file_security_scanner(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 同时具备文件读取和独立安全扫描命令权限。

    参数：Control 请求与 Bearer Session。返回：实时数据库矩阵复核后的 AuthContext。关键约束：
    dependency 在 path/query/body 解析前执行；``file.read``、``file.write`` 或前端可见性都不能替代
    ``file.security.scan``，避免无权请求触发企业 Blob 解密和 OCR 成本。
    """

    # 两项权限必须由同一实时矩阵快照同时证明，不能先读后扫形成撤权或跨 Session 竞态。
    return _require_request_permissions(
        request,
        authorization,
        ("file.read", "file.security.scan"),
    )


def require_batch_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """建立批任务只读依赖，在解析业务参数前按实时后端矩阵校验读取权限。

    Require the current database permission snapshot to contain ``batch.read``.
    """

    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return _require_request_permissions(request, authorization, ("batch.read",))


def require_batch_writer(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """建立批任务写入依赖，在解析业务参数前按实时后端矩阵校验写权限。

    Require the current database permission snapshot to contain ``batch.write``.
    """

    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return _require_request_permissions(request, authorization, ("batch.write",))


def require_batch_creator(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """建立批任务创建依赖，同时校验批写入与文件读取权限后才允许解析正文。

    Require both Batch mutation and file-read authority before parsing a create body.
    """

    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return _require_request_permissions(
        request,
        authorization,
        ("batch.write", "file.read"),
    )


def require_batch_canceller(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """建立批任务取消依赖，同时校验读取和写入权限以返回最新任务事实。

    Require read and write authority because cancellation returns the current job fact.
    """

    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return _require_request_permissions(
        request,
        authorization,
        ("batch.read", "batch.write"),
    )


def require_batch_task_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """建立批任务结果读取依赖，同时校验批读取与文件读取权限。

    Require access to both a Batch task and its file-derived projections.
    """

    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return _require_request_permissions(
        request,
        authorization,
        ("batch.read", "file.read"),
    )


def require_batch_reconciler(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """建立批任务对账依赖，同时校验批读取与财务调整权限。

    Require Batch visibility and FinOps write authority for possible-cost resolution.
    """

    # 权限以后端实时矩阵为准；缺失任一所需权限时拒绝并保留脱敏审计事实。
    return _require_request_permissions(
        request,
        authorization,
        ("batch.read", "billing.finops.write"),
    )


def require_mcp_reader(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """在解析 MCP path/query 前校验当前租户的治理读取权限。"""

    return _require_request_permissions(request, authorization, ("mcp.read",))


def require_mcp_server_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """在读取 Endpoint 或 Bearer 正文前校验 Server 管理权限。"""

    return _require_request_permissions(request, authorization, ("mcp.server.manage",))


def require_mcp_policy_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """要求当前 Session 具有逐工具治理策略管理权限。"""

    return _require_request_permissions(request, authorization, ("mcp.policy.manage",))


def require_mcp_network_manager(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """Require MCP visibility and network-policy authority before reading the body."""

    return _require_request_permissions(
        request,
        authorization,
        ("mcp.read", "mcp.network.manage"),
    )


def require_mcp_server_refresher(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """刷新发现会同时修改 Server 与 Tool 治理事实，必须双权限命中。"""

    return _require_request_permissions(
        request,
        authorization,
        ("mcp.server.manage", "mcp.policy.manage"),
    )


def require_mcp_invoker(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthContext:
    """在解析可能敏感的 MCP arguments 前校验基础调用权限。"""

    return _require_request_permissions(request, authorization, ("mcp.invoke",))
