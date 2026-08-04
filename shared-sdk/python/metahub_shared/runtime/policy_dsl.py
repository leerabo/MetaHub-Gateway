"""PRD-044 受限策略 DSL 的编译与纯求值实现。

本模块只处理白名单词法、确定性 AST 和无副作用求值，绝不调用 ``eval``、动态导入、
数据库、网络或日志接口。控制面与后续数据面必须复用这里的 canonical 合同，避免同一策略
在保存、快照和运行阶段出现不同解释；超时始终以拒绝决策收敛，不能被调用方放宽为放行。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, NoReturn

# 这些常量共同构成 policy-dsl-v1 的跨版本兼容边界；修改任何一项都需要新 DSL 版本，
# 不能静默改变已经持久化的 AST、hash 或运行语义。
DSL_VERSION = "policy-dsl-v1"
POLICY_DRAFT_FINGERPRINT_PREFIX = b"policy-dsl-v1\x00"
ALLOWED_VARIABLES = (
    "tenant",
    "env",
    "project",
    "model",
    "data_class",
    "budget_remaining",
)
STRING_VARIABLES = frozenset({"tenant", "env", "project", "model", "data_class"})
DECIMAL_VARIABLE = "budget_remaining"
ALLOWED_ACTIONS = frozenset({"allow", "deny", "mask", "log_only", "route_to"})
MAX_SOURCE_CODEPOINTS = 1_024
MAX_SOURCE_UTF8_BYTES = 2_048
MAX_TOKENS = 128
MAX_AST_NODES = 64
MAX_AST_DEPTH = 8
MAX_COMPARISONS = 16
MAX_PARENTHESIS_DEPTH = 3
MAX_NOT_DEPTH = 1
MAX_LIST_ITEMS = 20
MAX_STRING_CODEPOINTS = 128
MAX_COMPARISON_EXPLANATIONS = 64
MAX_LOG_ONLY_EXPLANATIONS = 20
# mask 与 log_only 一样是非终止修饰动作；单独限制摘要数量，避免大量命中扩大响应和日志内存。
MAX_MASK_EXPLANATIONS = 20
MAX_POLICIES_PER_SET = 200
MIN_TIMEOUT_MS = 1
MAX_TIMEOUT_MS = 100

# 十进制词法拒绝指数、前导加号和多余前导零；先限制位数，再由 Decimal 做精确比较，
# 从源头避免 float 隐式舍入改变预算判断。
_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?\Z")
_ASCII_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEYWORD_KINDS = {
    "and": "AND",
    "or": "OR",
    "not": "NOT",
    "in": "IN",
}
_OPERATOR_KINDS = {
    "==": "eq",
    "!=": "ne",
    "<": "lt",
    "<=": "lte",
    ">": "gt",
    ">=": "gte",
}
_DISPLAY_OPERATORS = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "in": "in",
    "not_in": "not in",
}
_PRECEDENCE = {"logical_or": 1, "logical_and": 2, "not": 3, "comparison": 4}
_STRING_CONTEXT_LIMITS = {
    "tenant": 128,
    "env": 64,
    "project": 128,
    "model": 128,
    "data_class": 64,
}

type AstNode = dict[str, Any]
PolicyActionType = Literal["allow", "deny", "mask", "log_only", "route_to"]
PolicyStatus = Literal["active", "disabled"]
type MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class PolicyDiagnostic:
    """表示一个不含输入片段的稳定 DSL 诊断。

    start/end 使用 Unicode code point 的零基、左闭右开位置；message 只能描述固定原因，
    不得拼接表达式、上下文值或底层异常，避免非法草稿扩散到事件和日志。
    """

    code: str
    field: str
    start: int | None
    end: int | None
    message: str

    def as_dict(self) -> dict[str, object]:
        """转换为控制面可直接序列化的安全诊断字典。"""

        return {
            "code": self.code,
            "field": self.field,
            "start": self.start,
            "end": self.end,
            "message": self.message,
        }


class PolicyDslError(ValueError):
    """表示表达式在词法、文法、类型或资源门限处被拒绝。

    对外领域错误固定为 ``policy.expression_invalid``，更细原因放在 diagnostics 中；异常
    本身不保存原始表达式，调用方因此可以安全地生成固定错误响应和失败事件。
    """

    code = "policy.expression_invalid"

    def __init__(self, diagnostic: PolicyDiagnostic) -> None:
        """保存单个确定性诊断，且不把不可信源码放入异常文本。"""

        super().__init__(diagnostic.message)
        self.diagnostics = (diagnostic,)
        self.diagnostic = diagnostic


class PolicyActionError(ValueError):
    """表示动作类型与 route target 组合不满足 DSL v1 合同。"""

    code = "policy.action_invalid"

    def __init__(self, message: str) -> None:
        """只接收固定安全消息，避免回显攻击者提交的动作正文。"""

        super().__init__(message)
        self.message = message


class PolicyContextError(ValueError):
    """表示调用方提供的已访问上下文字段类型或取值不合法。"""

    code = "policy.context_invalid"

    def __init__(self, variable: str, message: str) -> None:
        """记录白名单变量名和固定原因，不保存实际上下文值。"""

        super().__init__(message)
        self.variable = variable
        self.message = message


@dataclass(frozen=True, slots=True)
class CompiledPolicyExpression:
    """保存一次安全编译产生的稳定 AST、展示文本和 hash。

    ast 是 JSON-compatible 数据而不是可执行对象；condition_hash 只覆盖 canonical AST，
    action 则通过 ``policy_draft_fingerprint`` 另行纳入校验草稿指纹。
    """

    dsl_version: str
    ast: AstNode
    canonical_json: str
    canonical_expression: str
    variables: tuple[str, ...]
    condition_hash: str

    @property
    def canonical_ast(self) -> AstNode:
        """返回供持久化使用的规范 AST；调用方不得把它作为脚本执行。"""

        return self.ast

    @property
    def canonical_ast_json(self) -> str:
        """返回 key 排序且无多余空白的 UTF-8 canonical JSON 文本。"""

        return self.canonical_json


@dataclass(frozen=True, slots=True)
class ComparisonExplanation:
    """描述一个实际访问过的比较节点，不包含表达式或 AST 正文。"""

    variable: str
    operator: str
    value_type: str
    expected: str | tuple[str, ...]
    actual: str
    matched: bool

    def as_dict(self) -> dict[str, object]:
        """把不可变解释转换为 API 友好的 JSON-compatible 字典。"""

        expected: object = (
            list(self.expected) if isinstance(self.expected, tuple) else self.expected
        )
        return {
            "variable": self.variable,
            "operator": self.operator,
            "value_type": self.value_type,
            "expected": expected,
            "actual": self.actual,
            "matched": self.matched,
        }


@dataclass(frozen=True, slots=True)
class PolicyCheckExplanation:
    """汇总一条已完成策略的安全命中说明和实际短路比较。"""

    policy_id: str
    policy_rule_id: str
    policy_action_id: str
    priority: int
    condition_hash: str
    action_type: PolicyActionType
    matched: bool
    match_order: int | None
    comparisons: tuple[ComparisonExplanation, ...]

    def as_dict(self) -> dict[str, object]:
        """转换为不含 DSL、AST、Prompt 或 Provider 凭据的响应字典。"""

        return {
            "policy_id": self.policy_id,
            "policy_rule_id": self.policy_rule_id,
            "policy_action_id": self.policy_action_id,
            "priority": self.priority,
            "condition_hash": self.condition_hash,
            "action_type": self.action_type,
            "matched": self.matched,
            "match_order": self.match_order,
            "comparisons": [item.as_dict() for item in self.comparisons],
        }


@dataclass(frozen=True, slots=True)
class ExpressionEvaluationResult:
    """表示单个编译表达式的纯布尔求值结果。"""

    matched: bool
    reason_code: str
    comparisons: tuple[ComparisonExplanation, ...]
    explanation_truncated: bool
    timed_out: bool
    alert_required: bool
    elapsed_us: int
    missing_variable: str | None = None

    def as_dict(self) -> dict[str, object]:
        """生成控制面草稿校验可复用的安全结果字典。"""

        return {
            "matched": self.matched,
            "reason_code": self.reason_code,
            "comparisons": [item.as_dict() for item in self.comparisons],
            "explanation_truncated": self.explanation_truncated,
            "timed_out": self.timed_out,
            "alert_required": self.alert_required,
            "elapsed_us": self.elapsed_us,
            "missing_variable": self.missing_variable,
        }


@dataclass(frozen=True, slots=True)
class PolicyEvaluationInput:
    """描述一条可参与本地策略集求值的已编译策略。

    tenant_id 不属于该结构：调用方必须先按认证租户加载策略，并把真实 tenant 放入上下文；
    这样 shared 求值器保持纯函数，也不会提供绕过数据库租户隔离的入口。
    """

    policy_id: str
    policy_rule_id: str
    policy_action_id: str
    priority: int
    compiled_expression: CompiledPolicyExpression
    action_type: PolicyActionType
    target_model_id: str | None = None
    status: PolicyStatus = "active"


@dataclass(frozen=True, slots=True)
class PolicyEvaluationResult:
    """表示策略集的最终决定、非终止修饰摘要和超时告警要求。"""

    matched: bool
    decision: PolicyActionType
    reason_code: str
    target_model_id: str | None
    policy_reason: PolicyCheckExplanation | None
    policy_checks: tuple[PolicyCheckExplanation, ...]
    log_only_matches: tuple[PolicyCheckExplanation, ...]
    # 只保存稳定 Policy/Rule/Action ID 与条件 hash，不携带命中文本或请求正文。
    mask_matches: tuple[PolicyCheckExplanation, ...]
    mask_required: bool
    checked_policy_count: int
    explanation_truncated: bool
    timed_out: bool
    alert_required: bool
    elapsed_us: int
    missing_variable: str | None = None

    def as_dict(self) -> dict[str, object]:
        """生成不含原始表达式、AST 和敏感请求内容的响应字典。"""

        return {
            "matched": self.matched,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "target_model_id": self.target_model_id,
            "policy_reason": self.policy_reason.as_dict() if self.policy_reason else None,
            "policy_checks": [item.as_dict() for item in self.policy_checks],
            "log_only_matches": [item.as_dict() for item in self.log_only_matches],
            "mask_matches": [item.as_dict() for item in self.mask_matches],
            "mask_required": self.mask_required,
            "checked_policy_count": self.checked_policy_count,
            "explanation_truncated": self.explanation_truncated,
            "timed_out": self.timed_out,
            "alert_required": self.alert_required,
            "elapsed_us": self.elapsed_us,
            "missing_variable": self.missing_variable,
        }


@dataclass(frozen=True, slots=True)
class _Token:
    """保存词法 token 的类型、规范值及源码位置。"""

    kind: str
    value: str
    start: int
    end: int


class _EvaluationTimeout(Exception):
    """仅用于终止当前纯求值栈，外层必须转换为 fail-closed 结果。"""


class _ContextMissing(Exception):
    """表示短路求值实际访问了调用方未提供的白名单变量。"""

    def __init__(self, variable: str) -> None:
        """记录缺失的白名单变量名，且不携带任何上下文正文。"""

        super().__init__(variable)
        self.variable = variable


class _Deadline:
    """用可注入单调时钟维护覆盖整个策略集的唯一时间预算。"""

    def __init__(self, timeout_ms: int, monotonic: MonotonicClock) -> None:
        """校验 1-100 ms 硬边界并立即开始计时，禁止调用方无限放宽。"""

        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            # 时间预算是安全门限，类型含糊时必须在求值前明确拒绝。
            raise ValueError("timeout_ms must be an integer between 1 and 100")
        if not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS:
            # 超出设计门限会导致过早拒绝或放大 CPU 占用，因此不做隐式截断。
            raise ValueError("timeout_ms must be between 1 and 100")
        self._clock = monotonic
        self._budget_seconds = timeout_ms / 1_000
        started_at = float(monotonic())
        self._clock_invalid = not math.isfinite(started_at)
        self._started_at = started_at if not self._clock_invalid else 0.0
        self._last_elapsed = 0.0

    def check(self) -> None:
        """在策略和 AST 节点边界检查同一个 deadline，超时立即终止。"""

        current = float(self._clock())
        if self._clock_invalid or not math.isfinite(current):
            self._last_elapsed = self._budget_seconds
            # 时钟异常不能被解释成仍有预算，否则可能绕过 fail-closed 边界。
            raise _EvaluationTimeout
        # 即使底层时钟异常回拨，也保留已观察到的最大耗时，不能借回拨延长预算。
        self._last_elapsed = max(self._last_elapsed, 0.0, current - self._started_at)
        if self._last_elapsed >= self._budget_seconds:
            # 一旦达到全局预算就停止，不对下一条策略重新计时。
            raise _EvaluationTimeout

    @property
    def elapsed_us(self) -> int:
        """返回最近一次节点检查观察到的非负微秒数，不额外读取时钟。"""

        return max(0, int(self._last_elapsed * 1_000_000))


class _ExplanationCollector:
    """在不改变求值语义的前提下限制 comparison 解释数量。"""

    def __init__(self) -> None:
        """初始化全策略集共享的计数器和截断标记。"""

        self.retained_count = 0
        self.truncated = False

    def append(
        self,
        target: list[ComparisonExplanation],
        explanation: ComparisonExplanation,
    ) -> None:
        """保留前 64 条解释，超过后只标记截断并继续完整求值。"""

        if self.retained_count < MAX_COMPARISON_EXPLANATIONS:
            target.append(explanation)
            self.retained_count += 1
            return
        self.truncated = True


class _Parser:
    """按照 DSL v1 文法执行递归下降并在构建时规范化逻辑节点。"""

    def __init__(self, tokens: list[_Token], source_length: int) -> None:
        """接收已经过白名单扫描的 token，不保留原始表达式正文。"""

        self._tokens = tokens
        self._index = 0
        self._source_length = source_length
        self._parenthesis_depth = 0
        self._comparison_count = 0

    def parse(self) -> AstNode:
        """解析完整表达式，并拒绝任何未消费 token 或空输入。"""

        if self._current.kind == "EOF":
            _raise_dsl(
                "policy.dsl.empty",
                "Policy expression must not be empty.",
                start=0,
                end=0,
            )
        node = self._parse_or(not_depth=0)
        if self._current.kind != "EOF":
            _raise_at_token(
                "policy.dsl.syntax_invalid",
                "Unexpected token after the complete policy expression.",
                self._current,
            )
        return node

    def _parse_or(self, not_depth: int) -> AstNode:
        """解析最低优先级的 or 链并按输入顺序扁平相邻节点。"""

        node = self._parse_and(not_depth)
        while self._match("OR"):
            node = self._logical("or", node, self._parse_and(not_depth))
        return node

    def _parse_and(self, not_depth: int) -> AstNode:
        """解析高于 or 的 and 链并保留从左到右短路顺序。"""

        node = self._parse_unary(not_depth)
        while self._match("AND"):
            node = self._logical("and", node, self._parse_unary(not_depth))
        return node

    def _parse_unary(self, not_depth: int) -> AstNode:
        """解析 not、括号或比较，并分别限制 not 与括号嵌套深度。"""

        if self._current.kind == "NOT":
            token = self._current
            self._index += 1
            if not_depth >= MAX_NOT_DEPTH:
                _raise_at_token(
                    "policy.dsl.not_depth_exceeded",
                    "Policy expression exceeds the supported not nesting depth.",
                    token,
                )
            return {"kind": "not", "child": self._parse_unary(not_depth + 1)}

        if self._current.kind == "LPAREN":
            token = self._current
            if self._parenthesis_depth >= MAX_PARENTHESIS_DEPTH:
                _raise_at_token(
                    "policy.dsl.parenthesis_depth_exceeded",
                    "Policy expression exceeds the supported parenthesis depth.",
                    token,
                )
            self._index += 1
            self._parenthesis_depth += 1
            try:
                node = self._parse_or(not_depth)
                self._expect("RPAREN", "Expected a closing parenthesis.")
            finally:
                self._parenthesis_depth -= 1
            return node

        return self._parse_comparison()

    def _parse_comparison(self) -> AstNode:
        """解析一条变量比较并在 AST 生成前执行变量/操作符类型校验。"""

        variable_token = self._expect("VARIABLE", "Expected an allowed policy variable.")
        variable = variable_token.value
        operator_token = self._current

        # 操作符只按词法器的固定 token 类型分派，不能根据用户字符串动态查找函数。
        if operator_token.kind == "OPERATOR":
            self._index += 1
            operator = operator_token.value
            value = self._parse_scalar()
        # membership 单独要求 list，避免普通等号路径意外接收数组或嵌套对象。
        elif operator_token.kind == "IN":
            self._index += 1
            operator = "in"
            value = self._parse_list()
        # NOT 在变量后只能与 IN 成对；它不能在此处扩展为任意一元脚本操作。
        elif operator_token.kind == "NOT":
            self._index += 1
            self._expect("IN", "Only 'not in' is allowed after a policy variable.")
            operator = "not_in"
            value = self._parse_list()
        else:
            _raise_at_token(
                "policy.dsl.operator_invalid",
                "Expected an allowed comparison operator.",
                operator_token,
            )

        self._validate_comparison_type(variable, operator, value, operator_token)
        self._comparison_count += 1
        if self._comparison_count > MAX_COMPARISONS:
            _raise_at_token(
                "policy.dsl.comparison_limit_exceeded",
                "Policy expression contains too many comparisons.",
                variable_token,
            )
        return {
            "kind": "comparison",
            "variable": variable,
            "operator": operator,
            "value": value,
        }

    def _parse_list(self) -> AstNode:
        """解析 1-20 个同类标量组成的列表，并拒绝空表、嵌套和尾逗号。"""

        self._expect("LBRACKET", "Expected a list after the membership operator.")
        if self._current.kind == "RBRACKET":
            _raise_at_token(
                "policy.dsl.list_empty",
                "Membership lists must contain at least one value.",
                self._current,
            )

        items = [self._parse_scalar()]
        while self._match("COMMA"):
            if self._current.kind == "RBRACKET":
                _raise_at_token(
                    "policy.dsl.syntax_invalid",
                    "Trailing commas are not supported in policy lists.",
                    self._current,
                )
            items.append(self._parse_scalar())
            if len(items) > MAX_LIST_ITEMS:
                _raise_at_token(
                    "policy.dsl.list_limit_exceeded",
                    "Policy list contains too many values.",
                    self._current,
                )
        self._expect("RBRACKET", "Expected a closing list bracket.")

        first_kind = items[0]["kind"]
        if any(item["kind"] != first_kind for item in items[1:]):
            _raise_dsl(
                "policy.dsl.list_type_mismatch",
                "All values in a policy list must use the same scalar type.",
                start=None,
                end=None,
            )
        return {"kind": "list", "items": items}

    def _parse_scalar(self) -> AstNode:
        """把字符串或 Decimal token 转换为规范 literal 节点。"""

        token = self._current
        # 字符串 token 已在词法阶段完成 JSON 解码、NFC 和控制字符检查。
        if token.kind == "STRING":
            self._index += 1
            return {"kind": "string", "value": token.value}
        # Decimal token 再做 canonical 归一，确保等价值共享同一 AST/hash。
        if token.kind == "DECIMAL":
            self._index += 1
            return {"kind": "decimal", "value": _canonical_decimal(token.value)}
        _raise_at_token(
            "policy.dsl.scalar_invalid",
            "Expected a JSON string or plain decimal value.",
            token,
        )

    def _validate_comparison_type(
        self,
        variable: str,
        operator: str,
        value: AstNode,
        operator_token: _Token,
    ) -> None:
        """强制字符串变量和预算 Decimal 使用各自有限操作符集合。"""

        if variable in STRING_VARIABLES:
            allowed = {"eq", "ne", "in", "not_in"}
            expected_kind = "list" if operator in {"in", "not_in"} else "string"
            valid_list = value["kind"] != "list" or all(
                item["kind"] == "string" for item in value["items"]
            )
            if operator not in allowed or value["kind"] != expected_kind or not valid_list:
                _raise_at_token(
                    "policy.dsl.type_mismatch",
                    "String policy variables require string equality or string membership values.",
                    operator_token,
                )
            return

        allowed = {"eq", "ne", "lt", "lte", "gt", "gte"}
        if variable == DECIMAL_VARIABLE and operator in allowed and value["kind"] == "decimal":
            return
        _raise_at_token(
            "policy.dsl.type_mismatch",
            "budget_remaining requires a plain decimal comparison value.",
            operator_token,
        )

    def _expect(self, kind: str, message: str) -> _Token:
        """消费指定 token；不匹配时返回固定位置诊断而不回显 token 正文。"""

        token = self._current
        # 类型不符时只使用固定消息与位置，不能把 token 原文拼入异常或失败事件。
        if token.kind != kind:
            _raise_at_token("policy.dsl.syntax_invalid", message, token)
        self._index += 1
        # 返回值只含规范值和整数位置，调用方不会接触原始源码切片。
        return token

    def _match(self, kind: str) -> bool:
        """当前 token 类型匹配时消费并返回真，用于解析重复运算链。"""

        if self._current.kind != kind:
            return False
        self._index += 1
        return True

    @property
    def _current(self) -> _Token:
        """返回当前位置 token；词法器始终附加 EOF，因此不会越界。"""

        # EOF 哨兵由词法器无条件追加，解析器无需用异常路径探测数组边界。
        return self._tokens[self._index]

    @staticmethod
    def _logical(operator: str, left: AstNode, right: AstNode) -> AstNode:
        """扁平相邻同操作符 logical 节点，同时保留用户从左到右顺序。"""

        children: list[AstNode] = []
        for node in (left, right):
            if node.get("kind") == "logical" and node.get("operator") == operator:
                children.extend(node["children"])
            else:
                children.append(node)
        return {"kind": "logical", "operator": operator, "children": children}


def compile_policy_expression(source: str) -> CompiledPolicyExpression:
    """把不可信 DSL 源文本编译为稳定、不可执行的 canonical AST。

    参数必须是字符串，长度同时受 code point 与 UTF-8 bytes 限制；失败只抛
    ``PolicyDslError`` 固定诊断，不保存或回显原始表达式。返回值可由控制面持久化，后续
    求值只遍历白名单节点，不会执行 Python 代码。
    """

    if not isinstance(source, str):
        _raise_dsl(
            "policy.dsl.source_type_invalid",
            "Policy expression must be a string.",
            start=None,
            end=None,
        )
    if len(source) > MAX_SOURCE_CODEPOINTS:
        _raise_dsl(
            "policy.dsl.source_too_long",
            "Policy expression exceeds the code point limit.",
            start=MAX_SOURCE_CODEPOINTS,
            end=len(source),
        )
    try:
        encoded = source.encode("utf-8")
    # 非法代理字符无法形成稳定 UTF-8 canonical JSON，必须在词法解析前拒绝。
    except UnicodeEncodeError:
        _raise_dsl(
            "policy.dsl.unicode_invalid",
            "Policy expression contains invalid Unicode data.",
            start=None,
            end=None,
        )
    if len(encoded) > MAX_SOURCE_UTF8_BYTES:
        _raise_dsl(
            "policy.dsl.source_too_long",
            "Policy expression exceeds the UTF-8 byte limit.",
            start=None,
            end=None,
        )

    tokens = _tokenize(source)
    ast = _Parser(tokens, len(source)).parse()
    node_count, depth = _measure_ast(ast)
    if node_count > MAX_AST_NODES:
        _raise_dsl(
            "policy.dsl.node_limit_exceeded",
            "Policy expression produces too many AST nodes.",
            start=None,
            end=None,
        )
    if depth > MAX_AST_DEPTH:
        _raise_dsl(
            "policy.dsl.ast_depth_exceeded",
            "Policy expression produces an AST that is too deep.",
            start=None,
            end=None,
        )

    canonical_json = _canonical_json(ast)
    condition_hash = hashlib.sha256(
        DSL_VERSION.encode("ascii") + b"\x00" + canonical_json.encode("utf-8")
    ).hexdigest()
    return CompiledPolicyExpression(
        dsl_version=DSL_VERSION,
        ast=ast,
        canonical_json=canonical_json,
        canonical_expression=_render_expression(ast),
        variables=tuple(sorted(_collect_variables(ast))),
        condition_hash=condition_hash,
    )


def canonical_policy_action(action_type: str, target_model_id: str | None = None) -> str:
    """返回动作的稳定 canonical JSON，并严格校验 route target 组合。

    ``route_to`` 必须携带非空目标，其余四种动作必须没有目标；这里只验证结构和字符边界，
    目标是否属于当前租户且 active 仍须由控制面在写事务内校验。
    """

    if action_type not in ALLOWED_ACTIONS:
        # 动作白名单是脚本注入边界，未知值不能被透传到事件或求值分支。
        raise PolicyActionError("Policy action type is not supported.")
    if action_type == "route_to":
        if not isinstance(target_model_id, str) or not target_model_id:
            # 路由动作缺目标时不能退化成默认路由，否则管理员意图会被静默改变。
            raise PolicyActionError("route_to requires a target_model_id.")
        normalized_target = unicodedata.normalize("NFC", target_model_id)
        if len(normalized_target) > 128 or _contains_forbidden_string_character(
            normalized_target
        ):
            # 目标只作为模型 ID 使用，控制字符和无界文本会污染数据库与解释响应。
            raise PolicyActionError("target_model_id is not a valid bounded identifier.")
    else:
        if target_model_id is not None:
            # 非路由动作携带目标通常意味着客户端状态残留，保存时必须明确拒绝。
            raise PolicyActionError("Only route_to may include target_model_id.")
        normalized_target = None

    payload = {"target_model_id": normalized_target, "type": action_type}
    return _canonical_json(payload)


def policy_draft_fingerprint(
    compiled: CompiledPolicyExpression,
    action_type: str,
    target_model_id: str | None = None,
) -> str:
    """按设计合同计算 canonical AST 与动作共同形成的草稿指纹。

    指纹不覆盖非法原文，只有编译和动作校验都成功后才能生成；它用于关联安全摘要，
    不能替代保存接口重新编译，也不能当作权限或租户归属凭证。
    """

    if not isinstance(compiled, CompiledPolicyExpression) or compiled.dsl_version != DSL_VERSION:
        # 版本不明的 AST 不能套用 v1 指纹前缀，否则未来升级会产生错误等价关系。
        raise ValueError("compiled expression must use policy-dsl-v1")
    _validate_compiled_expression(compiled)
    action_json = canonical_policy_action(action_type, target_model_id)
    digest_input = (
        POLICY_DRAFT_FINGERPRINT_PREFIX
        + compiled.canonical_json.encode("utf-8")
        + b"\x00"
        + action_json.encode("utf-8")
    )
    return hashlib.sha256(digest_input).hexdigest()


def evaluate_compiled_expression(
    compiled: CompiledPolicyExpression,
    context: Mapping[str, object],
    *,
    timeout_ms: int = 5,
    monotonic: MonotonicClock = time.monotonic,
) -> ExpressionEvaluationResult:
    """在单一全局时间预算内纯求值一个编译表达式。

    缺失变量只在短路路径实际访问时返回 ``policy.context_missing``；类型非法则抛
    ``PolicyContextError`` 交由控制面形成领域诊断。超时不会泄露部分比较值，而是固定返回
    timed_out + alert_required，供调用方在不改变结果的前提下独立告警。
    """

    _validate_compiled_expression(compiled)
    if not isinstance(context, Mapping):
        # 上下文必须是显式白名单映射，禁止通过对象属性触发任意代码或反射访问。
        raise PolicyContextError("context", "Policy evaluation context must be a mapping.")
    deadline = _Deadline(timeout_ms, monotonic)
    collector = _ExplanationCollector()
    comparisons: list[ComparisonExplanation] = []
    try:
        deadline.check()
        matched = _evaluate_node(compiled.ast, context, deadline, collector, comparisons)
        deadline.check()
    except _EvaluationTimeout:
        return ExpressionEvaluationResult(
            matched=False,
            reason_code="policy.evaluation_timeout",
            comparisons=(),
            explanation_truncated=False,
            timed_out=True,
            alert_required=True,
            elapsed_us=deadline.elapsed_us,
        )
    except _ContextMissing as exc:
        return ExpressionEvaluationResult(
            matched=False,
            reason_code="policy.context_missing",
            comparisons=(),
            explanation_truncated=False,
            timed_out=False,
            alert_required=False,
            elapsed_us=deadline.elapsed_us,
            missing_variable=exc.variable,
        )

    reason_code = "policy.rule_matched" if matched else "policy.no_match_default_allow"
    return ExpressionEvaluationResult(
        matched=matched,
        reason_code=reason_code,
        comparisons=tuple(comparisons),
        explanation_truncated=collector.truncated,
        timed_out=False,
        alert_required=False,
        elapsed_us=deadline.elapsed_us,
    )


def evaluate_policy_set(
    policies: Iterable[PolicyEvaluationInput],
    context: Mapping[str, object],
    *,
    timeout_ms: int = 5,
    monotonic: MonotonicClock = time.monotonic,
) -> PolicyEvaluationResult:
    """按 priority、policy_id 稳定顺序纯求值 active 策略集。

    log_only 和 mask 命中只记录后继续，首个 allow/deny/route_to 立即决定；无决定性命中默认 allow。时间预算
    覆盖物化、排序、策略边界与所有访问的 AST 节点，超时和缺失上下文都固定拒绝，且不会
    返回此前的 allow、route_to 或部分 comparison 值。
    """

    if not isinstance(context, Mapping):
        # 禁止对象属性式上下文，确保 DSL 永远只能读取六个显式字段。
        raise PolicyContextError("context", "Policy evaluation context must be a mapping.")
    deadline = _Deadline(timeout_ms, monotonic)
    checked_count = 0
    collector = _ExplanationCollector()
    checks: list[PolicyCheckExplanation] = []
    log_only_matches: list[PolicyCheckExplanation] = []
    mask_matches: list[PolicyCheckExplanation] = []
    match_order = 0

    try:
        materialized: list[PolicyEvaluationInput] = []
        for policy in policies:
            # 逐条物化并检查 deadline，避免无限生成器在进入 AST 前无界占用内存。
            deadline.check()
            if len(materialized) >= MAX_POLICIES_PER_SET:
                # 求值器复用每租户 200 条业务配额，离线调用也不能绕过该资源上限。
                raise ValueError("policy set exceeds the supported 200 policy limit")
            _validate_policy_input(policy)
            materialized.append(policy)
        active = sorted(
            (policy for policy in materialized if policy.status == "active"),
            key=lambda item: (item.priority, item.policy_id),
        )
        deadline.check()

        for policy in active:
            # 每条策略共享同一个 deadline，不能把 5 ms 误实现成逐策略预算。
            deadline.check()
            local_comparisons: list[ComparisonExplanation] = []
            matched = _evaluate_node(
                policy.compiled_expression.ast,
                context,
                deadline,
                collector,
                local_comparisons,
            )
            checked_count += 1
            current_order: int | None = None
            if matched:
                match_order += 1
                current_order = match_order
            check = PolicyCheckExplanation(
                policy_id=policy.policy_id,
                policy_rule_id=policy.policy_rule_id,
                policy_action_id=policy.policy_action_id,
                priority=policy.priority,
                condition_hash=policy.compiled_expression.condition_hash,
                action_type=policy.action_type,
                matched=matched,
                match_order=current_order,
                comparisons=tuple(local_comparisons),
            )
            checks.append(check)

            if not matched:
                continue
            if policy.action_type == "log_only":
                if len(log_only_matches) < MAX_LOG_ONLY_EXPLANATIONS:
                    log_only_matches.append(check)
                else:
                    collector.truncated = True
                continue
            if policy.action_type == "mask":
                # mask 必须参与同一次稳定扫描，不能另起第二遍求值，否则超时、缺失上下文和解释顺序会分叉。
                if len(mask_matches) < MAX_MASK_EXPLANATIONS:
                    mask_matches.append(check)
                else:
                    collector.truncated = True
                continue

            deadline.check()
            return PolicyEvaluationResult(
                matched=True,
                decision=policy.action_type,
                reason_code="policy.rule_matched",
                target_model_id=policy.target_model_id,
                policy_reason=check,
                policy_checks=tuple(checks),
                log_only_matches=tuple(log_only_matches),
                mask_matches=tuple(mask_matches),
                mask_required=bool(mask_matches),
                checked_policy_count=checked_count,
                explanation_truncated=collector.truncated,
                timed_out=False,
                alert_required=False,
                elapsed_us=deadline.elapsed_us,
            )

        deadline.check()
    except _EvaluationTimeout:
        return _fail_closed_policy_result(
            reason_code="policy.evaluation_timeout",
            checked_policy_count=checked_count,
            elapsed_us=deadline.elapsed_us,
            timed_out=True,
            alert_required=True,
        )
    except _ContextMissing as exc:
        return _fail_closed_policy_result(
            reason_code="policy.context_missing",
            checked_policy_count=checked_count,
            elapsed_us=deadline.elapsed_us,
            timed_out=False,
            alert_required=False,
            missing_variable=exc.variable,
        )

    return PolicyEvaluationResult(
        matched=False,
        decision="allow",
        reason_code="policy.no_match_default_allow",
        target_model_id=None,
        policy_reason=None,
        policy_checks=tuple(checks),
        log_only_matches=tuple(log_only_matches),
        mask_matches=tuple(mask_matches),
        mask_required=bool(mask_matches),
        checked_policy_count=checked_count,
        explanation_truncated=collector.truncated,
        timed_out=False,
        alert_required=False,
        elapsed_us=deadline.elapsed_us,
    )


def _tokenize(source: str) -> list[_Token]:
    """逐字符白名单扫描源码，拒绝属性、函数、脚本符号和未知标识符。"""

    tokens: list[_Token] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character in " \t\r\n":
            index += 1
            continue
        if character == "\x00":
            _raise_dsl(
                "policy.dsl.nul_forbidden",
                "NUL is not allowed in policy expressions.",
                start=index,
                end=index + 1,
            )
        if character == '"':
            token, index = _read_string_token(source, index)
            tokens.append(token)
        elif character == "-" or character.isascii() and character.isdigit():
            token, index = _read_decimal_token(source, index)
            tokens.append(token)
        elif character.isascii() and (character.isalpha() or character == "_"):
            token, index = _read_identifier_token(source, index)
            tokens.append(token)
        elif source.startswith(("==", "!=", "<=", ">="), index):
            raw = source[index : index + 2]
            tokens.append(_Token("OPERATOR", _OPERATOR_KINDS[raw], index, index + 2))
            index += 2
        elif character in "<>":
            tokens.append(_Token("OPERATOR", _OPERATOR_KINDS[character], index, index + 1))
            index += 1
        elif character in "()[],":
            kind = {
                "(": "LPAREN",
                ")": "RPAREN",
                "[": "LBRACKET",
                "]": "RBRACKET",
                ",": "COMMA",
            }[character]
            tokens.append(_Token(kind, character, index, index + 1))
            index += 1
        else:
            _raise_dsl(
                "policy.dsl.character_forbidden",
                "Policy expression contains a character outside the DSL whitelist.",
                start=index,
                end=index + 1,
            )

        # token 上限在解析前持续执行，恶意长链不会先构建无界 AST。
        if len(tokens) > MAX_TOKENS:
            _raise_dsl(
                "policy.dsl.token_limit_exceeded",
                "Policy expression contains too many tokens.",
                start=tokens[-1].start,
                end=tokens[-1].end,
            )

    tokens.append(_Token("EOF", "", len(source), len(source)))
    # EOF 不计入 128 个用户 token，只用于让递归下降安全判断输入结束。
    return tokens


def _read_string_token(source: str, start: int) -> tuple[_Token, int]:
    """读取 JSON 双引号字符串，并在解码后执行 NFC、长度和控制字符检查。"""

    index = start + 1
    escaped = False
    while index < len(source):
        character = source[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\":
            escaped = True
            index += 1
            continue
        if character == '"':
            end = index + 1
            raw = source[start:end]
            try:
                decoded = json.loads(raw)
            # JSON 解码错误只能映射为固定诊断，不能把包含源码片段的原异常向外传播。
            except (json.JSONDecodeError, UnicodeDecodeError):
                _raise_dsl(
                    "policy.dsl.string_invalid",
                    "Policy string must use valid JSON double-quoted syntax.",
                    start=start,
                    end=end,
                )
            if not isinstance(decoded, str):
                _raise_dsl(
                    "policy.dsl.string_invalid",
                    "Policy string must decode to text.",
                    start=start,
                    end=end,
                )
            normalized = unicodedata.normalize("NFC", decoded)
            if len(normalized) > MAX_STRING_CODEPOINTS:
                _raise_dsl(
                    "policy.dsl.string_too_long",
                    "Policy string exceeds the supported length.",
                    start=start,
                    end=end,
                )
            if _contains_forbidden_string_character(normalized):
                _raise_dsl(
                    "policy.dsl.string_control_forbidden",
                    "Policy strings cannot contain control or invalid Unicode characters.",
                    start=start,
                    end=end,
                )
            if _contains_template_marker(normalized):
                _raise_dsl(
                    "policy.dsl.template_forbidden",
                    "Policy strings cannot contain template or interpolation markers.",
                    start=start,
                    end=end,
                )
            # token 只保存解码后的规范常量和位置，不保留可能含转义的原始片段。
            return _Token("STRING", normalized, start, end), end
        if ord(character) < 0x20:
            _raise_dsl(
                "policy.dsl.string_invalid",
                "Policy string must use valid JSON double-quoted syntax.",
                start=start,
                end=index + 1,
            )
        index += 1

    _raise_dsl(
        "policy.dsl.string_unterminated",
        "Policy string is missing a closing quote.",
        start=start,
        end=len(source),
    )


def _read_decimal_token(source: str, start: int) -> tuple[_Token, int]:
    """读取普通十进制 token，并拒绝指数、超长位数和不完整小数。"""

    index = start
    if source[index] == "-":
        index += 1
        if index >= len(source) or not source[index].isascii() or not source[index].isdigit():
            _raise_dsl(
                "policy.dsl.decimal_invalid",
                "Decimal values must use plain base-10 syntax.",
                start=start,
                end=min(len(source), index + 1),
            )
    while index < len(source) and source[index].isascii() and source[index].isdigit():
        index += 1
    if index < len(source) and source[index] == ".":
        index += 1
        fraction_start = index
        while index < len(source) and source[index].isascii() and source[index].isdigit():
            index += 1
        if fraction_start == index:
            _raise_dsl(
                "policy.dsl.decimal_invalid",
                "Decimal fractions must contain digits after the decimal point.",
                start=start,
                end=index,
            )

    raw = source[start:index]
    if not _DECIMAL_RE.fullmatch(raw):
        _raise_dsl(
            "policy.dsl.decimal_invalid",
            "Decimal value exceeds the supported precision or syntax.",
            start=start,
            end=index,
        )
    # Decimal token 的 raw 已通过完整正则，后续 canonical 化不会接受指数或非有限值。
    return _Token("DECIMAL", raw, start, index), index


def _read_identifier_token(source: str, start: int) -> tuple[_Token, int]:
    """读取 ASCII 标识符，只接受六变量和四个固定关键字。"""

    match = _ASCII_IDENTIFIER_RE.match(source, start)
    if match is None:
        _raise_dsl(
            "policy.dsl.identifier_invalid",
            "Policy identifiers must use the ASCII DSL whitelist.",
            start=start,
            end=start + 1,
        )
    raw = match.group(0)
    end = match.end()
    # 变量 token 只能来自六项白名单，禁止动态 metadata 路径和属性访问。
    if raw in ALLOWED_VARIABLES:
        return _Token("VARIABLE", raw, start, end), end
    # 关键字 token 只承担固定文法作用，不映射到 Python 名称或可调用对象。
    if raw in _KEYWORD_KINDS:
        return _Token(_KEYWORD_KINDS[raw], raw, start, end), end
    _raise_dsl(
        "policy.dsl.identifier_unknown",
        "Policy expression contains an unknown identifier.",
        start=start,
        end=end,
    )


def _canonical_decimal(raw: str) -> str:
    """把合法普通十进制文本归一为无指数、无尾零且无负零的字符串。"""

    try:
        value = Decimal(raw)
    # 词法器已经限制格式；此处仍收敛 Decimal 异常，防止未来调用路径绕过词法门禁。
    except InvalidOperation:
        _raise_dsl(
            "policy.dsl.decimal_invalid",
            "Decimal value is not valid.",
            start=None,
            end=None,
        )
    if not value.is_finite():
        _raise_dsl(
            "policy.dsl.decimal_invalid",
            "Decimal value must be finite.",
            start=None,
            end=None,
        )
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _measure_ast(node: AstNode) -> tuple[int, int]:
    """按合同统计 AST 节点总数与最大深度，括号本身不产生节点。"""

    kind = node["kind"]
    if kind in {"string", "decimal"}:
        return 1, 1
    if kind == "list":
        child_measures = [_measure_ast(item) for item in node["items"]]
    elif kind == "comparison":
        child_measures = [_measure_ast(node["value"])]
    elif kind == "not":
        child_measures = [_measure_ast(node["child"])]
    elif kind == "logical":
        child_measures = [_measure_ast(item) for item in node["children"]]
    else:
        # 编译器只应产生白名单节点；未知 kind 表示内部合同损坏，不能继续计算 hash。
        raise ValueError("unknown policy AST node kind")
    return 1 + sum(count for count, _ in child_measures), 1 + max(
        depth for _, depth in child_measures
    )


def _collect_variables(node: AstNode) -> set[str]:
    """遍历规范 AST，返回去重变量集合供持久化和 context 校验。"""

    kind = node["kind"]
    if kind == "comparison":
        return {node["variable"]}
    if kind == "not":
        return _collect_variables(node["child"])
    if kind == "logical":
        result: set[str] = set()
        for child in node["children"]:
            result.update(_collect_variables(child))
        return result
    return set()


def _canonical_json(payload: object) -> str:
    """按 key 升序、数组保序、NFC 值和无空白规则生成 canonical JSON。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _render_expression(node: AstNode, parent_precedence: int = 0) -> str:
    """仅从 AST 重新生成确定性展示表达式，并按优先级补最少括号。"""

    kind = node["kind"]
    if kind == "comparison":
        rendered = (
            f"{node['variable']} {_DISPLAY_OPERATORS[node['operator']]} "
            f"{_render_literal(node['value'])}"
        )
        precedence = _PRECEDENCE["comparison"]
    elif kind == "not":
        precedence = _PRECEDENCE["not"]
        rendered = f"not {_render_expression(node['child'], precedence)}"
    elif kind == "logical":
        operator = node["operator"]
        precedence = _PRECEDENCE[f"logical_{operator}"]
        rendered = f" {operator} ".join(
            _render_expression(child, precedence) for child in node["children"]
        )
    else:
        # canonical_expression 只能渲染编译器白名单节点，损坏 AST 必须快速失败。
        raise ValueError("unknown policy AST node kind")
    return f"({rendered})" if precedence < parent_precedence else rendered


def _render_literal(node: AstNode) -> str:
    """把规范 string、decimal 或 list 节点渲染为 DSL v1 字面量。"""

    kind = node["kind"]
    if kind == "string":
        return json.dumps(node["value"], ensure_ascii=False, separators=(",", ":"))
    if kind == "decimal":
        return node["value"]
    if kind == "list":
        return "[" + ", ".join(_render_literal(item) for item in node["items"]) + "]"
    # literal 渲染不接受对象或可执行结构，内部 AST 损坏时立即终止。
    raise ValueError("unknown policy literal node kind")


def _validate_compiled_expression(compiled: CompiledPolicyExpression) -> None:
    """确认求值输入来自当前 DSL 版本且 canonical/hash 尚未被调用方篡改。"""

    if not isinstance(compiled, CompiledPolicyExpression) or compiled.dsl_version != DSL_VERSION:
        # 未知版本不能沿用 v1 运行语义，必须等待显式升级或重新编译。
        raise ValueError("compiled expression must use policy-dsl-v1")
    canonical_json = _canonical_json(compiled.ast)
    expected_hash = hashlib.sha256(
        DSL_VERSION.encode("ascii") + b"\x00" + canonical_json.encode("utf-8")
    ).hexdigest()
    if canonical_json != compiled.canonical_json or expected_hash != compiled.condition_hash:
        # AST 与 hash 不一致表示持久化或内存数据损坏，不能继续做治理决策。
        raise ValueError("compiled policy expression failed integrity validation")


def _validate_policy_input(policy: PolicyEvaluationInput) -> None:
    """验证策略集输入结构，避免 bool priority、未知状态或非法动作改变排序语义。"""

    if not isinstance(policy, PolicyEvaluationInput):
        # 显式数据类可避免任意对象属性在求值时触发副作用。
        raise TypeError("policies must contain PolicyEvaluationInput values")
    for identifier in (policy.policy_id, policy.policy_rule_id, policy.policy_action_id):
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            # ID 只用于稳定排序和解释关联，必须保持有界且不可为空。
            raise ValueError("policy evaluation identifiers must be bounded strings")
    if isinstance(policy.priority, bool) or not isinstance(policy.priority, int):
        # Python bool 是 int 子类，若不显式拒绝会悄悄进入优先级排序。
        raise ValueError("policy priority must be an integer")
    if not 1 <= policy.priority <= 10_000:
        # 求值器沿用数据库优先级边界，避免离线输入和控制面产生不同顺序。
        raise ValueError("policy priority must be between 1 and 10000")
    if policy.status not in {"active", "disabled"}:
        # 未知状态不能默认当 active 或 disabled，防止配置漂移时误放行。
        raise ValueError("policy status must be active or disabled")
    _validate_compiled_expression(policy.compiled_expression)
    canonical_policy_action(policy.action_type, policy.target_model_id)


def _evaluate_node(
    node: AstNode,
    context: Mapping[str, object],
    deadline: _Deadline,
    collector: _ExplanationCollector,
    comparisons: list[ComparisonExplanation],
) -> bool:
    """按短路语义递归求值白名单 AST，并在每个节点入口检查 deadline。"""

    deadline.check()
    kind = node["kind"]
    if kind == "comparison":
        return _evaluate_comparison(node, context, deadline, collector, comparisons)
    if kind == "not":
        return not _evaluate_node(node["child"], context, deadline, collector, comparisons)
    if kind == "logical":
        if node["operator"] == "and":
            for child in node["children"]:
                if not _evaluate_node(child, context, deadline, collector, comparisons):
                    return False
            return True
        for child in node["children"]:
            if _evaluate_node(child, context, deadline, collector, comparisons):
                return True
        return False
    # 求值只接受编译器生成的固定节点，未知结构不能降级成默认放行。
    raise ValueError("unknown policy AST node kind")


def _evaluate_comparison(
    node: AstNode,
    context: Mapping[str, object],
    deadline: _Deadline,
    collector: _ExplanationCollector,
    comparisons: list[ComparisonExplanation],
) -> bool:
    """精确比较一个变量与 canonical literal，并追加有界安全解释。"""

    variable = node["variable"]
    actual_value, actual_display = _context_value(variable, context)
    expected_value, value_type, expected_display = _literal_value(node["value"], deadline)
    operator = node["operator"]

    if operator == "eq":
        matched = actual_value == expected_value
    elif operator == "ne":
        matched = actual_value != expected_value
    elif operator == "lt":
        matched = actual_value < expected_value
    elif operator == "lte":
        matched = actual_value <= expected_value
    elif operator == "gt":
        matched = actual_value > expected_value
    elif operator == "gte":
        matched = actual_value >= expected_value
    elif operator == "in":
        matched = actual_value in expected_value
    elif operator == "not_in":
        matched = actual_value not in expected_value
    else:
        # 未知操作符意味着 AST 完整性被破坏，绝不能按 false 后继续到默认 allow。
        raise ValueError("unknown policy comparison operator")

    collector.append(
        comparisons,
        ComparisonExplanation(
            variable=variable,
            operator=_DISPLAY_OPERATORS[operator],
            value_type=value_type,
            expected=expected_display,
            actual=actual_display,
            matched=matched,
        ),
    )
    return matched


def _literal_value(
    node: AstNode,
    deadline: _Deadline,
) -> tuple[object, str, str | tuple[str, ...]]:
    """把 AST literal 转为精确运行值和类型化解释展示值。"""

    deadline.check()
    kind = node["kind"]
    if kind == "string":
        return node["value"], "string", node["value"]
    if kind == "decimal":
        return Decimal(node["value"]), "decimal", node["value"]
    if kind == "list":
        runtime_items: list[object] = []
        display_items: list[str] = []
        item_type = "string"
        for child in node["items"]:
            runtime, item_type, display = _literal_value(child, deadline)
            runtime_items.append(runtime)
            if not isinstance(display, str):
                # v1 禁止嵌套列表；若出现说明 AST 已损坏，不能继续求值。
                raise ValueError("nested policy literal list is not supported")
            display_items.append(display)
        return tuple(runtime_items), f"{item_type}_list", tuple(display_items)
    # literal 类型超出白名单时必须拒绝，不能让任意对象参与比较。
    raise ValueError("unknown policy literal node kind")


def _context_value(variable: str, context: Mapping[str, object]) -> tuple[object, str]:
    """按变量类型惰性读取并规范化上下文，保证短路分支不会要求无关字段。"""

    if variable not in context:
        # 缺失变量只在实际访问时终止，既保留短路语义又避免用 None 静默比较。
        raise _ContextMissing(variable)
    value = context[variable]

    if variable in STRING_VARIABLES:
        if type(value) is not str:
            _raise_context(variable, "Policy string context values must be strings.")
        normalized = unicodedata.normalize("NFC", value)
        if len(normalized) > _STRING_CONTEXT_LIMITS[variable]:
            _raise_context(variable, "Policy string context value exceeds its length limit.")
        if _contains_forbidden_string_character(normalized):
            _raise_context(variable, "Policy string context contains invalid characters.")
        return normalized, normalized

    if variable == DECIMAL_VARIABLE:
        canonical = _canonical_context_decimal(variable, value)
        return Decimal(canonical), canonical

    # AST 变量超出六项白名单表示完整性被破坏，不能尝试从 context 任意取值。
    raise ValueError("unknown policy context variable")


def _canonical_context_decimal(variable: str, value: object) -> str:
    """把预算上下文规范为非负 Decimal 字符串，并显式拒绝 float/bool。"""

    if type(value) is str:
        if not _DECIMAL_RE.fullmatch(value):
            _raise_context(variable, "budget_remaining must use plain decimal string syntax.")
        canonical = _canonical_decimal(value)
    elif type(value) is Decimal:
        decimal_value = value
        if not decimal_value.is_finite():
            _raise_context(variable, "budget_remaining must be finite.")
        if decimal_value == 0:
            return "0"
        if decimal_value.adjusted() >= 12:
            _raise_context(variable, "budget_remaining exceeds the supported precision.")
        normalized_tuple = decimal_value.normalize().as_tuple()
        if normalized_tuple.exponent < -12:
            _raise_context(variable, "budget_remaining exceeds the supported precision.")
        rendered = format(decimal_value, "f")
        if not _DECIMAL_RE.fullmatch(rendered):
            _raise_context(variable, "budget_remaining exceeds the supported precision.")
        canonical = _canonical_decimal(rendered)
    else:
        _raise_context(variable, "budget_remaining must be a Decimal or decimal string.")

    if Decimal(canonical) < 0:
        _raise_context(variable, "budget_remaining must not be negative.")
    return canonical


def _fail_closed_policy_result(
    *,
    reason_code: str,
    checked_policy_count: int,
    elapsed_us: int,
    timed_out: bool,
    alert_required: bool,
    missing_variable: str | None = None,
) -> PolicyEvaluationResult:
    """构造不含部分比较或先前路由决定的固定拒绝结果。"""

    return PolicyEvaluationResult(
        matched=False,
        decision="deny",
        reason_code=reason_code,
        target_model_id=None,
        policy_reason=None,
        policy_checks=(),
        log_only_matches=(),
        mask_matches=(),
        mask_required=False,
        checked_policy_count=checked_policy_count,
        explanation_truncated=False,
        timed_out=timed_out,
        alert_required=alert_required,
        elapsed_us=elapsed_us,
        missing_variable=missing_variable,
    )


def _contains_forbidden_string_character(value: str) -> bool:
    """识别控制字符与 Unicode 代理字符，防止 canonical UTF-8 和日志边界被破坏。"""

    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _contains_template_marker(value: str) -> bool:
    """识别常见模板和插值起始符，确保 v1 字符串始终只是普通常量。"""

    return any(marker in value for marker in ("${", "{{", "{%", "<%"))


def _raise_context(variable: str, message: str) -> NoReturn:
    """以白名单变量和固定消息抛出上下文领域错误。"""

    # 上下文实际值可能包含企业数据，异常对象只允许保存变量名和固定原因。
    raise PolicyContextError(variable, message)


def _raise_at_token(code: str, message: str, token: _Token) -> NoReturn:
    """使用 token 的 code-point 位置生成不含源码片段的固定 DSL 诊断。"""

    _raise_dsl(code, message, start=token.start, end=token.end)


def _raise_dsl(
    code: str,
    message: str,
    *,
    start: int | None,
    end: int | None,
) -> NoReturn:
    """集中构造表达式错误，确保异常永远不保存原始 DSL 或底层异常。"""

    diagnostic = PolicyDiagnostic(
        code=code,
        field="condition_expr",
        start=start,
        end=end,
        message=message,
    )
    # 所有词法、文法、类型和资源失败统一映射为 policy.expression_invalid。
    raise PolicyDslError(diagnostic)
