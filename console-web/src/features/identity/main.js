// 账号安全入口只装配分域 Cookie API；企业资料和一次性证明不得进入全局或浏览器存储。
import {
  beginIdentityTotpEnrollment,
  confirmIdentityPasswordReset,
  getIdentityMe,
  getIdentityPlatformAccess,
  listIdentitySessions,
  loginIdentity,
  logoutIdentity,
  registerIdentity,
  requestIdentityPasswordReset,
  resendIdentityVerification,
  revokeIdentitySession,
  rotateIdentityPassword,
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
  changeWorkspaceSubscription,
  createWorkspaceSubscription,
  getWorkspaceSubscription,
  listPublicSaasPlans,
} from "../../lib/saasBilling.js";
import {
  downloadWorkspaceBillingExport,
  getWorkspaceBillingSummary,
  saveWorkspaceBillingExport,
} from "../../lib/platformBilling.js";
import {
  getWorkspaceBusinessVerification,
  submitWorkspaceBusinessVerification,
  updateWorkspaceBusinessBillingProfile,
} from "../../lib/businessVerification.js";
import { createIdentityPageController } from "./identityPageState.js";

const root = globalThis.document?.querySelector("#identity-app");
if (root) {
  const hostname = globalThis.window.location.hostname;
  const controlApiUrl = new Set(["localhost", "127.0.0.1", "::1"]).has(hostname)
    ? `${globalThis.window.location.protocol}//${hostname}:8001`
    : globalThis.window.location.origin;
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
      verifyMfa: (input) => verifyIdentityTotpEnrollment({ controlApiUrl, ...input }),
      me: (input) => getIdentityMe({ controlApiUrl, ...input }),
      platformAccess: (input) => getIdentityPlatformAccess({ controlApiUrl, ...input }),
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
      saasCatalog: (input) => listPublicSaasPlans({ controlApiUrl, ...input }),
      subscription: (input) => getWorkspaceSubscription({ controlApiUrl, ...input }),
      createSubscription: (input) => createWorkspaceSubscription({ controlApiUrl, ...input }),
      changeSubscription: (input) => changeWorkspaceSubscription({ controlApiUrl, ...input }),
      billingSummary: (input) => getWorkspaceBillingSummary({ controlApiUrl, ...input }),
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
    },
  });
  controller.mount();
}
