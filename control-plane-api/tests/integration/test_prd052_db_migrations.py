"""PRD-052 三级预算数据库迁移与 owner/scope 触发器专项测试。

测试数据只使用安全 ID、整数微元和无效示例 URL，不保存 Prompt、Response、
Provider Key、Token 或 Virtual Key 明文；跨租户用例专门验证数据库第二道隔离防线。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError


# 迁移源目录只用于构造截至 0042 的只读历史快照，避免后续版本改变三级预算栈顶语义。
MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "infra" / "migrations"


@pytest.fixture(scope="module", autouse=True)
def prd052_migration_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """冻结只含 0001..0042 的迁移目录，使三级预算专项始终以自身版本为栈顶。

    参数：pytest 模块级临时目录工厂。返回：无业务值，仅在 fixture 生命周期内切换迁移目录。
    关键约束：本文件的 owner/scope 攻击、条件 down 和重放断言都必须命中 0042；0043
    及以后迁移不能抢占栈顶，也不能让异常用例误测其他 PRD 的回滚门禁。
    """

    snapshot_directory = tmp_path_factory.mktemp("prd052-migrations")
    copied_versions: set[str] = set()
    for source_file in MIGRATIONS_DIRECTORY.glob("*.sql"):
        version = source_file.name[:4]
        if not version.isdigit() or int(version) > 42:
            continue
        shutil.copy2(source_file, snapshot_directory / source_file.name)
        if not source_file.name.endswith(".down.sql"):
            copied_versions.add(version)
    assert copied_versions == {f"{version:04d}" for version in range(1, 43)}

    previous_directory = os.environ.get("METAHUB_MIGRATIONS_DIR")
    # fixture 退出时无条件恢复进程原环境，避免模块级快照污染同进程中的当前迁移验证。
    os.environ["METAHUB_MIGRATIONS_DIR"] = str(snapshot_directory)
    try:
        yield
    finally:
        if previous_directory is None:
            os.environ.pop("METAHUB_MIGRATIONS_DIR", None)
        else:
            os.environ["METAHUB_MIGRATIONS_DIR"] = previous_directory


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离数据库路径转换为项目约定的 SQLite URL。

    参数：当前测试独占的临时文件。返回：迁移入口可解析的 URL。
    关键约束：路径必须由 ``tmp_path`` 提供，不能回落到共享开发数据库。
    """

    return f"sqlite:///{path.as_posix()}"


def _tenant_context(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> dict[str, str]:
    """读取同租户 admin、dev Environment 和 Virtual Key 的安全 ID。

    参数：数据库连接和可信 tenant_id。返回：迁移夹具所需的 ID 字典。
    关键约束：每个子查询都显式携带 tenant_id，不让测试自身掩盖横向引用问题。
    """

    row = connection.execute(
        """
        SELECT actor.user_id, environment.environment_id, key.virtual_key_id
        FROM users actor
        JOIN environments environment ON environment.tenant_id=actor.tenant_id
        JOIN virtual_keys key ON key.tenant_id=actor.tenant_id
        WHERE actor.tenant_id=? AND actor.role='admin'
          AND environment.code='dev'
        ORDER BY actor.user_id, key.virtual_key_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _bind_project_application(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    suffix: str,
) -> tuple[str, str]:
    """创建同租户 Project/Application，并把夹具 Key 绑定到完整归属链。

    参数：连接、tenant、可信上下文和唯一后缀。返回：Project 与 Application ID。
    关键约束：Application 和 Key 只绑定当前 tenant 的父对象与 dev Environment。
    """

    project_id = f"prj_prd052_{suffix}"
    application_id = f"app_prd052_{suffix}"
    now = "2026-07-27T02:00:00Z"
    connection.execute(
        """
        INSERT INTO projects(
          project_id, tenant_id, project_code, name, status,
          created_by, updated_by, created_at, updated_at
        ) VALUES(?, ?, ?, 'PRD052 项目', 'active', ?, ?, ?, ?)
        """,
        (
            project_id,
            tenant_id,
            f"prd052-{suffix}",
            context["user_id"],
            context["user_id"],
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id, tenant_id, project_id, application_code, name,
          status, created_by, updated_by, created_at, updated_at
        ) VALUES(?, ?, ?, ?, 'PRD052 应用', 'active', ?, ?, ?, ?)
        """,
        (
            application_id,
            tenant_id,
            project_id,
            f"prd052-app-{suffix}",
            context["user_id"],
            context["user_id"],
            now,
            now,
        ),
    )
    connection.execute(
        """
        UPDATE virtual_keys
        SET project_id=?, application_id=?, updated_at=?
        WHERE tenant_id=? AND virtual_key_id=? AND environment_id=?
        """,
        (
            project_id,
            application_id,
            now,
            tenant_id,
            context["virtual_key_id"],
            context["environment_id"],
        ),
    )
    assert connection.total_changes >= 3
    return project_id, application_id


def _create_second_tenant(
    connection: sqlite3.Connection,
    suffix: str,
) -> tuple[str, dict[str, str], str, str]:
    """创建第二租户及其 admin、Project、Application 和 Key 归属图。

    参数：数据库连接和唯一后缀。返回：tenant、上下文、Project、Application。
    关键约束：该图只用于证明跨租户 ID 即使真实存在也会被 PRD-052 拒绝。
    """

    tenant_id = f"tenant_prd052_{suffix}"
    user_id = f"usr_prd052_{suffix}"
    key_id = f"vk_prd052_{suffix}"
    now = "2026-07-27T02:10:00Z"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, ?, 'PRD052 隔离租户', 'internal_only', 'active', ?, ?)
        """,
        (tenant_id, f"prd052-{suffix}", now, now),
    )
    environment_id = str(
        connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (tenant_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(?, ?, ?, 'PRD052 Admin', 'test-hash-only',
                 'admin', 'active', ?, ?)
        """,
        (user_id, tenant_id, f"admin-{suffix}@example.invalid", now, now),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, created_at, updated_at
        ) VALUES(?, ?, 'PRD052 Key', ?, ?, 'active', 'dev', ?, ?, ?)
        """,
        (
            key_id,
            tenant_id,
            f"mhv_prd052_{suffix}",
            f"hash-prd052-{suffix}",
            environment_id,
            now,
            now,
        ),
    )
    context = {
        "user_id": user_id,
        "environment_id": environment_id,
        "virtual_key_id": key_id,
    }
    project_id, application_id = _bind_project_application(
        connection,
        tenant_id,
        context,
        suffix,
    )
    # 返回完整隔离图，调用方无需通过不带 tenant_id 的查询重新拼接归属。
    return tenant_id, context, project_id, application_id


def _insert_rule(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    suffix: str,
    *,
    scope_type: str,
    scope_id: str,
) -> str:
    """插入一条同租户预算规则并返回 Rule ID。

    参数：连接、owner 上下文、后缀与 scope。返回：新规则 ID。
    关键约束：tenant/environment/actor 均来自可信夹具，攻击用例只替换目标 scope。
    """

    rule_id = f"br_prd052_{suffix}"
    now = "2026-07-27T02:20:00Z"
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id, tenant_id, environment_id, scope_type, scope_id,
          name, daily_budget_cny, warning_threshold_percent,
          exhausted_threshold_percent, exhausted_action, status,
          created_by, updated_by, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, 'PRD052 预算', 100, 80, 100, 'block',
                 'active', ?, ?, ?, ?)
        """,
        (
            rule_id,
            tenant_id,
            context["environment_id"],
            scope_type,
            scope_id,
            context["user_id"],
            context["user_id"],
            now,
            now,
        ),
    )
    return rule_id


def _insert_legacy_child(
    connection: sqlite3.Connection,
    kind: str,
    tenant_id: str,
    context: dict[str, str],
    rule_id: str,
    scope_id: str,
    suffix: str,
) -> tuple[str, str, str]:
    """为四张既有预算子表插入一条记录并返回表、主键列和值。

    参数：表类型、owner、父 Rule、scope 和唯一后缀。返回：更新断言所需定位信息。
    关键约束：SQL 分支是测试内固定白名单，调用方数据不会参与表名拼接。
    """

    now = "2026-07-27T02:30:00Z"
    common = (
        tenant_id,
        context["environment_id"],
        rule_id,
        scope_id,
    )
    if kind == "alert":
        record_id = f"bae_prd052_{suffix}"
        connection.execute(
            """
            INSERT INTO budget_alert_events(
              budget_alert_event_id, tenant_id, environment_id, budget_rule_id,
              scope_type, scope_id, budget_date, threshold_percent, event_type,
              current_cost_cny, projected_cost_cny, budget_cny, action_taken,
              request_id, trace_id, created_at
            ) VALUES(?, ?, ?, ?, 'tenant', ?, '2026-07-27', 100,
                     'budget.exhausted', 100, 101, 100, 'block', ?, ?, ?)
            """,
            (record_id, *common, f"req_{suffix}", f"trc_{suffix}", now),
        )
        return "budget_alert_events", "budget_alert_event_id", record_id
    if kind == "evaluation":
        record_id = f"be_prd052_{suffix}"
        connection.execute(
            """
            INSERT INTO budget_evaluations(
              budget_evaluation_id, tenant_id, environment_id, request_id,
              trace_id, virtual_key_id, budget_rule_id, scope_type, scope_id,
              budget_date, spent_before_cny, estimated_request_cost_cny,
              projected_cost_cny, budget_cny, threshold_percent, budget_status,
              action_taken, reason_code, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'tenant', ?, '2026-07-27',
                     99, 2, 101, 100, 100, 'exhausted', 'block',
                     'budget.scope_exceeded', ?)
            """,
            (
                record_id,
                tenant_id,
                context["environment_id"],
                f"req_{suffix}",
                f"trc_{suffix}",
                context["virtual_key_id"],
                rule_id,
                scope_id,
                now,
            ),
        )
        return "budget_evaluations", "budget_evaluation_id", record_id
    if kind == "block":
        record_id = f"bb_prd052_{suffix}"
        connection.execute(
            """
            INSERT INTO budget_blocks(
              budget_block_id, tenant_id, environment_id, budget_rule_id,
              scope_type, scope_id, budget_date, status, blocked_cost_cny,
              budget_cny, blocked_request_id, blocked_trace_id, blocked_at,
              created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'tenant', ?, '2026-07-27', 'active', 101,
                     100, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                *common,
                f"req_{suffix}",
                f"trc_{suffix}",
                now,
                now,
                now,
            ),
        )
        return "budget_blocks", "budget_block_id", record_id
    if kind == "reservation":
        record_id = f"bres_prd052_{suffix}"
        connection.execute(
            """
            INSERT INTO budget_reservations(
              budget_reservation_id, tenant_id, environment_id, budget_rule_id,
              scope_type, scope_id, budget_date, request_id, trace_id,
              reserved_cost_cny, created_at
            ) VALUES(?, ?, ?, ?, 'tenant', ?, '2026-07-27', ?, ?, 1, ?)
            """,
            (record_id, *common, f"req_{suffix}", f"trc_{suffix}", now),
        )
        return "budget_reservations", "budget_reservation_id", record_id
    # kind 只允许来自测试内部白名单；新增表类型必须显式补充结构化 INSERT 分支。
    raise AssertionError(f"未支持的固定预算子表类型：{kind}")


def _insert_scope_event(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    rule_id: str,
    suffix: str,
    *,
    budget_date: str = "2026-07-27",
    scope_id: str | None = None,
    posted: int | float = 90_000_000,
    reserved: int = 5_000_000,
    estimated: int = 5_000_000,
    projected: int | float = 100_000_000,
) -> str:
    """插入 canonical ``budget.scope_exceeded`` 事实并返回内部事件 ID。

    参数：owner、父 Rule、ID 后缀、业务日、scope 和四类金额。返回：事件主键。
    关键约束：ID 只使用固定小写十六进制；默认金额严格满足整数微元求和关系。
    """

    hex_char = suffix[0]
    event_id = f"bse_{hex_char * 32}"
    connection.execute(
        """
        INSERT INTO budget_scope_events(
          budget_scope_event_id, event_id, tenant_id, environment_id,
          budget_rule_id, budget_scope_id, scope_type, scope_id, budget_date,
          action_taken, posted_cost_micros, reserved_cost_micros,
          estimated_request_cost_micros, projected_cost_micros, budget_micros,
          request_id, trace_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'tenant', ?, ?, 'block', ?, ?, ?, ?,
                 100000000, ?, ?, '2026-07-27T02:40:00Z')
        """,
        (
            event_id,
            f"bsevt_{hex_char * 32}",
            tenant_id,
            context["environment_id"],
            rule_id,
            f"bsc_{hex_char * 32}",
            scope_id or tenant_id,
            budget_date,
            posted,
            reserved,
            estimated,
            projected,
            f"req_prd052_{suffix}",
            f"trc_prd052_{suffix}",
        ),
    )
    return event_id


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    suffix: str,
    event_types_json: str,
) -> str:
    """插入无真实网络凭据的 Webhook Endpoint 并返回 ID。

    参数：owner、上下文、唯一后缀和事件数组。返回：Endpoint ID。
    安全：URL 使用 ``example.invalid``，密文与指纹均为测试占位值。
    """

    endpoint_id = f"wh_prd052_{suffix}"
    now = "2026-07-27T02:50:00Z"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status,
          created_by, updated_by, created_at, updated_at
        ) VALUES(?, ?, 'PRD052', 'https://example.invalid/webhook', ?,
                 'encrypted-test-only', 'whsec_test', ?, 'active', ?, ?, ?, ?)
        """,
        (
            endpoint_id,
            tenant_id,
            event_types_json,
            f"fingerprint-{suffix}",
            context["user_id"],
            context["user_id"],
            now,
            now,
        ),
    )
    return endpoint_id


def test_prd052_upgrade_preserves_legacy_budget_facts_and_roundtrips(
    tmp_path: Path,
) -> None:
    """0041→0042 保留 Application 与四类旧预算事实，空 scope event 时可逆。"""

    database_url = _sqlite_url(tmp_path / "prd052-upgrade.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0042"

    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)
    _, application_id = _bind_project_application(
        connection,
        seed.tenant_id,
        context,
        "upgrade",
    )
    tenant_rule = _insert_rule(
        connection,
        seed.tenant_id,
        context,
        "upgrade_tenant",
        scope_type="tenant",
        scope_id=seed.tenant_id,
    )
    application_rule = _insert_rule(
        connection,
        seed.tenant_id,
        context,
        "upgrade_application",
        scope_type="application",
        scope_id=application_id,
    )
    for kind in ("alert", "evaluation", "block", "reservation"):
        _insert_legacy_child(
            connection,
            kind,
            seed.tenant_id,
            context,
            tenant_rule,
            seed.tenant_id,
            f"upgrade_{kind}",
        )
    connection.commit()
    connection.close()

    assert apply_migrations(database_url).applied == ["0042"]
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM budget_rules WHERE budget_rule_id IN (?, ?)",
        (tenant_rule, application_rule),
    ).fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM budget_alert_events").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM budget_evaluations").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM budget_blocks").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 1

    index_rows = connection.execute(
        """
        SELECT name, sql FROM sqlite_master
        WHERE type='index' AND name LIKE 'idx_finops_budget_%_prd052'
        ORDER BY name
        """
    ).fetchall()
    assert len(index_rows) == 4
    for row in index_rows:
        normalized_sql = " ".join(str(row["sql"]).split())
        assert "entry_type='usage_charge'" in normalized_sql
        assert "direction='debit'" in normalized_sql
        assert "status='posted'" in normalized_sql
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    assert rollback_last_migration(database_url).rolled_back == "0042"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT scope_type FROM budget_rules WHERE budget_rule_id=?",
        (application_rule,),
    ).fetchone()[0] == "application"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='finops_ledger_entries'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='budget_scope_events'"
    ).fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    assert apply_migrations(database_url).applied == ["0042"]
    connection = connect_database(database_url)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd052_rule_and_legacy_children_reject_owner_scope_attacks(
    tmp_path: Path,
) -> None:
    """权限流程：跨租户 Rule 与四张子表的伪造 scope INSERT/UPDATE 均拒绝。"""

    database_url = _sqlite_url(tmp_path / "prd052-owner.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)
    other_tenant, other, other_project, _ = _create_second_tenant(connection, "owner")

    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_owner_scope_mismatch"):
        _insert_rule(
            connection,
            seed.tenant_id,
            context,
            "cross_project",
            scope_type="project",
            scope_id=other_project,
        )
    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_owner_scope_mismatch"):
        _insert_rule(
            connection,
            seed.tenant_id,
            context,
            "cross_key",
            scope_type="virtual_key",
            scope_id=other["virtual_key_id"],
        )
    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_owner_scope_mismatch"):
        _insert_rule(
            connection,
            seed.tenant_id,
            context,
            "bad_tenant_scope",
            scope_type="tenant",
            scope_id=other_tenant,
        )

    rule_id = _insert_rule(
        connection,
        seed.tenant_id,
        context,
        "child_parent",
        scope_type="tenant",
        scope_id=seed.tenant_id,
    )
    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_owner_scope_mismatch"):
        connection.execute(
            "UPDATE budget_rules SET environment_id=? WHERE budget_rule_id=?",
            (other["environment_id"], rule_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_owner_scope_mismatch"):
        connection.execute(
            "UPDATE budget_rules SET updated_by=? WHERE budget_rule_id=?",
            (other["user_id"], rule_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_identity_immutable"):
        connection.execute(
            "UPDATE budget_rules SET budget_rule_id='br_prd052_rewritten' WHERE budget_rule_id=?",
            (rule_id,),
        )
    expected_errors = {
        "alert": "budget_alert_event_owner_scope_mismatch",
        "evaluation": "budget_evaluation_owner_scope_mismatch",
        "block": "budget_block_owner_scope_mismatch",
        "reservation": "budget_reservation_owner_scope_mismatch",
    }
    for kind, expected_error in expected_errors.items():
        with pytest.raises(sqlite3.IntegrityError, match=expected_error):
            _insert_legacy_child(
                connection,
                kind,
                seed.tenant_id,
                context,
                rule_id,
                other_tenant,
                f"invalid_{kind}",
            )
        table, primary_key, record_id = _insert_legacy_child(
            connection,
            kind,
            seed.tenant_id,
            context,
            rule_id,
            seed.tenant_id,
            f"valid_{kind}",
        )
        with pytest.raises(sqlite3.IntegrityError, match=expected_error):
            connection.execute(
                f"UPDATE {table} SET scope_id=? WHERE {primary_key}=?",
                (other_tenant, record_id),
            )

    with pytest.raises(sqlite3.IntegrityError, match="budget_rule_owner_scope_mismatch"):
        connection.execute(
            "UPDATE budget_rules SET scope_id=? WHERE budget_rule_id=?",
            (other_tenant, rule_id),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd052_scope_event_enforces_amount_dedupe_owner_and_immutability(
    tmp_path: Path,
) -> None:
    """事件流程：整数求和、同规则业务日去重、父 scope 和 append-only 约束生效。"""

    database_url = _sqlite_url(tmp_path / "prd052-event.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)
    rule_id = _insert_rule(
        connection,
        seed.tenant_id,
        context,
        "event_parent",
        scope_type="tenant",
        scope_id=seed.tenant_id,
    )
    event_id = _insert_scope_event(
        connection,
        seed.tenant_id,
        context,
        rule_id,
        "a_event",
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_scope_event(
            connection,
            seed.tenant_id,
            context,
            rule_id,
            "b_dedupe",
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_scope_event(
            connection,
            seed.tenant_id,
            context,
            rule_id,
            "c_real",
            budget_date="2026-07-28",
            posted=90_000_000.5,
            projected=100_000_000.5,
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_scope_event(
            connection,
            seed.tenant_id,
            context,
            rule_id,
            "d_sum",
            budget_date="2026-07-29",
            projected=100_000_001,
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="budget_scope_event_owner_scope_mismatch",
    ):
        _insert_scope_event(
            connection,
            seed.tenant_id,
            context,
            rule_id,
            "e_owner",
            budget_date="2026-07-30",
            scope_id="tenant_prd052_wrong",
        )
    with pytest.raises(sqlite3.IntegrityError, match="budget_scope_event_immutable"):
        connection.execute(
            "UPDATE budget_scope_events SET action_taken='allow' WHERE budget_scope_event_id=?",
            (event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="budget_scope_event_immutable"):
        connection.execute(
            "DELETE FROM budget_scope_events WHERE budget_scope_event_id=?",
            (event_id,),
        )
    row = connection.execute(
        """
        SELECT posted_cost_micros, reserved_cost_micros,
               estimated_request_cost_micros, projected_cost_micros
        FROM budget_scope_events WHERE budget_scope_event_id=?
        """,
        (event_id,),
    ).fetchone()
    assert tuple(row) == (90_000_000, 5_000_000, 5_000_000, 100_000_000)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


@pytest.mark.parametrize("fact_kind", ["scope_event", "subscription", "delivery"])
def test_prd052_down_guard_rejects_external_facts_atomically(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """异常流程：event、订阅或 delivery 任一存在时，0042 down 在 DDL 前原子拒绝。"""

    database_url = _sqlite_url(tmp_path / f"prd052-down-{fact_kind}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)

    if fact_kind == "scope_event":
        rule_id = _insert_rule(
            connection,
            seed.tenant_id,
            context,
            "down_parent",
            scope_type="tenant",
            scope_id=seed.tenant_id,
        )
        _insert_scope_event(
            connection,
            seed.tenant_id,
            context,
            rule_id,
            "f_down",
        )
    else:
        event_types = '["budget.scope_exceeded"]' if fact_kind == "subscription" else "[]"
        endpoint_id = _insert_webhook_endpoint(
            connection,
            seed.tenant_id,
            context,
            fact_kind,
            event_types,
        )
        if fact_kind == "delivery":
            now = "2026-07-27T03:00:00Z"
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id,
                  event_type, event_timestamp, payload_json, status,
                  attempt_count, max_attempts, created_at, updated_at
                ) VALUES('whd_prd052_down', ?, ?, ?, 'budget.scope_exceeded',
                         ?, '{}', 'pending', 0, 3, ?, ?)
                """,
                (
                    seed.tenant_id,
                    endpoint_id,
                    f"bsevt_{'9' * 32}",
                    now,
                    now,
                    now,
                ),
            )
    connection.commit()
    object_count_before = connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE name='budget_scope_events'
           OR name LIKE 'idx_finops_budget_%_prd052'
           OR name LIKE 'trg_budget_%_prd052_%'
        """
    ).fetchone()[0]

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd052_down_business_facts_present" in exc.value.message
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE name='budget_scope_events'
           OR name LIKE 'idx_finops_budget_%_prd052'
           OR name LIKE 'trg_budget_%_prd052_%'
        """
    ).fetchone()[0] == object_count_before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0042'"
    ).fetchone()[0] == "applied"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_prd052_invalid_0041_owner_data_aborts_before_persistent_ddl(
    tmp_path: Path,
) -> None:
    """升级异常：0041 已有跨租户 scope 时，0042 在创建持久对象前拒绝。"""

    database_url = _sqlite_url(tmp_path / "prd052-invalid-history.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0042"
    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)
    _, _, other_project, _ = _create_second_tenant(connection, "history")
    # 0041 只有单列外键，允许这条语义跨租户记录；0042 的历史 preflight 必须识别它。
    _insert_rule(
        connection,
        seed.tenant_id,
        context,
        "invalid_history",
        scope_type="project",
        scope_id=other_project,
    )
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as exc:
        apply_migrations(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd052_historical_owner_mismatch" in exc.value.message
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='budget_scope_events'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'idx_finops_budget_%_prd052'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0042'"
    ).fetchone()[0] == "failed"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
