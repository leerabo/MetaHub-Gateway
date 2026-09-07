"""Create probe subjects through real identity and Workspace HTTP contracts.

The caller owns an exclusive sandbox and its teardown. Even a response lost before
acknowledgement may have created resources; an empty journal never proves cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from .probes import GLOBAL_CSRF, WORKSPACE_CSRF, LeaseGuard, ProbeFixtures
from .transport import REQUEST_TIMEOUT_SECONDS, guarded_request

Mailbox = Callable[[str, str], Awaitable[str]]


class BootstrapError(RuntimeError):
    """A fixed error without response bodies, credentials or underlying exceptions."""


@dataclass(frozen=True, slots=True)
class CreationObservation:
    step_code: str
    identity_sha256: str
    observed_at: datetime


@dataclass(slots=True, repr=False)
class HttpFixtureBootstrap:
    source: httpx.AsyncClient
    target: httpx.AsyncClient
    member: httpx.AsyncClient
    anonymous: httpx.AsyncClient
    origin: str
    environment_id: str
    mailbox: Mailbox
    lease_guard: LeaseGuard
    deadline_at: datetime
    observations: list[CreationObservation] = field(default_factory=list, init=False)
    attempted: bool = field(default=False, init=False)
    _fenced: bool = field(default=False, init=False)

    def __post_init__(self):
        clients = (self.source, self.target, self.member, self.anonymous)
        origin = httpx.URL(self.origin)
        if (
            re.fullmatch(r"aenv_[0-9a-f]{32}", self.environment_id) is None
            or origin.scheme != "https"
            or not origin.host
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in ("", "/")
            or self.deadline_at.tzinfo is None
            or self.deadline_at.utcoffset() is None
            or len({id(client) for client in clients}) != 4
            or any(
                client.base_url.copy_with(path="/") != origin.copy_with(path="/")
                or client.base_url.path != "/"
                for client in clients
            )
            or any(
                client.follow_redirects
                or client.trust_env
                or client.auth is not None
                or client.cookies
                or client.params
                or client.is_closed
                for client in clients
            )
            or any(
                name in client.headers
                for client in clients
                for name in (
                    "authorization",
                    "cookie",
                    "proxy-authorization",
                    "host",
                    "forwarded",
                    "x-forwarded-host",
                    "x-forwarded-for",
                )
            )
        ):
            raise ValueError("invalid isolated bootstrap boundary")
        self.origin = str(origin.copy_with(path=""))

    async def _guard(self):
        if self._fenced or datetime.now(UTC) >= self.deadline_at:
            self._fenced = True
            raise BootstrapError("bootstrap.lease_unavailable")
        try:
            allowed = await self.lease_guard()
        except Exception:
            allowed = False
        if allowed is not True or datetime.now(UTC) >= self.deadline_at:
            self._fenced = True
            raise BootstrapError("bootstrap.lease_unavailable")

    def _headers(self, client=None, *, workspace=False):
        result = {"Origin": self.origin, "Sec-Fetch-Site": "same-site"}
        if client is not None:
            token = client.cookies.get(WORKSPACE_CSRF if workspace else GLOBAL_CSRF)
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
                raise BootstrapError("bootstrap.session_missing")
            result["X-CSRF-Token"] = token
        return result

    async def _request(self, client, method, path, expected, **kwargs):
        response = await guarded_request(self._guard, client, method, path, **kwargs)
        if response.status_code != expected:
            raise BootstrapError("bootstrap.contract_rejected")
        body = response.json()
        if not isinstance(body, dict):
            raise BootstrapError("bootstrap.response_invalid")
        return body

    @staticmethod
    def _id(value, prefix):
        if not isinstance(value, str) or re.fullmatch(prefix + r"_[0-9a-f]{32}", value) is None:
            raise BootstrapError("bootstrap.identity_invalid")
        if value == prefix + "_" + "0" * 32:
            raise BootstrapError("bootstrap.identity_invalid")
        return value

    def _record(self, step, identity):
        self.observations.append(
            CreationObservation(
                step,
                hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                datetime.now(UTC),
            )
        )

    async def _register(self, client, label, invitation=None):
        email = f"{label}-{self.environment_id[5:]}@example.test"
        password = "Pr0be!" + secrets.token_urlsafe(32)
        payload = {
            "email": email,
            "display_name": f"Assurance {label}",
            "password": password,
            "consent_version": "privacy-2026-08",
        }
        if invitation is not None:
            payload["invitation_token"] = invitation
        pending = await self._request(
            client,
            "POST",
            "/identity/registrations",
            200,
            json=payload,
            headers=self._headers(),
        )
        intent_id = self._id(pending["intent_id"], "rgi")
        self._record(f"{label}.registration", intent_id)
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            await self._guard()
            code = await self.mailbox(intent_id, email)
            await self._guard()
        if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
            raise BootstrapError("bootstrap.mailbox_unavailable")
        await self._request(
            client,
            "POST",
            "/identity/verifications",
            200,
            json={"intent_id": intent_id, "code": code},
            headers=self._headers(),
        )
        logged_in = await self._request(
            client,
            "POST",
            "/identity/login",
            200,
            json={"email": email, "password": password},
            headers=self._headers(),
        )
        if logged_in["account"]["email"] != email or logged_in["account"]["status"] != "active":
            raise BootstrapError("bootstrap.account_mismatch")
        self._record(f"{label}.verified_account", email)

    async def _switch(self, client, workspace_id, role):
        result = await self._request(
            client,
            "POST",
            "/identity/workspace-switches",
            200,
            json={"workspace_id": workspace_id},
            headers=self._headers(client),
        )
        if result["context"]["workspace_id"] != workspace_id or result["context"]["role"] != role:
            raise BootstrapError("bootstrap.owner_mismatch")

    async def _workspace(self, client, label):
        workspace = (
            await self._request(
                client,
                "POST",
                "/workspaces",
                201,
                json={
                    "kind": "organization",
                    "name": f"Assurance {label}",
                    "slug": f"asr-{label}-{self.environment_id[5:]}",
                    "region": "cn-north-1",
                    "service_mode": "internal_only",
                },
                headers={**self._headers(client), "Idempotency-Key": uuid4().hex},
            )
        )["workspace"]
        workspace_id = self._id(workspace["workspace_id"], "wsp")
        self._record(f"{label}.workspace", workspace_id)
        if workspace["kind"] != "organization" or workspace["status"] != "active":
            raise BootstrapError("bootstrap.workspace_invalid")
        if (
            workspace["slug"] != f"asr-{label}-{self.environment_id[5:]}"
            or workspace["region"] != "cn-north-1"
            or workspace["service_mode"] != "internal_only"
        ):
            raise BootstrapError("bootstrap.workspace_invalid")
        await self._switch(client, workspace_id, "owner")
        project = (
            await self._request(
                client,
                "POST",
                "/projects",
                201,
                json={"project_code": f"probe-{label}", "name": f"Probe {label}"},
                headers=self._headers(client, workspace=True),
            )
        )["project"]
        self._record(f"{label}.project", self._id(project["project_id"], "prj"))
        self._id(project["tenant_id"], "ten")
        if (
            project["project_code"] != f"probe-{label}"
            or project["name"] != f"Probe {label}"
            or project["status"] != "active"
        ):
            raise BootstrapError("bootstrap.project_invalid")
        return workspace_id, project

    async def run(self) -> ProbeFixtures:
        if self.attempted:
            raise BootstrapError("bootstrap.already_attempted")
        self.attempted = True
        try:
            remaining = (self.deadline_at - datetime.now(UTC)).total_seconds()
            async with asyncio.timeout(max(0, remaining)):
                await self._guard()
                await self._register(self.source, "source")
                await self._register(self.target, "target")
                source_workspace, source_project = await self._workspace(self.source, "source")
                target_workspace, target_project = await self._workspace(self.target, "target")
                if source_project["tenant_id"] == target_project["tenant_id"]:
                    raise BootstrapError("bootstrap.owner_mismatch")
                application = (
                    await self._request(
                        self.target,
                        "POST",
                        "/applications",
                        201,
                        json={
                            "project_id": target_project["project_id"],
                            "application_code": "probe-target",
                            "name": "Probe target application",
                        },
                        headers=self._headers(self.target, workspace=True),
                    )
                )["application"]
                application_id = self._id(application["application_id"], "app")
                self._record("target.application", application_id)
                if (
                    application["project_id"] != target_project["project_id"]
                    or application["tenant_id"] != target_project["tenant_id"]
                ):
                    raise BootstrapError("bootstrap.owner_mismatch")
                if (
                    application["application_code"] != "probe-target"
                    or application["status"] != "active"
                ):
                    raise BootstrapError("bootstrap.application_invalid")
                invitation = await self._request(
                    self.source,
                    "POST",
                    "/membership-invitations",
                    201,
                    json={
                        "email": f"member-{self.environment_id[5:]}@example.test",
                        "role": "admin",
                        "expires_in_hours": 24,
                    },
                    headers=self._headers(self.source, workspace=True),
                )
                self._record(
                    "member.invitation",
                    self._id(
                        invitation["invitation"]["invitation_id"],
                        "min",
                    ),
                )
                if (
                    invitation["invitation"]["role"] != "admin"
                    or invitation["invitation"]["status"] != "pending"
                ):
                    raise BootstrapError("bootstrap.invitation_invalid")
                token = invitation["accept_token"]
                if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
                    raise BootstrapError("bootstrap.invitation_invalid")
                await self._register(self.member, "member", invitation=token)
                await self._switch(self.member, source_workspace, "admin")
                memberships = await self._request(self.member, "GET", "/memberships", 200)
                current = [
                    item for item in memberships["memberships"] if item["is_current"] is True
                ]
                if (
                    len(current) != 1
                    or current[0]["role"] != "admin"
                    or current[0]["status"] != "active"
                ):
                    raise BootstrapError("bootstrap.membership_invalid")
                membership = current[0]
                if type(membership["revision"]) is not int or membership["revision"] < 1:
                    raise BootstrapError("bootstrap.membership_invalid")
                membership_id = self._id(membership["membership_id"], "wmb")
                self._record("member.membership", membership_id)
                await self._guard()
                return ProbeFixtures(
                    self.source,
                    self.target,
                    self.member,
                    self.anonymous,
                    origin=self.origin,
                    source_workspace_id=source_workspace,
                    target_workspace_id=target_workspace,
                    source_project_id=source_project["project_id"],
                    target_project_id=target_project["project_id"],
                    target_application_id=application_id,
                    target_project_name=target_project["name"],
                    member_membership_id=membership_id,
                    member_revision=membership["revision"],
                )
        except asyncio.CancelledError:
            self._fenced = True
            raise
        except BootstrapError as error:
            self._fenced = True
            safe_codes = {
                "lease_unavailable",
                "session_missing",
                "contract_rejected",
                "response_invalid",
                "identity_invalid",
                "mailbox_unavailable",
                "account_mismatch",
                "owner_mismatch",
                "workspace_invalid",
                "project_invalid",
                "application_invalid",
                "invitation_invalid",
                "membership_invalid",
            }
            code = str(error)
            if code not in {f"bootstrap.{value}" for value in safe_codes}:
                code = "bootstrap.unavailable"
            raise BootstrapError(code) from None
        except Exception:
            self._fenced = True
            raise BootstrapError("bootstrap.unavailable") from None
