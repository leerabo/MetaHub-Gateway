"""PRD-064 Adapter 合同报告校验、不可变持久化与租户级只读查询服务。

内部上报只接受已经脱敏的版本化报告。服务会把正文 tenant、HTTP 链路、当前进程目录、租户
active 目录和动态 exact-set 矩阵交叉核对，再在单事务中写入 run、cases 与失败事件 source。
任何 SQL 都先以 tenant_id 收窄，跨租户详情统一返回 404。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping

from metahub_control.auth import AuthContext
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    ProviderAdapterSpec,
    catalog_identity,
    provider_adapter_specs,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import (
    AdapterContractCaseInfo,
    AdapterContractCaseReport,
    AdapterContractRunDetailResponse,
    AdapterContractRunInfo,
    AdapterContractRunIngestResponse,
    AdapterContractRunListResponse,
    AdapterContractRunReport,
)


# 所有枚举与 runner 冻结合同保持一处显式白名单。失败码只能说明断言类别，不能拼接
# Provider、URL、响应正文或底层异常。
# 这些固定值共同限定报告规模、身份格式和失败分类，任何变更都必须同步 runner 合同。
REPORT_SCHEMA_VERSION = "adapter-contract-report-v1"
MAX_CASES = 2048
MAX_LIST_LIMIT = 100
ALLOWED_FAILURE_CODES = frozenset(
    {
        "adapter.contract_fixture_missing",
        "adapter.contract_fixture_unexpected",
        "adapter.contract_fixture_invalid",
        "adapter.contract_coverage_mismatch",
        "adapter.contract_request_failed",
        "adapter.contract_non_stream_failed",
        "adapter.contract_stream_failed",
        "adapter.contract_error_mapping_failed",
        "adapter.contract_usage_failed",
        "adapter.contract_probe_failed",
    }
)
_CASE_FAILURE_CODE = {
    "request": "adapter.contract_request_failed",
    "non_stream": "adapter.contract_non_stream_failed",
    "stream": "adapter.contract_stream_failed",
    "error": "adapter.contract_error_mapping_failed",
    "usage": "adapter.contract_usage_failed",
}
_FIXED_CASES = (
    ("non_stream", "success"),
    ("stream", "success"),
    ("error", "auth_401"),
    ("error", "rate_limit_429"),
    ("error", "server_5xx"),
    ("error", "timeout"),
    ("usage", "standard"),
    ("usage", "missing"),
    ("usage", "invalid"),
)
_RUN_ID = re.compile(r"acr_[0-9a-f]{32}\Z")
_CASE_ID = re.compile(r"acc_[0-9a-f]{32}\Z")
_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_LINK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_RUNNER_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_CURSOR_RUN_ID = _RUN_ID


@dataclass(frozen=True, slots=True)
class _ExpectedCase:
    """由 Shared spec 动态推导的单条合同身份，不依赖 Gateway 包导入。"""

    case_key: str
    spec: ProviderAdapterSpec
    case_kind: str
    variant: str
    auth_type: str | None


def ingest_adapter_contract_run(
    config: AppConfig,
    tenant_id: str,
    report: AdapterContractRunReport,
    *,
    request_id: str,
    trace_id: str,
) -> AdapterContractRunIngestResponse:
    """校验并原子保存一次合同运行。

    参数：path tenant、严格报告以及中间件确定的 request/trace；返回首次写入或幂等回读。
    关键约束：path tenant 是唯一可信租户，失败事件先通过 Shared 严格构造器，再与 run/cases
    同事务写 source outbox；相同 request 只允许重放完全相同的 canonical 报告。
    """

    normalized_tenant = _normalize_tenant_id(tenant_id)
    report_hash = _canonical_report_hash(report)
    expected = _validate_report(
        report,
        tenant_id=normalized_tenant,
        request_id=request_id,
        trace_id=trace_id,
    )
    # 事件构造必须早于数据库事务提交；若共享 payload/envelope 合同漂移，本次 run 整体不落库。
    failed_event = _contract_failed_event(report) if report.status == "failed" else None
    connection = _connect(config)
    created = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_tenant_catalog(connection, normalized_tenant, report)
        existing = connection.execute(
            """
            SELECT * FROM adapter_contract_runs
            WHERE tenant_id=? AND source_request_id=?
            """,
            (normalized_tenant, request_id),
        ).fetchone()
        if existing is not None:
            if existing["report_hash"] != report_hash:
                # 相同幂等键不能描述另一报告；不比较或回显任何正文差异。
                raise ApiError("adapter.contract_run_conflict")
            connection.commit()
            return AdapterContractRunIngestResponse(
                adapter_contract_run=_run_info(existing),
                replayed=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        # run_id 是全局稳定身份；即使碰撞发生在其他租户，也不能泄露 owner 或复用该 ID。
        if connection.execute(
            "SELECT 1 FROM adapter_contract_runs WHERE adapter_contract_run_id=?",
            (report.run_id,),
        ).fetchone():
            # 全局 run 身份发生碰撞时统一报告冲突，不查询或回显已有记录所属租户。
            raise ApiError("adapter.contract_run_conflict")

        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO adapter_contract_runs(
              adapter_contract_run_id,tenant_id,source_request_id,source_trace_id,
              schema_version,catalog_version,catalog_hash,runner_version,suite_kind,mode,
              source_revision_hash,status,coverage_complete,total_cases,passed_cases,
              failed_cases,failure_codes_json,report_hash,started_at,completed_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report.run_id,
                normalized_tenant,
                request_id,
                trace_id,
                report.schema_version,
                report.catalog_version,
                report.catalog_hash,
                report.runner_version,
                report.suite_kind,
                report.mode,
                report.source_revision_hash,
                report.status,
                1 if report.coverage_complete else 0,
                report.total_cases,
                report.passed_cases,
                report.failed_cases,
                _json_dumps(report.failure_codes),
                report_hash,
                report.started_at,
                report.completed_at,
                now,
                now,
            ),
        )
        for case in report.cases:
            # expected 映射已经完成 exact identity 验证；数据库只写安全维度和不可逆证据。
            definition = expected[case.case_key]
            connection.execute(
                """
                INSERT INTO adapter_contract_cases(
                  adapter_contract_case_id,tenant_id,adapter_contract_run_id,contract_case_key,
                  spec_id,provider_type,adapter_name,adapter_version,case_kind,variant,auth_type,
                  status,failure_code,duration_ms,evidence_hash,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    case.adapter_contract_case_id,
                    normalized_tenant,
                    report.run_id,
                    case.case_key,
                    definition.spec.spec_id,
                    definition.spec.provider_type,
                    definition.spec.adapter_name,
                    definition.spec.adapter_version,
                    case.case_kind,
                    case.variant,
                    case.auth_type,
                    case.status,
                    case.failure_code,
                    case.duration_ms,
                    case.evidence_hash,
                    now,
                    now,
                ),
            )
        if failed_event is not None:
            # source 只保存稳定事件身份和调度状态；payload 始终由不可变 run 重建，避免双份漂移。
            connection.execute(
                """
                INSERT INTO adapter_contract_event_outbox(
                  event_id,tenant_id,adapter_contract_run_id,dispatch_status,dispatch_attempts,
                  next_attempt_at,dispatched_at,claim_token,lease_owner_id,lease_expires_at,
                  last_error_code,created_at,updated_at
                ) VALUES(?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
                """,
                (failed_event.event_id, normalized_tenant, report.run_id, now, now, now),
            )
        row = connection.execute(
            """
            SELECT * FROM adapter_contract_runs
            WHERE tenant_id=? AND adapter_contract_run_id=?
            """,
            (normalized_tenant, report.run_id),
        ).fetchone()
        if row is None:
            # 同一事务写后不可回读说明结构或触发器异常，禁止返回未经证明的成功。
            raise sqlite3.IntegrityError("adapter contract run write missing")
        connection.commit()
        created = True
    # 领域校验错误保留原错误码，但必须回滚同一事务中的 run、cases 与 source。
    except ApiError:
        connection.rollback()
        raise
    # BEGIN IMMEDIATE 已把冲突查询与写入置于单写事务；前置检查后的完整性异常说明
    # case/source 约束或数据库触发器失败，必须整体回滚并按存储不可用处理。
    except sqlite3.IntegrityError:
        connection.rollback()
        # 不能把 Outbox 入队失败伪装成业务冲突，否则调用方会停止重试且丢失失败事件。
        raise ApiError("adapter.contract_storage_unavailable") from None
    # 其他数据库异常属于暂时存储不可用，不能把 SQL 或数据库路径暴露给调用方。
    except sqlite3.Error:
        connection.rollback()
        # 回滚后切断底层异常链，仅返回统一的存储错误码和中间件链路标识。
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()
    if not created:
        # 该分支只用于静态证明 row 已赋值，正常运行不会到达。
        raise ApiError("adapter.contract_storage_unavailable")
    return AdapterContractRunIngestResponse(
        adapter_contract_run=_run_info(row),
        replayed=False,
        request_id=request_id,
        trace_id=trace_id,
    )


def list_adapter_contract_runs(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    suite_kind: str | None,
    mode: str | None,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> AdapterContractRunListResponse:
    """按当前认证租户和稳定倒序 cursor 返回运行摘要。"""

    normalized_status = _enum_filter(status, {"passed", "failed"})
    normalized_suite = _enum_filter(suite_kind, {"merge_gate", "probe"})
    normalized_mode = _enum_filter(mode, {"mock", "gateway_probe"})
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        # 服务层再次限制分页大小，防止绕过 HTTP 路由的内部调用形成无界查询。
        raise ApiError("adapter.contract_report_invalid")
    cursor_value = _decode_cursor(cursor) if cursor is not None else None
    clauses = ["tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    for column, value in (
        ("status", normalized_status),
        ("suite_kind", normalized_suite),
        ("mode", normalized_mode),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    if cursor_value is not None:
        completed_at, run_id = cursor_value
        clauses.append("(completed_at<? OR (completed_at=? AND adapter_contract_run_id<?))")
        params.extend((completed_at, completed_at, run_id))
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT * FROM adapter_contract_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY completed_at DESC, adapter_contract_run_id DESC
            LIMIT ?
            """,
            (*params, limit + 1),
        ).fetchall()
    # 列表查询失败只暴露固定存储错误，不返回 SQL、租户参数或数据库路径。
    except sqlite3.Error:
        # 切断 SQLite 异常文本，统一交由 API 错误中间件附加 request_id。
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()
    visible = rows[:limit]
    next_cursor = None
    if len(rows) > limit and visible:
        last = visible[-1]
        next_cursor = _encode_cursor(last["completed_at"], last["adapter_contract_run_id"])
    return AdapterContractRunListResponse(
        adapter_contract_runs=[_run_info(row) for row in visible],
        limit=limit,
        next_cursor=next_cursor,
        request_id=request_id,
        trace_id=trace_id,
    )


def get_adapter_contract_run(
    config: AppConfig,
    context: AuthContext,
    adapter_contract_run_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> AdapterContractRunDetailResponse:
    """读取当前租户单次运行；不存在与跨租户 ID 使用相同 404。"""

    if type(adapter_contract_run_id) is not str or not _RUN_ID.fullmatch(adapter_contract_run_id):
        # 非法 ID 与跨租户不存在使用同一错误，避免形成全局 run 身份枚举侧信道。
        raise ApiError("adapter.contract_run_not_found")
    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT * FROM adapter_contract_runs
            WHERE tenant_id=? AND adapter_contract_run_id=?
            """,
            (context.user.tenant_id, adapter_contract_run_id),
        ).fetchone()
        if row is None:
            # tenant 是第一条件，跨租户全局主键不会形成存在性侧信道。
            raise ApiError("adapter.contract_run_not_found")
        case_rows = connection.execute(
            """
            SELECT * FROM adapter_contract_cases
            WHERE tenant_id=? AND adapter_contract_run_id=?
            ORDER BY provider_type,case_kind,variant,contract_case_key
            """,
            (context.user.tenant_id, adapter_contract_run_id),
        ).fetchall()
    # 业务层 404 必须原样保留，不能被宽泛数据库错误转换覆盖。
    except ApiError:
        raise
    # 查询数据库失败时不泄露租户条件或 SQL，只转换为统一存储错误。
    except sqlite3.Error:
        # 固定领域码由统一中间件补齐 request_id，底层异常文本不进入响应。
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()
    if len(case_rows) != int(row["total_cases"]):
        # 不可变父子计数漂移意味着数据库证据损坏，不能返回看似完整的页面结果。
        raise ApiError("adapter.contract_storage_unavailable")
    return AdapterContractRunDetailResponse(
        adapter_contract_run=_run_info(row),
        cases=[_case_info(case_row) for case_row in case_rows],
        request_id=request_id,
        trace_id=trace_id,
    )


def _validate_report(
    report: AdapterContractRunReport,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> Mapping[str, _ExpectedCase]:
    """执行报告字段、计数、矩阵、身份、时间和隐私所需的完整语义校验。"""

    if (
        report.schema_version != REPORT_SCHEMA_VERSION
        or report.tenant_id != tenant_id
        or report.request_id != request_id
        or report.trace_id != trace_id
        or not _RUN_ID.fullmatch(report.run_id)
        or not _SAFE_LINK_ID.fullmatch(report.request_id)
        or not _SAFE_LINK_ID.fullmatch(report.trace_id)
        or not _SAFE_VERSION.fullmatch(report.catalog_version)
        or not _LOWER_HEX_64.fullmatch(report.catalog_hash)
        or not 5 <= len(report.runner_version) <= 32
        or not _RUNNER_VERSION.fullmatch(report.runner_version)
        or not _LOWER_HEX_64.fullmatch(report.source_revision_hash)
    ):
        # 租户、链路或版本身份任一不精确匹配都拒绝，正文身份不能覆盖 path 与中间件事实。
        raise ApiError("adapter.contract_report_invalid")
    started = _parse_time(report.started_at)
    completed = _parse_time(report.completed_at)
    if completed < started or completed > _utcnow() + timedelta(seconds=1):
        # 未完成或来自未来的报告不能进入不可变历史，也不能触发 Webhook。
        raise ApiError("adapter.contract_report_invalid")
    if (
        type(report.total_cases) is not int
        or not 1 <= report.total_cases <= MAX_CASES
        or report.total_cases != len(report.cases)
        or report.passed_cases + report.failed_cases != report.total_cases
        or report.passed_cases < 0
        or report.failed_cases < 0
        or (report.status == "passed") != (report.failed_cases == 0)
    ):
        # 计数和状态必须可由 cases 完整复算，防止伪造通过结论或截断失败证据。
        raise ApiError("adapter.contract_report_invalid")
    if report.failure_codes != sorted(set(report.failure_codes)) or any(
        code not in ALLOWED_FAILURE_CODES for code in report.failure_codes
    ):
        # 汇总失败码必须有序、唯一且来自白名单，禁止自由文本或厂商响应进入存储。
        raise ApiError("adapter.contract_report_invalid")
    if report.suite_kind == "merge_gate":
        if report.mode != "mock" or report.coverage_complete is not True:
            # 合并门禁只能接受完整 mock 矩阵，禁止真实探测子集冒充发布证据。
            raise ApiError("adapter.contract_report_invalid")
    elif report.mode != "gateway_probe" or report.coverage_complete is not False:
        # probe 只能声明显式 Gateway subset，永远不能冒充完整合并门禁。
        raise ApiError("adapter.contract_report_invalid")

    expected = _expected_cases()
    actual_keys = [case.case_key for case in report.cases]
    if len(actual_keys) != len(set(actual_keys)):
        # 重复 case 会扭曲计数并覆盖证据身份，因此在建立映射前整体拒绝。
        raise ApiError("adapter.contract_report_invalid")
    if report.suite_kind == "merge_gate":
        if set(actual_keys) != set(expected):
            # merge gate 必须与当前 Shared spec 动态 exact-set 完全相等，不能缺项或多项。
            raise ApiError("adapter.contract_report_invalid")
    elif not actual_keys or not set(actual_keys).issubset(expected):
        # probe 可以是显式子集，但至少一项且每项都必须来自当前受信目录。
        raise ApiError("adapter.contract_report_invalid")
    elif any(expected[key].case_kind not in {"non_stream", "stream"} for key in actual_keys):
        # 真实 Gateway probe 只执行响应与 SSE 合同；request/error/usage mock case 不能伪装成探测证据。
        raise ApiError("adapter.contract_report_invalid")

    actual_failed_codes: set[str] = set()
    passed = 0
    for case in report.cases:
        definition = expected.get(case.case_key)
        if definition is None:
            # 未知 case 身份没有受信 spec 可供绑定，不能作为合同证据落库。
            raise ApiError("adapter.contract_report_invalid")
        _validate_case(report, case, definition)
        if case.status == "passed":
            passed += 1
        elif case.failure_code is not None:
            actual_failed_codes.add(case.failure_code)
    if (
        passed != report.passed_cases
        or report.total_cases - passed != report.failed_cases
        or sorted(actual_failed_codes) != report.failure_codes
    ):
        # 汇总计数和失败码必须由逐项结果精确推导，避免摘要与明细出现双重事实。
        raise ApiError("adapter.contract_report_invalid")
    return expected


def _validate_case(
    report: AdapterContractRunReport,
    case: AdapterContractCaseReport,
    expected: _ExpectedCase,
) -> None:
    """把单条结果与 spec、稳定 ID、状态和固定失败码精确绑定。"""

    derived_id = "acc_" + hashlib.sha256(
        f"{report.run_id}:{case.case_key}".encode("utf-8")
    ).hexdigest()[:32]
    allowed_failure = {_CASE_FAILURE_CODE[expected.case_kind]}
    if report.suite_kind == "probe":
        allowed_failure.add("adapter.contract_probe_failed")
    if (
        not _CASE_ID.fullmatch(case.adapter_contract_case_id)
        or case.adapter_contract_case_id != derived_id
        or case.tenant_id != report.tenant_id
        or case.contract_case_id != expected.spec.contract_case_id
        or case.spec_id != expected.spec.spec_id
        or case.provider_type != expected.spec.provider_type
        or case.adapter_name != expected.spec.adapter_name
        or case.adapter_version != expected.spec.adapter_version
        or case.case_kind != expected.case_kind
        or case.variant != expected.variant
        or case.auth_type != expected.auth_type
        or type(case.duration_ms) is not int
        or not 0 <= case.duration_ms <= 300_000
        or not _LOWER_HEX_64.fullmatch(case.evidence_hash)
        or (case.status == "passed" and case.failure_code is not None)
        or (case.status == "failed" and case.failure_code not in allowed_failure)
    ):
        # case 身份、耗时、证据哈希和失败分类任一漂移都会破坏不可变合同证明。
        raise ApiError("adapter.contract_report_invalid")


def _expected_cases() -> dict[str, _ExpectedCase]:
    """由当前 Shared 九类 spec 动态生成 exact-set，禁止写死 92 或自动接受未知 spec。"""

    result: dict[str, _ExpectedCase] = {}
    for spec in provider_adapter_specs():
        for auth_type in spec.allowed_auth_types:
            key = f"{spec.contract_case_id}:request:auth:{auth_type}"
            result[key] = _ExpectedCase(key, spec, "request", "auth", auth_type)
        for kind, variant in _FIXED_CASES:
            key = f"{spec.contract_case_id}:{kind}:{variant}"
            result[key] = _ExpectedCase(key, spec, kind, variant, None)
    # 任何重复 key 都说明目录合同损坏，不能让后写项静默覆盖前一 spec。
    expected_count = sum(len(spec.allowed_auth_types) + len(_FIXED_CASES) for spec in provider_adapter_specs())
    if len(result) != expected_count:
        raise ApiError("adapter.contract_catalog_mismatch")
    return result


def _validate_tenant_catalog(
    connection: sqlite3.Connection,
    tenant_id: str,
    report: AdapterContractRunReport,
) -> None:
    """核对租户存在、active activation、报告目录与当前进程制品四方一致。"""

    if connection.execute(
        "SELECT 1 FROM tenants WHERE tenant_id=?", (tenant_id,)
    ).fetchone() is None:
        # 未知租户与非法租户统一返回 404，不暴露租户注册状态。
        raise ApiError("adapter.contract_run_not_found")
    row = connection.execute(
        """
        SELECT catalog_version,catalog_hash,spec_count
        FROM provider_adapter_catalog_activations
        WHERE tenant_id=? AND status='active'
        """,
        (tenant_id,),
    ).fetchone()
    identity = catalog_identity()
    if (
        row is None
        or row["catalog_version"] != identity.version
        or row["catalog_hash"] != identity.catalog_hash
        or int(row["spec_count"]) != identity.spec_count
        or report.catalog_version != identity.version
        or report.catalog_hash != identity.catalog_hash
    ):
        # active/制品/报告任一漂移都拒绝，不能按报告自报目录保存为可信 CI 证据。
        raise ApiError("adapter.contract_catalog_mismatch")


def _contract_failed_event(report: AdapterContractRunReport):
    """从已验证失败报告构造固定事件；调用方只持久化 event ID，不保存第二份 payload。"""

    return build_webhook_event(
        tenant_id=report.tenant_id,
        event_type="adapter.contract_failed",
        event_id=f"evt_{report.run_id}",
        timestamp=_parse_time(report.completed_at),
        actor_id="adapter-contract-runner",
        object_id=report.run_id,
        trace_id=report.trace_id,
        source_request_id=report.request_id,
        payload={
            "run_id": report.run_id,
            "catalog_version": report.catalog_version,
            "catalog_hash": report.catalog_hash,
            "suite_kind": report.suite_kind,
            "mode": report.mode,
            "total_cases": report.total_cases,
            "failed_cases": report.failed_cases,
            "failure_codes": report.failure_codes,
            "completed_at": report.completed_at,
        },
    )


def _run_info(row: sqlite3.Row) -> AdapterContractRunInfo:
    """把一条同租户 run 行转换为公开摘要，并拒绝损坏 failure code JSON。"""

    try:
        failure_codes = json.loads(row["failure_codes_json"])
    # 持久失败码 JSON 无法解析时禁止返回部分 run，且不得回显损坏原文。
    except (TypeError, json.JSONDecodeError):
        # 切断 JSON 异常链，只公开统一存储错误以保护持久内容。
        raise ApiError("adapter.contract_storage_unavailable") from None
    if (
        type(failure_codes) is not list
        or failure_codes != sorted(set(failure_codes))
        or any(type(code) is not str or code not in ALLOWED_FAILURE_CODES for code in failure_codes)
    ):
        # 非白名单或非 canonical 失败码说明不可变行已漂移，必须失败关闭。
        raise ApiError("adapter.contract_storage_unavailable")
    return AdapterContractRunInfo(
        adapter_contract_run_id=row["adapter_contract_run_id"],
        tenant_id=row["tenant_id"],
        source_request_id=row["source_request_id"],
        source_trace_id=row["source_trace_id"],
        schema_version=row["schema_version"],
        catalog_version=row["catalog_version"],
        catalog_hash=row["catalog_hash"],
        runner_version=row["runner_version"],
        suite_kind=row["suite_kind"],
        mode=row["mode"],
        source_revision_hash=row["source_revision_hash"],
        status=row["status"],
        coverage_complete=bool(row["coverage_complete"]),
        total_cases=int(row["total_cases"]),
        passed_cases=int(row["passed_cases"]),
        failed_cases=int(row["failed_cases"]),
        failure_codes=failure_codes,
        report_hash=row["report_hash"],
        started_at=_parse_time(row["started_at"], storage=True),
        completed_at=_parse_time(row["completed_at"], storage=True),
        created_at=_parse_time(row["created_at"], storage=True),
    )


def _case_info(row: sqlite3.Row) -> AdapterContractCaseInfo:
    """把不可变 case 行投影为页面安全字段，不返回 rowid 或 outbox 关联。"""

    return AdapterContractCaseInfo(
        adapter_contract_case_id=row["adapter_contract_case_id"],
        tenant_id=row["tenant_id"],
        adapter_contract_run_id=row["adapter_contract_run_id"],
        contract_case_key=row["contract_case_key"],
        spec_id=row["spec_id"],
        provider_type=row["provider_type"],
        adapter_name=row["adapter_name"],
        adapter_version=row["adapter_version"],
        case_kind=row["case_kind"],
        variant=row["variant"],
        auth_type=row["auth_type"],
        status=row["status"],
        failure_code=row["failure_code"],
        duration_ms=int(row["duration_ms"]),
        evidence_hash=row["evidence_hash"],
        created_at=_parse_time(row["created_at"], storage=True),
    )


def _canonical_report_hash(report: AdapterContractRunReport) -> str:
    """对完整严格报告生成与 Gateway 一致的 canonical SHA-256。"""

    encoded = json.dumps(
        report.model_dump(mode="python"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enum_filter(value: str | None, allowed: set[str]) -> str | None:
    """严格校验可选列表筛选，不做大小写或空白宽松归一。"""

    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        # 列表筛选只接受显式枚举，拒绝宽松大小写和未知值造成查询语义漂移。
        raise ApiError("adapter.contract_report_invalid")
    return value


def _encode_cursor(completed_at: str, run_id: str) -> str:
    """把稳定排序键编码为不含租户或敏感正文的不透明 Base64URL。"""

    payload = _json_dumps({"completed_at": completed_at, "run_id": run_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    """严格解码 canonical cursor，拒绝替代编码、额外字段和损坏时间。"""

    if type(value) is not str or not 1 <= len(value) <= 512 or "=" in value:
        # 非规范或超长 cursor 在解码前拒绝，避免等价编码和资源放大。
        raise ApiError("adapter.contract_report_invalid")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    # Base64、UTF-8 或 JSON 任一失败都映射为固定输入错误，不回显 cursor 内容。
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        # 切断解码异常文本，防止原始 query 片段进入统一错误响应。
        raise ApiError("adapter.contract_report_invalid") from None
    if (
        type(payload) is not dict
        or set(payload) != {"completed_at", "run_id"}
        or type(payload["completed_at"]) is not str
        or type(payload["run_id"]) is not str
        or not _CURSOR_RUN_ID.fullmatch(payload["run_id"])
        or _encode_cursor(payload["completed_at"], payload["run_id"]) != value
    ):
        # cursor 必须字段精确且可重新编码为自身，拒绝多义分页位置。
        raise ApiError("adapter.contract_report_invalid")
    _parse_time(payload["completed_at"])
    return payload["completed_at"], payload["run_id"]


def _normalize_tenant_id(value: object) -> str:
    """校验 path tenant；非法与不存在统一 404，避免形成租户命名侧信道。"""

    # path tenant 是内部上报唯一可信租户来源，格式不合法时不能尝试宽松归一。
    if type(value) is not str or not _TENANT_ID.fullmatch(value):
        # 固定 404 同时覆盖非法与不存在租户，避免暴露命名空间信息。
        raise ApiError("adapter.contract_run_not_found")
    return value


def _parse_time(value: object, *, storage: bool = False) -> datetime:
    """解析规范 UTC 秒；存储损坏映射 503，输入错误映射 422。"""

    error_code = "adapter.contract_storage_unavailable" if storage else "adapter.contract_report_invalid"
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        # 非规范时间按来源分别映射输入或存储错误，避免宽松解析改变排序。
        raise ApiError(error_code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 时间解析异常可能包含原值，转换前建立固定领域错误边界。
    except ValueError:
        # 不保留底层异常链，防止报告或持久时间原文进入响应。
        raise ApiError(error_code) from None
    if parsed.tzinfo is None or _db_time(parsed) != value:
        # 时区缺失或不可 canonical 往返会破坏游标和事件时间绑定，因此拒绝。
        raise ApiError(error_code)
    return parsed.astimezone(UTC)


def _json_dumps(value: object) -> str:
    """使用唯一 canonical JSON 编码保存数组和 cursor，不允许 NaN。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建短连接，并把数据库配置或路径异常折叠为领域 503。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL 或路径异常可能包含部署信息，必须在服务边界转换。
    except DbError as exc:
        # 对外仅公开统一存储错误码，原异常仅作为内部因果链保存。
        raise ApiError("adapter.contract_storage_unavailable") from exc


def _db_time(value: datetime) -> str:
    """把可信时间统一编码为秒级 UTC，保证 SQLite 排序与 Webhook 精确绑定。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回本服务使用的秒级 UTC 当前时间。"""

    return datetime.now(UTC).replace(microsecond=0)


__all__ = [
    "ALLOWED_FAILURE_CODES",
    "get_adapter_contract_run",
    "ingest_adapter_contract_run",
    "list_adapter_contract_runs",
]
