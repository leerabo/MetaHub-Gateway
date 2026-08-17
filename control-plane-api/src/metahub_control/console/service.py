"""按数据库权限快照生成控制台导航、保存用户偏好并记录安全页面事件。

所有入口都由当前 tenant 的实时权限矩阵过滤；客户端 hash、角色名和前端隐藏状态不能扩大授权。
事件只接收固定枚举与脱敏 payload，不保存页面业务数据、Provider Key 或 Token。
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.rbac import (
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    canonical_permission_codes,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.dashboard.role_service import role_dashboard_event_is_current
from metahub_control.permissions.service import load_permission_matrix_from_connection

from .models import (
    ConsoleEventRequest,
    ConsoleEventResponse,
    ConsoleNavigationResponse,
    NavigationItem,
    UpdateUserPreferenceRequest,
    UserPreferenceInfo,
    UserPreferenceResponse,
)

# 控制台事件 payload 白名单集中定义；导航授权只读取数据库 permission 快照。
PAYLOAD_ALLOWED_KEYS = {"source", "hash", "viewport_class"}
# PAGE-092 事件在通用三字段之外只允许实际视图和配置 revision，禁止夹带角色、权限或卡片数值。
ROLE_DASHBOARD_PAYLOAD_ALLOWED_KEYS = {
    "source",
    "hash",
    "viewport_class",
    "view_code",
    "config_revision",
}
# 事件枚举与导航入口一一绑定，防止调用方把高价值事件伪造到其他页面。
EVENT_TYPE_BY_NAVIGATION = {
    "overview": "dashboard.viewed",
    "local-models": "console.page_viewed",
    # PAGE-107 页面事件只保存 shell 元数据；问题、回答与来源值仅存在浏览器活动内存，
    # 专用助手审计图也只保存枚举、计数和链路 owner，不能借 page_viewed 复制正文。
    "admin-assistant": "console.page_viewed",
    # PAGE-108/109 页面事件只记录受控 shell 元数据，不携带 task、target、密码或票据。
    "approvals": "console.page_viewed",
    # PAGE-117 只记录页面外壳，不复制漏斗计数、ProductEvent、actor 或时间。
    "growth-analytics": "console.page_viewed",
    "migration-guide": "migration.guide_viewed",
    "competitor-comparison": "help.competitor_viewed",
    # PAGE-088 只记录页面访问，不携带 Provider 指标、筛选或故障内容。
    "provider-health": "status.viewed",
    # PAGE-091 访问事件只记录页面外壳，不携带 run、case、失败码或 CI 报告正文。
    "adapter-contract-runs": "console.page_viewed",
    # PAGE-071 使用通用页面访问事件，显式登记可防止后续默认事件变化造成前后端合同漂移。
    "feedback-summary": "console.page_viewed",
    # PAGE-085 访问事件不得携带矩阵正文、权限指纹或租户身份。
    "role-permissions": "console.page_viewed",
    # PAGE-081 访问事件只记录页面、来源和视口，不携带规则 pattern 或 Provider 信息。
    "error-maps": "console.page_viewed",
    # PAGE-083 访问事件不携带 event_id、Endpoint、错误码或重放结果。
    "webhook-dead-letters": "console.page_viewed",
    # PAGE-094 访问事件只记录页面来源/视口，不携带模板正文、变量 schema 或版本定义。
    "prompt-templates": "console.page_viewed",
    # PAGE-095 访问事件不携带模板绑定、影响 hash、nonce、Policy 或 Snapshot 标识。
    "policy-templates": "console.page_viewed",
    # PAGE-096 访问事件只记录页面外壳，分类正文、结果和原因码均不得进入通用页面事件。
    "intent-security": "console.page_viewed",
    # PAGE-098 访问事件不得携带会话摘要、风险分数、Key、cursor 或处置上下文。
    "session-risks": "console.page_viewed",
    # PAGE-099 访问事件只证明打开页面，scan、规则、暴露计数和 request_id 均由专用读取接口承载。
    "output-safety": "console.page_viewed",
    # PAGE-101/102 访问事件不携带文件名、文件 ID、DLP 命中、Token 数或处理结果。
    "files": "console.page_viewed",
    # PAGE-103 访问事件只证明页面被打开，文件 ID、报告、finding 和扫描结果均不得进入载荷。
    "file-security": "console.page_viewed",
    # PAGE-104 page events never carry job/task/file IDs, names, costs, failures, or result text.
    "batch-jobs": "console.page_viewed",
    # PAGE-105 只记录页面访问，实验名称、模型指标、分桶和请求级 owner 均不得进入事件载荷。
    "experiments": "console.page_viewed",
    # PAGE-110/111 页面事件只记录 shell 元数据，不携带套餐字段、余额、调整原因或幂等键。
    "plans": "console.page_viewed",
    "quota-buckets": "console.page_viewed",
    # PAGE-112 只记录页面访问，不携带争议原因、item、金额、幂等键或审核决定。
    "billing-disputes": "console.page_viewed",
    # PAGE-114 访问事件只记录外壳元数据，不携带 Endpoint、Token、Schema、参数或结果。
    "mcp-tools": "console.page_viewed",
}
# 页面来源与视口只接受前端状态机声明的固定枚举，其他文本一律不进入审计表。
EVENT_SOURCES = {"sidebar", "direct_hash"}
# 角色首页只在显式视图切换或同视图刷新时扩展来源枚举，其他页面不能借用这两个值。
ROLE_DASHBOARD_EVENT_SOURCES = {"sidebar", "direct_hash", "view_switch", "refresh"}
VIEWPORT_CLASSES = {"mobile", "desktop"}
PAYLOAD_SENSITIVE_KEYS = {
    "authorization",
    "provider_key",
    "providerkey",
    "virtual_key",
    "virtualkey",
    "token",
    "secret",
    "password",
    "prompt",
    "response",
    "message",
    "messages",
    "variables",
    "template_variables",
    "rendered_user",
    "change_summary",
    "definition_ciphertext",
    "input",
}


@dataclass(frozen=True)
class NavigationDefinition:
    """内部导航定义。

    导航项由后端集中声明，避免每个前端面板各自复制角色判断而产生权限视图漂移。
    """

    id: str
    label: str
    group: str
    target_section: str
    required_permissions: tuple[str, ...]
    description: str
    empty_state_hint: str
    order: int


# 导航定义由后端按角色下发，避免只靠前端隐藏按钮形成伪权限控制。
NAVIGATION_DEFINITIONS: tuple[NavigationDefinition, ...] = (
    NavigationDefinition(
        "overview",
        "概览",
        "总览",
        "overview",
        ("dashboard.read",),
        "查看本地服务状态、MVP 边界和下一步引导。",
        "先确认 Gateway、Control API 和 Console 都处于可用状态。",
        10,
    ),
    NavigationDefinition(
        "admin-assistant",
        "管理助手",
        "总览",
        "admin-assistant",
        ("admin_assistant.query",),
        "使用可验证数据来源查询用量、错误、成本和策略摘要。",
        "选择一个只读问题开始查询。",
        11,
    ),
    NavigationDefinition(
        "approvals",
        "审批中心",
        "总览",
        "approvals",
        ("approval.read",),
        "处理当前租户预算阻断与高风险会话操作的审批任务。",
        "当前没有可见审批任务。",
        12,
    ),
    NavigationDefinition(
        "growth-analytics",
        "增长分析",
        "总览",
        "growth-analytics",
        ("product_analytics.read",),
        "查看当前租户的产品接入与激活漏斗。",
        "当前租户尚未形成可展示的产品激活事实。",
        13,
    ),
    NavigationDefinition(
        "tenant",
        "租户设置",
        "配置",
        "tenant",
        ("tenant.settings.read",),
        "查看并维护当前租户基础信息。",
        "确认当前租户名称和服务模式，再继续配置模型治理能力。",
        20,
    ),
    NavigationDefinition(
        "users",
        "用户与角色",
        "配置",
        "users",
        ("user.manage",),
        "管理当前租户内控制台用户和基础 RBAC 角色。",
        "先创建开发者账号，再分配 Key、日志和 API 文档入口。",
        30,
    ),
    NavigationDefinition(
        "role-permissions",
        "角色权限",
        "租户治理",
        "role-permissions",
        ("rbac.matrix.manage",),
        "查看并调整固定角色的细粒度权限矩阵。",
        "选择一个可变角色，再按职责上限调整权限。",
        32,
    ),
    NavigationDefinition(
        "environments",
        "环境设置",
        "配置",
        "environments",
        ("environment.read",),
        "管理 dev/test/prod 环境边界，并查看 Key、模型别名和预算关联情况。",
        "先创建测试或生产环境，再配置对应的 Key、模型别名和预算。",
        35,
    ),
    NavigationDefinition(
        "providers",
        "Provider 与 KeyPool",
        "配置",
        "providers",
        ("provider.read",),
        "注册上游模型 Endpoint，并维护 Provider KeyPool。",
        "先注册模型厂商或客户本地 OpenAI-compatible Endpoint。",
        40,
    ),
    NavigationDefinition(
        "local-models",
        "本地模型",
        "配置",
        "local-models",
        ("local_endpoint.read",),
        "管理当前租户可访问的本地 OpenAI-compatible Endpoint。",
        "尚未注册本地模型 Endpoint。",
        41,
    ),
    NavigationDefinition(
        "adapter-specs",
        "Adapter 规范",
        "配置",
        "adapter-specs",
        ("provider.health.read",),
        "查看国内模型、客户 Endpoint 与迁移平台的版本化 Adapter 执行合同。",
        "当前目录由发布制品激活；若目录不可用，请联系平台管理员核对版本。",
        43,
    ),
    NavigationDefinition(
        "adapter-contract-runs",
        "CI 测试报告",
        "配置",
        "adapter-contract-runs",
        ("provider.health.read",),
        "查看 Provider Adapter 合同测试运行、覆盖结果与安全失败摘要。",
        "尚无合同测试报告；请先由受控 CI runner 完成一次内部上报。",
        44,
    ),
    NavigationDefinition(
        "provider-health",
        "Provider 状态",
        "配置",
        "provider-health",
        ("provider.health.read", "request_log.read"),
        "查看上游可用性、真实调用指标、探测结果和最近故障。",
        "等待真实调用或周期探测形成状态样本。",
        45,
    ),
    NavigationDefinition(
        "error-maps",
        "错误码管理",
        "配置",
        "error-maps",
        ("provider_error_map.read",),
        "查看 Provider 错误归一规则与当前租户覆盖层版本。",
        "平台内置规则只读；管理员可创建租户覆盖规则。",
        47,
    ),
    NavigationDefinition(
        "models",
        "模型目录",
        "配置",
        "models",
        ("model.read",),
        "维护或查看当前租户可用的内部标准模型 ID。",
        "先绑定 Provider，再创建内部模型 ID 和价格字段。",
        50,
    ),
    NavigationDefinition(
        "model-aliases",
        "模型别名",
        "配置",
        "model-aliases",
        ("routing.read",),
        "管理业务侧稳定模型别名和最小路由。",
        "先创建模型目录，再配置默认/备用模型别名。",
        60,
    ),
    NavigationDefinition(
        "routing-policies",
        "路由策略",
        "配置",
        "routing-policies",
        ("routing.read",),
        "查看 ModelAlias 的固定、成本优先与可用性优先路由模式。",
        "先创建带主备模型的 ModelAlias，再由管理员选择路由模式。",
        61,
    ),
    NavigationDefinition(
        "model-quality-scores",
        "模型评分",
        "配置",
        "model-quality-scores",
        ("model_quality.read",),
        "查看模型成功率、错误率、平均延迟和人工质量先验。",
        "先积累真实调用样本，再由管理员按业务效果维护人工评分。",
        62,
    ),
    NavigationDefinition(
        "experiments",
        "模型评测",
        "配置",
        "experiments",
        ("experiment.read",),
        "查看在线 A/B 实验的不可变配置、采样进度和后端权威质量结果。",
        "尚无模型实验，可在具备完整创建权限后配置两个可执行模型进行对比。",
        63,
    ),
    NavigationDefinition(
        "projects",
        "项目与应用",
        "调用与成本",
        "projects",
        ("project.read",),
        "按业务系统维护 Project/Application，并为 Key、日志、用量和预算提供归属维度。",
        "先创建 Project，再按业务系统创建 Application 并绑定 Virtual Key。",
        65,
    ),
    NavigationDefinition(
        "virtual-keys",
        "Virtual Key",
        "调用与成本",
        "virtual-keys",
        ("virtual_key.read",),
        "创建和禁用业务系统调用 Gateway 使用的 Virtual Key。",
        "创建 dev Key 后可在 API 文档或调试台完成第一次调用。",
        70,
    ),
    NavigationDefinition(
        "usage",
        "用量与成本",
        "调用与成本",
        "usage",
        ("billing.usage.read",),
        "查看 Token 用量、估算标识、价格快照和成本汇总。",
        "先通过调试台或业务系统发起一次请求，再查看用量。",
        80,
    ),
    NavigationDefinition(
        "finops-ledger",
        "FinOps 账本",
        "调用与成本",
        "finops-ledger",
        ("billing.finops.read",),
        "查看不可变正式分录、精确金额汇总和来源价格证据。",
        "暂无账本分录，可先创建人工调整或发起一次计费调用。",
        81,
    ),
    NavigationDefinition(
        "model-prices",
        "模型价格",
        "调用与成本",
        "model-prices",
        ("model_price.read",),
        "维护模型历史、当前和未来价格，并追溯 Usage 采用的不可变价格快照。",
        "先创建模型，再登记输入/输出 Token 单价和 UTC 生效时间。",
        82,
    ),
    NavigationDefinition(
        "response-cache",
        "响应缓存",
        "调用与成本",
        "response-cache",
        ("response_cache.read",),
        "查看模型别名的可选响应缓存策略、收费状态与安全失效入口。",
        "尚未配置响应缓存策略；选择一个 active 模型别名后再显式启用。",
        83,
    ),
    NavigationDefinition(
        "plans",
        "套餐管理",
        "调用与成本",
        "plans",
        ("billing.plan.read",),
        "查看当前租户套餐、试用周期和有效层级。",
        "当前租户尚未创建套餐。",
        84,
    ),
    NavigationDefinition(
        "quota-buckets",
        "额度管理",
        "调用与成本",
        "quota-buckets",
        ("billing.quota.read",),
        "查看四类额度、分类消费和独立未分配成本。",
        "创建套餐后可查看和调整额度。",
        85,
    ),
    NavigationDefinition(
        "billing-disputes",
        "账单争议",
        "调用与成本",
        "billing-disputes",
        ("billing.dispute.read",),
        "提交并跟踪账单争议、结算暂缓和追加式费用调整。",
        "当前没有账单争议。",
        86,
    ),
    NavigationDefinition(
        "budgets",
        "预算中心",
        "调用与成本",
        "budgets",
        ("billing.budget.read",),
        "查看租户、Project 与 Virtual Key 三级日预算、有效约束和阻断状态。",
        "先选择 Environment 与可信作用链，再创建三级预算规则。",
        86,
    ),
    NavigationDefinition(
        "cost-anomalies",
        "成本异常",
        "调用与成本",
        "cost-anomalies",
        ("billing.anomaly.read",),
        "查看确定性规则检测到的成本异常，并执行确认、忽略或临时限流治理。",
        "暂无成本异常；检测器会在正式账本形成可信异常证据后生成告警。",
        87,
    ),
    NavigationDefinition(
        "policies",
        "策略管理",
        "安全与治理",
        "policies",
        ("policy.read",),
        "配置受限条件 DSL，并校验路由、预算和安全动作。",
        "先校验策略表达式，再保存为下一策略快照候选。",
        87,
    ),
    NavigationDefinition(
        "policy-simulator",
        "策略模拟器",
        "安全与治理",
        "policy-simulator",
        ("policy.simulate",),
        "使用当前请求上下文预演 active 策略候选、路由、DLP 与预算结果。",
        "先选择 Environment、Project 和模型，再运行一条不调用真实 Provider 的预演。",
        88,
    ),
    NavigationDefinition(
        "policy-snapshots",
        "策略发布",
        "安全与治理",
        "policy-snapshots",
        ("policy.read",),
        "发布 active 策略候选、查看不可变快照历史并回滚上一版。",
        "先在策略管理中准备 active 候选，再显式发布第一份策略快照。",
        89,
    ),
    NavigationDefinition(
        "policy-templates",
        "策略模板库",
        "安全与治理",
        "policy-templates",
        ("policy_template.read",),
        "查看内置行业与场景策略模板、适配状态和精确影响。",
        "选择模板并绑定项目后查看当前策略影响。",
        89,
    ),
    NavigationDefinition(
        "dlp-rules",
        "DLP 规则",
        "安全与治理",
        "dlp-rules",
        ("dlp.read",),
        "配置租户级关键词和正则 DLP 规则，阻断或试运行明显敏感输入。",
        "先用 log_only 试运行规则，再把高置信规则切换为 block。",
        90,
    ),
    NavigationDefinition(
        "intent-security",
        "敏感意图分类",
        "安全与治理",
        "intent-security",
        ("intent_risk.classify",),
        "使用本地轻量规则识别越权、提取凭据和绕过治理等敏感意图。",
        "输入单次待分类文本后，仅返回不含原文和命中片段的安全摘要。",
        90,
    ),
    NavigationDefinition(
        "session-risks",
        "高危会话",
        "安全与治理",
        "session-risks",
        ("session_risk.read",),
        "查看本租户多轮会话风险、冻结覆盖和不可变处置记录。",
        "当前没有达到高风险阈值的企业 Chat 或 API 会话。",
        90,
    ),
    NavigationDefinition(
        "output-safety",
        "输出安全事件",
        "安全与治理",
        "output-safety",
        ("request_log.read",),
        "查看本租户被阻断或扫描不可用的模型输出安全事件与暴露边界。",
        "当前没有输出阻断或扫描不可用事件。",
        90,
    ),
    NavigationDefinition(
        "webhooks",
        "Webhook 设置",
        "安全与治理",
        "webhooks",
        ("webhook.manage",),
        "注册企业系统回调地址，订阅预算、DLP 和调用失败等治理事件。",
        "先创建 Endpoint 并复制签名密钥，再发送测试事件确认接收端验签通过。",
        91,
    ),
    NavigationDefinition(
        "webhook-dead-letters",
        "Webhook 死信",
        "安全与治理",
        "webhook-dead-letters",
        ("webhook.manage",),
        "查看接收端故障形成的持久死信，并执行安全重放或逻辑归档。",
        "当前没有待处理的 Webhook 死信。",
        92,
    ),
    NavigationDefinition(
        "audit-logs",
        "审计日志",
        "安全与治理",
        "audit-logs",
        ("audit.read",),
        "管理操作、对象变更与请求链路",
        "暂无审计记录",
        93,
    ),
    NavigationDefinition(
        "request-logs",
        "调用日志",
        "调用与成本",
        "request-logs",
        ("request_log.read",),
        "按 request_id 查看调用状态、延迟、错误码、路由和用量摘要。",
        "先完成一次 Gateway 调用，失败请求也会进入调用日志或错误日志。",
        94,
    ),
    NavigationDefinition(
        "request-explanations",
        "请求解释",
        "调用与成本",
        "request-explanations",
        ("request_explanation.read",),
        "按固定决策链解释单次请求的鉴权、治理、路由、Provider、用量与 Webhook 证据。",
        "从调用日志选择一条请求，或输入当前租户内的 request_id。",
        95,
    ),
    NavigationDefinition(
        "feedback-summary",
        "反馈报表",
        "调用与成本",
        "feedback-summary",
        ("feedback.use",),
        "查看当前租户反馈样本、平均评分、采纳率、评分分布和模型汇总。",
        "先在调用日志详情或业务系统中提交反馈，再查看满意度统计。",
        96,
    ),
    NavigationDefinition(
        "quickstart",
        "快速开始",
        "开发者",
        "quickstart",
        ("developer_portal.read",),
        "复制 Python、JavaScript 或 curl 示例，完成第一次非流式 chat 调用。",
        "先创建 Virtual Key 和模型别名，再复制示例完成首次调用。",
        100,
    ),
    NavigationDefinition(
        "sdk-examples",
        "SDK 示例",
        "开发者",
        "sdk-examples",
        ("developer_portal.read",),
        "下载 Python 与 JavaScript/Node OpenAI-compatible 示例，"
        "覆盖流式调用、超时、重试和 request_id。",
        "示例只使用 Virtual Key 环境变量；下载接口会再次执行后端权限和租户校验。",
        105,
    ),
    NavigationDefinition(
        "migration-guide",
        "迁移指南",
        "开发者",
        "migration-guide",
        ("developer_portal.read",),
        "把直连模型厂商的服务端调用迁移到 MetaHub Gateway。",
        "先准备 active Environment、模型别名与对应的 Virtual Key。",
        107,
    ),
    NavigationDefinition(
        "api-docs",
        "API 文档",
        "开发者",
        "api-docs",
        ("console.basic.read",),
        "查看 OpenAI-compatible 接口、SDK 示例和错误处理说明。",
        "使用 Virtual Key 调用 Gateway，不要直连上游 Provider Key。",
        110,
    ),
    NavigationDefinition(
        "competitor-comparison",
        "竞品对比",
        "帮助中心",
        "competitor-comparison",
        ("console.basic.read",),
        "查看 OpenRouter、硅基流动与 MetaHub 的中立定位和能力映射。",
        "先确认客户需求是模型聚合、迁移兼容，还是企业治理与审计。",
        115,
    ),
    NavigationDefinition(
        "enterprise-chat",
        "企业 Chat",
        "开发者",
        "enterprise-chat",
        ("chat.use",),
        "使用项目和模型别名发起受治理的企业对话。",
        "每次调用都经过实时权限、策略、预算、路由和用量审计。",
        118,
    ),
    NavigationDefinition(
        "prompt-templates",
        "Prompt 模板",
        "开发者",
        "prompt-templates",
        ("prompt_template.read",),
        "管理企业 Prompt 模板、不可变版本、变量与适用项目。",
        "创建模板并发布首个版本后，可在企业 Chat 中按权限调用。",
        119,
    ),
    NavigationDefinition(
        "files",
        "文件处理",
        "开发者",
        "files",
        # 导航以读取权限为最低门槛；file.write 仅决定上传控件与 POST 路由，不能让只读
        # SecurityAdmin 因缺少写权限而失去审查租户文件安全状态的入口。
        ("file.read",),
        "上传文档并查看当前租户的文本抽取、安全预扫描与 Token 估算结果。",
        "上传 TXT、DOCX 或 PDF 后，可在此查看处理状态与安全摘要。",
        120,
    ),
    NavigationDefinition(
        "file-security",
        "文件安全报告",
        "安全与治理",
        "file-security",
        # 报告读取以 file.read 为唯一导航门槛；扫描是独立写命令，不能裁掉只读审查入口。
        ("file.read",),
        "查看当前租户文件的隐藏 Prompt Injection 检测结果并按权限发起扫描。",
        "先上传并解析文件，再在此执行安全扫描或查看已有报告。",
        121,
    ),
    NavigationDefinition(
        "batch-jobs",
        "批处理任务",
        "开发者",
        "batch-jobs",
        # PAGE-104 读取权限只开放页面；创建、取消、任务诊断和结果接口仍使用更严格的后端组合权限。
        ("batch.read",),
        "查看异步批处理任务、进度、固定失败原因和成本摘要。",
        "提交前确认文件安全准入、模型价格和预算均可用。",
        122,
    ),
    NavigationDefinition(
        "mcp-tools",
        "MCP 工具",
        "开发者",
        "mcp-tools",
        # PAGE-114 的页面入口只要求读取权限；注册和策略按钮由独立写权限控制。
        ("mcp.read",),
        "注册 MCP Server，审核工具 Schema 与权限，并查看安全调用摘要。",
        "注册 Server 并完成工具策略审核后，可按权限追踪调用状态。",
        123,
    ),
    NavigationDefinition(
        "debug-console",
        "OpenAI 调试台",
        "开发者",
        "debug-console",
        ("debug_console.use",),
        "发送一次最小 Chat Completions 请求，查看 request_id 和 trace_id。",
        "粘贴一次性 Virtual Key 明文或业务系统 Key 进行本地调试。",
        120,
    ),
    NavigationDefinition(
        "system-status",
        "系统状态",
        "系统",
        "system-status",
        ("console.basic.read",),
        "查看 Gateway、Control API 和 Console 的健康状态。",
        "如果服务不可用，先检查本地启动脚本和 request_id。",
        130,
    ),
    NavigationDefinition(
        "language-settings",
        "语言设置",
        "系统",
        "language-settings",
        ("console.basic.read",),
        "切换控制台中文或英文展示。",
        "默认中文，可在个人偏好中切换。",
        132,
    ),
    NavigationDefinition(
        "config-summary",
        "环境信息",
        "系统",
        "config-summary",
        ("system.config.read",),
        "查看控制面脱敏配置摘要。",
        "敏感配置只显示已脱敏状态，不展示真实密钥。",
        140,
    ),
)


def get_console_navigation(
    config: AppConfig,
    context: AuthContext,
    *,
    requested_active_item_id: str | None,
    request_id: str,
    trace_id: str,
) -> ConsoleNavigationResponse:
    """读取当前用户可见的控制台导航。

    租户和角色只来自 AuthContext；requested_active_item_id 只是 UI 请求，
    必须经过后端导航可见性校验。
    """

    connection = _connect(config)
    role_permissions = _load_navigation_role_permissions(
        connection, context.user.tenant_id
    )
    items = _visible_navigation_items(
        context.permission_codes, role_permissions_by_role=role_permissions
    )
    preference = _ensure_preference(connection, context, items)
    active_item_id = _resolve_active_item(
        requested_active_item_id,
        preference.active_navigation_item_id,
        items,
    )
    gateway_base_url, gateway_status = _trusted_gateway_base_url(config)
    return ConsoleNavigationResponse(
        navigation_items=items,
        active_navigation_item_id=active_item_id,
        gateway_base_url=gateway_base_url,
        gateway_base_url_status=gateway_status,
        permission_codes=list(context.permission_codes),
        permission_revision=context.permission_revision,
        authorization_fingerprint=context.authorization_fingerprint,
        request_id=request_id,
        trace_id=trace_id,
    )


def get_user_preferences(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> UserPreferenceResponse:
    """读取当前用户的控制台偏好，不允许跨租户或跨用户查询。"""

    connection = _connect(config)
    items = _visible_navigation_items(context.permission_codes)
    preference = _ensure_preference(connection, context, items)
    return UserPreferenceResponse(preference=preference, request_id=request_id, trace_id=trace_id)


def update_user_preferences(
    config: AppConfig,
    context: AuthContext,
    payload: UpdateUserPreferenceRequest,
    *,
    request_id: str,
    trace_id: str,
) -> UserPreferenceResponse:
    """更新当前用户控制台偏好。

    关键判断：active_navigation_item_id 必须对当前角色可见，避免用户通过偏好接口固定到无权限页面。
    """

    if payload.active_navigation_item_id is None and payload.sidebar_collapsed is None:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise ApiError("console.preference_invalid")
    connection = _connect(config)
    items = _visible_navigation_items(context.permission_codes)
    current = _ensure_preference(connection, context, items)
    active_navigation_item_id = (
        _validate_navigation_item(payload.active_navigation_item_id, items)
        if payload.active_navigation_item_id is not None
        else current.active_navigation_item_id
    )
    sidebar_collapsed = (
        bool(payload.sidebar_collapsed)
        if payload.sidebar_collapsed is not None
        else current.sidebar_collapsed
    )
    now = _to_db_time(_utcnow())
    connection.execute(
        """
        UPDATE user_preferences
        SET active_navigation_item_id=?, sidebar_collapsed=?, updated_at=?
        WHERE tenant_id=? AND user_id=?
        """,
        (
            active_navigation_item_id,
            1 if sidebar_collapsed else 0,
            now,
            context.user.tenant_id,
            context.user.user_id,
        ),
    )
    connection.commit()
    return UserPreferenceResponse(
        preference=_load_preference(
            connection,
            context.user.tenant_id,
            context.user.user_id,
            items,
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def record_console_event(
    config: AppConfig,
    context: AuthContext,
    payload: ConsoleEventRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ConsoleEventResponse:
    """写入控制台页面访问事件。

    事件 payload 会先经过字段白名单校验，确保密钥、Token、Prompt、Response 不会通过控制台事件入库。
    """

    items = _visible_navigation_items(context.permission_codes)
    item = _navigation_definition(payload.navigation_item_id, items)
    is_role_dashboard_event = payload.event_type == "dashboard.role_viewed"
    allowed_payload_keys = (
        ROLE_DASHBOARD_PAYLOAD_ALLOWED_KEYS if is_role_dashboard_event else PAYLOAD_ALLOWED_KEYS
    )
    safe_payload = _safe_event_payload(payload.payload, allowed_payload_keys)
    expected_event_type = EVENT_TYPE_BY_NAVIGATION.get(item.id, "console.page_viewed")
    canonical_path = f"/#{item.id}"
    canonical_hash = f"#{item.id}"
    if (
        item.target_section != payload.target_section
        or (
            payload.event_type != expected_event_type
            and not (item.id == "overview" and is_role_dashboard_event)
        )
        or payload.path != canonical_path
        or set(safe_payload) != allowed_payload_keys
        or safe_payload.get("hash") != canonical_hash
        or safe_payload.get("source") not in (
            ROLE_DASHBOARD_EVENT_SOURCES if is_role_dashboard_event else EVENT_SOURCES
        )
        or safe_payload.get("viewport_class") not in VIEWPORT_CLASSES
        or (
            is_role_dashboard_event
            and (
                type(safe_payload.get("view_code")) is not str
                or type(safe_payload.get("config_revision")) is not int
            )
        )
    ):
        # event/nav/target/path/hash/source/viewport 必须整体匹配后端定义，任一伪造都拒绝落库。
        raise ApiError("console.event_invalid")
    try:
        connection = connect_database(config.database_url)
    # 连接异常属于事件遥测不可用，必须与其他控制台数据库错误隔离映射。
    except DbError as exc:
        # 事件存储连接失败统一映射为可重试 503，不能泄露数据库 URL 或驱动异常。
        raise ApiError("console.event_unavailable") from exc
    event_id = f"cpe_{uuid4().hex}"
    try:
        # 角色首页事件必须在写事务中复核当前配置和实时权限，防止撤权后的迟到页面继续落库。
        if is_role_dashboard_event:
            # 配置复核与事件 INSERT 共用写事务，防止 revision 在检查和落库之间漂移。
            connection.execute("BEGIN IMMEDIATE")
            if not role_dashboard_event_is_current(
                connection,
                context,
                view_code=safe_payload["view_code"],
                config_revision=safe_payload["config_revision"],
            ):
                # revision 或视图已失效时拒绝事件，不能把陈旧授权状态记录成有效访问事实。
                raise ApiError("console.event_invalid")
        connection.execute(
            """
            INSERT INTO console_page_events(
              event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
              target_section, path, request_id, trace_id, payload_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                context.user.tenant_id,
                context.user.user_id,
                context.session.session_id,
                payload.event_type,
                item.id,
                item.target_section,
                payload.path,
                request_id,
                trace_id,
                json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                _to_db_time(_utcnow()),
            ),
        )
        connection.commit()
    # 合同或权限错误必须保留原始统一错误码，不能被后续数据库异常映射覆盖。
    except ApiError:
        # 事件合同失败必须撤销已开始的 role-view 事务，且不能被折叠成依赖故障。
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    # 写入和提交共享同一失败出口，确保任何数据库错误都先撤销事务。
    except sqlite3.Error as exc:
        # INSERT 或 commit 失败必须回滚当前事务，确保不会留下无响应但已部分写入的事件。
        try:
            connection.rollback()
        except sqlite3.Error:
            # rollback 自身失败也只能返回统一 503，不能用第二个数据库异常覆盖安全响应。
            pass
        raise ApiError("console.event_unavailable") from exc
    finally:
        try:
            connection.close()
        # 关闭连接失败不能覆盖已提交结果或既定统一错误，也不得泄露数据库内部信息。
        except sqlite3.Error:
            # 关闭失败不能覆盖已完成事件或统一错误，连接中也不包含可公开细节。
            pass
    return ConsoleEventResponse(
        accepted=True,
        event_id=event_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def _load_navigation_role_permissions(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> dict[str, frozenset[str]]:
    """从当前租户矩阵反算固定角色的有效权限集合。

    参数：控制面数据库连接和认证恢复的 tenant_id。返回：六角色到权限集合的映射。
    目录、租户关系、职责上限或矩阵哈希任一异常时复用权限服务的失败关闭校验。
    """

    try:
        _, roles = load_permission_matrix_from_connection(connection, tenant_id)
    # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
    except ApiError:
        raise
    # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
    except sqlite3.Error as exc:
        # 数据库路径、SQL 和驱动文本不得进入公开响应。
        raise ApiError("rbac.unavailable") from exc
    return {role.code: frozenset(role.permission_codes) for role in roles}


def _visible_navigation_items(
    permission_codes: tuple[str, ...],
    *,
    role_permissions_by_role: dict[str, frozenset[str]] | None = None,
) -> list[NavigationItem]:
    """按当前数据库权限快照生成导航，不根据角色名推导当前用户入口。

    required_roles 仅为旧客户端兼容展示；生产导航传入当前租户完整矩阵反算该字段，
    required_permissions 才是前端裁剪和后端路由校验共同使用的权威合同。
    """

    try:
        normalized = canonical_permission_codes(permission_codes)
    # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
    except (TypeError, ValueError):
        # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
        raise ApiError("rbac.matrix_integrity_failed") from None
    # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
    if normalized != tuple(permission_codes):
        # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
        raise ApiError("rbac.matrix_integrity_failed")
    role_matrix = (
        ROLE_GRANTABLE_PERMISSIONS
        if role_permissions_by_role is None
        else role_permissions_by_role
    )
    # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
    if set(role_matrix) != set(ROLE_CODES):
        # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
        raise ApiError("rbac.matrix_integrity_failed")
    granted = set(normalized)
    return [
        NavigationItem(
            id=item.id,
            label=item.label,
            group=item.group,
            target_section=item.target_section,
            required_roles=[
                role_code
                for role_code in ROLE_CODES
                if set(item.required_permissions) <= set(role_matrix[role_code])
            ],
            required_permissions=list(item.required_permissions),
            visible=True,
            disabled=False,
            description=item.description,
            empty_state_hint=item.empty_state_hint,
            order=item.order,
        )
        for item in sorted(NAVIGATION_DEFINITIONS, key=lambda value: value.order)
        # 权限判断必须在后端执行；角色名和 required_roles 都不能替代当前数据库快照。
        if set(item.required_permissions) <= granted
    ]


def _fallback_navigation_item(items: list[NavigationItem]) -> str:
    """选择权限收窄后的安全首页；完全无导航权限时统一拒绝控制台访问。"""

    if not items:
        # 导航只信任本次数据库权限快照；异常时必须失败关闭并拒绝恢复受限页面。
        raise ApiError("auth.permission_denied")
    if any(item.id == "overview" for item in items):
        return "overview"
    return items[0].id


def _resolve_active_item(
    requested_active_item_id: str | None,
    stored_active_item_id: str,
    items: list[NavigationItem],
) -> str:
    """解析当前激活导航项，优先使用合法 hash，其次使用后端偏好，最后回退概览。"""

    visible_ids = {item.id for item in items}
    if requested_active_item_id:
        if requested_active_item_id not in visible_ids:
            # hash 中的入口必须属于当前角色可见集合，前端隐藏入口不能替代后端权限校验。
            raise ApiError("console.navigation_invalid")
        return requested_active_item_id
    if stored_active_item_id in visible_ids:
        return stored_active_item_id
    return _fallback_navigation_item(items)


def _validate_navigation_item(value: str, items: list[NavigationItem]) -> str:
    """校验偏好中的导航项必须存在且对当前角色可见。"""

    normalized = value.strip()
    if not normalized or normalized not in {item.id for item in items}:
        # 偏好写入只能保存当前角色允许访问的入口，防止跨角色恢复受限页面。
        raise ApiError("console.navigation_invalid")
    return normalized


def _navigation_definition(value: str, items: list[NavigationItem]) -> NavigationItem:
    """从当前角色可见导航中查找事件目标，避免页面事件伪造受限入口。"""

    normalized = value.strip()
    for item in items:
        if item.id == normalized:
            return item
    # 事件只接受当前角色可见导航项，未知或越权入口统一记录为事件参数非法。
    raise ApiError("console.event_invalid")


def _safe_event_payload(
    payload: dict[str, object],
    allowed_keys: set[str],
) -> dict[str, object]:
    """按当前事件 exact-set 白名单过滤 payload，只保留低风险短标量。"""

    if not isinstance(payload, dict) or len(payload) > len(allowed_keys):
        # payload 结构和字段数量先做硬限制，避免审计事件被滥用为任意数据写入通道。
        raise ApiError("console.event_invalid")
    safe: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = str(key).strip().lower()
        if normalized_key in PAYLOAD_SENSITIVE_KEYS or normalized_key not in allowed_keys:
            # 事件 payload 是审计数据入口，宁可拒绝未知字段，也不能静默保存敏感上下文。
            raise ApiError("console.event_invalid")
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            # 只允许简单标量进入事件日志，复杂对象可能携带 Prompt、响应正文或密钥片段。
            raise ApiError("console.event_invalid")
        if isinstance(value, str) and len(value) > 120:
            # 页面事件只记录短上下文，过长字符串按潜在敏感内容拒绝入库。
            raise ApiError("console.event_invalid")
        safe[normalized_key] = value
    return safe


def _trusted_gateway_base_url(config: AppConfig) -> tuple[str | None, str]:
    """校验并规范化导航响应中的公开 Gateway `/v1` 地址。

    非开发环境拒绝 loopback；所有环境都拒绝凭据、query、fragment、非 HTTP(S) 和额外路径。
    """

    raw_url = config.gateway_api_base_url.strip()
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        # 读取 port 会触发非法端口校验，避免把不可解析配置下发给迁移示例。
        _ = parsed.port
    except ValueError:
        return None, "configuration_required"
    if (
        not raw_url
        or parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/v1", "/v1/"}
    ):
        # URL 含凭据、附加路径或可变参数时不可信，必须阻断复制而不是猜测修正。
        return None, "configuration_required"
    is_loopback = hostname == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(hostname).is_loopback
    # DNS 主机名无法按 IP 解析是正常路径，不应误判为配置失败。
    except ValueError:
        # 普通 DNS 主机名不是 IP，继续按非 loopback 地址处理。
        pass
    if config.environment != "dev" and is_loopback:
        # test/prod 不能把本机地址作为企业迁移指南的权威 Gateway 地址。
        return None, "configuration_required"
    return f"{parsed.scheme.lower()}://{parsed.netloc}/v1", "ready"


def _ensure_preference(
    connection: sqlite3.Connection,
    context: AuthContext,
    items: list[NavigationItem],
) -> UserPreferenceInfo:
    """确保当前用户存在一条偏好记录，并在角色变化后把非法 active item 回退到概览。"""

    row = connection.execute(
        """
        SELECT preference_id, tenant_id, user_id, active_navigation_item_id,
               sidebar_collapsed, created_at, updated_at
        FROM user_preferences
        WHERE tenant_id=? AND user_id=?
        """,
        (context.user.tenant_id, context.user.user_id),
    ).fetchone()
    if row is None:
        now = _to_db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO user_preferences(
              preference_id, tenant_id, user_id, active_navigation_item_id,
              sidebar_collapsed, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, 0, ?, ?)
            """,
            (
                f"upr_{uuid4().hex}",
                context.user.tenant_id,
                context.user.user_id,
                _fallback_navigation_item(items),
                now,
                now,
            ),
        )
        connection.commit()
        # 新建偏好后重新按租户和用户读取，保证返回结构与后续读取路径完全一致。
        return _load_preference(connection, context.user.tenant_id, context.user.user_id, items)
    if row["active_navigation_item_id"] not in {item.id for item in items}:
        now = _to_db_time(_utcnow())
        connection.execute(
            """
            UPDATE user_preferences
            SET active_navigation_item_id=?, updated_at=?
            WHERE tenant_id=? AND user_id=?
            """,
            (
                _fallback_navigation_item(items),
                now,
                context.user.tenant_id,
                context.user.user_id,
            ),
        )
        connection.commit()
        # 角色收窄后把不可见入口回退到概览，再重新读取以避免返回过期权限状态。
        return _load_preference(connection, context.user.tenant_id, context.user.user_id, items)
    return _preference_info(row)


def _load_preference(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    items: list[NavigationItem],
) -> UserPreferenceInfo:
    """按租户和用户读取偏好记录，缺失时返回统一业务错误。"""

    row = connection.execute(
        """
        SELECT preference_id, tenant_id, user_id, active_navigation_item_id,
               sidebar_collapsed, created_at, updated_at
        FROM user_preferences
        WHERE tenant_id=? AND user_id=?
        """,
        (tenant_id, user_id),
    ).fetchone()
    if row is None:
        # 偏好记录缺失说明租户用户状态不完整，返回统一错误码便于前端展示恢复提示。
        raise ApiError("console.preference_invalid")
    if row["active_navigation_item_id"] not in {item.id for item in items}:
        # 存量偏好若指向当前角色不可见入口，必须阻止继续使用并由上层触发修复。
        raise ApiError("console.navigation_invalid")
    return _preference_info(row)


def _preference_info(row: sqlite3.Row) -> UserPreferenceInfo:
    """把数据库偏好记录转换为 API DTO。

    转换时保留 tenant_id 和 user_id，确保控制台偏好始终带租户边界，不混入其他租户用户状态。
    """

    return UserPreferenceInfo(
        preference_id=row["preference_id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        active_navigation_item_id=row["active_navigation_item_id"],
        sidebar_collapsed=bool(row["sidebar_collapsed"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并把底层连接错误映射为统一错误结构。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 底层数据库错误继续沿用统一错误码，避免控制台 API 返回不可解析的基础设施异常。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _utcnow() -> datetime:
    """返回 UTC 当前时间，统一控制台偏好和事件的时间口径。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把 UTC datetime 转换为数据库保存的 ISO 字符串。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """解析数据库 ISO 时间字符串为 UTC datetime。

    控制台偏好和页面事件统一使用 UTC，避免多时区部署时出现排序和审计时间偏移。
    """

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
