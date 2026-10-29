"""PRD-071 Control 输出安全与 FinOps 读模型专项集成测试。

本文件只验证 RequestLog/FinOps 的公开白名单、后端权限、租户隔离和完整性失败关闭。
测试库位于 pytest 的工作区 basetemp；正文、规则 pattern、Token 和密钥均不进入响应断言。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.finops.routes import router as finops_router
from metahub_control.request_logs.routes import router as request_logs_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app


# 以下密码和稳定 ID 只属于隔离测试库；响应断言不得输出访问令牌或密码哈希。
ADMIN_PASSWORD = "Prd071Admin!2026"
DEVELOPER_PASSWORD = "Prd071Developer!2026"
FINANCE_PASSWORD = "Prd071Finance!2026"
VIEWER_PASSWORD = "Prd071Viewer!2026"
OTHER_ADMIN_PASSWORD = "Prd071OtherAdmin!2026"
OTHER_TENANT_ID = "tenant_prd071_other"
OTHER_TENANT_SLUG = "prd071-other"
FINGERPRINT = "a" * 64


def _url(path: Path) -> str:
    """把隔离数据库路径转换为项目 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含认证、RequestLog 和 FinOps 的真实中间件应用。"""

    app = create_service_app("control-plane-api", "PRD-071 Control Read Model Test")
    app.include_router(auth_router)
    app.include_router(request_logs_router)
    app.include_router(finops_router)
    return app


@pytest.fixture(scope="module")
def prd071_control_template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """只迁移一次并准备角色/第二租户，后续用例复制关闭状态模板库。

    参数：pytest 模块级临时目录工厂。返回：模板路径、默认 tenant 和身份图。
    关键约束：每个测试仍使用独立数据库文件，损坏夹具不能污染其他用例。
    """

    root = tmp_path_factory.mktemp("prd071-control-template")
    database = root / "template.db"
    database_url = _url(database)
    # seed 管理员密码只存在隔离测试环境，不会写入业务日志或公开 DTO。
    import os

    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        apply_migrations(database_url)
        seed = apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    connection = connect_database(database_url)
    identity_row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,key.virtual_key_id,
               environment.environment_id,environment.code AS environment_code,
               model.model_id,provider.provider_id,provider.provider_code
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN virtual_keys key ON key.tenant_id=tenant.tenant_id
        JOIN environments environment
          ON environment.tenant_id=tenant.tenant_id AND environment.code='dev'
        JOIN models model ON model.tenant_id=tenant.tenant_id
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        WHERE tenant.tenant_id=?
        ORDER BY user.user_id,key.virtual_key_id,model.model_id
        LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert identity_row is not None
    identity = {key: str(identity_row[key]) for key in identity_row.keys()}
    now = "2026-08-04T10:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        [
            (
                "user_prd071_developer", seed.tenant_id, "developer@prd071.local",
                "PRD071 Developer", hash_password(DEVELOPER_PASSWORD), "developer", now, now,
            ),
            (
                "user_prd071_finance", seed.tenant_id, "finance@prd071.local",
                "PRD071 Finance", hash_password(FINANCE_PASSWORD), "finance_admin", now, now,
            ),
            (
                "user_prd071_viewer", seed.tenant_id, "viewer@prd071.local",
                "PRD071 Viewer", hash_password(VIEWER_PASSWORD), "viewer", now, now,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,'PRD-071 other tenant','internal_only','active',?,?)
        """,
        (OTHER_TENANT_ID, OTHER_TENANT_SLUG, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd071_other_admin',?,'admin@prd071-other.local','Other Admin',?,
                 'admin','active',?,?)
        """,
        (OTHER_TENANT_ID, hash_password(OTHER_ADMIN_PASSWORD), now, now),
    )
    connection.commit()
    connection.close()
    # 仅返回模板路径和非敏感 owner ID，供用例复制与参数绑定使用。
    return {"database": database, "tenant_id": seed.tenant_id, "identity": identity}


def _clone_database(
    template: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, dict[str, str]]:
    """复制模板并把 Control 配置限定到用例专属数据库。"""

    destination = tmp_path / "prd071-control.db"
    shutil.copy2(template["database"], destination)
    database_url = _url(destination)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd071-control-auth-secret")
    # 聚焦读模型测试不依赖异步查询事件投递；关闭 dispatcher 可避免后台轮询干扰隔离库。
    monkeypatch.setenv("METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED", "false")
    return database_url, dict(template["identity"])


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录隔离测试用户并返回仅用于 Authorization 头的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只在调用栈中返回给 Authorization 头，不写业务日志或测试输出。
    return response.json()["access_token"]


# 时间边界必须取自实际迁移记录，不能用当前时钟猜测历史/迁移后事实。
def _migration_times(connection) -> tuple[str, str]:
    """读取 0063 时间并返回前后各一分钟。

    参数：用例专属 SQLite 连接。返回：迁移前、迁移后 UTC 文本。
    关键约束：两个时间均由数据库 applied_at 派生，避免测试受执行时区影响。
    """

    row = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version='0063' AND status='applied'"
    ).fetchone()
    assert row is not None
    applied = datetime.fromisoformat(str(row["applied_at"]).replace("Z", "+00:00"))
    if applied.tzinfo is None:
        applied = applied.replace(tzinfo=UTC)
    return _db_time(applied - timedelta(minutes=1)), _db_time(applied + timedelta(minutes=1))


# 所有夹具时间统一归一到 UTC，保持 migration boundary 比较确定。
def _db_time(value: datetime) -> str:
    """规范化数据库时间。

    参数：带时区 datetime。返回：SQLite 接受的 UTC ``Z`` 文本。
    关键约束：不得丢失微秒，否则边界用例可能与 applied_at 相等。
    """

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _filter_token(filters: dict[str, object]) -> str:
    """编码 RequestLog v1 inline filters。

    参数：不含 tenant 的白名单筛选。返回：无填充 Base64URL 文本。
    关键约束：测试通过真实 wire 格式覆盖 normalize_filters，而不是直接调用内部函数。
    """

    payload = json.dumps({"v": 1, **filters}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _insert_request(
    connection,
    identity: dict[str, str],
    suffix: str,
    *,
    created_at: str,
    status: str = "failed",
    error_code: str | None = "content_policy_violation",
    endpoint: str | None = "/v1/chat/completions",
) -> tuple[str, str]:
    """插入同租户 claim 与 RequestLog，并返回稳定 request/trace。

    参数：连接、身份图、后缀、时间和公开终态。返回：request/trace。
    关键约束：owner 只来自身份图，日志不包含 Prompt、Response 或任何密钥。
    """

    request_id = f"req_prd071_control_{suffix}"
    trace_id = f"trc_prd071_control_{suffix}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (
            identity["tenant_id"], request_id, trace_id,
            identity["virtual_key_id"], created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
          endpoint,request_model,resolved_model,provider_code,status,error_code,
          input_tokens,output_tokens,total_tokens,estimated_cost_amount,currency,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,3,2,5,1.25,'CNY',?,?)
        """,
        (
            request_id, identity["tenant_id"], trace_id, identity["virtual_key_id"],
            identity["provider_id"], identity["model_id"], endpoint,
            "default-chat", "default-chat", identity["provider_code"], status,
            error_code, created_at, created_at,
        ),
    )
    return request_id, trace_id


def _insert_rule(connection, identity: dict[str, str], suffix: str, created_at: str) -> str:
    """插入只含占位 pattern 的 output DLP 规则并返回规则 ID。"""

    rule_id = f"dlp_prd071_control_{suffix}"
    pattern = f"fixture-{suffix}"
    connection.execute(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,status,
          priority,scan_scope,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'keyword',?,?,'block','active',10,'output',?,?,?,?)
        """,
        (
            rule_id, identity["tenant_id"], f"PRD071 {suffix}", pattern,
            hashlib.sha256(pattern.encode("ascii")).hexdigest(), identity["user_id"],
            identity["user_id"], created_at, created_at,
        ),
    )
    return rule_id


# scan 夹具严格走生产 pending→终态 trigger，不直接插入终态绕过状态机。
def _insert_scan(
    connection,
    identity: dict[str, str],
    request_id: str,
    trace_id: str,
    hex_character: str,
    *,
    created_at: str,
    scan_status: str,
    decision: str,
    rule_id: str | None = None,
) -> str:
    """写入非流式输出扫描。

    参数：连接、身份、request/trace、ID 字符、时间和目标状态。返回：scan ID。
    关键约束：先提交 pending 形状，再以唯一 UPDATE 进入冻结终态。
    """

    scan_id = "oss_" + hex_character * 32
    connection.execute(
        """
        INSERT INTO output_safety_scans(
          output_safety_scan_id,tenant_id,request_id,trace_id,scan_mode,
          scan_status,decision,match_count,detector_version,checkpoint_count,
          observed_output_token_count,scanned_token_count,emitted_token_count,
          emitted_sse_event_count,emitted_bytes,exposure_state,billing_disposition,started_at
        ) VALUES(?,?,?,?,'non_stream_full','pending','none',0,'output-safety-dlp-v1',
                 0,0,0,0,0,0,'unknown','not_required',?)
        """,
        (scan_id, identity["tenant_id"], request_id, trace_id, created_at),
    )
    if scan_status == "completed" and decision == "blocked":
        connection.execute(
            """
            UPDATE output_safety_scans
            SET scan_status='completed',decision='blocked',action='block',
                dlp_rule_id=?,rule_type='keyword',match_count=1,
                ruleset_fingerprint=?,checkpoint_count=1,
                observed_output_token_count=2,scanned_token_count=2,
                exposure_state='none',billing_disposition='dispute_pending',completed_at=?
            WHERE output_safety_scan_id=?
            """,
            (rule_id, FINGERPRINT, created_at, scan_id),
        )
    elif scan_status == "unavailable" and decision == "unknown":
        connection.execute(
            """
            UPDATE output_safety_scans
            SET scan_status='unavailable',decision='unknown',ruleset_fingerprint=?,
                exposure_state='unknown',failure_reason_code='output_safety.stream_incomplete',
                completed_at=?
            WHERE output_safety_scan_id=?
            """,
            (FINGERPRINT, created_at, scan_id),
        )
    else:
        # 测试 helper 只允许本文件明确覆盖的终态，避免夹具静默生成非法组合。
        raise AssertionError("unsupported PRD-071 scan fixture")
    return scan_id


def _insert_usage_charge(
    connection,
    identity: dict[str, str],
    request_id: str,
    suffix: str,
    created_at: str,
) -> tuple[str, str]:
    """写入真实价格/Usage，让既有 trigger 自动生成正式 usage_charge。"""

    snapshot_id = f"prs_prd071_control_{suffix}"
    usage_id = f"usg_prd071_control_{suffix}"
    connection.execute(
        """
        INSERT INTO price_snapshots(
          price_snapshot_id,tenant_id,request_id,model_id,provider_id,provider_code,
          currency,input_price_per_1k,output_price_per_1k,price_schema_version,
          billing_dimensions,provider_raw_mapping,source,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'CNY','0.001000','0.002000','price-v1','{}','{}',
                 'model_catalog','active',?,?)
        """,
        (
            snapshot_id, identity["tenant_id"], request_id, identity["model_id"],
            identity["provider_id"], identity["provider_code"], created_at, created_at,
        ),
    )
    normalized = {
        "input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "cache_tokens": 0,
        "cost_amount": "1.250000", "cost_cny": "1.250000", "cost_usd": "0",
        "currency": "CNY", "price_schema_version": "price-v1",
        "price_snapshot_id": snapshot_id, "posting_source": "direct",
    }
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
          cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,currency,
          price_schema_version,estimated,usage_source,price_snapshot_id,model_id,
          provider_id,virtual_key_id,environment_id,environment_code,provider_code,
          created_at,updated_at
        ) VALUES(?,?,?,3,2,5,0,'{}',?,'1.250000','1.250000','0','CNY','price-v1',
                 0,'upstream',?,?,?,?,?,?,?,?,?)
        """,
        (
            usage_id, identity["tenant_id"], request_id,
            json.dumps(normalized, separators=(",", ":")), snapshot_id,
            identity["model_id"], identity["provider_id"], identity["virtual_key_id"],
            identity["environment_id"], identity["environment_code"],
            identity["provider_code"], created_at, created_at,
        ),
    )
    charge = connection.execute(
        """
        SELECT finops_ledger_entry_id FROM finops_ledger_entries
        WHERE tenant_id=? AND source_request_id=? AND usage_ledger_id=?
        """,
        (identity["tenant_id"], request_id, usage_id),
    ).fetchone()
    assert charge is not None
    return usage_id, str(charge["finops_ledger_entry_id"])


# dispute 夹具只关联同 owner 的 blocked scan、Usage 与正式 charge。
def _insert_dispute(
    connection,
    identity: dict[str, str],
    request_id: str,
    trace_id: str,
    scan_id: str,
    usage_id: str,
    entry_id: str,
    hex_character: str,
    created_at: str,
) -> None:
    """写入账单复核 marker。

    参数：同事务连接、身份图、request/trace、scan/Usage/charge ID 与时间。返回：无。
    关键约束：只创建 dispute_pending，不创建自动 reversal 或修改正式费用。
    """

    connection.execute(
        """
        INSERT INTO finops_ledger_disputes(
          finops_ledger_dispute_id,tenant_id,request_id,trace_id,output_safety_scan_id,
          usage_ledger_id,finops_ledger_entry_id,review_status,reversal_eligible,
          reason_code,created_at
        ) VALUES(?,?,?,?,?,?,?,'dispute_pending',1,
                 'output_safety.content_policy_violation',?)
        """,
        (
            "fld_" + hex_character * 32, identity["tenant_id"], request_id, trace_id,
            scan_id, usage_id, entry_id, created_at,
        ),
    )


@pytest.mark.anyio
async def test_output_safety_stage_detail_and_finops_minimal_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd071_control_template: dict[str, object],
) -> None:
    """主流程：安全终态不遗漏 FinOps 503，详情/账本只返回各自白名单字段。"""

    database_url, identity = _clone_database(prd071_control_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    _, after = _migration_times(connection)
    rule_id = _insert_rule(connection, identity, "blocked", after)
    blocked_request, blocked_trace = _insert_request(
        connection, identity, "blocked", created_at=after
    )
    blocked_scan = _insert_scan(
        connection, identity, blocked_request, blocked_trace, "1", created_at=after,
        scan_status="completed", decision="blocked", rule_id=rule_id,
    )
    usage_id, entry_id = _insert_usage_charge(
        connection, identity, blocked_request, "blocked", after
    )
    _insert_dispute(
        connection, identity, blocked_request, blocked_trace, blocked_scan,
        usage_id, entry_id, "1", after,
    )
    lost_request, lost_trace = _insert_request(
        connection, identity, "finops_lost", created_at=after,
        error_code="finops.usage_record_unavailable",
    )
    _insert_scan(
        connection, identity, lost_request, lost_trace, "2", created_at=after,
        scan_status="completed", decision="blocked", rule_id=rule_id,
    )
    unavailable_request, unavailable_trace = _insert_request(
        connection, identity, "unavailable", created_at=after,
        error_code="gateway.output_safety_unavailable",
    )
    _insert_scan(
        connection, identity, unavailable_request, unavailable_trace, "3", created_at=after,
        scan_status="unavailable", decision="unknown",
    )
    connection.commit()
    connection.close()

    app = _build_app()
    unavailable_filters = _filter_token(
        {
            "terminal_stage": "output_safety",
            "output_safety_status": "unavailable",
            "output_safety_mode": "non_stream_full",
            "output_safety_exposure": "unknown",
            "output_safety_billing": "not_required",
        }
    )
    # 27 个 v1 白名单键（含版本）必须能作为一个完整对象通过；这条请求同时验证
    # 0063 QueryEvent 触发器的 28 键容量不会在查询成功后把审计写入关闭失败。
    complete_filters = _filter_token(
        {
            "request_id": "req_prd071_complete_filter",
            "trace_id": "trc_prd071_complete_filter",
            "status": "failed",
            "request_model": "default-chat",
            "resolved_model": "default-chat",
            "provider_code": identity["provider_code"],
            "virtual_key_id": identity["virtual_key_id"],
            "environment_id": identity["environment_id"],
            "project_id": "project_prd071_filter",
            "application_id": "application_prd071_filter",
            "snapshot_version": 1,
            "error_code": "content_policy_violation",
            "created_from": "2026-08-04T00:00:00Z",
            "created_to": "2026-08-05T00:00:00Z",
            "latency_ms_min": 0,
            "latency_ms_max": 1000,
            "total_tokens_min": 0,
            "total_tokens_max": 1000,
            "cost_amount_min": "0",
            "cost_amount_max": "1000.000000",
            "currency": "CNY",
            "terminal_stage": "output_safety",
            "output_safety_status": "completed",
            "output_safety_mode": "non_stream_full",
            "output_safety_exposure": "none",
            "output_safety_billing": "dispute_pending",
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        finance = await _login(client, "finance@prd071.local", FINANCE_PASSWORD)
        developer = await _login(client, "developer@prd071.local", DEVELOPER_PASSWORD)
        listed = await client.get(
            "/request-logs?terminal_stage=output_safety&limit=10",
            headers={"Authorization": f"Bearer {admin}"},
        )
        composite = await client.get(
            "/request-logs?terminal_stage=output_safety&output_safety_status=completed"
            "&output_safety_mode=non_stream_full&output_safety_exposure=none"
            "&output_safety_billing=dispute_pending&limit=10",
            headers={"Authorization": f"Bearer {admin}"},
        )
        inline_unavailable = await client.get(
            f"/request-logs?filters={unavailable_filters}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        complete_filtered = await client.get(
            f"/request-logs?filters={complete_filters}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        invalid_filters = [
            await client.get(
                f"/request-logs?terminal_stage=output_safety&{key}=invalid",
                headers={"Authorization": f"Bearer {admin}"},
            )
            for key in (
                "output_safety_status", "output_safety_mode",
                "output_safety_exposure", "output_safety_billing",
            )
        ]
        detail = await client.get(
            f"/request-logs/{blocked_request}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        lost_detail = await client.get(
            f"/request-logs/{lost_request}",
            headers={"Authorization": f"Bearer {developer}"},
        )
        finance_ledger = await client.get(
            "/finops/ledger?limit=100",
            headers={"Authorization": f"Bearer {finance}"},
        )
        finance_request_log = await client.get(
            f"/request-logs/{blocked_request}",
            headers={"Authorization": f"Bearer {finance}"},
        )
        developer_finops = await client.get(
            "/finops/ledger",
            headers={"Authorization": f"Bearer {developer}"},
        )

    assert listed.status_code == 200, listed.text
    listed_rows = listed.json()["request_logs"]
    assert {row["request_id"] for row in listed_rows} == {
        blocked_request, lost_request, unavailable_request,
    }
    summary_keys = {
        "output_safety_scan_id", "scan_mode", "scan_status", "decision",
        "exposure_state", "emitted_token_count", "emitted_sse_event_count",
        "emitted_bytes", "billing_disposition",
    }
    assert all(set(row["output_safety"]) == summary_keys for row in listed_rows)
    assert next(row for row in listed_rows if row["request_id"] == lost_request)[
        "terminal_stage"
    ] == "output_safety"
    assert all("pattern" not in json.dumps(row) for row in listed_rows)
    assert composite.status_code == 200, composite.text
    assert {row["request_id"] for row in composite.json()["request_logs"]} == {
        blocked_request, lost_request,
    }
    assert inline_unavailable.status_code == 200, inline_unavailable.text
    assert [row["request_id"] for row in inline_unavailable.json()["request_logs"]] == [
        unavailable_request
    ]
    assert complete_filtered.status_code == 200, complete_filtered.text
    assert complete_filtered.json()["request_logs"] == []
    with connect_database(database_url) as connection:
        audit = connection.execute(
            "SELECT filter_keys_json FROM request_log_query_events "
            "WHERE operation_request_id=?",
            (complete_filtered.json()["request_id"],),
        ).fetchone()
    assert audit is not None
    complete_filter_keys = json.loads(str(audit["filter_keys_json"]))
    assert len(complete_filter_keys) == 26
    assert complete_filter_keys == sorted(complete_filter_keys)
    assert all(response.status_code == 422 for response in invalid_filters)
    assert all(
        response.json()["code"] == "request_log.query_invalid"
        for response in invalid_filters
    )

    assert detail.status_code == 200, detail.text
    output_safety = detail.json()["request_log"]["output_safety"]
    assert output_safety["output_safety_scan_id"] == blocked_scan
    assert output_safety["decision"] == "blocked"
    assert output_safety["dlp_rule_id"] == rule_id
    assert output_safety["dispute"] == {
        "output_safety_scan_id": blocked_scan,
        "review_status": "dispute_pending",
        "reversal_eligible": True,
    }
    assert "pattern" not in json.dumps(output_safety)
    assert lost_detail.status_code == 200, lost_detail.text
    assert lost_detail.json()["request_log"]["log"]["terminal_stage"] == "output_safety"
    assert lost_detail.json()["request_log"]["output_safety"]["dispute"] is None

    assert finance_ledger.status_code == 200, finance_ledger.text
    charge = next(
        row for row in finance_ledger.json()["ledger_entries"]
        if row["finops_ledger_entry_id"] == entry_id
    )
    assert charge["output_safety_dispute"] == {
        "output_safety_scan_id": blocked_scan,
        "review_status": "dispute_pending",
        "reversal_eligible": True,
    }
    assert set(charge["output_safety_dispute"]) == {
        "output_safety_scan_id", "review_status", "reversal_eligible"
    }
    assert rule_id not in json.dumps(charge)
    assert finance_request_log.status_code == 403
    assert finance_request_log.json()["code"] == "auth.permission_denied"
    assert developer_finops.status_code == 403
    assert developer_finops.json()["code"] == "auth.permission_denied"


@pytest.mark.anyio
async def test_output_safety_cross_tenant_request_is_404_and_viewer_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd071_control_template: dict[str, object],
) -> None:
    """租户/权限流程：跨租户 request ID 与不存在同形 404，Viewer 由后端拒绝。"""

    database_url, _identity = _clone_database(prd071_control_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    _, after = _migration_times(connection)
    other_request = "req_prd071_control_other"
    other_trace = "trc_prd071_control_other"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,NULL,NULL,'disabled',?)
        """,
        (OTHER_TENANT_ID, other_request, other_trace, after),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,endpoint,status,error_code,created_at,updated_at
        ) VALUES(?,?,?,'/v1/chat/completions','failed','gateway.output_safety_unavailable',?,?)
        """,
        (other_request, OTHER_TENANT_ID, other_trace, after, after),
    )
    other_scan = "oss_" + "5" * 32
    connection.execute(
        """
        INSERT INTO output_safety_scans(
          output_safety_scan_id,tenant_id,request_id,trace_id,scan_mode,scan_status,
          decision,match_count,detector_version,checkpoint_count,
          observed_output_token_count,scanned_token_count,emitted_token_count,
          emitted_sse_event_count,emitted_bytes,exposure_state,billing_disposition,started_at
        ) VALUES(?,?,?,?,'non_stream_full','pending','none',0,'output-safety-dlp-v1',
                 0,0,0,0,0,0,'unknown','not_required',?)
        """,
        (other_scan, OTHER_TENANT_ID, other_request, other_trace, after),
    )
    connection.execute(
        """
        UPDATE output_safety_scans
        SET scan_status='unavailable',decision='unknown',ruleset_fingerprint=?,
            failure_reason_code='output_safety.evidence_unavailable',completed_at=?
        WHERE tenant_id=? AND output_safety_scan_id=?
        """,
        (FINGERPRINT, after, OTHER_TENANT_ID, other_scan),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        other_admin = await _login(
            client, "admin@prd071-other.local", OTHER_ADMIN_PASSWORD,
            tenant_slug=OTHER_TENANT_SLUG,
        )
        viewer = await _login(client, "viewer@prd071.local", VIEWER_PASSWORD)
        cross = await client.get(
            f"/request-logs/{other_request}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        missing = await client.get(
            "/request-logs/req_prd071_control_missing",
            headers={"Authorization": f"Bearer {admin}"},
        )
        own = await client.get(
            f"/request-logs/{other_request}",
            headers={"Authorization": f"Bearer {other_admin}"},
        )
        denied = await client.get(
            "/request-logs?terminal_stage=output_safety",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        default_filtered = await client.get(
            "/request-logs?terminal_stage=output_safety&output_safety_status=unavailable",
            headers={"Authorization": f"Bearer {admin}"},
        )
        other_filtered = await client.get(
            "/request-logs?terminal_stage=output_safety&output_safety_status=unavailable",
            headers={"Authorization": f"Bearer {other_admin}"},
        )
    assert cross.status_code == missing.status_code == 404
    assert cross.json()["code"] == missing.json()["code"] == "request_log.not_found"
    assert own.status_code == 200, own.text
    assert own.json()["request_log"]["output_safety"]["scan_status"] == "unavailable"
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert default_filtered.status_code == 200, default_filtered.text
    assert default_filtered.json()["total"] == 0
    assert other_filtered.status_code == 200, other_filtered.text
    assert [row["request_id"] for row in other_filtered.json()["request_logs"]] == [
        other_request
    ]


@pytest.mark.anyio
async def test_output_safety_missing_scan_uses_migration_boundary_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd071_control_template: dict[str, object],
) -> None:
    """兼容流程：迁移前 Chat 标历史不可用，迁移后成功 Chat 缺 scan 返回 503。"""

    database_url, identity = _clone_database(prd071_control_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    before, after = _migration_times(connection)
    legacy_request, _ = _insert_request(
        connection, identity, "legacy", created_at=before, status="success", error_code=None
    )
    missing_request, _ = _insert_request(
        connection, identity, "missing", created_at=after, status="success", error_code=None
    )
    pre_provider_request, _ = _insert_request(
        connection, identity, "pre_provider", created_at=after,
        error_code="gateway.model_not_found", endpoint=None,
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        legacy = await client.get(
            f"/request-logs/{legacy_request}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        missing = await client.get(
            f"/request-logs/{missing_request}",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_control_missing_scan",
            },
        )
        pre_provider = await client.get(
            f"/request-logs/{pre_provider_request}",
            headers={"Authorization": f"Bearer {admin}"},
        )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["request_log"]["output_safety"]["scan_status"] == "legacy_unavailable"
    assert legacy.json()["request_log"]["output_safety"]["decision"] == "unknown"
    assert missing.status_code == 503
    assert missing.json()["code"] == "output_safety.integrity_failed"
    assert missing.json()["request_id"] == "req_control_missing_scan"
    assert missing.headers["cache-control"] == "no-store, private"
    assert pre_provider.status_code == 200, pre_provider.text
    assert pre_provider.json()["request_log"]["output_safety"]["scan_status"] == "not_applicable"


@pytest.mark.anyio
async def test_output_safety_corrupt_dispute_returns_503_for_request_log_and_finops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prd071_control_template: dict[str, object],
) -> None:
    """异常流程：dispute trace owner 污染时安全详情和财务徽标都失败关闭。"""

    database_url, identity = _clone_database(prd071_control_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    _, after = _migration_times(connection)
    rule_id = _insert_rule(connection, identity, "corrupt", after)
    request_id, trace_id = _insert_request(
        connection, identity, "corrupt", created_at=after
    )
    scan_id = _insert_scan(
        connection, identity, request_id, trace_id, "4", created_at=after,
        scan_status="completed", decision="blocked", rule_id=rule_id,
    )
    usage_id, entry_id = _insert_usage_charge(
        connection, identity, request_id, "corrupt", after
    )
    _insert_dispute(
        connection, identity, request_id, trace_id, scan_id, usage_id, entry_id, "4", after
    )
    connection.commit()
    # 在隔离库主动移除 immutable UPDATE trigger，模拟约束被误关后的运维污染；
    # 生产迁移和业务库均不受影响，Control 读边界仍必须识别 owner 不一致。
    connection.execute("DROP TRIGGER trg_finops_ledger_disputes_update_prd071")
    connection.execute(
        "UPDATE finops_ledger_disputes SET trace_id='trc_prd071_corrupt_owner' "
        "WHERE tenant_id=? AND request_id=?",
        (identity["tenant_id"], request_id),
    )
    connection.commit()
    connection.close()
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        finance = await _login(client, "finance@prd071.local", FINANCE_PASSWORD)
        detail = await client.get(
            f"/request-logs/{request_id}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        ledger = await client.get(
            "/finops/ledger?limit=100",
            headers={"Authorization": f"Bearer {finance}"},
        )
    assert detail.status_code == 503
    assert detail.json()["code"] == "output_safety.integrity_failed"
    assert ledger.status_code == 503
    assert ledger.json()["code"] == "output_safety.integrity_failed"
    assert "corrupt_owner" not in detail.text
    assert "corrupt_owner" not in ledger.text
