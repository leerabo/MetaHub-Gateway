"""PRD-066 应用内 Browser 验收启动器的凭据生命周期行为测试。

测试数据库、统计文件和 pytest 临时目录全部位于 ``D:/MetaHub``。测试只绑定随机回环端口，
不启动 Gateway/Control、不访问开发数据库或外网，也不把测试 Provider Key 写入日志。
"""

from __future__ import annotations

import importlib.util
import secrets
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# 工作区物理根、验收启动器路径和动态模块描述只用于装载被测代码，不访问外部安装目录。
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    REPOSITORY_ROOT
    / ".artifacts"
    / "prd066-browser-iab-20260802b"
    / "iab_stack_runner.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location("prd066_iab_stack_runner", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块先注册再执行，保证 dataclass 等运行时类型能解析所属模块且不会触发脚本 main 入口。
RUNNER = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNNER
MODULE_SPEC.loader.exec_module(RUNNER)


def _create_runtime_database(database_path: Path) -> None:
    """创建只含启动器所需真实字段的隔离 SQLite 数据库和默认租户图。"""

    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE tenants(
              tenant_id TEXT PRIMARY KEY,
              slug TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE providers(
              provider_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              provider_code TEXT NOT NULL,
              status TEXT NOT NULL,
              FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
            );
            CREATE TABLE users(
              user_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              email TEXT NOT NULL,
              role TEXT NOT NULL,
              status TEXT NOT NULL,
              FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
            );
            CREATE TABLE provider_credentials(
              provider_credential_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              provider_id TEXT NOT NULL,
              key_fingerprint TEXT NOT NULL,
              encrypted_key TEXT NOT NULL,
              name TEXT,
              key_prefix TEXT,
              weight INTEGER NOT NULL,
              status TEXT NOT NULL,
              last_used_at TEXT,
              last_error_at TEXT,
              last_error_code TEXT,
              last_error_message TEXT,
              rate_limited_until TEXT,
              created_by TEXT,
              disabled_at TEXT,
              disabled_by TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
              FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
            );
            CREATE TABLE audit_logs(
              audit_log_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              actor_user_id TEXT NOT NULL,
              actor_email TEXT NOT NULL,
              actor_role TEXT NOT NULL,
              action TEXT NOT NULL,
              object_type TEXT NOT NULL,
              object_id TEXT NOT NULL,
              object_name TEXT,
              before_summary_json TEXT NOT NULL,
              after_summary_json TEXT NOT NULL,
              changed_fields_json TEXT NOT NULL,
              client_ip TEXT,
              user_agent TEXT,
              request_id TEXT NOT NULL,
              trace_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
            );
            INSERT INTO tenants(tenant_id,slug,name,status)
            VALUES('tenant_default','default','Default','active');
            INSERT INTO providers(provider_id,tenant_id,provider_code,status)
            VALUES('provider_local','tenant_default','local_openai','active');
            INSERT INTO users(user_id,tenant_id,email,role,status)
            VALUES('user_admin','tenant_default','admin@metahub.local','admin','active');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _insert_test_credential(
    database_path: Path,
    *,
    credential_id: str,
    name: str,
    status: str = "active",
) -> None:
    """插入不含真实密钥的测试 Credential，用于验证所有权和遗留判断。"""

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO provider_credentials(
              provider_credential_id,tenant_id,provider_id,key_fingerprint,encrypted_key,
              name,key_prefix,weight,status,created_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                credential_id,
                "tenant_default",
                "provider_local",
                f"fingerprint-{credential_id}",
                f"ciphertext-{credential_id}",
                name,
                "sk-test...",
                100,
                status,
                "user_admin",
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def isolated_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path]:
    """把启动器数据库、统计目录和监听地址重定向到本次测试的 D 盘临时目录。"""

    database_path = tmp_path / "runtime.db"
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir()
    _create_runtime_database(database_path)
    monkeypatch.setattr(RUNNER, "DATABASE_PATH", database_path)
    monkeypatch.setattr(RUNNER, "ARTIFACT_DIRECTORY", artifact_directory)
    monkeypatch.setattr(RUNNER, "PROVIDER_ADDRESS", ("127.0.0.1", 0))
    return RUNNER, database_path


def _database_rows(
    database_path: Path,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[sqlite3.Row]:
    """以参数化只读查询返回带列名快照，避免测试辅助函数拼接动态值。"""

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def test_owned_stale_cleanup_and_current_lifecycle_are_audited(
    isolated_runner: tuple[ModuleType, Path],
) -> None:
    """只回收带双重工具标识的遗留 Key，并审计创建和当前 run 停用。"""

    runner, database_path = isolated_runner
    _insert_test_credential(
        database_path,
        credential_id="pcred_prd066_iab_stale_owned",
        name=runner.RUNTIME_CREDENTIAL_NAME,
    )
    # 同名但没有验收专用 ID 前缀的用户 Key 不属于脚本，必须始终保持 active。
    _insert_test_credential(
        database_path,
        credential_id="pcred_user_owned",
        name=runner.RUNTIME_CREDENTIAL_NAME,
    )
    # 专用 ID 前缀但名称不符合 owner/run 合同也不能被宽泛回收。
    _insert_test_credential(
        database_path,
        credential_id="pcred_prd066_iab_foreign_name",
        name="customer credential",
    )
    owner = runner._new_runtime_owner()
    provider_key = f"sk-test-{secrets.token_urlsafe(24)}"
    encryption_secret = secrets.token_urlsafe(48)

    runner._prepare_runtime_credential(provider_key, encryption_secret, owner)
    assert runner._disable_runtime_credential(owner, retry_delays=()) is True

    credential_rows = _database_rows(
        database_path,
        """
        SELECT provider_credential_id,name,status
        FROM provider_credentials ORDER BY provider_credential_id
        """,
    )
    statuses = {row["provider_credential_id"]: row["status"] for row in credential_rows}
    assert statuses["pcred_prd066_iab_stale_owned"] == "disabled"
    assert statuses["pcred_user_owned"] == "active"
    assert statuses["pcred_prd066_iab_foreign_name"] == "active"
    assert statuses[owner.credential_id] == "disabled"

    audit_rows = _database_rows(
        database_path,
        """
        SELECT tenant_id,actor_user_id,action,object_id,before_summary_json,
               after_summary_json,request_id,trace_id
        FROM audit_logs ORDER BY created_at,audit_log_id
        """,
    )
    assert {(row["action"], row["object_id"]) for row in audit_rows} == {
        ("provider_key.updated", "pcred_prd066_iab_stale_owned"),
        ("provider_key.created", owner.credential_id),
        ("provider_key.updated", owner.credential_id),
    }
    assert all(row["tenant_id"] == "tenant_default" for row in audit_rows)
    assert all(row["actor_user_id"] == "user_admin" for row in audit_rows)
    assert all(row["request_id"].startswith("req_prd066_iab_") for row in audit_rows)
    assert all(row["trace_id"].startswith("trc_prd066_iab_") for row in audit_rows)
    # AuditLog 只能含 Key 前缀和治理状态，完整 Provider Key 不得进入任何摘要字段。
    audit_text = "\n".join(str(value) for row in audit_rows for value in tuple(row))
    assert provider_key not in audit_text


def test_second_instance_fails_before_credential_preparation(
    isolated_runner: tuple[ModuleType, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有实例持有监听时，第二次 main 启动不得调用任何 DB Credential 准备逻辑。"""

    runner, _database_path = isolated_runner
    first_server = runner._acquire_provider_server("sk-first-instance", runner.ProviderStats())
    preparation_calls: list[str] = []

    def forbidden_prepare(*_args: object, **_kwargs: object) -> None:
        """记录并拒绝本不应发生的第二实例数据库准备调用。"""

        preparation_calls.append("called")
        # 进入此分支说明独占监听未能在 Provider Credential 数据库变更前拦截第二实例。
        raise AssertionError("second instance touched credential database")

    monkeypatch.setattr(runner, "PROVIDER_ADDRESS", first_server.server_address)
    monkeypatch.setattr(runner, "_prepare_runtime_credential", forbidden_prepare)
    try:
        with pytest.raises(RuntimeError, match="instance already active"):
            runner.main()
    finally:
        first_server.server_close()
    assert preparation_calls == []


def test_main_disables_credential_when_prepare_raises_after_commit(
    isolated_runner: tuple[ModuleType, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟创建提交后函数异常，验证最外层生命周期仍按 owner 精确停用 Key。"""

    runner, database_path = isolated_runner
    original_prepare = runner._prepare_runtime_credential

    def prepare_then_fail(provider_key: str, encryption_secret: str, owner: Any) -> None:
        """先真实提交 Credential 和审计，再模拟返回调用方前发生异常。"""

        original_prepare(provider_key, encryption_secret, owner)
        # 该异常精确模拟 Credential 已提交、但准备函数尚未来得及把成功状态交给调用方的窗口。
        raise RuntimeError("injected failure after credential commit")

    monkeypatch.setattr(runner, "_prepare_runtime_credential", prepare_then_fail)
    with pytest.raises(RuntimeError, match="injected failure after credential commit"):
        runner.main()

    credentials = _database_rows(
        database_path,
        "SELECT provider_credential_id,status FROM provider_credentials",
    )
    assert len(credentials) == 1
    assert credentials[0]["status"] == "disabled"
    audit_rows = _database_rows(
        database_path,
        "SELECT action FROM audit_logs ORDER BY created_at",
    )
    audit_actions = [row["action"] for row in audit_rows]
    assert sorted(audit_actions) == ["provider_key.created", "provider_key.updated"]


def test_nested_cleanup_reaches_disable_stats_and_listener_close_after_errors(
    isolated_runner: tuple[ModuleType, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前序清理和 Credential 停用均异常时，统计与监听释放仍必须执行。"""

    runner, _database_path = isolated_runner
    calls: list[str] = []

    class RecordingServer:
        """记录最终 server_close 调用的监听资源清理替身。"""

        def server_close(self) -> None:
            """记录监听资源已到达最终释放阶段。"""

            calls.append("close")

    def fail_process_cleanup(_processes: object) -> None:
        """模拟子进程清理失败并确认后续安全清理阶段仍会执行。"""

        calls.append("processes")
        # 注入首阶段异常，验证嵌套 finally 不会跳过 Provider Credential 停用。
        raise RuntimeError("injected process cleanup failure")

    def fail_provider_cleanup(_server: object, _thread: object) -> None:
        """模拟 Provider shutdown 失败并确认 Credential 停用不被短路。"""

        calls.append("provider")
        # Provider 服务线程异常不能成为跳过数据库 Credential 清理的理由。
        raise RuntimeError("injected provider cleanup failure")

    def fail_credential_cleanup(_owner: object) -> None:
        """模拟 Credential 清理在全部重试后仍失败。"""

        calls.append("credential")
        # Credential 失败仍要落统计并释放监听，否则会形成无法再次验收的资源残留。
        raise RuntimeError("injected credential cleanup failure")

    def record_stats(_stats: object) -> None:
        """记录统计文件阶段仍被执行。"""

        calls.append("stats")

    monkeypatch.setattr(runner, "_stop_child_processes", fail_process_cleanup)
    monkeypatch.setattr(runner, "_quiesce_provider_server", fail_provider_cleanup)
    monkeypatch.setattr(runner, "_disable_runtime_credential", fail_credential_cleanup)
    monkeypatch.setattr(runner, "_write_provider_stats", record_stats)

    with pytest.raises(RuntimeError, match="resource cleanup failed"):
        runner._cleanup_runtime_resources(
            processes=[],
            server=RecordingServer(),
            provider_thread=None,
            owner=runner._new_runtime_owner(),
            credential_may_exist=True,
            stats=runner.ProviderStats(),
        )
    assert calls == ["processes", "provider", "credential", "stats", "close"]


def test_disable_retries_real_sqlite_lock_and_writes_single_update_audit(
    isolated_runner: tuple[ModuleType, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 SQLite 写锁首次冲突后释放时，停用应有界重试且只写一次更新审计。"""

    runner, database_path = isolated_runner
    owner = runner._new_runtime_owner()
    runner._prepare_runtime_credential(
        f"sk-lock-{secrets.token_urlsafe(24)}",
        secrets.token_urlsafe(48),
        owner,
    )
    locker = sqlite3.connect(database_path, timeout=0)
    locker.execute("BEGIN IMMEDIATE")
    sleep_calls: list[float] = []
    locker_closed = False

    def release_lock(delay: float) -> None:
        """在第一次退避点释放真实写锁，让下一次尝试可以提交。"""

        nonlocal locker_closed
        sleep_calls.append(delay)
        if not locker_closed:
            locker.rollback()
            locker.close()
            locker_closed = True

    monkeypatch.setattr(runner.time, "sleep", release_lock)
    try:
        assert runner._disable_runtime_credential(owner, retry_delays=(0.0, 0.0)) is True
    finally:
        if not locker_closed:
            locker.rollback()
            locker.close()
    assert sleep_calls == [0.0]
    status = _database_rows(
        database_path,
        "SELECT status FROM provider_credentials WHERE provider_credential_id=?",
        (owner.credential_id,),
    )[0]["status"]
    assert status == "disabled"
    actions = _database_rows(database_path, "SELECT action FROM audit_logs")
    assert [row["action"] for row in actions].count("provider_key.updated") == 1


def test_audit_failure_rolls_back_credential_disable(
    isolated_runner: tuple[ModuleType, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停用审计写入异常时，Credential UPDATE 必须与审计一起回滚。"""

    runner, database_path = isolated_runner
    owner = runner._new_runtime_owner()
    runner._prepare_runtime_credential(
        f"sk-audit-{secrets.token_urlsafe(24)}",
        secrets.token_urlsafe(48),
        owner,
    )

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        """模拟强审计服务拒绝写入，以验证业务 UPDATE 与审计原子回滚。"""

        # 审计不可用时必须回滚 Credential 状态，不能提交一条没有 request/trace 的变更。
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(runner, "_record_runtime_credential_audit", fail_audit)
    with pytest.raises(RuntimeError, match="credential cleanup failed"):
        runner._disable_runtime_credential(owner, retry_delays=())
    row = _database_rows(
        database_path,
        "SELECT status,disabled_at FROM provider_credentials WHERE provider_credential_id=?",
        (owner.credential_id,),
    )[0]
    assert row["status"] == "active"
    assert row["disabled_at"] is None


def test_current_migrations_accept_runtime_credential_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在全新执行至 0058 的真实结构上验证 Credential 与强审计触发器兼容。"""

    # 迁移和 seed 都显式指向本测试数据库；不会读取或改写当前开发库。
    from metahub_shared.db.migrate import apply_migrations
    from metahub_shared.db.seed import apply_seed_data

    database_path = tmp_path / "migrated-runtime.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    apply_migrations(
        database_url,
        migrations_dir=REPOSITORY_ROOT / "infra" / "migrations",
    )
    apply_seed_data(
        database_url,
        seed_file=REPOSITORY_ROOT / "infra" / "seeds" / "default_seed.json",
    )
    monkeypatch.setattr(RUNNER, "DATABASE_PATH", database_path)
    owner = RUNNER._new_runtime_owner()
    RUNNER._prepare_runtime_credential(
        f"sk-migrated-{secrets.token_urlsafe(24)}",
        secrets.token_urlsafe(48),
        owner,
    )
    assert RUNNER._disable_runtime_credential(owner, retry_delays=()) is True

    rows = _database_rows(
        database_path,
        """
        SELECT tenant_id,actor_user_id,action,object_type,object_id,request_id,trace_id
        FROM audit_logs WHERE object_id=? ORDER BY created_at,audit_log_id
        """,
        (owner.credential_id,),
    )
    # AuditLog 使用秒级时间且 ID 随机生成，同秒两条记录不能依赖 ID 字典序推断业务先后。
    assert sorted(row["action"] for row in rows) == [
        "provider_key.created",
        "provider_key.updated",
    ]
    assert all(row["object_type"] == "provider_key" for row in rows)
    assert all(row["tenant_id"] and row["actor_user_id"] for row in rows)
    assert all(row["request_id"] and row["trace_id"] for row in rows)
