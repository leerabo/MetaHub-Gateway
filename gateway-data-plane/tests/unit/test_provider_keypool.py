"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/unit/test_provider_keypool.py
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from metahub_gateway.cache_affinity import keypool as affinity_keypool_module
from metahub_gateway.providers import keypool as keypool_module
from metahub_gateway.providers.keypool import select_provider_key, snapshot_provider_key_pool
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import encrypt_provider_key, secret_fingerprint, secret_prefix
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, GatewayRoute]:
    """prepare_database：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "test-provider-key-encryption-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", "http://127.0.0.1:18080/v1")
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", "sk-env-fallback")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        """
        SELECT provider_id, provider_code, provider_type, auth_type, name, base_url
        FROM providers
        WHERE tenant_id=? AND provider_code='local_openai'
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    connection.execute(
        "DELETE FROM provider_credentials WHERE tenant_id=? AND provider_id=?",
        (seed.tenant_id, provider["provider_id"]),
    )
    connection.commit()
    route = GatewayRoute(
        tenant_id=seed.tenant_id,
        model_id="model_prd014",
        model_alias="default-chat",
        upstream_model_name="local-chat-model",
        capabilities={"chat": True, "stream": True},
        provider_id=provider["provider_id"],
        provider_code=provider["provider_code"],
        provider_type=provider["provider_type"],
        provider_auth_type=provider["auth_type"],
        provider_name=provider["name"],
        provider_base_url=provider["base_url"],
    )
    return database_url, route


def test_environment_key_is_bound_to_default_tenant_and_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全：第二租户或被管理员改写的 Endpoint 均不能取得进程级 Provider Key。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    config = load_app_config("gateway-data-plane")
    connection = connect_database(database_url)
    selected = select_provider_key(config, route)
    snapshot = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=datetime.now(UTC),
    )
    assert selected is not None
    assert selected.source == "environment"
    assert selected.plain_value == "sk-env-fallback"
    assert snapshot.state == "available"

    # 默认租户若把相同 Provider code 指向自控地址，必须同时在快照和实际选择阶段失败关闭。
    attacker_url = "https://attacker.invalid/v1/provider-key-sentinel"
    connection.execute(
        "UPDATE providers SET base_url=? WHERE tenant_id=? AND provider_id=?",
        (attacker_url, route.tenant_id, route.provider_id),
    )
    connection.commit()
    changed_route = GatewayRoute(**{**route.__dict__, "provider_base_url": attacker_url})
    with pytest.raises(ApiError) as changed_error:
        select_provider_key(config, changed_route)
    changed_snapshot = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=datetime.now(UTC),
    )
    assert changed_error.value.code == "provider_key.unavailable"
    assert changed_snapshot.state == "unavailable"

    # 第二租户即使完全复制 code 和配置基准 URL，也没有进程级环境 Key 的所有权。
    other_tenant_id = "tenant_keypool_attacker"
    other_provider_id = "provider_keypool_attacker"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(?, 'keypool-attacker', 'KeyPool Attacker', 'internal_only', 'active',
               datetime('now'), datetime('now'))
        """,
        (other_tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, health_status, created_at, updated_at
        )
        VALUES(?, ?, 'local_openai', 'Attacker Provider', ?, 'active',
               'openai_compatible', 'bearer', 'unknown', datetime('now'), datetime('now'))
        """,
        (other_provider_id, other_tenant_id, config.provider_base_urls["local_openai"]),
    )
    connection.commit()
    other_route = GatewayRoute(
        **{
            **route.__dict__,
            "tenant_id": other_tenant_id,
            "provider_id": other_provider_id,
            "provider_base_url": config.provider_base_urls["local_openai"],
        }
    )
    with pytest.raises(ApiError) as other_error:
        select_provider_key(config, other_route)
    other_snapshot = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=other_tenant_id,
        provider_id=other_provider_id,
        provider_code="local_openai",
        provider_auth_type="bearer",
        as_of=datetime.now(UTC),
    )
    assert other_error.value.code == "provider_key.unavailable"
    assert other_snapshot.state == "unavailable"


def test_environment_key_is_disabled_in_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """生产环境无法表达进程 Key 的租户归属，因此快照和选择器必须一致拒绝。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    config = load_app_config("gateway-data-plane").model_copy(update={"environment": "prod"})
    connection = connect_database(database_url)

    with pytest.raises(ApiError) as exc:
        select_provider_key(config, route)
    snapshot = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=datetime.now(UTC),
    )
    assert exc.value.code == "provider_key.unavailable"
    assert snapshot.state == "unavailable"


def insert_provider_key(
    database_url: str,
    route: GatewayRoute,
    provider_key_id: str,
    secret: str,
    status: str = "active",
) -> None:
    """insert_provider_key：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO provider_credentials(
          provider_credential_id, tenant_id, provider_id, key_fingerprint, encrypted_key,
          name, key_prefix, weight, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 100, ?, datetime('now'), datetime('now'))
        """,
        (
            provider_key_id,
            route.tenant_id,
            route.provider_id,
            secret_fingerprint(secret),
            encrypt_provider_key(secret),
            provider_key_id,
            secret_prefix(secret),
            status,
        ),
    )
    connection.commit()


@pytest.fixture()
def fake_rate_limited_upstream():
    """fake_rate_limited_upstream：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """Handler：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        def do_POST(self) -> None:  # noqa: N802
            """do_POST：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"type":"rate_limit","message":"too many requests"}}')

        def log_message(self, format: str, *args: object) -> None:
            """log_message：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_select_provider_key_prefers_database_key_and_skips_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test_select_provider_key_prefers_database_key_and_skips_disabled：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url, route = prepare_database(monkeypatch, tmp_path)
    insert_provider_key(database_url, route, "key_disabled", "sk-disabled-key", status="disabled")
    insert_provider_key(database_url, route, "key_active", "sk-database-key", status="active")
    config = load_app_config("gateway-data-plane")

    selected = select_provider_key(config, route)

    assert selected is not None
    assert selected.plain_value == "sk-database-key"
    assert selected.provider_credential_id == "key_active"
    assert selected.source == "database"
    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT last_used_at FROM provider_credentials WHERE provider_credential_id='key_active'"
    ).fetchone()
    assert row["last_used_at"] is not None


def test_select_provider_key_returns_unavailable_when_managed_keys_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """test_select_provider_key_returns_unavailable_when_managed_keys_are_disabled：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url, route = prepare_database(monkeypatch, tmp_path)
    insert_provider_key(database_url, route, "key_disabled", "sk-disabled-key", status="disabled")
    config = load_app_config("gateway-data-plane")

    with pytest.raises(ApiError) as exc:
        select_provider_key(config, route)

    assert exc.value.code == "provider_key.unavailable"


def test_select_provider_key_skips_fresh_unhealthy_key_but_allows_stale_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """KeyPool 只过滤新鲜认证失败 Key；过期结论 Fail Open，避免永久锁死凭据。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    insert_provider_key(database_url, route, "key_bad", "sk-bad-key")
    insert_provider_key(database_url, route, "key_good", "sk-good-key")
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE provider_credentials
        SET probe_health_status='unhealthy', probe_last_tested_at=datetime('now')
        WHERE provider_credential_id='key_bad'
        """
    )
    connection.commit()
    config = load_app_config("gateway-data-plane")

    selected = select_provider_key(config, route)
    assert selected is not None
    assert selected.provider_credential_id == "key_good"

    connection.execute(
        """
        UPDATE provider_credentials
        SET probe_last_tested_at=datetime('now', '-10 minutes'), last_used_at=NULL
        WHERE provider_credential_id='key_bad'
        """
    )
    connection.execute(
        "UPDATE provider_credentials SET last_used_at=datetime('now') WHERE provider_credential_id='key_good'"
    )
    connection.commit()
    selected_stale = select_provider_key(config, route)
    assert selected_stale is not None
    assert selected_stale.provider_credential_id == "key_bad"


def test_provider_key_decrypt_failure_log_omits_credential_id_and_short_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """敏感日志：普通 KeyPool 解密失败只记录 Provider/模型，不记录 ID 或完整短前缀。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    credential_id = "credential-decrypt-log-sentinel"
    short_secret = "tiny-secret"
    insert_provider_key(database_url, route, credential_id, short_secret)
    config = load_app_config("gateway-data-plane")

    def reject_decrypt(_ciphertext: str) -> str:
        """模拟不可解密托管 Key，异常正文不得进入应用日志。"""

        # 测试异常正文刻意含敏感哨兵，用于证明日志不会序列化底层异常。
        raise ValueError("ciphertext detail must remain private")

    monkeypatch.setattr(keypool_module, "decrypt_provider_key", reject_decrypt)
    caplog.set_level(logging.WARNING, logger="metahub.provider_keypool")

    with pytest.raises(ApiError) as exc:
        select_provider_key(config, route)

    assert exc.value.code == "provider_key.unavailable"
    records = [record for record in caplog.records if record.getMessage() == "provider_key.decrypt_failed"]
    assert len(records) == 1
    record = records[0]
    assert record.tenant_id == route.tenant_id
    assert record.provider_id == route.provider_id
    assert record.payload == {"provider_code": route.provider_code, "model_id": route.model_id}
    assert not hasattr(record, "object_id")
    rendered = json.dumps(record.__dict__, ensure_ascii=False, default=str)
    assert credential_id not in rendered
    assert short_secret not in rendered
    assert "ciphertext detail must remain private" not in rendered


def test_affinity_key_decrypt_failure_log_uses_real_request_without_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """敏感日志：亲和选 Key 透传真实链路 ID，同时隐藏 Credential ID、短前缀和异常正文。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    credential_id = "credential-affinity-log-sentinel"
    short_secret = "tiny-affinity"
    insert_provider_key(database_url, route, credential_id, short_secret)
    config = load_app_config("gateway-data-plane")
    connection = connect_database(database_url)

    def reject_decrypt(_ciphertext: str) -> str:
        """模拟亲和事务内解密失败，验证日志只消费显式安全上下文。"""

        # 亲和事务只允许记录真实链路字段，底层解密异常正文必须留在内存中。
        raise ValueError("affinity ciphertext detail must remain private")

    monkeypatch.setattr(affinity_keypool_module, "decrypt_provider_key", reject_decrypt)
    caplog.set_level(logging.WARNING, logger="metahub.gateway.cache_affinity")

    with pytest.raises(keypool_module.ProviderKeyDecryptAnomaly) as exc:
        affinity_keypool_module._select_key_in_transaction(
            config,
            connection,
            route=route,
            binding={"provider_credential_id": credential_id},  # type: ignore[arg-type]
            request_id="req_affinity_decrypt_log",
            trace_id="trc_affinity_decrypt_log",
        )

    assert exc.value.error_code == "provider_key.unavailable"
    assert exc.value.candidate_count == 1
    records = [record for record in caplog.records if record.getMessage() == "provider_key.decrypt_failed"]
    assert len(records) == 1
    record = records[0]
    assert record.request_id == "req_affinity_decrypt_log"
    assert record.trace_id == "trc_affinity_decrypt_log"
    assert record.tenant_id == route.tenant_id
    assert record.provider_id == route.provider_id
    assert record.payload == {"provider_code": route.provider_code, "model_id": route.model_id}
    assert not hasattr(record, "object_id")
    rendered = json.dumps(record.__dict__, ensure_ascii=False, default=str)
    assert credential_id not in rendered
    assert short_secret not in rendered
    assert "affinity ciphertext detail must remain private" not in rendered


def test_provider_decrypt_anomaly_persists_only_with_trusted_coordinates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """坏候选可被下一把 Key 接管，但仅生产链路生成最小安全 source。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    model = connection.execute(
        """SELECT model_id FROM models
             WHERE tenant_id=? AND provider_id=? ORDER BY model_id LIMIT 1""",
        (route.tenant_id, route.provider_id),
    ).fetchone()
    assert model is not None
    anomaly_route = GatewayRoute(
        **{
            **route.__dict__,
            "model_id": str(model["model_id"]),
        }
    )
    corrupt_id = "credential-prd081-corrupt-sentinel"
    corrupt_secret = "provider-prd081-corrupt-secret"
    insert_provider_key(database_url, anomaly_route, corrupt_id, corrupt_secret)
    insert_provider_key(
        database_url,
        anomaly_route,
        "credential-prd081-usable",
        "provider-prd081-usable-secret",
    )
    connection.execute(
        "UPDATE provider_credentials SET encrypted_key=? WHERE provider_credential_id=?",
        ("aesgcm-v1:invalid-prd081-ciphertext", corrupt_id),
    )
    connection.commit()
    config = load_app_config("gateway-data-plane")

    selected = select_provider_key(
        config,
        anomaly_route,
        request_id="req_prd081_provider_decrypt",
        trace_id="trc_prd081_provider_decrypt",
    )
    assert selected is not None and selected.source == "database"
    # 直接调用缺少可信生产坐标时仍可跳过坏候选，但不得制造第二条 source。
    selected_without_context = select_provider_key(config, anomaly_route)
    assert selected_without_context is not None

    anomaly = connection.execute(
        """SELECT tenant_id,provider_id,model_id,anomaly_type,request_id,trace_id,
                  candidate_count
             FROM provider_key_anomaly_events"""
    ).fetchone()
    source = connection.execute(
        """SELECT source_event_type,category,severity,severity_score,action,outcome,
                  resource_type,resource_id,occurrence_count
             FROM security_event_sources"""
    ).fetchone()
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(provider_key_anomaly_events)")
    }
    assert tuple(anomaly) == (
        anomaly_route.tenant_id,
        anomaly_route.provider_id,
        anomaly_route.model_id,
        "decrypt_failed",
        "req_prd081_provider_decrypt",
        "trc_prd081_provider_decrypt",
        1,
    )
    assert tuple(source) == (
        "security.provider_key_anomaly",
        "provider_credential",
        "critical",
        9,
        "decrypt_failed",
        "rejected",
        "provider",
        anomaly_route.provider_id,
        1,
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM provider_key_anomaly_events"
    ).fetchone()[0] == 1
    assert not columns & {
        "provider_credential_id",
        "key_prefix",
        "encrypted_key",
        "error_message",
    }


def test_keypool_snapshot_is_read_only_and_respects_managed_key_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """路由资格快照不解密、不写 last_used，并且托管 Key 存在时禁止环境回退。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    insert_provider_key(database_url, route, "key_snapshot", "sk-snapshot-key")
    config = load_app_config("gateway-data-plane")
    connection = connect_database(database_url)

    def reject_decrypt(_ciphertext: str) -> str:
        """快照若意外解密立即失败，防止路由资格阶段接触 Provider Key 明文。"""

        # 快照合同明确禁止解密，触发此函数即代表敏感信息边界发生回归。
        raise AssertionError("snapshot must not decrypt provider keys")

    monkeypatch.setattr(keypool_module, "decrypt_provider_key", reject_decrypt)
    before = connection.execute(
        """
        SELECT status, last_used_at, rate_limited_until
        FROM provider_credentials WHERE provider_credential_id='key_snapshot'
        """
    ).fetchone()

    available = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=datetime.now(UTC),
    )
    after = connection.execute(
        """
        SELECT status, last_used_at, rate_limited_until
        FROM provider_credentials WHERE provider_credential_id='key_snapshot'
        """
    ).fetchone()
    assert available.state == "available"
    assert tuple(after) == tuple(before)

    connection.execute(
        "UPDATE provider_credentials SET status='disabled' WHERE provider_credential_id='key_snapshot'"
    )
    connection.commit()
    unavailable = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=datetime.now(UTC),
    )
    # 环境变量虽然存在，但有托管 Key 时不能绕过租户 KeyPool 治理。
    assert unavailable.state == "unavailable"


def test_keypool_snapshot_rejects_unhealthy_key_at_exact_stale_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """300 秒整仍属于新鲜 unhealthy 证据；再早一秒才按 stale Fail Open。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    insert_provider_key(database_url, route, "key_boundary", "sk-boundary-key")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_STALE_SECONDS", "300")
    config = load_app_config("gateway-data-plane")
    connection = connect_database(database_url)
    as_of = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    boundary = as_of - timedelta(seconds=300)
    connection.execute(
        """
        UPDATE provider_credentials
        SET probe_health_status='unhealthy', probe_last_tested_at=?
        WHERE provider_credential_id='key_boundary'
        """,
        (boundary.isoformat().replace("+00:00", "Z"),),
    )
    connection.commit()

    exact = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=as_of,
    )
    assert exact.state == "unavailable"

    connection.execute(
        """
        UPDATE provider_credentials SET probe_last_tested_at=?
        WHERE provider_credential_id='key_boundary'
        """,
        ((boundary - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
    )
    connection.commit()
    stale = snapshot_provider_key_pool(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        provider_auth_type=route.provider_auth_type,
        as_of=as_of,
    )
    assert stale.state == "available"


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    [
        ("probe_health_status", "corrupt"),
        ("status", "corrupt"),
        ("probe_last_tested_at", "not-a-time"),
        ("rate_limited_until", "not-a-time"),
        ("probe_last_tested_at", "2026-07-18T12:00:01Z"),
    ],
)
def test_keypool_snapshot_rejects_corrupt_managed_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    column: str,
    corrupt_value: str,
) -> None:
    """损坏枚举、时间或未来探测必须中止快照，不能因当前状态分支而静默放行。"""

    database_url, route = prepare_database(monkeypatch, tmp_path)
    insert_provider_key(database_url, route, "key_corrupt", "sk-corrupt-key")
    config = load_app_config("gateway-data-plane")
    connection = connect_database(database_url)
    # 测试需模拟绕过正常写 API 的存量坏数据；列名来自上方固定参数白名单，不接收外部输入。
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        f"UPDATE provider_credentials SET {column}=? WHERE provider_credential_id='key_corrupt'",
        (corrupt_value,),
    )
    connection.commit()

    with pytest.raises(ValueError):
        snapshot_provider_key_pool(
            config,
            connection,
            tenant_id=route.tenant_id,
            provider_id=route.provider_id,
            provider_code=route.provider_code,
            provider_auth_type=route.provider_auth_type,
            as_of=datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC),
        )


@pytest.mark.anyio
async def test_adapter_marks_selected_key_rate_limited_on_429(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_rate_limited_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """429 时仍更新目标 Key，但日志只能用请求、Provider 和模型安全元数据关联。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    base_url, captured = fake_rate_limited_upstream
    database_url, route = prepare_database(monkeypatch, tmp_path)
    route = GatewayRoute(**{**route.__dict__, "provider_base_url": base_url})
    insert_provider_key(database_url, route, "key_active", "sk-database-key", status="active")
    config = load_app_config("gateway-data-plane")
    adapter = OpenAICompatibleAdapter()
    request = adapter.build_request(route, "chat/completions", {"model": "default-chat"}, config)
    request.request_id = "req_prd014_rate_limit"
    request.trace_id = "trc_prd014_rate_limit"
    caplog.set_level(logging.INFO, logger="metahub.provider_keypool")

    with pytest.raises(ApiError) as exc:
        await adapter.send_non_stream(request)

    assert exc.value.code == "gateway.upstream_rate_limited"
    assert captured[-1]["authorization"] == "Bearer sk-database-key"
    connection = connect_database(database_url)
    row = connection.execute(
        """
        SELECT status, rate_limited_until, last_error_code, last_error_message
        FROM provider_credentials
        WHERE provider_credential_id='key_active'
        """
    ).fetchone()
    assert row["status"] == "rate_limited"
    assert row["rate_limited_until"] is not None
    assert row["last_error_code"] == "gateway.upstream_rate_limited"
    assert "rate limited" in row["last_error_message"]
    records = [record for record in caplog.records if record.getMessage() == "provider_key.rate_limited"]
    assert len(records) == 1
    record = records[0]
    assert record.request_id == "req_prd014_rate_limit"
    assert record.trace_id == "trc_prd014_rate_limit"
    assert record.tenant_id == route.tenant_id
    assert record.provider_id == route.provider_id
    assert record.payload == {
        "provider_code": route.provider_code,
        "model_id": route.model_id,
        "http_status": 429,
        "error_code": "gateway.upstream_rate_limited",
    }
    assert not hasattr(record, "object_id")
    rendered = json.dumps(record.__dict__, ensure_ascii=False, default=str)
    assert "key_active" not in rendered
    assert secret_prefix("sk-database-key") not in rendered
