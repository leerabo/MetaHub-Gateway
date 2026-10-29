"""企业 Prompt 模板的严格定义、渲染、密文和分域摘要实现。

模板正文属于敏感业务数据。本模块的异常只携带固定错误码，所有含正文的字段均关闭
``repr``，并且没有任何日志路径。调用方只能把 canonical definition 加密落库，不能保存
本模块返回的明文副本、渲染结果或变量值。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from secrets import token_bytes
from string import Template
from typing import Any, Final

# schema、密文前缀和 key version 同时参与 AAD，后续轮换必须新增显式版本，不能静默改义。
PROMPT_TEMPLATE_SCHEMA_VERSION: Final[str] = "prompt-template-v1"
PROMPT_TEMPLATE_KEY_VERSION: Final[str] = "v1"
PROMPT_TEMPLATE_CIPHERTEXT_FORMAT: Final[str] = "ptpl-aesgcm-v1"

# 这些上限来自 PRD-067，Control、Gateway 和 Console 不得各自定义更宽边界。
MAX_PROMPT_CONTENT_BYTES: Final[int] = 32 * 1024
MAX_RENDERED_PROMPT_BYTES: Final[int] = 64 * 1024
MAX_TEMPLATE_VARIABLE_VALUE_BYTES: Final[int] = 64 * 1024
MAX_TEMPLATE_VARIABLES: Final[int] = 32
MAX_CHANGE_SUMMARY_BYTES: Final[int] = 900
MAX_DEFINITION_BYTES: Final[int] = 128 * 1024

_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_VARIABLE_NAMES: Final[frozenset[str]] = frozenset(
    {"constructor", "prototype", "__proto__"}
)
_ENCRYPTION_DOMAIN: Final[bytes] = b"metahub/prompt-template/encryption/v1"
_IDEMPOTENCY_DOMAIN: Final[bytes] = b"metahub/prompt-template/idempotency/v1"
_CHANGE_SUMMARY_DOMAIN: Final[bytes] = b"metahub/prompt-template/change-summary/v1"


class PromptTemplateContractError(ValueError):
    """只携带稳定公开错误码的模板合同异常。

    参数：固定错误码。返回值：无。关键约束：异常文本不得拼接正文、变量名、密文、
    owner ID 或密码学细节，防止框架日志和错误追踪系统扩大敏感数据暴露面。
    """

    def __init__(self, code: str) -> None:
        """保存固定错误码，供 Control 或 Gateway 映射各自公开合同。"""

        super().__init__(code)
        self.code = code


class PromptTemplateSecretUnavailable(PromptTemplateContractError):
    """表示独立模板根密钥缺失或不满足最小安全边界。"""

    def __init__(self) -> None:
        """构造不披露配置值和失败细节的 503 语义异常。"""

        super().__init__("prompt_template.unavailable")


class PromptTemplateDefinitionUnavailable(PromptTemplateContractError):
    """表示已存定义、密文格式、AAD 认证或 schema 已损坏。"""

    def __init__(self) -> None:
        """构造不披露密文和密码学细节的固定 500 语义异常。"""

        super().__init__("prompt_template.definition_unavailable")


# Control/Gateway 使用 Cipher 名称区分“部署密钥不可用”和“持久密文已损坏”；别名保持与
# 通用合同异常完全相同的固定错误码与捕获语义，不再维护一份重复密码学实现。
PromptTemplateCipherUnavailable = PromptTemplateSecretUnavailable
PromptTemplateCipherInvalid = PromptTemplateDefinitionUnavailable


@dataclass(frozen=True, slots=True)
class PromptVariableDefinition:
    """一个受限变量声明。

    ``name`` 是服务端替换键；``label`` 只供授权页面显示；``required`` 控制空字符串；
    ``max_chars`` 约束单值字符数。变量不支持默认值、类型转换、表达式或嵌套对象。
    """

    name: str = field(repr=False)
    label: str = field(repr=False)
    required: bool = field(repr=False)
    max_chars: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class PromptTemplateDefinition:
    """通过严格校验、可被加密的一份不可变模板定义。

    正文和变更说明关闭 ``repr``，避免 DTO 被调试日志打印；变量使用 tuple，防止发布后被
    调用方原地修改。该对象只能存在于创建、授权读取或单次 Gateway 渲染调用栈。
    """

    content: str = field(repr=False)
    variables: tuple[PromptVariableDefinition, ...] = field(repr=False)
    change_summary: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PromptTemplateOwner:
    """生成 AES-GCM AAD 所需的可信数据库 owner。

    这些字段必须来自同一 tenant 的数据库行，不能从浏览器正文恢复；四个 owner 维度使
    跨 tenant、跨模板、跨版本或篡改版本号的密文交换都因认证标签不匹配而失败。
    """

    tenant_id: str
    prompt_template_id: str
    prompt_template_version_id: str
    version_number: int


def validate_definition(
    *,
    content: object,
    variables: object,
    change_summary: object,
) -> PromptTemplateDefinition:
    """校验并返回不可变模板定义。

    参数：敏感正文、变量声明序列和变更说明。返回：规范化但不改写正文的不可变定义。
    关键约束：占位符只能是完整 ``${lower_snake_case}``，声明集合与使用集合必须完全一致；
    任何失败只抛固定错误码，不把输入值放入异常。
    """

    if (
        type(content) is not str
        or not content.strip()
        or _utf8_size(content) > MAX_PROMPT_CONTENT_BYTES
    ):
        # 正文必须是有界非空 UTF-8；固定错误不携带原始 Prompt 或失败位置。
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    # 变量声明只接受真正序列，字符串/bytes 不能被逐字符误当成变量集合。
    if not isinstance(variables, Sequence) or isinstance(variables, (str, bytes, bytearray)):
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    # 变量数量硬上限在构造 DTO 前执行，避免攻击者放大解析和 canonical JSON 开销。
    if len(variables) > MAX_TEMPLATE_VARIABLES:
        raise PromptTemplateContractError("prompt_template.definition_invalid")

    normalized_variables: list[PromptVariableDefinition] = []
    seen_names: set[str] = set()
    for raw_variable in variables:
        variable = _validate_variable_definition(raw_variable)
        # 同名声明会让 exact-set 和表单含义歧义，统一拒绝且不回显变量名。
        if variable.name in seen_names:
            raise PromptTemplateContractError("prompt_template.definition_invalid")
        seen_names.add(variable.name)
        normalized_variables.append(variable)

    normalized_summary = _validate_change_summary(change_summary)
    placeholders = _extract_placeholders(content)
    # exact-set 阻止未声明键被替换，也阻止声明但未使用的字段制造含糊调用合同。
    if placeholders != seen_names:
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    definition = PromptTemplateDefinition(
        content=content,
        variables=tuple(normalized_variables),
        change_summary=normalized_summary,
    )
    # 完整 canonical definition 也必须有硬上限，避免大量 label/schema 放大密文和数据库页。
    if len(canonical_definition_bytes(definition)) > MAX_DEFINITION_BYTES:
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    return definition


def canonical_definition_bytes(definition: PromptTemplateDefinition) -> bytes:
    """把已校验定义编码为稳定 UTF-8 JSON。

    参数：不可变模板定义。返回：用于 AES-GCM 明文和 ``definition_size_bytes`` 的 canonical
    bytes。键排序与紧凑分隔符固定跨进程表示，但该字节串仍是敏感明文，不得写日志或普通列。
    """

    # 只允许已校验的不可变 DTO 进入加密入口，禁止任意对象借 canonical 编码绕过 schema。
    if not isinstance(definition, PromptTemplateDefinition):
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    payload = {
        "content": definition.content,
        "variables": [
            {
                "name": variable.name,
                "label": variable.label,
                "required": variable.required,
                "max_chars": variable.max_chars,
            }
            for variable in definition.variables
        ],
        "change_summary": definition.change_summary,
    }
    return _canonical_json_bytes(payload)


def parse_definition_bytes(value: object) -> PromptTemplateDefinition:
    """从认证成功的密文明文恢复并重新校验定义。

    参数：AES-GCM 解密得到的 bytes。返回：不可变定义。关键约束：仍拒绝重复键、未知字段、
    非 UTF-8 和超限内容；认证成功不代表数据库中的 schema 一定满足当前代码合同。
    """

    # 解密结果仍有固定类型和长度边界，损坏密文不能驱动无界 JSON 解析。
    if type(value) is not bytes or not 1 <= len(value) <= MAX_DEFINITION_BYTES:
        raise PromptTemplateDefinitionUnavailable()
    try:
        decoded = json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
        # schema 必须 exact-set；未知字段可能来自旁路写入或未来不兼容版本，不能静默忽略。
        if not isinstance(decoded, dict) or set(decoded) != {
            "content",
            "variables",
            "change_summary",
        }:
            # 非对象或字段集合漂移说明 schema/密文损坏，不能继续尝试宽松恢复。
            raise ValueError
        definition = validate_definition(
            content=decoded["content"],
            variables=decoded["variables"],
            change_summary=decoded["change_summary"],
        )
        # 解密后必须已经是 canonical 表示；否则数据库中可能存在歧义 schema 或旁路写入。
        if not hmac.compare_digest(canonical_definition_bytes(definition), value):
            raise ValueError
        return definition
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        RecursionError,
        PromptTemplateContractError,
    ):
        raise PromptTemplateDefinitionUnavailable() from None


def render_prompt(
    definition: PromptTemplateDefinition,
    values: object,
) -> str:
    """使用 exact-set 字符串变量在内存中渲染最终 user message。

    参数：已认证定义和浏览器提供的变量 map。返回：应追加到 history 的最终 user 正文。
    关键约束：不执行表达式、属性、函数或转义求值；值集合、类型、单值长度、总字节和渲染后
    字节均失败关闭，异常不会回显变量名或内容。
    """

    # 渲染只接受经过认证的定义和普通字典，拒绝可执行 Mapping 或宽松对象转换。
    if not isinstance(definition, PromptTemplateDefinition) or type(values) is not dict:
        raise PromptTemplateContractError("prompt_template.variables_invalid")
    expected = {variable.name for variable in definition.variables}
    # 输入键必须与声明 exact-set；缺失、额外或非字符串键都不得被模板引擎猜测处理。
    if set(values) != expected or any(type(key) is not str for key in values):
        raise PromptTemplateContractError("prompt_template.variables_invalid")

    normalized: dict[str, str] = {}
    total_bytes = 0
    by_name = {variable.name: variable for variable in definition.variables}
    for name in sorted(expected):
        value = values[name]
        variable = by_name[name]
        if (
            type(value) is not str
            or len(value) > variable.max_chars
            or (variable.required and not value.strip())
        ):
            # 单值类型、长度和必填失败只返回目录化错误，避免暴露变量名或客户内容。
            raise PromptTemplateContractError("prompt_template.variables_invalid")
        try:
            total_bytes += len(value.encode("utf-8"))
        # 孤立代理字符无法稳定进入 Provider wire，切断异常链以免正文进入 traceback。
        except UnicodeEncodeError:
            raise PromptTemplateContractError("prompt_template.variables_invalid") from None
        # 多变量合计也有硬上限，不能用多个合法小值绕过内存和渲染边界。
        if total_bytes > MAX_TEMPLATE_VARIABLE_VALUE_BYTES:
            raise PromptTemplateContractError("prompt_template.variables_invalid")
        normalized[name] = value
    try:
        rendered = Template(definition.content).substitute(normalized)
    # 即使定义曾验证，未来解析器差异仍必须失败关闭且不能把 Prompt 带入异常链。
    except (KeyError, ValueError):
        raise PromptTemplateContractError("prompt_template.definition_invalid") from None
    # 展开后正文可能远大于输入模板，必须在交给治理链前执行最终 UTF-8 上限。
    if (
        _utf8_size(rendered, error_code="prompt_template.variables_invalid")
        > MAX_RENDERED_PROMPT_BYTES
    ):
        # 最终正文超限必须在治理链调用前终止，且错误中不得携带渲染内容。
        raise PromptTemplateContractError("prompt_template.variables_invalid")
    return rendered


def encrypt_definition(
    definition: PromptTemplateDefinition,
    *,
    secret: object,
    owner: PromptTemplateOwner,
) -> str:
    """使用独立根密钥和 owner-bound AAD 加密 canonical definition。

    参数：已校验定义、部署级根密钥和可信数据库 owner。返回：版本化 AES-GCM 密文。
    关键约束：密钥不回退 Auth/Provider/Webhook 域；随机 nonce 每次生成，调用方只能落库返回密文。
    """

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derived_key(secret, _ENCRYPTION_DOMAIN)
    aad = _owner_aad(owner)
    nonce = token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, canonical_definition_bytes(definition), aad)
    return ":".join(
        (
            PROMPT_TEMPLATE_CIPHERTEXT_FORMAT,
            PROMPT_TEMPLATE_KEY_VERSION,
            _b64(nonce),
            _b64(ciphertext),
        )
    )


def decrypt_definition(
    ciphertext: object,
    *,
    secret: object,
    owner: PromptTemplateOwner,
) -> PromptTemplateDefinition:
    """认证 owner AAD、解密并重新校验模板定义。

    参数：数据库密文、部署级根密钥和可信 owner。返回：仅在当前授权调用栈存活的定义。
    密钥缺失单独保留 503 语义；格式、Base64、nonce、认证标签、UTF-8 或 schema 错误全部折叠为
    ``definition_unavailable``，不得泄露密码学细节。
    """

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derived_key(secret, _ENCRYPTION_DOMAIN)
    if type(ciphertext) is not str:
        raise PromptTemplateDefinitionUnavailable()
    parts = ciphertext.split(":")
    if len(parts) != 4 or parts[:2] != [
        PROMPT_TEMPLATE_CIPHERTEXT_FORMAT,
        PROMPT_TEMPLATE_KEY_VERSION,
    ]:
        raise PromptTemplateDefinitionUnavailable()
    try:
        nonce = _unb64(parts[2])
        encrypted = _unb64(parts[3])
        if len(nonce) != 12 or len(encrypted) < 16:
            # AES-GCM nonce/tag 外形不满足固定格式时按定义不可用处理，不暴露密码学细节。
            raise ValueError
        plaintext = AESGCM(key).decrypt(nonce, encrypted, _owner_aad(owner))
        return parse_definition_bytes(plaintext)
    except (
        InvalidTag,
        binascii.Error,
        ValueError,
        TypeError,
        PromptTemplateDefinitionUnavailable,
    ):
        raise PromptTemplateDefinitionUnavailable() from None


def encrypt_prompt_template_definition(
    payload: object,
    *,
    encryption_secret: object,
    tenant_id: object,
    prompt_template_id: object,
    prompt_template_version_id: object,
    version_number: object,
) -> str:
    """兼容 Control/Gateway 的 canonical-bytes 加密入口。

    参数：严格 canonical definition bytes、独立根密钥和四维 owner。返回：版本化密文。
    入口先重新解析 definition，防止服务层把任意 bytes 当成已验证 Prompt 加密后落库。
    """

    definition = parse_definition_bytes(payload)
    owner = PromptTemplateOwner(
        tenant_id=tenant_id if type(tenant_id) is str else "",
        prompt_template_id=(prompt_template_id if type(prompt_template_id) is str else ""),
        prompt_template_version_id=(
            prompt_template_version_id
            if type(prompt_template_version_id) is str
            else ""
        ),
        version_number=version_number if type(version_number) is int else 0,
    )
    # 兼容入口仍复用同一 owner AAD 和独立密钥域，不能退回无 AAD 的通用 secret helper。
    return encrypt_definition(definition, secret=encryption_secret, owner=owner)


def decrypt_prompt_template_definition(
    encrypted: object,
    *,
    encryption_secret: object,
    tenant_id: object,
    prompt_template_id: object,
    prompt_template_version_id: object,
    version_number: object,
) -> bytes:
    """兼容 Control/Gateway 的 canonical-bytes 解密入口。

    参数：持久密文、独立根密钥和可信四维 owner。返回：重新校验后的 canonical bytes。
    调用方若需要 DTO 应继续调用 ``definition_from_canonical_bytes``，不能跳过 schema 复核。
    """

    owner = PromptTemplateOwner(
        tenant_id=tenant_id if type(tenant_id) is str else "",
        prompt_template_id=(prompt_template_id if type(prompt_template_id) is str else ""),
        prompt_template_version_id=(
            prompt_template_version_id
            if type(prompt_template_version_id) is str
            else ""
        ),
        version_number=version_number if type(version_number) is int else 0,
    )
    definition = decrypt_definition(encrypted, secret=encryption_secret, owner=owner)
    return canonical_definition_bytes(definition)


def prompt_template_intent_hmac(secret: object, intent: object) -> str:
    """计算管理写入规范意图的分域 keyed HMAC。

    参数：独立模板根密钥和只含 JSON 基础类型的完整规范意图。返回：64 位小写十六进制摘要。
    此摘要用于幂等比较，不是正文 hash；调用方必须覆盖操作、actor、CAS、定义和有序绑定，且不得
    把 canonical bytes 或摘要解释为可供审计展示的 Prompt 指纹。
    """

    canonical = _canonical_json_bytes(_validated_json_value(intent))
    key = _derived_key(secret, _IDEMPOTENCY_DOMAIN)
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def verify_prompt_template_intent_hmac(
    secret: object,
    intent: object,
    digest: object,
) -> bool:
    """以常量时间复核一次模板管理意图摘要。

    参数：根密钥、待复核完整意图和数据库摘要。返回：仅在格式及摘要同时匹配时为真。
    摘要格式错误直接返回假，不能用普通字符串比较形成可观察时序差异。
    """

    if type(digest) is not str or _HEX_SHA256.fullmatch(digest) is None:
        return False
    expected = prompt_template_intent_hmac(secret, intent)
    return hmac.compare_digest(expected, digest)


def change_summary_hmac(secret: object, change_summary: object) -> str:
    """计算变更说明的独立分域 HMAC，供 Audit/Event 关联而不泄露明文。

    参数：模板根密钥与通过同一长度/控制字符校验的变更说明。返回：64 位小写摘要。
    明文仍只能放入加密 definition；数据库、审计和 Webhook 只能保存本结果。
    """

    normalized = _validate_change_summary(change_summary)
    key = _derived_key(secret, _CHANGE_SUMMARY_DOMAIN)
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def _validate_variable_definition(value: object) -> PromptVariableDefinition:
    """校验单个变量 exact-set 与严格类型，拒绝原型保留键和隐式数值转换。"""

    if isinstance(value, PromptVariableDefinition):
        candidate: Mapping[str, object] = {
            "name": value.name,
            "label": value.label,
            "required": value.required,
            "max_chars": value.max_chars,
        }
    elif type(value) is dict:
        candidate = value
    else:
        # 任意对象/Mapping 可能执行用户代码或隐式转换，只允许冻结 DTO 与普通 dict。
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    # 变量 schema 必须 exact-set，未知字段不能在 HMAC/加密前被静默丢弃。
    if set(candidate) != {"name", "label", "required", "max_chars"}:
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    name = candidate["name"]
    label = candidate["label"]
    required = candidate["required"]
    max_chars = candidate["max_chars"]
    if (
        type(name) is not str
        or _VARIABLE_NAME.fullmatch(name) is None
        or name in _RESERVED_VARIABLE_NAMES
        or type(label) is not str
        or not 1 <= len(label) <= 80
        or _has_control_character(label)
        or type(required) is not bool
        or type(max_chars) is not int
        or not 1 <= max_chars <= 8_000
    ):
        # 名称、展示标签、必填和长度共同冻结调用合同；失败时绝不回显候选值。
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    return PromptVariableDefinition(
        name=name,
        label=label,
        required=required,
        max_chars=max_chars,
    )


def _validate_change_summary(value: object) -> str:
    """校验变更说明字符、UTF-8 字节与控制字符边界，不做 trim 或内容改写。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 300
        or not value.strip()
        or _has_control_character(value)
        or _utf8_size(value) > MAX_CHANGE_SUMMARY_BYTES
    ):
        # 变更说明会被加密和摘要，必须先限制字符/字节并阻断控制字符注入。
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    return value


def _extract_placeholders(content: str) -> set[str]:
    """用标准库 Template 语法表提取 braced 占位符，并拒绝其余全部 ``$`` 形式。"""

    template = Template(content)
    # 标准库判定无效时直接拒绝，不能尝试自定义修复或部分替换敏感正文。
    if not template.is_valid():
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    names: set[str] = set()
    for match in Template.pattern.finditer(content):
        name = match.group("braced")
        # ``$$``、``$name`` 和无效 token 即便标准库能解释，也超出 PRD-067 受限语法。
        if (
            name is None
            or match.group("escaped") is not None
            or match.group("named") is not None
            or _VARIABLE_NAME.fullmatch(name) is None
            or name in _RESERVED_VARIABLE_NAMES
            or match.group(0) != "${" + name + "}"
        ):
            # 只允许完整 braced lower_snake_case，阻断表达式、转义和保留原型键。
            raise PromptTemplateContractError("prompt_template.definition_invalid")
        names.add(name)
    return names


def _owner_aad(owner: PromptTemplateOwner) -> bytes:
    """从可信 owner 构造冻结的六项 canonical AAD，拒绝空 ID、布尔版本号和控制字符。"""

    if (
        not isinstance(owner, PromptTemplateOwner)
        or any(
            type(value) is not str or not value or _has_control_character(value)
            for value in (
                owner.tenant_id,
                owner.prompt_template_id,
                owner.prompt_template_version_id,
            )
        )
        or type(owner.version_number) is not int
        or owner.version_number < 1
    ):
        # AAD owner 任一维度无效都按密文不可用处理，不能降级为缺省 tenant 或无绑定解密。
        raise PromptTemplateDefinitionUnavailable()
    return _canonical_json_bytes(
        [
            PROMPT_TEMPLATE_SCHEMA_VERSION,
            owner.tenant_id,
            owner.prompt_template_id,
            owner.prompt_template_version_id,
            owner.version_number,
            PROMPT_TEMPLATE_KEY_VERSION,
        ]
    )


def _derived_key(secret: object, domain: bytes) -> bytes:
    """从独立根密钥按固定 domain 派生 256-bit key，不允许空值或短密钥回退。"""

    # 缺失或非字符串根密钥属于部署不可用，不能采用空值、repr 或 Auth 密钥回退。
    if type(secret) is not str:
        raise PromptTemplateSecretUnavailable()
    try:
        secret_bytes = secret.encode("utf-8")
    # 非法 Unicode 密钥不能产生跨进程稳定派生结果，统一视为部署密钥不可用。
    except UnicodeEncodeError:
        raise PromptTemplateSecretUnavailable() from None
    # 至少 32 字节是本域硬门槛，短密钥不能延迟到首次数据解密才暴露。
    if len(secret_bytes) < 32:
        raise PromptTemplateSecretUnavailable()
    # 先派生用途 key，再由 AES/HMAC 使用；domain 不与业务正文拼接，避免跨用途摘要复用。
    return hmac.new(secret_bytes, domain, hashlib.sha256).digest()


def _validated_json_value(value: object, *, depth: int = 0) -> Any:
    """递归复制幂等意图允许的 JSON 基础类型，拒绝浮点、非字符串键和过深结构。"""

    # 深度门禁避免恶意嵌套在 canonical/HMAC 阶段耗尽栈或制造不同实现语义。
    if depth > 16:
        raise PromptTemplateContractError("prompt_template.definition_invalid")
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, (list, tuple)):
        return [_validated_json_value(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        # JSON object 只允许字符串键，禁止 Python 特有键类型进入跨进程幂等指纹。
        if any(type(key) is not str for key in value):
            raise PromptTemplateContractError("prompt_template.definition_invalid")
        return {
            key: _validated_json_value(value[key], depth=depth + 1)
            for key in sorted(value)
        }
    # 浮点、bytes、自定义对象等非冻结 JSON 类型不能进入跨进程幂等指纹。
    raise PromptTemplateContractError("prompt_template.definition_invalid")


def _canonical_json_bytes(value: object) -> bytes:
    """使用排序键、紧凑分隔符和 UTF-8 生成跨进程稳定 JSON bytes。"""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    # 编码异常可能持有完整 Prompt/变量值，必须切断异常链并只保留固定合同错误。
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise PromptTemplateContractError("prompt_template.definition_invalid") from None


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """为 definition JSON 拒绝任意层重复键，防止解析器与 HMAC 对同一 wire 产生不同解释。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 任意层重复键会让解析器和 HMAC 看见不同意图，统一拒绝且不回显键名。
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _utf8_size(value: str, *, error_code: str = "prompt_template.definition_invalid") -> int:
    """返回字符串 UTF-8 字节数；孤立 surrogate 只映射为固定合同错误。"""

    try:
        return len(value.encode("utf-8"))
    # 非字符串或孤立代理字符不具稳定 wire 表示，异常链不能携带原始敏感值。
    except (AttributeError, UnicodeEncodeError):
        raise PromptTemplateContractError(error_code) from None


def _has_control_character(value: str) -> bool:
    """判断展示/审计文本是否含任意 Unicode 控制字符，避免日志与界面结构注入。"""

    return any(unicodedata.category(character) == "Cc" for character in value)


def _b64(value: bytes) -> str:
    """编码 URL-safe Base64；密文格式保留 padding 以获得唯一规范表示。"""

    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    """严格解码 URL-safe Base64，拒绝非法字符和非规范 padding。"""

    # 密文分段必须是非空字符串，禁止 bytes/对象的隐式转换形成替代表示。
    if type(value) is not str or not value:
        raise binascii.Error
    decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    # 回编码必须逐字节等于输入，拒绝多种 padding/字母表表示破坏密文规范性。
    if _b64(decoded) != value:
        raise binascii.Error
    return decoded
