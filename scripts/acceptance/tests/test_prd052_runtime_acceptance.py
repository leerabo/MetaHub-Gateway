"""PRD-052 Runtime 验收器的纯合同测试。

测试不启动服务、不访问端口、不执行迁移，也不写真实业务库；只冻结回环目标、批次数据库
硬边界、环境口令清除、统一链路字段、Session 哈希绑定、证据隐私与 write-once 语义。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database


# 文件名包含连字符，测试通过显式模块加载避免触发命令行 main 分支。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd052-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd052_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块只暴露纯函数和类；导入阶段不会连接网络、数据库或启动任何服务。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _response(
    *,
    status: int,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    cache_control: str = "no-store, private",
) -> httpx.Response:
    """构造带真实 Request 的最小 httpx 响应，供统一链路和安全投影测试。"""

    return httpx.Response(
        status,
        headers={
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Cache-Control": cache_control,
        },
        json={"request_id": request_id, "trace_id": trace_id, **payload},
        request=httpx.Request("GET", "http://localhost:8001/budgets"),
    )


def _runtime_config(batch: Path, database: Path) -> object:
    """构造不含真实凭据的最小配置，供纯 SQLite 绑定测试使用。"""

    return RUNTIME.RuntimeConfig(
        control_origin="http://localhost:8001",
        gateway_origin="http://localhost:8000",
        artifact_directory=batch,
        database_path=database,
        tenant_slug="default",
        admin_email="admin@metahub.local",
        admin_password="UnitOnly!2026",
        timeout_seconds=2.0,
    )


def _unused_transport(_request: httpx.Request) -> httpx.Response:
    """在误发网络请求时立即失败，证明纯合同测试没有访问任何端口。"""

    # 纯合同测试不得静默访问本机服务；任何 transport 调用都说明测试边界已被突破。
    raise AssertionError("contract test must not issue HTTP requests")


def test_loopback_origins_reject_external_credentials_and_paths() -> None:
    """Control/Gateway origin 只接受显式端口的 HTTP 回环地址。"""

    assert (
        RUNTIME.normalize_loopback_origin(
            "http://localhost:8001",
            stage="unit.origin",
        )
        == "http://localhost:8001"
    )
    assert (
        RUNTIME.normalize_loopback_origin(
            "http://127.0.0.1:18000/",
            stage="unit.origin",
        )
        == "http://127.0.0.1:18000"
    )
    for value in (
        "https://localhost:8001",
        "http://example.com:8001",
        "http://admin:plain@localhost:8001",
        "http://localhost:8001/budgets",
        "http://localhost:8001?next=outside",
        "http://localhost",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.normalize_loopback_origin(value, stage="unit.origin")


def test_artifact_and_database_are_hard_bound_to_one_runtime_batch(tmp_path: Path) -> None:
    """数据库 URL 必须精确指向当前 PRD-052 单层批次中的 runtime.db。"""

    workspace = tmp_path / "MetaHub"
    batch = workspace / "artifacts" / "prd052" / "unit-batch01"
    database = batch / "runtime.db"
    resolved_batch = RUNTIME.resolve_artifact_directory(
        batch,
        workspace_root=workspace,
    )
    database_url = RUNTIME.sqlite_url_for_path(database)
    assert resolved_batch == batch.resolve()
    assert RUNTIME.resolve_runtime_database(
        database_url,
        artifact_directory=resolved_batch,
    ) == database.resolve()

    invalid_directories = (
        workspace / "artifacts" / "prd052",
        batch / "nested",
        workspace / "artifacts" / "prd051" / "unit-batch01",
    )
    for invalid in invalid_directories:
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.resolve_artifact_directory(
                invalid,
                workspace_root=workspace,
            )
    invalid_urls = (
        RUNTIME.sqlite_url_for_path(batch / "other.db"),
        RUNTIME.sqlite_url_for_path(workspace / ".data" / "development.db"),
        f"{database_url}?mode=rw",
        "sqlite://external/runtime.db",
    )
    for invalid in invalid_urls:
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.resolve_runtime_database(
                invalid,
                artifact_directory=resolved_batch,
            )


def test_parse_config_uses_environment_password_and_separates_origins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析器不提供口令参数，并在返回前删除专用与通用口令环境变量。"""

    workspace = tmp_path / "MetaHub"
    batch = workspace / "artifacts" / "prd052" / "unit-batch01"
    database = batch / "runtime.db"
    monkeypatch.setattr(RUNTIME, "_workspace_root", lambda: workspace)
    environment = {
        "METAHUB_PRD052_ADMIN_PASSWORD": "UnitOnly!2026",
        "METAHUB_ADMIN_PASSWORD": "FallbackMustDisappear!2026",
        "METAHUB_PRD052_ADMIN_EMAIL": "ADMIN@METAHUB.LOCAL",
        "METAHUB_PRD052_TENANT_SLUG": "default",
    }
    arguments = [
        "--control-url",
        "http://localhost:8001",
        "--gateway-url",
        "http://localhost:8000",
        "--artifact-dir",
        str(batch),
        "--database-url",
        RUNTIME.sqlite_url_for_path(database),
    ]
    config = RUNTIME.parse_config(arguments, environ=environment)
    assert config.admin_password == "UnitOnly!2026"
    assert config.admin_email == "admin@metahub.local"
    assert config.database_path == database.resolve()
    assert "METAHUB_PRD052_ADMIN_PASSWORD" not in environment
    assert "METAHUB_ADMIN_PASSWORD" not in environment

    with pytest.raises(SystemExit):
        RUNTIME.parse_config(
            [*arguments, "--admin-password", "forbidden"],
            environ={"METAHUB_PRD052_ADMIN_PASSWORD": "UnitOnly!2026"},
        )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.parse_config(
            [
                *arguments,
                "--gateway-url",
                "http://localhost:8001",
            ],
            environ={"METAHUB_PRD052_ADMIN_PASSWORD": "UnitOnly!2026"},
        )


def test_success_and_error_responses_require_chain_fields_and_budget_no_store() -> None:
    """成功与错误响应都必须回显 Header/正文链路，预算响应还必须禁止缓存。"""

    success = _response(
        status=200,
        request_id="req_prd052_unit_success",
        trace_id="trc_prd052_unit_success",
        payload={"budget_rules": []},
    )
    payload = RUNTIME.verify_response_contract(
        success,
        stage="unit.success",
        expected_status=200,
        request_id="req_prd052_unit_success",
        trace_id="trc_prd052_unit_success",
        require_no_store=True,
    )
    assert payload["budget_rules"] == []

    forbidden = _response(
        status=403,
        request_id="req_prd052_unit_forbidden",
        trace_id="trc_prd052_unit_forbidden",
        payload={"code": "auth.permission_denied"},
    )
    assert (
        RUNTIME.verify_response_contract(
            forbidden,
            stage="unit.forbidden",
            expected_status=403,
            request_id="req_prd052_unit_forbidden",
            trace_id="trc_prd052_unit_forbidden",
            expected_code="auth.permission_denied",
        )["code"]
        == "auth.permission_denied"
    )


def test_response_contract_rejects_missing_cache_and_mismatched_request_id() -> None:
    """HTTP 状态正确但 no-store 或 request_id 不一致时仍必须失败关闭。"""

    missing_cache = _response(
        status=200,
        request_id="req_prd052_unit_cache",
        trace_id="trc_prd052_unit_cache",
        payload={},
        cache_control="private",
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            missing_cache,
            stage="unit.cache",
            expected_status=200,
            request_id="req_prd052_unit_cache",
            trace_id="trc_prd052_unit_cache",
            require_no_store=True,
        )

    mismatched = _response(
        status=402,
        request_id="req_prd052_unit_actual",
        trace_id="trc_prd052_unit_actual",
        payload={"code": "gateway.budget_exhausted"},
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            mismatched,
            stage="unit.mismatch",
            expected_status=402,
            request_id="req_prd052_unit_expected",
            trace_id="trc_prd052_unit_actual",
            expected_code="gateway.budget_exhausted",
        )


def test_http_helper_rejects_absolute_targets_before_transport() -> None:
    """相对路径门禁在 httpx transport 前拒绝绝对 URL，避免认证头被二次定向。"""

    with httpx.Client(
        base_url="http://localhost:8001",
        transport=httpx.MockTransport(_unused_transport),
    ) as client:
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME._safe_http_call(
                client,
                "GET",
                "http://example.com/budgets",
                stage="unit.http",
                headers={"Authorization": "Bearer unit-only"},
            )


def test_login_role_mismatch_keeps_provisional_session_for_cleanup(tmp_path: Path) -> None:
    """登录已建 Session 后角色校验失败时，仍登记有效 Token 供 finally 主动注销。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-batch01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    access_value = "unit-provisional-session"
    password = "UnitOnly!2026"

    def login_transport(request: httpx.Request) -> httpx.Response:
        """返回链路合法但角色错误的登录响应，模拟 Session 已落库后的本地校验失败。"""

        request_id = request.headers["X-Request-ID"]
        trace_id = request.headers["X-Trace-ID"]
        return httpx.Response(
            200,
            headers={
                "X-Request-ID": request_id,
                "X-Trace-ID": trace_id,
                "Cache-Control": "no-store",
            },
            json={
                "request_id": request_id,
                "trace_id": trace_id,
                "access_token": access_value,
                "session_id": "ses_unit_provisional",
                "user": {
                    "tenant_id": "tenant_unit",
                    "user_id": "usr_unit",
                    "role": "viewer",
                },
            },
            request=request,
        )

    config = _runtime_config(batch, database)
    with (
        httpx.Client(
            base_url=config.control_origin,
            transport=httpx.MockTransport(login_transport),
        ) as control,
        httpx.Client(
            base_url=config.gateway_origin,
            transport=httpx.MockTransport(_unused_transport),
        ) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        with pytest.raises(RUNTIME.AcceptanceFailure) as captured:
            acceptance._login(
                role="developer",
                email="developer@example.com",
                password=password,
            )

    assert captured.value.stage == "auth.login_developer"
    assert captured.value.code == "role_mismatch"
    credential = acceptance.sessions["developer"]
    assert credential.access_value == access_value
    assert credential.session_id == "ses_unit_provisional"
    assert credential.tenant_id == "tenant_unit"
    assert credential.user_id == "usr_unit"
    assert access_value in acceptance.known_sensitive
    assert password in acceptance.known_sensitive


def test_logout_all_publishes_browser_compatible_aggregate(tmp_path: Path) -> None:
    """三类 Session 全部注销后同时保留角色结果和 Browser 使用的聚合布尔值。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-batch01"
    batch.mkdir(parents=True)
    config = _runtime_config(batch, batch / "runtime.db")
    with (
        httpx.Client(
            base_url=config.control_origin,
            transport=httpx.MockTransport(_unused_transport),
        ) as control,
        httpx.Client(
            base_url=config.gateway_origin,
            transport=httpx.MockTransport(_unused_transport),
        ) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        for role in ("admin", "developer", "viewer"):
            acceptance.sessions[role] = RUNTIME.SessionCredential(
                role=role,
                tenant_id="tenant_unit",
                user_id=f"usr_{role}",
                session_id=f"ses_{role}",
                access_value=f"unit-{role}-session",
                password="UnitOnly!2026",
            )

        def revoke_without_network(role: str) -> bool:
            """用角色白名单替代真实 HTTP/SQLite，单独冻结 logout_all 的聚合投影。"""

            return role in {"admin", "developer", "viewer"}

        acceptance._logout_one = revoke_without_network
        result = acceptance.logout_all()

    assert result == {
        "viewer_session_revoked": True,
        "developer_session_revoked": True,
        "admin_session_revoked": True,
        "session_revoked": True,
    }


def test_environment_alias_source_matches_internal_model_not_alias_name(
    tmp_path: Path,
) -> None:
    """复制 dev 路由时按底层模型标识选择，不能被同名外层 Alias 误导。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-batch01"
    batch.mkdir(parents=True)
    config = _runtime_config(batch, batch / "runtime.db")
    posted_aliases: list[dict[str, object]] = []

    def control_transport(request: httpx.Request) -> httpx.Response:
        """返回一条同名干扰 Alias 和一条真实 default-chat 底层模型路由。"""

        request_id = request.headers["X-Request-ID"]
        trace_id = request.headers["X-Trace-ID"]
        payload: dict[str, object]
        status = 200
        if request.method == "GET" and request.url.path == "/environments":
            payload = {
                "environments": [
                    {
                        "environment_id": "env_dev",
                        "code": "dev",
                        "status": "active",
                    }
                ]
            }
        elif request.method == "GET" and request.url.path == "/model-aliases":
            payload = {
                "model_aliases": [
                    {
                        "alias": "default-chat",
                        "default_internal_model_id": "other-model",
                        "default_model_id": "mdl_decoy",
                        "status": "active",
                        "default_model_status": "active",
                        "default_provider_status": "active",
                    },
                    {
                        "alias": "prd036-resilient-chat",
                        "default_internal_model_id": "default-chat",
                        "default_model_id": "mdl_target",
                        "status": "active",
                        "default_model_status": "active",
                        "default_provider_status": "active",
                    },
                ]
            }
        elif request.method == "POST" and request.url.path == "/environments":
            status = 201
            payload = {
                "environment": {
                    "tenant_id": "tenant_unit",
                    "environment_id": "env_test",
                    "code": "test",
                    "status": "active",
                }
            }
        elif request.method == "POST" and request.url.path == "/model-aliases":
            status = 201
            posted = json.loads(request.content.decode("utf-8"))
            assert isinstance(posted, dict)
            posted_aliases.append(posted)
            payload = {
                "model_alias": {
                    "tenant_id": "tenant_unit",
                    "model_alias_id": "ma_test",
                    **posted,
                    "default_model_status": "active",
                    "default_provider_status": "active",
                }
            }
        else:
            # 合同桩只允许四条预期调用；出现其他 URL 说明验收器越过了冻结的本地边界。
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return httpx.Response(
            status,
            headers={
                "X-Request-ID": request_id,
                "X-Trace-ID": trace_id,
                "Cache-Control": "no-store",
            },
            json={"request_id": request_id, "trace_id": trace_id, **payload},
            request=request,
        )

    with (
        httpx.Client(
            base_url=config.control_origin,
            transport=httpx.MockTransport(control_transport),
        ) as control,
        httpx.Client(
            base_url=config.gateway_origin,
            transport=httpx.MockTransport(_unused_transport),
        ) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        acceptance.resource_ids["tenant_id"] = "tenant_unit"
        acceptance.sessions["admin"] = RUNTIME.SessionCredential(
            role="admin",
            tenant_id="tenant_unit",
            user_id="usr_admin",
            session_id="ses_admin",
            access_value="unit-admin-session",
            password="UnitOnly!2026",
        )
        result = acceptance.select_environment_and_model()

    assert result["default_model_route_active"] is True
    assert acceptance.resource_ids["environment_id"] == "env_test"
    assert posted_aliases == [
        {
            "alias": "default-chat",
            "environment_id": "env_test",
            "default_model_id": "mdl_target",
            "description": "PRD-052 isolated runtime acceptance",
            "status": "active",
        }
    ]


def test_formal_usage_insert_uses_valid_legacy_price_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在真实 0042 临时库中验证受控 Usage、价格证据和正式分录同事务落账。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-formal"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    database_url = RUNTIME.sqlite_url_for_path(database)
    # Seed 口令只用于当前临时库初始化；测试不登录，也不会把它写入证据或日志。
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "UnitOnly!2026")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)

    project_id = "prj_prd052_runtime"
    application_id = "app_prd052_runtime"
    created_at = "2026-07-27T11:00:00Z"
    connection = connect_database(database_url)
    try:
        key = connection.execute(
            """
            SELECT virtual_key_id, environment_id, status
            FROM virtual_keys
            WHERE tenant_id=?
            ORDER BY virtual_key_id
            LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert key is not None and key["status"] == "active"
        virtual_key_id = str(key["virtual_key_id"])
        environment_id = str(key["environment_id"])
        connection.execute(
            """
            INSERT INTO projects(
              project_id, tenant_id, project_code, name, status, created_at, updated_at
            )
            VALUES(?, ?, 'prd052-runtime', 'PRD052 runtime', 'active', ?, ?)
            """,
            (project_id, seed.tenant_id, created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO applications(
              application_id, tenant_id, project_id, application_code, name,
              status, created_at, updated_at
            )
            VALUES(
              ?, ?, ?, 'prd052-runtime', 'PRD052 runtime', 'active', ?, ?
            )
            """,
            (
                application_id,
                seed.tenant_id,
                project_id,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            UPDATE virtual_keys
            SET project_id=?, application_id=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (project_id, application_id, seed.tenant_id, virtual_key_id),
        )
        connection.commit()
    finally:
        connection.close()

    config = _runtime_config(batch, database)
    with (
        httpx.Client(
            base_url=config.control_origin,
            transport=httpx.MockTransport(_unused_transport),
        ) as control,
        httpx.Client(
            base_url=config.gateway_origin,
            transport=httpx.MockTransport(_unused_transport),
        ) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        acceptance.suffix = "unitformal052"
        acceptance.resource_ids.update(
            {
                "tenant_id": seed.tenant_id,
                "environment_id": environment_id,
                "project_id": project_id,
                "application_id": application_id,
                "virtual_key_id": virtual_key_id,
            }
        )
        assert acceptance.verify_runtime_database_ready()[
            "migration_0042_applied"
        ] is True
        result = acceptance.insert_formal_project_usage()

    assert result == {
        "posted_usage_charge_verified": True,
        "full_scope_snapshot_verified": True,
    }
    usage_id = acceptance.resource_ids["usage_ledger_id"]
    entry_id = acceptance.resource_ids["finops_ledger_entry_id"]
    request_id, _ = RUNTIME._request_identity("formal_usage", acceptance.suffix)
    snapshot_id = f"prs_prd052_accept_{acceptance.suffix}"
    connection = connect_database(database_url)
    try:
        price = connection.execute(
            """
            SELECT source, price_schema_version
            FROM price_snapshots
            WHERE tenant_id=? AND price_snapshot_id=?
            """,
            (seed.tenant_id, snapshot_id),
        ).fetchone()
        usage = connection.execute(
            """
            SELECT price_schema_version, virtual_key_id, environment_id,
                   project_id, application_id
            FROM usage_ledger
            WHERE tenant_id=? AND usage_ledger_id=?
            """,
            (seed.tenant_id, usage_id),
        ).fetchone()
        entry = connection.execute(
            """
            SELECT entry_type, direction, status, normalized_cny_micros,
                   price_evidence_status, virtual_key_id, environment_id,
                   project_id, application_id
            FROM finops_ledger_entries
            WHERE tenant_id=? AND finops_ledger_entry_id=?
            """,
            (seed.tenant_id, entry_id),
        ).fetchone()
        counts = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM gateway_request_claims WHERE request_id=?),
              (SELECT COUNT(*) FROM request_logs WHERE request_id=?),
              (SELECT COUNT(*) FROM price_snapshots WHERE price_snapshot_id=?),
              (SELECT COUNT(*) FROM usage_ledger WHERE usage_ledger_id=?),
              (SELECT COUNT(*) FROM finops_ledger_entries
               WHERE finops_ledger_entry_id=?)
            """,
            (request_id, request_id, snapshot_id, usage_id, entry_id),
        ).fetchone()
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert price is not None
    assert price["source"] == RUNTIME.FORMAL_USAGE_PRICE_SNAPSHOT_SOURCE
    assert price["price_schema_version"] == RUNTIME.FORMAL_USAGE_PRICE_SCHEMA_VERSION
    assert usage is not None
    assert usage["price_schema_version"] == RUNTIME.FORMAL_USAGE_PRICE_SCHEMA_VERSION
    assert [
        usage["virtual_key_id"],
        usage["environment_id"],
        usage["project_id"],
        usage["application_id"],
    ] == [virtual_key_id, environment_id, project_id, application_id]
    assert entry is not None
    assert [
        entry["entry_type"],
        entry["direction"],
        entry["status"],
        entry["normalized_cny_micros"],
        entry["price_evidence_status"],
    ] == ["usage_charge", "debit", "posted", 1_000_000, "legacy_amount_only"]
    assert [
        entry["virtual_key_id"],
        entry["environment_id"],
        entry["project_id"],
        entry["application_id"],
    ] == [virtual_key_id, environment_id, project_id, application_id]
    assert counts is not None and list(counts) == [1, 1, 1, 1, 1]
    assert foreign_key_issues == []


def test_session_binding_requires_same_database_token_hash_and_role(tmp_path: Path) -> None:
    """同库 Session 必须同时匹配租户、用户、角色和不可逆 Token 哈希。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-batch01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    token = "unit-session-value"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE users(
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              role TEXT NOT NULL,
              PRIMARY KEY(tenant_id, user_id)
            );
            CREATE TABLE sessions(
              session_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              token_hash TEXT NOT NULL,
              status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO users VALUES('tenant_unit', 'usr_unit', 'developer')"
        )
        connection.execute(
            "INSERT INTO sessions VALUES('ses_unit', 'tenant_unit', 'usr_unit', ?, 'active')",
            (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
        )

    config = _runtime_config(batch, database)
    with (
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as control,
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        acceptance.sessions["developer"] = RUNTIME.SessionCredential(
            role="developer",
            tenant_id="tenant_unit",
            user_id="usr_unit",
            session_id="ses_unit",
            access_value=token,
            password="UnitOnly!2026",
        )
        assert acceptance.verify_session_database_binding("developer") is True
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE sessions SET token_hash=? WHERE session_id='ses_unit'",
                ("0" * 64,),
            )
        with pytest.raises(RUNTIME.AcceptanceFailure):
            acceptance.verify_session_database_binding("developer")


def test_readiness_refuses_unmigrated_database_without_mutation(tmp_path: Path) -> None:
    """缺少 0042 的文件立即失败，验收器不得自行补表、迁移或改变版本记录。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-batch01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES('0041', 'applied')"
        )

    config = _runtime_config(batch, database)
    with (
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as control,
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        with pytest.raises(RUNTIME.AcceptanceFailure):
            acceptance.verify_runtime_database_ready()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version, status FROM schema_migrations"
        ).fetchall() == [("0041", "applied")]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 1


def test_safe_evidence_contract_and_frozen_browser_fields() -> None:
    """证据保留 Browser 所需安全字段，同时拒绝已知明文和敏感字段槽位。"""

    evidence = {
        "schema_version": 1,
        "prd_id": "PRD-052",
        "status": "passed",
        "generated_resources": {
            "tenant_id": "tenant_unit",
            "environment_id": "env_unit",
            "project_id": "prj_unit",
            "application_id": "app_unit",
            "virtual_key_id": "vk_unit",
            "budget_rule_ids": {
                "tenant": "br_tenant",
                "project": "br_project",
                "virtual_key": "br_key",
            },
            "usage_ledger_id": "usg_unit",
            "finops_ledger_entry_id": "fle_unit",
        },
        "checks": {
            "budget_status": {
                "expected_split": {
                    "ledger_spent_cny": "1",
                    "reserved_cny": "0",
                    "effective_spend_cny": "1",
                },
                "expected_split_scope_types": [
                    "tenant",
                    "project",
                    "virtual_key",
                ],
                "project_exhausted_verified": True,
            }
        },
    }
    RUNTIME.assert_safe_evidence(evidence, sensitive_values=("not-present",))
    for unsafe, known in (
        ({"admin_password": "plain"}, ()),
        ({"status": "known-sensitive"}, ("known-sensitive",)),
        ({"status": "Bearer opaque-session"}, ()),
        ({"status": "mhv_complete_virtual_key_value"}, ()),
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_evidence(unsafe, sensitive_values=known)


def test_evidence_publish_is_write_once(tmp_path: Path) -> None:
    """最终 JSON 只允许独占创建，同一批次重跑不得覆盖既有通过文件。"""

    batch = tmp_path / "artifacts" / "prd052" / "unit-batch01"
    batch.mkdir(parents=True)
    evidence = {"schema_version": 1, "status": "passed", "prd_id": "PRD-052"}
    target = RUNTIME.publish_evidence(batch, evidence)
    assert target.read_text(encoding="utf-8").endswith("\n")
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.publish_evidence(batch, evidence)
