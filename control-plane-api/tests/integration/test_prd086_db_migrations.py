"""PRD-086 迁移专项验证客户健康快照、租约、RBAC、往返恢复与原子回滚守卫。"""

from __future__ import annotations

import hashlib
import json
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

# 迁移目录、revision 20/21 哈希和冻结窗口共同定义 0079 往返验收基线。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
CATALOG_HASH_21 = "03776958f56b3ac3d3d0355d29b4ae2ea3520be09607295f4fa15a549bbfe28b"
ROLE_HASH_21 = "c737744b0a9f8aa577c49f40bd7cdf2457f69ed93bb0dedf8d01365a5855aef0"
CATALOG_HASH_20 = "5f77eaca2da3b37026d9891ee7fab0f4aa121fbd2ac775bcc2b21d740aacca14"
ROLE_HASH_20 = "4602777ad3937aa0351a4b9949826756904a4ee3b485d65984cdbecdce127711"
ADMIN_HASH_21 = "49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5"
OPS_HASH_21 = "1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783"
ADMIN_HASH_20 = "e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272"
OPS_HASH_20 = "c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7"
EVALUATED = "2030-01-01T00:00:06Z"
WINDOW_END = "2030-01-01T00:00:00Z"
WINDOW_START = "2029-12-25T00:00:00Z"
COMPARISON_START = "2029-12-18T00:00:00Z"


def _url(path: Path) -> str:
    """把临时数据库路径转换为迁移器使用的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _clone(source: Path, destination: Path) -> str:
    """复制测试数据库模板并返回目标 URL，保证每个用例独占状态。"""

    shutil.copy2(source, destination)
    return _url(destination)


def _healthy(connection: sqlite3.Connection) -> None:
    """断言数据库完整性检查成功且外键违规数量为零。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _hashes(connection: sqlite3.Connection) -> tuple[str, str]:
    """按规范顺序重算权限目录和角色模板 SHA-256 摘要。"""

    catalog = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT permission_code,category,name,description,risk_level,catalog_revision
            FROM rbac_permission_templates ORDER BY permission_code
            """
        )
    ]
    roles = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT role_code,permission_code,default_granted
            FROM rbac_role_permission_templates ORDER BY role_code,permission_code
            """
        )
    ]
    return (
        hashlib.sha256("\x1e".join(catalog).encode()).hexdigest(),
        hashlib.sha256("\x1e".join(roles).encode()).hexdigest(),
    )


@pytest.fixture(scope="module")
def empty_0079(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """从空 SQLite 实际迁移至 79/0079，作为无种子往返模板。"""

    root = tmp_path_factory.mktemp("prd086-empty")
    database = root / "empty.sqlite3"
    result = apply_migrations(_url(database))
    assert result.applied == [f"{version:04d}" for version in range(1, 80)]
    return database


@pytest.fixture(scope="module")
def seeded_0079(empty_0079: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """复制 0079 空库并执行种子，作为租户与 RBAC 专项模板。"""

    root = tmp_path_factory.mktemp("prd086-seeded")
    database = root / "seeded.sqlite3"
    shutil.copy2(empty_0079, database)
    apply_seed_data(_url(database))
    return database


@pytest.fixture(scope="module")
def seeded_0078(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """只复制并应用至 0078 的迁移，用于验证真实 revision 20 升级。"""

    root = tmp_path_factory.mktemp("prd086-revision20")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 78:
            shutil.copy2(source, migrations / source.name)
    database = root / "seeded.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0078"
    apply_seed_data(_url(database))
    return database


def _canonical_hash(values: list[object]) -> str:
    """以规范 ASCII JSON 对冻结字段序列生成稳定 SHA-256 摘要。"""

    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _health(score: int) -> str:
    """按冻结基点阈值映射四档健康等级，供测试独立复算。"""

    if score >= 8000:
        return "healthy"
    if score >= 6000:
        return "watch"
    if score >= 3000:
        return "at_risk"
    return "critical"


def _score_values(
    tenant_id: str,
    *,
    current: int = 10,
    previous: int = 20,
    successful: int = 8,
    unresolved: int = 0,
    critical: int = 0,
    budgets: int = 1,
    eligible: int = 2,
    active: int = 1,
    activation: int = 4,
) -> dict[str, object]:
    """独立计算一行完整评分字段，用于验证触发器、哈希和不可变约束。"""

    failed = current - successful
    usage = (
        0
        if current == 0
        else 10000
        if previous == 0
        else min(10000, current * 10000 // previous)
    )
    reliability = 5000 if current == 0 else (successful * 10000 + current // 2) // current
    cost = 0 if critical else 4000 if unresolved else 10000
    budget = 10000 if budgets else 3000
    team = 0 if eligible == 0 else (active * 10000 + eligible // 2) // eligible
    scores = (usage, reliability, cost, budget, team)
    weights = (2500, 2500, 1500, 1000, 1500)
    products = [score * weight for score, weight in zip(scores, weights, strict=True)]
    total = (sum(products) + 4500) // 9000
    bases = [product // 9000 for product in products]
    extras = total - sum(bases)
    priority = sorted(range(5), key=lambda index: (-(products[index] % 9000), index))
    contributions = bases[:]
    for index in priority[:extras]:
        contributions[index] += 1
    usage_declining = int(current == 0 or (previous > 0 and current * 10000 // previous < 5000))
    error_rate = 10000 - reliability
    high_error = int(current >= 5 and error_rate >= 500)
    cost_open = int(unresolved > 0)
    budget_missing = int(budgets == 0)
    team_inactive = int(active == 0 or (eligible >= 2 and team < 5000))
    # 测试端独立复现风险下限，确保迁移约束不依赖生产服务实现。
    if total < 3000 or critical:
        risk = "critical"
    # 零用量或高失败率至少为 at_risk，不能被其他信号高分抵消。
    elif total < 6000 or current == 0 or (current >= 5 and error_rate >= 1000):
        risk = "at_risk"
    # 普通异常和中等失败率至少为 watch，保持失败与成本事实可追踪。
    elif total < 8000 or unresolved or (current >= 5 and error_rate >= 500):
        risk = "watch"
    else:
        risk = "healthy"
    source_values: list[object] = [
        "customer-health-source-v1",
        tenant_id,
        "customer-health-v1",
        WINDOW_START,
        WINDOW_END,
        COMPARISON_START,
        WINDOW_START,
        current,
        previous,
        successful,
        failed,
        unresolved,
        critical,
        budgets,
        eligible,
        active,
        "unavailable",
        activation,
    ]
    source_hash = _canonical_hash(source_values)
    score_digest = _canonical_hash(
        ["customer-health-score-v1", tenant_id, "customer-health-v1", WINDOW_END, source_hash]
    )[:32]
    operation_digest = _canonical_hash(
        ["customer-health-operation-v1", tenant_id, WINDOW_END, source_hash]
    )[:32]
    projection_values: list[object] = [
        "customer-health-projection-v1",
        "customer-health-v1",
        usage,
        contributions[0],
        "no_samples" if current == 0 and previous == 0 else "available",
        _health(usage),
        reliability,
        contributions[1],
        "no_samples" if current == 0 else "available",
        "unknown" if current == 0 else _health(reliability),
        cost,
        contributions[2],
        "available",
        _health(cost),
        budget,
        contributions[3],
        "available",
        _health(budget),
        team,
        contributions[4],
        "no_samples" if eligible == 0 else "available",
        _health(team),
        None,
        0,
        "unavailable",
        "unknown",
        9000,
        total,
        risk,
        usage_declining,
        high_error,
        cost_open,
        budget_missing,
        team_inactive,
        "ticket_health",
    ]
    return {
        "customer_health_score_id": f"chs_{score_digest}",
        "tenant_id": tenant_id,
        "score_version": "customer-health-v1",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "comparison_start": COMPARISON_START,
        "comparison_end": WINDOW_START,
        "source_fingerprint": source_hash,
        "health_projection_sha256": _canonical_hash(projection_values),
        "current_request_count": current,
        "previous_request_count": previous,
        "successful_request_count": successful,
        "failed_request_count": failed,
        "unresolved_cost_anomaly_count": unresolved,
        "critical_cost_anomaly_count": critical,
        "active_budget_rule_count": budgets,
        "eligible_user_count": eligible,
        "recently_active_user_count": active,
        "ticket_source_status": "unavailable",
        "activation_stage_count": activation,
        "usage_trend_score_bps": usage,
        "usage_trend_contribution_bps": contributions[0],
        "reliability_score_bps": reliability,
        "reliability_contribution_bps": contributions[1],
        "cost_anomaly_health_score_bps": cost,
        "cost_anomaly_health_contribution_bps": contributions[2],
        "budget_readiness_score_bps": budget,
        "budget_readiness_contribution_bps": contributions[3],
        "team_activity_score_bps": team,
        "team_activity_contribution_bps": contributions[4],
        "ticket_health_score_bps": None,
        "ticket_health_contribution_bps": 0,
        "effective_weight_bps": 9000,
        "score_bps": total,
        "risk_level": risk,
        "usage_declining": usage_declining,
        "high_error_rate": high_error,
        "cost_anomaly_open": cost_open,
        "budget_missing": budget_missing,
        "team_inactive": team_inactive,
        "operation_request_id": f"req_chs_{operation_digest}",
        "operation_trace_id": f"trc_chs_{operation_digest}",
        "evaluated_at": EVALUATED,
        "created_at": EVALUATED,
        "updated_at": EVALUATED,
    }


def _tenant_id(connection: sqlite3.Connection) -> str:
    """读取模板中的首个租户标识，后续 SQL 全部显式携带该 tenant_id。"""

    row = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
    ).fetchone()
    return str(row[0])


def _claim_scheduler(connection: sqlite3.Connection, tenant_id: str) -> None:
    """以冻结 owner 和令牌领取指定租户 Scheduler，用于测试写入守卫。"""

    connection.execute(
        """
        UPDATE customer_health_scheduler_tenants
        SET status='leased',generation=generation+1,lease_owner_id='prd086-test-worker',
            lease_token=?,lease_expires_at='2030-01-01T00:02:00Z',last_error_code=NULL,
            updated_at='2030-01-01T00:00:05Z'
        WHERE tenant_id=? AND status='pending'
        """,
        ("a" * 64, tenant_id),
    )
    assert connection.execute("SELECT changes()").fetchone()[0] == 1


def _insert_score(connection: sqlite3.Connection, values: dict[str, object]) -> None:
    """将独立复算字段插入 append-only 评分表，触发数据库合同校验。"""

    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO customer_health_scores({columns}) VALUES({placeholders})",
        tuple(values.values()),
    )


def _insert_outbox(connection: sqlite3.Connection, values: dict[str, object]) -> None:
    """为指定租户评分插入稳定 source outbox 行，保持事件 ID 可复算。"""

    score_id = str(values["customer_health_score_id"])
    digest = hashlib.sha256(score_id.encode("ascii")).hexdigest()[:32]
    connection.execute(
        """
        INSERT INTO customer_health_score_outbox(
          customer_health_score_outbox_id,tenant_id,customer_health_score_id,
          webhook_event_id,status,attempt_count,generation,lease_owner_id,lease_token,
          lease_expires_at,next_attempt_at,subscription_snapshot_json,snapshot_frozen_at,
          last_error_code,delivered_at,created_at,updated_at)
        VALUES(?,?,?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,?,?)
        """,
        (
            f"chso_{digest}",
            values["tenant_id"],
            score_id,
            f"evt_chs_{digest}",
            values["created_at"],
            values["created_at"],
            values["created_at"],
        ),
    )


def test_fresh_repeat_pristine_down_reup_and_schema_contract(
    empty_0079: Path, tmp_path: Path
) -> None:
    """验证空库 fresh/repeat/down/re-up、schema、哈希、完整性与外键合同。"""

    database_url = _clone(empty_0079, tmp_path / "roundtrip.sqlite3")
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_21, ROLE_HASH_21)
        assert tuple(
            connection.execute(
                """
                SELECT permission_code,category,name,description,risk_level,catalog_revision
                FROM rbac_permission_templates WHERE permission_code='customer_health.read'
                """
            ).fetchone()
        ) == (
            "customer_health.read",
            "analytics",
            "读取客户健康评分",
            "读取当前租户确定性客户健康评分、聚合信号与可解释建议。",
            "read",
            21,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_permission_templates"
        ).fetchone()[0] == 90
        assert connection.execute(
            "SELECT COUNT(*) FROM rbac_role_permission_templates"
        ).fetchone()[0] == 236
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
              'customer_health_scores','customer_health_score_outbox',
              'customer_health_scheduler_tenants')
            """
        ).fetchone()[0] == 3
        _healthy(connection)
    finally:
        connection.close()
    repeated = apply_migrations(database_url)
    assert repeated.applied == [] and repeated.skipped[-1] == "0079"
    down = rollback_last_migration(database_url, expected_version="0079")
    assert down.rolled_back == "0079"
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_20, ROLE_HASH_20)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE name LIKE '%_prd086' OR name LIKE 'customer_health%'
            """
        ).fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()
    assert apply_migrations(database_url).applied == ["0079"]
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_21, ROLE_HASH_21)
        _healthy(connection)
    finally:
        connection.close()


def test_existing_new_and_custom_tenant_rbac_projection(
    seeded_0078: Path, tmp_path: Path
) -> None:
    """验证既有、新建和自定义租户在 revision 21 的精确 RBAC 投影。"""

    default_url = _clone(seeded_0078, tmp_path / "default.sqlite3")
    apply_migrations(default_url)
    connection = connect_database(default_url)
    try:
        tenant_id = _tenant_id(connection)
        grants = list(
            connection.execute(
                """
                SELECT role.code,role.matrix_sha256 FROM roles role
                JOIN role_permissions relation ON relation.tenant_id=role.tenant_id
                  AND relation.role_id=role.role_id
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE role.tenant_id=? AND permission.code='customer_health.read'
                ORDER BY role.code
                """,
                (tenant_id,),
            )
        )
        assert [tuple(row) for row in grants] == [
            ("admin", ADMIN_HASH_21),
            ("ops_admin", OPS_HASH_21),
        ]
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd086_new','prd086-new','PRD086 New','internal_only','active',?,?)
            """,
            (EVALUATED, EVALUATED),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd086_new'"
        ).fetchone()[0] == 90
        assert connection.execute(
            """
            SELECT COUNT(*) FROM customer_health_scheduler_tenants
            WHERE tenant_id='tenant_prd086_new'
            """
        ).fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()

    custom_url = _clone(seeded_0078, tmp_path / "custom.sqlite3")
    connection = connect_database(custom_url)
    try:
        tenant_id = _tenant_id(connection)
        role_id = connection.execute(
            "SELECT role_id FROM roles WHERE tenant_id=? AND code='ops_admin'", (tenant_id,)
        ).fetchone()[0]
        permission_id = connection.execute(
            """
            SELECT permission_id FROM permissions
            WHERE tenant_id=? AND code='product_analytics.read'
            """,
            (tenant_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
            (tenant_id, role_id, permission_id),
        )
        codes = [
            row[0]
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
                """,
                (tenant_id, role_id),
            )
        ]
        custom_hash = hashlib.sha256(
            json.dumps(codes, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        connection.execute(
            """
            UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (custom_hash, EVALUATED, tenant_id, role_id),
        )
        connection.commit()
    finally:
        connection.close()
    apply_migrations(custom_url)
    connection = connect_database(custom_url)
    try:
        assert connection.execute(
            "SELECT matrix_sha256 FROM roles WHERE tenant_id=? AND role_id=?",
            (tenant_id, role_id),
        ).fetchone()[0] == custom_hash
        assert connection.execute(
            """
            SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
              AND permission.code='customer_health.read'
            """,
            (tenant_id, role_id),
        ).fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()


def test_score_formula_hash_immutability_and_changed_only_outbox(
    seeded_0079: Path, tmp_path: Path
) -> None:
    """验证整数评分、双哈希、append-only 与 changed-only outbox 约束。"""

    database_url = _clone(seeded_0079, tmp_path / "scores.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id = _tenant_id(connection)
        _claim_scheduler(connection, tenant_id)
        first = _score_values(tenant_id)
        _insert_score(connection, first)
        _insert_outbox(connection, first)
        assert connection.execute(
            """
            SELECT score_bps,risk_level,usage_trend_contribution_bps,
                   reliability_contribution_bps,cost_anomaly_health_contribution_bps,
                   budget_readiness_contribution_bps,team_activity_contribution_bps
            FROM customer_health_scores
            """
        ).fetchone()[0] == first["score_bps"]
        assert sum(
            int(first[name])
            for name in (
                "usage_trend_contribution_bps",
                "reliability_contribution_bps",
                "cost_anomaly_health_contribution_bps",
                "budget_readiness_contribution_bps",
                "team_activity_contribution_bps",
            )
        ) == first["score_bps"]

        same_projection = _score_values(tenant_id, activation=5)
        assert same_projection["source_fingerprint"] != first["source_fingerprint"]
        assert same_projection["health_projection_sha256"] == first["health_projection_sha256"]
        _insert_score(connection, same_projection)
        with pytest.raises(sqlite3.IntegrityError, match="customer_health_outbox_owner_mismatch"):
            _insert_outbox(connection, same_projection)

        changed = _score_values(tenant_id, current=10, previous=20, successful=9, activation=5)
        assert changed["health_projection_sha256"] != first["health_projection_sha256"]
        _insert_score(connection, changed)
        _insert_outbox(connection, changed)
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_health_score_outbox"
        ).fetchone()[0] == 2

        safe_max = 922_337_203_685_477
        maximum = _score_values(
            tenant_id,
            current=safe_max,
            previous=safe_max,
            successful=safe_max,
            activation=6,
        )
        _insert_score(connection, maximum)
        too_large = _score_values(
            tenant_id,
            current=safe_max + 1,
            previous=safe_max + 1,
            successful=safe_max + 1,
            activation=7,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_score(connection, too_large)
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_health_scores WHERE customer_health_score_id=?",
            (too_large["customer_health_score_id"],),
        ).fetchone()[0] == 0

        corrupt = dict(_score_values(tenant_id, current=11, previous=20, successful=9))
        corrupt["health_projection_sha256"] = "f" * 64
        with pytest.raises(sqlite3.IntegrityError, match="customer_health_score_owner_mismatch"):
            _insert_score(connection, corrupt)
        with pytest.raises(sqlite3.IntegrityError, match="customer_health_score_immutable"):
            connection.execute(
                """
                UPDATE customer_health_scores SET score_bps=score_bps
                WHERE customer_health_score_id=?
                """,
                (first["customer_health_score_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="customer_health_score_immutable"):
            connection.execute(
                "DELETE FROM customer_health_scores WHERE customer_health_score_id=?",
                (first["customer_health_score_id"],),
            )
        _healthy(connection)
    finally:
        connection.close()


def test_scheduler_and_outbox_same_second_renew_takeover_and_cas(
    seeded_0079: Path, tmp_path: Path
) -> None:
    """验证 Scheduler 与 outbox 同秒续租、过期接管和完整 CAS 防护。"""

    database_url = _clone(seeded_0079, tmp_path / "leases.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id = _tenant_id(connection)
        _claim_scheduler(connection, tenant_id)
        connection.execute(
            """
            UPDATE customer_health_scheduler_tenants
            SET generation=generation+1,lease_token=?,lease_expires_at='2030-01-01T00:03:00Z',
                updated_at='2030-01-01T00:00:05Z'
            WHERE tenant_id=?
            """,
            ("b" * 64, tenant_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        values = _score_values(tenant_id)
        _insert_score(connection, values)
        _insert_outbox(connection, values)
        score_id = values["customer_health_score_id"]
        connection.execute(
            """
            UPDATE customer_health_score_outbox
            SET status='leased',attempt_count=1,generation=1,lease_owner_id='worker-a',
                lease_token=?,lease_expires_at='2030-01-01T00:02:00Z',next_attempt_at=NULL,
                subscription_snapshot_json='[]',snapshot_frozen_at='2030-01-01T00:00:06Z',
                last_error_code=NULL,delivered_at=NULL,updated_at='2030-01-01T00:00:06Z'
            WHERE tenant_id=? AND customer_health_score_id=?
            """,
            ("c" * 64, tenant_id, score_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        connection.execute(
            """
            UPDATE customer_health_score_outbox
            SET generation=2,lease_token=?,lease_expires_at='2030-01-01T00:03:00Z',
                updated_at='2030-01-01T00:00:06Z'
            WHERE tenant_id=? AND customer_health_score_id=?
            """,
            ("d" * 64, tenant_id, score_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        connection.execute(
            """
            UPDATE customer_health_score_outbox
            SET generation=3,lease_owner_id='worker-b',lease_token=?,
                lease_expires_at='2030-01-01T00:06:00Z',updated_at='2030-01-01T00:04:00Z'
            WHERE tenant_id=? AND customer_health_score_id=?
            """,
            ("e" * 64, tenant_id, score_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        with pytest.raises(
            sqlite3.IntegrityError,
            match="customer_health_outbox_transition_invalid",
        ):
            connection.execute(
                """
                UPDATE customer_health_score_outbox
                SET generation=4,lease_token=?,lease_expires_at='2030-01-01T00:07:00Z',
                    updated_at='2030-01-01T00:04:00Z'
                WHERE tenant_id=? AND customer_health_score_id=?
                """,
                ("e" * 64, tenant_id, score_id),
            )
        _healthy(connection)
    finally:
        connection.close()


def _insert_webhook_dependency(connection: sqlite3.Connection, fact_kind: str) -> None:
    """插入指定类型的 webhook 依赖事实，用于证明 0079 down 原子阻断。"""

    tenant_id = _tenant_id(connection)
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY user_id LIMIT 1", (tenant_id,)
    ).fetchone()[0]
    endpoint_id = f"whe_prd086_{fact_kind}"
    event_types = (
        '["customer.health_changed"]'
        if fact_kind == "subscription"
        else '["user.invited"]'
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
          secret_version,created_by,updated_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'active',5000,3,1,?,?,?,?)
        """,
        (
            endpoint_id,
            tenant_id,
            f"PRD-086 {fact_kind}",
            "https://example.invalid/prd086",
            event_types,
            "enc:v1:test-fixture",
            "test",
            "f" * 64,
            user_id,
            user_id,
            EVALUATED,
            EVALUATED,
        ),
    )
    if fact_kind == "subscription":
        return
    delivery_id = "whd_" + hashlib.sha256(fact_kind.encode()).hexdigest()[:32]
    failed = fact_kind == "dead_letter"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
          event_timestamp,payload_json,status,attempt_count,max_attempts,http_status,
          duration_ms,error_code,error_summary,next_retry_at,delivery_kind,
          replayed_from_delivery_id,replay_idempotency_key,replayed_by,replayed_at,
          secret_version,created_at,updated_at)
        VALUES(?,?,?,?,? ,?,'{}',?,?,3,?,?,?, ?,NULL,'original',
                 NULL,NULL,NULL,NULL,1,?,?)
        """,
        (
            delivery_id,
            tenant_id,
            endpoint_id,
            f"evt_prd086_{fact_kind}",
            "customer.health_changed",
            EVALUATED,
            "failed" if failed else "pending",
            1 if failed else 0,
            503 if failed else None,
            12 if failed else None,
            "webhook.delivery_unavailable" if failed else None,
            "test-only" if failed else None,
            EVALUATED,
            EVALUATED,
        ),
    )
    if fact_kind == "attempt":
        connection.execute(
            """
            INSERT INTO webhook_delivery_attempts(
              webhook_delivery_attempt_id,tenant_id,webhook_delivery_id,
              webhook_endpoint_id,attempt_no,status,http_status,duration_ms,
              error_code,error_summary,created_at)
            VALUES(?,?,?,?,1,'failed',503,12,'webhook.delivery_unavailable','test-only',?)
            """,
            ("wha_" + "8" * 32, tenant_id, delivery_id, endpoint_id, EVALUATED),
        )


@pytest.mark.parametrize(
    "fact_kind",
    ("scheduler", "score_outbox", "subscription", "delivery", "attempt", "dead_letter", "rbac"),
)
def test_down_guards_are_atomic(
    fact_kind: str, seeded_0079: Path, tmp_path: Path
) -> None:
    """逐类验证依赖存在时 down 完全回滚，0079 与 schema 均保持不变。"""

    database_url = _clone(seeded_0079, tmp_path / f"down-{fact_kind}.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id = _tenant_id(connection)
        if fact_kind == "scheduler":
            _claim_scheduler(connection, tenant_id)
        elif fact_kind == "score_outbox":
            _claim_scheduler(connection, tenant_id)
            values = _score_values(tenant_id)
            _insert_score(connection, values)
            _insert_outbox(connection, values)
        elif fact_kind == "rbac":
            connection.execute(
                """
                UPDATE roles SET permission_revision=permission_revision+1,
                  matrix_sha256=?,updated_at=?
                WHERE tenant_id=? AND code='ops_admin'
                """,
                ("f" * 64, EVALUATED, tenant_id),
            )
        else:
            _insert_webhook_dependency(connection, fact_kind)
        connection.commit()
        before = tuple(
            connection.execute(
                "SELECT name,type,sql FROM sqlite_schema WHERE name LIKE '%prd086%' ORDER BY name"
            )
        )
    finally:
        connection.close()
    with pytest.raises(DbError, match="prd086_down_guard_failed"):
        rollback_last_migration(database_url, expected_version="0079")
    connection = connect_database(database_url)
    try:
        assert connection.execute(
            """
            SELECT version FROM schema_migrations
            WHERE status='applied' ORDER BY version DESC LIMIT 1
            """
        ).fetchone()[0] == "0079"
        assert tuple(
            connection.execute(
                "SELECT name,type,sql FROM sqlite_schema WHERE name LIKE '%prd086%' ORDER BY name"
            )
        ) == before
        _healthy(connection)
    finally:
        connection.close()


def test_down_guard_lists_every_destructive_dependency_before_first_ddl() -> None:
    """静态确认 down 守卫在首个持久 DDL 前列出全部破坏性依赖。"""

    source = (MIGRATIONS_DIR / "0079_customer_health_score.down.sql").read_text(
        encoding="utf-8"
    )
    guard = source.index("prd086_down_guard_failed")
    first_persistent_ddl = source.index(
        "DROP TRIGGER trg_tenants_customer_health_scheduler_insert_prd086"
    )
    assert guard < first_persistent_ddl
    for marker in (
        "customer_health_scores",
        "customer_health_score_outbox",
        "customer_health_scheduler_tenants",
        "customer.health_changed",
        "webhook_endpoints",
        "webhook_deliveries",
        "webhook_delivery_attempts",
        "webhook_dead_letter_events",
        "customer_health.read",
    ):
        assert marker in source[guard:first_persistent_ddl]
