"""Guest configuration and lifecycle checks, without claiming deployed isolation."""

import asyncio
import os
import ssl
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509
from metahub_assurance_runner import guest, guest_runtime
from metahub_assurance_runner.lease_channel import LeaseChannelError


def test_guest_does_not_inherit_configuration_credentials_or_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "METAHUB_CONFIG_FILE": "private-config",
            "METAHUB_DATABASE_URL": "private-database",
            "HTTPS_PROXY": "private-proxy",
            "AWS_SECRET_ACCESS_KEY": "private-secret",
            "SYSTEMROOT": "C:\\Windows",
        },
    )
    monkeypatch.setattr(guest_runtime.logging, "disable", lambda _level: None)
    guest_runtime._configure(tmp_path / "control.sqlite3", "https://127.0.0.1:8443")
    assert os.environ["SYSTEMROOT"] == "C:\\Windows"
    assert not {"HTTPS_PROXY", "AWS_SECRET_ACCESS_KEY", "METAHUB_CONFIG_FILE"} & os.environ.keys()
    assert "private-" not in repr(os.environ)
    assert os.environ["METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED"] == "false"
    assert os.environ["METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE"] == "test_capture"
    generated = [value for key, value in os.environ.items() if key.endswith("_SECRET")]
    assert len(generated) == len(set(generated)) == 7
    assert os.environ["METAHUB_FILE_STORAGE_ROOT"] == str(tmp_path / "file-storage")
    assert all(len(value) >= 48 for value in generated)


def test_ephemeral_certificate_is_only_for_loopback_and_verification_is_enabled(tmp_path):
    certificate_path, key_path = guest_runtime._certificate(tmp_path)
    certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    alternative_names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert [
        str(value) for value in alternative_names.value.get_values_for_type(x509.IPAddress)
    ] == ["127.0.0.1"]
    assert certificate.not_valid_before_utc < datetime.now(UTC) < certificate.not_valid_after_utc
    context = ssl.create_default_context(cafile=str(certificate_path))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert key_path.is_file()
    with pytest.raises(FileExistsError):
        guest_runtime._certificate(tmp_path)


@pytest.mark.parametrize(
    "platform,args", [("win32", ["guest"]), ("linux", ["guest", "untrusted-command"])]
)
def test_entrypoint_rejects_unsupported_platform_or_arguments(monkeypatch, platform, args):
    monkeypatch.setattr(guest.sys, "platform", platform)
    monkeypatch.setattr(guest.sys, "argv", args)
    assert guest.main() == 1


@pytest.mark.anyio
async def test_denied_startup_does_not_call_initializer():
    called = False

    class Denied:
        async def guard(self):
            return False

    def initialize():
        nonlocal called
        called = True

    with pytest.raises(LeaseChannelError):
        await guest_runtime._startup(initialize, Denied())
    assert not called


@pytest.mark.anyio
async def test_cancelled_startup_joins_thread_before_private_directory_cleanup():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Allowed:
        async def guard(self):
            return True

    def initialize():
        entered.set()
        release.wait(3)
        finished.set()

    task = asyncio.create_task(guest_runtime._startup(initialize, Allowed()))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_runtime_rejects_relative_paths_before_opening_listener(tmp_path):
    with pytest.raises(ValueError, match="path_boundary"):
        await guest_runtime.execute_workload(None, Path("relative.sqlite3"), tmp_path, tmp_path)
