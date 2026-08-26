/** PRD-120 Dialog binding and synchronous one-use DOM cleanup. */

export function clearPrivateGatewayDom(documentRef = globalThis.document) {
  const root = documentRef?.querySelector?.(".private-gateways-panel");
  if (!root) return;
  eraseOneUse(root);
  root.replaceChildren?.();
}

export function bindPrivateGatewayInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-private-gateway-root]");
  if (!root) return false;
  root.querySelector("[data-private-gateway-refresh]")?.addEventListener("click", () => void callbacks.onRefresh?.());
  root.querySelector("[data-private-gateway-open]")?.addEventListener("click", () => void callbacks.onOpen?.());
  root.querySelectorAll("[data-private-gateway-revoke]").forEach((button) => {
    button.addEventListener("click", () => {
      const deploymentId = button.dataset.privateGatewayRevoke ?? "";
      const reason = root.querySelector(`[data-private-gateway-reason="${cssEscape(deploymentId)}"]`)?.value ?? "owner_requested";
      void callbacks.onRevoke?.(deploymentId, reason);
    });
  });
  bindDialog(root, callbacks);
  return true;
}

function bindDialog(root, callbacks) {
  const dialog = root.querySelector("[data-private-gateway-dialog]");
  if (!dialog) return;
  const close = async () => {
    eraseOneUse(dialog);
    const closed = await callbacks.onClose?.();
    if (closed !== false) globalThis.document?.querySelector?.("[data-private-gateway-open]")?.focus?.();
  };
  dialog.querySelectorAll("[data-private-gateway-close]").forEach((button) => button.addEventListener("click", () => void close()));
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); if (dialog.getAttribute("aria-busy") !== "true") void close(); });
  dialog.addEventListener("keydown", (event) => { if (event.key === "Tab") trapDialogFocus(dialog, event); });
  dialog.querySelector("[data-private-gateway-copy]")?.addEventListener("click", () => void callbacks.onCopy?.());
  const form = dialog.querySelector("[data-private-gateway-create]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    let input;
    try {
      const data = new FormData(form);
      input = {
        displayName: String(data.get("display_name") ?? "").trim(),
        regionCode: String(data.get("region_code") ?? "").trim().toLowerCase(),
        mode: String(data.get("mode") ?? "connected"),
        tokenTtlSeconds: strictInteger(data.get("token_ttl_seconds")),
      };
      if (input.displayName.length < 2 || input.displayName.length > 120 || !/^[a-z0-9][a-z0-9-]{1,39}$/u.test(input.regionCode) || !new Set(["connected", "isolated"]).has(input.mode) || input.tokenTtlSeconds < 60 || input.tokenTtlSeconds > 600) throw new TypeError("invalid");
      setFormError(dialog, "");
    } catch {
      setFormError(dialog, "请检查名称、区域和注册窗口");
      return;
    }
    void callbacks.onCreate?.(input);
  });
  if (!dialog.open) dialog.showModal?.();
  (dialog.querySelector('input[name="display_name"]') ?? dialog.querySelector("[data-private-gateway-copy]"))?.focus?.();
}

export function trapDialogFocus(dialog, event, documentRef = globalThis.document) {
  const controls = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter((item) => item.offsetParent !== null || typeof item.offsetParent === "undefined");
  if (!controls.length) return;
  const first = controls[0];
  const last = controls.at(-1);
  if (event.shiftKey && documentRef?.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && documentRef?.activeElement === last) { event.preventDefault(); first.focus(); }
}

function eraseOneUse(root) {
  for (const node of root?.querySelectorAll?.("[data-private-gateway-one-use] code") ?? []) node.textContent = "";
}
function strictInteger(value) { const text = String(value ?? ""); return /^[1-9][0-9]*$/u.test(text) && Number.isSafeInteger(Number(text)) ? Number(text) : 0; }
function setFormError(root, value) { const node = root.querySelector?.("[data-private-gateway-form-error]"); if (node) node.textContent = value; }
function cssEscape(value) { return globalThis.CSS?.escape?.(String(value)) ?? String(value).replace(/[^A-Za-z0-9_-]/gu, ""); }
