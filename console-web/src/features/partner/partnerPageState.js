import { PartnerApiError } from "../../lib/partnerChannel.js";
import { renderPartnerPage } from "./renderPartnerPage.js";

export function createPartnerPageController({ root, api } = {}) {
  if (!root || typeof api?.workspaces !== "function") {
    throw new TypeError("partner page requires root and Partner API");
  }
  let generation = 0;
  let request = null;
  const state = {
    phase: "loading",
    response: null,
    offset: 0,
    stale: false,
    error: null,
  };

  const controller = {
    mount() {
      bind();
      render();
      return load();
    },
    refresh() {
      return load({ preserve: true });
    },
    next() {
      if (!state.response || state.offset + state.response.limit >= state.response.total) return false;
      state.offset += state.response.limit;
      return load();
    },
    previous() {
      if (!state.response || state.offset === 0) return false;
      state.offset = Math.max(0, state.offset - state.response.limit);
      return load();
    },
    destroy() {
      generation += 1;
      request?.abort();
      request = null;
      state.response = null;
      state.error = null;
      state.stale = false;
      state.phase = "destroyed";
      root.replaceChildren();
    },
    snapshot() {
      return Object.freeze({ ...state });
    },
  };

  async function load({ preserve = false } = {}) {
    const ticket = begin();
    const previous = preserve ? state.response : null;
    state.phase = "loading";
    state.stale = false;
    state.error = null;
    if (!preserve) state.response = null;
    render();
    try {
      const response = await api.workspaces({ limit: 50, offset: state.offset, signal: ticket.signal });
      if (!current(ticket)) return false;
      state.response = response;
      state.phase = response.items.length ? "ready" : "empty";
      state.stale = false;
      state.error = null;
      render();
      return true;
    } catch (error) {
      if (!current(ticket) || error?.name === "AbortError") return false;
      if (isIdentityFailure(error)) {
        state.response = null;
        state.offset = 0;
        state.stale = false;
        state.phase = error.code === "partner.membership_required" ? "revoked" : "session";
      } else if (previous) {
        state.response = previous;
        state.stale = true;
        state.phase = previous.items.length ? "ready" : "empty";
      } else {
        state.response = null;
        state.phase = "unavailable";
      }
      state.error = safeError(error);
      render();
      return false;
    }
  }

  function bind() {
    root.addEventListener("click", (event) => {
      const action = event.target.closest("[data-partner-action]")?.dataset.partnerAction;
      if (action === "refresh") controller.refresh();
      if (action === "next") controller.next();
      if (action === "previous") controller.previous();
    });
  }

  function begin() {
    generation += 1;
    request?.abort();
    request = new AbortController();
    return { generation, signal: request.signal };
  }

  function current(ticket) {
    return ticket.generation === generation && !ticket.signal.aborted;
  }

  function render() {
    renderPartnerPage(root, state);
  }

  return controller;
}

function isIdentityFailure(error) {
  return error instanceof PartnerApiError && (
    [401, 403].includes(error.status)
    || new Set([
      "partner.session_required", "partner.session_invalid", "partner.credential_conflict",
      "partner.membership_required",
    ]).has(error.code)
  );
}

function safeError(error) {
  return Object.freeze({
    code: typeof error?.code === "string" ? error.code.slice(0, 100) : "partner.unavailable",
    message: typeof error?.message === "string"
      ? error.message.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 180)
      : "Partner service is unavailable",
    requestId: typeof error?.requestId === "string" ? error.requestId.slice(0, 160) : "-",
  });
}
