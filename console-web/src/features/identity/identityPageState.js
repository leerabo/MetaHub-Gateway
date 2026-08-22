import { IdentityApiError } from "../../lib/identity.js";
import { buildInvitationAcceptLink } from "../../lib/invitations.js";
import {
  renderIdentityLoginPage,
  renderIdentityRegistrationPage,
  renderIdentityResetConfirmPage,
  renderIdentityResetRequestPage,
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
  let workspaceView = "summary";
  let workspaceDraft = null;
  let invitationResult = null;
  let invitationSecretLink = null;
  let invitationSecretMail = null;
  let pendingChallenge = null;
  let mfaEnrollment = null;
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
    showLogin() {
      clearToLogin();
      return true;
    },
    showRegistration() {
      beginRequest();
      scrubSensitiveInputs();
      scrubProtectedState();
      pendingChallenge = null;
      renderIdentityRegistrationPage(root);
      return true;
    },
    showReset() {
      beginRequest();
      scrubSensitiveInputs();
      scrubProtectedState();
      pendingChallenge = null;
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
    async beginMfaEnrollment({ currentPassword }) {
      if (typeof api.beginMfa !== "function" || account?.mfa_configured) return false;
      const ticket = beginRequest();
      mfaEnrollment = null;
      scrubSensitiveInputs();
      renderSecurity({ feedback: "正在创建多重验证密钥…" });
      try {
        const result = await queueWrite(() =>
          api.beginMfa({ currentPassword, signal: ticket.signal }),
        );
        if (!isCurrent(ticket)) return false;
        mfaEnrollment = result;
        renderSecurity({ feedback: "多重验证密钥已创建。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
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
      return loadPlatformAccess(ticket, feedback, previousError);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      sessions = currentSession ? [currentSession] : [];
      return loadPlatformAccess(ticket, feedback, safePageError(error));
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
      onboardingBusy = false;
      onboardingIntent = null;
      if (onboardingOutcome.error) {
        onboarding = null;
        onboardingError = safePageError(onboardingOutcome.error);
      } else if (onboardingOutcome.value) {
        onboardingError = null;
        applyOnboardingProjection(onboardingOutcome.value);
      }
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
      workspaceView,
      workspaceDraft,
      invitationResult,
      hasInvitationSecret: Boolean(invitationSecretLink),
      mfaEnrollment,
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
        await controller.beginMfaEnrollment({ currentPassword: fields.get("currentPassword") });
      } else if (form?.matches?.("[data-identity-mfa-verification-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.verifyMfaEnrollment({ code: fields.get("code") });
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
      }
    });
    root.addEventListener?.("click", async (event) => {
      const button = event.target?.closest?.("[data-identity-action]");
      if (!button || !root.contains?.(button)) return;
      if (button.dataset.identityAction === "logout") {
        await controller.logout();
      } else if (button.dataset.identityAction === "revoke-session") {
        await controller.revokeSession(button.dataset.sessionIndex);
      } else if (button.dataset.identityAction === "show-register") {
        controller.showRegistration();
      } else if (button.dataset.identityAction === "show-reset") {
        controller.showReset();
      } else if (button.dataset.identityAction === "show-login") {
        controller.showLogin();
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
      } else if (button.dataset.identityAction === "copy-invitation-link") {
        await controller.copyInvitationSecret("link");
      } else if (button.dataset.identityAction === "copy-invitation-mail") {
        await controller.copyInvitationSecret("mail");
      }
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
  if (error instanceof IdentityApiError) {
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
