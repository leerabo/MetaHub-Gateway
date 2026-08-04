/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/src/app/main.js
 */

import {
  isConsoleNavigationOwnerCurrent,
  renderAdapterContractRunsLoading,
  renderAdapterSpecsLoading,
  renderDeveloperStart,
  renderProviderStatusLoading,
  renderRoutingPoliciesLoading,
} from "../features/developer-start/renderDeveloperStart.js";
import { bindFinOpsLedgerInteractions } from "../features/finops-ledger/bindFinOpsLedger.js";
import { createFinOpsPageState } from "../features/finops-ledger/finOpsPageState.js";
import { bindCostAnomalyInteractions } from "../features/cost-anomalies/bindCostAnomalies.js";
import {
  costAnomalyIdentitySnapshot,
  createCostAnomalyPageState,
  isCostAnomalyAbortError,
} from "../features/cost-anomalies/costAnomalyPageState.js";
import { bindSessionRiskInteractions } from "../features/session-risks/bindSessionRisks.js";
import {
  createSessionRiskPageState,
  isSessionRiskAbortError,
  sessionRiskPageOwner,
  sessionRiskReadIdentitySnapshot,
} from "../features/session-risks/sessionRiskPageState.js";
import {
  bindFileInteractions,
  fileUploadHasSelection,
} from "../features/files/bindFiles.js";
import {
  createFilePageState,
  filePageOwner,
  fileReadIdentitySnapshot,
  isFileAbortError,
} from "../features/files/filePageState.js";
import { bindWebhookDeadLetterInteractions } from "../features/webhook-dead-letters/bindWebhookDeadLetters.js";
import {
  createWebhookDeadLetterPageState,
  isWebhookDeadLetterAbortError,
  webhookDeadLetterIdentitySnapshot,
} from "../features/webhook-dead-letters/webhookDeadLetterPageState.js";
import { renderWebhookDeadLetters } from "../features/webhook-dead-letters/renderWebhookDeadLetters.js";
import { renderLoginPage } from "../features/auth/renderLoginPage.js";
import {
  createEnterpriseChatPageState,
  enterpriseChatIdentitySnapshot,
} from "../features/enterprise-chat/enterpriseChatPageState.js";
import {
  hydrateEnterpriseChatText,
  renderEnterpriseChat,
} from "../features/enterprise-chat/renderEnterpriseChat.js";
import {
  createPromptTemplatePageState,
  promptTemplateReadIdentitySnapshot,
  promptTemplateWriteIdentitySnapshot,
} from "../features/prompt-templates/promptTemplatePageState.js";
import {
  clearPromptTemplateSensitiveDom,
  hydratePromptTemplateSensitiveText,
  renderPromptTemplates,
} from "../features/prompt-templates/renderPromptTemplates.js";
import {
  createPolicyTemplatePageState,
  policyTemplateApplyIdentitySnapshot,
  policyTemplateReadIdentitySnapshot,
} from "../features/policy-templates/policyTemplatePageState.js";
import { renderPolicyTemplates as renderPolicyTemplateLibrary } from "../features/policy-templates/renderPolicyTemplates.js";
import { renderGatewayDebugConsole } from "../features/observability/renderObservabilityPanels.js";
import {
  normalizeSdkLanguage,
  sdkLanguageForKey,
} from "../features/python-sdk-examples/renderPythonSdkExamples.js";
import {
  migrationLanguageForKey,
  normalizeMigrationLanguage,
} from "../features/migration-guide/renderMigrationGuide.js";
import {
  competitorTabForKey,
  normalizeCompetitorTab,
} from "../features/competitor-comparison/renderCompetitorComparison.js";
import {
  createProviderStatusPageState,
  providerStatusFilterKey,
  providerStatusIdentitySnapshot,
} from "../features/provider-health/providerStatusPageState.js";
import {
  createRoleDashboardPageState,
  roleDashboardIdentitySnapshot,
  roleDashboardNavigationOwned,
} from "../features/dashboard/roleDashboardPageState.js";
import { renderRoleDashboardPanel } from "../features/dashboard/renderRoleDashboardPanel.js";
import {
  adapterSpecApiFilters,
  adapterSpecFilterKey,
  adapterSpecIdentitySnapshot,
  canonicalAdapterSpecFilters,
  createAdapterSpecPageState,
} from "../features/adapter-specs/adapterSpecPageState.js";
import {
  adapterContractRunFilterKey,
  adapterContractRunIdentitySnapshot,
  canonicalAdapterContractRunFilters,
  createAdapterContractRunPageState,
  isAdapterContractRunAbortError,
} from "../features/adapter-contract-runs/adapterContractRunPageState.js";
import { renderAdapterContractRuns } from "../features/adapter-contract-runs/renderAdapterContractRuns.js";
import { createI18n, DEFAULT_LOCALE } from "../i18n/messages.js";
import {
  AuthApiError,
  clearAuthSession,
  getCurrentUser,
  loginAdmin,
  logoutAdmin,
  readAuthSession,
  saveAuthSession,
} from "../lib/auth.js";
import { listAdapterSpecs } from "../lib/adapterSpecs.js";
import {
  getAdapterContractRun,
  listAdapterContractRuns,
} from "../lib/adapterContractRuns.js";
import {
  authorizationIdentitySnapshot,
  authorizeNavigationSnapshot,
  getPermissions,
  hasPermission,
  PERMISSIONS,
  replaceRolePermissions,
} from "../lib/permissions.js";
import {
  createBudgetRule,
  getBudgetStatus,
  listBudgets,
  updateBudgetMode,
  updateBudgetRule,
} from "../lib/budgets.js";
import {
  createAuditExportRequestId,
  exportAuditLogs,
  listAuditLogs,
  triggerAuditCsvDownload,
} from "../lib/auditLogs.js";
import { getConfigSummary } from "../lib/configSummary.js";
import {
  bindNavigationHashRefresh,
  getConsoleNavigation,
  getConsolePreferences,
  parseNavigationHash,
  recordConsolePageView,
  resolveVisibleNavigation,
  updateConsolePreferences,
} from "../lib/consoleShell.js";
import { sendDebugChatCompletion } from "../lib/debugConsole.js";
import {
  clearEnterpriseChatSession,
  createEnterpriseChatClientMessageId,
  createEnterpriseChatSession,
  getEnterpriseChatOptions,
  sendEnterpriseChatMessage,
} from "../lib/enterpriseChat.js";
import {
  createPromptTemplate,
  getPromptTemplate,
  getPromptTemplateOptions,
  getPromptTemplateVersion,
  listPromptTemplates,
  publishPromptTemplateVersion,
  updatePromptTemplate,
} from "../lib/promptTemplates.js";
import {
  applyPolicyTemplate,
  listPolicyTemplates as listPolicyTemplateLibrary,
} from "../lib/policyTemplates.js";
import { GatewayApiError, sendChatCompletion } from "../lib/gatewayApi.js";
import { createEnvironment, listEnvironments, updateEnvironment } from "../lib/environments.js";
import { getDashboardRoleView } from "../lib/dashboard.js";
import { getDeveloperQuickstartData } from "../lib/developerQuickstart.js";
import {
  downloadJavascriptSdkExample,
  downloadPythonSdkExample,
  getJavascriptSdkExample,
  getPythonSdkExample,
} from "../lib/pythonSdkExamples.js";
import { getUserPreferences, updateUserPreferences } from "../lib/userPreferences.js";
import { createDlpRule, listDlpRules, updateDlpRule } from "../lib/dlp.js";
import {
  createErrorMap,
  listErrorMaps,
  updateErrorMap,
} from "../lib/errorMaps.js";
import { createPolicy, listPolicies, updatePolicy, validatePolicy } from "../lib/policies.js";
import { executePolicyDryRun } from "../lib/policyDryRun.js";
import { classifyIntent } from "../lib/intentSecurity.js";
import {
  archiveWebhookDeadLetter,
  createWebhook,
  createWebhookDeadLetterIdempotencyKey,
  listWebhookDeadLetters,
  listWebhookDeliveries,
  listWebhooks,
  replayWebhook,
  replayWebhookDelivery,
  rotateWebhookSecret,
  shouldRetainWebhookDeadLetterIdempotencyKey,
  testWebhook,
} from "../lib/webhooks.js";
import { getServiceStatuses } from "../lib/serviceStatus.js";
import { getSystemHealth } from "../lib/systemHealth.js";
import { getCurrentTenant, updateCurrentTenant } from "../lib/tenant.js";
import { createUniquePageVisitTracker } from "../lib/consoleVisitTracker.js";
import {
  createUsageExportRequestId,
  createUsageExportTraceId,
  pollUsageExportUntilReady,
  requestUsageCsvExport,
  triggerUsageCsvDownload,
  getUsageSummary,
  listUsageRecords,
} from "../lib/usage.js";
import {
  createFinOpsAdjustment,
  createFinOpsIdempotencyKey,
  createFinOpsRequestId,
  createFinOpsTraceId,
  listFinOpsLedger,
} from "../lib/finops.js";
import {
  applyCostAnomalyAction,
  createCostAnomalyIdempotencyKey,
  createCostAnomalyRequestId,
  createCostAnomalyTraceId,
  listCostAnomalies,
  shouldRetainCostAnomalyIdempotencyKey,
} from "../lib/costAnomalies.js";
import {
  applySessionRiskAction,
  createSessionRiskIdempotencyKey,
  createSessionRiskRequestId,
  createSessionRiskTraceId,
  listSessionRisks,
  shouldRetainSessionRiskIdempotencyKey,
} from "../lib/sessionRisks.js";
import { listFiles, uploadFile } from "../lib/files.js";
import {
  createRoutingPolicyRequestId,
  createCacheAffinityRequestId,
  createModelAlias,
  listModelAliases,
  updateFallbackPolicy,
  updateCacheAffinity,
  updateModelAlias,
  updateRoutingMode,
} from "../lib/modelAliases.js";
import {
  createModelQualityScoreRequestId,
  listModelQualityScores,
  updateModelQualityScore,
} from "../lib/modelQualityScores.js";
import { createModel, listModels, updateModel } from "../lib/models.js";
import { createModelPrice, listModelPrices } from "../lib/modelPrices.js";
import { createApplication, createProject, listApplications, listProjects } from "../lib/projects.js";
import { probeProvider } from "../lib/providerHealth.js";
import { listProviderStatuses } from "../lib/providerStatus.js";
import {
  createSavedRequestLogFilter,
  deleteSavedRequestLogFilter,
  getRequestLog,
  listRequestLogs,
  listSavedRequestLogFilters,
  updateSavedRequestLogFilter,
} from "../lib/requestLogs.js";
import { getRequestExplanation } from "../lib/requestExplanations.js";
import {
  createRequestFeedback,
  getRequestFeedbackSummary,
} from "../lib/requestFeedback.js";
import {
  getCurrentPolicySnapshot,
  listPolicySnapshots,
  publishPolicySnapshot,
  rollbackPolicySnapshot,
} from "../lib/policySnapshots.js";
import { createUser, listUsers, updateUserRole } from "../lib/users.js";
import {
  acceptInvitation,
  captureInvitationRoute,
  createInvitation,
} from "../lib/invitations.js";
import {
  createProvider,
  createProviderKey,
  listProviderKeys,
  listProviders,
  testProviderConnection,
  updateProvider,
  updateProviderKey,
} from "../lib/providers.js";
import {
  createVirtualKey,
  disableVirtualKey,
  listVirtualKeys,
  updateVirtualKey,
  updateVirtualKeyLimits,
} from "../lib/virtualKeys.js";
import {
  budgetIdentitySnapshot,
  createBudgetMutationLifecycle,
  isBudgetRefreshOwnerCurrent,
  selectBudgetScopeOption,
} from "../features/budgets/budgetPageLifecycle.js";
import {
  createEmptyPolicyScope,
  createPolicyDraftMemory,
  createPolicyRequestLifecycle,
  policyDraftFromPolicy,
  policyDraftSnapshot,
  policyIdentityFromAuthContext,
  policyIdentitySnapshot,
  renderPoliciesPanel,
  shouldLoadPolicies,
} from "../features/policies/renderPoliciesPanel.js";
import {
  createPolicySnapshotLoadLifecycle,
  createPolicySnapshotOperationMemory,
  isPolicySnapshotReasonValid,
  policySnapshotIdentityFromAuthContext,
  policySnapshotIdentitySnapshot,
  renderPolicySnapshotsPanel,
  shouldLoadPolicySnapshots,
} from "../features/policy-snapshots/renderPolicySnapshotsPanel.js";
import {
  createEmptyPolicyDryRunState,
  createPolicyDryRunLifecycle,
  policyDryRunIdentitySnapshot,
  policyDryRunSelectionsFromForm,
  renderPolicyDryRunPanel,
  shouldOwnPolicyDryRun,
} from "../features/policy-dry-run/renderPolicyDryRunPanel.js";
import {
  createIntentSecurityPageState,
  intentSecurityPageOwner,
} from "../features/intent-security/intentSecurityPageState.js";
import { renderIntentSecurity } from "../features/intent-security/renderIntentSecurity.js";
import {
  createRoutingPolicyPageMemory,
  isRoutingQualityCostConfigValid,
  renderRoutingPolicyPanel,
  routingMinimumSampleCount,
  routingPolicyIdentitySnapshot,
  routingQualityPercentToBps,
} from "../features/routing-policies/renderRoutingPolicyPanel.js";
import {
  cacheAffinityDataClassValue,
  cacheAffinityMinimumPrefixValue,
  cacheAffinityTtlValue,
  createCacheAffinityPageMemory,
  isCacheAffinityDraftValid,
} from "../features/routing-policies/cacheAffinityControls.js";
import {
  createModelQualityScorePageMemory,
  modelQualityScoreIdentitySnapshot,
  qualityPercentToBps,
  renderModelQualityScoresPanel,
} from "../features/model-quality-scores/renderModelQualityScoresPanel.js";
import {
  createRequestLogPageMemory,
  renderRequestLogDetail,
  requestLogFiltersFromSavedFilter,
  requestLogIdentitySnapshot,
} from "../features/request-logs/renderRequestLogPanel.js";
import {
  createErrorMapHandoff,
  createErrorMapPageMemory,
  errorMapIdentitySnapshot,
} from "../features/error-maps/errorMapPageState.js";
import {
  createErrorMapDialogRestoreState,
  resolveErrorMapDialogRestoreTarget,
} from "../features/error-maps/errorMapDialogFocus.js";
import {
  errorMapDraftFromFormData,
  renderErrorMapPanel,
} from "../features/error-maps/renderErrorMapPanel.js";
import { renderRequestExplanationPanel } from "../features/request-explanations/renderRequestExplanation.js";
import {
  createRequestExplanationHandoff,
  createRequestExplanationPageMemory,
  requestExplanationIdentitySnapshot,
} from "../features/request-explanations/requestExplanationPageState.js";
import {
  createRequestFeedbackMutationMemory,
  createRequestFeedbackSummaryMemory,
  renderRequestFeedbackSection,
  requestFeedbackIdentitySnapshot,
} from "../features/request-feedback/renderRequestFeedback.js";
import {
  auditExportIdentitySnapshot,
  createAuditExportLifecycle,
} from "../features/audit-logs/renderAuditLogPanel.js";
import { renderRolePermissionMatrix } from "../features/role-permissions/renderRolePermissionMatrix.js";
import { createRolePermissionPageState } from "../features/role-permissions/rolePermissionPageState.js";
import { createInvitationPageState } from "../features/invitations/invitationPageState.js";
import { renderInvitationAcceptancePage } from "../features/invitations/renderInvitationAcceptancePage.js";

// 非浏览器单测只导入生命周期管理器，不启动应用；真实页面仍要求存在 #app 根节点。
const app = globalThis.document?.querySelector("#app") ?? null;
const gatewayUrl = "http://localhost:8000";
const controlApiUrl = "http://localhost:8001";
const consoleUrl = globalThis.window?.location?.origin ?? "";
// 公开邀请 fragment 必须先于认证 Storage 读取同步截获并清除，避免既有 Session 污染 accept 请求。
const publicInvitationRoute = globalThis.window
  ? captureInvitationRoute(globalThis.window)
  : null;
let authSession = publicInvitationRoute?.isPublicMode()
  ? { token: null, context: null }
  : readAuthSession();
let consoleShellActionError = null;
// 当前 i18n 上下文供 PAGE-070 局部重绘复用；身份清除时会回到默认中文，避免沿用上一用户偏好。
let currentI18n = createI18n(DEFAULT_LOCALE);
let gatewayDebugState = {};
// 生命周期管理器集中持有当前 AbortController 和递增代次，防止旧流回调覆盖连续提交后的新结果。
const gatewayDebugLifecycle = createGatewayDebugStreamLifecycle();
// PAGE-093 的正文、草稿、Session owner 和 AbortController 全部封装在独立闭包，绝不复用 Debug Console。
const enterpriseChatPageState = createEnterpriseChatPageState();
// PAGE-094 的解密正文、变量草稿、五条异步 lane 和稳定写 request_id 只驻留独立闭包。
const promptTemplatePageState = createPromptTemplatePageState();
// PAGE-095 的目录、nonce、双 CAS、确认原因和稳定写 request_id 只驻留独立闭包。
const policyTemplateLibraryPageState = createPolicyTemplatePageState();
let modelAliasActionError = null;
let modelAliasFeedback = "";
// PAGE-072 以 ModelAlias GET 为唯一候选源；两个行级仓库分别隔离 RoutingPolicy 与亲和配置保存。
const routingPolicyPageMemory = createRoutingPolicyPageMemory({
  requestIdFactory: createRoutingPolicyRequestId,
});
const cacheAffinityPageMemory = createCacheAffinityPageMemory({
  requestIdFactory: createCacheAffinityRequestId,
});
// PAGE-069 使用独立 GET/PATCH 代次；分页或身份变化会 abort 旧读取和全部行级评分保存。
const modelQualityScorePageMemory = createModelQualityScorePageMemory({
  requestIdFactory: createModelQualityScoreRequestId,
});
let modelQualityScoreFilters = { modelId: "", limit: 100, offset: 0 };
let modelActionError = null;
let modelFeedback = "";
let modelPriceActionError = null;
let modelPriceFeedback = "";
let modelPriceFilters = { effectiveStatus: "all", limit: 100, offset: 0 };
let providerActionError = null;
let providerFeedback = "";
let providerLastTestResult = null;
// PAGE-088 筛选只驻留当前页面内存；tenant_id、窗口和 Endpoint 永不进入浏览器筛选状态。
let providerStatusFilters = { limit: 50, offset: 0 };
let providerHealthActionError = null;
let providerHealthProbingProviderId = null;
let providerHealthRequestGeneration = 0;
let providerHealthController = null;
// Provider 状态 GET 使用完整身份、导航、筛选、Token 与 generation 共同约束迟到响应。
const providerStatusPageState = createProviderStatusPageState();
// PAGE-092 使用独立 owner 和记忆；selected view 只驻留内存，禁止写入 URL、Storage 或用户偏好。
const roleDashboardPageState = createRoleDashboardPageState();
let roleDashboardEntrySource = readActiveNavigationHash() === "overview" ? "direct_hash" : "refresh";
// PAGE-090 目录筛选和 GET owner 完全独立于 Provider 状态页，任一页面失败或离开都不能清空另一页面。
let adapterSpecFilters = { view: "default", status: "", provider_type: "" };
const adapterSpecPageState = createAdapterSpecPageState();
// PAGE-091 的列表、opaque cursor、详情和完整异步 owner 全部封装在独立内存容器，禁止写入 URL/Storage。
const adapterContractRunPageState = createAdapterContractRunPageState();
let projectActionError = null;
let projectFeedback = "";
let environmentActionError = null;
let environmentFeedback = "";
// RequestLog 列表和详情共享完整身份 owner；登出、换租户或离页会同步 abort 并销毁全部事实。
const requestLogPageMemory = createRequestLogPageMemory();
// PAGE-080 与 PAGE-099 共用读取链路，但导航 ID 必须进入 owner，防止两页迟到响应互相覆盖。
const REQUEST_LOG_NAVIGATION_IDS = new Set(["request-logs", "output-safety"]);
// PAGE-079 使用独立 owner 和单次内存 handoff；源 request_id 永不进入 URL 或浏览器 Storage。
const requestExplanationPageMemory = createRequestExplanationPageMemory();
const requestExplanationHandoff = createRequestExplanationHandoff();
// PAGE-070 POST 独立于日志 list/detail 刷新；只有详情或身份 owner 改变才会 abort 并清除 dirty 草稿。
const requestFeedbackMutationMemory = createRequestFeedbackMutationMemory();
// PAGE-071 GET 使用独立 AbortController，不能取消 PAGE-070 POST 或复用 RequestLog refresh ticket。
const requestFeedbackSummaryMemory = createRequestFeedbackSummaryMemory();
let requestFeedbackSummaryFilters = { modelId: "", fromTime: "", toTime: "", limit: 20, offset: 0 };
let auditLogFilters = { limit: 20, offset: 0 };
// 审计 CSV 只保存安全反馈；Blob 仅存在于单次调用栈，独立生命周期负责完整身份、导航和 Abort owner。
let auditExportState = { status: "idle" };
const auditExportLifecycle = createAuditExportLifecycle();
// PAGE-085 的矩阵、dirty 草稿和 CAS ticket 仅驻留当前授权身份内存；禁止进入 Storage 或 URL。
const rolePermissionPageState = createRolePermissionPageState();
// PAGE-087 的链接、邮件文案和异步 owner 全部由私有闭包持有，公开 snapshot 永不含密文。
const invitationPageState = createInvitationPageState();
// PAGE-082 到 PAGE-081 的引用只通过完整身份绑定的一次性内存交接，不进入 URL 或 Storage。
const errorMapHandoff = createErrorMapHandoff();
// PAGE-081 的规则、历史引用、dialog、mutation 与冲突 GET 只保存在完整身份 owner 下的内存仓库。
const errorMapPageMemory = createErrorMapPageMemory();
let errorMapFilters = { providerId: "", source: "", status: "", outputErrorCode: "", limit: 100, offset: 0 };
// 对话框恢复 descriptor 同时绑定稳定按钮身份和完整 Session owner，局部重绘后也不能跨身份聚焦。
let errorMapDialogRestoreState = null;
let errorMapDialogKeydownHandler = null;
let errorMapBodyOverflow = null;
// 每次刷新分配递增序号；较早刷新即使更晚返回，也不能覆盖用户已经切换的新页面。
let refreshSequence = 0;
// 完整控制台刷新包含多组租户接口，慢环境下可能超过定时周期；计数用于阻止定时器制造永不落地的刷新饥饿。
let activeRefreshCount = 0;
let usageFilters = { limit: 20, offset: 0 };
let usageActionError = null;
// CSV 导出状态只驻留当前认证会话内存；controller 和 generation 防止离页后的旧轮询覆盖新会话页面。
let usageExportState = { status: "idle", message: "" };
let usageExportController = null;
let usageExportGeneration = 0;
// 正式账本的 cursor、调整原因和幂等键只驻留该内存容器；离页、登出或身份变化必须整体清空。
const finOpsPageState = createFinOpsPageState();
// 成本异常的 cursor、处置草稿和异步 owner 只驻留当前 admin 会话；离页或身份变化必须整体销毁。
const costAnomalyPageState = createCostAnomalyPageState();
// PAGE-098 的风险 DTO、cursor、双权限 owner 与幂等意图只驻留当前授权 Session 内存。
const sessionRiskPageState = createSessionRiskPageState();
// PAGE-101/PAGE-102 的文件名、安全摘要、私有 cursor 与上传 ticket 只属于当前授权 owner。
const filePageState = createFilePageState();
let budgetActionError = null;
let budgetFeedback = "";
// 预算规则快照只属于当前认证会话，用于选择 create-only POST 或兼容 PATCH，不能跨租户复用。
let budgetRulesSnapshot = [];
// PAGE-076 作用链选择只保存不透明 Key ID；可信 Environment/Project/Application 由后端恢复。
let selectedBudgetVirtualKeyId = "";
// Budget Mutation 使用独立 owner；后台刷新、导航或身份变化都会使旧回调失效。
const budgetMutationLifecycle = createBudgetMutationLifecycle();
let dlpActionError = null;
let dlpFeedback = "";
// DLP 方向筛选只保存固定枚举，不携带 tenant_id；切换身份后列表仍由后端按新登录态重新读取。
let dlpScanScopeFilter = "";
// 策略正文和校验只驻留当前页面内存；仓库按 tenant+user 隔离，生命周期阻止旧异步响应跨身份或离页回写。
const policyDraftMemory = createPolicyDraftMemory();
const policyRequestLifecycle = createPolicyRequestLifecycle();
let policyListState = null;
let policyModelsState = null;
let policyListController = null;
let policyActionError = null;
let policyFeedback = "";
let policyOperationStatus = "idle";
let policyListFilters = { status: "all", action: "all" };
// 策略快照读写状态只驻留当前 admin 页面；读取 owner 与写 controller 分离，刷新不能取消已提交但结果未知的操作。
const policySnapshotLoadLifecycle = createPolicySnapshotLoadLifecycle();
const policySnapshotOperationMemory = createPolicySnapshotOperationMemory();
let policySnapshotsState = null;
let policySnapshotActionError = null;
let policySnapshotFeedback = "";
let policySnapshotOperationStatus = "idle";
let policySnapshotHistoryFilters = { limit: 20, offset: 0 };
let policySnapshotPublishReason = "";
let policySnapshotRollbackTargetId = null;
let policySnapshotRollbackReason = "";
let policySnapshotOperationController = null;
let policySnapshotOperationGeneration = 0;
// 策略预演只保存非敏感选择和脱敏响应；Prompt 永远只驻留 textarea 与当前 submit 调用栈。
const policyDryRunLifecycle = createPolicyDryRunLifecycle();
let policyDryRunState = createEmptyPolicyDryRunState();
let policyDryRunContextState = null;
// PAGE-096 只保存固定结果和错误摘要；分类正文永远不进入应用模块状态。
const intentSecurityPageState = createIntentSecurityPageState();
let webhookActionError = null;
let webhookFeedback = "";
let webhookCreatedSecret = null;
let webhookLastTestResult = null;
let webhookLastReplayResult = null;
let webhookSelectedEndpointId = null;
let webhookDeliveryFilters = { eventType: "", status: "all" };
// 未决重放键按 Endpoint+Delivery 保存在当前会话内存，网络结果不确定时再次点击必须复用同一键。
const webhookPendingReplayKeys = new Map();
// PAGE-083 的 cursor、幂等键、完整身份 owner、Abort 和轮询计数只驻留该独立内存容器。
const webhookDeadLetterPageState = createWebhookDeadLetterPageState();
let virtualKeyCreatedSecret = null;
let virtualKeyActionError = null;
let virtualKeyFeedback = "";
let userPreferenceActionError = null;
let userPreferenceFeedback = "";
// SDK active language 使用模块级状态，保证 15 秒刷新和下载反馈后仍保留选择；进入登录页时统一重置为 Python。
let activeSdkLanguage = "python";
// 迁移指南使用完全独立的语言和显式配置选择，禁止与 SDK tabs 或其他页面筛选状态相互覆盖。
let activeMigrationLanguage = "python";
let selectedMigrationEnvironmentId = null;
let selectedMigrationModelAlias = null;
let migrationGuideEventWarning = null;
// 初始 hash 直达使用 direct_hash；侧栏点击会在切换函数中显式改为 sidebar，且二者都只能取后端允许的枚举。
let migrationGuideEntrySource = readActiveNavigationHash() === "migration-guide" ? "direct_hash" : "sidebar";
// 唯一访问 tracker 在首屏渲染后记录迁移事件；模块级实例跨 15 秒刷新保留 attempted/inFlight 状态。
const migrationGuideVisitTracker = createMigrationGuideVisitTracker(recordMigrationGuideVisit);
// 竞品对比页使用独立 tab 与访问生命周期，定时刷新不得重置用户选择或重复写入事件。
let activeCompetitorTab = "positioning";
let competitorEventWarning = null;
let competitorEntrySource = readActiveNavigationHash() === "competitor-comparison" ? "direct_hash" : "sidebar";
const competitorVisitTracker = createUniquePageVisitTracker({
  targetItemId: "competitor-comparison",
  recordVisit: recordCompetitorVisit,
});
// PAGE-088 访问事件绑定已授权页面 shell；状态 API 为空或 503 都不影响每次进入只记一次。
let providerStatusEventWarning = null;
let providerStatusEntrySource = readActiveNavigationHash() === "provider-health" ? "direct_hash" : "sidebar";
const providerStatusVisitTracker = createProviderStatusVisitTracker(recordProviderStatusVisit);

// 只有真实页面根节点存在时才启动网络加载；Node 单测导入本模块不会产生定时器或请求副作用。
if (app && globalThis.window) {
  if (publicInvitationRoute?.isPublicMode()) {
    // 公开页不绑定控制台 hash、认证或遥测；离页只需销毁尚未消费的一次性凭据。
    window.addEventListener("pagehide", () => publicInvitationRoute.clear());
    window.addEventListener("hashchange", () => {
      if (window.location.hash === "#accept-invitation") return;
      // 公开页离开无凭据哨兵时立即销毁 Token；重载后再由普通认证/导航 bootstrap 接管。
      publicInvitationRoute.leave();
      window.location.reload();
    }, { once: true });
    renderPublicInvitation();
  } else {
    // 地址栏手写 hash 和同页锚点只触发 hashchange；必须重新请求后端导航，及时清理受限入口。
    bindNavigationHashRefresh(window, refresh);
    window.addEventListener("hashchange", () => {
      // PAGE-088 额外监听同一 hash 边界，同步淘汰旧状态回调；通用监听仍负责重读后端导航。
      providerStatusPageState.clear();
      // PAGE-090 同样不能等待导航 API 返回才失效；手写 hash 离页时立即阻止旧目录写回 DOM。
      adapterSpecPageState.clear();
      // PAGE-091 的列表和详情都绑定当前 hash；手写切换必须在导航 API 返回前淘汰全部迟到回调。
      adapterContractRunPageState.clear();
      const nextItemId = readActiveNavigationHash();
      if (nextItemId !== "enterprise-chat") {
        // 地址栏离开 PAGE-093 时先清正文和在途请求，不能等待导航 API 或下一轮全页渲染。
        clearEnterpriseChatSessionState();
      }
      if (nextItemId !== "prompt-templates") {
        // 地址栏离开 PAGE-094 时先清解密正文和五条 lane，禁止旧租户定义在下一页面恢复。
        clearPromptTemplateSessionState();
      }
      if (nextItemId !== "intent-security") {
        // 地址栏离开 PAGE-096 时同步擦除 textarea、abort POST 并推进 owner 代次，不能等待导航 API 返回。
        clearIntentSecuritySessionState();
      }
      if (nextItemId !== "session-risks") {
        // PAGE-098 的列表、cursor、Dialog 和未决幂等意图必须在 hash 离页时立即失效。
        sessionRiskPageState.clear();
      }
      if (nextItemId !== "files") {
        // 文件名、opaque cursor 和在途上传必须在 hash 离页时立即失效，不能等待后端导航返回。
        filePageState.clear();
      }
      if (nextItemId === "overview") {
        // 地址栏进入 overview 只记录页面来源；selected view 从不编码到 hash。
        roleDashboardEntrySource = "direct_hash";
      } else {
        // 离开首页时立即取消 role-view 并清除旧卡片，不等待导航接口或下一轮全页渲染。
        clearRoleDashboardSessionState();
      }
      if (nextItemId !== "adapter-specs") {
        adapterSpecFilters = { view: "default", status: "", provider_type: "" };
      }
      // Provider 状态页单独维护访问来源；其他 hash 必须销毁 tracker，避免跨页面误记事件。
      if (nextItemId === "provider-health") {
        providerStatusEntrySource = "direct_hash";
        providerStatusEventWarning = null;
      } else {
        providerStatusVisitTracker.leave();
        providerStatusEventWarning = null;
      }
    });
    // 浏览器关闭、刷新或离开页面时同步 abort 活动流并销毁全部一次性凭据。
    window.addEventListener("pagehide", () => {
      gatewayDebugLifecycle.leave();
      invitationPageState.clear();
      clearUsageExportSessionState();
      clearAuditExportSessionState();
      costAnomalyPageState.clear();
      sessionRiskPageState.clear();
      filePageState.clear();
      webhookDeadLetterPageState.clear();
      clearPolicyEditorSessionState();
      clearPolicySnapshotSessionState();
      clearPolicyDryRunSessionState();
      clearIntentSecuritySessionState();
      clearRoutingPolicySessionState();
      clearModelQualityScoreSessionState();
      clearErrorMapSessionState();
      clearRequestLogSessionState();
      clearRequestExplanationSessionState();
      clearRequestFeedbackSummarySessionState();
      migrationGuideVisitTracker.leave();
      competitorVisitTracker.leave();
      providerStatusPageState.clear();
      providerStatusVisitTracker.leave();
      clearRoleDashboardSessionState();
      adapterSpecPageState.clear();
      adapterContractRunPageState.clear();
      clearEnterpriseChatSessionState();
      clearPromptTemplateSessionState();
    });
    void start();
  }
}

/**
 * 渲染并绑定公开邀请接受页。
 *
 * 该路径在认证 bootstrap 前运行；提交只调用 `acceptInvitation`，不会读取或附加现有 Session。
 */
function renderPublicInvitation(state = null) {
  if (!app || !publicInvitationRoute?.isPublicMode()) return;
  const token = publicInvitationRoute.readToken();
  const viewState = state ?? (
    token
      ? { status: "form" }
      : { status: "invalid", message: "邀请链接不可用或已失效" }
  );
  app.innerHTML = renderInvitationAcceptancePage(viewState);

  const loginButton = document.querySelector("[data-invitation-login]");
  loginButton?.addEventListener("click", () => {
    // 接受页不复用可能属于管理员的旧 Session；回到登录页前先清空本地认证状态。
    publicInvitationRoute.leave();
    clearAuthSession();
    authSession = { token: null, context: null };
    window.location.reload();
  });

  const form = document.querySelector("#invitation-accept-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    const displayName = String(formData.get("displayName") ?? "");
    const password = String(formData.get("password") ?? "");
    const confirmPassword = String(formData.get("confirmPassword") ?? "");
    if (password !== confirmPassword) {
      renderPublicInvitation({ status: "error", message: "两次输入的密码不一致" });
      return;
    }
    const currentToken = publicInvitationRoute.readToken();
    // 哨兵页或凭据已销毁时必须失败关闭，不能从 Session、history 或 Storage 恢复邀请。
    if (!currentToken) {
      renderPublicInvitation({ status: "invalid", message: "邀请链接不可用或已失效" });
      return;
    }
    renderPublicInvitation({ status: "submitting" });
    try {
      const result = await acceptInvitation({
        controlApiUrl,
        token: currentToken,
        displayName,
        password,
      });
      // 成功后先销毁凭据再渲染用户摘要，后续任何 DOM handler 都无法再次读取 Token。
      publicInvitationRoute.clear();
      renderPublicInvitation({ status: "success", user: result.user });
    } catch (error) {
      // 公开终态统一销毁凭据并展示同形失效页，避免重试差异泄漏真实 Invitation 状态。
      if (Number(error?.status) === 410) {
        publicInvitationRoute.clear();
        renderPublicInvitation({
          status: "invalid",
          message: "邀请链接不可用或已失效",
          requestId: error?.requestId !== "-" ? error?.requestId : "",
        });
        return;
      }
      renderPublicInvitation({
        status: "error",
        message: error?.message || "邀请服务暂不可用",
        requestId: error?.requestId !== "-" ? error?.requestId : "",
      });
    }
  });
}

/**
 * 执行 控制台主应用 的 `start` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function start() {
  if (!authSession.token) {
    renderLogin();
    return;
  }
  try {
    const context = await getCurrentUser({ controlApiUrl, token: authSession.token });
    clearBudgetStateOnIdentityChange(authSession.context, context);
    clearWebhookStateOnIdentityChange(authSession.context, context);
    clearUsageExportStateOnIdentityChange(authSession.context, context);
    clearAuditExportStateOnIdentityChange(authSession.context, context);
    clearCostAnomalyStateOnIdentityChange(authSession.context, context);
    clearSessionRiskStateOnIdentityChange(authSession.context, context);
    clearFileStateOnIdentityChange(authSession.context, context);
    clearWebhookDeadLetterStateOnIdentityChange(authSession.context, context);
    clearPolicyStateOnIdentityChange(authSession.context, context);
    clearPolicySnapshotStateOnIdentityChange(authSession.context, context);
    clearPolicyDryRunStateOnIdentityChange(authSession.context, context);
    clearIntentSecurityStateOnIdentityChange(authSession.context, context);
    clearRoutingPolicyStateOnIdentityChange(authSession.context, context);
    clearModelQualityScoreStateOnIdentityChange(authSession.context, context);
    clearErrorMapStateOnIdentityChange(authSession.context, context);
    clearRequestLogStateOnIdentityChange(authSession.context, context);
    clearRequestExplanationStateOnIdentityChange(authSession.context, context);
    clearRequestFeedbackSummaryStateOnIdentityChange(authSession.context, context);
    clearRolePermissionStateOnIdentityChange(authSession.context, context);
    clearPromptTemplateStateOnIdentityChange(authSession.context, context);
    clearPolicyTemplateLibraryStateOnIdentityChange(authSession.context, context);
    clearEnterpriseChatStateOnIdentityChange(authSession.context, context);
    // tenant、Session、permission revision 或指纹任一漂移都必须在替换认证上下文前销毁旧首页 owner 和 DOM。
    if (roleDashboardIdentitySnapshot(authSession.context) !== roleDashboardIdentitySnapshot(context)) {
      clearRoleDashboardSessionState();
      roleDashboardEntrySource = "refresh";
    }
    saveAuthSession(authSession.token, context);
    authSession = { token: authSession.token, context };
    await refresh();
  } catch (error) {
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin(authErrorMessage(error), error?.requestId);
  }
}

/**
 * 执行 控制台主应用 的 `refresh` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function refresh() {
  // 交互刷新仍允许覆盖更早结果；活动计数只供后台定时器判断，不能吞掉用户刚完成的操作反馈。
  activeRefreshCount += 1;
  try {
    return await performRefresh();
  } finally {
    activeRefreshCount -= 1;
  }
}

/**
 * 执行单轮控制台数据读取和渲染。
 * refresh 外层必须用 finally 维护活动计数，确保认证失败或任一提前返回都不会永久关闭定时刷新。
 */
async function performRefresh() {
  const refreshId = ++refreshSequence;
  // Token 与刷新序号共同构成本轮 owner；只冻结公开的会话句柄，不复制用户上下文或任何业务敏感数据。
  const refreshToken = authSession.token;
  // 刷新入口仍以有效登录态为第一边界，序号不能替代认证校验。
  if (!refreshToken) {
    renderLogin();
    return;
  }
  try {
    const context = await getCurrentUser({ controlApiUrl, token: refreshToken });
    // 注销、新登录或更新一轮刷新都会让旧 owner 失效；必须在写 Storage、身份内存和租户页面状态前丢弃迟到响应。
    if (refreshId !== refreshSequence || authSession.token !== refreshToken) {
      return;
    }
    clearBudgetStateOnIdentityChange(authSession.context, context);
    clearWebhookStateOnIdentityChange(authSession.context, context);
    clearUsageExportStateOnIdentityChange(authSession.context, context);
    clearAuditExportStateOnIdentityChange(authSession.context, context);
    clearCostAnomalyStateOnIdentityChange(authSession.context, context);
    clearSessionRiskStateOnIdentityChange(authSession.context, context);
    clearFileStateOnIdentityChange(authSession.context, context);
    clearWebhookDeadLetterStateOnIdentityChange(authSession.context, context);
    clearFinOpsStateOnIdentityChange(authSession.context, context);
    clearPolicyStateOnIdentityChange(authSession.context, context);
    clearPolicySnapshotStateOnIdentityChange(authSession.context, context);
    clearPolicyDryRunStateOnIdentityChange(authSession.context, context);
    clearIntentSecurityStateOnIdentityChange(authSession.context, context);
    clearRoutingPolicyStateOnIdentityChange(authSession.context, context);
    clearModelQualityScoreStateOnIdentityChange(authSession.context, context);
    clearErrorMapStateOnIdentityChange(authSession.context, context);
    clearRequestLogStateOnIdentityChange(authSession.context, context);
    clearRequestExplanationStateOnIdentityChange(authSession.context, context);
    clearRequestFeedbackSummaryStateOnIdentityChange(authSession.context, context);
    clearRolePermissionStateOnIdentityChange(authSession.context, context);
    // PAGE-092 owner 显式包含 tenant、Session、permission revision 与指纹；漂移时先清 DOM，再写入新认证上下文。
    if (roleDashboardIdentitySnapshot(authSession.context) !== roleDashboardIdentitySnapshot(context)) {
      clearRoleDashboardSessionState();
      roleDashboardEntrySource = "refresh";
    }
    // 目录响应属于完整授权身份；tenant、Session、revision、权限指纹任一变化都先 abort 再替换 Session。
    if (adapterSpecIdentitySnapshot(authSession.context) !== adapterSpecIdentitySnapshot(context)) {
      adapterSpecPageState.clear();
      adapterSpecFilters = { view: "default", status: "", provider_type: "" };
    }
    // 契约运行事实同样属于完整 tenant/Session/RBAC owner；任一 revision 或指纹变化都先销毁旧列表与详情。
    if (
      adapterContractRunIdentitySnapshot(authSession.context)
      !== adapterContractRunIdentitySnapshot(context)
    ) {
      adapterContractRunPageState.clear();
    }
    saveAuthSession(refreshToken, context);
    authSession = { token: refreshToken, context };
  } catch (error) {
    // 旧请求的认证错误不能清除注销后新建的会话，也不能覆盖当前登录页或新身份页面。
    if (refreshId !== refreshSequence || authSession.token !== refreshToken) {
      return;
    }
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin(authErrorMessage(error), error?.requestId);
    return;
  }
  const consoleShellState = await loadConsoleShellState();
  if (consoleShellState?.activeNavigationItemId !== "enterprise-chat") {
    // 导航裁剪、非法 hash 或离页必须在任何其他租户读取前销毁 Chat 正文与在途回调。
    clearEnterpriseChatSessionState();
  }
  await loadEnterpriseChatState(consoleShellState);
  // Chat 加载若因实时 401 清空了 Token，必须停止后续受保护页面读取，避免携带失效身份继续请求。
  if (!authSession.token) {
    return;
  }
  if (consoleShellState?.activeNavigationItemId !== "prompt-templates") {
    // 导航裁剪、非法 hash 或降权先清 PAGE-094 正文/草稿，其他页面绝不后台预取模板定义。
    clearPromptTemplateSessionState();
  }
  const promptTemplateState = await loadPromptTemplateState(consoleShellState, refreshId);
  // 模板读取若确认 Session 失效会同步清空 Token；后续任何租户接口都不得继续使用旧登录态。
  if (!authSession.token) {
    return;
  }
  if (consoleShellState?.activeNavigationItemId !== "policy-templates") {
    // PAGE-095 的绑定、nonce 和双 CAS 只属于当前授权页面，其他导航绝不后台预取或保留。
    clearPolicyTemplateLibrarySessionState();
  }
  const policyTemplateLibraryState = await loadPolicyTemplateLibraryState(
    consoleShellState,
    refreshId,
  );
  // PAGE-095 读取若已确认 Session 失效会清空 Token，后续租户页面不得继续沿用旧认证事实。
  if (!authSession.token) {
    return;
  }
  // 角色首页只属于后端确认的 overview；导航裁剪、非法 hash 或离页时立即 abort 并清除受保护卡片。
  if (!roleDashboardNavigationOwned(consoleShellState, readNavigationHashState())) {
    clearRoleDashboardSessionState();
  }
  // PAGE-088 owner 只属于后端确认的 provider-health 导航；离页或权限裁剪后立即取消在途状态读取。
  if (consoleShellState?.activeNavigationItemId !== "provider-health") {
    providerStatusPageState.clear();
  }
  // PAGE-090 只有后端仍确认 active owner 时才能保留 GET；导航裁剪或离页立即销毁上一租户目录。
  if (consoleShellState?.activeNavigationItemId !== "adapter-specs") {
    adapterSpecPageState.clear();
    adapterSpecFilters = { view: "default", status: "", provider_type: "" };
  }
  // PAGE-091 只在后端仍确认 active owner 时保留内存事实；导航裁剪会同步 abort 列表和详情。
  if (consoleShellState?.activeNavigationItemId !== "adapter-contract-runs") {
    adapterContractRunPageState.clear();
  }
  // 邀请弹窗必须同时属于当前 Session、授权指纹和后端确认的 users 导航；任一漂移立即清除密文。
  invitationPageState.syncOwner({
    authContext: authSession.context,
    token: refreshToken,
    activeNavigation: consoleShellState?.activeNavigationItemId,
  });
  if (consoleShellState?.activeNavigationItemId !== "budgets") {
    // 离开 PAGE-076 会推进 Mutation revision，旧写响应不能回填其他页面或下一次登录。
    clearBudgetSessionState();
  }
  // Policy 草稿只属于后端确认的 active 策略页；直接 hash、角色裁剪或导航切换后必须立即清空正文和旧响应。
  if (consoleShellState?.activeNavigationItemId !== "policies") {
    clearPolicyEditorSessionState();
  }
  // 发布原因、双 CAS 和未决幂等键只属于 active 快照页；离页必须同步 abort 并清除，不能跨租户或角色残留。
  if (consoleShellState?.activeNavigationItemId !== "policy-snapshots") {
    clearPolicySnapshotSessionState();
  }
  // Prompt 与解释结果只属于当前 admin 模拟器；离页或后端裁剪导航后必须立即销毁。
  if (consoleShellState?.activeNavigationItemId !== "policy-simulator") {
    clearPolicyDryRunSessionState();
  }
  // PAGE-096 的正文、结果和 POST ticket 只属于后端确认的专用导航；裁剪或离页立即清理。
  if (consoleShellState?.activeNavigationItemId !== "intent-security") {
    clearIntentSecuritySessionState();
  }
  // 路由策略草稿与 ticket 只属于 PAGE-067；导航裁剪或离页必须在任何旧 PATCH 回调前失效。
  if (consoleShellState?.activeNavigationItemId !== "routing-policies") {
    clearRoutingPolicySessionState();
  }
  // PAGE-069 数据和人工评分草稿只属于后端确认的 active 导航；viewer 或 hash 回退不会触发后台预取。
  if (consoleShellState?.activeNavigationItemId !== "model-quality-scores") {
    clearModelQualityScoreSessionState();
  }
  // PAGE-081 的 dialog/dirty/busy/conflict 只属于后端确认的 active 导航；降权或裁剪必须立即销毁。
  if (consoleShellState?.activeNavigationItemId !== "error-maps") {
    clearErrorMapSessionState();
  } else if (errorMapPageMemory.blocksShellDomReplacement()) {
    // 15 秒刷新已经在上方重新校验 Session 与后端导航；同 owner 编辑中禁止 app.innerHTML 重建，
    // 以保留 dialog、焦点陷阱和滚动锁。身份或权限变化已经通过前置 clear/导航判断强制 leave。
    return;
  }
  // 调用日志只属于后端确认的 active 页面；离页先 abort list/detail，再销毁上一租户详情。
  if (!REQUEST_LOG_NAVIGATION_IDS.has(consoleShellState?.activeNavigationItemId)) {
    clearRequestLogSessionState();
  }
  // PAGE-079 状态只属于后端确认的解释入口；离页、导航裁剪或降权立即销毁源请求标识与响应。
  if (consoleShellState?.activeNavigationItemId !== "request-explanations") {
    clearRequestExplanationSessionState();
  }
  // PAGE-071 只属于后端授权的 active 导航；viewer/hash 回退和离页必须同步停止 GET 并清空上一租户报表。
  if (consoleShellState?.activeNavigationItemId !== "feedback-summary") {
    clearRequestFeedbackSummarySessionState();
  }
  // 导出任务只属于用量页面当前会话；后端任务继续持久化，但离页必须终止浏览器轮询并清除旧反馈。
  if (consoleShellState?.activeNavigationItemId !== "usage" && usageExportState.status !== "idle") {
    clearUsageExportSessionState();
  }
  // PAGE-084 导出 owner 只属于后端确认的审计导航；角色裁剪、非法 hash 回退或离页立即 abort 并清反馈。
  if (consoleShellState?.activeNavigationItemId !== "audit-logs" && auditExportState.status !== "idle") {
    clearAuditExportSessionState();
  }
  // 正式账本 cursor、详情和调整原因只属于后端确认的 admin 页面，离页立即销毁全部内存事实。
  if (consoleShellState?.activeNavigationItemId !== "finops-ledger") {
    finOpsPageState.clear();
  }
  // PAGE-077/078 只属于后端确认的 admin 导航；离页立即取消 GET/PATCH 并销毁 cursor 与处置草稿。
  if (consoleShellState?.activeNavigationItemId !== "cost-anomalies") {
    costAnomalyPageState.clear();
  }
  // PAGE-098 只属于后端确认的 read 导航；离页或降权立即 abort GET/POST 并销毁全部证据。
  if (consoleShellState?.activeNavigationItemId !== "session-risks") {
    sessionRiskPageState.clear();
  }
  // PAGE-101/PAGE-102 只属于后端确认的 files 导航；离页或撤权立即清除文件名和上传 owner。
  if (consoleShellState?.activeNavigationItemId !== "files") {
    filePageState.clear();
  }
  // PAGE-083 只属于后端确认的 manager 导航；离页、hash 回退或降权立即取消 GET/POST 与轮询。
  if (consoleShellState?.activeNavigationItemId !== "webhook-dead-letters") {
    webhookDeadLetterPageState.clear();
  }
  // PAGE-085 矩阵和草稿只能属于后端仍授权的 active 导航；降权或离页立即销毁。
  if (consoleShellState?.activeNavigationItemId !== "role-permissions") {
    rolePermissionPageState.clear();
  }
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  // PAGE-085 只在后端授权导航激活时读取；loader 还会冻结 tenant、Session、revision 与授权指纹。
  const rolePermissionState = await loadRolePermissionState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  const userPreferencesState = await loadUserPreferencesState();
  // 语言偏好同样依赖当前登录态，401 必须立即回到登录页。
  if (!authSession.token) {
    return;
  }
  const i18n = createI18n(userPreferencesState?.preference?.locale ?? DEFAULT_LOCALE);
  currentI18n = i18n;
  const roleDashboardState = await loadRoleDashboardState(
    consoleShellState,
    roleDashboardEntrySource,
  );
  // 角色首页失败会保留统一错误或同 owner partial，但当前 401 仍必须切断全部后续租户读取。
  if (!authSession.token) {
    return;
  }
  const quickstartState = await loadQuickstartState(consoleShellState);
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const sdkExampleStates = await loadSdkExampleStates(consoleShellState);
  // 任一语言元数据接口确认会话失效后，顺序加载器会统一清理会话，后续租户数据请求必须停止。
  if (!authSession.token) {
    return;
  }
  const tenantState = await loadTenantState();
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const usersState = await loadUsersState(consoleShellState);
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const providerHealthState = await loadProviderHealthState(consoleShellState);
  // 健康接口若确认会话失效会清空 Token，后续所有租户配置读取必须立即停止。
  if (!authSession.token) {
    return;
  }
  // PAGE-090 只在后端确认 active 导航后读取；loader 会再次冻结权限、身份、筛选和 Token owner。
  const adapterSpecsState = await loadAdapterSpecsState(consoleShellState);
  if (!authSession.token) {
    return;
  }
  // PAGE-091 只有精确后端导航 owner 才读取，viewer 和其他导航不会预取运行或 case。
  const adapterContractRunsState = await loadAdapterContractRunsState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  const environmentState = await loadEnvironmentState();
  // Environment 是 Key、别名和预算的共同依赖，认证失败时必须停止后续配置读取。
  if (!authSession.token) {
    return;
  }
  const providersState = await loadProvidersState();
  // Provider 管理读取可能因会话过期清空 Token，此时不能继续加载模型及健康数据。
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const modelsState = await loadModelsState();
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  // 策略正文只在后端确认 active policies 导航后读取；route_to 选项复用本轮已授权模型目录。
  const policiesState = await loadPoliciesState(consoleShellState, refreshId, modelsState);
  if (!authSession.token) {
    return;
  }
  // 策略快照原因和历史仅在后端确认 active admin 导航后读取，非管理员不会预取任何发布元数据。
  const policySnapshotsRenderState = await loadPolicySnapshotsState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  const modelPricesState = await loadModelPricesState();
  // 模型价格属于成本治理数据，认证失败时必须停止后续模型配置读取。
  if (!authSession.token) {
    return;
  }
  // PAGE-072 两个编辑器复用本轮唯一一次 ModelAlias GET，各自 refresh ticket 都不参与对方 PATCH 门禁。
  const routingPolicyRefreshTicket = (
    refreshId === refreshSequence
    && consoleShellState?.activeNavigationItemId === "routing-policies"
  )
    ? routingPolicyPageMemory.beginRefresh({
        identity: routingPolicyIdentitySnapshot(authSession.context),
        navigation: "routing-policies",
      })
    : null;
  const cacheAffinityRefreshTicket = routingPolicyRefreshTicket
    ? cacheAffinityPageMemory.beginRefresh({
        identity: routingPolicyIdentitySnapshot(authSession.context),
        navigation: "routing-policies",
      })
    : null;
  const modelAliasesState = await loadModelAliasesState();
  if (routingPolicyRefreshTicket) {
    if (modelAliasesState?.state === "ok") {
      routingPolicyPageMemory.acceptRefresh(routingPolicyRefreshTicket, modelAliasesState.model_aliases);
    } else {
      routingPolicyPageMemory.acceptRefreshError(routingPolicyRefreshTicket, modelAliasesState);
    }
  }
  if (cacheAffinityRefreshTicket) {
    if (modelAliasesState?.state === "ok") {
      cacheAffinityPageMemory.acceptRefresh(cacheAffinityRefreshTicket, modelAliasesState.model_aliases);
    } else {
      cacheAffinityPageMemory.acceptRefreshError(cacheAffinityRefreshTicket, modelAliasesState);
    }
  }
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  // PAGE-069 只有在后端授权且确认 active 导航后才读取；loader 自身还会复核角色、身份和 refresh owner。
  const modelQualityScoresState = await loadModelQualityScoresState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  // PAGE-081 只在后端 active 导航和 admin/developer 角色同时成立时读取，viewer 不会预取。
  const errorMapsState = await loadErrorMapsState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  const projectState = await loadProjectState();
  // 项目归属会影响 Key 创建和日志/用量筛选，认证失败时必须停止后续渲染。
  if (!authSession.token) {
    return;
  }
  const virtualKeysState = await loadVirtualKeysState();
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const usageState = await loadUsageState();
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  // PAGE-074/075 只在后端确认 active admin 导航后读取，其他页面不后台预取正式财务分录。
  const finOpsState = await loadFinOpsState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  // PAGE-077/078 只在后端授权的 admin 导航激活时读取，其他页面不预取成本异常治理事实。
  const costAnomaliesState = await loadCostAnomaliesState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  // PAGE-098 只有后端导航与实时 read 权限共同成立时读取，其他页面保持零预取。
  const sessionRisksState = await loadSessionRisksState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  // PAGE-101/PAGE-102 只有后端 files 导航与实时 file.read 同时成立时读取，其他页面零预取。
  const filesState = await loadFilesState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  const budgetState = await loadBudgetState(virtualKeysState, consoleShellState, refreshId);
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const dlpState = await loadDlpState();
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const webhookState = await loadWebhookState();
  // PAGE-083 仅在后端确认 active 且角色为 admin/developer 时读取；其他页面零死信预取。
  const webhookDeadLettersState = await loadWebhookDeadLettersState(consoleShellState, refreshId);
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  const requestLogsState = await loadRequestLogsState(consoleShellState, refreshId);
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!authSession.token) {
    return;
  }
  // PAGE-079 只在后端授权导航激活时读取一次完整解释，禁止在其他页面后台预取。
  const requestExplanationState = await loadRequestExplanationState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  // PAGE-071 只有在后端导航确认 active 且角色可读时才发 GET；loader 还会复核完整身份和 refresh owner。
  const requestFeedbackSummaryState = await loadRequestFeedbackSummaryState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  // 审计日志只有在后端确认当前导航为 audit-logs 后才读取，避免在其他页面后台预取治理数据。
  const auditLogsState = await loadAuditLogsState(consoleShellState, refreshId);
  if (!authSession.token) {
    return;
  }
  const [statuses, configSummary, systemHealth] = await Promise.all([
    getServiceStatuses({
      gatewayUrl,
      controlApiUrl,
      consoleUrl,
    }),
    getConfigSummary({ controlApiUrl }),
    getSystemHealth({
      gatewayUrl,
      controlApiUrl,
    }),
  ]);
  // 导航点击或定时器可能在当前请求期间启动新刷新，旧结果必须在渲染前丢弃。
  if (refreshId !== refreshSequence) {
    return;
  }
  // 上下文缓存只含已授权配置对象，不含 Prompt；供提交后的局部重绘复用。
  policyDryRunContextState = consoleShellState?.activeNavigationItemId === "policy-simulator"
    ? { environmentState, projectState, modelsState, modelAliasesState }
    : null;
  const intentSecurityOwner = intentSecurityPageOwner(
    authSession.context,
    consoleShellState,
    { refreshId, currentRefreshId: refreshSequence },
  );
  // 只有实时权限和本轮后端导航共同成立才绑定 PAGE-096 owner；失败分支清空结果且不会创建隐藏 DOM。
  if (intentSecurityOwner) {
    intentSecurityPageState.enter(intentSecurityOwner);
  } else {
    intentSecurityPageState.leave();
  }
  renderDeveloperStart(app, {
    gatewayUrl,
    controlApiUrl,
    consoleUrl,
    statuses,
    configSummary,
    systemHealth,
    authContext: authSession.context,
    consoleShellState,
    i18n,
    roleDashboardState,
    providerHealthState,
    adapterSpecsState,
    adapterContractRunsState,
    userPreferencesState,
    quickstartState,
    sdkExampleStates,
    activeSdkLanguage,
    activeMigrationLanguage,
    selectedMigrationEnvironmentId,
    selectedMigrationModelAlias,
    migrationGuideEventWarning,
    activeCompetitorTab,
    competitorEventWarning,
    tenantState,
    usersState,
    invitationState: invitationPageState.snapshot(),
    environmentState,
    providersState,
    modelsState,
    policiesState,
    policyDryRunState,
    intentSecurityState: intentSecurityPageState.snapshot(),
    policySnapshotsState: policySnapshotsRenderState,
    modelPricesState,
    modelAliasesState,
    // 读取后的其他接口可能较慢；渲染前重新取快照，避免期间发生的选择或保存被旧对象重建 DOM 覆盖。
    routingPoliciesState: consoleShellState?.activeNavigationItemId === "routing-policies"
      ? {
          ...routingPolicyPageMemory.snapshot(),
          cacheAffinity: cacheAffinityPageMemory.snapshot(),
        }
      : null,
    modelQualityScoresState,
    modelQualityScoreFilters,
    errorMapsState,
    errorMapFilters,
    projectState,
    virtualKeysState,
    usageState,
    finOpsState,
    costAnomaliesState,
    sessionRisksState,
    filesState,
    budgetState,
    dlpState,
    webhookState,
    webhookDeadLettersState,
    requestLogsState,
    requestExplanationState,
    requestFeedbackState: requestFeedbackMutationMemory.snapshot(),
    requestFeedbackSummaryState,
    requestFeedbackSummaryFilters,
    auditLogsState,
    rolePermissionState,
    promptTemplateState,
    policyTemplateLibraryState,
    // 全页读取可能与局部建会话/发送并行；渲染前必须重新取最新快照，不能用本轮开始时的旧 Chat 状态覆盖成功结果。
    enterpriseChatState: enterpriseChatPageState.snapshot(),
    gatewayDebugState,
  });
  // Prompt/Response 不进入 renderDeveloperStart 的 innerHTML 字符串，只在 DOM 就位后通过 textContent/value 注入。
  if (consoleShellState?.activeNavigationItemId === "enterprise-chat") {
    hydrateEnterpriseChatText(app, enterpriseChatPageState.snapshot());
  }
  if (consoleShellState?.activeNavigationItemId === "prompt-templates") {
    // 解密正文与变量草稿只能在当前授权 DOM 就位后通过 textContent/value 注入。
    hydratePromptTemplateSensitiveText(app, promptTemplatePageState.snapshot());
  }
  // 当前导航进入 API 调试台时使用 PRD-037 的直连 Gateway 流式面板；模板内所有动态值均已安全转义。
  if (consoleShellState?.activeNavigationItemId === "debug-console") {
    const gatewayDebugPanel = document.querySelector(".gateway-debug-panel");
    if (gatewayDebugPanel) {
      gatewayDebugPanel.innerHTML = renderGatewayDebugConsole(gatewayDebugState, { gatewayUrl });
    }
  }
  // PAGE-099 的稳定父面板已随本轮 DOM 一起生成；先绑定局部详情，避免通用长绑定链中
  // 任一无关模块异常让“查看详情”失去响应。真实读取仍由后端 RBAC 与 RequestLog owner 约束。
  bindOutputSafetyDetailInteractions(consoleShellState);
  bindAuthenticatedEvents(consoleShellState);
}

// 定时刷新只在浏览器应用启动后注册，避免测试导入时遗留活动句柄。
if (app && globalThis.window) {
  window.setInterval(() => {
    // 公开邀请模式没有认证刷新；即使页面停留很久也不能触发 /auth/me 或控制台导航请求。
    if (publicInvitationRoute?.isPublicMode()) {
      return;
    }
    // 未登录时登录表单本身就是唯一有效状态；后台重建 DOM 会清除用户正在输入的邮箱和密码，
    // 还可能让浏览器密码管理器或辅助技术失去当前焦点，因此必须等 Session 建立后再刷新。
    if (!authSession.token) {
      return;
    }
    // 慢环境中一轮完整读取可能超过 15 秒；后台刷新必须等待空闲，避免持续递增序号淘汰交互反馈。
    if (activeRefreshCount > 0) {
      return;
    }
    // 写预算会改变 Gateway 准入；请求未决时不能让定时刷新重建表单并制造第二个提交入口。
    if (budgetMutationLifecycle.isBusy()) {
      return;
    }
    // 成本处置结果未知时不能由定时刷新重建对话框；GET 与 PATCH 生命周期仍保持彼此独立。
    if (costAnomalyPageState.isActionBusy()) {
      return;
    }
    // 会话风险处置结果未知时禁止定时刷新重建 Dialog；显式提交完成后再由同 owner 刷新列表。
    if (sessionRiskPageState.isActionBusy()) {
      return;
    }
    // 在途上传或浏览器 file input 已选中文件时不重建 DOM，避免丢失不可恢复的用户选择或制造重复 POST。
    if (filePageState.isUploadBusy() || fileUploadHasSelection()) {
      return;
    }
    // 死信重放/归档结果未知时不得由全页定时刷新重建操作 DOM。
    if (webhookDeadLetterPageState.isActionBusy()) {
      return;
    }
    // PAGE-085 的 CAS 写请求或未保存矩阵草稿不能被全页刷新重建 DOM；离页、
    // 注销和显式身份变化仍会通过各自生命周期立即清理这些内存事实。
    const rolePermissionSnapshot = rolePermissionPageState.snapshot();
    if (rolePermissionSnapshot.busy || rolePermissionSnapshot.dirty) {
      return;
    }
    // PAGE-087 活动、脏、忙或持有一次性链接时都不能被全页刷新重建。
    if (invitationPageState.isBlockingRefresh()) {
      return;
    }
    // 策略保存尚未返回时不能启动新 refresh，避免取消已提交但结果未知的写请求。
    if (policyRequestLifecycle.isBusy()) {
      return;
    }
    // 发布/回滚写请求不能被 15 秒全页刷新替换 DOM 或取消；网络结果未知后恢复 idle，允许重新读取并安全复用原票据。
    if (policySnapshotOperationStatus !== "idle") {
      return;
    }
    // 预演提交或 textarea 中尚有敏感正文时不做全页定时刷新，避免重建 DOM 丢失或复制 Prompt。
    if (policyDryRunLifecycle.isBusy() || policyDryRunHasSensitiveInput()) {
      return;
    }
    // PAGE-096 在途请求或 textarea 正文均为短生命周期敏感数据；定时重绘不得复制或丢失取消入口。
    if (intentSecurityPageState.isBusy() || intentSecurityHasSensitiveInput()) {
      return;
    }
    // PAGE-093 的草稿、会话、正文或在途调用只能驻留当前 DOM/闭包；定时全页重建会丢失或复制敏感正文。
    if (enterpriseChatPageState.blocksRefresh()) {
      return;
    }
    // PAGE-094 激活期间只允许页面内显式刷新；全壳重建会丢失解密正文、焦点和本地 CAS 草稿。
    if (
      document.querySelector(".console-shell")?.dataset.activeNavigation === "prompt-templates"
      || promptTemplatePageState.blocksShellDomReplacement()
    ) {
      return;
    }
    // PAGE-095 的精确预览和确认依赖同一瞬时 CAS，激活期间只允许页面内显式刷新。
    if (document.querySelector(".console-shell")?.dataset.activeNavigation === "policy-templates") {
      return;
    }
    // 调用日志由查询、应用和分页显式刷新；定时重建会丢失未提交筛选并中断 SavedFilter 写请求。
    if (document.querySelector(".console-shell")?.dataset.activeNavigation === "request-logs") {
      return;
    }
    // PAGE-083 使用自身有界局部轮询和显式刷新，避免每次状态确认重载整套控制台数据。
    if (document.querySelector(".console-shell")?.dataset.activeNavigation === "webhook-dead-letters") {
      return;
    }
    void refresh();
  }, 15000);
}

/**
 * 渲染 控制台主应用 的 `renderLogin` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderLogin(error, requestId) {
  // 登录态变化等同于离开调试页，必须取消旧流并使尚未返回的回调失效。
  gatewayDebugLifecycle.leave();
  gatewayDebugState = {};
  // 登录页是强身份边界，上一 tenant 的 Chat 正文、Session ID 和迟到响应必须同步销毁。
  clearEnterpriseChatSessionState();
  // 登录页同样是 PAGE-094 强边界，先清可见正文再 abort，不能保留上一 tenant 模板定义。
  clearPromptTemplateSessionState();
  // 登录页不得保留上一 tenant 的策略模板绑定、预览 nonce、双 CAS 或应用结果。
  clearPolicyTemplateLibrarySessionState();
  // 登录边界不能保留上一 Session 的邀请草稿、链接或邮件文案。
  invitationPageState.clear();
  currentI18n = createI18n(DEFAULT_LOCALE);
  // 登录边界变化时停止导出轮询；job、文件名和 request_id 不得跨用户或租户残留。
  clearUsageExportSessionState();
  clearAuditExportSessionState();
  costAnomalyPageState.clear();
  sessionRiskPageState.clear();
  filePageState.clear();
  webhookDeadLetterPageState.clear();
  clearBudgetSessionState();
  // 登录页不得保留上一管理员的 Policy、DSL 草稿、校验指纹或异步请求 owner。
  clearPolicyEditorSessionState();
  // 登录边界变化必须取消快照读取/写入并销毁原因、ETag 和未决 request_id。
  clearPolicySnapshotSessionState();
  // 登录页绝不保留上一个管理员的 Prompt、解释或上下文选择。
  clearPolicyDryRunSessionState();
  // 登录页同样必须擦除 PAGE-096 textarea、结果和分类 POST owner。
  clearIntentSecuritySessionState();
  clearRoutingPolicySessionState();
  clearModelQualityScoreSessionState();
  clearErrorMapSessionState();
  // 登录页不能保留上一租户的调用列表、详情 request_id、错误反馈或未完成 fetch。
  clearRequestLogSessionState();
  // 登录页不能保留上一租户的请求解释、一次性跳转 ID 或活动读取。
  clearRequestExplanationSessionState();
  // 登录页不能保留上一租户的满意度报表、筛选窗口或活动 GET owner。
  clearRequestFeedbackSummarySessionState();
  // 登录页不能保留上一租户的角色矩阵、授权上限或未提交 CAS 草稿。
  rolePermissionPageState.clear();
  // 登录边界会切换用户、租户或 Session，必须清除一次性 Secret、旧操作结果和未决幂等键。
  clearWebhookSessionState();
  // 登出或 401 回到登录页时重置 SDK 语言，下一次登录必须从产品默认的 Python tab 开始。
  activeSdkLanguage = "python";
  // 登录边界变化必须结束当前 visit，并清空只属于上一会话的选择和遥测告警。
  migrationGuideVisitTracker.leave();
  activeMigrationLanguage = "python";
  selectedMigrationEnvironmentId = null;
  selectedMigrationModelAlias = null;
  migrationGuideEventWarning = null;
  migrationGuideEntrySource = readActiveNavigationHash() === "migration-guide" ? "direct_hash" : "sidebar";
  competitorVisitTracker.leave();
  activeCompetitorTab = "positioning";
  competitorEventWarning = null;
  competitorEntrySource = readActiveNavigationHash() === "competitor-comparison" ? "direct_hash" : "sidebar";
  providerStatusPageState.clear();
  providerStatusVisitTracker.leave();
  clearRoleDashboardSessionState();
  roleDashboardEntrySource = readActiveNavigationHash() === "overview" ? "direct_hash" : "refresh";
  providerStatusFilters = { limit: 50, offset: 0 };
  providerStatusEventWarning = null;
  providerStatusEntrySource = readActiveNavigationHash() === "provider-health" ? "direct_hash" : "sidebar";
  // 登录页不得保留上一租户 PAGE-090 的目录请求或筛选，即使旧 fetch 忽略 Abort 也会因 generation 失效。
  adapterSpecPageState.clear();
  adapterSpecFilters = { view: "default", status: "", provider_type: "" };
  // 登录边界必须销毁 PAGE-091 的运行、case、opaque cursor 和请求 owner，避免跨租户证据残留。
  adapterContractRunPageState.clear();
  renderLoginPage(app, { error, requestId, tenantSlug: "default" });
  const form = document.querySelector("#login-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "登录中";
    try {
      const login = await loginAdmin({
        controlApiUrl,
        tenantSlug: data.get("tenantSlug"),
        email: data.get("email"),
        password: data.get("password"),
      });
      const context = await getCurrentUser({ controlApiUrl, token: login.access_token });
      saveAuthSession(login.access_token, context);
      authSession = { token: login.access_token, context };
      await refresh();
    } catch (err) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(err), err?.requestId);
    }
  });
}

/** 生成当前邀请弹窗 owner 输入；真实权限仍由后端创建接口再次校验。 */
function invitationOwnerInput(consoleShellState) {
  return {
    authContext: authSession.context,
    token: authSession.token,
    activeNavigation: consoleShellState?.activeNavigationItemId,
  };
}

/** 用不含链接和 Token 的安全快照局部更新 PAGE-087。 */
function applyInvitationDialogSnapshot(dialog, snapshot) {
  const submitButton = dialog?.querySelector(".invitation-create-button");
  const closeButton = dialog?.querySelector("[data-invitation-close]");
  const feedback = dialog?.querySelector(".invitation-feedback");
  const result = dialog?.querySelector(".invitation-created-result");
  if (submitButton) {
    submitButton.disabled = Boolean(snapshot.busy);
    submitButton.textContent = snapshot.busy ? "正在生成" : "生成邀请";
  }
  if (closeButton) closeButton.disabled = Boolean(snapshot.busy);
  if (closeButton) closeButton.setAttribute("aria-disabled", String(Boolean(snapshot.busy)));
  if (feedback) {
    feedback.classList.toggle("error-state", snapshot.feedback?.state === "error");
    feedback.textContent = snapshot.feedback
      ? `${snapshot.feedback.message} · request_id: ${snapshot.feedback.requestId}`
      : "";
  }
  if (result) {
    result.hidden = !snapshot.hasSecret;
    result.querySelector("[data-invitation-result-email]")?.replaceChildren(
      snapshot.invitation?.email ?? "-",
    );
    result.querySelector("[data-invitation-result-role]")?.replaceChildren(
      snapshot.invitation?.role ?? "-",
    );
    result.querySelector("[data-invitation-result-expires]")?.replaceChildren(
      snapshot.invitation?.expiresAt ?? "-",
    );
  }
  // 没有一次性结果时同步清空手动复制 DOM，避免旧 Token 跨重绘残留。
  if (!snapshot.hasSecret) clearInvitationManualCopy(dialog);
}

const invitationManualCopyTimers = new WeakMap();
/** 清空显式手动复制区，确保 dialog 关闭、重试或身份变化后 DOM 不残留一次性凭据。 */
function clearInvitationManualCopy(dialog) {
  const container = dialog?.querySelector("[data-invitation-manual-copy]");
  const timer = container ? invitationManualCopyTimers.get(container) : null;
  if (timer) globalThis.clearTimeout(timer);
  if (container) {
    invitationManualCopyTimers.delete(container);
    container.replaceChildren();
    container.hidden = true;
  }
  const manualLink = dialog?.querySelector("[data-manual-invitation-link]");
  const manualMail = dialog?.querySelector("[data-manual-invitation-mail]");
  if (manualLink) manualLink.hidden = true;
  if (manualMail) manualMail.hidden = true;
}

/** 仅在用户再次点击“手动复制”后，短暂创建只读文本区供选择。 */
function revealInvitationSecret(dialog, value, labelText) {
  const container = dialog?.querySelector("[data-invitation-manual-copy]");
  clearInvitationManualCopy(dialog);
  if (!container || !value) return;
  const wrapper = document.createElement("label");
  const label = document.createElement("span");
  label.textContent = labelText;
  const textarea = document.createElement("textarea");
  textarea.className = "invitation-manual-copy-field";
  textarea.readOnly = true;
  textarea.rows = labelText === "邮件文案" ? 6 : 3;
  textarea.value = value;
  textarea.setAttribute("aria-label", `手动复制${labelText}`);
  wrapper.append(label, textarea);
  container.append(wrapper);
  container.hidden = false;
  textarea.focus();
  textarea.select();
  const timer = globalThis.setTimeout(() => clearInvitationManualCopy(dialog), 30_000);
  invitationManualCopyTimers.set(container, timer);
}

/** 自动写入 Clipboard；失败时只开放手工按钮，不能直接把一次性凭据放进 DOM。 */
async function copyInvitationSecret(
  value,
  feedback,
  successMessage,
  dialog,
  manualButtonSelector,
) {
  clearInvitationManualCopy(dialog);
  if (!value || !globalThis.navigator?.clipboard?.writeText) {
    const manualButton = dialog?.querySelector(manualButtonSelector);
    if (manualButton) manualButton.hidden = false;
    if (feedback) feedback.textContent = "当前浏览器无法访问剪贴板，请选择手动复制";
    return;
  }
  try {
    await globalThis.navigator.clipboard.writeText(value);
    if (feedback) feedback.textContent = successMessage;
  } catch {
    // Clipboard 拒绝后只展示第二次显式操作；固定反馈不能拼接一次性链接或原始异常。
    const manualButton = dialog?.querySelector(manualButtonSelector);
    if (manualButton) manualButton.hidden = false;
    if (feedback) feedback.textContent = "自动复制失败，请选择手动复制";
  }
}

/**
 * 执行 控制台主应用 的 `bindAuthenticatedEvents` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function bindAuthenticatedEvents(consoleShellState) {
  // PAGE-093 使用独立事件绑定；DOM 不存在或后端导航未授权时该函数立即失败关闭且不发请求。
  bindEnterpriseChatInteractions(consoleShellState);
  // PAGE-094 只在精确后端导航 owner 存在时绑定，伪造按钮不能绕过 read/write permission。
  bindPromptTemplateInteractions(consoleShellState);
  // PAGE-095 仅在精确后端导航 owner 存在时绑定，伪造 DOM 不能构造 preview 或 apply ticket。
  bindPolicyTemplateLibraryInteractions(consoleShellState);
  const invitationDialog = document.querySelector("#invitation-dialog");
  const invitationForm = document.querySelector("#invitation-form");
  const invitationFeedback = invitationDialog?.querySelector(".invitation-feedback");
  const closeInvitationDialog = () => {
    if (!invitationPageState.close()) return;
    clearInvitationManualCopy(invitationDialog);
    invitationForm?.reset();
    if (invitationDialog?.open) invitationDialog.close();
  };
  document.querySelector("#invite-user-button")?.addEventListener("click", () => {
    if (!invitationPageState.openDialog(invitationOwnerInput(consoleShellState))) return;
    applyInvitationDialogSnapshot(invitationDialog, invitationPageState.snapshot());
    if (invitationDialog && !invitationDialog.open) {
      invitationDialog.showModal();
      invitationForm?.querySelector("input[name='email']")?.focus();
    }
  });
  invitationDialog?.querySelector("[data-invitation-close]")?.addEventListener(
    "click",
    closeInvitationDialog,
  );
  invitationDialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeInvitationDialog();
  });
  invitationDialog?.addEventListener("close", () => {
    clearInvitationManualCopy(invitationDialog);
    invitationPageState.close();
  });
  invitationForm?.addEventListener("input", () => invitationPageState.markDirty());
  invitationForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ownerInput = invitationOwnerInput(consoleShellState);
    const data = new FormData(invitationForm);
    const intent = {
      email: String(data.get("email") ?? "").trim().toLowerCase(),
      role: String(data.get("role") ?? "").trim(),
    };
    const ticket = invitationPageState.beginCreate(ownerInput, intent);
    if (!ticket) return;
    clearInvitationManualCopy(invitationDialog);
    applyInvitationDialogSnapshot(invitationDialog, invitationPageState.snapshot());
    try {
      const result = await createInvitation({
        controlApiUrl,
        token: ownerInput.token,
        email: intent.email,
        role: intent.role,
        expiresInHours: Number(data.get("expiresInHours")),
      });
      if (invitationPageState.resolveCreate(ticket, result, consoleUrl)) {
        applyInvitationDialogSnapshot(invitationDialog, invitationPageState.snapshot());
      }
    } catch (error) {
      // Session 失效是强身份边界，必须先销毁邀请密文，再清理认证状态并回到登录页。
      if (Number(error?.status) === 401) {
        invitationPageState.clear();
        clearInvitationManualCopy(invitationDialog);
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(error?.message, error?.requestId);
        return;
      }
      // 后端 403 表示实时权限漂移；前端不能保留旧草稿或依赖隐藏按钮继续授权。
      if (Number(error?.status) === 403) {
        invitationPageState.clear();
        clearInvitationManualCopy(invitationDialog);
        if (invitationDialog?.open) invitationDialog.close();
        await refresh();
        return;
      }
      // 其余稳定业务错误只允许当前 owner 和代次接收，迟到响应不得覆盖新身份页面。
      if (invitationPageState.rejectCreate(ticket, error)) {
        applyInvitationDialogSnapshot(invitationDialog, invitationPageState.snapshot());
      }
    }
  });
  invitationDialog?.querySelector("[data-copy-invitation-link]")?.addEventListener(
    "click",
    () => copyInvitationSecret(
      invitationPageState.readAcceptLink(),
      invitationFeedback,
      "邀请链接已复制",
      invitationDialog,
      "[data-manual-invitation-link]",
    ),
  );
  invitationDialog?.querySelector("[data-copy-invitation-mail]")?.addEventListener(
    "click",
    () => copyInvitationSecret(
      invitationPageState.readMailText(),
      invitationFeedback,
      "邮件文案已复制",
      invitationDialog,
      "[data-manual-invitation-mail]",
    ),
  );
  invitationDialog?.querySelector("[data-manual-invitation-link]")?.addEventListener(
    "click",
    () => revealInvitationSecret(invitationDialog, invitationPageState.readAcceptLink(), "邀请链接"),
  );
  invitationDialog?.querySelector("[data-manual-invitation-mail]")?.addEventListener(
    "click",
    () => revealInvitationSecret(invitationDialog, invitationPageState.readMailText(), "邮件文案"),
  );
  if (invitationPageState.snapshot().open && invitationDialog && !invitationDialog.open) {
    applyInvitationDialogSnapshot(invitationDialog, invitationPageState.snapshot());
    invitationDialog.showModal();
  }

  const logoutButton = document.querySelector("#logout-button");
  logoutButton?.addEventListener("click", async () => {
    // 注销是强认证边界：冻结推进后的 owner，既淘汰旧刷新，也约束迟到注销响应不能覆盖后续新会话。
    const logoutGeneration = ++refreshSequence;
    // 退出登录前先关闭可能携带当前租户 Virtual Key 的活动连接，不能等待登出 API 返回。
    gatewayDebugLifecycle.leave();
    gatewayDebugState = {};
    // 注销请求发出前清除企业 Chat，不能等待后端撤销 Session 后再处理浏览器正文。
    clearEnterpriseChatSessionState();
    // 注销请求发出前同步清空模板正文、变量草稿和所有请求 lane。
    clearPromptTemplateSessionState();
    // 策略模板的 nonce、双 CAS、绑定和确认原因同样必须在注销请求前销毁。
    clearPolicyTemplateLibrarySessionState();
    // 登出在网络请求前就结束迁移 visit，避免失效 Session 的异步事件完成后污染下一次登录。
    migrationGuideVisitTracker.leave();
    competitorVisitTracker.leave();
    providerStatusPageState.clear();
    providerStatusVisitTracker.leave();
    clearRoleDashboardSessionState();
    adapterSpecPageState.clear();
    adapterContractRunPageState.clear();
    const token = authSession.token;
    // 一次性 Secret 必须在发起登出请求前从状态和 DOM 清除，不能等待网络响应后再处理。
    clearWebhookSessionState();
    clearUsageExportSessionState();
    clearAuditExportSessionState();
    finOpsPageState.clear();
    costAnomalyPageState.clear();
    sessionRiskPageState.clear();
    filePageState.clear();
    webhookDeadLetterPageState.clear();
    clearPolicyEditorSessionState();
    clearPolicySnapshotSessionState();
    clearPolicyDryRunSessionState();
    clearIntentSecuritySessionState();
    clearRoutingPolicySessionState();
    clearModelQualityScoreSessionState();
    clearErrorMapSessionState();
    clearRequestLogSessionState();
    clearRequestExplanationSessionState();
    clearRequestFeedbackSummarySessionState();
    rolePermissionPageState.clear();
    invitationPageState.clear();
    document.querySelector(".webhook-secret-box")?.remove();
    clearAuthSession();
    authSession = { token: null, context: null };
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (token) {
      try {
        await logoutAdmin({ controlApiUrl, token });
      } catch (error) {
        // 用户可能已通过其他认证路径建立新 owner；旧注销错误不得覆盖新 Session 的页面。
        if (logoutGeneration !== refreshSequence || authSession.token) {
          return;
        }
        // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        if (!(error instanceof AuthApiError) || error.status !== 401) {
          renderLogin(authErrorMessage(error), error?.requestId);
          return;
        }
      }
    }
    // 注销等待期间若产生更新代次或新 Token，本轮只完成后端撤销，不再改写当前前端身份。
    if (logoutGeneration !== refreshSequence || authSession.token) {
      return;
    }
    renderLogin();
  });

  for (const navigationButton of document.querySelectorAll("[data-console-nav]")) {
    navigationButton.addEventListener("click", async () => {
      await switchConsoleNavigation(navigationButton, consoleShellState);
    });
  }

  const languageSettingsForm = document.querySelector("#language-settings-form");
  languageSettingsForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 语言偏好只能写当前登录用户，前端不得提交 tenant_id 或 user_id。
    if (!token) {
      renderLogin();
      return;
    }
    const data = new FormData(languageSettingsForm);
    const nextLocale = String(data.get("locale") ?? DEFAULT_LOCALE);
    const formI18n = createI18n(nextLocale);
    const button = languageSettingsForm.querySelector("button[type='submit']");
    if (button) {
      button.disabled = true;
      button.textContent = formI18n.t("language.saving");
    }
    userPreferenceActionError = null;
    userPreferenceFeedback = formI18n.t("language.saving");
    try {
      const result = await updateUserPreferences({
        controlApiUrl,
        token,
        locale: nextLocale,
      });
      const savedLocale = result.preference?.locale ?? nextLocale;
      userPreferenceFeedback = createI18n(savedLocale).t("language.saved");
      await refresh();
    } catch (error) {
      // 认证失败必须清理本地登录态，避免语言设置页在过期会话上继续提交。
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      userPreferenceActionError = error;
      userPreferenceFeedback = "";
      await refresh();
    }
  });

  bindQuickstartCopyButtons();
  bindSdkLanguageTabs();
  bindSdkExampleDownloads();
  bindMigrationGuideInteractions();
  bindCompetitorInteractions();
  bindRolePermissionInteractions(consoleShellState);
  bindPolicyInteractions(consoleShellState);
  bindPolicySnapshotInteractions(consoleShellState);
  bindPolicyDryRunInteractions(consoleShellState);
  bindIntentSecurityInteractions(consoleShellState);
  bindRoutingPolicyInteractions(consoleShellState);
  bindModelQualityScoreInteractions(consoleShellState);
  bindErrorMapInteractions(consoleShellState);
  bindRequestExplanationInteractions(consoleShellState);
  bindRequestFeedbackInteractions(consoleShellState);
  bindRequestFeedbackSummaryInteractions(consoleShellState);
  bindRoleDashboardInteractions(consoleShellState);
  // 页面完成首屏渲染并绑定交互后再启动唯一访问 owner；失败只更新局部告警，不阻断指南阅读。
  void ownMigrationGuideVisitAfterRender(consoleShellState);
  void ownCompetitorVisitAfterRender(consoleShellState);
  void ownProviderStatusVisitAfterRender(consoleShellState);
  void ownRoleDashboardVisitAfterRender(consoleShellState);

  const tenantForm = document.querySelector("#tenant-form");
  tenantForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const button = tenantForm.querySelector("button[type='submit']");
    const feedback = tenantForm.querySelector(".tenant-feedback");
    const data = new FormData(tenantForm);
    button.disabled = true;
    if (feedback) {
      feedback.textContent = "保存中";
    }
    try {
      const result = await updateCurrentTenant({
        controlApiUrl,
        token,
        name: String(data.get("tenantName") ?? ""),
        serviceMode: String(data.get("serviceMode") ?? ""),
      });
      authSession.context = {
        ...authSession.context,
        tenant: {
          ...authSession.context.tenant,
          ...result.tenant,
        },
      };
      saveAuthSession(token, authSession.context);
      await refresh();
    } catch (error) {
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      button.disabled = false;
      if (feedback) {
        feedback.textContent = `${authErrorMessage(error)} request_id: ${error?.requestId ?? "-"}`;
      }
    }
  });

  const createUserForm = document.querySelector("#create-user-form");
  createUserForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createUserForm.querySelector("button[type='submit']");
    const feedback = createUserForm.querySelector(".users-feedback");
    const data = new FormData(createUserForm);
    button.disabled = true;
    if (feedback) {
      feedback.textContent = "创建中";
    }
    try {
      await createUser({
        controlApiUrl,
        token,
        email: String(data.get("email") ?? ""),
        displayName: String(data.get("displayName") ?? ""),
        password: String(data.get("password") ?? ""),
        role: String(data.get("role") ?? ""),
      });
      await refresh();
    } catch (error) {
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      button.disabled = false;
      if (feedback) {
        feedback.textContent = `${authErrorMessage(error)} request_id: ${error?.requestId ?? "-"}`;
      }
    }
  });

  for (const roleForm of document.querySelectorAll("[data-user-role-form]")) {
    roleForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = roleForm.querySelector("button[type='submit']");
      const feedback = document.querySelector("#create-user-form .users-feedback");
      const data = new FormData(roleForm);
      button.disabled = true;
      if (feedback) {
        feedback.textContent = "保存角色中";
      }
      try {
        await updateUserRole({
          controlApiUrl,
          token,
          userId: roleForm.dataset.userId,
          role: String(data.get("role") ?? ""),
        });
        await refresh();
      } catch (error) {
        // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        button.disabled = false;
        if (feedback) {
          feedback.textContent = `${authErrorMessage(error)} request_id: ${error?.requestId ?? "-"}`;
        }
      }
    });
  }

  const createEnvironmentForm = document.querySelector("#create-environment-form");
  createEnvironmentForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 环境创建必须依赖当前认证会话，令牌缺失时不得继续提交配置。
    if (!token) {
      renderLogin();
      return;
    }
    const data = new FormData(createEnvironmentForm);
    const button = createEnvironmentForm.querySelector("button[type='submit']");
    button.disabled = true;
    environmentActionError = null;
    environmentFeedback = "环境创建中";
    try {
      await createEnvironment({
        controlApiUrl,
        token,
        code: String(data.get("code") ?? ""),
        name: String(data.get("name") ?? ""),
        description: String(data.get("description") ?? ""),
      });
      environmentFeedback = "环境已创建";
      await refresh();
    } catch (error) {
      // 会话过期必须清理本地令牌；其他业务错误保留 request_id 回到环境面板展示。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      environmentActionError = error;
      environmentFeedback = "环境创建失败";
      await refresh();
    }
  });

  for (const form of document.querySelectorAll("[data-environment-status-form]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 环境启停会改变数据面访问边界，令牌缺失时必须立即回到登录页。
      if (!token) {
        renderLogin();
        return;
      }
      const data = new FormData(form);
      const nextStatus = String(data.get("status") ?? "");
      const button = form.querySelector("button[type='submit']");
      button.disabled = true;
      environmentActionError = null;
      environmentFeedback = nextStatus === "disabled" ? "环境禁用中" : "环境启用中";
      try {
        await updateEnvironment({
          controlApiUrl,
          token,
          environmentId: form.dataset.environmentId,
          status: nextStatus,
        });
        environmentFeedback = nextStatus === "disabled" ? "环境已禁用" : "环境已启用";
        await refresh();
      } catch (error) {
        // Environment 写权限由后端 admin 依赖裁决，前端仅展示返回的统一错误。
        environmentActionError = error;
        environmentFeedback = "环境状态更新失败";
        await refresh();
      }
    });
  }

  const providerHealthForm = document.querySelector("#provider-health-filters");
  providerHealthForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(providerHealthForm);
    providerStatusFilters = {
      health_status: String(data.get("health_status") ?? ""),
      provider_status: String(data.get("provider_status") ?? ""),
      provider_type: String(data.get("provider_type") ?? ""),
      limit: providerStatusFilters.limit ?? 50,
      offset: 0,
    };
    // 筛选提交即刻取消旧筛选请求，不能等待下一轮 /auth/me 和导航读取后才淘汰迟到响应。
    providerStatusPageState.clear();
    await refresh();
  });
  document.querySelector("[data-provider-health-reset]")?.addEventListener("click", async (event) => {
    event.preventDefault();
    providerStatusFilters = { limit: 50, offset: 0 };
    providerStatusPageState.clear();
    await refresh();
  });
  for (const button of document.querySelectorAll("[data-provider-status-offset]")) {
    button.addEventListener("click", async () => {
      const nextOffset = Number(button.dataset.providerStatusOffset);
      // offset 只能来自渲染器生成的非负安全整数；异常 DOM 值不触发网络。
      if (!Number.isSafeInteger(nextOffset) || nextOffset < 0) return;
      providerStatusFilters = {
        ...providerStatusFilters,
        limit: providerStatusFilters.limit ?? 50,
        offset: nextOffset,
      };
      providerStatusPageState.clear();
      await refresh();
    });
  }
  for (const button of document.querySelectorAll("[data-provider-probe]")) {
    button.addEventListener("click", async () => {
      await runProviderProbe(String(button.dataset.providerProbe ?? ""));
    });
  }

  for (const button of document.querySelectorAll("[data-adapter-spec-view]")) {
    button.addEventListener("click", async () => {
      const view = String(button.dataset.adapterSpecView ?? "");
      const next = canonicalAdapterSpecFilters({ ...adapterSpecFilters, view, provider_type: "" });
      adapterSpecFilters = { ...next };
      // tab 切换立即淘汰旧分类响应，不能等待完整 refresh 后再取消。
      adapterSpecPageState.clear();
      await refresh();
    });
  }
  const adapterSpecForm = document.querySelector("#adapter-spec-filters");
  adapterSpecForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(adapterSpecForm);
    adapterSpecFilters = {
      ...canonicalAdapterSpecFilters({
        view: adapterSpecFilters.view,
        status: data.get("status"),
        provider_type: data.get("provider_type"),
      }),
    };
    adapterSpecPageState.clear();
    await refresh();
  });
  document.querySelector("[data-adapter-spec-reset]")?.addEventListener("click", async (event) => {
    event.preventDefault();
    adapterSpecFilters = { view: adapterSpecFilters.view, status: "", provider_type: "" };
    adapterSpecPageState.clear();
    await refresh();
  });

  bindAdapterContractRunInteractions(consoleShellState);

  const createProjectForm = document.querySelector("#create-project-form");
  createProjectForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 项目写入会影响所有后续归因，前端只提交表单，admin 权限必须由后端重复校验。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createProjectForm.querySelector("button[type='submit']");
    const data = new FormData(createProjectForm);
    button.disabled = true;
    projectActionError = null;
    projectFeedback = "项目创建中";
    try {
      await createProject({
        controlApiUrl,
        token,
        projectCode: String(data.get("projectCode") ?? ""),
        name: String(data.get("name") ?? ""),
        description: String(data.get("description") ?? ""),
      });
      projectFeedback = "项目已创建";
      await refresh();
    } catch (error) {
      // 项目创建时会话过期必须清理本地态，避免后续 Key 绑定使用失效权限。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      projectActionError = error;
      projectFeedback = "项目创建失败";
      await refresh();
    }
  });

  const createApplicationForm = document.querySelector("#create-application-form");
  createApplicationForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 应用必须挂到当前租户项目下，真实 project_id 归属由后端校验。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createApplicationForm.querySelector("button[type='submit']");
    const data = new FormData(createApplicationForm);
    button.disabled = true;
    projectActionError = null;
    projectFeedback = "应用创建中";
    try {
      await createApplication({
        controlApiUrl,
        token,
        projectId: String(data.get("projectId") ?? ""),
        applicationCode: String(data.get("applicationCode") ?? ""),
        name: String(data.get("name") ?? ""),
        description: String(data.get("description") ?? ""),
      });
      projectFeedback = "应用已创建";
      await refresh();
    } catch (error) {
      // 应用创建时会话过期必须立即回到登录页，防止项目归属状态继续被旧令牌使用。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      projectActionError = error;
      projectFeedback = "应用创建失败";
      await refresh();
    }
  });

  const createVirtualKeyForm = document.querySelector("#create-virtual-key-form");
  createVirtualKeyForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createVirtualKeyForm.querySelector("button[type='submit']");
    const data = new FormData(createVirtualKeyForm);
    button.disabled = true;
    virtualKeyActionError = null;
    virtualKeyFeedback = "创建中";
    try {
      const result = await createVirtualKey({
        controlApiUrl,
        token,
        name: String(data.get("name") ?? ""),
        environmentId: String(data.get("environmentId") ?? ""),
        environment: undefined,
        description: String(data.get("description") ?? ""),
        projectId: String(data.get("projectId") ?? ""),
        applicationId: String(data.get("applicationId") ?? ""),
      });
      virtualKeyCreatedSecret = {
        plainValue: result.plain_value,
        keyPrefix: result.virtual_key?.key_prefix,
      };
      virtualKeyFeedback = "创建完成";
      await refresh();
    } catch (error) {
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      virtualKeyActionError = error;
      virtualKeyFeedback = "创建失败";
      await refresh();
    }
  });

  for (const virtualKeyForm of document.querySelectorAll("[data-virtual-key-form]")) {
    virtualKeyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = virtualKeyForm.querySelector("button[type='submit']");
      const data = new FormData(virtualKeyForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      virtualKeyActionError = null;
      virtualKeyFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        if (nextStatus === "disabled") {
          await disableVirtualKey({
            controlApiUrl,
            token,
            virtualKeyId: virtualKeyForm.dataset.virtualKeyId,
          });
        } else {
          await updateVirtualKey({
            controlApiUrl,
            token,
            virtualKeyId: virtualKeyForm.dataset.virtualKeyId,
            status: nextStatus,
          });
        }
        virtualKeyFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        virtualKeyActionError = error;
        virtualKeyFeedback = "操作失败";
        await refresh();
      }
    });
  }

  for (const limitForm of document.querySelectorAll("[data-virtual-key-limits-form]")) {
    limitForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = limitForm.querySelector("button[type='submit']");
      const data = new FormData(limitForm);
      button.disabled = true;
      virtualKeyActionError = null;
      virtualKeyFeedback = "保存限制中";
      try {
        await updateVirtualKeyLimits({
          controlApiUrl,
          token,
          virtualKeyId: limitForm.dataset.virtualKeyId,
          rpmLimit: String(data.get("rpmLimit") ?? ""),
          tpmLimit: String(data.get("tpmLimit") ?? ""),
          concurrencyLimit: String(data.get("concurrencyLimit") ?? ""),
          status: "active",
        });
        virtualKeyFeedback = "限制已保存";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        virtualKeyActionError = error;
        virtualKeyFeedback = "限制保存失败";
        await refresh();
      }
    });
  }

  const createProviderForm = document.querySelector("#create-provider-form");
  createProviderForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createProviderForm.querySelector("button[type='submit']");
    const data = new FormData(createProviderForm);
    button.disabled = true;
    providerActionError = null;
    providerFeedback = "创建中";
    providerLastTestResult = null;
    try {
      await createProvider({
        controlApiUrl,
        token,
        providerCode: String(data.get("providerCode") ?? ""),
        name: String(data.get("name") ?? ""),
        providerType: String(data.get("providerType") ?? "openai_compatible"),
        baseUrl: String(data.get("baseUrl") ?? ""),
        authType: String(data.get("authType") ?? "bearer"),
        description: String(data.get("description") ?? ""),
      });
      providerFeedback = "创建完成";
      await refresh();
    } catch (error) {
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      providerActionError = error;
      providerFeedback = "创建失败";
      await refresh();
    }
  });

  for (const providerForm of document.querySelectorAll("[data-provider-status-form]")) {
    providerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = providerForm.querySelector("button[type='submit']");
      const data = new FormData(providerForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      providerActionError = null;
      providerFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        await updateProvider({
          controlApiUrl,
          token,
          providerId: providerForm.dataset.providerId,
          status: nextStatus,
        });
        providerFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        providerActionError = error;
        providerFeedback = "操作失败";
        await refresh();
      }
    });
  }

  for (const providerForm of document.querySelectorAll("[data-provider-test-form]")) {
    providerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = providerForm.querySelector("button[type='submit']");
      button.disabled = true;
      providerActionError = null;
      providerFeedback = "测试中";
      try {
        const result = await testProviderConnection({
          controlApiUrl,
          token,
          providerId: providerForm.dataset.providerId,
          timeoutSeconds: 5,
        });
        providerFeedback = "测试完成";
        providerLastTestResult = {
          providerCode: result.provider?.provider_code,
          status: result.test_result?.status,
          message: result.test_result?.message,
          httpStatus: result.test_result?.http_status,
          latencyMs: result.test_result?.latency_ms,
        };
        await refresh();
      } catch (error) {
        // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        providerActionError = error;
        providerFeedback = "测试失败";
        await refresh();
      }
    });
  }

  for (const providerKeyForm of document.querySelectorAll("[data-provider-key-create-form]")) {
    providerKeyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = providerKeyForm.querySelector("button[type='submit']");
      const data = new FormData(providerKeyForm);
      button.disabled = true;
      providerActionError = null;
      providerFeedback = "Key 创建中";
      try {
        await createProviderKey({
          controlApiUrl,
          token,
          providerId: providerKeyForm.dataset.providerId,
          name: String(data.get("name") ?? ""),
          plainValue: String(data.get("plainValue") ?? ""),
          weight: optionalNumber(data.get("weight"), 100),
        });
        providerFeedback = "Key 创建完成";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        providerActionError = error;
        providerFeedback = "Key 创建失败";
        await refresh();
      }
    });
  }

  for (const providerKeyForm of document.querySelectorAll("[data-provider-key-status-form]")) {
    providerKeyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = providerKeyForm.querySelector("button[type='submit']");
      const data = new FormData(providerKeyForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      providerActionError = null;
      providerFeedback = nextStatus === "disabled" ? "Key 禁用中" : "Key 启用中";
      try {
        await updateProviderKey({
          controlApiUrl,
          token,
          providerKeyId: providerKeyForm.dataset.providerKeyId,
          status: nextStatus,
        });
        providerFeedback = nextStatus === "disabled" ? "Key 已禁用" : "Key 已启用";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        providerActionError = error;
        providerFeedback = "Key 操作失败";
        await refresh();
      }
    });
  }

  const createModelForm = document.querySelector("#create-model-form");
  createModelForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createModelForm.querySelector("button[type='submit']");
    const data = new FormData(createModelForm);
    button.disabled = true;
    modelActionError = null;
    modelFeedback = "创建中";
    try {
      await createModel({
        controlApiUrl,
        token,
        internalModelId: String(data.get("internalModelId") ?? ""),
        providerId: String(data.get("providerId") ?? ""),
        providerModelId: String(data.get("providerModelId") ?? ""),
        displayName: optionalText(data.get("displayName")),
        capabilities: readCapabilities(createModelForm),
        contextWindow: optionalNumber(data.get("contextWindow"), 0),
        inputPricePer1k: optionalNumber(data.get("inputPricePer1k"), 0),
        outputPricePer1k: optionalNumber(data.get("outputPricePer1k"), 0),
        currency: String(data.get("currency") ?? "CNY"),
        priceSchemaVersion: optionalText(data.get("priceSchemaVersion")),
        description: optionalText(data.get("description")),
      });
      modelFeedback = "创建完成";
      await refresh();
    } catch (error) {
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      modelActionError = error;
      modelFeedback = "创建失败";
      await refresh();
    }
  });

  for (const modelForm of document.querySelectorAll("[data-model-status-form]")) {
    modelForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = modelForm.querySelector("button[type='submit']");
      const data = new FormData(modelForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      modelActionError = null;
      modelFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        await updateModel({
          controlApiUrl,
          token,
          modelId: modelForm.dataset.modelId,
          status: nextStatus,
        });
        modelFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        modelActionError = error;
        modelFeedback = "操作失败";
        await refresh();
      }
    });
  }

  const createModelPriceForm = document.querySelector("#create-model-price-form");
  createModelPriceForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 价格写入会改变新请求成本，令牌缺失时必须立即停止并回到登录页。
    if (!token) {
      renderLogin();
      return;
    }
    const data = new FormData(createModelPriceForm);
    const button = createModelPriceForm.querySelector("button[type='submit']");
    button.disabled = true;
    modelPriceActionError = null;
    modelPriceFeedback = "价格计划创建中";
    try {
      await createModelPrice({
        controlApiUrl,
        token,
        modelId: String(data.get("modelId") ?? ""),
        inputPricePer1k: String(data.get("inputPricePer1k") ?? ""),
        outputPricePer1k: String(data.get("outputPricePer1k") ?? ""),
        currency: String(data.get("currency") ?? ""),
        effectiveFrom: toUtcIso(data.get("effectiveFrom")),
        priceSchemaVersion: String(data.get("priceSchemaVersion") ?? ""),
      });
      modelPriceFeedback = "价格计划已创建";
      await refresh();
    } catch (error) {
      // 会话失效必须清除本地 Token；业务错误保留 request_id 回到价格页面展示。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      modelPriceActionError = error;
      modelPriceFeedback = "价格计划创建失败";
      await refresh();
    }
  });

  const modelPriceFilterForm = document.querySelector("#model-price-filter-form");
  modelPriceFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(modelPriceFilterForm);
    modelPriceFilters = {
      modelId: optionalText(data.get("modelId")),
      effectiveStatus: String(data.get("effectiveStatus") ?? "all"),
      limit: 100,
      offset: 0,
    };
    await refresh();
  });

  const modelPriceFilterReset = document.querySelector("#model-price-filter-reset");
  modelPriceFilterReset?.addEventListener("click", async () => {
    modelPriceFilters = { effectiveStatus: "all", limit: 100, offset: 0 };
    modelPriceActionError = null;
    await refresh();
  });

  const createModelAliasForm = document.querySelector("#create-model-alias-form");
  createModelAliasForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const button = createModelAliasForm.querySelector("button[type='submit']");
    const data = new FormData(createModelAliasForm);
    button.disabled = true;
    modelAliasActionError = null;
    modelAliasFeedback = "创建中";
    try {
      await createModelAlias({
        controlApiUrl,
        token,
        alias: String(data.get("alias") ?? ""),
        environmentId: String(data.get("environmentId") ?? ""),
        environment: undefined,
        defaultModelId: String(data.get("defaultModelId") ?? ""),
        fallbackModelId: optionalText(data.get("fallbackModelId")),
        description: optionalText(data.get("description")),
      });
      modelAliasFeedback = "创建完成";
      await refresh();
    } catch (error) {
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      modelAliasActionError = error;
      modelAliasFeedback = "创建失败";
      await refresh();
    }
  });

  for (const modelAliasForm of document.querySelectorAll("[data-model-alias-status-form]")) {
    modelAliasForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (!token) {
        renderLogin();
        return;
      }
      const button = modelAliasForm.querySelector("button[type='submit']");
      const data = new FormData(modelAliasForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      modelAliasActionError = null;
      modelAliasFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        await updateModelAlias({
          controlApiUrl,
          token,
          modelAliasId: modelAliasForm.dataset.modelAliasId,
          status: nextStatus,
          // Alias 启停会改变初始路由图，必须携带当前 RoutingPolicy version 做锁内 CAS。
          expectedRoutingPolicyVersion: Number(modelAliasForm.dataset.routingPolicyVersion),
        });
        modelAliasFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        modelAliasActionError = error;
        modelAliasFeedback = "操作失败";
        await refresh();
      }
    });
  }

  const dismissSecretButton = document.querySelector("[data-dismiss-virtual-key-secret]");
  dismissSecretButton?.addEventListener("click", async () => {
    virtualKeyCreatedSecret = null;
    await refresh();
  });

  const copySecretButton = document.querySelector("[data-copy-virtual-key]");
  copySecretButton?.addEventListener("click", async () => {
    const value = copySecretButton.dataset.copyVirtualKey;
    if (!value || !navigator.clipboard?.writeText) {
      return;
    }
    await navigator.clipboard.writeText(value);
    virtualKeyFeedback = "已复制";
    await refresh();
  });

  const budgetScopeChainKey = document.querySelector("#budget-scope-chain-key");
  budgetScopeChainKey?.addEventListener("change", async () => {
    // 只保存不透明 Key ID；后端 GET 会再次恢复并验证完整归属链。
    selectedBudgetVirtualKeyId = budgetScopeChainKey.value;
    budgetActionError = null;
    budgetFeedback = "";
    await refresh();
  });

  const budgetRuleForm = document.querySelector("#budget-rule-form");
  const budgetScopeTypeControl = budgetRuleForm?.querySelector("[name='scopeType']");
  const budgetScopeIdControl = budgetRuleForm?.querySelector("[name='scopeId']");
  const budgetEnvironmentControl = budgetRuleForm?.querySelector("[name='environmentId']");

  /**
   * 让 Scope ID 选项与所选层级保持一致，避免把 Project ID 误发为 Key scope。
   * 这只是输入约束；后端仍会执行 tenant-qualified 归属校验。
   */
  function syncBudgetScopeOptions() {
    if (!budgetScopeTypeControl || !budgetScopeIdControl) {
      return;
    }
    const scopeType = budgetScopeTypeControl.value;
    const options = [...budgetScopeIdControl.options];
    const optionDescriptors = [];
    for (const option of options) {
      const matches = option.dataset.budgetScopeOption === scopeType;
      option.hidden = !matches;
      option.disabled = !matches;
      optionDescriptors.push({
        scopeType: option.dataset.budgetScopeOption,
        value: option.value,
        environmentId: option.dataset.environmentId,
      });
    }
    const selection = selectBudgetScopeOption({
      scopeType,
      currentScopeId: budgetScopeIdControl.value,
      options: optionDescriptors,
    });
    budgetScopeIdControl.value = selection.scopeId;
    let environmentExists = false;
    if (selection.environmentId && budgetEnvironmentControl) {
      for (const option of budgetEnvironmentControl.options) {
        if (option.value === selection.environmentId) {
          environmentExists = true;
          break;
        }
      }
    }
    if (selection.environmentId && budgetEnvironmentControl && environmentExists) {
      // 自动选中首个 Key 也必须同步 Environment；程序化赋值不会自行触发 change 事件。
      budgetEnvironmentControl.value = selection.environmentId;
    }
  }
  budgetScopeTypeControl?.addEventListener("change", syncBudgetScopeOptions);
  budgetScopeIdControl?.addEventListener("change", syncBudgetScopeOptions);
  syncBudgetScopeOptions();

  budgetRuleForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 预算写入会影响网关阻断策略，必须依赖后端 RBAC；前端只负责提交当前表单字段。
    if (!token) {
      renderLogin();
      return;
    }
    const operation = budgetMutationLifecycle.begin({
      identity: budgetIdentitySnapshot(authSession.context),
      navigation: currentRenderedNavigation(),
      refreshGeneration: refreshSequence,
    });
    if (!operation) {
      // 不完整身份或已离开预算页时失败关闭，不能发送失去 owner 的写请求。
      return;
    }
    const button = budgetRuleForm.querySelector("button[type='submit']");
    const data = new FormData(budgetRuleForm);
    const scopeType = String(data.get("scopeType") ?? "");
    const scopeId = scopeType === "tenant" ? "" : String(data.get("scopeId") ?? "");
    const environmentId = String(data.get("environmentId") ?? "");
    const exists = budgetRuleAlreadyExists(environmentId, scopeType, scopeId);
    const writeRule = exists ? updateBudgetRule : createBudgetRule;
    button.disabled = true;
    budgetActionError = null;
    budgetFeedback = exists ? "预算更新中" : "预算创建中";
    try {
      await writeRule({
        controlApiUrl,
        token,
        scopeType,
        scopeId,
        environmentId,
        name: String(data.get("name") ?? ""),
        dailyBudgetCny: String(data.get("dailyBudgetCny") ?? ""),
        warningThresholdPercent: optionalNumber(data.get("warningThresholdPercent"), 80),
        exhaustedAction: String(data.get("exhaustedAction") ?? "alert_only"),
        status: String(data.get("status") ?? "active"),
      });
      if (!isCurrentBudgetMutation(operation)) {
        budgetMutationLifecycle.finish(operation);
        return;
      }
      budgetFeedback = exists ? "预算已更新" : "预算已创建";
      budgetMutationLifecycle.finish(operation);
      await refresh();
    } catch (error) {
      if (!isCurrentBudgetMutation(operation)) {
        budgetMutationLifecycle.finish(operation);
        return;
      }
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearBudgetSessionState();
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      // 409 必须保留后端 code/message/request_id，并通过 refresh 获取并发创建后的最新规则。
      budgetActionError = error;
      budgetFeedback = error instanceof AuthApiError && error.status === 409 ? "预算规则已变化" : "预算保存失败";
      budgetMutationLifecycle.finish(operation);
      await refresh();
    }
  });
  document.querySelectorAll("[data-budget-mode-rule-id]").forEach((control) => {
    control.addEventListener("change", async () => {
      const token = authSession.token;
      const budgetRuleId = control.dataset.budgetModeRuleId;
      // 快捷切换会直接改变网关准入行为，缺少会话或规则 ID 时不能发送不完整请求。
      if (!token || !budgetRuleId) {
        renderLogin();
        return;
      }
      const operation = budgetMutationLifecycle.begin({
        identity: budgetIdentitySnapshot(authSession.context),
        navigation: currentRenderedNavigation(),
        refreshGeneration: refreshSequence,
      });
      if (!operation) {
        // 页面或身份 owner 不完整时不能改变 Gateway 的预算耗尽动作。
        return;
      }
      control.disabled = true;
      budgetActionError = null;
      budgetFeedback = "预算模式更新中";
      try {
        await updateBudgetMode({
          controlApiUrl,
          token,
          budgetRuleId,
          mode: control.value,
        });
        if (!isCurrentBudgetMutation(operation)) {
          budgetMutationLifecycle.finish(operation);
          return;
        }
        budgetFeedback = control.value === "block" ? "预算耗尽后将阻断请求" : "已切换为仅告警，阻断已解除";
        budgetMutationLifecycle.finish(operation);
        await refresh();
      } catch (error) {
        if (!isCurrentBudgetMutation(operation)) {
          budgetMutationLifecycle.finish(operation);
          return;
        }
        // 认证失效时必须清理本地会话；其他统一错误保留 request_id 回显。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        budgetActionError = error;
        budgetFeedback = "预算模式更新失败";
        budgetMutationLifecycle.finish(operation);
        await refresh();
      }
    });
  });

  const dlpRuleFilters = document.querySelector("#dlp-rule-filters");
  dlpRuleFilters?.addEventListener("change", async () => {
    const data = new FormData(dlpRuleFilters);
    const requestedScope = String(data.get("scanScope") ?? "");
    // DOM 值也不能直接成为治理查询；未知值清空为全部，后端仍会独立执行枚举和权限校验。
    dlpScanScopeFilter = ["input", "output", "both"].includes(requestedScope) ? requestedScope : "";
    await refresh();
  });

  const dlpRuleForm = document.querySelector("#dlp-rule-form");
  dlpRuleForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // DLP 规则会直接影响请求是否进入模型，前端只提交当前表单，admin 权限和 tenant_id 必须由后端校验。
    if (!token) {
      renderLogin();
      return;
    }
    const button = dlpRuleForm.querySelector("button[type='submit']");
    const data = new FormData(dlpRuleForm);
    button.disabled = true;
    dlpActionError = null;
    dlpFeedback = "DLP 规则保存中";
    try {
      await createDlpRule({
        controlApiUrl,
        token,
        name: String(data.get("name") ?? ""),
        ruleType: String(data.get("ruleType") ?? ""),
        pattern: String(data.get("pattern") ?? ""),
        action: String(data.get("action") ?? "log_only"),
        scanScope: String(data.get("scanScope") ?? "input"),
        status: String(data.get("status") ?? "active"),
        priority: optionalNumber(data.get("priority"), 100),
        description: String(data.get("description") ?? ""),
      });
      dlpFeedback = "DLP 规则已保存";
      await refresh();
    } catch (error) {
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      dlpActionError = error;
      dlpFeedback = "DLP 规则保存失败";
      await refresh();
    }
  });

  for (const dlpUpdateForm of document.querySelectorAll("[data-dlp-rule-update]")) {
    dlpUpdateForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 快捷更新只调整方向、动作、状态和优先级；规则归属和字段白名单仍由后端 PATCH 接口限制。
      if (!token) {
        renderLogin();
        return;
      }
      const button = dlpUpdateForm.querySelector("button[type='submit']");
      const data = new FormData(dlpUpdateForm);
      button.disabled = true;
      dlpActionError = null;
      dlpFeedback = "DLP 规则更新中";
      try {
        await updateDlpRule({
          controlApiUrl,
          token,
          ruleId: dlpUpdateForm.dataset.dlpRuleId,
          action: String(data.get("action") ?? ""),
          scanScope: String(data.get("scanScope") ?? "input"),
          status: String(data.get("status") ?? ""),
          priority: optionalNumber(data.get("priority"), 100),
        });
        dlpFeedback = "DLP 规则已更新";
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        dlpActionError = error;
        dlpFeedback = "DLP 规则更新失败";
        await refresh();
      }
    });
  }

  const webhookForm = document.querySelector("#webhook-form");
  webhookForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // Webhook 会把治理事件推送到企业系统，前端只提交表单字段，角色和 tenant_id 必须由后端校验。
    if (!token) {
      renderLogin();
      return;
    }
    const button = webhookForm.querySelector("button[type='submit']");
    const data = new FormData(webhookForm);
    button.disabled = true;
    webhookActionError = null;
    webhookFeedback = "Webhook Endpoint 保存中";
    try {
      const result = await createWebhook({
        controlApiUrl,
        token,
        name: String(data.get("name") ?? ""),
        endpointUrl: String(data.get("endpointUrl") ?? ""),
        eventTypes: data.getAll("eventTypes").map((item) => String(item)),
        timeoutMs: optionalNumber(data.get("timeoutMs"), 5000),
        status: String(data.get("status") ?? "active"),
      });
      webhookCreatedSecret = {
        signingSecret: result.signing_secret,
        webhookEndpointId: result.webhook?.webhook_endpoint_id,
        reason: "created",
      };
      webhookSelectedEndpointId = result.webhook?.webhook_endpoint_id ?? webhookSelectedEndpointId;
      webhookFeedback = { message: "Webhook Endpoint 已保存", requestId: result.request_id };
      await refresh();
    } catch (error) {
      // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      webhookActionError = error;
      webhookFeedback = "Webhook Endpoint 保存失败";
      await refresh();
    }
  });

  for (const testButton of document.querySelectorAll("[data-webhook-test]")) {
    testButton.addEventListener("click", async () => {
      const token = authSession.token;
      // 测试发送必须由后端按 Endpoint 归属和角色再次校验，前端不能只相信按钮可见性。
      if (!token) {
        renderLogin();
        return;
      }
      testButton.disabled = true;
      webhookActionError = null;
      webhookFeedback = "Webhook 测试发送中";
      try {
        webhookLastTestResult = await testWebhook({
          controlApiUrl,
          token,
          webhookEndpointId: testButton.dataset.webhookTest,
        });
        webhookSelectedEndpointId = testButton.dataset.webhookTest;
        webhookFeedback = {
          message:
            webhookLastTestResult.delivery?.status === "success"
              ? "Webhook 测试发送成功"
              : "Webhook 测试发送失败",
          requestId: webhookLastTestResult.request_id,
        };
        await refresh();
      } catch (error) {
        // 认证过期会改变前端会话边界，必须立即清理本地登录态并回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        webhookActionError = error;
        webhookFeedback = "Webhook 测试发送失败";
        await refresh();
      }
    });
  }

  for (const detailsButton of document.querySelectorAll("[data-webhook-details]")) {
    detailsButton.addEventListener("click", async () => {
      // Endpoint 选择只影响当前详情查询；租户归属仍由后端按登录态重新验证。
      webhookSelectedEndpointId = detailsButton.dataset.webhookDetails;
      webhookDeliveryFilters = { eventType: "", status: "all" };
      await refresh();
    });
  }

  const webhookDeliveryFilterForm = document.querySelector("#webhook-delivery-filters");
  webhookDeliveryFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(webhookDeliveryFilterForm);
    webhookSelectedEndpointId = webhookDeliveryFilterForm.dataset.webhookEndpointId;
    webhookDeliveryFilters = {
      eventType: String(data.get("eventType") ?? ""),
      status: String(data.get("status") ?? "all"),
    };
    await refresh();
  });

  const webhookDetailsRefreshButton = document.querySelector("[data-webhook-refresh-details]");
  webhookDetailsRefreshButton?.addEventListener("click", async () => {
    // 刷新只重读当前筛选的 Endpoint 详情，不改变 tenant、筛选或一次性 Secret 状态。
    webhookSelectedEndpointId = webhookDetailsRefreshButton.dataset.webhookRefreshDetails;
    webhookDetailsRefreshButton.disabled = true;
    webhookDetailsRefreshButton.setAttribute("aria-busy", "true");
    await refresh();
  });

  for (const replayButton of document.querySelectorAll("[data-webhook-replay]")) {
    replayButton.addEventListener("click", async () => {
      const token = authSession.token;
      // replay 请求只提交路径 Endpoint、来源 delivery 和本次幂等键，tenant_id/actor/payload 由后端恢复。
      if (!token) {
        renderLogin();
        return;
      }
      replayButton.disabled = true;
      replayButton.setAttribute("aria-busy", "true");
      webhookActionError = null;
      webhookFeedback = "Webhook 重放中";
      webhookSelectedEndpointId = replayButton.dataset.webhookEndpointId;
      try {
        const replayKey = webhookReplayKeyFor(
          replayButton.dataset.webhookEndpointId,
          replayButton.dataset.webhookReplay,
        );
        webhookLastReplayResult = await replayWebhook({
          controlApiUrl,
          token,
          webhookEndpointId: replayButton.dataset.webhookEndpointId,
          webhookDeliveryId: replayButton.dataset.webhookReplay,
          idempotencyKey: replayKey,
        });
        releaseWebhookReplayKey(replayButton.dataset.webhookEndpointId, replayButton.dataset.webhookReplay);
        webhookFeedback = {
          message:
            webhookLastReplayResult.delivery?.status === "success"
              ? "Webhook 重放成功"
              : "Webhook 重放已完成，接收端仍失败",
          requestId: webhookLastReplayResult.request_id,
        };
        await refresh();
      } catch (error) {
        // 重放遇到认证过期必须立即清空会话，不能让旧页面继续展示管理操作入口。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        webhookActionError = error;
        webhookFeedback = "Webhook 重放失败";
        // 4xx 已明确拒绝且未出网，可以释放幂等键；网络错误和 5xx 结果不确定时保留同一键供安全重试。
        if (error instanceof AuthApiError && error.status >= 400 && error.status < 500) {
          releaseWebhookReplayKey(replayButton.dataset.webhookEndpointId, replayButton.dataset.webhookReplay);
        }
        await refresh();
      }
    });
  }

  for (const rotateButton of document.querySelectorAll("[data-webhook-rotate]")) {
    rotateButton.addEventListener("click", async () => {
      const token = authSession.token;
      // Secret 轮换属于敏感管理操作，没有有效 Token 时不能继续展示确认或调用接口。
      if (!token) {
        renderLogin();
        return;
      }
      // 轮换会让旧 Secret 立即失效，必须由用户显式确认；确认框不展示旧密钥或 Endpoint URL。
      if (!globalThis.confirm?.("轮换后旧签名密钥将立即失效，确认继续？")) {
        return;
      }
      rotateButton.disabled = true;
      rotateButton.setAttribute("aria-busy", "true");
      webhookActionError = null;
      webhookFeedback = "Webhook Secret 轮换中";
      webhookSelectedEndpointId = rotateButton.dataset.webhookRotate;
      try {
        const result = await rotateWebhookSecret({
          controlApiUrl,
          token,
          webhookEndpointId: rotateButton.dataset.webhookRotate,
        });
        webhookCreatedSecret = {
          signingSecret: result.signing_secret,
          webhookEndpointId: result.webhook?.webhook_endpoint_id,
          reason: "rotated",
        };
        webhookFeedback = {
          message: `Webhook Secret 已轮换到版本 ${result.webhook?.secret_version ?? "-"}`,
          requestId: result.request_id,
        };
        await refresh();
      } catch (error) {
        // 轮换遇到认证过期必须回到登录页，不能保留可能过时的一次性 Secret 状态。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        webhookActionError = error;
        webhookFeedback = "Webhook Secret 轮换失败";
        await refresh();
      }
    });
  }

  const copyWebhookSecretButton = document.querySelector("[data-copy-webhook-secret]");
  copyWebhookSecretButton?.addEventListener("click", async () => {
    const value = copyWebhookSecretButton.dataset.copyWebhookSecret;
    // signing secret 只在刚创建后临时存在内存和 DOM，复制后不写入本地存储或事件 payload。
    if (!value || !navigator.clipboard?.writeText) {
      webhookActionError = { message: "当前浏览器不支持安全复制，请手工选择密钥", requestId: "-" };
      await refresh();
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      webhookFeedback = "Webhook 签名密钥已复制";
    } catch {
      // Clipboard 权限拒绝不能形成未处理 Promise，也不能把 Secret 附加到错误消息。
      webhookActionError = { message: "签名密钥复制失败，请手工选择", requestId: "-" };
    }
    await refresh();
  });

  const dismissWebhookSecretButton = document.querySelector("[data-dismiss-webhook-secret]");
  dismissWebhookSecretButton?.addEventListener("click", async () => {
    webhookCreatedSecret = null;
    webhookFeedback = "Webhook 签名密钥已隐藏";
    await refresh();
  });


  const requestLogFilterForm = document.querySelector("#request-log-filter-form");
  requestLogFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    // FormData 只读取 PAGE-080 白名单；时间统一转 UTC，Decimal 保持字符串精度。
    configureCurrentRequestLogState(requestLogFiltersFromForm(new FormData(requestLogFilterForm)), null);
    await refresh();
  });

  const requestLogFilterReset = document.querySelector("#request-log-filter-reset");
  requestLogFilterReset?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    configureCurrentRequestLogState({ limit: 20, offset: 0 }, null);
    await refresh();
  });

  const outputSafetyFilterForm = document.querySelector("#output-safety-filter-form");
  outputSafetyFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    // terminalStage 由 PAGE-099 固定，表单或旧内存都不能把安全事件查询降级成普通全量日志。
    configureCurrentRequestLogState(
      outputSafetyFiltersFromForm(new FormData(outputSafetyFilterForm)),
      null,
    );
    await refresh();
  });

  document.querySelector("#output-safety-filter-reset")?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    configureCurrentRequestLogState({ terminalStage: "output_safety", limit: 20, offset: 0 }, null);
    await refresh();
  });

  const savedFilterSelect = document.querySelector("#request-log-saved-filter-select");
  savedFilterSelect?.addEventListener("change", () => {
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect.value);
    // 下拉只选择待操作对象；查询必须由“应用”按钮显式触发，避免键盘浏览选项时意外发送敏感查询。
    const nameInput = document.querySelector("#request-log-saved-filter-name");
    if (nameInput) nameInput.value = savedFilter?.name ?? "";
    for (const buttonId of [
      "request-log-saved-filter-apply",
      "request-log-saved-filter-update",
      "request-log-saved-filter-delete",
    ]) {
      const button = document.querySelector(`#${buttonId}`);
      if (button) button.disabled = !savedFilter;
    }
  });

  document.querySelector("#request-log-saved-filter-apply")?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect?.value);
    if (!savedFilter) return;
    // 应用后恢复表单展示，但 GET 只发送 ID，让后端按当前 tenant + owner 加载权威筛选和排序。
    configureCurrentRequestLogState(requestLogFiltersFromSavedFilter(savedFilter), null);
    await refresh();
  });

  document.querySelector("#request-log-saved-filter-create")?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    const name = optionalText(document.querySelector("#request-log-saved-filter-name")?.value);
    const currentFilters = currentRequestLogFiltersFromForm();
    if (!currentFilters) return;
    await mutateCurrentSavedRequestLogFilter((signal) => createSavedRequestLogFilter({
      controlApiUrl,
      token: authSession.token,
      name,
      filters: currentFilters,
      sortBy: currentFilters.sortBy,
      sortDirection: currentFilters.sortDirection,
      signal,
    }), { activeNavigationItemId: consoleShellState?.activeNavigationItemId });
  });

  document.querySelector("#request-log-saved-filter-update")?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect?.value);
    if (!savedFilter) return;
    const name = optionalText(document.querySelector("#request-log-saved-filter-name")?.value);
    const currentFilters = currentRequestLogFiltersFromForm();
    if (!currentFilters) return;
    await mutateCurrentSavedRequestLogFilter((signal) => updateSavedRequestLogFilter({
      controlApiUrl,
      token: authSession.token,
      savedFilterId: savedFilter.saved_filter_id,
      version: savedFilter.version,
      name,
      filters: currentFilters,
      sortBy: currentFilters.sortBy,
      sortDirection: currentFilters.sortDirection,
      signal,
    }), { activeNavigationItemId: consoleShellState?.activeNavigationItemId });
  });

  document.querySelector("#request-log-saved-filter-delete")?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect?.value);
    if (!savedFilter) return;
    const confirmed = globalThis.window?.confirm?.(`删除保存筛选“${savedFilter.name}”？`) === true;
    if (!confirmed) return;
    await mutateCurrentSavedRequestLogFilter((signal) => deleteSavedRequestLogFilter({
      controlApiUrl,
      token: authSession.token,
      savedFilterId: savedFilter.saved_filter_id,
      version: savedFilter.version,
      signal,
    }), {
      clearSelection: true,
      activeNavigationItemId: consoleShellState?.activeNavigationItemId,
    });
  });

  const requestLogPrev = document.querySelector("#request-log-prev");
  requestLogPrev?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    const currentFilters = requestLogPageMemory.query().filters;
    const nextFilters = {
      ...currentFilters,
      offset: Math.max(Number(currentFilters.offset ?? 0) - Number(currentFilters.limit ?? 20), 0),
    };
    configureCurrentRequestLogState(nextFilters, null);
    await refresh();
  });

  const requestLogNext = document.querySelector("#request-log-next");
  requestLogNext?.addEventListener("click", async () => {
    if (!requestLogInteractionAllowed(consoleShellState)) return;
    const currentFilters = requestLogPageMemory.query().filters;
    const nextFilters = {
      ...currentFilters,
      offset: Number(currentFilters.offset ?? 0) + Number(currentFilters.limit ?? 20),
    };
    configureCurrentRequestLogState(nextFilters, null);
    await refresh();
  });

  if (consoleShellState?.activeNavigationItemId !== "output-safety") {
    for (const button of document.querySelectorAll("[data-request-log-detail]")) {
      button.addEventListener("click", async () => {
        if (!requestLogInteractionAllowed(consoleShellState)) return;
        configureCurrentRequestLogState(
          requestLogPageMemory.query().filters,
          button.dataset.requestLogDetail,
        );
        await refresh();
      });
    }

    const requestLogDetailRefresh = document.querySelector("[data-request-log-detail-refresh]");
    requestLogDetailRefresh?.addEventListener("click", async () => {
      if (!requestLogInteractionAllowed(consoleShellState)) return;
      const requestId = String(requestLogDetailRefresh.dataset.requestLogDetailRefresh ?? "").trim();
      const selectedRequestId = requestLogPageMemory.query().selectedRequestId;
      if (!requestId || requestId !== selectedRequestId) {
        // 伪造或过期的详情按钮不得切换 owner；只允许刷新当前内存已选择的 request_id。
        return;
      }
      // 显式命令只发起一轮现有详情读取，不建立 interval、递归轮询或持久后台任务。
      configureCurrentRequestLogState(requestLogPageMemory.query().filters, selectedRequestId);
      await refresh();
    });
  }

  for (const link of document.querySelectorAll("[data-provider-error-map-jump]")) {
    link.addEventListener("click", async (event) => {
      // hash 只保留固定 #error-maps；真正导航必须先完成 owner 绑定，不能依赖浏览器默认锚点行为。
      event.preventDefault();
      if (!requestLogInteractionAllowed(consoleShellState)) {
        errorMapHandoff.clear();
        return;
      }
      const owner = currentRequestLogOwner();
      const staged = errorMapHandoff.stage({
        identity: owner.identity,
        tenantId: owner.tenantId,
        source: link.dataset.providerErrorMapSource,
        errorMapId: link.dataset.providerErrorMapJump,
        matchedRuleVersion: link.dataset.providerErrorMapVersion,
        builtinCatalogVersion: link.dataset.providerErrorMapCatalog,
      });
      const navigationButton = Array.from(document.querySelectorAll("[data-console-nav]"))
        .find((candidate) => candidate.dataset.consoleNav === "error-maps");
      if (!staged || !navigationButton) {
        errorMapHandoff.clear();
        return;
      }
      await switchConsoleNavigation(navigationButton, consoleShellState);
    });
  }

  for (const button of document.querySelectorAll("[data-request-log-explain]")) {
    button.addEventListener("click", async () => {
      if (!requestLogInteractionAllowed(consoleShellState)) {
        requestExplanationHandoff.clear();
        return;
      }
      const owner = currentRequestLogOwner();
      // handoff 在导航前绑定完整身份；日志页销毁后目标页只能消费一次，失败时不保留源 ID。
      const staged = requestExplanationHandoff.stage({
        identity: owner.identity,
        tenantId: owner.tenantId,
        requestId: button.dataset.requestLogExplain,
        source: "request_logs",
      });
      const navigationButton = Array.from(document.querySelectorAll("[data-console-nav]"))
        .find((candidate) => candidate.dataset.consoleNav === "request-explanations");
      if (!staged || !navigationButton) {
        requestExplanationHandoff.clear();
        return;
      }
      await switchConsoleNavigation(navigationButton, consoleShellState);
    });
  }

  const auditLogFilterForm = document.querySelector("#audit-log-filter-form");
  auditLogFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    // 筛选变化会改变导出快照，必须先 abort 旧操作并清除上一筛选的 hash/行数反馈。
    clearAuditExportSessionState();
    const submitButton = auditLogFilterForm.querySelector("button[type='submit']");
    if (submitButton) {
      // 禁用提交按钮直到刷新完成，阻止快速重复提交把相同敏感查询并发发送多次。
      submitButton.disabled = true;
    }
    const data = new FormData(auditLogFilterForm);
    auditLogFilters = {
      createdFrom: optionalText(data.get("createdFrom")) ? toUtcIso(data.get("createdFrom")) : undefined,
      createdTo: optionalText(data.get("createdTo")) ? toUtcIso(data.get("createdTo")) : undefined,
      action: optionalText(data.get("action")),
      objectType: optionalText(data.get("objectType")),
      actorUserId: optionalText(data.get("actorUserId")),
      objectId: optionalText(data.get("objectId")),
      requestId: optionalText(data.get("requestId")),
      limit: 20,
      offset: 0,
    };
    await refresh();
  });

  const auditLogFilterReset = document.querySelector("#audit-log-filter-reset");
  auditLogFilterReset?.addEventListener("click", async () => {
    auditLogFilterReset.disabled = true;
    clearAuditExportSessionState();
    // 重置必须清除全部筛选并回到第一页，不能保留旧时间范围或旧 offset。
    auditLogFilters = { limit: 20, offset: 0 };
    await refresh();
  });

  const auditLogPrev = document.querySelector("#audit-log-prev");
  auditLogPrev?.addEventListener("click", async () => {
    auditLogPrev.disabled = true;
    auditLogFilters = {
      ...auditLogFilters,
      offset: Math.max(Number(auditLogFilters.offset ?? 0) - Number(auditLogFilters.limit ?? 20), 0),
    };
    await refresh();
  });

  const auditLogNext = document.querySelector("#audit-log-next");
  auditLogNext?.addEventListener("click", async () => {
    auditLogNext.disabled = true;
    auditLogFilters = {
      ...auditLogFilters,
      offset: Number(auditLogFilters.offset ?? 0) + Number(auditLogFilters.limit ?? 20),
    };
    await refresh();
  });

  const auditLogExportButton = document.querySelector("#audit-log-export-csv");
  auditLogExportButton?.addEventListener("click", async () => {
    const token = authSession.token;
    // DOM 门禁只改善体验；真实 audit.export 权限与 tenant_id 仍由后端从 Session 校验。
    if (
      !token
      || !hasPermission(authSession.context, PERMISSIONS.AUDIT_EXPORT)
      || consoleShellState?.activeNavigationItemId !== "audit-logs"
      || currentRenderedNavigation() !== "audit-logs"
    ) {
      // 缺少令牌时主动回到登录页，避免失效 DOM 入口仍表现为可导出；有令牌但角色或导航不符则静默拒绝。
      if (!token) {
        renderLogin();
      }
      return;
    }
    const operation = auditExportLifecycle.begin({
      identity: auditExportIdentitySnapshot(authSession.context),
      navigation: currentRenderedNavigation(),
    });
    if (!operation) {
      // 活动 owner 或不完整身份均失败关闭，重复点击不得产生第二份不可变 AuditExport。
      return;
    }
    // 每次新的人工点击都创建新的 operation ID；筛选中的 request_id 仍单独位于 query。
    const operationRequestId = createAuditExportRequestId();
    let refreshAfterSuccess = false;
    setAuditExportState({ status: "exporting" });
    try {
      const result = await exportAuditLogs({
        controlApiUrl,
        token,
        actorUserId: auditLogFilters.actorUserId,
        action: auditLogFilters.action,
        objectType: auditLogFilters.objectType,
        objectId: auditLogFilters.objectId,
        requestId: auditLogFilters.requestId,
        createdFrom: auditLogFilters.createdFrom,
        createdTo: auditLogFilters.createdTo,
        operationRequestId,
        signal: operation.signal,
      });
      // 离页、换身份或后续 generation 接管后，旧 Blob 必须被丢弃且不能创建 object URL。
      if (!ownsAuditExportOperation(operation)) {
        return;
      }
      triggerAuditCsvDownload(result);
      if (!ownsAuditExportOperation(operation)) {
        return;
      }
      setAuditExportState({
        status: "completed",
        auditExportId: result.auditExportId,
        contentSha256: result.contentSha256,
        rowCount: result.rowCount,
        requestId: result.requestId,
      });
      refreshAfterSuccess = true;
    } catch (error) {
      // Abort 和迟到异常属于预期生命周期，不能覆盖下一身份或下一导航的反馈。
      if (isAbortError(error) || !ownsAuditExportOperation(operation)) {
        return;
      }
      // 当前 owner 的 401 证明会话边界已失效，必须销毁审计反馈与令牌，避免旧租户状态被下一次登录复用。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuditExportSessionState();
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      setAuditExportState({
        status: "error",
        code: safeAuditExportErrorCode(error),
        requestId: safeAuditExportRequestId(error?.requestId, operationRequestId),
      });
    } finally {
      // 只有本 operation 可以释放自己的 owner；clear() 或新边界已经接管时 finish 不产生副作用。
      auditExportLifecycle.finish(operation);
      syncAuditExportDomState();
    }
    if (refreshAfterSuccess) {
      // 成功下载后只执行既有列表刷新，让新 audit.exported 行可见；不会再次调用导出接口。
      await refresh();
    }
  });

  for (const copyButton of document.querySelectorAll("[data-copy-audit-id]")) {
    copyButton.addEventListener("click", async () => {
      const value = copyButton.dataset.copyAuditId;
      // request_id 只复制到系统剪贴板，不写入本地存储、埋点 payload 或页面 URL。
      if (!value || !navigator.clipboard?.writeText) {
        return;
      }
      await navigator.clipboard.writeText(value);
      copyButton.title = "已复制 request_id";
    });
  }

  for (const fallbackForm of document.querySelectorAll("[data-fallback-policy-form]")) {
    fallbackForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // Fallback 策略属于租户管理操作；会话失效时立即回登录页，禁止发送无认证 PATCH。
      if (!token) {
        renderLogin();
        return;
      }
      const button = fallbackForm.querySelector("button[type='submit']");
      const data = new FormData(fallbackForm);
      button.disabled = true;
      modelAliasActionError = null;
      modelAliasFeedback = "Fallback 保存中";
      try {
        await updateFallbackPolicy({
          controlApiUrl,
          token,
          modelAliasId: fallbackForm.dataset.modelAliasId,
          enabled: data.get("enabled") === "on",
          fallbackModelId: optionalText(data.get("fallbackModelId")),
          triggerConditions: data.getAll("triggerCondition").map(String),
          clearFallbackModel: data.get("clearFallbackModel") === "on",
          // Fallback 目标与 PAGE-067 共用 Alias 候选图，不能绕过 RoutingPolicy version。
          expectedRoutingPolicyVersion: Number(fallbackForm.dataset.routingPolicyVersion),
        });
        modelAliasFeedback = "Fallback 已保存";
        await refresh();
      } catch (error) {
        // 登录态失效必须先清除 Token；其他策略错误保留 request_id 在当前页面展示。
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        modelAliasActionError = error;
        modelAliasFeedback = "Fallback 保存失败";
        await refresh();
      }
    });
  }

  const usageFilterForm = document.querySelector("#usage-filter-form");
  usageFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    // 筛选变化会改变导出快照；先取消旧任务轮询，避免旧 CSV 完成后覆盖新筛选页面反馈。
    clearUsageExportSessionState();
    const data = new FormData(usageFilterForm);
    usageFilters = {
      startDate: optionalText(data.get("startDate")),
      endDate: optionalText(data.get("endDate")),
      environmentId: optionalText(data.get("environmentId")),
      modelId: optionalText(data.get("modelId")),
      virtualKeyId: optionalText(data.get("virtualKeyId")),
      projectId: optionalText(data.get("projectId")),
      applicationId: optionalText(data.get("applicationId")),
      providerId: optionalText(data.get("providerId")),
      estimated: optionalText(data.get("estimated")),
      requestId: optionalText(data.get("requestId")),
      limit: 20,
      offset: 0,
    };
    usageActionError = null;
    await refresh();
  });

  const usageFilterReset = document.querySelector("#usage-filter-reset");
  usageFilterReset?.addEventListener("click", async () => {
    clearUsageExportSessionState();
    usageFilters = { limit: 20, offset: 0 };
    usageActionError = null;
    await refresh();
  });

  const usagePrev = document.querySelector("#usage-prev");
  usagePrev?.addEventListener("click", async () => {
    usageFilters = {
      ...usageFilters,
      offset: Math.max(Number(usageFilters.offset ?? 0) - Number(usageFilters.limit ?? 20), 0),
    };
    await refresh();
  });

  const usageNext = document.querySelector("#usage-next");
  usageNext?.addEventListener("click", async () => {
    usageFilters = {
      ...usageFilters,
      offset: Number(usageFilters.offset ?? 0) + Number(usageFilters.limit ?? 20),
    };
    await refresh();
  });

  const usageExportButton = document.querySelector("#usage-export-csv");
  usageExportButton?.addEventListener("click", async () => {
    const token = authSession.token;
    // 前端权限判断只避免误操作；DOM 被篡改时后端仍会校验 billing.usage.export。
    if (!token || !hasPermission(authSession.context, PERMISSIONS.BILLING_USAGE_EXPORT)) {
      if (!token) {
        renderLogin();
      }
      return;
    }
    // controller 存在表示创建、轮询或下载仍在进行，重复点击不能创建第二个财务任务。
    if (usageExportController) {
      return;
    }
    const controller = new AbortController();
    const generation = ++usageExportGeneration;
    usageExportController = controller;
    let activeJobId = usageExportState.jobId ?? null;
    const operationRequestId = usageExportState.operationRequestId ?? createUsageExportRequestId();
    let operationTraceId = usageExportState.traceId ?? createUsageExportTraceId();
    setUsageExportState({
      status: activeJobId ? "pending" : "requesting",
      jobId: activeJobId,
      operationRequestId,
      traceId: operationTraceId,
      requestId: usageExportState.requestId ?? operationRequestId,
      message: activeJobId
        ? `正在继续查询导出任务 ${activeJobId} · request_id: ${usageExportState.requestId ?? operationRequestId}`
        : "正在创建用量 CSV 导出任务",
    });
    try {
      let download = null;
      if (!activeJobId) {
        const initial = await requestUsageCsvExport({
          controlApiUrl,
          token,
          ...usageFilters,
          exportRequestId: operationRequestId,
          exportTraceId: operationTraceId,
          signal: controller.signal,
        });
        // 创建响应返回时可能已经离页或登出，旧代次不得继续启动轮询或下载。
        if (!ownsUsageExportGeneration(generation, controller)) {
          return;
        }
        operationTraceId = initial.traceId && initial.traceId !== "-" ? initial.traceId : operationTraceId;
        if (initial.kind === "pending") {
          activeJobId = initial.usageExportJobId;
          setUsageExportState({
            status: initial.exportJob.status,
            jobId: activeJobId,
            operationRequestId,
            traceId: operationTraceId,
            requestId: initial.requestId,
            message: usageExportProgressMessage(initial.exportJob, initial.requestId),
          });
        } else {
          download = initial;
          setUsageExportState({
            status: "downloading",
            jobId: null,
            operationRequestId,
            traceId: operationTraceId,
            requestId: initial.requestId,
            message: `用量 CSV 已生成，正在下载 · request_id: ${initial.requestId}`,
          });
        }
      }
      if (activeJobId) {
        download = await pollUsageExportUntilReady({
          controlApiUrl,
          token,
          usageExportJobId: activeJobId,
          traceId: operationTraceId,
          signal: controller.signal,
          onStatus(result) {
            // 状态回调可能与导航切换竞态，只有当前 controller 才能更新 aria-live 反馈。
            if (!ownsUsageExportGeneration(generation, controller)) {
              return;
            }
            const completed = result.exportJob.status === "completed";
            setUsageExportState({
              status: completed ? "downloading" : result.exportJob.status,
              jobId: result.usageExportJobId,
              operationRequestId,
              traceId: operationTraceId,
              requestId: result.requestId,
              message: completed
                ? `导出任务 ${result.usageExportJobId} 已完成，正在下载 · request_id: ${result.requestId}`
                : usageExportProgressMessage(result.exportJob, result.requestId),
            });
          },
        });
      }
      // Blob 到达后再次校验代次，避免离页瞬间仍触发浏览器保存窗口。
      if (!ownsUsageExportGeneration(generation, controller)) {
        return;
      }
      triggerUsageCsvDownload(download);
      setUsageExportState({
        status: "completed",
        jobId: null,
        operationRequestId: null,
        traceId: null,
        requestId: download.requestId,
        filename: download.filename,
        message: `已下载 ${download.filename} · request_id: ${download.requestId}`,
      });
    } catch (error) {
      // 离页、登出或身份变化触发的 Abort 是预期生命周期，不得显示成旧会话错误。
      if (isAbortError(error) || !ownsUsageExportGeneration(generation, controller)) {
        return;
      }
      // 401 表示会话边界已经失效，必须清空 Token 和导出状态后回到登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      setUsageExportState({
        status: "error",
        jobId: activeJobId,
        operationRequestId: shouldRetainUsageExportOperation(error, activeJobId) ? operationRequestId : null,
        traceId: shouldRetainUsageExportOperation(error, activeJobId) ? operationTraceId : null,
        code: error?.code ?? "usage.export_unavailable",
        requestId: error?.requestId ?? "-",
        message: `${authErrorMessage(error)} · code: ${error?.code ?? "usage.export_unavailable"} · request_id: ${error?.requestId ?? "-"}${activeJobId ? ` · job_id: ${activeJobId}` : ""}`,
      });
    } finally {
      // 旧代次的 finally 不能清掉新会话 controller；只有所有权仍匹配时才释放按钮。
      if (ownsUsageExportGeneration(generation, controller)) {
        usageExportController = null;
        syncUsageExportDomState();
      }
    }
  });

  bindFinOpsInteractions(consoleShellState);
  bindCostAnomalyPageInteractions(consoleShellState);
  bindSessionRiskPageInteractions(consoleShellState);
  bindFilesPageInteractions(consoleShellState);
  bindWebhookDeadLetterPageInteractions(consoleShellState);
  scheduleWebhookDeadLetterPoll(consoleShellState);
  bindDebugConsoleEvents();

  const gatewayDebugForm = document.querySelector("#gateway-debug-form");
  gatewayDebugForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(gatewayDebugForm);
    const model = String(data.get("model") ?? "");
    const message = String(data.get("message") ?? "");
    const stream = data.get("stream") === "on";
    // Virtual Key 只保存在当前局部变量直到 fetch 构造请求；读取后立即清空输入框，禁止进入页面状态。
    const virtualKey = String(data.get("virtualKey") ?? "");
    const virtualKeyInput = gatewayDebugForm.elements.namedItem("virtualKey");
    // namedItem 在浏览器中可能返回 RadioNodeList；只有具备 value 属性的单一输入节点才执行清空。
    if (virtualKeyInput && "value" in virtualKeyInput) {
      virtualKeyInput.value = "";
    }
    // begin() 会先 abort 上一次请求并分配新代次，连续提交不会并存两个隐式流连接。
    const { controller, generation } = gatewayDebugLifecycle.begin();
    gatewayDebugState = {
      model,
      message,
      stream,
      running: true,
      feedback: stream ? "等待首个分片" : "请求中",
      result: {
        mode: stream ? "stream" : "json",
        status: stream ? "waiting" : "processing",
        requestId: "-",
        traceId: "-",
        preview: "",
        previewTruncated: false,
        chunkCount: 0,
        byteCount: 0,
      },
    };
    updateGatewayDebugDom(gatewayDebugState);
    try {
      const result = await sendChatCompletion({
        gatewayUrl,
        virtualKey,
        model,
        message,
        stream,
        signal: controller.signal,
        onStreamUpdate: (snapshot) => {
          // 页面切换或新提交后，旧代次即使因调度竞态返回一块数据也不得覆盖当前结果。
          if (!gatewayDebugLifecycle.isCurrent(generation)) {
            return;
          }
          gatewayDebugState = {
            model,
            message,
            stream,
            running: true,
            feedback: snapshot.chunkCount === 0 ? "等待首个分片" : "接收中",
            result: snapshot,
          };
          updateGatewayDebugDom(gatewayDebugState);
        },
      });
      // 仅当前代次可以提交完成态；较早请求被替换后返回的结果直接丢弃。
      if (!gatewayDebugLifecycle.isCurrent(generation)) {
        return;
      }
      gatewayDebugState = {
        model,
        message,
        stream,
        running: false,
        feedback: "请求完成",
        result: { ...result, status: "completed" },
      };
      updateGatewayDebugDom(gatewayDebugState);
    } catch (error) {
      // 新提交或页面离开造成的旧请求终止不能回写“已停止”覆盖新页面状态。
      if (!gatewayDebugLifecycle.isCurrent(generation)) {
        return;
      }
      // 用户主动 Abort 是正常生命周期，不应显示 Provider 超时或网络故障。
      if (error?.name === "AbortError") {
        gatewayDebugState = {
          ...gatewayDebugState,
          model,
          message,
          stream,
          running: false,
          feedback: "已停止",
          result: {
            ...(gatewayDebugState.result ?? {}),
            status: "stopped",
          },
          error: null,
        };
        updateGatewayDebugDom(gatewayDebugState);
        return;
      }
      gatewayDebugState = {
        model,
        message,
        stream,
        running: false,
        feedback: "请求失败",
        error:
          error instanceof GatewayApiError
            ? error
            : {
                code: "gateway.request_failed",
                message: "无法连接网关数据面。",
                requestId: "-",
                traceId: "-",
                suggestion: "检查 Gateway Data Plane 是否启动。",
                diagnosis: null,
              },
      };
      updateGatewayDebugDom(gatewayDebugState);
    } finally {
      // finish 只清理同代 controller，旧请求的 finally 不能清掉新请求正在使用的控制器。
      gatewayDebugLifecycle.finish(generation);
    }
  });

  const gatewayDebugStopButton = document.querySelector("#gateway-debug-stop-button");
  gatewayDebugStopButton?.addEventListener("click", () => {
    // stop 不递增代次，让当前请求的 AbortError catch 可以确认并稳定写入“已停止”状态。
    const stoppedGeneration = gatewayDebugLifecycle.stop();
    if (stoppedGeneration === null) {
      return;
    }
    gatewayDebugState = {
      ...gatewayDebugState,
      running: false,
      feedback: "已停止",
      result: {
        ...(gatewayDebugState.result ?? {}),
        status: "stopped",
      },
      error: null,
    };
    updateGatewayDebugDom(gatewayDebugState);
  });
}

/**
 * 创建 Gateway 调试流生命周期管理器。
 *
 * 每次 begin 都先取消旧 controller 并递增 generation；leave 还会使旧异步回调失效；stop 只取消
 * 当前请求而保留 generation，便于同一请求把 AbortError 映射成用户可见的“已停止”。
 */
export function createGatewayDebugStreamLifecycle() {
  let activeController = null;
  let generation = 0;

  return {
    /** 开始新请求，先取消旧请求，再返回本代专属 AbortController 和代次编号。 */
    begin() {
      activeController?.abort();
      generation += 1;
      activeController = new AbortController();
      return { controller: activeController, generation };
    },
    /** 主动停止当前请求；返回 null 表示当前没有活动连接。 */
    stop() {
      if (!activeController) {
        return null;
      }
      activeController.abort();
      activeController = null;
      return generation;
    },
    /** 页面或登录态离开当前上下文时取消连接并递增代次，使所有旧回调立即失效。 */
    leave() {
      activeController?.abort();
      activeController = null;
      generation += 1;
    },
    /** 判断异步回调是否仍属于当前请求，所有状态写入前都必须通过该检查。 */
    isCurrent(candidateGeneration) {
      return candidateGeneration === generation;
    },
    /** 请求结束后只清理同代 controller，防止旧 finally 清除连续提交创建的新 controller。 */
    finish(candidateGeneration) {
      if (candidateGeneration === generation) {
        activeController = null;
      }
    },
  };
}

/**
 * 使用稳定 DOM 节点实时刷新调试台，不重建表单，也不把响应文本写入 innerHTML。
 * 关键边界：模型输出、错误文案和链路字段只赋给 textContent，Virtual Key 不在 state 中出现。
 */
function updateGatewayDebugDom(state) {
  const result = state.result ?? streamResultFromGatewayError(state.error);
  const hasResult = Boolean(result || state.error || state.running);
  const status = state.error ? "failed" : result?.status ?? (state.running ? "waiting" : "idle");
  const submitButton = document.querySelector("#gateway-debug-form button[type='submit']");
  const stopButton = document.querySelector("#gateway-debug-stop-button");
  const emptyState = document.querySelector("#gateway-debug-empty");
  const resultPanel = document.querySelector("#gateway-debug-result");
  const errorPanel = document.querySelector("#gateway-debug-error");
  const truncatedNotice = document.querySelector("#gateway-debug-truncated");

  // 发送按钮在流运行时保持可提交，以便用户连续提交；第二次提交由生命周期管理器先取消旧流。
  if (submitButton) {
    submitButton.disabled = false;
    submitButton.textContent = state.running ? "重新发送" : "发送";
  }
  if (stopButton) {
    stopButton.hidden = !(state.running && state.stream);
  }
  if (emptyState) {
    emptyState.hidden = hasResult;
  }
  if (resultPanel) {
    resultPanel.hidden = !hasResult;
  }
  // 错误容器只按当前安全错误对象控制可见性，不能把模型输出拼入 innerHTML。
  if (errorPanel) {
    errorPanel.hidden = !state.error;
  }
  if (truncatedNotice) {
    truncatedNotice.hidden = !result?.previewTruncated;
  }

  setGatewayDebugText("#gateway-debug-feedback", state.feedback ?? "");
  setGatewayDebugText("#gateway-debug-status", gatewayDebugStatusLabel(status));
  setGatewayDebugText("#gateway-debug-chunk-count", result?.chunkCount ?? 0);
  setGatewayDebugText("#gateway-debug-byte-count", result?.byteCount ?? 0);
  setGatewayDebugText("#gateway-debug-request-id", result?.requestId ?? state.error?.requestId ?? "-");
  setGatewayDebugText("#gateway-debug-trace-id", result?.traceId ?? state.error?.traceId ?? "-");
  setGatewayDebugText("#gateway-debug-preview", result?.preview ?? "");
  setGatewayDebugText("#gateway-debug-error-code", state.error?.code ?? "gateway.request_failed");
  setGatewayDebugText("#gateway-debug-error-message", state.error?.message ?? "网关请求失败");
  setGatewayDebugText("#gateway-debug-error-suggestion", state.error?.suggestion ?? "");
}

/**
 * 从 GatewayApiError 提取渲染所需的有界流快照，不复制完整错误对象或任何请求认证字段。
 */
function streamResultFromGatewayError(error) {
  if (!error) {
    return null;
  }
  return {
    mode: "stream",
    status: "failed",
    requestId: error.requestId ?? "-",
    traceId: error.traceId ?? "-",
    preview: error.preview ?? "",
    previewTruncated: Boolean(error.previewTruncated),
    chunkCount: error.chunkCount ?? 0,
    byteCount: error.byteCount ?? 0,
  };
}

/**
 * 把内部状态映射为调试台 aria-live 使用的中文状态，未知状态统一显示为“处理中”。
 */
function gatewayDebugStatusLabel(status) {
  const labels = {
    idle: "等待发送",
    waiting: "等待首个分片",
    processing: "请求中",
    streaming: "接收中",
    completed: "已完成",
    failed: "失败",
    stopped: "已停止",
  };
  return labels[status] ?? "处理中";
}

/**
 * 对单个调试台节点执行安全文本写入；节点不存在时直接返回，兼容导航切换和旧页面结构。
 */
function setGatewayDebugText(selector, value) {
  const element = document.querySelector(selector);
  if (!element) {
    return;
  }
  element.textContent = String(value ?? "");
}

/**
 * 执行 控制台主应用 的 `authErrorMessage` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
/**
 * 绑定 PAGE-077/078 的筛选、分页、详情和治理命令。
 *
 * 页面状态只接收当前 tenant-qualified GET 快照中的异常 ID；DOM 属性只是命令载体，
 * tenant、actor、状态迁移、处置期限和有效限流值仍由后端恢复并校验。
 */
function bindCostAnomalyPageInteractions(consoleShellState) {
  bindCostAnomalyInteractions({
    /** 新筛选会使签名 cursor、详情和未提交动作草稿同时失效。 */
    async onFilter(filters) {
      costAnomalyPageState.setFilters(filters);
      await refresh();
    },
    /** 清空筛选并回到第一页，不从 URL 或 DOM 恢复旧 cursor。 */
    async onResetFilters() {
      costAnomalyPageState.resetFilters();
      await refresh();
    },
    /** 上一页只能从闭包内 cursor 栈恢复。 */
    async onPreviousPage() {
      if (costAnomalyPageState.previousPage()) {
        await refresh();
      }
    },
    /** 下一页只能使用刚由后端签发并保存于闭包内的 cursor。 */
    async onNextPage() {
      if (costAnomalyPageState.nextPage()) {
        await refresh();
      }
    },
    /** 详情 ID 必须属于当前页，手工注入页外 ID 会在状态机内失败关闭。 */
    async onSelectAnomaly(anomalyId) {
      if (costAnomalyPageState.selectAnomaly(anomalyId)) {
        await refresh();
      }
    },
    /** 关闭详情会同时销毁其中尚未提交的治理草稿。 */
    async onCloseDetail() {
      costAnomalyPageState.closeDetail();
      await refresh();
    },
    /**
     * 每次显式打开动作时创建独立幂等意图；网络结果不确定后的重试会由状态机复用原键，
     * 幂等键和 operation IDs 不会出现在 DOM、URL、Storage 或日志。
     */
    async onOpenAction({ anomalyId, action }) {
      if (!hasPermission(authSession.context, PERMISSIONS.BILLING_ANOMALY_WRITE)) {
        return;
      }
      const opened = costAnomalyPageState.openAction({
        anomalyId,
        action,
        idempotencyKey: createCostAnomalyIdempotencyKey(),
        operationRequestId: createCostAnomalyRequestId("action"),
        operationTraceId: createCostAnomalyTraceId(),
      });
      if (opened) {
        await refresh();
      }
    },
    /** 取消动作立即丢弃原因、限流草稿和全部短生命周期标识。 */
    async onCloseAction() {
      costAnomalyPageState.closeAction();
      await refresh();
    },
    /** 写操作使用独立 owner，列表刷新不能取消结果未知的 PATCH。 */
    async onSubmitAction(values) {
      await submitCostAnomalyAction(values, consoleShellState);
    },
    /**
     * 代表 request_id 跳转前先清空异常页事实；Request Logs 会按当前 Session tenant
     * 再次查询，前端跳转不能绕过日志接口的租户和角色校验。
     */
    async onOpenRequest(requestId) {
      const normalizedRequestId = String(requestId ?? "").trim();
      const navigationButton = Array.from(document.querySelectorAll("[data-console-nav]"))
        .find((button) => button.dataset.consoleNav === "request-logs");
      if (!normalizedRequestId || !navigationButton) {
        return;
      }
      configureCurrentRequestLogState(
        { requestId: normalizedRequestId, limit: 20, offset: 0 },
        normalizedRequestId,
      );
      costAnomalyPageState.clear();
      await switchConsoleNavigation(navigationButton, consoleShellState);
    },
  });
}

/**
 * 提交当前成本异常治理动作。
 *
 * PATCH owner 绑定 tenant、用户、角色和 Session；离页、登出、换租户或降权会先清理 owner，
 * 因而迟到的成功、401 或失败都不能覆盖新身份页面。后端仍负责最终 RBAC、租户隔离、
 * 状态机、幂等冲突和临时限流是否真正收紧的权威判断。
 */
async function submitCostAnomalyAction(values, consoleShellState) {
  const token = authSession.token;
  const owner = currentCostAnomalyActionOwner(consoleShellState);
  // 写请求必须同时拥有当前 Token 和完整 admin 页面 owner，任一缺失都先销毁旧租户事实。
  if (!token || !owner) {
    costAnomalyPageState.clear();
    // 只有认证令牌缺失才回到登录页；单纯离页时保持当前已授权页面。
    if (!token) {
      renderLogin();
    }
    return;
  }
  const payload = costAnomalyPageState.beginAction(values);
  if (!payload) {
    return;
  }
  const operation = costAnomalyPageState.beginActionOperation(owner);
  if (!operation) {
    costAnomalyPageState.clear();
    return;
  }
  setCostAnomalySubmittingDom();

  try {
    const result = await applyCostAnomalyAction({
      controlApiUrl,
      token,
      ...payload,
      signal: operation.controller.signal,
    });
    const currentOwner = currentCostAnomalyActionOwner(consoleShellState);
    if (!costAnomalyPageState.isActionOperationCurrent(operation, currentOwner)) {
      return;
    }
    costAnomalyPageState.acceptAction(result);
    await refresh();
  } catch (error) {
    const currentOwner = currentCostAnomalyActionOwner(consoleShellState);
    if (!costAnomalyPageState.isActionOperationCurrent(operation, currentOwner)) {
      return;
    }
    // 会话失效先销毁异常证据、幂等键和认证上下文，不能继续展示上一租户页面。
    if (error instanceof AuthApiError && error.status === 401) {
      costAnomalyPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    const retainIdempotencyKey = shouldRetainCostAnomalyIdempotencyKey(error);
    costAnomalyPageState.rejectAction(
      {
        code: error?.code ?? "cost_anomaly.unavailable",
        message: authErrorMessage(error),
        requestId: error?.requestId ?? "-",
      },
      {
        retainIdempotencyKey,
        replacementIds: retainIdempotencyKey
          ? undefined
          : {
              idempotencyKey: createCostAnomalyIdempotencyKey(),
              operationRequestId: createCostAnomalyRequestId("action"),
              operationTraceId: createCostAnomalyTraceId(),
            },
      },
    );
    await refresh();
  } finally {
    costAnomalyPageState.finishActionOperation(operation);
  }
}

/**
 * 构造当前成本异常 PATCH owner。
 * 后端导航快照、当前 hash 和 admin 身份任一不匹配都返回 null，避免只凭可篡改 DOM 发起写入。
 */
function currentCostAnomalyActionOwner(consoleShellState) {
  const hashNavigation = readActiveNavigationHash();
  const identity = costAnomalyIdentitySnapshot(authSession.context);
  if (
    !hasPermission(authSession.context, PERMISSIONS.BILLING_ANOMALY_WRITE)
    || !identity
    || consoleShellState?.activeNavigationItemId !== "cost-anomalies"
    || (hashNavigation && hashNavigation !== "cost-anomalies")
  ) {
    return null;
  }
  return { identity, navigation: "cost-anomalies" };
}

/**
 * 在 PATCH 发出后立即锁定对话框，堵住异步全页重绘前的双击窗口。
 * exactly-once 仍依赖后端 tenant + actor + 幂等键约束，本地禁用只改善交互。
 */
function setCostAnomalySubmittingDom() {
  const form = document.querySelector("#cost-anomaly-action-form");
  form?.setAttribute("aria-busy", "true");
  for (const control of form?.querySelectorAll("button, input, select") ?? []) {
    control.disabled = true;
  }
}

/**
 * 绑定 PAGE-098 筛选、分页、详情、固定处置和 Request Logs 跳转。
 * DOM 只承载当前页命令；tenant、actor、revision、target 与幂等意图均由可信 owner 和闭包恢复。
 */
function bindSessionRiskPageInteractions(consoleShellState) {
  const readOwner = currentSessionRiskReadOwner(consoleShellState);
  // 未获得后端 read 导航或 owner 已变化时不绑定旧 DOM 命令。
  if (!readOwner || !sessionRiskPageState.enter(readOwner)) return;
  bindSessionRiskInteractions({
    /** 筛选变化会销毁 cursor、详情、Dialog 和未决请求，再读取后端签名快照。 */
    async onFilter(filters) {
      sessionRiskPageState.setFilters(filters);
      await refresh();
    },
    /** 清空筛选并回到后端默认 high_risk,frozen 首屏。 */
    async onResetFilters() {
      sessionRiskPageState.resetFilters();
      await refresh();
    },
    /** 上一页只从闭包 cursor 栈恢复。 */
    async onPreviousPage() {
      // 无上一页时保持现有 DOM，避免无意义刷新改变 owner 代次。
      if (sessionRiskPageState.previousPage()) await refresh();
    },
    /** 下一页只使用刚由 Control API 签发的 opaque cursor。 */
    async onNextPage() {
      // cursor 不存在时不发请求，完整 cursor 永远不经过 DOM 参数。
      if (sessionRiskPageState.nextPage()) await refresh();
    },
    /** 详情 ID 必须属于当前 tenant-qualified 页。 */
    async onSelectRisk(sessionRiskScoreId) {
      // 状态机拒绝页外 ID 后不刷新，避免攻击者用 DOM 值探测其他租户资源。
      if (sessionRiskPageState.selectRisk(sessionRiskScoreId)) await refresh();
    },
    /** 关闭详情会同步丢弃未提交动作意图。 */
    async onCloseDetail() {
      sessionRiskPageState.closeDetail();
      await refresh();
    },
    /**
     * 只有实时 read+manage owner 才能创建 Dialog 与内存幂等键；后端仍会在正文前和事务内复核权限。
     */
    async onOpenAction({ sessionRiskScoreId, action }) {
      if (!currentSessionRiskManageOwner(consoleShellState)) return;
      const opened = sessionRiskPageState.openAction({
        sessionRiskScoreId,
        action,
        idempotencyKey: createSessionRiskIdempotencyKey(),
        operationRequestId: createSessionRiskRequestId("action"),
        operationTraceId: createSessionRiskTraceId(),
      });
      if (opened) await refresh();
    },
    /** 取消 Dialog 立即销毁原因、revision、幂等键和 operation IDs。 */
    async onCloseAction() {
      sessionRiskPageState.closeAction();
      await refresh();
    },
    /** POST 使用独立双权限 owner；后台 GET 不能接管结果未知的治理写入。 */
    async onSubmitAction(values) {
      await submitSessionRiskAction(values, consoleShellState);
    },
    /**
     * request_id 跳转前销毁 PAGE-098 状态；Request Logs 会按当前 Session 权限与 tenant 再次查询。
     */
    async onOpenRequest(requestId) {
      const normalizedRequestId = String(requestId ?? "").trim();
      const navigationButton = Array.from(document.querySelectorAll("[data-console-nav]"))
        .find((button) => button.dataset.consoleNav === "request-logs");
      if (!normalizedRequestId || !navigationButton) return;
      configureCurrentRequestLogState(
        { requestId: normalizedRequestId, limit: 20, offset: 0 },
        normalizedRequestId,
      );
      sessionRiskPageState.clear();
      await switchConsoleNavigation(navigationButton, consoleShellState);
    },
  });
}

/**
 * 提交一次 PAGE-098 固定风险处置。
 * POST owner 同时绑定 tenant、用户、Session、权限 revision/fingerprint 和后端导航；前端按钮不构成授权。
 */
async function submitSessionRiskAction(values, consoleShellState) {
  const token = authSession.token;
  const owner = currentSessionRiskManageOwner(consoleShellState);
  // Token 或双权限 owner 任一缺失都销毁动作闭包，不能仅靠按钮隐藏阻止 POST。
  if (!token || !owner) {
    sessionRiskPageState.clear();
    // 只有 Token 缺失才切回登录；撤权但仍登录由下一轮后端导航决定页面。
    if (!token) renderLogin();
    return;
  }
  const payload = sessionRiskPageState.beginAction(values);
  if (!payload) return;
  const operation = sessionRiskPageState.beginActionOperation(owner);
  if (!operation) {
    sessionRiskPageState.clear();
    return;
  }
  setSessionRiskSubmittingDom();
  try {
    const result = await applySessionRiskAction({
      controlApiUrl,
      token,
      ...payload,
      signal: operation.controller.signal,
    });
    const currentOwner = currentSessionRiskManageOwner(consoleShellState);
    // POST 返回后先复核 owner/代次，再接受可能已经演进到后续 revision 的幂等结果。
    if (!sessionRiskPageState.isActionOperationCurrent(operation, currentOwner)) return;
    if (!sessionRiskPageState.acceptAction(operation, result, currentOwner)) return;
    await refresh();
  } catch (error) {
    const currentOwner = currentSessionRiskManageOwner(consoleShellState);
    // 离页、换租户或撤权后的迟到异常不得覆盖新页面或触发认证跳转。
    if (!sessionRiskPageState.isActionOperationCurrent(operation, currentOwner)) return;
    // 401 是认证边界变化，必须清理全部 PAGE-098 DTO 与私有幂等意图。
    if (error instanceof AuthApiError && error.status === 401) {
      sessionRiskPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 403 是授权边界变化，保留登录态但销毁旧权限下读取的风险事实。
    if (error instanceof AuthApiError && error.status === 403) {
      // 撤权响应必须销毁旧 DTO 和幂等意图，再通过 refresh 重读 /auth/me 与后端导航。
      sessionRiskPageState.clear();
      await refresh();
      return;
    }
    const retainIdempotencyKey = shouldRetainSessionRiskIdempotencyKey(error);
    sessionRiskPageState.rejectAction(operation, error, currentOwner, {
      retainIdempotencyKey,
      replacementIds: retainIdempotencyKey
        ? undefined
        : {
            idempotencyKey: createSessionRiskIdempotencyKey(),
            operationRequestId: createSessionRiskRequestId("action"),
            operationTraceId: createSessionRiskTraceId(),
        },
    });
    if (retainIdempotencyKey) {
      // 断网/5xx 可能发生在提交之后：不发 GET 接管未知结果，只用 textContent 更新当前 Dialog 并保留原键重试。
      renderSessionRiskActionErrorDom(sessionRiskPageState.snapshot().actionDialog?.error);
      return;
    }
    // 明确 4xx 已给出否定结论；重新读取当前 revision，用户修正意图后使用状态机生成的新键。
    await refresh();
  } finally {
    sessionRiskPageState.finishActionOperation(operation);
  }
}

/** 构造当前 PAGE-098 read owner，并额外绑定浏览器 hash，避免旧 DOM 在离页后发起 GET。 */
function currentSessionRiskReadOwner(consoleShellState) {
  const owner = sessionRiskPageOwner(authSession.context, consoleShellState);
  const hashNavigation = readActiveNavigationHash();
  // 后端导航与浏览器 hash 必须同时仍指向 PAGE-098，离页后不允许后台 GET。
  if (!owner || (hashNavigation && hashNavigation !== "session-risks")) return null;
  return owner;
}

/** 构造当前 PAGE-098 read+manage owner；manage-only 或 read-only 都固定返回 null。 */
function currentSessionRiskManageOwner(consoleShellState) {
  const owner = sessionRiskPageOwner(
    authSession.context,
    consoleShellState,
    { requireManage: true },
  );
  const hashNavigation = readActiveNavigationHash();
  // manage owner 还必须通过状态机的 read+manage 双权限检查，manage-only 固定失败。
  if (!owner || (hashNavigation && hashNavigation !== "session-risks")) return null;
  return owner;
}

/** POST 发出后立即锁定 Dialog，堵住全页重绘前的双击窗口；exactly-once 仍依赖后端幂等。 */
function setSessionRiskSubmittingDom() {
  const form = document.querySelector("#session-risk-action-form");
  form?.setAttribute("aria-busy", "true");
  for (const control of form?.querySelectorAll("button, select") ?? []) {
    control.disabled = true;
  }
}

/**
 * 在不重建页面的前提下展示结果未知错误并恢复 Dialog 控件。
 * 节点全部使用 textContent 创建，固定错误中不会混入响应正文、幂等键、Header 或 Prompt。
 */
function renderSessionRiskActionErrorDom(error) {
  const form = document.querySelector("#session-risk-action-form");
  // Dialog 已关闭或没有清洗后的错误时不重建节点，避免迟到请求复活旧意图。
  if (!form || !error) return;
  form.removeAttribute("aria-busy");
  for (const control of form.querySelectorAll("button, select")) control.disabled = false;
  let container = form.querySelector(".session-risk-error");
  if (!container) {
    container = document.createElement("div");
    container.className = "session-risk-error";
    container.setAttribute("role", "alert");
    const heading = form.querySelector(".session-risk-dialog-heading");
    heading?.insertAdjacentElement("afterend", container);
  }
  container.replaceChildren();
  const code = document.createElement("strong");
  code.textContent = String(error.code ?? "session_risk.unavailable");
  const message = document.createElement("span");
  message.textContent = String(error.message ?? "风险处置提交失败");
  const requestId = document.createElement("code");
  requestId.textContent = `request_id: ${String(error.requestId ?? "-")}`;
  container.append(code, message, requestId);
}

/** 构造当前文件 read owner，并绑定浏览器 hash，避免旧 DOM 在离页后发起 GET。 */
function currentFileReadOwner(consoleShellState) {
  const owner = filePageOwner(authSession.context, consoleShellState);
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "files")) return null;
  return owner;
}

/** 构造当前文件 read+write owner；只读身份或后端导航漂移固定返回 null。 */
function currentFileWriteOwner(consoleShellState) {
  const owner = filePageOwner(authSession.context, consoleShellState, { requireWrite: true });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "files")) return null;
  return owner;
}

/** 绑定文件上传、筛选和私有 cursor 翻页；按钮可见性不替代每次 owner 与后端 RBAC 校验。 */
function bindFilesPageInteractions(consoleShellState) {
  const readOwner = currentFileReadOwner(consoleShellState);
  if (!readOwner || !filePageState.enter(readOwner)) return;
  bindFileInteractions({
    /** 状态筛选变化会销毁旧 cursor 和在途 GET，再读取 tenant-qualified 首屏。 */
    async onFilter(status) {
      filePageState.setStatus(status);
      await refresh();
    },
    /** 清空筛选并回到全部状态首屏。 */
    async onResetFilter() {
      filePageState.resetStatus();
      await refresh();
    },
    /** 上一页 cursor 只从私有栈恢复。 */
    async onPreviousPage() {
      if (filePageState.previousPage()) await refresh();
    },
    /** 下一页只使用最近一次 Control API 返回的签名 cursor。 */
    async onNextPage() {
      if (filePageState.nextPage()) await refresh();
    },
    /** 原生 File 仅在该调用栈内传给上传客户端，不写入状态机或日志。 */
    async onUpload(file) {
      await submitFileUpload(file, consoleShellState);
    },
  });
}

/**
 * 提交一次文件上传并同步等待解析终态。
 * POST ticket 同时绑定 tenant、用户、Session、权限 revision/fingerprint 和后端 files 导航。
 */
async function submitFileUpload(file, consoleShellState) {
  const token = authSession.token;
  const owner = currentFileWriteOwner(consoleShellState);
  // Token 或实时写 owner 任一缺失都必须先销毁旧文件名和 cursor，不能仅依赖上传按钮隐藏。
  if (!token || !owner) {
    filePageState.clear();
    // Token 缺失属于登录边界；仅撤权时保留 Session 并由后续导航刷新收口。
    if (!token) renderLogin();
    return;
  }
  const operation = filePageState.beginUploadOperation(owner);
  if (!operation) return;
  let operationFinished = false;
  // 单一收口函数防止成功、异常和 finally 三条路径重复结束后来上传 ticket。
  const finishOperation = () => {
    if (operationFinished) return;
    operationFinished = true;
    filePageState.finishUploadOperation(operation);
  };
  try {
    const result = await uploadFile({
      controlApiUrl,
      token,
      file,
      signal: operation.controller.signal,
    });
    const currentOwner = currentFileWriteOwner(consoleShellState);
    if (!filePageState.acceptUpload(operation, result, currentOwner)) return;
    // 显式结束 POST 后再刷新列表，避免上传 busy 门禁把本轮结果误当成仍未决。
    finishOperation();
    await refresh();
  } catch (error) {
    const currentOwner = currentFileWriteOwner(consoleShellState);
    // owner 漂移或主动 Abort 的迟到错误必须静默丢弃，不能影响新租户页面。
    if (!filePageState.isUploadOperationCurrent(operation, currentOwner) || isFileAbortError(error)) {
      return;
    }
    // 401 是强认证边界，先清文件 DTO 与 Token，再渲染统一登录页。
    if (error instanceof AuthApiError && error.status === 401) {
      filePageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 只有 auth.permission_denied 表示撤权；file.dlp_blocked 同为 403，但它是可追踪业务终态。
    if (
      error instanceof AuthApiError
      && error.status === 403
      && error.code === "auth.permission_denied"
    ) {
      filePageState.clear();
      finishOperation();
      await refresh();
      return;
    }
    filePageState.rejectUpload(operation, error, currentOwner);
    // 解析/DLP/存储失败可能已经提交 failed/blocked owner，结束 POST 后权威刷新处理记录。
    finishOperation();
    await refresh();
  } finally {
    finishOperation();
  }
}
/**
 * 绑定 FinOps 筛选、opaque cursor、详情和调整命令。
 * 主应用只调度页面内存与 API；tenant、actor、currency、direction 和权限结论都不会在此生成。
 */
function bindFinOpsInteractions(consoleShellState) {
  bindFinOpsLedgerInteractions({
    // 新筛选会使旧签名 cursor 失效，状态机同步清除 cursor 栈与选中详情。
    async onFilter(filters) {
      finOpsPageState.setFilters(filters);
      await refresh();
    },
    // 清空筛选与手工回到第一页使用同一状态转换，避免残留旧 snapshot 水位。
    async onResetFilters() {
      finOpsPageState.resetFilters();
      await refresh();
    },
    // 上一页 cursor 只从内存栈恢复，不从 DOM 或 URL 读取。
    async onPreviousPage() {
      if (finOpsPageState.previousPage()) {
        await refresh();
      }
    },
    // 下一页 cursor 只能来自刚通过后端校验的当前页响应。
    async onNextPage() {
      if (finOpsPageState.nextPage()) {
        await refresh();
      }
    },
    // 详情 ID 必须存在于当前页，状态机拒绝 DOM 手工注入的其他 ID。
    async onSelectEntry(entryId) {
      if (finOpsPageState.selectEntry(entryId)) {
        await refresh();
      }
    },
    // 关闭详情不会改变筛选和 cursor，方便管理员继续逐条核对。
    async onCloseDetail() {
      finOpsPageState.closeDetail();
      await refresh();
    },
    // Request Logs 跳转先配置当前身份 owner，再走统一后端导航切换；手写 DOM ID 仍不能跨租户读取。
    async onOpenRequest(requestId) {
      const normalizedRequestId = String(requestId ?? "").trim();
      const navigationButton = Array.from(document.querySelectorAll("[data-console-nav]"))
        .find((button) => button.dataset.consoleNav === "request-logs");
      if (!normalizedRequestId || !navigationButton) {
        return;
      }
      configureCurrentRequestLogState(
        { requestId: normalizedRequestId, limit: 20, offset: 0 },
        normalizedRequestId,
      );
      finOpsPageState.clear();
      await switchConsoleNavigation(navigationButton, consoleShellState);
    },
    // 每次打开创建独立幂等键；同一对话框失败重试由内存复用，不会生成第二笔账。
    async onOpenAdjustment(targetEntryId) {
      if (!hasPermission(authSession.context, PERMISSIONS.BILLING_FINOPS_WRITE)) {
        return;
      }
      const opened = finOpsPageState.openDialog({
        targetEntryId,
        idempotencyKey: createFinOpsIdempotencyKey(),
        operationRequestId: createFinOpsRequestId("adjustment"),
        operationTraceId: createFinOpsTraceId(),
      });
      if (opened) {
        await refresh();
      }
    },
    // 取消时立即销毁 reason、幂等键和 operation IDs；重绘完成后把焦点交还原命令。
    async onCloseAdjustment() {
      const targetEntryId = finOpsPageState.snapshot().dialog?.targetEntryId ?? null;
      finOpsPageState.closeDialog();
      await refresh();
      restoreFinOpsAdjustmentFocus(targetEntryId);
    },
    // 提交路径由独立函数维护认证 owner、双击门禁和统一错误。
    async onSubmitAdjustment(values) {
      await submitFinOpsAdjustment(values);
    },
  });
}

/**
 * 提交当前 FinOps 调整草稿。
 *
 * 状态机在 fetch 前进入 submitting 并冻结幂等键；网络失败保留同一键供安全重试，
 * 成功后先销毁 reason 再重新读取第一页，避免正文停留在 DOM 或后续错误状态。
 */
async function submitFinOpsAdjustment(values) {
  const token = authSession.token;
  // 财务写入必须同时绑定当前 Session 与 billing.finops.write；任一缺失都不能保留或提交调整草稿。
  if (!token || !hasPermission(authSession.context, PERMISSIONS.BILLING_FINOPS_WRITE)) {
    if (!token) {
      finOpsPageState.clear();
      renderLogin();
    }
    return;
  }
  const payload = finOpsPageState.beginSubmit(values);
  if (!payload) {
    return;
  }
  setFinOpsSubmittingDom();
  try {
    const result = await createFinOpsAdjustment({
      controlApiUrl,
      token,
      ...payload,
    });
    // 登出、换 Session 或降权期间完成的旧响应不得进入新身份页面内存。
    if (authSession.token !== token || !hasPermission(authSession.context, PERMISSIONS.BILLING_FINOPS_WRITE)) {
      finOpsPageState.clear();
      return;
    }
    finOpsPageState.acceptAdjustment(result);
    await refresh();
  } catch (error) {
    // 请求期间发生登出或换 Session 时，旧身份的失败响应也不得进入新页面状态。
    if (authSession.token !== token) {
      finOpsPageState.clear();
      return;
    }
    // 服务端确认 Session 失效后必须销毁本地财务状态和认证上下文，避免继续展示旧租户分录。
    if (error instanceof AuthApiError && error.status === 401) {
      finOpsPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 统一错误对象只包含固定 code/message/request_id；reason 和请求 body 不进入反馈。
    finOpsPageState.rejectAdjustment(error);
    await refresh();
  }
}

/**
 * 在请求进行时立即锁定对话框控件，防止浏览器尚未完成重绘前发生第二次提交。
 * 真实 exactly-once 仍由后端 tenant + idempotency key 唯一约束保证。
 */
function setFinOpsSubmittingDom() {
  const form = document.querySelector("#finops-adjustment-form");
  form?.setAttribute("aria-busy", "true");
  for (const control of form?.querySelectorAll("button, input, select, textarea") ?? []) {
    control.disabled = true;
  }
}

/**
 * 调整对话框关闭后恢复键盘焦点。
 * 冲正回到原行命令；普通调整回到页头命令，不用 ID 拼接 CSS selector 以避免注入。
 */
function restoreFinOpsAdjustmentFocus(targetEntryId) {
  if (targetEntryId) {
    const trigger = Array.from(document.querySelectorAll("[data-finops-reverse-entry-id]"))
      .find((button) => button.dataset.finopsReverseEntryId === targetEntryId);
    trigger?.focus();
    return;
  }
  document.querySelector("#finops-open-adjustment")?.focus();
}

/**
 * 绑定内置 API 调试台的提交和日志跳转事件。
 * 新调试台只调用控制面 `/debug/chat-completions`，不读取、不保存也不提交 Virtual Key 明文。
 */
function bindDebugConsoleEvents() {
  const debugConsoleForm = document.querySelector("#debug-console-form");
  debugConsoleForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = authSession.token;
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (!token) {
      renderLogin();
      return;
    }
    const data = new FormData(debugConsoleForm);
    const button = debugConsoleForm.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "发送中";
    const environmentId = String(data.get("environmentId") ?? "");
    const virtualKeyId = String(data.get("virtualKeyId") ?? "");
    const modelAlias = String(data.get("modelAlias") ?? "");
    const messagesText = String(data.get("messages") ?? "");
    const temperature = optionalNumber(data.get("temperature"), undefined);
    const maxTokens = optionalNumber(data.get("maxTokens"), undefined);
    gatewayDebugState = {
      environmentId,
      virtualKeyId,
      modelAlias,
      messagesText,
      temperature,
      maxTokens,
      feedback: "请求中",
    };
    try {
      const result = await sendDebugChatCompletion({
        controlApiUrl,
        token,
        environmentId,
        virtualKeyId,
        modelAlias,
        messages: parseDebugMessages(messagesText),
        temperature,
        maxTokens,
      });
      gatewayDebugState = {
        environmentId,
        virtualKeyId,
        modelAlias,
        messagesText,
        temperature,
        maxTokens,
        feedback: "请求完成",
        result,
      };
      await refresh();
    } catch (error) {
      // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      gatewayDebugState = {
        environmentId,
        virtualKeyId,
        modelAlias,
        messagesText,
        temperature,
        maxTokens,
        feedback: "请求失败",
        error: debugConsoleError(error),
      };
      await refresh();
    }
  });

  for (const button of document.querySelectorAll("[data-debug-log-request-id]")) {
    button.addEventListener("click", async () => {
      await openRequestLogFromDebug(button.dataset.debugLogRequestId);
    });
  }
}

/**
 * 解析调试台 messages JSON。
 * 前端只做轻量语法校验，角色、长度和敏感字段边界仍交给后端按统一 debug.request_invalid 校验。
 */
function parseDebugMessages(value) {
  try {
    const parsed = JSON.parse(String(value ?? ""));
    if (!Array.isArray(parsed)) {
      // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
      throw new Error("messages must be an array");
    }
    return parsed;
  } catch (error) {
    const invalid = new Error("messages JSON 格式无效");
    invalid.code = "debug.request_invalid";
    invalid.requestId = "-";
    invalid.traceId = "-";
    invalid.suggestion = "检查 messages 是否为 JSON 数组。";
    throw invalid;
  }
}

/**
 * 把调试台异常归一化为渲染层可展示的错误对象。
 * 本地 JSON 解析错误和控制面统一错误都只展示 code/message/request_id，不输出浏览器堆栈。
 */
function debugConsoleError(error) {
  if (error instanceof AuthApiError) {
    return {
      code: error.code,
      message: error.message,
      requestId: error.requestId,
      traceId: error.traceId,
      suggestion: error.suggestion ?? "",
      diagnosis: error.diagnosis ?? null,
    };
  }
  return {
    code: error?.code ?? "debug.request_failed",
    message: error?.message ?? "无法连接控制面调试接口。",
    requestId: error?.requestId ?? "-",
    traceId: error?.traceId ?? "-",
    suggestion: error?.suggestion ?? "检查 Control Plane API 是否启动。",
    diagnosis: error?.diagnosis ?? null,
  };
}

/**
 * 从调试台跳转到调用日志并按 request_id 过滤。
 * 只把 request_id 写入前端过滤状态和 URL hash，不把 Prompt、响应正文或 Key 信息带入地址栏。
 */
async function openRequestLogFromDebug(requestId) {
  const normalized = String(requestId ?? "").trim();
  if (!normalized) {
    return;
  }
  // 跳转前先建立当前完整身份 owner；不能把上一租户同名 request_id 的缓存带入新页面。
  configureCurrentRequestLogState(
    { requestId: normalized, limit: 20, offset: 0 },
    normalized,
  );
  replaceNavigationHash("request-logs");
  await refresh();
}

/**
 * 生成认证相关错误文案。
 * 该函数只展示后端统一错误中的安全字段，不能输出登录密码、Token 原文或浏览器异常堆栈。
 */
function authErrorMessage(error) {
  if (error instanceof AuthApiError) {
    // 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return error.code === "auth.token_expired" || error.code === "auth.session_revoked"
      ? "登录状态已失效，请重新登录。"
      : error.message;
  }
  return "无法连接控制面认证接口。";
}

/**
 * 执行 控制台主应用 的 `readCapabilities` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function readCapabilities(form) {
  const capabilities = {};
  for (const input of form.querySelectorAll('input[name="capabilities"]')) {
    capabilities[input.value] = input.checked;
  }
  return capabilities;
}

/**
 * 执行 控制台主应用 的 `optionalText` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function optionalText(value) {
  const text = String(value ?? "").trim();
  return text || undefined;
}

/**
 * 执行 控制台主应用 的 `optionalNumber` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
function optionalNumber(value, fallback) {
  const text = String(value ?? "").trim();
  return text === "" ? fallback : Number(text);
}

/**
 * 把浏览器 datetime-local 输入转换为带时区 UTC ISO 字符串，非法值交给后端统一校验。
 */
function toUtcIso(value) {
  const date = new Date(String(value ?? ""));
  return Number.isNaN(date.getTime()) ? String(value ?? "") : date.toISOString();
}

/**
 * 按当前 active item 读取快速开始数据。
 * 安全：只有进入 quickstart 页面时才调用，避免自动刷新控制台时重复写 developer.quickstart_viewed 事件。
 */
async function loadQuickstartState(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "quickstart") {
    return null;
  }
  // 前端仅按后端快照减少无效请求，真实 developer_portal.read 仍由 API 校验。
  if (!hasPermission(authSession.context, PERMISSIONS.DEVELOPER_PORTAL_READ)) {
    return null;
  }
  try {
    return await getDeveloperQuickstartData({
      controlApiUrl,
      token: authSession.token,
    });
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 仅在 SDK 示例导航激活时按 Python、JavaScript 固定顺序读取两种制品元数据。
 * 安全：顺序请求消除双请求 401 竞态；任一 401 立即统一清理会话并停止下一请求，其他错误只写入对应语言状态。
 */
async function loadSdkExampleStates(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "sdk-examples") {
    return null;
  }
  // 前端权限判断只是减少无效请求，直接调用仍必须由后端 developer_portal.read 守卫拒绝。
  if (!hasPermission(authSession.context, PERMISSIONS.DEVELOPER_PORTAL_READ)) {
    return null;
  }

  const states = { python: null, javascript: null };
  try {
    states.python = await getPythonSdkExample({
      controlApiUrl,
      token: authSession.token,
    });
  } catch (error) {
    // Python 先返回 401 时必须在发出 JavaScript 请求前退出，避免同一失效 Token 触发第二个竞态响应。
    if (handleSdkUnauthorized(error)) {
      return null;
    }
    // 非 401 只形成 Python 独立错误态，JavaScript 元数据仍继续加载并保持可用。
    states.python = sdkExampleErrorState(error);
  }

  try {
    states.javascript = await getJavascriptSdkExample({
      controlApiUrl,
      token: authSession.token,
    });
  } catch (error) {
    // JavaScript 返回 401 时同样统一结束会话；Python 已返回的数据不能继续留在过期会话页面。
    if (handleSdkUnauthorized(error)) {
      return null;
    }
    // 非 401 只形成 JavaScript 独立错误态，已经成功的 Python 制品保持展示。
    states.javascript = sdkExampleErrorState(error);
  }
  return states;
}

/**
 * 处理 SDK 元数据或下载接口的统一 401，会话清理和登录页重置必须在一个同步分支完成。
 * 返回 true 表示调用方必须立即停止当前流程；403 与 5xx 返回 false 并保留语言专属错误反馈。
 */
function handleSdkUnauthorized(error) {
  if (!(error instanceof AuthApiError) || error.status !== 401) {
    return false;
  }
  clearAuthSession();
  authSession = { token: null, context: null };
  renderLogin(authErrorMessage(error), error.requestId);
  return true;
}

/**
 * 把非 401 SDK 错误转换为仅含公开消息和 request_id 的语言局部状态。
 */
function sdkExampleErrorState(error) {
  return {
    state: "error",
    message: authErrorMessage(error),
    requestId: error?.requestId ?? "-",
  };
}

/**
 * 读取当前用户语言偏好，并把保存反馈和统一错误透传给语言设置页。
 * 安全：请求体和查询参数都不携带 tenant_id/user_id，后端只能根据登录态恢复数据边界。
 */
async function loadUserPreferencesState() {
  // 用户偏好属于控制台基础能力，授权快照损坏时不得仅凭登录态继续后台读取。
  if (!hasPermission(authSession.context, PERMISSIONS.CONSOLE_BASIC_READ)) {
    return null;
  }
  try {
    const state = await getUserPreferences({
      controlApiUrl,
      token: authSession.token,
    });
    return {
      ...state,
      actionError: userPreferenceActionError,
      feedback: userPreferenceFeedback,
    };
  } catch (error) {
    // 认证失败必须清理本地登录态，避免过期会话继续驱动控制台请求。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      code: error?.code,
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      actionError: userPreferenceActionError,
    };
  }
}

/**
 * 构造 PAGE-092 当前请求 owner。
 *
 * identity 冻结 tenant、user、Session、permission revision 与 fingerprint；navigation 必须由同轮后端
 * shell 精确授权。Token 只返回给私有 ticket 做内存比较，不进入 snapshot、DOM、日志或遥测。
 */
function currentRoleDashboardOwner(consoleShellState, requestedView = "") {
  const identity = roleDashboardIdentitySnapshot(authSession.context);
  if (
    !identity
    || !authSession.token
    || !roleDashboardNavigationOwned(consoleShellState, readNavigationHashState())
  ) {
    return null;
  }
  return {
    identity,
    navigation: "overview",
    requestedView,
    token: authSession.token,
  };
}

/**
 * 读取 PAGE-092 角色首页。
 *
 * 默认读取不带 view；后续自动刷新只回传最近成功响应下发的 selected view。成功与所有错误共用同一个
 * owner 门禁，503 仅由状态机在同身份、同视图下保留 partial，其他失败清除旧受保护数据。
 */
async function loadRoleDashboardState(consoleShellState, source = "refresh") {
  const selectedView = roleDashboardPageState.selectedView();
  const owner = currentRoleDashboardOwner(consoleShellState, selectedView);
  if (!owner) {
    clearRoleDashboardSessionState();
    return null;
  }
  const availableViewCodes = roleDashboardPageState.availableViewCodes();
  const ticket = roleDashboardPageState.begin({
    ...owner,
    availableViewCodes,
    source,
  });
  // owner 构造失败时只返回已净化页面快照，禁止绕过状态机直接发起角色首页请求。
  if (!ticket) return roleDashboardPageState.snapshot();
  try {
    const response = await getDashboardRoleView({
      controlApiUrl,
      token: ticket.token,
      view: ticket.requestedView,
      availableViewCodes: ticket.availableViewCodes,
      signal: ticket.controller.signal,
    });
    const state = roleDashboardPageState.accept(
      ticket,
      response,
      currentRoleDashboardOwner(consoleShellState, ticket.requestedView),
    );
    // 只有当前 owner 接纳成功后才把后续自动读取来源恢复为 refresh，迟到成功不会改写入口语义。
    if (state) roleDashboardEntrySource = "refresh";
    return state;
  } catch (error) {
    // 成功、401、403 和 503 全部先走完整 owner；旧 Session 的迟到错误不能清认证或覆盖当前页面。
    if (!roleDashboardPageState.owns(
      ticket,
      currentRoleDashboardOwner(consoleShellState, ticket.requestedView),
    )) {
      return null;
    }
    // 主动取消表示 owner 已切换，不能把正常的离页或换视图渲染成依赖错误。
    if (error?.name === "AbortError") return null;
    const state = roleDashboardPageState.reject(
      ticket,
      error,
      currentRoleDashboardOwner(consoleShellState, ticket.requestedView),
    );
    roleDashboardEntrySource = "refresh";
    // 只有通过完整 owner 门禁的当前 401 才能销毁认证状态，迟到认证错误已经在上方丢弃。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRoleDashboardSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403、配置损坏和其他非 partial 错误都立即替换旧卡片；不能等待其余控制台接口后才清理受保护 DOM。
    if (state?.state === "error") {
      renderRoleDashboardRootState(state, consoleShellState);
    }
    return state;
  } finally {
    roleDashboardPageState.finish(ticket);
  }
}

/**
 * 同步销毁 PAGE-092 请求、事件和可见卡片。
 * 该函数用于离页、登出、pagehide、身份漂移和后端导航裁剪，清理不依赖网络请求成功。
 */
function clearRoleDashboardSessionState() {
  roleDashboardPageState.clear();
  document.querySelector("[data-role-dashboard-root]")?.replaceChildren();
}

/**
 * 局部替换角色首页根节点并重绑分段控制。
 * 只在当前 overview shell 内操作，避免视图切换触发全控制台数据重载或改写其他页面状态。
 */
function renderRoleDashboardRootState(state, consoleShellState) {
  const root = document.querySelector("[data-role-dashboard-root]");
  if (!root || currentRenderedNavigation() !== "overview") return false;
  const html = renderRoleDashboardPanel(state, authSession.context, consoleShellState);
  if (!html) {
    root.replaceChildren();
    return false;
  }
  root.outerHTML = html;
  bindRoleDashboardInteractions(consoleShellState);
  return true;
}

/**
 * 构造 PAGE-093 当前授权 owner。
 *
 * 后端导航必须精确声明 enterprise-chat，认证快照必须含 chat.use；前端 owner 只防迟到响应，不能替代
 * Control API 的实时权限、tenant、用户和登录 Session 校验。
 */
function enterpriseChatOwner(consoleShellState) {
  const item = consoleShellState?.navigation?.navigation_items?.find((candidate) => (
    candidate?.id === "enterprise-chat"
    && candidate?.target_section === "enterprise-chat"
    && candidate?.visible !== false
    && candidate?.disabled !== true
  ));
  const identity = enterpriseChatIdentitySnapshot(authSession.context);
  if (
    consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== "enterprise-chat"
    || !item
    || !identity
    || !authSession.token
  ) return null;
  // Token 只进入本次请求 owner，不写入页面状态或 DOM；后端仍按登录 Session 和 chat.use 实时鉴权。
  return Object.freeze({ identity, navigation: "enterprise-chat", token: authSession.token });
}

/**
 * 只在 PAGE-093 后端 owner 完整时读取安全 options。
 * 已加载目录、活动会话或正文由状态机保留；定时刷新不会重复 GET 或重建敏感状态。
 */
async function loadEnterpriseChatState(consoleShellState) {
  const owner = enterpriseChatOwner(consoleShellState);
  // 导航、身份或后端授权任一不完整时立即清空正文，禁止跨用户、跨租户沿用旧会话。
  if (!owner || !enterpriseChatPageState.syncOwner(authSession.context, "enterprise-chat")) {
    clearEnterpriseChatSessionState();
    return enterpriseChatPageState.snapshot();
  }
  const current = enterpriseChatPageState.snapshot();
  if (current.options) return current;
  const ticket = enterpriseChatPageState.beginOptions();
  if (!ticket) return enterpriseChatPageState.snapshot();
  try {
    const result = await getEnterpriseChatOptions({
      controlApiUrl,
      token: owner.token,
      signal: ticket.signal,
    });
    enterpriseChatPageState.acceptOptions(ticket, result);
  } catch (error) {
    // 离页和 owner 变化会主动中止请求；此时保留新 owner 状态，不能把旧请求误报为页面错误。
    if (error?.name === "AbortError") return enterpriseChatPageState.snapshot();
    const accepted = enterpriseChatPageState.reject(ticket, error);
    // 只有仍属于当前 owner 的 401 才能清理登录态；迟到认证错误已经被状态机拒绝。
    if (accepted && error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      clearEnterpriseChatSessionState();
      renderLogin(authErrorMessage(error), error.requestId);
    }
  }
  return enterpriseChatPageState.snapshot();
}

/** 身份、Session、permission revision 或授权指纹任一改变都必须同步销毁 Chat 正文。 */
function clearEnterpriseChatStateOnIdentityChange(previous, current) {
  if (enterpriseChatIdentitySnapshot(previous) !== enterpriseChatIdentitySnapshot(current)) {
    clearEnterpriseChatSessionState();
  }
}

/**
 * abort 所有企业 Chat 请求并清除正文、草稿、会话和可见 DOM。
 * 本函数不调用 DELETE：离页/注销时浏览器取消不是服务端取消证明，后端 Session 会按 TTL 失效。
 */
function clearEnterpriseChatSessionState() {
  enterpriseChatPageState.clear();
  document.querySelector("[data-enterprise-chat-root]")?.replaceChildren();
}

/** 局部重绘 PAGE-093 并重新绑定事件，避免消息操作触发整套控制台数据读取。 */
function renderEnterpriseChatPanel(consoleShellState, { scrollToEnd = false } = {}) {
  const shell = document.querySelector(".enterprise-chat-shell");
  if (!shell || !enterpriseChatOwner(consoleShellState)) return false;
  shell.innerHTML = renderEnterpriseChat(enterpriseChatPageState.snapshot(), authSession.context);
  // 正文必须在模板解析完成后使用 DOM 文本 API 投影，禁止模型输出成为 HTML。
  hydrateEnterpriseChatText(shell, enterpriseChatPageState.snapshot());
  bindEnterpriseChatInteractions(consoleShellState);
  if (scrollToEnd) {
    const messageRegion = shell.querySelector(".enterprise-chat-messages");
    if (messageRegion) messageRegion.scrollTop = messageRegion.scrollHeight;
  }
  return true;
}

/**
 * 绑定项目、模型、会话、发送和清空交互。
 * 所有 API 都只使用状态机冻结的安全 ID；Prompt history 只在 send ticket 与当前 fetch 调用栈存在。
 */
function bindEnterpriseChatInteractions(consoleShellState) {
  if (!enterpriseChatOwner(consoleShellState) || !document.querySelector("[data-enterprise-chat-root]")) return;
  document.querySelector("[data-enterprise-chat-project]")?.addEventListener("change", (event) => {
    const selected = event.currentTarget.selectedOptions?.[0];
    if (enterpriseChatPageState.selectProject(selected?.value, selected?.dataset.environmentId)) {
      renderEnterpriseChatPanel(consoleShellState);
    }
  });
  document.querySelector("[data-enterprise-chat-alias]")?.addEventListener("change", (event) => {
    if (enterpriseChatPageState.selectModelAlias(event.currentTarget.value)) {
      renderEnterpriseChatPanel(consoleShellState);
    }
  });
  document.querySelector("[data-enterprise-chat-template]")?.addEventListener("change", (event) => {
    if (enterpriseChatPageState.selectPromptTemplate(event.currentTarget.value)) {
      // 重新排序只影响下拉展示和推荐标记，状态机不会修改 selectedModelAliasId。
      renderEnterpriseChatPanel(consoleShellState);
    }
  });
  document.querySelector("[data-enterprise-chat-new]")?.addEventListener("click", async () => {
    const owner = enterpriseChatOwner(consoleShellState);
    const ticket = owner ? enterpriseChatPageState.beginCreateSession() : null;
    if (!owner || !ticket) return;
    renderEnterpriseChatPanel(consoleShellState);
    try {
      const result = await createEnterpriseChatSession({
        controlApiUrl,
        token: owner.token,
        projectId: ticket.projectId,
        environmentId: ticket.environmentId,
        modelAliasId: ticket.modelAliasId,
        signal: ticket.signal,
      });
      // 仅当前 owner 与创建 ticket 仍匹配时提交 Session，迟到响应不得覆盖租户切换后的页面。
      if (enterpriseChatPageState.acceptSession(ticket, result)) {
        renderEnterpriseChatPanel(consoleShellState);
      }
    } catch (error) {
      await rejectEnterpriseChatOperation(ticket, error, consoleShellState);
    }
  });

  const form = document.querySelector("[data-enterprise-chat-form]");
  const textarea = form?.querySelector("textarea[name='message']");
  textarea?.addEventListener("input", () => {
    const state = enterpriseChatPageState.setDraft(textarea.value);
    const submit = form.querySelector("button[type='submit']");
    if (submit) {
      submit.disabled = state.busy
        || state.status !== "ready"
        || !state.session
        || state.historyLimitReached
        || (state.selectedPromptTemplateVersionId ? !state.templateReady : !state.draft.trim());
    }
    const liveStatus = document.querySelector("[data-enterprise-chat-live-status]");
    if (liveStatus) {
      // 输入跨过 UTF-8 历史容量时立即显示状态机的固定提示；不把正文或累计字节写入 DOM、日志或属性。
      liveStatus.textContent = state.historyLimitMessage ?? "";
    }
  });
  for (const input of document.querySelectorAll("[data-enterprise-chat-template-variable]")) {
    input.addEventListener("input", () => {
      const name = input.dataset.enterpriseChatTemplateVariable;
      enterpriseChatPageState.setTemplateVariable(name, input.value);
      const state = enterpriseChatPageState.snapshot();
      const submit = form?.querySelector("button[type='submit']");
      if (submit) {
        submit.disabled = state.busy
          || state.status !== "ready"
          || !state.session
          || state.historyLimitReached
          || !state.templateReady;
      }
    });
  }
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const owner = enterpriseChatOwner(consoleShellState);
    if (!owner) return;
    enterpriseChatPageState.setDraft(textarea?.value ?? "");
    let clientMessageId;
    try {
      clientMessageId = createEnterpriseChatClientMessageId();
    } catch (error) {
      // Web Crypto 缺失使用与网络失败相同的固定公开错误，原始异常不进入页面状态。
      const unavailable = enterpriseChatPageState.beginSend("00000000-0000-4000-8000-000000000000");
      if (unavailable) await rejectEnterpriseChatOperation(unavailable, error, consoleShellState);
      return;
    }
    const ticket = enterpriseChatPageState.beginSend(clientMessageId);
    if (!ticket) return;
    renderEnterpriseChatPanel(consoleShellState);
    try {
      const result = await sendEnterpriseChatMessage({
        controlApiUrl,
        token: owner.token,
        chatSessionId: ticket.chatSessionId,
        clientMessageId: ticket.clientMessageId,
        history: ticket.history,
        promptTemplateVersionId: ticket.promptTemplateVersionId ?? null,
        templateVariables: ticket.templateVariables ?? null,
        signal: ticket.signal,
      });
      if (enterpriseChatPageState.acceptMessage(ticket, result)) {
        renderEnterpriseChatPanel(consoleShellState, { scrollToEnd: true });
      }
    } catch (error) {
      await rejectEnterpriseChatOperation(ticket, error, consoleShellState);
    }
  });

  document.querySelector("[data-enterprise-chat-clear]")?.addEventListener("click", async () => {
    if (!globalThis.window?.confirm("清空当前会话？")) return;
    const owner = enterpriseChatOwner(consoleShellState);
    const ticket = owner ? enterpriseChatPageState.beginClearSession() : null;
    if (!owner || !ticket) return;
    renderEnterpriseChatPanel(consoleShellState);
    try {
      await clearEnterpriseChatSession({
        controlApiUrl,
        token: owner.token,
        chatSessionId: ticket.chatSessionId,
        signal: ticket.signal,
      });
      if (enterpriseChatPageState.acceptClear(ticket)) {
        renderEnterpriseChatPanel(consoleShellState);
      }
    } catch (error) {
      await rejectEnterpriseChatOperation(ticket, error, consoleShellState);
    }
  });
}

/**
 * 统一处理企业 Chat 失败。
 * 401 回登录页，403 触发实时权限刷新；其他错误只局部显示 code/message/request_id，不自动重试。
 */
async function rejectEnterpriseChatOperation(ticket, error, consoleShellState) {
  if (error?.name === "AbortError") return;
  const accepted = enterpriseChatPageState.reject(ticket, error);
  if (!accepted) return;
  // 实时 401 表示登录身份已失效，必须同时清除认证信息与内存正文，避免敏感会话残留。
  if (error instanceof AuthApiError && error.status === 401) {
    clearAuthSession();
    authSession = { token: null, context: null };
    clearEnterpriseChatSessionState();
    renderLogin(authErrorMessage(error), error.requestId);
    return;
  }
  // 实时 403 只证明授权状态漂移，可能撤销 chat.use 或 prompt_template.use；重拉后再按最新导航决定页面。
  if (Number(error?.status) === 403) {
    clearEnterpriseChatSessionState();
    await refresh();
    return;
  }
  renderEnterpriseChatPanel(consoleShellState);
}

/**
 * 构造 PAGE-094 当前完整 owner。
 *
 * read/write 身份分别冻结；Token 只在返回对象闭包中供当前请求使用，不进入状态 snapshot、DOM 或日志。
 */
function promptTemplateOwner(consoleShellState, generation = refreshSequence) {
  const item = consoleShellState?.navigation?.navigation_items?.find((candidate) => (
    candidate?.id === "prompt-templates"
    && candidate?.target_section === "prompt-templates"
    && candidate?.visible !== false
    && candidate?.disabled !== true
  ));
  const readIdentity = promptTemplateReadIdentitySnapshot(authSession.context);
  if (
    consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== "prompt-templates"
    || !item
    || !readIdentity
    || !authSession.token
    || !Number.isSafeInteger(generation)
  ) return null;
  return Object.freeze({
    readIdentity,
    writeIdentity: promptTemplateWriteIdentitySnapshot(authSession.context),
    navigation: "prompt-templates",
    refreshGeneration: generation,
    token: authSession.token,
  });
}

/**
 * 首次进入 PAGE-094 时读取列表、表单 options 与首条详情。
 *
 * 其他导航不预取；list/detail lane 相互独立，任何迟到结果还会由状态机复核完整 owner 和 generation。
 */
async function loadPromptTemplateState(consoleShellState, refreshId) {
  const owner = promptTemplateOwner(consoleShellState, refreshId);
  if (!owner || !promptTemplatePageState.syncOwner(owner)) {
    clearPromptTemplateSessionState();
    return promptTemplatePageState.snapshot();
  }
  const current = promptTemplatePageState.snapshot();
  // 已有同 owner 列表或公开错误时保留页面内存；显式刷新按钮仍可独立重试 list lane。
  if (current.templates.length || current.pageError) return current;
  const ticket = promptTemplatePageState.beginList();
  if (!ticket) return promptTemplatePageState.snapshot();
  try {
    const [listResult, optionsResult] = await Promise.all([
      listPromptTemplates({
        controlApiUrl,
        token: owner.token,
        ...ticket.filters,
        signal: ticket.signal,
        requestId: ticket.requestId,
      }),
      getPromptTemplateOptions({
        controlApiUrl,
        token: owner.token,
        signal: ticket.signal,
        requestId: ticket.optionsRequestId,
      }),
    ]);
    if (promptTemplatePageState.acceptList(ticket, listResult, optionsResult)) {
      await loadPromptTemplateDetail(consoleShellState);
    }
  } catch (error) {
    await rejectPromptTemplateRead(ticket, error, consoleShellState);
  }
  return promptTemplatePageState.snapshot();
}

/** 精确读取当前选择详情；该函数不会自动读取任何解密版本正文。 */
async function loadPromptTemplateDetail(consoleShellState) {
  const owner = promptTemplateOwner(consoleShellState);
  if (!owner || !promptTemplatePageState.syncOwner(owner)) return false;
  const ticket = promptTemplatePageState.beginDetail();
  if (!ticket) return false;
  try {
    const result = await getPromptTemplate({
      controlApiUrl,
      token: owner.token,
      promptTemplateId: ticket.promptTemplateId,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    return promptTemplatePageState.acceptDetail(ticket, result);
  } catch (error) {
    await rejectPromptTemplateRead(ticket, error, consoleShellState);
    return false;
  }
}

/** 页面内显式刷新列表；不会启动全控制台读取，也不会取消 detail/definition/mutation/conflict lane。 */
async function refreshPromptTemplateList(consoleShellState) {
  const owner = promptTemplateOwner(consoleShellState);
  if (!owner || !promptTemplatePageState.syncOwner(owner)) return;
  const ticket = promptTemplatePageState.beginList();
  if (!ticket) return;
  renderPromptTemplatePanel(consoleShellState);
  try {
    const [listResult, optionsResult] = await Promise.all([
      listPromptTemplates({
        controlApiUrl,
        token: owner.token,
        ...ticket.filters,
        signal: ticket.signal,
        requestId: ticket.requestId,
      }),
      getPromptTemplateOptions({
        controlApiUrl,
        token: owner.token,
        signal: ticket.signal,
        requestId: ticket.optionsRequestId,
      }),
    ]);
    if (promptTemplatePageState.acceptList(ticket, listResult, optionsResult)) {
      await loadPromptTemplateDetail(consoleShellState);
    }
  } catch (error) {
    await rejectPromptTemplateRead(ticket, error, consoleShellState);
  }
  renderPromptTemplatePanel(consoleShellState);
}

/** 精确读取用户选择的不可变版本定义，并在成功后用 DOM 文本 API 局部投影。 */
async function loadPromptTemplateDefinition(consoleShellState, promptTemplateVersionId) {
  const owner = promptTemplateOwner(consoleShellState);
  if (!owner || !promptTemplatePageState.syncOwner(owner)) return;
  const ticket = promptTemplatePageState.beginDefinition(promptTemplateVersionId);
  if (!ticket) return;
  renderPromptTemplatePanel(consoleShellState);
  try {
    const result = await getPromptTemplateVersion({
      controlApiUrl,
      token: owner.token,
      promptTemplateId: ticket.promptTemplateId,
      promptTemplateVersionId: ticket.promptTemplateVersionId,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    promptTemplatePageState.acceptDefinition(ticket, result);
  } catch (error) {
    await rejectPromptTemplateRead(ticket, error, consoleShellState);
  }
  renderPromptTemplatePanel(consoleShellState);
}

/**
 * 同步销毁 PAGE-094 DOM、正文、草稿和全部 lane。
 *
 * 顺序固定为先清 DOM、再清闭包并 abort，避免取消回调或下一帧仍能观察旧敏感文本。
 */
function clearPromptTemplateSessionState() {
  clearPromptTemplateSensitiveDom(document);
  promptTemplatePageState.leave();
  document.querySelector("[data-prompt-templates-root]")?.replaceChildren();
}

/** tenant、Session、permission revision、授权指纹或 write owner 任一变化都立即销毁旧模板内存。 */
function clearPromptTemplateStateOnIdentityChange(previous, current) {
  const previousOwner = JSON.stringify([
    promptTemplateReadIdentitySnapshot(previous),
    promptTemplateWriteIdentitySnapshot(previous),
  ]);
  const currentOwner = JSON.stringify([
    promptTemplateReadIdentitySnapshot(current),
    promptTemplateWriteIdentitySnapshot(current),
  ]);
  // read 或 write 任一 owner 漂移都必须整体销毁，不能只隐藏写按钮后保留解密正文。
  if (previousOwner !== currentOwner) clearPromptTemplateSessionState();
}

/** 局部重绘 PAGE-094 并立即 hydration；renderer HTML 永不接收正文或变量草稿值。 */
function renderPromptTemplatePanel(consoleShellState) {
  const shell = document.querySelector(".prompt-templates-shell");
  const owner = promptTemplateOwner(consoleShellState);
  if (!shell || !owner) return false;
  const snapshot = promptTemplatePageState.snapshot();
  shell.innerHTML = renderPromptTemplates(snapshot, authSession.context);
  hydratePromptTemplateSensitiveText(shell, snapshot);
  bindPromptTemplateInteractions(consoleShellState);
  return true;
}

/**
 * 绑定 PAGE-094 搜索、详情、定义和表单事件。
 *
 * 事件只读取状态机已接受的资源 ID；伪造 DOM 仍需通过实时 owner 和后端 permission/tenant/CAS 校验。
 */
function bindPromptTemplateInteractions(consoleShellState) {
  const root = document.querySelector("[data-prompt-templates-root]");
  const owner = promptTemplateOwner(consoleShellState);
  if (!root || !owner || !promptTemplatePageState.syncOwner(owner)) return;
  root.querySelector("[data-prompt-template-filter-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    if (promptTemplatePageState.setFilters({ search: data.get("search"), offset: 0 })) {
      void refreshPromptTemplateList(consoleShellState);
    }
  });
  for (const button of root.querySelectorAll("[data-prompt-template-status]")) {
    button.addEventListener("click", () => {
      if (promptTemplatePageState.setFilters({ status: button.dataset.promptTemplateStatus, offset: 0 })) {
        void refreshPromptTemplateList(consoleShellState);
      }
    });
  }
  for (const button of root.querySelectorAll("[data-prompt-template-select]")) {
    button.addEventListener("click", async () => {
      if (!promptTemplatePageState.selectTemplate(button.dataset.promptTemplateSelect)) return;
      renderPromptTemplatePanel(consoleShellState);
      await loadPromptTemplateDetail(consoleShellState);
      renderPromptTemplatePanel(consoleShellState);
    });
  }
  for (const button of root.querySelectorAll("[data-prompt-template-tab]")) {
    button.addEventListener("click", () => {
      if (promptTemplatePageState.setDetailTab(button.dataset.promptTemplateTab)) {
        renderPromptTemplatePanel(consoleShellState);
      }
    });
  }
  for (const button of root.querySelectorAll("[data-prompt-template-load-definition]")) {
    button.addEventListener("click", () => {
      void loadPromptTemplateDefinition(consoleShellState, button.dataset.promptTemplateLoadDefinition);
    });
  }
  root.querySelector("[data-prompt-template-create]")?.addEventListener("click", () => {
    if (promptTemplatePageState.openCreate()) renderPromptTemplatePanel(consoleShellState);
  });
  root.querySelector("[data-prompt-template-edit-metadata]")?.addEventListener("click", () => {
    if (promptTemplatePageState.openMetadata()) renderPromptTemplatePanel(consoleShellState);
  });
  root.querySelector("[data-prompt-template-publish]")?.addEventListener("click", () => {
    if (promptTemplatePageState.openPublish()) renderPromptTemplatePanel(consoleShellState);
  });
  root.querySelector("[data-prompt-template-mobile-back]")?.addEventListener("click", () => {
    root.querySelector("[data-prompt-template-mobile-view]")?.setAttribute("data-prompt-template-mobile-view", "list");
  });
  bindPromptTemplateDialog(consoleShellState, root);
}

/** 绑定 PAGE-094 modal 内的非敏感索引和本地草稿更新；所有文本值只写状态闭包。 */
function bindPromptTemplateDialog(consoleShellState, root) {
  const form = root.querySelector("[data-prompt-template-dialog-form]");
  if (!form) return;
  const dialogElement = form.closest("[data-prompt-template-dialog]");
  dialogElement?.addEventListener("cancel", (event) => {
    // Escape 只能关闭非 busy 表单；已提交但结果未知的写请求不能被浏览器假装取消。
    event.preventDefault();
    if (promptTemplatePageState.closeDialog()) renderPromptTemplatePanel(consoleShellState);
  });
  for (const button of form.querySelectorAll("[data-prompt-template-dialog-close]")) {
    button.addEventListener("click", () => {
      if (promptTemplatePageState.closeDialog()) renderPromptTemplatePanel(consoleShellState);
    });
  }
  for (const field of form.querySelectorAll("input[name], textarea[name], select[name]")) {
    if (field.matches("[data-prompt-template-variable-field]")) continue;
    const eventName = field.tagName === "SELECT" ? "change" : "input";
    field.addEventListener(eventName, () => {
      const changed = promptTemplatePageState.updateDialog({ [field.name]: field.value });
      if (changed && field.name === "project_scope") {
        // 适用范围会清空或开放项目绑定，必须立即重绘复选框的 disabled/checked 状态；
        // renderer 仍不接收正文，重绘后由 hydration 把敏感草稿写回表单 value。
        renderPromptTemplatePanel(consoleShellState);
        return;
      }
      updatePromptTemplateSubmitState(form);
    });
  }
  form.querySelector("[data-prompt-template-add-variable]")?.addEventListener("click", () => {
    if (promptTemplatePageState.addVariable()) renderPromptTemplatePanel(consoleShellState);
  });
  for (const field of form.querySelectorAll("[data-prompt-template-variable-field]")) {
    field.addEventListener("input", () => {
      const index = Number(field.dataset.promptTemplateVariableIndex);
      const key = field.dataset.promptTemplateVariableField;
      const value = field.type === "checkbox"
        ? field.checked
        : (field.type === "number" ? Number(field.value) : field.value);
      promptTemplatePageState.updateVariable(index, { [key]: value });
      updatePromptTemplateSubmitState(form);
    });
  }
  for (const button of form.querySelectorAll("[data-prompt-template-variable-remove]")) {
    button.addEventListener("click", () => {
      if (promptTemplatePageState.removeVariable(Number(button.dataset.promptTemplateVariableRemove))) {
        renderPromptTemplatePanel(consoleShellState);
      }
    });
  }
  for (const checkbox of form.querySelectorAll("[data-prompt-template-project-id]")) {
    checkbox.addEventListener("change", () => {
      promptTemplatePageState.setBinding("project", checkbox.value, checkbox.checked);
      updatePromptTemplateSubmitState(form);
    });
  }
  for (const checkbox of form.querySelectorAll("[data-prompt-template-model-id]")) {
    checkbox.addEventListener("change", () => {
      promptTemplatePageState.setBinding("model", checkbox.value, checkbox.checked);
      updatePromptTemplateSubmitState(form);
    });
  }
  for (const button of form.querySelectorAll("[data-prompt-template-model-move]")) {
    button.addEventListener("click", () => {
      const moved = promptTemplatePageState.moveRecommendedModel(
        button.dataset.promptTemplateModelMove,
        button.dataset.direction,
      );
      // 顺序变化需要重新 hydration 顺序标签；正文仍只写 textarea.value，不进入 renderer HTML。
      if (moved) renderPromptTemplatePanel(consoleShellState);
    });
  }
  form.querySelector("[data-prompt-template-adopt-conflict]")?.addEventListener("click", () => {
    if (promptTemplatePageState.adoptConflict()) renderPromptTemplatePanel(consoleShellState);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await executePromptTemplateMutation(consoleShellState);
  });
}

/** 输入事件后只更新按钮和固定反馈，不重绘 modal，避免丢失焦点或复制正文。 */
function updatePromptTemplateSubmitState(form) {
  const snapshot = promptTemplatePageState.snapshot();
  const submit = form.querySelector("button[type='submit']");
  if (submit) submit.disabled = !snapshot.dialog?.dirty
    || snapshot.dialog?.busy
    || snapshot.dialog?.conflicted;
  const status = form.querySelector("footer [role='status']");
  if (status) status.textContent = snapshot.dialog?.feedback ?? "";
}

/** 执行 create/PATCH/publish 的稳定写 ticket；同 canonical intent 网络重试由状态机复用 request_id。 */
async function executePromptTemplateMutation(consoleShellState) {
  const owner = promptTemplateOwner(consoleShellState);
  if (!owner || !owner.writeIdentity || !promptTemplatePageState.syncOwner(owner)) return;
  const ticket = promptTemplatePageState.beginMutation();
  if (!ticket) {
    renderPromptTemplatePanel(consoleShellState);
    return;
  }
  renderPromptTemplatePanel(consoleShellState);
  try {
    let result;
    if (ticket.kind === "create") {
      result = await createPromptTemplate({
        controlApiUrl,
        token: owner.token,
        payload: ticket.payload,
        signal: ticket.signal,
        requestId: ticket.requestId,
      });
    } else if (ticket.kind === "metadata") {
      result = await updatePromptTemplate({
        controlApiUrl,
        token: owner.token,
        promptTemplateId: ticket.promptTemplateId,
        payload: ticket.payload,
        signal: ticket.signal,
        requestId: ticket.requestId,
      });
    } else {
      result = await publishPromptTemplateVersion({
        controlApiUrl,
        token: owner.token,
        promptTemplateId: ticket.promptTemplateId,
        payload: ticket.payload,
        signal: ticket.signal,
        requestId: ticket.requestId,
      });
    }
    if (promptTemplatePageState.acceptMutation(ticket, result)) {
      await loadPromptTemplateDetail(consoleShellState);
    }
  } catch (error) {
    // 离页和 owner 漂移的主动取消不产生可见错误，也不能触发冲突 GET。
    if (error?.name === "AbortError") return;
    const accepted = promptTemplatePageState.rejectMutation(ticket, error);
    // 401 是登录身份终止边界，必须先清模板 DOM/闭包再返回登录页。
    if (accepted && Number(error?.status) === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      clearPromptTemplateSessionState();
      renderLogin(authErrorMessage(error), error?.requestId);
      return;
    }
    // 403 只证明模板读写授权漂移，必须重读 /auth/me 与后端导航后再决定落点。
    if (accepted && Number(error?.status) === 403) {
      clearPromptTemplateSessionState();
      await refresh();
      return;
    }
    // 只有明确 409 才读取独立 conflict lane；普通 4xx 和结果未知不会覆盖本地草稿。
    if (accepted && Number(error?.status) === 409) {
      await loadPromptTemplateConflict(consoleShellState);
    }
  }
  renderPromptTemplatePanel(consoleShellState);
}

/** 409 后使用独立 conflict lane 精确读取最新模板，原草稿与失败写 request_id 均不被覆盖。 */
async function loadPromptTemplateConflict(consoleShellState) {
  const owner = promptTemplateOwner(consoleShellState);
  const ticket = owner ? promptTemplatePageState.beginConflictDetail() : null;
  if (!owner || !ticket) return;
  try {
    const result = await getPromptTemplate({
      controlApiUrl,
      token: owner.token,
      promptTemplateId: ticket.promptTemplateId,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    promptTemplatePageState.acceptConflictDetail(ticket, result);
  } catch (error) {
    // 冲突 GET 的主动取消说明 owner 已结束；其他错误只更新 dialog，原草稿仍保留。
    if (error?.name !== "AbortError") promptTemplatePageState.rejectConflictDetail(ticket, error);
  }
}

/** 处理 list/detail/definition 失败；401 清登录，403 立即重拉认证与后端导航。 */
async function rejectPromptTemplateRead(ticket, error, _consoleShellState) {
  if (error?.name === "AbortError") return;
  const accepted = promptTemplatePageState.reject(ticket, error);
  if (!accepted) return;
  // list/detail/definition 的 401 与 mutation 一致，立即结束登录身份并清全部敏感状态。
  if (Number(error?.status) === 401) {
    clearAuthSession();
    authSession = { token: null, context: null };
    clearPromptTemplateSessionState();
    renderLogin(authErrorMessage(error), error?.requestId);
    return;
  }
  // 403 可能只撤销 read 或导航，不得依据旧角色名保留 PAGE-094。
  if (Number(error?.status) === 403) {
    clearPromptTemplateSessionState();
    // 当前 performRefresh 尚在运行时不能递归等待自身；微任务启动新 owner 刷新并淘汰当前代次。
    globalThis.queueMicrotask?.(() => { void refresh(); });
  }
}

/**
 * 构造 PAGE-095 当前完整 owner。
 *
 * read/apply 身份分别冻结；Token 仅供当前调用栈使用，不进入状态 snapshot、DOM 或日志。
 */
function policyTemplateLibraryOwner(consoleShellState, generation = refreshSequence) {
  const item = consoleShellState?.navigation?.navigation_items?.find((candidate) => (
    candidate?.id === "policy-templates"
    && candidate?.target_section === "policy-templates"
    && candidate?.visible !== false
    && candidate?.disabled !== true
  ));
  const readIdentity = policyTemplateReadIdentitySnapshot(authSession.context);
  if (
    consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== "policy-templates"
    || !item
    || !readIdentity
    || !authSession.token
    || !Number.isSafeInteger(generation)
  ) return null;
  return Object.freeze({
    readIdentity,
    applyIdentity: policyTemplateApplyIdentitySnapshot(authSession.context),
    navigation: "policy-templates",
    refreshGeneration: generation,
    token: authSession.token,
  });
}

/** 首次进入 PAGE-095 时只读取固定目录；其他导航不做模板预取。 */
async function loadPolicyTemplateLibraryState(consoleShellState, refreshId) {
  const owner = policyTemplateLibraryOwner(consoleShellState, refreshId);
  if (!owner || !policyTemplateLibraryPageState.syncOwner(owner)) {
    clearPolicyTemplateLibrarySessionState();
    return policyTemplateLibraryPageState.snapshot();
  }
  const current = policyTemplateLibraryPageState.snapshot();
  // 已有目录或确定错误都属于当前 owner 的稳定结果，重复请求会覆盖绑定与预览状态。
  if (current.templates.length || current.pageError) return current;
  const ticket = policyTemplateLibraryPageState.beginCatalog();
  if (!ticket) return policyTemplateLibraryPageState.snapshot();
  try {
    const result = await listPolicyTemplateLibrary({
      controlApiUrl,
      token: owner.token,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    policyTemplateLibraryPageState.acceptCatalog(ticket, result);
  } catch (error) {
    await rejectPolicyTemplateLibrary("catalog", ticket, error);
  }
  return policyTemplateLibraryPageState.snapshot();
}

/** 页面内显式刷新目录；刷新会销毁旧绑定预览、nonce、CAS 和未提交确认。 */
async function refreshPolicyTemplateLibrary(consoleShellState) {
  const owner = policyTemplateLibraryOwner(consoleShellState);
  if (!owner || !policyTemplateLibraryPageState.syncOwner(owner)) return;
  const ticket = policyTemplateLibraryPageState.beginCatalog();
  if (!ticket) return;
  renderPolicyTemplateLibraryPanel(consoleShellState);
  try {
    const result = await listPolicyTemplateLibrary({
      controlApiUrl,
      token: owner.token,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    policyTemplateLibraryPageState.acceptCatalog(ticket, result);
  } catch (error) {
    await rejectPolicyTemplateLibrary("catalog", ticket, error);
  }
  renderPolicyTemplateLibraryPanel(consoleShellState);
}

/** 对当前 exact-set 绑定请求精确影响；后端响应 Header 与正文由状态机共同校验。 */
async function previewPolicyTemplateLibrary(consoleShellState) {
  const owner = policyTemplateLibraryOwner(consoleShellState);
  if (!owner || !policyTemplateLibraryPageState.syncOwner(owner)) return;
  const ticket = policyTemplateLibraryPageState.beginPreview();
  if (!ticket) return;
  renderPolicyTemplateLibraryPanel(consoleShellState);
  try {
    const result = await listPolicyTemplateLibrary({
      controlApiUrl,
      token: owner.token,
      templateId: ticket.policyTemplateId,
      bindings: ticket.bindings,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    policyTemplateLibraryPageState.acceptPreview(ticket, result);
  } catch (error) {
    await rejectPolicyTemplateLibrary("preview", ticket, error);
  }
  renderPolicyTemplateLibraryPanel(consoleShellState);
}

/** 提交状态机冻结的一次 apply 意图；网络未知时 request_id 不由 API client 静默更换。 */
async function submitPolicyTemplateLibrary(consoleShellState) {
  const owner = policyTemplateLibraryOwner(consoleShellState);
  if (!owner || !policyTemplateLibraryPageState.syncOwner(owner)) return;
  const ticket = policyTemplateLibraryPageState.beginApply();
  if (!ticket) return;
  renderPolicyTemplateLibraryPanel(consoleShellState);
  try {
    const result = await applyPolicyTemplate({
      controlApiUrl,
      token: owner.token,
      policyTemplateId: ticket.policyTemplateId,
      templateVersion: ticket.templateVersion,
      previewNonce: ticket.previewNonce,
      previewAsOf: ticket.previewAsOf,
      impactHash: ticket.impactHash,
      bindings: ticket.bindings,
      reason: ticket.reason,
      headEtag: ticket.headEtag,
      candidateEtag: ticket.candidateEtag,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    policyTemplateLibraryPageState.acceptApply(ticket, result);
  } catch (error) {
    await rejectPolicyTemplateLibrary("apply", ticket, error);
  }
  renderPolicyTemplateLibraryPanel(consoleShellState);
}

/** 统一收敛 PAGE-095 错误；只有当前 ticket 接纳的 401/403 才能改变登录或导航。 */
async function rejectPolicyTemplateLibrary(lane, ticket, error) {
  if (error?.name === "AbortError") return;
  const accepted = policyTemplateLibraryPageState.reject(lane, ticket, error);
  if (!accepted) return;
  // 当前请求确认 401 后必须销毁整个 Session，不能让旧 Token 继续访问其他租户接口。
  if (Number(error?.status) === 401) {
    clearAuthSession();
    authSession = { token: null, context: null };
    clearPolicyTemplateLibrarySessionState();
    renderLogin(authErrorMessage(error), error?.requestId);
    return;
  }
  // 权限可能在预览后被撤销，必须整体清除并重新读取后端导航，不能只隐藏 apply 按钮。
  if (Number(error?.status) === 403) {
    clearPolicyTemplateLibrarySessionState();
    globalThis.queueMicrotask?.(() => { void refresh(); });
  }
}

/** 局部重绘 PAGE-095，并重新绑定当前 DOM 事件。 */
function renderPolicyTemplateLibraryPanel(consoleShellState) {
  const shell = document.querySelector(".policy-template-shell");
  const owner = policyTemplateLibraryOwner(consoleShellState);
  if (!shell || !owner) return false;
  shell.innerHTML = renderPolicyTemplateLibrary(policyTemplateLibraryPageState.snapshot());
  bindPolicyTemplateLibraryInteractions(consoleShellState);
  return true;
}

/**
 * 绑定 PAGE-095 目录、绑定、预览、确认和快照跳转。
 *
 * 事件只读状态机接纳的公开 ID；伪造 DOM 仍需通过实时 owner 与后端 permission/tenant/CAS。
 */
function bindPolicyTemplateLibraryInteractions(consoleShellState) {
  const root = document.querySelector("[data-policy-template-page]");
  const owner = policyTemplateLibraryOwner(consoleShellState);
  if (!root || !owner || !policyTemplateLibraryPageState.syncOwner(owner)) return;
  root.querySelector('[data-policy-template-action="refresh"]')?.addEventListener("click", () => {
    void refreshPolicyTemplateLibrary(consoleShellState);
  });
  for (const button of root.querySelectorAll("[data-policy-template-select]")) {
    button.addEventListener("click", () => {
      if (policyTemplateLibraryPageState.selectTemplate(button.dataset.policyTemplateSelect)) {
        renderPolicyTemplateLibraryPanel(consoleShellState);
      }
    });
  }
  for (const input of root.querySelectorAll("[data-policy-template-binding]")) {
    // 输入和 change 共用同一白名单写入口，确保任一绑定变化都会使旧预览与双 CAS 立即失效。
    const update = () => policyTemplateLibraryPageState.setBinding(
      input.dataset.policyTemplateBinding,
      input.value,
    );
    if (input.tagName === "INPUT") input.addEventListener("input", update);
    input.addEventListener("change", () => {
      if (update()) renderPolicyTemplateLibraryPanel(consoleShellState);
    });
  }
  root.querySelector("[data-policy-template-bindings]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    void previewPolicyTemplateLibrary(consoleShellState);
  });
  root.querySelector('[data-policy-template-action="confirm"]')?.addEventListener("click", () => {
    if (policyTemplateLibraryPageState.openConfirmation()) {
      renderPolicyTemplateLibraryPanel(consoleShellState);
      document.querySelector("[data-policy-template-reason]")?.focus();
    }
  });
  for (const button of root.querySelectorAll('[data-policy-template-action="close"]')) {
    button.addEventListener("click", () => {
      if (policyTemplateLibraryPageState.closeConfirmation()) {
        renderPolicyTemplateLibraryPanel(consoleShellState);
      }
    });
  }
  const reason = root.querySelector("[data-policy-template-reason]");
  reason?.addEventListener("input", () => {
    if (!policyTemplateLibraryPageState.setReason(reason.value)) return;
    const applyButton = root.querySelector('[data-policy-template-action="apply"]');
    if (applyButton) applyButton.disabled = !String(reason.value).trim();
  });
  root.querySelector('[data-policy-template-action="apply"]')?.addEventListener("click", () => {
    void submitPolicyTemplateLibrary(consoleShellState);
  });
  root.querySelector('[data-policy-template-action="open-snapshot"]')?.addEventListener("click", () => {
    // 目标仍必须是后端已下发导航；无 policy.read 时按钮查找失败且不会手写 hash。
    document.querySelector('[data-console-nav="policy-snapshots"]')?.click();
  });
  for (const button of root.querySelectorAll("[data-policy-template-remediation]")) {
    button.addEventListener("click", () => {
      // 修复目标来自渲染器固定映射；即使 DOM 被伪造，也只能点击后端已授权并已渲染的导航。
      const target = String(button.dataset.policyTemplateRemediation ?? "");
      const allowedTargets = [
        "policy-snapshots",
        "environments",
        "providers",
        "models",
        "model-aliases",
        "virtual-keys",
        "budgets",
        "system-status",
        "policies",
      ];
      if (!allowedTargets.includes(target)) return;
      document.querySelector(`[data-console-nav="${target}"]`)?.click();
    });
  }
}

/** 同步销毁 PAGE-095 DOM、绑定、预览、确认原因和全部请求 lane。 */
function clearPolicyTemplateLibrarySessionState() {
  policyTemplateLibraryPageState.leave();
  document.querySelector("[data-policy-template-page]")?.replaceChildren();
}

/** tenant、Session、revision、授权指纹或 apply owner 任一变化都销毁旧模板事实。 */
function clearPolicyTemplateLibraryStateOnIdentityChange(previous, current) {
  const previousOwner = JSON.stringify([
    policyTemplateReadIdentitySnapshot(previous),
    policyTemplateApplyIdentitySnapshot(previous),
  ]);
  const currentOwner = JSON.stringify([
    policyTemplateReadIdentitySnapshot(current),
    policyTemplateApplyIdentitySnapshot(current),
  ]);
  // 任一授权 owner 变化都可能代表租户或权限切换，旧目录、nonce 和预览必须同步销毁。
  if (previousOwner !== currentOwner) clearPolicyTemplateLibrarySessionState();
}

/**
 * 构造 PAGE-085 当前授权 owner。
 *
 * owner 只保存 tenant、Session、permission revision 与授权指纹的规范身份串，不包含 Token；
 * 后端导航、权限码和 active 页面任一不满足时返回 null 并失败关闭。
 */
function rolePermissionOwner(consoleShellState, refreshGeneration = null) {
  const authorizedItem = consoleShellState?.navigation?.navigation_items?.find((item) => (
    item?.id === "role-permissions"
    && item?.target_section === "role-permissions"
  ));
  const identity = authorizationIdentitySnapshot(
    authSession.context,
    PERMISSIONS.RBAC_MATRIX_MANAGE,
  );
  const tenantId = String(
    authSession.context?.user?.tenant_id
      ?? authSession.context?.tenant?.tenant_id
      ?? "",
  ).trim();
  if (
    consoleShellState?.state !== "ok"
    || consoleShellState?.activeNavigationItemId !== "role-permissions"
    || !authorizedItem
    || !identity
    || !tenantId
  ) return null;
  const owner = { identity, tenantId, navigation: "role-permissions" };
  return Number.isSafeInteger(refreshGeneration) && refreshGeneration >= 1
    ? { ...owner, refreshGeneration }
    : owner;
}

/** 读取当前租户权限矩阵；迟到、跨身份或跨导航响应由页面状态机拒绝。 */
async function loadRolePermissionState(consoleShellState, refreshId) {
  const owner = rolePermissionOwner(consoleShellState, refreshId);
  if (!owner) {
    rolePermissionPageState.clear();
    // owner 缺失代表页面已经失权或离页，必须返回清空后的快照，不能保留上一身份矩阵。
    return rolePermissionPageState.snapshot();
  }
  const operation = rolePermissionPageState.beginLoad(owner);
  // 状态机拒绝创建 ticket 时只返回当前安全快照，禁止绕过 owner 校验直接发起 GET。
  if (!operation) return rolePermissionPageState.snapshot();
  try {
    const result = await getPermissions({
      controlApiUrl,
      token: authSession.token,
      signal: operation.controller.signal,
    });
    rolePermissionPageState.acceptLoad(
      operation,
      result,
      rolePermissionOwner(consoleShellState, refreshId),
    );
  } catch (error) {
    const currentOwner = rolePermissionOwner(consoleShellState, refreshId);
    const accepted = rolePermissionPageState.rejectLoad(operation, error, currentOwner);
    // 只有仍属于当前 owner 的 401 才能清理 Session；迟到认证错误不能覆盖后续登录。
    if (accepted && error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      rolePermissionPageState.clear();
      renderLogin(authErrorMessage(error), error.requestId);
    }
  }
  // 无论成功或失败都只向渲染层暴露脱敏快照，AbortController 和授权 owner 不进入 DOM。
  return rolePermissionPageState.snapshot();
}

/** 授权身份、租户、Session、revision 或指纹变化时销毁 PAGE-085 全部内存事实。 */
function clearRolePermissionStateOnIdentityChange(previous, current) {
  const previousIdentity = authorizationIdentitySnapshot(previous, PERMISSIONS.RBAC_MATRIX_MANAGE);
  const currentIdentity = authorizationIdentitySnapshot(current, PERMISSIONS.RBAC_MATRIX_MANAGE);
  // 指纹变化即视为新的授权主体，旧草稿和迟到响应不得跨身份复用。
  if (previousIdentity !== currentIdentity) rolePermissionPageState.clear();
}

/** 局部重绘 PAGE-085，避免 checkbox、Tab 和筛选操作触发整站数据预取。 */
function renderRolePermissionPanel(consoleShellState) {
  const panel = document.querySelector(".role-permission-page");
  if (!panel || !rolePermissionOwner(consoleShellState)) return false;
  panel.outerHTML = renderRolePermissionMatrix(
    rolePermissionPageState.snapshot(),
    authSession.context,
  );
  bindRolePermissionInteractions(consoleShellState);
  return true;
}

/**
 * 绑定 PAGE-085 的角色、分类、权限开关和 CAS 保存交互。
 *
 * 所有可编辑上限来自后端 ``grantable_permission_codes``；前端不提交 tenant、角色代码、用户或哈希。
 */
function bindRolePermissionInteractions(consoleShellState) {
  if (!rolePermissionOwner(consoleShellState)) return;
  document.querySelector("#role-permission-category")?.addEventListener("change", (event) => {
    rolePermissionPageState.setCategoryFilter(event.currentTarget.value);
    renderRolePermissionPanel(consoleShellState);
  });
  for (const tab of document.querySelectorAll("[data-rbac-role-tab]")) {
    tab.addEventListener("click", () => {
      rolePermissionPageState.selectRole(tab.dataset.rbacRoleTab);
      renderRolePermissionPanel(consoleShellState);
    });
  }
  for (const input of document.querySelectorAll("[data-rbac-permission-toggle]")) {
    input.addEventListener("change", () => {
      rolePermissionPageState.setPermission(
        input.dataset.roleId,
        input.dataset.permissionCode,
        input.checked,
      );
      renderRolePermissionPanel(consoleShellState);
    });
  }
  document.querySelector("#role-permission-discard")?.addEventListener("click", () => {
    rolePermissionPageState.discardDraft();
    renderRolePermissionPanel(consoleShellState);
  });
  document.querySelector("#role-permission-save")?.addEventListener("click", async () => {
    const token = authSession.token;
    const operation = rolePermissionPageState.beginMutation(rolePermissionOwner(consoleShellState));
    // Token 或冻结写 ticket 任一缺失时禁止发送 PATCH，只重绘当前失败关闭状态。
    if (!token || !operation) {
      renderRolePermissionPanel(consoleShellState);
      return;
    }
    renderRolePermissionPanel(consoleShellState);
    try {
      const result = await replaceRolePermissions({
        controlApiUrl,
        token,
        roleId: operation.roleId,
        expectedRevision: operation.expectedRevision,
        permissionCodes: operation.permissionCodes,
        requestId: operation.requestId,
        signal: operation.controller.signal,
      });
      rolePermissionPageState.acceptMutation(
        operation,
        result,
        authSession.token === token ? rolePermissionOwner(consoleShellState) : null,
      );
      renderRolePermissionPanel(consoleShellState);
    } catch (error) {
      const currentOwner = authSession.token === token ? rolePermissionOwner(consoleShellState) : null;
      const accepted = rolePermissionPageState.rejectMutation(operation, error, currentOwner);
      if (!accepted) return;
      // 当前 owner 的 401 才能注销会话；迟到请求已经在上一行被状态机拒绝。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        rolePermissionPageState.clear();
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      // 403 可能表示授权刚被收窄；409 需要重新读取权威 revision，同时状态机会保留 dirty 草稿。
      if (error?.status === 403 || error?.status === 409) {
        if (error.status === 403) rolePermissionPageState.clear();
        await refresh();
        return;
      }
      renderRolePermissionPanel(consoleShellState);
    }
  });
}

/**
 * 读取控制台导航、偏好和当前 active item。
 * 安全：active item 只是 UI 候选值，后端仍会按登录态角色确认是否可见。
 */
async function loadConsoleShellState() {
  const hashState = readNavigationHashState();
  try {
    const [navigation, preferences] = await Promise.all([
      getConsoleNavigation({
        controlApiUrl,
        token: authSession.token,
      }),
      getConsolePreferences({ controlApiUrl, token: authSession.token }),
    ]);
    // /auth/me 与 /console/navigation 必须携带完全一致的权限集合、revision 和指纹；
    // 任一漂移都整包失败关闭，禁止回退到历史 required_roles 或本地角色名。
    const authorizedNavigation = authorizeNavigationSnapshot(navigation, authSession.context);
    // hash 只在后端可见导航集合内解析；非法入口本地回退，后端直接调用仍保留 422 权限边界。
    const resolution = resolveVisibleNavigation(authorizedNavigation, hashState.value, {
      requestedRejected: hashState.rejected,
    });
    if (resolution.rejectedRequestedItem) {
      replaceNavigationHash("");
    }
    const resolvedNavigation = {
      ...authorizedNavigation,
      active_navigation_item_id: resolution.activeItemId,
    };
    return {
      state: "ok",
      navigation: resolvedNavigation,
      preferences,
      activeNavigationItemId: resolution.activeItemId,
      ...(resolution.rejectedRequestedItem
        ? { warning: "当前角色不可访问该入口，已回到概览。" }
        : {}),
      actionError: consoleShellActionError,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      error,
      activeNavigationItemId: null,
    };
  }
}

/**
 * 切换控制台导航入口，并写入用户偏好和 page_viewed 事件。
 * 关键边界：这里不传 tenant_id/user_id，后端只能使用当前登录态写入当前租户数据。
 */
async function switchConsoleNavigation(button, consoleShellState) {
  const token = authSession.token;
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!token) {
    renderLogin();
    return;
  }
  const itemId = button.dataset.consoleNav;
  const targetSection = button.dataset.consoleTarget || itemId;
  if (!itemId) {
    return;
  }
  // 导航 owner 在任何 await 前冻结；后续点击、全页刷新或换 Session 任一发生都会使它失效。
  // Token 只在闭包内做等值判断，绝不能进入 DOM、遥测、日志或错误消息。
  const navigationOwner = Object.freeze({
    generation: ++refreshSequence,
    token,
  });
  const navigationOwnerCurrent = () => isConsoleNavigationOwnerCurrent(
    navigationOwner,
    {
      generation: refreshSequence,
      token: authSession.token,
    },
  );
  // errorOwner 初始等于导航 owner；调用 refresh 时会同步切换为该 refresh 自己推进后的代次。
  // 这样 catch 既能处理本轮 refresh 的真实失败，也不会让更晚导航的异常写入当前页面。
  let errorOwner = navigationOwner;
  const errorOwnerCurrent = () => isConsoleNavigationOwnerCurrent(
    errorOwner,
    {
      generation: refreshSequence,
      token: authSession.token,
    },
  );
  // 任意导航跳转都视为调试组件销毁：取消活动流、使旧回调失效，并清除仅驻留页面内存的预览。
  gatewayDebugLifecycle.leave();
  gatewayDebugState = {};
  if (itemId !== "enterprise-chat") {
    // 导航偏好写入前就 abort PAGE-093 并清正文，迟到 assistant 响应不得落到目标页面。
    clearEnterpriseChatSessionState();
  }
  if (itemId !== "prompt-templates") {
    // 导航偏好写入前清空 PAGE-094 DOM 和闭包，迟到 GET/PATCH 不得落到目标页。
    clearPromptTemplateSessionState();
  }
  if (itemId !== "overview") {
    // 离开首页必须在偏好请求前同步清除 role-view owner 和卡片，迟到响应不能跨导航写回。
    clearRoleDashboardSessionState();
  } else {
    // overview 访问事件只能在 role-view 成功并完成 DOM 投影后发送，这里只冻结侧栏来源。
    roleDashboardEntrySource = "sidebar";
  }
  if (itemId !== "provider-health") {
    // 导航偏好请求发出前立即作废状态 GET 和 visit owner，迟到 200/401/403 均不能影响下一页面。
    providerStatusPageState.clear();
    providerStatusVisitTracker.leave();
    providerStatusEventWarning = null;
  }
  if (itemId !== "adapter-specs") {
    // 导航请求发出前销毁 PAGE-090 owner，迟到目录和迟到 401/403 均不能影响目标页面或新 Session。
    adapterSpecPageState.clear();
    adapterSpecFilters = { view: "default", status: "", provider_type: "" };
  }
  if (itemId !== "adapter-contract-runs") {
    // PAGE-091 的列表、详情与 opaque cursor 必须在偏好写入前销毁，不能跨导航或下一 Session 残留。
    adapterContractRunPageState.clear();
  }
  // 离开用量页立即终止异步轮询，不能等待偏好写入或下一轮完整刷新。
  if (itemId !== "usage") {
    clearUsageExportSessionState();
  }
  if (itemId !== "audit-logs") {
    // 导航偏好请求发出前即销毁审计制品 owner，迟到 200 不能触发下载或把旧租户 hash 写回新页面。
    clearAuditExportSessionState();
  }
  if (itemId !== "role-permissions") {
    // 导航请求发出前先销毁 PAGE-085 owner，迟到 GET/PATCH 不得回写下一页面或下一身份。
    rolePermissionPageState.clear();
  }
  if (itemId !== "policies") {
    // 离开策略页必须在偏好/API 请求前取消 validate/save，并从 DOM 与内存清除 DSL 草稿。
    clearPolicyEditorSessionState();
  }
  if (itemId !== "policy-snapshots") {
    // 离开发布页必须在导航请求前取消 current/history 与写请求，避免旧 tenant 的原因和 CAS 回写新页面。
    clearPolicySnapshotSessionState();
  }
  if (itemId !== "policy-simulator") {
    // 导航请求发出前先清空 textarea 并 abort，不能等待下一轮后端导航结果。
    clearPolicyDryRunSessionState();
  }
  if (itemId !== "intent-security") {
    // PAGE-096 离页在导航偏好请求前清正文和 POST owner，迟到 200/401/403 不得影响新页面。
    clearIntentSecuritySessionState();
  }
  if (itemId !== "routing-policies") {
    // PAGE-067 保存 ticket 在导航请求前失效，迟到 200/409 不得覆盖下一页面或下一身份。
    clearRoutingPolicySessionState();
  }
  if (itemId !== "model-quality-scores") {
    // PAGE-069 的 GET/PATCH owner 在导航请求前失效，迟到 200/401/409 均不得影响下一页面。
    clearModelQualityScoreSessionState();
  }
  if (itemId !== "error-maps") {
    // 导航请求发出前销毁 PAGE-081 dialog、焦点 owner 和异步 ticket，不能让旧租户响应回写新页面。
    clearErrorMapSessionState();
  }
  if (!REQUEST_LOG_NAVIGATION_IDS.has(itemId)) {
    // 导航请求发出前销毁调用日志 owner，不能等待后端偏好返回后才取消上一租户详情请求。
    clearRequestLogSessionState();
  }
  if (itemId !== "request-explanations") {
    // 一次性 handoff 与解释 owner 必须在非目标导航发出前销毁，不能跨页面或 Session 滞留。
    clearRequestExplanationSessionState();
  }
  if (itemId !== "feedback-summary") {
    // PAGE-071 导航切换在偏好请求前就推进 GET generation，迟到 200/401 不得影响新页面或新 Session。
    clearRequestFeedbackSummarySessionState();
  }
  if (itemId !== "budgets") {
    // 导航偏好请求发出前先销毁预算 owner，旧 POST/PATCH 完成后不得触发目标页面 refresh。
    clearBudgetSessionState();
  }
  if (itemId !== "cost-anomalies") {
    // 导航请求前终止成本异常读写 owner，迟到响应不能把上一租户证据或动作结果写回新页面。
    costAnomalyPageState.clear();
  }
  if (itemId !== "session-risks") {
    // 导航偏好请求发出前销毁 PAGE-098 双请求 owner，避免旧风险事实回写新页面。
    sessionRiskPageState.clear();
  }
  if (itemId !== "files") {
    // 导航偏好写入前取消文件 GET/POST 并销毁文件名与 cursor，迟到响应不得落入目标页面。
    filePageState.clear();
  }
  if (itemId !== "webhook-dead-letters") {
    // PAGE-083 在偏好写入前就清除读取、写入、幂等键与轮询，旧回调不能越过导航边界。
    webhookDeadLetterPageState.clear();
  }
  consoleShellActionError = null;
  // navigationOwner 创建时已经推进 refreshSequence，使正在运行的旧刷新立即失效；
  // 不能等偏好写入完成后才阻止旧页面回写。
  replaceNavigationHash(itemId);
  // 迁移入口的访问事件只能由渲染后 tracker 写入；这里仅标记来源并清理上次 visit 的非阻断告警。
  if (itemId === "migration-guide") {
    migrationGuideEntrySource = "sidebar";
    migrationGuideEventWarning = null;
  }
  // 竞品页与迁移页都由渲染后 tracker 独占事件写入；导航切换这里只记录进入来源。
  if (itemId === "competitor-comparison") {
    competitorEntrySource = "sidebar";
    competitorEventWarning = null;
  }
  // PAGE-088 使用独立 status.viewed；这里只冻结侧栏来源并清除上一次进入的非阻断告警。
  if (itemId === "provider-health") {
    providerStatusEntrySource = "sidebar";
    providerStatusEventWarning = null;
  }
  try {
    await updateConsolePreferences({
      controlApiUrl,
      token,
      activeNavigationItemId: itemId,
    });
    // 较新的导航或 Session 已接管时，旧操作不得继续写 page_viewed，也不得碰当前 DOM。
    if (!navigationOwnerCurrent()) {
      return;
    }
    // 专属遥测页面跳过通用 page-view，避免与各自的首屏 tracker 形成双写。
    if (
      !["overview", "migration-guide", "competitor-comparison", "request-explanations"].includes(itemId)
      && itemId !== "provider-health"
    ) {
      await recordConsolePageView({
        controlApiUrl,
        token,
        navigationItemId: itemId,
        targetSection,
        path: currentNavigationPath(itemId),
        eventType: "console.page_viewed",
        payload: {
          // 后端对所有页面事件都要求 source/hash/viewport_class 完整配对，不能只在专属事件中发送视口。
          source: "sidebar",
          hash: `#${itemId}`,
          viewport_class: globalThis.window?.innerWidth < 720 ? "mobile" : "desktop",
        },
      });
      // page_viewed 返回时再次核对 owner；迟到成功不能渲染旧 loading 或启动旧刷新。
      if (!navigationOwnerCurrent()) {
        return;
      }
    }
    // PAGE-088 只有通过双权限和后端导航校验后才能在真实状态 GET 前渲染 loading；503 与空态仍算有效访问。
    if (itemId === "provider-health") {
      const projectedState = renderProviderStatusLoading(app, {
        authContext: authSession.context,
        consoleShellState,
        i18n: currentI18n,
      });
      if (projectedState) {
        // loading 替换了整个 shell，必须重绑导航、注销和唯一 status.viewed tracker。
        bindAuthenticatedEvents(projectedState);
      }
    }
    // PAGE-090 的 loading 只在实时权限和后端导航 owner 同时成立时投影，随后 GET 仍由独立 owner 校验。
    if (itemId === "adapter-specs") {
      const projectedState = renderAdapterSpecsLoading(app, {
        authContext: authSession.context,
        consoleShellState,
        i18n: currentI18n,
      });
      // loading 会替换 shell DOM，只有授权投影成功时才重新绑定认证事件，空结果保持原页面不变。
      // loading 替换整个 shell 后必须重绑；未授权 null 结果保持旧 DOM 且不绑定 PAGE-091。
      if (projectedState) bindAuthenticatedEvents(projectedState);
    }
    // PAGE-091 loading 也要求后端导航与实时 provider.health.read 同时成立，viewer 不生成页面 DOM。
    if (itemId === "adapter-contract-runs") {
      const projectedState = renderAdapterContractRunsLoading(app, {
        authContext: authSession.context,
        consoleShellState,
        i18n: currentI18n,
      });
      // loading 替换整个 shell 后必须重绑；未授权 null 结果保持旧 DOM 且不创建 PAGE-091 事件。
      if (projectedState) bindAuthenticatedEvents(projectedState);
    }
    if (itemId === "routing-policies") {
      // PAGE-072 的读取可能受慢网络影响；偏好与 page_viewed 均落库后先展示真实 loading。
      // helper 会复核当前角色和后端导航，失败时保持旧 DOM，不能凭 data 属性伪造受限入口。
      const projectedState = renderRoutingPoliciesLoading(app, {
        authContext: authSession.context,
        consoleShellState,
        i18n: currentI18n,
      });
      if (projectedState) {
        // innerHTML 已替换，必须给新 shell 重新绑定导航和注销；新导航会推进 refreshSequence，
        // 因而当前慢刷新即使迟到也会在最终渲染门禁处被丢弃。
        bindAuthenticatedEvents(projectedState);
      }
    }
    // loading 渲染和事件重绑虽为同步操作，refresh 前仍显式复核 owner，固定“旧导航不发新读取”的边界。
    if (!navigationOwnerCurrent()) {
      return;
    }
    // refresh() 在返回 Promise 前同步推进 refreshSequence；无 await 的赋值窗口不会被其他事件插入。
    const refreshPromise = refresh();
    errorOwner = Object.freeze({ generation: refreshSequence, token });
    await refreshPromise;
  } catch (error) {
    // 旧 Session 的迟到 401/异常不得清除新 Session、写入当前错误态或触发另一轮 refresh。
    if (!errorOwnerCurrent()) {
      return;
    }
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    consoleShellActionError = error;
    await refresh();
  }
}

/**
 * 绑定快速开始代码复制按钮。
 * 复制内容直接从代码块读取，不放入 data 属性，避免代码文本或 Key 占位经过属性转义后失真。
 */
function bindQuickstartCopyButtons() {
  for (const copyButton of document.querySelectorAll("[data-copy-code-target]")) {
    copyButton.addEventListener("click", async () => {
      const target = document.getElementById(copyButton.dataset.copyCodeTarget);
      const value = target?.textContent ?? "";
      // SDK 命令反馈必须留在当前语言面板；快速开始仍使用其自身的局部反馈节点。
      const feedback = copyButton.closest(".quickstart-example")?.querySelector(".quickstart-copy-feedback")
        ?? copyButton.closest("[data-sdk-language-panel]")?.querySelector("[data-sdk-feedback]");
      if (!value || !navigator.clipboard?.writeText) {
        if (feedback) {
          feedback.textContent = "当前浏览器不支持自动复制";
        }
        return;
      }
      await navigator.clipboard.writeText(value);
      if (feedback) {
        feedback.textContent = "已复制";
      }
    });
  }
}

/**
 * 从 location.hash 读取导航解析状态，保留非法非空值和无值之间的区别。
 */
function readNavigationHashState() {
  return parseNavigationHash(globalThis.location?.hash);
}

/**
 * 返回语法合法的导航 ID，供事件来源和页面专属加载器读取。
 * 非法值在 loadConsoleShellState 中清理；其他调用点只把它视为无可用目标。
 */
function readActiveNavigationHash() {
  return readNavigationHashState().value;
}

/**
 * 更新浏览器 hash；空值表示回到概览且不保留非法 hash。
 */
function replaceNavigationHash(itemId) {
  if (!globalThis.history || !globalThis.location) {
    return;
  }
  const nextHash = itemId ? `#${encodeURIComponent(itemId)}` : "";
  globalThis.history.replaceState(
    null,
    "",
    `${globalThis.location.pathname}${globalThis.location.search}${nextHash}`,
  );
}

/**
 * 生成页面访问事件中的规范路径，只保留后端导航 ID 对应的根路径与 hash。
 * 查询串可能携带调试参数或临时上下文，不能进入访问事件，也不能参与后端事件配对。
 */
function currentNavigationPath(itemId) {
  return `/#${encodeURIComponent(itemId)}`;
}

/**
 * 执行 控制台主应用 的 `loadTenantState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadTenantState() {
  // 租户设置页面只消费后端 tenant.settings.read，不能因所有用户都已登录而自动预取。
  if (!hasPermission(authSession.context, PERMISSIONS.TENANT_SETTINGS_READ)) {
    return null;
  }
  try {
    // 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return await getCurrentTenant({ controlApiUrl, token: authSession.token });
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 执行 控制台主应用 的 `loadUsersState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadUsersState(consoleShellState) {
  // 用户邮箱属于租户 PII，只在后端已确认 PAGE-086 为当前导航时读取，定时刷新不得跨页面预取。
  if (
    consoleShellState?.activeNavigationItemId !== "users"
    || !hasPermission(authSession.context, PERMISSIONS.USER_MANAGE)
  ) {
    return null;
  }
  try {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return await listUsers({ controlApiUrl, token: authSession.token });
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 执行 控制台主应用 的 `loadProvidersState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadProvidersState() {
  if (!hasPermission(authSession.context, PERMISSIONS.PROVIDER_READ)) {
    return null;
  }
  try {
    const state = await listProviders({ controlApiUrl, token: authSession.token });
    const providerKeyEntries = await Promise.all(
      (state.providers ?? []).map(async (provider) => {
        try {
          return [
            provider.provider_id,
            await listProviderKeys({
              controlApiUrl,
              token: authSession.token,
              providerId: provider.provider_id,
            }),
          ];
        } catch (error) {
          return [
            provider.provider_id,
            {
              state: "error",
              message: authErrorMessage(error),
              requestId: error?.requestId ?? "-",
            },
          ];
        }
      }),
    );
    return {
      ...state,
      providerKeysByProviderId: Object.fromEntries(providerKeyEntries),
      actionError: providerActionError,
      feedback: providerFeedback,
      lastTestResult: providerLastTestResult,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 执行 控制台主应用 的 `loadModelsState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadModelsState() {
  if (!hasPermission(authSession.context, PERMISSIONS.MODEL_READ)) {
    return null;
  }
  try {
    const state = await listModels({ controlApiUrl, token: authSession.token });
    return {
      ...state,
      actionError: modelActionError,
      feedback: modelFeedback,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }

}

/**
 * 仅在后端权限快照包含 ``policy.publish`` 且导航为 policy-snapshots 时并行读取 current/history。
 * 请求前后同时核对刷新代次、hash、tenant/user/role/session 和 lifecycle owner，旧响应不得覆盖新身份页面。
 */
async function loadPolicySnapshotsState(consoleShellState, refreshId) {
  const identity = policySnapshotIdentitySnapshot(
    policySnapshotIdentityFromAuthContext(authSession.context),
  );
  const hashNavigation = readActiveNavigationHash();
  const loadArguments = {
    canPublish: hasPermission(authSession.context, PERMISSIONS.POLICY_PUBLISH),
    activeNavigationItemId: consoleShellState?.activeNavigationItemId,
    hashNavigation,
    refreshId,
    currentRefreshId: refreshSequence,
    requestIdentity: identity,
    currentIdentity: identity,
  };
  if (!shouldLoadPolicySnapshots(loadArguments)) {
    // 未获发布权限、非 active 发布页或旧刷新不能后台预取 change_reason、actor 与强 ETag。
    return null;
  }

  const ticket = policySnapshotLoadLifecycle.begin({
    refreshId,
    navigation: "policy-snapshots",
    identity,
  });
  try {
    const [current, history] = await Promise.all([
      getCurrentPolicySnapshot({
        controlApiUrl,
        token: authSession.token,
        signal: ticket.signal,
      }),
      listPolicySnapshots({
        controlApiUrl,
        token: authSession.token,
        limit: policySnapshotHistoryFilters.limit,
        offset: policySnapshotHistoryFilters.offset,
        signal: ticket.signal,
      }),
    ]);
    const currentIdentity = policySnapshotIdentitySnapshot(
      policySnapshotIdentityFromAuthContext(authSession.context),
    );
    const currentOwner = {
      refreshId,
      navigation: "policy-snapshots",
      identity: currentIdentity,
    };
    const stillAuthorized = shouldLoadPolicySnapshots({
      ...loadArguments,
      hashNavigation: readActiveNavigationHash(),
      currentRefreshId: refreshSequence,
      currentIdentity,
    });
    // 授权门禁或 owner 任一失效都必须丢弃整组 current/history，不能只提交其中一个响应。
    if (!stillAuthorized || !policySnapshotLoadLifecycle.isCurrent(ticket, currentOwner)) {
      // fetch 即使忽略 AbortSignal，也不能把旧租户的原因、操作者或 CAS 写回当前 DOM。
      return null;
    }
    policySnapshotsState = {
      ...history,
      ...current,
      state: "ok",
      policy_snapshots: Array.isArray(history.policy_snapshots) ? history.policy_snapshots : [],
      total: Number(history.total ?? 0),
      limit: Number(history.limit ?? policySnapshotHistoryFilters.limit),
      offset: Number(history.offset ?? policySnapshotHistoryFilters.offset),
    };
    return buildPolicySnapshotsRenderState();
  } catch (error) {
    // Abort 是主动生命周期收口，不应被包装成服务故障或重新展示旧快照元数据。
    if (isAbortError(error)) {
      // 主动取消属于页面生命周期，不显示服务故障，也不恢复任何旧历史或 ETag。
      return null;
    }
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      clearPolicySnapshotSessionState();
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403 必须销毁上一轮快照正文；其他错误也不复用可能已过时的强 CAS，以失败关闭写按钮。
    policySnapshotsState = {
      state: "error",
      code: error?.code ?? "policy_snapshot.unavailable",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      suggestion: error?.suggestion ?? "",
    };
    return buildPolicySnapshotsRenderState();
  } finally {
    policySnapshotLoadLifecycle.complete(ticket);
  }
}

/**
 * 组装策略快照渲染状态。
 * ETag、原因和未决 request_id 只存在当前内存；renderer 不把 CAS 写入 URL 或持久存储。
 */
function buildPolicySnapshotsRenderState() {
  if (!policySnapshotsState) {
    return null;
  }
  const identity = policySnapshotIdentitySnapshot(
    policySnapshotIdentityFromAuthContext(authSession.context),
  );
  return {
    ...policySnapshotsState,
    actionError: policySnapshotActionError,
    feedback: policySnapshotFeedback,
    operationStatus: policySnapshotOperationStatus,
    publishReason: policySnapshotPublishReason,
    rollbackTargetId: policySnapshotRollbackTargetId,
    rollbackReason: policySnapshotRollbackReason,
    pendingOperation: policySnapshotOperationMemory.read(identity),
  };
}

/**
 * 只重绘策略快照 panel 并重新绑定局部事件。
 * 局部更新用于输入、展开回滚和网络未知反馈，不会复制控制台导航监听器或触发其他租户 API。
 */
function renderPolicySnapshotPanelFromMemory() {
  const panel = document.querySelector(".policy-snapshots-panel");
  if (
    !panel
    || currentRenderedNavigation() !== "policy-snapshots"
    || !hasPermission(authSession.context, PERMISSIONS.POLICY_PUBLISH)
  ) {
    // 离页或权限撤销后的旧回调不能重新创建发布 DOM。
    return;
  }
  panel.innerHTML = renderPolicySnapshotsPanel(
    buildPolicySnapshotsRenderState(),
    authSession.context,
  );
  bindPolicySnapshotInteractions({ activeNavigationItemId: "policy-snapshots" });
}

/**
 * 绑定发布、上一版回滚、未知结果放弃和历史分页事件。
 * DOM 可见性不构成授权，所有写操作仍携带 Bearer、强 CAS 和稳定 request_id 由后端再次校验。
 */
function bindPolicySnapshotInteractions(consoleShellState) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.POLICY_PUBLISH)
    || consoleShellState?.activeNavigationItemId !== "policy-snapshots"
    || !policySnapshotsState
  ) {
    // 未获发布权限或非 active 页面既不绑定隐藏表单，也不能通过事件路径触发快照 API。
    return;
  }

  const publishForm = document.querySelector("#policy-snapshot-publish-form");
  publishForm?.addEventListener("input", () => {
    policySnapshotPublishReason = String(new FormData(publishForm).get("reason") ?? "");
    syncPolicySnapshotSubmitButton(publishForm, "publish");
  });
  publishForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    policySnapshotPublishReason = String(new FormData(publishForm).get("reason") ?? "");
    await submitPolicySnapshotPublish();
  });

  for (const openButton of document.querySelectorAll("[data-policy-snapshot-rollback-open]")) {
    openButton.addEventListener("click", () => {
      const targetId = openButton.dataset.policySnapshotRollbackOpen;
      const allowed = policySnapshotsState?.policy_snapshots?.some(
        (item) => item.policy_snapshot_id === targetId
          && item.snapshot_version === policySnapshotsState.current_snapshot?.previous_snapshot_version,
      );
      if (!allowed) {
        // 伪造或过时 data 属性不会形成请求；后端路径接口仍对跨租户 ID 返回统一 404。
        return;
      }
      policySnapshotRollbackTargetId = targetId;
      policySnapshotRollbackReason = "";
      policySnapshotActionError = null;
      policySnapshotFeedback = "";
      renderPolicySnapshotPanelFromMemory();
    });
  }

  const rollbackForm = document.querySelector("[data-policy-snapshot-rollback-form]");
  rollbackForm?.addEventListener("input", () => {
    policySnapshotRollbackReason = String(new FormData(rollbackForm).get("reason") ?? "");
    syncPolicySnapshotSubmitButton(rollbackForm, "rollback");
  });
  rollbackForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    policySnapshotRollbackReason = String(new FormData(rollbackForm).get("reason") ?? "");
    await submitPolicySnapshotRollback(rollbackForm.dataset.policySnapshotRollbackForm);
  });
  rollbackForm?.querySelector("[data-policy-snapshot-rollback-cancel]")?.addEventListener("click", () => {
    policySnapshotRollbackTargetId = null;
    policySnapshotRollbackReason = "";
    policySnapshotActionError = null;
    renderPolicySnapshotPanelFromMemory();
  });

  for (const abandonButton of document.querySelectorAll("[data-policy-snapshot-abandon]")) {
    abandonButton.addEventListener("click", async () => {
      await abandonPendingPolicySnapshotOperation();
    });
  }

  for (const pageButton of document.querySelectorAll("[data-policy-snapshot-page]")) {
    pageButton.addEventListener("click", async () => {
      const direction = pageButton.dataset.policySnapshotPage;
      const limit = Number(policySnapshotsState.limit ?? 20);
      const offset = Number(policySnapshotsState.offset ?? 0);
      policySnapshotHistoryFilters = {
        limit,
        offset: direction === "previous" ? Math.max(0, offset - limit) : offset + limit,
      };
      policySnapshotActionError = null;
      await refresh();
    });
  }
}

/**
 * 根据最新表单原因更新提交按钮。
 * 候选与 CAS 状态继续来自内存中的后端响应，输入事件不能自行伪造“可发布”。
 */
function syncPolicySnapshotSubmitButton(form, kind) {
  const button = form.querySelector("button[type='submit']");
  if (!button) {
    return;
  }
  const reason = String(new FormData(form).get("reason") ?? "");
  const pending = policySnapshotOperationMemory.read(
    policySnapshotIdentitySnapshot(policySnapshotIdentityFromAuthContext(authSession.context)),
  );
  const busy = policySnapshotOperationStatus !== "idle";
  const canRetryPublish = pending?.kind === "publish";
  const candidateReady = policySnapshotsState?.candidate_status === "valid"
    && Boolean(policySnapshotsState?.headEtag)
    && Boolean(policySnapshotsState?.candidateEtag)
    && policySnapshotsState?.candidate_matches_current !== true;
  const canRetryRollback = pending?.kind === "rollback"
    && pending.targetId === policySnapshotRollbackTargetId;
  const contractReady = kind === "publish"
    ? candidateReady || canRetryPublish
    : Boolean(policySnapshotsState?.headEtag) || canRetryRollback;
  button.disabled = busy || !contractReady || !isPolicySnapshotReasonValid(reason);
}

/**
 * 提交发布双 CAS。
 * 网络结果未知时保留原 reason/head/candidate/request_id；下一次点击只允许重试完全相同的业务操作。
 */
async function submitPolicySnapshotPublish() {
  const token = authSession.token;
  const identity = policySnapshotIdentitySnapshot(
    policySnapshotIdentityFromAuthContext(authSession.context),
  );
  const reason = policySnapshotPublishReason.trim();
  // 会话、身份或原因任一不可信时禁止占用幂等键并发起发布请求。
  if (
    !token
    || !identity
    || !hasPermission(authSession.context, PERMISSIONS.POLICY_PUBLISH)
    || !isPolicySnapshotReasonValid(reason)
  ) {
    // 本地按钮门禁可能被手工修改，缺失会话或非法原因仍不会发送网络请求。
    return;
  }
  const pending = policySnapshotOperationMemory.read(identity);
  const descriptor = {
    kind: "publish",
    targetId: "",
    reason,
    headEtag: pending?.kind === "publish" ? pending.headEtag : policySnapshotsState?.headEtag,
    candidateEtag: pending?.kind === "publish" ? pending.candidateEtag : policySnapshotsState?.candidateEtag,
  };
  const acquired = policySnapshotOperationMemory.acquire(identity, descriptor);
  if (acquired.state === "blocked") {
    showPolicySnapshotPendingConflict(acquired.pending);
    return;
  }
  if (!confirmPolicySnapshotAction("确认发布 active 策略候选？新请求将在数据面下一轮轮询后使用新版本。")) {
    // 首次操作在确认前不应占用票据；这里若刚创建票据需要释放，重试票据则继续保留。
    if (!acquired.reused) {
      policySnapshotOperationMemory.markDefinitive(acquired.operation);
    }
    return;
  }

  const operation = acquired.operation;
  const owner = beginPolicySnapshotWrite("publishing", identity);
  try {
    const result = await publishPolicySnapshot({
      controlApiUrl,
      token,
      reason: operation.reason,
      headEtag: operation.headEtag,
      candidateEtag: operation.candidateEtag,
      requestId: operation.requestId,
      signal: owner.signal,
    });
    if (!isCurrentPolicySnapshotWrite(owner)) {
      // 离页后成功的提交由下一次 GET 对账，旧回调不得重建 DOM 或跨身份展示原因。
      return;
    }
    policySnapshotOperationMemory.markDefinitive(operation);
    finishPolicySnapshotWrite(owner);
    policySnapshotPublishReason = "";
    policySnapshotActionError = null;
    policySnapshotFeedback = result.idempotent_replay
      ? `已确认 v${result.policy_snapshot?.snapshot_version ?? "-"}，本次为原 request_id 幂等回放。`
      : `已发布 v${result.policy_snapshot?.snapshot_version ?? "-"}；等待数据面在轮询周期内拉取。`;
    await refresh();
  } catch (error) {
    await handlePolicySnapshotWriteError(error, operation, owner);
  }
}

/**
 * 提交上一激活版本回滚。
 * 回滚只携带原 head CAS，不发送 candidate ETag，也不会修改 active Policy 候选。
 */
async function submitPolicySnapshotRollback(targetId) {
  const token = authSession.token;
  const identity = policySnapshotIdentitySnapshot(
    policySnapshotIdentityFromAuthContext(authSession.context),
  );
  const reason = policySnapshotRollbackReason.trim();
  const target = policySnapshotsState?.policy_snapshots?.find(
    (item) => item.policy_snapshot_id === targetId
      && item.snapshot_version === policySnapshotsState.current_snapshot?.previous_snapshot_version,
  );
  // 会话、上一版对象和原因必须同时有效，避免伪造 data 属性绕过页面回滚范围。
  if (
    !token
    || !identity
    || !hasPermission(authSession.context, PERMISSIONS.POLICY_PUBLISH)
    || !target
    || !isPolicySnapshotReasonValid(reason)
  ) {
    // 只允许页面权威历史中的上一版形成请求；伪造 ID 仍由后端做最终租户和对象校验。
    return;
  }
  const pending = policySnapshotOperationMemory.read(identity);
  const descriptor = {
    kind: "rollback",
    targetId: target.policy_snapshot_id,
    reason,
    headEtag: pending?.kind === "rollback" ? pending.headEtag : policySnapshotsState?.headEtag,
    candidateEtag: "",
  };
  const acquired = policySnapshotOperationMemory.acquire(identity, descriptor);
  if (acquired.state === "blocked") {
    showPolicySnapshotPendingConflict(acquired.pending);
    return;
  }
  if (!confirmPolicySnapshotAction(
    `确认复制 v${target.snapshot_version} 内容并创建新的更高版本？active Policy 候选不会改变。`,
  )) {
    if (!acquired.reused) {
      policySnapshotOperationMemory.markDefinitive(acquired.operation);
    }
    return;
  }

  const operation = acquired.operation;
  const owner = beginPolicySnapshotWrite("rolling_back", identity);
  try {
    const result = await rollbackPolicySnapshot({
      controlApiUrl,
      token,
      policySnapshotId: operation.targetId,
      reason: operation.reason,
      headEtag: operation.headEtag,
      requestId: operation.requestId,
      signal: owner.signal,
    });
    if (!isCurrentPolicySnapshotWrite(owner)) {
      return;
    }
    policySnapshotOperationMemory.markDefinitive(operation);
    finishPolicySnapshotWrite(owner);
    policySnapshotRollbackTargetId = null;
    policySnapshotRollbackReason = "";
    policySnapshotActionError = null;
    policySnapshotFeedback = result.idempotent_replay
      ? `已确认回滚版本 v${result.policy_snapshot?.snapshot_version ?? "-"}，本次为幂等回放。`
      : `已创建回滚版本 v${result.policy_snapshot?.snapshot_version ?? "-"}；active 候选保持不变。`;
    await refresh();
  } catch (error) {
    await handlePolicySnapshotWriteError(error, operation, owner);
  }
}

/** 创建快照写 owner；写 controller 不与 current/history 读取共享，避免刷新取消已提交事务。 */
function beginPolicySnapshotWrite(status, identity) {
  policySnapshotOperationController?.abort();
  const controller = new AbortController();
  const owner = {
    generation: ++policySnapshotOperationGeneration,
    identity,
    navigation: "policy-snapshots",
    controller,
    signal: controller.signal,
  };
  policySnapshotOperationController = controller;
  policySnapshotOperationStatus = status;
  policySnapshotActionError = null;
  policySnapshotFeedback = "";
  renderPolicySnapshotPanelFromMemory();
  return owner;
}

/** 核对写回调仍属于当前 tenant/user/session 和 active 发布页。 */
function isCurrentPolicySnapshotWrite(owner) {
  const currentIdentity = policySnapshotIdentitySnapshot(
    policySnapshotIdentityFromAuthContext(authSession.context),
  );
  return owner.generation === policySnapshotOperationGeneration
    && owner.controller === policySnapshotOperationController
    && !owner.signal.aborted
    && owner.identity === currentIdentity
    && hasPermission(authSession.context, PERMISSIONS.POLICY_PUBLISH)
    && currentRenderedNavigation() === "policy-snapshots";
}

/** 结束当前写 owner；旧回调不得把新操作状态改回 idle。 */
function finishPolicySnapshotWrite(owner) {
  if (owner.generation === policySnapshotOperationGeneration) {
    policySnapshotOperationStatus = "idle";
    if (policySnapshotOperationController === owner.controller) {
      policySnapshotOperationController = null;
    }
  }
}

/**
 * 处理发布/回滚错误。
 * AuthApiError 是服务端明确结论并释放票据；网络异常结果未知，保留原票据、原因和 CAS 供安全重试。
 */
async function handlePolicySnapshotWriteError(error, operation, owner) {
  if (isAbortError(error) || !isCurrentPolicySnapshotWrite(owner)) {
    // 离页取消会同时清理全部内存；不能把 AbortError 显示成发布失败或恢复旧 request_id。
    return;
  }
  finishPolicySnapshotWrite(owner);
  // 结构化 HTTP 错误代表服务端已给出明确结论，可以释放原幂等票据；网络异常则不能释放。
  if (error instanceof AuthApiError) {
    policySnapshotOperationMemory.markDefinitive(operation);
    // 认证失效会改变 tenant/user 边界，必须先清理原因、CAS 和票据再返回登录页。
    if (error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      clearPolicySnapshotSessionState();
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    policySnapshotActionError = error;
    policySnapshotFeedback = error.status === 409
      ? "线上版本或 active 候选已变化，已保留原因并重新读取最新状态。"
      : "";
    // 冲突保留管理员原因但重新读取权威 head/candidate，旧 CAS 不能继续启用写按钮。
    if (error.status === 409) {
      await refresh();
    } else {
      renderPolicySnapshotPanelFromMemory();
    }
    return;
  }

  policySnapshotOperationMemory.markUncertain(operation);
  policySnapshotActionError = {
    code: "policy_snapshot.result_unknown",
    message: "网络中断，提交结果暂不确定。请保持原原因并重试，系统会复用同一 request_id。",
    requestId: operation.requestId,
  };
  policySnapshotFeedback = "";
  renderPolicySnapshotPanelFromMemory();
}

/** 显示未决操作冲突，阻止用户在未知结果上改变 reason、target 或 CAS 生成第二个版本。 */
function showPolicySnapshotPendingConflict(pending) {
  policySnapshotActionError = {
    code: "policy_snapshot.pending_operation_conflict",
    message: "上一操作结果仍未确认。请恢复原原因重试，或先放弃未决重试并刷新权威状态。",
    requestId: pending?.requestId ?? "-",
  };
  renderPolicySnapshotPanelFromMemory();
}

/**
 * 用户明确放弃未知结果后清理内存票据并立即读取 head。
 * 若原请求实际已提交，刷新会显示新版本；若未提交，后续操作才会生成新的 request_id。
 */
async function abandonPendingPolicySnapshotOperation() {
  if (!confirmPolicySnapshotAction("放弃原 request_id 的安全重试并重新读取线上状态？")) {
    return;
  }
  policySnapshotOperationMemory.clear();
  policySnapshotActionError = null;
  policySnapshotFeedback = "已放弃未决重试，正在读取最新线上版本。";
  await refresh();
}

/** 浏览器确认对话框在非浏览器测试中默认允许；真实页面必须获得管理员二次确认。 */
function confirmPolicySnapshotAction(message) {
  return typeof globalThis.window?.confirm === "function"
    ? globalThis.window.confirm(message)
    : true;
}

/**
 * tenant/user/role/session 变化时清理快照页面状态。
 * 前后身份都用固定字段快照比较，不能只比较 email 或前端当前 hash。
 */
function clearPolicySnapshotStateOnIdentityChange(previous, current) {
  const previousIdentity = previous
    ? policySnapshotIdentitySnapshot(policySnapshotIdentityFromAuthContext(previous))
    : "";
  const currentIdentity = current
    ? policySnapshotIdentitySnapshot(policySnapshotIdentityFromAuthContext(current))
    : "";
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearPolicySnapshotSessionState();
  }
}

/**
 * 清除策略快照页面全部短生命周期状态。
 * 该函数会 abort current/history 与写请求，并销毁原因、ETag、历史、错误和未决 request_id。
 */
function clearPolicySnapshotSessionState() {
  policySnapshotLoadLifecycle.invalidate();
  policySnapshotOperationGeneration += 1;
  policySnapshotOperationController?.abort();
  policySnapshotOperationController = null;
  policySnapshotOperationMemory.clear();
  policySnapshotsState = null;
  policySnapshotActionError = null;
  policySnapshotFeedback = "";
  policySnapshotOperationStatus = "idle";
  policySnapshotHistoryFilters = { limit: 20, offset: 0 };
  policySnapshotPublishReason = "";
  policySnapshotRollbackTargetId = null;
  policySnapshotRollbackReason = "";
}

/**
 * 仅在后端权限快照包含 ``policy.read`` 且导航为 policies 时读取策略列表。
 * 请求前后均核对 refresh、tenant/user/role、hash 与 active navigation，旧响应不得提交到模块态或 DOM。
 */
async function loadPoliciesState(consoleShellState, refreshId, modelsState) {
  const requestIdentityObject = policyIdentityFromAuthContext(authSession.context);
  const requestIdentity = policyIdentitySnapshot(requestIdentityObject);
  const hashNavigation = readActiveNavigationHash();
  if (!shouldLoadPolicies({
    canRead: hasPermission(authSession.context, PERMISSIONS.POLICY_READ),
    activeNavigationItemId: consoleShellState?.activeNavigationItemId,
    hashNavigation,
    refreshId,
    currentRefreshId: refreshSequence,
    requestIdentity,
    currentIdentity: requestIdentity,
  })) {
    // 未获读取权限、非 active 策略页或旧刷新不得后台预取 Policy/DSL 正文。
    return null;
  }

  policyListController?.abort();
  const controller = new AbortController();
  policyListController = controller;
  try {
    const state = await listPolicies({
      controlApiUrl,
      token: authSession.token,
      status: "all",
      scanScope: dlpScanScopeFilter,
      signal: controller.signal,
    });
    const currentIdentity = policyIdentitySnapshot(policyIdentityFromAuthContext(authSession.context));
    const currentHashNavigation = readActiveNavigationHash();
    if (
      policyListController !== controller
      || !shouldLoadPolicies({
        canRead: hasPermission(authSession.context, PERMISSIONS.POLICY_READ),
        activeNavigationItemId: consoleShellState?.activeNavigationItemId,
        hashNavigation: currentHashNavigation,
        refreshId,
        currentRefreshId: refreshSequence,
        requestIdentity,
        currentIdentity,
      })
    ) {
      // 导航、刷新代次或身份变化后即使 fetch 忽略 abort，也必须丢弃包含策略正文的旧响应。
      return null;
    }
    policyModelsState = modelsState;
    policyListState = {
      ...state,
      policies: Array.isArray(state.policies) ? state.policies : [],
    };
    policyActionError = null;
    reconcilePolicyEditorScope(requestIdentityObject, policyListState.policies);
    return buildPoliciesRenderState();
  } catch (error) {
    // 取消或 controller owner 已变化都属于页面生命周期，不能当成后端策略错误展示。
    if (isAbortError(error) || policyListController !== controller) {
      // 主动取消属于页面生命周期，不展示成服务故障，也不能恢复旧列表。
      return null;
    }
    // 认证失效必须清理 Policy/DSL 内存态并回到登录页，后续租户请求全部停止。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      clearPolicyEditorSessionState();
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403 不能保留上一轮策略正文；真实权限仍以后端角色守卫为准。
    if (error instanceof AuthApiError && error.status === 403) {
      policyDraftMemory.clear();
      policyListState = {
        state: "error",
        policies: [],
        code: error.code,
        message: error.message,
        requestId: error.requestId,
      };
    } else {
      // 5xx/网络错误保留同身份已读取列表和 dirty draft，错误区只展示统一安全字段并允许稍后重试。
      policyListState = {
        state: "error",
        policies: policyListState?.policies ?? [],
        code: error?.code ?? "policy.unavailable",
        message: authErrorMessage(error),
        requestId: error?.requestId ?? "-",
      };
      policyDraftMemory.enter(requestIdentityObject);
    }
    policyModelsState = modelsState;
    return buildPoliciesRenderState();
  } finally {
    if (policyListController === controller) {
      policyListController = null;
    }
  }
}

/**
 * 把最新列表与当前内存草稿协调。
 * dirty draft 在同身份 15 秒刷新时保持不变；未修改编辑项则回填最新 ETag version，并使旧校验按快照失效。
 */
function reconcilePolicyEditorScope(identity, policies) {
  const current = policyDraftMemory.enter(identity);
  if (!current) {
    return;
  }
  let next = current;
  if (current.selectedPolicyId) {
    const selected = policies.find((policy) => policy.policy_id === current.selectedPolicyId);
    if (!selected) {
      // 已选策略不再属于当前列表时回到新建态，禁止保留可能已删除或跨租户的正文。
      next = createEmptyPolicyScope();
    } else if (!current.dirty) {
      next = {
        ...current,
        draft: policyDraftFromPolicy(selected),
      };
    }
  }
  const snapshot = policyDraftSnapshot(next.draft, { selectedPolicyId: next.selectedPolicyId });
  if (next.validation?.snapshot !== snapshot) {
    // 版本或任一可编辑字段变化都会改变精确快照，旧 valid=true 不能继续启用保存。
    next = { ...next, validation: null };
  }
  policyDraftMemory.write(identity, next);
}

/**
 * 组装策略渲染状态。
 * 保存能力只取“当前 scope 快照 === 校验快照且 valid=true”，按钮属性不构成后端信任边界。
 */
function buildPoliciesRenderState() {
  if (!policyListState) {
    return null;
  }
  const identity = policyIdentityFromAuthContext(authSession.context);
  const scope = policyDraftMemory.enter(identity) ?? createEmptyPolicyScope();
  const currentSnapshot = policyDraftSnapshot(scope.draft, { selectedPolicyId: scope.selectedPolicyId });
  const validation = scope.validation?.snapshot === currentSnapshot
    ? scope.validation.result
    : null;
  return {
    ...policyListState,
    filters: policyListFilters,
    actionError: policyActionError,
    feedback: policyFeedback,
    editor: {
      ...scope,
      validation,
      currentSnapshot,
      operationStatus: policyOperationStatus,
      canSave: validation?.valid === true && policyOperationStatus === "idle",
    },
  };
}

/**
 * 只重绘策略 panel 并重新绑定其局部事件。
 * 该路径避免校验、筛选和选择编辑项时触发整页租户接口刷新，同时不会复制控制台导航监听器。
 */
function renderPolicyPanelFromMemory() {
  const panel = document.querySelector(".policies-panel");
  const activeNavigation = currentRenderedNavigation();
  // DOM、active navigation 和实时读取权限必须同时成立，旧回调才有资格局部重绘策略正文。
  if (
    !panel
    || activeNavigation !== "policies"
    || !hasPermission(authSession.context, PERMISSIONS.POLICY_READ)
  ) {
    // 只有当前获授权策略页拥有 Policy DOM；离页或降权后局部回调不得重新创建面板。
    return;
  }
  panel.innerHTML = renderPoliciesPanel(
    buildPoliciesRenderState(),
    policyModelsState,
    authSession.context,
  );
  bindPolicyInteractions({ activeNavigationItemId: "policies" });
}

/**
 * 绑定策略新建、选择、筛选、字段变更、校验和保存事件。
 * 每轮全页或局部渲染后只绑定当前新 DOM，权限和租户事实仍由后端 API 再次验证。
 */
function bindPolicyInteractions(consoleShellState) {
  const identity = policyIdentityFromAuthContext(authSession.context);
  if (
    !hasPermission(authSession.context, PERMISSIONS.POLICY_READ)
    || consoleShellState?.activeNavigationItemId !== "policies"
    || !policyDraftMemory.read(identity)
  ) {
    // 未获读取权限或非 active 策略页不得绑定隐藏 DOM，也不得由事件路径触发 /policies 请求。
    return;
  }

  const filterForm = document.querySelector("#policy-filter-form");
  filterForm?.addEventListener("change", () => {
    const data = new FormData(filterForm);
    policyListFilters = {
      status: String(data.get("policyStatus") ?? "all"),
      action: String(data.get("policyAction") ?? "all"),
    };
    renderPolicyPanelFromMemory();
  });

  // 读取权限允许筛选列表；缺少写权限时在绑定任何编辑事件前返回，伪造 DOM 也不会形成写请求。
  if (!hasPermission(authSession.context, PERMISSIONS.POLICY_WRITE)) {
    return;
  }

  document.querySelector("#policy-new")?.addEventListener("click", () => {
    policyRequestLifecycle.invalidate();
    policyDraftMemory.write(identity, createEmptyPolicyScope());
    policyOperationStatus = "idle";
    policyActionError = null;
    policyFeedback = "";
    renderPolicyPanelFromMemory();
  });

  for (const editButton of document.querySelectorAll("[data-policy-edit]")) {
    editButton.addEventListener("click", () => {
      selectPolicyForEditing(identity, editButton.dataset.policyEdit);
    });
  }
  document.querySelector("#policy-filter-reset")?.addEventListener("click", () => {
    policyListFilters = { status: "all", action: "all" };
    renderPolicyPanelFromMemory();
  });

  const editorForm = document.querySelector("#policy-editor-form");
  editorForm?.addEventListener("input", () => {
    synchronizePolicyDraftFromForm(editorForm, identity);
    syncPolicyEditorDomAfterInput(editorForm);
  });
  editorForm?.addEventListener("change", (event) => {
    if (event.target?.name === "actionType") {
      synchronizePolicyDraftFromForm(editorForm, identity);
      syncPolicyEditorDomAfterInput(editorForm);
    }
  });
  editorForm?.querySelector("[data-policy-validate]")?.addEventListener("click", async () => {
    await validateCurrentPolicy(editorForm, identity);
  });
  editorForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveCurrentPolicy(editorForm, identity);
  });
}

/**
 * 切换到后端列表中的一条策略。
 * 选择变化先 abort 旧 validate/save，再回填公开管理字段并清除校验，不能按相同表达式复用另一对象结果。
 */
function selectPolicyForEditing(identity, policyId) {
  const selected = policyListState?.policies?.find((policy) => policy.policy_id === policyId);
  if (!selected) {
    // DOM 中伪造或已过期的 policy_id 不会形成请求，真实跨租户枚举仍由后端路径查询统一返回 404。
    return;
  }
  policyRequestLifecycle.invalidate();
  policyDraftMemory.write(identity, {
    selectedPolicyId: selected.policy_id,
    draft: policyDraftFromPolicy(selected),
    dirty: false,
    validation: null,
  });
  policyOperationStatus = "idle";
  policyActionError = null;
  policyFeedback = "";
  renderPolicyPanelFromMemory();
}

/**
 * 从编辑表单读取当前可编辑字段。
 * 只读取固定 name 列表；Policy ID/version 继续沿用内存 scope，tenant/AST/hash 不存在任何表单入口。
 */
function readPolicyDraftFromForm(form, currentDraft) {
  const data = new FormData(form);
  const actionType = String(data.get("actionType") ?? "allow");
  const targetModelId = actionType === "route_to"
    ? optionalText(data.get("targetModelId")) ?? null
    : null;
  return {
    name: String(data.get("name") ?? ""),
    description: String(data.get("description") ?? ""),
    status: String(data.get("status") ?? "active"),
    priority: String(data.get("priority") ?? ""),
    conditionExpr: String(data.get("conditionExpr") ?? ""),
    actionType,
    targetModelId,
    version: currentDraft?.version ?? null,
  };
}

/**
 * 将 DOM 字段同步到内存草稿并使旧校验失效。
 * 返回 changed 供保存路径识别手工 DOM 篡改；相同快照不会重复 abort 当前请求。
 */
function synchronizePolicyDraftFromForm(form, identity) {
  const current = policyDraftMemory.read(identity);
  if (!current) {
    return { scope: null, changed: false };
  }
  const nextDraft = readPolicyDraftFromForm(form, current.draft);
  const previousSnapshot = policyDraftSnapshot(current.draft, { selectedPolicyId: current.selectedPolicyId });
  const nextSnapshot = policyDraftSnapshot(nextDraft, { selectedPolicyId: current.selectedPolicyId });
  if (previousSnapshot === nextSnapshot) {
    return { scope: current, changed: false };
  }
  policyRequestLifecycle.invalidate();
  const next = {
    ...current,
    draft: nextDraft,
    dirty: true,
    validation: null,
  };
  policyDraftMemory.write(identity, next);
  policyOperationStatus = "idle";
  policyActionError = null;
  policyFeedback = "";
  return { scope: next, changed: true };
}

/**
 * 字段变化后原位同步 route_to 与校验区域。
 * 不整页重绘 textarea，避免输入光标跳动；保存立即禁用，旧 validation 文本同步移除。
 */
function syncPolicyEditorDomAfterInput(form) {
  const actionSelect = form.querySelector("[name='actionType']");
  const targetField = form.querySelector("[data-policy-route-target-field]");
  const targetSelect = form.querySelector("[name='targetModelId']");
  const routeTo = actionSelect?.value === "route_to";
  if (targetField) {
    targetField.hidden = !routeTo;
  }
  if (targetSelect) {
    targetSelect.disabled = !routeTo;
    targetSelect.required = routeTo;
    // 非 route_to 动作必须主动清空模型目标，避免隐藏 select 的旧值进入下一次草稿快照。
    if (!routeTo) {
      targetSelect.value = "";
    }
  }
  const saveButton = form.querySelector("[data-policy-save]");
  if (saveButton) {
    saveButton.disabled = true;
  }
  const validation = document.querySelector(".policy-validation");
  if (validation) {
    validation.textContent = "字段已变化，请重新校验。";
  }
}

/**
 * 调用后端校验当前策略草稿。
 * 响应只有在 revision/controller/snapshot/identity/navigation/refresh 全部一致时才进入内存并启用保存。
 */
async function validateCurrentPolicy(form, identity) {
  if (!hasPermission(authSession.context, PERMISSIONS.POLICY_WRITE)) {
    clearPolicyEditorSessionState();
    return;
  }
  if (typeof form.reportValidity === "function" && !form.reportValidity()) {
    return;
  }
  const synchronization = synchronizePolicyDraftFromForm(form, identity);
  const scope = synchronization.scope ?? policyDraftMemory.read(identity);
  if (!scope) {
    return;
  }
  const snapshot = policyDraftSnapshot(scope.draft, { selectedPolicyId: scope.selectedPolicyId });
  const operation = policyRequestLifecycle.begin({
    kind: "validate",
    snapshot,
    identity: policyIdentitySnapshot(identity),
    navigation: currentRenderedNavigation(),
    generation: refreshSequence,
  });
  policyOperationStatus = "validating";
  policyActionError = null;
  policyFeedback = "校验中";
  renderPolicyPanelFromMemory();
  try {
    const result = await validatePolicy({
      controlApiUrl,
      token: authSession.token,
      conditionExpr: scope.draft.conditionExpr,
      actionType: scope.draft.actionType,
      targetModelId: scope.draft.targetModelId,
      signal: operation.signal,
    });
    if (!isCurrentPolicyOperation(operation)) {
      // 旧校验即使成功也不能恢复保存按钮或覆盖较新的字段/身份/导航状态。
      return;
    }
    policyRequestLifecycle.finish(operation);
    const current = policyDraftMemory.read(policyIdentityFromAuthContext(authSession.context));
    policyDraftMemory.write(identity, {
      ...current,
      validation: { snapshot, result },
    });
    policyOperationStatus = "idle";
    policyFeedback = result.valid === true ? "校验通过" : "校验未通过";
    renderPolicyPanelFromMemory();
  } catch (error) {
    // 字段变化或离页会 abort 并转移 owner，旧校验错误也不能覆盖新编辑器状态。
    if (isAbortError(error) || !isCurrentPolicyOperation(operation)) {
      // Abort 或 owner 已变化属于预期生命周期，不显示错误，也不触碰新编辑器状态。
      return;
    }
    policyRequestLifecycle.finish(operation);
    await handlePolicyOperationError(error, { clearValidation: true });
  }
}

/**
 * 保存已经由当前精确快照校验通过的策略。
 * create/PATCH 仍会在后端重新编译；PATCH version 只来自后端回填并通过 If-Match 发送。
 */
async function saveCurrentPolicy(form, identity) {
  if (!hasPermission(authSession.context, PERMISSIONS.POLICY_WRITE)) {
    clearPolicyEditorSessionState();
    return;
  }
  if (typeof form.reportValidity === "function" && !form.reportValidity()) {
    return;
  }
  const synchronization = synchronizePolicyDraftFromForm(form, identity);
  const scope = synchronization.scope ?? policyDraftMemory.read(identity);
  if (!scope) {
    return;
  }
  const snapshot = policyDraftSnapshot(scope.draft, { selectedPolicyId: scope.selectedPolicyId });
  if (
    synchronization.changed
    || scope.validation?.snapshot !== snapshot
    || scope.validation?.result?.valid !== true
  ) {
    // 手工解锁按钮或无事件修改 DOM 不能绕过精确快照校验，保存必须保持禁用并要求重新 validate。
    policyActionError = {
      code: "policy.validation_required",
      message: "当前表单已变化，请重新校验。",
      requestId: "-",
    };
    policyFeedback = "保存已阻止";
    renderPolicyPanelFromMemory();
    return;
  }
  const operation = policyRequestLifecycle.begin({
    kind: "save",
    snapshot,
    identity: policyIdentitySnapshot(identity),
    navigation: currentRenderedNavigation(),
    generation: refreshSequence,
  });
  policyOperationStatus = "saving";
  policyActionError = null;
  policyFeedback = "保存中";
  renderPolicyPanelFromMemory();
  try {
    const arguments_ = policyWriteArguments(scope.draft);
    const result = scope.selectedPolicyId
      ? await updatePolicy({
        controlApiUrl,
        token: authSession.token,
        policyId: scope.selectedPolicyId,
        version: scope.draft.version,
        ...arguments_,
        signal: operation.signal,
      })
      : await createPolicy({
        controlApiUrl,
        token: authSession.token,
        ...arguments_,
        signal: operation.signal,
      });
    if (!isCurrentPolicyOperation(operation)) {
      // 保存响应失去 owner 后不能更新列表或 ETag；下一次 active loader 会从后端恢复权威状态。
      return;
    }
    policyRequestLifecycle.finish(operation);
    const savedPolicy = normalizedSavedPolicy(result);
    if (savedPolicy) {
      upsertPolicyListItem(savedPolicy);
      policyDraftMemory.write(identity, {
        selectedPolicyId: savedPolicy.policy_id,
        draft: policyDraftFromPolicy(savedPolicy),
        dirty: false,
        validation: null,
      });
    } else {
      // 成功响应缺少 PolicyInfo 时不猜测 version；清除校验并等待下一轮 active loader 权威回读。
      policyDraftMemory.write(identity, {
        ...scope,
        dirty: false,
        validation: null,
      });
    }
    policyOperationStatus = "idle";
    policyActionError = null;
    policyFeedback = scope.selectedPolicyId ? "策略已更新" : "策略已创建";
    renderPolicyPanelFromMemory();
  } catch (error) {
    // 保存 owner 已转移时由下一轮 GET 对账，旧 Promise 不得重新写入当前 tenant 的列表。
    if (isAbortError(error) || !isCurrentPolicyOperation(operation)) {
      // 离页/身份变化后的旧保存结果由后续 GET 对账，不能重新创建已销毁的 Policy DOM。
      return;
    }
    policyRequestLifecycle.finish(operation);
    await handlePolicyOperationError(error, { clearValidation: true });
  }
}

/**
 * 把草稿转换为 API 客户端可编辑参数。
 * 不包含 tenant_id、Policy ID、version、AST、hash、actor 或 request_id，版本由 update 调用单独放入 If-Match。
 */
function policyWriteArguments(draft) {
  return {
    name: draft.name,
    description: draft.description,
    status: draft.status,
    priority: draft.priority,
    conditionExpr: draft.conditionExpr,
    actionType: draft.actionType,
    targetModelId: draft.targetModelId,
  };
}

/**
 * 判断异步操作是否仍拥有当前策略编辑器。
 * 当前 scope 快照、认证身份、渲染导航和 refreshSequence 都实时读取，不能复用请求开始时的旧值冒充。
 */
function isCurrentPolicyOperation(operation) {
  if (!hasPermission(authSession.context, PERMISSIONS.POLICY_WRITE)) {
    return false;
  }
  const identity = policyIdentityFromAuthContext(authSession.context);
  const scope = policyDraftMemory.read(identity);
  const snapshot = scope
    ? policyDraftSnapshot(scope.draft, { selectedPolicyId: scope.selectedPolicyId })
    : "";
  return policyRequestLifecycle.isCurrent(operation, {
    snapshot,
    identity: policyIdentitySnapshot(identity),
    navigation: currentRenderedNavigation(),
    generation: refreshSequence,
  });
}

/**
 * 读取当前实际渲染的导航 ID。
 * 优先使用后端导航渲染到 shell 的受控 data 值，hash 只作为尚未渲染时的候选且仍受 loader 门禁约束。
 */
function currentRenderedNavigation() {
  return document.querySelector(".console-shell")?.dataset.activeNavigation
    ?? readActiveNavigationHash()
    ?? "overview";
}

/**
 * 统一处理策略 validate/save 错误。
 * 401 清会话，403 清全部 Policy DOM/内存；其他错误保留 dirty draft 但使旧校验失效并展示固定链路信息。
 */
async function handlePolicyOperationError(error, { clearValidation = false } = {}) {
  // 认证失败会改变 tenant/user 会话边界，必须先清草稿再回到登录页。
  if (error instanceof AuthApiError && error.status === 401) {
    clearAuthSession();
    authSession = { token: null, context: null };
    clearPolicyEditorSessionState();
    renderLogin(authErrorMessage(error), error.requestId);
    return;
  }
  // 后端权限拒绝必须同时清除 Policy DOM 和内存草稿，再由最新导航响应决定可见入口。
  if (error instanceof AuthApiError && error.status === 403) {
    // 后端拒绝权限后不能保留上一轮策略正文；刷新导航会按最新角色裁剪 policies 入口。
    clearPolicyEditorSessionState();
    consoleShellActionError = error;
    await refresh();
    return;
  }
  const identity = policyIdentityFromAuthContext(authSession.context);
  const scope = policyDraftMemory.read(identity);
  if (scope && clearValidation) {
    policyDraftMemory.write(identity, { ...scope, validation: null, dirty: true });
  }
  policyOperationStatus = "idle";
  policyActionError = {
    code: error?.code ?? "policy.unavailable",
    message: error?.message ?? "策略操作失败",
    requestId: error?.requestId ?? "-",
  };
  policyFeedback = "操作失败";
  renderPolicyPanelFromMemory();
}

/**
 * 从 create/PATCH 成功响应读取 PolicyInfo 并补齐代理返回的 ETag version。
 * 响应缺少 policy_id 时返回 null，避免前端伪造对象或版本。
 */
function normalizedSavedPolicy(result) {
  const policy = result?.policy;
  if (!policy?.policy_id) {
    return null;
  }
  return {
    ...policy,
    version: Number(policy.version ?? result.etagVersion ?? 0) || policy.version,
  };
}

/**
 * 将已保存 PolicyInfo 写回当前内存列表并恢复确定性 priority+policy_id 顺序。
 * 只更新同一身份 active 页的模块态，不向浏览器存储或 console event 写策略正文。
 */
function upsertPolicyListItem(savedPolicy) {
  const policies = [...(policyListState?.policies ?? [])];
  const index = policies.findIndex((policy) => policy.policy_id === savedPolicy.policy_id);
  if (index >= 0) {
    policies[index] = savedPolicy;
  } else {
    policies.push(savedPolicy);
  }
  policies.sort((left, right) => {
    const priorityDifference = Number(left.priority ?? 0) - Number(right.priority ?? 0);
    return priorityDifference || String(left.policy_id ?? "").localeCompare(String(right.policy_id ?? ""));
  });
  policyListState = {
    ...(policyListState ?? { state: "ok" }),
    state: "ok",
    policies,
  };
}

/**
 * 清除策略页全部短生命周期状态并取消列表/校验/保存请求。
 * 该函数不写 storage；离页、登出、401、角色/租户/用户变化与 pagehide 必须统一调用。
 */
function clearPolicyEditorSessionState() {
  policyListController?.abort();
  policyListController = null;
  policyRequestLifecycle.invalidate();
  policyDraftMemory.clear();
  policyListState = null;
  policyModelsState = null;
  policyActionError = null;
  policyFeedback = "";
  policyOperationStatus = "idle";
  policyListFilters = { status: "all", action: "all" };
  document.querySelector(".policies-panel")?.replaceChildren();
}

/**
 * 用户、租户或角色变化时清理 Policy/DSL 状态。
 * 比较只使用内存身份快照；正文、Token、邮箱显示值和 tenant 信息不会写入日志或 DOM 属性。
 */
function clearPolicyStateOnIdentityChange(previous, current) {
  const previousIdentity = policyIdentitySnapshot(policyIdentityFromAuthContext(previous));
  const currentIdentity = policyIdentitySnapshot(policyIdentityFromAuthContext(current));
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearPolicyEditorSessionState();
  }
}

/**
 * 绑定策略模拟器的上下文选择、提交和清空操作。
 * Prompt 只在 submit 当前调用栈读取，读取后立即清空 textarea，绝不写入模块状态。
 */
function bindPolicyDryRunInteractions(consoleShellState) {
  const identity = policyDryRunIdentitySnapshot(authSession.context);
  if (!shouldOwnPolicyDryRun({
    canSimulate: hasPermission(authSession.context, PERMISSIONS.POLICY_SIMULATE),
    activeNavigationItemId: consoleShellState?.activeNavigationItemId,
    identity,
  })) {
    return;
  }
  const form = document.querySelector("#policy-dry-run-form");
  if (!form) return;

  for (const field of form.querySelectorAll("select, input[name='dataClass'], input[name='maxOutputTokens']")) {
    const eventName = field.matches("input") ? "input" : "change";
    field.addEventListener(eventName, () => {
      const selections = policyDryRunSelectionsFromForm(form);
      if (field.name === "environmentId") {
        // Environment 改变会使模型候选失效；重绘前主动清空 Prompt，防止旧上下文正文被带入新环境。
        form.querySelector("textarea[name='prompt']").value = "";
        policyDryRunState = {
          ...policyDryRunState,
          ...selections,
          projectId: "",
          model: "",
          result: null,
          error: null,
          status: "idle",
        };
        renderPolicyDryRunPanelFromMemory({ focusSelector: "select[name='projectId']" });
        return;
      }
      policyDryRunState = {
        ...policyDryRunState,
        ...selections,
        result: null,
        error: null,
        status: "idle",
      };
      // 非 Environment 字段不重建 form，避免用户尚未提交的 Prompt 因普通选择变化而丢失。
      document.querySelector(".policy-dry-run-result")?.remove();
      document.querySelector(".policy-dry-run-error")?.remove();
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const token = authSession.token;
    // 预演必须绑定当前 ``policy.simulate`` 快照；本地令牌或权限缺失时立即失败关闭。
    if (!token || !hasPermission(authSession.context, PERMISSIONS.POLICY_SIMULATE)) {
      clearPolicyDryRunSessionState();
      renderLogin();
      return;
    }
    const data = new FormData(form);
    const prompt = String(data.get("prompt") ?? "");
    const selections = policyDryRunSelectionsFromForm(form);
    const parsedMaxOutputTokens = Number(selections.maxOutputTokens);
    const operation = policyDryRunLifecycle.begin({
      identity,
      navigation: "policy-simulator",
    });
    // 一旦取得局部字符串就立即从 DOM 移除；后续重绘也不会从 state 恢复 Prompt。
    form.querySelector("textarea[name='prompt']").value = "";
    policyDryRunState = {
      ...policyDryRunState,
      ...selections,
      status: "submitting",
      result: null,
      error: null,
    };
    renderPolicyDryRunPanelFromMemory({ focusSelector: ".policy-dry-run-status" });
    try {
      const result = await executePolicyDryRun({
        controlApiUrl,
        token,
        prompt,
        model: selections.model,
        projectId: selections.projectId,
        environmentId: selections.environmentId,
        dataClass: selections.dataClass,
        maxOutputTokens: Number.isInteger(parsedMaxOutputTokens) ? parsedMaxOutputTokens : undefined,
        signal: operation.signal,
      });
      if (!isCurrentPolicyDryRunOperation(operation)) return;
      policyDryRunState = {
        ...policyDryRunState,
        status: "success",
        result,
        error: null,
      };
      renderPolicyDryRunPanelFromMemory({ focusSelector: "#policy-dry-run-summary-title" });
    } catch (error) {
      if (operation.signal.aborted) return;
      if (!isCurrentPolicyDryRunOperation(operation)) return;
      // 401 表示可信会话边界已失效，旧租户的预演结果和认证信息必须一起清除。
      if (error instanceof AuthApiError && error.status === 401) {
        clearAuthSession();
        authSession = { token: null, context: null };
        clearPolicyDryRunSessionState();
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      // 403 以后端 RBAC 结论为准；立即清空面板内存并刷新导航，不能依赖前端隐藏按钮维持权限。
      if (error instanceof AuthApiError && error.status === 403) {
        clearPolicyDryRunSessionState();
        await refresh();
        return;
      }
      policyDryRunState = {
        ...policyDryRunState,
        status: "error",
        result: null,
        error: error instanceof AuthApiError
          ? error
          : {
            code: "policy.dry_run_unavailable",
            message: "策略预演请求未完成",
            requestId: "-",
          },
      };
      renderPolicyDryRunPanelFromMemory({ focusSelector: ".policy-dry-run-error" });
    } finally {
      policyDryRunLifecycle.finish(operation);
    }
  });

  document.querySelector("[data-policy-dry-run-clear]")?.addEventListener("click", () => {
    policyDryRunLifecycle.invalidate();
    form.querySelector("textarea[name='prompt']").value = "";
    policyDryRunState = createEmptyPolicyDryRunState();
    renderPolicyDryRunPanelFromMemory({ focusSelector: "textarea[name='prompt']" });
  });
}

/**
 * 局部重绘策略模拟器并重新绑定事件。
 * 上下文缓存只含当前已授权列表；没有 active panel 或模拟权限时直接清理而不创建隐藏 DOM。
 */
function renderPolicyDryRunPanelFromMemory({ focusSelector = null } = {}) {
  const panel = document.querySelector(".policy-dry-run-panel");
  // DOM 存在且当前权限快照仍允许模拟才可重绘，避免降权后把旧租户解释重新挂回页面。
  if (!panel || !hasPermission(authSession.context, PERMISSIONS.POLICY_SIMULATE)) return;
  panel.innerHTML = renderPolicyDryRunPanel(
    policyDryRunState,
    policyDryRunContextState,
    authSession.context,
  );
  bindPolicyDryRunInteractions({ activeNavigationItemId: "policy-simulator" });
  // 整体重绘会销毁原焦点节点；命令完成后把焦点送到新状态、结果、错误或下一输入。
  if (focusSelector) {
    document.querySelector(focusSelector)?.focus({ preventScroll: true });
  }
}

/** 核对异步结果仍属于当前身份和仍可见的模拟器页面。 */
function isCurrentPolicyDryRunOperation(operation) {
  return hasPermission(authSession.context, PERMISSIONS.POLICY_SIMULATE)
    && policyDryRunLifecycle.isCurrent(operation, {
      identity: policyDryRunIdentitySnapshot(authSession.context),
      navigation: document.querySelector(".policy-dry-run-panel") ? "policy-simulator" : "",
    });
}

/**
 * 清除策略模拟器全部短生命周期状态。
 * textarea 先原地清空，再 abort 和移除 DOM；调用方覆盖离页、登出、401、身份变化和 pagehide。
 */
function clearPolicyDryRunSessionState() {
  const prompt = document.querySelector("#policy-dry-run-form textarea[name='prompt']");
  if (prompt) prompt.value = "";
  policyDryRunLifecycle.invalidate();
  policyDryRunState = createEmptyPolicyDryRunState();
  policyDryRunContextState = null;
  document.querySelector(".policy-dry-run-panel")?.replaceChildren();
}

/** 用户、租户、角色或 Session 变化时销毁上一身份的 Prompt 与解释。 */
function clearPolicyDryRunStateOnIdentityChange(previous, current) {
  const previousIdentity = policyDryRunIdentitySnapshot(previous);
  const currentIdentity = policyDryRunIdentitySnapshot(current);
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearPolicyDryRunSessionState();
  }
}

/** 检查 DOM 是否仍持有未提交 Prompt；只返回布尔值，不复制正文。 */
function policyDryRunHasSensitiveInput() {
  return Boolean(document.querySelector("#policy-dry-run-form textarea[name='prompt']")?.value);
}

/**
 * 绑定 PAGE-096 的提交、取消和清空命令。
 * 正文只从 textarea 读取到当前 async 调用栈，读取后立刻清空 DOM；状态机只接收无正文 owner 和安全结果。
 */
function bindIntentSecurityInteractions(consoleShellState) {
  const owner = currentIntentSecurityOwner(consoleShellState);
  if (!owner || !intentSecurityPageState.enter(owner)) return;
  const form = document.querySelector("#intent-security-form");
  const textarea = form?.querySelector("textarea[name='text']");
  if (!form || !textarea) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const token = authSession.token;
    const submitOwner = currentIntentSecurityOwner(consoleShellState);
    // Token 或完整 owner 缺失意味着旧 DOM 已失权，必须在任何 POST 前擦除正文并停止。
    if (!token || !submitOwner) {
      // 手工触发旧 DOM submit 也必须先擦除正文并失败关闭，不能依赖按钮或导航可见性。
      textarea.value = "";
      clearIntentSecuritySessionState();
      return;
    }
    let sensitiveText = textarea.value;
    // 正文一旦进入本次提交栈就立即从 DOM 移除；成功、失败和取消都不会恢复原文。
    textarea.value = "";
    const operation = intentSecurityPageState.begin(submitOwner);
    // generation ticket 创建失败表示刷新或导航已经接管，旧提交只能清理不能继续网络调用。
    if (!operation) {
      sensitiveText = "";
      clearIntentSecuritySessionState();
      return;
    }
    renderIntentSecurityPanelFromMemory(consoleShellState, { focusSelector: ".intent-security-status" });
    try {
      const payload = await classifyIntent({
        controlApiUrl,
        token,
        text: sensitiveText,
        signal: operation.signal,
      });
      const currentOwner = currentIntentSecurityOwner(consoleShellState);
      if (!currentOwner || !intentSecurityPageState.accept(operation, payload, currentOwner)) return;
      renderIntentSecurityPanelFromMemory(consoleShellState, {
        focusSelector: "#intent-security-result-title",
      });
    } catch (error) {
      const currentOwner = currentIntentSecurityOwner(consoleShellState);
      // 用户取消、离页或任何 owner 代次变化都静默丢弃，旧错误不能恢复旧页面或结束新 Session。
      if (isAbortError(error) || !currentOwner || !intentSecurityPageState.isCurrent(operation, currentOwner)) {
        return;
      }
      // 当前 owner 的 401 才能销毁本地 Session；迟到 401 已被上方代次门禁丢弃。
      if (error instanceof AuthApiError && error.status === 401) {
        // 可信 401 属于当前完整 owner 时才销毁 Session；正文、结果和 DOM 必须先于登录页重建被清理。
        clearIntentSecuritySessionState();
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      // 当前 owner 的 403 以后端实时撤权为准，必须销毁页面并重新获取授权导航。
      if (error instanceof AuthApiError && error.status === 403) {
        // 后端撤权立即销毁工作台并重取导航，前端不能按旧角色或旧 permission_codes 保留入口。
        clearIntentSecuritySessionState();
        await refresh();
        return;
      }
      // 其余当前错误仅按固定字段写入页面状态，原始异常和正文均不允许进入 renderer。
      if (intentSecurityPageState.reject(operation, error, currentOwner)) {
        renderIntentSecurityPanelFromMemory(consoleShellState, {
          focusSelector: ".intent-security-error",
        });
      }
    } finally {
      // JavaScript 字符串不可原位擦除；覆盖当前提交栈引用，且不把它附加到日志或异常。
      sensitiveText = "";
    }
  });

  document.querySelector("[data-intent-security-cancel]")?.addEventListener("click", () => {
    textarea.value = "";
    intentSecurityPageState.cancel();
    renderIntentSecurityPanelFromMemory(consoleShellState, {
      focusSelector: "textarea[name='text']",
    });
  });

  document.querySelector("[data-intent-security-clear]")?.addEventListener("click", () => {
    textarea.value = "";
    intentSecurityPageState.clear();
    renderIntentSecurityPanelFromMemory(consoleShellState, {
      focusSelector: "textarea[name='text']",
    });
  });
}

/**
 * 局部重绘 PAGE-096 并重新绑定命令。
 * 重绘前再次校验后端导航与实时权限；失败时清空 panel，绝不创建无权工作台或 classify 预取。
 */
function renderIntentSecurityPanelFromMemory(consoleShellState, { focusSelector = null } = {}) {
  const panel = document.querySelector(".intent-security-panel");
  const owner = currentIntentSecurityOwner(consoleShellState);
  if (!panel || !owner) {
    clearIntentSecuritySessionState();
    return;
  }
  panel.innerHTML = renderIntentSecurity(
    intentSecurityPageState.snapshot(),
    authSession.context,
    consoleShellState,
  );
  bindIntentSecurityInteractions(consoleShellState);
  if (focusSelector) document.querySelector(focusSelector)?.focus({ preventScroll: true });
}

/**
 * 根据实时认证、后端导航和当前刷新代次构造 PAGE-096 owner。
 * 返回值不含 Token 或正文；permission revision/fingerprint 已由授权 identity 与导航 owner 双重冻结。
 */
function currentIntentSecurityOwner(consoleShellState) {
  return intentSecurityPageOwner(authSession.context, consoleShellState, {
    refreshId: refreshSequence,
    currentRefreshId: refreshSequence,
  });
}

/**
 * 清除 PAGE-096 全部短生命周期状态。
 * textarea 先原地清空，再 abort 请求、推进 generation 并移除 DOM，覆盖取消、离页、登出、401 和 403。
 */
function clearIntentSecuritySessionState() {
  const textarea = document.querySelector("#intent-security-form textarea[name='text']");
  if (textarea) textarea.value = "";
  intentSecurityPageState.leave();
  document.querySelector(".intent-security-panel")?.replaceChildren();
}

/** tenant、用户、角色、Session、permission revision 或 fingerprint 漂移时清除上一 owner。 */
function clearIntentSecurityStateOnIdentityChange(previous, current) {
  const previousIdentity = authorizationIdentitySnapshot(previous, PERMISSIONS.INTENT_RISK_CLASSIFY);
  const currentIdentity = authorizationIdentitySnapshot(current, PERMISSIONS.INTENT_RISK_CLASSIFY);
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearIntentSecuritySessionState();
  }
}

/** 检查真实 textarea 是否仍持有未提交正文；只返回布尔值，不复制、缓存或记录文本。 */
function intentSecurityHasSensitiveInput() {
  return Boolean(document.querySelector("#intent-security-form textarea[name='text']")?.value);
}

/**
 * 绑定 PAGE-067 行级选择、保存、取消和冲突 rebase。
 * developer 不生成表单；即便测试伪造 DOM，本函数也会同时核对角色、后端 active navigation 和完整身份快照。
 */
function bindRoutingPolicyInteractions(consoleShellState) {
  const identity = routingPolicyIdentitySnapshot(authSession.context);
  if (
    !hasPermission(authSession.context, PERMISSIONS.ROUTING_WRITE)
    || consoleShellState?.activeNavigationItemId !== "routing-policies"
    || !identity
  ) {
    return;
  }

  for (const form of document.querySelectorAll("[data-routing-policy-form]")) {
    const modelAliasId = String(form.dataset.modelAliasId ?? "");
    for (const radio of form.querySelectorAll("input[name='routingMode']")) {
      radio.addEventListener("change", () => {
        if (!radio.checked) {
          return;
        }
        routingPolicyPageMemory.selectMode(modelAliasId, radio.value);
        renderRoutingPolicyPanelFromMemory();
      });
    }

    // 百分比输入只在内存中转换为整数 bps；input 事件局部更新合计和保存门禁，避免重建 DOM 打断键盘输入。
    for (const input of form.querySelectorAll("[data-routing-quality-config] input")) {
      input.addEventListener("input", () => {
        const config = readRoutingQualityCostConfig(form);
        routingPolicyPageMemory.selectQualityCostConfig(modelAliasId, config);
        updateRoutingQualityCostFormState(form, modelAliasId);
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // 保存必须绑定当前认证 Session；Token 已清理时不能让旧表单继续构造写请求。
      if (!token) {
        clearRoutingPolicySessionState();
        renderLogin();
        return;
      }
      // submit 再读取一次 DOM，确保浏览器尚未触发 change 的最后一个键盘输入也进入 ticket 指纹。
      routingPolicyPageMemory.selectQualityCostConfig(modelAliasId, readRoutingQualityCostConfig(form));
      const acquired = routingPolicyPageMemory.acquireSave({
        identity,
        navigation: "routing-policies",
        modelAliasId,
      });
      // 同一个活动 ticket 的重复 submit 不得产生第二个 PATCH。
      if (acquired.state !== "ok" || acquired.reused) {
        return;
      }
      const ticket = acquired.ticket;
      renderRoutingPolicyPanelFromMemory();
      try {
        const result = await updateRoutingMode({
          controlApiUrl,
          token,
          modelAliasId: ticket.modelAliasId,
          routingMode: ticket.routingMode,
          qualityCostConfig: ticket.qualityCostConfig,
          expectedVersion: ticket.baseVersion,
          requestId: ticket.requestId,
          signal: ticket.signal,
        });
        const owner = currentRoutingPolicyOwner();
        if (!routingPolicyPageMemory.isCurrentOperation(ticket, owner)) {
          return;
        }
        if (!routingPolicyPageMemory.acceptSuccess(ticket, result, owner)) {
          // 200 缺少完整安全策略对象时失败关闭，不能用提交草稿伪造服务端新版本。
          routingPolicyPageMemory.acceptFailure(ticket, {
            status: 503,
            code: "model_alias.routing_policy_update_failed",
            message: "路由策略响应不完整",
            requestId: result?.request_id ?? ticket.requestId,
          }, owner);
          renderRoutingPolicyPanelFromMemory({ focusSelector: ".routing-policy-row-feedback" });
          return;
        }
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".routing-policy-row-feedback" });
        // PATCH 响应先成为权威值，再单独 GET 对账；该 GET 使用 refresh generation，不回溯淘汰已接纳的 200。
        await refresh();
      } catch (error) {
        if (ticket.signal.aborted) {
          return;
        }
        const owner = currentRoutingPolicyOwner();
        // 离页、登出、Session/tenant/role 变化后的迟到 401/409 都不能影响新身份。
        if (!routingPolicyPageMemory.isCurrentOperation(ticket, owner)) {
          return;
        }
        // 当前 ticket 收到 401 才能清理 Session；迟到 401 已被上面的 owner 门禁丢弃。
        if (error instanceof AuthApiError && error.status === 401) {
          clearRoutingPolicySessionState();
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        const safeError = error instanceof AuthApiError
          ? error
          : {
              code: "model_alias.routing_policy_update_failed",
              message: "路由策略请求未完成",
              requestId: ticket.requestId,
            };
        routingPolicyPageMemory.acceptFailure(ticket, safeError, owner);
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".routing-policy-row-feedback" });
        // 409 需要读取最新 Policy 形成旁路快照，其他确定错误保留原草稿即可。
        if (safeError.status === 409) {
          // 冲突后的 GET 只写 side_authoritative_snapshot，管理员必须显式载入后重新选择。
          await refresh();
        }
      }
    });
  }

  for (const button of document.querySelectorAll("[data-routing-policy-cancel]")) {
    button.addEventListener("click", () => {
      const modelAliasId = button.closest("[data-routing-policy-form]")?.dataset.modelAliasId;
      if (modelAliasId && routingPolicyPageMemory.cancel(modelAliasId)) {
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".routing-policy-row-feedback" });
      }
    });
  }

  for (const button of document.querySelectorAll("[data-routing-policy-rebase]")) {
    button.addEventListener("click", () => {
      const modelAliasId = button.closest("[data-routing-policy-form]")?.dataset.modelAliasId;
      if (modelAliasId && routingPolicyPageMemory.rebase(modelAliasId)) {
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".routing-policy-row-feedback" });
      }
    });
  }
  bindCacheAffinityInteractions(consoleShellState, identity);
}

/**
 * 绑定 PAGE-072 CacheAffinity 表单、CAS 保存和显式冲突 rebase。
 * 即使伪造 DOM，也必须同时满足 admin、后端 active navigation 和完整身份 owner。
 */
function bindCacheAffinityInteractions(consoleShellState, identity) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.ROUTING_WRITE)
    || consoleShellState?.activeNavigationItemId !== "routing-policies"
    || !identity
  ) {
    return;
  }
  for (const form of document.querySelectorAll("[data-cache-affinity-form]")) {
    const modelAliasId = String(form.dataset.modelAliasId ?? "");
    const syncDraft = () => {
      cacheAffinityPageMemory.selectDraft(modelAliasId, readCacheAffinityDraft(form));
      updateCacheAffinityFormState(form, modelAliasId);
    };
    const dataClassInput = form.elements.cacheAffinityDataClass;
    /** 无参数、无返回值；在 input 阶段统一 DOM 与草稿的 trim 语义，必须早于 submit 原生 pattern 校验。 */
    const syncDataClassDraft = () => {
      const normalizedDataClass = cacheAffinityDataClassValue(dataClassInput?.value);
      // 浏览器在 submit 事件前执行原生 pattern 校验；必须在 input 时同步 DOM，避免内存已 trim 而 DOM 仍被拦截。
      if (dataClassInput && dataClassInput.value !== (normalizedDataClass ?? "")) {
        dataClassInput.value = normalizedDataClass ?? "";
      }
      syncDraft();
    };
    for (const input of form.querySelectorAll("input:not([type='checkbox'])")) {
      input.addEventListener("input", input === dataClassInput ? syncDataClassDraft : syncDraft);
    }
    form.elements.cacheAffinityEnabled?.addEventListener("change", syncDraft);
    form.elements.cacheAffinityTtl?.addEventListener("change", syncDraft);

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = authSession.token;
      // Session 已清理时旧表单不得继续构造配置写请求或保留上一租户草稿。
      if (!token) {
        clearRoutingPolicySessionState();
        renderLogin();
        return;
      }
      cacheAffinityPageMemory.selectDraft(modelAliasId, readCacheAffinityDraft(form));
      const acquired = cacheAffinityPageMemory.acquireSave({
        identity,
        navigation: "routing-policies",
        modelAliasId,
      });
      // 重复 submit 复用同一活动 ticket，不能并发发送第二个 PATCH。
      if (acquired.state !== "ok" || acquired.reused) {
        return;
      }
      const ticket = acquired.ticket;
      renderRoutingPolicyPanelFromMemory();
      try {
        const result = await updateCacheAffinity({
          controlApiUrl,
          token,
          modelAliasId: ticket.modelAliasId,
          status: ticket.draft.status,
          dataClass: ticket.draft.data_class,
          minimumPrefixChars: ticket.draft.minimum_prefix_chars,
          ttlSeconds: ticket.draft.ttl_seconds,
          expectedVersion: ticket.baseVersion,
          requestId: ticket.requestId,
          signal: ticket.signal,
        });
        const owner = currentRoutingPolicyOwner();
        if (!cacheAffinityPageMemory.isCurrentOperation(ticket, owner)) {
          return;
        }
        if (!cacheAffinityPageMemory.acceptSuccess(ticket, result, owner)) {
          // 缺少精确配置、CAS +1 或 request_id 的 200 不能成为页面权威值。
          cacheAffinityPageMemory.acceptFailure(ticket, {
            status: 503,
            code: "cache_affinity.update_failed",
            message: "Prompt Cache 亲和配置响应不完整",
            requestId: result?.request_id ?? ticket.requestId,
          }, owner);
          renderRoutingPolicyPanelFromMemory({ focusSelector: ".cache-affinity-feedback" });
          return;
        }
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".cache-affinity-feedback" });
        // 已接纳 PATCH 后再用同租户 GET 对账；刷新只能写 clean 或旁路权威状态。
        await refresh();
      } catch (error) {
        if (ticket.signal.aborted) {
          return;
        }
        const owner = currentRoutingPolicyOwner();
        // 身份、Session、角色或导航已经变化时，迟到错误不得影响新页面。
        if (!cacheAffinityPageMemory.isCurrentOperation(ticket, owner)) {
          return;
        }
        // 401 表示当前认证边界已经失效，必须先销毁跨租户草稿与请求所有权再回到登录页。
        if (error instanceof AuthApiError && error.status === 401) {
          clearRoutingPolicySessionState();
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        const safeError = error instanceof AuthApiError
          ? error
          : {
              code: "cache_affinity.update_failed",
              message: "Prompt Cache 亲和配置请求未完成",
              requestId: ticket.requestId,
            };
        cacheAffinityPageMemory.acceptFailure(ticket, safeError, owner);
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".cache-affinity-feedback" });
        // 409 不执行 last-write-wins，只读取旁路最新值并等待管理员显式载入。
        if (safeError.status === 409) {
          await refresh();
        }
      }
    });

    form.querySelector("[data-cache-affinity-cancel]")?.addEventListener("click", () => {
      if (cacheAffinityPageMemory.cancel(modelAliasId)) {
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".cache-affinity-feedback" });
      }
    });
    form.querySelector("[data-cache-affinity-rebase]")?.addEventListener("click", () => {
      if (cacheAffinityPageMemory.rebase(modelAliasId)) {
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".cache-affinity-feedback" });
      }
    });
  }
}

/** 从 CacheAffinity 表单读取完整四字段草稿，不把 tenant、Alias 或隐藏属性放入 payload。 */
function readCacheAffinityDraft(form) {
  const dataClass = cacheAffinityDataClassValue(form.elements.cacheAffinityDataClass?.value);
  return {
    status: form.elements.cacheAffinityEnabled?.checked ? "enabled" : "disabled",
    data_class: dataClass || null,
    minimum_prefix_chars: cacheAffinityMinimumPrefixValue(
      form.elements.cacheAffinityMinimumPrefix?.value,
    ),
    ttl_seconds: cacheAffinityTtlValue(form.elements.cacheAffinityTtl?.value),
  };
}

/** 局部更新亲和表单的保存门禁和反馈，避免输入时替换 DOM 打断键盘操作。 */
function updateCacheAffinityFormState(form, modelAliasId) {
  const row = cacheAffinityPageMemory.snapshot().rows.find((item) => item.modelAliasId === modelAliasId);
  if (!row) {
    return;
  }
  const valid = isCacheAffinityDraftValid(row.draft);
  const submit = form.querySelector("button[type='submit']");
  if (submit) {
    submit.disabled = !row.dirty || row.busy || row.conflicted || !valid;
  }
  const cancel = form.querySelector("[data-cache-affinity-cancel]");
  if (cancel) {
    cancel.disabled = !row.dirty && !row.busy && !row.conflicted;
  }
  const output = form.querySelector("[data-cache-affinity-draft-status]");
  if (output) {
    output.textContent = row.dirty
      ? valid
        ? "存在未保存的亲和配置"
        : "请检查分类 code、前缀门限和 TTL"
      : row.authoritative
        ? `当前版本 v${row.baseVersion}`
        : "尚未创建配置";
  }
}

/**
 * 从 PAGE-068 表单读取完整质量成本配置并精确转换为整数 bps。
 * 空值、指数、超过两位小数、越界百分比和非整数样本均转换为 null，使内存状态关闭保存。
 */
function readRoutingQualityCostConfig(form) {
  return {
    quality_weight_bps: routingQualityPercentToBps(form.elements.qualityWeightPercent?.value),
    cost_weight_bps: routingQualityPercentToBps(form.elements.costWeightPercent?.value),
    latency_weight_bps: routingQualityPercentToBps(form.elements.latencyWeightPercent?.value),
    quality_floor_bps: routingQualityPercentToBps(form.elements.qualityFloorPercent?.value),
    minimum_sample_count: routingMinimumSampleCount(form.elements.minimumSampleCount?.value),
  };
}

/**
 * 在不替换输入节点的前提下刷新权重合计和保存按钮。
 * 这里仅改善交互，后端仍会独立校验完整配置、CAS、admin 权限和租户边界。
 */
function updateRoutingQualityCostFormState(form, modelAliasId) {
  const row = routingPolicyPageMemory.snapshot().rows.find((item) => item.modelAliasId === modelAliasId);
  if (!row) {
    return;
  }
  const config = row.draftQualityCostConfig;
  const weights = [config?.quality_weight_bps, config?.cost_weight_bps, config?.latency_weight_bps];
  const total = weights.every((value) => typeof value === "number" && Number.isSafeInteger(value))
    ? weights.reduce((sum, value) => sum + value, 0)
    : null;
  const valid = row.draftMode !== "quality_cost" || isRoutingQualityCostConfigValid(config);
  const output = form.querySelector("[data-routing-quality-total]");
  if (output) {
    output.textContent = `权重合计：${total == null ? "输入不完整" : formatRoutingBps(total)}`;
    output.classList.toggle("is-valid", valid);
    output.classList.toggle("is-invalid", !valid);
  }
  const submit = form.querySelector("button[type='submit']");
  if (submit) {
    submit.disabled = !row.dirty || row.busy || row.conflicted || !valid;
  }
}

/** 使用整数拆分格式化 bps，避免实时合计通过浮点除法产生显示漂移。 */
function formatRoutingBps(value) {
  const whole = Math.floor(value / 100);
  const fraction = String(value % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}%`;
}

/** 局部重绘 PAGE-067 并重新绑定行事件；不存在 active panel 时不创建隐藏 DOM。 */
function renderRoutingPolicyPanelFromMemory({ focusSelector = null } = {}) {
  const panel = document.querySelector(".routing-policies-panel");
  if (!panel || !hasPermission(authSession.context, PERMISSIONS.ROUTING_READ)) {
    return;
  }
  panel.innerHTML = renderRoutingPolicyPanel({
    ...routingPolicyPageMemory.snapshot(),
    cacheAffinity: cacheAffinityPageMemory.snapshot(),
  }, authSession.context);
  bindRoutingPolicyInteractions({ activeNavigationItemId: "routing-policies" });
  if (focusSelector) {
    document.querySelector(focusSelector)?.focus({ preventScroll: true });
  }
}

/** 返回当前 PATCH 回调使用的 owner；导航 DOM 消失时用空值使迟到响应失败。 */
function currentRoutingPolicyOwner() {
  return {
    identity: routingPolicyIdentitySnapshot(authSession.context),
    navigation: document.querySelector(".routing-policies-panel") ? "routing-policies" : "",
  };
}

/** 离页、登出、401、pagehide 或导航裁剪时 abort PAGE-072 两类 ticket 并清空 DOM。 */
function clearRoutingPolicySessionState() {
  routingPolicyPageMemory.invalidate();
  cacheAffinityPageMemory.invalidate();
  document.querySelector(".routing-policies-panel")?.replaceChildren();
}

/** tenant、用户、角色或 Session revision 变化时立即销毁旧身份草稿。 */
function clearRoutingPolicyStateOnIdentityChange(previous, current) {
  const previousIdentity = routingPolicyIdentitySnapshot(previous);
  const currentIdentity = routingPolicyIdentitySnapshot(current);
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearRoutingPolicySessionState();
  }
}

/**
 * 绑定 PAGE-069 筛选、分页和行级人工评分交互。
 * developer 只绑定读取操作；admin 写入仍须同时通过 DOM、后端 active 导航和完整身份快照门禁。
 */
function bindModelQualityScoreInteractions(consoleShellState) {
  const identity = modelQualityScoreIdentitySnapshot(authSession.context);
  if (
    !hasPermission(authSession.context, PERMISSIONS.MODEL_QUALITY_READ)
    || consoleShellState?.activeNavigationItemId !== "model-quality-scores"
    || !identity
  ) {
    return;
  }

  const filterForm = document.querySelector("#model-quality-filter-form");
  filterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    modelQualityScoreFilters = {
      ...modelQualityScoreFilters,
      modelId: String(new FormData(filterForm).get("modelId") ?? "").trim(),
      offset: 0,
    };
    // 查询 owner 变化立即 abort 旧 GET/PATCH，不能等完整刷新经过其他接口后才失效。
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });
  document.querySelector("#model-quality-filter-reset")?.addEventListener("click", async () => {
    modelQualityScoreFilters = { modelId: "", limit: 100, offset: 0 };
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });
  document.querySelector("#model-quality-prev")?.addEventListener("click", async () => {
    modelQualityScoreFilters = {
      ...modelQualityScoreFilters,
      offset: Math.max(0, modelQualityScoreFilters.offset - modelQualityScoreFilters.limit),
    };
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });
  document.querySelector("#model-quality-next")?.addEventListener("click", async () => {
    modelQualityScoreFilters = {
      ...modelQualityScoreFilters,
      offset: modelQualityScoreFilters.offset + modelQualityScoreFilters.limit,
    };
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });

  // developer 不会生成表单；即便测试伪造写 DOM，这个角色门禁也阻止构造 PATCH。
  if (!hasPermission(authSession.context, PERMISSIONS.MODEL_QUALITY_WRITE)) {
    return;
  }
  for (const form of document.querySelectorAll("[data-model-quality-form]")) {
    const modelId = String(form.dataset.modelId ?? "");
    const input = form.elements.manualQualityPercent;
    input?.addEventListener("input", () => {
      const draft = modelQualityDraftFromElement(input);
      input.setCustomValidity(draft.valid ? "" : "请输入 0 到 100 之间、最多两位小数的评分");
      if (draft.valid) {
        modelQualityScorePageMemory.selectManualQuality(modelId, draft.value);
      }
      updateModelQualityFormState(form, modelId, draft.valid);
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const draft = modelQualityDraftFromElement(input);
      if (!draft.valid) {
        input?.reportValidity();
        return;
      }
      modelQualityScorePageMemory.selectManualQuality(modelId, draft.value);
      const token = authSession.token;
      // Token 缺失表示认证 owner 已消失，旧表单不得继续构造包含人工评分意图的 PATCH。
      if (!token) {
        clearModelQualityScoreSessionState();
        renderLogin();
        return;
      }
      const acquired = modelQualityScorePageMemory.acquireSave({
        identity,
        navigation: "model-quality-scores",
        modelId,
      });
      if (acquired.state !== "ok" || acquired.reused) {
        updateModelQualityFormState(form, modelId, true);
        return;
      }
      const ticket = acquired.ticket;
      renderModelQualityScorePanelFromMemory();
      try {
        const result = await updateModelQualityScore({
          controlApiUrl,
          token,
          modelId: ticket.modelId,
          manualQualityBps: ticket.manualQualityBps,
          expectedFeedbackVersion: ticket.baseFeedbackVersion,
          requestId: ticket.requestId,
          signal: ticket.signal,
        });
        const owner = currentModelQualityScoreOwner();
        if (!modelQualityScorePageMemory.isCurrentOperation(ticket, owner)) {
          return;
        }
        if (!modelQualityScorePageMemory.acceptSuccess(ticket, result, owner)) {
          modelQualityScorePageMemory.acceptFailure(ticket, {
            status: 503,
            code: "model_quality_score.update_failed",
            message: "模型评分响应不完整",
            requestId: result?.request_id ?? ticket.requestId,
          }, owner);
          renderModelQualityScorePanelFromMemory({ focusModelId: ticket.modelId });
          return;
        }
        renderModelQualityScorePanelFromMemory({ focusModelId: ticket.modelId });
        // PATCH 响应先成为权威值，随后 GET 只更新统计与旁路事实，不回溯淘汰已接纳的写结果。
        await refresh();
      } catch (error) {
        if (ticket.signal.aborted) {
          return;
        }
        const owner = currentModelQualityScoreOwner();
        // 离页、登出或身份变化后的迟到 401/409 不得清理新会话或覆盖新租户草稿。
        if (!modelQualityScorePageMemory.isCurrentOperation(ticket, owner)) {
          return;
        }
        // 当前 ticket 的 401 才能结束会话；上面的 owner 门禁已先排除跨身份迟到响应。
        if (error instanceof AuthApiError && error.status === 401) {
          clearModelQualityScoreSessionState();
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        const safeError = error instanceof AuthApiError
          ? error
          : {
              code: "model_quality_score.update_failed",
              message: "模型评分请求未完成",
              requestId: ticket.requestId,
            };
        modelQualityScorePageMemory.acceptFailure(ticket, safeError, owner);
        renderModelQualityScorePanelFromMemory({ focusModelId: ticket.modelId });
        // 409 只触发旁路刷新，不能静默把服务器版本合并成用户原草稿并自动重试。
        if (safeError.status === 409) {
          // 冲突后的 GET 只形成旁路权威快照，管理员仍须显式载入并再次确认。
          await refresh();
        }
      }
    });
  }

  for (const button of document.querySelectorAll("[data-model-quality-clear]")) {
    button.addEventListener("click", () => {
      const modelId = button.closest("[data-model-quality-form]")?.dataset.modelId;
      if (modelId && modelQualityScorePageMemory.selectManualQuality(modelId, null)) {
        renderModelQualityScorePanelFromMemory();
      }
    });
  }
  for (const button of document.querySelectorAll("[data-model-quality-cancel]")) {
    button.addEventListener("click", () => {
      const modelId = button.closest("[data-model-quality-form]")?.dataset.modelId;
      if (modelId && modelQualityScorePageMemory.cancel(modelId)) {
        renderModelQualityScorePanelFromMemory({ focusModelId: modelId });
      }
    });
  }
  for (const button of document.querySelectorAll("[data-model-quality-rebase]")) {
    button.addEventListener("click", () => {
      const modelId = button.closest("[data-model-quality-form]")?.dataset.modelId;
      if (modelId && modelQualityScorePageMemory.rebase(modelId)) {
        renderModelQualityScorePanelFromMemory({ focusModelId: modelId });
      }
    });
  }
}

/** 空输入表示显式清除；其他文本必须精确转换为 0..10000 bps。 */
function modelQualityDraftFromInput(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    return { valid: true, value: null };
  }
  const bps = qualityPercentToBps(normalized);
  return bps === null ? { valid: false, value: null } : { valid: true, value: bps };
}

/** number 控件的 badInput 状态不能被空字符串误判为“清除评分”。 */
function modelQualityDraftFromElement(input) {
  if (!input || input.validity?.badInput) {
    return { valid: false, value: null };
  }
  return modelQualityDraftFromInput(input.value);
}

/** 局部更新 PAGE-069 保存门禁，不替换正在输入的 number 节点。 */
function updateModelQualityFormState(form, modelId, inputValid) {
  const row = modelQualityScorePageMemory.snapshot().rows.find((item) => item.modelId === modelId);
  const submit = form.querySelector("button[type='submit']");
  if (submit) {
    submit.disabled = !inputValid || !row?.dirty || row.busy || row.conflicted || !row.baseFeedbackVersion;
  }
}

/** 局部重绘 PAGE-069 并重新绑定事件；保存反馈只把焦点放回对应模型行。 */
function renderModelQualityScorePanelFromMemory({ focusModelId = null } = {}) {
  const panel = document.querySelector(".model-quality-scores-panel");
  if (!panel || !hasPermission(authSession.context, PERMISSIONS.MODEL_QUALITY_READ)) {
    return;
  }
  panel.innerHTML = renderModelQualityScoresPanel(
    modelQualityScorePageMemory.snapshot(),
    authSession.context,
    modelQualityScoreFilters,
  );
  bindModelQualityScoreInteractions({ activeNavigationItemId: "model-quality-scores" });
  if (focusModelId) {
    const row = [...document.querySelectorAll("[data-model-quality-row]")]
      .find((item) => item.dataset.modelQualityRow === focusModelId);
    row?.querySelector(".model-quality-row-feedback")?.focus({ preventScroll: true });
  }
}

/** 返回 PAGE-069 PATCH 的实时 owner；DOM 已销毁时空导航会使迟到响应失败关闭。 */
function currentModelQualityScoreOwner() {
  return {
    identity: modelQualityScoreIdentitySnapshot(authSession.context),
    navigation: document.querySelector(".model-quality-scores-panel") ? "model-quality-scores" : "",
  };
}

/** 离页、登出、401、pagehide 或导航裁剪时 abort PAGE-069 GET/PATCH，并清空筛选与 DOM。 */
function clearModelQualityScoreSessionState() {
  modelQualityScorePageMemory.invalidate();
  modelQualityScoreFilters = { modelId: "", limit: 100, offset: 0 };
  document.querySelector(".model-quality-scores-panel")?.replaceChildren();
}

/** tenant、用户、角色或 Session revision 变化时销毁上一身份的评分草稿和异步 owner。 */
function clearModelQualityScoreStateOnIdentityChange(previous, current) {
  const previousIdentity = modelQualityScoreIdentitySnapshot(previous);
  const currentIdentity = modelQualityScoreIdentitySnapshot(current);
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearModelQualityScoreSessionState();
  }
}

/**
 * 绑定 PAGE-081 筛选、分页、编辑和启停交互。
 * developer 只绑定 GET 命令；admin 写入还要同时通过实时身份、后端 active 导航和 DOM owner 门禁。
 */
function bindErrorMapInteractions(consoleShellState) {
  const identity = errorMapIdentitySnapshot(authSession.context);
  if (
    !hasPermission(authSession.context, PERMISSIONS.PROVIDER_ERROR_MAP_READ)
    || consoleShellState?.activeNavigationItemId !== "error-maps"
    || !identity
  ) {
    return;
  }
  const filterForm = document.querySelector("#error-map-filter-form");
  filterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(filterForm);
    errorMapFilters = {
      ...errorMapFilters,
      providerId: String(data.get("providerId") ?? "").trim(),
      source: String(data.get("source") ?? "").trim(),
      status: String(data.get("status") ?? "").trim(),
      outputErrorCode: String(data.get("outputErrorCode") ?? "").trim(),
      offset: 0,
    };
    errorMapPageMemory.leave();
    await refresh();
  });
  document.querySelector("#error-map-filter-reset")?.addEventListener("click", async () => {
    errorMapFilters = { providerId: "", source: "", status: "", outputErrorCode: "", limit: 100, offset: 0 };
    errorMapPageMemory.leave();
    await refresh();
  });
  document.querySelector("#error-map-refresh")?.addEventListener("click", async () => {
    errorMapPageMemory.leave();
    await refresh();
  });
  document.querySelector("#error-map-prev")?.addEventListener("click", async () => {
    errorMapFilters = { ...errorMapFilters, offset: Math.max(0, errorMapFilters.offset - errorMapFilters.limit) };
    errorMapPageMemory.leave();
    await refresh();
  });
  document.querySelector("#error-map-next")?.addEventListener("click", async () => {
    errorMapFilters = { ...errorMapFilters, offset: errorMapFilters.offset + errorMapFilters.limit };
    errorMapPageMemory.leave();
    await refresh();
  });

  // developer 不绑定任何写 listener；即使伪造按钮，也不会从主应用构造 POST/PATCH。
  if (!hasPermission(authSession.context, PERMISSIONS.PROVIDER_ERROR_MAP_WRITE)) {
    return;
  }
  document.querySelector("#error-map-create")?.addEventListener("click", (event) => {
    const restoreState = captureCurrentErrorMapDialogRestoreState(event.currentTarget, identity);
    if (!restoreState) {
      return;
    }
    // 创建入口只在当前 admin owner 可写时打开对话框，失败时不得渲染可提交表单。
    if (errorMapPageMemory.openCreate()) {
      errorMapDialogRestoreState = restoreState;
      renderErrorMapPanelFromMemory({ focusDialog: true });
    }
  });
  for (const button of document.querySelectorAll("[data-error-map-edit]")) {
    button.addEventListener("click", () => {
      const restoreState = captureCurrentErrorMapDialogRestoreState(button, identity);
      if (!restoreState) {
        return;
      }
      // 编辑入口再次校验租户规则可写性，builtin 与失效资源不会生成草稿。
      if (errorMapPageMemory.openEdit(button.dataset.errorMapEdit)) {
        errorMapDialogRestoreState = restoreState;
        renderErrorMapPanelFromMemory({ focusDialog: true });
      }
    });
  }
  for (const button of document.querySelectorAll("[data-error-map-status]")) {
    button.addEventListener("click", async () => {
      const restoreState = captureCurrentErrorMapDialogRestoreState(button, identity);
      if (!restoreState) {
        return;
      }
      // 启停先冻结资源版本和目标状态，最终 PATCH 仍由后端执行 RBAC 与 CAS 校验。
      if (errorMapPageMemory.prepareStatusChange(
        button.dataset.errorMapStatus,
        button.dataset.nextStatus,
      )) {
        errorMapDialogRestoreState = restoreState;
        renderErrorMapPanelFromMemory({ focusDialog: true });
        await submitCurrentErrorMapMutation();
      }
    });
  }

  const editor = document.querySelector("#error-map-editor-form");
  for (const control of editor?.elements ?? []) {
    if (control?.name) {
      control.addEventListener("input", () => syncErrorMapDraft(editor));
      control.addEventListener("change", () => syncErrorMapDraft(editor));
    }
  }
  editor?.addEventListener("submit", async (event) => {
    event.preventDefault();
    // 浏览器约束失败时不发送写请求，避免无效草稿占用或错误复用幂等键。
    if (!syncErrorMapDraft(editor, { report: true })) {
      return;
    }
    await submitCurrentErrorMapMutation();
  });
  for (const closeButton of document.querySelectorAll("[data-error-map-close], [data-error-map-cancel]")) {
    closeButton.addEventListener("click", () => closeCurrentErrorMapDialog());
  }
  document.querySelector("[data-error-map-adopt]")?.addEventListener("click", () => {
    // 只有同一 owner 的冲突精确读取可被显式采纳，跨租户或迟到结果必须失败关闭。
    if (errorMapPageMemory.adoptConflict()) {
      renderErrorMapPanelFromMemory({ focusDialog: true });
    }
  });
  bindErrorMapDialogLifecycle();
}

/**
 * 将 dialog 控件同步到内存并执行最小浏览器约束。
 * guard、状态输出矩阵、资源冲突和租户归属仍由后端重复校验，前端校验只用于即时反馈。
 */
function syncErrorMapDraft(form, { report = false } = {}) {
  if (!form) {
    return false;
  }
  const draft = errorMapDraftFromFormData(new FormData(form));
  const statusInput = form.elements.httpStatuses;
  const priorityInput = form.elements.priority;
  const hasInvalidStatus = draft.http_statuses.some((value) => (
    !Number.isSafeInteger(value) || value < 100 || value > 599
  ));
  const hasCondition = draft.http_statuses.length > 0
    || Boolean(draft.http_status_class)
    || Boolean(draft.field_pattern)
    || Boolean(draft.message_pattern);
  let message = "";
  if (hasInvalidStatus) {
    message = "HTTP 状态必须是 100 到 599 的三位整数，并使用逗号分隔";
  } else if (draft.http_statuses.length && draft.http_status_class) {
    message = "精确 HTTP 状态与状态类不能同时设置";
  } else if (!hasCondition) {
    message = "至少设置一个 HTTP、错误字段或 message 条件";
  } else if (draft.message_pattern && !draft.provider_id && !draft.http_statuses.length && !draft.http_status_class && !draft.field_pattern) {
    message = "仅使用 message 条件时必须绑定具体 Provider";
  }
  statusInput?.setCustomValidity(message);
  const priorityValid = Number.isSafeInteger(draft.priority) && draft.priority >= 1 && draft.priority <= 1000;
  priorityInput?.setCustomValidity(priorityValid ? "" : "优先级必须是 1 到 1000 的整数");
  const valid = !message && priorityValid;
  if (valid) {
    errorMapPageMemory.updateDraft(draft);
  }
  const submit = form.querySelector("button[type='submit']");
  const dialogState = errorMapPageMemory.snapshot().dialog;
  if (submit) {
    submit.disabled = !valid || !dialogState?.dirty || dialogState.busy || dialogState.conflicted;
  }
  if (report && !valid) {
    (message ? statusInput : priorityInput)?.reportValidity();
  }
  return valid;
}

/** 发出当前 dialog 的 POST/PATCH，并在每个回调前复核完整 owner。 */
async function submitCurrentErrorMapMutation() {
  const token = authSession.token;
  const owner = currentErrorMapOwner();
  // 写操作必须同时持有认证 Token 与当前页面 owner，任一失配都先销毁本地会话状态。
  if (!token || !owner.identity || owner.navigation !== "error-maps") {
    clearErrorMapSessionState();
    renderLogin();
    return;
  }
  const acquired = errorMapPageMemory.acquireMutation(owner);
  if (acquired.state !== "ok" || acquired.reused) {
    return;
  }
  const ticket = acquired.ticket;
  renderErrorMapPanelFromMemory({ focusDialog: true });
  try {
    const result = ticket.mode === "create"
      ? await createErrorMap({
          controlApiUrl,
          token,
          draft: ticket.draft,
          requestId: ticket.requestId,
          signal: ticket.signal,
        })
      : await updateErrorMap({
          controlApiUrl,
          token,
          errorMapId: ticket.errorMapId,
          expectedVersion: ticket.expectedVersion,
          draft: ticket.draft,
          requestId: ticket.requestId,
          signal: ticket.signal,
        });
    const currentOwner = currentErrorMapOwner();
    // 网络返回后复核实时 owner，迟到成功不得写入另一身份或已经离开的页面。
    if (!errorMapPageMemory.isCurrentOperation(ticket, currentOwner)) {
      return;
    }
    // 成功响应仍需通过完整 DTO 与 ticket 校验，损坏响应按安全错误保留当前草稿。
    if (!errorMapPageMemory.acceptMutationSuccess(ticket, result, currentOwner)) {
      errorMapPageMemory.acceptMutationFailure(ticket, {
        status: 502,
        code: "error_map.response_invalid",
        message: "错误规则响应不完整",
        requestId: result?.request_id ?? "-",
      }, currentOwner);
      renderErrorMapPanelFromMemory({ focusDialog: true });
      return;
    }
    renderErrorMapPanelFromMemory();
    await refresh();
  } catch (error) {
    if (ticket.signal.aborted) {
      return;
    }
    const currentOwner = currentErrorMapOwner();
    // 异常回调同样执行 owner 复核，旧 Session 的失败不得覆盖新用户页面。
    if (!errorMapPageMemory.isCurrentOperation(ticket, currentOwner)) {
      return;
    }
    // 后端 401 表示认证边界失效，必须清空规则状态和 Session 后回到登录页。
    if (error instanceof AuthApiError && error.status === 401) {
      clearErrorMapSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    const safeError = error instanceof AuthApiError
      ? error
      : { code: "error_map.network_unknown", message: "错误规则请求结果未知", requestId: ticket.requestId };
    errorMapPageMemory.acceptMutationFailure(ticket, safeError, currentOwner);
    renderErrorMapPanelFromMemory({ focusDialog: true });
    // 只有明确 409 才触发独立精确 GET；普通错误和网络未知不会擅自覆盖用户草稿。
    if (safeError.status === 409) {
      await refreshCurrentErrorMapConflict();
    }
  }
}

/** 409 后按稳定资源 ID 独立读取最新版本；该 ticket 与列表 GET、旧 PATCH 均不共享 request_id。 */
async function refreshCurrentErrorMapConflict() {
  const owner = currentErrorMapOwner();
  const ticket = errorMapPageMemory.beginConflictRefresh(owner);
  // 冲突重取必须拥有独立 ticket 和当前认证 Token，缺失时不发任何宽泛读取请求。
  if (!ticket || !authSession.token) {
    return;
  }
  try {
    const result = await listErrorMaps({
      controlApiUrl,
      token: authSession.token,
      errorMapId: ticket.errorMapId,
      signal: ticket.signal,
      requestId: ticket.requestId,
    });
    const currentOwner = currentErrorMapOwner();
    // 精确 GET 结果仅在资源、请求 ID 与完整 owner 都匹配时进入待采纳状态。
    if (errorMapPageMemory.acceptConflictRefresh(ticket, result, currentOwner)) {
      renderErrorMapPanelFromMemory({ focusDialog: true });
    }
  } catch (error) {
    if (ticket.signal.aborted) {
      return;
    }
    const currentOwner = currentErrorMapOwner();
    // 冲突读取的迟到失败不能污染已切换身份或已关闭的编辑器。
    if (!errorMapPageMemory.isCurrentConflict(ticket, currentOwner)) {
      return;
    }
    // 冲突读取收到 401 时同步销毁认证与草稿，禁止保留上一 Session 的规则内容。
    if (error instanceof AuthApiError && error.status === 401) {
      clearErrorMapSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    errorMapPageMemory.acceptConflictError(ticket, error, currentOwner);
    renderErrorMapPanelFromMemory({ focusDialog: true });
  }
}

/** 局部重绘 PAGE-081；编辑期绝不调用 app.innerHTML，也不触发其他租户接口。 */
function renderErrorMapPanelFromMemory({ focusDialog = false } = {}) {
  const panel = document.querySelector(".error-maps-panel");
  if (!panel || !hasPermission(authSession.context, PERMISSIONS.PROVIDER_ERROR_MAP_READ)) {
    return;
  }
  panel.innerHTML = renderErrorMapPanel(
    errorMapPageMemory.snapshot(),
    authSession.context,
    errorMapFilters,
  );
  bindErrorMapInteractions({ activeNavigationItemId: "error-maps" });
  if (focusDialog) {
    document.querySelector("#error-map-editor-form [autofocus]")?.focus({ preventScroll: true });
  }
}

/** 安装单层 dialog 的滚动锁、Escape 与 Tab/Shift+Tab 焦点陷阱。 */
function bindErrorMapDialogLifecycle() {
  const dialog = document.querySelector(".error-map-dialog");
  if (!dialog) {
    releaseErrorMapDialogLifecycle({ restoreFocus: true });
    return;
  }
  // 首次打开对话框时冻结原始滚动状态，嵌套重绘不能覆盖该恢复基线。
  if (errorMapBodyOverflow === null) {
    errorMapBodyOverflow = document.body.style.overflow;
  }
  document.body.style.overflow = "hidden";
  // 重绑焦点陷阱前先移除旧监听器，避免一次按键触发多个跨 owner 回调。
  if (errorMapDialogKeydownHandler) {
    document.removeEventListener("keydown", errorMapDialogKeydownHandler);
  }
  errorMapDialogKeydownHandler = (event) => {
    const currentDialog = document.querySelector(".error-map-dialog");
    const busy = currentDialog?.getAttribute("aria-busy") === "true";
    if (!currentDialog) {
      return;
    }
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      closeCurrentErrorMapDialog();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const focusable = [...currentDialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]')];
    if (!focusable.length) {
      event.preventDefault();
      currentDialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  document.addEventListener("keydown", errorMapDialogKeydownHandler);
}

/** 关闭当前非 busy dialog，并仅在相同身份 owner 下恢复仍连接的触发器焦点。 */
function closeCurrentErrorMapDialog() {
  if (errorMapPageMemory.closeDialog()) {
    renderErrorMapPanelFromMemory();
  }
}

/**
 * 捕获打开 dialog 的稳定触发器身份。
 * 状态机打开前先比较 listener 冻结身份与实时 owner，并把节点限定在当前 PAGE-081 面板。
 */
function captureCurrentErrorMapDialogRestoreState(target, identity) {
  const owner = currentErrorMapOwner();
  // 身份和导航必须与 listener 快照完全相同，旧 Session 回调在进入状态机前失败关闭。
  if (owner.identity !== identity || owner.navigation !== "error-maps") {
    return null;
  }
  return createErrorMapDialogRestoreState(target, {
    ownerIdentity: identity,
    navigation: owner.navigation,
    panel: document.querySelector(".error-maps-panel"),
  });
}

/** 始终解除滚动锁和 document listener；身份变化后不恢复上一用户的焦点节点。 */
function releaseErrorMapDialogLifecycle({ restoreFocus = false } = {}) {
  // 离页和身份变化都必须同步移除 document 监听器，防止旧草稿继续接收键盘事件。
  if (errorMapDialogKeydownHandler) {
    document.removeEventListener("keydown", errorMapDialogKeydownHandler);
    errorMapDialogKeydownHandler = null;
  }
  // 仅在保存过滚动基线时恢复页面状态，不能把 null 写回新的页面 owner。
  if (errorMapBodyOverflow !== null) {
    document.body.style.overflow = errorMapBodyOverflow;
    errorMapBodyOverflow = null;
  }
  // descriptor 先单次消费并清空；即使解析或 focus 抛错，也不能被下一次 dialog 重放。
  const restoreState = errorMapDialogRestoreState;
  errorMapDialogRestoreState = null;
  const owner = currentErrorMapOwner();
  const target = restoreFocus
    ? resolveErrorMapDialogRestoreTarget(restoreState, {
        ownerIdentity: owner.identity,
        navigation: owner.navigation,
        panel: document.querySelector(".error-maps-panel"),
      })
    : null;
  if (target) {
    target.focus({ preventScroll: true });
  }
}

/** 返回 PAGE-081 当前实时 owner；DOM 被销毁时空导航会让迟到响应失败关闭。 */
function currentErrorMapOwner() {
  return {
    identity: errorMapIdentitySnapshot(authSession.context),
    navigation: document.querySelector(".error-maps-panel") ? "error-maps" : "",
  };
}

/** 离页、登出、401、降权或 pagehide 时同步 abort 三类 ticket，清空 dialog 并解除滚动锁。 */
function clearErrorMapSessionState() {
  errorMapHandoff.clear();
  errorMapPageMemory.leave();
  errorMapFilters = { providerId: "", source: "", status: "", outputErrorCode: "", limit: 100, offset: 0 };
  releaseErrorMapDialogLifecycle();
  document.querySelector(".error-maps-panel")?.replaceChildren();
}

/** 完整身份任一维度变化都销毁上一 owner；前端不会在 tenant 间迁移草稿。 */
function clearErrorMapStateOnIdentityChange(previous, current) {
  if (errorMapIdentitySnapshot(previous) !== errorMapIdentitySnapshot(current)) {
    clearErrorMapSessionState();
  }
}

/** 返回 PAGE-070 POST 的实时 owner；DOM、身份或所选 request_id 任一消失都会让迟到回调失败关闭。 */
function currentRequestFeedbackOwner() {
  const snapshot = requestFeedbackMutationMemory.snapshot();
  const panelActive = Boolean(document.querySelector(".request-logs-panel .request-feedback-section"));
  return {
    identity: requestFeedbackIdentitySnapshot(authSession.context),
    navigation: panelActive ? "request-logs" : "",
    sourceRequestId: panelActive ? snapshot.sourceRequestId : "",
  };
}

/**
 * 绑定 PAGE-070 评分、采纳、标签、取消和提交交互。
 * 绑定前同时核对后端 active 导航与角色；伪造 DOM 不能让 viewer 构造反馈请求。
 */
function bindRequestFeedbackInteractions(consoleShellState) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.FEEDBACK_USE)
    || consoleShellState?.activeNavigationItemId !== "request-logs"
    || !requestFeedbackIdentitySnapshot(authSession.context)
  ) {
    return;
  }
  const form = document.querySelector("[data-request-feedback-form]");
  if (!form) {
    return;
  }

  for (const radio of form.querySelectorAll("input[name='rating']")) {
    radio.addEventListener("change", () => {
      if (radio.checked && requestFeedbackMutationMemory.selectRating(Number(radio.value))) {
        renderRequestFeedbackPanelFromMemory({ focusSelector: `input[name='rating'][value='${radio.value}']` });
      }
    });
  }
  for (const radio of form.querySelectorAll("input[name='accepted']")) {
    radio.addEventListener("change", () => {
      if (radio.checked && requestFeedbackMutationMemory.selectAccepted(radio.value === "true")) {
        renderRequestFeedbackPanelFromMemory({ focusSelector: `input[name='accepted'][value='${radio.value}']` });
      }
    });
  }
  for (const checkbox of form.querySelectorAll("input[name='tags']")) {
    checkbox.addEventListener("change", () => {
      if (!requestFeedbackMutationMemory.toggleTag(checkbox.value, checkbox.checked)) {
        // 标签超过五个或与已选标签互斥时恢复当前权威草稿，不能把无效选择留在 DOM 伪装成可提交。
        checkbox.checked = !checkbox.checked;
        checkbox.setCustomValidity(currentI18n.t(
          "feedback.validation.tags",
          {},
          "最多选择 5 个标签，且正反标签不能同时选择",
        ));
        checkbox.reportValidity();
        checkbox.setCustomValidity("");
        return;
      }
      renderRequestFeedbackPanelFromMemory({ focusSelector: `input[name='tags'][value='${checkbox.value}']` });
    });
  }
  form.querySelector("[data-request-feedback-cancel]")?.addEventListener("click", () => {
    if (requestFeedbackMutationMemory.cancelDraft()) {
      renderRequestFeedbackPanelFromMemory({ focusSelector: ".request-feedback-live" });
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) {
      return;
    }
    const token = authSession.token;
    // 身份 Token 缺失时必须先销毁草稿和详情 owner，禁止旧 DOM 继续构造 POST。
    if (!token) {
      // Token 消失说明 mutation owner 已结束，旧表单不得继续发送源调用 ID 或表单意图。
      clearRequestLogSessionState();
      renderLogin();
      return;
    }
    const acquired = requestFeedbackMutationMemory.acquireSubmit(currentRequestFeedbackOwner());
    if (acquired.state !== "ok" || acquired.reused) {
      return;
    }
    const ticket = acquired.ticket;
    renderRequestFeedbackPanelFromMemory();
    try {
      const result = await createRequestFeedback({
        controlApiUrl,
        token,
        requestId: ticket.sourceRequestId,
        rating: ticket.rating,
        accepted: ticket.accepted,
        tags: ticket.tags,
        operationRequestId: ticket.operationRequestId,
        signal: ticket.signal,
      });
      const owner = currentRequestFeedbackOwner();
      if (!requestFeedbackMutationMemory.isCurrent(ticket, owner)) {
        return;
      }
      if (!requestFeedbackMutationMemory.acceptSuccess(ticket, result, owner)) {
        requestFeedbackMutationMemory.acceptFailure(ticket, {
          status: 503,
          code: "feedback.unavailable",
          message: "反馈响应不完整",
          requestId: result?.request_id ?? ticket.operationRequestId,
        }, owner);
      }
      renderRequestFeedbackPanelFromMemory({ focusSelector: ".request-feedback-live" });
      // 成功事实先进入独立 mutation memory，再刷新日志 has_feedback 与服务端详情；刷新不会回溯取消已接纳 POST。
      if (requestFeedbackMutationMemory.snapshot().authoritativeFeedback) {
        await refresh();
      }
    } catch (error) {
      if (ticket.signal.aborted) {
        return;
      }
      const owner = currentRequestFeedbackOwner();
      // 迟到 401/409/5xx 先过完整 owner，避免上一租户错误退出新 Session 或覆盖新详情草稿。
      if (!requestFeedbackMutationMemory.isCurrent(ticket, owner)) {
        return;
      }
      // 只有仍属于当前 mutation owner 的 401 才能结束登录态，迟到响应已在上方失败关闭。
      if (error instanceof AuthApiError && error.status === 401) {
        clearRequestLogSessionState();
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return;
      }
      const safeError = error instanceof AuthApiError
        ? error
        : {
            code: "feedback.unavailable",
            message: "反馈请求未完成",
            requestId: ticket.operationRequestId,
          };
      requestFeedbackMutationMemory.acceptFailure(ticket, safeError, owner);
      renderRequestFeedbackPanelFromMemory({ focusSelector: ".request-feedback-live" });
    }
  });
}

/**
 * 用纯内存快照局部替换 PAGE-070 区域并重新绑定事件。
 * 局部重绘不会启动 RequestLog GET，也不会把草稿、标签或操作 ID 写入 storage/URL。
 */
function renderRequestFeedbackPanelFromMemory({ focusSelector = null } = {}) {
  const current = document.querySelector(".request-logs-panel .request-feedback-section");
  // DOM 已销毁或角色已收窄时不允许局部重建受限表单，后端权限仍是最终边界。
  if (!current || !hasPermission(authSession.context, PERMISSIONS.FEEDBACK_USE)) {
    return;
  }
  const html = renderRequestFeedbackSection(
    requestFeedbackMutationMemory.snapshot(),
    authSession.context,
    currentI18n,
  );
  if (!html) {
    current.remove();
    return;
  }
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  const replacement = template.content.firstElementChild;
  if (!replacement) {
    return;
  }
  current.replaceWith(replacement);
  bindRequestFeedbackInteractions({ activeNavigationItemId: "request-logs" });
  if (focusSelector) {
    replacement.querySelector(focusSelector)?.focus({ preventScroll: true });
  }
}

/** 绑定 PAGE-071 模型/时间筛选、显式刷新和分页命令。 */
function bindRequestFeedbackSummaryInteractions(consoleShellState) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.FEEDBACK_USE)
    || consoleShellState?.activeNavigationItemId !== "feedback-summary"
    || !requestFeedbackIdentitySnapshot(authSession.context)
  ) {
    return;
  }
  const form = document.querySelector("#feedback-summary-filter-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const fromTime = String(data.get("fromTime") ?? "").trim();
    const toTime = String(data.get("toTime") ?? "").trim();
    if (Boolean(fromTime) !== Boolean(toTime)) {
      const target = form.elements.fromTime;
      target?.setCustomValidity(currentI18n.t(
        "feedback.validation.time_pair",
        {},
        "开始时间和结束时间必须同时填写",
      ));
      target?.reportValidity();
      target?.setCustomValidity("");
      return;
    }
    requestFeedbackSummaryFilters = {
      ...requestFeedbackSummaryFilters,
      modelId: String(data.get("modelId") ?? "").trim(),
      fromTime,
      toTime,
      offset: 0,
    };
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-refresh")?.addEventListener("click", async () => {
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-reset")?.addEventListener("click", async () => {
    requestFeedbackSummaryFilters = { modelId: "", fromTime: "", toTime: "", limit: 20, offset: 0 };
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-prev")?.addEventListener("click", async () => {
    requestFeedbackSummaryFilters = {
      ...requestFeedbackSummaryFilters,
      offset: Math.max(0, requestFeedbackSummaryFilters.offset - requestFeedbackSummaryFilters.limit),
    };
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-next")?.addEventListener("click", async () => {
    requestFeedbackSummaryFilters = {
      ...requestFeedbackSummaryFilters,
      offset: requestFeedbackSummaryFilters.offset + requestFeedbackSummaryFilters.limit,
    };
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
}

/**
 * 绑定迁移页专属 tabs、显式 Environment/alias 选择和四个独立复制按钮。
 * 所有事件 owner 都在每轮 render 后重建；模块级选择状态保证 15 秒刷新不会静默改回第一项。
 */
function bindMigrationGuideInteractions() {
  const tabs = [...document.querySelectorAll("[data-migration-language-tab]")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      activateMigrationLanguage(tab.dataset.migrationLanguageTab, tabs);
    });
    tab.addEventListener("keydown", (event) => {
      const language = migrationLanguageForKey(tab.dataset.migrationLanguageTab, event.key);
      // 未识别按键保留浏览器默认行为，Tab 必须能够离开 tablist。
      if (!language) {
        return;
      }
      event.preventDefault();
      const target = tabs.find((candidate) => candidate.dataset.migrationLanguageTab === language);
      // DOM 关联被外部扩展破坏时保持当前 panel，不抛异常影响其他控制台交互。
      if (!target) {
        return;
      }
      activateMigrationLanguage(language, tabs);
      target.focus();
    });
  }

  const environmentSelect = document.querySelector("[data-migration-environment-select]");
  environmentSelect?.addEventListener("change", async () => {
    selectedMigrationEnvironmentId = optionalText(environmentSelect.value) ?? null;
    // Environment 改变后旧 alias 无论名称是否相同都必须清空，防止跨环境复用选择。
    selectedMigrationModelAlias = null;
    await refresh();
  });

  const aliasSelect = document.querySelector("[data-migration-alias-select]");
  aliasSelect?.addEventListener("change", async () => {
    selectedMigrationModelAlias = optionalText(aliasSelect.value) ?? null;
    await refresh();
  });

  for (const button of document.querySelectorAll("[data-migration-copy-target]")) {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.migrationCopyTarget);
      const feedback = button.closest("[data-migration-code-block]")?.querySelector(".migration-copy-feedback");
      const value = target?.textContent ?? "";
      // Clipboard 不可用或目标丢失时只写当前代码块反馈，绝不退化为包含密钥的隐藏输入框。
      if (!value || !navigator.clipboard?.writeText) {
        if (feedback) feedback.textContent = "当前浏览器不支持自动复制";
        return;
      }
      try {
        await navigator.clipboard.writeText(value);
        if (feedback) feedback.textContent = "已复制";
      } catch {
        // 浏览器权限拒绝属于可恢复的局部交互错误，其他三个复制按钮和指南阅读继续可用。
        if (feedback) feedback.textContent = "复制被浏览器拒绝，请检查剪贴板权限";
      }
    });
  }
}

/**
 * 绑定 PAGE-092 分段视图。
 * 按钮集合只来自已净化的后端 available_views；鼠标和方向键最终都进入同一个受控切换函数。
 */
function bindRoleDashboardInteractions(consoleShellState) {
  const tabs = [...document.querySelectorAll("[data-role-dashboard-view-option]")];
  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => {
      if (tab.getAttribute("aria-selected") === "true") return;
      void switchRoleDashboardView(tab.dataset.roleDashboardViewOption, consoleShellState);
    });
    tab.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (["ArrowRight", "ArrowDown"].includes(event.key)) nextIndex = (index + 1) % tabs.length;
      if (["ArrowLeft", "ArrowUp"].includes(event.key)) nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex == null || !tabs[nextIndex]) return;
      event.preventDefault();
      tabs[nextIndex].focus();
      if (tabs[nextIndex].getAttribute("aria-selected") !== "true") {
        void switchRoleDashboardView(
          tabs[nextIndex].dataset.roleDashboardViewOption,
          consoleShellState,
        );
      }
    });
  }
}

/**
 * 执行一次 PAGE-092 局部视图切换。
 *
 * 切换前推进全页 refresh owner、abort 旧 role-view 并立即渲染无卡片 loading；请求只携带上一份后端
 * 响应下发的 view code，不写 URL、Storage、用户偏好，也不重新加载其他控制台业务接口。
 */
async function switchRoleDashboardView(view, consoleShellState) {
  const selectedView = String(view ?? "");
  const availableViewCodes = roleDashboardPageState.availableViewCodes();
  if (!availableViewCodes.includes(selectedView)) return;
  const owner = currentRoleDashboardOwner(consoleShellState, selectedView);
  if (!owner) {
    clearRoleDashboardSessionState();
    return;
  }
  // 立即淘汰正在执行的全页刷新；即使旧 fetch 忽略 Abort，也会因 refreshSequence 和页面 ticket 双重失权。
  refreshSequence += 1;
  activeRefreshCount += 1;
  const ticket = roleDashboardPageState.begin({
    ...owner,
    availableViewCodes,
    source: "view_switch",
    transition: true,
  });
  if (!ticket) {
    activeRefreshCount -= 1;
    return;
  }
  renderRoleDashboardRootState(roleDashboardPageState.snapshot(), consoleShellState);
  try {
    const response = await getDashboardRoleView({
      controlApiUrl,
      token: ticket.token,
      view: ticket.requestedView,
      availableViewCodes: ticket.availableViewCodes,
      signal: ticket.controller.signal,
    });
    const state = roleDashboardPageState.accept(
      ticket,
      response,
      currentRoleDashboardOwner(consoleShellState, ticket.requestedView),
    );
    // 只有成功接纳并完成真实 DOM 投影后才允许继续记录角色首页访问事件。
    if (!state || !renderRoleDashboardRootState(state, consoleShellState)) return;
    // 事件必须晚于成功 GET、owner 复核和真实 DOM 投影；失败不撤销已经可读的首页。
    await ownRoleDashboardVisitAfterRender(consoleShellState);
  } catch (error) {
    if (!roleDashboardPageState.owns(
      ticket,
      currentRoleDashboardOwner(consoleShellState, ticket.requestedView),
    )) return;
    // Abort 是视图 owner 正常转移，不应展示错误或保留上一视图的任何卡片。
    if (error?.name === "AbortError") return;
    const state = roleDashboardPageState.reject(
      ticket,
      error,
      currentRoleDashboardOwner(consoleShellState, ticket.requestedView),
    );
    // 当前视图请求确认 401 时必须同步清理 Session 和受保护首页 DOM。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRoleDashboardSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 非认证错误只渲染状态机生成的安全快照，原始异常正文绝不进入页面。
    if (state) renderRoleDashboardRootState(state, consoleShellState);
  } finally {
    roleDashboardPageState.finish(ticket);
    activeRefreshCount -= 1;
  }
}

/**
 * 在成功角色首页已经出现在当前 overview DOM 后认领唯一访问事件。
 * 自动刷新不会重放同一 viewGeneration/view；用户显式切换会获得新代次并记录新视图。
 */
async function ownRoleDashboardVisitAfterRender(consoleShellState) {
  const root = document.querySelector("[data-role-dashboard-root]");
  const selectedView = root?.dataset.roleDashboardView ?? "";
  const configRevision = Number(root?.dataset.roleDashboardRevision);
  const owner = currentRoleDashboardOwner(consoleShellState, selectedView);
  if (!root || !owner || !selectedView || !Number.isSafeInteger(configRevision)) return;
  const ticket = roleDashboardPageState.claimRenderedEvent({
    ...owner,
    selectedView,
    configRevision,
  });
  if (!ticket) return;
  try {
    await recordConsolePageView({
      controlApiUrl,
      token: ticket.token,
      navigationItemId: "overview",
      targetSection: "overview",
      path: "/#overview",
      eventType: "dashboard.role_viewed",
      payload: {
        source: ticket.source,
        hash: "#overview",
        viewport_class: globalThis.window?.innerWidth < 720 ? "mobile" : "desktop",
        view_code: ticket.selectedView,
        config_revision: ticket.configRevision,
      },
    });
  } catch (error) {
    const current = currentRoleDashboardOwner(consoleShellState, ticket.selectedView);
    // 迟到事件错误不属于当前 Session 或视图，必须静默丢弃且不能影响新登录态。
    if (!roleDashboardPageState.ownsEvent(ticket, current)) return;
    // 事件 401 只有在完整 owner 仍一致时才改变认证边界并清空角色首页。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRoleDashboardSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 非认证遥测失败只写入安全告警节点；attempt 已冻结，定时刷新不会形成事件重试风暴。
    const warning = roleDashboardPageState.setEventWarning(error);
    const warningNode = document.querySelector("[data-role-dashboard-event-warning]");
    if (warningNode) {
      warningNode.textContent = `访问记录暂不可用：${warning.message} · request_id: ${warning.requestId}`;
    } else if (roleDashboardPageState.ownsEvent(ticket, current)) {
      // 首次失败时响应 HTML 尚无告警节点，局部重绘只使用已净化快照，不重新发送访问事件。
      renderRoleDashboardRootState(roleDashboardPageState.snapshot(), consoleShellState);
    }
  } finally {
    roleDashboardPageState.finishEvent(ticket);
  }
}

/**
 * 激活迁移指南语言并同步专属 tab/tabpanel 的 ARIA 状态，不触发网络请求或 SDK 下载状态变化。
 */
function activateMigrationLanguage(language, tabs) {
  activeMigrationLanguage = normalizeMigrationLanguage(language);
  for (const tab of tabs) {
    const selected = tab.dataset.migrationLanguageTab === activeMigrationLanguage;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const panel of document.querySelectorAll("[data-migration-language-panel]")) {
    panel.hidden = panel.dataset.migrationLanguagePanel !== activeMigrationLanguage;
  }
}

/**
 * 首屏渲染后的单一 owner 根据后端 active item 推进 visit 状态机，并处理 401 与非阻断遥测失败。
 */
async function ownMigrationGuideVisitAfterRender(consoleShellState) {
  try {
    await migrationGuideVisitTracker.afterRender({
      activeItemId: consoleShellState?.activeNavigationItemId,
      sessionKey: authSession.token,
      source: migrationGuideEntrySource,
    });
  } catch (error) {
    // 401 改变认证边界，必须清理会话；其他事件错误 fail-open 且本次 visit 不再重试。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    migrationGuideEventWarning = {
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
    const warning = document.querySelector("[data-migration-event-warning]");
    if (warning) {
      warning.hidden = false;
      warning.textContent = `访问记录暂不可用：${migrationGuideEventWarning.message} · request_id: ${migrationGuideEventWarning.requestId}`;
    }
  }
}

/**
 * 写入迁移指南专属访问事件，只发送后端允许的 source/hash/viewport_class 低风险字段。
 */
async function recordMigrationGuideVisit({ source }) {
  return recordConsolePageView({
    controlApiUrl,
    token: authSession.token,
    navigationItemId: "migration-guide",
    targetSection: "migration-guide",
    path: "/#migration-guide",
    eventType: "migration.guide_viewed",
    payload: {
      source: source === "direct_hash" ? "direct_hash" : "sidebar",
      hash: "#migration-guide",
      viewport_class: globalThis.window?.innerWidth < 720 ? "mobile" : "desktop",
    },
  });
}

/**
 * 绑定竞品对比页的 ARIA tabs；状态保存在模块内，15 秒刷新后仍停留在用户当前视图。
 */
function bindCompetitorInteractions() {
  const tabs = [...document.querySelectorAll("[data-competitor-tab]")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      activateCompetitorTab(tab.dataset.competitorTab, tabs);
    });
    tab.addEventListener("keydown", (event) => {
      const nextTab = competitorTabForKey(tab.dataset.competitorTab, event.key);
      // Tab 和未知按键保留浏览器默认行为，不能把键盘焦点困在标签列表中。
      if (!nextTab) {
        return;
      }
      event.preventDefault();
      const target = tabs.find((candidate) => candidate.dataset.competitorTab === nextTab);
      if (!target) {
        return;
      }
      activateCompetitorTab(nextTab, tabs);
      target.focus();
    });
  }
}

/**
 * 激活竞品页标签并同步 aria-selected、tabindex 和 tabpanel 可见状态。
 */
function activateCompetitorTab(tab, tabs) {
  activeCompetitorTab = normalizeCompetitorTab(tab);
  for (const candidate of tabs) {
    const selected = candidate.dataset.competitorTab === activeCompetitorTab;
    candidate.setAttribute("aria-selected", String(selected));
    candidate.tabIndex = selected ? 0 : -1;
  }
  for (const panel of document.querySelectorAll("[data-competitor-panel]")) {
    panel.hidden = panel.dataset.competitorPanel !== activeCompetitorTab;
  }
}

/**
 * 页面渲染后推进竞品页唯一访问状态机；401 清理认证，其他遥测失败只展示一次局部告警。
 */
async function ownCompetitorVisitAfterRender(consoleShellState) {
  try {
    await competitorVisitTracker.afterRender({
      activeItemId: consoleShellState?.activeNavigationItemId,
      sessionKey: authSession.token,
      source: competitorEntrySource,
    });
  } catch (error) {
    // 竞品事件 401 表示认证边界已经变化，必须清理旧会话，不能仅显示遥测告警。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    competitorEventWarning = {
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
    const warning = document.querySelector("[data-competitor-event-warning]");
    if (warning) {
      warning.hidden = false;
      warning.textContent = `访问记录暂不可用：${competitorEventWarning.message} · request_id: ${competitorEventWarning.requestId}`;
    }
  }
}

/**
 * 写入竞品帮助页专属事件；只发送后端白名单允许的来源、hash 和视口分类。
 */
async function recordCompetitorVisit({ source }) {
  return recordConsolePageView({
    controlApiUrl,
    token: authSession.token,
    navigationItemId: "competitor-comparison",
    targetSection: "competitor-comparison",
    path: "/#competitor-comparison",
    eventType: "help.competitor_viewed",
    payload: {
      source: source === "direct_hash" ? "direct_hash" : "sidebar",
      hash: "#competitor-comparison",
      viewport_class: globalThis.window?.innerWidth < 720 ? "mobile" : "desktop",
    },
  });
}

/**
 * 页面 shell 渲染后推进 PAGE-088 唯一访问状态机。
 * 只有双权限身份与后端 provider-health 导航同时成立才允许发送；状态 GET 成功与否不参与访问语义。
 */
async function ownProviderStatusVisitAfterRender(consoleShellState) {
  const identity = providerStatusIdentitySnapshot(authSession.context);
  const token = authSession.token;
  const navigationOwned = providerStatusNavigationOwned(consoleShellState);
  // 双权限身份、当前 Session 或后端导航任一缺失时只推进 inactive，不得发送状态访问事件。
  if (!identity || !token || !navigationOwned) {
    await providerStatusVisitTracker.afterRender({
      activeItemId: null,
      sessionKey: identity,
      source: providerStatusEntrySource,
    });
    return;
  }
  try {
    await providerStatusVisitTracker.afterRender({
      activeItemId: "provider-health",
      sessionKey: identity,
      source: providerStatusEntrySource,
    });
  } catch (error) {
    // 事件返回前身份或页面已变化时完全丢弃；迟到 401 不能清除之后建立的新会话。
    const currentNavigation = document.querySelector(".console-shell")?.dataset.activeNavigation;
    if (
      token !== authSession.token
      || identity !== providerStatusIdentitySnapshot(authSession.context)
      || currentNavigation !== "provider-health"
    ) {
      return;
    }
    // 只有仍属于当前身份和页面的事件 401 才能切断认证，迟到错误已经在上方被丢弃。
    // 只有仍属于当前完整 owner 的 401 才能销毁 Session；迟到 401 已在上方被丢弃。
    if (error instanceof AuthApiError && error.status === 401) {
      providerStatusPageState.clear();
      providerStatusVisitTracker.leave();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 遥测失败不阻断状态页读取；只将统一错误字段写入专属告警节点。
    providerStatusEventWarning = {
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
    const warning = document.querySelector("[data-provider-status-event-warning]");
    if (warning) {
      warning.hidden = false;
      warning.textContent = `访问记录暂不可用：${providerStatusEventWarning.message} · request_id: ${providerStatusEventWarning.requestId}`;
    }
  }
}

/** PAGE-088 访问事件只发送后端白名单允许的来源、固定 hash 与视口分类。 */
async function recordProviderStatusVisit({ source }) {
  return recordConsolePageView({
    controlApiUrl,
    token: authSession.token,
    navigationItemId: "provider-health",
    targetSection: "provider-health",
    path: "/#provider-health",
    eventType: "status.viewed",
    payload: {
      source: source === "direct_hash" ? "direct_hash" : "sidebar",
      hash: "#provider-health",
      viewport_class: globalThis.window?.innerWidth < 720 ? "mobile" : "desktop",
    },
  });
}

/** 创建 PAGE-088 唯一访问状态机，供主应用和单元测试复用同一 target 合同。 */
export function createProviderStatusVisitTracker(recordVisit) {
  return createUniquePageVisitTracker({ targetItemId: "provider-health", recordVisit });
}

/**
 * 创建迁移指南访问状态机；generation 标识进入代次，attempted/inFlight 同时阻止并发和定时刷新重放。
 * 非 401 是否展示告警由调用方决定，但任何失败都会保留 attempted=true，直到离开、登出或 Session 改变。
 */
export function createMigrationGuideVisitTracker(recordVisit) {
  return createUniquePageVisitTracker({ targetItemId: "migration-guide", recordVisit });
}

/**
 * 绑定 SDK 语言 tab 的点击和完整键盘交互。
 * 切换只更新当前 DOM 与模块级 activeSdkLanguage，不重新请求元数据，因此不会把一个语言的错误扩散到另一个面板。
 */
function bindSdkLanguageTabs() {
  const tabs = [...document.querySelectorAll("[data-sdk-language-tab]")];
  if (tabs.length === 0) {
    return;
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      // 点击激活当前按钮并同步 roving tabindex，后续 15 秒刷新会继续使用模块级语言状态。
      activateSdkLanguage(tab.dataset.sdkLanguageTab, tabs);
    });
    tab.addEventListener("keydown", (event) => {
      const targetLanguage = sdkLanguageForKey(tab.dataset.sdkLanguageTab, event.key);
      // 未识别按键保留浏览器默认行为，尤其不能拦截 Tab 离开 tablist。
      if (!targetLanguage) {
        return;
      }
      event.preventDefault();
      const targetTab = tabs.find((candidate) => candidate.dataset.sdkLanguageTab === targetLanguage);
      // DOM 若被扩展代码意外移除目标 tab，保持当前状态且不抛出会中断其他控制台事件的异常。
      if (!targetTab) {
        return;
      }
      // 元数据已经预加载，方向键、Home/End、Enter/Space 均可即时激活并聚焦目标语言。
      activateSdkLanguage(targetLanguage, tabs);
      targetTab.focus();
    });
  }
}

/**
 * 激活指定 SDK 语言，并同步 tab 与 tabpanel 的所有可访问性属性。
 * 未知语言统一回退 Python，避免 data 属性被篡改后出现无选中项或两个面板同时可见。
 */
function activateSdkLanguage(language, tabs) {
  activeSdkLanguage = normalizeSdkLanguage(language);
  for (const tab of tabs) {
    const selected = tab.dataset.sdkLanguageTab === activeSdkLanguage;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const panel of document.querySelectorAll("[data-sdk-language-panel]")) {
    panel.hidden = panel.dataset.sdkLanguagePanel !== activeSdkLanguage;
  }
}

// 下载函数白名单把 DOM 语言值映射到固定客户端函数，禁止通过 data 属性拼接任意下载 URL。
const SDK_EXAMPLE_DOWNLOADERS = Object.freeze({
  python: downloadPythonSdkExample,
  javascript: downloadJavascriptSdkExample,
});

/**
 * 绑定两种语言各自的受保护下载按钮。
 * 下载过程只在内存中持有 Blob；对象 URL 使用后立即释放，登录 Token 不进入 URL、DOM 或文件名。
 */
function bindSdkExampleDownloads() {
  for (const button of document.querySelectorAll("[data-download-sdk-language]")) {
    button.addEventListener("click", async () => {
      const language = normalizeSdkLanguage(button.dataset.downloadSdkLanguage);
      const downloader = SDK_EXAMPLE_DOWNLOADERS[language];
      const token = authSession.token;
      const feedback = button.closest("[data-sdk-language-panel]")?.querySelector("[data-sdk-feedback]")
        ?? document.querySelector(`[data-sdk-feedback="${language}"]`);
      // 没有登录 Token 时不能尝试裸下载，否则会绕开统一会话失效处理。
      if (!token) {
        renderLogin();
        return;
      }
      button.disabled = true;
      if (feedback) {
        feedback.textContent = "正在生成受控 ZIP 制品";
      }
      try {
        const result = await downloader({ controlApiUrl, token });
        // 浏览器缺少完整 Blob URL 生命周期能力时禁止降级到无认证链接，并保留本次 request_id。
        if (
          !globalThis.URL?.createObjectURL
          || !globalThis.URL?.revokeObjectURL
          || !globalThis.document?.createElement
        ) {
          if (feedback) {
            feedback.textContent = `浏览器不支持安全文件下载 · request_id: ${result.requestId}`;
          }
          return;
        }
        triggerSdkArchiveDownload(result);
        if (feedback) {
          feedback.textContent = `已下载 ${result.filename} · request_id: ${result.requestId}`;
        }
      } catch (error) {
        // 任一语言下载返回 401 时统一清理会话；403 与 5xx 只写入当前语言反馈。
        if (handleSdkUnauthorized(error)) {
          return;
        }
        if (feedback) {
          feedback.textContent = `${authErrorMessage(error)} · request_id: ${error?.requestId ?? "-"}`;
        }
      } finally {
        button.disabled = false;
      }
    });
  }
}

/**
 * 使用一次性对象 URL 触发浏览器下载，并在 click 成功或抛错后都立即回收 URL 与临时节点。
 * result.filename 已由 SDK 客户端完成 ASCII zip 白名单校验，此处不接受页面或用户输入覆盖。
 */
function triggerSdkArchiveDownload(result) {
  const objectUrl = URL.createObjectURL(result.blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = result.filename;
  link.hidden = true;
  document.body.append(link);
  try {
    link.click();
  } finally {
    // 即使浏览器扩展阻止 click，临时 DOM 和 Blob URL 也必须立即释放，避免内存泄漏或后续误用。
    link.remove();
    URL.revokeObjectURL(objectUrl);
  }
}

/**
 * 只在 PAGE-088 active 导航读取 Provider 状态。
 * PAGE-092 已由 /dashboard/role-view 在同一 tenant-first 快照中返回健康聚合，overview 禁止再后台预取旧摘要。
 */
async function loadProviderHealthState(consoleShellState) {
  const activeItemId = consoleShellState?.activeNavigationItemId;
  // 详情页必须走双权限状态接口，不能复用只含探测摘要的 Dashboard 数据形状。
  if (activeItemId === "provider-health") {
    // 进入详情页先淘汰可能仍在返回的 Dashboard 摘要，避免两套接口共享 controller 或状态形状。
    providerHealthRequestGeneration += 1;
    providerHealthController?.abort();
    providerHealthController = null;
    return loadProviderStatusState(consoleShellState);
  }
  providerStatusPageState.clear();
  // 其他页面不再发 /providers/health，避免角色首页绕过六卡片聚合与权限交集。
  providerHealthRequestGeneration += 1;
  providerHealthController?.abort();
  providerHealthController = null;
  return null;
}

/**
 * 读取 PAGE-088 状态快照。
 * ticket 冻结完整授权身份、后端导航、规范筛选、Token、generation 与 AbortController；所有结果共用同一接纳门禁。
 */
async function loadProviderStatusState(consoleShellState) {
  const owner = currentProviderStatusOwner(consoleShellState);
  if (!owner) {
    providerStatusPageState.clear();
    return null;
  }
  const ticket = providerStatusPageState.begin(owner);
  if (!ticket) return null;
  try {
    const state = await listProviderStatuses({
      controlApiUrl,
      token: ticket.token,
      filters: ticket.filters,
      signal: ticket.controller.signal,
    });
    // 状态响应必须仍属于相同身份、导航、筛选和 Token；任何漂移都完整丢弃。
    if (!providerStatusPageState.owns(ticket, currentProviderStatusOwner(consoleShellState))) {
      return null;
    }
    return {
      ...state,
      filters: ticket.filters,
      actionError: providerHealthActionError,
      probingProviderId: providerHealthProbingProviderId,
      eventWarning: providerStatusEventWarning,
    };
  } catch (error) {
    // 成功、401/403 与普通错误都必须先验证相同 owner；旧 Session 的迟到错误没有任何全局副作用。
    if (!providerStatusPageState.owns(ticket, currentProviderStatusOwner(consoleShellState))) {
      return null;
    }
    // Abort 只表示 owner 已被主动淘汰，不能进入页面错误态。
    if (error?.name === "AbortError") return null;
    // 仅当前 owner 的 401 可以销毁 Session，旧 ticket 的认证错误已在统一门禁前返回。
    if (error instanceof AuthApiError && error.status === 401) {
      providerStatusPageState.clear();
      providerStatusVisitTracker.leave();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403 代表实时授权已收窄，先销毁读取 owner；页面只保留统一错误，下一轮导航重校验会裁掉入口。
    if (error instanceof AuthApiError && error.status === 403) {
      providerStatusPageState.clear();
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      filters: ticket.filters,
      eventWarning: providerStatusEventWarning,
    };
  } finally {
    providerStatusPageState.finish(ticket);
  }
}

/**
 * 构造 PAGE-088 当前 owner。
 * 后端导航必须精确匹配 id/target，双权限身份必须完整；Token 只参与内存等值比较，不进入 DOM 或日志。
 */
function currentProviderStatusOwner(consoleShellState) {
  const identity = providerStatusIdentitySnapshot(authSession.context);
  // 缺少完整双权限身份、Token 或后端导航 owner 时失败关闭，禁止后台读取调用级故障。
  if (!identity || !authSession.token || !providerStatusNavigationOwned(consoleShellState)) {
    return null;
  }
  const filters = {
    ...providerStatusFilters,
    limit: providerStatusFilters.limit ?? 50,
    offset: providerStatusFilters.offset ?? 0,
  };
  return {
    identity,
    navigation: "provider-health",
    filters,
    filterKey: providerStatusFilterKey(filters),
    token: authSession.token,
  };
}

/** 后端已裁剪导航中只有精确 PAGE-088 owner 才允许读取或上报访问事件。 */
function providerStatusNavigationOwned(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "provider-health") return false;
  const hashState = readNavigationHashState();
  // 非法 hash 或指向其他页面的 hash 会立即使旧状态请求失权，不能等待下一轮导航响应。
  if (hashState.rejected || (hashState.value && hashState.value !== "provider-health")) return false;
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.some((item) => (
    item?.id === "provider-health"
    && item?.target_section === "provider-health"
    && item?.visible !== false
    && item?.disabled !== true
  ));
}

/**
 * 读取 PAGE-090 当前租户已激活的 Adapter 规范。
 * 成功、401/403 和普通错误在产生全局副作用或返回渲染状态前都必须通过同一个完整 owner 门禁。
 */
async function loadAdapterSpecsState(consoleShellState) {
  const owner = currentAdapterSpecOwner(consoleShellState);
  if (!owner) {
    adapterSpecPageState.clear();
    return null;
  }
  const ticket = adapterSpecPageState.begin(owner);
  if (!ticket) return null;
  try {
    const state = await listAdapterSpecs({
      controlApiUrl,
      token: ticket.token,
      filters: adapterSpecApiFilters(ticket.filters),
      signal: ticket.controller.signal,
    });
    if (!adapterSpecPageState.owns(ticket, currentAdapterSpecOwner(consoleShellState))) {
      return null;
    }
    return { ...state, filters: ticket.filters };
  } catch (error) {
    // Abort 是离页、筛选或身份变更的正常结果，不应制造错误 DOM 或覆盖下一页面。
    if (!adapterSpecPageState.owns(ticket, currentAdapterSpecOwner(consoleShellState))) {
      return null;
    }
    // Abort 属于离页或 owner 变化的预期结果，不能渲染错误态覆盖新页面。
    if (error?.name === "AbortError") return null;
    // 401 表示 Session 已失效，必须同时销毁页面 owner 和内存 Token，再回到登录界面。
    if (error instanceof AuthApiError && error.status === 401) {
      adapterSpecPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403 先销毁 owner；页面不会依据角色文本补权，下一轮后端导航刷新会裁掉入口。
    if (error instanceof AuthApiError && error.status === 403) {
      adapterSpecPageState.clear();
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      filters: ticket.filters,
    };
  } finally {
    adapterSpecPageState.finish(ticket);
  }
}

/** 构造 PAGE-090 当前 owner；Token 只在闭包内比较，绝不进入 DOM、日志或诊断快照。 */
function currentAdapterSpecOwner(consoleShellState) {
  const identity = adapterSpecIdentitySnapshot(authSession.context);
  if (!identity || !authSession.token || !adapterSpecNavigationOwned(consoleShellState)) {
    return null;
  }
  const filters = canonicalAdapterSpecFilters(adapterSpecFilters);
  return {
    identity,
    navigation: "adapter-specs",
    filters,
    filterKey: adapterSpecFilterKey(filters),
    token: authSession.token,
  };
}

/** 只有精确的后端导航 owner 和当前 hash 才允许 PAGE-090 发起目录请求。 */
function adapterSpecNavigationOwned(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "adapter-specs") return false;
  const hashState = readNavigationHashState();
  if (hashState.rejected || (hashState.value && hashState.value !== "adapter-specs")) return false;
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.some((item) => (
    item?.id === "adapter-specs"
    && item?.target_section === "adapter-specs"
    && item?.visible !== false
    && item?.disabled !== true
  ));
}

/**
 * 读取 PAGE-091 当前筛选的契约测试运行。
 * 成功、401、403 和普通错误都必须仍属于相同 tenant/Session/RBAC/navigation/filter/generation owner。
 */
async function loadAdapterContractRunsState(consoleShellState, refreshId) {
  const owner = currentAdapterContractRunOwner(consoleShellState, refreshId);
  if (!owner) {
    adapterContractRunPageState.clear();
    return null;
  }
  const operation = adapterContractRunPageState.beginListOperation(owner);
  if (!operation) return null;
  try {
    const result = await listAdapterContractRuns({
      controlApiUrl,
      token: operation.token,
      expectedTenantId: currentTenantId(),
      filters: adapterContractRunPageState.query(),
      signal: operation.controller.signal,
    });
    if (!adapterContractRunPageState.isListOperationCurrent(
      operation,
      currentAdapterContractRunOwner(consoleShellState, refreshId),
    )) return null;
    return adapterContractRunPageState.acceptPage(result);
  } catch (error) {
    // 主动取消和任何 owner 漂移都完整丢弃，尤其不能让旧 401 清除后来登录的 Session。
    if (
      !adapterContractRunPageState.isListOperationCurrent(
        operation,
        currentAdapterContractRunOwner(consoleShellState, refreshId),
      )
      || isAdapterContractRunAbortError(error)
    ) return null;
    // 只有仍属于当前完整 owner 的 401 才能销毁 Session；迟到 401 已被上方统一门禁丢弃。
    if (error instanceof AuthApiError && error.status === 401) {
      adapterContractRunPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403 由状态机立即销毁旧页面事实；随后导航仍按最新 RBAC 裁掉入口，前端不会按角色名补权。
    return adapterContractRunPageState.rejectList(error);
  } finally {
    adapterContractRunPageState.finishListOperation(operation);
  }
}

/**
 * 构造 PAGE-091 当前 owner。
 * Token 和 tenant 身份只用于内存比较/响应校验，不进入 URL、DOM、Storage 或错误消息。
 */
function currentAdapterContractRunOwner(consoleShellState, refreshId = refreshSequence) {
  if (refreshId !== refreshSequence) return null;
  const identity = adapterContractRunIdentitySnapshot(authSession.context);
  // 身份、Token 或后端导航任一缺失都禁止创建列表/详情 ticket，确保 viewer 零预取。
  if (!identity || !authSession.token || !adapterContractRunNavigationOwned(consoleShellState)) return null;
  const filters = adapterContractRunPageState.snapshot().filters;
  return {
    identity,
    navigation: "adapter-contract-runs",
    filterKey: adapterContractRunFilterKey(filters),
    token: authSession.token,
    refreshGeneration: refreshId,
  };
}

/** 后端授权导航、active item 和当前 hash 必须精确指向 PAGE-091 才允许任何 GET。 */
function adapterContractRunNavigationOwned(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "adapter-contract-runs") return false;
  const hashState = readNavigationHashState();
  if (hashState.rejected || (hashState.value && hashState.value !== "adapter-contract-runs")) return false;
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.some((item) => (
    item?.id === "adapter-contract-runs"
    && item?.target_section === "adapter-contract-runs"
    && item?.visible !== false
    && item?.disabled !== true
  ));
}

/** 从认证快照读取仅用于响应 owner 校验的 tenant_id；它从不作为 API query 或 path 参数。 */
function currentTenantId() {
  return String(authSession.context?.user?.tenant_id ?? authSession.context?.tenant?.tenant_id ?? "");
}

/**
 * 读取当前页选中运行的 case 摘要并局部重绘。
 * run ID 必须先由状态机确认存在于当前租户列表，DOM 注入的页外 ID 不会发出网络请求。
 */
async function loadAdapterContractRunDetail(adapterContractRunId, consoleShellState) {
  const owner = currentAdapterContractRunOwner(consoleShellState);
  const operation = adapterContractRunPageState.beginDetailOperation(owner, adapterContractRunId);
  if (!operation) return;
  renderAdapterContractRunPanel(consoleShellState);
  let shouldRender = false;
  try {
    const result = await getAdapterContractRun({
      controlApiUrl,
      token: operation.token,
      expectedTenantId: currentTenantId(),
      adapterContractRunId: operation.adapterContractRunId,
      signal: operation.controller.signal,
    });
    if (!adapterContractRunPageState.isDetailOperationCurrent(
      operation,
      currentAdapterContractRunOwner(consoleShellState),
    )) return;
    adapterContractRunPageState.acceptDetail(result);
    shouldRender = true;
  } catch (error) {
    if (
      !adapterContractRunPageState.isDetailOperationCurrent(
        operation,
        currentAdapterContractRunOwner(consoleShellState),
      )
      || isAdapterContractRunAbortError(error)
    ) return;
    // 详情迟到错误也先通过同一 ticket；只有当前 401 可以结束当前登录态。
    if (error instanceof AuthApiError && error.status === 401) {
      adapterContractRunPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    adapterContractRunPageState.rejectDetail(error);
    shouldRender = true;
  } finally {
    adapterContractRunPageState.finishDetailOperation(operation);
  }
  if (shouldRender && currentAdapterContractRunOwner(consoleShellState)) {
    renderAdapterContractRunPanel(consoleShellState);
  }
}

/** 局部替换 PAGE-091 内容并只重绑该页事件，避免重复绑定整个控制台导航和其他表单。 */
function renderAdapterContractRunPanel(consoleShellState) {
  if (!currentAdapterContractRunOwner(consoleShellState)) {
    adapterContractRunPageState.clear();
    return;
  }
  const panel = document.querySelector(".adapter-contract-runs-panel");
  if (!panel) return;
  panel.innerHTML = renderAdapterContractRuns(adapterContractRunPageState.snapshot(), authSession.context);
  bindAdapterContractRunInteractions(consoleShellState);
}

/**
 * 绑定 PAGE-091 的只读交互。
 * 事件层只改变内存筛选、闭包 cursor 或发起 GET；不存在 POST、真实探测 URL、Endpoint 或凭据输入。
 */
function bindAdapterContractRunInteractions(consoleShellState) {
  for (const button of document.querySelectorAll("[data-adapter-contract-suite]")) {
    button.addEventListener("click", async () => {
      const current = adapterContractRunPageState.snapshot().filters;
      adapterContractRunPageState.setFilters({
        ...current,
        suite_kind: String(button.dataset.adapterContractSuite ?? ""),
        // 分段切换清空旧 mode，避免把 mock 条件错误沿用到 probe，或反向沿用 gateway_probe。
        mode: "",
      });
      await refresh();
    });
  }
  const form = document.querySelector("#adapter-contract-filters");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const current = adapterContractRunPageState.snapshot().filters;
    adapterContractRunPageState.setFilters({
      ...current,
      status: data.get("status"),
      mode: data.get("mode"),
    });
    await refresh();
  });
  document.querySelector("[data-adapter-contract-reset]")?.addEventListener("click", async (event) => {
    event.preventDefault();
    const current = adapterContractRunPageState.snapshot().filters;
    adapterContractRunPageState.setFilters({
      suite_kind: current.suite_kind,
      status: "",
      mode: "",
      limit: 50,
    });
    await refresh();
  });
  document.querySelector("[data-adapter-contract-refresh]")?.addEventListener("click", async () => {
    // 显式刷新仍只执行 GET，并重新走 /auth/me、后端导航和完整 owner 校验。
    await refresh();
  });
  document.querySelector("[data-adapter-contract-previous]")?.addEventListener("click", async () => {
    if (adapterContractRunPageState.previousPage()) await refresh();
  });
  document.querySelector("[data-adapter-contract-next]")?.addEventListener("click", async () => {
    if (adapterContractRunPageState.nextPage()) await refresh();
  });
  for (const button of document.querySelectorAll("[data-adapter-contract-detail]")) {
    button.addEventListener("click", async () => {
      const runId = String(button.dataset.adapterContractDetail ?? "");
      if (adapterContractRunPageState.snapshot().selected_run_id === runId) {
        adapterContractRunPageState.closeDetail();
        renderAdapterContractRunPanel(consoleShellState);
        return;
      }
      await loadAdapterContractRunDetail(runId, consoleShellState);
    });
  }
  document.querySelector("[data-adapter-contract-close-detail]")?.addEventListener("click", () => {
    adapterContractRunPageState.closeDetail();
    renderAdapterContractRunPanel(consoleShellState);
  });
}

/** 触发单行手工探测；POST 开始前作废在途 GET，完成后统一刷新最新快照。 */
async function runProviderProbe(providerId) {
  const token = authSession.token;
  if (
    !providerId
    || !token
    || !hasPermission(authSession.context, PERMISSIONS.PROVIDER_HEALTH_PROBE)
  ) {
    return;
  }
  providerHealthProbingProviderId = providerId;
  providerHealthActionError = null;
  // 探测写开始前先淘汰状态 GET，旧指标不能在 POST 之后覆盖新探测结果。
  providerStatusPageState.clear();
  providerHealthRequestGeneration += 1;
  providerHealthController?.abort();
  const controller = new AbortController();
  providerHealthController = controller;
  try {
    await probeProvider({ controlApiUrl, token, providerId, signal: controller.signal });
  } catch (error) {
    // 只有真实 API 失败才进入行级错误反馈，主动取消不污染运维判断。
    if (error?.name !== "AbortError") {
      providerHealthActionError = {
        message: authErrorMessage(error),
        requestId: error?.requestId ?? "-",
      };
    }
  } finally {
    // POST 完成后仅在同一登录态刷新，避免旧账号请求覆盖新账号页面。
    if (token === authSession.token) {
      providerHealthProbingProviderId = null;
      await refresh();
    }
  }
}

/**
 * 读取当前租户模型价格时间线，并保留筛选、反馈和统一错误状态。
 */
async function loadModelPricesState() {
  // 缺少 model_price.read 时不主动读取财务价格，真实权限仍由后端再次校验。
  if (!hasPermission(authSession.context, PERMISSIONS.MODEL_PRICE_READ)) {
    return null;
  }
  try {
    const state = await listModelPrices({
      controlApiUrl,
      token: authSession.token,
      modelId: modelPriceFilters.modelId,
      effectiveStatus: modelPriceFilters.effectiveStatus,
      limit: modelPriceFilters.limit ?? 100,
      offset: modelPriceFilters.offset ?? 0,
    });
    return {
      ...state,
      filters: modelPriceFilters,
      actionError: modelPriceActionError,
      feedback: modelPriceFeedback,
    };
  } catch (error) {
    // 认证失效必须清除本地会话，避免继续用过期 Token 读取价格数据。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: error?.message ?? "模型价格读取失败",
      requestId: error?.requestId,
      filters: modelPriceFilters,
      actionError: modelPriceActionError,
      feedback: modelPriceFeedback,
    };
  }
}

/**
 * 执行 控制台主应用 的 `loadModelAliasesState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadEnvironmentState() {
  /** 读取当前租户全部 Environment，禁用项仍需展示在历史配置和设置页。 */
  // 缺少 Environment 读权限时不在每轮刷新制造可避免的 403；后端守卫仍是最终边界。
  if (!hasPermission(authSession.context, PERMISSIONS.ENVIRONMENT_READ)) {
    return null;
  }
  try {
    const result = await listEnvironments({
      controlApiUrl,
      token: authSession.token,
      status: "all",
    });
    return {
      ...result,
      actionError: environmentActionError,
      feedback: environmentFeedback,
    };
  } catch (error) {
    // 401 改变认证边界，必须回到登录页；403/业务错误保留为面板错误态。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return { state: "error", message: error.message, requestId: error.requestId };
    }
    return {
      state: "error",
      message: error?.message ?? "环境列表读取失败",
      requestId: error?.requestId,
      actionError: environmentActionError,
      feedback: environmentFeedback,
    };
  }
}


/**
 * 读取当前租户模型别名，并保留环境绑定字段供控制台按环境创建和展示。
 */
async function loadModelAliasesState() {
  // 模型别名只对具有 routing.read 的身份开放，其他身份不主动发起读取请求。
  if (!hasPermission(authSession.context, PERMISSIONS.ROUTING_READ)) {
    return null;
  }
  try {
    const state = await listModelAliases({ controlApiUrl, token: authSession.token });
    return {
      ...state,
      actionError: modelAliasActionError,
      feedback: modelAliasFeedback,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}


/**
 * 读取 Project/Application 列表，供归属管理页、Key 绑定和预算 scope 选择复用。
 * 管理员/开发者才会调用控制面 API；viewer 返回 null 只是界面优化，真实权限仍由后端项目接口校验。
 */
async function loadProjectState() {
  if (!hasPermission(authSession.context, PERMISSIONS.PROJECT_READ)) {
    return null;
  }
  try {
    const [projects, applications] = await Promise.all([
      listProjects({ controlApiUrl, token: authSession.token, status: "all" }),
      listApplications({ controlApiUrl, token: authSession.token, status: "all" }),
    ]);
    return {
      state: "ok",
      projects: projects.projects ?? [],
      applications: applications.applications ?? [],
      actionError: projectActionError,
      feedback: projectFeedback,
    };
  } catch (error) {
    // 认证失败必须立即清理登录态，避免项目归属页继续驱动后续 Key 绑定请求。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      actionError: projectActionError,
    };
  }
}

/**
 * 读取用量概览和用量明细。
 * 管理员/开发者才会调用控制面 API；viewer 返回 null 只是界面优化，真实权限仍由后端 /usage 校验。
 */
async function loadUsageState() {
  // 用量账本属于成本治理数据，缺少 billing.usage.read 时不触发请求，后端仍会二次校验。
  if (!hasPermission(authSession.context, PERMISSIONS.BILLING_USAGE_READ)) {
    return null;
  }
  try {
    const [summary, records] = await Promise.all([
      getUsageSummary({
        controlApiUrl,
        token: authSession.token,
        environmentId: usageFilters.environmentId,
        startDate: usageFilters.startDate,
        endDate: usageFilters.endDate,
        modelId: usageFilters.modelId,
        virtualKeyId: usageFilters.virtualKeyId,
        projectId: usageFilters.projectId,
        applicationId: usageFilters.applicationId,
        providerId: usageFilters.providerId,
        estimated: usageFilters.estimated,
        requestId: usageFilters.requestId,
      }),
      listUsageRecords({
        controlApiUrl,
        token: authSession.token,
        environmentId: usageFilters.environmentId,
        startDate: usageFilters.startDate,
        endDate: usageFilters.endDate,
        modelId: usageFilters.modelId,
        virtualKeyId: usageFilters.virtualKeyId,
        projectId: usageFilters.projectId,
        applicationId: usageFilters.applicationId,
        providerId: usageFilters.providerId,
        estimated: usageFilters.estimated,
        requestId: usageFilters.requestId,
        limit: usageFilters.limit ?? 20,
        offset: usageFilters.offset ?? 0,
      }),
    ]);
    return {
      state: "ok",
      summary,
      records,
      filters: usageFilters,
      actionError: usageActionError,
      exportState: usageExportState,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 只在 admin 且后端导航确认当前项为 finops-ledger 时读取正式账本。
 *
 * hash 只能作为“仍在当前页”的附加条件，不能代替后端角色化导航；否则手写 hash
 * 会在未授权页面后台预取财务数据。opaque cursor 只由 FinOps 页面内存提供。
 */
async function loadFinOpsState(consoleShellState, refreshId) {
  const hashNavigation = readActiveNavigationHash();
  if (
    refreshId !== refreshSequence
    || !hasPermission(authSession.context, PERMISSIONS.BILLING_FINOPS_READ)
    || consoleShellState?.activeNavigationItemId !== "finops-ledger"
    || (hashNavigation && hashNavigation !== "finops-ledger")
  ) {
    return null;
  }
  try {
    const state = await listFinOpsLedger({
      controlApiUrl,
      token: authSession.token,
      ...finOpsPageState.query(),
    });
    // 列表响应到达时再次确认刷新 owner，旧 Session 或离页响应不能进入页面内存。
    const responseHashNavigation = readActiveNavigationHash();
    if (
      refreshId !== refreshSequence
      || !hasPermission(authSession.context, PERMISSIONS.BILLING_FINOPS_READ)
      || (responseHashNavigation && responseHashNavigation !== "finops-ledger")
    ) {
      return null;
    }
    return finOpsPageState.acceptPage(state);
  } catch (error) {
    if (refreshId !== refreshSequence) {
      return null;
    }
    // 401 已改变认证边界，必须先销毁 cursor、reason 和幂等键，再清除登录态。
    if (error instanceof AuthApiError && error.status === 401) {
      finOpsPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    const memory = finOpsPageState.snapshot();
    return {
      state: "error",
      code: error?.code ?? "finops.ledger_unavailable",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      filters: memory.filters,
    };
  }
}
/**
 * 只在 admin 且后端导航确认 PAGE-077/078 激活时读取成本异常。
 *
 * GET 使用独立 AbortController 和刷新代次；手写 hash、前端角色文本或旧 Session 均不能
 * 触发后台预取。opaque cursor 只从页面闭包读取，不进入地址栏、DOM 或持久存储。
 */
async function loadCostAnomaliesState(consoleShellState, refreshId) {
  const owner = currentCostAnomalyListOwner(consoleShellState, refreshId);
  if (!owner) {
    return null;
  }
  const operation = costAnomalyPageState.beginListOperation(owner);
  if (!operation) {
    return null;
  }

  try {
    const state = await listCostAnomalies({
      controlApiUrl,
      token: authSession.token,
      ...costAnomalyPageState.query(),
      operationRequestId: createCostAnomalyRequestId("list"),
      operationTraceId: createCostAnomalyTraceId(),
      signal: operation.controller.signal,
    });
    const currentOwner = currentCostAnomalyListOwner(consoleShellState, refreshId);
    if (!costAnomalyPageState.isListOperationCurrent(operation, currentOwner)) {
      return null;
    }
    return costAnomalyPageState.acceptPage(state);
  } catch (error) {
    const currentOwner = currentCostAnomalyListOwner(consoleShellState, refreshId);
    if (
      !costAnomalyPageState.isListOperationCurrent(operation, currentOwner)
      || isCostAnomalyAbortError(error)
    ) {
      return null;
    }
    // 401 改变认证边界，必须先清空上一租户的 cursor、详情与动作草稿再回到登录页。
    if (error instanceof AuthApiError && error.status === 401) {
      costAnomalyPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    const memory = costAnomalyPageState.snapshot();
    return {
      state: "error",
      code: error?.code ?? "cost_anomaly.unavailable",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      filters: memory.filters,
    };
  } finally {
    costAnomalyPageState.finishListOperation(operation);
  }
}

/**
 * 构造成本异常 GET owner；refreshGeneration 用于淘汰较早全页刷新，
 * action owner 则额外确保当前身份、后端 active 导航和 hash 仍然一致。
 */
function currentCostAnomalyListOwner(consoleShellState, refreshId) {
  if (refreshId !== refreshSequence) {
    return null;
  }
  const actionOwner = currentCostAnomalyActionOwner(consoleShellState);
  return actionOwner ? { ...actionOwner, refreshGeneration: refreshId } : null;
}

/**
 * 只在后端授权导航与实时 session_risk.read 同时成立时读取 PAGE-098。
 * GET owner 绑定完整 Session、权限快照和 refresh 代次，其他页面不会预取风险记录。
 */
async function loadSessionRisksState(consoleShellState, refreshId) {
  const owner = currentSessionRiskReadOwner(consoleShellState);
  // GET 同时绑定实时 owner 与全页 refresh 代次，其他导航不会预取或保留风险事实。
  if (!owner || refreshId !== refreshSequence || !sessionRiskPageState.enter(owner)) return null;
  const operation = sessionRiskPageState.beginListOperation(owner, refreshId);
  if (!operation) return null;
  try {
    const result = await listSessionRisks({
      controlApiUrl,
      token: authSession.token,
      ...sessionRiskPageState.query(),
      operationRequestId: createSessionRiskRequestId("list"),
      operationTraceId: createSessionRiskTraceId(),
      signal: operation.controller.signal,
    });
    const currentOwner = currentSessionRiskReadOwner(consoleShellState);
    // 只接受同 owner、同 refresh 代次的列表；迟到页不会覆盖新租户快照。
    if (!sessionRiskPageState.acceptPage(operation, result, currentOwner, refreshId)) return null;
    // snapshot 已剔除 cursor、owner 指纹和幂等键，可安全交给 renderer。
    return sessionRiskPageState.snapshot();
  } catch (error) {
    const currentOwner = currentSessionRiskReadOwner(consoleShellState);
    if (
      !sessionRiskPageState.isListOperationCurrent(operation, currentOwner, refreshId)
      || isSessionRiskAbortError(error)
    ) return null;
    // 401 表示认证失效，旧租户风险 DTO 必须先于登录页渲染被清空。
    if (error instanceof AuthApiError && error.status === 401) {
      sessionRiskPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    // 403 表示实时撤权，保持 Session 但立即销毁已读取的风险事实。
    if (error instanceof AuthApiError && error.status === 403) {
      // GET 撤权必须立即销毁旧风险事实，并安排下一轮重新读取 /auth/me 与后端导航。
      sessionRiskPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    sessionRiskPageState.rejectPage(operation, error, currentOwner, refreshId);
    // 普通失败只返回固定错误快照，响应正文和底层异常不会进入 DOM。
    return sessionRiskPageState.snapshot();
  } finally {
    sessionRiskPageState.finishListOperation(operation);
  }
}

/**
 * 只在后端授权 files 导航与实时 file.read 同时成立时读取文件记录。
 * GET owner 绑定完整 Session、权限快照和 refresh 代次，其他页面不会预取文件名或处理摘要。
 */
async function loadFilesState(consoleShellState, refreshId) {
  const owner = currentFileReadOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !filePageState.enter(owner)) return null;
  const operation = filePageState.beginListOperation(owner, refreshId);
  if (!operation) return null;
  try {
    const result = await listFiles({
      controlApiUrl,
      token: authSession.token,
      ...filePageState.query(),
      signal: operation.controller.signal,
    });
    const currentOwner = currentFileReadOwner(consoleShellState);
    if (!filePageState.acceptPage(operation, result, currentOwner, refreshId)) return null;
    return filePageState.snapshot();
  } catch (error) {
    const currentOwner = currentFileReadOwner(consoleShellState);
    if (
      !filePageState.isListOperationCurrent(operation, currentOwner, refreshId)
      || isFileAbortError(error)
    ) return null;
    // 列表 401 与上传一致地销毁文件名、cursor 和认证状态，避免下一登录复用旧事实。
    if (error instanceof AuthApiError && error.status === 401) {
      filePageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    if (
      error instanceof AuthApiError
      && error.status === 403
      && error.code === "auth.permission_denied"
    ) {
      // GET 撤权先销毁文件名与 cursor，再异步重读 /auth/me 和后端导航，避免递归刷新当前调用栈。
      filePageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    filePageState.rejectPage(operation, error, currentOwner, refreshId);
    return filePageState.snapshot();
  } finally {
    filePageState.finishListOperation(operation);
  }
}
/**
 * 清除预算页会话内快照。
 *
 * 登出、401 或租户身份变化后不得保留上一租户的规则存在性和 Key 选择，否则可能把新建误判为更新。
 */
function clearBudgetSessionState() {
  budgetRulesSnapshot = [];
  selectedBudgetVirtualKeyId = "";
  budgetActionError = null;
  budgetFeedback = "";
  budgetMutationLifecycle.invalidate();
}

/**
 * 身份边界变化时清除预算页面全部会话内事实。
 * tenant、用户、角色或 Session 任一变化都不能沿用旧规则快照、Key 选择或 Mutation owner。
 */
function clearBudgetStateOnIdentityChange(previousContext, currentContext) {
  if (budgetIdentitySnapshot(previousContext) !== budgetIdentitySnapshot(currentContext)) {
    clearBudgetSessionState();
  }
}

/**
 * 返回当前 Budget Mutation owner，不包含 Token 或任何预算业务数据。
 * 实际渲染导航用于阻止离页后的迟到回调触发新页面刷新。
 */
function currentBudgetMutationOwner() {
  return {
    identity: budgetIdentitySnapshot(authSession.context),
    navigation: currentRenderedNavigation(),
    refreshGeneration: refreshSequence,
  };
}

/**
 * 核对预算写回仍属于当前 tenant/user/navigation/refresh generation。
 * 后端已提交的旧请求可以完成，但其响应不得修改新会话的快照或反馈。
 */
function isCurrentBudgetMutation(operation) {
  return budgetMutationLifecycle.isCurrent(operation, currentBudgetMutationOwner());
}

/**
 * 判断当前 Environment/scope 是否已有预算规则。
 *
 * 该判断只决定控制台使用 POST 还是兼容 PATCH；并发冲突和最终归属仍由后端 UNIQUE、
 * tenant_id 与权威资源绑定校验。
 */
function budgetRuleAlreadyExists(environmentId, scopeType, scopeId) {
  const effectiveScopeId = scopeType === "tenant"
    ? authSession.context?.user?.tenant_id
    : scopeId;
  return budgetRulesSnapshot.some((rule) => (
    rule.environment_id === environmentId
    && rule.scope_type === scopeType
    && rule.scope_id === effectiveScopeId
  ));
}

/**
 * 读取预算规则和选中 Virtual Key 的可信预算作用链。
 * 管理员/开发者才会调用控制面 API；只有 tenant/user/refresh owner 未变化时才接纳响应。
 */
async function loadBudgetState(virtualKeysState, consoleShellState, refreshId) {
  if (!hasPermission(authSession.context, PERMISSIONS.BILLING_BUDGET_READ)) {
    return null;
  }
  const identity = budgetIdentitySnapshot(authSession.context);
  const refreshOwner = Object.freeze({ identity, refreshGeneration: refreshId });
  if (!isBudgetRefreshOwnerCurrent(refreshOwner, {
    identity: budgetIdentitySnapshot(authSession.context),
    refreshGeneration: refreshSequence,
  })) {
    return null;
  }
  // Token 只冻结在请求闭包中，不进入 owner、DOM、错误或日志。
  const token = authSession.token;
  const virtualKeyIds = (virtualKeysState?.virtual_keys ?? []).map((item) => item.virtual_key_id);
  const isBudgetPage = consoleShellState?.activeNavigationItemId === "budgets";
  if (isBudgetPage && !virtualKeyIds.includes(selectedBudgetVirtualKeyId)) {
    selectedBudgetVirtualKeyId = virtualKeyIds[0] ?? "";
  }
  const scopeChainKeyId = isBudgetPage ? selectedBudgetVirtualKeyId : "";
  try {
    const [rules, status] = await Promise.all([
      listBudgets({
        controlApiUrl,
        token,
      }),
      getBudgetStatus({
        controlApiUrl,
        token,
        virtualKeyId: scopeChainKeyId || undefined,
      }),
    ]);
    // GET 完成后必须再次比较完整身份和主刷新代次，最终 DOM 门禁不能保护提前写入的模块状态。
    if (!isBudgetRefreshOwnerCurrent(refreshOwner, {
      identity: budgetIdentitySnapshot(authSession.context),
      refreshGeneration: refreshSequence,
    })) {
      return null;
    }
    // 只有 PAGE-076 需要规则存在性快照；其他页面只消费只读状态摘要。
    budgetRulesSnapshot = isBudgetPage ? (rules.budget_rules ?? []) : [];
    return {
      state: "ok",
      rules,
      scanScopeFilter: dlpScanScopeFilter,
      status,
      selectedVirtualKeyId: selectedBudgetVirtualKeyId,
      actionError: budgetActionError,
      feedback: budgetFeedback,
    };
  } catch (error) {
    // 迟到异常不能清空新身份快照、跳转登录页、覆盖反馈或触发额外刷新。
    if (!isBudgetRefreshOwnerCurrent(refreshOwner, {
      identity: budgetIdentitySnapshot(authSession.context),
      refreshGeneration: refreshSequence,
    })) {
      return null;
    }
    budgetRulesSnapshot = [];
    // 当前 owner 收到 401 时必须先清预算会话，再销毁认证状态，避免旧快照进入下一次登录。
    if (error instanceof AuthApiError && error.status === 401) {
      clearBudgetSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
      // POST create-only 的 409 是独立操作证据；刷新失败不能覆盖其 message/request_id。
      actionError: budgetActionError,
      feedback: budgetFeedback,
    };
  }
}
/**
 * 读取当前租户 DLP 规则。
 * 只有 admin 才会调用控制面 API；viewer/developer 返回 null 只是界面优化，真实权限仍由后端 /dlp/rules 校验。
 */
async function loadDlpState() {
  if (!hasPermission(authSession.context, PERMISSIONS.DLP_READ)) {
    return null;
  }
  try {
    const rules = await listDlpRules({
      controlApiUrl,
      token: authSession.token,
      status: "all",
    });
    return {
      state: "ok",
      rules,
      actionError: dlpActionError,
      feedback: dlpFeedback,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 读取当前租户 Webhook Endpoint 和最近发送记录。
 * 管理员/开发者才会调用控制面 API；viewer 返回 null 只是界面优化，真实权限仍由后端 /webhooks 校验。
 */
async function loadWebhookState() {
  if (!hasPermission(authSession.context, PERMISSIONS.WEBHOOK_MANAGE)) {
    return null;
  }
  try {
    const webhooks = await listWebhooks({
      controlApiUrl,
      token: authSession.token,
      status: "all",
      includeDeliveries: false,
    });
    const endpointIds = (webhooks.webhooks ?? []).map((endpoint) => endpoint.webhook_endpoint_id);
    if (!endpointIds.includes(webhookSelectedEndpointId)) {
      webhookSelectedEndpointId = endpointIds[0] ?? null;
    }
    const deliveryDetails = webhookSelectedEndpointId
      ? await listWebhookDeliveries({
          controlApiUrl,
          token: authSession.token,
          webhookEndpointId: webhookSelectedEndpointId,
          eventType: webhookDeliveryFilters.eventType,
          status: webhookDeliveryFilters.status,
          limit: 100,
        })
      : null;
    return {
      state: "ok",
      webhooks,
      actionError: webhookActionError,
      feedback: webhookFeedback,
      created: webhookCreatedSecret,
      lastTestResult: webhookLastTestResult,
      lastReplayResult: webhookLastReplayResult,
      selectedEndpointId: webhookSelectedEndpointId,
      deliveryDetails,
      deliveryFilters: webhookDeliveryFilters,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}

/**
 * 仅在后端确认 PAGE-083 active 且身份为 admin/developer 时读取死信安全摘要。
 * 其他页面、viewer、伪造 hash、旧刷新或不完整 Session 都在创建 fetch 前失败关闭。
 */
async function loadWebhookDeadLettersState(consoleShellState, refreshId) {
  const owner = currentWebhookDeadLetterListOwner(consoleShellState, refreshId);
  if (!owner) {
    return null;
  }
  const operation = webhookDeadLetterPageState.beginListOperation(owner);
  if (!operation) {
    return null;
  }
  const operationIds = createWebhookDeadLetterOperationIds("list");
  try {
    const result = await listWebhookDeadLetters({
      controlApiUrl,
      token: authSession.token,
      ...webhookDeadLetterPageState.query(),
      signal: operation.controller.signal,
      operationRequestId: operationIds.requestId,
      operationTraceId: operationIds.traceId,
    });
    const currentOwner = currentWebhookDeadLetterListOwner(consoleShellState, refreshId);
    if (!webhookDeadLetterPageState.isListOperationCurrent(operation, currentOwner)) {
      return null;
    }
    return webhookDeadLetterPageState.acceptPage(result);
  } catch (error) {
    const currentOwner = currentWebhookDeadLetterListOwner(consoleShellState, refreshId);
    if (
      !webhookDeadLetterPageState.isListOperationCurrent(operation, currentOwner)
      || isWebhookDeadLetterAbortError(error)
    ) {
      return null;
    }
    // 只有仍持有完整 identity/navigation/generation owner 的 401 才能结束当前 Session。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return webhookDeadLetterPageState.rejectList(error);
  } finally {
    webhookDeadLetterPageState.finishListOperation(operation);
  }
}

/** 构造 PAGE-083 列表 owner；refreshId 必须仍等于全局代次，防止旧全页刷新发出新 GET。 */
function currentWebhookDeadLetterListOwner(consoleShellState, refreshId = refreshSequence) {
  if (refreshId !== refreshSequence) {
    return null;
  }
  const actionOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
  return actionOwner ? { ...actionOwner, refreshGeneration: refreshId } : null;
}

/**
 * 构造 PAGE-083 写操作 owner。
 * active item、后端导航定义和完整身份必须同时成立，不能只信任 location.hash 或可篡改 DOM。
 */
function currentWebhookDeadLetterActionOwner(consoleShellState) {
  const navigationItems = consoleShellState?.navigation?.navigation_items;
  const authorized = Array.isArray(navigationItems)
    && navigationItems.some((item) => (
      item?.id === "webhook-dead-letters"
      && item?.target_section === "webhook-dead-letters"
    ));
  const identity = webhookDeadLetterIdentitySnapshot(authSession.context);
  // 后端导航授权、完整身份和当前页面任一不成立时都失败关闭，禁止创建网络 owner。
  if (!authorized || !identity || consoleShellState?.activeNavigationItemId !== "webhook-dead-letters") {
    return null;
  }
  return { identity, navigation: "webhook-dead-letters" };
}

/** 绑定 PAGE-083 交互；节点不存在时绑定层静默返回，因此其他导航不会产生死信副作用。 */
function bindWebhookDeadLetterPageInteractions(consoleShellState) {
  bindWebhookDeadLetterInteractions({
    async onFilter(filters) {
      webhookDeadLetterPageState.setFilters(filters);
      await refreshWebhookDeadLetterPanel(consoleShellState);
    },
    async onRefresh() {
      webhookDeadLetterPageState.cancelPoll();
      await refreshWebhookDeadLetterPanel(consoleShellState);
    },
    async onPreviousPage() {
      if (webhookDeadLetterPageState.previousPage()) {
        await refreshWebhookDeadLetterPanel(consoleShellState);
      }
    },
    async onNextPage() {
      if (webhookDeadLetterPageState.nextPage()) {
        await refreshWebhookDeadLetterPanel(consoleShellState);
      }
    },
    async onReplay(deadLetterEventId) {
      await submitWebhookDeadLetterReplay(deadLetterEventId, consoleShellState);
    },
    async onOpenArchive(deadLetterEventId) {
      if (webhookDeadLetterPageState.openArchive(deadLetterEventId)) {
        renderWebhookDeadLetterPanel(consoleShellState);
        document.querySelector("#webhook-dead-letter-archive-dialog")?.focus();
      }
    },
    async onCloseArchive() {
      const closedId = webhookDeadLetterPageState.closeArchive();
      renderWebhookDeadLetterPanel(consoleShellState);
      restoreWebhookDeadLetterArchiveFocus(closedId);
    },
    async onConfirmArchive() {
      await submitWebhookDeadLetterArchive(consoleShellState);
    },
  });
}

/**
 * 局部重新读取并渲染 PAGE-083，避免 pending 轮询触发整个控制台的串行 API 列表。
 * owner 在 GET 内外都会复核；离页或身份变化后本函数不再接触 DOM。
 */
async function refreshWebhookDeadLetterPanel(consoleShellState) {
  const state = await loadWebhookDeadLettersState(consoleShellState, refreshSequence);
  if (!state || !currentWebhookDeadLetterActionOwner(consoleShellState)) {
    return;
  }
  renderWebhookDeadLetterPanel(consoleShellState, state);
  scheduleWebhookDeadLetterPoll(consoleShellState);
}

/** 把当前安全快照局部写入既有 panel，并只重绑 PAGE-083 自身事件。 */
function renderWebhookDeadLetterPanel(consoleShellState, state = webhookDeadLetterPageState.snapshot()) {
  if (!currentWebhookDeadLetterActionOwner(consoleShellState)) {
    webhookDeadLetterPageState.clear();
    return;
  }
  const panel = document.querySelector(".webhook-dead-letters-panel");
  if (!panel) {
    return;
  }
  panel.innerHTML = renderWebhookDeadLetters(state, authSession.context);
  bindWebhookDeadLetterPageInteractions(consoleShellState);
}

/**
 * 提交持久重放 intent。
 * 网络结果不确定时保留同一内存键；确定性 4xx 或成功后删除，所有回调均经过 action owner 门禁。
 */
async function submitWebhookDeadLetterReplay(deadLetterEventId, consoleShellState) {
  const owner = currentWebhookDeadLetterActionOwner(consoleShellState);
  if (!owner) {
    webhookDeadLetterPageState.clear();
    return;
  }
  let intent;
  try {
    intent = webhookDeadLetterPageState.prepareReplay(
      deadLetterEventId,
      createWebhookDeadLetterIdempotencyKey,
    );
  } catch (error) {
    webhookDeadLetterPageState.rejectAction(error);
    renderWebhookDeadLetterPanel(consoleShellState);
    return;
  }
  if (!intent) {
    renderWebhookDeadLetterPanel(consoleShellState);
    return;
  }
  const operation = webhookDeadLetterPageState.beginActionOperation(owner, {
    action: "replay",
    deadLetterEventId: intent.deadLetterEventId,
  });
  if (!operation) {
    return;
  }
  const operationIds = createWebhookDeadLetterOperationIds("replay");
  renderWebhookDeadLetterPanel(consoleShellState);
  try {
    const result = await replayWebhookDelivery({
      controlApiUrl,
      token: authSession.token,
      webhookDeliveryId: intent.webhookDeliveryId,
      idempotencyKey: intent.idempotencyKey,
      signal: operation.controller.signal,
      operationRequestId: operationIds.requestId,
      operationTraceId: operationIds.traceId,
    });
    const currentOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
    if (!webhookDeadLetterPageState.isActionOperationCurrent(operation, currentOwner)) {
      return;
    }
    webhookDeadLetterPageState.acceptAction(result, "replay");
  } catch (error) {
    const currentOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
    if (
      !webhookDeadLetterPageState.isActionOperationCurrent(operation, currentOwner)
      || isWebhookDeadLetterAbortError(error)
    ) {
      return;
    }
    // 会话失效时先销毁当前租户页面事实，再回到登录页，不能残留死信或动作反馈。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    // 只有网络结果未知或服务端暂时失败才保留原幂等键，确定性拒绝必须释放旧键。
    if (!shouldRetainWebhookDeadLetterIdempotencyKey(error)) {
      webhookDeadLetterPageState.releaseReplayKey(intent.webhookDeliveryId);
    }
    webhookDeadLetterPageState.rejectAction(error);
  } finally {
    const currentOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
    const shouldRender = webhookDeadLetterPageState.isActionOperationCurrent(operation, currentOwner);
    webhookDeadLetterPageState.finishActionOperation(operation);
    if (shouldRender) {
      renderWebhookDeadLetterPanel(consoleShellState);
      scheduleWebhookDeadLetterPoll(consoleShellState);
    }
  }
}

/** 提交逻辑归档；ID 只能从状态机确认框恢复，表单和 DOM 不能覆盖 tenant、actor 或状态。 */
async function submitWebhookDeadLetterArchive(consoleShellState) {
  const owner = currentWebhookDeadLetterActionOwner(consoleShellState);
  const intent = webhookDeadLetterPageState.archiveIntent();
  if (!owner || !intent) {
    webhookDeadLetterPageState.clear();
    return;
  }
  const operation = webhookDeadLetterPageState.beginActionOperation(owner, {
    action: "archive",
    deadLetterEventId: intent.deadLetterEventId,
  });
  if (!operation) {
    return;
  }
  const operationIds = createWebhookDeadLetterOperationIds("archive");
  renderWebhookDeadLetterPanel(consoleShellState);
  try {
    const result = await archiveWebhookDeadLetter({
      controlApiUrl,
      token: authSession.token,
      deadLetterEventId: intent.deadLetterEventId,
      signal: operation.controller.signal,
      operationRequestId: operationIds.requestId,
      operationTraceId: operationIds.traceId,
    });
    const currentOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
    if (!webhookDeadLetterPageState.isActionOperationCurrent(operation, currentOwner)) {
      return;
    }
    webhookDeadLetterPageState.acceptAction(result, "archive");
  } catch (error) {
    const currentOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
    if (
      !webhookDeadLetterPageState.isActionOperationCurrent(operation, currentOwner)
      || isWebhookDeadLetterAbortError(error)
    ) {
      return;
    }
    // 归档遇到会话失效也必须清空租户页面事实，不能继续展示上一身份的治理证据。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    webhookDeadLetterPageState.rejectAction(error);
  } finally {
    const currentOwner = currentWebhookDeadLetterActionOwner(consoleShellState);
    const shouldRender = webhookDeadLetterPageState.isActionOperationCurrent(operation, currentOwner);
    webhookDeadLetterPageState.finishActionOperation(operation);
    if (shouldRender) {
      renderWebhookDeadLetterPanel(consoleShellState);
    }
  }
}

/**
 * 为 replay_pending 安排最多八次局部 GET。
 * 离页、登出、401、身份变化和 pagehide 都由 state.clear 取消 timer 并推进 revision。
 */
function scheduleWebhookDeadLetterPoll(consoleShellState) {
  if (!currentWebhookDeadLetterActionOwner(consoleShellState)) {
    webhookDeadLetterPageState.cancelPoll();
    return;
  }
  webhookDeadLetterPageState.schedulePoll(async () => {
    if (!currentWebhookDeadLetterActionOwner(consoleShellState)) {
      webhookDeadLetterPageState.clear();
      return;
    }
    await refreshWebhookDeadLetterPanel(consoleShellState);
  }, { delayMs: 1500, maxAttempts: 8 });
}

/** 生成不含 tenant、用户、delivery 或事件 ID 的随机 request/trace 标识。 */
function createWebhookDeadLetterOperationIds(kind) {
  const randomPart = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 14)}`;
  return {
    requestId: `console_whdl_${kind}_${randomPart}`,
    traceId: `trc_console_whdl_${randomPart}`,
  };
}

/** 归档确认取消后按等值 data 字段恢复焦点，避免把不透明 ID 拼进 CSS selector。 */
function restoreWebhookDeadLetterArchiveFocus(deadLetterEventId) {
  if (!deadLetterEventId) {
    return;
  }
  for (const button of document.querySelectorAll("[data-webhook-dead-letter-archive]")) {
    if (button.dataset.webhookDeadLetterArchive === deadLetterEventId) {
      button.focus();
      return;
    }
  }
}
/**
 * 仅在后端确认 PAGE-069 为 active 导航且角色可读时加载当前租户模型评分。
 * viewer、手写但未授权的 hash、非活动页面和旧 refresh 都不会发出 GET；tenant_id 从不进入 URL。
 */
async function loadModelQualityScoresState(consoleShellState, refreshId) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.MODEL_QUALITY_READ)
    || consoleShellState?.activeNavigationItemId !== "model-quality-scores"
    || refreshId !== refreshSequence
  ) {
    return null;
  }
  const identity = modelQualityScoreIdentitySnapshot(authSession.context);
  if (!identity) {
    return null;
  }
  const query = JSON.stringify([
    modelQualityScoreFilters.modelId,
    modelQualityScoreFilters.limit,
    modelQualityScoreFilters.offset,
  ]);
  const ticket = modelQualityScorePageMemory.beginRefresh({
    identity,
    navigation: "model-quality-scores",
    query,
  });
  try {
    const result = await listModelQualityScores({
      controlApiUrl,
      token: authSession.token,
      modelId: modelQualityScoreFilters.modelId,
      limit: modelQualityScoreFilters.limit,
      offset: modelQualityScoreFilters.offset,
      signal: ticket.signal,
    });
    if (refreshId !== refreshSequence || !modelQualityScorePageMemory.isCurrentRefresh(ticket)) {
      return null;
    }
    modelQualityScorePageMemory.acceptRefresh(ticket, result);
    return modelQualityScorePageMemory.snapshot();
  } catch (error) {
    // Abort 或 owner 转移后的迟到错误不得清 Session、渲染错误或覆盖下一租户页面。
    if (
      isAbortError(error)
      || refreshId !== refreshSequence
      || !modelQualityScorePageMemory.isCurrentRefresh(ticket)
    ) {
      return null;
    }
    // 只有仍属于当前 GET owner 的 401 才可清理认证态，防止旧租户迟到错误退出新会话。
    // 当前 Summary GET 的 401 才可结束 Session；owner 转移错误已在上方静默丢弃。
    if (error instanceof AuthApiError && error.status === 401) {
      clearModelQualityScoreSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    const safeError = error instanceof AuthApiError
      ? error
      : {
          code: "model_quality_score.unavailable",
          message: "模型评分读取失败",
          requestId: "-",
        };
    modelQualityScorePageMemory.acceptRefreshError(ticket, safeError);
    return modelQualityScorePageMemory.snapshot();
  }
}

/**
 * 仅在后端确认 PAGE-081 active 且角色可读时加载当前租户规则。
 * viewer、未授权 hash、非活动页面和旧 refresh 不会发出 /error-maps 请求，tenant_id 永不进入 URL。
 */
async function loadErrorMapsState(consoleShellState, refreshId) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.PROVIDER_ERROR_MAP_READ)
    || consoleShellState?.activeNavigationItemId !== "error-maps"
    || refreshId !== refreshSequence
  ) {
    return null;
  }
  const identity = errorMapIdentitySnapshot(authSession.context);
  if (!identity) {
    return null;
  }
  const tenantId = String(
    authSession.context?.user?.tenant_id
      ?? authSession.context?.tenant?.tenant_id
      ?? "",
  ).trim();
  const handedOffReference = errorMapHandoff.consume({
    identity,
    tenantId,
    navigation: "error-maps",
  });
  if (handedOffReference) {
    // 新 handoff 从确定的全量边界开始；旧筛选不得让历史规则在目标页静默缺失。
    errorMapFilters = {
      providerId: "",
      source: handedOffReference.source,
      status: "",
      outputErrorCode: "",
      limit: 100,
      offset: 0,
    };
  }
  const retainedReference = errorMapPageMemory.snapshot().reference;
  const effectiveReference = handedOffReference ?? retainedReference;
  const effectiveFilters = effectiveReference
    ? {
        providerId: "",
        source: effectiveReference.source,
        status: "",
        outputErrorCode: "",
        limit: 100,
        offset: 0,
      }
    : errorMapFilters;
  const query = JSON.stringify([
    effectiveFilters.providerId,
    effectiveFilters.source,
    effectiveFilters.status,
    effectiveFilters.outputErrorCode,
    effectiveFilters.limit,
    effectiveFilters.offset,
    effectiveReference?.source ?? "",
    effectiveReference?.errorMapId ?? "",
    effectiveReference?.requestedVersion ?? null,
    effectiveReference?.requestedCatalogVersion ?? "",
  ]);
  const ticket = errorMapPageMemory.beginRefresh({
    identity,
    navigation: "error-maps",
    query,
    reference: effectiveReference,
  });
  try {
    const result = await listErrorMaps({
      controlApiUrl,
      token: authSession.token,
      // tenant handoff 必须使用认证租户下的精确 GET；builtin 没有可写资源路径，只读取对应只读目录。
      errorMapId: effectiveReference?.source === "tenant" ? effectiveReference.errorMapId : "",
      providerId: effectiveFilters.providerId,
      source: effectiveFilters.source,
      status: effectiveFilters.status,
      outputErrorCode: effectiveFilters.outputErrorCode,
      limit: effectiveFilters.limit,
      offset: effectiveFilters.offset,
      signal: ticket.signal,
    });
    const owner = { identity: errorMapIdentitySnapshot(authSession.context), navigation: "error-maps" };
    // 列表响应只有在刷新序号和完整 owner 都仍有效时才能替换当前租户页面状态。
    if (refreshId !== refreshSequence || !errorMapPageMemory.acceptRefresh(ticket, result, owner)) {
      return null;
    }
    return errorMapPageMemory.snapshot();
  } catch (error) {
    const owner = { identity: errorMapIdentitySnapshot(authSession.context), navigation: "error-maps" };
    // 取消、迟到或 owner 失配的失败均静默丢弃，不能覆盖新身份下已完成的刷新。
    if (isAbortError(error) || refreshId !== refreshSequence || !errorMapPageMemory.isCurrentRefresh(ticket, owner)) {
      return null;
    }
    // 当前刷新收到后端 401 时清空认证和规则内存，避免继续展示上一 Session 数据。
    if (error instanceof AuthApiError && error.status === 401) {
      clearErrorMapSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    errorMapPageMemory.acceptRefreshError(ticket, error, owner);
    return errorMapPageMemory.snapshot();
  }
}

/**
 * 生成一次控制台重放操作的幂等键。
 * 优先使用浏览器随机 UUID；测试或旧环境回退到时间戳和随机片段，键只进入请求体，不写持久存储。
 */
function createWebhookReplayKey() {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) {
    return `console_${uuid}`;
  }
  return `console_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

/**
 * 返回当前逻辑重放操作的稳定幂等键。
 * 网络结果不确定时保留该键；只有收到确定成功或明确 4xx 拒绝后才释放。
 */
function webhookReplayKeyFor(endpointId, deliveryId) {
  const operationId = `${endpointId}:${deliveryId}`;
  if (!webhookPendingReplayKeys.has(operationId)) {
    webhookPendingReplayKeys.set(operationId, createWebhookReplayKey());
  }
  return webhookPendingReplayKeys.get(operationId);
}

/**
 * 释放已经得到确定结果的重放幂等键，让下一次显式手工重放成为新的业务操作。
 */
function releaseWebhookReplayKey(endpointId, deliveryId) {
  webhookPendingReplayKeys.delete(`${endpointId}:${deliveryId}`);
}

/** 更新 PAGE-084 导出安全反馈，并只同步当前审计页的专属 DOM。 */
function setAuditExportState(nextState) {
  auditExportState = { ...nextState };
  syncAuditExportDomState();
}

/**
 * 同步固定尺寸按钮和 aria-live 反馈。
 * 动态值只写 textContent，且调用点已限制为校验后的 export ID/hash/行数/code/request_id。
 */
function syncAuditExportDomState() {
  const busy = auditExportLifecycle.isBusy();
  const button = document.querySelector("#audit-log-export-csv");
  if (button) {
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
  }
  const feedback = document.querySelector("[data-audit-export-feedback]");
  if (!feedback) {
    return;
  }
  const status = auditExportState.status ?? "idle";
  feedback.hidden = status === "idle";
  feedback.className = `audit-export-feedback status-${status}`;
  feedback.setAttribute("role", status === "error" ? "alert" : "status");
  if (status === "exporting") {
    feedback.textContent = "正在生成审计 CSV";
  } else if (status === "completed") {
    feedback.textContent = `导出 ID: ${auditExportState.auditExportId} · SHA-256: ${auditExportState.contentSha256} · 行数: ${auditExportState.rowCount} · request_id: ${auditExportState.requestId}`;
  } else if (status === "error") {
    feedback.textContent = `审计 CSV 导出失败 · code: ${auditExportState.code} · request_id: ${auditExportState.requestId}`;
  } else {
    feedback.textContent = "";
  }
}

/** 当前 operation 必须仍匹配完整身份、实际导航、独立 generation 与未取消 signal。 */
function ownsAuditExportOperation(operation) {
  return auditExportLifecycle.isCurrent(operation, {
    identity: auditExportIdentitySnapshot(authSession.context),
    navigation: currentRenderedNavigation(),
  });
}

/**
 * 同步销毁审计导出 owner 和反馈。
 * Blob 从不进入模块状态；abort 后即使 fetch 无法取消，迟到回调也会被 generation 门禁丢弃。
 */
function clearAuditExportSessionState() {
  auditExportLifecycle.clear();
  auditExportState = { status: "idle" };
  syncAuditExportDomState();
}

/** tenant、用户、角色、Session ID 或 revision 任一变化都清除上一 owner 与导出证据反馈。 */
function clearAuditExportStateOnIdentityChange(previous, current) {
  if (auditExportIdentitySnapshot(previous) !== auditExportIdentitySnapshot(current)) {
    clearAuditExportSessionState();
  }
}

/** 只允许稳定英文错误码进入页面，未知异常统一收敛而不反射 Error.message。 */
function safeAuditExportErrorCode(error) {
  const code = error instanceof AuthApiError ? String(error.code ?? "") : "";
  return /^[a-z][a-z0-9_.]{1,127}$/.test(code) ? code : "audit_export.unavailable";
}

/** 只允许公开链路字符进入反馈；网络异常退回当前 operation ID，不能显示任意异常属性。 */
function safeAuditExportRequestId(value, fallback) {
  const candidate = String(value ?? "").trim();
  if (/^[A-Za-z0-9_.:-]{1,128}$/.test(candidate)) {
    return candidate;
  }
  const safeFallback = String(fallback ?? "").trim();
  return /^[A-Za-z0-9_.:-]{1,128}$/.test(safeFallback) ? safeFallback : "-";
}

/**
 * 更新当前会话内的 CSV 导出状态，并同步当前 DOM。
 * 完整控制台刷新较重，轮询阶段只更新专属按钮和 aria-live 反馈，状态仍会在下一轮刷新时由模板恢复。
 */
function setUsageExportState(nextState) {
  usageExportState = { ...nextState };
  syncUsageExportDomState();
}

/**
 * 把导出状态写入当前已渲染的命令和反馈节点。
 * 只使用 textContent 和固定 class/status，服务端 job、文件名或错误文本不能成为 HTML。
 */
function syncUsageExportDomState() {
  const busy = ["requesting", "pending", "processing", "downloading"].includes(usageExportState.status);
  const button = document.querySelector("#usage-export-csv");
  if (button) {
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
  }
  const feedback = document.querySelector("[data-usage-export-feedback]");
  if (!feedback) {
    return;
  }
  feedback.hidden = usageExportState.status === "idle";
  feedback.className = `usage-export-feedback status-${usageExportState.status}`;
  feedback.setAttribute("role", usageExportState.status === "error" ? "alert" : "status");
  feedback.textContent = usageExportState.message ?? "";
}

/**
 * 格式化 pending/processing 的安全反馈。
 * 后端状态只来自客户端白名单校验后的 DTO；中文映射避免直接把未知状态反射到页面。
 */
function usageExportProgressMessage(exportJob, requestId) {
  const statusLabel = exportJob.status === "processing" ? "正在生成" : "等待生成";
  return `导出任务 ${exportJob.usage_export_job_id} ${statusLabel} · request_id: ${requestId ?? "-"}`;
}

/**
 * 判断异步回调是否仍拥有当前导出代次。
 * controller、generation 和 AbortSignal 三者同时匹配，才能更新当前会话页面。
 */
function ownsUsageExportGeneration(generation, controller) {
  return generation === usageExportGeneration
    && controller === usageExportController
    && !controller.signal.aborted;
}

/**
 * 识别浏览器 fetch、DOMException 和 Node 单测产生的标准取消错误。
 */
function isAbortError(error) {
  return error?.name === "AbortError";
}

/**
 * 判断错误后是否保留逻辑导出幂等键。
 * 已知 job 永远保留以便继续查询；创建阶段的网络、5xx、响应格式或浏览器下载错误结果不确定，
 * 必须复用原 request_id。只有明确拒绝且未创建 job 的 4xx 才允许下一次形成新操作。
 */
function shouldRetainUsageExportOperation(error, jobId) {
  if (jobId) {
    return true;
  }
  // 网络异常没有结构化 HTTP 结论，服务端可能已经提交任务，因此必须保留原幂等键。
  if (!(error instanceof AuthApiError)) {
    return true;
  }
  const status = Number(error.status ?? 0);
  return status === 0 || status >= 500 || status === 200;
}

/**
 * 清除当前会话的导出状态并取消活动 fetch/轮询。
 * generation 先递增，使即将完成的旧 Promise 即使忽略 Abort 也不能覆盖新会话。
 */
function clearUsageExportSessionState() {
  usageExportGeneration += 1;
  usageExportController?.abort();
  usageExportController = null;
  usageExportState = { status: "idle", message: "" };
  syncUsageExportDomState();
}

/**
 * 用户、租户或 Session 变化时清除 job、文件名和 request_id，禁止跨身份恢复浏览器内轮询。
 */
function clearUsageExportStateOnIdentityChange(previous, current) {
  const previousIdentity = previous
    ? `${previous.user?.tenant_id}:${previous.user?.user_id ?? previous.user?.email}:${previous.session?.session_id}`
    : null;
  const currentIdentity = current
    ? `${current.user?.tenant_id}:${current.user?.user_id ?? current.user?.email}:${current.session?.session_id}`
    : null;
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearUsageExportSessionState();
  }
}

/**
 * 用户、租户、角色或 Session 变化时销毁正式账本页面内存。
 * 角色也参与身份指纹，admin 被降权后即使 Session ID 不变也不能保留账本详情和调整原因。
 */
function clearFinOpsStateOnIdentityChange(previous, current) {
  const previousIdentity = previous
    ? `${previous.user?.tenant_id}:${previous.user?.user_id ?? previous.user?.email}:${previous.user?.role}:${previous.session?.session_id}`
    : null;
  const currentIdentity = current
    ? `${current.user?.tenant_id}:${current.user?.user_id ?? current.user?.email}:${current.user?.role}:${current.session?.session_id}`
    : null;
  if (previousIdentity && previousIdentity !== currentIdentity) {
    finOpsPageState.clear();
  }
}

/**
 * tenant、用户、角色或 Session 变化时清除成本异常页面内存。
 * admin 被降权后 identity 会收敛为空，旧异常证据、cursor 和未决治理动作必须立即失效。
 */
function clearCostAnomalyStateOnIdentityChange(previous, current) {
  if (costAnomalyIdentitySnapshot(previous) !== costAnomalyIdentitySnapshot(current)) {
    costAnomalyPageState.clear();
  }
}

/**
 * tenant、用户、Session、权限 revision 或授权指纹变化时销毁 PAGE-098 全部内存。
 * read 被撤销会使 identity 收敛为空；manage 变化也会通过授权指纹触发 owner 重建。
 */
function clearSessionRiskStateOnIdentityChange(previous, current) {
  if (sessionRiskReadIdentitySnapshot(previous) !== sessionRiskReadIdentitySnapshot(current)) {
    sessionRiskPageState.clear();
  }
}

/**
 * tenant、用户、Session、权限 revision 或授权指纹变化时销毁文件工作台全部内存。
 * file.read 被撤销会使身份收敛为空；file.write 变化也会改变授权指纹并重新建立只读/读写投影。
 */
function clearFileStateOnIdentityChange(previous, current) {
  if (fileReadIdentitySnapshot(previous) !== fileReadIdentitySnapshot(current)) {
    filePageState.clear();
  }
}
/**
 * tenant、用户、角色、Session 或 Session revision 变化时销毁 PAGE-083 状态。
 * 完整 owner 比较同时覆盖原地降权和 Token 续期 revision，旧 cursor、键、反馈和轮询不得跨身份保留。
 */
function clearWebhookDeadLetterStateOnIdentityChange(previous, current) {
  if (webhookDeadLetterIdentitySnapshot(previous) !== webhookDeadLetterIdentitySnapshot(current)) {
    webhookDeadLetterPageState.clear();
  }
}
/**
 * 认证用户、租户或 Session 变化时清理 Webhook 短生命周期状态，防止跨账号复用 Secret 或结果。
 */
function clearWebhookStateOnIdentityChange(previous, current) {
  const previousIdentity = previous
    ? `${previous.user?.tenant_id}:${previous.user?.user_id ?? previous.user?.email}:${previous.session?.session_id}`
    : null;
  const currentIdentity = current
    ? `${current.user?.tenant_id}:${current.user?.user_id ?? current.user?.email}:${current.session?.session_id}`
    : null;
  if (previousIdentity && previousIdentity !== currentIdentity) {
    clearWebhookSessionState();
  }
}

/**
 * 清除仅属于当前认证会话的 Webhook 状态；该函数不得把一次性 Secret 写入任何持久存储。
 */
function clearWebhookSessionState() {
  // 操作反馈和错误可能包含上一租户的 request_id；身份切换时必须与一次性 Secret 一起清除。
  webhookActionError = null;
  webhookFeedback = "";
  webhookCreatedSecret = null;
  webhookLastTestResult = null;
  webhookLastReplayResult = null;
  webhookSelectedEndpointId = null;
  webhookDeliveryFilters = { eventType: "", status: "all" };
  webhookPendingReplayKeys.clear();
}

/** 将 PAGE-071 当前筛选序列化为稳定 GET owner，不包含 tenant_id、actor 或 Token。 */
function requestFeedbackSummaryQuery() {
  return JSON.stringify([
    requestFeedbackSummaryFilters.modelId,
    requestFeedbackSummaryFilters.fromTime,
    requestFeedbackSummaryFilters.toTime,
    requestFeedbackSummaryFilters.limit,
    requestFeedbackSummaryFilters.offset,
  ]);
}

/** 返回 PAGE-071 实时 owner；refresh 代次变化会立即让迟到 200/401 失效。 */
function currentRequestFeedbackSummaryOwner(refreshId = refreshSequence) {
  const current = refreshId === refreshSequence;
  return {
    identity: requestFeedbackIdentitySnapshot(authSession.context),
    navigation: current ? "feedback-summary" : "",
    query: requestFeedbackSummaryQuery(),
    refreshId,
    currentRefreshId: refreshSequence,
  };
}

/** 离页、登出、401、pagehide 或导航裁剪时 abort Summary GET 并清除筛选和旧租户 DOM。 */
function clearRequestFeedbackSummarySessionState() {
  requestFeedbackSummaryMemory.invalidate();
  requestFeedbackSummaryFilters = { modelId: "", fromTime: "", toTime: "", limit: 20, offset: 0 };
  document.querySelector(".feedback-summary-panel")?.replaceChildren();
}

/** tenant、用户、角色、Session ID 或 revision 变化时销毁上一身份的 Summary owner。 */
function clearRequestFeedbackSummaryStateOnIdentityChange(previous, current) {
  if (requestFeedbackIdentitySnapshot(previous) !== requestFeedbackIdentitySnapshot(current)) {
    clearRequestFeedbackSummarySessionState();
  }
}

/**
 * 只在后端确认 PAGE-071 为 active 且角色可读时读取当前租户 Summary。
 * viewer、手写未授权 hash、非活动页面和旧 refresh 都不会发 GET，tenant_id 从不进入 URL。
 */
async function loadRequestFeedbackSummaryState(consoleShellState, refreshId) {
  const owner = currentRequestFeedbackSummaryOwner(refreshId);
  if (
    !hasPermission(authSession.context, PERMISSIONS.FEEDBACK_USE)
    || consoleShellState?.activeNavigationItemId !== "feedback-summary"
    || refreshId !== refreshSequence
    || !owner.identity
  ) {
    return null;
  }
  const ticket = requestFeedbackSummaryMemory.beginRead(owner);
  if (!ticket) {
    return null;
  }
  try {
    const result = await getRequestFeedbackSummary({
      controlApiUrl,
      token: authSession.token,
      modelId: requestFeedbackSummaryFilters.modelId,
      fromTime: requestFeedbackSummaryFilters.fromTime,
      toTime: requestFeedbackSummaryFilters.toTime,
      limit: requestFeedbackSummaryFilters.limit,
      offset: requestFeedbackSummaryFilters.offset,
      signal: ticket.signal,
    });
    const currentOwner = currentRequestFeedbackSummaryOwner(refreshId);
    if (!requestFeedbackSummaryMemory.isCurrent(ticket, currentOwner)) {
      return null;
    }
    if (!requestFeedbackSummaryMemory.acceptSuccess(ticket, result, currentOwner)) {
      requestFeedbackSummaryMemory.acceptFailure(ticket, {
        code: "feedback.unavailable",
        message: "反馈报表响应不完整",
        requestId: result?.request_id ?? "-",
      }, currentOwner);
    }
    return requestFeedbackSummaryMemory.snapshot();
  } catch (error) {
    const currentOwner = currentRequestFeedbackSummaryOwner(refreshId);
    // Abort 或 owner 转移后的错误属于主动生命周期收口，不能退出新 Session 或显示上一租户 request_id。
    if (isAbortError(error) || !requestFeedbackSummaryMemory.isCurrent(ticket, currentOwner)) {
      return null;
    }
    // 只有当前 Summary owner 的 401 才能结束认证态，迟到 401 已在上方被 generation 门禁丢弃。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRequestFeedbackSummarySessionState();
      clearRequestLogSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    const safeError = error instanceof AuthApiError
      ? error
      : {
          code: "feedback.unavailable",
          message: "反馈报表读取失败",
          requestId: "-",
        };
    requestFeedbackSummaryMemory.acceptFailure(ticket, safeError, currentOwner);
    return requestFeedbackSummaryMemory.snapshot();
  }
}

/**
 * 绑定 PAGE-079 查询、显式刷新和清空动作。
 * 所有输入只进入一次 GET 的 path；tenant、角色、actor 与 include 开关永不由表单提交。
 */
function bindRequestExplanationInteractions(consoleShellState) {
  if (
    !hasPermission(authSession.context, PERMISSIONS.REQUEST_EXPLANATION_READ)
    || consoleShellState?.activeNavigationItemId !== "request-explanations"
  ) {
    return;
  }

  const form = document.querySelector("#request-explanation-form");
  const owner = currentRequestExplanationOwner();
  const requestIdInput = form?.querySelector("[name='requestId']");
  requestIdInput?.addEventListener("input", () => {
    // 草稿只驻留当前 owner 内存；不写 URL/Storage，也不在输入阶段触发 explain GET。
    requestExplanationPageMemory.setDraft({
      identity: owner.identity,
      tenantId: owner.tenantId,
      navigation: owner.navigation,
      requestId: requestIdInput.value,
    });
    requestExplanationPageMemory.setFocusTarget({
      identity: owner.identity,
      tenantId: owner.tenantId,
      navigation: owner.navigation,
      target: "request-id",
    });
  });

  for (const sectionElement of document.querySelectorAll("[data-request-explanation-section]")) {
    sectionElement.addEventListener("toggle", () => {
      const sectionKey = sectionElement.dataset.requestExplanationSection;
      requestExplanationPageMemory.setSectionDisclosure({
        identity: owner.identity,
        tenantId: owner.tenantId,
        navigation: owner.navigation,
        sectionKey,
        open: sectionElement.open,
      });
    });
  }

  // 焦点恢复只接受 renderer 的固定 data 值；单纯初始 open/toggle 不得覆盖用户实际焦点。
  for (const control of document.querySelectorAll("[data-request-explanation-focus]")) {
    control.addEventListener("focus", () => {
      requestExplanationPageMemory.setFocusTarget({
        identity: owner.identity,
        tenantId: owner.tenantId,
        navigation: owner.navigation,
        target: control.dataset.requestExplanationFocus,
      });
    });
  }

  restoreRequestExplanationFocus();

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitOwner = currentRequestExplanationOwner();
    const data = new FormData(form);
    requestExplanationPageMemory.select({
      identity: submitOwner.identity,
      tenantId: submitOwner.tenantId,
      navigation: submitOwner.navigation,
      requestId: String(data.get("requestId") ?? ""),
      source: "manual",
    });
    requestExplanationPageMemory.setFocusTarget({
      identity: submitOwner.identity,
      tenantId: submitOwner.tenantId,
      navigation: submitOwner.navigation,
      target: "query",
    });
    renderCurrentRequestExplanationSnapshot(consoleShellState);
    await refresh();
  });

  const refreshButton = document.querySelector("#request-explanation-refresh");
  refreshButton?.addEventListener("click", async () => {
    const owner = currentRequestExplanationOwner();
    const query = requestExplanationPageMemory.query();
    requestExplanationPageMemory.select({
      identity: owner.identity,
      tenantId: owner.tenantId,
      navigation: owner.navigation,
      requestId: query.requestId,
      source: query.source,
    });
    requestExplanationPageMemory.setFocusTarget({
      identity: owner.identity,
      tenantId: owner.tenantId,
      navigation: owner.navigation,
      target: "refresh",
    });
    renderCurrentRequestExplanationSnapshot(consoleShellState);
    await refresh();
  });

  const clearButton = document.querySelector("#request-explanation-clear");
  clearButton?.addEventListener("click", async () => {
    const clearOwner = currentRequestExplanationOwner();
    requestExplanationPageMemory.clearSelection({
      identity: clearOwner.identity,
      tenantId: clearOwner.tenantId,
      navigation: clearOwner.navigation,
    });
    requestExplanationHandoff.clear();
    renderCurrentRequestExplanationSnapshot(consoleShellState);
    await refresh();
  });
}

/**
 * 按状态机保存的固定目标恢复焦点。
 * 只在 renderer 生成的 data 属性集合中做等值查找，不把外部字符串拼接为 CSS selector。
 */
function restoreRequestExplanationFocus() {
  const target = requestExplanationPageMemory.query().focusTarget;
  if (!target) {
    return;
  }
  const candidate = Array.from(document.querySelectorAll("[data-request-explanation-focus]"))
    .find((element) => element.dataset.requestExplanationFocus === target);
  candidate?.focus();
}

/**
 * 局部渲染当前解释快照，让显式查询立即出现 loading/error，而不等待全控制台串行刷新结束。
 * 局部替换后重新绑定新 DOM；真实权限和数据仍只来自随后执行的 Control API GET。
 */
function renderCurrentRequestExplanationSnapshot(consoleShellState) {
  const panel = document.querySelector(".request-explanations-panel");
  if (!panel) {
    return;
  }
  panel.innerHTML = renderRequestExplanationPanel(
    requestExplanationPageMemory.snapshot(),
    authSession.context,
  );
  bindRequestExplanationInteractions(consoleShellState);
}

/** 返回当前 RequestExplanation owner；身份、租户、导航和刷新代次均从实时应用状态读取。 */
function currentRequestExplanationOwner(refreshId = refreshSequence) {
  const identity = requestExplanationIdentitySnapshot(authSession.context);
  const tenantId = String(
    authSession.context?.user?.tenant_id
      ?? authSession.context?.tenant?.tenant_id
      ?? "",
  ).trim();
  const current = refreshId === refreshSequence;
  return {
    identity,
    tenantId,
    navigation: current && identity ? "request-explanations" : "",
    refreshId,
    currentRefreshId: refreshSequence,
  };
}

/** 登出、离页、降权或导航裁剪时同时销毁解释、活动 GET、handoff 和已渲染租户事实。 */
function clearRequestExplanationSessionState() {
  requestExplanationPageMemory.leave();
  requestExplanationHandoff.clear();
  document.querySelector(".request-explanations-panel")?.replaceChildren();
}

/** tenant、用户、角色、Session ID 或 revision 任一变化都必须清空上一身份的解释。 */
function clearRequestExplanationStateOnIdentityChange(previous, current) {
  if (requestExplanationIdentitySnapshot(previous) !== requestExplanationIdentitySnapshot(current)) {
    clearRequestExplanationSessionState();
  }
}

/** 把未知浏览器异常收敛为固定公开字段，禁止 response、stack 或原始正文进入 DOM。 */
function safeRequestExplanationClientError(error, fallbackRequestId = "-") {
  if (error instanceof AuthApiError) {
    return error;
  }
  return {
    code: "request_explanation.unavailable",
    message: "请求解释读取失败",
    requestId: fallbackRequestId,
  };
}

/**
 * 只在后端确认的 active PAGE-079 中消费一次 handoff 并读取解释。
 * 成功 GET 已由后端原子创建 request.explanation_viewed，本函数不得再写通用页面访问事件。
 */
async function loadRequestExplanationState(consoleShellState, refreshId) {
  const owner = currentRequestExplanationOwner(refreshId);
  if (
    !hasPermission(authSession.context, PERMISSIONS.REQUEST_EXPLANATION_READ)
    || consoleShellState?.activeNavigationItemId !== "request-explanations"
    || refreshId !== refreshSequence
    || !owner.identity
    || !owner.tenantId
  ) {
    return null;
  }

  if (!requestExplanationPageMemory.query().requestId) {
    const handoff = requestExplanationHandoff.consume({
      identity: owner.identity,
      tenantId: owner.tenantId,
      navigation: owner.navigation,
    });
    if (handoff) {
      requestExplanationPageMemory.select({
        identity: owner.identity,
        tenantId: owner.tenantId,
        navigation: owner.navigation,
        requestId: handoff.requestId,
        source: handoff.source,
      });
    }
  }

  const ticket = requestExplanationPageMemory.beginRead(owner);
  if (!ticket) {
    return requestExplanationPageMemory.snapshot();
  }
  try {
    const result = await getRequestExplanation({
      controlApiUrl,
      token: authSession.token,
      requestId: ticket.requestId,
      signal: ticket.signal,
    });
    const currentOwner = currentRequestExplanationOwner(refreshId);
    if (!requestExplanationPageMemory.isCurrent(ticket, currentOwner)) {
      return null;
    }
    if (!requestExplanationPageMemory.acceptSuccess(ticket, result, currentOwner)) {
      requestExplanationPageMemory.acceptFailure(ticket, {
        code: "request_explanation.response_invalid",
        message: "请求解释响应不完整",
        requestId: result?.request_id ?? "-",
      }, currentOwner);
    }
    return requestExplanationPageMemory.snapshot();
  } catch (error) {
    const currentOwner = currentRequestExplanationOwner(refreshId);
    // Abort 或 owner 转移属于主动清理，迟到 401/错误不得结束新 Session 或恢复旧租户事实。
    if (isAbortError(error) || !requestExplanationPageMemory.isCurrent(ticket, currentOwner)) {
      return null;
    }
    // 只有当前解释 owner 的 401 才能结束会话；上方门禁已排除迟到的旧身份响应。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRequestExplanationSessionState();
      clearRequestLogSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    requestExplanationPageMemory.acceptFailure(
      ticket,
      safeRequestExplanationClientError(error, ticket.requestId),
      currentOwner,
    );
    return requestExplanationPageMemory.snapshot();
  }
}


/** 将 PAGE-080 表单读取为纯内存筛选；不把值写入 location、Storage 或日志。 */
function requestLogFiltersFromForm(data) {
  const utc = (name) => optionalText(data.get(name)) ? toUtcIso(data.get(name)) : "";
  return {
    requestId: optionalText(data.get("requestId")),
    traceId: optionalText(data.get("traceId")),
    status: optionalText(data.get("status")),
    requestModel: optionalText(data.get("requestModel")),
    resolvedModel: optionalText(data.get("resolvedModel")),
    providerCode: optionalText(data.get("providerCode")),
    virtualKeyId: optionalText(data.get("virtualKeyId")),
    environmentId: optionalText(data.get("environmentId")),
    projectId: optionalText(data.get("projectId")),
    applicationId: optionalText(data.get("applicationId")),
    snapshotVersion: optionalText(data.get("snapshotVersion")),
    errorCode: optionalText(data.get("errorCode")),
    createdFrom: utc("createdFrom"),
    createdTo: utc("createdTo"),
    latencyMsMin: optionalText(data.get("latencyMsMin")),
    latencyMsMax: optionalText(data.get("latencyMsMax")),
    totalTokensMin: optionalText(data.get("totalTokensMin")),
    totalTokensMax: optionalText(data.get("totalTokensMax")),
    costAmountMin: optionalText(data.get("costAmountMin")),
    costAmountMax: optionalText(data.get("costAmountMax")),
    currency: optionalText(data.get("currency")),
    terminalStage: optionalText(data.get("terminalStage")),
    sortBy: optionalText(data.get("sortBy")) || "created_at",
    sortDirection: optionalText(data.get("sortDirection")) || "desc",
    limit: 20,
    offset: 0,
  };
}

/**
 * 将 PAGE-099 表单收敛为输出安全高级筛选。
 * tenant_id 不由浏览器提交，terminalStage 固定为 output_safety，枚举仍会由页面内存和后端双重校验。
 */
function outputSafetyFiltersFromForm(data) {
  const utc = (name) => optionalText(data.get(name)) ? toUtcIso(data.get(name)) : "";
  return {
    requestId: optionalText(data.get("requestId")),
    outputSafetyStatus: optionalText(data.get("outputSafetyStatus")),
    outputSafetyMode: optionalText(data.get("outputSafetyMode")),
    outputSafetyExposure: optionalText(data.get("outputSafetyExposure")),
    outputSafetyBilling: optionalText(data.get("outputSafetyBilling")),
    createdFrom: utc("createdFrom"),
    createdTo: utc("createdTo"),
    terminalStage: "output_safety",
    sortBy: "created_at",
    sortDirection: "desc",
    limit: 20,
    offset: 0,
  };
}

/**
 * 从当前仍挂载的 PAGE-080 表单读取保存内容。
 * SavedFilter 保存/更新不能复用上次已查询内存，否则用户刚编辑但尚未查询的条件会被静默丢失。
 */
function currentRequestLogFiltersFromForm() {
  const form = document.querySelector("#request-log-filter-form");
  return form ? requestLogFiltersFromForm(new FormData(form)) : null;
}

/**
 * 绑定 PAGE-099 详情选择与刷新。
 *
 * 列表按钮直接绑定，局部详情渲染后再绑定新生成的刷新按钮；request_id 只进入当前 RequestLog
 * 内存 owner，不写入 URL、Storage 或日志。
 */
function bindOutputSafetyDetailInteractions(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "output-safety") return;
  const panel = document.querySelector(".output-safety-panel");
  if (!panel) return;
  // 同一 DOM 可能因局部更新再次进入绑定流程；面板标记和按钮标记共同保证监听器幂等。
  if (panel.dataset.outputSafetyDetailBound === "true") return;
  panel.dataset.outputSafetyDetailBound = "true";
  for (const button of panel.querySelectorAll("[data-request-log-detail]")) {
    bindOutputSafetyDetailButton(button, consoleShellState, panel);
  }
  bindOutputSafetyDetailButton(
    panel.querySelector("[data-request-log-detail-refresh]"),
    consoleShellState,
    panel,
  );
}

/**
 * 为单个 PAGE-099/100 按钮绑定 owner 门禁后的读取。
 * 按钮必须仍属于当前面板；局部重绘产生的新刷新按钮会由渲染函数再次调用本入口。
 */
function bindOutputSafetyDetailButton(button, consoleShellState, panel) {
  if (!button || button.dataset.outputSafetyDetailActionBound === "true") return;
  button.dataset.outputSafetyDetailActionBound = "true";
  button.addEventListener("click", async () => {
    if (!button.isConnected || !panel.contains(button) || !requestLogInteractionAllowed(consoleShellState)) return;
    const requestId = String(
      button.dataset.requestLogDetail ?? button.dataset.requestLogDetailRefresh ?? "",
    ).trim();
    const selectedRequestId = requestLogPageMemory.query().selectedRequestId;
    const isRefresh = button.hasAttribute("data-request-log-detail-refresh");
    // 刷新按钮只能重读当前 owner 已选择的 ID；伪造/迟到 DOM 不能借此切换详情。
    if (!requestId || (isRefresh && requestId !== selectedRequestId)) return;

    const configured = configureCurrentRequestLogState(
      requestLogPageMemory.query().filters,
      requestId,
      consoleShellState.activeNavigationItemId,
    );
    // owner 配置失败说明身份、租户或导航已失效；此时不能仅凭仍留在 DOM 的按钮继续读取详情。
    if (!configured) return;
    const detailPane = panel.querySelector(".output-safety-detail-pane");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    detailPane?.setAttribute("aria-busy", "true");
    try {
      await refreshOutputSafetyDetail(consoleShellState);
    } finally {
      // 局部渲染不会替换列表按钮；仍连接当前面板时恢复可操作状态。
      if (button.isConnected && panel.contains(button)) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
      detailPane?.removeAttribute("aria-busy");
    }
  });
}

/**
 * 只刷新 PAGE-099 的 PAGE-100 详情区，避免一次行点击串行重读整个控制台。
 *
 * 读取仍复用 RequestLog list/detail tenant owner 和 Control API 权限校验；推进全局刷新代次会淘汰
 * 已在途的旧页面刷新，活动计数则阻止 15 秒定时器在本轮中途接管 owner。
 */
async function refreshOutputSafetyDetail(consoleShellState) {
  if (
    !requestLogInteractionAllowed(consoleShellState)
    || currentRenderedNavigation() !== "output-safety"
  ) {
    return;
  }
  const refreshId = ++refreshSequence;
  activeRefreshCount += 1;
  try {
    const state = await loadRequestLogsState(consoleShellState, refreshId);
    if (
      refreshId !== refreshSequence
      || !authSession.token
      || currentRenderedNavigation() !== "output-safety"
    ) {
      return;
    }
    // 角色在页面驻留期间被撤权时，必须先销毁旧租户事实，再由完整刷新重读 /auth/me 与后端导航裁剪。
    if (state?.state === "error" && state.code === "auth.permission_denied") {
      clearRequestLogSessionState();
      await refresh();
      return;
    }
    const detailPane = document.querySelector(
      ".output-safety-panel .output-safety-detail-pane",
    );
    if (!detailPane) return;
    // 列表失败也只投影统一安全错误；原始响应、Prompt、Response 和 DLP pattern 永不进入 DOM。
    const detailError = state?.state === "error"
      ? { message: state.message, requestId: state.requestId }
      : state?.detailError;
    detailPane.innerHTML = renderRequestLogDetail(
      state?.detail,
      detailError,
      null,
      authSession.context,
      currentI18n,
    );
    // PAGE-100 innerHTML 会创建新的刷新按钮；只对该按钮直接绑定，列表和父面板均不重复注册。
    bindOutputSafetyDetailButton(
      detailPane.querySelector("[data-request-log-detail-refresh]"),
      consoleShellState,
      detailPane.closest(".output-safety-panel"),
    );
  } finally {
    activeRefreshCount -= 1;
  }
}

/**
 * 核验 PAGE-080 交互仍属于后端确认的活动导航和完整可读身份。
 * 前端门禁用于销毁迟到交互，真正的 tenant 隔离和 RBAC 仍由 Control API 执行。
 */
function requestLogInteractionAllowed(consoleShellState) {
  return Boolean(
    authSession.token
    && REQUEST_LOG_NAVIGATION_IDS.has(consoleShellState?.activeNavigationItemId)
    && requestLogIdentitySnapshot(authSession.context),
  );
}

/** 只从当前 owner 已接纳的 SavedFilter 列表查找，DOM 中伪造的 ID 不会触发跨 owner 请求。 */
function currentSavedRequestLogFilter(savedFilterId) {
  const id = String(savedFilterId ?? "").trim();
  return requestLogPageMemory.snapshot()?.savedFilters?.find(
    (item) => item.saved_filter_id === id,
  ) ?? null;
}

/**
 * 执行当前 owner 的 SavedFilter 变更。
 * Abort 只是及时取消网络；identity/generation ticket 才是迟到成功与错误的最终门禁。
 */
async function mutateCurrentSavedRequestLogFilter(
  operation,
  { clearSelection = false, activeNavigationItemId = "" } = {},
) {
  // 事件处理器之外再做一次导航与身份核验，手工触发的旧 DOM 回调不能复活已离开的 owner。
  if (!requestLogInteractionAllowed({ activeNavigationItemId })) return;
  const owner = currentRequestLogOwner();
  const ticket = requestLogPageMemory.beginSavedFilterMutation(owner);
  // 不完整身份或已失效 Session 不得发出 SavedFilter 请求，DOM 可见性不能替代该内存门禁。
  if (!ticket || !authSession.token) return;
  try {
    await operation(ticket.signal);
    const currentOwner = currentRequestLogOwner();
    if (!requestLogPageMemory.isCurrentSavedFilterMutation(ticket, currentOwner)) return;
    requestLogPageMemory.finishSavedFilterMutation(ticket, currentOwner);
    if (clearSelection) {
      configureCurrentRequestLogState({
        ...requestLogPageMemory.query().filters,
        selectedSavedFilterId: "",
        offset: 0,
      }, null);
    }
    await refresh();
  } catch (error) {
    const currentOwner = currentRequestLogOwner();
    // 主动取消和 owner 迁移都静默丢弃，禁止旧用户错误覆盖新身份页面。
    if (isAbortError(error) || !requestLogPageMemory.isCurrentSavedFilterMutation(ticket, currentOwner)) return;
    // 只有仍属于当前 owner 的 401 才能结束会话，迟到 401 已被上一分支拒绝。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRequestLogSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    requestLogPageMemory.acceptSavedFilterMutationError(
      ticket,
      safeRequestLogClientError(error, "保存筛选操作失败"),
      currentOwner,
    );
    requestLogPageMemory.finishSavedFilterMutation(ticket, currentOwner);
    await refresh();
  }
}

/** 返回当前 RequestLog owner；refresh、身份、租户和两个允许的导航 ID 均从实时状态读取。 */
function currentRequestLogOwner(
  refreshId = refreshSequence,
  requestedNavigation = currentRenderedNavigation(),
) {
  const identity = requestLogIdentitySnapshot(authSession.context);
  const tenantId = String(
    authSession.context?.user?.tenant_id
      ?? authSession.context?.tenant?.tenant_id
      ?? "",
  ).trim();
  const current = refreshId === refreshSequence;
  const navigation = REQUEST_LOG_NAVIGATION_IDS.has(requestedNavigation)
    ? requestedNavigation
    : "";
  return {
    identity,
    tenantId,
    navigation: current && identity ? navigation : "",
    refreshId,
    currentRefreshId: refreshSequence,
  };
}

/** 当前页面交互统一经完整身份 owner 配置筛选和详情选择，并立即淘汰旧异步请求。 */
function configureCurrentRequestLogState(
  nextFilters,
  nextSelectedRequestId,
  requestedNavigation = currentRenderedNavigation(),
) {
  const owner = currentRequestLogOwner(refreshSequence, requestedNavigation);
  const currentSelectedRequestId = requestLogPageMemory.query().selectedRequestId;
  const normalizedNextRequestId = String(nextSelectedRequestId ?? "").trim() || null;
  if (currentSelectedRequestId !== normalizedNextRequestId) {
    // 切换详情会立即 abort 独立 Feedback POST 并清除草稿，不能等待下一轮详情 GET 返回后再淘汰旧 owner。
    requestFeedbackMutationMemory.leave();
  }
  return requestLogPageMemory.configure({
    identity: owner.identity,
    tenantId: owner.tenantId,
    navigation: owner.navigation,
    nextFilters,
    nextSelectedRequestId,
  });
}

/** 登出、401、离页、pagehide 或导航裁剪时销毁 RequestLog 的全部租户内存和 DOM。 */
function clearRequestLogSessionState() {
  requestLogPageMemory.leave();
  requestFeedbackMutationMemory.leave();
  document.querySelector(".request-logs-panel")?.replaceChildren();
  document.querySelector(".output-safety-panel")?.replaceChildren();
}

/** tenant、用户、角色、Session ID 或 revision 任一变化都必须清除上一 owner。 */
function clearRequestLogStateOnIdentityChange(previous, current) {
  if (requestLogIdentitySnapshot(previous) !== requestLogIdentitySnapshot(current)) {
    clearRequestLogSessionState();
  }
}

/** 把未知浏览器错误折叠为 renderer 可展示的固定安全字段。 */
function safeRequestLogClientError(error, fallbackMessage, fallbackRequestId = "-") {
  if (error instanceof AuthApiError) {
    return error;
  }
  return {
    code: "request_log.client_unavailable",
    message: fallbackMessage,
    requestId: fallbackRequestId,
  };
}

/**
 * 只在后端确认的 active RequestLog 页面顺序读取列表和所选详情。
 * list/detail 各自持有 AbortSignal；所有成功和失败回调都必须通过完整 owner/generation 门禁。
 */
async function loadRequestLogsState(consoleShellState, refreshId) {
  const activeNavigation = consoleShellState?.activeNavigationItemId;
  const owner = currentRequestLogOwner(refreshId, activeNavigation);
  if (
    !hasPermission(authSession.context, PERMISSIONS.REQUEST_LOG_READ)
    || !REQUEST_LOG_NAVIGATION_IDS.has(activeNavigation)
    || refreshId !== refreshSequence
    || !owner.identity
    || !owner.tenantId
  ) {
    return null;
  }

  if (
    activeNavigation === "output-safety"
    && requestLogPageMemory.query().filters.terminalStage !== "output_safety"
  ) {
    // 首次进入或从普通日志切换时冻结安全阶段；旧 SavedFilter 和手写 hash 均不能移除该条件。
    requestLogPageMemory.configure({
      identity: owner.identity,
      tenantId: owner.tenantId,
      navigation: owner.navigation,
      nextFilters: {
        ...requestLogPageMemory.query().filters,
        terminalStage: "output_safety",
        selectedSavedFilterId: "",
        offset: 0,
      },
      nextSelectedRequestId: null,
    });
  }

  const ticket = requestLogPageMemory.beginList(owner);
  if (!ticket) {
    return null;
  }
  const token = authSession.token;
  try {
    // 两个 GET 共用同一 list ticket；任一迟到响应都必须通过完整身份和 refresh 代次。
    const savedFiltersPromise = listSavedRequestLogFilters({
      controlApiUrl,
      token,
      signal: ticket.signal,
    }).then(
      (value) => ({ value }),
      (error) => ({ error }),
    );
    const state = await listRequestLogs({
      controlApiUrl,
      token,
      advancedFilters: ticket.filters,
      savedFilterId: ticket.filters.selectedSavedFilterId,
      sortBy: ticket.filters.sortBy,
      sortDirection: ticket.filters.sortDirection,
      limit: ticket.filters.limit,
      offset: ticket.filters.offset,
      signal: ticket.signal,
    });
    const savedFiltersResult = await savedFiltersPromise;
    const currentOwner = currentRequestLogOwner(refreshId, activeNavigation);
    if (!requestLogPageMemory.isCurrentList(ticket, currentOwner)) {
      return null;
    }
    // 保存筛选不可用时允许日志主列表降级展示，但统一错误必须留在当前 owner 页面。
    if (savedFiltersResult.error) {
      // 认证失效不能按普通 SavedFilter 降级处理，必须交给外层统一销毁 Session 和租户内存。
      if (savedFiltersResult.error instanceof AuthApiError && savedFiltersResult.error.status === 401) {
        // 抛给现有 RequestLog 401 收口分支，避免两套登出实现产生清理差异。
        throw savedFiltersResult.error;
      }
      requestLogPageMemory.acceptSavedFiltersError(
        ticket,
        safeRequestLogClientError(savedFiltersResult.error, "保存筛选读取失败"),
        currentOwner,
      );
    } else {
      requestLogPageMemory.acceptSavedFilters(ticket, savedFiltersResult.value, currentOwner);
    }
    if (!requestLogPageMemory.acceptList(ticket, state, currentOwner)) {
      // 同租户校验失败说明响应不完整；固定错误不得携带旧列表或原始响应正文。
      requestLogPageMemory.acceptListError(ticket, {
        code: "request_log.response_invalid",
        message: "调用日志响应不完整",
        requestId: state?.request_id ?? "-",
      }, currentOwner);
      return requestLogPageMemory.snapshot();
    }

    const detailTicket = requestLogPageMemory.beginDetail(ticket, currentOwner);
    if (!detailTicket) {
      return requestLogPageMemory.snapshot();
    }
    try {
      const detail = await getRequestLog({
        controlApiUrl,
        token,
        requestId: detailTicket.requestId,
        signal: detailTicket.signal,
      });
      const detailOwner = currentRequestLogOwner(refreshId, activeNavigation);
      if (!requestLogPageMemory.isCurrentDetail(detailTicket, detailOwner)) {
        return null;
      }
      if (!requestLogPageMemory.acceptDetail(detailTicket, detail, detailOwner)) {
        requestLogPageMemory.acceptDetailError(detailTicket, {
          code: "request_log.response_invalid",
          message: "调用日志详情响应不完整",
          requestId: detail?.request_id ?? detailTicket.requestId,
        }, detailOwner);
      } else {
        // 详情资格和已有反馈只在 RequestLog owner 接纳后进入 PAGE-070；定时刷新不会覆盖 dirty 或取消独立 POST。
        if (activeNavigation === "request-logs") {
          requestFeedbackMutationMemory.syncDetail({
            identity: detailOwner.identity,
            navigation: "request-logs",
            detail,
          });
        }
      }
      return requestLogPageMemory.snapshot();
    } catch (error) {
      const detailOwner = currentRequestLogOwner(refreshId, activeNavigation);
      // Abort 或完整 owner 已改变都属于主动生命周期收口，不能展示为新租户错误。
      if (
        isAbortError(error)
        || !requestLogPageMemory.isCurrentDetail(detailTicket, detailOwner)
      ) {
        return null;
      }
      // 详情返回 401 表示认证边界已经失效，必须清除令牌及全部租户内 RequestLog 状态后再回登录页。
      if (error instanceof AuthApiError && error.status === 401) {
        clearRequestLogSessionState();
        clearAuthSession();
        authSession = { token: null, context: null };
        renderLogin(authErrorMessage(error), error.requestId);
        return null;
      }
      requestLogPageMemory.acceptDetailError(
        detailTicket,
        safeRequestLogClientError(error, "调用日志详情读取失败", detailTicket.requestId),
        detailOwner,
      );
      return requestLogPageMemory.snapshot();
    }
  } catch (error) {
    const currentOwner = currentRequestLogOwner(refreshId, activeNavigation);
    // list 请求的 Abort/owner 失效同样静默丢弃，不能恢复上一轮列表或清理新会话。
    if (isAbortError(error) || !requestLogPageMemory.isCurrentList(ticket, currentOwner)) {
      return null;
    }
    // 列表返回 401 与详情一致地销毁认证和页面 owner，避免旧租户内存被下一次登录复用。
    if (error instanceof AuthApiError && error.status === 401) {
      clearRequestLogSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    requestLogPageMemory.acceptListError(
      ticket,
      safeRequestLogClientError(error, "调用日志读取失败", ticket.filters.requestId || "-"),
      currentOwner,
    );
    return requestLogPageMemory.snapshot();
  }
}

/**
 * 仅在 admin 且后端导航确认当前项为 audit-logs 时读取管理审计。
 *
 * 不能只检查 location.hash：手写 hash 尚未经过后端角色化导航校验；也不能只检查角色，
 * 否则 15 秒定时刷新会在任意页面后台预取敏感治理摘要。
 */
async function loadAuditLogsState(consoleShellState, refreshId) {
  const hashNavigation = readActiveNavigationHash();
  if (
    refreshId !== refreshSequence ||
    !hasPermission(authSession.context, PERMISSIONS.AUDIT_READ) ||
    consoleShellState?.activeNavigationItemId !== "audit-logs" ||
    (hashNavigation && hashNavigation !== "audit-logs")
  ) {
    return null;
  }
  try {
    const state = await listAuditLogs({
      controlApiUrl,
      token: authSession.token,
      actorUserId: auditLogFilters.actorUserId,
      action: auditLogFilters.action,
      objectType: auditLogFilters.objectType,
      objectId: auditLogFilters.objectId,
      requestId: auditLogFilters.requestId,
      createdFrom: auditLogFilters.createdFrom,
      createdTo: auditLogFilters.createdTo,
      limit: auditLogFilters.limit ?? 20,
      offset: auditLogFilters.offset ?? 0,
    });
    return {
      ...state,
      filters: auditLogFilters,
      exportState: { ...auditExportState },
    };
  } catch (error) {
    // 401 改变认证边界，必须清除本地会话；其他错误在审计页面原位展示 code/request_id。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuditExportSessionState();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      code: error?.code ?? "audit_log.query_failed",
      requestId: error?.requestId ?? "-",
      filters: auditLogFilters,
      exportState: { ...auditExportState },
    };
  }
}

/**
 * 执行 控制台主应用 的 `loadVirtualKeysState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadVirtualKeysState() {
  if (!hasPermission(authSession.context, PERMISSIONS.VIRTUAL_KEY_READ)) {
    return null;
  }
  try {
    const state = await listVirtualKeys({ controlApiUrl, token: authSession.token });
    return {
      ...state,
      created: virtualKeyCreatedSecret,
      actionError: virtualKeyActionError,
      feedback: virtualKeyFeedback,
    };
  } catch (error) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (error instanceof AuthApiError && error.status === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    return {
      state: "error",
      message: authErrorMessage(error),
      requestId: error?.requestId ?? "-",
    };
  }
}
