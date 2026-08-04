"""策略快照公共管理 API 的严格请求与响应模型。

公共 DTO 不包含 DSL、AST 或 canonical expression。只有使用内部服务身份的拉取接口
会返回 wire artifact，并由路由直接构造受大小约束的 JSON 响应。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PolicySnapshotRequestModel(BaseModel):
    """快照写请求基类，保留未知字段供服务层映射稳定领域错误。

    Pydantic 若在路由执行前直接拒绝 extra，只能返回通用 ``infra.validation_failed``；
    这里临时接收后由服务层 fail closed，保证 tenant/actor 等夹带字段仍被拒绝且错误码稳定。
    """

    model_config = ConfigDict(extra="allow")


class PublishPolicySnapshotRequest(PolicySnapshotRequestModel):
    """发布或回滚请求；变更原因由服务层执行 Unicode 与控制字符校验。"""

    reason: object = None


class PolicySnapshotActorInfo(BaseModel):
    """管理员历史中可展示的租户内操作者安全标识。"""

    user_id: str
    email: str
    display_name: str


class PolicySnapshotInfo(BaseModel):
    """已发布快照的公共摘要，不泄露策略表达式或完整制品。"""

    policy_snapshot_id: str
    snapshot_version: int
    schema_version: str
    operation: Literal["publish", "rollback"]
    content_hash: str
    artifact_hash: str
    policy_count: int
    change_reason: str
    approval_status: Literal["not_required"]
    previous_snapshot_version: int | None = None
    rollback_source_snapshot_version: int | None = None
    created_by: PolicySnapshotActorInfo
    created_at: datetime
    published_at: datetime


class PolicySnapshotCurrentResponse(BaseModel):
    """当前 head 与锁定候选摘要，供页面建立发布双 CAS。"""

    current_snapshot: PolicySnapshotInfo | None
    candidate_status: Literal["valid", "invalid"]
    candidate_content_hash: str | None = None
    candidate_policy_count: int | None = None
    candidate_matches_current: bool | None = None
    request_id: str
    trace_id: str


class PolicySnapshotListResponse(BaseModel):
    """按版本倒序返回当前租户历史，并携带稳定分页元数据。"""

    policy_snapshots: list[PolicySnapshotInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class PolicySnapshotMutationResponse(BaseModel):
    """发布、回滚或同 actor 幂等回放后的统一成功响应。"""

    policy_snapshot: PolicySnapshotInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
