"""Response Cache 控制面服务：租户分页、策略 CAS、幂等失效与事务内审计。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.response_cache import (
    CacheTag,
    VersionedResponseCacheSecret,
    build_control_idempotency_digests,
    build_tag_digests,
    response_cache_secret_fingerprint,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    CacheInvalidationResponse,
    CacheInvalidationSummary,
    CachePolicyListItem,
    CachePolicyListResponse,
    CachePolicyPatchResponse,
    CachePolicySummary,
    InvalidateResponseCacheRequest,
    PatchCachePolicyRequest,
)
from .outbox import enqueue_response_cache_event

# 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
_CURSOR_DOMAIN = b"metahub/response-cache/policy-cursor/v1\x00"
_INTENT_DOMAIN = b"metahub/response-cache/control-intent/v1\x00"
_CURSOR_MAX_LENGTH = 4096
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_POLICY_ID = re.compile(r"^rcp_[0-9a-f]{32}$")
_MACHINE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SELECTOR_KINDS = {"policy", "model_alias", "model", "virtual_key", "data_class", "tag"}
_ZERO_FINGERPRINT = "0" * 64


@dataclass(frozen=True, slots=True)
class _IntentCandidate:
    """一个可验证的 retained 幂等 owner 及该版本对应的请求指纹。"""

    version: int
    digest: str
    fingerprint: str


def list_cache_policies(
    config: AppConfig,
    context: AuthContext,
    *,
    limit: object,
    cursor: object,
    request_id: str,
    trace_id: str,
) -> CachePolicyListResponse:
    """按当前 Session tenant 分页读取 active Alias 与可空策略的 exact DTO。"""

    normalized_limit = _limit(limit)
    position = _decode_cursor(
        cursor,
        secret=config.auth_token_secret,
        tenant_id=context.user.tenant_id,
        limit=normalized_limit,
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = require_transaction_permission(
            connection,
            context,
            "response_cache.read",
            unavailable_error_code="response_cache.unavailable",
        )
        upper = (
            position[0]
            if position is not None
            else _alias_upper_bound(
                connection,
                live.user.tenant_id,
            )
        )
        last = position[1] if position is not None else None
        rows = _policy_page_rows(
            connection,
            tenant_id=live.user.tenant_id,
            upper=upper,
            last=last,
            limit=normalized_limit + 1,
        )
        page = rows[:normalized_limit]
        has_more = len(rows) > normalized_limit
        next_cursor = None
        if has_more and page and upper is not None:
            tail = page[-1]
            next_cursor = _encode_cursor(
                upper=upper,
                last=(
                    str(tail["environment_code"]),
                    str(tail["alias"]),
                    str(tail["model_alias_id"]),
                ),
                limit=normalized_limit,
                secret=config.auth_token_secret,
                tenant_id=live.user.tenant_id,
            )
        result = CachePolicyListResponse(
            tenant_id=live.user.tenant_id,
            items=[_policy_list_item(row, live.user.tenant_id) for row in page],
            limit=normalized_limit,
            next_cursor=next_cursor,
            has_more=has_more,
            global_enabled=bool(getattr(config, "response_cache_enabled", False)),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        return result
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except ApiError:
        connection.rollback()
        raise
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None
    finally:
        connection.close()


def patch_cache_policy(
    config: AppConfig,
    context: AuthContext,
    payload: PatchCachePolicyRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> CachePolicyPatchResponse:
    """在一个写事务内完成实时 RBAC、幂等 replay、CAS、围栏、Audit 与 outbox。"""

    normalized_tags = sorted(payload.tags)
    fingerprint_fields = {
        "model_alias_id": payload.model_alias_id,
        "status": payload.status,
        "data_class": payload.data_class,
        "single_data_class_confirmed": payload.single_data_class_confirmed,
        "ttl_seconds": payload.ttl_seconds,
        "charge_cache_hits": payload.charge_cache_hits,
        "tags": normalized_tags,
        "expected_version": payload.expected_version,
    }
    candidates = _intent_candidates(
        config,
        tenant_id=context.user.tenant_id,
        actor_user_id=context.user.user_id,
        idempotency_key=idempotency_key,
        operation="patch_policy",
        fields=fingerprint_fields,
    )
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    response: CachePolicyPatchResponse | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _require_transaction_permissions(
            connection,
            context,
            ("response_cache.read", "response_cache.write"),
        )
        replay = _policy_replay(
            connection,
            tenant_id=live.user.tenant_id,
            actor_user_id=live.user.user_id,
            candidates=candidates,
            request_id=request_id,
            trace_id=trace_id,
        )
        if replay is not None:
            connection.commit()
            return replay

        _require_active_alias(connection, live.user.tenant_id, payload.model_alias_id)
        existing = _policy_row_by_alias(
            connection,
            live.user.tenant_id,
            payload.model_alias_id,
        )
        current_version = 0 if existing is None else int(existing["version"])
        if payload.expected_version != current_version:
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ApiError("response_cache.version_conflict")
        if payload.status == "enabled":
            _require_enabled_configuration(connection, config)

        now = _database_now(connection)
        active_intent = candidates[0]
        before = _policy_audit_summary(existing)
        changed = existing is None or not _policy_matches(
            existing,
            payload,
            normalized_tags,
        )
        invalidated_entry_count = 0
        fenced_fill_count = 0
        event_type = "policy_noop"
        if existing is None:
            policy_id = f"rcp_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO response_cache_policies(
                  cache_policy_id,tenant_id,model_alias_id,status,data_class,
                  single_data_class_confirmed,data_class_confirmed_by,
                  data_class_confirmed_version,ttl_seconds,charge_cache_hits,tags_json,
                  version,invalidation_generation,created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?)
                """,
                (
                    policy_id,
                    live.user.tenant_id,
                    payload.model_alias_id,
                    payload.status,
                    payload.data_class,
                    int(payload.single_data_class_confirmed),
                    live.user.user_id if payload.single_data_class_confirmed else None,
                    1 if payload.single_data_class_confirmed else None,
                    payload.ttl_seconds,
                    int(payload.charge_cache_hits),
                    _canonical_json(normalized_tags),
                    live.user.user_id,
                    live.user.user_id,
                    now,
                    now,
                ),
            )
            event_type = "policy_created"
        else:
            policy_id = str(existing["cache_policy_id"])
            if changed:
                next_version = current_version + 1
                connection.execute(
                    """
                    UPDATE response_cache_policies
                    SET status=?,data_class=?,single_data_class_confirmed=?,
                        data_class_confirmed_by=?,data_class_confirmed_version=?,
                        ttl_seconds=?,charge_cache_hits=?,tags_json=?,version=?,
                        invalidation_generation=invalidation_generation+1,
                        updated_by=?,updated_at=?
                    WHERE tenant_id=? AND cache_policy_id=? AND version=?
                    """,
                    (
                        payload.status,
                        payload.data_class,
                        int(payload.single_data_class_confirmed),
                        live.user.user_id if payload.single_data_class_confirmed else None,
                        next_version if payload.single_data_class_confirmed else None,
                        payload.ttl_seconds,
                        int(payload.charge_cache_hits),
                        _canonical_json(normalized_tags),
                        next_version,
                        live.user.user_id,
                        now,
                        live.user.tenant_id,
                        policy_id,
                        current_version,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
                    raise ApiError("response_cache.version_conflict")
                invalidated_entry_count, fenced_fill_count = _delete_policy_facts(
                    connection,
                    tenant_id=live.user.tenant_id,
                    policy_id=policy_id,
                )
                event_type = "policy_updated"

        policy_row = _policy_row_by_id(connection, live.user.tenant_id, policy_id)
        summary = _policy_summary(policy_row, live.user.tenant_id)
        after = _policy_audit_summary(policy_row)
        response = CachePolicyPatchResponse(
            tenant_id=live.user.tenant_id,
            cache_policy=summary,
            changed=changed,
            idempotent_replay=False,
            invalidated_entry_count=invalidated_entry_count,
            fenced_fill_count=fenced_fill_count,
            request_id=request_id,
            trace_id=trace_id,
        )
        _insert_policy_event(
            connection,
            tenant_id=live.user.tenant_id,
            policy_id=policy_id,
            actor_user_id=live.user.user_id,
            intent=active_intent,
            event_type=event_type,
            before=before,
            after=after,
            response=response,
            request_id=request_id,
            trace_id=trace_id,
            created_at=now,
        )
        if changed:
            action = (
                "response_cache.policy_created"
                if existing is None
                else "response_cache.policy_updated"
            )
            audit_record = record_audit_log(
                connection,
                live,
                action=action,
                object_type="response_cache_policy",
                object_id=policy_id,
                object_name=None,
                before_summary=before,
                after_summary=after,
                request_meta=audit_request_meta,
                request_id=request_id,
                trace_id=trace_id,
            )
            if audit_record is None:
                # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
                raise ApiError("response_cache.unavailable")
            if existing is not None:
                _record_policy_invalidation(
                    connection,
                    live=live,
                    policy_id=policy_id,
                    intent=active_intent,
                    matched_entry_count=invalidated_entry_count,
                    deleted_entry_count=invalidated_entry_count,
                    fenced_fill_count=fenced_fill_count,
                    request_id=request_id,
                    trace_id=trace_id,
                    created_at=now,
                )
            commit_audited_transaction(connection)
        else:
            connection.commit()
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except ApiError:
        connection.rollback()
        raise
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except sqlite3.IntegrityError:
        connection.rollback()
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None
    finally:
        connection.close()
    if audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )
    if response is None:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    return response


def invalidate_response_cache(
    config: AppConfig,
    context: AuthContext,
    payload: InvalidateResponseCacheRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> CacheInvalidationResponse:
    """按 all 或 all-match selector 线性化失效，并持久化可重放的安全收据。"""

    selectors = sorted(payload.tags)
    fingerprint_fields = {
        "scope": payload.scope,
        "match_mode": payload.match_mode,
        "tags": selectors,
        "reason_code": payload.reason_code,
    }
    candidates = _intent_candidates(
        config,
        tenant_id=context.user.tenant_id,
        actor_user_id=context.user.user_id,
        idempotency_key=idempotency_key,
        operation="invalidate",
        fields=fingerprint_fields,
    )
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    response: CacheInvalidationResponse | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _require_transaction_permissions(
            connection,
            context,
            ("response_cache.read", "response_cache.invalidate"),
        )
        replay = _invalidation_replay(
            connection,
            tenant_id=live.user.tenant_id,
            actor_user_id=live.user.user_id,
            candidates=candidates,
            request_id=request_id,
            trace_id=trace_id,
        )
        if replay is not None:
            connection.commit()
            return replay

        now = _database_now(connection)
        active_intent = candidates[0]
        if payload.scope == "all":
            matched, deleted, fenced = _invalidate_all(
                connection,
                tenant_id=live.user.tenant_id,
                now=now,
            )
            selector_hmac = _selector_receipt(active_intent.digest, ["all"])
        else:
            cache_tags = [_parse_selector(value) for value in selectors]
            _validate_selector_owners(connection, live.user.tenant_id, cache_tags)
            tag_digests = build_tag_digests(
                _cache_hmac_keyring(config),
                tenant_id=live.user.tenant_id,
                selectors=tuple(cache_tags),
            )
            matched, deleted, fenced = _invalidate_tags(
                connection,
                tenant_id=live.user.tenant_id,
                tag_digests=tag_digests,
                selector_count=len(cache_tags),
                now=now,
            )
            selector_hmac = _selector_receipt(
                active_intent.digest,
                [
                    f"{item.hmac_key_version}:{item.kind}:{item.digest}"
                    for item in tag_digests
                ],
            )

        invalidation_id = f"rci_{uuid4().hex}"
        summary = CacheInvalidationSummary(
            cache_invalidation_id=invalidation_id,
            tenant_id=live.user.tenant_id,
            scope=payload.scope,
            match_mode=payload.match_mode,
            tag_count=len(selectors),
            matched_entry_count=matched,
            deleted_entry_count=deleted,
            fenced_fill_count=fenced,
            reason_code=payload.reason_code,
            created_at=_time(now),
        )
        response = CacheInvalidationResponse(
            tenant_id=live.user.tenant_id,
            invalidation=summary,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        _insert_invalidation(
            connection,
            summary=summary,
            actor_type="user",
            actor_user_id=live.user.user_id,
            intent=active_intent,
            selector_hmac=selector_hmac,
            response=response,
            request_id=request_id,
            trace_id=trace_id,
            created_at=now,
        )
        enqueue_response_cache_event(
            connection,
            tenant_id=live.user.tenant_id,
            event_type="cache.invalidated",
            object_id=invalidation_id,
            actor_user_id=live.user.user_id,
            trace_id=trace_id,
            source_request_id=request_id,
            payload=_invalidation_event_payload(summary),
            created_at=now,
        )
        audit_record = record_audit_log(
            connection,
            live,
            action="response_cache.invalidated",
            object_type="response_cache_invalidation",
            object_id=invalidation_id,
            object_name=None,
            before_summary={},
            after_summary=_invalidation_audit_summary(summary),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        if audit_record is None:
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ApiError("response_cache.unavailable")
        commit_audited_transaction(connection)
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except ApiError:
        connection.rollback()
        raise
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except sqlite3.IntegrityError:
        connection.rollback()
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None
    finally:
        connection.close()
    if audit_record is not None:
        dispatch_audit_recorded(
            config,
            context,
            audit_record,
            request_id=request_id,
            trace_id=trace_id,
        )
    if response is None:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    return response


def _require_transaction_permissions(
    connection: sqlite3.Connection,
    context: AuthContext,
    permission_codes: tuple[str, ...],
) -> AuthContext:
    """在同一写事务内逐项重载完整权限快照，关闭依赖后撤权竞态。"""

    live = context
    for permission_code in permission_codes:
        live = require_transaction_permission(
            connection,
            live,
            permission_code,
            unavailable_error_code="response_cache.unavailable",
        )
    return live


def _intent_candidates(
    config: AppConfig,
    *,
    tenant_id: str,
    actor_user_id: str,
    idempotency_key: str,
    operation: str,
    fields: dict[str, object],
) -> tuple[_IntentCandidate, ...]:
    """以 active-first retained keyring 构造全部可验证 owner，轮换不能丢失旧重放。"""

    try:
        digests = build_control_idempotency_digests(
            _control_keyring(config),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
        )
        if not digests:
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        return tuple(
            _IntentCandidate(
                version=int(item.version),
                digest=str(item.digest),
                fingerprint=_intent_fingerprint(str(item.digest), operation, fields),
            )
            for item in digests
        )
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (AttributeError, TypeError, ValueError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.configuration_unavailable") from None


def _control_keyring(config: AppConfig) -> tuple[VersionedResponseCacheSecret, ...]:
    """读取独立 Control 幂等 active/previous secrets，禁止回退 Auth 或 cache-key Secret。

    缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。 当前定义负责 _control_keyring 对应步骤。"""

    values = getattr(config, "response_cache_control_idempotency_keyring", ())
    if type(values) is not tuple or not values:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("missing response cache Control idempotency keyring")
    return values


def _cache_hmac_keyring(config: AppConfig) -> tuple[VersionedResponseCacheSecret, ...]:
    """标签摘要只使用当前 Response Cache HMAC；停服轮换会先 purge 所有旧派生事实。"""

    key = getattr(config, "response_cache_hmac_key", None)
    if key is None:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.configuration_unavailable") from None
    return (key,)


def _intent_fingerprint(digest: str, operation: str, fields: dict[str, object]) -> str:
    """以 secret-derived owner 摘要为 HMAC key，低熵配置值不能被离线枚举。"""

    payload = _canonical_json(
        {"schema_version": 1, "operation": operation, "fields": fields}
    ).encode("ascii")
    try:
        key = bytes.fromhex(digest)
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except ValueError:
        key = digest.encode("ascii", errors="strict")
    if len(key) < 16:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid idempotency digest")
    return hmac.new(key, _INTENT_DOMAIN + payload, hashlib.sha256).hexdigest()


def _selector_receipt(intent_digest: str, selectors: list[str]) -> str:
    """持久化不可逆 selector 集合收据，不保存 tag/data-class/owner 明文。"""

    return _intent_fingerprint(
        intent_digest,
        "selector_receipt",
        {"selectors": sorted(selectors)},
    )


def _require_enabled_configuration(
    connection: sqlite3.Connection,
    config: AppConfig,
) -> None:
    """核验全局开关、独立密钥和数据库 key-state，禁止旧进程启用新代际策略。"""

    hmac_key = getattr(config, "response_cache_hmac_key", None)
    encryption_key = getattr(config, "response_cache_encryption_key", None)
    if (
        not bool(getattr(config, "response_cache_enabled", False))
        or hmac_key is None
        or encryption_key is None
    ):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.configuration_unavailable")
    try:
        state = connection.execute(
            """
            SELECT key_schema_version,active_hmac_key_version,
                   active_encryption_key_version,hmac_secret_fingerprint,
                   encryption_secret_fingerprint,generation
            FROM response_cache_key_state WHERE key_state_id=1
            """
        ).fetchone()
        hmac_fingerprint = response_cache_secret_fingerprint(
            hmac_key,
            purpose="hmac",
        )
        encryption_fingerprint = response_cache_secret_fingerprint(
            encryption_key,
            purpose="encryption",
        )
        if (
            state is not None
            and str(state["hmac_secret_fingerprint"]) == _ZERO_FINGERPRINT
            and str(state["encryption_secret_fingerprint"]) == _ZERO_FINGERPRINT
        ):
            # 首次启用必须由已经持有写锁的 Control 事务初始化 key-state；否则管理员
            # 无法先创建 enabled Policy，而 Gateway 又没有可执行 Policy 来触发初始化。
            derived_count = int(
                connection.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM response_cache_entries)
                         + (SELECT COUNT(*) FROM response_cache_entry_tags)
                         + (SELECT COUNT(*) FROM response_cache_tag_generations)
                         + (SELECT COUNT(*) FROM response_cache_key_generations)
                         + (SELECT COUNT(*) FROM response_cache_fill_leases)
                         + (SELECT COUNT(*) FROM response_cache_fill_tags)
                    """
                ).fetchone()[0]
            )
            if derived_count != 0:
                # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
                raise ApiError("response_cache.configuration_unavailable")
            connection.execute(
                """
                UPDATE response_cache_key_state
                SET active_hmac_key_version=?,active_encryption_key_version=?,
                    hmac_secret_fingerprint=?,encryption_secret_fingerprint=?,
                    generation=generation+1,updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE key_state_id=1
                """,
                (
                    hmac_key.version,
                    encryption_key.version,
                    hmac_fingerprint,
                    encryption_fingerprint,
                ),
            )
            state = connection.execute(
                """
                SELECT key_schema_version,active_hmac_key_version,
                       active_encryption_key_version,hmac_secret_fingerprint,
                       encryption_secret_fingerprint,generation
                FROM response_cache_key_state WHERE key_state_id=1
                """
            ).fetchone()
        if (
            state is None
            or int(state["key_schema_version"]) != 1
            or int(state["active_hmac_key_version"]) != hmac_key.version
            or int(state["active_encryption_key_version"]) != encryption_key.version
            or not hmac.compare_digest(
                str(state["hmac_secret_fingerprint"]),
                hmac_fingerprint,
            )
            or not hmac.compare_digest(
                str(state["encryption_secret_fingerprint"]),
                encryption_fingerprint,
            )
        ):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ApiError("response_cache.configuration_unavailable")
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except ApiError:
        raise
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (sqlite3.Error, TypeError, ValueError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.configuration_unavailable") from None


def _require_active_alias(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_alias_id: str,
) -> sqlite3.Row:
    """Alias 与 Environment 必须同 tenant 且 active；跨租户和不存在保持同形 404。"""

    row = connection.execute(
        """
        SELECT alias.model_alias_id
        FROM model_aliases alias
        JOIN environments environment
          ON environment.tenant_id=alias.tenant_id
         AND environment.environment_id=alias.environment_id
        WHERE alias.tenant_id=? AND alias.model_alias_id=?
          AND alias.status='active' AND environment.status='active'
        """,
        (tenant_id, model_alias_id),
    ).fetchone()
    if row is None:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.policy_not_found")
    return row


def _policy_row_by_alias(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_alias_id: str,
) -> sqlite3.Row | None:
    """按 tenant+Alias 唯一 owner 读取策略及当前 ready entry 计数。"""

    return connection.execute(
        """
        SELECT policy.*,
          (SELECT COUNT(*) FROM response_cache_entries entry
           WHERE entry.tenant_id=policy.tenant_id
             AND entry.cache_policy_id=policy.cache_policy_id
             AND unixepoch(entry.expires_at)>unixepoch('now')) AS ready_entry_count
        FROM response_cache_policies policy
        WHERE policy.tenant_id=? AND policy.model_alias_id=?
        """,
        (tenant_id, model_alias_id),
    ).fetchone()


def _policy_row_by_id(
    connection: sqlite3.Connection,
    tenant_id: str,
    policy_id: str,
) -> sqlite3.Row:
    """写后按 tenant-qualified ID 重读权威响应；缺失表示事务完整性失败。"""

    row = connection.execute(
        """
        SELECT policy.*,
          (SELECT COUNT(*) FROM response_cache_entries entry
           WHERE entry.tenant_id=policy.tenant_id
             AND entry.cache_policy_id=policy.cache_policy_id
             AND unixepoch(entry.expires_at)>unixepoch('now')) AS ready_entry_count
        FROM response_cache_policies policy
        WHERE policy.tenant_id=? AND policy.cache_policy_id=?
        """,
        (tenant_id, policy_id),
    ).fetchone()
    if row is None:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("missing response cache policy")
    return row


def _policy_matches(
    row: sqlite3.Row,
    payload: PatchCachePolicyRequest,
    tags: list[str],
) -> bool:
    """比较完整规范化快照；只有真实变化才推进 version/generation。"""

    return (
        str(row["status"]) == payload.status
        and row["data_class"] == payload.data_class
        and bool(row["single_data_class_confirmed"]) == payload.single_data_class_confirmed
        and int(row["ttl_seconds"]) == payload.ttl_seconds
        and bool(row["charge_cache_hits"]) == payload.charge_cache_hits
        and _tags(row["tags_json"]) == tags
    )


def _delete_policy_facts(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    policy_id: str,
) -> tuple[int, int]:
    """策略变化在同事务删除 entry/fill；返回提交后可精确重放的冻结计数。"""

    entry_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM response_cache_entries WHERE tenant_id=? AND cache_policy_id=?",
            (tenant_id, policy_id),
        ).fetchone()[0]
    )
    fill_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM response_cache_fill_leases
            WHERE tenant_id=? AND cache_policy_id=?
            """,
            (tenant_id, policy_id),
        ).fetchone()[0]
    )
    connection.execute(
        "DELETE FROM response_cache_entries WHERE tenant_id=? AND cache_policy_id=?",
        (tenant_id, policy_id),
    )
    connection.execute(
        "DELETE FROM response_cache_fill_leases WHERE tenant_id=? AND cache_policy_id=?",
        (tenant_id, policy_id),
    )
    return entry_count, fill_count


def _insert_policy_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    policy_id: str,
    actor_user_id: str,
    intent: _IntentCandidate,
    event_type: str,
    before: dict[str, object],
    after: dict[str, object],
    response: CachePolicyPatchResponse,
    request_id: str,
    trace_id: str,
    created_at: str,
) -> None:
    """持久化 PATCH 的完整安全业务快照，使提交后丢响应仍能返回原计数。"""

    connection.execute(
        """
        INSERT INTO response_cache_policy_events(
          response_cache_policy_event_id,tenant_id,cache_policy_id,actor_user_id,
          idempotency_hmac_version,idempotency_key_hmac,request_fingerprint,event_type,
          before_json,after_json,response_json,result_version,changed,
          invalidated_entry_count,fenced_fill_count,operation_request_id,
          operation_trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"rcpevt_{uuid4().hex}",
            tenant_id,
            policy_id,
            actor_user_id,
            intent.version,
            intent.digest,
            intent.fingerprint,
            event_type,
            _canonical_json(before),
            _canonical_json(after),
            _canonical_json(_policy_business_snapshot(response)),
            response.cache_policy.version,
            int(response.changed),
            response.invalidated_entry_count,
            response.fenced_fill_count,
            request_id,
            trace_id,
            created_at,
        ),
    )


def _record_policy_invalidation(
    connection: sqlite3.Connection,
    *,
    live: AuthContext,
    policy_id: str,
    intent: _IntentCandidate,
    matched_entry_count: int,
    deleted_entry_count: int,
    fenced_fill_count: int,
    request_id: str,
    trace_id: str,
    created_at: str,
) -> None:
    """策略真实变化形成独立 invalidation/event/outbox，但复用 PATCH intent 防止重复副作用。"""

    invalidation_id = f"rci_{uuid4().hex}"
    summary = CacheInvalidationSummary(
        cache_invalidation_id=invalidation_id,
        tenant_id=live.user.tenant_id,
        scope="tags",
        match_mode="all",
        tag_count=1,
        matched_entry_count=matched_entry_count,
        deleted_entry_count=deleted_entry_count,
        fenced_fill_count=fenced_fill_count,
        reason_code="policy_changed",
        created_at=_time(created_at),
    )
    internal_response = CacheInvalidationResponse(
        tenant_id=live.user.tenant_id,
        invalidation=summary,
        idempotent_replay=False,
        request_id=request_id,
        trace_id=trace_id,
    )
    _insert_invalidation(
        connection,
        summary=summary,
        actor_type="user",
        actor_user_id=live.user.user_id,
        intent=intent,
        selector_hmac=_selector_receipt(intent.digest, [f"policy:{policy_id}"]),
        response=internal_response,
        request_id=request_id,
        trace_id=trace_id,
        created_at=created_at,
    )
    enqueue_response_cache_event(
        connection,
        tenant_id=live.user.tenant_id,
        event_type="cache.invalidated",
        object_id=invalidation_id,
        actor_user_id=live.user.user_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload=_invalidation_event_payload(summary),
        created_at=created_at,
    )


def _insert_invalidation(
    connection: sqlite3.Connection,
    *,
    summary: CacheInvalidationSummary,
    actor_type: str,
    actor_user_id: str | None,
    intent: _IntentCandidate,
    selector_hmac: str,
    response: CacheInvalidationResponse,
    request_id: str,
    trace_id: str,
    created_at: str,
) -> None:
    """写入 actor-scoped 幂等失效 owner；明文 selector 只停留在当前调用栈。"""

    connection.execute(
        """
        INSERT INTO response_cache_invalidations(
          cache_invalidation_id,tenant_id,actor_type,actor_user_id,scope,match_mode,
          selector_hmac,tag_count,matched_entry_count,deleted_entry_count,fenced_fill_count,
          reason_code,idempotency_hmac_version,idempotency_key_hmac,request_fingerprint,
          response_json,operation_request_id,operation_trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            summary.cache_invalidation_id,
            summary.tenant_id,
            actor_type,
            actor_user_id,
            summary.scope,
            summary.match_mode,
            selector_hmac,
            summary.tag_count,
            summary.matched_entry_count,
            summary.deleted_entry_count,
            summary.fenced_fill_count,
            summary.reason_code,
            intent.version,
            intent.digest,
            intent.fingerprint,
            _canonical_json(_invalidation_business_snapshot(response)),
            request_id,
            trace_id,
            created_at,
        ),
    )


def _policy_replay(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_user_id: str,
    candidates: tuple[_IntentCandidate, ...],
    request_id: str,
    trace_id: str,
) -> CachePolicyPatchResponse | None:
    """跨 active/retained 版本定位唯一 PATCH owner，并拒绝跨端点复用同一 key。"""

    matches: list[tuple[sqlite3.Row, _IntentCandidate]] = []
    for candidate in candidates:
        rows = connection.execute(
            """
            SELECT * FROM response_cache_policy_events
            WHERE tenant_id=? AND actor_user_id=? AND idempotency_hmac_version=?
              AND idempotency_key_hmac=?
            """,
            (tenant_id, actor_user_id, candidate.version, candidate.digest),
        ).fetchall()
        matches.extend((row, candidate) for row in rows)
        _reject_foreign_intent(
            connection,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            candidate=candidate,
            table="response_cache_invalidations",
        )
    if not matches:
        return None
    if len(matches) != 1:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    row, candidate = matches[0]
    if not hmac.compare_digest(str(row["request_fingerprint"]), candidate.fingerprint):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.request_conflict")
    return _policy_response_from_snapshot(
        row,
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def _invalidation_replay(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_user_id: str,
    candidates: tuple[_IntentCandidate, ...],
    request_id: str,
    trace_id: str,
) -> CacheInvalidationResponse | None:
    """定位唯一手工失效 owner；policy_changed 行只在相同 PATCH 指纹时可共存。"""

    matches: list[tuple[sqlite3.Row, _IntentCandidate]] = []
    for candidate in candidates:
        rows = connection.execute(
            """
            SELECT * FROM response_cache_invalidations
            WHERE tenant_id=? AND actor_type='user' AND actor_user_id=?
              AND idempotency_hmac_version=? AND idempotency_key_hmac=?
            """,
            (tenant_id, actor_user_id, candidate.version, candidate.digest),
        ).fetchall()
        matches.extend((row, candidate) for row in rows)
        _reject_foreign_intent(
            connection,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            candidate=candidate,
            table="response_cache_policy_events",
        )
    if not matches:
        return None
    if len(matches) != 1:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    row, candidate = matches[0]
    if not hmac.compare_digest(str(row["request_fingerprint"]), candidate.fingerprint):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.request_conflict")
    return _invalidation_response_from_snapshot(
        row,
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def _reject_foreign_intent(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_user_id: str,
    candidate: _IntentCandidate,
    table: str,
) -> None:
    """同 actor/key 在另一个写端点出现不同指纹时保持全局 intent 冲突语义。"""

    if table == "response_cache_invalidations":
        rows = connection.execute(
            """
            SELECT request_fingerprint FROM response_cache_invalidations
            WHERE tenant_id=? AND actor_type='user' AND actor_user_id=?
              AND idempotency_hmac_version=? AND idempotency_key_hmac=?
            """,
            (tenant_id, actor_user_id, candidate.version, candidate.digest),
        ).fetchall()
    elif table == "response_cache_policy_events":
        rows = connection.execute(
            """
            SELECT request_fingerprint FROM response_cache_policy_events
            WHERE tenant_id=? AND actor_user_id=? AND idempotency_hmac_version=?
              AND idempotency_key_hmac=?
            """,
            (tenant_id, actor_user_id, candidate.version, candidate.digest),
        ).fetchall()
    else:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid idempotency table")
    if any(
        not hmac.compare_digest(str(row["request_fingerprint"]), candidate.fingerprint)
        for row in rows
    ):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.request_conflict")


def _policy_business_snapshot(response: CachePolicyPatchResponse) -> dict[str, object]:
    """保存 0069 白名单允许的完整响应外壳；计数另由不可变事件列冻结。"""

    return {
        "tenant_id": response.tenant_id,
        "cache_policy": response.cache_policy.model_dump(mode="json"),
        "idempotent_replay": False,
        "request_id": response.request_id,
        "trace_id": response.trace_id,
    }


def _invalidation_business_snapshot(response: CacheInvalidationResponse) -> dict[str, object]:
    """保存 0069 白名单允许的完整失效响应，重放时只替换本次 attempt 链路。"""

    return {
        "tenant_id": response.tenant_id,
        "invalidation": response.invalidation.model_dump(mode="json"),
        "idempotent_replay": False,
        "request_id": response.request_id,
        "trace_id": response.trace_id,
    }


def _policy_response_from_snapshot(
    row: sqlite3.Row,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> CachePolicyPatchResponse:
    """严格验证持久 snapshot owner，再换成本次 attempt 的链路 ID。"""

    decoded = _json_object(row["response_json"])
    if set(decoded) != {
        "tenant_id",
        "cache_policy",
        "idempotent_replay",
        "request_id",
        "trace_id",
    } or (
        decoded["tenant_id"] != tenant_id
        or decoded["idempotent_replay"] is not False
        or type(decoded["request_id"]) is not str
        or type(decoded["trace_id"]) is not str
    ):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    try:
        # snapshot 是持久 JSON；strict model 的 datetime 只能从 JSON 字符串恢复，
        # 不能经 Python-object 入口把合法 RFC 3339 时间误判为类型漂移。
        policy = CachePolicySummary.model_validate_json(
            _canonical_json(decoded["cache_policy"])
        )
        if (
            policy.tenant_id != tenant_id
            or policy.cache_policy_id != str(row["cache_policy_id"])
            or policy.version != int(row["result_version"])
        ):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        return CachePolicyPatchResponse(
            tenant_id=tenant_id,
            cache_policy=policy,
            changed=bool(row["changed"]),
            idempotent_replay=True,
            invalidated_entry_count=int(row["invalidated_entry_count"]),
            fenced_fill_count=int(row["fenced_fill_count"]),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (TypeError, ValueError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None


def _invalidation_response_from_snapshot(
    row: sqlite3.Row,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> CacheInvalidationResponse:
    """严格验证失效 snapshot，不按当前数据库重新计算历史计数。"""

    decoded = _json_object(row["response_json"])
    if set(decoded) != {
        "tenant_id",
        "invalidation",
        "idempotent_replay",
        "request_id",
        "trace_id",
    } or (
        decoded["tenant_id"] != tenant_id
        or decoded["idempotent_replay"] is not False
        or type(decoded["request_id"]) is not str
        or type(decoded["trace_id"]) is not str
    ):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    try:
        summary = CacheInvalidationSummary.model_validate_json(
            _canonical_json(decoded["invalidation"])
        )
        if (
            summary.tenant_id != tenant_id
            or summary.cache_invalidation_id != str(row["cache_invalidation_id"])
            or summary.scope != str(row["scope"])
            or summary.match_mode != str(row["match_mode"])
            or summary.tag_count != int(row["tag_count"])
            or summary.matched_entry_count != int(row["matched_entry_count"])
            or summary.deleted_entry_count != int(row["deleted_entry_count"])
            or summary.fenced_fill_count != int(row["fenced_fill_count"])
            or summary.reason_code != str(row["reason_code"])
        ):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        return CacheInvalidationResponse(
            tenant_id=tenant_id,
            invalidation=summary,
            idempotent_replay=True,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (TypeError, ValueError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None


def _invalidate_all(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    now: str,
) -> tuple[int, int, int]:
    """全量推进 tenant 的 tag/key 围栏并删除 entry/fill，阻止旧 owner 迟到写回。"""

    matched = int(
        connection.execute(
            "SELECT COUNT(*) FROM response_cache_entries WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )
    fenced = int(
        connection.execute(
            "SELECT COUNT(*) FROM response_cache_fill_leases WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )
    # 0069 的 policy trigger 把 version/generation 与真实配置变化绑定；手工全量失效因此推进
    # 每个在途 fill 必须冻结的 tag/key generations，而不能伪造一次策略配置更新。
    connection.execute(
        """
        UPDATE response_cache_tag_generations
        SET generation=generation+1,updated_at=? WHERE tenant_id=?
        """,
        (now, tenant_id),
    )
    connection.execute(
        """
        UPDATE response_cache_key_generations
        SET generation=generation+1,updated_at=? WHERE tenant_id=?
        """,
        (now, tenant_id),
    )
    connection.execute("DELETE FROM response_cache_entries WHERE tenant_id=?", (tenant_id,))
    deleted = int(connection.execute("SELECT changes()").fetchone()[0])
    connection.execute("DELETE FROM response_cache_fill_leases WHERE tenant_id=?", (tenant_id,))
    return matched, deleted, fenced


def _invalidate_tags(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    tag_digests: tuple[object, ...],
    selector_count: int,
    now: str,
) -> tuple[int, int, int]:
    """用临时 digest 表执行有界 all-match；0 match 仍先推进每个 tag generation。"""

    if not tag_digests or selector_count < 1:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.input_invalid")
    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS prd076_selected_tag_digests(
          hmac_key_version INTEGER NOT NULL,
          tag_kind TEXT NOT NULL,
          tag_digest TEXT NOT NULL,
          PRIMARY KEY(hmac_key_version,tag_kind,tag_digest)
        ) WITHOUT ROWID
        """
    )
    connection.execute("DELETE FROM prd076_selected_tag_digests")
    connection.executemany(
        "INSERT INTO prd076_selected_tag_digests VALUES(?,?,?)",
        [
            (int(item.hmac_key_version), str(item.kind), str(item.digest))
            for item in tag_digests
        ],
    )
    for item in tag_digests:
        connection.execute(
            """
            INSERT INTO response_cache_tag_generations(
              tenant_id,hmac_key_version,tag_kind,tag_digest,generation,updated_at
            ) VALUES(?,?,?,?,1,?)
            ON CONFLICT(tenant_id,hmac_key_version,tag_kind,tag_digest)
            DO UPDATE SET
              generation=response_cache_tag_generations.generation+1,
              updated_at=excluded.updated_at
            """,
            (
                tenant_id,
                int(item.hmac_key_version),
                str(item.kind),
                str(item.digest),
                now,
            ),
        )
    entry_match = """
        SELECT link.response_cache_entry_id
        FROM response_cache_entry_tags link
        JOIN prd076_selected_tag_digests selected
          ON selected.hmac_key_version=link.hmac_key_version
         AND selected.tag_kind=link.tag_kind AND selected.tag_digest=link.tag_digest
        WHERE link.tenant_id=?
        GROUP BY link.response_cache_entry_id
        HAVING COUNT(*)=?
    """
    fill_match = """
        SELECT link.response_cache_fill_lease_id
        FROM response_cache_fill_tags link
        JOIN prd076_selected_tag_digests selected
          ON selected.hmac_key_version=link.hmac_key_version
         AND selected.tag_kind=link.tag_kind AND selected.tag_digest=link.tag_digest
        WHERE link.tenant_id=?
        GROUP BY link.response_cache_fill_lease_id
        HAVING COUNT(*)=?
    """
    matched = int(
        connection.execute(
            f"SELECT COUNT(*) FROM ({entry_match})",
            (tenant_id, selector_count),
        ).fetchone()[0]
    )
    fenced = int(
        connection.execute(
            f"SELECT COUNT(*) FROM ({fill_match})",
            (tenant_id, selector_count),
        ).fetchone()[0]
    )
    connection.execute(
        f"""
        DELETE FROM response_cache_entries
        WHERE tenant_id=? AND response_cache_entry_id IN ({entry_match})
        """,
        (tenant_id, tenant_id, selector_count),
    )
    deleted = int(connection.execute("SELECT changes()").fetchone()[0])
    connection.execute(
        f"""
        DELETE FROM response_cache_fill_leases
        WHERE tenant_id=? AND response_cache_fill_lease_id IN ({fill_match})
        """,
        (tenant_id, tenant_id, selector_count),
    )
    return matched, deleted, fenced


def _parse_selector(value: str) -> CacheTag:
    """解析 exact namespace:value，明文只保留到 HMAC 构造完成。"""

    if type(value) is not str or value.count(":") != 1:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.input_invalid")
    kind, raw_value = value.split(":", 1)
    if kind not in _SELECTOR_KINDS or not raw_value:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.input_invalid")
    if kind == "policy":
        valid = _POLICY_ID.fullmatch(raw_value) is not None
    elif kind in {"data_class", "tag"}:
        valid = _MACHINE_CODE.fullmatch(raw_value) is not None
    else:
        valid = _RESOURCE_ID.fullmatch(raw_value) is not None
    if not valid:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.input_invalid")
    try:
        return CacheTag(kind=kind, value=raw_value)
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (TypeError, ValueError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.input_invalid") from None


def _validate_selector_owners(
    connection: sqlite3.Connection,
    tenant_id: str,
    selectors: list[CacheTag],
) -> None:
    """所有 ID namespace 都在 Session tenant 内验证，跨租户与不存在统一 404。"""

    tables = {
        "policy": ("response_cache_policies", "cache_policy_id"),
        "model_alias": ("model_aliases", "model_alias_id"),
        "model": ("models", "model_id"),
        "virtual_key": ("virtual_keys", "virtual_key_id"),
    }
    for selector in selectors:
        owner = tables.get(str(selector.kind))
        if owner is None:
            continue
        table, column = owner
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE tenant_id=? AND {column}=?",
            (tenant_id, str(selector.value)),
        ).fetchone()
        if row is None:
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ApiError("response_cache.policy_not_found")


def _invalidation_event_payload(summary: CacheInvalidationSummary) -> dict[str, object]:
    """构造 cache.invalidated exact payload，不包含 selector 或业务正文。

    缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。 当前定义负责 _invalidation_event_payload 对应步骤。"""

    return {
        "schema_version": 1,
        "cache_invalidation_id": summary.cache_invalidation_id,
        "scope": summary.scope,
        "match_mode": summary.match_mode,
        "tag_count": summary.tag_count,
        "matched_entry_count": summary.matched_entry_count,
        "deleted_entry_count": summary.deleted_entry_count,
        "fenced_fill_count": summary.fenced_fill_count,
        "reason_code": summary.reason_code,
    }


def _invalidation_audit_summary(summary: CacheInvalidationSummary) -> dict[str, object]:
    """Audit 只保存范围、固定 reason 与计数，不保存 tag value。"""

    return {
        "cache_invalidation_id": summary.cache_invalidation_id,
        "scope": summary.scope,
        "match_mode": summary.match_mode,
        "tag_count": summary.tag_count,
        "matched_entry_count": summary.matched_entry_count,
        "deleted_entry_count": summary.deleted_entry_count,
        "fenced_fill_count": summary.fenced_fill_count,
        "reason_code": summary.reason_code,
    }


def _policy_audit_summary(row: sqlite3.Row | None) -> dict[str, object]:
    """策略 Audit 允许配置 machine code，但排除 entry、digest 与 source owner。

    缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。 当前定义负责 _policy_audit_summary 对应步骤。"""

    if row is None:
        return {}
    return {
        "status": str(row["status"]),
        "data_class": row["data_class"],
        "ttl_seconds": int(row["ttl_seconds"]),
        "charge_cache_hits": bool(row["charge_cache_hits"]),
        # 通用 Audit 摘要坚持扁平标量；tags 已由 Policy 约束为短 machine code，
        # 使用规范排序的逗号串既可复核变化，又不会放宽审计容器或自由文本边界。
        "tags": ",".join(_tags(row["tags_json"])),
        "version": int(row["version"]),
        "invalidation_generation": int(row["invalidation_generation"]),
    }


def _alias_upper_bound(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> tuple[str, str, str] | None:
    """冻结第一页可见 active Alias 的最大排序键，cursor 不能越过该快照上界。"""

    row = connection.execute(
        """
        SELECT environment.code AS environment_code,alias.alias,alias.model_alias_id
        FROM model_aliases alias
        JOIN environments environment
          ON environment.tenant_id=alias.tenant_id
         AND environment.environment_id=alias.environment_id
        WHERE alias.tenant_id=? AND alias.status='active' AND environment.status='active'
        ORDER BY environment.code DESC,alias.alias DESC,alias.model_alias_id DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["environment_code"]), str(row["alias"]), str(row["model_alias_id"])


def _policy_page_rows(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    upper: tuple[str, str, str] | None,
    last: tuple[str, str, str] | None,
    limit: int,
) -> list[sqlite3.Row]:
    """使用稳定三元 keyset 读取一页；active Alias 为空时不执行无界查询。"""

    if upper is None:
        return []
    clauses = [
        "alias.tenant_id=?",
        "alias.status='active'",
        "environment.status='active'",
        """(environment.code<? OR (environment.code=? AND
        (alias.alias<? OR (alias.alias=? AND alias.model_alias_id<=?))))""",
    ]
    params: list[object] = [tenant_id, upper[0], upper[0], upper[1], upper[1], upper[2]]
    if last is not None:
        clauses.append(
            """(environment.code>? OR (environment.code=? AND
            (alias.alias>? OR (alias.alias=? AND alias.model_alias_id>?))))"""
        )
        params.extend([last[0], last[0], last[1], last[1], last[2]])
    params.append(limit)
    return connection.execute(
        f"""
        SELECT
          alias.tenant_id,alias.model_alias_id,alias.alias,
          environment.environment_id,environment.code AS environment_code,
          policy.cache_policy_id AS p_cache_policy_id,
          policy.tenant_id AS p_tenant_id,
          policy.model_alias_id AS p_model_alias_id,
          policy.status AS p_status,policy.data_class AS p_data_class,
          policy.single_data_class_confirmed AS p_single_data_class_confirmed,
          policy.ttl_seconds AS p_ttl_seconds,
          policy.charge_cache_hits AS p_charge_cache_hits,
          policy.tags_json AS p_tags_json,policy.version AS p_version,
          policy.updated_at AS p_updated_at,
          CASE WHEN policy.cache_policy_id IS NULL THEN 0 ELSE (
            SELECT COUNT(*) FROM response_cache_entries entry
            WHERE entry.tenant_id=policy.tenant_id
              AND entry.cache_policy_id=policy.cache_policy_id
              AND unixepoch(entry.expires_at)>unixepoch('now')
          ) END AS p_ready_entry_count
        FROM model_aliases alias
        JOIN environments environment
          ON environment.tenant_id=alias.tenant_id
         AND environment.environment_id=alias.environment_id
        LEFT JOIN response_cache_policies policy
          ON policy.tenant_id=alias.tenant_id
         AND policy.model_alias_id=alias.model_alias_id
        WHERE {" AND ".join(clauses)}
        ORDER BY environment.code ASC,alias.alias ASC,alias.model_alias_id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _policy_list_item(row: sqlite3.Row, tenant_id: str) -> CachePolicyListItem:
    """映射列表行并显式复核 envelope/item/policy tenant 与 Alias 镜像。"""

    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    if str(row["tenant_id"]) != tenant_id:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("tenant mirror mismatch")
    policy = None
    if row["p_cache_policy_id"] is not None:
        policy = _policy_summary(row, tenant_id, prefix="p_")
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        if policy.model_alias_id != str(row["model_alias_id"]):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError("alias mirror mismatch")
    return CachePolicyListItem(
        tenant_id=tenant_id,
        model_alias_id=str(row["model_alias_id"]),
        alias=str(row["alias"]),
        environment_id=str(row["environment_id"]),
        environment_code=str(row["environment_code"]),
        cache_policy=policy,
    )


def _policy_summary(
    row: sqlite3.Row,
    tenant_id: str,
    *,
    prefix: str = "",
) -> CachePolicySummary:
    """从白名单列构造 GET/PATCH 共用 DTO，任何 owner 漂移都失败关闭。"""

    def field(name: str) -> object:
        """缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。 当前定义负责 field 对应步骤。"""
        return row[f"{prefix}{name}"]

    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    if str(field("tenant_id")) != tenant_id:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("policy tenant mismatch")
    return CachePolicySummary(
        cache_policy_id=str(field("cache_policy_id")),
        tenant_id=tenant_id,
        model_alias_id=str(field("model_alias_id")),
        status=str(field("status")),
        data_class=field("data_class"),
        single_data_class_confirmed=bool(field("single_data_class_confirmed")),
        ttl_seconds=int(field("ttl_seconds")),
        charge_cache_hits=bool(field("charge_cache_hits")),
        tags=_tags(field("tags_json")),
        version=int(field("version")),
        ready_entry_count=int(field("ready_entry_count")),
        updated_at=_time(field("updated_at")),
    )


def _limit(value: object) -> int:
    """规范化 1..100 的十进制 limit，bool/符号/空白均拒绝。"""

    if value is None:
        return 50
    if type(value) is not str or not value.isascii() or not value.isdigit():
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.query_invalid")
    parsed = int(value)
    if not 1 <= parsed <= 100:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.query_invalid")
    return parsed


def _encode_cursor(
    *,
    upper: tuple[str, str, str],
    last: tuple[str, str, str],
    limit: int,
    secret: str,
    tenant_id: str,
) -> str:
    """签名 tenant、排序、limit、快照上界和最后键，客户端不能跨 owner 改写。"""

    payload = _canonical_json(
        {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "sort": "environment_code,alias,model_alias_id",
            "limit": limit,
            "upper": list(upper),
            "last": list(last),
        }
    ).encode("ascii")
    signature = hmac.new(_secret_bytes(secret), _CURSOR_DOMAIN + payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_cursor(
    value: object,
    *,
    secret: str,
    tenant_id: str,
    limit: int,
) -> tuple[tuple[str, str, str], tuple[str, str, str]] | None:
    """验证 opaque cursor 的签名和全部绑定字段，失败固定 query_invalid。"""

    if value is None:
        return None
    if type(value) is not str or not value or len(value) > _CURSOR_MAX_LENGTH:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.query_invalid")
    try:
        if any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) <= 32:
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(
            _secret_bytes(secret),
            _CURSOR_DOMAIN + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        decoded = json.loads(payload.decode("ascii"))
        if (
            type(decoded) is not dict
            or set(decoded)
            != {
                "schema_version",
                "tenant_id",
                "sort",
                "limit",
                "upper",
                "last",
            }
            or (
                decoded["schema_version"] != 1
                or decoded["tenant_id"] != tenant_id
                or decoded["sort"] != "environment_code,alias,model_alias_id"
                or decoded["limit"] != limit
            )
        ):
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        upper = _cursor_tuple(decoded["upper"])
        last = _cursor_tuple(decoded["last"])
        if last > upper:
            # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
            raise ValueError
        return upper, last
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.query_invalid") from None


def _cursor_tuple(value: object) -> tuple[str, str, str]:
    """游标排序键必须是三个短非空字符串，不能携带额外状态。"""

    if (
        type(value) is not list
        or len(value) != 3
        or any(type(item) is not str or not item or len(item) > 128 for item in value)
    ):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid response cache cursor tuple")
    return value[0], value[1], value[2]


def _tags(value: object) -> list[str]:
    """严格解析数据库 canonical tags；损坏事实不能静默显示为空。"""

    try:
        decoded = json.loads(str(value))
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (TypeError, ValueError, json.JSONDecodeError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid response cache tags") from None
    if (
        type(decoded) is not list
        or len(decoded) > 16
        or any(type(item) is not str or _MACHINE_CODE.fullmatch(item) is None for item in decoded)
        or decoded != sorted(set(decoded))
    ):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid response cache tags")
    return decoded


def _json_object(value: object) -> dict[str, object]:
    """读取数据库 snapshot 时只接受 JSON object，禁止宽松容错损坏事实。"""

    try:
        decoded = json.loads(str(value))
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except (TypeError, ValueError, json.JSONDecodeError):
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None
    if type(decoded) is not dict:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable")
    return decoded


def _canonical_json(value: object) -> str:
    """内部安全配置与收据采用稳定 ASCII JSON，绝不写入 Prompt/Response/Key。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _database_now(connection: sqlite3.Connection) -> str:
    """所有管理事实使用同一 SQLite UTC 秒时钟，客户端时间不可信。"""

    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None or type(row[0]) is not str:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("database time unavailable")
    _time(row[0])
    return row[0]


def _time(value: object) -> datetime:
    """解析 exact UTC 时间并归一到 timezone-aware datetime。

    缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。 当前定义负责 _time 对应步骤。"""

    if type(value) is not str:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid response cache time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid response cache time")
    return parsed.astimezone(UTC)


def _secret_bytes(value: object) -> bytes:
    """游标签名只接受既有 Auth secret，值不进入异常或日志。"""

    if type(value) is not str:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid cursor secret")
    encoded = value.encode("utf-8", errors="strict")
    if not 16 <= len(encoded) <= 512:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ValueError("invalid cursor secret")
    return encoded


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建启用行访问和 FK 的项目标准连接；底层 URL 不进入业务错误。"""

    try:
        return connect_database(config.database_url)
    # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
    except DbError:
        # 缓存事务必须保持租户归属、幂等和审计事实一致，失败时只返回固定错误码。
        raise ApiError("response_cache.unavailable") from None


__all__ = [
    "invalidate_response_cache",
    "list_cache_policies",
    "patch_cache_policy",
]
