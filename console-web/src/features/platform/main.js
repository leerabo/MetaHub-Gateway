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
    },
  }).mount();
}
