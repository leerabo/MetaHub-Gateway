"""Controlled bootstrap failures; these tests do not attest deployed isolation."""

from __future__ import annotations

import asyncio
import json
import traceback
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from metahub_assurance_runner.bootstrap import BootstrapError, HttpFixtureBootstrap
from metahub_assurance_runner.probes import GLOBAL_CSRF, WORKSPACE_CSRF

ORIGIN = "https://assurance.test"
ENVIRONMENT = "aenv_" + "a" * 32


async def _allowed():
    return True


async def _mailbox(*_):
    return "123456"


@asynccontextmanager
async def _bootstrap(handler=None, **kwargs):
    requests = []

    def respond(request):
        requests.append(request)
        label = request.headers["X-Test-Subject"]
        digit = {"source": "1", "target": "2", "member": "3", "anonymous": "4"}[label]
        payload = json.loads(request.content) if request.content else {}
        status = 200
        cookie = None
        match request.url.path:
            case "/identity/registrations":
                body = {"intent_id": "rgi_" + digit * 32}
            case "/identity/verifications":
                body = {}
            case "/identity/login":
                body = {"account": {
                    "email": payload["email"], "status": "active",
                }}
                cookie = GLOBAL_CSRF
            case "/workspaces":
                assert len(payload["slug"]) <= 48
                status = 201
                body = {
                    "workspace": {
                        **payload,
                        "workspace_id": "wsp_" + digit * 32,
                        "status": "active",
                    }
                }
            case "/identity/workspace-switches":
                body = {
                    "context": {
                        "workspace_id": payload["workspace_id"],
                        "role": "admin" if label == "member" else "owner",
                    }
                }
                cookie = WORKSPACE_CSRF
            case "/projects":
                status = 201
                body = {
                    "project": {
                        **payload,
                        "project_id": "prj_" + digit * 32,
                        "tenant_id": "ten_" + digit * 32,
                        "status": "active",
                    }
                }
            case "/applications":
                status = 201
                body = {
                    "application": {
                        **payload,
                        "application_id": "app_" + "4" * 32,
                        "tenant_id": "ten_" + "2" * 32,
                        "status": "active",
                    }
                }
            case "/membership-invitations":
                status = 201
                body = {
                    "invitation": {
                        "invitation_id": "min_" + "5" * 32,
                        "role": "admin",
                        "status": "pending",
                    },
                    "accept_token": "i" * 43,
                }
            case "/memberships":
                body = {
                    "memberships": [
                        {
                            "membership_id": "wmb_" + "6" * 32,
                            "role": "admin",
                            "status": "active",
                            "revision": 1,
                            "is_current": True,
                        }
                    ]
                }
            case _:
                raise AssertionError("unexpected bootstrap request")
        if handler:
            override = handler(request, body)
            if override is not None:
                return override
        response = httpx.Response(status, json=body)
        if cookie:
            response.headers["set-cookie"] = f"{cookie}={'c' * 43}; Path=/; Secure"
        return response

    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=ORIGIN,
                    trust_env=False,
                    transport=httpx.MockTransport(respond),
                    headers={"X-Test-Subject": label},
                )
            )
            for label in ("source", "target", "member", "anonymous")
        ]
        yield (
            HttpFixtureBootstrap(
                *clients,
                origin=ORIGIN,
                environment_id=ENVIRONMENT,
                mailbox=kwargs.get("mailbox", _mailbox),
                lease_guard=kwargs.get("guard", _allowed),
                deadline_at=kwargs.get("deadline", datetime.now(UTC) + timedelta(minutes=1)),
            ),
            requests,
        )


@pytest.mark.anyio
async def test_success_is_single_attempt_and_journal_is_hashed():
    async with _bootstrap() as (bootstrap, requests):
        fixtures = await bootstrap.run()
        assert fixtures.source_workspace_id != fixtures.target_workspace_id
        assert len(bootstrap.observations) == 13
        assert len(requests) == 19
        assert len({item.identity_sha256 for item in bootstrap.observations}) == 13
        serialized = repr(bootstrap) + repr(bootstrap.observations)
        assert "@example.test" not in serialized and "123456" not in serialized
        with pytest.raises(BootstrapError, match="already_attempted"):
            await bootstrap.run()
        assert len(requests) == 19


@pytest.mark.anyio
async def test_equivalent_root_urls_use_one_canonical_origin():
    async with _bootstrap() as (bootstrap, requests):
        bootstrap.target.base_url = ORIGIN + "/"
        bootstrap = replace(bootstrap, origin=ORIGIN + "/")
        fixtures = await bootstrap.run()
        assert fixtures.origin == ORIGIN
        assert all(request.headers.get("origin", ORIGIN) == ORIGIN for request in requests)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"origin": "http://assurance.test"},
        {"origin": "https://foreign.test"},
        {"origin": "https://user:secret@assurance.test"},
        {"origin": ORIGIN + "/path"},
        {"origin": ORIGIN + "?query=value"},
        {"origin": ORIGIN + "#fragment"},
        {"environment_id": "../../data"},
        {"deadline_at": datetime(2026, 1, 1)},
    ],
)
async def test_reject_invalid_boundary(change):
    async with _bootstrap() as (bootstrap, requests):
        with pytest.raises(ValueError):
            replace(bootstrap, **change)
        assert not requests


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "cookie",
        "auth",
        "authorization",
        "host",
        "forwarded",
        "proxy-authorization",
        "x-forwarded-host",
        "x-forwarded-for",
        "redirects",
        "params",
        "base_path",
        "same_client",
    ],
)
async def test_reject_dirty_or_ambiguous_clients(case):
    async with _bootstrap() as (bootstrap, requests):
        source = bootstrap.source
        changes = {}
        if case == "cookie":
            source.cookies.set("foreign", "secret")
        elif case == "auth":
            source.auth = ("user", "password")
        elif case == "redirects":
            source.follow_redirects = True
        elif case == "params":
            source.params = {"scope": "foreign"}
        elif case == "base_path":
            source.base_url = ORIGIN + "/foreign/"
        elif case == "same_client":
            changes["target"] = source
        else:
            source.headers[case] = "foreign-secret"
        with pytest.raises(ValueError):
            replace(bootstrap, **changes)
        assert not requests


@pytest.mark.anyio
@pytest.mark.parametrize("decision", [False, None, 1, "true"])
async def test_revocation_sends_nothing_and_cannot_be_retried(decision):
    async def guard():
        return decision

    async with _bootstrap(guard=guard) as (bootstrap, requests):
        with pytest.raises(BootstrapError, match="lease_unavailable"):
            await bootstrap.run()
        with pytest.raises(BootstrapError, match="already_attempted"):
            await bootstrap.run()
        assert not requests


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("private-mailbox-secret"),
        BootstrapError("private-mailbox-secret"),
        None,
        "bad-code",
    ],
)
async def test_mailbox_failure_preserves_partial_creation_without_secrets(failure):
    async def mailbox(*_):
        if isinstance(failure, Exception):
            raise failure
        return failure

    async with _bootstrap(mailbox=mailbox) as (bootstrap, requests):
        with pytest.raises(BootstrapError) as caught:
            await bootstrap.run()
        assert "private-mailbox-secret" not in "".join(traceback.format_exception(caught.value))
        assert len(requests) == len(bootstrap.observations) == 1
        assert bootstrap.observations[0].step_code == "source.registration"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("route", "key", "value"),
    [
        ("/identity/registrations", "intent_id", "../../secret"),
        ("/workspaces", "workspace", {"workspace_id": "wsp_" + "0" * 32}),
        ("/projects", "project", {"project_id": "prj_" + "1" * 32, "tenant_id": None}),
        ("/membership-invitations", "accept_token", "invalid"),
    ],
)
async def test_malformed_response_stops_at_that_request(route, key, value):
    def handler(request, body):
        if request.url.path == route:
            body[key] = value

    async with _bootstrap(handler) as (bootstrap, requests):
        with pytest.raises(BootstrapError):
            await bootstrap.run()
        assert requests[-1].url.path == route


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "tenant",
        "application",
        "revision_bool",
        "revision_zero",
        "membership_duplicate",
        "role",
        "workspace",
    ],
)
async def test_foreign_owner_and_membership_contracts_are_rejected(case):
    def handler(request, body):
        if case == "tenant" and request.url.path == "/projects":
            body["project"]["tenant_id"] = "ten_" + "1" * 32
        if case == "application" and request.url.path == "/applications":
            body["application"]["project_id"] = "prj_" + "1" * 32
        if case == "workspace" and request.url.path == "/identity/workspace-switches":
            body["context"]["workspace_id"] = "wsp_" + "f" * 32
        if request.url.path == "/memberships":
            member = body["memberships"][0]
            if case == "revision_bool":
                member["revision"] = True
            elif case == "revision_zero":
                member["revision"] = 0
            elif case == "membership_duplicate":
                body["memberships"].append(member.copy())
            elif case == "role":
                member["role"] = "viewer"

    async with _bootstrap(handler) as (bootstrap, _):
        with pytest.raises(BootstrapError):
            await bootstrap.run()
        assert bootstrap.observations


@pytest.mark.anyio
async def test_revoked_after_creation_response_does_not_mean_no_resources():
    allowed = True

    def handler(*_):
        nonlocal allowed
        allowed = False

    async def guard():
        return allowed

    async with _bootstrap(handler, guard=guard) as (bootstrap, requests):
        with pytest.raises(BootstrapError, match="lease_unavailable"):
            await bootstrap.run()
        assert len(requests) == 1
        assert bootstrap.attempted and not bootstrap.observations


@pytest.mark.anyio
async def test_cancellation_and_concurrent_attempt_are_fenced():
    entered = asyncio.Event()

    async def guard():
        entered.set()
        await asyncio.Event().wait()

    async with _bootstrap(guard=guard) as (bootstrap, requests):
        task = asyncio.create_task(bootstrap.run())
        await entered.wait()
        with pytest.raises(BootstrapError, match="already_attempted"):
            await bootstrap.run()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bootstrap._fenced and not requests


@pytest.mark.anyio
async def test_deadline_bounds_hanging_guard():
    async def guard():
        await asyncio.Event().wait()

    async with _bootstrap(guard=guard, deadline=datetime.now(UTC) + timedelta(milliseconds=20)) as (
        bootstrap,
        requests,
    ):
        with pytest.raises(BootstrapError):
            await bootstrap.run()
        assert bootstrap._fenced and not requests


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["mailbox", "response"])
async def test_per_operation_timeout_closes_and_fences(stage, monkeypatch):
    monkeypatch.setattr("metahub_assurance_runner.bootstrap.REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("metahub_assurance_runner.transport.REQUEST_TIMEOUT_SECONDS", 0.01)

    class HangingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = HangingStream()

    async def mailbox(*_):
        await asyncio.Event().wait()

    handler = (lambda *_: httpx.Response(200, stream=stream)) if stage == "response" else None
    async with _bootstrap(handler, mailbox=mailbox) as (bootstrap, requests):
        with pytest.raises(BootstrapError, match="bootstrap.unavailable"):
            await bootstrap.run()
        assert bootstrap._fenced and len(requests) == 1
        assert len(bootstrap.observations) == (1 if stage == "mailbox" else 0)
        if stage == "response":
            assert stream.closed
