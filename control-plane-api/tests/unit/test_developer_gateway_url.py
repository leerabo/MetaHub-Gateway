"""Public examples must not invent endpoints or expose credentials."""

from types import SimpleNamespace

import pytest
from metahub_control.console.gateway_url import trusted_gateway_base_url
from metahub_control.developer_portal.service import _gateway_base_url
from metahub_shared.runtime.errors import ApiError


@pytest.mark.parametrize("raw,expected", [
    ("https://gateway.example", "https://gateway.example/v1"),
    ("https://gateway.example/v1/", "https://gateway.example/v1"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000/v1"),
])
def test_sdk_and_navigation_share_public_address(raw, expected):
    config = SimpleNamespace(gateway_api_base_url=raw, environment="dev")
    assert trusted_gateway_base_url(config) == (expected, "ready")
    assert _gateway_base_url(config) == expected


@pytest.mark.parametrize("raw,environment", [
    ("", "dev"), ("javascript:alert(1)", "dev"),
    ("https://user:secret@gateway.example", "dev"),
    ("https://gateway.example?key=secret", "dev"),
    ("https://gateway.example#secret", "dev"),
    ("https://gateway.example/private", "dev"),
    ("https://gateway.example:invalid", "dev"),
    ("https://gate\nway.example", "dev"),
    ("https://gateway.example\\secret", "dev"),
    ("http://localhost:8000", "test"),
    ("http://127.0.0.1:8000", "prod"),
    ("http://[::1]:8000/v1", "prod"),
])
def test_invalid_config_is_unavailable_without_localhost_fallback(raw, environment):
    config = SimpleNamespace(gateway_api_base_url=raw, environment=environment)
    assert trusted_gateway_base_url(config) == (None, "configuration_required")
    with pytest.raises(ApiError) as raised:
        _gateway_base_url(config, error_code="developer.sdk_example_unavailable")
    assert raised.value.code == "developer.sdk_example_unavailable"
