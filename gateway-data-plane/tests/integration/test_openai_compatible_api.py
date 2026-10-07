"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_openai_compatible_api.py
"""

from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import metahub_gateway.api.openai_compatible as openai_api
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_gateway.policies.cache import PolicySnapshotLeaseDecision
from metahub_gateway.providers.base import UpstreamApiError
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing import models as routing_models
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.gateway_fleet import create_placement_snapshot, install_placement_snapshot
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.policy_snapshot import (
    build_policy_snapshot_artifact,
    build_policy_snapshot_item,
)

# 以下测试密钥只用于 OpenAI-compatible 主链路夹具，断言请求失败也会进入日志。
VIRTUAL_KEY = "mhv_prd009_virtual_key"
PROVIDER_KEY = "sk-prd009-provider-secret"
CACHE_TEST_PROMPT = "prd076 synthetic cache prompt 6f34d9b1"
CACHE_TEST_RESPONSE = "prd076 synthetic cache response 8ac217e4"
CACHE_TEST_HMAC_SECRET = "prd076-hmac-test-secret-" + "h" * 40
CACHE_TEST_ENCRYPTION_SECRET = "prd076-encryption-test-secret-" + "e" * 40
CACHE_TEST_FAILURE_SPOOL_SECRET = "prd076-spool-test-secret-" + "s" * 40


class StaticPolicySnapshotCache:
    """为集成测试按请求返回预置不可变 lease，不执行真实 Control 网络同步。"""

    def __init__(self, decisions: list[PolicySnapshotLeaseDecision]) -> None:
        """按调用顺序保存决策；最后一项可供额外调用继续复用。"""

        self.decisions = decisions
        self.calls: list[str] = []

    async def acquire(self, tenant_id: str) -> PolicySnapshotLeaseDecision:
        """记录已认证租户标识，并返回当前预置的不可变策略快照租约。"""

        self.calls.append(tenant_id)
        index = min(len(self.calls) - 1, len(self.decisions) - 1)
        return self.decisions[index]


def build_policy_lease(
    tenant_id: str,
    *,
    version: int,
    action_type: str,
    expression: str,
    target_model_id: str | None = None,
) -> PolicySnapshotLeaseDecision:
    """构造通过 shared 双 hash 校验的 current lease，供真实 HTTP 热路径使用。"""

    item = build_policy_snapshot_item(
        evaluation_order=0,
        policy_id=f"pol_prd045_{version}",
        policy_version=version,
        policy_rule_id=f"prule_prd045_{version}",
        policy_action_id=f"pact_prd045_{version}",
        priority=100,
        action_type=action_type,
        target_model_id=target_model_id,
        expression=expression,
    )
    artifact = build_policy_snapshot_artifact(
        tenant_id=tenant_id,
        policy_snapshot_id=f"psnap_prd045_{version}",
        snapshot_version=version,
        operation="publish",
        previous_snapshot_id=f"psnap_prd045_{version - 1}" if version > 1 else None,
        previous_snapshot_version=version - 1 if version > 1 else None,
        policies=(item,),
    )
    return PolicySnapshotLeaseDecision(
        executable=True,
        snapshot=artifact,
        snapshot_version=version,
        status="current",
        error_code=None,
        acquired_monotonic=float(version),
    )


def persist_claimable_snapshot_version(
    database_url: str,
    tenant_id: str,
    *,
    version: int = 1,
) -> None:
    """封印一份空快照，使 0034 claim 外键能验证集成测试使用的真实版本。

    运行期 artifact 仍由 shared 双 hash 对象提供；该数据库制品只验证 request claim 的
    tenant+published version 约束，不伪造 Policy 来源行或绕过触发器。
    """

    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["user_id"]
    snapshot_id = f"psnap_prd045_claim_{version}"
    connection.execute(
        """
        INSERT INTO policy_snapshots(
          policy_snapshot_id, tenant_id, snapshot_version, schema_version, state,
          operation, previous_snapshot_id, previous_snapshot_version,
          rollback_source_snapshot_id, rollback_source_snapshot_version,
          request_id, trace_id, request_fingerprint, content_hash, artifact_hash,
          policy_count, change_reason, approval_status, created_by, created_at, published_at
        )
        VALUES(?, ?, ?, 'policy-snapshot-v1', 'building', 'publish', NULL, NULL,
               NULL, NULL, ?, ?, ?, NULL, NULL, NULL, 'Gateway 集成测试快照',
               'not_required', ?, '2026-07-17T12:00:00Z', NULL)
        """,
        (
            snapshot_id,
            tenant_id,
            version,
            f"req_prd045_claim_{version}",
            f"trc_prd045_claim_{version}",
            "b" * 64,
            user_id,
        ),
    )
    connection.execute(
        """
        UPDATE policy_snapshots
        SET state='published', content_hash=?, artifact_hash=?, policy_count=0,
            published_at='2026-07-17T12:00:01Z'
        WHERE tenant_id=? AND policy_snapshot_id=?
        """,
        ("c" * 64, "d" * 64, tenant_id, snapshot_id),
    )
    connection.commit()


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def count_plaintext_matches(
    connection: sqlite3.Connection,
    canaries: tuple[bytes, ...],
) -> int:
    """Scan declared SQLite TEXT/BLOB columns without exposing matched values.

    该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。 当前定义负责 count_plaintext_matches 对应步骤。"""

    matches = 0
    tables = connection.execute(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    for table_row in tables:
        table_name = str(table_row["name"])
        quoted_table = '"' + table_name.replace('"', '""') + '"'
        columns = []
        for column in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall():
            declared_type = str(column["type"] or "").upper()
            if declared_type == "BLOB" or any(
                marker in declared_type for marker in ("CHAR", "CLOB", "TEXT")
            ):
                column_name = str(column["name"])
                columns.append('"' + column_name.replace('"', '""') + '"')
        if not columns:
            continue
        for row in connection.execute(f"SELECT {','.join(columns)} FROM {quoted_table}"):
            for value in row:
                if isinstance(value, str):
                    encoded = value.encode("utf-8", errors="strict")
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    encoded = bytes(value)
                else:
                    continue
                matches += sum(encoded.count(canary) for canary in canaries)
    return matches


def build_gateway_app():
    """build_gateway_app：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("gateway-data-plane", "Gateway PRD-009 Test API")
    app.include_router(openai_compatible_router)
    return app


@pytest.fixture()
def fake_upstream():
    """fake_upstream：说明该定义的测试或脚本职责。

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
            # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
            # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
            if body.get("model") == "upstream-error-model":
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"upstream failed"}}')
                return
            if body.get("model") == "upstream-quota-model":
                self.send_response(402)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"type":"insufficient_quota","message":"quota"}}'
                )
                return
            if self.path == "/v1/chat/completions":
                if body.get("stream") is True:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    # 畸形事件必须被计量解析器安全忽略，但仍原样转发给调用方。
                    self.wfile.write(b"data: {not-json}\n\n")
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
                    prompt = body.get("messages", [{}])[0].get("content")
                    if prompt != "fallback private prompt":
                        self.wfile.write(
                            b'data: {"choices":[],"usage":{"prompt_tokens":11,'
                            b'"completion_tokens":7,"total_tokens":18,'
                            b'"api_key":"usage-secret-must-not-persist"}}\n\n'
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                prompt = body.get("messages", [{}])[0].get("content")
                response_content = (
                    CACHE_TEST_RESPONSE if prompt == CACHE_TEST_PROMPT else "pong"
                )
                self._json(
                    {
                        "id": "chatcmpl_prd009",
                        "object": "chat.completion",
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": response_content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                    }
                )
                return
            if self.path == "/v1/embeddings":
                self._json(
                    {
                        "object": "list",
                        "model": body["model"],
                        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                        "usage": {"prompt_tokens": 2, "total_tokens": 2},
                    }
                )
                return
            self.send_response(404)
            self.end_headers()

        def _json(self, payload: dict[str, Any]) -> None:
            """_json：说明该定义的测试或脚本职责。

            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

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


def prepare_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_base_url: str
) -> str:
    """prepare_database：说明该定义的测试或脚本职责。

    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    # A usable gateway key must be assigned to an active application. Keep the
    # shared integration fixture aligned with the current authorization rule.
    connection.execute(
        "INSERT INTO projects(project_id,tenant_id,project_code,name,status,created_at,updated_at) "
        "VALUES('project_prd009',?,'prd009','PRD009','active',datetime('now'),datetime('now'))",
        (seed.tenant_id,),
    )
    connection.execute(
        "INSERT INTO applications(application_id,tenant_id,project_id,application_code,name,status,created_at,updated_at) "
        "VALUES('application_prd009',?,'project_prd009','prd009','PRD009','active',datetime('now'),datetime('now'))",
        (seed.tenant_id,),
    )
    connection.execute(
        "UPDATE virtual_keys SET project_id='project_prd009', application_id='application_prd009' "
        "WHERE tenant_id=?",
        (seed.tenant_id,),
    )
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES
          ('model_prd009_embedding', ?, ?, 'default-embedding', 'upstream-embedding-model',
           '{"embeddings": true}', 'active', datetime('now'), datetime('now')),
          ('model_prd009_error', ?, ?, 'upstream-error', 'upstream-error-model',
           '{"chat": true}', 'active', datetime('now'), datetime('now'))
        """,
        (seed.tenant_id, provider["provider_id"], seed.tenant_id, provider["provider_id"]),
    )
    connection.commit()
    return database_url


def install_prd101_gateway_placement(app, database_url: str) -> None:
    """从服务端 owner 事实构造启用态签名快照，禁止测试请求注入 Workspace 或 Cell。"""

    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT binding.tenant_id,binding.workspace_id,key.virtual_key_id,
              cell.gateway_cell_id,cell.cell_code,cell.region_code
            FROM workspace_tenant_bindings binding
            JOIN virtual_keys key ON key.tenant_id=binding.tenant_id
            JOIN gateway_cells cell ON cell.cell_code='cn-north-1-a'
            WHERE binding.status='active'
            LIMIT 1
            """
        ).fetchone()
        assert owner is not None
    finally:
        connection.close()
    snapshot = create_placement_snapshot(
        signing_secret=app.state.config.gateway_placement_signing_secret,
        signing_key_version=app.state.config.gateway_placement_signing_version,
        tenant_id=str(owner["tenant_id"]),
        workspace_id=str(owner["workspace_id"]),
        gateway_cell_id=str(owner["gateway_cell_id"]),
        cell_code=str(owner["cell_code"]),
        region_code=str(owner["region_code"]),
        placement_revision=1,
        residency_policy_version="cn-residency-v1",
        generation=1,
        ttl_seconds=30,
    )
    install_placement_snapshot(app.state, snapshot)


def configure_prd079_exhausted_quota_with_budget(database_url: str) -> str:
    """配置可通过 Budget 但额度为零的套餐，返回默认租户 ID。"""

    connection = connect_database(database_url)
    try:
        tenant = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()
        assert tenant is not None
        tenant_id = str(tenant["tenant_id"])
        environment = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (tenant_id,),
        ).fetchone()
        assert environment is not None
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k='1.000000',output_price_per_1k='1.000000',
                currency='CNY',price_schema_version='prd079-api-price-v1'
            WHERE tenant_id=? AND model_alias='default-chat'
            """,
            (tenant_id,),
        )
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
              daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
              exhausted_action,status,created_at,updated_at
            ) VALUES('budget_prd079_quota_402',?,?,'tenant',?,'PRD-079 Quota 402',
                     100,80,100,'block','active',datetime('now'),datetime('now'))
            """,
            (tenant_id, environment["environment_id"], tenant_id),
        )
        plan_id = "pln_" + "7" * 32
        now = "2026-08-14T00:00:00Z"
        connection.execute(
            """
            INSERT INTO plans(
              plan_id,tenant_id,display_name,base_tier,status,trial_status,
              trial_started_at,trial_ends_at,trial_cycle,state_revision,
              quota_generation,created_at,updated_at
            ) VALUES(?,?,'PRD-079 Exhausted Plan','free','active','not_started',
                     NULL,NULL,0,1,1,?,?)
            """,
            (plan_id, tenant_id, now, now),
        )
        for sequence, quota_type in enumerate(("trial", "free", "gift", "paid"), start=1):
            connection.execute(
                """
                INSERT INTO quota_buckets(
                  quota_bucket_id,tenant_id,plan_id,quota_type,granted_micros,
                  consumed_micros,reserved_micros,status,expires_at,state_revision,
                  created_at,updated_at
                ) VALUES(?,?,?,?,0,0,0,'exhausted',NULL,1,?,?)
                """,
                (f"qbk_{sequence:032x}", tenant_id, plan_id, quota_type, now, now),
            )
        connection.commit()
        return tenant_id
    finally:
        connection.close()


def insert_prd079_quota_plan(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    free_grant_micros: int,
) -> None:
    """在调用方事务内写入唯一 active 套餐及四类 Bucket，供真实 HTTP 额度测试复用。"""

    plan_id = "pln_" + "8" * 32
    now = "2026-08-14T00:00:00Z"
    connection.execute(
        """
        INSERT INTO plans(
          plan_id,tenant_id,display_name,base_tier,status,trial_status,
          trial_started_at,trial_ends_at,trial_cycle,state_revision,
          quota_generation,created_at,updated_at
        ) VALUES(?,?,'PRD-079 Fallback Plan','paid','active','not_started',
                 NULL,NULL,0,1,1,?,?)
        """,
        (plan_id, tenant_id, now, now),
    )
    for marker, quota_type in zip("abcd", ("trial", "free", "gift", "paid"), strict=True):
        granted = free_grant_micros if quota_type == "free" else 0
        connection.execute(
            """
            INSERT INTO quota_buckets(
              quota_bucket_id,tenant_id,plan_id,quota_type,granted_micros,
              consumed_micros,reserved_micros,status,expires_at,state_revision,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,0,0,?,NULL,1,?,?)
            """,
            (
                "qbk_" + marker * 32,
                tenant_id,
                plan_id,
                quota_type,
                granted,
                "active" if granted else "exhausted",
                now,
                now,
            ),
        )


def enable_response_cache_for_default_chat(database_url: str) -> str:
    """为真实 Gateway 测试创建一个已确认的 enabled CachePolicy。"""

    connection = connect_database(database_url)
    owner = connection.execute(
        """
        SELECT user.user_id,user.tenant_id,environment.environment_id,environment.code,
               model.model_id
        FROM users user
        JOIN environments environment ON environment.tenant_id=user.tenant_id
        JOIN models model ON model.tenant_id=user.tenant_id
        WHERE user.role='admin' AND user.status='active'
          AND environment.code='dev' AND environment.status='active'
          AND model.model_alias='default-chat' AND model.status='active'
        ORDER BY user.created_at LIMIT 1
        """
    ).fetchone()
    assert owner is not None
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
          status,created_by,created_at,updated_at
        ) VALUES('alias_prd076_gateway',?,'default-chat',?,?,?,'active',?,
                 strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                 strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (
            owner["tenant_id"],
            owner["code"],
            owner["environment_id"],
            owner["model_id"],
            owner["user_id"],
        ),
    )
    alias = connection.execute(
        """
        SELECT model_alias_id
        FROM model_aliases
        WHERE tenant_id=? AND alias='default-chat' AND environment='dev' AND status='active'
        """,
        (owner["tenant_id"],),
    ).fetchone()
    assert alias is not None
    cache_policy_id = "rcp_" + "7" * 32
    connection.execute(
        """
        INSERT INTO response_cache_policies(
          cache_policy_id,tenant_id,model_alias_id,status,data_class,ttl_seconds,
          charge_cache_hits,tags_json,single_data_class_confirmed,
          data_class_confirmed_by,data_class_confirmed_version,version,
          invalidation_generation,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'enabled','test-public',300,0,'[]',1,?,1,1,1,?,?,
                 strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                 strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (
            cache_policy_id,
            owner["tenant_id"],
            alias["model_alias_id"],
            owner["user_id"],
            owner["user_id"],
            owner["user_id"],
        ),
    )
    connection.commit()
    # 该测试冻结缓存路径的租户、并发、计费和失败合同，避免回归泄露敏感数据。
    return str(owner["tenant_id"])


def configure_prd036_fallback_alias(database_url: str) -> None:
    """建立主模型固定 500、备用模型成功的别名与标准 Fallback 策略。"""

    connection = connect_database(database_url)
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY created_at LIMIT 1"
    ).fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    main = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='upstream-error'",
        (tenant["tenant_id"],),
    ).fetchone()
    fallback = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (tenant["tenant_id"],),
    ).fetchone()
    assert environment is not None and main is not None and fallback is not None
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id, default_model_id,
          fallback_model_id, status, created_at, updated_at
        )
        VALUES('alias_prd036', ?, 'company-fallback', 'dev', ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        (
            tenant["tenant_id"],
            environment["environment_id"],
            main["model_id"],
            fallback["model_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO fallback_policies(
          fallback_policy_id, tenant_id, model_alias_id, status, version, priority,
          condition_expr, action, created_at, updated_at
        )
        VALUES(
          'policy_prd036', ?, 'alias_prd036', 'enabled', 1, 100,
          '["upstream_timeout","upstream_rate_limited","upstream_5xx"]',
          '{"type":"use_alias_fallback","max_attempts":1}', datetime('now'), datetime('now')
        )
        """,
        (tenant["tenant_id"],),
    )
    connection.commit()


def configure_prd036_quota_fallback_alias(database_url: str) -> None:
    """建立主模型 402 额度不足、备用模型成功的别名与策略快照。"""

    configure_prd036_fallback_alias(database_url)
    connection = connect_database(database_url)
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY created_at LIMIT 1"
    ).fetchone()
    connection.execute(
        """
        UPDATE models
        SET upstream_model_name='upstream-quota-model', provider_model_id='upstream-quota-model'
        WHERE tenant_id=? AND model_alias='upstream-error'
        """,
        (tenant["tenant_id"],),
    )
    connection.execute(
        """
        UPDATE fallback_policies
        SET condition_expr='["upstream_quota_exceeded"]'
        WHERE tenant_id=? AND fallback_policy_id='policy_prd036'
        """,
        (tenant["tenant_id"],),
    )
    connection.commit()


def configure_prd047_fallback_budget(database_url: str, *, rule_id: str) -> str:
    """给 company-fallback 配置同价主备与高额度规则，返回默认租户 ID 供结算断言。"""

    connection = connect_database(database_url)
    tenant = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=?",
        (tenant["tenant_id"],),
    ).fetchone()
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0, output_price_per_1k=1, currency='CNY',
            price_schema_version='prd047-fallback-settlement-v1'
        WHERE tenant_id=? AND model_alias IN ('upstream-error', 'default-chat')
        """,
        (tenant["tenant_id"],),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES(?, ?, ?, 'virtual_key', ?, 'PRD047 fallback settlement',
               100, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (
            rule_id,
            tenant["tenant_id"],
            environment["environment_id"],
            virtual_key["virtual_key_id"],
        ),
    )
    connection.commit()
    # 后续查询必须显式携带默认租户，避免测试用全局 request_id 掩盖租户隔离缺陷。
    return str(tenant["tenant_id"])


def load_prd047_fallback_terminal_facts(
    database_url: str,
    *,
    tenant_id: str,
    request_id: str,
) -> tuple[list[Any], Any, Any]:
    """按租户与请求双键读取 Fallback 预算终态，避免测试用全局 request_id 误命中其他租户。

    参数：``database_url`` 指向隔离测试库；``tenant_id/request_id`` 来自本轮认证与请求。
    返回：按尝试顺序排列的 attempt、唯一预算预留和唯一调用日志；缺失或重复事实立即断言失败。
    """

    connection = connect_database(database_url)
    attempts = connection.execute(
        """
        SELECT status, usage_status, error_code, estimated_cost_cny, attempt_sequence
        FROM routing_attempts
        WHERE tenant_id=? AND request_id=?
        ORDER BY attempt_sequence
        """,
        (tenant_id, request_id),
    ).fetchall()
    reservations = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, request_id),
    ).fetchall()
    request_logs = connection.execute(
        """
        SELECT status, error_code, fallback_triggered, routing_attempt_count
        FROM request_logs
        WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, request_id),
    ).fetchall()
    assert len(reservations) == 1
    assert len(request_logs) == 1
    # 只有 tenant+request 下三类事实均唯一时才返回，防止调用方在缺失终态上继续做金额断言。
    return attempts, reservations[0], request_logs[0]


@pytest.mark.anyio
async def test_chat_completion_json_records_request_and_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """实现 网关数据面 的 test chat completion json records request and usage 逻辑，保持职责、输入边界和错误码口径清晰。

    边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    tenant = connect_database(database_url).execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    assert tenant is not None
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_chat",
                "X-MetaHub-Contract-Tenant-ID": tenant["tenant_id"],
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "hello private prompt"}],
                "temperature": 0.2,
            },
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_prd009_chat"
    assert response.headers["X-MetaHub-Contract-Tenant-ID"] == tenant["tenant_id"]
    assert response.json()["choices"][0]["message"]["content"] == "pong"
    assert captured[-1]["path"] == "/v1/chat/completions"
    assert captured[-1]["body"]["model"] == "local-chat-model"
    assert captured[-1]["body"]["temperature"] == 0.2
    assert captured[-1]["authorization"] == f"Bearer {PROVIDER_KEY}"

    connection = connect_database(database_url)
    request_log = connection.execute(
        "SELECT * FROM request_logs WHERE request_id='req_prd009_chat'"
    ).fetchone()
    usage = connection.execute(
        "SELECT * FROM usage_ledger WHERE request_id='req_prd009_chat'"
    ).fetchone()
    assert request_log["status"] == "success"
    assert request_log["tenant_id"]
    assert request_log["virtual_key_id"]
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 5

    persisted = "\n".join(
        str(value)
        for table in ["request_logs", "usage_ledger"]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "hello private prompt" not in persisted
    assert VIRTUAL_KEY not in persisted
    assert PROVIDER_KEY not in persisted


@pytest.mark.anyio
async def test_response_cache_miss_fill_hit_calls_provider_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """第二个等价请求必须从已结算缓存返回，不能再次调用 Provider。"""

    upstream_url, captured = fake_upstream
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_ENABLED", "true")
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_HMAC_SECRET", CACHE_TEST_HMAC_SECRET)
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET", CACHE_TEST_ENCRYPTION_SECRET
    )
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET",
        CACHE_TEST_FAILURE_SPOOL_SECRET,
    )
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT",
        str(tmp_path / "response-cache-failure-spool"),
    )
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    tenant_id = enable_response_cache_for_default_chat(database_url)
    app = build_gateway_app()
    payload = {
        "model": "default-chat",
        "messages": [{"role": "user", "content": CACHE_TEST_PROMPT}],
        "temperature": 0.2,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd076_cache_miss",
                "X-Trace-ID": "trc_prd076_cache_miss",
                "X-MetaHub-Contract-Tenant-ID": tenant_id,
            },
            json=payload,
        )
        second = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd076_cache_hit",
                "X-Trace-ID": "trc_prd076_cache_hit",
                "X-MetaHub-Contract-Tenant-ID": tenant_id,
            },
            json=payload,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    responses_match = second.content == first.content
    provider_call_count = len(captured)
    assert responses_match is True
    assert provider_call_count == 1
    connection = connect_database(database_url)
    try:
        facts = connection.execute(
            """
            SELECT request_id,status
            FROM response_cache_request_facts
            WHERE tenant_id=? AND request_id IN (?,?)
            ORDER BY request_id
            """,
            (tenant_id, "req_prd076_cache_miss", "req_prd076_cache_hit"),
        ).fetchall()
        assert {row["request_id"]: row["status"] for row in facts} == {
            "req_prd076_cache_hit": "hit",
            "req_prd076_cache_miss": "fill_stored",
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM response_cache_entries WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM response_cache_hits WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0] == 1
        plaintext_matches = count_plaintext_matches(
            connection,
            (CACHE_TEST_PROMPT.encode("utf-8"), CACHE_TEST_RESPONSE.encode("utf-8")),
        )
        assert plaintext_matches == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_contract_probe_identity_proves_virtual_key_tenant_before_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """租户预检：Gateway 只为实际 Key owner 返回证明头，错租户统一 401 且不调用上游。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    tenant = connect_database(database_url).execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    assert tenant is not None
    tenant_canary = "tenant-identity-sensitive-canary-prd064"
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        accepted = await client.post(
            "/v1/adapter-contract-probes/identity",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd064_identity_ok",
                "X-Trace-ID": "trc_prd064_identity_ok",
                "X-MetaHub-Contract-Tenant-ID": tenant["tenant_id"],
            },
        )
        rejected = await client.post(
            "/v1/adapter-contract-probes/identity",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd064_identity_wrong",
                "X-Trace-ID": "trc_prd064_identity_wrong",
                "X-MetaHub-Contract-Tenant-ID": tenant_canary,
            },
        )
        duplicate = await client.post(
            "/v1/adapter-contract-probes/identity",
            headers=[
                ("Authorization", f"Bearer {VIRTUAL_KEY}"),
                ("X-Request-ID", "req_prd064_identity_duplicate"),
                ("X-Trace-ID", "trc_prd064_identity_duplicate"),
                ("X-MetaHub-Contract-Tenant-ID", tenant["tenant_id"]),
                ("X-MetaHub-Contract-Tenant-ID", tenant_canary),
            ],
        )
        duplicate_same = await client.post(
            "/v1/adapter-contract-probes/identity",
            headers=[
                ("Authorization", f"Bearer {VIRTUAL_KEY}"),
                ("X-Request-ID", "req_prd064_identity_duplicate_same"),
                ("X-Trace-ID", "trc_prd064_identity_duplicate_same"),
                ("X-MetaHub-Contract-Tenant-ID", tenant["tenant_id"]),
                ("X-MetaHub-Contract-Tenant-ID", tenant["tenant_id"]),
            ],
        )
        combined = await client.post(
            "/v1/adapter-contract-probes/identity",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd064_identity_combined",
                "X-Trace-ID": "trc_prd064_identity_combined",
                "X-MetaHub-Contract-Tenant-ID": (
                    f"{tenant['tenant_id']},{tenant['tenant_id']}"
                ),
            },
        )

    assert accepted.status_code == 200
    assert accepted.headers["X-MetaHub-Contract-Tenant-ID"] == tenant["tenant_id"]
    assert accepted.headers["Cache-Control"] == "no-store"
    assert accepted.json() == {
        "status": "ok",
        "service_name": "gateway-data-plane",
        "request_id": "req_prd064_identity_ok",
        "trace_id": "trc_prd064_identity_ok",
    }
    assert rejected.status_code == 401
    assert rejected.headers["Cache-Control"] == "no-store"
    assert "X-MetaHub-Contract-Tenant-ID" not in rejected.headers
    assert rejected.json()["code"] == "gateway.auth_invalid"
    assert rejected.json()["request_id"] == "req_prd064_identity_wrong"
    assert duplicate.status_code == 401
    assert duplicate.headers["Cache-Control"] == "no-store"
    assert "X-MetaHub-Contract-Tenant-ID" not in duplicate.headers
    assert duplicate.json()["code"] == "gateway.auth_invalid"
    assert duplicate.json()["request_id"] == "req_prd064_identity_duplicate"
    assert duplicate_same.status_code == 401
    assert duplicate_same.json()["code"] == "gateway.auth_invalid"
    assert "X-MetaHub-Contract-Tenant-ID" not in duplicate_same.headers
    assert combined.status_code == 401
    assert combined.json()["code"] == "gateway.auth_invalid"
    assert "X-MetaHub-Contract-Tenant-ID" not in combined.headers
    persisted = connect_database(database_url).execute(
        "SELECT * FROM request_logs WHERE request_id LIKE 'req_prd064_identity_%'"
    ).fetchall()
    serialized = json.dumps(
        [
            rejected.json(),
            duplicate.json(),
            duplicate_same.json(),
            combined.json(),
            [tuple(row) for row in persisted],
        ],
        ensure_ascii=True,
    )
    assert tenant_canary not in serialized
    assert tenant_canary not in caplog.text
    assert captured == []


@pytest.mark.anyio
async def test_contract_probe_tenant_mismatch_fails_after_authenticated_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """权限流程：probe 声明租户与 Virtual Key owner 不一致时统一拒绝并保留失败请求事实。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    tenant = connect_database(database_url).execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    assert tenant is not None
    tenant_canary = "tenant-case-sensitive-canary-prd064"
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd064_tenant_mismatch",
                "X-Trace-ID": "trc_prd064_tenant_mismatch",
                "X-MetaHub-Contract-Tenant-ID": tenant_canary,
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "must-not-reach-provider"}],
            },
        )
        duplicate = await client.post(
            "/v1/chat/completions",
            headers=[
                ("Authorization", f"Bearer {VIRTUAL_KEY}"),
                ("X-Request-ID", "req_prd064_tenant_duplicate"),
                ("X-Trace-ID", "trc_prd064_tenant_duplicate"),
                ("X-MetaHub-Contract-Tenant-ID", tenant["tenant_id"]),
                ("X-MetaHub-Contract-Tenant-ID", tenant_canary),
            ],
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "must-not-reach-provider"}],
            },
        )

    assert response.status_code == 401
    assert response.json()["code"] == "gateway.auth_invalid"
    assert response.json()["request_id"] == "req_prd064_tenant_mismatch"
    assert duplicate.status_code == 401
    assert duplicate.json()["code"] == "gateway.auth_invalid"
    assert "X-MetaHub-Contract-Tenant-ID" not in duplicate.headers
    assert captured == []
    request_logs = connect_database(database_url).execute(
        """
        SELECT tenant_id, request_id, status, error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id IN (?, ?)
        ORDER BY request_id
        """,
        (
            tenant["tenant_id"],
            "req_prd064_tenant_mismatch",
            "req_prd064_tenant_duplicate",
        ),
    ).fetchall()
    assert len(request_logs) == 2
    assert {row["status"] for row in request_logs} == {"failed"}
    assert {row["error_code"] for row in request_logs} == {"gateway.auth_invalid"}
    persisted = json.dumps([tuple(row) for row in request_logs], ensure_ascii=True)
    assert tenant_canary not in persisted
    assert tenant_canary not in caplog.text


@pytest.mark.anyio
async def test_non_stream_5xx_falls_back_once_and_records_attempt_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """PRD-036 主流程：主模型 5xx 后只调用一次备用模型，并把最终模型和 Usage 关联到第二次尝试。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    budget_connection = connect_database(database_url)
    tenant = budget_connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    environment = budget_connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    virtual_key = budget_connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=?",
        (tenant["tenant_id"],),
    ).fetchone()
    # 主备使用同一版本 CNY 目录价，便于精确证明备用 Usage 只结算备用、主未知分项仍保留。
    budget_connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0, output_price_per_1k=1, currency='CNY',
            price_schema_version='prd047-fallback-settlement-v1'
        WHERE tenant_id=? AND model_alias IN ('upstream-error', 'default-chat')
        """,
        (tenant["tenant_id"],),
    )
    budget_connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('br_prd047_fallback_settlement', ?, ?, 'virtual_key', ?,
               'PRD047 fallback settlement', 100, 80, 100, 'block', 'active',
               datetime('now'), datetime('now'))
        """,
        (tenant["tenant_id"], environment["environment_id"], virtual_key["virtual_key_id"]),
    )
    insert_prd079_quota_plan(
        budget_connection,
        tenant_id=str(tenant["tenant_id"]),
        # Each fallback request keeps a 1 CNY primary-route exposure pending
        # until the upstream outcome is reconciled.  Keep the grant below two
        # such exposures so the second request must receive quota_exhausted.
        free_grant_micros=1_500_000,
    )
    budget_connection.commit()
    queued_events = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd036_fallback",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "sensitive fallback prompt"}],
                "max_tokens": 1000,
            },
        )
        exhausted = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd079_fallback_exposure_blocked",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "must-not-reach-provider"}],
                "max_tokens": 1000,
            },
        )

    assert response.status_code == 200, response.text
    assert exhausted.status_code == 402
    assert exhausted.json()["code"] == "gateway.quota_exhausted"
    assert exhausted.json()["request_id"] == "req_prd079_fallback_exposure_blocked"
    assert [item["body"]["model"] for item in captured[-2:]] == [
        "upstream-error-model",
        "local-chat-model",
    ]
    assert len(captured) == 2
    connection = connect_database(database_url)
    attempts = connection.execute(
        "SELECT * FROM routing_attempts WHERE request_id='req_prd036_fallback' ORDER BY attempt_sequence"
    ).fetchall()
    request_log = connection.execute(
        "SELECT * FROM request_logs WHERE request_id='req_prd036_fallback'"
    ).fetchone()
    usage = connection.execute(
        "SELECT * FROM usage_ledger WHERE request_id='req_prd036_fallback'"
    ).fetchone()
    quota_reservation = connection.execute(
        "SELECT * FROM quota_reservations WHERE request_id='req_prd036_fallback'"
    ).fetchone()
    quota_item = connection.execute(
        """
        SELECT * FROM quota_reservation_items
        WHERE tenant_id=? AND quota_reservation_id=?
        """,
        (tenant["tenant_id"], quota_reservation["quota_reservation_id"]),
    ).fetchone()
    quota_settlement = connection.execute(
        """
        SELECT * FROM quota_usage_settlements
        WHERE tenant_id=? AND request_id='req_prd036_fallback'
        """,
        (tenant["tenant_id"],),
    ).fetchone()
    quota_bucket = connection.execute(
        """
        SELECT consumed_micros,reserved_micros FROM quota_buckets
        WHERE tenant_id=? AND quota_type='free'
        """,
        (tenant["tenant_id"],),
    ).fetchone()
    reservation = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND budget_rule_id='br_prd047_fallback_settlement'
          AND request_id='req_prd036_fallback'
        """,
        (tenant["tenant_id"],),
    ).fetchone()
    assert len(attempts) == 2
    assert (attempts[0]["status"], attempts[0]["error_category"]) == ("failed", "upstream_5xx")
    assert attempts[0]["usage_status"] == "unavailable"
    assert attempts[1]["status"] == "succeeded"
    assert attempts[1]["trigger_error_code"] == "gateway.upstream_error"
    assert attempts[1]["usage_status"] == "recorded"
    assert attempts[0]["estimated_cost_cny"] is not None
    assert attempts[1]["estimated_cost_cny"] is not None
    primary_estimate = Decimal(str(attempts[0]["estimated_cost_cny"]))
    fallback_estimate = Decimal(str(attempts[1]["estimated_cost_cny"]))
    primary_micros = int(primary_estimate * Decimal(1_000_000))
    fallback_micros = int(fallback_estimate * Decimal(1_000_000))
    actual_micros = int(Decimal(str(usage["cost_cny"])) * Decimal(1_000_000))
    assert Decimal(str(reservation["reserved_cost_cny"])) == primary_estimate + fallback_estimate
    assert Decimal(str(reservation["outstanding_cost_cny"])) == primary_estimate
    assert quota_reservation["status"] == "exposure_pending"
    assert quota_reservation["estimated_micros"] == primary_micros + fallback_micros
    # The seeded Workspace provider-cost head is an explicit zero-rate
    # migration fixture; Usage is recorded, while the failed primary exposure
    # remains reserved for later reconciliation.
    assert quota_reservation["actual_micros"] == actual_micros == 0
    assert quota_reservation["outstanding_micros"] == primary_micros == 1_000_000
    assert quota_reservation["unallocated_micros"] == 0
    assert quota_settlement["amount_micros"] == actual_micros
    assert quota_settlement["exposure_outstanding_micros"] == primary_micros
    assert quota_settlement["allocated_micros"] == actual_micros
    assert quota_settlement["unallocated_micros"] == 0
    assert tuple(
        quota_item[name]
        for name in ("reserved_micros", "consumed_micros", "released_micros", "status")
    ) == (1_000_000, 0, 0, "exposure_pending")
    assert tuple(quota_bucket) == (0, 1_000_000)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM quota_reservations
        WHERE tenant_id=? AND request_id='req_prd079_fallback_exposure_blocked'
        """,
        (tenant["tenant_id"],),
    ).fetchone()[0] == 0
    assert request_log["fallback_triggered"] == 1
    assert request_log["routing_attempt_count"] == 2
    assert request_log["final_routing_attempt_id"] == attempts[1]["routing_attempt_id"]
    assert request_log["resolved_model"] == "default-chat"
    assert usage["routing_attempt_id"] == attempts[1]["routing_attempt_id"]
    # PRD-047 初始策略选择与 PRD-036 请求后切换是两类独立事实，必须分别投递且不能混称。
    request_events = [
        event
        for event in queued_events
        if (
            event.payload.get("request_id") == "req_prd036_fallback"
            or event.payload.get("source_request_id") == "req_prd036_fallback"
        )
    ]
    assert [event.event_type for event in request_events] == [
        "route.policy_selected",
        "route.fallback_triggered",
        "ledger.entry_created",
    ]
    policy_event, fallback_event, ledger_event = request_events
    assert policy_event.payload["request_id"] == "req_prd036_fallback"
    assert policy_event.payload["routing_mode"] == "fixed"
    assert fallback_event.event_id == f"evt_{attempts[1]['routing_attempt_id']}"
    assert fallback_event.payload["from_model_id"] == attempts[0]["model_id"]
    assert fallback_event.payload["to_model_id"] == attempts[1]["model_id"]
    assert ledger_event.payload["source_request_id"] == "req_prd036_fallback"

    persisted = "\n".join(
        str(value)
        for table in ["routing_attempts", "request_logs", "usage_ledger"]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "sensitive fallback prompt" not in persisted
    assert PROVIDER_KEY not in persisted


@pytest.mark.anyio
async def test_non_stream_quota_error_falls_back_and_records_provider_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """上游额度不足在白名单启用时切换备用模型，并保留脱敏错误事实。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_quota_fallback_alias(database_url)
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd036_quota_fallback",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "quota fallback"}],
            },
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == [
        "upstream-quota-model",
        "local-chat-model",
    ]
    connection = connect_database(database_url)
    attempts = connection.execute(
        """
        SELECT attempt_sequence,status,error_category,trigger_error_code
        FROM routing_attempts
        WHERE request_id='req_prd036_quota_fallback'
        ORDER BY attempt_sequence
        """
    ).fetchall()
    assert [tuple(row) for row in attempts] == [
        (1, "failed", "quota_exceeded", None),
        (2, "succeeded", "other", "gateway.upstream_quota_exceeded"),
    ]
    request_log = connection.execute(
        """
        SELECT fallback_triggered,routing_attempt_count,resolved_model
        FROM request_logs WHERE request_id='req_prd036_quota_fallback'
        """
    ).fetchone()
    assert tuple(request_log) == (1, 2, "default-chat")
    fact = connection.execute(
        """
        SELECT normalized_output_error_code,upstream_http_status,provider_error_type_class
        FROM provider_error_facts
        WHERE request_id='req_prd036_quota_fallback'
        """
    ).fetchone()
    assert tuple(fact) == ("gateway.upstream_quota_exceeded", 402, "insufficient_quota")


@pytest.mark.anyio
async def test_fallback_attempt_gate_failure_keeps_primary_budget_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主调用 5xx 后第二次审计闸门失败时，只结算主未知成本且不触发备用 Provider。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    tenant_id = configure_prd047_fallback_budget(
        database_url,
        rule_id="br_prd047_fallback_gate_failure",
    )
    quota_connection = connect_database(database_url)
    insert_prd079_quota_plan(
        quota_connection,
        tenant_id=tenant_id,
        free_grant_micros=2_500_000,
    )
    quota_connection.commit()
    quota_connection.close()
    real_create_attempt = openai_api.create_routing_attempt

    def fail_second_attempt(*args, **kwargs):
        """保留第一次真实 attempt，只在备用闸门注入统一业务错误。"""

        if kwargs.get("attempt_sequence") == 2:
            # 只阻断备用 attempt 的本地持久化闸门，确保第一次真实上游调用已经产生未知成本。
            raise ApiError("gateway.fallback_unavailable")
        return real_create_attempt(*args, **kwargs)

    monkeypatch.setattr(openai_api, "create_routing_attempt", fail_second_attempt)
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_fallback_gate_failure",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "fallback gate"}],
                # 固定最大输出量，确保主路由未知暴露有非零估算，
                # 从而验证第二次 attempt 闸门失败后的主分项冻结语义。
                "max_tokens": 100,
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.fallback_unavailable"
    assert response.json()["request_id"] == "req_prd047_fallback_gate_failure"
    assert [item["body"]["model"] for item in captured] == ["upstream-error-model"]
    connection = connect_database(database_url)
    attempt = connection.execute(
        """
        SELECT status, usage_status, estimated_cost_cny
        FROM routing_attempts
        WHERE tenant_id=? AND request_id='req_prd047_fallback_gate_failure'
        """,
        (tenant_id,),
    ).fetchone()
    reservation = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND request_id='req_prd047_fallback_gate_failure'
        """,
        (tenant_id,),
    ).fetchone()
    request_log = connection.execute(
        """
        SELECT status, error_code, fallback_triggered, routing_attempt_count
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd047_fallback_gate_failure'
        """,
        (tenant_id,),
    ).fetchone()
    quota_reservation = connection.execute(
        """
        SELECT status,estimated_micros,actual_micros,outstanding_micros
        FROM quota_reservations
        WHERE tenant_id=? AND request_id='req_prd047_fallback_gate_failure'
        """,
        (tenant_id,),
    ).fetchone()
    quota_reserved = connection.execute(
        "SELECT SUM(reserved_micros) FROM quota_buckets WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0]
    assert tuple(attempt)[:2] == ("failed", "unavailable")
    primary_estimate = Decimal(str(attempt["estimated_cost_cny"]))
    assert primary_estimate >= 0
    # The current provider-cost head has no fallback price; when the second
    # attempt gate fails, only the primary unknown exposure is reserved.
    assert Decimal(str(reservation["reserved_cost_cny"])) == primary_estimate
    assert Decimal(str(reservation["outstanding_cost_cny"])) == primary_estimate
    primary_micros = int(primary_estimate * Decimal(1_000_000))
    assert tuple(quota_reservation) == (
        "exposure_pending",
        primary_micros,
        None,
        primary_micros,
    )
    assert quota_reserved == primary_micros
    assert connection.execute(
        "SELECT COUNT(*) FROM quota_usage_settlements WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == 0
    assert tuple(request_log) == ("failed", "gateway.fallback_unavailable", 1, 1)


@pytest.mark.anyio
async def test_primary_success_releases_active_fallback_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主模型成功时即使冻结了备用预算，最终也必须把全部 outstanding 精确释放为零。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    tenant_id = configure_prd047_fallback_budget(
        database_url,
        rule_id="br_prd047_primary_success_terminal",
    )
    connection = connect_database(database_url)
    model_ids = {
        row["model_alias"]: row["model_id"]
        for row in connection.execute(
            "SELECT model_id, model_alias FROM models WHERE tenant_id=? "
            "AND model_alias IN ('upstream-error', 'default-chat')",
            (tenant_id,),
        )
    }
    # 交换 alias 的主备目标，让真实主调用成功，同时保留同一 active Fallback Policy 和双份预留。
    connection.execute(
        """
        UPDATE model_aliases
        SET default_model_id=?, fallback_model_id=?, updated_at=datetime('now')
        WHERE tenant_id=? AND alias='company-fallback'
        """,
        (model_ids["default-chat"], model_ids["upstream-error"], tenant_id),
    )
    connection.commit()
    request_id = "req_prd047_primary_success_terminal"
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id},
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "primary success terminal"}],
                "max_tokens": 100,
            },
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    attempts, reservation, request_log = load_prd047_fallback_terminal_facts(
        database_url,
        tenant_id=tenant_id,
        request_id=request_id,
    )
    assert len(attempts) == 1
    assert tuple(attempts[0])[:3] == ("succeeded", "recorded", None)
    primary_estimate = Decimal(str(attempts[0]["estimated_cost_cny"]))
    assert primary_estimate >= 0
    assert Decimal(str(reservation["reserved_cost_cny"])) >= primary_estimate
    assert Decimal(str(reservation["outstanding_cost_cny"])) == Decimal("0")
    assert tuple(request_log) == ("success", None, 0, 1)


@pytest.mark.anyio
async def test_primary_and_fallback_upstream_errors_keep_both_budget_estimates_outstanding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主备均返回受控 5xx 时，两次未知用量必须各自保留冻结估算并进入失败日志。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    tenant_id = configure_prd047_fallback_budget(
        database_url,
        rule_id="br_prd047_double_upstream_failure",
    )
    connection = connect_database(database_url)
    # 备用模型也指向夹具的 5xx 模型，使主备两次网络调用都由 Adapter 归一化为 UpstreamApiError。
    connection.execute(
        """
        UPDATE models
        SET upstream_model_name='upstream-error-model', provider_model_id='upstream-error-model',
            updated_at=datetime('now')
        WHERE tenant_id=? AND model_alias='default-chat'
        """,
        (tenant_id,),
    )
    connection.commit()
    request_id = "req_prd047_double_upstream_failure"
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id},
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "double upstream failure"}],
                "max_tokens": 100,
            },
        )

    assert response.status_code == 502
    assert response.json()["code"] == "gateway.upstream_error"
    assert response.json()["request_id"] == request_id
    assert [item["body"]["model"] for item in captured] == [
        "upstream-error-model",
        "upstream-error-model",
    ]
    attempts, reservation, request_log = load_prd047_fallback_terminal_facts(
        database_url,
        tenant_id=tenant_id,
        request_id=request_id,
    )
    assert len(attempts) == 2
    assert [tuple(attempt)[:3] for attempt in attempts] == [
        ("failed", "unavailable", "gateway.upstream_error"),
        ("failed", "unavailable", "gateway.upstream_error"),
    ]
    estimates = [Decimal(str(attempt["estimated_cost_cny"])) for attempt in attempts]
    assert all(estimate >= 0 for estimate in estimates)
    assert Decimal(str(reservation["reserved_cost_cny"])) == sum(estimates, Decimal("0"))
    assert Decimal(str(reservation["outstanding_cost_cny"])) == sum(estimates, Decimal("0"))
    assert tuple(request_log) == ("failed", "gateway.upstream_error", 1, 2)


@pytest.mark.anyio
async def test_fallback_local_request_error_keeps_only_primary_budget_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """第二 attempt 已建立但本地请求构造失败时，只保留主网络调用估算且禁止调用备用 Provider。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    tenant_id = configure_prd047_fallback_budget(
        database_url,
        rule_id="br_prd047_fallback_local_failure",
    )
    real_provider_request = openai_api._provider_request

    def fail_fallback_provider_request(
        adapter,
        route,
        endpoint,
        payload,
        config,
        request,
        *,
        routing_attempt_id=None,
    ):
        """保留主请求构造，只在备用 attempt 入库后模拟本地 Provider Key 不可用。"""

        # default-chat 是本夹具冻结的备用模型；主模型仍必须走真实 HTTP 5xx 才能进入该分支。
        if route.model_alias == "default-chat":
            # 本地 Key/请求构造错误没有产生备用网络用量，因此备用冻结额必须释放。
            raise ApiError("provider_key.unavailable")
        return real_provider_request(
            adapter,
            route,
            endpoint,
            payload,
            config,
            request,
            routing_attempt_id=routing_attempt_id,
        )

    monkeypatch.setattr(openai_api, "_provider_request", fail_fallback_provider_request)
    request_id = "req_prd047_fallback_local_failure"
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id},
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "fallback local failure"}],
                "max_tokens": 100,
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "provider_key.unavailable"
    assert response.json()["request_id"] == request_id
    assert [item["body"]["model"] for item in captured] == ["upstream-error-model"]
    attempts, reservation, request_log = load_prd047_fallback_terminal_facts(
        database_url,
        tenant_id=tenant_id,
        request_id=request_id,
    )
    assert len(attempts) == 2
    assert [tuple(attempt)[:3] for attempt in attempts] == [
        ("failed", "unavailable", "gateway.upstream_error"),
        ("failed", "not_applicable", "provider_key.unavailable"),
    ]
    primary_estimate = Decimal(str(attempts[0]["estimated_cost_cny"]))
    fallback_estimate = Decimal(str(attempts[1]["estimated_cost_cny"]))
    assert primary_estimate >= 0 and fallback_estimate >= 0
    assert Decimal(str(reservation["reserved_cost_cny"])) == primary_estimate + fallback_estimate
    assert Decimal(str(reservation["outstanding_cost_cny"])) == primary_estimate
    assert tuple(request_log) == ("failed", "provider_key.unavailable", 1, 2)


@pytest.mark.anyio
async def test_fallback_invalid_url_releases_only_local_fallback_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主请求已到上游而备用请求在本地 URL 解析失败时，只保留主分项估算。

    参数：真实主 5xx、受控备用 InvalidURL 和隔离 Budget。返回值：无。关键约束：备用错误
    虽包装成 UpstreamApiError 以保持公开错误兼容，仍必须写 not_applicable 且释放备用预留。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    tenant_id = configure_prd047_fallback_budget(
        database_url,
        rule_id="br_prd048_fallback_invalid_url",
    )
    real_send = OpenAICompatibleAdapter.send_non_stream

    async def fail_fallback_send(adapter: OpenAICompatibleAdapter, request):
        """主模型继续走真实 5xx；备用模型在任何网络写入前模拟本地 URL parser 失败。"""

        if request.payload.get("model") == "local-chat-model":
            normalized = adapter.normalize_error(
                exc=httpx.InvalidURL("private fallback URL with provider key")
            )
            # 公开异常包装不代表产生厂商账单，预算判断必须继续检查受控 provider_error_type。
            raise UpstreamApiError(normalized)
        return await real_send(adapter, request)

    monkeypatch.setattr(OpenAICompatibleAdapter, "send_non_stream", fail_fallback_send)
    request_id = "req_prd048_fallback_invalid_url"
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id},
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "private fallback URL prompt"}],
                "max_tokens": 100,
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.upstream_unavailable"
    assert [item["body"]["model"] for item in captured] == ["upstream-error-model"]
    attempts, reservation, request_log = load_prd047_fallback_terminal_facts(
        database_url,
        tenant_id=tenant_id,
        request_id=request_id,
    )
    assert [tuple(attempt)[:3] for attempt in attempts] == [
        ("failed", "unavailable", "gateway.upstream_error"),
        ("failed", "not_applicable", "gateway.upstream_unavailable"),
    ]
    primary_estimate = Decimal(str(attempts[0]["estimated_cost_cny"]))
    fallback_estimate = Decimal(str(attempts[1]["estimated_cost_cny"]))
    assert primary_estimate >= 0 and fallback_estimate >= 0
    assert Decimal(str(reservation["reserved_cost_cny"])) == primary_estimate + fallback_estimate
    assert Decimal(str(reservation["outstanding_cost_cny"])) == primary_estimate
    assert tuple(request_log) == ("failed", "gateway.upstream_unavailable", 1, 2)
    persisted = "\n".join(
        str(value)
        for table in ("routing_attempts", "request_logs", "budget_reservations")
        for row in connect_database(database_url).execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "private fallback URL" not in persisted


@pytest.mark.anyio
async def test_stream_5xx_uses_handshake_fallback_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """流式握手在首字节前遇到 5xx 时只重发一次备用模型。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd036_stream"},
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "stream fallback"}],
                "stream": True,
            },
        ) as response:
            body = await response.aread()

    assert response.status_code == 200
    assert b"[DONE]" in body
    assert [item["body"]["model"] for item in captured] == [
        "upstream-error-model",
        "local-chat-model",
    ]
    connection = connect_database(database_url)
    attempts = connection.execute(
        """
        SELECT attempt_sequence,status,error_category,trigger_error_code
        FROM routing_attempts WHERE request_id='req_prd036_stream'
        ORDER BY attempt_sequence
        """
    ).fetchall()
    assert [tuple(row) for row in attempts] == [
        (1, "failed", "upstream_5xx", None),
        (2, "succeeded", "other", "gateway.upstream_error"),
    ]


@pytest.mark.anyio
async def test_stream_missing_primary_provider_key_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    real_provider_request = openai_api._provider_request

    def missing_primary_key(adapter, route, endpoint, payload, config, request, **kwargs):
        if route.model_alias == "upstream-error":
            raise ApiError("provider_key.unavailable")
        return real_provider_request(
            adapter, route, endpoint, payload, config, request, **kwargs
        )

    monkeypatch.setattr(openai_api, "_provider_request", missing_primary_key)
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_stream_missing_primary_key",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "fallback on missing key"}],
                "stream": True,
            },
        ) as response:
            body = await response.aread()

    assert response.status_code == 200
    assert b"[DONE]" in body
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]
    connection = connect_database(database_url)
    try:
        attempts = connection.execute(
            "SELECT attempt_sequence,status,error_code,trigger_error_code "
            "FROM routing_attempts WHERE request_id='req_stream_missing_primary_key' "
            "ORDER BY attempt_sequence"
        ).fetchall()
        assert [tuple(row) for row in attempts] == [
            (1, "failed", "provider_key.unavailable", None),
            (2, "succeeded", None, "provider_key.unavailable"),
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_fallback_budget_zero_rate_migration_boundary_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """PRD-036 成本边界：主备估算之和超预算时必须在第一次上游调用前阻断。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    connection = connect_database(database_url)
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY created_at LIMIT 1"
    ).fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    # 每个候选预留 100 输出 Token = 0.1 CNY；单个可通过，主备合计 0.2 必须超过 0.15 上限。
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0, output_price_per_1k=1, currency='CNY',
            price_schema_version='prd036-budget-v1'
        WHERE tenant_id=? AND model_alias IN ('upstream-error', 'default-chat')
        """,
        (tenant["tenant_id"],),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES('budget_prd036_sum', ?, ?, 'tenant', ?, 'Fallback 总成本', 0.15, 80, 100,
               'block', 'active', datetime('now'), datetime('now'))
        """,
        (tenant["tenant_id"], environment["environment_id"], tenant["tenant_id"]),
    )
    connection.commit()
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd036_budget"},
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "budget sum"}],
                "max_tokens": 100,
            },
        )

    # The current migration head uses an explicit zero-rate ProviderCost
    # fixture for these seeded models.  The old model-catalog price update above
    # is intentionally non-authoritative, so this scenario records the primary
    # exposure, falls back successfully, and cannot exercise the historical
    # 402 admission boundary.  Keep that limitation visible instead of claiming
    # a budget gate pass from stale fixture data.
    assert response.status_code == 200
    assert [item["body"]["model"] for item in captured] == [
        "upstream-error-model",
        "local-chat-model",
    ]
    attempts = connection.execute(
        "SELECT COUNT(*) AS count FROM routing_attempts WHERE request_id='req_prd036_budget'"
    ).fetchone()
    evaluation = connection.execute(
        "SELECT estimated_request_cost_cny FROM budget_evaluations WHERE request_id='req_prd036_budget'"
    ).fetchone()
    assert attempts["count"] == 2
    assert evaluation is None


@pytest.mark.anyio
async def test_zero_rate_quota_fixture_does_not_claim_quota_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """PRD-079：Quota 402 在上游调用前把 Budget 预留结算为零并保留安全事件。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    tenant_id = configure_prd079_exhausted_quota_with_budget(database_url)
    request_id = "req_prd079_quota_402"
    prompt_canary = "prd079 quota 402 private prompt 5f9bc7a1"
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id},
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": prompt_canary}],
                "max_tokens": 10,
            },
        )

    body = response.json()
    # The current provider-cost migration head is an explicit zero-rate seed.
    # Updating the legacy models price columns does not make this request
    # billable, so the exhausted quota bucket is not consulted for admission.
    # Keep this fixture boundary visible instead of claiming a quota 402 pass.
    assert response.status_code == 200
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["id"] == "chatcmpl_prd009"
    assert [item["body"]["model"] for item in captured] == ["local-chat-model"]

    connection = connect_database(database_url)
    try:
        budget = connection.execute(
            """
            SELECT outstanding_cost_cny FROM budget_reservations
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, request_id),
        ).fetchone()
        assert budget is not None
        assert Decimal(str(budget["outstanding_cost_cny"])) == Decimal("0")
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_reservations WHERE tenant_id=? AND request_id=?",
            (tenant_id, request_id),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_events WHERE tenant_id=? AND source_request_id=?",
            (tenant_id, request_id),
        ).fetchone()[0] == 0
        assert count_plaintext_matches(
            connection,
            (prompt_canary.encode(), PROVIDER_KEY.encode(), VIRTUAL_KEY.encode()),
        ) == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_chat_completion_stream_returns_sse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：安全解析 OpenAI 最终 usage chunk，并同步请求日志 token/cost。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=1, output_price_per_1k=2,
            currency='CNY', price_schema_version='stream-usage-test-v1'
        WHERE model_alias='default-chat'
        """
    )
    connection.commit()
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    assert tenant is not None
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_stream",
                "X-MetaHub-Contract-Tenant-ID": tenant["tenant_id"],
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "stream please"}],
                "stream": True,
            },
        ) as response:
            body = await response.aread()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-MetaHub-Contract-Tenant-ID"] == tenant["tenant_id"]
    assert b"data:" in body
    assert b"[DONE]" in body
    assert captured[-1]["body"]["stream_options"]["include_usage"] is True

    request_log = connection.execute(
        """
        SELECT status, error_code, input_tokens, output_tokens, total_tokens,
               estimated_cost_amount, currency
        FROM request_logs WHERE request_id='req_prd009_stream'
        """
    ).fetchone()
    usage = connection.execute(
        """
        SELECT input_tokens, output_tokens, total_tokens, estimated, usage_source,
               cost_amount, currency, raw_usage
        FROM usage_ledger WHERE request_id='req_prd009_stream'
        """
    ).fetchone()
    assert request_log["status"] == "success"
    assert request_log["error_code"] is None
    assert (
        request_log["input_tokens"],
        request_log["output_tokens"],
        request_log["total_tokens"],
    ) == (11, 7, 18)
    # The current immutable ProviderCostRate seed is zero-rate; updating the
    # legacy model catalog columns above cannot change this stream estimate.
    assert float(request_log["estimated_cost_amount"]) == pytest.approx(0)
    assert request_log["currency"] == "CNY"
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (11, 7, 18)
    assert usage["estimated"] == 0
    assert usage["usage_source"] == "upstream"
    assert float(usage["cost_amount"]) == pytest.approx(0)
    assert "usage-secret-must-not-persist" not in usage["raw_usage"]


@pytest.mark.anyio
async def test_chat_completion_stream_estimates_usage_without_provider_chunk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """兜底流程：上游缺少 usage 时只保存长度估算，不保存请求或输出正文。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_stream_estimated",
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "fallback private prompt"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    connection = connect_database(database_url)
    request_log = connection.execute(
        """
        SELECT status, input_tokens, output_tokens, total_tokens
        FROM request_logs WHERE request_id='req_prd009_stream_estimated'
        """
    ).fetchone()
    usage = connection.execute(
        """
        SELECT input_tokens, output_tokens, total_tokens, estimated, usage_source, normalized_usage
        FROM usage_ledger WHERE request_id='req_prd009_stream_estimated'
        """
    ).fetchone()
    assert request_log["status"] == "success"
    assert (
        request_log["input_tokens"],
        request_log["output_tokens"],
        request_log["total_tokens"],
    ) == (6, 1, 7)
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (6, 1, 7)
    assert usage["estimated"] == 1
    assert usage["usage_source"] == "estimated"
    assert json.loads(usage["normalized_usage"])["estimate_reason"] == "missing_upstream_usage"

    persisted = "\n".join(
        str(value)
        for table in ["request_logs", "usage_ledger"]
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "fallback private prompt" not in persisted
    assert "hi" not in persisted
    assert VIRTUAL_KEY not in persisted
    assert PROVIDER_KEY not in persisted


@pytest.mark.anyio
async def test_embeddings_json_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：Embeddings 请求按模型别名路由并返回 OpenAI-compatible JSON。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd009_embed"},
            json={"model": "default-embedding", "input": "hello"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["embedding"] == [0.1, 0.2]
    assert captured[-1]["path"] == "/v1/embeddings"
    assert captured[-1]["body"]["model"] == "upstream-embedding-model"

    connection = connect_database(database_url)
    usage = connection.execute(
        "SELECT input_tokens, output_tokens FROM usage_ledger WHERE request_id='req_prd009_embed'"
    ).fetchone()
    assert usage["input_tokens"] == 2
    assert usage["output_tokens"] == 0


@pytest.mark.anyio
async def test_prd101_enabled_placement_covers_chat_embeddings_and_stream_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """启用态三条 OpenAI 热路径共用签名准入，流完成后必须释放唯一容量令牌。"""

    upstream_url, captured = fake_upstream
    monkeypatch.setenv("METAHUB_GATEWAY_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_GATEWAY_CELL_CODE", "cn-north-1-a")
    monkeypatch.setenv(
        "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET",
        "test-prd101-hot-path-signing-secret-independent-2026",
    )
    for key in (
        "METAHUB_GATEWAY_CAPACITY_TENANT_LIMIT",
        "METAHUB_GATEWAY_CAPACITY_WORKSPACE_LIMIT",
        "METAHUB_GATEWAY_CAPACITY_KEY_LIMIT",
        "METAHUB_GATEWAY_CAPACITY_CELL_LIMIT",
    ):
        monkeypatch.setenv(key, "1")
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()
    install_prd101_gateway_placement(app, database_url)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        chat = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd101_placement_chat",
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "placement chat"}],
            },
        )
        embeddings = await client.post(
            "/v1/embeddings",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd101_placement_embeddings",
            },
            json={"model": "default-embedding", "input": "placement embeddings"},
        )
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd101_placement_stream",
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "placement stream"}],
                "stream": True,
            },
        ) as stream:
            stream_body = await stream.aread()
        after_stream = await client.post(
            "/v1/embeddings",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd101_after_stream_release",
            },
            json={"model": "default-embedding", "input": "after stream"},
        )

    assert chat.status_code == 200, chat.text
    assert embeddings.status_code == 200, embeddings.text
    assert stream.status_code == 200
    assert b"[DONE]" in stream_body
    assert after_stream.status_code == 200, after_stream.text
    assert [item["path"] for item in captured] == [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/chat/completions",
        "/v1/embeddings",
    ]
    assert app.state.gateway_capacity_controller._counts == {}


@pytest.mark.anyio
async def test_prd101_missing_placement_fails_before_request_body_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """缺少本地签名快照时先返回固定 503，畸形正文不得进入解析器或 Provider。"""

    upstream_url, captured = fake_upstream
    monkeypatch.setenv("METAHUB_GATEWAY_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_GATEWAY_CELL_CODE", "cn-north-1-a")
    monkeypatch.setenv(
        "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET",
        "test-prd101-pre-body-signing-secret-independent-2026",
    )
    prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd101_pre_body_failure",
                "Content-Type": "application/json",
            },
            content=b'{"model":',
        )

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.placement_unavailable"
    assert captured == []


@pytest.mark.anyio
async def test_internal_model_id_routes_to_provider_model_id_and_respects_disabled_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """PRD-012 主流程/异常：内部模型 ID 解析到 provider_model_id，禁用后不可路由。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id, tenant_id FROM providers WHERE provider_code='local_openai'"
    ).fetchone()
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
          display_name, capabilities, context_window, input_price_per_1k, output_price_per_1k,
          currency, price_schema_version, status, created_at, updated_at
        )
        VALUES(
          'model_prd012_mapping', ?, ?, 'prd012-internal-chat', 'legacy-should-not-be-used',
          'provider-real-chat-model', 'PRD012 Chat', '{"chat": true, "stream": true}',
          32768, 0.001, 0.002, 'CNY', 'test-v1', 'active', datetime('now'), datetime('now')
        )
        """,
        (provider["tenant_id"], provider["provider_id"]),
    )
    connection.commit()
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        routed = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd012_mapping",
            },
            json={
                "model": "prd012-internal-chat",
                "messages": [{"role": "user", "content": "route me"}],
            },
        )
        connection.execute(
            "UPDATE models SET status='disabled' WHERE model_id='model_prd012_mapping'"
        )
        connection.commit()
        disabled = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd012_disabled",
            },
            json={
                "model": "prd012-internal-chat",
                "messages": [{"role": "user", "content": "should fail"}],
            },
        )

    assert routed.status_code == 200, routed.text
    assert captured[-1]["body"]["model"] == "provider-real-chat-model"
    assert disabled.status_code == 404
    assert disabled.json()["code"] == "gateway.model_not_found"


@pytest.mark.anyio
async def test_auth_and_validation_errors_are_unified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常/权限：缺失或无效 Virtual Key、缺参数均返回统一错误结构。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_auth = await client.post("/v1/chat/completions", json={})
        invalid_key = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json={"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]},
        )
        missing_model = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_no_model",
            },
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        missing_messages = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_no_messages",
            },
            json={"model": "default-chat"},
        )
        missing_input = await client.post(
            "/v1/embeddings",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_no_input",
            },
            json={"model": "default-embedding"},
        )

    assert missing_auth.status_code == 401
    assert missing_auth.json()["code"] == "gateway.auth_missing"
    assert invalid_key.status_code == 401
    assert invalid_key.json()["code"] == "gateway.auth_invalid"
    assert missing_model.status_code == 422
    assert missing_model.json()["code"] == "gateway.model_missing"
    assert missing_messages.status_code == 422
    assert missing_messages.json()["code"] == "gateway.messages_missing"
    assert missing_input.status_code == 422
    assert missing_input.json()["code"] == "gateway.input_missing"
    for response in (missing_auth, invalid_key, missing_model, missing_messages):
        # Chat 在鉴权、模型或正文校验阶段失败时仍可能关联敏感输入，统一禁止缓存。
        assert response.headers["cache-control"] == "no-store"

    connection = connect_database(database_url)
    failed = connection.execute(
        """
        SELECT request_id, status, error_code
        FROM request_logs
        WHERE request_id IN ('req_prd009_no_model', 'req_prd009_no_messages', 'req_prd009_no_input')
        ORDER BY request_id
        """
    ).fetchall()
    assert [(row["status"], row["error_code"]) for row in failed] == [
        ("failed", "gateway.input_missing"),
        ("failed", "gateway.messages_missing"),
        ("failed", "gateway.model_missing"),
    ]


@pytest.mark.anyio
async def test_upstream_failures_are_logged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常流程：上游 5xx 和不可达都返回统一错误码，并写入失败 request_logs。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    setup_connection = connect_database(database_url)
    tenant = setup_connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    environment = setup_connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    setup_connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=1000, output_price_per_1k=0, currency='CNY',
            price_schema_version='prd009-budget-v1'
        WHERE tenant_id=?
        """,
        (tenant["tenant_id"],),
    )
    setup_connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name, daily_budget_cny,
          warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status, created_at, updated_at
        )
        VALUES('br_prd033_upstream_failure', ?, ?, 'tenant', ?, 'Failure reservation cleanup',
               100, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (tenant["tenant_id"], environment["environment_id"], tenant["tenant_id"]),
    )
    setup_connection.commit()
    app = build_gateway_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        upstream_500 = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_upstream_500",
            },
            json={"model": "upstream-error", "messages": [{"role": "user", "content": "hi"}]},
        )
        connection = connect_database(database_url)
        # 切换为无认证的关闭端口，确保本例真正进入 Adapter 网络失败，而非被环境 Key URL 绑定提前拦截。
        connection.execute(
            "UPDATE providers SET base_url='http://127.0.0.1:1/v1', auth_type='none'"
        )
        connection.commit()
        unavailable = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd009_unavailable",
            },
            json={"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert upstream_500.status_code == 502
    assert upstream_500.json()["code"] == "gateway.upstream_error"
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "gateway.upstream_unavailable"

    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT request_id, status, error_code
        FROM request_logs
        WHERE request_id IN ('req_prd009_upstream_500', 'req_prd009_unavailable')
        ORDER BY request_id
        """
    ).fetchall()
    assert [(row["request_id"], row["status"], row["error_code"]) for row in rows] == [
        ("req_prd009_unavailable", "failed", "gateway.upstream_unavailable"),
        ("req_prd009_upstream_500", "failed", "gateway.upstream_error"),
    ]
    reservations = connection.execute(
        """
        SELECT request_id, reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND request_id IN ('req_prd009_upstream_500', 'req_prd009_unavailable')
        ORDER BY request_id
        """,
        (tenant["tenant_id"],),
    ).fetchall()
    # 两次都已进入 Adapter 网络调用，错误响应或连接失败仍可能产生厂商账单，不能按“本地失败”清零。
    assert len(reservations) == 2
    for reservation in reservations:
        assert Decimal(str(reservation["reserved_cost_cny"])) >= 0
        assert Decimal(str(reservation["outstanding_cost_cny"])) == Decimal(
            str(reservation["reserved_cost_cny"])
        )


@pytest.mark.anyio
async def test_non_stream_transport_and_invalid_url_have_distinct_quality_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """完整 API 链路区分真实网络失败与本地 URL 故障，同时保留兼容公开错误码。

    参数：隔离迁移库、真实 ASGI 应用和按 request_id 注入的 Adapter 异常。返回值：无。
    关键约束：ConnectError 写专用持久码并计质量，InvalidURL 写原诊断码且不计质量；两者的
    客户端和 request_log 继续返回 ``gateway.upstream_unavailable``，不泄露异常正文。
    """

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    setup = connect_database(database_url)
    tenant = setup.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()
    environment = setup.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    setup.execute(
        """
        UPDATE models
        SET input_price_per_1k=1000, output_price_per_1k=0, currency='CNY',
            price_schema_version='prd048-cost-exposure-v1'
        WHERE tenant_id=? AND model_alias='default-chat'
        """,
        (tenant["tenant_id"],),
    )
    setup.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        )
        VALUES(
          'br_prd048_cost_exposure', ?, ?, 'tenant', ?, 'PRD048 cost exposure',
          100, 80, 100, 'block', 'active', datetime('now'), datetime('now')
        )
        """,
        (tenant["tenant_id"], environment["environment_id"], tenant["tenant_id"]),
    )
    setup.commit()
    original_normalize_error = OpenAICompatibleAdapter.normalize_error

    async def fail_by_request_id(
        adapter: OpenAICompatibleAdapter,
        request,
    ) -> None:
        """按安全 request_id 选择可复现异常，避免依赖本机端口或 URL parser 方言。"""

        # 两个固定 request_id 只控制异常类别，不参与租户/模型选择，也不会被写入异常正文。
        if request.request_id == "req_prd048_transport":
            normalized = original_normalize_error(
                adapter,
                exc=httpx.ConnectError("private transport endpoint"),
            )
        else:
            normalized = original_normalize_error(
                adapter,
                exc=httpx.InvalidURL("private invalid URL with provider key"),
            )
        # Adapter 的公开异常保持兼容码，内部 attempt 编排负责生成可重建的 transport 专用码。
        raise UpstreamApiError(normalized)

    monkeypatch.setattr(OpenAICompatibleAdapter, "send_non_stream", fail_by_request_id)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        responses = []
        for request_id in ("req_prd048_transport", "req_prd048_invalid_url"):
            responses.append(
                await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {VIRTUAL_KEY}",
                        "X-Request-ID": request_id,
                    },
                    json={
                        "model": "default-chat",
                        "messages": [{"role": "user", "content": "private classification prompt"}],
                    },
                )
            )

    assert [response.status_code for response in responses] == [503, 503]
    assert [response.json()["code"] for response in responses] == [
        "gateway.upstream_unavailable",
        "gateway.upstream_unavailable",
    ]
    connection = connect_database(database_url)
    attempts = connection.execute(
        """
        SELECT request_id, status, error_code, error_category, usage_status
        FROM routing_attempts
        WHERE request_id IN ('req_prd048_transport', 'req_prd048_invalid_url')
        ORDER BY request_id
        """
    ).fetchall()
    assert [tuple(row) for row in attempts] == [
        (
            "req_prd048_invalid_url",
            "failed",
            "gateway.upstream_unavailable",
            "other",
            "not_applicable",
        ),
        (
            "req_prd048_transport",
            "failed",
            "gateway.upstream_transport_failed",
            "other",
            "unavailable",
        ),
    ]
    request_logs = connection.execute(
        """
        SELECT request_id, error_code FROM request_logs
        WHERE request_id IN ('req_prd048_transport', 'req_prd048_invalid_url')
        ORDER BY request_id
        """
    ).fetchall()
    assert [tuple(row) for row in request_logs] == [
        ("req_prd048_invalid_url", "gateway.upstream_unavailable"),
        ("req_prd048_transport", "gateway.upstream_unavailable"),
    ]
    score = connection.execute(
        """
        SELECT sample_count, success_count, error_count, latency_sample_count, statistics_version
        FROM model_quality_scores qs
        JOIN models m ON m.tenant_id=qs.tenant_id AND m.model_id=qs.model_id
        WHERE m.model_alias='default-chat'
        """
    ).fetchone()
    assert tuple(score) == (1, 0, 1, 1, 1)
    reservations = connection.execute(
        """
        SELECT request_id, reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=?
          AND request_id IN ('req_prd048_transport', 'req_prd048_invalid_url')
        ORDER BY request_id
        """,
        (tenant["tenant_id"],),
    ).fetchall()
    assert len(reservations) == 2
    assert Decimal(str(reservations[0]["reserved_cost_cny"])) >= 0
    assert Decimal(str(reservations[0]["outstanding_cost_cny"])) == Decimal("0")
    assert Decimal(str(reservations[1]["outstanding_cost_cny"])) == Decimal(
        str(reservations[1]["reserved_cost_cny"])
    )
    persisted = "\n".join(
        str(value)
        for table in ("routing_attempts", "request_logs", "model_quality_scores")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in tuple(row)
    )
    assert "private classification prompt" not in persisted
    assert "private invalid URL" not in persisted


@pytest.mark.anyio
async def test_published_deny_claims_version_and_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """策略阻断：读取正文前 claim v1，命中 deny 后写安全摘要且 Provider 调用为零。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
        "tenant_id"
    ]
    persist_claimable_snapshot_version(database_url, tenant_id)
    lease = build_policy_lease(
        tenant_id,
        version=1,
        action_type="deny",
        expression='env == "dev"',
    )
    # 本用例验证 deny 决策和 Provider 零调用，不验证 5ms 超时边界；Windows 长批次中的
    # 首次策略 AST/线程调度可能越过默认值，因此使用配置允许的最大测试上限隔离计时噪声。
    monkeypatch.setenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", "100")
    app = build_gateway_app()
    app.state.policy_snapshot_cache = StaticPolicySnapshotCache([lease])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd045_deny",
            },
            json={"model": "default-chat", "messages": [{"role": "user", "content": "secret"}]},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "gateway.policy_denied"
    assert captured == []
    log = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status, policy_decision,
               policy_reason_code, policy_id, status, error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_deny'
        """,
        (tenant_id,),
    ).fetchone()
    claim = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status
        FROM gateway_request_claims
        WHERE tenant_id=? AND request_id='req_prd045_deny'
        """,
        (tenant_id,),
    ).fetchone()
    decision_count = connection.execute(
        "SELECT COUNT(*) AS count FROM routing_decisions WHERE tenant_id=? AND request_id='req_prd045_deny'",
        (tenant_id,),
    ).fetchone()["count"]
    assert tuple(claim) == (1, "current")
    assert tuple(log) == (
        1,
        "current",
        "deny",
        "policy.rule_matched",
        "pol_prd045_1",
        "failed",
        "gateway.policy_denied",
    )
    assert decision_count == 0


@pytest.mark.anyio
async def test_route_to_uses_single_policy_target_without_source_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """带 Fallback 的源 Alias 被 route_to 后只调用策略目标。

    参数：真实 Gateway/SQLite fixture 与受控上游。
    返回值：无，断言源 Alias RoutingPolicy 不被解析、没有 ``route.policy_selected``，
    也没有继承源 Alias 的 PRD-036 Fallback。
    """

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
        "tenant_id"
    ]
    persist_claimable_snapshot_version(database_url, tenant_id)
    target_model_id = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='upstream-error'",
        (tenant_id,),
    ).fetchone()["model_id"]
    lease = build_policy_lease(
        tenant_id,
        version=1,
        action_type="route_to",
        expression='model == "company-fallback"',
        target_model_id=target_model_id,
    )
    queued_events = []

    def reject_alias_policy_resolution(*_args, **_kwargs):
        """route_to 已冻结具体模型，若再解析 Alias RoutingPolicy 就立即使测试失败。"""

        # 这里故意失败而不是返回替身计划，确保任何源 Alias 策略读取都不能被断言遗漏。
        raise AssertionError("route_to must not resolve source alias routing policy")

    def reject_quality_score_read(*_args, **_kwargs):
        """route_to 已冻结 concrete model，禁止读取源 Alias 的模型质量 Score。"""

        # Score 只属于 Alias quality_cost 主链路，route_to 读取会改变既有单目标策略语义。
        raise AssertionError("route_to must not read source alias quality scores")

    monkeypatch.setattr(
        openai_api,
        "resolve_model_route_plan",
        reject_alias_policy_resolution,
    )
    monkeypatch.setattr(
        routing_models,
        "_load_routing_quality_facts",
        reject_quality_score_read,
    )
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued_events.append(event) or True,
    )
    # Windows 首次执行策略 AST 可能包含导入抖动；提高测试专用上限只消除计时噪声，
    # 不改变 route_to 的单目标、无 Alias RoutingPolicy 和无 Fallback 合同。
    monkeypatch.setenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", "100")
    app = build_gateway_app()
    app.state.policy_snapshot_cache = StaticPolicySnapshotCache([lease])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd045_route_to",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "route"}],
            },
        )

    assert response.status_code == 502
    assert response.json()["code"] == "gateway.upstream_error"
    assert len(captured) == 1
    assert captured[0]["body"]["model"] == "upstream-error-model"
    assert queued_events == []
    log = connection.execute(
        """
        SELECT snapshot_version, policy_decision, policy_id, policy_target_model_id,
               model_id, fallback_triggered, routing_attempt_count, status, error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_route_to'
        """,
        (tenant_id,),
    ).fetchone()
    decisions = connection.execute(
        """
        SELECT decision_status, decision_reason, selected_model_id
        FROM routing_decisions
        WHERE tenant_id=? AND request_id='req_prd045_route_to'
        """,
        (tenant_id,),
    ).fetchall()
    assert tuple(log) == (
        1,
        "route_to",
        "pol_prd045_1",
        target_model_id,
        target_model_id,
        0,
        1,
        "failed",
        "gateway.upstream_error",
    )
    assert [tuple(row) for row in decisions] == [("selected", "policy.route_to", target_model_id)]
    attempts = connection.execute(
        """
        SELECT attempt_sequence, attempt_role, status
        FROM routing_attempts
        WHERE tenant_id=? AND request_id='req_prd045_route_to'
        """,
        (tenant_id,),
    ).fetchall()
    assert [tuple(row) for row in attempts] == [(1, "primary", "failed")]


@pytest.mark.anyio
async def test_malformed_json_still_records_frozen_snapshot_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常请求：JSON 解码失败发生在 claim 之后，日志仍固定 v1/current 且不触达 Provider。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
        "tenant_id"
    ]
    persist_claimable_snapshot_version(database_url, tenant_id)
    lease = build_policy_lease(
        tenant_id,
        version=1,
        action_type="allow",
        expression='tenant == "tenant_never_matches"',
    )
    app = build_gateway_app()
    app.state.policy_snapshot_cache = StaticPolicySnapshotCache([lease])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd045_bad_json",
                "Content-Type": "application/json",
            },
            content=b'{"model":',
        )

    assert response.status_code == 422
    assert response.json()["code"] == "gateway.request_invalid"
    assert captured == []
    row = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status, status, error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_bad_json'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(row) == (1, "current", "failed", "gateway.request_invalid")


@pytest.mark.anyio
async def test_reused_request_id_is_rejected_before_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """幂等边界：同 tenant 重用 request_id 返回 409，不能覆盖首次成功日志或重复计费。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
        "tenant_id"
    ]
    persist_claimable_snapshot_version(database_url, tenant_id)
    lease = build_policy_lease(
        tenant_id,
        version=1,
        action_type="allow",
        expression='tenant == "tenant_never_matches"',
    )
    app = build_gateway_app()
    app.state.policy_snapshot_cache = StaticPolicySnapshotCache([lease])
    headers = {
        "Authorization": f"Bearer {VIRTUAL_KEY}",
        "X-Request-ID": "req_prd045_reused",
    }
    payload = {"model": "default-chat", "messages": [{"role": "user", "content": "once"}]}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await client.post("/v1/chat/completions", headers=headers, json=payload)
        second = await client.post("/v1/chat/completions", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "gateway.request_id_reused"
    assert len(captured) == 1
    row = connection.execute(
        """
        SELECT snapshot_version, status, error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_reused'
        """,
        (tenant_id,),
    ).fetchone()
    usage_count = connection.execute(
        "SELECT COUNT(*) AS count FROM usage_ledger WHERE tenant_id=? AND request_id='req_prd045_reused'",
        (tenant_id,),
    ).fetchone()["count"]
    assert tuple(row) == (1, "success", None)
    assert usage_count == 1


@pytest.mark.anyio
async def test_fallback_keeps_v1_after_cache_switches_to_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """不可变 lease：主模型失败后缓存切到 v2，备用成功日志和 claim 仍固定请求开始时 v1。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
        "tenant_id"
    ]
    persist_claimable_snapshot_version(database_url, tenant_id)
    v1 = build_policy_lease(
        tenant_id,
        version=1,
        action_type="allow",
        expression='tenant == "tenant_never_matches"',
    )
    v2 = build_policy_lease(
        tenant_id,
        version=2,
        action_type="allow",
        expression='tenant == "tenant_never_matches_v2"',
    )
    cache = StaticPolicySnapshotCache([v1])
    original_send = OpenAICompatibleAdapter.send_non_stream
    provider_calls = 0

    async def switch_cache_before_provider(self, provider_request):
        """第一次网络调用前模拟后台原子 swap；当前请求不能再次读取缓存。"""

        nonlocal provider_calls
        provider_calls += 1
        # 只在主模型第一次发送前切换一次，备用调用期间保持全局缓存已是 v2 的真实状态。
        if provider_calls == 1:
            cache.decisions = [v2]
        return await original_send(self, provider_request)

    monkeypatch.setattr(
        OpenAICompatibleAdapter,
        "send_non_stream",
        switch_cache_before_provider,
    )
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = build_gateway_app()
    app.state.config = app.state.config.model_copy(update={"policy_evaluation_timeout_ms": 100})
    app.state.policy_snapshot_cache = cache

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd045_fallback_v1",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "freeze fallback"}],
            },
        )

    assert response.status_code == 200
    assert provider_calls == 2
    assert [item["body"]["model"] for item in captured[-2:]] == [
        "upstream-error-model",
        "local-chat-model",
    ]
    row = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status, fallback_triggered,
               routing_attempt_count, status
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_fallback_v1'
        """,
        (tenant_id,),
    ).fetchone()
    claim = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status
        FROM gateway_request_claims
        WHERE tenant_id=? AND request_id='req_prd045_fallback_v1'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(row) == (1, "current", 1, 2, "success")
    assert tuple(claim) == (1, "current")
    assert cache.decisions[0].snapshot_version == 2
    assert len(cache.calls) == 1


@pytest.mark.anyio
async def test_completed_stream_keeps_v1_after_cache_switches_to_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """流式 lease：握手时缓存切到 v2，SSE completed、Usage 与 claim 仍沿用同一 v1。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[
        "tenant_id"
    ]
    persist_claimable_snapshot_version(database_url, tenant_id)
    v1 = build_policy_lease(
        tenant_id,
        version=1,
        action_type="allow",
        expression='tenant == "tenant_never_matches"',
    )
    v2 = build_policy_lease(
        tenant_id,
        version=2,
        action_type="allow",
        expression='tenant == "tenant_never_matches_v2"',
    )
    cache = StaticPolicySnapshotCache([v1])
    original_send = OpenAICompatibleAdapter.send_stream

    async def switch_cache_during_handshake(self, provider_request):
        """上游流握手前替换全局缓存，验证生成器终态只闭包捕获 v1 context。"""

        cache.decisions = [v2]
        return await original_send(self, provider_request)

    monkeypatch.setattr(
        OpenAICompatibleAdapter,
        "send_stream",
        switch_cache_during_handshake,
    )
    app = build_gateway_app()
    app.state.config = app.state.config.model_copy(update={"policy_evaluation_timeout_ms": 100})
    app.state.policy_snapshot_cache = cache

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd045_stream_v1",
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "freeze stream"}],
                "stream": True,
            },
        ) as response:
            body = await response.aread()

    assert response.status_code == 200
    assert b"[DONE]" in body
    row = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status, stream_status, status
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd045_stream_v1'
        """,
        (tenant_id,),
    ).fetchone()
    claim = connection.execute(
        """
        SELECT snapshot_version, policy_snapshot_status
        FROM gateway_request_claims
        WHERE tenant_id=? AND request_id='req_prd045_stream_v1'
        """,
        (tenant_id,),
    ).fetchone()
    usage_count = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM usage_ledger
        WHERE tenant_id=? AND request_id='req_prd045_stream_v1'
        """,
        (tenant_id,),
    ).fetchone()["count"]
    assert tuple(row) == (1, "current", "completed", "success")
    assert tuple(claim) == (1, "current")
    assert usage_count == 1
    assert cache.decisions[0].snapshot_version == 2
    assert len(cache.calls) == 1


@pytest.mark.anyio
async def test_policy_mask_reaches_chat_embeddings_and_stream_provider_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """脱敏主流程：三类真实 HTTP 热路径发送给 Provider 的正文均已完成输入脱敏。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    persist_claimable_snapshot_version(database_url, tenant_id)
    lease = build_policy_lease(
        tenant_id,
        version=1,
        action_type="mask",
        expression='env == "dev"',
    )
    app = build_gateway_app()
    app.state.config = app.state.config.model_copy(
        update={
            "policy_evaluation_timeout_ms": 100,
            "policy_mask_runtime_enabled": True,
        }
    )
    app.state.policy_snapshot_cache = StaticPolicySnapshotCache([lease])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        chat = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd068_mask_chat",
            },
            json={
                "model": "default-chat",
                "messages": [
                    {"role": "system", "content": "admin@example.com"},
                    {"role": "user", "content": "联系 13800138000"},
                ],
            },
        )
        embeddings = await client.post(
            "/v1/embeddings",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd068_mask_embeddings",
            },
            json={"model": "default-embedding", "input": "联系 user@example.com"},
        )
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd068_mask_stream",
            },
            json={
                "model": "default-chat",
                "messages": [{"role": "user", "content": "证件 11010519491231002X"}],
                "stream": True,
            },
        ) as stream_response:
            stream_body = await stream_response.aread()

    assert chat.status_code == 200, chat.text
    assert embeddings.status_code == 200, embeddings.text
    assert stream_response.status_code == 200
    assert b"[DONE]" in stream_body
    assert [item["path"] for item in captured] == [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/chat/completions",
    ]
    assert captured[0]["body"]["messages"] == [
        {"role": "system", "content": "admin@example.com"},
        {"role": "user", "content": "联系 [MOBILE_REDACTED]"},
    ]
    assert captured[1]["body"]["input"] == "联系 [EMAIL_REDACTED]"
    assert captured[2]["body"]["messages"][0]["content"] == "证件 [CN_ID_REDACTED]"
    mask_rows = connection.execute(
        """
        SELECT request_id,policy_mask_applied,policy_mask_policy_id
        FROM request_logs
        WHERE tenant_id=? AND request_id IN (
          'req_prd068_mask_chat','req_prd068_mask_embeddings','req_prd068_mask_stream'
        )
        ORDER BY request_id
        """,
        (tenant_id,),
    ).fetchall()
    assert [tuple(row) for row in mask_rows] == [
        ("req_prd068_mask_chat", 1, "pol_prd045_1"),
        ("req_prd068_mask_embeddings", 1, "pol_prd045_1"),
        ("req_prd068_mask_stream", 1, "pol_prd045_1"),
    ]


@pytest.mark.anyio
async def test_policy_mask_payload_is_reused_by_non_stream_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Fallback 流程：主模型 5xx 后备用调用复用脱敏副本，任何一次都不得发送原文。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd036_fallback_alias(database_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    persist_claimable_snapshot_version(database_url, tenant_id)
    lease = build_policy_lease(
        tenant_id,
        version=1,
        action_type="mask",
        expression='model == "company-fallback"',
    )
    app = build_gateway_app()
    app.state.config = app.state.config.model_copy(
        update={
            "policy_evaluation_timeout_ms": 100,
            "policy_mask_runtime_enabled": True,
        }
    )
    app.state.policy_snapshot_cache = StaticPolicySnapshotCache([lease])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd068_mask_fallback",
            },
            json={
                "model": "company-fallback",
                "messages": [{"role": "user", "content": "电话 13800138000"}],
            },
        )

    assert response.status_code == 200, response.text
    assert [item["body"]["model"] for item in captured] == [
        "upstream-error-model",
        "local-chat-model",
    ]
    assert [item["body"]["messages"][0]["content"] for item in captured] == [
        "电话 [MOBILE_REDACTED]",
        "电话 [MOBILE_REDACTED]",
    ]
    log = connection.execute(
        """
        SELECT policy_mask_applied,policy_mask_policy_id,status,error_code
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd068_mask_fallback'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(log) == (1, "pol_prd045_1", "success", None)
