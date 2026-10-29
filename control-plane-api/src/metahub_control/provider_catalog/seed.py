"""Frozen PRD-119 provider catalog seed manifest."""

from __future__ import annotations

from typing import Final


def _capability(
    code: str,
    *,
    context: int,
    stream: bool = False,
    tools: bool = False,
    files: bool = False,
) -> dict[str, object]:
    return {
        "code": code,
        "max_context_tokens": context,
        "supports_files": files,
        "supports_stream": stream,
        "supports_tools": tools,
    }


def _endpoint(host: str, path: str, *, region: str, cloud: str) -> dict[str, object]:
    return {
        "cloud_service_code": cloud,
        "host": host,
        "path_template": path,
        "region_code": region,
        "residency_tags": [region],
        "scheme": "https",
    }


def _entry(
    entry_code: str,
    *,
    vendor_code: str,
    display_name: str,
    provider_type: str,
    auth_modes: list[str],
    regions: list[str],
    compliance_tags: list[str],
    capabilities: list[dict[str, object]],
    endpoints: list[dict[str, object]],
    service_class: str = "official_cloud",
    status: str = "active",
    instantiation_mode: str = "provider",
    compatibility_level: str = "native",
    migration_source_code: str | None = None,
) -> dict[str, object]:
    return {
        "auth_modes": auth_modes,
        "capabilities": capabilities,
        "compatibility": {
            "doc_code": f"provider_catalog.{entry_code}.{compatibility_level}",
            "level": compatibility_level,
            "migration_source_code": migration_source_code,
            "request_difference_codes": [],
        },
        "compliance_tags": compliance_tags,
        "data_regions": regions,
        "display_name": display_name,
        "endpoints": endpoints,
        "entry_code": entry_code,
        "instantiation_mode": instantiation_mode,
        "provider_type": provider_type,
        "service_class": service_class,
        "status": status,
        "vendor_code": vendor_code,
    }


SEED_VERSION: Final = "2026.08.26.1"
SEED_KEY_ID: Final = "metahub-provider-catalog-seed-v1"
SEED_PUBLISHED_AT: Final = "2026-08-26T00:00:00.000Z"

# 外部官方厂商目录使用独立版本发布，避免在没有平台签名发布动作时悄悄改变已经
# 激活的种子目录。平台管理员发布该版本后，Workspace 会通过同一个签名目录入口
# 看到这些厂商；旧安装可以继续使用 2026.08.26.1 并安全回滚。
EXTENDED_SEED_VERSION: Final = "2026.10.27.1"

_CHAT = lambda context: _capability(  # noqa: E731
    "chat", context=context, stream=True, tools=True
)

SEED_MANIFEST: Final[dict[str, object]] = {
    "entries": [
        _entry(
            "dashscope",
            vendor_code="alibaba_cloud",
            display_name="Alibaba Cloud Model Studio",
            provider_type="dashscope",
            auth_modes=["bearer"],
            regions=["cn-beijing"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(131072), _capability("embeddings", context=8192)],
            endpoints=[
                _endpoint(
                    "dashscope.aliyuncs.com",
                    "/compatible-mode/v1",
                    region="cn-beijing",
                    cloud="alibaba_model_studio",
                )
            ],
        ),
        _entry(
            "deepseek",
            vendor_code="deepseek",
            display_name="DeepSeek",
            provider_type="deepseek",
            auth_modes=["bearer"],
            regions=["cn-mainland"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(65536)],
            endpoints=[
                _endpoint(
                    "api.deepseek.com",
                    "/v1",
                    region="cn-mainland",
                    cloud="deepseek_official",
                )
            ],
        ),
        _entry(
            "hunyuan",
            vendor_code="tencent_cloud",
            display_name="Tencent Hunyuan",
            provider_type="hunyuan",
            auth_modes=["bearer"],
            regions=["ap-guangzhou"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(32768)],
            endpoints=[
                _endpoint(
                    "api.hunyuan.cloud.tencent.com",
                    "/v1",
                    region="ap-guangzhou",
                    cloud="tencent_hunyuan",
                )
            ],
        ),
        _entry(
            "local_openai_compatible",
            vendor_code="customer_local",
            display_name="Local OpenAI-compatible endpoint",
            provider_type="local_endpoint",
            auth_modes=["bearer", "none"],
            regions=["customer-network"],
            compliance_tags=["customer_managed", "private_network"],
            capabilities=[_CHAT(32768), _capability("embeddings", context=8192)],
            endpoints=[],
            service_class="local_template",
            instantiation_mode="local_endpoint",
            compatibility_level="openai_compatible",
        ),
        _entry(
            "moonshot",
            vendor_code="moonshot_ai",
            display_name="Moonshot AI",
            provider_type="openai_compatible",
            auth_modes=["bearer"],
            regions=["cn-mainland"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(131072)],
            endpoints=[
                _endpoint(
                    "api.moonshot.cn",
                    "/v1",
                    region="cn-mainland",
                    cloud="moonshot_official",
                )
            ],
        ),
        _entry(
            "openrouter_migration",
            vendor_code="openrouter",
            display_name="OpenRouter migration source",
            provider_type="migration_compatible",
            auth_modes=["bearer"],
            regions=["global"],
            compliance_tags=["migration_source_only"],
            capabilities=[_CHAT(32768)],
            endpoints=[],
            service_class="migration_source",
            status="deprecated",
            instantiation_mode="migration_only",
            compatibility_level="migration_only",
            migration_source_code="openrouter",
        ),
        _entry(
            "siliconflow_migration",
            vendor_code="siliconflow",
            display_name="SiliconFlow migration source",
            provider_type="migration_compatible",
            auth_modes=["bearer"],
            regions=["cn-mainland"],
            compliance_tags=["migration_source_only"],
            capabilities=[_CHAT(32768)],
            endpoints=[],
            service_class="migration_source",
            status="deprecated",
            instantiation_mode="migration_only",
            compatibility_level="migration_only",
            migration_source_code="siliconflow",
        ),
        _entry(
            "volcengine",
            vendor_code="volcengine",
            display_name="Volcengine Ark",
            provider_type="volcengine",
            auth_modes=["bearer"],
            regions=["cn-beijing"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(131072)],
            endpoints=[
                _endpoint(
                    "ark.cn-beijing.volces.com",
                    "/api/v3",
                    region="cn-beijing",
                    cloud="volcengine_ark",
                )
            ],
        ),
        _entry(
            "wenxin",
            vendor_code="baidu_cloud",
            display_name="Baidu Qianfan",
            provider_type="wenxin",
            auth_modes=["bearer"],
            regions=["cn-beijing"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(128000)],
            endpoints=[
                _endpoint(
                    "qianfan.baidubce.com",
                    "/v2",
                    region="cn-beijing",
                    cloud="baidu_qianfan",
                )
            ],
        ),
        _entry(
            "zhipu",
            vendor_code="zhipu_ai",
            display_name="Zhipu AI",
            provider_type="zhipu",
            auth_modes=["bearer"],
            regions=["cn-beijing"],
            compliance_tags=["cn_mainland", "customer_credential"],
            capabilities=[_CHAT(128000)],
            endpoints=[
                _endpoint(
                    "open.bigmodel.cn",
                    "/api/paas/v4",
                    region="cn-beijing",
                    cloud="zhipu_official",
                )
            ],
        ),
    ],
    "schema_version": 1,
    "version": SEED_VERSION,
}

# 国际厂商扩展目录。除了 Anthropic 外均使用厂商公开的 OpenAI-compatible API；
# Anthropic 由 Gateway 的原生 Messages 转换层处理，入口仍复用受控 bearer 凭据。
_FOREIGN_ENTRIES: Final[list[dict[str, object]]] = [
    _entry(
        "anthropic",
        vendor_code="anthropic",
        display_name="Anthropic Claude",
        provider_type="openai_compatible",
        auth_modes=["bearer"],
        regions=["global"],
        compliance_tags=["customer_credential", "global"],
        capabilities=[_CHAT(200000)],
        endpoints=[
            _endpoint(
                "api.anthropic.com", "/v1", region="global", cloud="anthropic_official"
            )
        ],
        compatibility_level="native",
    ),
    _entry(
        "cohere",
        vendor_code="cohere",
        display_name="Cohere",
        provider_type="openai_compatible",
        auth_modes=["bearer"],
        regions=["global"],
        compliance_tags=["customer_credential", "global"],
        capabilities=[_CHAT(128000)],
        endpoints=[
            _endpoint(
                "api.cohere.com",
                "/compatibility/v1",
                region="global",
                cloud="cohere_compatibility",
            )
        ],
        compatibility_level="openai_compatible",
    ),
    _entry(
        "google_gemini",
        vendor_code="google",
        display_name="Google Gemini",
        provider_type="openai_compatible",
        auth_modes=["bearer"],
        regions=["global"],
        compliance_tags=["customer_credential", "global"],
        capabilities=[_CHAT(200000)],
        endpoints=[
            _endpoint(
                "generativelanguage.googleapis.com",
                "/v1beta/openai",
                region="global",
                cloud="google_gemini_openai_compatibility",
            )
        ],
        compatibility_level="openai_compatible",
    ),
    _entry(
        "mistral",
        vendor_code="mistral_ai",
        display_name="Mistral AI",
        provider_type="openai_compatible",
        auth_modes=["bearer"],
        regions=["global"],
        compliance_tags=["customer_credential", "global"],
        capabilities=[_CHAT(128000)],
        endpoints=[_endpoint("api.mistral.ai", "/v1", region="global", cloud="mistral_official")],
        compatibility_level="openai_compatible",
    ),
    _entry(
        "openai",
        vendor_code="openai",
        display_name="OpenAI",
        provider_type="openai_compatible",
        auth_modes=["bearer"],
        regions=["global"],
        compliance_tags=["customer_credential", "global"],
        capabilities=[_CHAT(200000)],
        endpoints=[_endpoint("api.openai.com", "/v1", region="global", cloud="openai_official")],
        compatibility_level="native",
    ),
    _entry(
        "xai_grok",
        vendor_code="xai",
        display_name="xAI Grok",
        provider_type="openai_compatible",
        auth_modes=["bearer"],
        regions=["global"],
        compliance_tags=["customer_credential", "global"],
        capabilities=[_CHAT(131072)],
        endpoints=[_endpoint("api.x.ai", "/v1", region="global", cloud="xai_official")],
        compatibility_level="openai_compatible",
    ),
]

EXTENDED_SEED_MANIFEST: Final[dict[str, object]] = {
    "entries": sorted(
        [*SEED_MANIFEST["entries"], *_FOREIGN_ENTRIES],
        key=lambda item: str(item["entry_code"]),
    ),
    "schema_version": 1,
    "version": EXTENDED_SEED_VERSION,
}

EXTENDED_REQUIRED_ENTRY_MODES: Final[dict[str, str]] = {
    "dashscope": "provider",
    "deepseek": "provider",
    "hunyuan": "provider",
    "local_openai_compatible": "local_endpoint",
    "moonshot": "provider",
    "openrouter_migration": "migration_only",
    "siliconflow_migration": "migration_only",
    "volcengine": "provider",
    "wenxin": "provider",
    "zhipu": "provider",
    **{entry["entry_code"]: "provider" for entry in _FOREIGN_ENTRIES},
}

EXTENDED_OFFICIAL_ENTRY_CODES: Final[frozenset[str]] = frozenset(
    {
        *{
            entry["entry_code"]
            for entry in SEED_MANIFEST["entries"]
            if entry["service_class"] == "official_cloud"
        },
        *(entry["entry_code"] for entry in _FOREIGN_ENTRIES),
    }
)

REQUIRED_ENTRY_MODES: Final[dict[str, str]] = {
    "dashscope": "provider",
    "deepseek": "provider",
    "hunyuan": "provider",
    "local_openai_compatible": "local_endpoint",
    "moonshot": "provider",
    "openrouter_migration": "migration_only",
    "siliconflow_migration": "migration_only",
    "volcengine": "provider",
    "wenxin": "provider",
    "zhipu": "provider",
}


__all__ = [
    "EXTENDED_OFFICIAL_ENTRY_CODES",
    "EXTENDED_REQUIRED_ENTRY_MODES",
    "EXTENDED_SEED_MANIFEST",
    "EXTENDED_SEED_VERSION",
    "REQUIRED_ENTRY_MODES",
    "SEED_KEY_ID",
    "SEED_MANIFEST",
    "SEED_PUBLISHED_AT",
    "SEED_VERSION",
]
