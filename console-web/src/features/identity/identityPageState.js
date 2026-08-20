import { IdentityApiError } from "../../lib/identity.js";
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
  confirmAction = defaultConfirm,
  invitationToken = null,
  newIdempotencyKey = defaultIdempotencyKey,
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
  let workspaceView = "summary";
  let workspaceDraft = null;
  let pendingChallenge = null;
  let pendingWrite = Promise.resolve();

  const controller = {
    async mount() {
      bindInteractions();
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
    showWorkspaceCreate() {
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
        renderSecurity({ feedback: "空间已创建。" });
        return true;
      } catch (error) {
        if (!isCurrent(ticket) || isAbort(error)) return false;
        renderSecurity({ error: safePageError(error) });
        return false;
      }
    },
    destroy() {
      generation += 1;
      activeRequest?.abort();
      activeRequest = null;
      scrubSensitiveInputs();
      pendingChallenge = null;
      scrubProtectedState();
      root.innerHTML = "";
    },
  };

  return controller;

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
      return loadWorkspaces(ticket, feedback, previousError);
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      sessions = currentSession ? [currentSession] : [];
      return loadWorkspaces(ticket, feedback, safePageError(error));
    }
  }

  async function loadWorkspaces(ticket, feedback, previousError) {
    if (typeof api.workspaces !== "function") {
      workspaces = [];
      renderSecurity({ feedback, error: previousError });
      return true;
    }
    try {
      const list = await api.workspaces({ signal: ticket.signal });
      if (!isCurrent(ticket)) return false;
      workspaces = Array.isArray(list.workspaces) ? list.workspaces : [];
      renderSecurity({ feedback, error: previousError });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      workspaces = [];
      renderSecurity({ feedback, error: safePageError(error) });
      return false;
    }
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
      workspaceView,
      workspaceDraft,
      feedback: extra.feedback,
      error: extra.error,
    });
  }

  function scrubProtectedState() {
    account = null;
    currentSession = null;
    sessions = [];
    workspaces = [];
    workspaceView = "summary";
    workspaceDraft = null;
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
      }
    });
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

function isAbort(error) {
  return error?.name === "AbortError";
}

function defaultConfirm(message) {
  return globalThis.confirm?.(message) === true;
}

function defaultIdempotencyKey() {
  const value = globalThis.crypto?.randomUUID?.();
  if (!value) throw new TypeError("secure idempotency key generation is unavailable");
  return `workspace-${value}`;
}
