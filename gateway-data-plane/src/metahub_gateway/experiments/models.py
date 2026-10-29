"""定义实验热路径的有界 owner、路由结果和 Provider 独立事实。

这些对象只携带 tenant/request/model 等安全本地 ID；execution token 使用 ``repr=False``，
任何模型都不得加入 Prompt、Response、Provider Key、Endpoint 或原始异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from metahub_gateway.providers.base import (
    NormalizedProviderError,
    is_quality_observation_eligible_error,
)
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan

ExperimentExposure = Literal["not_exposed", "exposed", "unknown"]
ExperimentGatewayOutcome = Literal["success", "failed", "unresolved"]
ExperimentSettlementDisposition = Literal["pending", "not_exposed", "unavailable"]
ResponseCacheDisposition = Literal[
    "not_in_scope",
    "active_scope_bypass",
    "unresolved",
]


@dataclass(frozen=True, slots=True)
class ExperimentExecutionHandle:
    """Assignment 事务返回的租约围栏 owner，覆盖请求直到客户端逻辑终态。"""

    tenant_id: str
    experiment_id: str
    assignment_id: str
    execution_id: str
    request_id: str
    trace_id: str
    generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    absolute_deadline: datetime


@dataclass(frozen=True, slots=True)
class ExperimentRouteResolution:
    """一次公开 Chat 的权威实验解析结果，明确区分旁路、选中与固定失败。

    ``response_cache_disposition`` 没有默认值，目的是迫使每条实验解析分支显式说明
    后续 Response Cache 能否介入。活跃实验即使未采样也必须旁路缓存，否则命中会改变
    实验总体 exposure、延迟和成本基线。
    """

    matched: bool
    response_cache_disposition: ResponseCacheDisposition
    route_plan: GatewayRoutePlan | None = None
    execution: ExperimentExecutionHandle | None = None
    failed_route: GatewayRoute | None = None
    error_code: str | None = None

    @classmethod
    def bypass(
        cls,
        *,
        response_cache_disposition: Literal["not_in_scope", "active_scope_bypass"],
    ) -> ExperimentRouteResolution:
        """返回未分配实验臂的结果，并要求调用者明确缓存处置。

        ``not_in_scope`` 只用于权威证明没有活跃实验；``active_scope_bypass`` 用于活跃
        实验的未采样请求。二者都继续普通路由，但只有前者可由 PRD-076 再做资格判断。
        """

        return cls(
            matched=False,
            response_cache_disposition=response_cache_disposition,
        )

    @classmethod
    def selected(
        cls,
        route_plan: GatewayRoutePlan,
        execution: ExperimentExecutionHandle,
    ) -> ExperimentRouteResolution:
        """返回唯一实验候选路由及 execution handle，禁止携带任何跨臂回退计划。"""

        return cls(
            matched=True,
            response_cache_disposition="active_scope_bypass",
            route_plan=route_plan,
            execution=execution,
        )

    @classmethod
    def failed(
        cls,
        route: GatewayRoute,
        error_code: str,
    ) -> ExperimentRouteResolution:
        """保留已分配 arm 的安全失败 route，使 RequestLog 可追踪但禁止调用 Provider。"""

        return cls(
            matched=True,
            response_cache_disposition="active_scope_bypass",
            failed_route=route,
            error_code=error_code,
        )


@dataclass(slots=True)
class ProviderOutcomeFacts:
    """记录一次 Assignment 的 exposure 与质量候选，两条事实轴互不推导。"""

    send_fenced: bool = False
    exposure: ExperimentExposure = "not_exposed"
    quality_eligible: bool = False
    quality_succeeded: bool = False
    quality_latency_ms: int | None = None
    provider_response_received: bool = False
    provider_error_received: bool = False
    routing_attempt_id: str | None = None
    stream_session_id: str | None = None

    def bind_attempt(self, routing_attempt_id: str) -> None:
        """绑定非流 Attempt owner，仅作为终态证据而不证明已经出网。"""

        self.routing_attempt_id = routing_attempt_id

    def bind_stream(self, stream_session_id: str) -> None:
        """绑定流 Session owner，握手与最终质量仍由后续方法分别确认。"""

        self.stream_session_id = stream_session_id

    def mark_send_fenced(self) -> None:
        """标记数据库最终发送许可已取得，后续无响应错误只能保守归类。"""

        self.send_fenced = True

    def mark_response(self, *, latency_ms: int = 0) -> None:
        """把可信非流成功响应记为 exposed 且质量成功，并冻结非负延迟。"""

        self.exposure = "exposed"
        self.quality_eligible = True
        self.quality_succeeded = True
        self.quality_latency_ms = max(0, latency_ms)
        self.provider_response_received = True

    def mark_stream_handshake(self) -> None:
        """记录可信流式 2xx 握手，但在流 body 收口前不提前判定质量。"""

        self.exposure = "exposed"
        self.provider_response_received = True
        self.quality_eligible = False
        self.quality_succeeded = False
        self.quality_latency_ms = None

    def mark_stream_terminal(
        self,
        *,
        quality_eligible: bool,
        quality_succeeded: bool,
        latency_ms: int,
    ) -> None:
        """在流 finalizer 后按既有 Session 终态冻结独立质量候选和延迟。"""

        self.quality_eligible = quality_eligible and self.exposure == "exposed"
        self.quality_succeeded = bool(quality_succeeded and self.quality_eligible)
        self.quality_latency_ms = max(0, latency_ms) if self.quality_eligible else None

    def mark_provider_error(
        self,
        error: NormalizedProviderError | None,
        *,
        latency_ms: int = 0,
    ) -> None:
        """依据可信 HTTP/transport 证据分类 exposure，再独立计算质量资格。"""

        if not self.send_fenced:
            self.exposure = "not_exposed"
            self.quality_eligible = False
            self.quality_succeeded = False
            self.quality_latency_ms = None
            return
        self.provider_error_received = True
        evidence_status = (
            error.evidence.upstream_http_status
            if error is not None and error.evidence is not None
            else None
        )
        http_status = error.http_status if error is not None else None
        transport_kind = error.transport_kind if error is not None else "other"
        if evidence_status is not None or http_status is not None:
            self.exposure = "exposed"
        elif transport_kind == "local_configuration":
            self.exposure = "not_exposed"
        else:
            self.exposure = "unknown"
        self.quality_eligible = bool(
            self.exposure == "exposed" and is_quality_observation_eligible_error(error)
        )
        self.quality_succeeded = False
        self.quality_latency_ms = max(0, latency_ms) if self.quality_eligible else None

    def mark_local_failure(self) -> None:
        """把发送许可后的本地不确定故障提升为 unknown，且不计模型质量。"""

        if self.send_fenced and self.exposure == "not_exposed":
            self.exposure = "unknown"
        self.quality_eligible = False
        self.quality_succeeded = False
        self.quality_latency_ms = None

    @property
    def settlement_disposition(self) -> ExperimentSettlementDisposition:
        """按 exposure 和 Provider 错误事实决定初始用量结算处置。"""

        if self.exposure == "not_exposed":
            return "not_exposed"
        # 已证明暴露但没有可用 Usage 的 Provider 错误必须显式记 unknown cost，不能显示为零。
        if self.exposure == "exposed" and self.provider_error_received:
            return "unavailable"
        return "pending"


@dataclass(frozen=True, slots=True)
class ExperimentTerminalIntent:
    """在有效 execution 租约下提交的 append-only Gateway 最终事实。"""

    gateway_outcome: ExperimentGatewayOutcome
    exposure: ExperimentExposure
    outcome_reason: str
    error_code: str | None
    routing_attempt_id: str | None
    stream_session_id: str | None
    quality_eligible: bool
    quality_succeeded: bool | None
    latency_ms: int | None
    settlement_disposition: ExperimentSettlementDisposition = "pending"
