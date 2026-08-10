"""验证 PRD-076 Response Cache 的 Shared 密码学、配置、错误与 Webhook 安全冻结合同。"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest
from metahub_shared.response_cache import (
    CacheHitWebhookPayload,
    CacheInvalidatedWebhookPayload,
    CacheTag,
    CredentialResponseScope,
    EncryptedResponseCachePayload,
    ResponseCacheAmount,
    ResponseCacheContractError,
    ResponseCacheEntryOwner,
    ResponseCacheHitAmounts,
    ResponseCacheIdentity,
    ResponseCacheIneligible,
    RouteRevisionInput,
    VersionedResponseCacheSecret,
    build_control_idempotency_digests,
    build_control_request_fingerprint,
    build_managed_credential_scope,
    build_none_credential_scope,
    build_response_cache_identity,
    build_tag_digests,
    calculate_saved_amount,
    canonicalize_cache_tags,
    decrypt_response_payload,
    encrypt_response_payload,
    entry_aad,
    parse_cache_tag,
    response_cache_secret_fingerprint,
    try_build_prompt_hash,
    typed_canonical_json_bytes,
    validate_response_cache_secrets,
    validate_secret_keyring,
)
from metahub_shared.runtime.config import DEFAULT_CONFIG, ConfigError, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 测试材料全部显式标记为本地非凭据值；它们只验证域分离，不进入日志或验收 JSON。
HMAC_SECRET = "test-only-prd076-hmac-secret-0000000000000001"
ENCRYPTION_SECRET = "test-only-prd076-encryption-secret-00000000002"
CONTROL_SECRET = "test-only-prd076-control-secret-00000000000003"
FAILURE_SECRET = "test-only-prd076-failure-secret-00000000000004"
CONTROL_PREVIOUS_SECRET = "test-only-prd076-control-previous-000000000005"
FAILURE_PREVIOUS_SECRET = "test-only-prd076-failure-previous-000000000006"


def _key(version: int = 1, secret: str = HMAC_SECRET) -> VersionedResponseCacheSecret:
    """构造只在测试调用栈内存在的版本化 HMAC key。"""

    # 测试 helper 仍复用正式 repr=False DTO，避免失败输出意外显示固定 Secret 材料。
    return VersionedResponseCacheSecret(version, secret)


def _route(
    key: VersionedResponseCacheSecret,
    *,
    tenant_id: str = "tenant_a",
    model_id: str = "model_a",
    credential: CredentialResponseScope | None = None,
) -> RouteRevisionInput:
    """构造完整路由快照，Endpoint/模型名和 Credential 摘要不会出现在 repr。"""

    scope = credential or build_none_credential_scope(
        key=key,
        tenant_id=tenant_id,
        provider_id="provider_a",
        provider_auth_type="none",
        scope_version=1,
    )
    return RouteRevisionInput(
        tenant_id=tenant_id,
        model_alias_id="model_alias_a",
        resolved_model_id=model_id,
        provider_id="provider_a",
        provider_type="openai_compatible",
        provider_auth_type="none",
        provider_base_url="https://local.example.invalid/v1",
        upstream_model="upstream-model-private",
        capabilities={"chat": True, "stream": True},
        adapter_catalog_version="catalog-v1",
        adapter_catalog_hash="a" * 64,
        policy_snapshot_id="policy_snapshot_a",
        policy_snapshot_version=1,
        policy_snapshot_status="current",
        credential_scope=scope,
    )


def test_route_revision_preserves_nullable_policy_snapshot_owner() -> None:
    """null head 使用 None/None；半空、版本零和 current+空 owner 均失败关闭。"""

    key = _key()
    absent = replace(
        _route(key),
        policy_snapshot_id=None,
        policy_snapshot_version=None,
        policy_snapshot_status="disabled",
    )
    first = build_response_cache_identity(
        {
            "model": "alias-a",
            "messages": [{"role": "user", "content": "test-only-absent-policy"}],
        },
        absent,
        key=key,
        virtual_key_id="virtual_key_a",
        data_class="internal",
        cache_policy_id="rcp_" + "1" * 32,
        cache_policy_version=1,
        cache_policy_generation=1,
        key_generation=1,
    )
    second = build_response_cache_identity(
        {
            "model": "alias-a",
            "messages": [{"role": "user", "content": "test-only-absent-policy"}],
        },
        absent,
        key=key,
        virtual_key_id="virtual_key_a",
        data_class="internal",
        cache_policy_id="rcp_" + "1" * 32,
        cache_policy_version=1,
        cache_policy_generation=1,
        key_generation=1,
    )
    assert first.route_revision_digest == second.route_revision_digest

    invalid_routes = (
        replace(absent, policy_snapshot_id="policy_snapshot_real"),
        replace(absent, policy_snapshot_version=0),
        replace(absent, policy_snapshot_status="current"),
        replace(_route(key), policy_snapshot_version=None),
    )
    for route in invalid_routes:
        with pytest.raises(ResponseCacheContractError):
            build_response_cache_identity(
                {"model": "alias-a", "messages": [{"role": "user", "content": "x"}]},
                route,
                key=key,
                virtual_key_id="virtual_key_a",
                data_class="internal",
                cache_policy_id="rcp_" + "1" * 32,
                cache_policy_version=1,
                cache_policy_generation=1,
                key_generation=1,
            )


def _identity(
    *,
    key: VersionedResponseCacheSecret | None = None,
    route: RouteRevisionInput | None = None,
    request: object | None = None,
    virtual_key_id: str = "virtual_key_a",
    data_class: str = "internal",
    policy_version: int = 3,
    policy_generation: int = 7,
    key_generation: int = 11,
) -> ResponseCacheIdentity:
    """构造三层缓存身份，便于逐维证明 miss 与 generation 围栏。"""

    selected_key = key or _key()
    selected_route = route or _route(selected_key)
    return build_response_cache_identity(
        request
        or {
            "model": "alias-a",
            "messages": [{"role": "user", "content": "test-only-input"}],
            "temperature": 0.25,
        },
        selected_route,
        key=selected_key,
        virtual_key_id=virtual_key_id,
        data_class=data_class,
        cache_policy_id="rcp_" + "1" * 32,
        cache_policy_version=policy_version,
        cache_policy_generation=policy_generation,
        key_generation=key_generation,
    )


def _owner(plaintext: bytes) -> ResponseCacheEntryOwner:
    """构造只含永久不可变 entry 与 source 事实的完整 AAD owner 测试快照。"""

    # byte count 必须来自本次明文 bytes，确保 round-trip 用例同时验证数据库镜像关系。
    return ResponseCacheEntryOwner(
        tenant_id="tenant_a",
        entry_id="rce_" + "2" * 32,
        cache_policy_id="rcp_" + "1" * 32,
        cache_policy_version=3,
        cache_policy_generation=7,
        hmac_key_version=1,
        encryption_key_version=1,
        cache_identity_digest="3" * 64,
        cache_key_digest="4" * 64,
        key_generation=11,
        model_alias_id="model_alias_a",
        model_id="model_a",
        provider_id="provider_a",
        route_revision_digest="5" * 64,
        credential_revision_digest="6" * 64,
        source_request_id="request_source_a",
        source_trace_id="trace_source_a",
        source_usage_ledger_id="usage_source_a",
        source_finops_ledger_entry_id="finops_source_a",
        input_tokens=3,
        output_tokens=5,
        total_tokens=8,
        source_generation_cost_status="known",
        source_generation_cost_cny_micros=12_345,
        expires_at="2026-08-10T12:00:00Z",
        plaintext_byte_count=len(plaintext),
    )


def _hit_payload(*, charge: bool = False) -> dict[str, object]:
    """生成可被 Shared DTO 与 runtime validator 双向接受的精确 cache.hit 安全载荷。"""

    current = ResponseCacheAmount("known", 12_300)
    billed = ResponseCacheAmount("known", 12_300 if charge else 0)
    amounts = ResponseCacheHitAmounts(
        source_generation=ResponseCacheAmount("known", 10_000),
        current_reference=current,
        saved=calculate_saved_amount(current, billed),
        billed=billed,
    )
    # 只通过正式 DTO 生成 wire 字段，测试不能手写绕过金额与收费模式等式。
    return CacheHitWebhookPayload(
        cache_hit_id="rch_" + "6" * 32,
        cache_policy_id="rcp_" + "1" * 32,
        cache_policy_version=3,
        charge_cache_hits=charge,
        amounts=amounts,
    ).to_payload()


def _invalidation_payload(
    *,
    reason: str = "manual_finops",
    scope: str = "tags",
) -> dict[str, object]:
    """生成不包含 selector/tag value 的缓存失效安全摘要。"""

    return CacheInvalidatedWebhookPayload(
        cache_invalidation_id="rci_" + "7" * 32,
        scope=scope,  # type: ignore[arg-type]
        match_mode="all",
        tag_count=1 if scope == "tags" else 0,
        matched_entry_count=4,
        deleted_entry_count=4,
        fenced_fill_count=1,
        reason_code=reason,  # type: ignore[arg-type]
    ).to_payload()


def test_typed_canonical_is_stable_and_preserves_json_number_types() -> None:
    """同一 JSON 语义稳定，但 int、float 与负零不能因语言宽松比较发生欠键。"""

    left = typed_canonical_json_bytes({"b": [True, None], "a": 1.0})
    right = typed_canonical_json_bytes({"a": 1e0, "b": [True, None]})
    assert left == right
    assert typed_canonical_json_bytes({"value": 1}) != typed_canonical_json_bytes(
        {"value": 1.0}
    )
    assert typed_canonical_json_bytes({"value": 0.0}) != typed_canonical_json_bytes(
        {"value": -0.0}
    )
    assert typed_canonical_json_bytes({"value": 1e-300}) != typed_canonical_json_bytes(
        {"value": 1e300}
    )

    for invalid in (
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": {1, 2}},
        ["top-level-array"],
    ):
        with pytest.raises(ResponseCacheIneligible):
            typed_canonical_json_bytes(invalid)


def test_prompt_hash_eligibility_never_truncates_or_changes_provider_behavior() -> None:
    """流式、Embeddings、内部调用和资源超限只旁路缓存，不改变正常 Provider 路径。"""

    key = _key()
    normal = {"messages": [], "stream": False, "unknown_option": {"x": 1}}
    assert try_build_prompt_hash(
        normal,
        key=key,
        request_kind="chat",
        public_virtual_key=True,
        internal_invocation=False,
    ) is not None
    for request, kind, public, internal in (
        ({**normal, "stream": True}, "chat", True, False),
        (normal, "embeddings", True, False),
        (normal, "chat", False, False),
        (normal, "chat", True, True),
        ({"messages": [], "unsupported": object()}, "chat", True, False),
        ({"messages": [], "large": "x" * (1024 * 1024 + 1)}, "chat", True, False),
        ({"messages": [], "nodes": [0] * 8_192}, "chat", True, False),
    ):
        assert (
            try_build_prompt_hash(
                request,
                key=key,
                request_kind=kind,
                public_virtual_key=public,
                internal_invocation=internal,
            )
            is None
        )


def test_secret_fingerprint_is_stable_and_separated_by_purpose_version_and_secret() -> None:
    """key-state fingerprint 可稳定比较配置，但不同用途、版本或 Secret 绝不共享标识。"""

    first = response_cache_secret_fingerprint(_key(), "hmac")
    assert first == response_cache_secret_fingerprint(_key(), "hmac")
    assert len(first) == 64
    assert first != response_cache_secret_fingerprint(_key(), "encryption")
    assert first != response_cache_secret_fingerprint(
        _key(2, HMAC_SECRET),
        "hmac",
    )
    assert first != response_cache_secret_fingerprint(
        _key(1, "test-only-prd076-other-fingerprint-secret-000010"),
        "hmac",
    )
    assert HMAC_SECRET not in first
    with pytest.raises(
        ResponseCacheContractError, match="response_cache.configuration_unavailable"
    ):
        response_cache_secret_fingerprint(_key(), "invalid")  # type: ignore[arg-type]


def test_cache_identity_changes_on_frozen_dimensions_but_not_key_generation() -> None:
    """身份维度任一变化产生 miss；数据库 key generation 只改变最终 key，不形成自引用。"""

    key = _key()
    route = _route(key)
    base = _identity(key=key, route=route)
    assert base == _identity(key=key, route=route)
    assert base.cache_identity_digest != _identity(
        key=key,
        route=route,
        request={"model": "alias-a", "messages": [], "new_option": True},
    ).cache_identity_digest
    assert base.cache_identity_digest != _identity(
        key=key, route=route, virtual_key_id="virtual_key_b"
    ).cache_identity_digest
    assert base.cache_identity_digest != _identity(
        key=key, route=route, data_class="restricted"
    ).cache_identity_digest
    assert base.cache_identity_digest != _identity(
        key=key, route=replace(route, upstream_model="different-private-model")
    ).cache_identity_digest
    assert base.cache_identity_digest != _identity(
        key=key, route=route, policy_version=4
    ).cache_identity_digest
    assert base.cache_identity_digest != _identity(
        key=key, route=route, policy_generation=8
    ).cache_identity_digest

    next_generation = _identity(key=key, route=route, key_generation=12)
    assert base.cache_identity_digest == next_generation.cache_identity_digest
    assert base.cache_key_digest != next_generation.cache_key_digest

    other_key = _key(2, "test-only-prd076-hmac-secret-version-two-0000007")
    other_route = _route(other_key)
    assert base.cache_identity_digest != _identity(
        key=other_key, route=other_route
    ).cache_identity_digest


def test_credential_scope_requires_one_managed_credential_and_binds_revision() -> None:
    """多 KeyPool 不能跨上游账号共享响应；单一 Credential 的版本变化必须 miss。"""

    key = _key()
    with pytest.raises(ResponseCacheIneligible):
        build_managed_credential_scope(
            key=key,
            tenant_id="tenant_a",
            provider_id="provider_a",
            provider_auth_type="bearer",
            scope_version=1,
            selectable_credential_count=2,
            credential_id="credential_private_a",
            credential_key_fingerprint="8" * 64,
            credential_version=1,
        )
    first = build_managed_credential_scope(
        key=key,
        tenant_id="tenant_a",
        provider_id="provider_a",
        provider_auth_type="bearer",
        scope_version=1,
        selectable_credential_count=1,
        credential_id="credential_private_a",
        credential_key_fingerprint="8" * 64,
        credential_version=1,
    )
    second = build_managed_credential_scope(
        key=key,
        tenant_id="tenant_a",
        provider_id="provider_a",
        provider_auth_type="bearer",
        scope_version=1,
        selectable_credential_count=1,
        credential_id="credential_private_a",
        credential_key_fingerprint="8" * 64,
        credential_version=2,
    )
    assert first.digest != second.digest
    assert "credential_private_a" not in repr(first)
    assert first.digest not in repr(first)


def test_sensitive_dtos_hide_secret_digest_endpoint_tag_and_source_owner() -> None:
    """repr 只能用于安全诊断，不能成为 Secret、拓扑、摘要或 owner 的旁路日志。"""

    key = _key()
    route = _route(key)
    identity = _identity(key=key, route=route)
    tag = CacheTag("tag", "private-value")
    plaintext = b'{"id":"safe-response","choices":[]}'
    owner = _owner(plaintext)
    encrypted = encrypt_response_payload(
        plaintext,
        secret=ENCRYPTION_SECRET,
        owner=owner,
        nonce_factory=lambda size: b"n" * size,
    )

    assert HMAC_SECRET not in repr(key)
    assert "local.example.invalid" not in repr(route)
    assert "upstream-model-private" not in repr(route)
    assert route.credential_scope.digest not in repr(route)
    for digest in (
        identity.prompt_hash,
        identity.route_revision_digest,
        identity.cache_identity_digest,
        identity.cache_key_digest,
    ):
        assert digest not in repr(identity)
    assert "private-value" not in repr(tag)
    for hidden in (
        owner.cache_identity_digest,
        owner.cache_key_digest,
        owner.route_revision_digest,
        owner.source_request_id,
        owner.source_trace_id,
        owner.source_usage_ledger_id,
        owner.source_finops_ledger_entry_id,
    ):
        assert hidden not in repr(owner)
    assert encrypted.nonce.hex() not in repr(encrypted)
    assert encrypted.ciphertext.hex() not in repr(encrypted)


def test_aes_gcm_round_trip_retries_nonce_collision_and_binds_every_owner_class() -> None:
    """每 entry HKDF/AES-GCM 可恢复；nonce 冲突重试，AAD owner 漂移统一完整性失败。"""

    plaintext = b'{"id":"safe-response","choices":[]}'
    owner = _owner(plaintext)
    candidates = iter((b"a" * 12, b"b" * 12))
    checked: list[bytes] = []

    def nonce_factory(size: int) -> bytes:
        """按固定次序模拟一次数据库 nonce 冲突。"""

        assert size == 12
        return next(candidates)

    def nonce_available(nonce: bytes) -> bool:
        """第一项已存在，第二项可用，证明实现不会复用冲突 nonce。"""

        checked.append(nonce)
        return nonce == b"b" * 12

    encrypted = encrypt_response_payload(
        plaintext,
        secret=ENCRYPTION_SECRET,
        owner=owner,
        nonce_factory=nonce_factory,
        nonce_available=nonce_available,
    )
    assert checked == [b"a" * 12, b"b" * 12]
    assert encrypted.nonce == b"b" * 12
    assert decrypt_response_payload(
        encrypted, secret=ENCRYPTION_SECRET, owner=owner
    ) == plaintext
    assert entry_aad(owner) == entry_aad(owner)

    drifted_owners = (
        replace(owner, tenant_id="tenant_b"),
        replace(owner, cache_policy_version=4),
        replace(owner, cache_policy_generation=8),
        replace(owner, hmac_key_version=2),
        replace(owner, encryption_key_version=2),
        replace(owner, key_generation=12),
        replace(owner, route_revision_digest="9" * 64),
        replace(owner, source_request_id="request_source_b"),
        replace(owner, input_tokens=4, total_tokens=9),
        replace(owner, source_generation_cost_cny_micros=12_346),
        replace(owner, expires_at="2026-08-10T12:00:01Z"),
    )
    for drifted in drifted_owners:
        assert entry_aad(drifted) != entry_aad(owner)
        with pytest.raises(
            ResponseCacheContractError, match="response_cache.integrity_failed"
        ):
            decrypt_response_payload(
                encrypted,
                secret=ENCRYPTION_SECRET,
                owner=drifted,
            )

    # current-reference/saved/billed 是未来 hit 事实；字段缺席即证明价格变化不会破坏 entry AAD。
    owner_fields = {item.name for item in fields(ResponseCacheEntryOwner)}
    assert not {
        "current_reference_cost_cny_micros",
        "saved_cost_cny_micros",
        "billed_cost_cny_micros",
    } & owner_fields


def test_crypto_rejects_tamper_invalid_json_and_exhausted_nonce_attempts() -> None:
    """tag、UTF-8、JSON object、重复 key 与 nonce 有界失败都只暴露固定内部码。"""

    plaintext = b'{"id":"safe-response","choices":[]}'
    owner = _owner(plaintext)
    encrypted = encrypt_response_payload(
        plaintext,
        secret=ENCRYPTION_SECRET,
        owner=owner,
        nonce_factory=lambda size: b"c" * size,
    )
    tampered = EncryptedResponseCachePayload(
        encrypted.encryption_key_version,
        encrypted.nonce,
        encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(ResponseCacheContractError, match="response_cache.integrity_failed"):
        decrypt_response_payload(tampered, secret=ENCRYPTION_SECRET, owner=owner)

    for invalid in (
        b"\xff",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'["not-an-object"]',
    ):
        with pytest.raises(
            ResponseCacheContractError, match="response_cache.integrity_failed"
        ):
            encrypt_response_payload(
                invalid,
                secret=ENCRYPTION_SECRET,
                owner=_owner(invalid),
                nonce_factory=lambda size: b"d" * size,
            )

    with pytest.raises(
        ResponseCacheContractError, match="response_cache.configuration_unavailable"
    ):
        encrypt_response_payload(
            plaintext,
            secret=ENCRYPTION_SECRET,
            owner=owner,
            nonce_factory=lambda size: b"e" * size,
            nonce_available=lambda nonce: False,
            max_nonce_attempts=2,
        )


def test_tag_and_control_hmac_contracts_are_sorted_versioned_and_owner_bound() -> None:
    """tag/幂等摘要绑定 tenant、actor 与 key version，且不在 DTO repr 中保留明文。"""

    tags = [parse_cache_tag("tag:finance"), parse_cache_tag("data_class:internal")]
    canonical = canonicalize_cache_tags(tags)
    assert [item.kind for item in canonical] == ["data_class", "tag"]
    assert all(item.value not in repr(item) for item in canonical)
    with pytest.raises(ResponseCacheContractError, match="response_cache.tag_invalid"):
        canonicalize_cache_tags([tags[0], tags[0]])
    with pytest.raises(ResponseCacheContractError, match="response_cache.tag_invalid"):
        parse_cache_tag("tag:UPPERCASE")

    keyring = (
        _key(),
        _key(2, "test-only-prd076-hmac-retained-version-00000008"),
    )
    digests = build_tag_digests(keyring, tenant_id="tenant_a", selectors=tags)
    assert [(item.hmac_key_version, item.kind) for item in digests] == [
        (1, "data_class"),
        (1, "tag"),
        (2, "data_class"),
        (2, "tag"),
    ]
    assert len({item.digest for item in digests}) == 4
    assert all(item.digest not in repr(item) for item in digests)
    assert [item.digest for item in digests] != [
        item.digest
        for item in build_tag_digests(keyring, tenant_id="tenant_b", selectors=tags)
    ]

    idempotency = build_control_idempotency_digests(
        keyring,
        tenant_id="tenant_a",
        actor_user_id="user_a",
        idempotency_key="idem-prd076-0001",
    )
    assert [item.version for item in idempotency] == [1, 2]
    assert all(item.digest not in repr(item) for item in idempotency)
    other_actor = build_control_idempotency_digests(
        keyring,
        tenant_id="tenant_a",
        actor_user_id="user_b",
        idempotency_key="idem-prd076-0001",
    )
    assert [item.digest for item in idempotency] != [item.digest for item in other_actor]

    first_intent = build_control_request_fingerprint(
        key=keyring[0],
        tenant_id="tenant_a",
        actor_user_id="user_a",
        operation="policy_update",
        request_body={"status": "enabled", "ttl_seconds": 900},
    )
    second_intent = build_control_request_fingerprint(
        key=keyring[0],
        tenant_id="tenant_a",
        actor_user_id="user_a",
        operation="policy_update",
        request_body={"status": "enabled", "ttl_seconds": 901},
    )
    assert first_intent.digest != second_intent.digest

    with pytest.raises(
        ResponseCacheContractError, match="response_cache.configuration_unavailable"
    ):
        validate_secret_keyring((_key(), _key(1, CONTROL_SECRET)))
    with pytest.raises(
        ResponseCacheContractError, match="response_cache.configuration_unavailable"
    ):
        validate_secret_keyring((_key(), _key(2, HMAC_SECRET)))


def test_amount_dto_enforces_integer_micros_saved_equation_and_charge_mode() -> None:
    """金额只用整数微元计算；免费与收费 hit 都必须提供可复核的 billed 事实。"""

    assert ResponseCacheAmount("known", 12_300).to_wire_value() == "0.012300"
    assert ResponseCacheAmount("unavailable", None).to_wire_value() is None
    assert _hit_payload(charge=False)["saved_cost_cny"] == "0.012300"
    assert _hit_payload(charge=True)["saved_cost_cny"] == "0.000000"

    unavailable = ResponseCacheHitAmounts(
        source_generation=ResponseCacheAmount("unavailable", None),
        current_reference=ResponseCacheAmount("unavailable", None),
        saved=ResponseCacheAmount("unavailable", None),
        billed=ResponseCacheAmount("known", 0),
    )
    payload = CacheHitWebhookPayload(
        cache_hit_id="rch_" + "6" * 32,
        cache_policy_id="rcp_" + "1" * 32,
        cache_policy_version=3,
        charge_cache_hits=False,
        amounts=unavailable,
    ).to_payload()
    assert payload["current_reference_cost_cny"] is None
    assert payload["saved_cost_cny"] is None
    assert payload["billed_cost_cny"] == "0.000000"

    with pytest.raises(ResponseCacheContractError, match="response_cache.amount_invalid"):
        ResponseCacheHitAmounts(
            source_generation=ResponseCacheAmount("not_applicable", None),
            current_reference=ResponseCacheAmount("known", 10),
            saved=ResponseCacheAmount("known", 10),
            billed=ResponseCacheAmount("known", 0),
        )
    with pytest.raises(ResponseCacheContractError, match="response_cache.amount_invalid"):
        CacheHitWebhookPayload(
            cache_hit_id="rch_" + "6" * 32,
            cache_policy_id="rcp_" + "1" * 32,
            cache_policy_version=3,
            charge_cache_hits=False,
            amounts=ResponseCacheHitAmounts(
                source_generation=ResponseCacheAmount("known", 10),
                current_reference=ResponseCacheAmount("unavailable", None),
                saved=ResponseCacheAmount("unavailable", None),
                billed=ResponseCacheAmount("unavailable", None),
            ),
        ).to_payload()


def test_cache_webhooks_accept_exact_payload_and_enforce_actor_owner_matrix() -> None:
    """cache source event 绑定 rcev_、object、链路和 actor，不能只验证通用 JSON 外形。"""

    timestamp = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
    common = {
        "tenant_id": "tenant_a",
        "trace_id": "trace_a",
        "source_request_id": "request_a",
        "event_id": "rcev_" + "8" * 32,
        "timestamp": timestamp,
    }
    hit_payload = _hit_payload()
    hit = build_webhook_event(
        event_type="cache.hit",
        payload=hit_payload,
        actor_id=None,
        object_id=str(hit_payload["cache_hit_id"]),
        **common,
    )
    assert hit.payload == hit_payload

    manual_payload = _invalidation_payload()
    manual = build_webhook_event(
        event_type="cache.invalidated",
        payload=manual_payload,
        actor_id="user_a",
        object_id=str(manual_payload["cache_invalidation_id"]),
        **common,
    )
    assert manual.payload == manual_payload

    system_payload = _invalidation_payload(reason="system_corrupt", scope="all")
    system = build_webhook_event(
        event_type="cache.invalidated",
        payload=system_payload,
        actor_id=None,
        object_id=str(system_payload["cache_invalidation_id"]),
        **common,
    )
    assert system.actor_id is None
    assert {"cache.hit", "cache.invalidated"} <= WEBHOOK_EVENT_TYPES
    assert {"cache.hit", "cache.invalidated"} <= WEBHOOK_SUBSCRIBABLE_EVENT_TYPES

    invalid_cases = (
        (
            "cache.hit",
            hit_payload,
            "user_must_not_own_runtime_hit",
            hit_payload["cache_hit_id"],
        ),
        (
            "cache.invalidated",
            manual_payload,
            None,
            manual_payload["cache_invalidation_id"],
        ),
        (
            "cache.invalidated",
            system_payload,
            "user_must_not_own_system_eviction",
            system_payload["cache_invalidation_id"],
        ),
        ("cache.hit", hit_payload, None, "rch_" + "9" * 32),
    )
    for event_type, payload, actor_id, object_id in invalid_cases:
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                event_type=event_type,
                payload=payload,
                actor_id=actor_id,
                object_id=object_id,
                **common,
            )


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": True},
        {"cache_policy_version": True},
        {"cache_hit_id": "rch_BAD"},
        {"saved_cost_cny": "0.0123"},
        {"saved_cost_cny": "0.012301"},
        {"source_generation_cost_status": "not_applicable"},
        {"billed_cost_status": "unavailable", "billed_cost_cny": None},
        {"prompt": "must-never-leave-gateway"},
    ],
)
def test_cache_hit_webhook_rejects_shape_amount_and_sensitive_mutations(
    mutation: dict[str, object],
) -> None:
    """extra/missing/type/金额等式和敏感键在签名与 outbox 前统一失败关闭。"""

    payload = {**_hit_payload(), **mutation}
    with pytest.raises(ValueError, match=r"webhook\.payload_(?:invalid|sensitive)"):
        build_webhook_event(
            tenant_id="tenant_a",
            event_type="cache.hit",
            payload=payload,
            actor_id=None,
            object_id="rch_" + "6" * 32,
            trace_id="trace_a",
            source_request_id="request_a",
            event_id="rcev_" + "8" * 32,
            timestamp=datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": 1},
        {"scope": "all", "tag_count": 1},
        {"scope": "tags", "tag_count": 0},
        {"match_mode": "any"},
        {"deleted_entry_count": 5},
        {"fenced_fill_count": -1},
        {"reason_code": "ttl_expired"},
        {"tag": "finance"},
    ],
)
def test_cache_invalidated_webhook_rejects_selector_and_count_mutations(
    mutation: dict[str, object],
) -> None:
    """失效事件不披露 selector，scope/tag/count/reason 组合必须可由持久 owner 重建。"""

    payload = {**_invalidation_payload(), **mutation}
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_a",
            event_type="cache.invalidated",
            payload=payload,
            actor_id="user_a",
            object_id="rci_" + "7" * 32,
            trace_id="trace_a",
            source_request_id="request_a",
            event_id="rcev_" + "8" * 32,
            timestamp=datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        )


def test_output_safety_not_billable_disposition_is_cache_candidate_only() -> None:
    """新增 disposition 只允许非流式、零暴露 cache candidate，不能免除 Provider 成本。"""

    payload: dict[str, object] = {
        "schema_version": 1,
        "output_safety_scan_id": "oss_" + "a" * 32,
        "request_id": "request_a",
        "scan_mode": "non_stream_full",
        "scan_status": "completed",
        "decision": "blocked",
        "error_code": "content_policy_violation",
        "exposure_state": "none",
        "emitted_token_count": 0,
        "checkpoint_count": 1,
        "dlp_rule_id": "dlp_" + "b" * 32,
        "detector_version": "output-safety-dlp-v1",
        "billing_disposition": "not_billable_response_cache_candidate",
        "occurred_at": "2026-08-10T06:00:00Z",
    }
    envelope = {
        "tenant_id": "tenant_a",
        "event_type": "security.output_blocked",
        "actor_id": None,
        "object_id": payload["output_safety_scan_id"],
        "trace_id": "trace_a",
        "source_request_id": "request_a",
        "event_id": "osevt_" + "c" * 32,
        "timestamp": datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
    }
    assert build_webhook_event(payload=payload, **envelope).payload == payload  # type: ignore[arg-type]

    for mutation in (
        {"scan_mode": "stream_window"},
        {"exposure_state": "partial", "emitted_token_count": 1},
        {"emitted_token_count": 1},
    ):
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                payload={**payload, **mutation},
                **envelope,  # type: ignore[arg-type]
            )


def _clear_response_cache_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除宿主机同名变量，使配置测试只依赖本用例显式事实。"""

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    for key in DEFAULT_CONFIG:
        if key.startswith("METAHUB_RESPONSE_CACHE_"):
            monkeypatch.delenv(key, raising=False)


def _set_production_response_cache_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置四个相互独立的生产测试 Secret，不使用仓库开发占位值。"""

    _clear_response_cache_environment(monkeypatch)
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_ENABLED", "true")
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_HMAC_SECRET", HMAC_SECRET)
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET", ENCRYPTION_SECRET
    )
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET", CONTROL_SECRET
    )
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET", FAILURE_SECRET
    )


def test_response_cache_config_defaults_keys_properties_and_summary_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dev 默认关闭但可实际验收；四密钥与 spool root 不进入 repr/model dump/摘要明文。"""

    _clear_response_cache_environment(monkeypatch)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("response-cache-contract-test")
    assert config.response_cache_enabled is False
    assert config.features["response_cache_enabled"] is False
    assert config.response_cache_hmac_key is not None
    assert config.response_cache_encryption_key is not None
    assert [item.version for item in config.response_cache_control_idempotency_keyring] == [
        1
    ]
    assert [item.version for item in config.response_cache_failure_spool_keyring] == [1]
    assert config.response_cache_max_ready_entries_per_tenant == 10_000
    assert config.response_cache_max_plaintext_bytes == 1_048_576
    assert config.response_cache_fill_lease_seconds == 90

    serialized = repr(config) + repr(config.model_dump())
    for secret in (
        config.response_cache_hmac_secret,
        config.response_cache_encryption_secret,
        config.response_cache_control_idempotency_secret,
        config.response_cache_failure_spool_secret,
    ):
        assert secret is not None
        assert secret not in serialized
    assert config.response_cache_failure_spool_root not in serialized

    summaries = {item.key: item for item in config.config_items}
    assert summaries["METAHUB_RESPONSE_CACHE_ENABLED"].value_preview == "false"
    assert summaries["METAHUB_RESPONSE_CACHE_HMAC_SECRET"].status in {
        "missing",
        "masked",
    }
    assert summaries["METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT"].status == "masked"
    assert (
        summaries["METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT"].value_preview
        == "***MASKED***"
    )


def test_response_cache_prod_requires_complete_independent_explicit_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prod 启用缺任一根域都失败关闭；完整独立配置可构造 active key。"""

    _clear_response_cache_environment(monkeypatch)
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_ENABLED", "true")
    with pytest.raises(ConfigError) as missing:
        load_app_config("response-cache-contract-test")
    assert missing.value.code == "infra.config_missing"

    _set_production_response_cache_environment(monkeypatch)
    config = load_app_config("response-cache-contract-test")
    assert config.response_cache_enabled is True
    assert config.response_cache_hmac_key is not None
    assert config.response_cache_encryption_key is not None


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        (
            {"METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_VERSION": "2"},
            "infra.config_invalid",
        ),
        (
            {
                "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_VERSION": "1",
                "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_SECRET": (
                    CONTROL_PREVIOUS_SECRET
                ),
            },
            "infra.config_invalid",
        ),
        (
            {"METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET": HMAC_SECRET},
            "infra.config_invalid",
        ),
        (
            {"METAHUB_AUTH_TOKEN_SECRET": HMAC_SECRET},
            "infra.config_invalid",
        ),
        (
            {"METAHUB_RESPONSE_CACHE_HMAC_SECRET": "short"},
            "infra.config_invalid",
        ),
    ],
)
def test_response_cache_config_rejects_partial_rotation_reuse_and_weak_secret(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, str],
    expected_code: str,
) -> None:
    """previous 必须成对且不同 version；任何跨域复用或弱 Secret 都在启动期拒绝。"""

    _set_production_response_cache_environment(monkeypatch)
    for key, value in updates.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as captured:
        load_app_config("response-cache-contract-test")
    assert captured.value.code == expected_code


def test_response_cache_config_accepts_retained_keyrings_and_masks_local_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active-first keyring 保留旧验证版本，spool 路径规范化但不进入安全摘要。"""

    _set_production_response_cache_environment(monkeypatch)
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_VERSION", "2"
    )
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_SECRET",
        CONTROL_PREVIOUS_SECRET,
    )
    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_VERSION", "2")
    monkeypatch.setenv(
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_SECRET",
        FAILURE_PREVIOUS_SECRET,
    )
    config = load_app_config("response-cache-contract-test")
    assert [item.version for item in config.response_cache_control_idempotency_keyring] == [
        1,
        2,
    ]
    assert [item.version for item in config.response_cache_failure_spool_keyring] == [
        1,
        2,
    ]

    monkeypatch.setenv("METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT", "\\\\server\\share")
    with pytest.raises(ConfigError) as path_error:
        load_app_config("response-cache-contract-test")
    assert path_error.value.code == "infra.config_invalid"
    assert "server" not in path_error.value.message


def test_response_cache_error_catalog_has_frozen_status_and_safe_owner() -> None:
    """统一错误目录精确覆盖 404/409/422/503，公开文本不包含缓存正文或密钥。"""

    expected = {
        "response_cache.policy_not_found": 404,
        "response_cache.version_conflict": 409,
        "response_cache.request_conflict": 409,
        "response_cache.input_invalid": 422,
        "response_cache.query_invalid": 422,
        "response_cache.configuration_unavailable": 503,
        "response_cache.unavailable": 503,
        "gateway.response_cache_settlement_unavailable": 503,
    }
    for code, status in expected.items():
        spec = ERROR_SPECS[code]
        assert spec.http_status == status
        assert spec.owner == "response_cache"
        public_text = f"{spec.message} {spec.suggestion}".lower()
        for forbidden in ("prompt", "response body", "provider key", "token", "ciphertext"):
            assert forbidden not in public_text


def test_response_cache_secret_validator_rejects_cross_domain_reuse() -> None:
    """纯合同层也证明四根密钥互异，调用方绕过 AppConfig 仍不能复用 Auth 域。"""

    validate_response_cache_secrets(
        hmac_secret=HMAC_SECRET,
        encryption_secret=ENCRYPTION_SECRET,
        control_idempotency_secret=CONTROL_SECRET,
        failure_spool_secret=FAILURE_SECRET,
        forbidden_secrets=("test-only-unrelated-auth-secret-00000000009",),
    )
    with pytest.raises(
        ResponseCacheContractError, match="response_cache.configuration_unavailable"
    ):
        validate_response_cache_secrets(
            hmac_secret=HMAC_SECRET,
            encryption_secret=HMAC_SECRET,
            control_idempotency_secret=CONTROL_SECRET,
            failure_spool_secret=FAILURE_SECRET,
        )
    with pytest.raises(
        ResponseCacheContractError, match="response_cache.configuration_unavailable"
    ):
        validate_response_cache_secrets(
            hmac_secret=HMAC_SECRET,
            encryption_secret=ENCRYPTION_SECRET,
            control_idempotency_secret=CONTROL_SECRET,
            failure_spool_secret=FAILURE_SECRET,
            forbidden_secrets=(HMAC_SECRET,),
        )
