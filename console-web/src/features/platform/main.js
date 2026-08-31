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
  createPlatformRollout,
  getFleetCompatibility,
  listPlatformGatewayReleases,
  listPlatformRollouts,
  mutatePlatformGatewayRelease,
  mutatePlatformRollout,
} from "../../lib/edgeUpgrades.js";
import {
  issuePlatformLicense,
  listPlatformLicenses,
  revokePlatformLicense,
} from "../../lib/edgeLicenses.js";
import { listPlatformBackupStatuses } from "../../lib/edgeBackups.js";
import {
  decidePlatformOfflineBundle,
  listPlatformOfflineBundles,
} from "../../lib/edgeOfflineBundles.js";
import {
  beginIdentityWebAuthnStepUp,
  createIdentityStepUpGrant,
  getWebAuthnAssertion,
} from "../../lib/identity.js";
import {
  createSupportIdempotencyKey,
  escalateSupportTicket,
  listSupportTickets,
  patchSupportTicket,
} from "../../lib/customerSupport.js";
import {
  approvePlatformDeveloperContract,
  buildPlatformDeveloperContract,
  createPlatformDeprecation,
  getPlatformDeveloperContracts,
  mutatePlatformDeprecation,
  withdrawPlatformSdk,
} from "../../lib/developerContracts.js";
import { listPlatformPrivacyQueue } from "../../lib/privacyCenter.js";
import { listPlatformComplianceControls } from "../../lib/complianceCenter.js";
import {
  applyPlatformSecurityException,
  getPlatformSupplyChainSecurity,
} from "../../lib/supplyChainSecurity.js";
import {
  applySecurityRemediation,
  getSecurityAssessment,
  startSecurityAssessment,
} from "../../lib/securityAssurance.js";
import {
  commandLaunchWave,
  createLaunchCandidate,
  decideLaunchCandidate,
  getLaunchGates,
  requestLaunchRollback,
  reviewLaunchCandidate,
  submitLaunchEvidence,
  verifyLaunchRollback,
} from "../../lib/launchGovernance.js";
import {
  approveChannelAttribution,
  commandChannelPartner,
  listChannelPartners,
  listChannelStatements,
  registerChannelLead,
  requestChannelAttribution,
} from "../../lib/channelManagement.js";
import { createPlatformPageController } from "./platformPageState.js";

async function getPlatformReleaseWorkspace({ controlApiUrl, signal } = {}) {
  const contracts = await getPlatformDeveloperContracts({ controlApiUrl, signal });
  const supplyChain = await getPlatformSupplyChainSecurity({
    controlApiUrl,
    releaseIds: contracts.sdk_releases
      .filter((item) => item.status === "published")
      .map((item) => item.sdk_release_id),
    signal,
  });
  return Object.freeze({ ...contracts, supply_chain: supplyChain });
}

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
      supportTickets: (input) => listSupportTickets({
        controlApiUrl, actorDomain: "platform", ...input,
      }),
      patchSupportTicket: (input) => patchSupportTicket({
        controlApiUrl, actorDomain: "platform", ...input,
      }),
      escalateSupportTicket: (input) => escalateSupportTicket({
        controlApiUrl, actorDomain: "platform", ...input,
      }),
      supportIdempotencyKey: (scope) => createSupportIdempotencyKey(scope),
      breakGlass: (input) => createBreakGlassSession({ controlApiUrl, ...input }),
      audit: (input) => listPlatformAudit({ controlApiUrl, ...input }),
      privacyQueue: (input) => listPlatformPrivacyQueue({ controlApiUrl, ...input }),
      complianceControls: (input) => listPlatformComplianceControls({
        controlApiUrl, ...input,
      }),
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
      gatewayReleases: (input) => listPlatformGatewayReleases({ controlApiUrl, ...input }),
      edgeLicenses: (input) => listPlatformLicenses({ controlApiUrl, ...input }),
      edgeBackups: (input) => listPlatformBackupStatuses({ controlApiUrl, ...input }),
      offlineBundles: (input) => listPlatformOfflineBundles({ controlApiUrl, ...input }),
      developerContracts: (input) => getPlatformReleaseWorkspace({ controlApiUrl, ...input }),
      buildDeveloperContract: (input) => buildPlatformDeveloperContract({
        controlApiUrl, ...input,
      }),
      approveDeveloperContract: (input) => approvePlatformDeveloperContract({
        controlApiUrl, ...input,
      }),
      createDeveloperDeprecation: (input) => createPlatformDeprecation({
        controlApiUrl, ...input,
      }),
      mutateDeveloperDeprecation: (input) => mutatePlatformDeprecation({
        controlApiUrl, ...input,
      }),
      withdrawDeveloperSdk: (input) => withdrawPlatformSdk({ controlApiUrl, ...input }),
      supplyChainException: (input) => applyPlatformSecurityException({
        controlApiUrl, ...input,
      }),
      securityAssessment: (input) => getSecurityAssessment({ controlApiUrl, ...input }),
      startSecurityAssessment: (input) => startSecurityAssessment({ controlApiUrl, ...input }),
      securityRemediation: (input) => applySecurityRemediation({ controlApiUrl, ...input }),
      launchGates: (input) => getLaunchGates({ controlApiUrl, ...input }),
      createLaunchCandidate: (input) => createLaunchCandidate({ controlApiUrl, ...input }),
      submitLaunchEvidence: (input) => submitLaunchEvidence({ controlApiUrl, ...input }),
      decideLaunchCandidate: (input) => decideLaunchCandidate({ controlApiUrl, ...input }),
      commandLaunchWave: (input) => commandLaunchWave({ controlApiUrl, ...input }),
      reviewLaunchCandidate: (input) => reviewLaunchCandidate({ controlApiUrl, ...input }),
      requestLaunchRollback: (input) => requestLaunchRollback({ controlApiUrl, ...input }),
      verifyLaunchRollback: (input) => verifyLaunchRollback({ controlApiUrl, ...input }),
      channelPartners: (input) => listChannelPartners({ controlApiUrl, ...input }),
      channelStatements: (input) => listChannelStatements({ controlApiUrl, ...input }),
      commandChannelPartner: (input) => commandChannelPartner({ controlApiUrl, ...input }),
      registerChannelLead: (input) => registerChannelLead({ controlApiUrl, ...input }),
      requestChannelAttribution: (input) => requestChannelAttribution({
        controlApiUrl,
        ...input,
      }),
      approveChannelAttribution: (input) => approveChannelAttribution({
        controlApiUrl,
        ...input,
      }),
      decideOfflineBundle: (input) => decidePlatformOfflineBundle({
        controlApiUrl,
        ...input,
      }),
      issueEdgeLicense: (input) => issuePlatformLicense({ controlApiUrl, ...input }),
      revokeEdgeLicense: (input) => revokePlatformLicense({ controlApiUrl, ...input }),
      fleetRollouts: (input) => listPlatformRollouts({ controlApiUrl, ...input }),
      mutateGatewayRelease: (input) => mutatePlatformGatewayRelease({
        controlApiUrl,
        ...input,
      }),
      createFleetRollout: (input) => createPlatformRollout({ controlApiUrl, ...input }),
      mutateFleetRollout: (input) => mutatePlatformRollout({ controlApiUrl, ...input }),
      fleetCompatibility: (input) => getFleetCompatibility({ controlApiUrl, ...input }),
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
