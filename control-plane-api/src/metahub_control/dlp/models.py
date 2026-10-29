"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/dlp/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DlpRuleInfo(BaseModel):
    """DLP 规则 DTO。

    pattern 是管理员配置内容，只在 admin 规则管理 API 中返回；普通调用日志和 DLP 命中记录不会返回该字段，
    避免把敏感规则或命中文本扩散到排障链路。
    """

    dlp_rule_id: str
    tenant_id: str
    name: str
    rule_type: str
    pattern: str
    pattern_hash: str
    action: str
    # 检测方向是规则治理合同的一部分；历史规则固定为 input，避免升级后静默扫描模型输出。
    scan_scope: str
    status: str
    priority: int
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class DlpRuleListResponse(BaseModel):
    """DLP 规则列表响应，统一携带 request_id 和 trace_id 便于审计追踪。"""

    dlp_rules: list[DlpRuleInfo]
    request_id: str
    trace_id: str


class DlpRuleResponse(BaseModel):
    """DLP 规则写入后的单条响应，供控制台保存后局部刷新使用。"""

    dlp_rule: DlpRuleInfo
    request_id: str
    trace_id: str


class CreateDlpRuleRequest(BaseModel):
    """创建 DLP 规则的请求体。

    请求体不允许指定 tenant_id；服务层必须从登录态恢复当前租户，避免跨租户写入安全策略。
    """

    name: str | None = None
    rule_type: str | None = None
    pattern: str | None = None
    action: str | None = None
    # 省略时由服务层固定为 input，不能依赖数据库默认掩盖非法客户端值。
    scan_scope: str | None = None
    status: str | None = None
    priority: int | None = None
    description: str | None = None


class UpdateDlpRuleRequest(BaseModel):
    """更新 DLP 规则的请求体。

    M0 不支持修改 rule_type；如果管理员需要把关键词改成正则，应禁用旧规则后新建规则，避免历史命中解释漂移。
    """

    name: str | None = None
    pattern: str | None = None
    action: str | None = None
    # PATCH 省略时保持原方向；显式提交时只接受 input/output/both。
    scan_scope: str | None = None
    status: str | None = None
    priority: int | None = None
    description: str | None = None
