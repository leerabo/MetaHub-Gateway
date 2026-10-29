/** PAGE-112 DOM binding, exact form parsing and native Dialog focus behavior. */

import {
  normalizeBillingDisputeCreateRequest,
  normalizeBillingDisputeListFilters,
  normalizeBillingDisputeTransitionRequest,
} from "../../lib/billingDisputes.js";

const guardedDocuments = new WeakSet();

/** Clear list, detail, free text and Dialog DOM before aborting an old owner. */
export function clearBillingDisputeDom(documentRef = globalThis.document) {
  documentRef?.querySelector?.(".billing-disputes-panel")?.replaceChildren?.();
}

/** Private reason text is injected with textContent only after an explicit reveal. */
export function hydrateBillingDisputePrivateText(
  documentRef = globalThis.document,
  values = {},
) {
  const mapping = { reason: values.reason ?? "", decisionReason: values.decisionReason ?? "" };
  for (const [kind, value] of Object.entries(mapping)) {
    const node = documentRef?.querySelector?.(`[data-billing-dispute-private-text="${kind}"]`);
    if (node) node.textContent = String(value);
  }
}

/** Bind filters, detail selection, explicit reveals and all write Dialog commands. */
export function bindBillingDisputeInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-billing-disputes-root]");
  if (!root) return false;

  root.querySelector("[data-billing-dispute-refresh]")?.addEventListener("click", () => {
    void callbacks.onRefresh?.();
  });
  root.querySelector("[data-billing-dispute-open-create]")?.addEventListener("click", () => {
    void callbacks.onOpenCreate?.();
  });
  root.querySelectorAll("[data-billing-dispute-select]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onSelectDetail?.(button.dataset.billingDisputeSelect ?? "");
    });
  });
  root.querySelector("[data-billing-dispute-close-detail]")?.addEventListener("click", () => {
    void callbacks.onCloseDetail?.();
  });
  root.querySelectorAll("[data-billing-dispute-open-action]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onOpenAction?.(button.dataset.billingDisputeOpenAction ?? "");
    });
  });
  root.querySelectorAll("[data-billing-dispute-reveal]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onReveal?.(button.dataset.billingDisputeReveal ?? "");
    });
  });
  root.querySelectorAll("[data-billing-dispute-collapse]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onCollapse?.(button.dataset.billingDisputeCollapse ?? "");
    });
  });

  bindFilters(root, callbacks);
  bindPagination(root, callbacks);
  bindDialog(root, callbacks);
  return true;
}

function bindFilters(root, callbacks) {
  const form = root.querySelector("[data-billing-dispute-filters]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const filters = billingDisputeFiltersFromFormData(new FormData(form));
      clearLocalValidation(root);
      void callbacks.onFilters?.(filters);
    } catch {
      showLocalValidation(root, "筛选条件不合法");
    }
  });
  root.querySelector("[data-billing-dispute-filter-reset]")?.addEventListener("click", (event) => {
    event.preventDefault();
    form?.reset?.();
    void callbacks.onFilters?.(normalizeBillingDisputeListFilters());
  });
}

function bindPagination(root, callbacks) {
  root.querySelector("[data-billing-dispute-next]")?.addEventListener("click", () => {
    void callbacks.onNextPage?.();
  });
  root.querySelector("[data-billing-dispute-first]")?.addEventListener("click", () => {
    void callbacks.onFirstPage?.();
  });
}

function bindDialog(root, callbacks) {
  const dialog = root.querySelector("[data-billing-dispute-dialog]");
  if (!dialog) return;
  installBillingDisputeBusyEscapeGuard(dialog.ownerDocument ?? globalThis.document);
  const returnSelector = dialog.dataset.dialogKind === "create"
    ? "[data-billing-dispute-open-create]"
    : `[data-billing-dispute-open-action="${dialog.dataset.action ?? ""}"]`;
  const close = async () => {
    const closed = await callbacks.onCloseDialog?.();
    if (closed !== false) globalThis.document?.querySelector?.(returnSelector)?.focus?.();
  };
  dialog.querySelectorAll("[data-billing-dispute-dialog-close], [data-billing-dispute-dialog-cancel]")
    .forEach((button) => button.addEventListener("click", () => void close()));
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    void close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Tab") trapBillingDisputeDialogFocus(dialog, event);
  });

  const createForm = dialog.querySelector("[data-billing-dispute-create-form]");
  if (createForm) {
    createForm.querySelectorAll('input[name="scope_type"]').forEach((input) => {
      input.addEventListener("change", () => updateCreateScope(createForm));
    });
    createForm.addEventListener("input", () => updateCreateScopeSummary(createForm));
    updateCreateScope(createForm);
    dialog.querySelector("[data-billing-dispute-submit-create]")?.addEventListener("click", () => {
      try {
        const request = billingDisputeCreateRequestFromFormData(new FormData(createForm));
        clearLocalValidation(dialog);
        void callbacks.onSubmitCreate?.(request);
      } catch {
        showLocalValidation(dialog, "争议申请字段不合法");
      }
    });
  }

  const actionForm = dialog.querySelector("[data-billing-dispute-action-form]");
  dialog.querySelector("[data-billing-dispute-submit-action]")?.addEventListener("click", () => {
    try {
      const request = billingDisputeTransitionRequestFromFormData(new FormData(actionForm));
      clearLocalValidation(dialog);
      void callbacks.onSubmitAction?.(request);
    } catch {
      showLocalValidation(dialog, "争议处理字段不合法");
    }
  });
  dialog.querySelector("[data-billing-dispute-retry]")?.addEventListener("click", () => {
    void callbacks.onRetry?.();
  });
  showModal(dialog);
}

/** Convert filters to canonical UTC without adding undeclared query fields. */
export function billingDisputeFiltersFromFormData(formData) {
  const createdFromInput = String(formData.get("created_from") ?? "").trim();
  const createdToInput = String(formData.get("created_to") ?? "").trim();
  return normalizeBillingDisputeListFilters({
    status: String(formData.get("status") ?? "").trim(),
    scope_type: String(formData.get("scope_type") ?? "").trim(),
    created_from: createdFromInput ? billingDisputeDateTimeLocalToUtc(createdFromInput) : "",
    created_to: createdToInput ? billingDisputeDateTimeLocalToUtc(createdToInput) : "",
    limit: 50,
    cursor: null,
  });
}

/** Build one of the two exact POST bodies. */
export function billingDisputeCreateRequestFromFormData(formData) {
  const scopeType = String(formData.get("scope_type") ?? "");
  const reason = String(formData.get("reason") ?? "");
  if (scopeType === "request_ids") {
    const requestIds = String(formData.get("request_ids") ?? "")
      .split(/[\r\n,]+/u)
      .map((value) => value.trim())
      .filter(Boolean);
    return normalizeBillingDisputeCreateRequest({ scope_type: scopeType, request_ids: requestIds, reason });
  }
  return normalizeBillingDisputeCreateRequest({
    scope_type: scopeType,
    period_start: billingDisputeDateTimeLocalToUtc(formData.get("period_start")),
    period_end: billingDisputeDateTimeLocalToUtc(formData.get("period_end")),
    reason,
  });
}

/** Build an exact action-specific PATCH body; blank approval amounts are omitted. */
export function billingDisputeTransitionRequestFromFormData(formData) {
  const action = String(formData.get("action") ?? "");
  const expectedRevision = strictPositiveInteger(formData.get("expected_revision"));
  if (action === "start_review") {
    return normalizeBillingDisputeTransitionRequest({ action, expected_revision: expectedRevision });
  }
  const decisionReason = String(formData.get("decision_reason") ?? "");
  if (action !== "approve") {
    return normalizeBillingDisputeTransitionRequest({
      action,
      expected_revision: expectedRevision,
      decision_reason: decisionReason,
    });
  }
  const adjustments = [];
  for (const [name, value] of formData.entries()) {
    if (!String(name).startsWith("adjustment:") || !String(value).trim()) continue;
    adjustments.push({
      billing_dispute_item_id: String(name).slice("adjustment:".length),
      amount: canonicalBillingDisputeAmount(value),
    });
  }
  return normalizeBillingDisputeTransitionRequest({
    action,
    expected_revision: expectedRevision,
    decision_reason: decisionReason,
    adjustments,
  });
}

/** Canonicalize a positive decimal to six places without floating-point conversion. */
export function canonicalBillingDisputeAmount(value) {
  const match = /^(0|[1-9][0-9]*)(?:\.([0-9]{1,6}))?$/.exec(String(value ?? "").trim());
  if (!match) return "";
  const result = `${match[1]}.${String(match[2] ?? "").padEnd(6, "0")}`;
  return result === "0.000000" ? "" : result;
}

/** Strict local datetime to canonical UTC seconds. */
export function billingDisputeDateTimeLocalToUtc(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(String(value ?? ""));
  if (!match) return "";
  const [, year, month, day, hour, minute, second = "00"] = match;
  const date = new Date(
    Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second), 0,
  );
  if (
    date.getFullYear() !== Number(year)
    || date.getMonth() !== Number(month) - 1
    || date.getDate() !== Number(day)
    || date.getHours() !== Number(hour)
    || date.getMinutes() !== Number(minute)
    || date.getSeconds() !== Number(second)
  ) return "";
  return date.toISOString().replace(".000Z", "Z");
}

/** Wrap Tab and Shift+Tab inside the current native modal. */
export function trapBillingDisputeDialogFocus(dialog, event, documentRef = globalThis.document) {
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

/** Keep busy Escape blocked even if a render replaces the dialog node. */
export function installBillingDisputeBusyEscapeGuard(documentRef = globalThis.document) {
  if (!documentRef || typeof documentRef.addEventListener !== "function" || guardedDocuments.has(documentRef)) {
    return false;
  }
  documentRef.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const dialog = documentRef.querySelector?.('[data-billing-dispute-dialog][aria-busy="true"]');
    if (dialog?.open === true) event.preventDefault();
  }, { capture: true });
  guardedDocuments.add(documentRef);
  return true;
}

function updateCreateScope(form) {
  const scope = String(new FormData(form).get("scope_type") ?? "request_ids");
  const requestSection = form.querySelector("[data-billing-dispute-request-scope]");
  const timeSection = form.querySelector("[data-billing-dispute-time-scope]");
  if (requestSection) requestSection.hidden = scope !== "request_ids";
  if (timeSection) timeSection.hidden = scope !== "time_range";
  updateCreateScopeSummary(form);
}

function updateCreateScopeSummary(form) {
  const output = form.querySelector("[data-billing-dispute-scope-summary]");
  if (!output) return;
  const data = new FormData(form);
  const scope = String(data.get("scope_type") ?? "request_ids");
  if (scope === "request_ids") {
    const count = String(data.get("request_ids") ?? "").split(/[\r\n,]+/u).map((value) => value.trim()).filter(Boolean).length;
    output.textContent = count ? `将冻结 ${count} 个 Request ID` : "范围待确认";
    return;
  }
  const start = String(data.get("period_start") ?? "");
  const end = String(data.get("period_end") ?? "");
  output.textContent = start && end ? `将冻结 ${start} 至 ${end}` : "范围待确认";
}

function showModal(dialog) {
  if (!dialog.open && typeof dialog.showModal === "function") dialog.showModal();
  dialog.querySelector?.('input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled])')?.focus?.();
}

function showLocalValidation(root, message) {
  const live = root.querySelector?.(".billing-dispute-live");
  if (!live) return;
  live.textContent = message;
  live.classList.add("is-error");
}

function clearLocalValidation(root) {
  const live = root.querySelector?.(".billing-dispute-live");
  if (!live) return;
  live.textContent = "";
  live.classList.remove("is-error");
}

function strictPositiveInteger(value) {
  const text = String(value ?? "");
  if (!/^[1-9][0-9]*$/.test(text)) return Number.NaN;
  const result = Number(text);
  return Number.isSafeInteger(result) ? result : Number.NaN;
}
