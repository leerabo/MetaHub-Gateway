import base64
from datetime import UTC, datetime
import hashlib
import hmac
import importlib.util
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
for directory in ("shared-sdk/python", "control-plane-api/src", "gateway-data-plane/src"):
    sys.path.insert(0, str(ROOT / directory))
spec = importlib.util.spec_from_file_location("first_owner", ROOT / "scripts/deploy/bootstrap-owner.py")
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)


def credentials():
    from metahub_shared.identity import generate_totp_secret
    now = datetime.now(UTC)
    secret = generate_totp_secret()
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", int(now.timestamp()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    code = f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"
    return dict(email="Owner@example.invalid", name=" Owner ", password="IndependentOwner!2026",
                secret=secret, code=code, now=now)


def test_real_authenticator_code_and_normalized_identity():
    email, name, counter = owner.checked_credentials(**credentials())
    assert email == "owner@example.invalid"
    assert name == "Owner"
    assert counter > 0


def test_password_only_owner_does_not_need_authenticator():
    values = credentials()
    values.update(secret=None, code=None)
    email, name, counter = owner.checked_credentials(**values)
    assert (email, name, counter) == ("owner@example.invalid", "Owner", None)


@pytest.mark.parametrize("field,value", [("email", "not-an-email"), ("name", " "),
                                        ("name", "bad\x00name"), ("password", "shortpwd"),
                                        ("code", "not-a-code")])
def test_invalid_credentials_are_rejected_before_database_access(field, value):
    values = credentials()
    values[field] = value
    with pytest.raises(ValueError):
        owner.provision(None, encryption_root="a" * 64, **values)


def test_cli_refuses_redirected_enrollment_material(monkeypatch, capsys):
    monkeypatch.setattr(owner.sys.stdin, "isatty", lambda: False)
    assert owner.main() == 1
    assert not capsys.readouterr().out
