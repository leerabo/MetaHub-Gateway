"""PRD-056 Runtime 验收器的纯沙箱合同测试。

测试只使用 MockTransport、回环随机端口和 pytest 在 ``D:\\MetaHub\\.artifacts`` 下分配的
临时目录，不访问正在运行的服务、开发数据库或外部网络。
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from metahub_shared.db import apply_migrations
from metahub_shared.runtime.webhooks import webhook_signature_header

# 文件名含连字符，显式按物理路径加载可避免误用环境中同名模块。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd056-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location("prd056_runtime_acceptance", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态加载对象只用于沙箱合同测试，注册到 sys.modules 可保证 dataclass 解析稳定且不执行外部入口。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _config(batch: Path, database: Path) -> object:
    """构造只用于合同测试的本机配置，不包含生产或用户凭据。"""

    return RUNTIME.RuntimeConfig(
        control_origin="http://localhost:8001",
        gateway_origin="http://localhost:8000",
        console_origin="http://localhost:5173",
        artifact_directory=batch,
        database_path=database,
        tenant_slug="default",
        admin_email="admin@metahub.local",
        admin_password="UnitOnly!2026",
        virtual_key="unit-only-virtual-key-value",
        timeout_seconds=2.0,
    )


def _unused_transport(_request: httpx.Request) -> httpx.Response:
    """拒绝意外 HTTP 调用，证明合同测试不会触碰本地服务或外网。"""

    # 任意出网尝试都说明合同测试越界，应在发出真实请求前立即失败。
    raise AssertionError("contract test must not issue HTTP requests")


def _acceptance(batch: Path, database: Path) -> tuple[object, tuple[httpx.Client, ...]]:
    """创建三个 MockTransport 客户端，并把关闭责任交给调用方。"""

    clients = tuple(
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) for _ in range(3)
    )
    return RUNTIME.RuntimeAcceptance(_config(batch, database), *clients), clients


def test_loopback_and_batch_paths_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只接受显式 HTTP 回环端口和单层普通批次目录，链接判断必须失败关闭。"""

    assert (
        RUNTIME.normalize_loopback_origin("http://127.0.0.1:8123", stage="unit.origin")
        == "http://127.0.0.1:8123"
    )
    for value in (
        "https://127.0.0.1:8123",
        "http://example.com:8123",
        "http://user:pass@127.0.0.1:8123",
        "http://127.0.0.1:8123/path",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.normalize_loopback_origin(value, stage="unit.origin")

    root = tmp_path / "MetaHub"
    root.mkdir()
    batch = RUNTIME.resolve_artifact_directory(
        ".artifacts/prd056/unit-batch01", workspace_root=root
    )
    database = batch / "runtime.db"
    database.write_bytes(b"")
    monkeypatch.setattr(RUNTIME, "_workspace_root", lambda: root)
    assert (
        RUNTIME.resolve_runtime_database(
            f"sqlite:///{database.as_posix()}",
            artifact_directory=batch,
        )
        == database.resolve()
    )
    monkeypatch.setattr(RUNTIME, "_path_contains_link", lambda *_args, **_kwargs: True)
    with pytest.raises(RUNTIME.AcceptanceFailure) as linked:
        RUNTIME.resolve_runtime_database(
            f"sqlite:///{database.as_posix()}",
            artifact_directory=batch,
        )
    assert linked.value.code == "link"


def test_parse_config_removes_all_password_and_virtual_key_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主别名命中后备用凭据也必须清除，避免子进程继承残留明文。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    database.write_bytes(b"")
    log_paths = (batch / "control.log", batch / "gateway.log")
    for log_path in log_paths:
        log_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(RUNTIME, "resolve_artifact_directory", lambda _value: batch)
    monkeypatch.setattr(
        RUNTIME,
        "resolve_runtime_database",
        lambda _value, *, artifact_directory: database,
    )
    environment = {
        "METAHUB_PRD056_ADMIN_PASSWORD": "PrimarySecret!2026",
        "METAHUB_ADMIN_PASSWORD": "FallbackSecret!2026",
        "METAHUB_PRD056_VIRTUAL_KEY": "primary-unit-virtual-key",
        "METAHUB_SEED_VIRTUAL_KEY": "fallback-unit-virtual-key",
    }
    config = RUNTIME.parse_config(
        [
            "--artifact-dir",
            "ignored",
            "--log-file",
            str(log_paths[0]),
            "--log-file",
            str(log_paths[1]),
            "--database-url",
            "ignored",
        ],
        environ=environment,
    )
    assert config.admin_password == "PrimarySecret!2026"
    assert config.virtual_key == "primary-unit-virtual-key"
    assert not {
        "METAHUB_PRD056_ADMIN_PASSWORD",
        "METAHUB_ADMIN_PASSWORD",
        "METAHUB_PRD056_VIRTUAL_KEY",
        "METAHUB_SEED_VIRTUAL_KEY",
    } & set(environment)
    assert config.log_paths == tuple(path.resolve() for path in log_paths)


def test_database_readiness_binds_0046_checksum_and_builtin_catalog(
    tmp_path: Path,
) -> None:
    """用正式迁移器证明 checksum、完整 0046 schema 与 13 条 builtin 同时成立。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    database_url = f"sqlite:///{database.as_posix()}"
    migration = apply_migrations(database_url)
    # 该专项绑定的是 0046 文件 checksum，而非永久冻结全局迁移头；后续迁移必须可共存。
    assert "0046" in migration.applied
    # 本节点仍只绑定 0046 checksum/builtin 合同，但 fresh 数据库必须走到仓库当前正式头部。
    # 从正式 up 文件动态求头部，避免每个后续 PRD 增加迁移后把旧专项误报为业务回归。
    migration_root = RUNTIME._workspace_root() / "infra" / "migrations"
    expected_head = max(
        path.name.split("_", 1)[0]
        for path in migration_root.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert migration.applied[-1] == expected_head

    acceptance, clients = _acceptance(batch, database)
    try:
        evidence = acceptance.verify_database_ready()
        assert evidence["migration"] == "0046"
        assert evidence["builtin_rule_count"] == 13
        assert evidence["required_tables_verified"] == len(RUNTIME.REQUIRED_TABLES)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version='0046'",
                ("0" * 64,),
            )
        with pytest.raises(RUNTIME.AcceptanceFailure) as tampered:
            acceptance.verify_database_ready()
        assert tampered.value.code == "checksum"
    finally:
        for client in clients:
            client.close()


def test_response_and_evidence_privacy_contracts() -> None:
    """统一错误必须回传双链路标识，最终证据拒绝凭据、正文和已知明文。"""

    response = httpx.Response(
        409,
        json={
            "code": "error_map.version_conflict",
            "request_id": "req_unit",
            "trace_id": "trc_unit",
        },
    )
    payload = RUNTIME.verify_response(
        response,
        stage="unit.response",
        status=409,
        request_id="req_unit",
        trace_id="trc_unit",
        code="error_map.version_conflict",
    )
    assert payload["code"] == "error_map.version_conflict"
    RUNTIME.assert_safe_payload({"prd_id": "PRD-056", "status": "passed"})
    for unsafe, sensitive in (
        ({"password": "hidden"}, ()),
        ({"value": "Bearer abcdefghijklmnop"}, ()),
        ({"value": RUNTIME.PRIVATE_PROVIDER_SENTINEL}, ()),
        ({"value": "unit-secret-value"}, ("unit-secret-value",)),
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_payload(unsafe, sensitive_values=sensitive)


def test_mock_provider_is_loopback_bounded_and_silent() -> None:
    """受控上游只在随机回环端口返回固定错误，且不产生默认访问日志。"""

    state = RUNTIME.MockProviderState()
    state.set(
        status=429,
        error_type="rate_limit_error",
        error_code="rate_limit_error",
        message="unit-message",
    )
    server = RUNTIME.MockProviderServer(state)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            json={"model": "unit"},
            timeout=2,
            trust_env=False,
        )
        assert response.status_code == 429
        assert response.json()["error"]["type"] == "rate_limit_error"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_temporary_user_cleanup_is_tenant_qualified(tmp_path: Path) -> None:
    """同 user_id 碰撞时只停用账本声明的 tenant owner，并撤销其活动 Session。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE users(
              user_id TEXT, tenant_id TEXT, status TEXT, updated_at TEXT,
              PRIMARY KEY(tenant_id,user_id)
            );
            CREATE TABLE sessions(
              session_id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT,
              status TEXT, revoked_at TEXT
            );
            INSERT INTO users VALUES('shared-user','tenant-a','active','now');
            INSERT INTO users VALUES('shared-user','tenant-b','active','now');
            INSERT INTO sessions VALUES('session-a','tenant-a','shared-user','active',NULL);
            INSERT INTO sessions VALUES('session-b','tenant-b','shared-user','active',NULL);
            """
        )
    acceptance, clients = _acceptance(batch, database)
    acceptance.temporary_user_ids = [("tenant-a", "shared-user")]
    try:
        assert acceptance.disable_temporary_users() is True
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM users WHERE tenant_id='tenant-a'"
                ).fetchone()[0]
                == "disabled"
            )
            assert (
                connection.execute(
                    "SELECT status FROM users WHERE tenant_id='tenant-b'"
                ).fetchone()[0]
                == "active"
            )
            assert (
                connection.execute(
                    "SELECT status FROM sessions WHERE session_id='session-a'"
                ).fetchone()[0]
                == "revoked"
            )
            assert (
                connection.execute(
                    "SELECT status FROM sessions WHERE session_id='session-b'"
                ).fetchone()[0]
                == "active"
            )
    finally:
        for client in clients:
            client.close()


def test_publish_evidence_is_exclusive(tmp_path: Path) -> None:
    """Runtime manifest 只能独占创建，第二次运行不得覆盖首次验收事实。"""

    evidence = {"prd_id": "PRD-056", "status": "passed"}
    target = RUNTIME.publish_evidence(tmp_path, evidence)
    assert json.loads(target.read_text(encoding="utf-8")) == evidence
    with pytest.raises(RUNTIME.AcceptanceFailure) as duplicate:
        RUNTIME.publish_evidence(tmp_path, evidence)
    assert duplicate.value.code == "already_exists"


def test_runtime_manifest_binds_all_browser_request_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终 manifest 必须携带 Browser 所需的映射、非流和真实流式请求身份。"""

    database = tmp_path / "runtime.db"
    database.write_bytes(b"")
    acceptance, clients = _acceptance(tmp_path, database)
    acceptance.resource_ids.update(
        {
            "error_map_id": f"pem_{'1' * 32}",
            "version": "2",
            "head_revision": "2",
            "mapped_request_id": "req_p56_mapped_unit",
            "nonstream_request_id": "req_p56_nonstream_unit",
            "stream_request_id": "req_p56_stream_unit",
        }
    )
    for method_name in (
        "verify_database_ready",
        "verify_services",
        "prepare_identities",
        "verify_permissions",
        "verify_builtin_baseline",
        "create_rule_and_verify_tenant",
        "verify_gateway_and_facts",
        "verify_database_terminal",
    ):
        monkeypatch.setattr(acceptance, method_name, lambda: {"verified": True})
    monkeypatch.setattr(
        acceptance,
        "create_runtime_webhook",
        lambda _receiver: {"verified": True},
    )
    monkeypatch.setattr(
        acceptance,
        "verify_runtime_webhook",
        lambda _receiver: {"verified": True},
    )
    try:
        manifest = acceptance.run()
    finally:
        for client in clients:
            client.close()
    assert manifest["generated_resources"] == {
        "error_map_id": f"pem_{'1' * 32}",
        "error_map_version": 2,
        "head_revision": 2,
        "mapped_request_id": "req_p56_mapped_unit",
        "nonstream_request_id": "req_p56_nonstream_unit",
        "stream_request_id": "req_p56_stream_unit",
    }


def test_mock_provider_rejects_negative_content_length() -> None:
    """负 Content-Length 必须在读取正文前返回 413，禁止退化为 read(-1)。"""

    state = RUNTIME.MockProviderState()
    server = RUNTIME.MockProviderServer(state)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Length", "-1")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        assert response.read() == b""
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_error_map_webhook_signature_and_exact_schema() -> None:
    """只接受签名有效、字段精确且不含正文哨兵的 error_map.updated。"""

    secret = "whsec_unit_prd056_signing_secret"
    timestamp = 1_800_000_000
    event_id = "pemevt_" + "a" * 32
    map_id = "pem_" + "b" * 32
    delivery_id = "whd_" + "c" * 32
    envelope = {
        "event_id": event_id,
        "event_type": "error_map.updated",
        "timestamp": "2027-01-15T08:00:00Z",
        "tenant_id": "tenant_unit",
        "actor_id": "user_unit",
        "object_id": map_id,
        "trace_id": "trc_unit",
        "payload": {
            "schema_version": 1,
            "operation": "create",
            "provider_error_map_id": map_id,
            "rule_version": 1,
            "head_revision": 1,
            "provider_scope": "all",
            "status": "active",
            "priority": 100,
            "output_error_code": "gateway.upstream_resource_not_found",
            "pattern_hash": "d" * 64,
            "request_id": "req_unit",
        },
    }
    body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = webhook_signature_header(secret, timestamp, body)
    summary = RUNTIME.validate_error_map_webhook(
        body,
        signature=signature,
        signing_secret=secret,
        event_header="error_map.updated",
        delivery_header=delivery_id,
        now_seconds=timestamp,
    )
    assert summary["event_id"] == event_id
    assert summary["provider_error_map_id"] == map_id
    assert summary["signature_valid"] is True
    with pytest.raises(RUNTIME.AcceptanceFailure) as invalid:
        RUNTIME.validate_error_map_webhook(
            body,
            signature=signature[:-1] + ("0" if signature[-1] != "0" else "1"),
            signing_secret=secret,
            event_header="error_map.updated",
            delivery_header=delivery_id,
            now_seconds=timestamp,
        )
    assert invalid.value.code == "signature"


def test_source_event_receipts_bind_id_to_operation_version_and_revision() -> None:
    """每个 Webhook event_id 必须绑定数据库中的操作、规则版本和 head revision。"""

    map_id = "pem_" + "b" * 32
    rows = [
        {
            "provider_error_map_event_id": "pemevt_" + "1" * 32,
            "operation": "create",
            "rule_version": 1,
            "head_revision": 1,
        },
        {
            "provider_error_map_event_id": "pemevt_" + "2" * 32,
            "operation": "update",
            "rule_version": 2,
            "head_revision": 2,
        },
    ]
    summaries = [
        {
            "event_id": row["provider_error_map_event_id"],
            "provider_error_map_id": map_id,
            "operation": row["operation"],
            "rule_version": row["rule_version"],
            "head_revision": row["head_revision"],
            "signature_valid": True,
            "privacy_verified": True,
        }
        for row in rows
    ]
    assert (
        RUNTIME.verify_source_event_bindings(
            rows,
            summaries,
            provider_error_map_id=map_id,
        )
        == 2
    )

    drifted = [dict(item) for item in summaries]
    drifted[0]["operation"] = "update"
    with pytest.raises(RUNTIME.AcceptanceFailure) as failed:
        RUNTIME.verify_source_event_bindings(
            rows,
            drifted,
            provider_error_map_id=map_id,
        )
    assert failed.value.stage == "webhook.delivery"
    assert failed.value.code == "event_binding"


def test_provider_endpoint_restore_is_tenant_qualified(tmp_path: Path) -> None:
    """恢复临时 Endpoint 时必须同时绑定 tenant_id 与 provider_id。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE providers(tenant_id TEXT,provider_id TEXT,base_url TEXT,updated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO providers VALUES(?,?,?,?)",
            [
                ("tenant-a", "provider-shared", "http://127.0.0.1:9/v1", "now"),
                ("tenant-b", "provider-shared", "https://tenant-b.invalid/v1", "now"),
            ],
        )
    acceptance, clients = _acceptance(batch, database)
    acceptance.sessions["admin"] = RUNTIME.SessionCredential(
        "admin",
        "admin",
        "default",
        "tenant-a",
        "user-a",
        "session-a",
        "token-a",
        "admin@example.invalid",
        "password-a",
    )
    acceptance.resource_ids["provider_id"] = "provider-shared"
    acceptance.original_provider_base_url = "https://tenant-a.invalid/v1"
    acceptance.provider_endpoint_changed = True
    try:
        assert acceptance.restore_provider_endpoint() is True
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT tenant_id,base_url FROM providers ORDER BY tenant_id"
            ).fetchall()
        assert rows == [
            ("tenant-a", "https://tenant-a.invalid/v1"),
            ("tenant-b", "https://tenant-b.invalid/v1"),
        ]
    finally:
        for client in clients:
            client.close()


def test_runtime_webhook_cleanup_is_tenant_qualified(tmp_path: Path) -> None:
    """Runtime 回收只能禁用管理员租户内的目标 Webhook Endpoint。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE webhook_endpoints("
            "tenant_id TEXT,webhook_endpoint_id TEXT,status TEXT,updated_by TEXT,updated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO webhook_endpoints VALUES(?,?,?,?,?)",
            [
                ("tenant-a", "webhook-a", "active", "user-a", "now"),
                ("tenant-b", "webhook-b", "active", "user-b", "now"),
            ],
        )
    acceptance, clients = _acceptance(batch, database)
    acceptance.sessions["admin"] = RUNTIME.SessionCredential(
        "admin",
        "admin",
        "default",
        "tenant-a",
        "user-a",
        "session-a",
        "token-a",
        "admin@example.invalid",
        "password-a",
    )
    try:
        acceptance.resource_ids["webhook_endpoint_id"] = "webhook-a"
        assert acceptance.disable_runtime_webhook() is True
        acceptance.resource_ids["webhook_endpoint_id"] = "webhook-b"
        assert acceptance.disable_runtime_webhook() is False
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT tenant_id,status,updated_by FROM webhook_endpoints ORDER BY tenant_id"
            ).fetchall()
        assert rows == [
            ("tenant-a", "disabled", "user-a"),
            ("tenant-b", "active", "user-b"),
        ]
    finally:
        for client in clients:
            client.close()


def test_provider_key_cooldown_restore_is_tenant_qualified(tmp_path: Path) -> None:
    """429 冷却恢复只修改管理员租户的唯一托管 Key，并保留错误追踪事实。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE provider_credentials("
            "provider_credential_id TEXT,tenant_id TEXT,provider_id TEXT,encrypted_key TEXT,"
            "status TEXT,rate_limited_until TEXT,last_error_code TEXT,"
            "last_error_message TEXT,updated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO provider_credentials VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    "credential-a",
                    "tenant-a",
                    "provider-shared",
                    "aesgcm-v1:tenant-a-ciphertext",
                    "rate_limited",
                    "2099-01-01T00:00:00Z",
                    "gateway.upstream_rate_limited",
                    "redacted",
                    "now",
                ),
                (
                    "credential-b",
                    "tenant-b",
                    "provider-shared",
                    "aesgcm-v1:tenant-b-ciphertext",
                    "rate_limited",
                    "2099-01-01T00:00:00Z",
                    "gateway.upstream_rate_limited",
                    "redacted",
                    "now",
                ),
            ],
        )
    acceptance, clients = _acceptance(batch, database)
    acceptance.sessions["admin"] = RUNTIME.SessionCredential(
        "admin",
        "admin",
        "default",
        "tenant-a",
        "user-a",
        "session-a",
        "token-a",
        "admin@example.invalid",
        "password-a",
    )
    acceptance.resource_ids["provider_id"] = "provider-shared"
    try:
        assert acceptance.restore_provider_key_cooldown() is True
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT tenant_id,status,rate_limited_until,last_error_code,last_error_message "
                "FROM provider_credentials ORDER BY tenant_id"
            ).fetchall()
        assert rows == [
            (
                "tenant-a",
                "active",
                None,
                "gateway.upstream_rate_limited",
                "redacted",
            ),
            (
                "tenant-b",
                "rate_limited",
                "2099-01-01T00:00:00Z",
                "gateway.upstream_rate_limited",
                "redacted",
            ),
        ]
        # 最终 cleanup 会再次执行恢复；active+NULL 必须保持幂等成功。
        assert acceptance.restore_provider_key_cooldown() is True
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE provider_credentials SET status='disabled',"
                "rate_limited_until='2099-02-02T00:00:00Z' "
                "WHERE tenant_id='tenant-a' AND provider_credential_id='credential-a'"
            )
        assert acceptance.restore_provider_key_cooldown() is False
        with sqlite3.connect(database) as connection:
            disabled = connection.execute(
                "SELECT status,rate_limited_until,last_error_code,last_error_message "
                "FROM provider_credentials "
                "WHERE tenant_id='tenant-a' AND provider_credential_id='credential-a'"
            ).fetchone()
        assert disabled == (
            "disabled",
            "2099-02-02T00:00:00Z",
            "gateway.upstream_rate_limited",
            "redacted",
        )
    finally:
        for client in clients:
            client.close()


def test_persisted_privacy_scans_database_and_logs(tmp_path: Path) -> None:
    """同批数据库、SQLite 伴生文件与日志均不得出现正文或凭据明文。"""

    batch = tmp_path / "batch"
    batch.mkdir()
    database = batch / "runtime.db"
    database.write_bytes(b"sqlite-safe-evidence")
    companions = tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
    for path in companions:
        path.write_bytes(b"sqlite-companion-safe-evidence")
    logs = (batch / "control.log", batch / "gateway.log")
    for path in logs:
        path.write_text("service_started request_id=req_unit\n", encoding="utf-8")
    config = replace(_config(batch, database), log_paths=logs)
    clients = tuple(
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) for _ in range(3)
    )
    acceptance = RUNTIME.RuntimeAcceptance(config, *clients)
    try:
        result = acceptance.verify_persisted_privacy()
        assert result["database_scanned"] is True
        assert result["database_files_scanned"] == 4
        assert result["service_logs_scanned"] == 2
        companions[-1].write_text(RUNTIME.PRIVATE_PROVIDER_SENTINEL, encoding="utf-8")
        with pytest.raises(RUNTIME.AcceptanceFailure) as companion_leaked:
            acceptance.verify_persisted_privacy()
        assert companion_leaked.value.code == "sensitive_value"
        companions[-1].write_bytes(b"sqlite-companion-safe-evidence")
        logs[1].write_text(RUNTIME.PRIVATE_PROMPT_SENTINEL, encoding="utf-8")
        with pytest.raises(RUNTIME.AcceptanceFailure) as leaked:
            acceptance.verify_persisted_privacy()
        assert leaked.value.code == "sensitive_value"
    finally:
        for client in clients:
            client.close()
