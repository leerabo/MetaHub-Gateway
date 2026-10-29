"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/fastapi_app.py
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTasks
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from .config import (
    AppConfig,
    ConfigError,
    build_service_info,
    config_loaded_log_payload,
    load_app_config,
)
from .errors import ApiError, build_error_response, list_error_codes
from .health import build_liveness_result, build_readiness_result
from .logging import configure_json_logging
from .request_id import REQUEST_ID_HEADER, TRACE_ID_HEADER, resolve_request_id, resolve_trace_id

logger = logging.getLogger("metahub.runtime")
_CLEAR_IDENTITY_COOKIES_ON_ERROR_STATE = "metahub_clear_identity_cookies_on_error"
_CLEAR_IDENTITY_COOKIES_ON_AUTH_ERROR_STATE = "metahub_clear_identity_cookies_on_auth_error"
_CLEAR_WORKSPACE_COOKIES_ON_ERROR_STATE = "metahub_clear_workspace_cookies_on_error"


def clear_identity_cookies_on_error(request: Request) -> None:
    """Ensure an authorized logout attempt clears browser credentials on every outcome."""

    setattr(request.state, _CLEAR_IDENTITY_COOKIES_ON_ERROR_STATE, True)


def clear_identity_cookies_on_auth_error(request: Request) -> None:
    """Clear identity credentials only when the authenticated request is rejected as 401."""

    setattr(request.state, _CLEAR_IDENTITY_COOKIES_ON_AUTH_ERROR_STATE, True)


def clear_workspace_cookies_on_error(request: Request) -> None:
    """Clear a rejected Workspace credential when authentication returns 401."""

    setattr(request.state, _CLEAR_WORKSPACE_COOKIES_ON_ERROR_STATE, True)


# 业务路由仅在已提交安全事实后向这里绑定任务；统一错误处理器据此保留响应后的尽力副作用。
ERROR_RESPONSE_BACKGROUND_TASKS_STATE = "metahub_error_response_background_tasks"


def create_service_app(
    service_name: str,
    title: str,
    *,
    extra_cors_allow_methods: tuple[str, ...] = (),
    extra_cors_allow_headers: tuple[str, ...] = (),
    extra_cors_expose_headers: tuple[str, ...] = (),
    cors_allow_credentials: bool = False,
) -> FastAPI:
    """创建 PRD-001 服务空壳。

    数据面和控制面共用这套工厂函数，确保 /health、/version、错误响应、
    request_id 和日志脱敏从第一天起保持一致。额外 CORS 头必须由确实提供该响应
    合同的服务显式声明，避免控制面功能无意扩大 Gateway 浏览器接口。后续 PRD 新增
    业务路由时，应在各自服务包内注册，不能绕过这里的中间件和异常处理。
    """

    config = load_app_config(service_name)
    configure_json_logging(config.log_level)
    logger.info(
        "config.loaded",
        extra={
            "service_name": service_name,
            "event": "config.loaded",
            "request_id": "startup",
            "trace_id": "startup",
            "status_code": 0,
            "config_summary": config_loaded_log_payload(config),
        },
    )
    logger.debug(
        "config.summary",
        extra={
            "service_name": service_name,
            "event": "config.loaded",
            "request_id": "startup",
            "trace_id": "startup",
            "status_code": 0,
            "config_summary": config_loaded_log_payload(config),
        },
    )

    app = FastAPI(title=title, version="0.1.0-dev")
    # 各服务只能在启动期自检成功后写入固定能力名；默认空集合兼容旧服务并防止误报能力。
    app.state.runtime_capabilities = ()
    app.state.config = config

    cors_expose_headers = (
        REQUEST_ID_HEADER,
        TRACE_ID_HEADER,
        "Content-Disposition",
        *extra_cors_expose_headers,
    )
    if cors_allow_credentials and "*" in config.allowed_origins:
        raise ConfigError(
            code="infra.config_invalid",
            message="Credentialed CORS requires an exact origin allowlist.",
            suggestion="Replace the wildcard with explicit MetaHub console origins.",
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allowed_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=[
            "GET",
            "POST",
            "PATCH",
            "DELETE",
            "OPTIONS",
            *extra_cors_allow_methods,
        ],
        allow_headers=[
            REQUEST_ID_HEADER,
            TRACE_ID_HEADER,
            "Content-Type",
            "Authorization",
            "X-Internal-Token",
            *extra_cors_allow_headers,
        ],
        # 浏览器只能读取 CORS 响应中显式暴露的非简单响应头。调试台依赖这两个链路标识
        # 跳转到调用日志，因此必须由共享运行时统一暴露，不能让各业务路由重复配置。
        # CSV 导出文件名使用标准 Content-Disposition；服务专属响应头只能经显式扩展暴露。
        expose_headers=list(cors_expose_headers),
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next: Callable):  # type: ignore[type-arg]
        # 每个请求进入服务后立刻确定 request_id/trace_id，并在响应头中透出。
        # 二者都存入 request.state，避免并发请求通过全局变量串号。
        """实现 共享运行时 的 request context middleware 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        started = time.perf_counter()
        # 绝对进入时刻供需要 endpoint-wide 预算的业务依赖复用；认证、RBAC 和路由不能
        # 分别重置时钟。deadline 全链路统一使用 monotonic，访问延迟继续使用 perf_counter。
        request.state.request_started_monotonic = time.monotonic()
        authorization_values = tuple(
            value.decode("latin-1")
            for name, value in request.scope.get("headers", ())
            if name.lower() == b"authorization"
        )
        authorization = request.headers.get("Authorization")
        authorization_sensitive_values = _sensitive_header_candidates(authorization_values)
        invitation_token = _invitation_path_credential(request.url.path)
        # Adapter 发布 Token 与用户 Bearer 一样属于不可回显凭据；内部激活路径必须在
        # request_id/trace_id 归一前纳入碰撞扫描，防止调用方把 Token 填进链路 Header。
        raw_adapter_internal_tokens = (
            tuple(
                value.decode("latin-1")
                for name, value in request.scope.get("headers", ())
                if name.lower() == b"x-internal-token"
            )
            if request.url.path.startswith("/internal/adapter-specs/")
            or (
                request.url.path.startswith("/internal/tenants/")
                and (
                    request.url.path.endswith("/adapter-contract-runs")
                    or request.url.path.endswith("/approvals")
                )
            )
            else ()
        )
        adapter_internal_sensitive_values = _sensitive_header_candidates(
            raw_adapter_internal_tokens
        )
        # 企业 Chat 使用独立服务身份，不能复用 Adapter 或用户 Bearer Token。内部路由
        # 在任何认证结果前都要把该值加入链路 ID 碰撞扫描，避免错误响应头和访问日志回显。
        chat_internal_token = (
            request.headers.get("X-MetaHub-Internal-Chat-Token")
            if request.url.path == "/internal/chat/completions"
            else None
        )
        batch_internal_tokens = (
            tuple(request.headers.getlist("X-MetaHub-Internal-Batch-Token"))
            if request.url.path == "/internal/batch/chat/completions"
            else ()
        )
        sensitive_values = tuple(
            value
            for value in (
                *authorization_sensitive_values,
                invitation_token,
                *adapter_internal_sensitive_values,
                chat_internal_token,
                *batch_internal_tokens,
            )
            if value is not None
        )
        # 链路 Header 与 Bearer credential 碰撞时先换成内部 ID，后续错误、响应和
        # access log 均不得泄露。邀请接受把一次性凭据放在 path，因此也必须在
        # 业务路由执行前纳入碰撞检测，不能等数据库校验后才开始脱敏。
        # 创建正文含邀请邮箱、公开 accept 正文含密码，而 middleware 在读取正文前无法知道
        # 这些值；敏感意图分类和账单争议正文也可能被攻击者原样复制到链路 Header。争议
        # detail 还会返回授权 reason，因此其整个路径前缀都不接受客户端链路 ID。统一生成
        # 内部 ID，避免响应头、access log 或持久 source identity 二次回显。
        sensitive_request_boundary = (
            invitation_token is not None
            # Every MCP route may carry an endpoint, bearer, arguments, result, or Schema.
            # Client-provided correlation IDs are therefore never trusted on this prefix.
            or request.url.path == "/identity"
            or request.url.path.startswith("/identity/")
            or request.url.path == "/workspaces"
            or request.url.path.startswith("/workspaces/")
            or request.url.path == "/edge"
            or request.url.path.startswith("/edge/")
            or request.url.path == "/mcp"
            or request.url.path.startswith("/mcp/")
            or (request.method == "POST" and request.url.path.rstrip("/") == "/invitations")
            or (
                (
                    request.url.path == "/billing-disputes"
                    or request.url.path.startswith("/billing-disputes/")
                )
                and _request_matches_registered_route(request)
            )
            or (
                request.method == "POST"
                and request.url.path.rstrip("/") == "/security/classify-intent"
            )
            or (request.method == "POST" and request.url.path.rstrip("/") == "/webhooks")
            or (
                request.method == "POST"
                and request.url.path.startswith("/internal/tenants/")
                and request.url.path.endswith("/approvals")
            )
            or (
                request.method == "POST"
                and request.url.path.startswith("/approvals/")
                and request.url.path.rsplit("/", 1)[-1] in {"step-up", "approve", "reject"}
                and _request_matches_registered_route(request)
            )
        )
        incoming_request_id = (
            None if sensitive_request_boundary else request.headers.get(REQUEST_ID_HEADER)
        )
        incoming_trace_id = (
            None if sensitive_request_boundary else request.headers.get(TRACE_ID_HEADER)
        )
        request_id = resolve_request_id(
            incoming_request_id,
            authorization=authorization,
            sensitive_values=sensitive_values,
        )
        trace_id = resolve_trace_id(
            incoming_trace_id,
            request_id,
            authorization=authorization,
            sensitive_values=sensitive_values,
        )
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = trace_id
        _apply_path_cache_control(request, response)
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "request.completed",
            extra={
                "request_id": request_id,
                "trace_id": trace_id,
                "service_name": service_name,
                "event": "request.completed",
                "path": _safe_log_path(request),
                "method": request.method,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        # 可预期错误直接按统一错误码返回，不暴露内部实现细节。
        """实现 共享运行时 的 api error handler 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        return _error_response(request, exc)

    @app.exception_handler(ConfigError)
    async def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
        # 配置错误在 API 层同样归一，确保调用方永远能拿到 code/request_id/suggestion。
        """实现 共享运行时 的 config error handler 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        return _error_response(
            request,
            ApiError(exc.code, message=exc.message, suggestion=exc.suggestion),
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        # Starlette 默认 404 结构不符合本项目契约，所以这里显式转换。
        """实现 共享运行时 的 not found handler 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        response = _error_response(request, ApiError("infra.not_found"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(405)
    async def method_not_allowed_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """把框架 405 转换成统一错误体，并只保留路由器计算出的 Allow。

        Allow 是客户端修正请求方法所需的标准响应头；异常携带的其他头可能来自业务
        代码或底层框架，不能未经审查透传。CORS 暴露列表也不全局加入 Allow，避免
        为所有接口扩大浏览器可读取的响应头合同。合法预检在更外层 CORSMiddleware 内
        直接完成，因此这里只处理普通不支持方法和不带预检头的 OPTIONS。
        """

        response = _error_response(request, ApiError("infra.method_not_allowed"))
        # Starlette 当前使用普通 dict 保存异常头，但这里按大小写不敏感方式匹配，避免
        # 框架升级改变键大小写后丢失标准 Allow；其他头一律不复制。
        allow = next(
            (value for name, value in (exc.headers or {}).items() if name.lower() == "allow"),
            None,
        )
        if allow:
            response.headers["Allow"] = allow
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI 默认 422 不带 request_id/trace_id，统一转成本项目错误结构。
        """实现 共享运行时 的 validation error handler 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        return _error_response(request, ApiError("infra.validation_failed"))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # 未捕获异常必须进入错误日志，但响应只返回通用错误码，避免泄露堆栈和环境信息。
        """实现 共享运行时 的 unhandled handler 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        # 邀请 path 本身携带凭据；管理助手和账单争议异常对象还可能携带自由文本正文。
        # 这些边界只记录固定错误事实，不附加 exc_info；其他路由保留既有 traceback。
        safe_path = _safe_log_path(request)
        failure_logger = (
            logger.error
            if _invitation_path_credential(request.url.path) is not None
            or safe_path == "/admin-assistant/query"
            or safe_path == "/billing-disputes"
            or safe_path == "/billing-disputes/{billing_dispute_id}"
            or safe_path == "/identity"
            or safe_path.startswith("/identity/")
            or safe_path == "/workspaces"
            or safe_path.startswith("/workspaces/")
            or safe_path == "/mcp"
            or safe_path.startswith("/mcp/")
            else logger.exception
        )
        failure_logger(
            "request.failed",
            extra={
                "request_id": _request_id(request),
                "trace_id": _trace_id(request),
                "service_name": service_name,
                "event": "request.failed",
                "path": safe_path,
                "method": request.method,
                "error_code": "infra.unhandled",
            },
        )
        # Exception handler 由最外层 ServerErrorMiddleware 调用，响应不会再穿过用户中间件。
        # 因此必须在这里补齐敏感路径缓存策略和 CORS；否则数据库/认证基础设施故障产生的
        # 500 可能被共享缓存，浏览器也无法读取统一错误码和 request_id。
        response = _error_response(request, ApiError("infra.unhandled"))
        _apply_path_cache_control(request, response)
        _apply_unhandled_cors_headers(
            request,
            response,
            allowed_origins=config.allowed_origins,
            expose_headers=cors_expose_headers,
            allow_credentials=cors_allow_credentials,
        )
        return response

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        """实现 共享运行时 的 health 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        current_config = _config(request)
        return {
            "status": "ok",
            "service_name": current_config.service_name,
            "environment": current_config.environment,
            "request_id": _request_id(request),
            "trace_id": _trace_id(request),
        }

    @app.get("/internal/health")
    async def internal_health(request: Request) -> dict[str, str]:
        # 内部 health 与 /health 共用轻量检查，不依赖数据库/Redis/Provider。
        """实现 共享运行时 的 internal health 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        current_config = _config(request)
        return {
            "status": "ok",
            "service_name": current_config.service_name,
            "environment": current_config.environment,
            "request_id": _request_id(request),
            "trace_id": _trace_id(request),
        }

    @app.get("/version")
    async def version(request: Request) -> dict[str, object]:
        # 版本信息用于本地启动页和交付排障，必须带 request_id。
        """实现 共享运行时 的 version 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        info = build_service_info(_config(request), _request_id(request), _trace_id(request))
        return info.model_dump(mode="json")

    @app.get("/error-codes")
    async def error_codes(request: Request, locale: str = "zh-CN") -> dict[str, object]:
        """返回统一错误码和基础诊断规则元数据。

        该接口只暴露全局只读服务元数据，不读取 tenant_id；locale 只影响展示 label，
        不影响内部错误码。
        """

        return {
            "error_codes": [item.model_dump(mode="json") for item in list_error_codes(locale)],
            "request_id": _request_id(request),
            "trace_id": _trace_id(request),
        }

    @app.get("/health/live")
    async def health_live(request: Request) -> dict[str, object]:
        """实现 共享运行时 的 health live 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        result = build_liveness_result(_config(request), _request_id(request), _trace_id(request))
        return result.model_dump(mode="json")

    @app.get("/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        """实现 共享运行时 的 health ready 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        result = build_readiness_result(
            _config(request),
            _request_id(request),
            _trace_id(request),
            capabilities=tuple(getattr(request.app.state, "runtime_capabilities", ())),
        )
        status_code = 200 if result.ready else 503
        return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))

    return app


def _sensitive_header_candidates(values: tuple[str, ...]) -> tuple[str, ...]:
    """提取 raw 敏感 Header 的完整值、逗号分段和分段内 token。"""

    candidates: list[str] = []
    for value in values:
        whole = value.strip()
        if whole:
            candidates.append(whole)
        for raw_segment in value.split(","):
            segment = raw_segment.strip()
            if not segment:
                continue
            candidates.append(segment)
            candidates.extend(token for token in segment.split() if token)
    return tuple(dict.fromkeys(candidates))


def _config(request: Request) -> AppConfig:
    """在 共享运行时 内部复用 config 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return request.app.state.config


def _apply_path_cache_control(request: Request, response: Response) -> None:
    """按业务路径统一设置缓存边界。

    Feedback 响应包含租户内评分与统计，所有状态码都必须禁止浏览器和共享代理缓存；
    ModelAlias 会投影分类、路由和亲和配置，也必须按私有状态禁止缓存；Policy 正文同样
    属于管理员敏感配置。该函数同时供普通中间件与最外层 500 处理复用，避免异常拓扑
    绕过安全 Header。
    """

    path = request.url.path
    if (
        path == "/identity"
        or path.startswith("/identity/")
        or path == "/workspaces"
        or path.startswith("/workspaces/")
        or path == "/consumers"
        or path.startswith("/consumers/")
        or path == "/virtual-keys"
        or path.startswith("/virtual-keys/")
    ):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
    elif path == "/mcp" or path.startswith("/mcp/"):
        # MCP responses can expose tenant-private governance state and the one authorized,
        # redacted result.  The outer boundary overrides every success and error response.
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
    elif path == "/invitations" or path.startswith("/invitations/"):
        # 创建响应含仅本次可见 Token，接受响应也属于凭据换取的私有身份结果；任何状态码
        # 都禁止浏览器/共享代理缓存，并阻止当前 URL 成为后续导航 Referer。
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Referrer-Policy"] = "no-referrer"
    elif path == "/files" or path.startswith("/files/"):
        # 文件响应会投影企业文档名和租户内安全状态；认证、权限、multipart、容量和未处理
        # 异常都可能在业务路由设置 Header 之前结束，因此由最外层覆盖全部状态码。
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/request-logs" or path.startswith("/request-logs/"):
        # RequestLog 详情和输出安全证据属于租户私有排障数据；权限拒绝、跨租户 404
        # 与完整性 503 都会在路由返回前重建 Response，因此必须由最外层覆盖所有状态码。
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/finops" or path.startswith("/finops/"):
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/usage" or path.startswith("/usage/"):
        # Usage and export ownership are private on success and every error path.
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/budgets" or path.startswith("/budgets/") or path == "/budget-status":
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/feedback" or path.startswith("/feedback/"):
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/model-aliases" or path.startswith("/model-aliases/"):
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/policies" or path.startswith("/policies/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.rstrip("/") == "/security/classify-intent":
        # 单次分类正文只允许存在于调用栈；成功、权限拒绝、校验错误和未处理异常都必须
        # 禁止浏览器及共享代理缓存，不能只依赖业务路由在 200 响应上补 Header。
        response.headers["Cache-Control"] = "no-store"
    elif path == "/security/session-risks" or path.startswith("/security/session-risks/"):
        # 会话风险报表和处置结果属于租户内安全事实；认证、权限与正文校验错误都可能在
        # 路由函数设置响应头之前返回，因此必须在最外层按路径覆盖所有状态码。
        response.headers["Cache-Control"] = "no-store"
    elif path.rstrip("/") == "/v1/chat/completions":
        # 公共 Chat 的成功正文和治理错误都可能关联 Prompt/Response；会话风险预检还会在
        # Provider 调用与业务响应构造前失败，因此必须由最外层对所有状态码禁止缓存。
        response.headers["Cache-Control"] = "no-store"
    elif path == "/batch-jobs" or path.startswith("/batch-jobs/"):
        # Batch responses expose tenant-private job, task, cost and result state. Apply the
        # private cache boundary to success and every middleware-generated error response.
        response.headers["Cache-Control"] = "no-store, private"
    elif (
        path == "/billing-disputes" or path.startswith("/billing-disputes/")
    ) and _request_matches_registered_route(request):
        # Dispute detail contains tenant-private billing evidence and authorized free text.
        # Authentication, validation and unhandled failures must share the same boundary.
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/local-endpoints" or path.startswith("/local-endpoints/"):
        # Local endpoint state is tenant-private, including authentication and unhandled failures.
        response.headers["Cache-Control"] = "no-store, private"
    elif path.rstrip("/") == "/admin-assistant/query" and _request_matches_registered_route(
        request
    ):
        # 管理助手的成功、认证拒绝、wire 错误和最外层未捕获异常都可能关联租户私有查询。
        # 统一在共享异常出口补齐 exact Header，避免 500 绕过 Control 层用户中间件。
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
    elif (
        path == "/approvals" or path.startswith("/approvals/")
    ) and _request_matches_registered_route(request):
        # 审批列表、详情、密码二次验证与决策都属于租户私有治理状态。认证、权限、
        # wire 校验和未捕获异常可能早于路由 Header，因此最外层统一覆盖所有状态码。
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Authorization"
    # 内部审批创建使用服务 Token，缓存隔离必须区分服务身份而不是用户 Authorization。
    elif path.startswith("/internal/tenants/") and path.endswith("/approvals"):
        # Vary 绑定唯一内部 Token Header，避免代理把不同服务身份的私有响应混用。
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "X-Internal-Token"
    elif path == "/experiments" or path.startswith("/experiments/"):
        # Experiment configuration and result snapshots are tenant-private. Authentication,
        # validation, owner and infrastructure failures must share the same cache boundary.
        response.headers["Cache-Control"] = "no-store, private"
    elif (
        path == "/chat"
        or path.startswith("/chat/")
        or path == "/internal/chat/completions"
        or path == "/internal/batch/chat/completions"
    ):
        # Chat 响应可能含本次 assistant 正文、治理摘要或内部认证结果；业务异常会重建
        # Response，因此所有状态码都必须由最外层路径策略统一禁止缓存。
        response.headers["Cache-Control"] = "no-store"
    elif path == "/prompt-templates" or path.startswith("/prompt-templates/"):
        # Prompt 模板错误可能在认证/权限依赖阶段产生，早于业务路由设置响应头；最外层
        # 路径策略必须覆盖 401/403/404/409/422/500/503，避免模板存在性、授权状态或
        # 解密失败结果进入浏览器及共享缓存。正文仍由各路由和服务负责不回显。
        response.headers["Cache-Control"] = "no-store"
    elif (
        path == "/webhooks"
        or path.startswith("/webhooks/")
        or path == "/webhook-deliveries"
        or path.startswith("/webhook-deliveries/")
        or path == "/webhook-dead-letters"
        or path.startswith("/webhook-dead-letters/")
    ):
        # Endpoint 配置、投递快照与死信均属于租户私有安全状态。认证、RBAC、exact wire
        # 校验和未捕获异常会在业务路由设置响应头之前返回，因此所有状态码统一禁止缓存。
        response.headers["Cache-Control"] = "no-store"
    elif path == "/edge" or path.startswith("/edge/"):
        # Registration and session responses may contain one-time credentials. Apply the
        # same boundary to authentication, validation, and unhandled failures.
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
    elif path == "/platform/fleet/edge-deployments" or path.startswith(
        "/platform/fleet/edge-deployments/"
    ):
        response.headers["Cache-Control"] = "no-store, private"
    elif path == "/config" or path.startswith("/config/"):
        # 配置迁移响应可能包含租户拓扑、校验明细或仅本次可见的 Virtual Key。
        # 业务路由抛出 ApiError 后 FastAPI 会重建响应，因此必须由最外层中间件按路径
        # 再次施加 no-store，确保 400/401/403/409/413 等错误响应也不会进入共享缓存。
        response.headers["Cache-Control"] = "no-store"
    elif (
        path.startswith("/internal/adapter-specs/")
        or path == "/v1/adapter-contract-probes/identity"
        or (path.startswith("/internal/tenants/") and path.endswith("/adapter-contract-runs"))
        or (
            (
                path == "/adapter-specs"
                or path.startswith("/adapter-specs/")
                or path == "/adapter-contract-runs"
                or path.startswith("/adapter-contract-runs/")
            )
            and _request_matches_registered_route(request)
        )
    ):
        # 目录正文、租户激活身份和 probe Key 归属证明都属于授权后的治理状态；权限、校验
        # 和未捕获异常响应也必须由同一最外层路径策略禁止缓存。合同报告还包含 CI 运行和
        # 失败摘要，不能只依赖成功路由设置 Header，否则 401/403/409/422/503 可能进入缓存。
        response.headers["Cache-Control"] = "no-store"


def _request_matches_registered_route(request: Request) -> bool:
    """Return whether the current method and path belong to an installed route."""

    if request.scope.get("route") is not None:
        return True
    return any(route.matches(request.scope)[0] is Match.FULL for route in request.app.routes)


def _invitation_path_credential(path: str) -> str | None:
    """从固定接受路径提取只用于内存脱敏的一次性 Invitation 凭据。

    参数：ASGI 已解析但尚未经过业务路由的 URL path。
    返回：形如 ``/invitations/<token>/accept`` 的单段凭据，否则返回 None。
    约束：不判断 Token 是否有效，也不记录返回值；恶意格式只要占据该凭据位置就按
    敏感值处理，避免 404/422 或数据库失败分支先把它回显到链路 Header。
    """

    prefix = "/invitations/"
    suffix = "/accept"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    credential = path[len(prefix) : -len(suffix)]
    if not credential or "/" in credential:
        return None
    return credential


def _safe_log_path(request: Request) -> str:
    """只返回服务端声明的路由模板，未知路径使用固定占位符。

    动态 path 参数可能承载 Invitation Token、对象 ID 或客户输入；日志聚合只需要路由
    维度，不需要原始值。Starlette 匹配后会把 Route 放入 scope，业务错误和未处理异常
    也复用该结果。未匹配请求不能回退原始 path，否则攻击者可借 404 注入敏感文本。
    """

    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    # 只信任应用注册的绝对路由模板，未知对象不得回退为攻击者提供的原始 path。
    if type(route_path) is str and route_path.startswith("/"):
        return route_path
    return "<unmatched>"


def _apply_unhandled_cors_headers(
    request: Request,
    response: Response,
    *,
    allowed_origins: list[str],
    expose_headers: tuple[str, ...],
    allow_credentials: bool = False,
) -> None:
    """为绕过内层 CORS 中间件的未捕获异常补齐浏览器响应头。

    参数 ``allowed_origins`` 和 ``expose_headers`` 与应用注册 CORSMiddleware 时完全相同。
    只回显明确允许的 Origin；通配来源保持 ``*``，因为本项目关闭 CORS credentials。
    该顺序与当前 Starlette 简单响应一致，不能把任意攻击者 Origin 反射到 500 响应。
    """

    origin = request.headers.get("Origin")
    if origin is None:
        return

    # Starlette 会先附加公开响应头，再判断调用来源是否允许读取正文；这里保持同一机器合同。
    if expose_headers:
        response.headers["Access-Control-Expose-Headers"] = ", ".join(expose_headers)

    allow_all_origins = "*" in allowed_origins
    if not allow_all_origins and origin not in allowed_origins:
        return

    if allow_all_origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    else:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers.add_vary_header("Origin")
    if allow_credentials:
        response.headers["Access-Control-Allow-Credentials"] = "true"


def _request_id(request: Request) -> str:
    """在 共享运行时 内部复用 request id 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "request_id", resolve_request_id(None))


def _trace_id(request: Request) -> str:
    """在 共享运行时 内部复用 trace id 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "trace_id", resolve_trace_id(None, _request_id(request)))


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    """记录失败请求并返回统一错误结构。"""

    request_id = _request_id(request)
    trace_id = _trace_id(request)
    logger.warning(
        "request.failed",
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "service_name": (
                _config(request).service_name if hasattr(request.app.state, "config") else ""
            ),
            "event": "request.failed",
            "path": _safe_log_path(request),
            "method": request.method,
            "status_code": error.http_status,
            "error_code": error.code,
        },
    )
    response = build_error_response(error, request_id, trace_id)
    diagnosis = response.diagnosis
    logger.info(
        "error.diagnosed",
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "service_name": (
                _config(request).service_name if hasattr(request.app.state, "config") else ""
            ),
            "event": "error.diagnosed",
            "path": _safe_log_path(request),
            "method": request.method,
            "status_code": error.http_status,
            "error_code": error.code,
            "diagnosis_rule_id": diagnosis.rule_id,
            "category": diagnosis.category,
            "retryable": diagnosis.retryable,
        },
    )
    background_tasks = getattr(
        request.state,
        ERROR_RESPONSE_BACKGROUND_TASKS_STATE,
        None,
    )
    # request.state 可被业务代码写入任意对象；只接受 Starlette 的受控任务容器，避免错误
    # handler 调用攻击者可控对象，或把非任务状态误挂到响应生命周期。
    if not isinstance(background_tasks, BackgroundTasks):
        background_tasks = None
    error_response = JSONResponse(
        status_code=error.http_status,
        content=response.model_dump(),
        headers={REQUEST_ID_HEADER: request_id, TRACE_ID_HEADER: trace_id},
        background=background_tasks,
    )
    if (
        getattr(request.state, _CLEAR_IDENTITY_COOKIES_ON_ERROR_STATE, False) is True
        or (
            error.http_status == 401
            and getattr(request.state, _CLEAR_IDENTITY_COOKIES_ON_AUTH_ERROR_STATE, False)
            is True
        )
    ):
        error_response.delete_cookie(
            "__Host-metahub-global-session",
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        error_response.delete_cookie(
            "__Host-metahub-global-csrf",
            path="/",
            secure=True,
            httponly=False,
            samesite="strict",
        )
        _delete_workspace_cookies(error_response)
    if (
        error.http_status == 401
        and getattr(request.state, _CLEAR_WORKSPACE_COOKIES_ON_ERROR_STATE, False) is True
    ):
        _delete_workspace_cookies(error_response)
    return error_response


def _delete_workspace_cookies(response: Response) -> None:
    response.delete_cookie(
        "__Host-metahub-workspace-session",
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        "__Host-metahub-workspace-csrf",
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
