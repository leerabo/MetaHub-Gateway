"""PRD-061 0053 Provider 状态页迁移的保真、索引和原子回滚专项测试。"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

# 专项测试始终复制正式迁移目录，避免手工 SQL 与生产迁移内容发生漂移。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占 SQLite 文件转换为迁移器 URL，确保每个用例隔离运行。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于目标版本的正式迁移，构造确定的 0052/0053 栈顶。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 内容完整性和全部外键，不能只凭迁移版本判断成功。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _dump_without_migration_version(
    connection: sqlite3.Connection,
    version: str,
) -> tuple[str, ...]:
    """冻结业务库并排除指定迁移的运行日志。

    参数：当前连接和迁移版本。返回：可稳定比较的 ``iterdump`` 行。
    迁移器会在失败 SQL 回滚后用独立事务记录 ``status=failed``；原子性测试必须保留并单独
    断言该运维证据，同时只从业务结构与数据的前后比较中排除这一行。
    """

    marker = f"VALUES('{version}',"
    return tuple(
        line
        for line in connection.iterdump()
        if not (line.startswith('INSERT INTO "schema_migrations"') and marker in line)
    )


def _event_snapshot(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    """冻结事件表 rowid 与全部十二列，检测重建过程的任意漂移。"""

    return [
        tuple(row)
        for row in connection.execute(
            "SELECT rowid,* FROM console_page_events ORDER BY rowid"
        ).fetchall()
    ]


def _event_index_signatures(
    connection: sqlite3.Connection,
) -> dict[str, tuple[int, str, int, tuple[tuple[str, int], ...]]]:
    """读取事件表普通索引的唯一性、来源、partial 标志和有序列方向。

    事件表在 up/down 都会重建，单纯断言索引名称存在无法发现列顺序或 DESC 方向漂移；
    因此测试冻结 SQLite ``pragma_index_list/index_xinfo`` 的完整可执行签名。
    """

    signatures: dict[str, tuple[int, str, int, tuple[tuple[str, int], ...]]] = {}
    for index in connection.execute("PRAGMA index_list(console_page_events)").fetchall():
        if index["origin"] != "c":
            continue
        columns = tuple(
            (str(column["name"]), int(column["desc"]))
            for column in connection.execute(
                f'PRAGMA index_xinfo("{index["name"]}")'
            ).fetchall()
            if int(column["key"]) == 1
        )
        signatures[str(index["name"])] = (
            int(index["unique"]),
            str(index["origin"]),
            int(index["partial"]),
            columns,
        )
    return signatures


def _provider_snapshot_without_note(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    """按迁移前正式列顺序冻结 Provider rowid 和全部业务字段。"""

    columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(providers)").fetchall()
        if row["name"] != "maintenance_note"
    ]
    quoted = ",".join(f'"{column}"' for column in columns)
    return [
        tuple(row)
        for row in connection.execute(f"SELECT rowid,{quoted} FROM providers ORDER BY rowid")
    ]


def _insert_old_event(connection: sqlite3.Connection, tenant_id: str) -> int:
    """插入 0030 已允许的竞品帮助事件，并返回其稳定 rowid。"""

    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin' ORDER BY user_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert admin is not None
    connection.execute(
        """
        INSERT INTO console_page_events(
          event_id,tenant_id,user_id,session_id,event_type,navigation_item_id,
          target_section,path,request_id,trace_id,payload_json,created_at
        ) VALUES(
          'cpe_prd061_old',?,?,NULL,'help.competitor_viewed','competitor-comparison',
          'competitor-comparison','/#competitor-comparison','req_prd061_old',
          'trc_prd061_old','{}','2026-07-31T12:00:00.000Z'
        )
        """,
        (tenant_id, admin["user_id"]),
    )
    connection.commit()
    return int(
        connection.execute(
            "SELECT rowid FROM console_page_events WHERE event_id='cpe_prd061_old'"
        ).fetchone()[0]
    )


def test_prd061_migration_up_down_reup_preserves_rows_and_events(tmp_path: Path) -> None:
    """主流程：0053 up/down/re-up 保留 Provider 与事件，且只精确降级新事件名。"""

    database_url = _sqlite_url(tmp_path / "prd061-roundtrip.db")
    through_0052 = _migration_subset(tmp_path / "migrations-0052", 52)
    through_0053 = _migration_subset(tmp_path / "migrations-0053", 53)
    assert apply_migrations(database_url, migrations_dir=through_0052).applied == [
        f"{version:04d}" for version in range(1, 53)
    ]
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    old_rowid = _insert_old_event(connection, seed.tenant_id)
    providers_before = _provider_snapshot_without_note(connection)
    events_before = _event_snapshot(connection)
    event_indexes_before = _event_index_signatures(connection)

    assert apply_migrations(database_url, migrations_dir=through_0053).applied == ["0053"]
    connection = connect_database(database_url)
    assert "maintenance_note" in {
        row["name"] for row in connection.execute("PRAGMA table_info(providers)")
    }
    assert _provider_snapshot_without_note(connection) == providers_before
    assert _event_snapshot(connection) == events_before
    assert _event_index_signatures(connection) == event_indexes_before
    assert connection.execute(
        "SELECT rowid FROM console_page_events WHERE event_id='cpe_prd061_old'"
    ).fetchone()[0] == old_rowid

    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin' ORDER BY user_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO console_page_events(
          event_id,tenant_id,user_id,event_type,navigation_item_id,target_section,path,
          request_id,trace_id,payload_json,created_at
        ) VALUES(
          'cpe_prd061_status',?,?,'status.viewed','provider-health','provider-health',
          '/#provider-health','req_prd061_status','trc_prd061_status','{}',
          '2026-07-31T12:01:00.000Z'
        )
        """,
        (seed.tenant_id, admin["user_id"]),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO console_page_events(
              event_id,tenant_id,user_id,event_type,navigation_item_id,target_section,path,
              request_id,payload_json,created_at
            ) VALUES('cpe_prd061_unknown',?,?,'unknown.viewed','provider-health',
                     'provider-health','/#provider-health','req_unknown','{}',
                     '2026-07-31T12:02:00.000Z')
            """,
            (seed.tenant_id, admin["user_id"]),
        )
    connection.commit()
    status_rowid = connection.execute(
        "SELECT rowid FROM console_page_events WHERE event_id='cpe_prd061_status'"
    ).fetchone()[0]

    assert rollback_last_migration(
        database_url, migrations_dir=through_0053
    ).rolled_back == "0053"
    connection = connect_database(database_url)
    assert "maintenance_note" not in {
        row["name"] for row in connection.execute("PRAGMA table_info(providers)")
    }
    assert _provider_snapshot_without_note(connection) == providers_before
    assert _event_index_signatures(connection) == event_indexes_before
    assert tuple(
        connection.execute(
            "SELECT event_type,rowid FROM console_page_events WHERE event_id='cpe_prd061_status'"
        ).fetchone()
    ) == ("console.page_viewed", status_rowid)
    _assert_healthy(connection)

    assert apply_migrations(database_url, migrations_dir=through_0053).applied == ["0053"]
    connection = connect_database(database_url)
    assert tuple(
        connection.execute(
            "SELECT event_type,rowid FROM console_page_events WHERE event_id='cpe_prd061_status'"
        ).fetchone()
    ) == ("console.page_viewed", status_rowid)
    assert _provider_snapshot_without_note(connection) == providers_before
    assert _event_index_signatures(connection) == event_indexes_before
    _assert_healthy(connection)


def test_prd061_indexes_constraints_and_query_plans(tmp_path: Path) -> None:
    """索引/约束：四个索引命中代表查询，备注边界拒绝控制字符与超长值。"""

    database_url = _sqlite_url(tmp_path / "prd061-indexes.db")
    migrations = _migration_subset(tmp_path / "migrations", 53)
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY provider_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()[0]
    accepted_note = "维" * 240
    connection.execute(
        "UPDATE providers SET maintenance_note=? WHERE tenant_id=? AND provider_id=?",
        (accepted_note, seed.tenant_id, provider_id),
    )
    assert connection.execute(
        "SELECT maintenance_note FROM providers WHERE tenant_id=? AND provider_id=?",
        (seed.tenant_id, provider_id),
    ).fetchone()[0] == accepted_note
    invalid_notes = ["", " ", " 前导空格", "尾随空格 ", "长" * 241]
    invalid_notes.extend(
        f"前{chr(codepoint)}后"
        for codepoint in (*range(0, 32), *range(127, 160))
    )
    for invalid_note in invalid_notes:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE providers SET maintenance_note=? WHERE tenant_id=? AND provider_id=?",
                (invalid_note, seed.tenant_id, provider_id),
            )
    connection.rollback()

    index_sql = {
        row["name"]: row["sql"]
        for row in connection.execute(
            """
            SELECT name,sql FROM sqlite_schema
            WHERE type='index' AND name LIKE 'idx_%tenant_provider%'
            """
        )
    }
    assert "WHERE status IN ('succeeded', 'failed')" in index_sql[
        "idx_routing_attempts_tenant_provider_completed"
    ]
    assert "completed_at IS NOT NULL" in index_sql[
        "idx_stream_sessions_tenant_provider_completed"
    ]
    plans = [
        connection.execute(
            """
            EXPLAIN QUERY PLAN SELECT routing_attempt_id FROM routing_attempts
            WHERE tenant_id=? AND provider_id=? AND status IN ('succeeded','failed')
              AND completed_at IS NOT NULL AND completed_at>=?
            ORDER BY completed_at DESC
            """,
            (seed.tenant_id, provider_id, "2026-07-30T00:00:00.000Z"),
        ).fetchall(),
        connection.execute(
            """
            EXPLAIN QUERY PLAN SELECT stream_session_id FROM stream_sessions
            WHERE tenant_id=? AND provider_id=?
              AND status IN ('completed','timed_out','client_disconnected','upstream_failed')
              AND completed_at IS NOT NULL AND completed_at>=?
            ORDER BY completed_at DESC
            """,
            (seed.tenant_id, provider_id, "2026-07-30T00:00:00.000Z"),
        ).fetchall(),
        connection.execute(
            """
            EXPLAIN QUERY PLAN SELECT request_id FROM provider_health_probe_logs
            WHERE tenant_id=? AND provider_id=? AND checked_at>=?
            ORDER BY checked_at DESC
            """,
            (seed.tenant_id, provider_id, "2026-07-30T00:00:00.000Z"),
        ).fetchall(),
        connection.execute(
            """
            EXPLAIN QUERY PLAN SELECT probe_log_id FROM provider_health_probe_logs
            WHERE tenant_id=? AND provider_id=? AND request_id=?
            ORDER BY attempt_sequence,checked_at,probe_log_id
            """,
            (seed.tenant_id, provider_id, "req_probe"),
        ).fetchall(),
    ]
    serialized = [" ".join(str(column) for row in plan for column in row) for plan in plans]
    assert "idx_routing_attempts_tenant_provider_completed" in serialized[0]
    assert "idx_stream_sessions_tenant_provider_completed" in serialized[1]
    assert "idx_provider_health_logs_tenant_provider_time_request" in serialized[2]
    assert "idx_provider_health_logs_tenant_provider_request_sequence" in serialized[3]
    _assert_healthy(connection)


def test_prd061_down_blocks_note_and_unknown_dependency_atomically(tmp_path: Path) -> None:
    """异常流程：非空备注或未知依赖均在首项持久 DDL 前阻断，并保持 0053 完整。"""

    database_url = _sqlite_url(tmp_path / "prd061-guards.db")
    migrations = _migration_subset(tmp_path / "migrations", 53)
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY provider_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()[0]
    connection.execute(
        "UPDATE providers SET maintenance_note='不可丢失' WHERE tenant_id=? AND provider_id=?",
        (seed.tenant_id, provider_id),
    )
    connection.commit()
    before = tuple(connection.iterdump())
    with pytest.raises(DbError):
        rollback_last_migration(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0053'"
    ).fetchone()[0] == "applied"

    connection.execute(
        "UPDATE providers SET maintenance_note=NULL WHERE tenant_id=? AND provider_id=?",
        (seed.tenant_id, provider_id),
    )
    connection.execute(
        "CREATE VIEW prd061_unknown_note_view AS SELECT maintenance_note FROM providers"
    )
    connection.commit()
    before_unknown = tuple(connection.iterdump())
    with pytest.raises(DbError):
        rollback_last_migration(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before_unknown
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0053'"
    ).fetchone()[0] == "applied"
    _assert_healthy(connection)


def test_prd061_up_rejects_unknown_schema_before_persistent_changes(tmp_path: Path) -> None:
    """异常流程：0052 表出现未知列时，up 在加列、建索引和事件重建之前原子阻断。"""

    database_url = _sqlite_url(tmp_path / "prd061-up-guard.db")
    through_0052 = _migration_subset(tmp_path / "migrations-0052", 52)
    through_0053 = _migration_subset(tmp_path / "migrations-0053", 53)
    apply_migrations(database_url, migrations_dir=through_0052)
    connection = connect_database(database_url)
    connection.execute("ALTER TABLE routing_attempts ADD COLUMN unknown_prd061_field TEXT")
    connection.commit()
    before = _dump_without_migration_version(connection, "0053")

    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=through_0053)

    connection = connect_database(database_url)
    assert _dump_without_migration_version(connection, "0053") == before
    assert "maintenance_note" not in {
        row["name"] for row in connection.execute("PRAGMA table_info(providers)")
    }
    failed = connection.execute(
        "SELECT status,execution_time_ms,error_message FROM schema_migrations WHERE version='0053'"
    ).fetchone()
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["execution_time_ms"] >= 0
    assert failed["error_message"]
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name IN (
          'idx_routing_attempts_tenant_provider_completed',
          'idx_stream_sessions_tenant_provider_completed',
          'idx_provider_health_logs_tenant_provider_time_request',
          'idx_provider_health_logs_tenant_provider_request_sequence'
        )
        """
    ).fetchone()[0] == 0


def test_prd061_up_blocks_unknown_event_dependencies_and_name_collision(
    tmp_path: Path,
) -> None:
    """异常流程：事件表未知依赖或重建目标名冲突不得被 up 静默删除。"""

    database_url = _sqlite_url(tmp_path / "prd061-up-event-dependency.db")
    through_0052 = _migration_subset(tmp_path / "migrations-0052", 52)
    through_0053 = _migration_subset(tmp_path / "migrations-0053", 53)
    apply_migrations(database_url, migrations_dir=through_0052)
    connection = connect_database(database_url)
    connection.executescript(
        """
        CREATE INDEX idx_prd061_unknown_event_path ON console_page_events(path);
        CREATE TRIGGER trg_prd061_unknown_event_insert
        AFTER INSERT ON console_page_events BEGIN SELECT 1; END;
        CREATE VIEW view_prd061_unknown_event AS
          SELECT event_id FROM console_page_events;
        CREATE TABLE console_page_events_prd061_new(marker TEXT);
        """
    )
    before = _dump_without_migration_version(connection, "0053")

    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=through_0053)

    connection = connect_database(database_url)
    assert _dump_without_migration_version(connection, "0053") == before
    assert {
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE name IN (
              'idx_prd061_unknown_event_path','trg_prd061_unknown_event_insert',
              'view_prd061_unknown_event','console_page_events_prd061_new'
            )
            """
        ).fetchall()
    } == {
        "idx_prd061_unknown_event_path",
        "trg_prd061_unknown_event_insert",
        "view_prd061_unknown_event",
        "console_page_events_prd061_new",
    }
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0053'"
    ).fetchone()[0] == "failed"
    assert "maintenance_note" not in {
        row["name"] for row in connection.execute("PRAGMA table_info(providers)")
    }


def test_prd061_up_blocks_event_index_signature_drift(tmp_path: Path) -> None:
    """异常流程：同名事件索引的列或方向漂移也必须在 up 首项持久 DDL 前阻断。"""

    database_url = _sqlite_url(tmp_path / "prd061-up-event-index.db")
    through_0052 = _migration_subset(tmp_path / "migrations-0052", 52)
    through_0053 = _migration_subset(tmp_path / "migrations-0053", 53)
    apply_migrations(database_url, migrations_dir=through_0052)
    connection = connect_database(database_url)
    connection.executescript(
        """
        DROP INDEX idx_console_page_events_item_time;
        CREATE INDEX idx_console_page_events_item_time
          ON console_page_events(tenant_id, path, created_at ASC);
        """
    )
    before = _dump_without_migration_version(connection, "0053")

    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=through_0053)

    connection = connect_database(database_url)
    assert _dump_without_migration_version(connection, "0053") == before
    assert _event_index_signatures(connection)["idx_console_page_events_item_time"][3] == (
        ("tenant_id", 0),
        ("path", 0),
        ("created_at", 0),
    )
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0053'"
    ).fetchone()[0] == "failed"


def test_prd061_down_blocks_unknown_event_dependencies_and_name_collision(
    tmp_path: Path,
) -> None:
    """异常流程：down 不得删除 0053 后新增的事件依赖或占用重建名称的持久对象。"""

    database_url = _sqlite_url(tmp_path / "prd061-down-event-dependency.db")
    migrations = _migration_subset(tmp_path / "migrations", 53)
    apply_migrations(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    connection.executescript(
        """
        CREATE INDEX idx_prd061_unknown_event_path ON console_page_events(path);
        CREATE TRIGGER trg_prd061_unknown_event_insert
        AFTER INSERT ON console_page_events BEGIN SELECT 1; END;
        CREATE VIEW view_prd061_unknown_event AS
          SELECT event_id FROM console_page_events;
        CREATE TABLE console_page_events_prd061_old(marker TEXT);
        """
    )
    before = tuple(connection.iterdump())

    with pytest.raises(DbError):
        rollback_last_migration(database_url, migrations_dir=migrations)

    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0053'"
    ).fetchone()[0] == "applied"
    assert "maintenance_note" in {
        row["name"] for row in connection.execute("PRAGMA table_info(providers)")
    }


def test_prd061_down_blocks_event_index_signature_drift(tmp_path: Path) -> None:
    """异常流程：down 也必须拒绝同名但列签名已漂移的事件索引。"""

    database_url = _sqlite_url(tmp_path / "prd061-down-event-index.db")
    migrations = _migration_subset(tmp_path / "migrations", 53)
    apply_migrations(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    connection.executescript(
        """
        DROP INDEX idx_console_page_events_item_time;
        CREATE INDEX idx_console_page_events_item_time
          ON console_page_events(tenant_id, path, created_at ASC);
        """
    )
    before = tuple(connection.iterdump())

    with pytest.raises(DbError):
        rollback_last_migration(database_url, migrations_dir=migrations)

    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before
    assert _event_index_signatures(connection)["idx_console_page_events_item_time"][3] == (
        ("tenant_id", 0),
        ("path", 0),
        ("created_at", 0),
    )
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0053'"
    ).fetchone()[0] == "applied"
