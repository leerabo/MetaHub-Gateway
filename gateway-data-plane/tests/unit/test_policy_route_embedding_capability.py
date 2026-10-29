"""固定策略路由允许对话模型，同时阻止未声明向量能力的上游调用。"""

from __future__ import annotations

import pytest
from metahub_gateway.api.openai_compatible import _validate_policy_route_embedding_capability
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.runtime.errors import ApiError


def _route(capabilities: dict[str, object]) -> GatewayRoute:
    return GatewayRoute(
        tenant_id="tenant_test",
        model_id="model_test",
        model_alias="model-test",
        upstream_model_name="upstream-test",
        capabilities=capabilities,
        provider_id="provider_test",
        provider_code="provider-test",
        provider_type="openai_compatible",
        provider_auth_type="none",
        provider_name="Provider Test",
        provider_base_url="http://127.0.0.1:18080/v1",
    )


def test_policy_route_accepts_chat_only_model_without_embeddings() -> None:
    chat_only = _route({"chat": True, "stream": True})

    _validate_policy_route_embedding_capability("route_to", "chat", chat_only)
    with pytest.raises(ApiError) as error:
        _validate_policy_route_embedding_capability("route_to", "embeddings", chat_only)
    assert error.value.code == "gateway.model_not_found"

    _validate_policy_route_embedding_capability(
        "route_to", "embeddings", _route({"embeddings": True})
    )
    _validate_policy_route_embedding_capability("allow", "embeddings", chat_only)
