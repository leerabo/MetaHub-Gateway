"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_request_logs.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.request_logs.routes import router as request_logs_router
from metahub_control.request_logs.service import _load_intent_risk
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.provider_error_map import (
    builtin_provider_error_map_lease,
    extract_provider_error_facts,
    normalize_provider_error,
)

# 以下测试密码只用于调用日志读取权限，日志内容仍按 tenant_id 隔离断言。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证和调用日志路由的测试应用。"""

    app = create_service_app("control-plane-api", "Request Log Test Control API")
    app.include_router(auth_router)
    app.include_router(request_logs_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """准备 PRD-016 所需的租户、用户、日志、路由决策和用量账本数据。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = seed.tenant_id
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()
    assert provider is not None
    assert model is not None
    assert virtual_key is not None
    assert environment is not None

    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd016_developer",
                tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd016_viewer",
                tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    # PRD-047 详情夹具使用真实 Alias trigger 生成 fixed v1，避免测试手工伪造 Policy ID。
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_by, created_at, updated_at
        )
        VALUES(
          'ma_prd047_log', ?, 'company-default', 'dev', ?, ?, 'active',
          'user_prd016_developer', '2026-07-08T08:59:00Z', '2026-07-08T08:59:00Z'
        )
        """,
        (tenant_id, environment["environment_id"], model["model_id"]),
    )
    routing_policy = connection.execute(
        """
        SELECT routing_policy_id, routing_mode, version
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id='ma_prd047_log'
        """,
        (tenant_id,),
    ).fetchone()
    assert routing_policy is not None
    # 0034 起 RequestLog 的 tenant/trace/version/status 必须由不可变 claim 先行冻结；
    # 这些 PRD-016/023/024 历史夹具没有发布快照，因此统一使用 NULL+disabled 基线。
    connection.executemany(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        [
            (tenant_id, "req_prd016_success", "trc_prd016_success", virtual_key["virtual_key_id"], "2026-07-08T09:00:00Z"),
            (tenant_id, "req_prd016_failed", "trc_prd016_failed", virtual_key["virtual_key_id"], "2026-07-08T09:01:00Z"),
            (tenant_id, "req_prd023_budget_block", "trc_prd023_budget_block", virtual_key["virtual_key_id"], "2026-07-08T09:02:00Z"),
            (tenant_id, "req_prd024_dlp_block", "trc_prd024_dlp_block", virtual_key["virtual_key_id"], "2026-07-08T09:03:00Z"),
        ],
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, created_at, updated_at
        )
        VALUES(
          'req_prd016_success', ?, 'trc_prd016_success', ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          'success', NULL, 42, 11, 7, 18, 0.123456, 'CNY', 'seed-v1',
          '2026-07-08T09:00:00Z', '2026-07-08T09:00:01Z'
        )
        """,
        (tenant_id, virtual_key["virtual_key_id"], provider["provider_id"], model["model_id"]),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, created_at, updated_at
        )
        VALUES(
          'req_prd016_failed', ?, 'trc_prd016_failed', ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          'failed', 'gateway.upstream_rate_limited', 88, 0, 0, 0, 0, 'CNY', NULL,
          '2026-07-08T09:01:00Z', '2026-07-08T09:01:00Z'
        )
        """,
        (tenant_id, virtual_key["virtual_key_id"], provider["provider_id"], model["model_id"]),
    )
    # PRD-037 流会话夹具严格按 starting -> active -> timed_out 推进，再关联 failed 调用日志。
    connection.execute(
        """
        INSERT INTO stream_sessions(
          stream_session_id, tenant_id, request_id, trace_id, virtual_key_id,
          environment_id, environment_code, model_id, provider_id, provider_code,
          status, inter_token_timeout_ms, started_at, created_at, updated_at
        )
        VALUES(
          'str_prd037_failed', ?, 'req_prd016_failed', 'trc_prd016_failed', ?,
          ?, 'dev', ?, ?, 'local_openai', 'starting', 30000,
          '2026-07-08T09:00:59Z', '2026-07-08T09:00:59Z', '2026-07-08T09:00:59Z'
        )
        """,
        (
            tenant_id,
            virtual_key["virtual_key_id"],
            environment["environment_id"],
            model["model_id"],
            provider["provider_id"],
        ),
    )
    connection.execute(
        """
        UPDATE stream_sessions
        SET status='active', updated_at='2026-07-08T09:01:00Z'
        WHERE tenant_id=? AND stream_session_id='str_prd037_failed'
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        UPDATE stream_sessions
        SET status='timed_out', chunk_count=5, bytes_sent=2048,
            first_chunk_at='2026-07-08T09:01:00Z', last_chunk_at='2026-07-08T09:01:03Z',
            error_code='gateway.upstream_stream_timeout', completed_at='2026-07-08T09:01:33Z',
            updated_at='2026-07-08T09:01:33Z'
        WHERE tenant_id=? AND stream_session_id='str_prd037_failed'
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        UPDATE request_logs
        SET stream_session_id='str_prd037_failed', stream_status='timed_out'
        WHERE tenant_id=? AND request_id='req_prd016_failed'
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name, daily_budget_cny,
          warning_threshold_percent, exhausted_threshold_percent, exhausted_action, status,
          created_at, updated_at
        )
        VALUES(
          'br_prd023_log', ?, ?, 'virtual_key', ?, '日志详情预算', 10, 80, 100, 'block', 'active',
          '2026-07-08T09:02:00Z', '2026-07-08T09:02:00Z'
        )
        """,
        (tenant_id, environment["environment_id"], virtual_key["virtual_key_id"]),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, budget_status, budget_rule_id, budget_scope_type,
          budget_scope_id, budget_action, budget_reason_code, budget_current_cost_cny,
          budget_limit_cny, created_at, updated_at
        )
        VALUES(
          'req_prd023_budget_block', ?, 'trc_prd023_budget_block', ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          'failed', 'gateway.budget_exhausted', 12, 0, 0, 0, 0, 'CNY', NULL,
          'exhausted', 'br_prd023_log', 'virtual_key', ?, 'block', 'budget.exhausted.block',
          12.5, 10, '2026-07-08T09:02:00Z', '2026-07-08T09:02:00Z'
        )
        """,
        (
            tenant_id,
            virtual_key["virtual_key_id"],
            provider["provider_id"],
            model["model_id"],
            virtual_key["virtual_key_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO budget_evaluations(
          budget_evaluation_id, tenant_id, environment_id, request_id, trace_id, virtual_key_id,
          budget_rule_id, scope_type, scope_id, budget_date, spent_before_cny,
          estimated_request_cost_cny, projected_cost_cny, budget_cny, threshold_percent,
          budget_status, action_taken, reason_code, created_at
        )
        VALUES(
          'be_prd023_log', ?, ?, 'req_prd023_budget_block', 'trc_prd023_budget_block', ?,
          'br_prd023_log', 'virtual_key', ?, '2026-07-08', 9.5, 3, 12.5, 10, 100,
          'exhausted', 'block', 'budget.exhausted.block', '2026-07-08T09:02:00Z'
        )
        """,
        (
            tenant_id,
            environment["environment_id"],
            virtual_key["virtual_key_id"],
            virtual_key["virtual_key_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id, tenant_id, name, rule_type, pattern, pattern_hash,
          action, status, priority, created_at, updated_at
        )
        VALUES(
          'dlp_prd024_log', ?, '阻断规则A', 'keyword', '敏感词', 'sha256:test',
          'block', 'active', 100, '2026-07-08T09:03:00Z', '2026-07-08T09:03:00Z'
        )
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status, error_code,
          latency_ms, input_tokens, output_tokens, total_tokens, estimated_cost_amount,
          currency, price_schema_version, dlp_status, dlp_rule_id, dlp_action,
          dlp_match_count, dlp_reason_code, created_at, updated_at
        )
        VALUES(
          'req_prd024_dlp_block', ?, 'trc_prd024_dlp_block', ?, ?, ?,
          '/v1/chat/completions', 'company-default', 'default-chat', 'local_openai',
          'failed', 'gateway.dlp_blocked', 4, 0, 0, 0, 0, 'CNY', NULL,
          'blocked', 'dlp_prd024_log', 'block', 1, 'dlp.keyword_matched',
          '2026-07-08T09:03:00Z', '2026-07-08T09:03:00Z'
        )
        """,
        (tenant_id, virtual_key["virtual_key_id"], provider["provider_id"], model["model_id"]),
    )
    connection.execute(
        """
        INSERT INTO dlp_matches(
          dlp_match_id, tenant_id, request_id, trace_id, dlp_rule_id, rule_name,
          rule_type, action, message_index, match_text_hash, match_length, created_at
        )
        VALUES(
          'dm_prd024_log', ?, 'req_prd024_dlp_block', 'trc_prd024_dlp_block',
          'dlp_prd024_log', '阻断规则A', 'keyword', 'block', 0,
          'sha256:hash-only', 3, '2026-07-08T09:03:00Z'
        )
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          selected_model_id, selected_internal_model_id,
          provider_id, provider_code, decision_status, decision_reason, candidate_summary,
          error_code, created_at, routing_policy_id, routing_policy_version, routing_mode,
          routing_policy_event_id
        )
        VALUES(
          'rtd_prd016_success', ?, 'req_prd016_success', 'trc_prd016_success', ?,
          'ma_prd047_log', 'company-default', 'dev', ?, ?, 'default-chat', ?, 'local_openai',
          'selected', 'routing.fixed_priority', '{"selected": true}', NULL,
          '2026-07-08T09:00:00Z', ?, 1, 'fixed',
          'rpe_11111111111111111111111111111111'
        )
        """,
        (
            tenant_id,
            virtual_key["virtual_key_id"],
            environment["environment_id"],
            model["model_id"],
            provider["provider_id"],
            routing_policy["routing_policy_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO routing_candidates(
          routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
          model_alias_id, model_id, provider_id, candidate_role, configured_priority,
          rank, status, reason_code, health_state, health_fresh, keypool_state,
          price_available, price_source, created_at, updated_at
        )
        VALUES(
          'rcand_11111111111111111111111111111111', ?, 'rtd_prd016_success', ?,
          'ma_prd047_log', ?, ?, 'default', 0, 1, 'selected',
          'routing.fixed_priority', 'unknown', 0, 'not_evaluated',
          0, 'not_evaluated', '2026-07-08T09:00:00Z', '2026-07-08T09:00:00Z'
        )
        """,
        (
            tenant_id,
            routing_policy["routing_policy_id"],
            model["model_id"],
            provider["provider_id"],
        ),
    )
    # PRD-036 详情夹具：两条 attempt 只保存安全元数据，最终 Usage 关联成功的备用尝试。
    connection.executemany(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, attempt_sequence, attempt_role,
          trigger_type, trigger_error_code, model_id, internal_model_id, provider_id, provider_code,
          status, error_code, error_category, upstream_http_status, retryable, latency_ms,
          usage_status, usage_ledger_id, created_at, completed_at, updated_at
        )
        VALUES(?, ?, 'req_prd016_success', 'trc_prd016_success', ?, ?, ?, ?, ?, 'default-chat', ?,
               'local_openai', ?, ?, ?, ?, ?, ?, ?, ?, '2026-07-08T09:00:00Z',
               '2026-07-08T09:00:01Z', '2026-07-08T09:00:01Z')
        """,
        [
            (
                "rta_prd036_primary",
                tenant_id,
                1,
                "primary",
                "initial",
                None,
                model["model_id"],
                provider["provider_id"],
                "failed",
                "gateway.upstream_error",
                "upstream_5xx",
                503,
                1,
                40,
                "unavailable",
                None,
            ),
            (
                "rta_prd036_fallback",
                tenant_id,
                2,
                "fallback",
                "fallback",
                "gateway.upstream_error",
                model["model_id"],
                provider["provider_id"],
                "succeeded",
                None,
                None,
                None,
                0,
                2,
                "recorded",
                None,
            ),
        ],
    )
    # 0041 要求非零 canonical Usage 在写入前已有同租户、同请求、同币种和同版本的不可变价格证据。
    # 这里的单价按 11 input + 7 output 精确得到 0.123456 CNY，避免旧夹具只报金额却没有来源快照。
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id, tenant_id, request_id, model_id, provider_id,
          provider_code, currency, input_price_per_1k, output_price_per_1k,
          price_schema_version, billing_dimensions, provider_raw_mapping,
          source, status, created_at, updated_at
        )
        VALUES(
          'prs_prd016_success', ?, 'req_prd016_success', ?, ?, 'local_openai',
          'CNY', '6.000000', '8.208000', 'seed-v1', '{}', '{}',
          'model_catalog', 'active', '2026-07-08T09:00:01Z',
          '2026-07-08T09:00:01Z'
        )
        """,
        (tenant_id, model["model_id"], provider["provider_id"]),
    )
    # PRD-043 后 Usage Ledger 是追加事实：先有 attempt 和冻结价格证据，再一次写入最终
    # routing_attempt_id；完整 normalized 字段使 0041 正式账本可在同一事务生成且禁止事后改账。
    # JSON 证据使用参数绑定，避免字符串中的双引号或后续扩展字段破坏 SQL 语法。
    normalized_usage = (
        '{"input_tokens":11,"output_tokens":7,"total_tokens":18,"cache_tokens":0,'
        '"cost_amount":"0.123456","cost_cny":"0.123456","cost_usd":"0.000000",'
        '"currency":"CNY","price_snapshot_id":"prs_prd016_success",'
        '"price_schema_version":"seed-v1","posting_source":"direct"}'
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id, tenant_id, request_id, routing_attempt_id,
          input_tokens, output_tokens, total_tokens, cache_tokens,
          raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd,
          currency, price_schema_version, estimated, usage_source,
          price_snapshot_id, model_id, provider_id, virtual_key_id,
          environment_id, environment_code, provider_code, created_at, updated_at
        )
        VALUES(
          'usg_prd016_success', ?, 'req_prd016_success', 'rta_prd036_fallback',
          11, 7, 18, 0,
          '{"prompt_tokens": 11, "secret_sample": "sk-provider-secret"}', ?,
          0.123456, 0.123456, 0, 'CNY', 'seed-v1', 0, 'upstream',
          'prs_prd016_success', ?, ?, ?, ?, 'dev', 'local_openai',
          '2026-07-08T09:00:01Z', '2026-07-08T09:00:01Z'
        )
        """,
        (
            tenant_id,
            normalized_usage,
            model["model_id"],
            provider["provider_id"],
            virtual_key["virtual_key_id"],
            environment["environment_id"],
        ),
    )
    # attempt 的反向引用在账本存在后补齐；只改运行轨迹，不触碰已经分配导出序号的财务事实。
    connection.execute(
        """
        UPDATE routing_attempts
        SET usage_ledger_id='usg_prd016_success'
        WHERE tenant_id=? AND routing_attempt_id='rta_prd036_fallback'
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        UPDATE request_logs
        SET fallback_triggered=1, routing_attempt_count=2, final_routing_attempt_id='rta_prd036_fallback'
        WHERE tenant_id=? AND request_id='req_prd016_success'
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_other_prd016', 'other-prd016', 'Other Tenant', 'internal_only', 'active', datetime('now'), datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES('tenant_other_prd016', 'req_other_tenant', 'trc_other', NULL,
               NULL, 'disabled', datetime('now'))
        """
    )
    connection.execute(
        """
        INSERT INTO request_logs(request_id, tenant_id, trace_id, status, created_at, updated_at)
        VALUES('req_other_tenant', 'tenant_other_prd016', 'trc_other', 'success', datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return database_url, tenant_id


async def login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并返回访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return response.json()["access_token"]


def insert_versioned_request_log(database_url: str, tenant_id: str) -> None:
    """通过 building->published 与 claim 合同插入一条 v1 Policy 决策日志。"""

    connection = connect_database(database_url)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["user_id"]
    connection.execute(
        """
        INSERT INTO policy_snapshots(
          policy_snapshot_id, tenant_id, snapshot_version, schema_version, state,
          operation, request_id, trace_id, request_fingerprint, change_reason,
          approval_status, created_by, created_at
        )
        VALUES('psnap_prd045_log_v1', ?, 1, 'policy-snapshot-v1', 'building',
               'publish', 'req_prd045_log_publish', 'trc_prd045_log_publish', ?,
               '调用日志版本筛选夹具', 'not_required', ?, '2026-07-08T09:04:00Z')
        """,
        (tenant_id, "e" * 64, user_id),
    )
    connection.execute(
        """
        UPDATE policy_snapshots
        SET state='published', content_hash=?, artifact_hash=?, policy_count=0,
            published_at='2026-07-08T09:04:01Z'
        WHERE tenant_id=? AND policy_snapshot_id='psnap_prd045_log_v1'
        """,
        ("f" * 64, "a" * 64, tenant_id),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, snapshot_version,
          policy_snapshot_status, claimed_at
        )
        VALUES(?, 'req_prd045_versioned', 'trc_prd045_versioned', 1,
               'current', '2026-07-08T09:04:02Z')
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, snapshot_version, policy_snapshot_status,
          policy_decision, policy_reason_code, policy_id, policy_action_type,
          policy_target_model_id, policy_mask_applied, policy_mask_policy_id,
          status, created_at, updated_at
        )
        VALUES('req_prd045_versioned', ?, 'trc_prd045_versioned', 1, 'current',
               'route_to', 'policy.rule_matched', 'pol_prd045_log', 'route_to',
               'model_prd045_target', 1, 'pol_prd068_mask',
               'success', '2026-07-08T09:04:02Z',
               '2026-07-08T09:04:03Z')
        """,
        (tenant_id,),
    )
    connection.commit()


def insert_quality_cost_request_log(database_url: str, tenant_id: str) -> None:
    """插入一条合法的单候选 quality_cost 冻结事实供详情 DTO 验证。

    参数：已完成通用夹具的数据库和认证租户。返回值：无。关键约束：Score、Policy、
    Decision 与 Candidate 使用同一租户和精确双版本；测试只读取这些冻结列，不允许服务
    JOIN 当前 Score 回算，因此后续会在读取前继续修改当前人工评分来证明历史不漂移。
    """

    connection = connect_database(database_url)
    row = connection.execute(
        """
        SELECT
          vk.virtual_key_id, e.environment_id,
          a.model_alias_id, a.default_model_id,
          m.model_alias AS internal_model_id, m.provider_id,
          p.provider_code, rp.routing_policy_id,
          qs.model_quality_score_id, qs.statistics_version,
          qs.sample_count, qs.success_count, qs.error_count,
          qs.feedback_version
        FROM model_aliases a
        JOIN model_alias_routing_policies rp
          ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id
        JOIN models m
          ON m.tenant_id=a.tenant_id AND m.model_id=a.default_model_id
        JOIN providers p
          ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        JOIN model_quality_scores qs
          ON qs.tenant_id=m.tenant_id AND qs.model_id=m.model_id
        JOIN environments e
          ON e.tenant_id=a.tenant_id AND e.environment_id=a.environment_id
        JOIN virtual_keys vk
          ON vk.tenant_id=e.tenant_id AND vk.environment_id=e.environment_id
        WHERE a.tenant_id=? AND a.model_alias_id='ma_prd047_log'
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    actor_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='developer' ORDER BY user_id LIMIT 1",
        (tenant_id,),
    ).fetchone()["user_id"]
    connection.execute(
        """
        UPDATE model_quality_scores
        SET manual_quality_bps=9000, feedback_version=feedback_version+1,
            feedback_updated_by=?, feedback_updated_at='2026-07-08T10:00:00Z',
            updated_at='2026-07-08T10:00:00Z'
        WHERE tenant_id=? AND model_quality_score_id=?
        """,
        (actor_id, tenant_id, row["model_quality_score_id"]),
    )
    connection.execute(
        """
        UPDATE model_alias_routing_policies
        SET routing_mode='quality_cost', version=version+1,
            updated_by=?, updated_at='2026-07-08T10:00:00Z'
        WHERE tenant_id=? AND routing_policy_id=?
        """,
        (actor_id, tenant_id, row["routing_policy_id"]),
    )
    current = connection.execute(
        """
        SELECT rp.version, rp.quality_weight_bps, rp.cost_weight_bps,
               rp.latency_weight_bps, rp.quality_floor_bps, rp.minimum_sample_count,
               qs.statistics_version, qs.feedback_version, qs.sample_count,
               qs.success_count, qs.error_count, qs.manual_quality_bps
        FROM model_alias_routing_policies rp
        JOIN model_quality_scores qs
          ON qs.tenant_id=rp.tenant_id AND qs.model_quality_score_id=?
        WHERE rp.tenant_id=? AND rp.routing_policy_id=?
        """,
        (row["model_quality_score_id"], tenant_id, row["routing_policy_id"]),
    ).fetchone()
    assert current is not None
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, 'req_prd048_quality_log', 'trc_prd048_quality_log', ?,
               NULL, 'disabled', '2026-07-08T10:00:00Z')
        """,
        (tenant_id, row["virtual_key_id"]),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          endpoint, request_model, resolved_model, provider_code, status,
          created_at, updated_at
        )
        VALUES(
          'req_prd048_quality_log', ?, 'trc_prd048_quality_log', ?, ?, ?,
          '/v1/chat/completions', 'company-default', ?, ?, 'success',
          '2026-07-08T10:00:00Z', '2026-07-08T10:00:01Z'
        )
        """,
        (
            tenant_id,
            row["virtual_key_id"],
            row["provider_id"],
            row["default_model_id"],
            row["internal_model_id"],
            row["provider_code"],
        ),
    )
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          selected_model_id, selected_internal_model_id, provider_id, provider_code,
          decision_status, decision_reason, candidate_summary, created_at,
          routing_policy_id, routing_policy_version, routing_mode,
          quality_weight_bps, cost_weight_bps, latency_weight_bps,
          quality_floor_bps, minimum_sample_count, quality_as_of,
          quality_formula_version, quality_scoring_performed
        )
        VALUES(
          'rtd_prd048_quality_log', ?, 'req_prd048_quality_log',
          'trc_prd048_quality_log', ?, 'ma_prd047_log', 'company-default', 'dev', ?,
          ?, ?, ?, ?, 'selected', 'routing.quality_cost_single_eligible_candidate',
          '{"quality_snapshot": true}', '2026-07-08T10:00:00Z', ?, ?, 'quality_cost',
          ?, ?, ?, ?, ?, '2026-07-08T10:00:00Z', 'quality_cost_v1', 0
        )
        """,
        (
            tenant_id,
            row["virtual_key_id"],
            row["environment_id"],
            row["default_model_id"],
            row["internal_model_id"],
            row["provider_id"],
            row["provider_code"],
            row["routing_policy_id"],
            current["version"],
            current["quality_weight_bps"],
            current["cost_weight_bps"],
            current["latency_weight_bps"],
            current["quality_floor_bps"],
            current["minimum_sample_count"],
        ),
    )
    connection.execute(
        """
        INSERT INTO routing_candidates(
          routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
          model_alias_id, model_id, provider_id, candidate_role, configured_priority,
          rank, status, reason_code, health_state, health_fresh, keypool_state,
          price_available, price_source,
          model_quality_score_id, quality_statistics_version,
          quality_feedback_version, quality_evidence_state,
          quality_sample_count, quality_success_count, quality_error_count,
          manual_quality_bps, quality_component_bps, created_at, updated_at
        )
        VALUES(
          'rcand_22222222222222222222222222222222', ?, 'rtd_prd048_quality_log', ?,
          'ma_prd047_log', ?, ?, 'default', 0, 1, 'selected',
          'routing.quality_cost_single_eligible_candidate', 'unknown', 0, 'unknown',
          0, 'not_evaluated', ?, ?, ?, 'ready', ?, ?, ?, 9000, 9000,
          '2026-07-08T10:00:00Z', '2026-07-08T10:00:00Z'
        )
        """,
        (
            tenant_id,
            row["routing_policy_id"],
            row["default_model_id"],
            row["provider_id"],
            row["model_quality_score_id"],
            current["statistics_version"],
            current["feedback_version"],
            current["sample_count"],
            current["success_count"],
            current["error_count"],
        ),
    )
    connection.commit()
    # 冻结事实提交后改变当前人工评分；详情必须继续显示 Candidate 内的 9000/v2。
    connection.execute(
        """
        UPDATE model_quality_scores
        SET manual_quality_bps=8000, feedback_version=feedback_version+1,
            feedback_updated_by=?, feedback_updated_at='2026-07-08T10:01:00Z',
            updated_at='2026-07-08T10:01:00Z'
        WHERE tenant_id=? AND model_quality_score_id=?
        """,
        (actor_id, tenant_id, row["model_quality_score_id"]),
    )
    connection.commit()


@pytest.mark.anyio
async def test_request_log_quality_cost_reads_only_frozen_decision_and_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """质量日志返回公式、配置、Score 双版本和分量，当前评分变化不改写历史。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    insert_quality_cost_request_log(database_url, tenant_id)
    control_app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control_app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.get(
            "/request-logs/req_prd048_quality_log",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd048_quality_log_detail",
            },
        )

    assert response.status_code == 200, response.text
    detail = response.json()["request_log"]
    decision = detail["routing_decision"]
    assert decision["routing_mode"] == "quality_cost"
    assert decision["quality_formula_version"] == "quality_cost_v1"
    assert decision["quality_scoring_performed"] is False
    assert decision["quality_as_of"] == "2026-07-08T10:00:00Z"
    assert (
        decision["quality_weight_bps"],
        decision["cost_weight_bps"],
        decision["latency_weight_bps"],
        decision["quality_floor_bps"],
        decision["minimum_sample_count"],
    ) == (5000, 3000, 2000, 6000, 20)
    assert len(detail["routing_candidates"]) == 1
    candidate = detail["routing_candidates"][0]
    assert candidate["quality_evidence_state"] == "ready"
    assert candidate["quality_statistics_version"] == 0
    assert candidate["quality_feedback_version"] == 2
    assert candidate["quality_sample_count"] == 0
    assert candidate["quality_success_count"] == 0
    assert candidate["quality_error_count"] == 0
    assert candidate["manual_quality_bps"] == 9000
    assert candidate["quality_component_bps"] == 9000
    assert candidate["average_latency_ms"] is None
    assert candidate["cost_component_bps"] is None
    assert candidate["latency_component_bps"] is None
    assert candidate["weighted_score_bps"] is None


@pytest.mark.anyio
async def test_request_log_list_filters_pagination_and_detail_are_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员/开发者可按条件查询日志，并且详情不暴露敏感 raw_usage。"""

    _database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        listed = await client.get(
            "/request-logs?limit=1&offset=0",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd016_list"},
        )
        filtered = await client.get(
            "/request-logs?status=success&model=company-default&request_id=req_prd016_success",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd016_filter"},
        )
        stream_filtered = await client.get(
            "/request-logs?status=failed&request_id=req_prd016_failed",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "req_prd037_filter",
            },
        )
        detail = await client.get(
            "/request-logs/req_prd016_success",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd016_detail"},
        )
        failed_detail = await client.get(
            "/request-logs/req_prd016_failed",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd021_failed_detail"},
        )
        budget_detail = await client.get(
            "/request-logs/req_prd023_budget_block",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd023_budget_detail"},
        )
        dlp_detail = await client.get(
            "/request-logs/req_prd024_dlp_block",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd024_dlp_detail"},
        )

    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store, private"
    listed_body = listed.json()
    assert listed_body["total"] == 4
    assert len(listed_body["request_logs"]) == 1
    assert listed_body["request_logs"][0]["tenant_id"] == tenant_id
    assert "req_other_tenant" not in listed.text

    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    log = filtered.json()["request_logs"][0]
    assert log["request_id"] == "req_prd016_success"
    assert log["status"] == "success"
    assert log["total_tokens"] == 18
    assert log["estimated_cost_amount"] == 0.123456
    assert log["virtual_key_prefix"] is not None
    assert log["stream_session_id"] is None
    assert log["stream_status"] is None

    assert stream_filtered.status_code == 200, stream_filtered.text
    stream_log = stream_filtered.json()["request_logs"][0]
    assert stream_log["stream_session_id"] == "str_prd037_failed"
    assert stream_log["stream_status"] == "timed_out"
    # 高频列表只返回终态摘要，不扩大为逐块时间、阈值或计数。
    assert "chunk_count" not in stream_log
    assert "bytes_sent" not in stream_log
    assert "first_chunk_at" not in stream_log

    assert detail.status_code == 200, detail.text
    assert detail.headers["cache-control"] == "no-store, private"
    detail_text = detail.text
    detail_body = detail.json()["request_log"]
    assert detail_body["log"]["request_id"] == "req_prd016_success"
    routing_decision = detail_body["routing_decision"]
    assert routing_decision["decision_reason"] == "routing.fixed_priority"
    assert routing_decision["routing_policy_version"] == 1
    assert routing_decision["routing_mode"] == "fixed"
    assert routing_decision["routing_policy_event_id"] == "rpe_11111111111111111111111111111111"
    assert routing_decision["estimated_input_tokens"] is None
    assert routing_decision["quality_formula_version"] is None
    assert routing_decision["quality_as_of"] is None
    assert routing_decision["quality_scoring_performed"] is False
    assert routing_decision["quality_weight_bps"] is None
    candidates = detail_body["routing_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["candidate_role"] == "default"
    assert candidates[0]["configured_priority"] == 0
    assert candidates[0]["status"] == "selected"
    assert candidates[0]["health_state"] == "unknown"
    assert candidates[0]["health_fresh"] is False
    assert candidates[0]["keypool_state"] == "not_evaluated"
    assert candidates[0]["price_source"] == "not_evaluated"
    assert candidates[0]["estimated_cost_amount"] is None
    assert candidates[0]["quality_evidence_state"] == "not_evaluated"
    assert candidates[0]["model_quality_score_id"] is None
    assert candidates[0]["quality_component_bps"] is None
    assert candidates[0]["weighted_score_bps"] is None
    assert detail_body["usage"][0]["total_tokens"] == 18
    assert detail_body["log"]["fallback_triggered"] is True
    assert [item["attempt_sequence"] for item in detail_body["routing_attempts"]] == [1, 2]
    assert detail_body["routing_attempts"][0]["error_category"] == "upstream_5xx"
    assert detail_body["routing_attempts"][1]["usage_status"] == "recorded"
    assert detail_body["usage"][0]["routing_attempt_id"] == "rta_prd036_fallback"
    assert detail_body["stream_session"] is None
    assert "sk-provider-secret" not in detail_text
    assert "secret_sample" not in detail_text

    assert failed_detail.status_code == 200, failed_detail.text
    failed_body = failed_detail.json()["request_log"]
    assert failed_body["error_diagnosis"]["rule_id"] == "upstream_rate_limited"
    assert failed_body["error_diagnosis"]["retryable"] is True
    stream_session = failed_body["stream_session"]
    assert stream_session == {
        "stream_session_id": "str_prd037_failed",
        "status": "timed_out",
        "inter_token_timeout_ms": 30000,
        "chunk_count": 5,
        "bytes_sent": 2048,
        "first_chunk_at": "2026-07-08T09:01:00Z",
        "last_chunk_at": "2026-07-08T09:01:03Z",
        "error_code": "gateway.upstream_stream_timeout",
        "upstream_http_status": None,
        "retryable": False,
        "provider_error_fact": None,
        "started_at": "2026-07-08T09:00:59Z",
        "completed_at": "2026-07-08T09:01:33Z",
    }
    assert "provider_id" not in stream_session
    assert "virtual_key_id" not in stream_session
    assert "environment_id" not in stream_session
    assert "sk-" not in failed_detail.text

    assert budget_detail.status_code == 200, budget_detail.text
    budget_body = budget_detail.json()["request_log"]
    budget_log = budget_body["log"]
    assert budget_log["error_code"] == "gateway.budget_exhausted"
    assert budget_log["budget_status"] == "exhausted"
    assert budget_log["budget_rule_id"] == "br_prd023_log"
    assert budget_log["budget_action"] == "block"
    assert budget_log["budget_reason_code"] == "budget.exhausted.block"
    assert budget_body["budget_evaluations"][0]["projected_cost_cny"] == 12.5
    assert budget_body["budget_evaluations"][0]["threshold_percent"] == 100

    assert dlp_detail.status_code == 200, dlp_detail.text
    dlp_body = dlp_detail.json()["request_log"]
    dlp_log = dlp_body["log"]
    assert dlp_log["error_code"] == "gateway.dlp_blocked"
    assert dlp_log["dlp_status"] == "blocked"
    assert dlp_log["dlp_rule_id"] == "dlp_prd024_log"
    assert dlp_log["dlp_action"] == "block"
    assert dlp_log["dlp_match_count"] == 1
    assert dlp_body["dlp_matches"][0]["match_text_hash"] == "sha256:hash-only"
    assert dlp_body["dlp_matches"][0]["match_length"] == 3
    assert "敏感词" not in dlp_detail.text


@pytest.mark.anyio
async def test_request_log_detail_resolves_preserved_legacy_duplicate_decisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """0036 兼容：新 Policy 事实优先，纯历史重复则确定性返回最新 Decision。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    metadata = connection.execute(
        """
        SELECT vk.virtual_key_id, e.environment_id
        FROM virtual_keys vk
        JOIN environments e
          ON e.tenant_id=vk.tenant_id AND e.environment_id=vk.environment_id
        WHERE vk.tenant_id=? AND e.code='dev'
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert metadata is not None
    # 第一行故意比已有 Policy Decision 更新，证明“最新时间”不能覆盖 PRD-047 权威事实；
    # 后两行模拟 0035 以前允许的纯 legacy 重复，必须保真并稳定选择最后一条。
    connection.executemany(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          requested_model, environment, environment_id, decision_status,
          decision_reason, candidate_summary, error_code, created_at
        )
        VALUES(?, ?, ?, ?, ?, 'legacy-model', 'dev', ?, 'failed', ?, '{}',
               'gateway.model_not_found', ?)
        """,
        [
            (
                "rtd_prd047_legacy_success_newer",
                tenant_id,
                "req_prd016_success",
                "trc_prd016_success",
                metadata["virtual_key_id"],
                metadata["environment_id"],
                "legacy.success.newer",
                "2026-07-08T09:10:00Z",
            ),
            (
                "rtd_prd047_legacy_failed_old",
                tenant_id,
                "req_prd016_failed",
                "trc_prd016_failed",
                metadata["virtual_key_id"],
                metadata["environment_id"],
                "legacy.failed.old",
                "2026-07-08T09:10:01Z",
            ),
            (
                "rtd_prd047_legacy_failed_new",
                tenant_id,
                "req_prd016_failed",
                "trc_prd016_failed",
                metadata["virtual_key_id"],
                metadata["environment_id"],
                "legacy.failed.new",
                "2026-07-08T09:10:02Z",
            ),
        ],
    )
    connection.commit()

    control_app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control_app), base_url="http://control"
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        policy_detail = await client.get("/request-logs/req_prd016_success", headers=headers)
        legacy_detail = await client.get("/request-logs/req_prd016_failed", headers=headers)

    assert policy_detail.status_code == 200, policy_detail.text
    policy_decision = policy_detail.json()["request_log"]["routing_decision"]
    assert policy_decision["routing_decision_id"] == "rtd_prd016_success"
    assert policy_decision["routing_policy_id"] is not None
    assert legacy_detail.status_code == 200, legacy_detail.text
    legacy_body = legacy_detail.json()["request_log"]
    assert legacy_body["routing_decision"]["routing_decision_id"] == (
        "rtd_prd047_legacy_failed_new"
    )
    assert legacy_body["routing_decision"]["decision_reason"] == "legacy.failed.new"
    assert legacy_body["routing_candidates"] == []


@pytest.mark.anyio
async def test_request_logs_filter_and_detail_return_safe_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-045/068：返回版本、决定与 mask 安全摘要，非法版本统一拒绝。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    insert_versioned_request_log(database_url, tenant_id)
    control_app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control_app), base_url="http://control"
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        filtered = await client.get("/request-logs?snapshot_version=1", headers=headers)
        detail = await client.get("/request-logs/req_prd045_versioned", headers=headers)
        bad_zero = await client.get("/request-logs?snapshot_version=0", headers=headers)
        bad_text = await client.get("/request-logs?snapshot_version=true", headers=headers)

    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    summary = filtered.json()["request_logs"][0]
    assert summary["tenant_id"] == tenant_id
    assert summary["snapshot_version"] == 1
    assert summary["policy_snapshot_status"] == "current"
    assert summary["policy_decision"] == "route_to"
    assert summary["policy_reason_code"] == "policy.rule_matched"
    assert summary["policy_id"] == "pol_prd045_log"
    assert summary["policy_target_model_id"] == "model_prd045_target"
    assert summary["policy_mask_applied"] is True
    assert summary["policy_mask_policy_id"] == "pol_prd068_mask"
    assert "canonical_expression" not in filtered.text
    assert "condition_expr" not in filtered.text
    assert detail.status_code == 200
    detail_log = detail.json()["request_log"]["log"]
    assert detail_log["snapshot_version"] == 1
    assert detail_log["policy_decision"] == "route_to"
    assert detail_log["policy_id"] == "pol_prd045_log"
    assert detail_log["policy_mask_applied"] is True
    assert detail_log["policy_mask_policy_id"] == "pol_prd068_mask"
    for response in (bad_zero, bad_text):
        assert response.status_code == 422
        assert response.json()["code"] == "request_log.query_invalid"


@pytest.mark.anyio
async def test_request_log_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：viewer 被后端拒绝，非法状态、非法分页和缺失详情返回统一错误码。"""

    prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_denied = await client.get(
            "/request-logs",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        bad_status = await client.get(
            "/request-logs?status=running",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        bad_limit = await client.get(
            "/request-logs?limit=101",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        not_found = await client.get(
            "/request-logs/req_missing",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        cross_tenant_not_found = await client.get(
            "/request-logs/req_other_tenant",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert bad_status.status_code == 422
    assert bad_status.json()["code"] == "request_log.status_invalid"
    assert bad_limit.status_code == 422
    assert bad_limit.json()["code"] == "request_log.query_invalid"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "request_log.not_found"
    assert cross_tenant_not_found.status_code == 404
    assert cross_tenant_not_found.json()["code"] == "request_log.not_found"


def _insert_prd056_provider_error_fact(
    connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    provider_id: str,
    model_id: str,
    provider_code: str,
    upstream_http_status: int,
    raw_body: bytes,
    owner_terminal_error_code: str,
    created_at: str,
    routing_attempt_id: str | None = None,
    stream_session_id: str | None = None,
) -> str:
    """从 Shared 归一结果写入一条真实 0046 Fact 测试夹具。

    参数均来自同租户 owner 行；返回生成的 Fact ID。原始正文只在本函数内短暂参与脱敏，
    SQL 只保存 ``ProviderErrorEvidence`` 的固定投影，借此同时验证数据库 trigger 合同。
    """

    assert (routing_attempt_id is None) != (stream_session_id is None)
    extracted = extract_provider_error_facts(
        upstream_http_status=upstream_http_status,
        transport_kind="none",
        raw_body=raw_body,
        body_truncated=False,
        known_body_length=len(raw_body),
    )
    normalized = normalize_provider_error(
        extracted,
        lease=builtin_provider_error_map_lease(tenant_id),
        provider_id=provider_id,
    )
    evidence = normalized.evidence
    fact_id = f"pef_{'a' if routing_attempt_id else 'b'}" + "0" * 31
    values = (
        fact_id,
        tenant_id,
        request_id,
        trace_id,
        routing_attempt_id,
        stream_session_id,
        provider_id,
        model_id,
        provider_code,
        evidence.upstream_http_status,
        evidence.transport_kind,
        evidence.provider_error_code_class,
        evidence.provider_error_type_class,
        1 if evidence.provider_error_code_present else 0,
        1 if evidence.provider_error_type_present else 0,
        1 if evidence.message_present else 0,
        evidence.redacted_projection_json,
        evidence.projection_hash,
        evidence.body_length_bucket,
        1 if evidence.body_truncated else 0,
        1 if evidence.is_json else 0,
        evidence.matched_error_map_id,
        evidence.matched_rule_version,
        evidence.matched_pattern_hash,
        evidence.matched_rule_source,
        evidence.error_map_revision,
        evidence.overlay_content_hash,
        evidence.builtin_catalog_version,
        evidence.error_map_snapshot_source,
        evidence.match_basis,
        evidence.normalized_output_error_code,
        owner_terminal_error_code,
        created_at,
    )
    # 列名与 0046 Fact schema 保持一一对应；测试不借助 RequestLog service 重算任何历史值。
    columns = (
        "provider_error_fact_id,tenant_id,request_id,trace_id,routing_attempt_id,"
        "stream_session_id,provider_id,model_id,provider_code,upstream_http_status,"
        "transport_kind,provider_error_code_class,provider_error_type_class,"
        "provider_error_code_present,provider_error_type_present,message_present,"
        "redacted_projection_json,projection_hash,body_length_bucket,body_truncated,is_json,"
        "matched_error_map_id,matched_rule_version,matched_pattern_hash,matched_rule_source,"
        "error_map_revision,overlay_content_hash,builtin_catalog_version,"
        "error_map_snapshot_source,match_basis,normalized_output_error_code,"
        "owner_terminal_error_code,created_at"
    )
    connection.execute(
        f"INSERT INTO provider_error_facts({columns}) "
        f"VALUES({','.join('?' for _ in values)})",
        values,
    )
    return fact_id


def _prepare_prd056_request_log_facts(database_url: str, tenant_id: str) -> tuple[str, str]:
    """为 attempt 与 stream 两个真实详情挂载点准备同租户冻结事实。

    返回两个 Fact ID。stream 使用独立 RequestLog，避免改写 PRD-037 的 timed_out 历史夹具；
    所有 owner 状态先终结，再在同一连接中插入 Fact，符合 Gateway 的事务顺序。
    """

    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT attempt.provider_id, attempt.model_id, attempt.provider_code,
                   claim.virtual_key_id
            FROM routing_attempts attempt
            JOIN gateway_request_claims claim
              ON claim.tenant_id=attempt.tenant_id AND claim.request_id=attempt.request_id
            WHERE attempt.tenant_id=? AND attempt.routing_attempt_id='rta_prd036_primary'
            """,
            (tenant_id,),
        ).fetchone()
        assert owner is not None
        attempt_fact_id = _insert_prd056_provider_error_fact(
            connection,
            tenant_id=tenant_id,
            request_id="req_prd016_success",
            trace_id="trc_prd016_success",
            routing_attempt_id="rta_prd036_primary",
            provider_id=str(owner["provider_id"]),
            model_id=str(owner["model_id"]),
            provider_code=str(owner["provider_code"]),
            upstream_http_status=503,
            raw_body=b"",
            owner_terminal_error_code="gateway.upstream_error",
            created_at="2026-07-08T09:00:01Z",
        )

        stream_request_id = "req_prd056_stream_fact"
        stream_trace_id = "trc_prd056_stream_fact"
        stream_session_id = "str_prd056_handshake_failed"
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            ) VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-29T12:10:00Z')
            """,
            (tenant_id, stream_request_id, stream_trace_id, owner["virtual_key_id"]),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
              endpoint, request_model, resolved_model, provider_code, status, error_code,
              created_at, updated_at
            ) VALUES(
              ?, ?, ?, ?, ?, ?, '/v1/chat/completions', 'default-chat', 'default-chat', ?,
              'failed', 'gateway.upstream_stream_failed',
              '2026-07-29T12:10:00Z', '2026-07-29T12:10:01Z'
            )
            """,
            (
                stream_request_id,
                tenant_id,
                stream_trace_id,
                owner["virtual_key_id"],
                owner["provider_id"],
                owner["model_id"],
                owner["provider_code"],
            ),
        )
        connection.execute(
            """
            INSERT INTO stream_sessions(
              stream_session_id, tenant_id, request_id, trace_id, virtual_key_id,
              model_id, provider_id, provider_code, status, inter_token_timeout_ms,
              started_at, created_at, updated_at
            ) VALUES(
              ?, ?, ?, ?, ?, ?, ?, ?, 'starting', 30000,
              '2026-07-29T12:10:00Z', '2026-07-29T12:10:00Z', '2026-07-29T12:10:00Z'
            )
            """,
            (
                stream_session_id,
                tenant_id,
                stream_request_id,
                stream_trace_id,
                owner["virtual_key_id"],
                owner["model_id"],
                owner["provider_id"],
                owner["provider_code"],
            ),
        )
        connection.execute(
            """
            UPDATE stream_sessions
            SET status='upstream_failed', error_code='gateway.upstream_stream_failed',
                upstream_http_status=429, retryable=1,
                completed_at='2026-07-29T12:10:01Z', updated_at='2026-07-29T12:10:01Z'
            WHERE tenant_id=? AND stream_session_id=? AND status='starting'
            """,
            (tenant_id, stream_session_id),
        )
        connection.execute(
            """
            UPDATE request_logs
            SET stream_session_id=?, stream_status='upstream_failed'
            WHERE tenant_id=? AND request_id=?
            """,
            (stream_session_id, tenant_id, stream_request_id),
        )
        stream_fact_id = _insert_prd056_provider_error_fact(
            connection,
            tenant_id=tenant_id,
            request_id=stream_request_id,
            trace_id=stream_trace_id,
            stream_session_id=stream_session_id,
            provider_id=str(owner["provider_id"]),
            model_id=str(owner["model_id"]),
            provider_code=str(owner["provider_code"]),
            upstream_http_status=429,
            raw_body=b'{"error":{"message":"prd056-private-upstream-sentinel"}}',
            owner_terminal_error_code="gateway.upstream_stream_failed",
            created_at="2026-07-29T12:10:01Z",
        )
        connection.commit()
        return attempt_fact_id, stream_fact_id
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd056_request_log_detail_returns_attempt_and_stream_frozen_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PAGE-082 主流程：真实详情分别返回 attempt/stream Fact，且原始正文不外泄。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    attempt_fact_id, stream_fact_id = _prepare_prd056_request_log_facts(
        database_url, tenant_id
    )
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        attempt_response = await client.get(
            "/request-logs/req_prd016_success",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd056_attempt_fact_detail",
            },
        )
        stream_response = await client.get(
            "/request-logs/req_prd056_stream_fact",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd056_stream_fact_detail",
            },
        )

    assert attempt_response.status_code == 200, attempt_response.text
    attempt_payload = attempt_response.json()
    assert attempt_payload["request_id"] == "req_prd056_attempt_fact_detail"
    primary = attempt_payload["request_log"]["routing_attempts"][0]
    attempt_fact = primary["provider_error_fact"]
    assert attempt_fact["provider_error_fact_id"] == attempt_fact_id
    assert attempt_fact["routing_attempt_id"] == "rta_prd036_primary"
    assert attempt_fact["stream_session_id"] is None
    assert attempt_fact["retryable"] is True
    assert attempt_fact["matched_rule_source"] == "guard"
    assert attempt_fact["normalized_output_error_code"] == "gateway.upstream_error"

    assert stream_response.status_code == 200, stream_response.text
    stream_payload = stream_response.json()
    assert stream_payload["request_id"] == "req_prd056_stream_fact_detail"
    stream = stream_payload["request_log"]["stream_session"]
    stream_fact = stream["provider_error_fact"]
    assert stream_fact["provider_error_fact_id"] == stream_fact_id
    assert stream_fact["routing_attempt_id"] is None
    assert stream_fact["stream_session_id"] == "str_prd056_handshake_failed"
    assert stream_fact["retryable"] is True
    assert stream_fact["matched_rule_source"] == "builtin"
    assert stream_fact["normalized_output_error_code"] == "gateway.upstream_rate_limited"
    assert stream_fact["redacted_projection_json"]["message"] == "[REDACTED]"
    assert "prd056-private-upstream-sentinel" not in stream_response.text


def insert_prd069_intent_request_log(
    connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    case_name: str,
    schedule_status: str,
    result_status: str | None,
) -> str:
    """插入一条 PRD-069 Gateway 调用及可选不可变分类终态。

    参数：数据库连接、可信 owner、用例名、RequestLog 调度态和可选结果终态。返回：源请求 ID。
    夹具严格复用真实 claim/owner trigger，只使用固定安全枚举，不保存测试输入正文。
    """

    request_id = f"req_prd069_{case_name}"
    trace_id = f"trc_prd069_{case_name}"
    result_id = f"irr_prd069_{case_name}"
    mode = "sync_strong" if schedule_status == "blocked" else "async_light"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-08-03T12:00:00Z')
        """,
        (tenant_id, request_id, trace_id, virtual_key_id),
    )
    result_values = {
        "clean": ("none", 0, "allow", "no_risk_detected", None),
        "detected": (
            "policy_bypass",
            8200,
            "observe",
            "explicit_policy_bypass",
            None,
        ),
        "blocked": (
            "jailbreak",
            9800,
            "block",
            "explicit_instruction_override",
            None,
        ),
        "failed": (
            "none",
            0,
            "none",
            "classifier_failed",
            "intent_risk.classifier_failed",
        ),
    }
    if result_status is not None:
        risk_type, risk_score, action, reason_code, error_code = result_values[result_status]
        connection.execute(
            """
            INSERT INTO intent_risk_results(
              intent_risk_result_id, tenant_id, source_kind, source_request_id,
              source_trace_id, actor_type, actor_id, status, classification_mode,
              risk_type, risk_score, risk_score_scale, action, reason_code,
              detector_version, ruleset_version, input_code_points, input_truncated,
              duration_ms, error_code, created_at, completed_at
            ) VALUES(
              ?, ?, 'gateway', ?, ?, 'virtual_key', ?, ?, ?, ?, ?, 10000, ?, ?,
              'intent-rule-engine-v1', 'intent-rules-v1', 128, 0, 2, ?,
              '2026-08-03T12:00:00Z', '2026-08-03T12:00:00Z'
            )
            """,
            (
                result_id,
                tenant_id,
                request_id,
                trace_id,
                virtual_key_id,
                result_status,
                mode,
                risk_type,
                risk_score,
                action,
                reason_code,
                error_code,
            ),
        )

    schedule_reason = {
        "queued": "queued",
        "skipped": "queue_full",
        "blocked": result_values["blocked"][3],
    }[schedule_status]
    schedule_risk_type = result_values["blocked"][0] if schedule_status == "blocked" else None
    schedule_risk_score = result_values["blocked"][1] if schedule_status == "blocked" else None
    schedule_action = "block" if schedule_status == "blocked" else None
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, status, error_code,
          intent_risk_result_id, intent_risk_schedule_status, intent_risk_mode,
          intent_risk_type, intent_risk_score, intent_risk_action,
          intent_risk_reason_code, intent_risk_detector_version,
          intent_risk_input_truncated, created_at, updated_at
        ) VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent-rule-engine-v1', 0,
          '2026-08-03T12:00:00Z', '2026-08-03T12:00:00Z'
        )
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            virtual_key_id,
            "failed" if schedule_status == "blocked" else "success",
            "gateway.intent_blocked" if schedule_status == "blocked" else None,
            result_id,
            schedule_status,
            mode,
            schedule_risk_type,
            schedule_risk_score,
            schedule_action,
            schedule_reason,
        ),
    )
    return request_id


@pytest.mark.anyio
async def test_prd069_request_log_detail_maps_all_intent_risk_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PAGE-097 后端合同精确区分七种状态，且只公开安全摘要。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    owner = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert owner is not None
    cases = {
        "pending": ("queued", None),
        "clean": ("queued", "clean"),
        "detected": ("queued", "detected"),
        "blocked": ("blocked", "blocked"),
        "failed": ("queued", "failed"),
        "skipped": ("skipped", None),
    }
    request_ids = {
        expected_status: insert_prd069_intent_request_log(
            connection,
            tenant_id=tenant_id,
            virtual_key_id=owner["virtual_key_id"],
            case_name=expected_status,
            schedule_status=schedule_status,
            result_status=result_status,
        )
        for expected_status, (schedule_status, result_status) in cases.items()
    }
    connection.commit()
    connection.close()

    control_app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control_app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        responses = {
            "disabled": await client.get(
                "/request-logs/req_prd016_success",
                headers={"Authorization": f"Bearer {token}"},
            )
        }
        for expected_status, source_request_id in request_ids.items():
            responses[expected_status] = await client.get(
                f"/request-logs/{source_request_id}",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert set(responses) == {
        "disabled",
        "pending",
        "clean",
        "detected",
        "blocked",
        "failed",
        "skipped",
    }
    for expected_status, response in responses.items():
        assert response.status_code == 200, response.text
        intent_risk = response.json()["request_log"]["intent_risk"]
        assert intent_risk["status"] == expected_status
        assert set(intent_risk) == {
            "intent_risk_result_id",
            "status",
            "classification_mode",
            "risk_type",
            "risk_score",
            "risk_score_scale",
            "action",
            "reason_code",
            "detector_version",
            "ruleset_version",
            "input_code_points",
            "input_truncated",
            "duration_ms",
            "completed_at",
        }
        assert intent_risk["risk_score_scale"] == 10000
        assert "actor_id" not in response.text
        assert "source_trace_id" not in response.text
    assert responses["pending"].json()["request_log"]["intent_risk"]["completed_at"] is None
    assert responses["detected"].json()["request_log"]["intent_risk"]["action"] == "observe"
    assert responses["blocked"].json()["request_log"]["intent_risk"]["action"] == "block"
    failed_intent = responses["failed"].json()["request_log"]["intent_risk"]
    assert failed_intent["reason_code"] == "classifier_failed"


@pytest.mark.anyio
async def test_prd069_request_log_detail_rejects_cross_owner_intent_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """嵌套结果 ID 即使存在，也不能跨 tenant/source owner 拼接到当前日志。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role,
          status, created_at, updated_at
        ) VALUES(
          'user_prd069_other', 'tenant_other_prd016', 'other-prd069@example.com',
          'Other PRD069', ?, 'admin', 'active',
          '2026-08-03T12:00:00Z', '2026-08-03T12:00:00Z'
        )
        """,
        (hash_password("OtherLocal!2026"),),
    )
    connection.execute(
        """
        INSERT INTO intent_risk_results(
          intent_risk_result_id, tenant_id, source_kind, source_request_id,
          source_trace_id, actor_type, actor_id, status, classification_mode,
          risk_type, risk_score, risk_score_scale, action, reason_code,
          detector_version, ruleset_version, input_code_points, input_truncated,
          duration_ms, error_code, created_at, completed_at
        ) VALUES(
          'irr_prd069_wrong_owner', 'tenant_other_prd016', 'manual',
          'req_prd069_other_manual', 'trc_prd069_other_manual', 'user',
          'user_prd069_other', 'clean', 'manual_sync', 'none', 0, 10000,
          'allow', 'no_risk_detected', 'intent-rule-engine-v1', 'intent-rules-v1',
          12, 0, 1, NULL, '2026-08-03T12:00:00Z', '2026-08-03T12:00:00Z'
        )
        """
    )
    # queued 本来允许结果尚未生成，因此数据库 trigger 不读取结果表；详情层必须补做三项 owner 复核。
    connection.execute(
        """
        UPDATE request_logs
        SET intent_risk_result_id='irr_prd069_wrong_owner',
            intent_risk_schedule_status='queued', intent_risk_mode='async_light',
            intent_risk_reason_code='queued',
            intent_risk_detector_version='intent-rule-engine-v1',
            intent_risk_input_truncated=0
        WHERE tenant_id=? AND request_id='req_prd016_success'
        """,
        (tenant_id,),
    )
    connection.commit()
    connection.close()

    control_app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control_app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.get(
            "/request-logs/req_prd016_success",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "intent_risk.result_integrity_failed"
    assert "tenant_other_prd016" not in response.text
    assert "req_prd069_other_manual" not in response.text


def test_prd069_request_log_detail_rejects_execution_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """完整性流程：同 tenant/request 的结果也必须绑定 trace、actor、模式、版本与截断标志。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    owner = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert owner is not None
    request_id = insert_prd069_intent_request_log(
        connection,
        tenant_id=tenant_id,
        virtual_key_id=owner["virtual_key_id"],
        case_name="metadata_drift",
        schedule_status="queued",
        result_status="detected",
    )
    connection.commit()
    request_log_row = connection.execute(
        """
        SELECT trace_id,virtual_key_id,intent_risk_result_id,intent_risk_schedule_status,
               intent_risk_mode,intent_risk_action,intent_risk_reason_code,
               intent_risk_detector_version,intent_risk_input_truncated
        FROM request_logs WHERE tenant_id=? AND request_id=?
        """,
        (tenant_id, request_id),
    ).fetchone()
    assert request_log_row is not None
    trusted = dict(request_log_row)
    mutations = (
        {"trace_id": "trc_prd069_forged"},
        {"virtual_key_id": "vk_prd069_forged"},
        {"intent_risk_mode": "sync_strong"},
        {"intent_risk_detector_version": "intent-rule-engine-forged"},
        {"intent_risk_input_truncated": 1},
    )
    for mutation in mutations:
        with pytest.raises(ApiError) as raised:
            _load_intent_risk(
                connection,
                tenant_id,
                request_id,
                {**trusted, **mutation},  # type: ignore[arg-type]
            )
        assert raised.value.code == "intent_risk.result_integrity_failed"
    connection.close()


def test_prd069_failed_closed_without_result_preserves_explicit_none_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """失败关闭无 result 时仍返回冻结动作 none，不能与尚未执行的 pending 空动作混淆。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    owner = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert owner is not None
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,'req_prd069_failed_closed_detail','trc_prd069_failed_closed_detail',?,
                 NULL,'disabled','2026-08-03T12:10:00Z')
        """,
        (tenant_id, owner["virtual_key_id"]),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,status,error_code,
          snapshot_version,policy_snapshot_status,
          intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
          intent_risk_type,intent_risk_score,intent_risk_action,intent_risk_reason_code,
          intent_risk_detector_version,intent_risk_input_truncated,created_at,updated_at
        ) VALUES('req_prd069_failed_closed_detail',?,
                 'trc_prd069_failed_closed_detail',?,'failed',
                 'gateway.intent_result_store_unavailable',NULL,'disabled',NULL,
                 'failed_closed','sync_strong',NULL,NULL,'none','result_store_unavailable',
                 'intent-rule-engine-v1',0,'2026-08-03T12:10:00Z','2026-08-03T12:10:00Z')
        """,
        (tenant_id, owner["virtual_key_id"]),
    )
    connection.commit()
    row = connection.execute(
        """
        SELECT trace_id,virtual_key_id,intent_risk_result_id,intent_risk_schedule_status,
               intent_risk_mode,intent_risk_action,intent_risk_reason_code,
               intent_risk_detector_version,intent_risk_input_truncated
        FROM request_logs
        WHERE tenant_id=? AND request_id='req_prd069_failed_closed_detail'
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None

    intent_risk = _load_intent_risk(
        connection,
        tenant_id,
        "req_prd069_failed_closed_detail",
        row,
    )
    connection.close()

    assert intent_risk.status == "failed"
    assert intent_risk.action == "none"
    assert intent_risk.reason_code == "result_store_unavailable"
    assert intent_risk.intent_risk_result_id is None
