/** PRD-120..123 registration, telemetry, and local Secret binding. */

export function clearPrivateGatewayDom(documentRef = globalThis.document) {
  const root = documentRef?.querySelector?.(".private-gateways-panel");
  if (!root) return;
  eraseOneUse(root);
  eraseSensitive(root);
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
  root.querySelectorAll("[data-private-gateway-state-open]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onStateOpen?.(
      button.dataset.deploymentId ?? "",
      button.dataset.privateGatewayStateOpen ?? "",
    ));
  });
  root.querySelectorAll("[data-private-gateway-policy]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onTelemetryPolicy?.(
      button.dataset.deploymentId ?? "",
      button.dataset.level ?? "",
    ));
  });
  root.querySelectorAll("[data-private-gateway-secret-open]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onSecretOpen?.(
      button.dataset.deploymentId ?? "",
      button.dataset.privateGatewaySecretOpen ?? "",
      button.dataset.referenceId ?? "",
    ));
  });
  root.querySelectorAll("[data-private-gateway-secret-command]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onSecretCommand?.({
      action: button.dataset.privateGatewaySecretCommand ?? "",
      deploymentId: button.dataset.deploymentId ?? "",
      referenceId: button.dataset.referenceId ?? "",
    }));
  });
  bindDialog(root, callbacks);
  bindStateDialog(root, callbacks);
  bindSecretDialog(root, callbacks);
  return true;
}

function bindSecretDialog(root, callbacks) {
  const dialog = root.querySelector("[data-private-gateway-secret-dialog]");
  if (!dialog) return;
  const close = async () => {
    eraseSensitive(dialog);
    const closed = await callbacks.onSecretClose?.();
    if (closed !== false) {
      globalThis.document?.querySelector?.("[data-private-gateway-secret-open]")?.focus?.();
    }
  };
  dialog.querySelectorAll("[data-private-gateway-secret-close]").forEach((button) => {
    button.addEventListener("click", () => void close());
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    eraseSensitive(dialog);
    if (dialog.getAttribute("aria-busy") !== "true") void close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Tab") trapDialogFocus(dialog, event);
  });
  const form = dialog.querySelector("[data-private-gateway-secret-form]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    void submitSecretForm(form, dialog, callbacks);
  });
  if (!dialog.open) dialog.showModal?.();
  dialog.querySelector("input")?.focus?.();
}

async function submitSecretForm(form, dialog, callbacks) {
  const action = form.dataset.privateGatewaySecretForm ?? "";
  const deploymentId = dialog.dataset.deploymentId ?? "";
  const sensitive = form.querySelector("[data-private-gateway-sensitive]");
  const bytes = consumeSensitive(sensitive);
  try {
    const data = new FormData(form);
    if (action === "handoff") {
      const localApiOrigin = String(data.get("local_api_origin") ?? "").trim();
      if (!/^https?:\/\/[^/?#]+$/u.test(localApiOrigin) || bytes.length < 32
        || bytes.length > 512 || bytes.some((value) => value < 33 || value > 126)) {
        setSecretFormError(dialog, "请检查本地 Origin 和管理凭据");
        return;
      }
      setSecretFormError(dialog, "");
      await callbacks.onLocalSession?.({ deploymentId, localApiOrigin, handoffCredential: bytes });
      return;
    }
    if (!bytes.length || bytes.length > 65_536) {
      setSecretFormError(dialog, "Secret 长度无效");
      return;
    }
    if (action === "create") {
      const input = {
        action,
        deploymentId,
        providerConnectionId: String(data.get("provider_connection_id") ?? "").trim(),
        label: String(data.get("label") ?? "").trim(),
        kind: String(data.get("kind") ?? ""),
        secretBytes: bytes,
      };
      if (!/^[a-z0-9][a-z0-9_:-]{2,79}$/u.test(input.providerConnectionId)
        || !/^[A-Za-z0-9][A-Za-z0-9 ._:/()-]{0,79}$/u.test(input.label)
        || !["api_key", "bearer_token", "basic_password", "oauth_client_secret"].includes(input.kind)) {
        setSecretFormError(dialog, "请检查 Provider、标签和类型");
        return;
      }
      setSecretFormError(dialog, "");
      await callbacks.onSecretMutation?.(input);
      return;
    }
    setSecretFormError(dialog, "");
    await callbacks.onSecretMutation?.({
      action: "rotate",
      deploymentId,
      referenceId: form.dataset.referenceId ?? "",
      secretBytes: bytes,
    });
  } finally {
    bytes.fill(0);
    eraseSensitive(form);
  }
}

function bindStateDialog(root, callbacks) {
  const dialog = root.querySelector("[data-private-gateway-state-dialog]");
  if (!dialog) return;
  const close = async () => {
    const closed = await callbacks.onStateClose?.();
    if (closed !== false) {
      globalThis.document?.querySelector?.("[data-private-gateway-state-open]")?.focus?.();
    }
  };
  dialog.querySelectorAll("[data-private-gateway-state-close]").forEach((button) => {
    button.addEventListener("click", () => void close());
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (dialog.getAttribute("aria-busy") !== "true") void close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Tab") trapDialogFocus(dialog, event);
  });
  const form = dialog.querySelector("[data-private-gateway-state-form]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const action = form.dataset.action ?? "";
    if (data.get("confirmed") !== "on") {
      setStateFormError(dialog, "请确认本次操作");
      return;
    }
    const input = {
      action,
      sourceConfigSnapshotId: String(data.get("source_config_snapshot_id") ?? "").trim(),
      reasonCode: String(data.get("reason_code") ?? ""),
    };
    if (action === "publish" && !/^csn_[0-9a-f]{32}$/u.test(input.sourceConfigSnapshotId)) {
      setStateFormError(dialog, "请输入有效的配置快照 ID");
      return;
    }
    setStateFormError(dialog, "");
    void callbacks.onStateCommand?.(input);
  });
  if (!dialog.open) dialog.showModal?.();
  (dialog.querySelector("input:not([type=checkbox]), select, input[type=checkbox]"))?.focus?.();
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
function eraseSensitive(root) {
  for (const node of root?.querySelectorAll?.("[data-private-gateway-sensitive]") ?? []) {
    node.value = "";
  }
}
function consumeSensitive(input) {
  let value = String(input?.value ?? "");
  if (input) input.value = "";
  const bytes = new TextEncoder().encode(value);
  value = "";
  return bytes;
}
function strictInteger(value) { const text = String(value ?? ""); return /^[1-9][0-9]*$/u.test(text) && Number.isSafeInteger(Number(text)) ? Number(text) : 0; }
function setFormError(root, value) { const node = root.querySelector?.("[data-private-gateway-form-error]"); if (node) node.textContent = value; }
function setStateFormError(root, value) { const node = root.querySelector?.("[data-private-gateway-state-form-error]"); if (node) node.textContent = value; }
function setSecretFormError(root, value) { const node = root.querySelector?.("[data-private-gateway-secret-form-error]"); if (node) node.textContent = value; }
function cssEscape(value) { return globalThis.CSS?.escape?.(String(value)) ?? String(value).replace(/[^A-Za-z0-9_-]/gu, ""); }
