"""验证 Response Cache Credential scope 不触发 Key 解密或选择副作用。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from metahub_gateway.providers.keypool import snapshot_response_cache_credential_scope
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.response_cache import VersionedResponseCacheSecret


def _route(*, auth_type: str = "bearer") -> GatewayRoute:
    """构造固定 tenant/Provider 路由，测试不保存任何可用凭据正文。"""

    return GatewayRoute(
        tenant_id="tenant-prd076-keypool",
        model_id="model-prd076-keypool",
        model_alias="prd076-keypool-alias",
        upstream_model_name="synthetic-upstream",
        capabilities={"chat": True},
        provider_id="provider-prd076-keypool",
        provider_code="synthetic_provider",
        provider_type="openai_compatible",
        provider_auth_type=auth_type,
        provider_name="PRD076 synthetic provider",
        provider_base_url="http://127.0.0.1:1/v1",
        model_alias_id="alias-prd076-keypool",
        public_model_alias="prd076-keypool-alias",
    )


def _connection() -> sqlite3.Connection:
    """创建 snapshot 查询所需的最小内存表，并启用按列读取。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE provider_credentials(
          provider_credential_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          provider_id TEXT NOT NULL,
          key_fingerprint TEXT NOT NULL,
          encrypted_key TEXT NOT NULL,
          status TEXT NOT NULL,
          rate_limited_until TEXT,
          probe_health_status TEXT,
          probe_last_tested_at TEXT,
          last_used_at TEXT
        )
        """
    )
    return connection


def _insert(
    connection: sqlite3.Connection,
    credential_id: str,
    fingerprint: str,
    *,
    status: str = "active",
) -> None:
    """插入不可解密的合成托管行；snapshot 只应读取前缀和安全元数据。"""

    connection.execute(
        """
        INSERT INTO provider_credentials(
          provider_credential_id,tenant_id,provider_id,key_fingerprint,encrypted_key,status,
          rate_limited_until,probe_health_status,probe_last_tested_at,last_used_at
        ) VALUES(?,?,?,?,?,?,NULL,'healthy',?,NULL)
        """,
        (
            credential_id,
            "tenant-prd076-keypool",
            "provider-prd076-keypool",
            fingerprint,
            "aesgcm-v1:not-decryptable-by-design",
            status,
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        ),
    )
    connection.commit()


def _config() -> SimpleNamespace:
    """返回 snapshot 只读健康边界所需的最小配置。"""

    return SimpleNamespace(provider_health_stale_seconds=300)


def _hmac_key() -> VersionedResponseCacheSecret:
    """返回仅用于单测的独立合成 HMAC 根密钥。"""

    # 该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。
    return VersionedResponseCacheSecret(version=1, secret="r" * 48)


def test_none_and_single_managed_credential_have_stable_distinct_scopes() -> None:
    """无认证与唯一托管 Credential 都可证明唯一响应域，且不会写选择时间。"""

    connection = _connection()
    none_scope = snapshot_response_cache_credential_scope(
        _config(),
        connection,
        _route(auth_type="none"),
        hmac_key=_hmac_key(),
        as_of=datetime.now(UTC),
    )
    _insert(connection, "credential-prd076-a", "a" * 64)
    managed_scope = snapshot_response_cache_credential_scope(
        _config(),
        connection,
        _route(),
        hmac_key=_hmac_key(),
        as_of=datetime.now(UTC),
    )

    assert none_scope is not None and none_scope.kind == "none"
    assert managed_scope is not None and managed_scope.kind == "managed"
    assert none_scope.digest != managed_scope.digest
    assert connection.execute(
        "SELECT last_used_at FROM provider_credentials WHERE provider_credential_id=?",
        ("credential-prd076-a",),
    ).fetchone()[0] is None


def test_zero_or_multiple_selectable_managed_credentials_bypass_cache() -> None:
    """零成员或多成员 KeyPool 无法证明唯一响应域，必须返回 None。"""

    connection = _connection()
    _insert(connection, "credential-prd076-disabled", "b" * 64, status="disabled")
    assert snapshot_response_cache_credential_scope(
        _config(), connection, _route(), hmac_key=_hmac_key(), as_of=datetime.now(UTC)
    ) is None

    _insert(connection, "credential-prd076-a", "c" * 64)
    _insert(connection, "credential-prd076-b", "d" * 64)
    assert snapshot_response_cache_credential_scope(
        _config(), connection, _route(), hmac_key=_hmac_key(), as_of=datetime.now(UTC)
    ) is None
