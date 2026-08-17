/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/src/features/developer-start/renderDeveloperStart.js
 */

import {
  renderApiDocsPanel,
  renderErrorPagePreview,
} from "../observability/renderObservabilityPanels.js";
import { renderBudgetPanel } from "../budgets/renderBudgetPanel.js";
import { renderAuditLogPanel } from "../audit-logs/renderAuditLogPanel.js";
import { renderRoleDashboardPanel } from "../dashboard/renderRoleDashboardPanel.js";
import { renderDebugConsolePanel } from "../debug-console/renderDebugConsolePanel.js";
import { renderEnterpriseChat } from "../enterprise-chat/renderEnterpriseChat.js";
import { renderPromptTemplates } from "../prompt-templates/renderPromptTemplates.js";
import { renderDlpPanel } from "../dlp/renderDlpPanel.js";
import { renderIntentSecurity } from "../intent-security/renderIntentSecurity.js";
import { renderSessionRisks } from "../session-risks/renderSessionRisks.js";
import { renderFiles } from "../files/renderFiles.js";
import { renderFileSecurity } from "../file-security/renderFileSecurity.js";
import { renderBatchJobs } from "../batch-jobs/renderBatchJobs.js";
import { renderMcpTools } from "../mcp-tools/renderMcpTools.js";
import { renderLocalEndpoints } from "../local-models/renderLocalEndpoints.js";
import { renderExperiments } from "../experiments/renderExperiments.js";
import { renderResponseCache } from "../cache-policies/renderResponseCache.js";
import {
  renderPlanManagement,
  renderQuotaManagement,
} from "../plan-billing/renderPlanBilling.js";
import { renderBillingDisputes } from "../billing-disputes/renderBillingDisputes.js";
import { renderAdminAssistant } from "../admin-assistant/renderAdminAssistant.js";
import { renderApprovals } from "../approvals/renderApprovals.js";
import { renderEnvironmentPanel } from "../environments/renderEnvironmentPanel.js";
import { renderErrorMapPanel } from "../error-maps/renderErrorMapPanel.js";
import { renderModelAliasPanel } from "../model-aliases/renderModelAliasPanel.js";
import { renderRoutingPolicyPanel } from "../routing-policies/renderRoutingPolicyPanel.js";
import { renderModelQualityScoresPanel } from "../model-quality-scores/renderModelQualityScoresPanel.js";
import { renderModelCatalogPanel } from "../models/renderModelCatalogPanel.js";
import { renderModelPricePanel } from "../model-prices/renderModelPricePanel.js";
import { renderPoliciesPanel } from "../policies/renderPoliciesPanel.js";
import { renderPolicyDryRunPanel } from "../policy-dry-run/renderPolicyDryRunPanel.js";
import { renderPolicySnapshotsPanel } from "../policy-snapshots/renderPolicySnapshotsPanel.js";
import { renderPolicyTemplates as renderPolicyTemplateLibrary } from "../policy-templates/renderPolicyTemplates.js";
import { renderProviderPanel } from "../providers/renderProviderPanel.js";
import { renderProviderStatusPanel } from "../provider-health/renderProviderStatusPanel.js";
import { renderAdapterSpecs } from "../adapter-specs/renderAdapterSpecs.js";
import { renderAdapterContractRuns } from "../adapter-contract-runs/renderAdapterContractRuns.js";
import { renderProjectApplicationPanel } from "../projects/renderProjectApplicationPanel.js";
import { renderLanguageSettingsPanel } from "../language-settings/renderLanguageSettingsPanel.js";
import { renderRequestLogPanel } from "../request-logs/renderRequestLogPanel.js";
import { renderOutputSafety } from "../output-safety/renderOutputSafety.js";
import { renderRequestExplanationPanel } from "../request-explanations/renderRequestExplanation.js";
import { renderRequestFeedbackSummaryPanel } from "../request-feedback/renderRequestFeedback.js";
import { renderProductAnalytics } from "../product-analytics/renderProductAnalytics.js";
import { renderCustomerHealth } from "../customer-health/renderCustomerHealth.js";
import { renderSystemStatusPanel } from "../system-status/renderSystemStatusPanel.js";
import { renderTenantPanel } from "../tenant/renderTenantPanel.js";
import { renderUsagePanel } from "../usage/renderUsagePanel.js";
import { renderFinOpsLedger } from "../finops-ledger/renderFinOpsLedger.js";
import { renderCostAnomalies } from "../cost-anomalies/renderCostAnomalies.js";
import { renderUserManagementPanel } from "../users/renderUserManagementPanel.js";
import { renderVirtualKeyPanel } from "../virtual-keys/renderVirtualKeyPanel.js";
import { renderWebhookPanel } from "../webhooks/renderWebhookPanel.js";
import { renderWebhookDeadLetters } from "../webhook-dead-letters/renderWebhookDeadLetters.js";
import {
  activeNavigationItem,
  navigationItemsFromState,
  renderConsoleShell,
} from "../console-shell/renderConsoleShell.js";
import { renderDeveloperQuickstart } from "../developer-quickstart/renderDeveloperQuickstart.js";
import { renderSdkExamples } from "../python-sdk-examples/renderPythonSdkExamples.js";
import { buildMigrationGuide, renderMigrationGuide } from "../migration-guide/renderMigrationGuide.js";
import {
  buildCompetitorComparison,
  renderCompetitorComparison,
} from "../competitor-comparison/renderCompetitorComparison.js";
import { renderRolePermissionMatrix } from "../role-permissions/renderRolePermissionMatrix.js";
import { hasEveryPermission, hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 开发者首页默认状态用于接口加载前的安全占位，避免把未知配置误展示为已完成。
const DEFAULT_STATUSES = [
  { name: "Gateway Data Plane", kind: "gateway", state: "checking", requestId: "-" },
  { name: "Control Plane API", kind: "control", state: "checking", requestId: "-" },
  { name: "Console Web", kind: "console", state: "ok", requestId: "local" },
];

// 状态标签把后端 onboarding 状态映射为中文展示，不参与真实配置完成度计算。
const STATE_LABELS = {
  ok: "运行中",
  checking: "检查中",
  error: "不可用",
};

/**
 * 判断异步导航操作是否仍属于当前刷新代次和登录 Session。
 *
 * @param {{ generation: number, token: string } | null} owner 导航发起时冻结的代次与 Token。
 * @param {{ generation: number, token: string | null }} current 当前全局代次与 Token 快照。
 * @returns {boolean} 两个维度均严格相同时返回 true，否则返回 false。
 *
 * 安全约束：Token 只做内存等值比较，禁止写入 DOM、日志或错误消息。代次阻止旧导航覆盖
 * 新页面，Token 维度则阻止登出或换 Session 后的迟到 401 清除新登录态。
 */
export function isConsoleNavigationOwnerCurrent(owner, current) {
  return Boolean(
    owner
    && current
    && Number.isSafeInteger(owner.generation)
    && owner.generation >= 1
    && typeof owner.token === "string"
    && owner.token.length > 0
    && Number.isSafeInteger(current.generation)
    && owner.generation === current.generation
    && owner.token === current.token
  );
}

/**
 * 渲染 开发者首页 的 `renderDeveloperStart` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
export function renderDeveloperStart(container, options) {
  if (!container) {
    return;
  }

  const statuses = options.statuses ?? DEFAULT_STATUSES;
  const navigationItems = navigationItemsFromState(options.consoleShellState);
  const activeItem = activeNavigationItem(options.consoleShellState, navigationItems);
  const hasAuthorizedNavigation = Boolean(
    options.consoleShellState?.state !== "error"
    && !options.consoleShellState?.error
    && Array.isArray(options.consoleShellState?.navigation?.navigation_items)
    && options.consoleShellState.navigation.navigation_items.length > 0,
  );
  const contentHtml = hasAuthorizedNavigation
    ? renderActiveContent(activeItem.id, options, statuses)
    : renderAuthorizationUnavailable();

  // PRD-018 起由统一 shell 承载导航和主内容区；业务面板仍保持原有渲染函数，降低权限与表单回归面。
  container.innerHTML = renderConsoleShell({
    authContext: options.authContext,
    consoleShellState: options.consoleShellState,
    contentHtml,
    i18n: options.i18n,
  });
}

/**
 * 在 PAGE-072 首轮数据读取期间渲染真实 loading shell，并返回重新绑定事件所需的状态投影。
 *
 * 安全边界：只有当前身份为 admin/developer，且后端导航同时授权
 * `id=routing-policies` 与 `target_section=routing-policies` 时才允许投影。该函数不会修改
 * 原始 shell state；校验失败时保持现有 DOM 不变，避免前端伪造入口生成受限页面节点。
 */
export function renderRoutingPoliciesLoading(container, options) {
  const navigationItems = options?.consoleShellState?.navigation?.navigation_items;
  const authorized = Array.isArray(navigationItems)
    && navigationItems.some((item) => (
      item?.id === "routing-policies"
      && item?.target_section === "routing-policies"
    ));
  // 任一权限事实不足都保留原页面，既不泄露 PAGE-072 DOM，也不把合法页面误清空成空白。
  if (!container || !hasPermission(options?.authContext, PERMISSIONS.ROUTING_READ) || !authorized) {
    return null;
  }

  // 复制两层状态后再切换 active item，防止 loading 瞬态污染调用方持有的后端导航快照。
  const projectedState = {
    ...options.consoleShellState,
    navigation: {
      ...options.consoleShellState.navigation,
      active_navigation_item_id: "routing-policies",
    },
    activeNavigationItemId: "routing-policies",
  };
  renderDeveloperStart(container, {
    ...options,
    consoleShellState: projectedState,
    // null 是路由策略渲染器既有的首轮读取状态，不伪造 Alias、版本或配置行。
    routingPoliciesState: null,
  });
  return projectedState;
}

/**
 * 渲染导航授权不可用时的失败关闭占位。
 *
 * 导航是业务面板可见性的后端授权事实。缺失、空集合或错误状态下不能回退渲染历史全量页面，
 * 否则会在权限校验失败时把其他租户治理入口和缓存数据重新放入 DOM。
 */
function renderAuthorizationUnavailable() {
  return `
    <section class="empty-state error-state authorization-unavailable" aria-labelledby="authorization-unavailable-title" role="alert">
      <h2 id="authorization-unavailable-title">授权信息不可用</h2>
      <p>暂时无法验证当前会话的导航权限，请刷新后重试。</p>
    </section>
  `;
}

/**
 * 在 PAGE-088 首轮状态读取期间渲染已授权 loading shell。
 * 后端导航和双读取权限任一缺失都保持原 DOM；loading 不依赖 `/status/providers` 成功，访问事件才能绑定页面本身。
 */
export function renderProviderStatusLoading(container, options) {
  const navigationItems = options?.consoleShellState?.navigation?.navigation_items;
  const authorized = Array.isArray(navigationItems)
    && navigationItems.some((item) => (
      item?.id === "provider-health"
      && item?.target_section === "provider-health"
      && item?.visible !== false
      && item?.disabled !== true
    ));
  // 页面会读取调用级故障，必须同时拥有健康与调用日志权限；前端投影不能用旧角色名补权。
  if (!container || !authorized || !hasEveryPermission(options?.authContext, [
    PERMISSIONS.PROVIDER_HEALTH_READ,
    PERMISSIONS.REQUEST_LOG_READ,
  ])) {
    return null;
  }
  const projectedState = {
    ...options.consoleShellState,
    navigation: {
      ...options.consoleShellState.navigation,
      active_navigation_item_id: "provider-health",
    },
    activeNavigationItemId: "provider-health",
  };
  renderDeveloperStart(container, {
    ...options,
    consoleShellState: projectedState,
    providerHealthState: null,
  });
  return projectedState;
}

/**
 * 在 PAGE-090 首次读取期间渲染后端导航授权的 loading shell。
 *
 * 只有实时权限快照和精确 `adapter-specs` 导航同时成立才投影页面；该函数不修改原始导航对象，
 * 失败时保留现有 DOM，避免手写 hash 或旧角色文本制造受限页面节点。
 */
export function renderAdapterSpecsLoading(container, options) {
  const navigationItems = options?.consoleShellState?.navigation?.navigation_items;
  const authorized = Array.isArray(navigationItems)
    && navigationItems.some((item) => (
      item?.id === "adapter-specs"
      && item?.target_section === "adapter-specs"
      && item?.visible !== false
      && item?.disabled !== true
    ));
  // 导航 owner、容器或实时权限任一缺失都保持原 DOM，不允许手写 hash 生成受限页面。
  if (!container || !authorized || !hasPermission(
    options?.authContext,
    PERMISSIONS.PROVIDER_HEALTH_READ,
  )) {
    return null;
  }
  const projectedState = {
    ...options.consoleShellState,
    navigation: {
      ...options.consoleShellState.navigation,
      active_navigation_item_id: "adapter-specs",
    },
    activeNavigationItemId: "adapter-specs",
  };
  renderDeveloperStart(container, {
    ...options,
    consoleShellState: projectedState,
    adapterSpecsState: null,
  });
  return projectedState;
}

/**
 * 在 PAGE-091 首次读取期间投影已授权 loading shell。
 *
 * 后端导航必须精确声明 adapter-contract-runs，当前认证快照也必须包含 provider.health.read；
 * 任一条件缺失都保留原 DOM，防止 viewer 或手写 hash 生成页面节点并触发预取。
 */
export function renderAdapterContractRunsLoading(container, options) {
  const navigationItems = options?.consoleShellState?.navigation?.navigation_items;
  const authorized = Array.isArray(navigationItems)
    && navigationItems.some((item) => (
      item?.id === "adapter-contract-runs"
      && item?.target_section === "adapter-contract-runs"
      && item?.visible !== false
      && item?.disabled !== true
    ));
  // loading 也属于受限页面 DOM，不能在权限或导航事实不完整时宽松投影。
  if (!container || !authorized || !hasPermission(
    options?.authContext,
    PERMISSIONS.PROVIDER_HEALTH_READ,
  )) {
    return null;
  }
  const projectedState = {
    ...options.consoleShellState,
    navigation: {
      ...options.consoleShellState.navigation,
      active_navigation_item_id: "adapter-contract-runs",
    },
    activeNavigationItemId: "adapter-contract-runs",
  };
  renderDeveloperStart(container, {
    ...options,
    consoleShellState: projectedState,
    adapterContractRunsState: null,
  });
  return projectedState;
}

/**
 * 根据当前导航项渲染主内容区。
 * 关键边界：这里只做前端内容调度，任何真实读写权限仍由对应后端业务 API 校验。
 */
function renderActiveContent(activeItemId, options, statuses) {
  switch (activeItemId) {
    case "tenant":
      // 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
      return `<section class="panel tenant-panel" aria-labelledby="tenant-title">${renderTenantPanel(options.tenantState, options.authContext)}</section>`;
    case "users":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderUsersSection(
        options.usersState,
        options.authContext,
        options.invitationState,
      );
    case "environments":
      // Environment 页面只做前端展示，创建和启停权限仍由后端 admin 依赖校验。
      return renderEnvironmentsSection(options.environmentState, options.authContext);
    case "providers":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderProvidersSection(options.providersState, options.authContext);
    case "provider-health":
      return `<section class="panel provider-health-shell" aria-labelledby="provider-health-title">${renderProviderStatusPanel(options.providerHealthState, options.authContext)}</section>`;
    case "adapter-specs":
      // PAGE-090 只消费当前租户已激活的公开 spec；真实 RBAC、激活事实与 tenant 边界仍由 Control API 强制执行。
      return `<section class="panel adapter-spec-shell" aria-labelledby="adapter-specs-title">${renderAdapterSpecs(options.adapterSpecsState, options.authContext)}</section>`;
    case "adapter-contract-runs":
      // PAGE-091 只消费不可变运行摘要；执行探测、RBAC 和 tenant owner 均不属于浏览器职责。
      return renderAdapterContractRunsSection(options.adapterContractRunsState, options.authContext);
    case "models":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderModelsSection(options.modelsState, options.providersState, options.authContext);
    case "model-prices":
      // 价格页面只做前端内容调度，金额校验、租户隔离和写权限由后端接口执行。
      return renderModelPricesSection(options.modelPricesState, options.modelsState, options.authContext);
    case "response-cache":
      // PAGE-106 只消费后端授权 owner 的安全策略摘要；真实读写与失效权限由每次 Control API 复核。
      return renderResponseCacheSection(
        options.responseCacheState,
        options.authContext,
        options.consoleShellState,
      );
    case "admin-assistant":
      // PAGE-107 只投影完整 query owner；来源链接还会与最新导航及各源域原始权限再次求交。
      return renderAdminAssistantSection(
        options.adminAssistantState,
        options.authContext,
        options.consoleShellState,
      );
    case "approvals":
      // PAGE-108/109 只投影后端 approval.read 导航 owner；行与动作仍分别叠加目标域实时权限。
      return renderApprovalsSection(
        options.approvalsState,
        options.authContext,
        options.consoleShellState,
      );
    case "model-aliases":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderModelAliasesSection(options.modelAliasesState, options.modelsState, options.authContext, options.environmentState);
    case "routing-policies":
      // 页面只复用 ModelAlias 的主备候选摘要；保存权限、租户隔离和 CAS 仍由 Control API 强制执行。
      return renderRoutingPoliciesSection(options.routingPoliciesState, options.authContext);
    case "model-quality-scores":
      // PAGE-069 只消费当前租户安全评分 DTO；读写权限、CAS 和 tenant_id 边界仍由 Control API 强制执行。
      return renderModelQualityScoresSection(
        options.modelQualityScoresState,
        options.authContext,
        options.modelQualityScoreFilters,
      );
    case "experiments":
      // PAGE-105 只展示后端权威实验与统计；浏览器不从请求日志或反馈自行重算结果。
      return renderExperimentsSection(
        options.experimentsState,
        options.authContext,
        options.consoleShellState,
      );
    case "projects":
      // Project/Application 只是业务归属视图，真实租户和写权限仍由后端接口校验。
      return renderProjectsSection(options.projectState, options.authContext);
    case "virtual-keys":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderVirtualKeysSection(options.virtualKeysState, options.authContext, options.projectState, options.environmentState);
    case "usage":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderUsageSection(options.usageState, options.authContext, options.budgetState);
    case "finops-ledger":
      // 正式账本仅消费后端 admin 导航；前端渲染不能替代 /finops 的后端权限与租户校验。
      return renderFinOpsSection(options.finOpsState, options.authContext);
    case "plans":
      // PAGE-110 只消费完整 plan.read owner；保存按钮与 PATCH 分别复核实时 plan.write。
      return renderPlanManagementSection(
        options.planManagementState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "quota-buckets":
      // PAGE-111 只消费完整 quota.read owner；调整 Dialog 与 POST 分别复核实时 quota.write。
      return renderQuotaManagementSection(
        options.quotaManagementState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "billing-disputes":
      // PAGE-112 list/detail requires billing.dispute.read; write commands are projected independently.
      return renderBillingDisputesSection(
        options.billingDisputesState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "cost-anomalies":
      // 成本异常页仅消费后端 admin 导航和 tenant-qualified DTO；真实治理动作仍由 PATCH 接口校验。
      return renderCostAnomaliesSection(options.costAnomaliesState, options.authContext);
    case "budgets":
      // 预算页面只做前端内容调度，真实读写权限由预算后端接口校验。
      return renderBudgetsSection(options.budgetState, options.authContext, options.virtualKeysState, options.projectState, options.environmentState);
    case "policies":
      // 策略页面只对后端导航授权的 admin 渲染；列表、校验和保存接口仍会再次执行 RBAC 与租户隔离。
      return renderPoliciesSection(options.policiesState, options.modelsState, options.authContext);
    case "policy-simulator":
      // 模拟器只渲染已脱敏解释；Prompt 生命周期、租户和 admin 权限分别由主应用与后端守卫负责。
      return renderPolicyDryRunSection(options.policyDryRunState, options, options.authContext);
    case "policy-snapshots":
      // 快照发布页只消费后端 admin-only 导航；发布、回滚、双 CAS 和 tenant 隔离仍由 Control API 强制执行。
      return renderPolicySnapshotsSection(options.policySnapshotsState, options.authContext);
    case "policy-templates":
      // PAGE-095 只消费当前授权 owner 的安全投影；真实复合权限、租户和双 CAS 由 Control API 逐请求复核。
      return `<div class="policy-template-shell">${renderPolicyTemplateLibrary(options.policyTemplateLibraryState)}</div>`;
    case "dlp-rules":
      // DLP 页面只做前端内容调度，规则读写、正则安全和 tenant_id 边界必须由后端校验。
      return renderDlpSection(options.dlpState, options.authContext);
    case "intent-security":
      // PAGE-096 必须同时持有后端导航与实时专用权限；正文生命周期和 POST RBAC 分别由 main 与 Control 负责。
      return renderIntentSecuritySection(
        options.intentSecurityState,
        options.authContext,
        options.consoleShellState,
      );
    case "session-risks":
      // PAGE-098 只消费后端授权导航和 strict 安全 DTO；处置权限、tenant 与状态机由 POST 再次复核。
      return renderSessionRisksSection(
        options.sessionRisksState,
        options.authContext,
        options.consoleShellState,
      );
    case "files":
      // PAGE-101/PAGE-102 只消费 strict 文件摘要；上传、tenant 与正文安全边界由 Control API 再次校验。
      return renderFilesSection(
        options.filesState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "file-security":
      // PAGE-103 只消费 strict 报告；扫描命令、报告读取、tenant 与模型准入均由后端再次校验。
      return renderFileSecuritySection(
        options.fileSecurityState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "batch-jobs":
      // PAGE-104 uses an owner-bound state; public routes still recheck RBAC and tenant ownership.
      return renderBatchJobsSection(
        options.batchJobsState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "mcp-tools":
      // PAGE-114 列表只展示安全摘要；Schema 仅由显式详情 Dialog 加载，真实 RBAC 仍由 MCP API 强制执行。
      return renderMcpToolsSection(
        options.mcpToolsState,
        options.authContext,
        options.consoleShellState,
      );
    case "local-models":
      // PAGE-116 only consumes the dedicated safe aggregate; Provider and KeyPool pages are never loaded here.
      return renderLocalEndpointsSection(
        options.localEndpointsState,
        options.authContext,
        options.consoleShellState,
      );
    case "output-safety":
      // PAGE-099 复用 RequestLog owner；真实 request_log.read 与 tenant 条件仍由每次 Control GET 强制执行。
      return renderOutputSafetySection(
        options.requestLogsState,
        options.authContext,
        options.i18n,
      );
    case "error-maps":
      // PAGE-081 只消费后端授权导航和安全规则 DTO；真实 RBAC、tenant owner 与 guard 由 Control API 强制执行。
      return renderErrorMapsSection(options.errorMapsState, options.authContext, options.errorMapFilters);
    case "webhooks":
      // Webhook 页面只做前端内容调度，Endpoint 权限、签名密钥和 tenant_id 边界必须由后端校验。
      return renderWebhooksSection(options.webhookState, options.authContext);
    case "webhook-dead-letters":
      // PAGE-083 仅消费后端授权导航和无 payload 安全 DTO；真实 RBAC、租户和状态机由 Control API 强制执行。
      return renderWebhookDeadLettersSection(options.webhookDeadLettersState, options.authContext);
    case "request-logs":
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      return renderRequestLogsSection(
        options.requestLogsState,
        options.authContext,
        options.requestFeedbackState,
        options.i18n,
      );
    case "request-explanations":
      // PAGE-079 只渲染后端单次 GET 返回的严格白名单；权限、租户归属和访问审计均由后端强制执行。
      return renderRequestExplanationsSection(
        options.requestExplanationState,
        options.authContext,
      );
    case "feedback-summary":
      // PAGE-071 只消费后端按当前 Session tenant 聚合的统计；前端不会从分页行重算总计或接受 viewer 直达。
      return renderRequestFeedbackSummarySection(
        options.requestFeedbackSummaryState,
        options.authContext,
        options.requestFeedbackSummaryFilters,
        options.i18n,
      );
    case "growth-analytics":
      // PAGE-117 只展示后端权威七阶段聚合；权限 capability、tenant 与漏斗不变量均在独立边界复核。
      return renderProductAnalyticsSection(
        options.productAnalyticsState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "customer-health":
      // PAGE-118 only consumes the dedicated score DTO; source-domain APIs are never composed in the browser.
      return renderCustomerHealthSection(
        options.customerHealthState,
        options.authContext,
        options.consoleShellState,
        options.i18n,
      );
    case "audit-logs":
      // 审计日志只允许 admin 进入；前端隐藏只是体验优化，后端仍必须再次执行 RBAC 和租户隔离。
      return renderAuditLogsSection(options.auditLogsState, options.authContext);
    case "role-permissions":
      // PAGE-085 只消费当前授权 owner 的矩阵快照；GET/PATCH 权限与租户边界仍由 Control API 强制执行。
      return renderRolePermissionMatrix(options.rolePermissionState, options.authContext);
    case "language-settings":
      // 语言设置页只保存当前用户偏好，真实租户和用户边界由 /user/preferences 后端接口恢复。
      return renderLanguageSettingsContent(options);
    case "quickstart":
      return renderQuickstartContent(options);
    case "sdk-examples":
      // SDK 下载页同时保留两种语言的独立状态；真正下载仍需携带登录 Token 由后端再次校验。
      return renderSdkExamples({
        pythonState: options.sdkExampleStates?.python ?? options.pythonSdkExampleState,
        javascriptState: options.sdkExampleStates?.javascript,
        activeLanguage: options.activeSdkLanguage,
      });
    case "migration-guide": {
      // viewer 不应收到后端迁移入口；这里再次按角色收口，防止测试或扩展代码绕过导航直接渲染。
      const guide = buildMigrationGuide({
        consoleShellState: options.consoleShellState,
        environmentState: options.environmentState,
        modelAliasesState: options.modelAliasesState,
        selectedEnvironmentId: options.selectedMigrationEnvironmentId,
        selectedModelAlias: options.selectedMigrationModelAlias,
      });
      return renderMigrationGuide({
        guide,
        authContext: options.authContext,
        activeLanguage: options.activeMigrationLanguage,
        eventWarning: options.migrationGuideEventWarning,
      });
    }
    case "competitor-comparison": {
      // 竞品页只把后端已授权导航转换为可点击映射，静态帮助内容本身不承担权限判定。
      const comparison = buildCompetitorComparison({
        consoleShellState: options.consoleShellState,
      });
      return renderCompetitorComparison({
        comparison,
        authContext: options.authContext,
        activeTab: options.activeCompetitorTab,
        eventWarning: options.competitorEventWarning,
      });
    }
    case "api-docs":
      return renderApiDocsContent(options);
    case "debug-console":
      return renderDebugConsoleContent(options);
    case "enterprise-chat":
      // PAGE-093 只渲染独立内存状态；真实 chat.use、tenant 和 Gateway 治理均由后端逐请求执行。
      return renderEnterpriseChatContent(options);
    case "prompt-templates":
      // PAGE-094 只消费当前授权 owner 的模板投影；读写权限、tenant 和 CAS 均由后端逐请求执行。
      return renderPromptTemplatesContent(options);
    case "system-status":
      return `<section class="panel system-status-panel" aria-labelledby="system-status-title">${renderSystemStatusPanel(options.systemHealth)}</section>`;
    case "config-summary":
      return `<section class="panel config-panel" aria-labelledby="config-title">${renderConfigSummary(options.configSummary)}</section>`;
    case "overview":
    default:
      return renderOverviewContent(options, statuses);
  }
}

/**
 * 渲染 PAGE-092 角色首页。
 * 后端 role-view 响应同时给出视图目录与六类聚合卡片，旧 overview 和 Provider Health 预取不能在这里回退绕权。
 */
function renderOverviewContent(options) {
  return `
    <section class="panel dashboard-overview-shell" aria-labelledby="role-dashboard-title">
      ${renderRoleDashboardPanel(options.roleDashboardState, options.authContext, options.consoleShellState)}
    </section>
  `;
}

/**
 * 渲染开发者快速开始导航页。
 * 边界：页面只展示后端生成的安全占位示例，不接收或持久化 Virtual Key 明文。
 */
function renderQuickstartContent(options) {
  return renderDeveloperQuickstart(options.quickstartState);
}

/**
 * 渲染语言设置导航页，所有可见文本由 i18n 字典提供。
 * 边界：该页不接受 tenant_id/user_id 输入，保存时只能调用当前登录用户偏好接口。
 */
function renderLanguageSettingsContent(options) {
  return `
    <section class="panel language-settings-shell" aria-labelledby="language-settings-title">
      ${renderLanguageSettingsPanel(options.userPreferencesState, options.i18n)}
    </section>
  `;
}

/**
 * 渲染 API 文档导航页，并保留统一错误响应示例。
 */
function renderApiDocsContent(options) {
  return `
    <section class="panel api-docs-panel" aria-labelledby="api-docs-title">
      ${renderApiDocsPanel({ gatewayUrl: options.gatewayUrl })}
    </section>
    <section class="panel error-page-panel" aria-labelledby="error-page-title">
      ${renderErrorPagePreview({
        code: "infra.not_found",
        message: "Requested resource was not found.",
        requestId: "req_example",
        traceId: "trc_example",
        suggestion: "Check the API path and method.",
      })}
    </section>
  `;
}

/**
 * 渲染 Gateway 调试台导航页。
 * 边界：调试台只接受用户临时输入的 Virtual Key，不把明文 Key 写入页面状态、偏好或事件 payload。
 */
function renderDebugConsoleContent(options) {
  return `
    <section class="panel gateway-debug-panel" aria-labelledby="gateway-debug-title">
      ${renderDebugConsolePanel(options.gatewayDebugState ?? {}, {
        virtualKeysState: options.virtualKeysState,
        modelAliasesState: options.modelAliasesState,
        environmentState: options.environmentState,
      })}
    </section>
  `;
}

/**
 * 渲染 开发者首页 的 `renderUsersSection` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderUsersSection(usersState, authContext, invitationState) {
  const html = renderUserManagementPanel(usersState, authContext, invitationState);
  return html
    ? `
      <section class="panel users-panel" aria-labelledby="users-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 开发者首页 的 `renderVirtualKeysSection` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderVirtualKeysSection(virtualKeysState, authContext, projectState, environmentState) {
  const html = renderVirtualKeyPanel(virtualKeysState, authContext, projectState, environmentState);
  return html
    ? `
      <section class="panel virtual-keys-panel" aria-labelledby="virtual-keys-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染模型价格时间线区域，admin 可新增计划，developer 仅查看。
 */
function renderModelPricesSection(modelPricesState, modelsState, authContext) {
  const html = renderModelPricePanel(modelPricesState, modelsState, authContext);
  return html
    ? `<section class="panel model-prices-panel" aria-labelledby="model-prices-title">${html}</section>`
    : "";
}

/**
 * 渲染租户级 Environment 设置区域，developer 只读、admin 可写。
 */
function renderEnvironmentsSection(environmentState, authContext) {
  const html = renderEnvironmentPanel(environmentState, authContext);
  return html
    ? `<section class="panel environments-panel" aria-labelledby="environments-title">${html}</section>`
    : "";
}

/**
 * 渲染项目与应用归属区域。
 * 前端只把当前后端返回的归属元数据交给面板，真实读写权限仍由 Project/Application API 校验。
 */
function renderProjectsSection(projectState, authContext) {
  const html = renderProjectApplicationPanel(projectState, authContext);
  return html
    ? `
      <section class="panel projects-panel" aria-labelledby="projects-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 开发者首页 的 `renderProvidersSection` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderProvidersSection(providersState, authContext) {
  const html = renderProviderPanel(providersState, authContext);
  return html
    ? `
      <section class="panel providers-panel" aria-labelledby="providers-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 开发者首页 的 `renderModelsSection` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderModelsSection(modelsState, providersState, authContext) {
  const html = renderModelCatalogPanel(modelsState, providersState, authContext);
  return html
    ? `
      <section class="panel models-panel" aria-labelledby="models-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 开发者首页 的 `renderModelAliasesSection` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderModelAliasesSection(modelAliasesState, modelsState, authContext, environmentState) {
  const html = renderModelAliasPanel(modelAliasesState, modelsState, authContext, environmentState);
  return html
    ? `
      <section class="panel model-aliases-panel" aria-labelledby="model-aliases-title">
        ${html}
      </section>
    `
    : "";
}


/**
 * 渲染用量与成本区域。
 * 前端按角色隐藏该区域只是体验优化，后端 /usage 仍会按管理员/开发者权限校验。
 */
function renderUsageSection(usageState, authContext, budgetState) {
  const html = renderUsagePanel(usageState, authContext, budgetState);
  return html
    ? `
      <section class="panel usage-panel" aria-labelledby="usage-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-074/075 FinOps 正式账本工作区。
 * 前端仅对 admin 输出 DOM；查询、调整、冲正和 tenant_id 边界仍由 Control API 强制执行。
 */
function renderFinOpsSection(finOpsState, authContext) {
  const html = renderFinOpsLedger(finOpsState, authContext);
  return html
    ? `
      <section class="panel finops-ledger-panel" aria-labelledby="finops-ledger-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-077/078 成本异常工作区。
 * 前端只对 admin 输出安全 DTO；租户隔离、状态迁移和限流有效性均由 Control API 再次校验。
 */
function renderCostAnomaliesSection(costAnomaliesState, authContext) {
  const html = renderCostAnomalies(costAnomaliesState, authContext);
  return html
    ? `
      <section class="panel cost-anomalies-panel" aria-labelledby="cost-anomaly-title">
        ${html}
      </section>
    `
    : "";
}
/**
 * 渲染预算中心区域。
 * 前端按角色隐藏预算设置入口只是体验优化，预算写入权限仍由控制面后端强制校验。
 */
function renderBudgetsSection(budgetState, authContext, virtualKeysState, projectState, environmentState) {
  const html = renderBudgetPanel(budgetState, authContext, virtualKeysState, projectState, environmentState);
  return html
    ? `
      <section class="panel budgets-panel" aria-labelledby="budgets-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 DLP 规则区域。
 * 前端只负责把 admin 入口放进控制台；后端 /dlp/rules 仍要强制校验角色、租户和正则安全边界。
 */
function renderDlpSection(dlpState, authContext) {
  const html = renderDlpPanel(dlpState, authContext);
  return html
    ? `
      <section class="panel dlp-rules-panel" aria-labelledby="dlp-rules-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-096 单次敏感意图分类工作台。
 * renderer 会再次核对后端导航 owner；失败时返回零 DOM，不能由 active ID 或角色名单独开放页面。
 */
function renderIntentSecuritySection(state, authContext, consoleShellState) {
  const html = renderIntentSecurity(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel intent-security-panel" aria-labelledby="intent-security-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-098 高危会话工作区。
 * renderer 会再次核对后端导航、实时 read 权限和双权限动作门禁；失败时返回零 DOM。
 */
function renderSessionRisksSection(state, authContext, consoleShellState) {
  const html = renderSessionRisks(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel session-risks-panel" aria-labelledby="session-risk-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-101/PAGE-102 文件处理工作台。
 * renderer 会再次核对后端导航和实时 file.read/file.write 投影；失败时返回零 DOM。
 */
function renderFilesSection(state, authContext, consoleShellState, i18n) {
  const html = renderFiles(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel files-panel" aria-labelledby="files-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-103 文件安全报告工作区。
 * renderer 会再次核对后端 file-security 导航、实时 file.read 与独立扫描权限；失败时返回零 DOM。
 */
function renderFileSecuritySection(state, authContext, consoleShellState, i18n) {
  const html = renderFileSecurity(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel file-security-panel" aria-labelledby="file-security-title">
        ${html}
      </section>
    `
    : "";
}

/** Render PAGE-104 only when its exact backend navigation owner remains current. */
/** 渲染批任务页面并保持权限撤销后的状态清理边界。 */
function renderBatchJobsSection(state, authContext, consoleShellState, i18n) {
  const html = renderBatchJobs(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel batch-jobs-panel" aria-labelledby="batch-jobs-title">
        ${html}
      </section>
    `
    : "";
}

/** 仅在 mcp.read 导航 owner 精确匹配时渲染 PAGE-114，失权时不保留空壳 DOM。 */
function renderMcpToolsSection(state, authContext, consoleShellState) {
  const html = renderMcpTools(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel mcp-tools-panel" aria-labelledby="mcp-tools-title">
        ${html}
      </section>
    `
    : "";
}

/** Render PAGE-116 only while local_endpoint.read and its backend navigation owner remain exact. */
function renderLocalEndpointsSection(state, authContext, consoleShellState) {
  const html = renderLocalEndpoints(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel local-models-panel" aria-labelledby="local-models-title">
        ${html}
      </section>
    `
    : "";
}

/** 仅在 experiment.read 导航 owner 精确匹配时渲染 PAGE-105，失权时不保留空壳 DOM。 */
function renderExperimentsSection(state, authContext, consoleShellState) {
  const html = renderExperiments(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel experiments-panel" aria-labelledby="experiments-title">
        ${html}
      </section>
    `
    : "";
}

/** 仅在 response_cache.read 导航 owner 精确匹配时渲染 PAGE-106，失权时不保留空壳 DOM。 */
function renderResponseCacheSection(state, authContext, consoleShellState) {
  const html = renderResponseCache(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel response-cache-panel" aria-labelledby="response-cache-title">
        ${html}
      </section>
    `
    : "";
}

/** PAGE-110 失权时不保留套餐表单或上一租户余额 DOM。 */
function renderPlanManagementSection(state, authContext, consoleShellState, i18n) {
  const html = renderPlanManagement(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel plans-panel" aria-labelledby="plans-title">
        ${html}
      </section>
    `
    : "";
}

/** PAGE-111 失权时不保留额度表格、reason Dialog 或调整反馈。 */
function renderQuotaManagementSection(state, authContext, consoleShellState, i18n) {
  const html = renderQuotaManagement(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel quota-buckets-panel" aria-labelledby="quota-buckets-title">
        ${html}
      </section>
    `
    : "";
}

/** PAGE-112 loses its entire DOM when the read/navigation owner is no longer current. */
function renderBillingDisputesSection(state, authContext, consoleShellState, i18n) {
  const html = renderBillingDisputes(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel billing-disputes-panel" aria-labelledby="billing-disputes-title">
        ${html}
      </section>
    `
    : "";
}

/** PAGE-107 失权时不保留空壳 DOM；前端可见性不能替代 POST 的实时 base/source 权限复核。 */
function renderAdminAssistantSection(state, authContext, consoleShellState) {
  const html = renderAdminAssistant(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel admin-assistant-panel" aria-labelledby="admin-assistant-title">
        ${html}
      </section>
    `
    : "";
}

/** PAGE-108/109 失权时不保留空壳 DOM；任务 ID 只是选择器，不是授权凭据。 */
function renderApprovalsSection(state, authContext, consoleShellState) {
  const html = renderApprovals(state, authContext, consoleShellState);
  return html
    ? `
      <section class="panel approvals-panel" aria-labelledby="approvals-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-081 Provider 错误规则工作区。
 * 前端仅对 admin/developer 输出 DOM；租户隔离、内置只读和写权限均由后端再次校验。
 */
function renderErrorMapsSection(state, authContext, filters) {
  const html = renderErrorMapPanel(state, authContext, filters);
  return html
    ? `
      <section class="panel error-maps-panel" aria-labelledby="error-maps-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 Webhook 设置和发送记录区域。
 * 前端按角色隐藏入口只是体验优化，后端 /webhooks 仍要强制校验角色、租户、签名和发送记录边界。
 */
function renderWebhooksSection(webhookState, authContext) {
  const html = renderWebhookPanel(webhookState, authContext);
  return html
    ? `
      <section class="panel webhooks-panel" aria-labelledby="webhooks-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-083 Webhook 死信工作区。
 * viewer 即使绕过导航直接调用也不会得到 DOM；读取、重放和归档权限仍由后端独立校验。
 */
function renderWebhookDeadLettersSection(state, authContext) {
  const html = renderWebhookDeadLetters(state, authContext);
  return html
    ? `
      <section class="panel webhook-dead-letters-panel" aria-labelledby="webhook-dead-letters-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染企业 Chat 主工具。
 * 正文只来自当前页面状态机快照，不能复用 Debug Console 的 Key 选择、历史或 Gateway 直连状态。
 */
function renderEnterpriseChatContent(options) {
  const html = renderEnterpriseChat(options.enterpriseChatState, options.authContext);
  return html
    ? `<section class="enterprise-chat-shell" aria-labelledby="enterprise-chat-title">${html}</section>`
    : "";
}

/** 渲染 PAGE-094 模板管理主工具；解密正文和变量草稿由 main.js 在 DOM 就位后 hydration。 */
function renderPromptTemplatesContent(options) {
  const html = renderPromptTemplates(options.promptTemplateState, options.authContext);
  return html
    ? `<section class="prompt-templates-shell" aria-labelledby="prompt-templates-title">${html}</section>`
    : "";
}

/**
 * 渲染 PAGE-091 契约测试只读证据面。
 * viewer 即使绕过导航直接调用内容分发也得到零 DOM；后端 GET 仍会再次校验实时权限和租户。
 */
function renderAdapterContractRunsSection(state, authContext) {
  const html = renderAdapterContractRuns(state, authContext);
  return html
    ? `
      <section class="panel adapter-contract-runs-panel" aria-labelledby="adapter-contract-runs-title">
        ${html}
      </section>
    `
    : "";
}
/**
 * 渲染调用日志区域。
 * 前端会按角色隐藏该区域，但后端 /request-logs 仍会做管理员/开发者权限校验。
 */
function renderRequestLogsSection(requestLogsState, authContext, feedbackState, i18n) {
  const html = renderRequestLogPanel(requestLogsState, authContext, feedbackState, i18n);
  return html
    ? `
      <section class="panel request-logs-panel" aria-labelledby="request-logs-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-099 输出安全事件工作区。
 * 缺少实时 request_log.read 时 renderer 返回零 DOM，页面外壳不能替代后端权限校验。
 */
function renderOutputSafetySection(requestLogsState, authContext, i18n) {
  const html = renderOutputSafety(requestLogsState, authContext, i18n);
  return html
    ? `
      <section class="panel output-safety-panel" aria-labelledby="output-safety-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-079 请求解释中心。
 * 前端角色门禁只保证 viewer 零 DOM；真实读取权限、同形 404 和 tenant 条件仍以后端为权威。
 */
function renderRequestExplanationsSection(state, authContext) {
  const html = renderRequestExplanationPanel(state, authContext);
  return html
    ? `
      <section class="panel request-explanations-panel" aria-labelledby="request-explanations-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-071 反馈报表区域。
 * viewer 即使绕过后端导航直接触发内容调度也不会得到报表 DOM，真实 GET 权限仍由 Control API 校验。
 */
function renderRequestFeedbackSummarySection(summaryState, authContext, filters, i18n) {
  const html = renderRequestFeedbackSummaryPanel(summaryState, authContext, filters, i18n);
  return html
    ? `
      <section class="panel feedback-summary-panel" aria-labelledby="feedback-summary-title">
        ${html}
      </section>
    `
    : "";
}

/** 仅当实时身份与后端导航指向同一租户 owner 时渲染 PAGE-117。 */
function renderProductAnalyticsSection(state, authContext, consoleShellState, i18n) {
  const html = renderProductAnalytics(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel growth-analytics-panel" aria-labelledby="product-analytics-title">
        ${html}
      </section>
    `
    : "";
}

/** 仅在租户、读取权限和后端导航 owner 都仍有效时渲染 PAGE-118。 */
function renderCustomerHealthSection(state, authContext, consoleShellState, i18n) {
  const html = renderCustomerHealth(state, authContext, consoleShellState, i18n);
  return html
    ? `
      <section class="panel customer-health-panel" aria-labelledby="customer-health-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-067 路由策略区域。
 * admin/developer 的前端可见性来自后端导航，viewer 即使直接调用渲染器也不会得到 DOM。
 */
function renderRoutingPoliciesSection(routingPoliciesState, authContext) {
  const html = renderRoutingPolicyPanel(routingPoliciesState, authContext);
  return html
    ? `
      <section class="panel routing-policies-panel" aria-labelledby="routing-policies-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染 PAGE-069 模型评分区域。
 * admin/developer 可见性来自后端导航，viewer 即使直接调用渲染器也不会得到 DOM 或编辑表单。
 */
function renderModelQualityScoresSection(modelQualityScoresState, authContext, filters) {
  const html = renderModelQualityScoresPanel(modelQualityScoresState, authContext, filters);
  return html
    ? `
      <section class="panel model-quality-scores-panel" aria-labelledby="model-quality-scores-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染策略管理区域。
 * 外层只提供控制台 panel 布局，编辑器内存态、文本转义和 active Model 选项由专属组件负责。
 */
function renderPoliciesSection(policiesState, modelsState, authContext) {
  const html = renderPoliciesPanel(policiesState, modelsState, authContext);
  return html
    ? `
      <section class="panel policies-panel" aria-labelledby="policies-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染策略模拟器区域。
 * 上下文选择复用本轮已授权 Environment/Project/Model 数据，Prompt 永远不从 options 状态恢复。
 */
function renderPolicyDryRunSection(policyDryRunState, options, authContext) {
  const html = renderPolicyDryRunPanel(
    policyDryRunState,
    {
      environmentState: options.environmentState,
      projectState: options.projectState,
      modelsState: options.modelsState,
      modelAliasesState: options.modelAliasesState,
    },
    authContext,
  );
  return html
    ? `
      <section class="panel policy-dry-run-panel" aria-labelledby="policy-dry-run-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染策略快照发布与历史区域。
 * 外层只提供无嵌套卡片的 panel 布局，原因转义、上一版回滚和 ETag 状态由专属组件负责。
 */
function renderPolicySnapshotsSection(policySnapshotsState, authContext) {
  const html = renderPolicySnapshotsPanel(policySnapshotsState, authContext);
  return html
    ? `
      <section class="panel policy-snapshots-panel" aria-labelledby="policy-snapshots-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染管理操作审计区域。
 * 该区域只消费 GET /audit-logs 的列表响应，展开摘要不会触发详情接口或读取额外敏感数据。
 */
function renderAuditLogsSection(auditLogsState, authContext) {
  const html = renderAuditLogPanel(auditLogsState, authContext);
  return html
    ? `
      <section class="panel audit-logs-panel" aria-labelledby="audit-logs-title">
        ${html}
      </section>
    `
    : "";
}

/**
 * 渲染概览页的单个下一步条目。
 */
function renderNextStep(label, done, description) {
  return `
    <article class="next-step ${done ? "done" : "pending"}">
      <span aria-hidden="true">${done ? "✓" : "·"}</span>
      <div>
        <strong>${escapeHtml(label)}</strong>
        <p>${escapeHtml(description)}</p>
      </div>
    </article>
  `;
}

/**
 * 统计 Provider 数量，空或无权限状态视为 0。
 */
function providerCount(state) {
  return Array.isArray(state?.providers) ? state.providers.length : 0;
}

/**
 * 统计模型目录数量，空或无权限状态视为 0。
 */
function modelCount(state) {
  return Array.isArray(state?.models) ? state.models.length : 0;
}

/**
 * 统计 Virtual Key 数量，空或无权限状态视为 0。
 */
function virtualKeyCount(state) {
  return Array.isArray(state?.virtual_keys) ? state.virtual_keys.length : 0;
}

/**
 * 统计调用日志数量，空或无权限状态视为 0。
 */
function requestLogCount(state) {
  return Array.isArray(state?.request_logs) ? state.request_logs.length : 0;
}

/**
 * 渲染 开发者首页 的 `renderConfigSummary` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderConfigSummary(summary) {
  if (!summary) {
    return `
      <div class="panel-heading">
        <h2 id="config-title">系统设置 · 环境信息</h2>
        <p>正在读取控制面配置摘要。</p>
      </div>
      <div class="empty-state">等待 /internal/config/summary 响应。</div>
    `;
  }
  // 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
  if (summary.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="config-title">系统设置 · 环境信息</h2>
        <p>配置摘要暂不可用，控制台保持可用。</p>
      </div>
      <div class="empty-state error-state">${escapeHtml(summary.message)}</div>
    `;
  }

  const items = summary.items ?? [];
  const warnings = summary.warnings ?? [];
  return `
    <div class="panel-heading config-heading">
      <div>
        <h2 id="config-title">系统设置 · 环境信息</h2>
        <p>只读摘要。敏感配置仅展示已配置/已脱敏状态。</p>
      </div>
      <span class="environment-badge env-${escapeHtml(summary.environment)}">
        ${escapeHtml(summary.environment)}
      </span>
    </div>
    ${warnings.length > 0 ? `<ul class="warning-list">${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
    <div class="config-table" role="table" aria-label="运行配置摘要">
      <div class="config-row config-head" role="row">
        <span>配置项</span>
        <span>来源</span>
        <span>状态</span>
        <span>安全预览</span>
      </div>
      ${items.map(renderConfigItem).join("")}
    </div>
  `;
}

/**
 * 渲染 开发者首页 的 `renderConfigItem` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderConfigItem(item) {
  return `
    <div class="config-row" role="row">
      <span title="${escapeAttribute(item.key)}">${escapeHtml(item.key)}</span>
      <span>${escapeHtml(item.source)}</span>
      <span><mark class="status-mark status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</mark></span>
      <span title="${escapeAttribute(item.value_preview)}">${escapeHtml(item.value_preview || "-")}</span>
    </div>
  `;
}

/**
 * 渲染 开发者首页 的 `renderStatus` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderStatus(item) {
  const state = item.state ?? "checking";
  const requestId = item.requestId ?? "-";
  return `
    <article class="service-row" data-kind="${escapeHtml(item.kind)}">
      <div class="state-dot state-${escapeHtml(state)}" aria-hidden="true"></div>
      <div>
        <h3>${escapeHtml(item.name)}</h3>
        <p>${escapeHtml(STATE_LABELS[state] ?? state)} · request_id: ${escapeHtml(requestId)}</p>
      </div>
    </article>
  `;
}

/**
 * 渲染 开发者首页 的 `renderUserMenu` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderUserMenu(authContext) {
  if (!authContext?.user) {
    return `<div class="version-pill">M0-W1 / PRD-006</div>`;
  }
  const expiresAt = authContext.session?.expires_at ?? "-";
  return `
    <div class="user-menu" aria-label="个人菜单">
      <div>
        <strong>${escapeHtml(authContext.user.display_name)}</strong>
        <span>${escapeHtml(authContext.user.email)} · ${escapeHtml(authContext.user.role)}</span>
        <span>${escapeHtml(authContext.tenant?.name ?? authContext.user.tenant_id)}</span>
        <span>Session 到期：${escapeHtml(expiresAt)}</span>
      </div>
      <button id="logout-button" type="button">登出</button>
    </div>
  `;
}

/**
 * 渲染 开发者首页 的 `renderLink` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderLink(label, href, description) {
  return `
    <a class="quick-link" href="${escapeAttribute(href)}" target="_blank" rel="noreferrer">
      <strong>${escapeHtml(label)}</strong>
      <span>${escapeHtml(description)}</span>
    </a>
  `;
}

/**
 * 执行 开发者首页 的 `escapeHtml` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * 执行 开发者首页 的 `escapeAttribute` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
