"""在 RoutingPolicy 冻结事务内执行 Prompt Cache endpoint reservation。

本模块只保存租户限定的不可逆摘要和短租约，不接收凭据明文；任何状态图损坏都必须在
Provider 调用前失败关闭，不能静默退回无审计路由。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from metahub_shared.db.security import decrypt_provider_key
from metahub_shared.runtime.cache_affinity import (
    CacheAffinityPreferenceOutcome,
    apply_cache_affinity_endpoint_preference,
    build_cache_affinity_key_digest,
    build_cache_affinity_request_context,
    build_endpoint_revision_digest,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.routing_policy import RoutingSelectionResult

from .lifecycle import reconcile_pending_cache_affinity_selections
from .models import CacheAffinityRouteContext, CacheAffinityRouteFacts

# 短租约只协调首批并发，不表示 Provider 已建立缓存。测试通过注入 ``as_of`` 控制边界。
PENDING_LEASE_SECONDS = 30
# 容量上限同时约束单 Key 与租户总量，防止通过大量 Virtual Key 放大 Prompt 指纹存储。
MAX_TENANT_SCOPES = 100_000
MAX_VIRTUAL_KEY_SCOPES = 10_000
MAX_BINDINGS_PER_SCOPE = 8
LAZY_CLEANUP_LIMIT = 64
MAX_WARM_RETENTION_SECONDS = 86_400


def prepare_cache_affinity_route(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str | None,
    model_alias_id: str,
    request_kind: str,
    payload: dict[str, Any],
    base_selection: RoutingSelectionResult,
    candidate_rows: dict[str, sqlite3.Row],
    as_of: datetime,
) -> CacheAffinityRouteFacts:
    """读取 Alias 配置、应用 endpoint 偏好并提交前构造 reservation。

    参数中的连接必须已经处于调用方 ``BEGIN IMMEDIATE`` 事务，候选和 Alias 均属于同一
    tenant。返回值只含安全 summary 和内部上下文；配置缺失、短前缀或非 Chat 请求返回原
    selection。任何数据库图损坏抛出 ``ValueError/sqlite3.Error``，由路由层统一失败关闭。
    """

    # 三个标识共同限定亲和事实的最小可信作用域；任一缺失都必须在查询和 pending
    # 对账前零副作用返回，避免不完整上下文误触其他租户、Key 或 Alias 的状态。
    if not tenant_id or not virtual_key_id or not model_alias_id:
        return CacheAffinityRouteFacts(base_selection, None, None)
    config_row = connection.execute(
        """
        SELECT cache_affinity_id, tenant_id, model_alias_id, status, data_class,
               minimum_prefix_chars, ttl_seconds, version
        FROM cache_affinities
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (tenant_id, model_alias_id),
    ).fetchone()
    if config_row is None or str(config_row["status"]) != "enabled":
        return CacheAffinityRouteFacts(base_selection, None, None)
    _validate_cache_affinity_config(config_row, tenant_id=tenant_id, model_alias_id=model_alias_id)
    request_context = build_cache_affinity_request_context(
        config.cache_affinity_hmac_secret,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        data_class=str(config_row["data_class"]),
        request_kind=request_kind,
        payload=payload,
        minimum_prefix_chars=int(config_row["minimum_prefix_chars"]),
    )
    if request_context is None:
        return CacheAffinityRouteFacts(base_selection, None, None)

    endpoint_revisions = {
        model_id: _endpoint_revision(config, tenant_id=tenant_id, row=row)
        for model_id, row in candidate_rows.items()
    }
    scope_row = _load_scope(
        connection,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_digest=request_context.affinity_scope_digest,
    )
    if scope_row is not None:
        # Alias 不属于五维缓存域；同域的其他 Alias 也可能留下父终态 pending，因此请求期
        # 必须按 tenant + Scope 收口。Scope 尚不存在时不得退化成租户或全库扫描。
        reconcile_pending_cache_affinity_selections(
            connection,
            tenant_id=tenant_id,
            cache_affinity_scope_id_snapshot=str(scope_row["cache_affinity_scope_id"]),
        )
        # 对账失败终态可能清除 Scope 指针；重新读取可避免继续使用对账前的陈旧偏好快照。
        scope_row = _load_scope(
            connection,
            tenant_id=tenant_id,
            virtual_key_id=virtual_key_id,
            scope_digest=request_context.affinity_scope_digest,
        )
        if scope_row is None:  # pragma: no cover - 同一写事务内 Scope 不会被并发删除。
            # 无法读回既有 Scope 表示数据库图损坏，必须在 Provider 前失败关闭。
            raise sqlite3.IntegrityError("cache affinity scope disappeared")
    binding_rows = _load_candidate_bindings(
        connection,
        tenant_id=tenant_id,
        scope_id=str(scope_row["cache_affinity_scope_id"]) if scope_row else None,
        endpoint_revisions=endpoint_revisions,
    )
    ttl_seconds = int(config_row["ttl_seconds"])
    preferred = _choose_endpoint_preference(
        config,
        connection,
        scope_row=scope_row,
        binding_rows=binding_rows,
        selection=base_selection,
        candidate_rows=candidate_rows,
        endpoint_revisions=endpoint_revisions,
        ttl_seconds=ttl_seconds,
        as_of=as_of,
    )
    outcome: CacheAffinityPreferenceOutcome | None = None
    final_selection = base_selection
    if preferred is not None and base_selection.routing_mode != "fixed":
        preferred_model_id, preferred_provider_id, preference_state = preferred
        outcome = apply_cache_affinity_endpoint_preference(
            base_selection,
            preferred_model_id=preferred_model_id,
            preferred_provider_id=preferred_provider_id,
            preference_state=preference_state,
        )
        final_selection = outcome.selection

    selected = next(
        (item for item in final_selection.candidate_results if item.status == "selected"),
        None,
    )
    if selected is None:
        # 无可用候选仍由既有路由错误处理；不能为失败 Decision 制造亲和运行状态。
        return CacheAffinityRouteFacts(
            final_selection, None, _preference_summary(config_row, outcome)
        )
    selected_model_id = selected.candidate.model_id
    selected_row = candidate_rows[selected_model_id]
    endpoint_revision = endpoint_revisions[selected_model_id]
    scope_row = _ensure_scope(
        connection,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_digest=request_context.affinity_scope_digest,
        existing=scope_row,
        as_of=as_of,
    )
    binding_row = _ensure_binding_reservation(
        config,
        connection,
        tenant_id=tenant_id,
        scope_row=scope_row,
        scope_digest=request_context.affinity_scope_digest,
        model_id=selected_model_id,
        row=selected_row,
        candidate=selected.candidate,
        endpoint_revision=endpoint_revision,
        ttl_seconds=ttl_seconds,
        as_of=as_of,
    )
    selected_state = _binding_state_for_request(
        config,
        connection,
        binding_row,
        candidate=selected.candidate,
        ttl_seconds=ttl_seconds,
        as_of=as_of,
    )
    _point_scope_to_binding(connection, scope_row=scope_row, binding_row=binding_row, as_of=as_of)
    context = CacheAffinityRouteContext(
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        cache_affinity_id=str(config_row["cache_affinity_id"]),
        cache_affinity_version=int(config_row["version"]),
        model_alias_id=model_alias_id,
        ttl_seconds=ttl_seconds,
        digest_schema_version=1,
        cache_affinity_scope_id=str(scope_row["cache_affinity_scope_id"]),
        cache_affinity_binding_id=str(binding_row["cache_affinity_binding_id"]),
        model_endpoint_id=selected_model_id,
        pending_lease_generation=int(binding_row["pending_lease_generation"]),
        endpoint_preference_applied=bool(outcome and outcome.action == "reordered"),
        endpoint_preference_state=selected_state
        if selected_state in {"pending", "warm"}
        else "pending",
        scope_digest=request_context.affinity_scope_digest,
        endpoint_revision_digest=endpoint_revision,
    )
    return CacheAffinityRouteFacts(
        selection=final_selection,
        context=context,
        candidate_summary=_preference_summary(config_row, outcome, selected_state=selected_state),
    )


def endpoint_revision_for_route(config: AppConfig, route: object) -> str:
    """根据已冻结 GatewayRoute 计算同一 endpoint revision，供 Fallback KeyPool 使用。

    该函数只读取 route 的服务端字段，不接受客户端 metadata；返回 HMAC 摘要且不得记录。
    """

    return build_endpoint_revision_digest(
        config.cache_affinity_hmac_secret,
        tenant_id=str(route.tenant_id),
        model_endpoint_id=str(route.model_id),
        provider_id=str(route.provider_id),
        provider_type=str(route.provider_type),
        auth_type=str(route.provider_auth_type),
        base_url=str(route.provider_base_url),
        provider_model_id=str(route.upstream_model_name),
    )


def _validate_cache_affinity_config(
    row: sqlite3.Row,
    *,
    tenant_id: str,
    model_alias_id: str,
) -> None:
    """严格验证事务内配置快照，损坏行不能降级成“未启用”。"""

    # 配置行必须同时属于可信 tenant 与当前 Alias，防止损坏外键图跨域复用运行状态。
    if str(row["tenant_id"]) != tenant_id or str(row["model_alias_id"]) != model_alias_id:
        # 所有权图异常属于服务端一致性故障，不能回显实际对象或尝试使用该配置。
        raise ValueError("cache affinity owner graph invalid")
    # data_class 是五维隔离域的一部分，空值会把原本不同敏感等级错误合并。
    if not row["data_class"]:
        raise ValueError("cache affinity data class invalid")
    minimum = row["minimum_prefix_chars"]
    ttl = row["ttl_seconds"]
    version = row["version"]
    # bool 也是 int 子类，因此必须用精确类型和设计上下限共同拒绝畸形门限。
    if type(minimum) is not int or not 256 <= minimum <= 16_384:
        raise ValueError("cache affinity minimum prefix invalid")
    # TTL 直接决定 warm 证据解释，超出合同的持久值不能被运行时静默裁剪。
    if type(ttl) is not int or not 60 <= ttl <= 86_400:
        raise ValueError("cache affinity ttl invalid")
    # 配置版本参与两事务重验，非正整数会破坏 CAS 与历史 Selection 解释。
    if type(version) is not int or version < 1:
        raise ValueError("cache affinity version invalid")


def _endpoint_revision(config: AppConfig, *, tenant_id: str, row: sqlite3.Row) -> str:
    """从同一候选快照派生 endpoint revision，避免 model_id 原地变更复用旧缓存域。"""

    return build_endpoint_revision_digest(
        config.cache_affinity_hmac_secret,
        tenant_id=tenant_id,
        model_endpoint_id=str(row["model_id"]),
        provider_id=str(row["provider_id"]),
        provider_type=str(row["provider_type"]),
        auth_type=str(row["auth_type"]),
        base_url=str(row["base_url"]),
        provider_model_id=str(row["upstream_model_name"]),
    )


def _load_scope(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    scope_digest: str,
) -> sqlite3.Row | None:
    """按完整租户、Virtual Key 与摘要读取唯一 scope。"""

    return connection.execute(
        """
        SELECT * FROM cache_affinity_scopes
        WHERE tenant_id=? AND virtual_key_id=? AND digest_schema_version=1
          AND affinity_scope_digest=?
        """,
        (tenant_id, virtual_key_id, scope_digest),
    ).fetchone()


def _load_candidate_bindings(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scope_id: str | None,
    endpoint_revisions: dict[str, str],
) -> tuple[sqlite3.Row, ...]:
    """读取 scope 下候选 endpoint 对应的全部 Binding，不扫描其他 Prompt 域。"""

    if not scope_id or not endpoint_revisions:
        return ()
    rows = connection.execute(
        """
        SELECT * FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        ORDER BY last_selected_at DESC, cache_affinity_binding_id
        """,
        (tenant_id, scope_id),
    ).fetchall()
    return tuple(
        row
        for row in rows
        if endpoint_revisions.get(str(row["model_endpoint_id"]))
        == str(row["endpoint_revision_digest"])
    )


def _choose_endpoint_preference(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    scope_row: sqlite3.Row | None,
    binding_rows: tuple[sqlite3.Row, ...],
    selection: RoutingSelectionResult,
    candidate_rows: dict[str, sqlite3.Row],
    endpoint_revisions: dict[str, str],
    ttl_seconds: int,
    as_of: datetime,
) -> tuple[str, str, str] | None:
    """按 pointer、warm 时间和 pending 活动顺序选择一个兼容 endpoint 偏好。"""

    if selection.status != "selected" or not binding_rows:
        return None
    eligible = {
        item.candidate.model_id: item.candidate
        for item in selection.candidate_results
        if item.status in {"selected", "standby"} and item.rank is not None
    }
    assessed: list[tuple[sqlite3.Row, str]] = []
    for binding in binding_rows:
        model_id = str(binding["model_endpoint_id"])
        candidate = eligible.get(model_id)
        if candidate is None or endpoint_revisions.get(model_id) != str(
            binding["endpoint_revision_digest"]
        ):
            continue
        state = _binding_state_for_request(
            config,
            connection,
            binding,
            candidate=candidate,
            ttl_seconds=ttl_seconds,
            as_of=as_of,
        )
        if state in {"warm", "pending"}:
            assessed.append((binding, state))
    if not assessed:
        return None
    pointer_id = str(scope_row["preferred_binding_id"] or "") if scope_row else ""
    pointer_generation = (
        int(scope_row["preferred_lease_generation"])
        if scope_row and scope_row["preferred_lease_generation"] is not None
        else None
    )
    for binding, state in assessed:
        if (
            str(binding["cache_affinity_binding_id"]) == pointer_id
            and int(binding["pending_lease_generation"]) == pointer_generation
        ):
            return (
                str(binding["model_endpoint_id"]),
                str(binding["provider_id"]),
                state,
            )
    assessed.sort(key=lambda item: _binding_preference_sort_key(item[0], item[1]))
    winner, state = assessed[0]
    return str(winner["model_endpoint_id"]), str(winner["provider_id"]), state


def _binding_preference_sort_key(row: sqlite3.Row, state: str) -> tuple[int, str, str, str]:
    """生成跨 Binding 稳定排序键；warm 证据始终先于 pending reservation。"""

    return (
        0 if state == "warm" else 1,
        _reverse_time_key(row["last_success_at"] if state == "warm" else row["last_selected_at"]),
        str(row["model_endpoint_id"]),
        str(row["cache_affinity_binding_id"]),
    )


def _reverse_time_key(value: object) -> str:
    """把规范 UTC 秒时间转换为降序字典键，空时间稳定排到最后。"""

    if not value:
        return "9"
    timestamp = int(_parse_db_time(value).timestamp())
    return f"{9_999_999_999 - timestamp:010d}"


def _binding_state_for_request(
    config: AppConfig,
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    candidate: object,
    ttl_seconds: int,
    as_of: datetime,
) -> str:
    """派生当前请求的 warm/pending/expired/ineligible，不回写共享 TTL。"""

    if not _binding_target_is_selectable(config, connection, row, candidate=candidate, as_of=as_of):
        return "ineligible"
    last_success_at = _parse_optional_db_time(row["last_success_at"])
    if last_success_at is not None and as_of < last_success_at + timedelta(seconds=ttl_seconds):
        return "warm"
    lease_expires_at = _parse_optional_db_time(row["pending_lease_expires_at"])
    if (
        lease_expires_at is not None
        and as_of < lease_expires_at
        or int(row["active_selection_count"] or 0) > 0
    ):
        return "pending"
    return "expired"


def _binding_target_is_selectable(
    config: AppConfig,
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    candidate: object,
    as_of: datetime,
) -> bool:
    """验证 Binding target 未越过当前候选和 KeyPool 硬约束。"""

    # Binding 的 Provider 必须仍等于当前候选，旧 Provider 不能凭历史 warm 证据被升格。
    if str(row["provider_id"]) != str(candidate.provider_id):
        return False
    target_kind = str(row["target_kind"])
    if target_kind == "unassigned":
        return str(candidate.keypool_state) != "unavailable"
    if target_kind in {"environment", "none"}:
        return str(candidate.keypool_state) != "unavailable"
    # 只有带当前 Credential 快照的 managed target 才能继续做数据库资格和解密重验。
    if target_kind != "managed" or not row["provider_credential_id"]:
        return False
    credential = connection.execute(
        """
        SELECT provider_credential_id, encrypted_key, status, rate_limited_until,
               probe_health_status, probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=? AND provider_credential_id=?
        """,
        (row["tenant_id"], row["provider_id"], row["provider_credential_id"]),
    ).fetchone()
    # 删除、跨 Provider 或旧占位密文都使目标失格；这里只返回布尔，不能记录 Credential。
    if credential is None or not str(credential["encrypted_key"] or "").startswith("aesgcm-v1:"):
        return False
    # 局部导入避免 routing.models -> cache_affinity -> keypool -> routing.models 的加载期循环。
    from metahub_gateway.providers.keypool import provider_key_row_is_selectable

    if not provider_key_row_is_selectable(
        credential,
        stale_seconds=config.provider_health_stale_seconds,
        now=as_of,
    ):
        return False
    try:
        # 亲和升格发生在 Decision 提交前，必须证明旧 Key 真能解密；只看密文前缀会让损坏
        # Credential 把本可成功的其他 endpoint 降格。明文只在当前栈短暂存在且绝不记录。
        plain_value = decrypt_provider_key(
            str(credential["encrypted_key"]), encryption_secret=config.provider_key_encryption_secret,
        )
    except ValueError:
        return False
    selectable = bool(plain_value)
    del plain_value
    return selectable


def _ensure_scope(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    scope_digest: str,
    existing: sqlite3.Row | None,
    as_of: datetime,
) -> sqlite3.Row:
    """创建唯一 scope；容量满时只淘汰可证明不活跃的运行状态。"""

    if existing is not None:
        return existing
    _enforce_scope_capacity(
        connection, tenant_id=tenant_id, virtual_key_id=virtual_key_id, as_of=as_of
    )
    now = _to_db_time(as_of)
    scope_id = f"cafs_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO cache_affinity_scopes(
          cache_affinity_scope_id, tenant_id, virtual_key_id, digest_schema_version,
          affinity_scope_digest, preferred_binding_id, preferred_lease_generation,
          last_selected_at, created_at, updated_at
        ) VALUES(?, ?, ?, 1, ?, NULL, NULL, ?, ?, ?)
        """,
        (scope_id, tenant_id, virtual_key_id, scope_digest, now, now, now),
    )
    row = connection.execute(
        "SELECT * FROM cache_affinity_scopes WHERE tenant_id=? AND cache_affinity_scope_id=?",
        (tenant_id, scope_id),
    ).fetchone()
    # INSERT 后必须在同一 tenant 事务内读回唯一行，否则禁止后续创建悬空 Binding。
    if row is None:
        raise sqlite3.IntegrityError("cache affinity scope insert unavailable")
    return row


def _ensure_binding_reservation(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scope_row: sqlite3.Row,
    scope_digest: str,
    model_id: str,
    row: sqlite3.Row,
    candidate: object,
    endpoint_revision: str,
    ttl_seconds: int,
    as_of: datetime,
) -> sqlite3.Row:
    """创建或续租完整 endpoint Binding，绝不覆盖同 scope 的其他 endpoint。"""

    scope_id = str(scope_row["cache_affinity_scope_id"])
    binding = connection.execute(
        """
        SELECT * FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_scope_id=? AND model_endpoint_id=?
          AND endpoint_revision_digest=?
        """,
        (tenant_id, scope_id, model_id, endpoint_revision),
    ).fetchone()
    now = _to_db_time(as_of)
    lease_expires = _to_db_time(as_of + timedelta(seconds=PENDING_LEASE_SECONDS))
    if binding is None:
        _enforce_binding_capacity(connection, tenant_id=tenant_id, scope_id=scope_id, as_of=as_of)
        binding_id = f"cab_{uuid4().hex}"
        key_digest = build_cache_affinity_key_digest(
            config.cache_affinity_hmac_secret,
            affinity_scope_digest=scope_digest,
            model_endpoint_id=model_id,
            endpoint_revision_digest=endpoint_revision,
        )
        connection.execute(
            """
            INSERT INTO cache_affinity_bindings(
              cache_affinity_binding_id, tenant_id, cache_affinity_scope_id,
              affinity_key_digest, model_endpoint_id, endpoint_revision_digest,
              provider_id, provider_credential_id, target_kind, target_generation,
              pending_lease_generation, pending_lease_started_at, pending_lease_expires_at,
              active_selection_count, last_success_at, selection_count,
              estimated_hit_count, last_selected_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL, 'unassigned', 1, 1, ?, ?, 0,
                     NULL, 0, 0, ?, ?, ?)
            """,
            (
                binding_id,
                tenant_id,
                scope_id,
                key_digest,
                model_id,
                endpoint_revision,
                row["provider_id"],
                now,
                lease_expires,
                now,
                now,
                now,
            ),
        )
    else:
        # 当前请求 TTL 已过期且本代没有活动调用时开启新周期；不能清空共享 last_success_at，
        # 因为另一个较长 TTL Alias 可能仍把同一成功证据解释为 warm。
        warm = (
            _binding_state_for_request(
                config,
                connection,
                binding,
                candidate=candidate,
                ttl_seconds=ttl_seconds,
                as_of=as_of,
            )
            == "warm"
        )
        lease_expires_at = _parse_optional_db_time(binding["pending_lease_expires_at"])
        lease_live = lease_expires_at is not None and as_of < lease_expires_at
        active = int(binding["active_selection_count"] or 0)
        if not warm and not lease_live and active == 0:
            connection.execute(
                """
                UPDATE cache_affinity_bindings
                SET pending_lease_generation=pending_lease_generation+1,
                    pending_lease_started_at=?, pending_lease_expires_at=?,
                    active_selection_count=0, last_selected_at=?, updated_at=?
                WHERE tenant_id=? AND cache_affinity_binding_id=?
                """,
                (now, lease_expires, now, now, tenant_id, binding["cache_affinity_binding_id"]),
            )
        elif not warm and active > 0 and not lease_live:
            # 长调用超过短租约时只延长同 generation，旧 Selection 仍能正确结算当前计数。
            connection.execute(
                """
                UPDATE cache_affinity_bindings
                SET pending_lease_expires_at=?, last_selected_at=?, updated_at=?
                WHERE tenant_id=? AND cache_affinity_binding_id=?
                """,
                (lease_expires, now, now, tenant_id, binding["cache_affinity_binding_id"]),
            )
    refreshed = connection.execute(
        """
        SELECT * FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_scope_id=? AND model_endpoint_id=?
          AND endpoint_revision_digest=?
        """,
        (tenant_id, scope_id, model_id, endpoint_revision),
    ).fetchone()
    # Binding 是 Selection 的执行前父事实，创建或续租后读不回时必须整体回滚。
    if refreshed is None:
        raise sqlite3.IntegrityError("cache affinity binding unavailable")
    return refreshed


def _point_scope_to_binding(
    connection: sqlite3.Connection,
    *,
    scope_row: sqlite3.Row,
    binding_row: sqlite3.Row,
    as_of: datetime,
) -> None:
    """以 Binding + lease generation 原子更新短期偏好指针。

    指针与 generation 必须同写，避免旧 lease 的迟到终态清除并发建立的新偏好。
    """

    now = _to_db_time(as_of)
    cursor = connection.execute(
        """
        UPDATE cache_affinity_scopes
        SET preferred_binding_id=?, preferred_lease_generation=?,
            last_selected_at=?, updated_at=?
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        """,
        (
            binding_row["cache_affinity_binding_id"],
            binding_row["pending_lease_generation"],
            now,
            now,
            scope_row["tenant_id"],
            scope_row["cache_affinity_scope_id"],
        ),
    )
    # 租户内 Scope 必须恰好命中一行，零行代表 reservation 图已并发失效或损坏。
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("cache affinity scope pointer unavailable")


def _preference_summary(
    config_row: sqlite3.Row,
    outcome: CacheAffinityPreferenceOutcome | None,
    *,
    selected_state: str | None = None,
) -> dict[str, object]:
    """构造可持久化到 candidate_summary 的固定安全白名单。"""

    summary: dict[str, object] = {
        "evaluated": True,
        "cache_affinity_id": str(config_row["cache_affinity_id"]),
        "cache_affinity_version": int(config_row["version"]),
        "endpoint_preference_applied": bool(outcome and outcome.action == "reordered"),
        "preference_state": selected_state or (outcome.preference_state if outcome else "pending"),
        "action": outcome.action if outcome else "already_selected",
    }
    if outcome is not None:
        summary.update(
            {
                "reason_code": outcome.selection.selection_reason,
                "base_selection_reason": outcome.base_selection_reason,
                "base_selected_model_id": outcome.base_selected_model_id,
                "candidate_transitions": [asdict(item) for item in outcome.candidate_transitions],
            }
        )
    return summary


def _enforce_scope_capacity(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    as_of: datetime,
) -> None:
    """惰性回收安全 Scope，并在无可淘汰目标时失败关闭。"""

    _cleanup_stale_scopes(connection, tenant_id=tenant_id, as_of=as_of)
    tenant_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM cache_affinity_scopes WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0]
    )
    key_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM cache_affinity_scopes WHERE tenant_id=? AND virtual_key_id=?",
            (tenant_id, virtual_key_id),
        ).fetchone()[0]
    )
    # 清理至多 64 个确定性 LRU 候选后仍达任一上限时必须拒绝新 Scope，不能突破
    # tenant 或 Virtual Key 配额，也不能删除仍有 lease/warm/Selection 证据的状态。
    if tenant_count >= MAX_TENANT_SCOPES or key_count >= MAX_VIRTUAL_KEY_SCOPES:
        raise sqlite3.IntegrityError("cache affinity scope capacity exhausted")


def _enforce_binding_capacity(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scope_id: str,
    as_of: datetime,
) -> None:
    """限制单 scope endpoint 数量，优先删除最旧且无活动 Selection 的 Binding。"""

    count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM cache_affinity_bindings
            WHERE tenant_id=? AND cache_affinity_scope_id=?
            """,
            (tenant_id, scope_id),
        ).fetchone()[0]
    )
    if count < MAX_BINDINGS_PER_SCOPE:
        return
    _delete_oldest_safe_binding(connection, tenant_id=tenant_id, scope_id=scope_id, as_of=as_of)
    remaining = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM cache_affinity_bindings
            WHERE tenant_id=? AND cache_affinity_scope_id=?
            """,
            (tenant_id, scope_id),
        ).fetchone()[0]
    )
    # 没有可安全淘汰的 Binding 时固定失败关闭，禁止第九个 endpoint 绕过容量合同。
    if remaining >= MAX_BINDINGS_PER_SCOPE:
        raise sqlite3.IntegrityError("cache affinity binding capacity exhausted")


def _cleanup_stale_scopes(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    as_of: datetime,
) -> None:
    """按确定性 LRU 顺序处理至多 64 个 Scope，严格执行清指针、删 Binding、删 Scope。"""

    scope_rows = connection.execute(
        """
        SELECT cache_affinity_scope_id
        FROM cache_affinity_scopes
        WHERE tenant_id=?
        ORDER BY last_selected_at, cache_affinity_scope_id
        LIMIT ?
        """,
        (tenant_id, LAZY_CLEANUP_LIMIT),
    ).fetchall()
    for scope in scope_rows:
        scope_id = str(scope["cache_affinity_scope_id"])
        # 指针只指向可淘汰运行状态；先清除可证明过期或已经悬空的目标，满足删除触发器顺序。
        pointer = connection.execute(
            """
            SELECT b.* FROM cache_affinity_scopes s
            LEFT JOIN cache_affinity_bindings b
              ON b.tenant_id=s.tenant_id AND b.cache_affinity_binding_id=s.preferred_binding_id
            WHERE s.tenant_id=? AND s.cache_affinity_scope_id=?
            """,
            (tenant_id, scope_id),
        ).fetchone()
        if pointer is None or _binding_is_stale_for_cleanup(connection, pointer, as_of=as_of):
            connection.execute(
                """
                UPDATE cache_affinity_scopes
                SET preferred_binding_id=NULL, preferred_lease_generation=NULL, updated_at=?
                WHERE tenant_id=? AND cache_affinity_scope_id=?
                """,
                (_to_db_time(as_of), tenant_id, scope_id),
            )
        bindings = connection.execute(
            """
            SELECT * FROM cache_affinity_bindings
            WHERE tenant_id=? AND cache_affinity_scope_id=?
            ORDER BY last_selected_at, cache_affinity_binding_id
            """,
            (tenant_id, scope_id),
        ).fetchall()
        for binding in bindings:
            if _binding_is_stale_for_cleanup(connection, binding, as_of=as_of):
                connection.execute(
                    """
                    DELETE FROM cache_affinity_bindings
                    WHERE tenant_id=? AND cache_affinity_binding_id=?
                      AND NOT EXISTS (
                        SELECT 1 FROM cache_affinity_scopes s
                        WHERE s.tenant_id=?
                          AND s.preferred_binding_id =
                              cache_affinity_bindings.cache_affinity_binding_id
                      )
                    """,
                    (
                        tenant_id,
                        binding["cache_affinity_binding_id"],
                        tenant_id,
                    ),
                )
        connection.execute(
            """
            DELETE FROM cache_affinity_scopes
            WHERE tenant_id=? AND cache_affinity_scope_id=?
              AND NOT EXISTS (
                SELECT 1 FROM cache_affinity_bindings b
                WHERE b.tenant_id=?
                  AND b.cache_affinity_scope_id=cache_affinity_scopes.cache_affinity_scope_id
              )
            """,
            (tenant_id, scope_id, tenant_id),
        )


def _delete_oldest_safe_binding(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scope_id: str,
    as_of: datetime,
) -> None:
    """删除单 scope 最旧安全 Binding；当前 pointer 必须先显式清除。"""

    rows = connection.execute(
        """
        SELECT * FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        ORDER BY last_selected_at, cache_affinity_binding_id
        """,
        (tenant_id, scope_id),
    ).fetchall()
    for row in rows:
        if not _binding_is_stale_for_cleanup(connection, row, as_of=as_of):
            continue
        connection.execute(
            """
            UPDATE cache_affinity_scopes
            SET preferred_binding_id=NULL, preferred_lease_generation=NULL, updated_at=?
            WHERE tenant_id=? AND cache_affinity_scope_id=? AND preferred_binding_id=?
            """,
            (_to_db_time(as_of), tenant_id, scope_id, row["cache_affinity_binding_id"]),
        )
        connection.execute(
            "DELETE FROM cache_affinity_bindings WHERE tenant_id=? AND cache_affinity_binding_id=?",
            (tenant_id, row["cache_affinity_binding_id"]),
        )
        return


def _binding_is_stale_for_cleanup(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    as_of: datetime,
) -> bool:
    """仅当 lease、Selection 和最长 TTL 证据全部失效时允许清理 Binding。"""

    lease_expires = _parse_optional_db_time(row["pending_lease_expires_at"])
    if lease_expires is not None and as_of < lease_expires:
        return False
    pending = connection.execute(
        """
        SELECT 1 FROM cache_affinity_selections
        WHERE tenant_id=? AND cache_affinity_binding_id_snapshot=? AND outcome_status='pending'
        LIMIT 1
        """,
        (row["tenant_id"], row["cache_affinity_binding_id"]),
    ).fetchone()
    if pending is not None:
        return False
    last_success = _parse_optional_db_time(row["last_success_at"])
    return last_success is None or as_of >= last_success + timedelta(
        seconds=MAX_WARM_RETENTION_SECONDS
    )


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析可空 UTC 数据库时间；坏时间属于状态图损坏并抛出 ValueError。"""

    if value is None or value == "":
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """严格解析 UTC 时间并统一到有时区 datetime。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """转换为仓库统一的 UTC 秒级数据库格式。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
