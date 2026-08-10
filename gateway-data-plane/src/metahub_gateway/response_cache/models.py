"""Gateway Response Cache 的单请求 owner、候选与结算模型。

这些对象只在认证、数据库事务、加解密和响应栈之间传递。可关联摘要、data class、标签、
lease token、密文和解密正文全部隐藏于 ``repr``；任何对象都不得直接写入日志、Webhook、
RequestLog 或验收证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from metahub_shared.response_cache import (
    EncryptedResponseCachePayload,
    ResponseCacheEntryOwner,
    ResponseCacheIdentity,
    VersionedTagDigest,
)

ResponseCacheRequestStatus = Literal[
    "disabled",
    "ineligible",
    "bypassed_experiment",
    "unavailable_bypass",
    "eligible",
    "miss_owner",
    "miss_non_owner",
    "hit_candidate",
    "corrupt_miss",
]


@dataclass(frozen=True, slots=True)
class ResponseCacheKeyState:
    """一次 lookup 事务读取的数据库单例密钥状态。

    fingerprint 虽然不可逆，仍可能用于跨部署关联，因此只允许等时比较并从 repr 隐藏。
    ``generation`` 用于停服轮换的全局 ABA 围栏，不能由进程本地猜测。
    """

    schema_version: int
    active_hmac_key_version: int
    active_encryption_key_version: int
    hmac_secret_fingerprint: str = field(repr=False)
    encryption_secret_fingerprint: str = field(repr=False)
    generation: int


@dataclass(frozen=True, slots=True)
class ResponseCachePolicySnapshot:
    """当前 tenant + active Alias 的完整 CachePolicy 快照。

    ``data_class`` 与自定义标签只用于构造不可逆缓存域，不能进入调用日志或运行事件。
    version 与 invalidation generation 必须在 hit/fill 线性化事务内再次逐项验证。
    """

    tenant_id: str
    cache_policy_id: str
    model_alias_id: str
    status: Literal["enabled", "disabled"]
    data_class: str = field(repr=False)
    ttl_seconds: int
    charge_cache_hits: bool
    tags: tuple[str, ...] = field(repr=False)
    version: int
    invalidation_generation: int
    single_data_class_confirmed: bool


@dataclass(frozen=True, slots=True)
class ResponseCacheTagGeneration:
    """fill/hit 冻结的 tenant + key-version 标签代际。

    标签摘要和代际必须成对复核；只比较 digest 而不比较 generation 会允许手动失效后的
    迟到写回，只比较 generation 而不比较 digest 则可能把不同标签错误合并。
    """

    tag: VersionedTagDigest = field(repr=False)
    generation: int


@dataclass(frozen=True, slots=True)
class ResponseCacheFillLease:
    """单写者 fill lease 的完整不可伪造句柄。

    owner request/trace 与 token 只供 commit/release 的 tenant-qualified CAS 使用；调用方不得
    序列化或复制字段到异常。``identity`` 和标签代际共同防止 policy/tag/key 失效后的旧写回。
    """

    tenant_id: str
    fill_lease_id: str
    owner_request_id: str = field(repr=False)
    owner_trace_id: str = field(repr=False)
    lease_owner_id: str
    lease_generation: int
    lease_token: str = field(repr=False)
    policy: ResponseCachePolicySnapshot
    identity: ResponseCacheIdentity = field(repr=False)
    encryption_key_version: int
    key_state_generation: int
    route_revision_digest: str = field(repr=False)
    credential_scope_digest: str = field(repr=False)
    # 策略权威 null head 使用真实 None/None；route digest 已校验 owner/status 组合。
    policy_snapshot_id: str | None = field(repr=False)
    policy_snapshot_version: int | None
    policy_snapshot_status: str
    tags: tuple[ResponseCacheTagGeneration, ...] = field(repr=False)
    expires_at: str


@dataclass(frozen=True, slots=True)
class ResponseCacheCandidate:
    """已经通过 lookup owner 检查与 AES-GCM 验真的 cache-hit candidate。

    解密 JSON 仅在当前请求内存中交给 Output Safety；candidate 不是命中事实，只有后续原子
    settlement commit 成功才允许返回正文。entry owner 用于二次 TTL/policy/route/key 重验。
    """

    owner: ResponseCacheEntryOwner = field(repr=False)
    encrypted: EncryptedResponseCachePayload = field(repr=False)
    payload: dict[str, object] = field(repr=False)
    policy: ResponseCachePolicySnapshot
    identity: ResponseCacheIdentity = field(repr=False)
    credential_scope_digest: str = field(repr=False)
    tags: tuple[ResponseCacheTagGeneration, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedResponseCache:
    """Rate Limit 后一次 cache lookup/reservation 的封闭结果。

    每个状态只允许携带对应 owner：``hit_candidate`` 必须有 candidate，``miss_owner`` 必须有
    fill lease，其他状态二者都为空。固定 reason 只用于安全 RequestLog 投影，不含输入细节。
    """

    status: ResponseCacheRequestStatus
    policy: ResponseCachePolicySnapshot | None = None
    candidate: ResponseCacheCandidate | None = field(default=None, repr=False)
    fill_lease: ResponseCacheFillLease | None = field(default=None, repr=False)
    reason_code: str | None = None

    def __post_init__(self) -> None:
        """拒绝松散的状态/owner 组合，防止重构后意外返回未结算正文。"""

        candidate_expected = self.status == "hit_candidate"
        fill_expected = self.status in {"miss_owner", "corrupt_miss"}
        if (self.candidate is not None) != candidate_expected:
            # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
            raise ValueError("response_cache_candidate_state_invalid")
        if (self.fill_lease is not None) != fill_expected:
            # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
            raise ValueError("response_cache_fill_state_invalid")
        if self.status in {
            "eligible",
            "miss_owner",
            "miss_non_owner",
            "hit_candidate",
            "corrupt_miss",
        } and self.policy is None:
            # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
            raise ValueError("response_cache_policy_state_invalid")


@dataclass(frozen=True, slots=True)
class ResponseCacheSettlementIds:
    """hit 事务前预生成的全部幂等 owner ID。

    commit 抛出异常后只能按这组 ID exact read-back；禁止重新生成 ID 或仅检查其中一张表，
    否则无法区分完整提交、完整回滚和 partial integrity failure。
    """

    cache_hit_id: str
    usage_ledger_id: str
    finops_ledger_entry_id: str
    finops_event_id: str
    reference_price_snapshot_id: str
    billed_price_snapshot_id: str
    response_cache_event_id: str


@dataclass(frozen=True, slots=True)
class ResponseCacheFillResult:
    """Provider 成功后的机会性 fill 结果，不携带 entry 或摘要身份。"""

    status: Literal["stored", "skipped", "lease_lost"]


__all__ = [
    "PreparedResponseCache",
    "ResponseCacheCandidate",
    "ResponseCacheFillLease",
    "ResponseCacheFillResult",
    "ResponseCacheKeyState",
    "ResponseCachePolicySnapshot",
    "ResponseCacheRequestStatus",
    "ResponseCacheSettlementIds",
    "ResponseCacheTagGeneration",
]
