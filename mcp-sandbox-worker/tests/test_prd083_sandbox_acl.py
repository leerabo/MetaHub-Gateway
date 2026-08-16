"""Focused PRD-083 ACL, IP pinning, and session cleanup tests."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from metahub_mcp_sandbox.acl import NetworkBlocked, authorize_endpoint
from metahub_mcp_sandbox.registry import SessionRegistry
from metahub_mcp_sandbox.transport import PinnedAsyncTransport


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "mcp_sandbox_dns_timeout_seconds": 2,
        "mcp_sandbox_max_dns_answers": 8,
        "mcp_sandbox_private_cidrs": (),
        "mcp_sandbox_deny_cidrs": (),
        "mcp_sandbox_allowed_ports": (443,),
        "mcp_sandbox_deny_ports": (1433, 3306, 5432, 6379),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _answer(address: str) -> tuple[object, ...]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr: tuple[object, ...]
    if family == socket.AF_INET6:
        sockaddr = (address, 443, 0, 0)
    else:
        sockaddr = (address, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def test_acl_allows_only_complete_public_answer_set(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [_answer("93.184.216.34"), _answer("2606:2800:220:1:248:1893:25c8:1946")]

    async def run() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        decision = await authorize_endpoint(
            "https://mcp.example.test/tools",
            policy_status="active",
            egress_mode="registered_endpoint_only",
            config=_config(),
        )
        assert decision.address_count == 2
        assert decision.port == 443
        assert "93.184" not in repr(decision)

    asyncio.run(run())


def test_acl_rejects_mixed_or_disabled_without_disclosing_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [_answer("93.184.216.34"), _answer("127.0.0.1")]

    async def run() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(NetworkBlocked) as mixed:
            await authorize_endpoint(
                "https://mcp.example.test/tools",
                policy_status="active",
                egress_mode="registered_endpoint_only",
                config=_config(),
            )
        assert mixed.value.reason_code == "mixed_dns_answers"
        assert "127.0.0.1" not in str(mixed.value)
        with pytest.raises(NetworkBlocked) as disabled:
            await authorize_endpoint(
                "https://mcp.example.test/tools",
                policy_status="disabled",
                egress_mode="registered_endpoint_only",
                config=_config(),
            )
        assert disabled.value.reason_code == "policy_disabled"

    asyncio.run(run())


def test_acl_rejects_excess_answers_with_bounded_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [_answer(f"93.184.216.{index}") for index in range(1, 18)]

    async def run() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(NetworkBlocked) as excess:
            await authorize_endpoint(
                "https://mcp.example.test/tools",
                policy_status="active",
                egress_mode="registered_endpoint_only",
                config=_config(mcp_sandbox_max_dns_answers=8),
            )
        assert excess.value.reason_code == "address_denied"
        assert excess.value.address_count == 8

    asyncio.run(run())


def test_acl_private_exception_requires_exact_address() -> None:
    async def run() -> None:
        decision = await authorize_endpoint(
            "https://10.24.8.9/tools",
            policy_status="active",
            egress_mode="registered_endpoint_only",
            config=_config(mcp_sandbox_private_cidrs=("10.24.8.9/32",)),
        )
        assert decision.address_count == 1
        with pytest.raises(NetworkBlocked):
            await authorize_endpoint(
                "https://10.24.8.10/tools",
                policy_status="active",
                egress_mode="registered_endpoint_only",
                config=_config(mcp_sandbox_private_cidrs=("10.24.8.9/32",)),
            )

    asyncio.run(run())


def test_acl_permanently_rejects_ipv4_mapped_ipv6() -> None:
    async def run() -> None:
        with pytest.raises(NetworkBlocked) as mapped:
            await authorize_endpoint(
                "https://[::ffff:8.8.8.8]/tools",
                policy_status="active",
                egress_mode="registered_endpoint_only",
                config=_config(),
            )
        assert mapped.value.reason_code == "address_denied"
        assert "8.8.8.8" not in str(mapped.value)

    asyncio.run(run())


def test_pinned_transport_preserves_host_and_sni_without_second_dns() -> None:
    class CaptureTransport(httpx.AsyncBaseTransport):
        request: httpx.Request | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.request = request
            return httpx.Response(204, request=request)

        async def aclose(self) -> None:
            return None

    async def run() -> None:
        transport = PinnedAsyncTransport(
            scheme="https",
            hostname="mcp.example.test",
            port=443,
            pinned_ip="93.184.216.34",
        )
        assert transport._inner._pool._max_connections == 1  # type: ignore[attr-defined]
        assert transport._inner._pool._max_keepalive_connections == 0  # type: ignore[attr-defined]
        capture = CaptureTransport()
        transport._inner = capture  # type: ignore[assignment]
        request = httpx.Request("POST", "https://mcp.example.test/rpc")
        response = await transport.handle_async_request(request)
        assert response.status_code == 204
        assert capture.request is not None
        assert capture.request.url.host == "93.184.216.34"
        assert capture.request.headers["host"] == "mcp.example.test"
        assert capture.request.extensions["sni_hostname"] == "mcp.example.test"

    asyncio.run(run())


def test_registry_forces_cleanup_and_atomically_unregisters() -> None:
    calls: list[str] = []
    graceful_cancelled: asyncio.Event | None = None

    async def blocked_close() -> None:
        assert graceful_cancelled is not None
        try:
            await asyncio.Event().wait()
        finally:
            graceful_cancelled.set()

    async def force_close() -> None:
        calls.append("force")

    async def run() -> None:
        nonlocal graceful_cancelled
        graceful_cancelled = asyncio.Event()
        registry = SessionRegistry(maximum=1)
        await registry.register("dispatch-safe-id")
        task: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(60))
        await registry.attach(
            "dispatch-safe-id",
            task=task,
            graceful_close=blocked_close,
            force_close=force_close,
            connection=True,
        )
        existing_tasks = set(asyncio.all_tasks())
        await registry.cleanup("dispatch-safe-id", deadline_seconds=0.01)
        await asyncio.sleep(0)
        assert calls == ["force"]
        assert graceful_cancelled.is_set()
        assert task.cancelled()
        assert not {current for current in asyncio.all_tasks() if current not in existing_tasks}
        assert await registry.snapshot() == {
            "active_sessions": 0,
            "active_connections": 0,
            "owned_tasks": 0,
        }

    asyncio.run(run())


def test_registry_cleanup_never_cancels_its_caller() -> None:
    async def run() -> None:
        registry = SessionRegistry(maximum=1)
        await registry.register("dispatch-current-task")
        current = asyncio.current_task()
        assert current is not None
        await registry.attach("dispatch-current-task", task=current)
        await registry.cleanup("dispatch-current-task", deadline_seconds=0.1)
        assert not current.cancelled()
        assert await registry.snapshot() == {
            "active_sessions": 0,
            "active_connections": 0,
            "owned_tasks": 0,
        }

    asyncio.run(run())
