"""Controlled transport tests for probe failure behavior, not security attestations."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from metahub_assurance_runner.probes import (
    GLOBAL_CSRF,
    MAX_RESPONSE_BYTES,
    WORKSPACE_CSRF,
    WORKSPACE_SESSION,
    FixedProbeRunner,
    ProbeFixtures,
)


async def _allowed():
    return True


@asynccontextmanager
async def _fixtures(handler):
    async with AsyncExitStack() as stack:
        clients = []
        for label in ("source", "target", "member", "anonymous"):
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url="https://assurance.test",
                    transport=httpx.MockTransport(handler),
                    trust_env=False,
                    headers={"X-Test-Subject": label},
                )
            )
            if label != "anonymous":
                for cookie in (GLOBAL_CSRF, WORKSPACE_CSRF, WORKSPACE_SESSION):
                    client.cookies.set(cookie, "c" * 43)
            clients.append(client)
        yield ProbeFixtures(
            *clients,
            origin="https://assurance.test",
            source_workspace_id="wsp_" + "1" * 32,
            target_workspace_id="wsp_" + "2" * 32,
            source_project_id="prj_" + "3" * 32,
            target_project_id="prj_" + "4" * 32,
            target_application_id="app_" + "5" * 32,
            target_project_name="Target project",
            member_membership_id="wmb_" + "6" * 32,
            member_revision=1,
        )


def _runner(fixtures, guard=_allowed, **kwargs):
    return FixedProbeRunner(
        fixtures,
        guard,
        deadline_at=kwargs.get("deadline_at", datetime.now(UTC) + timedelta(minutes=1)),
    )


def _session(request):
    digit = "1" if request.headers["X-Test-Subject"] == "source" else "2"
    return httpx.Response(200, json={"context": {"workspace_id": "wsp_" + digit * 32}})


@pytest.mark.anyio
@pytest.mark.parametrize("allowed", [False, None, 1, "true"])
async def test_revocation_permanently_fences_without_sending(allowed):
    requests = []
    decision = allowed

    async def guard():
        return decision

    async with _fixtures(lambda request: requests.append(request)) as fixtures:
        runner = _runner(fixtures, guard)
        first = await runner.run("api_idor")
        decision = True
        second = await runner.run("api_idor")
        assert first.reason_code == second.reason_code == "lease_revoked"
        assert first.outcome == second.outcome == "error"
        assert not requests


@pytest.mark.anyio
async def test_guard_failure_does_not_disclose_exception():
    async def guard():
        raise RuntimeError("private-lease-secret")

    async with _fixtures(_session) as fixtures:
        result = await _runner(fixtures, guard).run("api_idor")
        assert result.reason_code == "lease_revoked"
        assert "private-lease-secret" not in repr(asdict(result))


@pytest.mark.anyio
async def test_revocation_after_response_prevents_next_request():
    requests = []
    allowed = True

    def handler(request):
        nonlocal allowed
        requests.append(request)
        allowed = False
        return _session(request)

    async def guard():
        return allowed

    async with _fixtures(handler) as fixtures:
        result = await _runner(fixtures, guard).run("api_idor")
        assert result.reason_code == "lease_revoked"
        assert not result.checks
        assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": str(MAX_RESPONSE_BYTES + 1)},
        {"content-length": "-1"},
        {"content-length": "invalid"},
        {"content-encoding": "gzip"},
    ],
)
async def test_response_headers_rejected_before_body(headers):
    class UnreadableStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("body should not be read")
            yield b""

    async with _fixtures(
        lambda _: httpx.Response(
            200,
            headers=headers,
            stream=UnreadableStream(),
        )
    ) as fixtures:
        result = await _runner(fixtures).run("api_idor")
        assert (result.outcome, result.reason_code) == ("error", "probe_unavailable")


@pytest.mark.anyio
async def test_stream_limit_closes_response_without_reading_to_eof():
    class LargeStream(httpx.AsyncByteStream):
        count = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.count += 1
                yield b"x" * 16_384

        async def aclose(self):
            self.closed = True

    stream = LargeStream()
    async with _fixtures(lambda _: httpx.Response(200, stream=stream)) as fixtures:
        result = await _runner(fixtures).run("api_idor")
        assert result.reason_code == "probe_unavailable"
        assert stream.count == MAX_RESPONSE_BYTES // 16_384 + 1
        assert stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("body", [b"not-json", b"null", b"[]", b"{}", b'{"context":null}'])
async def test_invalid_response_does_not_escape_runner(body):
    async with _fixtures(lambda _: httpx.Response(200, content=body)) as fixtures:
        result = await _runner(fixtures).run("api_idor")
        assert result.outcome == "error"
        assert result.reason_code == "probe_unavailable"


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["missing", "duplicate", "write_unavailable"])
async def test_write_probe_requires_present_unique_writable_resource(case):
    def handler(request):
        if request.url.path == "/workspace/session":
            return _session(request)
        if request.method == "PATCH":
            return httpx.Response(404 if case == "write_unavailable" else 200, json={})
        project = {"project_id": "prj_" + "4" * 32, "name": "Target project"}
        return httpx.Response(
            200,
            json={
                "projects": (
                    [] if case == "missing" else [project] * (2 if case == "duplicate" else 1)
                )
            },
        )

    async with _fixtures(handler) as fixtures:
        result = await _runner(fixtures).run("write_authorization")
        assert (result.outcome, result.reason_code) == ("failed", "contract_violation")
        assert result.checks[-1].code == (
            "target.project_write" if case == "write_unavailable" else "target.project_present"
        )


@pytest.mark.anyio
async def test_hanging_guard_times_out_and_fences():
    requests = []

    async def guard():
        await asyncio.Event().wait()

    async with _fixtures(lambda request: requests.append(request)) as fixtures:
        runner = _runner(
            fixtures,
            guard,
            deadline_at=datetime.now(UTC) + timedelta(milliseconds=50),
        )
        result = await runner.run("api_idor")
        assert result.reason_code == "probe_timeout"
        assert (await runner.run("api_idor")).reason_code == "lease_revoked"
        assert not requests


@pytest.mark.anyio
async def test_cancellation_and_concurrent_run_fencing():
    entered = asyncio.Event()

    async def guard():
        entered.set()
        await asyncio.Event().wait()

    async with _fixtures(_session) as fixtures:
        runner = _runner(fixtures, guard)
        task = asyncio.create_task(runner.run("api_idor"))
        await entered.wait()
        with pytest.raises(ValueError, match="concurrent"):
            await runner.run("api_idor")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await runner.run("api_idor")).reason_code == "lease_revoked"


@pytest.mark.anyio
async def test_unimplemented_scenarios_remain_errors():
    requests = []
    async with _fixtures(lambda request: requests.append(request)) as fixtures:
        runner = _runner(fixtures)
        for code in (
            "cache_owner",
            "queue_owner",
            "object_prefix",
            "metrics_labels",
            "late_response",
        ):
            result = await runner.run(code)
            assert (result.outcome, result.reason_code) == ("error", "scenario_not_implemented")
        assert not requests


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"source_project_id": "../project"},
        {"target_project_id": "prj_" + "0" * 32},
        {"source_workspace_id": "wsp_" + "2" * 32},
        {"origin": "http://assurance.test"},
        {"origin": "https://user:password@assurance.test"},
        {"origin": "https://assurance.test/path"},
    ],
)
async def test_fixture_boundary_rejects_unsafe_configuration(change):
    async with _fixtures(_session) as fixtures:
        with pytest.raises(ValueError):
            replace(fixtures, **change)


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["noop_write", "denied_but_changed"])
async def test_write_probe_verifies_actual_write_and_denial_side_effects(case):
    project = {"project_id": "prj_" + "4" * 32, "name": "Target project"}

    def handler(request):
        if request.url.path == "/workspace/session":
            return _session(request)
        if request.method == "PATCH":
            if request.headers["X-Test-Subject"] == "source":
                project["name"] = "unauthorized-side-effect"
                return httpx.Response(404, json={})
            if case != "noop_write":
                project["name"] = "assurance-authorized-control-a"
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"projects": [project]})

    async with _fixtures(handler) as fixtures:
        result = await _runner(fixtures).run("write_authorization")
        assert result.outcome == "failed"
        assert result.checks[-1].code == (
            "target.write_applied" if case == "noop_write" else "target.project_unchanged"
        )


@pytest.mark.anyio
async def test_deep_json_does_not_escape_runner():
    async with _fixtures(
        lambda _: httpx.Response(200, content=b"[" * 2000 + b"]" * 2000)
    ) as fixtures:
        result = await _runner(fixtures).run("api_idor")
        assert (result.outcome, result.reason_code) == ("error", "probe_unavailable")


@pytest.mark.anyio
async def test_ambiguous_cookie_does_not_escape_runner():
    async with _fixtures(_session) as fixtures:
        fixtures.source.cookies.set(WORKSPACE_SESSION, "d" * 43, domain="assurance.test")
        result = await _runner(fixtures).run("replay_fencing")
        assert (result.outcome, result.reason_code) == ("error", "probe_unavailable")
