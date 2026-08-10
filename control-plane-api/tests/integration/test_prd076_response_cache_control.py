"""PRD-076 Response Cache Control API 的权限、租户、CAS 与幂等集成测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.response_cache import outbox as response_cache_outbox
from metahub_control.response_cache.routes import router as response_cache_router
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.response_cache import CacheTag, build_tag_digests
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。
ADMIN_PASSWORD = "MetaHubLocal!2026"
VIEWER_PASSWORD = "Prd076ViewerOnly!2026"
FINANCE_PASSWORD = "Prd076FinanceOnly!2026"
AUTH_SECRET = "prd076-auth-control-" + "a" * 48
CACHE_HMAC_SECRET = "prd076-cache-hmac-control-" + "b" * 48
CACHE_ENCRYPTION_SECRET = "prd076-cache-encryption-control-" + "c" * 48
CONTROL_SECRET = "prd076-control-idempotency-" + "d" * 48
SPOOL_SECRET = "prd076-failure-spool-control-" + "e" * 48
NOW = "2026-08-10T00:00:00Z"


def _url(path: Path) -> str:
    """把仓库内隔离 SQLite 路径转换为共享数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def prd076_control_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一次构建完整 0069 模板，后续测试复制模板避免重复执行历史迁移。"""

    database = tmp_path_factory.mktemp("prd076-control-template") / "template.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0069"
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        # 两个合成用户验证“认证先于正文”和 finance_admin 的动态撤权。
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?, 'active',?,?)
            """,
            (
                (
                    "user_prd076_viewer",
                    seed.tenant_id,
                    "viewer@prd076.local",
                    "PRD076 Viewer",
                    hash_password(VIEWER_PASSWORD),
                    "viewer",
                    NOW,
                    NOW,
                ),
                (
                    "user_prd076_finance",
                    seed.tenant_id,
                    "finance@prd076.local",
                    "PRD076 Finance",
                    hash_password(FINANCE_PASSWORD),
                    "finance_admin",
                    NOW,
                    NOW,
                ),
            ),
        )
        _insert_default_alias(connection, seed.tenant_id)
        _insert_foreign_alias(connection)
        connection.commit()
    finally:
        connection.close()
    return database


def _insert_default_alias(connection: sqlite3.Connection, tenant_id: str) -> None:
    """使用种子环境和模型创建当前租户可配置的 active Alias。"""

    owner = connection.execute(
        """
        SELECT environment.environment_id,environment.code,model.model_id,user.user_id
        FROM environments environment
        JOIN models model ON model.tenant_id=environment.tenant_id
        JOIN users user ON user.tenant_id=environment.tenant_id
        WHERE environment.tenant_id=? AND environment.code='dev'
          AND environment.status='active' AND model.status='active'
          AND user.role='admin' AND user.status='active'
        ORDER BY model.model_id,user.user_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert owner is not None
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
          status,created_by,created_at,updated_at
        ) VALUES('alias_prd076_default',?,'prd076-default-alias',?,?,?,
                 'active',?,?,?)
        """,
        (
            tenant_id,
            str(owner[1]),
            str(owner[0]),
            str(owner[2]),
            str(owner[3]),
            NOW,
            NOW,
        ),
    )


def _insert_foreign_alias(connection: sqlite3.Connection) -> None:
    """建立第二租户完整 Alias owner，证明跨租户 ID 与不存在 ID 返回同形 404。"""

    tenant_id = "tenant_prd076_foreign"
    user_id = "user_prd076_foreign"
    provider_id = "provider_prd076_foreign"
    model_id = "model_prd076_foreign"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?, 'prd076-foreign', 'PRD076 foreign', 'internal_only', 'active', ?, ?)
        """,
        (tenant_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,'admin@prd076-foreign.local','PRD076 foreign admin',?,
                 'admin','active',?,?)
        """,
        (user_id, tenant_id, hash_password("Prd076ForeignOnly!2026"), NOW, NOW),
    )
    environment_row = connection.execute(
        """
        SELECT environment_id FROM environments
        WHERE tenant_id=? AND code='dev' AND status='active'
        """,
        (tenant_id,),
    ).fetchone()
    assert environment_row is not None
    environment_id = str(environment_row[0])
    connection.execute(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,provider_type,
          auth_type,created_by,created_at,updated_at
        ) VALUES(?,?,'prd076_foreign','PRD076 foreign provider',
                 'https://example.invalid/v1','active','openai_compatible','none',?,?,?)
        """,
        (provider_id, tenant_id, user_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,context_window,created_by,created_at,updated_at
        ) VALUES(?,?,?,'prd076-foreign-model','prd076-foreign-upstream',
                 '{"chat":true,"stream":true}','active',8192,?,?,?)
        """,
        (model_id, tenant_id, provider_id, user_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
          status,created_by,created_at,updated_at
        ) VALUES('alias_prd076_foreign',?,'prd076-foreign-alias','dev',?,?,
                 'active',?,?,?)
        """,
        (tenant_id, environment_id, model_id, user_id, NOW, NOW),
    )


def _clone_environment(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str]:
    """复制模板并把全部运行目录、Secret 和数据库限定在当前测试 owner。"""

    database = tmp_path / "prd076-control.sqlite3"
    shutil.copy2(template, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_ENABLED", "true")
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_HMAC_SECRET", CACHE_HMAC_SECRET)
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET",
        CACHE_ENCRYPTION_SECRET,
    )
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET",
        CONTROL_SECRET,
    )
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET", SPOOL_SECRET)
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT",
        str(tmp_path / "failure-spool"),
    )
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT model_alias_id FROM model_aliases
            WHERE tenant_id=(SELECT tenant_id FROM tenants WHERE slug='default')
              AND status='active' ORDER BY model_alias_id LIMIT 1
            """
        ).fetchone()
        assert row is not None
        alias_id = str(row[0])
    finally:
        connection.close()
    return database_url, alias_id


def _build_app():
    """构造只包含认证和 PRD-076 路由的真实错误中间件测试应用。"""

    app = create_service_app("control-plane-api", "PRD-076 Control Test")

    @app.middleware("http")
    async def private_cache_boundary(request: Request, call_next):
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 private_cache_boundary 对应步骤。"""
        response = await call_next(request)
        if request.url.path in {"/cache-policies", "/cache/invalidate"}:
            response.headers["Cache-Control"] = "no-store, private"
        return response

    app.include_router(auth_router)
    app.include_router(response_cache_router)
    return app


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录合成用户，Token 只停留在当前测试调用栈。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200
    # 该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str, idempotency_key: str | None = None):
    """构造单值安全 Header；断言失败时不输出 Token。"""

    values = {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
    if idempotency_key is not None:
        values["Idempotency-Key"] = idempotency_key
    return values


def _payload(alias_id: str, *, expected_version: int, ttl_seconds: int = 900):
    """返回启用策略的完整 CAS 快照，全部 machine code 均为公开合成值。"""

    return {
        "model_alias_id": alias_id,
        "status": "enabled",
        "data_class": "fixture-public",
        "single_data_class_confirmed": True,
        "ttl_seconds": ttl_seconds,
        "charge_cache_hits": False,
        "tags": ["fixture-tag"],
        "expected_version": expected_version,
    }


def _insert_page_aliases(connection: sqlite3.Connection) -> None:
    """复制同租户 Alias owner，稳定产生三页而不引入额外 Provider 或 Model。"""

    owner = connection.execute(
        """
        SELECT tenant_id,environment,environment_id,default_model_id,created_by
        FROM model_aliases WHERE model_alias_id='alias_prd076_default'
        """
    ).fetchone()
    assert owner is not None
    connection.executemany(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
          status,created_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?,?)
        """,
        [
            (
                f"alias_prd076_page_{suffix}",
                owner["tenant_id"],
                f"prd076-page-{suffix}",
                owner["environment"],
                owner["environment_id"],
                owner["default_model_id"],
                owner["created_by"],
                NOW,
                NOW,
            )
            for suffix in ("a", "b")
        ],
    )


def _insert_cache_entry(
    connection: sqlite3.Connection,
    *,
    config: object,
    alias_id: str,
    cache_policy_id: str,
    policy_version: int,
    policy_generation: int,
    ordinal: int,
    tags: tuple[str, ...],
) -> str:
    """建立受 Request/Usage/FinOps owner 约束的真实密文条目，只暴露合成计数。"""

    owner = connection.execute(
        """
        SELECT alias.tenant_id,alias.default_model_id AS model_id,model.provider_id,
               provider.provider_code,alias.environment_id,environment.code,
               key.virtual_key_id
        FROM model_aliases alias
        JOIN models model
          ON model.tenant_id=alias.tenant_id AND model.model_id=alias.default_model_id
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        JOIN environments environment
          ON environment.tenant_id=alias.tenant_id
         AND environment.environment_id=alias.environment_id
        JOIN virtual_keys key ON key.tenant_id=alias.tenant_id
        WHERE alias.model_alias_id=?
        ORDER BY key.virtual_key_id LIMIT 1
        """,
        (alias_id,),
    ).fetchone()
    assert owner is not None
    tenant_id = str(owner["tenant_id"])
    request_id = f"req_prd076_entry_{ordinal}"
    trace_id = f"trace_prd076_entry_{ordinal}"
    usage_id = f"usg_prd076_entry_{ordinal}"
    price_snapshot_id = f"prs_prd076_entry_{ordinal}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, owner["virtual_key_id"], NOW),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
          provider_code,status,created_at,updated_at,environment_id,environment_code
        ) VALUES(?,?,?,?,?,?,?,'success',?,?,?,?)
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            owner["virtual_key_id"],
            owner["provider_id"],
            owner["model_id"],
            owner["provider_code"],
            NOW,
            NOW,
            owner["environment_id"],
            owner["code"],
        ),
    )
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id,tenant_id,request_id,model_id,provider_id,provider_code,
          currency,input_price_per_1k,output_price_per_1k,price_schema_version,
          billing_dimensions,provider_raw_mapping,source,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'CNY','0.001000','0.002000','price-v1',
                 '{}','{}','model_catalog','active',?,?)
        """,
        (
            price_snapshot_id,
            tenant_id,
            request_id,
            owner["model_id"],
            owner["provider_id"],
            owner["provider_code"],
            NOW,
            NOW,
        ),
    )
    normalized_usage = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_tokens": 0,
        "cost_amount": "0.000005",
        "cost_cny": "0.000005",
        "cost_usd": "0",
        "currency": "CNY",
        "price_schema_version": "price-v1",
        "price_snapshot_id": price_snapshot_id,
        "posting_source": "direct",
    }
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
          cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,
          currency,price_schema_version,estimated,usage_source,price_snapshot_id,
          model_id,provider_id,virtual_key_id,environment_id,environment_code,
          provider_code,created_at,updated_at
        ) VALUES(?,?,?,3,2,5,0,'{}',?,'0.000005','0.000005','0','CNY',
                 'price-v1',0,'upstream',?,?,?,?,?,?,?,?,?)
        """,
        (
            usage_id,
            tenant_id,
            request_id,
            json.dumps(normalized_usage, ensure_ascii=True, separators=(",", ":")),
            price_snapshot_id,
            owner["model_id"],
            owner["provider_id"],
            owner["virtual_key_id"],
            owner["environment_id"],
            owner["code"],
            owner["provider_code"],
            NOW,
            NOW,
        ),
    )
    charge = connection.execute(
        """
        SELECT finops_ledger_entry_id,normalized_cny_micros
        FROM finops_ledger_entries WHERE tenant_id=? AND usage_ledger_id=?
        """,
        (tenant_id, usage_id),
    ).fetchone()
    assert charge is not None and int(charge["normalized_cny_micros"]) == 5

    state = connection.execute(
        """
        SELECT key_schema_version,active_hmac_key_version,
               active_encryption_key_version
        FROM response_cache_key_state WHERE key_state_id=1
        """
    ).fetchone()
    assert state is not None
    identity_digest = f"{ordinal:x}" * 64
    key_digest = f"{ordinal + 2:x}" * 64
    entry_id = f"rce_{ordinal:032x}"
    connection.execute(
        """
        INSERT INTO response_cache_key_generations(
          tenant_id,hmac_key_version,cache_identity_digest,generation,updated_at
        ) VALUES(?,?,?,1,?)
        """,
        (tenant_id, state["active_hmac_key_version"], identity_digest, NOW),
    )
    connection.execute(
        """
        INSERT INTO response_cache_entries(
          response_cache_entry_id,tenant_id,cache_policy_id,cache_policy_version,
          policy_invalidation_generation,key_schema_version,hmac_key_version,
          encryption_key_version,cache_identity_digest,key_generation,cache_key_digest,
          model_alias_id,model_id,provider_id,route_revision_digest,
          credential_revision_digest,nonce,ciphertext,plaintext_byte_count,input_tokens,
          output_tokens,total_tokens,source_request_id,source_trace_id,
          source_usage_ledger_id,source_finops_ledger_entry_id,
          source_generation_cost_status,source_generation_cost_cny_micros,
          expires_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,1,3,2,5,?,?,?,?,
                 'known',5,'2099-01-01T00:00:00Z',?)
        """,
        (
            entry_id,
            tenant_id,
            cache_policy_id,
            policy_version,
            policy_generation,
            state["key_schema_version"],
            state["active_hmac_key_version"],
            state["active_encryption_key_version"],
            identity_digest,
            key_digest,
            alias_id,
            owner["model_id"],
            owner["provider_id"],
            f"{ordinal + 4:x}" * 64,
            f"{ordinal + 6:x}" * 64,
            bytes([ordinal]) * 12,
            bytes([ordinal]) * 16,
            request_id,
            trace_id,
            usage_id,
            charge["finops_ledger_entry_id"],
            NOW,
        ),
    )
    hmac_key = getattr(config, "response_cache_hmac_key", None)
    assert hmac_key is not None
    digests = build_tag_digests(
        (hmac_key,),
        tenant_id=tenant_id,
        selectors=tuple(CacheTag(kind="tag", value=value) for value in tags),
    )
    for digest in digests:
        connection.execute(
            """
            INSERT OR IGNORE INTO response_cache_tag_generations(
              tenant_id,hmac_key_version,tag_kind,tag_digest,generation,updated_at
            ) VALUES(?,?,?,?,1,?)
            """,
            (tenant_id, digest.hmac_key_version, digest.kind, digest.digest, NOW),
        )
        connection.execute(
            """
            INSERT INTO response_cache_entry_tags(
              tenant_id,hmac_key_version,response_cache_entry_id,
              tag_kind,tag_digest,tag_generation
            ) VALUES(?,?,?,?,?,1)
            """,
            (tenant_id, digest.hmac_key_version, entry_id, digest.kind, digest.digest),
        )
    return entry_id


def _insert_cache_webhook_endpoint(
    connection: sqlite3.Connection,
    *,
    endpoint_id: str,
) -> None:
    """建立仅订阅 cache.invalidated 的合成 Endpoint，Secret 不离开测试数据库。"""

    owner = connection.execute(
        """
        SELECT tenant_id,user_id FROM users
        WHERE email='admin@metahub.local' AND status='active'
        """
    ).fetchone()
    assert owner is not None
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,'PRD076 cache sink','https://example.invalid/prd076-cache',
                 '["cache.invalidated"]','synthetic-ciphertext','whsec_test',
                 'prd076-synthetic-fingerprint','active',5000,3,?,?,?,?)
        """,
        (
            endpoint_id,
            owner["tenant_id"],
            owner["user_id"],
            owner["user_id"],
            NOW,
            NOW,
        ),
    )


def _assert_private_response(response, expected_code: str | None = None) -> None:
    """统一验证 no-store 和 request/trace 镜像，不打印认证或输入正文。"""

    body = response.json()
    assert response.headers["cache-control"] == "no-store, private"
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["trace_id"] == response.headers["x-trace-id"]
    if expected_code is not None:
        assert body["code"] == expected_code


def test_policy_cas_replay_conflict_and_invalidation_source(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖 create/no-op/update、跨 attempt replay、intent 冲突和失效 outbox。

    该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 test_policy_cas_replay_conflict_and_invalidation_source 对应步骤。"""

    database_url, alias_id = _clone_environment(
        prd076_control_template,
        monkeypatch,
        tmp_path,
    )
    app = _build_app()

    async def scenario() -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 scenario 对应步骤。"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            listed = await client.get(
                "/cache-policies?limit=50",
                headers=_headers(token, "req_prd076_list"),
            )
            assert listed.status_code == 200
            _assert_private_response(listed)
            assert listed.json()["tenant_id"] == listed.json()["items"][0]["tenant_id"]

            intent_key = "prd076-policy-intent-0001"
            created = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=0),
                headers=_headers(token, "req_prd076_create", intent_key),
            )
            assert created.status_code == 200, created.json().get("code")
            _assert_private_response(created)
            created_body = created.json()
            assert created_body["changed"] is True
            assert created_body["idempotent_replay"] is False
            assert created_body["cache_policy"]["version"] == 1

            replayed = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=0),
                headers=_headers(token, "req_prd076_replay", intent_key),
            )
            assert replayed.status_code == 200
            _assert_private_response(replayed)
            assert replayed.json()["idempotent_replay"] is True
            assert replayed.json()["cache_policy"] == created_body["cache_policy"]

            conflict = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=0, ttl_seconds=901),
                headers=_headers(token, "req_prd076_conflict", intent_key),
            )
            assert conflict.status_code == 409
            _assert_private_response(conflict, "response_cache.request_conflict")

            no_op = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=1),
                headers=_headers(
                    token,
                    "req_prd076_noop",
                    "prd076-policy-intent-0002",
                ),
            )
            assert no_op.status_code == 200
            assert no_op.json()["changed"] is False
            assert no_op.json()["cache_policy"]["version"] == 1

            updated = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=1, ttl_seconds=901),
                headers=_headers(
                    token,
                    "req_prd076_update",
                    "prd076-policy-intent-0003",
                ),
            )
            assert updated.status_code == 200
            assert updated.json()["changed"] is True
            assert updated.json()["cache_policy"]["version"] == 2

            invalidated = await client.post(
                "/cache/invalidate",
                json={
                    "scope": "tags",
                    "match_mode": "all",
                    "tags": ["tag:fixture-tag"],
                    "reason_code": "manual_finops",
                },
                headers=_headers(
                    token,
                    "req_prd076_invalidate",
                    "prd076-invalidation-0001",
                ),
            )
            assert invalidated.status_code == 200
            _assert_private_response(invalidated)
            assert invalidated.json()["invalidation"]["tag_count"] == 1

    asyncio.run(scenario())
    connection = connect_database(database_url)
    try:
        state = connection.execute(
            """
            SELECT hmac_secret_fingerprint,encryption_secret_fingerprint
            FROM response_cache_key_state
            """
        ).fetchone()
        assert state is not None and tuple(state) != ("0" * 64, "0" * 64)
        assert (
            connection.execute("SELECT COUNT(*) FROM response_cache_policy_events").fetchone()[0]
            == 3
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM response_cache_invalidations").fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM response_cache_event_outbox").fetchone()[0]
            == 2
        )
    finally:
        connection.close()


def test_authentication_and_permission_precede_body_parsing(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """匿名和无权请求在读取非法正文前分别固定为 401 与 403。"""

    _, _ = _clone_environment(prd076_control_template, monkeypatch, tmp_path)
    app = _build_app()

    async def scenario() -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 scenario 对应步骤。"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.patch(
                "/cache-policies",
                content=b"{not-json",
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": "prd076-anonymous-0001",
                },
            )
            assert anonymous.status_code == 401
            _assert_private_response(anonymous, "auth.token_missing")

            viewer_token = await _login(client, "viewer@prd076.local", VIEWER_PASSWORD)
            denied = await client.patch(
                "/cache-policies",
                content=b"{not-json",
                headers={
                    **_headers(
                        viewer_token,
                        "req_prd076_denied",
                        "prd076-viewer-intent-0001",
                    ),
                    "Content-Type": "application/json",
                },
            )
            assert denied.status_code == 403
            _assert_private_response(denied, "auth.permission_denied")

            admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            invalid = await client.patch(
                "/cache-policies",
                content=b"{not-json",
                headers={
                    **_headers(
                        admin_token,
                        "req_prd076_invalid",
                        "prd076-admin-intent-0001",
                    ),
                    "Content-Type": "application/json",
                },
            )
            assert invalid.status_code == 422
            _assert_private_response(invalid, "response_cache.input_invalid")

    asyncio.run(scenario())


def test_cross_tenant_alias_is_indistinguishable_from_missing_alias(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """当前租户不能通过策略写接口枚举另一个租户的真实 Alias。"""

    _, _ = _clone_environment(prd076_control_template, monkeypatch, tmp_path)
    app = _build_app()

    async def scenario() -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 scenario 对应步骤。"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            statuses: list[tuple[int, str]] = []
            for index, alias_id in enumerate(
                ("alias_prd076_foreign", "alias_prd076_missing"),
                start=1,
            ):
                response = await client.patch(
                    "/cache-policies",
                    json=_payload(alias_id, expected_version=0),
                    headers=_headers(
                        token,
                        f"req_prd076_owner_{index}",
                        f"prd076-owner-intent-000{index}",
                    ),
                )
                statuses.append((response.status_code, response.json()["code"]))
            assert statuses == [
                (404, "response_cache.policy_not_found"),
                (404, "response_cache.policy_not_found"),
            ]

    asyncio.run(scenario())


def test_finance_permission_is_reloaded_after_role_revocation(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """已签发 Session 在 role 变化后立即失去写权限，不能依赖前端隐藏按钮。"""

    database_url, alias_id = _clone_environment(
        prd076_control_template,
        monkeypatch,
        tmp_path,
    )
    app = _build_app()

    async def scenario() -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 scenario 对应步骤。"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "finance@prd076.local", FINANCE_PASSWORD)
            before = await client.get(
                "/cache-policies",
                headers=_headers(token, "req_prd076_finance_read"),
            )
            assert before.status_code == 200
            connection = connect_database(database_url)
            try:
                connection.execute(
                    """
                    UPDATE users SET role='viewer',updated_at=?
                    WHERE user_id='user_prd076_finance'
                    """,
                    (NOW,),
                )
                connection.commit()
            finally:
                connection.close()
            denied = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=0),
                headers=_headers(
                    token,
                    "req_prd076_revoked",
                    "prd076-revoked-intent-0001",
                ),
            )
            assert denied.status_code == 403
            _assert_private_response(denied, "auth.permission_denied")

    asyncio.run(scenario())


def test_get_contract_cursor_owner_and_global_disable(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖 GET 空/配置态、严格输入、租户镜像、签名游标 owner 和全局关闭。"""

    database_url, alias_id = _clone_environment(
        prd076_control_template,
        monkeypatch,
        tmp_path,
    )
    connection = connect_database(database_url)
    try:
        _insert_page_aliases(connection)
        connection.commit()
    finally:
        connection.close()
    app = _build_app()

    async def scenario() -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 scenario 对应步骤。"""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            unconfigured = await client.get(
                "/cache-policies?limit=100",
                headers={
                    **_headers(token, "req_prd076_get_unconfigured"),
                    "X-Tenant-ID": "tenant_prd076_foreign",
                },
            )
            assert unconfigured.status_code == 200
            _assert_private_response(unconfigured)
            unconfigured_body = unconfigured.json()
            assert set(unconfigured_body) == {
                "tenant_id",
                "items",
                "limit",
                "next_cursor",
                "has_more",
                "global_enabled",
                "request_id",
                "trace_id",
            }
            assert unconfigured_body["tenant_id"] != "tenant_prd076_foreign"
            assert len(unconfigured_body["items"]) == 3
            assert all(
                item["tenant_id"] == unconfigured_body["tenant_id"]
                for item in unconfigured_body["items"]
            )
            assert all(item["cache_policy"] is None for item in unconfigured_body["items"])

            created = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=0),
                headers=_headers(
                    token,
                    "req_prd076_get_configured",
                    "prd076-get-configured-0001",
                ),
            )
            assert created.status_code == 200

            first = await client.get(
                "/cache-policies?limit=1",
                headers=_headers(token, "req_prd076_get_page_1"),
            )
            assert first.status_code == 200
            _assert_private_response(first)
            first_body = first.json()
            assert first_body["has_more"] is True
            assert len(first_body["items"]) == 1
            assert first_body["items"][0]["model_alias_id"] == alias_id
            assert first_body["items"][0]["cache_policy"]["ready_entry_count"] == 0
            cursor = first_body["next_cursor"]
            assert isinstance(cursor, str) and cursor

            second = await client.get(
                f"/cache-policies?limit=1&cursor={cursor}",
                headers=_headers(token, "req_prd076_get_page_2"),
            )
            assert second.status_code == 200
            assert second.json()["items"][0]["model_alias_id"] != alias_id

            invalid_requests = (
                await client.get(
                    "/cache-policies?limit=1&unknown=value",
                    headers=_headers(token, "req_prd076_get_unknown_query"),
                ),
                await client.get(
                    "/cache-policies?limit=1&limit=1",
                    headers=_headers(token, "req_prd076_get_repeated_query"),
                ),
                await client.request(
                    "GET",
                    "/cache-policies",
                    content=b"{}",
                    headers=_headers(token, "req_prd076_get_body"),
                ),
                await client.get(
                    "/cache-policies",
                    headers=[
                        ("Authorization", f"Bearer {token}"),
                        ("X-Request-ID", "req_prd076_get_header_a"),
                        ("X-Request-ID", "req_prd076_get_header_b"),
                    ],
                ),
            )
            expected = (
                "response_cache.query_invalid",
                "response_cache.query_invalid",
                "response_cache.input_invalid",
                "response_cache.input_invalid",
            )
            for response, code in zip(invalid_requests, expected, strict=True):
                assert response.status_code == 422
                _assert_private_response(response, code)

            replacement = "A" if cursor[-1] != "A" else "B"
            tampered = await client.get(
                f"/cache-policies?limit=1&cursor={cursor[:-1]}{replacement}",
                headers=_headers(token, "req_prd076_get_tampered_cursor"),
            )
            rebound_limit = await client.get(
                f"/cache-policies?limit=2&cursor={cursor}",
                headers=_headers(token, "req_prd076_get_limit_owner"),
            )
            for response in (tampered, rebound_limit):
                assert response.status_code == 422
                _assert_private_response(response, "response_cache.query_invalid")

            foreign_login = await client.post(
                "/auth/login",
                json={
                    "tenant_slug": "prd076-foreign",
                    "email": "admin@prd076-foreign.local",
                    "password": "Prd076ForeignOnly!2026",
                },
            )
            assert foreign_login.status_code == 200
            foreign_cursor = await client.get(
                f"/cache-policies?limit=1&cursor={cursor}",
                headers=_headers(
                    str(foreign_login.json()["access_token"]),
                    "req_prd076_get_tenant_owner",
                ),
            )
            assert foreign_cursor.status_code == 422
            _assert_private_response(foreign_cursor, "response_cache.query_invalid")

        monkeypatch.setenv("METAHUB_RESPONSE_CACHE_ENABLED", "false")
        disabled_app = _build_app()
        async with AsyncClient(
            transport=ASGITransport(app=disabled_app),
            base_url="http://test",
        ) as disabled_client:
            disabled = await disabled_client.get(
                "/cache-policies?limit=100",
                headers=_headers(token, "req_prd076_get_global_disabled"),
            )
            assert disabled.status_code == 200
            _assert_private_response(disabled)
            assert disabled.json()["global_enabled"] is False
            configured = [
                item for item in disabled.json()["items"] if item["model_alias_id"] == alias_id
            ]
            assert len(configured) == 1
            assert configured[0]["cache_policy"]["status"] == "enabled"

    asyncio.run(scenario())


def test_tag_all_match_zero_match_and_all_invalidation(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """用真实 entry/tag owner 证明 0-match 围栏、all-match 交集和全量删除计数。"""

    database_url, alias_id = _clone_environment(
        prd076_control_template,
        monkeypatch,
        tmp_path,
    )
    app = _build_app()

    async def scenario() -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 scenario 对应步骤。"""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            created = await client.patch(
                "/cache-policies",
                json=_payload(alias_id, expected_version=0),
                headers=_headers(
                    token,
                    "req_prd076_invalidation_policy",
                    "prd076-invalidation-policy-0001",
                ),
            )
            assert created.status_code == 200
            policy = created.json()["cache_policy"]
            connection = connect_database(database_url)
            try:
                first_entry = _insert_cache_entry(
                    connection,
                    config=app.state.config,
                    alias_id=alias_id,
                    cache_policy_id=policy["cache_policy_id"],
                    policy_version=policy["version"],
                    policy_generation=1,
                    ordinal=1,
                    tags=("alpha", "beta"),
                )
                second_entry = _insert_cache_entry(
                    connection,
                    config=app.state.config,
                    alias_id=alias_id,
                    cache_policy_id=policy["cache_policy_id"],
                    policy_version=policy["version"],
                    policy_generation=1,
                    ordinal=2,
                    tags=("alpha",),
                )
                connection.commit()
            finally:
                connection.close()

            listed = await client.get(
                "/cache-policies?limit=100",
                headers=_headers(token, "req_prd076_invalidation_entry_count"),
            )
            assert listed.status_code == 200
            summary = next(
                item["cache_policy"]
                for item in listed.json()["items"]
                if item["model_alias_id"] == alias_id
            )
            assert summary["ready_entry_count"] == 2

            zero_match = await client.post(
                "/cache/invalidate",
                json={
                    "scope": "tags",
                    "match_mode": "all",
                    "tags": ["tag:missing"],
                    "reason_code": "manual_security",
                },
                headers=_headers(
                    token,
                    "req_prd076_invalidation_zero",
                    "prd076-invalidation-zero-0001",
                ),
            )
            assert zero_match.status_code == 200
            _assert_private_response(zero_match)
            assert (
                zero_match.json()["invalidation"]["matched_entry_count"],
                zero_match.json()["invalidation"]["deleted_entry_count"],
            ) == (0, 0)
            connection = connect_database(database_url)
            try:
                assert {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT response_cache_entry_id FROM response_cache_entries"
                    )
                } == {first_entry, second_entry}
            finally:
                connection.close()

            all_match = await client.post(
                "/cache/invalidate",
                json={
                    "scope": "tags",
                    "match_mode": "all",
                    "tags": ["tag:alpha", "tag:beta"],
                    "reason_code": "manual_configuration",
                },
                headers=_headers(
                    token,
                    "req_prd076_invalidation_match",
                    "prd076-invalidation-match-0001",
                ),
            )
            assert all_match.status_code == 200
            _assert_private_response(all_match)
            assert (
                all_match.json()["invalidation"]["matched_entry_count"],
                all_match.json()["invalidation"]["deleted_entry_count"],
            ) == (1, 1)
            connection = connect_database(database_url)
            try:
                assert (
                    connection.execute(
                        "SELECT response_cache_entry_id FROM response_cache_entries"
                    ).fetchone()[0]
                    == second_entry
                )
            finally:
                connection.close()

            invalidate_all = await client.post(
                "/cache/invalidate",
                json={
                    "scope": "all",
                    "match_mode": "all",
                    "tags": [],
                    "reason_code": "manual_finops",
                },
                headers=_headers(
                    token,
                    "req_prd076_invalidation_all",
                    "prd076-invalidation-all-0001",
                ),
            )
            assert invalidate_all.status_code == 200
            _assert_private_response(invalidate_all)
            assert (
                invalidate_all.json()["invalidation"]["matched_entry_count"],
                invalidate_all.json()["invalidation"]["deleted_entry_count"],
            ) == (1, 1)

    asyncio.run(scenario())
    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM response_cache_entries").fetchone()[0] == 0
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM response_cache_invalidations
            WHERE reason_code IN ('manual_security','manual_configuration','manual_finops')
            """
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT MIN(generation) FROM response_cache_tag_generations"
            ).fetchone()[0]
            >= 2
        )
    finally:
        connection.close()


def test_source_outbox_snapshot_retry_dead_letter_and_owner_tamper(
    prd076_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """复用真实 source 服务验证冻结快照、恢复重试、死信和 envelope owner 防篡改。"""

    database_url, _ = _clone_environment(
        prd076_control_template,
        monkeypatch,
        tmp_path,
    )
    connection = connect_database(database_url)
    try:
        _insert_cache_webhook_endpoint(connection, endpoint_id="wh_prd076_cache_primary")
        connection.commit()
    finally:
        connection.close()
    app = _build_app()

    async def create_source() -> tuple[str, str]:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 create_source 对应步骤。"""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            response = await client.post(
                "/cache/invalidate",
                json={
                    "scope": "all",
                    "match_mode": "all",
                    "tags": [],
                    "reason_code": "manual_other",
                },
                headers=_headers(
                    token,
                    "req_prd076_source_outbox",
                    "prd076-source-outbox-0001",
                ),
            )
            assert response.status_code == 200
            return (
                str(response.json()["invalidation"]["cache_invalidation_id"]),
                str(response.json()["request_id"]),
            )

    invalidation_id, source_request_id = asyncio.run(create_source())
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT event.response_cache_event_id,outbox.next_attempt_at
            FROM response_cache_events event
            JOIN response_cache_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.response_cache_event_id=event.response_cache_event_id
            WHERE event.object_id=?
            """,
            (invalidation_id,),
        ).fetchone()
        assert source is not None
        event_id = str(source["response_cache_event_id"])
        first_attempt_at = datetime.fromisoformat(
            str(source["next_attempt_at"]).replace("Z", "+00:00")
        ) + timedelta(seconds=1)
    finally:
        connection.close()

    claim = response_cache_outbox.claim_next_response_cache_event(
        app.state.config,
        worker_id="prd076-control-source-worker",
        now=first_attempt_at,
    )
    assert claim is not None and claim.event_id == event_id
    assert claim.source_request_id == source_request_id
    connection = connect_database(database_url)
    try:
        frozen = connection.execute(
            """
            SELECT subscription_snapshot_json,snapshot_frozen_at
            FROM response_cache_event_outbox
            WHERE tenant_id=? AND response_cache_event_id=?
            """,
            (claim.tenant_id, event_id),
        ).fetchone()
        deliveries = connection.execute(
            """
            SELECT webhook_delivery_id FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            """,
            (claim.tenant_id, event_id),
        ).fetchall()
        assert frozen is not None and frozen["snapshot_frozen_at"] is not None
        assert len(deliveries) == 1
        frozen_ids = json.loads(str(frozen["subscription_snapshot_json"]))
        assert frozen_ids == [str(deliveries[0]["webhook_delivery_id"])]
        assert (
            response_cache_outbox.recover_response_cache_source_request_id(
                connection,
                claim.event.to_payload(),
            )
            == source_request_id
        )
        tampered = dict(claim.event.to_payload())
        tampered["trace_id"] = "trace_prd076_tampered"
        with pytest.raises(ApiError) as error:
            response_cache_outbox.recover_response_cache_source_request_id(
                connection,
                tampered,
            )
        assert error.value.code == "webhook.delivery_unavailable"

        _insert_cache_webhook_endpoint(connection, endpoint_id="wh_prd076_cache_late")
        connection.commit()
    finally:
        connection.close()

    def unavailable_delivery(*_args: object, **_kwargs: object) -> None:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 unavailable_delivery 对应步骤。"""
        # 该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。
        raise RuntimeError("synthetic delivery failure")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", unavailable_delivery)
    current = first_attempt_at
    for expected_attempt in range(1, response_cache_outbox.EVENT_MAX_ATTEMPTS + 1):
        if expected_attempt > 1:
            current += timedelta(hours=1)
            claim = response_cache_outbox.claim_next_response_cache_event(
                app.state.config,
                worker_id="prd076-control-source-worker",
                now=current,
            )
            assert claim is not None and claim.event_id == event_id
        assert claim.attempt_count == expected_attempt - 1
        assert (
            response_cache_outbox.dispatch_claimed_response_cache_event(
                app.state.config,
                claim,
                now=current,
            )
            is True
        )

    assert (
        response_cache_outbox.claim_next_response_cache_event(
            app.state.config,
            worker_id="prd076-control-source-worker",
            now=current + timedelta(hours=1),
        )
        is None
    )
    connection = connect_database(database_url)
    try:
        terminal = connection.execute(
            """
            SELECT status,attempt_count,last_error_code,subscription_snapshot_json
            FROM response_cache_event_outbox
            WHERE tenant_id=? AND response_cache_event_id=?
            """,
            (claim.tenant_id, event_id),
        ).fetchone()
        assert terminal is not None
        assert (
            terminal["status"],
            terminal["attempt_count"],
            terminal["last_error_code"],
        ) == ("dead_letter", 10, "webhook.delivery_unavailable")
        assert json.loads(str(terminal["subscription_snapshot_json"])) == frozen_ids
        delivery = connection.execute(
            """
            SELECT status,error_code FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            """,
            (claim.tenant_id, event_id),
        ).fetchall()
        assert [(row["status"], row["error_code"]) for row in delivery] == [
            ("failed", "webhook.delivery_unavailable")
        ]
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM webhook_dead_letter_events
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
                (claim.tenant_id, frozen_ids[0]),
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
