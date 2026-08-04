"""PRD-050 Prompt Cache 亲和路由的 SQLite 状态机边界测试。

本文件刻意不复用 HTTP 集成测试的大型夹具：冷并发仍穿过真实 Route、Attempt 与 KeyPool
提交入口，其余用例直接驱动 0040 表、触发器和生命周期函数。这样可以精确覆盖 lease、
target generation、确定性容量清理和多批启动恢复，同时确保测试数据不包含 Prompt、Key
明文、Credential 标识或可逆业务内容。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_gateway.cache_affinity import keypool as affinity_keypool
from metahub_gateway.cache_affinity import lifecycle as affinity_lifecycle
from metahub_gateway.cache_affinity import routing as affinity_routing
from metahub_gateway.cache_affinity.keypool import select_provider_access
from metahub_gateway.cache_affinity.lifecycle import (
    _rebuild_binding_activity,
    reconcile_pending_cache_affinity_selections,
    recover_cache_affinity_state,
    safe_finalize_cache_affinity_selection,
)
from metahub_gateway.cache_affinity.models import (
    CacheAffinityRouteContext,
    CacheAffinitySelectionHandle,
)
from metahub_gateway.providers.keypool import SelectedProviderKey
from metahub_gateway.routing.attempts import RoutingAttempt, create_routing_attempt
from metahub_gateway.routing.models import (
    GatewayRoute,
    GatewayRoutePlan,
    resolve_model_route,
    resolve_model_route_plan,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError

# 固定测试秘密只用于进程内配置。断言、数据库事实和异常文本均不得包含这些值。
VIRTUAL_KEY = "mhv_prd050_state_machine"
PROVIDER_KEY = "sk-prd050-state-machine"
CACHE_SECRET = "prd050-state-machine-hmac-secret-0123456789abcdef"
AFFINITY_ID = "caf_" + "5" * 32
ALIAS_ID = "alias_prd050_state_machine"
PUBLIC_ALIAS = "state-machine-cache"
# 固定在当前执行时间之前，确保生命周期函数生成的真实 now 始终满足 0040 单调时间守卫。
BASE_TIME = datetime(2020, 7, 21, 1, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StateGraph:
    """保存每个测试独占的真实 0040 最小租户图。

    字段只包含数据库 URL 和非敏感业务 ID。Provider Key、Virtual Key 明文及 HMAC Secret
    仅存在于环境配置，不允许放进该对象的 ``repr`` 或失败诊断。
    """

    database_url: str
    config: AppConfig
    tenant_id: str
    virtual_key_id: str
    model_alias_id: str
    affinity_id: str
    model_id: str
    model_name: str
    provider_id: str
    provider_code: str


@dataclass(frozen=True, slots=True)
class OpenSelection:
    """汇总一次已经提交但尚未结束的 Provider 前 Selection。

    返回对象只服务测试内状态跳转；Selection 句柄自身已通过 ``repr=False`` 隐藏 Binding、
    revision 与 Credential 快照，测试也不会输出完整对象。
    """

    plan: GatewayRoutePlan
    attempt: RoutingAttempt
    handle: CacheAffinitySelectionHandle


@pytest.fixture
def state_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StateGraph:
    """应用真实迁移和种子，建立带 fixed RoutingPolicy 的 enabled CacheAffinity。

    参数：pytest 环境补丁与独占临时目录。返回：最小状态图。关键约束：Alias INSERT 由
    0036/0037 触发器自动创建 fixed v1，测试不能绕过生产数据库的 owner 图与版本约束。
    """

    database_url = f"sqlite:///{(tmp_path / 'prd050-state-machine.db').as_posix()}"
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_CACHE_AFFINITY_HMAC_SECRET", CACHE_SECRET)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        environment = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (seed.tenant_id,),
        ).fetchone()
        model = connection.execute(
            """
            SELECT model_id, model_alias, provider_id
            FROM models WHERE tenant_id=? AND model_alias='default-chat'
            """,
            (seed.tenant_id,),
        ).fetchone()
        actor = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at, user_id LIMIT 1",
            (seed.tenant_id,),
        ).fetchone()
        virtual_key = connection.execute(
            "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND key_prefix=?",
            (seed.tenant_id, seed.virtual_key_prefix),
        ).fetchone()
        provider = connection.execute(
            "SELECT provider_code FROM providers WHERE tenant_id=? AND provider_id=?",
            (seed.tenant_id, model["provider_id"] if model else ""),
        ).fetchone()
        assert all(item is not None for item in (environment, model, actor, virtual_key, provider))
        now = _db_time(BASE_TIME)
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id,
              default_model_id, fallback_model_id, status, created_by, created_at, updated_at
            ) VALUES(?, ?, ?, 'dev', ?, ?, NULL, 'active', ?, ?, ?)
            """,
            (
                ALIAS_ID,
                seed.tenant_id,
                PUBLIC_ALIAS,
                environment["environment_id"],
                model["model_id"],
                actor["user_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinities(
              cache_affinity_id, tenant_id, model_alias_id, status, data_class,
              minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
              created_at, updated_at
            ) VALUES(?, ?, ?, 'enabled', 'internal', 256, 3600, 1, ?, ?, ?, ?)
            """,
            (
                AFFINITY_ID,
                seed.tenant_id,
                ALIAS_ID,
                actor["user_id"],
                actor["user_id"],
                now,
                now,
            ),
        )
        connection.commit()
        return StateGraph(
            database_url=database_url,
            config=load_app_config("gateway-data-plane"),
            tenant_id=seed.tenant_id,
            virtual_key_id=str(virtual_key["virtual_key_id"]),
            model_alias_id=ALIAS_ID,
            affinity_id=AFFINITY_ID,
            model_id=str(model["model_id"]),
            model_name=str(model["model_alias"]),
            provider_id=str(model["provider_id"]),
            provider_code=str(provider["provider_code"]),
        )
    finally:
        connection.close()


def _db_time(value: datetime) -> str:
    """把测试时刻转换为生产状态机使用的秒级 UTC ``Z`` 文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _long_payload() -> dict[str, object]:
    """构造越过 256 字符门限的固定 Chat 请求，不在断言消息中展开正文。"""

    return {
        "model": PUBLIC_ALIAS,
        "messages": [
            {"role": "system", "content": "企业制度固定前缀" * 40},
            {"role": "user", "content": "状态机测试"},
        ],
    }


def _insert_claim(graph: StateGraph, request_id: str, trace_id: str) -> None:
    """先提交可信 Request Claim，使后续 Selection 触发器能验证租户与 Virtual Key。"""

    connection = connect_database(graph.database_url)
    try:
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
            """,
            (graph.tenant_id, request_id, trace_id, graph.virtual_key_id, _db_time(BASE_TIME)),
        )
        connection.commit()
    finally:
        connection.close()


def _open_selection(
    graph: StateGraph, ordinal: int, *, claim_exists: bool = False
) -> OpenSelection:
    """走真实 Route/Attempt/KeyPool 三段提交并保留 pending Selection。

    参数：状态图、请求序号和 Claim 是否已由并发测试预建。返回：路由计划、父 Attempt 与
    Selection 句柄。函数不调用 Provider，也不提交任何成功证据，因此所有返回行都应是 miss。
    """

    request_id = f"req_prd050_sm_{ordinal:04d}"
    trace_id = f"trc_prd050_sm_{ordinal:04d}"
    if not claim_exists:
        _insert_claim(graph, request_id, trace_id)
    plan = resolve_model_route_plan(
        graph.config,
        tenant_id=graph.tenant_id,
        model_alias=PUBLIC_ALIAS,
        environment="dev",
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=graph.virtual_key_id,
        request_kind="chat",
        payload=_long_payload(),
    )
    assert plan.cache_affinity_context is not None
    attempt = create_routing_attempt(
        graph.config,
        plan,
        plan.initial_route,
        request_id=request_id,
        trace_id=trace_id,
        attempt_sequence=1,
        attempt_role="primary",
    )
    access = select_provider_access(
        graph.config,
        plan.initial_route,
        affinity=plan.cache_affinity_context,
        request_id=request_id,
        trace_id=trace_id,
        attempt_sequence=1,
        routing_attempt_id=attempt.routing_attempt_id,
        stream_session_id=None,
    )
    assert access.cache_affinity_selection is not None
    return OpenSelection(plan=plan, attempt=attempt, handle=access.cache_affinity_selection)


def _finish_attempt_parent(
    graph: StateGraph,
    attempt: RoutingAttempt,
    *,
    succeeded: bool,
    completed_at: datetime,
) -> None:
    """直接提交触发器认可的父 Attempt 终态，避免把质量计分引入状态机专项测试。"""

    connection = connect_database(graph.database_url)
    try:
        now = _db_time(completed_at)
        cursor = connection.execute(
            """
            UPDATE routing_attempts
            SET status=?, error_code=?, error_category=?, latency_ms=1,
                usage_status=?, completed_at=?, updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
            """,
            (
                "succeeded" if succeeded else "failed",
                None if succeeded else "gateway.execution_interrupted",
                None if succeeded else "other",
                "pending" if succeeded else "not_applicable",
                now,
                now,
                graph.tenant_id,
                attempt.routing_attempt_id,
            ),
        )
        assert cursor.rowcount == 1
        connection.commit()
    finally:
        connection.close()


def _route_for_direct_state(graph: StateGraph) -> GatewayRoute:
    """解析同租户真实 Model/Provider 快照，供容量失败映射测试重验 endpoint。"""

    return resolve_model_route(
        graph.config,
        tenant_id=graph.tenant_id,
        model_alias=graph.model_name,
    )


def _insert_scope(
    connection: sqlite3.Connection,
    graph: StateGraph,
    ordinal: int,
    *,
    selected_at: datetime = BASE_TIME,
) -> tuple[str, str]:
    """插入无指针 Scope，并返回严格形状的 Scope ID 与不可逆测试摘要。"""

    scope_id = f"cafs_{ordinal:032x}"
    scope_digest = f"{ordinal + 10_000:064x}"
    now = _db_time(selected_at)
    connection.execute(
        """
        INSERT INTO cache_affinity_scopes(
          cache_affinity_scope_id, tenant_id, virtual_key_id, digest_schema_version,
          affinity_scope_digest, preferred_binding_id, preferred_lease_generation,
          last_selected_at, created_at, updated_at
        ) VALUES(?, ?, ?, 1, ?, NULL, NULL, ?, ?, ?)
        """,
        (scope_id, graph.tenant_id, graph.virtual_key_id, scope_digest, now, now, now),
    )
    return scope_id, scope_digest


def _insert_binding(
    connection: sqlite3.Connection,
    graph: StateGraph,
    *,
    scope_id: str,
    ordinal: int,
    lease_live: bool = True,
    point_scope: bool = False,
) -> sqlite3.Row:
    """插入 environment Binding，可选择建立同 generation Scope 指针。

    ``lease_live`` 控制该行能否安全淘汰。摘要和 revision 均为不可逆固定十六进制值，且
    每个 ordinal 全局唯一，防止测试自身撞上 0040 唯一索引而掩盖容量行为。
    """

    binding_id = f"cab_{ordinal:032x}"
    endpoint_revision = f"{ordinal + 20_000:064x}"
    affinity_key = f"{ordinal + 30_000:064x}"
    now = _db_time(BASE_TIME)
    lease_expires = _db_time(BASE_TIME + (timedelta(days=3650) if lease_live else -timedelta(1)))
    connection.execute(
        """
        INSERT INTO cache_affinity_bindings(
          cache_affinity_binding_id, tenant_id, cache_affinity_scope_id,
          affinity_key_digest, model_endpoint_id, endpoint_revision_digest,
          provider_id, provider_credential_id, target_kind, target_generation,
          pending_lease_generation, pending_lease_started_at, pending_lease_expires_at,
          active_selection_count, last_success_at, selection_count,
          estimated_hit_count, last_selected_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, 'environment', 1, 1, ?, ?, 0,
                 NULL, 0, 0, ?, ?, ?)
        """,
        (
            binding_id,
            graph.tenant_id,
            scope_id,
            affinity_key,
            graph.model_id,
            endpoint_revision,
            graph.provider_id,
            now,
            lease_expires,
            now,
            now,
            now,
        ),
    )
    if point_scope:
        connection.execute(
            """
            UPDATE cache_affinity_scopes
            SET preferred_binding_id=?, preferred_lease_generation=1, updated_at=?
            WHERE tenant_id=? AND cache_affinity_scope_id=?
            """,
            (binding_id, now, graph.tenant_id, scope_id),
        )
    row = connection.execute(
        "SELECT * FROM cache_affinity_bindings WHERE tenant_id=? AND cache_affinity_binding_id=?",
        (graph.tenant_id, binding_id),
    ).fetchone()
    assert row is not None
    return row


def _insert_pending_selection_fact(
    connection: sqlite3.Connection,
    graph: StateGraph,
    binding: sqlite3.Row,
    ordinal: int,
    *,
    terminal: str | None = None,
) -> tuple[str, str]:
    """按 0040 完整图插入 Claim、started Attempt 与 pending Selection。

    参数：事务连接、状态图、当前 Binding、唯一序号及可选父终态。返回 request/attempt ID。
    若指定终态，必须先以 started 通过 Selection 插入触发器，再推进父门；Selection 本身仍
    保持 pending，留给惰性对账或启动恢复处理。
    """

    if terminal not in {None, "succeeded", "failed"}:
        # 非法夹具会制造迁移合同之外的父状态，必须在任何数据库事实写入前显式拒绝。
        raise ValueError("test_terminal_invalid")
    request_id = f"req_prd050_fact_{ordinal:04d}"
    trace_id = f"trc_prd050_fact_{ordinal:04d}"
    attempt_id = f"rta_prd050_fact_{ordinal:04d}"
    selection_id = f"cas_{ordinal + 100_000:032x}"
    created_at = _db_time(BASE_TIME + timedelta(seconds=ordinal))
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        (graph.tenant_id, request_id, trace_id, graph.virtual_key_id, created_at),
    )
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, model_alias_id,
          fallback_policy_id, fallback_policy_version, attempt_sequence, attempt_role,
          trigger_type, trigger_error_code, model_id, internal_model_id, provider_id,
          provider_code, status, retryable, usage_status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, NULL, NULL, 1, 'primary', 'initial', NULL,
                 ?, ?, ?, ?, 'started', 0, 'pending', ?, ?)
        """,
        (
            attempt_id,
            graph.tenant_id,
            request_id,
            trace_id,
            graph.model_alias_id,
            graph.model_id,
            graph.model_name,
            graph.provider_id,
            graph.provider_code,
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO cache_affinity_selections(
          cache_affinity_selection_id, tenant_id, request_id, trace_id,
          attempt_sequence, routing_attempt_id, stream_session_id,
          cache_affinity_id, cache_affinity_version,
          cache_affinity_scope_id_snapshot, cache_affinity_binding_id_snapshot,
          model_alias_id, model_endpoint_id, endpoint_revision_digest_snapshot,
          provider_id, provider_code_snapshot, provider_credential_id_snapshot,
          target_kind, target_generation, pending_lease_generation_snapshot,
          ttl_seconds_snapshot, warm_evidence_at_snapshot,
          endpoint_preference_applied, endpoint_preference_state,
          binding_state_before, selection_reason, estimated_hit,
          outcome_status, outcome_at, created_at
        ) VALUES(
          ?, ?, ?, ?, 1, ?, NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, 3600, ?, 0, 'pending', 'pending',
          'binding_pending_reused', 0, 'pending', NULL, ?
        )
        """,
        (
            selection_id,
            graph.tenant_id,
            request_id,
            trace_id,
            attempt_id,
            graph.affinity_id,
            binding["cache_affinity_scope_id"],
            binding["cache_affinity_binding_id"],
            graph.model_alias_id,
            graph.model_id,
            binding["endpoint_revision_digest"],
            graph.provider_id,
            graph.provider_code,
            binding["provider_credential_id"],
            binding["target_kind"],
            binding["target_generation"],
            binding["pending_lease_generation"],
            binding["last_success_at"],
            created_at,
        ),
    )
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=active_selection_count+1,
            selection_count=selection_count+1, last_selected_at=?, updated_at=?
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (
            created_at,
            created_at,
            graph.tenant_id,
            binding["cache_affinity_binding_id"],
        ),
    )
    if terminal is not None:
        completed_at = _db_time(BASE_TIME + timedelta(hours=1, seconds=ordinal))
        connection.execute(
            """
            UPDATE routing_attempts
            SET status=?, error_code=?, error_category=?, latency_ms=1,
                usage_status=?, completed_at=?, updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
            """,
            (
                terminal,
                None if terminal == "succeeded" else "gateway.execution_interrupted",
                None if terminal == "succeeded" else "other",
                "pending" if terminal == "succeeded" else "not_applicable",
                completed_at,
                completed_at,
                graph.tenant_id,
                attempt_id,
            ),
        )
    return request_id, attempt_id


@pytest.mark.parametrize(
    "status",
    ["output_blocked", "output_safety_unavailable"],
)
def test_output_safety_stream_terminal_maps_cache_affinity_to_provider_success(
    status: str,
) -> None:
    """Provider 后本地输出安全终态必须映射为 Selection succeeded。

    参数：冻结的两个 PRD-071 会话终态。返回值：无。关键约束：映射只读取同 tenant、
    request、trace 的已完成父行，防止安全阻断被错误解释成 Provider 质量失败。
    """

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE stream_sessions(
          tenant_id TEXT NOT NULL,
          stream_session_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          status TEXT NOT NULL,
          completed_at TEXT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO stream_sessions(
          tenant_id, stream_session_id, request_id, trace_id, status, completed_at
        ) VALUES('tenant_prd071', 'str_prd071', 'req_prd071', 'trc_prd071', ?, ?)
        """,
        (status, _db_time(BASE_TIME)),
    )

    terminal = affinity_lifecycle._execution_terminal(
        connection,
        {
            "tenant_id": "tenant_prd071",
            "stream_session_id": "str_prd071",
            "routing_attempt_id": None,
            "request_id": "req_prd071",
            "trace_id": "trc_prd071",
        },
    )

    assert terminal == ("succeeded", _db_time(BASE_TIME))
    connection.close()


def test_cold_concurrency_creates_one_binding_and_all_selections_are_misses(
    state_graph: StateGraph,
) -> None:
    """八个冷请求并发时只创建一个 Binding，且全部首选均为 miss。

    并发 worker 使用独立 SQLite 连接；``BEGIN IMMEDIATE``、唯一索引和 KeyPool 原子提交
    必须共同保证一条 Binding、八条 pending Selection 以及准确的活动计数。
    """

    request_count = 8
    for ordinal in range(request_count):
        _insert_claim(
            state_graph,
            f"req_prd050_sm_{ordinal:04d}",
            f"trc_prd050_sm_{ordinal:04d}",
        )

    with ThreadPoolExecutor(max_workers=request_count) as executor:
        opened = list(
            executor.map(
                lambda ordinal: _open_selection(state_graph, ordinal, claim_exists=True),
                range(request_count),
            )
        )

    assert len(opened) == request_count
    first = opened[0]
    assert first.plan.cache_affinity_context is not None
    connection = connect_database(state_graph.database_url)
    try:
        state_before_reentry = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM cache_affinity_selections WHERE tenant_id=?) AS selections,
              (SELECT active_selection_count FROM cache_affinity_bindings WHERE tenant_id=?)
                AS active_selections,
              (SELECT selection_count FROM cache_affinity_bindings WHERE tenant_id=?)
                AS selection_count
            """,
            (state_graph.tenant_id, state_graph.tenant_id, state_graph.tenant_id),
        ).fetchone()
    finally:
        connection.close()

    # 模拟 KeyPool COMMIT 成功但调用方未收到结果后的同意图重入：只能恢复原 Selection，
    # 不能重复增加 Binding 计数，也不能再次生成 request_selected 事件。
    restored = select_provider_access(
        state_graph.config,
        first.plan.initial_route,
        affinity=first.plan.cache_affinity_context,
        request_id=first.attempt.request_id,
        trace_id=first.attempt.trace_id,
        attempt_sequence=first.attempt.attempt_sequence,
        routing_attempt_id=first.attempt.routing_attempt_id,
        stream_session_id=None,
    )
    assert restored.cache_affinity_selection is not None
    assert (
        restored.cache_affinity_selection.cache_affinity_selection_id
        == first.handle.cache_affinity_selection_id
    )
    assert restored.cache_affinity_event is None

    # 相同 request/attempt 序号却换成另一请求的父 Attempt 属于异意图重入；固定失败且不得
    # 改写原 Selection、Binding 活动数或累计选择数。
    with pytest.raises(ApiError) as conflict:
        select_provider_access(
            state_graph.config,
            first.plan.initial_route,
            affinity=first.plan.cache_affinity_context,
            request_id=first.attempt.request_id,
            trace_id=first.attempt.trace_id,
            attempt_sequence=first.attempt.attempt_sequence,
            routing_attempt_id=opened[1].attempt.routing_attempt_id,
            stream_session_id=None,
        )
    assert conflict.value.code == "gateway.cache_affinity_unavailable"

    connection = connect_database(state_graph.database_url)
    try:
        counts = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM cache_affinity_scopes WHERE tenant_id=?) AS scopes,
              (SELECT COUNT(*) FROM cache_affinity_bindings WHERE tenant_id=?) AS bindings,
              (SELECT COUNT(*) FROM cache_affinity_selections WHERE tenant_id=?) AS selections
            """,
            (state_graph.tenant_id, state_graph.tenant_id, state_graph.tenant_id),
        ).fetchone()
        binding = connection.execute(
            """
            SELECT active_selection_count, selection_count, estimated_hit_count
            FROM cache_affinity_bindings WHERE tenant_id=?
            """,
            (state_graph.tenant_id,),
        ).fetchone()
        selection_rows = connection.execute(
            """
            SELECT estimated_hit, binding_state_before, outcome_status
            FROM cache_affinity_selections WHERE tenant_id=? ORDER BY request_id
            """,
            (state_graph.tenant_id,),
        ).fetchall()
        assert tuple(counts) == (1, 1, request_count)
        assert tuple(binding) == (request_count, request_count, 0)
        assert {tuple(row) for row in selection_rows} <= {
            (0, "missing", "pending"),
            (0, "pending", "pending"),
        }
        assert len(selection_rows) == request_count
        assert tuple(state_before_reentry) == (
            request_count,
            request_count,
            request_count,
        )
        assert tuple(state_before_reentry) == (
            counts["selections"],
            binding["active_selection_count"],
            binding["selection_count"],
        )
    finally:
        connection.close()


def test_expired_lease_renews_and_late_success_warms_without_decrementing_new_lease(
    state_graph: StateGraph,
) -> None:
    """过期 lease 先同代续租，跨 lease 迟到成功只推进同 target warm 证据。

    第一阶段保留活动 Selection 并把 expiry 置为过去，路由 reservation 必须续租同一代。
    第二阶段模拟恢复重建后开启新 lease，再提交旧代成功；旧代不得扣减新代活动计数。
    """

    old = _open_selection(state_graph, 100)
    connection = connect_database(state_graph.database_url)
    try:
        before = connection.execute(
            "SELECT * FROM cache_affinity_bindings WHERE tenant_id=?",
            (state_graph.tenant_id,),
        ).fetchone()
        assert before is not None
        expired_at = _db_time(datetime.now(UTC) - timedelta(seconds=1))
        connection.execute(
            """
            UPDATE cache_affinity_bindings SET pending_lease_expires_at=?
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (expired_at, state_graph.tenant_id, before["cache_affinity_binding_id"]),
        )
        connection.commit()
        lease_generation = int(before["pending_lease_generation"])
    finally:
        connection.close()

    # 第二次只完成路由 reservation；active>0 时应延长同 generation，而非切断旧调用。
    request_id = "req_prd050_lease_renew"
    trace_id = "trc_prd050_lease_renew"
    _insert_claim(state_graph, request_id, trace_id)
    renewed_plan = resolve_model_route_plan(
        state_graph.config,
        tenant_id=state_graph.tenant_id,
        model_alias=PUBLIC_ALIAS,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=state_graph.virtual_key_id,
        request_kind="chat",
        payload=_long_payload(),
    )
    assert renewed_plan.cache_affinity_context is not None
    connection = connect_database(state_graph.database_url)
    try:
        renewed = connection.execute(
            "SELECT * FROM cache_affinity_bindings WHERE tenant_id=?",
            (state_graph.tenant_id,),
        ).fetchone()
        assert int(renewed["pending_lease_generation"]) == lease_generation
        assert affinity_routing._parse_db_time(renewed["pending_lease_expires_at"]) > datetime.now(
            UTC
        )

        # 模拟单节点恢复把旧活动计数归零后开启下一 lease；旧 Selection 事实仍不可删除。
        next_start = datetime.now(UTC)
        connection.execute(
            """
            UPDATE cache_affinity_bindings
            SET pending_lease_generation=pending_lease_generation+1,
                pending_lease_started_at=?, pending_lease_expires_at=?,
                active_selection_count=0, last_selected_at=?, updated_at=?
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (
                _db_time(next_start),
                _db_time(next_start + timedelta(seconds=30)),
                _db_time(next_start),
                _db_time(next_start),
                state_graph.tenant_id,
                renewed["cache_affinity_binding_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    current = _open_selection(state_graph, 101)
    late_at = datetime.now(UTC)
    _finish_attempt_parent(state_graph, old.attempt, succeeded=True, completed_at=late_at)
    assert safe_finalize_cache_affinity_selection(state_graph.config, old.handle, succeeded=True)
    connection = connect_database(state_graph.database_url)
    try:
        binding = connection.execute(
            "SELECT * FROM cache_affinity_bindings WHERE tenant_id=?",
            (state_graph.tenant_id,),
        ).fetchone()
        old_row = connection.execute(
            "SELECT outcome_status FROM cache_affinity_selections WHERE request_id=?",
            (old.attempt.request_id,),
        ).fetchone()
        current_row = connection.execute(
            """
            SELECT outcome_status, estimated_hit
            FROM cache_affinity_selections
            WHERE request_id=?
            """,
            (current.attempt.request_id,),
        ).fetchone()
        assert binding["last_success_at"] == _db_time(late_at)
        assert binding["active_selection_count"] == 1
        assert int(binding["pending_lease_generation"]) == lease_generation + 1
        assert old_row["outcome_status"] == "succeeded"
        assert tuple(current_row) == ("pending", 0)
    finally:
        connection.close()


def test_target_aba_late_success_cannot_warm_or_decrement_new_generation(
    state_graph: StateGraph,
) -> None:
    """A→B→A 后旧 A 成功不得污染新 A generation，也不得扣减新代计数。"""

    old = _open_selection(state_graph, 200)
    connection = connect_database(state_graph.database_url)
    try:
        binding = connection.execute(
            "SELECT * FROM cache_affinity_bindings WHERE tenant_id=?",
            (state_graph.tenant_id,),
        ).fetchone()
        old_generation = int(binding["target_generation"])
        b_time = datetime.now(UTC)
        binding, changed_to_b = affinity_keypool._apply_binding_target(
            connection,
            binding=binding,
            selected_key=None,
            target_kind="none",
            as_of=b_time,
        )
        # 返回 A 时仍用环境目标；generation 必须再次推进，不能仅比较当前 target 文本。
        binding, changed_back_to_a = affinity_keypool._apply_binding_target(
            connection,
            binding=binding,
            selected_key=SelectedProviderKey(None, "test-only", "environment", None),
            target_kind="environment",
            as_of=b_time,
        )
        connection.commit()
        assert changed_to_b and changed_back_to_a
        assert int(binding["target_generation"]) == old_generation + 2
    finally:
        connection.close()

    current = _open_selection(state_graph, 201)
    _finish_attempt_parent(
        state_graph,
        old.attempt,
        succeeded=True,
        completed_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    assert safe_finalize_cache_affinity_selection(state_graph.config, old.handle, succeeded=True)
    connection = connect_database(state_graph.database_url)
    try:
        binding = connection.execute(
            "SELECT * FROM cache_affinity_bindings WHERE tenant_id=?",
            (state_graph.tenant_id,),
        ).fetchone()
        pointer = connection.execute(
            "SELECT preferred_binding_id, preferred_lease_generation FROM cache_affinity_scopes",
        ).fetchone()
        assert int(binding["target_generation"]) == old_generation + 2
        assert binding["last_success_at"] is None
        assert binding["active_selection_count"] == 1
        assert pointer["preferred_binding_id"] == binding["cache_affinity_binding_id"]
        assert pointer["preferred_lease_generation"] == binding["pending_lease_generation"]
        assert current.handle is not None
    finally:
        connection.close()


def test_scope_cleanup_is_deterministic_and_never_processes_more_than_sixty_four(
    state_graph: StateGraph,
) -> None:
    """同时间 70 个空 Scope 按 ID 确定性 LRU 清理，单次严格最多处理 64 个。"""

    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for ordinal in range(1, 71):
            _insert_scope(connection, state_graph, ordinal)
        affinity_routing._cleanup_stale_scopes(
            connection,
            tenant_id=state_graph.tenant_id,
            as_of=BASE_TIME + timedelta(days=1),
        )
        remaining = connection.execute(
            """
            SELECT cache_affinity_scope_id FROM cache_affinity_scopes
            WHERE tenant_id=? ORDER BY cache_affinity_scope_id
            """,
            (state_graph.tenant_id,),
        ).fetchall()
        connection.commit()
        assert [row[0] for row in remaining] == [f"cafs_{value:032x}" for value in range(65, 71)]
    finally:
        connection.close()


def test_scope_capacity_fails_closed_when_every_lru_target_is_live(
    monkeypatch: pytest.MonkeyPatch,
    state_graph: StateGraph,
) -> None:
    """达到租户/Key Scope 上限且所有 lease 活跃时，创建新 Scope 必须失败关闭。"""

    monkeypatch.setattr(affinity_routing, "MAX_TENANT_SCOPES", 2)
    monkeypatch.setattr(affinity_routing, "MAX_VIRTUAL_KEY_SCOPES", 2)
    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for ordinal in (80, 81):
            scope_id, _digest = _insert_scope(connection, state_graph, ordinal)
            _insert_binding(
                connection,
                state_graph,
                scope_id=scope_id,
                ordinal=ordinal,
                lease_live=True,
                point_scope=True,
            )
        with pytest.raises(sqlite3.IntegrityError, match="scope capacity exhausted"):
            affinity_routing._ensure_scope(
                connection,
                tenant_id=state_graph.tenant_id,
                virtual_key_id=state_graph.virtual_key_id,
                scope_digest="f" * 64,
                existing=None,
                as_of=BASE_TIME + timedelta(days=1),
            )
        connection.rollback()
    finally:
        connection.close()


def test_eight_live_bindings_map_no_safe_eviction_to_fixed_503(
    state_graph: StateGraph,
) -> None:
    """单 Scope 八条 live Binding 无安全淘汰目标时，KeyPool 统一映射固定 503 错误。"""

    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        scope_id, scope_digest = _insert_scope(connection, state_graph, 90)
        for ordinal in range(90, 98):
            _insert_binding(
                connection,
                state_graph,
                scope_id=scope_id,
                ordinal=ordinal,
                lease_live=True,
                point_scope=ordinal == 90,
            )
        connection.commit()
    finally:
        connection.close()

    request_id = "req_prd050_capacity_503"
    trace_id = "trc_prd050_capacity_503"
    _insert_claim(state_graph, request_id, trace_id)
    route = _route_for_direct_state(state_graph)
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    attempt = create_routing_attempt(
        state_graph.config,
        plan,
        route,
        request_id=request_id,
        trace_id=trace_id,
        attempt_sequence=1,
        attempt_role="primary",
    )
    context = CacheAffinityRouteContext(
        tenant_id=state_graph.tenant_id,
        virtual_key_id=state_graph.virtual_key_id,
        cache_affinity_id=state_graph.affinity_id,
        cache_affinity_version=1,
        model_alias_id=state_graph.model_alias_id,
        ttl_seconds=3600,
        digest_schema_version=1,
        cache_affinity_scope_id=scope_id,
        cache_affinity_binding_id="cab_" + f"{90:032x}",
        model_endpoint_id=route.model_id,
        pending_lease_generation=1,
        endpoint_preference_applied=False,
        endpoint_preference_state="pending",
        scope_digest=scope_digest,
        endpoint_revision_digest="0" * 64,
    )
    with pytest.raises(ApiError) as captured:
        select_provider_access(
            state_graph.config,
            route,
            affinity=context,
            request_id=request_id,
            trace_id=trace_id,
            attempt_sequence=1,
            routing_attempt_id=attempt.routing_attempt_id,
            stream_session_id=None,
        )
    assert captured.value.code == "gateway.cache_affinity_unavailable"
    assert ERROR_SPECS[captured.value.code].http_status == 503


def test_parent_terminal_is_lazily_reconciled_in_calling_transaction(
    state_graph: StateGraph,
) -> None:
    """已具有父成功终态的 pending Selection 被惰性对账，并原子写 warm 与活动计数。"""

    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        scope_id, _digest = _insert_scope(connection, state_graph, 300)
        binding = _insert_binding(
            connection,
            state_graph,
            scope_id=scope_id,
            ordinal=300,
            point_scope=True,
        )
        request_id, _attempt_id = _insert_pending_selection_fact(
            connection,
            state_graph,
            binding,
            300,
            terminal="succeeded",
        )
        assert (
            reconcile_pending_cache_affinity_selections(
                connection,
                tenant_id=state_graph.tenant_id,
                cache_affinity_scope_id_snapshot=scope_id,
                limit=1,
            )
            == 1
        )
        selection = connection.execute(
            "SELECT outcome_status, outcome_at FROM cache_affinity_selections WHERE request_id=?",
            (request_id,),
        ).fetchone()
        refreshed = connection.execute(
            "SELECT active_selection_count, last_success_at FROM cache_affinity_bindings",
        ).fetchone()
        connection.commit()
        assert selection["outcome_status"] == "succeeded"
        assert selection["outcome_at"] == refreshed["last_success_at"]
        assert refreshed["active_selection_count"] == 0
    finally:
        connection.close()


def test_startup_recovery_reconciles_more_than_one_batch(
    state_graph: StateGraph,
) -> None:
    """130 条父终态跨三个 64 行批次全部收口，启动前不得残留 pending。"""

    fact_count = affinity_lifecycle.RECONCILE_BATCH_SIZE * 2 + 2
    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        scope_id, _digest = _insert_scope(connection, state_graph, 400)
        binding = _insert_binding(
            connection,
            state_graph,
            scope_id=scope_id,
            ordinal=400,
            point_scope=True,
        )
        for ordinal in range(1, fact_count + 1):
            _insert_pending_selection_fact(
                connection,
                state_graph,
                binding,
                400 + ordinal,
                terminal="succeeded",
            )
        connection.commit()
    finally:
        connection.close()

    summary = recover_cache_affinity_state(state_graph.config)
    assert summary == {
        "reconciled": fact_count,
        "interrupted_attempts": 0,
        "interrupted_streams": 0,
        "bindings_rebuilt": 1,
    }
    connection = connect_database(state_graph.database_url)
    try:
        pending = connection.execute(
            "SELECT COUNT(*) FROM cache_affinity_selections WHERE outcome_status='pending'"
        ).fetchone()[0]
        binding = connection.execute(
            """
            SELECT active_selection_count, selection_count, last_success_at
            FROM cache_affinity_bindings
            """
        ).fetchone()
        assert pending == 0
        assert tuple(binding[:2]) == (0, fact_count)
        assert binding["last_success_at"] is not None
    finally:
        connection.close()


def test_orphan_pending_selection_prevents_gateway_recovery(
    state_graph: StateGraph,
) -> None:
    """父 Attempt 被删除后的孤儿 pending 不得伪造失败终态，恢复必须阻止 ready。"""

    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        scope_id, _digest = _insert_scope(connection, state_graph, 600)
        binding = _insert_binding(
            connection,
            state_graph,
            scope_id=scope_id,
            ordinal=600,
            point_scope=True,
        )
        _request_id, attempt_id = _insert_pending_selection_fact(
            connection,
            state_graph,
            binding,
            600,
        )
        connection.execute(
            "DELETE FROM routing_attempts WHERE tenant_id=? AND routing_attempt_id=?",
            (state_graph.tenant_id, attempt_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="cache_affinity_startup_recovery_failed"):
        recover_cache_affinity_state(state_graph.config)
    connection = connect_database(state_graph.database_url)
    try:
        row = connection.execute("SELECT outcome_status FROM cache_affinity_selections").fetchone()
        assert row["outcome_status"] == "pending"
    finally:
        connection.close()


def test_injected_second_reconcile_batch_failure_blocks_recovery(
    monkeypatch: pytest.MonkeyPatch,
    state_graph: StateGraph,
) -> None:
    """第二个对账批次注入失败时，即使首批已提交也必须阻止实例进入 ready。"""

    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        scope_id, _digest = _insert_scope(connection, state_graph, 700)
        binding = _insert_binding(
            connection,
            state_graph,
            scope_id=scope_id,
            ordinal=700,
            point_scope=True,
        )
        for ordinal in range(affinity_lifecycle.RECONCILE_BATCH_SIZE + 1):
            _insert_pending_selection_fact(
                connection,
                state_graph,
                binding,
                700 + ordinal,
                terminal="succeeded",
            )
        connection.commit()
    finally:
        connection.close()

    original = affinity_lifecycle.reconcile_pending_cache_affinity_selections
    calls = 0

    def fail_second_batch(connection: sqlite3.Connection, *, limit: int = 64) -> int:
        """保留首批真实事务，第二批抛固定数据库错误以验证 ready 门禁。"""

        nonlocal calls
        calls += 1
        if calls == 2:
            # 固定在第二批失败，证明首批已提交后仍不能绕过 Gateway 的 ready 门禁。
            raise sqlite3.OperationalError("injected-reconcile-failure")
        return original(connection, limit=limit)

    monkeypatch.setattr(
        affinity_lifecycle,
        "reconcile_pending_cache_affinity_selections",
        fail_second_batch,
    )
    with pytest.raises(RuntimeError, match="cache_affinity_startup_recovery_failed"):
        recover_cache_affinity_state(state_graph.config)
    assert calls == 2
    connection = connect_database(state_graph.database_url)
    try:
        counts = connection.execute(
            """
            SELECT outcome_status, COUNT(*) AS amount FROM cache_affinity_selections
            GROUP BY outcome_status ORDER BY outcome_status
            """
        ).fetchall()
        assert [(row["outcome_status"], row["amount"]) for row in counts] == [
            ("pending", 1),
            ("succeeded", affinity_lifecycle.RECONCILE_BATCH_SIZE),
        ]
    finally:
        connection.close()


def test_rebuild_counts_only_current_target_and_lease_generation(
    state_graph: StateGraph,
) -> None:
    """活动重建只统计当前双 generation，并保留当前指针而忽略旧代 pending。"""

    connection = connect_database(state_graph.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        scope_id, _digest = _insert_scope(connection, state_graph, 900)
        binding = _insert_binding(
            connection,
            state_graph,
            scope_id=scope_id,
            ordinal=900,
            point_scope=True,
        )
        _insert_pending_selection_fact(connection, state_graph, binding, 900)
        # target 变更会同时推进 target/lease 并归零旧代计数，旧 Selection 仍保留审计事实。
        binding, _changed = affinity_keypool._apply_binding_target(
            connection,
            binding=binding,
            selected_key=None,
            target_kind="none",
            as_of=BASE_TIME + timedelta(seconds=900),
        )
        connection.execute(
            """
            UPDATE cache_affinity_scopes
            SET preferred_binding_id=?, preferred_lease_generation=?,
                last_selected_at=?, updated_at=?
            WHERE tenant_id=? AND cache_affinity_scope_id=?
            """,
            (
                binding["cache_affinity_binding_id"],
                binding["pending_lease_generation"],
                _db_time(BASE_TIME + timedelta(seconds=900)),
                _db_time(BASE_TIME + timedelta(seconds=900)),
                state_graph.tenant_id,
                scope_id,
            ),
        )
        _insert_pending_selection_fact(connection, state_graph, binding, 901)
        # 人为放大计数模拟崩溃中断；恢复必须以当前双代 Selection 为唯一真相重算为 1。
        connection.execute(
            """
            UPDATE cache_affinity_bindings SET active_selection_count=7
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (state_graph.tenant_id, binding["cache_affinity_binding_id"]),
        )
        assert _rebuild_binding_activity(connection) == 1
        rebuilt = connection.execute(
            "SELECT active_selection_count FROM cache_affinity_bindings"
        ).fetchone()
        pointer = connection.execute(
            "SELECT preferred_binding_id, preferred_lease_generation FROM cache_affinity_scopes"
        ).fetchone()
        connection.commit()
        assert rebuilt["active_selection_count"] == 1
        assert pointer["preferred_binding_id"] == binding["cache_affinity_binding_id"]
        assert pointer["preferred_lease_generation"] == binding["pending_lease_generation"]
    finally:
        connection.close()
