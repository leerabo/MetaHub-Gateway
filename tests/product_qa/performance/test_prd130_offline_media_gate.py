"""Focused contracts for the PRD-130 near-limit media gate."""

from __future__ import annotations

from metahub_shared.private_gateway import MAX_OFFLINE_ENTRY_BYTES

from scripts.quality.prd130_offline_media_gate import _entry_plaintext_sizes


def test_entry_sizes_cover_single_and_multi_entry_ciphertext_boundaries() -> None:
    maximum_ciphertext_size = MAX_OFFLINE_ENTRY_BYTES + 16
    cases = (
        17,
        maximum_ciphertext_size,
        maximum_ciphertext_size + 1,
        maximum_ciphertext_size + 16,
        maximum_ciphertext_size + 17,
        (maximum_ciphertext_size * 2) + 1,
    )

    for ciphertext_bytes in cases:
        plaintext_sizes = _entry_plaintext_sizes(ciphertext_bytes)
        assert sum(size + 16 for size in plaintext_sizes) == ciphertext_bytes
        assert all(1 <= size <= MAX_OFFLINE_ENTRY_BYTES for size in plaintext_sizes)
