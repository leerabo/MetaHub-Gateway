"""Real privacy-export jobs in the fixed private guest, with read-only observations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.twofactor.totp import TOTP
from metahub_control.privacy_center import service
from metahub_control.privacy_center.crypto import decrypt_identity_export
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .resources import (
    LocalResourceInspector,
    ResourceContractViolation,
    ResourceObservation,
    ResourceUnavailable,
    _digest,
    _identity,
)

_DOMAINS = {"identity", "membership", "audit", "billing", "support", "edge", "data_governance"}


@dataclass(frozen=True, slots=True, repr=False)
class PrivacyQueueSubject:
    workspace_id: str
    tenant_id: str
    account_id: str
    request_id: str

    def __post_init__(self):
        for value, prefix in (
            (self.workspace_id, "wsp"),
            (self.tenant_id, "ten"),
            (self.account_id, "gac"),
            (self.request_id, "dsr"),
        ):
            _identity(value, prefix)

    def owns(self, claim):
        return isinstance(claim, service.PrivacyOwnerJobLease) and (
            claim.workspace_id,
            claim.tenant_id,
            claim.account_id,
            claim.data_subject_request_id,
        ) == (self.workspace_id, self.tenant_id, self.account_id, self.request_id)


@dataclass(frozen=True, slots=True, repr=False)
class LocalPrivacyQueue:
    config: AppConfig
    inspector: LocalResourceInspector

    def __post_init__(self):
        if (
            self.config.database_url != f"sqlite:///{self.inspector.database.as_posix()}"
            or self.config.environment != "test"
            or self.config.control_background_schedulers_enabled
            or not self.config.tenant_data_encryption_secret
        ):
            raise ResourceUnavailable("resource.privacy_profile_invalid")

    def _rows(self, subject):
        with closing(self.inspector._read()) as connection:
            request = connection.execute(
                "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
                (subject.request_id,),
            ).fetchone()
            jobs = connection.execute(
                "SELECT * FROM data_subject_request_owner_jobs WHERE data_subject_request_id=? "
                "ORDER BY owner_domain LIMIT 8",
                (subject.request_id,),
            ).fetchall()
            exports = connection.execute(
                "SELECT * FROM identity_exports WHERE data_subject_request_id=? LIMIT 2",
                (subject.request_id,),
            ).fetchall()
            account = connection.execute(
                "SELECT account_id,email_normalized,display_name FROM global_accounts "
                "WHERE account_id=?",
                (subject.account_id,),
            ).fetchone()
        if request is None or account is None:
            raise ResourceContractViolation("resource.privacy_subject_missing")
        return (
            dict(request),
            [dict(row) for row in jobs],
            [dict(row) for row in exports],
            dict(account),
        )

    def subject(self, workspace_id, tenant_id, email, request_id):
        # Public identity DTOs deliberately omit account IDs. Resolve only the
        # actual registered subject in this private guest, without changing auth.
        with closing(self.inspector._read()) as connection:
            rows = connection.execute(
                "SELECT account.account_id FROM global_accounts account "
                "JOIN workspace_memberships member ON member.account_id=account.account_id "
                "WHERE account.email_normalized=? AND member.workspace_id=? AND member.tenant_id=? "
                "AND member.status='active' AND member.role_code='owner' LIMIT 2",
                (email, workspace_id, tenant_id),
            ).fetchall()
        if len(rows) != 1:
            raise ResourceContractViolation("resource.privacy_registered_owner_missing")
        return PrivacyQueueSubject(workspace_id, tenant_id, rows[0][0], request_id)

    def state(self, subjects):
        return _digest([self._rows(subject) for subject in subjects])

    def require_exclusive_queue(self, subjects):
        with closing(self.inspector._read()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT data_subject_request_id FROM data_subject_request_owner_jobs "
                "WHERE status IN('pending','running') LIMIT 3"
            ).fetchall()
        if {row[0] for row in rows} != {subject.request_id for subject in subjects}:
            raise ResourceContractViolation("resource.privacy_queue_not_exclusive")

    def observe(self, subject, phase):
        request, jobs, exports, account = self._rows(subject)
        owner = (subject.workspace_id, subject.tenant_id, subject.account_id)
        checks = [
            (
                "seven_owner_jobs",
                len(jobs) == 7 and {job["owner_domain"] for job in jobs} == _DOMAINS,
            ),
            (
                "compound_owner_bound",
                all(
                    (row["workspace_id"], row["tenant_id"], row["account_id"]) == owner
                    and row["data_subject_request_id"] == subject.request_id
                    for row in [request, *jobs, *exports]
                ),
            ),
            ("export_request", request["request_kind"] == "export"),
        ]
        if phase == "pending":
            checks.extend(
                (
                    (
                        "pending_not_executed",
                        request["status"] == "received"
                        and not exports
                        and all(
                            job["status"] == "pending"
                            and job["attempt_count"] == 0
                            and job["lease_owner_id"] is None
                            and job["export_fragment_json"] is None
                            for job in jobs
                        ),
                    ),
                )
            )
        elif phase in ("ready", "downloaded"):
            checks.extend(
                (
                    (
                        "completed_once",
                        all(
                            job["status"] == "completed"
                            and job["attempt_count"] == 1
                            and job["revision"] == 3
                            and job["outcome_code"] == "exported"
                            and job["lease_owner_id"] is None
                            and job["lease_token_sha256"] is None
                            and job["lease_expires_at"] is None
                            and job["completed_at"] is not None
                            and re.fullmatch(r"[0-9a-f]{64}", job["executor_receipt_sha256"] or "")
                            for job in jobs
                        ),
                    ),
                    ("one_encrypted_export", len(exports) == 1),
                    (
                        "request_state",
                        request["status"] == ("ready" if phase == "ready" else "completed"),
                    ),
                )
            )
            if len(exports) != 1:
                raise ResourceContractViolation("resource.privacy_export_missing")
            export = exports[0]
            values = dict(
                export_id=export["identity_export_id"],
                workspace_id=subject.workspace_id,
                tenant_id=subject.tenant_id,
                account_id=subject.account_id,
                plaintext_sha256=export["plaintext_sha256"],
                artifact_sha256=export["artifact_sha256"],
            )
            plaintext = decrypt_identity_export(
                self.config,
                export["artifact_ciphertext_b64"],
                export["artifact_nonce_b64"],
                **values,
            )
            artifact = json.loads(plaintext)
            fragments = {
                job["owner_domain"]: json.loads(job["export_fragment_json"]) for job in jobs
            }
            manifest = [
                {
                    "owner_domain": job["owner_domain"],
                    "adapter_version": job["adapter_version"],
                    "outcome": job["outcome_code"],
                    "fragment_sha256": service._digest(
                        fragments[job["owner_domain"]],
                    ),
                }
                for job in jobs
            ]
            checks.extend(
                (
                    (
                        "owner_receipts_match",
                        all(
                            job["executor_receipt_sha256"]
                            == service._digest(
                                {
                                    "schema_version": 1,
                                    "request_id": subject.request_id,
                                    "owner_domain": job["owner_domain"],
                                    "adapter_version": job["adapter_version"],
                                    "outcome": "exported",
                                    "fragment_sha256": service._digest(
                                        fragments[job["owner_domain"]]
                                    ),
                                    "policy_generation": job["policy_generation"],
                                    "hold_generation": job["hold_generation"],
                                }
                            )
                            for job in jobs
                        ),
                    ),
                    (
                        "manifest_matches_fragments",
                        json.loads(export["owner_manifest_json"]) == manifest
                        and service._digest(manifest) == export["owner_manifest_sha256"],
                    ),
                    (
                        "nonempty_real_identity",
                        artifact["owners"]["identity"]["account_id"] == subject.account_id
                        and artifact["owners"]["identity"]["email"] == account["email_normalized"]
                        and artifact["owners"]["identity"]["display_name"]
                        == account["display_name"],
                    ),
                    (
                        "nonempty_real_membership",
                        artifact["owners"]["membership"]["workspace_id"] == subject.workspace_id
                        and artifact["owners"]["membership"]["role"] == "owner"
                        and artifact["owners"]["membership"]["status"] == "active",
                    ),
                    (
                        "all_fragments_assembled",
                        artifact["owners"] == fragments and set(fragments) == _DOMAINS,
                    ),
                    (
                        "artifact_subject",
                        artifact["subject"]
                        == {
                            "workspace_id": subject.workspace_id,
                            "account_id": subject.account_id,
                        },
                    ),
                    (
                        "artifact_size",
                        len(plaintext) == export["size_bytes"] and len(plaintext) > 100,
                    ),
                    (
                        "receipt_state",
                        export["receipt_status"] == ("none" if phase == "ready" else "consumed"),
                    ),
                )
            )
            for name, prefix in (
                ("export_id", "pex"),
                ("workspace_id", "wsp"),
                ("tenant_id", "ten"),
                ("account_id", "gac"),
            ):
                rejected = False
                try:
                    decrypt_identity_export(
                        self.config,
                        export["artifact_ciphertext_b64"],
                        export["artifact_nonce_b64"],
                        **{**values, name: prefix + "_" + uuid4().hex},
                    )
                except ApiError as error:
                    rejected = error.code == "privacy.export_corrupt"
                checks.append(("encrypted_" + name + "_bound", rejected))
        else:
            raise ResourceContractViolation("resource.privacy_phase_invalid")
        return ResourceObservation(
            "privacy_owner_job",
            _digest([subject.request_id, phase]),
            _digest([request, jobs, exports]),
            datetime.now(UTC),
            tuple(checks),
        )

    def claim_observation(self, subject, claim):
        with closing(self.inspector._read()) as connection:
            job, _now = service._live_owner_job(connection, claim)
        return ResourceObservation(
            "privacy_owner_lease",
            _digest(claim.owner_job_id),
            _digest(dict(job)),
            datetime.now(UTC),
            (
                (
                    "actual_committed_lease",
                    subject.owns(claim)
                    and claim.attempt_count == 1
                    and claim.revision == 2
                    and claim.owner_domain in _DOMAINS,
                ),
            ),
        )


async def _thread_operation(runner, operation):
    await runner._check_lease()
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            await runner._check_lease()
        result = await task
        await runner._check_lease()
        return result
    except (ApiError, sqlite3.Error, DbError, OSError):
        raise ResourceUnavailable("resource.privacy_worker_unavailable") from None
    finally:
        # Cancellation cannot stop a database thread. Join before guest cleanup.
        cancelled_while_joining = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled_while_joining = True
            except (ApiError, sqlite3.Error, DbError, OSError):
                break
        if not task.cancelled():
            task.exception()
        if cancelled_while_joining:
            raise asyncio.CancelledError


def _fenced_completion(queue, claim):
    try:
        service._process_owner_job(queue.config, claim)
    except ApiError as error:
        return error.code == "privacy.worker_fenced"
    return False


async def _json_request(runner, client, method, path, expected, code, **kwargs):
    response = await runner._request(client, method, path, **kwargs)
    runner._check(code, response.status_code == expected, response)
    return response.json()


async def _create_export(runner, queue, client, account, workspace_id, tenant_id, label):
    if account is None:
        raise ResourceUnavailable("resource.privacy_account_material_missing")

    def headers():
        return runner._headers(client, global_session=True)

    enrollment = await _json_request(
        runner,
        client,
        "POST",
        "/identity/mfa/totp/enrollments",
        200,
        label + ".totp_enrolled",
        headers=headers(),
        json={"current_password": account.password},
    )
    secret = enrollment["secret"]
    if not isinstance(secret, str) or re.fullmatch(r"[A-Z2-7]{16,128}", secret) is None:
        raise ResourceContractViolation("resource.privacy_factor_invalid")
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    code = TOTP(key, 6, hashes.SHA1(), 30).generate(datetime.now(UTC).timestamp()).decode("ascii")
    await _json_request(
        runner,
        client,
        "POST",
        "/identity/mfa/totp/verifications",
        200,
        label + ".totp_verified",
        headers=headers(),
        json={"factor_id": enrollment["factor_id"], "code": code},
    )
    recovery = await _json_request(
        runner,
        client,
        "POST",
        "/identity/mfa/enrollments",
        200,
        label + ".recovery_created",
        headers=headers(),
        json={"method": "recovery_codes", "current_password": account.password},
    )
    codes = recovery["recovery_codes"]
    runner._check(label + ".ten_recovery_codes", isinstance(codes, list) and len(set(codes)) == 10)
    grant = await _json_request(
        runner,
        client,
        "POST",
        "/identity/step-up",
        200,
        label + ".step_up_verified",
        headers=headers(),
        json={
            "method": "recovery_code",
            "code": codes[0],
            "workspace_id": workspace_id,
            "action_code": "privacy.dsr.create",
            "resource_type": "privacy_request",
            "resource_id": workspace_id,
        },
    )
    created = await _json_request(
        runner,
        client,
        "POST",
        "/identity/data-subject-requests",
        201,
        label + ".accepted",
        headers={**headers(), "Idempotency-Key": uuid4().hex},
        json={"request_kind": "export", "step_up_grant": grant["grant"], "confirmed": True},
    )
    return queue.subject(
        workspace_id, tenant_id, account.email, created["request"]["data_subject_request_id"]
    )


async def run_queue_probe(runner, queue: LocalPrivacyQueue):
    try:
        await _run_queue_probe(runner, queue)
    except (ApiError, sqlite3.Error, DbError, OSError):
        raise ResourceUnavailable("resource.privacy_unavailable") from None


async def _run_queue_probe(runner, queue: LocalPrivacyQueue):
    fixtures = runner.fixtures
    subjects = []
    clients = (fixtures.source, fixtures.target)
    for label, client, account, workspace_id, tenant_id in (
        (
            "queue.source",
            fixtures.source,
            fixtures.source_account,
            fixtures.source_workspace_id,
            fixtures.source_tenant_id,
        ),
        (
            "queue.target",
            fixtures.target,
            fixtures.target_account,
            fixtures.target_workspace_id,
            fixtures.target_tenant_id,
        ),
    ):
        subject = await _create_export(
            runner,
            queue,
            client,
            account,
            workspace_id,
            tenant_id,
            label,
        )
        subjects.append(subject)
        await runner._observe(queue.observe, label + ".pending", subject, "pending")
    queue.require_exclusive_queue(subjects)
    for index, subject in enumerate(subjects):
        other = clients[1 - index]
        before = queue.state(subjects)
        path = "/identity/data-subject-requests/" + subject.request_id
        for method, kwargs in (
            ("GET", {}),
            (
                "DELETE",
                {
                    "headers": {
                        **runner._attack_headers(other, global_session=True),
                        "If-Match": '"1"',
                        "Idempotency-Key": uuid4().hex,
                    },
                    "json": {"step_up_grant": secrets.token_urlsafe(32), "confirmed": True},
                },
            ),
        ):
            kwargs.setdefault("headers", runner._attack_headers(other, global_session=True))
            response = await runner._request(fixtures.anonymous, method, path, **kwargs)
            runner._check(
                "queue.cross_subject." + method.lower(), response.status_code == 404, response
            )
        runner._check("queue.cross_subject.unchanged", queue.state(subjects) == before)
    exercised = set()
    for _ in range(14):
        claim = await _thread_operation(
            runner,
            lambda: service._claim_owner_job(queue.config, "private-privacy-worker"),
        )
        owners = [subject for subject in subjects if subject.owns(claim)]
        runner._check("queue.claim_owned", len(owners) == 1)
        subject = owners[0]
        await runner._observe(queue.claim_observation, "queue.claim", subject, claim)
        if subject.request_id not in exercised:
            exercised.add(subject.request_id)
            other = next(value for value in subjects if value != subject)
            for name, value in (
                ("workspace_id", other.workspace_id),
                ("tenant_id", other.tenant_id),
                ("account_id", other.account_id),
                ("data_subject_request_id", other.request_id),
                ("lease_owner_id", "forged-worker"),
                ("lease_token", secrets.token_urlsafe(32)),
                ("revision", claim.revision + 1),
            ):
                forged = replace(claim, **{name: value})
                before = queue.state(subjects)
                rejected = await _thread_operation(
                    runner, lambda forged=forged: _fenced_completion(queue, forged)
                )
                runner._check("queue.forged." + name + ".completion_rejected", rejected)
                await _thread_operation(
                    runner,
                    lambda forged=forged: service._fail_owner_job(
                        queue.config,
                        forged,
                        "privacy.owner_unavailable",
                    ),
                )
                runner._check(
                    "queue.forged." + name + ".unchanged", queue.state(subjects) == before
                )
        await _thread_operation(
            runner, lambda claim=claim: service._process_owner_job(queue.config, claim)
        )
        before = queue.state(subjects)
        rejected = await _thread_operation(
            runner, lambda claim=claim: _fenced_completion(queue, claim)
        )
        runner._check("queue.completion_replay_rejected", rejected)
        await _thread_operation(
            runner,
            lambda claim=claim: service._fail_owner_job(
                queue.config,
                claim,
                "privacy.owner_unavailable",
            ),
        )
        runner._check("queue.late_failure_unchanged", queue.state(subjects) == before)
    for index, subject in enumerate(subjects):
        client, other = clients[index], clients[1 - index]
        await runner._observe(queue.observe, "queue.ready", subject, "ready")
        ready = await _json_request(
            runner,
            client,
            "GET",
            "/identity/data-subject-requests/" + subject.request_id,
            200,
            "queue.owner_read",
        )
        export = ready["request"]["identity_export"]
        receipt = await _json_request(
            runner,
            client,
            "POST",
            f"/identity/data-subject-requests/{subject.request_id}/export-receipts",
            200,
            "queue.receipt_issued",
            headers={
                **runner._headers(client, global_session=True),
                "Idempotency-Key": uuid4().hex,
                "If-Match": f'"{export["revision"]}"',
            },
        )
        path = f"/identity/exports/{export['identity_export_id']}/download"
        before = queue.state(subjects)
        response = await runner._request(
            fixtures.anonymous,
            "GET",
            path,
            headers={
                **runner._attack_headers(other, global_session=True),
                "X-Privacy-Export-Receipt": receipt["receipt"],
            },
        )
        runner._check(
            "queue.cross_subject.download_rejected", response.status_code == 404, response
        )
        runner._check("queue.cross_subject.receipt_unchanged", queue.state(subjects) == before)
        downloaded = await runner._request(
            client,
            "GET",
            path,
            headers={"X-Privacy-Export-Receipt": receipt["receipt"]},
        )
        runner._check("queue.downloaded", downloaded.status_code == 200, downloaded)
        request, _jobs, exports, _account = queue._rows(subject)
        runner._check(
            "queue.download_matches_encrypted_artifact",
            hashlib.sha256(downloaded.content).hexdigest() == exports[0]["plaintext_sha256"]
            and request["status"] == "completed",
        )
        replay = await runner._request(
            client,
            "GET",
            path,
            headers={"X-Privacy-Export-Receipt": receipt["receipt"]},
        )
        runner._check("queue.download_receipt_one_use", replay.status_code == 403, replay)
        await runner._observe(queue.observe, "queue.downloaded", subject, "downloaded")
