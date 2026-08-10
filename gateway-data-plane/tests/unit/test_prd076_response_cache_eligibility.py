"""验证 PRD-076 只允许公共非流式 Alias Chat 进入缓存资格检查。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from metahub_gateway.response_cache import (
    classify_response_cache_request,
    trusted_response_cache_tags,
)
from metahub_gateway.routing.models import GatewayRoute


def _route() -> GatewayRoute:
    """构造不含凭据正文的公共 Alias 路由。"""

    return GatewayRoute(
        tenant_id="tenant-prd076-eligibility",
        model_id="model-prd076-eligibility",
        model_alias="prd076-cache-alias",
        upstream_model_name="synthetic-upstream",
        capabilities={"chat": True, "stream": True},
        provider_id="provider-prd076-eligibility",
        provider_code="local_openai",
        provider_type="openai_compatible",
        provider_auth_type="none",
        provider_name="PRD076 synthetic provider",
        provider_base_url="http://127.0.0.1:1/v1",
        requested_model="prd076-cache-alias",
        model_alias_id="alias-prd076-eligibility",
        public_model_alias="prd076-cache-alias",
    )


def _classify(**overrides: object) -> str | None:
    """以合格基线调用纯函数，单个测试只覆盖一个明确偏差。"""

    values: dict[str, object] = {
        "globally_enabled": True,
        "request_kind": "chat",
        "payload": {"model": "prd076-cache-alias", "messages": [], "stream": False},
        "public_virtual_key": True,
        "internal_invocation": False,
        "policy_decision": "allow",
        "experiment_disposition": "not_in_scope",
        "route": _route(),
    }
    values.update(overrides)
    return classify_response_cache_request(**values)  # type: ignore[arg-type]


def test_only_public_non_stream_alias_chat_is_structurally_eligible() -> None:
    """合格路径返回 None，表示下一步仍须读取策略与 key-state。"""

    assert _classify() is None
    assert _classify(globally_enabled=False) == "disabled"
    assert _classify(request_kind="embeddings") == "ineligible"
    assert _classify(payload={"model": "x", "stream": True}) == "ineligible"
    assert _classify(public_virtual_key=False, internal_invocation=True) == "ineligible"
    assert _classify(policy_decision="route_to") == "ineligible"
    assert _classify(route=replace(_route(), model_alias_id=None)) == "ineligible"


def test_active_experiment_including_unsampled_request_bypasses_cache() -> None:
    """活跃 scope 不论是否分配 arm 都不能 lookup、reserve 或 fill。"""

    assert _classify(experiment_disposition="active_scope_bypass") == "bypassed_experiment"
    with pytest.raises(ValueError, match="response_cache_experiment_disposition_unresolved"):
        _classify(experiment_disposition="unresolved")
    with pytest.raises(ValueError, match="response_cache_experiment_disposition_invalid"):
        _classify(experiment_disposition="future-value")


def test_trusted_tags_are_complete_stable_and_do_not_accept_duplicates() -> None:
    """五个强制 owner 与策略标签稳定排序，重复标签固定拒绝。"""

    tags = trusted_response_cache_tags(
        tenant_id="tenant-prd076-tags",
        cache_policy_id="rcp_" + "a" * 32,
        model_alias_id="alias-prd076-tags",
        model_id="model-prd076-tags",
        virtual_key_id="vkey-prd076-tags",
        data_class="fixture-public",
        policy_tags=("zeta", "alpha"),
    )
    assert [(tag.kind, tag.value) for tag in tags] == [
        ("data_class", "fixture-public"),
        ("model", "model-prd076-tags"),
        ("model_alias", "alias-prd076-tags"),
        ("policy", "rcp_" + "a" * 32),
        ("tag", "alpha"),
        ("tag", "zeta"),
        ("virtual_key", "vkey-prd076-tags"),
    ]
    with pytest.raises(ValueError):
        trusted_response_cache_tags(
            tenant_id="tenant-prd076-tags",
            cache_policy_id="rcp_" + "a" * 32,
            model_alias_id="alias-prd076-tags",
            model_id="model-prd076-tags",
            virtual_key_id="vkey-prd076-tags",
            data_class="fixture-public",
            policy_tags=("duplicate", "duplicate"),
        )
