"""PRD-050 Prompt Cache 亲和摘要与候选偏好共享纯函数。

本模块只处理内存中的有界 canonical 数据和不可逆 HMAC 摘要，不持久化 Prompt、
Virtual Key、数据分级或上游端点详情。Gateway 只能在原路由候选已经完成资格判断后调用
偏好函数，亲和不得复活 rejected 候选，也不得降低健康或质量证据层级。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from .routing_policy import (
    REASON_STANDBY,
    CandidateFinalStatus,
    RoutingCandidateResult,
    RoutingSelectionResult,
)

# canonical 和四类摘要各自带不可变版本；修改任何序列化语义时必须升级对应版本，
# 不能让新代码把旧 Binding 误解释为同一个上游缓存域。
CACHE_AFFINITY_DIGEST_SCHEMA_VERSION = 1
MAX_CACHE_AFFINITY_PREFIX_CHARS = 131_072
MAX_CACHE_AFFINITY_CANONICAL_BYTES = 512 * 1024
MAX_CACHE_AFFINITY_CANONICAL_DEPTH = 16
MAX_CACHE_AFFINITY_CANONICAL_NODES = 4_096
MAX_CACHE_AFFINITY_TOOLS = 128

# 原因码会进入不可变 RoutingDecision/Candidate，必须与迁移 CHECK、API 和前端字典一致。
REASON_CACHE_AFFINITY_PENDING_PREFERRED = "routing.cache_affinity_pending_preferred"
REASON_CACHE_AFFINITY_WARM_PREFERRED = "routing.cache_affinity_warm_preferred"

# 每种 HMAC 使用独立域分隔符，避免相同字段正文跨用途得到可替换的摘要。
_PROMPT_FINGERPRINT_DOMAIN = b"metahub/cache-affinity/prompt-fingerprint/v1\x00"
_AFFINITY_SCOPE_DOMAIN = b"metahub/cache-affinity/scope/v1\x00"
_ENDPOINT_REVISION_DOMAIN = b"metahub/cache-affinity/endpoint-revision/v1\x00"
_AFFINITY_KEY_DOMAIN = b"metahub/cache-affinity/key/v1\x00"
_CANONICAL_OPTION_FIELDS = (
    "tools",
    "tool_choice",
    "response_format",
    "parallel_tool_calls",
)

# 偏好状态来自当前事务读取的 pending reservation 或有效 warm 证据；动作说明是否真正改序。
type CacheAffinityPreferenceState = Literal["pending", "warm"]
type CacheAffinityPreferenceAction = Literal["already_selected", "reordered", "tier_blocked"]


@dataclass(frozen=True, slots=True)
class CacheAffinityRequestContext:
    """一次请求的不可逆亲和上下文。

    五个字段均属于租户或 Prompt 安全域，即使已经 HMAC 也不得进入 repr、结构化日志或
    Webhook；调用方只可把摘要用于当前租户数据库查询。
    """

    tenant_id: str = field(repr=False)
    virtual_key_id: str = field(repr=False)
    data_class: str = field(repr=False)
    prompt_fingerprint: str = field(repr=False)
    affinity_scope_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CacheAffinityCandidateTransition:
    """记录候选应用亲和前后的安全排序事实。

    结构只包含本地对象 ID、rank、状态和固定原因码，不包含 scope、Prompt 摘要、
    data_class、Credential 或端点 URL，可用于 Gateway 构造 candidate_summary。
    """

    model_id: str
    provider_id: str | None
    base_rank: int | None
    base_status: CandidateFinalStatus
    base_reason_code: str
    final_rank: int | None
    final_status: CandidateFinalStatus
    final_reason_code: str


@dataclass(frozen=True, slots=True)
class CacheAffinityPreferenceOutcome:
    """端点亲和偏好的完整纯函数结果。

    ``selection`` 保留原算法冻结的价格、质量、Token 与公式证据；``action`` 供调用方
    区分原本已命中、实际重排和因硬层级阻断三种可审计结论。
    """

    selection: RoutingSelectionResult
    action: CacheAffinityPreferenceAction
    preference_state: CacheAffinityPreferenceState
    preferred_model_id: str
    preferred_provider_id: str
    base_selection_reason: str
    base_selected_model_id: str | None
    base_selected_provider_id: str | None
    candidate_transitions: tuple[CacheAffinityCandidateTransition, ...]


@dataclass(slots=True)
class _CanonicalBudget:
    """限制辅助 JSON 的遍历节点，避免 tools 等复杂结构放大 CPU 与内存占用。"""

    nodes: int = 0


def build_prompt_fingerprint(
    secret: str,
    *,
    request_kind: str,
    payload: Mapping[str, object],
    minimum_prefix_chars: int,
) -> str | None:
    """为 Chat 开头连续 system/developer 前缀生成 HMAC-SHA256 指纹。

    参数：独立 HMAC Secret、请求类型、已解析 JSON 对象和配置门限。
    返回：合法长前缀对应的 64 位小写摘要；短前缀、Embeddings 或结构不明确时返回
    ``None``，让 Gateway 沿用原路由但不产生任何亲和事实。
    约束：正文只在当前栈内短暂 canonical 化；不截断、不归一化，也不返回长度或原文。
    """

    _secret_bytes(secret)
    if (
        request_kind != "chat"
        or not isinstance(payload, Mapping)
        or type(minimum_prefix_chars) is not int
        or not 1 <= minimum_prefix_chars <= MAX_CACHE_AFFINITY_PREFIX_CHARS
    ):
        # 请求种类和门限不满足严格合同意味着亲和未启用，不能猜测另一种 Provider 方言。
        return None

    canonical = _build_prompt_canonical(payload, minimum_prefix_chars)
    if canonical is None:
        # 不可唯一解释的 Prompt 必须关闭亲和，避免不同请求被错误绑定到同一上游缓存域。
        return None
    return _domain_hmac(secret, _PROMPT_FINGERPRINT_DOMAIN, canonical)


def build_cache_affinity_scope_digest(
    secret: str,
    *,
    tenant_id: str,
    virtual_key_id: str,
    data_class: str,
    prompt_fingerprint: str,
) -> str:
    """生成 tenant、Virtual Key、数据分级与 Prompt 指纹组成的 scope 摘要。

    参数：独立 HMAC Secret 和四个完整安全域字段；返回：64 位小写摘要。
    约束：任一空值、控制字符、孤立 surrogate 或非标准 Prompt 摘要均抛出固定
    ``cache_affinity.input_invalid``，调用方不得省略隔离维度后继续查询 Binding。
    """

    values = {
        "tenant_id": _required_safe_text(tenant_id),
        "virtual_key_id": _required_safe_text(virtual_key_id),
        "data_class": _required_safe_text(data_class),
        "prompt_fingerprint": _required_digest(prompt_fingerprint),
    }
    # scope 摘要只返回不可逆十六进制值，tenant、Virtual Key 与分类原文不会离开当前调用栈。
    return _domain_hmac(secret, _AFFINITY_SCOPE_DOMAIN, _canonical_json_bytes(values))


def build_endpoint_revision_digest(
    secret: str,
    *,
    tenant_id: str,
    model_endpoint_id: str,
    provider_id: str,
    provider_type: str,
    auth_type: str,
    base_url: str,
    provider_model_id: str,
) -> str:
    """为端点身份和可变上游映射生成独立 revision 摘要。

    参数：租户、本地端点/Provider 身份、Provider 与认证类型、base URL 和上游模型名；
    返回：64 位小写 HMAC。URL 只在内存规范化，绝不由本函数返回或记录。
    约束：上游地址、Provider 或模型映射任一变化都必须产生新的 Binding 域。
    """

    values = {
        "tenant_id": _required_safe_text(tenant_id),
        "model_endpoint_id": _required_safe_text(model_endpoint_id),
        "provider_id": _required_safe_text(provider_id),
        "provider_type": _required_safe_text(provider_type),
        "auth_type": _required_safe_text(auth_type),
        "base_url": _normalize_base_url(base_url),
        "provider_model_id": _required_safe_text(provider_model_id),
    }
    # 端点敏感映射仅作为 HMAC 正文使用，返回值不能反推出 Provider URL 或上游模型名。
    return _domain_hmac(secret, _ENDPOINT_REVISION_DOMAIN, _canonical_json_bytes(values))


def build_cache_affinity_key_digest(
    secret: str,
    *,
    affinity_scope_digest: str,
    model_endpoint_id: str,
    endpoint_revision_digest: str,
) -> str:
    """生成完整 scope 与当前 endpoint revision 组成的 Binding 身份摘要。

    参数：scope 摘要、本地端点 ID 和端点 revision 摘要；返回：64 位小写 HMAC。
    约束：只接受标准摘要和非空本地 ID，禁止把 Alias、配置版本或 Credential 加入身份，
    从而保持架构定义的五维隔离与跨 Alias 合法复用。
    """

    values = {
        "affinity_scope_digest": _required_digest(affinity_scope_digest),
        "model_endpoint_id": _required_safe_text(model_endpoint_id),
        "endpoint_revision_digest": _required_digest(endpoint_revision_digest),
    }
    # Binding 身份保留完整五维隔离，绝不省略 scope 或 endpoint revision 后生成宽域摘要。
    return _domain_hmac(secret, _AFFINITY_KEY_DOMAIN, _canonical_json_bytes(values))


def build_cache_affinity_request_context(
    secret: str,
    *,
    tenant_id: str | None,
    virtual_key_id: str | None,
    data_class: str | None,
    request_kind: str,
    payload: Mapping[str, object],
    minimum_prefix_chars: int,
) -> CacheAffinityRequestContext | None:
    """在全部可信隔离维度齐备时构造 repr-safe 请求上下文。

    参数：认证所得 tenant/Virtual Key、服务端 CacheAffinity 分类及请求正文。
    返回：完整不可逆上下文；缺维度、短前缀或不可 canonical 时返回 ``None``。
    约束：不得从 Header、OpenAI metadata 或客户端正文补造 ``data_class``。
    """

    if not all(
        isinstance(value, str) and _is_safe_text(value)
        for value in (tenant_id, virtual_key_id, data_class)
    ):
        # 缺少任何租户隔离维度时亲和必须整体关闭，不能构造范围更宽的降级 cache key。
        return None
    fingerprint = build_prompt_fingerprint(
        secret,
        request_kind=request_kind,
        payload=payload,
        minimum_prefix_chars=minimum_prefix_chars,
    )
    if fingerprint is None:
        # 短或结构不明确的正文继续走旧路由，但绝不能留下 scope 或 Selection 事实。
        return None
    # 上方强类型检查已经证明三个 Optional 值均为非空安全字符串。
    safe_tenant_id = tenant_id
    safe_virtual_key_id = virtual_key_id
    safe_data_class = data_class
    scope_digest = build_cache_affinity_scope_digest(
        secret,
        tenant_id=safe_tenant_id,
        virtual_key_id=safe_virtual_key_id,
        data_class=safe_data_class,
        prompt_fingerprint=fingerprint,
    )
    return CacheAffinityRequestContext(
        tenant_id=safe_tenant_id,
        virtual_key_id=safe_virtual_key_id,
        data_class=safe_data_class,
        prompt_fingerprint=fingerprint,
        affinity_scope_digest=scope_digest,
    )


def apply_cache_affinity_endpoint_preference(
    base_selection: RoutingSelectionResult,
    *,
    preferred_model_id: str,
    preferred_provider_id: str,
    preference_state: CacheAffinityPreferenceState,
) -> CacheAffinityPreferenceOutcome:
    """在现有合格候选内部应用 pending/warm 端点偏好。

    参数：原算法冻结结果、Binding 指向的 model/Provider 和证据状态。
    返回：保持候选 tuple 顺序的新结果以及逐候选前后状态。
    约束：fixed 永不改序；rejected 永不复活；availability_first 与 quality_cost 只允许
    同证据层交换。价格、质量、Token、公式版本和依赖执行标志全部通过 ``replace`` 保留。
    """

    if not isinstance(preference_state, str) or preference_state not in ("pending", "warm"):
        # 未知状态无法映射到稳定原因码，必须拒绝而不是默认成可能虚报预计命中的 warm。
        raise ValueError("cache_affinity.input_invalid")
    _required_safe_text(preferred_model_id)
    _required_safe_text(preferred_provider_id)

    selected_index = _selected_candidate_index(base_selection)
    preferred_index = _preferred_candidate_index(
        base_selection,
        model_id=preferred_model_id,
        provider_id=preferred_provider_id,
    )
    action: CacheAffinityPreferenceAction = "tier_blocked"
    final_selection = base_selection

    if selected_index is not None and preferred_index == selected_index:
        # 原算法已经选择同一 model/provider，不重写基础 reason，保留真实选择依据用于审计。
        action = "already_selected"
    elif (
        selected_index is not None
        and preferred_index is not None
        and base_selection.routing_mode != "fixed"
        and _can_promote_preferred(
            base_selection,
            selected_index=selected_index,
            preferred_index=preferred_index,
        )
    ):
        # 只有动态模式且证据层相同的 standby 才能交换 rank/status/reason，硬拒绝事实保持原样。
        final_selection = _reorder_selection(
            base_selection,
            selected_index=selected_index,
            preferred_index=preferred_index,
            preference_state=preference_state,
        )
        action = "reordered"

    transitions = _candidate_transitions(base_selection, final_selection)
    return CacheAffinityPreferenceOutcome(
        selection=final_selection,
        action=action,
        preference_state=preference_state,
        preferred_model_id=preferred_model_id,
        preferred_provider_id=preferred_provider_id,
        base_selection_reason=base_selection.selection_reason,
        base_selected_model_id=base_selection.selected_model_id,
        base_selected_provider_id=base_selection.selected_provider_id,
        candidate_transitions=transitions,
    )


def _build_prompt_canonical(
    payload: Mapping[str, object], minimum_prefix_chars: int
) -> bytes | None:
    """提取严格 leading messages 与影响 Provider 缓存域的可选工具字段。"""

    messages = payload.get("messages")
    if type(messages) is not list:
        # Chat messages 不是标准 JSON 数组时无法唯一重放 Provider 序列化，关闭亲和。
        return None
    leading: list[dict[str, object]] = []
    prefix_chars = 0
    for raw_message in messages:
        if type(raw_message) is not dict:
            # 即使对象出现在 user 后缀之外，也不能遍历未知类型来猜测消息边界。
            return None
        role = raw_message.get("role")
        if not isinstance(role, str):
            # null、数组或对象 role 不是合法消息边界，不能当作普通 user 后缀静默跳过。
            return None
        if role not in ("system", "developer"):
            break
        canonical_message = _canonical_leading_message(raw_message)
        if canonical_message is None:
            # leading 消息出现 extra key 或非文本 part 时，整个请求禁用亲和而不是丢字段摘要。
            return None
        prefix_chars += _canonical_message_text_chars(canonical_message)
        if prefix_chars > MAX_CACHE_AFFINITY_PREFIX_CHARS:
            # 超长正文不能截断；截断会让不同 Prompt 在相同前缀处发生人为碰撞。
            return None
        leading.append(canonical_message)

    if not leading or prefix_chars < minimum_prefix_chars:
        # 空前缀和短前缀没有稳定的上游缓存收益，继续原路由且不生成任何摘要。
        return None

    canonical_payload: dict[str, object] = {
        "digest_schema_version": CACHE_AFFINITY_DIGEST_SCHEMA_VERSION,
        "messages": leading,
    }
    for field_name in _CANONICAL_OPTION_FIELDS:
        if field_name in payload:
            value = payload[field_name]
            if field_name == "tools" and (
                type(value) is not list or len(value) > MAX_CACHE_AFFINITY_TOOLS
            ):
                # tools 不是数组或超过硬上限时关闭亲和，避免无界工具图拖慢每个模型请求。
                return None
            canonical_payload[field_name] = value

    budget = _CanonicalBudget()
    if not _validate_json_value(canonical_payload, depth=1, budget=budget, active_ids=set()):
        # 非 JSON、控制字符、循环、过深或过宽结构都没有稳定 canonical 表达。
        return None
    try:
        encoded = _canonical_json_bytes(canonical_payload)
    # 孤立 surrogate 等非法 Unicode 必须关闭亲和，不能采用替换字符后制造摘要碰撞。
    except (UnicodeEncodeError, ValueError, TypeError):
        return None
    if len(encoded) > MAX_CACHE_AFFINITY_CANONICAL_BYTES:
        # canonical 字节上限独立于字符上限，覆盖多字节中文和复杂 tools 配置。
        return None
    return encoded


def _canonical_leading_message(message: dict[object, object]) -> dict[str, object] | None:
    """把一条 system/developer 消息转换为精确白名单结构。"""

    # 消息白名单必须同时拒绝缺少 role/content 和任何扩展字段，避免 Provider 序列化分叉。
    if set(message) - {"role", "content", "name"} or not {"role", "content"} <= set(message):
        # extra 或缺字段会改变 Provider 方言的实际序列化，不能静默删除后继续指纹计算。
        return None
    role = message["role"]
    name = message.get("name")
    if (
        not isinstance(role, str)
        or role not in ("system", "developer")
        or ("name" in message and not _is_safe_text(name))
    ):
        # role/name 必须是安全文本；非字符串 name 不能通过 JSON 强制转换兼容。
        return None
    content = message["content"]
    if isinstance(content, str):
        if not _is_json_text(content):
            # 控制字符或孤立 surrogate 会污染日志/签名边界，整条请求关闭亲和。
            return None
        canonical_content: object = content
    elif type(content) is list:
        canonical_parts: list[dict[str, str]] = []
        for part in content:
            if type(part) is not dict or set(part) != {"type", "text"}:
                # 图片、音频、文件、未知 part 或 extra key 均不具备跨 Provider 的稳定文本语义。
                return None
            part_type = part["type"]
            text = part["text"]
            if (
                not isinstance(part_type, str)
                or part_type not in ("text", "input_text")
                or not _is_json_text(text)
            ):
                # part 类型和正文必须保持原值，非法值不能默认转换成普通 text。
                return None
            canonical_parts.append({"type": part_type, "text": text})
        canonical_content = canonical_parts
    else:
        # null、对象和数值 content 都无法形成明确的固定文本前缀。
        return None

    canonical: dict[str, object] = {"role": role, "content": canonical_content}
    if "name" in message:
        canonical["name"] = name
    return canonical


def _canonical_message_text_chars(message: dict[str, object]) -> int:
    """计算已验证 leading message 的 Unicode code point 数且不保留额外正文副本。"""

    content = message["content"]
    if isinstance(content, str):
        return len(content)
    # canonical 构造器保证数组每项都有字符串 text；这里只统计门限，不作为外部可见指标。
    return sum(len(part["text"]) for part in content if isinstance(part, dict))


def _validate_json_value(
    value: object,
    *,
    depth: int,
    budget: _CanonicalBudget,
    active_ids: set[int],
) -> bool:
    """递归校验 canonical JSON 类型、深度、节点、文本与循环引用上限。"""

    budget.nodes += 1
    if (
        budget.nodes > MAX_CACHE_AFFINITY_CANONICAL_NODES
        or depth > MAX_CACHE_AFFINITY_CANONICAL_DEPTH
    ):
        # 节点或深度越界直接失败关闭，不能先完整序列化后才检查内存大小。
        return False
    if value is None or type(value) in {bool, int}:
        return True
    if type(value) is float:
        # JSON 标准不包含 NaN/Infinity；允许它们会让不同语言 canonical 结果分叉。
        return math.isfinite(value)
    if isinstance(value, str):
        return _is_json_text(value)
    if type(value) not in {dict, list}:
        # Decimal、bytes、tuple 和自定义对象不得借 repr 或隐式转换进入 HMAC 正文。
        return False
    identity = id(value)
    if identity in active_ids:
        # 内存调用方可能传入循环容器；JSON 无法表达循环，必须在递归前阻断。
        return False
    active_ids.add(identity)
    try:
        if type(value) is list:
            return all(
                _validate_json_value(
                    child,
                    depth=depth + 1,
                    budget=budget,
                    active_ids=active_ids,
                )
                for child in value
            )
        for key, child in value.items():
            if not isinstance(key, str) or not _is_json_text(key):
                # 非字符串或不安全对象键不能依赖 json.dumps 的隐式转换与排序行为。
                return False
            if not _validate_json_value(
                child,
                depth=depth + 1,
                budget=budget,
                active_ids=active_ids,
            ):
                # 任一子树非法都关闭整个亲和上下文，不能删除坏字段后改变上游缓存语义。
                return False
        return True
    finally:
        active_ids.remove(identity)


def _canonical_json_bytes(value: object) -> bytes:
    """按 Unicode 键序、数组原序和紧凑 UTF-8 生成稳定 JSON 字节。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _domain_hmac(secret: str, domain: bytes, canonical: bytes) -> str:
    """用独立域分隔符计算 HMAC-SHA256，明文 Secret 不离开当前调用栈。"""

    # HMAC 而不是裸 SHA-256 可防止低熵固定 Prompt 被离线字典反推；摘要只返回小写 hex。
    return hmac.new(_secret_bytes(secret), domain + canonical, hashlib.sha256).hexdigest()


def _secret_bytes(secret: str) -> bytes:
    """把运行时 Secret 转为 UTF-8 字节并拒绝空值或非法 Unicode。"""

    # 空值或非字符串表示配置门禁被绕过，必须在计算任何 Prompt 摘要前立即失败关闭。
    if not isinstance(secret, str) or not secret:
        # 缺 Secret 属于进程配置错误，不能退回裸 hash 或固定公共密钥继续生成 Binding。
        raise ValueError("cache_affinity.input_invalid")
    try:
        return secret.encode("utf-8")
    # 错误信息只返回固定业务码，绝不回显 Secret 或其可关联摘要。
    except UnicodeEncodeError:
        raise ValueError("cache_affinity.input_invalid") from None


def _required_digest(value: object) -> str:
    """校验数据库可持久化摘要为 64 位小写十六进制字符串。"""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        # 非标准摘要无法与迁移 CHECK 和 HMAC 合同对应，必须在写库前拒绝。
        raise ValueError("cache_affinity.input_invalid")
    return value


def _required_safe_text(value: object) -> str:
    """返回非空、无控制字符且可 UTF-8 编码的内部身份文本。"""

    if not _is_safe_text(value):
        # 内部 ID 或端点字段不合法时不能拼接字符串生成含糊的 HMAC 正文。
        raise ValueError("cache_affinity.input_invalid")
    return value


def _is_safe_text(value: object) -> bool:
    """判断身份/分类文本是否非空、可编码且没有空白或控制字符。"""

    if not isinstance(value, str) or not value:
        return False
    if any(character.isspace() or ord(character) < 32 for character in value):
        # 身份字段不允许空白，避免日志、URL 或数据库审计中出现多种视觉等价值。
        return False
    try:
        value.encode("utf-8")
    # 孤立 surrogate 没有合法 UTF-8 表达，不能参与跨服务稳定摘要。
    except UnicodeEncodeError:
        return False
    return True


def _is_json_text(value: object) -> bool:
    """判断 Prompt/canonical 文本可 UTF-8 编码且不含 Unicode 控制字符。"""

    if not isinstance(value, str):
        return False
    # 任何 C0/C1 控制字符都会造成不同 Provider 序列化分叉，不能因为 JSON 可转义就放宽。
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        return False
    try:
        value.encode("utf-8")
    # 孤立 surrogate 不采用替换字符，避免两个不同非法输入映射到同一摘要。
    except UnicodeEncodeError:
        return False
    return True


def _normalize_base_url(value: object) -> str:
    """规范化端点 URL，确保语义等价地址得到同一个 revision 摘要。

    参数：配置图读取的 Provider base URL；返回：仅供 HMAC 使用的 scheme/host/端口/路径。
    约束：拒绝凭据、query、fragment 和非法端口，调用方不得记录规范化结果。
    """

    raw = _required_safe_text(value)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    # 非法 IPv6 或端口只返回固定错误码，不能把包含凭据的 URL 写入异常正文。
    except ValueError:
        raise ValueError("cache_affinity.input_invalid") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        # 凭据、query 与 fragment 会改变身份解释或泄露敏感信息，不能进入 endpoint revision。
        raise ValueError("cache_affinity.input_invalid")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _selected_candidate_index(selection: RoutingSelectionResult) -> int | None:
    """定位与 selection 顶层身份一致的 selected 候选，损坏快照返回 None。"""

    if selection.status != "selected":
        return None
    matches = [
        index
        for index, result in enumerate(selection.candidate_results)
        if result.status == "selected"
        and result.candidate.model_id == selection.selected_model_id
        and result.candidate.provider_id == selection.selected_provider_id
    ]
    # 只有唯一 selected 行才可参与重排；损坏快照失败关闭为 tier_blocked。
    return matches[0] if len(matches) == 1 else None


def _preferred_candidate_index(
    selection: RoutingSelectionResult, *, model_id: str, provider_id: str
) -> int | None:
    """定位当前候选图中与 Binding 完整 model/provider 身份一致的非 rejected 行。"""

    matches = [
        index
        for index, result in enumerate(selection.candidate_results)
        if result.candidate.model_id == model_id
        and result.candidate.provider_id == provider_id
        and result.status != "rejected"
    ]
    # 重复候选身份没有唯一事务目标，不能任意选择其中一行。
    return matches[0] if len(matches) == 1 else None


def _can_promote_preferred(
    selection: RoutingSelectionResult, *, selected_index: int, preferred_index: int
) -> bool:
    """判断偏好候选是否与当前选择处于允许交换的健康/质量证据层。"""

    selected = selection.candidate_results[selected_index]
    preferred = selection.candidate_results[preferred_index]
    if preferred.status != "standby" or preferred.rank is None or selected.rank is None:
        # rejected、无 rank 或状态损坏的候选不得被亲和复活成 selected。
        return False
    if selection.routing_mode == "availability_first":
        selected_tier = (
            selected.candidate.health_state == "healthy" and selected.candidate.health_fresh
        )
        preferred_tier = (
            preferred.candidate.health_state == "healthy" and preferred.candidate.health_fresh
        )
        # fresh healthy 只能和 fresh healthy 互换；unknown/stale 不能覆盖更强健康证据。
        return selected_tier == preferred_tier
    if selection.routing_mode == "quality_cost":
        # ready 与 cold 属于不同证据层，亲和不允许改变 PRD-047 的质量优先关系。
        return selected.quality_evidence_state == preferred.quality_evidence_state
    # cost_first 的价格排序是软偏好；资格硬约束已在 rejected 状态中冻结。
    return selection.routing_mode == "cost_first"


def _reorder_selection(
    selection: RoutingSelectionResult,
    *,
    selected_index: int,
    preferred_index: int,
    preference_state: CacheAffinityPreferenceState,
) -> RoutingSelectionResult:
    """只交换两条合格候选的 rank/status/reason 并保留全部其他冻结证据。"""

    reason = (
        REASON_CACHE_AFFINITY_WARM_PREFERRED
        if preference_state == "warm"
        else REASON_CACHE_AFFINITY_PENDING_PREFERRED
    )
    original_selected = selection.candidate_results[selected_index]
    preferred = selection.candidate_results[preferred_index]
    updated: list[RoutingCandidateResult] = list(selection.candidate_results)
    # 降格候选只改为既有 standby 原因；价格、质量和依赖字段由 dataclass replace 原样复制。
    updated[selected_index] = replace(
        original_selected,
        rank=preferred.rank,
        status="standby",
        reason_code=REASON_STANDBY,
    )
    # 升格候选取得原 selected rank，并使用唯一可审计的 pending/warm 亲和原因。
    updated[preferred_index] = replace(
        preferred,
        rank=original_selected.rank,
        status="selected",
        reason_code=reason,
    )
    return replace(
        selection,
        selection_reason=reason,
        selected_model_id=preferred.candidate.model_id,
        selected_provider_id=preferred.candidate.provider_id,
        candidate_results=tuple(updated),
    )


def _candidate_transitions(
    base: RoutingSelectionResult, final: RoutingSelectionResult
) -> tuple[CacheAffinityCandidateTransition, ...]:
    """按原 tuple 顺序构造每条候选的前后 rank/status/reason 快照。"""

    if len(base.candidate_results) != len(final.candidate_results):
        # 纯函数不应增删候选；长度漂移代表内部合同损坏，不能生成误导性 candidate_summary。
        raise ValueError("cache_affinity.input_invalid")
    return tuple(
        CacheAffinityCandidateTransition(
            model_id=before.candidate.model_id,
            provider_id=before.candidate.provider_id,
            base_rank=before.rank,
            base_status=before.status,
            base_reason_code=before.reason_code,
            final_rank=after.rank,
            final_status=after.status,
            final_reason_code=after.reason_code,
        )
        for before, after in zip(base.candidate_results, final.candidate_results, strict=True)
    )


# 显式导出共享合同，避免 Gateway 依赖带下划线的 canonical 或安全校验实现细节。
__all__ = [
    "CACHE_AFFINITY_DIGEST_SCHEMA_VERSION",
    "MAX_CACHE_AFFINITY_CANONICAL_BYTES",
    "MAX_CACHE_AFFINITY_CANONICAL_DEPTH",
    "MAX_CACHE_AFFINITY_CANONICAL_NODES",
    "MAX_CACHE_AFFINITY_PREFIX_CHARS",
    "MAX_CACHE_AFFINITY_TOOLS",
    "REASON_CACHE_AFFINITY_PENDING_PREFERRED",
    "REASON_CACHE_AFFINITY_WARM_PREFERRED",
    "CacheAffinityCandidateTransition",
    "CacheAffinityPreferenceAction",
    "CacheAffinityPreferenceOutcome",
    "CacheAffinityPreferenceState",
    "CacheAffinityRequestContext",
    "apply_cache_affinity_endpoint_preference",
    "build_cache_affinity_key_digest",
    "build_cache_affinity_request_context",
    "build_cache_affinity_scope_digest",
    "build_endpoint_revision_digest",
    "build_prompt_fingerprint",
]
