"""PRD-074 批处理共享契约：统一字段、加密和状态语义，不持有租户授权或服务端业务决策。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 shared Batch contracts and cryptographic envelopes.
"""

from .contracts import (
    BATCH_OPERATION_TEMPLATE_VERSION,
    MAX_BATCH_RESULT_BYTES,
    BatchContractError,
    BatchOperationSpec,
    canonical_batch_price,
    canonical_operation_spec_bytes,
    render_batch_messages,
    validate_operation_spec,
)
from .crypto import (
    BATCH_KEY_VERSION,
    BatchCryptoError,
    BatchEnvelopeOwner,
    batch_payload_hmac,
    batch_result_hmac,
    decrypt_batch_payload,
    decrypt_batch_result,
    encrypt_batch_payload,
    encrypt_batch_result,
)

__all__ = [
    "BATCH_KEY_VERSION",
    "BATCH_OPERATION_TEMPLATE_VERSION",
    "MAX_BATCH_RESULT_BYTES",
    "BatchContractError",
    "BatchCryptoError",
    "BatchEnvelopeOwner",
    "BatchOperationSpec",
    "batch_payload_hmac",
    "batch_result_hmac",
    "canonical_batch_price",
    "canonical_operation_spec_bytes",
    "decrypt_batch_payload",
    "decrypt_batch_result",
    "encrypt_batch_payload",
    "encrypt_batch_result",
    "render_batch_messages",
    "validate_operation_spec",
]
