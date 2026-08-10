"""本文件验证 PRD-074 批处理调度边界，覆盖租户隔离、权限拒绝、并发领取、失败追踪与回退。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 internal Batch grant consumption against the real 0067 schema.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from metahub_gateway.api.internal_batch import (
    InternalBatchExecution,
    _consume_execution_grant,
    _persist_encrypted_result,
    _record_execution_failure,
)
from metahub_gateway.budgets import service as budget_service
from metahub_gateway.budgets.service import enforce_budget_guard
from metahub_gateway.output_safety.service import prepare_output_safety_scan
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.usage.ledger import (
    CostEstimate,
    PriceSnapshotEvidence,
    RequestLogContext,
    _prepare_usage_ledger_intent,
    _serialize_usage_spool_record,
    claim_gateway_request,
    record_request_log,
    replay_usage_spool_record,
)
from metahub_gateway.usage.spool import UsageSpool
from metahub_shared.batch import (
    BATCH_OPERATION_TEMPLATE_VERSION,
    BatchEnvelopeOwner,
    batch_payload_hmac,
    encrypt_batch_payload,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import TokenEstimate

# 该常量组统一批任务状态、限额和错误语义，变更时同步契约与测试。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SECRET = "test-only-prd074-batch-envelope-secret-0000000001"
GRANT = "grant_prd074_" + "g" * 43


def _url(path: Path) -> str:
    """验证批处理场景。

    定义：_url。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    return f"sqlite:///{path.as_posix()}"


def _db_time(value: datetime) -> str:
    """验证批处理场景。

    定义：_db_time。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """验证批处理场景。

    定义：migrated_template。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = tmp_path_factory.mktemp("prd074-gateway-template") / "template.sqlite3"
    database_url = _url(database)
    applied = apply_migrations(database_url, migrations_dir=MIGRATIONS_DIR).applied
    # 相邻回归必须在当前完整 schema 上运行；只冻结 Batch 自身的 0067 已实际应用，
    # 不能因后续 PRD 合法新增迁移就让所有 Batch 行为测试在夹具阶段失败。
    assert "0067" in applied
    apply_seed_data(database_url)
    return database


def _config(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
) -> AppConfig:
    """验证批处理场景。

    定义：_config。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    monkeypatch.setenv("METAHUB_DATABASE_URL", _url(database))
    monkeypatch.setenv("METAHUB_BATCH_ENCRYPTION_SECRET", SECRET)
    monkeypatch.setenv("METAHUB_INTERNAL_BATCH_TOKEN", "test-only-internal-batch-token-00000001")
    monkeypatch.setenv(
        "METAHUB_FINOPS_USAGE_SPOOL_ROOT",
        str(database.parent / f"{database.stem}-finops-spool"),
    )
    return load_app_config("gateway-data-plane")


def _route_plan(
    *,
    model_alias_id: str,
    environment_id: str,
    virtual_key_id: str,
    application_id: str | None,
    model_id: str,
    provider_id: str,
) -> str:
    """验证批处理场景。

    定义：_route_plan。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    return json.dumps(
        {
            "schema_version": 1,
            "model_alias_id": model_alias_id,
            "environment_id": environment_id,
            "virtual_key_id": virtual_key_id,
            "application_id": application_id,
            "fallback_policy_version": None,
            "candidates": [
                {
                    "role": "default",
                    "model_id": model_id,
                    "provider_id": provider_id,
                    "input_price_per_1k": "0.010000000000",
                    "output_price_per_1k": "0.020000000000",
                    "currency": "CNY",
                    "price_source": "model_catalog",
                    "price_version": "prd074-batch-route-v1",
                }
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _prepare_execution(
    database: Path,
    *,
    corrupt_hmac: bool = False,
    expired_grant: bool = False,
    max_attempts: int = 2,
    attempt_count: int = 0,
    attempt_no: int = 1,
    with_allocation: bool = True,
    application_id: str | None = None,
) -> dict[str, str]:
    """验证批处理场景。

    定义：_prepare_execution。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    connection = connect_database(_url(database))
    now = datetime.now(UTC).replace(microsecond=0)
    created = now - timedelta(seconds=2)
    deadline = created + timedelta(seconds=300)
    lease_expires = now + timedelta(seconds=120)
    grant_issued = now - timedelta(seconds=120 if expired_grant else 1)
    grant_expires = grant_issued + timedelta(seconds=60)
    identity = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,environment.environment_id,
               environment.code,model.model_id,model.provider_id,provider.provider_code
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        JOIN environments environment ON environment.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=tenant.tenant_id
        JOIN providers provider ON provider.tenant_id=model.tenant_id
          AND provider.provider_id=model.provider_id
        WHERE tenant.slug='default' AND user.role='admin' AND user.status='active'
          AND environment.code='dev' AND environment.status='active'
          AND model.status='active'
        ORDER BY user.user_id,model.model_id LIMIT 1
        """
    ).fetchone()
    assert identity is not None
    (
        tenant_id,
        user_id,
        environment_id,
        environment_code,
        model_id,
        provider_id,
        provider_code,
    ) = map(str, identity)
    values = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": environment_id,
        "project_id": "prj_prd074_gateway",
        "model_alias_id": "mal_prd074_gateway",
        "virtual_key_id": "vkey_prd074_gateway",
        "file_id": "file_" + "1" * 32,
        "file_extract_id": "fext_" + "c" * 32,
        "batch_job_id": "bjob_" + "2" * 32,
        "batch_task_id": "btsk_" + "3" * 32,
        "batch_attempt_id": "batm_" + "4" * 32,
        "request_id": "req_prd074_internal_gateway",
        "trace_id": "trc_prd074_internal_gateway",
        "lease_token": "lease_prd074_gateway",
        "lease_owner_id": "worker_prd074_gateway",
        "batch_execution_grant_id": "bgrt_" + "5" * 32,
        "budget_rule_id": "budget_prd074_gateway",
        "batch_budget_hold_id": "bbhd_" + "6" * 32,
        "batch_budget_allocation_id": "bbal_" + "7" * 32,
        "environment_code": environment_code,
        "model_id": model_id,
        "provider_id": provider_id,
        "provider_code": provider_code,
    }
    route_plan = _route_plan(
        model_alias_id=values["model_alias_id"],
        environment_id=environment_id,
        virtual_key_id=values["virtual_key_id"],
        application_id=application_id,
        model_id=model_id,
        provider_id=provider_id,
    )
    values["route_plan_hash"] = hashlib.sha256(route_plan.encode("utf-8")).hexdigest()
    values["budget_date"] = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,
          status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'Batch gateway fixture',NULL,?,'active',?,?,?,?)
        """,
        (
            values["project_id"],
            tenant_id,
            "prd074-gateway",
            user_id,
            user_id,
            user_id,
            _db_time(created),
            _db_time(created),
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
            values["model_alias_id"],
            tenant_id,
            "batch-gateway-alias",
            environment_code,
            model_id,
            user_id,
            _db_time(created),
            _db_time(created),
            environment_id,
        ),
    )
    if application_id is not None:
        connection.execute(
            """
            INSERT INTO applications(
              application_id,tenant_id,project_id,application_code,name,status,
              created_at,updated_at
            ) VALUES(?,?,?,'prd074-gateway-app','PRD074 Gateway App','active',?,?)
            """,
            (
                application_id,
                tenant_id,
                values["project_id"],
                _db_time(created),
                _db_time(created),
            ),
        )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'project',?,'PRD-074 batch allocation',100,80,100,
                 'block','active',?,?,?,?)
        """,
        (
            values["budget_rule_id"],
            tenant_id,
            environment_id,
            values["project_id"],
            user_id,
            user_id,
            _db_time(created),
            _db_time(created),
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mh_batch',?,'active',?,?,?,NULL,NULL,?,NULL,NULL,?,?,?)
        """,
        (
            values["virtual_key_id"],
            tenant_id,
            "Batch gateway key",
            "6" * 64,
            _db_time(created),
            _db_time(created),
            environment_code,
            user_id,
            values["project_id"],
            application_id,
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
            values["file_id"],
            tenant_id,
            user_id,
            "req_prd074_file_gateway",
            "trc_prd074_file_gateway",
            b"x" * 64,
            "hmac-sha256:" + "7" * 64,
            _db_time(created),
            _db_time(created),
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
            "sha256:" + "8" * 64,
            "files/prd074-gateway/original.enc",
            _db_time(created),
            _db_time(created),
            tenant_id,
            values["file_id"],
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
            values["file_extract_id"],
            tenant_id,
            values["file_id"],
            "hmac-sha256:" + "9" * 64,
            "files/prd074-gateway/extracted.enc",
            _db_time(created),
            _db_time(created),
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
          reconciliation_pending,deadline_at,source_request_id,source_trace_id,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'batch-operation-templates-v1','normal',
                 300,?,256,1,'queued',16,16,'0','0','0',0,?,?,?,?,?)
        """,
        (
            values["batch_job_id"],
            tenant_id,
            user_id,
            "8" * 64,
            "9" * 64,
            values["project_id"],
            environment_id,
            values["model_alias_id"],
            values["virtual_key_id"],
            route_plan,
            "enc:v1:" + "a" * 40,
            "summarize",
            "enc:v1:" + "b" * 40,
            max_attempts,
            _db_time(deadline),
            "req_prd074_job_gateway",
            "trc_prd074_job_gateway",
            _db_time(created),
            _db_time(created),
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_budget_holds(
          batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
          scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
          reconciliation_cost_cny,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'project',?,?,'1.000000','1.000000','0','active',?,?)
        """,
        (
            values["batch_budget_hold_id"],
            tenant_id,
            values["batch_job_id"],
            values["budget_rule_id"],
            environment_id,
            values["project_id"],
            values["budget_date"],
            _db_time(created),
            _db_time(created),
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_tasks(
          batch_task_id,tenant_id,batch_job_id,file_id,task_index,priority_rank,
          status,attempt_count,next_attempt_at,lease_generation,result_available,
          created_at,updated_at
        ) VALUES(?,?,?,?,0,2,'queued',?,?,0,0,?,?)
        """,
        (
            values["batch_task_id"],
            tenant_id,
            values["batch_job_id"],
            values["file_id"],
            attempt_count,
            _db_time(created),
            _db_time(created),
            _db_time(created),
        ),
    )
    owner = BatchEnvelopeOwner(
        tenant_id=tenant_id,
        batch_job_id=values["batch_job_id"],
        batch_task_id=values["batch_task_id"],
        batch_attempt_id=values["batch_attempt_id"],
        request_id=values["request_id"],
        template_version=BATCH_OPERATION_TEMPLATE_VERSION,
    )
    plaintext = json.dumps(
        {
            "schema_version": 1,
            "messages": [
                {"role": "system", "content": "fixed batch template"},
                {"role": "user", "content": "sensitive file content"},
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = encrypt_batch_payload(plaintext, secret=SECRET, owner=owner)
    fingerprint = batch_payload_hmac(plaintext, secret=SECRET, owner=owner)
    if corrupt_hmac:
        fingerprint = "0" * 64
    connection.execute(
        """
        INSERT INTO batch_attempts(
          batch_attempt_id,tenant_id,batch_job_id,batch_task_id,attempt_no,
          request_id,trace_id,status,execution_outcome,retry_safe,
          request_payload_ciphertext,request_payload_key_version,request_payload_hmac,
          route_plan_json,result_state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'prepared','not_invoked',0,?,1,?,?,'none',?,?)
        """,
        (
            values["batch_attempt_id"],
            tenant_id,
            values["batch_job_id"],
            values["batch_task_id"],
            attempt_no,
            values["request_id"],
            values["trace_id"],
            ciphertext,
            fingerprint,
            route_plan,
            _db_time(created),
            _db_time(created),
        ),
    )
    if with_allocation:
        connection.execute(
            """
            INSERT INTO batch_budget_allocations(
              batch_budget_allocation_id,tenant_id,batch_job_id,batch_task_id,
              batch_attempt_id,batch_budget_hold_id,budget_rule_id,budget_date,
              reserved_cost_cny,route_plan_hash,execution_outcome,charge_state,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'not_invoked','none','active',?,?)
            """,
            (
                values["batch_budget_allocation_id"],
                tenant_id,
                values["batch_job_id"],
                values["batch_task_id"],
                values["batch_attempt_id"],
                values["batch_budget_hold_id"],
                values["budget_rule_id"],
                values["budget_date"],
                "0.007840",
                values["route_plan_hash"],
                _db_time(created),
                _db_time(created),
            ),
        )
    connection.execute(
        """
        UPDATE batch_tasks
        SET status='leased',lease_token=?,lease_owner_id=?,lease_generation=1,
            lease_expires_at=?,current_attempt_id=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=? AND status='queued'
        """,
        (
            values["lease_token"],
            values["lease_owner_id"],
            _db_time(lease_expires),
            values["batch_attempt_id"],
            _db_time(created),
            tenant_id,
            values["batch_task_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_execution_grants(
          batch_execution_grant_id,tenant_id,batch_job_id,batch_task_id,
          batch_attempt_id,request_id,trace_id,virtual_key_id,model_alias_id,
          lease_token,lease_generation,grant_generation,grant_hash,status,
          issued_at,expires_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,1,1,?,'issued',?,?)
        """,
        (
            values["batch_execution_grant_id"],
            tenant_id,
            values["batch_job_id"],
            values["batch_task_id"],
            values["batch_attempt_id"],
            values["request_id"],
            values["trace_id"],
            values["virtual_key_id"],
            values["model_alias_id"],
            values["lease_token"],
            hashlib.sha256(GRANT.encode("ascii")).hexdigest(),
            _db_time(grant_issued),
            _db_time(grant_expires),
        ),
    )
    connection.commit()
    connection.close()
    return values


def _database_copy(template: Path, destination: Path) -> Path:
    """验证批处理场景。

    定义：_database_copy。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    shutil.copy2(template, destination)
    return destination


def _add_released_historical_allocation(
    database: Path,
    values: dict[str, str],
) -> tuple[str, str]:
    """验证批处理场景。

    定义：_add_released_historical_allocation。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    old_date = (
        datetime.fromisoformat(values["budget_date"]).date() - timedelta(days=1)
    ).isoformat()
    old_hold_id = "bbhd_" + "a" * 32
    old_allocation_id = "bbal_" + "b" * 32
    recorded_at = _db_time(datetime.now(UTC) - timedelta(days=1))
    connection = connect_database(_url(database))
    connection.execute(
        """
        INSERT INTO batch_budget_holds(
          batch_budget_hold_id,tenant_id,batch_job_id,budget_rule_id,environment_id,
          scope_type,scope_id,budget_date,reserved_cost_cny,remaining_cost_cny,
          reconciliation_cost_cny,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'project',?,?,'1.000000','0','0','released',?,?)
        """,
        (
            old_hold_id,
            values["tenant_id"],
            values["batch_job_id"],
            values["budget_rule_id"],
            values["environment_id"],
            values["project_id"],
            old_date,
            recorded_at,
            recorded_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO batch_budget_allocations(
          batch_budget_allocation_id,tenant_id,batch_job_id,batch_task_id,
          batch_attempt_id,batch_budget_hold_id,budget_rule_id,budget_date,
          reserved_cost_cny,route_plan_hash,execution_outcome,charge_state,status,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'known_failed','none','released',?,?)
        """,
        (
            old_allocation_id,
            values["tenant_id"],
            values["batch_job_id"],
            values["batch_task_id"],
            values["batch_attempt_id"],
            old_hold_id,
            values["budget_rule_id"],
            old_date,
            "0.007840",
            values["route_plan_hash"],
            recorded_at,
            recorded_at,
        ),
    )
    connection.commit()
    connection.close()
    return old_date, old_allocation_id


def _states(database: Path, values: dict[str, str]) -> tuple[str, str, str]:
    """验证批处理场景。

    定义：_states。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    connection = connect_database(_url(database))
    states = (
        str(
            connection.execute(
                "SELECT status FROM batch_execution_grants WHERE batch_execution_grant_id=?",
                (values["batch_execution_grant_id"],),
            ).fetchone()[0]
        ),
        str(
            connection.execute(
                "SELECT status FROM batch_tasks WHERE batch_task_id=?",
                (values["batch_task_id"],),
            ).fetchone()[0]
        ),
        str(
            connection.execute(
                "SELECT status FROM batch_attempts WHERE batch_attempt_id=?",
                (values["batch_attempt_id"],),
            ).fetchone()[0]
        ),
    )
    connection.close()
    return states


def _spooled_usage_record(
    config: AppConfig,
    execution: InternalBatchExecution,
    values: dict[str, str],
) -> dict[str, object]:
    """验证批处理场景。

    定义：_spooled_usage_record。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    batch = execution.invocation.batch
    assert batch is not None
    assert batch.token_estimate == TokenEstimate(
        input_tokens=272,
        output_tokens=256,
        truncated=False,
    )
    context = RequestLogContext(
        tenant_id=batch.tenant_id,
        request_id=batch.request_id,
        trace_id=batch.trace_id,
        virtual_key_id=values["virtual_key_id"],
        environment_id=values["environment_id"],
        environment_code=values["environment_code"],
        project_id=values["project_id"],
        provider_id=values["provider_id"],
        model_id=values["model_id"],
        endpoint="chat/completions",
        request_model="batch-gateway-alias",
        resolved_model=values["model_id"],
        provider_code=values["provider_code"],
        batch_job_id=batch.batch_job_id,
        batch_task_id=batch.batch_task_id,
        batch_attempt_id=batch.batch_attempt_id,
        batch_route_plan_hash=batch.route_plan_hash,
    )
    claim_gateway_request(config, context)
    scan = prepare_output_safety_scan(
        config,
        tenant_id=batch.tenant_id,
        request_id=batch.request_id,
        trace_id=batch.trace_id,
        scan_mode="non_stream_full",
    )
    assert scan.scan_status == "not_configured"
    context = replace(
        context,
        output_safety_scan_id=scan.output_safety_scan_id,
        output_safety_scan_status="not_configured",
        output_safety_decision="none",
        billing_disposition="not_required",
    )
    record_request_log(
        config,
        context,
        status="success",
        error_code=None,
        latency_ms=12,
    )
    intent = _prepare_usage_ledger_intent(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "usage_source": "upstream",
            "raw_usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
    )
    snapshot_id = "prs_" + "8" * 32
    evidence = PriceSnapshotEvidence(
        price_snapshot_id=snapshot_id,
        model_id=values["model_id"],
        provider_id=values["provider_id"],
        provider_code=values["provider_code"],
        currency="CNY",
        input_price_per_1k="0.010000",
        output_price_per_1k="0.020000",
        price_schema_version="prd074-batch-route-v1",
        source="model_catalog",
    )
    intent.captured_estimate = CostEstimate(
        amount=Decimal("0.002000"),
        currency="CNY",
        price_schema_version="prd074-batch-route-v1",
        cost_cny=Decimal("0.002000"),
        cost_usd=Decimal("0"),
        price_snapshot_id=snapshot_id,
        price_snapshot_evidence=evidence,
    )
    record = _serialize_usage_spool_record(context, intent)
    assert record["schema_version"] == 6
    return record


def test_valid_grant_is_consumed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_valid_grant_is_consumed_once。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "valid.sqlite3")
    values = _prepare_execution(database)
    config = _config(monkeypatch, database)

    execution = _consume_execution_grant(
        config,
        grant=GRANT,
        request_id=values["request_id"],
        trace_id=values["trace_id"],
    )
    assert execution.invocation.source == "batch"
    assert execution.invocation.payload["stream"] is False
    assert _states(database, values) == ("consumed", "calling", "calling")

    with pytest.raises(ApiError) as replay:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=values["trace_id"],
        )
    assert replay.value.code == "batch.execution_invalid"


def test_cross_day_grant_coverage_and_usage_settlement_use_latest_allocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_cross_day_grant_coverage_and_usage_settlement_use_latest_allocations。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "cross-day-latest.sqlite3")
    values = _prepare_execution(database)
    old_date, old_allocation_id = _add_released_historical_allocation(database, values)
    config = _config(monkeypatch, database)

    execution = _consume_execution_grant(
        config,
        grant=GRANT,
        request_id=values["request_id"],
        trace_id=values["trace_id"],
    )
    batch = execution.invocation.batch
    assert batch is not None
    assert [
        (item.batch_budget_allocation_id, item.budget_date)
        for item in batch.allocations
    ] == [(values["batch_budget_allocation_id"], values["budget_date"])]

    route = GatewayRoute(
        tenant_id=values["tenant_id"],
        model_id=values["model_id"],
        model_alias="batch-gateway-alias",
        upstream_model_name="batch-gateway-model",
        capabilities={},
        provider_id=values["provider_id"],
        provider_code=values["provider_code"],
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="Batch gateway provider",
        provider_base_url="https://provider.invalid/v1",
        environment=values["environment_code"],
        environment_id=values["environment_id"],
        model_alias_id=values["model_alias_id"],
    )

    def reject_body_estimation(*_args: object, **_kwargs: object) -> TokenEstimate:
        """验证批处理场景。

        定义：reject_body_estimation。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise AssertionError("Batch budget must not estimate decrypted request content")

    monkeypatch.setattr(budget_service, "estimate_request_tokens", reject_body_estimation)
    guard = enforce_budget_guard(
        config,
        execution.invocation.auth_context,
        route,
        execution.invocation.payload,
        endpoint="chat/completions",
        request_kind="chat",
        request_id=batch.request_id,
        trace_id=batch.trace_id,
        routing_token_estimate=batch.token_estimate,
        batch_context=batch,
        route_plan=GatewayRoutePlan(initial_route=route, initial_role="default"),
    )
    assert guard is not None
    assert guard.initial_estimated_cost_cny == Decimal("0.007840")

    record = _spooled_usage_record(config, execution, values)
    # PRD-075 扩展版本白名单后，真实 Batch v6 必须仍可由文件队列原子存储并幂等重放。
    spool = UsageSpool(config)
    assert spool.store(record) is True
    assert spool.store(record) is False
    _persist_encrypted_result(
        config,
        execution,
        assistant="synthetic cross-day result",
        input_tokens=100,
        output_tokens=50,
    )
    assert replay_usage_spool_record(config, record).created is True

    connection = connect_database(_url(database))
    allocations = connection.execute(
        """
        SELECT batch_budget_allocation_id,budget_date,execution_outcome,charge_state,status
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_attempt_id=?
        ORDER BY budget_date
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchall()
    connection.close()
    assert [tuple(row) for row in allocations] == [
        (old_allocation_id, old_date, "known_failed", "none", "released"),
        (
            values["batch_budget_allocation_id"],
            values["budget_date"],
            "succeeded",
            "posted",
            "posted",
        ),
    ]


def test_cross_day_failure_after_posted_usage_ignores_historical_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_cross_day_failure_after_posted_usage_ignores_historical_release。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "cross-day-failure.sqlite3")
    values = _prepare_execution(database)
    old_date, old_allocation_id = _add_released_historical_allocation(database, values)
    config = _config(monkeypatch, database)
    execution = _consume_execution_grant(
        config,
        grant=GRANT,
        request_id=values["request_id"],
        trace_id=values["trace_id"],
    )

    connection = connect_database(_url(database))
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET execution_outcome='succeeded',charge_state='posted',status='posted'
        WHERE tenant_id=? AND batch_budget_allocation_id=?
        """,
        (values["tenant_id"], values["batch_budget_allocation_id"]),
    )
    connection.execute(
        """
        UPDATE batch_attempts
        SET status='provider_succeeded',execution_outcome='succeeded',retry_safe=0
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    )
    connection.commit()
    connection.close()

    assert _record_execution_failure(
        config,
        execution,
        "batch.worker_unavailable",
    ) == "batch.worker_unavailable"
    connection = connect_database(_url(database))
    attempt = connection.execute(
        """
        SELECT status,execution_outcome,retry_safe
        FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchone()
    allocations = connection.execute(
        """
        SELECT batch_budget_allocation_id,budget_date,execution_outcome,charge_state,status
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_attempt_id=? ORDER BY budget_date
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchall()
    connection.close()
    assert tuple(attempt) == ("known_failed", "succeeded", 0)
    assert [tuple(row) for row in allocations] == [
        (old_allocation_id, old_date, "known_failed", "none", "released"),
        (
            values["batch_budget_allocation_id"],
            values["budget_date"],
            "succeeded",
            "posted",
            "posted",
        ),
    ]


@pytest.mark.parametrize("expired_grant", [False, True])
def test_grant_owner_and_expiry_fail_closed_without_state_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
    expired_grant: bool,
) -> None:
    """验证批处理场景。

    定义：test_grant_owner_and_expiry_fail_closed_without_state_change。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(
        migrated_template,
        tmp_path / f"invalid-{expired_grant}.sqlite3",
    )
    values = _prepare_execution(database, expired_grant=expired_grant)
    config = _config(monkeypatch, database)
    trace_id = values["trace_id"] if expired_grant else "trc_wrong_owner"

    with pytest.raises(ApiError) as captured:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=trace_id,
        )
    assert captured.value.code == "batch.execution_invalid"
    assert _states(database, values) == ("issued", "leased", "prepared")


def test_grant_rejects_damaged_complete_rbac_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_grant_rejects_damaged_complete_rbac_matrix。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "rbac-integrity.sqlite3")
    values = _prepare_execution(database)
    connection = connect_database(_url(database))
    connection.execute("DROP TRIGGER trg_roles_matrix_update_prd059")
    connection.execute(
        """
        UPDATE roles SET matrix_sha256=?
        WHERE tenant_id=? AND code='admin'
        """,
        ("0" * 64, values["tenant_id"]),
    )
    connection.commit()
    connection.close()
    config = _config(monkeypatch, database)

    with pytest.raises(ApiError) as captured:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=values["trace_id"],
        )
    assert captured.value.code == "batch.execution_invalid"
    assert _states(database, values) == ("issued", "leased", "prepared")


def test_grant_rejects_virtual_key_application_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_grant_rejects_virtual_key_application_rebinding。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "application-rebind.sqlite3")
    values = _prepare_execution(database, application_id="app_prd074_gateway")
    connection = connect_database(_url(database))
    connection.execute(
        """
        UPDATE virtual_keys SET application_id=NULL
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (values["tenant_id"], values["virtual_key_id"]),
    )
    connection.commit()
    connection.close()
    config = _config(monkeypatch, database)

    with pytest.raises(ApiError) as captured:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=values["trace_id"],
        )
    assert captured.value.code == "batch.execution_invalid"
    assert _states(database, values) == ("issued", "leased", "prepared")


def test_corrupt_payload_hmac_rolls_back_all_grant_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_corrupt_payload_hmac_rolls_back_all_grant_state。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "corrupt.sqlite3")
    values = _prepare_execution(database, corrupt_hmac=True)
    config = _config(monkeypatch, database)

    with pytest.raises(ApiError) as captured:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=values["trace_id"],
        )
    assert captured.value.code == "batch.worker_unavailable"
    assert _states(database, values) == ("issued", "leased", "prepared")


def test_grant_consumption_honors_job_max_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_grant_consumption_honors_job_max_attempts。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "max-attempts.sqlite3")
    values = _prepare_execution(database, max_attempts=1, attempt_count=1)
    config = _config(monkeypatch, database)

    with pytest.raises(ApiError) as captured:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=values["trace_id"],
        )
    assert captured.value.code == "batch.execution_invalid"
    assert _states(database, values) == ("issued", "leased", "prepared")


def test_grant_consumption_requires_next_attempt_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_grant_consumption_requires_next_attempt_sequence。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "attempt-sequence.sqlite3")
    values = _prepare_execution(
        database,
        max_attempts=2,
        attempt_count=1,
        attempt_no=1,
    )
    config = _config(monkeypatch, database)

    with pytest.raises(ApiError) as captured:
        _consume_execution_grant(
            config,
            grant=GRANT,
            request_id=values["request_id"],
            trace_id=values["trace_id"],
        )
    assert captured.value.code == "batch.execution_invalid"
    assert _states(database, values) == ("issued", "leased", "prepared")


def test_spooled_usage_replay_posts_batch_allocation_after_result_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_spooled_usage_replay_posts_batch_allocation_after_result_persistence。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "usage-replay.sqlite3")
    values = _prepare_execution(database)
    config = _config(monkeypatch, database)
    execution = _consume_execution_grant(
        config,
        grant=GRANT,
        request_id=values["request_id"],
        trace_id=values["trace_id"],
    )
    record = _spooled_usage_record(config, execution, values)

    _persist_encrypted_result(
        config,
        execution,
        assistant="sensitive batch result",
        input_tokens=100,
        output_tokens=50,
    )
    connection = connect_database(_url(database))
    before = connection.execute(
        """
        SELECT status,execution_outcome,result_state,input_tokens,output_tokens,cost_cny
        FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchone()
    allocation_before = connection.execute(
        """
        SELECT execution_outcome,charge_state,status
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_budget_allocation_id=?
        """,
        (values["tenant_id"], values["batch_budget_allocation_id"]),
    ).fetchone()
    assert tuple(before) == ("provider_succeeded", "succeeded", "available", 100, 50, None)
    assert tuple(allocation_before) == ("not_invoked", "none", "active")
    connection.close()

    committed = replay_usage_spool_record(config, record)
    assert committed.created is True
    assert committed.amount == "0.002000"
    replayed = replay_usage_spool_record(config, record)
    assert replayed.created is False
    assert replayed.ledger_entry_id == committed.ledger_entry_id

    connection = connect_database(_url(database))
    after = connection.execute(
        """
        SELECT status,execution_outcome,result_state,input_tokens,output_tokens,cost_cny
        FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchone()
    allocation_after = connection.execute(
        """
        SELECT execution_outcome,charge_state,status
        FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_budget_allocation_id=?
        """,
        (values["tenant_id"], values["batch_budget_allocation_id"]),
    ).fetchone()
    ordinary_reservations = connection.execute(
        """
        SELECT COUNT(*) FROM budget_reservations
        WHERE tenant_id=? AND request_id=?
        """,
        (values["tenant_id"], values["request_id"]),
    ).fetchone()[0]
    usage_rows = connection.execute(
        """
        SELECT COUNT(*) FROM usage_ledger
        WHERE tenant_id=? AND request_id=?
        """,
        (values["tenant_id"], values["request_id"]),
    ).fetchone()[0]
    connection.close()
    assert tuple(after) == (
        "provider_succeeded",
        "succeeded",
        "available",
        100,
        50,
        "0.002000",
    )
    assert tuple(allocation_after) == ("succeeded", "posted", "posted")
    assert ordinary_reservations == 0
    assert usage_rows == 1


def test_spooled_usage_replay_recovers_attempt_without_allocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_template: Path,
) -> None:
    """验证批处理场景。

    定义：test_spooled_usage_replay_recovers_attempt_without_allocations。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    database = _database_copy(migrated_template, tmp_path / "usage-no-allocation.sqlite3")
    values = _prepare_execution(database, with_allocation=False)
    config = _config(monkeypatch, database)
    execution = _consume_execution_grant(
        config,
        grant=GRANT,
        request_id=values["request_id"],
        trace_id=values["trace_id"],
    )
    batch = execution.invocation.batch
    assert batch is not None and batch.allocations == ()
    record = _spooled_usage_record(config, execution, values)
    _persist_encrypted_result(
        config,
        execution,
        assistant="sensitive batch result",
        input_tokens=100,
        output_tokens=50,
    )

    committed = replay_usage_spool_record(config, record)
    assert committed.created is True
    replayed = replay_usage_spool_record(config, record)
    assert replayed.created is False

    connection = connect_database(_url(database))
    attempt = connection.execute(
        """
        SELECT status,execution_outcome,result_state,cost_cny
        FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchone()
    allocation_count = connection.execute(
        """
        SELECT COUNT(*) FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (values["tenant_id"], values["batch_attempt_id"]),
    ).fetchone()[0]
    ordinary_reservations = connection.execute(
        """
        SELECT COUNT(*) FROM budget_reservations
        WHERE tenant_id=? AND request_id=?
        """,
        (values["tenant_id"], values["request_id"]),
    ).fetchone()[0]
    connection.close()
    assert tuple(attempt) == ("provider_succeeded", "succeeded", "available", "0.002000")
    assert allocation_count == 0
    assert ordinary_reservations == 0
