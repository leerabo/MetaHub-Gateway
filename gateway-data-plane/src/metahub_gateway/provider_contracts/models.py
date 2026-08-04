"""定义 Provider Adapter 契约框架的不可变内部数据结构。

这些结构是 CI 制品模型而不是客户请求 DTO。所有正文类字段都隐藏 repr，运行报告只允许
输出安全身份、固定状态、耗时、错误码和不可逆证据摘要。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping


# 合并门禁只承认这五类稳定合同，新增类别必须先升级报告 schema 和持久化白名单。
ContractCaseKind = Literal["request", "non_stream", "stream", "error", "usage"]
# skip/unknown 会让合并门禁形成假通过，因此结果只能是明确通过或失败。
ContractCaseStatus = Literal["passed", "failed"]
# merge gate 与真实探测的覆盖语义不同，报告必须显式区分。
ContractSuiteKind = Literal["merge_gate", "probe"]
# mock 是默认离线门禁；gateway_probe 只能由显式受保护任务启用。
ContractMode = Literal["mock", "gateway_probe"]


@dataclass(frozen=True)
class ContractUsageExpectation:
    """保存 usage 归一化后的数值合同。

    参数均为非敏感数值或固定标志。Provider 原始 usage 只存在于隐藏夹具中，不能进入
    结果或报告。
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    estimated: bool
    estimate_reason: str | None = None
    provider_cost_amount: str | None = None
    provider_cost_currency: str | None = None
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterContractFixture:
    """描述一个冻结 Provider spec 的显式合同夹具。

    每个 spec 必须在源码中提交一个实例。JSON/SSE/错误正文只包含合成占位数据且
    `repr=False`；runner 只把断言结论的哈希写入报告。
    """

    contract_case_id: str
    tenant_id: str
    spec_id: str
    provider_type: str
    adapter_name: str
    adapter_version: str
    success_payload: Mapping[str, object] = field(repr=False)
    stream_body: bytes = field(repr=False)
    standard_usage_payload: Mapping[str, object] = field(repr=False)
    standard_usage: ContractUsageExpectation
    invalid_usage_payload: Mapping[str, object] = field(repr=False)
    invalid_usage: ContractUsageExpectation


@dataclass(frozen=True)
class ContractCaseDefinition:
    """表示 exact-set 门禁中的一个稳定用例键。

    case key 由 spec 引用 ID、kind、variant 和可选 auth 规范生成，数据库与 CI 报告均使用
    同一键，不能依赖测试收集顺序。
    """

    case_key: str
    contract_case_id: str
    tenant_id: str
    spec_id: str
    provider_type: str
    adapter_name: str
    adapter_version: str
    case_kind: ContractCaseKind
    variant: str
    auth_type: str | None = None


@dataclass(frozen=True)
class ContractCaseResult:
    """保存单个契约用例的安全结果。

    failure_code 只能取框架固定枚举；evidence_hash 是安全断言的 canonical 摘要，不能由
    请求、响应或异常原文直接计算后暴露可猜测内容。
    """

    case_key: str
    adapter_contract_case_id: str
    contract_case_id: str
    tenant_id: str
    spec_id: str
    provider_type: str
    adapter_name: str
    adapter_version: str
    case_kind: ContractCaseKind
    variant: str
    auth_type: str | None
    status: ContractCaseStatus
    failure_code: str | None
    duration_ms: int
    evidence_hash: str

    def to_wire(self) -> dict[str, object]:
        """返回严格、安全的持久化字段，不包含夹具或异常正文。"""

        return {
            "case_key": self.case_key,
            "adapter_contract_case_id": self.adapter_contract_case_id,
            "contract_case_id": self.contract_case_id,
            "tenant_id": self.tenant_id,
            "spec_id": self.spec_id,
            "provider_type": self.provider_type,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "case_kind": self.case_kind,
            "variant": self.variant,
            "auth_type": self.auth_type,
            "status": self.status,
            "failure_code": self.failure_code,
            "duration_ms": self.duration_ms,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class ContractRunReport:
    """保存一次合并门禁或真实探测的版本化安全报告。

    报告必须携带 tenant/request/trace 和 catalog identity，便于 Control 按租户持久化并把
    失败事件关联到同一链路。`cases` 不包含任何 wire 正文。
    """

    schema_version: str
    tenant_id: str
    run_id: str
    request_id: str
    trace_id: str
    catalog_version: str
    catalog_hash: str
    runner_version: str
    suite_kind: ContractSuiteKind
    mode: ContractMode
    source_revision_hash: str
    status: ContractCaseStatus
    coverage_complete: bool
    total_cases: int
    passed_cases: int
    failed_cases: int
    failure_codes: tuple[str, ...]
    started_at: str
    completed_at: str
    cases: tuple[ContractCaseResult, ...]

    def to_wire(self) -> dict[str, object]:
        """按报告 v1 冻结字段返回新的 canonical JSON 对象。

        返回值只包含安全身份、计数、固定错误码和不可逆摘要；调用方修改列表不会污染
        不可变报告，也不能借此加入 Header、正文或 Endpoint。
        """

        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "catalog_version": self.catalog_version,
            "catalog_hash": self.catalog_hash,
            "runner_version": self.runner_version,
            "suite_kind": self.suite_kind,
            "mode": self.mode,
            "source_revision_hash": self.source_revision_hash,
            "status": self.status,
            "coverage_complete": self.coverage_complete,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "failure_codes": list(self.failure_codes),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cases": [case.to_wire() for case in self.cases],
        }
