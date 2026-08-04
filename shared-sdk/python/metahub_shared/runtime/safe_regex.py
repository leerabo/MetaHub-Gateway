from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

# DLP 正则安全阈值和危险模式集中定义，阻止高风险表达式进入数据面扫描。
MAX_DLP_REGEX_LENGTH = 512

BACKREFERENCE_RE = re.compile(r"\\[1-9]|\(\?P=")
LOOKAROUND_RE = re.compile(r"\(\?(?:[=!]|<[=!])")
NESTED_QUANTIFIER_RE = re.compile(r"\((?:[^()\\]|\\.)*[*+](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})")
REPEATED_WILDCARD_RE = re.compile(r"\.\*.*\.\*")


@dataclass(frozen=True)
class SafeRegexResult:
    """安全正则编译结果。

    控制面保存 DLP 规则和数据面执行 DLP 检查都复用这个对象，避免两个服务对同一个
    pattern 得出不同安全结论。compiled 只在进程内使用，不写入数据库或日志。
    """

    pattern: str
    compiled: Pattern[str]


class UnsafeRegexError(ValueError):
    """DLP 正则安全检查失败。

    reason_code 面向内部错误映射使用，不能包含用户提交的 pattern，避免异常被日志捕获时带出敏感规则内容。
    """

    def __init__(self, reason_code: str) -> None:
        """创建正则安全错误，并只保留安全原因码。"""

        self.reason_code = reason_code
        super().__init__(reason_code)


def compile_safe_dlp_regex(pattern: str) -> SafeRegexResult:
    """编译 DLP 正则并执行 M0 热路径安全检查。

    这不是完整形式化 ReDoS 证明，但会拦截 M0 最容易拖垮数据面热路径的模式：超长表达式、
    反向引用、前后向断言、嵌套量词和多段贪婪通配。后续如果引入 RE2，可在这里替换实现。
    """

    normalized = str(pattern or "")
    if not normalized or len(normalized) > MAX_DLP_REGEX_LENGTH:
        # 正则长度直接影响保存校验和数据面匹配成本，超出上限必须拒绝。
        raise UnsafeRegexError("regex.length_invalid")
    if BACKREFERENCE_RE.search(normalized):
        # 反向引用可能导致指数级回溯，也会让规则行为难以解释，M0 不允许。
        raise UnsafeRegexError("regex.backreference_unsafe")
    if LOOKAROUND_RE.search(normalized):
        # 前后向断言可读性和性能边界都较差，先留到后续更强正则引擎再支持。
        raise UnsafeRegexError("regex.lookaround_unsafe")
    if NESTED_QUANTIFIER_RE.search(normalized):
        # 典型的 (a+)+、(.*){2,} 类嵌套量词是 ReDoS 高风险模式。
        raise UnsafeRegexError("regex.nested_quantifier_unsafe")
    if REPEATED_WILDCARD_RE.search(normalized):
        # 多段 .* 会扩大回溯空间，在 DLP 热路径中先要求管理员写更具体的模式。
        raise UnsafeRegexError("regex.wildcard_unsafe")
    try:
        compiled = re.compile(normalized)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except re.error as exc:
        # 编译错误统一映射为安全原因码，pattern 原文不进入异常消息。
        raise UnsafeRegexError("regex.compile_invalid") from exc
    return SafeRegexResult(pattern=normalized, compiled=compiled)
