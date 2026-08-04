"""PRD-065 0057 角色首页配置、事件保真和原子回滚门禁专项测试。"""

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

# 专项测试复制正式迁移文件，确保测试执行的 SQL 与交付脚本始终一致。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"

# 六角色初始配置是迁移、服务端视图目录和回滚安全门禁共享的冻结合同。
CANONICAL_CONFIGS = {
    "admin": (
        "executive",
        '["executive","finance","security","developer","operations"]',
    ),
    "developer": ("developer", '["developer","operations","executive"]'),
    "viewer": ("executive", '["executive"]'),
    "finance_admin": ("finance", '["finance","executive"]'),
    "security_admin": ("security", '["security","executive"]'),
    "ops_admin": ("operations", '["operations","executive"]'),
}

EVENT_INDEX_SIGNATURES = {
    "idx_console_page_events_tenant_time": (
        0,
        "c",
        0,
        (("tenant_id", 0, "BINARY"), ("created_at", 1, "BINARY")),
    ),
    "idx_console_page_events_user_time": (
        0,
        "c",
        0,
        (
            ("tenant_id", 0, "BINARY"),
            ("user_id", 0, "BINARY"),
            ("created_at", 1, "BINARY"),
        ),
    ),
    "idx_console_page_events_item_time": (
        0,
        "c",
        0,
        (
            ("tenant_id", 0, "BINARY"),
            ("navigation_item_id", 0, "BINARY"),
            ("created_at", 1, "BINARY"),
        ),
    ),
}


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为迁移器 URL，避免用例之间共享状态。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制指定栈顶及之前的正式 up/down SQL，构造可重复的迁移边界。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd065_migration_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """一次构建 0056 与 fresh 0057 基线，供专项用例复制后隔离验证。

    历史 57 条迁移无需在每个参数边界重复执行；模板连接在复制前已经关闭，
    每个测试仍使用自己的数据库文件，不共享事务、失败状态或业务事实。
    """

    root = tmp_path_factory.mktemp("prd065-migration-templates")
    through_0056 = _migration_subset(root / "migrations-0056", 56)
    through_0057 = _migration_subset(root / "migrations-0057", 57)
    database_0056 = root / "template-0056.db"
    database_0057 = root / "template-0057.db"
    assert apply_migrations(
        _sqlite_url(database_0056), migrations_dir=through_0056
    ).applied[-1] == "0056"
    assert apply_migrations(
        _sqlite_url(database_0057), migrations_dir=through_0057
    ).applied[-1] == "0057"
    return {
        "database_0056": database_0056,
        "database_0057": database_0057,
        "migrations_0056": through_0056,
        "migrations_0057": through_0057,
    }


def _clone_database(template: Path, destination: Path) -> str:
    """复制已关闭连接的只读基线，并返回当前用例专属 SQLite URL。"""

    shutil.copy2(template, destination)
    return _sqlite_url(destination)


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """同时验证 SQLite 页面完整性与复合租户外键，避免只看版本号误判成功。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _dump_without_migration_version(
    connection: sqlite3.Connection,
    version: str,
) -> tuple[str, ...]:
    """冻结业务结构与数据，但排除迁移器为失败版本单独保存的运维状态行。"""

    marker = f"VALUES('{version}',"
    return tuple(
        line
        for line in connection.iterdump()
        if not (line.startswith('INSERT INTO "schema_migrations"') and marker in line)
    )


def _event_snapshot(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    """冻结事件 rowid 和全部十二列，检测重建过程中任何审计字段漂移。"""

    return [
        tuple(row)
        for row in connection.execute(
            "SELECT rowid,* FROM console_page_events ORDER BY rowid"
        ).fetchall()
    ]


def _event_index_signatures(
    connection: sqlite3.Connection,
) -> dict[str, tuple[int, str, int, tuple[tuple[str, int, str], ...]]]:
    """读取普通索引唯一性、来源、partial、列顺序、方向与 collation。"""

    signatures: dict[
        str,
        tuple[int, str, int, tuple[tuple[str, int, str], ...]],
    ] = {}
    for index in connection.execute("PRAGMA index_list(console_page_events)"):
        if index[3] != "c":
            continue
        columns = tuple(
            (str(column[2]), int(column[3]), str(column[4]))
            for column in connection.execute(
                f'PRAGMA index_xinfo("{index[1]}")'
            ).fetchall()
            if int(column[5]) == 1
        )
        signatures[str(index[1])] = (
            int(index[2]),
            str(index[3]),
            int(index[4]),
            columns,
        )
    return signatures


def _assert_canonical_configs(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> None:
    """核对指定租户精确拥有六条 revision 1 active 初始角色首页配置。"""

    rows = connection.execute(
        """
        SELECT role_code,default_view_code,allowed_view_codes_json,
               config_revision,status,created_at,updated_at
        FROM role_dashboard_configs
        WHERE tenant_id=? ORDER BY role_code
        """,
        (tenant_id,),
    ).fetchall()
    assert len(rows) == 6
    for row in rows:
        expected_default, expected_allowed = CANONICAL_CONFIGS[str(row[0])]
        assert tuple(row[1:5]) == (
            expected_default,
            expected_allowed,
            1,
            "active",
        )
        assert row[5] == row[6]


def _insert_tenant(
    connection: sqlite3.Connection,
    tenant_id: str,
    slug: str,
) -> None:
    """插入最小合法租户，让现有 RBAC 与 PRD-065 初始化触发器在同一事务运行。"""

    timestamp = "2026-08-01T01:00:00Z"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (tenant_id, slug, f"Tenant {slug}", timestamp, timestamp),
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    tenant_id: str,
    user_id: str,
    event_type: str,
) -> int:
    """写入不含敏感正文的控制台事件，并返回用于保真断言的稳定 rowid。"""

    connection.execute(
        """
        INSERT INTO console_page_events(
          event_id,tenant_id,user_id,session_id,event_type,navigation_item_id,
          target_section,path,request_id,trace_id,payload_json,created_at
        ) VALUES(?,?,?,NULL,?,'overview','overview','/#overview',?,?,?,?)
        """,
        (
            event_id,
            tenant_id,
            user_id,
            event_type,
            f"req_{event_id}",
            f"trc_{event_id}",
            '{}',
            "2026-08-01T01:02:03.000Z",
        ),
    )
    return int(
        connection.execute(
            "SELECT rowid FROM console_page_events WHERE event_id=?",
            (event_id,),
        ).fetchone()[0]
    )


def test_prd065_up_down_reup_preserves_events_and_canonical_configs(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
) -> None:
    """主流程：0056→0057、幂等、down/re-up 保留事件且只映射新增事件类型。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0056"],
        tmp_path / "prd065-roundtrip.db",
    )
    through_0057 = prd065_migration_templates["migrations_0057"]
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND role='admin'",
        (seed.tenant_id,),
    ).fetchone()
    assert admin is not None
    old_rowid = _insert_event(
        connection,
        event_id="cpe_prd065_old",
        tenant_id=seed.tenant_id,
        user_id=str(admin[0]),
        event_type="status.viewed",
    )
    connection.commit()
    events_before = _event_snapshot(connection)
    indexes_before = _event_index_signatures(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0057).applied == ["0057"]
    connection = connect_database(database_url)
    assert [row[1] for row in connection.execute("PRAGMA table_info(role_dashboard_configs)")] == [
        "role_dashboard_config_id",
        "tenant_id",
        "role_code",
        "default_view_code",
        "allowed_view_codes_json",
        "config_revision",
        "status",
        "created_at",
        "updated_at",
    ]
    _assert_canonical_configs(connection, seed.tenant_id)
    assert _event_snapshot(connection) == events_before
    assert _event_index_signatures(connection) == indexes_before == EVENT_INDEX_SIGNATURES
    assert connection.execute(
        "SELECT rowid FROM console_page_events WHERE event_id='cpe_prd065_old'"
    ).fetchone()[0] == old_rowid
    role_rowid = _insert_event(
        connection,
        event_id="cpe_prd065_role",
        tenant_id=seed.tenant_id,
        user_id=str(admin[0]),
        event_type="dashboard.role_viewed",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(
            connection,
            event_id="cpe_prd065_unknown",
            tenant_id=seed.tenant_id,
            user_id=str(admin[0]),
            event_type="dashboard.unknown_viewed",
        )
    connection.commit()
    before_down = _event_snapshot(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0057).applied == []
    assert rollback_last_migration(
        database_url,
        migrations_dir=through_0057,
    ).rolled_back == "0057"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name='role_dashboard_configs'"
    ).fetchone()[0] == 0
    after_down = _event_snapshot(connection)
    assert len(after_down) == len(before_down)
    for source, target in zip(before_down, after_down, strict=True):
        expected = list(source)
        # 回滚只允许把新增角色首页事件映射为既有 Dashboard 事件，其余十二列不变。
        if expected[5] == "dashboard.role_viewed":
            expected[5] = "dashboard.viewed"
        assert tuple(expected) == target
    assert tuple(
        connection.execute(
            "SELECT event_type,rowid FROM console_page_events "
            "WHERE event_id='cpe_prd065_role'"
        ).fetchone()
    ) == ("dashboard.viewed", role_rowid)
    assert _event_index_signatures(connection) == EVENT_INDEX_SIGNATURES
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(database_url, migrations_dir=through_0057).applied == ["0057"]
    connection = connect_database(database_url)
    _assert_canonical_configs(connection, seed.tenant_id)
    assert tuple(
        connection.execute(
            "SELECT event_type,rowid FROM console_page_events "
            "WHERE event_id='cpe_prd065_role'"
        ).fetchone()
    ) == ("dashboard.viewed", role_rowid)
    _assert_healthy(connection)


def test_prd065_new_tenant_initializes_roles_and_configs_without_trigger_order(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
) -> None:
    """租户流程：两个 AFTER INSERT 触发器同事务完成，且不依赖创建或执行顺序。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0057"],
        tmp_path / "prd065-new-tenant.db",
    )
    connection = connect_database(database_url)
    _insert_tenant(connection, "tenant_prd065_ok", "prd065-ok")
    assert connection.execute(
        "SELECT COUNT(*) FROM roles WHERE tenant_id='tenant_prd065_ok'"
    ).fetchone()[0] == 6
    assert connection.execute(
        "SELECT COUNT(*) FROM role_dashboard_configs WHERE tenant_id='tenant_prd065_ok'"
    ).fetchone()[0] == 6
    connection.commit()
    _assert_canonical_configs(connection, "tenant_prd065_ok")
    _assert_healthy(connection)

    # 临时移除 RBAC 触发器后，PRD-065 触发器仍应在语句内生成六配置；
    # 延迟复合外键只在 COMMIT 发现角色缺失并撤销整个事务，证明实现没有查询 roles 或依赖顺序。
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DROP TRIGGER trg_tenants_rbac_initialize_prd059")
    _insert_tenant(connection, "tenant_prd065_deferred", "prd065-deferred")
    assert connection.execute(
        "SELECT COUNT(*) FROM roles WHERE tenant_id='tenant_prd065_deferred'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM role_dashboard_configs WHERE tenant_id='tenant_prd065_deferred'"
    ).fetchone()[0] == 6
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM tenants WHERE tenant_id='tenant_prd065_deferred'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name=?",
        ("trg_tenants_rbac_initialize_prd059",),
    ).fetchone()[0] == 1
    _assert_healthy(connection)


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("role_dashboard_config_id", "rdc_invalid"),
        ("role_code", "unknown_role"),
        ("default_view_code", "unknown_view"),
        ("allowed_view_codes_json", "not-json"),
        ("allowed_view_codes_json", '["executive","executive"]'),
        ("allowed_view_codes_json", '["finance"]'),
        ("allowed_view_codes_json", '["executive",1]'),
        ("config_revision", 0),
        ("status", "unknown"),
    ],
)
def test_prd065_rejects_invalid_configuration_values(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
    assignment: str,
    value: object,
) -> None:
    """异常流程：ID、角色、视图、JSON、revision 与状态的旁路修改均由数据库拒绝。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0057"],
        tmp_path / f"prd065-invalid-{assignment}.db",
    )
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"""
            UPDATE role_dashboard_configs SET {assignment}=?
            WHERE tenant_id=? AND role_code='admin'
            """,
            (value, seed.tenant_id),
        )
    connection.rollback()
    _assert_canonical_configs(connection, seed.tenant_id)
    _assert_healthy(connection)


def test_prd065_enforces_owner_revision_delete_and_replace_boundaries(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
) -> None:
    """权限边界：owner/identity 不可变，更新必须 revision+1，DELETE/REPLACE 均失败关闭。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0057"],
        tmp_path / "prd065-write-boundaries.db",
    )
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    admin = connection.execute(
        "SELECT * FROM role_dashboard_configs WHERE tenant_id=? AND role_code='admin'",
        (seed.tenant_id,),
    ).fetchone()
    assert admin is not None

    invalid_updates = [
        ("tenant_id", "tenant_missing"),
        ("role_code", "viewer"),
        ("created_at", "2026-08-01T02:00:00Z"),
        ("config_revision", 3),
        ("updated_at", admin[8]),
    ]
    for column, value in invalid_updates:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                UPDATE role_dashboard_configs SET {column}=?
                WHERE tenant_id=? AND role_code='admin'
                """,
                (value, seed.tenant_id),
            )
        connection.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM role_dashboard_configs WHERE tenant_id=? AND role_code='admin'",
            (seed.tenant_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT OR REPLACE INTO role_dashboard_configs(
              role_dashboard_config_id,tenant_id,role_code,default_view_code,
              allowed_view_codes_json,config_revision,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,1,'active',?,?)
            """,
            tuple(admin[index] for index in (0, 1, 2, 3, 4, 7, 8)),
        )
    connection.rollback()

    # INSERT trigger 不查询 tenant/roles，避免与两个 tenant AFTER trigger 形成顺序耦合；
    # canonical 数据可先进入事务，但提交必须由延迟复合 FK 拒绝不存在的 owner。
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        INSERT INTO role_dashboard_configs(
          role_dashboard_config_id,tenant_id,role_code,default_view_code,
          allowed_view_codes_json,config_revision,status,created_at,updated_at
        ) VALUES(
          'rdc_11111111111111111111111111111111','tenant_missing','admin','executive',
          '["executive","finance","security","developer","operations"]',
          1,'active','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z'
        )
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()
    connection.rollback()
    _assert_canonical_configs(connection, seed.tenant_id)
    _assert_healthy(connection)


def test_prd065_legal_revision_update_blocks_destructive_down_atomically(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
) -> None:
    """受控变更：合法 revision+1 可保存，但存在非 canonical 配置时 down 必须原子阻断。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0057"],
        tmp_path / "prd065-revision-down-guard.db",
    )
    migrations = prd065_migration_templates["migrations_0057"]
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE role_dashboard_configs
        SET default_view_code='finance',config_revision=2,
            updated_at='2099-01-01T00:00:00Z'
        WHERE tenant_id=? AND role_code='admin'
        """,
        (seed.tenant_id,),
    )
    connection.commit()
    before = tuple(connection.iterdump())
    connection.close()

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url, migrations_dir=migrations)
    assert exc.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before
    assert tuple(
        connection.execute(
            "SELECT config_revision,default_view_code FROM role_dashboard_configs "
            "WHERE tenant_id=? AND role_code='admin'",
            (seed.tenant_id,),
        ).fetchone()
    ) == (2, "finance")
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0057'"
    ).fetchone()[0] == "applied"
    _assert_healthy(connection)


@pytest.mark.parametrize("drift_kind", ["roles_column", "event_dependencies", "event_index"])
def test_prd065_up_rejects_schema_drift_before_persistent_changes(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
    drift_kind: str,
) -> None:
    """up 门禁：角色结构、事件依赖或同名索引签名漂移均不得被静默覆盖。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0056"],
        tmp_path / f"prd065-up-{drift_kind}.db",
    )
    through_0057 = prd065_migration_templates["migrations_0057"]
    connection = connect_database(database_url)
    # 三类漂移分别覆盖父角色列、事件未知依赖和仅 collation 变化的同名索引。
    if drift_kind == "roles_column":
        connection.execute("ALTER TABLE roles ADD COLUMN unknown_prd065 TEXT")
    elif drift_kind == "event_dependencies":
        connection.executescript(
            """
            CREATE VIEW view_prd065_unknown_event AS
              SELECT event_id FROM console_page_events;
            CREATE TABLE console_page_events_prd065_new(marker TEXT);
            """
        )
    else:
        connection.executescript(
            """
            DROP INDEX idx_console_page_events_item_time;
            CREATE INDEX idx_console_page_events_item_time
              ON console_page_events(
                tenant_id,navigation_item_id,created_at COLLATE NOCASE DESC
              );
            """
        )
    connection.commit()
    before = _dump_without_migration_version(connection, "0057")
    connection.close()

    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=through_0057)
    connection = connect_database(database_url)
    assert _dump_without_migration_version(connection, "0057") == before
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name='role_dashboard_configs'"
    ).fetchone()[0] == 0
    failed = connection.execute(
        "SELECT status,error_message FROM schema_migrations WHERE version='0057'"
    ).fetchone()
    assert failed is not None and failed[0] == "failed" and failed[1]


@pytest.mark.parametrize("drift_kind", ["unknown_dependency", "event_index", "target_collision"])
def test_prd065_down_rejects_schema_drift_atomically(
    tmp_path: Path,
    prd065_migration_templates: dict[str, Path],
    drift_kind: str,
) -> None:
    """down 门禁：未知配置依赖、事件索引漂移或重建名称碰撞都必须保留完整 0057。"""

    database_url = _clone_database(
        prd065_migration_templates["database_0057"],
        tmp_path / f"prd065-down-{drift_kind}.db",
    )
    migrations = prd065_migration_templates["migrations_0057"]
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    if drift_kind == "unknown_dependency":
        connection.execute(
            "CREATE VIEW view_prd065_unknown_config AS "
            "SELECT tenant_id FROM role_dashboard_configs"
        )
    elif drift_kind == "event_index":
        connection.executescript(
            """
            DROP INDEX idx_console_page_events_tenant_time;
            CREATE INDEX idx_console_page_events_tenant_time
              ON console_page_events(tenant_id,created_at COLLATE NOCASE DESC);
            """
        )
    else:
        connection.execute("CREATE TABLE console_page_events_prd065_old(marker TEXT)")
    connection.commit()
    before = tuple(connection.iterdump())
    connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    assert tuple(connection.iterdump()) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0057'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT COUNT(*) FROM role_dashboard_configs"
    ).fetchone()[0] == 6
    _assert_healthy(connection)
