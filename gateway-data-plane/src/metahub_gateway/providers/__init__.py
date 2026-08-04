"""Provider Adapter 模块入口，统一导出 OpenAI-compatible 适配器、通用 HTTP 适配器和 Key 池选择能力。"""

from .base import (
    NormalizedProviderError,
    NormalizedResponse,
    NormalizedUsage,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamResponse,
)
from .factory import adapter_for_route
from .generic_http import GenericHttpConfigAdapter
from .keypool import SelectedProviderKey, mark_provider_key_error, select_provider_key
from .openai_compatible import OpenAICompatibleAdapter

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
