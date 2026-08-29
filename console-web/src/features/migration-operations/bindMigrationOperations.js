/** PRD-131 Migration Center DOM bindings. Sensitive step-up input is cleared before async work. */

export function bindMigrationOperationInteractions({
  root = globalThis.document,
  onAssessment,
  onRun,
  onRefreshDifferences,
  onOpenTransition,
  onCloseTransition,
  onTransition,
} = {}) {
  const page = root?.querySelector?.("[data-migration-root]");
  if (!page) return false;

  page.querySelector("[data-migration-assessment-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const domainInputs = [...form.querySelectorAll("input[name='domains']")];
    domainInputs.forEach((input) => input.setCustomValidity?.(""));
    const selectedDomains = domainInputs.filter((input) => input.checked);
    if (!selectedDomains.length) {
      domainInputs[0]?.setCustomValidity?.("请选择至少一个迁移域");
    }
    if (!form.checkValidity()) {
      form.reportValidity?.();
      return;
    }
    const data = new FormData(form);
    const domains = data.getAll("domains").map(String).sort();
    void onAssessment?.({
      sourceKind: String(data.get("source_kind") ?? ""),
      manifestVersion: String(data.get("manifest_version") ?? ""),
      sourceManifestSha256: String(data.get("source_manifest_sha256") ?? ""),
      domains,
      reasonCode: String(data.get("reason_code") ?? ""),
      confirmed: data.get("confirmed") === "on",
    });
  });

  page.querySelector("[data-migration-run-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!event.currentTarget.checkValidity()) {
      event.currentTarget.reportValidity?.();
      return;
    }
    const data = new FormData(event.currentTarget);
    void onRun?.({
      reasonCode: String(data.get("reason_code") ?? ""),
      confirmed: data.get("confirmed") === "on",
    });
  });

  page.querySelector("[data-migration-refresh-differences]")?.addEventListener(
    "click", () => void onRefreshDifferences?.(),
  );

  page.querySelectorAll("[data-migration-open-transition]").forEach((button) => {
    button.addEventListener("click", () => {
      void onOpenTransition?.(String(button.dataset.migrationOpenTransition ?? ""));
    });
  });

  const dialog = page.querySelector("[data-migration-dialog]");
  if (!dialog) return true;
  const method = dialog.querySelector("[data-migration-step-up-method]");
  const syncMethod = () => {
    const codeLabel = dialog.querySelector("[data-migration-step-up-code]");
    const codeInput = codeLabel?.querySelector("input");
    const webauthn = method?.value === "webauthn";
    if (codeLabel) codeLabel.hidden = webauthn;
    if (codeInput) codeInput.required = !webauthn;
  };
  method?.addEventListener("change", syncMethod);
  syncMethod();

  dialog.querySelectorAll("[data-migration-close-transition]").forEach((button) => {
    button.addEventListener("click", () => {
      clearMigrationSensitiveInputs(dialog);
      void onCloseTransition?.();
    });
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (dialog.getAttribute("aria-busy") === "true") return;
    clearMigrationSensitiveInputs(dialog);
    void onCloseTransition?.();
  });
  dialog.addEventListener("keydown", trapDialogFocus);
  dialog.querySelector("[data-migration-transition-form]")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.checkValidity()) {
      form.reportValidity?.();
      return;
    }
    const data = new FormData(form);
    const codeInput = form.querySelector("[data-migration-sensitive]");
    let verificationCode = String(data.get("verification_code") ?? "");
    if (codeInput) codeInput.value = "";
    const input = {
      action: String(data.get("action") ?? ""),
      reasonCode: String(data.get("reason_code") ?? ""),
      method: String(data.get("method") ?? ""),
      verificationCode,
      confirmed: data.get("confirmed") === "on",
    };
    verificationCode = "";
    void onTransition?.(input);
  });
  if (!dialog.open) dialog.showModal?.();
  if (dialog.open && dialog.getAttribute("aria-busy") !== "true") {
    dialog.querySelector("select, input:not([type='hidden']), button")?.focus?.({
      preventScroll: true,
    });
  }
  return true;
}

export function clearMigrationOperationDom(root = globalThis.document) {
  const panel = root?.querySelector?.(".migration-operations-panel");
  clearMigrationSensitiveInputs(panel);
  panel?.replaceChildren?.();
}

function clearMigrationSensitiveInputs(root) {
  root?.querySelectorAll?.("[data-migration-sensitive]").forEach((input) => {
    input.value = "";
  });
}

function trapDialogFocus(event) {
  if (event.key !== "Tab") return;
  const dialog = event.currentTarget;
  const focusable = [...dialog.querySelectorAll(
    "button:not([disabled]), select:not([disabled]), input:not([disabled]):not([type='hidden'])",
  )].filter((item) => !item.closest("[hidden]"));
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && globalThis.document?.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && globalThis.document?.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
