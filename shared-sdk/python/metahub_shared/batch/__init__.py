"""PRD-074 shared Batch contracts and cryptographic envelopes."""

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
