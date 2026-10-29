"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_config_summary.py
"""

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.config.routes import router as config_router
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.fastapi_app import create_service_app


def build_test_app():
    """build_test_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Control Plane Test API")
    app.include_router(config_router)
    return app


@pytest.mark.anyio
async def test_config_summary_returns_masked_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """主流程：配置摘要接口返回 request_id，并且敏感 Provider Key 只显示脱敏状态。"""

    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv("METAHUB_PROVIDER_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("METAHUB_PROVIDER_DEEPSEEK_API_KEY", "sk-live-secret")
    app = build_test_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/internal/config/summary",
            headers={"X-Request-ID": "req_config_summary"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_config_summary"
    assert response.headers["X-Trace-ID"] == "trc_config_summary"
    payload = response.json()
    assert payload["request_id"] == "req_config_summary"
    assert payload["environment"] == "dev"
    assert "sk-live-secret" not in response.text
    assert any(
        item["key"] == "METAHUB_PROVIDER_DEEPSEEK_API_KEY"
        and item["status"] == "masked"
        and item["value_preview"] == "***MASKED***"
        for item in payload["items"]
    )


@pytest.mark.anyio
async def test_config_summary_requires_internal_token_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权限验收：prod 环境访问内部配置摘要必须通过后端 token 校验。"""

    monkeypatch.setenv("METAHUB_ENV", "prod")
    internal_token = "expected-control-token-0123456789abcdef"
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", internal_token)
    production_values = {
        "METAHUB_AUTH_TOKEN_SECRET": "config-summary-prod-auth-secret-0123456789abcdef",
        "METAHUB_IDENTITY_SESSION_SECRET": "config-summary-prod-session-secret-0123456789abcdef",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "config-summary-prod-challenge-secret-0123456789abcdef",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "config-summary-prod-pii-secret-0123456789abcdef",
        "METAHUB_WORKSPACE_SESSION_SECRET": "config-summary-prod-workspace-secret-0123456789abcdef",
        "METAHUB_PLATFORM_SESSION_SECRET": "config-summary-prod-platform-secret-0123456789abcdef",
        "METAHUB_SUPPORT_SESSION_SECRET": "config-summary-prod-support-secret-0123456789abcdef",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "config-summary-prod-mfa-secret-0123456789abcdef",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "config-summary-prod-recovery-secret-0123456789abcdef",
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET": "config-summary-prod-cache-secret-0123456789abcdef",
        "METAHUB_DATABASE_URL": "postgresql://app@db.test/metahub?sslmode=require",
        "METAHUB_REDIS_URL": "rediss://cache.test:6379/0",
    }
    for key, value in production_values.items():
        monkeypatch.setenv(key, value)
    app = build_test_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = await client.get(
            "/internal/config/summary",
            headers={"X-Request-ID": "req_missing_token"},
        )
        wrong = await client.get(
            "/internal/config/summary",
            headers={
                "X-Request-ID": "req_wrong_token",
                "X-Internal-Token": "wrong-token",
            },
        )
        ok = await client.get(
            "/internal/config/summary",
            headers={
                "X-Request-ID": "req_right_token",
                "X-Internal-Token": internal_token,
            },
        )

    assert missing.status_code == 401
    assert missing.json()["code"] == "auth.internal_token_required"
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "auth.internal_token_invalid"
    assert ok.status_code == 200
    assert ok.json()["environment"] == "prod"


def test_stream_watchdog_config_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式配置默认值可直接运行，越界阈值必须在服务启动阶段被拒绝。"""

    config = load_app_config("gateway-data-plane")

    assert config.stream_inter_token_timeout_seconds == 30
    assert config.stream_connect_timeout_seconds == 30
    assert config.stream_write_timeout_seconds == 30
    assert config.stream_pool_timeout_seconds == 5
    assert any(
        item.key == "METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS" and item.value_preview == "30"
        for item in config.config_items
    )

    monkeypatch.setenv("METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigError) as invalid:
        load_app_config("gateway-data-plane")
    assert invalid.value.code == "infra.config_invalid"
    assert "METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS" in invalid.value.message
