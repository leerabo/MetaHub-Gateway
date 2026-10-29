/** Bind the shared visual editor without retaining PNG or step-up plaintext. */

import { pngFileToBase64 } from "../../lib/brandDomains.js";

export function bindBrandEditorInteractions(root = globalThis.document, actions = {}) {
  const forms = root?.querySelectorAll?.("[data-brand-command-form]") ?? [];
  for (const form of forms) {
    form.querySelector?.("[data-brand-step-up-method]")?.addEventListener("change", (event) => {
      const code = form.querySelector("[data-brand-step-up-code]");
      const input = code?.querySelector("[data-brand-sensitive]");
      const webAuthn = event.currentTarget.value === "webauthn";
      if (input) { input.value = ""; input.disabled = webAuthn; input.required = !webAuthn; }
      if (code) code.hidden = webAuthn;
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!form.reportValidity?.() || typeof actions.onCommand !== "function") return;
      const data = new FormData(form);
      let verificationCode = String(data.get("verificationCode") ?? "");
      const command = await commandFromForm(data);
      eraseSensitive(form);
      try {
        await actions.onCommand({
          scope: String(form.dataset.brandCommandForm ?? ""),
          command,
          method: String(data.get("method") ?? "totp"),
          verificationCode,
          confirmed: data.get("confirmed") === "on",
        });
      } finally {
        verificationCode = "";
        eraseSensitive(form);
      }
    });
  }
  return forms.length > 0;
}

export function clearBrandEditorDom(root = globalThis.document) {
  for (const editor of root?.querySelectorAll?.("[data-brand-domain-editor]") ?? []) {
    eraseSensitive(editor);
    editor.replaceChildren?.();
  }
}

async function commandFromForm(data) {
  const action = String(data.get("action") ?? "");
  if (action === "update") return {
    action,
    product_name: String(data.get("product_name") ?? ""),
    primary_color: String(data.get("primary_color") ?? "").toUpperCase(),
    secondary_color: String(data.get("secondary_color") ?? "").toUpperCase(),
    login_title: String(data.get("login_title") ?? ""),
    login_message: String(data.get("login_message") ?? ""),
  };
  if (action === "attach_asset") {
    const file = data.get("png_file");
    return { action, asset_kind: String(data.get("asset_kind") ?? ""), png_base64: await pngFileToBase64(file) };
  }
  if (action === "remove_asset") return { action, asset_kind: String(data.get("asset_kind") ?? "") };
  if (action === "request_domain") return { action, domain: String(data.get("domain") ?? "").trim().toLowerCase() };
  return { action };
}

function eraseSensitive(root) {
  root?.querySelectorAll?.("[data-brand-sensitive], input[type='file']")?.forEach((input) => { input.value = ""; });
}
