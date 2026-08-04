"""PRD-068 Control/Gateway 共用 KeyPool 资格算法专项测试。

本文件验证凭据事实输入校验、异常数据失败关闭、环境回退边界和依赖指纹稳定性。测试只构造
无密文事实，Provider Key 不得进入断言、异常消息或测试失败输出。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.runtime.keypool import (
    ManagedCredentialFact,
    evaluate_provider_key_pool,
)

# 所有时序用例共用固定 UTC 观察时点，避免系统时钟抖动掩盖 probe 新鲜度和冷却边界。
AS_OF = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _fact(
    credential_id: str,
    *,
    status: str = "active",
    rate_limited_until: str | None = None,
    probe_health_status: str = "unknown",
    probe_last_tested_at: str | None = None,
) -> ManagedCredentialFact:
    """构造不含密文的固定凭据事实，便于逐项验证资格边界。

    参数分别表示不可逆测试标识、托管状态、冷却截止时点和探测状态/时点；返回仅含资格字段的
    ``ManagedCredentialFact``。这里故意不接受 Provider Key，防止敏感信息进入测试证据。
    """

    return ManagedCredentialFact(
        credential_id=credential_id,
        status=status,
        rate_limited_until=rate_limited_until,
        probe_health_status=probe_health_status,
        probe_last_tested_at=probe_last_tested_at,
    )


def test_none_auth_and_environment_fallback_are_explicit() -> None:
    """无认证始终可用；有认证且无托管 Key 时只认调用方已经证明的环境回退。"""

    none_auth = evaluate_provider_key_pool(
        provider_auth_type="none",
        managed_credentials=(),
        environment_fallback_available=False,
        as_of=AS_OF,
        stale_seconds=60,
    )
    without_fallback = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=(),
        environment_fallback_available=False,
        as_of=AS_OF,
        stale_seconds=60,
    )
    with_fallback = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=(),
        environment_fallback_available=True,
        as_of=AS_OF,
        stale_seconds=60,
    )

    assert none_auth.state == "available"
    assert without_fallback.state == "unavailable"
    assert with_fallback.state == "available"
    assert len(with_fallback.dependency_hash) == 64


def test_managed_key_presence_disables_environment_fallback() -> None:
    """只要存在托管事实，disabled/冷却结果就不能回退进程级 Key。"""

    result = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=(
            _fact("pcred_disabled", status="disabled"),
            _fact(
                "pcred_cooling",
                status="rate_limited",
                rate_limited_until=(AS_OF + timedelta(minutes=1)).isoformat(),
            ),
        ),
        environment_fallback_available=True,
        as_of=AS_OF,
        stale_seconds=60,
    )

    assert result.state == "unavailable"


def test_probe_freshness_and_state_transition_enter_dependency_hash() -> None:
    """新鲜 unhealthy 摘除 Key；相同事实变陈旧后恢复可用并产生不同依赖指纹。"""

    facts = (
        _fact(
            "pcred_probe",
            probe_health_status="unhealthy",
            probe_last_tested_at=(AS_OF - timedelta(seconds=30)).isoformat(),
        ),
    )
    fresh = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=facts,
        environment_fallback_available=False,
        as_of=AS_OF,
        stale_seconds=60,
    )
    stale = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=facts,
        environment_fallback_available=False,
        as_of=AS_OF + timedelta(seconds=31),
        stale_seconds=60,
    )

    assert fresh.state == "unavailable"
    assert stale.state == "available"
    assert fresh.dependency_hash != stale.dependency_hash


def test_fact_order_does_not_change_hash_and_future_probe_fails_closed() -> None:
    """凭据读取顺序不进入 CAS；未来 probe 和重复 ID 都视为损坏事实。"""

    first = _fact("pcred_a")
    second = _fact("pcred_b", status="disabled")
    left = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=(first, second),
        environment_fallback_available=False,
        as_of=AS_OF,
        stale_seconds=60,
    )
    right = evaluate_provider_key_pool(
        provider_auth_type="bearer",
        managed_credentials=(second, first),
        environment_fallback_available=False,
        as_of=AS_OF,
        stale_seconds=60,
    )
    assert left == right

    with pytest.raises(ValueError):
        evaluate_provider_key_pool(
            provider_auth_type="bearer",
            managed_credentials=(
                _fact(
                    "pcred_future",
                    probe_last_tested_at=(AS_OF + timedelta(seconds=1)).isoformat(),
                ),
            ),
            environment_fallback_available=False,
            as_of=AS_OF,
            stale_seconds=60,
        )
    with pytest.raises(ValueError):
        evaluate_provider_key_pool(
            provider_auth_type="bearer",
            managed_credentials=(first, first),
            environment_fallback_available=False,
            as_of=AS_OF,
            stale_seconds=60,
        )
