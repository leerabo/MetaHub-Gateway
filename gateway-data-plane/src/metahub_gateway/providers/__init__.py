"""Provider Adapter 模块入口，统一导出 OpenAI-compatible 适配器、通用 HTTP 适配器和 Key 池选择能力。"""

from .base import (
    NormalizedProviderError,
    NormalizedResponse,
    NormalizedUsage,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamResponse,
)
from .keypool import SelectedProviderKey, mark_provider_key_error, select_provider_key


def __getattr__(name: str):
    """Load adapters only when requested, keeping base types independent of routing."""

    from importlib import import_module

    modules = {
        "adapter_for_route": ".factory",
        "GenericHttpConfigAdapter": ".generic_http",
        "OpenAICompatibleAdapter": ".openai_compatible",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "GenericHttpConfigAdapter",
    "NormalizedProviderError",
    "NormalizedResponse",
    "NormalizedUsage",
    "OpenAICompatibleAdapter",
    "ProviderAdapter",
    "ProviderRequest",
    "ProviderStreamResponse",
    "SelectedProviderKey",
    "adapter_for_route",
    "mark_provider_key_error",
    "select_provider_key",
]
