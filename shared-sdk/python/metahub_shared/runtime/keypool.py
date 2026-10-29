"""Provider KeyPool 的无副作用资格快照纯函数。

本模块不连接数据库、不读取环境变量、不解密，也不返回 Credential ID。调用方先在可信租户
事务中投影托管凭据事实和环境回退是否可用，再由这里统一执行状态、冷却和 probe 新鲜度
判断；Control 预览与 Gateway 路由因此不会维护两套略有差异的资格规则。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

KeyPoolState = Literal["available", "unavailable"]

# 状态集合来自 provider_credentials 数据库合同；未知枚举若按可用猜测会绕过凭据治理。
_CREDENTIAL_STATES = frozenset({"active", "disabled", "rate_limited"})
_PROBE_STATES = frozenset({"unknown", "healthy", "unhealthy"})
_MAX_CREDENTIAL_FACTS = 1_000


@dataclass(frozen=True, slots=True)
class ManagedCredentialFact:
    """一条托管凭据的无密文资格事实。

    ``credential_id`` 只参与返回指纹且不会直接暴露；调用方不得把 encrypted_key、前缀、权重
    或明文传入本结构。时间保留数据库文本，由纯函数按严格 UTC 规则统一验证。
    """

    credential_id: str
    status: str
    rate_limited_until: str | None
    probe_health_status: str
    probe_last_tested_at: str | None


@dataclass(frozen=True, slots=True)
class ProviderKeyPoolEligibility:
    """只含安全状态与不可逆依赖指纹的 KeyPool 资格结论。"""

    state: KeyPoolState
    dependency_hash: str


def evaluate_provider_key_pool(
    *,
    provider_auth_type: str,
    managed_credentials: tuple[ManagedCredentialFact, ...],
    environment_fallback_available: bool,
    as_of: datetime,
    stale_seconds: int,
) -> ProviderKeyPoolEligibility:
    """按 Gateway 现有语义计算一个 Provider 的无副作用 KeyPool 快照。

    参数：认证类型和凭据事实来自同租户 Provider 图；环境回退布尔值必须由调用方按默认租户、
    非生产、Provider code/base_url 与启动配置完全一致的规则计算；``as_of`` 是本次计划唯一
    UTC 时点。返回：available/unavailable 和安全依赖 hash。关键约束：存在任一托管 Key 时
    永不回退环境配置；新鲜 unhealthy、disabled 和仍在冷却的 Key 均不可选择。
    """

    # 认证类型决定是否需要凭据；拒绝空值可防止未知认证方式被误当成无认证端点放行。
    if type(provider_auth_type) is not str or not provider_auth_type:
        raise ValueError("provider auth type is invalid")
    # 环境回退必须是调用方完成租户和启动配置比对后的明确布尔值，禁止真值转换扩大可用范围。
    if type(environment_fallback_available) is not bool:
        raise TypeError("environment fallback flag must be bool")
    # 新鲜度窗口必须为正整数；布尔值虽是整数子类，但不能被误解为有效秒数。
    if type(stale_seconds) is not int or isinstance(stale_seconds, bool) or stale_seconds < 1:
        raise ValueError("provider health stale seconds is invalid")
    snapshot_time = _require_utc_time(as_of)
    # 限制单次凭据事实数量，避免异常租户数据造成内存占用或指纹计算资源耗尽。
    if len(managed_credentials) > _MAX_CREDENTIAL_FACTS:
        raise ValueError("provider key pool exceeds safe limit")

    normalized: list[dict[str, object]] = []
    selectable = False
    seen_ids: set[str] = set()
    for fact in managed_credentials:
        # 只接收已经去密且结构固定的事实对象，避免任意映射夹带明文或绕过字段校验。
        if type(fact) is not ManagedCredentialFact:
            raise TypeError("managed credential fact is invalid")
        credential_id = _bounded_identifier(fact.credential_id)
        # 重复身份会让同一凭据产生歧义，因此整批失败关闭而不是静默去重后继续路由。
        if credential_id in seen_ids:
            raise ValueError("managed credential identity is duplicated")
        seen_ids.add(credential_id)
        # 未知状态不得推测为可用，否则新增枚举会在旧版本中绕过停用与健康治理。
        if fact.status not in _CREDENTIAL_STATES or fact.probe_health_status not in _PROBE_STATES:
            raise ValueError("managed credential state is invalid")
        rate_limited_until = _parse_optional_time(fact.rate_limited_until)
        probe_last_tested_at = _parse_optional_time(fact.probe_last_tested_at)
        if probe_last_tested_at is not None and probe_last_tested_at > snapshot_time:
            # 未来 probe 不属于当前快照，按 unknown 放行会制造伪造的新鲜度。
            raise ValueError("managed credential probe time is invalid")
        normalized.append(
            {
                "credential_id": credential_id,
                "status": fact.status,
                "rate_limited_until": _canonical_optional_time(rate_limited_until),
                "probe_health_status": fact.probe_health_status,
                "probe_last_tested_at": _canonical_optional_time(probe_last_tested_at),
            }
        )
        if _credential_is_selectable(
            status=fact.status,
            rate_limited_until=rate_limited_until,
            probe_health_status=fact.probe_health_status,
            probe_last_tested_at=probe_last_tested_at,
            as_of=snapshot_time,
            stale_seconds=stale_seconds,
        ):
            selectable = True

    # 无认证 Endpoint 不需要 Credential；有托管事实时只认托管结果，否则才允许调用方证明的环境回退。
    if provider_auth_type == "none":
        state: KeyPoolState = "available"
    elif managed_credentials:
        state = "available" if selectable else "unavailable"
    else:
        state = "available" if environment_fallback_available else "unavailable"
    payload = {
        "schema_version": "provider-keypool-eligibility-v1",
        "provider_auth_type": provider_auth_type,
        "managed_credentials": sorted(normalized, key=lambda item: str(item["credential_id"])),
        "environment_fallback_available": environment_fallback_available,
        "state": state,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return ProviderKeyPoolEligibility(
        state=state,
        dependency_hash=hashlib.sha256(encoded).hexdigest(),
    )


def _credential_is_selectable(
    *,
    status: str,
    rate_limited_until: datetime | None,
    probe_health_status: str,
    probe_last_tested_at: datetime | None,
    as_of: datetime,
    stale_seconds: int,
) -> bool:
    """复现 Gateway 的单 Key 硬资格，不把陈旧 unhealthy 永久放大为 Provider 故障。"""

    if (
        probe_health_status == "unhealthy"
        and probe_last_tested_at is not None
        and probe_last_tested_at >= as_of - timedelta(seconds=stale_seconds)
    ):
        return False
    if status == "active":
        return True
    if status != "rate_limited":
        return False
    return rate_limited_until is None or rate_limited_until <= as_of


def _bounded_identifier(value: object) -> str:
    """验证 Credential 身份只用于确定性指纹，不允许控制字符或超长值扩大内存。"""

    # 标识只用于去重和指纹；拒绝非字符串、空值与超长值，避免隐式转换和资源消耗。
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise ValueError("managed credential identity is invalid")
    # 控制字符会破坏日志与序列化边界，即使不回传标识也必须在进入指纹前拒绝。
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("managed credential identity is invalid")
    return value


def _require_utc_time(value: object) -> datetime:
    """要求调用方传入有时区 datetime，并规范为 UTC。"""

    # 快照时点必须携带时区；猜测本地时区会让冷却和健康新鲜度在各进程间产生分歧。
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("key pool snapshot time is invalid")
    return value.astimezone(UTC)


def _parse_optional_time(value: object) -> datetime | None:
    """严格解析可空数据库时间，损坏事实必须让整个资格快照失败关闭。"""

    if value is None:
        return None
    # 时间事实只接受非空字符串；类型或空值异常代表投影损坏，必须整批失败关闭。
    if type(value) is not str or not value:
        raise ValueError("managed credential time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非法 ISO 文本不能降级为无时间，否则失效冷却或健康结果可能被错误放行。
    except ValueError:
        raise ValueError("managed credential time is invalid") from None
    # 无时区文本无法与 UTC 快照安全比较，禁止按运行机器的本地时区进行猜测。
    if parsed.tzinfo is None:
        raise ValueError("managed credential time is invalid")
    return parsed.astimezone(UTC)


def _canonical_optional_time(value: datetime | None) -> str | None:
    """为依赖指纹生成固定 UTC 微秒文本；观察时钟本身不进入 payload。"""

    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "ManagedCredentialFact",
    "ProviderKeyPoolEligibility",
    "evaluate_provider_key_pool",
]
