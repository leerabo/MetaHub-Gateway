"""Validate the public Gateway address shared by navigation and SDK examples."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from metahub_shared.runtime.config import AppConfig


def trusted_gateway_base_url(config: AppConfig) -> tuple[str | None, str]:
    raw_url = config.gateway_api_base_url.strip()
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        _ = parsed.port
    except ValueError:
        return None, "configuration_required"
    if (
        not raw_url
        or any(character.isspace() or ord(character) < 32 for character in raw_url)
        or "\\" in raw_url
        or parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/v1", "/v1/"}
    ):
        return None, "configuration_required"
    is_loopback = hostname == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if config.environment != "dev" and is_loopback:
        return None, "configuration_required"
    return f"{parsed.scheme.lower()}://{parsed.netloc}/v1", "ready"
