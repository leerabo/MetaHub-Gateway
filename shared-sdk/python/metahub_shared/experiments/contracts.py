"""PRD-075 在线模型实验的纯函数合同。

这里只处理有界标识、整数比例和 HMAC 摘要。Prompt、Response、Provider Key、Token、
Endpoint、Authorization 和 Cookie 不得传入，也不会由任何返回对象携带。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import RFC_4122, UUID

from metahub_shared.feedback import round_ratio_half_up

# 实验采样算法、桶数量与样本上限属于跨 Gateway/Control 的冻结 wire 合同，升级必须显式改版本。
EXPERIMENT_ALLOCATION_ALGORITHM_VERSION = 1
EXPERIMENT_BUCKET_COUNT = 10_000
MAX_EXPERIMENT_SAMPLE_COUNT = 100_000

_SAMPLE_DOMAIN = b"metahub/experiment/sample/v1\x00"
_VARIANT_DOMAIN = b"metahub/experiment/variant/v1\x00"
_KEY_FINGERPRINT_DOMAIN = b"metahub/experiment/key-fingerprint/v1\x00"
_IDEMPOTENCY_KEY_DOMAIN = b"metahub/experiment/idempotency-key/v1\x00"
_CREATE_REQUEST_DOMAIN = b"metahub/experiment/create-request/v1\x00"
_ROUTE_CONFIG_DOMAIN = b"metahub/experiment/route-config/v1\x00"
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]{0,127}$")
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

ExperimentVariant = Literal["a", "b"]
ExperimentAssignmentReason = Literal[
    "hmac_preferred",
    "floor_forced",
    "ceiling_forced",
]


@dataclass(frozen=True, slots=True)
class ExperimentBuckets:
    """保存一次请求的两个域隔离 HMAC 桶，只用于采样和选臂且不得进入日志。"""

    sample_bucket: int
    variant_bucket: int
    algorithm_version: int = EXPERIMENT_ALLOCATION_ALGORITHM_VERSION


@dataclass(frozen=True, slots=True)
class ExperimentAllocationPlan:
    """创建实验时冻结的目标样本和 A/B 配额。"""

    target_sample_count: int
    model_a_allocation_bps: int
    model_a_target_count: int
    model_b_target_count: int


@dataclass(frozen=True, slots=True)
class ExperimentAllocation:
    """Assignment 事务使用的 variant 及插入后的严格计数。"""

    variant: ExperimentVariant
    assignment_reason: ExperimentAssignmentReason
    assigned_count_after: int
    model_a_count_after: int
    model_b_count_after: int


def calculate_variant_targets(
    target_sample_count: int,
    model_a_allocation_bps: int,
) -> ExperimentAllocationPlan:
    """把 bps 转为两个非空整数目标，使用项目统一 half-up 语义。"""

    if (
        type(target_sample_count) is not int
        or not 2 <= target_sample_count <= MAX_EXPERIMENT_SAMPLE_COUNT
        or type(model_a_allocation_bps) is not int
        or not 1 <= model_a_allocation_bps < EXPERIMENT_BUCKET_COUNT
    ):
        # 样本与比例必须能形成两个非空整数臂，拒绝 bool 和浮点等宽松数值。
        raise ValueError("experiment.allocation_invalid")
    model_a_target_count = round_ratio_half_up(
        target_sample_count,
        EXPERIMENT_BUCKET_COUNT,
        model_a_allocation_bps,
    )
    model_b_target_count = target_sample_count - model_a_target_count
    if model_a_target_count < 1 or model_b_target_count < 1:
        # 极端比例在小样本下可能舍入为空臂，此类实验无法产生可信对比结果。
        raise ValueError("experiment.allocation_invalid")
    return ExperimentAllocationPlan(
        target_sample_count=target_sample_count,
        model_a_allocation_bps=model_a_allocation_bps,
        model_a_target_count=model_a_target_count,
        model_b_target_count=model_b_target_count,
    )


def build_experiment_buckets(
    secret: str,
    *,
    tenant_id: str,
    experiment_id: str,
    request_id: str,
) -> ExperimentBuckets:
    """为一次请求生成相互独立的 sample/variant bucket。"""

    canonical = _canonical_identity(
        tenant_id=tenant_id,
        experiment_id=experiment_id,
        request_id=request_id,
    )
    return ExperimentBuckets(
        sample_bucket=_bucket(secret, _SAMPLE_DOMAIN, canonical),
        variant_bucket=_bucket(secret, _VARIANT_DOMAIN, canonical),
    )


def build_experiment_key_fingerprint(
    secret: str,
    *,
    tenant_id: str,
    experiment_id: str,
) -> str:
    """生成 active Experiment 内部使用的不可逆部署 key 指纹。"""

    canonical = _canonical_json_bytes(
        {
            "algorithm_version": EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
            "experiment_id": _safe_identifier(experiment_id),
            "tenant_id": _safe_identifier(tenant_id),
        }
    )
    # 只返回不可逆摘要；部署密钥和 canonical 输入均不得写入日志、API 或验收证据。
    return _domain_hmac(secret, _KEY_FINGERPRINT_DOMAIN, canonical).hex()


def build_experiment_idempotency_key_hash(
    idempotency_key: str,
    *,
    tenant_id: str,
    actor_user_id: str,
) -> str:
    """生成不依赖部署 Auth root 的 actor-scoped 幂等查找摘要。"""

    normalized_key = _canonical_uuid4(idempotency_key)
    canonical = _canonical_json_bytes(
        {
            "actor_user_id": _safe_identifier(actor_user_id),
            "idempotency_key": normalized_key,
            "tenant_id": _safe_identifier(tenant_id),
        }
    )
    return hashlib.sha256(_IDEMPOTENCY_KEY_DOMAIN + canonical).hexdigest()


def build_experiment_create_request_fingerprint(
    idempotency_key: str,
    *,
    name: str,
    project_id: str,
    environment_id: str,
    model_alias_id: str,
    model_a_id: str,
    model_b_id: str,
    target_sample_count: int,
    traffic_sample_bps: int,
    model_a_allocation_bps: int,
    sampling_duration_seconds: int,
    feedback_collection_enabled: bool,
    feedback_window_seconds: int,
) -> str:
    """为 POST /experiments 的 exact body 生成轮换稳定的冲突指纹。"""

    normalized_key = _canonical_uuid4(idempotency_key)
    _validate_create_fingerprint_numbers(
        target_sample_count=target_sample_count,
        traffic_sample_bps=traffic_sample_bps,
        model_a_allocation_bps=model_a_allocation_bps,
        sampling_duration_seconds=sampling_duration_seconds,
        feedback_collection_enabled=feedback_collection_enabled,
        feedback_window_seconds=feedback_window_seconds,
    )
    normalized_model_a = _safe_identifier(model_a_id)
    normalized_model_b = _safe_identifier(model_b_id)
    if normalized_model_a == normalized_model_b:
        # A/B 引用同一模型无法形成对照，必须在生成幂等指纹前按输入无效拒绝。
        raise ValueError("experiment.create_fingerprint_invalid")
    canonical = _canonical_json_bytes(
        {
            "environment_id": _safe_identifier(environment_id),
            "feedback_collection_enabled": feedback_collection_enabled,
            "feedback_window_seconds": feedback_window_seconds,
            "model_a_allocation_bps": model_a_allocation_bps,
            "model_a_id": normalized_model_a,
            "model_alias_id": _safe_identifier(model_alias_id),
            "model_b_id": normalized_model_b,
            "name": _visible_text(name, maximum=120),
            "project_id": _safe_identifier(project_id),
            "sampling_duration_seconds": sampling_duration_seconds,
            "target_sample_count": target_sample_count,
            "traffic_sample_bps": traffic_sample_bps,
        }
    )
    return hmac.new(
        normalized_key.encode("ascii"),
        _CREATE_REQUEST_DOMAIN + canonical,
        hashlib.sha256,
    ).hexdigest()


def build_experiment_route_config_fingerprint(
    secret: str,
    *,
    tenant_id: str,
    model_id: str,
    model_status: str,
    provider_id: str,
    provider_code: str,
    provider_status: str,
    provider_type: str,
    provider_auth_type: str,
    provider_base_url: str,
    upstream_model_name: str,
    capabilities: Mapping[str, object],
    context_window: int,
    adapter_catalog_version: str,
    adapter_catalog_hash: str,
    adapter_spec_id: str,
    adapter_version: str,
) -> str:
    """冻结一个实验 arm 的可执行路由配置，摘要不公开敏感 URL 或上游模型名。"""

    canonical = _canonical_json_bytes(
        {
            "adapter_catalog_hash": _lower_hex_64(adapter_catalog_hash),
            "adapter_catalog_version": _safe_code(adapter_catalog_version),
            "adapter_spec_id": _safe_code(adapter_spec_id),
            "adapter_version": _safe_version(adapter_version),
            "algorithm_version": EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
            "capabilities": _canonical_capabilities(capabilities),
            "context_window": _positive_int(context_window),
            "model_id": _safe_identifier(model_id),
            "model_status": _safe_code(model_status),
            "provider_auth_type": _safe_code(provider_auth_type),
            "provider_base_url": _bounded_text(provider_base_url, maximum=2048),
            "provider_code": _safe_code(provider_code),
            "provider_id": _safe_identifier(provider_id),
            "provider_status": _safe_code(provider_status),
            "provider_type": _safe_code(provider_type),
            "tenant_id": _safe_identifier(tenant_id),
            "upstream_model_name": _bounded_text(upstream_model_name, maximum=512),
        }
    )
    # URL 与上游模型名仅在内存摘要中出现，调用方只能持久化这个不可逆指纹。
    return _domain_hmac(secret, _ROUTE_CONFIG_DOMAIN, canonical).hex()


def choose_experiment_variant(
    plan: ExperimentAllocationPlan,
    *,
    assigned_count: int,
    model_a_count: int,
    model_b_count: int,
    variant_bucket: int,
) -> ExperimentAllocation:
    """按 HMAC 偏好和前缀 floor/ceil 配额选择下一个 variant。

    任意前缀的 A count 都落在最终 A target 按当前前缀缩放的 floor/ceil 内；
    因而时间提前收口时最多偏一个样本，满额时精确命中最终目标。
    """

    _validate_plan(plan)
    if (
        type(assigned_count) is not int
        or type(model_a_count) is not int
        or type(model_b_count) is not int
        or min(assigned_count, model_a_count, model_b_count) < 0
        or assigned_count != model_a_count + model_b_count
        or assigned_count >= plan.target_sample_count
        or model_a_count > plan.model_a_target_count
        or model_b_count > plan.model_b_target_count
        or type(variant_bucket) is not int
        or not 0 <= variant_bucket < EXPERIMENT_BUCKET_COUNT
    ):
        # 已分配计数必须与冻结目标和当前前缀一致，否则不能继续消耗在线流量。
        raise ValueError("experiment.allocation_state_invalid")

    next_count = assigned_count + 1
    numerator = next_count * plan.model_a_target_count
    prefix_a_floor = numerator // plan.target_sample_count
    prefix_a_ceil = (numerator + plan.target_sample_count - 1) // plan.target_sample_count

    if model_a_count < prefix_a_floor or model_b_count >= plan.model_b_target_count:
        variant: ExperimentVariant = "a"
        assignment_reason: ExperimentAssignmentReason = "floor_forced"
    elif model_a_count >= prefix_a_ceil or model_a_count >= plan.model_a_target_count:
        variant = "b"
        assignment_reason = "ceiling_forced"
    else:
        variant = (
            "a"
            if variant_bucket < plan.model_a_allocation_bps
            else "b"
        )
        assignment_reason = "hmac_preferred"

    model_a_count_after = model_a_count + (1 if variant == "a" else 0)
    model_b_count_after = model_b_count + (1 if variant == "b" else 0)
    if not (
        prefix_a_floor <= model_a_count_after <= prefix_a_ceil
        and model_a_count_after <= plan.model_a_target_count
        and model_b_count_after <= plan.model_b_target_count
    ):
        # 选臂后的前缀若越过 floor/ceil 或最终配额，说明输入状态已损坏而非可修正偏差。
        raise ValueError("experiment.allocation_state_invalid")
    return ExperimentAllocation(
        variant=variant,
        assignment_reason=assignment_reason,
        assigned_count_after=next_count,
        model_a_count_after=model_a_count_after,
        model_b_count_after=model_b_count_after,
    )


def _validate_plan(plan: ExperimentAllocationPlan) -> None:
    """复算并拒绝调用方手工构造的非规范配额计划，避免绕过整数比例边界。"""

    if not isinstance(plan, ExperimentAllocationPlan):
        # 只接受冻结 DTO，避免任意对象伪造样本目标或选臂比例。
        raise ValueError("experiment.allocation_state_invalid")
    expected = calculate_variant_targets(
        plan.target_sample_count,
        plan.model_a_allocation_bps,
    )
    if plan != expected:
        # DTO 字段必须与统一 half-up 复算完全一致，不能信任调用方提供的派生计数。
        raise ValueError("experiment.allocation_state_invalid")


def _validate_create_fingerprint_numbers(
    *,
    target_sample_count: int,
    traffic_sample_bps: int,
    model_a_allocation_bps: int,
    sampling_duration_seconds: int,
    feedback_collection_enabled: bool,
    feedback_window_seconds: int,
) -> None:
    """拒绝无法由冻结 POST schema 产生的数值或布尔组合。"""

    try:
        calculate_variant_targets(target_sample_count, model_a_allocation_bps)
    # 创建指纹只暴露统一业务错误，不能把内部配额校验细节带到日志或响应。
    except ValueError as exc:
        raise ValueError("experiment.create_fingerprint_invalid") from exc
    if (
        type(traffic_sample_bps) is not int
        or not 1 <= traffic_sample_bps <= EXPERIMENT_BUCKET_COUNT
        or type(sampling_duration_seconds) is not int
        or not 60 <= sampling_duration_seconds <= 604_800
        or type(feedback_collection_enabled) is not bool
        or type(feedback_window_seconds) is not int
        or (
            feedback_collection_enabled
            and not 60 <= feedback_window_seconds <= 604_800
        )
        or (not feedback_collection_enabled and feedback_window_seconds != 0)
    ):
        # 数值组合必须精确来自 POST schema，防止不同宽松解析得到同一或冲突摘要。
        raise ValueError("experiment.create_fingerprint_invalid")


def _canonical_identity(*, tenant_id: str, experiment_id: str, request_id: str) -> bytes:
    """构造不含请求正文的稳定实验身份字节。"""

    return _canonical_json_bytes(
        {
            "algorithm_version": EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
            "experiment_id": _safe_identifier(experiment_id),
            "request_id": _safe_identifier(request_id),
            "tenant_id": _safe_identifier(tenant_id),
        }
    )


def _canonical_json_bytes(value: dict[str, object]) -> bytes:
    """使用显式排序和紧凑 UTF-8 形成跨进程稳定字节。"""

    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _safe_identifier(value: object) -> str:
    """实验 HMAC 只接受有界、无空白/控制字符的现有本地 ID。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(
            character.isspace() or ord(character) < 33 or ord(character) > 126
            for character in value
        )
    ):
        # HMAC 身份只允许可见 ASCII 本地 ID，空白和控制字符会造成跨实现 canonical 歧义。
        raise ValueError("experiment.identity_invalid")
    return value


def _canonical_uuid4(value: object) -> str:
    """只接受无歧义的小写 RFC 4122 UUIDv4 文本，作为高熵且轮换稳定的幂等材料。"""

    if type(value) is not str:
        # 禁止 UUID 对象或可隐式转字符串的值，确保 HTTP 与持久化使用同一 canonical 文本。
        raise ValueError("experiment.idempotency_key_invalid")
    try:
        parsed = UUID(value)
    # 解析失败统一收敛为固定输入错误，不透传解释器异常或原始幂等键。
    except (ValueError, AttributeError) as exc:
        raise ValueError("experiment.idempotency_key_invalid") from exc
    if str(parsed) != value or parsed.version != 4 or parsed.variant != RFC_4122:
        # 大写、非 v4 或非 RFC 4122 变体会削弱唯一性或制造多文本等价，必须拒绝。
        raise ValueError("experiment.idempotency_key_invalid")
    return value


def _visible_text(value: object, *, maximum: int) -> str:
    """校验允许展示但不得进入日志的规范用户文本。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        # 实验名称允许展示但禁止控制字符，且永远不能进入审计或结构化日志。
        raise ValueError("experiment.create_fingerprint_invalid")
    return value


def _bounded_text(value: object, *, maximum: int) -> str:
    """校验内部敏感配置文本；调用方只能使用返回摘要，不能记录原值。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        # 敏感执行配置必须有界且无控制字符；合法原值也只能参与内存摘要。
        raise ValueError("experiment.route_config_invalid")
    return value


def _safe_code(value: object) -> str:
    """校验路由指纹中的稳定小写目录代码，防止非规范 Provider 身份产生摘要歧义。"""

    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        # 目录 code 必须匹配冻结字符集，不能通过隐式转换接收任意对象。
        raise ValueError("experiment.route_config_invalid")
    return value


def _safe_version(value: object) -> str:
    """校验允许数字开头的 Adapter 语义版本。"""

    if type(value) is not str or _SAFE_VERSION.fullmatch(value) is None:
        # Adapter 版本允许数字开头但仍须有界，避免不同节点采用不同规范化规则。
        raise ValueError("experiment.route_config_invalid")
    return value


def _lower_hex_64(value: object) -> str:
    """校验 Adapter 目录的完整小写 SHA-256，确保实验期间执行协议身份不漂移。"""

    if type(value) is not str or _LOWER_HEX_64.fullmatch(value) is None:
        # 截断、大写或非十六进制摘要均不能证明同一 Adapter 目录制品。
        raise ValueError("experiment.route_config_invalid")
    return value


def _positive_int(value: object) -> int:
    """校验路由指纹中不会溢出 SQLite signed-64 的严格正整数配置值。"""

    if type(value) is not int or not 1 <= value <= 9_223_372_036_854_775_807:
        # bool、零、负数或越界整数不能作为模型执行配置参与冻结摘要。
        raise ValueError("experiment.route_config_invalid")
    return value


def _canonical_capabilities(value: object) -> dict[str, bool]:
    """只接受有界 code -> bool 能力图，避免任意嵌套对象进入摘要入口。"""

    if not isinstance(value, Mapping) or not 1 <= len(value) <= 32:
        # 能力图必须有界且非空，避免任意嵌套结构进入跨服务摘要合同。
        raise ValueError("experiment.route_config_invalid")
    normalized: dict[str, bool] = {}
    for key, enabled in value.items():
        normalized_key = _safe_code(key)
        if type(enabled) is not bool or normalized_key in normalized:
            # 每项能力必须是唯一规范 code 到严格布尔值，不能接受真值兼容类型。
            raise ValueError("experiment.route_config_invalid")
        normalized[normalized_key] = enabled
    return dict(sorted(normalized.items()))


def _bucket(secret: str, domain: bytes, canonical: bytes) -> int:
    """按冻结合同把 SHA-256 digest 前 8 字节映射到 10000 个 bucket。"""

    digest = _domain_hmac(secret, domain, canonical)
    return int.from_bytes(digest[:8], "big", signed=False) % EXPERIMENT_BUCKET_COUNT


def _domain_hmac(secret: str, domain: bytes, canonical: bytes) -> bytes:
    """验证部署根密钥并执行域隔离 HMAC-SHA256；密钥、输入与摘要均不得记录。"""

    # HMAC 根密钥必须是非空字符串，禁止隐式转换环境对象或缺省为弱密钥。
    if type(secret) is not str or not secret:
        raise ValueError("experiment.hmac_secret_invalid")
    try:
        secret_bytes = secret.encode("utf-8", errors="strict")
    # 编码失败时仅返回固定合同错误，不能在异常链之外记录原始密钥文本。
    except UnicodeEncodeError as exc:
        raise ValueError("experiment.hmac_secret_invalid") from exc
    if not 16 <= len(secret_bytes) <= 512:
        # 过短密钥不满足安全强度，异常超长值也不应进入热路径 HMAC 计算。
        raise ValueError("experiment.hmac_secret_invalid")
    # 域前缀含冻结 NUL 终止字节，防止采样、选臂、幂等和路由指纹互相替代。
    return hmac.new(secret_bytes, domain + canonical, hashlib.sha256).digest()
