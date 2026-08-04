"""Provider 上游错误归一化的共享目录、wire 合同与确定性匹配器。

本模块只处理数据库无关的纯数据合同。原始 Provider 正文只会短暂存在于 ``repr=False``
的匹配输入中；对外返回、日志、数据库事实和 Webhook 只能使用固定平台分类与脱敏投影。
租户规则不能覆盖鉴权、超时、传输故障和 5xx guard，避免治理配置改变网关安全语义。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

# wire 版本、不可变目录摘要、资源上限和允许枚举共同构成 Control/Gateway 双端校验边界，
# 任何值变化都需要配套迁移与滚动升级设计，不能由单一服务独立调整。
PROVIDER_ERROR_MAP_WIRE_SCHEMA_VERSION = 1
BUILTIN_CATALOG_VERSION = "provider-error-builtin-v1"
BUILTIN_CATALOG_HASH = "a209e9d5e86e5b08d6fe97a1d0475ad8533704f6f782fa20dd732fc4dafdc507"
EMPTY_OVERLAY_CONTENT_HASH = "994a863c694e05021d65d0f8b862f24a28da461080286b7b35d1695ee0775fdc"

MAX_ERROR_BODY_BYTES = 65_536
MAX_ERROR_JSON_DEPTH = 16
MAX_ERROR_JSON_NODES = 256
MAX_ERROR_CANDIDATE_FIELDS = 8
MAX_ERROR_CANDIDATE_CHARS = 512
MAX_RULE_PATTERN_CHARS = 256
MAX_RULE_HTTP_STATUSES = 16

TRANSPORT_KINDS = frozenset(
    {"none", "local_configuration", "network", "timeout", "other"}
)
BODY_LENGTH_BUCKETS = frozenset(
    {
        "unknown",
        "0",
        "1_255",
        "256_1023",
        "1024_4095",
        "4096_16383",
        "16384_65535",
        "over_65535",
    }
)
MATCH_BASES = frozenset(
    {
        "transport_timeout",
        "transport_error",
        "http_status",
        "provider_error_code",
        "provider_error_type",
        "message",
        "compound",
        "default",
    }
)
PLATFORM_ERROR_CLASSES = frozenset(
    {
        "auth_error",
        "rate_limit_error",
        "quota_error",
        "model_not_found_error",
        "resource_not_found_error",
        "timeout_error",
        "invalid_request_error",
        "server_error",
        "[REDACTED]",
    }
)

PROVIDER_ERROR_OUTPUT_CODES = frozenset(
    {
        "gateway.upstream_auth_failed",
        "gateway.upstream_rate_limited",
        "gateway.upstream_quota_exceeded",
        "gateway.upstream_model_not_found",
        "gateway.upstream_resource_not_found",
        "gateway.upstream_timeout",
        "gateway.upstream_request_invalid",
        "gateway.upstream_unavailable",
        "gateway.upstream_error",
    }
)
TENANT_PROVIDER_ERROR_OUTPUT_CODES = frozenset(
    {
        "gateway.upstream_rate_limited",
        "gateway.upstream_quota_exceeded",
        "gateway.upstream_model_not_found",
        "gateway.upstream_resource_not_found",
        "gateway.upstream_request_invalid",
    }
)

_RULE_ID_RE = re.compile(r"^pem_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CATALOG_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SENSITIVE_PATTERN_RE = re.compile(
    r"(?i)(authorization|bearer\s|api[_-]?key|sk-[a-z0-9]|https?://[^\s/@:]+:[^\s/@]+@)"
)
_HIGH_ENTROPY_RE = re.compile(r"(?i)(?:[0-9a-f]{24,}|[a-z0-9+/]{24,}={0,2})")

_QUOTA_FIELD_SIGNALS = (
    "balance_not_enough",
    "billing_hard_limit_reached",
    "credit_exhausted",
    "insufficient_balance",
    "insufficient_quota",
    "quota_exceeded",
)
_QUOTA_MESSAGE_SIGNALS = (
    "balance not enough",
    "credit exhausted",
    "insufficient balance",
    "insufficient quota",
    "余额不足",
    "余额已用尽",
    "额度不足",
    "配额不足",
)
_MODEL_FIELD_SIGNALS = (
    "invalid_model",
    "model_not_exist",
    "model_not_found",
    "unknown_model",
)
_MODEL_MESSAGE_SIGNALS = (
    "model does not exist",
    "model not found",
    "unknown model",
    "未找到模型",
    "模型不存在",
)


class ProviderErrorRuleValidationError(ValueError):
    """携带稳定错误原因的规则或快照校验异常。

    ``code`` 只取平台固定值，异常正文也不包含 pattern、原始错误或上游响应，调用方可以安全地
    把它转换为统一 API 错误，但仍不得直接把 Python 异常栈返回给客户端。
    """

    def __init__(self, code: str, message: str) -> None:
        """保存固定错误码与不含敏感输入的诊断摘要。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderErrorOutputSpec:
    """统一错误码的不可变平台语义，租户规则无权覆盖这些字段。"""

    code: str
    public_http_status: int
    retryable: bool
    user_action: str


@dataclass(frozen=True)
class ProviderErrorRule:
    """已经完成规范化与安全校验的租户覆盖规则。

    pattern 字段允许开发者在控制台只读查看，因此对象 ``repr`` 明确隐藏它们，防止测试失败、
    调试日志或异常上下文把管理员误填的敏感字符串带出可信边界。
    """

    error_map_id: str
    tenant_id: str
    provider_id: str | None
    priority: int
    http_statuses: tuple[int, ...]
    http_status_class: Literal["4xx"] | None
    error_field: Literal["error.code", "error.type", "code", "type"] | None
    field_operator: Literal["equals", "contains"] | None
    field_pattern: str | None = field(repr=False)
    message_operator: Literal["contains"] | None = None
    message_pattern: str | None = field(default=None, repr=False)
    output_error_code: str = "gateway.upstream_error"
    status: Literal["active", "disabled"] = "active"
    version: int = 1
    pattern_hash: str = ""

    @property
    def specificity(self) -> int:
        """返回稳定条件数量，用于同优先级规则的确定性排序。"""

        return sum(
            (
                bool(self.http_statuses or self.http_status_class),
                self.error_field is not None,
                self.message_pattern is not None,
            )
        )


@dataclass(frozen=True, repr=False)
class ProviderErrorMatchInput:
    """一次请求内短暂使用的原始匹配候选，任何字段都不得持久化或进入 repr。"""

    error_code: str | None = None
    error_type: str | None = None
    top_level_code: str | None = None
    top_level_type: str | None = None
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderErrorFacts:
    """从有界正文提取的可持久化脱敏事实和短生命周期匹配输入。"""

    upstream_http_status: int | None
    transport_kind: str
    provider_error_code_class: str | None
    provider_error_type_class: str | None
    provider_error_code_present: bool
    provider_error_type_present: bool
    message_present: bool
    redacted_projection_json: str
    projection_hash: str
    body_length_bucket: str
    body_truncated: bool
    is_json: bool
    match_input: ProviderErrorMatchInput = field(repr=False, compare=False)


@dataclass(frozen=True)
class ProviderErrorMapArtifact:
    """完成 wire 校验后可在多请求间共享的不可变租户覆盖制品。"""

    tenant_id: str
    revision: int
    overlay_content_hash: str
    builtin_catalog_version: str
    rules: tuple[ProviderErrorRule, ...]


@dataclass(frozen=True)
class ProviderErrorMapLease:
    """单个模型请求冻结的规则租约，主调用、Fallback 与流式握手共享同一引用。"""

    artifact: ProviderErrorMapArtifact
    source: Literal["fresh", "lkg", "builtin_fallback"]

    @property
    def tenant_id(self) -> str:
        """返回制品绑定的认证租户，便于 Adapter 做防御性一致性校验。"""

        # 租户只能来自已校验制品，禁止调用方另传字符串覆盖租约的隔离边界。
        return self.artifact.tenant_id


@dataclass(frozen=True)
class ProviderErrorEvidence:
    """Adapter 归一化结果附带的 repr-safe、可落库事实。"""

    upstream_http_status: int | None
    transport_kind: str
    provider_error_code_class: str | None
    provider_error_type_class: str | None
    provider_error_code_present: bool
    provider_error_type_present: bool
    message_present: bool
    redacted_projection_json: str
    projection_hash: str
    body_length_bucket: str
    body_truncated: bool
    is_json: bool
    matched_error_map_id: str
    matched_rule_version: int | None
    matched_pattern_hash: str
    matched_rule_source: Literal["guard", "tenant", "builtin"]
    error_map_revision: int
    overlay_content_hash: str
    builtin_catalog_version: str
    error_map_snapshot_source: Literal["fresh", "lkg", "builtin_fallback"]
    match_basis: str
    normalized_output_error_code: str


@dataclass(frozen=True)
class ProviderErrorMatchResult:
    """确定性 matcher 的完整安全输出，供 Adapter 转换为现有错误 DTO。"""

    code: str
    message: str
    retryable: bool
    error_category: str
    transport_kind: str
    evidence: ProviderErrorEvidence


# 公开错误码在这里冻结 HTTP、重试和治理动作语义；安全消息表只返回固定文案，
# 归一化路径绝不能把 Provider 原始错误文本拼入响应或持久事实。
_OUTPUT_SPECS: dict[str, ProviderErrorOutputSpec] = {
    "gateway.upstream_auth_failed": ProviderErrorOutputSpec(
        "gateway.upstream_auth_failed", 502, False, "check_provider_credentials"
    ),
    "gateway.upstream_rate_limited": ProviderErrorOutputSpec(
        "gateway.upstream_rate_limited", 429, True, "retry_or_switch_provider_key"
    ),
    "gateway.upstream_quota_exceeded": ProviderErrorOutputSpec(
        "gateway.upstream_quota_exceeded", 402, False, "check_provider_billing"
    ),
    "gateway.upstream_model_not_found": ProviderErrorOutputSpec(
        "gateway.upstream_model_not_found", 404, False, "check_provider_model_mapping"
    ),
    "gateway.upstream_resource_not_found": ProviderErrorOutputSpec(
        "gateway.upstream_resource_not_found", 502, False, "check_provider_endpoint"
    ),
    "gateway.upstream_timeout": ProviderErrorOutputSpec(
        "gateway.upstream_timeout", 504, True, "retry_or_check_provider_latency"
    ),
    "gateway.upstream_request_invalid": ProviderErrorOutputSpec(
        "gateway.upstream_request_invalid", 502, False, "check_provider_parameters"
    ),
    "gateway.upstream_unavailable": ProviderErrorOutputSpec(
        "gateway.upstream_unavailable", 503, True, "check_provider_connectivity"
    ),
    "gateway.upstream_error": ProviderErrorOutputSpec(
        "gateway.upstream_error", 502, True, "inspect_request_by_request_id"
    ),
}

_SAFE_MESSAGES = {
    "gateway.upstream_auth_failed": "Upstream provider authentication failed.",
    "gateway.upstream_rate_limited": "Upstream provider is rate limited.",
    "gateway.upstream_quota_exceeded": "Upstream provider quota is exhausted.",
    "gateway.upstream_model_not_found": "Upstream provider model was not found.",
    "gateway.upstream_resource_not_found": "Upstream provider resource was not found.",
    "gateway.upstream_timeout": "Upstream provider request timed out.",
    "gateway.upstream_request_invalid": "Upstream provider rejected the request payload.",
    "gateway.upstream_unavailable": "Upstream provider is unavailable.",
    "gateway.upstream_error": "Upstream provider returned an error.",
}


def _canonical_bytes(value: object) -> bytes:
    """使用跨进程固定 JSON 规则生成 hash 输入，禁止环境相关空白或 Unicode 差异。"""

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    """返回 canonical JSON 的小写 SHA-256，供目录、规则、投影和 ETag 共享同一摘要算法。"""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


# guard、内置规则及信号定义按固定序号组成不可变目录，租户覆盖不能重排或替换安全 guard；
# pattern 摘要还会写入事实与迁移种子，因此元数据调整必须显式升级目录版本。
_GUARD_RULE_METADATA: tuple[dict[str, object], ...] = (
    {
        "error_map_id": "guard_transport_timeout",
        "ordinal": 1,
        "output_error_code": "gateway.upstream_timeout",
        "transport_kinds": ["timeout"],
    },
    {
        "error_map_id": "guard_transport_unavailable",
        "ordinal": 2,
        "output_error_code": "gateway.upstream_unavailable",
        "transport_kinds": ["local_configuration", "network", "other"],
    },
    {
        "error_map_id": "guard_http_auth",
        "ordinal": 3,
        "output_error_code": "gateway.upstream_auth_failed",
        "http_statuses": [401, 403],
    },
    {
        "error_map_id": "guard_http_timeout",
        "ordinal": 4,
        "output_error_code": "gateway.upstream_timeout",
        "http_statuses": [408, 504],
    },
    {
        "error_map_id": "guard_http_5xx",
        "excluded_http_statuses": [504],
        "http_status_class": "5xx",
        "ordinal": 5,
        "output_error_code": "gateway.upstream_error",
    },
)

_BUILTIN_RULE_METADATA: tuple[dict[str, object], ...] = (
    {
        "error_map_id": "builtin_quota_signal",
        "excluded_http_statuses": [401, 403, 404, 408],
        "http_status_class": "4xx",
        "ordinal": 10,
        "output_error_code": "gateway.upstream_quota_exceeded",
        "signal": "quota",
    },
    {
        "error_map_id": "builtin_model_not_found_signal",
        "http_statuses": [400, 404, 422],
        "ordinal": 20,
        "output_error_code": "gateway.upstream_model_not_found",
        "signal": "model_not_found",
    },
    {
        "error_map_id": "builtin_http_402",
        "http_statuses": [402],
        "ordinal": 30,
        "output_error_code": "gateway.upstream_quota_exceeded",
    },
    {
        "error_map_id": "builtin_http_404",
        "http_statuses": [404],
        "ordinal": 40,
        "output_error_code": "gateway.upstream_resource_not_found",
    },
    {
        "error_map_id": "builtin_http_429",
        "http_statuses": [429],
        "ordinal": 50,
        "output_error_code": "gateway.upstream_rate_limited",
    },
    {
        "error_map_id": "builtin_http_400_422",
        "http_statuses": [400, 422],
        "ordinal": 60,
        "output_error_code": "gateway.upstream_request_invalid",
    },
    {
        "error_map_id": "builtin_http_other_4xx",
        "excluded_http_statuses": [401, 402, 403, 404, 408, 422, 429],
        "http_status_class": "4xx",
        "ordinal": 70,
        "output_error_code": "gateway.upstream_request_invalid",
    },
    {
        "error_map_id": "builtin_response_default",
        "ordinal": 80,
        "output_error_code": "gateway.upstream_error",
        "response_default": True,
    },
)

_SIGNAL_DEFINITIONS: dict[str, dict[str, list[str]]] = {
    "quota": {
        "field_equals": list(_QUOTA_FIELD_SIGNALS),
        "message_contains": list(_QUOTA_MESSAGE_SIGNALS),
    },
    "model_not_found": {
        "field_equals": list(_MODEL_FIELD_SIGNALS),
        "message_contains": list(_MODEL_MESSAGE_SIGNALS),
    },
}


def _catalog_rule_with_hash(rule: Mapping[str, object]) -> dict[str, object]:
    """把规则元数据与稳定 pattern hash 合并，signal 定义也纳入防漂移校验。"""

    mutable = dict(rule)
    signal = mutable.get("signal")
    hash_input: object = mutable
    if isinstance(signal, str):
        hash_input = {"rule": mutable, "signal_definition": _SIGNAL_DEFINITIONS[signal]}
    mutable["pattern_hash"] = _sha256(hash_input)
    return mutable


# 导出的规则元组、目录对象及 canonical bytes 是 Gateway 与数据库登记表共同使用的冻结制品，
# 导入期长度和 hash 断言用于在发布前阻断无意合同漂移。
BUILTIN_GUARD_RULES = tuple(_catalog_rule_with_hash(rule) for rule in _GUARD_RULE_METADATA)
BUILTIN_NORMALIZATION_RULES = tuple(
    _catalog_rule_with_hash(rule) for rule in _BUILTIN_RULE_METADATA
)
BUILTIN_CATALOG_OBJECT: dict[str, object] = {
    "catalog_version": BUILTIN_CATALOG_VERSION,
    "guards": list(BUILTIN_GUARD_RULES),
    "rules": list(BUILTIN_NORMALIZATION_RULES),
    "signals": _SIGNAL_DEFINITIONS,
}
BUILTIN_CATALOG_CANONICAL_BYTES = _canonical_bytes(BUILTIN_CATALOG_OBJECT)

# 这两个断言把设计文档、Shared、Gateway 和 0046 seed 绑定到同一字节合同；任何无意字段漂移都
# 必须在进程导入时失败，不能等到滚动升级后才由 Fact 外键暴露不一致。
if len(BUILTIN_CATALOG_CANONICAL_BYTES) != 3_664:
    raise RuntimeError("provider error builtin catalog canonical length drifted")
# 长度相同仍可能发生字段值漂移，因此必须再校验内容摘要，避免错误目录进入滚动升级。
if hashlib.sha256(BUILTIN_CATALOG_CANONICAL_BYTES).hexdigest() != BUILTIN_CATALOG_HASH:
    raise RuntimeError("provider error builtin catalog hash drifted")


def provider_error_output_spec(code: str) -> ProviderErrorOutputSpec:
    """读取平台冻结的 HTTP、重试和用户动作语义，不信任数据库中的派生字段。"""

    try:
        return _OUTPUT_SPECS[code]
    # 未登记 code 不得回退为通用码，否则租户规则可能绕过输出白名单与重试矩阵。
    except KeyError as exc:
        raise ProviderErrorRuleValidationError(
            "error_map.output_code_invalid", "provider error output code is not registered"
        ) from exc


def normalize_rule_pattern(value: object, *, required: bool) -> str | None:
    """对规则 pattern 执行 NFC、长度、控制字符与敏感值门禁。

    参数：原始字段和值是否必填；返回：规范化字符串或 ``None``。该函数不会在异常正文中回显
    输入，因为 pattern 对 developer 可见，必须在写入前阻止 Provider Key 被保存为规则。
    """

    # 必填与可空由调用场景决定，缺失值必须在规范化前处理，避免被转成字符串 None。
    if value is None:
        if required:
            raise ProviderErrorRuleValidationError(
                "error_map.pattern_invalid", "provider error pattern is required"
            )
        return None
    # 只接收真实字符串，禁止任意对象通过 repr 把密钥或内部字段带入规则与异常。
    if not isinstance(value, str):
        raise ProviderErrorRuleValidationError(
            "error_map.pattern_invalid", "provider error pattern must be text"
        )
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or len(normalized) > MAX_RULE_PATTERN_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        # 空值、超长值和控制字符都会破坏确定性匹配或日志边界，因此统一按非法 pattern 拒绝。
        raise ProviderErrorRuleValidationError(
            "error_map.pattern_invalid", "provider error pattern is outside the bounded contract"
        )
    # 常见密钥前缀和高熵候选在写库前失败关闭，避免 Provider Key 被误配置成匹配条件。
    if _SENSITIVE_PATTERN_RE.search(normalized) or _HIGH_ENTROPY_RE.search(normalized):
        raise ProviderErrorRuleValidationError(
            "error_map.pattern_sensitive", "provider error pattern contains a sensitive marker"
        )
    return normalized


def tenant_rule_pattern_hash(rule: Mapping[str, object]) -> str:
    """计算只覆盖匹配条件的稳定 hash，事件与 Audit 不携带 pattern 原文。"""

    condition = {
        "error_field": rule.get("error_field"),
        "field_operator": rule.get("field_operator"),
        "field_pattern": rule.get("field_pattern"),
        "http_status_class": rule.get("http_status_class"),
        "http_statuses": list(rule.get("http_statuses") or ()),
        "message_operator": rule.get("message_operator"),
        "message_pattern": rule.get("message_pattern"),
    }
    return _sha256(condition)


def tenant_rule_condition_hash(rule: Mapping[str, object]) -> str:
    """计算冲突检测 hash，把 Provider scope、优先级和全部条件纳入唯一语义。"""

    return _sha256(
        {
            "pattern_hash": tenant_rule_pattern_hash(rule),
            "priority": rule.get("priority"),
            "provider_id": rule.get("provider_id"),
        }
    )


def _allowed_outputs_for_status(status: int) -> frozenset[str]:
    """按实际上游状态返回 tenant 可选择的输出集合，guard 状态返回空集。"""

    if status in {401, 403, 408, 504} or 500 <= status <= 599:
        return frozenset()
    if status == 402:
        return frozenset({"gateway.upstream_quota_exceeded"})
    if status == 404:
        return frozenset(
            {
                "gateway.upstream_model_not_found",
                "gateway.upstream_resource_not_found",
            }
        )
    if status == 429:
        return frozenset(
            {
                "gateway.upstream_rate_limited",
                "gateway.upstream_quota_exceeded",
            }
        )
    if status in {400, 422}:
        return frozenset(
            {
                "gateway.upstream_request_invalid",
                "gateway.upstream_model_not_found",
                "gateway.upstream_quota_exceeded",
            }
        )
    if 400 <= status <= 499:
        return frozenset(
            {
                "gateway.upstream_request_invalid",
                "gateway.upstream_quota_exceeded",
            }
        )
    return frozenset()


def compile_provider_error_rule(
    raw: Mapping[str, object], *, expected_tenant_id: str | None = None
) -> ProviderErrorRule:
    """把 Control 输入或 Gateway wire 规则编译为不可变 matcher 结构。

    所有枚举、边界、guard 冲突、HTTP/output 矩阵和 pattern hash 都在两端重复校验。Gateway
    不能因为 Control 已验证就信任网络载荷；跨租户 tenant_id 会在创建任何缓存制品前被拒绝。
    """

    required_keys = {
        "error_map_id",
        "tenant_id",
        "provider_id",
        "priority",
        "http_statuses",
        "http_status_class",
        "error_field",
        "field_operator",
        "field_pattern",
        "message_operator",
        "message_pattern",
        "output_error_code",
        "status",
        "version",
        "pattern_hash",
    }
    # exact schema 拒绝未知字段，防止新旧服务对未约定条件产生不同匹配解释。
    if set(raw) != required_keys:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error rule fields do not match the wire schema"
        )
    error_map_id = raw["error_map_id"]
    tenant_id = raw["tenant_id"]
    provider_id = raw["provider_id"]
    # 规则 ID 必须由平台固定前缀和长度生成，避免跨资源类型混淆或路径注入。
    if not isinstance(error_map_id, str) or _RULE_ID_RE.fullmatch(error_map_id) is None:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error map id is invalid"
        )
    # 制品必须显式绑定有界租户，空租户或异常类型不能进入共享缓存。
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 128:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error tenant id is invalid"
        )
    # Gateway 传入的认证租户是最终边界，wire 中的 tenant_id 不可信且不得覆盖它。
    if expected_tenant_id is not None and tenant_id != expected_tenant_id:
        raise ProviderErrorRuleValidationError(
            "error_map.tenant_unavailable", "provider error rule tenant does not match"
        )
    if provider_id is not None and (
        not isinstance(provider_id, str) or not provider_id or len(provider_id) > 128
    ):
        # Provider 范围只允许空值或有界内部 ID，名称和 Endpoint 均不能混入规则合同。
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider scope is invalid"
        )
    priority = raw["priority"]
    version = raw["version"]
    # bool 虽是 int 子类也不能充当优先级，范围与控制面和数据库约束保持一致。
    if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 1000:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error priority is outside the bounded range"
        )
    # 版本号必须是正整数，确保不可变历史和事件中的版本可以单调追踪。
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error rule version is invalid"
        )
    raw_statuses = raw["http_statuses"]
    # HTTP 条件必须是有界序列，防止超大规则放大每次请求的匹配成本。
    if not isinstance(raw_statuses, (list, tuple)) or len(raw_statuses) > MAX_RULE_HTTP_STATUSES:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error HTTP status list is invalid"
        )
    statuses: list[int] = []
    for status in raw_statuses:
        # 每个状态都必须是真实三位 HTTP 整数，拒绝 bool 和越界值造成矩阵歧义。
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ProviderErrorRuleValidationError(
                "error_map.input_invalid", "provider error HTTP status is invalid"
            )
        statuses.append(status)
    statuses_tuple = tuple(sorted(set(statuses)))
    # 重复状态会让 canonical hash 与用户输入不一致，必须拒绝而不是静默去重。
    if len(statuses_tuple) != len(statuses):
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error HTTP statuses must be unique"
        )
    status_class = raw["http_status_class"]
    if (
        status_class is not None
        and (not isinstance(status_class, str) or status_class != "4xx")
    ) or (statuses_tuple and status_class is not None):
        # 精确状态与状态类互斥，且 M1 只开放 4xx 类，避免租户规则覆盖五百段 guard。
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error HTTP class is invalid"
        )
    # 鉴权、超时与五百段属于不可覆盖安全 guard，租户规则在编译阶段即失败关闭。
    if any(status in {401, 403, 408, 504} or status >= 500 for status in statuses_tuple):
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error rule overlaps an immutable guard"
        )
    error_field = raw["error_field"]
    field_operator = raw["field_operator"]
    message_operator = raw["message_operator"]
    if error_field is not None and (
        not isinstance(error_field, str)
        or error_field not in {"error.code", "error.type", "code", "type"}
    ):
        # 只允许已脱敏事实中的固定字段，任意 JSON 路径可能读取敏感 Provider 正文。
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error field is invalid"
        )
    if field_operator is not None and (
        not isinstance(field_operator, str)
        or field_operator not in {"equals", "contains"}
    ):
        # M1 明确禁用 regex，只允许有界 equals/contains，避免 ReDoS 和跨实现差异。
        raise ProviderErrorRuleValidationError(
            "error_map.pattern_invalid", "provider error field operator is invalid"
        )
    # 字段与操作符必须成对出现，禁止半配置规则在不同服务中产生不同默认语义。
    if (error_field is None) != (field_operator is None):
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error field and operator must be paired"
        )
    field_pattern = normalize_rule_pattern(
        raw["field_pattern"], required=error_field is not None
    )
    # 规范化后的 pattern 仍须与字段成对，空值不能偷偷退化为只按状态匹配。
    if (error_field is None) != (field_pattern is None):
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error field and pattern must be paired"
        )
    if message_operator is not None and (
        not isinstance(message_operator, str) or message_operator != "contains"
    ):
        # message 条件仅支持 contains，避免错误正文上执行复杂或不确定的匹配算法。
        raise ProviderErrorRuleValidationError(
            "error_map.pattern_invalid", "provider error message operator is invalid"
        )
    message_pattern = normalize_rule_pattern(
        raw["message_pattern"], required=message_operator is not None
    )
    # message 操作符与 pattern 必须成对，保证 Control 与 Gateway 编译结果完全一致。
    if (message_operator is None) != (message_pattern is None):
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error message operator and pattern must be paired"
        )
    if (
        not statuses_tuple
        and status_class is None
        and error_field is None
        and message_pattern is None
    ):
        # 至少一个有界条件才能创建规则，禁止无条件租户规则吞掉所有 Provider 失败。
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error rule needs at least one condition"
        )
    if (
        message_pattern is not None
        and not statuses_tuple
        and status_class is None
        and error_field is None
    ):
        # 只有精确 Provider 范围才能使用 message-only，降低通用文本误命中的影响面。
        if provider_id is None:
            raise ProviderErrorRuleValidationError(
                "error_map.input_invalid", "message-only rules require an exact provider scope"
            )
    output_code = raw["output_error_code"]
    # 租户规则只能输出批准的公开码，鉴权、超时和通用五百段码由不可覆盖 guard 管理。
    if not isinstance(output_code, str) or output_code not in TENANT_PROVIDER_ERROR_OUTPUT_CODES:
        raise ProviderErrorRuleValidationError(
            "error_map.output_code_invalid", "tenant provider error output code is invalid"
        )
    allowed = TENANT_PROVIDER_ERROR_OUTPUT_CODES
    if statuses_tuple:
        for status in statuses_tuple:
            allowed = allowed.intersection(_allowed_outputs_for_status(status))
    elif status_class == "4xx":
        allowed = frozenset(
            {
                "gateway.upstream_request_invalid",
                "gateway.upstream_quota_exceeded",
            }
        )
    else:
        allowed = frozenset(
            {
                "gateway.upstream_quota_exceeded",
                "gateway.upstream_model_not_found",
                "gateway.upstream_request_invalid",
                "gateway.upstream_resource_not_found",
            }
        )
    # 输出码还必须满足所有 HTTP 条件的交集，不能把 429 伪装成不可重试业务错误。
    if output_code not in allowed:
        raise ProviderErrorRuleValidationError(
            "error_map.output_code_invalid",
            "provider error output is incompatible with HTTP conditions",
        )
    status_value = raw["status"]
    # 状态闭集与数据库一致，未知值不能由 Gateway 猜测为启用或禁用。
    if not isinstance(status_value, str) or status_value not in {"active", "disabled"}:
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error rule status is invalid"
        )
    normalized_for_hash = dict(raw)
    normalized_for_hash["http_statuses"] = statuses_tuple
    normalized_for_hash["field_pattern"] = field_pattern
    normalized_for_hash["message_pattern"] = message_pattern
    expected_hash = tenant_rule_pattern_hash(normalized_for_hash)
    supplied_hash = raw["pattern_hash"]
    if supplied_hash is not None and (
        not isinstance(supplied_hash, str) or supplied_hash not in {"", expected_hash}
    ):
        # Control 提供的摘要必须由 Gateway 重算一致，防止条件正文与审计摘要被替换。
        raise ProviderErrorRuleValidationError(
            "error_map.input_invalid", "provider error rule pattern hash does not match"
        )
    return ProviderErrorRule(
        error_map_id=error_map_id,
        tenant_id=tenant_id,
        provider_id=provider_id,
        priority=priority,
        http_statuses=statuses_tuple,
        http_status_class=status_class,
        error_field=error_field,
        field_operator=field_operator,
        field_pattern=field_pattern,
        message_operator=message_operator,
        message_pattern=message_pattern,
        output_error_code=output_code,
        status=status_value,
        version=version,
        pattern_hash=expected_hash,
    )


def provider_error_rule_to_wire(rule: ProviderErrorRule) -> dict[str, object]:
    """把已编译规则输出为 exact-schema wire 对象，不包含 actor、Endpoint 或凭据。"""

    return {
        "error_map_id": rule.error_map_id,
        "tenant_id": rule.tenant_id,
        "provider_id": rule.provider_id,
        "priority": rule.priority,
        "http_statuses": list(rule.http_statuses),
        "http_status_class": rule.http_status_class,
        "error_field": rule.error_field,
        "field_operator": rule.field_operator,
        "field_pattern": rule.field_pattern,
        "message_operator": rule.message_operator,
        "message_pattern": rule.message_pattern,
        "output_error_code": rule.output_error_code,
        "status": rule.status,
        "version": rule.version,
        "pattern_hash": rule.pattern_hash,
    }


def provider_error_overlay_content_hash(rules: Sequence[ProviderErrorRule]) -> str:
    """按稳定资源 ID 排序计算完整 active overlay hash，空集返回冻结常量。"""

    active = [provider_error_rule_to_wire(rule) for rule in rules if rule.status == "active"]
    active.sort(key=lambda item: str(item["error_map_id"]))
    digest = _sha256({"rules": active, "schema_version": PROVIDER_ERROR_MAP_WIRE_SCHEMA_VERSION})
    # 空覆盖的摘要是 revision-0 Fact 的数据库合同，漂移时必须在导入阶段阻断发布。
    if not active and digest != EMPTY_OVERLAY_CONTENT_HASH:
        raise RuntimeError("provider error empty overlay hash drifted")
    return digest


def provider_error_map_etag(
    tenant_id: str,
    revision: int,
    overlay_content_hash: str,
    builtin_catalog_version: str = BUILTIN_CATALOG_VERSION,
) -> str:
    """生成绑定 schema、租户、revision、overlay 和 builtin 目录的强 ETag，防止跨租户缓存复用。"""

    payload = {
        "builtin_catalog_version": builtin_catalog_version,
        "overlay_content_hash": overlay_content_hash,
        "revision": revision,
        "schema_version": PROVIDER_ERROR_MAP_WIRE_SCHEMA_VERSION,
        "tenant_id": tenant_id,
    }
    return f'"{_sha256(payload)}"'


def serialize_provider_error_map_snapshot(
    artifact: ProviderErrorMapArtifact, *, request_id: str, trace_id: str
) -> dict[str, object]:
    """生成 Control 内部接口的 exact-schema 响应对象，并携带本次链路 request_id 与 trace_id。"""

    return {
        "wire_schema_version": PROVIDER_ERROR_MAP_WIRE_SCHEMA_VERSION,
        "tenant_id": artifact.tenant_id,
        "revision": artifact.revision,
        "overlay_content_hash": artifact.overlay_content_hash,
        "builtin_catalog_version": artifact.builtin_catalog_version,
        "rules": [provider_error_rule_to_wire(rule) for rule in artifact.rules],
        "request_id": request_id,
        "trace_id": trace_id,
    }


def deserialize_provider_error_map_snapshot(
    raw: Mapping[str, object], *, expected_tenant_id: str
) -> ProviderErrorMapArtifact:
    """严格解析 Gateway 收到的内部快照并重新编译全部规则与内容 hash。"""

    expected_keys = {
        "wire_schema_version",
        "tenant_id",
        "revision",
        "overlay_content_hash",
        "builtin_catalog_version",
        "rules",
        "request_id",
        "trace_id",
    }
    # 快照采用 exact schema，未知字段不能绕过 Gateway 的重新编译与摘要校验。
    if set(raw) != expected_keys:
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot fields are invalid"
        )
    wire_schema_version = raw["wire_schema_version"]
    if (
        isinstance(wire_schema_version, bool)
        or not isinstance(wire_schema_version, int)
        or wire_schema_version != PROVIDER_ERROR_MAP_WIRE_SCHEMA_VERSION
    ):
        # wire 版本不兼容时必须保留 LKG 或 builtin，禁止按新旧字段的交集继续解析。
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot schema is unsupported"
        )
    # 认证租户与快照租户必须精确一致，任何跨租户载荷都在创建缓存制品前拒绝。
    if raw["tenant_id"] != expected_tenant_id:
        raise ProviderErrorRuleValidationError(
            "error_map.tenant_unavailable", "provider error snapshot tenant does not match"
        )
    # Gateway 仅接受本进程登记的不可变目录版本，滚动升级不得混用未知目录。
    if raw["builtin_catalog_version"] != BUILTIN_CATALOG_VERSION:
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error builtin catalog does not match"
        )
    revision = raw["revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= 2_147_483_647
    ):
        # revision 必须是含零的有界整数；bool 与越界值会破坏单调缓存和事实外键。
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot revision is invalid"
        )
    overlay_hash = raw["overlay_content_hash"]
    # 覆盖摘要必须是固定长度小写十六进制，不能信任控制面传入的任意字符串。
    if not isinstance(overlay_hash, str) or _SHA256_RE.fullmatch(overlay_hash) is None:
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error overlay hash is invalid"
        )
    raw_rules = raw["rules"]
    # 活跃规则数量受租户上限约束，Gateway 解析时再次限制以防无界内存和编译开销。
    if not isinstance(raw_rules, list) or len(raw_rules) > 128:
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot rule count is invalid"
        )
    rules: list[ProviderErrorRule] = []
    for raw_rule in raw_rules:
        # 每个元素必须是结构化映射，禁止对象 repr 或标量进入规则编译路径。
        if not isinstance(raw_rule, Mapping):
            raise ProviderErrorRuleValidationError(
                "error_map.snapshot_unavailable", "provider error snapshot rule is invalid"
            )
        rule = compile_provider_error_rule(raw_rule, expected_tenant_id=expected_tenant_id)
        # 内部快照只包含 active overlay，禁用规则若混入会造成 hash 与运行匹配不一致。
        if rule.status != "active":
            raise ProviderErrorRuleValidationError(
                "error_map.snapshot_unavailable", "provider error snapshot contains a disabled rule"
            )
        rules.append(rule)
    # 资源 ID 在单个快照内必须唯一，否则排序后同名规则的优先级与审计归属不确定。
    if len({rule.error_map_id for rule in rules}) != len(rules):
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot rule ids are duplicated"
        )
    calculated_hash = provider_error_overlay_content_hash(rules)
    # Gateway 必须重算完整覆盖摘要；revision 0 只代表本次没有使用任何租户覆盖。
    if overlay_hash != calculated_hash or (revision == 0 and rules):
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot content hash does not match"
        )
    # revision 0 使用冻结空摘要，不能携带任意空列表摘要以伪造可追溯事实。
    if revision == 0 and overlay_hash != EMPTY_OVERLAY_CONTENT_HASH:
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable",
            "provider error revision zero must use the empty overlay",
        )
    # 链路标识必须至少保持字符串类型，避免序列化对象把敏感 repr 混入日志与错误响应。
    if not isinstance(raw["request_id"], str) or not isinstance(raw["trace_id"], str):
        raise ProviderErrorRuleValidationError(
            "error_map.snapshot_unavailable", "provider error snapshot trace metadata is invalid"
        )
    return ProviderErrorMapArtifact(
        tenant_id=expected_tenant_id,
        revision=revision,
        overlay_content_hash=overlay_hash,
        builtin_catalog_version=BUILTIN_CATALOG_VERSION,
        rules=tuple(rules),
    )


def builtin_provider_error_map_lease(tenant_id: str) -> ProviderErrorMapLease:
    """为关闭同步、冷启动失败或 LKG 超龄构造 revision-0 builtin 租约。"""

    return ProviderErrorMapLease(
        artifact=ProviderErrorMapArtifact(
            tenant_id=tenant_id,
            revision=0,
            overlay_content_hash=EMPTY_OVERLAY_CONTENT_HASH,
            builtin_catalog_version=BUILTIN_CATALOG_VERSION,
            rules=(),
        ),
        source="builtin_fallback",
    )


def body_length_bucket(length: int | None, *, truncated: bool) -> str:
    """把可证明的正文长度转为固定桶；不确定截断只能返回 ``unknown``。"""

    if length is None:
        # 无法证明长度时必须明确标记截断，不能把未知正文误当成合法空正文参与匹配。
        if not truncated:
            raise ValueError("indeterminate provider body length must be marked truncated")
        return "unknown"
    # 负长度不可能来自合法 wire 事实，必须失败而不能映射到最小长度桶。
    if length < 0:
        raise ValueError("provider body length cannot be negative")
    if length == 0:
        return "0"
    if length <= 255:
        return "1_255"
    if length <= 1023:
        return "256_1023"
    if length <= 4095:
        return "1024_4095"
    if length <= 16_383:
        return "4096_16383"
    if length <= 65_535:
        return "16384_65535"
    return "over_65535"


def _strict_json_object(raw_body: bytes) -> dict[str, object] | None:
    """在深度、节点、重复 key 和常量门禁内解析 JSON object，失败时稳定回落状态码。"""

    def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """把 JSON 键值对还原为对象，并在重复键造成匹配歧义时立即拒绝整个正文。"""

        result: dict[str, object] = {}
        for key, value in pairs:
            # 重复键的取值策略在解析器间不同，拒绝整个正文才能保持归一化结果确定。
            if key in result:
                raise ValueError("duplicate provider error JSON key")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        """拒绝 NaN 与 Infinity 等非标准常量，避免跨运行库产生不同归一化结果。"""

        # 非标准数值不能进入 canonical JSON，统一按正文不可解析处理。
        raise ValueError("non-standard provider error JSON constant")

    try:
        value = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    # 解码、结构和递归异常均只触发状态码回落，绝不回显原始正文或解析异常。
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        # 深度与节点双上限阻断深层或宽幅 JSON 的 CPU/内存放大，并丢弃全部部分候选。
        if nodes > MAX_ERROR_JSON_NODES or depth > MAX_ERROR_JSON_DEPTH:
            return None
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            return None
    return value


def _bounded_candidate(value: object) -> str | None:
    """只把有限标量转换为匹配文本，容器和任意对象不会触发潜在敏感 ``repr``。"""

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)[:MAX_ERROR_CANDIDATE_CHARS]
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)[:MAX_ERROR_CANDIDATE_CHARS]
    return None


def _platform_class(value: str | None) -> str | None:
    """把原始 code/type 映射为平台固定 class；未知值只保留 ``[REDACTED]``。"""

    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", value).casefold()
    classes = (
        ("auth_error", ("authentication_error", "auth_error", "invalid_api_key", "unauthorized")),
        ("rate_limit_error", ("rate_limit_error", "rate_limited", "too_many_requests")),
        ("quota_error", _QUOTA_FIELD_SIGNALS),
        ("model_not_found_error", _MODEL_FIELD_SIGNALS),
        ("resource_not_found_error", ("resource_not_found", "not_found")),
        ("timeout_error", ("timeout", "timeout_error", "request_timeout")),
        ("invalid_request_error", ("bad_request", "invalid_request", "invalid_request_error")),
        ("server_error", ("internal_error", "server_error", "service_unavailable")),
    )
    for class_name, signals in classes:
        if normalized in signals:
            return class_name
    return "[REDACTED]"


def extract_provider_error_facts(
    *,
    upstream_http_status: int | None,
    transport_kind: str,
    raw_body: bytes | None = None,
    body_truncated: bool = False,
    known_body_length: int | None = None,
) -> ProviderErrorFacts:
    """从一次有界上游失败提取匹配输入与可持久化脱敏事实。

    ``raw_body`` 必须已经由 Adapter 的 wire helper 限制到 64 KiB；本函数仍重复校验，任何越界、
    截断或结构异常都丢弃候选并回落到状态码规则，绝不保留部分正文造成错误命中。
    """

    # transport_kind 是持久事实闭集，未知值不能被猜测为网络故障或普通响应。
    if transport_kind not in TRANSPORT_KINDS:
        raise ValueError("provider transport kind is invalid")
    body = raw_body if raw_body is not None else b""
    # Adapter 已有 wire 上限但 Shared 仍防御性复核；越界时丢弃全文，禁止部分 JSON 命中。
    if len(body) > MAX_ERROR_BODY_BYTES:
        body = b""
        body_truncated = True
        known_body_length = MAX_ERROR_BODY_BYTES + 1
    if known_body_length is None and not body_truncated:
        known_body_length = len(body)
    bucket = body_length_bucket(known_body_length, truncated=body_truncated)
    parsed = None if body_truncated else _strict_json_object(body)
    error_object = parsed.get("error") if isinstance(parsed, dict) else None
    nested_error = error_object if isinstance(error_object, dict) else {}
    error_code_present = "code" in nested_error
    error_type_present = "type" in nested_error
    top_code_present = isinstance(parsed, dict) and "code" in parsed
    top_type_present = isinstance(parsed, dict) and "type" in parsed
    error_code = _bounded_candidate(nested_error.get("code")) if error_code_present else None
    error_type = _bounded_candidate(nested_error.get("type")) if error_type_present else None
    top_code = _bounded_candidate(parsed.get("code")) if top_code_present and parsed else None
    top_type = _bounded_candidate(parsed.get("type")) if top_type_present and parsed else None
    message_values: list[str] = []
    message_present = False
    for container, key in (
        (nested_error, "message"),
        (nested_error, "msg"),
        (parsed or {}, "message"),
        (parsed or {}, "msg"),
    ):
        if key in container:
            message_present = True
            candidate = _bounded_candidate(container.get(key))
            if candidate is not None and len(message_values) < 4:
                message_values.append(candidate)
    persisted_code_present = error_code_present or top_code_present
    persisted_type_present = error_type_present or top_type_present
    code_class = _platform_class(error_code if error_code_present else top_code)
    type_class = _platform_class(error_type if error_type_present else top_type)
    if persisted_code_present and code_class is None:
        code_class = "[REDACTED]"
    if persisted_type_present and type_class is None:
        type_class = "[REDACTED]"
    projection = {
        "body_length_bucket": bucket,
        "body_truncated": body_truncated,
        "is_json": parsed is not None,
        "message": "[REDACTED]" if message_present else None,
        "message_present": message_present,
        "provider_error_code_class": code_class,
        "provider_error_code_present": persisted_code_present,
        "provider_error_type_class": type_class,
        "provider_error_type_present": persisted_type_present,
    }
    projection_json = _canonical_bytes(projection).decode("utf-8")
    return ProviderErrorFacts(
        upstream_http_status=upstream_http_status,
        transport_kind=transport_kind,
        provider_error_code_class=code_class,
        provider_error_type_class=type_class,
        provider_error_code_present=persisted_code_present,
        provider_error_type_present=persisted_type_present,
        message_present=message_present,
        redacted_projection_json=projection_json,
        projection_hash=hashlib.sha256(projection_json.encode("utf-8")).hexdigest(),
        body_length_bucket=bucket,
        body_truncated=body_truncated,
        is_json=parsed is not None,
        match_input=ProviderErrorMatchInput(
            error_code=error_code,
            error_type=error_type,
            top_level_code=top_code,
            top_level_type=top_type,
            messages=tuple(message_values),
        ),
    )


def _field_candidate(rule: ProviderErrorRule, match_input: ProviderErrorMatchInput) -> str | None:
    """按白名单字段名读取短生命周期候选，不支持 JSONPath 或任意表达式。"""

    return {
        "error.code": match_input.error_code,
        "error.type": match_input.error_type,
        "code": match_input.top_level_code,
        "type": match_input.top_level_type,
    }.get(rule.error_field or "")


def _tenant_rule_matches(
    rule: ProviderErrorRule, facts: ProviderErrorFacts, provider_id: str
) -> tuple[bool, str]:
    """执行有界 equals/contains AND 匹配并返回安全命中依据。"""

    if rule.status != "active" or (
        rule.provider_id is not None and rule.provider_id != provider_id
    ):
        return False, "default"
    http_status = facts.upstream_http_status
    bases: list[str] = []
    if rule.http_statuses:
        if http_status not in rule.http_statuses:
            return False, "default"
        bases.append("http_status")
    if rule.http_status_class is not None:
        if http_status is None or not 400 <= http_status <= 499:
            return False, "default"
        bases.append("http_status")
    # 字段候选只从已限定的 code/type 标量读取，原始 JSON 路径不会进入租户匹配器。
    if rule.error_field is not None:
        candidate = _field_candidate(rule, facts.match_input)
        if candidate is None or rule.field_pattern is None:
            return False, "default"
        candidate_folded = unicodedata.normalize("NFC", candidate).casefold()
        pattern_folded = rule.field_pattern.casefold()
        matched = (
            candidate_folded == pattern_folded
            if rule.field_operator == "equals"
            else pattern_folded in candidate_folded
        )
        if not matched:
            return False, "default"
        bases.append(
            "provider_error_code" if rule.error_field.endswith("code") else "provider_error_type"
        )
    if rule.message_pattern is not None:
        pattern_folded = rule.message_pattern.casefold()
        if not any(pattern_folded in message.casefold() for message in facts.match_input.messages):
            return False, "default"
        bases.append("message")
    if (
        http_status is None
        or rule.output_error_code not in _allowed_outputs_for_status(http_status)
    ):
        # 即使 Control 已校验规则，仍按本次实际 HTTP 状态二次约束，防止无 HTTP 条件规则越权。
        return False, "default"
    return True, bases[0] if len(bases) == 1 else "compound"


def _contains_signal(facts: ProviderErrorFacts, signal: str) -> tuple[bool, str]:
    """只对冻结 field equals 与 message contains 集合匹配 builtin 信号，并返回安全命中依据。"""

    definition = _SIGNAL_DEFINITIONS[signal]
    field_values = (
        facts.match_input.error_code,
        facts.match_input.error_type,
        facts.match_input.top_level_code,
        facts.match_input.top_level_type,
    )
    allowed_fields = set(definition["field_equals"])
    if any(value is not None and value.casefold() in allowed_fields for value in field_values):
        return True, "provider_error_code"
    for message in facts.match_input.messages:
        normalized = message.casefold()
        if any(pattern.casefold() in normalized for pattern in definition["message_contains"]):
            return True, "message"
    return False, "default"


def _builtin_match(facts: ProviderErrorFacts) -> tuple[dict[str, object], str, str]:
    """按冻结 ordinal 选择 builtin 规则，普通 404 不再冒充模型不存在。"""

    status = facts.upstream_http_status
    if status is not None and 400 <= status <= 499 and status not in {401, 403, 404, 408}:
        matched, basis = _contains_signal(facts, "quota")
        if matched:
            return BUILTIN_NORMALIZATION_RULES[0], basis, "builtin"
    if status in {400, 404, 422}:
        matched, basis = _contains_signal(facts, "model_not_found")
        if matched:
            return BUILTIN_NORMALIZATION_RULES[1], basis, "builtin"
    for rule in BUILTIN_NORMALIZATION_RULES[2:]:
        statuses = rule.get("http_statuses")
        if isinstance(statuses, list) and status in statuses:
            return rule, "http_status", "builtin"
        if rule.get("http_status_class") == "4xx" and status is not None and 400 <= status <= 499:
            excluded = rule.get("excluded_http_statuses")
            if not isinstance(excluded, list) or status not in excluded:
                return rule, "http_status", "builtin"
        if rule.get("response_default") is True:
            return rule, "default", "builtin"
    # 冻结目录必须始终包含兜底规则；缺失说明部署制品已损坏，不能静默返回任意错误码。
    raise RuntimeError("provider error builtin catalog has no default rule")


def _guard_match(facts: ProviderErrorFacts) -> tuple[dict[str, object], str, str] | None:
    """在 tenant overlay 前匹配不可覆盖 guard，冻结鉴权、超时、传输故障与五百段语义。"""

    if facts.transport_kind == "timeout":
        return BUILTIN_GUARD_RULES[0], "transport_timeout", "guard"
    if facts.transport_kind in {"local_configuration", "network", "other"}:
        return BUILTIN_GUARD_RULES[1], "transport_error", "guard"
    status = facts.upstream_http_status
    if status in {401, 403}:
        return BUILTIN_GUARD_RULES[2], "http_status", "guard"
    if status in {408, 504}:
        return BUILTIN_GUARD_RULES[3], "http_status", "guard"
    if status is not None and 500 <= status <= 599 and status != 504:
        return BUILTIN_GUARD_RULES[4], "http_status", "guard"
    return None


def normalize_provider_error(
    facts: ProviderErrorFacts,
    *,
    lease: ProviderErrorMapLease,
    provider_id: str,
) -> ProviderErrorMatchResult:
    """按 guard -> tenant overlay -> builtin 顺序归一 Provider 失败，并产出可安全持久化的证据。"""

    matched = _guard_match(facts)
    matched_version: int | None = None
    if matched is None:
        sorted_rules = sorted(
            lease.artifact.rules,
            key=lambda rule: (
                0 if rule.provider_id == provider_id else 1,
                -rule.priority,
                -rule.specificity,
                rule.error_map_id,
            ),
        )
        for rule in sorted_rules:
            does_match, basis = _tenant_rule_matches(rule, facts, provider_id)
            if does_match:
                matched = (
                    {
                        "error_map_id": rule.error_map_id,
                        "output_error_code": rule.output_error_code,
                        "pattern_hash": rule.pattern_hash,
                    },
                    basis,
                    "tenant",
                )
                matched_version = rule.version
                break
    if matched is None:
        matched = _builtin_match(facts)
    rule_metadata, match_basis, source = matched
    code = str(rule_metadata["output_error_code"])
    spec = provider_error_output_spec(code)
    http_status = facts.upstream_http_status
    # category 只从已归一化 code 与状态推导，供既有 Fallback 预算使用而不读取原始正文。
    if code == "gateway.upstream_rate_limited" and http_status == 429:
        category = "rate_limited"
    elif code == "gateway.upstream_timeout":
        category = "timeout"
    # 五百段分类仅接受不可覆盖 guard 输出，其他错误统一归入安全的 other 类别。
    elif code == "gateway.upstream_error" and http_status is not None and 500 <= http_status <= 599:
        category = "upstream_5xx"
    else:
        category = "other"
    evidence = ProviderErrorEvidence(
        upstream_http_status=http_status,
        transport_kind=facts.transport_kind,
        provider_error_code_class=facts.provider_error_code_class,
        provider_error_type_class=facts.provider_error_type_class,
        provider_error_code_present=facts.provider_error_code_present,
        provider_error_type_present=facts.provider_error_type_present,
        message_present=facts.message_present,
        redacted_projection_json=facts.redacted_projection_json,
        projection_hash=facts.projection_hash,
        body_length_bucket=facts.body_length_bucket,
        body_truncated=facts.body_truncated,
        is_json=facts.is_json,
        matched_error_map_id=str(rule_metadata["error_map_id"]),
        matched_rule_version=matched_version,
        matched_pattern_hash=str(rule_metadata["pattern_hash"]),
        matched_rule_source=source,  # type: ignore[arg-type]
        error_map_revision=lease.artifact.revision,
        overlay_content_hash=lease.artifact.overlay_content_hash,
        builtin_catalog_version=lease.artifact.builtin_catalog_version,
        error_map_snapshot_source=lease.source,
        match_basis=match_basis,
        normalized_output_error_code=code,
    )
    return ProviderErrorMatchResult(
        code=code,
        message=_SAFE_MESSAGES[code],
        retryable=spec.retryable,
        error_category=category,
        transport_kind=facts.transport_kind,
        evidence=evidence,
    )


def builtin_catalog_registry_rows() -> tuple[dict[str, object], ...]:
    """返回 0046 可直接核对的 13 条不可变目录 seed 元数据。"""

    rows: list[dict[str, object]] = []
    for source, rules in (
        ("guard", BUILTIN_GUARD_RULES),
        ("builtin", BUILTIN_NORMALIZATION_RULES),
    ):
        for rule in rules:
            metadata = {key: value for key, value in rule.items() if key != "pattern_hash"}
            signal = metadata.get("signal")
            persisted_metadata: object = metadata
            if isinstance(signal, str):
                persisted_metadata = {
                    "rule": metadata,
                    "signal_definition": _SIGNAL_DEFINITIONS[signal],
                }
            rows.append(
                {
                    "builtin_catalog_version": BUILTIN_CATALOG_VERSION,
                    "error_map_id": rule["error_map_id"],
                    "source": source,
                    "ordinal": rule["ordinal"],
                    "pattern_hash": rule["pattern_hash"],
                    "output_error_code": rule["output_error_code"],
                    "rule_metadata_json": _canonical_bytes(persisted_metadata).decode("utf-8"),
                }
            )
    return tuple(rows)


__all__ = [
    "BODY_LENGTH_BUCKETS",
    "BUILTIN_CATALOG_CANONICAL_BYTES",
    "BUILTIN_CATALOG_HASH",
    "BUILTIN_CATALOG_OBJECT",
    "BUILTIN_CATALOG_VERSION",
    "BUILTIN_GUARD_RULES",
    "BUILTIN_NORMALIZATION_RULES",
    "EMPTY_OVERLAY_CONTENT_HASH",
    "MATCH_BASES",
    "MAX_ERROR_BODY_BYTES",
    "PLATFORM_ERROR_CLASSES",
    "PROVIDER_ERROR_MAP_WIRE_SCHEMA_VERSION",
    "PROVIDER_ERROR_OUTPUT_CODES",
    "ProviderErrorEvidence",
    "ProviderErrorFacts",
    "ProviderErrorMapArtifact",
    "ProviderErrorMapLease",
    "ProviderErrorMatchInput",
    "ProviderErrorMatchResult",
    "ProviderErrorOutputSpec",
    "ProviderErrorRule",
    "ProviderErrorRuleValidationError",
    "TENANT_PROVIDER_ERROR_OUTPUT_CODES",
    "TRANSPORT_KINDS",
    "body_length_bucket",
    "builtin_catalog_registry_rows",
    "builtin_provider_error_map_lease",
    "compile_provider_error_rule",
    "deserialize_provider_error_map_snapshot",
    "extract_provider_error_facts",
    "normalize_provider_error",
    "normalize_rule_pattern",
    "provider_error_map_etag",
    "provider_error_output_spec",
    "provider_error_overlay_content_hash",
    "provider_error_rule_to_wire",
    "serialize_provider_error_map_snapshot",
    "tenant_rule_condition_hash",
    "tenant_rule_pattern_hash",
]
