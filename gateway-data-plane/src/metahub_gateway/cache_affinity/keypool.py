"""Prompt Cache 亲和请求的 KeyPool 选择与 Selection 原子提交。"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import decrypt_provider_key
from metahub_shared.runtime.cache_affinity import build_cache_affinity_key_digest
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_gateway.providers.keypool import (
    ProviderKeyDecryptAnomaly,
    SelectedProviderKey,
    _environment_provider_key,
    _insert_provider_key_anomaly,
    _row_is_selectable,
    _selection_sort_key,
    persist_provider_key_decrypt_anomaly,
    select_provider_key,
)
from metahub_gateway.routing.models import GatewayRoute

from .lifecycle import reconcile_pending_cache_affinity_selections
from .models import CacheAffinityRouteContext, CacheAffinitySelectionHandle
from .routing import PENDING_LEASE_SECONDS, _enforce_binding_capacity, endpoint_revision_for_route

logger = logging.getLogger("metahub.gateway.cache_affinity")


@dataclass(frozen=True, slots=True)
class ProviderAccessSelection:
    """返回已提交事务对应的 Provider Key、Selection 句柄和一次性事件。

    ``selected_key`` 可能含仅供 Adapter 使用的明文，因此整个结果禁止进入日志；无亲和配置
    时 handle / event 为空并完全复用旧 KeyPool 行为。
    """

    selected_key: SelectedProviderKey | None = field(repr=False)
    cache_affinity_selection: CacheAffinitySelectionHandle | None
    cache_affinity_event: WebhookEvent | None


def select_provider_access(
    config: AppConfig,
    route: GatewayRoute,
    *,
    affinity: CacheAffinityRouteContext | None,
    request_id: str,
    trace_id: str,
    attempt_sequence: int,
    routing_attempt_id: str | None,
    stream_session_id: str | None,
) -> ProviderAccessSelection:
    """在 Provider 前选择 Key，并在启用亲和时原子提交 Selection。

    非亲和请求直接调用既有选择器。亲和请求要求非流 attempt 与 stream session 二选一且已经
    提交；配置、scope、endpoint、Credential 和 Selection 任一步失败都不能把明文 Key 交给
    Adapter。基础 KeyPool 无可用 Key 仍保留 ``provider_key.unavailable``。
    """

    # 未启用亲和时必须完整复用既有租户 KeyPool，不能创建 Selection 或改变错误码。
    if affinity is None:
        # 返回值仅把旧选择结果包装为空句柄，确保旧主流程没有任何亲和副作用。
        return ProviderAccessSelection(
            select_provider_key(
                config,
                route,
                request_id=request_id,
                trace_id=trace_id,
            ),
            None,
            None,
        )
    _validate_execution_gate_arguments(
        request_id=request_id,
        trace_id=trace_id,
        attempt_sequence=attempt_sequence,
        routing_attempt_id=routing_attempt_id,
        stream_session_id=stream_session_id,
    )
    connection: sqlite3.Connection | None = None
    selected_key: SelectedProviderKey | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        # Key 事务只能收口同一 tenant/五维 Scope 的父终态；Alias 不属于缓存域，同域其他
        # Alias 的遗留 Selection 也必须释放。全库扫描会让外域损坏事实拖垮正常调用。
        reconcile_pending_cache_affinity_selections(
            connection,
            tenant_id=affinity.tenant_id,
            cache_affinity_scope_id_snapshot=affinity.cache_affinity_scope_id,
        )
        _revalidate_configuration(connection, affinity)
        endpoint_revision = _revalidate_route_graph(config, connection, route)
        existing = _load_existing_selection(
            connection,
            tenant_id=affinity.tenant_id,
            request_id=request_id,
            attempt_sequence=attempt_sequence,
        )
        if existing is not None:
            selected_key, handle = _restore_existing_selection(
                config,
                connection,
                route=route,
                affinity=affinity,
                selection=existing,
                routing_attempt_id=routing_attempt_id,
                stream_session_id=stream_session_id,
                endpoint_revision=endpoint_revision,
            )
            connection.rollback()
            return ProviderAccessSelection(selected_key, handle, None)

        binding, binding_created = _load_or_create_binding(
            config,
            connection,
            route=route,
            affinity=affinity,
            endpoint_revision=endpoint_revision,
        )
        state_before = _binding_state_before(
            binding,
            ttl_seconds=affinity.ttl_seconds,
            as_of=_utcnow(),
            binding_created=binding_created,
        )
        selected_key, target_kind = _select_key_in_transaction(
            config,
            connection,
            route=route,
            binding=binding,
            request_id=request_id,
            trace_id=trace_id,
        )
        binding, target_rebound = _apply_binding_target(
            connection,
            binding=binding,
            selected_key=selected_key,
            target_kind=target_kind,
            as_of=_utcnow(),
        )
        warm_evidence = _parse_optional_db_time(binding["last_success_at"])
        estimated_hit = (
            not target_rebound
            and warm_evidence is not None
            and _utcnow() < warm_evidence + timedelta(seconds=affinity.ttl_seconds)
        )
        logical_created = binding_created or state_before == "missing"
        if target_rebound and state_before != "missing":
            # 旧目标失去硬资格时不能把仅凭时间仍 warm 的行描述成可复用证据。
            state_before = "ineligible"
        reason = _selection_reason(
            binding_created=logical_created,
            target_rebound=target_rebound,
            estimated_hit=estimated_hit,
        )
        selection_id = f"cas_{uuid4().hex}"
        created_at = _to_db_time(_utcnow())
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
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, 'pending', NULL, ?
            )
            """,
            (
                selection_id,
                affinity.tenant_id,
                request_id,
                trace_id,
                attempt_sequence,
                routing_attempt_id,
                stream_session_id,
                affinity.cache_affinity_id,
                affinity.cache_affinity_version,
                affinity.cache_affinity_scope_id,
                binding["cache_affinity_binding_id"],
                affinity.model_alias_id,
                route.model_id,
                endpoint_revision,
                route.provider_id,
                route.provider_code,
                selected_key.provider_credential_id if selected_key else None,
                target_kind,
                binding["target_generation"],
                binding["pending_lease_generation"],
                affinity.ttl_seconds,
                _to_db_time(warm_evidence) if warm_evidence else None,
                int(
                    affinity.endpoint_preference_applied
                    and route.model_id == affinity.model_endpoint_id
                ),
                "warm" if estimated_hit else "pending",
                state_before,
                reason,
                int(estimated_hit),
                created_at,
            ),
        )
        _record_selection_effects(
            connection,
            tenant_id=affinity.tenant_id,
            scope_id=affinity.cache_affinity_scope_id,
            binding=binding,
            estimated_hit=estimated_hit,
            selected_key=selected_key,
            as_of=_parse_db_time(created_at),
        )
        connection.commit()
        handle = CacheAffinitySelectionHandle(
            cache_affinity_selection_id=selection_id,
            tenant_id=affinity.tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            attempt_sequence=attempt_sequence,
            routing_attempt_id=routing_attempt_id,
            stream_session_id=stream_session_id,
            cache_affinity_binding_id=str(binding["cache_affinity_binding_id"]),
            target_kind=target_kind,
            target_generation=int(binding["target_generation"]),
            pending_lease_generation=int(binding["pending_lease_generation"]),
            model_endpoint_id=route.model_id,
            provider_id=route.provider_id,
            endpoint_revision_digest=endpoint_revision,
            provider_credential_id=selected_key.provider_credential_id if selected_key else None,
        )
        event = _selection_event(
            affinity,
            handle=handle,
            route=route,
            selection_reason=reason,
            target_kind=target_kind,
            estimated_hit=estimated_hit,
            created_at=_parse_db_time(created_at),
        )
        return ProviderAccessSelection(selected_key, handle, event)
    # 已分类领域错误要保留 provider_key 或亲和固定错误码，同时回滚当前 Selection 事务。
    except ProviderKeyDecryptAnomaly as anomaly:
        if connection is not None:
            connection.rollback()
        persist_provider_key_decrypt_anomaly(
            config,
            route,
            request_id=request_id,
            trace_id=trace_id,
            candidate_count=anomaly.candidate_count,
            error_code=anomaly.error_code,
        )
        raise ApiError(anomaly.error_code) from None
    except ApiError:
        if connection is not None:
            connection.rollback()
        raise
    # 数据库、解析或状态图异常统一折叠为亲和不可用，底层路径和摘要不得进入公开错误。
    except (DbError, sqlite3.Error, ValueError, TypeError) as exc:
        if connection is not None:
            connection.rollback()
        # Provider 尚未收到请求，因此一致性故障必须在此失败关闭而不能回退旧 KeyPool。
        raise ApiError("gateway.cache_affinity_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def _validate_execution_gate_arguments(
    *,
    request_id: str,
    trace_id: str,
    attempt_sequence: int,
    routing_attempt_id: str | None,
    stream_session_id: str | None,
) -> None:
    """验证 Selection 必需的链路和 execution gate 形状。

    request/trace 必须来自已认证请求，且非流 attempt 与 StreamSession 必须严格二选一；
    这样每条 Selection 才能由唯一父终态恢复，而不会制造无法审计的 Provider 调用。
    """

    # 空链路或第三次 attempt 都无法绑定唯一父门，必须在打开数据库事务前拒绝。
    if not request_id or not trace_id or attempt_sequence not in {1, 2}:
        raise ApiError("gateway.cache_affinity_unavailable")
    # 同时存在或同时缺失父 ID 都会让终态来源不唯一，禁止创建 Selection。
    if (routing_attempt_id is None) == (stream_session_id is None):
        raise ApiError("gateway.cache_affinity_unavailable")
    # 本期流式不执行 Fallback，因此 StreamSession 只能对应首个 attempt 序号。
    if stream_session_id is not None and attempt_sequence != 1:
        raise ApiError("gateway.cache_affinity_unavailable")


def _revalidate_configuration(
    connection: sqlite3.Connection,
    affinity: CacheAffinityRouteContext,
) -> None:
    """防止 Route 与 KeyPool 两段事务之间的配置变化产生隐形调用。"""

    row = connection.execute(
        """
        SELECT status, version FROM cache_affinities
        WHERE tenant_id=? AND cache_affinity_id=? AND model_alias_id=?
        """,
        (affinity.tenant_id, affinity.cache_affinity_id, affinity.model_alias_id),
    ).fetchone()
    # 两事务之间禁用或改版的配置不能继续使用旧 route reservation；必须停在 Provider 前。
    # 同租户、配置和 Alias 三个条件共同阻止跨对象恢复。
    if (
        row is None
        or str(row["status"]) != "enabled"
        or int(row["version"]) != affinity.cache_affinity_version
    ):
        # 配置缺失、禁用或版本漂移统一证明当前 reservation 已失效，必须在 Key 解密前拒绝。
        # 公开错误不区分具体原因，避免借此枚举 Alias 配置状态。
        raise ApiError("gateway.cache_affinity_unavailable")
    scope = connection.execute(
        """
        SELECT affinity_scope_digest FROM cache_affinity_scopes
        WHERE tenant_id=? AND cache_affinity_scope_id=? AND virtual_key_id=?
          AND digest_schema_version=?
        """,
        (
            affinity.tenant_id,
            affinity.cache_affinity_scope_id,
            affinity.virtual_key_id,
            affinity.digest_schema_version,
        ),
    ).fetchone()
    # Scope 必须仍属于同一 Virtual Key 和摘要版本；摘要只在内存比较且不得写入日志。
    # 行缺失或摘要漂移都表示 reservation 已失效，不能重新创建后继续当前请求。
    if scope is None or str(scope["affinity_scope_digest"]) != affinity.scope_digest:
        raise ApiError("gateway.cache_affinity_unavailable")


def _revalidate_route_graph(
    config: AppConfig,
    connection: sqlite3.Connection,
    route: GatewayRoute,
) -> str:
    """在 KeyPool 第二事务重读 Model/Provider，并返回当前 endpoint revision。

    路由事务和 Key 事务之间可能发生禁用、改 URL、改上游模型或切换认证方式。托管 Key
    不能因为不读取环境变量而跳过这些变化；全部服务端字段、状态和新鲜 unhealthy 证据
    必须仍与冻结 route 一致，否则在明文 Key 交给 Adapter 前失败关闭。
    """

    row = connection.execute(
        """
        SELECT
          model.model_id, model.tenant_id, model.model_alias,
          COALESCE(model.provider_model_id, model.upstream_model_name) AS upstream_model_name,
          model.capabilities, model.status AS model_status,
          provider.provider_id, provider.provider_code, provider.provider_type,
          provider.auth_type, provider.name AS provider_name, provider.base_url,
          provider.status AS provider_status, provider.health_status,
          provider.health_updated_at
        FROM models model
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        WHERE model.tenant_id=? AND model.model_id=?
        """,
        (route.tenant_id, route.model_id),
    ).fetchone()
    # Model/Provider 必须仍在当前租户且均 active，删除和跨租户缺失使用同形错误防枚举。
    # 该检查先于任何 Key 解密，避免把明文凭据交给失效 Endpoint。
    if row is None or row["model_status"] != "active" or row["provider_status"] != "active":
        # 删除、禁用或跨租户图缺失均使用同一执行门错误，不能向客户端暴露对象存在性。
        raise ApiError("gateway.cache_affinity_unavailable")
    expected_text = {
        "model_id": route.model_id,
        "tenant_id": route.tenant_id,
        "model_alias": route.model_alias,
        "upstream_model_name": route.upstream_model_name,
        "provider_id": route.provider_id,
        "provider_code": route.provider_code,
        "provider_type": route.provider_type,
        "auth_type": route.provider_auth_type,
        "provider_name": route.provider_name,
        "base_url": route.provider_base_url,
    }
    if any(str(row[column]) != expected for column, expected in expected_text.items()):
        # 任一 Endpoint/Provider 原地变化都必须造成当前请求失败，而不是把旧 Key 发往新目标。
        # 对外只返回固定错误，绝不能附带实际 URL、上游模型名或差异字段。
        raise ApiError("gateway.cache_affinity_unavailable")
    capabilities = json.loads(str(row["capabilities"] or "{}"))
    # 能力图漂移可能让旧 Chat/stream 校验失效，必须与冻结 route 完全一致。
    if not isinstance(capabilities, dict) or capabilities != route.capabilities:
        raise ApiError("gateway.cache_affinity_unavailable")
    health_state = str(row["health_status"] or "unknown")
    # 未知持久枚举不能按 healthy 或 stale 解释，否则损坏行可能绕过健康硬门禁。
    if health_state not in {"healthy", "unhealthy", "unknown"}:
        raise ApiError("gateway.cache_affinity_unavailable")
    health_updated_at = _parse_optional_db_time(row["health_updated_at"])
    as_of = _utcnow()
    # 来自未来的健康时间会破坏 stale 窗口判断，不能被当作永久新鲜证据继续调用 Provider。
    # 该一致性错误只返回固定亲和码，不回显数据库时间或节点信息。
    if health_updated_at is not None and health_updated_at > as_of:
        raise ApiError("gateway.cache_affinity_unavailable")
    if (
        health_state == "unhealthy"
        and health_updated_at is not None
        and health_updated_at >= as_of - timedelta(seconds=config.provider_health_stale_seconds)
    ):
        # 只有新鲜 unhealthy 是硬拒绝；历史陈旧证据继续按既有路由 unknown 语义处理。
        raise ApiError("gateway.cache_affinity_unavailable")
    return endpoint_revision_for_route(config, route)


def _load_or_create_binding(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    route: GatewayRoute,
    affinity: CacheAffinityRouteContext,
    endpoint_revision: str,
) -> tuple[sqlite3.Row, bool]:
    """读取最终 endpoint Binding；Fallback 可在 KeyPool 事务内新建独立行。"""

    row = connection.execute(
        """
        SELECT * FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_scope_id=? AND model_endpoint_id=?
          AND endpoint_revision_digest=?
        """,
        (
            affinity.tenant_id,
            affinity.cache_affinity_scope_id,
            route.model_id,
            endpoint_revision,
        ),
    ).fetchone()
    if row is not None:
        return row, False
    _enforce_binding_capacity(
        connection,
        tenant_id=affinity.tenant_id,
        scope_id=affinity.cache_affinity_scope_id,
        as_of=_utcnow(),
    )
    now = _utcnow()
    now_db = _to_db_time(now)
    binding_id = f"cab_{uuid4().hex}"
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
            affinity.tenant_id,
            affinity.cache_affinity_scope_id,
            build_cache_affinity_key_digest(
                config.cache_affinity_hmac_secret,
                affinity_scope_digest=affinity.scope_digest,
                model_endpoint_id=route.model_id,
                endpoint_revision_digest=endpoint_revision,
            ),
            route.model_id,
            endpoint_revision,
            route.provider_id,
            now_db,
            _to_db_time(now + timedelta(seconds=PENDING_LEASE_SECONDS)),
            now_db,
            now_db,
            now_db,
        ),
    )
    created = connection.execute(
        "SELECT * FROM cache_affinity_bindings WHERE tenant_id=? AND cache_affinity_binding_id=?",
        (affinity.tenant_id, binding_id),
    ).fetchone()
    # Fallback Binding 创建后必须在同一租户事务内读回，否则不能生成后续 Selection。
    if created is None:
        raise sqlite3.IntegrityError("cache affinity fallback binding unavailable")
    return created, True


def _select_key_in_transaction(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    route: GatewayRoute,
    binding: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> tuple[SelectedProviderKey | None, str]:
    """优先复用健康绑定 Key，否则按既有稳定排序选择新目标。

    ``request_id/trace_id`` 来自已验证的执行门，只用于无 Credential 标识的安全告警；选择、
    解密和绑定仍限定在当前 tenant/provider 事务内，任何异常正文与 Key 前缀均不得落日志。
    """

    # 无认证 Endpoint 不应伪造 Credential 或调用旧 KeyPool，但仍以 none target 参与节点粘性。
    # 该分支只返回目标类型，不读取任何环境变量或数据库密钥。
    if route.provider_auth_type == "none":
        return None, "none"
    rows = connection.execute(
        """
        SELECT provider_credential_id, encrypted_key, key_prefix, weight, status,
               last_used_at, rate_limited_until, probe_health_status, probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=?
        """,
        (route.tenant_id, route.provider_id),
    ).fetchall()
    managed_rows = [row for row in rows if str(row["encrypted_key"] or "").startswith("aesgcm-v1:")]
    if managed_rows:
        decrypt_failure_count = 0
        preferred_id = str(binding["provider_credential_id"] or "")
        candidates = [
            row
            for row in managed_rows
            if _row_is_selectable(row, stale_seconds=config.provider_health_stale_seconds)
        ]
        candidates.sort(
            key=lambda row: (
                0 if str(row["provider_credential_id"]) == preferred_id else 1,
                _selection_sort_key(row),
            )
        )
        for row in candidates:
            try:
                plain_value = decrypt_provider_key(str(row["encrypted_key"]))
            # 单把损坏密文只使该候选失格；日志使用真实 request/trace 但禁止 Credential 标识。
            # 继续尝试同租户同 Provider 的下一把托管 Key，不能回退全局环境 Key。
            except ValueError:
                decrypt_failure_count += 1
                logger.warning(
                    "provider_key.decrypt_failed",
                    extra={
                        "event": "provider_key.decrypt_failed",
                        "tenant_id": route.tenant_id,
                        "provider_id": route.provider_id,
                        "request_id": request_id,
                        "trace_id": trace_id,
                        "payload": {
                            "provider_code": route.provider_code,
                            "model_id": route.model_id,
                        },
                    },
                )
                continue
            if decrypt_failure_count:
                _insert_provider_key_anomaly(
                    connection,
                    route,
                    request_id=request_id,
                    trace_id=trace_id,
                    candidate_count=decrypt_failure_count,
                )
            return (
                SelectedProviderKey(
                    provider_credential_id=str(row["provider_credential_id"]),
                    plain_value=plain_value,
                    source="database",
                    key_prefix=str(row["key_prefix"] or "") or None,
                ),
                "managed",
            )
        # 租户一旦配置托管 Key，全部不合格时必须保留既有 KeyPool 固定错误码并停止。
        if decrypt_failure_count:
            raise ProviderKeyDecryptAnomaly(
                decrypt_failure_count,
                error_code="provider_key.unavailable",
            )
        raise ApiError("provider_key.unavailable")
    environment_key = _environment_provider_key(
        config,
        connection,
        tenant_id=route.tenant_id,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        route_base_url=route.provider_base_url,
    )
    if environment_key:
        return SelectedProviderKey(None, environment_key, "environment", None), "environment"
    # 认证路由没有托管或严格归属的环境 Key 时，不得构造无 Authorization 的上游请求。
    raise ApiError("provider_key.unavailable")


def _apply_binding_target(
    connection: sqlite3.Connection,
    *,
    binding: sqlite3.Row,
    selected_key: SelectedProviderKey | None,
    target_kind: str,
    as_of: datetime,
) -> tuple[sqlite3.Row, bool]:
    """在 Credential / target 变化时推进 generation 并清除旧 warm 证据。"""

    credential_id = selected_key.provider_credential_id if selected_key else None
    target_changed = (
        str(binding["target_kind"]) != target_kind
        or (binding["provider_credential_id"] or None) != credential_id
    )
    now = _to_db_time(as_of)
    if target_changed:
        connection.execute(
            """
            UPDATE cache_affinity_bindings
            SET provider_credential_id=?, target_kind=?, target_generation=target_generation+1,
                pending_lease_generation=pending_lease_generation+1,
                pending_lease_started_at=?, pending_lease_expires_at=?,
                active_selection_count=0, last_success_at=NULL,
                last_selected_at=?, updated_at=?
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (
                credential_id,
                target_kind,
                now,
                _to_db_time(as_of + timedelta(seconds=PENDING_LEASE_SECONDS)),
                now,
                now,
                binding["tenant_id"],
                binding["cache_affinity_binding_id"],
            ),
        )
    refreshed = connection.execute(
        "SELECT * FROM cache_affinity_bindings WHERE tenant_id=? AND cache_affinity_binding_id=?",
        (binding["tenant_id"], binding["cache_affinity_binding_id"]),
    ).fetchone()
    # target/generation 更新后必须读回同一 Binding，零行表示状态图已失效并需整体回滚。
    if refreshed is None:
        raise sqlite3.IntegrityError("cache affinity target unavailable")
    return refreshed, target_changed


def _record_selection_effects(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scope_id: str,
    binding: sqlite3.Row,
    estimated_hit: bool,
    selected_key: SelectedProviderKey | None,
    as_of: datetime,
) -> None:
    """在 Selection 同一事务更新计数、pointer 和 Credential LRU。

    三组副作用与 Selection 插入必须原子提交，避免计数、偏好指针或 Key 最近使用时间漂移。
    """

    now = _to_db_time(as_of)
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=active_selection_count+1,
            selection_count=selection_count+1,
            estimated_hit_count=estimated_hit_count+?,
            last_selected_at=?, updated_at=?
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (
            int(estimated_hit),
            now,
            now,
            tenant_id,
            binding["cache_affinity_binding_id"],
        ),
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
            now,
            now,
            tenant_id,
            scope_id,
        ),
    )
    # 只有托管 Credential 才更新租户限定 LRU；环境 Key 与 none target 没有可写数据库身份。
    # Credential ID 仅作为 SQL 参数使用，绝不能加入事件、日志或返回 DTO。
    if selected_key is not None and selected_key.provider_credential_id is not None:
        connection.execute(
            """
            UPDATE provider_credentials
            SET status='active', last_used_at=?, rate_limited_until=NULL, updated_at=?
            WHERE tenant_id=? AND provider_credential_id=?
            """,
            (now, now, tenant_id, selected_key.provider_credential_id),
        )


def _load_existing_selection(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    attempt_sequence: int,
) -> sqlite3.Row | None:
    """读取 request/attempt 唯一 Selection，支持事务结果未知后的安全重入。"""

    return connection.execute(
        """
        SELECT * FROM cache_affinity_selections
        WHERE tenant_id=? AND request_id=? AND attempt_sequence=?
        """,
        (tenant_id, request_id, attempt_sequence),
    ).fetchone()


def _restore_existing_selection(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    route: GatewayRoute,
    affinity: CacheAffinityRouteContext,
    selection: sqlite3.Row,
    routing_attempt_id: str | None,
    stream_session_id: str | None,
    endpoint_revision: str,
) -> tuple[SelectedProviderKey | None, CacheAffinitySelectionHandle]:
    """严格恢复同一意图和 target；不一致时拒绝第二次 Provider 调用。"""

    expected = (
        str(selection["cache_affinity_id"]) == affinity.cache_affinity_id
        and int(selection["cache_affinity_version"]) == affinity.cache_affinity_version
        and str(selection["model_endpoint_id"]) == route.model_id
        and str(selection["provider_id"]) == route.provider_id
        and (selection["routing_attempt_id"] or None) == routing_attempt_id
        and (selection["stream_session_id"] or None) == stream_session_id
        and str(selection["endpoint_revision_digest_snapshot"]) == endpoint_revision
    )
    # 重入只接受同配置、同 endpoint、同父门且仍 pending 的原意图，终态请求不得二次调用上游。
    # 不一致时固定失败，不向调用方暴露是哪一项内部快照发生变化。
    if not expected or str(selection["outcome_status"]) != "pending":
        raise ApiError("gateway.cache_affinity_unavailable")
    current_binding = connection.execute(
        """
        SELECT target_kind, target_generation, provider_credential_id,
               model_endpoint_id, provider_id, endpoint_revision_digest
        FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (affinity.tenant_id, selection["cache_affinity_binding_id_snapshot"]),
    ).fetchone()
    if (
        current_binding is None
        or str(current_binding["target_kind"]) != str(selection["target_kind"])
        or int(current_binding["target_generation"]) != int(selection["target_generation"])
        or (current_binding["provider_credential_id"] or None)
        != (selection["provider_credential_id_snapshot"] or None)
        or str(current_binding["model_endpoint_id"]) != route.model_id
        or str(current_binding["provider_id"]) != route.provider_id
        or str(current_binding["endpoint_revision_digest"]) != endpoint_revision
    ):
        # 幂等恢复也必须重验当前目标，避免 K1 -> K2 后再次使用旧 Selection 的 K1。
        raise ApiError("gateway.cache_affinity_unavailable")
    target_kind = str(selection["target_kind"])
    credential_id = selection["provider_credential_id_snapshot"] or None
    if target_kind == "managed":
        row = connection.execute(
            """
            SELECT encrypted_key, key_prefix, status, rate_limited_until,
                   probe_health_status, probe_last_tested_at
            FROM provider_credentials
            WHERE tenant_id=? AND provider_id=? AND provider_credential_id=?
            """,
            (affinity.tenant_id, route.provider_id, credential_id),
        ).fetchone()
        # 幂等恢复仍要重验 Key 状态、冷却和健康证据，不能因旧 Selection 绕过当前硬门禁。
        if row is None or not _row_is_selectable(
            row, stale_seconds=config.provider_health_stale_seconds
        ):
            # 删除或失格的 Credential 使用同一亲和错误，禁止回退环境变量或另一把 Key。
            raise ApiError("gateway.cache_affinity_unavailable")
        try:
            plain_value = decrypt_provider_key(str(row["encrypted_key"]))
        # 合法格式但认证标签损坏也已在共享解密边界归一为 ValueError；这里只映射固定错误。
        except ValueError:
            # 外层先回滚读取事务，再用独立短事务持久化安全异常；不能在持写锁时另开连接。
            raise ProviderKeyDecryptAnomaly(
                1,
                error_code="gateway.cache_affinity_unavailable",
            ) from None
        selected_key = SelectedProviderKey(
            str(credential_id), plain_value, "database", str(row["key_prefix"] or "") or None
        )
    elif target_kind == "environment":
        value = _environment_provider_key(
            config,
            connection,
            tenant_id=route.tenant_id,
            provider_id=route.provider_id,
            provider_code=route.provider_code,
            route_base_url=route.provider_base_url,
        )
        # 环境 Key 必须继续满足默认租户、Provider 和 Endpoint 严格归属，缺失时不重新选 Key。
        if not value:
            raise ApiError("gateway.cache_affinity_unavailable")
        selected_key = SelectedProviderKey(None, value, "environment", None)
    # 无认证恢复只接受原 target_kind=none 与当前 route 无认证同时成立，避免认证降级。
    elif target_kind == "none" and route.provider_auth_type == "none":
        selected_key = None
    else:
        # 未知目标类型或认证方式漂移都表示 Selection 已不可安全恢复。
        raise ApiError("gateway.cache_affinity_unavailable")
    handle = CacheAffinitySelectionHandle(
        cache_affinity_selection_id=str(selection["cache_affinity_selection_id"]),
        tenant_id=affinity.tenant_id,
        request_id=str(selection["request_id"]),
        trace_id=str(selection["trace_id"]),
        attempt_sequence=int(selection["attempt_sequence"]),
        routing_attempt_id=selection["routing_attempt_id"],
        stream_session_id=selection["stream_session_id"],
        cache_affinity_binding_id=str(selection["cache_affinity_binding_id_snapshot"]),
        target_kind=target_kind,
        target_generation=int(selection["target_generation"]),
        pending_lease_generation=int(selection["pending_lease_generation_snapshot"]),
        model_endpoint_id=str(selection["model_endpoint_id"]),
        provider_id=str(selection["provider_id"]),
        endpoint_revision_digest=str(selection["endpoint_revision_digest_snapshot"]),
        provider_credential_id=credential_id,
    )
    return selected_key, handle


def _binding_state_before(
    row: sqlite3.Row,
    *,
    ttl_seconds: int,
    as_of: datetime,
    binding_created: bool,
) -> str:
    """按选择前快照生成 Selection 的稳定状态枚举。"""

    if binding_created or (
        str(row["target_kind"]) == "unassigned" and int(row["selection_count"]) == 0
    ):
        return "missing"
    last_success = _parse_optional_db_time(row["last_success_at"])
    if last_success is not None and as_of < last_success + timedelta(seconds=ttl_seconds):
        return "warm"
    lease_expires = _parse_optional_db_time(row["pending_lease_expires_at"])
    if (
        lease_expires is not None
        and as_of < lease_expires
        or int(row["active_selection_count"]) > 0
    ):
        return "pending"
    return "expired"


def _selection_reason(
    *,
    binding_created: bool,
    target_rebound: bool,
    estimated_hit: bool,
) -> str:
    """把 Binding 状态转换成 API / Webhook 共用固定原因。"""

    if target_rebound and not binding_created:
        return "binding_rebound"
    if binding_created:
        return "binding_created"
    if estimated_hit:
        return "binding_warm_reused"
    return "binding_pending_reused"


def _selection_event(
    affinity: CacheAffinityRouteContext,
    *,
    handle: CacheAffinitySelectionHandle,
    route: GatewayRoute,
    selection_reason: str,
    target_kind: str,
    estimated_hit: bool,
    created_at: datetime,
) -> WebhookEvent:
    """构造 Selection 提交后、Provider 前投递的一次性安全事件。"""

    return build_webhook_event(
        tenant_id=affinity.tenant_id,
        event_type="cache.affinity_selected",
        event_id=handle.cache_affinity_selection_id,
        timestamp=created_at,
        actor_id=None,
        object_id=affinity.cache_affinity_id,
        trace_id=handle.trace_id,
        payload={
            "trigger": "request_selected",
            "request_id": handle.request_id,
            "cache_affinity_selection_id": handle.cache_affinity_selection_id,
            "cache_affinity_id": affinity.cache_affinity_id,
            "cache_affinity_version": affinity.cache_affinity_version,
            "model_alias_id": affinity.model_alias_id,
            "model_endpoint_id": route.model_id,
            "selected_provider_id": route.provider_id,
            "target_kind": target_kind,
            "selection_reason": selection_reason,
            "estimated_hit": estimated_hit,
        },
    )


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析可空 UTC 数据库时间。

    空值表示尚无成功或 lease 证据；非空值必须交给严格时区解析，不能猜测本机时区。
    """

    if value is None or value == "":
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """严格解析数据库 UTC 时间并统一时区。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """生成统一 UTC 秒级数据库时间。

    秒级 Z 格式与迁移触发器的文本比较口径一致，避免微秒文本造成同秒顺序反转。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回无微秒 UTC 时间，确保同一 Selection 的比较边界稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
