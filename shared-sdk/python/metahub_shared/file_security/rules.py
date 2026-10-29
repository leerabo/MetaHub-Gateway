"""PRD-073 固定中英文 Prompt Injection 规则和可见文本检测。

规则是发布包静态代码，不接受请求级正则或阈值。输入文本只在调用栈内规范化和匹配，finding
证据最多保存有界规则 ID 与局部规范化摘要，不保存命中正文。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from .contracts import FindingCollector, FindingLocationKind, FindingSourceKind

# 单条规则使用有界通配，避免用户文本触发灾难性回溯；规则 ID 才进入 HMAC 证据。
_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "ignore_previous_instructions",
        re.compile(r"\b(?:ignore|disregard|override)\b.{0,80}\b(?:previous|prior|above)\b.{0,32}\binstructions?\b"),
    ),
    (
        "reveal_system_prompt",
        re.compile(r"\b(?:reveal|show|print|leak|expose)\b.{0,64}\b(?:system|developer)\b.{0,32}\bprompt\b"),
    ),
    (
        "change_role_or_policy",
        re.compile(r"\b(?:act|behave|respond)\b.{0,48}\b(?:as|like)\b.{0,48}\b(?:system|administrator|root)\b"),
    ),
    (
        "execute_external_command",
        re.compile(r"\b(?:execute|run|invoke)\b.{0,48}\b(?:shell|command|powershell|bash|tool)\b"),
    ),
    (
        "document_as_instruction",
        re.compile(r"\b(?:treat|use|follow)\b.{0,64}\b(?:document|file|text)\b.{0,48}\b(?:system|instruction|command)\b"),
    ),
    (
        "zh_ignore_previous",
        re.compile(r"(?:忽略|无视|覆盖|绕过).{0,40}(?:之前|以上|上文|先前).{0,24}(?:指令|提示词|要求)"),
    ),
    (
        "zh_reveal_system_prompt",
        re.compile(r"(?:泄露|显示|输出|打印|公开).{0,32}(?:系统|开发者).{0,24}(?:提示词|提示|指令)"),
    ),
    (
        "zh_change_role",
        re.compile(r"(?:扮演|切换|变成|作为).{0,24}(?:系统|管理员|超级用户|root)"),
    ),
    (
        "zh_execute_command",
        re.compile(r"(?:执行|运行|调用).{0,24}(?:命令|脚本|shell|工具)"),
    ),
    (
        "zh_document_as_instruction",
        re.compile(r"(?:把|将).{0,24}(?:文档|文件|内容).{0,24}(?:当作|视为).{0,24}(?:系统指令|命令|提示词)"),
    ),
)
_BOUNDARY_COLLISION = re.compile(r"metahub[_ -]?untrusted[_ -]?file[_ -]?(?:begin|end)", re.I)
_WHITESPACE = re.compile(r"\s+")
MAX_RULE_TEXT_CODE_POINTS: Final[int] = 4_000_000


@dataclass(frozen=True, slots=True)
class PromptRuleMatch:
    """只公开静态规则 ID 的匹配结果；命中正文从不成为字段。"""

    rule_id: str


def normalize_prompt_text(text: object) -> str:
    """对受限文本执行 NFKC、casefold 和空白折叠，不删除任意标点制造误匹配。"""

    if type(text) is not str or len(text) > MAX_RULE_TEXT_CODE_POINTS:
        # 上游抽取已有同级上限；再次校验防止内部调用绕过文件解析边界。
        return ""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def find_prompt_injection(text: object) -> tuple[PromptRuleMatch, ...]:
    """返回命中的固定规则 ID，按发布顺序去重且不保存任何正文片段。"""

    normalized = normalize_prompt_text(text)
    if not normalized:
        return ()
    matches: list[PromptRuleMatch] = []
    for rule_id, pattern in _RULES:
        if pattern.search(normalized) is not None:
            matches.append(PromptRuleMatch(rule_id=rule_id))
    if _BOUNDARY_COLLISION.search(normalized) is not None:
        matches.append(PromptRuleMatch(rule_id="boundary_collision"))
    return tuple(matches)


def add_prompt_findings(
    collector: FindingCollector,
    text: object,
    *,
    detector_code: str = "prompt.visible_instruction",
    source_kind: FindingSourceKind = "visible_text",
    location_kind: FindingLocationKind = "document",
    location_index: int | None = None,
    evidence_prefix: bytes = b"visible:",
) -> int:
    """把规则命中转换为 high/critical finding，并返回新增候选数。

    OCR 命中使用 critical，其余可见或隐藏结构命中使用 high。证据只包含静态规则 ID 和调用方
    固定域前缀，不包含文本；收集器仍负责去重和 256 条上限。
    """

    matches = find_prompt_injection(text)
    severity = "critical" if detector_code == "ocr.prompt_instruction" else "high"
    category = "ocr" if detector_code == "ocr.prompt_instruction" else "prompt_injection"
    for match in matches:
        collector.add(
            detector_code=detector_code,
            category=category,
            severity=severity,
            confidence_milli=980,
            source_kind=source_kind,
            location_kind=location_kind,
            location_index=location_index,
            evidence=evidence_prefix + match.rule_id.encode("ascii"),
        )
    return len(matches)


def normalized_text_set(values: Iterable[str]) -> frozenset[str]:
    """为 OCR/可见文本去重生成有界规范集合，空文本不会参与比较。"""

    normalized: set[str] = set()
    for value in values:
        candidate = normalize_prompt_text(value)
        if candidate:
            normalized.add(candidate)
    return frozenset(normalized)
