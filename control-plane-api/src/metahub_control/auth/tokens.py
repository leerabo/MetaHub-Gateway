"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/auth/tokens.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from metahub_shared.runtime.errors import ApiError

# JWT issuer/audience 固定为 MetaHub 控制面，避免不同服务令牌被误互信。
ISSUER = "metahub-control-plane"
AUDIENCE = "metahub-console"


@dataclass(frozen=True)
class TokenClaims:
    """承载 控制面认证与会话 的令牌声明字段，签发和验签必须保持同一套字段口径。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    user_id: str
    tenant_id: str
    session_id: str
    role: str
    jti: str
    expires_at: datetime


def encode_access_token(claims: TokenClaims, secret: str, *, issued_at: datetime) -> str:
    """用标准库生成 HS256 JWT，避免为了 M0 登录引入额外运行时依赖。"""

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": claims.user_id,
        "tid": claims.tenant_id,
        "sid": claims.session_id,
        "role": claims.role,
        "jti": claims.jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(claims.expires_at.timestamp()),
    }
    signing_input = ".".join(
        [
            _b64url_json(header),
            _b64url_json(payload),
        ]
    )
    signature = _sign(signing_input, secret)
    return f"{signing_input}.{signature}"


def decode_access_token(token: str, secret: str, *, now: datetime | None = None) -> TokenClaims:
    """校验 JWT 签名和核心声明。

    这里先只验证控制台访问 Token 所需的最小声明，Session 是否仍有效由数据库二次确认。
    """

    parts = token.split(".")
    if len(parts) != 3:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_invalid")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = _sign(signing_input, secret)
    if not hmac.compare_digest(expected, parts[2]):
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_invalid")

    try:
        header = _b64url_decode_json(parts[0])
        payload = _b64url_decode_json(parts[1])
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_invalid") from exc

    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        raise ApiError("auth.token_invalid")
    if payload.get("iss") != ISSUER or payload.get("aud") != AUDIENCE:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_invalid")

    required = ["sub", "tid", "sid", "role", "jti", "exp"]
    if any(not payload.get(item) for item in required):
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_invalid")

    expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
    current = now or datetime.now(UTC)
    if expires_at <= current:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.token_expired")

    return TokenClaims(
        user_id=str(payload["sub"]),
        tenant_id=str(payload["tid"]),
        session_id=str(payload["sid"]),
        role=str(payload["role"]),
        jti=str(payload["jti"]),
        expires_at=expires_at,
    )


def _b64url_json(value: dict[str, Any]) -> str:
    """在 控制面认证与会话 内部复用 b64url json 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return _b64url_encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64url_decode_json(value: str) -> dict[str, Any]:
    """在 控制面认证与会话 内部复用 b64url decode json 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    decoded = _b64url_decode(value)
    result = json.loads(decoded.decode("utf-8"))
    if not isinstance(result, dict):
        # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
        raise ValueError("JWT part is not a JSON object.")
    return result


def _sign(signing_input: str, secret: str) -> str:
    """在 控制面认证与会话 内部复用 sign 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _b64url_encode(value: bytes) -> str:
    """在 控制面认证与会话 内部复用 b64url encode 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """在 控制面认证与会话 内部复用 b64url decode 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
