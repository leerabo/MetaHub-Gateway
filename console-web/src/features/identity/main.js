// 账号安全入口只装配分域 Cookie API；企业资料和一次性证明不得进入全局或浏览器存储。
import {
  beginIdentityAccountRecovery,
  beginIdentityTotpEnrollment,
  beginIdentityWebAuthnStepUp,
  completeIdentityAccountRecovery,
  completeIdentityWebAuthnRegistration,
  confirmIdentityPasswordReset,
  createIdentityMfaEnrollment,
  createIdentityStepUpGrant,
  createWebAuthnCredential,
  disableIdentityMfaEnrollment,
  getIdentityMe,
  getIdentityPlatformAccess,
  getIdentitySecurity,
  getWebAuthnAssertion,
  listIdentitySessions,
  loginIdentity,
  logoutIdentity,
  registerIdentity,
  requestIdentityPasswordReset,
  resendIdentityVerification,
  revokeIdentityTrustedDevice,
  revokeIdentitySession,
  rotateIdentityPassword,
  trustIdentityDevice,
  verifyIdentityTotpEnrollment,
  verifyIdentityRegistration,
} from "../../lib/identity.js";
import {
  createAccountWorkspace,
  createWorkspaceMembershipInvitation,
  getCurrentWorkspaceSession,
  getWorkspaceConsoleNavigation,
  getWorkspaceOnboarding,
  listAccountWorkspaces,
  listIdentityWorkspaces,
  listWorkspaceMemberships,
  recheckWorkspaceOnboarding,
  switchIdentityWorkspace,
  transferWorkspaceOwner,
  updateWorkspaceMembership,
  updateWorkspaceOnboardingStep,
} from "../../lib/workspaces.js";
import {
  downloadWorkspaceBillingExport,
  getWorkspaceBillingSummary,
  saveWorkspaceBillingExport,
} from "../../lib/platformBilling.js";
import {
  createWorkspaceInvoice,
  downloadWorkspaceInvoiceArtifact,
  getWorkspaceInvoice,
  listWorkspaceBillingStatements,
  saveWorkspaceInvoiceArtifact,
} from "../../lib/invoiceReconciliation.js";
import {
  getWorkspaceBusinessVerification,
  submitWorkspaceBusinessVerification,
  updateWorkspaceBusinessBillingProfile,
} from "../../lib/businessVerification.js";
import {
  getWorkspaceNotificationPreferences,
  listWorkspaceNotifications,
  markWorkspaceNotificationRead,
  updateWorkspaceNotificationPreferences,
} from "../../lib/notifications.js";
import {
  createEnterpriseDomain,
  createEnterpriseSsoConnection,
  getEnterpriseIdentitySummary,
  testEnterpriseSsoConnection,
  updateEnterpriseSsoPolicy,
  verifyEnterpriseDomain,
} from "../../lib/enterpriseIdentity.js";
import {
  createScimToken,
  getScimDirectorySummary,
  resolveScimConflict,
  rotateScimToken,
  upsertScimRoleMapping,
} from "../../lib/scimDirectory.js";
import {
  getWorkspaceConditionalAccess,
  simulateWorkspaceConditionalAccess,
  updateWorkspaceConditionalAccess,
} from "../../lib/conditionalAccess.js";
import {
  createWorkspaceLegalHold,
  getWorkspaceDataGovernance,
  releaseWorkspaceLegalHold,
  updateWorkspaceDataGovernance,
} from "../../lib/dataGovernance.js";
import {
  createWorkspaceOAuthClient,
  createWorkspaceServiceAccount,
  listWorkspaceServiceAccounts,
  revokeWorkspaceServiceAccount,
  rotateWorkspaceServiceAccountCredential,
} from "../../lib/machineIdentity.js";
import { completeRiskChallenge } from "../../lib/riskControls.js";
import {
  cancelDataSubjectRequest,
  createDataSubjectRequest,
  downloadPrivacyExport,
  getPrivacyPreferences,
  issuePrivacyExportReceipt,
  listDataSubjectRequests,
  savePrivacyExport,
  updatePrivacyPreferences,
} from "../../lib/privacyCenter.js";
import { listWorkspaceComplianceReports } from "../../lib/complianceCenter.js";
import { getPublicBranding } from "../../lib/brandDomains.js";
import {
  createAccountPreferenceIdempotencyKey,
  getAccountPreferences,
  updateAccountPreferences,
} from "../../lib/accountPreferences.js";
import { createIdentityPageController } from "./identityPageState.js";
import { setIdentityPublicBranding } from "./renderIdentityPage.js";

const root = globalThis.document?.querySelector("#identity-app");
if (root) {
  const hostname = globalThis.window.location.hostname;
  const controlApiUrl = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname)
    ? `${globalThis.window.location.protocol}//${hostname}:8001`
    : globalThis.window.location.origin;
  const pendingRiskChallenge = consumeRiskChallengeLocation(globalThis.window);
  const publicBrandingReady = getPublicBranding({ controlApiUrl })
    .then((response) => setIdentityPublicBranding(response.branding, globalThis.document))
    .catch(() => setIdentityPublicBranding(null, globalThis.document));
  const controller = createIdentityPageController({
    root,
    recordWorkspaceSwitch: ({ durationMs }) => {
      globalThis.console?.debug?.(`workspace_switch_interactive_ms=${durationMs.toFixed(1)}`);
    },
    api: {
      login: (input) => loginIdentity({ controlApiUrl, ...input }),
      register: (input) => registerIdentity({ controlApiUrl, ...input }),
      verify: (input) => verifyIdentityRegistration({ controlApiUrl, ...input }),
      resend: (input) => resendIdentityVerification({ controlApiUrl, ...input }),
      requestReset: (input) => requestIdentityPasswordReset({ controlApiUrl, ...input }),
      confirmReset: (input) => confirmIdentityPasswordReset({ controlApiUrl, ...input }),
      beginMfa: (input) => beginIdentityTotpEnrollment({ controlApiUrl, ...input }),
      createMfa: (input) => createIdentityMfaEnrollment({ controlApiUrl, ...input }),
      verifyMfa: (input) => verifyIdentityTotpEnrollment({ controlApiUrl, ...input }),
      completeWebAuthn: (input) => completeIdentityWebAuthnRegistration({
        controlApiUrl,
        ...input,
      }),
      beginRecovery: (input) => beginIdentityAccountRecovery({ controlApiUrl, ...input }),
      completeRecovery: (input) => completeIdentityAccountRecovery({ controlApiUrl, ...input }),
      createWebAuthnCredential: (input) => createWebAuthnCredential(input.publicKeyOptions),
      beginWebAuthnStepUp: (input) => beginIdentityWebAuthnStepUp({ controlApiUrl, ...input }),
      getWebAuthnAssertion: (input) => getWebAuthnAssertion(input.publicKeyOptions),
      disableMfa: (input) => disableIdentityMfaEnrollment({ controlApiUrl, ...input }),
      security: (input) => getIdentitySecurity({ controlApiUrl, ...input }),
      trustDevice: (input) => trustIdentityDevice({ controlApiUrl, ...input }),
      revokeTrustedDevice: (input) => revokeIdentityTrustedDevice({ controlApiUrl, ...input }),
      stepUp: (input) => createIdentityStepUpGrant({ controlApiUrl, ...input }),
      me: (input) => getIdentityMe({ controlApiUrl, ...input }),
      platformAccess: (input) => getIdentityPlatformAccess({ controlApiUrl, ...input }),
      preferences: (input) => getAccountPreferences({ controlApiUrl, ...input }),
      updatePreferences: (input) => updateAccountPreferences({ controlApiUrl, ...input }),
      createAccountPreferenceIdempotencyKey,
      sessions: (input) => listIdentitySessions({ controlApiUrl, ...input }),
      revoke: (input) => revokeIdentitySession({ controlApiUrl, ...input }),
      logout: (input) => logoutIdentity({ controlApiUrl, ...input }),
      rotate: (input) => rotateIdentityPassword({ controlApiUrl, ...input }),
      workspaces: (input) => listAccountWorkspaces({ controlApiUrl, ...input }),
      createWorkspace: (input) => createAccountWorkspace({ controlApiUrl, ...input }),
      identityWorkspaces: (input) => listIdentityWorkspaces({ controlApiUrl, ...input }),
      switchWorkspace: (input) => switchIdentityWorkspace({ controlApiUrl, ...input }),
      workspaceSession: (input) => getCurrentWorkspaceSession({ controlApiUrl, ...input }),
      memberships: (input) => listWorkspaceMemberships({ controlApiUrl, ...input }),
      updateMembership: (input) => updateWorkspaceMembership({ controlApiUrl, ...input }),
      inviteMembership: (input) =>
        createWorkspaceMembershipInvitation({ controlApiUrl, ...input }),
      transferOwner: (input) => transferWorkspaceOwner({ controlApiUrl, ...input }),
      workspaceNavigation: (input) => getWorkspaceConsoleNavigation({ controlApiUrl, ...input }),
      onboarding: (input) => getWorkspaceOnboarding({ controlApiUrl, ...input }),
      updateOnboardingStep: (input) =>
        updateWorkspaceOnboardingStep({ controlApiUrl, ...input }),
      recheckOnboarding: (input) => recheckWorkspaceOnboarding({ controlApiUrl, ...input }),
      billingSummary: (input) => getWorkspaceBillingSummary({ controlApiUrl, ...input }),
      billingStatements: (input) => listWorkspaceBillingStatements({ controlApiUrl, ...input }),
      createInvoice: (input) => createWorkspaceInvoice({ controlApiUrl, ...input }),
      invoice: (input) => getWorkspaceInvoice({ controlApiUrl, ...input }),
      invoiceDownload: async (input) => {
        const result = await downloadWorkspaceInvoiceArtifact({ controlApiUrl, ...input });
        saveWorkspaceInvoiceArtifact(result);
        return result;
      },
      businessVerification: (input) => getWorkspaceBusinessVerification({
        controlApiUrl,
        ...input,
      }),
      submitBusinessVerification: (input) => submitWorkspaceBusinessVerification({
        controlApiUrl,
        ...input,
      }),
      updateBusinessBilling: (input) => updateWorkspaceBusinessBillingProfile({
        controlApiUrl,
        ...input,
      }),
      billingExport: async (input) => {
        const result = await downloadWorkspaceBillingExport({ controlApiUrl, ...input });
        saveWorkspaceBillingExport(result);
        return result;
      },
      notifications: (input) => listWorkspaceNotifications({ controlApiUrl, ...input }),
      markNotificationRead: (input) => markWorkspaceNotificationRead({
        controlApiUrl,
        ...input,
      }),
      notificationPreferences: (input) => getWorkspaceNotificationPreferences({
        controlApiUrl,
        ...input,
      }),
      updateNotificationPreferences: (input) => updateWorkspaceNotificationPreferences({
        controlApiUrl,
        ...input,
      }),
      enterpriseIdentity: (input) => getEnterpriseIdentitySummary({ controlApiUrl, ...input }),
      createEnterpriseDomain: (input) => createEnterpriseDomain({ controlApiUrl, ...input }),
      verifyEnterpriseDomain: (input) => verifyEnterpriseDomain({ controlApiUrl, ...input }),
      createEnterpriseSsoConnection: (input) => createEnterpriseSsoConnection({
        controlApiUrl,
        ...input,
      }),
      testEnterpriseSsoConnection: (input) => testEnterpriseSsoConnection({
        controlApiUrl,
        ...input,
      }),
      updateEnterpriseSsoPolicy: (input) => updateEnterpriseSsoPolicy({
        controlApiUrl,
        ...input,
      }),
      scimDirectory: (input) => getScimDirectorySummary({ controlApiUrl, ...input }),
      createScimToken: (input) => createScimToken({ controlApiUrl, ...input }),
      rotateScimToken: (input) => rotateScimToken({ controlApiUrl, ...input }),
      upsertScimRoleMapping: (input) => upsertScimRoleMapping({ controlApiUrl, ...input }),
      resolveScimConflict: (input) => resolveScimConflict({ controlApiUrl, ...input }),
      conditionalAccess: (input) => getWorkspaceConditionalAccess({ controlApiUrl, ...input }),
      simulateConditionalAccess: (input) => simulateWorkspaceConditionalAccess({
        controlApiUrl,
        ...input,
      }),
      updateConditionalAccess: (input) => updateWorkspaceConditionalAccess({
        controlApiUrl,
        ...input,
      }),
      dataGovernance: (input) => getWorkspaceDataGovernance({ controlApiUrl, ...input }),
      updateDataGovernance: (input) => updateWorkspaceDataGovernance({
        controlApiUrl,
        ...input,
      }),
      createLegalHold: (input) => createWorkspaceLegalHold({ controlApiUrl, ...input }),
      releaseLegalHold: (input) => releaseWorkspaceLegalHold({ controlApiUrl, ...input }),
      privacyPreferences: (input) => getPrivacyPreferences({ controlApiUrl, ...input }),
      updatePrivacyPreferences: (input) => updatePrivacyPreferences({ controlApiUrl, ...input }),
      privacyRequests: (input) => listDataSubjectRequests({ controlApiUrl, ...input }),
      complianceReports: (input) => listWorkspaceComplianceReports({
        controlApiUrl, ...input,
      }),
      createPrivacyRequest: (input) => createDataSubjectRequest({ controlApiUrl, ...input }),
      cancelPrivacyRequest: (input) => cancelDataSubjectRequest({ controlApiUrl, ...input }),
      issuePrivacyExportReceipt: (input) => issuePrivacyExportReceipt({ controlApiUrl, ...input }),
      downloadPrivacyExport: async (input) => {
        const result = await downloadPrivacyExport({ controlApiUrl, ...input });
        savePrivacyExport(result);
        return result;
      },
      machineIdentity: (input) => listWorkspaceServiceAccounts({ controlApiUrl, ...input }),
      createServiceAccount: (input) => createWorkspaceServiceAccount({ controlApiUrl, ...input }),
      createOAuthClient: (input) => createWorkspaceOAuthClient({ controlApiUrl, ...input }),
      rotateServiceAccountCredential: (input) =>
        rotateWorkspaceServiceAccountCredential({ controlApiUrl, ...input }),
      revokeServiceAccount: (input) => revokeWorkspaceServiceAccount({
        controlApiUrl,
        ...input,
      }),
      completeRiskChallenge: (input) => completeRiskChallenge({ controlApiUrl, ...input }),
    },
  });
  void publicBrandingReady.then(() => controller.mount()).then(() => {
    if (pendingRiskChallenge) controller.showRiskChallenge(pendingRiskChallenge);
  });
  globalThis.window.addEventListener("pagehide", () => controller.destroy(), { once: true });
}

function consumeRiskChallengeLocation(windowObject) {
  if (!windowObject?.location) return null;
  const url = new URL(windowObject.location.href);
  const challengeId = url.searchParams.get("risk_challenge_id");
  const challengeToken = url.searchParams.get("risk_challenge_token");
  const revision = Number(url.searchParams.get("risk_challenge_revision"));
  const credential = url.searchParams.get("risk_challenge_credential") || "global";
  const valid = /^rch_[0-9a-f]{32}$/u.test(String(challengeId ?? ""))
    && typeof challengeToken === "string" && challengeToken.length >= 32
    && challengeToken.length <= 256 && Number.isSafeInteger(revision) && revision >= 1
    && new Set(["global", "workspace"]).has(credential);
  for (const key of [
    "risk_challenge_id",
    "risk_challenge_token",
    "risk_challenge_revision",
    "risk_challenge_credential",
  ]) url.searchParams.delete(key);
  windowObject.history?.replaceState?.(null, "", `${url.pathname}${url.search}${url.hash}`);
  return valid ? { challengeId, challengeToken, revision, credential } : null;
}
