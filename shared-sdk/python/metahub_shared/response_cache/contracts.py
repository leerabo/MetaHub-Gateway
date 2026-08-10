"""PRD-076 Response Cache 的纯数据、摘要、标签和金额合同。

本模块不连接数据库、不读取环境变量，也不执行租户授权。调用方必须从已经认证且
tenant-qualified 的服务端事实构造输入；Prompt、Response、Credential、摘要和标签值均通过
``repr=False`` 或仅在函数栈内存在，不能进入日志、Webhook 或验收证据。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

# canonical 与资源预算常量共同冻结跨语言 key 语义，任何变更都必须提升 schema version。
CANONICAL_SCHEMA_VERSION = 1
CACHE_KEY_SCHEMA_VERSION = 1
MAX_CANONICAL_DEPTH = 24
MAX_CANONICAL_NODES = 8_192
MAX_CANONICAL_BYTES = 2 * 1024 * 1024
MAX_CANONICAL_STRING_BYTES = 1024 * 1024
MAX_CACHE_TAGS = 32
MAX_INVALIDATION_TAGS = 16
MAX_CNY_MICROS = 9_223_372_036_854_775_807

CredentialScopeKind = Literal["none", "environment", "managed"]
CacheTagKind = Literal[
    "policy",
    "model_alias",
    "model",
    "virtual_key",
    "data_class",
    "tag",
]
CostEvidenceStatus = Literal["known", "unavailable", "not_applicable"]
CacheInvalidationScope = Literal["all", "tags"]
CacheInvalidationReason = Literal[
    "manual_finops",
    "manual_security",
    "manual_configuration",
    "manual_other",
    "policy_changed",
    "system_corrupt",
    "system_output_blocked",
]

# 正则、对象前缀与每层 HMAC domain 共同阻止字段拼接歧义和不同安全用途之间的摘要复用。
_DATA_CLASS_OR_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_CACHE_OBJECT_ID = re.compile(r"^(?:rcp|rch|rci)_[0-9a-f]{32}$")
_CANONICAL_PREFIX = b"metahub/response-cache/typed-json/v1\x00"
_PROMPT_HASH_DOMAIN = b"metahub/response-cache/prompt-hash/v1\x00"
_CREDENTIAL_SCOPE_DOMAIN = b"metahub/response-cache/credential-scope/v1\x00"
_ROUTE_REVISION_DOMAIN = b"metahub/response-cache/route-revision/v1\x00"
_CACHE_IDENTITY_DOMAIN = b"metahub/response-cache/identity/v1\x00"
_CACHE_KEY_DOMAIN = b"metahub/response-cache/final-key/v1\x00"
_TAG_DOMAIN = b"metahub/response-cache/tag/v1\x00"
_CONTROL_IDEMPOTENCY_DOMAIN = b"metahub/response-cache/control-idempotency/v1\x00"
_CONTROL_REQUEST_DOMAIN = b"metahub/response-cache/control-request/v1\x00"
_SECRET_FINGERPRINT_DOMAIN = b"metahub/response-cache/secret-fingerprint/v1\x00"


class ResponseCacheContractError(ValueError):
    """只携带固定内部错误码的 Response Cache 合同异常。"""

    def __init__(self, code: str) -> None:
        """保存固定错误码，禁止把输入对象或底层异常正文拼入异常消息。"""

        self.code = code
        super().__init__(code)


class ResponseCacheIneligible(ResponseCacheContractError):
    """表示请求不满足缓存资格，但正常 Provider 调用仍可继续。"""

    def __init__(self) -> None:
        """使用单一固定码，避免通过异常区分 Prompt 的具体非法位置。"""

        super().__init__("response_cache.ineligible")


@dataclass(frozen=True, slots=True)
class VersionedResponseCacheSecret:
    """一个只在受控内存中使用的版本化根密钥。

    ``version`` 可以进入数据库 key-state；``secret`` 绝不能进入 repr、model dump、日志或证据。
    """

    version: int
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VersionedDigest:
    """版本化不可逆摘要；摘要仍可能成为关联标识，因此从 repr 隐藏。"""

    version: int
    digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CredentialResponseScope:
    """lookup 前无副作用冻结的唯一 Credential 响应域。

    对象不持有 Provider Key、Credential ID 或公开 fingerprint，只保留 HMAC 后的 scope digest。
    """

    kind: CredentialScopeKind
    hmac_key_version: int
    scope_version: int
    digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RouteRevisionInput:
    """生成路由修订摘要所需的完整服务端快照。

    Endpoint 与上游模型名可能暴露客户本地拓扑，因此即使该 DTO 被误放入诊断也不显示这些字段。
    """

    tenant_id: str
    model_alias_id: str
    resolved_model_id: str
    provider_id: str
    provider_type: str
    provider_auth_type: str
    provider_base_url: str = field(repr=False)
    upstream_model: str = field(repr=False)
    capabilities: Mapping[str, bool]
    adapter_catalog_version: str
    adapter_catalog_hash: str = field(repr=False)
    # 尚未发布或同步被显式关闭时，快照 owner 必须保持真实的 None/None；禁止用伪 ID
    # 或版本零冒充持久快照。build_route_revision_digest 会校验 owner/status 矩阵。
    policy_snapshot_id: str | None
    policy_snapshot_version: int | None
    policy_snapshot_status: str
    credential_scope: CredentialResponseScope = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResponseCacheIdentity:
    """完整三层请求身份与最终 generation-qualified key。

    所有 digest 都从 repr 隐藏；数据库只允许保存设计明确要求的 identity/key/route digest。
    """

    hmac_key_version: int
    key_schema_version: int
    key_generation: int
    prompt_hash: str = field(repr=False)
    route_revision_digest: str = field(repr=False)
    cache_identity_digest: str = field(repr=False)
    cache_key_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CacheTag:
    """服务端可信标签；明文值只在生成 HMAC 或授权页面内存中存在。"""

    kind: CacheTagKind
    value: str = field(repr=False)

    @property
    def selector(self) -> str:
        """仅供受权 Control 请求构造 selector，调用方不得记录返回值。"""

        return f"{self.kind}:{self.value}"


@dataclass(frozen=True, slots=True)
class VersionedTagDigest:
    """一个 HMAC key version 下的标签摘要，不保留标签明文。"""

    hmac_key_version: int
    kind: CacheTagKind
    digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResponseCacheAmount:
    """CNY 微元金额及独立证据状态。

    内部始终使用整数微元；wire 层只通过 ``to_wire_value`` 生成六位小数字符串。
    """

    status: CostEvidenceStatus
    cny_micros: int | None

    def __post_init__(self) -> None:
        """冻结金额证据状态、空值组合以及 signed-64 非负微元边界。"""

        # 未知状态不能进入账务或 Webhook；错误不携带原始金额，避免异常成为证据旁路。
        if self.status not in {"known", "unavailable", "not_applicable"}:
            raise ResponseCacheContractError("response_cache.amount_invalid")
        # known 必须有严格整数微元，其他状态只能使用 null，不能把未知成本伪装为零。
        if self.status == "known":
            _nonnegative_int(self.cny_micros, maximum=MAX_CNY_MICROS)
        elif self.cny_micros is not None:
            # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
            raise ResponseCacheContractError("response_cache.amount_invalid")

    def to_wire_value(self) -> str | None:
        """把已知微元格式化为不使用指数记法的六位小数 CNY 字符串。"""

        if self.status != "known":
            return None
        assert self.cny_micros is not None
        units, micros = divmod(self.cny_micros, 1_000_000)
        return f"{units}.{micros:06d}"


@dataclass(frozen=True, slots=True)
class ResponseCacheHitAmounts:
    """保存一次命中的四组独立金额事实，并阻止历史成本与本次节省相互冒充。"""

    source_generation: ResponseCacheAmount
    current_reference: ResponseCacheAmount
    saved: ResponseCacheAmount
    billed: ResponseCacheAmount

    def __post_init__(self) -> None:
        """证明四组状态适用于真实命中，且 saved 仅由本次 reference 与 billed 派生。"""

        if any(
            amount.status not in {"known", "unavailable"}
            for amount in (
                self.source_generation,
                self.current_reference,
                self.saved,
                self.billed,
            )
        ):
            # ``not_applicable`` 只属于非 hit 的 RequestLog 投影，不能进入 cache.hit 事件。
            raise ResponseCacheContractError("response_cache.amount_invalid")
        expected = calculate_saved_amount(self.current_reference, self.billed)
        # saved 不接受调用方自报；即使数值非负，也必须与同一时点的两个价格事实相等。
        if self.saved != expected:
            raise ResponseCacheContractError("response_cache.amount_invalid")


@dataclass(frozen=True, slots=True)
class CacheHitWebhookPayload:
    """构造 ``cache.hit`` 的精确无正文载荷，并冻结收费模式对应的金额证据关系。"""

    cache_hit_id: str
    cache_policy_id: str
    cache_policy_version: int
    charge_cache_hits: bool
    amounts: ResponseCacheHitAmounts

    def to_payload(self) -> dict[str, object]:
        """生成与 runtime Webhook validator 完全一致的安全字段集合。"""

        _cache_id(self.cache_hit_id, prefix="rch_")
        _cache_id(self.cache_policy_id, prefix="rcp_")
        _positive_int(self.cache_policy_version)
        # bool 必须使用精确类型，避免整数 0/1 在不同序列化器中形成两种 wire 合同。
        if type(self.charge_cache_hits) is not bool:
            raise ResponseCacheContractError("response_cache.webhook_invalid")
        if self.charge_cache_hits:
            if self.amounts.current_reference != self.amounts.billed:
                # 收费 hit 的 billed 与 current reference 必须复用同一冻结价格证据。
                raise ResponseCacheContractError("response_cache.amount_invalid")
        elif self.amounts.billed != ResponseCacheAmount("known", 0):
            # 免费 hit 仍必须产生正式零价事实，不能用 unavailable/null 代替 0 元结算。
            raise ResponseCacheContractError("response_cache.amount_invalid")
        return {
            "schema_version": 1,
            "cache_hit_id": self.cache_hit_id,
            "cache_policy_id": self.cache_policy_id,
            "cache_policy_version": self.cache_policy_version,
            "charge_cache_hits": self.charge_cache_hits,
            **_amount_payload("source_generation_cost", self.amounts.source_generation),
            **_amount_payload("current_reference_cost", self.amounts.current_reference),
            **_amount_payload("saved_cost", self.amounts.saved),
            **_amount_payload("billed_cost", self.amounts.billed),
        }


@dataclass(frozen=True, slots=True)
class CacheInvalidatedWebhookPayload:
    """构造 ``cache.invalidated`` 精确安全计数载荷，不包含 selector 或标签明文。"""

    cache_invalidation_id: str
    scope: CacheInvalidationScope
    match_mode: Literal["all"]
    tag_count: int
    matched_entry_count: int
    deleted_entry_count: int
    fenced_fill_count: int
    reason_code: CacheInvalidationReason

    def to_payload(self) -> dict[str, object]:
        """验证 scope/count/reason 矩阵后生成固定九字段 payload。"""

        _cache_id(self.cache_invalidation_id, prefix="rci_")
        # v1 只支持 all-match；接受 any 会让同一 selector 集产生不同删除语义。
        if self.scope not in {"all", "tags"} or self.match_mode != "all":
            raise ResponseCacheContractError("response_cache.webhook_invalid")
        _nonnegative_int(self.tag_count, maximum=MAX_INVALIDATION_TAGS)
        # all 不携带 tag，tags 至少携带一个；计数不一致说明 source owner 已损坏。
        if (self.scope == "all" and self.tag_count != 0) or (
            self.scope == "tags" and self.tag_count < 1
        ):
            # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
            raise ResponseCacheContractError("response_cache.webhook_invalid")
        matched = _nonnegative_int(self.matched_entry_count, maximum=MAX_CNY_MICROS)
        deleted = _nonnegative_int(self.deleted_entry_count, maximum=MAX_CNY_MICROS)
        _nonnegative_int(self.fenced_fill_count, maximum=MAX_CNY_MICROS)
        # 删除数不能超过匹配数，reason 也只能来自数据库 CHECK 冻结的业务集合。
        if deleted > matched or self.reason_code not in {
            "manual_finops",
            "manual_security",
            "manual_configuration",
            "manual_other",
            "policy_changed",
            "system_corrupt",
            "system_output_blocked",
        }:
            # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
            raise ResponseCacheContractError("response_cache.webhook_invalid")
        return {
            "schema_version": 1,
            "cache_invalidation_id": self.cache_invalidation_id,
            "scope": self.scope,
            "match_mode": self.match_mode,
            "tag_count": self.tag_count,
            "matched_entry_count": matched,
            "deleted_entry_count": deleted,
            "fenced_fill_count": self.fenced_fill_count,
            "reason_code": self.reason_code,
        }


@dataclass(slots=True)
class _CanonicalBudget:
    """typed canonical 单次调用的节点预算，避免递归分支各自遗漏总量。"""

    nodes: int = 0

    def take_node(self) -> None:
        """每个值和 object key 都消耗一个节点，超限立即标记不适合缓存。"""

        self.nodes += 1
        if self.nodes > MAX_CANONICAL_NODES:
            raise ResponseCacheIneligible()


def typed_canonical_json_bytes(value: object) -> bytes:
    """把 JSON object 编码为版本化 typed canonical bytes。

    参数：已经由严格 JSON parser 产生的 object。返回：类型、长度、顺序均无歧义的 bytes。
    关键约束：不截断、不归一字符串，bool/int 与 int/float 始终分开；任何资源超限只表示
    ineligible，调用方必须继续正常 Provider 路径。
    """

    if type(value) is not dict:
        raise ResponseCacheIneligible()
    encoded = _encode_canonical(value, depth=1, budget=_CanonicalBudget())
    result = _CANONICAL_PREFIX + encoded
    if len(result) > MAX_CANONICAL_BYTES:
        raise ResponseCacheIneligible()
    return result


def try_build_prompt_hash(
    request: object,
    *,
    key: VersionedResponseCacheSecret,
    request_kind: str,
    public_virtual_key: bool,
    internal_invocation: bool,
) -> str | None:
    """为 Gateway eligibility 提供无异常的 Prompt hash 尝试入口。

    stream、Embeddings、内部调用和 canonical 超限都返回 ``None``；它们不是公开错误，不能
    因可选缓存能力改变原请求结果。
    """

    if request_kind != "chat" or public_virtual_key is not True or internal_invocation is not False:
        return None
    try:
        return build_prompt_hash(request, key=key)
    except ResponseCacheIneligible:
        return None


def build_prompt_hash(request: object, *, key: VersionedResponseCacheSecret) -> str:
    """对完整非流式 Chat JSON 生成版本化 HMAC，未知透传字段同样参与缓存身份。"""

    normalized = _normalized_non_stream_request(request)
    return _domain_hmac(key, _PROMPT_HASH_DOMAIN, typed_canonical_json_bytes(normalized))


def response_cache_secret_fingerprint(
    secret: VersionedResponseCacheSecret,
    purpose: Literal["hmac", "encryption"],
) -> str:
    """生成可存入 key-state 的 purpose/version-qualified 非敏感 Secret fingerprint。

    fingerprint 使用根 Secret 作为 HMAC key，消息只含固定 domain、purpose 和非敏感版本；
    因而数据库既能比较进程配置，又不会保存 Secret、可逆派生材料或跨用途相同标识。
    """

    checked = _validated_key(secret)
    # purpose 必须精确区分 cache-key 与 entry 加密域，未知用途不能产生可持久标识。
    if purpose not in {"hmac", "encryption"}:
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    message = (
        _SECRET_FINGERPRINT_DOMAIN
        + _frame(b"p", purpose.encode("ascii"))
        + _u64(checked.version)
    )
    # 固定消息由 Secret 作 HMAC key，数据库只得到不可逆且用途分离的比较值。
    return hmac.new(_secret_bytes(checked.secret), message, hashlib.sha256).hexdigest()


def build_none_credential_scope(
    *,
    key: VersionedResponseCacheSecret,
    tenant_id: str,
    provider_id: str,
    provider_auth_type: str,
    scope_version: int,
) -> CredentialResponseScope:
    """为 ``auth=none`` Endpoint 构造不伪造 Credential 的稳定响应域。"""

    # auth=none 才能使用无凭据响应域，其他认证类型必须走环境或托管 Credential 分支。
    if provider_auth_type != "none":
        raise ResponseCacheIneligible()
    payload = {
        "tenant_id": _safe_identity(tenant_id),
        "provider_id": _safe_identity(provider_id),
        "provider_auth_type": provider_auth_type,
        "scope_kind": "none",
        "scope_version": _positive_int(scope_version),
    }
    return _credential_scope(key, "none", scope_version, payload)


def build_environment_credential_scope(
    *,
    key: VersionedResponseCacheSecret,
    tenant_id: str,
    provider_id: str,
    provider_auth_type: str,
    scope_version: int,
    environment_key_fingerprint: str,
) -> CredentialResponseScope:
    """为固定环境 Credential 构造绑定配置版本与不可逆 fingerprint 的响应域。"""

    # 无认证 Endpoint 没有环境 Key fingerprint，混入该分支会制造虚假的 Credential 修订。
    if provider_auth_type == "none":
        raise ResponseCacheIneligible()
    payload = {
        "tenant_id": _safe_identity(tenant_id),
        "provider_id": _safe_identity(provider_id),
        "provider_auth_type": _safe_code(provider_auth_type),
        "scope_kind": "environment",
        "scope_version": _positive_int(scope_version),
        "environment_key_fingerprint": _digest(environment_key_fingerprint),
    }
    return _credential_scope(key, "environment", scope_version, payload)


def build_managed_credential_scope(
    *,
    key: VersionedResponseCacheSecret,
    tenant_id: str,
    provider_id: str,
    provider_auth_type: str,
    scope_version: int,
    selectable_credential_count: int,
    credential_id: str,
    credential_key_fingerprint: str,
    credential_version: int,
) -> CredentialResponseScope:
    """仅为恰好一个可选托管 Credential 构造响应域。

    多 KeyPool 无法证明同一上游账号/组织/过滤配置，本期必须 bypass，不能仅摘要 pool ID 后共享响应。
    """

    # 多个可选 Key 无法在 lookup 前证明响应等价，因此必须旁路而不是摘要 pool 身份。
    if provider_auth_type == "none" or selectable_credential_count != 1:
        raise ResponseCacheIneligible()
    payload = {
        "tenant_id": _safe_identity(tenant_id),
        "provider_id": _safe_identity(provider_id),
        "provider_auth_type": _safe_code(provider_auth_type),
        "scope_kind": "managed",
        "scope_version": _positive_int(scope_version),
        "credential_id": _safe_identity(credential_id),
        "credential_key_fingerprint": _digest(credential_key_fingerprint),
        "credential_version": _positive_int(credential_version),
        "selectable_credential_count": 1,
    }
    return _credential_scope(key, "managed", scope_version, payload)


def build_route_revision_digest(
    route: RouteRevisionInput,
    *,
    key: VersionedResponseCacheSecret,
) -> str:
    """绑定 Alias、真实 Endpoint、Adapter、Policy 与唯一 Credential scope 的完整路由修订。"""

    # 只接受冻结 DTO，禁止宽松 mapping 遗漏未来新增的路由身份字段。
    if not isinstance(route, RouteRevisionInput):
        raise ResponseCacheIneligible()
    # Credential scope 必须由同一 HMAC version 生成，跨版本拼接会形成不可验证身份。
    if route.credential_scope.hmac_key_version != key.version:
        raise ResponseCacheIneligible()
    capabilities = _canonical_capabilities(route.capabilities)
    policy_snapshot_status = _safe_code(route.policy_snapshot_status)
    if policy_snapshot_status not in {
        "disabled",
        "not_published",
        "current",
        "lkg",
        "emergency_lkg",
    }:
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.contract_invalid")
    # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
    if route.policy_snapshot_id is None and route.policy_snapshot_version is None:
        # null head 在刷新失败后也可能成为 LKG；current 则必然拥有真实快照 owner。
        if policy_snapshot_status not in {
            "disabled",
            "not_published",
            "lkg",
            "emergency_lkg",
        }:
            # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
            raise ResponseCacheContractError("response_cache.contract_invalid")
        policy_snapshot_id: str | None = None
        policy_snapshot_version: int | None = None
    # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
    elif route.policy_snapshot_id is not None and route.policy_snapshot_version is not None:
        # disabled/not_published 表示权威 null head，不能同时声称存在持久快照。
        if policy_snapshot_status in {"disabled", "not_published"}:
            raise ResponseCacheContractError("response_cache.contract_invalid")
        policy_snapshot_id = _safe_identity(route.policy_snapshot_id)
        policy_snapshot_version = _positive_int(route.policy_snapshot_version)
    else:
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.contract_invalid")
    payload = {
        "tenant_id": _safe_identity(route.tenant_id),
        "model_alias_id": _safe_identity(route.model_alias_id),
        "resolved_model_id": _safe_identity(route.resolved_model_id),
        "provider_id": _safe_identity(route.provider_id),
        "provider_type": _safe_code(route.provider_type),
        "provider_auth_type": _safe_code(route.provider_auth_type),
        "provider_base_url": _bounded_text(route.provider_base_url, maximum=2_048),
        "upstream_model": _bounded_text(route.upstream_model, maximum=512),
        "capabilities": capabilities,
        "adapter_catalog_version": _bounded_text(route.adapter_catalog_version, maximum=128),
        "adapter_catalog_hash": _digest(route.adapter_catalog_hash),
        "policy_snapshot_id": policy_snapshot_id,
        "policy_snapshot_version": policy_snapshot_version,
        "policy_snapshot_status": policy_snapshot_status,
        "credential_scope_kind": route.credential_scope.kind,
        "credential_scope_version": _positive_int(route.credential_scope.scope_version),
        "credential_scope_digest": _digest(route.credential_scope.digest),
    }
    return _domain_hmac(key, _ROUTE_REVISION_DOMAIN, typed_canonical_json_bytes(payload))


def build_cache_identity_digest(
    *,
    key: VersionedResponseCacheSecret,
    tenant_id: str,
    virtual_key_id: str,
    data_class: str,
    model_id: str,
    prompt_hash: str,
    route_revision_digest: str,
    cache_policy_id: str,
    cache_policy_version: int,
    cache_policy_generation: int,
) -> str:
    """生成不含 key generation 的稳定身份摘要，使数据库能先读取并推进安全围栏。"""

    if _DATA_CLASS_OR_TAG.fullmatch(data_class) is None:
        raise ResponseCacheIneligible()
    _cache_id(cache_policy_id, prefix="rcp_")
    payload = {
        "key_schema_version": CACHE_KEY_SCHEMA_VERSION,
        "hmac_key_version": _validated_key(key).version,
        "tenant_id": _safe_identity(tenant_id),
        "virtual_key_id": _safe_identity(virtual_key_id),
        "data_class": data_class,
        "model_id": _safe_identity(model_id),
        "prompt_hash": _digest(prompt_hash),
        "route_revision_digest": _digest(route_revision_digest),
        "cache_policy_id": cache_policy_id,
        "cache_policy_version": _positive_int(cache_policy_version),
        "cache_policy_generation": _positive_int(cache_policy_generation),
    }
    return _domain_hmac(key, _CACHE_IDENTITY_DOMAIN, typed_canonical_json_bytes(payload))


def build_cache_key_digest(
    *,
    key: VersionedResponseCacheSecret,
    cache_identity_digest: str,
    key_generation: int,
) -> str:
    """把稳定 identity 与数据库读取的 generation 组合为最终 key。"""

    payload = {
        "key_schema_version": CACHE_KEY_SCHEMA_VERSION,
        "hmac_key_version": _validated_key(key).version,
        "cache_identity_digest": _digest(cache_identity_digest),
        "key_generation": _positive_int(key_generation),
    }
    return _domain_hmac(key, _CACHE_KEY_DOMAIN, typed_canonical_json_bytes(payload))


def build_response_cache_identity(
    request: object,
    route: RouteRevisionInput,
    *,
    key: VersionedResponseCacheSecret,
    virtual_key_id: str,
    data_class: str,
    cache_policy_id: str,
    cache_policy_version: int,
    cache_policy_generation: int,
    key_generation: int,
) -> ResponseCacheIdentity:
    """一次性构造 Prompt、路由、稳定 identity 与 generation-qualified 最终缓存键。"""

    prompt_hash = build_prompt_hash(request, key=key)
    route_digest = build_route_revision_digest(route, key=key)
    identity_digest = build_cache_identity_digest(
        key=key,
        tenant_id=route.tenant_id,
        virtual_key_id=virtual_key_id,
        data_class=data_class,
        model_id=route.resolved_model_id,
        prompt_hash=prompt_hash,
        route_revision_digest=route_digest,
        cache_policy_id=cache_policy_id,
        cache_policy_version=cache_policy_version,
        cache_policy_generation=cache_policy_generation,
    )
    final_digest = build_cache_key_digest(
        key=key,
        cache_identity_digest=identity_digest,
        key_generation=key_generation,
    )
    return ResponseCacheIdentity(
        hmac_key_version=key.version,
        key_schema_version=CACHE_KEY_SCHEMA_VERSION,
        key_generation=key_generation,
        prompt_hash=prompt_hash,
        route_revision_digest=route_digest,
        cache_identity_digest=identity_digest,
        cache_key_digest=final_digest,
    )


def parse_cache_tag(selector: object) -> CacheTag:
    """严格解析一个 ``kind:value`` selector，不 trim 或静默规范化。"""

    # selector 必须只有一个命名空间分隔符，trim 或额外冒号都会改变管理员失效意图。
    if type(selector) is not str or selector.count(":") != 1:
        raise ResponseCacheContractError("response_cache.tag_invalid")
    kind, value = selector.split(":", 1)
    # 未知命名空间不能静默退化为自定义 tag，否则授权页面与数据库 generation 会分叉。
    if kind not in {"policy", "model_alias", "model", "virtual_key", "data_class", "tag"}:
        raise ResponseCacheContractError("response_cache.tag_invalid")
    if kind in {"data_class", "tag"}:
        # 管理员可配置值只能是有界 machine code，禁止自由文本进入失效 owner。
        if _DATA_CLASS_OR_TAG.fullmatch(value) is None:
            raise ResponseCacheContractError("response_cache.tag_invalid")
    else:
        _safe_identity(value)
    return CacheTag(kind=kind, value=value)  # type: ignore[arg-type]


def canonicalize_cache_tags(
    tags: Sequence[CacheTag],
    *,
    maximum: int = MAX_CACHE_TAGS,
) -> tuple[CacheTag, ...]:
    """校验、拒绝重复并按 kind/value 对可信标签确定性排序。"""

    # 集合类型、数量上限和非空要求共同防止无界 selector 扩大失效事务。
    if (
        type(tags) not in {tuple, list}
        or type(maximum) is not int
        or not 1 <= maximum <= MAX_CACHE_TAGS
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.tag_invalid")
    # 空集合没有明确 all/tags 语义，超过上限则可能放大 HMAC 与数据库锁持有时间。
    if not 1 <= len(tags) <= maximum:
        raise ResponseCacheContractError("response_cache.tag_invalid")
    normalized: list[CacheTag] = []
    seen: set[tuple[str, str]] = set()
    for tag in tags:
        # 只接受可信 DTO，mapping 或字符串不能绕过命名空间解析与 repr 脱敏。
        if not isinstance(tag, CacheTag):
            raise ResponseCacheContractError("response_cache.tag_invalid")
        checked = parse_cache_tag(tag.selector)
        identity = (checked.kind, checked.value)
        # 重复 selector 通常代表客户端意图含糊，不能静默去重后改变 request fingerprint。
        if identity in seen:
            raise ResponseCacheContractError("response_cache.tag_invalid")
        seen.add(identity)
        normalized.append(checked)
    return tuple(sorted(normalized, key=lambda item: (item.kind, item.value)))


def build_tag_digest(
    *,
    key: VersionedResponseCacheSecret,
    tenant_id: str,
    tag: CacheTag,
) -> VersionedTagDigest:
    """生成 tenant 与 key version 双重限定的标签 HMAC，不返回或持久化标签明文。"""

    checked = parse_cache_tag(tag.selector)
    payload = {
        "tenant_id": _safe_identity(tenant_id),
        "hmac_key_version": _validated_key(key).version,
        "tag_kind": checked.kind,
        "tag_value": checked.value,
    }
    digest = _domain_hmac(key, _TAG_DOMAIN, typed_canonical_json_bytes(payload))
    return VersionedTagDigest(key.version, checked.kind, digest)


def build_tag_digests(
    keyring: Sequence[VersionedResponseCacheSecret],
    *,
    tenant_id: str,
    selectors: Sequence[CacheTag],
) -> tuple[VersionedTagDigest, ...]:
    """为 active 与 retained HMAC versions 生成完整 selector 摘要笛卡尔集用于跨版本查询。"""

    keys = validate_secret_keyring(keyring)
    tags = canonicalize_cache_tags(selectors, maximum=MAX_INVALIDATION_TAGS)
    return tuple(
        build_tag_digest(key=key, tenant_id=tenant_id, tag=tag)
        for key in keys
        for tag in tags
    )


def build_control_idempotency_digests(
    keyring: Sequence[VersionedResponseCacheSecret],
    *,
    tenant_id: str,
    actor_user_id: str,
    idempotency_key: str,
) -> tuple[VersionedDigest, ...]:
    """用独立 Control keyring 生成全部 retained-version owner，防止轮换后重复执行旧意图。"""

    keys = validate_secret_keyring(keyring)
    visible_key = _visible_ascii(idempotency_key, minimum=16, maximum=128)
    payload = typed_canonical_json_bytes(
        {
            "tenant_id": _safe_identity(tenant_id),
            "actor_user_id": _safe_identity(actor_user_id),
            "idempotency_key": visible_key,
        }
    )
    return tuple(
        VersionedDigest(
            key.version,
            _domain_hmac(key, _CONTROL_IDEMPOTENCY_DOMAIN, payload),
        )
        for key in keys
    )


def build_control_request_fingerprint(
    *,
    key: VersionedResponseCacheSecret,
    tenant_id: str,
    actor_user_id: str,
    operation: str,
    request_body: object,
) -> VersionedDigest:
    """对严格写操作意图生成 tenant、actor 与 operation 限定的 HMAC 冲突指纹。"""

    payload = {
        "tenant_id": _safe_identity(tenant_id),
        "actor_user_id": _safe_identity(actor_user_id),
        "operation": _safe_code(operation),
        "request_body": request_body,
    }
    digest = _domain_hmac(key, _CONTROL_REQUEST_DOMAIN, typed_canonical_json_bytes(payload))
    return VersionedDigest(key.version, digest)


def validate_secret_keyring(
    keyring: Sequence[VersionedResponseCacheSecret],
) -> tuple[VersionedResponseCacheSecret, ...]:
    """验证 active-first keyring 的版本唯一性和每个 Secret 强度。"""

    # active-first keyring 必须显式非空，调用方不能把缺失密钥误解释为无需幂等验证。
    if type(keyring) not in {tuple, list} or not keyring:
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    result: list[VersionedResponseCacheSecret] = []
    versions: set[int] = set()
    secrets: list[bytes] = []
    for key in keyring:
        checked = _validated_key(key)
        # 同一版本只能对应一把 Secret，否则数据库 owner 无法确定验证材料。
        if checked.version in versions:
            raise ResponseCacheContractError("response_cache.configuration_unavailable")
        encoded = _secret_bytes(checked.secret)
        # 不同版本也禁止复用物理 Secret，确保轮换确实改变验证根域而非只改数字。
        if any(hmac.compare_digest(encoded, existing) for existing in secrets):
            raise ResponseCacheContractError("response_cache.configuration_unavailable")
        versions.add(checked.version)
        secrets.append(encoded)
        result.append(checked)
    return tuple(result)


def calculate_saved_amount(
    current_reference: ResponseCacheAmount,
    billed: ResponseCacheAmount,
) -> ResponseCacheAmount:
    """按同次证据计算 ``max`` 不参与的严格 reference-billed 差额。"""

    if current_reference.status != "known" or billed.status != "known":
        return ResponseCacheAmount("unavailable", None)
    assert current_reference.cny_micros is not None and billed.cny_micros is not None
    # billed 高于 reference 会产生负节省，说明价格 owner 不一致而不是可展示的业务结果。
    if billed.cny_micros > current_reference.cny_micros:
        raise ResponseCacheContractError("response_cache.amount_invalid")
    return ResponseCacheAmount("known", current_reference.cny_micros - billed.cny_micros)


def _encode_canonical(value: object, *, depth: int, budget: _CanonicalBudget) -> bytes:
    """递归编码一个 JSON value；每个节点统一使用 tag+u64 payload length。"""

    if depth > MAX_CANONICAL_DEPTH:
        raise ResponseCacheIneligible()
    budget.take_node()
    if value is None:
        return _frame(b"n", b"")
    if type(value) is bool:
        return _frame(b"b", b"1" if value else b"0")
    if type(value) is int:
        try:
            payload = str(value).encode("ascii")
        # 非法整数表示不能降级为字符串，否则同一值会形成两种 typed canonical。
        except (ValueError, UnicodeEncodeError):
            raise ResponseCacheIneligible() from None
        return _frame(b"i", payload)
    if type(value) is float:
        if not math.isfinite(value):
            raise ResponseCacheIneligible()
        return _frame(b"f", struct.pack(">d", value).hex().encode("ascii"))
    if type(value) is str:
        return _frame(b"s", _utf8(value))
    if type(value) is list:
        children = [_u64(len(value))]
        for child in value:
            children.append(_encode_canonical(child, depth=depth + 1, budget=budget))
        return _frame(b"l", _bounded_join(children))
    if type(value) is dict:
        keys = list(value)
        if any(type(key) is not str for key in keys):
            raise ResponseCacheIneligible()
        children = [_u64(len(keys))]
        for key in sorted(keys):
            budget.take_node()
            children.append(_frame(b"k", _utf8(key)))
            children.append(_encode_canonical(value[key], depth=depth + 1, budget=budget))
        return _frame(b"o", _bounded_join(children))
    raise ResponseCacheIneligible()


def _frame(tag: bytes, payload: bytes) -> bytes:
    """给单一 typed 节点增加一字节标签和无符号 64 位长度。"""

    framed = tag + _u64(len(payload)) + payload
    if len(framed) > MAX_CANONICAL_BYTES:
        raise ResponseCacheIneligible()
    return framed


def _bounded_join(parts: Sequence[bytes]) -> bytes:
    """在拼接容器 payload 前累计检查上限，避免先制造无界临时 bytes。"""

    total = 0
    for part in parts:
        total += len(part)
        if total > MAX_CANONICAL_BYTES:
            raise ResponseCacheIneligible()
    return b"".join(parts)


def _u64(value: int) -> bytes:
    """编码 canonical 唯一允许的无符号长度前缀。"""

    if type(value) is not int or not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF:
        raise ResponseCacheIneligible()
    return value.to_bytes(8, "big", signed=False)


def _utf8(value: str) -> bytes:
    """保留原字符串编码并拒绝 surrogate 与单字符串资源放大。"""

    try:
        encoded = value.encode("utf-8", errors="strict")
    # 孤立 surrogate 无法跨语言稳定编码，超出资格时只旁路缓存而不截断正文。
    except UnicodeEncodeError:
        raise ResponseCacheIneligible() from None
    if len(encoded) > MAX_CANONICAL_STRING_BYTES:
        raise ResponseCacheIneligible()
    return encoded


def _normalized_non_stream_request(value: object) -> dict[str, object]:
    """只统一缺失 stream 与显式 false，其他字段和值完全保留。"""

    if type(value) is not dict:
        raise ResponseCacheIneligible()
    stream = value.get("stream", False)
    if type(stream) is not bool or stream is True:
        raise ResponseCacheIneligible()
    normalized = dict(value)
    normalized["stream"] = False
    return normalized


def _credential_scope(
    key: VersionedResponseCacheSecret,
    kind: CredentialScopeKind,
    scope_version: int,
    payload: dict[str, object],
) -> CredentialResponseScope:
    """集中构造只保留不可逆摘要的 Credential scope DTO，避免凭据身份进入日志。"""

    digest = _domain_hmac(key, _CREDENTIAL_SCOPE_DOMAIN, typed_canonical_json_bytes(payload))
    return CredentialResponseScope(kind, key.version, _positive_int(scope_version), digest)


def _domain_hmac(key: VersionedResponseCacheSecret, domain: bytes, payload: bytes) -> str:
    """统一绑定 HMAC domain、key version 与 payload 长度，消除跨层拼接二义性。"""

    checked = _validated_key(key)
    message = domain + _u64(checked.version) + _u64(len(payload)) + payload
    # Secret 只作为 HMAC key 存在于当前栈帧，返回值仅为不可逆小写摘要。
    return hmac.new(_secret_bytes(checked.secret), message, hashlib.sha256).hexdigest()


def _validated_key(value: object) -> VersionedResponseCacheSecret:
    """验证版本化密钥 DTO，拒绝宽松 mapping、tuple 以及 bool 冒充整数版本。"""

    # 宽松容器可能漏掉 repr=False 语义或隐式转换版本，必须只接受冻结 DTO。
    if not isinstance(value, VersionedResponseCacheSecret):
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    _positive_int(value.version)
    _secret_bytes(value.secret)
    return value


def _secret_bytes(value: object) -> bytes:
    """把根密钥规范为 32..4096 UTF-8 bytes，不 trim 错误配置。"""

    # 不 trim 错误配置；不同进程对空白处理不一致会导致无法解密或命中。
    if type(value) is not str or value != value.strip():
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    try:
        encoded = value.encode("utf-8", errors="strict")
    # 非法 Unicode 无法在 Control/Gateway 间稳定形成同一 HMAC key。
    except UnicodeEncodeError:
        raise ResponseCacheContractError("response_cache.configuration_unavailable") from None
    # 根密钥必须有最小强度且有界，防止弱值和异常大的环境变量进入密码学调用。
    if not 32 <= len(encoded) <= 4_096:
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    return encoded


def _safe_identity(value: object) -> str:
    """验证只用于摘要的服务端身份，拒绝空白、控制字符和无界值。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 160
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ResponseCacheIneligible()
    return value


def _safe_code(value: object) -> str:
    """验证稳定枚举/code 文本，不接受大小写或空白归一。"""

    if type(value) is not str or _DATA_CLASS_OR_TAG.fullmatch(value) is None:
        raise ResponseCacheIneligible()
    return value


def _bounded_text(value: object, *, maximum: int) -> str:
    """验证需要精确保留但不适合 machine-code 正则的路由文本。"""

    if type(value) is not str or not value or len(value) > maximum:
        raise ResponseCacheIneligible()
    try:
        value.encode("utf-8", errors="strict")
    # Endpoint/模型文本必须逐字可编码，不能在摘要前使用替换字符修复。
    except UnicodeEncodeError:
        raise ResponseCacheIneligible() from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ResponseCacheIneligible()
    return value


def _visible_ascii(value: object, *, minimum: int, maximum: int) -> str:
    """验证 HTTP Idempotency-Key 的可见 ASCII 精确值，禁止 trim 或编码归一。"""

    # Header owner 只接受可见 ASCII，避免代理层折叠空白或多种 Unicode 编码。
    if (
        type(value) is not str
        or not minimum <= len(value) <= maximum
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.input_invalid")
    return value


def _digest(value: object) -> str:
    """验证内部持久摘要使用无前缀小写 64 hex。"""

    if type(value) is not str or _LOWER_HEX_64.fullmatch(value) is None:
        raise ResponseCacheIneligible()
    return value


def _cache_id(value: object, *, prefix: str) -> str:
    """验证 Response Cache 持久对象使用领域前缀和 UUID4 等长 hex。"""

    # 对象 ID 必须匹配领域前缀与固定 32 hex，任意文本不能成为 Webhook owner。
    if (
        type(value) is not str
        or not value.startswith(prefix)
        or _CACHE_OBJECT_ID.fullmatch(value) is None
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.webhook_invalid")
    return value


def _positive_int(value: object) -> int:
    """验证 SQLite signed-64 正整数并拒绝 bool，保证版本与 generation 可持久。"""

    # Python bool 是 int 子类，使用 exact type 防止 true 被解释为版本一。
    if type(value) is not int or not 1 <= value <= MAX_CNY_MICROS:
        raise ResponseCacheContractError("response_cache.contract_invalid")
    return value


def _nonnegative_int(value: object, *, maximum: int) -> int:
    """验证计数/金额的严格非负整数边界。"""

    # 计数与金额不接受 bool、负数或越过调用方冻结上限的无界整数。
    if type(value) is not int or not 0 <= value <= maximum:
        raise ResponseCacheContractError("response_cache.contract_invalid")
    return value


def _canonical_capabilities(value: Mapping[str, bool]) -> dict[str, bool]:
    """冻结 Adapter capability map，未知值类型不能被真值转换。"""

    if type(value) is not dict or not 1 <= len(value) <= 64:
        raise ResponseCacheIneligible()
    result: dict[str, bool] = {}
    for name, enabled in value.items():
        code = _safe_code(name)
        if type(enabled) is not bool:
            raise ResponseCacheIneligible()
        result[code] = enabled
    return result


def _amount_payload(prefix: str, amount: ResponseCacheAmount) -> dict[str, object]:
    """把内部金额映射为 Webhook 状态和值字段对，并拒绝非命中专用状态。"""

    # not_applicable 只用于非 hit RequestLog，发送 cache.hit 会伪造一次成本证据。
    if amount.status == "not_applicable":
        raise ResponseCacheContractError("response_cache.webhook_invalid")
    return {f"{prefix}_status": amount.status, f"{prefix}_cny": amount.to_wire_value()}
