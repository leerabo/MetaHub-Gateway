from __future__ import annotations

import ssl

import httpx
import pytest
from metahub_gateway.local_secrets import (
    LocalProviderTesterRegistry,
    OpenAICompatibleProviderTester,
)


class _UnreadBody(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("provider response bodies must not be read")


def test_openai_compatible_probe_is_body_blind_and_returns_fixed_capability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL("https://provider.example.test/v1/models")
        assert request.headers["authorization"] == "Bearer local-test-credential"
        assert request.headers["cache-control"] == "no-store"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_UnreadBody(),
        )

    tester = OpenAICompatibleProviderTester(
        "https://provider.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    value = bytearray(b"local-test-credential")
    result = tester(memoryview(value))
    assert result.outcome == "succeeded"
    assert result.capabilities == ("models",)
    assert result.latency_bucket == "lt_100ms"
    assert "provider.example.test" not in repr(tester)
    assert value == bytearray(b"local-test-credential")


@pytest.mark.parametrize(
    ("status_code", "outcome"),
    [
        (401, "authentication_failed"),
        (403, "authorization_failed"),
        (404, "unsupported"),
        (408, "timeout"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ],
)
def test_openai_compatible_probe_maps_status_to_fixed_outcome(status_code, outcome) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code))
    tester = OpenAICompatibleProviderTester(
        "https://provider.example.test", transport=transport
    )
    result = tester(memoryview(b"local-test-credential"))
    assert result.outcome == outcome
    assert result.capabilities == ()


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("timeout", "timeout"),
        ("network", "network_unreachable"),
        ("tls", "tls_failed"),
    ],
)
def test_openai_compatible_probe_maps_transport_without_exposing_exception(kind, outcome) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout("sensitive endpoint detail", request=request)
        error = httpx.ConnectError("sensitive endpoint detail", request=request)
        if kind == "tls":
            error.__cause__ = ssl.SSLError()
        raise error

    tester = OpenAICompatibleProviderTester(
        "https://provider.example.test",
        transport=httpx.MockTransport(handler),
    )
    result = tester(memoryview(b"local-test-credential"))
    assert result.outcome == outcome
    assert result.capabilities == ()


def test_probe_latency_bucket_and_registry_resolution_are_bounded() -> None:
    ticks = iter((10.0, 10.6))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    tester = OpenAICompatibleProviderTester(
        "https://provider.example.test",
        transport=transport,
        monotonic=lambda: next(ticks),
    )
    registry = LocalProviderTesterRegistry({"pcn_private_001": tester})
    result = registry("pcn_private_001", memoryview(b"local-test-credential"))
    assert result.latency_bucket == "500_1999ms"
    assert registry("pcn_missing_001", memoryview(b"ignored")).outcome == "unsupported"
    assert repr(registry) == "LocalProviderTesterRegistry(target_count=1)"


def test_probe_rejects_unsafe_target_and_credential_before_network() -> None:
    with pytest.raises(ValueError, match="local_secret.provider_target_invalid"):
        OpenAICompatibleProviderTester("https://user:password@provider.example.test")

    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    tester = OpenAICompatibleProviderTester(
        "https://provider.example.test",
        transport=httpx.MockTransport(handler),
    )
    result = tester(memoryview(b"line-one\nline-two"))
    assert result.outcome == "authentication_failed"
    assert called is False
