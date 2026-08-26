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
    "REQUIRED_ENTRY_MODES",
    "SEED_KEY_ID",
    "SEED_MANIFEST",
    "SEED_PUBLISHED_AT",
    "SEED_VERSION",
]
