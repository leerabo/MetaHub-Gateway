// PAGE-120 操作绑定：只把表单白名单和本地 PNG 转为 API 输入，不提交 tenant_id 或远程 URL。
import { pngFileToBase64 } from "../../lib/branding.js";

/** 绑定白标保存和 Logo 清理；后端仍需重复执行 branding.manage 与 revision CAS。 */
export function bindBrandingSettingsInteractions(documentApi = globalThis.document, actions = {}) {
  const form = documentApi?.querySelector?.("[data-branding-form]");
  if (!form || typeof actions.onSave !== "function") return false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity?.()) return;
    const data = new FormData(form);
    const input = {
      product_name: String(data.get("product_name") ?? ""),
      primary_color: String(data.get("primary_color") ?? ""),
      secondary_color: String(data.get("secondary_color") ?? ""),
      login_title: String(data.get("login_title") ?? ""),
      login_message: String(data.get("login_message") ?? ""),
      custom_domain: String(data.get("custom_domain") ?? "").trim() || null,
    };
    const file = data.get("logo_png");
    if (typeof File !== "undefined" && file instanceof File && file.size > 0) input.logo_png_base64 = await pngFileToBase64(file);
    if (data.get("logo_clear") === "on") input.logo_png_base64 = null;
    await actions.onSave(input);
  }, { once: true });
  return true;
}

/** 清理设置面板敏感控件；Logo Blob URL 由 Branding state 在清理时回收。 */
export function clearBrandingSettingsDom(documentApi = globalThis.document) {
  const panel = documentApi?.querySelector?.("[data-branding-page]");
  panel?.querySelectorAll?.("input, textarea")?.forEach((control) => {
    if (control.type !== "hidden") control.value = "";
  });
  panel?.replaceChildren?.();
}
