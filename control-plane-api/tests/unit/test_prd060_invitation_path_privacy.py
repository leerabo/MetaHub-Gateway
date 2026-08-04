"""PRD-060 邀请 path、链路 Header 与异常日志隐私专项测试。"""

from __future__ import annotations

import logging

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.logging import JsonFormatter


# 固定 canary 只用于证明完整值和连续 16 字符片段不会进入日志或链路响应。
TOKEN_CANARY = "T" * 11 + "0123456789abcdef" + "U" * 16


def _build_privacy_app():
    """构造只含邀请接受路由的隔离应用，覆盖成功、业务错误与未捕获异常。"""

    app = create_service_app("control-plane-api", "PRD-060 Path Privacy Test")

    @app.post("/invitations/{token}/accept")
    async def accept_probe(
        token: str,
        request: Request,
        mode: str = "success",
    ) -> dict[str, str]:
        """按固定 mode 进入目标日志分支；Token 只用于触发异常隐私验证。"""

        if mode == "business":
            # 固定领域错误进入共享 410 分支，异常不携带 Token 或 URL。
            raise ApiError("invitation.not_available")
        if mode == "unhandled":
            # 故意把 path credential 放进异常文本，证明公开邀请分支不会附加 exc_info。
            raise RuntimeError(token)
        return {
            "request_id": request.state.request_id,
            "trace_id": request.state.trace_id,
        }

    @app.post("/invitations")
    async def create_probe(request: Request) -> dict[str, str]:
        """返回中间件链路 ID，证明邀请邮箱不能借客户端 Header 进入日志。"""

        return {
            "request_id": request.state.request_id,
            "trace_id": request.state.trace_id,
        }

    return app


@pytest.mark.anyio
async def test_invitation_path_and_header_credentials_never_enter_logs_or_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """安全流程：成功、410 和 500 都只记录模板路径并生成内部链路 ID。"""

    app = _build_privacy_app()
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    fragment = TOKEN_CANARY[11:27]
    headers = {
        "X-Request-ID": TOKEN_CANARY,
        "X-Trace-ID": f"trace-{fragment}",
    }
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        success = await client.post(
            f"/invitations/{TOKEN_CANARY}/accept",
            headers=headers,
        )
        business = await client.post(
            f"/invitations/{TOKEN_CANARY}/accept?mode=business",
            headers=headers,
        )
        unhandled = await client.post(
            f"/invitations/{TOKEN_CANARY}/accept?mode=unhandled",
            headers=headers,
        )

    assert success.status_code == 200
    assert business.status_code == 410
    assert unhandled.status_code == 500
    for response in (success, business, unhandled):
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-request-id"].startswith("req_")
        assert response.headers["x-trace-id"].startswith("trc_")
        assert TOKEN_CANARY not in response.text
        assert fragment not in response.text
        assert TOKEN_CANARY not in response.headers["x-request-id"]
        assert fragment not in response.headers["x-trace-id"]

    runtime_records = [
        record
        for record in caplog.records
        if record.name == "metahub.runtime"
        and getattr(record, "event", "") in {"request.completed", "request.failed", "error.diagnosed"}
    ]
    assert runtime_records
    assert {getattr(record, "path", None) for record in runtime_records} == {
        "/invitations/{token}/accept"
    }
    rendered = "\n".join(JsonFormatter().format(record) for record in runtime_records)
    assert TOKEN_CANARY not in rendered
    assert fragment not in rendered
    # 邀请未处理异常不能携带 traceback；否则第三方 handler 可能重新格式化出原始 path。
    assert all(record.exc_info is None for record in runtime_records)


@pytest.mark.anyio
async def test_public_accept_discards_even_safe_looking_client_chain_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """密码碰撞流程：公开 accept 无条件生成内部链路 ID，不回显任意客户端 Header。"""

    app = _build_privacy_app()
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    # 该值只含允许字符，普通路由会原样透传；这里模拟攻击者把正文密码复制进链路 Header。
    password_header_canary = "Prd060PasswordCanary2026"
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        response = await client.post(
            f"/invitations/{TOKEN_CANARY}/accept",
            headers={
                "X-Request-ID": password_header_canary,
                "X-Trace-ID": password_header_canary,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["x-trace-id"].startswith("trc_")
    assert response.headers["x-request-id"] != password_header_canary
    assert response.headers["x-trace-id"] != password_header_canary
    rendered = "\n".join(
        JsonFormatter().format(record)
        for record in caplog.records
        if record.name == "metahub.runtime"
    )
    assert password_header_canary not in response.text
    assert password_header_canary not in rendered


@pytest.mark.anyio
async def test_invitation_create_discards_email_bearing_client_chain_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """邮箱碰撞流程：邀请创建固定生成内部链路 ID，不把邮箱写入响应或访问日志。"""

    app = _build_privacy_app()
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    email_header_canary = "private.prd060.email@example.invalid"
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        response = await client.post(
            "/invitations",
            headers={
                "X-Request-ID": email_header_canary,
                "X-Trace-ID": email_header_canary,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["x-trace-id"].startswith("trc_")
    assert response.json()["request_id"] != email_header_canary
    assert response.json()["trace_id"] != email_header_canary
    rendered = "\n".join(
        JsonFormatter().format(record)
        for record in caplog.records
        if record.name == "metahub.runtime"
    )
    assert email_header_canary not in response.text
    assert email_header_canary not in rendered


@pytest.mark.anyio
async def test_unmatched_path_uses_fixed_log_placeholder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """404 流程：任意未知 path 不得作为日志字段原样保存。"""

    app = _build_privacy_app()
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    unknown_canary = "private-unknown-path-canary"
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        response = await client.get(f"/{unknown_canary}")

    assert response.status_code == 404
    records = [
        record
        for record in caplog.records
        if record.name == "metahub.runtime" and hasattr(record, "path")
    ]
    assert records
    assert {record.path for record in records} == {"<unmatched>"}
    assert unknown_canary not in "\n".join(JsonFormatter().format(record) for record in records)
