"""Run fixed HTTP isolation probes with real sessions and per-request fencing.

This module does not provision or attest an environment. Its clients and fixture
manifest must come from the isolated coordinator, never an assessment HTTP body.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from metahub_shared.config_distribution import canonical_json_bytes

from .transport import MAX_RESPONSE_BYTES as MAX_RESPONSE_BYTES
from .transport import REQUEST_TIMEOUT_SECONDS as REQUEST_TIMEOUT_SECONDS
from .transport import guarded_request

GLOBAL_CSRF = "__Host-metahub-global-csrf"
WORKSPACE_CSRF = "__Host-metahub-workspace-csrf"
WORKSPACE_SESSION = "__Host-metahub-workspace-session"
SCENARIOS = (
    "api_idor",
    "write_authorization",
    "cache_owner",
    "queue_owner",
    "object_prefix",
    "metrics_labels",
    "dynamic_revocation",
    "replay_fencing",
    "late_response",
    "input_fuzz",
)
IMPLEMENTED_SCENARIOS = (
    "api_idor",
    "write_authorization",
    "dynamic_revocation",
    "replay_fencing",
    "input_fuzz",
)
LeaseGuard = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True, repr=False)
class ProbeFixtures:
    source: httpx.AsyncClient
    target: httpx.AsyncClient
    member: httpx.AsyncClient
    anonymous: httpx.AsyncClient
    origin: str
    source_workspace_id: str
    target_workspace_id: str
    source_project_id: str
    target_project_id: str
    target_application_id: str
    target_project_name: str
    member_membership_id: str
    member_revision: int

    def __post_init__(self):
        for prefix, value in (
            ("wsp", self.source_workspace_id),
            ("wsp", self.target_workspace_id),
            ("prj", self.source_project_id),
            ("prj", self.target_project_id),
            ("app", self.target_application_id),
            ("wmb", self.member_membership_id),
        ):
            if (
                re.fullmatch(prefix + r"_[0-9a-f]{32}", value) is None
                or value == prefix + "_" + "0" * 32
            ):
                raise ValueError("invalid isolated fixture identity")
        if (
            self.source_workspace_id == self.target_workspace_id
            or self.source_project_id == self.target_project_id
            or type(self.member_revision) is not int
            or self.member_revision < 1
            or not 1 <= len(self.target_project_name) <= 120
        ):
            raise ValueError("isolated subjects must differ")
        clients = (self.source, self.target, self.member, self.anonymous)
        if (
            len({id(client) for client in clients}) != 4
            or any(
                client.base_url.copy_with(path="/") != self.source.base_url.copy_with(path="/")
                or client.base_url.path != "/"
                for client in clients
            )
            or any(client.follow_redirects for client in clients)
            or any(client.trust_env or client.is_closed or client.params for client in clients)
            or any(client.auth is not None for client in clients)
            or any("authorization" in client.headers for client in clients)
            or any(
                name in client.headers
                for client in clients
                for name in (
                    "cookie",
                    "proxy-authorization",
                    "host",
                    "forwarded",
                    "x-forwarded-host",
                    "x-forwarded-for",
                )
            )
            or bool(self.anonymous.cookies)
            or self.source.base_url.scheme != "https"
            or self.source.base_url.username
            or self.source.base_url.password
            or self.source.base_url.path != "/"
            or self.source.base_url.query
            or self.source.base_url.fragment
        ):
            raise ValueError("invalid isolated client boundary")
        parsed_origin = httpx.URL(self.origin)
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.host
            or parsed_origin.username
            or parsed_origin.password
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.copy_with(path="/") != self.source.base_url.copy_with(path="/")
        ):
            raise ValueError("invalid fixture origin")


@dataclass(frozen=True, slots=True)
class ProbeCheck:
    code: str
    http_status: int | None
    satisfied: bool


@dataclass(frozen=True, slots=True)
class ProbeResult:
    scenario_code: str
    outcome: Literal["passed", "failed", "error"]
    reason_code: str
    started_at: datetime
    finished_at: datetime
    checks: tuple[ProbeCheck, ...]
    evidence_sha256: str

    @property
    def duration_ms(self) -> int:
        return (self.finished_at - self.started_at) // timedelta(milliseconds=1)


class _ProbeFailure(Exception):
    pass


class _LeaseRevoked(Exception):
    pass


@dataclass(slots=True)
class FixedProbeRunner:
    fixtures: ProbeFixtures = field(repr=False)
    lease_guard: LeaseGuard = field(repr=False)
    deadline_at: datetime
    _checks: list[ProbeCheck] = field(default_factory=list, init=False, repr=False)
    _running: bool = field(default=False, init=False)
    _fenced: bool = field(default=False, init=False)

    def __post_init__(self):
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("runner deadline must be timezone aware")

    async def run(self, scenario_code: str) -> ProbeResult:
        if scenario_code not in SCENARIOS or self._running:
            raise ValueError("invalid or concurrent fixed scenario")
        started = datetime.now(UTC)
        self._checks = []
        self._running = True
        outcome, reason = "error", "scenario_not_implemented"
        try:
            if scenario_code in IMPLEMENTED_SCENARIOS:
                if self._fenced:
                    raise _LeaseRevoked
                operation = {
                    "api_idor": self._api_idor,
                    "write_authorization": self._write_authorization,
                    "dynamic_revocation": self._dynamic_revocation,
                    "replay_fencing": self._replay_fencing,
                    "input_fuzz": self._input_fuzz,
                }[scenario_code]
                remaining = (self.deadline_at - datetime.now(UTC)).total_seconds()
                async with asyncio.timeout(max(0, remaining)):
                    await self._check_lease()
                    await operation()
                    await self._check_lease()
                outcome, reason = "passed", "checks_satisfied"
        except asyncio.CancelledError:
            self._fenced = True
            raise
        except TimeoutError:
            self._fenced = True
            reason = "probe_timeout"
        except _LeaseRevoked:
            self._fenced = True
            reason = "lease_revoked"
        except _ProbeFailure:
            outcome, reason = "failed", "contract_violation"
        except (
            httpx.HTTPError,
            httpx.CookieConflict,
            ValueError,
            KeyError,
            TypeError,
            RecursionError,
        ):
            reason = "probe_unavailable"
        finally:
            self._running = False
        finished = datetime.now(UTC)
        if finished < started:
            finished, outcome, reason = started, "error", "clock_invalid"
        evidence = {
            "schema_version": "metahub.assurance-probe.v1",
            "scenario_code": scenario_code,
            "outcome": outcome,
            "reason_code": reason,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "checks": [asdict(check) for check in self._checks],
        }
        return ProbeResult(
            scenario_code=scenario_code,
            outcome=outcome,
            reason_code=reason,
            started_at=started,
            finished_at=finished,
            checks=tuple(self._checks),
            evidence_sha256=hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
        )

    async def _check_lease(self):
        if self._fenced or datetime.now(UTC) >= self.deadline_at:
            raise _LeaseRevoked
        try:
            allowed = await self.lease_guard()
        except Exception:
            raise _LeaseRevoked from None
        if allowed is not True or datetime.now(UTC) >= self.deadline_at:
            raise _LeaseRevoked

    def _check(self, code: str, satisfied: bool, response: httpx.Response | None = None):
        self._checks.append(ProbeCheck(code, response.status_code if response else None, satisfied))
        if not satisfied:
            raise _ProbeFailure

    async def _request(self, client, method, path, **kwargs):
        return await guarded_request(self._check_lease, client, method, path, **kwargs)

    def _headers(self, client, *, global_session=False):
        csrf = client.cookies.get(GLOBAL_CSRF if global_session else WORKSPACE_CSRF)
        if not csrf:
            raise ValueError("fixture session missing")
        return {"Origin": self.fixtures.origin, "Sec-Fetch-Site": "same-site", "X-CSRF-Token": csrf}

    async def _owner(self, client, workspace_id, label):
        response = await self._request(client, "GET", "/workspace/session")
        self._check(label + ".authenticated", response.status_code == 200, response)
        self._check(
            label + ".workspace_bound", response.json()["context"]["workspace_id"] == workspace_id
        )
        return response.json()

    def _project(self, response, project_id, label):
        projects = [
            item for item in response.json()["projects"] if item["project_id"] == project_id
        ]
        self._check(label, len(projects) == 1)
        return projects[0]

    def _session_cookie(self, client):
        cookie = client.cookies.get(WORKSPACE_SESSION)
        if not isinstance(cookie, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie) is None:
            raise ValueError("fixture session missing")
        return cookie

    async def _api_idor(self):
        f = self.fixtures
        await self._owner(f.source, f.source_workspace_id, "source")
        await self._owner(f.target, f.target_workspace_id, "target")
        source_read = await self._request(
            f.source,
            "GET",
            "/applications",
            params={"project_id": f.source_project_id},
        )
        self._check("source.application_read", source_read.status_code == 200, source_read)
        legitimate = await self._request(
            f.target,
            "GET",
            "/applications",
            params={"project_id": f.target_project_id},
        )
        self._check("target.application_read", legitimate.status_code == 200, legitimate)
        self._check(
            "target.application_present",
            any(
                item["application_id"] == f.target_application_id
                for item in legitimate.json()["applications"]
            ),
        )
        foreign = await self._request(
            f.source,
            "GET",
            "/applications",
            params={"project_id": f.target_project_id},
        )
        absent = await self._request(
            f.source,
            "GET",
            "/applications",
            params={"project_id": "prj_" + "0" * 32},
        )
        self._check(
            "foreign.application_denied", foreign.status_code == absent.status_code == 404, foreign
        )
        self._check(
            "foreign.indistinguishable",
            all(foreign.json()[key] == absent.json()[key] for key in ("code", "message")),
        )

    async def _write_authorization(self):
        f = self.fixtures
        await self._owner(f.source, f.source_workspace_id, "source")
        await self._owner(f.target, f.target_workspace_id, "target")
        before = await self._request(f.target, "GET", "/projects")
        self._check("target.project_read", before.status_code == 200, before)
        initial = self._project(before, f.target_project_id, "target.project_present")
        self._check("target.initial_name", initial["name"] == f.target_project_name)
        control_name = self._control_name(initial["name"])
        writable = await self._request(
            f.target,
            "PATCH",
            f"/projects/{f.target_project_id}",
            json={"name": control_name},
            headers=self._headers(f.target),
        )
        self._check("target.project_write", writable.status_code == 200, writable)
        written = await self._request(f.target, "GET", "/projects")
        self._check("target.control_read", written.status_code == 200, written)
        original = self._project(written, f.target_project_id, "target.control_present")
        self._check("target.write_applied", original["name"] == control_name)
        foreign = await self._request(
            f.source,
            "PATCH",
            f"/projects/{f.target_project_id}",
            json={"name": "assurance-unauthorized-write"},
            headers=self._headers(f.source),
        )
        self._check("foreign.project_write_denied", foreign.status_code == 404, foreign)
        after = await self._request(f.target, "GET", "/projects")
        self._check("target.project_read_after", after.status_code == 200, after)
        current = self._project(after, f.target_project_id, "target.project_present_after")
        self._check("target.project_unchanged", current == original)

    @staticmethod
    def _control_name(original):
        first, second = "assurance-authorized-control-a", "assurance-authorized-control-b"
        return second if original == first else first

    async def _switch(self, client, workspace_id):
        response = await self._request(
            client,
            "POST",
            "/identity/workspace-switches",
            json={"workspace_id": workspace_id},
            headers=self._headers(client, global_session=True),
        )
        self._check("session.switch", response.status_code == 200, response)

    async def _dynamic_revocation(self):
        f = self.fixtures
        initial = await self._owner(f.member, f.source_workspace_id, "member")
        self._check("member.initial_role", initial["context"]["role"] == "admin")
        before = await self._request(f.member, "GET", "/projects")
        self._check("member.initial_read", before.status_code == 200, before)
        project = self._project(before, f.source_project_id, "member.project_present")
        control_name = self._control_name(project["name"])
        writable = await self._request(
            f.member,
            "PATCH",
            f"/projects/{f.source_project_id}",
            json={"name": control_name},
            headers=self._headers(f.member),
        )
        self._check("member.initial_write", writable.status_code == 200, writable)
        owner_read = await self._request(f.source, "GET", "/projects")
        self._check("member.control_read", owner_read.status_code == 200, owner_read)
        original = self._project(owner_read, f.source_project_id, "member.control_present")
        self._check("member.write_applied", original["name"] == control_name)
        old_cookie = self._session_cookie(f.member)
        changed = await self._request(
            f.source,
            "PATCH",
            f"/memberships/{f.member_membership_id}",
            json={"role": "viewer"},
            headers={**self._headers(f.source), "If-Match": f'"{f.member_revision}"'},
        )
        self._check("member.downgrade", changed.status_code == 200, changed)
        revoked = await self._request(
            f.anonymous,
            "GET",
            "/workspace/session",
            headers={"Cookie": f"{WORKSPACE_SESSION}={old_cookie}"},
        )
        self._check("member.old_session_revoked", revoked.status_code == 401, revoked)
        await self._switch(f.member, f.source_workspace_id)
        current = await self._owner(f.member, f.source_workspace_id, "member.after_downgrade")
        self._check("member.current_role", current["context"]["role"] == "viewer")
        denied = await self._request(
            f.member,
            "PATCH",
            f"/projects/{f.source_project_id}",
            json={"name": "assurance-denied"},
            headers=self._headers(f.member),
        )
        self._check("member.viewer_write_denied", denied.status_code == 403, denied)
        after = await self._request(f.source, "GET", "/projects")
        self._check("member.project_read_after", after.status_code == 200, after)
        current_project = self._project(after, f.source_project_id, "member.project_present_after")
        self._check("member.project_unchanged", current_project == original)

    async def _replay_fencing(self):
        f = self.fixtures
        await self._owner(f.source, f.source_workspace_id, "source")
        old_cookie = self._session_cookie(f.source)
        await self._switch(f.source, f.source_workspace_id)
        self._check("session.token_rotated", self._session_cookie(f.source) != old_cookie)
        replay = await self._request(
            f.anonymous,
            "GET",
            "/projects",
            headers={"Cookie": f"{WORKSPACE_SESSION}={old_cookie}"},
        )
        self._check("session.replay_denied", replay.status_code == 401, replay)
        legitimate = await self._request(f.source, "GET", "/projects")
        self._check("session.current_read", legitimate.status_code == 200, legitimate)

    async def _input_fuzz(self):
        f = self.fixtures
        await self._owner(f.source, f.source_workspace_id, "source")
        before = self._session_cookie(f.source)
        payloads = (
            {"workspace_id": f.source_workspace_id, "target_url": "https://invalid.example"},
            {"workspace_id": "../" + f.target_workspace_id},
            {"workspace_id": [f.source_workspace_id]},
            {"workspace_id": "x" * 17000},
        )
        for index, payload in enumerate(payloads):
            response = await self._request(
                f.source,
                "POST",
                "/identity/workspace-switches",
                json=payload,
                headers=self._headers(f.source, global_session=True),
            )
            self._check(
                f"input.case_{index}.rejected",
                response.status_code == 422
                and response.json()["code"] == "identity.request_invalid",
                response,
            )
        duplicate = await self._request(
            f.source,
            "POST",
            "/identity/workspace-switches",
            content='{"workspace_id":"first","workspace_id":"second"}',
            headers={
                **self._headers(f.source, global_session=True),
                "Content-Type": "application/json",
            },
        )
        self._check(
            "input.duplicate_rejected",
            duplicate.status_code == 422 and duplicate.json()["code"] == "identity.request_invalid",
            duplicate,
        )
        self._check("input.session_unchanged", f.source.cookies.get(WORKSPACE_SESSION) == before)
        await self._owner(f.source, f.source_workspace_id, "source.after_fuzz")
