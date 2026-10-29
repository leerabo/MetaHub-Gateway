"""PRD-080 账单争议生产模块的中文职责与关键安全注释门禁。"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

SOURCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "metahub_control"
    / "billing_disputes"
)
SOURCE_FILES = (
    "__init__.py",
    "models.py",
    "routes.py",
    "scheduler.py",
    "outbox.py",
    "service.py",
)
HAN = re.compile(r"[\u4e00-\u9fff]")

MODULE_TERMS = {
    "models.py": ("金额", "租户", "敏感"),
    "routes.py": ("权限", "幂等", "缓存"),
    "scheduler.py": ("租户", "租约", "停服"),
    "outbox.py": ("owner", "generation", "CAS"),
    "service.py": ("tenant_id", "事务", "整数微单位"),
}

DOCUMENTED_SYMBOLS = {
    "models.py": (
        "BillingDisputeAdjustmentRequest",
        "BillingDisputeCreateRequest",
        "BillingDisputePatchRequest",
        "BillingDisputeSummary",
        "BillingSettlementHoldInfo",
        "BillingDisputeItemInfo",
        "BillingDisputeTransitionInfo",
        "BillingDisputeEventInfo",
        "BillingDisputeDetail",
    ),
    "routes.py": (
        "BillingDisputeRoute",
        "get_billing_disputes_route",
        "get_billing_dispute_route",
        "post_billing_dispute_route",
        "patch_billing_dispute_route",
        "_parse_exact_json",
        "_raw_header_values",
        "_private",
    ),
    "scheduler.py": (
        "install_billing_dispute_scheduler",
        "_event_loop",
        "run_billing_dispute_event_once",
    ),
    "outbox.py": (
        "BillingDisputeEventClaim",
        "claim_next_billing_dispute_event",
        "_claim_with_progress",
        "dispatch_claimed_billing_dispute_event",
        "drain_billing_dispute_event_once",
        "recover_billing_dispute_source_request_id",
        "_event_from_row",
        "_verify_snapshot",
        "_renew",
        "_finish",
        "_quarantine",
        "_format_micros",
    ),
    "service.py": (
        "list_billing_disputes",
        "get_billing_dispute",
        "create_billing_dispute",
        "transition_billing_dispute",
        "_write_approved_adjustments",
        "_freeze_scope",
        "_detail",
        "_normalize_create",
        "_normalize_patch",
        "_remaining_micros",
        "_decode_cursor",
        "_parse_amount",
        "_reason",
        "_format_micros",
    ),
}

COMMENT_TERMS = {
    "models.py": ("金额、租户和敏感投递事实",),
    "routes.py": ("重复 Header", "实际字节数"),
    "scheduler.py": ("同步网络线程",),
    "outbox.py": (
        "BEGIN IMMEDIATE",
        "投递快照已在事件创建事务冻结",
        "网络调用在领取事务提交后",
        "late owner",
    ),
    "service.py": (
        "事务内权限复核",
        "BEGIN IMMEDIATE 后事务内重新校验权限",
        "hold 自身修订号",
        "提交后 dispatch",
        "整数微单位",
    ),
}


def _source(name: str) -> str:
    """按 UTF-8 读取一个生产模块，避免平台默认编码掩盖中文注释损坏。"""

    return (SOURCE_ROOT / name).read_text(encoding="utf-8")


def _comments(source: str) -> str:
    """只提取真正的 Python 注释 token，普通字符串不能满足注释门禁。"""

    return "\n".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    )


def test_prd080_modules_explain_their_security_boundary_in_chinese() -> None:
    """每个新增模块必须有中文职责说明，关键模块还要明确自身安全边界。"""

    for name in SOURCE_FILES:
        module_doc = ast.get_docstring(ast.parse(_source(name)), clean=False)
        assert module_doc is not None
        assert HAN.search(module_doc), name
        for term in MODULE_TERMS.get(name, ()):
            assert term in module_doc, f"{name}: {term}"


def test_prd080_critical_symbols_keep_chinese_contract_docs() -> None:
    """公开入口与复杂私有块必须保留可审查的中文输入、返回和约束说明。"""

    for name, expected_symbols in DOCUMENTED_SYMBOLS.items():
        module = ast.parse(_source(name))
        definitions = {
            node.name: node
            for node in module.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for symbol in expected_symbols:
            assert symbol in definitions, f"{name}: {symbol}"
            doc = ast.get_docstring(definitions[symbol], clean=False)
            assert doc is not None, f"{name}: {symbol}"
            assert len(HAN.findall(doc)) >= 8, f"{name}: {symbol}"


def test_prd080_key_rationale_is_present_in_actual_comments() -> None:
    """租户、事务、金额、快照、租约与迟到 owner 的理由必须位于真实代码注释。"""

    for name, terms in COMMENT_TERMS.items():
        comments = _comments(_source(name))
        for term in terms:
            assert term in comments, f"{name}: {term}"
