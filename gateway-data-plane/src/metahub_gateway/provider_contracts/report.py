"""PRD-064 契约报告 canonical 编码、摘要和工作区内原子落盘。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from .models import ContractCaseResult, ContractRunReport

# 报告版本、大小和各类稳定码同时约束本地 CI 制品与 Control 上报边界；调整任一值都需要
# 升级 schema 并同步后端严格 validator，不能靠宽松反序列化兼容未知字段。
_REPORT_SCHEMA_VERSION = "adapter-contract-report-v1"
_MAX_REPORT_BYTES = 512 * 1024
_MAX_CASES = 2048
_SAFE_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}\Z")
_TRACE_ID = re.compile(r"trc_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}\Z")
_RUN_ID = re.compile(r"acr_[0-9a-f]{32}\Z")
_CASE_ID = re.compile(r"acc_[0-9a-f]{32}\Z")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,159}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_SAFE_CASE_KEY = re.compile(r"[a-z0-9_][a-z0-9_.:-]{0,255}\Z")
_CONTRACT_CASE_ID = re.compile(r"(?:prd063|prd084)_[a-z0-9_]{2,80}\Z")
_ADAPTER_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")

# kind、variant、auth 和失败码必须形成一组精确合同；这能阻止 skip/unknown 或错误码漂移
# 被序列化成看似完整的报告。
_CASE_VARIANTS: dict[str, frozenset[str]] = {
    "request": frozenset({"auth"}),
    "non_stream": frozenset({"success"}),
    "stream": frozenset({"success"}),
    "error": frozenset({"auth_401", "rate_limit_429", "server_5xx", "timeout"}),
    "usage": frozenset({"standard", "missing", "invalid"}),
}
_FAILURE_CODE_BY_KIND = {
    "request": "adapter.contract_request_failed",
    "non_stream": "adapter.contract_non_stream_failed",
    "stream": "adapter.contract_stream_failed",
    "error": "adapter.contract_error_mapping_failed",
    "usage": "adapter.contract_usage_failed",
}


def _invalid_report() -> NoReturn:
    """以固定错误关闭无效报告，不把触发字段或正文拼入异常。"""

    # 统一错误文本防止畸形字段、绝对路径或可疑正文穿透到 CLI stderr。
    raise ValueError("adapter.contract_report_invalid")


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    """先核对字符串类型再执行正则，确保畸形字段也只产生固定报告错误。"""

    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _parse_utc_second(value: object) -> datetime:
    """解析 canonical UTC 秒时间，拒绝时区别名、小数秒和宽松日期格式。"""

    if not isinstance(value, str):
        _invalid_report()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # 宽松日期解析可能接受时区别名或小数秒，报告身份必须只承认冻结格式。
    except ValueError:
        _invalid_report()
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        _invalid_report()
    return parsed


def _validate_case(report: ContractRunReport, case: object) -> ContractCaseResult:
    """核对单个 case 的租户、稳定 ID、状态、错误码和安全证据字段。"""

    if not isinstance(case, ContractCaseResult):
        _invalid_report()
    if (
        not _matches(_CASE_ID, case.adapter_contract_case_id)
        or not _matches(_CONTRACT_CASE_ID, case.contract_case_id)
        or case.tenant_id != report.tenant_id
        or not _matches(_SAFE_CODE, case.spec_id)
        or not _matches(_SAFE_CODE, case.provider_type)
        or not _matches(_SAFE_CODE, case.adapter_name)
        or not _matches(_ADAPTER_VERSION, case.adapter_version)
        or not _matches(_SAFE_CASE_KEY, case.case_key)
        or not case.case_key.startswith(f"{case.contract_case_id}:")
        or not _matches(_LOWER_HEX_64, case.evidence_hash)
        or not isinstance(case.case_kind, str)
        or type(case.duration_ms) is not int
        or not 0 <= case.duration_ms <= 300_000
    ):
        _invalid_report()
    expected_case_id = (
        "acc_"
        + hashlib.sha256(
            f"{report.run_id}:{case.case_key}".encode()
        ).hexdigest()[:32]
    )
    if case.adapter_contract_case_id != expected_case_id:
        _invalid_report()
    variants = _CASE_VARIANTS.get(case.case_kind)
    if variants is None or case.variant not in variants:
        _invalid_report()
    # request 必须指出实际鉴权分支，避免 none/bearer 覆盖只留下无法审计的通用结果。
    if case.case_kind == "request":
        if case.auth_type not in {"none", "bearer"}:
            _invalid_report()
    # 非 request case 不按鉴权拆分，携带 auth 会制造矩阵之外的额外维度。
    elif case.auth_type is not None:
        _invalid_report()
    # probe 只证明 Gateway 端到端结果，无法把失败归因到内部五类 mock 断言，因此使用专用码。
    expected_failure = (
        "adapter.contract_probe_failed"
        if report.suite_kind == "probe"
        else _FAILURE_CODE_BY_KIND[case.case_kind]
    )
    if case.status == "passed":
        if case.failure_code is not None:
            _invalid_report()
    elif case.status == "failed":
        if case.failure_code != expected_failure:
            _invalid_report()
    else:
        _invalid_report()
    return case


def validate_report(report: ContractRunReport) -> None:
    """严格验证报告全部可持久化语义，防止无效状态被 canonical 化。

    参数：runner 生成的不可变报告。返回：验证通过时无返回值。计数、状态、failure codes、
    case ID、租户 owner、时间和 suite/mode 任一漂移都使用固定错误失败关闭。
    """

    if not isinstance(report, ContractRunReport):
        _invalid_report()
    if (
        report.schema_version != _REPORT_SCHEMA_VERSION
        or not _matches(_SAFE_TENANT_ID, report.tenant_id)
        or not _matches(_RUN_ID, report.run_id)
        or not _matches(_REQUEST_ID, report.request_id)
        or not _matches(_TRACE_ID, report.trace_id)
        or not _matches(_SAFE_VERSION, report.catalog_version)
        or not _matches(_LOWER_HEX_64, report.catalog_hash)
        or not _matches(_ADAPTER_VERSION, report.runner_version)
        or not _matches(_LOWER_HEX_64, report.source_revision_hash)
        or type(report.coverage_complete) is not bool
        or type(report.total_cases) is not int
        or type(report.passed_cases) is not int
        or type(report.failed_cases) is not int
        or not isinstance(report.cases, tuple)
        or not isinstance(report.failure_codes, tuple)
    ):
        _invalid_report()
    if report.suite_kind == "merge_gate":
        if report.mode != "mock" or not report.coverage_complete:
            _invalid_report()
    elif report.suite_kind == "probe":
        if report.mode != "gateway_probe" or report.coverage_complete:
            _invalid_report()
    else:
        _invalid_report()
    if not 0 < report.total_cases <= _MAX_CASES or report.total_cases != len(report.cases):
        _invalid_report()

    cases = tuple(_validate_case(report, case) for case in report.cases)
    if report.suite_kind == "probe" and any(
        case.case_kind not in {"non_stream", "stream"} for case in cases
    ):
        # 真实探测只允许端到端请求，不得把 request/error/usage mock case 冒充在线证据。
        _invalid_report()
    case_keys = tuple(case.case_key for case in cases)
    case_ids = tuple(case.adapter_contract_case_id for case in cases)
    if (
        case_keys != tuple(sorted(case_keys))
        or len(case_keys) != len(set(case_keys))
        or len(case_ids) != len(set(case_ids))
    ):
        _invalid_report()
    passed = sum(case.status == "passed" for case in cases)
    failed = len(cases) - passed
    failure_codes = tuple(
        sorted({case.failure_code for case in cases if case.failure_code is not None})
    )
    if (
        report.passed_cases != passed
        or report.failed_cases != failed
        or report.total_cases != passed + failed
        or report.status != ("passed" if failed == 0 else "failed")
        or report.failure_codes != failure_codes
    ):
        _invalid_report()

    started = _parse_utc_second(report.started_at)
    completed = _parse_utc_second(report.completed_at)
    if completed < started or completed > datetime.now(UTC) + timedelta(minutes=5):
        _invalid_report()


def canonical_report_bytes(report: ContractRunReport) -> bytes:
    """返回稳定 UTF-8 JSON；拒绝 NaN 并固定 key/分隔符，供幂等 hash 和上报共用。"""

    validate_report(report)
    encoded = json.dumps(
        report.to_wire(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_REPORT_BYTES:
        _invalid_report()
    return encoded


def report_hash(report: ContractRunReport) -> str:
    """计算完整 canonical 报告的 SHA-256，不暴露报告正文。"""

    return hashlib.sha256(canonical_report_bytes(report)).hexdigest()


def write_report_atomic(
    report: ContractRunReport,
    output_path: Path,
    *,
    workspace_root: Path,
) -> Path:
    """只在工作区 `.artifacts` 下原子写入报告。

    参数：不可变报告、目标文件和显式工作区根。返回：resolve 后目标路径。
    关键约束：先校验父路径归属再创建目录；pending 文件与目标位于同一目录，`os.replace`
    保证读者不会观察到半份 JSON。失败清理也只触及本次生成的 pending 文件。
    """

    root = workspace_root.resolve(strict=True)
    artifact_root = (root / ".artifacts").resolve(strict=False)
    try:
        # `.artifacts` 自身若被链接到工作区外也必须失败，不能把“目标相对 artifacts”误当成
        # “目标仍在工作区”的证明。
        artifact_root.relative_to(root)
    # 链接逃逸时不回显解析后的外部路径，只返回固定归属错误。
    except ValueError as exc:
        raise ValueError("adapter.contract_report_path_invalid") from exc
    target = output_path.resolve(strict=False)
    try:
        target.relative_to(artifact_root)
    # 目标不属于制品根时必须在 mkdir 和 pending 写入前失败，避免留下越界文件。
    except ValueError as exc:
        raise ValueError("adapter.contract_report_path_invalid") from exc
    content = canonical_report_bytes(report) + b"\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(f".{target.name}.{uuid4().hex}.pending")
    try:
        pending.write_bytes(content)
        os.replace(pending, target)
    finally:
        # 仅删除本次随机 pending；目标已替换成功时该路径不存在，不会误删其他测试证据。
        if pending.exists():
            pending.unlink()
    return target
