"""定义 Gateway Cell 与 Workspace placement 的严格安全 DTO，边界是不接收内部拓扑或签名。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GatewayResponseModel(BaseModel):
    """所有平台 Gateway 响应拒绝未知字段，避免内部拓扑或签名意外外泄。"""

    model_config = ConfigDict(extra="forbid")


class GatewayCellSummary(GatewayResponseModel):
    """只暴露固定 Cell code、Region、容量带、健康枚举和公开水位。"""

    cell_code: str
    region_code: str
    capacity_class: Literal["standard", "high", "burst"]
    capacity_band: Literal["available", "busy", "full"]
    status: Literal["active", "draining", "unavailable"]
    health_status: Literal["healthy", "degraded", "unavailable"]
    release_watermark: int = Field(ge=0)
    config_watermark: int = Field(ge=0)
    revision: int = Field(ge=1)
    updated_at: datetime


class GatewayCellListResponse(GatewayResponseModel):
    """Cell 安全投影列表及请求关联标识。"""

    cells: tuple[GatewayCellSummary, ...]
    request_id: str
    trace_id: str


class GatewayPlacementUpdateRequest(GatewayResponseModel):
    """显式 placement 写意图；tenant/workspace owner 不允许由正文提供。"""

    primary_region_code: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9-]+$")
    primary_cell_code: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9-]+$")
    failover_policy: Literal["deny", "explicit"]
    failover_cell_code: str | None = Field(
        default=None, min_length=3, max_length=40, pattern=r"^[a-z0-9-]+$"
    )
    residency_policy_version: str = Field(min_length=1, max_length=80)
    reason_code: Literal["initial_placement", "capacity_rebalance", "regional_recovery"]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_failover(self) -> GatewayPlacementUpdateRequest:
        """冻结 deny/explicit 字段组合并禁止主备指向同一 Cell。"""

        # 主备字段组合必须与显式策略完全一致，避免空值被解释成隐式跨区灾备。
        if (self.failover_policy == "deny") != (self.failover_cell_code is None):
            raise ValueError("failover fields do not match policy")
        # 相同主备 Cell 不具备灾备语义，必须在进入服务层前拒绝。
        if self.failover_cell_code == self.primary_cell_code:
            raise ValueError("failover Cell must differ from primary")
        return self


class GatewayPlacementSummary(GatewayResponseModel):
    """不含 tenant、签名和内部地址的当前 placement 投影。"""

    workspace_id: str
    primary_region_code: str
    primary_cell_code: str
    failover_policy: Literal["deny", "explicit"]
    failover_cell_code: str | None
    residency_policy_version: str
    revision: int = Field(ge=1)
    snapshot_generation: int = Field(ge=1)
    updated_at: datetime


class GatewayPlacementResponse(GatewayResponseModel):
    """返回公开的 CAS revision、签名 generation 与幂等重放状态，不包含签名正文。"""

    placement: GatewayPlacementSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
