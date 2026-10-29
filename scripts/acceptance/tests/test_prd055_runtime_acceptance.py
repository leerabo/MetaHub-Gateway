"""PRD-055 Runtime 验收器的纯沙箱合同测试。

测试只使用 MockTransport 与 pytest 在 D:\\MetaHub\\.tmp 下分配的临时数据库，不访问本地服务、
外部网络或开发数据库；重点证明验收证据与正式迁移、真实 actor 和精确夹具规模绑定。
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest


# 脚本文件名包含连字符，显式加载可避免导入阶段误执行命令行入口。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd055-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd055_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块固定到当前工作区脚本，防止误用环境中安装的同名包。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _runtime_config(batch: Path, database: Path) -> object:
    """构造只含单元测试凭据的配置，所有持久文件由 pytest 临时目录隔离。"""

    return RUNTIME.RuntimeConfig(
        control_origin="http://localhost:8001",
        gateway_origin="http://localhost:8000",
        console_origin="http://localhost:5173",
        artifact_directory=batch,
        database_path=database,
        tenant_slug="default",
        admin_email="admin@metahub.local",
        admin_password="UnitOnly!2026",
        timeout_seconds=2.0,
    )


def _unused_transport(_request: httpx.Request) -> httpx.Response:
    """任何 HTTP 调用都立即失败，证明合同测试没有偷访真实服务端口。"""

    # 合同测试若触发网络即表示隔离边界失守，不能返回伪造响应继续执行。
    raise AssertionError("contract test must not issue HTTP requests")


def _acceptance(config: object) -> tuple[object, tuple[httpx.Client, ...]]:
    """构造三个 MockTransport 客户端，并把句柄交给调用方显式关闭。"""

    clients = tuple(
        httpx.Client(transport=httpx.MockTransport(_unused_transport))
        for _ in range(3)
    )
    return RUNTIME.RuntimeAcceptance(config, *clients), clients


def _credential(
    *,
    session_key: str = "developer",
    tenant_id: str = "tenant_unit",
    user_id: str = "user_unit",
    session_id: str = "session_unit",
) -> object:
    """生成仅供内存绑定断言使用的短生命周期 Session 身份。"""

    # 夹具只驻留测试进程内存，不写入数据库、终端或最终验收证据。
    return RUNTIME.SessionCredential(
        session_key=session_key,
        role="developer",
        tenant_slug="default",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        access_value="opaque-unit-token",
        email="unit@metahub.local",
        password="UnitOnly!2026",
    )


def _create_query_event_table(database: Path) -> None:
    """建立 QueryEvent 验证器读取所需的最小列集合，不复制生产触发器语义。"""

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE request_log_query_events(
              query_event_id TEXT, event_id TEXT, tenant_id TEXT,
              actor_user_id TEXT, actor_session_id TEXT, filter_version INTEGER,
              filter_fingerprint TEXT, filter_keys_json TEXT, saved_filter_id TEXT,
              sort_by TEXT, sort_direction TEXT, query_limit INTEGER,
              query_offset INTEGER, matched_count INTEGER, returned_count INTEGER,
              has_more INTEGER, query_duration_ms INTEGER, operation_request_id TEXT,
              operation_trace_id TEXT, event_dispatch_status TEXT,
              event_dispatch_attempts INTEGER
            )
            """
        )


def test_config_removes_both_password_variables_and_rejects_link_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主密码命中时 fallback 也必须移除，证据批次路径包含链接节点则拒绝。"""

    root = tmp_path / "MetaHub"
    batch = root / "artifacts" / "prd055" / "unit-batch01"
    batch.mkdir(parents=True)
    monkeypatch.setattr(RUNTIME, "_path_contains_link", lambda *_args, **_kwargs: True)
    with pytest.raises(RUNTIME.AcceptanceFailure) as link_error:
        RUNTIME.resolve_artifact_directory(batch, workspace_root=root)
    assert link_error.value.code == "symlink"

    monkeypatch.setattr(RUNTIME, "resolve_artifact_directory", lambda _value: batch)
    monkeypatch.setattr(
        RUNTIME,
        "resolve_runtime_database",
        lambda _value, *, artifact_directory: artifact_directory / "runtime.db",
    )
    environment = {
        "METAHUB_PRD055_ADMIN_PASSWORD": "primary-secret",
        "METAHUB_ADMIN_PASSWORD": "fallback-secret",
    }
    config = RUNTIME.parse_config(
        ["--artifact-dir", "ignored", "--database-url", "ignored"],
        environ=environment,
    )
    assert config.admin_password == "primary-secret"
    assert "METAHUB_PRD055_ADMIN_PASSWORD" not in environment
    assert "METAHUB_ADMIN_PASSWORD" not in environment


def test_readiness_binds_checksum_to_official_migration(tmp_path: Path) -> None:
    """0045 记录必须等于正式迁移文件摘要，任意合法长度哈希不能冒充已验收迁移。"""

    migration = SCRIPT_PATH.parents[2] / "infra" / "migrations" / RUNTIME.MIGRATION_FILENAME
    expected = hashlib.sha256(
        migration.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    assert RUNTIME._migration_checksum() == expected

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version TEXT, status TEXT, checksum TEXT)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES('0045', 'applied', ?)",
            (expected,),
        )
    config = _runtime_config(batch, database)
    acceptance, clients = _acceptance(config)
    try:
        with pytest.raises(RUNTIME.AcceptanceFailure) as missing_objects:
            acceptance.verify_runtime_database_ready()
        assert missing_objects.value.code == "table_missing"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version='0045'",
                ("0" * 64,),
            )
        with pytest.raises(RUNTIME.AcceptanceFailure) as tampered:
            acceptance.verify_runtime_database_ready()
        assert tampered.value.code == "migration_checksum"
    finally:
        for client in clients:
            client.close()


def test_required_objects_exactly_match_migration_source() -> None:
    """readiness 的索引与触发器清单必须完整覆盖 0045，不能漏掉并发唯一约束。"""

    migration = SCRIPT_PATH.parents[2] / "infra" / "migrations" / RUNTIME.MIGRATION_FILENAME
    sql = migration.read_text(encoding="utf-8")
    indexes = frozenset(
        re.findall(r"^CREATE(?: UNIQUE)? INDEX ([A-Za-z0-9_]+)", sql, re.MULTILINE)
    )
    triggers = frozenset(
        re.findall(r"^CREATE TRIGGER ([A-Za-z0-9_]+)", sql, re.MULTILINE)
    )
    assert RUNTIME.REQUIRED_INDEXES == indexes
    assert RUNTIME.REQUIRED_TRIGGERS == triggers


def test_privacy_gate_and_query_expectation_bind_real_session(tmp_path: Path) -> None:
    """隐私门禁拒绝认证身份字段，查询预期则绑定真实 Session 与 canonical 指纹。"""

    for unsafe in (
        {"actor_session_id": "session_private"},
        {"access_token": "opaque"},
        {"password_hash": "hash"},
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_payload(unsafe)

    batch = tmp_path / "batch"
    batch.mkdir()
    config = _runtime_config(batch, batch / "runtime.db")
    acceptance, clients = _acceptance(config)
    try:
        acceptance.successful_query_request_ids = []
        acceptance.sessions["developer"] = _credential()
        operation_id = "req_prd055_unit_query"
        trace_id = "trc_prd055_unit_query"
        acceptance.operation_trace_ids[operation_id] = trace_id
        filters = {"v": 1, "provider_code": "deepseek", "status": "failed"}
        payload = {
            "query_event_id": "lqe_" + "1" * 32,
            "request_logs": [],
            "total": 0,
            "has_more": False,
        }
        acceptance._remember_query_audit(
            operation_id,
            session_key="developer",
            filters=filters,
            payload=payload,
            saved_filter_id=None,
            sort_by="created_at",
            sort_direction="desc",
            query_limit=20,
            query_offset=0,
        )
        expected = acceptance.query_audit_expectations[operation_id]
        canonical = '{"provider_code":"deepseek","status":"failed","v":1}'
        assert expected.tenant_id == "tenant_unit"
        assert expected.actor_user_id == "user_unit"
        assert expected.actor_session_id == "session_unit"
        assert expected.operation_trace_id == trace_id
        assert expected.filter_keys == ("provider_code", "status")
        assert expected.filter_fingerprint == hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
    finally:
        for client in clients:
            client.close()


def test_query_event_requires_actor_binding_and_each_dispatch_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QueryEvent 必须逐条匹配 actor/trace/result，且每条 dispatched 事件至少 claim 一次。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    _create_query_event_table(database)
    operation_id = "req_prd055_audit_unit"
    query_event_id = "lqe_" + "2" * 32
    expectation = RUNTIME.QueryAuditExpectation(
        query_event_id=query_event_id,
        tenant_id="tenant_unit",
        actor_user_id="user_unit",
        actor_session_id="session_unit",
        operation_trace_id="trc_prd055_audit_unit",
        filter_fingerprint="a" * 64,
        filter_keys=("status",),
        saved_filter_id=None,
        sort_by="created_at",
        sort_direction="desc",
        query_limit=20,
        query_offset=0,
        matched_count=1,
        returned_count=1,
        has_more=False,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO request_log_query_events VALUES(
              ?, ?, 'tenant_wrong', 'user_unit', 'session_unit', 1, ?,
              '["status"]', NULL, 'created_at', 'desc', 20, 0, 1, 1, 0, 2,
              ?, 'trc_prd055_audit_unit', 'dispatched', 1
            )
            """,
            (
                query_event_id,
                "lqevt_" + "3" * 32,
                "a" * 64,
                operation_id,
            ),
        )
    config = _runtime_config(batch, database)
    acceptance, clients = _acceptance(config)
    acceptance.successful_query_request_ids = [operation_id]
    acceptance.query_audit_expectations[operation_id] = expectation
    monkeypatch.setattr(acceptance, "verify_database_immutability", lambda _event_id: None)
    try:
        with pytest.raises(RUNTIME.AcceptanceFailure) as actor_error:
            acceptance.verify_query_events_and_outbox()
        assert actor_error.value.code == "actor_binding"

        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                UPDATE request_log_query_events
                SET tenant_id='tenant_unit', event_dispatch_attempts=0
                """
            )
        with pytest.raises(RUNTIME.AcceptanceFailure) as attempt_error:
            acceptance.verify_query_events_and_outbox()
        assert attempt_error.value.code == "attempts"

        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE request_log_query_events SET event_dispatch_attempts=1"
            )
        result = acceptance.verify_query_events_and_outbox()
        assert result["owner_session_bound"] is True
        assert result["attempts_bounded"] is True
    finally:
        for client in clients:
            client.close()


def test_performance_fixture_requires_exact_claim_and_log_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """性能规模同时依赖日志、claim 和目标分布，缺一条 claim 也不得报告通过。"""

    monkeypatch.setattr(RUNTIME, "PERFORMANCE_FIXTURE_ROWS", 4)
    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    config = _runtime_config(batch, database)
    acceptance, clients = _acceptance(config)
    prefix = "req_fixture_"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE request_logs(
              request_id TEXT, tenant_id TEXT, trace_id TEXT, request_model TEXT,
              total_tokens INTEGER, currency TEXT, price_schema_version TEXT,
              status TEXT, provider_code TEXT, error_code TEXT, latency_ms INTEGER
            )
            """
        )
        connection.execute(
            "CREATE TABLE gateway_request_claims(request_id TEXT, tenant_id TEXT, trace_id TEXT)"
        )
        for index in range(4):
            request_id = f"{prefix}{index}"
            target = index == 0
            connection.execute(
                "INSERT INTO gateway_request_claims VALUES(?, 'tenant_unit', ?)",
                (request_id, f"trace_{index}"),
            )
            connection.execute(
                """
                INSERT INTO request_logs VALUES(
                  ?, 'tenant_unit', ?, 'enterprise-chat', 10, 'CNY', 'prd055-v1',
                  ?, ?, ?, ?
                )
                """,
                (
                    request_id,
                    f"trace_{index}",
                    "failed" if target else "success",
                    "deepseek" if index % 2 == 0 else "qwen",
                    "gateway.upstream_error" if target else None,
                    index,
                ),
            )
        facts = acceptance._verify_performance_fixture(
            connection,
            tenant_id="tenant_unit",
            fixture_prefix=prefix,
        )
        assert facts == {"row_count": 4, "target_count": 1}
        connection.execute(
            "DELETE FROM gateway_request_claims WHERE request_id=?",
            (f"{prefix}3",),
        )
        with pytest.raises(RUNTIME.AcceptanceFailure) as incomplete:
            acceptance._verify_performance_fixture(
                connection,
                tenant_id="tenant_unit",
                fixture_prefix=prefix,
            )
        assert incomplete.value.code == "row_contract"
    finally:
        connection.close()
        for client in clients:
            client.close()


def test_logout_continues_after_one_failure_and_clears_sensitive_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个注销失败时其余 Session 仍需撤销，随后必须清空所有明文引用。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    config = _runtime_config(batch, batch / "runtime.db")
    acceptance, clients = _acceptance(config)
    calls: list[str] = []
    for key in ("viewer", "developer", "tenant_b_admin", "admin"):
        acceptance.sessions[key] = _credential(session_key=key, session_id=f"session_{key}")

    def logout_one(session_key: str) -> bool:
        """记录注销顺序，并用 viewer 模拟不会泄露正文的固定失败。"""

        calls.append(session_key)
        # 首个身份失败用于证明后续三次注销仍会继续执行。
        if session_key == "viewer":
            raise RUNTIME.AcceptanceFailure("unit.logout", "failed")
        return True

    monkeypatch.setattr(acceptance, "_logout_one", logout_one)
    try:
        result = acceptance.logout_all()
        assert calls == ["viewer", "developer", "tenant_b_admin", "admin"]
        assert result["viewer_session_revoked"] is False
        assert result["admin_session_revoked"] is True
        assert result["session_revoked"] is False
        acceptance.clear_sensitive_memory()
        assert acceptance.known_sensitive == []
        assert all(not item.access_value and not item.password for item in acceptance.sessions.values())
    finally:
        for client in clients:
            client.close()


def test_secondary_fixture_rejects_cross_tenant_user_id_collision(
    tmp_path: Path,
) -> None:
    """确定性夹具 user_id 冲突时不得改写原租户用户，并应回滚新建第二租户。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    timestamp = "2026-07-28T00:00:00Z"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tenants(
              tenant_id TEXT PRIMARY KEY, slug TEXT UNIQUE, name TEXT,
              service_mode TEXT, status TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE users(
              user_id TEXT PRIMARY KEY, tenant_id TEXT, email TEXT,
              display_name TEXT, password_hash TEXT, role TEXT, status TEXT,
              created_at TEXT, updated_at TEXT, failed_login_count INTEGER,
              locked_until TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO tenants VALUES(
              'tenant_default', 'default', 'Default', 'internal_only',
              'active', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO users VALUES(
              'user_prd055_collision', 'tenant_default', 'owner@metahub.local',
              'Original owner', 'original-hash', 'viewer', 'active', ?, ?, 0, NULL
            )
            """,
            (timestamp, timestamp),
        )

    config = _runtime_config(batch, database)
    acceptance, clients = _acceptance(config)
    acceptance.fixture_marker = "collision"
    acceptance.suffix = "collision"
    try:
        with pytest.raises(RUNTIME.AcceptanceFailure) as conflict:
            acceptance.prepare_fixtures()
        assert conflict.value.code == "identity_conflict"
        with sqlite3.connect(database) as connection:
            original = connection.execute(
                """
                SELECT tenant_id, email, password_hash, role
                FROM users WHERE user_id='user_prd055_collision'
                """
            ).fetchone()
            assert original == (
                "tenant_default",
                "owner@metahub.local",
                "original-hash",
                "viewer",
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM tenants WHERE tenant_id='tenant_prd055_collision'"
            ).fetchone()[0] == 0
    finally:
        for client in clients:
            client.close()
