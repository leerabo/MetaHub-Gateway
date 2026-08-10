"""验证 Gateway Response Cache 内存 owner 的状态与脱敏边界。"""

from __future__ import annotations

import pytest
from metahub_gateway.response_cache import PreparedResponseCache, ResponseCachePolicySnapshot


def _policy() -> ResponseCachePolicySnapshot:
    """构造当前租户的合成策略，不包含任何真实数据分级或自定义标签。"""

    return ResponseCachePolicySnapshot(
        tenant_id="tenant-prd076-models",
        cache_policy_id="rcp_" + "a" * 32,
        model_alias_id="alias-prd076-models",
        status="enabled",
        data_class="fixture-private-class",
        ttl_seconds=900,
        charge_cache_hits=False,
        tags=("fixture-hidden-tag",),
        version=1,
        invalidation_generation=1,
        single_data_class_confirmed=True,
    )


def test_policy_repr_hides_data_class_and_tags() -> None:
    """可关联分类和标签不能因调试 repr 进入结构化日志。"""

    rendered = repr(_policy())
    assert "fixture-private-class" not in rendered
    assert "fixture-hidden-tag" not in rendered
    assert "tenant-prd076-models" in rendered


def test_prepared_result_rejects_missing_candidate_or_fill_owner() -> None:
    """命中与单写者状态必须携带对应 owner，普通旁路则保持空对象。"""

    assert PreparedResponseCache(status="disabled").status == "disabled"
    with pytest.raises(ValueError, match="response_cache_candidate_state_invalid"):
        PreparedResponseCache(status="hit_candidate", policy=_policy())
    with pytest.raises(ValueError, match="response_cache_fill_state_invalid"):
        PreparedResponseCache(status="miss_owner", policy=_policy())
    with pytest.raises(ValueError, match="response_cache_policy_state_invalid"):
        PreparedResponseCache(status="eligible")
