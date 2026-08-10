"""PRD-076 Response Cache 受信维护专项。

本文件验证停服轮换、外部恢复队列门禁、迁移回滚和错误输出脱敏，不保存任何真实密钥。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import metahub_shared.db.migrate as db_migrate
import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.response_cache import VersionedResponseCacheSecret


def _load_maintenance_module() -> ModuleType:
    """加载正式维护核心，测试不复制轮换或错误收口逻辑。"""

    path = Path(__file__).resolve().parents[1] / "response_cache_maintenance.py"
    specification = importlib.util.spec_from_file_location(
        "prd076_response_cache_maintenance",
        path,
    )
    if specification is None or specification.loader is None:
        # 正式维护模块无法加载时必须直接失败，不能用测试内复制逻辑替代真实入口。
        raise AssertionError("unable to load PRD-076 response cache maintenance")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


# 模块实例和固定数据库时间供全部用例复用，避免不同测试验证不同入口身份。
MAINTENANCE = _load_maintenance_module()
NOW = "2026-08-10T00:00:00Z"


def _url(path: Path) -> str:
    """把隔离 SQLite 路径转换为跨平台 URL，且不把结果写入测试证据。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def migration_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """只执行一次历史迁移，后续用例复制未初始化 0069 模板。"""

    root = tmp_path_factory.mktemp("prd076-maintenance-template")
    database = root / "template.sqlite3"
    assert apply_migrations(_url(database)).applied[-1] == "0069"
    return database


def _clone(template: Path, tmp_path: Path) -> Path:
    """每个用例使用独立数据库，失败分支不会污染其他验证。"""

    database = tmp_path / "maintenance.sqlite3"
    shutil.copy2(template, database)
    return database


def _config(
    database: Path,
    spool_root: Path,
    *,
    version: int,
    secret_marker: str,
    enabled: bool = False,
) -> SimpleNamespace:
    """构造 runner 所需的最小配置；所有 Secret 只停留在测试进程内存。"""

    return SimpleNamespace(
        database_url=_url(database),
        response_cache_enabled=enabled,
        response_cache_hmac_key=VersionedResponseCacheSecret(
            version,
            f"prd076-maintenance-hmac-{secret_marker}-" + "h" * 48,
        ),
        response_cache_encryption_key=VersionedResponseCacheSecret(
            version,
            f"prd076-maintenance-encryption-{secret_marker}-" + "e" * 48,
        ),
        response_cache_failure_spool_root=str(spool_root.resolve()),
        response_cache_failure_spool_keyring=(
            VersionedResponseCacheSecret(
                1,
                "prd076-maintenance-spool-independent-" + "s" * 48,
            ),
        ),
        response_cache_failure_spool_claim_lease_seconds=60,
    )


def _seed_policy(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """建立一个 disabled Policy，验证轮换只推进围栏而不改业务配置版本。"""

    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd076-maint-auth-" + "a" * 48)
    monkeypatch.setenv(
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "prd076-maint-provider-encryption-" + "p" * 48,
    )
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd076MaintOnly!2026")
    monkeypatch.setenv("METAHUB_SEED_PROVIDER_API_KEY", "prd076-maint-provider-" + "k" * 48)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", "prd076-maint-virtual-" + "v" * 48)
    seed = apply_seed_data(_url(database))
    connection = connect_database(_url(database))
    try:
        owner = connection.execute(
            """
            SELECT environment.environment_id,environment.code,model.model_id,user.user_id
            FROM environments environment
            JOIN models model ON model.tenant_id=environment.tenant_id
            JOIN users user ON user.tenant_id=environment.tenant_id
            WHERE environment.tenant_id=? AND environment.code='dev'
              AND model.status='active' AND user.role='admin' AND user.status='active'
            ORDER BY model.model_id,user.user_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert owner is not None
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
              status,created_by,created_at,updated_at
            ) VALUES('alias_prd076_maintenance',?,'prd076-maintenance',?,?,?,
                     'active',?,?,?)
            """,
            (
                seed.tenant_id,
                str(owner[1]),
                str(owner[0]),
                str(owner[2]),
                str(owner[3]),
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO response_cache_policies(
              cache_policy_id,tenant_id,model_alias_id,status,data_class,ttl_seconds,
              charge_cache_hits,tags_json,single_data_class_confirmed,
              data_class_confirmed_by,data_class_confirmed_version,version,
              invalidation_generation,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,'disabled',NULL,900,0,'[]',0,NULL,NULL,1,1,?,?,?,?)
            """,
            (
                "rcp_" + "a" * 32,
                seed.tenant_id,
                "alias_prd076_maintenance",
                str(owner[3]),
                str(owner[3]),
                NOW,
                NOW,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    # 测试只返回 tenant owner 给后续同库插入，终端和断言不输出该值。
    return seed.tenant_id


def test_real_rotation_initializes_then_purges_generations_and_fences_policy(
    migration_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 EXCLUSIVE 轮换保留 trigger schema，并清除全部旧 HMAC generation。"""

    database = _clone(migration_template, tmp_path)
    tenant_id = _seed_policy(database, monkeypatch)
    spool_root = tmp_path / "spool"
    first = MAINTENANCE.rotate_response_cache_keys(
        _config(database, spool_root, version=1, secret_marker="first")
    )
    assert first.key_state_generation == 2
    assert first.policies_fenced == 1

    connection = connect_database(_url(database))
    try:
        connection.execute(
            """
            INSERT INTO response_cache_tag_generations(
              tenant_id,hmac_key_version,tag_kind,tag_digest,generation,updated_at
            ) VALUES(?,1,'tag',?,1,?)
            """,
            (tenant_id, "b" * 64, NOW),
        )
        connection.execute(
            """
            INSERT INTO response_cache_key_generations(
              tenant_id,hmac_key_version,cache_identity_digest,generation,updated_at
            ) VALUES(?,1,?,1,?)
            """,
            (tenant_id, "c" * 64, NOW),
        )
        connection.commit()
    finally:
        connection.close()

    second = MAINTENANCE.rotate_response_cache_keys(
        _config(database, spool_root, version=2, secret_marker="second")
    )
    assert second.hmac_key_version == 2
    assert second.encryption_key_version == 2
    assert second.key_state_generation == 3
    assert second.tag_generations_purged == 1
    assert second.key_generations_purged == 1
    assert second.spool_pending == second.spool_processing == second.spool_quarantine == 0

    connection = connect_database(_url(database))
    try:
        policy = connection.execute(
            """
            SELECT version,invalidation_generation,status,ttl_seconds
            FROM response_cache_policies
            """
        ).fetchone()
        assert tuple(policy) == (1, 3, "disabled", 900)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        trigger_rows = connection.execute(
            """
            SELECT name,sql FROM sqlite_schema
            WHERE type='trigger' AND name LIKE 'trg_response_cache_%_prd076'
            """
        ).fetchall()
        trigger_names = {str(row["name"]) for row in trigger_rows}
        assert set(MAINTENANCE.MAINTENANCE_TRIGGER_SHA256) <= trigger_names
    finally:
        connection.close()


def test_nonempty_spool_blocks_before_key_state_changes(
    migration_template: Path,
    tmp_path: Path,
) -> None:
    """任一 pending 文件都在数据库事务前阻断，且 runner 不读取或删除其内容。"""

    database = _clone(migration_template, tmp_path)
    spool_root = tmp_path / "spool"
    first_config = _config(database, spool_root, version=1, secret_marker="first")
    MAINTENANCE.rotate_response_cache_keys(first_config)
    pending = spool_root / "pending" / "synthetic-unknown-record"
    pending.write_bytes(b"not inspected")

    with pytest.raises(MAINTENANCE.ResponseCacheMaintenanceError) as failure:
        MAINTENANCE.rotate_response_cache_keys(
            _config(database, spool_root, version=2, secret_marker="second")
        )
    assert failure.value.code == "response_cache.maintenance_spool_not_empty"
    assert pending.read_bytes() == b"not inspected"
    connection = connect_database(_url(database))
    try:
        state = connection.execute(
            "SELECT active_hmac_key_version,generation FROM response_cache_key_state"
        ).fetchone()
        assert tuple(state) == (1, 2)
    finally:
        connection.close()


def test_same_version_secret_replacement_and_schema_drift_fail_closed(
    migration_template: Path,
    tmp_path: Path,
) -> None:
    """同版本换 Secret 与维护 trigger 漂移都不能产生半次 Policy/key-state 更新。"""

    database = _clone(migration_template, tmp_path)
    spool_root = tmp_path / "spool"
    MAINTENANCE.rotate_response_cache_keys(
        _config(database, spool_root, version=1, secret_marker="first")
    )
    with pytest.raises(MAINTENANCE.ResponseCacheMaintenanceError) as transition:
        MAINTENANCE.rotate_response_cache_keys(
            _config(database, spool_root, version=1, secret_marker="changed")
        )
    assert transition.value.code == "response_cache.maintenance_key_transition_invalid"

    connection = connect_database(_url(database))
    try:
        connection.execute("DROP TRIGGER trg_response_cache_tag_generations_delete_prd076")
        connection.execute(
            """
            CREATE TRIGGER trg_response_cache_tag_generations_delete_prd076
            BEFORE DELETE ON response_cache_tag_generations FOR EACH ROW
            BEGIN SELECT RAISE(ABORT,'synthetic_drift'); END
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(MAINTENANCE.ResponseCacheMaintenanceError) as drift:
        MAINTENANCE.rotate_response_cache_keys(
            _config(database, spool_root, version=2, secret_marker="second")
        )
    assert drift.value.code == "response_cache.maintenance_schema_drift"


def test_empty_0069_down_uses_spool_preflight_and_preserves_database_health(
    migration_template: Path,
    tmp_path: Path,
) -> None:
    """完全空的新事实库可经受信 runner 回到 0068，且 integrity/FK 仍通过。"""

    database = _clone(migration_template, tmp_path)
    result = MAINTENANCE.down_response_cache_migration(
        _config(database, tmp_path / "spool", version=1, secret_marker="first")
    )
    assert result.rolled_back == "0069"
    assert result.spool_pending == result.spool_processing == result.spool_quarantine == 0
    connection = connect_database(_url(database))
    try:
        migration = connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0069'"
        ).fetchone()
        assert migration[0] == "rolled_back"
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='response_cache_key_state'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_down_rejects_future_applied_migration_before_rollback(
    migration_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最高 applied 版本不是 0069 时不得调用共享 rollback，也不得触碰数据库。"""

    database = _clone(migration_template, tmp_path)
    connection = connect_database(_url(database))
    try:
        connection.execute("CREATE TABLE synthetic_future_0070_guard(value TEXT NOT NULL)")
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0070','synthetic_future_guard',?,'applied',?,0,NULL)
            """,
            ("0" * 64, NOW),
        )
        connection.commit()
        schema_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
            ).fetchall()
        ]
        migrations_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    finally:
        connection.close()
    bytes_before = database.read_bytes()
    rollback_called = False

    def unexpected_rollback(_database_url: str) -> None:
        """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 unexpected_rollback 对应步骤。"""
        nonlocal rollback_called
        rollback_called = True
        # 该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。
        raise AssertionError("rollback must not be called for a future migration")

    monkeypatch.setattr(MAINTENANCE, "rollback_last_migration", unexpected_rollback)
    with pytest.raises(MAINTENANCE.ResponseCacheMaintenanceError) as failure:
        MAINTENANCE.down_response_cache_migration(
            _config(database, tmp_path / "spool", version=1, secret_marker="first")
        )
    assert failure.value.code == "response_cache.maintenance_down_version_invalid"
    assert failure.value.blocked is True
    assert rollback_called is False
    assert database.read_bytes() == bytes_before

    connection = connect_database(_url(database))
    try:
        schema_after = [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
            ).fetchall()
        ]
        migrations_after = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    assert schema_after == schema_before
    assert migrations_after == migrations_before


def test_shared_rollback_expected_version_guard_closes_the_locked_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版本在首次查询后变化时，同一 BEGIN IMMEDIATE 内的守卫必须阻止 owner down。"""

    database = tmp_path / "atomic-rollback.sqlite3"
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations(
              version TEXT PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
              status TEXT NOT NULL,applied_at TEXT NOT NULL,
              execution_time_ms INTEGER NOT NULL,error_message TEXT
            )
            """
        )
        connection.execute("CREATE TABLE synthetic_0069_owner(value TEXT NOT NULL)")
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0069','atomic_guard',?,'applied',?,0,NULL)
            """,
            ("0" * 64, NOW),
        )
        connection.commit()
    finally:
        connection.close()

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0069_atomic_guard.down.sql").write_text(
        db_migrate.EXPECTED_VERSION_ROLLBACK_PREFIX
        + "\nDROP TABLE synthetic_0069_owner;\n"
        + db_migrate.EXPECTED_VERSION_ROLLBACK_SUFFIX,
        encoding="utf-8",
    )

    initial_read_complete = threading.Event()
    future_committed = threading.Event()
    original_inject = db_migrate._inject_expected_version_guard
    result: list[BaseException | object] = []

    def synchronized_inject(sql: str, expected_version: str) -> str:
        """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 synchronized_inject 对应步骤。"""
        initial_read_complete.set()
        assert future_committed.wait(timeout=2.0)
        return original_inject(sql, expected_version)

    def rollback_in_thread() -> None:
        """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 rollback_in_thread 对应步骤。"""
        try:
            result.append(
                rollback_last_migration(
                    database_url,
                    migrations_dir=migrations,
                    expected_version="0069",
                )
            )
        except BaseException as exc:
            result.append(exc)

    monkeypatch.setattr(db_migrate, "_inject_expected_version_guard", synchronized_inject)
    worker = threading.Thread(target=rollback_in_thread, daemon=True)
    worker.start()
    assert initial_read_complete.wait(timeout=2.0)
    connection = connect_database(database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE synthetic_0070_owner(value TEXT NOT NULL)")
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0070','synthetic_race',?,'applied',?,0,NULL)
            """,
            ("0" * 64, NOW),
        )
        connection.commit()
    finally:
        connection.close()
    future_committed.set()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], DbError)
    assert result[0].code == "db.migration_version_mismatch"

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='synthetic_0069_owner'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='synthetic_0070_owner'"
        ).fetchone()[0] == 1
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT version,status FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == [("0069", "applied"), ("0070", "applied")]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_shared_rollback_rejects_persistent_sql_before_trusted_prefix(
    tmp_path: Path,
) -> None:
    """受信前缀前存在持久 SQL 时必须在 executescript 前失败，不能留下部分 DDL。"""

    database = tmp_path / "untrusted-prefix.sqlite3"
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations(
              version TEXT PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
              status TEXT NOT NULL,applied_at TEXT NOT NULL,
              execution_time_ms INTEGER NOT NULL,error_message TEXT
            )
            """
        )
        connection.execute("CREATE TABLE synthetic_0069_owner(value TEXT NOT NULL)")
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0069','prefix_guard',?,'applied',?,0,NULL)
            """,
            ("0" * 64, NOW),
        )
        connection.commit()
    finally:
        connection.close()

    migrations = tmp_path / "untrusted-migrations"
    migrations.mkdir()
    (migrations / "0069_prefix_guard.down.sql").write_text(
        "CREATE TABLE forbidden_pre_guard_damage(value TEXT);\n"
        + db_migrate.EXPECTED_VERSION_ROLLBACK_PREFIX
        + "\nDROP TABLE synthetic_0069_owner;\n"
        + db_migrate.EXPECTED_VERSION_ROLLBACK_SUFFIX,
        encoding="utf-8",
    )
    with pytest.raises(DbError) as failure:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0069",
        )
    assert failure.value.code == "db.migration_failed"

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='forbidden_pre_guard_damage'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='synthetic_0069_owner'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0069'"
        ).fetchone()[0] == "applied"
    finally:
        connection.close()


def test_shared_expected_version_guard_rejects_untrusted_transaction_tail() -> None:
    """中途 COMMIT 后继续 DDL 的脚本不得进入 executescript。"""

    malformed = (
        db_migrate.EXPECTED_VERSION_ROLLBACK_PREFIX
        + "\nCOMMIT;\nCREATE TABLE forbidden_after_commit(value TEXT);\n"
        + db_migrate.EXPECTED_VERSION_ROLLBACK_SUFFIX
    )
    with pytest.raises(DbError) as failure:
        db_migrate._inject_expected_version_guard(malformed, "0069")
    assert failure.value.code == "db.migration_failed"


def test_shared_expected_version_status_failure_rolls_back_down_ddl(
    tmp_path: Path,
) -> None:
    """版本终态写失败时，受信 down 的 DDL 与状态必须一起回滚。"""

    database = tmp_path / "status-write-failure.sqlite3"
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations(
              version TEXT PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
              status TEXT NOT NULL,applied_at TEXT NOT NULL,
              execution_time_ms INTEGER NOT NULL,error_message TEXT
            )
            """
        )
        connection.execute("CREATE TABLE synthetic_0069_owner(value TEXT NOT NULL)")
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0069','status_failure',?,'applied',?,0,NULL)
            """,
            ("0" * 64, NOW),
        )
        connection.execute(
            """
            CREATE TRIGGER block_0069_status_update
            BEFORE UPDATE OF status ON schema_migrations FOR EACH ROW
            WHEN OLD.version='0069'
            BEGIN SELECT RAISE(ABORT,'synthetic_status_write_failure'); END
            """
        )
        connection.commit()
    finally:
        connection.close()

    migrations = tmp_path / "status-failure-migrations"
    migrations.mkdir()
    (migrations / "0069_status_failure.down.sql").write_text(
        db_migrate.EXPECTED_VERSION_ROLLBACK_PREFIX
        + "\nDROP TABLE synthetic_0069_owner;\n"
        + db_migrate.EXPECTED_VERSION_ROLLBACK_SUFFIX,
        encoding="utf-8",
    )
    with pytest.raises(DbError) as failure:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0069",
        )
    assert failure.value.code == "db.migration_failed"

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='synthetic_0069_owner'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0069'"
        ).fetchone()[0] == "applied"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_shared_expected_version_down_never_exposes_split_schema_and_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一连接在 down 脚本返回后只能同时看到已回退结构和 rolled_back。"""

    database = tmp_path / "atomic-visible-state.sqlite3"
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations(
              version TEXT PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
              status TEXT NOT NULL,applied_at TEXT NOT NULL,
              execution_time_ms INTEGER NOT NULL,error_message TEXT
            )
            """
        )
        connection.execute("CREATE TABLE synthetic_0069_owner(value TEXT NOT NULL)")
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES('0069','atomic_visible',?,'applied',?,0,NULL)
            """,
            ("0" * 64, NOW),
        )
        connection.commit()
    finally:
        connection.close()

    migrations = tmp_path / "atomic-visible-migrations"
    migrations.mkdir()
    (migrations / "0069_atomic_visible.down.sql").write_text(
        db_migrate.EXPECTED_VERSION_ROLLBACK_PREFIX
        + "\nDROP TABLE synthetic_0069_owner;\n"
        + db_migrate.EXPECTED_VERSION_ROLLBACK_SUFFIX,
        encoding="utf-8",
    )

    script_returned = threading.Event()
    observer_complete = threading.Event()
    observed: list[tuple[int, str]] = []
    original_connect = db_migrate.connect_database

    class PausingConnection:
        """只在 executescript 已提交后暂停 runner，确定性暴露事务边界。"""

        def __init__(self, wrapped: sqlite3.Connection) -> None:
            """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 __init__ 对应步骤。"""
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 __getattr__ 对应步骤。"""
            return getattr(self._wrapped, name)

        def executescript(self, sql: str) -> sqlite3.Cursor:
            """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 executescript 对应步骤。"""
            cursor = self._wrapped.executescript(sql)
            script_returned.set()
            assert observer_complete.wait(timeout=2.0)
            return cursor

    monkeypatch.setattr(
        db_migrate,
        "connect_database",
        lambda value: PausingConnection(original_connect(value)),
    )

    def observe_after_script_commit() -> None:
        """该测试冻结停机维护的配置、事务、清理和失败关闭边界，避免在线误操作。 当前定义负责 observe_after_script_commit 对应步骤。"""
        assert script_returned.wait(timeout=2.0)
        observer = original_connect(database_url)
        try:
            observer.execute("BEGIN IMMEDIATE")
            table_count = int(
                observer.execute(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE name='synthetic_0069_owner'"
                ).fetchone()[0]
            )
            status = str(
                observer.execute(
                    "SELECT status FROM schema_migrations WHERE version='0069'"
                ).fetchone()[0]
            )
            observed.append((table_count, status))
            observer.commit()
        finally:
            observer.close()
            observer_complete.set()

    observer_thread = threading.Thread(target=observe_after_script_commit, daemon=True)
    observer_thread.start()
    result = rollback_last_migration(
        database_url,
        migrations_dir=migrations,
        expected_version="0069",
    )
    observer_thread.join(timeout=2.0)
    assert not observer_thread.is_alive()
    assert result.rolled_back == "0069"
    assert observed == [(0, "rolled_back")]


def test_cli_never_echoes_database_error_or_argument_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """底层 DbError 即使携带路径/Secret，CLI 也只输出固定错误 JSON。"""

    sensitive = "synthetic-secret-database-path"
    monkeypatch.setattr(MAINTENANCE, "load_app_config", lambda _service: object())

    def fail_rotation(_config: object) -> None:
        """模拟带敏感诊断的 shared DbError，验证 CLI 固定错误收口。"""

        # 原始错误刻意带敏感标记；正式 CLI 不得将异常文本拼入 stderr。
        raise DbError("db.migration_failed", sensitive, sensitive)

    monkeypatch.setattr(MAINTENANCE, "rotate_response_cache_keys", fail_rotation)
    exit_code = MAINTENANCE.run_cli("rotate", ["--confirm-stopped"])
    output = capsys.readouterr()
    assert exit_code == 2
    assert output.out == ""
    assert json.loads(output.err) == {
        "code": "response_cache.maintenance_database_unavailable",
        "operation": "rotate",
        "status": "failed",
    }
    assert sensitive not in output.err

    exit_code = MAINTENANCE.run_cli("rotate", ["--unknown", sensitive])
    output = capsys.readouterr()
    assert exit_code == 3
    assert sensitive not in output.err
    assert "Traceback" not in output.err
