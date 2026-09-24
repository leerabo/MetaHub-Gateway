from __future__ import annotations

from types import SimpleNamespace

import pytest
from metahub_control.console_experience import service
from metahub_shared.runtime.errors import ApiError


def _config():
    return SimpleNamespace(
        platform_session_secret="platform-secret",
        workspace_session_secret="workspace-secret",
    )


def _alias_last_sextet(value: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    index = alphabet.index(value[-1])
    # For a 32-byte signature the final sextet has two unused low bits.  The
    # adjacent alias decodes to the same bytes but is not canonical.
    assert index % 4 == 0
    alias = index + 1 if index < 60 else index - 1
    return value[:-1] + alphabet[alias]


@pytest.mark.parametrize("domain", ["platform", "workspace"])
def test_approval_cursor_rejects_noncanonical_signed_base64(domain: str) -> None:
    config = _config()
    cursor = service._encode_approval_cursor(
        config,
        domain=domain,
        session_id="session-1",
        permission_revision=1,
        fingerprint="fingerprint",
        owner=("owner",),
        filters=("pending", None, None),
        deadline_at="2026-09-15T00:00:00Z",
        projection_id="projection-1",
    )
    payload, signature = cursor.split(".", 1)
    alias = f"{payload}.{_alias_last_sextet(signature)}"
    with pytest.raises(ApiError):
        service._decode_approval_cursor(
            config,
            alias,
            domain=domain,
            session_id="session-1",
            permission_revision=1,
            fingerprint="fingerprint",
            owner=("owner",),
            filters=("pending", None, None),
            error_code="platform.request_invalid",
        )
