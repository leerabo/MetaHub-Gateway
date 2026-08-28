"""Generate the authoritative, extensible SaaS roadmap starting at PRD-090."""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx"
PLAN_DATE = "2026-08-22"
FIRST_PRD = 90
COMPLETED_THROUGH_PRD = 130


def prd(
    number: int,
    stage: str,
    priority: str,
    layer: str,
    module: str,
    title: str,
    users: str,
    story: str,
    scope: str,
    boundary: str,
    pages: str,
    apis: tuple[str, ...],
    objects: tuple[str, ...],
    events: tuple[str, ...],
    permissions: str,
    acceptance: str,
    dependencies: str,
    migration: str,
    security: str,
    tests: str,
    slo: str,
    effort: str,
    team: str,
) -> dict[str, object]:
    return {
        "id": f"PRD-{number:03d}",
        "status": "已完成" if number <= COMPLETED_THROUGH_PRD else "未启动",
        "stage": stage,
        "priority": priority,
        "layer": layer,
        "module": module,
        "title": title,
        "users": users,
        "story": story,
        "scope": scope,
        "boundary": boundary,
        "pages": pages,
        "apis": apis,
        "objects": objects,
        "events": events,
        "permissions": permissions,
        "acceptance": acceptance,
        "dependencies": dependencies,
        "migration": migration,
        "security": security,
        "tests": tests,
        "slo": slo,
        "effort": effort,
        "team": team,
        "evidence": "设计文档、两轮编码前自审、迁移往返、专项测试、必要回归、真实 API/浏览器/运行验收、安全与禁止文件审计",
    }


INITIAL_DRAFT_PRDS = [
    prd(
        90,
        "S0-路线重基线",
        "P0",
        "平台基础层",
        "SaaS架构",
        "SaaS总体架构与兼容迁移基线",
        "产品负责人/架构师/研发/SRE/安全",
        "作为平台负责人，我希望冻结旧私有化路线并形成可执行的SaaS与云管私有网关总架构。",
        "废弃旧PRD-090..108；冻结个人SaaS、企业SaaS、云管私有Gateway三种标准形态，以及完全隔离环境的签名包异步管理能力；定义全局身份、Workspace、平台域与租户域、云边信任、数据驻留、密钥托管、迁移波次和兼容期；形成ADR、威胁模型、服务边界和目标拓扑。",
        "不编码业务功能；不修改PRD-001..089完成事实；兼容旧完成标记必须保持false；不承诺完全离线环境可被云端实时管理。",
        "架构决策记录、路线图、迁移评估页原型",
        ("无公开业务API；冻结平台/租户/边缘内部合同草案",),
        ("GlobalAccount", "Workspace", "WorkspaceTenantBinding", "WorkspaceMembership", "PlatformRole", "EdgeDeployment", "DesiredState"),
        (),
        "冻结平台角色与租户角色两套命名空间；任何后续PRD不得混用",
        "两轮独立设计自审关闭P0/P1；现有表/API逐项给出保留、扩展、迁移或废弃结论；所有后续PRD有明确依赖和回滚边界；旧090..108被权威标记为禁止开发。",
        "PRD-001..089现状、系统架构设计、当前迁移0082",
        "只新增文档与合同基线；不得迁移生产数据；回滚为撤销路线版本，不改业务库。",
        "完成数据流图、STRIDE威胁模型、租户逃逸面、平台支持越权面、云边密钥边界和中国境内数据驻留边界。",
        "架构一致性、旧路线引用扫描、依赖无环、权限命名冲突、故障模式和回滚可执行性。",
        "定义后续统一SLO：Control读p95<300ms、可交互页面p75<1.5s、Gateway平台开销p95<20ms（不含上游）。",
        "2-3人周",
        "架构1、后端1、安全/SRE各0.5",
    ),
    prd(
        91,
        "S1-全局身份",
        "P0",
        "平台身份层",
        "全局账号",
        "全局用户账号与认证凭据",
        "账号本人",
        "作为用户，我希望一个账号可以拥有个人空间并加入多个企业，而不是账号被固定在单一租户。",
        "将登录主体从tenant-bound User拆分为GlobalAccount；支持密码哈希、邮箱/手机号规范化、账号状态、MFA预留、凭据轮换、全局Session主体；保留现有租户用户兼容投影。Workspace与Membership由PRD-093/094承接。",
        "不在本条实现公开注册、SSO或SCIM；不允许全局账号直接访问任意Workspace业务数据。",
        "账号安全设置、会话与设备列表",
        ("POST /identity/login", "GET /identity/me", "POST /identity/logout", "GET /identity/sessions", "DELETE /identity/sessions/{session_id}", "POST /identity/credentials/password/rotate"),
        ("GlobalAccount", "AccountCredential", "GlobalSession", "AccountIdentityLink"),
        ("identity.login_succeeded", "identity.login_failed", "identity.session_revoked", "identity.credential_rotated"),
        "仅账号本人；GlobalSession不授予平台或Workspace权限，后续必须从独立Platform/Workspace上下文恢复授权",
        "同一邮箱账号可关联多个Workspace；登录Token不包含可伪造tenant选择；现有admin账号可无损映射；失败、锁定和撤销均返回统一错误与request_id。",
        "PRD-090",
        "新增全局表并双写兼容映射；先回填再切读；迁移前后账号数/哈希不变；回滚继续使用旧登录且不丢Session撤销事实。",
        "密码/验证码/Token不入日志；防账号枚举；Session绑定设备与风险摘要；平台账号与客户账号逻辑隔离。",
        "哈希与锁定、并发登录/撤销、旧账号回填、重复身份竞争、跨Workspace零授权、错误隐私。",
        "登录API p95<500ms；Session校验p95<30ms；并发撤销后下一请求立即失效。",
        "4-5人周",
        "后端2、前端1、测试1、安全0.5",
    ),
    prd(
        92,
        "S1-全局身份",
        "P0",
        "平台身份层",
        "自助注册",
        "个人与企业用户自助注册验证",
        "未登录个人/企业注册人",
        "作为新用户，我希望自助注册、验证身份并安全找回账号。",
        "注册、邮箱验证、验证码重发、密码设置、找回密码、同形响应、防滥用限流、同意隐私条款版本；支持企业邀请验证接续和已有全局账号的密码加邮箱双重证明；未验证邮箱只以HMAC和owner-bound AES-256-GCM信封落库。",
        "不在本条创建Workspace业务资源；不做手机号或社交登录；不按邮箱/Hash合并账号；不把验证码或找回Token放入URL查询、日志或Storage。",
        "注册页、验证页、找回密码页、邀请接续页",
        ("POST /identity/registrations", "POST /identity/verifications", "POST /identity/verifications/resend", "POST /identity/password-resets", "POST /identity/password-resets/confirm"),
        (
            "RegistrationIntent",
            "VerificationChallenge",
            "PasswordResetIntent",
            "AccountVerification",
            "ConsentRecord",
            "AbuseCounter",
            "InvitationIdentityBinding",
            "IdentityLifecycleEvent",
            "IdentityLifecycleOutbox",
        ),
        (
            "identity.challenge_dispatched",
            "identity.challenge_delivery_failed",
            "identity.registered",
            "identity.verified",
            "identity.password_reset",
        ),
        "公开入口仅能操作一次性意图；账号本人完成验证后获得全局Session",
        "注册全流程可恢复；重复邮箱不泄露存在性；验证码过期/重放/并发只成功一次；邀请用户注册后继续原邀请；所有响应带request_id。",
        "PRD-091、现有PRD-060邀请",
        "0084新增全局Identity intent/challenge/verification/consent/binding/event表；现有邀请Token合同保持兼容；pristine可down，任何安全事实阻断回滚；已创建账号不可删除。",
        "验证码使用独立Challenge根密钥分域HMAC；未验证邮箱使用独立PII根密钥和owner-bound AES-256-GCM；频率/设备/IP限流、CSRF/Origin、弱密码拒绝、审计最小化、国内隐私同意留痕。",
        "公开黑盒、枚举防护、并发重放、验证码渠道故障、邀请接续、真人鼠标/键盘、移动端兼容。",
        "注册/验证API p95<500ms（不含短信邮件送达）；同IP/账号限流不拖慢正常登录。",
        "4-5人周",
        "后端2、前端1、测试1、安全0.5",
    ),
    prd(
        93,
        "S1-Workspace",
        "P0",
        "租户基础层",
        "空间创建",
        "个人空间与企业Workspace创建",
        "已验证个人/企业Owner",
        "作为已注册用户，我希望创建个人空间或企业，并成为该空间Owner。",
        "统一Workspace模型，kind=personal|organization；创建个人空间、企业名称/slug、境内Region、服务模式、Owner Membership和默认dev环境；企业资料分草稿/确认状态；支持一个账号拥有多个企业。",
        "不自动创建Provider、占位Key、模型、Virtual Key或成功调用；不把系统默认环境伪装为用户已完成配置。",
        "创建空间向导、企业资料页、空间选择页",
        ("POST /workspaces", "GET /workspaces", "GET /workspaces/{workspace_id}", "PATCH /workspaces/{workspace_id}"),
        (
            "Workspace",
            "WorkspaceTenantBinding",
            "WorkspaceMembership",
            "OrganizationProfile",
            "PersonalProfile",
            "WorkspaceRegion",
            "WorkspaceProvisioningJob",
            "WorkspaceEnvironmentOrigin",
            "WorkspaceEvent",
            "WorkspaceEventOutbox",
            "WorkspaceUserProjectionClaim",
        ),
        ("workspace.created", "workspace.profile_confirmed", "workspace.provisioning_failed"),
        "GlobalSession只允许已验证账号创建空间；personal/organization Workspace的owner仅本人可读写；本条不授予平台角色或其他成员权限",
        "注册后可创建个人或企业空间；同一账号可创建并列出多个空间；创建事务不会产生假Provider；失败可重试且不产生半初始化租户；本条不签发WorkspaceSession。",
        "PRD-091、PRD-092、现有Tenant/Environment",
        "0085新增Workspace、不可变WorkspaceTenantBinding、owner foundation、Region/profile/provisioning/environment origin、event/outbox和一次性legacy User投影证明；现有Tenant确定性回填；pristine可down，新增安全事实原子阻断回滚。",
        "GlobalSession与CSRF/Origin边界；Idempotency-Key只存HMAC并绑定规范body；slug冲突同形；企业名输出转义且不进日志/事件/幂等记录；Region允许列表失败关闭；Owner不可孤儿。",
        "个人/企业分支、创建幂等、Owner原子性、默认环境来源标签、跨Workspace隔离、失败补偿。",
        "Workspace创建p95<1s；后台Provisioning不阻塞首屏超过2s。",
        "4-5人周",
        "后端2、前端1、测试1",
    ),
    prd(
        94,
        "S1-Workspace",
        "P0",
        "租户身份层",
        "成员上下文",
        "成员关系、空间切换与邀请升级",
        "Workspace Owner/管理员/成员",
        "作为加入多个企业的成员，我希望登录后选择空间，并在不重新输入租户标识的情况下安全切换。",
        "完整Membership、Owner转移、成员停用、MembershipInvitation升级桥、空间选择器、独立WorkspaceSession、后端导航投影和跨标签页失效；企业owner/admin在委派上限内分配admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer，service_account只保留机器主体枚举。",
        "前端不得通过tenant_id参数切换；GlobalSession不授予tenant权限；Legacy Bearer、Platform/Support/Device主体不能解释为WorkspaceSession或Membership；service_account不能签发浏览器Session；本条不做SSO/SCIM。",
        "空间选择器、成员列表、Owner转移、邀请页",
        ("GET /identity/workspaces", "POST /identity/workspace-switches", "GET /workspace/session", "GET /memberships", "PATCH /memberships/{membership_id}", "POST /membership-invitations", "POST /workspace-owner-transfers；GET /console/navigation接受WorkspaceSession"),
        ("WorkspaceMembership", "WorkspaceSession", "MembershipInvitation", "OwnerTransfer", "MembershipEvent", "MembershipEventOutbox"),
        ("membership.invited", "membership.created", "membership.updated", "membership.disabled", "invitation.accepted", "workspace.switched", "workspace.owner_transferred"),
        "租户owner/admin邀请、停用并分配企业职能角色；admin不得授予owner或突破自身权限；Owner转移需二次验证；developer/viewer后端拒绝",
        "登录页不再要求租户标识；单空间直达、多空间可选；企业管理员可添加职能用户；每种角色只收到后端计算的页面清单；直达URL/API仍逐请求鉴权；切换后旧请求/缓存/草稿立即失效；跨租户资源同形404；最后Owner不能被停用。",
        "PRD-091..093、PRD-059/060",
        "0086从0085重建完整Membership，回填legacy link并校验唯一owner与复合所有权；新增WorkspaceSession、MembershipInvitation、OwnerTransfer、MembershipEvent/Outbox；pristine可down，任何新授权事实、revision变化、Session、邀请消费或转移均原子阻断回滚。",
        "Workspace Cookie与METAHUB_WORKSPACE_SESSION_SECRET独立于Global/Legacy/Challenge/Cache等域；每请求按数据库权威复核account/workspace/membership/legacy permission revision；Owner转移密码重认证；事件不含邮箱、Token、Cookie或密码。",
        "权限矩阵、并发切换、迟到响应、Owner竞争、邀请已存在账号、跨标签页、浏览器Storage清理。",
        "空间切换API p95<300ms；切换后首个页面可交互p75<1.5s。",
        "5-6人周",
        "后端2、前端1、测试1、安全0.5",
    ),
    prd(
        95,
        "S2-平台运营",
        "P0",
        "平台治理层",
        "平台IAM",
        "SaaS平台运营角色与受控支持访问",
        "SaaS总管理员/运维/安全/财务/客服",
        "作为SaaS运营团队，我希望管理平台但默认不能读取客户敏感业务内容。",
        "平台角色platform_owner/platform_ops/platform_security/platform_billing/platform_support/platform_verification/platform_compliance/platform_privacy/release_manager/platform_provider_engineer/platform_channel/platform_customer_success/platform_growth；每个角色独立页面与动作矩阵；员工账号MFA；JIT支持访问申请、客户授权、双人审批、原因、时限、撤销；break-glass独立流程；平台操作不可变审计。",
        "平台角色不等于租户admin；禁止永久模拟客户；支持访问不允许读取Provider Key、Token、Prompt、Response。",
        "平台员工与角色、角色页面授权、支持访问申请、审批与审计页",
        ("POST /identity/mfa/totp/enrollments", "POST /identity/mfa/totp/verifications", "GET /identity/platform-access", "POST /platform/bootstrap", "POST /platform/sessions", "GET /platform/session", "GET /platform/navigation", "GET/POST /platform/operators", "PATCH /platform/operators/{operator_id}", "GET/POST /platform/support-grants", "POST /platform/support-grants/{grant_id}/approve", "POST /workspace/support-grants/{grant_id}/approve", "POST /platform/support-grants/{grant_id}/sessions", "POST .../{grant_id}/revoke", "GET /support/workspace/diagnostics", "POST /platform/break-glass-sessions", "POST .../{id}/reviews", "GET /platform/audit-logs"),
        ("AccountMfaFactor", "PlatformBootstrapState", "PlatformOperator", "PlatformRole", "PlatformPermission", "PlatformSession", "SupportGrant", "SupportGrantApproval", "SupportSession", "BreakGlassSession", "BreakGlassReview", "PlatformAuditLog", "PlatformEventOutbox"),
        ("platform.operator_changed", "support.access_requested", "support.access_granted", "support.access_revoked", "break_glass.used"),
        "平台角色与租户角色命名空间隔离；platform_support只能通过SupportGrant获得固定只读诊断权限",
        "无平台角色不能访问/platform；真实TOTP与一次性bootstrap完成；每个角色只收到后端计算的导航和页面数据；直达URL/API再次鉴权；support不能自行审批；到期自动撤权；租户内容字段后端裁剪；审计使用会话/角色绑定HMAC游标；所有平台动作有actor/reason/request_id。",
        "PRD-090、PRD-091、PRD-094",
        "0087从完整0086 expand TOTP、bootstrap、Platform IAM/Session、Support grant/approval/session、break-glass review和不可变审计/outbox；pristine可往返，任何安全事实存在时down原子失败。禁止按admin@metahub.local或任何存量邮箱自动提权；成功bootstrap后数据库tombstone永久关闭入口。",
        "强制真实MFA与counter防重放；Global/Workspace/Platform/Support主体、Cookie和根密钥分域；JIT双域审批；逐请求动态撤权；不可变审计；客服最小权限；break-glass告警和事后复核；秘密不进日志、DOM、Storage或证据。",
        "0086->0087/fresh/down/up、角色目录、权限白盒、越权黑盒、MFA加密/重放/限流、bootstrap并发、100并发审批/Session、游标绑定、到期撤销、敏感裁剪、break-glass双复核、动态撤权与三视口浏览器流程。",
        "平台授权warm p95<50ms；Operator/grant列表warm p95<300ms；撤权传播<5s。",
        "5-6人周",
        "后端2、前端1、测试1、安全1",
    ),
    prd(
        96,
        "S2-平台运营",
        "P0",
        "平台治理层",
        "租户运营",
        "平台租户目录与生命周期运营台",
        "platform_ops/platform_customer_success/platform_support/platform_security/platform_billing",
        "作为平台运维，我希望查看租户健康、套餐和部署状态，并安全执行冻结、恢复和联系Owner。",
        "全局租户目录、脱敏搜索、状态/Region/计划/部署筛选、健康摘要、冻结/恢复、风险标签、联系Owner通知、数据导出/删除进度聚合；平台动作与租户动作分离。",
        "不展示请求正文、Provider Endpoint全文、Key、Token或客户成员列表明文；不允许平台直接编辑租户Provider/策略。",
        "平台总览、租户目录、租户运营详情、事件时间线",
        ("GET /platform/workspaces", "GET /platform/workspaces/{workspace_id}", "GET /platform/workspaces/{workspace_id}/health", "POST /platform/workspaces/{workspace_id}/suspend", "POST /platform/workspaces/{workspace_id}/restore", "POST/DELETE /platform/workspaces/{workspace_id}/risk-tags", "POST /platform/workspaces/{workspace_id}/owner-notifications", "GET /platform/workspaces/{workspace_id}/events"),
        ("WorkspaceOperationalState", "WorkspaceRiskTag", "WorkspaceHealthProjection", "PlatformActionIntent", "WorkspaceOwnerNotificationOutbox", "WorkspaceOperationalEvent", "WorkspaceOperationalEventOutbox"),
        ("workspace.suspended", "workspace.restored", "workspace.risk_tag_added", "workspace.risk_tag_removed", "workspace.owner_notification_requested"),
        "platform_ops可生命周期操作；platform_customer_success管理客户运营事实；platform_support只读；platform_security管理风险；platform_billing只读计划/欠费状态",
        "平台可管理多租户且无跨租户内容泄漏；冻结撤销WorkspaceSession并阻止新Session/tenant资源写；Gateway明确展示transition_pending而不伪造传播；恢复与风险操作使用独立CAS；关键动作需确认、原因、幂等和审计。",
        "PRD-095、现有PRD-086/089",
        "0088从完整0087回填operational/health投影并安装N/N-1 Session失败关闭guard；pristine可down/re-up，任何生命周期、风险、通知、事件或Session撤销事实存在时down原子失败。",
        "exact ID/slug搜索、防枚举、HMAC游标绑定Session/角色/Operator revision/筛选摘要、固定健康与风险投影、Owner邮箱不落outbox、平台与租户Audit分域。",
        "0087->0088/fresh/down/up、五角色字段裁剪、游标篡改/跨角色、health fresh/stale/坏源、风险职责隔离、冻结恢复CAS/幂等/并发、Session撤权、Owner通知无邮箱、Console迟到响应与390响应布局。",
        "10万租户目录查询p95<500ms；单租户详情p95<300ms。",
        "5-6人周",
        "后端2、前端1、测试1、SRE0.5",
    ),
    prd(
        97,
        "S2-激活体验",
        "P0",
        "产品体验层",
        "SaaS引导",
        "个人与企业初始化向导重构",
        "个人Owner/企业Owner/管理员",
        "作为首次登录用户，我希望系统明确告诉我先做什么，并能查看或修改每个步骤。",
        "按personal/organization区分向导；步骤来源标记system_created/user_confirmed/verified；已完成步骤可点击；企业资料需确认；Provider完成要求真实可解密凭据或免密本地Endpoint且探测成功；移除seed占位Provider/Key/模型/Virtual Key；提供跳过、继续和失败诊断。",
        "不得用active占位行推断完成；不得生成演示响应；不得把研发术语、表名、PRD号显示给普通用户。",
        "首次进入首页、初始化中心、步骤详情与恢复入口",
        ("GET /workspaces/current/onboarding", "PATCH /workspaces/current/onboarding/steps/{step_id}", "POST /workspaces/current/onboarding/recheck"),
        ("OnboardingProgress", "OnboardingStepFact", "OnboardingAcknowledgement", "ActivationCheckpoint"),
        ("onboarding.step_completed", "onboarding.step_reopened", "workspace.activated"),
        "owner/admin可操作；developer可完成技术步骤；个人owner拥有本空间完整初始化权限",
        "空Workspace初始0个真实接入步骤完成；系统预置与用户配置清晰区分；所有步骤可查看；真实Provider前置成功后才完成；八步全流程可由真人鼠标操作完成。",
        "PRD-093/094、现有Provider/Model/Project/VirtualKey/Usage",
        "现有seed记录标记legacy_seed并不计完成；不得删除客户真实资源；可回滚到旧首页但不能恢复假完成判断。",
        "Provider Key只在提交中出现；进度API只返回安全布尔事实；后端权限权威；错误带request_id。",
        "空库/旧库、占位Key、真实Key、免密本地Endpoint、探测失败、已完成跳转、动态撤权、UCD与全浏览器矩阵。",
        "向导摘要p95<300ms；步骤跳转立即响应；页面可交互p75<1.5s。",
        "4-5人周",
        "前端1.5、后端1.5、测试1、UCD0.5",
    ),
    prd(
        98,
        "S2-商业化",
        "P1",
        "商业平台层",
        "订阅套餐",
        "SaaS套餐、试用、订阅与Entitlement",
        "个人Owner/企业Owner/平台财务/销售",
        "作为客户，我希望选择适合个人或企业的套餐，并清楚知道配额和到期影响。",
        "扩展现有Plan/Trial为平台产品目录、版本化价格、Subscription、Entitlement、试用、人工订单、续费/降配计划、宽限期；套餐限制Provider、Key、并发、Token、日志留存、成员和私有部署节点。",
        "首版不接在线支付和自动开票；不在热路径实时请求计费系统；降配不立即删除既有资源。",
        "公开价格页、选择套餐、订阅详情、平台套餐管理",
        ("GET /catalog/plans", "POST /subscriptions", "GET /subscriptions/current", "POST /subscriptions/current/change-plan", "GET /platform/plans", "POST /platform/plans"),
        ("PlanCatalog", "PlanVersion", "Subscription", "EntitlementSnapshot", "Trial", "OrderRecord"),
        ("subscription.created", "subscription.plan_changed", "subscription.expiring", "entitlement.changed"),
        "租户owner管理订阅；platform_billing管理产品目录；其他租户角色只读或无权",
        "个人/企业套餐独立；Entitlement版本冻结并可追踪；超限错误可解释；过期进入宽限且不突然中断存量调用；所有变更幂等。",
        "PRD-093、PRD-095、现有PRD-079",
        "现有Plan/Bucket映射到新PlanVersion/Subscription；双读校验额度一致；回滚继续使用最后有效Entitlement。",
        "价格与金额Decimal/整数最小单位；平台财务权限分离；订单不含支付卡信息；防客户端篡改套餐。",
        "套餐状态机、并发变更、试用到期恢复、降配存量、跨租户、时区、账务舍入、浏览器购买流程。",
        "套餐读p95<200ms；Entitlement本地缓存命中p95<5ms。",
        "5-6人周",
        "后端2、前端1、测试1、财务产品0.5",
    ),
    prd(
        99,
        "S2-商业化",
        "P1",
        "商业平台层",
        "SaaS计量",
        "平台级用量计量、配额结算与账单摘要",
        "租户财务/平台财务/运营",
        "作为平台和客户财务，我希望平台用量、Provider成本和套餐消费可以核对。",
        "复用Usage/FinOps Ledger，新增平台Meter、月度周期、订阅配额分配、超额量、失败请求分类、账单摘要、调整和租户对账导出；区分上游成本、平台计费量和客户自带Key。",
        "不保存Prompt/Response；不把估算成本当正式应收；首版不做税务发票和自动扣款。",
        "订阅用量、账单摘要、平台收入/成本汇总、对账导出",
        ("GET /billing/summary", "GET /billing/usage", "GET /platform/billing/summary", "POST /platform/billing/adjustments", "GET /billing/reconciliation-export"),
        ("MeterDefinition", "MeterReading", "BillingCycle", "QuotaAllocation", "BillingSummary", "BillingAdjustment"),
        ("quota.threshold_reached", "billing.cycle_closed", "billing.adjusted", "billing.reconciliation_ready"),
        "租户finance_admin/admin查看；platform_billing管理调整；platform_ops只看健康计数",
        "每个成功、失败和取消请求可追踪；租户账本与平台Meter可按request_id/周期对账；重复事件不重复计费；跨币种不隐式换算。",
        "PRD-098、现有Usage/FinOps/Budget",
        "以ledger_sequence增量投影，旧账本不改写；可重建Meter；回滚停用新投影但保留append-only事实。",
        "账单调整双人复核；金额不可浮点漂移；导出防公式注入；平台汇总不得泄露其他租户明细。",
        "计量幂等、迟到事件、失败请求、周期关闭、调整冲正、跨租户、10亿账本行查询计划、CSV安全。",
        "热路径异步计量；Gateway附加开销<2ms；月度汇总任务可断点恢复。",
        "6-7人周",
        "后端2、数据1、前端1、测试1、财务产品0.5",
    ),
    prd(
        100,
        "S2-生产基线",
        "P0",
        "云基础设施层",
        "数据平台",
        "PostgreSQL、Redis、对象存储与KMS生产底座",
        "SRE/研发/安全",
        "作为SaaS运维，我希望多实例服务使用可扩展、可备份且隔离明确的生产数据底座。",
        "正式PostgreSQL迁移适配、连接池、Redis缓存/限流/短租约、对象存储制品、KMS/Envelope Encryption、迁移协调锁、备份恢复、分区/索引/归档、SQLite开发兼容；提供环境基线和IaC。",
        "不在本条拆成大量微服务；SQLite仅保留本地开发/测试；不把数据库RLS作为唯一授权。",
        "平台基础设施状态、备份与迁移状态页",
        ("GET /platform/infrastructure/status", "POST /platform/backups", "GET /platform/backups/{backup_id}"),
        ("DatabaseCluster", "CacheCluster", "ObjectStore", "KeyRing", "BackupSet", "MigrationLease"),
        ("infrastructure.degraded", "backup.completed", "backup.failed", "migration.completed"),
        "platform_ops运维；platform_security管理密钥策略；租户不可访问基础设施接口",
        "PostgreSQL从空库和迁移库均通过；多实例无SQLite锁争用；备份可恢复；KMS不可用按固定策略失败；所有tenant复合归属约束保留。",
        "PRD-090、PRD-095；可与091..099局部并行设计",
        "建立SQLite->PostgreSQL校验工具和双写/停机窗口方案；逻辑校验hash、行数、FK和账本水位；失败回到SQLite或旧Postgres版本。",
        "TLS、KMS、凭据轮换、最小数据库角色、RLS纵深防御、备份加密、对象存储租户前缀和审计。",
        "SQL方言、迁移往返、连接池耗尽、Redis/对象存储/KMS故障、备份恢复、并发迁移、多租户隔离。",
        "Control普通读p95<300ms；数据库连接池饱和可降级；RPO<=15分钟、RTO<=2小时基线。",
        "8-10人周",
        "后端2、SRE2、数据1、测试1、安全0.5",
    ),
    prd(
        101,
        "S2-生产基线",
        "P0",
        "云数据面层",
        "云Gateway",
        "多租户云Gateway池与区域路由",
        "个人用户/企业客户/平台SRE",
        "作为SaaS客户，我希望无需部署本地软件即可获得隔离、稳定的模型调用入口。",
        "共享Gateway池、Region/Cell、租户路由、容量权重、过载保护、租户并发隔离、配置快照、灰度发布、专属池预留、健康摘除；复用现有Provider Adapter、Policy、Budget、Usage。",
        "不允许按前端tenant参数路由；不让单租户耗尽全局连接池；不跨Region静默转移数据。",
        "Gateway Endpoint与区域设置、平台Cell状态页",
        ("POST /v1/chat/completions", "POST /v1/embeddings", "GET /platform/gateway-cells", "PATCH /platform/workspaces/{workspace_id}/gateway-placement"),
        ("GatewayCell", "WorkspacePlacement", "CapacityLease", "ConfigSnapshotLease", "GatewayRelease"),
        ("gateway.cell_degraded", "gateway.workspace_rebalanced", "gateway.release_changed"),
        "Virtual Key恢复Workspace；platform_ops仅管理placement；租户不能指定其他Cell或Workspace",
        "100+租户并发无串租；噪声租户被限流且不拖垮邻居；Cell故障按Region策略恢复；所有用量、成本和失败请求仍可追踪。",
        "PRD-094、098..100、现有Gateway",
        "先单Cell影子流量，再小租户灰度；配置generation可回退；不改变现有OpenAI-compatible合同。",
        "mTLS内部通信、配置签名、tenant-bound缓存、SSRF/Provider ACL、DLP/预算前置、日志无正文。",
        "跨租户压力、噪声邻居、Cell故障、配置陈旧、流式卡死、Provider故障、灰度回滚、区域边界。",
        "平台附加开销p95<20ms；单Cell可用性99.9%基线；过载时固定429/503和request_id。",
        "8-10人周",
        "Gateway2、后端1、SRE2、测试1、安全0.5",
    ),
    prd(
        102,
        "S3-生产SaaS",
        "P0",
        "企业安全层",
        "企业身份与合规",
        "企业域名、SSO/SCIM、MFA与数据驻留",
        "企业Owner/安全管理员/平台安全",
        "作为企业安全负责人，我希望使用企业身份源并控制成员生命周期和数据区域。",
        "企业域名验证、OIDC/SAML SSO、强制MFA策略、SCIM用户/组同步、自动停用、JIT加入开关、Region锁定、数据处理与留存策略、合规同意版本；个人空间保留本地认证。",
        "不允许域名所有权自动授予既有Workspace；不接收IdP密码；不支持未审批的跨境Region。",
        "企业身份、域名验证、SSO、SCIM、数据驻留与留存设置",
        ("POST /enterprise-domains", "POST /enterprise-domains/{domain_id}/verify", "GET/POST /sso-configurations", "POST /scim/v2/Users", "POST /scim/v2/Groups", "PATCH /workspace-security-policy"),
        ("EnterpriseDomain", "SsoConfiguration", "ScimDirectory", "ExternalIdentity", "WorkspaceSecurityPolicy", "DataResidencyPolicy"),
        ("enterprise.domain_verified", "sso.configuration_changed", "scim.membership_changed", "workspace.security_policy_changed"),
        "tenant owner/security_admin管理；平台security只看合规状态，不读取IdP secret",
        "域名挑战不可伪造；SSO断言严格校验；SCIM幂等且停用实时撤权；MFA策略强制；Region不静默变化；本地break-glass受控。",
        "PRD-094/095/100",
        "现有成员默认local身份；开启SSO后分阶段enforce；配置可回滚但已撤销Session不恢复。",
        "SAML签名/OIDC nonce/state/PKCE、SCIM bearer加密、域名接管防护、MFA恢复码、Region合规。",
        "协议合同、重放、时钟漂移、证书轮换、SCIM乱序、账号合并、动态撤权、多浏览器SSO流程。",
        "SSO回调p95<1s（不含IdP）；SCIM批量可恢复；撤权传播<5s。",
        "7-9人周",
        "后端2、前端1、测试1、安全1、企业IAM0.5",
    ),
    prd(
        103,
        "S3-生产SaaS",
        "P0",
        "平台可靠性层",
        "SRE运营",
        "SaaS可观测性、容量、容灾与反滥用",
        "平台SRE/安全/客服",
        "作为平台运维，我希望从统一视图发现故障、容量风险和滥用，并有可演练的恢复流程。",
        "Metrics/Trace/安全日志、SLO/错误预算、告警路由、容量预测、租户公平性、注册与调用反滥用、状态页、事件管理、备份恢复演练、Region灾备、Runbook；遥测只使用脱敏聚合。",
        "不采集Prompt/Response作为可观测数据；不在生产随意执行破坏性混沌；不把内部堆栈暴露客户。",
        "平台SRE总览、告警、事件、容量、公开状态页",
        ("GET /platform/sre/overview", "GET/POST /platform/incidents", "GET /platform/capacity", "GET /status"),
        ("ServiceLevelObjective", "ErrorBudget", "Alert", "Incident", "CapacityForecast", "AbuseDecision", "RecoveryExercise"),
        ("slo.budget_burned", "incident.created", "incident.resolved", "abuse.detected", "recovery.exercise_completed"),
        "platform_ops/SRE管理；security处理滥用；support只读客户安全摘要",
        "关键服务SLO和告警可验证；一次Region级演练满足RPO/RTO；反滥用不误伤已授权高流量租户；状态页不泄露内部拓扑。",
        "PRD-096、100、101",
        "旁路接入遥测，不改变业务响应；告警规则版本化；失败可停用collector且业务继续。",
        "遥测字段白名单、日志脱敏、告警Webhook签名、DDoS/撞库限流、事件访问隔离。",
        "长稳、压力、故障注入、睡眠/时钟、告警风暴、Region切换、日志敏感扫描、Runbook桌面推演。",
        "控制面99.9%、云Gateway99.9%基线；RPO<=15分钟、RTO<=2小时；告警发现<5分钟。",
        "7-9人周",
        "SRE2、后端1、数据/可观测1、测试1、安全0.5",
    ),
    prd(
        104,
        "S4-云管私有网关",
        "P0",
        "云边管理层",
        "设备注册",
        "Edge Agent注册、设备身份与mTLS",
        "企业运维/平台运维",
        "作为企业客户，我希望把本地Gateway安全登记到自己的Workspace，由云端识别和管理。",
        "Edge Agent最小进程、一次性Enrollment Code、设备密钥本地生成、CSR、平台CA签发、Workspace绑定、证书轮换、心跳、吊销、设备状态；仅出站443连接。",
        "不把Provider Key上传云端；不开放公网入站运维端口；设备码不能跨Workspace使用；不绑定特定硬件盒子。",
        "私有部署接入向导、设备列表、证书与心跳状态",
        ("POST /edge/enrollment-codes", "POST /edge/enrollments", "POST /edge/certificates/rotate", "POST /edge/heartbeats", "DELETE /edge/deployments/{deployment_id}"),
        ("EdgeDeployment", "EnrollmentCode", "DeviceIdentity", "DeviceCertificate", "EdgeHeartbeat"),
        ("edge.enrolled", "edge.certificate_rotated", "edge.offline", "edge.revoked"),
        "tenant owner/ops_admin创建设备码；平台ops看Fleet状态；Agent使用独立设备身份",
        "设备码一次性且短时；证书绑定Workspace/Deployment；复制设备身份无效；吊销后不可拉配置；断网不会中断本地已有调用。",
        "PRD-095/096/100/102",
        "无现有设备数据；新增独立服务身份域；Agent可卸载且云端保留审计墓碑。",
        "mTLS、私钥不可导出、证书短期轮换、重放防护、心跳隐私、代理/中间人/时钟漂移。",
        "Enrollment竞态、证书重放/轮换/吊销、代理网络、断网、克隆设备、跨租户、Windows/Linux兼容。",
        "心跳处理p95<200ms；证书吊销传播<60s；10万设备心跳容量基线。",
        "6-8人周",
        "后端2、Agent1、前端1、测试1、安全1",
    ),
    prd(
        105,
        "S4-云管私有网关",
        "P0",
        "云边管理层",
        "配置同步",
        "云边期望状态、签名快照与离线LKG",
        "企业管理员/本地Gateway/平台运维",
        "作为企业管理员，我希望在云端修改配置后可靠下发本地，同时断网时Gateway继续使用最后可信版本。",
        "Desired/Reported State、不可变配置快照、租户/Deployment绑定签名、generation/CAS、增量下载、确认/失败码、回滚、兼容矩阵、离线Last Known Good、云端命令队列；Provider Key引用保持本地。",
        "云端不能直接写本地数据库；不下发Prompt/Response；不允许降版、同版异hash或过期签名覆盖LKG。",
        "部署配置、同步状态、版本差异、回滚页",
        ("GET /edge/configurations/current", "POST /edge/configurations/{generation}/ack", "POST /edge/deployments/{deployment_id}/rollbacks", "GET /edge/deployments/{deployment_id}/drift"),
        ("DesiredState", "ReportedState", "EdgeConfigSnapshot", "EdgeCommand", "ConfigApplicationAttempt"),
        ("edge.configuration_published", "edge.configuration_applied", "edge.configuration_failed", "edge.drift_detected"),
        "租户admin发布租户配置；平台ops只管理版本/兼容性；Agent只能读取自身Deployment",
        "配置按generation单调应用；签名/tenant/deployment/version/hash均校验；失败保留LKG；重连幂等补齐；回滚可追踪。",
        "PRD-104、现有Policy Snapshot/Config Transfer",
        "先同步只读安全配置，再Provider引用和路由；每类配置有版本开关；失败回旧本地管理模式。",
        "域分离签名、抗回放、下载大小上限、内容白名单、Agent日志无配置正文、命令最小化。",
        "损坏/降版/同版异hash、网络分片、断电、并发发布、长离线、时钟漂移、回滚和跨设备隔离。",
        "配置发布到在线Agent p95<30s；离线恢复后5分钟内收敛；热路径不依赖云端。",
        "7-9人周",
        "后端2、Agent1.5、前端1、测试1、安全0.5",
    ),
    prd(
        106,
        "S4-云管私有网关",
        "P0",
        "云边数据层",
        "遥测与密钥托管",
        "私有网关遥测、用量同步与本地Secret托管",
        "企业安全/财务/平台运维",
        "作为私有化客户，我希望请求和Key留在本地，同时云端能够看到健康、用量、成本和失败趋势。",
        "本地Secret Store、Provider Key本地加密、云端只存指纹/状态；用量/成本/失败/健康白名单遥测，序列水位、持久Outbox、断网补传、租户账单归集、可配置数据上传策略；正文遥测固定关闭。",
        "不上传Provider Key、Virtual Key、Token、Prompt、Response、文件内容、Endpoint全文或原始异常；不允许云端远程解密本地Secret。",
        "遥测策略、同步水位、本地Key状态、云端私有部署用量页",
        ("POST /edge/telemetry/batches", "GET /edge/telemetry/policy", "POST /edge/secret-fingerprints", "GET /edge/deployments/{deployment_id}/usage"),
        ("EdgeTelemetryPolicy", "TelemetryBatch", "TelemetryWatermark", "LocalSecretReference", "EdgeUsageAggregate"),
        ("edge.telemetry_received", "edge.telemetry_lagging", "edge.secret_status_changed"),
        "租户security_admin管理上传策略；finance读聚合；平台ops只看健康；Agent写自身遥测",
        "敏感明文命中0；重复/乱序批次不重复计量；断网恢复水位连续；关闭遥测后仍保留最低设备健康且明确告知；失败请求可追踪。",
        "PRD-099/104/105、现有Usage/FinOps/outbox",
        "本地现有SQLite/Provider Key不迁云；新增安全投影；回滚停发新遥测并保留本地Outbox。",
        "字段白名单、批次签名、mTLS、压缩炸弹防护、本地KMS/OS Key Store、删除与留存策略。",
        "敏感canary、乱序/重复/大批次、断网积压、磁盘满、计量对账、策略关闭、跨Deployment。",
        "批次摄取p95<500ms；正常在线遥测延迟<5分钟；Agent资源开销CPU<2%、内存<100MB基线。",
        "7-9人周",
        "后端2、Agent1.5、数据1、测试1、安全1",
    ),
    prd(
        107,
        "S4-云管私有网关",
        "P1",
        "交付层",
        "私有部署包",
        "云管私有Gateway安装包与接入向导",
        "企业运维/交付工程师",
        "作为企业客户，我希望下载标准软件包部署到VM、Docker或K8s，并接入云端管理。",
        "签名发行包、Docker Compose生产模板、VM安装脚本、Helm Chart、外部Postgres/Redis/对象存储选项、初始化向导、预检、升级前检查、诊断包、离线安装介质；默认启动真实Gateway与Edge Agent。",
        "不预装真实客户凭据；不默认开启公网管理端口；不做专有硬件盒子；完全离线不宣称实时云管。",
        "下载中心、部署向导、预检结果、安装诊断",
        ("GET /downloads/private-gateway", "POST /edge/installations/preflight", "CLI metahub-edge install", "helm install metahub-edge"),
        ("ReleaseArtifact", "DeploymentProfile", "InstallationReport", "PreflightCheck", "DiagnosticBundle"),
        ("edge.installation_started", "edge.installation_completed", "edge.installation_failed"),
        "租户Owner/ops_admin下载；平台release管理员发布；制品访问绑定Subscription/Entitlement",
        "三种形态至少两种真实环境一键安装；空环境可完成Enrollment；重启可用；包签名可验；错误有回滚/诊断；Secret和日志审计通过。",
        "PRD-100、104..106",
        "现有single-node Compose升级为受支持Profile；保留开发脚本；安装失败回滚服务/迁移且不删除外部数据。",
        "SBOM、制品签名、最小容器权限、Secret挂载、非root、镜像扫描、诊断包脱敏。",
        "全新/升级/回滚、Windows/Linux、Docker/Helm、代理/无公网、端口冲突、外部依赖、供应链扫描。",
        "标准安装<30分钟；重启恢复<5分钟；升级业务中断目标<5分钟。",
        "8-10人周",
        "SRE/交付2、后端1、前端1、测试1、安全0.5",
    ),
    prd(
        108,
        "S4-发布运营",
        "P0",
        "平台运营层",
        "Fleet与上线",
        "Fleet运维、升级回滚、License、备份与SaaS正式上线门禁",
        "平台SRE/企业运维/安全/财务",
        "作为SaaS总管理和运维团队，我希望统一管理云端与私有部署版本、容量、License、备份和事故恢复。",
        "Fleet版本/健康/容量、分批升级、维护窗口、回滚、私有节点Entitlement/License缓存、配置/账本备份、恢复演练、兼容期、迁移旧default租户、正式域名/证书/状态页、发布Runbook和全产品验收。",
        "License到期不突然中断存量调用；不允许平台越权读取本地Secret；旧PRD-090..108任何功能不得借本条回流。",
        "Fleet总览、升级批次、维护窗口、License、备份恢复、发布门禁",
        ("GET /platform/fleet", "POST /platform/releases/{release_id}/rollouts", "POST /edge/deployments/{deployment_id}/rollback", "GET /edge/license/status", "POST /edge/backups", "POST /edge/restores"),
        ("FleetDeployment", "ReleaseChannel", "Rollout", "MaintenanceWindow", "EdgeEntitlement", "EdgeBackup", "RestoreJob", "LaunchGate"),
        ("fleet.rollout_started", "fleet.rollout_paused", "fleet.rollback_completed", "license.expiring", "edge.backup_completed", "platform.launched"),
        "平台ops管Fleet；release管理员发布；租户ops设维护窗口；security读安全状态；billing管理Entitlement",
        "分批升级可暂停/回滚；错误版本不扩散；License宽限只限制新增；备份可恢复；旧租户迁移可回退；全量WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW通过后才可上线。",
        "PRD-090..107全部完成",
        "按内部租户、试点个人、试点企业、私有试点、正式租户五波迁移；每波有备份/水位/回退；旧登录租户字段最终移除。",
        "发布双人审批、制品签名、兼容矩阵、备份加密、License离线签名、支持访问复核、敏感审计。",
        "全量回归、九档浏览器/DPR、生产影子流量、72小时长稳、压力/混沌、升级/回滚、灾备、敏感与禁止文件审计。",
        "正式目标：Control 99.9%、Gateway 99.9%；关键页面p75<1.5s；RPO<=15分钟、RTO<=2小时。",
        "10-12人周",
        "跨职能：后端2、Gateway1、Agent1、前端1、SRE2、测试2、安全1、产品0.5",
    ),
]


def planned(
    number: int,
    stage: str,
    priority: str,
    layer: str,
    module: str,
    title: str,
    users: str,
    scope: str,
    apis: str,
    objects: str,
    events: str,
    permissions: str,
    acceptance: str,
    dependencies: str,
    slo: str,
    effort: str,
) -> dict[str, object]:
    """Build a detailed row while keeping the source roadmap readable."""
    edge_scope = number in range(120, 131)
    boundary = (
        f"只交付“{title}”的最小生产闭环，不提前实现后续PRD；前端可见性不作为权限边界；"
        "不把Provider Key、Token、Prompt、Response或客户业务正文写入日志、事件和验收证据。"
    )
    if edge_scope:
        boundary += " 云端不得读取本地Secret；完全断网时只允许签名包异步管理，不承诺实时云管。"
    migration = (
        "采用expand-migrate-contract；先增表/增列与回填校验，再切读写；迁移可幂等重放；"
        "保留旧事实与审计水位；down不得静默删除业务事实，回滚使用功能开关或兼容读路径。"
    )
    security = (
        "后端逐请求校验主体、角色、tenant/workspace owner与资源状态；跨租户同形404；写操作幂等/CAS；"
        "统一错误码、request_id和no-store；敏感值加密或只保存摘要；关键操作不可变审计并经outbox恢复。"
    )
    tests = (
        "状态机、幂等/CAS、复合租户所有权、动态撤权、跨租户零泄漏、依赖故障、重启恢复、"
        "迁移往返、真实API/浏览器、敏感信息和静止终态"
    )
    api_items = tuple(part.strip() for part in apis.split("；") if part.strip())
    object_items = tuple(part.strip() for part in objects.split("；") if part.strip())
    event_items = () if events == "无" else tuple(part.strip() for part in events.split("；") if part.strip())
    return prd(
        number,
        stage,
        priority,
        layer,
        module,
        title,
        users,
        f"作为{users}，我希望完成{title}，并在失败、撤权和恢复场景下得到可解释、可追踪的结果。",
        scope,
        boundary,
        f"{title}工作台；详情/状态；操作记录与失败恢复入口",
        api_items,
        object_items,
        event_items,
        permissions,
        acceptance,
        dependencies,
        migration,
        security,
        tests,
        slo,
        effort,
        "后端2、前端1、测试1；按领域增加SRE/安全/财务/Agent 0.5-1",
    )


# The first draft stopped at PRD-108. The clarified roadmap keeps its fully
# reviewed PRD-090..101 rows, then unbundles production, enterprise, edge,
# compliance and launch work instead of forcing every remaining item into 108.
PRDS = INITIAL_DRAFT_PRDS[:12] + [
    planned(
        102, "S2-商业服务", "P0", "平台商业层", "企业认证", "企业认证、合同主体与开票资料",
        "企业Owner/财务管理员/平台审核员",
        "企业实名/主体资料草稿、统一社会信用代码受保护索引、异步审核、补件、拒绝、重新提交、合同主体和开票资料版本快照；个人空间明确不强制企业认证。",
        "POST /workspaces/{workspace_id}/business-verifications；GET /workspaces/{workspace_id}/business-verification；POST /platform/business-verifications/{id}/review；PATCH /workspaces/{workspace_id}/billing-profile",
        "LegalEntityProfile；BusinessVerificationIntent；VerificationEvidence；ContractProfile；BillingProfile",
        "workspace.business_verification_submitted；workspace.business_verified；workspace.business_verification_rejected",
        "owner/finance_admin维护本企业资料；platform_verification只读必要字段并审核；platform_support无权查看证件原件。",
        "审核状态单调且可补件；重复提交幂等；个人空间不被阻断；企业认证结果可供订阅、合同和发票引用；证件原件不进入通用日志/事件。",
        "PRD-091..099", "内部API p95<500ms，不含外部核验时延；审核队列积压可观测", "5-6人周",
    ),
    planned(
        103, "S2-商业服务", "P0", "平台商业层", "支付订阅", "订阅订单、支付、催缴与退款",
        "Workspace Owner/财务管理员/平台财务",
        "套餐下单、试用转付费、支付渠道抽象、签名回调、订阅变更、按比例规则、取消、催缴、宽限、退款和人工对账入口；账本与Entitlement使用明确状态机衔接。",
        "POST /subscriptions；PATCH /subscriptions/{id}；POST /payments/intents；POST /payments/webhooks/{provider}；POST /refunds；GET /billing/orders",
        "SubscriptionOrder；PaymentIntent；PaymentAttempt；PaymentWebhookReceipt；Refund；DunningCase",
        "subscription.changed；payment.succeeded；payment.failed；refund.completed；dunning.started",
        "租户finance_admin管理本空间订阅；platform_billing处理渠道和退款；platform_support只读脱敏状态；任何角色不得读取支付凭据。",
        "回调验签、乱序、重复和延迟不重复记账；支付成功只发放一次权益；失败/退款/催缴全链可追踪；宽限策略不突然中断已接受的请求。",
        "PRD-098..102、PRD-051/079/080", "内部支付API p95<500ms；回调入库p95<300ms；99.99%重复回调只处理一次", "7-9人周",
    ),
    planned(
        104, "S2-商业服务", "P1", "平台商业层", "发票对账", "发票、税务资料与资金对账",
        "租户财务管理员/平台财务",
        "账期结算单、发票申请、抬头快照、税额/币种、红冲、支付渠道与FinOps账本三方对账、差异工单、导出和审计；首版不自建税控开票。",
        "GET /billing/statements；POST /invoices；GET /invoices/{id}；POST /platform/reconciliations；GET /platform/reconciliations/{id}/differences",
        "BillingStatement；InvoiceRequest；InvoiceArtifact；ReconciliationRun；ReconciliationDifference",
        "invoice.requested；invoice.issued；reconciliation.difference_found；reconciliation.closed",
        "租户finance_admin仅看本空间；platform_billing执行开票/对账；platform_support无金额调整权；红冲需双人审批。",
        "订单、支付、账本、结算单金额可逐项追溯；币种不隐式换算；差异可关闭且保留证据；下载授权短时且不可跨租户。",
        "PRD-099、PRD-102/103", "月账期百万分录对账在30分钟内完成；列表p95<500ms", "6-8人周",
    ),
    planned(
        105, "S2-商业服务", "P1", "平台体验层", "消息通知", "通知中心、消息模板与联系偏好",
        "个人用户/企业成员/平台运营",
        "站内信、邮件/短信渠道抽象、模板版本、多语言、用户偏好、强制安全通知、退订、频控、失败重试和已读状态；任何业务事务不等待外部送达。",
        "GET /notifications；PATCH /notifications/{id}/read；GET/PATCH /notification-preferences；GET/POST /platform/notification-templates",
        "Notification；NotificationTemplate；NotificationPreference；DeliveryAttempt；ChannelSuppression",
        "notification.queued；notification.delivered；notification.failed；notification.read",
        "用户管理本人偏好；Workspace管理员只能触发白名单模板；platform_ops管理模板；安全强制通知不可被普通退订关闭。",
        "事件到通知幂等；偏好即时生效；站内信与外部渠道状态可区分；外部失败不回滚业务事实；模板参数严格白名单且无敏感正文。",
        "PRD-091..104、现有Webhook outbox", "站内信查询p95<300ms；排队到投递启动p95<10s", "5-6人周",
    ),
    planned(
        106, "S3-生产云底座", "P0", "平台运行层", "分布式任务", "平台分布式任务队列、Scheduler与Worker生产运行时",
        "platform_ops/平台SRE/后端开发",
        "将平台后台任务沉淀为持久运行时：队列、优先级、租户公平、lease/generation、checkpoint、幂等效果、重试/取消/死信、背压、停服恢复和任务类型注册。企业异步批处理继续使用tenant-owned任务事实和企业任务页，只复用受审的运行时协议，不共用平台工作台、API或授权。",
        "GET /platform/job-queues；GET /platform/jobs/{id}；POST /platform/jobs/{id}/retry；POST /platform/jobs/{id}/cancel；POST /platform/job-types",
        "PlatformJob；JobAttempt；WorkQueue；SchedulerLease；JobCheckpoint；DeadLetterJob",
        "platform_job.started；platform_job.failed；platform_job.completed；platform_job.dead_lettered",
        "platform_ops管理平台队列和任务类型；任何Workspace角色均拒绝/platform/job-*；企业批处理由Workspace API逐请求鉴权；Worker从持久owner恢复执行域，不接受消息体伪造。",
        "平台任务至少一次领取但业务效果恰好一次；租户公平无饥饿；进程崩溃可接管；取消不留下半终态；企业任务与平台队列无跨域枚举；所有失败和恢复有request_id/attempt。",
        "PRD-057/074/099/100/105", "入队p95<100ms；空闲队列启动p95<10s；崩溃任务接管<60s", "8-10人周",
    ),
    planned(
        107, "S3-生产云底座", "P0", "发布控制层", "渐进发布", "功能开关、租户分群与渐进发布",
        "平台发布/SRE/产品Owner/租户管理员",
        "后端权威Feature Flag、环境/Region/租户分群、百分比Canary、前置条件、不可变版本、曝光事实、自动健康门禁、暂停/回滚和全局Kill Switch；前端只消费结果。",
        "GET /platform/feature-flags；POST /platform/feature-flags；POST /platform/feature-flags/{id}/rollouts；POST /platform/feature-flags/{id}/kill；GET /workspaces/current/capabilities",
        "FeatureFlag；FlagVersion；TenantCohort；FlagRollout；FlagEvaluationSnapshot；FeatureExposure",
        "feature.rollout_started；feature.rollout_paused；feature.killed；feature.exposed",
        "release_manager发布且双人审批P0能力；租户只能读自身能力；客户端不能通过本地Storage或参数启用未授权功能。",
        "同一主体稳定分桶；动态撤权优先于Flag；健康恶化自动暂停；Kill Switch目标时间内生效；回滚不删除曝光审计。",
        "PRD-085/090/095/100/101/106", "Gateway本地判断p95<1ms；控制面判断p95<20ms；开关传播<5s", "7-9人周",
    ),
    planned(
        108, "S3-生产云底座", "P0", "云控制面", "配置分发", "租户配置分发、缓存一致性与失效",
        "平台SRE/租户管理员/Gateway",
        "策略、路由、Key引用、套餐权益和撤权事实的版本化快照，租户级增量分发、签名、ACK、水位、LKG、缓存失效、迟到消息拒绝和重建。",
        "GET /internal/config-snapshots/{workspace_id}；POST /internal/config-snapshots/{id}/acks；POST /platform/config-rebuilds",
        "ConfigSnapshot；ConfigDelta；ConfigAck；CacheWatermark；ConfigRebuildJob",
        "config.snapshot_published；config.ack_lagging；config.rebuild_completed",
        "快照发布来自后端已授权事实；Gateway仅取所属Cell/租户；platform_support不能借快照读取Secret或客户正文。",
        "动态撤权和Key禁用在目标时间内生效；乱序快照不回退；冷启动可用LKG且标陈旧；重建与增量结果hash一致；签名失败fail closed。",
        "PRD-045/059/098/106/107", "99%配置变更<5s传播；撤权<2s；Gateway本地读取p95<2ms", "8-10人周",
    ),
    planned(
        109, "S3-生产云底座", "P0", "平台可靠性", "SaaS可观测", "SaaS可观测性、SLO与错误预算",
        "平台SRE/安全/服务Owner",
        "Metrics/Logs/Traces统一关联、服务目录、黄金信号、租户脱敏维度、高基数治理、SLO/SLI、错误预算、告警路由、值班和审计证据；禁止采集提示词/响应正文。",
        "GET /platform/observability/services；GET/POST /platform/slos；GET /platform/error-budgets；POST /platform/alerts/{id}/ack",
        "ServiceCatalogEntry；ServiceLevelObjective；ErrorBudget；Alert；OnCallRoute；TelemetryPolicy",
        "slo.breached；error_budget.exhausted；alert.triggered；alert.resolved",
        "platform_ops管理；platform_security只读安全指标；租户仅看本空间聚合；平台指标标签不得包含客户敏感字段。",
        "请求可由request_id/trace_id串起但无敏感正文；SLO可重算；告警去重/抑制/恢复完整；指标系统故障不阻塞Gateway热路径。",
        "PRD-004/055/081/106..108", "关键告警检测p95<60s；仪表盘p95<1s；遥测额外CPU<5%", "8-10人周",
    ),
    planned(
        110, "S3-生产云底座", "P0", "平台安全层", "反滥用", "注册、API与资源反滥用及风控",
        "平台安全/运营/正常用户",
        "注册/IP/设备/账号/Workspace多维限流，验证码升级、资源创建配额、异常调用评分、凭据填充防护、自动冻结与人工复核、申诉和误伤恢复。",
        "POST /risk/challenges；GET /platform/risk/cases；POST /platform/risk/cases/{id}/decisions；POST /risk/appeals",
        "RiskSignal；RiskDecision；AbuseCounter；Challenge；RiskCase；RiskAppeal",
        "risk.challenge_required；risk.workspace_restricted；risk.appeal_resolved",
        "platform_security决策；platform_support只读脱敏原因；用户只能处理本人挑战/申诉；风控不能授予租户业务权限。",
        "账号枚举防护；分布式限流一致；正常用户基线无明显延迟；自动冻结可申诉；风险原因可解释但不泄露规则阈值。",
        "PRD-091/092/098/106/109", "风控判断p95<30ms；攻击流量下登录/网关正常用户p95增幅<20%", "7-9人周",
    ),
    planned(
        111, "S3-生产云底座", "P0", "平台可靠性", "多区域容灾", "多区域高可用、备份与灾难恢复",
        "平台SRE/安全/业务Owner",
        "控制面多可用区、区域故障切换、数据库恢复、对象跨区副本、KMS恢复、Gateway DNS/路由切换、数据驻留约束、灾备演练和恢复证据。",
        "GET /platform/dr/status；POST /platform/dr/drills；POST /platform/regions/{id}/failovers；GET /platform/backups",
        "Region；FailoverPlan；DisasterRecoveryRun；RecoveryPoint；RestoreEvidence",
        "dr.drill_started；region.failover_started；region.failover_completed；dr.objective_missed",
        "platform_ops双人执行切换；platform_security复核；租户只看本空间状态；任何自动切换不得跨越其数据驻留策略。",
        "单区故障演练达到RPO/RTO；恢复后账本/Outbox/配置水位一致；脑裂被阻断；回切可控；备份加密且季度恢复成功。",
        "PRD-106..110", "Control RPO<=15分钟/RTO<=2小时；Gateway区域切换RTO<=10分钟", "10-12人周",
    ),
    planned(
        112, "S3-生产云底座", "P1", "平台体验层", "服务状态", "公共状态页、事故与维护沟通",
        "租户管理员/开发者/平台SRE/客服",
        "公开服务状态、订阅通知、事故时间线、影响区域/组件、维护窗口、复盘发布；状态页与主控制面隔离，租户私有事故只向授权成员展示。",
        "GET /public/status；GET/POST /status/subscriptions；GET /incidents；POST /platform/incidents；POST /platform/maintenance-windows",
        "StatusComponent；Incident；IncidentUpdate；MaintenanceWindow；StatusSubscription；Postmortem",
        "incident.created；incident.updated；incident.resolved；maintenance.scheduled",
        "platform_ops发布；platform_security审批安全事故披露；租户成员只看授权范围；公共内容不暴露租户名、内部拓扑和攻击细节。",
        "主控制面不可用时状态页仍可访问；事故更新有审计；订阅可验证/退订；影响范围准确；维护不被误报为事故。",
        "PRD-105/109/111", "公共状态页可用性99.99%、p95<500ms；事故确认后10分钟内首条更新", "5-7人周",
    ),
    planned(
        113, "S3-生产云底座", "P1", "平台可靠性", "容量验证", "容量、性能、混沌与单位经济性",
        "平台SRE/FinOps/研发负责人",
        "容量模型、租户增长预测、Gateway/Control基准、压力/浸泡、依赖混沌、成本归集、单位请求/Token成本、扩容建议和发布性能回归门禁。",
        "GET /platform/capacity；POST /platform/capacity/forecasts；POST /platform/load-tests；POST /platform/chaos-experiments；GET /platform/unit-economics",
        "CapacityForecast；LoadTestRun；ChaosExperiment；PerformanceBaseline；PlatformCostAllocation",
        "capacity.threshold_reached；performance.regressed；chaos.objective_missed",
        "SRE执行测试；platform_billing只读成本；租户不可看到其他租户数据；生产混沌需审批、范围和停止条件。",
        "100+租户噪声邻居基线；72小时长稳无泄漏；关键依赖故障符合fail策略；性能回归自动阻断；成本可追溯不含客户正文。",
        "PRD-099/107/109..112", "满足各服务SLO；目标峰值2倍压力仍可受控降级；容量预测误差可量化", "8-10人周",
    ),
    planned(
        114, "S4-企业身份安全", "P0", "企业身份层", "企业域名SSO", "企业域名、OIDC/SAML SSO与登录发现",
        "企业Owner/安全管理员/企业成员",
        "域名验证、登录发现、OIDC/SAML连接、元数据/证书轮换、强制SSO、break-glass、账号链接和IdP故障恢复；平台管理员账号走独立域。",
        "POST /workspaces/{id}/domains；POST /workspaces/{id}/sso-connections；GET /identity/login-discovery；POST /identity/sso/callback；POST /workspaces/{id}/sso/test",
        "VerifiedDomain；SsoConnection；SsoCertificate；AccountFederationLink；SsoPolicy",
        "domain.verified；sso.connection_changed；sso.login_failed；sso.certificate_expiring",
        "owner/security_admin管理本企业；platform_support无权代改；SSO断言只能映射已验证域和明确Membership。",
        "域名抢占被阻断；SAML/OIDC签名/nonce/state/replay防护；强制SSO有安全恢复；证书轮换不中断；登录不要求租户标识。",
        "PRD-091..096、PRD-106", "登录发现p95<200ms；本地回调处理p95<500ms，不含IdP时延", "8-10人周",
    ),
    planned(
        115, "S4-企业身份安全", "P1", "企业身份层", "SCIM", "SCIM目录同步与成员生命周期",
        "企业IT管理员/安全管理员/平台支持",
        "SCIM 2.0 Users/Groups、Bearer凭据轮换、增量同步、软停用、组到角色映射、冲突队列、审计和紧急撤权；Owner不可被SCIM孤立。",
        "GET/POST/PATCH /scim/v2/Users；GET/POST/PATCH /scim/v2/Groups；POST /workspaces/{id}/scim-tokens；GET /workspaces/{id}/scim-syncs",
        "ScimToken；ScimUserLink；ScimGroupLink；ScimRoleMapping；ScimSyncRun；ScimConflict",
        "scim.sync_completed；membership.disabled_by_scim；scim.conflict_detected",
        "企业security_admin配置；SCIM Token仅显示一次；同步只能作用本Workspace；platform_support只能看脱敏错误。",
        "SCIM幂等/过滤/分页符合合同；停用在目标时间内撤权；乱序更新不复活成员；最后Owner保护；冲突可人工解决。",
        "PRD-094/114", "SCIM请求p95<500ms；停用到权限失效<5s；1万成员全量同步<30分钟", "7-9人周",
    ),
    planned(
        116, "S4-企业身份安全", "P0", "企业身份层", "强认证", "MFA、条件访问、设备与会话安全",
        "所有账号/企业安全管理员/平台安全",
        "TOTP/WebAuthn、恢复码、风险升级认证、设备记录、会话列表/撤销、IP/区域/网络条件、平台员工强制MFA和高危操作二次认证。",
        "POST /identity/mfa/enrollments；POST /identity/mfa/challenges；GET/DELETE /identity/sessions；GET/PATCH /workspaces/{id}/conditional-access；POST /identity/step-up",
        "MfaEnrollment；WebAuthnCredential；RecoveryCodeSet；TrustedDevice；ConditionalAccessPolicy；StepUpGrant",
        "identity.mfa_changed；identity.session_revoked；conditional_access.denied；step_up.completed",
        "用户管理本人因子；security_admin制定本企业策略但不能读取凭据；platform_security制定员工策略；高危操作后端强制step-up。",
        "恢复码单次使用；凭据重放/降级被阻断；撤销即时；条件策略先审计模拟再强制；失去全部因子有受控恢复而非绕过。",
        "PRD-091/095/110/114", "Session校验p95<30ms；MFA本地验证p95<300ms；撤销传播<2s", "8-10人周",
    ),
    planned(
        117, "S4-企业身份安全", "P0", "数据治理层", "数据驻留", "数据驻留、保留策略与法律保全",
        "企业安全/法务/平台合规/SRE",
        "Workspace区域锁定、数据类别与存储位置、跨区处理审批、保留策略、删除队列、法律保全、备份到期、导出/注销协调和证据报告。",
        "GET/PATCH /workspaces/{id}/data-governance；POST /workspaces/{id}/legal-holds；DELETE /workspaces/{id}/legal-holds/{hold_id}；GET /platform/data-residency/evidence",
        "DataClassification；ResidencyPolicy；RetentionPolicy；LegalHold；DeletionSchedule；ResidencyEvidence",
        "data.retention_changed；legal_hold.created；deletion.executed；residency.violation_detected",
        "security_admin管理策略；legal_admin管理保全；platform_compliance只读证据；任何跨区操作必须有显式授权和审计。",
        "驻留策略在路由/存储/备份均生效；保全阻断删除但不扩大读取权；到期删除可证明；与PRD-089导出注销状态一致。",
        "PRD-089/102/106/111", "策略判断p95<10ms；到期删除调度偏差<24h；驻留违规检测<5分钟", "8-10人周",
    ),
    planned(
        118, "S4-企业身份安全", "P1", "开发者平台层", "机器身份", "Service Account、OAuth客户端与受限控制面API",
        "企业开发者/自动化系统/安全管理员",
        "非人类主体、OAuth client credentials、短时Token、细粒度Scope、IP/mTLS约束、密钥轮换、最后使用时间、撤销和自动化审计；不复用个人Session。",
        "POST /service-accounts；POST /oauth/clients；POST /oauth/token；POST /service-accounts/{id}/rotate；DELETE /service-accounts/{id}",
        "ServiceAccount；OAuthClient；ClientCredential；AccessTokenGrant；MachineIdentityAudit",
        "service_account.created；service_account.credential_rotated；service_account.revoked",
        "owner/security_admin创建；Scope不得超过操作者授权模板；机器主体仍绑定tenant/workspace；平台角色不能发行租户Token。",
        "Secret只展示一次且只存hash；Scope最小化；撤销即时；Token不能切换Workspace；自动化调用产生actor/request_id/tenant审计。",
        "PRD-094/059/116", "Token签发p95<300ms；鉴权p95<20ms；撤销传播<2s", "7-9人周",
    ),
    planned(
        119, "S4-企业身份安全", "P1", "接入体验层", "Provider目录", "官方Provider连接目录与区域合规接入",
        "企业管理员/开发者/平台运营",
        "智谱、通义、月之暗面、文心、火山、混元、DeepSeek官方/云服务和客户本地OpenAI-compatible连接模板、区域/能力/计费元数据、凭据自持、连通性预检和迁移兼容说明。",
        "GET /provider-catalog；POST /providers/from-catalog；POST /providers/{id}/preflight；GET /provider-catalog/{id}/compatibility",
        "ProviderCatalogEntry；ProviderCapability；RegionalEndpointTemplate；ProviderPreflightRun；CompatibilityProfile",
        "provider_catalog.updated；provider.preflight_completed；provider.compatibility_changed",
        "platform_ops发布签名目录；租户admin创建本空间Provider；developer只读；任何目录模板不能携带平台共用客户Key。",
        "默认上游仅为指定官方/云服务和本地Endpoint；OpenRouter/硅基流动只标竞品/迁移兼容；预检失败不落明文凭据；能力可追溯。",
        "PRD-011..014/063/064/084/108", "目录查询p95<300ms；预检状态<30s或明确超时；目录更新传播<5分钟", "6-8人周",
    ),
    planned(
        120, "S5-云管私有网关", "P0", "云边信任层", "Edge身份", "Edge Agent注册、设备身份与mTLS",
        "企业运维/平台Fleet运维/Edge Agent",
        "一次性注册令牌、设备密钥本地生成、CSR签发、mTLS、Deployment绑定、证书轮换/吊销、防重放、出站连接和设备清退。",
        "POST /edge/registrations；POST /edge/certificates；POST /edge/sessions；POST /edge/deployments/{id}/revoke",
        "EdgeDeployment；EdgeRegistrationIntent；DeviceIdentity；DeviceCertificate；EdgeSession",
        "edge.registered；edge.certificate_rotated；edge.revoked；edge.authentication_failed",
        "租户owner创建注册意图；platform_ops管理Fleet但不能读取租户Secret；Agent仅访问自身Deployment合同。",
        "令牌单次/短时；复制身份不能注册第二设备；证书吊销生效；网络抓包无敏感明文；跨Deployment同形拒绝。",
        "PRD-090/094/106/116", "mTLS握手p95<500ms；证书吊销传播<60s；注册流程<10分钟", "8-10人周",
    ),
    planned(
        121, "S5-云管私有网关", "P0", "云边控制层", "期望状态", "云边期望状态、签名快照与离线LKG",
        "企业管理员/平台运维/Edge Agent",
        "云端Desired State、版本/目标选择、签名Manifest、Agent拉取/验签/预检/原子应用/ACK、失败回滚、本地LKG、断网陈旧状态和冲突检测。",
        "POST /edge/deployments/{id}/desired-state；GET /edge/deployments/{id}/desired-state；POST /edge/deployments/{id}/acks；POST /edge/deployments/{id}/rollbacks",
        "DesiredState；SignedSnapshot；SnapshotArtifact；ApplyAttempt；EdgeAck；LastKnownGood",
        "edge.desired_state_published；edge.apply_failed；edge.state_converged；edge.rollback_completed",
        "租户admin发布本空间配置；release_manager只能发布平台版本策略；Agent只取自身签名制品；高危变更需审批。",
        "旧/乱序/过期/错误签名被拒绝；部分应用不暴露；失败回LKG；断网继续服务并标陈旧；云端清晰显示期望/实际差异。",
        "PRD-108/120", "在线配置传播p95<60s；本地读取p95<2ms；失败回滚<5分钟", "9-11人周",
    ),
    planned(
        122, "S5-云管私有网关", "P0", "云边数据层", "遥测同步", "私有网关遥测、用量与失败事实同步",
        "企业运维/财务/平台SRE/Edge Agent",
        "允许字段白名单、聚合Metrics、Usage/成本/失败请求摘要、单调水位、批量压缩、断点续传、离线积压、去重、时钟偏差和租户可配置的遥测级别。",
        "POST /edge/telemetry/batches；GET /edge/deployments/{id}/telemetry-watermark；PATCH /edge/deployments/{id}/telemetry-policy",
        "TelemetryPolicy；TelemetryBatch；UsageSyncCursor；EdgeFailureSummary；TelemetryReceipt",
        "edge.telemetry_received；edge.telemetry_lagging；edge.usage_reconciled",
        "租户security_admin控制允许级别；finance_admin读本租户用量；platform_ops只见运营摘要；Agent只能追加自身序列。",
        "断网恢复无重复/丢水位；失败请求可追踪；Usage/成本与本地账本核对；Key/Token/Prompt/Response/Endpoint原文上传命中为0。",
        "PRD-017/051/099/109/120", "在线遥测延迟p95<60s；断网7天恢复后2小时内追平；热路径额外开销<2%", "8-10人周",
    ),
    planned(
        123, "S5-云管私有网关", "P0", "云边安全层", "本地Secret", "私有网关本地Secret托管与轮换",
        "企业安全管理员/Edge Agent/平台支持",
        "Provider Key/Token只在客户环境写入、本地KMS/OS KeyStore信封加密、引用ID进入云端、轮换、连通性测试、备份排除和支持诊断字段裁剪。",
        "POST /local/secrets；POST /local/secrets/{id}/rotate；DELETE /local/secrets/{id}；POST /local/provider-connections/{id}/test；GET /edge/secret-references",
        "LocalSecret；SecretReference；SecretVersion；SecretRotation；ProviderConnectionTest",
        "edge.secret_reference_changed；edge.secret_rotation_failed；edge.provider_test_completed",
        "仅本地tenant security_admin写入；云端任何平台角色只见引用/状态；Agent进程按最小权限取用且不回传。",
        "云端数据库/日志/抓包/支持包中明文命中0；轮换原子且可回退；删除前引用检查；备份恢复不泄漏；诊断只返回固定错误码。",
        "PRD-084/120..122", "本地Secret解封p95<10ms；轮换不造成已接受请求失败；云端明文0", "7-9人周",
    ),
    planned(
        124, "S5-云管私有网关", "P0", "交付层", "私有安装", "云管私有Gateway安装包、预检与接入向导",
        "企业运维/平台交付/安全管理员",
        "签名Compose/容器包、Linux基线、CPU/内存/磁盘/端口/DNS/时间预检、代理/CA、初始化、云端注册、升级通道、卸载保留策略和可复现离线介质。",
        "CLI metahub preflight；CLI metahub install；CLI metahub connect；CLI metahub uninstall；GET /edge/installers",
        "InstallerRelease；InstallProfile；PreflightReport；Installation；InstallAudit",
        "edge.install_started；edge.install_completed；edge.install_failed",
        "租户owner下载本空间引导；release_manager发布签名包；本地root操作有确认和审计；安装包无预置客户Secret。",
        "全新Linux一条向导可完成；预检失败不半安装；签名/版本不匹配阻断；安装后健康/注册通过；卸载不误删用户选择保留的数据。",
        "PRD-120/121/123、PRD-028", "标准环境安装<20分钟；预检<2分钟；安装失败清理可验证", "9-11人周",
    ),
    planned(
        125, "S5-云管私有网关", "P1", "交付层", "部署验收", "私有部署验证与脱敏支持包",
        "企业运维/平台交付/客服",
        "端到端接入验收、Provider连通、策略/用量/故障同步、证书/备份检查、基准测试、可下载脱敏支持包、客户确认与交付证据。",
        "POST /edge/deployments/{id}/validations；GET /edge/deployments/{id}/validations/{run_id}；POST /local/support-bundles；GET /edge/support-bundles/{id}",
        "DeploymentValidation；ValidationCheck；SupportBundle；DeliveryAcceptance",
        "edge.validation_completed；edge.support_bundle_ready；edge.delivery_accepted",
        "租户admin执行；platform_support需JIT授权下载；支持包在本地生成白名单且客户可预览；平台不得远程任意取文件。",
        "验收覆盖真实调用且Usage/成本/失败可追踪；支持包敏感扫描0；失败项有修复建议和重试；跨版本结果可比较。",
        "PRD-120..124", "全套验证<15分钟（不含模型上游长超时）；支持包<100MB且生成<5分钟", "5-7人周",
    ),
    planned(
        126, "S5-云管私有网关", "P1", "Fleet层", "Fleet健康", "Fleet资产、健康与受控远程诊断",
        "平台Fleet运维/租户运维/安全团队",
        "Deployment清单、版本/容量/证书/配置水位/遥测滞后、健康评分、告警、维护模式、远程诊断命令白名单、客户批准和完整审计。",
        "GET /platform/fleet；GET /edge/deployments/{id}/health；POST /edge/deployments/{id}/diagnostic-grants；POST /edge/deployments/{id}/diagnostics",
        "FleetDeployment；EdgeHealthSnapshot；DiagnosticGrant；DiagnosticCommand；DiagnosticResult",
        "edge.health_changed；edge.diagnostic_requested；edge.diagnostic_completed",
        "platform_ops只见运营字段；远程诊断需租户批准、限时、白名单；命令执行不获得通用Shell；结果字段裁剪。",
        "平台角色不自动成为租户成员；未授权诊断调用0；到期撤销；健康来自可解释信号；命令、actor、reason、request_id全审计。",
        "PRD-095/120..125", "Fleet 1万节点列表p95<1s；健康变化<2分钟可见；诊断超时可取消", "8-10人周",
    ),
    planned(
        127, "S5-云管私有网关", "P0", "Fleet层", "版本运维", "Fleet分批升级、回滚与维护窗口",
        "平台发布/SRE/租户运维",
        "Release Channel、兼容矩阵、维护窗口、Canary/批次/暂停、健康门禁、数据库迁移预检、流量排空、失败回滚和N/N-1支持期。",
        "POST /platform/releases；POST /platform/releases/{id}/rollouts；POST /fleet/rollouts/{id}/pause；POST /edge/deployments/{id}/rollback；GET /fleet/compatibility",
        "GatewayRelease；ReleaseArtifact；ReleaseChannel；Rollout；RolloutBatch；MaintenanceWindow；RollbackAttempt",
        "fleet.rollout_started；fleet.rollout_paused；fleet.rollout_failed；fleet.rollback_completed",
        "release_manager发布且双人审批；租户选择窗口/通道；platform_ops执行但不能绕过客户冻结；Agent只接受签名兼容制品。",
        "错误版本不扩散；Canary失败自动暂停；升级/回滚数据兼容；已接受请求排空；N-1可回退；进度和失败节点可追踪。",
        "PRD-121/124..126", "1% Canary观察>=30分钟；单节点升级<10分钟；失败自动暂停<60s", "9-11人周",
    ),
    planned(
        128, "S5-云管私有网关", "P1", "Fleet商业层", "私有授权", "私有Gateway License、容量与离线宽限",
        "平台财务/租户Owner/Edge Agent",
        "签名Entitlement、节点/并发/功能容量、在线刷新、离线缓存、到期提醒、宽限、扩容、吊销和防时钟回拨；热路径不依赖云端在线校验。",
        "POST /edge/licenses；GET /edge/licenses/{deployment_id}；POST /edge/licenses/{id}/refresh；CLI metahub license import",
        "EdgeLicense；SignedEntitlement；CapacityGrant；LicenseLease；LicenseAudit",
        "license.issued；license.expiring；license.grace_started；license.capacity_exceeded",
        "platform_billing签发；租户owner查看/导入；Agent本地验签；platform_support无签发权；License不授予数据访问。",
        "离线验签、防篡改/回拨；过期先宽限且只限制新增，不突然中断存量；扩容原子；云端与本地状态最终一致。",
        "PRD-098/103/120/127", "本地授权判断p95<2ms；在线刷新p95<1s；默认离线宽限>=7天", "6-8人周",
    ),
    planned(
        129, "S5-云管私有网关", "P0", "Fleet可靠性", "私有灾备", "私有Gateway备份、恢复与灾备演练",
        "企业运维/安全/平台交付",
        "配置、策略、账本水位、审计和本地Secret的分域加密备份，目标存储、保留、校验、恢复向导、替换节点、恢复后重注册和定期演练。",
        "POST /local/backups；POST /local/restores；GET /edge/deployments/{id}/backup-status；POST /edge/deployments/{id}/restore-drills",
        "EdgeBackup；BackupChunk；BackupKeyEnvelope；RestoreJob；RestoreVerification；PrivateDrDrill",
        "edge.backup_completed；edge.backup_failed；edge.restore_completed；edge.dr_objective_missed",
        "备份密钥由客户控制；平台只见状态/摘要；恢复需本地高权限+租户批准；支持不能下载备份正文。",
        "备份校验和/加密通过；真实恢复后配置/账本/审计一致；Secret不上传云端；损坏/错误版本失败关闭；RPO/RTO有证据。",
        "PRD-121..128", "默认RPO<=24小时/RTO<=4小时，可按套餐提升；恢复演练季度通过", "8-10人周",
    ),
    planned(
        130, "S5-云管私有网关", "P1", "云边控制层", "断网管理", "隔离环境签名包异步管理与冲突合并",
        "隔离网络运维/平台运维/安全管理员",
        "云端导出签名期望状态/License/升级包，本地离线导入验签与预检，本地导出状态/用量/审计摘要，云端导入水位去重、冲突报告和人工决策。",
        "POST /edge/offline-bundles/export；POST /edge/offline-bundles/import；CLI metahub bundle verify；CLI metahub bundle apply",
        "OfflineBundle；BundleManifest；BundleTransfer；OfflineSyncCursor；OfflineConflict",
        "edge.offline_bundle_exported；edge.offline_bundle_applied；edge.offline_conflict_detected",
        "导出/导入均需双人审批；介质包最小字段、签名、加密和到期；平台不能因导入获得本地Secret。",
        "重复/乱序/过期/错误目标包被拒绝；离线状态水位无重复；冲突不自动覆盖；敏感明文命中0；明确不声称实时云管。",
        "PRD-121..129", "10GB以内包验证<10分钟；导入可断点恢复；冲突报告100%可追溯", "7-9人周",
    ),
    planned(
        131, "S6-迁移与客户运营", "P0", "迁移层", "迁移切换", "旧租户、直连与私有部署迁移切换向导",
        "现有客户管理员/平台交付/开发者",
        "旧default租户到Workspace、账号绑定、Provider/Key/策略/账本/审计映射，直连配置清单，试运行、差异报告、双读/冻结窗口、切换、回退和迁移波次。",
        "POST /migrations/assessments；POST /migrations/runs；GET /migrations/{id}/differences；POST /migrations/{id}/cutover；POST /migrations/{id}/rollback",
        "MigrationAssessment；MigrationPlan；MigrationRun；MigrationDifference；CutoverCheckpoint；RollbackPlan",
        "migration.started；migration.validation_failed；migration.cutover_completed；migration.rolled_back",
        "租户owner授权本空间；platform_support通过JIT协助；迁移工具不能跨租户扫描；凭据只在目标安全域重录或信封迁移。",
        "行数/hash/FK/账本序列/审计水位一致；试运行无写；切换可回退；身份/权限不扩大；Provider Key不进入报告。",
        "PRD-090..130、PRD-040/062/089", "10万配置对象评估<30分钟；计划停机窗口可量化；回滚<2小时", "10-12人周",
    ),
    planned(
        132, "S6-迁移与客户运营", "P1", "客户服务层", "服务支持", "客户工单、SLA与安全诊断协作",
        "租户成员/平台客服/SRE/安全",
        "工单、优先级、SLA计时、附件安全扫描、关联request_id/Deployment/事故、内部备注、客户沟通、升级、满意度和JIT支持授权联动。",
        "GET/POST /support/tickets；PATCH /support/tickets/{id}；POST /support/tickets/{id}/attachments；POST /support/tickets/{id}/escalations",
        "SupportTicket；TicketMessage；TicketAttachment；SlaClock；Escalation；SatisfactionSurvey",
        "support.ticket_created；support.sla_at_risk；support.ticket_resolved",
        "租户只见本空间；platform_support处理但内容字段最小化；内部备注不回传；读取诊断仍需PRD-095 JIT授权。",
        "SLA暂停/恢复规则确定；附件恶意内容隔离；租户隔离；关联request_id不自动拉取Prompt/Response；升级和通知不丢。",
        "PRD-095/105/109/112/125/126", "工单列表p95<500ms；P1告警到升级<5分钟；附件扫描有超时状态", "7-9人周",
    ),
    planned(
        133, "S6-迁移与客户运营", "P1", "开发者平台层", "API生命周期", "公共控制面API、SDK版本与废弃策略",
        "企业开发者/集成商/平台研发",
        "公开OpenAPI、版本策略、稳定分页/幂等、SDK生成与签名发布、Changelog、Deprecated/Sunset、N/N-1兼容、迁移指南和Webhook Schema Registry。",
        "GET /developer/api-versions；GET /developer/changelog；GET /developer/webhook-schemas；GET /developer/sdks/{language}",
        "ApiVersion；ApiContractSnapshot；SdkRelease；DeprecationNotice；WebhookSchemaVersion",
        "api.deprecated；api.sunset_scheduled；sdk.released；webhook.schema_published",
        "release_manager发布合同；租户developer只读/下载；废弃不能绕过现有RBAC；SDK制品不包含默认凭据。",
        "合同diff阻断破坏性改动；兼容期>=90天；旧接口到期前多渠道提醒；SDK可复现/验签；错误码/request_id一致。",
        "PRD-038/039/042/057/118", "文档/Schema查询p95<300ms；SDK smoke全平台通过；兼容测试覆盖N/N-1", "6-8人周",
    ),
    planned(
        134, "S6-迁移与客户运营", "P0", "隐私治理层", "隐私中心", "全局账号隐私、同意与数据主体请求",
        "个人用户/企业成员/平台隐私团队",
        "隐私条款版本、同意记录、营销偏好、全局账号数据副本、更正、删除/匿名化、Workspace成员关系协调、法定保留和请求时限。",
        "GET/PATCH /identity/privacy-preferences；POST /identity/data-subject-requests；GET /identity/data-subject-requests/{id}；GET /platform/privacy/queue",
        "ConsentRecord；PrivacyPreference；DataSubjectRequest；IdentityExport；ErasureDecision；RetentionException",
        "privacy.consent_changed；privacy.request_received；privacy.request_completed",
        "用户管理本人；platform_privacy处理最小必要字段；企业不能删除平台全局账号但可移除Membership；legal_admin独立审批保全。",
        "身份核验后受理；导出不含其他Workspace；删除与PRD-089/117一致；法定保留可解释；全过程无账号枚举和敏感日志。",
        "PRD-089/091/092/105/117", "请求状态查询p95<500ms；法定SLA前预警；导出大任务异步可恢复", "7-9人周",
    ),
    planned(
        135, "S6-迁移与客户运营", "P0", "合规治理层", "合规证据", "等保、个保与企业审计证据中心",
        "平台合规/安全/企业审计员",
        "控制项库、等保/个保/数据安全法映射、责任人、证据采集、有效期、例外、整改、租户合规报告和审计导出；不宣称未取得的认证。",
        "GET /platform/compliance/controls；POST /platform/compliance/evidence；GET /workspaces/{id}/compliance-reports；POST /platform/compliance/exceptions",
        "ComplianceControl；ControlMapping；ComplianceEvidence；ComplianceException；RemediationTask；ComplianceReport",
        "compliance.evidence_expiring；compliance.control_failed；compliance.exception_approved",
        "platform_compliance管理平台证据；租户security_admin只读本空间报告；审计员只读；例外双人审批且到期。",
        "证据可追溯/hash/有效期；控制失败自动整改；报告不泄漏其他租户；法规与认证措辞准确；敏感证据分级授权。",
        "PRD-034/058/081/109/117/134", "报告生成<10分钟；过期检测<24小时；关键控制失败<5分钟告警", "8-10人周",
    ),
    planned(
        136, "S6-迁移与客户运营", "P0", "供应链安全层", "发布安全", "SBOM、制品签名、漏洞与发布证明",
        "平台安全/发布工程/SRE/客户安全",
        "源码依赖锁定、SBOM、容器/安装包扫描、SLSA式Provenance、制品签名/验签、漏洞SLA、例外审批、补丁和客户可下载安全报告。",
        "GET /releases/{id}/security-report；POST /platform/vulnerabilities/{id}/exceptions；GET /releases/{id}/provenance",
        "SbomReport；ArtifactSignature；BuildProvenance；VulnerabilityFinding；SecurityException；PatchRelease",
        "release.security_scan_failed；vulnerability.critical_found；release.signed；patch.released",
        "release_manager/platform_security双人审批；高危未修复默认阻断；客户只读公开报告；签名私钥仅KMS构建身份可用。",
        "每个制品有SBOM/签名/Provenance；高危阻断或有时限例外；验签失败不可安装；补丁可回滚且覆盖云/边兼容矩阵。",
        "PRD-106/127/133/135", "发布门禁<20分钟；关键漏洞识别<24小时；紧急修复目标<72小时", "7-9人周",
    ),
    planned(
        137, "S6-迁移与客户运营", "P0", "安全验证层", "隔离验证", "租户隔离持续验证与安全攻防",
        "平台安全/测试/SRE/外部审计",
        "租户/平台域授权模型白盒验证、IDOR/越权/缓存污染/队列owner/对象存储前缀/指标标签测试、动态撤权、模糊测试、渗透测试和修复闭环。",
        "POST /platform/security/assessments；GET /platform/security/assessments/{id}；POST /platform/security/findings/{id}/remediations",
        "SecurityAssessment；IsolationTestRun；PenTestFinding；Remediation；RiskAcceptance",
        "security.assessment_failed；security.finding_created；security.finding_closed",
        "platform_security发起；租户仅见自身相关通知；测试身份/数据专用且验收后清除；风险接受需owner+security_admin双批。",
        "所有业务对象owner矩阵覆盖；跨租户读取/写入/侧信道已验证为0；P0/P1关闭；动态撤权和迟到响应全链；禁止测试污染生产客户。",
        "PRD-090..136", "每次发布增量隔离门禁<30分钟；季度完整攻防；P0修复<24小时", "8-10人周",
    ),
    planned(
        138, "S6-迁移与客户运营", "P0", "发布治理层", "发布候选", "SaaS发布候选、核心迁移波次与全产品门禁",
        "产品负责人/研发/SRE/安全/测试/运营/财务/客服",
        "内部、个人试点、企业试点、云管私有试点四波；功能开关、Runbook、培训、支持排班、财务对账、法务文案、回滚、72小时长稳和发布候选评审。公开发布必须等待PRD-139起的存量控制台分域迁移和最终PRD-148门禁。",
        "GET /platform/launch-gates；POST /platform/launch-gates/{id}/evidence；POST /platform/launches；POST /platform/launches/{id}/rollback",
        "LaunchGate；LaunchEvidence；LaunchWave；GoNoGoDecision；LaunchRollback；PostLaunchReview",
        "launch.gate_failed；launch.wave_started；launch.wave_completed；launch.rolled_back",
        "产品/SRE/安全/测试/财务/法务是会签actor kind，不新增RBAC角色；platform_owner只能在全部会签后执行，任何人不能单独绕过P0门禁；租户数据访问边界不变。",
        "核心域WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW全组通过；四波试点可回退；72小时长稳；灾备/支付/Edge演练；敏感与禁止文件审计0；生成发布候选但不得标记公开GA。",
        "PRD-090..137", "Control 99.9%、Gateway 99.95%；关键页面p75<1.5s；连续72小时无P0/P1", "12-15人周",
    ),
    planned(
        139, "S7-平台运营增量", "P1", "平台增长层", "渠道伙伴", "渠道伙伴、代理商与客户归属管理",
        "platform_channel/platform_billing/platform_owner/渠道伙伴",
        "把现有PRD-087 tenant-scoped伙伴门户迁到纯SaaS平台/伙伴域：迁移渠道伙伴组织与成员、代理等级、合同和区域范围、邀请/推荐码、商机报备、Workspace归属、归属变更审批、佣金规则快照、结算对账和伙伴门户；复用并迁移现有/partners数据与审计，不重建第二套事实。企业Workspace不再提供partner-portal。",
        "GET/POST /platform/channel-partners；POST /platform/channel-leads；POST /platform/workspace-attributions；POST /platform/workspace-attributions/{id}/approve；GET /platform/channel-statements；GET /partner/workspaces",
        "ChannelPartner；ChannelMembership；ReferralCode；RegisteredLead；WorkspaceAttribution；AttributionChange；CommissionRule；ChannelStatement",
        "channel.partner_changed；channel.lead_registered；workspace.attribution_changed；channel.statement_closed",
        "platform_channel管理渠道目录、商机和归属但不能读取客户业务内容；platform_billing复核结算；platform_owner审批高风险归属变更；伙伴身份只能访问/partner白名单；企业角色全部拒绝。",
        "渠道角色页面与其他平台角色隔离；企业Workspace导航/DOM/预取/API均无渠道、客户管理或增长分析模块；同一Workspace归属状态机可追溯且变更双审批；重复推荐/商机幂等；佣金按版本快照可对账；伙伴不能枚举非归属客户或进入租户控制面。",
        "PRD-087/095/096/098/099/102..105/132/138", "渠道目录p95<500ms；归属查询p95<300ms；月结1万Workspace<30分钟", "8-10人周",
    ),
    planned(
        140, "S7-平台运营增量", "P1", "平台运营层", "客户成功", "SaaS平台客户管理、健康与续费协同",
        "platform_customer_success/platform_support/platform_billing/platform_owner",
        "把现有PRD-086 tenant-scoped customer-health评分迁到纯SaaS平台客户运营投影，并扩展客户目录、联系人和沟通偏好、激活状态、采用率、安全脱敏健康、续费机会、流失风险、成功计划、跟进任务和QBR摘要；复用既有评分事实和审计，不重建第二套。企业Workspace不再提供customer-health。",
        "GET /platform/customer-accounts；GET /platform/customer-accounts/{id}/health；POST /platform/customer-success-plans；POST /platform/customer-tasks；GET /platform/renewal-pipeline",
        "CustomerAccountProjection；CustomerContact；CustomerHealthSnapshot；SuccessPlan；CustomerTask；RenewalOpportunity；QbrSummary",
        "customer.health_changed；customer.task_due；renewal.risk_changed；success_plan.completed",
        "platform_customer_success管理客户运营；platform_support处理工单协作；platform_billing只读续费财务状态；platform_owner审批批量操作；任何企业角色和伙伴身份均拒绝/platform/customer-*。",
        "平台客户目录与租户业务数据严格分离；健康结论可解释；联系偏好生效；批量任务有上限和审计；企业Workspace导航/DOM/预取/API无客户管理模块；跨客户枚举为0。",
        "PRD-086/095/096/099/105/109/132/138", "10万客户目录p95<500ms；健康投影更新<5分钟；续费任务到期偏差<1分钟", "8-10人周",
    ),
    planned(
        141, "S7-平台运营增量", "P1", "平台增长层", "增长分析", "SaaS平台增长漏斗、活动与转化归因",
        "platform_growth/platform_channel/platform_customer_success/platform_owner",
        "把现有PRD-085 tenant-scoped product-analytics漏斗迁到纯SaaS平台聚合域，并扩展获客来源、注册/激活/试用/付费/留存漏斗、活动和实验、渠道归因、同期群、转化率、单位经济摘要和脱敏导出；复用既有ProductEvent/outbox事实，不重建第二套。企业Workspace不再提供growth-analytics；接入步骤由PRD-097初始化向导承接。",
        "GET /platform/growth/funnels；GET /platform/growth/cohorts；GET/POST /platform/growth/campaigns；GET/POST /platform/growth/experiments；GET /platform/growth/attribution",
        "AcquisitionSource；GrowthFunnelSnapshot；CohortSnapshot；Campaign；GrowthExperiment；ConversionAttribution；GrowthExport",
        "growth.conversion_recorded；growth.campaign_changed；growth.experiment_decided；growth.exported",
        "platform_growth管理活动/实验和聚合分析；platform_channel只读渠道归因；platform_customer_success只读采用/留存摘要；platform_owner审批敏感导出；所有企业角色均拒绝。",
        "漏斗口径版本化且可复算；实验分流有同意/退出和停止规则；归因不跨客户泄漏；高基数与PII受控；企业Workspace导航/DOM/预取/API无增长分析模块；导出审计和删除策略通过。",
        "PRD-085/092/096/097/099/105/107/110/134/139/140", "聚合查询p95<1s；日增量10分钟内可见；百万事件重算<60分钟", "9-11人周",
    ),
    planned(
        142, "S7-平台运营分域", "P1", "品牌与域名层", "品牌边界", "平台品牌、渠道白标与私有Gateway外观分域",
        "platform_owner/platform_channel/release_manager；Workspace owner仅管理本地Deployment外观",
        "拆分现有tenant-scoped branding：SaaS主登录页和平台品牌仅由平台管理；渠道白标归platform_channel且绑定伙伴/合同/域名验证；企业Workspace不再改写SaaS登录页；云管私有Gateway可由Workspace owner在Deployment范围配置本地外观。本地Deployment外观与平台品牌/渠道白标使用独立导航、DTO、写权限和owner；只迁移复用branding_configs/events/outbox历史事实并保留tenant_id审计。",
        "GET/PATCH /platform/branding；GET/PATCH /platform/channel-partners/{id}/branding；GET/PATCH /deployments/{id}/branding；GET /branding/public",
        "PlatformBrand；ChannelBrand；DeploymentBrand；VerifiedDomain；BrandAsset；BrandMigrationCursor",
        "platform_brand.changed；channel_brand.changed；deployment_brand.changed；brand_domain.verified",
        "platform_owner管理SaaS主品牌；platform_channel只能管理获授权伙伴品牌；Workspace owner只能管理本企业Deployment外观；任何企业角色不得修改平台登录页或其他Workspace域名。",
        "custom_domain全局唯一且重新验证；Logo安全扫描；旧branding行/event/outbox水位保真；企业SaaS页面没有白标管理导航；私有Gateway离线LKG仍可显示最后有效品牌；回滚不恢复跨域写权限。",
        "PRD-088/093/095/098/120/121/123/139", "公开品牌读取p95<200ms；变更传播99%<5秒；资源上传<5秒", "6-8人周",
    ),
    planned(
        143, "S7-平台运营分域", "P1", "Provider工程层", "Provider工程台", "Provider Adapter目录、合同测试与错误规则分域",
        "platform_provider_engineer/platform_security/release_manager",
        "把adapter-specs、adapter-contract-runs和内置error-map维护迁到平台Provider工程台；官方Adapter目录、合同CI、内置错误规则和发布证明由平台管理。企业开发者只在Provider详情查看兼容版本、健康摘要和自身Endpoint的受限错误覆盖，不展示CI内部case、Runner、构建路径或竞品运营信息。",
        "GET /platform/provider-adapters；GET /platform/provider-adapters/{id}/contract-runs；POST /platform/provider-error-catalogs/releases；GET/PATCH /workspace/provider-error-overrides",
        "ProviderAdapterRelease；AdapterContractRunProjection；BuiltinErrorCatalog；WorkspaceErrorOverride；ProviderCompatibilityProjection",
        "provider_adapter.released；adapter_contract.failed；error_catalog.released；workspace_error_override.changed",
        "release_manager发布Adapter；platform_provider_engineer维护目录并运行合同测试；platform_security复核失败与供应链；Workspace developer只管理本Workspace覆盖且不能修改内置目录。",
        "现有0055/0056/0046表与事件按版本保真；企业导航无Adapter规范和CI报告；Provider详情仍能解释兼容性和归一错误；平台内部路径/源码/Runner Secret不进入企业DTO或日志；默认上游与竞品边界不变。",
        "PRD-063/064/119/123/133/136/137", "目录读取p95<300ms；合同报告10万case分页p95<500ms；发布传播<5分钟", "8-10人周",
    ),
    planned(
        144, "S7-平台运营分域", "P0", "商业账务层", "商业账务分域", "平台套餐账务与企业用量财务边界重构",
        "platform_billing/platform_channel/finance_admin/Workspace owner",
        "拆分现有套餐、账单争议和模型价格页面：平台管理套餐目录、订阅、权益、官方建议价、支付/退款/发票、争议裁决和渠道结算；企业finance_admin查看本Workspace订阅、账单、发票、争议进度，并管理BYOK成本价、预算、额度和内部FinOps。平台商业账不得与企业内部成本账混成一套可写事实。",
        "GET /platform/plans；GET /platform/subscriptions；POST /platform/billing-disputes/{id}/decisions；GET /workspace/subscription；POST /workspace/billing-disputes；GET/PATCH /workspace/provider-cost-rates",
        "PlatformPlanCatalog；WorkspaceSubscriptionProjection；PlatformInvoice；BillingDisputeCase；ProviderCostRate；InternalFinOpsLedger；BillingDomainReconciliation",
        "plan.released；subscription.changed；billing_dispute.submitted；billing_dispute.decided；provider_cost_rate.changed",
        "platform_billing管理商业账和裁决；platform_channel只读渠道结算；finance_admin管理本Workspace财务与争议申请；企业admin无财务授权时不得看金额；平台支持不能查看Provider Secret或业务正文。",
        "金额、币种、税率和价格版本可复算；平台账与企业内部成本账双边对账但独立授权；旧0072/0073及现有model price/FinOps事实可回滚；企业不出现套餐目录管理或争议裁决按钮；失败请求和费用调整全链可追踪。",
        "PRD-072/073/098/099/102/103/104/132/139", "账单查询p95<500ms；对账10万分录<10分钟；争议状态传播<5秒", "10-12人周",
    ),
    planned(
        145, "S7-平台运营分域", "P0", "控制台体验层", "共享页面分域", "平台与Workspace总览、审批、助手、状态和设置分域",
        "platform_owner/platform_ops/platform_security/Workspace owner/职能管理员",
        "对overview、admin-assistant、approvals、system-status、config-summary、language-settings逐一拆分主体和数据源：平台页面只读平台运营聚合；Workspace页面只读本企业治理事实；审批队列按actor domain隔离；语言属于GlobalAccount偏好；环境信息改为角色受控的Deployment诊断，不向普通用户展示研发配置。页面切换只加载当前路由依赖。",
        "GET /platform/overview；GET /workspace/overview；POST /platform/assistant/query；POST /workspace/assistant/query；GET /platform/approvals；GET /workspace/approvals；GET /identity/preferences；GET /workspace/deployment-summary",
        "PlatformDashboardProjection；WorkspaceDashboardProjection；DomainApprovalQueue；AssistantQueryProjection；AccountPreference；DeploymentDiagnosticProjection",
        "platform_dashboard.refreshed；workspace_dashboard.refreshed；approval.domain_rejected；account_preference.changed",
        "每个接口只接受对应PlatformSession、WorkspaceSession或GlobalSession；SupportSession不继承任一导航；助手引用来源继续逐对象鉴权；审批申请人不得跨域或自批。",
        "平台/Workspace同名页面不复用授权；迟到响应不污染切换后的页面；普通角色看不到配置路径、内部token状态或开发环境变量；当前路由可交互p75<1.5秒；语言切换不改变权限或owner。",
        "PRD-057/070/071/094/095/106/109/112/140..144", "页面切换p75<1.5秒；普通读p95<300ms；在线撤权<2秒", "9-11人周",
    ),
    planned(
        146, "S7-平台运营分域", "P1", "信息架构层", "企业工作区重组", "企业Gateway角色导航、任务首页与高级入口重组",
        "Workspace owner/admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer",
        "以现有64项导航处置矩阵重组企业Gateway：默认折叠并保持单组展开；首页按角色显示下一步任务；保留BYOK、Provider/模型/路由/评测、Project/Application、Virtual Key、额度预算、用量成本、企业内部财务、员工权限、安全策略、日志审计、Prompt、文件与文件安全、企业异步批处理、MCP、数据生命周期和私有部署；competitor-comparison移出产品UI；debug、intent测试、Webhook死信、环境诊断等仅高级角色按需进入。",
        "GET /workspace/navigation；GET /workspace/getting-started；PATCH /identity/preferences/navigation；GET /workspace/advanced-tools",
        "WorkspaceNavigationProjection；RoleTaskHome；NavigationPreference；AdvancedToolEntitlement；NavigationContractSnapshot",
        "workspace_navigation.changed；role_task.completed；advanced_tool.opened",
        "后端按Membership revision和permission返回页面；owner/admin添加职能用户但不能授予platform_*；service_account无浏览器导航；隐藏菜单不能替代API鉴权。",
        "64项现状导航每项有唯一处置且源码清单无遗漏；所有企业角色只看职责相关页；默认折叠、单组展开、直达URL负测试、真人鼠标主流程、390/1280/1440布局和无研发术语通过；每页只加载当前数据。",
        "PRD-094/097/119/120/139..145", "导航投影p95<200ms；路由切换p75<1.5秒；首屏关键任务p75<1.5秒", "8-10人周",
    ),
    planned(
        147, "S7-平台运营分域", "P0", "迁移治理层", "控制台分域切换", "存量控制台全面分域迁移与旧入口归零",
        "platform_owner/platform_ops/platform_security/迁移负责人",
        "按64项导航处置矩阵对代码、API、权限、表、事件、Scheduler、导航、DOM、文案、遥测和审计执行expand/dual-read/backfill/cutover/contract。盘点control_background_schedulers_enabled当前统一开关下安装的23个后台循环，逐个迁移执行身份、queue owner、lease和水位；Web副本不重复承担无界Worker。平台接管运营、品牌、Provider工程和商业目录；企业保留AI治理；拆分页使用独立主体；退役页面设置固定tombstone，不直接删除历史tenant_id事实。",
        "GET /platform/console-migration/status；POST /platform/console-migration/backfills；POST /platform/console-migration/cutovers；POST /platform/console-migration/rollbacks",
        "ConsoleDomainMigration；ConsoleBackfillCursor；LegacyRouteTombstone；NavigationContractSnapshot；DomainReconciliation",
        "console_domain.backfilled；console_domain.cutover；console_domain.rolled_back；legacy_route.retired",
        "platform_owner批准切换；platform_ops执行；platform_security复核跨域负测试；Workspace角色拒绝平台旧API；平台角色也不能用PlatformSession或旧WorkspaceSession绕过SupportGrant读取客户业务。",
        "所有迁移表行数/hash/outbox/审计/Scheduler水位保真；23个后台循环每项有唯一平台/Workspace执行域，Web多副本不重复消费；企业旧平台导航、DOM、预取、直达API归零；平台和企业新页面真实可用；N/N-1与回滚不复活越权；敏感与禁止文件审计0。",
        "PRD-131/137/139..146", "单Workspace切换<5分钟；10万Workspace回填<4小时；页面切换p75<1.5秒", "12-15人周",
    ),
    planned(
        148, "S8-正式发布", "P0", "发布治理层", "SaaS正式上线", "SaaS公开发布、迁移终验与全产品最终门禁",
        "产品负责人/研发/SRE/安全/测试/运营/财务/客服/法务",
        "在PRD-138发布候选和PRD-139..147控制台分域全部通过后执行公开发布：最终数据对账、权限负矩阵、真实注册到付费/企业治理/私有Gateway全流程、72小时长稳、回滚演练、支持排班、用户文档和发布后观察。",
        "GET /platform/ga-gates；POST /platform/ga-gates/{id}/evidence；POST /platform/ga-launches；POST /platform/ga-launches/{id}/rollback",
        "GaGate；GaEvidence；GaDecision；GaLaunch；GaRollback；PostGaReview",
        "ga.gate_failed；ga.approved；ga.launched；ga.rolled_back",
        "产品/SRE/安全/测试/财务/法务独立会签；platform_owner只执行已批准决策；任何单一角色都不能跳过迁移、安全、财务或隐私门禁。",
        "PRD-090..147全部完成；64项导航处置落地；平台/企业/伙伴/支持/设备五域负测试通过；WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW全组通过；72小时无P0/P1；README/Excel/docs/HEAD/origin一致。",
        "PRD-138/139..147", "Control 99.9%、Gateway 99.95%；关键页面p75<1.5s；连续72小时无P0/P1", "12-15人周",
    ),
]

# This is the end of the currently detailed planning batch, not a product-wide
# PRD ceiling. Appending the next contiguous PRD automatically extends it.
CURRENT_PLANNED_LAST_PRD = int(PRDS[-1]["id"].removeprefix("PRD-"))
NEXT_PLANNED_PRD = next(item["id"] for item in PRDS if item["status"] == "未启动")


LEGACY_TITLES = (
    "虚拟一体机镜像",
    "Kubernetes Helm Chart",
    "AI Gateway Appliance产品化",
    "硬件盒子安全加固",
    "添加设备与集群扩容向导",
    "容量预测与采购建议",
    "本地GPU成本核算",
    "License与容量授权",
    "离线增量补丁与回滚",
    "硬件盒子备份与整机恢复",
    "信创系统适配规划",
    "SBOM与镜像扫描",
    "常态化压测与混沌测试",
    "Prompt安全红蓝对抗演练",
    "API版本管理与兼容期",
    "套餐规格与资源配额",
    "渠道伙伴标准交付包",
    "客户价值报告/QBR",
    "跨境/境外模型可选合规模块预留",
)


PHASES = (
    ("S0", "PRD-090", "路线重基线", "冻结旧路线，完成架构/迁移/安全边界", "G0通过；旧090..108禁止开发", "不可并行跳过", "2-3人周"),
    ("S1", "PRD-091..094", "全局身份与Workspace", "注册账号不绑定租户；个人/企业空间和安全切换", "G1通过；旧账号无损迁移；跨Workspace零泄漏", "091设计后092/093可局部并行，094收口", "17-21人周"),
    ("S2", "PRD-095..105", "平台运营与商业服务", "平台总控、激活、套餐、计量、企业认证、支付、发票和通知", "G2通过；角色不越权；从试用到收款/退款/开票可核对", "095/096与097并行；098/099后接102；103/104依赖账务；105横向接入", "55-68人周"),
    ("S3", "PRD-106..113", "生产云运行与可靠性", "分布式任务、渐进发布、配置分发、SRE、反滥用、容灾、状态和容量", "G3通过；任务可恢复、发布可回退、多区域与100+租户压力达标", "106先行；107/108后接；109/110并行；111-113收口", "68-82人周"),
    ("S4", "PRD-114..119", "企业身份与数据治理", "SSO、SCIM、MFA、驻留、机器身份和官方Provider目录", "G4通过；企业身份生命周期和区域治理闭环", "114先行；115/116并行；117-119可并行集成", "44-56人周"),
    ("S5", "PRD-120..130", "云管私有Gateway", "Edge信任、配置、遥测、Secret、安装、Fleet、升级、授权、灾备和断网异步管理", "G5通过；在线/断网/升级/恢复均可验收", "120先行；121-123并行；124-126后接；127-130收口", "88-108人周"),
    ("S6", "PRD-131..138", "迁移、客户运营与发布候选", "迁移、客服、API生命周期、隐私、合规、供应链、隔离验证和RC", "G6通过；四波试点和发布候选门禁闭环，不对外GA", "131/132/133并行；134-137门禁化；138生成发布候选", "70-84人周"),
    ("S7", "PRD-139..147", "平台运营分域与企业Gateway重组", "把平台运营、品牌、Provider工程与商业目录迁出企业Gateway，企业保留角色化AI治理", "G7通过；64项导航唯一处置、历史保真、企业旧平台入口归零", "139/140可并行；141-145按域迁移；146重组；147统一切换", "80-98人周"),
    ("S8", "PRD-148", "SaaS正式发布", "全域迁移终验、公开发布和发布后观察", "G8通过；全部PRD完成、72小时长稳和最终Go/No-Go", "只能在G0..G7全部通过后执行", "12-15人周"),
)


PERMISSION_ROWS = (
    ("platform_owner", "平台", "全部平台治理；不得默认读取租户内容", "管理", "管理", "受控JIT", "管理", "管理", "管理"),
    ("platform_ops", "平台", "平台运维与Fleet", "只读", "管理", "受控JIT只读", "只读", "管理", "只读"),
    ("platform_security", "平台", "安全、合规与支持审批", "只读", "安全冻结", "审批后安全摘要", "无", "只读安全状态", "管理"),
    ("platform_billing", "平台", "套餐、订阅、调整", "只读", "只读", "无", "管理", "只读Entitlement", "只读"),
    ("platform_support", "平台", "客服诊断", "无", "只读脱敏目录", "JIT且客户授权", "无", "只读健康", "只读自身"),
    ("platform_verification", "平台", "企业主体审核", "无", "只读审核资料", "无", "只读合同状态", "无", "管理审核审计"),
    ("platform_compliance", "平台", "隐私与合规证据", "只读", "只读治理摘要", "无正文", "无", "只读驻留状态", "管理合规证据"),
    ("platform_privacy", "平台", "数据主体请求与同意治理", "只读", "只读隐私队列", "无业务正文", "无", "无", "管理隐私审计"),
    ("release_manager", "平台", "云与Edge制品发布", "无", "无", "无", "无", "发布/回滚", "管理发布审计"),
    ("platform_provider_engineer", "平台", "官方Provider Adapter、合同CI与错误目录", "无", "无", "无客户业务正文", "无", "只读Provider运行摘要", "管理Provider工程审计"),
    ("platform_channel", "平台", "渠道伙伴、商机与客户归属", "无", "只读归属摘要", "无客户业务内容", "只读结算摘要", "无", "管理渠道审计"),
    ("platform_customer_success", "平台", "客户运营、健康与续费协同", "无", "管理脱敏客户运营投影", "无客户业务正文", "只读续费摘要", "只读健康", "管理客户成功审计"),
    ("platform_growth", "平台", "获客、活动、漏斗与增长分析", "无", "只读聚合客户阶段", "无客户业务正文", "只读单位经济摘要", "无", "管理增长审计"),
    ("owner", "租户", "企业/个人空间最高Owner", "无", "管理本空间", "管理本空间", "管理订阅", "管理本空间部署", "管理本空间"),
    ("admin", "租户", "租户日常管理员", "无", "受限管理", "管理本空间", "按授权只读", "按授权管理", "管理本空间"),
    ("developer", "租户", "技术接入", "无", "无", "Provider/模型/Key按现有RBAC", "无", "只读部署状态", "只读技术审计"),
    ("security_admin", "租户", "租户安全", "无", "无", "策略/DLP/安全事件", "无", "管理遥测策略", "管理安全审计"),
    ("finance_admin", "租户", "租户财务", "无", "无", "只读用量归属", "管理账单/预算", "只读成本", "只读财务审计"),
    ("legal_admin", "租户", "合同、隐私与法律保全", "无", "只读本空间", "无正文", "只读合同/发票", "只读驻留状态", "管理保全/隐私审计"),
    ("auditor", "租户", "企业审计与合规复核", "无", "只读本空间", "只读治理摘要", "只读账单摘要", "只读部署摘要", "只读审计"),
    ("service_account", "租户", "非人类自动化主体", "无", "无", "仅授权Scope", "默认无", "默认无", "记录机器身份审计"),
    ("viewer", "租户", "租户只读", "无", "无", "只读明确授权页面", "只读摘要", "只读摘要", "只读"),
)


ROLE_PAGE_ROWS = (
    ("platform_owner", "平台", "平台总览；平台人员与角色；租户目录；品牌；套餐账务；SRE；安全合规；发布；渠道；客户成功；增长", "平台授权、全局配置和最终审批", "无SupportGrant不得读取客户业务内容", "显式bootstrap后由platform_owner按委派上限管理"),
    ("platform_ops", "平台", "平台总览；租户运行摘要；基础设施；Gateway Fleet；Provider与Cell运行健康；事故与容量", "运行健康、冻结/恢复、容量和事故处置", "Provider Adapter/合同CI维护；最终制品发布；客户业务正文；平台IAM授权；财务调整", "platform_owner或具备平台IAM委派权限者"),
    ("platform_security", "平台", "安全中心；风险；支持审批；隔离验证；安全审计", "风险冻结、审批和安全处置", "账务调整；默认客户正文", "platform_owner或具备平台IAM委派权限者"),
    ("platform_billing", "平台", "套餐目录；订阅；支付；退款；发票；争议裁决；对账；渠道结算摘要", "财务审核、调整、争议裁决和对账", "Provider Key/Prompt/Response；平台IAM；企业内部成本价", "platform_owner或具备平台IAM委派权限者"),
    ("platform_support", "平台", "工单；客户脱敏目录；本人SupportGrant；受控诊断", "工单处理；授权后的字段白名单诊断", "永久模拟租户；Key/Token/Prompt/Response", "platform_owner或具备平台IAM委派权限者"),
    ("platform_verification", "平台", "企业认证审核；补件；审核审计", "主体资料审核", "租户业务页；非审核所需字段", "platform_owner或具备平台IAM委派权限者"),
    ("platform_compliance", "平台", "合规控制；驻留证据；例外；审计证据", "合规评估和例外审批", "客户正文；账务和平台IAM", "platform_owner或具备平台IAM委派权限者"),
    ("platform_privacy", "平台", "隐私请求队列；同意；导出/删除决策；隐私审计", "最小字段处理数据主体请求", "其他Workspace列表和业务正文", "platform_owner或具备平台IAM委派权限者"),
    ("release_manager", "平台", "版本；制品；SBOM；渐进发布；回滚；发布审计", "签名发布和回滚", "租户成员、账务和业务内容", "platform_owner或具备平台IAM委派权限者"),
    ("platform_provider_engineer", "平台", "Provider Adapter目录；合同CI；内置错误规则；兼容性发布候选", "维护Adapter和运行合同测试；提交发布候选", "客户Provider Secret/请求正文；租户控制面；最终制品发布", "platform_owner或具备平台IAM委派权限者"),
    ("platform_channel", "平台", "渠道伙伴；代理商成员；商机报备；客户归属；佣金结算摘要", "渠道目录、商机和归属申请", "租户控制面；客户业务内容；财务最终调整", "platform_owner或具备平台IAM委派权限者"),
    ("platform_customer_success", "平台", "客户运营目录；健康；成功计划；续费协同；QBR摘要", "客户成功计划、跟进和脱敏运营记录", "租户控制面；客户业务正文；平台IAM", "platform_owner或具备平台IAM委派权限者"),
    ("platform_growth", "平台", "增长总览；漏斗；同期群；活动；实验；转化归因", "活动/实验和聚合分析", "租户控制面；客户业务正文；个体营销画像", "platform_owner或具备平台IAM委派权限者"),
    ("owner", "租户", "Workspace任务首页；成员与角色；BYOK/Provider；模型/路由/评测；项目/Key；额度预算；企业财务；安全；日志审计；Prompt/文件/批处理/MCP；数据生命周期；私有部署", "本空间最高权限、Owner转移和职能授权", "平台控制面；其他Workspace；平台品牌/套餐目录/争议裁决/Provider工程；渠道/客户管理/增长分析", "Workspace创建或经二次验证的Owner转移"),
    ("admin", "租户", "Workspace任务首页；成员；BYOK/Provider/模型/路由/Key；额度预算；策略；用量；Prompt/文件/批处理/MCP；数据生命周期；部署；常规审计", "邀请/停用成员并分配非Owner职能角色；日常治理", "授予owner；平台控制面；平台品牌/套餐目录/争议裁决/Provider工程；渠道/客户管理/增长分析；越过自身权限", "owner或具备成员管理权限的admin"),
    ("developer", "租户", "开发者快速开始；Provider/模型只读；Virtual Key；Prompt；文件；企业异步批处理；MCP；调试；调用日志；技术用量", "按授权管理技术接入资源", "成员、合同、付款、法律保全、平台页", "owner/admin在自身委派上限内"),
    ("security_admin", "租户", "安全中心；策略/DLP；Session；文件安全；MCP网络策略；审计；Edge遥测策略", "安全策略、会话撤权和风险处置", "付款调整；平台页；默认Provider Secret", "owner/admin在自身委派上限内"),
    ("finance_admin", "租户", "用量成本；BYOK成本价；额度预算；内部FinOps；订阅摘要；账单；争议申请；发票；对账", "本Workspace财务资料、预算和争议申请", "Provider Secret；平台套餐目录写入；争议裁决；成员授权；平台页", "owner/admin在自身委派上限内"),
    ("legal_admin", "租户", "合同主体；隐私；驻留；保留；法律保全；合规审计", "合同、隐私和保全操作", "Provider/Key；成员授权；平台页", "owner/admin在自身委派上限内"),
    ("auditor", "租户", "只读审计；安全摘要；账单摘要；部署摘要；合规证据", "导出授权范围内的审计证据", "任何写操作；Secret/正文；平台页", "owner/admin在自身委派上限内"),
    ("service_account", "租户", "无交互页面；仅机器身份详情由管理员查看", "仅后端签发的scope API", "浏览器登录；未授权scope；平台页", "owner/admin通过独立机器身份流程"),
    ("viewer", "租户", "Workspace总览；被授权的只读用量、日志和状态页", "只读", "所有写操作；敏感配置；平台页", "owner/admin在自身委派上限内"),
)


GATES = (
    ("G0", "PRD-090", "路线与架构冻结", "两轮设计自审；旧090..108引用扫描为0；服务/数据/权限/云边边界完整；回滚可执行"),
    ("G1", "PRD-091..094", "身份与Workspace", "无租户标识登录；全局账号多Workspace；动态撤权；跨Workspace 0泄漏；旧账号回填一致"),
    ("G2", "PRD-095..105", "平台运营与商业闭环", "平台/租户角色隔离；JIT支持；真实初始化；套餐/计量/支付/退款/开票可对账；失败可追踪"),
    ("G3", "PRD-106..113", "生产SaaS运行与可靠性", "分布式任务可恢复；渐进发布可回退；配置撤权传播达标；100+租户压力；SLO、反滥用和多区域灾备"),
    ("G4", "PRD-114..119", "企业身份与治理", "域名/SSO/SCIM/MFA全生命周期；驻留/保留生效；机器身份最小授权；Provider目录边界准确"),
    ("G5", "PRD-120..130", "云管私有Gateway", "mTLS设备身份；签名快照/LKG；敏感遥测0；本地Secret不上云；安装、升级、授权、恢复和断网包通过"),
    ("G6", "PRD-131..138", "迁移与发布候选", "全11类核心产品QA；四波试点；72小时长稳；灾备/支付/Edge演练；合规/供应链/隔离门禁；只生成RC，不标记公开GA"),
    ("G7", "PRD-139..147", "平台运营分域与企业Gateway重组", "源码64项导航处置100%；平台运营/品牌/Provider工程/商业账务分域；历史行数/hash/outbox/Scheduler/审计水位保真；企业旧平台入口、DOM、预取/API归零"),
    ("G8", "PRD-148", "SaaS正式上线", "PRD-090..147全部完成；全域11类QA；72小时长稳；最终迁移/安全/财务/隐私会签；HEAD与origin一致"),
)


RISKS = (
    ("R-01", "P0", "平台角色与租户角色混用导致越权", "PRD-090/095/137", "双命名空间、JIT支持、后端逐请求授权、跨域同形404", "Security Owner"),
    ("R-02", "P0", "旧tenant-bound账号迁移破坏登录或Membership", "PRD-091/094/131", "双写、回填hash、会话兼容期、分波切换和可回退", "Identity Owner"),
    ("R-03", "P0", "支付回调乱序或重复造成重复权益/账务", "PRD-099/103/104", "验签、幂等收据、账本序列、三方对账和差异工单", "Billing Owner"),
    ("R-04", "P0", "共享Gateway Cell出现跨租户或噪声邻居", "PRD-107/108/113/137", "复合owner、容量舱壁、压力隔离、持续IDOR/缓存污染测试", "Gateway Owner"),
    ("R-05", "P0", "故障切换违反数据驻留", "PRD-111/117", "Region硬约束、禁止隐式跨区、演练与证据", "SRE + Compliance"),
    ("R-06", "P0", "云边身份复制或错误签名配置控制私有节点", "PRD-120/121/127/136", "设备密钥本地生成、mTLS、签名制品、吊销和防回放", "Edge Security"),
    ("R-07", "P0", "本地Provider Secret泄漏到云端或支持包", "PRD-122/123/125/129/130", "字段白名单、本地托管、自动敏感扫描和客户预览", "Edge Security"),
    ("R-08", "P1", "断网双向变更产生不可解释覆盖", "PRD-121/130", "单调水位、版本向量、冲突报告和人工决策", "Edge Owner"),
    ("R-09", "P1", "旧PRD-090..108范围被误当作待开发", "PRD-090", "旧表只读保留；新工作簿和Markdown为唯一依据；CI/评审扫描", "Product Owner"),
    ("R-10", "P0", "为赶发布跳过安全、迁移或真实运行门禁", "PRD-138/148", "RC与GA两段不可单人绕过的Go/No-Go会签；证据缺失即阻断", "Release Owner"),
    ("R-11", "P0", "渠道角色或伙伴身份枚举、读取或改绑非归属客户", "PRD-095/139", "独立页面/API权限、复合归属、客户确认、平台双审批、跨客户同形失败", "Channel + Security Owner"),
    ("R-12", "P0", "平台客户管理或增长分析被错误暴露到企业Workspace", "PRD-095/140/141", "平台独立路由和主体；企业导航/DOM/预取/API负测试；聚合数据最小化", "Growth + Security Owner"),
    ("R-13", "P0", "直接删除或改写旧租户控制台模块造成历史、账务、outbox、Scheduler或审计断链", "PRD-139..147", "expand/dual-read/backfill/cutover/contract；逐表hash和水位；旧路由tombstone；可回滚读路径", "Migration Owner"),
    ("R-14", "P0", "套餐目录、平台账单与企业BYOK内部成本账混域导致越权或金额错误", "PRD-098/099/144", "双账本域、独立角色、价格版本、争议申请/裁决分离和双边对账", "Billing + FinOps Owner"),
    ("R-15", "P0", "Adapter CI、内置错误规则或内部配置暴露给普通企业用户", "PRD-119/143/146", "Provider工程台平台化、企业DTO白名单、高级入口分级和源码导航合同测试", "Provider + Security Owner"),
)


# Exact disposition of the tenant Console navigation frozen by PRD-090. This
# inventory is intentionally exhaustive: the validator compares these IDs with
# NAVIGATION_DEFINITIONS in the control-plane source so future items cannot be
# silently omitted from the SaaS/domain migration plan.
NAVIGATION_DISPOSITION_ROWS = (
    ("总览", "overview", "概览", "平台/企业拆分", "企业任务首页；平台运营总览", "owner/admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer", "platform_owner/platform_ops/platform_security/platform_billing/platform_support", "GET /dashboard/overview、/dashboard/role-view；租户投影保留，平台另建聚合投影", "PRD-094/095/145/146", "两域Session、缓存、预取和迟到响应完全隔离"),
    ("总览", "admin-assistant", "管理助手", "平台/企业拆分", "企业治理助手；平台运营助手", "owner/admin及获授权职能角色", "platform_owner/platform_ops/platform_security/platform_billing", "POST /admin-assistant/query；来源仍逐对象鉴权，平台另建知识投影", "PRD-095/145", "不得把平台运营事实或其他Workspace事实带入企业回答"),
    ("总览", "approvals", "审批中心", "平台/企业拆分", "企业审批；平台审批", "owner/admin/security_admin/finance_admin/legal_admin", "platform_owner/platform_security/platform_billing/platform_compliance/release_manager", "GET/POST /approvals/*；现有企业预算/风险审批保留，平台建立独立队列", "PRD-095/145", "申请、审批、step-up和审计均绑定actor domain；禁止跨域和自批"),
    ("总览", "growth-analytics", "增长分析", "迁出企业", "SaaS平台增长域", "无", "platform_growth/platform_channel/platform_customer_success/platform_owner", "GET /product-analytics/funnel；product_events、product_event_outbox及触发器", "PRD-141/147", "企业导航、DOM、预取和旧API归零；历史事件/hash/outbox水位保真"),
    ("总览", "customer-health", "客户健康", "迁出企业", "SaaS平台客户成功域", "无", "platform_customer_success/platform_support/platform_billing/platform_owner", "GET /customer-health；customer_health_scheduler_tenants、scores、score_outbox及Scheduler", "PRD-140/147", "企业不查看平台客户健康；Scheduler与历史评分水位保真"),
    ("总览", "partner-portal", "伙伴门户", "迁出企业", "SaaS平台渠道域与独立伙伴入口", "无", "platform_channel/platform_billing/platform_owner；伙伴身份", "GET/POST /partners/*；partners、partner_customers、commission_records、outbox及Scheduler", "PRD-139/147", "企业角色全部拒绝；伙伴只能看归属客户；佣金与审计可对账"),
    ("配置", "branding", "白标设置", "平台/私有部署拆分", "平台品牌/渠道白标/Deployment外观", "owner仅管理本企业私有Deployment外观", "platform_owner/platform_channel/release_manager", "GET/PATCH /branding；branding_configs、branding_events、branding_event_outbox及Scheduler", "PRD-142/147", "企业SaaS不能改平台登录页；域名唯一验证；旧事件与outbox保真"),
    ("配置", "tenant", "租户设置", "企业保留并改名", "企业资料与Workspace设置", "owner/admin；auditor只读", "platform_ops只读脱敏生命周期", "现有tenant.settings.read；Tenant通过不可变绑定投影为Workspace", "PRD-093/094/096/146", "企业可维护本企业资料，不能修改tenant/workspace绑定或平台状态"),
    ("配置", "users", "用户与角色", "企业保留并改名", "成员与职能角色", "owner/admin", "无", "现有user.manage、邀请与users表；渐进映射GlobalAccount+Membership", "PRD-091/094/115/146", "owner/admin添加本企业职能用户；不能授予platform_*或越过委派上限"),
    ("租户治理", "role-permissions", "角色权限", "企业保留", "企业角色与权限", "owner/admin；auditor只读结果", "无", "现有rbac.matrix.manage、roles/permissions/role_permissions", "PRD-094/115/146", "后端权限为权威；service_account走机器身份流程"),
    ("配置", "environments", "环境设置", "企业保留并归组", "Gateway环境与发布边界", "owner/admin/developer；auditor只读", "platform_ops仅看运行摘要", "现有environment.read及dev/test/prod owner关系", "PRD-101/108/146", "环境、Key、路由、预算均绑定本Workspace；普通viewer不见写入口"),
    ("配置", "providers", "Provider 与 KeyPool", "企业保留", "BYOK与上游接入", "owner/admin/developer按权限", "platform_support默认无；仅目录角色看非Secret元数据", "现有provider.read及Provider/KeyPool；Secret继续独立托管", "PRD-100/119/123/146", "Key不进入日志、平台支持或验收证据；默认官方上游边界不变"),
    ("配置", "local-models", "本地模型", "企业保留并归组", "本地OpenAI-compatible Endpoint", "owner/admin/developer按权限", "platform_ops只看脱敏Deployment健康", "现有local_endpoint.read、local_endpoints模块", "PRD-119/120/123/146", "Endpoint和本地Secret属于Workspace/Deployment；平台不得任意读取"),
    ("配置", "private-gateways", "私有 Gateway", "企业保留并归组", "客户自有Edge Deployment", "owner/admin管理；developer/security_admin按权限只读", "platform_ops只看独立Fleet脱敏投影", "GET/POST /edge/deployments、/edge/registrations、/edge/deployments/{id}/revoke；edge_deployments及设备身份表", "PRD-120..130/146", "Workspace与Platform使用独立Session/API/DTO；注册材料、设备私钥、本地Secret和业务正文不得进入平台投影"),
    ("配置", "adapter-specs", "Adapter 规范", "迁出企业", "平台Provider工程台；企业Provider详情另建兼容摘要", "无", "platform_provider_engineer/platform_security/release_manager", "GET /adapter-specs；POST /internal/adapter-specs/{tenant_id}/activate；provider_adapter_catalog_activations", "PRD-143/147", "企业旧规范导航、DOM、预取和API归零；兼容摘要由Provider详情独立鉴权，不暴露内部schema、构建信息或发布权限"),
    ("配置", "adapter-contract-runs", "CI 测试报告", "迁出企业", "平台Provider工程台", "无", "platform_provider_engineer/platform_security/release_manager", "GET /adapter-contract-runs及/{id}；POST /internal/tenants/{tenant_id}/adapter-contract-runs；runs/cases/outbox", "PRD-143/147", "Runner、case详情和构建路径不进入企业DTO；企业兼容结论改在Provider详情提供；旧run/outbox水位保真"),
    ("配置", "provider-health", "Provider 状态", "平台/企业拆分", "本企业Provider健康；平台全局Provider/Cell健康", "owner/admin/developer/security_admin/viewer按授权", "platform_ops/platform_security/platform_support只读脱敏", "GET /provider-health、POST /providers/{id}/probe；真实调用与探测事实", "PRD-109/112/119/143/145", "企业只见自身Endpoint/调用；平台聚合不得泄漏业务正文或Secret"),
    ("配置", "error-maps", "错误码管理", "平台/企业拆分并降级", "平台内置目录；企业受限覆盖", "owner/admin/developer高级入口", "platform_provider_engineer/platform_security/release_manager", "GET/POST/PATCH /error-maps；builtin catalog、maps、versions、heads、events、facts", "PRD-143/147", "企业不能改内置规则；覆盖只作用本Workspace且每个失败请求可解释追踪"),
    ("配置", "models", "模型目录", "企业保留", "企业可用模型", "owner/admin/developer；viewer按授权只读", "平台Provider目录仅提供发布源", "GET/POST/PATCH /models；模型owner保留tenant_id", "PRD-119/146", "企业选择并治理自身可用模型；平台目录更新不能自动扩权"),
    ("配置", "model-aliases", "模型别名", "企业保留", "稳定业务模型别名", "owner/admin/developer按权限", "无", "现有routing.read、model_aliases模块", "PRD-101/108/146", "别名、环境和路由均绑定Workspace，切换可回滚"),
    ("配置", "routing-policies", "路由策略", "企业保留", "企业模型路由", "owner/admin/developer按权限；auditor只读", "无", "现有routing.read及固定/成本/可用性路由", "PRD-101/108/146", "后端准入与签名快照生效；前端隐藏不构成权限"),
    ("配置", "model-quality-scores", "模型评分", "企业保留", "企业模型质量与SLO", "owner/admin/developer/viewer按授权", "platform_ops只看平台运行聚合", "现有model_quality.read、真实成功率/延迟与人工先验", "PRD-109/119/146", "企业评分不成为平台客户健康或增长画像；聚合最小化"),
    ("配置", "experiments", "模型评测", "企业保留", "企业模型评测与A/B", "owner/admin/developer按权限；auditor只读", "无", "现有experiment.read及不可变实验配置/结果", "PRD-101/106/146", "分流、停止、用量和成本都绑定本Workspace并可审计"),
    ("调用与成本", "projects", "项目与应用", "企业保留", "企业项目与应用", "owner/admin/developer；finance/security按范围只读", "无", "现有project.read；Project/Application为Key、日志、预算归属", "PRD-094/146", "所有对象带tenant_id；职能角色只见授权项目"),
    ("调用与成本", "virtual-keys", "Virtual Key", "企业保留", "业务调用凭据", "owner/admin/developer按权限", "platform_support与平台运营均不可读Secret", "现有virtual_key.read及Key生命周期", "PRD-100/118/146", "只显示一次的Secret不入日志；撤销、失败、用量和成本可追踪"),
    ("调用与成本", "usage", "用量与成本", "企业保留；平台另建计量", "企业技术用量与内部成本", "owner/admin/developer/finance_admin/auditor/viewer按字段", "platform_billing只见平台商业计量", "GET /usage/*；Usage与价格快照", "PRD-099/122/144/146", "平台计量与企业业务用量字段分域；成功失败取消重试均可追踪"),
    ("调用与成本", "finops-ledger", "FinOps 账本", "企业保留；平台账本拆分", "企业内部FinOps", "owner/finance_admin；auditor只读", "platform_billing仅平台商业账", "GET/POST /finops/ledger；不可变分录与价格证据", "PRD-099/104/144/146", "企业内部成本调整与平台收费分录不能互相写入或冒充"),
    ("调用与成本", "model-prices", "模型价格", "平台/企业拆分", "平台官方建议价；企业BYOK成本价", "owner/finance_admin；developer只读可用价格", "platform_billing/release_manager", "GET/POST /model-prices；历史/当前/未来价格与Usage快照", "PRD-098/119/144/147", "价格来源和版本明确；企业不能发布平台价，平台不能改企业私有成本"),
    ("调用与成本", "response-cache", "响应缓存", "企业保留并入高级配置", "模型别名/路由详情的缓存策略", "owner/admin/developer/security_admin按权限", "无", "现有response_cache.read及按模型别名的可选能力", "PRD-101/108/117/146", "不占普通一级导航；默认不缓存正文；租户、模型、保留和计费owner一致"),
    ("调用与成本", "plans", "套餐管理", "平台/企业拆分并改名", "平台套餐目录；企业订阅摘要", "owner/finance_admin只读本Workspace订阅", "platform_owner/platform_billing", "GET/PATCH /plans；plan/trial/quota事实", "PRD-098/144/147", "企业无套餐目录写入；Entitlement后端生效且回滚不扩大权益"),
    ("调用与成本", "quota-buckets", "额度管理", "企业保留；平台发放拆分", "企业内部额度分配与消费", "owner/admin/finance_admin；developer只读自身", "platform_billing管理Entitlement上限而非企业分配", "GET /quota-buckets；POST /quota-adjustments；四类额度及消费", "PRD-098/099/144/146", "平台权益上限和企业Project/Key额度分别授权、共同失败关闭"),
    ("调用与成本", "billing-disputes", "账单争议", "平台/企业拆分", "企业争议申请；平台争议裁决", "owner/finance_admin提交和查看自身", "platform_billing裁决；platform_support协作但不裁决", "GET/POST/PATCH /billing-disputes；0073争议/调整状态机", "PRD-104/132/144/147", "申请人不能裁决；金额调整不可变、双审批、统一审计和request_id"),
    ("调用与成本", "budgets", "预算中心", "企业保留", "企业多级预算", "owner/admin/finance_admin；developer/auditor按范围只读", "无", "GET/POST/PATCH /budgets及/budget-status", "PRD-099/106/146", "tenant/Project/Key预算后端准入；失败、阻断、审批和成本可追踪"),
    ("调用与成本", "cost-anomalies", "成本异常", "企业保留；平台另建商业异常", "企业BYOK与内部成本异常", "owner/finance_admin/security_admin；auditor只读", "platform_billing只看平台收费异常", "GET/PATCH /cost-anomalies；runs/anomalies/actions/temporary overrides", "PRD-099/106/109/144/146", "企业异常不泄漏到其他客户；平台异常不得获得Prompt/Response"),
    ("安全与治理", "policies", "策略管理", "企业保留", "企业治理策略", "owner/admin/security_admin；developer/auditor只读授权范围", "无", "现有policy.read及受限DSL", "PRD-108/117/146", "策略owner、版本、发布和回滚可审计；失效按安全类别失败关闭"),
    ("安全与治理", "policy-simulator", "策略模拟器", "企业保留并归高级工具", "企业策略预演", "owner/admin/security_admin/developer按权限", "无", "现有policy.simulate；不调用真实Provider", "PRD-108/146", "预演不能改变生产事实；输入正文不进入日志或验收证据"),
    ("安全与治理", "policy-snapshots", "策略发布", "企业保留", "企业策略版本与回滚", "owner/admin/security_admin；auditor只读", "无", "现有policy.read及不可变快照", "PRD-108/121/146", "签名、generation、LKG和动态撤权合同生效"),
    ("安全与治理", "policy-templates", "策略模板库", "平台/企业拆分", "平台模板目录；企业应用与覆盖", "owner/admin/security_admin应用；auditor只读", "platform_security/platform_compliance/release_manager发布", "GET /policy-templates；POST /policy-templates/{policy_template_id}/apply；内置模板、适配和绑定事实", "PRD-117/119/143/146", "企业不能篡改内置模板；套用产生tenant-scoped版本并可回滚"),
    ("安全与治理", "dlp-rules", "DLP 规则", "企业保留", "企业DLP", "owner/admin/security_admin；auditor只读", "无", "现有dlp.read及log_only/block规则", "PRD-108/117/146", "Provider前失败关闭；命中证据不保存Prompt/Response正文"),
    ("安全与治理", "intent-security", "敏感意图分类", "企业保留并降级", "安全管理员测试工具", "security_admin；owner/admin按权限", "无", "现有intent_risk.classify；本地轻量规则", "PRD-110/146", "不作为普通导航；结果不含原文/命中片段；限流与审计"),
    ("安全与治理", "session-risks", "高危会话", "企业保留", "企业会话风险处置", "owner/admin/security_admin；auditor只读", "platform_support仅经SupportGrant看固定摘要", "现有session_risk.read及冻结/审批记录", "PRD-116/137/146", "处置绑定Workspace并动态撤权；平台默认看不到会话正文"),
    ("安全与治理", "output-safety", "输出安全事件", "企业保留", "企业输出安全", "owner/admin/security_admin；auditor只读", "platform_security只看脱敏聚合", "现有request_log.read的阻断/扫描事实", "PRD-108/109/117/146", "正文默认不持久化；事件含owner、code、request_id而非输出明文"),
    ("安全与治理", "webhooks", "Webhook 设置", "企业保留", "企业治理事件订阅", "owner/admin/security_admin", "无", "现有webhook.manage、Endpoint与签名Secret", "PRD-105/133/146", "签名Secret只显示一次；SSRF、重放、出站ACL和审计通过"),
    ("安全与治理", "webhook-dead-letters", "Webhook 死信", "企业保留并降级", "企业高级运维入口", "owner/admin/security_admin", "无", "现有webhook.manage、持久死信与安全重放", "PRD-106/146", "不默认出现在普通导航；重放幂等、限次且不能修改冻结payload"),
    ("安全与治理", "audit-logs", "审计日志", "企业保留；平台审计拆分", "企业不可变审计", "owner/admin/security_admin/legal_admin/auditor", "各platform_*只见自身平台审计；支持受Grant限制", "现有audit.read及不可变audit_logs", "PRD-095/117/135/145/146", "平台和企业审计域分离；Key/Token/Prompt/Response不入证据"),
    ("调用与成本", "request-logs", "调用日志", "企业保留", "企业调用可观测性", "owner/admin/developer/security_admin/auditor/viewer按字段", "platform_support仅JIT白名单摘要", "现有request_log.read及分页/trace", "PRD-109/117/146", "只加载当前页；正文/Secret不记录；失败与用量成本可关联"),
    ("调用与成本", "request-explanations", "请求解释", "企业保留并入日志详情", "调用日志详情的路由/策略/成本解释", "owner/admin/developer/security_admin/finance_admin/auditor按字段", "platform_support仅JIT白名单摘要", "现有request_explanation.read", "PRD-108/109/146", "不占普通一级导航；解释字段按职能裁剪；不能通过解释恢复Prompt/Response或Key"),
    ("调用与成本", "feedback-summary", "反馈报表", "企业保留", "企业模型反馈与质量", "owner/admin/developer/viewer按权限", "platform_customer_success只读脱敏采用摘要，不读反馈正文", "现有feedback.use及聚合报表", "PRD-109/140/146", "企业反馈不成为平台个体画像；跨域只传最小聚合"),
    ("开发者", "quickstart", "快速开始", "企业保留并任务化", "角色化初始化向导", "owner/admin/developer", "platform_support只协助脱敏状态", "现有developer_portal.read；静态步骤改为真实后端完成事实", "PRD-097/146", "步骤可点击、可查看证据、失败可恢复，不以假数据标记已完成"),
    ("开发者", "sdk-examples", "SDK 示例", "企业保留并入开发者中心", "开发者中心的SDK示例", "developer；owner/admin按需", "无", "现有developer_portal.read及示例", "PRD-133/146", "不占普通一级导航；示例使用占位值，不含真实Key；与N/N-1合同一致"),
    ("开发者", "migration-guide", "迁移指南", "企业保留并入开发者中心", "开发者中心的迁移兼容指南", "owner/admin/developer", "platform_support可见公开内容", "现有developer_portal.read；迁移平台兼容说明", "PRD-131/133/146", "不占普通一级导航；OpenRouter/硅基流动仅作为迁移兼容对象，不列为默认上游"),
    ("开发者", "api-docs", "API 文档", "企业保留并入开发者中心", "开发者中心的API文档", "developer；owner/admin按需", "platform_support只见公开合同", "现有console.basic.read；API合同与SDK版本", "PRD-133/146", "不占普通一级导航；文档示例无Secret；错误码/request_id、版本和废弃期明确"),
    ("帮助中心", "competitor-comparison", "竞品对比", "迁出产品控制台", "平台市场/销售内容或外部文档", "无", "platform_growth/platform_channel只管理已审核内容", "当前主要为console-web静态render，无租户业务API", "PRD-141/146/147", "企业登录后产品UI不展示销售对比；迁移兼容说明仍可在迁移指南出现"),
    ("开发者", "enterprise-chat", "企业 Chat", "企业保留", "企业AI应用", "获授权业务用户；配置由owner/admin/developer", "无", "现有chat.use及企业会话owner", "PRD-116/117/146", "会话和正文按租户策略处理；不同职能页面与动作不同"),
    ("开发者", "prompt-templates", "Prompt 模板", "企业保留", "企业Prompt资产", "owner/admin/developer及获授权用户", "platform_support无正文", "现有prompt_template.read及企业模板库", "PRD-117/146", "Prompt不得进入日志或验收证据；版本/权限/审计绑定tenant"),
    ("开发者", "files", "文件处理", "企业保留", "企业文件调用工具", "owner/admin/developer及获授权用户", "platform_support无正文", "现有file.read、提取与处理任务", "PRD-117/122/146", "文件正文、对象路径和提取文本受驻留/保留/DLP约束"),
    ("安全与治理", "file-security", "文件安全报告", "企业保留并入详情", "文件详情与安全中心", "owner/admin/security_admin/developer/auditor按字段", "platform_security只看脱敏聚合", "现有file.read、0066 outbox恢复事实", "PRD-117/122/137/146", "不占普通一级导航；不重复实现PRD-073；报告不含正文，outbox水位可恢复"),
    ("开发者", "batch-jobs", "批处理任务", "企业保留", "企业异步批处理", "owner/admin/developer；finance/auditor只读归属摘要", "platform_ops只看平台队列健康", "现有batch.read、0067 Scheduler/Worker", "PRD-106/113/146", "job/lease/副作用均带tenant owner；企业看不到其他租户或平台队列内部"),
    ("开发者", "mcp-tools", "MCP 工具", "企业保留并入开发者中心", "开发者中心的MCP治理", "owner/admin/developer/security_admin按权限", "无", "现有mcp.read及注册/权限/网络ACL", "PRD-110/117/118/146", "不占普通一级导航；工具scope、出站网络和机器身份最小授权；Secret不入日志"),
    ("租户治理", "tenant-data-lifecycle", "数据管理", "企业保留并改名", "Workspace数据与隐私", "owner/legal_admin/security_admin/auditor按动作", "platform_privacy只处理全局账号请求和最小队列", "现有tenant_data.read、导出/删除/注销状态", "PRD-117/134/146", "复核tenant_data_states；导出/删除不允许平台支持绕过客户授权"),
    ("开发者", "debug-console", "OpenAI 调试台", "企业保留并降级", "开发者高级工具", "developer；owner/admin按权限", "无", "现有debug_console.use；真实Gateway调用", "PRD-110/133/146", "默认不向非开发角色展示；Key/Prompt/Response不进入日志或验收证据"),
    ("系统", "system-status", "系统状态", "平台/企业拆分", "Workspace服务状态；平台SRE状态", "owner/admin/developer/viewer按授权", "platform_ops/platform_support/platform_owner", "现有console.basic.read/provider status；平台另建Cell/Fleet/依赖状态", "PRD-109/112/145", "企业只见与自身相关的可操作状态；平台状态不暴露客户业务数据"),
    ("系统", "language-settings", "语言设置", "迁到全局账号壳层", "GlobalAccount偏好", "所有交互式账号本人", "所有平台交互式账号本人", "现有user_preferences；不再作为tenant业务页面", "PRD-091/105/145", "语言不携带tenant角色、不改变权限；跨Workspace一致且可回退"),
    ("系统", "config-summary", "环境信息", "平台/企业拆分并降级", "Deployment诊断；平台运行配置摘要", "owner/admin/developer高级入口；auditor只读脱敏", "platform_ops/platform_security；support需Grant", "GET /internal/config/summary；当前system.config.read", "PRD-109/120/125/145/147", "普通用户不见环境变量、路径、内部token或研发配置；平台和企业DTO白名单分离"),
)


HEADER_FILL = PatternFill("solid", fgColor="16324F")
SECTION_FILL = PatternFill("solid", fgColor="DCE6F1")
P0_FILL = PatternFill("solid", fgColor="F4CCCC")
P1_FILL = PatternFill("solid", fgColor="FFF2CC")
P2_FILL = PatternFill("solid", fgColor="D9EAD3")
MUTED_FILL = PatternFill("solid", fgColor="E7E6E6")
WHITE_FONT = Font(color="FFFFFF", bold=True)
THIN = Side(style="thin", color="B7C4CE")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_table(ws, widths: dict[int, float] | None = None) -> None:
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[1].height = 34
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER
        ws.row_dimensions[row[0].row].height = 66
    if widths:
        for column, width in widths.items():
            ws.column_dimensions[get_column_letter(column)].width = width


def add_readme_sheet(wb: Workbook) -> None:
    ws = wb.active
    ws.title = "00_变更说明"
    ws.sheet_view.showGridLines = False
    rows = (
        ("MetaHub SaaS 增量开发计划", f"PRD-{FIRST_PRD:03d} 起（当前已细化至 PRD-{CURRENT_PLANNED_LAST_PRD:03d}，后续开放追加）"),
        ("计划日期", PLAN_DATE),
        ("权威状态", "本工作簿是PRD-090起唯一开发依据；旧总清单PRD-090..108全部废弃、禁止开发，仅保留历史审计。"),
        ("编号策略", f"保留PRD-001..089完成事实；从PRD-090重新规划，不设需求总数或最终编号上限；当前已细化{len(PRDS)}条、至PRD-{CURRENT_PLANNED_LAST_PRD:03d}，后续按连续编号追加并记录变更。"),
        ("总体状态", f"saas_incremental_roadmap_complete=false；兼容旧状态all_108_prds_complete=false；PRD-090..{COMPLETED_THROUGH_PRD:03d}已完成，下一项{NEXT_PLANNED_PRD}。"),
        ("产品目标", "云端SaaS支持个人和企业自助注册；平台团队统一运营；企业可部署本地Gateway并由SaaS云管。"),
        ("标准模式", "个人SaaS、企业SaaS、云管私有Gateway；完全离线仅支持签名包异步管理，不承诺云端实时管理。"),
        ("身份原则", "全局账号不绑定单租户；Workspace承载个人/企业；Membership恢复租户角色；登录不填写租户标识。"),
        ("权限原则", "平台角色与租户角色完全分域；平台支持默认看不到Provider Key、Token、Prompt、Response或客户正文。"),
        ("页面原则", "平台员工由平台IAM分配角色；企业owner/admin添加职能用户；导航由后端按角色/权限计算，直达URL和API仍逐请求鉴权；渠道、客户、增长、平台品牌、Provider工程、套餐目录、商业账务裁决和平台SRE不进入企业Workspace。"),
        ("现有导航归属", "源码64项：45项企业Gateway保留，12项拆为独立平台/企业投影，5项完全迁入SaaS平台，1项迁到GlobalAccount壳层，1项退出登录后产品UI；不得只隐藏菜单，DOM、预取、API和数据owner必须同步切换。"),
        ("数据原则", "所有业务数据继续携带tenant_id/workspace_id；后端授权权威；统一错误码和request_id；用量、成本和失败可追踪。"),
        ("云边原则", "本地Gateway仅出站mTLS；云端下发签名期望状态；本地使用LKG；Provider Key默认只保存在本地。"),
        ("默认上游", "智谱、通义、月之暗面、文心、火山、混元、DeepSeek官方/云服务及客户本地OpenAI-compatible Endpoint。"),
        ("竞品边界", "OpenRouter、硅基流动只作为竞品和迁移兼容对象，不是默认上游。"),
        ("执行纪律", "每条PRD先设计、两轮自审修正，再编码、专项测试、迁移、文档、真实运行验收、安全审计、提交和推送。"),
    )
    for row in rows:
        ws.append(row)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 115
    for index, row in enumerate(ws.iter_rows(), start=1):
        row[0].font = Font(bold=True, color="16324F")
        row[0].fill = SECTION_FILL
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER
        ws.row_dimensions[index].height = 38 if index > 1 else 52
    ws["A1"].fill = HEADER_FILL
    ws["A1"].font = WHITE_FONT
    ws["B1"].fill = HEADER_FILL
    ws["B1"].font = WHITE_FONT


def add_prd_sheet(wb: Workbook) -> None:
    headers = (
        "PRD_ID", "计划状态", "阶段", "优先级", "能力层", "模块", "PRD名称", "主要用户/角色", "用户故事",
        "需求范围/功能说明", "明确不做/边界", "页面/入口", "API/动作", "核心数据对象", "事件/Webhook",
        "关键权限", "验收标准", "依赖关系", "数据迁移/回滚", "安全与合规", "测试重点", "性能/SLO",
        "预计工作量", "建议团队", "交付证据",
    )
    ws = wb.create_sheet("01_PRD总表")
    ws.append(headers)
    for item in PRDS:
        ws.append(
            (
                item["id"], item["status"], item["stage"], item["priority"], item["layer"], item["module"], item["title"],
                item["users"], item["story"], item["scope"], item["boundary"], item["pages"], "\n".join(item["apis"]),
                "、".join(item["objects"]), "\n".join(item["events"]) or "无", item["permissions"], item["acceptance"],
                item["dependencies"], item["migration"], item["security"], item["tests"], item["slo"], item["effort"],
                item["team"], item["evidence"],
            )
        )
    widths = {1: 12, 2: 11, 3: 17, 4: 9, 5: 16, 6: 16, 7: 30, 8: 24, 9: 42, 10: 60, 11: 48,
              12: 34, 13: 48, 14: 42, 15: 42, 16: 45, 17: 62, 18: 26, 19: 52, 20: 52, 21: 52,
              22: 42, 23: 14, 24: 30, 25: 58}
    style_table(ws, widths)
    for row in ws.iter_rows(min_row=2):
        row[3].fill = {"P0": P0_FILL, "P1": P1_FILL, "P2": P2_FILL}.get(row[3].value, MUTED_FILL)
        row[0].font = Font(bold=True, color="16324F")
        ws.row_dimensions[row[0].row].height = 128


def add_roadmap_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("02_阶段路线图")
    ws.append(("阶段", "PRD范围", "阶段名称", "目标", "退出标准", "并行策略", "总工作量参考"))
    for row in PHASES:
        ws.append(row)
    style_table(ws, {1: 10, 2: 18, 3: 24, 4: 55, 5: 65, 6: 48, 7: 18})


def add_objects_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("03_核心数据对象")
    ws.append(("PRD_ID", "数据对象", "归属域", "租户字段规则", "生命周期/关键约束"))
    global_objects = {
        "GlobalAccount", "AccountCredential", "GlobalSession", "AccountIdentityLink", "RegistrationIntent",
        "VerificationChallenge", "PasswordResetIntent", "AccountVerification", "ConsentRecord", "AbuseCounter",
        "InvitationIdentityBinding", "IdentityLifecycleEvent", "IdentityLifecycleOutbox", "PlatformOperator", "PlatformRole",
        "PlatformPermission", "PlatformAuditLog", "PlanCatalog", "PlanVersion", "DatabaseCluster", "CacheCluster",
        "ObjectStore", "KeyRing", "GatewayCell", "GatewayRelease", "ServiceCatalogEntry", "ServiceLevelObjective",
        "ErrorBudget", "Alert", "OnCallRoute", "Region", "FailoverPlan", "DisasterRecoveryRun", "RecoveryPoint",
        "StatusComponent", "Incident", "IncidentUpdate", "CapacityForecast", "PerformanceBaseline", "FeatureFlag",
        "FlagVersion", "ProviderCatalogEntry", "ProviderCapability", "RegionalEndpointTemplate", "ApiVersion",
        "ApiContractSnapshot", "SdkRelease", "DeprecationNotice", "WebhookSchemaVersion", "ComplianceControl",
        "ControlMapping", "SbomReport", "ArtifactSignature", "BuildProvenance", "VulnerabilityFinding", "LaunchGate",
        "LaunchWave", "GoNoGoDecision", "PlatformBrand", "ChannelBrand", "VerifiedDomain", "BrandMigrationCursor",
        "ProviderAdapterRelease", "AdapterContractRunProjection", "BuiltinErrorCatalog", "ProviderCompatibilityProjection",
        "PlatformPlanCatalog", "PlatformInvoice", "BillingDomainReconciliation", "PlatformDashboardProjection",
        "AccountPreference", "GaGate", "GaEvidence", "GaDecision", "GaLaunch", "GaRollback", "PostGaReview",
    }
    for item in PRDS:
        for name in item["objects"]:
            domain = (
                "平台全局/Identity abuse domain"
                if name == "AbuseCounter"
                else "平台全局"
                if name in global_objects or name.startswith("Platform")
                else "Workspace/租户"
            )
            tenant_rule = (
                "全局对象不带tenant_id；通过受控关联引用Workspace"
                if domain.startswith("平台全局")
                else "必须带workspace_id/tenant_id并以复合外键绑定owner"
            )
            constraint = "版本化/状态机/CAS；删除或停用保留审计事实；公开DTO使用字段白名单"
            ws.append((item["id"], name, domain, tenant_rule, constraint))
    style_table(ws, {1: 12, 2: 32, 3: 18, 4: 52, 5: 62})


def add_api_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("04_API与事件")
    ws.append(("PRD_ID", "类型", "接口/事件", "调用主体", "授权与隔离", "隐私/幂等要求"))
    for item in PRDS:
        for endpoint in item["apis"]:
            subject = "公开/账号主体" if "/identity/" in endpoint else "平台、Workspace或Edge设备主体"
            ws.append((item["id"], "API/动作", endpoint, subject, item["permissions"], "统一错误码/request_id；拒绝未知字段；写操作幂等；敏感字段白名单"))
        for event in item["events"]:
            ws.append((item["id"], "事件/Webhook", event, "持久source/outbox", "事件绑定平台域或tenant/workspace owner", "exact schema；不含Key/Token/Prompt/Response；可恢复投递"))
    style_table(ws, {1: 12, 2: 15, 3: 50, 4: 24, 5: 62, 6: 62})


def add_permissions_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("05_角色权限矩阵")
    ws.append(("角色", "角色域", "定位", "平台IAM", "租户目录", "租户业务内容", "订阅账单", "Fleet/Edge", "审计安全"))
    for row in PERMISSION_ROWS:
        ws.append(row)
    style_table(ws, {1: 22, 2: 12, 3: 40, 4: 20, 5: 20, 6: 28, 7: 22, 8: 24, 9: 24})
    ws.insert_rows(1)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=9)
    ws["A1"] = "硬边界：平台角色不自动成为任何Workspace成员；租户角色不能访问/platform；支持访问必须JIT、客户授权、双审批、限时且字段裁剪。"
    ws["A1"].fill = P0_FILL
    ws["A1"].font = Font(bold=True, color="7F0000")
    ws["A1"].alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 42
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:I{ws.max_row}"


def add_role_pages_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("10_角色页面矩阵")
    ws.append(("角色", "角色域", "允许导航/页面", "关键动作", "明确禁止", "分配主体"))
    for row in ROLE_PAGE_ROWS:
        ws.append(row)
    style_table(ws, {1: 24, 2: 12, 3: 68, 4: 55, 5: 58, 6: 48})
    for row in ws.iter_rows(min_row=2):
        row[0].font = Font(bold=True, color="16324F")
        ws.row_dimensions[row[0].row].height = 92


def add_navigation_disposition_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("11_现有导航处置")
    ws.append((
        "现有分组", "navigation_id", "现有页面", "处置", "目标产品域/入口", "企业可见角色",
        "平台可见角色", "现有API/数据/Scheduler证据", "承接PRD", "切换验收边界",
    ))
    for row in NAVIGATION_DISPOSITION_ROWS:
        ws.append(row)
    style_table(ws, {1: 16, 2: 24, 3: 25, 4: 22, 5: 38, 6: 50, 7: 50, 8: 70, 9: 28, 10: 72})
    for row in ws.iter_rows(min_row=2):
        row[1].font = Font(bold=True, color="16324F")
        ws.row_dimensions[row[0].row].height = 108


def add_test_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("06_测试与验收计划")
    headers = ("PRD_ID", "WB白盒", "API接口", "BB黑盒", "GR灰盒", "PERF性能压力", "WEB自动化", "UI元素", "UCD易用性", "COMP兼容", "SEC安全", "FLOW全流程")
    ws.append(headers)
    for item in PRDS:
        identity = item["id"]
        ws.append((
            identity,
            f"对象owner、状态机、CAS、复合外键、事务/outbox；专项：{item['tests']}",
            "成功/失败/匿名/越权/未知字段/幂等/分页；统一code、request_id、no-store",
            "只通过公开入口验证角色、租户、状态和可见结果，不依赖实现细节",
            "注入DB/Redis/KMS/网络/邮件/IdP/Edge故障，验证fail-open/closed与恢复水位",
            f"验证目标：{item['slo']}；含基线、并发、压力、长稳、资源与噪声邻居",
            "Chromium/Edge/Firefox真实浏览器，网络/Console/Page error为0，迟到响应与注销清理",
            "390/1280/1440、DPR 1/1.25/1.5、键盘/焦点/读屏、无重叠/裁剪/横向溢出",
            "真人鼠标完成主要任务；文案不出现研发术语；下一步明确；失败可恢复；已完成可查看",
            "Windows/Linux、SQLite/PostgreSQL、代理/断网、版本N/N-1、升级/回滚和数据迁移",
            f"跨租户/跨域/动态撤权/重放/SSRF/注入/敏感日志；专项：{item['security']}",
            "从注册/登录到业务终态、用量成本、审计事件、退出清理；真实依赖与静止终态复核",
        ))
    style_table(ws, {1: 12, 2: 52, 3: 48, 4: 45, 5: 50, 6: 48, 7: 48, 8: 48, 9: 48, 10: 50, 11: 58, 12: 55})
    for row in ws.iter_rows(min_row=2):
        ws.row_dimensions[row[0].row].height = 105


def add_legacy_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("07_旧计划废弃映射")
    ws.append(("PRD_ID", "旧需求名称", "旧计划状态", "新需求名称", "替换原因", "执行规则"))
    for item, old_title in zip(PRDS[: len(LEGACY_TITLES)], LEGACY_TITLES, strict=True):
        ws.append((item["id"], old_title, "已废弃-禁止开发", item["title"], "产品路线切换为SaaS总控、个人/企业自助注册和云管私有Gateway", "仅保留历史审计；不得实现旧范围；以01_PRD总表为唯一依据"))
    style_table(ws, {1: 12, 2: 38, 3: 20, 4: 45, 5: 65, 6: 65})
    for row in ws.iter_rows(min_row=2):
        row[2].fill = P0_FILL
        row[2].font = Font(bold=True, color="9C0006")


def add_gate_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("08_里程碑门禁")
    ws.append(("门禁", "适用PRD", "名称", "必须满足的退出条件", "阻断规则"))
    for gate, prds, title, criteria in GATES:
        ws.append((gate, prds, title, criteria, "任一必需证据缺失、跳过、失败或含敏感明文即阻断后续阶段"))
    style_table(ws, {1: 10, 2: 20, 3: 28, 4: 90, 5: 62})


def add_risk_sheet(wb: Workbook) -> None:
    ws = wb.create_sheet("09_风险与决策")
    ws.append(("风险ID", "级别", "风险", "关联PRD", "预防/缓解", "责任角色", "当前状态"))
    for risk_id, severity, risk, prds, mitigation, owner in RISKS:
        ws.append((risk_id, severity, risk, prds, mitigation, owner, "开放-随对应PRD关闭"))
    style_table(ws, {1: 11, 2: 9, 3: 52, 4: 24, 5: 75, 6: 24, 7: 24})
    for row in ws.iter_rows(min_row=2):
        row[1].fill = P0_FILL if row[1].value == "P0" else P1_FILL


def verify_workbook(path: Path) -> None:
    expected_ids = [f"PRD-{number:03d}" for number in range(FIRST_PRD, FIRST_PRD + len(PRDS))]
    assert [item["id"] for item in PRDS] == expected_ids
    assert len({item["title"] for item in PRDS}) == len(PRDS)
    for item in PRDS:
        current = int(item["id"].removeprefix("PRD-"))
        dependencies = [int(value) for value in re.findall(r"(?<!\d)(\d{3})(?!\d)", item["dependencies"])]
        assert all(dependency < current for dependency in dependencies)

    workbook = load_workbook(path, read_only=True, data_only=False)
    expected = {
        "00_变更说明", "01_PRD总表", "02_阶段路线图", "03_核心数据对象", "04_API与事件",
        "05_角色权限矩阵", "06_测试与验收计划", "07_旧计划废弃映射", "08_里程碑门禁",
        "09_风险与决策", "10_角色页面矩阵", "11_现有导航处置",
    }
    assert set(workbook.sheetnames) == expected
    rows = list(workbook["01_PRD总表"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == len(PRDS)
    assert rows[0][0] == f"PRD-{FIRST_PRD:03d}"
    assert rows[-1][0] == f"PRD-{CURRENT_PLANNED_LAST_PRD:03d}"
    assert [row[0] for row in rows] == expected_ids
    assert [row[1] for row in rows] == [item["status"] for item in PRDS]
    navigation_rows = list(workbook["11_现有导航处置"].iter_rows(min_row=2, values_only=True))
    assert len(navigation_rows) == 64
    assert len({row[1] for row in navigation_rows}) == 64
    legacy = list(workbook["07_旧计划废弃映射"].iter_rows(min_row=2, values_only=True))
    assert len(legacy) == 19 and all(row[2] == "已废弃-禁止开发" for row in legacy)
    assert "all_108_prds_complete=false" in workbook["00_变更说明"]["B5"].value
    assert "saas_incremental_roadmap_complete=false" in workbook["00_变更说明"]["B5"].value


def main() -> None:
    workbook = Workbook()
    add_readme_sheet(workbook)
    add_prd_sheet(workbook)
    add_roadmap_sheet(workbook)
    add_objects_sheet(workbook)
    add_api_sheet(workbook)
    add_permissions_sheet(workbook)
    add_test_sheet(workbook)
    add_legacy_sheet(workbook)
    add_gate_sheet(workbook)
    add_risk_sheet(workbook)
    add_role_pages_sheet(workbook)
    add_navigation_disposition_sheet(workbook)
    workbook.properties.title = f"MetaHub SaaS增量开发计划 PRD-{FIRST_PRD:03d}起"
    workbook.properties.subject = "废弃旧PRD-090..108，建设可扩展SaaS总控与云管私有Gateway"
    workbook.properties.creator = "MetaHub"
    workbook.properties.description = (
        "旧PRD-090..108已废弃；本工作簿为PRD-090起唯一开发依据；"
        f"当前已细化至PRD-{CURRENT_PLANNED_LAST_PRD:03d}，后续编号开放追加。"
    )
    workbook.save(OUTPUT)
    verify_workbook(OUTPUT)
    print(f"generated={OUTPUT.name}")
    print(f"prd_count={len(PRDS)}")
    print("status=verified")


if __name__ == "__main__":
    main()
