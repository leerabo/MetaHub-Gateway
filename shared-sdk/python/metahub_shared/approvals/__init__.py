"""Public approval contracts shared by Control and worker processes.
集中导出审批加密契约，调用方只能取得版本化摘要，不能记录密钥或票据原文。
"""

from .contracts import (
    CREATE_INTENT_DOMAIN,
    CREATE_KEY_DOMAIN,
    CURSOR_DOMAIN,
    DECISION_INTENT_DOMAIN,
    DECISION_KEY_DOMAIN,
    MAX_APPROVAL_CANONICAL_BYTES,
    MAX_APPROVAL_DOMAIN_BYTES,
    STEP_UP_DOMAIN,
    TARGET_FINGERPRINT_DOMAIN,
    ApprovalContractError,
    ApprovalDigest,
    ApprovalKeyring,
    VersionedApprovalSecret,
    canonical_approval_bytes,
)

__all__ = [
    "CREATE_INTENT_DOMAIN",
    "CREATE_KEY_DOMAIN",
    "CURSOR_DOMAIN",
    "DECISION_INTENT_DOMAIN",
    "DECISION_KEY_DOMAIN",
    "MAX_APPROVAL_CANONICAL_BYTES",
    "MAX_APPROVAL_DOMAIN_BYTES",
    "STEP_UP_DOMAIN",
    "TARGET_FINGERPRINT_DOMAIN",
    "ApprovalContractError",
    "ApprovalDigest",
    "ApprovalKeyring",
    "VersionedApprovalSecret",
    "canonical_approval_bytes",
]
