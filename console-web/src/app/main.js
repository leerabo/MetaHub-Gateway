/** 读取 Host-scoped 公开品牌；不接受 tenant slug，也不构造 owner 查询。 */
async function loadPublicBrandingPreview(generation, hydrate) {
  const ownerGeneration = Number(generation);
  try {
    const result = await getHostPublicBranding({ controlApiUrl: workspaceControlApiUrl });
    if (ownerGeneration !== publicBrandingGeneration || hasWorkspaceConsoleSession(authSession)) return false;
    publicBrandingLogoUrl = result.branding.logo_path ?? "";
    publicBrandingPreview = result.branding;
    applyPublicBrandingDocument(result.branding);
    if (hydrate) hydrateLoginBranding();
    return true;
  } catch {
    // 公开品牌解析不能阻断登录；固定默认品牌避免把上游异常正文暴露到登录页。
    return false;
  }
}

/** 将公开品牌投影写入现有登录 DOM，不重建表单，避免清除用户正在输入的密码。 */
function hydrateLoginBranding() {
  if (hasWorkspaceConsoleSession(authSession) || !globalThis.document) return;
  const branding = publicBrandingPreview;
  const productName = String(branding?.product_name ?? "MetaHub");
  const loginTitle = String(branding?.login_title ?? "MetaHub 控制台登录");
  const message = String(branding?.login_message ?? "");
  const brand = document.querySelector(".login-brand");
  const eyebrow = brand?.querySelector(".eyebrow");
  const title = document.querySelector("#login-title");
  const messageNode = document.querySelector("[data-login-brand-message]");
  if (eyebrow) eyebrow.textContent = productName;
  if (title) title.textContent = loginTitle;
  if (messageNode) {
    messageNode.textContent = message;
    messageNode.hidden = !message;
  }
  const mark = brand?.querySelector(".brand-mark");
  if (!mark) return;
  mark.replaceChildren();
  const logoUrl = publicBrandingLogoUrl;
  if (branding?.logo_path && logoUrl) {
    mark.classList.add("brand-mark-image");
    mark.removeAttribute("aria-hidden");
    const image = document.createElement("img");
    image.className = "brand-logo";
    image.alt = productName;
    image.src = logoUrl;
    mark.append(image);
  } else {
    mark.classList.remove("brand-mark-image");
    mark.setAttribute("aria-hidden", "true");
    mark.innerHTML = "<span></span><span></span><span></span>";
  }
}

/** Public-brand document effects are restricted to validated colors and content-addressed paths. */
function applyPublicBrandingDocument(branding) {
  const root = globalThis.document?.documentElement;
  if (!root || !branding) return;
  root.style.setProperty("--branding-primary", branding.primary_color);
  root.style.setProperty("--branding-secondary", branding.secondary_color);
  document.title = `${branding.product_name} AI Gateway`;
  const favicon = document.querySelector("link[rel='icon']");
  if (favicon) favicon.setAttribute("href", branding.favicon_path ?? "data:,");
}

/** 登录边界不继承上一 Host 的公开品牌。 */
function clearPublicBrandingPreview() {
  publicBrandingGeneration += 1;
  publicBrandingLogoUrl = "";
  publicBrandingPreview = null;
}

/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/src/app/main.js
 */

import {
  isConsoleNavigationOwnerCurrent,
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
import { bindFileSecurityInteractions } from "../features/file-security/bindFileSecurity.js";
import {
  createFileSecurityPageState,
  fileSecurityPageOwner,
  fileSecurityReadIdentitySnapshot,
  isFileSecurityAbortError,
} from "../features/file-security/fileSecurityPageState.js";
import {
  bindBatchJobInteractions,
  clearBatchResultDom,
  hydrateBatchResultText,
} from "../features/batch-jobs/bindBatchJobs.js";
import {
  batchJobPageOwner,
  batchReadIdentitySnapshot,
  createBatchJobPageState,
  isBatchAbortError,
} from "../features/batch-jobs/batchJobPageState.js";
import {
  bindMcpToolInteractions,
  clearMcpToolDom,
} from "../features/mcp-tools/bindMcpTools.js";
import {
  createMcpToolPageState,
  isMcpToolAbortError,
  mcpToolPageOwner,
  mcpToolReadIdentitySnapshot,
} from "../features/mcp-tools/mcpToolPageState.js";
import { renderMcpTools } from "../features/mcp-tools/renderMcpTools.js";
import {
  bindLocalEndpointInteractions,
  clearLocalEndpointDom,
} from "../features/local-models/bindLocalEndpoints.js";
import {
  createLocalEndpointPageState,
  isLocalEndpointAbortError,
  localEndpointPageOwner,
  localEndpointReadIdentitySnapshot,
} from "../features/local-models/localEndpointPageState.js";
import { renderLocalEndpoints } from "../features/local-models/renderLocalEndpoints.js";
import {
  bindPrivateGatewayInteractions,
  clearPrivateGatewayDom,
} from "../features/private-gateways/bindPrivateGateways.js";
import {
  createPrivateGatewayPageState,
  isPrivateGatewayAbortError,
  privateGatewayPageOwner,
  privateGatewayReadIdentitySnapshot,
} from "../features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../features/private-gateways/renderPrivateGateways.js";
import { bindBrandEditorInteractions } from "../features/brand-domains/bindBrandEditor.js";
import { createDeploymentBrandingController } from "../features/brand-domains/deploymentBrandingController.js";
import {
  bindMigrationOperationInteractions,
  clearMigrationOperationDom,
} from "../features/migration-operations/bindMigrationOperations.js";
import {
  createMigrationOperationPageState,
  isMigrationOperationAbortError,
  migrationOperationPageOwner,
  migrationOperationReadIdentitySnapshot,
} from "../features/migration-operations/migrationOperationPageState.js";
import { renderMigrationOperations } from "../features/migration-operations/renderMigrationOperations.js";
import {
  bindSupportTicketInteractions,
  clearSupportTicketDom,
} from "../features/support-tickets/bindSupportTickets.js";
import {
  createSupportTicketPageState,
  isSupportTicketAbortError,
  supportTicketPageOwner,
  supportTicketReadIdentitySnapshot,
} from "../features/support-tickets/supportTicketPageState.js";
import { renderSupportTickets } from "../features/support-tickets/renderSupportTickets.js";
import {
  bindExperimentInteractions,
  clearExperimentDom,
} from "../features/experiments/bindExperiments.js";
import {
  createExperimentPageState,
  experimentPageOwner,
  experimentReadIdentitySnapshot,
  isExperimentAbortError,
} from "../features/experiments/experimentPageState.js";
import { renderExperiments } from "../features/experiments/renderExperiments.js";
import {
  bindResponseCacheInteractions,
  clearResponseCacheDom,
} from "../features/cache-policies/bindResponseCache.js";
import {
  createResponseCachePageState,
  isResponseCacheAbortError,
  responseCachePageOwner,
  responseCacheReadIdentitySnapshot,
} from "../features/cache-policies/responseCachePageState.js";
import { renderResponseCache } from "../features/cache-policies/renderResponseCache.js";
import {
  bindWorkspaceQuotaInteractions,
  clearWorkspaceQuotaDom,
} from "../features/workspace-quota/bindWorkspaceQuota.js";
import {
  createQuotaPageState,
  isWorkspaceQuotaAbortError,
  quotaPageOwner,
  quotaReadIdentitySnapshot,
} from "../features/workspace-quota/workspaceQuotaPageState.js";
import { renderWorkspaceQuota } from "../features/workspace-quota/renderWorkspaceQuota.js";
import { createWorkspaceConsoleExperienceController } from "../features/workspace-console-experience/workspaceConsoleExperienceController.js";
import {
  bindWorkspaceConsoleExperience,
  hydrateWorkspaceConsoleAssistant,
  renderWorkspaceConsoleExperience,
} from "../features/workspace-console-experience/renderWorkspaceConsoleExperience.js";
import { bindWebhookDeadLetterInteractions } from "../features/webhook-dead-letters/bindWebhookDeadLetters.js";
import {
  createWebhookDeadLetterPageState,
  isWebhookDeadLetterAbortError,
  webhookDeadLetterIdentitySnapshot,
} from "../features/webhook-dead-letters/webhookDeadLetterPageState.js";
import { renderWebhookDeadLetters } from "../features/webhook-dead-letters/renderWebhookDeadLetters.js";
import { applyWebhookFormatMode } from "../features/webhooks/renderWebhookPanel.js";
import { renderWorkspaceSessionPrompt } from "../features/auth/renderLoginPage.js";
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
  createProviderStatusPageState,
  providerStatusFilterKey,
  providerStatusIdentitySnapshot,
} from "../features/provider-health/providerStatusPageState.js";
import { createI18n, DEFAULT_LOCALE } from "../i18n/messages.js";
import {
  AuthApiError,
  clearAuthSession,
} from "../lib/auth.js";
import {
  hasWorkspaceConsoleSession,
  isConsoleRefreshOwnerCurrent,
  sameWorkspaceConsoleSession,
  workspaceConsoleSessionFromShell,
} from "../lib/workspaceConsoleSession.js";
import { createWorkspaceResourcePage, workspaceResourcePageOwner } from "../lib/workspaceResourcePage.js";
import {
  downloadDeveloperSdk,
  getDeveloperContractWorkspace,
} from "../lib/developerContracts.js";
import { getDeveloperSupplyChainSecurity } from "../lib/supplyChainSecurity.js";
import {
  authorizationIdentitySnapshot,
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
import {
  bindNavigationHashRefresh,
  parseNavigationHash,
  recordConsolePageView,
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
import { getTenantOnboardingSummary } from "../lib/onboarding.js";
import { getDeveloperQuickstartData } from "../lib/developerQuickstart.js";
import {
  downloadJavascriptSdkExample,
  downloadPythonSdkExample,
  getJavascriptSdkExample,
  getPythonSdkExample,
} from "../lib/pythonSdkExamples.js";
import { createDlpRule, listDlpRules, updateDlpRule } from "../lib/dlp.js";
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
import { getCurrentTenant, updateCurrentTenant, getWorkspaceProfile, updateWorkspaceProfile, reenterWorkspaceProfile } from "../lib/tenant.js";
import {
  createTenantDataIdempotencyKey,
  createTenantExport,
  downloadTenantExport,
  getTenantExport,
  getTenantDataManagement,
  isTenantDataAbortError,
  requestTenantDeletion,
} from "../lib/tenantDataLifecycle.js";
import { bindTenantDataLifecycleInteractions } from "../features/tenant-data-lifecycle/bindTenantDataLifecycle.js";
import { renderTenantDataLifecycle } from "../features/tenant-data-lifecycle/renderTenantDataLifecycle.js";
import {
  createTenantDataLifecyclePageState,
  tenantDataLifecycleOwner,
} from "../features/tenant-data-lifecycle/tenantDataLifecyclePageState.js";
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
  downloadFileSecurityReportCsv,
  getFileSecurityReport,
  listFileSecurityFiles,
  scanFileSecurity,
  triggerFileSecurityReportCsvDownload,
} from "../lib/fileSecurity.js";
import {
  cancelBatchJob,
  createBatchJob,
  getBatchJob,
  getBatchTaskResult,
  listBatchJobs,
  listBatchTasks,
} from "../lib/batchJobs.js";
import {
  getMcpTool,
  listMcpInvocations,
  listMcpServers,
  listMcpTools,
  refreshMcpServerTools,
  registerMcpServer,
  updateMcpServerNetworkPolicy,
  updateMcpServerStatus,
  updateMcpToolPolicy,
} from "../lib/mcpTools.js";
import {
  createLocalEndpoint,
  listLocalEndpoints,
  testLocalEndpoint,
} from "../lib/localEndpoints.js";
import {
  acceptEdgeDelivery,
  createEdgeIdempotencyKey,
  createEdgeRegistration,
  decideEdgeDesiredState,
  edgeInstallerArtifactUrl,
  getEdgeDesiredState,
  getEdgeSupportBundle,
  getEdgeTelemetryWatermark,
  getEdgeValidation,
  listEdgeDeployments,
  listEdgeInstallers,
  listEdgeSecretReferences,
  patchEdgeTelemetryPolicy,
  publishEdgeDesiredState,
  rollbackEdgeDesiredState,
  revokeEdgeDeployment,
  startEdgeValidation,
  workspaceCookieApiOrigin,
} from "../lib/edgeManagement.js";
import {
  decideWorkspaceDiagnosticGrant,
  getWorkspaceEdgeHealth,
} from "../lib/edgeDiagnostics.js";
import {
  getWorkspaceUpgrade,
  requestWorkspaceRollback,
  updateWorkspaceMaintenance,
} from "../lib/edgeUpgrades.js";
import {
  getWorkspaceLicense,
  refreshWorkspaceLicense,
} from "../lib/edgeLicenses.js";
import {
  getWorkspaceBackupStatus,
  mutateWorkspaceRestoreDrill,
} from "../lib/edgeBackups.js";
import { getWorkspaceOfflineBundleState } from "../lib/edgeOfflineBundles.js";
import {
  createMigrationAssessment,
  createMigrationIdempotencyKey,
  createMigrationRun,
  cutoverMigrationRun,
  getMigrationDifferences,
  rollbackMigrationRun,
} from "../lib/migrationOperations.js";
import {
  attachSupportFile,
  createSupportIdempotencyKey,
  createSupportTicket,
  escalateSupportTicket,
  listSupportTickets,
  patchSupportTicket,
} from "../lib/customerSupport.js";
import {
  approveLocalSupportBundle,
  cancelLocalSupportBundle,
  clearLocalAdminSession,
  createLocalAdminSession,
  createLocalBackup,
  createLocalSecret,
  deleteLocalAdminSession,
  deleteLocalSecret,
  previewLocalSupportBundle,
  rotateLocalSecret,
  restoreLocalBackup,
  testLocalProviderConnection,
} from "../lib/localSecretManagement.js";
import {
  IdentityApiError,
  beginIdentityWebAuthnStepUp,
  createIdentityStepUpGrant,
  getIdentityMe,
  getWebAuthnAssertion,
  logoutIdentity,
} from "../lib/identity.js";
import { getCurrentWorkspaceSession } from "../lib/workspaces.js";
import {
  WORKSPACE_ROUTE_REGISTRY,
  WORKSPACE_SOURCE_ROUTE_REGISTRY,
  createWorkspaceNavigationIdempotencyKey,
  getWorkspaceAdvancedTools,
  getWorkspaceGettingStarted,
  getWorkspaceNavigation,
  openWorkspaceAdvancedTool,
  projectWorkspaceNavigationForConsole,
  recordWorkspacePageVisit,
  resolveWorkspaceNavigationRoute,
  updateWorkspaceNavigationPreference,
} from "../lib/workspaceNavigation.js";
import { createWorkspaceNavigationController } from "../features/workspace-navigation/workspaceNavigationController.js";
import { createWorkspacePageVisitTracker } from "../lib/workspacePageVisitTracker.js";
import {
  renderWorkspaceAdvancedTools,
} from "../features/workspace-navigation/renderWorkspaceNavigation.js";
import {
  createBrandingIdempotencyKey,
  getDeploymentBranding,
  getPublicBranding as getHostPublicBranding,
  updateDeploymentBranding,
} from "../lib/brandDomains.js";
import {
  createExperiment,
  getExperiment,
  getExperimentResults,
  listExperiments,
} from "../lib/experiments.js";
import {
  createResponseCacheIdempotencyKey,
  invalidateResponseCache,
  listResponseCachePolicies,
  updateResponseCachePolicy,
} from "../lib/responseCache.js";
import {
  createQuotaAdjustment,
  getQuotaBuckets,
} from "../lib/workspaceQuota.js";
import {
  getWorkspaceConsoleOverview,
  getWorkspaceDeploymentSummary,
  listWorkspaceConsoleApprovals,
  queryWorkspaceConsoleAssistant,
} from "../lib/workspaceConsoleExperience.js";
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
import { createUser, listWorkspaceMembers, updateUserRole, updateWorkspaceMember } from "../lib/users.js";
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
  createProviderCatalogIdempotencyKey,
  createProviderFromCatalog,
  getProviderCatalogCompatibility,
  listProviderCatalog,
  preflightCatalogProvider,
} from "../lib/providerCatalog.js";
import {
  applyWorkspaceProviderErrorOverrideCommand,
  listWorkspaceProviderErrorOverrides,
} from "../lib/providerEngineering.js";
import { createWorkspaceProviderEngineeringController } from "../features/workspace-provider-engineering/workspaceProviderEngineeringController.js";
import {
  renderWorkspaceProviderEngineering,
  renderWorkspaceProviderErrorMaps,
} from "../features/workspace-provider-engineering/renderWorkspaceProviderEngineering.js";
import {
  applyWorkspaceProviderCostRateCommand,
  createWorkspaceCommercialDispute,
  getWorkspaceCommercialSubscription,
  listWorkspaceProviderCostRates,
} from "../lib/workspaceCommercialAccounting.js";
import { createWorkspaceCommercialAccountingController } from "../features/workspace-commercial-accounting/workspaceCommercialAccountingController.js";
import {
  renderWorkspaceInternalFinOps,
  renderWorkspaceSubscription,
} from "../features/workspace-commercial-accounting/renderWorkspaceCommercialAccounting.js";
import {
  createVirtualKey,
  disableVirtualKey,
  listVirtualKeys,
  rotateVirtualKey,
  updateVirtualKey,
  updateVirtualKeyLimits,
} from "../lib/virtualKeys.js";
import { createConsumer, listConsumers, updateConsumer } from "../lib/consumers.js";
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
// Gateway traffic has no Workspace Cookie; Control must retain the page's host.
const localLoopback = globalThis.window?.location?.hostname === "localhost";
const gatewayUrl = localLoopback ? "http://127.0.0.1:8000" : "http://localhost:8000";
const controlPage = globalThis.window?.location;
const controlApiUrl = workspaceCookieApiOrigin(
  !controlPage?.origin || new Set(["localhost", "127.0.0.1", "::1", "[::1]"]).has(controlPage.hostname)
    ? `${controlPage?.protocol ?? "http:"}//${controlPage?.hostname ?? "localhost"}:8001`
    : controlPage.origin,
);
const workspaceControlApiUrl = controlApiUrl;
const consoleUrl = globalThis.window?.location?.origin ?? "";
// 公开邀请 fragment 必须先于认证 Storage 读取同步截获并清除，避免既有 Session 污染 accept 请求。
const publicInvitationRoute = globalThis.window
  ? captureInvitationRoute(globalThis.window)
  : null;
let authSession = { context: null, owner: null };
let resourcePageShellState = null;
let workspacePageVisitShellState = null;
let workspacePageVisitSource = "direct_hash";
const workspacePageVisitTracker = createWorkspacePageVisitTracker({
  current: () => ({ session: authSession, shell: workspacePageVisitShellState,
    hash: readActiveNavigationHash(),
    renderedNavigation: document.querySelector(".console-shell")?.dataset.activeNavigation }),
  recordVisit: (options) => recordWorkspacePageVisit({ controlApiUrl: workspaceControlApiUrl, ...options }),
  createIdempotencyKey: createWorkspaceNavigationIdempotencyKey,
  onError: (error) => {
    if (error?.status === 401) renderLogin(authErrorMessage(error), error.requestId);
    else if ([403, 409, 412].includes(error?.status)) void refresh();
  },
});
const RESOURCE_NAVIGATION_IDS = new Set(["sdk-examples", "tenant", "users", "environments", "projects", "providers", "models", "virtual-keys", "files", "provider-health",
  "model-aliases", "routing-policies", "model-quality-scores", "experiments",
  "request-logs", "request-explanations", "output-safety", "audit-logs", "feedback-summary"]);
const resourcePageLifecycle = createWorkspaceResourcePage({ current: () => ({
  session: authSession, shell: resourcePageShellState, hash: readActiveNavigationHash(),
  refreshGeneration: refreshSequence,
}) });
let consoleLogoutPending = false;
let workspaceMembersOffset = 0;
let workspaceMembersFeedback = "";
let workspaceMembersActionError = null;
let consoleShellActionError = null;
let publicBrandingPreview = null;
let publicBrandingGeneration = 0;
let publicBrandingLogoUrl = "";
// 当前 i18n 上下文供 PAGE-070 局部重绘复用；身份清除时会回到默认中文，避免沿用上一用户偏好。
let currentI18n = createI18n(DEFAULT_LOCALE);
let gatewayDebugState = {};
// 0138 分组状态只来自当前 Workspace 的服务端偏好；默认全部折叠。
let expandedConsoleGroupKey = null;
let sidebarGroupSequence = 0;
let workspaceAdvancedToolsDialogState = null;
let workspaceAdvancedToolsDialogController = null;
let workspaceAdvancedToolsDialogTrigger = null;
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
let providerActionError = null;
let providerFeedback = "";
let providerLastTestResult = null;
let providerCatalogActiveTab = "catalog";
let providerCatalogFilters = { status: "", vendorCode: "", capabilityCode: "", regionCode: "" };
let providerCatalogActionError = null;
let providerCatalogFeedback = "";
let providerCatalogCompatibilityByEntryCode = {};
let providerCatalogLastCreated = null;
let providerCatalogLastPreflight = null;
let providerCatalogCreateIntent = null;
let providerCatalogPreflightIntent = null;
// PAGE-088 筛选只驻留当前页面内存；tenant_id、窗口和 Endpoint 永不进入浏览器筛选状态。
let providerStatusFilters = { limit: 50, offset: 0 };
let providerHealthActionError = null;
let providerHealthProbingProviderId = null;
let providerHealthRequestGeneration = 0;
let providerHealthController = null;
// Provider 列表及其 Key 读取绑定当前刷新；导航或注销时必须先取消，避免失效 Token 继续请求。
let workspaceProviderEngineeringProviders = [];
const WORKSPACE_PROVIDER_ENGINEERING_NAVIGATION_IDS = new Set(["providers", "error-maps"]);
const workspaceProviderEngineeringController = createWorkspaceProviderEngineeringController({
  api: {
    workspaceSession: (input) => getCurrentWorkspaceSession({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    providerErrorOverrides: (input) => listWorkspaceProviderErrorOverrides({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    commandProviderErrorOverride: (input) => applyWorkspaceProviderErrorOverrideCommand({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    beginIdentityStepUp: (input) => beginIdentityWebAuthnStepUp({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    createIdentityStepUpGrant: (input) => createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    getWebAuthnAssertion,
  },
  onChange: renderCurrentWorkspaceProviderEngineering,
  onSessionInvalid() {
    if (authSession.token) void refresh();
  },
  onPermissionRevoked() {
    if (authSession.token) void refresh();
  },
});
const workspaceNavigationController = createWorkspaceNavigationController({
  api: {
    identitySession: (input) => getIdentityMe({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    workspaceSession: (input) => getCurrentWorkspaceSession({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    navigation: (input) => getWorkspaceNavigation({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    gettingStarted: (input) => getWorkspaceGettingStarted({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    advancedTools: (input) => getWorkspaceAdvancedTools({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    updatePreference: (input) => updateWorkspaceNavigationPreference({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    openAdvancedTool: (input) => openWorkspaceAdvancedTool({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
  },
  createIdempotencyKey: createWorkspaceNavigationIdempotencyKey,
});
const WORKSPACE_COMMERCIAL_NAVIGATION_IDS = new Set([
  "workspace-subscription",
  "billing-disputes",
  "internal-finops",
  "model-prices",
]);
let workspaceCommercialNavigationItems = [];
const workspaceCommercialAccountingController = createWorkspaceCommercialAccountingController({
  api: {
    workspaceSession: (input) => getCurrentWorkspaceSession({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    workspaceSubscription: (input) => getWorkspaceCommercialSubscription({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    createDispute: (input) => createWorkspaceCommercialDispute({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    providerCostRates: (input) => listWorkspaceProviderCostRates({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    commandProviderCostRate: (input) => applyWorkspaceProviderCostRateCommand({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    beginIdentityStepUp: (input) => beginIdentityWebAuthnStepUp({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    createIdentityStepUpGrant: (input) => createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
    }),
    getWebAuthnAssertion,
  },
  onChange: renderCurrentWorkspaceCommercialAccounting,
  onSessionInvalid() {
    if (authSession.token) void refresh();
  },
  onPermissionRevoked() {
    if (authSession.token) void refresh();
  },
});
let consoleSidebarController = null;
let mobileSidebarCollapsed = true;
// 导航偏好和访问事件是低优先级写入；快速切页时只保留最后一页。
const CONSOLE_NAVIGATION_SIDE_EFFECT_DELAY_MS = 5_000;
let consoleNavigationSideEffectTimer = null;
// Provider 状态 GET 使用完整身份、导航、筛选、Token 与 generation 共同约束迟到响应。
const providerStatusPageState = createProviderStatusPageState();
let projectActionError = null;
let projectFeedback = "";
let environmentActionError = null;
let environmentFeedback = "";
// RequestLog 列表和详情共享完整身份 owner；登出、换租户或离页会同步 abort 并销毁全部事实。
const requestLogPageMemory = createRequestLogPageMemory();
// PAGE-080 与 PAGE-099 共用读取链路，但导航 ID 必须进入 owner，防止两页迟到响应互相覆盖。
const REQUEST_LOG_NAVIGATION_IDS = new Set(["request-logs", "output-safety"]);
// PAGE-079 使用独立 owner 和单次内存 handoff；源 request_id 永不进入 URL 或浏览器 Storage。
const requestExplanationPageMemory = createRequestExplanationPageMemory({ authentication: "workspace" });
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
// 每次刷新分配递增序号；较早刷新即使更晚返回，也不能覆盖用户已经切换的新页面。
let refreshSequence = 0;
let developerContractDownloadController = null;
let developerContractSecurityController = null;
let developerContractDownloadMetadata = new Map();
let consoleWorkspaceContextChannel = null;
// 完整控制台刷新包含多组租户接口，慢环境下可能超过定时周期；计数用于阻止定时器制造永不落地的刷新饥饿。
let activeRefreshCount = 0;
let tenantDataDeletionTrigger = null;
// 导航偏好、page-view 与目标刷新共同构成一次交互；定时器不得在中间推进 refreshSequence 淘汰用户点击。
let activeConsoleNavigationCount = 0;
// 定时器在同步调用 refresh 前写入一次性标记；performRefresh 立即消费，显式刷新不继承该 DOM 保留策略。
let nextRefreshIsBackground = false;
let workspaceProfileState = null;
let workspaceProfileMutation = null;
let workspaceProfileRecovery = null;
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
// PAGE-103 的 ready 文件名、严格报告、扫描/导出 ticket 只属于独立 file-security owner。
const fileSecurityPageState = createFileSecurityPageState();
// PAGE-104 的 Job/Task cursor、Mutation 与结果 ticket 属于完整 Batch owner；assistant 正文只驻留 DOM。
const batchJobPageState = createBatchJobPageState({
  clearResultDom: () => clearBatchResultDom(globalThis.document),
});
// PAGE-121 export/deletion status and confirmation are owned by the current tenant session only.
const tenantDataLifecyclePageState = createTenantDataLifecyclePageState();
// PAGE-114 的 Endpoint/Token、Schema Dialog、cursor 和异步 owner 只属于当前授权页面内存。
const mcpToolPageState = createMcpToolPageState({ clearDom: clearMcpToolDom });
// PAGE-116 的 URL/Token 只存在当前授权 Dialog；列表、创建和测试 ticket 均绑定独立 owner/generation。
const localEndpointPageState = createLocalEndpointPageState({ clearDom: clearLocalEndpointDom });
// PRD-120 registration token/challenge live only inside this owner and its transient Dialog DOM.
const privateGatewayPageState = createPrivateGatewayPageState({
  clearDom: clearPrivateGatewayDom,
  clearLocalSession(localApiOrigin) {
    if (localApiOrigin) void deleteLocalAdminSession({ localApiOrigin });
    else clearLocalAdminSession();
  },
});
let privateGatewayBrandingShellState = null;
const deploymentBrandingController = createDeploymentBrandingController({
  api: {
    deploymentBrand: (input) => getDeploymentBranding({
      controlApiUrl: workspaceControlApiUrl, ...input,
    }),
    updateDeploymentBrand: (input) => updateDeploymentBranding({
      controlApiUrl: workspaceControlApiUrl, ...input,
    }),
    beginIdentityStepUp: (input) => beginIdentityWebAuthnStepUp({
      controlApiUrl: workspaceControlApiUrl, ...input,
    }),
    createIdentityStepUpGrant: (input) => createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl, ...input,
    }),
    getWebAuthnAssertion,
  },
  newIdempotencyKey: createBrandingIdempotencyKey,
  onChange() {
    if (privateGatewayBrandingShellState) {
      renderCurrentPrivateGatewayPanel(privateGatewayBrandingShellState);
    }
  },
  onSessionInvalid(error) {
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin(authErrorMessage(error), error.requestId);
  },
  onPermissionRevoked() { globalThis.queueMicrotask?.(() => void refresh()); },
});
// PRD-131 assessment, differences, run, and step-up dialog facts never leave this owner closure.
const migrationOperationPageState = createMigrationOperationPageState({
  clearDom: clearMigrationOperationDom,
});
// PRD-132 subjects, messages, file references, SLA facts, and tickets belong only to this live Workspace owner.
const supportTicketPageState = createSupportTicketPageState({
  clearDom: clearSupportTicketDom,
});
// PAGE-105 的 cursor、详情、统计与创建幂等 owner 只驻留当前授权 Session 内存。
const experimentPageState = createExperimentPageState({ clearDom: clearExperimentDom });
// PAGE-106 的策略草稿、cursor、Dialog 和三条异步 lane 只属于完整 response-cache owner。
const responseCachePageState = createResponseCachePageState({ clearDom: clearResponseCacheDom });
// PAGE-110 的 Plan CAS 草稿和确认 Dialog 只属于完整 plan.read owner。
// PAGE-111 的 reason、幂等键和额度调整 ticket 只驻留完整 quota.read owner 的 Dialog 闭包。
const quotaManagementPageState = createQuotaPageState({ clearDom: clearWorkspaceQuotaDom });
let workspaceConsoleExperienceShellState = null;
const workspaceConsoleExperienceController = createWorkspaceConsoleExperienceController({
  api: {
    consoleOverview: (input) => getWorkspaceConsoleOverview({ controlApiUrl: workspaceControlApiUrl, ...input }),
    consoleAssistant: (input) => queryWorkspaceConsoleAssistant({ controlApiUrl: workspaceControlApiUrl, ...input }),
    consoleApprovals: (input) => listWorkspaceConsoleApprovals({ controlApiUrl: workspaceControlApiUrl, ...input }),
    deploymentSummary: (input) => getWorkspaceDeploymentSummary({ controlApiUrl: workspaceControlApiUrl, ...input }),
  },
  onChange(snapshot) {
    if (snapshot.status !== "draft") renderCurrentWorkspaceConsoleExperience();
  },
  onSessionInvalid(error) {
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin(authErrorMessage(error), error?.requestId);
  },
  onPermissionRevoked() {
    globalThis.queueMicrotask?.(() => {
      if (workspaceConsoleExperienceShellState) void refresh();
    });
  },
});
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
let consumerDirectoryOffset = 0;
let virtualKeyFeedback = "";
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
      clearBasicResourceSessionState({ clearDom: true });
      if (window.location.hash === "#accept-invitation") return;
      // 公开页离开无凭据哨兵时立即销毁 Token；重载后再由普通认证/导航 bootstrap 接管。
      publicInvitationRoute.leave();
      window.location.reload();
    }, { once: true });
    renderPublicInvitation();
  } else {
    bindConsoleWorkspaceContextInvalidation();
    window.addEventListener("hashchange", () => {
      workspaceConsoleExperienceShellState = null;
      workspacePageVisitTracker.leave();
      workspacePageVisitShellState = null;
      workspacePageVisitSource = "direct_hash";
      workspaceConsoleExperienceController.leave();
      // PAGE-088 额外监听同一 hash 边界，同步淘汰旧状态回调；通用监听仍负责重读后端导航。
      providerStatusPageState.clear();
      const nextItemId = readActiveNavigationHash();
      if (nextItemId !== "providers") {
        workspaceProviderEngineeringProviders = [];
        workspaceProviderEngineeringController.close();
      }
      if (!WORKSPACE_COMMERCIAL_NAVIGATION_IDS.has(nextItemId)) {
        workspaceCommercialNavigationItems = [];
        workspaceCommercialAccountingController.close();
      }
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
      if (nextItemId !== "file-security") {
        // PAGE-103 文件名、报告与扫描/导出 ticket 在 hash 离页时立即销毁，不能等待导航 API。
        fileSecurityPageState.clear();
      }
      if (nextItemId !== "batch-jobs") {
        // PAGE-104 必须先擦除可见结果正文，再取消列表、详情和写请求。
        batchJobPageState.clear();
      }
      if (nextItemId !== "mcp-tools") {
        // PAGE-114 的 Endpoint/Token 输入、Schema Dialog 和全部迟到 ticket 必须在 hash 离页时同步销毁。
        mcpToolPageState.clear();
      }
      if (nextItemId !== "local-models") {
        // PAGE-116 的 URL/Token DOM 和全部异步 owner 在 hash 离页时立即销毁。
        localEndpointPageState.clear();
      }
      if (nextItemId !== "private-gateways") {
        privateGatewayPageState.clear();
        privateGatewayBrandingShellState = null;
        deploymentBrandingController.clear();
      }
      if (nextItemId !== "migration-operations") {
        migrationOperationPageState.clear();
      }
      if (nextItemId !== "support-tickets") {
        supportTicketPageState.clear();
      }
      if (nextItemId !== "experiments") {
        // PAGE-105 的私有 cursor、创建幂等 owner 和统计 ticket 不得越过 hash 导航边界。
        experimentPageState.clear();
      }
      if (nextItemId !== "response-cache") {
        // PAGE-106 的策略草稿、Dialog、cursor 和幂等 owner 不得越过 hash 导航边界。
        responseCachePageState.clear();
      }
      if (nextItemId !== "quota-buckets") {
        // PAGE-111 的 reason、幂等键和调整 ticket 不能越过 hash 导航边界。
        quotaManagementPageState.clear();
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
    // 清理监听器必须先于通用 refresh 注册，确保任何 hashchange 都先销毁私有页面 owner。
    bindNavigationHashRefresh(window, refresh);
    // 浏览器关闭、刷新或离开页面时同步 abort 活动流并销毁全部一次性凭据。
    window.addEventListener("pagehide", () => {
      clearBasicResourceSessionState({ clearDom: true });
      refreshSequence += 1;
      authSession = { context: null, owner: null };
      closeConsoleWorkspaceContextInvalidation();
      gatewayDebugLifecycle.leave();
      invitationPageState.clear();
      clearUsageExportSessionState();
      clearAuditExportSessionState();
      costAnomalyPageState.clear();
      sessionRiskPageState.clear();
      filePageState.clear();
      fileSecurityPageState.clear();
      batchJobPageState.clear();
      tenantDataLifecyclePageState.clear();
      mcpToolPageState.clear();
      localEndpointPageState.clear();
      privateGatewayPageState.clear();
      migrationOperationPageState.clear();
      supportTicketPageState.clear();
      experimentPageState.clear();
      responseCachePageState.clear();
      quotaManagementPageState.clear();
      workspaceConsoleExperienceShellState = null;
      workspaceConsoleExperienceController.leave();
      workspaceNavigationController.leave();
      closeWorkspaceAdvancedToolsDialog({ restoreFocus: false });
      webhookDeadLetterPageState.clear();
      clearPolicyEditorSessionState();
      clearPolicySnapshotSessionState();
      clearPolicyDryRunSessionState();
      clearIntentSecuritySessionState();
      clearRoutingPolicySessionState();
      clearModelQualityScoreSessionState();
      clearRequestLogSessionState();
      clearRequestExplanationSessionState();
      clearRequestFeedbackSummarySessionState();
      migrationGuideVisitTracker.leave();
      providerStatusPageState.clear();
      providerStatusVisitTracker.leave();
      workspaceProviderEngineeringProviders = [];
      workspaceProviderEngineeringController.close();
      workspaceCommercialNavigationItems = [];
      workspaceCommercialAccountingController.close();
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
  clearAuthSession();
  await refresh();
}

/**
 * 执行 控制台主应用 的 `refresh` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function refresh(options = {}) {
  // 交互刷新仍允许覆盖更早结果；活动计数只供后台定时器判断，不能吞掉用户刚完成的操作反馈。
  activeRefreshCount += 1;
  try {
    return await performRefresh(options);
  } finally {
    activeRefreshCount -= 1;
  }
}

/** Only the exact non-sensitive envelope emitted by identity.html may invalidate Console state. */
export function isConsoleWorkspaceContextInvalidation(message) {
  return Boolean(
    message
    && typeof message === "object"
    && !Array.isArray(message)
    && Object.keys(message).length === 2
    && message.type === "context-invalidated"
    && Number.isSafeInteger(message.generation)
    && message.generation >= 1
  );
}

/** Subscribe the Console to Workspace Cookie changes made by another same-origin page. */
export function bindConsoleWorkspaceContextInvalidation({
  channelFactory = (name) => (
    typeof globalThis.BroadcastChannel === "function"
      ? new globalThis.BroadcastChannel(name)
      : null
  ),
  onInvalidated = handleConsoleWorkspaceContextInvalidation,
} = {}) {
  if (consoleWorkspaceContextChannel || typeof channelFactory !== "function"
    || typeof onInvalidated !== "function") return false;
  try {
    consoleWorkspaceContextChannel = channelFactory("metahub-workspace-context-v1");
  } catch {
    consoleWorkspaceContextChannel = null;
    return false;
  }
  if (!consoleWorkspaceContextChannel || typeof consoleWorkspaceContextChannel !== "object") {
    consoleWorkspaceContextChannel = null;
    return false;
  }
  consoleWorkspaceContextChannel.onmessage = (event) => {
    if (!isConsoleWorkspaceContextInvalidation(event?.data)) return;
    onInvalidated();
  };
  return true;
}

/** Fence old Cookie-scoped Edge work before any server-authoritative reload can begin. */
function handleConsoleWorkspaceContextInvalidation() {
  if (consoleLogoutPending) return;
  refreshSequence += 1;
  clearConsoleSessionState();
  authSession = { context: null, owner: null };
  app?.replaceChildren();
  void refresh();
}

export function closeConsoleWorkspaceContextInvalidation() {
  const channel = consoleWorkspaceContextChannel;
  consoleWorkspaceContextChannel = null;
  if (!channel) return;
  channel.onmessage = null;
  try { channel.close?.(); } catch {
    // Page teardown must continue even if the browser has already closed the channel.
  }
}

function broadcastConsoleWorkspaceContextInvalidation() {
  try {
    consoleWorkspaceContextChannel?.postMessage?.({
      type: "context-invalidated", generation: Math.max(1, refreshSequence),
    });
  } catch {
    // Local notification failure does not change the server logout result.
  }
}

/**
 * 执行单轮控制台数据读取和渲染。
 * refresh 外层必须用 finally 维护活动计数，确保认证失败或任一提前返回都不会永久关闭定时刷新。
 */
async function performRefresh(options = {}) {
  if (consoleLogoutPending) return;
  const refreshId = ++refreshSequence;
  resourcePageLifecycle.cancelReads();
  filePageState.cancelList();
  clearDeveloperContractSecurityLoad();
  const isBackgroundRefresh = nextRefreshIsBackground;
  nextRefreshIsBackground = false;
  // Profile revision changes invalidate the old session until explicit reentry completes.
  if ((workspaceProfileMutation && resourcePageLifecycle.isMutationCurrent(workspaceProfileMutation))
    || (workspaceProfileRecovery && resourcePageLifecycle.isBindingCurrent(workspaceProfileRecovery.binding))) return;
  let refreshSession = authSession;
  const refreshOwnerCurrent = () => isConsoleRefreshOwnerCurrent(
    { generation: refreshId, session: refreshSession },
    { generation: refreshSequence, session: authSession },
  );
  const consoleShellState = await loadConsoleShellState(refreshOwnerCurrent);
  // 导航读取期间可能发生来源跳转、侧栏切换或新 Session 登录；旧 shell 快照不得清理新页面 owner。
  if (!refreshOwnerCurrent()) {
    return;
  }
  if (!consoleShellState) return;
  const nextSession = workspaceConsoleSessionFromShell(consoleShellState);
  if (!nextSession) {
    renderLogin(consoleShellState.error ? authErrorMessage(consoleShellState.error) : undefined,
      consoleShellState.error?.requestId);
    return;
  }
  const session = sameWorkspaceConsoleSession(authSession, nextSession) ? authSession : nextSession;
  if (authSession !== session) {
    clearConsoleSessionState({ clearNavigation: false });
    app?.replaceChildren();
  }
  authSession = session;
  refreshSession = session;
  const previousResourceNavigation = resourcePageLifecycle.navigation();
  resourcePageShellState = consoleShellState;
  if (resourcePageLifecycle.enter().changed) {
    clearBasicResourceFeedback();
    if (RESOURCE_NAVIGATION_IDS.has(previousResourceNavigation)) app?.replaceChildren();
  }
  if (resourcePageLifecycle.blocksReplacement(isBackgroundRefresh)) return;
  currentI18n = createI18n(consoleShellState.locale);
  const providerEngineeringSnapshot = workspaceProviderEngineeringController.snapshot();
  if (!WORKSPACE_PROVIDER_ENGINEERING_NAVIGATION_IDS.has(
    consoleShellState.activeNavigationItemId,
  ) || (providerEngineeringSnapshot.activeNavigationItemId
    && providerEngineeringSnapshot.activeNavigationItemId
      !== consoleShellState.activeNavigationItemId)) {
    workspaceProviderEngineeringProviders = [];
    workspaceProviderEngineeringController.close();
  }
  if (!WORKSPACE_COMMERCIAL_NAVIGATION_IDS.has(consoleShellState.activeNavigationItemId)) {
    workspaceCommercialNavigationItems = [];
    workspaceCommercialAccountingController.close();
  }
  consoleShellState.sidebarExpandedGroupKey = expandedConsoleGroupKey;
  consoleShellState.mobileSidebarCollapsed = mobileSidebarCollapsed;
  if (!hasWorkspaceConsoleSession(authSession) || !refreshOwnerCurrent()) {
    return;
  }
  if (consoleShellState?.activeNavigationItemId !== "enterprise-chat") {
    // 导航裁剪、非法 hash 或离页必须在任何其他租户读取前销毁 Chat 正文与在途回调。
    clearEnterpriseChatSessionState();
  }
  if (consoleShellState?.activeNavigationItemId !== "prompt-templates") {
    // 导航裁剪、非法 hash 或降权先清 PAGE-094 正文/草稿，其他页面绝不后台预取模板定义。
    clearPromptTemplateSessionState();
  }
  const promptTemplateState = null;
  if (consoleShellState?.activeNavigationItemId !== "policy-templates") {
    // PAGE-095 的绑定、nonce 和双 CAS 只属于当前授权页面，其他导航绝不后台预取或保留。
    clearPolicyTemplateLibrarySessionState();
  }
  const policyTemplateLibraryState = null;
  // PAGE-088 owner 只属于后端确认的 provider-health 导航；离页或权限裁剪后立即取消在途状态读取。
  if (consoleShellState?.activeNavigationItemId !== "provider-health") {
    providerStatusPageState.clear();
  }
  // 邀请弹窗必须同时属于当前 Session、授权指纹和后端确认的 users 导航；任一漂移立即清除密文。
  invitationPageState.syncOwner({
    authContext: authSession.context,
    token: null,
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
  // 调用日志只属于后端确认的 active 页面；离页先 abort list/detail，再销毁上一租户详情。
  if (!REQUEST_LOG_NAVIGATION_IDS.has(consoleShellState?.activeNavigationItemId)) {
    clearRequestLogSessionState();
  }
  // PAGE-079 状态只属于后端确认的解释入口；离页、导航裁剪或降权立即销毁源请求标识与响应。
  if (!["request-logs", "request-explanations"].includes(consoleShellState?.activeNavigationItemId)) {
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
  // PAGE-103 只属于后端确认的 file-security 导航；撤权或裁剪必须立即销毁文件名和报告。
  if (consoleShellState?.activeNavigationItemId !== "file-security") {
    fileSecurityPageState.clear();
  }
  // PAGE-104 只属于后端确认的 batch-jobs 导航；撤权或离页同步清正文并取消全部 ticket。
  if (consoleShellState?.activeNavigationItemId !== "batch-jobs") {
    batchJobPageState.clear();
  }
  if (consoleShellState?.activeNavigationItemId !== "tenant-data-lifecycle") {
    tenantDataLifecyclePageState.clear();
  }
  // PAGE-114 只属于后端确认的 mcp.read owner；Dialog 保留期间后台刷新只重验 auth/navigation。
  if (consoleShellState?.activeNavigationItemId !== "mcp-tools") {
    mcpToolPageState.clear();
  } else if (
    isBackgroundRefresh
    && mcpToolPageState.blocksShellDomReplacement()
    && mcpToolPageState.enter(currentMcpToolOwner(consoleShellState))
  ) {
    return;
  }
  // PAGE-116 同 owner Dialog 在后台鉴权刷新后保留；撤权、换租户或离页则先清敏感 DOM。
  if (consoleShellState?.activeNavigationItemId !== "local-models") {
    localEndpointPageState.clear();
  } else if (
    isBackgroundRefresh
    && localEndpointPageState.blocksShellDomReplacement()
    && localEndpointPageState.enter(currentLocalEndpointOwner(consoleShellState))
  ) {
    return;
  }
  if (consoleShellState?.activeNavigationItemId !== "private-gateways") {
    privateGatewayPageState.clear();
  } else if (
    isBackgroundRefresh
    && privateGatewayPageState.blocksShellDomReplacement()
    && privateGatewayPageState.enter(currentPrivateGatewayOwner(consoleShellState))
  ) {
    return;
  }
  if (consoleShellState?.activeNavigationItemId !== "migration-operations") {
    migrationOperationPageState.clear();
  } else if (
    isBackgroundRefresh
    && migrationOperationPageState.blocksShellDomReplacement()
    && migrationOperationPageState.enter(currentMigrationOperationOwner(consoleShellState))
  ) {
    return;
  }
  if (consoleShellState?.activeNavigationItemId !== "support-tickets") {
    supportTicketPageState.clear();
  } else if (
    isBackgroundRefresh
    && supportTicketPageState.blocksShellDomReplacement()
    && supportTicketPageState.enter(currentSupportTicketOwner(consoleShellState))
  ) {
    return;
  }
  // PAGE-105 只属于后端确认的 experiments 导航；离页或降权立即销毁 cursor、统计和创建 owner。
  if (consoleShellState?.activeNavigationItemId !== "experiments") {
    experimentPageState.clear();
  } else if (
    isBackgroundRefresh
    &&
    experimentPageState.blocksShellDomReplacement()
    && experimentPageState.enter(currentExperimentOwner(consoleShellState))
  ) {
    // 定时刷新已重新校验 Session、RBAC 指纹和后端导航；同 owner 草稿仅阻止 DOM 替换。
    return;
  }
  // PAGE-106 只属于后端确认的 response-cache 导航；owner 漂移先清 DOM/state，再允许新 owner fresh GET。
  if (consoleShellState?.activeNavigationItemId !== "response-cache") {
    responseCachePageState.clear();
  } else if (
    isBackgroundRefresh
    && responseCachePageState.blocksShellDomReplacement()
    && responseCachePageState.enter(currentResponseCacheOwner(consoleShellState))
  ) {
    // 后台已完成 auth/navigation 复核；同 owner 草稿、冲突或 Dialog 仅阻止普通 DOM replacement。
    return;
  }
  // PAGE-111 只属于后端确认的 quota-buckets 导航；busy/dirty Dialog 不得被 15 秒刷新覆盖。
  if (consoleShellState?.activeNavigationItemId !== "quota-buckets") {
    quotaManagementPageState.clear();
  } else if (
    isBackgroundRefresh
    && quotaManagementPageState.blocksShellDomReplacement()
    && quotaManagementPageState.enter(currentQuotaManagementOwner(consoleShellState))
  ) {
    return;
  }
  // PAGE-107 后台刷新仍先重读 auth/navigation；同 owner 有正文时保留 DOM，撤权则 leave 后继续失败关闭。
  // 上方多个页面 loader 都包含 await；离开旧导航后必须再次核对本轮 owner，禁止旧 overview 快照
  // 在新 PAGE-107 已经建立 owner 后调用 leave() 并移除刚渲染的根节点。
  if (!refreshOwnerCurrent()) {
    return;
  }
  // PAGE-083 只属于后端确认的 manager 导航；离页、hash 回退或降权立即取消 GET/POST 与轮询。
  if (consoleShellState?.activeNavigationItemId !== "webhook-dead-letters") {
    webhookDeadLetterPageState.clear();
  }
  // PAGE-085 矩阵和草稿只能属于后端仍授权的 active 导航；降权或离页立即销毁。
  if (consoleShellState?.activeNavigationItemId !== "role-permissions") {
    rolePermissionPageState.clear();
  }
  if (!hasWorkspaceConsoleSession(authSession)) {
    return;
  }
  await loadAndRenderActiveNavigation({
    consoleShellState,
    refreshId,
    isBackgroundRefresh,
    refreshOwnerCurrent,
  });
  return;
}

/**
 * Load only the backend facts consumed by the active, backend-authorized page.
 * Every loader retains its own RBAC, tenant, token, generation, and abort checks;
 * this dispatcher only narrows orchestration and never grants access.
 */
async function loadAndRenderActiveNavigation({
  consoleShellState,
  refreshId,
  isBackgroundRefresh,
  refreshOwnerCurrent,
  preserveConsoleSidebar = false,
}) {
  if (!refreshOwnerCurrent() || refreshId !== refreshSequence) return;
  const previousResourceNavigation = resourcePageLifecycle.navigation();
  resourcePageShellState = consoleShellState;
  if (resourcePageLifecycle.enter().changed) {
    clearBasicResourceFeedback();
    if (RESOURCE_NAVIGATION_IDS.has(previousResourceNavigation)) app?.replaceChildren();
  }
  const activeNavigationId = consoleShellState.activeNavigationItemId;
  const consoleExperienceView = workspaceConsoleExperienceView(activeNavigationId);
  if (!consoleExperienceView) {
    workspaceConsoleExperienceShellState = null;
    workspaceConsoleExperienceController.leave();
  }
  const state = {};
  const load = async (key, loader) => {
    state[key] = await loader();
    return state[key];
  };

  switch (activeNavigationId) {
    case "overview":
      state.workspaceConsoleExperienceState = await loadWorkspaceConsoleExperienceState(
        consoleShellState,
        "overview",
      );
      break;
    case "tenant":
      await load("tenantState", loadTenantState);
      break;
    case "users":
      await load("usersState", () => loadUsersState(consoleShellState));
      state.invitationState = invitationPageState.snapshot();
      break;
    case "environments":
      await load("environmentState", loadEnvironmentState);
      break;
    case "providers":
      await load("providersState", () => loadProvidersState({ includeCatalog: true }));
      break;
    case "error-maps":
      await workspaceProviderEngineeringController.open({
        authContext: authSession.context,
        activeNavigationItemId: activeNavigationId,
        providerId: "",
      });
      state.workspaceProviderEngineeringState = workspaceProviderEngineeringController.snapshot();
      break;
    case "provider-health":
      await load("providerHealthState", () => loadProviderHealthState(consoleShellState));
      break;
    case "models":
      [state.modelsState, state.providersState] = await Promise.all([
        loadModelsState(), loadProvidersState(),
      ]);
      break;
    case "response-cache":
      await load("responseCacheState", () => loadResponseCacheState(consoleShellState, refreshId));
      break;
    case "model-aliases":
      [state.modelAliasesState, state.modelsState, state.environmentState] = await Promise.all([
        loadModelAliasesState(), loadModelsState(), loadEnvironmentState(),
      ]);
      break;
    case "routing-policies": {
      const identity = currentEvaluationPageIdentity("routing-policies", PERMISSIONS.ROUTING_READ);
      const ticket = routingPolicyPageMemory.beginRefresh({
        identity,
        navigation: "routing-policies",
      });
      const cacheTicket = cacheAffinityPageMemory.beginRefresh({
        identity,
        navigation: "routing-policies",
        workspaceOwner: workspaceResourcePageOwner(authSession, resourcePageShellState, {
          navigationId: "routing-policies", permissions: [PERMISSIONS.ROUTING_READ], hash: readActiveNavigationHash(),
        }),
      });
      const aliases = await loadModelAliasesState();
      if (!ticket || !cacheTicket) break;
      if (aliases?.state === "ok") {
        routingPolicyPageMemory.acceptRefresh(ticket, aliases.model_aliases);
        cacheAffinityPageMemory.acceptRefresh(cacheTicket, aliases.model_aliases);
      } else {
        routingPolicyPageMemory.acceptRefreshError(ticket, aliases);
        cacheAffinityPageMemory.acceptRefreshError(cacheTicket, aliases);
      }
      state.routingPoliciesState = {
        ...routingPolicyPageMemory.snapshot(),
        cacheAffinity: cacheAffinityPageMemory.snapshot(),
      };
      break;
    }
    case "model-quality-scores":
      state.modelQualityScoresState = await loadModelQualityScoresState(consoleShellState, refreshId);
      state.modelQualityScoreFilters = modelQualityScoreFilters;
      break;
    case "experiments": {
      let resources = { projectState: null, environmentState: null, modelAliasesState: null, modelsState: null };
      if (shouldLoadExperimentCreateResources(consoleShellState)) {
        [resources.projectState, resources.environmentState, resources.modelAliasesState, resources.modelsState] = await Promise.all([
          loadProjectState(), loadEnvironmentState(), loadModelAliasesState(), loadModelsState(),
        ]);
      }
      Object.assign(state, resources);
      state.experimentsState = await loadExperimentsState(consoleShellState, refreshId, resources, {
        preserveFinalOnStatisticsUnavailable: isBackgroundRefresh,
      });
      break;
    }
    case "projects":
      await load("projectState", loadProjectState);
      break;
    case "virtual-keys":
      [state.virtualKeysState, state.projectState, state.environmentState] = await Promise.all([
        loadVirtualKeysState(), loadProjectState(), loadEnvironmentState(),
      ]);
      break;
    case "usage":
      await load("usageState", loadUsageState);
      break;
    case "workspace-subscription":
    case "billing-disputes":
    case "internal-finops":
    case "model-prices":
      workspaceCommercialNavigationItems = [
        ...(consoleShellState.navigation?.navigation_items ?? []),
      ];
      await workspaceCommercialAccountingController.open({
        authContext: authSession.context,
        activeNavigationItemId: activeNavigationId,
      });
      state.workspaceCommercialAccountingState =
        workspaceCommercialAccountingController.snapshot();
      break;
    case "finops-ledger":
      await load("finOpsState", () => loadFinOpsState(consoleShellState, refreshId));
      break;
    case "quota-buckets":
      await load("quotaManagementState", () => loadQuotaManagementState(consoleShellState, refreshId));
      break;
    case "cost-anomalies":
      await load("costAnomaliesState", () => loadCostAnomaliesState(consoleShellState, refreshId));
      break;
    case "budgets":
      [state.virtualKeysState, state.projectState, state.environmentState] = await Promise.all([
        loadVirtualKeysState(), loadProjectState(), loadEnvironmentState(),
      ]);
      state.budgetState = await loadBudgetState(state.virtualKeysState, consoleShellState, refreshId);
      break;
    case "policies":
      state.modelsState = await loadModelsState();
      state.policiesState = await loadPoliciesState(consoleShellState, refreshId, state.modelsState);
      break;
    case "policy-simulator":
      [state.environmentState, state.projectState, state.modelsState, state.modelAliasesState] = await Promise.all([
        loadEnvironmentState(), loadProjectState(), loadModelsState(), loadModelAliasesState(),
      ]);
      policyDryRunContextState = {
        environmentState: state.environmentState,
        projectState: state.projectState,
        modelsState: state.modelsState,
        modelAliasesState: state.modelAliasesState,
      };
      state.policyDryRunState = policyDryRunState;
      break;
    case "policy-snapshots":
      await load("policySnapshotsState", () => loadPolicySnapshotsState(consoleShellState, refreshId));
      break;
    case "policy-templates":
      await load("policyTemplateLibraryState", () => loadPolicyTemplateLibraryState(consoleShellState, refreshId));
      break;
    case "dlp-rules":
      await load("dlpState", loadDlpState);
      break;
    case "intent-security": {
      const owner = intentSecurityPageOwner(authSession.context, consoleShellState, {
        refreshId,
        currentRefreshId: refreshSequence,
      });
      if (owner) intentSecurityPageState.enter(owner);
      else intentSecurityPageState.leave();
      state.intentSecurityState = intentSecurityPageState.snapshot();
      break;
    }
    case "session-risks":
      await load("sessionRisksState", () => loadSessionRisksState(consoleShellState, refreshId));
      break;
    case "files":
      await load("filesState", () => loadFilesState(consoleShellState, refreshId));
      break;
    case "file-security":
      await load("fileSecurityState", () => loadFileSecurityState(consoleShellState, refreshId));
      break;
    case "batch-jobs": {
      const resources = {};
      [resources.projectState, resources.environmentState, resources.modelAliasesState] = await Promise.all([
        loadProjectState(), loadEnvironmentState(), loadModelAliasesState(),
      ]);
      Object.assign(state, resources);
      state.batchJobsState = await loadBatchJobsState(consoleShellState, refreshId, resources);
      break;
    }
    case "tenant-data-lifecycle":
      await load("tenantDataLifecycleState", () => loadTenantDataLifecycleState(consoleShellState, refreshId));
      break;
    case "mcp-tools":
      await load("mcpToolsState", () => loadMcpToolsState(consoleShellState, refreshId));
      break;
    case "local-models":
      await load("localEndpointsState", () => loadLocalEndpointsState(consoleShellState, refreshId));
      break;
    case "private-gateways":
      await load("privateGatewaysState", () => loadPrivateGatewaysState(consoleShellState, refreshId));
      break;
    case "migration-operations":
      await load("migrationOperationsState", () => loadMigrationOperationsState(
        consoleShellState, refreshId,
      ));
      break;
    case "support-tickets":
      await load("supportTicketsState", () => loadSupportTicketsState(
        consoleShellState, refreshId,
      ));
      break;
    case "output-safety":
    case "request-logs":
      await load("requestLogsState", () => loadRequestLogsState(consoleShellState, refreshId));
      state.requestFeedbackState = requestFeedbackMutationMemory.snapshot();
      state.requestExplanationState = requestExplanationPageMemory.snapshot();
      break;
    case "webhooks":
      await load("webhookState", loadWebhookState);
      break;
    case "webhook-dead-letters":
      await load("webhookDeadLettersState", () => loadWebhookDeadLettersState(consoleShellState, refreshId));
      break;
    case "request-explanations":
      state.requestExplanationState = requestExplanationPageMemory.snapshot();
      break;
    case "feedback-summary":
      state.requestFeedbackSummaryState = await loadRequestFeedbackSummaryState(consoleShellState, refreshId);
      state.requestFeedbackSummaryFilters = requestFeedbackSummaryFilters;
      break;
    case "audit-logs":
      await load("auditLogsState", () => loadAuditLogsState(consoleShellState, refreshId));
      break;
    case "role-permissions":
      await load("rolePermissionState", () => loadRolePermissionState(consoleShellState, refreshId));
      break;
    case "language-settings":
      state.accountPreferenceLocation = "/identity.html#account-preferences";
      break;
    case "quickstart":
      await load("workspaceTaskHomeState", () => (
        workspaceNavigationController.loadGettingStarted()
      ));
      break;
    case "sdk-examples":
      await load("sdkExampleStates", () => loadSdkExampleStates(consoleShellState));
      break;
    case "migration-guide":
      [state.environmentState, state.modelAliasesState] = await Promise.all([
        loadEnvironmentState(), loadModelAliasesState(),
      ]);
      break;
    case "api-docs":
      if (hasPermission(authSession.context, PERMISSIONS.DEVELOPER_PORTAL_READ)) {
        const token = authSession.token;
        const controller = new AbortController();
        developerContractSecurityController = controller;
        const contracts = await getDeveloperContractWorkspace({
          controlApiUrl,
          token,
          signal: controller.signal,
        });
        const supplyChain = await getDeveloperSupplyChainSecurity({
          controlApiUrl,
          token,
          releaseIds: contracts.sdk_releases
            .filter((item) => item.status === "published")
            .map((item) => item.sdk_release_id),
          signal: controller.signal,
        });
        if (developerContractSecurityController !== controller
          || controller.signal.aborted || token !== authSession.token
          || refreshId !== refreshSequence || !refreshOwnerCurrent()
          || consoleShellState.activeNavigationItemId !== "api-docs") break;
        developerContractSecurityController = null;
        state.developerContractState = Object.freeze({ ...contracts, supply_chain: supplyChain });
      }
      break;
    case "enterprise-chat":
      await loadEnterpriseChatState(consoleShellState);
      state.enterpriseChatState = enterpriseChatPageState.snapshot();
      break;
    case "prompt-templates":
      await load("promptTemplateState", () => loadPromptTemplateState(consoleShellState, refreshId));
      break;
    case "approvals":
      state.workspaceConsoleExperienceState = await loadWorkspaceConsoleExperienceState(
        consoleShellState,
        "approvals",
      );
      break;
    case "admin-assistant":
      state.workspaceConsoleExperienceState = await loadWorkspaceConsoleExperienceState(
        consoleShellState,
        "admin-assistant",
      );
      break;
    case "system-status":
      state.workspaceConsoleExperienceState = await loadWorkspaceConsoleExperienceState(
        consoleShellState,
        "system-status",
      );
      break;
    case "config-summary":
      state.workspaceConsoleExperienceState = await loadWorkspaceConsoleExperienceState(
        consoleShellState,
        "deployment-summary",
      );
      break;
    default:
      // Static pages (API docs, comparison, and debug console) require no data API.
      break;
  }

  if (!hasWorkspaceConsoleSession(authSession) || refreshId !== refreshSequence || !refreshOwnerCurrent()) return;

  developerContractDownloadMetadata = activeNavigationId === "api-docs"
    ? new Map((state.developerContractState?.sdk_releases ?? []).map((release) => [
      release.sdk_release_id, Object.freeze({
        ...release,
        supply_chain: state.developerContractState?.supply_chain?.[release.sdk_release_id] ?? null,
      }),
    ]))
    : new Map();

  if (resourcePageLifecycle.blocksReplacement(false)) return;
  resourcePageLifecycle.render();
  renderDeveloperStart(app, {
    gatewayUrl,
    controlApiUrl,
    consoleUrl,
    authContext: authSession.context,
    consoleShellState,
    i18n: currentI18n,
    branding: publicBrandingDisplay(),
    gatewayDebugState,
    policyDryRunState,
    enterpriseChatState: enterpriseChatPageState.snapshot(),
    workspaceConsoleExperienceState:
      state.workspaceConsoleExperienceState ?? workspaceConsoleExperienceController.snapshot(),
    invitationState: invitationPageState.snapshot(),
    preserveConsoleSidebar,
    ...state,
  });
  if (activeNavigationId === "enterprise-chat") {
    hydrateEnterpriseChatText(app, enterpriseChatPageState.snapshot());
  }
  if (activeNavigationId === "admin-assistant") {
    hydrateWorkspaceConsoleAssistant(app, workspaceConsoleExperienceController.snapshot());
  }
  if (activeNavigationId === "prompt-templates") {
    hydratePromptTemplateSensitiveText(app, promptTemplatePageState.snapshot());
  }
  if (activeNavigationId === "debug-console") {
    const panel = document.querySelector(".gateway-debug-panel");
    if (panel) panel.innerHTML = renderGatewayDebugConsole(gatewayDebugState, { gatewayUrl });
  }
  bindOutputSafetyDetailInteractions(consoleShellState);
  bindAuthenticatedEvents(consoleShellState);
}

/**
 * 渲染 控制台主应用 的 `renderLogin` 界面，把后端状态转换为用户可操作的视图。
 * 边界：前端隐藏按钮只改善体验，不能替代后端 RBAC 和租户校验。
 */
function renderLogin(error, requestId) {
  refreshSequence += 1;
  clearAuthSession();
  authSession = { context: null, owner: null };
  clearConsoleSessionState();
  renderWorkspaceSessionPrompt(app, { error, requestId, branding: publicBrandingPreview });
  const retry = document.querySelector("[data-console-session-retry]");
  if (retry) retry.onclick = () => refresh();
  void loadPublicBrandingPreview(publicBrandingGeneration, true);
}

function clearBasicResourceFeedback() {
  workspaceProfileState = workspaceProfileMutation = workspaceProfileRecovery = null;
  clearRequestLogSessionState();
  clearRequestExplanationSessionState();
  clearRequestFeedbackSummarySessionState();
  clearAuditExportSessionState();
  auditLogFilters = { limit: 20, offset: 0 };
  workspaceMembersOffset = 0;
  workspaceMembersFeedback = "";
  workspaceMembersActionError = null;
  modelAliasActionError = null;
  modelAliasFeedback = "";
  clearRoutingPolicySessionState();
  clearModelQualityScoreSessionState();
  experimentPageState.clear();
  providerHealthActionError = providerHealthProbingProviderId = providerStatusEventWarning = null;
  providerStatusFilters = { limit: 50, offset: 0 };
  providerStatusPageState.clear();
  providerStatusVisitTracker.leave();
  environmentActionError = projectActionError = providerActionError = modelActionError = virtualKeyActionError = null;
  environmentFeedback = projectFeedback = providerFeedback = modelFeedback = virtualKeyFeedback = "";
  providerLastTestResult = virtualKeyCreatedSecret = null;
  consumerDirectoryOffset = 0;
  providerCatalogActiveTab = "catalog";
  providerCatalogFilters = { status: "", vendorCode: "", capabilityCode: "", regionCode: "" };
  providerCatalogCompatibilityByEntryCode = {};
  providerCatalogLastCreated = providerCatalogLastPreflight = null;
  providerCatalogCreateIntent = providerCatalogPreflightIntent = null;
  providerCatalogActionError = null;
  providerCatalogFeedback = "";
  filePageState.clear();
}

function clearBasicResourceSessionState({ clearDom = false } = {}) {
  workspacePageVisitTracker.leave();
  workspacePageVisitShellState = null;
  if (clearDom && RESOURCE_NAVIGATION_IDS.has(resourcePageLifecycle.navigation())) app?.replaceChildren();
  resourcePageLifecycle.clear();
  resourcePageShellState = null;
  clearBasicResourceFeedback();
}

function bindResourceElement(element, permissions, navigationId) {
  const binding = resourcePageLifecycle.bind(element, permissions, navigationId);
  if (binding) {
    element.addEventListener("input", () => resourcePageLifecycle.markDirty(binding));
    element.addEventListener("change", () => resourcePageLifecycle.markDirty(binding));
  }
  return binding;
}

function currentEvaluationPageIdentity(navigationId, permission) {
  return workspaceResourcePageOwner(authSession, resourcePageShellState, {
    navigationId, permissions: [permission], hash: readActiveNavigationHash(),
  })?.key ?? "";
}

function handleResourceAuthorizationError(error) {
  if (!(error instanceof AuthApiError)) return false;
  if (error.status === 401) {
    renderLogin(authErrorMessage(error), error.requestId);
    return true;
  }
  if (error.status !== 403 || error.code !== "auth.permission_denied") return false;
  const session = authSession;
  const generation = refreshSequence;
  const hash = readActiveNavigationHash();
  clearBasicResourceSessionState({ clearDom: true });
  globalThis.queueMicrotask(() => {
    if (session === authSession && generation === refreshSequence && hash === readActiveNavigationHash()) {
      void refresh();
    }
  });
  return true;
}

function handleResourceMutationError(operation, error) {
  if (!resourcePageLifecycle.isMutationCurrent(operation) || isAbortError(error)) {
    resourcePageLifecycle.finishMutation(operation);
    return true;
  }
  return handleResourceAuthorizationError(error);
}

async function resourceMutationRequest(operation, request, input) {
  if (!resourcePageLifecycle.isMutationCurrent(operation)) throw new DOMException("Cancelled", "AbortError");
  const result = await request({ ...input, authentication: "workspace", signal: operation.controller.signal });
  if (!resourcePageLifecycle.isMutationCurrent(operation)) throw new DOMException("Cancelled", "AbortError");
  return result;
}

async function refreshResourceMutation(operation) {
  const current = resourcePageLifecycle.isMutationCurrent(operation);
  resourcePageLifecycle.finishMutation(operation);
  if (current) await refresh();
}

async function loadResourceState(lane, permissions, request, project, errorFields = {}) {
  const operation = resourcePageLifecycle.beginRead(lane, permissions);
  if (!operation) return null;
  const current = () => resourcePageLifecycle.isReadCurrent(operation);
  try {
    const result = await request({ controlApiUrl, authentication: "workspace", signal: operation.controller.signal }, current);
    return current() ? project(result) : null;
  } catch (error) {
    if (!current() || isAbortError(error) || handleResourceAuthorizationError(error)) return null;
    return { ...errorFields, state: "error", message: authErrorMessage(error), requestId: error?.requestId ?? "-" };
  } finally {
    resourcePageLifecycle.finishRead(operation);
  }
}

function clearConsoleSessionState({ clearNavigation = true } = {}) {
  clearBasicResourceSessionState();
  consoleSidebarController?.abort();
  consoleSidebarController = null;
  mobileSidebarCollapsed = true;
  cancelPendingConsoleNavigationSideEffects();
  workspaceConsoleExperienceShellState = null;
  workspaceConsoleExperienceController.leave();
  if (clearNavigation) workspaceNavigationController.leave();
  privateGatewayBrandingShellState = null;
  deploymentBrandingController.clear();
  workspaceProviderEngineeringProviders = [];
  workspaceProviderEngineeringController.close();
  workspaceCommercialNavigationItems = [];
  workspaceCommercialAccountingController.close();
  finOpsPageState.clear();
  closeWorkspaceAdvancedToolsDialog({ restoreFocus: false });
  clearDeveloperContractDownload();
  clearDeveloperContractSecurityLoad();
  developerContractDownloadMetadata = new Map();
  clearPublicBrandingPreview();
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
  fileSecurityPageState.clear();
  batchJobPageState.clear();
  tenantDataLifecyclePageState.clear();
  mcpToolPageState.clear();
  localEndpointPageState.clear();
  privateGatewayPageState.clear();
  migrationOperationPageState.clear();
  supportTicketPageState.clear();
  experimentPageState.clear();
  responseCachePageState.clear();
  quotaManagementPageState.clear();
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
  providerStatusPageState.clear();
  providerStatusVisitTracker.leave();
  providerStatusFilters = { limit: 50, offset: 0 };
  providerStatusEventWarning = null;
  providerStatusEntrySource = readActiveNavigationHash() === "provider-health" ? "direct_hash" : "sidebar";
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
  bindWorkspaceMemberInteractions();
  bindDeveloperContractDownloads(consoleShellState);
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
    if (consoleLogoutPending) return;
    consoleLogoutPending = true;
    const logoutGeneration = ++refreshSequence;
    clearConsoleSessionState();
    clearAuthSession();
    authSession = { context: null, owner: null };
    renderWorkspaceSessionPrompt(app, { pending: true });
    broadcastConsoleWorkspaceContextInvalidation();
    let failure = null;
    try {
      await logoutIdentity({ controlApiUrl: workspaceControlApiUrl });
    } catch (error) {
      if (Number(error?.status) !== 401) failure = error;
    } finally {
      consoleLogoutPending = false;
      broadcastConsoleWorkspaceContextInvalidation();
    }
    if (logoutGeneration !== refreshSequence || hasWorkspaceConsoleSession(authSession)) return;
    renderLogin(failure ? authErrorMessage(failure) : undefined, failure?.requestId);
  });

  for (const navigationButton of document.querySelectorAll("[data-console-nav]")) {
    navigationButton.onclick = async () => {
      await switchConsoleNavigation(navigationButton, consoleShellState);
    };
  }

  for (const groupToggle of document.querySelectorAll("[data-sidebar-group-toggle]")) {
    groupToggle.onclick = async () => {
      await toggleConsoleSidebarGroup(groupToggle, consoleShellState);
    };
  }
  bindWorkspaceNavigationInteractions(consoleShellState);

  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  if (sidebarToggle) sidebarToggle.onclick = async () => {
    await toggleConsoleSidebar(sidebarToggle, consoleShellState);
  };

  bindQuickstartCopyButtons();
  bindSdkLanguageTabs();
  bindSdkExampleDownloads();
  bindMigrationGuideInteractions();
  bindRolePermissionInteractions(consoleShellState);
  bindPolicyInteractions(consoleShellState);
  bindPolicySnapshotInteractions(consoleShellState);
  bindPolicyDryRunInteractions(consoleShellState);
  bindIntentSecurityInteractions(consoleShellState);
  bindRoutingPolicyInteractions(consoleShellState);
  bindModelQualityScoreInteractions(consoleShellState);
  bindWorkspaceProviderEngineeringInteractions(consoleShellState);
  bindWorkspaceCommercialAccountingInteractions();
  bindRequestExplanationInteractions(consoleShellState);
  bindRequestFeedbackInteractions(consoleShellState);
  bindRequestFeedbackSummaryInteractions(consoleShellState);
  // 页面完成首屏渲染并绑定交互后再启动唯一访问 owner；失败只更新局部告警，不阻断指南阅读。
  void ownMigrationGuideVisitAfterRender(consoleShellState);
  void ownProviderStatusVisitAfterRender(consoleShellState);
  void ownWorkspacePageVisitAfterRender(consoleShellState);

  const tenantForm = document.querySelector("#tenant-form");
  bindWorkspaceProfileInteractions();
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
  const environmentCreateBinding = bindResourceElement(createEnvironmentForm, [PERMISSIONS.ENVIRONMENT_READ, PERMISSIONS.ENVIRONMENT_WRITE], "environments");
  createEnvironmentForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(environmentCreateBinding);
    if (!operation) return;
    const data = new FormData(createEnvironmentForm);
    const button = createEnvironmentForm.querySelector("button[type='submit']");
    button.disabled = true;
    environmentActionError = null;
    environmentFeedback = "环境创建中";
    try {
      await resourceMutationRequest(operation, createEnvironment, {
        controlApiUrl,
        code: String(data.get("code") ?? ""),
        name: String(data.get("name") ?? ""),
        description: String(data.get("description") ?? ""),
      });
      environmentFeedback = "环境已创建";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      environmentActionError = error;
      environmentFeedback = "环境创建失败";
      await refreshResourceMutation(operation);
    }
  });

  for (const form of document.querySelectorAll("[data-environment-status-form]")) {
    const environmentStatusBinding = bindResourceElement(form, [PERMISSIONS.ENVIRONMENT_READ, PERMISSIONS.ENVIRONMENT_WRITE], "environments");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(environmentStatusBinding);
      if (!operation) return;
      const data = new FormData(form);
      const nextStatus = String(data.get("status") ?? "");
      const button = form.querySelector("button[type='submit']");
      button.disabled = true;
      environmentActionError = null;
      environmentFeedback = nextStatus === "disabled" ? "环境禁用中" : "环境启用中";
      try {
        await resourceMutationRequest(operation, updateEnvironment, {
          controlApiUrl,
          environmentId: form.dataset.environmentId,
          status: nextStatus,
        });
        environmentFeedback = nextStatus === "disabled" ? "环境已禁用" : "环境已启用";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        // Environment 写权限由后端 admin 依赖裁决，前端仅展示返回的统一错误。
        environmentActionError = error;
        environmentFeedback = "环境状态更新失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  const providerHealthForm = document.querySelector("#provider-health-filters");
  const providerHealthFilterBinding = bindResourceElement(providerHealthForm, [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ], "provider-health");
  providerHealthForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!resourcePageLifecycle.isBindingCurrent(providerHealthFilterBinding)) return;
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
    resourcePageLifecycle.cancelReads();
    await refresh();
  });
  const providerHealthReset = document.querySelector("[data-provider-health-reset]");
  const providerHealthResetBinding = bindResourceElement(providerHealthReset, [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ], "provider-health");
  providerHealthReset?.addEventListener("click", async (event) => {
    event.preventDefault();
    if (!resourcePageLifecycle.isBindingCurrent(providerHealthResetBinding)) return;
    providerStatusFilters = { limit: 50, offset: 0 };
    providerStatusPageState.clear();
    resourcePageLifecycle.cancelReads();
    await refresh();
  });
  for (const button of document.querySelectorAll("[data-provider-status-offset]")) {
    const providerStatusOffsetBinding = bindResourceElement(button, [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ], "provider-health");
    button.addEventListener("click", async () => {
      if (!resourcePageLifecycle.isBindingCurrent(providerStatusOffsetBinding)) return;
      const nextOffset = Number(button.dataset.providerStatusOffset);
      // offset 只能来自渲染器生成的非负安全整数；异常 DOM 值不触发网络。
      if (!Number.isSafeInteger(nextOffset) || nextOffset < 0) return;
      providerStatusFilters = {
        ...providerStatusFilters,
        limit: providerStatusFilters.limit ?? 50,
        offset: nextOffset,
      };
      providerStatusPageState.clear();
      resourcePageLifecycle.cancelReads();
      await refresh();
    });
  }
  for (const button of document.querySelectorAll("[data-provider-probe]")) {
    const providerProbeBinding = bindResourceElement(button, [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ, PERMISSIONS.PROVIDER_HEALTH_PROBE], "provider-health");
    button.addEventListener("click", async () => {
      const operation = resourcePageLifecycle.beginMutation(providerProbeBinding);
      if (!operation) return;
      await runProviderProbe(String(button.dataset.providerProbe ?? ""), operation);
    });
  }

  const createProjectForm = document.querySelector("#create-project-form");
  const projectCreateBinding = bindResourceElement(createProjectForm, [PERMISSIONS.PROJECT_READ, PERMISSIONS.PROJECT_WRITE], "projects");
  createProjectForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(projectCreateBinding);
    if (!operation) return;
    const button = createProjectForm.querySelector("button[type='submit']");
    const data = new FormData(createProjectForm);
    button.disabled = true;
    projectActionError = null;
    projectFeedback = "项目创建中";
    try {
      await resourceMutationRequest(operation, createProject, {
        controlApiUrl,
        projectCode: String(data.get("projectCode") ?? ""),
        name: String(data.get("name") ?? ""),
        description: String(data.get("description") ?? ""),
      });
      projectFeedback = "项目已创建";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      projectActionError = error;
      projectFeedback = "项目创建失败";
      await refreshResourceMutation(operation);
    }
  });

  const createApplicationForm = document.querySelector("#create-application-form");
  const applicationCreateBinding = bindResourceElement(createApplicationForm, [PERMISSIONS.PROJECT_READ, PERMISSIONS.PROJECT_WRITE], "projects");
  createApplicationForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(applicationCreateBinding);
    if (!operation) return;
    const button = createApplicationForm.querySelector("button[type='submit']");
    const data = new FormData(createApplicationForm);
    button.disabled = true;
    projectActionError = null;
    projectFeedback = "应用创建中";
    try {
      await resourceMutationRequest(operation, createApplication, {
        controlApiUrl,
        projectId: String(data.get("projectId") ?? ""),
        applicationCode: String(data.get("applicationCode") ?? ""),
        name: String(data.get("name") ?? ""),
        description: String(data.get("description") ?? ""),
      });
      projectFeedback = "应用已创建";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      projectActionError = error;
      projectFeedback = "应用创建失败";
      await refreshResourceMutation(operation);
    }
  });

  const createVirtualKeyForm = document.querySelector("#create-virtual-key-form");
  const virtualKeyCreateBinding = bindResourceElement(createVirtualKeyForm, [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE], "virtual-keys");
  createVirtualKeyForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(virtualKeyCreateBinding);
    if (!operation) return;
    const button = createVirtualKeyForm.querySelector("button[type='submit']");
    const data = new FormData(createVirtualKeyForm);
    button.disabled = true;
    virtualKeyActionError = null;
    virtualKeyFeedback = "创建中";
    try {
      const result = await resourceMutationRequest(operation, createVirtualKey, {
        controlApiUrl,
        name: String(data.get("name") ?? ""),
        environmentId: String(data.get("environmentId") ?? ""),
        environment: undefined,
        description: String(data.get("description") ?? ""),
        projectId: String(data.get("projectId") ?? ""),
        applicationId: String(data.get("applicationId") ?? ""),
        consumerId: String(data.get("consumerId") ?? ""),
      });
      virtualKeyCreatedSecret = {
        plainValue: result.plain_value,
        keyPrefix: result.virtual_key?.key_prefix,
      };
      virtualKeyFeedback = "创建完成";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      virtualKeyActionError = error;
      virtualKeyFeedback = "创建失败";
      await refreshResourceMutation(operation);
    }
  });

  for (const virtualKeyForm of document.querySelectorAll("[data-virtual-key-form]")) {
    const virtualKeyStatusBinding = bindResourceElement(virtualKeyForm, [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE], "virtual-keys");
    virtualKeyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(virtualKeyStatusBinding);
      if (!operation) return;
      const button = virtualKeyForm.querySelector("button[type='submit']");
      const data = new FormData(virtualKeyForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      virtualKeyActionError = null;
      virtualKeyFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        if (nextStatus === "disabled") {
          await resourceMutationRequest(operation, disableVirtualKey, {
            controlApiUrl,
            virtualKeyId: virtualKeyForm.dataset.virtualKeyId,
          });
        } else {
          await resourceMutationRequest(operation, updateVirtualKey, {
            controlApiUrl,
            virtualKeyId: virtualKeyForm.dataset.virtualKeyId,
            status: nextStatus,
          });
        }
        virtualKeyFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        virtualKeyActionError = error;
        virtualKeyFeedback = "操作失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const form of document.querySelectorAll("#create-consumer-form, [data-consumer-form]")) {
    const binding = bindResourceElement(form, [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE, PERMISSIONS.USER_MANAGE], "virtual-keys");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(binding);
      if (!operation) return;
      form.querySelector("button[type='submit']").disabled = true;
      const data = new FormData(form);
      virtualKeyActionError = null;
      try {
        await resourceMutationRequest(operation, form.dataset.consumerId ? updateConsumer : createConsumer, {
          controlApiUrl, consumerId: form.dataset.consumerId,
          displayName: String(data.get("displayName") ?? ""), department: String(data.get("department") ?? ""),
          status: data.has("active") ? "active" : "disabled", revision: Number(form.dataset.revision),
        });
        virtualKeyFeedback = form.dataset.consumerId ? "使用者已更新" : "使用者已添加";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        virtualKeyActionError = error;
        await refreshResourceMutation(operation);
      }
    });
  }
  for (const button of document.querySelectorAll("[data-consumer-page], [data-rotate-virtual-key]")) {
    const binding = bindResourceElement(button, [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE], "virtual-keys");
    button.addEventListener("click", async () => {
      const operation = resourcePageLifecycle.beginMutation(binding);
      if (!operation) return;
      button.disabled = true;
      virtualKeyActionError = null;
      try {
        if (button.dataset.consumerPage !== undefined) {
          consumerDirectoryOffset = Number(button.dataset.consumerPage);
        } else {
          const result = await resourceMutationRequest(operation, rotateVirtualKey, {
            controlApiUrl, virtualKeyId: button.dataset.rotateVirtualKey, expectedRevision: Number(button.dataset.keyRevision),
          });
          virtualKeyCreatedSecret = { plainValue: result.plain_value, keyPrefix: result.virtual_key?.key_prefix };
          virtualKeyFeedback = "KEY 已轮换";
        }
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        virtualKeyActionError = error;
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const limitForm of document.querySelectorAll("[data-virtual-key-limits-form]")) {
    const virtualKeyLimitsBinding = bindResourceElement(limitForm, [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE], "virtual-keys");
    limitForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(virtualKeyLimitsBinding);
      if (!operation) return;
      const button = limitForm.querySelector("button[type='submit']");
      const data = new FormData(limitForm);
      button.disabled = true;
      virtualKeyActionError = null;
      virtualKeyFeedback = "保存限制中";
      try {
        await resourceMutationRequest(operation, updateVirtualKeyLimits, {
          controlApiUrl,
          virtualKeyId: limitForm.dataset.virtualKeyId,
          rpmLimit: String(data.get("rpmLimit") ?? ""),
          tpmLimit: String(data.get("tpmLimit") ?? ""),
          concurrencyLimit: String(data.get("concurrencyLimit") ?? ""),
          status: "active",
        });
        virtualKeyFeedback = "限制已保存";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        virtualKeyActionError = error;
        virtualKeyFeedback = "限制保存失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const tab of document.querySelectorAll("[data-provider-tab]")) {
    const providerTabBinding = bindResourceElement(tab, [], "providers");
    tab.addEventListener("click", async () => {
      if (!resourcePageLifecycle.isBindingCurrent(providerTabBinding)) return;
      const nextTab = tab.dataset.providerTab;
      if (!new Set(["catalog", "providers"]).has(nextTab)) return;
      providerCatalogActiveTab = nextTab;
      providerCatalogActionError = null;
      await refresh();
    });
  }

  const providerCatalogFilterForm = document.querySelector("[data-provider-catalog-filter-form]");
  const providerCatalogFilterBinding = bindResourceElement(providerCatalogFilterForm, [PERMISSIONS.PROVIDER_CATALOG_READ], "providers");
  providerCatalogFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!resourcePageLifecycle.isBindingCurrent(providerCatalogFilterBinding)) return;
    const data = new FormData(providerCatalogFilterForm);
    providerCatalogFilters = {
      status: String(data.get("status") ?? ""),
      vendorCode: String(data.get("vendorCode") ?? "").trim(),
      capabilityCode: String(data.get("capabilityCode") ?? "").trim(),
      regionCode: String(data.get("regionCode") ?? "").trim(),
    };
    providerCatalogActionError = null;
    providerCatalogFeedback = "筛选已应用";
    await refresh();
  });

  for (const button of document.querySelectorAll("[data-provider-catalog-compatibility]")) {
    const providerCompatibilityBinding = bindResourceElement(button, [PERMISSIONS.PROVIDER_CATALOG_READ], "providers");
    button.addEventListener("click", async () => {
      const operation = resourcePageLifecycle.beginMutation(providerCompatibilityBinding);
      if (!operation) return;
      button.disabled = true;
      providerCatalogActionError = null;
      providerCatalogFeedback = "正在读取兼容说明";
      try {
        const result = await resourceMutationRequest(operation, getProviderCatalogCompatibility, {
          controlApiUrl,
          entryCode: button.dataset.entryCode,
        });
        providerCatalogCompatibilityByEntryCode = {
          ...providerCatalogCompatibilityByEntryCode,
          [result.entry_code]: result,
        };
        providerCatalogFeedback = "兼容说明已更新";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        providerCatalogActionError = error;
        providerCatalogFeedback = "兼容说明读取失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const form of document.querySelectorAll("[data-provider-catalog-create-form]")) {
    const providerCatalogCreateBinding = bindResourceElement(form, [PERMISSIONS.PROVIDER_CATALOG_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(providerCatalogCreateBinding);
      if (!operation) return;
      try {
        const data = new FormData(form);
        const input = {
          entryCode: String(data.get("entryCode") ?? ""),
          version: String(data.get("version") ?? ""),
          regionCode: String(data.get("regionCode") ?? ""),
          capabilityCodes: data.getAll("capabilityCodes").map(String).sort(),
          providerCode: String(data.get("providerCode") ?? "").trim(),
          name: String(data.get("name") ?? "").trim(),
          confirmed: data.get("confirmed") === "on",
        };
        const fingerprint = JSON.stringify(input);
        const idempotencyKey = providerCatalogCreateIntent?.fingerprint === fingerprint
          ? providerCatalogCreateIntent.idempotencyKey
          : createProviderCatalogIdempotencyKey("instantiate");
        providerCatalogCreateIntent = { fingerprint, idempotencyKey };
        form.querySelector("button[type='submit']").disabled = true;
        providerCatalogActionError = null;
        providerCatalogFeedback = "正在创建目录实例";
        providerCatalogLastPreflight = null;
        const result = await resourceMutationRequest(operation, createProviderFromCatalog, {
          controlApiUrl,
          ...input,
          idempotencyKey,
        });
        providerCatalogCreateIntent = null;
        providerCatalogLastCreated = result;
        providerCatalogFeedback = result.idempotent_replay ? "已恢复同一创建结果" : "Provider 已从目录创建";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        if (!(error instanceof AuthApiError) || ![0, 503].includes(error.status)) {
          providerCatalogCreateIntent = null;
        }
        providerCatalogActionError = error;
        providerCatalogFeedback = "目录实例创建失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  const providerCatalogPreflightForm = document.querySelector("[data-provider-catalog-preflight-form]");
  const providerCatalogPreflightBinding = bindResourceElement(providerCatalogPreflightForm, [PERMISSIONS.PROVIDER_CATALOG_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
  providerCatalogPreflightForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(providerCatalogPreflightBinding);
    if (!operation) return;
    try {
      const data = new FormData(providerCatalogPreflightForm);
      const input = {
        providerId: String(data.get("providerId") ?? ""),
        timeoutSeconds: Number(data.get("timeoutSeconds")),
      };
      const fingerprint = JSON.stringify(input);
      const idempotencyKey = providerCatalogPreflightIntent?.fingerprint === fingerprint
        ? providerCatalogPreflightIntent.idempotencyKey
        : createProviderCatalogIdempotencyKey("preflight");
      providerCatalogPreflightIntent = { fingerprint, idempotencyKey };
      providerCatalogPreflightForm.querySelector("button[type='submit']").disabled = true;
      providerCatalogActionError = null;
      providerCatalogFeedback = "正在运行受限预检";
      const result = await resourceMutationRequest(operation, preflightCatalogProvider, {
        controlApiUrl,
        ...input,
        idempotencyKey,
      });
      providerCatalogPreflightIntent = null;
      providerCatalogLastPreflight = result;
      providerCatalogFeedback = result.idempotent_replay ? "已恢复同一预检结果" : "预检已完成";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      if (!(error instanceof AuthApiError) || ![0, 503].includes(error.status)) {
        providerCatalogPreflightIntent = null;
      }
      providerCatalogActionError = error;
      providerCatalogFeedback = "预检失败";
      await refreshResourceMutation(operation);
    }
  });

  const createProviderForm = document.querySelector("#create-provider-form");
  const providerCreateBinding = bindResourceElement(createProviderForm, [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
  createProviderForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(providerCreateBinding);
    if (!operation) return;
    const button = createProviderForm.querySelector("button[type='submit']");
    const data = new FormData(createProviderForm);
    button.disabled = true;
    providerActionError = null;
    providerFeedback = "创建中";
    providerLastTestResult = null;
    try {
      await resourceMutationRequest(operation, createProvider, {
        controlApiUrl,
        providerCode: String(data.get("providerCode") ?? ""),
        name: String(data.get("name") ?? ""),
        providerType: String(data.get("providerType") ?? "openai_compatible"),
        baseUrl: String(data.get("baseUrl") ?? ""),
        authType: String(data.get("authType") ?? "bearer"),
        description: String(data.get("description") ?? ""),
      });
      providerFeedback = "创建完成";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      providerActionError = error;
      providerFeedback = "创建失败";
      await refreshResourceMutation(operation);
    }
  });

  for (const providerForm of document.querySelectorAll("[data-provider-status-form]")) {
    const providerStatusBinding = bindResourceElement(providerForm, [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
    providerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(providerStatusBinding);
      if (!operation) return;
      const button = providerForm.querySelector("button[type='submit']");
      const data = new FormData(providerForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      providerActionError = null;
      providerFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        await resourceMutationRequest(operation, updateProvider, {
          controlApiUrl,
          providerId: providerForm.dataset.providerId,
          status: nextStatus,
        });
        providerFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        providerActionError = error;
        providerFeedback = "操作失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const providerForm of document.querySelectorAll("[data-provider-test-form]")) {
    const providerTestBinding = bindResourceElement(providerForm, [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
    providerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(providerTestBinding);
      if (!operation) return;
      const button = providerForm.querySelector("button[type='submit']");
      button.disabled = true;
      providerActionError = null;
      providerFeedback = "测试中";
      try {
        const result = await resourceMutationRequest(operation, testProviderConnection, {
          controlApiUrl,
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
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        providerActionError = error;
        providerFeedback = "测试失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const providerKeyForm of document.querySelectorAll("[data-provider-key-create-form]")) {
    const providerKeyCreateBinding = bindResourceElement(providerKeyForm, [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
    providerKeyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(providerKeyCreateBinding);
      if (!operation) return;
      const button = providerKeyForm.querySelector("button[type='submit']");
      const data = new FormData(providerKeyForm);
      button.disabled = true;
      providerActionError = null;
      providerFeedback = "Key 创建中";
      try {
        await resourceMutationRequest(operation, createProviderKey, {
          controlApiUrl,
          providerId: providerKeyForm.dataset.providerId,
          name: String(data.get("name") ?? ""),
          plainValue: String(data.get("plainValue") ?? ""),
          weight: optionalNumber(data.get("weight"), 100),
        });
        providerFeedback = "Key 创建完成";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        providerActionError = error;
        providerFeedback = "Key 创建失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  for (const providerKeyForm of document.querySelectorAll("[data-provider-key-status-form]")) {
    const providerKeyStatusBinding = bindResourceElement(providerKeyForm, [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE], "providers");
    providerKeyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(providerKeyStatusBinding);
      if (!operation) return;
      const button = providerKeyForm.querySelector("button[type='submit']");
      const data = new FormData(providerKeyForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      providerActionError = null;
      providerFeedback = nextStatus === "disabled" ? "Key 禁用中" : "Key 启用中";
      try {
        await resourceMutationRequest(operation, updateProviderKey, {
          controlApiUrl,
          providerKeyId: providerKeyForm.dataset.providerKeyId,
          status: nextStatus,
        });
        providerFeedback = nextStatus === "disabled" ? "Key 已禁用" : "Key 已启用";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        providerActionError = error;
        providerFeedback = "Key 操作失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  const createModelForm = document.querySelector("#create-model-form");
  const modelCreateBinding = bindResourceElement(createModelForm, [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE], "models");
  createModelForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(modelCreateBinding);
    if (!operation) return;
    const button = createModelForm.querySelector("button[type='submit']");
    const data = new FormData(createModelForm);
    button.disabled = true;
    modelActionError = null;
    modelFeedback = "创建中";
    try {
      await resourceMutationRequest(operation, createModel, {
        controlApiUrl,
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
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      modelActionError = error;
      modelFeedback = "创建失败";
      await refreshResourceMutation(operation);
    }
  });

  for (const modelForm of document.querySelectorAll("[data-model-status-form]")) {
    const modelStatusBinding = bindResourceElement(modelForm, [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE], "models");
    modelForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(modelStatusBinding);
      if (!operation) return;
      const button = modelForm.querySelector("button[type='submit']");
      const data = new FormData(modelForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      modelActionError = null;
      modelFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        await resourceMutationRequest(operation, updateModel, {
          controlApiUrl,
          modelId: modelForm.dataset.modelId,
          status: nextStatus,
        });
        modelFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        modelActionError = error;
        modelFeedback = "操作失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  const createModelAliasForm = document.querySelector("#create-model-alias-form");
  const createModelAliasBinding = bindResourceElement(createModelAliasForm,
    [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "model-aliases");
  createModelAliasForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(createModelAliasBinding);
    if (!operation) return;
    const button = createModelAliasForm.querySelector("button[type='submit']");
    const data = new FormData(createModelAliasForm);
    button.disabled = true;
    modelAliasActionError = null;
    modelAliasFeedback = "创建中";
    try {
      await resourceMutationRequest(operation, createModelAlias, {
        controlApiUrl,
        alias: String(data.get("alias") ?? ""),
        environmentId: String(data.get("environmentId") ?? ""),
        environment: undefined,
        defaultModelId: String(data.get("defaultModelId") ?? ""),
        fallbackModelId: optionalText(data.get("fallbackModelId")),
        description: optionalText(data.get("description")),
      });
      modelAliasFeedback = "创建完成";
      await refreshResourceMutation(operation);
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      modelAliasActionError = error;
      modelAliasFeedback = "创建失败";
      await refreshResourceMutation(operation);
    }
  });

  for (const modelAliasForm of document.querySelectorAll("[data-model-alias-status-form]")) {
    const binding = bindResourceElement(modelAliasForm,
      [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "model-aliases");
    modelAliasForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(binding);
      if (!operation) return;
      const button = modelAliasForm.querySelector("button[type='submit']");
      const data = new FormData(modelAliasForm);
      const nextStatus = String(data.get("status") ?? "");
      button.disabled = true;
      modelAliasActionError = null;
      modelAliasFeedback = nextStatus === "disabled" ? "禁用中" : "启用中";
      try {
        await resourceMutationRequest(operation, updateModelAlias, {
          controlApiUrl,
          modelAliasId: modelAliasForm.dataset.modelAliasId,
          status: nextStatus,
          // Alias 启停会改变初始路由图，必须携带当前 RoutingPolicy version 做锁内 CAS。
          expectedRoutingPolicyVersion: Number(modelAliasForm.dataset.routingPolicyVersion),
        });
        modelAliasFeedback = nextStatus === "disabled" ? "已禁用" : "已启用";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        modelAliasActionError = error;
        modelAliasFeedback = "操作失败";
        await refreshResourceMutation(operation);
      }
    });
  }

  const dismissSecretButton = document.querySelector("[data-dismiss-virtual-key-secret]");
  const dismissSecretBinding = bindResourceElement(dismissSecretButton,
    [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE], "virtual-keys");
  dismissSecretButton?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(dismissSecretBinding)) return;
    virtualKeyCreatedSecret = null;
    dismissSecretButton.closest(".one-time-secret")?.replaceChildren();
    await refresh();
  });

  const copySecretButton = document.querySelector("[data-copy-virtual-key]");
  const copySecretBinding = bindResourceElement(copySecretButton,
    [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE], "virtual-keys");
  copySecretButton?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(copySecretBinding)) return;
    const value = virtualKeyCreatedSecret?.plainValue;
    if (!value || !navigator.clipboard?.writeText) {
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      if (!resourcePageLifecycle.isBindingCurrent(copySecretBinding)) return;
      virtualKeyFeedback = "复制失败";
      await refresh();
      return;
    }
    if (!resourcePageLifecycle.isBindingCurrent(copySecretBinding)) return;
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
  const syncWebhookFormatMode = () => {
    const selectedFormat = webhookForm
      ?.querySelector('input[name="format"]:checked')
      ?.value;
    applyWebhookFormatMode(webhookForm, selectedFormat);
  };
  for (const formatInput of webhookForm?.querySelectorAll('input[name="format"]') ?? []) {
    formatInput.addEventListener("change", syncWebhookFormatMode);
  }
  syncWebhookFormatMode();
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
        format: String(data.get("format") ?? "json"),
        securityEventRateLimitPerMinute: optionalNumber(
          data.get("securityEventRateLimitPerMinute"),
          60,
        ),
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


  const requestLogBinding = bindResourceElement(
    document.querySelector(".request-logs-panel, .output-safety-panel"),
    [PERMISSIONS.REQUEST_LOG_READ], consoleShellState?.activeNavigationItemId,
  );
  const requestLogEventCurrent = () => resourcePageLifecycle.isBindingCurrent(requestLogBinding);
  const requestLogFilterForm = document.querySelector("#request-log-filter-form");
  requestLogFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requestLogEventCurrent()) return;
    // FormData 只读取 PAGE-080 白名单；时间统一转 UTC，Decimal 保持字符串精度。
    configureCurrentRequestLogState(requestLogFiltersFromForm(new FormData(requestLogFilterForm)), null);
    await refresh();
  });

  const requestLogFilterReset = document.querySelector("#request-log-filter-reset");
  requestLogFilterReset?.addEventListener("click", async () => {
    if (!requestLogEventCurrent()) return;
    configureCurrentRequestLogState({ limit: 20, offset: 0 }, null);
    await refresh();
  });

  const outputSafetyFilterForm = document.querySelector("#output-safety-filter-form");
  outputSafetyFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!requestLogEventCurrent()) return;
    // terminalStage 由 PAGE-099 固定，表单或旧内存都不能把安全事件查询降级成普通全量日志。
    configureCurrentRequestLogState(
      outputSafetyFiltersFromForm(new FormData(outputSafetyFilterForm)),
      null,
    );
    await refresh();
  });

  document.querySelector("#output-safety-filter-reset")?.addEventListener("click", async () => {
    if (!requestLogEventCurrent()) return;
    configureCurrentRequestLogState({ terminalStage: "output_safety", limit: 20, offset: 0 }, null);
    await refresh();
  });

  const savedFilterSelect = document.querySelector("#request-log-saved-filter-select");
  savedFilterSelect?.addEventListener("change", () => {
    if (!requestLogEventCurrent()) return;
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
    if (!requestLogEventCurrent()) return;
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect?.value);
    if (!savedFilter) return;
    // 应用后恢复表单展示，但 GET 只发送 ID，让后端按当前 tenant + owner 加载权威筛选和排序。
    configureCurrentRequestLogState(requestLogFiltersFromSavedFilter(savedFilter), null);
    await refresh();
  });

  document.querySelector("#request-log-saved-filter-create")?.addEventListener("click", async () => {
    if (!requestLogEventCurrent()) return;
    const name = optionalText(document.querySelector("#request-log-saved-filter-name")?.value);
    const currentFilters = currentRequestLogFiltersFromForm();
    if (!currentFilters) return;
    await mutateCurrentSavedRequestLogFilter((signal) => createSavedRequestLogFilter({
      controlApiUrl,
      authentication: "workspace",
      name,
      filters: currentFilters,
      sortBy: currentFilters.sortBy,
      sortDirection: currentFilters.sortDirection,
      signal,
    }), { activeNavigationItemId: consoleShellState?.activeNavigationItemId, binding: requestLogBinding });
  });

  document.querySelector("#request-log-saved-filter-update")?.addEventListener("click", async () => {
    if (!requestLogEventCurrent()) return;
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect?.value);
    if (!savedFilter) return;
    const name = optionalText(document.querySelector("#request-log-saved-filter-name")?.value);
    const currentFilters = currentRequestLogFiltersFromForm();
    if (!currentFilters) return;
    await mutateCurrentSavedRequestLogFilter((signal) => updateSavedRequestLogFilter({
      controlApiUrl,
      authentication: "workspace",
      savedFilterId: savedFilter.saved_filter_id,
      version: savedFilter.version,
      name,
      filters: currentFilters,
      sortBy: currentFilters.sortBy,
      sortDirection: currentFilters.sortDirection,
      signal,
    }), { activeNavigationItemId: consoleShellState?.activeNavigationItemId, binding: requestLogBinding });
  });

  document.querySelector("#request-log-saved-filter-delete")?.addEventListener("click", async () => {
    if (!requestLogEventCurrent()) return;
    const savedFilter = currentSavedRequestLogFilter(savedFilterSelect?.value);
    if (!savedFilter) return;
    const confirmed = globalThis.window?.confirm?.(`删除保存筛选“${savedFilter.name}”？`) === true;
    if (!confirmed) return;
    await mutateCurrentSavedRequestLogFilter((signal) => deleteSavedRequestLogFilter({
      controlApiUrl,
      authentication: "workspace",
      savedFilterId: savedFilter.saved_filter_id,
      version: savedFilter.version,
      signal,
    }), {
      clearSelection: true,
      activeNavigationItemId: consoleShellState?.activeNavigationItemId,
      binding: requestLogBinding,
    });
  });

  const requestLogPrev = document.querySelector("#request-log-prev");
  requestLogPrev?.addEventListener("click", async () => {
    if (!requestLogEventCurrent()) return;
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
    if (!requestLogEventCurrent()) return;
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
        if (!button.isConnected || !requestLogEventCurrent()) return;
        configureCurrentRequestLogState(
          requestLogPageMemory.query().filters,
          button.dataset.requestLogDetail,
        );
        await refresh();
      });
    }

    const requestLogDetailRefresh = document.querySelector("[data-request-log-detail-refresh]");
    requestLogDetailRefresh?.addEventListener("click", async () => {
      if (!requestLogEventCurrent()) return;
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

  for (const button of document.querySelectorAll("[data-request-log-explain]")) {
    button.addEventListener("click", async () => {
      if (!button.isConnected || !requestLogEventCurrent()) {
        requestExplanationHandoff.clear();
        return;
      }
      const binding = bindResourceElement(document.querySelector(".request-logs-panel"),
        [PERMISSIONS.REQUEST_LOG_READ, PERMISSIONS.REQUEST_EXPLANATION_READ], "request-logs");
      await runRequestExplanationRead(button.dataset.requestLogExplain, "request_logs", binding,
        consoleShellState);
    });
  }

  const auditLogBinding = bindResourceElement(document.querySelector(".audit-logs-panel"),
    [PERMISSIONS.AUDIT_READ], "audit-logs");
  const auditLogFilterForm = document.querySelector("#audit-log-filter-form");
  auditLogFilterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!resourcePageLifecycle.isBindingCurrent(auditLogBinding)) return;
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
    if (!resourcePageLifecycle.isBindingCurrent(auditLogBinding)) return;
    auditLogFilterReset.disabled = true;
    clearAuditExportSessionState();
    // 重置必须清除全部筛选并回到第一页，不能保留旧时间范围或旧 offset。
    auditLogFilters = { limit: 20, offset: 0 };
    await refresh();
  });

  const auditLogPrev = document.querySelector("#audit-log-prev");
  auditLogPrev?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(auditLogBinding)) return;
    auditLogPrev.disabled = true;
    auditLogFilters = {
      ...auditLogFilters,
      offset: Math.max(Number(auditLogFilters.offset ?? 0) - Number(auditLogFilters.limit ?? 20), 0),
    };
    await refresh();
  });

  const auditLogNext = document.querySelector("#audit-log-next");
  auditLogNext?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(auditLogBinding)) return;
    auditLogNext.disabled = true;
    auditLogFilters = {
      ...auditLogFilters,
      offset: Number(auditLogFilters.offset ?? 0) + Number(auditLogFilters.limit ?? 20),
    };
    await refresh();
  });

  const auditLogExportButton = document.querySelector("#audit-log-export-csv");
  const auditExportBinding = bindResourceElement(auditLogExportButton,
    [PERMISSIONS.AUDIT_READ, PERMISSIONS.AUDIT_EXPORT], "audit-logs");
  auditLogExportButton?.addEventListener("click", async () => {
    const mutation = resourcePageLifecycle.beginMutation(auditExportBinding);
    if (!mutation) return;
    const operation = auditExportLifecycle.begin({
      identity: currentEvaluationPageIdentity("audit-logs", PERMISSIONS.AUDIT_EXPORT),
      navigation: currentRenderedNavigation(),
    });
    if (!operation) {
      resourcePageLifecycle.finishMutation(mutation);
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
        authentication: "workspace",
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
      if (!resourcePageLifecycle.isMutationCurrent(mutation) || !ownsAuditExportOperation(operation)) {
        return;
      }
      triggerAuditCsvDownload(result);
      if (!resourcePageLifecycle.isMutationCurrent(mutation) || !ownsAuditExportOperation(operation)) {
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
      if (isAbortError(error) || !resourcePageLifecycle.isMutationCurrent(mutation)
        || !ownsAuditExportOperation(operation)) {
        return;
      }
      if (handleResourceAuthorizationError(error)) return;
      setAuditExportState({
        status: "error",
        code: safeAuditExportErrorCode(error),
        requestId: safeAuditExportRequestId(error?.requestId, operationRequestId),
      });
    } finally {
      // 只有本 operation 可以释放自己的 owner；clear() 或新边界已经接管时 finish 不产生副作用。
      auditExportLifecycle.finish(operation);
      resourcePageLifecycle.finishMutation(mutation);
      if (resourcePageLifecycle.isBindingCurrent(auditExportBinding)) syncAuditExportDomState();
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
    const binding = bindResourceElement(fallbackForm,
      [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "model-aliases");
    fallbackForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(binding);
      if (!operation) return;
      const button = fallbackForm.querySelector("button[type='submit']");
      const data = new FormData(fallbackForm);
      button.disabled = true;
      modelAliasActionError = null;
      modelAliasFeedback = "Fallback 保存中";
      try {
        await resourceMutationRequest(operation, updateFallbackPolicy, {
          controlApiUrl,
          modelAliasId: fallbackForm.dataset.modelAliasId,
          enabled: data.get("enabled") === "on",
          fallbackModelId: optionalText(data.get("fallbackModelId")),
          triggerConditions: data.getAll("triggerCondition").map(String),
          clearFallbackModel: data.get("clearFallbackModel") === "on",
          // Fallback 目标与 PAGE-067 共用 Alias 候选图，不能绕过 RoutingPolicy version。
          expectedRoutingPolicyVersion: Number(fallbackForm.dataset.routingPolicyVersion),
        });
        modelAliasFeedback = "Fallback 已保存";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        modelAliasActionError = error;
        modelAliasFeedback = "Fallback 保存失败";
        await refreshResourceMutation(operation);
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
  bindFileSecurityPageInteractions(consoleShellState);
  bindBatchJobsPageInteractions(consoleShellState);
  bindTenantDataLifecyclePageInteractions(consoleShellState);
  bindMcpToolsPageInteractions(consoleShellState);
  bindLocalEndpointsPageInteractions(consoleShellState);
  bindPrivateGatewayPageInteractions(consoleShellState);
  bindMigrationOperationPageInteractions(consoleShellState);
  bindSupportTicketPageInteractions(consoleShellState);
  bindExperimentsPageInteractions(consoleShellState);
  bindResponseCachePageInteractions(consoleShellState);
  bindQuotaManagementPageInteractions(consoleShellState);
  bindWorkspaceConsoleExperiencePageInteractions(consoleShellState);
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
  const owner = filePageOwner(authSession.context, consoleShellState, { session: authSession });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "files")) return null;
  return owner;
}

/** 构造当前文件 read+write owner；只读身份或后端导航漂移固定返回 null。 */
function currentFileWriteOwner(consoleShellState) {
  const owner = filePageOwner(authSession.context, consoleShellState, { requireWrite: true, session: authSession });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "files")) return null;
  return owner;
}

/** 绑定文件上传、筛选和私有 cursor 翻页；按钮可见性不替代每次 owner 与后端 RBAC 校验。 */
function bindFilesPageInteractions(consoleShellState) {
  const readOwner = currentFileReadOwner(consoleShellState);
  if (!readOwner || !filePageState.enter(readOwner)) return;
  const binding = bindResourceElement(document.querySelector("#file-status-filter-form"), [PERMISSIONS.FILE_READ], "files");
  const uploadBinding = bindResourceElement(document.querySelector("#file-upload-form"),
    [PERMISSIONS.FILE_READ, PERMISSIONS.FILE_WRITE], "files");
  bindFileInteractions({
    isCurrent(element) {
      return element?.isConnected && resourcePageLifecycle.isBindingCurrent(binding);
    },
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
      await submitFileUpload(file, consoleShellState, uploadBinding);
    },
  });
}

/**
 * 提交一次文件上传并同步等待解析终态。
 * POST ticket 同时绑定 tenant、用户、Session、权限 revision/fingerprint 和后端 files 导航。
 */
async function submitFileUpload(file, consoleShellState, binding) {
  const resourceOperation = resourcePageLifecycle.beginMutation(binding);
  if (!resourceOperation) return;
  const owner = currentFileWriteOwner(consoleShellState);
  if (!owner) {
    resourcePageLifecycle.finishMutation(resourceOperation);
    return;
  }
  const operation = filePageState.beginUploadOperation(owner);
  if (!operation) {
    resourcePageLifecycle.finishMutation(resourceOperation);
    return;
  }
  let operationFinished = false;
  // 单一收口函数防止成功、异常和 finally 三条路径重复结束后来上传 ticket。
  const finishOperation = () => {
    if (operationFinished) return;
    operationFinished = true;
    filePageState.finishUploadOperation(operation);
    resourcePageLifecycle.finishMutation(resourceOperation);
  };
  try {
    const result = await uploadFile({
      controlApiUrl,
      authentication: "workspace",
      file,
      signal: operation.controller.signal,
    });
    if (!resourcePageLifecycle.isMutationCurrent(resourceOperation)) return;
    const currentOwner = currentFileWriteOwner(consoleShellState);
    if (!filePageState.acceptUpload(operation, result, currentOwner)) return;
    // 显式结束 POST 后再刷新列表，避免上传 busy 门禁把本轮结果误当成仍未决。
    finishOperation();
    await refresh();
  } catch (error) {
    const currentOwner = currentFileWriteOwner(consoleShellState);
    // owner 漂移或主动 Abort 的迟到错误必须静默丢弃，不能影响新租户页面。
    if (!resourcePageLifecycle.isMutationCurrent(resourceOperation)
      || !filePageState.isUploadOperationCurrent(operation, currentOwner) || isFileAbortError(error)) {
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

/** 构造 PAGE-103 read owner，并绑定浏览器 hash，避免旧 DOM 离页后发起报告 GET/CSV。 */
function currentFileSecurityReadOwner(consoleShellState) {
  const owner = fileSecurityPageOwner(authSession.context, consoleShellState);
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "file-security")) return null;
  return owner;
}

/** 构造 PAGE-103 read+scan owner；只读身份、撤权或导航漂移固定返回 null。 */
function currentFileSecurityScanOwner(consoleShellState) {
  const owner = fileSecurityPageOwner(authSession.context, consoleShellState, { requireScan: true });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "file-security")) return null;
  return owner;
}

/** 绑定 ready 文件选择、扫描和 CSV；DOM 可见性不替代每次 owner 与后端 RBAC 校验。 */
function bindFileSecurityPageInteractions(consoleShellState) {
  const owner = currentFileSecurityReadOwner(consoleShellState);
  if (!owner || !fileSecurityPageState.enter(owner)) return;
  bindFileSecurityInteractions({
    /** 文件 ID 必须由状态机确认属于当前 strict ready 列表，再触发 owner-bound 报告读取。 */
    async onSelect(fileId) {
      if (fileSecurityPageState.selectFile(fileId)) await refresh();
    },
    /** 上一页 cursor 只从 PAGE-103 私有栈恢复。 */
    async onPreviousPage() {
      if (fileSecurityPageState.previousPage()) await refresh();
    },
    /** 下一页只使用最近一次后端响应中保存的签名 cursor。 */
    async onNextPage() {
      if (fileSecurityPageState.nextPage()) await refresh();
    },
    /** 扫描命令在主应用中再次冻结复合权限 owner。 */
    async onScan() {
      await submitFileSecurityScan(consoleShellState);
    },
    /** CSV 读取仅要求 read owner，但后端仍逐请求复核租户与 file.read。 */
    async onExport() {
      await exportFileSecurityReport(consoleShellState);
    },
  });
}

/**
 * 提交一次 PAGE-103 安全扫描。
 * ticket 同时绑定 tenant、用户、Session、权限 revision/fingerprint、导航与选中文件。
 */
async function submitFileSecurityScan(consoleShellState) {
  const token = authSession.token;
  const owner = currentFileSecurityScanOwner(consoleShellState);
  // Token 或复合权限 owner 缺失都先销毁报告，不能仅依赖扫描按钮隐藏。
  if (!token || !owner) {
    fileSecurityPageState.clear();
    if (!token) renderLogin();
    return;
  }
  const operation = fileSecurityPageState.beginScanOperation(owner);
  if (!operation) return;
  let operationFinished = false;
  const finishOperation = () => {
    if (operationFinished) return;
    operationFinished = true;
    fileSecurityPageState.finishScanOperation(operation);
  };
  try {
    const result = await scanFileSecurity({
      controlApiUrl,
      token,
      fileId: operation.fileId,
      signal: operation.controller.signal,
    });
    const currentOwner = currentFileSecurityScanOwner(consoleShellState);
    if (!fileSecurityPageState.acceptScan(operation, result, currentOwner)) return;
    finishOperation();
    await refresh();
  } catch (error) {
    const currentOwner = currentFileSecurityScanOwner(consoleShellState);
    if (
      !fileSecurityPageState.isScanOperationCurrent(operation, currentOwner)
      || isFileSecurityAbortError(error)
    ) return;
    // 401 是强认证边界，必须在显示登录页前销毁当前 tenant 的文件名和安全事实。
    if (error instanceof AuthApiError && error.status === 401) {
      fileSecurityPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    if (
      error instanceof AuthApiError
      && error.status === 403
      && error.code === "auth.permission_denied"
    ) {
      // 撤权响应先销毁旧报告和文件名，再重读 /auth/me 与后端导航；业务阻断不能走该分支。
      fileSecurityPageState.clear();
      finishOperation();
      await refresh();
      return;
    }
    fileSecurityPageState.rejectScan(operation, error, currentOwner);
    finishOperation();
    await refresh();
  } finally {
    finishOperation();
  }
}

/**
 * 下载当前报告 CSV，并在同一调用栈内创建和 revoke Object URL。
 * Blob 不进入状态机；下载前后都核对 read owner、选中文件与 generation。
 */
async function exportFileSecurityReport(consoleShellState) {
  const token = authSession.token;
  const owner = currentFileSecurityReadOwner(consoleShellState);
  // CSV 命令必须绑定当前 read owner；缺失时即使旧 DOM 仍存在也不能发请求。
  if (!token || !owner) {
    // Token 或 read owner 不完整时立即失效页面；不能仅依赖 CSV 按钮是否存在。
    fileSecurityPageState.clear();
    // 只有 Token 缺失才切换登录页，撤权仍交给后端导航刷新收口。
    if (!token) renderLogin();
    return;
  }
  const operation = fileSecurityPageState.beginExportOperation(owner);
  if (!operation) return;
  let operationFinished = false;
  // 单一收口函数避免成功、异常和 finally 三条路径重复结束同一导出 ticket。
  const finishOperation = () => {
    if (operationFinished) return;
    operationFinished = true;
    fileSecurityPageState.finishExportOperation(operation);
  };
  try {
    const result = await downloadFileSecurityReportCsv({
      controlApiUrl,
      token,
      fileId: operation.fileId,
      signal: operation.controller.signal,
    });
    const currentOwner = currentFileSecurityReadOwner(consoleShellState);
    if (!fileSecurityPageState.isExportOperationCurrent(operation, currentOwner)) return;
    // trigger 内部 finally 会在 click 成功或失败后立即 revoke；调用方绝不保存 Blob URL。
    triggerFileSecurityReportCsvDownload(result);
    if (!fileSecurityPageState.acceptExport(operation, result, currentOwner)) return;
    finishOperation();
    await refresh();
  } catch (error) {
    const currentOwner = currentFileSecurityReadOwner(consoleShellState);
    if (
      !fileSecurityPageState.isExportOperationCurrent(operation, currentOwner)
      || isFileSecurityAbortError(error)
    ) return;
    // CSV 的 401 与 JSON 报告使用相同强认证清理边界。
    if (error instanceof AuthApiError && error.status === 401) {
      fileSecurityPageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    if (
      error instanceof AuthApiError
      && error.status === 403
      && error.code === "auth.permission_denied"
    ) {
      fileSecurityPageState.clear();
      finishOperation();
      await refresh();
      return;
    }
    fileSecurityPageState.rejectExport(operation, error, currentOwner);
    finishOperation();
    await refresh();
  } finally {
    finishOperation();
  }
}

/** Construct the current PAGE-114 owner from live auth, backend navigation and hash state. */
function currentMcpToolOwner(consoleShellState) {
  const owner = mcpToolPageOwner(authSession.context, consoleShellState);
  const hashNavigation = readActiveNavigationHash();
  if (!owner || hashNavigation !== "mcp-tools") return null;
  return owner;
}

/** Repaint only PAGE-114 after a local tab or Dialog operation. */
function renderCurrentMcpToolsPanel(consoleShellState) {
  const panel = globalThis.document?.querySelector?.(".mcp-tools-panel");
  const owner = currentMcpToolOwner(consoleShellState);
  if (!panel || !owner || !mcpToolPageState.enter(owner)) return false;
  panel.innerHTML = renderMcpTools(
    mcpToolPageState.snapshot(),
    authSession.context,
    consoleShellState,
  );
  bindMcpToolsPageInteractions(consoleShellState);
  return true;
}

/** Bind PAGE-114 commands; each callback rechecks the complete owner before creating a request. */
function bindMcpToolsPageInteractions(consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  if (!owner || !mcpToolPageState.enter(owner)) return false;
  return bindMcpToolInteractions({
    toolId: () => mcpToolPageState.snapshot().toolDialog?.toolId ?? "",
    networkServerId: () => mcpToolPageState.snapshot().networkDialog?.serverId ?? "",
    async onRefresh() {
      const generation = ++refreshSequence;
      const state = await loadMcpToolsState(consoleShellState, generation);
      if (state) renderCurrentMcpToolsPanel(consoleShellState);
    },
    async onTab(tab) {
      if (!mcpToolPageState.switchTab(currentMcpToolOwner(consoleShellState), tab)) return;
      const generation = ++refreshSequence;
      renderCurrentMcpToolsPanel(consoleShellState);
      const state = await loadMcpToolsState(consoleShellState, generation);
      if (state) renderCurrentMcpToolsPanel(consoleShellState);
    },
    async onPreviousPage() {
      if (!mcpToolPageState.previousPage(currentMcpToolOwner(consoleShellState))) return;
      const generation = ++refreshSequence;
      renderCurrentMcpToolsPanel(consoleShellState);
      const state = await loadMcpToolsState(consoleShellState, generation);
      if (state) renderCurrentMcpToolsPanel(consoleShellState);
    },
    async onNextPage() {
      if (!mcpToolPageState.nextPage(currentMcpToolOwner(consoleShellState))) return;
      const generation = ++refreshSequence;
      renderCurrentMcpToolsPanel(consoleShellState);
      const state = await loadMcpToolsState(consoleShellState, generation);
      if (state) renderCurrentMcpToolsPanel(consoleShellState);
    },
    async onOpenRegister() {
      if (!mcpToolPageState.openRegister(currentMcpToolOwner(consoleShellState))) return false;
      refreshSequence += 1;
      renderCurrentMcpToolsPanel(consoleShellState);
      return true;
    },
    async onCloseDialog() {
      const currentOwner = currentMcpToolOwner(consoleShellState);
      const snapshot = mcpToolPageState.snapshot();
      const closed = snapshot.networkDialog?.open
        ? mcpToolPageState.closeNetworkDialog(currentOwner)
        : snapshot.toolDialog?.open
          ? mcpToolPageState.closeToolDialog(currentOwner)
          : mcpToolPageState.closeRegister(currentOwner);
      if (closed) renderCurrentMcpToolsPanel(consoleShellState);
      return closed;
    },
    async onSubmitRegister(request) {
      return submitMcpServerRegistration(request, consoleShellState);
    },
    async onRefreshServer(serverId) {
      await submitMcpServerRefresh(serverId, consoleShellState);
    },
    async onServerStatus(serverId) {
      await submitMcpServerStatus(serverId, consoleShellState);
    },
    async onOpenTool(toolId, kind) {
      await loadMcpToolDialog(toolId, kind, consoleShellState);
    },
    async onSubmitPolicy(policy) {
      return submitMcpToolPolicy(policy, consoleShellState);
    },
    async onOpenNetworkPolicy(serverId) {
      if (!mcpToolPageState.openNetworkDialog(currentMcpToolOwner(consoleShellState), serverId)) {
        return false;
      }
      refreshSequence += 1;
      renderCurrentMcpToolsPanel(consoleShellState);
      return true;
    },
    async onSubmitNetworkPolicy(policy) {
      return submitMcpNetworkPolicy(policy, consoleShellState);
    },
  });
}

/** Submit registration while keeping Endpoint and Token out of page state and post-submit DOM. */
async function submitMcpServerRegistration(request, consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  const token = authSession.token;
  const ticket = mcpToolPageState.beginRegister(owner);
  if (!token || !ticket) return null;
  refreshSequence += 1;
  renderCurrentMcpToolsPanel(consoleShellState);
  try {
    const pending = registerMcpServer({
      controlApiUrl,
      token,
      name: request.name,
      endpointUrl: request.endpointUrl,
      authType: request.authType,
      bearerToken: request.bearerToken,
      idempotencyKey: ticket.idempotencyKey,
      signal: ticket.controller.signal,
    });
    // The client has synchronously constructed fetch; erase the last callback-owned plaintext references now.
    request.endpointUrl = "";
    request.bearerToken = "";
    const result = await pending;
    const currentOwner = currentMcpToolOwner(consoleShellState);
    mcpToolPageState.acceptRegister(ticket, result, currentOwner);
  } catch (error) {
    const currentOwner = currentMcpToolOwner(consoleShellState);
    if (isMcpToolAbortError(error)) return null;
    if (!mcpToolPageState.rejectRegister(ticket, error, currentOwner)) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
  } finally {
    request.endpointUrl = "";
    request.bearerToken = "";
    mcpToolPageState.finishRegister(ticket);
  }
  renderCurrentMcpToolsPanel(consoleShellState);
  return mcpToolPageState.snapshot();
}

/** Enqueue a tools/list refresh from a safe Server row summary. */
async function submitMcpServerRefresh(serverId, consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  const ticket = mcpToolPageState.beginServerRefresh(owner, serverId);
  if (!authSession.token || !ticket) return null;
  refreshSequence += 1;
  renderCurrentMcpToolsPanel(consoleShellState);
  try {
    const result = await refreshMcpServerTools({
      controlApiUrl,
      token: authSession.token,
      serverId: ticket.serverId,
      expectedRevision: ticket.expectedRevision,
      idempotencyKey: ticket.idempotencyKey,
      signal: ticket.controller.signal,
    });
    mcpToolPageState.acceptServerRefresh(
      ticket,
      result,
      currentMcpToolOwner(consoleShellState),
    );
  } catch (error) {
    if (isMcpToolAbortError(error)) return null;
    if (!mcpToolPageState.rejectServerRefresh(
      ticket,
      error,
      currentMcpToolOwner(consoleShellState),
    )) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
  } finally {
    mcpToolPageState.finishServerRefresh(ticket);
  }
  renderCurrentMcpToolsPanel(consoleShellState);
  return mcpToolPageState.snapshot();
}

/** Apply one Server enable/disable CAS from the owner-bound row snapshot. */
async function submitMcpServerStatus(serverId, consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  const ticket = mcpToolPageState.beginServerStatus(owner, serverId);
  if (!authSession.token || !ticket) return null;
  refreshSequence += 1;
  renderCurrentMcpToolsPanel(consoleShellState);
  try {
    const result = await updateMcpServerStatus({
      controlApiUrl,
      token: authSession.token,
      serverId: ticket.serverId,
      expectedRevision: ticket.expectedRevision,
      status: ticket.nextStatus,
      idempotencyKey: ticket.idempotencyKey,
      signal: ticket.controller.signal,
    });
    mcpToolPageState.acceptServerStatus(
      ticket,
      result,
      currentMcpToolOwner(consoleShellState),
    );
  } catch (error) {
    if (isMcpToolAbortError(error)) return null;
    if (!mcpToolPageState.rejectServerStatus(
      ticket,
      error,
      currentMcpToolOwner(consoleShellState),
    )) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
  } finally {
    mcpToolPageState.finishServerStatus(ticket);
  }
  renderCurrentMcpToolsPanel(consoleShellState);
  return mcpToolPageState.snapshot();
}

/** Load bounded canonical Schema or policy only after an explicit Tool-row command. */
async function loadMcpToolDialog(toolId, kind, consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  if (!mcpToolPageState.openToolDialog(owner, toolId, kind)) return null;
  refreshSequence += 1;
  renderCurrentMcpToolsPanel(consoleShellState);
  const ticket = mcpToolPageState.beginToolDetail(currentMcpToolOwner(consoleShellState));
  if (!authSession.token || !ticket) return null;
  try {
    const result = await getMcpTool({
      controlApiUrl,
      token: authSession.token,
      toolId: ticket.toolId,
      signal: ticket.controller.signal,
    });
    mcpToolPageState.acceptToolDetail(ticket, result, currentMcpToolOwner(consoleShellState));
  } catch (error) {
    if (isMcpToolAbortError(error)) return null;
    if (!mcpToolPageState.rejectToolDetail(
      ticket,
      error,
      currentMcpToolOwner(consoleShellState),
    )) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
  } finally {
    mcpToolPageState.finishToolDetail(ticket);
  }
  renderCurrentMcpToolsPanel(consoleShellState);
  return mcpToolPageState.snapshot();
}

/** Apply one policy CAS; Server/name/Schema fields cannot enter this callback. */
async function submitMcpToolPolicy(policy, consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  const ticket = mcpToolPageState.beginPolicyUpdate(owner);
  if (!authSession.token || !ticket) return null;
  refreshSequence += 1;
  renderCurrentMcpToolsPanel(consoleShellState);
  try {
    const result = await updateMcpToolPolicy({
      controlApiUrl,
      token: authSession.token,
      toolId: ticket.toolId,
      policy,
      signal: ticket.controller.signal,
    });
    mcpToolPageState.acceptPolicyUpdate(
      ticket,
      result,
      currentMcpToolOwner(consoleShellState),
    );
  } catch (error) {
    if (isMcpToolAbortError(error)) return null;
    if (!mcpToolPageState.rejectPolicyUpdate(
      ticket,
      error,
      currentMcpToolOwner(consoleShellState),
    )) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
  } finally {
    mcpToolPageState.finishPolicyUpdate(ticket);
  }
  renderCurrentMcpToolsPanel(consoleShellState);
  return mcpToolPageState.snapshot();
}

/** Apply one endpoint-only network policy CAS and explicitly rebase a stale safe draft. */
async function submitMcpNetworkPolicy(policy, consoleShellState) {
  const owner = currentMcpToolOwner(consoleShellState);
  const ticket = mcpToolPageState.beginNetworkPolicyUpdate(owner, policy);
  if (!authSession.token || !ticket) return null;
  refreshSequence += 1;
  renderCurrentMcpToolsPanel(consoleShellState);
  let conflict = false;
  try {
    const result = await updateMcpServerNetworkPolicy({
      controlApiUrl,
      token: authSession.token,
      serverId: ticket.serverId,
      policy: ticket.policy,
      signal: ticket.controller.signal,
    });
    mcpToolPageState.acceptNetworkPolicyUpdate(
      ticket,
      result,
      currentMcpToolOwner(consoleShellState),
    );
  } catch (error) {
    if (isMcpToolAbortError(error)) return null;
    if (!mcpToolPageState.rejectNetworkPolicyUpdate(
      ticket,
      error,
      currentMcpToolOwner(consoleShellState),
    )) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
    conflict = error instanceof AuthApiError && error.code === "mcp.state_conflict";
  } finally {
    mcpToolPageState.finishNetworkPolicyUpdate(ticket);
  }
  renderCurrentMcpToolsPanel(consoleShellState);
  if (conflict) {
    const generation = ++refreshSequence;
    const state = await loadMcpToolsState(consoleShellState, generation);
    if (state) renderCurrentMcpToolsPanel(consoleShellState);
  }
  return mcpToolPageState.snapshot();
}

/** Apply the same strong 401/403 boundary to every PAGE-114 lane. */
function handleMcpAuthenticationFailure(error) {
  if (!(error instanceof AuthApiError)) return false;
  if (error.status === 401) {
    mcpToolPageState.clear();
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin(authErrorMessage(error), error.requestId);
    return true;
  }
  if (error.status === 403) {
    mcpToolPageState.clear();
    globalThis.window?.setTimeout?.(() => void refresh(), 0);
    return true;
  }
  return false;
}

/** Construct PAGE-116 owner from the live auth, backend navigation, and current hash. */
function currentLocalEndpointOwner(consoleShellState) {
  const owner = localEndpointPageOwner(authSession.context, consoleShellState);
  const hashNavigation = readActiveNavigationHash();
  if (!owner || hashNavigation !== "local-models") return null;
  return owner;
}

/** Repaint only PAGE-116 after a local filter, Dialog, create, or test operation. */
function renderCurrentLocalEndpointsPanel(consoleShellState) {
  const panel = globalThis.document?.querySelector?.(".local-models-panel");
  const owner = currentLocalEndpointOwner(consoleShellState);
  if (!panel || !owner || !localEndpointPageState.enter(owner)) return false;
  panel.innerHTML = renderLocalEndpoints(
    localEndpointPageState.snapshot(),
    authSession.context,
    consoleShellState,
  );
  bindLocalEndpointsPageInteractions(consoleShellState);
  return true;
}

/** Bind PAGE-116 controls; every callback revalidates the complete live owner. */
function bindLocalEndpointsPageInteractions(consoleShellState) {
  const owner = currentLocalEndpointOwner(consoleShellState);
  if (!owner || !localEndpointPageState.enter(owner)) return false;
  return bindLocalEndpointInteractions({
    async onRefresh() {
      const generation = ++refreshSequence;
      const state = await loadLocalEndpointsState(consoleShellState, generation);
      if (state) renderCurrentLocalEndpointsPanel(consoleShellState);
    },
    async onFilters(filters) {
      if (!localEndpointPageState.setFilters(currentLocalEndpointOwner(consoleShellState), filters)) {
        return false;
      }
      renderCurrentLocalEndpointsPanel(consoleShellState);
      return true;
    },
    async onOpenCreate() {
      if (!localEndpointPageState.openCreate(currentLocalEndpointOwner(consoleShellState))) return false;
      refreshSequence += 1;
      renderCurrentLocalEndpointsPanel(consoleShellState);
      return true;
    },
    async onCloseCreate() {
      const closed = localEndpointPageState.closeCreate(currentLocalEndpointOwner(consoleShellState));
      if (closed) renderCurrentLocalEndpointsPanel(consoleShellState);
      return closed;
    },
    async onSubmitCreate(request) {
      return submitLocalEndpointCreate(request, consoleShellState);
    },
    async onTest(localEndpointId) {
      return submitLocalEndpointTest(localEndpointId, consoleShellState);
    },
  });
}

/** Create an Endpoint without retaining URL or token in state, errors, or post-submit DOM. */
async function submitLocalEndpointCreate(request, consoleShellState) {
  const owner = currentLocalEndpointOwner(consoleShellState);
  const token = authSession.token;
  const operation = localEndpointPageState.beginCreate(owner);
  if (!token || !operation) return null;
  refreshSequence += 1;
  renderCurrentLocalEndpointsPanel(consoleShellState);
  try {
    const pending = createLocalEndpoint({
      controlApiUrl,
      token,
      tenantId: owner.tenantId,
      request,
      requestId: operation.requestId,
      signal: operation.controller.signal,
    });
    // requestJson has synchronously serialized the body; remove the remaining callback-owned plaintext now.
    request.base_url = "";
    if (Object.hasOwn(request, "bearer_token")) request.bearer_token = "";
    const result = await pending;
    localEndpointPageState.acceptCreate(
      operation,
      result,
      currentLocalEndpointOwner(consoleShellState),
    );
  } catch (error) {
    const currentOwner = currentLocalEndpointOwner(consoleShellState);
    if (isLocalEndpointAbortError(error)) return null;
    if (!localEndpointPageState.rejectCreate(operation, error, currentOwner)) return null;
    if (handleLocalEndpointAuthenticationFailure(error)) return null;
  } finally {
    request.base_url = "";
    if (Object.hasOwn(request, "bearer_token")) request.bearer_token = "";
    localEndpointPageState.finishCreate(operation);
  }
  renderCurrentLocalEndpointsPanel(consoleShellState);
  return localEndpointPageState.snapshot();
}

/** Test one accepted row using an exact empty request body and owner-bound operation. */
async function submitLocalEndpointTest(localEndpointId, consoleShellState) {
  const owner = currentLocalEndpointOwner(consoleShellState);
  const operation = localEndpointPageState.beginTest(owner, localEndpointId);
  if (!authSession.token || !operation) return null;
  refreshSequence += 1;
  renderCurrentLocalEndpointsPanel(consoleShellState);
  try {
    const result = await testLocalEndpoint({
      controlApiUrl,
      token: authSession.token,
      tenantId: owner.tenantId,
      localEndpointId: operation.localEndpointId,
      signal: operation.controller.signal,
    });
    localEndpointPageState.acceptTest(
      operation,
      result,
      currentLocalEndpointOwner(consoleShellState),
    );
  } catch (error) {
    const currentOwner = currentLocalEndpointOwner(consoleShellState);
    if (isLocalEndpointAbortError(error)) return null;
    if (!localEndpointPageState.rejectTest(operation, error, currentOwner)) return null;
    if (handleLocalEndpointAuthenticationFailure(error)) return null;
  } finally {
    localEndpointPageState.finishTest(operation);
  }
  renderCurrentLocalEndpointsPanel(consoleShellState);
  return localEndpointPageState.snapshot();
}

/** Apply the strong 401/403 boundary after the current ticket has accepted the fixed error. */
function handleLocalEndpointAuthenticationFailure(error) {
  if (!(error instanceof AuthApiError)) return false;
  if (error.status === 401) {
    localEndpointPageState.clear();
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin(authErrorMessage(error), error.requestId);
    return true;
  }
  if (error.status === 403) {
    localEndpointPageState.clear();
    globalThis.window?.setTimeout?.(() => void refresh(), 0);
    return true;
  }
  return false;
}

/** Construct the PRD-120 owner from live auth, backend navigation, and current hash. */
function currentPrivateGatewayOwner(consoleShellState) {
  const owner = privateGatewayPageOwner(authSession.context, consoleShellState);
  return owner && readActiveNavigationHash() === "private-gateways" ? owner : null;
}

function renderCurrentPrivateGatewayPanel(consoleShellState) {
  const panel = globalThis.document?.querySelector?.(".private-gateways-panel");
  const owner = currentPrivateGatewayOwner(consoleShellState);
  if (!panel || !owner || !privateGatewayPageState.enter(owner)) return false;
  privateGatewayBrandingShellState = consoleShellState;
  panel.innerHTML = renderPrivateGateways(
    {
      ...privateGatewayPageState.snapshot(),
      deploymentBranding: deploymentBrandingController.snapshot(),
    },
    authSession.context,
    consoleShellState,
  );
  bindPrivateGatewayPageInteractions(consoleShellState);
  return true;
}

function bindPrivateGatewayPageInteractions(consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  if (!owner || !privateGatewayPageState.enter(owner)) return false;
  const bound = bindPrivateGatewayInteractions({
    async onRefresh() {
      const generation = ++refreshSequence;
      const state = await loadPrivateGatewaysState(consoleShellState, generation);
      if (state) renderCurrentPrivateGatewayPanel(consoleShellState);
    },
    async onInstallerSelection(input) {
      const selected = privateGatewayPageState.selectInstallerEnvironment(
        currentPrivateGatewayOwner(consoleShellState),
        input,
      );
      if (selected) renderCurrentPrivateGatewayPanel(consoleShellState);
      return selected;
    },
    async onInstallerDownload(downloadPath) {
      return downloadPrivateGatewayInstallerArtifact(downloadPath, consoleShellState);
    },
    async onOpen() {
      if (!privateGatewayPageState.openCreate(currentPrivateGatewayOwner(consoleShellState))) return false;
      refreshSequence += 1;
      renderCurrentPrivateGatewayPanel(consoleShellState);
      return true;
    },
    async onClose() {
      const closed = privateGatewayPageState.closeCreate(currentPrivateGatewayOwner(consoleShellState));
      if (closed) renderCurrentPrivateGatewayPanel(consoleShellState);
      return closed;
    },
    async onCreate(input) { return submitPrivateGatewayCreate(input, consoleShellState); },
    async onCopy() { return copyPrivateGatewayRegistration(consoleShellState); },
    async onBrandSelect(deploymentId) {
      const selected = await deploymentBrandingController.select(deploymentId);
      renderCurrentPrivateGatewayPanel(consoleShellState);
      return selected;
    },
    async onStateOpen(deploymentId, action) {
      if (!privateGatewayPageState.openStateCommand(
        currentPrivateGatewayOwner(consoleShellState),
        deploymentId,
        action,
      )) return false;
      refreshSequence += 1;
      renderCurrentPrivateGatewayPanel(consoleShellState);
      return true;
    },
    async onStateClose() {
      const closed = privateGatewayPageState.closeStateCommand(
        currentPrivateGatewayOwner(consoleShellState),
      );
      if (closed) renderCurrentPrivateGatewayPanel(consoleShellState);
      return closed;
    },
    async onStateCommand(input) {
      return submitPrivateGatewayStateCommand(input, consoleShellState);
    },
    async onTelemetryPolicy(deploymentId, telemetryLevel) {
      return submitPrivateGatewayTelemetryPolicy(
        deploymentId,
        telemetryLevel,
        consoleShellState,
      );
    },
    async onUpgradeMaintenance(input) {
      return submitPrivateGatewayMaintenance(input, consoleShellState);
    },
    async onUpgradeRollback(input) {
      return submitPrivateGatewayUpgradeRollback(input, consoleShellState);
    },
    async onLicenseRefresh(deploymentId) {
      return submitPrivateGatewayLicenseRefresh(deploymentId, consoleShellState);
    },
    async onDrillMutation(input) {
      return submitPrivateGatewayDrillMutation(input, consoleShellState);
    },
    async onLocalBackup(input) {
      return submitPrivateGatewayLocalBackup(input, consoleShellState);
    },
    async onLocalRestore(input) {
      return submitPrivateGatewayLocalRestore(input, consoleShellState);
    },
    async onValidation(deploymentId, action) {
      return submitPrivateGatewayValidation(deploymentId, action, consoleShellState);
    },
    async onAcceptanceOpen(deploymentId) {
      if (!privateGatewayPageState.openDeliveryAcceptance(
        currentPrivateGatewayOwner(consoleShellState),
        deploymentId,
      )) return false;
      refreshSequence += 1;
      renderCurrentPrivateGatewayPanel(consoleShellState);
      return true;
    },
    async onAcceptanceClose() {
      const closed = privateGatewayPageState.closeDeliveryAcceptance(
        currentPrivateGatewayOwner(consoleShellState),
      );
      if (closed) renderCurrentPrivateGatewayPanel(consoleShellState);
      return closed;
    },
    async onAcceptance(input) {
      return submitPrivateGatewayDeliveryAcceptance(input, consoleShellState);
    },
    async onSupportBundle(deploymentId, action) {
      return submitPrivateGatewaySupportBundle(deploymentId, action, consoleShellState);
    },
    async onDiagnosticOpen(deploymentId, grantId, action) {
      if (!privateGatewayPageState.openDiagnosticDecision(
        currentPrivateGatewayOwner(consoleShellState),
        deploymentId,
        grantId,
        action,
      )) return false;
      refreshSequence += 1;
      renderCurrentPrivateGatewayPanel(consoleShellState);
      return true;
    },
    async onDiagnosticClose() {
      const closed = privateGatewayPageState.closeDiagnosticDecision(
        currentPrivateGatewayOwner(consoleShellState),
      );
      if (closed) renderCurrentPrivateGatewayPanel(consoleShellState);
      return closed;
    },
    async onDiagnosticDecision(input) {
      return submitPrivateGatewayDiagnosticDecision(input, consoleShellState);
    },
    async onSecretOpen(deploymentId, action, referenceId) {
      if (!privateGatewayPageState.openSecretDialog(
        currentPrivateGatewayOwner(consoleShellState),
        deploymentId,
        action,
        referenceId,
      )) return false;
      refreshSequence += 1;
      renderCurrentPrivateGatewayPanel(consoleShellState);
      return true;
    },
    async onSecretClose() {
      const closed = privateGatewayPageState.closeSecretDialog(
        currentPrivateGatewayOwner(consoleShellState),
      );
      if (closed) renderCurrentPrivateGatewayPanel(consoleShellState);
      return closed;
    },
    async onLocalSession(input) {
      return submitPrivateGatewayLocalSession(input, consoleShellState);
    },
    async onSecretMutation(input) {
      return submitPrivateGatewaySecretMutation(input, consoleShellState);
    },
    async onSecretCommand(input) {
      if (input.action === "delete") {
        const confirmed = globalThis.confirm?.(
          "仅当配置、LKG、回滚和备份均未引用时才能删除。确认继续？",
        ) === true;
        if (!confirmed) return false;
      }
      return submitPrivateGatewaySecretMutation(input, consoleShellState);
    },
    async onRevoke(deploymentId, reasonCode) {
      const confirmed = globalThis.confirm?.("吊销会立即终止该 Deployment 的活跃 Edge Session。确认继续？") === true;
      return confirmed ? submitPrivateGatewayRevoke(deploymentId, reasonCode, consoleShellState) : false;
    },
  });
  bindBrandEditorInteractions(globalThis.document, {
    onCommand(input) {
      return input.scope === "deployment-brand"
        ? deploymentBrandingController.mutate(input)
        : false;
    },
  });
  return bound;
}

function downloadPrivateGatewayInstallerArtifact(downloadPath, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  if (!owner?.canReadInstallers) return false;
  let url;
  try {
    url = edgeInstallerArtifactUrl({
      controlApiUrl: workspaceControlApiUrl,
      downloadPath,
    });
  } catch {
    return false;
  }
  const anchor = globalThis.document?.createElement?.("a");
  if (!anchor) return false;
  anchor.href = url;
  anchor.download = "";
  anchor.rel = "noopener";
  anchor.hidden = true;
  globalThis.document.body?.append?.(anchor);
  try { anchor.click?.(); } finally { anchor.remove?.(); }
  return true;
}

async function submitPrivateGatewayValidation(deploymentId, action, consoleShellState) {
  const operation = privateGatewayPageState.beginValidation(
    currentPrivateGatewayOwner(consoleShellState),
    deploymentId,
    action,
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = operation.action === "refresh"
      ? await getEdgeValidation({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId: operation.deploymentId,
        validationRunId: operation.validationRunId,
        signal: operation.controller.signal,
      })
      : await startEdgeValidation({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId: operation.deploymentId,
        action: operation.action,
        sourceValidationRunId: operation.action === "retry_failed"
          ? operation.validationRunId
          : null,
        idempotencyKey: createEdgeIdempotencyKey(`validation-${operation.action}`),
        signal: operation.controller.signal,
      });
    privateGatewayPageState.acceptValidation(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectValidation(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishValidation(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayDeliveryAcceptance(input, consoleShellState) {
  const operation = privateGatewayPageState.beginDeliveryAcceptance(
    currentPrivateGatewayOwner(consoleShellState),
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  let verificationCode = String(input?.verificationCode ?? "");
  let stepUpGrant = "";
  let webAuthnCredential = null;
  try {
    let challengeId;
    if (input?.method === "webauthn") {
      const challenge = await beginIdentityWebAuthnStepUp({
        controlApiUrl: workspaceControlApiUrl,
        signal: operation.controller.signal,
      });
      if (!privateGatewayPageState.isDeliveryAcceptanceCurrent(
        operation,
        currentPrivateGatewayOwner(consoleShellState),
      )) return null;
      challengeId = challenge.challenge_id;
      webAuthnCredential = await getWebAuthnAssertion(challenge.public_key_options);
      if (!privateGatewayPageState.isDeliveryAcceptanceCurrent(
        operation,
        currentPrivateGatewayOwner(consoleShellState),
      )) return null;
    }
    const stepUp = await createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      method: input?.method,
      workspaceId: operation.workspaceId,
      actionCode: "edge.delivery.accept",
      resourceType: "edge_deployment",
      resourceId: operation.deploymentId,
      code: verificationCode,
      challengeId,
      credential: webAuthnCredential,
      signal: operation.controller.signal,
    });
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = stepUp.grant;
    if (!privateGatewayPageState.isDeliveryAcceptanceCurrent(
      operation,
      currentPrivateGatewayOwner(consoleShellState),
    )) return null;
    const result = await acceptEdgeDelivery({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      validationRunId: operation.validationRunId,
      expectedRevision: operation.expectedRevision,
      idempotencyKey: createEdgeIdempotencyKey("delivery-acceptance"),
      stepUpGrant,
      signal: operation.controller.signal,
    });
    stepUpGrant = "";
    privateGatewayPageState.acceptDeliveryAcceptance(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    verificationCode = "";
    stepUpGrant = "";
    webAuthnCredential = null;
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectDeliveryAcceptance(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    verificationCode = "";
    stepUpGrant = "";
    webAuthnCredential = null;
    privateGatewayPageState.finishDeliveryAcceptance(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayDiagnosticDecision(input, consoleShellState) {
  const operation = privateGatewayPageState.beginDiagnosticDecision(
    currentPrivateGatewayOwner(consoleShellState),
    input?.ttlSeconds,
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  let verificationCode = String(input?.verificationCode ?? "");
  let stepUpGrant = "";
  let webAuthnCredential = null;
  try {
    let challengeId;
    if (input?.method === "webauthn") {
      const challenge = await beginIdentityWebAuthnStepUp({
        controlApiUrl: workspaceControlApiUrl,
        signal: operation.controller.signal,
      });
      if (!privateGatewayPageState.isDiagnosticDecisionCurrent(
        operation,
        currentPrivateGatewayOwner(consoleShellState),
      )) return null;
      challengeId = challenge.challenge_id;
      webAuthnCredential = await getWebAuthnAssertion(challenge.public_key_options);
      if (!privateGatewayPageState.isDiagnosticDecisionCurrent(
        operation,
        currentPrivateGatewayOwner(consoleShellState),
      )) return null;
    }
    const stepUp = await createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      method: input?.method,
      workspaceId: operation.workspaceId,
      actionCode: operation.action === "approve"
        ? "edge.diagnostic.approve_grant"
        : "edge.diagnostic.revoke_grant",
      resourceType: "edge_diagnostic_grant",
      resourceId: operation.grantId,
      code: verificationCode,
      challengeId,
      credential: webAuthnCredential,
      signal: operation.controller.signal,
    });
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = stepUp.grant;
    if (!privateGatewayPageState.isDiagnosticDecisionCurrent(
      operation,
      currentPrivateGatewayOwner(consoleShellState),
    )) return null;
    const result = await decideWorkspaceDiagnosticGrant({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      grantId: operation.grantId,
      action: operation.action,
      reasonCode: operation.action === "approve" ? "customer_approved" : "customer_revoked",
      ttlSeconds: operation.ttlSeconds,
      stepUpGrant,
      idempotencyKey: createEdgeIdempotencyKey(`diagnostic-${operation.action}`),
      signal: operation.controller.signal,
    });
    stepUpGrant = "";
    privateGatewayPageState.acceptDiagnosticDecision(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    verificationCode = "";
    stepUpGrant = "";
    webAuthnCredential = null;
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectDiagnosticDecision(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    verificationCode = "";
    stepUpGrant = "";
    webAuthnCredential = null;
    privateGatewayPageState.finishDiagnosticDecision(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewaySupportBundle(deploymentId, action, consoleShellState) {
  const operation = privateGatewayPageState.beginSupportBundle(
    currentPrivateGatewayOwner(consoleShellState),
    deploymentId,
    action,
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    let result;
    if (operation.action === "preview") {
      result = await previewLocalSupportBundle({
        deploymentId: operation.deploymentId,
        signal: operation.controller.signal,
      });
    } else if (operation.action === "approve") {
      const local = await approveLocalSupportBundle({
        deploymentId: operation.deploymentId,
        bundleId: operation.bundleId,
        revision: operation.revision,
        signal: operation.controller.signal,
      });
      let cloud = null;
      try {
        cloud = await getEdgeSupportBundle({
          controlApiUrl: workspaceControlApiUrl,
          bundleId: local.cloud_object_reference,
          signal: operation.controller.signal,
        });
      } catch (error) {
        if (isPrivateGatewayAbortError(error)) throw error;
      }
      result = Object.freeze({ local, cloud });
    } else if (operation.action === "cancel") {
      result = await cancelLocalSupportBundle({
        deploymentId: operation.deploymentId,
        bundleId: operation.bundleId,
        revision: operation.revision,
        signal: operation.controller.signal,
      });
    } else {
      result = await getEdgeSupportBundle({
        controlApiUrl: workspaceControlApiUrl,
        bundleId: operation.bundleId,
        signal: operation.controller.signal,
      });
    }
    privateGatewayPageState.acceptSupportBundle(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectSupportBundle(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishSupportBundle(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayLocalSession(input, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginLocalSession(owner, input.localApiOrigin);
  if (!operation) {
    input.handoffCredential?.fill?.(0);
    return null;
  }
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const previous = privateGatewayPageState.snapshot().localSession;
    if (previous && previous.localApiOrigin !== operation.localApiOrigin) {
      await deleteLocalAdminSession({ localApiOrigin: previous.localApiOrigin });
    }
    const result = await createLocalAdminSession({
      localApiOrigin: operation.localApiOrigin,
      deploymentId: operation.deploymentId,
      accountId: operation.accountId,
      tenantId: operation.tenantId,
      workspaceId: operation.workspaceId,
      handoffCredential: input.handoffCredential,
      signal: operation.controller.signal,
    });
    const accepted = privateGatewayPageState.acceptLocalSession(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
    if (!accepted) {
      await deleteLocalAdminSession({ localApiOrigin: result.local_api_origin });
    }
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectLocalSession(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    input.handoffCredential?.fill?.(0);
    privateGatewayPageState.finishLocalSession(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewaySecretMutation(input, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginSecretMutation(owner, input.action, input);
  if (!operation) {
    input.secretBytes?.fill?.(0);
    return null;
  }
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    let result;
    if (operation.action === "create") {
      result = await createLocalSecret({
        deploymentId: operation.deploymentId,
        ...operation.request,
        secretBytes: input.secretBytes,
        signal: operation.controller.signal,
      });
    } else if (operation.action === "rotate") {
      result = await rotateLocalSecret({
        deploymentId: operation.deploymentId,
        ...operation.request,
        secretBytes: input.secretBytes,
        signal: operation.controller.signal,
      });
    } else if (operation.action === "delete") {
      result = await deleteLocalSecret({
        deploymentId: operation.deploymentId,
        ...operation.request,
        signal: operation.controller.signal,
      });
    } else {
      result = await testLocalProviderConnection({
        deploymentId: operation.deploymentId,
        providerConnectionId: operation.request.providerConnectionId,
        signal: operation.controller.signal,
      });
    }
    privateGatewayPageState.acceptSecretMutation(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectSecretMutation(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
      if (error?.code === "local_admin.authentication_failed") clearLocalAdminSession();
    }
  } finally {
    input.secretBytes?.fill?.(0);
    privateGatewayPageState.finishSecretMutation(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayTelemetryPolicy(
  deploymentId,
  telemetryLevel,
  consoleShellState,
) {
  const operation = privateGatewayPageState.beginTelemetryPolicy(
    currentPrivateGatewayOwner(consoleShellState),
    deploymentId,
    telemetryLevel,
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await patchEdgeTelemetryPolicy({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      telemetryLevel: operation.telemetryLevel,
      expectedRevision: operation.expectedRevision,
      idempotencyKey: createEdgeIdempotencyKey("telemetry-policy"),
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptTelemetryPolicy(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectTelemetryPolicy(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishTelemetryPolicy(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayMaintenance(input, consoleShellState) {
  const operation = privateGatewayPageState.beginMaintenance(
    currentPrivateGatewayOwner(consoleShellState),
    input?.deploymentId,
    input,
    createEdgeIdempotencyKey("upgrade-maintenance"),
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await updateWorkspaceMaintenance({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      ...operation.request,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptMaintenance(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectMaintenance(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishMaintenance(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayLicenseRefresh(deploymentId, consoleShellState) {
  const operation = privateGatewayPageState.beginLicenseRefresh(
    currentPrivateGatewayOwner(consoleShellState),
    deploymentId,
    createEdgeIdempotencyKey("license-refresh"),
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await refreshWorkspaceLicense({
      controlApiUrl: workspaceControlApiUrl,
      licenseId: operation.licenseId,
      expectedRevision: operation.expectedRevision,
      reasonCode: operation.reasonCode,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptLicenseRefresh(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectLicenseRefresh(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishLicenseRefresh(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayDrillMutation(input, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const action = String(input?.action ?? "");
  const operation = privateGatewayPageState.beginDrillMutation(
    owner,
    input,
    createEdgeIdempotencyKey(`drill-${action}`),
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  let verificationCode = String(input?.verificationCode ?? "");
  let webAuthnCredential = null;
  let stepUpGrant = "";
  try {
    let challengeId;
    if (input?.method === "webauthn") {
      const challenge = await beginIdentityWebAuthnStepUp({
        controlApiUrl: workspaceControlApiUrl,
        signal: operation.controller.signal,
      });
      if (!privateGatewayPageState.isDrillMutationCurrent(
        operation, currentPrivateGatewayOwner(consoleShellState),
      )) return null;
      challengeId = challenge.challenge_id;
      webAuthnCredential = await getWebAuthnAssertion(challenge.public_key_options);
      if (!privateGatewayPageState.isDrillMutationCurrent(
        operation, currentPrivateGatewayOwner(consoleShellState),
      )) return null;
    }
    const actionCode = action === "request" ? "edge.drill.request" : "edge.restore.approve";
    const stepUp = await createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      method: input?.method,
      workspaceId: operation.workspaceId,
      actionCode,
      resourceType: action === "request" ? "edge_deployment" : "private_dr_drill",
      resourceId: action === "request"
        ? operation.deploymentId : operation.request.drillId,
      code: verificationCode,
      challengeId,
      credential: webAuthnCredential,
      signal: operation.controller.signal,
    });
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = stepUp.grant;
    if (!privateGatewayPageState.isDrillMutationCurrent(
      operation, currentPrivateGatewayOwner(consoleShellState),
    )) return null;
    const result = await mutateWorkspaceRestoreDrill({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      ...operation.request,
      stepUpGrant,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    stepUpGrant = "";
    privateGatewayPageState.acceptDrillMutation(
      operation, result, currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = "";
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectDrillMutation(
        operation, error, currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = "";
    privateGatewayPageState.finishDrillMutation(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayLocalBackup(input, consoleShellState) {
  const operation = privateGatewayPageState.beginLocalBackup(
    currentPrivateGatewayOwner(consoleShellState), input,
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await createLocalBackup({
      deploymentId: operation.deploymentId,
      ...operation.request,
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptLocalBackup(
      operation, result, currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectLocalBackup(
        operation, error, currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishLocalBackup(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayLocalRestore(input, consoleShellState) {
  const operation = privateGatewayPageState.beginLocalRestore(
    currentPrivateGatewayOwner(consoleShellState), input,
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await restoreLocalBackup({
      deploymentId: operation.deploymentId,
      ...operation.request,
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptLocalRestore(
      operation, result, currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectLocalRestore(
        operation, error, currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishLocalRestore(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayUpgradeRollback(input, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginUpgradeRollback(
    owner,
    input?.deploymentId,
    input?.targetReleaseId || null,
    input?.reasonCode,
    createEdgeIdempotencyKey("upgrade-rollback"),
  );
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  let verificationCode = String(input?.verificationCode ?? "");
  let webAuthnCredential = null;
  let stepUpGrant = "";
  try {
    let challengeId;
    if (input?.method === "webauthn") {
      const challenge = await beginIdentityWebAuthnStepUp({
        controlApiUrl: workspaceControlApiUrl,
        signal: operation.controller.signal,
      });
      if (!privateGatewayPageState.isUpgradeRollbackCurrent(
        operation,
        currentPrivateGatewayOwner(consoleShellState),
      )) return null;
      challengeId = challenge.challenge_id;
      webAuthnCredential = await getWebAuthnAssertion(challenge.public_key_options);
      if (!privateGatewayPageState.isUpgradeRollbackCurrent(
        operation,
        currentPrivateGatewayOwner(consoleShellState),
      )) return null;
    }
    const stepUp = await createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      method: input?.method,
      workspaceId: operation.workspaceId,
      actionCode: "edge.rollback.request",
      resourceType: "edge_deployment",
      resourceId: operation.deploymentId,
      code: verificationCode,
      challengeId,
      credential: webAuthnCredential,
      signal: operation.controller.signal,
    });
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = stepUp.grant;
    if (!privateGatewayPageState.isUpgradeRollbackCurrent(
      operation,
      currentPrivateGatewayOwner(consoleShellState),
    )) return null;
    await requestWorkspaceRollback({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      targetReleaseId: operation.targetReleaseId,
      expectedHeadRevision: operation.expectedHeadRevision,
      reasonCode: operation.reasonCode,
      stepUpGrant,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    stepUpGrant = "";
    if (!privateGatewayPageState.isUpgradeRollbackCurrent(
      operation,
      currentPrivateGatewayOwner(consoleShellState),
    )) return null;
    const result = await getWorkspaceUpgrade({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptUpgradeRollback(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = "";
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectUpgradeRollback(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = "";
    privateGatewayPageState.finishUpgradeRollback(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayStateCommand(input, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginStateCommand(owner, input);
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const common = {
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      expectedRevision: operation.expectedRevision,
      idempotencyKey: createEdgeIdempotencyKey(`desired-${operation.action}`),
      signal: operation.controller.signal,
    };
    let result;
    if (operation.action === "publish") {
      result = await publishEdgeDesiredState({ ...common, ...operation.request });
    } else if (["approve", "reject"].includes(operation.action)) {
      result = await decideEdgeDesiredState({
        ...common,
        ...operation.request,
        action: operation.action,
      });
    } else {
      result = await rollbackEdgeDesiredState({ ...common, ...operation.request });
    }
    privateGatewayPageState.acceptStateCommand(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectStateCommand(
        operation,
        error,
        currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishStateCommand(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function submitPrivateGatewayCreate(input, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginCreate(owner);
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await createEdgeRegistration({
      controlApiUrl: workspaceControlApiUrl,
      ...input,
      idempotencyKey: createEdgeIdempotencyKey("registration"),
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptCreate(
      operation, result, currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectCreate(
        operation, error, currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishCreate(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function copyPrivateGatewayRegistration(consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginCopy(owner);
  if (!operation) return false;
  let copyText = operation.text;
  try {
    const pending = writePrivateGatewayClipboard(copyText);
    copyText = "";
    await pending;
    privateGatewayPageState.acceptCopy(
      operation, currentPrivateGatewayOwner(consoleShellState),
    );
  } catch {
    copyText = "";
    privateGatewayPageState.rejectCopy(
      operation, currentPrivateGatewayOwner(consoleShellState),
    );
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

async function writePrivateGatewayClipboard(value) {
  if (globalThis.navigator?.clipboard?.writeText) {
    await globalThis.navigator.clipboard.writeText(value);
    return;
  }
  const textarea = globalThis.document?.createElement?.("textarea");
  if (!textarea || typeof globalThis.document?.execCommand !== "function") throw new Error("clipboard unavailable");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  globalThis.document.body.append(textarea);
  try {
    textarea.select();
    if (globalThis.document.execCommand("copy") !== true) throw new Error("copy rejected");
  } finally {
    textarea.value = "";
    textarea.remove();
  }
}

async function submitPrivateGatewayRevoke(deploymentId, reasonCode, consoleShellState) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  const operation = privateGatewayPageState.beginRevoke(owner, deploymentId);
  if (!operation) return null;
  refreshSequence += 1;
  renderCurrentPrivateGatewayPanel(consoleShellState);
  try {
    const result = await revokeEdgeDeployment({
      controlApiUrl: workspaceControlApiUrl,
      deploymentId: operation.deploymentId,
      revision: operation.revision,
      reasonCode,
      idempotencyKey: createEdgeIdempotencyKey("revoke"),
      signal: operation.controller.signal,
    });
    privateGatewayPageState.acceptRevoke(
      operation, result, currentPrivateGatewayOwner(consoleShellState),
    );
  } catch (error) {
    if (!isPrivateGatewayAbortError(error)) {
      privateGatewayPageState.rejectRevoke(
        operation, error, currentPrivateGatewayOwner(consoleShellState),
      );
    }
  } finally {
    privateGatewayPageState.finishRevoke(operation);
  }
  renderCurrentPrivateGatewayPanel(consoleShellState);
  return privateGatewayPageState.snapshot();
}

/** Build PAGE-119 owner only from the exact live RBAC/navigation snapshot and current hash. */
function currentBatchReadOwner(consoleShellState) {
  const owner = batchJobPageOwner(authSession.context, consoleShellState);
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "batch-jobs")) return null;
  return owner;
}

/** Creation requires batch.write + file.read in addition to the read/navigation owner. */
/** 从当前导航和认证快照取得批任务页面所有者；失败时返回空值并清除旧状态。 */
function currentBatchCreateOwner(consoleShellState) {
  const owner = batchJobPageOwner(authSession.context, consoleShellState, { mode: "create" });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "batch-jobs")) return null;
  return owner;
}

/** Cancellation requires batch.write, while file.read is deliberately not part of this owner. */
/** 从当前导航和认证快照取得批任务页面所有者；失败时返回空值并清除旧状态。 */
function currentBatchWriteOwner(consoleShellState) {
  const owner = batchJobPageOwner(authSession.context, consoleShellState, { mode: "write" });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "batch-jobs")) return null;
  return owner;
}

/** Result reads require file.read and never reuse the create or cancel permission projection. */
/** 从当前导航和认证快照取得批任务页面所有者；失败时返回空值并清除旧状态。 */
function currentBatchResultOwner(consoleShellState) {
  const owner = batchJobPageOwner(authSession.context, consoleShellState, { mode: "result" });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "batch-jobs")) return null;
  return owner;
}

/** Bind PAGE-104 commands; every callback transitions private state before starting a new GET/POST. */
/** 绑定批任务页面交互，并在每次命令前重新核对当前权限与页面所有者。 */
function bindBatchJobsPageInteractions(consoleShellState) {
  const owner = currentBatchReadOwner(consoleShellState);
  if (!owner || !batchJobPageState.enter(owner)) return;
  bindBatchJobInteractions({
    async onFilter(status) {
      if (batchJobPageState.setStatus(status)) await refresh();
    },
    async onRefresh() {
      batchJobPageState.invalidateResult();
      await refresh();
    },
    async onPreviousPage() {
      if (batchJobPageState.previousPage()) await refresh();
    },
    async onNextPage() {
      if (batchJobPageState.nextPage()) await refresh();
    },
    async onSelect(batchJobId) {
      if (batchJobPageState.selectJob(batchJobId)) await refresh();
    },
    async onTaskPreviousPage() {
      if (batchJobPageState.previousTaskPage()) await refresh();
    },
    async onTaskNextPage() {
      if (batchJobPageState.nextTaskPage()) await refresh();
    },
    async onResult(batchTaskId) {
      return loadBatchTaskResult(consoleShellState, batchTaskId);
    },
    async onCloseResult() {
      batchJobPageState.invalidateResult();
      return true;
    },
    async onOpenCreate() {
      const opened = batchJobPageState.openCreate(currentBatchCreateOwner(consoleShellState));
      // Opening a form invalidates any older full-shell refresh before the user can enter draft text.
      if (opened) refreshSequence += 1;
      return opened;
    },
    async onCloseCreate() {
      return batchJobPageState.closeCreate(currentBatchCreateOwner(consoleShellState));
    },
    async onCreate(request, idempotencyKey) {
      return submitBatchJob(request, idempotencyKey, consoleShellState);
    },
    async onCancel(batchJobId) {
      return submitBatchCancellation(batchJobId, consoleShellState);
    },
  });
}

function currentMigrationOperationOwner(consoleShellState) {
  const owner = migrationOperationPageOwner(authSession.context, consoleShellState);
  return owner && readActiveNavigationHash() === "migration-operations" ? owner : null;
}

async function loadMigrationOperationsState(consoleShellState, refreshId) {
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !migrationOperationPageState.enter(owner)) {
    return null;
  }
  return migrationOperationPageState.snapshot();
}

function renderCurrentMigrationOperationPanel(consoleShellState) {
  const panel = globalThis.document?.querySelector?.(".migration-operations-panel");
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!panel || !owner || !migrationOperationPageState.enter(owner)) return false;
  const html = renderMigrationOperations(
    migrationOperationPageState.snapshot(), authSession.context, consoleShellState,
  );
  if (!html) {
    migrationOperationPageState.clear();
    return false;
  }
  panel.innerHTML = html;
  bindMigrationOperationPageInteractions(consoleShellState);
  return true;
}

function bindMigrationOperationPageInteractions(consoleShellState) {
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!owner || !migrationOperationPageState.enter(owner)) return false;
  return bindMigrationOperationInteractions({
    onAssessment: (input) => submitMigrationAssessment(input, consoleShellState),
    onRun: (input) => submitMigrationRun(input, consoleShellState),
    onRefreshDifferences: () => refreshMigrationDifferences(consoleShellState),
    onOpenTransition(action) {
      if (migrationOperationPageState.openTransition(
        currentMigrationOperationOwner(consoleShellState), action,
      )) renderCurrentMigrationOperationPanel(consoleShellState);
    },
    onCloseTransition() {
      if (migrationOperationPageState.closeTransition(
        currentMigrationOperationOwner(consoleShellState),
      )) renderCurrentMigrationOperationPanel(consoleShellState);
    },
    onTransition: (input) => submitMigrationTransition(input, consoleShellState),
  });
}

async function submitMigrationAssessment(input, consoleShellState) {
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!owner?.canManage || !migrationOperationPageState.enter(owner)) return null;
  const commandIntent = {
    sourceKind: String(input?.sourceKind ?? ""),
    manifestVersion: String(input?.manifestVersion ?? ""),
    sourceManifestSha256: String(input?.sourceManifestSha256 ?? ""),
    domains: Array.isArray(input?.domains) ? [...input.domains].map(String).sort() : [],
    reasonCode: String(input?.reasonCode ?? ""),
    confirmed: input?.confirmed === true,
  };
  const idempotencyKey = migrationOperationPageState.idempotencyKey(
    owner, "assessment", commandIntent, () => createMigrationIdempotencyKey("assessment"),
  );
  if (!idempotencyKey) return null;
  const operation = migrationOperationPageState.beginAssessment(owner);
  if (!operation) return null;
  renderCurrentMigrationOperationPanel(consoleShellState);
  let accepted = false;
  try {
    const result = await createMigrationAssessment({
      controlApiUrl: workspaceControlApiUrl,
      ...commandIntent,
      idempotencyKey,
      signal: operation.controller.signal,
    });
    accepted = migrationOperationPageState.acceptAssessment(
      operation, result, currentMigrationOperationOwner(consoleShellState),
    );
    if (accepted) migrationOperationPageState.completeIntent(
      currentMigrationOperationOwner(consoleShellState), "assessment",
    );
  } catch (error) {
    if (!isMigrationOperationAbortError(error)) {
      migrationOperationPageState.reject(
        operation, error, currentMigrationOperationOwner(consoleShellState),
      );
    }
  } finally {
    migrationOperationPageState.finish(operation);
    renderCurrentMigrationOperationPanel(consoleShellState);
  }
  if (accepted) await refreshMigrationDifferences(consoleShellState);
  return migrationOperationPageState.snapshot();
}

async function refreshMigrationDifferences(consoleShellState) {
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!owner || !migrationOperationPageState.enter(owner)) return null;
  const snapshot = migrationOperationPageState.snapshot();
  const migrationId = snapshot.run?.run_id ?? snapshot.assessment?.assessment_id;
  const operation = migrationOperationPageState.beginDifferences(owner, migrationId);
  if (!operation) return snapshot;
  renderCurrentMigrationOperationPanel(consoleShellState);
  try {
    const result = await getMigrationDifferences({
      controlApiUrl: workspaceControlApiUrl,
      migrationId: operation.migrationId,
      signal: operation.controller.signal,
    });
    migrationOperationPageState.acceptDifferences(
      operation, result, currentMigrationOperationOwner(consoleShellState),
    );
  } catch (error) {
    if (!isMigrationOperationAbortError(error)) {
      migrationOperationPageState.reject(
        operation, error, currentMigrationOperationOwner(consoleShellState),
      );
    }
  } finally {
    migrationOperationPageState.finish(operation);
    renderCurrentMigrationOperationPanel(consoleShellState);
  }
  return migrationOperationPageState.snapshot();
}

async function submitMigrationRun(input, consoleShellState) {
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!owner?.canManage || !migrationOperationPageState.enter(owner)) return null;
  const snapshot = migrationOperationPageState.snapshot();
  const commandIntent = {
    assessmentId: String(snapshot.assessment?.assessment_id ?? ""),
    assessmentRevision: Number(snapshot.assessment?.revision ?? 0),
    reasonCode: String(input?.reasonCode ?? ""),
    confirmed: input?.confirmed === true,
  };
  const idempotencyKey = migrationOperationPageState.idempotencyKey(
    owner, "run", commandIntent, () => createMigrationIdempotencyKey("run"),
  );
  if (!idempotencyKey) return null;
  const operation = migrationOperationPageState.beginRun(owner);
  if (!operation) return null;
  renderCurrentMigrationOperationPanel(consoleShellState);
  let accepted = false;
  try {
    const result = await createMigrationRun({
      controlApiUrl: workspaceControlApiUrl,
      assessmentId: operation.assessmentId,
      expectedAssessmentRevision: operation.assessmentRevision,
      reasonCode: commandIntent.reasonCode,
      confirmed: commandIntent.confirmed,
      idempotencyKey,
      signal: operation.controller.signal,
    });
    accepted = migrationOperationPageState.acceptRun(
      operation, result, currentMigrationOperationOwner(consoleShellState),
    );
    if (accepted) migrationOperationPageState.completeIntent(
      currentMigrationOperationOwner(consoleShellState), "run",
    );
  } catch (error) {
    if (!isMigrationOperationAbortError(error)) {
      migrationOperationPageState.reject(
        operation, error, currentMigrationOperationOwner(consoleShellState),
      );
    }
  } finally {
    migrationOperationPageState.finish(operation);
    renderCurrentMigrationOperationPanel(consoleShellState);
  }
  if (accepted) await refreshMigrationDifferences(consoleShellState);
  return migrationOperationPageState.snapshot();
}

async function submitMigrationTransition(input, consoleShellState) {
  const owner = currentMigrationOperationOwner(consoleShellState);
  if (!owner?.canManage || !migrationOperationPageState.enter(owner)) return null;
  const action = String(input?.action ?? "");
  const reasonCode = String(input?.reasonCode ?? "");
  const method = String(input?.method ?? "");
  const confirmed = input?.confirmed === true;
  let verificationCode = String(input?.verificationCode ?? "");
  try { if (input && "verificationCode" in input) input.verificationCode = ""; } catch {
    // A frozen caller object remains confined to this call; the local copy is still wiped below.
  }
  if (!confirmed) {
    verificationCode = "";
    return null;
  }
  const transitionState = migrationOperationPageState.snapshot();
  const commandIntent = {
    action,
    reasonCode,
    runId: String(transitionState.run?.run_id ?? ""),
    revision: Number(transitionState.run?.revision ?? 0),
  };
  let idempotencyKey;
  try {
    idempotencyKey = migrationOperationPageState.idempotencyKey(
      owner,
      "transition",
      commandIntent,
      () => createMigrationIdempotencyKey(action || "transition"),
    );
  } catch {
    verificationCode = "";
    return null;
  }
  const operation = migrationOperationPageState.beginTransition(owner, {
    action,
    reasonCode,
    idempotencyKey,
  });
  idempotencyKey = "";
  if (!operation) {
    verificationCode = "";
    return null;
  }
  renderCurrentMigrationOperationPanel(consoleShellState);
  let webAuthnCredential = null;
  let stepUpGrant = "";
  let accepted = false;
  try {
    let challengeId;
    if (method === "webauthn") {
      const challenge = await beginIdentityWebAuthnStepUp({
        controlApiUrl: workspaceControlApiUrl,
        signal: operation.controller.signal,
      });
      if (!migrationOperationPageState.isCurrent(
        operation, currentMigrationOperationOwner(consoleShellState),
      )) return null;
      challengeId = challenge.challenge_id;
      webAuthnCredential = await getWebAuthnAssertion(challenge.public_key_options);
      if (!migrationOperationPageState.isCurrent(
        operation, currentMigrationOperationOwner(consoleShellState),
      )) return null;
    }
    const stepUp = await createIdentityStepUpGrant({
      controlApiUrl: workspaceControlApiUrl,
      method,
      workspaceId: operation.workspaceId,
      actionCode: `migration.${operation.action}.execute`,
      resourceType: "migration_run",
      resourceId: operation.runId,
      code: verificationCode,
      challengeId,
      credential: webAuthnCredential,
      signal: operation.controller.signal,
    });
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = stepUp.grant;
    if (!migrationOperationPageState.isCurrent(
      operation, currentMigrationOperationOwner(consoleShellState),
    )) return null;
    const transition = operation.action === "cutover" ? cutoverMigrationRun : rollbackMigrationRun;
    const result = await transition({
      controlApiUrl: workspaceControlApiUrl,
      runId: operation.runId,
      expectedRevision: operation.revision,
      reasonCode: operation.reasonCode,
      confirmed: true,
      stepUpGrant,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    stepUpGrant = "";
    accepted = migrationOperationPageState.acceptTransition(
      operation, result, currentMigrationOperationOwner(consoleShellState),
    );
    if (accepted) migrationOperationPageState.completeIntent(
      currentMigrationOperationOwner(consoleShellState), "transition",
    );
  } catch (error) {
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = "";
    if (!isMigrationOperationAbortError(error)) {
      migrationOperationPageState.reject(
        operation, error, currentMigrationOperationOwner(consoleShellState),
      );
    }
  } finally {
    verificationCode = "";
    webAuthnCredential = null;
    stepUpGrant = "";
    migrationOperationPageState.finish(operation);
    renderCurrentMigrationOperationPanel(consoleShellState);
  }
  if (accepted) await refreshMigrationDifferences(consoleShellState);
  return migrationOperationPageState.snapshot();
}

function currentSupportTicketOwner(consoleShellState) {
  const owner = supportTicketPageOwner(authSession.context, consoleShellState);
  return owner && readActiveNavigationHash() === "support-tickets" ? owner : null;
}

async function loadSupportTicketsState(consoleShellState, refreshId = refreshSequence) {
  const owner = currentSupportTicketOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !supportTicketPageState.enter(owner)) return null;
  const operation = supportTicketPageState.beginList(owner);
  if (!operation) return supportTicketPageState.snapshot();
  try {
    const filters = supportTicketPageState.snapshot().filters;
    const result = await listSupportTickets({
      controlApiUrl: workspaceControlApiUrl,
      actorDomain: "workspace",
      status: filters.status,
      severity: filters.severity,
      signal: operation.controller.signal,
    });
    if (refreshId !== refreshSequence) return null;
    supportTicketPageState.acceptList(
      operation, result, currentSupportTicketOwner(consoleShellState),
    );
  } catch (error) {
    if (!isSupportTicketAbortError(error)) {
      supportTicketPageState.reject(
        operation, error, currentSupportTicketOwner(consoleShellState),
      );
    }
  } finally {
    supportTicketPageState.finish(operation);
  }
  return supportTicketPageState.snapshot();
}

function renderCurrentSupportTicketPanel(consoleShellState) {
  const panel = globalThis.document?.querySelector?.(".support-tickets-panel");
  const owner = currentSupportTicketOwner(consoleShellState);
  if (!panel || !owner || !supportTicketPageState.enter(owner)) return false;
  const html = renderSupportTickets(
    supportTicketPageState.snapshot(), authSession.context, consoleShellState,
  );
  if (!html) {
    supportTicketPageState.clear();
    return false;
  }
  clearSupportTicketDom(globalThis.document);
  panel.innerHTML = html;
  bindSupportTicketPageInteractions(consoleShellState);
  return true;
}

function bindSupportTicketPageInteractions(consoleShellState) {
  const owner = currentSupportTicketOwner(consoleShellState);
  if (!owner || !supportTicketPageState.enter(owner)) return false;
  return bindSupportTicketInteractions({
    onFilter: async (filters) => {
      const currentOwner = currentSupportTicketOwner(consoleShellState);
      if (!supportTicketPageState.setFilters(currentOwner, filters)) return false;
      renderCurrentSupportTicketPanel(consoleShellState);
      await loadSupportTicketsState(consoleShellState);
      return renderCurrentSupportTicketPanel(consoleShellState);
    },
    onRefresh: async () => {
      await loadSupportTicketsState(consoleShellState);
      return renderCurrentSupportTicketPanel(consoleShellState);
    },
    onSelect: (ticketId) => {
      const selected = supportTicketPageState.select(
        currentSupportTicketOwner(consoleShellState), ticketId,
      );
      if (selected) renderCurrentSupportTicketPanel(consoleShellState);
      return selected;
    },
    onCreate: (input) => submitSupportTicketCreate(input, consoleShellState),
    onPatch: (input, action) => submitSupportTicketPatch(input, action, consoleShellState),
    onAttachment: (input) => submitSupportTicketAttachment(input, consoleShellState),
    onEscalation: (input) => submitSupportTicketEscalation(input, consoleShellState),
  });
}

async function submitSupportTicketCreate(input, consoleShellState) {
  const owner = currentSupportTicketOwner(consoleShellState);
  if (!owner?.canCreate || !supportTicketPageState.enter(owner)) return null;
  const intent = {
    category: String(input?.category ?? ""), severity: String(input?.severity ?? ""),
    subject: String(input?.subject ?? ""), message: String(input?.message ?? ""),
    relatedKind: String(input?.relatedKind ?? "none"),
    relatedReference: input?.relatedReference === null ? null : String(input?.relatedReference ?? ""),
  };
  const scope = "create";
  const idempotencyKey = supportTicketPageState.idempotencyKey(
    owner, scope, intent, () => createSupportIdempotencyKey(scope),
  );
  const operation = supportTicketPageState.beginMutation(owner, { scope });
  if (!operation || !idempotencyKey) return null;
  renderCurrentSupportTicketPanel(consoleShellState);
  try {
    const result = await createSupportTicket({
      controlApiUrl: workspaceControlApiUrl, ...intent, idempotencyKey,
      signal: operation.controller.signal,
    });
    if (supportTicketPageState.acceptTicket(
      operation, result, currentSupportTicketOwner(consoleShellState), "support.ticket_created",
    )) supportTicketPageState.completeIntent(currentSupportTicketOwner(consoleShellState), scope);
  } catch (error) {
    if (!isSupportTicketAbortError(error)) supportTicketPageState.reject(
      operation, error, currentSupportTicketOwner(consoleShellState),
    );
  } finally {
    supportTicketPageState.finish(operation);
    renderCurrentSupportTicketPanel(consoleShellState);
  }
  return supportTicketPageState.snapshot();
}

async function submitSupportTicketPatch(input, action, consoleShellState) {
  const owner = currentSupportTicketOwner(consoleShellState);
  if ((!owner?.canCreate && !owner?.canManage) || !supportTicketPageState.enter(owner)) return null;
  const intent = {
    ticketId: String(input?.ticketId ?? ""),
    expectedRevision: Number(input?.expectedRevision),
    changes: input?.changes ?? {},
  };
  const scope = `patch-${String(action ?? "update")}-${intent.ticketId}`;
  const idempotencyKey = supportTicketPageState.idempotencyKey(
    owner, scope, intent, () => createSupportIdempotencyKey("patch"),
  );
  const operation = supportTicketPageState.beginMutation(owner, {
    scope, ticketId: intent.ticketId,
  });
  if (!operation || !idempotencyKey) return null;
  renderCurrentSupportTicketPanel(consoleShellState);
  try {
    const result = await patchSupportTicket({
      controlApiUrl: workspaceControlApiUrl,
      actorDomain: "workspace",
      ...intent,
      idempotencyKey,
      signal: operation.controller.signal,
    });
    if (supportTicketPageState.acceptTicket(
      operation, result, currentSupportTicketOwner(consoleShellState), "support.ticket_updated",
    )) supportTicketPageState.completeIntent(currentSupportTicketOwner(consoleShellState), scope);
  } catch (error) {
    if (!isSupportTicketAbortError(error)) supportTicketPageState.reject(
      operation, error, currentSupportTicketOwner(consoleShellState),
    );
  } finally {
    supportTicketPageState.finish(operation);
    renderCurrentSupportTicketPanel(consoleShellState);
  }
  return supportTicketPageState.snapshot();
}

async function submitSupportTicketAttachment(input, consoleShellState) {
  const owner = currentSupportTicketOwner(consoleShellState);
  if (!owner?.canCreate || input?.confirmed !== true || !supportTicketPageState.enter(owner)) return null;
  const intent = {
    ticketId: String(input.ticketId ?? ""), expectedRevision: Number(input.expectedRevision),
    fileId: String(input.fileId ?? ""), intentCode: String(input.intentCode ?? ""),
  };
  const scope = `attachment-${intent.ticketId}`;
  const idempotencyKey = supportTicketPageState.idempotencyKey(
    owner, scope, intent, () => createSupportIdempotencyKey("attachment"),
  );
  const operation = supportTicketPageState.beginMutation(owner, {
    scope, ticketId: intent.ticketId,
  });
  if (!operation || !idempotencyKey) return null;
  renderCurrentSupportTicketPanel(consoleShellState);
  try {
    const result = await attachSupportFile({
      controlApiUrl: workspaceControlApiUrl, ...intent, idempotencyKey,
      signal: operation.controller.signal,
    });
    if (supportTicketPageState.acceptAttachment(
      operation, result, currentSupportTicketOwner(consoleShellState),
    )) supportTicketPageState.completeIntent(currentSupportTicketOwner(consoleShellState), scope);
  } catch (error) {
    if (!isSupportTicketAbortError(error)) supportTicketPageState.reject(
      operation, error, currentSupportTicketOwner(consoleShellState),
    );
  } finally {
    supportTicketPageState.finish(operation);
    renderCurrentSupportTicketPanel(consoleShellState);
  }
  return supportTicketPageState.snapshot();
}

async function submitSupportTicketEscalation(input, consoleShellState) {
  const owner = currentSupportTicketOwner(consoleShellState);
  if ((!owner?.canCreate && !owner?.canManage) || input?.confirmed !== true
    || !supportTicketPageState.enter(owner)) return null;
  const intent = {
    ticketId: String(input.ticketId ?? ""), expectedRevision: Number(input.expectedRevision),
    reasonCode: String(input.reasonCode ?? ""),
  };
  const scope = `escalation-${intent.ticketId}`;
  const idempotencyKey = supportTicketPageState.idempotencyKey(
    owner, scope, intent, () => createSupportIdempotencyKey("escalation"),
  );
  const operation = supportTicketPageState.beginMutation(owner, {
    scope, ticketId: intent.ticketId,
  });
  if (!operation || !idempotencyKey) return null;
  renderCurrentSupportTicketPanel(consoleShellState);
  try {
    const result = await escalateSupportTicket({
      controlApiUrl: workspaceControlApiUrl,
      actorDomain: "workspace",
      ...intent,
      idempotencyKey,
      signal: operation.controller.signal,
    });
    if (supportTicketPageState.acceptEscalation(
      operation, result, currentSupportTicketOwner(consoleShellState),
    )) supportTicketPageState.completeIntent(currentSupportTicketOwner(consoleShellState), scope);
  } catch (error) {
    if (!isSupportTicketAbortError(error)) supportTicketPageState.reject(
      operation, error, currentSupportTicketOwner(consoleShellState),
    );
  } finally {
    supportTicketPageState.finish(operation);
    renderCurrentSupportTicketPanel(consoleShellState);
  }
  return supportTicketPageState.snapshot();
}

function currentTenantDataLifecycleOwner(consoleShellState, mode = "read") {
  const owner = tenantDataLifecycleOwner(authSession.context, consoleShellState, mode);
  const hash = readActiveNavigationHash();
  return owner && (!hash || hash === "tenant-data-lifecycle") ? owner : null;
}

async function loadTenantDataLifecycleState(consoleShellState, refreshId) {
  const owner = currentTenantDataLifecycleOwner(consoleShellState);
  if (!tenantDataLifecyclePageState.enter(owner) || !authSession.token) return null;
  const ticket = tenantDataLifecyclePageState.beginLane("read", owner);
  if (!ticket) return null;
  try {
    const result = await getTenantDataManagement({ controlApiUrl, token: authSession.token, signal: ticket.controller.signal });
    if (refreshId !== refreshSequence || !tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState), "read")) return null;
    tenantDataLifecyclePageState.set(result);
    tenantDataLifecyclePageState.finishLane(ticket);
    return result;
  } catch (error) {
    if (isTenantDataAbortError(error) || !tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState), "read")) return null;
    if (error instanceof AuthApiError && error.status === 401) {
      tenantDataLifecyclePageState.finishLane(ticket);
      tenantDataLifecyclePageState.clear();
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return null;
    }
    tenantDataLifecyclePageState.set({ state: { lifecycle_status: "active", revision: 1, cache_generation: 0 }, exports: [], deletion_request: null, error: { code: error?.code ?? "tenant_data.unavailable", requestId: error?.requestId ?? "-" } });
    tenantDataLifecyclePageState.finishLane(ticket);
    return tenantDataLifecyclePageState.snapshot();
  }
}

function renderCurrentTenantDataLifecyclePanel(consoleShellState) {
  const owner = currentTenantDataLifecycleOwner(consoleShellState);
  if (!tenantDataLifecyclePageState.enter(owner)) return false;
  const panel = globalThis.document?.querySelector?.(".tenant-data-lifecycle-panel");
  if (!panel) return false;
  const html = renderTenantDataLifecycle(tenantDataLifecyclePageState.snapshot(), authSession.context, consoleShellState, currentI18n);
  if (!html) { tenantDataLifecyclePageState.clear(); return false; }
  panel.innerHTML = html;
  bindTenantDataLifecyclePageInteractions(consoleShellState);
  return true;
}

function bindTenantDataLifecyclePageInteractions(consoleShellState) {
  const owner = currentTenantDataLifecycleOwner(consoleShellState);
  if (!tenantDataLifecyclePageState.enter(owner)) return;
  bindTenantDataLifecycleInteractions({
    onRefresh: () => void refresh(),
    onExport: () => void submitTenantDataExport(consoleShellState),
    onDownload: (jobId) => void downloadTenantDataExport(consoleShellState, jobId),
    onDelete: (formData) => formData ? void submitTenantDataDeletion(consoleShellState, String(formData.get("confirmation") ?? "")) : openTenantDataDeletionDialog(),
    onCancelDelete: closeTenantDataDeletionDialog,
  });
}

function openTenantDataDeletionDialog() {
  const dialog = document.querySelector("[data-tenant-data-dialog]");
  if (dialog && !dialog.open) {
    tenantDataDeletionTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.setAttribute("aria-busy", "false");
    dialog.showModal();
  }
  dialog?.querySelector("input[name='confirmation']")?.focus();
}

function closeTenantDataDeletionDialog() {
  const dialog = document.querySelector("[data-tenant-data-dialog]");
  if (dialog?.getAttribute("aria-busy") === "true") return;
  if (dialog?.open) dialog.close();
  const input = dialog?.querySelector("input[name='confirmation']");
  if (input) input.value = "";
  if (tenantDataDeletionTrigger?.isConnected) tenantDataDeletionTrigger.focus({ preventScroll: true });
  tenantDataDeletionTrigger = null;
}

function setTenantDataDeletionBusy(busy) {
  const dialog = document.querySelector("[data-tenant-data-dialog]");
  if (!dialog) return;
  dialog.setAttribute("aria-busy", busy ? "true" : "false");
  dialog.querySelectorAll("button, input").forEach((control) => { control.disabled = Boolean(busy); });
  if (!busy) dialog.querySelector("input[name='confirmation']")?.focus({ preventScroll: true });
}

async function submitTenantDataExport(consoleShellState) {
  const owner = currentTenantDataLifecycleOwner(consoleShellState, "export");
  if (!authSession.token || !owner) { tenantDataLifecyclePageState.clear(); if (!authSession.token) renderLogin(); return; }
  tenantDataLifecyclePageState.enter(owner);
  const ticket = tenantDataLifecyclePageState.beginLane("export", owner);
  if (!ticket) return;
  try {
    const result = await createTenantExport({ controlApiUrl, token: authSession.token, idempotencyKey: createTenantDataIdempotencyKey(), signal: ticket.controller.signal });
    if (!tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState, "export"), "export")) return;
    const current = tenantDataLifecyclePageState.snapshot();
    if (current) tenantDataLifecyclePageState.set({ ...current, exports: [result.export_job, ...(current.exports ?? []).filter((item) => item.tenant_export_job_id !== result.export_job.tenant_export_job_id)] });
    await refresh();
    await pollTenantDataExport(consoleShellState, result.export_job.tenant_export_job_id, ticket);
  } catch (error) {
    tenantDataLifecyclePageState.finishLane(ticket);
    if (error instanceof AuthApiError && error.status === 401) { tenantDataLifecyclePageState.clear(); clearAuthSession(); authSession = { token: null, context: null }; renderLogin(authErrorMessage(error), error.requestId); return; }
    const feedback = document.querySelector("[data-tenant-data-feedback]");
    if (feedback) feedback.textContent = `导出失败 · code: ${error?.code ?? "tenant_data.unavailable"} · request_id: ${error?.requestId ?? "-"}`;
  }
}

async function pollTenantDataExport(consoleShellState, jobId, ticket) {
  const delays = [500, 1000, 2000, 4000, 8000, 12000];
  for (let attempt = 0; attempt < delays.length; attempt += 1) {
    await new Promise((resolve) => globalThis.setTimeout(resolve, delays[attempt]));
    if (!tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState, "export"), "export")) return;
    const result = await getTenantExport({ controlApiUrl, token: authSession.token, jobId, signal: ticket.controller.signal });
    if (!tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState, "export"), "export")) return;
    const current = tenantDataLifecyclePageState.snapshot();
    if (current) tenantDataLifecyclePageState.set({ ...current, exports: [result.export_job, ...(current.exports ?? []).filter((item) => item.tenant_export_job_id !== jobId)] });
    renderCurrentTenantDataLifecyclePanel(consoleShellState);
    if (["completed", "failed", "expired"].includes(result.export_job.status)) break;
  }
  tenantDataLifecyclePageState.finishLane(ticket);
}

async function downloadTenantDataExport(consoleShellState, jobId) {
  const owner = currentTenantDataLifecycleOwner(consoleShellState, "download");
  if (!authSession.token || !owner) return;
  tenantDataLifecyclePageState.enter(owner);
  const ticket = tenantDataLifecyclePageState.beginLane("download", owner);
  if (!ticket) return;
  try {
    const result = await downloadTenantExport({ controlApiUrl, token: authSession.token, jobId, signal: ticket.controller.signal });
    if (!tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState, "download"), "download")) return;
    const url = URL.createObjectURL(result.blob);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = result.filename; anchor.click();
    try { await new Promise((resolve) => globalThis.setTimeout(resolve, 0)); } finally { URL.revokeObjectURL(url); }
    tenantDataLifecyclePageState.finishLane(ticket);
  } catch (error) {
    tenantDataLifecyclePageState.finishLane(ticket);
    const feedback = document.querySelector("[data-tenant-data-feedback]");
    if (feedback) feedback.textContent = `下载失败 · code: ${error?.code ?? "tenant_data.unavailable"} · request_id: ${error?.requestId ?? "-"}`;
  }
}

async function submitTenantDataDeletion(consoleShellState, confirmation) {
  const owner = currentTenantDataLifecycleOwner(consoleShellState, "delete");
  const snapshot = tenantDataLifecyclePageState.snapshot();
  if (!authSession.token || !owner || !snapshot?.state?.revision || !confirmation.trim()) return;
  tenantDataLifecyclePageState.enter(owner);
  const ticket = tenantDataLifecyclePageState.beginLane("delete", owner);
  if (!ticket) return;
  setTenantDataDeletionBusy(true);
  try {
    await requestTenantDeletion({ controlApiUrl, token: authSession.token, idempotencyKey: createTenantDataIdempotencyKey(), confirmation, tenantRevision: Number(snapshot.state.revision), signal: ticket.controller.signal });
    if (!tenantDataLifecyclePageState.isCurrent(ticket, currentTenantDataLifecycleOwner(consoleShellState, "delete"), "delete")) return;
    tenantDataLifecyclePageState.finishLane(ticket);
    tenantDataLifecyclePageState.clear();
    clearAuthSession();
    authSession = { token: null, context: null };
    renderLogin("注销申请已提交，请等待租户生命周期任务完成");
  } catch (error) {
    tenantDataLifecyclePageState.finishLane(ticket);
    if (error instanceof AuthApiError && error.status === 401) { tenantDataLifecyclePageState.clear(); clearAuthSession(); authSession = { token: null, context: null }; renderLogin(authErrorMessage(error), error.requestId); return; }
    setTenantDataDeletionBusy(false);
    const feedback = document.querySelector("[data-tenant-data-feedback]");
    if (feedback) feedback.textContent = `注销申请失败 · code: ${error?.code ?? "tenant_data.unavailable"} · request_id: ${error?.requestId ?? "-"}`;
  }
}

/** Fetch one assistant result and hydrate it only after the exact owner/job/task ticket accepts it. */
/** 按当前所有者加载单项批任务结果；权限撤销或选择漂移时丢弃迟到响应。 */
async function loadBatchTaskResult(consoleShellState, batchTaskId) {
  const token = authSession.token;
  const owner = currentBatchResultOwner(consoleShellState);
  // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
  if (!token || !owner) {
    batchJobPageState.clear();
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (!token) renderLogin();
    return null;
  }
  const operation = batchJobPageState.beginResult(owner, batchTaskId);
  if (!operation) return batchJobPageState.snapshot();
  // A result click takes ownership from an already-running shell refresh before assistant text arrives.
  refreshSequence += 1;
  try {
    const result = await getBatchTaskResult({
      controlApiUrl,
      token,
      batchJobId: operation.batchJobId,
      batchTaskId: operation.batchTaskId,
      signal: operation.controller.signal,
    });
    const currentOwner = currentBatchResultOwner(consoleShellState);
    const assistantText = batchJobPageState.acceptResult(operation, result, currentOwner);
    if (assistantText === null) return null;
    const hydrated = hydrateBatchResultText(globalThis.document, {
      batchJobId: operation.batchJobId,
      batchTaskId: operation.batchTaskId,
      assistantText,
    });
    // A replaced DOM cannot retain assistant text in state; fail closed by invalidating metadata too.
    if (!hydrated) batchJobPageState.invalidateResult();
    return batchJobPageState.snapshot();
  } catch (error) {
    const currentOwner = currentBatchResultOwner(consoleShellState);
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (!batchJobPageState.isResultCurrent(operation, currentOwner) || isBatchAbortError(error)) {
      return null;
    }
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (handleBatchAuthenticationFailure(error)) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (isBatchPermissionRevocation(error)) {
      batchJobPageState.clear();
      await refresh();
      return null;
    }
    batchJobPageState.rejectResult(operation, error, currentOwner);
    return batchJobPageState.snapshot();
  } finally {
    batchJobPageState.finishResult(operation);
  }
}

/** Submit one create request without persisting form values or its idempotency key. */
/** 提交批任务创建命令，幂等键与请求体只驻留当前操作且不写入页面日志。 */
async function submitBatchJob(request, idempotencyKey, consoleShellState) {
  const token = authSession.token;
  const owner = currentBatchCreateOwner(consoleShellState);
  // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
  if (!token || !owner) {
    batchJobPageState.clear();
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (!token) renderLogin();
    return null;
  }
  const operation = batchJobPageState.beginCreate(owner);
  if (!operation) return batchJobPageState.snapshot();
  // The POST and current form DOM now own the page; an older refresh must not replace either.
  refreshSequence += 1;
  let finished = false;
  /** 结束当前批任务页面操作并释放代次状态，防止重复完成回调覆盖新请求。 */
  const finish = () => {
    if (finished) return;
    finished = true;
    batchJobPageState.finishCreate(operation);
  };
  try {
    const result = await createBatchJob({
      controlApiUrl,
      token,
      idempotencyKey,
      request,
      signal: operation.controller.signal,
    });
    const currentOwner = currentBatchCreateOwner(consoleShellState);
    if (!batchJobPageState.acceptCreate(operation, result, currentOwner)) return null;
    finish();
    await refresh();
    return batchJobPageState.snapshot();
  } catch (error) {
    const currentOwner = currentBatchCreateOwner(consoleShellState);
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (!batchJobPageState.isCreateCurrent(operation, currentOwner) || isBatchAbortError(error)) {
      return null;
    }
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (handleBatchAuthenticationFailure(error)) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (isBatchPermissionRevocation(error)) {
      batchJobPageState.clear();
      finish();
      await refresh();
      return null;
    }
    batchJobPageState.rejectCreate(operation, error, currentOwner);
    return batchJobPageState.snapshot();
  } finally {
    finish();
  }
}

/** Cancel only the currently selected active Job under a fresh batch.write owner. */
/** 提交批任务取消命令；后端拒绝、权限撤销和迟到响应均按当前所有者收敛。 */
async function submitBatchCancellation(batchJobId, consoleShellState) {
  const token = authSession.token;
  const owner = currentBatchWriteOwner(consoleShellState);
  // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
  if (!token || !owner) {
    batchJobPageState.clear();
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (!token) renderLogin();
    return null;
  }
  const operation = batchJobPageState.beginCancel(owner, batchJobId);
  if (!operation) return batchJobPageState.snapshot();
  // Keep an older full-shell GET from rebuilding the selected row while cancellation is unresolved.
  refreshSequence += 1;
  let finished = false;
  /** 结束当前批任务页面操作并释放代次状态，防止重复完成回调覆盖新请求。 */
  const finish = () => {
    if (finished) return;
    finished = true;
    batchJobPageState.finishCancel(operation);
  };
  try {
    const result = await cancelBatchJob({
      controlApiUrl,
      token,
      batchJobId: operation.batchJobId,
      signal: operation.controller.signal,
    });
    const currentOwner = currentBatchWriteOwner(consoleShellState);
    if (!batchJobPageState.acceptCancel(operation, result, currentOwner)) return null;
    finish();
    await refresh();
    return batchJobPageState.snapshot();
  } catch (error) {
    const currentOwner = currentBatchWriteOwner(consoleShellState);
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (!batchJobPageState.isCancelCurrent(operation, currentOwner) || isBatchAbortError(error)) {
      return null;
    }
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (handleBatchAuthenticationFailure(error)) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (isBatchPermissionRevocation(error)) {
      batchJobPageState.clear();
      finish();
      await refresh();
      return null;
    }
    batchJobPageState.rejectCancel(operation, error, currentOwner);
    return batchJobPageState.snapshot();
  } finally {
    finish();
  }
}

/** A 401 is the only Batch error that directly clears the authenticated browser session. */
/** 识别后端认证或撤权失败并清空受保护页面状态，前端不自行降级授权。 */
function handleBatchAuthenticationFailure(error) {
  // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
  if (!(error instanceof AuthApiError) || error.status !== 401) return false;
  batchJobPageState.clear();
  clearAuthSession();
  authSession = { token: null, context: null };
  renderLogin(authErrorMessage(error), error.requestId);
  return true;
}

/** Business 403s remain visible; only the canonical permission error triggers authorization reload. */
/** 识别后端认证或撤权失败并清空受保护页面状态，前端不自行降级授权。 */
function isBatchPermissionRevocation(error) {
  // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
  return error instanceof AuthApiError
    && error.status === 403
    && error.code === "auth.permission_denied";
}

/** 构造 PAGE-105 实时 owner；权限 fingerprint 仅用于内存比较，绝不进入 DOM。 */
function currentExperimentOwner(consoleShellState, mode = "read") {
  if (!currentEvaluationPageIdentity("experiments", PERMISSIONS.EXPERIMENT_READ)) return null;
  const owner = experimentPageOwner(authSession.context, consoleShellState, { mode, session: authSession });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "experiments")) return null;
  return owner;
}

/**
 * 决定共享资源目录是否可以读取。
 * PAGE-105 必须拥有完整创建 composite owner 才加载 Project/Environment/Alias/Model 候选；
 * 其他导航继续沿用各目录自身的后端权限校验和既有加载流程。
 */
function shouldLoadExperimentCreateResources(consoleShellState) {
  return consoleShellState?.activeNavigationItemId !== "experiments"
    || Boolean(currentExperimentOwner(consoleShellState, "create"));
}

/** 绑定列表、详情、分页和一个只驻留 Dialog closure 的创建意图。 */
function bindExperimentsPageInteractions(consoleShellState) {
  const owner = currentExperimentOwner(consoleShellState);
  if (!experimentPageState.enter(owner)) return;
  const binding = bindResourceElement(document.querySelector("[data-experiments-root]"),
    [PERMISSIONS.EXPERIMENT_READ], "experiments");
  bindExperimentInteractions({
    isCurrent: (mode = "read") => resourcePageLifecycle.isBindingCurrent(binding)
      && Boolean(currentExperimentOwner(consoleShellState, mode)),
    async onFilter(status) {
      if (experimentPageState.setStatus(status)) await refresh();
    },
    async onRefresh() {
      await refresh();
    },
    async onPreviousPage() {
      if (experimentPageState.previousPage()) await refresh();
    },
    async onNextPage() {
      if (experimentPageState.nextPage()) await refresh();
    },
    async onSelect(experimentId) {
      if (experimentPageState.selectExperiment(experimentId)) {
        await refreshSelectedExperimentPanel(consoleShellState);
      }
    },
    async onOpenCreate() {
      const opened = experimentPageState.openCreate(
        currentExperimentOwner(consoleShellState, "create"),
      );
      if (opened) refreshSequence += 1;
      return opened;
    },
    async onCloseCreate() {
      return experimentPageState.closeCreate(
        currentExperimentOwner(consoleShellState, "create"),
      );
    },
    async onCreate(request, idempotencyKey) {
      return submitExperiment(request, idempotencyKey, consoleShellState);
    },
  });
}

/**
 * 只重建当前 PAGE-105 面板并重新绑定面板内事件；owner 不完整时状态机会先擦除旧租户 DOM。
 * 外层 Console Shell、导航和其他页面数据不参与详情点击，避免无关慢接口阻塞选中反馈。
 */
function renderCurrentExperimentPanel(consoleShellState) {
  const owner = currentExperimentOwner(consoleShellState);
  if (!experimentPageState.enter(owner)) return false;
  const panel = globalThis.document?.querySelector?.(".experiments-panel");
  if (!panel) return false;
  const html = renderExperiments(
    experimentPageState.snapshot(),
    authSession.context,
    consoleShellState,
  );
  if (!html) {
    experimentPageState.clear();
    return false;
  }
  panel.innerHTML = html;
  bindExperimentsPageInteractions(consoleShellState);
  return true;
}

/**
 * 详情点击推进全局刷新代次，再用 detail/results 两条独立 generation lane 局部读取并渲染。
 * 因此旧整页刷新、旧实验响应、离页或 tenant/RBAC owner 漂移都不能覆盖当前选择。
 */
async function refreshSelectedExperimentPanel(consoleShellState) {
  const owner = currentExperimentOwner(consoleShellState);
  if (
    !experimentPageState.enter(owner)
    || !experimentPageState.snapshot().selectedExperimentId
  ) return null;
  const refreshId = ++refreshSequence;
  if (!renderCurrentExperimentPanel(consoleShellState)) return null;
  const state = await loadExperimentDetailState(consoleShellState, refreshId);
  if (!state || refreshId !== refreshSequence) return null;
  return renderCurrentExperimentPanel(consoleShellState) ? state : null;
}

/** 提交不可变实验；UUIDv4 只由绑定闭包传入且不会写入全局页面状态。 */
async function submitExperiment(request, idempotencyKey, consoleShellState) {
  const owner = currentExperimentOwner(consoleShellState, "create");
  // Token 或 composite owner 缺失时先销毁草稿与幂等状态，绝不尝试 POST。
  if (!owner) {
    return null;
  }
  const operation = experimentPageState.beginCreate(owner);
  if (!operation) return experimentPageState.snapshot();
  refreshSequence += 1;
  let finished = false;
  /** 只结束本次 POST ticket；try/catch/finally 的重复调用保持幂等。 */
  const finish = () => {
    if (finished) return;
    finished = true;
    experimentPageState.finishCreate(operation);
  };
  try {
    const response = await createExperiment({
      controlApiUrl,
      authentication: "workspace",
      idempotencyKey,
      request,
      signal: operation.controller.signal,
    });
    const currentOwner = currentExperimentOwner(consoleShellState, "create");
    if (!experimentPageState.acceptCreate(operation, response, currentOwner)) return null;
    finish();
    await refresh();
    return experimentPageState.snapshot();
  } catch (error) {
    const currentOwner = currentExperimentOwner(consoleShellState, "create");
    // owner 漂移或主动 abort 的旧异常不能覆盖新身份页面。
    if (
      !experimentPageState.isCreateCurrent(operation, currentOwner)
      || isExperimentAbortError(error)
    ) return null;
    // 当前 owner 的 401 才能清 Session，其他业务失败继续显示固定错误。
    if (handleExperimentAuthenticationFailure(error)) return null;
    // canonical permission_denied 表示矩阵已撤权，先清状态再刷新后端导航。
    if (isExperimentPermissionRevocation(error)) {
      experimentPageState.clear();
      finish();
      await refresh();
      return null;
    }
    experimentPageState.rejectCreate(operation, error, currentOwner);
    return experimentPageState.snapshot();
  } finally {
    finish();
  }
}

/** 仅在当前实验 owner 收到 401 时清除 Session，旧回调不能注销新身份。 */
function handleExperimentAuthenticationFailure(error) {
  if (!(error instanceof AuthApiError) || error.status !== 401) return false;
  experimentPageState.clear();
  clearAuthSession();
  authSession = { token: null, context: null };
  renderLogin(authErrorMessage(error), error.requestId);
  return true;
}

/** 只有 canonical auth.permission_denied 代表撤权，业务型 403 继续作为固定错误展示。 */
function isExperimentPermissionRevocation(error) {
  return error instanceof AuthApiError
    && error.status === 403
    && error.code === "auth.permission_denied";
}

/** 构造 PAGE-106 实时 owner；完整权限集合和 fingerprint 只用于内存等值比较。 */
function currentResponseCacheOwner(consoleShellState, mode = "read") {
  const owner = responseCachePageOwner(authSession.context, consoleShellState, { mode });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "response-cache")) return null;
  return owner;
}

/** 绑定 Alias、草稿、CAS rebase 和缓存失效动作；每个回调都会重新读取实时 owner。 */
function bindResponseCachePageInteractions(consoleShellState) {
  const owner = currentResponseCacheOwner(consoleShellState);
  if (!responseCachePageState.enter(owner)) return;
  bindResponseCacheInteractions({
    currentEditor: () => responseCachePageState.snapshot().editor,
    async onRefresh() {
      await refreshResponseCachePanel(consoleShellState);
    },
    async onPreviousPage() {
      if (responseCachePageState.previousPage()) await refreshResponseCachePanel(consoleShellState);
    },
    async onNextPage() {
      if (responseCachePageState.nextPage()) await refreshResponseCachePanel(consoleShellState);
    },
    async onSelect(modelAliasId) {
      if (responseCachePageState.selectAlias(modelAliasId)) {
        renderCurrentResponseCachePanel(consoleShellState);
      }
    },
    async onDraft(request) {
      if (responseCachePageState.replaceDraft(request)) {
        renderCurrentResponseCachePanel(consoleShellState);
      }
    },
    async onDiscard() {
      if (responseCachePageState.discardDraft()) {
        renderCurrentResponseCachePanel(consoleShellState);
      }
    },
    async onRebase() {
      await refreshResponseCachePanel(consoleShellState, { rebase: true });
    },
    async onSave(request) {
      await submitResponseCachePolicy(request, consoleShellState);
    },
    async onOpenInvalidation() {
      const opened = responseCachePageState.openInvalidation(
        currentResponseCacheOwner(consoleShellState, "invalidate"),
      );
      if (opened) {
        refreshSequence += 1;
        renderCurrentResponseCachePanel(consoleShellState);
      }
      return opened;
    },
    async onCloseInvalidation() {
      const closed = responseCachePageState.closeInvalidation(
        currentResponseCacheOwner(consoleShellState, "invalidate"),
      );
      if (closed) renderCurrentResponseCachePanel(consoleShellState);
      return closed;
    },
    async onInvalidate(request) {
      await submitResponseCacheInvalidation(request, consoleShellState);
    },
  });
}

/** 局部重建 PAGE-106 面板；无权 owner 会由状态机先擦除旧租户 DOM。 */
function renderCurrentResponseCachePanel(consoleShellState) {
  const owner = currentResponseCacheOwner(consoleShellState);
  if (!responseCachePageState.enter(owner)) return false;
  const panel = globalThis.document?.querySelector?.(".response-cache-panel");
  if (!panel) return false;
  const html = renderResponseCache(
    responseCachePageState.snapshot(),
    authSession.context,
    consoleShellState,
  );
  if (!html) {
    responseCachePageState.clear();
    return false;
  }
  panel.innerHTML = html;
  bindResponseCachePageInteractions(consoleShellState);
  return true;
}

/** 显式局部 GET 推进全局代次，防止旧整页刷新在稍后覆盖当前草稿或 Dialog。 */
async function refreshResponseCachePanel(consoleShellState, { rebase = false } = {}) {
  const owner = currentResponseCacheOwner(consoleShellState);
  if (!responseCachePageState.enter(owner)) return null;
  const refreshId = ++refreshSequence;
  renderCurrentResponseCachePanel(consoleShellState);
  const state = await loadResponseCacheState(consoleShellState, refreshId, { rebase });
  if (!state || refreshId !== refreshSequence) return null;
  return renderCurrentResponseCachePanel(consoleShellState) ? state : null;
}

/** 提交完整 CAS 策略；未知结果的稳定幂等键只由状态机 operation 闭包保存。 */
async function submitResponseCachePolicy(request, consoleShellState) {
  const token = authSession.token;
  const owner = currentResponseCacheOwner(consoleShellState, "write");
  // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
  if (!token || !owner) {
    responseCachePageState.clear();
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (!token) renderLogin();
    return null;
  }
  responseCachePageState.replaceDraft(request);
  const operation = responseCachePageState.beginSave(
    owner,
    request,
    createResponseCacheIdempotencyKey,
  );
  if (!operation) return responseCachePageState.snapshot();
  refreshSequence += 1;
  renderCurrentResponseCachePanel(consoleShellState);
  let finished = false;
  /** 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。 */
  const finish = () => {
    if (finished) return;
    finished = true;
    responseCachePageState.finishSave(operation);
  };
  try {
    const response = await updateResponseCachePolicy({
      controlApiUrl,
      token,
      tenantId: owner.tenantId,
      request: operation.request,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    const currentOwner = currentResponseCacheOwner(consoleShellState, "write");
    if (!responseCachePageState.acceptSave(operation, response, currentOwner)) return null;
    finish();
    renderCurrentResponseCachePanel(consoleShellState);
    return responseCachePageState.snapshot();
  } catch (error) {
    const currentOwner = currentResponseCacheOwner(consoleShellState, "write");
    if (
      !responseCachePageState.isSaveCurrent(operation, currentOwner)
      || isResponseCacheAbortError(error)
    ) return null;
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (handleResponseCacheAuthenticationFailure(error)) return null;
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (isResponseCachePermissionRevocation(error)) {
      responseCachePageState.clear();
      finish();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    responseCachePageState.rejectSave(operation, error, currentOwner);
    renderCurrentResponseCachePanel(consoleShellState);
    return responseCachePageState.snapshot();
  } finally {
    finish();
  }
}

/** 提交全量或 all-match selector 失效；成功后 fresh GET 重建所有 ready count。 */
async function submitResponseCacheInvalidation(request, consoleShellState) {
  const token = authSession.token;
  const owner = currentResponseCacheOwner(consoleShellState, "invalidate");
  // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
  if (!token || !owner) {
    responseCachePageState.clear();
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (!token) renderLogin();
    return null;
  }
  const operation = responseCachePageState.beginInvalidation(
    owner,
    request,
    createResponseCacheIdempotencyKey,
  );
  if (!operation) return responseCachePageState.snapshot();
  refreshSequence += 1;
  renderCurrentResponseCachePanel(consoleShellState);
  let finished = false;
  /** 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。 */
  const finish = () => {
    if (finished) return;
    finished = true;
    responseCachePageState.finishInvalidation(operation);
  };
  try {
    const response = await invalidateResponseCache({
      controlApiUrl,
      token,
      tenantId: owner.tenantId,
      request: operation.request,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    const currentOwner = currentResponseCacheOwner(consoleShellState, "invalidate");
    if (!responseCachePageState.acceptInvalidation(operation, response, currentOwner)) return null;
    finish();
    // 失效计数提交后立即 fresh GET，不能继续展示失效前的 ready_entry_count。
    const refreshed = await refreshResponseCachePanel(consoleShellState);
    return refreshed ?? responseCachePageState.snapshot();
  } catch (error) {
    const currentOwner = currentResponseCacheOwner(consoleShellState, "invalidate");
    if (
      !responseCachePageState.isInvalidationCurrent(operation, currentOwner)
      || isResponseCacheAbortError(error)
    ) return null;
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (handleResponseCacheAuthenticationFailure(error)) return null;
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (isResponseCachePermissionRevocation(error)) {
      responseCachePageState.clear();
      finish();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    responseCachePageState.rejectInvalidation(operation, error, currentOwner);
    renderCurrentResponseCachePanel(consoleShellState);
    return responseCachePageState.snapshot();
  } finally {
    finish();
  }
}

/** 当前 owner 的 401 才能清 Session；旧 generation 已在调用方提前拒绝。 */
function handleResponseCacheAuthenticationFailure(error) {
  if (!(error instanceof AuthApiError) || error.status !== 401) return false;
  responseCachePageState.clear();
  clearAuthSession();
  authSession = { token: null, context: null };
  renderLogin(authErrorMessage(error), error.requestId);
  return true;
}

/** canonical permission_denied 表示后端已撤权，必须重读 auth/navigation 而非仅隐藏按钮。 */
function isResponseCachePermissionRevocation(error) {
  return error instanceof AuthApiError
    && error.status === 403
    && error.code === "auth.permission_denied";
}

/** 构造 PAGE-111 实时 owner；quota.write 始终与 quota.read 独立复核。 */
function currentQuotaManagementOwner(consoleShellState, mode = "read") {
  const owner = quotaPageOwner(authSession.context, consoleShellState, { mode });
  const hashNavigation = readActiveNavigationHash();
  if (!owner || (hashNavigation && hashNavigation !== "quota-buckets")) return null;
  return owner;
}

/** 绑定 Quota 刷新、调整 Dialog 和同一幂等意图恢复。 */
function bindQuotaManagementPageInteractions(consoleShellState) {
  const owner = currentQuotaManagementOwner(consoleShellState);
  if (!quotaManagementPageState.enter(owner)) return;
  bindWorkspaceQuotaInteractions({
    currentBuckets: () => quotaManagementPageState.snapshot().quotaBuckets,
    async onRefresh() {
      await refreshQuotaManagementPanel(consoleShellState);
    },
    async onOpenAdjustment(quotaType) {
      const opened = quotaManagementPageState.openAdjustment(
        currentQuotaManagementOwner(consoleShellState, "write"),
        quotaType,
      );
      if (opened) {
        refreshSequence += 1;
        renderCurrentQuotaManagementPanel(consoleShellState);
      }
      return opened;
    },
    async onCloseAdjustment() {
      const closed = quotaManagementPageState.closeAdjustment(
        currentQuotaManagementOwner(consoleShellState, "write"),
      );
      if (closed) renderCurrentQuotaManagementPanel(consoleShellState);
      return closed;
    },
    async onSubmitAdjustment(request) {
      await submitQuotaAdjustment(request, consoleShellState);
    },
    async onRetryAdjustment() {
      await submitQuotaAdjustment(null, consoleShellState, { retry: true });
    },
  });
}

/** 局部重建 PAGE-111；reason 从不作为 render 参数传出状态闭包。 */
function renderCurrentQuotaManagementPanel(consoleShellState) {
  const owner = currentQuotaManagementOwner(consoleShellState);
  if (!quotaManagementPageState.enter(owner)) return false;
  const panel = globalThis.document?.querySelector?.(".quota-buckets-panel");
  if (!panel) return false;
  const html = renderWorkspaceQuota(
    quotaManagementPageState.snapshot(),
    authSession.context,
    consoleShellState,
    currentI18n,
  );
  if (!html) {
    quotaManagementPageState.clear();
    return false;
  }
  panel.innerHTML = html;
  bindQuotaManagementPageInteractions(consoleShellState);
  return true;
}

/** 显式 Quota GET 推进全局代次，旧整页刷新不能覆盖调整结果。 */
async function refreshQuotaManagementPanel(consoleShellState) {
  const owner = currentQuotaManagementOwner(consoleShellState);
  if (!quotaManagementPageState.enter(owner)) return null;
  const refreshId = ++refreshSequence;
  renderCurrentQuotaManagementPanel(consoleShellState);
  const state = await loadQuotaManagementState(consoleShellState, refreshId);
  if (!state || refreshId !== refreshSequence) return null;
  return renderCurrentQuotaManagementPanel(consoleShellState) ? state : null;
}

/** 提交额度调整；503 重试复用同一闭包 reason/key，其他失败销毁私有意图。 */
async function submitQuotaAdjustment(request, consoleShellState, { retry = false } = {}) {
  const token = authSession.token;
  const owner = currentQuotaManagementOwner(consoleShellState, "write");
  // 额度写入同时要求有效令牌与实时写所有者，任一缺失都销毁私有调整意图。
  if (!token || !owner) {
    quotaManagementPageState.clear();
    if (!token) renderLogin();
    return null;
  }
  const operation = quotaManagementPageState.beginAdjustment(owner, request, { retry });
  if (!operation) return quotaManagementPageState.snapshot();
  refreshSequence += 1;
  renderCurrentQuotaManagementPanel(consoleShellState);
  let finished = false;
  /** 幂等完成当前额度调整通道，避免成功、异常和 finally 重复清理。 */
  const finish = () => {
    if (finished) return;
    finished = true;
    quotaManagementPageState.finishAdjustment(operation);
  };
  try {
    const response = await createQuotaAdjustment({
      controlApiUrl,
      token,
      tenantId: owner.tenantId,
      request: operation.request,
      idempotencyKey: operation.idempotencyKey,
      signal: operation.controller.signal,
    });
    const currentOwner = currentQuotaManagementOwner(consoleShellState, "write");
    if (!quotaManagementPageState.acceptAdjustment(operation, response, currentOwner)) return null;
    finish();
    renderCurrentQuotaManagementPanel(consoleShellState);
    return quotaManagementPageState.snapshot();
  } catch (error) {
    const currentOwner = currentQuotaManagementOwner(consoleShellState, "write");
    if (
      !quotaManagementPageState.isAdjustmentCurrent(operation, currentOwner)
      || isWorkspaceQuotaAbortError(error)
    ) return null;
    // 当前操作的认证失败统一清会话，权限撤销则清页面并重建后端导航。
    if (handleWorkspaceQuotaAuthenticationFailure(error)) return null;
    if (isWorkspaceQuotaPermissionRevocation(error)) {
      quotaManagementPageState.clear();
      finish();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    quotaManagementPageState.rejectAdjustment(operation, error, currentOwner);
    finish();
    // 修订冲突必须重新读取额度事实，不能继续展示或重放陈旧桶版本。
    if (Number(error?.status) === 409) {
      return await refreshQuotaManagementPanel(consoleShellState);
    }
    renderCurrentQuotaManagementPanel(consoleShellState);
    return quotaManagementPageState.snapshot();
  } finally {
    finish();
  }
}

/** 当前 owner 的 401 才能清 Session；旧 generation 已在调用方提前拒绝。 */
function handleWorkspaceQuotaAuthenticationFailure(error) {
  if (!(error instanceof AuthApiError) || error.status !== 401) return false;
  quotaManagementPageState.clear();
  clearAuthSession();
  authSession = { token: null, context: null };
  renderLogin(authErrorMessage(error), error.requestId);
  return true;
}

/** 识别套餐额度权限撤销错误，仅接受固定错误类型、状态和错误码组合。 */
function isWorkspaceQuotaPermissionRevocation(error) {
  // 权限撤销判断必须精确匹配受控认证错误，普通异常不得触发授权刷新。
  return error instanceof AuthApiError
    && error.status === 403
    && error.code === "auth.permission_denied";
}

/** Build the current PAGE-112 owner; hash can only narrow the backend navigation decision. */
function workspaceConsoleExperienceView(navigationId) {
  return ({
    overview: "overview",
    "admin-assistant": "admin-assistant",
    approvals: "approvals",
    "system-status": "system-status",
    "config-summary": "deployment-summary",
  })[navigationId] ?? "";
}

async function loadWorkspaceConsoleExperienceState(consoleShellState, view) {
  workspaceConsoleExperienceShellState = consoleShellState;
  if (!workspaceConsoleExperienceController.enter(consoleShellState, view)) {
    return null;
  }
  if (view !== "admin-assistant") await workspaceConsoleExperienceController.load();
  return workspaceConsoleExperienceController.snapshot();
}

function renderCurrentWorkspaceConsoleExperience() {
  const consoleShellState = workspaceConsoleExperienceShellState;
  const view = workspaceConsoleExperienceView(consoleShellState?.activeNavigationItemId);
  const panel = globalThis.document?.querySelector?.(".workspace-console-experience-panel");
  if (!panel || !view) return false;
  if (!workspaceConsoleExperienceController.owns(consoleShellState, view)) {
    panel.innerHTML = '<p class="console-experience-state is-error" role="alert">页面会话或权限已变化，请刷新后继续。</p>'
      + '<button type="button" data-console-experience-action="refresh">刷新</button>';
    bindWorkspaceConsoleExperience(panel, { onRefresh: () => refresh() });
    return false;
  }
  const state = workspaceConsoleExperienceController.snapshot();
  panel.innerHTML = renderWorkspaceConsoleExperience(state, view);
  hydrateWorkspaceConsoleAssistant(panel, state);
  bindWorkspaceConsoleExperiencePageInteractions(consoleShellState, panel);
  return true;
}

function bindWorkspaceConsoleExperiencePageInteractions(
  consoleShellState,
  root = globalThis.document,
) {
  const view = workspaceConsoleExperienceView(consoleShellState?.activeNavigationItemId);
  if (!view) return false;
  workspaceConsoleExperienceShellState = consoleShellState;
  return bindWorkspaceConsoleExperience(root, {
    onAssistantDraft(value) {
      return workspaceConsoleExperienceController.setAssistantDraft(value);
    },
    onAssistantSubmit() {
      return workspaceConsoleExperienceController.submitAssistant();
    },
    onAssistantClear() {
      return workspaceConsoleExperienceController.clearAssistant();
    },
    onApprovalFilter(filters) {
      return workspaceConsoleExperienceController.configureApprovals(filters);
    },
    onApprovalPrevious() {
      return workspaceConsoleExperienceController.previousApprovals();
    },
    onApprovalNext() {
      return workspaceConsoleExperienceController.nextApprovals();
    },
    onRefresh() {
      return refresh();
    },
  });
}

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
  if (error instanceof IdentityApiError) {
    if (error.status === 401 && error.code.startsWith("workspace_session.")) {
      return "Workspace 会话不可用，请在账号页重新选择空间。";
    }
    if (error.status === 401 && error.code.startsWith("identity.session_")) {
      return "登录状态已失效，请重新登录。";
    }
    return error.message;
  }
  if (error instanceof AuthApiError) {
    // 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return error.code === "auth.token_expired" || error.code === "auth.session_revoked"
      ? "登录状态已失效，请重新登录。"
      : error.message;
  }
  return "无法连接控制面认证接口。";
}

/** Download a signed SDK only while the API docs navigation owner remains current. */
function bindDeveloperContractDownloads(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "api-docs"
    || !hasPermission(authSession.context, PERMISSIONS.DEVELOPER_PORTAL_READ)) {
    clearDeveloperContractDownload();
    developerContractDownloadMetadata = new Map();
    return;
  }
  for (const button of document.querySelectorAll("[data-developer-sdk-download]")) {
    button.addEventListener("click", async () => {
      const token = authSession.token;
      if (!token || button.disabled) return;
      const release = developerContractDownloadMetadata.get(button.dataset.sdkReleaseId);
      if (!release
        || release.language !== button.dataset.language
        || release.sdk_version !== button.dataset.sdkVersion
        || release.artifact_sha256 !== button.dataset.artifactSha256
        || release.artifact_receipt_sha256 !== button.dataset.artifactReceiptSha256) return;
      const owner = Object.freeze({ token, generation: refreshSequence });
      clearDeveloperContractDownload();
      const downloadController = new AbortController();
      developerContractDownloadController = downloadController;
      const feedback = document.querySelector("[data-developer-contract-feedback]");
      const current = () => owner.token === authSession.token
        && owner.generation === refreshSequence
        && developerContractDownloadController === downloadController
        && !downloadController.signal.aborted
        && button.isConnected
        && hasPermission(authSession.context, PERMISSIONS.DEVELOPER_PORTAL_READ);
      button.disabled = true;
      if (feedback) feedback.textContent = "正在验证 SDK 制品...";
      try {
        const result = await downloadDeveloperSdk({
          controlApiUrl,
          token,
          language: button.dataset.language,
          sdkVersion: button.dataset.sdkVersion,
          contractSha256: release.contract_sha256,
          artifactSha256: button.dataset.artifactSha256,
          artifactReceiptSha256: button.dataset.artifactReceiptSha256,
          signatureB64: release.signature_b64,
          signatureSha256: release.signature_sha256,
          publicKeyB64: release.public_key_b64,
          signal: downloadController.signal,
        });
        if (!current()) return;
        const objectUrl = globalThis.URL.createObjectURL(result.blob);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = result.filename;
        link.hidden = true;
        document.body.append(link);
        link.click();
        link.remove();
        globalThis.setTimeout(() => globalThis.URL.revokeObjectURL(objectUrl), 0);
        developerContractDownloadController = null;
        button.disabled = false;
        if (feedback) feedback.textContent = `已验证并下载 ${result.filename}`;
      } catch (error) {
        if (!current()) return;
        developerContractDownloadController = null;
        if (error instanceof AuthApiError && error.status === 401) {
          clearAuthSession();
          authSession = { token: null, context: null };
          renderLogin(authErrorMessage(error), error.requestId);
          return;
        }
        if (error instanceof AuthApiError && error.status === 403) {
          await refresh();
          return;
        }
        button.disabled = false;
        if (feedback) {
          feedback.textContent = `${authErrorMessage(error)} request_id: ${error?.requestId ?? "-"}`;
        }
      }
    });
  }
  for (const button of document.querySelectorAll("[data-developer-security-download]")) {
    button.addEventListener("click", () => {
      const release = developerContractDownloadMetadata.get(button.dataset.sdkReleaseId);
      const evidence = release?.supply_chain;
      if (!release || evidence?.status !== "ready" || !button.isConnected
        || !authSession.token || consoleShellState.activeNavigationItemId !== "api-docs"
        || !hasPermission(authSession.context, PERMISSIONS.DEVELOPER_PORTAL_READ)) return;
      const blob = new Blob([JSON.stringify({
        report: evidence.report,
        provenance: evidence.provenance,
      }, null, 2)], { type: "application/json" });
      const objectUrl = globalThis.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `metahub-${release.language}-${release.sdk_version}-security-report.json`;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      globalThis.setTimeout(() => globalThis.URL.revokeObjectURL(objectUrl), 0);
    });
  }
}

function clearDeveloperContractDownload() {
  developerContractDownloadController?.abort();
  developerContractDownloadController = null;
}

function clearDeveloperContractSecurityLoad() {
  developerContractSecurityController?.abort();
  developerContractSecurityController = null;
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
  if (hasWorkspaceConsoleSession(authSession)) {
    const result = await loadResourceState("sdk-examples", [PERMISSIONS.DEVELOPER_PORTAL_READ],
      async (input, current) => {
        const states = {};
        for (const [language, request] of [["python", getPythonSdkExample], ["javascript", getJavascriptSdkExample]]) {
          if (!current()) throw new DOMException("Cancelled", "AbortError");
          try { states[language] = await request(input); }
          catch (error) {
            if (!current() || isAbortError(error) || [401, 403].includes(error?.status)) throw error;
            states[language] = sdkExampleErrorState(error);
          }
        }
        return states;
      }, (states) => states);
    return result?.state === "error" ? { python: result, javascript: result } : result;
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
async function loadConsoleShellState(isCurrent = () => true) {
  const hashState = readNavigationHashState();
  try {
    const loaded = await workspaceNavigationController.loadNavigation();
    if (!isCurrent()) return null;
    const navigation = loaded.navigation;
    const requestedRegistry = WORKSPACE_ROUTE_REGISTRY[hashState.value];
    let advancedTools = null;
    if (
      navigation.advanced_tools_available
      && requestedRegistry?.placement === "advanced"
    ) {
      advancedTools = await workspaceNavigationController.loadAdvancedTools();
    }
    if (!isCurrent()) return null;
    const resolution = resolveWorkspaceNavigationRoute(navigation, hashState.value, {
      requestedRejected: hashState.rejected,
      advancedTools,
    });
    if (resolution.rejected) {
      replaceNavigationHash("");
    }
    const resolvedNavigation = projectWorkspaceNavigationForConsole(
      navigation,
      resolution.routeCode,
      advancedTools,
    );
    expandedConsoleGroupKey = navigation.preference.expanded_group_code;
    return {
      state: "ok",
      navigation: resolvedNavigation,
      preferences: { preference: navigation.preference },
      workspaceContext: loaded.context,
      workspaceIdentity: loaded.identity,
      workspaceAuthorization: loaded.authorization,
      workspaceOwner: loaded.owner,
      workspaceNavigationContract: true,
      locale: navigation.account_locale,
      activeNavigationItemId: resolvedNavigation.active_navigation_item_id,
      activeRouteCode: resolution.routeCode,
      sidebarExpandedGroupKey: expandedConsoleGroupKey,
      ...(resolution.rejected
        ? { warning: "该入口不可用，已切换至可访问页面。" }
        : {}),
      actionError: consoleShellActionError,
    };
  } catch (error) {
    if (!isCurrent()) return null;
    if (Number(error?.status) === 401) {
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
  const session = authSession;
  if (!hasWorkspaceConsoleSession(session)) {
    renderLogin();
    return;
  }
  if (!sameWorkspaceConsoleSession(session, workspaceConsoleSessionFromShell(consoleShellState))) return;
  const itemId = button.dataset.consoleNav;
  const routeCode = button.dataset.consoleRoute
    || WORKSPACE_SOURCE_ROUTE_REGISTRY[itemId];
  const targetSection = button.dataset.consoleTarget || itemId;
  if (!itemId || !routeCode || WORKSPACE_ROUTE_REGISTRY[routeCode]?.sourceId !== itemId) {
    return;
  }
  clearBasicResourceSessionState({ clearDom: true });
  workspacePageVisitSource = "sidebar";
  workspaceConsoleExperienceShellState = null;
  workspaceConsoleExperienceController.leave();
  workspaceNavigationController.changeRoute();
  closeWorkspaceAdvancedToolsDialog({ restoreFocus: false });
  cancelPendingConsoleNavigationSideEffects();
  // 导航 owner 在任何 await 前冻结；后续点击、全页刷新或换 Session 任一发生都会使它失效。
  // 已验证的内存 Session 只用于本地回调隔离，不进入 DOM、遥测或请求凭据。
  const navigationOwner = Object.freeze({
    generation: ++refreshSequence,
    session,
  });
  const navigationOwnerCurrent = () => isConsoleNavigationOwnerCurrent(
    navigationOwner,
    {
      generation: refreshSequence,
      session: authSession,
    },
  );
  // errorOwner 初始等于导航 owner；调用 refresh 时会同步切换为该 refresh 自己推进后的代次。
  // 这样 catch 既能处理本轮 refresh 的真实失败，也不会让更晚导航的异常写入当前页面。
  let errorOwner = navigationOwner;
  const errorOwnerCurrent = () => isConsoleNavigationOwnerCurrent(
    errorOwner,
    {
      generation: refreshSequence,
      session: authSession,
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
  if (itemId !== "provider-health") {
    // 导航偏好请求发出前立即作废状态 GET 和 visit owner，迟到 200/401/403 均不能影响下一页面。
    providerStatusPageState.clear();
    providerStatusVisitTracker.leave();
    providerStatusEventWarning = null;
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
  if (itemId !== "file-security") {
    // 导航写入前取消 PAGE-103 GET/POST/CSV 并销毁文件名与报告，迟到响应不得跨页面落地。
    fileSecurityPageState.clear();
  }
  if (itemId !== "batch-jobs") {
    // 导航请求前同步擦除结果 DOM，再使 PAGE-104 的 GET/POST ticket 全部失效。
    batchJobPageState.clear();
  }
  if (itemId !== "local-models") {
    // 导航偏好请求前清除 PAGE-116 URL/Token DOM，并淘汰列表、创建和测试 ticket。
    localEndpointPageState.clear();
  }
  if (itemId !== "private-gateways") {
    privateGatewayPageState.clear();
    privateGatewayBrandingShellState = null;
    deploymentBrandingController.clear();
  }
  if (itemId !== "migration-operations") {
    migrationOperationPageState.clear();
  }
  if (itemId !== "support-tickets") {
    supportTicketPageState.clear();
  }
  if (itemId !== "experiments") {
    // 导航请求前销毁 PAGE-105 私有 cursor、Dialog 草稿 owner 与全部 GET/POST ticket。
    experimentPageState.clear();
  }
  if (itemId !== "response-cache") {
    // 导航请求前销毁 PAGE-106 草稿、失效 Dialog、私有 cursor 与全部异步票据。
    responseCachePageState.clear();
  }
  if (itemId !== "quota-buckets") {
    // 导航请求前销毁 PAGE-111 reason、幂等键与额度调整 ticket。
    quotaManagementPageState.clear();
  }
  if (!WORKSPACE_COMMERCIAL_NAVIGATION_IDS.has(itemId)) {
    workspaceCommercialNavigationItems = [];
    workspaceCommercialAccountingController.close();
  }
  if (itemId !== "webhook-dead-letters") {
    // PAGE-083 在偏好写入前就清除读取、写入、幂等键与轮询，旧回调不能越过导航边界。
    webhookDeadLetterPageState.clear();
  }
  consoleShellActionError = null;
  // navigationOwner 创建时已经推进 refreshSequence，使正在运行的旧刷新立即失效；
  // 不能等偏好写入完成后才阻止旧页面回写。
  replaceNavigationHash(routeCode);
  // 先投影目标入口和加载态，避免等待偏好、审计事件及完整数据刷新时用户看到旧页面。
  renderConsoleNavigationTransition(button, consoleShellState, itemId);
  // 当前按钮来自上一轮后端授权导航快照；先投影同一快照中的目标入口安全空态，
  // 让标题和页面骨架在点击帧内更新。真实数据仍只在后续 auth/navigation owner 复核后读取。
  const projectedNavigationItem = consoleShellState?.navigation?.navigation_items?.find(
    (item) => item?.id === itemId && item.visible !== false && item.disabled !== true,
  );
  let authorizedProjectedState = null;
  if (projectedNavigationItem) {
    authorizedProjectedState = {
      ...consoleShellState,
      warning: null,
      actionError: null,
      navigation: {
        ...consoleShellState.navigation,
        active_navigation_item_id: itemId,
        active_route_code: routeCode,
      },
      activeNavigationItemId: itemId,
      activeRouteCode: routeCode,
    };
    renderDeveloperStart(app, {
      gatewayUrl,
      controlApiUrl,
      consoleUrl,
      authContext: authSession.context,
      consoleShellState: authorizedProjectedState,
      i18n: currentI18n,
      branding: publicBrandingDisplay(),
    });
    bindOutputSafetyDetailInteractions(authorizedProjectedState);
    bindAuthenticatedEvents(authorizedProjectedState);
  }
  // 迁移入口的访问事件只能由渲染后 tracker 写入；这里仅标记来源并清理上次 visit 的非阻断告警。
  if (itemId === "migration-guide") {
    migrationGuideEntrySource = "sidebar";
    migrationGuideEventWarning = null;
  }
  // PAGE-088 使用独立 status.viewed；这里只冻结侧栏来源并清除上一次进入的非阻断告警。
  if (itemId === "provider-health") {
    providerStatusEntrySource = "sidebar";
    providerStatusEventWarning = null;
  }
  activeConsoleNavigationCount += 1;
  try {
    // PAGE-088 只有通过双权限和后端导航校验后才能在真实状态 GET 前渲染 loading；503 与空态仍算有效访问。
    if (itemId === "provider-health") {
      const projectedState = renderProviderStatusLoading(app, {
        authContext: authSession.context,
        consoleShellState: authorizedProjectedState,
        i18n: currentI18n,
      });
      if (projectedState) {
        // loading 替换了整个 shell，必须重绑导航、注销和唯一 status.viewed tracker。
        bindAuthenticatedEvents(projectedState);
      }
    }
    if (itemId === "routing-policies") {
      // PAGE-072 的读取可能受慢网络影响；目标页先展示真实 loading，偏好与 page_viewed 延后串行写入。
      // helper 会复核当前角色和后端导航，失败时保持旧 DOM，不能凭 data 属性伪造受限入口。
      const projectedState = renderRoutingPoliciesLoading(app, {
        authContext: authSession.context,
        consoleShellState: authorizedProjectedState,
        i18n: currentI18n,
      });
      if (projectedState) {
        // innerHTML 已替换，必须给新 shell 重新绑定导航和注销；新导航会推进 refreshSequence，
        // 因而当前慢刷新即使迟到也会在最终渲染门禁处被丢弃。
        bindAuthenticatedEvents(projectedState);
      }
    }
    // loading 渲染和事件重绑虽为同步操作，refresh 前仍显式复核 owner，固定“旧导航不发新读取”的边界。
    if (!navigationOwnerCurrent() || !authorizedProjectedState) {
      return;
    }
    // Each page API revalidates Cookie authentication and RBAC independently; a menu click
    // can reuse the already authorized navigation snapshot for route-only hydration.
    await loadAndRenderActiveNavigation({
      consoleShellState: authorizedProjectedState,
      refreshId: navigationOwner.generation,
      isBackgroundRefresh: false,
      refreshOwnerCurrent: navigationOwnerCurrent,
      preserveConsoleSidebar: true,
    });
    if (!navigationOwnerCurrent()) {
      return;
    }
    scheduleConsoleNavigationSideEffects({
      itemId,
      routeCode,
      targetSection,
      navigationOwnerCurrent,
    });
  } catch (error) {
    // 旧 Session 的迟到 401/异常不得清除新 Session、写入当前错误态或触发另一轮 refresh。
    if (!errorOwnerCurrent()) {
      return;
    }
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if (Number(error?.status) === 401) {
      clearAuthSession();
      authSession = { token: null, context: null };
      renderLogin(authErrorMessage(error), error.requestId);
      return;
    }
    consoleShellActionError = error;
    await refresh();
  } finally {
    activeConsoleNavigationCount -= 1;
  }
}

function cancelPendingConsoleNavigationSideEffects() {
  if (consoleNavigationSideEffectTimer !== null) {
    globalThis.clearTimeout(consoleNavigationSideEffectTimer);
    consoleNavigationSideEffectTimer = null;
  }
}

function bindWorkspaceNavigationInteractions(consoleShellState) {
  if (consoleShellState?.workspaceNavigationContract !== true) return;
  for (const button of document.querySelectorAll("[data-workspace-task-route]")) {
    const routeCode = String(button.dataset.workspaceTaskRoute ?? "");
    const route = WORKSPACE_ROUTE_REGISTRY[routeCode];
    if (!route) {
      button.disabled = true;
      continue;
    }
    const navigationButton = [...document.querySelectorAll("[data-console-route]")].find(
      (candidate) => candidate.dataset.consoleRoute === routeCode,
    );
    button.onclick = async () => {
      button.dataset.consoleNav = route.sourceId;
      button.dataset.consoleRoute = routeCode;
      button.dataset.consoleTarget = route.sourceId;
      button.dataset.consoleNavLabel = navigationButton?.dataset.consoleNavLabel ?? "当前任务";
      button.dataset.consoleGroup = navigationButton?.dataset.consoleGroup ?? "任务";
      button.dataset.consoleDescription = navigationButton?.dataset.consoleDescription ?? "";
      await switchConsoleNavigation(button, consoleShellState);
    };
  }
  const advanced = document.querySelector("[data-workspace-advanced-open]");
  if (advanced) {
    advanced.onclick = async () => {
      await openWorkspaceAdvancedToolsDialog(consoleShellState);
    };
  }
}

async function openWorkspaceAdvancedToolsDialog(consoleShellState) {
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  closeWorkspaceAdvancedToolsDialog({ restoreFocus: false });
  workspaceAdvancedToolsDialogTrigger = trigger;
  const controller = new AbortController();
  workspaceAdvancedToolsDialogController = controller;
  workspaceAdvancedToolsDialogState = { status: "loading" };
  renderWorkspaceAdvancedToolsDialogState(consoleShellState);
  try {
    const tools = await workspaceNavigationController.loadAdvancedTools();
    if (controller.signal.aborted
      || workspaceAdvancedToolsDialogController !== controller) return;
    workspaceAdvancedToolsDialogState = tools;
    consoleShellState.navigation = projectWorkspaceNavigationForConsole(
      workspaceNavigationController.snapshot().navigation,
      consoleShellState.activeRouteCode,
      tools,
    );
    renderWorkspaceAdvancedToolsDialogState(consoleShellState);
  } catch (error) {
    if (controller.signal.aborted
      || workspaceAdvancedToolsDialogController !== controller) return;
    workspaceAdvancedToolsDialogState = { status: "error", error };
    renderWorkspaceAdvancedToolsDialogState(consoleShellState);
  }
}

function renderWorkspaceAdvancedToolsDialogState(consoleShellState) {
  document.querySelector("[data-workspace-advanced-dialog]")?.remove();
  const shell = document.querySelector(".console-shell");
  if (!shell || consoleShellState?.workspaceNavigationContract !== true) return;
  shell.insertAdjacentHTML(
    "beforeend",
    renderWorkspaceAdvancedTools(workspaceAdvancedToolsDialogState),
  );
  const dialog = shell.querySelector("[data-workspace-advanced-dialog]");
  if (dialog && typeof dialog.showModal === "function") {
    try {
      dialog.showModal();
    } catch {
      dialog.setAttribute("open", "");
    }
  } else {
    dialog?.setAttribute("open", "");
  }
  const close = () => closeWorkspaceAdvancedToolsDialog();
  dialog?.querySelector("[data-workspace-advanced-close]")?.addEventListener("click", close);
  dialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    close();
  });
  for (const button of dialog?.querySelectorAll("[data-workspace-advanced-tool]") ?? []) {
    button.onclick = async () => {
      const toolCode = String(button.dataset.workspaceAdvancedTool ?? "");
      const routeCode = String(button.dataset.workspaceAdvancedRoute ?? "");
      const route = WORKSPACE_ROUTE_REGISTRY[routeCode];
      if (!route || route.placement !== "advanced") return;
      const row = button.closest(".workspace-advanced-row");
      button.disabled = true;
      try {
        await workspaceNavigationController.openAdvancedTool(toolCode);
        button.dataset.consoleNav = route.sourceId;
        button.dataset.consoleRoute = routeCode;
        button.dataset.consoleTarget = route.sourceId;
        button.dataset.consoleNavLabel = row?.querySelector("h3")?.textContent ?? "高级工具";
        button.dataset.consoleGroup = "高级工具";
        button.dataset.consoleDescription = row?.querySelector("p")?.textContent ?? "";
        await switchConsoleNavigation(button, consoleShellState);
        close();
      } catch (error) {
        if (!button.isConnected) return;
        workspaceAdvancedToolsDialogState = { status: "error", error };
        renderWorkspaceAdvancedToolsDialogState(consoleShellState);
      }
    };
  }
}

function closeWorkspaceAdvancedToolsDialog({ restoreFocus = true } = {}) {
  const trigger = workspaceAdvancedToolsDialogTrigger;
  workspaceAdvancedToolsDialogController?.abort();
  workspaceAdvancedToolsDialogController = null;
  workspaceAdvancedToolsDialogState = null;
  workspaceAdvancedToolsDialogTrigger = null;
  workspaceNavigationController.cancelAdvancedTools();
  document.querySelector("[data-workspace-advanced-dialog]")?.remove();
  if (restoreFocus && trigger?.isConnected && typeof trigger.focus === "function") {
    trigger.focus();
  }
}

/**
 * 页面可用后再防抖、串行写入偏好和低风险访问事件。
 * 两次写入不得与当前页读取并发，也不得为已离开的页面继续补写事件。
 */
function scheduleConsoleNavigationSideEffects({
  itemId,
  routeCode,
  targetSection,
  navigationOwnerCurrent,
}) {
  cancelPendingConsoleNavigationSideEffects();
  consoleNavigationSideEffectTimer = globalThis.setTimeout(async () => {
    consoleNavigationSideEffectTimer = null;
    if (!navigationOwnerCurrent()) return;
    const canonical = WORKSPACE_ROUTE_REGISTRY[routeCode];
    if (canonical) {
      if (new Set(["primary", "task_home"]).has(canonical.placement)) {
        const result = await workspaceNavigationController.savePreference({
          last_primary_route_code: routeCode,
        }).catch((error) => {
          if (navigationOwnerCurrent() && [401, 403, 409].includes(Number(error?.status))) {
            handleConsoleWorkspaceContextInvalidation();
          }
          return null;
        });
        if (!navigationOwnerCurrent()) return;
        if (result?.navigation) {
          expandedConsoleGroupKey = result.navigation.preference.expanded_group_code;
        }
      }
      return;
    }
  }, CONSOLE_NAVIGATION_SIDE_EFFECT_DELAY_MS);
}

/** 分组手风琴只保存当前 Workspace 的服务端 UI 偏好，失败时恢复已确认 revision。 */
async function toggleConsoleSidebarGroup(button, consoleShellState) {
  const session = authSession;
  const refreshId = refreshSequence;
  const groupId = ++sidebarGroupSequence;
  const current = () => hasWorkspaceConsoleSession(session) && session === authSession
    && refreshId === refreshSequence && groupId === sidebarGroupSequence;
  if (!current() || !sameWorkspaceConsoleSession(session, workspaceConsoleSessionFromShell(consoleShellState))) return;
  const key = String(button?.dataset?.sidebarGroupToggle ?? "").trim();
  const group = button?.closest?.("[data-sidebar-group]");
  const items = group?.querySelector?.(".sidebar-group-items");
  if (!key || !group || !items) return;
  const previous = expandedConsoleGroupKey;
  const shouldExpand = button.getAttribute("aria-expanded") !== "true";
  const next = shouldExpand ? key : null;
  applyExpandedConsoleGroup(next);
  expandedConsoleGroupKey = next;
  if (consoleShellState) consoleShellState.sidebarExpandedGroupKey = next;
  if (consoleShellState?.workspaceNavigationContract !== true) return;
  try {
    const result = await workspaceNavigationController.savePreference({
      expanded_group_code: next,
    });
    if (!current()) return;
    expandedConsoleGroupKey = result.navigation.preference.expanded_group_code;
    consoleShellState.sidebarExpandedGroupKey = expandedConsoleGroupKey;
    consoleShellState.navigation = projectWorkspaceNavigationForConsole(
      result.navigation,
      consoleShellState.activeRouteCode,
      workspaceNavigationController.snapshot().advancedTools,
    );
  } catch (error) {
    if (!current()) return;
    if ([401, 403, 409].includes(Number(error?.status))) {
      handleConsoleWorkspaceContextInvalidation();
      return;
    }
    expandedConsoleGroupKey = previous;
    consoleShellState.sidebarExpandedGroupKey = previous;
    applyExpandedConsoleGroup(previous);
    renderConsoleSidebarPreferenceError(error);
  }
}

function applyExpandedConsoleGroup(expandedKey) {
  for (const candidate of document.querySelectorAll("[data-sidebar-group]")) {
    const candidateButton = candidate.querySelector("[data-sidebar-group-toggle]");
    const candidateItems = candidate.querySelector(".sidebar-group-items");
    if (!candidateButton || !candidateItems) continue;
    const expanded = candidate.dataset.sidebarGroup === expandedKey;
    const label = candidate.getAttribute("aria-label") ?? "导航分组";
    candidateButton.setAttribute("aria-expanded", String(expanded));
    candidateButton.setAttribute("aria-label", `${expanded ? "收起" : "展开"}${label}`);
    candidateButton.title = candidateButton.getAttribute("aria-label") ?? "";
    candidateItems.hidden = !expanded;
    candidate.classList.toggle("is-collapsed", !expanded);
  }
}

/** 导航请求期间立即切换标题和加载态；完整页面数据仍由后端权限校验后的 refresh 提供。 */
function renderConsoleNavigationTransition(button, consoleShellState, itemId) {
  const shell = button?.closest?.(".console-shell");
  const main = shell?.querySelector?.(".console-main");
  if (!shell || !main) return;
  const group = String(button.dataset.consoleGroup ?? "");
  const label = String(button.dataset.consoleNavLabel ?? button.getAttribute("aria-label") ?? itemId);
  const description = String(button.dataset.consoleDescription ?? "");
  shell.dataset.activeNavigation = itemId;
  shell.dataset.activeRoute = button.dataset.consoleRoute ?? "";
  for (const candidate of shell.querySelectorAll("[data-console-nav]")) {
    const active = candidate === button;
    candidate.classList.toggle("active", active);
    candidate.setAttribute("aria-current", active ? "page" : "false");
  }
  main.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "console-main-heading";
  const copy = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = group;
  const title = document.createElement("h2");
  title.id = "console-main-title";
  title.textContent = label;
  const detail = document.createElement("p");
  detail.textContent = description;
  copy.append(eyebrow, title, detail);
  const pill = document.createElement("span");
  pill.className = "active-section-pill";
  pill.textContent = "当前页面";
  heading.append(copy, pill);
  const loading = document.createElement("section");
  loading.className = "empty-state console-navigation-loading";
  loading.setAttribute("aria-live", "polite");
  loading.textContent = "正在加载页面数据…";
  main.append(heading, loading);
}

/**
 * 切换侧栏的纯 UI 偏好。布局先即时响应，持久化仍由后端按当前 Session 写入，失败时回滚。
 */
async function toggleConsoleSidebar(button, consoleShellState) {
  const session = authSession;
  const refreshId = refreshSequence;
  const current = () => session === authSession && refreshId === refreshSequence
    && hasWorkspaceConsoleSession(session);
  const shell = button?.closest?.(".console-shell");
  if (!current() || consoleShellState?.workspaceNavigationContract !== true) {
    renderLogin();
    return;
  }
  if (!sameWorkspaceConsoleSession(session, workspaceConsoleSessionFromShell(consoleShellState))) return;
  if (!shell || button.disabled) {
    return;
  }

  const previous = shell.dataset.sidebarCollapsed === "true";
  const next = !previous;
  if (Number(globalThis.window?.innerWidth ?? 0) > 0
    && Number(globalThis.window.innerWidth) <= 820) {
    mobileSidebarCollapsed = next;
    consoleShellState.mobileSidebarCollapsed = next;
    applyConsoleSidebarCollapsedState(shell, next);
    return;
  }
  button.disabled = true;
  applyConsoleSidebarCollapsedState(shell, next);
  consoleSidebarController?.abort();
  const controller = new AbortController();
  consoleSidebarController = controller;
  try {
    const result = await workspaceNavigationController.savePreference({ sidebar_collapsed: next });
    // 保留本轮已确认的偏好，后续导航重绘时不会短暂回到旧状态。
    if (shell.isConnected && current() && consoleSidebarController === controller) {
      consoleShellState.navigation = projectWorkspaceNavigationForConsole(
        result.navigation,
        consoleShellState.activeRouteCode,
        workspaceNavigationController.snapshot().advancedTools,
      );
      consoleShellState.preferences = { preference: result.navigation.preference };
    }
  } catch (error) {
    if (!current() || isAbortError(error) || consoleSidebarController !== controller) return;
    if ([401, 403, 409].includes(Number(error?.status))) {
      handleConsoleWorkspaceContextInvalidation();
      return;
    }
    if (shell.isConnected) {
      applyConsoleSidebarCollapsedState(shell, previous);
      renderConsoleSidebarPreferenceError(error);
    }
  } finally {
    if (consoleSidebarController === controller) consoleSidebarController = null;
    if (button.isConnected) {
      button.disabled = false;
    }
  }
}

function applyConsoleSidebarCollapsedState(shell, collapsed) {
  const normalized = Boolean(collapsed);
  shell.classList.toggle("sidebar-collapsed", normalized);
  shell.dataset.sidebarCollapsed = String(normalized);
  const toggle = shell.querySelector("[data-sidebar-toggle]");
  if (!toggle) return;
  const label = normalized
    ? currentI18n?.t?.("shell.sidebar_expand", {}, "展开导航") ?? "展开导航"
    : currentI18n?.t?.("shell.sidebar_collapse", {}, "收缩导航") ?? "收缩导航";
  toggle.setAttribute("aria-expanded", String(!normalized));
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  const icon = toggle.querySelector(".sidebar-toggle-icon");
  if (icon) icon.textContent = normalized ? "\u203A" : "\u2039";
  const text = toggle.querySelector(".sidebar-toggle-text");
  if (text) text.textContent = label;
}

function renderConsoleSidebarPreferenceError(error) {
  const main = document.querySelector(".console-main");
  if (!main) return;
  let message = main.querySelector(".console-shell-message");
  if (!message) {
    message = document.createElement("div");
    message.className = "empty-state error-state console-shell-message";
    main.prepend(message);
  }
  message.textContent = `${error?.message ?? "导航偏好暂时无法保存"} · request_id: ${error?.requestId ?? "-"}`;
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
  const parsed = parseNavigationHash(globalThis.location?.hash);
  // Retained source links are candidates only; current backend visibility still decides access.
  if (!parsed.rejected && Object.hasOwn(WORKSPACE_SOURCE_ROUTE_REGISTRY, parsed.value)) {
    return { ...parsed, value: WORKSPACE_SOURCE_ROUTE_REGISTRY[parsed.value] };
  }
  return parsed;
}

/**
 * 返回语法合法的导航 ID，供事件来源和页面专属加载器读取。
 * 非法值在 loadConsoleShellState 中清理；其他调用点只把它视为无可用目标。
 */
function readActiveNavigationHash() {
  const routeCode = readNavigationHashState().value;
  return WORKSPACE_ROUTE_REGISTRY[routeCode]?.sourceId;
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

/** PAGE-109 只允许 canonical approval_id 进入 hash；返回中心时恢复无 query 的 approvals 路由。 */
function currentNavigationPath(itemId) {
  return `/#${encodeURIComponent(itemId)}`;
}

/**
 * 执行 控制台主应用 的 `loadTenantState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadTenantState() {
  if (hasWorkspaceConsoleSession(authSession)) {
    const workspaceId = authSession.owner.workspaceId;
    return loadResourceState("tenant", [PERMISSIONS.TENANT_SETTINGS_READ],
      (options) => getWorkspaceProfile({ ...options, workspaceId }),
      (state) => { workspaceProfileState = state; return state; });
  }
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

function bindWorkspaceProfileInteractions() {
  const form = document.querySelector("#workspace-profile-form");
  const binding = bindResourceElement(form, [PERMISSIONS.TENANT_SETTINGS_READ], "tenant");
  const profile = workspaceProfileState?.workspace;
  if (!form || !binding || profile?.status !== "active" || authSession.owner?.role !== "owner") return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operation = resourcePageLifecycle.beginMutation(binding);
    if (!operation) return;
    workspaceProfileMutation = operation;
    const current = () => resourcePageLifecycle.isMutationCurrent(operation);
    const button = form.querySelector('button[type="submit"]');
    const name = form.querySelector('[name="workspaceName"]');
    const feedback = form.querySelector(".tenant-feedback");
    button.disabled = true;
    feedback.textContent = workspaceProfileRecovery ? "正在重新进入组织" : "正在保存";
    try {
      if (!workspaceProfileRecovery) {
        await resourceMutationRequest(operation, updateWorkspaceProfile, {
          controlApiUrl, workspaceId: profile.workspace_id, revision: profile.revision,
          kind: profile.kind, name: name.value,
        });
        if (!current()) return;
        workspaceProfileRecovery = { binding, workspaceId: profile.workspace_id };
        name.disabled = true;
      }
      await resourceMutationRequest(operation, reenterWorkspaceProfile, {
        controlApiUrl, workspaceId: workspaceProfileRecovery.workspaceId,
      });
      if (!current()) return;
      workspaceProfileRecovery = null;
      workspaceProfileMutation = null;
      await refreshResourceMutation(operation);
    } catch (error) {
      if (!current() || isAbortError(error)) return;
      if (workspaceProfileRecovery) {
        feedback.textContent = `资料已保存，暂时无法重新进入组织。request_id: ${error?.requestId ?? "-"}`;
        button.textContent = "重新进入组织";
      } else if (!handleResourceMutationError(operation, error)) {
        feedback.textContent = `${authErrorMessage(error)} request_id: ${error?.requestId ?? "-"}`;
      }
      if (current()) button.disabled = false;
    } finally {
      if (workspaceProfileMutation === operation) workspaceProfileMutation = null;
      resourcePageLifecycle.finishMutation(operation);
    }
  });
}

/** Load admin-only onboarding facts; backend user.manage remains authoritative. */
async function loadTenantOnboardingState() {
  if (!hasPermission(authSession.context, PERMISSIONS.USER_MANAGE)) return null;
  try {
    return await getTenantOnboardingSummary({
      controlApiUrl,
      token: authSession.token,
    });
  } catch (error) {
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
function bindWorkspaceMemberInteractions() {
  for (const button of document.querySelectorAll("[data-workspace-members-offset], [data-workspace-members-refresh]")) {
    const binding = bindResourceElement(button, [PERMISSIONS.USER_MANAGE], "users");
    button.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const offset = button.dataset.workspaceMembersOffset;
      if (offset !== undefined) {
        const value = Number(offset);
        if (!Number.isSafeInteger(value) || value < 0) return;
        workspaceMembersOffset = value;
      }
      resourcePageLifecycle.cancelReads();
      void refresh();
    });
  }
  for (const form of document.querySelectorAll("[data-workspace-member-form]")) {
    const binding = bindResourceElement(form, [PERMISSIONS.USER_MANAGE], "users");
    form.addEventListener("change", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const data = new FormData(form);
      form.querySelector("button[type='submit']").disabled =
        (data.get("role") === form.dataset.membershipRole && data.get("status") === form.dataset.membershipStatus)
        || resourcePageLifecycle.blocksReplacement(false);
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const operation = resourcePageLifecycle.beginMutation(binding);
      if (!operation) return;
      const data = new FormData(form);
      form.querySelector("button[type='submit']").disabled = true;
      if (data.get("role") === form.dataset.membershipRole && data.get("status") === form.dataset.membershipStatus) {
        resourcePageLifecycle.finishMutation(operation);
        return;
      }
      workspaceMembersActionError = null;
      workspaceMembersFeedback = "";
      try {
        await resourceMutationRequest(operation, updateWorkspaceMember, {
          controlApiUrl, membershipId: form.dataset.membershipId,
          revision: Number(form.dataset.membershipRevision),
          role: String(data.get("role") ?? ""), status: String(data.get("status") ?? ""),
        });
        broadcastConsoleWorkspaceContextInvalidation();
        workspaceMembersFeedback = "成员已更新";
        await refreshResourceMutation(operation);
      } catch (error) {
        if (handleResourceMutationError(operation, error)) return;
        workspaceMembersActionError = error;
        await refreshResourceMutation(operation);
      }
    });
  }
}

async function loadUsersState(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "users") return null;
  return loadResourceState("users", [PERMISSIONS.USER_MANAGE],
    (input) => listWorkspaceMembers({ ...input, offset: workspaceMembersOffset, limit: 20 }),
    (result) => ({ ...result, kind: "workspace", state: "ok", role: authSession.owner?.role,
      feedback: workspaceMembersFeedback, actionError: workspaceMembersActionError }),
    { kind: "workspace" });
}

function renderCurrentWorkspaceCommercialAccounting(
  snapshot = workspaceCommercialAccountingController.snapshot(),
) {
  if (!WORKSPACE_COMMERCIAL_NAVIGATION_IDS.has(snapshot.navigation)) return false;
  const root = document.querySelector("[data-workspace-commercial-root]");
  const subscriptionNavigation = ["workspace-subscription", "billing-disputes"]
    .includes(snapshot.navigation);
  const canonicalRoot = subscriptionNavigation ? "workspace-subscription" : "internal-finops";
  if (!root || root.dataset.workspaceCommercialRoot !== canonicalRoot) return false;
  const html = subscriptionNavigation
    ? renderWorkspaceSubscription(snapshot)
    : renderWorkspaceInternalFinOps(snapshot, workspaceCommercialNavigationItems);
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  const next = template.content.firstElementChild;
  if (!next) return false;
  root.replaceWith(next);
  bindWorkspaceCommercialAccountingInteractions();
  return true;
}

function bindWorkspaceCommercialAccountingInteractions() {
  const root = document.querySelector("[data-workspace-commercial-root]");
  if (!root) return;
  for (const button of root.querySelectorAll("[data-workspace-commercial-action]")) {
    button.onclick = () => {
      const action = String(button.dataset.workspaceCommercialAction ?? "");
      if (action === "refresh") void workspaceCommercialAccountingController.refresh();
      if (action === "rate-next") {
        void workspaceCommercialAccountingController.moveRatePage("next");
      }
      if (action === "rate-previous") {
        void workspaceCommercialAccountingController.moveRatePage("previous");
      }
    };
  }
  const filterForm = root.querySelector("[data-workspace-provider-cost-filter-form]");
  if (filterForm) filterForm.onsubmit = (event) => {
    event.preventDefault();
    const data = new FormData(filterForm);
    void workspaceCommercialAccountingController.configureRates({
      provider_id: String(data.get("providerId") ?? "").trim(),
      model_id: String(data.get("modelId") ?? "").trim(),
      currency: String(data.get("currency") ?? ""),
      status: String(data.get("status") ?? ""),
      effective_at: workspaceCommercialTimestamp(data.get("effectiveAt"), true),
    });
  };
  const disputeForm = root.querySelector("[data-workspace-commercial-dispute-form]");
  if (disputeForm) disputeForm.onsubmit = (event) => {
    event.preventDefault();
    const data = new FormData(disputeForm);
    const source = disputeForm.querySelector('select[name="sourceId"]')?.selectedOptions?.[0];
    const stepUp = takeWorkspaceCommercialStepUp(disputeForm, data);
    void workspaceCommercialAccountingController.submitDispute({
      command: {
        source_kind: "invoice",
        source_id: String(data.get("sourceId") ?? ""),
        reason_code: String(data.get("reasonCode") ?? ""),
      },
      expectedSourceRevision: Number(source?.dataset.sourceRevision),
    }, stepUp);
  };
  for (const form of root.querySelectorAll("[data-workspace-provider-cost-command-form]")) {
    form.onsubmit = (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const action = String(data.get("action") ?? "");
      const stepUp = takeWorkspaceCommercialStepUp(form, data);
      let command;
      if (action === "disable") {
        command = {
          action,
          workspace_provider_cost_rate_id: String(data.get("rateId") ?? ""),
        };
      } else if (action === "adopt_reference") {
        command = {
          action,
          provider_id: String(data.get("providerId") ?? ""),
          model_id: String(data.get("modelId") ?? ""),
          currency: String(data.get("currency") ?? ""),
          provider_reference_rate_release_id: String(data.get("referenceRateId") ?? "").trim(),
        };
      } else {
        const effectiveUntil = workspaceCommercialTimestamp(data.get("effectiveUntil"), true);
        command = {
          action: "create_version",
          provider_id: String(data.get("providerId") ?? ""),
          model_id: String(data.get("modelId") ?? ""),
          currency: String(data.get("currency") ?? ""),
          input_rate_micros_per_1k: Number(data.get("inputRate")),
          output_rate_micros_per_1k: Number(data.get("outputRate")),
          effective_from: workspaceCommercialTimestamp(data.get("effectiveFrom")),
          ...(effectiveUntil ? { effective_until: effectiveUntil } : {}),
        };
      }
      void workspaceCommercialAccountingController.applyRate({
        command,
        expectedHeadRevision: Number(data.get("expectedHeadRevision")),
        resourceId: String(data.get("resourceId") ?? "").trim() || undefined,
      }, stepUp);
    };
  }
  for (const select of root.querySelectorAll("[data-workspace-commercial-step-up-method]")) {
    select.onchange = () => {
      const code = select.form?.querySelector("[data-workspace-commercial-step-up-code]");
      const input = code?.querySelector("[data-workspace-commercial-sensitive]");
      const webAuthn = select.value === "webauthn";
      if (input) {
        input.value = "";
        input.disabled = webAuthn;
        input.required = !webAuthn;
      }
      if (code) code.hidden = webAuthn;
    };
  }
}

function takeWorkspaceCommercialStepUp(form, data) {
  let verificationCode = String(data.get("verificationCode") ?? "");
  form.querySelectorAll("[data-workspace-commercial-sensitive]").forEach((node) => {
    node.value = "";
  });
  const result = { method: String(data.get("method") ?? "totp"), verificationCode };
  verificationCode = "";
  return result;
}

function workspaceCommercialTimestamp(value, optional = false) {
  const input = String(value ?? "").trim();
  if (!input && optional) return "";
  const parsed = new Date(input);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : input;
}

function renderCurrentWorkspaceProviderEngineering(snapshot = workspaceProviderEngineeringController.snapshot()) {
  const errorMapsRoot = document.querySelector("[data-workspace-provider-error-maps-root]");
  if (errorMapsRoot) {
    const template = document.createElement("template");
    template.innerHTML = renderWorkspaceProviderErrorMaps(snapshot).trim();
    const next = template.content.firstElementChild;
    if (!next) return false;
    errorMapsRoot.replaceWith(next);
    bindWorkspaceProviderEngineeringDetailInteractions();
    return true;
  }
  const host = document.querySelector("[data-workspace-provider-engineering-host]");
  if (!host) return false;
  const provider = workspaceProviderEngineeringProviders.find(
    (item) => item.provider_id === snapshot.providerId,
  );
  host.innerHTML = provider ? renderWorkspaceProviderEngineering(snapshot, provider) : "";
  for (const button of document.querySelectorAll("[data-workspace-provider-engineering-open]")) {
    button.setAttribute(
      "aria-expanded",
      String(button.dataset.workspaceProviderEngineeringOpen === snapshot.providerId),
    );
  }
  bindWorkspaceProviderEngineeringDetailInteractions();
  return true;
}

function bindWorkspaceProviderEngineeringInteractions(consoleShellState) {
  for (const button of document.querySelectorAll("[data-workspace-provider-engineering-open]")) {
    button.onclick = () => {
      const providerId = String(button.dataset.workspaceProviderEngineeringOpen ?? "");
      if (!WORKSPACE_PROVIDER_ENGINEERING_NAVIGATION_IDS.has(
        consoleShellState?.activeNavigationItemId,
      )
        || !workspaceProviderEngineeringProviders.some(
          (provider) => provider.provider_id === providerId,
        )) return;
      void workspaceProviderEngineeringController.open({
        authContext: authSession.context,
        activeNavigationItemId: consoleShellState.activeNavigationItemId,
        providerId,
      });
    };
  }
  bindWorkspaceProviderEngineeringDetailInteractions();
}

function bindWorkspaceProviderEngineeringDetailInteractions() {
  const root = document.querySelector("[data-workspace-provider-engineering-root]");
  if (!root) return;
  for (const button of root.querySelectorAll("[data-workspace-provider-engineering-action]")) {
    button.onclick = () => {
      const action = button.dataset.workspaceProviderEngineeringAction;
      if (action === "close") workspaceProviderEngineeringController.close();
      if (action === "refresh") void workspaceProviderEngineeringController.refresh();
      if (action === "next") void workspaceProviderEngineeringController.nextPage();
      if (action === "previous") void workspaceProviderEngineeringController.previousPage();
    };
  }
  const filterForm = root.querySelector("[data-workspace-provider-override-filter-form]");
  if (filterForm) filterForm.onsubmit = (event) => {
    event.preventDefault();
    const data = new FormData(filterForm);
    void workspaceProviderEngineeringController.configure({
      provider_id: String(data.get("providerId") ?? "").trim(),
      status: String(data.get("status") ?? ""),
      output_error_code: String(data.get("outputErrorCode") ?? "").trim(),
    });
  };
  const createForm = root.querySelector("[data-workspace-provider-override-create-form]");
  if (createForm) createForm.onsubmit = (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    const secure = takeWorkspaceProviderStepUp(createForm, data);
    void workspaceProviderEngineeringController.apply({
      action: "create",
      provider_id: String(data.get("providerId") ?? ""),
      ...workspaceOverrideFields(data),
    }, secure);
  };
  for (const form of root.querySelectorAll("[data-workspace-provider-override-update-form]")) {
    form.onsubmit = (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const secure = takeWorkspaceProviderStepUp(form, data);
      void workspaceProviderEngineeringController.apply({
        action: "update",
        provider_error_override_id: String(data.get("overrideId") ?? ""),
        expected_version: Number(data.get("expectedVersion")),
        provider_id: String(data.get("providerId") ?? ""),
        ...workspaceOverrideFields(data),
      }, secure);
    };
  }
  for (const form of root.querySelectorAll("[data-workspace-provider-override-transition-form]")) {
    form.onsubmit = (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const secure = takeWorkspaceProviderStepUp(form, data);
      void workspaceProviderEngineeringController.apply({
        action: String(data.get("action") ?? ""),
        provider_error_override_id: String(data.get("overrideId") ?? ""),
        expected_version: Number(data.get("expectedVersion")),
      }, secure);
    };
  }
  for (const select of root.querySelectorAll("[data-workspace-provider-step-up-method]")) {
    select.onchange = () => {
      const code = select.form?.querySelector("[data-workspace-provider-step-up-code]");
      const input = code?.querySelector("[data-workspace-provider-sensitive]");
      const webAuthn = select.value === "webauthn";
      if (input) {
        input.value = "";
        input.disabled = webAuthn;
        input.required = !webAuthn;
      }
      if (code) code.hidden = webAuthn;
    };
  }
}

function workspaceOverrideFields(data) {
  const statuses = String(data.get("httpStatuses") ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
    .map(Number);
  const optional = (name) => {
    const value = String(data.get(name) ?? "").trim();
    return value || null;
  };
  return {
    priority: Number(data.get("priority")),
    http_statuses: [...new Set(statuses)],
    http_status_class: optional("httpStatusClass"),
    error_field: optional("errorField"),
    field_operator: optional("fieldOperator"),
    field_pattern: optional("fieldPattern"),
    output_error_code: String(data.get("outputErrorCode") ?? "").trim(),
  };
}

function takeWorkspaceProviderStepUp(form, data) {
  let verificationCode = String(data.get("verificationCode") ?? "");
  form.querySelectorAll("[data-workspace-provider-sensitive]").forEach((node) => {
    node.value = "";
  });
  const result = { method: String(data.get("method") ?? "totp"), verificationCode };
  verificationCode = "";
  return result;
}

/**
 * 执行 控制台主应用 的 `loadProvidersState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadProvidersState({ includeCatalog = false } = {}) {
  const canReadProviders = hasPermission(authSession.context, PERMISSIONS.PROVIDER_READ);
  const canReadCatalog = includeCatalog && hasPermission(authSession.context, PERMISSIONS.PROVIDER_CATALOG_READ);
  if (!canReadProviders && !canReadCatalog) return null;
  const permissions = [
    ...(canReadProviders ? [PERMISSIONS.PROVIDER_READ] : []),
    ...(canReadCatalog ? [PERMISSIONS.PROVIDER_CATALOG_READ] : []),
  ];
  return loadResourceState("providers", permissions, async (options, current) => {
    const [state, catalog] = await Promise.all([
      canReadProviders ? listProviders(options) : Promise.resolve({ state: "ok", providers: [] }),
      canReadCatalog ? listProviderCatalog({ ...options, filters: providerCatalogFilters }).catch((error) => {
        if (isAbortError(error) || (error instanceof AuthApiError && [401, 403].includes(error.status))) throw error;
        return { state: "error", message: authErrorMessage(error), requestId: error?.requestId ?? "-" };
      }) : Promise.resolve(null),
    ]);
    if (!current()) throw new DOMException("Cancelled", "AbortError");
    const entries = await Promise.all((state.providers ?? []).map(async (provider) => {
      try {
        return [provider.provider_id, await listProviderKeys({ ...options, providerId: provider.provider_id })];
      } catch (error) {
        if (isAbortError(error) || (error instanceof AuthApiError && [401, 403].includes(error.status))) throw error;
        return [provider.provider_id, { state: "error", message: authErrorMessage(error), requestId: error?.requestId ?? "-" }];
      }
    }));
    return { state, catalog, entries };
  }, ({ state, catalog, entries }) => {
    if (includeCatalog) {
      workspaceProviderEngineeringProviders = [...(state.providers ?? [])];
      const selected = workspaceProviderEngineeringController.snapshot().providerId;
      if (selected && !workspaceProviderEngineeringProviders.some((provider) => provider.provider_id === selected)) {
        workspaceProviderEngineeringController.close();
      }
    }
    return { ...state, providerKeysByProviderId: Object.fromEntries(entries),
      actionError: providerActionError, feedback: providerFeedback, lastTestResult: providerLastTestResult,
      providerEngineering: includeCatalog ? workspaceProviderEngineeringController.snapshot() : null,
      providerCatalog: catalog ? { ...catalog, activeTab: providerCatalogActiveTab, filters: providerCatalogFilters,
        actionError: providerCatalogActionError, feedback: providerCatalogFeedback,
        compatibilityByEntryCode: providerCatalogCompatibilityByEntryCode,
        lastCreated: providerCatalogLastCreated, lastPreflight: providerCatalogLastPreflight } : null };
  });
}

/**
 * 执行 控制台主应用 的 `loadModelsState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadModelsState() {
  return loadResourceState("models", [PERMISSIONS.MODEL_READ],
    (options) => listModels(options),
    (state) => ({ ...state, actionError: modelActionError, feedback: modelFeedback }));
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
  const identity = currentEvaluationPageIdentity("routing-policies", PERMISSIONS.ROUTING_READ);
  if (
    !hasPermission(authSession.context, PERMISSIONS.ROUTING_WRITE)
    || consoleShellState?.activeNavigationItemId !== "routing-policies"
    || !identity
  ) {
    return;
  }

  for (const form of document.querySelectorAll("[data-routing-policy-form]")) {
    const binding = bindResourceElement(form,
      [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "routing-policies");
    const modelAliasId = String(form.dataset.modelAliasId ?? "");
    for (const radio of form.querySelectorAll("input[name='routingMode']")) {
      radio.addEventListener("change", () => {
        if (!resourcePageLifecycle.isBindingCurrent(binding) || !radio.checked) {
          return;
        }
        routingPolicyPageMemory.selectMode(modelAliasId, radio.value);
        renderRoutingPolicyPanelFromMemory();
      });
    }

    // 百分比输入只在内存中转换为整数 bps；input 事件局部更新合计和保存门禁，避免重建 DOM 打断键盘输入。
    for (const input of form.querySelectorAll("[data-routing-quality-config] input")) {
      input.addEventListener("input", () => {
        if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
        const config = readRoutingQualityCostConfig(form);
        routingPolicyPageMemory.selectQualityCostConfig(modelAliasId, config);
        updateRoutingQualityCostFormState(form, modelAliasId);
      });
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
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
          authentication: "workspace",
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
        if (handleResourceAuthorizationError(error)) return;
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
    const binding = bindResourceElement(button, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "routing-policies");
    button.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const modelAliasId = button.closest("[data-routing-policy-form]")?.dataset.modelAliasId;
      if (modelAliasId && routingPolicyPageMemory.cancel(modelAliasId)) {
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".routing-policy-row-feedback" });
      }
    });
  }

  for (const button of document.querySelectorAll("[data-routing-policy-rebase]")) {
    const binding = bindResourceElement(button, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "routing-policies");
    button.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
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
    const binding = bindResourceElement(form, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE], "routing-policies");
    const modelAliasId = String(form.dataset.modelAliasId ?? "");
    const syncDraft = () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      cacheAffinityPageMemory.selectDraft(modelAliasId, readCacheAffinityDraft(form));
      updateCacheAffinityFormState(form, modelAliasId);
    };
    const dataClassInput = form.elements.cacheAffinityDataClass;
    /** 无参数、无返回值；在 input 阶段统一 DOM 与草稿的 trim 语义，必须早于 submit 原生 pattern 校验。 */
    const syncDataClassDraft = () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
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
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
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
          authentication: "workspace",
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
        if (handleResourceAuthorizationError(error)) return;
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
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      if (cacheAffinityPageMemory.cancel(modelAliasId)) {
        renderRoutingPolicyPanelFromMemory({ focusSelector: ".cache-affinity-feedback" });
      }
    });
    form.querySelector("[data-cache-affinity-rebase]")?.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
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
  if (!panel || !currentEvaluationPageIdentity("routing-policies", PERMISSIONS.ROUTING_READ)) {
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
    identity: currentEvaluationPageIdentity("routing-policies", PERMISSIONS.ROUTING_READ),
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
  const identity = currentEvaluationPageIdentity("model-quality-scores", PERMISSIONS.MODEL_QUALITY_READ);
  if (
    !hasPermission(authSession.context, PERMISSIONS.MODEL_QUALITY_READ)
    || consoleShellState?.activeNavigationItemId !== "model-quality-scores"
    || !identity
  ) {
    return;
  }

  const filterForm = document.querySelector("#model-quality-filter-form");
  const filterBinding = bindResourceElement(filterForm, [PERMISSIONS.MODEL_QUALITY_READ], "model-quality-scores");
  filterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!resourcePageLifecycle.isBindingCurrent(filterBinding)) return;
    modelQualityScoreFilters = {
      ...modelQualityScoreFilters,
      modelId: String(new FormData(filterForm).get("modelId") ?? "").trim(),
      offset: 0,
    };
    // 查询 owner 变化立即 abort 旧 GET/PATCH，不能等完整刷新经过其他接口后才失效。
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });
  const resetButton = document.querySelector("#model-quality-filter-reset");
  const resetBinding = bindResourceElement(resetButton, [PERMISSIONS.MODEL_QUALITY_READ], "model-quality-scores");
  resetButton?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(resetBinding)) return;
    modelQualityScoreFilters = { modelId: "", limit: 100, offset: 0 };
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });
  const previousButton = document.querySelector("#model-quality-prev");
  const previousBinding = bindResourceElement(previousButton, [PERMISSIONS.MODEL_QUALITY_READ], "model-quality-scores");
  previousButton?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(previousBinding)) return;
    modelQualityScoreFilters = {
      ...modelQualityScoreFilters,
      offset: Math.max(0, modelQualityScoreFilters.offset - modelQualityScoreFilters.limit),
    };
    modelQualityScorePageMemory.invalidate();
    await refresh();
  });
  const nextButton = document.querySelector("#model-quality-next");
  const nextBinding = bindResourceElement(nextButton, [PERMISSIONS.MODEL_QUALITY_READ], "model-quality-scores");
  nextButton?.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(nextBinding)) return;
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
    const binding = bindResourceElement(form, [PERMISSIONS.MODEL_QUALITY_READ, PERMISSIONS.MODEL_QUALITY_WRITE], "model-quality-scores");
    const modelId = String(form.dataset.modelId ?? "");
    const input = form.elements.manualQualityPercent;
    input?.addEventListener("input", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const draft = modelQualityDraftFromElement(input);
      input.setCustomValidity(draft.valid ? "" : "请输入 0 到 100 之间、最多两位小数的评分");
      if (draft.valid) {
        modelQualityScorePageMemory.selectManualQuality(modelId, draft.value);
      }
      updateModelQualityFormState(form, modelId, draft.valid);
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const draft = modelQualityDraftFromElement(input);
      if (!draft.valid) {
        input?.reportValidity();
        return;
      }
      modelQualityScorePageMemory.selectManualQuality(modelId, draft.value);
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
          authentication: "workspace",
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
        if (handleResourceAuthorizationError(error)) return;
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
    const binding = bindResourceElement(button, [PERMISSIONS.MODEL_QUALITY_READ, PERMISSIONS.MODEL_QUALITY_WRITE], "model-quality-scores");
    button.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const modelId = button.closest("[data-model-quality-form]")?.dataset.modelId;
      if (modelId && modelQualityScorePageMemory.selectManualQuality(modelId, null)) {
        renderModelQualityScorePanelFromMemory();
      }
    });
  }
  for (const button of document.querySelectorAll("[data-model-quality-cancel]")) {
    const binding = bindResourceElement(button, [PERMISSIONS.MODEL_QUALITY_READ, PERMISSIONS.MODEL_QUALITY_WRITE], "model-quality-scores");
    button.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
      const modelId = button.closest("[data-model-quality-form]")?.dataset.modelId;
      if (modelId && modelQualityScorePageMemory.cancel(modelId)) {
        renderModelQualityScorePanelFromMemory({ focusModelId: modelId });
      }
    });
  }
  for (const button of document.querySelectorAll("[data-model-quality-rebase]")) {
    const binding = bindResourceElement(button, [PERMISSIONS.MODEL_QUALITY_READ, PERMISSIONS.MODEL_QUALITY_WRITE], "model-quality-scores");
    button.addEventListener("click", () => {
      if (!resourcePageLifecycle.isBindingCurrent(binding)) return;
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
  if (!panel || !currentEvaluationPageIdentity("model-quality-scores", PERMISSIONS.MODEL_QUALITY_READ)) {
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
    identity: currentEvaluationPageIdentity("model-quality-scores", PERMISSIONS.MODEL_QUALITY_READ),
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

/** 返回 PAGE-070 POST 的实时 owner；DOM、身份或所选 request_id 任一消失都会让迟到回调失败关闭。 */
function currentRequestFeedbackOwner() {
  const snapshot = requestFeedbackMutationMemory.snapshot();
  const panelActive = Boolean(document.querySelector(".request-logs-panel .request-feedback-section"));
  return {
    identity: hasPermission(authSession.context, PERMISSIONS.FEEDBACK_USE)
      ? currentRequestLogOwner().identity : "",
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
    || !currentRequestFeedbackOwner().identity
  ) {
    return;
  }
  const form = document.querySelector("[data-request-feedback-form]");
  if (!form) {
    return;
  }
  const binding = bindResourceElement(document.querySelector(".request-logs-panel"),
    [PERMISSIONS.REQUEST_LOG_READ, PERMISSIONS.FEEDBACK_USE], "request-logs");
  const allowed = () => form.isConnected && resourcePageLifecycle.isBindingCurrent(binding);
  if (!allowed()) return;

  for (const radio of form.querySelectorAll("input[name='rating']")) {
    radio.addEventListener("change", () => {
      if (allowed() && radio.checked && requestFeedbackMutationMemory.selectRating(Number(radio.value))) {
        renderRequestFeedbackPanelFromMemory({ focusSelector: `input[name='rating'][value='${radio.value}']` });
      }
    });
  }
  for (const radio of form.querySelectorAll("input[name='accepted']")) {
    radio.addEventListener("change", () => {
      if (allowed() && radio.checked && requestFeedbackMutationMemory.selectAccepted(radio.value === "true")) {
        renderRequestFeedbackPanelFromMemory({ focusSelector: `input[name='accepted'][value='${radio.value}']` });
      }
    });
  }
  for (const checkbox of form.querySelectorAll("input[name='tags']")) {
    checkbox.addEventListener("change", () => {
      if (!allowed()) return;
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
    if (allowed() && requestFeedbackMutationMemory.cancelDraft()) {
      renderRequestFeedbackPanelFromMemory({ focusSelector: ".request-feedback-live" });
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!allowed() || !form.reportValidity()) {
      return;
    }
    const mutation = resourcePageLifecycle.beginMutation(binding);
    if (!mutation) return;
    const acquired = requestFeedbackMutationMemory.acquireSubmit(currentRequestFeedbackOwner());
    if (acquired.state !== "ok" || acquired.reused) {
      resourcePageLifecycle.finishMutation(mutation);
      return;
    }
    const ticket = acquired.ticket;
    renderRequestFeedbackPanelFromMemory();
    try {
      const result = await createRequestFeedback({
        controlApiUrl,
        authentication: "workspace",
        requestId: ticket.sourceRequestId,
        rating: ticket.rating,
        accepted: ticket.accepted,
        tags: ticket.tags,
        operationRequestId: ticket.operationRequestId,
        signal: ticket.signal,
      });
      const owner = currentRequestFeedbackOwner();
      if (!resourcePageLifecycle.isMutationCurrent(mutation)
        || !requestFeedbackMutationMemory.isCurrent(ticket, owner)) {
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
        resourcePageLifecycle.finishMutation(mutation);
        await refresh();
      }
    } catch (error) {
      if (ticket.signal.aborted) {
        return;
      }
      const owner = currentRequestFeedbackOwner();
      // 迟到 401/409/5xx 先过完整 owner，避免上一租户错误退出新 Session 或覆盖新详情草稿。
      if (!resourcePageLifecycle.isMutationCurrent(mutation)
        || !requestFeedbackMutationMemory.isCurrent(ticket, owner)) {
        return;
      }
      // 只有仍属于当前 mutation owner 的 401 才能结束登录态，迟到响应已在上方失败关闭。
      if (handleResourceAuthorizationError(error)) return;
      const safeError = error instanceof AuthApiError
        ? error
        : {
            code: "feedback.unavailable",
            message: "反馈请求未完成",
            requestId: ticket.operationRequestId,
          };
      requestFeedbackMutationMemory.acceptFailure(ticket, safeError, owner);
      renderRequestFeedbackPanelFromMemory({ focusSelector: ".request-feedback-live" });
    } finally {
      resourcePageLifecycle.finishMutation(mutation);
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
  if (!current || !currentRequestFeedbackOwner().identity) {
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
    || !currentEvaluationPageIdentity("feedback-summary", PERMISSIONS.FEEDBACK_USE)
  ) {
    return;
  }
  const form = document.querySelector("#feedback-summary-filter-form");
  const binding = bindResourceElement(document.querySelector(".feedback-summary-panel"),
    [PERMISSIONS.FEEDBACK_USE], "feedback-summary");
  const allowed = () => resourcePageLifecycle.isBindingCurrent(binding);
  if (!allowed()) return;
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!allowed() || !form.isConnected) return;
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
    if (!allowed()) return;
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-reset")?.addEventListener("click", async () => {
    if (!allowed()) return;
    requestFeedbackSummaryFilters = { modelId: "", fromTime: "", toTime: "", limit: 20, offset: 0 };
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-prev")?.addEventListener("click", async () => {
    if (!allowed()) return;
    requestFeedbackSummaryFilters = {
      ...requestFeedbackSummaryFilters,
      offset: Math.max(0, requestFeedbackSummaryFilters.offset - requestFeedbackSummaryFilters.limit),
    };
    requestFeedbackSummaryMemory.invalidate();
    await refresh();
  });
  document.querySelector("#feedback-summary-next")?.addEventListener("click", async () => {
    if (!allowed()) return;
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
function ownWorkspacePageVisitAfterRender(consoleShellState) {
  workspacePageVisitShellState = consoleShellState;
  return workspacePageVisitTracker.afterRender({ source: workspacePageVisitSource,
    viewportClass: globalThis.window?.innerWidth < 720 ? "mobile" : "desktop" });
}

async function ownMigrationGuideVisitAfterRender(consoleShellState) {
  if (hasWorkspaceConsoleSession(authSession)) {
    migrationGuideVisitTracker.leave();
    return;
  }
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
 * 页面 shell 渲染后推进 PAGE-088 唯一访问状态机。
 * 只有双权限身份与后端 provider-health 导航同时成立才允许发送；状态 GET 成功与否不参与访问语义。
 */
async function ownProviderStatusVisitAfterRender(consoleShellState) {
  if (hasWorkspaceConsoleSession(authSession)) {
    providerStatusVisitTracker.leave();
    return;
  }
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
    if (hasWorkspaceConsoleSession(authSession)) {
      bindWorkspaceSdkDownload(button);
      continue;
    }
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

function bindWorkspaceSdkDownload(button) {
  const binding = bindResourceElement(button, [PERMISSIONS.DEVELOPER_PORTAL_READ], "sdk-examples");
  button.addEventListener("click", async () => {
    const operation = resourcePageLifecycle.beginMutation(binding);
    if (!operation) return;
    const downloader = SDK_EXAMPLE_DOWNLOADERS[button.dataset.downloadSdkLanguage];
    const feedback = button.closest("[data-sdk-language-panel]")?.querySelector("[data-sdk-feedback]");
    button.disabled = true;
    try {
      if (!downloader) return;
      if (feedback) feedback.textContent = "正在准备下载";
      const result = await resourceMutationRequest(operation, downloader, { controlApiUrl });
      triggerSdkArchiveDownload(result);
      if (feedback) feedback.textContent = `已下载 ${result.filename}`;
    } catch (error) {
      if (handleResourceMutationError(operation, error)) return;
      if (feedback) feedback.textContent = `${authErrorMessage(error)} request_id: ${error?.requestId ?? "-"}`;
    } finally {
      if (resourcePageLifecycle.isMutationCurrent(operation)) button.disabled = false;
      resourcePageLifecycle.finishMutation(operation);
    }
  });
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
 * 共用 Workspace owner、刷新代次和取消门禁；筛选快照在成功与错误状态中保持一致。
 */
async function loadProviderStatusState(consoleShellState) {
  if (consoleShellState?.activeNavigationItemId !== "provider-health") return null;
  const filters = Object.freeze({ ...providerStatusFilters, limit: providerStatusFilters.limit ?? 50,
    offset: providerStatusFilters.offset ?? 0 });
  const feedback = { filters, actionError: providerHealthActionError,
    probingProviderId: providerHealthProbingProviderId, eventWarning: providerStatusEventWarning };
  return loadResourceState(
    "provider-status", [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ],
    (options) => listProviderStatuses({ ...options, filters }),
    (state) => ({ ...state, ...feedback }), feedback,
  );
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

/** 从认证快照读取仅用于响应 owner 校验的 tenant_id；它从不作为 API query 或 path 参数。 */
function currentTenantId() {
  return String(authSession.context?.user?.tenant_id ?? authSession.context?.tenant?.tenant_id ?? "");
}

/** 触发单行手工探测；POST 开始前作废在途 GET，完成后统一刷新最新快照。 */
async function runProviderProbe(providerId, operation) {
  if (!providerId || !resourcePageLifecycle.isMutationCurrent(operation)) {
    resourcePageLifecycle.finishMutation(operation);
    return;
  }
  providerHealthProbingProviderId = providerId;
  providerHealthActionError = null;
  // 探测写开始前先淘汰状态 GET，旧指标不能在 POST 之后覆盖新探测结果。
  providerStatusPageState.clear();
  resourcePageLifecycle.cancelReads();
  try {
    await resourceMutationRequest(operation, probeProvider, { controlApiUrl, providerId });
    providerHealthProbingProviderId = null;
    await refreshResourceMutation(operation);
  } catch (error) {
    if (handleResourceMutationError(operation, error)) return;
    providerHealthActionError = { message: authErrorMessage(error), requestId: error?.requestId ?? "-" };
    providerHealthProbingProviderId = null;
    await refreshResourceMutation(operation);
  }
}

/**
 * 读取当前租户模型价格时间线，并保留筛选、反馈和统一错误状态。
 */
async function loadEnvironmentState() {
  return loadResourceState("environments", [PERMISSIONS.ENVIRONMENT_READ],
    (options) => listEnvironments({ ...options, status: "all" }),
    (state) => ({ ...state, actionError: environmentActionError, feedback: environmentFeedback }));
}


/**
 * 读取当前租户模型别名，并保留环境绑定字段供控制台按环境创建和展示。
 */
async function loadModelAliasesState() {
  return loadResourceState("model-aliases", [PERMISSIONS.ROUTING_READ], listModelAliases,
    (state) => ({ ...state, actionError: modelAliasActionError, feedback: modelAliasFeedback }));
}


/**
 * 读取 Project/Application 列表，供归属管理页、Key 绑定和预算 scope 选择复用。
 * 管理员/开发者才会调用控制面 API；viewer 返回 null 只是界面优化，真实权限仍由后端项目接口校验。
 */
async function loadProjectState() {
  return loadResourceState("projects", [PERMISSIONS.PROJECT_READ],
    (options) => Promise.all([
      listProjects({ ...options, status: "all" }), listApplications({ ...options, status: "all" }),
    ]),
    ([projects, applications]) => ({ state: "ok", projects: projects.projects ?? [],
      applications: applications.applications ?? [], actionError: projectActionError, feedback: projectFeedback }));
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
  const session = authSession;
  const current = () => session === authSession && refreshId === refreshSequence
    && filePageState.isListOperationCurrent(operation, currentFileReadOwner(consoleShellState), refreshSequence);
  try {
    const result = await listFiles({
      controlApiUrl,
      authentication: "workspace",
      ...filePageState.query(),
      signal: operation.controller.signal,
    });
    if (!current()) return null;
    const currentOwner = currentFileReadOwner(consoleShellState);
    if (!filePageState.acceptPage(operation, result, currentOwner, refreshId)) return null;
    return filePageState.snapshot();
  } catch (error) {
    const currentOwner = currentFileReadOwner(consoleShellState);
    if (
      !current()
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
      handleResourceAuthorizationError(error);
      return null;
    }
    filePageState.rejectPage(operation, error, currentOwner, refreshId);
    return filePageState.snapshot();
  } finally {
    filePageState.finishListOperation(operation);
  }
}

/**
 * 只在后端授权 file-security 导航与实时 file.read 同时成立时读取 ready 文件及当前报告。
 * 两段 GET 共用一个 owner/generation/Abort ticket，其他页面不会后台预取文件名或安全事实。
 */
async function loadFileSecurityState(consoleShellState, refreshId) {
  const owner = currentFileSecurityReadOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !fileSecurityPageState.enter(owner)) return null;
  const operation = fileSecurityPageState.beginLoadOperation(owner, refreshId);
  if (!operation) return null;
  let filesAccepted = false;
  try {
    const filesResult = await listFileSecurityFiles({
      controlApiUrl,
      token: authSession.token,
      ...fileSecurityPageState.query(),
      signal: operation.controller.signal,
    });
    // 新一轮全页刷新可能尚未执行到 PAGE-103；旧响应必须先核对全局代次，不能短暂覆盖新 owner 的页面事实。
    if (refreshId !== refreshSequence) return null;
    const currentOwner = currentFileSecurityReadOwner(consoleShellState);
    if (!fileSecurityPageState.acceptFiles(operation, filesResult, currentOwner, refreshId)) return null;
    filesAccepted = true;
    const fileId = fileSecurityPageState.selectedFile();
    if (!fileId) return fileSecurityPageState.snapshot();
    try {
      const reportResult = await getFileSecurityReport({
        controlApiUrl,
        token: authSession.token,
        fileId,
        signal: operation.controller.signal,
      });
      // 报告 GET 与列表 GET 分属两个 await，二者都必须独立核对全局刷新代次。
      if (refreshId !== refreshSequence) return null;
      const reportOwner = currentFileSecurityReadOwner(consoleShellState);
      if (!fileSecurityPageState.acceptReport(operation, reportResult, reportOwner, refreshId)) return null;
    } catch (error) {
      const reportOwner = currentFileSecurityReadOwner(consoleShellState);
      if (
        refreshId !== refreshSequence
        || !fileSecurityPageState.isLoadOperationCurrent(operation, reportOwner, refreshId)
        || isFileSecurityAbortError(error)
      ) return null;
      if (
        error instanceof AuthApiError
        && error.status === 404
        && error.code === "file.security_report_not_found"
      ) {
        fileSecurityPageState.acceptReportNotFound(operation, reportOwner, refreshId);
        return fileSecurityPageState.snapshot();
      }
      // 其他报告异常交给外层统一处理 401/403 与固定页面错误。
      throw error;
    }
    return fileSecurityPageState.snapshot();
  } catch (error) {
    const currentOwner = currentFileSecurityReadOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !fileSecurityPageState.isLoadOperationCurrent(operation, currentOwner, refreshId)
      || isFileSecurityAbortError(error)
    ) return null;
    // 顺序 GET 任一阶段确认 Session 失效，都必须清理全部 PAGE-103 owner 事实。
    if (error instanceof AuthApiError && error.status === 401) {
      fileSecurityPageState.clear();
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
      // GET 撤权先销毁文件名与报告，再异步重读授权和导航，避免当前刷新栈递归。
      fileSecurityPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    if (filesAccepted) {
      fileSecurityPageState.rejectReport(operation, error, currentOwner, refreshId);
    } else {
      fileSecurityPageState.rejectLoad(operation, error, currentOwner, refreshId);
    }
    return fileSecurityPageState.snapshot();
  } finally {
    fileSecurityPageState.finishLoadOperation(operation);
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

/** Read only the active PAGE-114 tab; Tool Schema and descriptions remain explicit-detail only. */
async function loadMcpToolsState(consoleShellState, refreshId) {
  const owner = currentMcpToolOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !mcpToolPageState.enter(owner)) return null;
  const tab = mcpToolPageState.snapshot().activeTab;
  const operation = mcpToolPageState.beginList(owner, tab, refreshId);
  if (!operation || !authSession.token) return null;
  try {
    const common = {
      controlApiUrl,
      token: authSession.token,
      ...mcpToolPageState.listQuery(tab),
      signal: operation.controller.signal,
    };
    const result = tab === "servers" || tab === "network"
      ? await listMcpServers(common)
      : tab === "tools"
        ? await listMcpTools(common)
        : await listMcpInvocations(common);
    if (refreshId !== refreshSequence) return null;
    const currentOwner = currentMcpToolOwner(consoleShellState);
    mcpToolPageState.acceptList(operation, result, currentOwner, refreshId);
    return mcpToolPageState.snapshot();
  } catch (error) {
    const currentOwner = currentMcpToolOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !mcpToolPageState.isListCurrent(operation, currentOwner, refreshId)
      || isMcpToolAbortError(error)
    ) return null;
    if (handleMcpAuthenticationFailure(error)) return null;
    mcpToolPageState.rejectList(operation, error, currentOwner, refreshId);
    return mcpToolPageState.snapshot();
  } finally {
    mcpToolPageState.finishList(operation);
  }
}

/** Load only the dedicated PAGE-116 aggregate and reject every obsolete owner before auth handling. */
async function loadLocalEndpointsState(consoleShellState, refreshId) {
  const owner = currentLocalEndpointOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !localEndpointPageState.enter(owner)) return null;
  const operation = localEndpointPageState.beginLoad(owner, refreshId);
  if (!operation || !authSession.token) return null;
  try {
    const result = await listLocalEndpoints({
      controlApiUrl,
      token: authSession.token,
      tenantId: owner.tenantId,
      signal: operation.controller.signal,
    });
    if (refreshId !== refreshSequence) return null;
    const currentOwner = currentLocalEndpointOwner(consoleShellState);
    localEndpointPageState.acceptLoad(operation, result, currentOwner, refreshId);
    return localEndpointPageState.snapshot();
  } catch (error) {
    const currentOwner = currentLocalEndpointOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !localEndpointPageState.isLoadCurrent(operation, currentOwner, refreshId)
      || isLocalEndpointAbortError(error)
    ) return null;
    if (handleLocalEndpointAuthenticationFailure(error)) return null;
    localEndpointPageState.rejectLoad(operation, error, currentOwner, refreshId);
    return localEndpointPageState.snapshot();
  } finally {
    localEndpointPageState.finishLoad(operation);
  }
}

/** Read only the active Workspace's PRD-120 projection with Cookie credentials. */
async function loadPrivateGatewaysState(consoleShellState, refreshId) {
  const owner = currentPrivateGatewayOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !privateGatewayPageState.enter(owner)) return null;
  const operation = privateGatewayPageState.beginLoad(owner, refreshId);
  if (!operation) return null;
  try {
    const [result, installerCatalog] = await Promise.all([
      listEdgeDeployments({
        controlApiUrl: workspaceControlApiUrl,
        signal: operation.controller.signal,
      }),
      owner.canReadInstallers
        ? listEdgeInstallers({
          controlApiUrl: workspaceControlApiUrl,
          signal: operation.controller.signal,
        })
        : Promise.resolve(null),
    ]);
    const [
      desiredStates, telemetryStates, secretReferences, healthStates, upgradeStates,
      licenseStates, backupStates, offlineBundleStates,
    ] = await Promise.all([
      loadPrivateGatewayDesiredStates(result.deployments, operation.controller.signal),
      owner.canReadTelemetry || owner.canReadUsage
        ? loadPrivateGatewayTelemetryStates(result.deployments, operation.controller.signal)
        : Promise.resolve([]),
      owner.canReadSecrets
        ? listEdgeSecretReferences({
          controlApiUrl: workspaceControlApiUrl,
          signal: operation.controller.signal,
        })
        : Promise.resolve(null),
      owner.canReadHealth
        ? loadPrivateGatewayHealthStates(result.deployments, operation.controller.signal)
        : Promise.resolve([]),
      owner.canReadUpgrades
        ? loadPrivateGatewayUpgradeStates(result.deployments, operation.controller.signal)
        : Promise.resolve([]),
      owner.canReadLicenses
        ? loadPrivateGatewayLicenseStates(result.deployments, operation.controller.signal)
        : Promise.resolve([]),
      owner.canReadBackups
        ? loadPrivateGatewayBackupStates(result.deployments, operation.controller.signal)
        : Promise.resolve([]),
      owner.canReadOfflineBundles
        ? loadPrivateGatewayOfflineBundleStates(
          result.deployments, operation.controller.signal,
        )
        : Promise.resolve([]),
    ]);
    if (refreshId !== refreshSequence) return null;
    privateGatewayPageState.acceptLoad(
      operation,
      result,
      currentPrivateGatewayOwner(consoleShellState),
      refreshId,
      desiredStates,
      telemetryStates,
      secretReferences,
      installerCatalog,
      healthStates,
      upgradeStates,
      licenseStates,
      backupStates,
      offlineBundleStates,
    );
    deploymentBrandingController.enter(
      currentPrivateGatewayOwner(consoleShellState),
      result.deployments,
    );
    return privateGatewayPageState.snapshot();
  } catch (error) {
    const currentOwner = currentPrivateGatewayOwner(consoleShellState);
    if (refreshId !== refreshSequence
      || !privateGatewayPageState.isLoadCurrent(operation, currentOwner, refreshId)
      || isPrivateGatewayAbortError(error)) return null;
    privateGatewayPageState.rejectLoad(operation, error, currentOwner, refreshId);
    return privateGatewayPageState.snapshot();
  } finally {
    privateGatewayPageState.finishLoad(operation);
  }
}

async function loadPrivateGatewayBackupStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      const response = await getWorkspaceBackupStatus({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId,
        signal,
      });
      results[index] = Object.freeze({ deploymentId, response });
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(4, deployments.length) },
    () => worker(),
  ));
  return results;
}

async function loadPrivateGatewayOfflineBundleStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      const response = await getWorkspaceOfflineBundleState({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId,
        signal,
      });
      results[index] = { deploymentId, response };
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(4, deployments.length) }, () => worker()),
  );
  return results;
}

async function loadPrivateGatewayLicenseStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      try {
        const response = await getWorkspaceLicense({
          controlApiUrl: workspaceControlApiUrl,
          deploymentId,
          signal,
        });
        results[index] = Object.freeze({ deploymentId, response });
      } catch (error) {
        if (error instanceof AuthApiError
          && error.status === 404 && error.code === "edge_license.not_found") {
          results[index] = Object.freeze({ deploymentId, response: null });
        } else {
          throw error;
        }
      }
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(4, deployments.length) },
    () => worker(),
  ));
  return results;
}

async function loadPrivateGatewayUpgradeStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      const response = await getWorkspaceUpgrade({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId,
        signal,
      });
      results[index] = Object.freeze({ deploymentId, response });
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(4, deployments.length) },
    () => worker(),
  ));
  return results;
}

async function loadPrivateGatewayHealthStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      const response = await getWorkspaceEdgeHealth({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId,
        signal,
      });
      results[index] = Object.freeze({ deploymentId, response });
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(4, deployments.length) },
    () => worker(),
  ));
  return results;
}

async function loadPrivateGatewayTelemetryStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      const response = await getEdgeTelemetryWatermark({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId,
        signal,
      });
      results[index] = Object.freeze({ deploymentId, response });
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(4, deployments.length) },
    () => worker(),
  ));
  return results;
}

async function loadPrivateGatewayDesiredStates(deployments, signal) {
  const results = new Array(deployments.length);
  let cursor = 0;
  async function worker() {
    while (cursor < deployments.length) {
      const index = cursor;
      cursor += 1;
      const deploymentId = deployments[index].deployment_id;
      const response = await getEdgeDesiredState({
        controlApiUrl: workspaceControlApiUrl,
        deploymentId,
        signal,
      });
      results[index] = Object.freeze({ deploymentId, response });
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(4, deployments.length) },
    () => worker(),
  ));
  return results;
}

/** Load PAGE-119 profile first, then its independent project/customer/commission lanes. */
function publicBrandingDisplay() {
  const item = publicBrandingPreview;
  if (!item) return null;
  return Object.freeze({
    product_name: item.product_name,
    primary_color: item.primary_color,
    secondary_color: item.secondary_color,
    logoPresent: Boolean(item.logo_path),
    logoUrl: item.logo_path ?? "",
  });
}

async function loadBatchJobsState(consoleShellState, refreshId, resourceStates) {
  const owner = currentBatchReadOwner(consoleShellState);
  if (!owner || refreshId !== refreshSequence || !batchJobPageState.enter(owner)) return null;
  const operation = batchJobPageState.beginList(owner, refreshId);
  if (!operation) return null;
  let accepted = false;
  try {
    const listResult = await listBatchJobs({
      controlApiUrl,
      token: authSession.token,
      ...batchJobPageState.listQuery(),
      limit: 50,
      signal: operation.controller.signal,
    });
    if (refreshId !== refreshSequence) return null;
    let optionPayload;
    if (owner.canCreate) {
      let readyFiles = null;
      try {
        readyFiles = await listFiles({
          controlApiUrl,
          token: authSession.token,
          status: "ready",
          limit: 100,
          signal: operation.controller.signal,
        });
      } catch (error) {
        if (
          isBatchAbortError(error)
          || (error instanceof AuthApiError && error.status === 401)
          || isBatchPermissionRevocation(error)
        ) throw error;
        // Non-auth candidate failures keep the readable Job list while the create form fails closed.
      }
      optionPayload = {
        projects: resourceStates?.projectState?.state === "ok"
          ? resourceStates.projectState.projects
          : null,
        environments: resourceStates?.environmentState?.state === "ok"
          ? resourceStates.environmentState.environments
          : null,
        modelAliases: resourceStates?.modelAliasesState?.state === "ok"
          ? resourceStates.modelAliasesState.model_aliases
          : null,
        files: readyFiles?.state === "ok" ? readyFiles.files : null,
      };
    }
    const currentOwner = currentBatchReadOwner(consoleShellState);
    accepted = batchJobPageState.acceptList(
      operation,
      listResult,
      currentOwner,
      refreshId,
      optionPayload,
    );
  } catch (error) {
    const currentOwner = currentBatchReadOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !batchJobPageState.isListCurrent(operation, currentOwner, refreshId)
      || isBatchAbortError(error)
    ) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (handleBatchAuthenticationFailure(error)) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (isBatchPermissionRevocation(error)) {
      batchJobPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    batchJobPageState.rejectList(operation, error, currentOwner, refreshId);
    return batchJobPageState.snapshot();
  } finally {
    batchJobPageState.finishList(operation);
  }
  if (!accepted || refreshId !== refreshSequence) return null;
  if (!batchJobPageState.snapshot().selectedJobId) return batchJobPageState.snapshot();
  await loadBatchJobDetailState(consoleShellState, refreshId);
  return refreshId === refreshSequence ? batchJobPageState.snapshot() : null;
}

/** Load the selected Job and one private-cursor Task page under a single detail ticket. */
/** 加载批任务列表或详情，并用刷新代次阻止旧租户响应覆盖当前页面。 */
async function loadBatchJobDetailState(consoleShellState, refreshId) {
  const owner = currentBatchReadOwner(consoleShellState);
  const operation = batchJobPageState.beginDetail(owner, refreshId);
  if (!operation) return null;
  try {
    const [detailResult, taskResult] = await Promise.all([
      getBatchJob({
        controlApiUrl,
        token: authSession.token,
        batchJobId: operation.batchJobId,
        signal: operation.controller.signal,
      }),
      listBatchTasks({
        controlApiUrl,
        token: authSession.token,
        batchJobId: operation.batchJobId,
        ...batchJobPageState.taskQuery(),
        limit: 50,
        signal: operation.controller.signal,
      }),
    ]);
    if (refreshId !== refreshSequence) return null;
    const currentOwner = currentBatchReadOwner(consoleShellState);
    batchJobPageState.acceptDetail(
      operation,
      detailResult,
      taskResult,
      currentOwner,
      refreshId,
    );
    return batchJobPageState.snapshot();
  } catch (error) {
    const currentOwner = currentBatchReadOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !batchJobPageState.isDetailCurrent(operation, currentOwner, refreshId)
      || isBatchAbortError(error)
    ) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (handleBatchAuthenticationFailure(error)) return null;
    // 页面鉴权或所有者快照失效时清理状态；前端不得恢复后端已撤销权限。
    if (isBatchPermissionRevocation(error)) {
      batchJobPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    batchJobPageState.rejectDetail(operation, error, currentOwner, refreshId);
    return batchJobPageState.snapshot();
  } finally {
    batchJobPageState.finishDetail(operation);
  }
}

/** 在同一授权 owner 下读取 PAGE-105 列表，并裁剪可用创建候选。 */
async function loadExperimentsState(
  consoleShellState,
  refreshId,
  resourceStates,
  { preserveFinalOnStatisticsUnavailable = false } = {},
) {
  const owner = currentExperimentOwner(consoleShellState);
  if (refreshId !== refreshSequence || !experimentPageState.enter(owner)) return null;
  const operation = experimentPageState.beginList(owner, refreshId);
  if (!operation) return null;
  let accepted = false;
  try {
    const response = await listExperiments({
      controlApiUrl,
      authentication: "workspace",
      ...experimentPageState.listQuery(),
      limit: 50,
      signal: operation.controller.signal,
    });
    if (refreshId !== refreshSequence) return null;
    const optionPayload = owner.canCreate
      ? {
          projects: resourceStates?.projectState?.state === "ok"
            ? resourceStates.projectState.projects
            : null,
          environments: resourceStates?.environmentState?.state === "ok"
            ? resourceStates.environmentState.environments
            : null,
          modelAliases: resourceStates?.modelAliasesState?.state === "ok"
            ? resourceStates.modelAliasesState.model_aliases
            : null,
          models: resourceStates?.modelsState?.state === "ok"
            ? resourceStates.modelsState.models
            : null,
          requestId: [
            resourceStates?.projectState,
            resourceStates?.environmentState,
            resourceStates?.modelAliasesState,
            resourceStates?.modelsState,
          ].find((state) => state?.state === "error")?.requestId ?? "-",
        }
      : undefined;
    accepted = experimentPageState.acceptList(
      operation,
      response,
      currentExperimentOwner(consoleShellState),
      refreshId,
      optionPayload,
    );
  } catch (error) {
    const currentOwner = currentExperimentOwner(consoleShellState);
    // 列表旧代次、owner 漂移或主动 abort 都不允许回写错误状态。
    if (
      refreshId !== refreshSequence
      || !experimentPageState.isListCurrent(operation, currentOwner)
      || isExperimentAbortError(error)
    ) return null;
    // 当前 owner 的认证失败切断 Session；旧回调已在上方被 generation 拒绝。
    if (handleExperimentAuthenticationFailure(error)) return null;
    // 后端确认撤权时同步销毁 cursor/详情/创建候选，并异步刷新导航。
    if (isExperimentPermissionRevocation(error)) {
      experimentPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    experimentPageState.rejectList(operation, error, currentOwner);
    return experimentPageState.snapshot();
  } finally {
    experimentPageState.finishList(operation);
  }
  if (!accepted || refreshId !== refreshSequence) return null;
  if (!experimentPageState.snapshot().selectedExperimentId) {
    return experimentPageState.snapshot();
  }
  await loadExperimentDetailState(consoleShellState, refreshId, {
    preserveFinalOnStatisticsUnavailable,
  });
  return refreshId === refreshSequence ? experimentPageState.snapshot() : null;
}

/** 同时读取详情和后端权威统计；浏览器不合并请求级 Usage、Feedback 或 FinOps 事实。 */
async function loadExperimentDetailState(
  consoleShellState,
  refreshId,
  { preserveFinalOnStatisticsUnavailable = false } = {},
) {
  const owner = currentExperimentOwner(consoleShellState);
  const detailOperation = experimentPageState.beginDetail(owner, refreshId);
  const resultsOperation = experimentPageState.beginResults(owner, refreshId);
  if (!detailOperation || !resultsOperation) return null;
  try {
    const [detail, results] = await Promise.all([
      getExperiment({
        controlApiUrl,
        authentication: "workspace",
        experimentId: detailOperation.experimentId,
        signal: detailOperation.controller.signal,
      }),
      getExperimentResults({
        controlApiUrl,
        authentication: "workspace",
        experimentId: resultsOperation.experimentId,
        signal: resultsOperation.controller.signal,
      }),
    ]);
    if (refreshId !== refreshSequence) return null;
    experimentPageState.acceptDetail(
      detailOperation,
      resultsOperation,
      detail,
      results,
      currentExperimentOwner(consoleShellState),
    );
    return experimentPageState.snapshot();
  } catch (error) {
    const currentOwner = currentExperimentOwner(consoleShellState);
    // 详情与结果共享 owner；旧代次或 abort 不能留下半份错误或统计。
    if (
      refreshId !== refreshSequence
      || !experimentPageState.isDetailCurrent(detailOperation, currentOwner)
      || !experimentPageState.isResultsCurrent(resultsOperation, currentOwner)
      || isExperimentAbortError(error)
    ) return null;
    // 当前 owner 的 401 统一退出登录，不能继续显示旧租户详情。
    if (handleExperimentAuthenticationFailure(error)) return null;
    // 读取权限撤回时立即清除详情和结果，再重新获取后端导航快照。
    if (isExperimentPermissionRevocation(error)) {
      experimentPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    experimentPageState.rejectDetail(
      detailOperation,
      resultsOperation,
      error,
      currentOwner,
      { preserveFinalOnStatisticsUnavailable },
    );
    return experimentPageState.snapshot();
  } finally {
    experimentPageState.finishDetail(detailOperation);
    experimentPageState.finishResults(resultsOperation);
  }
}

/** 在同一授权 owner 下读取 PAGE-106 策略；普通 GET 不能覆盖 dirty/conflict 草稿。 */
async function loadResponseCacheState(
  consoleShellState,
  refreshId,
  { rebase = false } = {},
) {
  const owner = currentResponseCacheOwner(consoleShellState);
  if (refreshId !== refreshSequence || !responseCachePageState.enter(owner)) return null;
  const operation = responseCachePageState.beginLoad(owner, { rebase });
  if (!operation) return responseCachePageState.snapshot();
  try {
    const response = await listResponseCachePolicies({
      controlApiUrl,
      token: authSession.token,
      tenantId: owner.tenantId,
      limit: 50,
      cursor: operation.cursor,
      signal: operation.controller.signal,
    });
    if (refreshId !== refreshSequence) return null;
    const accepted = responseCachePageState.acceptLoad(
      operation,
      response,
      currentResponseCacheOwner(consoleShellState),
    );
    return accepted ? responseCachePageState.snapshot() : null;
  } catch (error) {
    const currentOwner = currentResponseCacheOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !responseCachePageState.isLoadCurrent(operation, currentOwner)
      || isResponseCacheAbortError(error)
    ) return null;
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (handleResponseCacheAuthenticationFailure(error)) return null;
    // 控制台只展示后端授权的脱敏事实，身份或权限变化时必须清除旧页面数据。
    if (isResponseCachePermissionRevocation(error)) {
      responseCachePageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    responseCachePageState.rejectLoad(operation, error, currentOwner);
    return responseCachePageState.snapshot();
  } finally {
    responseCachePageState.finishLoad(operation);
  }
}

/** PAGE-111 只在同一授权 owner 下读取；打开或 busy Dialog 时不启动 GET。 */
async function loadQuotaManagementState(consoleShellState, refreshId) {
  const owner = currentQuotaManagementOwner(consoleShellState);
  if (refreshId !== refreshSequence || !quotaManagementPageState.enter(owner)) return null;
  const operation = quotaManagementPageState.beginLoad(owner);
  if (!operation) return quotaManagementPageState.snapshot();
  try {
    const response = await getQuotaBuckets({
      controlApiUrl,
      token: authSession.token,
      tenantId: owner.tenantId,
      signal: operation.controller.signal,
    });
    if (refreshId !== refreshSequence) return null;
    const accepted = quotaManagementPageState.acceptLoad(
      operation,
      response,
      currentQuotaManagementOwner(consoleShellState),
    );
    return accepted ? quotaManagementPageState.snapshot() : null;
  } catch (error) {
    const currentOwner = currentQuotaManagementOwner(consoleShellState);
    if (
      refreshId !== refreshSequence
      || !quotaManagementPageState.isLoadCurrent(operation, currentOwner)
      || isWorkspaceQuotaAbortError(error)
    ) return null;
    // 当前读取的认证失败统一清会话，权限撤销则清页面并重建后端导航。
    if (handleWorkspaceQuotaAuthenticationFailure(error)) return null;
    if (isWorkspaceQuotaPermissionRevocation(error)) {
      quotaManagementPageState.clear();
      globalThis.window?.setTimeout?.(() => void refresh(), 0);
      return null;
    }
    quotaManagementPageState.rejectLoad(operation, error, currentOwner);
    return quotaManagementPageState.snapshot();
  } finally {
    quotaManagementPageState.finishLoad(operation);
  }
}

/** PAGE-112 list loader; no read permission means no hidden prefetch or retained DOM. */
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
  const identity = currentEvaluationPageIdentity("model-quality-scores", PERMISSIONS.MODEL_QUALITY_READ);
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
      authentication: "workspace",
      modelId: modelQualityScoreFilters.modelId,
      limit: modelQualityScoreFilters.limit,
      offset: modelQualityScoreFilters.offset,
      signal: ticket.signal,
    });
    if (refreshId !== refreshSequence || !modelQualityScorePageMemory.isCurrentRefresh(ticket)
      || identity !== currentEvaluationPageIdentity("model-quality-scores", PERMISSIONS.MODEL_QUALITY_READ)) {
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
      || identity !== currentEvaluationPageIdentity("model-quality-scores", PERMISSIONS.MODEL_QUALITY_READ)
    ) {
      return null;
    }
    if (handleResourceAuthorizationError(error)) return null;
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
    identity: currentEvaluationPageIdentity("audit-logs", PERMISSIONS.AUDIT_EXPORT),
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
 * tenant、用户、Session、权限 revision 或授权指纹变化时销毁 PAGE-103 文件名、报告和命令。
 * file.read 被撤销时 identity 收敛为空；file.security.scan 变化也由授权指纹触发 owner 重建。
 */
function clearFileSecurityStateOnIdentityChange(previous, current) {
  if (fileSecurityReadIdentitySnapshot(previous) !== fileSecurityReadIdentitySnapshot(current)) {
    fileSecurityPageState.clear();
  }
}

/** Destroy PAGE-104 facts when tenant, Session, revision, fingerprint or batch.read identity changes. */
function clearBatchJobStateOnIdentityChange(previous, current) {
  if (batchReadIdentitySnapshot(previous) !== batchReadIdentitySnapshot(current)) {
    batchJobPageState.clear();
  }
}

/** Destroy PAGE-114 facts when tenant, Session, revision, fingerprint or mcp.read changes. */
function clearMcpToolStateOnIdentityChange(previous, current) {
  if (mcpToolReadIdentitySnapshot(previous) !== mcpToolReadIdentitySnapshot(current)) {
    mcpToolPageState.clear();
  }
}

/** Destroy PAGE-116 facts when tenant, Session, RBAC revision, fingerprint, or any local capability drifts. */
function clearLocalEndpointStateOnIdentityChange(previous, current) {
  if (localEndpointReadIdentitySnapshot(previous) !== localEndpointReadIdentitySnapshot(current)) {
    localEndpointPageState.clear();
  }
}

/** Destroy PRD-120 facts and one-use values on any Edge read owner drift. */
function clearPrivateGatewayStateOnIdentityChange(previous, current) {
  if (privateGatewayReadIdentitySnapshot(previous) !== privateGatewayReadIdentitySnapshot(current)) {
    privateGatewayPageState.clear();
    privateGatewayBrandingShellState = null;
    deploymentBrandingController.clear();
  }
}

/** Destroy PRD-131 assessment and cutover facts on any migration.read owner drift. */
function clearMigrationOperationStateOnIdentityChange(previous, current) {
  if (migrationOperationReadIdentitySnapshot(previous)
    !== migrationOperationReadIdentitySnapshot(current)) {
    migrationOperationPageState.clear();
  }
}

/** Destroy PRD-132 subjects, messages, attachment facts, and requests on any support read owner drift. */
function clearSupportTicketStateOnIdentityChange(previous, current) {
  if (supportTicketReadIdentitySnapshot(previous) !== supportTicketReadIdentitySnapshot(current)) {
    supportTicketPageState.clear();
  }
}

/** Destroy PAGE-119 branding, aggregates, dialogs, and attachment tickets on any read-owner drift. */
function clearExperimentStateOnIdentityChange(previous, current) {
  if (experimentReadIdentitySnapshot(previous) !== experimentReadIdentitySnapshot(current)) {
    experimentPageState.clear();
  }
}

/** tenant、Session 或完整 RBAC snapshot 变化时销毁 PAGE-106 策略、草稿与 Dialog。 */
function clearResponseCacheStateOnIdentityChange(previous, current) {
  if (responseCacheReadIdentitySnapshot(previous) !== responseCacheReadIdentitySnapshot(current)) {
    responseCachePageState.clear();
  }
}

/** tenant、Session、权限 revision 或额度授权变化时销毁 Workspace quota 状态。 */
function clearWorkspaceQuotaStateOnIdentityChange(previous, current) {
  if (quotaReadIdentitySnapshot(previous) !== quotaReadIdentitySnapshot(current)) {
    quotaManagementPageState.clear();
  }
}

/** Destroy PAGE-112 on tenant, Session, permission revision, matrix or fingerprint drift. */
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
    identity: currentEvaluationPageIdentity("feedback-summary", PERMISSIONS.FEEDBACK_USE),
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
      authentication: "workspace",
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
    if (handleResourceAuthorizationError(error)) return null;
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
    || !["request-logs", "request-explanations"].includes(consoleShellState?.activeNavigationItemId)
  ) {
    return;
  }

  const form = document.querySelector("#request-explanation-form");
  const owner = currentRequestExplanationOwner();
  const panel = document.querySelector(owner.navigation === "request-logs"
    ? ".request-logs-panel" : ".request-explanations-panel");
  const binding = bindResourceElement(panel, [PERMISSIONS.REQUEST_EXPLANATION_READ], owner.navigation);
  const allowed = () => resourcePageLifecycle.isBindingCurrent(binding);
  if (!allowed()) return;
  const requestIdInput = form?.querySelector("[name='requestId']");
  requestIdInput?.addEventListener("input", () => {
    if (!allowed() || !requestIdInput.isConnected) return;
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
      if (!allowed() || !sectionElement.isConnected) return;
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
      if (!allowed() || !control.isConnected) return;
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
    if (!allowed() || !form.isConnected) return;
    const data = new FormData(form);
    await runRequestExplanationRead(String(data.get("requestId") ?? ""), "manual", binding,
      consoleShellState);
  });

  const refreshButton = document.querySelector("#request-explanation-refresh");
  refreshButton?.addEventListener("click", async () => {
    if (!allowed() || !refreshButton.isConnected) return;
    const query = requestExplanationPageMemory.query();
    await runRequestExplanationRead(query.requestId, query.source, binding, consoleShellState);
  });

  const clearButton = document.querySelector("#request-explanation-clear");
  clearButton?.addEventListener("click", async () => {
    if (!allowed() || !clearButton.isConnected) return;
    const mutation = resourcePageLifecycle.beginMutation(binding);
    if (!mutation) return;
    try {
      requestExplanationPageMemory.clearSelection(currentRequestExplanationOwner());
      requestExplanationHandoff.clear();
      renderCurrentRequestExplanationSnapshot(consoleShellState);
    } finally {
      resourcePageLifecycle.finishMutation(mutation);
    }
  });
}

async function runRequestExplanationRead(requestId, source, binding, consoleShellState) {
  const mutation = resourcePageLifecycle.beginMutation(binding);
  if (!mutation) return;
  try {
    const owner = currentRequestExplanationOwner();
    requestExplanationPageMemory.select({ ...owner, requestId, source });
    renderCurrentRequestExplanationSnapshot(consoleShellState);
    await loadRequestExplanationState(consoleShellState, refreshSequence, mutation);
    if (resourcePageLifecycle.isMutationCurrent(mutation)) {
      renderCurrentRequestExplanationSnapshot(consoleShellState);
    }
  } finally {
    resourcePageLifecycle.finishMutation(mutation);
  }
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
  const navigation = resourcePageShellState?.activeNavigationItemId;
  const identity = ["request-logs", "request-explanations"].includes(navigation)
    ? currentEvaluationPageIdentity(navigation, PERMISSIONS.REQUEST_EXPLANATION_READ) : "";
  return {
    identity,
    tenantId: "",
    navigation: identity ? navigation : "",
    refreshId,
    currentRefreshId: refreshId,
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
async function loadRequestExplanationState(consoleShellState, refreshId, mutation) {
  const owner = currentRequestExplanationOwner(refreshId);
  if (
    !hasPermission(authSession.context, PERMISSIONS.REQUEST_EXPLANATION_READ)
    || !["request-logs", "request-explanations"].includes(consoleShellState?.activeNavigationItemId)
    || refreshId !== refreshSequence
    || !owner.identity
    || !resourcePageLifecycle.isMutationCurrent(mutation)
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
      authentication: "workspace",
      requestId: ticket.requestId,
      signal: ticket.signal,
    });
    const currentOwner = currentRequestExplanationOwner(refreshId);
    if (!resourcePageLifecycle.isMutationCurrent(mutation)
      || !requestExplanationPageMemory.isCurrent(ticket, currentOwner)) {
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
    if (isAbortError(error) || !resourcePageLifecycle.isMutationCurrent(mutation)
      || !requestExplanationPageMemory.isCurrent(ticket, currentOwner)) {
      return null;
    }
    // 只有当前解释 owner 的 401 才能结束会话；上方门禁已排除迟到的旧身份响应。
    if (handleResourceAuthorizationError(error)) return null;
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
  const binding = bindResourceElement(button, [PERMISSIONS.REQUEST_LOG_READ], "output-safety");
  button.addEventListener("click", async () => {
    if (!resourcePageLifecycle.isBindingCurrent(binding) || !panel.contains(button)) return;
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
      if (resourcePageLifecycle.isBindingCurrent(binding) && panel.contains(button)) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
      if (resourcePageLifecycle.isBindingCurrent(binding)) detailPane?.removeAttribute("aria-busy");
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
      || !requestLogInteractionAllowed(consoleShellState)
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
    REQUEST_LOG_NAVIGATION_IDS.has(consoleShellState?.activeNavigationItemId)
    && currentEvaluationPageIdentity(consoleShellState.activeNavigationItemId, PERMISSIONS.REQUEST_LOG_READ),
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
  { clearSelection = false, activeNavigationItemId = "", binding } = {},
) {
  // 事件处理器之外再做一次导航与身份核验，手工触发的旧 DOM 回调不能复活已离开的 owner。
  if (!requestLogInteractionAllowed({ activeNavigationItemId })) return;
  const mutation = resourcePageLifecycle.beginMutation(binding);
  if (!mutation) return;
  const owner = currentRequestLogOwner();
  const ticket = requestLogPageMemory.beginSavedFilterMutation(owner);
  // 不完整身份或已失效 Session 不得发出 SavedFilter 请求，DOM 可见性不能替代该内存门禁。
  if (!ticket) {
    resourcePageLifecycle.finishMutation(mutation);
    return;
  }
  try {
    await operation(ticket.signal);
    const currentOwner = currentRequestLogOwner();
    if (!resourcePageLifecycle.isMutationCurrent(mutation)
      || !requestLogPageMemory.isCurrentSavedFilterMutation(ticket, currentOwner)) return;
    requestLogPageMemory.finishSavedFilterMutation(ticket, currentOwner);
    if (clearSelection) {
      configureCurrentRequestLogState({
        ...requestLogPageMemory.query().filters,
        selectedSavedFilterId: "",
        offset: 0,
      }, null);
    }
    resourcePageLifecycle.finishMutation(mutation);
    await refresh();
  } catch (error) {
    const currentOwner = currentRequestLogOwner();
    // 主动取消和 owner 迁移都静默丢弃，禁止旧用户错误覆盖新身份页面。
    if (isAbortError(error) || !resourcePageLifecycle.isMutationCurrent(mutation)
      || !requestLogPageMemory.isCurrentSavedFilterMutation(ticket, currentOwner)) return;
    if (handleResourceAuthorizationError(error)) return;
    requestLogPageMemory.acceptSavedFilterMutationError(
      ticket,
      safeRequestLogClientError(error, "保存筛选操作失败"),
      currentOwner,
    );
    requestLogPageMemory.finishSavedFilterMutation(ticket, currentOwner);
    resourcePageLifecycle.finishMutation(mutation);
    await refresh();
  } finally {
    resourcePageLifecycle.finishMutation(mutation);
  }
}

/** 返回当前 RequestLog owner；refresh、身份、租户和两个允许的导航 ID 均从实时状态读取。 */
function currentRequestLogOwner(
  refreshId = refreshSequence,
  requestedNavigation = currentRenderedNavigation(),
) {
  const workspaceOwner = workspaceResourcePageOwner(authSession, resourcePageShellState, {
    navigationId: requestedNavigation, permissions: [PERMISSIONS.REQUEST_LOG_READ], hash: readActiveNavigationHash(),
  });
  const identity = workspaceOwner?.key ?? "";
  const tenantId = "";
  const current = refreshId === refreshSequence;
  const navigation = REQUEST_LOG_NAVIGATION_IDS.has(requestedNavigation)
    ? requestedNavigation
    : "";
  return {
    identity,
    tenantId,
    workspaceOwner,
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
    workspaceOwner: owner.workspaceOwner,
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
    || !owner.workspaceOwner
  ) {
    return null;
  }

  if (
    activeNavigation === "output-safety"
    && requestLogPageMemory.query().filters.terminalStage !== "output_safety"
  ) {
    // 首次进入或从普通日志切换时冻结安全阶段；旧 SavedFilter 和手写 hash 均不能移除该条件。
    requestLogPageMemory.configure({
      workspaceOwner: owner.workspaceOwner,
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
  try {
    // 两个 GET 共用同一 list ticket；任一迟到响应都必须通过完整身份和 refresh 代次。
    const savedFiltersPromise = listSavedRequestLogFilters({
      controlApiUrl,
      authentication: "workspace",
      signal: ticket.signal,
    }).then(
      (value) => ({ value }),
      (error) => ({ error }),
    );
    const state = await listRequestLogs({
      controlApiUrl,
      authentication: "workspace",
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
        authentication: "workspace",
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
      if (handleResourceAuthorizationError(error)) return null;
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
    if (handleResourceAuthorizationError(error)) return null;
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
  const filters = { ...auditLogFilters };
  return loadResourceState("audit-logs", [PERMISSIONS.AUDIT_READ],
    (options) => listAuditLogs({ ...options, ...filters }),
    (state) => {
      const rows = state?.audit_logs;
      if (!Array.isArray(rows) || rows.some((row) => !row?.tenant_id)
        || new Set(rows.map((row) => row.tenant_id)).size > 1) {
        throw new AuthApiError({ status: 502, code: "audit_log.response_invalid",
          message: "审计日志响应不完整", requestId: state?.request_id ?? "-" });
      }
      return { ...state, filters, exportState: { ...auditExportState } };
    }, { filters, exportState: { ...auditExportState } });
}

/**
 * 执行 控制台主应用 的 `loadVirtualKeysState` 辅助逻辑，保持页面状态和后端响应口径一致。
 * 边界：不得在前端伪造权限结果或展示未脱敏的密钥、Token、Prompt。
 */
async function loadVirtualKeysState() {
  return loadResourceState("virtual-keys", [PERMISSIONS.VIRTUAL_KEY_READ],
    async (options) => {
      const keys = await listVirtualKeys(options);
      if (!hasPermission(authSession?.context, PERMISSIONS.USER_MANAGE)) return keys;
      try {
        return { ...keys, consumerDirectory: await listConsumers({ ...options, offset: consumerDirectoryOffset }) };
      } catch (error) {
        if (isAbortError(error) || error.status === 401 || error.status === 403) throw error;
        return { ...keys, consumerDirectory: { state: "error", message: authErrorMessage(error) } };
      }
    },
    (state) => ({ ...state, created: virtualKeyCreatedSecret,
      actionError: virtualKeyActionError, feedback: virtualKeyFeedback }));
}
