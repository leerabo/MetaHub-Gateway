// 账号安全状态机统一管理 Workspace 授权、企业认证敏感值清理和过期请求取消。
import { IdentityApiError } from "../../lib/identity.js";
import { PlatformApiError } from "../../lib/platform.js";
import { buildInvitationAcceptLink } from "../../lib/invitations.js";
import { consumePaymentClientToken } from "../../lib/paymentCommerce.js";
import {
  renderIdentityLoginPage,
  renderIdentityRegistrationPage,
  renderIdentityResetConfirmPage,
  renderIdentityResetRequestPage,
  renderIdentityRiskChallengePage,
  renderIdentitySecurityPage,
  renderIdentityVerificationPage,
} from "./renderIdentityPage.js";

const CONSENT_VERSION = "privacy-2026-08";

export function createIdentityPageController({
  root,
  api,
  channelFactory = defaultChannelFactory,
  confirmAction = defaultConfirm,
  copyText = defaultCopyText,
  consoleUrl = globalThis.window?.location?.origin ?? "http://localhost",
  consumePaymentToken = () => {},
  invitationToken = null,
  monotonicNow = defaultMonotonicNow,
  newIdempotencyKey = defaultIdempotencyKey,
  recordWorkspaceSwitch = () => {},
} = {}) {
  if (!root || !api) {
    throw new TypeError("identity page requires root and api");
  }
  let generation = 0;
  let activeRequest = null;
  let account = null;
  let currentSession = null;
  let sessions = [];
  let workspaces = [];
  let workspaceChoices = [];
  let workspaceContext = null;
  let memberships = [];
  let navigationItems = [];
  let onboarding = null;
  let onboardingSelectedStepId = null;
  let onboardingBusy = false;
  let onboardingError = null;
  let onboardingIntent = null;
  let saasCatalog = null;
  let subscription = null;
  let subscriptionBusy = false;
  let subscriptionError = null;
  let subscriptionIntent = null;
  let billingSummary = null;
  let billingBusy = false;
  let billingError = null;
  let billingOrders = null;
  let paymentBusy = false;
  let paymentError = null;
  let paymentIntentIntent = null;
  let refundIntent = null;
  let billingStatements = null;
  let currentInvoice = null;
  let invoiceBusy = false;
  let invoiceError = null;
  let invoiceIntent = null;
  let businessVerification = null;
  let businessVerificationBusy = false;
  let businessVerificationError = null;
  let enterpriseIdentity = null;
  let enterpriseIdentityBusy = false;
  let enterpriseIdentityError = null;
  let enterpriseVerificationRecord = null;
  let enterpriseConnectionProtocol = "oidc";
  let enterpriseIntents = new Map();
  let scimDirectory = null;
  let scimDirectoryBusy = false;
  let scimDirectoryError = null;
  let scimBearerToken = null;
  let scimIntents = new Map();
  let notificationCenterOpen = false;
  let notificationInbox = null;
  let notificationPreferences = null;
  let notificationBusy = false;
  let notificationError = null;
  let notificationUnreadOnly = false;
  let notificationCursor = null;
  let notificationCursorHistory = [];
  let workspaceView = "summary";
  let workspaceDraft = null;
  let invitationResult = null;
  let invitationSecretLink = null;
  let invitationSecretMail = null;
  let pendingChallenge = null;
  let riskChallenge = null;
  let riskChallengeIntent = null;
  let mfaEnrollment = null;
  let mfaEnrollmentMethod = "totp";
  let identitySecurity = null;
  let identitySecurityBusy = false;
  let identitySecurityError = null;
  let recoveryCodes = null;
  let factorDisableIndex = null;
  let conditionalAccess = null;
  let conditionalAccessBusy = false;
  let conditionalAccessError = null;
  let platformAccess = false;
  let pendingWrite = Promise.resolve();
  let contextGeneration = 0;
  let contextChannel = null;
  let autoSwitchAttempted = false;
  let destroyed = false;

  const controller = {
    async mount() {
      destroyed = false;
      bindInteractions();
      bindContextChannel();
      return loadProtectedState();
    },
    async login({ email, password }) {
      const ticket = beginRequest();
      pendingChallenge = null;
      riskChallenge = null;
      riskChallengeIntent = null;
      renderIdentityLoginPage(root, { email });
      try {
        const result = await queueWrite(() => api.login({ email, password }));
        if (!isCurrent(ticket)) return false;
        account = result.account;
        currentSession = result.current_session;
        sessions = [currentSession];
        await loadSessions(ticket, "登录成功。", null);
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        scrubPasswords();
        renderIdentityLoginPage(root, { email, error: safePageError(error) });
        return false;
      }
    },
    showRiskChallenge({ challengeId, challengeToken, revision, credential = "global" } = {}) {
      if (!/^rch_[0-9a-f]{32}$/u.test(String(challengeId ?? ""))
        || typeof challengeToken !== "string" || challengeToken.length < 32
        || challengeToken.length > 256 || !Number.isSafeInteger(Number(revision))
        || Number(revision) < 1 || !new Set(["global", "workspace"]).has(credential)) {
        return false;
      }
      beginRequest();
      scrubSensitiveInputs();
      riskChallenge = {
        challengeId,
        challengeToken,
        revision: Number(revision),
        credential,
      };
      riskChallengeIntent = null;
      renderIdentityRiskChallengePage(root, { challengeId });
      return true;
    },
    async completeRiskChallenge({ answer } = {}) {
      if (!riskChallenge || typeof api.completeRiskChallenge !== "function"
        || typeof answer !== "string" || answer.length < 6 || answer.length > 128) {
        scrubSensitiveInputs();
        return false;
      }
      const challenge = riskChallenge;
      const fingerprint = JSON.stringify({
        challengeId: challenge.challengeId,
        revision: challenge.revision,
        answer,
      });
      const idempotencyKey = riskChallengeIntent?.fingerprint === fingerprint
        ? riskChallengeIntent.idempotencyKey
        : newIdempotencyKey();
      riskChallengeIntent = { fingerprint, idempotencyKey };
      const ticket = beginRequest();
      scrubSensitiveInputs();
      renderIdentityRiskChallengePage(root, { challengeId: challenge.challengeId, busy: true });
      try {
        await queueWrite(() => api.completeRiskChallenge({
          ...challenge,
          answer,
          idempotencyKey,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        riskChallenge = null;
        riskChallengeIntent = null;
        scrubSensitiveInputs();
        return loadProtectedState("附加验证已完成。", ticket);
      } catch (error) {
        scrubSensitiveInputs();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          riskChallengeIntent = null;
        }
        if (error instanceof PlatformApiError
          && new Set(["risk.challenge_expired", "risk.challenge_replayed",
            "risk.challenge_not_found"]).has(error.code)) {
          riskChallenge = null;
        }
        renderIdentityRiskChallengePage(root, {
          challengeId: challenge.challengeId,
          error: safePageError(error),
        });
        return false;
      }
    },
    async cancelRiskChallenge() {
      beginRequest();
      scrubSensitiveInputs();
      riskChallenge = null;
      riskChallengeIntent = null;
      return loadProtectedState();
    },
    showLogin() {
      clearToLogin();
      return true;
    },
    showRegistration() {
      beginRequest();
      scrubSensitiveInputs();
      scrubProtectedState();
      pendingChallenge = null;
      riskChallenge = null;
      riskChallengeIntent = null;
      renderIdentityRegistrationPage(root);
      return true;
    },
    showReset() {
      beginRequest();
      scrubSensitiveInputs();
      scrubProtectedState();
      pendingChallenge = null;
      riskChallenge = null;
      riskChallengeIntent = null;
      renderIdentityResetRequestPage(root);
      return true;
    },
    async registerAccount({ email, displayName, password, confirmPassword, consent }) {
      if (password !== confirmPassword || consent !== true) {
        scrubSensitiveInputs();
        renderIdentityRegistrationPage(root, {
          email,
          displayName,
          error: {
            message: password !== confirmPassword ? "两次输入的密码不一致。" : "请先同意隐私条款。",
            requestId: "-",
          },
        });
        return false;
      }
      const ticket = beginRequest();
      renderIdentityRegistrationPage(root, { email, displayName });
      try {
        const result = await queueWrite(() =>
          api.register({
            email,
            displayName,
            password,
            consentVersion: CONSENT_VERSION,
            invitationToken,
            signal: ticket.signal,
          }),
        );
        if (!isCurrent(ticket)) return false;
        scrubSensitiveInputs();
        pendingChallenge = { intentId: result.intent_id, purpose: result.purpose };
        renderIdentityVerificationPage(root, { feedback: "验证码已发送。" });
        return true;
      } catch (error) {
        scrubSensitiveInputs();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderIdentityRegistrationPage(root, {
          email,
          displayName,
          error: safePageError(error),
        });
        return false;
      }
    },
    async verifyRegistration({ code }) {
      if (pendingChallenge?.purpose !== "registration") {
        clearToLogin();
        return false;
      }
      const intentId = pendingChallenge.intentId;
      const ticket = beginRequest();
      try {
        const result = await queueWrite(() =>
          api.verify({ intentId, code, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        pendingChallenge = null;
        applyIdentityResult(result);
        await loadSessions(ticket, "邮箱已验证。", null);
        return true;
      } catch (error) {
        scrubSensitiveInputs();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderIdentityVerificationPage(root, { error: safePageError(error) });
        return false;
      }
    },
    async resendVerification() {
      if (pendingChallenge?.purpose !== "registration") return false;
      const ticket = beginRequest();
      try {
        const result = await queueWrite(() =>
          api.resend({ intentId: pendingChallenge.intentId, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        pendingChallenge = { intentId: result.intent_id, purpose: result.purpose };
        scrubSensitiveInputs();
        renderIdentityVerificationPage(root, { feedback: "验证码已重新发送。" });
        return true;
      } catch (error) {
        scrubSensitiveInputs();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderIdentityVerificationPage(root, { error: safePageError(error) });
        return false;
      }
    },
    async requestPasswordReset({ email }) {
      const ticket = beginRequest();
      renderIdentityResetRequestPage(root, { email });
      try {
        const result = await queueWrite(() =>
          api.requestReset({ email, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        pendingChallenge = { intentId: result.intent_id, purpose: result.purpose };
        renderIdentityResetConfirmPage(root, { feedback: "验证码已发送。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderIdentityResetRequestPage(root, { email, error: safePageError(error) });
        return false;
      }
    },
    async confirmPasswordReset({ code, newPassword, confirmPassword }) {
      if (newPassword !== confirmPassword) {
        scrubSensitiveInputs();
        renderIdentityResetConfirmPage(root, {
          error: { message: "两次输入的新密码不一致。", requestId: "-" },
        });
        return false;
      }
      if (pendingChallenge?.purpose !== "password_reset") {
        clearToLogin();
        return false;
      }
      const intentId = pendingChallenge.intentId;
      const ticket = beginRequest();
      try {
        const result = await queueWrite(() =>
          api.confirmReset({
            intentId,
            code,
            newPassword,
            signal: ticket.signal,
          }),
        );
        if (!isCurrent(ticket)) return false;
        pendingChallenge = null;
        scrubSensitiveInputs();
        applyIdentityResult(result);
        await loadSessions(ticket, "密码已重置，其他设备已退出。", null);
        return true;
      } catch (error) {
        scrubSensitiveInputs();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderIdentityResetConfirmPage(root, { error: safePageError(error) });
        return false;
      }
    },
    async logout() {
      clearToLogin();
      broadcastContextInvalidation();
      try {
        await queueWrite(() => api.logout({}));
      } catch (error) {
        if (isAbort(error)) return false;
      }
      return true;
    },
    async revokeSession(index) {
      const session = sessions[Number(index)];
      if (!session) return false;
      if (!session.is_current && !confirmAction("确认撤销这台设备的登录状态？")) {
        return false;
      }
      if (session.is_current) {
        clearToLogin();
        broadcastContextInvalidation();
        try {
          await queueWrite(() => api.revoke({ sessionId: session.session_id }));
        } catch (error) {
          if (isAbort(error)) return false;
        }
        return true;
      }

      const ticket = beginRequest();
      try {
        await queueWrite(() => api.revoke({ sessionId: session.session_id }));
        if (!isCurrent(ticket)) return false;
        return loadProtectedState("已撤销该设备。", ticket);
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        if (isExpired(error)) {
          clearToLogin();
          return false;
        }
        renderSecurity({ error: safePageError(error) });
        return false;
      }
    },
    async rotatePassword({ currentPassword, newPassword, confirmPassword }) {
      if (newPassword !== confirmPassword) {
        scrubPasswords();
        renderSecurity({ error: { message: "两次输入的新密码不一致。", requestId: "-" } });
        return false;
      }
      const ticket = beginRequest();
      try {
        const result = await queueWrite(() => api.rotate({ currentPassword, newPassword }));
        if (!isCurrent(ticket)) return false;
        scrubPasswords();
        account = result.account;
        currentSession = result.current_session;
        sessions = [currentSession];
        scrubWorkspaceScope({ clearChoices: true, clearRichWorkspaces: true });
        broadcastContextInvalidation();
        await loadSessions(ticket, "密码已更新，其他设备已退出。", null);
        return true;
      } catch (error) {
        scrubPasswords();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        if (isExpired(error)) {
          clearToLogin();
          return false;
        }
        renderSecurity({ error: safePageError(error) });
        return false;
      }
    },
    setMfaEnrollmentMethod(method) {
      if (!new Set(["totp", "webauthn", "recovery_codes"]).has(method)) return false;
      mfaEnrollmentMethod = method;
      mfaEnrollment = null;
      factorDisableIndex = null;
      scrubSensitiveInputs();
      renderSecurity();
      return true;
    },
    async beginMfaEnrollment({ currentPassword, method = mfaEnrollmentMethod }) {
      const unified = typeof api.createMfa === "function";
      if ((!unified && typeof api.beginMfa !== "function")
        || !new Set(["totp", "webauthn", "recovery_codes"]).has(method)
        || (!unified && method !== "totp")) {
        return false;
      }
      const ticket = beginRequest();
      mfaEnrollment = null;
      recoveryCodes = null;
      factorDisableIndex = null;
      identitySecurityBusy = true;
      identitySecurityError = null;
      scrubSensitiveInputs();
      renderSecurity({ feedback: method === "recovery_codes" ? "正在生成恢复码…" : "正在准备验证因子…" });
      let credential = null;
      try {
        const result = await queueWrite(() =>
          unified
            ? api.createMfa({ method, currentPassword, signal: ticket.signal })
            : api.beginMfa({ currentPassword, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        if (method === "webauthn") {
          if (typeof api.createWebAuthnCredential !== "function"
            || typeof api.completeWebAuthn !== "function") {
            throw new IdentityApiError({
              code: "identity.webauthn_unavailable",
              message: "当前浏览器无法使用安全密钥。",
            });
          }
          credential = await api.createWebAuthnCredential({
            publicKeyOptions: result.public_key_options,
          });
          if (!isCurrent(ticket)) return false;
          await queueWrite(() => api.completeWebAuthn({
            challengeId: result.challenge_id,
            credential,
            signal: ticket.signal,
          }));
          credential = null;
          if (!isCurrent(ticket)) return false;
          identitySecurityBusy = false;
          return loadProtectedState("安全密钥已启用。", ticket);
        }
        if (method === "recovery_codes") {
          recoveryCodes = Array.isArray(result.recovery_codes)
            ? [...result.recovery_codes]
            : null;
          identitySecurityBusy = false;
          await loadIdentitySecurity(ticket, "", null, { continueLoad: false });
          if (!isCurrent(ticket)) return false;
          renderSecurity({ feedback: "恢复码已重新生成。" });
          return Boolean(recoveryCodes);
        }
        mfaEnrollment = result;
        identitySecurityBusy = false;
        renderSecurity({ feedback: "动态验证密钥已创建。" });
        return true;
      } catch (error) {
        credential = null;
        if (!isCurrent(ticket) || isAbort(error)) return false;
        identitySecurityBusy = false;
        identitySecurityError = safePageError(error);
        renderSecurity({ error: safePageError(error) });
        return false;
      }
    },
    async verifyMfaEnrollment({ code }) {
      if (typeof api.verifyMfa !== "function" || !mfaEnrollment) return false;
      const factorId = mfaEnrollment.factor_id;
      const ticket = beginRequest();
      mfaEnrollment = null;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在验证动态验证码…" });
      try {
        await queueWrite(() => api.verifyMfa({ factorId, code, signal: ticket.signal }));
        if (!isCurrent(ticket)) return false;
        account = { ...account, mfa_configured: true };
        return loadProtectedState("多重验证已启用。", ticket);
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderSecurity({ error: safePageError(error) });
        return false;
      }
    },
    closeRecoveryCodes() {
      recoveryCodes = null;
      scrubSensitiveInputs();
      renderSecurity();
      return true;
    },
    async beginAccountRecovery({ currentPassword, confirmed }) {
      const hasActiveFactor = identitySecurity?.factors?.some(
        (factor) => factor.status === "active",
      );
      const hasRecoveryCodes = identitySecurity?.recovery_code_set?.status === "active"
        && identitySecurity.recovery_code_set.remaining_count > 0;
      if (typeof api.beginRecovery !== "function" || !hasActiveFactor || !hasRecoveryCodes) {
        scrubSensitiveInputs();
        return false;
      }
      if (confirmed !== true) {
        scrubSensitiveInputs();
        renderSecurity({
          error: { message: "开始受控恢复前必须确认会话与验证因子将被撤销。", requestId: "-" },
        });
        return false;
      }
      const ticket = beginRequest();
      identitySecurityBusy = true;
      identitySecurityError = null;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在创建受控恢复挑战…" });
      try {
        await queueWrite(() => api.beginRecovery({
          currentPassword,
          signal: ticket.signal,
        }));
        currentPassword = "";
        if (!isCurrent(ticket)) return false;
        identitySecurityBusy = false;
        await loadIdentitySecurity(ticket, "", null, { continueLoad: false });
        if (!isCurrent(ticket)) return false;
        renderSecurity({ feedback: "恢复挑战已创建。" });
        return true;
      } catch (error) {
        currentPassword = "";
        if (!isCurrent(ticket) || isAbort(error)) return false;
        identitySecurityBusy = false;
        identitySecurityError = safePageError(error);
        renderSecurity({ error: identitySecurityError });
        return false;
      }
    },
    async completeAccountRecovery(input = {}) {
      const challenge = identitySecurity?.recovery_challenge;
      if (typeof api.completeRecovery !== "function" || !challenge) {
        scrubSensitiveInputs();
        input.notificationCode = "";
        input.recoveryCode = "";
        return false;
      }
      if (input.confirmed !== true) {
        scrubSensitiveInputs();
        input.notificationCode = "";
        input.recoveryCode = "";
        renderSecurity({
          error: { message: "完成恢复前必须确认撤销现有身份安全状态。", requestId: "-" },
        });
        return false;
      }
      const ticket = beginRequest();
      identitySecurityBusy = true;
      identitySecurityError = null;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在验证受控恢复证明…" });
      try {
        await queueWrite(() => api.completeRecovery({
          challengeId: challenge.challenge_id,
          notificationCode: input.notificationCode,
          recoveryCode: input.recoveryCode,
          signal: ticket.signal,
        }));
        input.notificationCode = "";
        input.recoveryCode = "";
        if (!isCurrent(ticket)) return false;
        identitySecurityBusy = false;
        mfaEnrollment = null;
        recoveryCodes = null;
        factorDisableIndex = null;
        scrubWorkspaceScope();
        broadcastContextInvalidation();
        return loadProtectedState("账号已进入受控恢复状态，请重新登记验证因子。", ticket);
      } catch (error) {
        input.notificationCode = "";
        input.recoveryCode = "";
        if (!isCurrent(ticket) || isAbort(error)) return false;
        identitySecurityBusy = false;
        identitySecurityError = safePageError(error);
        renderSecurity({ error: identitySecurityError });
        return false;
      }
    },
    selectFactorDisable(index) {
      const factor = identitySecurity?.factors?.[Number(index)];
      if (!factor || factor.status !== "active") return false;
      factorDisableIndex = Number(index);
      scrubSensitiveInputs();
      renderSecurity();
      return true;
    },
    cancelFactorDisable() {
      factorDisableIndex = null;
      scrubSensitiveInputs();
      renderSecurity();
      return true;
    },
    async disableMfaFactor({ currentPassword }) {
      const factor = identitySecurity?.factors?.[Number(factorDisableIndex)];
      if (!factor || factor.status !== "active" || typeof api.disableMfa !== "function") {
        scrubSensitiveInputs();
        return false;
      }
      const ticket = beginRequest();
      const factorId = factor.factor_id;
      factorDisableIndex = null;
      identitySecurityBusy = true;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在停用验证因子…" });
      try {
        await queueWrite(() => api.disableMfa({
          factorId,
          currentPassword,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        identitySecurityBusy = false;
        broadcastContextInvalidation();
        return loadProtectedState("验证因子已停用，现有会话保证级别已重新校验。", ticket);
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        identitySecurityBusy = false;
        identitySecurityError = safePageError(error);
        renderSecurity({ error: identitySecurityError });
        return false;
      }
    },
    async trustCurrentDevice({ label, totpCode }) {
      if (typeof api.trustDevice !== "function") return false;
      const ticket = beginRequest();
      identitySecurityBusy = true;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在验证当前设备…" });
      try {
        await queueWrite(() => api.trustDevice({ label, totpCode, signal: ticket.signal }));
        if (!isCurrent(ticket)) return false;
        identitySecurityBusy = false;
        return loadProtectedState("当前设备已设为受信设备。", ticket);
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        identitySecurityBusy = false;
        identitySecurityError = safePageError(error);
        renderSecurity({ error: identitySecurityError });
        return false;
      }
    },
    async revokeTrustedDevice(index) {
      const device = identitySecurity?.trusted_devices?.[Number(index)];
      if (!device || device.status !== "active" || typeof api.revokeTrustedDevice !== "function") {
        return false;
      }
      if (!confirmAction("确认撤销这台设备的信任状态？")) return false;
      const ticket = beginRequest();
      identitySecurityBusy = true;
      renderSecurity({ feedback: "正在撤销设备信任…" });
      try {
        await queueWrite(() => api.revokeTrustedDevice({
          trustedDeviceId: device.trusted_device_id,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        identitySecurityBusy = false;
        broadcastContextInvalidation();
        return loadProtectedState("设备信任已撤销。", ticket);
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        identitySecurityBusy = false;
        identitySecurityError = safePageError(error);
        renderSecurity({ error: identitySecurityError });
        return false;
      }
    },
    showWorkspaceCreate() {
      clearInvitationSecret();
      invitationResult = null;
      workspaceView = "create";
      workspaceDraft = {
        kind: "organization",
        name: "",
        slug: "",
        region: "cn-north-1",
        serviceMode: "internal_only",
        idempotencyKey: newIdempotencyKey(),
        requestFingerprint: null,
      };
      renderSecurity();
      return true;
    },
    setWorkspaceKind(kind) {
      if (workspaceView !== "create" || !new Set(["personal", "organization"]).has(kind)) {
        return false;
      }
      workspaceDraft = { ...workspaceDraft, kind };
      renderSecurity();
      return true;
    },
    cancelWorkspaceCreate() {
      workspaceView = "summary";
      workspaceDraft = null;
      renderSecurity();
      return true;
    },
    async createWorkspace({ kind, name, slug, region, serviceMode }) {
      if (workspaceView !== "create" || !workspaceDraft) return false;
      const input = {
        kind,
        name: kind === "organization" ? name : undefined,
        slug: kind === "organization" ? slug : undefined,
        region,
        serviceMode,
      };
      const fingerprint = JSON.stringify(input);
      const idempotencyKey =
        workspaceDraft.requestFingerprint && workspaceDraft.requestFingerprint !== fingerprint
          ? newIdempotencyKey()
          : workspaceDraft.idempotencyKey;
      workspaceDraft = {
        ...workspaceDraft,
        ...input,
        idempotencyKey,
        requestFingerprint: fingerprint,
      };
      const ticket = beginRequest();
      renderSecurity();
      try {
        const result = await queueWrite(() =>
          api.createWorkspace({
            ...input,
            idempotencyKey,
            signal: ticket.signal,
          }),
        );
        if (!isCurrent(ticket)) return false;
        workspaces = [
          ...workspaces.filter(
            (workspace) => workspace.workspace_id !== result.workspace.workspace_id,
          ),
          result.workspace,
        ];
        workspaceView = "summary";
        workspaceDraft = null;
        autoSwitchAttempted = false;
        return loadWorkspaceAccess(ticket, "空间已创建。", null);
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderSecurity({ error: safePageError(error) });
        return false;
      }
    },
    async switchWorkspace(index) {
      const choice = workspaceChoices[Number(index)];
      if (!choice || choice.role === "service_account" || typeof api.switchWorkspace !== "function") {
        return false;
      }
      const startedAt = monotonicNow();
      const ticket = beginRequest();
      scrubWorkspaceScope();
      renderSecurity({ feedback: "正在进入空间…" });
      try {
        const sessionResult = await queueWrite(() =>
          api.switchWorkspace({ workspaceId: choice.workspace_id, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        autoSwitchAttempted = true;
        broadcastContextInvalidation();
        const interactive = await loadWorkspaceAuthorization(
          ticket,
          sessionResult,
          "空间上下文已更新。",
          null,
        );
        if (interactive) {
          recordWorkspaceSwitch({ durationMs: Math.max(0, monotonicNow() - startedAt) });
        }
        return interactive;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        return handleWorkspaceActionError(error, "无法进入该空间。", ticket);
      }
    },
    setEnterpriseConnectionProtocol(protocol) {
      if (!new Set(["oidc", "saml"]).has(protocol) || !workspaceEnterpriseManageAllowed()) {
        return false;
      }
      enterpriseConnectionProtocol = protocol;
      renderSecurity();
      return true;
    },
    async refreshEnterpriseIdentity() {
      if (!workspaceEnterpriseReadAllowed() || typeof api.enterpriseIdentity !== "function") {
        return false;
      }
      const ticket = beginRequest();
      enterpriseIdentityBusy = true;
      enterpriseIdentityError = null;
      renderSecurity();
      return loadEnterpriseIdentity(ticket, "企业身份状态已刷新。");
    },
    async createEnterpriseDomain({ domain }) {
      if (!workspaceEnterpriseManageAllowed()
        || typeof api.createEnterpriseDomain !== "function") return false;
      const input = { domain: String(domain ?? "").trim().toLowerCase() };
      return mutateEnterpriseIdentity(
        "domain.create",
        input,
        (ticket, idempotencyKey) => api.createEnterpriseDomain({
          workspaceId: workspaceContext.workspace_id,
          domain: input.domain,
          idempotencyKey,
          signal: ticket.signal,
        }),
        (result) => {
          enterpriseVerificationRecord = {
            domain: result.domain.domain,
            value: result.verification_record,
          };
        },
        "域名验证记录已创建。",
      );
    },
    async verifyEnterpriseDomain(index) {
      const domain = enterpriseIdentity?.domains?.[Number(index)];
      if (!workspaceEnterpriseManageAllowed() || !domain || domain.status !== "pending"
        || typeof api.verifyEnterpriseDomain !== "function") return false;
      return mutateEnterpriseIdentity(
        "domain.verify",
        { domainId: domain.verified_domain_id, revision: domain.revision },
        (ticket, idempotencyKey) => api.verifyEnterpriseDomain({
          workspaceId: workspaceContext.workspace_id,
          domainId: domain.verified_domain_id,
          revision: domain.revision,
          idempotencyKey,
          signal: ticket.signal,
        }),
        () => {
          enterpriseVerificationRecord = null;
        },
        "域名验证已完成。",
      );
    },
    async createEnterpriseSsoConnection(input) {
      if (!workspaceEnterpriseManageAllowed()
        || typeof api.createEnterpriseSsoConnection !== "function") return false;
      const domain = enterpriseIdentity?.domains?.[Number(input.domainIndex)];
      if (!domain || domain.status !== "verified") return false;
      let jwks = input.jwks;
      if (enterpriseConnectionProtocol === "oidc" && typeof input.jwksText === "string") {
        try {
          jwks = JSON.parse(input.jwksText);
        } catch {
          scrubSensitiveInputs();
          renderSecurity({
            error: { message: "JWKS 必须是有效的 JSON 对象。", requestId: "-" },
          });
          return false;
        }
      }
      const request = {
        verifiedDomainId: domain.verified_domain_id,
        connectionCode: String(input.connectionCode ?? "").trim(),
        displayName: String(input.displayName ?? "").trim(),
        protocol: enterpriseConnectionProtocol,
        issuer: String(input.issuer ?? "").trim(),
        authorizationEndpoint: input.authorizationEndpoint,
        tokenEndpoint: input.tokenEndpoint,
        clientId: input.clientId,
        clientSecret: input.clientSecret,
        jwks,
        ssoUrl: input.ssoUrl,
        certificatePem: input.certificatePem,
      };
      try {
        return await mutateEnterpriseIdentity(
          "connection.create",
          request,
          (ticket, idempotencyKey) => api.createEnterpriseSsoConnection({
            workspaceId: workspaceContext.workspace_id,
            input: request,
            idempotencyKey,
            signal: ticket.signal,
          }),
          null,
          "SSO 连接已创建，可执行协议测试。",
        );
      } finally {
        request.clientSecret = "";
        request.certificatePem = "";
        request.jwks = null;
        input.clientSecret = "";
        input.certificatePem = "";
        input.jwksText = "";
        input.jwks = null;
      }
    },
    async testEnterpriseSsoConnection(index) {
      const connection = enterpriseIdentity?.connections?.[Number(index)];
      if (!workspaceEnterpriseManageAllowed() || !connection
        || typeof api.testEnterpriseSsoConnection !== "function") return false;
      return mutateEnterpriseIdentity(
        "connection.test",
        { connectionId: connection.sso_connection_id, revision: connection.revision },
        (ticket, idempotencyKey) => api.testEnterpriseSsoConnection({
          workspaceId: workspaceContext.workspace_id,
          connectionId: connection.sso_connection_id,
          revision: connection.revision,
          idempotencyKey,
          signal: ticket.signal,
        }),
        null,
        "SSO 协议测试已通过。",
      );
    },
    async updateEnterpriseSsoPolicy({ mode, connectionIndex, candidateIndex }) {
      if (!workspaceEnterpriseManageAllowed()
        || typeof api.updateEnterpriseSsoPolicy !== "function") return false;
      const normalizedMode = new Set(["optional", "enforced"]).has(mode) ? mode : null;
      const connection = String(connectionIndex ?? "") === ""
        ? null
        : enterpriseIdentity?.connections?.[Number(connectionIndex)] ?? null;
      const candidate = String(candidateIndex ?? "") === ""
        ? null
        : enterpriseIdentity?.break_glass_candidates?.[Number(candidateIndex)] ?? null;
      if (!normalizedMode || (normalizedMode === "enforced" && (
        connection?.status !== "active"
        || !candidate?.password_configured
        || !candidate?.mfa_configured
      ))) return false;
      const request = {
        mode: normalizedMode,
        connectionId: connection?.sso_connection_id ?? null,
        breakGlassAccountId: normalizedMode === "enforced" ? candidate.account_id : null,
        revision: enterpriseIdentity?.policy?.revision ?? 0,
      };
      return mutateEnterpriseIdentity(
        "policy.update",
        request,
        (ticket, idempotencyKey) => api.updateEnterpriseSsoPolicy({
          workspaceId: workspaceContext.workspace_id,
          ...request,
          idempotencyKey,
          signal: ticket.signal,
        }),
        null,
        normalizedMode === "enforced" ? "强制 SSO 已启用。" : "SSO 策略已设为可选。",
      );
    },
    async refreshScimDirectory() {
      if (!workspaceScimReadAllowed() || typeof api.scimDirectory !== "function") {
        return false;
      }
      const ticket = beginRequest();
      scimDirectoryBusy = true;
      scimDirectoryError = null;
      renderSecurity();
      return loadScimDirectory(ticket, "SCIM 目录状态已刷新。");
    },
    async createScimToken({ expiresInDays }) {
      if (!workspaceScimManageAllowed() || typeof api.createScimToken !== "function") {
        return false;
      }
      scimBearerToken = null;
      return mutateScimDirectory(
        "token.create",
        { expiresInDays: Number(expiresInDays) },
        (ticket, idempotencyKey) => api.createScimToken({
          workspaceId: workspaceContext.workspace_id,
          expiresInDays: Number(expiresInDays),
          idempotencyKey,
          signal: ticket.signal,
        }),
        (result) => {
          scimBearerToken = result.bearer_token;
        },
        "SCIM token 已创建。",
      );
    },
    async rotateScimToken(index, expiresInDays = 90) {
      const token = scimDirectory?.tokens?.[Number(index)];
      if (!workspaceScimManageAllowed() || !token || token.status !== "active"
        || typeof api.rotateScimToken !== "function") return false;
      scimBearerToken = null;
      return mutateScimDirectory(
        "token.rotate",
        {
          tokenId: token.scim_token_id,
          revision: token.revision,
          expiresInDays: Number(expiresInDays),
        },
        (ticket, idempotencyKey) => api.rotateScimToken({
          workspaceId: workspaceContext.workspace_id,
          tokenId: token.scim_token_id,
          revision: token.revision,
          expiresInDays: Number(expiresInDays),
          idempotencyKey,
          signal: ticket.signal,
        }),
        (result) => {
          scimBearerToken = result.bearer_token;
        },
        "SCIM token 已轮换。",
      );
    },
    async upsertScimRoleMapping({ groupIndex, roleCode }) {
      const group = scimDirectory?.groups?.[Number(groupIndex)];
      if (!workspaceScimManageAllowed() || !group
        || typeof api.upsertScimRoleMapping !== "function") return false;
      const mapping = scimDirectory.role_mappings.find(
        (item) => item.scim_group_link_id === group.scim_group_link_id,
      );
      return mutateScimDirectory(
        "mapping.upsert",
        {
          groupId: group.scim_group_link_id,
          roleCode,
          revision: mapping?.revision ?? 0,
        },
        (ticket, idempotencyKey) => api.upsertScimRoleMapping({
          workspaceId: workspaceContext.workspace_id,
          groupId: group.scim_group_link_id,
          roleCode,
          revision: mapping?.revision ?? 0,
          idempotencyKey,
          signal: ticket.signal,
        }),
        null,
        "SCIM Group 角色映射已保存。",
      );
    },
    async resolveScimConflict(index, resolutionCode) {
      const conflict = scimDirectory?.conflicts?.[Number(index)];
      if (!workspaceScimManageAllowed() || conflict?.status !== "open"
        || typeof api.resolveScimConflict !== "function") return false;
      return mutateScimDirectory(
        "conflict.resolve",
        {
          conflictId: conflict.scim_conflict_id,
          revision: conflict.revision,
          resolutionCode,
        },
        (ticket, idempotencyKey) => api.resolveScimConflict({
          workspaceId: workspaceContext.workspace_id,
          conflictId: conflict.scim_conflict_id,
          revision: conflict.revision,
          resolutionCode,
          idempotencyKey,
          signal: ticket.signal,
        }),
        null,
        "SCIM 冲突已处理。",
      );
    },
    async copyScimBearerToken() {
      if (typeof scimBearerToken !== "string" || !scimBearerToken) return false;
      try {
        await copyText(scimBearerToken);
        return true;
      } catch {
        return false;
      }
    },
    dismissScimBearerToken() {
      if (scimBearerToken === null) return false;
      scimBearerToken = null;
      renderSecurity();
      return true;
    },
    async refreshConditionalAccess() {
      if (!workspaceConditionalReadAllowed() || typeof api.conditionalAccess !== "function") {
        return false;
      }
      const ticket = beginRequest();
      conditionalAccessBusy = true;
      conditionalAccessError = null;
      renderSecurity();
      return loadConditionalAccess(ticket, "条件访问策略已刷新。");
    },
    async simulateConditionalAccess() {
      if (!workspaceConditionalManageAllowed()
        || conditionalAccessBusy
        || typeof api.simulateConditionalAccess !== "function") {
        return false;
      }
      const ticket = beginRequest();
      const ownerWorkspaceId = workspaceContext.workspace_id;
      conditionalAccessBusy = true;
      conditionalAccessError = null;
      renderSecurity({ feedback: "正在评估当前策略影响…" });
      try {
        const result = await queueWrite(() => api.simulateConditionalAccess({
          workspaceId: ownerWorkspaceId,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket) || workspaceContext?.workspace_id !== ownerWorkspaceId) return false;
        conditionalAccess = {
          policy: result.policy,
          latest_simulation: result.simulation,
        };
        conditionalAccessBusy = false;
        conditionalAccessError = null;
        renderSecurity({
          feedback: result.simulation.owner_ready
            ? "策略影响评估已完成。"
            : "策略影响评估完成，但 Owner 恢复路径尚未就绪。",
        });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        conditionalAccessBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        conditionalAccessError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async saveConditionalAccess(input = {}) {
      if (!workspaceConditionalManageAllowed()
        || conditionalAccessBusy
        || typeof api.stepUp !== "function"
        || typeof api.switchWorkspace !== "function"
        || typeof api.updateConditionalAccess !== "function") {
        scrubSensitiveInputs();
        return false;
      }
      const mode = String(input.mode ?? "");
      if (!new Set(["audit", "enforced"]).has(mode)
        || (mode === "enforced" && input.confirmed !== true)) {
        scrubSensitiveInputs();
        renderSecurity({
          error: { message: "启用强制模式前必须确认影响与恢复条件。", requestId: "-" },
        });
        return false;
      }
      const ownerWorkspaceId = workspaceContext.workspace_id;
      const method = String(input.method ?? "totp");
      const policy = {
        expectedRevision: conditionalAccess?.policy?.revision ?? 0,
        mode,
        requireMfa: input.requireMfa === true,
        requireStepUpForHighRisk: input.requireStepUpForHighRisk === true,
        allowTrustedDevices: input.allowTrustedDevices === true,
        regionRule: input.regionRule,
        networkRule: input.networkRule,
        allowedCidrs: parsePolicyLines(input.allowedCidrs),
        exceptionMembershipIds: Array.isArray(input.exceptionMembershipIds)
          ? [...input.exceptionMembershipIds]
          : [],
      };
      const ticket = beginRequest();
      conditionalAccessBusy = true;
      conditionalAccessError = null;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在完成一次性验证并保存策略…" });
      let credential = null;
      let grant = "";
      try {
        let challengeId;
        if (method === "webauthn") {
          if (typeof api.beginWebAuthnStepUp !== "function"
            || typeof api.getWebAuthnAssertion !== "function") {
            throw new IdentityApiError({
              code: "identity.webauthn_unavailable",
              message: "当前浏览器无法使用安全密钥。",
            });
          }
          const challenge = await api.beginWebAuthnStepUp({ signal: ticket.signal });
          if (!isCurrent(ticket)) return false;
          challengeId = challenge.challenge_id;
          credential = await api.getWebAuthnAssertion({
            publicKeyOptions: challenge.public_key_options,
          });
          if (!isCurrent(ticket)) return false;
        }
        const issued = await queueWrite(() => api.stepUp({
          method,
          workspaceId: ownerWorkspaceId,
          actionCode: "conditional_access.manage",
          resourceType: "conditional_access_policy",
          resourceId: ownerWorkspaceId,
          code: input.code,
          challengeId,
          credential,
          signal: ticket.signal,
        }));
        credential = null;
        if (!isCurrent(ticket) || workspaceContext?.workspace_id !== ownerWorkspaceId) return false;
        grant = issued.grant;
        await queueWrite(() => api.updateConditionalAccess({
          workspaceId: ownerWorkspaceId,
          policy,
          stepUpGrant: grant,
          signal: ticket.signal,
        }));
        grant = "";
        input.code = "";
        if (!isCurrent(ticket) || workspaceContext?.workspace_id !== ownerWorkspaceId) return false;
        broadcastContextInvalidation();
        scrubWorkspaceAuthorization();
        renderSecurity({ feedback: "策略已保存，正在重新建立空间上下文…" });
        const sessionResult = await queueWrite(() => api.switchWorkspace({
          workspaceId: ownerWorkspaceId,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        autoSwitchAttempted = true;
        return loadWorkspaceAuthorization(
          ticket,
          sessionResult,
          mode === "enforced" ? "条件访问强制模式已启用。" : "条件访问审计策略已保存。",
          null,
        );
      } catch (error) {
        credential = null;
        grant = "";
        input.code = "";
        if (!isCurrent(ticket) || isAbort(error)) return false;
        conditionalAccessBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        conditionalAccessError = safePageError(error);
        renderSecurity();
        return false;
      } finally {
        credential = null;
        grant = "";
        input.code = "";
        scrubSensitiveInputs();
      }
    },
    async refreshSubscription() {
      if (!workspaceFinanceAllowed() || typeof api.saasCatalog !== "function"
        || typeof api.subscription !== "function") return false;
      const ticket = beginRequest();
      subscriptionBusy = true;
      subscriptionError = null;
      renderSecurity();
      return loadSaasBilling(ticket, "套餐与权益已刷新。");
    },
    async createSubscription({ planVersionId, startTrial }) {
      if (!workspaceFinanceAllowed() || subscription?.subscription
        || typeof api.createSubscription !== "function") return false;
      return mutateSubscription({
        kind: "create",
        planVersionId,
        startTrial: startTrial === true,
      });
    },
    async changeSubscription({ planVersionId }) {
      const revision = Number(subscription?.subscription?.state_revision);
      if (!workspaceFinanceAllowed() || !Number.isSafeInteger(revision) || revision < 1
        || typeof api.changeSubscription !== "function") return false;
      return mutateSubscription({ kind: "change", planVersionId, revision });
    },
    async cancelSubscription() {
      const current = subscription?.subscription;
      const revision = Number(current?.state_revision);
      if (!workspaceFinanceAllowed() || !current
        || !Number.isSafeInteger(revision) || revision < 1
        || typeof api.cancelSubscription !== "function"
        || !confirmAction("确认取消当前订阅？待支付订单也将被取消。")) return false;
      return mutateSubscription({
        kind: "cancel",
        subscriptionId: current.subscription_id,
        revision,
      });
    },
    async refreshPaymentCommerce() {
      if (!workspaceFinanceAllowed() || typeof api.billingOrders !== "function") return false;
      const ticket = beginRequest();
      paymentBusy = true;
      paymentError = null;
      renderSecurity();
      return loadPaymentCommerce(ticket, "订单与支付状态已刷新。");
    },
    async createPaymentIntent({ orderId, provider }) {
      const payable = billingOrders?.orders?.some(
        (order) => order.order_id === orderId && order.status === "pending_payment",
      );
      if (!workspaceFinanceAllowed() || paymentBusy || !payable
        || typeof api.createPaymentIntent !== "function") return false;
      const fingerprint = JSON.stringify({ orderId, provider });
      const idempotencyKey = paymentIntentIntent?.fingerprint === fingerprint
        ? paymentIntentIntent.idempotencyKey
        : newIdempotencyKey();
      paymentIntentIntent = { fingerprint, idempotencyKey };
      const ticket = beginRequest();
      paymentBusy = true;
      paymentError = null;
      renderSecurity();
      let result = null;
      try {
        result = await queueWrite(() => api.createPaymentIntent({
          orderId,
          provider,
          idempotencyKey,
          signal: ticket.signal,
        }));
        await consumePaymentClientToken(result, consumePaymentToken);
        result = null;
        if (!isCurrent(ticket)) return false;
        paymentIntentIntent = null;
        return loadPaymentCommerce(ticket, "支付意图已创建。");
      } catch (error) {
        if (result && typeof result === "object") result.client_token = "";
        result = null;
        if (!isCurrent(ticket) || isAbort(error)) return false;
        paymentBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          paymentIntentIntent = null;
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        if (!(error instanceof IdentityApiError) || ![0, 503].includes(error.status)) {
          paymentIntentIntent = null;
        }
        paymentError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async requestRefund({ orderId, amountMinor, reasonCode, confirmed }) {
      const order = billingOrders?.orders?.find((item) => item.order_id === orderId);
      const refundable = Number(order?.captured_amount_minor) - Number(order?.refunded_amount_minor);
      const amount = Number(amountMinor);
      if (!workspaceFinanceAllowed() || paymentBusy || !order || confirmed !== true
        || !Number.isSafeInteger(amount) || amount < 1 || amount > refundable
        || typeof api.requestRefund !== "function") return false;
      const input = { orderId, amountMinor: amount, reasonCode, confirmed: true };
      const fingerprint = JSON.stringify(input);
      const idempotencyKey = refundIntent?.fingerprint === fingerprint
        ? refundIntent.idempotencyKey
        : newIdempotencyKey();
      refundIntent = { fingerprint, idempotencyKey };
      const ticket = beginRequest();
      paymentBusy = true;
      paymentError = null;
      renderSecurity();
      try {
        await queueWrite(() => api.requestRefund({
          ...input,
          idempotencyKey,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        refundIntent = null;
        return loadPaymentCommerce(ticket, "退款申请已提交审核。");
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        paymentBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          refundIntent = null;
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        if (!(error instanceof IdentityApiError) || ![0, 503].includes(error.status)) {
          refundIntent = null;
        }
        paymentError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async refreshInvoices() {
      if (!workspaceFinanceAllowed() || typeof api.billingStatements !== "function") return false;
      const ticket = beginRequest();
      invoiceBusy = true;
      invoiceError = null;
      renderSecurity();
      return loadWorkspaceInvoices(ticket, "发票与结算单已刷新。");
    },
    async createInvoice({ billingStatementId, invoiceType }) {
      const statement = billingStatements?.statements?.find(
        (item) => item.billing_statement_id === billingStatementId,
      );
      if (!workspaceFinanceAllowed() || invoiceBusy || !statement
        || typeof api.createInvoice !== "function") return false;
      const fingerprint = JSON.stringify({ billingStatementId, invoiceType });
      const idempotencyKey = invoiceIntent?.fingerprint === fingerprint
        ? invoiceIntent.idempotencyKey
        : newIdempotencyKey();
      invoiceIntent = { fingerprint, idempotencyKey };
      const ticket = beginRequest();
      invoiceBusy = true;
      invoiceError = null;
      renderSecurity();
      try {
        currentInvoice = await queueWrite(() => api.createInvoice({
          billingStatementId,
          invoiceType,
          idempotencyKey,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        invoiceIntent = null;
        invoiceBusy = false;
        renderSecurity({ feedback: "发票申请已提交，开票状态可在此刷新。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        invoiceBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          invoiceIntent = null;
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        if (!(error instanceof IdentityApiError) || ![0, 503].includes(error.status)) {
          invoiceIntent = null;
        }
        invoiceError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async refreshInvoice() {
      const invoiceId = currentInvoice?.invoice?.invoice_request_id;
      if (!workspaceFinanceAllowed() || !invoiceId || typeof api.invoice !== "function") return false;
      const ticket = beginRequest();
      invoiceBusy = true;
      invoiceError = null;
      renderSecurity();
      return loadWorkspaceInvoices(ticket, "发票状态已刷新。");
    },
    async downloadInvoiceArtifact(artifactId) {
      const invoice = currentInvoice?.invoice;
      const artifact = invoice?.artifacts?.find(
        (item) => item.invoice_artifact_id === artifactId,
      );
      if (!workspaceFinanceAllowed() || invoiceBusy || !invoice || !artifact
        || typeof api.invoiceDownload !== "function") return false;
      const ticket = beginRequest();
      invoiceBusy = true;
      invoiceError = null;
      renderSecurity();
      try {
        await api.invoiceDownload({
          invoiceId: invoice.invoice_request_id,
          artifact,
          signal: ticket.signal,
        });
        if (!isCurrent(ticket)) return false;
        invoiceBusy = false;
        renderSecurity({ feedback: "发票产物已安全下载。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        invoiceBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        invoiceError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async refreshBilling() {
      if (!workspaceFinanceAllowed() || typeof api.billingSummary !== "function") return false;
      const ticket = beginRequest();
      billingBusy = true;
      billingError = null;
      renderSecurity();
      return loadWorkspaceBilling(ticket, null, "账单摘要已刷新。");
    },
    async selectBillingCycle(cycleId) {
      if (!workspaceFinanceAllowed() || typeof api.billingSummary !== "function") return false;
      const ticket = beginRequest();
      billingBusy = true;
      billingError = null;
      renderSecurity();
      return loadWorkspaceBilling(ticket, cycleId, "账单周期已更新。");
    },
    async downloadBillingExport() {
      const cycleId = billingSummary?.cycle?.billing_cycle_id;
      if (!workspaceFinanceAllowed() || !cycleId || typeof api.billingExport !== "function") {
        return false;
      }
      const ticket = beginRequest();
      billingBusy = true;
      billingError = null;
      renderSecurity();
      try {
        await api.billingExport({ cycleId, signal: ticket.signal });
        if (!isCurrent(ticket)) return false;
        billingBusy = false;
        renderSecurity({ feedback: "账单对账 CSV 已下载。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        billingBusy = false;
        billingError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async refreshBusinessVerification() {
      if (!workspaceBusinessAllowed() || typeof api.businessVerification !== "function") {
        return false;
      }
      const ticket = beginRequest();
      businessVerificationBusy = true;
      businessVerificationError = null;
      renderSecurity();
      return loadBusinessVerification(ticket, "企业认证已刷新。");
    },
    async submitBusinessVerification(input) {
      if (!workspaceBusinessAllowed() || businessVerificationBusy
        || typeof api.submitBusinessVerification !== "function") return false;
      const ticket = beginRequest();
      businessVerificationBusy = true;
      businessVerificationError = null;
      renderSecurity();
      try {
        const result = await queueWrite(() => api.submitBusinessVerification({
          workspaceId: workspaceContext.workspace_id,
          submission: input,
          idempotencyKey: newIdempotencyKey(),
          signal: ticket.signal,
        }));
        scrubBusinessInput(input);
        if (!isCurrent(ticket)) return false;
        businessVerification = result;
        businessVerificationBusy = false;
        renderSecurity({ feedback: "企业认证已提交。" });
        return true;
      } catch (error) {
        scrubBusinessInput(input);
        if (!isCurrent(ticket) || isAbort(error)) return false;
        businessVerificationBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        businessVerificationError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async updateBusinessBilling(input) {
      if (!workspaceBusinessAllowed() || businessVerificationBusy
        || typeof api.updateBusinessBilling !== "function") return false;
      const ticket = beginRequest();
      businessVerificationBusy = true;
      businessVerificationError = null;
      renderSecurity();
      try {
        const result = await queueWrite(() => api.updateBusinessBilling({
          workspaceId: workspaceContext.workspace_id,
          billingProfile: input.billingProfile,
          revision: input.revision,
          idempotencyKey: newIdempotencyKey(),
          signal: ticket.signal,
        }));
        scrubBusinessInput(input);
        if (!isCurrent(ticket)) return false;
        businessVerification = {
          ...(businessVerification ?? {}),
          workspace_id: workspaceContext.workspace_id,
          billing_profile: result.billing_profile,
          request_id: result.request_id,
          trace_id: result.trace_id,
        };
        businessVerificationBusy = false;
        renderSecurity({ feedback: "开票资料已更新。" });
        return true;
      } catch (error) {
        scrubBusinessInput(input);
        if (!isCurrent(ticket) || isAbort(error)) return false;
        businessVerificationBusy = false;
        if (isWorkspaceAuthorizationDrift(error)) {
          return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
        }
        businessVerificationError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    selectOnboardingStep(index) {
      const step = onboarding?.steps?.[Number(index)];
      if (!step) return false;
      onboardingSelectedStepId = step.id;
      renderSecurity();
      focusOnboardingPanel();
      return true;
    },
    async confirmOnboardingStep() {
      return mutateOnboardingStep("confirm", "user_confirmed");
    },
    async deferOnboardingStep() {
      return mutateOnboardingStep("defer", "do_later");
    },
    async reopenOnboardingStep() {
      return mutateOnboardingStep("reopen", "configuration_changed");
    },
    async recheckOnboarding() {
      if (!onboarding || onboardingBusy || typeof api.recheckOnboarding !== "function") {
        return false;
      }
      const ticket = beginRequest();
      const fingerprint = JSON.stringify({
        workspace: workspaceContext?.workspace_id,
        action: "recheck",
        revision: onboarding.revision,
      });
      const idempotencyKey = onboardingIntent?.fingerprint === fingerprint
        ? onboardingIntent.idempotencyKey
        : newIdempotencyKey();
      onboardingIntent = { fingerprint, idempotencyKey };
      onboardingBusy = true;
      onboardingError = null;
      renderSecurity();
      try {
        const result = await queueWrite(() =>
          api.recheckOnboarding({
            revision: onboarding.revision,
            idempotencyKey,
            signal: ticket.signal,
          }),
        );
        if (!isCurrent(ticket)) return false;
        onboardingIntent = null;
        onboardingBusy = false;
        applyOnboardingProjection(result);
        renderSecurity({ feedback: "初始化状态已重新检查。" });
        focusOnboardingPanel();
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        onboardingBusy = false;
        return handleOnboardingActionError(error, ticket);
      }
    },
    async refreshOnboarding() {
      if (!workspaceContext || typeof api.onboarding !== "function") return false;
      const ticket = beginRequest();
      onboardingBusy = true;
      onboardingError = null;
      renderSecurity();
      return loadOnboarding(ticket, "初始化状态已刷新。");
    },
    async updateMembership(index, { role, status }) {
      const target = memberships[Number(index)];
      if (!target?.can_manage || typeof api.updateMembership !== "function") return false;
      const changes = {};
      if (role && role !== target.role) changes.role = role;
      if (status && status !== target.status) changes.status = status;
      if (Object.keys(changes).length === 0) {
        renderSecurity({ error: { message: "成员信息没有变化。", requestId: "-" } });
        return false;
      }
      const ticket = beginRequest();
      clearInvitationSecret();
      try {
        await queueWrite(() =>
          api.updateMembership({
            membershipId: target.membership_id,
            revision: target.revision,
            ...changes,
            signal: ticket.signal,
          }),
        );
        if (!isCurrent(ticket)) return false;
        broadcastContextInvalidation();
        return invalidateWorkspaceContext({
          broadcast: false,
          feedback: "成员权限已更新。",
          existingTicket: ticket,
        });
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        return handleWorkspaceActionError(error, "成员更新失败。", ticket);
      }
    },
    async inviteMembership({ email, role, expiresInHours }) {
      if (!workspaceContext || !new Set(["owner", "admin"]).has(workspaceContext.role)) {
        return false;
      }
      if (typeof api.inviteMembership !== "function") return false;
      const ticket = beginRequest();
      clearInvitationSecret();
      invitationResult = null;
      renderSecurity();
      try {
        const result = await queueWrite(() =>
          api.inviteMembership({ email, role, expiresInHours, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        const normalizedEmail = String(email ?? "").trim().toLowerCase();
        invitationSecretLink = buildInvitationAcceptLink(consoleUrl, result.accept_token);
        invitationSecretMail = [
          "MetaHub 团队邀请",
          `受邀邮箱：${normalizedEmail}`,
          `角色：${result.invitation.role}`,
          `接受邀请：${invitationSecretLink}`,
        ].join("\n");
        invitationResult = {
          email: normalizedEmail,
          role: result.invitation.role,
          expiresAt: result.invitation.expires_at,
        };
        renderSecurity({ feedback: "邀请已生成，请通过受控渠道发送。" });
        return true;
      } catch (error) {
        clearInvitationSecret();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        return handleWorkspaceActionError(error, "邀请创建失败。", ticket);
      }
    },
    async copyInvitationSecret(kind = "link") {
      const value = kind === "mail" ? invitationSecretMail : invitationSecretLink;
      if (!value) return false;
      try {
        await copyText(value);
        if (invitationSecretLink === null) return false;
        renderSecurity({ feedback: kind === "mail" ? "邀请文案已复制。" : "邀请链接已复制。" });
        return true;
      } catch {
        if (invitationSecretLink === null) return false;
        renderSecurity({
          error: { message: "浏览器未允许访问剪贴板，请检查站点权限。", requestId: "-" },
        });
        return false;
      }
    },
    async transferOwner({ targetIndex, currentPassword, confirmed = false }) {
      const target = memberships[Number(targetIndex)];
      if (
        workspaceContext?.role !== "owner" ||
        !target ||
        target.status !== "active" ||
        new Set(["owner", "service_account"]).has(target.role) ||
        typeof api.transferOwner !== "function"
      ) {
        scrubPasswords();
        return false;
      }
      if (confirmed !== true) {
        scrubPasswords();
        return false;
      }
      const ticket = beginRequest();
      clearInvitationSecret();
      try {
        await queueWrite(() =>
          api.transferOwner({
            targetMembershipId: target.membership_id,
            currentPassword,
            signal: ticket.signal,
          }),
        );
        scrubPasswords();
        if (!isCurrent(ticket)) return false;
        broadcastContextInvalidation();
        return invalidateWorkspaceContext({
          broadcast: false,
          feedback: "所有权已转移，请重新进入空间。",
          existingTicket: ticket,
        });
      } catch (error) {
        scrubPasswords();
        if (!isCurrent(ticket) || isAbort(error)) return false;
        return handleWorkspaceActionError(error, "所有权转移失败。", ticket);
      }
    },
    async openNotifications() {
      if (!workspaceContext || typeof api.notifications !== "function") return false;
      notificationCenterOpen = true;
      renderSecurity();
      if (notificationInbox && notificationPreferences) return true;
      return controller.refreshNotifications();
    },
    closeNotifications() {
      notificationCenterOpen = false;
      notificationError = null;
      renderSecurity();
      return true;
    },
    async refreshNotifications() {
      if (!workspaceContext || typeof api.notifications !== "function") return false;
      const ticket = beginRequest();
      notificationBusy = true;
      notificationError = null;
      renderSecurity();
      return loadNotificationCenter(ticket);
    },
    async setNotificationFilter(unreadOnly) {
      if (typeof unreadOnly !== "boolean" || notificationUnreadOnly === unreadOnly) return false;
      notificationUnreadOnly = unreadOnly;
      notificationCursor = null;
      notificationCursorHistory = [];
      return controller.refreshNotifications();
    },
    async nextNotificationPage() {
      const cursor = notificationInbox?.next_cursor;
      if (!cursor) return false;
      notificationCursorHistory.push(notificationCursor);
      notificationCursor = cursor;
      return controller.refreshNotifications();
    },
    async previousNotificationPage() {
      if (notificationCursorHistory.length === 0) return false;
      notificationCursor = notificationCursorHistory.pop() ?? null;
      return controller.refreshNotifications();
    },
    async markNotificationRead(index) {
      const notification = notificationInbox?.notifications?.[Number(index)];
      if (!notification || notification.read || typeof api.markNotificationRead !== "function") {
        return false;
      }
      const ticket = beginRequest();
      notificationBusy = true;
      notificationError = null;
      renderSecurity();
      try {
        const result = await queueWrite(() => api.markNotificationRead({
          notificationId: notification.notification_id,
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        const notifications = notificationInbox.notifications
          .map((item) => item.notification_id === result.notification.notification_id
            ? result.notification
            : item)
          .filter((item) => !notificationUnreadOnly || !item.read);
        notificationInbox = {
          ...notificationInbox,
          notifications,
          unread_count: Math.max(0, notificationInbox.unread_count - 1),
        };
        notificationBusy = false;
        renderSecurity({ feedback: "通知已标记为已读。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        notificationBusy = false;
        notificationError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async updateNotificationPreference(category, channel, enabled) {
      const current = notificationPreferences?.preferences?.find(
        (item) => item.category === category && item.channel === channel,
      );
      if (
        !current
        || current.mandatory
        || !current.available
        || current.enabled === enabled
        || typeof enabled !== "boolean"
        || typeof api.updateNotificationPreferences !== "function"
      ) return false;
      const ticket = beginRequest();
      notificationBusy = true;
      notificationError = null;
      renderSecurity();
      try {
        notificationPreferences = await queueWrite(() => api.updateNotificationPreferences({
          revision: notificationPreferences.revision,
          changes: [{ category, channel, enabled }],
          signal: ticket.signal,
        }));
        if (!isCurrent(ticket)) return false;
        notificationBusy = false;
        renderSecurity({ feedback: "通知偏好已更新。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        notificationBusy = false;
        notificationError = safePageError(error);
        renderSecurity();
        return false;
      }
    },
    async refreshWorkspaceContext() {
      return invalidateWorkspaceContext({ broadcast: false, feedback: "空间上下文已刷新。" });
    },
    destroy() {
      destroyed = true;
      generation += 1;
      activeRequest?.abort();
      activeRequest = null;
      contextChannel?.close?.();
      contextChannel = null;
      scrubSensitiveInputs();
      pendingChallenge = null;
      riskChallenge = null;
      riskChallengeIntent = null;
      scrubProtectedState();
      root.innerHTML = "";
    },
  };

  return controller;

  async function mutateOnboardingStep(action, reasonCode) {
    const step = selectedOnboardingStep();
    if (
      !step ||
      onboardingBusy ||
      typeof api.updateOnboardingStep !== "function" ||
      !canMutateOnboardingStep(step, action)
    ) {
      return false;
    }
    const ticket = beginRequest();
    const fingerprint = JSON.stringify({
      workspace: workspaceContext?.workspace_id,
      step: step.id,
      action,
      reasonCode,
      revision: onboarding.revision,
    });
    const idempotencyKey = onboardingIntent?.fingerprint === fingerprint
      ? onboardingIntent.idempotencyKey
      : newIdempotencyKey();
    onboardingIntent = { fingerprint, idempotencyKey };
    onboardingBusy = true;
    onboardingError = null;
    renderSecurity();
    try {
      const result = await queueWrite(() =>
        api.updateOnboardingStep({
          stepId: step.id,
          action,
          reasonCode,
          revision: onboarding.revision,
          idempotencyKey,
          signal: ticket.signal,
        }),
      );
      if (!isCurrent(ticket)) return false;
      onboardingIntent = null;
      onboardingBusy = false;
      applyOnboardingProjection(result);
      renderSecurity({
        feedback: action === "confirm"
          ? "步骤已确认。"
          : action === "defer"
            ? "已标记稍后处理。"
            : "步骤已重新打开。",
      });
      focusOnboardingPanel();
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      onboardingBusy = false;
      return handleOnboardingActionError(error, ticket);
    }
  }

  function canMutateOnboardingStep(step, action) {
    if (step.id === "workspace_profile") {
      const allowed = onboarding?.workspace_kind === "personal"
        ? workspaceContext?.role === "owner"
        : new Set(["owner", "admin"]).has(workspaceContext?.role);
      if (!allowed) return false;
    } else if (!new Set(["owner", "admin", "developer"]).has(workspaceContext?.role)) {
      return false;
    }
    if (action === "confirm") {
      return new Set(["workspace_profile", "environment"]).has(step.id)
        && new Set(["current", "deferred", "needs_attention"]).has(step.status);
    }
    if (action === "defer") return new Set(["current", "needs_attention"]).has(step.status);
    return action === "reopen"
      && new Set(["workspace_profile", "environment"]).has(step.id)
      && step.status === "complete";
  }

  async function handleOnboardingActionError(error, ticket) {
    if (isWorkspaceAuthorizationDrift(error)) {
      onboardingIntent = null;
      broadcastContextInvalidation();
      await invalidateWorkspaceContext({
        broadcast: false,
        feedback: "空间权限已变化，正在重新校验。",
        existingTicket: ticket,
      });
      return false;
    }
    if (isOnboardingRevisionDrift(error)) {
      onboardingIntent = null;
      await loadOnboarding(ticket, "初始化状态已变化，请重新操作。");
      return false;
    }
    onboardingError = safePageError(error);
    renderSecurity();
    focusOnboardingPanel();
    return false;
  }

  async function loadOnboarding(ticket, feedback = "") {
    try {
      const result = await api.onboarding({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      onboardingBusy = false;
      onboardingIntent = null;
      onboardingError = null;
      applyOnboardingProjection(result);
      renderSecurity({ feedback });
      focusOnboardingPanel();
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      onboardingBusy = false;
      if (isWorkspaceAuthorizationDrift(error)) {
        onboardingIntent = null;
        broadcastContextInvalidation();
        return invalidateWorkspaceContext({
          broadcast: false,
          feedback: "空间权限已变化，正在重新校验。",
          existingTicket: ticket,
        });
      }
      onboarding = null;
      onboardingError = safePageError(error);
      renderSecurity();
      return false;
    }
  }

  function applyOnboardingProjection(result) {
    onboarding = result;
    const selectedStillExists = onboarding.steps.some(
      (step) => step.id === onboardingSelectedStepId,
    );
    if (!selectedStillExists) {
      onboardingSelectedStepId = (
        onboarding.steps.find((step) => step.status === "current")
        ?? onboarding.steps.find((step) => step.status !== "complete")
        ?? onboarding.steps[0]
      )?.id ?? null;
    }
  }

  function selectedOnboardingStep() {
    return onboarding?.steps?.find((step) => step.id === onboardingSelectedStepId) ?? null;
  }

  function focusOnboardingPanel() {
    root.querySelector?.("[data-onboarding-step-panel]")?.focus?.({ preventScroll: true });
  }

  async function loadProtectedState(feedback = "", existingTicket = null) {
    const ticket = existingTicket ?? beginRequest();
    try {
      const me = await api.me({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      account = me.account;
      currentSession = me.current_session;
      sessions = [currentSession];
      await loadSessions(ticket, feedback, null);
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
      } else if (account) {
        renderSecurity({ error: safePageError(error) });
      } else {
        scrubProtectedState();
        renderIdentityLoginPage(root, { error: safePageError(error) });
      }
      return false;
    }
  }

  async function loadSessions(ticket, feedback, previousError) {
    try {
      const list = await api.sessions({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      sessions = normalizeSessions(list.sessions, currentSession);
      return loadIdentitySecurity(ticket, feedback, previousError);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      sessions = currentSession ? [currentSession] : [];
      return loadIdentitySecurity(ticket, feedback, safePageError(error));
    }
  }

  async function loadIdentitySecurity(
    ticket,
    feedback,
    previousError,
    { continueLoad = true } = {},
  ) {
    identitySecurityBusy = false;
    if (typeof api.security !== "function") {
      identitySecurity = null;
      identitySecurityError = null;
      return continueLoad ? loadPlatformAccess(ticket, feedback, previousError) : true;
    }
    identitySecurityBusy = true;
    try {
      const result = await api.security({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      identitySecurity = result;
      identitySecurityBusy = false;
      identitySecurityError = null;
      sessions = normalizeSessions(result.sessions, currentSession);
      account = {
        ...account,
        mfa_configured: result.factors.some((factor) => factor.status === "active"),
      };
      return continueLoad ? loadPlatformAccess(ticket, feedback, previousError) : true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      identitySecurity = null;
      identitySecurityBusy = false;
      identitySecurityError = safePageError(error);
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      return continueLoad
        ? loadPlatformAccess(ticket, feedback, previousError ?? identitySecurityError)
        : false;
    }
  }

  async function loadPlatformAccess(ticket, feedback, previousError) {
    platformAccess = false;
    if (typeof api.platformAccess !== "function") {
      return loadWorkspaces(ticket, feedback, previousError);
    }
    try {
      const projection = await api.platformAccess({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      platformAccess = projection.available === true;
      return loadWorkspaces(ticket, feedback, previousError);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      return loadWorkspaces(ticket, feedback, previousError ?? safePageError(error));
    }
  }

  async function loadWorkspaces(ticket, feedback, previousError, retryAuthorizationDrift = true) {
    if (typeof api.workspaces !== "function") {
      workspaces = [];
      return loadWorkspaceAccess(ticket, feedback, previousError, retryAuthorizationDrift);
    }
    try {
      const list = await api.workspaces({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      workspaces = Array.isArray(list.workspaces) ? list.workspaces : [];
      return loadWorkspaceAccess(ticket, feedback, previousError, retryAuthorizationDrift);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      workspaces = [];
      return loadWorkspaceAccess(
        ticket,
        feedback,
        safePageError(error),
        retryAuthorizationDrift,
      );
    }
  }

  function workspaceFinanceAllowed() {
    return Boolean(
      workspaceContext
      && new Set(["owner", "finance_admin"]).has(workspaceContext.role),
    );
  }

  /** 企业认证只适用于组织 Workspace，且前端可见性仍不替代后端动态权限校验。 */
  function workspaceBusinessAllowed() {
    return workspaceFinanceAllowed() && workspaceContext?.kind === "organization";
  }

  function workspaceEnterpriseReadAllowed() {
    return Boolean(
      workspaceContext
      && new Set(["owner", "security_admin", "auditor"]).has(workspaceContext.role),
    );
  }

  function workspaceEnterpriseManageAllowed() {
    return Boolean(
      workspaceContext
      && new Set(["owner", "security_admin"]).has(workspaceContext.role),
    );
  }

  function workspaceScimReadAllowed() {
    return Boolean(
      workspaceContext
      && new Set(["owner", "admin", "security_admin", "auditor", "viewer"])
        .has(workspaceContext.role),
    );
  }

  function workspaceScimManageAllowed() {
    return Boolean(
      workspaceContext
      && new Set(["owner", "admin", "security_admin"]).has(workspaceContext.role),
    );
  }

  function workspaceConditionalReadAllowed() {
    return Boolean(
      workspaceContext
      && Array.isArray(workspaceContext.permission_codes)
      && workspaceContext.permission_codes.includes("conditional_access.read"),
    );
  }

  function workspaceConditionalManageAllowed() {
    return Boolean(
      workspaceContext
      && Array.isArray(workspaceContext.permission_codes)
      && workspaceContext.permission_codes.includes("conditional_access.manage"),
    );
  }

  async function loadEnterpriseIdentity(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceEnterpriseReadAllowed() || typeof api.enterpriseIdentity !== "function") {
      enterpriseIdentity = null;
      enterpriseIdentityBusy = false;
      enterpriseIdentityError = null;
      enterpriseVerificationRecord = null;
      enterpriseIntents = new Map();
      if (render) renderSecurity({ feedback });
      return false;
    }
    enterpriseIdentityBusy = true;
    try {
      const result = await api.enterpriseIdentity({
        workspaceId: workspaceContext.workspace_id,
        signal: ticket.signal,
      });
      if (!isCurrent(ticket)) return false;
      enterpriseIdentity = result;
      enterpriseIdentityBusy = false;
      enterpriseIdentityError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      enterpriseIdentity = null;
      enterpriseIdentityBusy = false;
      enterpriseIdentityError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function mutateEnterpriseIdentity(action, input, operation, applyResult, feedback) {
    if (enterpriseIdentityBusy) return false;
    const ownerWorkspaceId = workspaceContext?.workspace_id;
    enterpriseIdentityBusy = true;
    enterpriseIdentityError = null;
    renderSecurity();
    let fingerprint;
    try {
      fingerprint = await secureEnterpriseFingerprint(input);
    } catch {
      enterpriseIdentityBusy = false;
      enterpriseIdentityError = {
        message: "无法安全准备企业身份请求，请刷新页面后重试。",
        requestId: "-",
      };
      renderSecurity();
      return false;
    }
    if (!enterpriseIdentityBusy || workspaceContext?.workspace_id !== ownerWorkspaceId) return false;
    const previous = enterpriseIntents.get(action);
    const idempotencyKey = previous?.fingerprint === fingerprint
      ? previous.idempotencyKey
      : newIdempotencyKey();
    enterpriseIntents.set(action, { fingerprint, idempotencyKey });
    const ticket = beginRequest();
    try {
      const result = await queueWrite(() => operation(ticket, idempotencyKey));
      if (!isCurrent(ticket)) return false;
      applyResult?.(result);
      enterpriseIntents.delete(action);
      enterpriseIdentityBusy = false;
      return loadEnterpriseIdentity(ticket, feedback);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      enterpriseIdentityBusy = false;
      if (isWorkspaceAuthorizationDrift(error)) {
        enterpriseIntents.delete(action);
        return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
      }
      if (!(error instanceof IdentityApiError) || ![0, 503].includes(error.status)) {
        enterpriseIntents.delete(action);
      }
      enterpriseIdentityError = safePageError(error);
      renderSecurity();
      return false;
    }
  }

  async function loadScimDirectory(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceScimReadAllowed() || typeof api.scimDirectory !== "function") {
      scimDirectory = null;
      scimDirectoryBusy = false;
      scimDirectoryError = null;
      scimBearerToken = null;
      scimIntents = new Map();
      if (render) renderSecurity({ feedback });
      return false;
    }
    scimDirectoryBusy = true;
    try {
      const result = await api.scimDirectory({
        workspaceId: workspaceContext.workspace_id,
        signal: ticket.signal,
      });
      if (!isCurrent(ticket)) return false;
      scimDirectory = result;
      scimDirectoryBusy = false;
      scimDirectoryError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      scimDirectory = null;
      scimDirectoryBusy = false;
      scimDirectoryError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function loadConditionalAccess(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceConditionalReadAllowed() || typeof api.conditionalAccess !== "function") {
      conditionalAccess = null;
      conditionalAccessBusy = false;
      conditionalAccessError = null;
      if (render) renderSecurity({ feedback });
      return false;
    }
    conditionalAccessBusy = true;
    try {
      const result = await api.conditionalAccess({
        workspaceId: workspaceContext.workspace_id,
        signal: ticket.signal,
      });
      if (!isCurrent(ticket)) return false;
      conditionalAccess = result;
      conditionalAccessBusy = false;
      conditionalAccessError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      conditionalAccess = null;
      conditionalAccessBusy = false;
      conditionalAccessError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function mutateScimDirectory(action, input, operation, applyResult, feedback) {
    if (scimDirectoryBusy) return false;
    const ownerWorkspaceId = workspaceContext?.workspace_id;
    scimDirectoryBusy = true;
    scimDirectoryError = null;
    renderSecurity();
    let fingerprint;
    try {
      fingerprint = await secureEnterpriseFingerprint(input);
    } catch {
      scimDirectoryBusy = false;
      scimDirectoryError = {
        message: "无法安全准备 SCIM 目录请求，请刷新页面后重试。",
        requestId: "-",
      };
      renderSecurity();
      return false;
    }
    if (!scimDirectoryBusy || workspaceContext?.workspace_id !== ownerWorkspaceId) return false;
    const previous = scimIntents.get(action);
    const idempotencyKey = previous?.fingerprint === fingerprint
      ? previous.idempotencyKey
      : newIdempotencyKey();
    scimIntents.set(action, { fingerprint, idempotencyKey });
    const ticket = beginRequest();
    try {
      const result = await queueWrite(() => operation(ticket, idempotencyKey));
      if (!isCurrent(ticket)) return false;
      applyResult?.(result);
      scimIntents.delete(action);
      scimDirectoryBusy = false;
      return loadScimDirectory(ticket, feedback);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      scimDirectoryBusy = false;
      if (isWorkspaceAuthorizationDrift(error)) {
        scimIntents.delete(action);
        return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
      }
      if (!(error instanceof IdentityApiError) || ![0, 503].includes(error.status)) {
        scimIntents.delete(action);
      }
      scimDirectoryError = safePageError(error);
      renderSecurity();
      return false;
    }
  }

  async function loadSaasBilling(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceFinanceAllowed() || typeof api.saasCatalog !== "function"
      || typeof api.subscription !== "function") {
      saasCatalog = null;
      subscription = null;
      subscriptionBusy = false;
      subscriptionError = null;
      subscriptionIntent = null;
      if (render) renderSecurity({ feedback });
      return false;
    }
    subscriptionBusy = true;
    const [catalogOutcome, subscriptionOutcome] = await Promise.all([
      api.saasCatalog({ signal: ticket.signal }).then(
        (value) => ({ value, error: null }),
        (error) => ({ value: null, error }),
      ),
      api.subscription({ signal: ticket.signal }).then(
        (value) => ({ value, error: null }),
        (error) => ({ value: null, error }),
      ),
    ]);
    if (!isCurrent(ticket)) return false;
    const drift = [catalogOutcome.error, subscriptionOutcome.error]
      .find((error) => error && isWorkspaceAuthorizationDrift(error));
    if (drift) throw drift;
    saasCatalog = catalogOutcome.value;
    subscription = subscriptionOutcome.value;
    subscriptionBusy = false;
    const error = catalogOutcome.error ?? subscriptionOutcome.error;
    subscriptionError = error ? safePageError(error) : null;
    if (render) renderSecurity({ feedback: error ? "" : feedback });
    return !error;
  }

  async function loadWorkspaceBilling(ticket, cycleId = null, feedback = "", { render = true } = {}) {
    if (!workspaceFinanceAllowed() || typeof api.billingSummary !== "function") {
      billingSummary = null;
      billingBusy = false;
      billingError = null;
      if (render) renderSecurity({ feedback });
      return false;
    }
    billingBusy = true;
    try {
      const result = await api.billingSummary({ cycleId, signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      billingSummary = result;
      billingBusy = false;
      billingError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      billingSummary = null;
      billingBusy = false;
      billingError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function loadPaymentCommerce(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceFinanceAllowed() || typeof api.billingOrders !== "function") {
      billingOrders = null;
      paymentBusy = false;
      paymentError = null;
      paymentIntentIntent = null;
      refundIntent = null;
      if (render) renderSecurity({ feedback });
      return false;
    }
    paymentBusy = true;
    try {
      const result = await api.billingOrders({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      billingOrders = result;
      paymentBusy = false;
      paymentError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      billingOrders = null;
      paymentBusy = false;
      paymentError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function loadWorkspaceInvoices(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceFinanceAllowed() || typeof api.billingStatements !== "function") {
      billingStatements = null;
      currentInvoice = null;
      invoiceBusy = false;
      invoiceError = null;
      invoiceIntent = null;
      if (render) renderSecurity({ feedback });
      return false;
    }
    invoiceBusy = true;
    try {
      const statements = await api.billingStatements({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      const invoiceId = currentInvoice?.invoice?.invoice_request_id;
      const invoice = invoiceId && typeof api.invoice === "function"
        ? await api.invoice({ invoiceId, signal: ticket.signal })
        : currentInvoice;
      if (!isCurrent(ticket)) return false;
      billingStatements = statements;
      currentInvoice = invoice;
      invoiceBusy = false;
      invoiceError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      billingStatements = null;
      invoiceBusy = false;
      invoiceError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  /** 读取企业认证脱敏摘要，并在 Workspace 授权漂移时交由统一恢复链路处理。 */
  async function loadBusinessVerification(ticket, feedback = "", { render = true } = {}) {
    if (!workspaceBusinessAllowed() || typeof api.businessVerification !== "function") {
      businessVerification = null;
      businessVerificationBusy = false;
      businessVerificationError = null;
      if (render) renderSecurity({ feedback });
      return false;
    }
    businessVerificationBusy = true;
    try {
      const result = await api.businessVerification({
        workspaceId: workspaceContext.workspace_id,
        signal: ticket.signal,
      });
      if (!isCurrent(ticket)) return false;
      businessVerification = result;
      businessVerificationBusy = false;
      businessVerificationError = null;
      if (render) renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) throw error;
      businessVerification = null;
      businessVerificationBusy = false;
      businessVerificationError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function mutateSubscription(input) {
    if (subscriptionBusy) return false;
    const planVersionId = String(input.planVersionId ?? "");
    const fingerprint = JSON.stringify({
      kind: input.kind,
      planVersionId,
      revision: input.revision ?? null,
      subscriptionId: input.subscriptionId ?? null,
      startTrial: input.startTrial === true,
    });
    const idempotencyKey = subscriptionIntent?.fingerprint === fingerprint
      ? subscriptionIntent.idempotencyKey
      : newIdempotencyKey();
    subscriptionIntent = { fingerprint, idempotencyKey };
    const ticket = beginRequest();
    subscriptionBusy = true;
    subscriptionError = null;
    renderSecurity();
    try {
      const operation = input.kind === "create"
        ? api.createSubscription
        : input.kind === "change" ? api.changeSubscription : api.cancelSubscription;
      const result = await queueWrite(() => operation({
        planVersionId,
        subscriptionId: input.subscriptionId,
        startTrial: input.startTrial === true,
        revision: input.revision,
        idempotencyKey,
        signal: ticket.signal,
      }));
      if (!isCurrent(ticket)) return false;
      subscription = result;
      subscriptionBusy = false;
      subscriptionIntent = null;
      const feedback = input.kind === "create"
        ? "订阅意图已创建。"
        : input.kind === "change" ? "套餐变更已登记。" : "订阅已取消。";
      renderSecurity({ feedback });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      subscriptionBusy = false;
      if (isWorkspaceAuthorizationDrift(error)) {
        subscriptionIntent = null;
        return handleWorkspaceActionError(error, "空间权限已变化。", ticket);
      }
      if (!(error instanceof IdentityApiError) || ![0, 503].includes(error.status)) {
        subscriptionIntent = null;
      }
      subscriptionError = safePageError(error);
      renderSecurity();
      return false;
    }
  }

  async function loadWorkspaceAccess(
    ticket,
    feedback,
    previousError,
    retryAuthorizationDrift = true,
  ) {
    if (typeof api.identityWorkspaces !== "function") {
      workspaceChoices = workspaces.map((workspace) => ({
        workspace_id: workspace.workspace_id,
        kind: workspace.kind,
        name: workspace.name,
        slug: workspace.slug,
        role: "owner",
      }));
      workspaceContext = null;
      memberships = [];
      navigationItems = [];
      renderSecurity({ feedback, error: previousError });
      return true;
    }
    try {
      const result = await api.identityWorkspaces({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      workspaceChoices = Array.isArray(result.workspaces) ? result.workspaces : [];
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      workspaceChoices = [];
      scrubWorkspaceAuthorization();
      renderSecurity({ feedback, error: safePageError(error) });
      return false;
    }

    if (typeof api.workspaceSession !== "function") {
      scrubWorkspaceAuthorization();
      renderSecurity({ feedback, error: previousError });
      return true;
    }

    let sessionResult = null;
    try {
      sessionResult = await api.workspaceSession({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (!isWorkspaceSessionMissing(error)) {
        if (isWorkspaceAuthorizationDrift(error)) {
          scrubWorkspaceScope({ clearChoices: true, clearRichWorkspaces: true });
          renderSecurity({ feedback: "空间权限已变化，正在重新校验。" });
          return retryAuthorizationDrift
            ? loadWorkspaces(ticket, feedback, null, false)
            : false;
        }
        scrubWorkspaceAuthorization();
        renderSecurity({ feedback, error: safePageError(error) });
        return false;
      }
    }

    if (
      sessionResult === null &&
      workspaceChoices.length === 1 &&
      workspaceChoices[0].role !== "service_account" &&
      currentSession?.assurance_level !== "recovery" &&
      !autoSwitchAttempted &&
      typeof api.switchWorkspace === "function"
    ) {
      autoSwitchAttempted = true;
      try {
        await queueWrite(() =>
          api.switchWorkspace({
            workspaceId: workspaceChoices[0].workspace_id,
            signal: ticket.signal,
          }),
        );
        if (!isCurrent(ticket)) return false;
        broadcastContextInvalidation();
        sessionResult = await api.workspaceSession({ signal: ticket.signal });
        if (!isCurrent(ticket)) return false;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        if (isExpired(error)) {
          clearToLogin();
          return false;
        }
        scrubWorkspaceAuthorization();
        renderSecurity({ feedback, error: safePageError(error) });
        return false;
      }
    }

    if (sessionResult === null) {
      scrubWorkspaceAuthorization();
      renderSecurity({ feedback, error: previousError });
      return true;
    }

    return loadWorkspaceAuthorization(
      ticket,
      sessionResult,
      feedback,
      previousError,
      retryAuthorizationDrift,
    );
  }

  async function loadNotificationCenter(ticket, { render = true } = {}) {
    if (!workspaceContext || typeof api.notifications !== "function") return false;
    try {
      const [inbox, preferences] = await Promise.all([
        api.notifications({
          unreadOnly: notificationUnreadOnly,
          cursor: notificationCursor,
          signal: ticket.signal,
        }),
        typeof api.notificationPreferences === "function"
          ? api.notificationPreferences({ signal: ticket.signal })
          : Promise.resolve(null),
      ]);
      if (!isCurrent(ticket)) return false;
      notificationInbox = inbox;
      if (preferences) notificationPreferences = preferences;
      notificationBusy = false;
      notificationError = null;
      if (render) renderSecurity();
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      notificationBusy = false;
      notificationError = safePageError(error);
      if (render) renderSecurity();
      return false;
    }
  }

  async function loadWorkspaceAuthorization(
    ticket,
    sessionResult,
    feedback,
    previousError,
    retryAuthorizationDrift = true,
  ) {
    workspaceContext = sessionResult.context;
    if (
      workspaceContext.role === "service_account" ||
      !workspaceChoices.some((choice) => choice.workspace_id === workspaceContext.workspace_id)
    ) {
      scrubWorkspaceAuthorization();
      renderSecurity({
        feedback,
        error: { message: "当前空间上下文与账号空间列表不一致。", requestId: "-" },
      });
      return false;
    }

    try {
      const [membershipResult, navigationResult, onboardingOutcome] = await Promise.all([
        typeof api.memberships === "function"
          ? api.memberships({ signal: ticket.signal })
          : Promise.resolve({ memberships: [] }),
        typeof api.workspaceNavigation === "function"
          ? api.workspaceNavigation({ signal: ticket.signal })
          : Promise.resolve({ navigation_items: [] }),
        typeof api.onboarding === "function"
          ? api.onboarding({ signal: ticket.signal }).then(
            (value) => ({ value, error: null }),
            (error) => ({ value: null, error }),
          )
          : Promise.resolve({ value: null, error: null }),
      ]);
      if (!isCurrent(ticket)) return false;
      if (onboardingOutcome.error && isWorkspaceAuthorizationDrift(onboardingOutcome.error)) {
        throw onboardingOutcome.error;
      }
      memberships = Array.isArray(membershipResult.memberships)
        ? membershipResult.memberships
        : [];
      navigationItems = Array.isArray(navigationResult.navigation_items)
        ? navigationResult.navigation_items
        : [];
      workspaceContext = {
        ...workspaceContext,
        permission_codes: Array.isArray(navigationResult.permission_codes)
          ? [...navigationResult.permission_codes]
          : [],
        permission_revision: navigationResult.permission_revision,
        authorization_fingerprint: navigationResult.authorization_fingerprint,
      };
      onboardingBusy = false;
      onboardingIntent = null;
      if (onboardingOutcome.error) {
        onboarding = null;
        onboardingError = safePageError(onboardingOutcome.error);
      } else if (onboardingOutcome.value) {
        onboardingError = null;
        applyOnboardingProjection(onboardingOutcome.value);
      }
      await Promise.all([
        loadSaasBilling(ticket, "", { render: false }),
        loadWorkspaceBilling(ticket, null, "", { render: false }),
        loadPaymentCommerce(ticket, "", { render: false }),
        loadWorkspaceInvoices(ticket, "", { render: false }),
        loadBusinessVerification(ticket, "", { render: false }),
        loadEnterpriseIdentity(ticket, "", { render: false }),
        loadScimDirectory(ticket, "", { render: false }),
        loadConditionalAccess(ticket, "", { render: false }),
        loadNotificationCenter(ticket, { render: false }),
      ]);
      if (!isCurrent(ticket)) return false;
      renderSecurity({ feedback, error: previousError });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isWorkspaceAuthorizationDrift(error)) {
        scrubWorkspaceScope({ clearChoices: true, clearRichWorkspaces: true });
        renderSecurity({ feedback: "空间权限已变化，正在重新校验。" });
        return retryAuthorizationDrift
          ? loadWorkspaces(ticket, feedback, null, false)
          : false;
      } else {
        memberships = [];
        navigationItems = [];
      }
      renderSecurity({ feedback, error: safePageError(error) });
      return false;
    }
  }

  async function invalidateWorkspaceContext({
    broadcast = true,
    feedback = "空间上下文已失效，正在重新校验。",
    existingTicket = null,
  } = {}) {
    const ticket = existingTicket ?? beginRequest();
    scrubWorkspaceScope({ clearChoices: true, clearRichWorkspaces: true });
    renderSecurity({ feedback });
    if (broadcast) broadcastContextInvalidation();
    return loadWorkspaces(ticket, feedback, null);
  }

  function handleWorkspaceActionError(error, fallbackMessage, ticket) {
    if (isWorkspaceAuthorizationDrift(error)) {
      broadcastContextInvalidation();
      return invalidateWorkspaceContext({
        broadcast: false,
        feedback: "空间权限已变化，正在重新校验。",
        existingTicket: ticket,
      });
    }
    const safe = safePageError(error);
    renderSecurity({
      error: {
        message: safe.message || fallbackMessage,
        requestId: safe.requestId,
      },
    });
    return false;
  }

  function beginRequest() {
    generation += 1;
    activeRequest?.abort();
    activeRequest = new AbortController();
    return { generation, signal: activeRequest.signal };
  }

  function queueWrite(write) {
    const operation = pendingWrite.catch(() => undefined).then(write);
    pendingWrite = operation.then(
      () => undefined,
      () => undefined,
    );
    return operation;
  }

  function clearToLogin() {
    const ticket = beginRequest();
    scrubSensitiveInputs();
    pendingChallenge = null;
    riskChallenge = null;
    riskChallengeIntent = null;
    scrubProtectedState();
    renderIdentityLoginPage(root);
    return ticket;
  }

  function applyIdentityResult(result) {
    account = result.account;
    currentSession = result.current_session;
    sessions = [currentSession];
  }

  function renderSecurity(extra = {}) {
    if (!account || !currentSession) {
      renderIdentityLoginPage(root, { error: extra.error });
      return;
    }
    renderIdentitySecurityPage(root, {
      account,
      currentSession,
      sessions,
      workspaces,
      workspaceChoices,
      workspaceContext,
      memberships,
      navigationItems,
      onboarding,
      onboardingSelectedStepId,
      onboardingBusy,
      onboardingError,
      saasCatalog,
      subscription,
      subscriptionBusy,
      subscriptionError,
      billingSummary,
      billingBusy,
      billingError,
      billingOrders,
      paymentBusy,
      paymentError,
      billingStatements,
      currentInvoice,
      invoiceBusy,
      invoiceError,
      businessVerification,
      businessVerificationBusy,
      businessVerificationError,
      enterpriseIdentity,
      enterpriseIdentityBusy,
      enterpriseIdentityError,
      enterpriseVerificationRecord,
      enterpriseConnectionProtocol,
      scimDirectory,
      scimDirectoryBusy,
      scimDirectoryError,
      scimBearerToken,
      conditionalAccess,
      conditionalAccessBusy,
      conditionalAccessError,
      conditionalAccessVisible: workspaceConditionalReadAllowed(),
      conditionalAccessCanManage: workspaceConditionalManageAllowed(),
      notificationCenterOpen,
      notificationInbox,
      notificationPreferences,
      notificationBusy,
      notificationError,
      notificationUnreadOnly,
      notificationPage: notificationCursorHistory.length + 1,
      workspaceView,
      workspaceDraft,
      invitationResult,
      hasInvitationSecret: Boolean(invitationSecretLink),
      mfaEnrollment,
      mfaEnrollmentMethod,
      identitySecurity,
      identitySecurityBusy,
      identitySecurityError,
      recoveryCodes,
      factorDisableIndex,
      platformAccess,
      feedback: extra.feedback,
      error: extra.error,
    });
  }

  function scrubProtectedState() {
    account = null;
    currentSession = null;
    sessions = [];
    mfaEnrollment = null;
    mfaEnrollmentMethod = "totp";
    identitySecurity = null;
    identitySecurityBusy = false;
    identitySecurityError = null;
    recoveryCodes = null;
    factorDisableIndex = null;
    platformAccess = false;
    autoSwitchAttempted = false;
    scrubWorkspaceScope({ clearChoices: true, clearRichWorkspaces: true });
  }

  function scrubWorkspaceScope({ clearChoices = false, clearRichWorkspaces = false } = {}) {
    if (clearChoices) workspaceChoices = [];
    if (clearRichWorkspaces) workspaces = [];
    scrubWorkspaceAuthorization();
    workspaceView = "summary";
    workspaceDraft = null;
    invitationResult = null;
    clearInvitationSecret();
  }

  function scrubWorkspaceAuthorization() {
    workspaceContext = null;
    memberships = [];
    navigationItems = [];
    onboarding = null;
    onboardingSelectedStepId = null;
    onboardingBusy = false;
    onboardingError = null;
    onboardingIntent = null;
    saasCatalog = null;
    subscription = null;
    subscriptionBusy = false;
    subscriptionError = null;
    subscriptionIntent = null;
    billingSummary = null;
    billingBusy = false;
    billingError = null;
    billingOrders = null;
    paymentBusy = false;
    paymentError = null;
    paymentIntentIntent = null;
    refundIntent = null;
    billingStatements = null;
    currentInvoice = null;
    invoiceBusy = false;
    invoiceError = null;
    invoiceIntent = null;
    businessVerification = null;
    businessVerificationBusy = false;
    businessVerificationError = null;
    enterpriseIdentity = null;
    enterpriseIdentityBusy = false;
    enterpriseIdentityError = null;
    enterpriseVerificationRecord = null;
    enterpriseConnectionProtocol = "oidc";
    enterpriseIntents = new Map();
    scimDirectory = null;
    scimDirectoryBusy = false;
    scimDirectoryError = null;
    scimBearerToken = null;
    scimIntents = new Map();
    conditionalAccess = null;
    conditionalAccessBusy = false;
    conditionalAccessError = null;
    notificationCenterOpen = false;
    notificationInbox = null;
    notificationPreferences = null;
    notificationBusy = false;
    notificationError = null;
    notificationUnreadOnly = false;
    notificationCursor = null;
    notificationCursorHistory = [];
  }

  function clearInvitationSecret() {
    invitationSecretLink = null;
    invitationSecretMail = null;
  }

  function scrubPasswords() {
    scrubSensitiveInputs();
  }

  function scrubSensitiveInputs() {
    for (const selector of [
      'input[type="password"]',
      'input[name="code"]',
      'input[name="answer"]',
      'input[autocomplete="one-time-code"]',
    ]) {
      for (const input of root.querySelectorAll?.(selector) ?? []) {
        input.value = "";
      }
    }
  }

  function isCurrent(ticket) {
    return ticket.generation === generation && ticket.signal.aborted !== true;
  }

  function bindContextChannel() {
    if (contextChannel || destroyed) return;
    try {
      contextChannel = channelFactory?.("metahub-workspace-context-v1") ?? null;
    } catch {
      contextChannel = null;
    }
    if (!contextChannel) return;
    contextChannel.onmessage = (event) => {
      const message = event?.data;
      if (
        destroyed ||
        !message ||
        typeof message !== "object" ||
        Array.isArray(message) ||
        Object.keys(message).length !== 2 ||
        message.type !== "context-invalidated" ||
        !Number.isSafeInteger(message.generation) ||
        message.generation < 1
      ) {
        return;
      }
      void invalidateWorkspaceContext({ broadcast: false });
    };
  }

  function broadcastContextInvalidation() {
    contextGeneration += 1;
    try {
      contextChannel?.postMessage?.({
        type: "context-invalidated",
        generation: contextGeneration,
      });
    } catch {
      // A closed or unavailable channel must not block server-authoritative revalidation.
    }
  }

  function bindInteractions() {
    root.addEventListener?.("submit", async (event) => {
      const form = event.target;
      if (form?.matches?.("[data-identity-login-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.login({ email: fields.get("email"), password: fields.get("password") });
      } else if (form?.matches?.("[data-identity-risk-challenge-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const answer = String(fields.get("answer") ?? "");
        for (const input of form.querySelectorAll?.('input[name="answer"]') ?? []) {
          input.value = "";
        }
        await controller.completeRiskChallenge({ answer });
      } else if (form?.matches?.("[data-identity-registration-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.registerAccount({
          email: fields.get("email"),
          displayName: fields.get("displayName"),
          password: fields.get("password"),
          confirmPassword: fields.get("confirmPassword"),
          consent: fields.get("consent") === "on",
        });
      } else if (form?.matches?.("[data-identity-verification-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.verifyRegistration({ code: fields.get("code") });
      } else if (form?.matches?.("[data-identity-reset-request-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.requestPasswordReset({ email: fields.get("email") });
      } else if (form?.matches?.("[data-identity-reset-confirm-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.confirmPasswordReset({
          code: fields.get("code"),
          newPassword: fields.get("newPassword"),
          confirmPassword: fields.get("confirmPassword"),
        });
      } else if (form?.matches?.("[data-identity-password-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.rotatePassword({
          currentPassword: fields.get("currentPassword"),
          newPassword: fields.get("newPassword"),
          confirmPassword: fields.get("confirmPassword"),
        });
      } else if (form?.matches?.("[data-identity-mfa-enrollment-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          currentPassword: fields.get("currentPassword"),
          method: fields.get("method") || mfaEnrollmentMethod,
        };
        scrubFormSecrets(form);
        await controller.beginMfaEnrollment(input);
        input.currentPassword = "";
      } else if (form?.matches?.("[data-identity-mfa-verification-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = { code: fields.get("code") };
        scrubFormSecrets(form);
        await controller.verifyMfaEnrollment(input);
        input.code = "";
      } else if (form?.matches?.("[data-identity-factor-disable-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = { currentPassword: fields.get("currentPassword") };
        scrubFormSecrets(form);
        await controller.disableMfaFactor(input);
        input.currentPassword = "";
      } else if (form?.matches?.("[data-identity-recovery-begin-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          currentPassword: fields.get("currentPassword"),
          confirmed: fields.get("confirmed") === "on",
        };
        scrubFormSecrets(form);
        await controller.beginAccountRecovery(input);
        input.currentPassword = "";
      } else if (form?.matches?.("[data-identity-recovery-complete-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          notificationCode: fields.get("notificationCode"),
          recoveryCode: fields.get("recoveryCode"),
          confirmed: fields.get("confirmed") === "on",
        };
        scrubFormSecrets(form);
        await controller.completeAccountRecovery(input);
        input.notificationCode = "";
        input.recoveryCode = "";
      } else if (form?.matches?.("[data-identity-trusted-device-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          label: fields.get("label"),
          totpCode: fields.get("totpCode"),
        };
        scrubFormSecrets(form);
        await controller.trustCurrentDevice(input);
        input.totpCode = "";
      } else if (form?.matches?.("[data-conditional-access-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          mode: fields.get("mode"),
          requireMfa: fields.get("requireMfa") === "on",
          requireStepUpForHighRisk: fields.get("requireStepUpForHighRisk") === "on",
          allowTrustedDevices: fields.get("allowTrustedDevices") === "on",
          regionRule: fields.get("regionRule"),
          networkRule: fields.get("networkRule"),
          allowedCidrs: fields.get("allowedCidrs"),
          exceptionMembershipIds: fields.getAll("exceptionMembershipIds"),
          method: fields.get("stepUpMethod"),
          code: fields.get("stepUpCode"),
          confirmed: fields.get("confirmed") === "on",
        };
        scrubFormSecrets(form);
        await controller.saveConditionalAccess(input);
        input.code = "";
      } else if (form?.matches?.("[data-workspace-create-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.createWorkspace({
          kind: fields.get("kind"),
          name: fields.get("name"),
          slug: fields.get("slug"),
          region: fields.get("region"),
          serviceMode: fields.get("serviceMode"),
        });
      } else if (form?.matches?.("[data-membership-update-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.updateMembership(form.dataset.membershipIndex, {
          role: fields.get("role"),
          status: fields.get("status"),
        });
      } else if (form?.matches?.("[data-membership-invitation-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.inviteMembership({
          email: fields.get("email"),
          role: fields.get("role"),
          expiresInHours: Number(fields.get("expiresInHours")),
        });
      } else if (form?.matches?.("[data-owner-transfer-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.transferOwner({
          targetIndex: fields.get("targetIndex"),
          currentPassword: fields.get("currentPassword"),
          confirmed: fields.get("confirmTransfer") === "on",
        });
      } else if (form?.matches?.("[data-enterprise-domain-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.createEnterpriseDomain({ domain: fields.get("domain") });
      } else if (form?.matches?.("[data-enterprise-connection-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          domainIndex: fields.get("domainIndex"),
          connectionCode: fields.get("connectionCode"),
          displayName: fields.get("displayName"),
          issuer: fields.get("issuer"),
          authorizationEndpoint: fields.get("authorizationEndpoint"),
          tokenEndpoint: fields.get("tokenEndpoint"),
          clientId: fields.get("clientId"),
          clientSecret: fields.get("clientSecret"),
          jwksText: fields.get("jwks"),
          ssoUrl: fields.get("ssoUrl"),
          certificatePem: fields.get("certificatePem"),
        };
        scrubEnterpriseConnectionForm(form);
        try {
          await controller.createEnterpriseSsoConnection(input);
        } finally {
          input.clientSecret = "";
          input.certificatePem = "";
          input.jwksText = "";
          input.jwks = null;
        }
      } else if (form?.matches?.("[data-enterprise-policy-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.updateEnterpriseSsoPolicy({
          mode: fields.get("mode"),
          connectionIndex: fields.get("connectionIndex"),
          candidateIndex: fields.get("candidateIndex"),
        });
      } else if (form?.matches?.("[data-scim-token-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.createScimToken({
          expiresInDays: Number(fields.get("expiresInDays")),
        });
      } else if (form?.matches?.("[data-scim-mapping-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.upsertScimRoleMapping({
          groupIndex: fields.get("groupIndex"),
          roleCode: fields.get("roleCode"),
        });
      } else if (form?.matches?.("[data-saas-subscription-create-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.createSubscription({
          planVersionId: fields.get("planVersionId"),
          startTrial: fields.get("startTrial") === "on",
        });
      } else if (form?.matches?.("[data-saas-subscription-change-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.changeSubscription({ planVersionId: fields.get("planVersionId") });
      } else if (form?.matches?.("[data-workspace-billing-cycle-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.selectBillingCycle(fields.get("cycleId"));
      } else if (form?.matches?.("[data-payment-intent-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.createPaymentIntent({
          orderId: fields.get("orderId"),
          provider: fields.get("provider"),
        });
      } else if (form?.matches?.("[data-refund-request-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.requestRefund({
          orderId: fields.get("orderId"),
          amountMinor: fields.get("amountMinor"),
          reasonCode: fields.get("reasonCode"),
          confirmed: fields.get("confirmed") === "on",
        });
      } else if (form?.matches?.("[data-invoice-request-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.createInvoice({
          billingStatementId: fields.get("billingStatementId"),
          invoiceType: fields.get("invoiceType"),
        });
      } else if (form?.matches?.("[data-business-verification-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          country_code: fields.get("countryCode"),
          entity_type: fields.get("entityType"),
          legal_name: fields.get("legalName"),
          registration_number: fields.get("registrationNumber"),
          evidence: [{
            evidence_type: fields.get("evidenceType"),
            object_reference: fields.get("objectReference"),
            content_sha256: fields.get("contentSha256"),
            mime_type: fields.get("mimeType"),
            size_bytes: Number(fields.get("sizeBytes")),
            kms_key_version: Number(fields.get("kmsKeyVersion")),
            malware_status: "passed",
            dlp_status: "passed",
            scan_attestation: fields.get("scanAttestation"),
          }],
          reason_code: businessVerification?.verification ? "resubmission" : "initial_submission",
          confirmed: fields.get("confirmed") === "on",
        };
        scrubBusinessForm(form);
        await controller.submitBusinessVerification(input);
      } else if (form?.matches?.("[data-business-billing-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        const input = {
          revision: Number(businessVerification?.billing_profile?.revision ?? 0),
          billingProfile: {
            invoice_title: fields.get("invoiceTitle"),
            tax_number: fields.get("taxNumber"),
            address: fields.get("address") || null,
            phone: fields.get("phone") || null,
            bank_name: fields.get("bankName") || null,
            bank_account: fields.get("bankAccount") || null,
            confirmed: fields.get("confirmed") === "on",
          },
        };
        scrubBusinessForm(form);
        await controller.updateBusinessBilling(input);
      }
    });
    root.addEventListener?.("click", async (event) => {
      const button = event.target?.closest?.("[data-identity-action]");
      if (!button || !root.contains?.(button)) return;
      if (button.dataset.identityAction === "logout") {
        await controller.logout();
      } else if (button.dataset.identityAction === "revoke-session") {
        await controller.revokeSession(button.dataset.sessionIndex);
      } else if (button.dataset.identityAction === "set-mfa-method") {
        controller.setMfaEnrollmentMethod(button.dataset.mfaMethod);
      } else if (button.dataset.identityAction === "close-recovery-codes") {
        controller.closeRecoveryCodes();
      } else if (button.dataset.identityAction === "select-factor-disable") {
        controller.selectFactorDisable(button.dataset.factorIndex);
      } else if (button.dataset.identityAction === "cancel-factor-disable") {
        controller.cancelFactorDisable();
      } else if (button.dataset.identityAction === "revoke-trusted-device") {
        await controller.revokeTrustedDevice(button.dataset.trustedDeviceIndex);
      } else if (button.dataset.identityAction === "refresh-conditional-access") {
        await controller.refreshConditionalAccess();
      } else if (button.dataset.identityAction === "simulate-conditional-access") {
        await controller.simulateConditionalAccess();
      } else if (button.dataset.identityAction === "show-register") {
        controller.showRegistration();
      } else if (button.dataset.identityAction === "show-reset") {
        controller.showReset();
      } else if (button.dataset.identityAction === "show-login") {
        controller.showLogin();
      } else if (button.dataset.identityAction === "cancel-risk-challenge") {
        await controller.cancelRiskChallenge();
      } else if (button.dataset.identityAction === "resend-verification") {
        await controller.resendVerification();
      } else if (button.dataset.identityAction === "show-workspace-create") {
        controller.showWorkspaceCreate();
      } else if (button.dataset.identityAction === "cancel-workspace-create") {
        controller.cancelWorkspaceCreate();
      } else if (button.dataset.identityAction === "set-workspace-kind") {
        controller.setWorkspaceKind(button.dataset.workspaceKind);
      } else if (button.dataset.identityAction === "switch-workspace") {
        await controller.switchWorkspace(button.dataset.workspaceIndex);
      } else if (button.dataset.identityAction === "refresh-workspace-context") {
        await controller.refreshWorkspaceContext();
      } else if (button.dataset.identityAction === "select-onboarding-step") {
        controller.selectOnboardingStep(button.dataset.onboardingStepIndex);
      } else if (button.dataset.identityAction === "confirm-onboarding-step") {
        await controller.confirmOnboardingStep();
      } else if (button.dataset.identityAction === "defer-onboarding-step") {
        await controller.deferOnboardingStep();
      } else if (button.dataset.identityAction === "reopen-onboarding-step") {
        await controller.reopenOnboardingStep();
      } else if (button.dataset.identityAction === "recheck-onboarding") {
        await controller.recheckOnboarding();
      } else if (button.dataset.identityAction === "refresh-onboarding") {
        await controller.refreshOnboarding();
      } else if (button.dataset.identityAction === "refresh-subscription") {
        await controller.refreshSubscription();
      } else if (button.dataset.identityAction === "refresh-billing") {
        await controller.refreshBilling();
      } else if (button.dataset.identityAction === "refresh-payment-commerce") {
        await controller.refreshPaymentCommerce();
      } else if (button.dataset.identityAction === "refresh-invoices") {
        await controller.refreshInvoices();
      } else if (button.dataset.identityAction === "refresh-invoice") {
        await controller.refreshInvoice();
      } else if (button.dataset.identityAction === "download-invoice-artifact") {
        await controller.downloadInvoiceArtifact(button.dataset.artifactId);
      } else if (button.dataset.identityAction === "cancel-subscription") {
        await controller.cancelSubscription();
      } else if (button.dataset.identityAction === "download-billing-export") {
        await controller.downloadBillingExport();
      } else if (button.dataset.identityAction === "refresh-business-verification") {
        await controller.refreshBusinessVerification();
      } else if (button.dataset.identityAction === "refresh-enterprise-identity") {
        await controller.refreshEnterpriseIdentity();
      } else if (button.dataset.identityAction === "refresh-scim-directory") {
        await controller.refreshScimDirectory();
      } else if (button.dataset.identityAction === "rotate-scim-token") {
        await controller.rotateScimToken(button.dataset.scimTokenIndex);
      } else if (button.dataset.identityAction === "resolve-scim-conflict") {
        await controller.resolveScimConflict(
          button.dataset.scimConflictIndex,
          button.dataset.scimResolution,
        );
      } else if (button.dataset.identityAction === "copy-scim-token") {
        await controller.copyScimBearerToken();
      } else if (button.dataset.identityAction === "dismiss-scim-token") {
        controller.dismissScimBearerToken();
      } else if (button.dataset.identityAction === "set-enterprise-protocol") {
        controller.setEnterpriseConnectionProtocol(button.dataset.enterpriseProtocol);
      } else if (button.dataset.identityAction === "verify-enterprise-domain") {
        await controller.verifyEnterpriseDomain(button.dataset.enterpriseDomainIndex);
      } else if (button.dataset.identityAction === "test-enterprise-connection") {
        await controller.testEnterpriseSsoConnection(
          button.dataset.enterpriseConnectionIndex,
        );
      } else if (button.dataset.identityAction === "copy-invitation-link") {
        await controller.copyInvitationSecret("link");
      } else if (button.dataset.identityAction === "copy-invitation-mail") {
        await controller.copyInvitationSecret("mail");
      } else if (button.dataset.identityAction === "open-notifications") {
        await controller.openNotifications();
      } else if (button.dataset.identityAction === "close-notifications") {
        controller.closeNotifications();
      } else if (button.dataset.identityAction === "refresh-notifications") {
        await controller.refreshNotifications();
      } else if (button.dataset.identityAction === "notification-filter-all") {
        await controller.setNotificationFilter(false);
      } else if (button.dataset.identityAction === "notification-filter-unread") {
        await controller.setNotificationFilter(true);
      } else if (button.dataset.identityAction === "notification-next") {
        await controller.nextNotificationPage();
      } else if (button.dataset.identityAction === "notification-previous") {
        await controller.previousNotificationPage();
      } else if (button.dataset.identityAction === "mark-notification-read") {
        await controller.markNotificationRead(button.dataset.notificationIndex);
      }
    });
    root.addEventListener?.("change", async (event) => {
      const toggle = event.target;
      if (!toggle?.matches?.("[data-notification-preference]")) return;
      await controller.updateNotificationPreference(
        toggle.dataset.notificationCategory,
        toggle.dataset.notificationChannel,
        toggle.checked === true,
      );
    });
    root.addEventListener?.("input", (event) => {
      if (event.target?.matches?.("[data-workspace-search]")) {
        filterWorkspaceOptions(event.target.value);
      }
    });
  }

  function filterWorkspaceOptions(value) {
    const query = String(value ?? "").trim().toLocaleLowerCase("zh-CN");
    for (const option of root.querySelectorAll?.("[data-workspace-option]") ?? []) {
      const text = String(option.dataset?.workspaceSearchText ?? "").toLocaleLowerCase("zh-CN");
      option.hidden = Boolean(query) && !text.includes(query);
    }
  }
}

/** 提交开始后立即清空可见企业资料控件，隐藏 revision 不属于敏感明文。 */
function scrubBusinessForm(form) {
  for (const input of form?.querySelectorAll?.("input, textarea") ?? []) {
    if (input.type === "hidden") continue;
    if (input.type === "checkbox" || input.type === "radio") input.checked = false;
    else input.value = "";
  }
}

function scrubFormSecrets(form) {
  for (const input of form?.querySelectorAll?.(
    'input[type="password"], input[autocomplete="one-time-code"]',
  ) ?? []) {
    input.value = "";
  }
}

function scrubEnterpriseConnectionForm(form) {
  for (const name of ["clientSecret", "certificatePem", "jwks"]) {
    for (const input of form?.querySelectorAll?.(`[name="${name}"]`) ?? []) {
      input.value = "";
    }
  }
}

async function secureEnterpriseFingerprint(input) {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle || typeof globalThis.TextEncoder !== "function") {
    throw new TypeError("secure enterprise request fingerprinting is unavailable");
  }
  const bytes = new TextEncoder().encode(JSON.stringify(input));
  const digest = await subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

/** 擦除控制器调用栈中的主体、证据和开票明文，避免异步状态长期持有。 */
function scrubBusinessInput(input) {
  if (!input || typeof input !== "object") return;
  for (const key of ["legal_name", "registration_number"]) {
    if (key in input) input[key] = "";
  }
  for (const evidence of input.evidence ?? []) {
    evidence.object_reference = "";
    evidence.scan_attestation = "";
  }
  if (input.billingProfile) {
    for (const key of [
      "invoice_title", "tax_number", "address", "phone", "bank_name", "bank_account",
    ]) input.billingProfile[key] = null;
  }
}

function parsePolicyLines(value) {
  const entries = String(value ?? "")
    .split(/[\r\n,]+/u)
    .map((item) => item.trim())
    .filter(Boolean);
  return [...new Set(entries)];
}

function normalizeSessions(list, currentSession) {
  const sessions = Array.isArray(list) ? [...list] : [];
  const currentIndex = sessions.findIndex((session) => session.is_current);
  if (currentIndex < 0 && currentSession) {
    sessions.unshift(currentSession);
  } else if (currentIndex > 0) {
    sessions.unshift(sessions.splice(currentIndex, 1)[0]);
  }
  return sessions;
}

function safePageError(error) {
  if (error instanceof IdentityApiError || error instanceof PlatformApiError) {
    return { message: error.message, requestId: error.requestId };
  }
  return { message: "账号请求未完成，请稍后重试。", requestId: "-" };
}

function isExpired(error) {
  return error instanceof IdentityApiError && error.status === 401;
}

function isWorkspaceSessionMissing(error) {
  return error instanceof IdentityApiError && error.status === 401;
}

function isWorkspaceAuthorizationDrift(error) {
  return error instanceof IdentityApiError && new Set([401, 403]).has(error.status);
}

function isOnboardingRevisionDrift(error) {
  return error instanceof IdentityApiError && (
    error.status === 412 ||
    new Set(["onboarding.revision_conflict", "onboarding.conflict"]).has(error.code)
  );
}

function isAbort(error) {
  return error?.name === "AbortError";
}

function defaultConfirm(message) {
  return globalThis.confirm?.(message) === true;
}

function defaultChannelFactory(name) {
  return globalThis.window && typeof globalThis.BroadcastChannel === "function"
    ? new globalThis.BroadcastChannel(name)
    : null;
}

async function defaultCopyText(value) {
  if (typeof globalThis.navigator?.clipboard?.writeText !== "function") {
    throw new Error("clipboard unavailable");
  }
  await globalThis.navigator.clipboard.writeText(value);
}

function defaultMonotonicNow() {
  return globalThis.performance?.now?.() ?? Date.now();
}

function defaultIdempotencyKey() {
  const value = globalThis.crypto?.randomUUID?.();
  if (!value) throw new TypeError("secure idempotency key generation is unavailable");
  return `workspace-${value}`;
}
