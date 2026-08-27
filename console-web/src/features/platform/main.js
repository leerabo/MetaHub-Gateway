// 平台入口只装配受权限约束的 API 与页面控制器；不得在全局状态持有 Session、TOTP 或 Gateway 签名材料。
import {
  addPlatformWorkspaceRiskTag,
  approveSupportGrant,
  createBreakGlassSession,
  createPlatformOperator,
  createPlatformSaasPlan,
  createPlatformWorkspaceOwnerNotification,
  createSupportGrant,
  createSupportSession,
  getPlatformNavigation,
  getPlatformProviderCatalog,
  getPlatformSession,
  getPlatformWorkspace,
  getPlatformWorkspaceHealth,
  getSupportDiagnostics,
  listPlatformAudit,
  listPlatformOperators,
  listPlatformSaasPlans,
  listPlatformWorkspaceEvents,
  listPlatformWorkspaces,
  listSupportGrants,
  openPlatformSession,
  publishPlatformProviderCatalog,
  removePlatformWorkspaceRiskTag,
  restorePlatformWorkspace,
  revokeSupportGrant,
  suspendPlatformWorkspace,
  updatePlatformOperator,
} from "../../lib/platform.js";
import {
  createPlatformBillingAdjustment,
  listPlatformBillingSummary,
} from "../../lib/platformBilling.js";
import {
  listPlatformRefunds,
  reviewPlatformRefund,
} from "../../lib/paymentCommerce.js";
import {
  createPlatformInvoiceReversal,
  createPlatformReconciliation,
  listPlatformReconciliationDifferences,
  reviewPlatformInvoiceReversal,
} from "../../lib/invoiceReconciliation.js";
import {
  createPlatformBackup,
  getPlatformBackup,
  getPlatformInfrastructureStatus,
} from "../../lib/platformInfrastructure.js";
import {
  getPlatformGatewayCells,
  updatePlatformGatewayPlacement,
} from "../../lib/platformGateway.js";
import {
  issuePlatformEvidenceDownloadToken,
  listPlatformBusinessVerifications,
  reviewPlatformBusinessVerification,
} from "../../lib/businessVerification.js";
import {
  createPlatformNotificationTemplate,
  listPlatformNotificationTemplates,
} from "../../lib/notifications.js";
import {
  cancelPlatformJob,
  commandPlatformJobType,
  getPlatformJob,
  listPlatformJobQueues,
  retryPlatformJob,
} from "../../lib/platformJobs.js";
import {
  commandFeatureRollout,
  createFeatureFlag,
  killFeatureFlag,
  listFeatureFlags,
} from "../../lib/featureRollouts.js";
import {
  listConfigDistribution,
  rebuildWorkspaceConfig,
} from "../../lib/configDistribution.js";
import {
  acknowledgePlatformAlert,
  createServiceLevelObjective,
  listErrorBudgets,
  listObservableServices,
  listServiceLevelObjectives,
} from "../../lib/observability.js";
import {
  decideRiskCase,
  listRiskCases,
} from "../../lib/riskControls.js";
import {
  getDisasterRecoveryStatus,
  listDisasterRecoveryBackups,
  startDisasterRecoveryDrill,
  startRegionFailover,
} from "../../lib/disasterRecovery.js";
import {
  approvePlatformSecurityIncident,
  createPlatformIncident,
  createPlatformMaintenanceWindow,
  getPlatformStatusIncidents,
  publishPlatformPostmortem,
  updatePlatformIncident,
} from "../../lib/statusIncidents.js";
import {
  commandCapacityChaos,
  createCapacityForecast,
  createCapacityLoadTest,
  getPlatformCapacity,
  getPlatformUnitEconomics,
} from "../../lib/capacityManagement.js";
import { listPlatformResidencyEvidence } from "../../lib/dataGovernance.js";
import {
  cancelPlatformDiagnostic,
  listPlatformFleet,
  requestPlatformDiagnosticGrant,
  runPlatformDiagnostic,
} from "../../lib/edgeDiagnostics.js";
import {
  beginIdentityWebAuthnStepUp,
  createIdentityStepUpGrant,
  getWebAuthnAssertion,
} from "../../lib/identity.js";
import { createPlatformPageController } from "./platformPageState.js";

const root = globalThis.document?.querySelector("#platform-app");
if (root) {
  const hostname = globalThis.window.location.hostname;
  const controlApiUrl = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname)
    ? `${globalThis.window.location.protocol}//${hostname}:8001`
    : globalThis.window.location.origin;
  createPlatformPageController({
    root,
    api: {
      session: (input) => getPlatformSession({ controlApiUrl, ...input }),
      openSession: (input) => openPlatformSession({ controlApiUrl, ...input }),
      navigation: (input) => getPlatformNavigation({ controlApiUrl, ...input }),
      workspaces: (input) => listPlatformWorkspaces({ controlApiUrl, ...input }),
      workspace: (input) => getPlatformWorkspace({ controlApiUrl, ...input }),
      workspaceHealth: (input) => getPlatformWorkspaceHealth({ controlApiUrl, ...input }),
      workspaceEvents: (input) => listPlatformWorkspaceEvents({ controlApiUrl, ...input }),
      suspendWorkspace: (input) => suspendPlatformWorkspace({ controlApiUrl, ...input }),
      restoreWorkspace: (input) => restorePlatformWorkspace({ controlApiUrl, ...input }),
      addWorkspaceRiskTag: (input) => addPlatformWorkspaceRiskTag({ controlApiUrl, ...input }),
      removeWorkspaceRiskTag: (input) => removePlatformWorkspaceRiskTag({ controlApiUrl, ...input }),
      notifyWorkspaceOwner: (input) => createPlatformWorkspaceOwnerNotification({ controlApiUrl, ...input }),
      operators: (input) => listPlatformOperators({ controlApiUrl, ...input }),
      plans: (input) => listPlatformSaasPlans({ controlApiUrl, ...input }),
      providerCatalog: (input) => getPlatformProviderCatalog({ controlApiUrl, ...input }),
      publishProviderCatalog: (input) => publishPlatformProviderCatalog({
        controlApiUrl,
        ...input,
      }),
      createPlan: (input) => createPlatformSaasPlan({ controlApiUrl, ...input }),
      createOperator: (input) => createPlatformOperator({ controlApiUrl, ...input }),
      updateOperator: (input) => updatePlatformOperator({ controlApiUrl, ...input }),
      grants: (input) => listSupportGrants({ controlApiUrl, ...input }),
      createGrant: (input) => createSupportGrant({ controlApiUrl, ...input }),
      approveGrant: (input) => approveSupportGrant({ controlApiUrl, ...input }),
      revokeGrant: (input) => revokeSupportGrant({ controlApiUrl, ...input }),
      supportSession: (input) => createSupportSession({ controlApiUrl, ...input }),
      diagnostics: (input) => getSupportDiagnostics({ controlApiUrl, ...input }),
      breakGlass: (input) => createBreakGlassSession({ controlApiUrl, ...input }),
      audit: (input) => listPlatformAudit({ controlApiUrl, ...input }),
      billingSummary: (input) => listPlatformBillingSummary({ controlApiUrl, ...input }),
      createBillingAdjustment: (input) => createPlatformBillingAdjustment({
        controlApiUrl,
        ...input,
      }),
      refunds: (input) => listPlatformRefunds({ controlApiUrl, ...input }),
      reviewRefund: (input) => reviewPlatformRefund({ controlApiUrl, ...input }),
      createReconciliation: (input) => createPlatformReconciliation({
        controlApiUrl,
        ...input,
      }),
      reconciliationDifferences: (input) => listPlatformReconciliationDifferences({
        controlApiUrl,
        ...input,
      }),
      createInvoiceReversal: (input) => createPlatformInvoiceReversal({
        controlApiUrl,
        ...input,
      }),
      reviewInvoiceReversal: (input) => reviewPlatformInvoiceReversal({
        controlApiUrl,
        ...input,
      }),
      infrastructureStatus: (input) => getPlatformInfrastructureStatus({
        controlApiUrl,
        ...input,
      }),
      createBackup: (input) => createPlatformBackup({ controlApiUrl, ...input }),
      backup: (input) => getPlatformBackup({ controlApiUrl, ...input }),
      gatewayCells: (input) => getPlatformGatewayCells({ controlApiUrl, ...input }),
      updateGatewayPlacement: (input) => updatePlatformGatewayPlacement({
        controlApiUrl,
        ...input,
      }),
      businessVerifications: (input) => listPlatformBusinessVerifications({
        controlApiUrl,
        ...input,
      }),
      reviewBusinessVerification: (input) => reviewPlatformBusinessVerification({
        controlApiUrl,
        ...input,
      }),
      evidenceToken: (input) => issuePlatformEvidenceDownloadToken({
        controlApiUrl,
        ...input,
      }),
      notificationTemplates: (input) => listPlatformNotificationTemplates({
        controlApiUrl,
        ...input,
      }),
      createNotificationTemplate: (input) => createPlatformNotificationTemplate({
        controlApiUrl,
        ...input,
      }),
      jobQueues: (input) => listPlatformJobQueues({ controlApiUrl, ...input }),
      job: (input) => getPlatformJob({ controlApiUrl, ...input }),
      retryJob: (input) => retryPlatformJob({ controlApiUrl, ...input }),
      cancelJob: (input) => cancelPlatformJob({ controlApiUrl, ...input }),
      commandJobType: (input) => commandPlatformJobType({ controlApiUrl, ...input }),
      featureFlags: (input) => listFeatureFlags({ controlApiUrl, ...input }),
      createFeatureFlag: (input) => createFeatureFlag({ controlApiUrl, ...input }),
      commandFeatureRollout: (input) => commandFeatureRollout({
        controlApiUrl,
        ...input,
      }),
      killFeatureFlag: (input) => killFeatureFlag({ controlApiUrl, ...input }),
      configDistribution: (input) => listConfigDistribution({ controlApiUrl, ...input }),
      rebuildWorkspaceConfig: (input) => rebuildWorkspaceConfig({
        controlApiUrl,
        ...input,
      }),
      observabilityServices: (input) => listObservableServices({ controlApiUrl, ...input }),
      serviceLevelObjectives: (input) => listServiceLevelObjectives({
        controlApiUrl,
        ...input,
      }),
      errorBudgets: (input) => listErrorBudgets({ controlApiUrl, ...input }),
      createServiceLevelObjective: (input) => createServiceLevelObjective({
        controlApiUrl,
        ...input,
      }),
      acknowledgePlatformAlert: (input) => acknowledgePlatformAlert({
        controlApiUrl,
        ...input,
      }),
      riskCases: (input) => listRiskCases({ controlApiUrl, ...input }),
      decideRiskCase: (input) => decideRiskCase({ controlApiUrl, ...input }),
      disasterRecoveryStatus: (input) => getDisasterRecoveryStatus({
        controlApiUrl,
        ...input,
      }),
      disasterRecoveryBackups: (input) => listDisasterRecoveryBackups({
        controlApiUrl,
        ...input,
      }),
      startDisasterRecoveryDrill: (input) => startDisasterRecoveryDrill({
        controlApiUrl,
        ...input,
      }),
      startRegionFailover: (input) => startRegionFailover({ controlApiUrl, ...input }),
      statusIncidents: (input) => getPlatformStatusIncidents({ controlApiUrl, ...input }),
      createStatusIncident: (input) => createPlatformIncident({ controlApiUrl, ...input }),
      approveStatusIncident: (input) => approvePlatformSecurityIncident({
        controlApiUrl,
        ...input,
      }),
      updateStatusIncident: (input) => updatePlatformIncident({ controlApiUrl, ...input }),
      publishStatusPostmortem: (input) => publishPlatformPostmortem({
        controlApiUrl,
        ...input,
      }),
      createStatusMaintenance: (input) => createPlatformMaintenanceWindow({
        controlApiUrl,
        ...input,
      }),
      capacity: (input) => getPlatformCapacity({ controlApiUrl, ...input }),
      unitEconomics: (input) => getPlatformUnitEconomics({ controlApiUrl, ...input }),
      createCapacityForecast: (input) => createCapacityForecast({ controlApiUrl, ...input }),
      createCapacityLoadTest: (input) => createCapacityLoadTest({ controlApiUrl, ...input }),
      commandCapacityChaos: (input) => commandCapacityChaos({ controlApiUrl, ...input }),
      residencyEvidence: (input) => listPlatformResidencyEvidence({ controlApiUrl, ...input }),
      edgeFleet: (input) => listPlatformFleet({ controlApiUrl, ...input }),
      requestEdgeDiagnosticGrant: (input) => requestPlatformDiagnosticGrant({
        controlApiUrl,
        ...input,
      }),
      runEdgeDiagnostic: (input) => runPlatformDiagnostic({ controlApiUrl, ...input }),
      cancelEdgeDiagnostic: (input) => cancelPlatformDiagnostic({ controlApiUrl, ...input }),
      beginIdentityStepUp: (input) => beginIdentityWebAuthnStepUp({
        controlApiUrl,
        ...input,
      }),
      createIdentityStepUpGrant: (input) => createIdentityStepUpGrant({
        controlApiUrl,
        ...input,
      }),
      getWebAuthnAssertion,
    },
  }).mount();
}
