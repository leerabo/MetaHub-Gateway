"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/migrate.py
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .connection import connect_database, database_backend
from .errors import DbError

# 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
EXPECTED_VERSION_ROLLBACK_PREFIX = """-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;"""
EXPECTED_VERSION_ROLLBACK_SUFFIX = "COMMIT;\nPRAGMA foreign_keys = ON;\n"
TOP_LEVEL_BEGIN_RE = re.compile(r"(?m)^BEGIN(?: IMMEDIATE)?;[ \t]*$")
TOP_LEVEL_COMMIT_RE = re.compile(r"(?m)^COMMIT;[ \t]*$")
POSTGRESQL_BASELINE_VERSION = "0139"
POSTGRESQL_BASELINE_NAME = "postgresql_baseline"
POSTGRESQL_BASELINE_STATE = "metahub_bootstrap.baseline_state"
POSTGRESQL_BASELINE_GENERATOR_REVISION = 4


@dataclass(frozen=True)
class MigrationResult:
    """承载 数据库基础设施 的 MigrationResult 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    run_id: str
    applied: list[str]
    skipped: list[str]
    rolled_back: str | None = None


@dataclass(frozen=True)
class MigrationFile:
    """承载 数据库基础设施 的 MigrationFile 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    version: str
    name: str
    path: Path
    checksum: str
    sql: str


def apply_migrations(
    database_url: str | None = None,
    *,
    migrations_dir: Path | None = None,
) -> MigrationResult:
    """执行所有未应用迁移。

    schema_migrations 是全局基础设施表，不带 tenant_id；真正业务表从第一版迁移开始
    全部显式包含 tenant_id，保证后续多租户隔离不会靠口头约定。
    """

    run_id = f"dbm_{uuid4().hex}"
    configured_url = database_url or os.getenv("METAHUB_DATABASE_URL")
    connection = connect_database(configured_url)
    try:
        if database_backend(configured_url) == "postgresql":
            return _apply_postgresql_migrations(
                connection,
                run_id=run_id,
                migrations_dir=migrations_dir,
            )
        return _apply_migrations_with_connection(
            connection,
            run_id=run_id,
            migrations_dir=migrations_dir,
        )
    finally:
        # 迁移入口每次调用都独占连接；正常、幂等和异常分支都必须立即释放文件句柄，
        # 否则 Windows 下后续复制、替换或删除验收数据库会被残留连接阻塞。
        connection.close()


def _apply_migrations_with_connection(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    migrations_dir: Path | None,
) -> MigrationResult:
    """在已登记生命周期的连接内执行正向迁移，连接关闭统一由公开入口负责。"""

    directory = migrations_dir or _default_migrations_dir()
    migrations = _load_migration_files(directory)
    applied: list[str] = []
    skipped: list[str] = []

    _ensure_schema_migrations(connection)
    for migration in migrations:
        existing = _existing_migration(connection, migration.version)
        if existing and existing["status"] == "applied":
            if existing["checksum"] != migration.checksum:
                # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
                raise DbError(
                    "db.migration_checksum_changed",
                    f"Migration {migration.version} checksum changed.",
                    "Do not edit applied migrations; create a new migration instead.",
                )
            skipped.append(migration.version)
            continue
        _apply_one_migration(connection, migration)
        applied.append(migration.version)
    return MigrationResult(run_id=run_id, applied=applied, skipped=skipped)


def rollback_last_migration(
    database_url: str | None = None,
    *,
    migrations_dir: Path | None = None,
    confirm_prod: bool = False,
    expected_version: str | None = None,
) -> MigrationResult:
    """回滚最近一次已应用迁移。

    生产回滚必须显式确认，避免误删核心业务表。M0 只面向本地/测试回滚验证。
    """

    if os.getenv("METAHUB_ENV", "dev") == "prod" and not confirm_prod:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.migration_failed",
            "Rollback in prod requires explicit confirmation.",
            "Pass --confirm-prod only after backup and approval.",
        )
    if expected_version is not None:
        _validate_expected_version(expected_version)
    run_id = f"dbr_{uuid4().hex}"
    configured_url = database_url or os.getenv("METAHUB_DATABASE_URL")
    connection = connect_database(configured_url)
    try:
        if database_backend(configured_url) == "postgresql":
            return _rollback_postgresql_migration(
                connection,
                run_id=run_id,
                migrations_dir=migrations_dir,
                expected_version=expected_version,
            )
        return _rollback_last_migration_with_connection(
            connection,
            run_id=run_id,
            migrations_dir=migrations_dir,
            expected_version=expected_version,
        )
    finally:
        # 回滚可能在“无已应用版本”处提前返回，也可能因缺少 down SQL 或执行 SQL 失败；
        # 入口统一关闭独占连接，保证所有控制流都不会遗留 SQLite 文件锁。
        connection.close()


def _rollback_last_migration_with_connection(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    migrations_dir: Path | None,
    expected_version: str | None = None,
) -> MigrationResult:
    """在已登记生命周期的连接内执行最近版本回滚，连接关闭统一由公开入口负责。"""

    directory = migrations_dir or _default_migrations_dir()
    _ensure_schema_migrations(connection)
    row = connection.execute(
        """
        SELECT version,name FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        if expected_version is not None:
            raise _migration_version_mismatch()
        return MigrationResult(run_id=run_id, applied=[], skipped=[], rolled_back=None)
    if expected_version is not None and row["version"] != expected_version:
        raise _migration_version_mismatch()
    down_path = directory / f"{row['version']}_{row['name']}.down.sql"
    if not down_path.exists():
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.migration_failed",
            f"Rollback script not found for migration {row['version']}.",
            "Create a .down.sql file or write a corrective migration.",
        )
    started = time.perf_counter()
    try:
        down_sql = down_path.read_text(encoding="utf-8")
        if expected_version is not None:
            down_sql = _inject_expected_version_guard(down_sql, expected_version)
        connection.executescript(down_sql)
        if expected_version is None:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            connection.execute(
                """
                UPDATE schema_migrations
                SET status='rolled_back',applied_at=datetime('now'),
                    execution_time_ms=?,error_message=NULL
                WHERE version=?
                """,
                (elapsed_ms, row["version"]),
            )
            connection.commit()
    # 回滚脚本的 SQL 或提交失败时统一撤销显式事务，避免保留半完成结构。
    except sqlite3.Error as exc:
        # 必须在抛出统一错误前 rollback，调用方后续复用连接也不能提交中间 DDL。
        connection.rollback()
        if expected_version is not None and str(exc) == "db_migration_version_mismatch":
            raise _migration_version_mismatch() from exc
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.migration_failed",
            f"Rollback failed for migration {row['version']}: {exc}",
            "Inspect the rollback SQL and database state.",
        ) from exc
    return MigrationResult(run_id=run_id, applied=[], skipped=[], rolled_back=row["version"])


def _inject_expected_version_guard(sql: str, expected_version: str) -> str:
    """把 owner 复查与版本终态写入 down 的同一个受信事务。"""

    _validate_expected_version(expected_version)
    if (
        not sql.startswith(EXPECTED_VERSION_ROLLBACK_PREFIX)
        or sql.count("BEGIN IMMEDIATE;") != 1
        or sql.count("COMMIT;") != 1
        or not sql.endswith(EXPECTED_VERSION_ROLLBACK_SUFFIX)
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise DbError(
            "db.migration_failed",
            "Expected-version rollback requires a trusted immediate transaction.",
            "Use a reviewed down migration with the trusted rollback prefix.",
        )
    expected_literal = "'" + expected_version.replace("'", "''") + "'"
    guard = f"""

DROP TABLE IF EXISTS temp.metahub_rollback_expected_version_guard;
CREATE TEMP TABLE metahub_rollback_expected_version_guard(
  actual_version TEXT,
  expected_version TEXT NOT NULL,
  started_at_julian REAL NOT NULL
);
CREATE TEMP TRIGGER metahub_rollback_expected_version_guard_abort
BEFORE INSERT ON metahub_rollback_expected_version_guard FOR EACH ROW
WHEN NEW.actual_version IS NOT NEW.expected_version
BEGIN SELECT RAISE(ABORT,'db_migration_version_mismatch'); END;
INSERT INTO metahub_rollback_expected_version_guard(
  actual_version,expected_version,started_at_julian
)
SELECT (
  SELECT version FROM schema_migrations
  WHERE status='applied' ORDER BY version DESC LIMIT 1
), {expected_literal}, julianday('now');
DROP TRIGGER metahub_rollback_expected_version_guard_abort;
"""
    terminal = f"""

-- DDL 与版本终态必须在同一写锁事务提交；任何状态写失败都会回滚整段 down。
DROP TABLE IF EXISTS temp.metahub_rollback_status_guard;
CREATE TEMP TABLE metahub_rollback_status_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER metahub_rollback_status_guard_abort
BEFORE INSERT ON metahub_rollback_status_guard FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'db_migration_status_update_failed'); END;
UPDATE schema_migrations
SET status='rolled_back', applied_at=datetime('now'),
    execution_time_ms=MAX(0,CAST(ROUND(
      (julianday('now')-(
        SELECT started_at_julian
        FROM temp.metahub_rollback_expected_version_guard
      ))*86400000
    ) AS INTEGER)),
    error_message=NULL
WHERE version={expected_literal} AND status='applied';
INSERT INTO metahub_rollback_status_guard(issue_count)
VALUES(CASE WHEN changes()=1 THEN 0 ELSE 1 END);
DROP TRIGGER metahub_rollback_status_guard_abort;
DROP TABLE metahub_rollback_status_guard;
DROP TABLE metahub_rollback_expected_version_guard;
"""
    guarded = sql.replace(
        EXPECTED_VERSION_ROLLBACK_PREFIX,
        EXPECTED_VERSION_ROLLBACK_PREFIX + guard,
        1,
    )
    return (
        guarded[: -len(EXPECTED_VERSION_ROLLBACK_SUFFIX)]
        + terminal
        + EXPECTED_VERSION_ROLLBACK_SUFFIX
    )


def _validate_expected_version(expected_version: str) -> None:
    """限制原子回滚 owner 为仓库使用的四位 ASCII 迁移版本。"""

    if (
        type(expected_version) is not str
        or len(expected_version) != 4
        or not expected_version.isascii()
        or not expected_version.isdigit()
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise DbError(
            "db.migration_failed",
            "Expected rollback version is invalid.",
            "Use the four-digit version from the reviewed migration owner.",
        )


def _apply_postgresql_migrations(
    connection: Any,
    *,
    run_id: str,
    migrations_dir: Path | None,
) -> MigrationResult:
    """Install the reviewed baseline, then apply PostgreSQL-native deltas."""

    root, native_directory = _postgresql_migration_directories(migrations_dir)
    source_migrations = _postgresql_baseline_sources(root)
    native_migrations = _load_migration_files(native_directory)
    baseline = next(
        (
            migration
            for migration in native_migrations
            if migration.version == POSTGRESQL_BASELINE_VERSION
            and migration.name == POSTGRESQL_BASELINE_NAME
        ),
        None,
    )
    if baseline is None:
        raise DbError(
            "db.migration_failed",
            "The reviewed PostgreSQL baseline is missing.",
            "Restore the generated 0139 PostgreSQL baseline before deployment.",
        )

    applied: list[str] = []
    skipped: list[str] = []
    state = _postgresql_baseline_state(connection)
    if state is None:
        public_tables = connection.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone()[0]
        connection.rollback()
        if public_tables:
            raise DbError(
                "db.migration_failed",
                "PostgreSQL baseline requires an empty public schema.",
                "Use a fresh database or complete the reviewed recovery procedure.",
            )
        try:
            connection.executescript(baseline.sql)
        except sqlite3.Error as exc:
            raise DbError(
                "db.migration_failed",
                "PostgreSQL baseline installation failed.",
                "Inspect the non-ready bootstrap state and database migration logs.",
            ) from exc
        _verify_postgresql_baseline(connection, source_migrations)
        applied.extend(migration.version for migration in source_migrations)
    else:
        _verify_postgresql_baseline(connection, source_migrations, state=state)
        skipped.extend(migration.version for migration in source_migrations)

    connection.rollback()
    for migration in native_migrations:
        if migration.version <= POSTGRESQL_BASELINE_VERSION:
            continue
        if _apply_one_postgresql_migration(connection, migration):
            applied.append(migration.version)
        else:
            skipped.append(migration.version)
    return MigrationResult(run_id=run_id, applied=applied, skipped=skipped)


def _rollback_postgresql_migration(
    connection: Any,
    *,
    run_id: str,
    migrations_dir: Path | None,
    expected_version: str | None,
) -> MigrationResult:
    """Rollback only post-baseline native deltas under one advisory lock."""

    root, native_directory = _postgresql_migration_directories(migrations_dir)
    _verify_postgresql_baseline(connection, _postgresql_baseline_sources(root))
    row = connection.execute(
        "SELECT version,name FROM schema_migrations "
        "WHERE status='applied' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        connection.rollback()
        if expected_version is not None:
            raise _migration_version_mismatch()
        return MigrationResult(run_id=run_id, applied=[], skipped=[], rolled_back=None)
    if expected_version is not None and row["version"] != expected_version:
        connection.rollback()
        raise _migration_version_mismatch()
    if row["version"] <= POSTGRESQL_BASELINE_VERSION:
        connection.rollback()
        raise DbError(
            "db.migration_failed",
            "The PostgreSQL baseline cannot be rolled back in place.",
            "Restore a reviewed backup into an isolated database for baseline rollback.",
        )

    down_path = native_directory / f"{row['version']}_{row['name']}.down.sql"
    if not down_path.exists():
        connection.rollback()
        raise DbError(
            "db.migration_failed",
            f"Rollback script not found for migration {row['version']}.",
            "Create a reviewed PostgreSQL-native down migration.",
        )
    down_sql = down_path.read_text(encoding="utf-8")
    _require_transaction_body(down_sql)
    version = row["version"]
    connection.rollback()
    started = time.perf_counter()
    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('metahub.schema.migration',0))"
        ).close()
        locked = connection.execute(
            "SELECT version FROM schema_migrations "
            "WHERE status='applied' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if locked is None or locked["version"] != version:
            raise _migration_version_mismatch()
        connection.execute(down_sql).close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        cursor = connection.execute(
            "UPDATE schema_migrations SET status='rolled_back',"
            "applied_at=metahub_utc_now(),execution_time_ms=?,error_message=NULL "
            "WHERE version=? AND status='applied'",
            (elapsed_ms, version),
        )
        if cursor.rowcount != 1:
            raise sqlite3.DatabaseError("Migration state update failed.")
        cursor.close()
        connection.commit()
    except DbError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise DbError(
            "db.migration_failed",
            f"Rollback failed for migration {version}.",
            "Inspect the reviewed PostgreSQL rollback and database state.",
        ) from exc
    return MigrationResult(run_id=run_id, applied=[], skipped=[], rolled_back=version)


def _postgresql_migration_directories(
    migrations_dir: Path | None,
) -> tuple[Path, Path]:
    root = migrations_dir or _default_migrations_dir()
    if root.name == "postgresql":
        return root.parent, root
    return root, root / "postgresql"


def _postgresql_baseline_sources(root: Path) -> list[MigrationFile]:
    migrations = [
        migration
        for migration in _load_migration_files(root)
        if migration.version <= POSTGRESQL_BASELINE_VERSION
    ]
    if (
        len(migrations) != 139
        or not migrations
        or migrations[-1].version != POSTGRESQL_BASELINE_VERSION
        or len({migration.version for migration in migrations}) != len(migrations)
    ):
        raise DbError(
            "db.migration_failed",
            "The SQLite source manifest for the PostgreSQL baseline is incomplete.",
            "Restore the reviewed 0001 through 0139 migration chain.",
        )
    return migrations


def _postgresql_manifest_sha256(migrations: list[MigrationFile]) -> str:
    digest = hashlib.sha256()
    for migration in migrations:
        digest.update(
            f"{migration.version}\0{migration.name}\0{migration.checksum}\n".encode()
        )
    return digest.hexdigest()


def _postgresql_baseline_state(connection: Any):
    exists = connection.execute(
        "SELECT to_regclass('metahub_bootstrap.baseline_state')"
    ).fetchone()[0]
    if exists is None:
        return None
    rows = connection.execute(
        "SELECT baseline_version,source_manifest_sha256,generator_revision,status,"
        "application_table_count,check_constraint_count,foreign_key_count,"
        "trigger_count,seed_row_count,completed_at "
        f"FROM {POSTGRESQL_BASELINE_STATE}"
    ).fetchall()
    if len(rows) != 1:
        raise DbError(
            "db.migration_failed",
            "PostgreSQL baseline state is invalid.",
            "Recover from the reviewed baseline backup before startup.",
        )
    return rows[0]


def _verify_postgresql_baseline(
    connection: Any,
    source_migrations: list[MigrationFile],
    *,
    state: Any | None = None,
) -> None:
    state = state or _postgresql_baseline_state(connection)
    expected_manifest = _postgresql_manifest_sha256(source_migrations)
    if (
        state is None
        or state["status"] != "ready"
        or state["baseline_version"] != POSTGRESQL_BASELINE_VERSION
        or state["source_manifest_sha256"] != expected_manifest
        or state["generator_revision"] != POSTGRESQL_BASELINE_GENERATOR_REVISION
        or state["completed_at"] is None
    ):
        raise DbError(
            "db.migration_failed",
            "PostgreSQL baseline is not ready or its source manifest changed.",
            "Do not start services; recover or regenerate the reviewed baseline.",
        )

    live = connection.execute(
        "SELECT "
        "(SELECT count(*) FROM pg_tables WHERE schemaname='public') AS tables,"
        "(SELECT count(*) FROM pg_constraint c JOIN pg_namespace n "
        " ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='c' "
        " AND c.convalidated) AS checks,"
        "(SELECT count(*) FROM pg_constraint c JOIN pg_namespace n "
        " ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='f' "
        " AND c.convalidated) AS foreign_keys,"
        "(SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid "
        " JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' "
        " AND NOT t.tgisinternal AND t.tgenabled='O') AS triggers,"
        "(SELECT count(*) FROM pg_constraint c JOIN pg_namespace n "
        " ON n.oid=c.connamespace WHERE n.nspname='public' "
        " AND c.contype IN('c','f') AND NOT c.convalidated) AS invalid_constraints,"
        "(SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid "
        " JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' "
        " AND NOT t.tgisinternal AND t.tgenabled!='O') AS disabled_triggers"
    ).fetchone()
    expected_counts = (
        state["application_table_count"],
        state["check_constraint_count"],
        state["foreign_key_count"],
        state["trigger_count"],
    )
    live_counts = tuple(live)
    if (
        any(
            actual < expected
            for actual, expected in zip(live_counts[:4], expected_counts, strict=True)
        )
        or live_counts[4:] != (0, 0)
    ):
        raise DbError(
            "db.migration_failed",
            "PostgreSQL baseline object validation failed.",
            "Do not start services; restore the reviewed schema state.",
        )

    rows = connection.execute(
        "SELECT version,name,checksum,status FROM schema_migrations "
        "WHERE version<=? ORDER BY version",
        (POSTGRESQL_BASELINE_VERSION,),
    ).fetchall()
    expected_rows = [
        (migration.version, migration.name, migration.checksum, "applied")
        for migration in source_migrations
    ]
    if [tuple(row) for row in rows] != expected_rows:
        raise DbError(
            "db.migration_checksum_changed",
            "PostgreSQL baseline migration history changed.",
            "Restore the reviewed source migrations and database history.",
        )


def _apply_one_postgresql_migration(connection: Any, migration: MigrationFile) -> bool:
    _require_transaction_body(migration.sql)
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('metahub.schema.migration',0))"
    ).close()
    existing = connection.execute(
        "SELECT checksum,status FROM schema_migrations WHERE version=?",
        (migration.version,),
    ).fetchone()
    if existing is not None and existing["status"] == "applied":
        if existing["checksum"] != migration.checksum:
            connection.rollback()
            raise DbError(
                "db.migration_checksum_changed",
                f"Migration {migration.version} checksum changed.",
                "Do not edit applied PostgreSQL migrations; create a new migration.",
            )
        connection.commit()
        return False

    started = time.perf_counter()
    try:
        connection.execute(migration.sql).close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        connection.execute(
            "INSERT INTO schema_migrations(version,name,checksum,status,applied_at,"
            "execution_time_ms,error_message) VALUES(?,?,?,'applied',metahub_utc_now(),?,NULL) "
            "ON CONFLICT(version) DO UPDATE SET name=excluded.name,checksum=excluded.checksum,"
            "status='applied',applied_at=excluded.applied_at,"
            "execution_time_ms=excluded.execution_time_ms,error_message=NULL",
            (migration.version, migration.name, migration.checksum, elapsed_ms),
        ).close()
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        _record_failed_postgresql_migration(connection, migration, started)
        raise DbError(
            "db.migration_failed",
            f"Migration {migration.version} failed.",
            "Fix the reviewed PostgreSQL-native migration and rerun db:migrate.",
        ) from exc
    return True


def _record_failed_postgresql_migration(
    connection: Any, migration: MigrationFile, started: float
) -> None:
    try:
        # A retry can win after the failed DDL releases its transaction lock.
        # Serialize history again and never overwrite that retry's terminal fact.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('metahub.schema.migration',0))"
        ).close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        connection.execute(
            "INSERT INTO schema_migrations(version,name,checksum,status,applied_at,"
            "execution_time_ms,error_message) VALUES(?,?,?,'failed',metahub_utc_now(),?,NULL) "
            "ON CONFLICT(version) DO UPDATE SET name=excluded.name,checksum=excluded.checksum,"
            "status='failed',applied_at=excluded.applied_at,"
            "execution_time_ms=excluded.execution_time_ms,error_message=NULL "
            "WHERE schema_migrations.status='failed'",
            (migration.version, migration.name, migration.checksum, elapsed_ms),
        ).close()
        connection.commit()
    except sqlite3.Error:
        connection.rollback()


def _require_transaction_body(sql: str) -> None:
    try:
        from pglast import ast, parse_sql
        from pglast.parser import ParseError
    except ImportError as exc:
        raise DbError(
            "db.migration_failed",
            "PostgreSQL migration parser is unavailable.",
            "Install the reviewed PostgreSQL runtime dependencies before migration.",
        ) from exc
    try:
        statements = parse_sql(sql)
    except ParseError as exc:
        raise DbError(
            "db.migration_failed",
            "PostgreSQL delta cannot be parsed.",
            "Review the native SQL before running the migration.",
        ) from exc
    if any(isinstance(statement.stmt, ast.TransactionStmt) for statement in statements):
        raise DbError(
            "db.migration_failed",
            "PostgreSQL delta contains a transaction boundary.",
            "Let the migration runner own the native delta transaction.",
        )


def _migration_version_mismatch() -> DbError:
    """返回不包含实际版本或路径的固定失败坐标。"""

    return DbError(
        "db.migration_version_mismatch",
        "The applied migration does not match the expected rollback version.",
        "Refresh migration state and use the owner-specific maintenance command.",
    )


def _ensure_schema_migrations(connection: sqlite3.Connection) -> None:
    """在 数据库基础设施 内部复用 ensure schema migrations 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('applied', 'rolled_back', 'failed')),
            applied_at TEXT NOT NULL,
            execution_time_ms INTEGER NOT NULL,
            error_message TEXT
        )
        """
    )
    connection.commit()


def _existing_migration(connection: sqlite3.Connection, version: str) -> sqlite3.Row | None:
    """在 数据库基础设施 内部复用 existing migration 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return connection.execute(
        "SELECT version, name, checksum, status FROM schema_migrations WHERE version=?",
        (version,),
    ).fetchone()


def _apply_one_migration(connection: sqlite3.Connection, migration: MigrationFile) -> None:
    """在 数据库基础设施 内部复用 apply one migration 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    started = time.perf_counter()
    try:
        atomic_sql = _inject_atomic_migration_registration(migration)
        connection.executescript(atomic_sql or migration.sql)
        if atomic_sql is None:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            connection.execute(
                """
                INSERT INTO schema_migrations(
                  version,name,checksum,status,applied_at,execution_time_ms,error_message
                ) VALUES(?,?,?,'applied',datetime('now'),?,NULL)
                ON CONFLICT(version) DO UPDATE SET
                  name=excluded.name,
                  checksum=excluded.checksum,
                  status='applied',
                  applied_at=excluded.applied_at,
                  execution_time_ms=excluded.execution_time_ms,
                  error_message=NULL
                """,
                (migration.version, migration.name, migration.checksum, elapsed_ms),
            )
            connection.commit()
    # 正向脚本的 SQL 或提交失败时，先撤销迁移事务，再单独记录失败状态。
    except sqlite3.Error as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # 失败记录必须在 rollback 之后写入，否则 commit 会连同临时表或部分数据一起提交。
        connection.rollback()
        _record_failed_migration(connection, migration, elapsed_ms, exc)
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.migration_failed",
            f"Migration {migration.version} failed: {exc}",
            "Fix the migration SQL and rerun db:migrate.",
        ) from exc


def _inject_atomic_migration_registration(migration: MigrationFile) -> str | None:
    """把版本登记注入单一顶层事务；无显式事务的旧脚本保持原执行路径。"""

    begin_matches = tuple(TOP_LEVEL_BEGIN_RE.finditer(migration.sql))
    commit_matches = tuple(TOP_LEVEL_COMMIT_RE.finditer(migration.sql))
    if not begin_matches and not commit_matches:
        return None
    if (
        len(begin_matches) != 1
        or len(commit_matches) != 1
        or begin_matches[0].start() >= commit_matches[0].start()
    ):
        # 原子登记只支持一个明确的顶层事务，形状歧义时拒绝改写迁移脚本。
        raise DbError(
            "db.migration_failed",
            "Migration transaction shape is not supported for atomic registration.",
            "Use one reviewed top-level BEGIN and one final COMMIT.",
        )

    version = _sqlite_text_literal(migration.version)
    name = _sqlite_text_literal(migration.name)
    checksum = _sqlite_text_literal(migration.checksum)
    registration = f"""
-- Schema changes and their applied-version fact must become visible together.
DROP TABLE IF EXISTS temp.metahub_migration_registration_guard;
CREATE TEMP TABLE metahub_migration_registration_guard(
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER metahub_migration_registration_guard_abort
BEFORE INSERT ON metahub_migration_registration_guard FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'db_migration_registration_failed'); END;
INSERT INTO schema_migrations(
  version,name,checksum,status,applied_at,execution_time_ms,error_message
) VALUES({version},{name},{checksum},'applied',datetime('now'),0,NULL)
ON CONFLICT(version) DO UPDATE SET
  name=excluded.name,
  checksum=excluded.checksum,
  status='applied',
  applied_at=excluded.applied_at,
  execution_time_ms=excluded.execution_time_ms,
  error_message=NULL;
INSERT INTO metahub_migration_registration_guard(issue_count)
VALUES(CASE WHEN changes()=1 THEN 0 ELSE 1 END);
DROP TRIGGER metahub_migration_registration_guard_abort;
DROP TABLE metahub_migration_registration_guard;
"""
    commit_at = commit_matches[0].start()
    return migration.sql[:commit_at] + registration + migration.sql[commit_at:]


def _sqlite_text_literal(value: str) -> str:
    """生成 SQLite TEXT literal，禁止 NUL 改变脚本边界或底层解析。"""

    # NUL 会越过 SQL 字面量转义边界，迁移元数据必须在脚本拼接前失败关闭。
    if "\x00" in value:
        raise DbError(
            "db.migration_failed",
            "Migration metadata contains an invalid character.",
            "Use a migration filename and checksum without NUL characters.",
        )
    return "'" + value.replace("'", "''") + "'"


def _record_failed_migration(
    connection: sqlite3.Connection,
    migration: MigrationFile,
    elapsed_ms: int,
    original_error: sqlite3.Error,
) -> None:
    """尽力记录失败；登记表自身拒绝写入时不得遮蔽原始迁移错误。"""

    try:
        connection.execute(
            """
            INSERT INTO schema_migrations(
              version,name,checksum,status,applied_at,execution_time_ms,error_message
            ) VALUES(?,?,?,'failed',datetime('now'),?,?)
            ON CONFLICT(version) DO UPDATE SET
              name=excluded.name,
              checksum=excluded.checksum,
              status='failed',
              applied_at=excluded.applied_at,
              execution_time_ms=excluded.execution_time_ms,
              error_message=excluded.error_message
            """,
            (
                migration.version,
                migration.name,
                migration.checksum,
                elapsed_ms,
                str(original_error)[:1000],
            ),
        )
        connection.commit()
    # 失败状态只是辅助证据；登记表不可写时回滚，不能覆盖原始迁移异常。
    except sqlite3.Error:
        connection.rollback()


def _load_migration_files(directory: Path) -> list[MigrationFile]:
    """按当前租户加载 数据库基础设施 的 load migration files 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    if not directory.exists():
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.migration_failed",
            f"Migration directory does not exist: {directory}",
            "Check infra/migrations path.",
        )
    files = sorted(path for path in directory.glob("*.sql") if not path.name.endswith(".down.sql"))
    migrations: list[MigrationFile] = []
    for path in files:
        version, name = _parse_migration_name(path)
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(
            MigrationFile(
                version=version,
                name=name,
                path=path,
                checksum=checksum,
                sql=sql,
            )
        )
    return migrations


def _parse_migration_name(path: Path) -> tuple[str, str]:
    """解析 数据库基础设施 的 parse migration name 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    stem = path.stem
    if "_" not in stem:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.migration_failed",
            f"Invalid migration filename: {path.name}",
            "Use <version>_<name>.sql, for example 0001_core_mvp_tables.sql.",
        )
    version, name = stem.split("_", 1)
    return version, name


def _default_migrations_dir() -> Path:
    """在 数据库基础设施 内部复用 default migrations dir 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return Path(os.getenv("METAHUB_MIGRATIONS_DIR", "infra/migrations"))
