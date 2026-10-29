/** PAGE-114 DOM bindings, exact form parsing and native Dialog focus handling. */

import {
  normalizeMcpNetworkPolicy,
  normalizeMcpToolPolicy,
  normalizeServerRegistration,
} from "../../lib/mcpTools.js";

/** Erase credential inputs and all page facts before invalidating an old owner. */
export function clearMcpToolDom(documentRef = globalThis.document) {
  const root = documentRef?.querySelector?.(".mcp-tools-panel");
  if (!root) return;
  for (const input of root.querySelectorAll?.(
    'input[name="endpoint_url"], input[name="bearer_token"], textarea',
  ) ?? []) {
    input.value = "";
  }
  root.replaceChildren?.();
}

/** Bind tabs, list actions and the current native modal. */
export function bindMcpToolInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-mcp-root]");
  if (!root) return false;

  root.querySelector("[data-mcp-refresh]")?.addEventListener("click", () => {
    void callbacks.onRefresh?.();
  });
  root.querySelector("[data-mcp-open-register]")?.addEventListener("click", () => {
    void callbacks.onOpenRegister?.();
  });
  root.querySelectorAll("[data-mcp-tab]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onTab?.(button.dataset.mcpTab ?? ""));
    button.addEventListener("keydown", (event) => handleTabKey(root, button, event, callbacks));
  });
  root.querySelector("[data-mcp-previous-page]")?.addEventListener("click", () => {
    void callbacks.onPreviousPage?.();
  });
  root.querySelector("[data-mcp-next-page]")?.addEventListener("click", () => {
    void callbacks.onNextPage?.();
  });
  root.querySelectorAll("[data-mcp-refresh-server]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onRefreshServer?.(button.dataset.mcpRefreshServer ?? "");
    });
  });
  root.querySelectorAll("[data-mcp-server-status]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onServerStatus?.(button.dataset.mcpServerStatus ?? "");
    });
  });
  root.querySelectorAll("[data-mcp-tool-detail]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onOpenTool?.(button.dataset.mcpToolDetail ?? "", "detail");
    });
  });
  root.querySelectorAll("[data-mcp-tool-policy]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onOpenTool?.(button.dataset.mcpToolPolicy ?? "", "policy");
    });
  });
  root.querySelectorAll("[data-mcp-network-policy]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onOpenNetworkPolicy?.(button.dataset.mcpNetworkPolicy ?? "");
    });
  });

  bindDialog(root, callbacks);
  return true;
}

function bindDialog(root, callbacks) {
  const dialog = root.querySelector("[data-mcp-dialog]");
  if (!dialog) return;
  const kind = dialog.dataset.mcpDialog ?? "";
  const restoreSelector = kind === "register"
    ? "[data-mcp-open-register]"
    : kind === "network"
      ? `[data-mcp-network-policy="${cssEscape(callbacks.networkServerId?.() ?? "")}"]`
    : kind === "policy"
      ? `[data-mcp-tool-policy="${cssEscape(callbacks.toolId?.() ?? "")}"]`
      : `[data-mcp-tool-detail="${cssEscape(callbacks.toolId?.() ?? "")}"]`;
  const requestClose = async () => {
    const closed = await callbacks.onCloseDialog?.();
    if (closed !== false) globalThis.document?.querySelector?.(restoreSelector)?.focus?.();
  };
  dialog.querySelectorAll("[data-mcp-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => void requestClose());
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (dialog.getAttribute("aria-busy") !== "true") void requestClose();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Tab") trapMcpDialogFocus(dialog, event);
  });

  if (kind === "register") bindRegisterForm(dialog, callbacks, restoreSelector);
  if (kind === "policy") bindPolicyForm(dialog, callbacks, restoreSelector);
  if (kind === "network") bindNetworkPolicyForm(dialog, callbacks, restoreSelector);
  showModal(dialog);
}

function bindRegisterForm(dialog, callbacks, restoreSelector) {
  const form = dialog.querySelector("[data-mcp-register-form]");
  const authType = form?.elements?.namedItem?.("auth_type");
  const bearerField = form?.querySelector?.("[data-mcp-bearer-field]");
  const bearerInput = form?.elements?.namedItem?.("bearer_token");
  const syncAuth = () => {
    const bearer = authType?.value === "bearer";
    if (bearerField) bearerField.hidden = !bearer;
    if (bearerInput) {
      bearerInput.required = bearer;
      if (!bearer) bearerInput.value = "";
    }
  };
  authType?.addEventListener?.("change", syncAuth);
  syncAuth();
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const request = mcpServerRegistrationFromFormData(new FormData(form));
      clearFormError(dialog);
      // Endpoint and Token leave the DOM before the asynchronous API result can settle or trigger a redraw.
      eraseRegistrationInputs(form);
      const pending = callbacks.onSubmitRegister?.(request);
      request.endpointUrl = "";
      request.bearerToken = "";
      void Promise.resolve(pending).then((nextState) => {
        if (!nextState?.registerDialog?.open) {
          globalThis.document?.querySelector?.(restoreSelector)?.focus?.();
        }
      });
    } catch {
      showFormError(dialog, "Server 注册字段不合法");
    }
  });
}

function bindPolicyForm(dialog, callbacks, restoreSelector) {
  const form = dialog.querySelector("[data-mcp-policy-form]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const policy = mcpToolPolicyFromFormData(new FormData(form));
      clearFormError(dialog);
      void Promise.resolve(callbacks.onSubmitPolicy?.(policy)).then((nextState) => {
        if (!nextState?.toolDialog?.open) {
          globalThis.document?.querySelector?.(restoreSelector)?.focus?.();
        }
      });
    } catch {
      showFormError(dialog, "Tool 策略字段不合法");
    }
  });
}

function bindNetworkPolicyForm(dialog, callbacks, restoreSelector) {
  const form = dialog.querySelector("[data-mcp-network-form]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const policy = mcpNetworkPolicyFromFormData(new FormData(form));
      clearFormError(dialog);
      void Promise.resolve(callbacks.onSubmitNetworkPolicy?.(policy)).then((nextState) => {
        if (!nextState?.networkDialog?.open) {
          globalThis.document?.querySelector?.(restoreSelector)?.focus?.();
        }
      });
    } catch {
      showFormError(dialog, "网络安全策略字段不合法");
    }
  });
}

/** Parse only the closed registration body. Returned secrets must be consumed synchronously. */
export function mcpServerRegistrationFromFormData(formData) {
  const authType = String(formData.get("auth_type") ?? "none");
  const normalized = normalizeServerRegistration({
    name: String(formData.get("name") ?? ""),
    endpointUrl: String(formData.get("endpoint_url") ?? ""),
    authType,
    bearerToken: authType === "bearer" ? String(formData.get("bearer_token") ?? "") : "",
  });
  return {
    name: normalized.name,
    endpointUrl: normalized.endpoint_url,
    authType: normalized.auth.type,
    bearerToken: normalized.auth.bearer_token ?? "",
  };
}

/** Parse newline sets and delegate all cross-field policy checks to the API contract helper. */
export function mcpToolPolicyFromFormData(formData) {
  return normalizeMcpToolPolicy({
    expected_policy_revision: strictInteger(formData.get("expected_policy_revision")),
    status: String(formData.get("status") ?? ""),
    risk_level: String(formData.get("risk_level") ?? ""),
    approval_required: formData.get("approval_required") === "on",
    approval_expires_seconds: strictInteger(formData.get("approval_expires_seconds")),
    required_permission_codes: lineSet(formData.get("required_permission_codes")),
    allowed_role_ids: lineSet(formData.get("allowed_role_ids")),
    allowed_user_ids: lineSet(formData.get("allowed_user_ids")),
    result_redaction_pointers: lineSet(formData.get("result_redaction_pointers"), { trim: false }),
  });
}

/** Parse the fixed endpoint-only network policy form. */
export function mcpNetworkPolicyFromFormData(formData) {
  return normalizeMcpNetworkPolicy({
    expected_revision: strictInteger(formData.get("expected_revision")),
    status: String(formData.get("status") ?? ""),
    egress_mode: String(formData.get("egress_mode") ?? ""),
    connect_timeout_seconds: strictInteger(formData.get("connect_timeout_seconds")),
    probe_timeout_seconds: strictInteger(formData.get("probe_timeout_seconds")),
    call_timeout_seconds: strictInteger(formData.get("call_timeout_seconds")),
    failure_threshold: strictInteger(formData.get("failure_threshold")),
    open_seconds: strictInteger(formData.get("open_seconds")),
  });
}

/** Keep Tab and Shift+Tab inside the current modal. */
export function trapMcpDialogFocus(dialog, event, documentRef = globalThis.document) {
  const controls = [...dialog.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((element) => element.offsetParent !== null || typeof element.offsetParent === "undefined");
  if (!controls.length) return;
  const first = controls[0];
  const last = controls.at(-1);
  if (event.shiftKey && documentRef?.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && documentRef?.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function handleTabKey(root, button, event, callbacks) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...root.querySelectorAll("[data-mcp-tab]")];
  const index = tabs.indexOf(button);
  if (index < 0 || !tabs.length) return;
  event.preventDefault();
  const nextIndex = event.key === "Home"
    ? 0
    : event.key === "End"
      ? tabs.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  const next = tabs[nextIndex];
  next.focus();
  void callbacks.onTab?.(next.dataset.mcpTab ?? "");
}

function eraseRegistrationInputs(form) {
  for (const name of ["endpoint_url", "bearer_token"]) {
    const input = form?.elements?.namedItem?.(name);
    if (input && "value" in input) input.value = "";
  }
}

function lineSet(value, { trim = true } = {}) {
  const values = String(value ?? "").split(/\r?\n/u).map((item) => trim ? item.trim() : item).filter(Boolean);
  if (new Set(values).size !== values.length) throw new TypeError("duplicate value");
  return values;
}

function strictInteger(value) {
  const text = String(value ?? "");
  if (!/^[1-9][0-9]*$/.test(text)) return 0;
  const number = Number(text);
  return Number.isSafeInteger(number) ? number : 0;
}

function showModal(dialog) {
  if (!dialog.open) dialog.showModal?.();
  dialog.querySelector?.("button:not([disabled]), input:not([disabled])")?.focus?.();
}

function showFormError(root, message) {
  const node = root.querySelector?.("[data-mcp-form-error]");
  if (node) node.textContent = message;
}

function clearFormError(root) {
  showFormError(root, "");
}

function cssEscape(value) {
  return globalThis.CSS?.escape?.(String(value ?? "")) ?? String(value ?? "").replaceAll('"', '\\"');
}
