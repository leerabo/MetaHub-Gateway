"""PRD-059 共享角色与权限目录。

该模块只保存产品级静态元数据和规范化算法，不读取租户数据库。真正授权必须由
Control API 每次请求从 ``roles``、``permissions`` 和 ``role_permissions`` 恢复，
禁止把这里的默认集合当作运行时回退权限。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

RoleCode = Literal[
    "admin",
    "developer",
    "viewer",
    "finance_admin",
    "security_admin",
    "ops_admin",
]
RiskLevel = Literal["read", "write", "critical"]

# 角色、目录 revision 与权限码正则共同冻结产品合同；运行时授权仍必须读取租户数据库。
ADMIN_ROLE: Final[RoleCode] = "admin"
DEVELOPER_ROLE: Final[RoleCode] = "developer"
VIEWER_ROLE: Final[RoleCode] = "viewer"
FINANCE_ADMIN_ROLE: Final[RoleCode] = "finance_admin"
SECURITY_ADMIN_ROLE: Final[RoleCode] = "security_admin"
OPS_ADMIN_ROLE: Final[RoleCode] = "ops_admin"
ROLE_CODES: Final[tuple[RoleCode, ...]] = (
    ADMIN_ROLE,
    DEVELOPER_ROLE,
    VIEWER_ROLE,
    FINANCE_ADMIN_ROLE,
    SECURITY_ADMIN_ROLE,
    OPS_ADMIN_ROLE,
)
# 权限目录版本只描述产品级目录结构，不可替代租户角色自身的 permission_revision。
# PRD-070 新增高危会话读取与处置边界后提升目录版本；认证层会据此发现旧迁移并失败关闭，
# 不能把 revision 6 的目录静默当成完整权限矩阵。
# PRD-075 增加在线模型实验读写权限后提升目录版本；认证层会拒绝仍停留 revision 10 的旧投影，
# 不能把 Shared 默认矩阵当成运行时回退授权，更不能用前端导航代替后端动态矩阵。
# PRD-076 再把 Response Cache 的读取、策略写入与主动失效拆成三个独立权限；目录版本提升后，
# 运行时仍须逐请求从当前 tenant 数据库恢复实际矩阵，不能因 Shared 中存在默认值而放宽授权。
# PRD-077 的助手权限只开放只读入口，不隐含 Usage、FinOps、RequestLog 或 Policy 源域权限；
# 最终授权仍必须从当前 tenant 的动态角色矩阵逐请求恢复并在审计事务内复核。
# PRD-078 把审批入口与目标域权限拆开：read/decide 只表达审批职责，Control 仍须实时
# 复核预算或会话风险的目标权限，不能让审批权限成为跨域写入后门。
# PRD-080 再拆分账单争议读取、申请与审核权限；批准争议仍须同时实时具备
# ``billing.finops.write``，不能把争议审核权限当作任意账本写入能力。
# PRD-082 把 MCP 读取、Server 管理、工具策略、调用和审批拆为五个独立后端权限；
# 工具附加权限和高危审批都必须叠加校验，不能由页面可见性或 Shared 默认矩阵替代。
# PRD-083 再把出站网络策略与 MCP 内容策略分离，只有后端 network manage 权限可修改。
# PRD-084 为本地模型聚合入口增加独立 read/manage/test 权限；developer 仅获 read/test，
# 不得借页面可见性读取完整 Provider/KeyPool 或注册凭据。
# PRD-085 的激活漏斗只对 admin 与 ops_admin 开放，页面可见性不能替代后端权限校验。
# PRD-086 的客户健康评分沿用当前租户边界，并增加独立只读权限；不存在的客户成功/销售
# persona 不能变成跨租户授权，默认只向 admin 与 ops_admin 扩展。
# PRD-087 将伙伴读取、首次配置、客户管理、白标、分润导出和交付下载拆成六个权限；
# 运行时还必须叠加当前 tenant 的 Partner owner 校验，不能把角色上限当成客户范围。
# PRD-088 把全局控制台/登录页白标和伙伴门户白标分开，读写权限不能复用后者。
# PRD-089 再把租户数据生命周期拆成只读、创建导出、下载和注销四个后端权限；危险命令不能因
# PAGE-121 可见或角色名称而得到隐式授权。
# PRD-114 将企业 SSO 安全摘要和管理动作拆成独立权限；Workspace 角色仍需在 API 边界叠加校验，
# 不能仅凭租户角色或前端页面可见性管理域名、连接和强制登录策略。
# PRD-115 再把 SCIM 只读摘要与目录管理分离；机器 Bearer 仍由独立路由鉴权，不能获得浏览器权限。
CATALOG_REVISION: Final[int] = 26
_PERMISSION_CODE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    """描述一个稳定权限点。

    字段全部来自产品迁移，不能包含租户名称、用户邮箱、密钥或业务请求正文；
    ``risk_level`` 只用于控制台提示，不参与后端授权计算。
    """

    code: str
    category: str
    name: str
    description: str
    risk_level: RiskLevel


# 目录顺序是 API 和迁移测试的稳定合同；新增权限必须提升 catalog revision 并新建迁移。
PERMISSION_CATALOG: Final[tuple[PermissionDefinition, ...]] = (
    PermissionDefinition(
        "console.basic.read",
        "console",
        "基础控制台",
        "访问概览、API 文档、系统状态和迁移兼容页面。",
        "read",
    ),
    PermissionDefinition(
        "dashboard.read",
        "console",
        "读取概览",
        "读取当前租户的聚合运行概览，不包含正式账单明细。",
        "read",
    ),
    PermissionDefinition(
        "product_analytics.read",
        "analytics",
        "读取产品激活漏斗",
        "读取当前租户产品使用行为聚合与七阶段激活漏斗。",
        "read",
    ),
    PermissionDefinition(
        "customer_health.read",
        "analytics",
        "读取客户健康评分",
        "读取当前租户确定性客户健康评分、聚合信号与可解释建议。",
        "read",
    ),
    PermissionDefinition(
        "branding.read",
        "branding",
        "读取白标配置",
        "读取当前租户控制台与登录页的安全白标配置和受限 Logo。",
        "read",
    ),
    PermissionDefinition(
        "branding.manage",
        "branding",
        "管理白标配置",
        "修改当前租户控制台与登录页的 Logo、名称、配色和文案。",
        "critical",
    ),
    PermissionDefinition(
        "partner_portal.read",
        "partner",
        "读取伙伴门户",
        "读取当前租户伙伴资料、客户安全聚合与分润摘要。",
        "read",
    ),
    PermissionDefinition(
        "partner_portal.configure",
        "partner",
        "配置伙伴门户",
        "首次创建当前租户唯一伙伴资料与冻结分润基线。",
        "critical",
    ),
    PermissionDefinition(
        "partner_customer.manage",
        "partner",
        "管理伙伴客户",
        "创建或终止当前伙伴租户内的 Project 客户映射。",
        "critical",
    ),
    PermissionDefinition(
        "partner_branding.manage",
        "partner",
        "管理伙伴白标",
        "更新当前伙伴门户的纯文本品牌名称、颜色和支持信息。",
        "write",
    ),
    PermissionDefinition(
        "partner_commission.export",
        "partner",
        "导出伙伴分润",
        "导出当前伙伴按币种汇总的非结算分润报表。",
        "critical",
    ),
    PermissionDefinition(
        "partner_delivery.download",
        "partner",
        "下载伙伴交付包",
        "下载当前伙伴不含凭据和内部地址的安全交付清单。",
        "critical",
    ),
    PermissionDefinition(
        "tenant.settings.read", "tenant", "读取租户设置", "读取当前租户的脱敏基础配置。", "read"
    ),
    PermissionDefinition(
        "enterprise_sso.read",
        "identity",
        "读取企业 SSO",
        "读取当前 Workspace 的域名、连接、证书窗口和强制登录安全摘要。",
        "read",
    ),
    PermissionDefinition(
        "enterprise_sso.manage",
        "identity",
        "管理企业 SSO",
        "创建、测试和维护当前 Workspace 的域名、身份连接与强制登录策略。",
        "critical",
    ),
    PermissionDefinition(
        "scim.read",
        "identity",
        "读取 SCIM 目录",
        "读取当前 Workspace 的 SCIM token 状态、同步计数、角色映射与冲突摘要。",
        "read",
    ),
    PermissionDefinition(
        "scim.manage",
        "identity",
        "管理 SCIM 目录",
        "创建和轮换 SCIM token、维护角色映射并处理目录冲突。",
        "critical",
    ),
    PermissionDefinition(
        "tenant_data.read",
        "tenant",
        "读取租户数据生命周期",
        "读取当前租户导出任务、注销状态与安全留存摘要。",
        "read",
    ),
    PermissionDefinition(
        "tenant_data.export",
        "tenant",
        "创建租户数据导出",
        "创建当前租户不含凭据、Prompt 和 Response 的受保护数据导出。",
        "critical",
    ),
    PermissionDefinition(
        "tenant_data.download",
        "tenant",
        "下载租户数据导出",
        "下载当前租户已完成且未过期的加密存储导出制品。",
        "critical",
    ),
    PermissionDefinition(
        "tenant_data.delete",
        "tenant",
        "申请租户注销",
        "冻结当前租户并启动可恢复的数据归档、缓存失效和业务数据擦除流程。",
        "critical",
    ),
    PermissionDefinition(
        "user.manage", "tenant", "管理用户", "创建租户用户并分配固定角色。", "critical"
    ),
    PermissionDefinition(
        "rbac.matrix.manage",
        "tenant",
        "管理权限矩阵",
        "读取并调整固定角色的可授权权限集合。",
        "critical",
    ),
    PermissionDefinition(
        "environment.read", "environment", "读取环境", "读取当前租户的环境边界和状态。", "read"
    ),
    PermissionDefinition(
        "environment.write", "environment", "管理环境", "创建、启停和维护当前租户环境。", "write"
    ),
    PermissionDefinition(
        "project.read", "project", "读取项目", "读取项目、应用及其当前租户归属。", "read"
    ),
    PermissionDefinition(
        "project.write", "project", "管理项目", "创建和维护项目、应用及资源绑定。", "write"
    ),
    PermissionDefinition(
        "virtual_key.read", "key", "读取 Virtual Key", "读取脱敏 Virtual Key 元数据。", "read"
    ),
    PermissionDefinition(
        "virtual_key.write",
        "key",
        "管理 Virtual Key",
        "创建、更新和撤销当前租户 Virtual Key。",
        "critical",
    ),
    PermissionDefinition(
        "provider.read",
        "provider",
        "读取 Provider",
        "读取 Provider 与 KeyPool 的脱敏配置。",
        "read",
    ),
    PermissionDefinition(
        "provider.write",
        "provider",
        "管理 Provider",
        "创建、更新和测试 Provider 与 KeyPool。",
        "critical",
    ),
    PermissionDefinition(
        "provider.health.read",
        "provider",
        "读取 Provider 健康",
        "读取上游健康状态和归一化错误摘要。",
        "read",
    ),
    PermissionDefinition(
        "provider.health.probe",
        "provider",
        "探测 Provider",
        "主动触发当前租户 Provider 健康探测。",
        "write",
    ),
    PermissionDefinition(
        "local_endpoint.read",
        "provider",
        "读取本地模型 Endpoint",
        "读取当前租户本地模型 Endpoint 的脱敏聚合与目录摘要。",
        "read",
    ),
    PermissionDefinition(
        "local_endpoint.manage",
        "provider",
        "管理本地模型 Endpoint",
        "原子注册当前租户本地 Endpoint、初始模型和可选凭据。",
        "critical",
    ),
    PermissionDefinition(
        "local_endpoint.test",
        "provider",
        "测试本地模型 Endpoint",
        "执行当前租户本地 Endpoint 的受限健康与目录探测。",
        "write",
    ),
    PermissionDefinition(
        "provider_error_map.read",
        "provider",
        "读取错误映射",
        "读取 Provider 错误归一规则。",
        "read",
    ),
    PermissionDefinition(
        "provider_error_map.write",
        "provider",
        "管理错误映射",
        "创建和更新 Provider 错误归一规则。",
        "critical",
    ),
    PermissionDefinition("model.read", "model", "读取模型", "读取当前租户模型目录。", "read"),
    PermissionDefinition(
        "model.write", "model", "管理模型", "创建和更新当前租户模型目录。", "write"
    ),
    PermissionDefinition(
        "model_price.read", "model", "读取价格", "读取模型价格时间线和版本。", "read"
    ),
    PermissionDefinition(
        "model_price.write", "model", "管理价格", "创建不可变模型价格计划。", "critical"
    ),
    PermissionDefinition(
        "routing.read", "routing", "读取路由", "读取别名、回退、路由和缓存亲和配置。", "read"
    ),
    PermissionDefinition(
        "routing.write", "routing", "管理路由", "创建和更新别名、回退及路由策略。", "critical"
    ),
    # 配置迁移是独立能力边界：读取权限只开放严格白名单导出，写权限只开放 create-only 原子导入。
    # 两者不能由普通 Provider/Model/路由/Virtual Key CRUD 权限推导，避免交付工具绕过后端最小授权。
    PermissionDefinition(
        "config.transfer.read",
        "migration",
        "导出配置",
        "读取并导出当前租户的严格白名单配置。",
        "read",
    ),
    PermissionDefinition(
        "config.transfer.write",
        "migration",
        "导入配置",
        "以 create-only 方式批量导入配置并生成全新 Virtual Key。",
        "critical",
    ),
    PermissionDefinition(
        "model_quality.read", "routing", "读取模型质量", "读取模型质量、成功率和延迟聚合。", "read"
    ),
    PermissionDefinition(
        "model_quality.write", "routing", "管理模型质量", "维护模型人工质量评分。", "write"
    ),
    PermissionDefinition("policy.read", "security", "读取策略", "读取治理策略和版本摘要。", "read"),
    PermissionDefinition(
        "policy.write", "security", "管理策略", "创建和更新治理策略。", "critical"
    ),
    PermissionDefinition(
        "policy.simulate", "security", "模拟策略", "对脱敏输入执行策略 Dry-Run。", "write"
    ),
    PermissionDefinition(
        "policy.publish", "security", "发布策略", "发布或回滚策略快照。", "critical"
    ),
    PermissionDefinition("dlp.read", "security", "读取 DLP", "读取 DLP 规则的脱敏元数据。", "read"),
    PermissionDefinition("dlp.write", "security", "管理 DLP", "创建和更新 DLP 规则。", "critical"),
    PermissionDefinition(
        "request_log.read",
        "observability",
        "读取调用日志",
        "读取当前租户调用日志、Trace 和保存筛选。",
        "read",
    ),
    PermissionDefinition(
        "request_explanation.read",
        "observability",
        "读取请求解释",
        "读取鉴权、路由、成本和安全解释事实。",
        "read",
    ),
    PermissionDefinition(
        "feedback.use", "observability", "使用反馈", "提交请求反馈并读取租户反馈汇总。", "write"
    ),
    PermissionDefinition(
        "billing.usage.read", "billing", "读取用量", "读取全租户用量、Token 和成本明细。", "read"
    ),
    PermissionDefinition(
        "billing.usage.export", "billing", "导出用量", "导出当前租户用量和成本账单。", "critical"
    ),
    PermissionDefinition(
        "billing.finops.read",
        "billing",
        "读取正式账本",
        "读取 append-only 正式账本和调整记录。",
        "read",
    ),
    PermissionDefinition(
        "billing.finops.write", "billing", "调整正式账本", "创建财务调整和冲正。", "critical"
    ),
    PermissionDefinition(
        "billing.budget.read", "billing", "读取预算", "读取预算规则和执行状态。", "read"
    ),
    PermissionDefinition(
        "billing.budget.write", "billing", "管理预算", "创建和更新预算规则。", "critical"
    ),
    PermissionDefinition(
        "billing.anomaly.read", "billing", "读取成本异常", "读取成本异常和检测器状态。", "read"
    ),
    PermissionDefinition(
        "billing.anomaly.write",
        "billing",
        "处置成本异常",
        "确认、忽略异常并维护临时限流。",
        "critical",
    ),
    # 套餐配置、额度查询和额度调整是三个不同风险面；写权限必须由后端逐请求校验，
    # 不能从 FinOps 或前端页面可见性隐式推导。
    PermissionDefinition(
        "billing.plan.read", "billing", "读取套餐", "读取当前租户套餐与试用状态。", "read"
    ),
    PermissionDefinition(
        "billing.plan.write",
        "billing",
        "管理套餐",
        "创建或修改当前租户套餐与试用周期。",
        "critical",
    ),
    PermissionDefinition(
        "billing.quota.read", "billing", "读取额度", "读取当前租户额度桶与分类消费摘要。", "read"
    ),
    PermissionDefinition(
        "billing.quota.write",
        "billing",
        "调整额度",
        "创建当前租户可审计的额度增减事实。",
        "critical",
    ),
    PermissionDefinition(
        "billing.dispute.read",
        "billing",
        "读取账单争议",
        "读取当前租户账单争议、暂缓标记与调整链路。",
        "read",
    ),
    PermissionDefinition(
        "billing.dispute.create",
        "billing",
        "提交账单争议",
        "提交或取消当前租户本人创建的账单争议。",
        "write",
    ),
    PermissionDefinition(
        "billing.dispute.review",
        "billing",
        "审核账单争议",
        "审核当前租户账单争议；批准仍需正式账本写权限。",
        "critical",
    ),
    PermissionDefinition(
        "audit.read", "audit", "读取审计", "读取当前租户不可变管理审计事实。", "read"
    ),
    PermissionDefinition(
        "audit.export", "audit", "导出审计", "导出不可变审计快照并生成复核哈希。", "critical"
    ),
    PermissionDefinition(
        "webhook.manage",
        "integration",
        "管理 Webhook",
        "管理 Endpoint、死信、重放和归档。",
        "critical",
    ),
    PermissionDefinition(
        "developer_portal.read",
        "developer",
        "读取开发者门户",
        "读取快速开始、SDK 示例和迁移指南。",
        "read",
    ),
    PermissionDefinition(
        "debug_console.use",
        "developer",
        "使用 API 调试台",
        "使用内置 OpenAI-compatible API 调试台。",
        "write",
    ),
    # 企业 Chat 是独立模型调用能力。它只允许经过 Gateway 全治理链路发送短生命周期会话，
    # 不能由项目、模型或调试台读写权限隐式推导，否则撤销 chat.use 将无法实时阻断调用。
    PermissionDefinition(
        "chat.use",
        "chat",
        "使用企业 Chat",
        "使用受 Gateway 全链路治理且不长期保存正文的企业 Chat。",
        "write",
    ),
    # 模板管理、定义读取和调用必须独立授权：Viewer 可只调用最小投影，不能因此读取正文；
    # Developer/Admin 的管理权限也不能隐式替代每次 Chat 调用时的 prompt_template.use 复核。
    PermissionDefinition(
        "prompt_template.read",
        "prompt_template",
        "读取 Prompt 模板",
        "读取当前租户模板、不可变版本历史和授权解密后的定义。",
        "read",
    ),
    PermissionDefinition(
        "prompt_template.write",
        "prompt_template",
        "管理 Prompt 模板",
        "创建、启停、修改元数据并发布当前租户的不可变模板版本。",
        "critical",
    ),
    PermissionDefinition(
        "prompt_template.use",
        "prompt_template",
        "使用 Prompt 模板",
        "在企业 Chat 中调用当前已发布模板，并接收本次内存渲染结果。",
        "write",
    ),
    # 策略模板读取和应用必须独立授权。apply 只是服务端冻结模板的受限命令，
    # Control API 仍会按模板类别复核 policy/routing/budget 领域权限，不能由本权限绕过原始 CRUD。
    PermissionDefinition(
        "policy_template.read",
        "security",
        "读取策略模板",
        "读取当前租户内置策略模板、适配状态和安全影响预览。",
        "read",
    ),
    PermissionDefinition(
        "policy_template.apply",
        "security",
        "应用策略模板",
        "应用服务端冻结的策略模板并原子发布策略快照。",
        "critical",
    ),
    # 单次敏感意图分类会短暂接触企业 Prompt，因此不能由普通安全只读或 DLP 管理权限推导。
    # 后端必须逐请求验证这个独立权限，前端隐藏入口仅用于改善体验。
    PermissionDefinition(
        "intent_risk.classify",
        "security",
        "分类敏感意图",
        "对一次性文本执行本地敏感意图分类，不保存输入正文或命中片段。",
        "write",
    ),
    # 高危会话读取与处置必须拆成两个后端权限：只读报告不能隐式获得冻结/释放能力，
    # manage 也不能绕过 read 的 tenant owner 复核；控制台隐藏按钮不承担授权职责。
    PermissionDefinition(
        "session_risk.read",
        "security",
        "读取会话风险",
        "读取当前租户高危会话、冻结状态和不可变处置摘要。",
        "read",
    ),
    PermissionDefinition(
        "session_risk.manage",
        "security",
        "处置会话风险",
        "冻结或释放已识别会话及其关联 Virtual Key 风险覆盖层。",
        "critical",
    ),
    # 文件读取和上传必须独立授权：业务用户上传并不意味着可管理 DLP，安全人员只读摘要
    # 也不能隐式获得上传能力。原文/抽取正文仍由文件服务加密，不进入权限目录元数据。
    PermissionDefinition(
        "file.read",
        "file",
        "读取文件处理记录",
        "读取当前租户文件名称、解析状态、DLP 摘要和 Token 预估。",
        "read",
    ),
    PermissionDefinition(
        "file.write",
        "file",
        "上传并解析文件",
        "上传当前租户 PDF、DOCX 或 TXT，并执行解析、DLP 预扫描和加密存储。",
        "write",
    ),
    # 安全扫描会读取认证密文并写入不可变安全事实，必须使用独立命令权限；file.read 只允许
    # 查看报告，file.write 只允许接收文件，两者都不能隐式获得这项有成本的后端能力。
    PermissionDefinition(
        "file.security.scan",
        "file",
        "扫描文件 Prompt Injection",
        "对当前租户文件执行隐藏 Prompt Injection 检测并写入不可变安全报告。",
        "write",
    ),
    PermissionDefinition(
        "batch.read",
        "batch",
        "读取批量任务",
        "读取当前租户批量任务、任务状态、成本摘要和授权后的结果。",
        "read",
    ),
    PermissionDefinition(
        "batch.write",
        "batch",
        "管理批量任务",
        "创建、取消和执行当前租户受治理的异步批量任务。",
        "write",
    ),
    # 在线实验会改变真实请求路由，读取与创建必须拆分；write 使用 critical 风险提示，
    # 但最终授权仍由 Control 从当前 tenant 的数据库矩阵逐请求验证。
    PermissionDefinition(
        "experiment.read",
        "experiment",
        "读取模型实验",
        "读取当前租户模型 A/B 实验的安全配置、进度和权威结果。",
        "read",
    ),
    PermissionDefinition(
        "experiment.write",
        "experiment",
        "创建模型实验",
        "创建影响当前租户在线 Alias 流量的不可变模型 A/B 实验。",
        "critical",
    ),
    # Response Cache 的读取、策略变更与主动失效影响面不同，必须独立授权；读取权限只返回
    # 安全策略摘要，任何权限都不允许读取缓存正文、Prompt 指纹或密钥材料。
    PermissionDefinition(
        "response_cache.read",
        "response_cache",
        "读取响应缓存策略",
        "读取当前租户 Response Cache 的安全策略、状态和节省金额摘要。",
        "read",
    ),
    PermissionDefinition(
        "response_cache.write",
        "response_cache",
        "管理响应缓存策略",
        "创建或修改当前租户 Response Cache 的显式启用、TTL、收费和标签策略。",
        "critical",
    ),
    PermissionDefinition(
        "response_cache.invalidate",
        "response_cache",
        "失效响应缓存",
        "按当前租户的受控选择器推进围栏并主动失效 Response Cache。",
        "critical",
    ),
    PermissionDefinition(
        "admin_assistant.query",
        "admin_assistant",
        "使用只读管理助手",
        "查询当前租户的用量、错误、成本或策略安全摘要，不授予任何修改能力。",
        "read",
    ),
    PermissionDefinition(
        "approval.read",
        "approval",
        "读取审批任务",
        "读取当前租户内同时满足目标域授权的审批任务与安全审计摘要。",
        "read",
    ),
    PermissionDefinition(
        "approval.decide",
        "approval",
        "处理审批任务",
        "批准或拒绝当前租户审批任务，且仍须满足目标域写权限与二次验证。",
        "critical",
    ),
    PermissionDefinition(
        "mcp.read",
        "mcp",
        "读取 MCP 治理",
        "读取当前租户 MCP Server、Tool 与调用安全摘要。",
        "read",
    ),
    PermissionDefinition(
        "mcp.server.manage",
        "mcp",
        "管理 MCP Server",
        "注册、刷新、禁用 MCP Server 及轮换候选凭据。",
        "critical",
    ),
    PermissionDefinition(
        "mcp.policy.manage",
        "mcp",
        "管理 MCP 工具策略",
        "审核 Schema 并配置工具风险、审批、权限、授权与脱敏策略。",
        "critical",
    ),
    PermissionDefinition(
        "mcp.network.manage",
        "mcp",
        "管理 MCP 网络策略",
        "管理当前租户 MCP Server 的出站网络策略与熔断边界。",
        "critical",
    ),
    PermissionDefinition(
        "mcp.invoke",
        "mcp",
        "调用 MCP 工具",
        "发起当前租户已审核工具调用并读取本人安全结果。",
        "write",
    ),
    PermissionDefinition(
        "mcp.approve",
        "mcp",
        "审批 MCP 调用",
        "审批当前租户高危 MCP 工具调用。",
        "critical",
    ),
    PermissionDefinition(
        "system.config.read", "system", "读取系统配置", "读取脱敏运行配置和系统状态。", "read"
    ),
)

PERMISSION_CODES: Final[tuple[str, ...]] = tuple(item.code for item in PERMISSION_CATALOG)
PERMISSION_BY_CODE: Final[dict[str, PermissionDefinition]] = {
    item.code: item for item in PERMISSION_CATALOG
}

_DEVELOPER = {
    "console.basic.read",
    "dashboard.read",
    "environment.read",
    "project.read",
    "virtual_key.read",
    "virtual_key.write",
    "provider.health.read",
    "provider.health.probe",
    "local_endpoint.read",
    "local_endpoint.test",
    "provider_error_map.read",
    "model.read",
    "model_price.read",
    "routing.read",
    "model_quality.read",
    "request_log.read",
    "request_explanation.read",
    "feedback.use",
    "webhook.manage",
    "developer_portal.read",
    "debug_console.use",
    "config.transfer.read",
    "config.transfer.write",
    "chat.use",
    "prompt_template.read",
    "prompt_template.write",
    "prompt_template.use",
    "file.read",
    "file.write",
    "file.security.scan",
    "batch.read",
    "batch.write",
    "experiment.read",
    "experiment.write",
    "mcp.read",
    "mcp.server.manage",
    "mcp.invoke",
    "branding.read",
}
_VIEWER = {
    "console.basic.read",
    "dashboard.read",
    "chat.use",
    "prompt_template.use",
    "file.read",
    "file.write",
    "file.security.scan",
    "partner_portal.read",
    "partner_customer.manage",
    "partner_branding.manage",
    "partner_commission.export",
    "partner_delivery.download",
    "branding.read",
    "enterprise_sso.read",
    "scim.read",
}
_FINANCE = {
    "console.basic.read",
    "dashboard.read",
    "model.read",
    "model_price.read",
    "model_price.write",
    "billing.usage.read",
    "billing.usage.export",
    "billing.finops.read",
    "billing.finops.write",
    "billing.budget.read",
    "billing.budget.write",
    "billing.anomaly.read",
    "billing.anomaly.write",
    "billing.plan.read",
    "billing.plan.write",
    "billing.quota.read",
    "billing.quota.write",
    "billing.dispute.read",
    "billing.dispute.create",
    "billing.dispute.review",
    "policy_template.read",
    "policy_template.apply",
    "response_cache.read",
    "response_cache.write",
    "response_cache.invalidate",
    "admin_assistant.query",
    "approval.read",
    "approval.decide",
    "branding.read",
}
_SECURITY = {
    "console.basic.read",
    "dashboard.read",
    "provider_error_map.read",
    "provider_error_map.write",
    "model.read",
    "model_price.read",
    "policy.read",
    "policy.write",
    "policy.simulate",
    "policy.publish",
    "dlp.read",
    "dlp.write",
    "request_log.read",
    "request_explanation.read",
    "billing.anomaly.read",
    "billing.anomaly.write",
    "audit.read",
    "audit.export",
    "webhook.manage",
    "policy_template.read",
    "policy_template.apply",
    "intent_risk.classify",
    "session_risk.read",
    "session_risk.manage",
    "file.read",
    "file.security.scan",
    "admin_assistant.query",
    "approval.read",
    "approval.decide",
    "mcp.read",
    "mcp.policy.manage",
    "mcp.network.manage",
    "mcp.approve",
    "branding.read",
    "tenant_data.read",
    "enterprise_sso.read",
    "enterprise_sso.manage",
    "scim.read",
    "scim.manage",
}
_OPS = {
    "console.basic.read",
    "dashboard.read",
    "product_analytics.read",
    "customer_health.read",
    "environment.read",
    "environment.write",
    "project.read",
    "project.write",
    "virtual_key.read",
    "virtual_key.write",
    "provider.read",
    "provider.write",
    "provider.health.read",
    "provider.health.probe",
    "local_endpoint.read",
    "local_endpoint.manage",
    "local_endpoint.test",
    "provider_error_map.read",
    "model.read",
    "model.write",
    "model_price.read",
    "routing.read",
    "routing.write",
    "model_quality.read",
    "model_quality.write",
    "request_log.read",
    "request_explanation.read",
    "webhook.manage",
    "system.config.read",
    "config.transfer.read",
    "config.transfer.write",
    "policy_template.read",
    "policy_template.apply",
    "file.read",
    "batch.read",
    "batch.write",
    "experiment.read",
    "experiment.write",
    "mcp.read",
    "mcp.server.manage",
    "mcp.network.manage",
    "branding.read",
}

# 这些集合是产品授权上限和新租户默认值；运行时必须读取租户矩阵，绝不能在矩阵损坏时回落此处。
ROLE_GRANTABLE_PERMISSIONS: Final[dict[RoleCode, frozenset[str]]] = {
    ADMIN_ROLE: frozenset(PERMISSION_CODES),
    DEVELOPER_ROLE: frozenset(_DEVELOPER),
    VIEWER_ROLE: frozenset(_VIEWER),
    FINANCE_ADMIN_ROLE: frozenset(_FINANCE),
    SECURITY_ADMIN_ROLE: frozenset(_SECURITY),
    OPS_ADMIN_ROLE: frozenset(_OPS),
}


def canonical_permission_codes(values: Iterable[str]) -> tuple[str, ...]:
    """校验并返回去歧义的权限 code 元组。

    参数：权限 code 可迭代对象。返回：按字典序排序的唯一元组。
    约束：重复、非字符串、非规范小写或目录外 code 均拒绝，避免不同正文得到同一矩阵事实。
    """

    normalized: list[str] = []
    for value in values:
        # 精确类型与规范格式先行校验，避免 bool/对象或大小写变体形成不同矩阵字节语义。
        if type(value) is not str or _PERMISSION_CODE.fullmatch(value) is None:
            raise ValueError("rbac.permission_code_invalid")
        # 规范但未登记的 code 也必须拒绝，禁止客户端自行扩展权限目录。
        if value not in PERMISSION_BY_CODE:
            raise ValueError("rbac.permission_code_invalid")
        normalized.append(value)
    # 重复权限会让集合、JSON 和数据库关系产生多种表示，故不能静默去重。
    if len(normalized) != len(set(normalized)):
        raise ValueError("rbac.permission_code_duplicate")
    return tuple(sorted(normalized))


def permission_matrix_sha256(values: Iterable[str]) -> str:
    """计算角色权限集合的稳定 SHA-256。

    规范输入只包含排序后的 permission code；不混入名称、时间或 tenant，便于数据库和客户端复核。
    """

    canonical = canonical_permission_codes(values)
    payload = json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def authorization_fingerprint(
    *,
    tenant_id: str,
    user_id: str,
    role: str,
    permission_revision: int,
    matrix_sha256: str,
) -> str:
    """计算当前用户授权身份指纹，供长生命周期页面检测降权。

    指纹只含安全关联 ID、角色、revision 和矩阵哈希，不包含邮箱、Session Token 或权限页面正文。
    """

    if (
        type(tenant_id) is not str
        or not tenant_id
        or type(user_id) is not str
        or not user_id
        or role not in ROLE_CODES
        or type(permission_revision) is not int
        or permission_revision < 1
        or type(matrix_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", matrix_sha256) is None
    ):
        # 指纹输入任一维度不完整时失败关闭，不能退化为仅角色或仅 tenant 的弱 owner。
        raise ValueError("rbac.authorization_invalid")
    payload = json.dumps(
        [tenant_id, user_id, role, permission_revision, matrix_sha256],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_catalog() -> None:
    """启动或测试时校验共享目录自身完整性。

    目录重复、非法 code、未覆盖角色或越界授权说明发布包损坏，必须失败关闭而不是删掉未知项继续运行。
    """

    # 目录 code 必须唯一，否则 hash、关系表和前端 checkbox 无法形成一一对应。
    if len(PERMISSION_CODES) != len(set(PERMISSION_CODES)):
        raise ValueError("rbac.catalog_duplicate")
    # 非规范 code 不得进入迁移模板或 API，避免不同客户端产生大小写/标点双义。
    if any(_PERMISSION_CODE.fullmatch(code) is None for code in PERMISSION_CODES):
        raise ValueError("rbac.catalog_invalid")
    # 六角色上限必须完整覆盖固定目录，缺失角色不能靠调用方默认值补齐。
    if set(ROLE_GRANTABLE_PERMISSIONS) != set(ROLE_CODES):
        raise ValueError("rbac.role_catalog_invalid")
    catalog = set(PERMISSION_CODES)
    # 不可变 admin 必须拥有目录全集，确保租户始终保留权限矩阵恢复入口。
    if ROLE_GRANTABLE_PERMISSIONS[ADMIN_ROLE] != catalog:
        raise ValueError("rbac.admin_catalog_invalid")
    # 任何角色上限越过 catalog 都说明发布包损坏，不能在运行时删掉未知权限继续启动。
    if any(not permissions <= catalog for permissions in ROLE_GRANTABLE_PERMISSIONS.values()):
        raise ValueError("rbac.role_catalog_invalid")


validate_catalog()
