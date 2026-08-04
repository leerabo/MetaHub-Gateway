"""ModelAlias Prompt Cache 亲和配置的租户级写事务与幂等恢复。

本模块只管理管理员配置事实，不读取或返回 Prompt 指纹、运行 Binding、Provider
Credential 或 Key 标识。配置、不可变 ConfigEvent 与管理审计必须共同成功或共同回滚；
Webhook 是提交后的外部副作用，失败只能记录脱敏定位信息。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import CacheAffinityInfo, CacheAffinityMutationResponse

logger = logging.getLogger("metahub.cache_affinity")

# PATCH 使用完整快照语义；固定字段集合避免旧客户端遗漏字段时静默继承未知配置。
CACHE_AFFINITY_REQUEST_FIELDS = frozenset(
    {"status", "data_class", "minimum_prefix_chars", "ttl_seconds", "expected_version"}
)
CACHE_AFFINITY_STATUSES = frozenset({"enabled", "disabled"})
DATA_CLASS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MIN_PREFIX_CHARS = 256
MAX_PREFIX_CHARS = 16_384
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86_400
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
CONFIG_SNAPSHOT_FIELDS = frozenset(
    {"status", "data_class", "minimum_prefix_chars", "ttl_seconds", "version"}
)
CONFIG_FIELD_ORDER = ("status", "data_class", "minimum_prefix_chars", "ttl_seconds")


@dataclass(frozen=True)
class _DesiredCacheAffinity:
    """经过严格类型和业务边界校验的完整配置意图。"""

    status: str
    data_class: str | None
    minimum_prefix_chars: int
    ttl_seconds: int
    expected_version: int


@dataclass(frozen=True)
class _ConfigEventRecord:
    """从不可变 ConfigEvent 重建幂等响应和 Webhook 的安全事实。"""

    event_id: str
    tenant_id: str
    cache_affinity_id: str
    model_alias_id: str
    cache_affinity_version: int
    actor_user_id: str
    request_id: str
    trace_id: str
    request_fingerprint: str
    event_type: str
    before: dict[str, object]
    after: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class _RecoveredMutation:
    """同一 actor/request_id 精确恢复时需要重派的持久事实。"""

    cache_affinity: CacheAffinityInfo
    event: _ConfigEventRecord
    audit: AuditRecord | None


def update_cache_affinity(
    config: AppConfig,
    context: AuthContext,
    model_alias_id: str,
    payload: dict[str, Any],
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> CacheAffinityMutationResponse:
    """按独立版本 CAS 创建或更新当前租户 Alias 的 CacheAffinity。

    参数：应用配置、认证上下文、路径 Alias、严格 JSON 字典、审计来源和链路 ID。
    返回：提交后的安全配置快照与 changed 标记。
    关键约束：后端再次确认 admin；写锁覆盖幂等恢复、CAS、配置、事件和审计；no-op
    仍消费 request_id 并写 ConfigEvent，但不升版本、不写 Audit 或配置 Webhook。
    """

    # 路由依赖是首道权限门；服务层复核避免内部调用绕过后端 RBAC。
    if context.user.role != "admin":
        raise ApiError("auth.permission_denied")
    desired = _normalize_payload(payload)
    normalized_request_id = _normalize_request_id(request_id)
    fingerprint = _request_fingerprint(model_alias_id, desired)
    connection = _connect(config)
    event: _ConfigEventRecord | None = None
    audit_record: AuditRecord | None = None
    cache_affinity: CacheAffinityInfo | None = None
    changed = False
    recovered = False
    response_trace_id = trace_id
    changed_fields: list[str] = []
    alias_name = ""
    try:
        # BEGIN IMMEDIATE 让“恢复检查 -> CAS -> 三事实提交”只有一个确定顺序。
        connection.execute("BEGIN IMMEDIATE")
        recovered_mutation = _recover_mutation(
            connection,
            context,
            request_id=normalized_request_id,
            fingerprint=fingerprint,
        )
        if recovered_mutation is not None:
            # 这里只读取已提交事实；回滚读事务后再执行幂等的提交后投递。
            connection.rollback()
            recovered = True
            cache_affinity = recovered_mutation.cache_affinity
            event = recovered_mutation.event
            audit_record = recovered_mutation.audit
            changed = event.event_type != "noop"
            response_trace_id = event.trace_id
        else:
            alias_row = _require_model_alias(
                connection,
                tenant_id=context.user.tenant_id,
                model_alias_id=model_alias_id,
            )
            alias_name = str(alias_row["alias"])
            before = _load_cache_affinity_for_alias(
                connection,
                tenant_id=context.user.tenant_id,
                model_alias_id=model_alias_id,
            )
            _enforce_expected_version(before, desired.expected_version)
            now = _to_db_time(_utcnow())
            before_snapshot = _config_snapshot(before) if before is not None else {}
            desired_matches = before is not None and _config_matches(before, desired)
            if desired_matches:
                # CAS 已在上方通过；no-op 不触碰配置时间和版本，但必须占用幂等键。
                cache_affinity = before
                event = _insert_config_event(
                    connection,
                    context,
                    cache_affinity,
                    request_id=normalized_request_id,
                    trace_id=trace_id,
                    fingerprint=fingerprint,
                    event_type="noop",
                    before=before_snapshot,
                    after=before_snapshot,
                    now=now,
                )
                connection.commit()
            else:
                if before is None:
                    cache_affinity_id = f"caf_{uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO cache_affinities(
                          cache_affinity_id, tenant_id, model_alias_id, status, data_class,
                          minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
                          created_at, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            cache_affinity_id,
                            context.user.tenant_id,
                            model_alias_id,
                            desired.status,
                            desired.data_class,
                            desired.minimum_prefix_chars,
                            desired.ttl_seconds,
                            context.user.user_id,
                            context.user.user_id,
                            now,
                            now,
                        ),
                    )
                    event_type = "created"
                else:
                    now = _next_update_db_time(before.updated_at)
                    cursor = connection.execute(
                        """
                        UPDATE cache_affinities
                        SET status=?, data_class=?, minimum_prefix_chars=?, ttl_seconds=?,
                            version=version+1, updated_by=?, updated_at=?
                        WHERE tenant_id=? AND cache_affinity_id=? AND version=?
                        """,
                        (
                            desired.status,
                            desired.data_class,
                            desired.minimum_prefix_chars,
                            desired.ttl_seconds,
                            context.user.user_id,
                            now,
                            context.user.tenant_id,
                            before.cache_affinity_id,
                            desired.expected_version,
                        ),
                    )
                    # 写锁内仍使用条件更新，防止未来连接策略变化时退化成 last-write-wins。
                    if cursor.rowcount != 1:
                        raise ApiError("cache_affinity.version_conflict")
                    event_type = "updated"
                cache_affinity = _require_cache_affinity_for_alias(
                    connection,
                    tenant_id=context.user.tenant_id,
                    model_alias_id=model_alias_id,
                )
                after_snapshot = _config_snapshot(cache_affinity)
                changed_fields = _changed_config_fields(before_snapshot, after_snapshot)
                audit_record = record_audit_log(
                    connection,
                    context,
                    action=f"cache_affinity.{event_type}",
                    object_type="cache_affinity",
                    object_id=cache_affinity.cache_affinity_id,
                    object_name=alias_name,
                    before_summary=(
                        _audit_summary(model_alias_id, before_snapshot) if before_snapshot else {}
                    ),
                    after_summary=_audit_summary(model_alias_id, after_snapshot),
                    request_meta=audit_request_meta,
                    request_id=normalized_request_id,
                    trace_id=trace_id,
                )
                if audit_record is None:
                    # 真实变更必须留下审计事实；空结果说明摘要合同异常，整笔事务失败关闭。
                    raise ApiError("cache_affinity.update_failed")
                event = _insert_config_event(
                    connection,
                    context,
                    cache_affinity,
                    request_id=normalized_request_id,
                    trace_id=trace_id,
                    fingerprint=fingerprint,
                    event_type=event_type,
                    before=before_snapshot,
                    after=after_snapshot,
                    now=now,
                )
                commit_audited_transaction(connection)
                changed = True
    # 领域异常也必须先回滚当前写事务，避免配置、审计与事件出现部分提交。
    except ApiError as exc:
        connection.rollback()
        # 审计基础设施错误必须折叠到本领域事务错误，不能暴露内部模块边界。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("cache_affinity.update_failed") from exc
        raise
    # SQLite 异常可能包含结构细节，回滚后统一转换为不泄露内部信息的领域错误。
    except sqlite3.Error as exc:
        connection.rollback()
        # SQL、数据库路径和 trigger 文本都可能泄露实现细节，只返回稳定 503。
        raise ApiError("cache_affinity.update_failed") from exc
    finally:
        connection.close()

    if cache_affinity is None or event is None:
        # 防御性检查阻止未来分支在三事实不完整时返回虚假 200。
        raise ApiError("cache_affinity.update_failed")
    if audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    if event.event_type != "noop":
        _dispatch_configuration_event(config, event)
    if changed and not recovered:
        _log_changed(
            context,
            cache_affinity,
            request_id=normalized_request_id,
            trace_id=trace_id,
            changed_fields=changed_fields,
        )
    return CacheAffinityMutationResponse(
        cache_affinity=cache_affinity,
        changed=changed,
        request_id=normalized_request_id,
        trace_id=response_trace_id,
    )


def _normalize_payload(payload: object) -> _DesiredCacheAffinity:
    """严格校验完整配置字段、精确 JSON 类型及跨字段约束。"""

    if not isinstance(payload, dict) or set(payload) != CACHE_AFFINITY_REQUEST_FIELDS:
        # 完整快照不允许缺字段或夹带未来字段，避免旧客户端静默覆盖新增配置。
        raise ApiError("cache_affinity.input_invalid")
    status = payload.get("status")
    data_class = payload.get("data_class")
    minimum_prefix_chars = payload.get("minimum_prefix_chars")
    ttl_seconds = payload.get("ttl_seconds")
    expected_version = payload.get("expected_version")
    if type(status) is not str or status not in CACHE_AFFINITY_STATUSES:
        # 状态枚举决定运行期开关，未知值不得落库后由各数据面自行解释。
        raise ApiError("cache_affinity.input_invalid")
    if data_class is not None and (
        type(data_class) is not str or DATA_CLASS_PATTERN.fullmatch(data_class) is None
    ):
        # 分类编码进入隔离摘要，只接受受限字符集以消除歧义和控制字符风险。
        raise ApiError("cache_affinity.input_invalid")
    if status == "enabled" and data_class is None:
        # enabled 没有可信分类会突破架构五维隔离，必须在写入前失败关闭。
        raise ApiError("cache_affinity.input_invalid")
    if (
        type(minimum_prefix_chars) is not int
        or not MIN_PREFIX_CHARS <= minimum_prefix_chars <= MAX_PREFIX_CHARS
        or type(ttl_seconds) is not int
        or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS
        or type(expected_version) is not int
        or not 0 <= expected_version <= MAX_SQLITE_INTEGER
    ):
        # bool 继承 int，因此必须用 type 精确比较，不能让 true 变成版本 1 或 TTL 1。
        raise ApiError("cache_affinity.input_invalid")
    return _DesiredCacheAffinity(
        status=status,
        data_class=data_class,
        minimum_prefix_chars=minimum_prefix_chars,
        ttl_seconds=ttl_seconds,
        expected_version=expected_version,
    )


def _enforce_expected_version(before: CacheAffinityInfo | None, expected_version: int) -> None:
    """在写锁内执行新建 0 / 更新精确版本 CAS 规则。"""

    if before is None:
        if expected_version != 0:
            # 首次创建只接受版本零，防止客户端误把缺失配置当成已有版本覆盖。
            raise ApiError("cache_affinity.version_conflict")
        return
    if expected_version != before.version:
        # 更新必须精确命中当前版本，冲突时要求客户端重新读取而不是后写覆盖。
        raise ApiError("cache_affinity.version_conflict")


def _request_fingerprint(model_alias_id: str, desired: _DesiredCacheAffinity) -> str:
    """为路径、完整配置和期望版本生成稳定 exactly-once 指纹。"""

    canonical = json.dumps(
        {
            "operation": "cache_affinity_upsert",
            "model_alias_id": model_alias_id,
            "status": desired.status,
            "data_class": desired.data_class,
            "minimum_prefix_chars": desired.minimum_prefix_chars,
            "ttl_seconds": desired.ttl_seconds,
            "expected_version": desired.expected_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalize_request_id(value: str) -> str:
    """校验 ConfigEvent 唯一键，不裁剪可能与其他请求碰撞的非法输入。"""

    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        # request_id 同时承担幂等唯一键，非法值不可裁剪后与其他请求发生碰撞。
        raise ApiError("cache_affinity.input_invalid")
    return normalized


def _require_model_alias(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_alias_id: str,
) -> sqlite3.Row:
    """只在认证租户内加载 Alias；跨租户 ID 与不存在 ID 使用同一 404。"""

    row = connection.execute(
        "SELECT model_alias_id, alias FROM model_aliases WHERE tenant_id=? AND model_alias_id=?",
        (tenant_id, model_alias_id),
    ).fetchone()
    if row is None:
        # 跨租户与不存在 Alias 使用同一结果，避免通过路径标识枚举其他租户资源。
        raise ApiError("model_alias.not_found")
    return row


def _load_cache_affinity_for_alias(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_alias_id: str,
) -> CacheAffinityInfo | None:
    """按 tenant+Alias 加载可空配置，禁止只凭全局配置 ID 查询。"""

    row = connection.execute(
        """
        SELECT cache_affinity_id, tenant_id, model_alias_id, status, data_class,
               minimum_prefix_chars, ttl_seconds, version, created_at, updated_at
        FROM cache_affinities
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (tenant_id, model_alias_id),
    ).fetchone()
    return _cache_affinity_info(row) if row is not None else None


def _require_cache_affinity_for_alias(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_alias_id: str,
) -> CacheAffinityInfo:
    """回读刚写入的同租户配置；缺失说明事务事实不完整。"""

    result = _load_cache_affinity_for_alias(
        connection,
        tenant_id=tenant_id,
        model_alias_id=model_alias_id,
    )
    if result is None:
        # 写后回读缺失表示事务事实不完整，不能向调用方返回虚假的成功快照。
        raise ApiError("cache_affinity.update_failed")
    return result


def _load_cache_affinity_by_id(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    cache_affinity_id: str,
) -> CacheAffinityInfo | None:
    """幂等恢复按 tenant+配置 ID 回读当前权威快照。"""

    row = connection.execute(
        """
        SELECT cache_affinity_id, tenant_id, model_alias_id, status, data_class,
               minimum_prefix_chars, ttl_seconds, version, created_at, updated_at
        FROM cache_affinities
        WHERE tenant_id=? AND cache_affinity_id=?
        """,
        (tenant_id, cache_affinity_id),
    ).fetchone()
    return _cache_affinity_info(row) if row is not None else None


def _cache_affinity_info(row: sqlite3.Row) -> CacheAffinityInfo:
    """把数据库配置行转换为不含 actor 和运行事实的安全 DTO。"""

    return CacheAffinityInfo(
        cache_affinity_id=str(row["cache_affinity_id"]),
        tenant_id=str(row["tenant_id"]),
        model_alias_id=str(row["model_alias_id"]),
        status=str(row["status"]),
        data_class=row["data_class"],
        minimum_prefix_chars=int(row["minimum_prefix_chars"]),
        ttl_seconds=int(row["ttl_seconds"]),
        version=int(row["version"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _config_matches(current: CacheAffinityInfo, desired: _DesiredCacheAffinity) -> bool:
    """比较可编辑字段，版本和生命周期时间不参与 no-op 判定。"""

    return (
        current.status == desired.status
        and current.data_class == desired.data_class
        and current.minimum_prefix_chars == desired.minimum_prefix_chars
        and current.ttl_seconds == desired.ttl_seconds
    )


def _config_snapshot(config: CacheAffinityInfo) -> dict[str, object]:
    """构造 ConfigEvent 精确五字段快照，不复制 tenant、actor 或运行标识。"""

    return {
        "status": config.status,
        "data_class": config.data_class,
        "minimum_prefix_chars": config.minimum_prefix_chars,
        "ttl_seconds": config.ttl_seconds,
        "version": config.version,
    }


def _audit_summary(model_alias_id: str, snapshot: dict[str, object]) -> dict[str, object]:
    """构造管理审计白名单摘要；Prompt、Binding 和 Key 永不进入参数。"""

    return {"model_alias_id": model_alias_id, **snapshot}


def _changed_config_fields(
    before: dict[str, object],
    after: dict[str, object],
) -> list[str]:
    """按固定业务顺序生成 Webhook changed_fields，排除自动版本字段。"""

    if not before:
        return list(CONFIG_FIELD_ORDER)
    return [field for field in CONFIG_FIELD_ORDER if before.get(field) != after.get(field)]


def _insert_config_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    cache_affinity: CacheAffinityInfo,
    *,
    request_id: str,
    trace_id: str,
    fingerprint: str,
    event_type: str,
    before: dict[str, object],
    after: dict[str, object],
    now: str,
) -> _ConfigEventRecord:
    """在配置事务内写不可变事件并回读权威行。"""

    event_id = f"cae_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO cache_affinity_config_events(
          cache_affinity_config_event_id, tenant_id, cache_affinity_id, model_alias_id,
          cache_affinity_version, actor_user_id, operation_request_id, trace_id,
          request_fingerprint, event_type, before_json, after_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            context.user.tenant_id,
            cache_affinity.cache_affinity_id,
            cache_affinity.model_alias_id,
            cache_affinity.version,
            context.user.user_id,
            request_id,
            trace_id,
            fingerprint,
            event_type,
            _json_text(before),
            _json_text(after),
            now,
        ),
    )
    row = connection.execute(
        """
        SELECT * FROM cache_affinity_config_events
        WHERE tenant_id=? AND cache_affinity_config_event_id=?
        """,
        (context.user.tenant_id, event_id),
    ).fetchone()
    if row is None:
        # 事件写入后必须能在同一租户内回读，否则不可证明配置与事件原子一致。
        raise ApiError("cache_affinity.update_failed")
    return _event_record(row)


def _event_record(row: sqlite3.Row) -> _ConfigEventRecord:
    """解析 ConfigEvent；历史 JSON 损坏时失败关闭且不回显原文。"""

    try:
        before = json.loads(str(row["before_json"]))
        after = json.loads(str(row["after_json"]))
    # 持久事件 JSON 损坏时不能恢复幂等结果，也不能把原始内容带入错误响应。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("cache_affinity.update_failed") from exc
    event_type = str(row["event_type"])
    if (
        event_type not in {"created", "updated", "noop"}
        or not _valid_persisted_snapshot(before, allow_empty=event_type == "created")
        or not _valid_persisted_snapshot(after, allow_empty=False)
    ):
        # 事件类型或快照结构漂移会破坏幂等证明，因此按不可恢复事务故障处理。
        raise ApiError("cache_affinity.update_failed")
    return _ConfigEventRecord(
        event_id=str(row["cache_affinity_config_event_id"]),
        tenant_id=str(row["tenant_id"]),
        cache_affinity_id=str(row["cache_affinity_id"]),
        model_alias_id=str(row["model_alias_id"]),
        cache_affinity_version=int(row["cache_affinity_version"]),
        actor_user_id=str(row["actor_user_id"]),
        request_id=str(row["operation_request_id"]),
        trace_id=str(row["trace_id"]),
        request_fingerprint=str(row["request_fingerprint"]),
        event_type=event_type,
        before=before,
        after=after,
        created_at=_parse_db_time(row["created_at"]),
    )


def _valid_persisted_snapshot(value: object, *, allow_empty: bool) -> bool:
    """验证内部事件快照精确形状，防止损坏 JSON 被重派到外部。"""

    if allow_empty and value == {}:
        return True
    if not isinstance(value, dict) or set(value) != CONFIG_SNAPSHOT_FIELDS:
        return False
    status = value.get("status")
    data_class = value.get("data_class")
    return (
        type(status) is str
        and status in CACHE_AFFINITY_STATUSES
        and (
            data_class is None
            or (type(data_class) is str and DATA_CLASS_PATTERN.fullmatch(data_class))
        )
        and type(value.get("minimum_prefix_chars")) is int
        and MIN_PREFIX_CHARS <= value["minimum_prefix_chars"] <= MAX_PREFIX_CHARS
        and type(value.get("ttl_seconds")) is int
        and MIN_TTL_SECONDS <= value["ttl_seconds"] <= MAX_TTL_SECONDS
        and type(value.get("version")) is int
        and 1 <= value["version"] <= MAX_SQLITE_INTEGER
        and not (status == "enabled" and data_class is None)
    )


def _recover_mutation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    request_id: str,
    fingerprint: str,
) -> _RecoveredMutation | None:
    """按 tenant/actor/request_id 恢复原结果，并拒绝漂移状态或不同意图。"""

    row = connection.execute(
        """
        SELECT * FROM cache_affinity_config_events
        WHERE tenant_id=? AND actor_user_id=? AND operation_request_id=?
        """,
        (context.user.tenant_id, context.user.user_id, request_id),
    ).fetchone()
    if row is None:
        return None
    event = _event_record(row)
    if event.request_fingerprint != fingerprint:
        # 相同 actor/request_id 只能表示同一意图，不同指纹必须拒绝而非复用旧结果。
        raise ApiError("cache_affinity.request_conflict")
    current = _load_cache_affinity_by_id(
        connection,
        tenant_id=context.user.tenant_id,
        cache_affinity_id=event.cache_affinity_id,
    )
    # 后续版本已演进时不能把旧 200 当成当前权威状态，统一要求客户端重新加载。
    if (
        current is None
        or current.model_alias_id != event.model_alias_id
        or current.version != event.cache_affinity_version
        or _config_snapshot(current) != event.after
    ):
        # 旧事件对应的版本已被后续更新替代时，禁止重放过期成功响应误导客户端。
        raise ApiError("cache_affinity.version_conflict")
    audit = _load_audit_record(connection, context, event)
    return _RecoveredMutation(cache_affinity=current, event=event, audit=audit)


def _load_audit_record(
    connection: sqlite3.Connection,
    context: AuthContext,
    event: _ConfigEventRecord,
) -> AuditRecord | None:
    """恢复真实变更对应的唯一审计；no-op 按合同不得存在变更审计。"""

    rows = connection.execute(
        """
        SELECT audit_log_id, tenant_id, actor_user_id, action, object_type,
               object_id, changed_fields_json, trace_id, created_at
        FROM audit_logs
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
          AND object_type='cache_affinity' AND object_id=?
        ORDER BY created_at, audit_log_id
        """,
        (
            context.user.tenant_id,
            context.user.user_id,
            event.request_id,
            event.cache_affinity_id,
        ),
    ).fetchall()
    if event.event_type == "noop":
        if rows:
            # no-op 合同明确不写审计，出现审计行说明历史事实被污染，恢复必须失败。
            raise ApiError("cache_affinity.update_failed")
        return None
    if len(rows) != 1 or str(rows[0]["action"]) != f"cache_affinity.{event.event_type}":
        # 真实变更必须且只能关联一条动作匹配的审计，避免幂等重放选择错误事实。
        raise ApiError("cache_affinity.update_failed")
    row = rows[0]
    try:
        changed_fields = json.loads(str(row["changed_fields_json"]))
    # 审计摘要损坏时不返回不可信 changed_fields，也不泄露持久化原文。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("cache_affinity.update_failed") from exc
    if not isinstance(changed_fields, list) or not all(
        type(item) is str for item in changed_fields
    ):
        # changed_fields 只允许字符串数组，异常结构不能用于后续 Webhook 重放。
        raise ApiError("cache_affinity.update_failed")
    return AuditRecord(
        audit_log_id=str(row["audit_log_id"]),
        tenant_id=str(row["tenant_id"]),
        actor_user_id=str(row["actor_user_id"]),
        action=str(row["action"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        changed_fields=tuple(changed_fields),
        trace_id=str(row["trace_id"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _dispatch_configuration_event(config: AppConfig, event: _ConfigEventRecord) -> None:
    """提交后投递 configuration_changed；外部失败不改变已提交配置。"""

    try:
        changed_fields = _changed_config_fields(event.before, event.after)
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="cache.affinity_selected",
            event_id=event.event_id,
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.cache_affinity_id,
            trace_id=event.trace_id,
            payload={
                "trigger": "configuration_changed",
                "request_id": event.request_id,
                "cache_affinity_id": event.cache_affinity_id,
                "cache_affinity_version": event.cache_affinity_version,
                "model_alias_id": event.model_alias_id,
                "change_kind": event.event_type,
                "changed_fields": changed_fields,
                "status": event.after["status"],
            },
        )
        # 延迟导入避免 ModelAlias 与 Webhook 服务在应用启动时形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            webhook_event,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        # URL、SQL 或接收端正文可能出现在异常字符串中，日志只留固定码和稳定 ID。
        logger.error(
            "cache_affinity.webhook_dispatch_failed",
            extra={
                "event": "cache_affinity.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.cache_affinity_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _log_changed(
    context: AuthContext,
    cache_affinity: CacheAffinityInfo,
    *,
    request_id: str,
    trace_id: str,
    changed_fields: list[str],
) -> None:
    """记录不含 data_class 值和运行亲和标识的配置变更摘要。"""

    logger.info(
        "cache_affinity.changed",
        extra={
            "event": "cache_affinity.changed",
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": cache_affinity.cache_affinity_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "model_alias_id": cache_affinity.model_alias_id,
                "version": cache_affinity.version,
                "changed_fields": changed_fields,
            },
        },
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """建立数据库连接，并把基础设施异常折叠为 CacheAffinity 统一 503。"""

    try:
        return connect_database(config.database_url)
    # 连接异常可能包含数据库路径，统一转换后仅暴露稳定领域错误码。
    except DbError as exc:
        raise ApiError("cache_affinity.update_failed") from exc


def _json_text(value: dict[str, object]) -> str:
    """用稳定键序列化 ConfigEvent 快照，便于幂等恢复逐字段核对。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC 时间，损坏值按事务不可恢复处理。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 非法持久时间会破坏版本先后关系，拒绝解析且不回显原始数据库值。
    except (TypeError, ValueError) as exc:
        raise ApiError("cache_affinity.update_failed") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 UTC 时间转换为 SQLite 可排序的秒级 ISO-8601 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _next_update_db_time(previous: datetime) -> str:
    """生成严格晚于上一版本的秒级时间，满足数据库单调版本约束。"""

    candidate = _utcnow()
    if candidate <= previous:
        # 高频连续更新可能落在同一秒；推进一秒比放宽数据库不变量更可靠。
        candidate = previous + timedelta(seconds=1)
    return _to_db_time(candidate)


def _utcnow() -> datetime:
    """返回秒级 UTC，保证配置、事件与审计边界稳定。"""

    return datetime.now(UTC).replace(microsecond=0)
