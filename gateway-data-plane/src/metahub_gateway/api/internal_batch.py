"""PRD-074 internal Batch grant consumer and governed non-stream execution route."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from metahub_shared.batch import (
    MAX_BATCH_RESULT_BYTES,
    BatchCryptoError,
    BatchEnvelopeOwner,
    batch_payload_hmac,
    batch_result_hmac,
    decrypt_batch_payload,
    encrypt_batch_result,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.rbac_snapshot import require_actor_runtime_permissions
from metahub_shared.runtime.routing_policy import TokenEstimate

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.batch import (
    BatchAllocationCoverage,
    BatchRouteCandidateCoverage,
    BatchRouteCoverage,
    TrustedBatchContext,
    TrustedGatewayInvocation,
    get_batch_gateway_gate,
)

from .openai_compatible import _handle_gateway_request

router = APIRouter(prefix="/internal/batch", tags=["internal-batch"])

_TOKEN_HEADER = "X-MetaHub-Internal-Batch-Token"
_PROVIDER_ENDPOINT = "chat/completions"
_AUDIT_ENDPOINT = "/internal/batch/chat/completions"
_MAX_WIRE_BODY_BYTES = 16 * 1024
_FORBIDDEN_PROXY_HEADERS = (
    "Forwarded",
    "X-Forwarded-Host",
    "X-Original-URL",
    "X-Rewrite-URL",
)


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InternalBatchExecution:
    invocation: TrustedGatewayInvocation
    owner: BatchEnvelopeOwner


@router.post("/chat/completions")
async def internal_batch_chat_completions(request: Request) -> JSONResponse:
    """Consume one exact-owner grant and persist the encrypted result before responding."""

    config = _config(request)
    _authenticate_internal_service(request, config)
    _validate_request_surface(request)
    try:
        gate = get_batch_gateway_gate(
            request.app.state,
            batch_concurrency=config.batch_gateway_concurrency,
            realtime_cooldown_milliseconds=config.batch_realtime_cooldown_milliseconds,
        )
        gate_lease = await gate.try_enter_batch()
    except (RuntimeError, ValueError):
        raise ApiError("batch.worker_unavailable") from None
    if gate_lease is None:
        raise ApiError("batch.gateway_busy")

    execution: InternalBatchExecution | None = None
    try:
        grant = await _validated_grant_body(request)
        execution = _consume_execution_grant(
            config,
            grant=grant,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response = await _handle_gateway_request(
            request,
            endpoint=_PROVIDER_ENDPOINT,
            request_kind="chat",
            audit_endpoint=_AUDIT_ENDPOINT,
            trusted_invocation=execution.invocation,
        )
        if isinstance(response, StreamingResponse):
            raise ApiError("batch.worker_unavailable")
        assistant, input_tokens, output_tokens = _validated_assistant_result(request, response)
        _persist_encrypted_result(
            config,
            execution,
            assistant=assistant,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        batch = execution.invocation.batch
        if batch is None:  # pragma: no cover - constructor enforces the source contract.
            raise ApiError("batch.worker_unavailable")
        return JSONResponse(
            {
                "status": "succeeded",
                "batch_attempt_id": batch.batch_attempt_id,
                "request_id": batch.request_id,
                "trace_id": batch.trace_id,
            },
            headers={"Cache-Control": "no-store"},
        )
    except ApiError as exc:
        if execution is not None:
            mapped = _record_execution_failure(config, execution, exc.code)
            if mapped != exc.code:
                raise ApiError(mapped) from None
        raise
    except Exception:
        if execution is not None:
            _record_execution_failure(config, execution, "batch.worker_unavailable")
        raise ApiError("batch.worker_unavailable") from None
    finally:
        await gate_lease.release()


def _authenticate_internal_service(request: Request, config: AppConfig) -> None:
    expected = config.internal_batch_token
    if expected is None:
        raise ApiError("batch.worker_unavailable")
    values = tuple(request.headers.getlist(_TOKEN_HEADER))
    if len(values) != 1:
        raise ApiError("batch.execution_invalid")
    supplied = values[0]
    try:
        valid = supplied.isascii() and hmac.compare_digest(supplied, expected)
    except (TypeError, UnicodeError):
        valid = False
    if not valid:
        raise ApiError("batch.execution_invalid")


def _validate_request_surface(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("batch.request_invalid")
    if (
        request.headers.get("Authorization") is not None
        or request.headers.get("Cookie") is not None
        or request.headers.get("X-MetaHub-Session-ID") is not None
        or request.headers.get("X-MetaHub-Turn-ID") is not None
        or any(request.headers.get(name) is not None for name in _FORBIDDEN_PROXY_HEADERS)
    ):
        raise ApiError("batch.execution_invalid")
    content_types = _raw_header_values(request, b"content-type")
    if (
        len(content_types) != 1
        or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json"
    ):
        raise ApiError("batch.request_invalid")


async def _validated_grant_body(request: Request) -> str:
    try:
        raw = await _read_bounded_body(request)
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateJsonKey, RecursionError):
        raise ApiError("batch.request_invalid") from None
    if type(payload) is not dict or set(payload) != {"grant"}:
        raise ApiError("batch.request_invalid")
    grant = payload["grant"]
    if (
        type(grant) is not str
        or not 43 <= len(grant) <= 128
        or not grant.isascii()
        or any(not (character.isalnum() or character in "-_") for character in grant)
    ):
        raise ApiError("batch.execution_invalid")
    return grant


async def _read_bounded_body(request: Request) -> bytes:
    declared = _content_length(request)
    if declared is not None and (declared < 1 or declared > _MAX_WIRE_BODY_BYTES):
        raise ApiError("batch.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_WIRE_BODY_BYTES:
                raise ApiError("batch.request_invalid")
            body.extend(chunk)
    except ApiError:
        raise
    except Exception:
        raise ApiError("batch.request_invalid") from None
    if not body or (declared is not None and declared != len(body)):
        raise ApiError("batch.request_invalid")
    return bytes(body)


def _raw_header_values(request: Request, name: bytes) -> tuple[bytes, ...]:
    """Return every raw ASGI value for one case-insensitive wire header."""

    try:
        return tuple(
            value
            for raw_name, value in request.scope.get("headers", ())
            if raw_name.lower() == name
        )
    except (AttributeError, TypeError, ValueError):
        raise ApiError("batch.request_invalid") from None


def _content_length(request: Request) -> int | None:
    values = _raw_header_values(request, b"content-length")
    if not values:
        return None
    value = values[0]
    if (
        len(values) != 1
        or not value
        or len(value) > 12
        or any(character < 48 or character > 57 for character in value)
    ):
        raise ApiError("batch.request_invalid")
    return int(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _consume_execution_grant(
    config: AppConfig,
    *,
    grant: str,
    request_id: str,
    trace_id: str,
) -> InternalBatchExecution:
    connection = _connect(config)
    now = _db_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT grant.*,job.submitted_by_user_id,job.project_id,job.environment_id,
                   job.model_alias_id AS job_model_alias_id,
                   job.virtual_key_id AS job_virtual_key_id,job.route_plan_json AS job_route_plan,
                   job.operation_template_version,job.max_attempts,job.max_output_tokens,
                   job.status AS job_status,
                   job.deadline_at,job.cancel_requested_at AS job_cancel_requested_at,
                   task.status AS task_status,task.attempt_count,task.cancel_requested_at,
                   task.lease_token AS task_lease_token,
                   task.lease_owner_id AS task_lease_owner_id,
                   task.lease_generation AS task_lease_generation,
                   task.lease_expires_at,task.current_attempt_id,
                   attempt.attempt_no,attempt.status AS attempt_status,
                   attempt.execution_outcome,
                   attempt.result_state,attempt.request_payload_ciphertext,
                   attempt.request_payload_key_version,attempt.request_payload_hmac,
                   attempt.route_plan_json AS attempt_route_plan,
                   extract.estimated_tokens AS file_estimated_tokens
            FROM batch_execution_grants grant
            JOIN batch_jobs job
              ON job.tenant_id=grant.tenant_id AND job.batch_job_id=grant.batch_job_id
            JOIN batch_tasks task
              ON task.tenant_id=grant.tenant_id AND task.batch_task_id=grant.batch_task_id
            JOIN batch_attempts attempt
              ON attempt.tenant_id=grant.tenant_id
             AND attempt.batch_attempt_id=grant.batch_attempt_id
            JOIN file_extracts extract
              ON extract.tenant_id=grant.tenant_id AND extract.file_id=task.file_id
             AND extract.status='ready'
            WHERE grant.grant_hash=? AND grant.status='issued'
              AND grant.request_id=? AND grant.trace_id=?
              AND unixepoch(grant.expires_at)>unixepoch(?)
            """,
            (hashlib.sha256(grant.encode("ascii")).hexdigest(), request_id, trace_id, now),
        ).fetchone()
        if row is None:
            raise ApiError("batch.execution_invalid")
        tenant_id = str(row["tenant_id"])
        try:
            require_actor_runtime_permissions(
                connection,
                tenant_id=tenant_id,
                user_id=str(row["submitted_by_user_id"]),
                permission_codes=("batch.write", "file.read"),
                unavailable_error_code="batch.worker_unavailable",
            )
        except ApiError:
            # The internal grant contract never distinguishes revocation from damaged RBAC facts.
            raise ApiError("batch.execution_invalid") from None
        environment = connection.execute(
            """
            SELECT code,name,status FROM environments
            WHERE tenant_id=? AND environment_id=? AND status='active'
            """,
            (tenant_id, row["environment_id"]),
        ).fetchone()
        project = connection.execute(
            "SELECT 1 FROM projects WHERE tenant_id=? AND project_id=? AND status='active'",
            (tenant_id, row["project_id"]),
        ).fetchone()
        alias = connection.execute(
            """
            SELECT alias FROM model_aliases
            WHERE tenant_id=? AND model_alias_id=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (
                tenant_id,
                row["model_alias_id"],
                row["environment_id"],
                environment["code"] if environment is not None else "",
            ),
        ).fetchone()
        virtual_key = connection.execute(
            """
            SELECT key_prefix,application_id FROM virtual_keys
            WHERE tenant_id=? AND virtual_key_id=? AND project_id=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (
                tenant_id,
                row["virtual_key_id"],
                row["project_id"],
                row["environment_id"],
                environment["code"] if environment is not None else "",
            ),
        ).fetchone()
        key_count = connection.execute(
            """
            SELECT COUNT(*) FROM virtual_keys
            WHERE tenant_id=? AND project_id=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (
                tenant_id,
                row["project_id"],
                row["environment_id"],
                environment["code"] if environment is not None else "",
            ),
        ).fetchone()[0]
        if (
            environment is None
            or project is None
            or alias is None
            or virtual_key is None
            or int(key_count) != 1
            or row["job_status"] not in {"queued", "running"}
            or row["job_cancel_requested_at"] is not None
            or row["task_status"] != "leased"
            or row["cancel_requested_at"] is not None
            or str(row["current_attempt_id"]) != str(row["batch_attempt_id"])
            or str(row["task_lease_token"]) != str(row["lease_token"])
            or int(row["task_lease_generation"]) != int(row["lease_generation"])
            or row["attempt_status"] != "prepared"
            or row["execution_outcome"] != "not_invoked"
            or row["result_state"] != "none"
            or type(row["file_estimated_tokens"]) is not int
            or int(row["file_estimated_tokens"]) < 1
            or type(row["max_output_tokens"]) is not int
            or int(row["max_output_tokens"]) < 0
            or str(row["job_model_alias_id"]) != str(row["model_alias_id"])
            or str(row["job_virtual_key_id"]) != str(row["virtual_key_id"])
            or unixepoch(row["deadline_at"]) <= unixepoch(now)
            or unixepoch(row["lease_expires_at"]) <= unixepoch(now)
        ):
            raise ApiError("batch.execution_invalid")
        application_id = (
            str(virtual_key["application_id"])
            if virtual_key["application_id"] is not None
            else None
        )
        route, route_hash = _validated_route_plan(
            row["attempt_route_plan"],
            model_alias_id=str(row["model_alias_id"]),
            environment_id=str(row["environment_id"]),
            virtual_key_id=str(row["virtual_key_id"]),
            application_id=application_id,
        )
        if row["attempt_route_plan"] != row["job_route_plan"]:
            raise ApiError("batch.execution_invalid")
        allocation_rows = connection.execute(
            """
            SELECT allocation.batch_budget_allocation_id,allocation.budget_rule_id,
                   allocation.budget_date,allocation.reserved_cost_cny,
                   allocation.route_plan_hash,allocation.execution_outcome,
                   allocation.charge_state,allocation.status,
                   hold.tenant_id AS hold_tenant_id,hold.batch_job_id AS hold_job_id,
                   hold.budget_rule_id AS hold_rule_id,hold.budget_date AS hold_budget_date
            FROM batch_budget_allocations allocation
            JOIN batch_budget_holds hold
              ON hold.tenant_id=allocation.tenant_id
             AND hold.batch_budget_hold_id=allocation.batch_budget_hold_id
            WHERE allocation.tenant_id=? AND allocation.batch_job_id=?
              AND allocation.batch_task_id=? AND allocation.batch_attempt_id=?
              AND allocation.budget_date=(
                SELECT MAX(latest.budget_date)
                FROM batch_budget_allocations latest
                WHERE latest.tenant_id=allocation.tenant_id
                  AND latest.batch_job_id=allocation.batch_job_id
                  AND latest.batch_task_id=allocation.batch_task_id
                  AND latest.batch_attempt_id=allocation.batch_attempt_id
              )
            ORDER BY allocation.budget_rule_id
            """,
            (tenant_id, row["batch_job_id"], row["batch_task_id"], row["batch_attempt_id"]),
        ).fetchall()
        if any(
            str(item["route_plan_hash"]) != route_hash
            or str(item["execution_outcome"]) != "not_invoked"
            or str(item["charge_state"]) != "none"
            or str(item["status"]) != "active"
            or str(item["hold_tenant_id"]) != tenant_id
            or str(item["hold_job_id"]) != str(row["batch_job_id"])
            or str(item["hold_rule_id"]) != str(item["budget_rule_id"])
            or str(item["hold_budget_date"]) != str(item["budget_date"])
            for item in allocation_rows
        ):
            raise ApiError("batch.execution_invalid")
        write_deadline = min(
            _parse_db_time(str(row["deadline_at"])),
            _parse_db_time(str(row["lease_expires_at"])),
        )
        deadline_text = _to_db_time(write_deadline)
        grant_cursor = connection.execute(
            """
            UPDATE batch_execution_grants SET status='consumed',used_at=?
            WHERE tenant_id=? AND batch_execution_grant_id=? AND status='issued'
              AND grant_hash=? AND request_id=? AND trace_id=?
              AND unixepoch(expires_at)>unixepoch(?)
            """,
            (
                now,
                tenant_id,
                row["batch_execution_grant_id"],
                row["grant_hash"],
                request_id,
                trace_id,
                now,
            ),
        )
        attempt_cursor = connection.execute(
            """
            UPDATE batch_attempts
            SET status='calling',gateway_write_deadline_at=?,updated_at=?
            WHERE tenant_id=? AND batch_attempt_id=? AND status='prepared'
              AND execution_outcome='not_invoked' AND result_state='none'
            """,
            (deadline_text, now, tenant_id, row["batch_attempt_id"]),
        )
        task_cursor = connection.execute(
            """
            UPDATE batch_tasks SET status='calling',attempt_count=attempt_count+1,updated_at=?
            WHERE tenant_id=? AND batch_task_id=? AND status='leased'
              AND current_attempt_id=? AND lease_token=? AND lease_owner_id=?
              AND lease_generation=? AND unixepoch(lease_expires_at)>unixepoch(?)
              AND attempt_count<? AND ?=attempt_count+1
            """,
            (
                now,
                tenant_id,
                row["batch_task_id"],
                row["batch_attempt_id"],
                row["lease_token"],
                row["task_lease_owner_id"],
                row["lease_generation"],
                now,
                row["max_attempts"],
                row["attempt_no"],
            ),
        )
        if any(cursor.rowcount != 1 for cursor in (grant_cursor, attempt_cursor, task_cursor)):
            raise ApiError("batch.execution_invalid")

        owner = BatchEnvelopeOwner(
            tenant_id=tenant_id,
            batch_job_id=str(row["batch_job_id"]),
            batch_task_id=str(row["batch_task_id"]),
            batch_attempt_id=str(row["batch_attempt_id"]),
            request_id=request_id,
            template_version=str(row["operation_template_version"]),
            key_version=int(row["request_payload_key_version"]),
        )
        plaintext = decrypt_batch_payload(
            row["request_payload_ciphertext"],
            secret=config.batch_encryption_secret,
            owner=owner,
        )
        expected_hmac = batch_payload_hmac(
            plaintext,
            secret=config.batch_encryption_secret,
            owner=owner,
        )
        if not hmac.compare_digest(expected_hmac, str(row["request_payload_hmac"])):
            raise ApiError("batch.worker_unavailable")
        messages = _validated_request_payload(plaintext)
        auth_context = GatewayAuthContext(
            tenant_id=tenant_id,
            virtual_key_id=str(row["virtual_key_id"]),
            key_prefix=str(virtual_key["key_prefix"]),
            environment=str(environment["code"]),
            environment_id=str(row["environment_id"]),
            environment_name=str(environment["name"]),
            environment_status=str(environment["status"]),
            project_id=str(row["project_id"]),
            application_id=application_id,
        )
        batch_context = TrustedBatchContext(
            tenant_id=tenant_id,
            batch_job_id=str(row["batch_job_id"]),
            batch_task_id=str(row["batch_task_id"]),
            batch_attempt_id=str(row["batch_attempt_id"]),
            request_id=request_id,
            trace_id=trace_id,
            route_plan_hash=route_hash,
            route=route,
            token_estimate=TokenEstimate(
                input_tokens=int(row["file_estimated_tokens"]) + 256,
                output_tokens=int(row["max_output_tokens"]),
                truncated=False,
            ),
            allocations=tuple(
                BatchAllocationCoverage(
                    batch_budget_allocation_id=str(item["batch_budget_allocation_id"]),
                    budget_rule_id=str(item["budget_rule_id"]),
                    budget_date=str(item["budget_date"]),
                    reserved_cost_cny=str(item["reserved_cost_cny"]),
                )
                for item in allocation_rows
            ),
        )
        execution = InternalBatchExecution(
            invocation=TrustedGatewayInvocation(
                source="batch",
                auth_context=auth_context,
                payload={
                    "model": str(alias["alias"]),
                    "messages": messages,
                    "stream": False,
                    "max_tokens": int(row["max_output_tokens"]),
                },
                batch=batch_context,
            ),
            owner=owner,
        )
        # CAS consumption and sensitive-payload validation are one transaction. A corrupt
        # envelope must not strand the attempt and task in the calling state.
        connection.commit()
        return execution
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except BatchCryptoError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    except (DbError, sqlite3.Error, TypeError, ValueError, OverflowError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def _validated_route_plan(
    value: object,
    *,
    model_alias_id: str,
    environment_id: str,
    virtual_key_id: str,
    application_id: str | None,
) -> tuple[BatchRouteCoverage, str]:
    if type(value) is not str:
        raise ApiError("batch.execution_invalid")
    try:
        payload = json.loads(value, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, _DuplicateJsonKey, TypeError, ValueError):
        raise ApiError("batch.execution_invalid") from None
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "model_alias_id",
        "environment_id",
        "virtual_key_id",
        "application_id",
        "fallback_policy_version",
        "candidates",
    }:
        raise ApiError("batch.execution_invalid")
    if (
        payload["schema_version"] != 1
        or payload["model_alias_id"] != model_alias_id
        or payload["environment_id"] != environment_id
        or payload["virtual_key_id"] != virtual_key_id
        or payload["application_id"] != application_id
        or (
            payload["fallback_policy_version"] is not None
            and type(payload["fallback_policy_version"]) is not int
        )
    ):
        raise ApiError("batch.execution_invalid")
    candidates = payload["candidates"]
    if type(candidates) is not list or not 1 <= len(candidates) <= 2:
        raise ApiError("batch.execution_invalid")
    parsed: list[BatchRouteCandidateCoverage] = []
    for index, candidate in enumerate(candidates):
        if type(candidate) is not dict or set(candidate) != {
            "role",
            "model_id",
            "provider_id",
            "input_price_per_1k",
            "output_price_per_1k",
            "currency",
            "price_source",
            "price_version",
        }:
            raise ApiError("batch.execution_invalid")
        expected_role = "default" if index == 0 else "fallback"
        if candidate["role"] != expected_role or candidate["currency"] != "CNY":
            raise ApiError("batch.execution_invalid")
        try:
            item = BatchRouteCandidateCoverage(
                role=expected_role,
                model_id=candidate["model_id"],
                provider_id=candidate["provider_id"],
                input_price_per_1k=candidate["input_price_per_1k"],
                output_price_per_1k=candidate["output_price_per_1k"],
                price_source=candidate["price_source"],
                price_version=candidate["price_version"],
            )
        except ValueError:
            raise ApiError("batch.execution_invalid") from None
        if (
            item.input_price_per_1k != candidate["input_price_per_1k"]
            or item.output_price_per_1k != candidate["output_price_per_1k"]
        ):
            raise ApiError("batch.execution_invalid")
        parsed.append(item)
    if (len(parsed) == 1) != (payload["fallback_policy_version"] is None):
        raise ApiError("batch.execution_invalid")
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != value:
        raise ApiError("batch.execution_invalid")
    return BatchRouteCoverage(tuple(parsed)), hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validated_request_payload(value: bytes) -> list[dict[str, str]]:
    try:
        payload = json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateJsonKey, RecursionError):
        raise ApiError("batch.worker_unavailable") from None
    if type(payload) is not dict or set(payload) != {"schema_version", "messages"}:
        raise ApiError("batch.worker_unavailable")
    messages = payload["messages"]
    if payload["schema_version"] != 1 or type(messages) is not list or len(messages) != 2:
        raise ApiError("batch.worker_unavailable")
    normalized: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        role = "system" if index == 0 else "user"
        if (
            type(message) is not dict
            or set(message) != {"role", "content"}
            or message["role"] != role
            or type(message["content"]) is not str
            or not message["content"]
        ):
            raise ApiError("batch.worker_unavailable")
        normalized.append({"role": role, "content": message["content"]})
    return normalized


def _validated_assistant_result(
    request: Request,
    response: JSONResponse,
) -> tuple[str, int, int]:
    try:
        payload = json.loads(bytes(response.body).decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        raise ApiError("batch.result_unavailable") from None
    choices = payload.get("choices") if type(payload) is dict else None
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise ApiError("batch.result_unavailable")
    message = choices[0].get("message")
    if (
        type(message) is not dict
        or message.get("role") != "assistant"
        or type(message.get("content")) is not str
    ):
        raise ApiError("batch.result_unavailable")
    assistant = message["content"]
    try:
        size = len(assistant.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ApiError("batch.result_unavailable") from None
    maximum = min(_config(request).batch_max_result_bytes, MAX_BATCH_RESULT_BYTES)
    if not assistant or not 1 <= size <= maximum:
        raise ApiError("batch.result_unavailable")
    usage = getattr(request.state, "batch_gateway_usage", None)
    if (
        type(usage) is not tuple
        or len(usage) != 2
        or any(type(item) is not int or item < 0 for item in usage)
    ):
        raise ApiError("batch.worker_unavailable")
    return assistant, usage[0], usage[1]


def _persist_encrypted_result(
    config: AppConfig,
    execution: InternalBatchExecution,
    *,
    assistant: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    batch = execution.invocation.batch
    if batch is None:
        raise ApiError("batch.worker_unavailable")
    try:
        ciphertext = encrypt_batch_result(
            assistant,
            secret=config.batch_encryption_secret,
            owner=execution.owner,
        )
        fingerprint = batch_result_hmac(
            assistant,
            secret=config.batch_encryption_secret,
            owner=execution.owner,
        )
    except BatchCryptoError:
        raise ApiError("batch.worker_unavailable") from None
    connection = _connect(config)
    now = _db_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        request_log = connection.execute(
            """
            SELECT status FROM request_logs
            WHERE tenant_id=? AND request_id=? AND trace_id=?
            """,
            (batch.tenant_id, batch.request_id, batch.trace_id),
        ).fetchone()
        if request_log is None or request_log["status"] != "success":
            raise ApiError("batch.worker_unavailable")
        cursor = connection.execute(
            """
            UPDATE batch_attempts
            SET status='provider_succeeded',execution_outcome='succeeded',retry_safe=0,
                gateway_error_code=NULL,result_state='available',result_ciphertext=?,
                result_key_version=1,result_hmac=?,input_tokens=COALESCE(input_tokens,?),
                output_tokens=COALESCE(output_tokens,?),gateway_completed_at=?,updated_at=?
            WHERE tenant_id=? AND batch_job_id=? AND batch_task_id=? AND batch_attempt_id=?
              AND request_id=? AND trace_id=? AND status IN ('calling','provider_succeeded')
              AND execution_outcome IN ('not_invoked','succeeded') AND result_state='none'
              AND unixepoch(gateway_write_deadline_at)>=unixepoch(?)
            """,
            (
                ciphertext,
                fingerprint,
                input_tokens,
                output_tokens,
                now,
                now,
                batch.tenant_id,
                batch.batch_job_id,
                batch.batch_task_id,
                batch.batch_attempt_id,
                batch.request_id,
                batch.trace_id,
                now,
            ),
        )
        if cursor.rowcount != 1:
            raise ApiError("batch.execution_invalid")
        connection.commit()
    except ApiError:
        connection.rollback()
        raise
    except (DbError, sqlite3.Error):
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def _record_execution_failure(
    config: AppConfig,
    execution: InternalBatchExecution,
    error_code: str,
) -> str:
    batch = execution.invocation.batch
    if batch is None:
        return "batch.worker_unavailable"
    connection = _connect(config)
    now = _db_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        attempts = connection.execute(
            """
            SELECT status,usage_status FROM routing_attempts
            WHERE tenant_id=? AND request_id=? ORDER BY attempt_sequence
            """,
            (batch.tenant_id, batch.request_id),
        ).fetchall()
        allocations = connection.execute(
            """
            SELECT allocation.budget_date,allocation.status,allocation.charge_state
            FROM batch_budget_allocations allocation
            WHERE allocation.tenant_id=? AND allocation.batch_attempt_id=?
              AND allocation.budget_date=(
                SELECT MAX(latest.budget_date)
                FROM batch_budget_allocations latest
                WHERE latest.tenant_id=allocation.tenant_id
                  AND latest.batch_attempt_id=allocation.batch_attempt_id
              )
            """,
            (batch.tenant_id, batch.batch_attempt_id),
        ).fetchall()
        latest_budget_date = (
            str(allocations[0]["budget_date"]) if allocations else None
        )
        posted = bool(allocations) and all(
            row["status"] == "posted" and row["charge_state"] == "posted" for row in allocations
        )
        not_invoked = not attempts or all(
            row["status"] == "failed" and row["usage_status"] == "not_applicable"
            for row in attempts
        )
        if posted:
            target_status = "known_failed"
            execution_outcome = "succeeded"
            retry_safe = 0
            mapped = error_code
        elif not_invoked:
            target_status = "known_failed"
            execution_outcome = "known_failed"
            retry_safe = int(error_code in {"batch.gateway_busy", "batch.worker_unavailable"})
            mapped = error_code
            if latest_budget_date is not None:
                connection.execute(
                    """
                    UPDATE batch_budget_allocations
                    SET execution_outcome='known_failed',charge_state='none',status='released',
                        updated_at=?
                    WHERE tenant_id=? AND batch_attempt_id=? AND budget_date=?
                      AND status='active' AND execution_outcome='not_invoked'
                      AND charge_state='none'
                    """,
                    (now, batch.tenant_id, batch.batch_attempt_id, latest_budget_date),
                )
        else:
            target_status = "outcome_unknown"
            execution_outcome = "unknown"
            retry_safe = 0
            mapped = "batch.provider_outcome_unknown"
            if latest_budget_date is not None:
                connection.execute(
                    """
                    UPDATE batch_budget_allocations
                    SET execution_outcome='unknown',charge_state='possible',
                        status='reconciliation',updated_at=?
                    WHERE tenant_id=? AND batch_attempt_id=? AND budget_date=?
                      AND status='active' AND charge_state='none'
                    """,
                    (now, batch.tenant_id, batch.batch_attempt_id, latest_budget_date),
                )
        connection.execute(
            """
            UPDATE batch_attempts
            SET status=?,execution_outcome=?,retry_safe=?,gateway_error_code=?,
                gateway_completed_at=?,updated_at=?
            WHERE tenant_id=? AND batch_attempt_id=?
              AND status IN ('calling','provider_succeeded') AND result_state='none'
            """,
            (
                target_status,
                execution_outcome,
                retry_safe,
                mapped,
                now,
                now,
                batch.tenant_id,
                batch.batch_attempt_id,
            ),
        )
        connection.commit()
        return mapped
    except (DbError, sqlite3.Error, ValueError, TypeError):
        connection.rollback()
        return "batch.worker_unavailable"
    finally:
        connection.close()


def unixepoch(value: object) -> int:
    return int(_parse_db_time(str(value)).timestamp())


def _parse_db_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _db_now() -> str:
    return _to_db_time(datetime.now(UTC))


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("batch.worker_unavailable") from None


def _config(request: Request) -> AppConfig:
    config = getattr(request.app.state, "config", None)
    if not isinstance(config, AppConfig):
        raise ApiError("batch.worker_unavailable")
    return config


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if type(value) is not str or not value:
        raise ApiError("batch.worker_unavailable")
    return value


def _trace_id(request: Request) -> str:
    value = getattr(request.state, "trace_id", None)
    if type(value) is not str or not value:
        raise ApiError("batch.worker_unavailable")
    return value
