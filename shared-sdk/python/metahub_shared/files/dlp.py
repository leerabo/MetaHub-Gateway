"""文件抽取全文的 tenant DLP 冻结规则与有界扫描器。

数据库规则由 Control 按 tenant-first SQL 加载后转换为本模块 DTO。pattern 和正文关闭 repr，
扫描结果只含规则 ID、动作、不同命中规则数和规则集 fingerprint，不含位置或命中片段。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex

from .contracts import FileContractError

FileDlpAction = Literal["allow", "log_only", "block"]
FileDlpRuleType = Literal["keyword", "regex"]
FileDlpStatus = Literal["clean", "matched", "blocked"]

# regex 分窗限制单次回溯输入，重叠覆盖现有 512 code-point 最大 pattern 边界。
FILE_DLP_WINDOW_CODE_POINTS: Final[int] = 65_536
FILE_DLP_OVERLAP_CODE_POINTS: Final[int] = 512
MAX_FILE_DLP_RULES: Final[int] = 100


@dataclass(frozen=True, slots=True)
class FileDlpRule:
    """一次文件扫描内冻结的 tenant DLP 规则。

    pattern 关闭 repr 且只在内存 matcher 中存在；pattern_hash、updated_at 和 order 仅用于
    规则集 fingerprint/确定顺序，扫描结果不会复制这些敏感管理字段。
    """

    dlp_rule_id: str
    rule_type: FileDlpRuleType
    pattern: str = field(repr=False)
    pattern_hash: str = field(repr=False)
    action: FileDlpAction
    priority: int
    updated_at: str
    order: int


@dataclass(frozen=True, slots=True)
class FileDlpMatch:
    """一条不同规则的安全命中摘要，不含次数、位置、片段或 pattern。"""

    dlp_rule_id: str
    rule_type: FileDlpRuleType
    action: FileDlpAction


@dataclass(frozen=True, slots=True)
class FileDlpDecision:
    """完整文件 DLP 的稳定决策和安全持久化投影。"""

    status: FileDlpStatus
    action: FileDlpAction | None
    effective_dlp_rule_id: str | None
    matches: tuple[FileDlpMatch, ...]
    match_count: int
    ruleset_fingerprint: str


def scan_file_text(text: object, rules: object) -> FileDlpDecision:
    """扫描完整抽取文本并按 block 优先生成决策。

    参数：已受抽取上限保护的正文和 tenant 冻结规则序列。返回：不含正文的稳定决策。
    关键约束：每条规则最多计一次；规则损坏失败关闭，不能跳过后把文件标为 clean。
    """

    if type(text) is not str or not isinstance(rules, Sequence):
        # 正文与冻结规则快照必须由服务层明确构造，禁止隐式转换后绕过 tenant DLP。
        raise FileContractError("file.dlp_unavailable")
    normalized_rules = _validate_rules(rules)
    fingerprint = file_ruleset_fingerprint(normalized_rules)
    matched: list[FileDlpMatch] = []
    for rule in normalized_rules:
        if _rule_matches(rule, text):
            matched.append(
                FileDlpMatch(
                    dlp_rule_id=rule.dlp_rule_id,
                    rule_type=rule.rule_type,
                    action=rule.action,
                )
            )
    if not matched:
        return FileDlpDecision(
            status="clean",
            action=None,
            effective_dlp_rule_id=None,
            matches=(),
            match_count=0,
            ruleset_fingerprint=fingerprint,
        )
    blocking = next((item for item in matched if item.action == "block"), None)
    effective = blocking or matched[0]
    return FileDlpDecision(
        status="blocked" if blocking is not None else "matched",
        action=effective.action,
        effective_dlp_rule_id=effective.dlp_rule_id,
        matches=tuple(matched),
        match_count=len(matched),
        ruleset_fingerprint=fingerprint,
    )


def file_ruleset_fingerprint(rules: object) -> str:
    """计算不含 pattern 的稳定规则集 SHA-256。

    参数：已经按 SQL 权威顺序冻结的规则序列。返回：带算法前缀的摘要。pattern_hash 本身
    是控制面已有摘要，只参与 canonical bytes，不会出现在公开响应或事件中。
    """

    if not isinstance(rules, Sequence):
        # 指纹必须基于有序规则快照，任意 iterable 会造成重复遍历或顺序漂移。
        raise FileContractError("file.dlp_unavailable")
    normalized = _validate_rules(rules)
    payload = [
        {
            "dlp_rule_id": rule.dlp_rule_id,
            "rule_type": rule.rule_type,
            "pattern_hash": rule.pattern_hash,
            "action": rule.action,
            "priority": rule.priority,
            "updated_at": rule.updated_at,
            "order": rule.order,
        }
        for rule in normalized
    ]
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_rules(rules: Sequence[object]) -> tuple[FileDlpRule, ...]:
    """校验规则类型、数量、顺序和固定管理摘要。"""

    if len(rules) > MAX_FILE_DLP_RULES:
        # 单租户规则快照保持硬上限，避免一次文件上传触发无界扫描成本。
        raise FileContractError("file.dlp_unavailable")
    normalized: list[FileDlpRule] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(rules):
        if not isinstance(value, FileDlpRule):
            # 只接受关闭 pattern repr 的受信 DTO，数据库行或浏览器字典不能直接扫描。
            raise FileContractError("file.dlp_unavailable")
        if (
            not value.dlp_rule_id
            or value.dlp_rule_id in seen_ids
            or value.rule_type not in {"keyword", "regex"}
            or value.action not in {"allow", "log_only", "block"}
            or type(value.pattern) is not str
            or not value.pattern
            or type(value.pattern_hash) is not str
            or not value.pattern_hash
            or type(value.priority) is not int
            or not 1 <= value.priority <= 1000
            or type(value.updated_at) is not str
            or not value.updated_at
            or value.order != index
        ):
            # 顺序必须由 tenant-first SQL 一次冻结，调用方不能在扫描中途重排规则。
            raise FileContractError("file.dlp_unavailable")
        seen_ids.add(value.dlp_rule_id)
        normalized.append(value)
    return tuple(normalized)


def _rule_matches(rule: FileDlpRule, text: str) -> bool:
    """判断单条规则是否在全文任一位置命中，但最多返回一次。"""

    if rule.rule_type == "keyword":
        return rule.pattern in text
    try:
        compiled = compile_safe_dlp_regex(rule.pattern).compiled
        start = 0
        text_length = len(text)
        while start < text_length:
            end = min(text_length, start + FILE_DLP_WINDOW_CODE_POINTS)
            if compiled.search(text, start, end) is not None:
                return True
            if end == text_length:
                return False
            # 下一窗回退 512 code points，覆盖短规则跨边界拆分且不重复累计命中次数。
            start = end - FILE_DLP_OVERLAP_CODE_POINTS
        return False
    except UnsafeRegexError:
        # 正则原因码和 pattern 都不进入文件错误；规则损坏统一使预扫描失败关闭。
        raise FileContractError("file.dlp_unavailable") from None
    except Exception:
        raise FileContractError("file.dlp_unavailable") from None
