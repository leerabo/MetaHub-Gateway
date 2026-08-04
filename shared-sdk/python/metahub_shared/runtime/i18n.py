"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/i18n.py
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


class LocaleInfo(BaseModel):
    """控制台支持的语言信息。

    该对象是全局产品元数据，不属于租户业务数据；用户选择结果才会写入带 tenant_id 的 user_preferences。
    """

    locale: str
    label: str
    native_label: str
    default: bool = False


@dataclass(frozen=True)
class I18nMessage:
    """代码内静态 i18n 文案。

    M0 不做租户自定义翻译和在线热更新，因此消息只保存在代码字典中，不写入业务数据库。
    """

    key: str
    locale: str
    text: str
    namespace: str


# 支持语言、默认语言和静态文案注册表集中定义，确保后端和前端对 locale 白名单保持同一口径。
DEFAULT_LOCALE = "zh-CN"
FALLBACK_LOCALE = "zh-CN"
SUPPORTED_LOCALES = ("zh-CN", "en-US")
AVAILABLE_LOCALES = (
    LocaleInfo(locale="zh-CN", label="简体中文", native_label="简体中文", default=True),
    LocaleInfo(locale="en-US", label="English", native_label="English", default=False),
)
I18N_MESSAGES: tuple[I18nMessage, ...] = (
    I18nMessage("error.auth.token_missing.label", "zh-CN", "需要登录", "errors"),
    I18nMessage("error.auth.token_missing.label", "en-US", "Sign in required", "errors"),
    I18nMessage("error.auth.permission_denied.label", "zh-CN", "权限不足", "errors"),
    I18nMessage("error.auth.permission_denied.label", "en-US", "Permission denied", "errors"),
    I18nMessage("error.rbac.role_not_found.label", "zh-CN", "角色不存在", "errors"),
    I18nMessage("error.rbac.role_not_found.label", "en-US", "Role not found", "errors"),
    I18nMessage("error.rbac.payload_invalid.label", "zh-CN", "权限请求不合法", "errors"),
    I18nMessage("error.rbac.payload_invalid.label", "en-US", "Invalid permission payload", "errors"),
    I18nMessage("error.rbac.permission_not_grantable.label", "zh-CN", "权限超出角色边界", "errors"),
    I18nMessage("error.rbac.permission_not_grantable.label", "en-US", "Permission is not grantable", "errors"),
    I18nMessage("error.rbac.role_immutable.label", "zh-CN", "角色权限不可修改", "errors"),
    I18nMessage("error.rbac.role_immutable.label", "en-US", "Role permissions are immutable", "errors"),
    I18nMessage("error.rbac.revision_conflict.label", "zh-CN", "权限版本已变化", "errors"),
    I18nMessage("error.rbac.revision_conflict.label", "en-US", "Permission revision changed", "errors"),
    I18nMessage("error.rbac.matrix_integrity_failed.label", "zh-CN", "权限矩阵完整性异常", "errors"),
    I18nMessage("error.rbac.matrix_integrity_failed.label", "en-US", "Permission matrix integrity failed", "errors"),
    I18nMessage("error.rbac.unavailable.label", "zh-CN", "权限矩阵暂不可用", "errors"),
    I18nMessage("error.rbac.unavailable.label", "en-US", "Permission matrix unavailable", "errors"),
    # Prompt 模板标签只映射固定目录状态，绝不拼接名称、正文、变量或跨租户目标。
    I18nMessage("error.prompt_template.not_found.label", "zh-CN", "Prompt 模板不存在", "errors"),
    I18nMessage("error.prompt_template.not_found.label", "en-US", "Prompt template not found", "errors"),
    I18nMessage("error.prompt_template.request_invalid.label", "zh-CN", "模板请求不合法", "errors"),
    I18nMessage("error.prompt_template.request_invalid.label", "en-US", "Invalid template request", "errors"),
    I18nMessage("error.prompt_template.definition_invalid.label", "zh-CN", "模板定义不合法", "errors"),
    I18nMessage("error.prompt_template.definition_invalid.label", "en-US", "Invalid template definition", "errors"),
    I18nMessage("error.prompt_template.binding_not_found.label", "zh-CN", "模板绑定不存在", "errors"),
    I18nMessage("error.prompt_template.binding_not_found.label", "en-US", "Template binding not found", "errors"),
    I18nMessage("error.prompt_template.variables_invalid.label", "zh-CN", "模板变量不合法", "errors"),
    I18nMessage("error.prompt_template.variables_invalid.label", "en-US", "Invalid template variables", "errors"),
    I18nMessage("error.prompt_template.name_conflict.label", "zh-CN", "模板名称已存在", "errors"),
    I18nMessage("error.prompt_template.name_conflict.label", "en-US", "Template name already exists", "errors"),
    I18nMessage("error.prompt_template.revision_conflict.label", "zh-CN", "模板版本已变化", "errors"),
    I18nMessage("error.prompt_template.revision_conflict.label", "en-US", "Template revision changed", "errors"),
    I18nMessage("error.prompt_template.idempotency_conflict.label", "zh-CN", "模板请求冲突", "errors"),
    I18nMessage("error.prompt_template.idempotency_conflict.label", "en-US", "Template request conflict", "errors"),
    I18nMessage("error.prompt_template.disabled.label", "zh-CN", "模板已停用", "errors"),
    I18nMessage("error.prompt_template.disabled.label", "en-US", "Template disabled", "errors"),
    I18nMessage("error.prompt_template.version_conflict.label", "zh-CN", "模板版本已过期", "errors"),
    I18nMessage("error.prompt_template.version_conflict.label", "en-US", "Template version changed", "errors"),
    I18nMessage("error.prompt_template.not_applicable.label", "zh-CN", "模板不适用于当前项目", "errors"),
    I18nMessage("error.prompt_template.not_applicable.label", "en-US", "Template not applicable", "errors"),
    I18nMessage("error.prompt_template.definition_unavailable.label", "zh-CN", "模板定义不可用", "errors"),
    I18nMessage("error.prompt_template.definition_unavailable.label", "en-US", "Template definition unavailable", "errors"),
    I18nMessage("error.prompt_template.integrity_failed.label", "zh-CN", "模板完整性异常", "errors"),
    I18nMessage("error.prompt_template.integrity_failed.label", "en-US", "Template integrity failed", "errors"),
    I18nMessage("error.prompt_template.unavailable.label", "zh-CN", "模板服务暂不可用", "errors"),
    I18nMessage("error.prompt_template.unavailable.label", "en-US", "Template service unavailable", "errors"),
    # Chat 标签只描述固定状态，不把会话正文、执行 grant 或上游错误拼入本地化输出。
    I18nMessage("error.chat.request_invalid.label", "zh-CN", "Chat 请求不合法", "errors"),
    I18nMessage("error.chat.request_invalid.label", "en-US", "Invalid Chat request", "errors"),
    I18nMessage("error.chat.session_not_found.label", "zh-CN", "Chat 会话不存在", "errors"),
    I18nMessage("error.chat.session_not_found.label", "en-US", "Chat session not found", "errors"),
    I18nMessage("error.chat.session_expired.label", "zh-CN", "Chat 会话已过期", "errors"),
    I18nMessage("error.chat.session_expired.label", "en-US", "Chat session expired", "errors"),
    I18nMessage("error.chat.session_cleared.label", "zh-CN", "Chat 会话已清空", "errors"),
    I18nMessage("error.chat.session_cleared.label", "en-US", "Chat session cleared", "errors"),
    I18nMessage("error.chat.session_busy.label", "zh-CN", "Chat 会话正在处理", "errors"),
    I18nMessage("error.chat.session_busy.label", "en-US", "Chat session busy", "errors"),
    I18nMessage("error.chat.message_duplicate.label", "zh-CN", "Chat 消息已提交", "errors"),
    I18nMessage("error.chat.message_duplicate.label", "en-US", "Chat message already submitted", "errors"),
    I18nMessage("error.chat.project_unavailable.label", "zh-CN", "Chat 项目不可用", "errors"),
    I18nMessage("error.chat.project_unavailable.label", "en-US", "Chat project unavailable", "errors"),
    I18nMessage("error.chat.model_unavailable.label", "zh-CN", "Chat 模型不可用", "errors"),
    I18nMessage("error.chat.model_unavailable.label", "en-US", "Chat model unavailable", "errors"),
    I18nMessage("error.chat.virtual_key_unavailable.label", "zh-CN", "Chat 执行身份不可用", "errors"),
    I18nMessage("error.chat.virtual_key_unavailable.label", "en-US", "Chat execution identity unavailable", "errors"),
    I18nMessage("error.chat.execution_invalid.label", "zh-CN", "Chat 执行授权无效", "errors"),
    I18nMessage("error.chat.execution_invalid.label", "en-US", "Invalid Chat execution grant", "errors"),
    I18nMessage("error.chat.gateway_unavailable.label", "zh-CN", "Chat 网关暂不可用", "errors"),
    I18nMessage("error.chat.gateway_unavailable.label", "en-US", "Chat Gateway unavailable", "errors"),
    I18nMessage("error.chat.response_invalid.label", "zh-CN", "Chat 响应不受支持", "errors"),
    I18nMessage("error.chat.response_invalid.label", "en-US", "Unsupported Chat response", "errors"),
    # 配置迁移标签只描述稳定边界，不回显文件名、字段值、URL、Key 或底层异常。
    I18nMessage("error.config_transfer.query_invalid.label", "zh-CN", "配置迁移查询不合法", "errors"),
    I18nMessage("error.config_transfer.query_invalid.label", "en-US", "Invalid transfer query", "errors"),
    I18nMessage("error.config_transfer.body_invalid.label", "zh-CN", "配置迁移正文不合法", "errors"),
    I18nMessage("error.config_transfer.body_invalid.label", "en-US", "Invalid transfer body", "errors"),
    I18nMessage("error.config_transfer.body_too_large.label", "zh-CN", "配置迁移正文超限", "errors"),
    I18nMessage("error.config_transfer.body_too_large.label", "en-US", "Transfer body too large", "errors"),
    I18nMessage("error.config_transfer.schema_unsupported.label", "zh-CN", "配置版本不受支持", "errors"),
    I18nMessage("error.config_transfer.schema_unsupported.label", "en-US", "Unsupported config schema", "errors"),
    I18nMessage("error.config_transfer.import_sensitive_field.label", "zh-CN", "导入文件含凭据字段", "errors"),
    I18nMessage("error.config_transfer.import_sensitive_field.label", "en-US", "Credential field in import", "errors"),
    I18nMessage("error.config_transfer.import_invalid.label", "zh-CN", "导入配置不合法", "errors"),
    I18nMessage("error.config_transfer.import_invalid.label", "en-US", "Invalid configuration import", "errors"),
    I18nMessage("error.config_transfer.import_request_conflict.label", "zh-CN", "导入请求冲突", "errors"),
    I18nMessage("error.config_transfer.import_request_conflict.label", "en-US", "Import request conflict", "errors"),
    I18nMessage("error.config_transfer.import_unavailable.label", "zh-CN", "配置导入暂不可用", "errors"),
    I18nMessage("error.config_transfer.import_unavailable.label", "en-US", "Configuration import unavailable", "errors"),
    I18nMessage("error.config_transfer.export_state_invalid.label", "zh-CN", "当前配置无法安全导出", "errors"),
    I18nMessage("error.config_transfer.export_state_invalid.label", "en-US", "Configuration is not exportable", "errors"),
    I18nMessage("error.config_transfer.export_request_conflict.label", "zh-CN", "导出请求冲突", "errors"),
    I18nMessage("error.config_transfer.export_request_conflict.label", "en-US", "Export request conflict", "errors"),
    I18nMessage("error.config_transfer.export_unavailable.label", "zh-CN", "配置导出暂不可用", "errors"),
    I18nMessage("error.config_transfer.export_unavailable.label", "en-US", "Configuration export unavailable", "errors"),
    I18nMessage("error.invitation.create_invalid.label", "zh-CN", "邀请信息不合法", "errors"),
    I18nMessage("error.invitation.create_invalid.label", "en-US", "Invalid invitation", "errors"),
    I18nMessage("error.invitation.role_invalid.label", "zh-CN", "邀请角色不合法", "errors"),
    I18nMessage("error.invitation.role_invalid.label", "en-US", "Invalid invitation role", "errors"),
    I18nMessage("error.invitation.user_exists.label", "zh-CN", "用户已存在", "errors"),
    I18nMessage("error.invitation.user_exists.label", "en-US", "User already exists", "errors"),
    I18nMessage("error.invitation.pending_exists.label", "zh-CN", "邀请已存在", "errors"),
    I18nMessage("error.invitation.pending_exists.label", "en-US", "Invitation already exists", "errors"),
    I18nMessage("error.invitation.not_available.label", "zh-CN", "邀请已失效", "errors"),
    I18nMessage("error.invitation.not_available.label", "en-US", "Invitation unavailable", "errors"),
    I18nMessage("error.invitation.accept_invalid.label", "zh-CN", "加入信息不合法", "errors"),
    I18nMessage("error.invitation.accept_invalid.label", "en-US", "Invalid acceptance details", "errors"),
    I18nMessage("error.invitation.unavailable.label", "zh-CN", "邀请服务暂不可用", "errors"),
    I18nMessage("error.invitation.unavailable.label", "en-US", "Invitation service unavailable", "errors"),
    I18nMessage("error.infra.method_not_allowed.label", "zh-CN", "请求方法不受支持", "errors"),
    I18nMessage(
        "error.infra.method_not_allowed.label", "en-US", "Method not allowed", "errors"
    ),
    I18nMessage("error.infra.validation_failed.label", "zh-CN", "请求参数不合法", "errors"),
    I18nMessage(
        "error.infra.validation_failed.label", "en-US", "Request payload is invalid", "errors"
    ),
    I18nMessage("error.user_preference.locale_invalid.label", "zh-CN", "语言不受支持", "errors"),
    I18nMessage(
        "error.user_preference.locale_invalid.label", "en-US", "Unsupported language", "errors"
    ),
    I18nMessage("error.user_preference.payload_invalid.label", "zh-CN", "偏好请求不合法", "errors"),
    I18nMessage(
        "error.user_preference.payload_invalid.label",
        "en-US",
        "Preference request is invalid",
        "errors",
    ),
    I18nMessage("error.user_preference.unavailable.label", "zh-CN", "用户偏好暂不可用", "errors"),
    I18nMessage(
        "error.user_preference.unavailable.label",
        "en-US",
        "User preferences are unavailable",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_mode_invalid.label", "zh-CN", "路由模式不合法", "errors"
    ),
    I18nMessage(
        "error.model_alias.routing_mode_invalid.label", "en-US", "Invalid routing mode", "errors"
    ),
    I18nMessage(
        "error.model_alias.quality_cost_settings_invalid.label",
        "zh-CN",
        "质量成本配置不合法",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.quality_cost_settings_invalid.label",
        "en-US",
        "Invalid quality-cost settings",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_version_invalid.label",
        "zh-CN",
        "路由策略版本不合法",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_version_invalid.label",
        "en-US",
        "Invalid routing policy version",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_version_conflict.label",
        "zh-CN",
        "路由策略版本冲突",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_version_conflict.label",
        "en-US",
        "Routing policy version conflict",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_request_conflict.label",
        "zh-CN",
        "路由策略请求冲突",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_request_conflict.label",
        "en-US",
        "Routing policy request conflict",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_update_failed.label",
        "zh-CN",
        "路由策略更新失败",
        "errors",
    ),
    I18nMessage(
        "error.model_alias.routing_policy_update_failed.label",
        "en-US",
        "Routing policy update failed",
        "errors",
    ),
    I18nMessage("error.model_quality_score.query_invalid.label", "zh-CN", "评分查询不合法", "errors"),
    I18nMessage("error.model_quality_score.query_invalid.label", "en-US", "Invalid score query", "errors"),
    I18nMessage("error.model_quality_score.input_invalid.label", "zh-CN", "人工评分不合法", "errors"),
    I18nMessage("error.model_quality_score.input_invalid.label", "en-US", "Invalid manual score", "errors"),
    I18nMessage("error.model_quality_score.version_invalid.label", "zh-CN", "评分版本不合法", "errors"),
    I18nMessage("error.model_quality_score.version_invalid.label", "en-US", "Invalid score version", "errors"),
    I18nMessage("error.model_quality_score.not_found.label", "zh-CN", "模型评分不存在", "errors"),
    I18nMessage("error.model_quality_score.not_found.label", "en-US", "Model quality score not found", "errors"),
    I18nMessage("error.model_quality_score.version_conflict.label", "zh-CN", "评分版本冲突", "errors"),
    I18nMessage("error.model_quality_score.version_conflict.label", "en-US", "Score version conflict", "errors"),
    I18nMessage("error.model_quality_score.request_conflict.label", "zh-CN", "评分请求冲突", "errors"),
    I18nMessage("error.model_quality_score.request_conflict.label", "en-US", "Score request conflict", "errors"),
    I18nMessage("error.model_quality_score.unavailable.label", "zh-CN", "模型评分暂不可用", "errors"),
    I18nMessage("error.model_quality_score.unavailable.label", "en-US", "Model quality scores unavailable", "errors"),
    I18nMessage("error.model_quality_score.update_failed.label", "zh-CN", "模型评分更新失败", "errors"),
    I18nMessage("error.model_quality_score.update_failed.label", "en-US", "Model quality score update failed", "errors"),
    I18nMessage("error.feedback.request_not_found.label", "zh-CN", "源调用不存在", "errors"),
    I18nMessage("error.feedback.request_not_found.label", "en-US", "Source request not found", "errors"),
    I18nMessage("error.feedback.request_not_feedbackable.label", "zh-CN", "源调用不可反馈", "errors"),
    I18nMessage("error.feedback.request_not_feedbackable.label", "en-US", "Source request cannot be rated", "errors"),
    I18nMessage("error.feedback.already_exists.label", "zh-CN", "反馈已存在", "errors"),
    I18nMessage("error.feedback.already_exists.label", "en-US", "Feedback already exists", "errors"),
    I18nMessage("error.feedback.request_conflict.label", "zh-CN", "反馈请求冲突", "errors"),
    I18nMessage("error.feedback.request_conflict.label", "en-US", "Feedback request conflict", "errors"),
    I18nMessage("error.feedback.input_invalid.label", "zh-CN", "反馈内容不合法", "errors"),
    I18nMessage("error.feedback.input_invalid.label", "en-US", "Invalid feedback payload", "errors"),
    I18nMessage("error.feedback.query_invalid.label", "zh-CN", "反馈查询不合法", "errors"),
    I18nMessage("error.feedback.query_invalid.label", "en-US", "Invalid feedback query", "errors"),
    I18nMessage("error.feedback.unavailable.label", "zh-CN", "反馈服务暂不可用", "errors"),
    I18nMessage("error.feedback.unavailable.label", "en-US", "Feedback unavailable", "errors"),
    I18nMessage("error.feedback.record_failed.label", "zh-CN", "反馈记录失败", "errors"),
    I18nMessage("error.feedback.record_failed.label", "en-US", "Feedback record failed", "errors"),
    I18nMessage("error.budget.rule_conflict.label", "zh-CN", "预算规则已存在", "errors"),
    I18nMessage("error.budget.rule_conflict.label", "en-US", "Budget rule already exists", "errors"),
    I18nMessage("error.budget.scope_mismatch.label", "zh-CN", "预算作用域归属冲突", "errors"),
    I18nMessage("error.budget.scope_mismatch.label", "en-US", "Budget scope mismatch", "errors"),
    # 成本异常公开错误提供中英文短标签；code 与英文 message 仍保持稳定 API 契约。
    I18nMessage(
        "error.cost_anomaly.query_invalid.label", "zh-CN", "成本异常查询不合法", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.query_invalid.label", "en-US", "Invalid cost anomaly query", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.not_found.label", "zh-CN", "成本异常资源不存在", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.not_found.label", "en-US", "Cost anomaly not found", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.media_type_invalid.label", "zh-CN", "成本异常请求格式不合法", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.media_type_invalid.label", "en-US", "Invalid action media type", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.action_invalid.label", "zh-CN", "成本异常处置不合法", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.action_invalid.label", "en-US", "Invalid cost anomaly action", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.action_conflict.label", "zh-CN", "成本异常状态冲突", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.action_conflict.label", "en-US", "Cost anomaly action conflict", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.idempotency_conflict.label", "zh-CN", "成本异常幂等键冲突", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.idempotency_conflict.label", "en-US", "Cost anomaly idempotency conflict", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_not_applicable.label",
        "zh-CN",
        "临时限流不适用",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_not_applicable.label",
        "en-US",
        "Temporary rate limit not applicable",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_active.label", "zh-CN", "临时限流已生效", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_active.label", "en-US", "Temporary rate limit active", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_expired.label", "zh-CN", "异常处置窗口已过期", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_expired.label", "en-US", "Mitigation window expired", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_key_unavailable.label",
        "zh-CN",
        "异常 Key 暂不可处置",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_key_unavailable.label",
        "en-US",
        "Virtual Key unavailable for mitigation",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_no_effect.label",
        "zh-CN",
        "临时限流未实际收紧",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_no_effect.label",
        "en-US",
        "Temporary rate limit has no effect",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_not_active.label", "zh-CN", "临时限流未生效", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.mitigation_not_active.label",
        "en-US",
        "Temporary rate limit not active",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.unavailable.label", "zh-CN", "成本异常服务暂不可用", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.unavailable.label", "en-US", "Cost anomalies unavailable", "errors"
    ),
    I18nMessage(
        "error.cost_anomaly.detector_unavailable.label",
        "zh-CN",
        "成本异常检测暂不可用",
        "errors",
    ),
    I18nMessage(
        "error.cost_anomaly.detector_unavailable.label",
        "en-US",
        "Cost anomaly detection unavailable",
        "errors",
    ),
    I18nMessage("error.finops.query_invalid.label", "zh-CN", "账本查询不合法", "errors"),
    I18nMessage("error.finops.query_invalid.label", "en-US", "Invalid ledger query", "errors"),
    I18nMessage("error.finops.adjustment_invalid.label", "zh-CN", "财务调整不合法", "errors"),
    I18nMessage("error.finops.adjustment_invalid.label", "en-US", "Invalid FinOps adjustment", "errors"),
    I18nMessage("error.finops.amount_invalid.label", "zh-CN", "财务金额不合法", "errors"),
    I18nMessage("error.finops.amount_invalid.label", "en-US", "Invalid FinOps amount", "errors"),
    I18nMessage("error.finops.entry_not_found.label", "zh-CN", "账本分录不存在", "errors"),
    I18nMessage("error.finops.entry_not_found.label", "en-US", "Ledger entry not found", "errors"),
    I18nMessage("error.finops.request_not_found.label", "zh-CN", "关联计费请求不存在", "errors"),
    I18nMessage("error.finops.request_not_found.label", "en-US", "Related billed request not found", "errors"),
    I18nMessage("error.finops.reversal_not_allowed.label", "zh-CN", "账本分录不可冲正", "errors"),
    I18nMessage("error.finops.reversal_not_allowed.label", "en-US", "Ledger entry cannot be reversed", "errors"),
    I18nMessage("error.finops.reversal_exceeds_remaining.label", "zh-CN", "冲正金额超过余额", "errors"),
    I18nMessage("error.finops.reversal_exceeds_remaining.label", "en-US", "Reversal exceeds remaining amount", "errors"),
    I18nMessage("error.finops.idempotency_conflict.label", "zh-CN", "财务幂等键冲突", "errors"),
    I18nMessage("error.finops.idempotency_conflict.label", "en-US", "FinOps idempotency conflict", "errors"),
    I18nMessage("error.finops.ledger_unavailable.label", "zh-CN", "账本服务暂不可用", "errors"),
    I18nMessage("error.finops.ledger_unavailable.label", "en-US", "FinOps ledger unavailable", "errors"),
    I18nMessage("error.finops.usage_record_unavailable.label", "zh-CN", "用量账本记录暂不可用", "errors"),
    I18nMessage("error.finops.usage_record_unavailable.label", "en-US", "Usage ledger recording unavailable", "errors"),
    I18nMessage("error.finops.adjustment_failed.label", "zh-CN", "财务调整记录失败", "errors"),
    I18nMessage("error.finops.adjustment_failed.label", "en-US", "FinOps adjustment failed", "errors"),
    I18nMessage("error.gateway.no_route_available.label", "zh-CN", "当前没有可用路由", "errors"),
    I18nMessage("error.gateway.no_route_available.label", "en-US", "No route available", "errors"),
    I18nMessage(
        "error.gateway.routing_policy_invalid.label", "zh-CN", "路由策略配置损坏", "errors"
    ),
    I18nMessage(
        "error.gateway.routing_policy_invalid.label", "en-US", "Invalid routing policy", "errors"
    ),
    I18nMessage(
        "error.gateway.routing_snapshot_unavailable.label", "zh-CN", "路由快照暂不可用", "errors"
    ),
    I18nMessage(
        "error.gateway.routing_snapshot_unavailable.label",
        "en-US",
        "Routing snapshot unavailable",
        "errors",
    ),
    I18nMessage(
        "error.gateway.routing_snapshot_invalid.label", "zh-CN", "路由快照事实损坏", "errors"
    ),
    I18nMessage(
        "error.gateway.routing_snapshot_invalid.label",
        "en-US",
        "Invalid routing snapshot",
        "errors",
    ),
    I18nMessage(
        "error.gateway.routing_decision_unavailable.label", "zh-CN", "路由决策暂不可记录", "errors"
    ),
    I18nMessage(
        "error.gateway.routing_decision_unavailable.label",
        "en-US",
        "Routing decision unavailable",
        "errors",
    ),
    # 调用日志高级查询与个人保存筛选提供中英文短标签；code 与 ErrorSpec 英文详情继续保持
    # 稳定，前端只把这些静态标签用于展示，不能据此替代后端权限或错误分支判断。
    I18nMessage("error.request_log.not_found.label", "zh-CN", "调用日志不存在", "errors"),
    I18nMessage("error.request_log.not_found.label", "en-US", "Request log not found", "errors"),
    I18nMessage("error.request_log.query_invalid.label", "zh-CN", "调用日志查询不合法", "errors"),
    I18nMessage(
        "error.request_log.query_invalid.label",
        "en-US",
        "Invalid request log query",
        "errors",
    ),
    I18nMessage("error.request_log.status_invalid.label", "zh-CN", "调用日志状态不合法", "errors"),
    I18nMessage(
        "error.request_log.status_invalid.label",
        "en-US",
        "Invalid request log status",
        "errors",
    ),
    I18nMessage("error.request_log.sort_invalid.label", "zh-CN", "调用日志排序不合法", "errors"),
    I18nMessage(
        "error.request_log.sort_invalid.label",
        "en-US",
        "Invalid request log sort",
        "errors",
    ),
    I18nMessage(
        "error.request_log.pagination_invalid.label",
        "zh-CN",
        "调用日志分页不合法",
        "errors",
    ),
    I18nMessage(
        "error.request_log.pagination_invalid.label",
        "en-US",
        "Invalid request log pagination",
        "errors",
    ),
    I18nMessage(
        "error.request_log.query_unavailable.label",
        "zh-CN",
        "调用日志查询暂不可用",
        "errors",
    ),
    I18nMessage(
        "error.request_log.query_unavailable.label",
        "en-US",
        "Request log query unavailable",
        "errors",
    ),
    I18nMessage(
        "error.request_log.query_audit_unavailable.label",
        "zh-CN",
        "调用日志查询审计暂不可用",
        "errors",
    ),
    I18nMessage(
        "error.request_log.query_audit_unavailable.label",
        "en-US",
        "Request log query audit unavailable",
        "errors",
    ),
    I18nMessage(
        "error.request_log.query_event_unavailable.label",
        "zh-CN",
        "调用日志查询事件暂不可投递",
        "errors",
    ),
    I18nMessage(
        "error.request_log.query_event_unavailable.label",
        "en-US",
        "Request log query event unavailable",
        "errors",
    ),
    I18nMessage("error.saved_filter.not_found.label", "zh-CN", "保存筛选不存在", "errors"),
    I18nMessage("error.saved_filter.not_found.label", "en-US", "Saved filter not found", "errors"),
    I18nMessage("error.saved_filter.invalid.label", "zh-CN", "保存筛选不合法", "errors"),
    I18nMessage("error.saved_filter.invalid.label", "en-US", "Invalid saved filter", "errors"),
    I18nMessage("error.saved_filter.name_conflict.label", "zh-CN", "保存筛选名称冲突", "errors"),
    I18nMessage(
        "error.saved_filter.name_conflict.label",
        "en-US",
        "Saved filter name conflict",
        "errors",
    ),
    I18nMessage(
        "error.saved_filter.limit_exceeded.label",
        "zh-CN",
        "保存筛选数量已达上限",
        "errors",
    ),
    I18nMessage(
        "error.saved_filter.limit_exceeded.label",
        "en-US",
        "Saved filter limit reached",
        "errors",
    ),
    I18nMessage("error.saved_filter.version_conflict.label", "zh-CN", "保存筛选版本冲突", "errors"),
    I18nMessage(
        "error.saved_filter.version_conflict.label",
        "en-US",
        "Saved filter version conflict",
        "errors",
    ),
    I18nMessage("error.saved_filter.unavailable.label", "zh-CN", "保存筛选暂不可用", "errors"),
    I18nMessage(
        "error.saved_filter.unavailable.label",
        "en-US",
        "Saved filters unavailable",
        "errors",
    ),
    # 请求解释错误提供稳定中英文短标签，详情仍由统一 ErrorSpec 控制，不能混入底层异常正文。
    I18nMessage("error.request_explanation.not_found.label", "zh-CN", "请求解释不存在", "errors"),
    I18nMessage("error.request_explanation.not_found.label", "en-US", "Request explanation not found", "errors"),
    I18nMessage("error.request_explanation.request_id_invalid.label", "zh-CN", "源请求标识不合法", "errors"),
    I18nMessage("error.request_explanation.request_id_invalid.label", "en-US", "Invalid source request id", "errors"),
    I18nMessage("error.request_explanation.request_invalid.label", "zh-CN", "解释请求不合法", "errors"),
    I18nMessage("error.request_explanation.request_invalid.label", "en-US", "Invalid explanation request", "errors"),
    I18nMessage("error.request_explanation.unavailable.label", "zh-CN", "请求解释暂不可用", "errors"),
    I18nMessage("error.request_explanation.unavailable.label", "en-US", "Request explanation unavailable", "errors"),
    I18nMessage("error.request_explanation.audit_unavailable.label", "zh-CN", "解释访问审计暂不可用", "errors"),
    I18nMessage("error.request_explanation.audit_unavailable.label", "en-US", "Explanation audit unavailable", "errors"),
    I18nMessage("error.request_explanation.integrity_failed.label", "zh-CN", "解释证据完整性校验失败", "errors"),
    I18nMessage("error.request_explanation.integrity_failed.label", "en-US", "Explanation integrity failed", "errors"),
    I18nMessage("error.request_explanation.event_unavailable.label", "zh-CN", "解释查看事件暂不可投递", "errors"),
    I18nMessage("error.request_explanation.event_unavailable.label", "en-US", "Explanation event unavailable", "errors"),
    # 审计导出标签只描述稳定平台语义，不拼接筛选、CSV、操作者邮箱或底层异常。
    I18nMessage("error.audit_export.filter_invalid.label", "zh-CN", "审计导出筛选不合法", "errors"),
    I18nMessage("error.audit_export.filter_invalid.label", "en-US", "Invalid audit export filter", "errors"),
    I18nMessage("error.audit_export.request_conflict.label", "zh-CN", "审计导出请求冲突", "errors"),
    I18nMessage("error.audit_export.request_conflict.label", "en-US", "Audit export request conflict", "errors"),
    I18nMessage("error.audit_export.too_large.label", "zh-CN", "审计导出超过规模上限", "errors"),
    I18nMessage("error.audit_export.too_large.label", "en-US", "Audit export too large", "errors"),
    I18nMessage("error.audit_export.integrity_failed.label", "zh-CN", "审计导出完整性校验失败", "errors"),
    I18nMessage("error.audit_export.integrity_failed.label", "en-US", "Audit export integrity failed", "errors"),
    I18nMessage("error.audit_export.unavailable.label", "zh-CN", "审计导出暂不可用", "errors"),
    I18nMessage("error.audit_export.unavailable.label", "en-US", "Audit export unavailable", "errors"),
    # Provider 错误映射标签只描述稳定平台语义，不展示租户 pattern 或上游原始正文。
    I18nMessage("error.error_map.not_found.label", "zh-CN", "错误映射不存在", "errors"),
    I18nMessage("error.error_map.not_found.label", "en-US", "Error map not found", "errors"),
    I18nMessage("error.error_map.input_invalid.label", "zh-CN", "错误映射输入不合法", "errors"),
    I18nMessage("error.error_map.input_invalid.label", "en-US", "Invalid error map input", "errors"),
    I18nMessage("error.error_map.update_empty.label", "zh-CN", "错误映射更新为空", "errors"),
    I18nMessage("error.error_map.update_empty.label", "en-US", "Empty error map update", "errors"),
    I18nMessage("error.error_map.output_code_invalid.label", "zh-CN", "归一错误码不合法", "errors"),
    I18nMessage("error.error_map.output_code_invalid.label", "en-US", "Invalid normalized error code", "errors"),
    I18nMessage("error.error_map.pattern_invalid.label", "zh-CN", "错误模式不合法", "errors"),
    I18nMessage("error.error_map.pattern_invalid.label", "en-US", "Invalid error pattern", "errors"),
    I18nMessage("error.error_map.pattern_sensitive.label", "zh-CN", "错误模式含敏感信息", "errors"),
    I18nMessage("error.error_map.pattern_sensitive.label", "en-US", "Sensitive error pattern", "errors"),
    I18nMessage("error.error_map.conflict.label", "zh-CN", "错误映射规则冲突", "errors"),
    I18nMessage("error.error_map.conflict.label", "en-US", "Error map conflict", "errors"),
    I18nMessage("error.error_map.version_conflict.label", "zh-CN", "错误映射版本冲突", "errors"),
    I18nMessage("error.error_map.version_conflict.label", "en-US", "Error map version conflict", "errors"),
    I18nMessage("error.error_map.request_conflict.label", "zh-CN", "错误映射请求冲突", "errors"),
    I18nMessage("error.error_map.request_conflict.label", "en-US", "Error map request conflict", "errors"),
    I18nMessage("error.error_map.limit_exceeded.label", "zh-CN", "错误映射数量已达上限", "errors"),
    I18nMessage("error.error_map.limit_exceeded.label", "en-US", "Error map limit reached", "errors"),
    I18nMessage("error.error_map.unavailable.label", "zh-CN", "错误映射服务暂不可用", "errors"),
    I18nMessage("error.error_map.unavailable.label", "en-US", "Error map service unavailable", "errors"),
    I18nMessage("error.error_map.snapshot_unavailable.label", "zh-CN", "错误映射快照暂不可用", "errors"),
    I18nMessage("error.error_map.snapshot_unavailable.label", "en-US", "Error map snapshot unavailable", "errors"),
    I18nMessage("error.error_map.event_unavailable.label", "zh-CN", "错误映射事件暂不可用", "errors"),
    I18nMessage("error.error_map.event_unavailable.label", "en-US", "Error map event unavailable", "errors"),
    I18nMessage("error.error_map.tenant_unavailable.label", "zh-CN", "错误映射租户不可用", "errors"),
    I18nMessage("error.error_map.tenant_unavailable.label", "en-US", "Error map tenant unavailable", "errors"),
    I18nMessage("error.gateway.upstream_resource_not_found.label", "zh-CN", "上游资源不存在", "errors"),
    I18nMessage("error.gateway.upstream_resource_not_found.label", "en-US", "Upstream resource not found", "errors"),
    # 文件标签只描述固定错误阶段，禁止把文件名、正文、存储路径或 DLP 证据拼入展示文本。
    I18nMessage("error.file.multipart_invalid.label", "zh-CN", "文件上传格式不合法", "errors"),
    I18nMessage("error.file.multipart_invalid.label", "en-US", "Invalid file upload", "errors"),
    I18nMessage("error.file.name_invalid.label", "zh-CN", "文件名不合法", "errors"),
    I18nMessage("error.file.name_invalid.label", "en-US", "Invalid file name", "errors"),
    I18nMessage("error.file.unsupported_format.label", "zh-CN", "文件格式不受支持", "errors"),
    I18nMessage(
        "error.file.unsupported_format.label",
        "en-US",
        "Unsupported file format",
        "errors",
    ),
    I18nMessage("error.file.too_large.label", "zh-CN", "文件超过大小上限", "errors"),
    I18nMessage("error.file.too_large.label", "en-US", "File too large", "errors"),
    I18nMessage("error.file.processing_busy.label", "zh-CN", "文件处理繁忙", "errors"),
    I18nMessage("error.file.processing_busy.label", "en-US", "File processing busy", "errors"),
    I18nMessage("error.file.structure_invalid.label", "zh-CN", "文件结构不安全", "errors"),
    I18nMessage("error.file.structure_invalid.label", "en-US", "Invalid file structure", "errors"),
    I18nMessage("error.file.no_extractable_text.label", "zh-CN", "文件没有可提取文本", "errors"),
    I18nMessage("error.file.no_extractable_text.label", "en-US", "No extractable text", "errors"),
    I18nMessage("error.file.extract_limit_exceeded.label", "zh-CN", "文件解析内容超限", "errors"),
    I18nMessage(
        "error.file.extract_limit_exceeded.label",
        "en-US",
        "File extraction limit exceeded",
        "errors",
    ),
    I18nMessage("error.file.parse_failed.label", "zh-CN", "文件解析失败", "errors"),
    I18nMessage("error.file.parse_failed.label", "en-US", "File parsing failed", "errors"),
    I18nMessage("error.file.dlp_blocked.label", "zh-CN", "文件被 DLP 阻断", "errors"),
    I18nMessage("error.file.dlp_blocked.label", "en-US", "File blocked by DLP", "errors"),
    I18nMessage("error.file.dlp_unavailable.label", "zh-CN", "文件 DLP 扫描暂不可用", "errors"),
    I18nMessage("error.file.dlp_unavailable.label", "en-US", "File DLP unavailable", "errors"),
    I18nMessage("error.file.storage_unavailable.label", "zh-CN", "文件存储暂不可用", "errors"),
    I18nMessage(
        "error.file.storage_unavailable.label",
        "en-US",
        "File storage unavailable",
        "errors",
    ),
    I18nMessage("error.file.integrity_failed.label", "zh-CN", "文件完整性校验失败", "errors"),
    I18nMessage("error.file.integrity_failed.label", "en-US", "File integrity failed", "errors"),
    I18nMessage("error.file.not_found.label", "zh-CN", "文件不存在", "errors"),
    I18nMessage("error.file.not_found.label", "en-US", "File not found", "errors"),
)
MESSAGE_INDEX = {(message.locale, message.key): message.text for message in I18N_MESSAGES}


def normalize_locale(value: str | None) -> str:
    """归一化 locale，非法值回退到默认中文。

    公共只读元数据接口使用回退而不是报错，避免无认证页面因为 locale 拼写错误无法读取错误码目录。
    """

    normalized = str(value or DEFAULT_LOCALE).strip()
    if normalized in SUPPORTED_LOCALES:
        return normalized
    return DEFAULT_LOCALE


def is_supported_locale(value: str | None) -> bool:
    """判断用户偏好 API 是否允许写入指定 locale。"""

    return str(value or "").strip() in SUPPORTED_LOCALES


def available_locales() -> list[LocaleInfo]:
    """返回控制台可选语言列表，供用户偏好 API 直接序列化。"""

    return list(AVAILABLE_LOCALES)


def translate(locale: str | None, key: str) -> str:
    """按 locale 查询静态文案，缺失时按中文和 key 兜底。

    这个函数只返回展示文本，调用方不能把返回值写入错误码、事件类型或日志 code 字段。
    """

    normalized = normalize_locale(locale)
    return MESSAGE_INDEX.get((normalized, key)) or MESSAGE_INDEX.get((FALLBACK_LOCALE, key)) or key


def error_label(code: str, locale: str | None = DEFAULT_LOCALE) -> str:
    """返回错误码展示 label，未知错误码回退到稳定英文 code。"""

    label = translate(locale, f"error.{code}.label")
    return code if label == f"error.{code}.label" else label
