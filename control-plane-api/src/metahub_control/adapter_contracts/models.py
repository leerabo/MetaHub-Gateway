"""PRD-064 Provider Adapter 契约运行的严格输入与只读响应模型。

本模块只承载已经脱敏的合同身份、固定状态、计数与不可逆哈希。Provider Key、Token、
Endpoint、Prompt、Response、Header 和上游错误正文都不属于报告合同，也不得进入 DTO。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# 报告输入由路由在鉴权后手工解析，因此模型必须使用 strict/forbid，不能把字符串数字或额外
# 字段宽松转换成另一份事实。响应模型同样 forbid，防止以后误把内部 outbox 字段暴露给页面。
class _StrictModel(BaseModel):
    """为 PRD-064 DTO 统一启用严格类型和未知字段拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class AdapterContractCaseReport(_StrictModel):
    """CI 上报的单条安全用例结果。

    字段只描述冻结 spec、用例维度、状态和证据摘要；``contract_case_id`` 用于核对 PRD-063
    显式夹具，持久化时由完整 ``case_key`` 和 spec 身份替代，不保存任何断言正文。
    """

    case_key: str
    adapter_contract_case_id: str
    contract_case_id: str
    tenant_id: str
    spec_id: str
    provider_type: str
    adapter_name: str
    adapter_version: str
    case_kind: Literal["request", "non_stream", "stream", "error", "usage"]
    variant: str
    auth_type: Literal["none", "bearer"] | None
    status: Literal["passed", "failed"]
    failure_code: str | None
    duration_ms: int
    evidence_hash: str


class AdapterContractRunReport(_StrictModel):
    """内部入口接收的 ``adapter-contract-report-v1`` 完整报告。

    request/trace 必须由服务层与本次 HTTP 中间件身份精确绑定；模型本身不信任正文里的
    tenant_id，也不会根据正文选择数据库租户。
    """

    schema_version: Literal["adapter-contract-report-v1"]
    tenant_id: str
    run_id: str
    request_id: str
    trace_id: str
    catalog_version: str
    catalog_hash: str
    runner_version: str
    suite_kind: Literal["merge_gate", "probe"]
    mode: Literal["mock", "gateway_probe"]
    source_revision_hash: str
    status: Literal["passed", "failed"]
    coverage_complete: bool
    total_cases: int
    passed_cases: int
    failed_cases: int
    failure_codes: list[str] = Field(max_length=16)
    started_at: str
    completed_at: str
    cases: list[AdapterContractCaseReport] = Field(min_length=1, max_length=2048)


class AdapterContractCaseInfo(_StrictModel):
    """PAGE-091 可展示的持久用例摘要，不含报告正文或内部调度状态。"""

    adapter_contract_case_id: str
    tenant_id: str
    adapter_contract_run_id: str
    contract_case_key: str
    spec_id: str
    provider_type: str
    adapter_name: str
    adapter_version: str
    case_kind: Literal["request", "non_stream", "stream", "error", "usage"]
    variant: str
    auth_type: Literal["none", "bearer"] | None
    status: Literal["passed", "failed"]
    failure_code: str | None
    duration_ms: int
    evidence_hash: str
    created_at: datetime


class AdapterContractRunInfo(_StrictModel):
    """一次合同运行的租户级只读摘要。"""

    adapter_contract_run_id: str
    tenant_id: str
    source_request_id: str
    source_trace_id: str
    schema_version: str
    catalog_version: str
    catalog_hash: str
    runner_version: str
    suite_kind: Literal["merge_gate", "probe"]
    mode: Literal["mock", "gateway_probe"]
    source_revision_hash: str
    status: Literal["passed", "failed"]
    coverage_complete: bool
    total_cases: int
    passed_cases: int
    failed_cases: int
    failure_codes: list[str]
    report_hash: str
    started_at: datetime
    completed_at: datetime
    created_at: datetime


class AdapterContractRunIngestResponse(_StrictModel):
    """内部上报响应；replayed 明确区分首次写入和纯幂等回读。"""

    adapter_contract_run: AdapterContractRunInfo
    replayed: bool
    request_id: str
    trace_id: str


class AdapterContractRunListResponse(_StrictModel):
    """当前租户游标分页列表，cursor 对客户端保持不透明。"""

    adapter_contract_runs: list[AdapterContractRunInfo]
    limit: int
    next_cursor: str | None
    request_id: str
    trace_id: str


class AdapterContractRunDetailResponse(_StrictModel):
    """当前租户单次运行及其有界用例明细。"""

    adapter_contract_run: AdapterContractRunInfo
    cases: list[AdapterContractCaseInfo]
    request_id: str
    trace_id: str


__all__ = [
    "AdapterContractCaseInfo",
    "AdapterContractCaseReport",
    "AdapterContractRunDetailResponse",
    "AdapterContractRunInfo",
    "AdapterContractRunIngestResponse",
    "AdapterContractRunListResponse",
    "AdapterContractRunReport",
]
