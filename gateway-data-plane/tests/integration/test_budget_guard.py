"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_budget_guard.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.api import openai_compatible as openai_api
from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_gateway.budgets import service as budget_service
from metahub_gateway.routing import models as routing_models
from metahub_gateway.usage import ledger as usage_ledger
from metahub_gateway.usage import spool as usage_spool
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_secret, secret_prefix
from metahub_shared.runtime.budget_blocks import release_active_budget_blocks
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密钥和时间锚点只用于数据面预算守卫夹具，不会作为真实凭据写入日志。
VIRTUAL_KEY = "mhv_prd023_budget_key"
VIRTUAL_KEY_B = "mhv_prd052_project_b_key"
PROVIDER_KEY = "sk-prd023-provider-secret"
FIXED_NOW = datetime(2026, 7, 9, 4, 0, 0, tzinfo=UTC)


def sqlite_url(path: Path) -> str:
    """生成 SQLite 测试数据库 URL，确保每个集成用例使用独立文件。"""

    return f"sqlite:///{path.as_posix()}"


def build_gateway_app():
    """创建只包含 OpenAI-compatible 路由的数据面测试应用。"""

    app = create_service_app("gateway-data-plane", "Gateway PRD-023 Test API")
    app.include_router(openai_compatible_router)
    return app


@pytest.fixture()
def fake_upstream():
    """启动可观测的假上游，用于确认预算阻断是否发生在 Provider 调用前。"""

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """处理测试上游请求，并记录穿透到 Provider 的请求摘要。"""

        def do_POST(self) -> None:  # noqa: N802
            """返回最小 Chat Completions 响应，供网关完成正常用量入账。"""

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append({"path": self.path, "body": body})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl_prd023",
                        "object": "chat.completion",
                        "model": body.get("model", "local-chat-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            """关闭 http.server 默认日志，避免测试输出混入请求内容。"""

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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_base_url: str,
) -> tuple[str, str, str]:
    """准备包含预算规则、模型价格和种子 Virtual Key 的网关测试数据库。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    # 失败入账会写入 spool；每个用例使用 tmp_path 隔离，避免测试草稿污染开发环境默认目录。
    monkeypatch.setenv("METAHUB_FINOPS_USAGE_SPOOL_ROOT", str(tmp_path / "finops-usage-spool"))
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    monkeypatch.setattr(budget_service, "_utcnow", lambda: FIXED_NOW)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()["virtual_key_id"]
    # Real gateway credentials require an active project and application. Keep
    # that authorization gate in these budget integration tests so a 401 cannot
    # accidentally stand in for the expected budget decision.
    timestamp = "2026-07-09T00:00:00Z"
    project_id, application_id = "prj_prd023_budget", "app_prd023_budget"
    connection.execute(
        """INSERT INTO projects(project_id,tenant_id,project_code,name,status,created_at,updated_at)
        VALUES(?,?,'budget-test','Budget test project','active',?,?)""",
        (project_id, seed.tenant_id, timestamp, timestamp),
    )
    connection.execute(
        """INSERT INTO applications(application_id,tenant_id,project_id,application_code,name,status,created_at,updated_at)
        VALUES(?,?,?,'budget-test','Budget test application','active',?,?)""",
        (application_id, seed.tenant_id, project_id, timestamp, timestamp),
    )
    connection.execute(
        "UPDATE virtual_keys SET project_id=?,application_id=? WHERE tenant_id=? AND virtual_key_id=?",
        (project_id, application_id, seed.tenant_id, virtual_key_id),
    )
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=1000, output_price_per_1k=0, currency='CNY',
            price_schema_version='prd023-budget-test'
        WHERE tenant_id=?
        """,
        (seed.tenant_id,),
    )
    connection.commit()
    connection.close()
    # 返回测试租户和 Key，用例随后用它们验证预算规则只作用于当前租户调用链路。
    return database_url, seed.tenant_id, virtual_key_id


def insert_budget_rule(
    database_url: str,
    *,
    tenant_id: str,
    scope_type: str,
    scope_id: str,
    daily_budget_cny: str,
    exhausted_action: str,
    budget_rule_id: str | None = None,
) -> str:
    """直接写入预算规则，让集成测试聚焦数据面准入行为。"""

    rule_id = budget_rule_id or f"br_prd023_{scope_type}_{exhausted_action}"
    connection = connect_database(database_url)
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()
    assert environment is not None
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name, daily_budget_cny,
          warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'PRD023 Budget', ?, 80, 100, ?, 'active', ?, ?)
        """,
        (
            rule_id,
            tenant_id,
            environment["environment_id"],
            scope_type,
            scope_id,
            daily_budget_cny,
            exhausted_action,
            "2026-07-09T00:00:00Z",
            "2026-07-09T00:00:00Z",
        ),
    )
    connection.commit()
    return rule_id


def publish_test_cost_rate(database_url: str, tenant_id: str, *, input_price: int,
                           effective_from: str = "2026-07-09T00:00:00Z") -> None:
    """Append an immutable effective workspace price, using current per-million units."""
    connection = connect_database(database_url)
    try:
        rate = connection.execute(
            "SELECT workspace_provider_cost_rate_id FROM workspace_provider_cost_rates "
            "WHERE tenant_id=? AND currency='CNY'", (tenant_id,),
        ).fetchone()
        assert rate is not None
        rate_id = rate["workspace_provider_cost_rate_id"]
        number = connection.execute(
            "SELECT coalesce(max(version_number),0)+1 FROM workspace_provider_cost_rate_versions "
            "WHERE workspace_provider_cost_rate_id=?", (rate_id,),
        ).fetchone()[0]
        version_id = "pcrv_" + uuid4().hex
        digest = hashlib.sha256(f"{rate_id}|{number}|{input_price}|{effective_from}".encode()).hexdigest()
        connection.execute(
            """INSERT INTO workspace_provider_cost_rate_versions(
            workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,version_number,
            input_rate_micros_per_1k,output_rate_micros_per_1k,cache_rate_micros_per_1k,
            effective_from,source_type,source_id,source_sha256,status,revision,content_sha256,created_at)
            VALUES(?,?,?,?,0,0,?,'manual',?,?,'active',1,?,?)""",
            (version_id, rate_id, number, input_price * 1_000_000, effective_from,
             version_id, digest, digest, effective_from),
        )
        connection.execute(
            "UPDATE workspace_provider_cost_rate_heads SET workspace_provider_cost_rate_version_id=?, "
            "head_revision=head_revision+1,adoption_state='active',updated_at=? "
            "WHERE workspace_provider_cost_rate_id=?", (version_id, effective_from, rate_id),
        )
        connection.commit()
    finally:
        connection.close()


def freeze_budget_price_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Selection, reservation and accounting must observe the same simulated day;
    # otherwise a seed price created today shadows this historical test price.
    monkeypatch.setattr(routing_models, "_utcnow", lambda: budget_service._utcnow())
    monkeypatch.setattr(usage_ledger, "_utcnow", lambda: budget_service._utcnow())


def insert_output_block_rule(
    database_url: str,
    *,
    tenant_id: str,
    pattern: str,
) -> str:
    """写入当前租户的输出阻断规则，用于验证安全终态与预算预留的事务边界。

    参数：测试数据库、租户 ID 和固定命中词。返回：新建规则 ID。
    约束：规则仅扫描输出，不能改变同一请求的输入 DLP 准入结果。
    """

    rule_id = "dlp_prd071_budget_spool_block"
    pattern_hash = f"sha256:{hashlib.sha256(f'keyword:{pattern}'.encode()).hexdigest()}"
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
          action, scan_scope, status, priority, created_at, updated_at
        )
        VALUES(?, ?, 'PRD071 输出预算门禁', 'keyword', ?, ?,
          'block', 'output', 'active', 10, ?, ?)
        """,
        (
            rule_id,
            tenant_id,
            pattern,
            pattern_hash,
            "2026-08-04T08:00:00Z",
            "2026-08-04T08:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    return rule_id


def insert_prior_usage(
    database_url: str,
    tenant_id: str,
    virtual_key_id: str,
    cost_cny: str,
) -> None:
    """写入同日 Usage 与正式 Usage Charge，冻结 PRD-052 的权威预算来源。"""

    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT environment_id, project_id, application_id
            FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?
            """,
            (tenant_id, virtual_key_id),
        ).fetchone()
        assert owner is not None
        usage_id = "usg_prd023_prior"
        snapshot_id = "prs_prd023_prior"
        # 0034 起历史测试用量也必须先有同租户 claim/request；正式账本不能绕过真实 owner 触发器。
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            )
            VALUES(?, 'req_prd023_prior', 'trc_prd023_prior', ?, NULL, 'disabled',
              '2026-07-09T03:00:00Z')
            """,
            (tenant_id, virtual_key_id),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, virtual_key_id, environment_id,
              project_id, application_id, status, input_tokens, output_tokens,
              total_tokens, estimated_cost_amount, currency, created_at, updated_at
            )
            VALUES('req_prd023_prior', ?, 'trc_prd023_prior', ?, ?, ?, ?, 'success', 1, 0, 1,
              ?, 'CNY', '2026-07-09T03:00:00Z', '2026-07-09T03:00:00Z')
            """,
            (
                tenant_id,
                virtual_key_id,
                owner["environment_id"],
                owner["project_id"],
                owner["application_id"],
                cost_cny,
            ),
        )
        connection.execute(
            """
            INSERT INTO price_snapshots(
              price_snapshot_id, tenant_id, request_id, currency, input_price_per_1k,
              output_price_per_1k, price_schema_version, billing_dimensions,
              provider_raw_mapping, source, created_at, updated_at
            ) VALUES(?, ?, 'req_prd023_prior', 'CNY', 0, 0, 'legacy-unversioned-v1',
              ?, '{}', 'finops_legacy_backfill', '2026-07-09T03:00:00Z',
              '2026-07-09T03:00:00Z')
            """,
            (
                snapshot_id,
                tenant_id,
                json.dumps({"finops_usage_ledger_id": usage_id}, separators=(",", ":")),
            ),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens, total_tokens,
              raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd, currency,
              price_snapshot_id, price_schema_version, virtual_key_id, environment_id,
              project_id, application_id, created_at, updated_at
            )
            VALUES(?, ?, 'req_prd023_prior', 1, 0, 1, '{}', ?, ?, ?, 0, 'CNY',
              ?, 'legacy-unversioned-v1', ?, ?, ?, ?, '2026-07-09T03:00:00Z',
              '2026-07-09T03:00:00Z')
            """,
            (
                usage_id,
                tenant_id,
                json.dumps({"cost_amount": cost_cny}, separators=(",", ":")),
                cost_cny,
                cost_cny,
                snapshot_id,
                virtual_key_id,
                owner["environment_id"],
                owner["project_id"],
                owner["application_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

def configure_prd052_project_keys(
    database_url: str,
    *,
    tenant_id: str,
    virtual_key_a_id: str,
) -> tuple[str, str, str]:
    """创建 Project A/B 并把两把 Key 绑定到可信项目，返回项目和 Key B ID。"""

    connection = connect_database(database_url)
    try:
        environment = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (tenant_id,),
        ).fetchone()
        assert environment is not None
        now = "2026-07-09T00:00:00Z"
        project_a_id = "prj_prd052_a"
        project_b_id = "prj_prd052_b"
        virtual_key_b_id = "vk_prd052_b"
        connection.executemany(
            """
            INSERT INTO projects(
              project_id, tenant_id, project_code, name, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'active', ?, ?)
            """,
            [
                (project_a_id, tenant_id, "prd052-a", "PRD052 Project A", now, now),
                (project_b_id, tenant_id, "prd052-b", "PRD052 Project B", now, now),
            ],
        )
        connection.execute(
            """
            INSERT INTO applications(application_id,tenant_id,project_id,application_code,name,status,created_at,updated_at)
            VALUES('app_prd052_a',?,?,'prd052-a','PRD052 Application A','active',?,?),
                  ('app_prd052_b',?,?,'prd052-b','PRD052 Application B','active',?,?)
            """,
            (tenant_id, project_a_id, now, now, tenant_id, project_b_id, now, now),
        )
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?,application_id='app_prd052_a'
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (project_a_id, tenant_id, virtual_key_a_id),
        )
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
              environment, environment_id, project_id, application_id, created_at, updated_at
            ) VALUES(?, ?, 'PRD052 Key B', ?, ?, 'active', 'dev', ?, ?, 'app_prd052_b', ?, ?)
            """,
            (
                virtual_key_b_id,
                tenant_id,
                secret_prefix(VIRTUAL_KEY_B),
                hash_secret(VIRTUAL_KEY_B),
                environment["environment_id"],
                project_b_id,
                now,
                now,
            ),
        )
        connection.commit()
        return project_a_id, project_b_id, virtual_key_b_id
    finally:
        connection.close()


def insert_scoped_finops_adjustment(
    database_url: str,
    *,
    tenant_id: str,
    related_request_id: str,
    adjustment_type: str,
    amount_micros: int,
) -> str:
    """插入继承真实 Usage scope 的正式 FinOps 调整分录并返回 Entry ID。

    参数：数据库、租户、已成功模型请求、调整类型及整数微元金额。返回值只包含公开
    Ledger ID。测试通过生产迁移的 owner trigger 校验所有派生字段，确保调整分录确实
    携带同一 Environment/Project/Key，而不是因为 scope 为空被预算查询偶然排除。
    """

    suffix_by_type = {
        "manual_debit": "a",
        "manual_credit": "b",
        "reversal": "c",
        "gift_credit": "d",
        "trial_credit": "e",
    }
    suffix = suffix_by_type[adjustment_type]
    adjustment_id = f"fadj_{suffix * 32}"
    ledger_entry_id = f"fle_{suffix * 32}"
    event_id = f"flevt_{suffix * 32}"
    operation_request_id = f"req_prd052_finops_{adjustment_type}"
    operation_trace_id = f"trc_prd052_finops_{adjustment_type}"
    created_at = "2026-07-09T03:30:00Z"
    connection = connect_database(database_url)
    try:
        actor = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? ORDER BY user_id LIMIT 1",
            (tenant_id,),
        ).fetchone()
        usage_entry = connection.execute(
            """
            SELECT * FROM finops_ledger_entries
            WHERE tenant_id=? AND entry_type='usage_charge' AND source_request_id=?
            """,
            (tenant_id, related_request_id),
        ).fetchone()
        assert actor is not None and usage_entry is not None

        # reversal 必须引用真实 Usage Charge，其他调整通过 related_request_id 继承相同 scope。
        reversal_of_entry_id = (
            usage_entry["finops_ledger_entry_id"] if adjustment_type == "reversal" else None
        )
        adjustment_related_request_id = (
            None if adjustment_type == "reversal" else related_request_id
        )
        connection.execute(
            """
            INSERT INTO finops_adjustments(
              finops_adjustment_id, tenant_id, adjustment_type, amount_micros,
              currency, reversal_of_entry_id, related_request_id, reason,
              reason_sha256, actor_user_id, operation_request_id, operation_trace_id,
              idempotency_key_sha256, payload_sha256, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'CNY', ?, ?, 'PRD052预算口径验证', ?, ?, ?, ?, ?, ?,
                     'posted', ?, ?)
            """,
            (
                adjustment_id,
                tenant_id,
                adjustment_type,
                amount_micros,
                reversal_of_entry_id,
                adjustment_related_request_id,
                suffix * 64,
                actor["user_id"],
                operation_request_id,
                operation_trace_id,
                "f" * 64,
                "9" * 64,
                created_at,
                created_at,
            ),
        )

        if adjustment_type == "reversal":
            # 冲正复制目标价格证据和安全维度，但预算仍只能统计 usage_charge/debit/posted。
            direction = "credit" if usage_entry["direction"] == "debit" else "debit"
            price_evidence_status = usage_entry["price_evidence_status"]
            price_snapshot_source = usage_entry["price_snapshot_source"]
            quota_type = usage_entry["quota_type"]
            source_request_id = usage_entry["source_request_id"]
            source_trace_id = usage_entry["source_trace_id"]
            price_snapshot_id = usage_entry["price_snapshot_id"]
            price_schema_version = usage_entry["price_schema_version"]
        else:
            direction = "debit" if adjustment_type == "manual_debit" else "credit"
            price_evidence_status = "not_applicable"
            price_snapshot_source = None
            quota_type = {
                "gift_credit": "gift",
                "trial_credit": "trial",
            }.get(adjustment_type, "paid")
            source_request_id = related_request_id
            source_trace_id = usage_entry["source_trace_id"]
            price_snapshot_id = None
            price_schema_version = None

        connection.execute(
            """
            INSERT INTO finops_ledger_entries(
              finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
              amount_micros, currency, normalized_cny_micros, cny_normalization_status,
              price_evidence_status, price_snapshot_source, posting_source, quota_type,
              finops_adjustment_id, source_request_id, source_trace_id,
              operation_request_id, operation_trace_id, price_snapshot_id,
              price_schema_version, reversal_of_entry_id, actor_user_id,
              model_id, provider_id, virtual_key_id, environment_id, project_id,
              application_id, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'CNY', ?, 'normalized', ?, ?, 'direct', ?, ?, ?, ?,
                     ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?)
            """,
            (
                ledger_entry_id,
                event_id,
                tenant_id,
                adjustment_type,
                direction,
                amount_micros,
                amount_micros,
                price_evidence_status,
                price_snapshot_source,
                quota_type,
                adjustment_id,
                source_request_id,
                source_trace_id,
                operation_request_id,
                operation_trace_id,
                price_snapshot_id,
                price_schema_version,
                reversal_of_entry_id,
                actor["user_id"],
                usage_entry["model_id"],
                usage_entry["provider_id"],
                usage_entry["virtual_key_id"],
                usage_entry["environment_id"],
                usage_entry["project_id"],
                usage_entry["application_id"],
                created_at,
                created_at,
            ),
        )
        connection.commit()
        return ledger_entry_id
    finally:
        connection.close()


@pytest.mark.anyio
async def test_budget_uses_frozen_price_plan_and_reuses_routing_token_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """预算主备估算复用路由 as_of/TokenEstimate，不采用目录价或未来 PricePlan。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(
        monkeypatch,
        tmp_path,
        upstream_url,
    )
    connection = connect_database(database_url)
    default_model = connection.execute(
        """
        SELECT m.model_id, m.provider_id, e.environment_id
        FROM models m
        JOIN environments e ON e.tenant_id=m.tenant_id AND e.code='dev'
        WHERE m.tenant_id=? AND m.model_alias='default-chat'
        """,
        (tenant_id,),
    ).fetchone()
    assert default_model is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, status, created_at, updated_at
        ) VALUES(
          'model_prd047_budget_fallback', ?, ?, 'budget-fallback-chat',
          'budget-fallback-model', 'budget-fallback-model', 'Budget Fallback',
          '{"chat": true, "stream": true}', 'active',
          '2026-07-09T03:00:00Z', '2026-07-09T03:00:00Z'
        )
        """,
        (tenant_id, default_model["provider_id"]),
    )
    # Alias 在 0036 下会由数据库触发器原子创建 fixed v1 Policy，随后测试按 CAS 合同改为 cost_first。
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, description, created_at, updated_at
        ) VALUES(
          'alias_prd047_budget_as_of', ?, 'company-default', 'dev', ?, ?,
          'model_prd047_budget_fallback', 'active', 'budget frozen price test',
          '2026-07-09T03:00:00Z', '2026-07-09T03:00:00Z'
        )
        """,
        (tenant_id, default_model["environment_id"], default_model["model_id"]),
    )
    alias = connection.execute(
        """
        SELECT model_alias_id, default_model_id, fallback_model_id
        FROM model_aliases WHERE tenant_id=? AND alias='company-default'
        """,
        (tenant_id,),
    ).fetchone()
    assert alias is not None
    assert alias["fallback_model_id"] is not None
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=1, output_price_per_1k=1,
            currency='CNY', price_schema_version='prd047-catalog-one'
        WHERE tenant_id=? AND model_id IN (?, ?)
        """,
        (tenant_id, alias["default_model_id"], alias["fallback_model_id"]),
    )
    for role, model_id in (
        ("default", alias["default_model_id"]),
        ("fallback", alias["fallback_model_id"]),
    ):
        connection.execute(
            """
            INSERT INTO model_price_plans(
              price_plan_id, tenant_id, model_id, input_price_per_1k,
              output_price_per_1k, currency, price_schema_version,
              effective_from, source, created_at
            ) VALUES(?, ?, ?, 9, 9, 'CNY', ?, '2026-07-09T03:00:00Z',
              'manual', '2026-07-09T03:00:00Z')
            """,
            (
                f"pp_prd047_{role}_current",
                tenant_id,
                model_id,
                f"prd047-{role}-current",
            ),
        )
        connection.execute(
            """
            INSERT INTO model_price_plans(
              price_plan_id, tenant_id, model_id, input_price_per_1k,
              output_price_per_1k, currency, price_schema_version,
              effective_from, source, created_at
            ) VALUES(?, ?, ?, 20, 20, 'CNY', ?, '2026-07-09T05:00:00Z',
              'manual', '2026-07-09T03:30:00Z')
            """,
            (
                f"pp_prd047_{role}_future",
                tenant_id,
                model_id,
                f"prd047-{role}-future",
            ),
        )
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='cost_first', version=version+1,
            updated_at='2026-07-09T03:45:00Z'
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (tenant_id, alias["model_alias_id"]),
    )
    connection.execute(
        """
        INSERT INTO fallback_policies(
          fallback_policy_id, tenant_id, model_alias_id, status, version,
          priority, condition_expr, action, created_at, updated_at
        ) VALUES(
          'fbp_prd047_budget_as_of', ?, ?, 'enabled', 1, 100,
          '["upstream_timeout","upstream_rate_limited","upstream_5xx"]',
          '{"type":"use_alias_fallback","max_attempts":1}',
          '2026-07-09T03:45:00Z', '2026-07-09T03:45:00Z'
        )
        """,
        (tenant_id, alias["model_alias_id"]),
    )
    connection.commit()
    insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="0.2",
        exhausted_action="alert_only",
    )
    monkeypatch.setattr(routing_models, "_utcnow", lambda: FIXED_NOW)

    def reject_budget_reestimate(_request_kind: str, _payload: dict[str, Any]):
        """预算若未复用 cost_first 的 TokenEstimate 就立即失败，避免双重字符遍历。"""

        # 预算重新估算会造成主备金额与冻结决策漂移，因此触发后必须让集成测试失败。
        raise AssertionError("budget must reuse routing token estimate")

    monkeypatch.setattr(budget_service, "estimate_request_tokens", reject_budget_reestimate)
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd047_budget_frozen_price",
            },
            json={
                "model": "company-default",
                "messages": [{"role": "user", "content": "12345678"}],
                "max_completion_tokens": 8,
            },
        )

    assert response.status_code == 200, response.text
    assert captured[-1]["body"]["model"] == "local-chat-model"
    decision = connection.execute(
        """
        SELECT routing_decision_id, price_as_of, estimated_input_tokens,
               estimated_output_tokens
        FROM routing_decisions WHERE request_id='req_prd047_budget_frozen_price'
        """
    ).fetchone()
    assert decision["price_as_of"] == "2026-07-09T04:00:00Z"
    assert (decision["estimated_input_tokens"], decision["estimated_output_tokens"]) == (2, 8)
    candidates = connection.execute(
        """
        SELECT candidate_role, price_source, estimated_cost_amount
        FROM routing_candidates WHERE routing_decision_id=?
        ORDER BY configured_priority
        """,
        (decision["routing_decision_id"],),
    ).fetchall()
    assert [(row["candidate_role"], row["price_source"]) for row in candidates] == [
        ("default", "price_plan"),
        ("fallback", "price_plan"),
    ]
    assert [Decimal(str(row["estimated_cost_amount"])) for row in candidates] == [
        Decimal("0.09"),
        Decimal("0.09"),
    ]
    evaluation = connection.execute(
        """
        SELECT estimated_request_cost_cny, budget_status, action_taken
        FROM budget_evaluations WHERE request_id='req_prd047_budget_frozen_price'
        """
    ).fetchone()
    # 目录价会得到 0.02、未来计划会得到 0.40；0.18 证明主备都使用冻结时点的 9 CNY。
    assert Decimal(str(evaluation["estimated_request_cost_cny"])) == Decimal("0.18")
    assert (evaluation["budget_status"], evaluation["action_taken"]) == (
        "threshold_reached",
        "warn",
    )


@pytest.mark.anyio
async def test_budget_distinguishes_missing_catalog_zero_from_versioned_free_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """预算成本真实性：缺价失败关闭，带版本目录 0 才允许形成合法零元预留。

    无版本目录默认 0 不得调用 Provider，也不得留下 evaluation/reservation；补上版本后，
    相同单价代表真实免费，请求正常执行且 0 元预留保留幂等审计事实。
    """

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(
        monkeypatch,
        tmp_path,
        upstream_url,
    )
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="100",
        exhausted_action="block",
    )
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0, output_price_per_1k=0,
            currency='CNY', price_schema_version=NULL
        WHERE tenant_id=? AND model_alias='default-chat'
        """,
        (tenant_id,),
    )
    connection.commit()
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        missing = await _chat(client, "req_budget_catalog_zero_missing")
        # 只增加版本、不改变 0 单价，精确证明行为差异来自价格真实性而非金额变化。
        connection.execute(
            """
            UPDATE models
            SET price_schema_version='budget-catalog-free-v1'
            WHERE tenant_id=? AND model_alias='default-chat'
            """,
            (tenant_id,),
        )
        connection.commit()
        versioned = await _chat(client, "req_budget_catalog_zero_versioned")

    assert missing.status_code == 503
    assert missing.json()["code"] == "gateway.budget_unavailable"
    assert missing.json()["request_id"] == "req_budget_catalog_zero_missing"
    assert versioned.status_code == 200, versioned.text
    assert len(captured) == 1
    missing_log = connection.execute(
        """
        SELECT status, error_code FROM request_logs
        WHERE request_id='req_budget_catalog_zero_missing'
        """
    ).fetchone()
    assert tuple(missing_log) == ("failed", "gateway.budget_unavailable")
    missing_reservations = connection.execute(
        """
        SELECT COUNT(*) AS count FROM budget_reservations
        WHERE request_id='req_budget_catalog_zero_missing'
        """
    ).fetchone()["count"]
    missing_evaluations = connection.execute(
        """
        SELECT COUNT(*) AS count FROM budget_evaluations
        WHERE request_id='req_budget_catalog_zero_missing'
        """
    ).fetchone()["count"]
    assert missing_reservations == missing_evaluations == 0

    free_reservation = connection.execute(
        """
        SELECT reserved_cost_cny FROM budget_reservations
        WHERE tenant_id=? AND budget_rule_id=?
          AND request_id='req_budget_catalog_zero_versioned'
        """,
        (tenant_id, rule_id),
    ).fetchone()
    free_usage = connection.execute(
        """
        SELECT cost_cny, price_schema_version, price_snapshot_id
        FROM usage_ledger WHERE request_id='req_budget_catalog_zero_versioned'
        """
    ).fetchone()
    assert Decimal(str(free_reservation["reserved_cost_cny"])) == Decimal("0")
    assert Decimal(str(free_usage["cost_cny"])) == Decimal("0")
    assert free_usage["price_schema_version"] == "budget-catalog-free-v1"
    assert free_usage["price_snapshot_id"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("input_price", "currency", "case_name"),
    [
        ("1000", "USD", "usd_without_fx"),
        ("1000000001", "CNY", "above_shared_max"),
        ("0.1234567890123", "CNY", "too_many_decimals"),
        ("1e100", "CNY", "extreme_exponent"),
    ],
)
async def test_budget_rejects_unconvertible_or_out_of_contract_prices_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    input_price: str,
    currency: str,
    case_name: str,
) -> None:
    """安全边界：CNY Budget 对 USD 无汇率和越界 Decimal 统一 503，且不产生上游流量。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(
        monkeypatch,
        tmp_path,
        upstream_url,
    )
    insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="100",
        exhausted_action="block",
    )
    connection = connect_database(database_url)
    # 直接 SQL 模拟迁移前或外部导入的损坏事实；正常控制面入口另有同上限校验。
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=?, output_price_per_1k=0, currency=?,
            price_schema_version='prd047-budget-price-boundary'
        WHERE tenant_id=? AND model_alias='default-chat'
        """,
        (input_price, currency, tenant_id),
    )
    connection.commit()
    request_id = f"req_prd047_budget_{case_name}"

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        response = await _chat(client, request_id)

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.budget_unavailable"
    assert response.json()["request_id"] == request_id
    assert captured == []
    log = connection.execute(
        "SELECT status, error_code FROM request_logs WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()
    assert tuple(log) == ("failed", "gateway.budget_unavailable")
    assert connection.execute(
        "SELECT COUNT(*) FROM budget_reservations WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM budget_evaluations WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_budget_block_stops_before_upstream_and_logs_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """阻断策略：100% 命中返回 402，不调用上游，并写入失败调用日志和预算命中解释。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    freeze_budget_price_clocks(monkeypatch)
    publish_test_cost_rate(database_url, tenant_id, input_price=1_000_000)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(client, "req_prd023_block")
        # 首次大请求建立 active block 后把模型价格降为 0，验证后续小请求仍不能绕过阻断事实。
        publish_test_cost_rate(database_url, tenant_id, input_price=0,
                               effective_from="2026-07-09T03:59:00Z")
        repeated = await _chat(client, "req_prd033_active_block")

    assert response.status_code == 402
    assert response.json()["code"] == "gateway.budget_exhausted"
    assert repeated.status_code == 402
    assert repeated.json()["code"] == "gateway.budget_exhausted"
    assert captured == []
    connection = connect_database(database_url)
    request_log = connection.execute(
        """
        SELECT status, error_code, budget_status, budget_rule_id, budget_action, budget_reason_code,
               budget_current_cost_cny, budget_limit_cny
        FROM request_logs
        WHERE request_id='req_prd023_block'
        """
    ).fetchone()
    assert request_log["status"] == "failed"
    assert request_log["error_code"] == "gateway.budget_exhausted"
    assert request_log["budget_status"] == "exhausted"
    assert request_log["budget_rule_id"] == rule_id
    assert request_log["budget_action"] == "block"
    assert request_log["budget_reason_code"] == "budget.exhausted.block"
    assert str(request_log["budget_current_cost_cny"]) == "1"
    assert str(request_log["budget_limit_cny"]) == "1"
    evaluation_count = connection.execute(
        "SELECT COUNT(*) AS count FROM budget_evaluations "
        "WHERE tenant_id=? AND request_id='req_prd023_block'",
        (tenant_id,),
    ).fetchone()["count"]
    event = connection.execute(
        "SELECT event_type, threshold_percent, action_taken "
        "FROM budget_alert_events WHERE budget_rule_id=?",
        (rule_id,),
    ).fetchone()
    assert evaluation_count == 1
    assert event["event_type"] == "budget.exhausted"
    assert event["threshold_percent"] == 100
    assert event["action_taken"] == "block"
    active_blocks = connection.execute(
        "SELECT COUNT(*) AS count FROM budget_blocks "
        "WHERE tenant_id=? AND budget_rule_id=? AND status='active'",
        (tenant_id, rule_id),
    ).fetchone()["count"]
    block_events = connection.execute(
        "SELECT COUNT(*) AS count FROM budget_block_events "
        "WHERE tenant_id=? AND budget_rule_id=? AND event_type='budget.blocked'",
        (tenant_id, rule_id),
    ).fetchone()["count"]
    repeated_log = connection.execute(
        "SELECT budget_reason_code FROM request_logs WHERE request_id='req_prd033_active_block'"
    ).fetchone()
    assert active_blocks == 1
    assert block_events == 1
    assert repeated_log["budget_reason_code"] == "budget.active_block"

    monkeypatch.setattr(
        budget_service,
        "_utcnow",
        lambda: datetime(2026, 7, 10, 4, 0, 0, tzinfo=UTC),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        next_day = await _chat(client, "req_prd033_next_business_day")
    assert next_day.status_code == 200, next_day.text
    assert len(captured) == 1
    released_block = connection.execute(
        "SELECT status, release_reason FROM budget_blocks WHERE tenant_id=? AND budget_rule_id=?",
        (tenant_id, rule_id),
    ).fetchone()
    assert released_block["status"] == "released"
    assert released_block["release_reason"] == "business_day_ended"


@pytest.mark.anyio
async def test_budget_warning_event_is_deduplicated_per_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """阈值边界：达到 80% 只产生一次告警事件，重复请求仍保留每次预算评估记录。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    freeze_budget_price_clocks(monkeypatch)
    publish_test_cost_rate(database_url, tenant_id, input_price=1_000_000)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="tenant",
        scope_id=tenant_id,
        daily_budget_cny="100",
        exhausted_action="alert_only",
    )
    insert_prior_usage(database_url, tenant_id, virtual_key_id, "79")
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await _chat(client, "req_prd023_warn_1")
        second = await _chat(client, "req_prd023_warn_2")

    assert [first.status_code, second.status_code] == [200, 200]
    assert len(captured) == 2
    connection = connect_database(database_url)
    alert_count = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM budget_alert_events
        WHERE budget_rule_id=? AND threshold_percent=80 AND event_type='budget.threshold_reached'
        """,
        (rule_id,),
    ).fetchone()["count"]
    evaluation_count = connection.execute(
        "SELECT COUNT(*) AS count FROM budget_evaluations WHERE budget_rule_id=?",
        (rule_id,),
    ).fetchone()["count"]
    first_log = connection.execute(
        "SELECT status, budget_status, budget_action FROM request_logs "
        "WHERE request_id='req_prd023_warn_1'"
    ).fetchone()
    assert alert_count == 1
    assert evaluation_count == 2
    assert first_log["status"] == "success"
    assert first_log["budget_status"] == "threshold_reached"
    assert first_log["budget_action"] == "warn"


@pytest.mark.anyio
async def test_budget_raise_recovers_next_request_without_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """恢复时效：硬阻断建立后提额并释放，下一请求立即放行且不需要重启 Gateway。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    # Budget exhaustion is only meaningful when the routed model has an
    # auditable versioned price. Missing prices must remain the distinct
    # gateway.budget_unavailable (503) failure-closed path.
    publish_test_cost_rate(database_url, tenant_id, input_price=1_000_000)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    app = build_gateway_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        blocked = await _chat(client, "req_prd033_before_raise")
        assert blocked.status_code == 402
        assert captured == []

        connection = connect_database(database_url)
        connection.execute(
            "UPDATE budget_rules SET daily_budget_cny=2, updated_at=? "
            "WHERE tenant_id=? AND budget_rule_id=?",
            ("2026-07-09T04:00:30Z", tenant_id, rule_id),
        )
        released = release_active_budget_blocks(
            connection,
            tenant_id=tenant_id,
            budget_rule_id=rule_id,
            reason="budget_raised",
            actor_id=None,
            request_id="req_prd033_raise",
            trace_id="trc_prd033_raise",
            now="2026-07-09T04:00:30Z",
        )
        connection.commit()
        recovered = await _chat(client, "req_prd033_after_raise")

    assert len(released) == 1
    assert recovered.status_code == 200, recovered.text
    assert len(captured) == 1
    lifecycle = connect_database(database_url).execute(
        "SELECT status, release_reason, released_at FROM budget_blocks "
        "WHERE tenant_id=? AND budget_rule_id=?",
        (tenant_id, rule_id),
    ).fetchone()
    assert lifecycle["status"] == "released"
    assert lifecycle["release_reason"] == "budget_raised"
    assert lifecycle["released_at"] == "2026-07-09T04:00:30Z"


@pytest.mark.anyio
async def test_concurrent_budget_admission_is_serialized_by_persistent_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """并发准入：两笔各 1 CNY 的请求竞争 2 CNY 预算时只能放行一笔，且必须在 Provider 前阻断。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="2",
        exhausted_action="block",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        responses = await asyncio.gather(
            _chat(client, "req_prd033_concurrent_1"),
            _chat(client, "req_prd033_concurrent_2"),
        )

    assert sorted(response.status_code for response in responses) == [200, 402]
    assert len(captured) == 1
    connection = connect_database(database_url)
    reservation_count = connection.execute(
        "SELECT COUNT(*) AS count FROM budget_reservations WHERE tenant_id=? AND budget_rule_id=?",
        (tenant_id, rule_id),
    ).fetchone()["count"]
    usage_count = connection.execute(
        """
        SELECT COUNT(*) AS count FROM usage_ledger
        WHERE tenant_id=? AND request_id LIKE 'req_prd033_concurrent_%'
        """,
        (tenant_id,),
    ).fetchone()["count"]
    # 成功 Usage 已存在后 reservation 仍保留审计事实，但后续聚合通过 NOT EXISTS 不再重复计费。
    outstanding = connection.execute(
        """
        SELECT COALESCE(SUM(r.reserved_cost_cny), 0) AS amount
        FROM budget_reservations r
        WHERE r.tenant_id=? AND r.budget_rule_id=?
          AND NOT EXISTS (
            SELECT 1 FROM usage_ledger u WHERE u.tenant_id=r.tenant_id AND u.request_id=r.request_id
          )
        """,
        (tenant_id, rule_id),
    ).fetchone()["amount"]
    assert reservation_count == 1
    assert usage_count == 1
    assert str(outstanding) == "0"


@pytest.mark.anyio
async def test_failed_usage_write_keeps_reservation_conservatively_counted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实 Usage 事务在 settlement 失败时整体回滚，原总预留继续阻断下一笔请求。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="2",
        exhausted_action="block",
    )
    def fail_outstanding_settlement(*_args, **_kwargs) -> int:
        """在 Usage、PriceSnapshot 和关联更新之后失败，验证整个 SQLite 事务原子回滚。"""

        # 异常文本故意携带哨兵；生产 warning 只能记录异常类型，不能泄露底层摘要。
        raise sqlite3.OperationalError("SENSITIVE_SETTLEMENT_SQL_MUST_NOT_LOG")

    monkeypatch.setattr(
        usage_ledger,
        "set_request_budget_reservations_outstanding",
        fail_outstanding_settlement,
    )
    caplog.set_level(logging.WARNING, logger="metahub.gateway.usage")
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        admitted = await _chat(client, "req_prd033_usage_failed")
        blocked = await _chat(client, "req_prd033_after_usage_failed")

    assert admitted.status_code == 200
    assert blocked.status_code == 402
    assert blocked.json()["code"] == "gateway.budget_exhausted"
    assert len(captured) == 1
    connection = connect_database(database_url)
    reservation = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND budget_rule_id=? AND request_id='req_prd033_usage_failed'
        """,
        (tenant_id, rule_id),
    ).fetchone()
    assert str(reservation["reserved_cost_cny"]) == "1"
    assert reservation["outstanding_cost_cny"] is None
    assert connection.execute(
        """
        SELECT COUNT(*) FROM usage_ledger
        WHERE tenant_id=? AND request_id='req_prd033_usage_failed'
        """,
        (tenant_id,),
    ).fetchone()[0] == 0
    assert connection.execute(
        """
        SELECT COUNT(*) FROM price_snapshots
        WHERE tenant_id=? AND request_id='req_prd033_usage_failed'
        """,
        (tenant_id,),
    ).fetchone()[0] == 0
    attempt = connection.execute(
        """
        SELECT status, usage_status, usage_ledger_id
        FROM routing_attempts
        WHERE tenant_id=? AND request_id='req_prd033_usage_failed'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(attempt) == ("succeeded", "pending", None)
    request_log = connection.execute(
        """
        SELECT status, input_tokens, output_tokens, total_tokens,
               estimated_cost_amount, currency, price_schema_version
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd033_usage_failed'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(request_log) == ("success", 0, 0, 0, 0, "CNY", None)
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "usage_ledger.write_failed"
        and record.request_id == "req_prd033_usage_failed"
    ]
    assert len(warnings) == 1
    assert "SENSITIVE_SETTLEMENT_SQL_MUST_NOT_LOG" not in caplog.text
    assert database_url not in caplog.text


@pytest.mark.anyio
async def test_output_block_with_spooled_usage_keeps_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """输出阻断后的 Usage 仅进入 spool 时，预算预留必须等待正式重放入账后再结算。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="2",
        exhausted_action="block",
    )
    insert_output_block_rule(database_url, tenant_id=tenant_id, pattern="pong")

    def fail_usage_database(*_args, **_kwargs):
        """只模拟 Usage 主库事务失败，让真实安全 spool 接管持久化。"""

        # 固定敏感哨兵用于证明底层数据库正文不会穿透统一日志或客户端错误。
        raise sqlite3.OperationalError("SENSITIVE_PRD071_USAGE_FAILURE_MUST_NOT_ESCAPE")

    monkeypatch.setattr(usage_ledger, "record_usage_ledger", fail_usage_database)
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        blocked_output = await _chat(client, "req_prd071_budget_spooled_block")
        budget_blocked = await _chat(client, "req_prd071_budget_after_spool")

    assert blocked_output.status_code == 403
    assert blocked_output.json()["code"] == "content_policy_violation"
    assert budget_blocked.status_code == 402
    assert budget_blocked.json()["code"] == "gateway.budget_exhausted"
    assert len(captured) == 1

    connection = connect_database(database_url)
    reservation = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND budget_rule_id=? AND request_id=?
        """,
        (tenant_id, rule_id, "req_prd071_budget_spooled_block"),
    ).fetchone()
    assert reservation is not None
    assert str(reservation["reserved_cost_cny"]) == "1"
    # spool 尚未重放为正式 usage_charge，提前写 0 会让并发请求绕过预算上限。
    assert reservation["outstanding_cost_cny"] is None
    assert connection.execute(
        """
        SELECT COUNT(*) FROM usage_ledger
        WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, "req_prd071_budget_spooled_block"),
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_output_block_with_lost_usage_keeps_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Usage 主库与 spool 双失败时返回 FinOps 503，并保留预算预留阻断后续请求。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="2",
        exhausted_action="block",
    )
    insert_output_block_rule(database_url, tenant_id=tenant_id, pattern="pong")

    def fail_usage_database(*_args, **_kwargs):
        """模拟正式 Usage 事务失败，强制进入 crash spool 回退。"""

        # 固定哨兵不得出现在统一错误响应，避免数据库摘要泄露内部结构。
        raise sqlite3.OperationalError("SENSITIVE_PRD071_LOST_DATABASE_MUST_NOT_ESCAPE")

    def fail_usage_spool(*_args, **_kwargs):
        """模拟本请求的 crash spool 同步持久化也失败。"""

        # 双失败只能暴露 FinOps 固定错误码，文件系统细节不得进入客户端响应。
        raise OSError("SENSITIVE_PRD071_LOST_SPOOL_MUST_NOT_ESCAPE")

    monkeypatch.setattr(usage_ledger, "record_usage_ledger", fail_usage_database)
    monkeypatch.setattr(usage_spool, "store_usage_spool_record", fail_usage_spool)
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        lost_usage = await _chat(client, "req_prd071_budget_lost_block")
        budget_blocked = await _chat(client, "req_prd071_budget_after_lost")

    assert lost_usage.status_code == 503
    assert lost_usage.json()["code"] == "finops.usage_record_unavailable"
    assert lost_usage.json()["request_id"] == "req_prd071_budget_lost_block"
    assert "SENSITIVE_PRD071" not in lost_usage.text
    assert budget_blocked.status_code == 402
    assert budget_blocked.json()["code"] == "gateway.budget_exhausted"
    assert len(captured) == 1

    connection = connect_database(database_url)
    reservation = connection.execute(
        """
        SELECT reserved_cost_cny, outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND budget_rule_id=? AND request_id=?
        """,
        (tenant_id, rule_id, "req_prd071_budget_lost_block"),
    ).fetchone()
    assert reservation is not None
    assert str(reservation["reserved_cost_cny"]) == "1"
    assert reservation["outstanding_cost_cny"] is None
    assert connection.execute(
        """
        SELECT COUNT(*) FROM usage_ledger
        WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, "req_prd071_budget_lost_block"),
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_later_budget_rule_block_removes_reservations_created_for_current_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """多规则准入：tenant 规则先预留、Virtual Key 规则后阻断时，当前请求不能遗留幽灵占用。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="tenant",
        scope_id=tenant_id,
        daily_budget_cny="100",
        exhausted_action="block",
    )
    insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(client, "req_prd033_later_rule_block")

    assert response.status_code == 402
    assert captured == []
    remaining = connect_database(database_url).execute(
        "SELECT COUNT(*) AS count FROM budget_reservations WHERE tenant_id=? AND request_id=?",
        (tenant_id, "req_prd033_later_rule_block"),
    ).fetchone()["count"]
    assert remaining == 0


@pytest.mark.anyio
async def test_unblocked_log_is_not_emitted_before_commit_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """commit 失败必须同时回滚 block/event，且不得提前输出恢复日志或投递事件。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, virtual_key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=virtual_key_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    connection = connect_database(database_url)
    environment_id = connection.execute(
        "SELECT environment_id FROM budget_rules WHERE budget_rule_id=?",
        (rule_id,),
    ).fetchone()["environment_id"]
    connection.execute(
        """
        INSERT INTO budget_blocks(
          budget_block_id, tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
          budget_date, status, blocked_cost_cny, budget_cny, blocked_request_id,
          blocked_trace_id, blocked_at, created_at, updated_at
        )
        VALUES('bb_prd033_old', ?, ?, ?, 'virtual_key', ?, '2026-07-08', 'active',
          10, 10, 'req_old', 'trc_old', '2026-07-08T04:00:00Z',
          '2026-07-08T04:00:00Z', '2026-07-08T04:00:00Z')
        """,
        (tenant_id, environment_id, rule_id, virtual_key_id),
    )
    connection.commit()
    connection.close()

    real_connect = budget_service.connect_database

    class CommitFailingConnection:
        """仅让预算事务提交失败，其余 SQLite 行为透明委托给真实连接。"""

        def __init__(self, inner: sqlite3.Connection) -> None:
            """保存真实 SQLite 连接，未覆盖的方法和属性都委托给该对象。"""

            self.inner = inner

        def __getattr__(self, name: str):
            """把非 commit 操作透明转发，确保用例只注入事务提交故障。"""

            return getattr(self.inner, name)

        def commit(self) -> None:
            """在预算状态提交点强制失败，验证未提交恢复不会提前写结构化日志。"""

            # 该异常只含固定测试摘要，不包含数据库路径、SQL、请求正文或任何密钥。
            raise sqlite3.OperationalError("forced budget commit failure")

    monkeypatch.setattr(
        budget_service,
        "connect_database",
        lambda url: CommitFailingConnection(real_connect(url)),
    )
    caplog.set_level(logging.INFO, logger="metahub.gateway.budgets")
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(client, "req_prd033_commit_failed")

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.budget_unavailable"
    assert captured == []
    assert not any(record.getMessage() == "budget.unblocked" for record in caplog.records)
    lifecycle = real_connect(database_url).execute(
        "SELECT status FROM budget_blocks WHERE budget_block_id='bb_prd033_old'"
    ).fetchone()
    assert lifecycle["status"] == "active"


def test_finalize_reservation_is_tenant_scoped_and_zero_row_only_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """失败终态只更新目标租户；0-row 保留原账并安全告警，不能覆盖原业务异常。"""

    database_url, tenant_id, _virtual_key_id = prepare_database(
        monkeypatch,
        tmp_path,
        "http://127.0.0.1:9/v1",
    )
    main_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="tenant",
        scope_id=tenant_id,
        daily_budget_cny="100",
        exhausted_action="block",
    )
    connection = connect_database(database_url)
    main_environment_id = connection.execute(
        "SELECT environment_id FROM budget_rules WHERE budget_rule_id=?",
        (main_rule_id,),
    ).fetchone()["environment_id"]
    other_tenant_id = "tenant_prd047_finalize_other"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, 'prd047-finalize-other', 'Finalize Other', 'internal_only', 'active',
                 datetime('now'), datetime('now'))
        """,
        (other_tenant_id,),
    )
    other_environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (other_tenant_id,),
    ).fetchone()
    assert other_environment is not None
    other_environment_id = str(other_environment["environment_id"])
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
          daily_budget_cny, warning_threshold_percent, exhausted_threshold_percent,
          exhausted_action, status, created_at, updated_at
        ) VALUES('br_prd047_finalize_other', ?, ?, 'tenant', ?, 'Other tenant',
                 100, 80, 100, 'block', 'active', datetime('now'), datetime('now'))
        """,
        (other_tenant_id, other_environment_id, other_tenant_id),
    )
    connection.executemany(
        """
        INSERT INTO budget_reservations(
          budget_reservation_id, tenant_id, environment_id, budget_rule_id,
          scope_type, scope_id, budget_date, request_id, trace_id,
          reserved_cost_cny, outstanding_cost_cny, created_at
        ) VALUES(?, ?, ?, ?, 'tenant', ?, '2026-07-09', 'req_prd047_shared',
                 ?, 10, NULL, datetime('now'))
        """,
        [
            (
                "bres_prd047_finalize_main",
                tenant_id,
                main_environment_id,
                main_rule_id,
                tenant_id,
                "trc_prd047_finalize_main",
            ),
            (
                "bres_prd047_finalize_other",
                other_tenant_id,
                other_environment_id,
                "br_prd047_finalize_other",
                other_tenant_id,
                "trc_prd047_finalize_other",
            ),
        ],
    )
    connection.commit()
    connection.close()
    config = SimpleNamespace(database_url=database_url)

    budget_service.finalize_request_budget_reservations(
        config,  # type: ignore[arg-type] - 该函数只消费 database_url，测试使用最小配置替身。
        tenant_id=tenant_id,
        request_id="req_prd047_shared",
        trace_id="trc_prd047_finalize_main",
        outstanding_cost_cny=Decimal("2.5"),
    )
    rows = connect_database(database_url).execute(
        """
        SELECT tenant_id, outstanding_cost_cny
        FROM budget_reservations
        WHERE request_id='req_prd047_shared'
        ORDER BY tenant_id
        """
    ).fetchall()
    assert [(row["tenant_id"], row["outstanding_cost_cny"]) for row in rows] == [
        (tenant_id, 2.5),
        (other_tenant_id, None),
    ]

    caplog.clear()
    caplog.set_level(logging.WARNING, logger="metahub.gateway.budgets")
    budget_service.finalize_request_budget_reservations(
        config,  # type: ignore[arg-type] - 继续验证相同最小配置下的 0-row 容错边界。
        tenant_id=tenant_id,
        request_id="req_prd047_missing",
        trace_id="trc_prd047_missing",
        outstanding_cost_cny=Decimal("987654.321"),
    )

    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "budget.reservation_finalize_failed"
    ]
    assert len(warnings) == 1
    assert warnings[0].tenant_id == tenant_id
    assert warnings[0].request_id == "req_prd047_missing"
    assert warnings[0].trace_id == "trc_prd047_missing"
    assert "987654.321" not in caplog.text
    assert database_url not in caplog.text


@pytest.mark.anyio
async def test_prd052_project_block_isolated_and_scope_event_replayed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Project A 耗尽只阻断 A，B 放行；重复拒绝复用同一持久 Scope 事件。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    project_a_id, _, _ = configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_a_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    queued = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await _chat(client, "req_prd052_project_a_first")
        repeated = await _chat(client, "req_prd052_project_a_repeat")
        project_b = await _chat(
            client,
            "req_prd052_project_b",
            virtual_key=VIRTUAL_KEY_B,
        )

    assert [first.status_code, repeated.status_code, project_b.status_code] == [402, 402, 200]
    assert len(captured) == 1
    budget_events = [event for event in queued if event.event_type == "budget.scope_exceeded"]
    assert len(budget_events) == 2
    assert budget_events[0].event_id == budget_events[1].event_id
    assert budget_events[0].payload["scope_type"] == "project"
    assert budget_events[0].payload["scope_id"] == project_a_id
    assert budget_events[0].payload["request_id"] == "req_prd052_project_a_first"
    serialized = json.dumps(budget_events[0].to_payload(), ensure_ascii=False)
    assert VIRTUAL_KEY not in serialized and PROVIDER_KEY not in serialized

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count, MIN(event_id) AS event_id
            FROM budget_scope_events WHERE tenant_id=? AND budget_rule_id=?
            """,
            (tenant_id, rule_id),
        ).fetchone()
        assert row["count"] == 1
        assert row["event_id"] == budget_events[0].event_id
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_tenant_block_governs_all_projects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Tenant 预算耗尽必须同时阻断 Project A/B，且稳定主因始终是 Tenant Rule。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="tenant",
        scope_id=tenant_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    queued = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        project_a = await _chat(client, "req_prd052_tenant_a")
        project_b = await _chat(client, "req_prd052_tenant_b", virtual_key=VIRTUAL_KEY_B)

    assert [project_a.status_code, project_b.status_code] == [402, 402]
    assert captured == []
    budget_events = [event for event in queued if event.event_type == "budget.scope_exceeded"]
    assert len(budget_events) == 2
    assert {event.event_id for event in budget_events} == {budget_events[0].event_id}
    assert budget_events[0].payload["scope_type"] == "tenant"
    assert budget_events[0].payload["budget_rule_id"] == rule_id

    connection = connect_database(database_url)
    try:
        logged_rules = connection.execute(
            """
            SELECT DISTINCT budget_rule_id FROM request_logs
            WHERE request_id IN ('req_prd052_tenant_a', 'req_prd052_tenant_b')
            """
        ).fetchall()
        assert [row["budget_rule_id"] for row in logged_rules] == [rule_id]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_virtual_key_block_does_not_affect_other_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Key A 规则耗尽只能阻断 A，其他 Project 的 Key B 仍可调用 Provider。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=key_a_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    queued = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued.append(event) or True,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        key_a = await _chat(client, "req_prd052_key_a")
        key_b = await _chat(client, "req_prd052_key_b", virtual_key=VIRTUAL_KEY_B)

    assert [key_a.status_code, key_b.status_code] == [402, 200]
    assert len(captured) == 1
    budget_events = [event for event in queued if event.event_type == "budget.scope_exceeded"]
    assert len(budget_events) == 1
    assert budget_events[0].payload["scope_type"] == "virtual_key"
    assert budget_events[0].payload["scope_id"] == key_a_id
    assert budget_events[0].payload["budget_rule_id"] == rule_id


@pytest.mark.anyio
async def test_prd052_child_block_overrides_parent_alert_and_releases_parent_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Project 告警允许不能覆盖 Key 阻断，且后序拒绝必须撤销本请求的上层预留。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    project_a_id, _, _ = configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    project_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_a_id,
        daily_budget_cny="1",
        exhausted_action="alert_only",
    )
    key_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=key_a_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    queued = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued.append(event) or True,
    )

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        response = await _chat(client, "req_prd052_project_alert_key_block")

    assert response.status_code == 402
    assert response.json()["code"] == "gateway.budget_exhausted"
    assert captured == []
    budget_events = [event for event in queued if event.event_type == "budget.scope_exceeded"]
    assert [event.payload["scope_type"] for event in budget_events] == [
        "project",
        "virtual_key",
    ]
    assert [event.payload["action_taken"] for event in budget_events] == ["allow", "block"]
    assert {event.payload["conflict_policy"] for event in budget_events} == {
        "all_applicable_caps_deny_wins_v1"
    }

    connection = connect_database(database_url)
    try:
        request_log = connection.execute(
            """
            SELECT budget_rule_id, budget_scope_type, budget_scope_id, budget_action
            FROM request_logs
            WHERE tenant_id=? AND request_id='req_prd052_project_alert_key_block'
            """,
            (tenant_id,),
        ).fetchone()
        assert tuple(request_log) == (key_rule_id, "virtual_key", key_a_id, "block")
        remaining_reservations = connection.execute(
            """
            SELECT COUNT(*) FROM budget_reservations
            WHERE tenant_id=? AND request_id='req_prd052_project_alert_key_block'
            """,
            (tenant_id,),
        ).fetchone()[0]
        assert remaining_reservations == 0
        scope_rows = connection.execute(
            """
            SELECT budget_rule_id, action_taken FROM budget_scope_events
            WHERE tenant_id=? AND budget_rule_id IN (?, ?)
            ORDER BY CASE scope_type WHEN 'project' THEN 0 ELSE 1 END
            """,
            (tenant_id, project_rule_id, key_rule_id),
        ).fetchall()
        assert [tuple(row) for row in scope_rows] == [
            (project_rule_id, "allow"),
            (key_rule_id, "block"),
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_tenant_alert_allows_project_block_to_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Tenant 告警允许不能覆盖 Project 硬阻断，且父层预留必须随拒绝撤销。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    project_a_id, _, _ = configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    tenant_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="tenant",
        scope_id=tenant_id,
        daily_budget_cny="1",
        exhausted_action="alert_only",
    )
    project_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_a_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    queued = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued.append(event) or True,
    )

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        response = await _chat(client, "req_prd052_tenant_alert_project_block")

    assert response.status_code == 402
    assert response.json()["code"] == "gateway.budget_exhausted"
    assert captured == []
    scope_events = [event for event in queued if event.event_type == "budget.scope_exceeded"]
    assert [event.payload["scope_type"] for event in scope_events] == ["tenant", "project"]
    assert [event.payload["action_taken"] for event in scope_events] == ["allow", "block"]

    connection = connect_database(database_url)
    try:
        request_log = connection.execute(
            """
            SELECT budget_rule_id, budget_scope_type, budget_scope_id, budget_action
            FROM request_logs
            WHERE tenant_id=? AND request_id='req_prd052_tenant_alert_project_block'
            """,
            (tenant_id,),
        ).fetchone()
        assert tuple(request_log) == (project_rule_id, "project", project_a_id, "block")
        reservations = connection.execute(
            """
            SELECT COUNT(*) FROM budget_reservations
            WHERE tenant_id=? AND request_id='req_prd052_tenant_alert_project_block'
            """,
            (tenant_id,),
        ).fetchone()[0]
        assert reservations == 0
        event_rules = connection.execute(
            """
            SELECT budget_rule_id FROM budget_scope_events
            WHERE tenant_id=? AND budget_rule_id IN (?, ?)
            ORDER BY CASE scope_type WHEN 'tenant' THEN 0 ELSE 1 END
            """,
            (tenant_id, tenant_rule_id, project_rule_id),
        ).fetchall()
        assert [row["budget_rule_id"] for row in event_rules] == [
            tenant_rule_id,
            project_rule_id,
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_tenant_block_short_circuits_key_alert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Tenant 硬阻断优先拒绝，后序 Key 告警不得放行或创建预留及伪事件。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    tenant_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="tenant",
        scope_id=tenant_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    key_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=key_id,
        daily_budget_cny="1",
        exhausted_action="alert_only",
    )
    queued = []
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: queued.append(event) or True,
    )

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        response = await _chat(client, "req_prd052_tenant_block_key_alert")

    assert response.status_code == 402
    assert captured == []
    scope_events = [event for event in queued if event.event_type == "budget.scope_exceeded"]
    assert len(scope_events) == 1
    assert scope_events[0].payload["budget_rule_id"] == tenant_rule_id
    assert scope_events[0].payload["scope_type"] == "tenant"
    assert scope_events[0].payload["action_taken"] == "block"

    connection = connect_database(database_url)
    try:
        request_log = connection.execute(
            """
            SELECT budget_rule_id, budget_scope_type, budget_action
            FROM request_logs
            WHERE tenant_id=? AND request_id='req_prd052_tenant_block_key_alert'
            """,
            (tenant_id,),
        ).fetchone()
        assert tuple(request_log) == (tenant_rule_id, "tenant", "block")
        key_side_effects = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM budget_evaluations
               WHERE tenant_id=? AND budget_rule_id=?) AS evaluations,
              (SELECT COUNT(*) FROM budget_reservations
               WHERE tenant_id=? AND budget_rule_id=?) AS reservations,
              (SELECT COUNT(*) FROM budget_scope_events
               WHERE tenant_id=? AND budget_rule_id=?) AS scope_events
            """,
            (tenant_id, key_rule_id, tenant_id, key_rule_id, tenant_id, key_rule_id),
        ).fetchone()
        assert tuple(key_side_effects) == (0, 0, 0)
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_project_concurrency_serializes_only_the_competing_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """同 Project 最后一笔额度最多放行一次，另一个 Project 的独立额度仍可用。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    monkeypatch.setattr(usage_ledger, "_utcnow", lambda: FIXED_NOW)
    project_a_id, project_b_id, _ = configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    project_a_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_a_id,
        daily_budget_cny="2",
        exhausted_action="block",
        budget_rule_id="br_prd052_project_a_concurrency",
    )
    project_b_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_b_id,
        daily_budget_cny="2",
        exhausted_action="block",
        budget_rule_id="br_prd052_project_b_isolation",
    )

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        project_a_responses = await asyncio.gather(
            _chat(client, "req_prd052_project_concurrent_a1"),
            _chat(client, "req_prd052_project_concurrent_a2"),
        )
        project_b_response = await _chat(
            client,
            "req_scope_isolated_b",
            virtual_key=VIRTUAL_KEY_B,
        )

    assert sorted(response.status_code for response in project_a_responses) == [200, 402]
    assert project_b_response.status_code == 200
    assert len(captured) == 2

    connection = connect_database(database_url)
    try:
        usage_by_project = connection.execute(
            """
            SELECT project_id, COUNT(*) AS count
            FROM finops_ledger_entries
            WHERE tenant_id=? AND entry_type='usage_charge'
              AND source_request_id IN (
                'req_prd052_project_concurrent_a1',
                'req_prd052_project_concurrent_a2',
                'req_scope_isolated_b'
              )
            GROUP BY project_id
            """,
            (tenant_id,),
        ).fetchall()
        assert {row["project_id"]: row["count"] for row in usage_by_project} == {
            project_a_id: 1,
            project_b_id: 1,
        }
        blocks = connection.execute(
            """
            SELECT budget_rule_id, status FROM budget_blocks
            WHERE tenant_id=? AND budget_rule_id IN (?, ?)
            """,
            (tenant_id, project_a_rule_id, project_b_rule_id),
        ).fetchall()
        assert [tuple(row) for row in blocks] == [(project_a_rule_id, "active")]
        scope_events = connection.execute(
            """
            SELECT budget_rule_id, COUNT(*) AS count FROM budget_scope_events
            WHERE tenant_id=? AND budget_rule_id IN (?, ?)
            GROUP BY budget_rule_id
            """,
            (tenant_id, project_a_rule_id, project_b_rule_id),
        ).fetchall()
        assert [tuple(row) for row in scope_events] == [(project_a_rule_id, 1)]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("adjustment_type", "budget_cny", "amount_micros", "expected_statuses", "posted_micros"),
    [
        ("manual_debit", "3", 10_000_000, [200, 200, 402], 2_000_000),
        ("manual_credit", "2", 10_000_000, [200, 402], 1_000_000),
        ("reversal", "2", 1_000_000, [200, 402], 1_000_000),
        ("gift_credit", "2", 10_000_000, [200, 402], 1_000_000),
        ("trial_credit", "2", 10_000_000, [200, 402], 1_000_000),
    ],
)
@pytest.mark.anyio
async def test_prd052_finops_adjustments_do_not_change_model_consumption_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
    adjustment_type: str,
    budget_cny: str,
    amount_micros: int,
    expected_statuses: list[int],
    posted_micros: int,
) -> None:
    """五类人工财务分录即使继承同一 scope，也不增加或冲减模型消费预算。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    monkeypatch.setattr(usage_ledger, "_utcnow", lambda: FIXED_NOW)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=key_id,
        daily_budget_cny=budget_cny,
        exhausted_action="block",
    )
    first_request_id = f"req_prd052_finops_{adjustment_type}_usage_1"

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        responses = [await _chat(client, first_request_id)]
        ledger_entry_id = insert_scoped_finops_adjustment(
            database_url,
            tenant_id=tenant_id,
            related_request_id=first_request_id,
            adjustment_type=adjustment_type,
            amount_micros=amount_micros,
        )
        responses.append(
            await _chat(client, f"req_prd052_finops_{adjustment_type}_usage_2")
        )
        if adjustment_type == "manual_debit":
            responses.append(
                await _chat(client, "req_prd052_finops_manual_debit_usage_3")
            )

    assert [response.status_code for response in responses] == expected_statuses
    assert len(captured) == expected_statuses.count(200)

    connection = connect_database(database_url)
    try:
        adjustment = connection.execute(
            """
            SELECT entry_type, virtual_key_id, environment_id, project_id
            FROM finops_ledger_entries
            WHERE tenant_id=? AND finops_ledger_entry_id=?
            """,
            (tenant_id, ledger_entry_id),
        ).fetchone()
        usage_scope = connection.execute(
            """
            SELECT virtual_key_id, environment_id, project_id
            FROM finops_ledger_entries
            WHERE tenant_id=? AND entry_type='usage_charge' AND source_request_id=?
            """,
            (tenant_id, first_request_id),
        ).fetchone()
        assert adjustment["entry_type"] == adjustment_type
        assert tuple(adjustment)[1:] == tuple(usage_scope)
        event_amounts = connection.execute(
            """
            SELECT posted_cost_micros, reserved_cost_micros,
                   estimated_request_cost_micros, projected_cost_micros
            FROM budget_scope_events
            WHERE tenant_id=? AND budget_rule_id=?
            """,
            (tenant_id, rule_id),
        ).fetchone()
        assert tuple(event_amounts) == (
            posted_micros,
            0,
            1_000_000,
            posted_micros + 1_000_000,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_releasing_one_project_block_keeps_other_project_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """按 Project Rule 精确解封只恢复目标 scope，不得释放同租户其他 Project。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_a_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    project_a_id, project_b_id, _ = configure_prd052_project_keys(
        database_url,
        tenant_id=tenant_id,
        virtual_key_a_id=key_a_id,
    )
    project_a_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_a_id,
        daily_budget_cny="1",
        exhausted_action="block",
        budget_rule_id="br_prd052_project_a_release",
    )
    project_b_rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="project",
        scope_id=project_b_id,
        daily_budget_cny="1",
        exhausted_action="block",
        budget_rule_id="br_prd052_project_b_sticky",
    )

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        blocked_a = await _chat(client, "req_prd052_release_a_before")
        blocked_b = await _chat(
            client,
            "req_prd052_release_b_before",
            virtual_key=VIRTUAL_KEY_B,
        )

        connection = connect_database(database_url)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE budget_rules SET daily_budget_cny=2, updated_at=?
                WHERE tenant_id=? AND budget_rule_id=?
                """,
                ("2026-07-09T04:01:00Z", tenant_id, project_a_rule_id),
            )
            released = release_active_budget_blocks(
                connection,
                tenant_id=tenant_id,
                budget_rule_id=project_a_rule_id,
                reason="budget_raised",
                actor_id=None,
                request_id="req_prd052_release_project_a",
                trace_id="trc_prd052_release_project_a",
                now="2026-07-09T04:01:00Z",
            )
            connection.commit()
        finally:
            connection.close()

        recovered_a = await _chat(client, "req_prd052_release_a_after")
        still_blocked_b = await _chat(
            client,
            "req_prd052_release_b_after",
            virtual_key=VIRTUAL_KEY_B,
        )

    assert [blocked_a.status_code, blocked_b.status_code] == [402, 402]
    assert len(released) == 1
    assert released[0].budget_rule_id == project_a_rule_id
    assert [recovered_a.status_code, still_blocked_b.status_code] == [200, 402]
    assert len(captured) == 1

    connection = connect_database(database_url)
    try:
        lifecycle = connection.execute(
            """
            SELECT budget_rule_id, status, release_reason FROM budget_blocks
            WHERE tenant_id=? AND budget_rule_id IN (?, ?)
            ORDER BY budget_rule_id
            """,
            (tenant_id, project_a_rule_id, project_b_rule_id),
        ).fetchall()
        assert [tuple(row) for row in lifecycle] == [
            (project_a_rule_id, "released", "budget_raised"),
            (project_b_rule_id, "active", None),
        ]
        event_counts = connection.execute(
            """
            SELECT budget_rule_id, event_type, COUNT(*) AS count
            FROM budget_block_events
            WHERE tenant_id=? AND budget_rule_id IN (?, ?)
            GROUP BY budget_rule_id, event_type
            ORDER BY budget_rule_id, event_type
            """,
            (tenant_id, project_a_rule_id, project_b_rule_id),
        ).fetchall()
        assert [tuple(row) for row in event_counts] == [
            (project_a_rule_id, "budget.blocked", 1),
            (project_a_rule_id, "budget.unblocked", 1),
            (project_b_rule_id, "budget.blocked", 1),
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd052_scope_event_rolls_back_and_is_not_enqueued_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """Guard 提交失败时回滚 Scope 事件及治理写入，且不得调用 Provider 或事件队列。"""

    upstream_url, captured = fake_upstream
    database_url, tenant_id, key_id = prepare_database(monkeypatch, tmp_path, upstream_url)
    rule_id = insert_budget_rule(
        database_url,
        tenant_id=tenant_id,
        scope_type="virtual_key",
        scope_id=key_id,
        daily_budget_cny="1",
        exhausted_action="block",
    )
    real_connect = budget_service.connect_database

    class CommitFailingConnection:
        """仅在预算事务提交点注入故障，其余行为透明委托给真实 SQLite 连接。"""

        def __init__(self, inner: sqlite3.Connection) -> None:
            """保存真实连接，使回滚和关闭仍执行生产代码的原始路径。"""

            self.inner = inner

        def __getattr__(self, name: str):
            """转发非 commit 操作，确保用例只改变最终事务提交结果。"""

            return getattr(self.inner, name)

        def commit(self) -> None:
            """在 Scope 事件已写入事务但尚未对外可见时强制提交失败。"""

            # 固定异常文本不包含 SQL、数据库路径、请求正文、Key 或 Provider 凭据。
            raise sqlite3.OperationalError("forced budget scope event commit failure")

    monkeypatch.setattr(
        budget_service,
        "connect_database",
        lambda url: CommitFailingConnection(real_connect(url)),
    )
    queued: list[object] = []

    def record_unexpected_event(_state: object, event: object) -> bool:
        """记录任何意外入队；正确回滚路径不应调用该函数。"""

        queued.append(event)
        return True

    monkeypatch.setattr(openai_api, "enqueue_runtime_event", record_unexpected_event)

    async with AsyncClient(
        transport=ASGITransport(app=build_gateway_app()),
        base_url="http://gateway",
    ) as client:
        response = await _chat(client, "req_prd052_scope_event_commit_failed")

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.budget_unavailable"
    assert response.json()["request_id"] == "req_prd052_scope_event_commit_failed"
    assert captured == []
    assert queued == []
    connection = real_connect(database_url)
    try:
        rolled_back_counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE tenant_id=? AND budget_rule_id=?",
                (tenant_id, rule_id),
            ).fetchone()[0]
            for table in (
                "budget_scope_events",
                "budget_blocks",
                "budget_evaluations",
                "budget_alert_events",
                "budget_reservations",
            )
        }
        assert rolled_back_counts == {
            "budget_scope_events": 0,
            "budget_blocks": 0,
            "budget_evaluations": 0,
            "budget_alert_events": 0,
            "budget_reservations": 0,
        }
        request_log = connection.execute(
            """
            SELECT status, error_code FROM request_logs
            WHERE tenant_id=? AND request_id='req_prd052_scope_event_commit_failed'
            """,
            (tenant_id,),
        ).fetchone()
        assert tuple(request_log) == ("failed", "gateway.budget_unavailable")
    finally:
        connection.close()


async def _chat(
    client: AsyncClient,
    request_id: str,
    *,
    virtual_key: str = VIRTUAL_KEY,
):
    """使用指定测试 Key 发送固定 Chat 请求，便于核对跨 Project 预算隔离。"""

    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {virtual_key}", "X-Request-ID": request_id},
        json={"model": "default-chat", "messages": [{"role": "user", "content": "hi"}]},
    )
