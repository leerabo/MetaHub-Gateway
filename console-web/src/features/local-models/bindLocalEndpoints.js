/** PAGE-116 DOM binding, one-use credential cleanup, and exact form parsing. */

import { normalizeLocalEndpointCreateRequest } from "../../lib/localEndpoints.js";

/** Remove URL/token values before replacing a stale authorization owner. */
export function clearLocalEndpointDom(documentRef = globalThis.document) {
  const root = documentRef?.querySelector?.(".local-models-panel");
  if (!root) return;
  eraseSensitiveInputs(root);
  root.replaceChildren?.();
}

/** Bind filters, create/test commands, and the current native Dialog. */
export function bindLocalEndpointInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-local-endpoint-root]");
  if (!root) return false;
  root.querySelector("[data-local-endpoint-refresh]")?.addEventListener("click", () => {
    void callbacks.onRefresh?.();
  });
  root.querySelector("[data-local-endpoint-open-create]")?.addEventListener("click", () => {
    void callbacks.onOpenCreate?.();
  });
  root.querySelector("[data-local-endpoint-filters]")?.addEventListener("change", (event) => {
    const form = event.currentTarget;
    const data = new FormData(form);
    void callbacks.onFilters?.({
      engine: String(data.get("engine") ?? ""),
      status: String(data.get("status") ?? ""),
    });
  });
  root.querySelectorAll("[data-local-endpoint-test]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onTest?.(button.dataset.localEndpointTest ?? "");
    });
  });
  bindDialog(root, callbacks);
  return true;
}

function bindDialog(root, callbacks) {
  const dialog = root.querySelector("[data-local-endpoint-dialog]");
  if (!dialog) return;
  const restore = "[data-local-endpoint-open-create]";
  const requestClose = async () => {
    eraseSensitiveInputs(dialog);
    const closed = await callbacks.onCloseCreate?.();
    if (closed !== false) globalThis.document?.querySelector?.(restore)?.focus?.();
  };
  dialog.querySelectorAll("[data-local-endpoint-close]").forEach((button) => {
    button.addEventListener("click", () => void requestClose());
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (dialog.getAttribute("aria-busy") !== "true") void requestClose();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Tab") trapLocalEndpointDialogFocus(dialog, event);
  });
  bindCreateForm(dialog, callbacks, restore);
  if (!dialog.open) dialog.showModal?.();
  dialog.querySelector?.('input[name="name"]')?.focus?.();
}

function bindCreateForm(dialog, callbacks, restoreSelector) {
  const form = dialog.querySelector("[data-local-endpoint-create-form]");
  const tokenField = form?.querySelector?.("[data-local-endpoint-token-field]");
  const tokenInput = form?.elements?.namedItem?.("bearer_token");
  const syncAuth = () => {
    const authType = form?.querySelector?.('input[name="auth_type"]:checked')?.value ?? "none";
    const bearer = authType === "bearer";
    if (tokenField) tokenField.hidden = !bearer;
    if (tokenInput) {
      tokenInput.required = bearer;
      if (!bearer) tokenInput.value = "";
    }
  };
  form?.querySelectorAll?.('input[name="auth_type"]').forEach((input) => {
    input.addEventListener("change", syncAuth);
  });
  syncAuth();
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    let request;
    try {
      request = localEndpointCreateFromFormData(new FormData(form));
      setFormError(dialog, "");
    } catch {
      setFormError(dialog, "请检查 Endpoint、认证和初始模型字段");
      return;
    }
    // fetch construction consumes the request synchronously; erase both DOM and local object before it settles.
    eraseSensitiveInputs(form);
    const pending = callbacks.onSubmitCreate?.(request);
    request.base_url = "";
    if (Object.hasOwn(request, "bearer_token")) request.bearer_token = "";
    void Promise.resolve(pending).then((nextState) => {
      if (!nextState?.dialog?.open) {
        globalThis.document?.querySelector?.(restoreSelector)?.focus?.();
      }
    });
  });
}

/** Parse only the closed POST /local-endpoints body. */
export function localEndpointCreateFromFormData(formData) {
  const authType = String(formData.get("auth_type") ?? "none");
  const capabilityValues = formData.getAll("capabilities").map(String);
  if (capabilityValues.length !== new Set(capabilityValues).size) {
    throw new TypeError("duplicate capability");
  }
  const capabilities = Object.fromEntries(capabilityValues.map((value) => [value, true]));
  if (!Object.keys(capabilities).length) Object.assign(capabilities, { chat: true, stream: true });
  const providerModelId = String(formData.get("provider_model_id") ?? "").trim();
  const internalModelId = String(formData.get("internal_model_id") ?? "").trim()
    || providerModelId.toLowerCase().replace(/[^a-z0-9._-]/gu, "-").replace(/^[^a-z0-9]+/u, "").slice(0, 160);
  const candidate = {
    provider_code: String(formData.get("provider_code") ?? ""),
    name: String(formData.get("name") ?? ""),
    engine_kind: String(formData.get("engine_kind") ?? ""),
    base_url: String(formData.get("base_url") ?? ""),
    auth_type: authType,
    ...(authType === "bearer" ? { bearer_token: String(formData.get("bearer_token") ?? "") } : {}),
    initial_model: {
      internal_model_id: internalModelId,
      provider_model_id: providerModelId,
      display_name: String(formData.get("display_name") ?? ""),
      capabilities,
      context_window: strictPositiveInteger(formData.get("context_window")),
      input_price_per_1k: formData.has("input_price_per_million")
        ? strictPrice(formData.get("input_price_per_million"))
        : strictPrice(formData.get("input_price_per_1k")),
      output_price_per_1k: formData.has("output_price_per_million")
        ? strictPrice(formData.get("output_price_per_million"))
        : strictPrice(formData.get("output_price_per_1k")),
      cache_price_per_1k: formData.has("cache_price_per_million")
        ? strictPrice(formData.get("cache_price_per_million"))
        : strictPrice(
          formData.get("cache_price_per_1k")
          || (formData.has("input_price_per_million")
            ? formData.get("input_price_per_million")
            : formData.get("input_price_per_1k")),
        ),
      currency: String(formData.get("currency") ?? ""),
    },
  };
  const normalized = normalizeLocalEndpointCreateRequest(candidate);
  return {
    ...normalized,
    initial_model: { ...normalized.initial_model, capabilities: { ...normalized.initial_model.capabilities } },
  };
}

/** Keep Tab and Shift+Tab inside the current create modal. */
export function trapLocalEndpointDialogFocus(dialog, event, documentRef = globalThis.document) {
  const controls = [...dialog.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
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

function eraseSensitiveInputs(root) {
  for (const input of root?.querySelectorAll?.(
    'input[name="base_url"], input[name="bearer_token"]',
  ) ?? []) input.value = "";
}

function strictPositiveInteger(value) {
  const text = String(value ?? "");
  if (!/^[1-9][0-9]*$/.test(text)) return 0;
  const number = Number(text);
  return Number.isSafeInteger(number) ? number : 0;
}

function strictPrice(value) {
  const text = String(value ?? "");
  if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$/.test(text)) return Number.NaN;
  return Number(text);
}

function setFormError(root, value) {
  const node = root.querySelector?.("[data-local-endpoint-form-error]");
  if (node) node.textContent = value;
}
