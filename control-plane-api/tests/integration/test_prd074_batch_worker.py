"""本文件验证 PRD-074 批处理调度边界，覆盖租户隔离、权限拒绝、并发领取、失败追踪与回退。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 Control Worker settlement integration tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import metahub_control.batch_jobs.outbox as batch_outbox
import metahub_control.batch_jobs.service as batch_service
import metahub_control.batch_jobs.worker as batch_worker
import metahub_control.webhooks.service as webhook_service
import pytest
from metahub_control.batch_jobs.models import ReconcileBatchTaskRequest
from metahub_control.batch_jobs.worker import (
    BatchTaskLease,
    GatewayCallFact,
    PreparedBatchAttempt,
    reap_expired_batch_tasks,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import encrypt_secret, secret_fingerprint, secret_prefix
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError, build_error_response

# 该常量组统一批任务状态、限额和错误语义，变更时同步契约与测试。
NOW = "2026-08-08T00:00:00Z"
LEASE_EXPIRED = "2026-08-08T00:30:00Z"
REAP_AT = datetime(2026, 8, 8, 0, 45, tzinfo=UTC)
DEADLINE = "2026-08-08T01:00:00Z"


def _url(path: Path) -> str:
    """验证批处理场景。

    定义：_url。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def prd074_worker_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """验证批处理场景。

    定义：prd074_worker_template。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path_factory.mktemp("prd074-worker-template") / "template.sqlite3"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = "Prd074WorkerTestOnly!2026"
    try:
        applied = apply_migrations(database_url).applied
        # Worker 相邻回归必须覆盖后续迁移共存，精确 0067 栈顶由迁移专项负责。
        assert "0067" in applied
        apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return database


def _insert_success_fixture(
    database_url: str,
    *,
    with_allocation: bool = True,
    prepared_attempt: bool = False,
    cross_business_day: bool = False,
    allocation_amount: str = "0.000784",
) -> dict[str, str]:
    """验证批处理场景。

    定义：_insert_success_fixture。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    connection = connect_database(database_url)
    identity = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,environment.environment_id,environment.code,
               model.model_id,model.provider_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        JOIN environments environment ON environment.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' AND user.role='admin'
          AND environment.code='dev' AND environment.status='active'
          AND model.status='active'
        ORDER BY user.user_id,model.model_id LIMIT 1
        """
    ).fetchone()
    assert identity is not None
    tenant_id, user_id, environment_id, environment_code, model_id, provider_id = map(
        str,
        identity,
    )
    ids = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": environment_id,
        "model_id": model_id,
        "provider_id": provider_id,
        "project_id": "prj_prd074_worker",
        "model_alias_id": "mal_prd074_worker",
        "virtual_key_id": "vkey_prd074_worker",
        "file_id": "file_" + "1" * 32,
        "batch_job_id": "bjob_" + "2" * 32,
        "batch_task_id": "btsk_" + "3" * 32,
        "batch_attempt_id": "batm_" + "4" * 32,
        "budget_rule_id": "br_prd074_worker",
        "batch_budget_hold_id": "bbhd_" + "5" * 32,
        "batch_budget_allocation_id": "bbal_" + "6" * 32,
    }
    route_plan = json.dumps(
        {
            "schema_version": 1,
            "model_alias_id": ids["model_alias_id"],
            "environment_id": environment_id,
            "virtual_key_id": ids["virtual_key_id"],
            "application_id": None,
            "fallback_policy_version": None,
            "candidates": [
                {
                    "role": "default",
                    "model_id": model_id,
                    "provider_id": provider_id,
                    "input_price_per_1k": "0.001000",
                    "output_price_per_1k": "0.002000",
                    "currency": "CNY",
                    "price_source": "model_catalog",
                    "price_version": "prd074-worker-test-v1",
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    ids["route_plan_json"] = route_plan
    job_created = (
        "2026-08-08T15:59:30Z" if cross_business_day else NOW
    )
    job_deadline = (
        "2026-08-08T16:59:30Z" if cross_business_day else DEADLINE
    )
    lease_expires_at = (
        "2026-08-08T16:01:00Z"
        if cross_business_day
        else "2026-08-08T00:01:30Z"
        if prepared_attempt
        else LEASE_EXPIRED
    )
    budget_date = "2026-08-08"
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'Batch worker fixture',NULL,?,'active',?,?,?,?)
        """,
        (
            ids["project_id"],
            tenant_id,
            "prd074-worker",
            user_id,
            user_id,
            user_id,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,?,?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
        """,
        (
            ids["model_alias_id"],
            tenant_id,
            "batch-worker-alias",
            environment_code,
            model_id,
            user_id,
            NOW,
            NOW,
            environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_batch',?,'active',?,?,?,NULL,NULL,?,NULL,NULL,?,NULL,?)
        """,
        (
            ids["virtual_key_id"],
            tenant_id,
            "Batch worker key",
            "7" * 64,
            NOW,
            NOW,
            environment_code,
            user_id,
            ids["project_id"],
            environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO uploaded_files(
          file_id,tenant_id,uploaded_by,source_request_id,source_trace_id,
          original_name_ciphertext,file_extension,media_type,size_bytes,
          content_fingerprint,status,virus_scan_status,dlp_scan_status,dlp_action,
          effective_dlp_rule_id,match_count,ruleset_fingerprint,storage_backend,
          original_object_key,failure_error_code,created_at,updated_at,completed_at
        ) VALUES(?,?,?,?,?,?,'txt','text/plain',64,?,'processing','not_configured',
                 'pending',NULL,NULL,0,NULL,NULL,NULL,NULL,?,?,NULL)
        """,
        (
            ids["file_id"],
            tenant_id,
            user_id,
            "req_prd074_worker_file",
            "trc_prd074_worker_file",
            b"x" * 64,
            "hmac-sha256:" + "8" * 64,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        UPDATE uploaded_files
        SET status='ready',dlp_scan_status='clean',ruleset_fingerprint=?,
            storage_backend='local_encrypted_v1',original_object_key=?,
            updated_at=?,completed_at=?
        WHERE tenant_id=? AND file_id=? AND status='processing'
        """,
        (
            "sha256:" + "9" * 64,
            "files/prd074-worker/original.enc",
            NOW,
            NOW,
            tenant_id,
            ids["file_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO file_extracts(
          file_extract_id,tenant_id,file_id,status,extractor_version,text_size_bytes,
          text_code_points,estimated_tokens,estimator_version,text_fingerprint,
          storage_backend,extracted_object_key,failure_error_code,created_at,updated_at
        ) VALUES(?,?,?,'ready','txt-v1',64,64,16,'file-token-estimator-v1',?,
                 'local_encrypted_v1',?,NULL,?,?)
        """,
        (
            "fext_" + "a" * 32,
            tenant_id,
            ids["file_id"],
            "hmac-sha256:" + "b" * 64,
            "files/prd074-worker/extracted.enc",
            NOW,
            NOW,
        ),
    )
    if with_allocation:
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
              daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
              exhausted_action,status,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,'virtual_key',?,'Batch worker budget',100,80,100,
                     'block','active',?,?,?,?)
            """,
            (
                ids["budget_rule_id"],
                tenant_id,
                environment_id,
                ids["virtual_key_id"],
                user_id,
                user_id,
                NOW,
                NOW,
            ),
        )
    connection.execute(
        """
        INSERT INTO batch_jobs(
          batch_job_id,tenant_id,submitted_by_user_id,idempotency_key_hash,
          request_fingerprint,project_id,environment_id,model_alias_id,virtual_key_id,
          route_plan_json,name_ciphertext,operation,operation_spec_ciphertext,
          operation_template_version,priority,max_duration_seconds,max_attempts,
          max_output_tokens,task_count,status,estimated_input_tokens,
          estimated_output_tokens,estimated_cost_cny,posted_cost_cny,possible_cost_cny,
          reconciliation_pending,deadline_at,started_at,source_request_id,source_trace_id,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'summarize',?,
                 'batch-operation-templates-v1','normal',3600,2,256,1,'running',
                 64,32,'0.001568','0','0',0,?,?,?,?,?,?)
        """,
        (
            ids["batch_job_id"],
            tenant_id,
            user_id,
            "9" * 64,
            "a" * 64,
            ids["project_id"],
            environment_id,
            ids["model_alias_id"],
            ids["virtual_key_id"],
            route_plan,
            "enc:v1:" + "b" * 40,
            "enc:v1:" + "c" * 40,
            job_deadline,
            job_created,
            "req_prd074_worker_job",
            "trc_prd074_worker_job",
            job_created,
            job_created,
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_tasks(
          batch_task_id,tenant_id,batch_job_id,file_id,task_index,priority_rank,
          status,attempt_count,next_attempt_at,lease_token,lease_owner_id,
          lease_generation,lease_expires_at,current_attempt_id,result_available,
          created_at,updated_at
        ) VALUES(?,?,?,?,0,2,?,?,?,'lease-prd074-worker','worker-prd074',
                 1,?,?,0,?,?)
        """,
        (
            ids["batch_task_id"],
            tenant_id,
            ids["batch_job_id"],
            ids["file_id"],
            "leased" if prepared_attempt else "calling",
            0 if prepared_attempt else 1,
            job_created,
            lease_expires_at,
            ids["batch_attempt_id"],
            job_created,
            job_created,
        ),
    )
    if prepared_attempt:
        connection.execute(
            """
            INSERT INTO batch_attempts(
              batch_attempt_id,tenant_id,batch_job_id,batch_task_id,attempt_no,
              request_id,trace_id,status,execution_outcome,retry_safe,gateway_error_code,
              request_payload_ciphertext,request_payload_key_version,request_payload_hmac,
              route_plan_json,gateway_write_deadline_at,result_state,created_at,updated_at
            ) VALUES(?,?,?,?,1,?,?,'prepared','not_invoked',0,NULL,?,1,?,?,
                     NULL,'none',?,?)
            """,
            (
                ids["batch_attempt_id"],
                tenant_id,
                ids["batch_job_id"],
                ids["batch_task_id"],
                "breq_prd074_worker",
                "btrc_prd074_worker",
                "enc:v1:" + "d" * 40,
                "e" * 64,
                route_plan,
                job_created,
                job_created,
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO batch_attempts(
              batch_attempt_id,tenant_id,batch_job_id,batch_task_id,attempt_no,
              request_id,trace_id,status,execution_outcome,retry_safe,gateway_error_code,
              request_payload_ciphertext,request_payload_key_version,request_payload_hmac,
              route_plan_json,gateway_write_deadline_at,result_state,result_ciphertext,
              result_key_version,result_hmac,input_tokens,output_tokens,cost_cny,
              provider_started_at,gateway_completed_at,created_at,updated_at
            ) VALUES(?,?,?,?,1,?,?,'provider_succeeded','succeeded',0,NULL,?,1,?,?,
                     ?,'available',?,1,?,10,5,NULL,?,?,?,?)
            """,
            (
                ids["batch_attempt_id"],
                tenant_id,
                ids["batch_job_id"],
                ids["batch_task_id"],
                "breq_prd074_worker",
                "btrc_prd074_worker",
                "enc:v1:" + "d" * 40,
                "e" * 64,
                route_plan,
                job_deadline,
                "enc:v1:" + "f" * 40,
                "0" * 64,
                job_created,
                job_created,
                job_created,
                job_created,
            ),
        )
    if with_allocation:
        connection.execute(
            """
            INSERT INTO batch_budget_holds(
              batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
              scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
              reconciliation_cost_cny,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'virtual_key',?,?,'0.001568','0.000784',
                     '0','active',?,?)
            """,
            (
                ids["batch_budget_hold_id"],
                tenant_id,
                ids["batch_job_id"],
                ids["budget_rule_id"],
                environment_id,
                ids["virtual_key_id"],
                budget_date,
                job_created,
                job_created,
            ),
        )
        connection.execute(
            """
            INSERT INTO batch_budget_allocations(
              batch_budget_allocation_id,tenant_id,batch_job_id,batch_task_id,
              batch_attempt_id,batch_budget_hold_id,budget_rule_id,budget_date,
              reserved_cost_cny,route_plan_hash,execution_outcome,charge_state,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'not_invoked','none',
                     'active',?,?)
            """,
            (
                ids["batch_budget_allocation_id"],
                tenant_id,
                ids["batch_job_id"],
                ids["batch_task_id"],
                ids["batch_attempt_id"],
                ids["batch_budget_hold_id"],
                ids["budget_rule_id"],
                budget_date,
                allocation_amount,
                hashlib.sha256(route_plan.encode("utf-8")).hexdigest(),
                job_created,
                job_created,
            ),
        )
    connection.commit()
    connection.close()
    return ids


def _copy_row(
    connection: sqlite3.Connection,
    table: str,
    where: str,
    parameters: tuple[object, ...],
    updates: dict[str, object],
) -> None:
    """验证批处理场景。

    定义：_copy_row。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    row = connection.execute(f"SELECT * FROM {table} WHERE {where}", parameters).fetchone()
    assert row is not None
    columns = tuple(row.keys())
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        tuple(updates.get(column, row[column]) for column in columns),
    )


def _clone_tenant_owner_graph(
    connection: sqlite3.Connection,
    source: dict[str, str],
    marker: str,
) -> dict[str, str]:
    """验证批处理场景。

    定义：_clone_tenant_owner_graph。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    tenant_id = f"tenant_prd074_scheduler_{marker}"
    user_id = f"user_prd074_scheduler_{marker}"
    provider_id = f"provider_prd074_scheduler_{marker}"
    model_id = f"model_prd074_scheduler_{marker}"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (tenant_id, f"prd074-scheduler-{marker}", f"Scheduler {marker}", NOW, NOW),
    )
    environment = connection.execute(
        "SELECT environment_id,code FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()
    assert environment is not None
    environment_id = str(environment["environment_id"])
    _copy_row(
        connection,
        "users",
        "tenant_id=? AND user_id=?",
        (source["tenant_id"], source["user_id"]),
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "email": f"prd074-scheduler-{marker}@example.invalid",
            "display_name": f"Scheduler {marker}",
        },
    )
    _copy_row(
        connection,
        "providers",
        "tenant_id=? AND provider_id=?",
        (source["tenant_id"], source["provider_id"]),
        {
            "provider_id": provider_id,
            "tenant_id": tenant_id,
            "created_by": user_id,
            "disabled_by": None,
        },
    )
    _copy_row(
        connection,
        "models",
        "tenant_id=? AND model_id=?",
        (source["tenant_id"], source["model_id"]),
        {
            "model_id": model_id,
            "tenant_id": tenant_id,
            "provider_id": provider_id,
            "created_by": user_id,
            "disabled_by": None,
        },
    )
    graph = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": environment_id,
        "model_id": model_id,
        "provider_id": provider_id,
        "project_id": f"prj_prd074_scheduler_{marker}",
        "model_alias_id": f"mal_prd074_scheduler_{marker}",
        "virtual_key_id": f"vkey_prd074_scheduler_{marker}",
        "file_id": "file_" + marker * 32,
    }
    _copy_row(
        connection,
        "projects",
        "tenant_id=? AND project_id=?",
        (source["tenant_id"], source["project_id"]),
        {
            "project_id": graph["project_id"],
            "tenant_id": tenant_id,
            "project_code": f"prd074-scheduler-{marker}",
            "owner_user_id": user_id,
            "created_by": user_id,
            "updated_by": user_id,
        },
    )
    _copy_row(
        connection,
        "model_aliases",
        "tenant_id=? AND model_alias_id=?",
        (source["tenant_id"], source["model_alias_id"]),
        {
            "model_alias_id": graph["model_alias_id"],
            "tenant_id": tenant_id,
            "alias": f"batch-scheduler-{marker}",
            "default_model_id": model_id,
            "created_by": user_id,
            "environment_id": environment_id,
        },
    )
    _copy_row(
        connection,
        "virtual_keys",
        "tenant_id=? AND virtual_key_id=?",
        (source["tenant_id"], source["virtual_key_id"]),
        {
            "virtual_key_id": graph["virtual_key_id"],
            "tenant_id": tenant_id,
            "name": f"Batch scheduler key {marker}",
            "key_hash": marker * 64,
            "created_by": user_id,
            "project_id": graph["project_id"],
            "environment_id": environment_id,
        },
    )
    _copy_row(
        connection,
        "uploaded_files",
        "tenant_id=? AND file_id=?",
        (source["tenant_id"], source["file_id"]),
        {
            "file_id": graph["file_id"],
            "tenant_id": tenant_id,
            "uploaded_by": user_id,
            "source_request_id": f"req_prd074_scheduler_file_{marker}",
            "source_trace_id": f"trc_prd074_scheduler_file_{marker}",
            "content_fingerprint": "hmac-sha256:" + marker * 64,
            "status": "processing",
            "dlp_scan_status": "pending",
            "dlp_action": None,
            "effective_dlp_rule_id": None,
            "match_count": 0,
            "ruleset_fingerprint": None,
            "storage_backend": None,
            "original_object_key": None,
            "failure_error_code": None,
            "completed_at": None,
        },
    )
    connection.execute(
        """
        UPDATE uploaded_files
        SET status='ready',dlp_scan_status='clean',ruleset_fingerprint=?,
            storage_backend='local_encrypted_v1',original_object_key=?,
            updated_at=?,completed_at=?
        WHERE tenant_id=? AND file_id=? AND status='processing'
        """,
        (
            "sha256:" + marker * 64,
            f"files/prd074-scheduler-{marker}/original.enc",
            NOW,
            NOW,
            tenant_id,
            graph["file_id"],
        ),
    )
    return graph


def _insert_queued_job_task(
    connection: sqlite3.Connection,
    source: dict[str, str],
    owner: dict[str, str],
    marker: str,
    *,
    priority_rank: int,
) -> tuple[str, str]:
    """验证批处理场景。

    定义：_insert_queued_job_task。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    job_id = "bjob_" + marker * 32
    task_id = "btsk_" + marker * 32
    route_plan = source["route_plan_json"]
    route_plan = route_plan.replace(source["model_id"], owner["model_id"])
    route_plan = route_plan.replace(source["provider_id"], owner["provider_id"])
    route_plan = route_plan.replace(source["model_alias_id"], owner["model_alias_id"])
    route_plan = route_plan.replace(source["virtual_key_id"], owner["virtual_key_id"])
    _copy_row(
        connection,
        "batch_jobs",
        "tenant_id=? AND batch_job_id=?",
        (source["tenant_id"], source["batch_job_id"]),
        {
            "batch_job_id": job_id,
            "tenant_id": owner["tenant_id"],
            "submitted_by_user_id": owner["user_id"],
            "idempotency_key_hash": marker * 64,
            "request_fingerprint": marker * 63 + "0",
            "project_id": owner["project_id"],
            "environment_id": owner["environment_id"],
            "model_alias_id": owner["model_alias_id"],
            "virtual_key_id": owner["virtual_key_id"],
            "route_plan_json": route_plan,
            "priority": {1: "low", 2: "normal", 3: "high"}[priority_rank],
            "status": "queued",
            "posted_cost_cny": "0",
            "possible_cost_cny": "0",
            "reconciliation_pending": 0,
            "cancel_requested_at": None,
            "cancelled_by_user_id": None,
            "started_at": None,
            "completed_at": None,
            "terminal_error_code": None,
            "source_request_id": f"req_prd074_scheduler_job_{marker}",
            "source_trace_id": f"trc_prd074_scheduler_job_{marker}",
        },
    )
    _copy_row(
        connection,
        "batch_tasks",
        "tenant_id=? AND batch_task_id=?",
        (source["tenant_id"], source["batch_task_id"]),
        {
            "batch_task_id": task_id,
            "tenant_id": owner["tenant_id"],
            "batch_job_id": job_id,
            "file_id": owner["file_id"],
            "priority_rank": priority_rank,
            "status": "queued",
            "attempt_count": 0,
            "next_attempt_at": NOW,
            "cancel_requested_at": None,
            "lease_token": None,
            "lease_owner_id": None,
            "lease_generation": 0,
            "lease_expires_at": None,
            "current_attempt_id": None,
            "terminal_request_id": None,
            "terminal_trace_id": None,
            "failure_code": None,
            "result_available": 0,
            "completed_at": None,
        },
    )
    return job_id, task_id


def _insert_created_event(
    connection: sqlite3.Connection,
    ids: dict[str, str],
    marker: str,
    *,
    payload: dict[str, object] | None = None,
) -> tuple[str, str]:
    """验证批处理场景。

    定义：_insert_created_event。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    event_id = "bjev_" + marker * 32
    serialized = json.dumps(
        payload
        or {
            "schema_version": 1,
            "batch_job_id": ids["batch_job_id"],
            "operation": "summarize",
            "priority": "normal",
            "task_count": 1,
            "estimated_cost_cny": "0.001568",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO batch_job_events(
          batch_job_event_id,tenant_id,batch_job_id,event_type,actor_type,actor_id,
          source_request_id,source_trace_id,payload_json,occurred_at
        ) VALUES(?,?,?,'batch_job.created','user',?,?,?,?,?)
        """,
        (
            event_id,
            ids["tenant_id"],
            ids["batch_job_id"],
            ids["user_id"],
            f"req_prd074_outbox_{marker}",
            f"trc_prd074_outbox_{marker}",
            serialized,
            NOW,
        ),
    )
    return event_id, serialized


def _insert_batch_resume_fixture(
    connection: sqlite3.Connection,
    ids: dict[str, str],
    marker: str,
    *,
    event_type: str,
    occurred_at: str,
) -> tuple[str, str, str]:
    """验证批处理场景。

    定义：_insert_batch_resume_fixture。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    event_id = "bjev_" + marker * 32
    payload = (
        {
            "schema_version": 1,
            "batch_job_id": ids["batch_job_id"],
            "operation": "summarize",
            "priority": "normal",
            "task_count": 1,
            "estimated_cost_cny": "0.001568",
        }
        if event_type == "batch_job.created"
        else {
            "schema_version": 1,
            "batch_job_id": ids["batch_job_id"],
            "status": "completed",
            "succeeded_count": 1,
            "failed_count": 0,
            "cancelled_count": 0,
            "actual_cost_cny": "0.000784",
            "possible_cost_cny": "0.000000",
        }
    )
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    request_id = f"req_prd074_resume_{marker}"
    trace_id = f"trc_prd074_resume_{marker}"
    actor_type = "user" if event_type == "batch_job.created" else "system"
    actor_id = ids["user_id"] if actor_type == "user" else "batch-worker-prd074-test"
    connection.execute(
        """
        INSERT INTO batch_job_events(
          batch_job_event_id,tenant_id,batch_job_id,event_type,actor_type,actor_id,
          source_request_id,source_trace_id,payload_json,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            ids["tenant_id"],
            ids["batch_job_id"],
            event_type,
            actor_type,
            actor_id,
            request_id,
            trace_id,
            payload_json,
            occurred_at,
        ),
    )
    endpoint_id = f"wh_prd074_resume_{marker}"
    signing_secret = f"whsec-prd074-resume-{marker}-test-only"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_at,updated_at
        ) VALUES(?,?,?,'http://127.0.0.1:65530/webhook',?,?,?,?, 'active',1000,3,?,?)
        """,
        (
            endpoint_id,
            ids["tenant_id"],
            f"PRD074 resume {marker}",
            json.dumps([event_type], separators=(",", ":")),
            encrypt_secret(signing_secret),
            secret_prefix(signing_secret),
            secret_fingerprint(signing_secret),
            occurred_at,
            occurred_at,
        ),
    )
    return event_id, request_id, payload_json


def _unit_prepared_attempt() -> tuple[BatchTaskLease, PreparedBatchAttempt]:
    """验证批处理场景。

    定义：_unit_prepared_attempt。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    lease = BatchTaskLease(
        tenant_id="tenant_prd074_worker",
        batch_job_id="bjob_" + "1" * 32,
        batch_task_id="btsk_" + "2" * 32,
        batch_attempt_id="batm_" + "3" * 32,
        file_id="file_" + "4" * 32,
        submitted_by_user_id="user_prd074_worker",
        model_alias_id="mal_prd074_worker",
        virtual_key_id="vkey_prd074_worker",
        route_plan_json="{}",
        operation_spec_ciphertext="enc:v1:test-only",
        max_output_tokens=256,
        max_attempts=2,
        attempt_no=1,
        lease_token="lease-test-only",
        lease_owner_id="worker-test-only",
        lease_generation=1,
        lease_expires_at=datetime(2026, 8, 8, 0, 30, tzinfo=UTC),
        deadline_at=datetime(2026, 8, 8, 2, 0, tzinfo=UTC),
    )
    return lease, PreparedBatchAttempt(
        lease=lease,
        request_id="breq_prd074_worker",
        trace_id="btrc_prd074_worker",
        batch_execution_grant_id="begr_" + "5" * 32,
        grant="grant-test-only",
    )


def _cross_day_prepared_attempt(
    ids: dict[str, str],
) -> tuple[BatchTaskLease, PreparedBatchAttempt]:
    """验证批处理场景。

    定义：_cross_day_prepared_attempt。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    lease = BatchTaskLease(
        tenant_id=ids["tenant_id"],
        batch_job_id=ids["batch_job_id"],
        batch_task_id=ids["batch_task_id"],
        batch_attempt_id=ids["batch_attempt_id"],
        file_id=ids["file_id"],
        submitted_by_user_id=ids["user_id"],
        model_alias_id=ids["model_alias_id"],
        virtual_key_id=ids["virtual_key_id"],
        route_plan_json=ids["route_plan_json"],
        operation_spec_ciphertext="enc:v1:" + "c" * 40,
        max_output_tokens=256,
        max_attempts=2,
        attempt_no=1,
        lease_token="lease-prd074-worker",
        lease_owner_id="worker-prd074",
        lease_generation=1,
        lease_expires_at=datetime(2026, 8, 8, 16, 1, tzinfo=UTC),
        deadline_at=datetime(2026, 8, 8, 16, 59, 30, tzinfo=UTC),
        existing_attempt=True,
    )
    return lease, PreparedBatchAttempt(
        lease=lease,
        request_id="breq_prd074_worker",
        trace_id="btrc_prd074_worker",
        batch_execution_grant_id="bgrt_" + "8" * 32,
        grant="grant-test-only",
    )


def test_gateway_call_recognizes_unified_top_level_busy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_gateway_call_recognizes_unified_top_level_busy_error。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    _lease, prepared = _unit_prepared_attempt()
    payload = build_error_response(
        ApiError("batch.gateway_busy"),
        request_id=prepared.request_id,
        trace_id=prepared.trace_id,
    ).model_dump(mode="json")

    def gateway_busy(request: httpx.Request) -> httpx.Response:
        """验证批处理场景。

        定义：gateway_busy。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        assert request.url == "http://127.0.0.1:8000/internal/batch/chat/completions"
        assert request.headers["X-MetaHub-Internal-Batch-Token"] == "batch-token-test-only"
        assert json.loads(request.content) == {"grant": prepared.grant}
        return httpx.Response(429, json=payload)

    monkeypatch.setattr(batch_worker, "_heartbeat_loop", lambda *_args: None)
    config = SimpleNamespace(
        internal_batch_token="batch-token-test-only",
        gateway_api_base_url="http://127.0.0.1:8000",
        batch_heartbeat_seconds=1,
    )
    with httpx.Client(transport=httpx.MockTransport(gateway_busy)) as client:
        fact = batch_worker.call_batch_gateway(
            config,  # type: ignore[arg-type]
            prepared,
            client=client,
        )
    assert fact == GatewayCallFact(outcome="busy", error_code="batch.gateway_busy")


def test_busy_retries_use_bounded_backoff_and_one_provider_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_busy_retries_use_bounded_backoff_and_one_provider_attempt。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    current = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    lease, prepared = _unit_prepared_attempt()
    facts = [GatewayCallFact(outcome="busy", error_code="batch.gateway_busy") for _ in range(16)]
    facts.append(GatewayCallFact(outcome="accepted"))
    delays: list[float] = []
    counters = {"prepare": 0, "refresh": 0, "calls": 0, "settle": 0}

    def prepare_once(_config: object, claimed: BatchTaskLease) -> PreparedBatchAttempt:
        """验证批处理场景。

        定义：prepare_once。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        assert claimed is lease
        counters["prepare"] += 1
        return prepared

    def call_once(
        _config: object,
        current_prepared: PreparedBatchAttempt,
        *,
        client: object,
    ) -> GatewayCallFact:
        """验证批处理场景。

        定义：call_once。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        assert current_prepared is prepared
        assert client is None
        counters["calls"] += 1
        return facts.pop(0)

    def refresh_once(
        _config: object,
        current_prepared: PreparedBatchAttempt,
    ) -> PreparedBatchAttempt:
        """验证批处理场景。

        定义：refresh_once。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        assert current_prepared is prepared
        counters["refresh"] += 1
        return prepared

    def settle_once(
        _config: object,
        current_prepared: PreparedBatchAttempt,
        fact: GatewayCallFact,
    ) -> bool:
        """验证批处理场景。

        定义：settle_once。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        assert current_prepared is prepared
        assert fact.outcome == "accepted"
        counters["settle"] += 1
        return True

    monkeypatch.setattr(batch_worker, "reap_expired_batch_tasks", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(batch_worker, "claim_next_batch_task", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(batch_worker, "prepare_batch_attempt", prepare_once)
    monkeypatch.setattr(batch_worker, "call_batch_gateway", call_once)
    monkeypatch.setattr(batch_worker, "refresh_batch_grant", refresh_once)
    monkeypatch.setattr(batch_worker, "settle_batch_attempt", settle_once)
    monkeypatch.setattr(batch_worker, "_utcnow", lambda: current)
    monkeypatch.setattr(batch_worker.time, "sleep", delays.append)

    config = SimpleNamespace(batch_realtime_cooldown_milliseconds=250)
    assert batch_worker.run_batch_task_once(
        config,  # type: ignore[arg-type]
        worker_id="worker-prd074-test",
    )
    assert counters == {"prepare": 1, "refresh": 16, "calls": 17, "settle": 1}
    assert sum(delays) > 60
    assert delays[:6] == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0]
    assert max(delays) == 5.0


def test_stopped_worker_does_not_reap_or_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证批处理场景。

    定义：test_stopped_worker_does_not_reap_or_claim。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    stop = threading.Event()
    stop.set()

    def unexpected(*_args: object, **_kwargs: object) -> None:
        """验证批处理场景。

        定义：unexpected。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise AssertionError("stopped worker touched the queue")

    monkeypatch.setattr(batch_worker, "reap_expired_batch_tasks", unexpected)
    monkeypatch.setattr(batch_worker, "claim_next_batch_task", unexpected)
    config = SimpleNamespace(batch_realtime_cooldown_milliseconds=250)
    assert not batch_worker.run_batch_task_once(
        config,  # type: ignore[arg-type]
        worker_id="worker-prd074-stopped",
        stop_event=stop,
    )


def test_stop_interrupts_busy_wait_before_grant_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_stop_interrupts_busy_wait_before_grant_refresh。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    lease, prepared = _unit_prepared_attempt()
    delays: list[float] = []
    refreshed = 0

    class StopOnWait:
        """验证批处理场景。

        定义：StopOnWait。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        stopped = False

        def is_set(self) -> bool:
            """验证批处理场景。

            定义：is_set。
            固定租户隔离、权限拒绝、失败追踪和回退边界。
            """
            return self.stopped

        def wait(self, timeout: float) -> bool:
            """验证批处理场景。

            定义：wait。
            固定租户隔离、权限拒绝、失败追踪和回退边界。
            """
            delays.append(timeout)
            self.stopped = True
            return True

    def refresh_unexpected(*_args: object, **_kwargs: object) -> PreparedBatchAttempt:
        """验证批处理场景。

        定义：refresh_unexpected。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        nonlocal refreshed
        refreshed += 1
        return prepared

    monkeypatch.setattr(batch_worker, "reap_expired_batch_tasks", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(batch_worker, "claim_next_batch_task", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(
        batch_worker,
        "prepare_batch_attempt",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        batch_worker,
        "call_batch_gateway",
        lambda *_args, **_kwargs: GatewayCallFact(
            outcome="busy",
            error_code="batch.gateway_busy",
        ),
    )
    monkeypatch.setattr(batch_worker, "refresh_batch_grant", refresh_unexpected)
    monkeypatch.setattr(
        batch_worker,
        "_utcnow",
        lambda: datetime(2026, 8, 8, 0, 0, tzinfo=UTC),
    )
    config = SimpleNamespace(batch_realtime_cooldown_milliseconds=250)
    assert batch_worker.run_batch_task_once(
        config,  # type: ignore[arg-type]
        worker_id="worker-prd074-stopping",
        stop_event=StopOnWait(),  # type: ignore[arg-type]
    )
    assert delays == [0.25]
    assert refreshed == 0


def test_busy_beyond_grant_ttl_rotates_one_attempt_without_reaper_takeover(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_busy_beyond_grant_ttl_rotates_one_attempt_without_reaper_takeover。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-worker-busy-grants.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(
        database_url,
        with_allocation=False,
        prepared_attempt=True,
    )
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    started = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    lease = BatchTaskLease(
        tenant_id=ids["tenant_id"],
        batch_job_id=ids["batch_job_id"],
        batch_task_id=ids["batch_task_id"],
        batch_attempt_id=ids["batch_attempt_id"],
        file_id=ids["file_id"],
        submitted_by_user_id=ids["user_id"],
        model_alias_id=ids["model_alias_id"],
        virtual_key_id=ids["virtual_key_id"],
        route_plan_json=ids["route_plan_json"],
        operation_spec_ciphertext="enc:v1:" + "c" * 40,
        max_output_tokens=256,
        max_attempts=2,
        attempt_no=1,
        lease_token="lease-prd074-worker",
        lease_owner_id="worker-prd074",
        lease_generation=1,
        lease_expires_at=started + timedelta(seconds=90),
        deadline_at=datetime(2026, 8, 8, 1, 0, tzinfo=UTC),
        existing_attempt=True,
    )
    prepared = PreparedBatchAttempt(
        lease=lease,
        request_id="breq_prd074_worker",
        trace_id="btrc_prd074_worker",
        batch_execution_grant_id="bgrt_" + "7" * 32,
        grant="grant-test-only",
    )
    elapsed = 0.0
    for consecutive_busy in range(1, 17):
        elapsed += batch_worker._busy_backoff_seconds(config, consecutive_busy)
        prepared = batch_worker.refresh_batch_grant(
            config,
            prepared,
            now=started + timedelta(seconds=elapsed),
        )

    assert elapsed > 60
    assert prepared.request_id == "breq_prd074_worker"
    assert prepared.trace_id == "btrc_prd074_worker"
    assert prepared.lease.batch_attempt_id == ids["batch_attempt_id"]
    assert prepared.grant_generation == 16
    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-busy-reaper",
        now=started + timedelta(seconds=65),
    ) == 0

    connection = connect_database(database_url)
    task = connection.execute(
        """
        SELECT status,attempt_count,current_attempt_id,lease_generation
        FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?
        """,
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    attempt = connection.execute(
        """
        SELECT status,request_id,trace_id FROM batch_attempts
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchone()
    grants = connection.execute(
        """
        SELECT status,COUNT(*) AS count FROM batch_execution_grants
        WHERE tenant_id=? AND batch_attempt_id=? GROUP BY status
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchall()
    assert tuple(task) == ("leased", 0, ids["batch_attempt_id"], 1)
    assert tuple(attempt) == (
        "prepared",
        "breq_prd074_worker",
        "btrc_prd074_worker",
    )
    assert {row["status"]: row["count"] for row in grants} == {
        "issued": 1,
        "revoked": 15,
    }
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_attempts WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prepared_attempt_rolls_budget_to_new_business_day_idempotently(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_prepared_attempt_rolls_budget_to_new_business_day_idempotently。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-worker-rollover.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(
        database_url,
        prepared_attempt=True,
        cross_business_day=True,
    )
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    _lease, prepared = _cross_day_prepared_attempt(ids)
    current = datetime(2026, 8, 8, 16, 0, 5, tzinfo=UTC)

    first = batch_worker.refresh_batch_grant(config, prepared, now=current)
    second = batch_worker.refresh_batch_grant(
        config,
        first,
        now=current + timedelta(seconds=10),
    )
    assert first.grant_generation == 1
    assert second.grant_generation == 2
    assert first.request_id == second.request_id == "breq_prd074_worker"
    assert first.lease.batch_attempt_id == second.lease.batch_attempt_id

    connection = connect_database(database_url)
    task = connection.execute(
        "SELECT status,attempt_count,current_attempt_id FROM batch_tasks "
        "WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    holds = connection.execute(
        """
        SELECT budget_date,status,reserved_cost_cny,remaining_cost_cny
        FROM batch_budget_holds WHERE tenant_id=? AND batch_job_id=?
        ORDER BY budget_date
        """,
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchall()
    allocations = connection.execute(
        """
        SELECT budget_date,status,execution_outcome,charge_state,reserved_cost_cny
        FROM batch_budget_allocations WHERE tenant_id=? AND batch_attempt_id=?
        ORDER BY budget_date
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchall()
    grants = connection.execute(
        """
        SELECT grant_generation,status,request_id,trace_id,batch_attempt_id
        FROM batch_execution_grants WHERE tenant_id=? AND batch_attempt_id=?
        ORDER BY grant_generation
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchall()
    assert tuple(task) == ("leased", 0, ids["batch_attempt_id"])
    assert [tuple(row) for row in holds] == [
        ("2026-08-08", "released", "0.001568", "0"),
        ("2026-08-09", "active", "0.001568", "0.000784"),
    ]
    assert [tuple(row) for row in allocations] == [
        ("2026-08-08", "released", "not_invoked", "none", "0.000784"),
        ("2026-08-09", "active", "not_invoked", "none", "0.000784"),
    ]
    assert [tuple(row) for row in grants] == [
        (1, "revoked", "breq_prd074_worker", "btrc_prd074_worker", ids["batch_attempt_id"]),
        (2, "issued", "breq_prd074_worker", "btrc_prd074_worker", ids["batch_attempt_id"]),
    ]
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_attempts WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prepared_attempt_rollover_budget_rejection_rolls_back_new_day(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_prepared_attempt_rollover_budget_rejection_rolls_back_new_day。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-worker-rollover-rejected.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(
        database_url,
        prepared_attempt=True,
        cross_business_day=True,
    )
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_by,updated_by,created_at,updated_at
        ) VALUES('zz_prd074_rollover_reject',?,?,'project',?,
                 'Rollover rejection fixture','0.001568',80,100,'block','active',?,?,?,?)
        """,
        (
            ids["tenant_id"],
            ids["environment_id"],
            ids["project_id"],
            ids["user_id"],
            ids["user_id"],
            NOW,
            NOW,
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    _lease, prepared = _cross_day_prepared_attempt(ids)

    with pytest.raises(ApiError) as captured:
        batch_worker.refresh_batch_grant(
            config,
            prepared,
            now=datetime(2026, 8, 8, 16, 0, 5, tzinfo=UTC),
        )
    assert captured.value.code == "batch.budget_exceeded"

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_budget_holds WHERE tenant_id=? AND batch_job_id=? "
        "AND budget_date='2026-08-09'",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_budget_allocations "
        "WHERE tenant_id=? AND batch_attempt_id=? AND budget_date='2026-08-09'",
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchone()[0] == 0
    task = connection.execute(
        "SELECT status,attempt_count,failure_code FROM batch_tasks "
        "WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    job = connection.execute(
        "SELECT status,terminal_error_code FROM batch_jobs "
        "WHERE tenant_id=? AND batch_job_id=?",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    old_hold = connection.execute(
        "SELECT status,remaining_cost_cny FROM batch_budget_holds "
        "WHERE tenant_id=? AND batch_budget_hold_id=?",
        (ids["tenant_id"], ids["batch_budget_hold_id"]),
    ).fetchone()
    old_allocation = connection.execute(
        "SELECT status,execution_outcome,charge_state FROM batch_budget_allocations "
        "WHERE tenant_id=? AND batch_budget_allocation_id=?",
        (ids["tenant_id"], ids["batch_budget_allocation_id"]),
    ).fetchone()
    assert tuple(task) == ("failed", 0, "batch.budget_exceeded")
    assert tuple(job) == ("failed", "batch.budget_exceeded")
    assert tuple(old_hold) == ("released", "0")
    assert tuple(old_allocation) == ("released", "not_invoked", "none")
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_execution_grants WHERE tenant_id=? AND batch_attempt_id=?",
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_reaper_waits_for_usage_replay_before_immutable_completion_event(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_reaper_waits_for_usage_replay_before_immutable_completion_event。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-worker.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )

    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-reaper",
        now=REAP_AT,
    ) == 0
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()[0] == "calling"
    assert connection.execute(
        "SELECT status FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()[0] == "running"
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_job_events "
        "WHERE tenant_id=? AND event_type='batch_job.completed'",
        (ids["tenant_id"],),
    ).fetchone()[0] == 0

    connection.execute(
        "UPDATE batch_attempts SET cost_cny='0.123456',updated_at=? "
        "WHERE tenant_id=? AND batch_attempt_id=?",
        (NOW, ids["tenant_id"], ids["batch_attempt_id"]),
    )
    connection.commit()
    connection.close()
    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-reaper",
        now=REAP_AT,
    ) == 0

    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET execution_outcome='succeeded',charge_state='posted',status='posted',updated_at=?
        WHERE tenant_id=? AND batch_budget_allocation_id=?
        """,
        (NOW, ids["tenant_id"], ids["batch_budget_allocation_id"]),
    )
    connection.commit()
    connection.close()

    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-reaper",
        now=REAP_AT,
    ) == 1
    connection = connect_database(database_url)
    task = connection.execute(
        "SELECT status,result_available FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    job = connection.execute(
        "SELECT status,posted_cost_cny FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    event = connection.execute(
        """
        SELECT event.payload_json,outbox.dispatch_status
        FROM batch_job_events event
        JOIN batch_job_event_outbox outbox
          ON outbox.tenant_id=event.tenant_id
         AND outbox.batch_job_event_id=event.batch_job_event_id
        WHERE event.tenant_id=? AND event.batch_job_id=?
          AND event.event_type='batch_job.completed'
        """,
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    hold = connection.execute(
        "SELECT status,remaining_cost_cny FROM batch_budget_holds "
        "WHERE tenant_id=? AND batch_budget_hold_id=?",
        (ids["tenant_id"], ids["batch_budget_hold_id"]),
    ).fetchone()
    assert tuple(task) == ("succeeded", 1)
    assert tuple(job) == ("completed", "0.123456")
    assert event["dispatch_status"] == "pending"
    assert json.loads(event["payload_json"])["actual_cost_cny"] == "0.123456"
    assert tuple(hold) == ("released", "0")
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_reaper_allows_posted_cost_without_applicable_budget_allocations(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_reaper_allows_posted_cost_without_applicable_budget_allocations。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-worker-no-allocation.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url, with_allocation=False)
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )

    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-no-allocation",
        now=REAP_AT,
    ) == 0
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_budget_allocations WHERE tenant_id=?",
        (ids["tenant_id"],),
    ).fetchone()[0] == 0
    connection.execute(
        "UPDATE batch_attempts SET cost_cny='0.010000',updated_at=? "
        "WHERE tenant_id=? AND batch_attempt_id=?",
        (NOW, ids["tenant_id"], ids["batch_attempt_id"]),
    )
    connection.commit()
    connection.close()

    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-no-allocation",
        now=REAP_AT,
    ) == 1
    connection = connect_database(database_url)
    job = connection.execute(
        "SELECT status,posted_cost_cny FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    event = connection.execute(
        """
        SELECT payload_json FROM batch_job_events
        WHERE tenant_id=? AND batch_job_id=? AND event_type='batch_job.completed'
        """,
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    assert tuple(job) == ("completed", "0.010000")
    assert json.loads(event["payload_json"])["actual_cost_cny"] == "0.010000"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_reaper_preserves_known_non_retryable_failure(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_reaper_preserves_known_non_retryable_failure。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-known-failure-reaper.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE batch_attempts
        SET status='known_failed',execution_outcome='known_failed',retry_safe=0,
            gateway_error_code='batch.request_invalid',result_state='none',
            result_ciphertext=NULL,result_key_version=NULL,result_hmac=NULL,
            input_tokens=NULL,output_tokens=NULL,cost_cny=NULL,
            gateway_completed_at=?,updated_at=?
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (NOW, NOW, ids["tenant_id"], ids["batch_attempt_id"]),
    )
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET execution_outcome='known_failed',charge_state='none',status='released',
            updated_at=?
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (NOW, ids["tenant_id"], ids["batch_attempt_id"]),
    )
    connection.commit()
    connection.close()

    assert reap_expired_batch_tasks(
        config,
        worker_id="test-prd074-known-failure-reaper",
        now=datetime(2026, 8, 8, 1, 15, tzinfo=UTC),
    ) == 1

    connection = connect_database(database_url)
    task = connection.execute(
        """
        SELECT status,failure_code,result_available
        FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?
        """,
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    attempt = connection.execute(
        """
        SELECT status,execution_outcome,retry_safe,gateway_error_code
        FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchone()
    allocation = connection.execute(
        """
        SELECT status,execution_outcome,charge_state
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchone()
    job = connection.execute(
        """
        SELECT status,terminal_error_code,possible_cost_cny,reconciliation_pending
        FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?
        """,
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    assert tuple(task) == ("failed", "batch.request_invalid", 0)
    assert tuple(attempt) == (
        "known_failed",
        "known_failed",
        0,
        "batch.request_invalid",
    )
    assert tuple(allocation) == ("released", "known_failed", "none")
    assert tuple(job) == ("failed", "batch.request_invalid", "0.000000", 0)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM batch_job_events
        WHERE tenant_id=? AND batch_job_id=? AND event_type='batch_job.completed'
        """,
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_reconciliation_rejects_inconsistent_multi_rule_amounts_without_cost_update(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_reconciliation_rejects_inconsistent_multi_rule_amounts_without_cost_update。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-reconciliation-inconsistent.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url, allocation_amount="100")
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    connection = connect_database(database_url)
    second_rule_id = "br_prd074_worker_secondary"
    second_hold_id = "bbhd_" + "7" * 32
    second_allocation_id = "bbal_" + "8" * 32
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET execution_outcome='unknown',charge_state='possible',status='reconciliation',
            updated_at=?
        WHERE tenant_id=? AND batch_budget_allocation_id=?
        """,
        (NOW, ids["tenant_id"], ids["batch_budget_allocation_id"]),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'project',?,'Secondary reconciliation rule',200,80,100,
                 'block','active',?,?,?,?)
        """,
        (
            second_rule_id,
            ids["tenant_id"],
            ids["environment_id"],
            ids["project_id"],
            ids["user_id"],
            ids["user_id"],
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_budget_holds(
          batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
          scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
          reconciliation_cost_cny,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'project',?,'2026-08-08','9','0','9',
                 'reconciliation',?,?)
        """,
        (
            second_hold_id,
            ids["tenant_id"],
            ids["batch_job_id"],
            second_rule_id,
            ids["environment_id"],
            ids["project_id"],
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_budget_allocations(
          batch_budget_allocation_id,tenant_id,batch_job_id,batch_task_id,
          batch_attempt_id,batch_budget_hold_id,budget_rule_id,budget_date,
          reserved_cost_cny,route_plan_hash,execution_outcome,charge_state,status,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'2026-08-08','9',?,'unknown','possible',
                 'reconciliation',?,?)
        """,
        (
            second_allocation_id,
            ids["tenant_id"],
            ids["batch_job_id"],
            ids["batch_task_id"],
            ids["batch_attempt_id"],
            second_hold_id,
            second_rule_id,
            hashlib.sha256(ids["route_plan_json"].encode("utf-8")).hexdigest(),
            NOW,
            NOW,
        ),
    )
    connection.commit()
    connection.close()

    context = SimpleNamespace(
        user=SimpleNamespace(tenant_id=ids["tenant_id"], user_id=ids["user_id"])
    )
    monkeypatch.setattr(
        batch_service,
        "require_transaction_permission",
        lambda _connection, current, _permission, **_kwargs: current,
    )
    with pytest.raises(ApiError) as error:
        batch_service.reconcile_batch_task(
            config,
            context,
            ids["batch_job_id"],
            ids["batch_task_id"],
            ReconcileBatchTaskRequest(decision="confirm_not_charged"),
            idempotency_key="00000000-0000-4000-8000-000000000074",
            request_id="req_prd074_reconciliation_conflict",
            trace_id="trc_prd074_reconciliation_conflict",
        )
    assert error.value.code == "batch.reconciliation_conflict"

    connection = connect_database(database_url)
    allocations = connection.execute(
        """
        SELECT reserved_cost_cny,execution_outcome,charge_state,status
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_attempt_id=?
        ORDER BY reserved_cost_cny
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchall()
    assert [tuple(row) for row in allocations] == [
        ("100", "unknown", "possible", "reconciliation"),
        ("9", "unknown", "possible", "reconciliation"),
    ]
    assert connection.execute(
        "SELECT COUNT(*) FROM batch_reconciliation_events WHERE tenant_id=?",
        (ids["tenant_id"],),
    ).fetchone()[0] == 0
    with pytest.raises(ApiError) as refresh_error:
        batch_service._refresh_job_costs(
            connection,
            ids["tenant_id"],
            ids["batch_job_id"],
            now=NOW,
        )
    assert refresh_error.value.code == "batch.worker_unavailable"
    connection.rollback()
    job = connection.execute(
        "SELECT possible_cost_cny,reconciliation_pending FROM batch_jobs "
        "WHERE tenant_id=? AND batch_job_id=?",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    assert tuple(job) == ("0", 0)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_scheduler_rotates_due_work_across_tenants(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_scheduler_rotates_due_work_across_tenants。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-scheduler-fairness.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    source = _insert_success_fixture(database_url)
    connection = connect_database(database_url)
    other = _clone_tenant_owner_graph(connection, source, "d")
    _insert_queued_job_task(connection, source, source, "c", priority_rank=2)
    _insert_queued_job_task(connection, source, source, "e", priority_rank=2)
    _insert_queued_job_task(connection, source, other, "d", priority_rank=2)
    _insert_queued_job_task(connection, source, other, "f", priority_rank=2)
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={
            "database_url": database_url,
            "batch_global_concurrency": 8,
            "batch_tenant_concurrency": 4,
        }
    )
    leases = [
        batch_worker.claim_next_batch_task(
            config,
            worker_id=f"prd074-fair-worker-{index}",
            now=REAP_AT,
        )
        for index in range(1, 5)
    ]
    assert all(lease is not None for lease in leases)
    tenant_sequence = [lease.tenant_id for lease in leases if lease is not None]
    assert tenant_sequence[0] != tenant_sequence[1]
    assert tenant_sequence[0] == tenant_sequence[2]
    assert tenant_sequence[1] == tenant_sequence[3]
    assert set(tenant_sequence) == {
        source["tenant_id"],
        other["tenant_id"],
    }

    connection = connect_database(database_url)
    cursors = connection.execute(
        """
        SELECT tenant_id,last_dispatched_at,dispatch_revision
        FROM batch_scheduler_tenants
        WHERE tenant_id IN (?,?) ORDER BY tenant_id
        """,
        (source["tenant_id"], other["tenant_id"]),
    ).fetchall()
    assert len(cursors) == 2
    assert {row["last_dispatched_at"] for row in cursors} == {
        "2026-08-08T00:45:00Z"
    }
    assert {row["dispatch_revision"] for row in cursors} == {2}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_scheduler_prefers_high_priority_within_one_tenant(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_scheduler_prefers_high_priority_within_one_tenant。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-scheduler-priority.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    source = _insert_success_fixture(database_url)
    connection = connect_database(database_url)
    _, low_task_id = _insert_queued_job_task(
        connection,
        source,
        source,
        "c",
        priority_rank=1,
    )
    _, high_task_id = _insert_queued_job_task(
        connection,
        source,
        source,
        "e",
        priority_rank=3,
    )
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={
            "database_url": database_url,
            "batch_global_concurrency": 8,
            "batch_tenant_concurrency": 4,
        }
    )
    high = batch_worker.claim_next_batch_task(
        config,
        worker_id="prd074-priority-worker-1",
        now=REAP_AT,
    )
    low = batch_worker.claim_next_batch_task(
        config,
        worker_id="prd074-priority-worker-2",
        now=REAP_AT,
    )
    assert high is not None and high.batch_task_id == high_task_id
    assert low is not None and low.batch_task_id == low_task_id


def test_cancel_after_claim_is_terminalized_by_first_reaper_pass(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_cancel_after_claim_is_terminalized_by_first_reaper_pass。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-cancel-claim-race.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    source = _insert_success_fixture(database_url)
    connection = connect_database(database_url)
    job_id, task_id = _insert_queued_job_task(
        connection,
        source,
        source,
        "c",
        priority_rank=2,
    )
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url, "batch_lease_seconds": 90}
    )
    claimed_at = datetime(2026, 8, 8, 0, 45, tzinfo=UTC)
    lease = batch_worker.claim_next_batch_task(
        config,
        worker_id="prd074-cancel-race-worker",
        now=claimed_at,
    )
    assert lease is not None and lease.batch_task_id == task_id

    cancelled_at = "2026-08-08T00:45:30Z"
    # Reproduce the durable interleaving written by cancel_batch_job between claim and prepare.
    connection = connect_database(database_url)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """
        UPDATE batch_jobs
        SET cancel_requested_at=?,cancelled_by_user_id=?,updated_at=?
        WHERE tenant_id=? AND batch_job_id=?
        """,
        (
            cancelled_at,
            source["user_id"],
            cancelled_at,
            source["tenant_id"],
            job_id,
        ),
    )
    connection.execute(
        """
        UPDATE batch_tasks SET cancel_requested_at=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=? AND status='leased'
        """,
        (cancelled_at, cancelled_at, source["tenant_id"], task_id),
    )
    connection.commit()
    connection.close()

    assert reap_expired_batch_tasks(
        config,
        worker_id="prd074-cancel-race-reaper",
        now=datetime(2026, 8, 8, 0, 47, tzinfo=UTC),
    ) == 1
    connection = connect_database(database_url)
    task = connection.execute(
        """
        SELECT status,current_attempt_id,lease_token,result_available
        FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?
        """,
        (source["tenant_id"], task_id),
    ).fetchone()
    job = connection.execute(
        "SELECT status FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (source["tenant_id"], job_id),
    ).fetchone()
    assert tuple(task) == ("cancelled", None, None, 0)
    assert job["status"] == "cancelled"
    connection.close()


def test_claim_without_attempt_expires_on_first_reaper_pass(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_claim_without_attempt_expires_on_first_reaper_pass。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-deadline-claim-race.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    source = _insert_success_fixture(database_url)
    connection = connect_database(database_url)
    job_id, task_id = _insert_queued_job_task(
        connection,
        source,
        source,
        "c",
        priority_rank=2,
    )
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url, "batch_lease_seconds": 120}
    )
    lease = batch_worker.claim_next_batch_task(
        config,
        worker_id="prd074-deadline-race-worker",
        now=datetime(2026, 8, 8, 0, 59, tzinfo=UTC),
    )
    assert lease is not None and lease.batch_task_id == task_id
    assert lease.lease_expires_at == datetime(2026, 8, 8, 1, 0, tzinfo=UTC)

    assert reap_expired_batch_tasks(
        config,
        worker_id="prd074-deadline-race-reaper",
        now=datetime(2026, 8, 8, 1, 1, tzinfo=UTC),
    ) == 1
    connection = connect_database(database_url)
    task = connection.execute(
        """
        SELECT status,current_attempt_id,lease_token,result_available
        FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?
        """,
        (source["tenant_id"], task_id),
    ).fetchone()
    job = connection.execute(
        "SELECT status FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (source["tenant_id"], job_id),
    ).fetchone()
    assert tuple(task) == ("expired", None, None, 0)
    assert job["status"] == "expired"
    connection.close()


def test_cancel_after_provider_result_discards_payload_but_posts_cost(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_cancel_after_provider_result_discards_payload_but_posts_cost。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-cancel-settle-race.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    cancelled_at = "2026-08-08T00:10:00Z"
    connection = connect_database(database_url)
    connection.execute(
        "UPDATE batch_attempts SET cost_cny='0.123456',updated_at=? "
        "WHERE tenant_id=? AND batch_attempt_id=?",
        (cancelled_at, ids["tenant_id"], ids["batch_attempt_id"]),
    )
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET execution_outcome='succeeded',charge_state='posted',status='posted',updated_at=?
        WHERE tenant_id=? AND batch_budget_allocation_id=?
        """,
        (cancelled_at, ids["tenant_id"], ids["batch_budget_allocation_id"]),
    )
    connection.execute(
        """
        UPDATE batch_jobs SET cancel_requested_at=?,cancelled_by_user_id=?,updated_at=?
        WHERE tenant_id=? AND batch_job_id=?
        """,
        (
            cancelled_at,
            ids["user_id"],
            cancelled_at,
            ids["tenant_id"],
            ids["batch_job_id"],
        ),
    )
    connection.execute(
        """
        UPDATE batch_tasks SET cancel_requested_at=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=?
        """,
        (cancelled_at, cancelled_at, ids["tenant_id"], ids["batch_task_id"]),
    )
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    lease = BatchTaskLease(
        tenant_id=ids["tenant_id"],
        batch_job_id=ids["batch_job_id"],
        batch_task_id=ids["batch_task_id"],
        batch_attempt_id=ids["batch_attempt_id"],
        file_id=ids["file_id"],
        submitted_by_user_id=ids["user_id"],
        model_alias_id=ids["model_alias_id"],
        virtual_key_id=ids["virtual_key_id"],
        route_plan_json=ids["route_plan_json"],
        operation_spec_ciphertext="enc:v1:" + "c" * 40,
        max_output_tokens=256,
        max_attempts=2,
        attempt_no=1,
        lease_token="lease-prd074-worker",
        lease_owner_id="worker-prd074",
        lease_generation=1,
        lease_expires_at=datetime(2026, 8, 8, 0, 30, tzinfo=UTC),
        deadline_at=datetime(2026, 8, 8, 1, 0, tzinfo=UTC),
        existing_attempt=True,
    )
    prepared = PreparedBatchAttempt(
        lease=lease,
        request_id="breq_prd074_worker",
        trace_id="btrc_prd074_worker",
        batch_execution_grant_id="bgrt_" + "7" * 32,
        grant="test-only-grant",
    )
    assert batch_worker.settle_batch_attempt(
        config,
        prepared,
        GatewayCallFact(outcome="accepted"),
        now=datetime(2026, 8, 8, 0, 15, tzinfo=UTC),
    )

    connection = connect_database(database_url)
    task = connection.execute(
        "SELECT status,result_available FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?",
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    attempt = connection.execute(
        """
        SELECT result_state,result_ciphertext,cost_cny
        FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (ids["tenant_id"], ids["batch_attempt_id"]),
    ).fetchone()
    job = connection.execute(
        "SELECT status,posted_cost_cny FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?",
        (ids["tenant_id"], ids["batch_job_id"]),
    ).fetchone()
    assert tuple(task) == ("cancelled", 0)
    assert tuple(attempt) == ("discarded", None, "0.123456")
    assert tuple(job) == ("cancelled", "0.123456")
    connection.close()


def test_prepared_attempt_is_reclaimed_with_same_identity_after_downtime(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_prepared_attempt_is_reclaimed_with_same_identity_after_downtime。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-worker-downtime.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url, prepared_attempt=True)
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )

    assert reap_expired_batch_tasks(
        config,
        worker_id="prd074-downtime-reaper",
        now=REAP_AT,
    ) == 1
    connection = connect_database(database_url)
    recovered = connection.execute(
        """
        SELECT status,current_attempt_id,lease_token
        FROM batch_tasks WHERE tenant_id=? AND batch_task_id=?
        """,
        (ids["tenant_id"], ids["batch_task_id"]),
    ).fetchone()
    assert tuple(recovered) == ("retry_wait", ids["batch_attempt_id"], None)
    connection.close()

    reclaimed = batch_worker.claim_next_batch_task(
        config,
        worker_id="prd074-downtime-worker",
        now=datetime(2026, 8, 8, 0, 46, tzinfo=UTC),
    )
    assert reclaimed is not None
    assert reclaimed.batch_attempt_id == ids["batch_attempt_id"]
    assert reclaimed.existing_attempt is True
    assert reclaimed.lease_generation == 2


def test_batch_outbox_freezes_empty_endpoint_snapshot_and_completes(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_batch_outbox_freezes_empty_endpoint_snapshot_and_completes。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-outbox-empty-snapshot.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_endpoints WHERE tenant_id=? AND status='active'",
        (ids["tenant_id"],),
    ).fetchone()[0] == 0
    event_id, payload_json = _insert_created_event(connection, ids, "c")
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    now = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    claim = batch_outbox.claim_next_batch_event(
        config,
        worker_id="prd074-outbox-empty",
        now=now,
    )
    assert claim is not None
    assert batch_outbox.dispatch_claimed_batch_event(config, claim, now=now)

    connection = connect_database(database_url)
    outbox = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,delivery_snapshot_frozen_at,last_error_code
        FROM batch_job_event_outbox WHERE tenant_id=? AND batch_job_event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    source_payload = connection.execute(
        "SELECT payload_json FROM batch_job_events WHERE tenant_id=? AND batch_job_event_id=?",
        (ids["tenant_id"], event_id),
    ).fetchone()[0]
    deliveries = connection.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE tenant_id=? AND event_id=?",
        (ids["tenant_id"], event_id),
    ).fetchone()[0]
    assert tuple(outbox) == ("dispatched", 1, NOW, None)
    assert source_payload == payload_json
    assert deliveries == 0
    connection.close()


@pytest.mark.parametrize(
    ("event_type", "marker"),
    [
        ("batch_job.created", "a"),
        ("batch_job.completed", "b"),
    ],
)
def test_batch_outbox_pending_resume_restores_immutable_source_request(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    marker: str,
) -> None:
    """验证批处理场景。

    定义：test_batch_outbox_pending_resume_restores_immutable_source_request。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / f"prd074-outbox-resume-{marker}.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    occurred = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=2)
    occurred_at = occurred.isoformat().replace("+00:00", "Z")
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv(
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "prd074-resume-encryption-secret-test-only",
    )
    connection = connect_database(database_url)
    event_id, source_request_id, payload_json = _insert_batch_resume_fixture(
        connection,
        ids,
        marker,
        event_type=event_type,
        occurred_at=occurred_at,
    )
    connection.commit()
    connection.close()
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    sent_bodies: list[dict[str, object]] = []

    def record_success(
        _config: object,
        _endpoint: object,
        body: bytes,
        seen_delivery_id: str,
    ) -> dict[str, object]:
        """验证批处理场景。

        定义：record_success。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        assert seen_delivery_id.startswith("whd_")
        sent_bodies.append(json.loads(body.decode("utf-8")))
        return {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }

    monkeypatch.setattr(webhook_service, "_send_once", record_success)
    claim = batch_outbox.claim_next_batch_event(
        config,
        worker_id=f"prd074-resume-{marker}",
    )
    assert claim is not None
    assert claim.event_id == event_id
    assert claim.source_request_id == source_request_id
    assert batch_outbox.dispatch_claimed_batch_event(config, claim)

    assert len(sent_bodies) == 1
    assert sent_bodies[0]["event_id"] == event_id
    assert sent_bodies[0]["event_type"] == event_type
    assert sent_bodies[0]["payload"] == json.loads(payload_json)
    assert source_request_id not in json.dumps(sent_bodies[0], sort_keys=True)
    connection = connect_database(database_url)
    outbox = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,last_error_code
        FROM batch_job_event_outbox
        WHERE tenant_id=? AND batch_job_event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    delivery = connection.execute(
        """
        SELECT status,attempt_count,event_type,payload_json
        FROM webhook_deliveries WHERE tenant_id=? AND event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    attempts = connection.execute(
        """
        SELECT attempt_no,status FROM webhook_delivery_attempts
        WHERE tenant_id=? AND webhook_delivery_id=(
          SELECT webhook_delivery_id FROM webhook_deliveries
          WHERE tenant_id=? AND event_id=?
        )
        """,
        (ids["tenant_id"], ids["tenant_id"], event_id),
    ).fetchall()
    assert tuple(outbox) == ("dispatched", 1, None)
    assert tuple(delivery) == (
        "success",
        1,
        event_type,
        json.dumps(sent_bodies[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    assert [tuple(row) for row in attempts] == [(1, "success")]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_batch_source_request_recovery_fails_closed_on_identity_drift(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_batch_source_request_recovery_fails_closed_on_identity_drift。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-outbox-source-drift.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    occurred_at = "2026-08-08T00:00:01Z"
    monkeypatch.setenv(
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "prd074-resume-encryption-secret-test-only",
    )
    connection = connect_database(database_url)
    event_id, source_request_id, _payload_json = _insert_batch_resume_fixture(
        connection,
        ids,
        "f",
        event_type="batch_job.created",
        occurred_at=occurred_at,
    )
    connection.commit()
    source = connection.execute(
        """
        SELECT * FROM batch_job_events
        WHERE tenant_id=? AND batch_job_event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    envelope = batch_outbox._event(
        batch_outbox._claim_from_row(
            source,
            token="1" * 32,
            owner="prd074-source-drift",
            attempts=1,
            expires=datetime(2026, 8, 8, 0, 5, tzinfo=UTC),
        )
    ).to_payload()
    assert webhook_service._batch_job_source_request_id(connection, envelope) == source_request_id

    drifted: list[dict[str, object]] = []
    for key, value in (
        ("tenant_id", "tenant_prd074_other"),
        ("event_id", "bjev_" + "e" * 32),
        ("actor_id", "user_prd074_other"),
        ("trace_id", "trc_prd074_other"),
        ("timestamp", "2026-08-08T00:00:02Z"),
    ):
        candidate = json.loads(json.dumps(envelope))
        candidate[key] = value
        drifted.append(candidate)
    wrong_job = json.loads(json.dumps(envelope))
    wrong_job["object_id"] = "bjob_" + "e" * 32
    wrong_job["payload"]["batch_job_id"] = wrong_job["object_id"]
    drifted.append(wrong_job)
    wrong_payload = json.loads(json.dumps(envelope))
    wrong_payload["payload"]["estimated_cost_cny"] = "0.001569"
    drifted.append(wrong_payload)
    wrong_type = json.loads(json.dumps(envelope))
    wrong_type["event_type"] = "batch_job.completed"
    wrong_type["payload"] = {
        "schema_version": 1,
        "batch_job_id": ids["batch_job_id"],
        "status": "completed",
        "succeeded_count": 1,
        "failed_count": 0,
        "cancelled_count": 0,
        "actual_cost_cny": "0.000784",
        "possible_cost_cny": "0.000000",
    }
    drifted.append(wrong_type)

    for candidate in drifted:
        with pytest.raises(ApiError) as captured:
            webhook_service._batch_job_source_request_id(connection, candidate)
        assert captured.value.code == "webhook.delivery_unavailable"
    connection.close()


def test_batch_outbox_retries_then_reclaims_expired_processing_lease(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_batch_outbox_retries_then_reclaims_expired_processing_lease。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-outbox-retry-reclaim.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    connection = connect_database(database_url)
    event_id, payload_json = _insert_created_event(connection, ids, "d")
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    original_dispatch = webhook_service.dispatch_webhook_event

    def fail_dispatch(*_args: object, **_kwargs: object) -> None:
        """验证批处理场景。

        定义：fail_dispatch。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise RuntimeError("test-only delivery outage")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    start = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    first = batch_outbox.claim_next_batch_event(
        config,
        worker_id="prd074-outbox-retry-1",
        now=start,
    )
    assert first is not None and first.dispatch_attempts == 1
    assert batch_outbox.dispatch_claimed_batch_event(config, first, now=start)

    connection = connect_database(database_url)
    retry = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,next_attempt_at,claim_token,
               lease_owner_id,last_error_code,delivery_snapshot_frozen_at
        FROM batch_job_event_outbox WHERE tenant_id=? AND batch_job_event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    assert tuple(retry) == (
        "pending",
        1,
        "2026-08-08T00:00:05Z",
        None,
        None,
        "webhook.delivery_unavailable",
        NOW,
    )
    connection.close()

    second_at = datetime(2026, 8, 8, 0, 0, 5, tzinfo=UTC)
    second = batch_outbox.claim_next_batch_event(
        config,
        worker_id="prd074-outbox-retry-2",
        now=second_at,
    )
    assert second is not None and second.dispatch_attempts == 2
    reclaimed_at = datetime(2026, 8, 8, 0, 1, 35, tzinfo=UTC)
    third = batch_outbox.claim_next_batch_event(
        config,
        worker_id="prd074-outbox-retry-3",
        now=reclaimed_at,
    )
    assert third is not None and third.dispatch_attempts == 3
    assert third.claim_token != second.claim_token
    assert third.lease_owner_id == "prd074-outbox-retry-3"

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", original_dispatch)
    assert batch_outbox.dispatch_claimed_batch_event(config, third, now=reclaimed_at)
    connection = connect_database(database_url)
    final = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,claim_token,lease_owner_id,last_error_code
        FROM batch_job_event_outbox WHERE tenant_id=? AND batch_job_event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    source_payload = connection.execute(
        "SELECT payload_json FROM batch_job_events WHERE tenant_id=? AND batch_job_event_id=?",
        (ids["tenant_id"], event_id),
    ).fetchone()[0]
    assert tuple(final) == ("dispatched", 3, None, None, None)
    assert source_payload == payload_json
    connection.close()


def test_batch_outbox_isolates_corrupt_source_without_mutating_event(
    prd074_worker_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_batch_outbox_isolates_corrupt_source_without_mutating_event。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path / "prd074-outbox-corrupt-source.sqlite3"
    shutil.copy2(prd074_worker_template, database)
    database_url = _url(database)
    ids = _insert_success_fixture(database_url)
    corrupt_payload = {
        "schema_version": 1,
        "batch_job_id": "bjob_" + "f" * 32,
        "operation": "summarize",
        "priority": "normal",
        "task_count": 1,
        "estimated_cost_cny": "0.001568",
    }
    connection = connect_database(database_url)
    event_id, payload_json = _insert_created_event(
        connection,
        ids,
        "e",
        payload=corrupt_payload,
    )
    connection.commit()
    connection.close()

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": database_url}
    )
    assert batch_outbox.claim_next_batch_event(
        config,
        worker_id="prd074-outbox-corrupt",
        now=datetime(2026, 8, 8, 0, 0, tzinfo=UTC),
    ) is None

    connection = connect_database(database_url)
    source = connection.execute(
        "SELECT payload_json FROM batch_job_events WHERE tenant_id=? AND batch_job_event_id=?",
        (ids["tenant_id"], event_id),
    ).fetchone()[0]
    outbox = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,last_error_code,claim_token,lease_owner_id
        FROM batch_job_event_outbox WHERE tenant_id=? AND batch_job_event_id=?
        """,
        (ids["tenant_id"], event_id),
    ).fetchone()
    assert source == payload_json
    assert tuple(outbox) == ("failed", 1, "webhook.payload_invalid", None, None)
    with pytest.raises(sqlite3.IntegrityError, match="batch_event_immutable"):
        connection.execute(
            "UPDATE batch_job_events SET payload_json=payload_json WHERE batch_job_event_id=?",
            (event_id,),
        )
    connection.rollback()
    connection.close()
