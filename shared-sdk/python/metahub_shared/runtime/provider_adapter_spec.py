"""PRD-063 国内模型 Endpoint 的共享 Provider Adapter 规范目录。

本模块只发布可公开、可执行的机器合同，不保存租户 Endpoint、Provider Key、Token、Prompt
或 Response。Control、Gateway、健康探测和 Console 必须共享这里的十类冻结目录；未知类型、
不受支持的鉴权组合及目录漂移均在网络 I/O 前失败关闭，不能回退到猜测的 Adapter。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, NoReturn

from .provider_error_map import BUILTIN_CATALOG_VERSION

# 目录版本是数据库激活事实、Gateway 制品和 Console 展示共同使用的发布身份；字段语义或目录
# 内容变化都必须显式升级版本，不能沿用旧版本静默改变 hash。
CATALOG_VERSION: Final = "adapter-spec-v2"

# 四类产品边界明确区分默认国内模型上游、客户 Endpoint 与迁移入口，避免把竞品聚合平台
# 错误归入 MetaHub 默认模型厂商集合并影响推荐、统计和适配行为。
MODEL_VENDOR_CLOUD: Final = "model_vendor_cloud"
LOCAL_COMPATIBLE: Final = "local_compatible"
CUSTOMER_MANAGED: Final = "customer_managed"
MIGRATION_PLATFORM: Final = "migration_platform"
CLASSIFICATIONS: Final = frozenset(
    {MODEL_VENDOR_CLOUD, LOCAL_COMPATIBLE, CUSTOMER_MANAGED, MIGRATION_PLATFORM}
)

# Adapter、协议、模型映射和探测枚举共同约束可执行行为；M1 只支持 OpenAI-compatible 与
# 通用 JSON/SSE 两条受控路径，不在这里暗示厂商原生签名协议已经实现。
OPENAI_ADAPTER: Final = "openai_compatible"
GENERIC_ADAPTER: Final = "generic_http"
ADAPTER_NAMES: Final = frozenset({OPENAI_ADAPTER, GENERIC_ADAPTER})
OPENAI_PROTOCOL: Final = "openai_chat_completions"
GENERIC_PROTOCOL: Final = "generic_json_sse"
PROTOCOL_FAMILIES: Final = frozenset({OPENAI_PROTOCOL, GENERIC_PROTOCOL})
PROVIDER_MODEL_ID: Final = "provider_model_id"
ENDPOINT_OR_MODEL_ID: Final = "endpoint_or_model_id"
MODEL_ID_STRATEGIES: Final = frozenset({PROVIDER_MODEL_ID, ENDPOINT_OR_MODEL_ID})
OPENAI_PROBE_PROFILE: Final = "openai_models"
GENERIC_PROBE_PROFILE: Final = "generic_reachability"
PROBE_PROFILES: Final = frozenset({OPENAI_PROBE_PROFILE, GENERIC_PROBE_PROFILE})

# 子 profile ID 是 API 展示和运行时交叉核对使用的稳定短标识；具体字段族仍保存在冻结子结构
# 中，调用方不得只凭 ID 自行补出另一套行为。
OPENAI_USAGE_PROFILE_ID: Final = "openai_compatible_v1"
GENERIC_USAGE_PROFILE_ID: Final = "generic_usage_v1"
ERROR_PROFILE_ID: Final = "provider_error_map_v1"
OPENAI_STREAM_PROFILE_ID: Final = "openai_sse_final_usage_v1"
GENERIC_STREAM_PROFILE_ID: Final = "generic_json_sse_v1"

# 价格字段仅是 Provider 价格表的白名单，不包含任何实际单价，也不能覆盖租户 PricePlan。
PRICE_FIELDS: Final = (
    "input_price_per_1k",
    "output_price_per_1k",
    "currency",
    "price_schema_version",
)

# active 表示默认可选上游合同；迁移兼容入口只能为 optional，不能进入默认推荐或国内厂家计数。
ACTIVE: Final = "active"
OPTIONAL: Final = "optional"
SPEC_STATUSES: Final = frozenset({ACTIVE, OPTIONAL})

# 六类国内厂家和十类完整目录均使用不可变集合公开，防止调用方原地扩充白名单并绕过 spec。
DOMESTIC_PROVIDER_TYPES: Final = frozenset(
    {"zhipu", "dashscope", "wenxin", "volcengine", "hunyuan", "deepseek"}
)
EXPECTED_PROVIDER_TYPES: Final = frozenset(
    {
        *DOMESTIC_PROVIDER_TYPES,
        "openai_compatible",
        "local_endpoint",
        "custom",
        "migration_compatible",
    }
)

# 正则和字段黑名单只检查发布目录自身，不接收或保存用户输入；所有失败原因均为固定枚举，
# 避免把目录中的异常值、Header 或疑似密钥带入异常和日志。
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_SAFE_SPEC_ID = re.compile(r"adapter-spec-[a-z][a-z0-9_]{1,63}-v[1-9][0-9]{0,3}\Z")
_SAFE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_SAFE_PROFILE_ID = re.compile(r"[a-z][a-z0-9_]{1,79}\Z")
_SAFE_CONTRACT_CASE = re.compile(r"(?:prd063|prd084)_[a-z0-9_]{2,80}\Z")
_SAFE_HEADER = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:https?://|bearer\s+\S+|sk-[a-z0-9]{8,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=])"
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "authorization_value",
        "base_url",
        "credential_value",
        "endpoint_url",
        "password",
        "prompt",
        "response",
        "secret",
        "token",
    }
)


class ProviderAdapterSpecError(ValueError):
    """表示目录、查询或 Provider 鉴权组合不满足固定领域合同。

    ``code`` 和 ``reason`` 都只能由模块内固定分支产生；异常正文使用固定安全文案，不包含
    provider_type、Header、URL、凭据或目录原值，调用方可据此映射统一 Control/Gateway 错误码。
    """

    _MESSAGES: Final = MappingProxyType(
        {
            "adapter.spec_catalog_invalid": "Provider adapter spec catalog is invalid.",
            "adapter.spec_filter_invalid": "Provider adapter spec filter is invalid.",
            "provider.adapter_spec_missing": "Provider adapter spec is unavailable.",
            "provider.auth_not_supported": "Provider authentication is not supported.",
        }
    )

    def __init__(self, code: str, reason: str) -> None:
        """保存固定领域码和安全原因，不回显触发失败的不可信输入。"""

        # 未知内部 code 也收敛为目录损坏，避免调用方通过异常构造扩展对外错误面。
        safe_code = (
            code if code in self._MESSAGES else "adapter.spec_catalog_invalid"
        )
        super().__init__(self._MESSAGES[safe_code])
        self.code = safe_code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProviderUsageProfile:
    """声明允许解析的 usage 根路径与 Token 字段族。

    该结构只给出 PRD-032 已支持的方言白名单，不保存原始响应或 raw usage；字段使用 tuple
    保持不可变，Gateway 仍需通过既有有界解析器和脱敏器生成用量事实。
    """

    profile_id: str
    root_paths: tuple[str, ...]
    input_token_fields: tuple[str, ...]
    output_token_fields: tuple[str, ...]
    cache_token_fields: tuple[str, ...]
    total_token_fields: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        """返回 JSON 安全的新对象，列表修改不会污染冻结 profile。"""

        return {
            "profile_id": self.profile_id,
            "root_paths": list(self.root_paths),
            "input_token_fields": list(self.input_token_fields),
            "output_token_fields": list(self.output_token_fields),
            "cache_token_fields": list(self.cache_token_fields),
            "total_token_fields": list(self.total_token_fields),
        }


@dataclass(frozen=True, slots=True)
class ProviderErrorProfile:
    """引用 PRD-056 固定错误目录和请求期租户覆盖租约。

    profile 不复制规则正文，也不保存上游自由错误文本；两个布尔约束要求数据面保留租户 overlay
    和单请求 lease，从而使主调用、流式与 Fallback 使用同一不可变错误语义。
    """

    profile_id: str
    builtin_catalog_version: str
    tenant_overlay_enabled: bool
    request_lease_required: bool
    safe_classification_required: bool

    def to_wire(self) -> dict[str, object]:
        """返回只含固定引用和布尔约束的公开 wire 对象。"""

        return {
            "profile_id": self.profile_id,
            "builtin_catalog_version": self.builtin_catalog_version,
            "tenant_overlay_enabled": self.tenant_overlay_enabled,
            "request_lease_required": self.request_lease_required,
            "safe_classification_required": self.safe_classification_required,
        }


@dataclass(frozen=True, slots=True)
class ProviderStreamProfile:
    """声明 SSE 方言、终止标记和 final usage 行为。

    ``allow_include_usage`` 只决定 Gateway 是否可以注入 OpenAI 的 stream option；它不会修改
    调用方原 payload。通用 JSON/SSE 不猜测私有终止标记，也不伪造 Provider 自报 usage。
    """

    profile_id: str
    sse_dialect: str
    termination_marker: str | None
    allow_include_usage: bool
    final_usage_expected: bool

    def to_wire(self) -> dict[str, object]:
        """返回完整流式公开合同，显式保留可空终止标记。"""

        return {
            "profile_id": self.profile_id,
            "sse_dialect": self.sse_dialect,
            "termination_marker": self.termination_marker,
            "allow_include_usage": self.allow_include_usage,
            "final_usage_expected": self.final_usage_expected,
        }


@dataclass(frozen=True, slots=True)
class ProviderAdapterSpec:
    """描述一个 Provider 类型可执行且可公开的完整 Adapter 合同。

    所有复合字段均为 frozen dataclass 或 tuple；结构不含 tenant_id、实际 base URL、模型价格
    和凭据。Provider 类型与 auth 仍必须在每个管理、导入、探测和建连入口重新校验。
    """

    spec_id: str
    provider_type: str
    display_name: str
    classification: str
    adapter_name: str
    adapter_version: str
    protocol_family: str
    allowed_auth_types: tuple[str, ...]
    credential_header: str | None
    credential_scheme: str | None
    model_id_strategy: str
    usage_profile: ProviderUsageProfile
    error_profile: ProviderErrorProfile
    stream_profile: ProviderStreamProfile
    price_fields: tuple[str, ...]
    probe_profile: str
    platform_examples: tuple[str, ...]
    status: str
    contract_case_id: str

    @property
    def usage_profile_id(self) -> str:
        """返回 API 表格使用的稳定 usage profile 短标识。"""

        return self.usage_profile.profile_id

    @property
    def error_profile_id(self) -> str:
        """返回 API 表格使用的稳定错误 profile 短标识。"""

        return self.error_profile.profile_id

    @property
    def stream_profile_id(self) -> str:
        """返回 API 表格使用的稳定 stream profile 短标识。"""

        return self.stream_profile.profile_id

    @property
    def stream_include_usage(self) -> bool:
        """返回 Gateway 是否可以按 spec 注入 ``include_usage`` 的兼容只读属性。"""

        return self.stream_profile.allow_include_usage

    def to_wire(self) -> dict[str, object]:
        """返回只含公开合同的深拷贝 JSON 对象，不暴露 Endpoint 或凭据。"""

        return {
            "spec_id": self.spec_id,
            "provider_type": self.provider_type,
            "display_name": self.display_name,
            "classification": self.classification,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "protocol_family": self.protocol_family,
            "allowed_auth_types": list(self.allowed_auth_types),
            "credential_header": self.credential_header,
            "credential_scheme": self.credential_scheme,
            "model_id_strategy": self.model_id_strategy,
            "usage_profile": self.usage_profile.to_wire(),
            "error_profile": self.error_profile.to_wire(),
            "stream_profile": self.stream_profile.to_wire(),
            "price_fields": list(self.price_fields),
            "probe_profile": self.probe_profile,
            "platform_examples": list(self.platform_examples),
            "status": self.status,
            "contract_case_id": self.contract_case_id,
        }


@dataclass(frozen=True, slots=True)
class ProviderAdapterCatalogIdentity:
    """供租户激活事实、迁移种子和 API 对账使用的冻结目录身份。"""

    version: str
    catalog_hash: str
    spec_count: int
    canonical_byte_length: int

    @property
    def catalog_version(self) -> str:
        """返回数据库命名风格使用的版本别名，值与 ``version`` 完全一致。"""

        return self.version


# usage 字段族复用 PRD-032 的四种根路径和常见 OpenAI/Gemini 命名；这里不读取响应正文，
# 只允许既有解析器从这些字段中生成脱敏数字事实。
_USAGE_ROOT_PATHS: Final = ("usage", "usage_metadata", "usageMetadata", "output.usage")
_INPUT_TOKEN_FIELDS: Final = (
    "prompt_tokens",
    "input_tokens",
    "promptTokenCount",
    "prompt_token_count",
)
_OUTPUT_TOKEN_FIELDS: Final = (
    "completion_tokens",
    "output_tokens",
    "candidatesTokenCount",
    "candidates_token_count",
)
_CACHE_TOKEN_FIELDS: Final = (
    "cache_tokens",
    "cached_tokens",
    "cachedContentTokenCount",
    "cached_content_token_count",
)
_TOTAL_TOKEN_FIELDS: Final = ("total_tokens", "totalTokenCount", "total_token_count")

# 两类 usage profile 的字段族相同但协议身份不同，便于后续显式版本升级而不让调用方根据
# Adapter 名称临时推导；错误 profile 只引用 PRD-056 目录而不复制规则。
_OPENAI_USAGE_PROFILE: Final = ProviderUsageProfile(
    OPENAI_USAGE_PROFILE_ID,
    _USAGE_ROOT_PATHS,
    _INPUT_TOKEN_FIELDS,
    _OUTPUT_TOKEN_FIELDS,
    _CACHE_TOKEN_FIELDS,
    _TOTAL_TOKEN_FIELDS,
)
_GENERIC_USAGE_PROFILE: Final = ProviderUsageProfile(
    GENERIC_USAGE_PROFILE_ID,
    _USAGE_ROOT_PATHS,
    _INPUT_TOKEN_FIELDS,
    _OUTPUT_TOKEN_FIELDS,
    _CACHE_TOKEN_FIELDS,
    _TOTAL_TOKEN_FIELDS,
)
_ERROR_PROFILE: Final = ProviderErrorProfile(
    ERROR_PROFILE_ID,
    BUILTIN_CATALOG_VERSION,
    True,
    True,
    True,
)

# OpenAI SSE 允许请求 final usage 并以 ``[DONE]`` 终止；通用 JSON/SSE 没有可信私有标记，
# 因而不注入 stream option，缺失 usage 时仍交给既有估算与账本路径追踪。
_OPENAI_STREAM_PROFILE: Final = ProviderStreamProfile(
    OPENAI_STREAM_PROFILE_ID,
    "openai_sse",
    "[DONE]",
    True,
    True,
)
_GENERIC_STREAM_PROFILE: Final = ProviderStreamProfile(
    GENERIC_STREAM_PROFILE_ID,
    "generic_json_sse",
    None,
    False,
    False,
)


def _spec(
    provider_type: str,
    display_name: str,
    classification: str,
    *,
    adapter_name: str = OPENAI_ADAPTER,
    protocol_family: str = OPENAI_PROTOCOL,
    allowed_auth_types: tuple[str, ...] = ("bearer",),
    model_id_strategy: str = PROVIDER_MODEL_ID,
    status: str = ACTIVE,
    platform_examples: tuple[str, ...] = (),
    contract_case_id: str | None = None,
) -> ProviderAdapterSpec:
    """构造一条发布目录常量，集中绑定 Adapter 与三个子 profile。

    参数：传入类型、固定展示名、分类和少量差异字段；返回完整冻结 spec。
    约束：本辅助函数只用于模块内静态目录，运行时不得用租户输入动态生成可执行 spec。
    """

    openai_profile = adapter_name == OPENAI_ADAPTER
    return ProviderAdapterSpec(
        spec_id=f"adapter-spec-{provider_type}-v1",
        provider_type=provider_type,
        display_name=display_name,
        classification=classification,
        adapter_name=adapter_name,
        adapter_version="1.0.0",
        protocol_family=protocol_family,
        allowed_auth_types=allowed_auth_types,
        credential_header="Authorization" if "bearer" in allowed_auth_types else None,
        credential_scheme="Bearer" if "bearer" in allowed_auth_types else None,
        model_id_strategy=model_id_strategy,
        usage_profile=_OPENAI_USAGE_PROFILE if openai_profile else _GENERIC_USAGE_PROFILE,
        error_profile=_ERROR_PROFILE,
        stream_profile=(
            _OPENAI_STREAM_PROFILE if openai_profile else _GENERIC_STREAM_PROFILE
        ),
        price_fields=PRICE_FIELDS,
        probe_profile=(OPENAI_PROBE_PROFILE if openai_profile else GENERIC_PROBE_PROFILE),
        platform_examples=platform_examples,
        status=status,
        contract_case_id=(
            contract_case_id or f"prd063_{provider_type}_minimum_contract"
        ),
    )


# v2 目录逐项列出六个国内厂家、三种客户 Endpoint 与唯一迁移入口；声明顺序不作为
# canonical 顺序，发布制品会再次按 provider_type 排序以消除人工调整顺序造成的漂移。
_DECLARED_SPECS: Final = (
    _spec("zhipu", "智谱官方或云服务", MODEL_VENDOR_CLOUD),
    _spec("dashscope", "通义千问官方或云服务", MODEL_VENDOR_CLOUD),
    _spec("wenxin", "文心官方或云服务", MODEL_VENDOR_CLOUD),
    _spec(
        "volcengine",
        "火山引擎模型服务",
        MODEL_VENDOR_CLOUD,
        model_id_strategy=ENDPOINT_OR_MODEL_ID,
    ),
    _spec("hunyuan", "腾讯混元官方或云服务", MODEL_VENDOR_CLOUD),
    _spec("deepseek", "DeepSeek 官方或云服务", MODEL_VENDOR_CLOUD),
    _spec(
        "openai_compatible",
        "客户本地 OpenAI-compatible Endpoint",
        LOCAL_COMPATIBLE,
        allowed_auth_types=("none", "bearer"),
    ),
    _spec(
        "local_endpoint",
        "本地模型 Endpoint",
        LOCAL_COMPATIBLE,
        allowed_auth_types=("none", "bearer"),
        contract_case_id="prd084_local_endpoint_minimum_contract",
    ),
    _spec(
        "custom",
        "客户自管 Endpoint",
        CUSTOMER_MANAGED,
        adapter_name=GENERIC_ADAPTER,
        protocol_family=GENERIC_PROTOCOL,
        allowed_auth_types=("none", "bearer"),
    ),
    _spec(
        "migration_compatible",
        "竞品或迁移兼容平台入口",
        MIGRATION_PLATFORM,
        adapter_name=GENERIC_ADAPTER,
        protocol_family=GENERIC_PROTOCOL,
        status=OPTIONAL,
        platform_examples=("OpenRouter", "硅基流动"),
    ),
)
_SPECS: Final = tuple(sorted(_DECLARED_SPECS, key=lambda item: item.provider_type))

# 每类 spec 的执行矩阵按不可变映射冻结，验证时逐项核对而非仅检查枚举成员，避免合法枚举被
# 拼成错误组合后仍进入网络路径。
_EXPECTED_EXECUTION_SHAPES: Final = MappingProxyType(
    {
        "zhipu": (
            MODEL_VENDOR_CLOUD,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("bearer",),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "dashscope": (
            MODEL_VENDOR_CLOUD,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("bearer",),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "wenxin": (
            MODEL_VENDOR_CLOUD,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("bearer",),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "volcengine": (
            MODEL_VENDOR_CLOUD,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("bearer",),
            ENDPOINT_OR_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "hunyuan": (
            MODEL_VENDOR_CLOUD,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("bearer",),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "deepseek": (
            MODEL_VENDOR_CLOUD,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("bearer",),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "openai_compatible": (
            LOCAL_COMPATIBLE,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("none", "bearer"),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "local_endpoint": (
            LOCAL_COMPATIBLE,
            OPENAI_ADAPTER,
            OPENAI_PROTOCOL,
            ("none", "bearer"),
            PROVIDER_MODEL_ID,
            OPENAI_PROBE_PROFILE,
            ACTIVE,
        ),
        "custom": (
            CUSTOMER_MANAGED,
            GENERIC_ADAPTER,
            GENERIC_PROTOCOL,
            ("none", "bearer"),
            PROVIDER_MODEL_ID,
            GENERIC_PROBE_PROFILE,
            ACTIVE,
        ),
        "migration_compatible": (
            MIGRATION_PLATFORM,
            GENERIC_ADAPTER,
            GENERIC_PROTOCOL,
            ("bearer",),
            PROVIDER_MODEL_ID,
            GENERIC_PROBE_PROFILE,
            OPTIONAL,
        ),
    }
)


def _fail(code: str, reason: str) -> NoReturn:
    """抛出不含目录原值的固定领域异常，集中保持失败关闭语义。"""

    # 所有校验出口都经固定异常收敛，禁止把目录原值或疑似秘密拼进异常正文。
    raise ProviderAdapterSpecError(code, reason)


def _contains_control(value: str) -> bool:
    """判断文本是否含 C0/DEL 控制字符，防止 JSON、日志或 DOM 边界注入。"""

    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_text(value: object, *, maximum: int, reason: str) -> str:
    """验证目录文本的类型、NFC、长度和控制字符，不回显失败原值。"""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or unicodedata.normalize("NFC", value) != value
        or _contains_control(value)
    ):
        _fail("adapter.spec_catalog_invalid", reason)
    return value


def _validate_tuple(
    values: object,
    *,
    reason: str,
    maximum_items: int = 16,
) -> tuple[str, ...]:
    """验证非空、有界且无重复的字符串 tuple，拒绝可变 list 混入冻结目录。"""

    if not isinstance(values, tuple) or not values or len(values) > maximum_items:
        _fail("adapter.spec_catalog_invalid", reason)
    result = tuple(
        _validate_text(value, maximum=96, reason=reason) for value in values
    )
    if len(set(result)) != len(result):
        _fail("adapter.spec_catalog_invalid", reason)
    return result


def _validate_usage_profile(profile: object) -> None:
    """核对 usage 子 profile 的冻结类型、标识和 PRD-032 完整字段族。"""

    # 只接受冻结子结构，防止可变映射在请求期间改变用量解析和成本归因口径。
    if not isinstance(profile, ProviderUsageProfile):
        _fail("adapter.spec_catalog_invalid", "usage_profile_invalid")
    if not _SAFE_PROFILE_ID.fullmatch(profile.profile_id):
        _fail("adapter.spec_catalog_invalid", "usage_profile_invalid")
    expected = (
        _USAGE_ROOT_PATHS,
        _INPUT_TOKEN_FIELDS,
        _OUTPUT_TOKEN_FIELDS,
        _CACHE_TOKEN_FIELDS,
        _TOTAL_TOKEN_FIELDS,
    )
    actual = (
        profile.root_paths,
        profile.input_token_fields,
        profile.output_token_fields,
        profile.cache_token_fields,
        profile.total_token_fields,
    )
    for values in actual:
        _validate_tuple(values, reason="usage_profile_invalid")
    # 字段族必须与既有解析器完全一致，不能在单个 Provider 中私增自由正文路径。
    if actual != expected:
        _fail("adapter.spec_catalog_invalid", "usage_profile_invalid")


def _validate_error_profile(profile: object) -> None:
    """核对错误 profile 仅引用固定 builtin 与请求期租户 overlay，不复制错误正文。"""

    # 错误合同必须是冻结引用，不能用自由映射夹带上游正文或绕过租户覆盖租约。
    if not isinstance(profile, ProviderErrorProfile):
        _fail("adapter.spec_catalog_invalid", "error_profile_invalid")
    expected = ProviderErrorProfile(
        ERROR_PROFILE_ID,
        BUILTIN_CATALOG_VERSION,
        True,
        True,
        True,
    )
    # 任一布尔约束关闭都会改变租户覆盖、lease 或脱敏语义，必须作为目录漂移拒绝。
    if profile != expected:
        _fail("adapter.spec_catalog_invalid", "error_profile_invalid")


def _validate_stream_profile(profile: object, *, adapter_name: str) -> None:
    """核对 SSE 方言、终止标记和 include_usage 与 Adapter 类型严格绑定。"""

    # 流式行为只能来自冻结 profile，避免运行时猜测终止标记或擅自注入 usage 选项。
    if not isinstance(profile, ProviderStreamProfile):
        _fail("adapter.spec_catalog_invalid", "stream_profile_invalid")
    expected = (
        _OPENAI_STREAM_PROFILE if adapter_name == OPENAI_ADAPTER else _GENERIC_STREAM_PROFILE
    )
    # 通用 Endpoint 不得因猜测 OpenAI 行为而注入 stream option 或伪造终止标记。
    if profile != expected:
        _fail("adapter.spec_catalog_invalid", "stream_profile_invalid")


def _validate_public_wire(value: object) -> None:
    """递归阻断秘密类字段名、URL 和疑似凭据值进入公开 spec DTO。"""

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                _fail("adapter.spec_catalog_invalid", "sensitive_field_forbidden")
            _validate_public_wire(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_public_wire(item)
        return
    # Authorization/Bearer 的固定 Header 元数据分别不匹配带值规则；任何 URL 或疑似秘密正文
    # 都在发布期失败，不能等到 API 或日志扫描才发现。
    if isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        _fail("adapter.spec_catalog_invalid", "sensitive_value_forbidden")


def _validate_catalog(specs: object) -> None:
    """验证十类目录的唯一性、分类、组合、子 profile 和隐私边界。

    参数：待验证的冻结 spec tuple；返回：成功时无返回值。
    约束：该函数供模块导入和专项篡改测试共享，任何不完整或未知值均失败关闭。
    """

    # 十类目录必须以不可变 tuple 一次性交付，数量异常时不能构造部分可用索引。
    if not isinstance(specs, tuple) or len(specs) != len(EXPECTED_PROVIDER_TYPES):
        _fail("adapter.spec_catalog_invalid", "spec_count_invalid")
    provider_types_seen: set[str] = set()
    spec_ids: set[str] = set()
    contract_ids: set[str] = set()
    for spec in specs:
        # 每一项必须是冻结领域结构，禁止 dict 等可变输入绕过字段与秘密边界校验。
        if not isinstance(spec, ProviderAdapterSpec):
            _fail("adapter.spec_catalog_invalid", "spec_type_invalid")
        provider_type = _validate_text(
            spec.provider_type,
            maximum=64,
            reason="provider_type_invalid",
        )
        # 类型代码需要符合固定小写格式，防止路径字符或大小写别名制造重复身份。
        if not _SAFE_CODE.fullmatch(provider_type):
            _fail("adapter.spec_catalog_invalid", "provider_type_invalid")
        # Provider 类型是运行时 Adapter 选择键，重复时不能任意保留其中一条合同。
        if provider_type in provider_types_seen:
            _fail("adapter.spec_catalog_invalid", "provider_type_duplicate")
        if spec.spec_id in spec_ids:
            _fail("adapter.spec_catalog_invalid", "spec_id_duplicate")
        if spec.contract_case_id in contract_ids:
            _fail("adapter.spec_catalog_invalid", "contract_case_duplicate")
        provider_types_seen.add(provider_type)
        spec_ids.add(spec.spec_id)
        contract_ids.add(spec.contract_case_id)

        _validate_text(spec.display_name, maximum=120, reason="display_name_invalid")
        if not isinstance(spec.spec_id, str) or not _SAFE_SPEC_ID.fullmatch(spec.spec_id):
            _fail("adapter.spec_catalog_invalid", "spec_id_invalid")
        if not isinstance(spec.adapter_version, str) or not _SAFE_VERSION.fullmatch(
            spec.adapter_version
        ):
            _fail("adapter.spec_catalog_invalid", "adapter_version_invalid")
        if spec.classification not in CLASSIFICATIONS:
            _fail("adapter.spec_catalog_invalid", "classification_unknown")
        if spec.adapter_name not in ADAPTER_NAMES:
            _fail("adapter.spec_catalog_invalid", "adapter_name_unknown")
        if spec.protocol_family not in PROTOCOL_FAMILIES:
            _fail("adapter.spec_catalog_invalid", "protocol_family_unknown")
        # 模型映射策略直接决定上游 model 字段，未知值不得进入网络请求构造路径。
        if spec.model_id_strategy not in MODEL_ID_STRATEGIES:
            _fail("adapter.spec_catalog_invalid", "model_id_strategy_unknown")
        if spec.probe_profile not in PROBE_PROFILES:
            _fail("adapter.spec_catalog_invalid", "probe_profile_unknown")
        if spec.status not in SPEC_STATUSES:
            _fail("adapter.spec_catalog_invalid", "status_unknown")

        auth_types = _validate_tuple(
            spec.allowed_auth_types,
            reason="auth_types_invalid",
            maximum_items=2,
        )
        # custom_header 没有受控 Header profile；即使数据库历史枚举存在也不能进入可执行 spec。
        if not set(auth_types) <= {"none", "bearer"} or auth_types not in {
            ("none",),
            ("bearer",),
            ("none", "bearer"),
        }:
            _fail("adapter.spec_catalog_invalid", "auth_types_invalid")
        if "none" in auth_types and spec.classification not in {
            LOCAL_COMPATIBLE,
            CUSTOMER_MANAGED,
        }:
            _fail("adapter.spec_catalog_invalid", "auth_types_invalid")
        # Bearer 合同只允许平台冻结的 Authorization Header，不能接受租户自由 Header 名。
        if "bearer" in auth_types:
            if (
                spec.credential_header != "Authorization"
                or spec.credential_scheme != "Bearer"
            ):
                _fail("adapter.spec_catalog_invalid", "credential_profile_invalid")
        elif spec.credential_header is not None or spec.credential_scheme is not None:
            _fail("adapter.spec_catalog_invalid", "credential_profile_invalid")
        if spec.credential_header is not None and not _SAFE_HEADER.fullmatch(
            spec.credential_header
        ):
            _fail("adapter.spec_catalog_invalid", "credential_profile_invalid")

        _validate_usage_profile(spec.usage_profile)
        _validate_error_profile(spec.error_profile)
        _validate_stream_profile(spec.stream_profile, adapter_name=spec.adapter_name)
        if spec.price_fields != PRICE_FIELDS:
            _fail("adapter.spec_catalog_invalid", "price_fields_invalid")
        if not isinstance(spec.contract_case_id, str) or not _SAFE_CONTRACT_CASE.fullmatch(
            spec.contract_case_id
        ):
            _fail("adapter.spec_catalog_invalid", "contract_case_invalid")
        expected_contract_case = (
            "prd084_local_endpoint_minimum_contract"
            if provider_type == "local_endpoint"
            else f"prd063_{provider_type}_minimum_contract"
        )
        if spec.contract_case_id != expected_contract_case:
            _fail("adapter.spec_catalog_invalid", "contract_case_invalid")
        _validate_public_wire(spec.to_wire())

        expected_shape = _EXPECTED_EXECUTION_SHAPES.get(provider_type)
        actual_shape = (
            spec.classification,
            spec.adapter_name,
            spec.protocol_family,
            spec.allowed_auth_types,
            spec.model_id_strategy,
            spec.probe_profile,
            spec.status,
        )
        # 精确矩阵同时保护国内厂家分类、migration optional 和 Adapter/profile 组合。
        if expected_shape is None or actual_shape != expected_shape:
            _fail("adapter.spec_catalog_invalid", "execution_shape_invalid")
        # 竞品名称只能作为迁移文档示例出现，不能扩散到厂家或客户 Endpoint 分类。
        if provider_type == "migration_compatible":
            if spec.platform_examples != ("OpenRouter", "硅基流动"):
                _fail("adapter.spec_catalog_invalid", "migration_examples_invalid")
        elif spec.platform_examples:
            _fail("adapter.spec_catalog_invalid", "migration_examples_invalid")

    # 缺失厂家或混入 OpenRouter/硅基流动独立类型都会使集合与十类冻结边界不一致。
    if provider_types_seen != EXPECTED_PROVIDER_TYPES:
        _fail("adapter.spec_catalog_invalid", "provider_types_incomplete")
    domestic = {
        spec.provider_type for spec in specs if spec.classification == MODEL_VENDOR_CLOUD
    }
    # 国内厂家集合必须恰好六类，缺失或混入迁移平台都会破坏默认上游产品边界。
    if domestic != DOMESTIC_PROVIDER_TYPES:
        _fail("adapter.spec_catalog_invalid", "domestic_provider_types_invalid")


def _canonical_catalog_bytes(specs: tuple[ProviderAdapterSpec, ...]) -> bytes:
    """按 provider_type 排序并生成无空白、保留 UTF-8 中文的 canonical JSON。"""

    _validate_catalog(specs)
    payload = {
        "catalog_version": CATALOG_VERSION,
        "specs": [spec.to_wire() for spec in sorted(specs, key=lambda item: item.provider_type)],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _assert_catalog_identity(
    canonical_bytes: bytes,
    *,
    expected_length: int,
    expected_hash: str,
) -> None:
    """核对冻结 byte length 与 SHA-256，阻断同长内容漂移和无意字段变更。"""

    if len(canonical_bytes) != expected_length:
        _fail("adapter.spec_catalog_invalid", "catalog_length_drift")
    actual_hash = hashlib.sha256(canonical_bytes).hexdigest()
    if actual_hash != expected_hash:
        _fail("adapter.spec_catalog_invalid", "catalog_hash_drift")


# canonical bytes 是迁移种子、Control 激活和 Gateway 发布包共同引用的唯一目录正文。下方长度
# 与 hash 在生成初版后改为字面量并由导入期双断言保护，不能动态自证正确。
CATALOG_CANONICAL_BYTES: Final = _canonical_catalog_bytes(_SPECS)
CATALOG_CANONICAL_BYTE_LENGTH: Final = 15_151
CATALOG_CANONICAL_LENGTH: Final = CATALOG_CANONICAL_BYTE_LENGTH
CATALOG_HASH: Final = "c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf"
_assert_catalog_identity(
    CATALOG_CANONICAL_BYTES,
    expected_length=CATALOG_CANONICAL_BYTE_LENGTH,
    expected_hash=CATALOG_HASH,
)

# 类型索引只在通过完整目录与身份校验后创建，避免进程在部分损坏目录上继续提供查找结果。
_SPEC_BY_TYPE: Final = MappingProxyType({spec.provider_type: spec for spec in _SPECS})


def _normalize_lookup_code(value: object) -> str | None:
    """规范化短代码查询；非法输入返回 ``None``，且不进入异常正文。"""

    if not isinstance(value, str) or len(value) > 64 or _contains_control(value):
        return None
    normalized = value.strip().lower()
    return normalized if _SAFE_CODE.fullmatch(normalized) else None


def provider_adapter_specs() -> tuple[ProviderAdapterSpec, ...]:
    """返回按 provider_type 稳定排序的十类冻结目录。

    返回：只读 tuple；各项及其 usage/error/stream 子 profile 都是 frozen dataclass。
    约束：调用方不得从数据库或请求体追加 spec，目录升级只能随发布制品完成。
    """

    return _SPECS


def provider_adapter_spec(provider_type: object) -> ProviderAdapterSpec:
    """按 Provider 类型读取唯一冻结 spec，未知输入以固定领域码失败关闭。

    参数：来自已校验 Provider 元数据的类型；返回：对应不可变 spec。
    约束：即使 Control 已校验，Gateway 仍须调用本函数防御数据库损坏，不能回退通用 Adapter。
    """

    normalized = _normalize_lookup_code(provider_type)
    # 未知、控制字符或 OpenRouter/硅基流动别名都不能降级到 generic 发送网络请求。
    if normalized is None or normalized not in _SPEC_BY_TYPE:
        _fail("provider.adapter_spec_missing", "provider_type_missing")
    return _SPEC_BY_TYPE[normalized]


def validate_provider_auth(provider_type: object, auth_type: object) -> ProviderAdapterSpec:
    """校验 Provider 类型与鉴权组合，并返回已经冻结的执行 spec。

    参数：Provider 类型与持久化 auth_type；返回：组合合法时的 spec。
    约束：``custom_header`` 只保留历史读取兼容，所有联网页面均须在 I/O 前拒绝且不回退 Bearer。
    """

    spec = provider_adapter_spec(provider_type)
    normalized_auth = _normalize_lookup_code(auth_type)
    # auth 不匹配必须返回固定领域错误，异常中不包含原 auth、Header 或 Provider 类型。
    if normalized_auth is None or normalized_auth not in spec.allowed_auth_types:
        _fail("provider.auth_not_supported", "auth_not_supported")
    return spec


def provider_types(*, classification: str | None = None) -> frozenset[str]:
    """返回全部或指定产品分类的 Provider 类型不可变集合。

    参数：可选固定 classification；返回：匹配类型的 frozenset。
    约束：过滤器不做模糊匹配，未知值使用固定 API 可映射错误且不回显原文。
    """

    if classification is None:
        return EXPECTED_PROVIDER_TYPES
    if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
        _fail("adapter.spec_filter_invalid", "classification_invalid")
    return frozenset(
        spec.provider_type for spec in _SPECS if spec.classification == classification
    )


def catalog_identity() -> ProviderAdapterCatalogIdentity:
    """返回版本、固定 SHA-256、数量和 canonical 字节长度组成的目录身份。"""

    return ProviderAdapterCatalogIdentity(
        version=CATALOG_VERSION,
        catalog_hash=CATALOG_HASH,
        spec_count=len(_SPECS),
        canonical_byte_length=CATALOG_CANONICAL_BYTE_LENGTH,
    )


# 显式导出稳定公共合同；带下划线的构造、校验和矩阵仅供模块导入期及专项白盒测试使用。
__all__ = [
    "ACTIVE",
    "ADAPTER_NAMES",
    "CATALOG_CANONICAL_BYTE_LENGTH",
    "CATALOG_CANONICAL_BYTES",
    "CATALOG_CANONICAL_LENGTH",
    "CATALOG_HASH",
    "CATALOG_VERSION",
    "CLASSIFICATIONS",
    "CUSTOMER_MANAGED",
    "DOMESTIC_PROVIDER_TYPES",
    "ENDPOINT_OR_MODEL_ID",
    "ERROR_PROFILE_ID",
    "EXPECTED_PROVIDER_TYPES",
    "GENERIC_ADAPTER",
    "GENERIC_PROBE_PROFILE",
    "GENERIC_STREAM_PROFILE_ID",
    "GENERIC_USAGE_PROFILE_ID",
    "LOCAL_COMPATIBLE",
    "MIGRATION_PLATFORM",
    "MODEL_VENDOR_CLOUD",
    "OPENAI_ADAPTER",
    "OPENAI_PROBE_PROFILE",
    "OPENAI_STREAM_PROFILE_ID",
    "OPENAI_USAGE_PROFILE_ID",
    "OPTIONAL",
    "PRICE_FIELDS",
    "PROVIDER_MODEL_ID",
    "ProviderAdapterCatalogIdentity",
    "ProviderAdapterSpec",
    "ProviderAdapterSpecError",
    "ProviderErrorProfile",
    "ProviderStreamProfile",
    "ProviderUsageProfile",
    "catalog_identity",
    "provider_adapter_spec",
    "provider_adapter_specs",
    "provider_types",
    "validate_provider_auth",
]
