/** Bind PAGE-119 commands to the current owner-bound action facade. */
/** 绑定 PAGE-119 操作入口；权限和租户所有权仍由后端及状态机校验。 */
export function bindPartnerPortalInteractions(documentApi = globalThis.document, actions = {}) {
  if (!documentApi?.querySelector) return false;
  bindClick(documentApi, "[data-partner-refresh]", actions.onRefresh);
  bindForm(documentApi, "[data-partner-setup-form]", (form) => actions.onSetup?.(formValues(form)));
  bindForm(documentApi, "[data-partner-branding-form]", (form) => {
    const values = formValues(form);
    return actions.onBranding?.({
      portal_name: values.portal_name,
      brand_primary_color: values.brand_primary_color,
      support_text: values.support_text,
    }, Number(values.revision));
  });
  bindForm(documentApi, "[data-partner-customer-filters]", (form) => {
    const values = formValues(form);
    return actions.onCustomerFilter?.({ ...values, limit: 50, offset: 0 });
  });
  bindForm(documentApi, "[data-partner-commission-filters]", (form) => {
    const values = formValues(form);
    return actions.onCommissionFilter?.({ ...values, limit: 50, offset: 0 });
  });
  bindClick(documentApi, "[data-partner-customer-create]", () => actions.onOpenCustomer?.("create", ""));
  documentApi.querySelectorAll?.("[data-partner-customer-rename]").forEach((button) => {
    button.addEventListener("click", () => actions.onOpenCustomer?.("rename", button.dataset.partnerCustomerRename), { once: true });
  });
  documentApi.querySelectorAll?.("[data-partner-customer-disable]").forEach((button) => {
    button.addEventListener("click", () => actions.onOpenCustomer?.("disable", button.dataset.partnerCustomerDisable), { once: true });
  });
  bindClick(documentApi, "[data-partner-dialog-close]", actions.onCloseDialog);
  const dialog = documentApi.querySelector(".partner-portal-dialog");
  if (dialog) {
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      void actions.onCloseDialog?.();
    }, { once: true });
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    else if (!dialog.open) dialog.setAttribute?.("open", "");
  }
  bindForm(documentApi, "[data-partner-customer-create-form]", (form) => actions.onCreateCustomer?.(formValues(form)));
  bindForm(documentApi, "[data-partner-customer-update-form]", (form) => {
    const kind = form.dataset.kind;
    const input = kind === "disable" ? { status: "disabled" } : { display_name: formValues(form).display_name };
    return actions.onUpdateCustomer?.(kind, form.dataset.customerId, Number(form.dataset.revision), input);
  });
  bindClick(documentApi, "[data-partner-commission-export]", actions.onExport);
  bindClick(documentApi, "[data-partner-delivery-download]", actions.onDelivery);
  return Boolean(documentApi.querySelector("[data-partner-portal-root], [data-partner-setup-form]"));
}

/** 清理伙伴门户 DOM，防止撤权或切租户后残留数据。 */
export function clearPartnerPortalDom(documentApi = globalThis.document) {
  const panel = documentApi?.querySelector?.(".partner-portal-panel");
  panel?.querySelectorAll?.("input, textarea, select").forEach((control) => {
    if (control.type !== "hidden") control.value = "";
  });
  panel?.querySelector?.("dialog")?.close?.();
  panel?.replaceChildren?.();
}

/** 清理白标 CSS 变量，避免跨租户品牌残留。 */
export function clearPartnerPortalBranding(documentApi = globalThis.document) {
  documentApi?.documentElement?.style?.removeProperty?.("--partner-brand-primary");
}

function bindClick(documentApi, selector, handler) {
  const element = documentApi.querySelector(selector);
  if (!element || typeof handler !== "function") return;
  element.addEventListener("click", (event) => {
    event.preventDefault();
    void handler();
  }, { once: true });
}

function bindForm(documentApi, selector, handler) {
  const form = documentApi.querySelector(selector);
  if (!form || typeof handler !== "function") return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form.reportValidity?.()) return;
    void handler(form);
  }, { once: true });
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}
