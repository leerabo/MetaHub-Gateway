"""PRD-068 Policy ``mask`` 的有界输入脱敏执行器。

本模块只在内存副本中处理 Chat 用户文本和文本型 Embeddings 输入。它不记录原文、
命中片段、位置、可逆哈希或完整替换后正文；任何未知文本结构和资源越界都整体失败关闭，
避免“只脱敏前缀后继续发送”造成敏感数据泄露。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final, Literal

from fastapi import FastAPI

# capability 名称进入部署门禁和应用依赖，必须固定，不能随展示文案变化。
POLICY_MASK_CAPABILITY: Final = "policy-mask-v1"
# Chat 与 Embeddings 分别冻结结构上限；即使 JSON 基础解析允许更大数组，mask 命中后也必须
# 在复制和替换前完整证明输入位于本执行器可理解的有界子集。
MAX_CHAT_MESSAGES: Final = 100
MAX_CHAT_CONTENT_PARTS: Final = 100
MAX_EMBEDDING_INPUTS: Final = 2_048
# 最坏 Chat 可以有 100*100 个文本 part，因此总段数不能沿用较小的通用接口默认值。
MAX_REDACTION_TEXT_SEGMENTS: Final = MAX_CHAT_MESSAGES * MAX_CHAT_CONTENT_PARTS
# 字符总量上限保证必须完整扫描后才放行，不允许对超大正文只处理前缀。
MAX_REDACTION_TOTAL_CHARACTERS: Final = 20_000

# 只允许仓库当前明确识别且不含用户文本语义的媒体/工具 part 原样保留。未知 type 可能是
# 新版本新增的文本载体，旧 Gateway 必须失败关闭，不能因向前兼容而漏扫正文。
_NON_TEXT_PART_TYPES = frozenset(
    {"image_url", "input_audio", "audio", "file", "tool", "tool_call", "tool_result"}
)

# 三个模式都使用有界字符类，不使用回溯爆炸结构；顺序先处理身份证和邮箱，再处理手机号，
# 防止长数字或邮箱局部被较短模式抢先替换而留下可识别残片。
_CN_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}"
    r"(?![A-Za-z0-9-])"
)
_MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

RequestKind = Literal["chat", "embeddings"]


class PolicyMaskUnsupportedError(ValueError):
    """表示正文结构或资源边界无法被完整、安全地脱敏。

    异常正文必须保持固定，不得附加输入类型的 ``repr``、原始文本或命中内容；HTTP 层只把
    该异常映射为 ``gateway.policy_mask_unsupported``。
    """


@dataclass(slots=True)
class _RedactionBudget:
    """跟踪一次请求已完整遍历的文本段数和字符数。"""

    segments: int = 0
    characters: int = 0

    def claim(self, text: str) -> None:
        """登记一个文本段；超过任一硬上限时在替换和 Provider 调用前失败关闭。"""

        self.segments += 1
        self.characters += len(text)
        if (
            self.segments > MAX_REDACTION_TEXT_SEGMENTS
            or self.characters > MAX_REDACTION_TOTAL_CHARACTERS
        ):
            # 任一上限越界都丢弃整个副本，不能把已经替换的前缀误当成完整安全结果。
            raise PolicyMaskUnsupportedError("policy mask input exceeds safe limits")


def redact_policy_payload(
    request_kind: RequestKind,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """返回完成 copy-on-write 脱敏的新 payload。

    参数：``request_kind`` 只接受现有 Chat/Embeddings 两条入口，``payload`` 是通过基础
    JSON 校验的请求映射。返回：仅复制被允许文本路径的新映射。关键约束：原对象和所有
    非文本结构保持不变；不能完整理解时抛固定异常，绝不返回部分结果。
    """

    if type(payload) is not dict:
        # 顶层非 object 无法沿用 OpenAI-compatible 字段合同，必须在接触 Provider 前拒绝。
        raise PolicyMaskUnsupportedError("policy mask payload must be an object")
    budget = _RedactionBudget()
    if request_kind == "chat":
        return _redact_chat_payload(payload, budget)
    if request_kind == "embeddings":
        return _redact_embeddings_payload(payload, budget)
    # 未知入口可能包含本执行器不理解的新文本字段，默认拒绝防止版本升级后静默漏扫。
    raise PolicyMaskUnsupportedError("policy mask request kind is unsupported")


@lru_cache(maxsize=1)
def policy_mask_runtime_self_test() -> bool:
    """执行不含外部依赖的固定能力自检，供 Gateway readiness 决定是否声明 capability。"""

    try:
        source = {
            "model": "demo",
            "messages": [
                {
                    "role": "user",
                    "content": "13800138000 11010519491231002X a@example.com",
                }
            ],
        }
        redacted = redact_policy_payload("chat", source)
        content = redacted["messages"][0]["content"]
        embedding = redact_policy_payload(
            "embeddings",
            {"input": ["11010519491231002X", "a@example.com"]},
        )
        # 自检还必须证明未知潜在文本 part 会失败关闭；只验证成功样本无法发现宽松透传回归。
        unknown_failed_closed = False
        try:
            redact_policy_payload(
                "chat",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "future_text", "value": "13800138000"}],
                        }
                    ]
                },
            )
        # 未知文本 part 被预期拒绝才说明失败关闭有效；异常在自检内消化，不能泄露固定样本。
        except PolicyMaskUnsupportedError:
            unknown_failed_closed = True
        return (
            source["messages"][0]["content"]
            == "13800138000 11010519491231002X a@example.com"
            and content
            == "[MOBILE_REDACTED] [CN_ID_REDACTED] [EMAIL_REDACTED]"
            and embedding["input"]
            == ["[CN_ID_REDACTED]", "[EMAIL_REDACTED]"]
            and unknown_failed_closed
        )
    # readiness 只暴露布尔能力；自检异常类型和固定样本细节都不应进入健康响应。
    except (PolicyMaskUnsupportedError, KeyError, TypeError):
        return False


def install_policy_mask_runtime(app: FastAPI) -> None:
    """在 Gateway 启动装配期执行自检，并只在门禁开启且自检通过时声明 capability。

    参数：已由共享工厂创建且持有 ``AppConfig`` 的应用。返回：无。关键约束：失败只留下
    空 capability 集合，不能让健康响应误报，也不能因可选能力失败阻止旧非 mask 流量启动。
    """

    config = app.state.config
    if config.policy_mask_runtime_enabled and policy_mask_runtime_self_test():
        # 固定单元素元组避免请求期可变集合被其他模块篡改或加入内部部署信息。
        app.state.runtime_capabilities = (POLICY_MASK_CAPABILITY,)


def _redact_chat_payload(
    payload: dict[str, Any],
    budget: _RedactionBudget,
) -> dict[str, Any]:
    """只复制并脱敏 ``role=user`` 的受支持 Chat 文本路径。"""

    messages = payload.get("messages")
    if type(messages) is not list:
        # mask 已命中时缺失标准消息数组属于制品/正文不兼容，不能当作“没有文本”放行。
        raise PolicyMaskUnsupportedError("policy mask messages are unsupported")
    if len(messages) > MAX_CHAT_MESSAGES:
        # 消息数量必须在创建任何副本前校验，避免越界请求留下可误用的部分脱敏结果。
        raise PolicyMaskUnsupportedError("policy mask messages exceed safe limits")
    copied_messages: list[Any] = []
    for message in messages:
        if type(message) is not dict:
            # 非对象消息没有可信 role/content 边界，跳过会漏掉潜在用户正文。
            raise PolicyMaskUnsupportedError("policy mask message is unsupported")
        # System/Assistant 不是本 PRD 的处理范围，保留原引用避免无意改变提示语义。
        if message.get("role") != "user":
            copied_messages.append(message)
            continue
        copied_message = dict(message)
        copied_message["content"] = _redact_chat_content(message.get("content"), budget)
        copied_messages.append(copied_message)
    copied = dict(payload)
    copied["messages"] = copied_messages
    return copied


def _redact_chat_content(content: Any, budget: _RedactionBudget) -> Any:
    """处理用户消息字符串或一层文本 parts；工具、图片和音频结构保持原引用。"""

    if type(content) is str:
        return _redact_text(content, budget)
    if type(content) is not list:
        # 用户 content 不是字符串或一层 parts 时无法证明已经遍历全部文本位置。
        raise PolicyMaskUnsupportedError("policy mask user content is unsupported")
    if len(content) > MAX_CHAT_CONTENT_PARTS:
        # 单条消息的 part 数量独立受限，不能只依赖全局段数掩盖病态结构。
        raise PolicyMaskUnsupportedError("policy mask content parts exceed safe limits")
    copied_parts: list[Any] = []
    for part in content:
        if type(part) is str:
            copied_parts.append(_redact_text(part, budget))
            continue
        if type(part) is not dict:
            # parts 仅允许裸字符串或对象；嵌套数组等形态会导致部分遍历风险。
            raise PolicyMaskUnsupportedError("policy mask content part is unsupported")
        part_type = part.get("type")
        if part_type == "text" and type(part.get("text")) is str:
            copied_part = dict(part)
            copied_part["text"] = _redact_text(part["text"], budget)
            copied_parts.append(copied_part)
            continue
        # 任何看似文本但不属于精确 type=text+string 的结构都失败关闭；已知非文本媒体/工具不递归。
        if (
            "text" in part
            or part_type in {"input_text", "output_text"}
            or part_type not in _NON_TEXT_PART_TYPES
        ):
            # 新文本协议若未经审计就透传会绕过脱敏，因此宁可拒绝整次请求也不能部分扫描。
            raise PolicyMaskUnsupportedError("policy mask text part is unsupported")
        copied_parts.append(part)
    return copied_parts


def _redact_embeddings_payload(
    payload: dict[str, Any],
    budget: _RedactionBudget,
) -> dict[str, Any]:
    """复制并脱敏字符串或纯字符串数组 Embeddings 输入，数字 token 与混合数组一律拒绝。"""

    value = payload.get("input")
    if type(value) is str:
        redacted: Any = _redact_text(value, budget)
    elif (
        type(value) is list
        and len(value) <= MAX_EMBEDDING_INPUTS
        and all(type(item) is str for item in value)
    ):
        redacted = [_redact_text(item, budget) for item in value]
    else:
        # 数字 token 和混合/嵌套数组没有可逆文本语义，禁止猜测转换后继续发送。
        raise PolicyMaskUnsupportedError("policy mask embeddings input is unsupported")
    copied = dict(payload)
    copied["input"] = redacted
    return copied


def _redact_text(text: str, budget: _RedactionBudget) -> str:
    """按固定顺序替换一个文本段，且只向调用方返回脱敏结果。"""

    budget.claim(text)
    redacted = _CN_ID_PATTERN.sub("[CN_ID_REDACTED]", text)
    redacted = _EMAIL_PATTERN.sub("[EMAIL_REDACTED]", redacted)
    return _MOBILE_PATTERN.sub("[MOBILE_REDACTED]", redacted)


__all__ = [
    "MAX_CHAT_CONTENT_PARTS",
    "MAX_CHAT_MESSAGES",
    "MAX_EMBEDDING_INPUTS",
    "MAX_REDACTION_TEXT_SEGMENTS",
    "MAX_REDACTION_TOTAL_CHARACTERS",
    "POLICY_MASK_CAPABILITY",
    "PolicyMaskUnsupportedError",
    "install_policy_mask_runtime",
    "policy_mask_runtime_self_test",
    "redact_policy_payload",
]
