"""PRD-070 公开 Header 与企业 Chat grant 身份的安全边界测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.session_risk.identity import (
    reject_public_session_headers_on_internal_route,
    resolve_enterprise_chat_session_risk_candidates,
    resolve_public_session_risk_candidates,
)
from metahub_shared.runtime.errors import ApiError


def _config(*, enabled: bool = True, previous: bool = False) -> SimpleNamespace:
    """构造只含专用 keyring 的测试配置，secret 不进入断言输出。"""

    return SimpleNamespace(
        session_risk_enabled=enabled,
        session_risk_hmac_version=2,
        session_risk_hmac_secret="c" * 32,
        session_risk_previous_hmac_version=1 if previous else None,
        session_risk_previous_hmac_secret="d" * 32 if previous else None,
    )


def _auth(*, tenant_id: str = "tenant_a", key_id: str = "vk_a") -> GatewayAuthContext:
    """构造认证后可信 tenant/Virtual Key 上下文，禁止从测试请求正文取 owner。"""

    # tenant 与 Key 都模拟服务端鉴权结果，测试调用方不能用 Header 覆盖这两个归属字段。
    return GatewayAuthContext(
        tenant_id=tenant_id,
        virtual_key_id=key_id,
        key_prefix="mh_test",
        environment="dev",
        environment_id="env_a",
        environment_name="开发",
        environment_status="active",
        project_id="project_a",
        application_id=None,
    )


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    """从原始 ASGI Header 构造请求，保留重复行而不经过字典合并。"""

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": headers,
        }
    )


def test_public_headers_are_pairwise_unique_and_canonical() -> None:
    """只出现一侧、重复行、逗号合并和大小写漂移全部返回固定 400 错误码。"""

    session = b"mhs_" + b"a" * 32
    turn = b"mht_" + b"b" * 32
    invalid_requests = (
        _request([(b"x-metahub-session-id", session)]),
        _request(
            [
                (b"x-metahub-session-id", session),
                (b"x-metahub-session-id", session),
                (b"x-metahub-turn-id", turn),
            ]
        ),
        _request(
            [
                (b"x-metahub-session-id", session + b"," + session),
                (b"x-metahub-turn-id", turn),
            ]
        ),
        _request(
            [
                (b"x-metahub-session-id", b"MHS_" + b"A" * 32),
                (b"x-metahub-turn-id", turn),
            ]
        ),
    )
    for request in invalid_requests:
        with pytest.raises(ApiError) as error:
            resolve_public_session_risk_candidates(request, _config(), _auth())  # type: ignore[arg-type]
        assert error.value.code == "gateway.session_risk_identifier_invalid"


def test_disabled_mode_does_not_parse_new_headers() -> None:
    """升级默认关闭时即使客户端偶然使用同名 Header，也保持旧调用合同。"""

    request = _request([(b"x-metahub-session-id", b"not-canonical")])
    assert resolve_public_session_risk_candidates(  # type: ignore[arg-type]
        request,
        _config(enabled=False),
        _auth(),
    ) == ()


def test_public_identity_uses_keyring_and_does_not_retain_raw_headers() -> None:
    """current/previous 产生不同摘要，repr 中不出现原始 Header 或专用 secret。"""

    raw_session = "mhs_" + "a" * 32
    raw_turn = "mht_" + "b" * 32
    request = _request(
        [
            (b"x-metahub-session-id", raw_session.encode("ascii")),
            (b"x-metahub-turn-id", raw_turn.encode("ascii")),
        ]
    )
    candidates = resolve_public_session_risk_candidates(  # type: ignore[arg-type]
        request,
        _config(previous=True),
        _auth(),
    )

    assert [candidate.scope.hmac_key_version for candidate in candidates] == [2, 1]
    assert candidates[0].scope.scope_key != candidates[1].scope.scope_key
    assert candidates[0].turn_key_digest != candidates[1].turn_key_digest
    rendered = repr(candidates)
    assert raw_session not in rendered
    assert raw_turn not in rendered
    assert "c" * 32 not in rendered
    assert "d" * 32 not in rendered


def test_same_headers_are_tenant_and_virtual_key_scoped() -> None:
    """相同 Header 在不同 tenant 或 Key 下派生不同 owner，不能共享 Redis/数据库状态。"""

    headers = [
        (b"x-metahub-session-id", ("mhs_" + "a" * 32).encode("ascii")),
        (b"x-metahub-turn-id", ("mht_" + "b" * 32).encode("ascii")),
    ]
    first = resolve_public_session_risk_candidates(  # type: ignore[arg-type]
        _request(headers), _config(), _auth()
    )[0]
    other_tenant = resolve_public_session_risk_candidates(  # type: ignore[arg-type]
        _request(headers), _config(), _auth(tenant_id="tenant_b")
    )[0]
    other_key = resolve_public_session_risk_candidates(  # type: ignore[arg-type]
        _request(headers), _config(), _auth(key_id="vk_b")
    )[0]

    assert len(
        {
            first.scope.owner_slot,
            other_tenant.scope.owner_slot,
            other_key.scope.owner_slot,
        }
    ) == 3


def test_enterprise_chat_identity_comes_only_from_grant_ids() -> None:
    """企业 Chat 保留服务端 session/message 身份，同时只把 HMAC owner/turn 放入 Redis。"""

    candidates = resolve_enterprise_chat_session_risk_candidates(  # type: ignore[arg-type]
        _config(previous=True),
        _auth(),
        chat_session_id=f"chs_{'a' * 32}",
        chat_message_id=f"chm_{'b' * 32}",
    )
    assert [candidate.scope.hmac_key_version for candidate in candidates] == [2, 1]
    assert all(candidate.scope.scope_key == f"chs_{'a' * 32}" for candidate in candidates)
    assert all(candidate.chat_message_id == f"chm_{'b' * 32}" for candidate in candidates)
    assert candidates[0].scope.owner_slot != candidates[1].scope.owner_slot


def test_internal_route_rejects_public_identity_headers() -> None:
    """内部服务 Token 与公开会话 Header 不能并存，避免客户端伪造第二 owner。"""

    request = _request([(b"x-metahub-turn-id", (b"mht_" + b"a" * 32))])
    with pytest.raises(ApiError) as error:
        reject_public_session_headers_on_internal_route(request)
    assert error.value.code == "chat.execution_invalid"
