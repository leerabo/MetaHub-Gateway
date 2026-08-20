import { IdentityApiError } from "../../lib/identity.js";
import { renderIdentityLoginPage, renderIdentitySecurityPage } from "./renderIdentityPage.js";

export function createIdentityPageController({ root, api, confirmAction = defaultConfirm } = {}) {
  if (!root || !api) {
    throw new TypeError("identity page requires root and api");
  }
  let generation = 0;
  let activeRequest = null;
  let account = null;
  let currentSession = null;
  let sessions = [];
  let pendingWrite = Promise.resolve();

  const controller = {
    async mount() {
      bindInteractions();
      return loadProtectedState();
    },
    async login({ email, password }) {
      const ticket = beginRequest();
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
    destroy() {
      generation += 1;
      activeRequest?.abort();
      activeRequest = null;
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
      renderSecurity({ feedback, error: previousError });
      return true;
    } catch (error) {
      if (!isCurrent(ticket) || isAbort(error)) return false;
      if (isExpired(error)) {
        clearToLogin();
        return false;
      }
      sessions = currentSession ? [currentSession] : [];
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
    scrubPasswords();
    scrubProtectedState();
    renderIdentityLoginPage(root);
    return ticket;
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
      feedback: extra.feedback,
      error: extra.error,
    });
  }

  function scrubProtectedState() {
    account = null;
    currentSession = null;
    sessions = [];
  }

  function scrubPasswords() {
    for (const input of root.querySelectorAll?.('input[type="password"]') ?? []) {
      input.value = "";
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
      } else if (form?.matches?.("[data-identity-password-form]")) {
        event.preventDefault();
        const fields = new FormData(form);
        await controller.rotatePassword({
          currentPassword: fields.get("currentPassword"),
          newPassword: fields.get("newPassword"),
          confirmPassword: fields.get("confirmPassword"),
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
