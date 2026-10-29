"""Response Cache Control API 的严格请求、响应与 RequestLog 安全投影合同。"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CachePolicyStatus = Literal["enabled", "disabled"]
CostEvidenceStatus = Literal["known", "unavailable", "not_applicable"]
ResponseCacheRequestStatus = Literal[
    "disabled",
    "ineligible",
    "bypassed_experiment",
    "unavailable_bypass",
    "corrupt_miss",
    "miss_owner",
    "miss_non_owner",
    "fill_stored",
    "fill_skipped",
    "hit",
    "hit_blocked",
    "settlement_failed",
]

# 配置值会进入受权页面和审计快照，因此只接受短 machine code；自由文本不得穿透。
_MACHINE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CNY_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{6}$")


class PatchCachePolicyRequest(BaseModel):
    """完整 CAS 策略快照；tenant、actor 与版本权威值均由后端恢复。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    model_alias_id: StrictStr = Field(min_length=1, max_length=128)
    status: CachePolicyStatus
    data_class: StrictStr | None
    single_data_class_confirmed: StrictBool
    ttl_seconds: StrictInt = Field(ge=60, le=86_400)
    charge_cache_hits: StrictBool
    tags: list[StrictStr] = Field(min_length=0, max_length=16)
    expected_version: StrictInt = Field(ge=0)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        """拒绝重复或自由文本标签，避免服务端静默改写管理员意图。"""

        if len(set(value)) != len(value) or any(
            _MACHINE_CODE.fullmatch(item) is None for item in value
        ):
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.input_invalid")
        return value

    @model_validator(mode="after")
    def validate_policy_state(self) -> PatchCachePolicyRequest:
        """启用必须重新确认单一 data class，禁用不能保留可误解的确认位。"""

        valid_data_class = self.data_class is not None and _MACHINE_CODE.fullmatch(self.data_class)
        if self.status == "enabled":
            if not valid_data_class or not self.single_data_class_confirmed:
                # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
                raise ValueError("response_cache.input_invalid")
        elif self.single_data_class_confirmed or (
            self.data_class is not None and _MACHINE_CODE.fullmatch(self.data_class) is None
        ):
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.input_invalid")
        return self


class InvalidateResponseCacheRequest(BaseModel):
    """手工失效请求，只接收固定 selector 与 reason，不接收 tenant 或自由审计文本。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Literal["all", "tags"]
    match_mode: Literal["all"]
    tags: list[StrictStr] = Field(min_length=0, max_length=16)
    reason_code: Literal[
        "manual_finops",
        "manual_security",
        "manual_configuration",
        "manual_other",
    ]

    @model_validator(mode="after")
    def validate_scope(self) -> InvalidateResponseCacheRequest:
        """全量与标签失效采用互斥形态，重复 selector 不能被静默去重。"""

        if self.scope == "all":
            if self.tags:
                # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
                raise ValueError("response_cache.input_invalid")
        elif not self.tags or len(set(self.tags)) != len(self.tags):
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.input_invalid")
        return self


class CachePolicySummary(BaseModel):
    """GET/PATCH 复用的租户镜像策略 DTO，不暴露 entry 或内部摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    cache_policy_id: str
    tenant_id: str
    model_alias_id: str
    status: CachePolicyStatus
    data_class: str | None
    single_data_class_confirmed: bool
    ttl_seconds: int = Field(ge=60, le=86_400)
    charge_cache_hits: bool
    tags: list[str] = Field(max_length=16)
    version: int = Field(ge=1)
    ready_entry_count: int = Field(ge=0)
    updated_at: datetime


class CachePolicyListItem(BaseModel):
    """一个 active Alias 及其可空策略；三个 tenant/alias owner 必须由服务层交叉核对。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str
    model_alias_id: str
    alias: str
    environment_id: str
    environment_code: str
    cache_policy: CachePolicySummary | None


class CachePolicyListResponse(BaseModel):
    """有界、签名游标分页的 Cache Policy 列表响应。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str
    items: list[CachePolicyListItem]
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    has_more: bool
    global_enabled: bool
    request_id: str
    trace_id: str


class CachePolicyPatchResponse(BaseModel):
    """策略写入或精确幂等重放的安全业务结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str
    cache_policy: CachePolicySummary
    changed: bool
    idempotent_replay: bool
    invalidated_entry_count: int = Field(ge=0)
    fenced_fill_count: int = Field(ge=0)
    request_id: str
    trace_id: str


class CacheInvalidationSummary(BaseModel):
    """手工失效不可变事实的公开白名单。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    cache_invalidation_id: str
    tenant_id: str
    scope: Literal["all", "tags"]
    match_mode: Literal["all"]
    tag_count: int = Field(ge=0, le=16)
    matched_entry_count: int = Field(ge=0)
    deleted_entry_count: int = Field(ge=0)
    fenced_fill_count: int = Field(ge=0)
    reason_code: Literal[
        "manual_finops",
        "manual_security",
        "manual_configuration",
        "manual_other",
        "policy_changed",
        "system_corrupt",
        "system_output_blocked",
    ]
    created_at: datetime


class CacheInvalidationResponse(BaseModel):
    """失效命令或精确幂等重放响应。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str
    invalidation: CacheInvalidationSummary
    idempotent_replay: bool
    request_id: str
    trace_id: str


class ResponseCacheRequestLogInfo(BaseModel):
    """RequestLog 详情中的 exact Response Cache 投影，不含正文、tag 或内部 owner。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str
    status: ResponseCacheRequestStatus
    hit: bool
    charge_cache_hits: bool | None
    cache_policy_id: str | None
    cache_policy_version: int | None = Field(default=None, ge=1)
    source_generation_cost_status: CostEvidenceStatus
    source_generation_cost_cny: str | None
    current_reference_cost_status: CostEvidenceStatus
    current_reference_cost_cny: str | None
    saved_cost_status: CostEvidenceStatus
    saved_cost_cny: str | None
    billed_cost_status: CostEvidenceStatus
    billed_cost_cny: str | None

    @model_validator(mode="after")
    def validate_evidence(self) -> ResponseCacheRequestLogInfo:
        """把 hit、policy owner 与四组金额状态锁成可机械验证的组合。"""

        amount_pairs = (
            (self.source_generation_cost_status, self.source_generation_cost_cny),
            (self.current_reference_cost_status, self.current_reference_cost_cny),
            (self.saved_cost_status, self.saved_cost_cny),
            (self.billed_cost_status, self.billed_cost_cny),
        )
        if self.hit != (self.status == "hit"):
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.unavailable")
        owner_values = (self.charge_cache_hits, self.cache_policy_id, self.cache_policy_version)
        if any(item is None for item in owner_values) and any(
            item is not None for item in owner_values
        ):
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.unavailable")
        if self.hit:
            if any(item is None for item in owner_values):
                # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
                raise ValueError("response_cache.unavailable")
            for evidence_status, amount in amount_pairs:
                if evidence_status not in {"known", "unavailable"}:
                    # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
                    raise ValueError("response_cache.unavailable")
                self._validate_amount_pair(evidence_status, amount)
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            if self.current_reference_cost_status == "known" and self.billed_cost_status == "known":
                if self.saved_cost_status != "known" or (
                    self._amount(self.current_reference_cost_cny)
                    - self._amount(self.billed_cost_cny)
                    != self._amount(self.saved_cost_cny)
                ):
                    # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
                    raise ValueError("response_cache.unavailable")
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            elif self.saved_cost_status != "unavailable" or self.saved_cost_cny is not None:
                # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
                raise ValueError("response_cache.unavailable")
        elif any(
            status != "not_applicable" or amount is not None for status, amount in amount_pairs
        ):
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.unavailable")
        return self

    @staticmethod
    def _validate_amount_pair(status: CostEvidenceStatus, amount: str | None) -> None:
        """known 金额必须是非负六位小数；其他状态不得伪造零值。"""

        if status == "known":
            ResponseCacheRequestLogInfo._amount(amount)
        elif amount is not None:
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.unavailable")

    @staticmethod
    def _amount(value: str | None) -> Decimal:
        """只解析 exact CNY 字符串，避免 float 舍入改变 saved 等式。"""

        if value is None or _CNY_AMOUNT.fullmatch(value) is None:
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.unavailable")
        try:
            return Decimal(value)
        except InvalidOperation:
            # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
            raise ValueError("response_cache.unavailable") from None


__all__ = [
    "CacheInvalidationResponse",
    "CacheInvalidationSummary",
    "CachePolicyListItem",
    "CachePolicyListResponse",
    "CachePolicyPatchResponse",
    "CachePolicySummary",
    "InvalidateResponseCacheRequest",
    "PatchCachePolicyRequest",
    "ResponseCacheRequestLogInfo",
]
