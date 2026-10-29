"""PRD-076 Response Cache 的 SQLite lookup、lease 与 fill 事务。

本模块只在公共非流式 Chat 已通过输入安全闸门和 Rate Limit 后调用。Prompt、Response、
Credential、cache/tag digest、nonce 与密文仅存在于受控内存或专用表，任何异常和日志都只
使用固定错误码。命中在这里仍只是 candidate；只有 settlement 模块的原子事务才可返回正文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import UTC, datetime
from secrets import token_hex
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.response_cache import (
    CACHE_KEY_SCHEMA_VERSION,
    CacheInvalidatedWebhookPayload,
    EncryptedResponseCachePayload,
    ResponseCacheContractError,
    ResponseCacheEntryOwner,
    ResponseCacheIdentity,
    ResponseCacheIneligible,
    RouteRevisionInput,
    VersionedResponseCacheSecret,
    build_cache_identity_digest,
    build_cache_key_digest,
    build_prompt_hash,
    build_route_revision_digest,
    build_tag_digest,
    decrypt_response_payload,
    encrypt_response_payload,
    response_cache_secret_fingerprint,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.provider_adapter_spec import CATALOG_HASH, CATALOG_VERSION

from metahub_gateway.providers.base import NormalizedResponse, ProviderRequest
from metahub_gateway.providers.keypool import (
    response_cache_scope_matches_provider_selection,
    snapshot_response_cache_credential_scope,
)
from metahub_gateway.routing.models import GatewayRoute
from metahub_gateway.usage.ledger import RequestLogContext

from .eligibility import classify_response_cache_request, trusted_response_cache_tags
from .models import (
    PreparedResponseCache,
    ResponseCacheCandidate,
    ResponseCacheFillLease,
    ResponseCacheFillResult,
    ResponseCacheKeyState,
    ResponseCachePolicySnapshot,
    ResponseCacheTagGeneration,
)

logger = logging.getLogger("metahub.gateway.response_cache")

# 进程 owner 只用于诊断过期 lease 的来源，不包含主机名、路径、PID 或配置。
_PROCESS_LEASE_OWNER_ID = f"rcgw_{uuid4().hex}"
_ZERO_FINGERPRINT = "0" * 64


def prepare_response_cache(
    config: AppConfig,
    *,
    context: RequestLogContext,
    route: GatewayRoute,
    payload: dict[str, object],
    request_kind: str,
    public_virtual_key: bool,
    internal_invocation: bool,
    policy_decision: str,
    experiment_disposition: str,
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
) -> PreparedResponseCache:
    """读取一次精确缓存并在 miss 时争抢单写者 lease。

    返回值绝不表示已经命中：``hit_candidate`` 仍须执行当前 Output Safety、价格/预算冻结和
    原子 settlement。可选能力自身的数据库或合同故障默认返回 ``unavailable_bypass``，不能
    改变正常 Provider 请求；实验范围 ``unresolved`` 仍由调用方失败关闭。
    """

    structural = classify_response_cache_request(
        globally_enabled=config.response_cache_enabled,
        request_kind=request_kind,
        payload=payload,
        public_virtual_key=public_virtual_key,
        internal_invocation=internal_invocation,
        policy_decision=policy_decision,
        experiment_disposition=experiment_disposition,
        route=route,
    )
    if structural is not None:
        _safe_insert_initial_fact(config, context, status=structural, policy=None)
        return PreparedResponseCache(status=structural)

    hmac_key = config.response_cache_hmac_key
    encryption_key = config.response_cache_encryption_key
    if hmac_key is None or encryption_key is None:
        _safe_insert_initial_fact(config, context, status="unavailable_bypass", policy=None)
        return PreparedResponseCache(
            status="unavailable_bypass", reason_code="configuration_unavailable"
        )

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        key_state = _load_or_initialize_key_state(
            connection,
            hmac_key=hmac_key,
            encryption_key=encryption_key,
            now=now,
        )
        if key_state is None:
            _insert_initial_fact(
                connection, context, status="unavailable_bypass", policy=None, now=now
            )
            connection.commit()
            return PreparedResponseCache(
                status="unavailable_bypass", reason_code="key_state_mismatch"
            )

        policy = _load_enabled_policy(connection, context.tenant_id, route.model_alias_id)
        if policy is None:
            _insert_initial_fact(connection, context, status="disabled", policy=None, now=now)
            connection.commit()
            return PreparedResponseCache(status="disabled")

        credential_scope = snapshot_response_cache_credential_scope(
            config,
            connection,
            route,
            hmac_key=hmac_key,
            as_of=_parse_db_time(now),
        )
        if credential_scope is None:
            _insert_initial_fact(
                connection, context, status="ineligible", policy=policy, now=now
            )
            connection.commit()
            return PreparedResponseCache(status="ineligible", policy=policy)

        route_input = _route_revision_input(
            route,
            credential_scope=credential_scope,
            policy_snapshot_id=policy_snapshot_id,
            policy_snapshot_version=policy_snapshot_version,
            policy_snapshot_status=policy_snapshot_status,
        )
        prompt_hash = build_prompt_hash(payload, key=hmac_key)
        route_revision_digest = build_route_revision_digest(route_input, key=hmac_key)
        cache_identity_digest = build_cache_identity_digest(
            key=hmac_key,
            tenant_id=context.tenant_id,
            virtual_key_id=_required_virtual_key(context),
            data_class=policy.data_class,
            model_id=route.model_id,
            prompt_hash=prompt_hash,
            route_revision_digest=route_revision_digest,
            cache_policy_id=policy.cache_policy_id,
            cache_policy_version=policy.version,
            cache_policy_generation=policy.invalidation_generation,
        )
        key_generation = _ensure_key_generation(
            connection,
            tenant_id=context.tenant_id,
            hmac_key_version=hmac_key.version,
            cache_identity_digest=cache_identity_digest,
            now=now,
        )
        identity = _identity(
            hmac_key=hmac_key,
            prompt_hash=prompt_hash,
            route_revision_digest=route_revision_digest,
            cache_identity_digest=cache_identity_digest,
            key_generation=key_generation,
        )
        tag_generations = _ensure_tag_generations(
            connection,
            hmac_key=hmac_key,
            tenant_id=context.tenant_id,
            policy=policy,
            route=route,
            virtual_key_id=_required_virtual_key(context),
            now=now,
        )
        _insert_initial_fact(connection, context, status="eligible", policy=policy, now=now)
        _delete_expired_entries(connection, tenant_id=context.tenant_id, now=now)

        row = connection.execute(
            """
            SELECT *
            FROM response_cache_entries
            WHERE tenant_id=? AND key_schema_version=? AND cache_key_digest=?
            """,
            (context.tenant_id, CACHE_KEY_SCHEMA_VERSION, identity.cache_key_digest),
        ).fetchone()
        if row is not None:
            candidate = _load_candidate(
                connection,
                row=row,
                policy=policy,
                identity=identity,
                credential_scope_digest=credential_scope.digest,
                expected_tags=tag_generations,
                encryption_key=encryption_key,
                now=now,
            )
            if candidate is not None:
                _transition_fact(
                    connection,
                    context,
                    from_status="eligible",
                    to_status="hit_candidate",
                    now=now,
                )
                connection.commit()
                return PreparedResponseCache(
                    status="hit_candidate", policy=policy, candidate=candidate
                )

            # 密文、AAD、owner 或 tag generation 漂移都按安全投毒处理：先推进 key
            # generation 并形成 system invalidation，再为新 generation 争抢 lease。
            _fence_corrupt_entry(
                connection,
                context=context,
                policy=policy,
                identity=identity,
                hmac_key=hmac_key,
                now=now,
            )
            key_generation += 1
            identity = _identity(
                hmac_key=hmac_key,
                prompt_hash=prompt_hash,
                route_revision_digest=route_revision_digest,
                cache_identity_digest=cache_identity_digest,
                key_generation=key_generation,
            )
            lease = _reserve_fill_lease(
                connection,
                config=config,
                context=context,
                policy=policy,
                identity=identity,
                encryption_key_version=encryption_key.version,
                key_state_generation=key_state.generation,
                route_revision_digest=route_revision_digest,
                credential_scope_digest=credential_scope.digest,
                policy_snapshot_id=policy_snapshot_id,
                policy_snapshot_version=policy_snapshot_version,
                policy_snapshot_status=policy_snapshot_status,
                tags=tag_generations,
                now=now,
            )
            target = "corrupt_miss" if lease is not None else "miss_non_owner"
            _transition_fact(
                connection,
                context,
                from_status="eligible",
                to_status=target,
                now=now,
            )
            connection.commit()
            return PreparedResponseCache(
                status=target,
                policy=policy,
                fill_lease=lease,
                reason_code="integrity_failed",
            )

        lease = _reserve_fill_lease(
            connection,
            config=config,
            context=context,
            policy=policy,
            identity=identity,
            encryption_key_version=encryption_key.version,
            key_state_generation=key_state.generation,
            route_revision_digest=route_revision_digest,
            credential_scope_digest=credential_scope.digest,
            policy_snapshot_id=policy_snapshot_id,
            policy_snapshot_version=policy_snapshot_version,
            policy_snapshot_status=policy_snapshot_status,
            tags=tag_generations,
            now=now,
        )
        target = "miss_owner" if lease is not None else "miss_non_owner"
        _transition_fact(
            connection,
            context,
            from_status="eligible",
            to_status=target,
            now=now,
        )
        connection.commit()
        return PreparedResponseCache(status=target, policy=policy, fill_lease=lease)
    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    except (ResponseCacheContractError, ResponseCacheIneligible, sqlite3.Error, ValueError):
        if connection is not None:
            connection.rollback()
        _safe_insert_initial_fact(
            config, context, status="unavailable_bypass", policy=None
        )
        _log_optional_failure(context, "lookup_unavailable")
        return PreparedResponseCache(
            status="unavailable_bypass", reason_code="lookup_unavailable"
        )
    finally:
        if connection is not None:
            connection.close()


def commit_response_cache_fill(
    config: AppConfig,
    *,
    lease: ResponseCacheFillLease,
    context: RequestLogContext,
    route: GatewayRoute,
    provider_request: ProviderRequest,
    response: NormalizedResponse,
) -> ResponseCacheFillResult:
    """在 Provider Usage/FinOps 主库提交后机会性写入加密 entry。

    lease 或 Credential 任一漂移只会跳过缓存，不改变已经完成的模型响应。Fallback、spooled
    Usage、失败 RequestLog、超限正文和非 initial route 都不会满足本函数的 owner 查询。
    """

    try:
        plaintext = json.dumps(
            response.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    except (TypeError, ValueError, UnicodeError):
        release_response_cache_fill(config, lease=lease, context=context)
        return ResponseCacheFillResult("skipped")
    if not 1 <= len(plaintext) <= config.response_cache_max_plaintext_bytes:
        release_response_cache_fill(config, lease=lease, context=context)
        return ResponseCacheFillResult("skipped")

    hmac_key = config.response_cache_hmac_key
    encryption_key = config.response_cache_encryption_key
    if hmac_key is None or encryption_key is None:
        release_response_cache_fill(config, lease=lease, context=context)
        return ResponseCacheFillResult("skipped")

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        current_scope = snapshot_response_cache_credential_scope(
            config,
            connection,
            route,
            hmac_key=hmac_key,
            as_of=_parse_db_time(now),
        )
        if (
            current_scope is None
            or not hmac.compare_digest(current_scope.digest, lease.credential_scope_digest)
            or not response_cache_scope_matches_provider_selection(
                config,
                connection,
                route,
                scope_kind=current_scope.kind,
                provider_credential_id=provider_request.provider_credential_id,
                provider_key_source=provider_request.provider_key_source,
                as_of=_parse_db_time(now),
            )
        ):
            connection.rollback()
            release_response_cache_fill(config, lease=lease, context=context)
            return ResponseCacheFillResult("lease_lost")

        source = _load_committed_source(
            connection,
            context=context,
            route=route,
            response=response,
        )
        if source is None or not _lease_is_current(
            connection,
            lease=lease,
            route=route,
            hmac_key=hmac_key,
            encryption_key=encryption_key,
            credential_scope=current_scope,
            now=now,
        ):
            connection.rollback()
            release_response_cache_fill(config, lease=lease, context=context)
            return ResponseCacheFillResult("lease_lost")

        _delete_expired_entries(connection, tenant_id=lease.tenant_id, now=now)
        _evict_to_capacity(
            connection,
            tenant_id=lease.tenant_id,
            maximum=config.response_cache_max_ready_entries_per_tenant,
        )
        entry_id = f"rce_{uuid4().hex}"
        expires_at = str(
            connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ',?,'+'||?||' seconds')",
                (now, lease.policy.ttl_seconds),
            ).fetchone()[0]
        )
        owner = ResponseCacheEntryOwner(
            tenant_id=lease.tenant_id,
            entry_id=entry_id,
            cache_policy_id=lease.policy.cache_policy_id,
            cache_policy_version=lease.policy.version,
            cache_policy_generation=lease.policy.invalidation_generation,
            hmac_key_version=lease.identity.hmac_key_version,
            encryption_key_version=lease.encryption_key_version,
            cache_identity_digest=lease.identity.cache_identity_digest,
            cache_key_digest=lease.identity.cache_key_digest,
            key_generation=lease.identity.key_generation,
            model_alias_id=lease.policy.model_alias_id,
            model_id=route.model_id,
            provider_id=route.provider_id,
            route_revision_digest=lease.route_revision_digest,
            credential_revision_digest=lease.credential_scope_digest,
            source_request_id=context.request_id,
            source_trace_id=context.trace_id,
            source_usage_ledger_id=source["usage_ledger_id"],
            source_finops_ledger_entry_id=source["finops_ledger_entry_id"],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.total_tokens,
            source_generation_cost_status=source["cost_status"],
            source_generation_cost_cny_micros=source["cost_micros"],
            expires_at=expires_at,
            plaintext_byte_count=len(plaintext),
        )
        encrypted = encrypt_response_payload(
            plaintext,
            secret=encryption_key.secret,
            owner=owner,
            nonce_available=lambda nonce: _nonce_available(
                connection, encryption_key.version, nonce
            ),
        )
        connection.execute(
            """
            INSERT INTO response_cache_entries(
              response_cache_entry_id,tenant_id,cache_policy_id,cache_policy_version,
              policy_invalidation_generation,key_schema_version,hmac_key_version,
              encryption_key_version,cache_identity_digest,key_generation,cache_key_digest,
              model_alias_id,model_id,provider_id,route_revision_digest,
              credential_revision_digest,nonce,ciphertext,plaintext_byte_count,input_tokens,
              output_tokens,total_tokens,source_request_id,source_trace_id,
              source_usage_ledger_id,source_finops_ledger_entry_id,
              source_generation_cost_status,source_generation_cost_cny_micros,
              expires_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                owner.entry_id,
                owner.tenant_id,
                owner.cache_policy_id,
                owner.cache_policy_version,
                owner.cache_policy_generation,
                owner.key_schema_version,
                owner.hmac_key_version,
                owner.encryption_key_version,
                owner.cache_identity_digest,
                owner.key_generation,
                owner.cache_key_digest,
                owner.model_alias_id,
                owner.model_id,
                owner.provider_id,
                owner.route_revision_digest,
                lease.credential_scope_digest,
                encrypted.nonce,
                encrypted.ciphertext,
                owner.plaintext_byte_count,
                owner.input_tokens,
                owner.output_tokens,
                owner.total_tokens,
                owner.source_request_id,
                owner.source_trace_id,
                owner.source_usage_ledger_id,
                owner.source_finops_ledger_entry_id,
                owner.source_generation_cost_status,
                owner.source_generation_cost_cny_micros,
                owner.expires_at,
                now,
            ),
        )
        for frozen in lease.tags:
            connection.execute(
                """
                INSERT INTO response_cache_entry_tags(
                  tenant_id,hmac_key_version,response_cache_entry_id,tag_kind,
                  tag_digest,tag_generation
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    lease.tenant_id,
                    frozen.tag.hmac_key_version,
                    entry_id,
                    frozen.tag.kind,
                    frozen.tag.digest,
                    frozen.generation,
                ),
            )
        deleted = connection.execute(
            """
            DELETE FROM response_cache_fill_leases
            WHERE tenant_id=? AND response_cache_fill_lease_id=?
              AND lease_generation=? AND lease_token=?
            """,
            (
                lease.tenant_id,
                lease.fill_lease_id,
                lease.lease_generation,
                lease.lease_token,
            ),
        )
        if deleted.rowcount != 1:
            # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
            raise sqlite3.IntegrityError("response_cache_fill_lease_lost")
        _transition_fact(
            connection,
            context,
            from_status=("corrupt_miss", "miss_owner"),
            to_status="fill_stored",
            now=now,
        )
        connection.commit()
        return ResponseCacheFillResult("stored")
    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    except (ResponseCacheContractError, sqlite3.Error, ValueError):
        if connection is not None:
            connection.rollback()
        release_response_cache_fill(config, lease=lease, context=context)
        _log_optional_failure(context, "fill_skipped")
        return ResponseCacheFillResult("skipped")
    finally:
        if connection is not None:
            connection.close()


def release_response_cache_fill(
    config: AppConfig,
    *,
    lease: ResponseCacheFillLease,
    context: RequestLogContext,
) -> None:
    """按 tenant+token CAS 释放未使用 lease，并把安全请求事实收口为 fill_skipped。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        connection.execute(
            """
            DELETE FROM response_cache_fill_leases
            WHERE tenant_id=? AND response_cache_fill_lease_id=?
              AND lease_generation=? AND lease_token=?
            """,
            (
                lease.tenant_id,
                lease.fill_lease_id,
                lease.lease_generation,
                lease.lease_token,
            ),
        )
        _transition_fact(
            connection,
            context,
            from_status=("corrupt_miss", "miss_owner"),
            to_status="fill_skipped",
            now=now,
            required=False,
        )
        connection.commit()
    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
        _log_optional_failure(context, "lease_release_failed")
    finally:
        if connection is not None:
            connection.close()


def _route_revision_input(
    route: GatewayRoute,
    *,
    credential_scope: object,
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
) -> RouteRevisionInput:
    """把已冻结路由转换为 Shared exact DTO，避免在 repository 自行拼接摘要。"""

    return RouteRevisionInput(
        tenant_id=route.tenant_id,
        model_alias_id=str(route.model_alias_id or ""),
        resolved_model_id=route.model_id,
        provider_id=route.provider_id,
        provider_type=route.provider_type,
        provider_auth_type=route.provider_auth_type,
        provider_base_url=route.provider_base_url,
        upstream_model=route.upstream_model_name,
        capabilities={
            key: value
            for key, value in route.capabilities.items()
            if type(value) is bool
        },
        adapter_catalog_version=CATALOG_VERSION,
        adapter_catalog_hash=CATALOG_HASH,
        policy_snapshot_id=policy_snapshot_id,
        policy_snapshot_version=policy_snapshot_version,
        policy_snapshot_status=policy_snapshot_status,
        credential_scope=credential_scope,  # type: ignore[arg-type]
    )


def _load_or_initialize_key_state(
    connection: sqlite3.Connection,
    *,
    hmac_key: VersionedResponseCacheSecret,
    encryption_key: VersionedResponseCacheSecret,
    now: str,
) -> ResponseCacheKeyState | None:
    """只在零 fingerprint 且没有密文/lease 时初始化单例，否则要求配置 exact 匹配。"""

    row = connection.execute(
        "SELECT * FROM response_cache_key_state WHERE key_state_id=1"
    ).fetchone()
    if row is None:
        return None
    hmac_fingerprint = response_cache_secret_fingerprint(hmac_key, purpose="hmac")
    encryption_fingerprint = response_cache_secret_fingerprint(
        encryption_key, purpose="encryption"
    )
    if (
        row["hmac_secret_fingerprint"] == _ZERO_FINGERPRINT
        and row["encryption_secret_fingerprint"] == _ZERO_FINGERPRINT
    ):
        derived_count = int(
            connection.execute(
                """
                SELECT (SELECT COUNT(*) FROM response_cache_entries)
                     + (SELECT COUNT(*) FROM response_cache_fill_leases)
                """
            ).fetchone()[0]
        )
        if derived_count != 0:
            return None
        connection.execute(
            """
            UPDATE response_cache_key_state
            SET active_hmac_key_version=?,active_encryption_key_version=?,
                hmac_secret_fingerprint=?,encryption_secret_fingerprint=?,
                generation=generation+1,updated_at=?
            WHERE key_state_id=1
            """,
            (
                hmac_key.version,
                encryption_key.version,
                hmac_fingerprint,
                encryption_fingerprint,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM response_cache_key_state WHERE key_state_id=1"
        ).fetchone()
    if (
        int(row["key_schema_version"]) != CACHE_KEY_SCHEMA_VERSION
        or int(row["active_hmac_key_version"]) != hmac_key.version
        or int(row["active_encryption_key_version"]) != encryption_key.version
        or not hmac.compare_digest(str(row["hmac_secret_fingerprint"]), hmac_fingerprint)
        or not hmac.compare_digest(
            str(row["encryption_secret_fingerprint"]), encryption_fingerprint
        )
    ):
        return None
    return ResponseCacheKeyState(
        schema_version=int(row["key_schema_version"]),
        active_hmac_key_version=int(row["active_hmac_key_version"]),
        active_encryption_key_version=int(row["active_encryption_key_version"]),
        hmac_secret_fingerprint=str(row["hmac_secret_fingerprint"]),
        encryption_secret_fingerprint=str(row["encryption_secret_fingerprint"]),
        generation=int(row["generation"]),
    )


def _load_enabled_policy(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_alias_id: str | None,
) -> ResponseCachePolicySnapshot | None:
    """只读取当前 tenant+active Alias 的 enabled、已确认单一 data class 策略。"""

    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    if not model_alias_id:
        return None
    row = connection.execute(
        """
        SELECT policy.*
        FROM response_cache_policies policy
        JOIN model_aliases alias
          ON alias.tenant_id=policy.tenant_id
         AND alias.model_alias_id=policy.model_alias_id
        WHERE policy.tenant_id=? AND policy.model_alias_id=?
          AND policy.status='enabled' AND policy.single_data_class_confirmed=1
          AND alias.status='active'
        """,
        (tenant_id, model_alias_id),
    ).fetchone()
    if row is None:
        return None
    tags = json.loads(str(row["tags_json"]))
    if type(tags) is not list or any(type(tag) is not str for tag in tags):
        # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
        raise sqlite3.IntegrityError("response_cache_policy_tags_invalid")
    return ResponseCachePolicySnapshot(
        tenant_id=str(row["tenant_id"]),
        cache_policy_id=str(row["cache_policy_id"]),
        model_alias_id=str(row["model_alias_id"]),
        status="enabled",
        data_class=str(row["data_class"]),
        ttl_seconds=int(row["ttl_seconds"]),
        charge_cache_hits=bool(row["charge_cache_hits"]),
        tags=tuple(tags),
        version=int(row["version"]),
        invalidation_generation=int(row["invalidation_generation"]),
        single_data_class_confirmed=True,
    )


def _ensure_key_generation(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    hmac_key_version: int,
    cache_identity_digest: str,
    now: str,
) -> int:
    """创建或读取 identity generation；唯一键冲突只用于同一完整复合主键。"""

    connection.execute(
        """
        INSERT INTO response_cache_key_generations(
          tenant_id,hmac_key_version,cache_identity_digest,generation,updated_at
        ) VALUES(?,?,?,1,?) ON CONFLICT DO NOTHING
        """,
        (tenant_id, hmac_key_version, cache_identity_digest, now),
    )
    row = connection.execute(
        """
        SELECT generation FROM response_cache_key_generations
        WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
        """,
        (tenant_id, hmac_key_version, cache_identity_digest),
    ).fetchone()
    if row is None:
        # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
        raise sqlite3.IntegrityError("response_cache_key_generation_missing")
    return int(row["generation"])


def _identity(
    *,
    hmac_key: VersionedResponseCacheSecret,
    prompt_hash: str,
    route_revision_digest: str,
    cache_identity_digest: str,
    key_generation: int,
) -> ResponseCacheIdentity:
    """从数据库 generation 生成最终 key，避免 identity/key 自引用。"""

    return ResponseCacheIdentity(
        hmac_key_version=hmac_key.version,
        key_schema_version=CACHE_KEY_SCHEMA_VERSION,
        key_generation=key_generation,
        prompt_hash=prompt_hash,
        route_revision_digest=route_revision_digest,
        cache_identity_digest=cache_identity_digest,
        cache_key_digest=build_cache_key_digest(
            key=hmac_key,
            cache_identity_digest=cache_identity_digest,
            key_generation=key_generation,
        ),
    )


def _ensure_tag_generations(
    connection: sqlite3.Connection,
    *,
    hmac_key: VersionedResponseCacheSecret,
    tenant_id: str,
    policy: ResponseCachePolicySnapshot,
    route: GatewayRoute,
    virtual_key_id: str,
    now: str,
) -> tuple[ResponseCacheTagGeneration, ...]:
    """HMAC 后再创建/读取标签代际，数据库永不接触 selector 明文。"""

    tags = trusted_response_cache_tags(
        tenant_id=tenant_id,
        cache_policy_id=policy.cache_policy_id,
        model_alias_id=policy.model_alias_id,
        model_id=route.model_id,
        virtual_key_id=virtual_key_id,
        data_class=policy.data_class,
        policy_tags=policy.tags,
    )
    frozen: list[ResponseCacheTagGeneration] = []
    for tag in tags:
        digest = build_tag_digest(key=hmac_key, tenant_id=tenant_id, tag=tag)
        connection.execute(
            """
            INSERT INTO response_cache_tag_generations(
              tenant_id,hmac_key_version,tag_kind,tag_digest,generation,updated_at
            ) VALUES(?,?,?,?,1,?) ON CONFLICT DO NOTHING
            """,
            (tenant_id, digest.hmac_key_version, digest.kind, digest.digest, now),
        )
        row = connection.execute(
            """
            SELECT generation FROM response_cache_tag_generations
            WHERE tenant_id=? AND hmac_key_version=? AND tag_kind=? AND tag_digest=?
            """,
            (tenant_id, digest.hmac_key_version, digest.kind, digest.digest),
        ).fetchone()
        if row is None:
            # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
            raise sqlite3.IntegrityError("response_cache_tag_generation_missing")
        frozen.append(ResponseCacheTagGeneration(digest, int(row["generation"])))
    return tuple(frozen)


def _load_candidate(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    policy: ResponseCachePolicySnapshot,
    identity: ResponseCacheIdentity,
    credential_scope_digest: str,
    expected_tags: tuple[ResponseCacheTagGeneration, ...],
    encryption_key: VersionedResponseCacheSecret,
    now: str,
) -> ResponseCacheCandidate | None:
    """逐项验证不可变 owner、标签代际和 AES-GCM；任一异常只返回 corrupt 信号。"""

    try:
        if (
            str(row["tenant_id"]) != policy.tenant_id
            or str(row["cache_policy_id"]) != policy.cache_policy_id
            or int(row["cache_policy_version"]) != policy.version
            or int(row["policy_invalidation_generation"])
            != policy.invalidation_generation
            or int(row["hmac_key_version"]) != identity.hmac_key_version
            or int(row["encryption_key_version"]) != encryption_key.version
            or str(row["cache_identity_digest"]) != identity.cache_identity_digest
            or int(row["key_generation"]) != identity.key_generation
            or str(row["cache_key_digest"]) != identity.cache_key_digest
            or str(row["route_revision_digest"]) != identity.route_revision_digest
            or not hmac.compare_digest(
                str(row["credential_revision_digest"]), credential_scope_digest
            )
            or int(
                connection.execute(
                    "SELECT unixepoch(?)>unixepoch(?)", (row["expires_at"], now)
                ).fetchone()[0]
            )
            != 1
        ):
            return None
        tag_rows = connection.execute(
            """
            SELECT hmac_key_version,tag_kind,tag_digest,tag_generation
            FROM response_cache_entry_tags
            WHERE tenant_id=? AND response_cache_entry_id=?
            ORDER BY tag_kind,tag_digest
            """,
            (policy.tenant_id, row["response_cache_entry_id"]),
        ).fetchall()
        actual_tags = tuple(
            (
                int(tag_row["hmac_key_version"]),
                str(tag_row["tag_kind"]),
                str(tag_row["tag_digest"]),
                int(tag_row["tag_generation"]),
            )
            for tag_row in tag_rows
        )
        frozen_tags = tuple(
            sorted(
                (
                    tag.tag.hmac_key_version,
                    tag.tag.kind,
                    tag.tag.digest,
                    tag.generation,
                )
                for tag in expected_tags
            )
        )
        if actual_tags != frozen_tags:
            return None
        owner = _entry_owner(row)
        encrypted = EncryptedResponseCachePayload(
            owner.encryption_key_version,
            bytes(row["nonce"]),
            bytes(row["ciphertext"]),
        )
        plaintext = decrypt_response_payload(
            encrypted, secret=encryption_key.secret, owner=owner
        )
        payload = json.loads(plaintext.decode("utf-8", errors="strict"))
        if type(payload) is not dict:
            return None
        return ResponseCacheCandidate(
            owner=owner,
            encrypted=encrypted,
            payload=payload,
            policy=policy,
            identity=identity,
            credential_scope_digest=credential_scope_digest,
            tags=expected_tags,
        )
    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    except (ResponseCacheContractError, UnicodeError, ValueError, TypeError, sqlite3.Error):
        return None


def _entry_owner(row: sqlite3.Row) -> ResponseCacheEntryOwner:
    """只从同一 entry 行重建 AAD owner，禁止调用方补写可漂移字段。"""

    return ResponseCacheEntryOwner(
        tenant_id=str(row["tenant_id"]),
        entry_id=str(row["response_cache_entry_id"]),
        cache_policy_id=str(row["cache_policy_id"]),
        cache_policy_version=int(row["cache_policy_version"]),
        cache_policy_generation=int(row["policy_invalidation_generation"]),
        hmac_key_version=int(row["hmac_key_version"]),
        encryption_key_version=int(row["encryption_key_version"]),
        cache_identity_digest=str(row["cache_identity_digest"]),
        cache_key_digest=str(row["cache_key_digest"]),
        key_generation=int(row["key_generation"]),
        model_alias_id=str(row["model_alias_id"]),
        model_id=str(row["model_id"]),
        provider_id=str(row["provider_id"]),
        route_revision_digest=str(row["route_revision_digest"]),
        credential_revision_digest=str(row["credential_revision_digest"]),
        source_request_id=str(row["source_request_id"]),
        source_trace_id=str(row["source_trace_id"]),
        source_usage_ledger_id=str(row["source_usage_ledger_id"]),
        source_finops_ledger_entry_id=str(row["source_finops_ledger_entry_id"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        total_tokens=int(row["total_tokens"]),
        source_generation_cost_status=str(row["source_generation_cost_status"]),
        source_generation_cost_cny_micros=(
            int(row["source_generation_cost_cny_micros"])
            if row["source_generation_cost_cny_micros"] is not None
            else None
        ),
        expires_at=str(row["expires_at"]),
        plaintext_byte_count=int(row["plaintext_byte_count"]),
        key_schema_version=int(row["key_schema_version"]),
    )


def _reserve_fill_lease(
    connection: sqlite3.Connection,
    *,
    config: AppConfig,
    context: RequestLogContext,
    policy: ResponseCachePolicySnapshot,
    identity: ResponseCacheIdentity,
    encryption_key_version: int,
    key_state_generation: int,
    route_revision_digest: str,
    credential_scope_digest: str,
    policy_snapshot_id: str | None,
    policy_snapshot_version: int | None,
    policy_snapshot_status: str,
    tags: tuple[ResponseCacheTagGeneration, ...],
    now: str,
) -> ResponseCacheFillLease | None:
    """删除过期 owner 后以唯一 key 插入新 lease；并发 loser 不等待 Provider 之外的工作。"""

    existing = connection.execute(
        """
        SELECT response_cache_fill_lease_id,lease_expires_at
        FROM response_cache_fill_leases
        WHERE tenant_id=? AND hmac_key_version=? AND cache_key_digest=?
        """,
        (context.tenant_id, identity.hmac_key_version, identity.cache_key_digest),
    ).fetchone()
    if existing is not None:
        expired = int(
            connection.execute(
                "SELECT unixepoch(?)<=unixepoch(?)", (existing["lease_expires_at"], now)
            ).fetchone()[0]
        )
        if not expired:
            return None
        connection.execute(
            """
            DELETE FROM response_cache_fill_leases
            WHERE tenant_id=? AND response_cache_fill_lease_id=?
            """,
            (context.tenant_id, existing["response_cache_fill_lease_id"]),
        )
    lease_id = f"rcfl_{uuid4().hex}"
    lease_token = token_hex(32)
    expires_at = str(
        connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ',?,'+'||?||' seconds')",
            (now, config.response_cache_fill_lease_seconds),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO response_cache_fill_leases(
          response_cache_fill_lease_id,tenant_id,key_schema_version,hmac_key_version,
          encryption_key_version,cache_identity_digest,key_generation,cache_key_digest,
          cache_policy_id,cache_policy_version,policy_invalidation_generation,
          owner_request_id,owner_trace_id,lease_generation,lease_token,lease_owner_id,
          route_revision_digest,credential_revision_digest,lease_expires_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)
        """,
        (
            lease_id,
            context.tenant_id,
            identity.key_schema_version,
            identity.hmac_key_version,
            encryption_key_version,
            identity.cache_identity_digest,
            identity.key_generation,
            identity.cache_key_digest,
            policy.cache_policy_id,
            policy.version,
            policy.invalidation_generation,
            context.request_id,
            context.trace_id,
            lease_token,
            _PROCESS_LEASE_OWNER_ID,
            route_revision_digest,
            credential_scope_digest,
            expires_at,
            now,
            now,
        ),
    )
    for frozen in tags:
        connection.execute(
            """
            INSERT INTO response_cache_fill_tags(
              tenant_id,response_cache_fill_lease_id,hmac_key_version,tag_kind,
              tag_digest,expected_generation
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                context.tenant_id,
                lease_id,
                frozen.tag.hmac_key_version,
                frozen.tag.kind,
                frozen.tag.digest,
                frozen.generation,
            ),
        )
    return ResponseCacheFillLease(
        tenant_id=context.tenant_id,
        fill_lease_id=lease_id,
        owner_request_id=context.request_id,
        owner_trace_id=context.trace_id,
        lease_owner_id=_PROCESS_LEASE_OWNER_ID,
        lease_generation=1,
        lease_token=lease_token,
        policy=policy,
        identity=identity,
        encryption_key_version=encryption_key_version,
        key_state_generation=key_state_generation,
        route_revision_digest=route_revision_digest,
        credential_scope_digest=credential_scope_digest,
        policy_snapshot_id=policy_snapshot_id,
        policy_snapshot_version=policy_snapshot_version,
        policy_snapshot_status=policy_snapshot_status,
        tags=tags,
        expires_at=expires_at,
    )


def _lease_is_current(
    connection: sqlite3.Connection,
    *,
    lease: ResponseCacheFillLease,
    route: GatewayRoute,
    hmac_key: VersionedResponseCacheSecret,
    encryption_key: VersionedResponseCacheSecret,
    credential_scope: object,
    now: str,
) -> bool:
    """fill commit 前在同一写事务复核所有 generation、owner 与当前路由。"""

    row = connection.execute(
        """
        SELECT lease.*,state.generation AS key_state_generation,
               state.active_hmac_key_version,state.active_encryption_key_version,
               state.hmac_secret_fingerprint,state.encryption_secret_fingerprint,
               policy.status AS policy_status,policy.version AS current_policy_version,
               policy.invalidation_generation AS current_policy_generation,
               generation.generation AS current_key_generation
        FROM response_cache_fill_leases lease
        JOIN response_cache_key_state state ON state.key_state_id=1
        JOIN response_cache_policies policy
          ON policy.tenant_id=lease.tenant_id
         AND policy.cache_policy_id=lease.cache_policy_id
        JOIN response_cache_key_generations generation
          ON generation.tenant_id=lease.tenant_id
         AND generation.hmac_key_version=lease.hmac_key_version
         AND generation.cache_identity_digest=lease.cache_identity_digest
        WHERE lease.tenant_id=? AND lease.response_cache_fill_lease_id=?
          AND lease.lease_generation=? AND lease.lease_token=?
        """,
        (
            lease.tenant_id,
            lease.fill_lease_id,
            lease.lease_generation,
            lease.lease_token,
        ),
    ).fetchone()
    if row is None:
        return False
    if (
        str(row["owner_request_id"]) != lease.owner_request_id
        or str(row["owner_trace_id"]) != lease.owner_trace_id
        or int(row["key_state_generation"]) != lease.key_state_generation
        or int(row["active_hmac_key_version"]) != hmac_key.version
        or int(row["active_encryption_key_version"]) != encryption_key.version
        or not hmac.compare_digest(
            str(row["hmac_secret_fingerprint"]),
            response_cache_secret_fingerprint(hmac_key, purpose="hmac"),
        )
        or not hmac.compare_digest(
            str(row["encryption_secret_fingerprint"]),
            response_cache_secret_fingerprint(encryption_key, purpose="encryption"),
        )
        or int(row["current_key_generation"]) != lease.identity.key_generation
        or str(row["policy_status"]) != "enabled"
        or int(row["current_policy_version"]) != lease.policy.version
        or int(row["current_policy_generation"])
        != lease.policy.invalidation_generation
        or str(row["route_revision_digest"]) != lease.route_revision_digest
        or str(row["credential_revision_digest"]) != lease.credential_scope_digest
        or route.tenant_id != lease.tenant_id
        or route.model_alias_id != lease.policy.model_alias_id
        or not hmac.compare_digest(
            build_route_revision_digest(
                _route_revision_input(
                    route,
                    credential_scope=credential_scope,
                    policy_snapshot_id=lease.policy_snapshot_id,
                    policy_snapshot_version=lease.policy_snapshot_version,
                    policy_snapshot_status=lease.policy_snapshot_status,
                ),
                key=hmac_key,
            ),
            lease.route_revision_digest,
        )
        or int(
            connection.execute(
                "SELECT unixepoch(?)>unixepoch(?)", (row["lease_expires_at"], now)
            ).fetchone()[0]
        )
        != 1
    ):
        return False
    tag_rows = connection.execute(
        """
        SELECT frozen.hmac_key_version,frozen.tag_kind,frozen.tag_digest,
               frozen.expected_generation,current.generation AS current_generation
        FROM response_cache_fill_tags frozen
        LEFT JOIN response_cache_tag_generations current
          ON current.tenant_id=frozen.tenant_id
         AND current.hmac_key_version=frozen.hmac_key_version
         AND current.tag_kind=frozen.tag_kind
         AND current.tag_digest=frozen.tag_digest
        WHERE frozen.tenant_id=? AND frozen.response_cache_fill_lease_id=?
        ORDER BY frozen.tag_kind,frozen.tag_digest
        """,
        (lease.tenant_id, lease.fill_lease_id),
    ).fetchall()
    actual_tags = tuple(
        (
            int(tag["hmac_key_version"]),
            str(tag["tag_kind"]),
            str(tag["tag_digest"]),
            int(tag["expected_generation"]),
            int(tag["current_generation"])
            if tag["current_generation"] is not None
            else None,
        )
        for tag in tag_rows
    )
    expected_tags = tuple(
        sorted(
            (
                frozen.tag.hmac_key_version,
                frozen.tag.kind,
                frozen.tag.digest,
                frozen.generation,
                frozen.generation,
            )
            for frozen in lease.tags
        )
    )
    return actual_tags == expected_tags


def _load_committed_source(
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    route: GatewayRoute,
    response: NormalizedResponse,
) -> dict[str, object] | None:
    """证明成功 RequestLog、Usage 和正式 FinOps 已在主库一一提交。"""

    row = connection.execute(
        """
        SELECT usage.usage_ledger_id,charge.finops_ledger_entry_id,
               charge.cny_normalization_status,charge.normalized_cny_micros,
               usage.input_tokens,usage.output_tokens,usage.total_tokens
        FROM request_logs request
        JOIN usage_ledger usage
          ON usage.tenant_id=request.tenant_id AND usage.request_id=request.request_id
        JOIN finops_ledger_entries charge
          ON charge.tenant_id=usage.tenant_id
         AND charge.usage_ledger_id=usage.usage_ledger_id
         AND charge.entry_type='usage_charge'
        WHERE request.tenant_id=? AND request.request_id=? AND request.trace_id=?
          AND request.status='success' AND request.model_id=? AND request.provider_id=?
          AND usage.usage_source IN('upstream','estimated')
        ORDER BY usage.created_at DESC,usage.usage_ledger_id DESC
        LIMIT 1
        """,
        (
            context.tenant_id,
            context.request_id,
            context.trace_id,
            route.model_id,
            route.provider_id,
        ),
    ).fetchone()
    if row is None or (
        int(row["input_tokens"]) != response.usage.input_tokens
        or int(row["output_tokens"]) != response.usage.output_tokens
        or int(row["total_tokens"]) != response.usage.total_tokens
    ):
        return None
    known = (
        str(row["cny_normalization_status"]) == "normalized"
        and row["normalized_cny_micros"] is not None
    )
    return {
        "usage_ledger_id": str(row["usage_ledger_id"]),
        "finops_ledger_entry_id": str(row["finops_ledger_entry_id"]),
        "cost_status": "known" if known else "unavailable",
        "cost_micros": int(row["normalized_cny_micros"]) if known else None,
    }


def _fence_corrupt_entry(
    connection: sqlite3.Connection,
    *,
    context: RequestLogContext,
    policy: ResponseCachePolicySnapshot,
    identity: ResponseCacheIdentity,
    hmac_key: VersionedResponseCacheSecret,
    now: str,
) -> None:
    """推进 key generation、删除旧 entry/fill，并形成无敏感 selector 的系统失效事实。"""

    matched = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM response_cache_entries
            WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
            """,
            (context.tenant_id, identity.hmac_key_version, identity.cache_identity_digest),
        ).fetchone()[0]
    )
    fenced = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM response_cache_fill_leases
            WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
            """,
            (context.tenant_id, identity.hmac_key_version, identity.cache_identity_digest),
        ).fetchone()[0]
    )
    updated = connection.execute(
        """
        UPDATE response_cache_key_generations
        SET generation=generation+1,updated_at=?
        WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
          AND generation=?
        """,
        (
            now,
            context.tenant_id,
            identity.hmac_key_version,
            identity.cache_identity_digest,
            identity.key_generation,
        ),
    )
    if updated.rowcount != 1:
        # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
        raise sqlite3.IntegrityError("response_cache_corrupt_fence_lost")
    connection.execute(
        """
        DELETE FROM response_cache_entries
        WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
        """,
        (context.tenant_id, identity.hmac_key_version, identity.cache_identity_digest),
    )
    connection.execute(
        """
        DELETE FROM response_cache_fill_leases
        WHERE tenant_id=? AND hmac_key_version=? AND cache_identity_digest=?
        """,
        (context.tenant_id, identity.hmac_key_version, identity.cache_identity_digest),
    )
    invalidation_id = f"rci_{uuid4().hex}"
    event_id = f"rcev_{uuid4().hex}"
    selector_hmac = hmac.new(
        hmac_key.secret.encode("utf-8"),
        b"metahub/response-cache/system-selector/v1\x00"
        + identity.cache_identity_digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    request_fingerprint = hmac.new(
        hmac_key.secret.encode("utf-8"),
        b"metahub/response-cache/system-invalidation/v1\x00"
        + invalidation_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    invalidation = {
        "cache_invalidation_id": invalidation_id,
        "tenant_id": context.tenant_id,
        "scope": "tags",
        "match_mode": "all",
        "tag_count": 1,
        "matched_entry_count": matched,
        "deleted_entry_count": matched,
        "fenced_fill_count": fenced,
        "reason_code": "system_corrupt",
        "created_at": now,
    }
    response_json = {
        "tenant_id": context.tenant_id,
        "invalidation": invalidation,
        "idempotent_replay": False,
        "request_id": context.request_id,
        "trace_id": context.trace_id,
    }
    connection.execute(
        """
        INSERT INTO response_cache_invalidations(
          cache_invalidation_id,tenant_id,actor_type,actor_user_id,
          idempotency_hmac_version,idempotency_key_hmac,request_fingerprint,
          scope,match_mode,selector_hmac,tag_count,matched_entry_count,
          deleted_entry_count,fenced_fill_count,reason_code,response_json,
          operation_request_id,operation_trace_id,created_at
        ) VALUES(?,?,'system',NULL,NULL,NULL,?, 'tags','all',?,1,?,?,?,
                 'system_corrupt',?,?,?,?)
        """,
        (
            invalidation_id,
            context.tenant_id,
            request_fingerprint,
            selector_hmac,
            matched,
            matched,
            fenced,
            json.dumps(response_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            context.request_id,
            context.trace_id,
            now,
        ),
    )
    payload = CacheInvalidatedWebhookPayload(
        cache_invalidation_id=invalidation_id,
        scope="tags",
        match_mode="all",
        tag_count=1,
        matched_entry_count=matched,
        deleted_entry_count=matched,
        fenced_fill_count=fenced,
        reason_code="system_corrupt",
    ).to_payload()
    connection.execute(
        """
        INSERT INTO response_cache_events(
          response_cache_event_id,tenant_id,event_type,object_id,actor_user_id,
          trace_id,payload_json,occurred_at
        ) VALUES(?,?,'cache.invalidated',?,NULL,?,?,?)
        """,
        (
            event_id,
            context.tenant_id,
            invalidation_id,
            context.trace_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )


def _insert_initial_fact(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    *,
    status: str,
    policy: ResponseCachePolicySnapshot | None,
    now: str,
) -> None:
    """为本请求创建唯一安全 cache fact；不保存 entry、key、tag 或 data class。"""

    connection.execute(
        """
        INSERT INTO response_cache_request_facts(
          response_cache_request_fact_id,tenant_id,request_id,trace_id,
          cache_policy_id,cache_policy_version,status,response_cache_hit_id,hit,
          charge_cache_hits,source_generation_cost_status,
          source_generation_cost_cny_micros,current_reference_cost_status,
          current_reference_cost_cny_micros,saved_cost_status,saved_cost_cny_micros,
          billed_cost_status,billed_cost_cny_micros,failure_code,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,NULL,0,?,'not_applicable',NULL,
                 'not_applicable',NULL,'not_applicable',NULL,'not_applicable',NULL,NULL,?,?)
        """,
        (
            f"rcrf_{uuid4().hex}",
            context.tenant_id,
            context.request_id,
            context.trace_id,
            policy.cache_policy_id if policy is not None else None,
            policy.version if policy is not None else None,
            status,
            (1 if policy.charge_cache_hits else 0) if policy is not None else None,
            now,
            now,
        ),
    )


def _safe_insert_initial_fact(
    config: AppConfig,
    context: RequestLogContext,
    *,
    status: str,
    policy: ResponseCachePolicySnapshot | None,
) -> None:
    """结构旁路的 fact 写入失败不改变原请求；仅记录固定元数据告警。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        _insert_initial_fact(
            connection,
            context,
            status=status,
            policy=policy,
            now=_database_now(connection),
        )
        connection.commit()
    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
        _log_optional_failure(context, "request_fact_unavailable")
    finally:
        if connection is not None:
            connection.close()


def _transition_fact(
    connection: sqlite3.Connection,
    context: RequestLogContext,
    *,
    from_status: str | tuple[str, ...],
    to_status: str,
    now: str,
    required: bool = True,
) -> None:
    """按 tenant+request+旧状态 CAS 推进单向事实状态机。"""

    states = (from_status,) if isinstance(from_status, str) else from_status
    placeholders = ",".join("?" for _ in states)
    cursor = connection.execute(
        f"""
        UPDATE response_cache_request_facts
        SET status=?,updated_at=?
        WHERE tenant_id=? AND request_id=? AND status IN({placeholders})
        """,
        (to_status, now, context.tenant_id, context.request_id, *states),
    )
    if required and cursor.rowcount != 1:
        # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
        raise sqlite3.IntegrityError("response_cache_request_fact_transition_lost")


def _delete_expired_entries(
    connection: sqlite3.Connection, *, tenant_id: str, now: str
) -> None:
    """删除当前租户已到 TTL 边界的密文；正常生命周期不产生业务失效事件。"""

    connection.execute(
        """
        DELETE FROM response_cache_entries
        WHERE tenant_id=? AND unixepoch(expires_at)<=unixepoch(?)
        """,
        (tenant_id, now),
    )


def _evict_to_capacity(
    connection: sqlite3.Connection, *, tenant_id: str, maximum: int
) -> None:
    """在写入新 entry 前稳定淘汰超出 ``maximum-1`` 的最旧密文。"""

    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM response_cache_entries WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )
    remove_count = max(0, count - maximum + 1)
    if remove_count == 0:
        return
    rows = connection.execute(
        """
        SELECT response_cache_entry_id FROM response_cache_entries
        WHERE tenant_id=?
        ORDER BY expires_at,created_at,response_cache_entry_id
        LIMIT ?
        """,
        (tenant_id, remove_count),
    ).fetchall()
    connection.executemany(
        """
        DELETE FROM response_cache_entries
        WHERE tenant_id=? AND response_cache_entry_id=?
        """,
        ((tenant_id, row["response_cache_entry_id"]) for row in rows),
    )


def _nonce_available(
    connection: sqlite3.Connection, encryption_key_version: int, nonce: bytes
) -> bool:
    """以数据库 UNIQUE 前置检查 nonce；最终冲突仍由 INSERT 约束裁决。"""

    return (
        connection.execute(
            """
            SELECT 1 FROM response_cache_entries
            WHERE encryption_key_version=? AND nonce=? LIMIT 1
            """,
            (encryption_key_version, nonce),
        ).fetchone()
        is None
    )


def _required_virtual_key(context: RequestLogContext) -> str:
    """公共缓存身份必须绑定认证 Virtual Key；缺失时仅标记不适合缓存。"""

    if not context.virtual_key_id:
        raise ResponseCacheIneligible()
    return context.virtual_key_id


def _database_now(connection: sqlite3.Connection) -> str:
    """所有 TTL/lease/事件共享当前 SQLite UTC 秒线性化时点。"""

    value = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchone()[0]
    if type(value) is not str:
        # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
        raise sqlite3.IntegrityError("response_cache_database_time_unavailable")
    return value


def _parse_db_time(value: str) -> datetime:
    """把 canonical SQLite UTC 秒转换为 Credential snapshot 的 aware datetime。

    数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。 当前定义负责 _parse_db_time 对应步骤。"""

    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)


def _log_optional_failure(context: RequestLogContext, reason: str) -> None:
    """只记录固定原因和请求 owner，不记录正文、摘要、标签、Secret 或数据库异常。"""

    logger.warning(
        "response_cache.optional_operation_failed",
        extra={
            "event": "response_cache.optional_operation_failed",
            "tenant_id": context.tenant_id,
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "error_code": f"response_cache.{reason}",
        },
    )


__all__ = [
    "commit_response_cache_fill",
    "prepare_response_cache",
    "release_response_cache_fill",
]
