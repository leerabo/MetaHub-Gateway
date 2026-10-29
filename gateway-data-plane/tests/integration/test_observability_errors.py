"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_observability_errors.py
"""

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_shared.runtime.fastapi_app import create_service_app
from starlette.exceptions import HTTPException as StarletteHTTPException


@pytest.mark.anyio
async def test_unhandled_exception_returns_trace_id_without_stack() -> None:
    """异常流程：未捕获异常必须归一为错误码，并且不能把堆栈泄露给客户端。"""

    app = create_service_app("observability-test", "Observability Test")

    @app.get("/boom")
    async def boom() -> None:
        """boom：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
        raise RuntimeError("provider_key=sk-should-not-leak")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/boom",
            headers={"X-Request-ID": "req_boom", "X-Trace-ID": "trc_boom"},
        )

    payload = response.json()
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req_boom"
    assert response.headers["X-Trace-ID"] == "trc_boom"
    assert payload["code"] == "infra.unhandled"
    assert payload["message"] == "Internal server error."
    assert payload["request_id"] == "req_boom"
    assert payload["trace_id"] == "trc_boom"
    assert payload["suggestion"] == "Use request_id to locate structured logs."
    assert payload["diagnosis"]["rule_id"] == "infra_unhandled"
    assert "RuntimeError" not in response.text
    assert "sk-should-not-leak" not in response.text


@pytest.mark.parametrize(
    ("service_name", "extra_expose_headers"),
    (
        ("control-plane-api", ("ETag", "X-Policy-Candidate-ETag")),
        ("gateway-data-plane", ()),
    ),
)
@pytest.mark.anyio
async def test_feedback_unhandled_exception_keeps_cache_and_cors_security_headers(
    service_name: str,
    extra_expose_headers: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：两个服务的 Feedback 500 都必须禁缓存，并且只允许受信来源读取。"""

    allowed_origin = "http://localhost:5173"
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", allowed_origin)
    app = create_service_app(
        service_name,
        "Feedback Error Header Test",
        extra_cors_expose_headers=extra_expose_headers,
    )

    @app.get("/feedback/boom")
    async def feedback_boom() -> None:
        """模拟认证或数据库基础设施在 Feedback 链路抛出未捕获异常。"""

        # 故障文本故意伪装成 Provider Key，用于证明最外层 500 响应不会回显底层异常正文。
        raise RuntimeError("provider_key=sk-feedback-must-not-leak")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.get(
            "/feedback/boom",
            headers={
                "Origin": allowed_origin,
                "X-Request-ID": "req_feedback_boom",
                "X-Trace-ID": "trc_feedback_boom",
            },
        )
        denied = await client.get(
            "/feedback/boom",
            headers={
                "Origin": "https://attacker.invalid",
                "X-Request-ID": "req_feedback_denied_origin",
            },
        )
        no_origin = await client.get(
            "/feedback/boom",
            headers={"X-Request-ID": "req_feedback_no_origin"},
        )

    assert allowed.status_code == denied.status_code == no_origin.status_code == 500
    assert allowed.json()["code"] == denied.json()["code"] == "infra.unhandled"
    assert no_origin.json()["code"] == "infra.unhandled"
    for response in (allowed, denied, no_origin):
        assert response.headers["cache-control"] == "no-store, private"
    assert allowed.headers["x-request-id"] == "req_feedback_boom"
    assert allowed.headers["x-trace-id"] == "trc_feedback_boom"
    assert allowed.headers["access-control-allow-origin"] == allowed_origin
    assert "Origin" in allowed.headers["vary"]
    exposed = {
        item.strip().lower()
        for item in allowed.headers["access-control-expose-headers"].split(",")
    }
    assert {"x-request-id", "x-trace-id"} <= exposed
    assert {item.lower() for item in extra_expose_headers} <= exposed
    # 未列入配置的来源不能被错误处理器反射，否则 500 正文可被任意站点跨域读取。
    assert "access-control-allow-origin" not in denied.headers
    assert denied.headers["access-control-expose-headers"] == allowed.headers[
        "access-control-expose-headers"
    ]
    assert "access-control-allow-origin" not in no_origin.headers
    assert "access-control-expose-headers" not in no_origin.headers
    assert "sk-feedback-must-not-leak" not in allowed.text


@pytest.mark.anyio
async def test_feedback_unhandled_cors_wildcard_matches_runtime_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容流程：关闭 credentials 时通配 Origin 即使带 Cookie 也保持星号且不加 Vary。"""

    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", "*")
    app = create_service_app("observability-test", "Feedback Wildcard CORS Test")

    @app.get("/feedback/boom")
    async def feedback_boom() -> None:
        """模拟通配 CORS 配置下的 Feedback 未捕获异常。"""

        # 固定异常只用于进入 ServerErrorMiddleware，不携带请求、身份或租户动态数据。
        raise RuntimeError("feedback wildcard failure")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/feedback/boom",
            headers={
                "Origin": "https://console.example",
                "Cookie": "session=opaque",
                "X-Request-ID": "req_feedback_wildcard",
            },
        )

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["access-control-allow-origin"] == "*"
    assert "vary" not in response.headers
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


@pytest.mark.anyio
async def test_error_codes_endpoint_returns_diagnosis_metadata() -> None:
    """主流程：共享服务工厂暴露只读错误码目录，且每个典型错误带诊断元数据。"""

    app = create_service_app("observability-test", "Observability Test")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/error-codes",
            headers={"X-Request-ID": "req_error_codes", "X-Trace-ID": "trc_error_codes"},
        )

    payload = response.json()
    codes = {item["code"]: item for item in payload["error_codes"]}

    assert response.status_code == 200
    assert payload["request_id"] == "req_error_codes"
    assert payload["trace_id"] == "trc_error_codes"
    assert codes["gateway.upstream_rate_limited"]["diagnosis"]["rule_id"] == "upstream_rate_limited"
    assert codes["gateway.upstream_auth_failed"]["diagnosis"]["category"] == "provider_auth"
    assert "tenant_id" not in payload


@pytest.mark.anyio
async def test_method_not_allowed_is_unified_and_only_copies_allow_header() -> None:
    """异常/安全流程：405 使用统一错误体，且异常头只允许复制标准 Allow。"""

    app = create_service_app("observability-test", "Method Contract Test")

    @app.post("/method-only")
    async def method_only() -> dict[str, bool]:
        """提供只允许 POST 的探针路由，业务正文不读取租户或敏感输入。"""

        return {"ok": True}

    @app.get("/explicit-method-error")
    async def explicit_method_error() -> None:
        """模拟业务代码携带额外内部头抛出 405，验证共享处理器不会透传。"""

        raise StarletteHTTPException(
            status_code=405,
            headers={"allow": "PATCH", "X-Internal-Debug": "must-not-leak"},
        )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        routed = await client.get(
            "/method-only",
            headers={"X-Request-ID": "req_method_405", "X-Trace-ID": "trc_method_405"},
        )
        explicit = await client.get(
            "/explicit-method-error",
            headers={"X-Request-ID": "req_explicit_405", "X-Trace-ID": "trc_explicit_405"},
        )

    payload = routed.json()
    assert routed.status_code == explicit.status_code == 405
    assert routed.headers["allow"] == "POST"
    assert explicit.headers["allow"] == "PATCH"
    assert "x-internal-debug" not in explicit.headers
    assert payload["code"] == "infra.method_not_allowed"
    assert payload["message"] == "Request method is not allowed."
    assert payload["suggestion"] == "Use a method listed in the Allow response header."
    assert payload["request_id"] == routed.headers["x-request-id"] == "req_method_405"
    assert payload["trace_id"] == routed.headers["x-trace-id"] == "trc_method_405"
    assert payload["diagnosis"]["rule_id"] == "infra_method_not_allowed"
    assert "detail" not in payload
    assert "must-not-leak" not in explicit.text
