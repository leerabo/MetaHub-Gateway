"""PRD-074 批任务控制面模块：负责租户内调度、领取、续租、取消、计量和可恢复事件投递。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

Tenant-scoped PRD-074 Batch API persistence and terminal aggregation.

This module persists only encrypted operation metadata and safe execution/accounting facts. It
never logs or returns Provider keys, tokens, prompts, file text, assistant results, grants, lease
tokens, ciphertext, or internal endpoints.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from metahub_shared.batch import (
    BATCH_KEY_VERSION,
    BATCH_OPERATION_TEMPLATE_VERSION,
    BatchContractError,
    BatchCryptoError,
    BatchEnvelopeOwner,
    batch_result_hmac,
    canonical_batch_price,
    canonical_operation_spec_bytes,
    decrypt_batch_payload,
    decrypt_batch_result,
    encrypt_batch_payload,
    validate_operation_spec,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.budget_spend import (
    MICROS_PER_CNY,
    BudgetSpendUnavailableError,
    load_budget_spend_amounts,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_transaction_permission
from metahub_control.file_security import validate_file_for_batch_admission

from .models import (
    BatchCostSummary,
    BatchJobInfo,
    BatchJobListResponse,
    BatchJobResponse,
    BatchProgress,
    BatchReconciliationResponse,
    BatchTaskInfo,
    BatchTaskListResponse,
    BatchTaskResultResponse,
    CreateBatchJobRequest,
    ReconcileBatchTaskRequest,
)

# 该常量组统一批任务状态、限额和错误语义，变更时同步契约与测试。
_JOB_ID = re.compile(r"^bjob_[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^btsk_[0-9a-f]{32}$")
_ADJUSTMENT_ID = re.compile(r"^fadj_[0-9a-f]{32}$")
_BINDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_JOB_STATUSES = frozenset(
    {"queued", "running", "completed", "partial_failed", "failed", "cancelled", "expired"}
)
_TASK_STATUSES = frozenset(
    {
        "queued",
        "leased",
        "calling",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
        "outcome_unknown",
    }
)
_NON_TERMINAL_TASKS = frozenset({"queued", "leased", "calling", "retry_wait"})
_PRIORITY_RANK = {"low": 1, "normal": 2, "high": 3}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MONEY_QUANT = Decimal("0.000001")
_MAX_MONEY = Decimal("1000000000")
_TEMPLATE_INPUT_OVERHEAD_TOKENS = 256
_CURSOR_MAX_BYTES = 2048


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """实现批任务业务步骤。

    定义：RouteCandidate。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    role: str
    model_id: str
    provider_id: str
    input_price_per_1k: Decimal
    output_price_per_1k: Decimal
    price_source: str
    price_version: str


@dataclass(frozen=True, slots=True)
class BatchBinding:
    """实现批任务业务步骤。

    定义：BatchBinding。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    project_id: str
    environment_id: str
    environment_code: str
    model_alias_id: str
    virtual_key_id: str
    application_id: str | None
    candidates: tuple[RouteCandidate, ...]
    route_plan_json: str


@dataclass(frozen=True, slots=True)
class _FrozenRoutePlan:
    """实现批任务业务步骤。

    定义：_FrozenRoutePlan。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    application_id: str | None
    candidates: tuple[RouteCandidate, ...]


def create_batch_job(
    config: AppConfig,
    context: AuthContext,
    payload: CreateBatchJobRequest,
    *,
    idempotency_key: object,
    request_id: str,
    trace_id: str,
) -> BatchJobResponse:
    """实现批任务业务步骤。

    定义：create_batch_job。
    数据访问始终绑定租户并保留审计和失败事实。

    Atomically create an encrypted Job, tasks, budget holds, and created source event.
    """

    secret = _batch_secret(config)
    normalized_key = _canonical_uuid(idempotency_key, "batch.idempotency_required")
    canonical_request = _canonical_create_request(payload)
    key_hash = _domain_hmac(secret, b"idempotency/v1", normalized_key.encode("ascii"))
    fingerprint = _domain_hmac(secret, b"request/v1", canonical_request)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "batch.write",
            unavailable_error_code="batch.worker_unavailable",
        )
        live = require_transaction_permission(
            connection,
            live,
            "file.read",
            unavailable_error_code="batch.worker_unavailable",
        )
        existing = connection.execute(
            "SELECT * FROM batch_jobs WHERE tenant_id=? AND idempotency_key_hash=?",
            (live.user.tenant_id, key_hash),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["request_fingerprint"]), fingerprint):
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
                raise ApiError("batch.idempotency_conflict")
            job = _job_info(connection, existing, secret=secret)
            connection.commit()
            return BatchJobResponse(job=job, request_id=request_id, trace_id=trace_id)

        _enforce_capacity(connection, config, live.user.tenant_id, len(payload.file_ids))
        binding = _resolve_binding_and_route(
            connection,
            live.user.tenant_id,
            project_id=payload.project_id,
            environment_id=payload.environment_id,
            model_alias_id=payload.model_alias_id,
            as_of=_utcnow(),
        )
        admissions = [
            validate_file_for_batch_admission(
                connection,
                tenant_id=live.user.tenant_id,
                file_id=file_id,
            )
            for file_id in payload.file_ids
        ]
        input_tokens_by_task = tuple(
            admission.estimated_tokens + _TEMPLATE_INPUT_OVERHEAD_TOKENS
            for admission in admissions
        )
        per_attempt_costs = tuple(
            _attempt_cost(
                binding.candidates,
                input_tokens=input_tokens,
                output_tokens=payload.max_output_tokens,
            )
            for input_tokens in input_tokens_by_task
        )
        estimated_cost = _money(sum(per_attempt_costs, Decimal("0")) * payload.max_attempts)
        estimated_input_tokens = sum(input_tokens_by_task) * payload.max_attempts
        estimated_output_tokens = (
            len(admissions) * payload.max_output_tokens * payload.max_attempts
        )
        created = _utcnow()
        created_at = _db_time(created)
        deadline_at = _db_time(created + timedelta(seconds=payload.max_duration_seconds))
        job_id = f"bjob_{uuid4().hex}"
        operation_spec = validate_operation_spec(payload.operation_wire())
        owner = _job_envelope_owner(live.user.tenant_id, job_id)
        try:
            name_ciphertext = encrypt_batch_payload(
                payload.name,
                secret=secret,
                owner=owner,
            )
            operation_ciphertext = encrypt_batch_payload(
                canonical_operation_spec_bytes(operation_spec),
                secret=secret,
                owner=owner,
            )
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        except BatchCryptoError:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable") from None
        connection.execute(
            """
            INSERT INTO batch_jobs(
              batch_job_id,tenant_id,submitted_by_user_id,idempotency_key_hash,
              request_fingerprint,project_id,environment_id,model_alias_id,virtual_key_id,
              route_plan_json,name_ciphertext,operation,operation_spec_ciphertext,
              operation_template_version,priority,max_duration_seconds,max_attempts,
              max_output_tokens,task_count,status,estimated_input_tokens,
              estimated_output_tokens,estimated_cost_cny,posted_cost_cny,possible_cost_cny,
              reconciliation_pending,deadline_at,cancel_requested_at,cancelled_by_user_id,
              started_at,completed_at,terminal_error_code,source_request_id,source_trace_id,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                live.user.tenant_id,
                live.user.user_id,
                key_hash,
                fingerprint,
                binding.project_id,
                binding.environment_id,
                binding.model_alias_id,
                binding.virtual_key_id,
                binding.route_plan_json,
                name_ciphertext,
                payload.operation,
                operation_ciphertext,
                BATCH_OPERATION_TEMPLATE_VERSION,
                payload.priority,
                payload.max_duration_seconds,
                payload.max_attempts,
                payload.max_output_tokens,
                len(admissions),
                "queued",
                estimated_input_tokens,
                estimated_output_tokens,
                _money_text(estimated_cost),
                "0",
                "0",
                0,
                deadline_at,
                None,
                None,
                None,
                None,
                None,
                request_id,
                trace_id,
                created_at,
                created_at,
            ),
        )
        for index, admission in enumerate(admissions):
            connection.execute(
                """
                INSERT INTO batch_tasks(
                  batch_task_id,tenant_id,batch_job_id,file_id,task_index,priority_rank,status,
                  attempt_count,next_attempt_at,cancel_requested_at,lease_token,lease_owner_id,
                  lease_generation,lease_expires_at,current_attempt_id,terminal_request_id,
                  terminal_trace_id,failure_code,result_available,created_at,updated_at,completed_at
                ) VALUES(
                  ?,?,?,?,?,?,'queued',0,?,NULL,NULL,NULL,0,NULL,NULL,NULL,NULL,NULL,0,?,?,NULL
                )
                """,
                (
                    f"btsk_{uuid4().hex}",
                    live.user.tenant_id,
                    job_id,
                    admission.file_id,
                    index,
                    _PRIORITY_RANK[payload.priority],
                    created_at,
                    created_at,
                    created_at,
                ),
            )
        _create_budget_holds(
            connection,
            tenant_id=live.user.tenant_id,
            job_id=job_id,
            binding=binding,
            reserved=estimated_cost,
            created=created,
        )
        _insert_job_event(
            connection,
            tenant_id=live.user.tenant_id,
            job_id=job_id,
            event_type="batch_job.created",
            actor_type="user",
            actor_id=live.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=created_at,
            payload={
                "schema_version": 1,
                "batch_job_id": job_id,
                "operation": payload.operation,
                "priority": payload.priority,
                "task_count": len(admissions),
                "estimated_cost_cny": _money_text(estimated_cost),
            },
        )
        row = connection.execute(
            "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (live.user.tenant_id, job_id),
        ).fetchone()
        if row is None:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable")
        job = _job_info(connection, row, secret=secret)
        connection.commit()
        return BatchJobResponse(job=job, request_id=request_id, trace_id=trace_id)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        connection.rollback()
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ArithmeticError, ValueError, TypeError):
        connection.rollback()
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def list_batch_jobs(
    config: AppConfig,
    context: AuthContext,
    *,
    status: object,
    limit: object,
    cursor: object,
    request_id: str,
    trace_id: str,
) -> BatchJobListResponse:
    """实现批任务业务步骤。

    定义：list_batch_jobs。
    数据访问始终绑定租户并保留审计和失败事实。

    List safe job projections in stable reverse creation order.
    """

    secret = _batch_secret(config)
    normalized_status = _optional_enum(status, _JOB_STATUSES)
    normalized_limit = _limit(limit)
    position = _decode_cursor(
        cursor,
        secret=secret,
        tenant_id=context.user.tenant_id,
        kind="jobs",
        status=normalized_status,
    )
    clauses = ["tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status is not None:
        clauses.append("status=?")
        params.append(normalized_status)
    if position is not None:
        clauses.append("(created_at<? OR (created_at=? AND batch_job_id<?))")
        params.extend([position[0], position[0], position[1]])
    params.append(normalized_limit + 1)
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT * FROM batch_jobs
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC,batch_job_id DESC LIMIT ?
            """,
            params,
        ).fetchall()
        page = rows[:normalized_limit]
        next_cursor = None
        if len(rows) > normalized_limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                [str(last["created_at"]), str(last["batch_job_id"])],
                secret=secret,
                tenant_id=context.user.tenant_id,
                kind="jobs",
                status=normalized_status,
            )
        return BatchJobListResponse(
            jobs=[_job_info(connection, row, secret=secret) for row in page],
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ValueError, TypeError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def get_batch_job(
    config: AppConfig,
    context: AuthContext,
    batch_job_id: object,
    *,
    request_id: str,
    trace_id: str,
) -> BatchJobResponse:
    """实现批任务业务步骤。

    定义：get_batch_job。
    数据访问始终绑定租户并保留审计和失败事实。

    Return one tenant-owned job without internal execution fields.
    """

    job_id = _owned_job_id(batch_job_id)
    secret = _batch_secret(config)
    connection = _connect(config)
    try:
        row = connection.execute(
            "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (context.user.tenant_id, job_id),
        ).fetchone()
        if row is None:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.not_found")
        return BatchJobResponse(
            job=_job_info(connection, row, secret=secret),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ValueError, TypeError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def list_batch_tasks(
    config: AppConfig,
    context: AuthContext,
    batch_job_id: object,
    *,
    status: object,
    limit: object,
    cursor: object,
    request_id: str,
    trace_id: str,
) -> BatchTaskListResponse:
    """实现批任务业务步骤。

    定义：list_batch_tasks。
    数据访问始终绑定租户并保留审计和失败事实。

    List task execution summaries without result or request plaintext.
    """

    job_id = _owned_job_id(batch_job_id)
    secret = _batch_secret(config)
    normalized_status = _optional_enum(status, _TASK_STATUSES)
    normalized_limit = _limit(limit)
    position = _decode_cursor(
        cursor,
        secret=secret,
        tenant_id=context.user.tenant_id,
        kind=f"tasks:{job_id}",
        status=normalized_status,
    )
    connection = _connect(config)
    try:
        if connection.execute(
            "SELECT 1 FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (context.user.tenant_id, job_id),
        ).fetchone() is None:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.not_found")
        clauses = ["task.tenant_id=?", "task.batch_job_id=?"]
        params: list[object] = [context.user.tenant_id, job_id]
        if normalized_status is not None:
            clauses.append("task.status=?")
            params.append(normalized_status)
        if position is not None:
            clauses.append("(task.task_index>? OR (task.task_index=? AND task.batch_task_id>?))")
            params.extend([int(position[0]), int(position[0]), position[1]])
        params.append(normalized_limit + 1)
        rows = connection.execute(
            f"""
            SELECT task.*,attempt.input_tokens,attempt.output_tokens,attempt.cost_cny
            FROM batch_tasks task
            LEFT JOIN batch_attempts attempt
              ON attempt.tenant_id=task.tenant_id
             AND attempt.batch_attempt_id=task.current_attempt_id
            WHERE {' AND '.join(clauses)}
            ORDER BY task.task_index,task.batch_task_id LIMIT ?
            """,
            params,
        ).fetchall()
        page = rows[:normalized_limit]
        next_cursor = None
        if len(rows) > normalized_limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                [int(last["task_index"]), str(last["batch_task_id"])],
                secret=secret,
                tenant_id=context.user.tenant_id,
                kind=f"tasks:{job_id}",
                status=normalized_status,
            )
        return BatchTaskListResponse(
            tasks=[_task_info(row) for row in page],
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ValueError, TypeError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def get_batch_task_result(
    config: AppConfig,
    context: AuthContext,
    batch_job_id: object,
    batch_task_id: object,
) -> BatchTaskResultResponse:
    """实现批任务业务步骤。

    定义：get_batch_task_result。
    数据访问始终绑定租户并保留审计和失败事实。

    Recheck both permissions and return one authenticated assistant result.
    """

    job_id = _owned_job_id(batch_job_id)
    task_id = _owned_task_id(batch_task_id)
    secret = _batch_secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "batch.read",
            unavailable_error_code="batch.worker_unavailable",
        )
        live = require_transaction_permission(
            connection,
            live,
            "file.read",
            unavailable_error_code="batch.worker_unavailable",
        )
        row = connection.execute(
            """
            SELECT task.status AS task_status,task.failure_code,task.result_available,
                   attempt.*,job.operation_template_version
            FROM batch_tasks task
            JOIN batch_jobs job
              ON job.tenant_id=task.tenant_id AND job.batch_job_id=task.batch_job_id
            LEFT JOIN batch_attempts attempt
              ON attempt.tenant_id=task.tenant_id
             AND attempt.batch_attempt_id=task.current_attempt_id
            WHERE task.tenant_id=? AND task.batch_job_id=? AND task.batch_task_id=?
            """,
            (live.user.tenant_id, job_id, task_id),
        ).fetchone()
        if row is None:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.task_not_found")
        if row["task_status"] == "outcome_unknown":
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.provider_outcome_unknown")
        if row["task_status"] != "succeeded":
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.result_not_ready")
        if (
            row["result_available"] != 1
            or row["result_state"] != "available"
            or row["result_ciphertext"] is None
            or row["result_hmac"] is None
        ):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.result_unavailable")
        owner = BatchEnvelopeOwner(
            tenant_id=live.user.tenant_id,
            batch_job_id=job_id,
            batch_task_id=task_id,
            batch_attempt_id=str(row["batch_attempt_id"]),
            request_id=str(row["request_id"]),
            template_version=str(row["operation_template_version"]),
            key_version=int(row["result_key_version"]),
        )
        try:
            assistant = decrypt_batch_result(
                row["result_ciphertext"],
                secret=secret,
                owner=owner,
            )
            expected_hmac = batch_result_hmac(assistant, secret=secret, owner=owner)
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        except BatchCryptoError:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.result_unavailable") from None
        if not hmac.compare_digest(str(row["result_hmac"]), expected_hmac):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.result_unavailable")
        connection.commit()
        return BatchTaskResultResponse(
            batch_job_id=job_id,
            batch_task_id=task_id,
            assistant=assistant,
            request_id=str(row["request_id"]),
            trace_id=str(row["trace_id"]),
        )
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        connection.rollback()
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ValueError, TypeError):
        connection.rollback()
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def cancel_batch_job(
    config: AppConfig,
    context: AuthContext,
    batch_job_id: object,
    *,
    request_id: str,
    trace_id: str,
) -> BatchJobResponse:
    """实现批任务业务步骤。

    定义：cancel_batch_job。
    数据访问始终绑定租户并保留审计和失败事实。

    Idempotently request cancellation and cancel work not yet calling a Provider.
    """

    job_id = _owned_job_id(batch_job_id)
    secret = _batch_secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "batch.write",
            unavailable_error_code="batch.worker_unavailable",
        )
        row = connection.execute(
            "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
            (live.user.tenant_id, job_id),
        ).fetchone()
        if row is None:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.not_found")
        if row["status"] not in {"completed", "partial_failed", "failed", "cancelled", "expired"}:
            now = _db_time(_utcnow())
            connection.execute(
                """
                UPDATE batch_jobs
                SET cancel_requested_at=COALESCE(cancel_requested_at,?),
                    cancelled_by_user_id=COALESCE(cancelled_by_user_id,?),updated_at=?
                WHERE tenant_id=? AND batch_job_id=?
                """,
                (now, live.user.user_id, now, live.user.tenant_id, job_id),
            )
            connection.execute(
                """
                UPDATE batch_tasks
                SET status='cancelled',cancel_requested_at=COALESCE(cancel_requested_at,?),
                    current_attempt_id=NULL,terminal_request_id=?,terminal_trace_id=?,
                    failure_code=NULL,result_available=0,completed_at=?,updated_at=?
                WHERE tenant_id=? AND batch_job_id=? AND status IN ('queued','retry_wait')
                """,
                (now, request_id, trace_id, now, now, live.user.tenant_id, job_id),
            )
            connection.execute(
                """
                UPDATE batch_tasks
                SET cancel_requested_at=COALESCE(cancel_requested_at,?),updated_at=?
                WHERE tenant_id=? AND batch_job_id=? AND status IN ('leased','calling')
                """,
                (now, now, live.user.tenant_id, job_id),
            )
            _finalize_job_if_terminal(
                connection,
                tenant_id=live.user.tenant_id,
                job_id=job_id,
                actor_type="user",
                actor_id=live.user.user_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
                (live.user.tenant_id, job_id),
            ).fetchone()
        if row is None:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.not_found")
        job = _job_info(connection, row, secret=secret)
        connection.commit()
        return BatchJobResponse(job=job, request_id=request_id, trace_id=trace_id)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        connection.rollback()
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ValueError, TypeError):
        connection.rollback()
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def reconcile_batch_task(
    config: AppConfig,
    context: AuthContext,
    batch_job_id: object,
    batch_task_id: object,
    payload: ReconcileBatchTaskRequest,
    *,
    idempotency_key: object,
    request_id: str,
    trace_id: str,
) -> BatchReconciliationResponse:
    """实现批任务业务步骤。

    定义：reconcile_batch_task。
    数据访问始终绑定租户并保留审计和失败事实。

    Resolve possible cost with an idempotent, immutable management fact.
    """

    job_id = _owned_job_id(batch_job_id)
    task_id = _owned_task_id(batch_task_id)
    secret = _batch_secret(config)
    key = _canonical_uuid(idempotency_key, "batch.idempotency_required")
    key_hash = _domain_hmac(secret, b"reconciliation/v1", key.encode("ascii"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "batch.read",
            unavailable_error_code="batch.worker_unavailable",
        )
        live = require_transaction_permission(
            connection,
            live,
            "billing.finops.write",
            unavailable_error_code="batch.worker_unavailable",
        )
        existing = connection.execute(
            """
            SELECT * FROM batch_reconciliation_events
            WHERE tenant_id=? AND idempotency_key_hash=?
            """,
            (live.user.tenant_id, key_hash),
        ).fetchone()
        if existing is not None:
            if (
                existing["batch_job_id"] != job_id
                or existing["batch_task_id"] != task_id
                or existing["decision"] != payload.decision
                or existing["adjustment_id"] != payload.adjustment_id
            ):
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
                raise ApiError("batch.reconciliation_conflict")
            pending = _job_reconciliation_pending(connection, live.user.tenant_id, job_id)
            connection.commit()
            return BatchReconciliationResponse(
                batch_job_id=job_id,
                batch_task_id=task_id,
                decision=payload.decision,
                reconciliation_pending=pending,
                request_id=request_id,
                trace_id=trace_id,
            )
        allocation_rows = connection.execute(
            """
            SELECT attempt.batch_attempt_id,attempt.request_id,
                   allocation.reserved_cost_cny
            FROM batch_attempts attempt
            JOIN batch_budget_allocations allocation
              ON allocation.tenant_id=attempt.tenant_id
             AND allocation.batch_attempt_id=attempt.batch_attempt_id
             AND allocation.status='reconciliation' AND allocation.charge_state='possible'
            WHERE attempt.tenant_id=? AND attempt.batch_job_id=? AND attempt.batch_task_id=?
            ORDER BY attempt.attempt_no DESC,allocation.budget_date DESC,
                     allocation.budget_rule_id
            """,
            (live.user.tenant_id, job_id, task_id),
        ).fetchall()
        if not allocation_rows:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.reconciliation_conflict")
        attempt_id = str(allocation_rows[0]["batch_attempt_id"])
        attempt_rows = [
            row for row in allocation_rows if row["batch_attempt_id"] == attempt_id
        ]
        possible_cny = _consistent_allocation_amount(
            (row["reserved_cost_cny"] for row in attempt_rows),
            error_code="batch.reconciliation_conflict",
        )
        attempt_request_id = str(attempt_rows[0]["request_id"])
        adjustment_id = payload.adjustment_id
        if payload.decision == "confirm_charged":
            if (
                adjustment_id is None
                or _ADJUSTMENT_ID.fullmatch(adjustment_id) is None
                or not _matching_adjustment(
                    connection,
                    tenant_id=live.user.tenant_id,
                    adjustment_id=adjustment_id,
                    request_id=attempt_request_id,
                    amount=possible_cny,
                )
            ):
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
                raise ApiError("batch.reconciliation_conflict")
            allocation_status = "posted"
            charge_state = "posted"
            execution_outcome = "succeeded"
        else:
            allocation_status = "released"
            charge_state = "none"
            execution_outcome = "not_invoked"
        now = _db_time(_utcnow())
        connection.execute(
            """
            UPDATE batch_budget_allocations
            SET status=?,charge_state=?,execution_outcome=?,updated_at=?
            WHERE tenant_id=? AND batch_attempt_id=?
              AND status='reconciliation' AND charge_state='possible'
            """,
            (
                allocation_status,
                charge_state,
                execution_outcome,
                now,
                live.user.tenant_id,
                attempt_id,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] < 1:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.reconciliation_conflict")
        connection.execute(
            """
            INSERT INTO batch_reconciliation_events(
              batch_reconciliation_event_id,tenant_id,batch_job_id,batch_task_id,
              batch_attempt_id,actor_user_id,idempotency_key_hash,decision,adjustment_id,
              request_id,trace_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"brec_{uuid4().hex}",
                live.user.tenant_id,
                job_id,
                task_id,
                attempt_id,
                live.user.user_id,
                key_hash,
                payload.decision,
                adjustment_id,
                request_id,
                trace_id,
                now,
            ),
        )
        _refresh_job_costs(connection, live.user.tenant_id, job_id, now=now)
        pending = _job_reconciliation_pending(connection, live.user.tenant_id, job_id)
        connection.commit()
        return BatchReconciliationResponse(
            batch_job_id=job_id,
            batch_task_id=task_id,
            decision=payload.decision,
            reconciliation_pending=pending,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        connection.rollback()
        raise
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (sqlite3.Error, ValueError, TypeError):
        connection.rollback()
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def decrypt_job_operation_spec(
    config: AppConfig,
    *,
    tenant_id: str,
    batch_job_id: str,
    ciphertext: object,
) -> object:
    """实现批任务业务步骤。

    定义：decrypt_job_operation_spec。
    数据访问始终绑定租户并保留审计和失败事实。

    Decrypt and validate a durable operation spec for the Worker without logging it.
    """

    try:
        plaintext = decrypt_batch_payload(
            ciphertext,
            secret=_batch_secret(config),
            owner=_job_envelope_owner(tenant_id, batch_job_id),
        )
        decoded = json.loads(plaintext.decode("utf-8", errors="strict"))
        return validate_operation_spec(decoded)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (BatchCryptoError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None


def finalize_batch_job_if_terminal(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    batch_job_id: str,
    request_id: str,
    trace_id: str,
    now: str,
    actor_id: str = "batch-worker",
) -> bool:
    """实现批任务业务步骤。

    定义：finalize_batch_job_if_terminal。
    数据访问始终绑定租户并保留审计和失败事实。

    Public transaction helper used by Worker/reaper after a task reaches a terminal state.
    """

    return _finalize_job_if_terminal(
        connection,
        tenant_id=tenant_id,
        job_id=batch_job_id,
        actor_type="system",
        actor_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def attempt_reserved_cost(
    route_plan_json: object,
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """实现批任务业务步骤。

    定义：attempt_reserved_cost。
    数据访问始终绑定租户并保留审计和失败事实。

    Recompute one attempt's frozen worst-case cost from an exact route plan.
    """

    route = _route_plan_from_json(route_plan_json)
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return _attempt_cost(route.candidates, input_tokens=input_tokens, output_tokens=output_tokens)


def route_plan_hash(route_plan_json: object) -> str:
    """实现批任务业务步骤。

    定义：route_plan_hash。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    # 模型和上游只用授权解析结果；解析失败即停止，不得切换未批准路由。
    if type(route_plan_json) is not str:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    try:
        encoded = route_plan_json.encode("utf-8", errors="strict")
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except UnicodeError:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    return hashlib.sha256(encoded).hexdigest()


def rollover_batch_budget_holds(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    batch_job_id: str,
    current: datetime,
) -> None:
    """实现批任务业务步骤。

    定义：rollover_batch_budget_holds。
    数据访问始终绑定租户并保留审计和失败事实。

    Move unallocated worst-case cost to the current Shanghai business day.
    """

    if not connection.in_transaction:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    budget_date, _start, _end = _budget_window(current)
    job = connection.execute(
        """
        SELECT job.*,key.application_id AS current_application_id
        FROM batch_jobs job
        JOIN virtual_keys key
          ON key.tenant_id=job.tenant_id AND key.virtual_key_id=job.virtual_key_id
        WHERE job.tenant_id=? AND job.batch_job_id=?
        """,
        (tenant_id, batch_job_id),
    ).fetchone()
    if job is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    latest_row = connection.execute(
        """
        SELECT MAX(budget_date) FROM batch_budget_holds
        WHERE tenant_id=? AND batch_job_id=?
        """,
        (tenant_id, batch_job_id),
    ).fetchone()
    latest_date = str(latest_row[0]) if latest_row is not None and latest_row[0] else None
    created_date = _budget_window(_time(job["created_at"]))[0]
    comparison_date = latest_date or created_date
    if comparison_date == budget_date:
        return
    if comparison_date > budget_date:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")

    frozen_route = _route_plan_from_json(job["route_plan_json"])
    current_application_id = (
        str(job["current_application_id"]) if job["current_application_id"] else None
    )
    # 模型和上游只用授权解析结果；解析失败即停止，不得切换未批准路由。
    if current_application_id != frozen_route.application_id:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.binding_unavailable")
    candidates = frozen_route.candidates
    task_rows = connection.execute(
        """
        SELECT task.file_id,task.attempt_count,extract.status AS extract_status,
               extract.estimated_tokens
        FROM batch_tasks task
        LEFT JOIN file_extracts extract
          ON extract.tenant_id=task.tenant_id AND extract.file_id=task.file_id
        WHERE task.tenant_id=? AND task.batch_job_id=?
          AND task.status IN ('queued','leased','calling','retry_wait')
        ORDER BY task.task_index,task.batch_task_id
        """,
        (tenant_id, batch_job_id),
    ).fetchall()
    remaining = Decimal("0")
    for task in task_rows:
        if (
            task["extract_status"] != "ready"
            or type(task["estimated_tokens"]) is not int
            or type(task["attempt_count"]) is not int
        ):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable")
        attempts_left = int(job["max_attempts"]) - int(task["attempt_count"])
        if attempts_left < 0:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable")
        remaining += _attempt_cost(
            candidates,
            input_tokens=int(task["estimated_tokens"]) + _TEMPLATE_INPUT_OVERHEAD_TOKENS,
            output_tokens=int(job["max_output_tokens"]),
        ) * attempts_left
    remaining = _money(remaining)
    binding = BatchBinding(
        project_id=str(job["project_id"]),
        environment_id=str(job["environment_id"]),
        environment_code="",
        model_alias_id=str(job["model_alias_id"]),
        virtual_key_id=str(job["virtual_key_id"]),
        application_id=frozen_route.application_id,
        candidates=candidates,
        route_plan_json=str(job["route_plan_json"]),
    )
    _create_budget_holds(
        connection,
        tenant_id=tenant_id,
        job_id=batch_job_id,
        binding=binding,
        reserved=remaining,
        created=current,
    )
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET execution_outcome='not_invoked',charge_state='none',status='released',updated_at=?
        WHERE tenant_id=? AND batch_job_id=? AND status='active' AND budget_date<>?
          AND EXISTS(
            SELECT 1 FROM batch_attempts attempt
            WHERE attempt.tenant_id=batch_budget_allocations.tenant_id
              AND attempt.batch_attempt_id=batch_budget_allocations.batch_attempt_id
              AND attempt.status='prepared' AND attempt.execution_outcome='not_invoked'
          )
        """,
        (_db_time(current), tenant_id, batch_job_id, budget_date),
    )
    connection.execute(
        """
        UPDATE batch_budget_holds
        SET remaining_cost_cny='0',status=CASE
              WHEN CAST(reconciliation_cost_cny AS NUMERIC)>0 THEN 'reconciliation'
              ELSE 'released' END,
            updated_at=?
        WHERE tenant_id=? AND batch_job_id=? AND status='active' AND budget_date<>?
        """,
        (_db_time(current), tenant_id, batch_job_id, budget_date),
    )


def _resolve_binding_and_route(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    project_id: str,
    environment_id: str,
    model_alias_id: str,
    as_of: datetime,
) -> BatchBinding:
    """实现批任务业务步骤。

    定义：_resolve_binding_and_route。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    owner = connection.execute(
        """
        SELECT project.project_id,environment.environment_id,environment.code
        FROM projects project
        JOIN environments environment ON environment.tenant_id=project.tenant_id
        WHERE project.tenant_id=? AND project.project_id=? AND project.status='active'
          AND environment.environment_id=? AND environment.status='active'
        """,
        (tenant_id, project_id, environment_id),
    ).fetchone()
    if owner is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.binding_unavailable")
    alias = connection.execute(
        """
        SELECT alias.model_alias_id,alias.default_model_id,alias.fallback_model_id,
               fallback.status AS fallback_status,fallback.version AS fallback_version
        FROM model_aliases alias
        LEFT JOIN fallback_policies fallback
          ON fallback.tenant_id=alias.tenant_id
         AND fallback.model_alias_id=alias.model_alias_id
        WHERE alias.tenant_id=? AND alias.model_alias_id=? AND alias.status='active'
          AND (alias.environment_id=? OR (alias.environment_id IS NULL AND alias.environment=?))
        """,
        (tenant_id, model_alias_id, environment_id, owner["code"]),
    ).fetchone()
    if alias is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.binding_unavailable")
    keys = connection.execute(
        """
        SELECT virtual_key_id,application_id FROM virtual_keys
        WHERE tenant_id=? AND project_id=? AND status='active'
          AND (environment_id=? OR (environment_id IS NULL AND environment=?))
        ORDER BY virtual_key_id LIMIT 2
        """,
        (tenant_id, project_id, environment_id, owner["code"]),
    ).fetchall()
    if len(keys) != 1:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.binding_unavailable")
    model_roles: list[tuple[str, str]] = [("default", str(alias["default_model_id"]))]
    if (
        alias["fallback_model_id"] is not None
        and alias["fallback_status"] == "enabled"
    ):
        model_roles.append(("fallback", str(alias["fallback_model_id"])))
    candidates = tuple(
        _priced_candidate(connection, tenant_id, role=role, model_id=model_id, as_of=as_of)
        for role, model_id in model_roles
    )
    route_plan = {
        "schema_version": 1,
        "model_alias_id": str(alias["model_alias_id"]),
        "environment_id": str(owner["environment_id"]),
        "virtual_key_id": str(keys[0]["virtual_key_id"]),
        "application_id": (
            str(keys[0]["application_id"]) if keys[0]["application_id"] else None
        ),
        "fallback_policy_version": (
            int(alias["fallback_version"])
            if len(candidates) == 2 and alias["fallback_version"] is not None
            else None
        ),
        "candidates": [
            {
                "role": candidate.role,
                "model_id": candidate.model_id,
                "provider_id": candidate.provider_id,
                "input_price_per_1k": _price_text(candidate.input_price_per_1k),
                "output_price_per_1k": _price_text(candidate.output_price_per_1k),
                "currency": "CNY",
                "price_source": candidate.price_source,
                "price_version": candidate.price_version,
            }
            for candidate in candidates
        ],
    }
    route_plan_json = json.dumps(
        route_plan,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return BatchBinding(
        project_id=str(owner["project_id"]),
        environment_id=str(owner["environment_id"]),
        environment_code=str(owner["code"]),
        model_alias_id=str(alias["model_alias_id"]),
        virtual_key_id=str(keys[0]["virtual_key_id"]),
        application_id=(str(keys[0]["application_id"]) if keys[0]["application_id"] else None),
        candidates=candidates,
        route_plan_json=route_plan_json,
    )


def _priced_candidate(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    role: str,
    model_id: str,
    as_of: datetime,
) -> RouteCandidate:
    """实现批任务业务步骤。

    定义：_priced_candidate。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    row = connection.execute(
        """
        SELECT model.model_id,model.provider_id,model.status,
               plan.price_plan_id,CAST(plan.input_price_per_1k AS TEXT) AS plan_input,
               CAST(plan.output_price_per_1k AS TEXT) AS plan_output,
               plan.currency AS plan_currency,plan.price_schema_version AS plan_version,
               CAST(model.input_price_per_1k AS TEXT) AS catalog_input,
               CAST(model.output_price_per_1k AS TEXT) AS catalog_output,
               model.currency AS catalog_currency,model.price_schema_version AS catalog_version
        FROM models model
        LEFT JOIN model_price_plans plan ON plan.price_plan_id=(
          SELECT selected.price_plan_id FROM model_price_plans selected
          WHERE selected.tenant_id=model.tenant_id AND selected.model_id=model.model_id
            AND selected.effective_from<=?
          ORDER BY selected.effective_from DESC,selected.created_at DESC,selected.price_plan_id DESC
          LIMIT 1
        )
        WHERE model.tenant_id=? AND model.model_id=?
        """,
        (_db_time(as_of), tenant_id, model_id),
    ).fetchone()
    if row is None or row["status"] != "active":
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.binding_unavailable")
    if row["price_plan_id"] is not None:
        source = "price_plan"
        input_price = row["plan_input"]
        output_price = row["plan_output"]
        currency = row["plan_currency"]
        version = row["plan_version"]
    else:
        source = "model_catalog"
        input_price = row["catalog_input"]
        output_price = row["catalog_output"]
        currency = row["catalog_currency"]
        version = row["catalog_version"]
    # 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
    if currency != "CNY" or type(version) is not str or not version:
        # 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
        raise ApiError("batch.cost_estimate_unavailable")
    try:
        normalized_input = _price(input_price)
        normalized_output = _price(output_price)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ValueError:
        # 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
        raise ApiError("batch.cost_estimate_unavailable") from None
    return RouteCandidate(
        role=role,
        model_id=str(row["model_id"]),
        provider_id=str(row["provider_id"]),
        input_price_per_1k=normalized_input,
        output_price_per_1k=normalized_output,
        price_source=source,
        price_version=version,
    )


def _create_budget_holds(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    job_id: str,
    binding: BatchBinding,
    reserved: Decimal,
    created: datetime,
) -> None:
    """实现批任务业务步骤。

    定义：_create_budget_holds。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    budget_date, start, end = _budget_window(created)
    scopes = {
        ("tenant", tenant_id),
        ("project", binding.project_id),
        ("virtual_key", binding.virtual_key_id),
    }
    if binding.application_id is not None:
        scopes.add(("application", binding.application_id))
    predicates = " OR ".join("(scope_type=? AND scope_id=?)" for _ in scopes)
    params: list[object] = [tenant_id, binding.environment_id]
    for scope_type, scope_id in sorted(scopes):
        params.extend([scope_type, scope_id])
    rules = connection.execute(
        f"""
        SELECT * FROM budget_rules
        WHERE tenant_id=? AND environment_id=? AND status='active'
          AND ({predicates})
        ORDER BY budget_rule_id
        """,
        params,
    ).fetchall()
    reserved_micros = _money_micros(reserved)
    for rule in rules:
        try:
            amounts = load_budget_spend_amounts(
                connection,
                tenant_id=tenant_id,
                environment_id=binding.environment_id,
                scope_type=str(rule["scope_type"]),
                scope_id=str(rule["scope_id"]),
                budget_rule_id=str(rule["budget_rule_id"]),
                budget_date=budget_date,
                start_utc=start,
                end_utc=end,
            )
            budget_micros = _money_micros(_money(rule["daily_budget_cny"]))
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        except (BudgetSpendUnavailableError, ValueError, TypeError):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable") from None
        if amounts.effective_cny_micros + reserved_micros >= budget_micros:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.budget_exceeded")
        amount = _money_text(reserved)
        now = _db_time(created)
        connection.execute(
            """
            INSERT INTO batch_budget_holds(
              batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
              scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
              reconciliation_cost_cny,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'0','active',?,?)
            """,
            (
                f"bbhd_{uuid4().hex}",
                tenant_id,
                job_id,
                rule["budget_rule_id"],
                binding.environment_id,
                rule["scope_type"],
                rule["scope_id"],
                budget_date,
                amount,
                amount,
                now,
                now,
            ),
        )


def _finalize_job_if_terminal(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    job_id: str,
    actor_type: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> bool:
    """实现批任务业务步骤。

    定义：_finalize_job_if_terminal。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    job = connection.execute(
        "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (tenant_id, job_id),
    ).fetchone()
    if job is None or job["status"] in {
        "completed", "partial_failed", "failed", "cancelled", "expired"
    }:
        return False
    counts = _task_counts(connection, tenant_id, job_id)
    if any(counts[status] for status in _NON_TERMINAL_TASKS):
        return False
    if counts["succeeded"] == int(job["task_count"]):
        terminal = "completed"
    elif counts["cancelled"] == int(job["task_count"]):
        terminal = "cancelled"
    elif counts["expired"] and not counts["succeeded"]:
        terminal = "expired"
    elif counts["succeeded"]:
        terminal = "partial_failed"
    else:
        terminal = "failed"
    failure = connection.execute(
        """
        SELECT failure_code FROM batch_tasks
        WHERE tenant_id=? AND batch_job_id=? AND failure_code IS NOT NULL
        ORDER BY task_index,batch_task_id LIMIT 1
        """,
        (tenant_id, job_id),
    ).fetchone()
    connection.execute(
        """
        UPDATE batch_jobs
        SET status=?,completed_at=?,terminal_error_code=?,updated_at=?
        WHERE tenant_id=? AND batch_job_id=? AND status IN ('queued','running')
        """,
        (
            terminal,
            now,
            failure["failure_code"] if failure is not None else None,
            now,
            tenant_id,
            job_id,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        return False
    connection.execute(
        """
        UPDATE batch_budget_holds
        SET remaining_cost_cny='0',status=CASE
              WHEN CAST(reconciliation_cost_cny AS NUMERIC)>0 THEN 'reconciliation'
              ELSE 'released' END,
            updated_at=?
        WHERE tenant_id=? AND batch_job_id=? AND status='active'
        """,
        (now, tenant_id, job_id),
    )
    _refresh_job_costs(connection, tenant_id, job_id, now=now)
    refreshed = connection.execute(
        "SELECT * FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (tenant_id, job_id),
    ).fetchone()
    if refreshed is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    _insert_job_event(
        connection,
        tenant_id=tenant_id,
        job_id=job_id,
        event_type="batch_job.completed",
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        occurred_at=now,
        payload={
            "schema_version": 1,
            "batch_job_id": job_id,
            "status": terminal,
            "succeeded_count": counts["succeeded"],
            "failed_count": counts["failed"] + counts["outcome_unknown"],
            "cancelled_count": counts["cancelled"],
            "actual_cost_cny": _money_text(_money(refreshed["posted_cost_cny"])),
            "possible_cost_cny": _money_text(_money(refreshed["possible_cost_cny"])),
        },
    )
    return True


def _refresh_job_costs(
    connection: sqlite3.Connection,
    tenant_id: str,
    job_id: str,
    *,
    now: str,
) -> None:
    """实现批任务业务步骤。

    定义：_refresh_job_costs。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    attempts = connection.execute(
        """
        SELECT batch_attempt_id,cost_cny FROM batch_attempts
        WHERE tenant_id=? AND batch_job_id=? AND cost_cny IS NOT NULL
        """,
        (tenant_id, job_id),
    ).fetchall()
    posted = sum((_money(row["cost_cny"]) for row in attempts), Decimal("0"))
    possible_rows = connection.execute(
        """
        SELECT batch_attempt_id,reserved_cost_cny
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_job_id=?
          AND status='reconciliation' AND charge_state='possible'
        ORDER BY batch_attempt_id,budget_date,budget_rule_id
        """,
        (tenant_id, job_id),
    ).fetchall()
    possible_by_attempt: dict[str, list[object]] = {}
    for row in possible_rows:
        possible_by_attempt.setdefault(str(row["batch_attempt_id"]), []).append(
            row["reserved_cost_cny"]
        )
    possible = sum(
        (
            _consistent_allocation_amount(
                values,
                error_code="batch.worker_unavailable",
            )
            for values in possible_by_attempt.values()
        ),
        Decimal("0"),
    )
    connection.execute(
        """
        UPDATE batch_jobs
        SET posted_cost_cny=?,possible_cost_cny=?,reconciliation_pending=?,updated_at=?
        WHERE tenant_id=? AND batch_job_id=?
        """,
        (
            _money_text(_money(posted)),
            _money_text(_money(possible)),
            int(bool(possible_rows)),
            now,
            tenant_id,
            job_id,
        ),
    )


def _insert_job_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    job_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    occurred_at: str,
    payload: dict[str, object],
) -> None:
    """实现批任务业务步骤。

    定义：_insert_job_event。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    payload_json = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    connection.execute(
        """
        INSERT INTO batch_job_events(
          batch_job_event_id,tenant_id,batch_job_id,event_type,actor_type,actor_id,
          source_request_id,source_trace_id,payload_json,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"bjev_{uuid4().hex}",
            tenant_id,
            job_id,
            event_type,
            actor_type,
            actor_id,
            request_id,
            trace_id,
            payload_json,
            occurred_at,
        ),
    )


def _job_info(connection: sqlite3.Connection, row: sqlite3.Row, *, secret: str) -> BatchJobInfo:
    """实现批任务业务步骤。

    定义：_job_info。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    owner = _job_envelope_owner(str(row["tenant_id"]), str(row["batch_job_id"]))
    try:
        name = decrypt_batch_payload(row["name_ciphertext"], secret=secret, owner=owner).decode(
            "utf-8", errors="strict"
        )
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (BatchCryptoError, UnicodeError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    counts = _task_counts(connection, str(row["tenant_id"]), str(row["batch_job_id"]))
    return BatchJobInfo(
        batch_job_id=str(row["batch_job_id"]),
        name=name,
        project_id=str(row["project_id"]),
        environment_id=str(row["environment_id"]),
        model_alias_id=str(row["model_alias_id"]),
        operation=str(row["operation"]),
        priority=str(row["priority"]),
        max_duration_seconds=int(row["max_duration_seconds"]),
        max_attempts=int(row["max_attempts"]),
        max_output_tokens=int(row["max_output_tokens"]),
        status=str(row["status"]),
        progress=BatchProgress(
            total=int(row["task_count"]),
            queued=counts["queued"] + counts["retry_wait"],
            running=counts["leased"] + counts["calling"],
            succeeded=counts["succeeded"],
            failed=counts["failed"],
            cancelled=counts["cancelled"],
            expired=counts["expired"],
            outcome_unknown=counts["outcome_unknown"],
        ),
        cost=BatchCostSummary(
            estimated_cny=_money_text(_money(row["estimated_cost_cny"])),
            posted_cny=_money_text(_money(row["posted_cost_cny"])),
            possible_cny=_money_text(_money(row["possible_cost_cny"])),
            reconciliation_pending=bool(row["reconciliation_pending"]),
        ),
        deadline_at=_time(row["deadline_at"]),
        cancel_requested_at=_optional_time(row["cancel_requested_at"]),
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
        failure_code=(str(row["terminal_error_code"]) if row["terminal_error_code"] else None),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _task_info(row: sqlite3.Row) -> BatchTaskInfo:
    """实现批任务业务步骤。

    定义：_task_info。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return BatchTaskInfo(
        batch_task_id=str(row["batch_task_id"]),
        file_id=str(row["file_id"]),
        task_index=int(row["task_index"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        request_id=(str(row["terminal_request_id"]) if row["terminal_request_id"] else None),
        trace_id=(str(row["terminal_trace_id"]) if row["terminal_trace_id"] else None),
        failure_code=(str(row["failure_code"]) if row["failure_code"] else None),
        result_available=bool(row["result_available"]),
        input_tokens=(int(row["input_tokens"]) if row["input_tokens"] is not None else None),
        output_tokens=(int(row["output_tokens"]) if row["output_tokens"] is not None else None),
        cost_cny=(
            _money_text(_money(row["cost_cny"])) if row["cost_cny"] is not None else None
        ),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _task_counts(connection: sqlite3.Connection, tenant_id: str, job_id: str) -> dict[str, int]:
    """实现批任务业务步骤。

    定义：_task_counts。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    counts = {status: 0 for status in _TASK_STATUSES}
    rows = connection.execute(
        """
        SELECT status,COUNT(*) AS count FROM batch_tasks
        WHERE tenant_id=? AND batch_job_id=? GROUP BY status
        """,
        (tenant_id, job_id),
    ).fetchall()
    for row in rows:
        status = str(row["status"])
        if status not in counts or type(row["count"]) is not int:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable")
        counts[status] = int(row["count"])
    return counts


def _enforce_capacity(
    connection: sqlite3.Connection,
    config: AppConfig,
    tenant_id: str,
    tasks: int,
) -> None:
    """实现批任务业务步骤。

    定义：_enforce_capacity。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    pending_jobs = connection.execute(
        """
        SELECT COUNT(*) FROM batch_jobs
        WHERE tenant_id=? AND status IN ('queued','running')
        """,
        (tenant_id,),
    ).fetchone()[0]
    pending_tasks = connection.execute(
        """
        SELECT COUNT(*) FROM batch_tasks
        WHERE tenant_id=? AND status IN ('queued','leased','calling','retry_wait')
        """,
        (tenant_id,),
    ).fetchone()[0]
    if (
        type(pending_jobs) is not int
        or type(pending_tasks) is not int
        or pending_jobs >= config.batch_max_pending_jobs_per_tenant
        or pending_tasks + tasks > config.batch_max_pending_tasks_per_tenant
    ):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.capacity_full")


def _route_plan_from_json(value: object) -> _FrozenRoutePlan:
    """实现批任务业务步骤。

    定义：_route_plan_from_json。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if type(value) is not str:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    try:
        payload = json.loads(value)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (json.JSONDecodeError, TypeError, ValueError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "model_alias_id",
        "environment_id",
        "virtual_key_id",
        "application_id",
        "fallback_policy_version",
        "candidates",
    } or payload["schema_version"] != 1:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    application_id = payload["application_id"]
    if application_id is not None and (
        type(application_id) is not str or _BINDING_ID.fullmatch(application_id) is None
    ):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    rows = payload["candidates"]
    if type(rows) is not list or not 1 <= len(rows) <= 2:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    result: list[RouteCandidate] = []
    for index, item in enumerate(rows):
        if type(item) is not dict or set(item) != {
            "role",
            "model_id",
            "provider_id",
            "input_price_per_1k",
            "output_price_per_1k",
            "currency",
            "price_source",
            "price_version",
        }:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable")
        expected_role = "default" if index == 0 else "fallback"
        if (
            item["role"] != expected_role
            or item["currency"] != "CNY"
            or item["price_source"] not in {"price_plan", "model_catalog"}
            or type(item["price_version"]) is not str
            or not item["price_version"]
        ):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable")
        try:
            result.append(
                RouteCandidate(
                    role=expected_role,
                    model_id=str(item["model_id"]),
                    provider_id=str(item["provider_id"]),
                    input_price_per_1k=_price(item["input_price_per_1k"]),
                    output_price_per_1k=_price(item["output_price_per_1k"]),
                    price_source=str(item["price_source"]),
                    price_version=str(item["price_version"]),
                )
            )
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        except ValueError:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.worker_unavailable") from None
    return _FrozenRoutePlan(
        application_id=application_id,
        candidates=tuple(result),
    )


def _attempt_cost(
    candidates: tuple[RouteCandidate, ...],
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """实现批任务业务步骤。

    定义：_attempt_cost。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if (
        not candidates
        or type(input_tokens) is not int
        or input_tokens < 0
        or type(output_tokens) is not int
        or output_tokens < 0
    ):
        # 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
        raise ApiError("batch.cost_estimate_unavailable")
    with localcontext() as context:
        context.prec = 64
        total = sum(
            (
                (
                    Decimal(input_tokens) * candidate.input_price_per_1k
                    + Decimal(output_tokens) * candidate.output_price_per_1k
                )
                / Decimal(1000)
                for candidate in candidates
            ),
            Decimal("0"),
        )
        return total.quantize(_MONEY_QUANT, rounding=ROUND_CEILING)


def _matching_adjustment(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    adjustment_id: str,
    request_id: str,
    amount: Decimal,
) -> bool:
    """实现批任务业务步骤。

    定义：_matching_adjustment。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(finops_adjustments)")}
    required = {
        "finops_adjustment_id",
        "tenant_id",
        "adjustment_type",
        "amount_micros",
        "currency",
        "related_request_id",
        "status",
    }
    if not required <= columns:
        return False
    row = connection.execute(
        """
        SELECT adjustment_type,amount_micros,currency,status
        FROM finops_adjustments
        WHERE tenant_id=? AND finops_adjustment_id=? AND related_request_id=?
        """,
        (tenant_id, adjustment_id, request_id),
    ).fetchone()
    return (
        row is not None
        and row["adjustment_type"] == "manual_debit"
        and row["currency"] == "CNY"
        and row["status"] == "posted"
        and type(row["amount_micros"]) is int
        and row["amount_micros"] == _money_micros(amount)
    )


def _job_reconciliation_pending(
    connection: sqlite3.Connection,
    tenant_id: str,
    job_id: str,
) -> bool:
    """实现批任务业务步骤。

    定义：_job_reconciliation_pending。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    row = connection.execute(
        "SELECT reconciliation_pending FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (tenant_id, job_id),
    ).fetchone()
    if row is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.not_found")
    return bool(row["reconciliation_pending"])


def _canonical_create_request(payload: CreateBatchJobRequest) -> bytes:
    """实现批任务业务步骤。

    定义：_canonical_create_request。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    value = {
        "name": payload.name,
        "project_id": payload.project_id,
        "environment_id": payload.environment_id,
        "model_alias_id": payload.model_alias_id,
        "operation_spec": payload.operation_wire(),
        "file_ids": payload.file_ids,
        "priority": payload.priority,
        "max_duration_seconds": payload.max_duration_seconds,
        "max_attempts": payload.max_attempts,
        "max_output_tokens": payload.max_output_tokens,
    }
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (UnicodeError, TypeError, ValueError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid") from None


def _encode_cursor(
    position: list[object],
    *,
    secret: str,
    tenant_id: str,
    kind: str,
    status: str | None,
) -> str:
    """实现批任务业务步骤。

    定义：_encode_cursor。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    payload = json.dumps(
        {"v": 1, "tenant": tenant_id, "kind": kind, "status": status, "position": position},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(_secret_bytes(secret), b"cursor/v1\x00" + payload, hashlib.sha256).digest()
    return _b64(payload + signature)


def _decode_cursor(
    value: object,
    *,
    secret: str,
    tenant_id: str,
    kind: str,
    status: str | None,
) -> tuple[Any, str] | None:
    """实现批任务业务步骤。

    定义：_decode_cursor。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > _CURSOR_MAX_BYTES:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    try:
        raw = _unb64(value)
        if len(raw) <= 32:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ValueError
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(
            _secret_bytes(secret), b"cursor/v1\x00" + payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ValueError
        decoded = json.loads(payload.decode("ascii"))
        if (
            type(decoded) is not dict
            or set(decoded) != {"v", "tenant", "kind", "status", "position"}
            or decoded["v"] != 1
            or decoded["tenant"] != tenant_id
            or decoded["kind"] != kind
            or decoded["status"] != status
            or type(decoded["position"]) is not list
            or len(decoded["position"]) != 2
            or type(decoded["position"][1]) is not str
        ):
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ValueError
        return decoded["position"][0], decoded["position"][1]
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid") from None


def _canonical_uuid(value: object, error_code: str) -> str:
    """实现批任务业务步骤。

    定义：_canonical_uuid。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if type(value) is not str:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError(error_code)
    try:
        parsed = UUID(value)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (ValueError, TypeError, AttributeError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError(error_code) from None
    canonical = str(parsed)
    if value != canonical:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError(error_code)
    return canonical


def _owned_job_id(value: object) -> str:
    """实现批任务业务步骤。

    定义：_owned_job_id。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if type(value) is not str or _JOB_ID.fullmatch(value) is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.not_found")
    return value


def _owned_task_id(value: object) -> str:
    """实现批任务业务步骤。

    定义：_owned_task_id。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if type(value) is not str or _TASK_ID.fullmatch(value) is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.task_not_found")
    return value


def _optional_enum(value: object, allowed: frozenset[str]) -> str | None:
    """实现批任务业务步骤。

    定义：_optional_enum。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    return value


def _limit(value: object) -> int:
    """实现批任务业务步骤。

    定义：_limit。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if value is None:
        return 50
    if type(value) is not str or not value.isascii() or not value.isdigit():
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    parsed = int(value)
    if not 1 <= parsed <= 100:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    return parsed


def _job_envelope_owner(tenant_id: str, job_id: str) -> BatchEnvelopeOwner:
    """实现批任务业务步骤。

    定义：_job_envelope_owner。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return BatchEnvelopeOwner(
        tenant_id=tenant_id,
        batch_job_id=job_id,
        batch_task_id=f"{job_id}:metadata",
        batch_attempt_id=f"{job_id}:metadata",
        request_id=job_id,
        template_version=BATCH_OPERATION_TEMPLATE_VERSION,
        key_version=BATCH_KEY_VERSION,
    )


def _batch_secret(config: AppConfig) -> str:
    """实现批任务业务步骤。

    定义：_batch_secret。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    secret = config.batch_encryption_secret
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    if type(secret) is not str or not secret:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable")
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return secret


def _secret_bytes(secret: str) -> bytes:
    """实现批任务业务步骤。

    定义：_secret_bytes。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    try:
        # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
        return secret.encode("utf-8", errors="strict")
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except UnicodeError:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None


def _domain_hmac(secret: str, domain: bytes, value: bytes) -> str:
    """实现批任务业务步骤。

    定义：_domain_hmac。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return hmac.new(
        _secret_bytes(secret),
        b"metahub/batch/" + domain + b"\x00" + value,
        hashlib.sha256,
    ).hexdigest()


def _price(value: object) -> Decimal:
    """实现批任务业务步骤。

    定义：_price。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    try:
        return Decimal(canonical_batch_price(value))
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except BatchContractError:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError from None


def _money(value: object) -> Decimal:
    """实现批任务业务步骤。

    定义：_money。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if value is None or isinstance(value, bool):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError
    text = str(value)
    if not text or text != text.strip() or "e" in text.lower():
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError
    try:
        amount = Decimal(text)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (InvalidOperation, ValueError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError from None
    if (
        not amount.is_finite()
        or amount < 0
        or amount > _MAX_MONEY
        or amount != amount.quantize(_MONEY_QUANT)
    ):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError
    return amount


def _consistent_allocation_amount(
    values: Iterable[object],
    *,
    error_code: str,
) -> Decimal:
    """实现批任务业务步骤。

    定义：_consistent_allocation_amount。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    try:
        amounts = [_money(value) for value in values]
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (TypeError, ValueError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError(error_code) from None
    if not amounts or any(amount != amounts[0] for amount in amounts[1:]):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError(error_code)
    return amounts[0]


def _money_text(value: Decimal) -> str:
    """实现批任务业务步骤。

    定义：_money_text。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return format(_money(value), ".6f")


def _price_text(value: Decimal) -> str:
    """实现批任务业务步骤。

    定义：_price_text。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return canonical_batch_price(value)


def _money_micros(value: Decimal) -> int:
    """实现批任务业务步骤。

    定义：_money_micros。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return int(_money(value) * MICROS_PER_CNY)


def _budget_window(now: datetime) -> tuple[str, str, str]:
    """实现批任务业务步骤。

    定义：_budget_window。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    local = now.astimezone(_SHANGHAI)
    start = datetime(local.year, local.month, local.day, tzinfo=_SHANGHAI)
    return start.date().isoformat(), _db_time(start), _db_time(start + timedelta(days=1))


def _b64(value: bytes) -> str:
    """实现批任务业务步骤。

    定义：_b64。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    """实现批任务业务步骤。

    定义：_unb64。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if any(character not in alphabet for character in value):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise binascii.Error
    return base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )


def _time(value: object) -> datetime:
    """实现批任务业务步骤。

    定义：_time。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    if type(value) is not str:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ValueError
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    """实现批任务业务步骤。

    定义：_optional_time。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return None if value is None else _time(value)


def _utcnow() -> datetime:
    """实现批任务业务步骤。

    定义：_utcnow。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    """实现批任务业务步骤。

    定义：_db_time。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    """实现批任务业务步骤。

    定义：_connect。
    数据访问始终绑定租户并保留审计和失败事实。
    """
    try:
        return connect_database(config.database_url)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except DbError:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.worker_unavailable") from None


__all__ = [
    "attempt_reserved_cost",
    "cancel_batch_job",
    "create_batch_job",
    "decrypt_job_operation_spec",
    "finalize_batch_job_if_terminal",
    "get_batch_job",
    "get_batch_task_result",
    "list_batch_jobs",
    "list_batch_tasks",
    "reconcile_batch_task",
    "rollover_batch_budget_holds",
    "route_plan_hash",
]
