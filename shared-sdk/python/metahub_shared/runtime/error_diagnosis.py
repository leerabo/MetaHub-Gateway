"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/error_diagnosis.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel


class ErrorDiagnosisInfo(BaseModel):
    """统一错误诊断的对外安全 DTO。

    该结构只允许包含错误码、分类、建议、可重试性和安全摘要，不允许放入上游响应正文、
    Authorization、Provider Key、Virtual Key 明文、Prompt 或 Response。
    """

    rule_id: str
    category: str
    reason: str
    suggestion: str
    action: str
    severity: str
    retryable: bool
    matched_by: str
    safe_summary: str


@dataclass(frozen=True)
class ErrorDiagnosisRule:
    """静态错误归因规则。

    M0 规则库是全局元数据，不属于租户业务数据；后续如果要支持租户自定义规则，再迁移到带
    tenant_id 的持久化表。
    """

    rule_id: str
    error_codes: tuple[str, ...]
    category: str
    reason: str
    suggestion: str
    action: str
    severity: str
    retryable: bool
    http_statuses: tuple[int, ...] = field(default_factory=tuple)
    provider_error_types: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)


# 错误诊断兜底规则和专项规则表集中定义，确保统一错误码都能给出可执行排障建议。
UNKNOWN_ERROR_RULE = ErrorDiagnosisRule(
    rule_id="unknown_error",
    error_codes=(),
    category="unknown",
    reason="未命中特定错误归因规则。",
    suggestion="使用 request_id 查看调用日志和上游安全摘要，必要时联系平台管理员。",
    action="检查请求日志、Provider 状态、模型别名配置和最近的错误事件。",
    severity="warning",
    retryable=False,
)


ERROR_DIAGNOSIS_RULES: tuple[ErrorDiagnosisRule, ...] = (
    ErrorDiagnosisRule(
        rule_id="file_input_invalid",
        error_codes=(
            "file.multipart_invalid",
            "file.name_invalid",
            "file.unsupported_format",
            "file.too_large",
            "file.structure_invalid",
            "file.no_extractable_text",
            "file.extract_limit_exceeded",
            "file.parse_failed",
        ),
        category="file_input",
        reason="文件上传边界、格式、结构或可提取文本不符合安全处理合同。",
        suggestion="按文件上传文档修正格式和规模后重新提交，不要复用失败请求正文。",
        action="检查扩展名、媒体类型、文件结构、大小和可选择文本是否满足固定限制。",
        severity="warning",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="file_processing_busy",
        error_codes=("file.processing_busy",),
        category="file_capacity",
        reason="当前进程的有界文件解析容量已满。",
        suggestion="等待已有文件处理结束后再提交。",
        action="检查文件解析并发和处理耗时，避免用无界并发绕过容量保护。",
        severity="warning",
        retryable=True,
    ),
    ErrorDiagnosisRule(
        rule_id="file_dlp_blocked",
        error_codes=("file.dlp_blocked",),
        category="file_security",
        reason="租户 DLP 阻断规则命中，系统未保存原文件或抽取正文。",
        suggestion="由安全管理员复核当前租户 DLP 规则后再决定后续处理。",
        action="在当前租户内检查 DLP 策略摘要，禁止从日志或 Webhook 恢复命中正文。",
        severity="error",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="file_dependency_unavailable",
        error_codes=("file.dlp_unavailable", "file.storage_unavailable"),
        category="file_dependency",
        reason="DLP、数据库、加密密钥或对象存储依赖暂不可用，文件处理已失败关闭。",
        suggestion="恢复相关依赖后重新提交一个新的文件上传请求。",
        action="检查租户 DLP、数据库、文件加密配置和对象存储健康状态。",
        severity="error",
        retryable=True,
    ),
    ErrorDiagnosisRule(
        rule_id="file_integrity_failed",
        error_codes=("file.integrity_failed",),
        category="file_integrity",
        reason="文件 owner、密文、状态或不可变事件之间的完整性校验失败。",
        suggestion="使用 request_id 联系平台管理员，不要尝试绕过完整性校验。",
        action="复核同租户 owner、数据库状态机、密文认证和 source event 约束。",
        severity="error",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="file_not_found",
        error_codes=("file.not_found", "file.security_report_not_found"),
        category="file_resource",
        reason="当前租户下没有可访问的目标文件。",
        suggestion="刷新当前租户文件列表并选择仍存在的文件。",
        action="检查 file_id 和当前 Session 的 tenant，不披露其他租户是否存在同名对象。",
        severity="warning",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="file_security_not_ready",
        error_codes=("file.security_not_ready", "file.model_access_blocked"),
        category="file_security",
        reason="文件尚未形成 complete/passed 的可信安全报告，模型准入失败关闭。",
        suggestion="等待文件 ready，执行安全扫描并复核报告后再用于模型上下文。",
        action="检查 DLP、coverage、OCR、finding 风险和当前 detector version。",
        severity="error",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="file_security_capacity",
        error_codes=("file.security_scan_busy",),
        category="file_capacity",
        reason="当前进程的文件安全扫描容量已满，尚未读取企业文件 Blob。",
        suggestion="等待已有扫描结束后重试，不要提高客户端并发绕过门禁。",
        action="检查安全扫描和 OCR 独立 limiter 的占用与耗时。",
        severity="warning",
        retryable=True,
    ),
    ErrorDiagnosisRule(
        rule_id="file_security_unavailable",
        error_codes=("file.security_scan_unavailable",),
        category="file_dependency",
        reason="检测器、OCR 或安全事实事务无法形成可信终态。",
        suggestion="恢复离线检测依赖、数据库和文件存储后重试。",
        action="检查 detector/OCR 依赖、迁移、密钥、Blob Store 和数据库完整性。",
        severity="error",
        retryable=True,
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_auth_failed",
        error_codes=("gateway.upstream_auth_failed",),
        category="provider_auth",
        reason="上游 Provider 拒绝鉴权，常见原因是 Provider Key 失效、权限不足或账号被禁用。",
        suggestion="检查 Provider KeyPool 中当前 Key 的状态，必要时禁用失效 Key 并启用新的可用 Key。",
        action="在 Provider 控制台确认 Key 权限、账号状态和模型访问权限，再重试请求。",
        severity="error",
        retryable=False,
        http_statuses=(401, 403),
        provider_error_types=("invalid_api_key", "authentication_error", "permission_denied"),
        keywords=("unauthorized", "forbidden", "invalid_api_key", "authentication"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_rate_limited",
        error_codes=("gateway.upstream_rate_limited",),
        category="provider_rate_limit",
        reason="上游 Provider 返回限流或配额保护。",
        suggestion="稍后重试，或在 Provider KeyPool 中启用其他可用 Key。",
        action="检查 Provider 控制台限流、KeyPool 状态和本租户流量峰值。",
        severity="warning",
        retryable=True,
        http_statuses=(429,),
        provider_error_types=("rate_limit_exceeded", "rate_limit_error"),
        keywords=("rate_limit", "rate limit", "too many requests"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_quota_exceeded",
        error_codes=("gateway.upstream_quota_exceeded",),
        category="provider_quota",
        reason="上游余额、额度或账单状态不足。",
        suggestion="检查 Provider 账户余额、账单状态和 Key 对应项目额度。",
        action="充值或提升额度后重试；如果有备用 Provider，可临时切换模型路由。",
        severity="error",
        retryable=False,
        http_statuses=(402,),
        provider_error_types=("insufficient_quota", "billing_hard_limit_reached"),
        keywords=("insufficient_quota", "quota", "billing", "balance", "credit"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_model_not_found",
        error_codes=("gateway.upstream_model_not_found",),
        category="provider_model",
        reason="上游模型 ID 不存在、已下线或当前 Key 无权访问。",
        suggestion="检查模型目录中的 provider_model_id/upstream_model_name，并确认上游账号具备访问权限。",
        action="修正模型目录或模型别名路由，必要时更换 Provider 侧模型 ID。",
        severity="error",
        retryable=False,
        http_statuses=(404,),
        provider_error_types=("model_not_found", "model_not_exist"),
        keywords=("model_not_found", "model not found", "model_not_exist", "does not exist"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_timeout",
        error_codes=("gateway.upstream_timeout",),
        category="provider_timeout",
        reason="网关访问上游超时或上游连接长时间无响应。",
        suggestion="稍后重试；如果持续出现，请检查 Provider Endpoint 网络、超时配置和模型响应延迟。",
        action="查看同一 Provider 的近期失败率，必要时临时切换到备用 Provider。",
        severity="warning",
        retryable=True,
        http_statuses=(408, 504),
        provider_error_types=("TimeoutException", "ReadTimeout", "ConnectTimeout"),
        keywords=("timeout", "timed out"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_stream_timeout",
        error_codes=("gateway.upstream_stream_timeout",),
        category="provider_stream_timeout",
        reason="上游在流式输出期间超过块间等待阈值，网关已主动关闭连接并释放资源。",
        suggestion="检查 Provider 模型排队、流式输出延迟、网络稳定性和 Inter-Token Timeout 配置。",
        action="使用 request_id 查看 StreamSession 的首块、末块和分片计数；需要结果时发起一笔全新请求。",
        severity="warning",
        retryable=True,
        http_statuses=(504,),
        keywords=("stream timeout", "inter-token timeout"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_request_invalid",
        error_codes=("gateway.upstream_request_invalid",),
        category="provider_request",
        reason="请求通过了网关基础校验，但被上游判定为参数错误或能力不兼容。",
        suggestion="检查模型能力、messages/input 结构、temperature/max_tokens 等参数是否符合上游要求。",
        action="对照目标 Provider 文档修正参数；如果模型不支持该能力，请调整模型别名。",
        severity="error",
        retryable=False,
        http_statuses=(400, 422),
        provider_error_types=("invalid_request_error", "bad_request", "validation_error"),
        keywords=("invalid_request", "bad request", "validation", "invalid parameter"),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_unavailable",
        error_codes=("gateway.upstream_unavailable",),
        category="provider_network",
        reason="网关无法连接上游 Provider Endpoint。",
        suggestion="检查 Provider base_url、网络连通性、DNS、代理和防火墙配置。",
        action="使用 health/readiness 或本地 curl 验证 Endpoint 可达性，再重试请求。",
        severity="warning",
        retryable=True,
        keywords=("connection", "network", "invalidurl", "http error"),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_rate_limit_exceeded",
        error_codes=("gateway.rate_limit_exceeded",),
        category="gateway_rate_limit",
        reason="当前 Virtual Key 已达到管理员配置的 RPM、TPM 或并发限制。",
        suggestion="等待当前窗口重置，降低调用频率，或联系管理员调整该 Virtual Key 的限制。",
        action="检查 Virtual Key 限制设置、调用方重试策略和近期流量峰值。",
        severity="warning",
        retryable=True,
        http_statuses=(429,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_rate_limit_unavailable",
        error_codes=("gateway.rate_limit_unavailable",),
        category="gateway_rate_limit",
        reason="当前 Virtual Key 已启用限流规则，但 Redis 计数服务不可用。",
        suggestion="检查 METAHUB_REDIS_URL、Redis 网络连通性和认证配置，恢复后再放行受保护流量。",
        action="查看服务健康检查和 Redis 日志；在恢复前不要绕过限流保护。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_budget_exhausted",
        error_codes=("gateway.budget_exhausted",),
        category="gateway_budget",
        reason="当前租户或 Virtual Key 已达到当天预算上限，且预算策略配置为阻断。",
        suggestion="检查预算设置和今日用量；如需继续调用，请提高日预算或切换到仅告警策略。",
        action="在预算设置页查看命中的预算规则、今日成本和阻断策略，再决定是否调整额度。",
        severity="error",
        retryable=False,
        http_statuses=(402,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_budget_unavailable",
        error_codes=("gateway.budget_unavailable",),
        category="gateway_budget",
        reason="预算保护已启用，但预算规则或用量账本暂时不可读取。",
        suggestion="检查数据库迁移、usage_ledger 和预算规则表状态，恢复后再放行受保护流量。",
        action="查看控制面和数据面日志中的 budget_guard 事件；不要通过绕过预算保护来恢复流量。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_dlp_blocked",
        error_codes=("gateway.dlp_blocked",),
        category="gateway_dlp",
        reason="请求中的用户消息命中了当前租户启用的 DLP 阻断规则。",
        suggestion="在调用日志详情中查看命中的 DLP 规则 ID 和动作；如需试运行，请将规则动作调整为 log_only。",
        action="检查 DLP 规则配置、误拦截反馈和调用方输入来源；不要在日志或工单中粘贴原始敏感文本。",
        severity="error",
        retryable=False,
        http_statuses=(403,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_dlp_unavailable",
        error_codes=("gateway.dlp_unavailable",),
        category="gateway_dlp",
        reason="DLP 保护已启用，但规则读取或匹配执行暂时不可用。",
        suggestion="检查 DLP 规则表、数据库连接和正则安全校验结果，恢复后再放行受保护流量。",
        action="查看数据面日志中的 security.dlp_unavailable 事件；不要通过绕过 DLP 检查恢复生产调用。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_configuration_invalid",
        error_codes=(
            "policy.payload_invalid",
            "policy.expression_invalid",
            "policy.action_invalid",
            "policy.route_target_invalid",
            "policy.context_invalid",
            "policy.query_invalid",
            "policy.update_empty",
            "policy.quota_exceeded",
        ),
        category="policy_configuration",
        reason="策略字段、受限 DSL、动作、示例上下文或查询参数不符合当前最小版合同。",
        suggestion="检查策略编辑器诊断，只使用白名单变量、操作符、动作和当前租户 active 模型。",
        action="修正策略草稿后重新校验；不要尝试脚本、函数、属性访问或外部目标。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_conflict",
        error_codes=("policy.duplicate", "policy.version_conflict"),
        category="policy_conflict",
        reason="策略名称重复，或编辑期间策略版本已被其他管理员更新。",
        suggestion="刷新策略列表，确认最新版本和优先级后重新提交。",
        action="不要覆盖旧版本；基于最新 PolicyInfo 重新校验并保存。",
        severity="warning",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_unavailable",
        error_codes=("policy.unavailable",),
        category="policy_storage",
        reason="策略表、审计或失败事件暂时无法可靠读写。",
        suggestion="检查 0033 迁移、数据库完整性、审计表和事件约束。",
        action="恢复存储后使用新的 request_id 重试，不要绕过策略校验或审计。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    # Dry-Run 的固定诊断不读取 Prompt、Pydantic input 或底层 SQLite 异常；诊断只能由稳定
    # 平台错误码决定，避免敏感输入进入错误目录，也避免把 what-if 业务结果误归因为接口失败。
    ErrorDiagnosisRule(
        rule_id="policy_dry_run_payload_invalid",
        error_codes=(
            "policy.dry_run_payload_invalid",
            "policy.dry_run_payload_too_large",
        ),
        category="policy_dry_run_input",
        reason="策略预演请求不是受支持的有界 JSON 对象，或正文超过 256 KiB 上限。",
        suggestion="只提交当前预演所需字段，并检查严格类型、字符范围和正文大小。",
        action="不要在日志或工单中粘贴 Prompt；使用 request_id 定位被拒绝的请求边界。",
        severity="error",
        retryable=False,
        http_statuses=(413, 422),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_dry_run_context_not_found",
        error_codes=("policy.dry_run_context_not_found",),
        category="policy_dry_run_scope",
        reason="所选 Environment、Project 或 Model 不属于当前租户、未启用或不存在。",
        suggestion="刷新当前租户可见的 active 上下文后重新选择，跨租户与不存在对象使用同一结果。",
        action="检查后端 tenant-qualified 查询；不要通过差异化响应探测其他租户对象。",
        severity="error",
        retryable=False,
        http_statuses=(404,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_dry_run_request_id_reused",
        error_codes=("policy.dry_run_request_id_reused",),
        category="policy_dry_run_conflict",
        reason="同一租户和操作者已经用该 request_id 完成过一次预演。",
        suggestion="保留已提交安全事实，使用新的 request_id 发起新的预演。",
        action="不要尝试从持久摘要重建旧 Prompt 或完整解释；仅按稳定事件 ID 恢复投递。",
        severity="warning",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_dry_run_rate_limited",
        error_codes=("policy.dry_run_rate_limited",),
        category="policy_dry_run_rate_limit",
        reason="当前 actor 或 tenant 已达到本进程的策略预演 CPU 防护额度。",
        suggestion="等待当前一分钟窗口释放后重试，并减少并发预演请求。",
        action="检查 actor/tenant 预演频率；该限制不代表真实 Gateway Virtual Key 限流。",
        severity="warning",
        retryable=True,
        http_statuses=(429,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_dry_run_unavailable",
        error_codes=("policy.dry_run_unavailable",),
        category="policy_dry_run_storage",
        reason="候选策略图、一致性只读快照或 explanation/event/audit 安全事实无法形成一致结果。",
        suggestion="检查 0035 迁移、数据库完整性、Policy 图和安全事件事务后重试。",
        action="故障恢复前不要绕过 query-only 读取、权威回读或三类事实的同事务提交。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_snapshot_configuration_invalid",
        error_codes=(
            "policy_snapshot.payload_invalid",
            "policy_snapshot.action_unsupported",
            "policy_snapshot.variable_unsupported",
            "policy_snapshot.route_target_invalid",
        ),
        category="policy_snapshot_configuration",
        reason="待发布策略图包含非法字段、不可信变量、未实现动作或不可用路由目标。",
        suggestion="修正 active Policy 后重新校验候选，不能绕过发布阶段的完整编译。",
        action="检查固定诊断字段、Policy Rule/Action 和同租户 active 目标模型。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_snapshot_precondition_failed",
        error_codes=(
            "policy_snapshot.if_match_required",
            "policy_snapshot.version_conflict",
            "policy_snapshot.candidate_conflict",
            "policy_snapshot.idempotency_conflict",
            "policy_snapshot.rollback_target_invalid",
            "policy_snapshot.no_changes",
        ),
        category="policy_snapshot_conflict",
        reason="发布或回滚的 head、候选、幂等键或目标不再满足提交前置条件。",
        suggestion="刷新当前版本与候选 ETag，核对变更后再使用正确的 request_id 提交。",
        action="不要覆盖并发管理员结果；只允许回滚当前 head 的上一版。",
        severity="warning",
        retryable=False,
        http_statuses=(409, 428),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_snapshot_not_found",
        error_codes=(
            "policy_snapshot.not_found",
            "policy_snapshot.tenant_unavailable",
        ),
        category="policy_snapshot_scope",
        reason="当前租户无权访问目标快照，或内部同步目标租户不存在、已停用。",
        suggestion="核对当前 tenant 和快照历史；不要使用其他租户的 ID。",
        action="检查租户状态和后端 tenant_id 过滤，不要通过返回差异泄露跨租户对象。",
        severity="error",
        retryable=False,
        http_statuses=(404,),
    ),
    ErrorDiagnosisRule(
        rule_id="policy_snapshot_unavailable",
        error_codes=(
            "policy_snapshot.graph_invalid",
            "policy_snapshot.unavailable",
        ),
        category="policy_snapshot_storage",
        reason="策略来源图、快照迁移、双 hash、审计或持久事件暂时无法形成一致事实。",
        suggestion="检查 0034 迁移、数据库完整性和 Policy Rule/Action 关联后重试。",
        action="故障恢复前保留旧 head，不要发布半快照或跳过事务审计。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_policy_snapshot_unavailable",
        error_codes=(
            "gateway.policy_snapshot_unavailable",
            "gateway.policy_snapshot_stale",
            "gateway.policy_snapshot_expired",
        ),
        category="gateway_policy_snapshot",
        reason="Gateway 没有可信快照，或 LKG 已超过当前服务模式允许的有效期。",
        suggestion="恢复 Control API、内部 Token 和按租户快照同步，等待成功刷新后重试。",
        action="检查同步状态与安全计数；不要通过无限延长 stale LKG 绕过保护模式。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_policy_enforced",
        error_codes=(
            "gateway.policy_denied",
            "gateway.policy_evaluation_timeout",
            "gateway.policy_context_missing",
            "gateway.policy_action_unsupported",
        ),
        category="gateway_policy_enforcement",
        reason="已发布策略明确拒绝，或安全求值因超时、缺少可信上下文、未知动作而失败关闭。",
        suggestion="查看不含 DSL/上下文值的安全决策摘要，并由租户管理员修正策略。",
        action="不要重试绕过拒绝；确认策略 deadline、可信变量来源和动作执行器。",
        severity="error",
        retryable=False,
        http_statuses=(403,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_policy_mask_unsupported",
        error_codes=("gateway.policy_mask_unsupported",),
        category="gateway_policy_redaction",
        reason="请求正文包含当前执行器无法完整、安全遍历的文本结构。",
        suggestion="改用受支持的 Chat 用户文本或纯文本 Embeddings 输入后重新提交。",
        action="不要把未知结构降级为原文放行；确认 Provider 尚未收到该请求。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_policy_runtime_unavailable",
        error_codes=(
            "gateway.policy_context_unavailable",
            "gateway.policy_route_unavailable",
            "gateway.policy_mask_unavailable",
        ),
        category="gateway_policy_runtime",
        reason="策略所需预算上下文或 route_to 目标运行时暂时不可用。",
        suggestion="恢复预算存储，或检查目标模型、Provider 健康和能力后重试。",
        action="保持现有 DLP、限流和预算 Guard，不要把运行时故障降级成 allow。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_request_id_reused",
        error_codes=("gateway.request_id_reused",),
        category="gateway_request_tracking",
        reason="请求 ID 已被同租户的历史或并发请求占用，不能跨快照改写追踪事实。",
        suggestion="为每笔不同或重新发起的模型调用使用全新 X-Request-ID。",
        action="不要通过 UPSERT 覆盖旧 claim、版本或 request log。",
        severity="error",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_request_log_unavailable",
        error_codes=("gateway.request_log_unavailable",),
        category="gateway_request_tracking",
        reason="不可变请求追踪闸门无法可靠写入，调用已在 Provider 前失败关闭。",
        suggestion="恢复 request log 数据库和 0034 request claim 约束后重试。",
        action="Provider 调用前确认 tenant 级 request claim 成功，禁止无追踪请求继续上游。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="webhook_endpoint_invalid",
        error_codes=("webhook.endpoint_invalid", "webhook.event_type_invalid"),
        category="webhook_config",
        reason="Webhook Endpoint 配置字段非法，常见原因是 URL 协议、事件类型或超时时间不符合 M0 约束。",
        suggestion="检查 Endpoint URL、订阅事件类型和 timeout_ms；生产环境优先使用 HTTPS。",
        action="修正 Webhook 设置后重新保存，不要把 secret 放入 URL 用户名密码或日志。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="webhook_delivery_failed",
        error_codes=("webhook.delivery_failed",),
        category="webhook_delivery",
        reason="Webhook 接收端在 3 次尝试后仍未返回 2xx，或连接超时、网络不可达。",
        suggestion="查看发送记录中的 attempt HTTP 状态和错误码，确认接收端可达并能校验 HMAC 签名。",
        action="修复接收端 URL、证书、网络 ACL 或签名校验逻辑后重新测试。",
        severity="warning",
        retryable=True,
        http_statuses=(502,),
    ),
    ErrorDiagnosisRule(
        rule_id="webhook_secret_unavailable",
        error_codes=("webhook.secret_unavailable", "webhook.delivery_unavailable"),
        category="webhook_delivery",
        reason="Webhook 签名密钥或投递记录暂时不可用，无法可靠完成发送。",
        suggestion="检查数据库迁移、加密密钥配置和 Webhook 表状态。",
        action="恢复密钥配置或数据库后重试；不要通过记录明文 secret 的方式临时修复。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="dashboard_unavailable",
        error_codes=("dashboard.unavailable",),
        category="dashboard",
        reason="Dashboard 聚合查询暂时不可用，常见原因是数据库连接、迁移版本或索引状态异常。",
        suggestion="检查控制面数据库连接、0018 迁移状态以及 request_logs 与 usage_ledger 是否可读。",
        action="使用响应中的 request_id 查询控制面日志，并确认 Dashboard 查询始终带 tenant_id 与今日时间窗口。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="dashboard_query_invalid",
        error_codes=("dashboard.query_invalid",),
        category="dashboard",
        reason="Dashboard 查询包含重复、空值、未知视图或未支持的额外参数。",
        suggestion="总览使用固定今日窗口；角色首页最多传一个服务端已下发的小写 view code。",
        action="移除 tenant、role、时间范围等身份或范围参数，并按后端 available_views 重新请求。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="dashboard_config_invalid",
        error_codes=("dashboard.config_invalid",),
        category="dashboard",
        reason="当前租户角色首页配置缺失、停用或完整性校验失败。",
        suggestion="检查 0057 迁移、当前租户角色配置及 revision，不要在前端硬编码回退布局。",
        action="使用响应中的 request_id 定位配置完整性问题，修复后重新加载角色首页。",
        severity="error",
        retryable=False,
        http_statuses=(500,),
    ),
    ErrorDiagnosisRule(
        rule_id="user_preference_locale_invalid",
        error_codes=("user_preference.locale_invalid", "i18n.locale_unsupported"),
        category="user_preference",
        reason="用户语言偏好传入了当前版本不支持的 locale。",
        suggestion="使用 zh-CN 或 en-US，并确认前端选项来自 /user/preferences 返回的 available_locales。",
        action="检查语言设置页面提交的 locale 字段。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="user_preference_unavailable",
        error_codes=("user_preference.unavailable",),
        category="user_preference",
        reason="用户偏好暂时不可读取或更新，常见原因是数据库连接或 0019 迁移未完成。",
        suggestion="检查控制面数据库连接、user_preferences.locale 字段和 user_preference_events 表。",
        action="使用 request_id 查询控制面日志，并确认偏好查询按 tenant_id 与 user_id 过滤。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="routing_policy_payload_invalid",
        error_codes=(
            "model_alias.routing_mode_invalid",
            "model_alias.quality_cost_settings_invalid",
            "model_alias.routing_policy_version_invalid",
        ),
        category="routing_policy_validation",
        reason="路由模式或期望版本不符合固定的 RoutingPolicy 写入合同。",
        suggestion="提交受支持模式和当前正整数版本；quality_cost 还必须携带总和为 10000 的完整权重与门槛。",
        action="刷新模型别名后修正请求字段；不要在请求体中传 tenant_id 或候选配置。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="routing_policy_write_conflict",
        error_codes=(
            "model_alias.routing_policy_version_conflict",
            "model_alias.routing_policy_request_conflict",
        ),
        category="routing_policy_conflict",
        reason="路由策略已被并发修改，或同一 request_id 被用于不同配置意图。",
        suggestion="重新读取最新策略版本；只有完全相同的原请求才能复用 request_id。",
        action="基于最新版本重新确认模式并使用新的 request_id 提交。",
        severity="error",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="routing_policy_update_failed",
        error_codes=("model_alias.routing_policy_update_failed",),
        category="routing_policy_storage",
        reason="路由配置、审计或不可变配置事件未能在同一事务中提交。",
        suggestion="确认 0036 迁移、数据库写入和审计约束可用后重试。",
        action="使用 request_id 检查安全日志，不要绕过后端权限或直接修改策略版本。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_no_route_available",
        error_codes=("gateway.no_route_available",),
        category="gateway_routing",
        reason="动态路由的所有候选均因模型、Provider、健康或 KeyPool 状态被剔除。",
        suggestion="检查调用日志中的候选原因码，并恢复至少一条健康且有可用凭据的路由。",
        action="修复候选状态后重试；不要把 Provider Key 或上游错误正文粘贴到日志。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_routing_snapshot_invalid",
        error_codes=(
            "gateway.routing_policy_invalid",
            "gateway.routing_snapshot_invalid",
        ),
        category="gateway_routing_integrity",
        reason="路由策略或同一快照中的租户关系、健康、价格等持久事实不合法。",
        suggestion="检查 0036 约束及对应 Alias、Model、Provider、价格和策略事实。",
        action="修复损坏配置后再请求；该错误不应通过盲目重试绕过。",
        severity="error",
        retryable=False,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_routing_snapshot_unavailable",
        error_codes=(
            "gateway.routing_snapshot_unavailable",
            "gateway.routing_decision_unavailable",
        ),
        category="gateway_routing_storage",
        reason="路由快照读取或动态决策事实的原子写入暂时不可用。",
        suggestion="检查数据库连接、锁竞争和 routing_decisions/routing_candidates 可写性。",
        action="依照 request_id 确认没有发生上游调用后重试。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="experiment_input_invalid",
        error_codes=(
            "experiment.input_invalid",
            "experiment.query_invalid",
            "experiment.idempotency_required",
            "experiment.model_invalid",
        ),
        category="experiment_validation",
        reason="实验请求、查询、幂等键或模型组合不符合冻结的 exact-set 合同。",
        suggestion="检查 PAGE-105 提交字段、UUIDv4 幂等键和当前租户模型作用域。",
        action="修正输入后使用符合幂等语义的请求重试，不要提交 Prompt、Response 或 Provider Key。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="experiment_storage_unavailable",
        error_codes=(
            "experiment.unavailable",
            "experiment.statistics_unavailable",
        ),
        category="experiment_storage",
        reason="实验 owner 图、调度状态或同一统计快照暂时无法可靠读取。",
        suggestion="检查 0068 schema、SQLite 锁竞争和 Experiment scheduler 状态。",
        action="保留 request_id 并等待依赖恢复；不要绕过 tenant owner 或从页面自行拼装结果。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_experiment_routing_unavailable",
        error_codes=(
            "gateway.experiment_unavailable",
            "gateway.experiment_variant_unavailable",
        ),
        category="gateway_experiment_routing",
        reason="Gateway 无法证明实验作用域、arm 配置或当前可执行 owner 图。",
        suggestion="检查实验、Alias、Model、Provider、Adapter catalog、健康和 KeyPool 状态。",
        action="修复冻结 arm 后再发送新请求；不能静默旁路实验或改派另一个 arm。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_experiment_execution_timeout",
        error_codes=("gateway.experiment_execution_timeout",),
        category="gateway_experiment_execution",
        reason="实验请求超过创建时冻结的绝对执行截止时间。",
        suggestion="使用 request_id 检查 Outcome、RequestLog、Usage 和流式终态。",
        action="确认终态后再决定是否创建新请求，不要重放同一 request_id。",
        severity="warning",
        retryable=True,
        http_statuses=(504,),
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_model_not_found",
        error_codes=("gateway.model_not_found", "model_alias.model_invalid"),
        category="gateway_routing",
        reason="当前租户未配置可用的模型别名或别名指向的模型无效。",
        suggestion="检查模型别名、模型目录和 Provider 状态是否一致。",
        action="在控制台修正模型别名或启用可用模型后重试。",
        severity="error",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="gateway_request_invalid",
        error_codes=(
            "gateway.request_invalid",
            "gateway.model_missing",
            "gateway.messages_missing",
            "gateway.input_missing",
            "debug.request_invalid",
        ),
        category="request_validation",
        reason="请求缺少必填字段或字段格式不符合网关支持的 OpenAI-compatible 合同。",
        suggestion="检查 model、messages/input、stream 和调试台参数格式。",
        action="修正请求体后重新发送；调试台只支持非流式 Chat Completions。",
        severity="error",
        retryable=False,
        http_statuses=(400, 422),
    ),
    ErrorDiagnosisRule(
        rule_id="provider_key_unavailable",
        error_codes=("provider_key.unavailable",),
        category="provider_key_state",
        reason="当前租户没有可用的托管 Provider Key，且不满足本地环境 Key 的受控回退条件。",
        suggestion=(
            "为当前租户创建或启用托管 Provider Key；只有非生产环境的默认租户，"
            "且 Provider code 和 Endpoint 与启动配置一致时，才允许环境 Key。"
        ),
        action=(
            "检查当前租户、Provider 状态、托管 Key 和基准 Endpoint；"
            "不要让生产或其他租户复用进程级 Provider Key。"
        ),
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="model_quality_score_input_invalid",
        error_codes=(
            "model_quality_score.query_invalid",
            "model_quality_score.input_invalid",
            "model_quality_score.version_invalid",
        ),
        category="model_quality_score_validation",
        reason="模型评分查询、人工评分或期望版本不符合严格接口合同。",
        suggestion="刷新模型评分页，只提交当前租户模型、0-10000 基点评分或 null，以及最新人工版本。",
        action="移除 tenant_id、统计字段和 rating/accepted/tags 等后续 PRD 字段后重试。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="model_quality_score_not_found",
        error_codes=("model_quality_score.not_found",),
        category="model_quality_score",
        reason="当前认证租户内不存在指定模型评分对象。",
        suggestion="重新加载当前租户模型目录，避免复用其他租户或已删除模型 ID。",
        action="从模型评分页重新选择模型；系统不会说明该 ID 是否属于其他租户。",
        severity="error",
        retryable=False,
        http_statuses=(404,),
    ),
    ErrorDiagnosisRule(
        rule_id="model_quality_score_conflict",
        error_codes=(
            "model_quality_score.version_conflict",
            "model_quality_score.request_conflict",
        ),
        category="model_quality_score_conflict",
        reason="人工评分已被并发修改，或 request_id 已绑定另一评分意图。",
        suggestion="刷新最新 feedback_version；只有完全相同的原请求才能复用 request_id。",
        action="确认最新评分后重新提交，不要覆盖自动 statistics_version。",
        severity="error",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="model_quality_score_unavailable",
        error_codes=(
            "model_quality_score.unavailable",
            "model_quality_score.update_failed",
        ),
        category="model_quality_score_storage",
        reason="评分查询或人工评分、审计、不可变事件未能完整读取或提交。",
        suggestion="确认 0037 迁移、数据库连接和审计/Webhook 事件约束可用后重试。",
        action="使用 request_id 检查安全日志，不要直接修改评分计数或版本。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="feedback_input_invalid",
        error_codes=("feedback.input_invalid", "feedback.query_invalid"),
        category="feedback_validation",
        reason="反馈正文、操作链路或报表查询不符合严格类型、标签、时间和资源边界。",
        suggestion="只提交四个反馈字段；报表使用 canonical UTC 秒精度窗口和允许的分页参数。",
        action="移除 tenant/actor/模型注入、未知标签、重复键或重复 query 后再试。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="feedback_request_not_found",
        error_codes=("feedback.request_not_found",),
        category="feedback_scope",
        reason="当前认证租户或 Virtual Key 范围内不存在可见的源调用。",
        suggestion="从当前租户调用日志重新选择 request_id；系统不会披露其他租户或 Key 的归属。",
        action="确认登录租户或产生调用的 Virtual Key，不要枚举未知 request_id。",
        severity="error",
        retryable=False,
        http_statuses=(404,),
    ),
    ErrorDiagnosisRule(
        rule_id="feedback_request_not_feedbackable",
        error_codes=("feedback.request_not_feedbackable",),
        category="feedback_source_state",
        reason="源调用尚未形成可验证的成功终态、完成流或最终模型归因。",
        suggestion="等待流 completed，或改选具有完整最终路由归因的成功调用。",
        action="在调用日志详情检查后端给出的不可反馈原因，不要手工修改 RequestLog。",
        severity="error",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="feedback_conflict",
        error_codes=("feedback.already_exists", "feedback.request_conflict"),
        category="feedback_conflict",
        reason="源调用已有不可变反馈，或操作 request_id 已绑定另一规范化意图。",
        suggestion="读取现有反馈；丢响应重试必须复用原 operation request_id 和完全相同的正文。",
        action="不要覆盖或删除历史评分；新意图只能选择尚未反馈的源调用。",
        severity="error",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="feedback_unavailable",
        error_codes=("feedback.unavailable",),
        category="feedback_storage",
        reason="反馈数据库连接、迁移、写锁或只读汇总预算暂不可用。",
        suggestion="确认 0038→0039 完整迁移链和数据库健康后，使用相同 operation request_id 安全重试。",
        action="使用 request_id 检查脱敏日志；不要绕过 trigger 直接补写反馈或统计。",
        severity="warning",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="feedback_record_failed",
        error_codes=("feedback.record_failed",),
        category="feedback_integrity",
        reason="反馈、派生聚合与持久事件未能作为一致事务完整形成。",
        suggestion="检查 0038 graph/aggregate/deferred FK、0039 冲突守卫和完整性结果，再复用原操作 ID 重试。",
        action="保留 request_id/trace_id 排查固定失败分类，禁止返回 SQL 或敏感请求正文。",
        severity="error",
        retryable=True,
        http_statuses=(500,),
    ),
    # 映射管理错误只依据稳定平台码诊断，不能读取租户 pattern、上游原文或数据库异常。
    ErrorDiagnosisRule(
        rule_id="provider_error_map_not_found",
        error_codes=("error_map.not_found", "error_map.tenant_unavailable"),
        category="provider_error_map_resource",
        reason="当前租户下不存在可访问的 Provider 错误映射或内部快照租户。",
        suggestion="刷新当前租户规则列表，并确认内部同步请求使用了正确的已认证 tenant。",
        action="使用 request_id 核对租户身份和资源 ID；跨租户资源始终按不存在处理。",
        severity="error",
        retryable=False,
        http_statuses=(404,),
    ),
    ErrorDiagnosisRule(
        rule_id="provider_error_map_input_invalid",
        error_codes=(
            "error_map.input_invalid",
            "error_map.update_empty",
            "error_map.output_code_invalid",
            "error_map.pattern_invalid",
            "error_map.pattern_sensitive",
        ),
        category="provider_error_map_validation",
        reason="错误映射字段、归一错误码或安全 pattern 不符合受限规则合同。",
        suggestion="仅使用有界 equals/contains 条件和白名单归一错误码，并移除凭据或 URL 片段。",
        action="修正规则草稿后使用新的 request_id 提交；不要把原始上游正文复制到 pattern。",
        severity="error",
        retryable=False,
        http_statuses=(422,),
    ),
    ErrorDiagnosisRule(
        rule_id="provider_error_map_conflict",
        error_codes=(
            "error_map.conflict",
            "error_map.version_conflict",
            "error_map.request_conflict",
            "error_map.limit_exceeded",
        ),
        category="provider_error_map_conflict",
        reason="规则条件与 active 规则冲突、版本已变化、请求 ID 被异形复用或资源达到上限。",
        suggestion="刷新最新 revision 和规则版本，合并草稿或减少规则后再提交。",
        action="已得到 409 响应的 mutation request_id 已终结；修正输入后必须生成新 ID。",
        severity="warning",
        retryable=False,
        http_statuses=(409,),
    ),
    ErrorDiagnosisRule(
        rule_id="provider_error_map_unavailable",
        error_codes=(
            "error_map.unavailable",
            "error_map.snapshot_unavailable",
            "error_map.event_unavailable",
        ),
        category="provider_error_map_storage",
        reason="规则存储、内部快照或 Source Outbox 暂时无法保持一致性。",
        suggestion="检查 0046 迁移、数据库完整性、内部快照身份和事件调度器状态。",
        action="Gateway 继续使用已验证 LKG 或 builtin；恢复后再按 request_id 重试控制面操作。",
        severity="error",
        retryable=True,
        http_statuses=(503,),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_resource_not_found",
        error_codes=("gateway.upstream_resource_not_found",),
        category="provider_resource",
        reason="上游返回普通资源不存在，且没有命中明确的模型不存在信号。",
        suggestion="检查 Provider Endpoint、部署或其他上游资源路径，不要自动改写为模型不存在。",
        action="使用 request_id 查看脱敏状态和命中规则 ID，再核对目标 Provider 配置。",
        severity="error",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="virtual_key_invalid",
        error_codes=("debug.virtual_key_invalid", "gateway.auth_invalid"),
        category="key_state",
        reason="调用使用的 Virtual Key 不存在、已禁用或当前租户无权访问。",
        suggestion="检查 Virtual Key 状态，并确认请求使用的是当前租户的 active Key。",
        action="重新创建或启用 Virtual Key；不要在日志、URL 或错误详情中粘贴明文 Key。",
        severity="error",
        retryable=False,
    ),
    ErrorDiagnosisRule(
        rule_id="permission_denied",
        error_codes=("auth.permission_denied",),
        category="permission",
        reason="当前用户角色缺少访问该控制面资源的权限。",
        suggestion="联系管理员授予对应角色；前端隐藏按钮不能替代后端权限校验。",
        action="确认当前用户角色和后端 RBAC 配置，再重新登录验证。",
        severity="error",
        retryable=False,
        http_statuses=(403,),
    ),
    ErrorDiagnosisRule(
        rule_id="upstream_generic_error",
        error_codes=("gateway.upstream_error", "debug.upstream_failed"),
        category="provider_error",
        reason="上游 Provider 返回失败，但未命中更细的 401/429/402/404/timeout/参数错误规则。",
        suggestion="使用 request_id 查看调用日志中的上游 HTTP 状态和安全错误类型。",
        action="检查 Provider 状态、模型路由、KeyPool 和近期错误趋势。",
        severity="warning",
        retryable=True,
    ),
    ErrorDiagnosisRule(
        rule_id="infra_method_not_allowed",
        error_codes=("infra.method_not_allowed",),
        category="request_validation",
        reason="请求路径存在，但当前 HTTP 方法不在该路径公开的接口合同内。",
        suggestion="读取 Allow 响应头，并改用该路径明确支持的 HTTP 方法。",
        action="核对客户端路由和方法配置；不要通过重试重复发送相同的不支持方法。",
        severity="error",
        retryable=False,
        http_statuses=(405,),
    ),
    ErrorDiagnosisRule(
        rule_id="infra_unhandled",
        error_codes=("infra.unhandled",),
        category="platform_internal",
        reason="平台内部出现未捕获异常。",
        suggestion="使用 request_id 定位结构化日志；如果持续出现，请回滚最近变更或联系平台维护者。",
        action="检查服务日志、配置变更和数据库迁移状态。",
        severity="error",
        retryable=True,
        http_statuses=(500,),
    ),
)
# 通用上游错误码集合用于补齐未命中专项规则的 Provider 故障诊断。
GENERIC_UPSTREAM_ERROR_CODES = {"gateway.upstream_error", "debug.upstream_failed"}


def diagnose_error(
    error_code: str,
    *,
    http_status: int | None = None,
    provider_error_type: str | None = None,
    message: str | None = None,
) -> ErrorDiagnosisInfo:
    """根据统一错误码和安全元数据返回可操作诊断。

    message 只用于内存内关键词匹配，不会进入 safe_summary 或响应正文，避免上游原始错误、
    Prompt、Response 或密钥片段被诊断字段带出去。
    """

    metadata = _normalization_text(
        http_status=http_status, provider_error_type=provider_error_type, message=message
    )
    # Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
    if error_code in GENERIC_UPSTREAM_ERROR_CODES:
        metadata_diagnosis = _diagnose_by_metadata(
            error_code,
            metadata=metadata,
            http_status=http_status,
            provider_error_type=provider_error_type,
        )
        if metadata_diagnosis is not None:
            return metadata_diagnosis

    # 错误码是最稳定的归因入口，优先使用它可以避免上游文案漂移导致诊断不确定。
    for rule in ERROR_DIAGNOSIS_RULES:
        if error_code in rule.error_codes:
            return _diagnosis(
                rule, matched_by="error_code", error_code=error_code, http_status=http_status
            )

    # 只有上游泛化错误允许继续看 HTTP/provider 元数据，控制面和基础设施错误不能被误判成上游问题。
    if _allows_metadata_diagnosis(error_code):
        metadata_diagnosis = _diagnose_by_metadata(
            error_code,
            metadata=metadata,
            http_status=http_status,
            provider_error_type=provider_error_type,
        )
        if metadata_diagnosis is not None:
            return metadata_diagnosis

    return _diagnosis(
        UNKNOWN_ERROR_RULE, matched_by="unknown", error_code=error_code, http_status=http_status
    )


def _allows_metadata_diagnosis(error_code: str) -> bool:
    """判断错误码是否允许用 HTTP/provider 元数据兜底归因。

    非上游错误不能仅因为 HTTP 404/422 就被归因为模型不存在或上游参数错误，避免误导控制面和基础设施错误。
    """

    return error_code in GENERIC_UPSTREAM_ERROR_CODES or error_code.startswith("gateway.upstream_")


def _diagnose_by_metadata(
    error_code: str,
    *,
    metadata: str,
    http_status: int | None,
    provider_error_type: str | None,
) -> ErrorDiagnosisInfo | None:
    """用 HTTP 状态和 Provider 安全类型做兜底诊断。

    泛化上游错误码会先走这里，以便把 insufficient_quota/model_not_found 这类安全类型还原为可操作原因。
    """

    for rule in ERROR_DIAGNOSIS_RULES:
        # HTTP 状态和 Provider error type 只作为兜底匹配，不把上游原文返回给调用方。
        if _matches_metadata(
            rule, metadata, http_status=http_status, provider_error_type=provider_error_type
        ):
            matched_by = (
                "http_status" if http_status in rule.http_statuses else "provider_error_type"
            )
            return _diagnosis(
                rule, matched_by=matched_by, error_code=error_code, http_status=http_status
            )
    return None


def _matches_metadata(
    rule: ErrorDiagnosisRule,
    metadata: str,
    *,
    http_status: int | None,
    provider_error_type: str | None,
) -> bool:
    """判断安全元数据是否命中某条诊断规则。

    该函数只比较状态码、Provider 类型和关键词，绝不把 metadata 写入日志或响应。
    """

    if http_status is not None and http_status in rule.http_statuses:
        return True
    normalized_type = str(provider_error_type or "").lower()
    # Provider error type 是安全分类字段，命中后即可归因，不需要暴露上游 message 原文。
    if normalized_type and any(
        item.lower() == normalized_type for item in rule.provider_error_types
    ):
        return True
    return any(keyword.lower() in metadata for keyword in rule.keywords)


def _diagnosis(
    rule: ErrorDiagnosisRule,
    *,
    matched_by: str,
    error_code: str,
    http_status: int | None,
) -> ErrorDiagnosisInfo:
    """把内部规则转换为对外 DTO，并生成不含上游原文的安全摘要。"""

    return ErrorDiagnosisInfo(
        rule_id=rule.rule_id,
        category=rule.category,
        reason=rule.reason,
        suggestion=rule.suggestion,
        action=rule.action,
        severity=rule.severity,
        retryable=rule.retryable,
        matched_by=matched_by,
        safe_summary=_safe_summary(error_code=error_code, http_status=http_status),
    )


def _normalization_text(
    *,
    http_status: int | None,
    provider_error_type: str | None,
    message: str | None,
) -> str:
    """生成仅用于匹配的归一化文本。

    返回值不允许暴露给 API 调用方；它只是为了识别 insufficient_quota、model_not_found 等上游类型。
    """

    parts = [str(http_status or ""), str(provider_error_type or ""), str(message or "")]
    return " ".join(parts).lower()


def _safe_summary(*, error_code: str, http_status: int | None) -> str:
    """生成诊断安全摘要，只包含平台错误码和公开 HTTP 状态。"""

    if http_status is None:
        return error_code
    return f"{error_code} http_status={http_status}"
