/** PRD-120..126 installer, health, diagnostic, support, and security binding. */

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
  const installerEnvironment = root.querySelector("[data-private-gateway-installer-environment]");
  installerEnvironment?.addEventListener("change", () => {
    const data = new FormData(installerEnvironment);
    void callbacks.onInstallerSelection?.({
      deploymentId: String(data.get("deployment_id") ?? ""),
      releaseId: String(data.get("release_id") ?? ""),
      profileId: String(data.get("profile_id") ?? ""),
      architecture: String(data.get("architecture") ?? ""),
    });
  });
  root.querySelectorAll("[data-private-gateway-installer-download]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onInstallerDownload?.(
      button.dataset.privateGatewayInstallerDownload ?? "",
    ));
  });
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
  root.querySelectorAll("[data-private-gateway-maintenance]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const startMinute = timeMinute(data.get("startTime"));
      const freezeStartsAt = optionalLocalTimestamp(data.get("freezeStartsAt"));
      const freezeEndsAt = optionalLocalTimestamp(data.get("freezeEndsAt"));
      if (startMinute === null || freezeStartsAt.invalid || freezeEndsAt.invalid
        || freezeStartsAt.value === null !== (freezeEndsAt.value === null)) return;
      void callbacks.onUpgradeMaintenance?.({
        deploymentId: String(data.get("deploymentId") ?? ""),
        releaseChannel: String(data.get("releaseChannel") ?? ""),
        timezoneName: String(data.get("timezoneName") ?? ""),
        weekdays: data.getAll("weekdays").map(Number),
        startMinute,
        durationMinutes: Number(data.get("durationMinutes")),
        freezeStartsAt: freezeStartsAt.value,
        freezeEndsAt: freezeEndsAt.value,
        status: String(data.get("status") ?? ""),
        reasonCode: String(data.get("reasonCode") ?? ""),
        confirmed: data.get("confirmed") === "on",
      });
    });
  });
  root.querySelectorAll("[data-private-gateway-upgrade-rollback]").forEach((form) => {
    const method = form.querySelector('select[name="method"]');
    method?.addEventListener("change", () => {
      const code = form.querySelector("[data-private-gateway-upgrade-code]");
      const input = code?.querySelector("[data-private-gateway-sensitive]");
      const webAuthn = method.value === "webauthn";
      if (input) {
        input.value = "";
        input.disabled = webAuthn;
        input.required = !webAuthn;
      }
      if (code) code.hidden = webAuthn;
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const input = form.querySelector("[data-private-gateway-sensitive]");
      let verificationCode = String(input?.value ?? "").trim().toUpperCase();
      if (input) input.value = "";
      const methodValue = String(data.get("method") ?? "");
      const validCode = methodValue === "webauthn"
        || methodValue === "totp" && /^[0-9]{6}$/u.test(verificationCode)
        || methodValue === "recovery_code"
          && /^MHRC-[23456789A-HJ-NP-Z]{5}-[23456789A-HJ-NP-Z]{5}$/u
            .test(verificationCode);
      const error = form.querySelector("[data-private-gateway-upgrade-error]");
      if (!validCode || data.get("confirmed") !== "on") {
        verificationCode = "";
        if (error) error.textContent = "请确认回滚并完成有效的二次验证";
        return;
      }
      if (error) error.textContent = "";
      try {
        await callbacks.onUpgradeRollback?.({
          deploymentId: String(data.get("deploymentId") ?? ""),
          targetReleaseId: String(data.get("targetReleaseId") ?? ""),
          reasonCode: String(data.get("reasonCode") ?? ""),
          method: methodValue,
          verificationCode,
          confirmed: data.get("confirmed") === "on",
        });
      } finally {
        verificationCode = "";
        eraseSensitive(form);
      }
    });
  });
  root.querySelectorAll("[data-private-gateway-validation]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onValidation?.(
      button.dataset.deploymentId ?? "",
      button.dataset.privateGatewayValidation ?? "",
    ));
  });
  root.querySelectorAll("[data-private-gateway-acceptance-open]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onAcceptanceOpen?.(
      button.dataset.deploymentId ?? "",
    ));
  });
  root.querySelectorAll("[data-private-gateway-support-bundle]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onSupportBundle?.(
      button.dataset.deploymentId ?? "",
      button.dataset.privateGatewaySupportBundle ?? "",
    ));
  });
  root.querySelectorAll("[data-private-gateway-diagnostic]").forEach((button) => {
    button.addEventListener("click", () => void callbacks.onDiagnosticOpen?.(
      button.dataset.deploymentId ?? "",
      button.dataset.grantId ?? "",
      button.dataset.privateGatewayDiagnostic ?? "",
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
  bindAcceptanceDialog(root, callbacks);
  bindDiagnosticDialog(root, callbacks);
  return true;
}

function timeMinute(value) {
  const match = /^([01][0-9]|2[0-3]):([0-5][0-9])$/u.exec(String(value ?? ""));
  return match ? Number(match[1]) * 60 + Number(match[2]) : null;
}

function optionalLocalTimestamp(value) {
  const text = String(value ?? "");
  if (!text) return { value: null, invalid: false };
  const date = new Date(text);
  return Number.isNaN(date.valueOf())
    ? { value: null, invalid: true }
    : { value: date.toISOString(), invalid: false };
}

function bindDiagnosticDialog(root, callbacks) {
  const dialog = root.querySelector("[data-private-gateway-diagnostic-dialog]");
  if (!dialog) return;
  const close = async () => {
    eraseSensitive(dialog);
    const closed = await callbacks.onDiagnosticClose?.();
    if (closed !== false) {
      root.querySelector("[data-private-gateway-diagnostic]")?.focus?.();
    }
  };
  dialog.querySelectorAll("[data-private-gateway-diagnostic-close]").forEach((button) => {
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
  const form = dialog.querySelector("[data-private-gateway-diagnostic-form]");
  const methodSelect = form?.querySelector("[data-private-gateway-diagnostic-method]");
  const codeLabel = form?.querySelector("[data-private-gateway-diagnostic-code]");
  const codeInput = codeLabel?.querySelector("[data-private-gateway-sensitive]");
  methodSelect?.addEventListener("change", () => {
    const usesSecurityKey = methodSelect.value === "webauthn";
    if (codeInput) {
      codeInput.value = "";
      codeInput.disabled = usesSecurityKey;
      codeInput.required = !usesSecurityKey;
    }
    if (codeLabel) codeLabel.hidden = usesSecurityKey;
  });
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const method = String(data.get("method") ?? "");
    const input = form.querySelector("[data-private-gateway-sensitive]");
    let verificationCode = String(input?.value ?? "").trim().toUpperCase();
    if (input) input.value = "";
    const validCode = method === "webauthn"
      || method === "totp" && /^[0-9]{6}$/u.test(verificationCode)
      || method === "recovery_code"
        && /^MHRC-[23456789A-HJ-NP-Z]{5}-[23456789A-HJ-NP-Z]{5}$/u.test(verificationCode);
    if (data.get("confirmed") !== "on" || !validCode) {
      verificationCode = "";
      setDiagnosticFormError(dialog, "请完成确认并输入有效的二次验证码");
      return;
    }
    setDiagnosticFormError(dialog, "");
    try {
      await callbacks.onDiagnosticDecision?.({
        method,
        verificationCode,
        ttlSeconds: Number(data.get("ttl_seconds") ?? 300),
      });
    } finally {
      verificationCode = "";
      eraseSensitive(form);
    }
  });
  if (!dialog.open) dialog.showModal?.();
  dialog.querySelector("select")?.focus?.();
}

function bindAcceptanceDialog(root, callbacks) {
  const dialog = root.querySelector("[data-private-gateway-acceptance-dialog]");
  if (!dialog) return;
  const close = async () => {
    eraseSensitive(dialog);
    const closed = await callbacks.onAcceptanceClose?.();
    if (closed !== false) {
      globalThis.document?.querySelector?.("[data-private-gateway-acceptance-open]")?.focus?.();
    }
  };
  dialog.querySelectorAll("[data-private-gateway-acceptance-close]").forEach((button) => {
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
  const form = dialog.querySelector("[data-private-gateway-acceptance-form]");
  const methodSelect = form?.querySelector("[data-private-gateway-acceptance-method]");
  const codeLabel = form?.querySelector("[data-private-gateway-acceptance-code]");
  const codeInput = codeLabel?.querySelector("[data-private-gateway-sensitive]");
  methodSelect?.addEventListener("change", () => {
    const usesSecurityKey = methodSelect.value === "webauthn";
    if (codeInput) {
      codeInput.value = "";
      codeInput.disabled = usesSecurityKey;
      codeInput.required = !usesSecurityKey;
    }
    if (codeLabel) codeLabel.hidden = usesSecurityKey;
  });
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const method = String(data.get("method") ?? "");
    const input = form.querySelector("[data-private-gateway-sensitive]");
    let verificationCode = String(input?.value ?? "").trim().toUpperCase();
    if (input) input.value = "";
    const valid = data.get("confirmed") === "on" && (method === "webauthn"
      || (method === "totp"
      ? /^[0-9]{6}$/u.test(verificationCode)
      : method === "recovery_code"
        && /^MHRC-[23456789A-HJ-NP-Z]{5}-[23456789A-HJ-NP-Z]{5}$/u.test(verificationCode)));
    if (!valid) {
      verificationCode = "";
      setAcceptanceFormError(dialog, "请输入有效的二次验证码");
      return;
    }
    setAcceptanceFormError(dialog, "");
    try {
      await callbacks.onAcceptance?.({ method, verificationCode });
    } finally {
      verificationCode = "";
      eraseSensitive(form);
    }
  });
  if (!dialog.open) dialog.showModal?.();
  dialog.querySelector("select")?.focus?.();
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
function setAcceptanceFormError(root, value) { const node = root.querySelector?.("[data-private-gateway-acceptance-form-error]"); if (node) node.textContent = value; }
function setDiagnosticFormError(root, value) { const node = root.querySelector?.("[data-private-gateway-diagnostic-form-error]"); if (node) node.textContent = value; }
function cssEscape(value) { return globalThis.CSS?.escape?.(String(value)) ?? String(value).replace(/[^A-Za-z0-9_-]/gu, ""); }
