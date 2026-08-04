"""配置迁移的公开 DTO。

本模块只描述已经脱敏的导出清单、导入报告和一次性新 Virtual Key 响应。持久报告、审计和
Webhook 永远不能复用含 ``plain_value`` 的 DTO，避免一次性密钥被普通序列化路径复制。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ImportRowError(BaseModel):
    """一条不回显输入值的行级错误。

    ``row`` 在各对象集合内从 1 开始；``field`` 只能使用服务端白名单路径，不能复制攻击者构造的
    JSON key。调用方应使用 ``code`` 做机器判断，而不是解析展示文案。
    """

    object_type: str
    row: int
    field: str
    code: str

    model_config = ConfigDict(extra="forbid")


class ImportCounts(BaseModel):
    """记录 create-only merge 的创建、复用和新密钥数量。"""

    providers_created: int = 0
    providers_unchanged: int = 0
    models_created: int = 0
    models_unchanged: int = 0
    aliases_created: int = 0
    aliases_unchanged: int = 0
    virtual_keys_created: int = 0
    virtual_keys_unchanged: int = 0
    generated_virtual_keys: int = 0
    disabled_virtual_keys: int = 0

    model_config = ConfigDict(extra="forbid")


class ImportReport(BaseModel):
    """导入预检、提交或精确重试的安全报告。"""

    status: str
    valid: bool
    applied: bool
    manifest_sha256: str
    schema_version: str
    counts: ImportCounts
    error_count: int
    errors_truncated: bool
    errors: list[ImportRowError]
    config_import_id: str | None
    request_id: str
    trace_id: str

    model_config = ConfigDict(extra="forbid")


class GeneratedVirtualKey(BaseModel):
    """首次成功响应中一次性交付的新 Virtual Key。

    安全约束：该 DTO 不能进入数据库、AuditLog、Webhook 或普通 logger。CLI 必须把该集合从普通
    ImportReport 中剥离并写入显式的私有文件；精确重试返回空集合。
    """

    virtual_key_id: str
    name: str
    environment: str
    key_prefix: str
    plain_value: str

    model_config = ConfigDict(extra="forbid")


class ConfigImportResponse(BaseModel):
    """Control API 导入响应，分离安全报告与一次性秘密。"""

    report: ImportReport
    generated_virtual_keys: list[GeneratedVirtualKey]
    request_id: str
    trace_id: str

    model_config = ConfigDict(extra="forbid")


class ProviderExport(BaseModel):
    """Provider 的可迁移字段白名单，明确排除 Credential 和健康运行状态。"""

    provider_code: str
    name: str
    provider_type: str
    base_url: str
    auth_type: str
    description: str | None
    maintenance_note: str | None
    status: str

    model_config = ConfigDict(extra="forbid")


class ModelExport(BaseModel):
    """Model 的业务引用、能力和价格白名单，不携带源数据库 ID。"""

    internal_model_id: str
    provider_code: str
    provider_model_id: str
    display_name: str | None
    capabilities: dict[str, bool]
    context_window: int
    input_price_per_1k: float
    output_price_per_1k: float
    currency: str
    price_schema_version: str | None
    description: str | None
    status: str

    model_config = ConfigDict(extra="forbid")


class QualityCostConfigExport(BaseModel):
    """Alias 的五字段质量成本配置快照。"""

    quality_weight_bps: int
    cost_weight_bps: int
    latency_weight_bps: int
    quality_floor_bps: int
    minimum_sample_count: int

    model_config = ConfigDict(extra="forbid")


class AliasExport(BaseModel):
    """Alias、候选业务引用、Fallback 条件和路由模式白名单。"""

    alias: str
    environment: str
    default_internal_model_id: str
    fallback_internal_model_id: str | None
    fallback_conditions: list[str]
    routing_mode: str
    quality_cost_config: QualityCostConfigExport
    description: str | None
    status: str

    model_config = ConfigDict(extra="forbid")


class VirtualKeyExport(BaseModel):
    """Virtual Key 可迁移元数据；明文、hash、prefix 和源 ID 均不属于该 DTO。"""

    name: str
    environment: str
    description: str | None
    project_code: str | None
    application_code: str | None
    status: str
    rpm_limit: int | None
    tpm_limit: int | None
    concurrency_limit: int | None
    rate_limit_status: str | None

    model_config = ConfigDict(extra="forbid")


class ConfigExport(BaseModel):
    """可跨租户迁移的 v1 配置清单。

    四个集合中的引用只允许使用稳定业务键。``source_tenant_id`` 说明来源，但导入服务不得用它
    选择目标租户；目标租户只能来自 Bearer Session。
    """

    schema_version: str
    source_tenant_id: str
    exported_at: datetime
    providers: list[ProviderExport]
    models: list[ModelExport]
    aliases: list[AliasExport]
    virtual_keys: list[VirtualKeyExport]

    model_config = ConfigDict(extra="forbid")


class ConfigExportResponse(BaseModel):
    """配置导出 HTTP envelope，统一携带请求和链路 ID。"""

    config_export: ConfigExport
    config_export_id: str
    manifest_sha256: str
    request_id: str
    trace_id: str

    model_config = ConfigDict(extra="forbid")
