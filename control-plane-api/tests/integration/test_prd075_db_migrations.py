"""PRD-075 迁移测试：验证 0068 正反迁移、租户外键、并发守卫和业务回滚阻断。

PRD-075 migration coverage for model A/B testing and quality evaluation.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.rbac import permission_matrix_sha256

# 该测试常量组只使用合成凭据、固定时钟和预期哈希，不得进入日志或验收证据。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
EXPECTED_0067_MIGRATION_SHA256 = (
    "6e02752f1a86fe28cfb8e30ecf534bc88ed064981ab9cbb2710cd732f752f76b"
)
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 69)]
TEST_ORIGIN = datetime.now(UTC).replace(microsecond=0)
NOW = TEST_ORIGIN.isoformat().replace("+00:00", "Z")
LEASE = (TEST_ORIGIN + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
EXECUTION_LEASE = (TEST_ORIGIN + timedelta(minutes=5)).isoformat().replace(
    "+00:00", "Z"
)
DEADLINE = (TEST_ORIGIN + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
SETTLEMENT_DEADLINE = (TEST_ORIGIN + timedelta(hours=2)).isoformat().replace(
    "+00:00", "Z"
)
ACTIVE_FINALIZATION_LEASE = (TEST_ORIGIN + timedelta(minutes=5)).isoformat().replace(
    "+00:00", "Z"
)

EXPERIMENT_TABLE_COLUMNS = {
    "model_experiments": 51,
    "gateway_request_scope_claims": 9,
    "experiment_assignments": 19,
    "experiment_request_executions": 14,
    "experiment_request_outcomes": 21,
    "experiment_usage_settlements": 18,
    "experiment_feedback_attributions": 11,
    "experiment_results": 40,
    "experiment_events": 12,
    "experiment_event_outbox": 16,
    "experiment_scheduler_tenants": 8,
}
REVISION_11_HASHES = {
    "admin": "33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148",
    "developer": "71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd",
    "ops_admin": "0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a",
}
REVISION_10_HASHES = {
    "admin": "ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262",
    "developer": "89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30",
    "ops_admin": "72a396186bd78f79cbbeadab59ba322d2088640f37787377bb74c034486b65c6",
}
EXPERIMENT_PERMISSION_TEMPLATES = (
    (
        "experiment.read",
        "experiment",
        "读取模型实验",
        "读取当前租户模型 A/B 实验的安全配置、进度和权威结果。",
        "read",
        11,
    ),
    (
        "experiment.write",
        "experiment",
        "创建模型实验",
        "创建影响当前租户在线 Alias 流量的不可变模型 A/B 实验。",
        "critical",
        11,
    ),
)
EXPERIMENT_ROLE_TEMPLATE_ROWS = tuple(
    (role_code, permission_code, 1)
    for role_code in ("admin", "developer", "ops_admin")
    for permission_code in ("experiment.read", "experiment.write")
)
# 该 B 编码来自 0068 down；单独放进 A 集合时必须被整组 exact-set 守卫拒绝。
DOWN_CANONICAL_PERMISSION_UPDATE_TRIGGER_SQL = (
    "CREATE TRIGGER trg_permissions_update_prd059 "
    "BEFORE UPDATE ON permissions FOR EACH ROW\n"
    "BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END"
)


def test_prd075_0067_trigger_source_is_checksum_locked() -> None:
    """锁定 A canonical 的 0067 原始迁移来源，防止基线与期望值同步漂移。"""
    migration = MIGRATIONS_DIR / "0067_batch_scheduler_worker.sql"
    assert hashlib.sha256(migration.read_bytes()).hexdigest() == (
        EXPECTED_0067_MIGRATION_SHA256
    )


def _url(path: Path) -> str:
    """构造独立 SQLite 测试环境和迁移序列，避免复用或污染其他测试数据库。"""
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """构造独立 SQLite 测试环境和迁移序列，避免复用或污染其他测试数据库。"""
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd075_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    root = tmp_path_factory.mktemp("prd075-migration-templates")
    migrations_0067 = _migration_subset(root / "migrations-0067", 67)
    migrations_0068 = _migration_subset(root / "migrations-0068", 68)
    database_0067 = root / "template-0067.sqlite3"
    database_url = _url(database_0067)
    assert apply_migrations(database_url, migrations_dir=migrations_0067).applied[-1] == "0067"
    apply_seed_data(database_url)
    return {
        "database_0067": database_0067,
        "migrations_0068": migrations_0068,
    }


def _clone_and_upgrade(
    templates: dict[str, Path],
    destination: Path,
) -> tuple[str, sqlite3.Connection]:
    """构造独立 SQLite 测试环境和迁移序列，避免复用或污染其他测试数据库。"""
    shutil.copy2(templates["database_0067"], destination)
    database_url = _url(destination)
    assert apply_migrations(
        database_url,
        migrations_dir=templates["migrations_0068"],
    ).applied == ["0068"]
    return database_url, connect_database(database_url)


def _applied_head(connection: sqlite3.Connection) -> str:
    """读取并断言迁移后的结构、外键或 RBAC 事实，不放宽任何生产约束。"""
    row = connection.execute(
        """
        SELECT version FROM schema_migrations
        WHERE status='applied' ORDER BY version DESC LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """读取并断言迁移后的结构、外键或 RBAC 事实，不放宽任何生产约束。"""
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _foreign_key_groups(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """读取并断言迁移后的结构、外键或 RBAC 事实，不放宽任何生产约束。"""
    grouped: dict[int, tuple[str, list[str], list[str]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
        foreign_key_id = int(row[0])
        target = str(row[2])
        source_column = str(row[3])
        target_column = str(row[4])
        current = grouped.setdefault(foreign_key_id, (target, [], []))
        current[1].append(source_column)
        current[2].append(target_column)
    return {
        (target, tuple(source_columns), tuple(target_columns))
        for target, source_columns, target_columns in grouped.values()
    }


def _role_grants(
    connection: sqlite3.Connection,
    tenant_id: str,
    role_code: str,
) -> set[str]:
    """读取并断言迁移后的结构、外键或 RBAC 事实，不放宽任何生产约束。"""
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code
            FROM roles role
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE role.tenant_id=? AND role.code=?
            """,
            (tenant_id, role_code),
        )
    }


def _assert_revision(
    connection: sqlite3.Connection,
    *,
    revision: int,
    permission_count: int,
    relation_count: int,
    hashes: dict[str, str],
    tenant_id: str | None = None,
) -> str:
    """读取并断言迁移后的结构、外键或 RBAC 事实，不放宽任何生产约束。"""
    assert (
        connection.execute("SELECT COUNT(*) FROM rbac_permission_templates").fetchone()[0]
        == permission_count
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=?",
        (revision,),
    ).fetchone()[0] == permission_count
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_role_permission_templates"
    ).fetchone()[0] == relation_count
    # 迁移夹具或数据库事实不满足预期时立即失败，避免用弱断言掩盖约束漂移。
    if tenant_id is None:
        row = connection.execute(
            "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
        ).fetchone()
        assert row is not None
        tenant_id = str(row[0])
    assert connection.execute(
        "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=?",
        (tenant_id, revision),
    ).fetchone()[0] == permission_count
    for role_code, expected_hash in hashes.items():
        assert connection.execute(
            "SELECT matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()[0] == expected_hash
    # 迁移夹具或数据库事实不满足预期时立即失败，避免用弱断言掩盖约束漂移。
    return tenant_id


def _insert_tenant(connection: sqlite3.Connection, marker: str) -> str:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    tenant_id = f"tenant_prd075_{marker}"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (tenant_id, f"prd075-{marker}", f"PRD075 {marker}", NOW, NOW),
    )
    connection.commit()
    # 迁移夹具或数据库事实不满足预期时立即失败，避免用弱断言掩盖约束漂移。
    return tenant_id


def _resource_graph(connection: sqlite3.Connection, marker: str = "a") -> dict[str, str]:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    identity = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,environment.environment_id,environment.code,
               provider.provider_id,provider.provider_code,model.model_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        JOIN environments environment ON environment.tenant_id=tenant.tenant_id
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=tenant.tenant_id
          AND model.provider_id=provider.provider_id
        WHERE tenant.slug='default' AND user.role='admin' AND user.status='active'
          AND environment.code='dev' AND environment.status='active'
          AND provider.status='active' AND model.status='active'
        ORDER BY user.user_id,provider.provider_id,model.model_id LIMIT 1
        """
    ).fetchone()
    assert identity is not None
    (
        tenant_id,
        user_id,
        environment_id,
        environment_code,
        provider_id,
        provider_code,
        model_a_id,
    ) = map(str, identity)
    graph = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": environment_id,
        "environment_code": environment_code,
        "provider_id": provider_id,
        "provider_code": provider_code,
        "model_a_id": model_a_id,
        "model_b_id": f"model_prd075_{marker}_b",
        "project_id": f"project_prd075_{marker}",
        "application_id": f"application_prd075_{marker}",
        "model_alias_id": f"alias_prd075_{marker}",
        "alias": f"experiment-alias-{marker}",
        "virtual_key_id": f"vkey_prd075_{marker}",
        "experiment_id": "exp_" + marker * 32,
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD075 project',NULL,?,'active',?,?,?,?)
        """,
        (
            graph["project_id"], tenant_id, f"prd075-{marker}", user_id,
            user_id, user_id, NOW, NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id,tenant_id,project_id,application_code,name,description,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,'PRD075 application',NULL,'active',?,?,?,?)
        """,
        (
            graph["application_id"], tenant_id, graph["project_id"],
            f"prd075-{marker}", user_id, user_id, NOW, NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,created_at,updated_at,context_window
        ) VALUES(?,?,?,?,?,'{"chat":true,"stream":true}','active',?,?,8192)
        """,
        (
            graph["model_b_id"], tenant_id, provider_id,
            f"prd075-model-{marker}", f"prd075-upstream-{marker}", NOW, NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,?,?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
        """,
        (
            graph["model_alias_id"], tenant_id, graph["alias"], environment_code,
            model_a_id, user_id, NOW, NOW, environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_prd075',?,'active',?,?,?,NULL,NULL,?,NULL,NULL,?,?,?)
        """,
        (
            graph["virtual_key_id"], tenant_id, f"PRD075 key {marker}",
            marker * 64, NOW, NOW, environment_code, user_id,
            graph["project_id"], graph["application_id"], environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_experiments(
          experiment_id,tenant_id,name,project_id,environment_id,model_alias_id,
          model_a_id,model_a_provider_id,model_a_provider_code,
          model_a_route_config_fingerprint,model_b_id,model_b_provider_id,
          model_b_provider_code,model_b_route_config_fingerprint,target_sample_count,
          traffic_sample_bps,model_a_allocation_bps,model_b_allocation_bps,
          model_a_target_count,model_b_target_count,sampling_duration_seconds,
          feedback_collection_enabled,feedback_window_seconds,
          allocation_algorithm_version,sampling_key_fingerprint,status,
          assigned_count,model_a_assigned_count,model_b_assigned_count,facts_version,
          finalization_generation,created_by,idempotency_key_hash,request_fingerprint,
          operation_request_id,operation_trace_id,execution_max_seconds,
          settlement_grace_seconds,started_at,sampling_deadline_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,2,10000,5000,5000,1,1,3600,
                 1,3600,1,?,'active',0,0,0,0,0,?,?,?,?,?,3600,3600,?,?,?,?)
        """,
        (
            graph["experiment_id"], tenant_id, "PRD075 comparison",
            graph["project_id"], environment_id, graph["model_alias_id"],
            model_a_id, provider_id, provider_code, "a" * 64,
            graph["model_b_id"], provider_id, provider_code, "b" * 64,
            "c" * 64, user_id, "d" * 64, "e" * 64,
            f"req_prd075_create_{marker}", f"trace_prd075_create_{marker}",
            NOW, DEADLINE, NOW, NOW,
        ),
    )
    connection.commit()
    return graph


def _insert_claim(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    marker: str,
) -> tuple[str, str]:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    request_id = f"req_prd075_{marker}"
    trace_id = f"trace_prd075_{marker}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (graph["tenant_id"], request_id, trace_id, graph["virtual_key_id"], NOW),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_scope_claims(
          tenant_id,request_id,trace_id,virtual_key_id,project_id,application_id,
          environment_id,environment_code,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            graph["tenant_id"], request_id, trace_id, graph["virtual_key_id"],
            graph["project_id"], graph["application_id"], graph["environment_id"],
            graph["environment_code"], NOW,
        ),
    )
    connection.commit()
    return request_id, trace_id


def _insert_assignment(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    marker: str,
    *,
    include_execution: bool,
    include_settlement: bool,
) -> dict[str, str]:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    request_id, trace_id = _insert_claim(connection, graph, marker)
    assignment_id = "exas_" + marker * 32
    execution_id = "exrn_" + marker * 32
    settlement_id = "exst_" + marker * 32
    decision_id = "rdec_prd075_" + marker
    summary = json.dumps(
        {
            "algorithm_version": 1,
            "assignment_id": assignment_id,
            "assignment_reason": "hmac_preferred",
            "experiment_id": graph["experiment_id"],
            "variant": "a",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id,tenant_id,request_id,trace_id,virtual_key_id,
          model_alias_id,requested_model,environment,selected_model_id,
          selected_internal_model_id,provider_id,provider_code,decision_status,
          decision_reason,candidate_summary,error_code,created_at,environment_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,
          'selected','experiment.variant_a',?,NULL,?,?)
        """,
        (
            decision_id, graph["tenant_id"], request_id, trace_id,
            graph["virtual_key_id"], graph["model_alias_id"], graph["alias"],
            graph["environment_code"], graph["model_a_id"], graph["model_a_id"],
            graph["provider_id"], graph["provider_code"], summary, NOW,
            graph["environment_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO experiment_assignments(
          assignment_id,tenant_id,experiment_id,request_id,trace_id,virtual_key_id,
          routing_decision_id,execution_id,usage_settlement_id,assignment_sequence,
          variant,model_id,provider_id,provider_code,sample_bucket,variant_bucket,
          assignment_reason,algorithm_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,1,'a',?,?,?,0,0,'hmac_preferred',1,?)
        """,
        (
            assignment_id, graph["tenant_id"], graph["experiment_id"], request_id,
            trace_id, graph["virtual_key_id"], decision_id, execution_id, settlement_id,
            graph["model_a_id"], graph["provider_id"], graph["provider_code"], NOW,
        ),
    )
    if include_execution:
        connection.execute(
            """
            INSERT INTO experiment_request_executions(
              execution_id,tenant_id,experiment_id,assignment_id,request_id,status,
              generation,lease_owner_id,lease_token,lease_expires_at,
              absolute_deadline_at,send_fence_at,created_at,updated_at
            ) VALUES(
              ?,?,?,?,?,'assigned',1,'gateway-prd075',lower(hex(randomblob(16))),
              ?,?,NULL,?,?
            )
            """,
            (
                execution_id, graph["tenant_id"], graph["experiment_id"],
                assignment_id, request_id, EXECUTION_LEASE, DEADLINE, NOW, NOW,
            ),
        )
    if include_settlement:
        connection.execute(
            """
            INSERT INTO experiment_usage_settlements(
              usage_settlement_id,tenant_id,experiment_id,assignment_id,request_id,
              variant,model_id,provider_id,status,normalization_status,
              settlement_reason,settlement_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,'a',?,?,'pending','pending','awaiting_usage',1,?,?)
            """,
            (
                settlement_id, graph["tenant_id"], graph["experiment_id"],
                assignment_id, request_id, graph["model_a_id"], graph["provider_id"],
                NOW, NOW,
            ),
        )
    return {
        "request_id": request_id,
        "trace_id": trace_id,
        "assignment_id": assignment_id,
        "execution_id": execution_id,
        "settlement_id": settlement_id,
    }


def _experiment_audit_summary(graph: dict[str, str]) -> dict[str, object]:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    return {
        "experiment_id": graph["experiment_id"],
        "project_id": graph["project_id"],
        "environment_id": graph["environment_id"],
        "model_alias_id": graph["model_alias_id"],
        "model_a_id": graph["model_a_id"],
        "model_b_id": graph["model_b_id"],
        "target_sample_count": 2,
        "traffic_sample_bps": 10000,
        "model_a_allocation_bps": 5000,
        "sampling_duration_seconds": 3600,
        "feedback_collection_enabled": True,
        "feedback_window_seconds": 3600,
    }


def _insert_experiment_audit(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    marker: str,
) -> str:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    actor = connection.execute(
        "SELECT email,role FROM users WHERE tenant_id=? AND user_id=?",
        (graph["tenant_id"], graph["user_id"]),
    ).fetchone()
    assert actor is not None
    after_summary = _experiment_audit_summary(graph)
    audit_log_id = "aud_" + marker * 32
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'experiment.created','experiment',?,NULL,?,?,?,NULL,NULL,?,?,?)
        """,
        (
            audit_log_id,
            graph["tenant_id"],
            graph["user_id"],
            str(actor[0]),
            str(actor[1]),
            graph["experiment_id"],
            "{}",
            json.dumps(after_summary, separators=(",", ":"), sort_keys=True),
            json.dumps(sorted(after_summary), separators=(",", ":")),
            f"req_prd075_create_{marker}",
            f"trace_prd075_create_{marker}",
            NOW,
        ),
    )
    return audit_log_id


def _activate_test_scheduler(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> None:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    connection.execute(
        """
        INSERT INTO experiment_scheduler_tenants(
          tenant_id,next_run_at,lease_generation,created_at,updated_at
        ) VALUES(?,?,0,?,?)
        """,
        (tenant_id, NOW, NOW, NOW),
    )
    connection.execute(
        """
        UPDATE experiment_scheduler_tenants
        SET lease_generation=1,lease_token='prd075-scheduler-token',
            lease_owner_id='control-prd075',lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND lease_generation=0
        """,
        (ACTIVE_FINALIZATION_LEASE, NOW, tenant_id),
    )


def _complete_empty_experiment(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    marker: str,
) -> str:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    connection.execute(
        """
        UPDATE model_experiments
        SET status='collecting_feedback',sampling_ended_at=?,feedback_deadline_at=?,
            settlement_deadline_at=?,feedback_fence_closed_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=?
        """,
        (
            NOW,
            DEADLINE,
            SETTLEMENT_DEADLINE,
            NOW,
            NOW,
            graph["tenant_id"],
            graph["experiment_id"],
        ),
    )
    facts_version = int(connection.execute(
        "SELECT facts_version FROM model_experiments WHERE tenant_id=? AND experiment_id=?",
        (graph["tenant_id"], graph["experiment_id"]),
    ).fetchone()[0])
    assert facts_version == 1
    _activate_test_scheduler(connection, graph["tenant_id"])
    connection.execute(
        """
        UPDATE model_experiments
        SET finalization_generation=1,finalization_owner_id='control-prd075',
            finalization_lease_token='prd075-finalization-token',
            finalization_lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=?
        """,
        (
            ACTIVE_FINALIZATION_LEASE,
            NOW,
            graph["tenant_id"],
            graph["experiment_id"],
        ),
    )
    for variant, model_key, fingerprint in (
        ("a", "model_a_id", "a" * 64),
        ("b", "model_b_id", "b" * 64),
    ):
        connection.execute(
            """
            INSERT INTO experiment_results(
              experiment_result_id,tenant_id,experiment_id,variant,model_id,provider_id,
              provider_code,route_config_fingerprint,assigned_count,gateway_success_count,
              gateway_failure_count,unresolved_count,exposed_count,not_exposed_count,
              exposure_unknown_count,quality_success_count,quality_failure_count,
              gateway_success_rate_bps,quality_success_rate_bps,latency_sample_count,
              total_latency_ms,average_latency_micros,p95_latency_ms,
              settlement_recorded_count,settlement_not_exposed_count,
              settlement_unavailable_count,gross_normalized_cny_micros,cost_known_count,
              cost_unknown_count,average_cost_cny_micros,feedback_enabled,feedback_count,
              rating_sum,average_rating_milli,accepted_count,adoption_rate_bps,
              statistics_version,facts_version,facts_through_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,0,0,0,0,0,0,0,0,0,NULL,NULL,0,0,NULL,NULL,
                     0,0,0,0,0,0,NULL,1,0,0,0,0,0,1,?,?,?)
            """,
            (
                "exrs_" + marker * 31 + variant,
                graph["tenant_id"],
                graph["experiment_id"],
                variant,
                graph[model_key],
                graph["provider_id"],
                graph["provider_code"],
                fingerprint,
                facts_version,
                NOW,
                NOW,
            ),
        )
    payload = {
        "schema_version": 1,
        "experiment_id": graph["experiment_id"],
        "project_id": graph["project_id"],
        "environment_id": graph["environment_id"],
        "model_alias_id": graph["model_alias_id"],
        "model_a_id": graph["model_a_id"],
        "model_b_id": graph["model_b_id"],
        "status": "completed",
        "completion_reason": "sampling_deadline_reached",
        "assigned_count": 0,
        "model_a_assigned_count": 0,
        "model_b_assigned_count": 0,
        "gateway_success_count": 0,
        "gateway_failure_count": 0,
        "unresolved_count": 0,
        "cost_unknown_count": 0,
        "feedback_count": 0,
        "facts_version": facts_version,
        "facts_through_at": NOW,
    }
    event_id = "exev_" + marker * 32
    connection.execute(
        """
        INSERT INTO experiment_events(
          experiment_event_id,tenant_id,experiment_id,event_type,actor_type,actor_id,
          object_type,object_id,source_request_id,source_trace_id,payload_json,occurred_at
        ) VALUES(?,?,?,'experiment.completed','user',?,'experiment',?,?,?,?,?)
        """,
        (
            event_id,
            graph["tenant_id"],
            graph["experiment_id"],
            graph["user_id"],
            graph["experiment_id"],
            f"req_prd075_create_{marker}",
            f"trace_prd075_create_{marker}",
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            NOW,
        ),
    )
    connection.execute(
        """
        UPDATE model_experiments
        SET status='completed',completion_reason='sampling_deadline_reached',
            completed_at=?,updated_at=?,finalization_owner_id=NULL,
            finalization_lease_token=NULL,finalization_lease_expires_at=NULL
        WHERE tenant_id=? AND experiment_id=?
        """,
        (NOW, NOW, graph["tenant_id"], graph["experiment_id"]),
    )
    connection.commit()
    return event_id


def _insert_single_failure_result(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    **overrides: object,
) -> None:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    facts_version = int(connection.execute(
        "SELECT facts_version FROM model_experiments WHERE experiment_id=?",
        (graph["experiment_id"],),
    ).fetchone()[0])
    values: dict[str, object] = {
        "experiment_result_id": "exrs_" + "2" * 32,
        "tenant_id": graph["tenant_id"],
        "experiment_id": graph["experiment_id"],
        "variant": "a",
        "model_id": graph["model_a_id"],
        "provider_id": graph["provider_id"],
        "provider_code": graph["provider_code"],
        "route_config_fingerprint": "a" * 64,
        "assigned_count": 1,
        "gateway_success_count": 0,
        "gateway_failure_count": 1,
        "unresolved_count": 0,
        "exposed_count": 0,
        "not_exposed_count": 1,
        "exposure_unknown_count": 0,
        "quality_success_count": 0,
        "quality_failure_count": 0,
        "gateway_success_rate_bps": 0,
        "quality_success_rate_bps": None,
        "latency_sample_count": 0,
        "total_latency_ms": 0,
        "average_latency_micros": None,
        "p95_latency_ms": None,
        "settlement_recorded_count": 0,
        "settlement_not_exposed_count": 1,
        "settlement_unavailable_count": 0,
        "gross_normalized_cny_micros": 0,
        "cost_known_count": 0,
        "cost_unknown_count": 0,
        "average_cost_cny_micros": None,
        "feedback_enabled": 1,
        "feedback_count": 0,
        "rating_sum": 0,
        "average_rating_milli": 0,
        "accepted_count": 0,
        "adoption_rate_bps": 0,
        "statistics_version": 1,
        "facts_version": facts_version,
        "facts_through_at": NOW,
        "created_at": NOW,
    }
    values.update(overrides)
    connection.execute(
        """
        INSERT INTO experiment_results(
          experiment_result_id,tenant_id,experiment_id,variant,model_id,provider_id,
          provider_code,route_config_fingerprint,assigned_count,gateway_success_count,
          gateway_failure_count,unresolved_count,exposed_count,not_exposed_count,
          exposure_unknown_count,quality_success_count,quality_failure_count,
          gateway_success_rate_bps,quality_success_rate_bps,latency_sample_count,
          total_latency_ms,average_latency_micros,p95_latency_ms,
          settlement_recorded_count,settlement_not_exposed_count,
          settlement_unavailable_count,gross_normalized_cny_micros,cost_known_count,
          cost_unknown_count,average_cost_cny_micros,feedback_enabled,feedback_count,
          rating_sum,average_rating_milli,accepted_count,adoption_rate_bps,
          statistics_version,facts_version,facts_through_at,created_at
        ) VALUES(
          :experiment_result_id,:tenant_id,:experiment_id,:variant,:model_id,:provider_id,
          :provider_code,:route_config_fingerprint,:assigned_count,:gateway_success_count,
          :gateway_failure_count,:unresolved_count,:exposed_count,:not_exposed_count,
          :exposure_unknown_count,:quality_success_count,:quality_failure_count,
          :gateway_success_rate_bps,:quality_success_rate_bps,:latency_sample_count,
          :total_latency_ms,:average_latency_micros,:p95_latency_ms,
          :settlement_recorded_count,:settlement_not_exposed_count,
          :settlement_unavailable_count,:gross_normalized_cny_micros,:cost_known_count,
          :cost_unknown_count,:average_cost_cny_micros,:feedback_enabled,:feedback_count,
          :rating_sum,:average_rating_milli,:accepted_count,:adoption_rate_bps,
          :statistics_version,:facts_version,:facts_through_at,:created_at
        )
        """,
        values,
    )


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    marker: str,
    event_types: list[str],
) -> str:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    endpoint_id = f"whe_prd075_{marker}"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,printf('https://%s','example.invalid'),?,lower(hex(randomblob(24))),
                 'fixture',lower(hex(randomblob(16))),'active',1000,3,?,?,?,?)
        """,
        (
            endpoint_id,
            tenant_id,
            f"PRD075 {marker}",
            json.dumps(event_types, separators=(",", ":")),
            user_id,
            user_id,
            NOW,
            NOW,
        ),
    )
    return endpoint_id


def _insert_webhook_delivery(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
    marker: str,
    *,
    failed: bool = False,
) -> str:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    delivery_id = "whd_" + marker * 32
    if failed:
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,error_code,
              delivery_kind,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,'experiment.completed',?,'{}','failed',3,3,
                     'webhook.delivery_unavailable','original',1,?,?)
            """,
            (delivery_id, tenant_id, endpoint_id, f"event_prd075_{marker}", NOW, NOW, NOW),
        )
    else:
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
              event_timestamp,payload_json,status,attempt_count,max_attempts,
              delivery_kind,secret_version,created_at,updated_at
            ) VALUES(?,?,?,?,'experiment.completed',?,'{}','pending',0,3,
                     'original',1,?,?)
            """,
            (delivery_id, tenant_id, endpoint_id, f"event_prd075_{marker}", NOW, NOW, NOW),
        )
    return delivery_id


def _insert_role_permission_event(connection: sqlite3.Connection, marker: str) -> str:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    role = connection.execute(
        """
        SELECT role_id,tenant_id,code,name,permission_revision,matrix_sha256
        FROM roles WHERE code='developer' ORDER BY tenant_id LIMIT 1
        """
    ).fetchone()
    assert role is not None
    actor = connection.execute(
        """
        SELECT user_id,email,role FROM users
        WHERE tenant_id=? AND role='admin' AND status='active' ORDER BY user_id LIMIT 1
        """,
        (role[1],),
    ).fetchone()
    assert actor is not None
    before_codes = sorted(_role_grants(connection, str(role[1]), "developer"))
    removed_code = "experiment.write"
    assert removed_code in before_codes
    after_codes = sorted(set(before_codes) - {removed_code})
    after_hash = permission_matrix_sha256(after_codes)
    before_revision = int(role[4])
    after_revision = before_revision + 1
    connection.execute(
        """
        DELETE FROM role_permissions
        WHERE tenant_id=? AND role_id=? AND permission_id=(
          SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?
        )
        """,
        (role[1], role[0], role[1], removed_code),
    )
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (after_revision, after_hash, NOW, role[1], role[0]),
    )
    before_summary = {
        "role_code": role[2],
        "permission_revision": before_revision,
        "matrix_sha256": role[5],
        "permission_count": len(before_codes),
    }
    after_summary = {
        "role_code": role[2],
        "permission_revision": after_revision,
        "matrix_sha256": after_hash,
        "permission_count": len(after_codes),
    }
    audit_log_id = "aud_role_prd075_" + marker
    request_id = f"req_role_prd075_{marker}"
    trace_id = f"trace_role_prd075_{marker}"
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'role.permission_changed','role_permission',?,?,?,?,?,
                 NULL,NULL,?,?,?)
        """,
        (
            audit_log_id,
            role[1],
            actor[0],
            actor[1],
            actor[2],
            role[0],
            role[3],
            json.dumps(before_summary, separators=(",", ":"), sort_keys=True),
            json.dumps(after_summary, separators=(",", ":"), sort_keys=True),
            '["matrix_sha256","permission_count","permission_revision"]',
            request_id,
            trace_id,
            NOW,
        ),
    )
    removed = json.dumps([removed_code], separators=(",", ":"))
    payload = {
        "schema_version": 1,
        "role_id": role[0],
        "role_code": role[2],
        "before_revision": before_revision,
        "after_revision": after_revision,
        "before_sha256": role[5],
        "after_sha256": after_hash,
        "added_permissions": [],
        "removed_permissions": [removed_code],
    }
    event_id = "rpevt_" + marker * 32
    connection.execute(
        """
        INSERT INTO role_permission_events(
          event_id,tenant_id,role_id,actor_user_id,actor_role,request_id,
          before_revision,after_revision,before_sha256,after_sha256,
          added_permissions_json,removed_permissions_json,audit_log_id,trace_id,
          payload_json,delivery_status,last_error_code,created_at,updated_at,dispatched_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'[]',?,?,?,?,
                 'pending',NULL,?,?,NULL)
        """,
        (
            event_id,
            role[1],
            role[0],
            actor[0],
            actor[2],
            request_id,
            before_revision,
            after_revision,
            role[5],
            after_hash,
            removed,
            audit_log_id,
            trace_id,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            NOW,
            NOW,
        ),
    )
    connection.commit()
    return event_id


def _assert_down_blocked(
    database_url: str,
    migrations_dir: Path,
    *,
    expected_guard: str = "prd075_down_guard_ok",
) -> None:
    """读取并断言迁移后的结构、外键或 RBAC 事实，不放宽任何生产约束。"""
    with pytest.raises(DbError) as error:
        rollback_last_migration(database_url, migrations_dir=migrations_dir)
    assert error.value.code == "db.migration_failed"
    assert expected_guard in error.value.message
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0068"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='model_experiments'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=11"
    ).fetchone()[0] == 66
    _assert_healthy(connection)
    connection.close()


def _persistent_schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结主 schema，证明守卫失败没有留下半套 0068 对象。"""
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name
            """
        )
    )


def _rbac_guard_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结 exact-set 守卫覆盖的模板、默认关系和租户权限投影。"""
    statements = (
        """
        SELECT permission_code,category,name,description,risk_level,catalog_revision
        FROM rbac_permission_templates ORDER BY permission_code
        """,
        """
        SELECT role_code,permission_code,default_granted
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
        """,
        """
        SELECT tenant_id,code,category,name,description,risk_level,catalog_revision,status
        FROM permissions ORDER BY tenant_id,code
        """,
    )
    return tuple(tuple(row) for statement in statements for row in connection.execute(statement))


def _permission_template_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """读取完整权限 metadata，避免 revision 断言退化成行数检查。"""
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT permission_code,category,name,description,risk_level,catalog_revision
            FROM rbac_permission_templates ORDER BY permission_code
            """
        )
    )


def _role_template_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    """读取角色默认关系的完整 canonical 三元组。

    ``default_granted`` 不得从快照中省略，否则同 code 关系仍会退化为弱集合断言。
    """
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT role_code,permission_code,default_granted
            FROM rbac_role_permission_templates ORDER BY role_code,permission_code
            """
        )
    )


def _install_same_count_rbac_drift(
    connection: sqlite3.Connection,
    drift_kind: str,
) -> None:
    """绕开不可变 trigger 写入合成漂移，再原样恢复 trigger 形状。"""
    trigger_names = {
        "permission_metadata": ("trg_rbac_permission_templates_update_prd059",),
        "role_relation": (
            "trg_rbac_role_templates_insert_prd059",
            "trg_rbac_role_templates_delete_prd059",
        ),
        "role_default_granted": (
            "trg_tenants_rbac_initialize_prd059",
            "trg_role_permissions_owner_insert_prd059",
            "trg_role_permission_events_owner_insert_prd059",
            "trg_rbac_role_templates_insert_prd059",
            "trg_rbac_role_templates_update_prd059",
            "trg_rbac_role_templates_delete_prd059",
        ),
        "tenant_permission_metadata": ("trg_permissions_update_prd059",),
    }[drift_kind]
    trigger_sql: list[str] = []
    for trigger_name in trigger_names:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()
        assert row is not None and row[0]
        trigger_sql.append(str(row[0]))
        connection.execute(f"DROP TRIGGER {trigger_name}")

    # 模板元数据保持行数和 code 不变，专门验证弱计数守卫无法发现的漂移。
    if drift_kind == "permission_metadata":
        changed = connection.execute(
            """
            UPDATE rbac_permission_templates
            SET description=description || ' [synthetic drift]'
            WHERE permission_code='batch.read'
            """
        ).rowcount
        assert changed == 1
    # 以同数量合法 code 替换 developer 关系，default_granted 仍参与三元组比对。
    elif drift_kind == "role_relation":
        deleted = connection.execute(
            """
            DELETE FROM rbac_role_permission_templates
            WHERE role_code='developer' AND permission_code='batch.read'
            """
        ).rowcount
        assert deleted == 1
        connection.execute(
            """
            INSERT INTO rbac_role_permission_templates(
              role_code,permission_code,default_granted
            ) VALUES('developer','audit.export',1)
            """
        )
    # 放宽合成库的表级 CHECK 后写入 0，证明 exact-set 会独立校验 default_granted。
    elif drift_kind == "role_default_granted":
        connection.executescript(
            """
            CREATE TABLE rbac_role_permission_templates_prd075_drift(
              role_code TEXT NOT NULL CHECK(role_code IN (
                'admin','developer','viewer','finance_admin','security_admin','ops_admin'
              )),
              permission_code TEXT NOT NULL,
              default_granted INTEGER NOT NULL CHECK(default_granted IN (0,1)),
              PRIMARY KEY(role_code,permission_code),
              FOREIGN KEY(permission_code)
                REFERENCES rbac_permission_templates(permission_code)
            );
            INSERT INTO rbac_role_permission_templates_prd075_drift(
              role_code,permission_code,default_granted
            )
            SELECT role_code,permission_code,
              CASE WHEN role_code='developer' AND permission_code='batch.read'
                THEN 0 ELSE default_granted END
            FROM rbac_role_permission_templates;
            DROP TABLE rbac_role_permission_templates;
            ALTER TABLE rbac_role_permission_templates_prd075_drift
              RENAME TO rbac_role_permission_templates;
            """
        )
    else:
        changed = connection.execute(
            """
            UPDATE permissions SET description=description || ' [synthetic drift]'
            WHERE tenant_id=(SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1)
              AND code='batch.read'
            """
        ).rowcount
        assert changed == 1

    for statement in trigger_sql:
        connection.execute(statement)
    connection.commit()


@pytest.mark.parametrize(
    "drift_kind",
    (
        "permission_metadata",
        "role_relation",
        "role_default_granted",
        "tenant_permission_metadata",
    ),
)
def test_prd075_up_rejects_same_count_rbac_drift_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
    drift_kind: str,
) -> None:
    """同计数目录、关系、默认值或租户投影漂移不得被标记为 0068。"""
    database = tmp_path / f"up-rbac-drift-{drift_kind}.sqlite3"
    shutil.copy2(prd075_templates["database_0067"], database)
    database_url = _url(database)
    connection = connect_database(database_url)
    _install_same_count_rbac_drift(connection, drift_kind)
    schema_before = _persistent_schema_snapshot(connection)
    rbac_before = _rbac_guard_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd075_templates["migrations_0068"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd075_up_guard_ok" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='model_experiments'"
    ).fetchone()[0] == 0
    assert _persistent_schema_snapshot(connection) == schema_before
    assert _rbac_guard_snapshot(connection) == rbac_before
    _assert_healthy(connection)
    connection.close()


def _replace_permission_update_trigger_with_noop(
    connection: sqlite3.Connection,
) -> None:
    """保留 trigger 的类型、名称和 owner 表，仅把安全 body 替换为空操作。"""
    connection.execute("DROP TRIGGER trg_permissions_update_prd059")
    connection.execute(
        """
        CREATE TRIGGER trg_permissions_update_prd059
        BEFORE UPDATE ON permissions FOR EACH ROW
        BEGIN SELECT 1; END
        """
    )
    connection.commit()
    assert tuple(
        connection.execute(
            """
            SELECT type,name,tbl_name FROM sqlite_schema
            WHERE name='trg_permissions_update_prd059'
            """
        ).fetchone()
    ) == ("trigger", "trg_permissions_update_prd059", "permissions")


def test_prd075_up_rejects_same_name_noop_rbac_trigger_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """同名同表的空实现 trigger 不得冒充 checksum-locked 0067 RBAC 守卫。"""
    database = tmp_path / "up-rbac-noop-trigger.sqlite3"
    shutil.copy2(prd075_templates["database_0067"], database)
    database_url = _url(database)
    connection = connect_database(database_url)
    _replace_permission_update_trigger_with_noop(connection)
    schema_before = _persistent_schema_snapshot(connection)
    rbac_before = _rbac_guard_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd075_templates["migrations_0068"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd075_up_guard_ok" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd075'"
    ).fetchone()[0] == 0
    assert _persistent_schema_snapshot(connection) == schema_before
    assert _rbac_guard_snapshot(connection) == rbac_before
    _assert_healthy(connection)
    connection.close()


def test_prd075_up_rejects_mixed_trigger_canonical_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """A 基线只混入一条合法 B trigger 时也必须整体失败关闭。"""
    database = tmp_path / "up-rbac-mixed-trigger.sqlite3"
    shutil.copy2(prd075_templates["database_0067"], database)
    database_url = _url(database)
    connection = connect_database(database_url)
    connection.execute("DROP TRIGGER trg_permissions_update_prd059")
    connection.execute(DOWN_CANONICAL_PERMISSION_UPDATE_TRIGGER_SQL)
    connection.commit()
    assert connection.execute(
        """
        SELECT sql FROM sqlite_schema
        WHERE type='trigger' AND name='trg_permissions_update_prd059'
        """
    ).fetchone()[0] == DOWN_CANONICAL_PERMISSION_UPDATE_TRIGGER_SQL
    schema_before = _persistent_schema_snapshot(connection)
    rbac_before = _rbac_guard_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd075_templates["migrations_0068"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd075_up_guard_ok" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd075'"
    ).fetchone()[0] == 0
    assert _persistent_schema_snapshot(connection) == schema_before
    assert _rbac_guard_snapshot(connection) == rbac_before
    _assert_healthy(connection)
    connection.close()


def test_prd075_up_rejects_down_canonical_noop_trigger_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """down 生成的 B canonical 被替换为空实现后也必须原子拒绝 re-up。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "up-rbac-down-noop-trigger.sqlite3",
    )
    connection.close()
    assert rollback_last_migration(
        database_url,
        migrations_dir=prd075_templates["migrations_0068"],
    ).rolled_back == "0068"

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    _replace_permission_update_trigger_with_noop(connection)
    schema_before = _persistent_schema_snapshot(connection)
    rbac_before = _rbac_guard_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as error:
        apply_migrations(
            database_url,
            migrations_dir=prd075_templates["migrations_0068"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd075_up_guard_ok" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd075'"
    ).fetchone()[0] == 0
    assert _persistent_schema_snapshot(connection) == schema_before
    assert _rbac_guard_snapshot(connection) == rbac_before
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    "drift_kind",
    (
        "permission_metadata",
        "role_relation",
        "role_default_granted",
        "tenant_permission_metadata",
    ),
)
def test_prd075_down_rejects_same_count_rbac_drift_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
    drift_kind: str,
) -> None:
    """0068 exact-set 漂移必须阻断 down，并原子保留完整 0068。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / f"down-rbac-drift-{drift_kind}.sqlite3",
    )
    _install_same_count_rbac_drift(connection, drift_kind)
    schema_before = _persistent_schema_snapshot(connection)
    rbac_before = _rbac_guard_snapshot(connection)
    connection.close()

    with pytest.raises(DbError) as error:
        rollback_last_migration(
            database_url,
            migrations_dir=prd075_templates["migrations_0068"],
        )
    assert error.value.code == "db.migration_failed"
    assert "prd075_down_guard_ok" in error.value.message

    connection = connect_database(database_url)
    assert _applied_head(connection) == "0068"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='model_experiments'"
    ).fetchone()[0] == 1
    assert _persistent_schema_snapshot(connection) == schema_before
    assert _rbac_guard_snapshot(connection) == rbac_before
    _assert_healthy(connection)
    connection.close()


def test_prd075_fresh_repeat_exact_schema_integrity_and_rbac(tmp_path: Path) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    migrations = _migration_subset(tmp_path / "migrations-0068", 68)
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url, migrations_dir=migrations).applied == EXPECTED_VERSIONS
    repeat = apply_migrations(database_url, migrations_dir=migrations)
    assert repeat.applied == [] and repeat.skipped == EXPECTED_VERSIONS
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0068"
    actual_tables = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema WHERE type='table'
            AND name IN (
              'model_experiments','gateway_request_scope_claims','experiment_assignments',
              'experiment_request_executions','experiment_request_outcomes',
              'experiment_usage_settlements','experiment_feedback_attributions',
              'experiment_results','experiment_events','experiment_event_outbox',
              'experiment_scheduler_tenants'
            )
            """
        )
    }
    assert actual_tables == set(EXPERIMENT_TABLE_COLUMNS)
    assert {
        table: len(connection.execute(f"PRAGMA table_info({table})").fetchall())
        for table in EXPERIMENT_TABLE_COLUMNS
    } == EXPERIMENT_TABLE_COLUMNS
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='index' AND name LIKE '%prd075'"
    ).fetchone()[0] == 17
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE '%prd075'"
    ).fetchone()[0] == 72
    for table in EXPERIMENT_TABLE_COLUMNS:
        for target, source_columns, target_columns in _foreign_key_groups(connection, table):
            # 迁移夹具或数据库事实不满足预期时立即失败，避免用弱断言掩盖约束漂移。
            if target == "tenants":
                assert source_columns == ("tenant_id",)
                assert target_columns == ("tenant_id",)
            else:
                assert source_columns[0] == "tenant_id", (table, target, source_columns)
                assert target_columns[0] == "tenant_id", (table, target, target_columns)
    assignment_sql = str(connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='experiment_assignments'"
    ).fetchone()[0]).lower().replace(" ", "").replace("\n", "")
    assert assignment_sql.count("deferrableinitiallydeferred") == 3
    audit_sql = str(connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0])
    assert "'experiment.created'" in audit_sql
    assert "'experiment'" in audit_sql
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_schema
        WHERE type='index' AND name='idx_audit_logs_experiment_owner_unique'
        """
    ).fetchone()[0] == 1
    tenant_id = _assert_revision(
        connection,
        revision=11,
        permission_count=66,
        relation_count=179,
        hashes=REVISION_11_HASHES,
    )
    for role_code in REVISION_11_HASHES:
        assert {"experiment.read", "experiment.write"} <= _role_grants(
            connection, tenant_id, role_code
        )
    _assert_healthy(connection)
    connection.close()


def test_prd075_legacy_virtual_key_scope_claim_uses_canonical_environment(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """兼容 code-only 历史 Key，同时拒绝错误环境和跨租户 scope owner。"""

    _database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "legacy-virtual-key-scope.sqlite3",
    )
    owner = connection.execute(
        """
        SELECT tenant.tenant_id,environment.environment_id,environment.code
        FROM tenants tenant
        JOIN environments environment ON environment.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' AND environment.code='dev'
        """
    ).fetchone()
    assert owner is not None
    tenant_id, environment_id, environment_code = map(str, owner)

    cross_tenant_id = _insert_tenant(connection, "legacy-scope-cross")
    cross_environment = connection.execute(
        """
        SELECT environment_id,code FROM environments
        WHERE tenant_id=? AND code='dev'
        """,
        (cross_tenant_id,),
    ).fetchone()
    assert cross_environment is not None
    cross_environment_id, cross_environment_code = map(str, cross_environment)

    wrong_environment_id = "env_prd075_legacy_wrong"
    legacy_virtual_key_id = "vkey_prd075_legacy_scope"
    connection.execute(
        """
        INSERT INTO environments(
          environment_id,tenant_id,code,name,description,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,'test','PRD075 legacy wrong',NULL,'active',NULL,NULL,?,?)
        """,
        (wrong_environment_id, tenant_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_prd075_legacy',?,'active',?,?,?,NULL,NULL,NULL,
                 NULL,NULL,NULL,NULL,NULL)
        """,
        (
            legacy_virtual_key_id,
            tenant_id,
            "PRD075 legacy scope key",
            "f" * 64,
            NOW,
            NOW,
            environment_code,
        ),
    )
    connection.commit()

    def insert_request_claim(marker: str) -> tuple[str, str]:
        """为一个合成请求写入基线 claim，后续 scope 写入仍由 0068 触发器裁决。"""

        request_id = f"req_prd075_legacy_{marker}"
        trace_id = f"trace_prd075_legacy_{marker}"
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at
            ) VALUES(?,?,?,?,NULL,'disabled',?)
            """,
            (tenant_id, request_id, trace_id, legacy_virtual_key_id, NOW),
        )
        return request_id, trace_id

    def insert_scope_claim(
        *,
        row_tenant_id: str,
        request_id: str,
        trace_id: str,
        row_environment_id: str,
        row_environment_code: str,
    ) -> None:
        """尝试写入不可变 scope；调用方负责提交成功事实或回滚预期拒绝。"""

        connection.execute(
            """
            INSERT INTO gateway_request_scope_claims(
              tenant_id,request_id,trace_id,virtual_key_id,project_id,application_id,
              environment_id,environment_code,created_at
            ) VALUES(?,?,?,?,NULL,NULL,?,?,?)
            """,
            (
                row_tenant_id,
                request_id,
                trace_id,
                legacy_virtual_key_id,
                row_environment_id,
                row_environment_code,
                NOW,
            ),
        )

    valid_request_id, valid_trace_id = insert_request_claim("valid")
    insert_scope_claim(
        row_tenant_id=tenant_id,
        request_id=valid_request_id,
        trace_id=valid_trace_id,
        row_environment_id=environment_id,
        row_environment_code=environment_code,
    )
    connection.commit()
    assert tuple(
        connection.execute(
            """
            SELECT environment_id,environment_code
            FROM gateway_request_scope_claims
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, valid_request_id),
        ).fetchone()
    ) == (environment_id, environment_code)

    wrong_request_id, wrong_trace_id = insert_request_claim("wrong_environment")
    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_scope_claim_owner_mismatch",
    ):
        insert_scope_claim(
            row_tenant_id=tenant_id,
            request_id=wrong_request_id,
            trace_id=wrong_trace_id,
            row_environment_id=wrong_environment_id,
            row_environment_code="test",
        )
    connection.rollback()

    cross_request_id, cross_trace_id = insert_request_claim("cross_tenant")
    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_scope_claim_owner_mismatch",
    ):
        insert_scope_claim(
            row_tenant_id=cross_tenant_id,
            request_id=cross_request_id,
            trace_id=cross_trace_id,
            row_environment_id=cross_environment_id,
            row_environment_code=cross_environment_code,
        )
    connection.rollback()
    _assert_healthy(connection)
    connection.close()


def test_prd075_0067_up_down_reapply_and_new_tenant_initializers(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    baseline = connect_database(_url(prd075_templates["database_0067"]))
    revision_10_permissions = _permission_template_snapshot(baseline)
    revision_10_relations = _role_template_snapshot(baseline)
    baseline.close()
    revision_11_permissions = tuple(
        sorted(
            tuple((*row[:5], 11) for row in revision_10_permissions)
            + EXPERIMENT_PERMISSION_TEMPLATES
        )
    )
    revision_11_relations = tuple(
        sorted(revision_10_relations + EXPERIMENT_ROLE_TEMPLATE_ROWS)
    )

    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "round-trip.sqlite3",
    )
    assert _permission_template_snapshot(connection) == revision_11_permissions
    assert _role_template_snapshot(connection) == revision_11_relations
    up_tenant = _insert_tenant(connection, "up")
    _assert_revision(
        connection,
        revision=11,
        permission_count=66,
        relation_count=179,
        hashes=REVISION_11_HASHES,
        tenant_id=up_tenant,
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (up_tenant,)
    ).fetchone()[0] == 179
    connection.close()

    assert rollback_last_migration(
        database_url,
        migrations_dir=prd075_templates["migrations_0068"],
    ).rolled_back == "0068"
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert _permission_template_snapshot(connection) == revision_10_permissions
    assert _role_template_snapshot(connection) == revision_10_relations
    _assert_revision(
        connection,
        revision=10,
        permission_count=64,
        relation_count=173,
        hashes=REVISION_10_HASHES,
        tenant_id=up_tenant,
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd075'"
    ).fetchone()[0] == 0
    down_audit_sql = str(connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0])
    assert "'experiment.created'" not in down_audit_sql
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_schema
        WHERE type='index' AND name='idx_audit_logs_experiment_owner_unique'
        """
    ).fetchone()[0] == 0
    down_tenant = _insert_tenant(connection, "down")
    _assert_revision(
        connection,
        revision=10,
        permission_count=64,
        relation_count=173,
        hashes=REVISION_10_HASHES,
        tenant_id=down_tenant,
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?", (down_tenant,)
    ).fetchone()[0] == 173
    _assert_healthy(connection)
    connection.close()

    assert apply_migrations(
        database_url,
        migrations_dir=prd075_templates["migrations_0068"],
    ).applied == ["0068"]
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0068"
    assert _permission_template_snapshot(connection) == revision_11_permissions
    assert _role_template_snapshot(connection) == revision_11_relations
    _assert_revision(
        connection,
        revision=11,
        permission_count=66,
        relation_count=179,
        hashes=REVISION_11_HASHES,
        tenant_id=down_tenant,
    )
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    ("include_execution", "include_settlement", "marker"),
    ((False, True, "b"), (True, False, "c")),
)
def test_prd075_deferred_closure_rolls_back_counter_and_facts(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
    include_execution: bool,
    include_settlement: bool,
    marker: str,
) -> None:
    """验证实验主流程、幂等分页和所有者链，固定租户、失败追踪与脱敏边界。"""
    _, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / f"missing-child-{marker}.sqlite3",
    )
    graph = _resource_graph(connection, marker)
    before = tuple(connection.execute(
        """
        SELECT assigned_count,model_a_assigned_count,model_b_assigned_count,facts_version
        FROM model_experiments WHERE tenant_id=? AND experiment_id=?
        """,
        (graph["tenant_id"], graph["experiment_id"]),
    ).fetchone())
    assert before == (0, 0, 0, 0)
    _insert_assignment(
        connection,
        graph,
        marker,
        include_execution=include_execution,
        include_settlement=include_settlement,
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        connection.commit()
    connection.rollback()
    assert tuple(connection.execute(
        """
        SELECT assigned_count,model_a_assigned_count,model_b_assigned_count,facts_version
        FROM model_experiments WHERE tenant_id=? AND experiment_id=?
        """,
        (graph["tenant_id"], graph["experiment_id"]),
    ).fetchone()) == before
    assert connection.execute("SELECT COUNT(*) FROM experiment_assignments").fetchone()[0] == 0
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM experiment_request_executions"
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM experiment_usage_settlements"
        ).fetchone()[0]
        == 0
    )
    _assert_healthy(connection)
    connection.close()


def test_prd075_execution_takeover_uses_database_clock(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证数据库时钟、租约接管、硬截止和回收并发只产生一个权威终态。"""
    _, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "execution-database-clock.sqlite3",
    )
    graph = _resource_graph(connection, "1")
    owner = _insert_assignment(
        connection,
        graph,
        "1",
        include_execution=True,
        include_settlement=True,
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="experiment_execution_state_invalid"):
        connection.execute(
            """
            UPDATE experiment_request_executions
            SET generation=2,lease_owner_id='forged-owner',
                lease_token='forged-takeover-token',lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND execution_id=?
            """,
            (DEADLINE, DEADLINE, graph["tenant_id"], owner["execution_id"]),
        )
    connection.rollback()
    assert tuple(connection.execute(
        """
        SELECT generation,lease_owner_id FROM experiment_request_executions
        WHERE tenant_id=? AND execution_id=?
        """,
        (graph["tenant_id"], owner["execution_id"]),
    ).fetchone()) == (1, "gateway-prd075")
    _assert_healthy(connection)
    connection.close()


def test_prd075_result_recomputes_facts_and_requires_finalization_lease(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证数据库时钟、租约接管、硬截止和回收并发只产生一个权威终态。"""
    _, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "result-owner-facts.sqlite3",
    )
    graph = _resource_graph(connection, "2")
    owner = _insert_assignment(
        connection,
        graph,
        "2",
        include_execution=True,
        include_settlement=True,
    )
    connection.execute(
        """
        INSERT INTO experiment_request_outcomes(
          outcome_id,tenant_id,experiment_id,assignment_id,execution_id,request_id,
          variant,model_id,provider_id,gateway_status,exposure_status,outcome_reason,
          error_code,routing_attempt_id,stream_session_id,quality_eligible,
          quality_succeeded,latency_ms,recovery_generation,facts_at,created_at
        ) VALUES(?,?,?,?,?,?,'a',?,?,'failed','not_exposed','variant_unavailable',
                 'gateway.experiment_variant_unavailable',NULL,NULL,0,NULL,NULL,1,?,?)
        """,
        (
            "exoc_" + "2" * 32,
            graph["tenant_id"],
            graph["experiment_id"],
            owner["assignment_id"],
            owner["execution_id"],
            owner["request_id"],
            graph["model_a_id"],
            graph["provider_id"],
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        UPDATE experiment_request_executions SET status='terminal',updated_at=?
        WHERE tenant_id=? AND execution_id=?
        """,
        (NOW, graph["tenant_id"], owner["execution_id"]),
    )
    connection.execute(
        """
        UPDATE experiment_usage_settlements
        SET status='not_exposed',normalization_status='not_applicable',
            settlement_reason='variant_unavailable',settled_at=?,updated_at=?
        WHERE tenant_id=? AND usage_settlement_id=?
        """,
        (NOW, NOW, graph["tenant_id"], owner["settlement_id"]),
    )
    connection.execute(
        """
        UPDATE model_experiments
        SET status='collecting_feedback',sampling_ended_at=?,feedback_deadline_at=?,
            settlement_deadline_at=?,feedback_fence_closed_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=?
        """,
        (
            NOW,
            DEADLINE,
            SETTLEMENT_DEADLINE,
            NOW,
            NOW,
            graph["tenant_id"],
            graph["experiment_id"],
        ),
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="experiment_result_owner_mismatch"):
        _insert_single_failure_result(connection, graph)
    connection.rollback()

    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_finalization_state_invalid",
    ):
        connection.execute(
            """
            UPDATE model_experiments
            SET finalization_generation=1,finalization_owner_id='control-prd075',
                finalization_lease_token='prd075-finalization-token',
                finalization_lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND experiment_id=?
            """,
            (
                ACTIVE_FINALIZATION_LEASE,
                NOW,
                graph["tenant_id"],
                graph["experiment_id"],
            ),
        )
    connection.rollback()
    _activate_test_scheduler(connection, graph["tenant_id"])
    connection.commit()
    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_finalization_state_invalid",
    ):
        connection.execute(
            """
            UPDATE model_experiments
            SET finalization_generation=2,finalization_owner_id='control-prd075',
                finalization_lease_token='prd075-finalization-token',
                finalization_lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND experiment_id=?
            """,
            (
                ACTIVE_FINALIZATION_LEASE,
                NOW,
                graph["tenant_id"],
                graph["experiment_id"],
            ),
        )
    connection.rollback()
    connection.execute(
        """
        UPDATE model_experiments
        SET finalization_generation=1,finalization_owner_id='control-prd075',
            finalization_lease_token='prd075-finalization-token',
            finalization_lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=?
        """,
        (
            ACTIVE_FINALIZATION_LEASE,
            NOW,
            graph["tenant_id"],
            graph["experiment_id"],
        ),
    )
    connection.commit()

    with pytest.raises(
        sqlite3.IntegrityError,
        match="experiment_finalization_state_invalid",
    ):
        connection.execute(
            """
            UPDATE model_experiments SET updated_at=?
            WHERE tenant_id=? AND experiment_id=?
            """,
            (
                (TEST_ORIGIN + timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z"),
                graph["tenant_id"],
                graph["experiment_id"],
            ),
        )
    connection.rollback()
    invalid_metrics = (
        {"gateway_success_count": 1, "gateway_failure_count": 0,
         "gateway_success_rate_bps": 10000},
        {"quality_success_count": 1, "latency_sample_count": 1,
         "total_latency_ms": 10, "average_latency_micros": 10000,
         "p95_latency_ms": 10, "quality_success_rate_bps": 10000},
        {"settlement_recorded_count": 1, "settlement_not_exposed_count": 0,
         "gross_normalized_cny_micros": 10, "cost_known_count": 1,
         "average_cost_cny_micros": 10},
        {"feedback_count": 1, "rating_sum": 5, "average_rating_milli": 5000,
         "accepted_count": 1, "adoption_rate_bps": 10000},
        {
            "facts_through_at": (TEST_ORIGIN + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "created_at": (TEST_ORIGIN + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    for metrics in invalid_metrics:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="experiment_result_owner_mismatch",
        ):
            _insert_single_failure_result(connection, graph, **metrics)
        connection.rollback()

    _insert_single_failure_result(connection, graph)
    connection.commit()
    assert tuple(connection.execute(
        """
        SELECT assigned_count,gateway_success_count,gateway_failure_count,
               settlement_not_exposed_count,feedback_count
        FROM experiment_results WHERE tenant_id=? AND experiment_id=? AND variant='a'
        """,
        (graph["tenant_id"], graph["experiment_id"]),
    ).fetchone()) == (1, 0, 1, 1, 0)
    _assert_healthy(connection)
    connection.close()


def test_prd075_owner_immutability_replace_and_business_down_guard(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "constraints.sqlite3",
    )
    graph = _resource_graph(connection, "d")
    owner = _insert_assignment(
        connection,
        graph,
        "d",
        include_execution=True,
        include_settlement=True,
    )
    connection.commit()
    assert tuple(connection.execute(
        """
        SELECT assigned_count,model_a_assigned_count,model_b_assigned_count,facts_version
        FROM model_experiments WHERE tenant_id=? AND experiment_id=?
        """,
        (graph["tenant_id"], graph["experiment_id"]),
    ).fetchone()) == (1, 1, 0, 1)
    assert connection.execute(
        "SELECT COUNT(*) FROM experiment_assignments WHERE assignment_id=?",
        (owner["assignment_id"],),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM experiment_request_executions WHERE execution_id=?",
        (owner["execution_id"],),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM experiment_usage_settlements WHERE usage_settlement_id=?",
        (owner["settlement_id"],),
    ).fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError, match="experiment_assignment_immutable"):
        connection.execute(
            "UPDATE experiment_assignments SET sample_bucket=1 WHERE assignment_id=?",
            (owner["assignment_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_immutable"):
        connection.execute(
            "UPDATE model_experiments SET name='changed' WHERE experiment_id=?",
            (graph["experiment_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_scope_claim_immutable"):
        connection.execute(
            "UPDATE gateway_request_scope_claims SET environment_code='test' WHERE request_id=?",
            (owner["request_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_arm_parent_immutable"):
        connection.execute(
            "UPDATE models SET capabilities=? WHERE model_id=?",
            ('{"chat":false}', graph["model_a_id"]),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_owner_mismatch"):
        connection.execute(
            """
            INSERT OR REPLACE INTO model_experiments
            SELECT * FROM model_experiments WHERE experiment_id=?
            """,
            (graph["experiment_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_immutable"):
        connection.execute(
            "UPDATE OR REPLACE model_experiments SET name='changed' WHERE experiment_id=?",
            (graph["experiment_id"],),
        )
    connection.rollback()
    _assert_healthy(connection)
    connection.close()

    _assert_down_blocked(database_url, prd075_templates["migrations_0068"])


def test_prd075_experiment_audit_owner_and_down_guard(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "experiment-audit.sqlite3",
    )
    graph = _resource_graph(connection, "e")
    wrong_graph = dict(graph)
    wrong_graph["model_a_id"] = graph["model_b_id"]
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
        _insert_experiment_audit(connection, wrong_graph, "e")
    connection.rollback()
    audit_log_id = _insert_experiment_audit(connection, graph, "e")
    connection.commit()
    assert connection.execute(
        """
        SELECT COUNT(*) FROM audit_logs
        WHERE tenant_id=? AND audit_log_id=? AND action='experiment.created'
          AND object_type='experiment' AND object_id=?
        """,
        (graph["tenant_id"], audit_log_id, graph["experiment_id"]),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_log_export_sequences WHERE tenant_id=? AND audit_log_id=?",
        (graph["tenant_id"], audit_log_id),
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()

    _assert_down_blocked(database_url, prd075_templates["migrations_0068"])
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE audit_log_id=?",
        (audit_log_id,),
    ).fetchone()[0] == 1
    connection.close()


def test_prd075_outbox_claim_processing_heartbeat_snapshot_and_owner_cas(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证实验完成事件源、Outbox 租约、恢复上限和 Webhook 外部事实边界。"""
    _, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "outbox-heartbeat.sqlite3",
    )
    graph = _resource_graph(connection, "f")
    event_id = _complete_empty_experiment(connection, graph, "f")
    assert tuple(connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,lease_generation,next_attempt_at,
               delivery_snapshot_frozen_at
        FROM experiment_event_outbox WHERE tenant_id=? AND experiment_event_id=?
        """,
        (graph["tenant_id"], event_id),
    ).fetchone()) == ("pending", 0, 0, NOW, None)
    connection.execute(
        """
        UPDATE experiment_event_outbox
        SET dispatch_status='processing',dispatch_attempts=1,lease_generation=1,
            next_attempt_at=NULL,claim_token=lower(hex(randomblob(16))),
            lease_owner_id='control-prd075',lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_event_id=?
        """,
        (LEASE, NOW, graph["tenant_id"], event_id),
    )
    connection.commit()
    heartbeat_at = (TEST_ORIGIN + timedelta(seconds=30)).isoformat().replace(
        "+00:00", "Z"
    )
    heartbeat_lease = (TEST_ORIGIN + timedelta(minutes=1, seconds=30)).isoformat().replace(
        "+00:00", "Z"
    )
    connection.execute(
        """
        UPDATE experiment_event_outbox
        SET delivery_snapshot_frozen_at=?,lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_event_id=? AND dispatch_status='processing'
        """,
        (NOW, heartbeat_lease, heartbeat_at, graph["tenant_id"], event_id),
    )
    connection.commit()
    assert tuple(connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,lease_generation,lease_owner_id,
               lease_expires_at,delivery_snapshot_frozen_at
        FROM experiment_event_outbox WHERE tenant_id=? AND experiment_event_id=?
        """,
        (graph["tenant_id"], event_id),
    ).fetchone()) == (
        "processing",
        1,
        1,
        "control-prd075",
        heartbeat_lease,
        NOW,
    )
    with pytest.raises(sqlite3.IntegrityError, match="experiment_outbox_state_invalid"):
        connection.execute(
            """
            UPDATE experiment_event_outbox
            SET lease_owner_id='other-control',updated_at=?
            WHERE tenant_id=? AND experiment_event_id=?
            """,
            (
                (TEST_ORIGIN + timedelta(seconds=40)).isoformat().replace(
                    "+00:00", "Z"
                ),
                graph["tenant_id"],
                event_id,
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_outbox_state_invalid"):
        connection.execute(
            """
            UPDATE experiment_event_outbox
            SET claim_token=lower(hex(randomblob(16))),updated_at=?
            WHERE tenant_id=? AND experiment_event_id=?
            """,
            (
                (TEST_ORIGIN + timedelta(seconds=40)).isoformat().replace(
                    "+00:00", "Z"
                ),
                graph["tenant_id"],
                event_id,
            ),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="experiment_outbox_state_invalid"):
        connection.execute(
            """
            UPDATE experiment_event_outbox
            SET lease_generation=lease_generation+1,updated_at=?
            WHERE tenant_id=? AND experiment_event_id=?
            """,
            (
                (TEST_ORIGIN + timedelta(seconds=40)).isoformat().replace(
                    "+00:00", "Z"
                ),
                graph["tenant_id"],
                event_id,
            ),
        )
    connection.rollback()
    takeover_at = (TEST_ORIGIN + timedelta(minutes=1, seconds=31)).isoformat().replace(
        "+00:00", "Z"
    )
    takeover_lease = (TEST_ORIGIN + timedelta(minutes=3)).isoformat().replace(
        "+00:00", "Z"
    )
    connection.execute(
        """
        UPDATE experiment_event_outbox
        SET dispatch_attempts=dispatch_attempts+1,lease_generation=lease_generation+1,
            claim_token=lower(hex(randomblob(16))),lease_owner_id='other-control',
            lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_event_id=? AND dispatch_status='processing'
        """,
        (takeover_lease, takeover_at, graph["tenant_id"], event_id),
    )
    connection.commit()
    assert tuple(connection.execute(
        """
        SELECT dispatch_attempts,lease_generation,lease_owner_id,lease_expires_at
        FROM experiment_event_outbox WHERE tenant_id=? AND experiment_event_id=?
        """,
        (graph["tenant_id"], event_id),
    ).fetchone()) == (2, 2, "other-control", takeover_lease)
    _assert_healthy(connection)
    connection.close()


def test_prd075_role_permission_event_blocks_down_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "role-event-down-guard.sqlite3",
    )
    event_id = _insert_role_permission_event(connection, "a")
    connection.close()
    _assert_down_blocked(database_url, prd075_templates["migrations_0068"])
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permission_events WHERE event_id=?",
        (event_id,),
    ).fetchone()[0] == 1
    connection.close()


@pytest.mark.parametrize(
    "error_code",
    (
        "gateway.experiment_unavailable",
        "gateway.experiment_variant_unavailable",
        "gateway.experiment_execution_timeout",
    ),
)
def test_prd075_request_log_error_codes_block_down_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
    error_code: str,
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    marker = error_code.rsplit("_", 1)[-1].replace(".", "-")
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / f"request-log-{marker}.sqlite3",
    )
    tenant_id = str(connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0])
    request_id = f"req_prd075_{marker}"
    trace_id = f"trace_prd075_{marker}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,NULL,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, NOW),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,status,error_code,created_at,updated_at
        ) VALUES(?,?,?,'failed',?,?,?)
        """,
        (request_id, tenant_id, trace_id, error_code, NOW, NOW),
    )
    connection.commit()
    connection.close()
    _assert_down_blocked(database_url, prd075_templates["migrations_0068"])
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM request_logs WHERE tenant_id=? AND request_id=? AND error_code=?",
        (tenant_id, request_id, error_code),
    ).fetchone()[0] == 1
    connection.close()


@pytest.mark.parametrize("fact_kind", ("endpoint", "delivery", "attempt", "dead_letter"))
def test_prd075_webhook_external_facts_block_down_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
    fact_kind: str,
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    marker = {"endpoint": "a", "delivery": "b", "attempt": "c", "dead_letter": "d"}[
        fact_kind
    ]
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / f"webhook-{fact_kind}.sqlite3",
    )
    tenant_id, user_id = map(str, connection.execute(
        """
        SELECT tenant_id,user_id FROM users
        WHERE role='admin' AND status='active' ORDER BY tenant_id,user_id LIMIT 1
        """
    ).fetchone())
    endpoint_id = _insert_webhook_endpoint(
        connection,
        tenant_id,
        user_id,
        marker,
        ["experiment.completed"] if fact_kind == "endpoint" else ["audit.recorded"],
    )
    delivery_id: str | None = None
    if fact_kind != "endpoint":
        delivery_id = _insert_webhook_delivery(
            connection,
            tenant_id,
            endpoint_id,
            marker,
            failed=fact_kind == "dead_letter",
        )
    if fact_kind == "attempt":
        connection.execute(
            """
            INSERT INTO webhook_delivery_attempts(
              webhook_delivery_attempt_id,tenant_id,webhook_delivery_id,
              webhook_endpoint_id,attempt_no,status,error_code,created_at
            ) VALUES(?,?,?,?,1,'failed','webhook.delivery_unavailable',?)
            """,
            (f"wha_prd075_{marker}", tenant_id, delivery_id, endpoint_id, NOW),
        )
    connection.commit()
    if fact_kind == "dead_letter":
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_dead_letter_events WHERE webhook_delivery_id=?",
            (delivery_id,),
        ).fetchone()[0] == 1
    connection.close()
    _assert_down_blocked(database_url, prd075_templates["migrations_0068"])
    connection = connect_database(database_url)
    if fact_kind == "endpoint":
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_endpoints WHERE webhook_endpoint_id=?",
            (endpoint_id,),
        ).fetchone()[0] == 1
    else:
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_delivery_id=?",
            (delivery_id,),
        ).fetchone()[0] == 1
    connection.close()


def test_prd075_idle_scheduler_cursor_allows_down(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "scheduler-idle.sqlite3",
    )
    tenant_id = str(connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0])
    connection.execute(
        """
        INSERT INTO experiment_scheduler_tenants(
          tenant_id,next_run_at,lease_generation,created_at,updated_at
        ) VALUES(?,?,0,?,?)
        """,
        (tenant_id, NOW, NOW, NOW),
    )
    connection.commit()
    connection.close()
    assert rollback_last_migration(
        database_url,
        migrations_dir=prd075_templates["migrations_0068"],
    ).rolled_back == "0068"
    connection = connect_database(database_url)
    assert _applied_head(connection) == "0067"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name='experiment_scheduler_tenants'"
    ).fetchone()[0] == 0
    _assert_healthy(connection)
    connection.close()


def test_prd075_active_scheduler_lease_blocks_down_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "scheduler-active.sqlite3",
    )
    tenant_id = str(connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0])
    _activate_test_scheduler(connection, tenant_id)
    connection.commit()
    connection.close()
    _assert_down_blocked(database_url, prd075_templates["migrations_0068"])


def test_prd075_custom_developer_and_ops_are_not_silently_expanded(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证实验主流程、幂等分页和所有者链，固定租户、失败追踪与脱敏边界。"""
    database = tmp_path / "custom-role.sqlite3"
    shutil.copy2(prd075_templates["database_0067"], database)
    database_url = _url(database)
    connection = connect_database(database_url)
    tenant_id = str(connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0])
    snapshots: dict[str, tuple[int, str]] = {}
    for index, role_code in enumerate(("developer", "ops_admin"), start=1):
        role_id, revision = connection.execute(
            "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()
        permission_id = connection.execute(
            "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='batch.write'",
            (tenant_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
            (tenant_id, role_id, permission_id),
        )
        custom_hash = str(index) * 64
        connection.execute(
            """
            UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (custom_hash, NOW, tenant_id, role_id),
        )
        snapshots[role_code] = (int(revision) + 1, custom_hash)
    connection.commit()
    connection.close()

    assert apply_migrations(
        database_url,
        migrations_dir=prd075_templates["migrations_0068"],
    ).applied == ["0068"]
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM permissions WHERE tenant_id=? AND catalog_revision=11",
        (tenant_id,),
    ).fetchone()[0] == 66
    for role_code, snapshot in snapshots.items():
        assert tuple(connection.execute(
            "SELECT permission_revision,matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()) == snapshot
        assert not ({"experiment.read", "experiment.write"} & _role_grants(
            connection, tenant_id, role_code
        ))
    connection.close()

    assert rollback_last_migration(
        database_url,
        migrations_dir=prd075_templates["migrations_0068"],
    ).rolled_back == "0068"
    connection = connect_database(database_url)
    for role_code, snapshot in snapshots.items():
        assert tuple(connection.execute(
            "SELECT permission_revision,matrix_sha256 FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()) == snapshot
    _assert_healthy(connection)
    connection.close()


def test_prd075_post_migration_matrix_drift_blocks_down_atomically(
    tmp_path: Path,
    prd075_templates: dict[str, Path],
) -> None:
    """验证 0068 正反迁移、RBAC revision 与业务阻断守卫保持原子且可重复。"""
    database_url, connection = _clone_and_upgrade(
        prd075_templates,
        tmp_path / "matrix-drift.sqlite3",
    )
    tenant_id, role_id = connection.execute(
        "SELECT tenant_id,role_id FROM roles WHERE code='developer' ORDER BY tenant_id LIMIT 1"
    ).fetchone()
    permission_id = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='experiment.write'",
        (tenant_id,),
    ).fetchone()[0]
    connection.execute(
        "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
        (tenant_id, role_id, permission_id),
    )
    connection.execute(
        """
        UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        ("f" * 64, NOW, tenant_id, role_id),
    )
    connection.commit()
    connection.close()
    _assert_down_blocked(
        database_url,
        prd075_templates["migrations_0068"],
        expected_guard="prd075_down_role_guard_ok",
    )
