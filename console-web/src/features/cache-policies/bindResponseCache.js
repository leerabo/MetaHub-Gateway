/**
 * PAGE-106 DOM 事件与 strict 请求快照组装。
 *
 * 表单值只在当前事件闭包中存在；幂等键、Bearer、tenant owner 和 request_id 均由 main/state/client
 * 分层管理，本模块不会把它们写入 DOM、URL 或浏览器存储。
 */

import {
  normalizeResponseCacheInvalidationRequest,
  normalizeResponseCachePolicyRequest,
} from "../../lib/responseCache.js";

/** owner 漂移时同步擦除 PAGE-106 DOM；状态机会在此之后才触发旧 Controller abort。 */
export function clearResponseCacheDom(documentRef = globalThis.document) {
  documentRef?.querySelector?.(".response-cache-panel")?.replaceChildren?.();
}

/** 绑定策略选择、编辑、分页和失效 Dialog；真实授权仍由每次 Control API 请求复核。 */
export function bindResponseCacheInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-response-cache-root]");
  if (!root) return;

  root.querySelector("[data-response-cache-refresh]")?.addEventListener("click", () => {
    void callbacks.onRefresh?.();
  });
  root.querySelector("[data-response-cache-previous]")?.addEventListener("click", () => {
    void callbacks.onPreviousPage?.();
  });
  root.querySelector("[data-response-cache-next]")?.addEventListener("click", () => {
    void callbacks.onNextPage?.();
  });
  root.querySelectorAll("[data-response-cache-select]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onSelect?.(button.dataset.responseCacheSelect ?? "");
    });
  });

  const policyForm = root.querySelector("[data-response-cache-policy-form]");
  /** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
  const publishDraft = (event) => {
    if (!policyForm || !callbacks.currentEditor) return;
    // 从 disabled 切到 enabled 时先开放显式确认框；未确认状态仍由 strict normalizer 拒绝保存。
    if (event?.target?.name === "enabled") {
      const confirmation = policyForm.elements.namedItem("single_data_class_confirmed");
      if (confirmation && "disabled" in confirmation) {
        confirmation.disabled = !event.target.checked;
        if (!event.target.checked && "checked" in confirmation) confirmation.checked = false;
      }
    }
    try {
      const current = callbacks.currentEditor();
      const request = responseCachePolicyRequestFromFormData(
        new FormData(policyForm),
        current?.model_alias_id,
        current?.expected_version,
      );
      clearLocalValidation(policyForm);
      void callbacks.onDraft?.(request);
    } catch {
      showLocalValidation(policyForm, "策略字段不合法");
    }
  };
  policyForm?.addEventListener("change", publishDraft);
  policyForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const current = callbacks.currentEditor?.();
      const request = responseCachePolicyRequestFromFormData(
        new FormData(policyForm),
        current?.model_alias_id,
        current?.expected_version,
      );
      clearLocalValidation(policyForm);
      void callbacks.onSave?.(request);
    } catch {
      showLocalValidation(policyForm, "策略字段不合法");
    }
  });
  root.querySelector("[data-response-cache-discard]")?.addEventListener("click", () => {
    void callbacks.onDiscard?.();
  });
  root.querySelector("[data-response-cache-rebase]")?.addEventListener("click", () => {
    void callbacks.onRebase?.();
  });

  root.querySelector("[data-response-cache-invalidate-open]")?.addEventListener("click", async () => {
    const opened = await callbacks.onOpenInvalidation?.();
    if (opened !== false) {
      globalThis.document?.querySelector?.("[data-response-cache-invalidate-close]")?.focus?.();
    }
  });
  bindInvalidationDialog(callbacks);
}

/** 把编辑器控件转换为完整 CAS body；重复 tag 保持重复并由 strict normalizer 拒绝。 */
export function responseCachePolicyRequestFromFormData(
  formData,
  modelAliasId,
  expectedVersion,
) {
  const enabled = formData.get("enabled") === "on";
  const dataClass = String(formData.get("data_class") ?? "").trim();
  const tags = String(formData.get("tags") ?? "")
    .split(/[\s,]+/u)
    .filter(Boolean)
    .sort();
  return normalizeResponseCachePolicyRequest({
    model_alias_id: String(modelAliasId ?? ""),
    status: enabled ? "enabled" : "disabled",
    data_class: dataClass || null,
    single_data_class_confirmed: enabled
      ? formData.get("single_data_class_confirmed") === "on"
      : false,
    ttl_seconds: strictInteger(formData.get("ttl_seconds")),
    charge_cache_hits: formData.get("charge_cache_hits") === "on",
    tags,
    expected_version: strictInteger(expectedVersion),
  });
}

/** 把 Dialog 收紧为 all 或 all-match selector；当前页外 selector 会在状态机再次拒绝。 */
export function responseCacheInvalidationRequestFromFormData(formData) {
  const scope = String(formData.get("scope") ?? "");
  const tags = scope === "tags"
    ? formData.getAll("selector").map(String).sort()
    : [];
  return normalizeResponseCacheInvalidationRequest({
    scope,
    match_mode: "all",
    tags,
    reason_code: String(formData.get("reason_code") ?? ""),
  });
}

/** 绑定 modal 范围切换、Escape、backdrop 和提交；关闭后焦点回到新的失效按钮。 */
function bindInvalidationDialog(callbacks) {
  const backdrop = globalThis.document?.querySelector?.("[data-response-cache-dialog-backdrop]");
  const form = backdrop?.querySelector?.("[data-response-cache-invalidate-form]");
  if (!backdrop || !form) return;
  const close = async () => {
    const closed = await callbacks.onCloseInvalidation?.();
    if (closed !== false) {
      globalThis.document?.querySelector?.("[data-response-cache-invalidate-open]")?.focus?.();
    }
  };
  backdrop.querySelector("[data-response-cache-invalidate-close]")?.addEventListener("click", () => {
    void close();
  });
  backdrop.querySelector("[data-response-cache-invalidate-cancel]")?.addEventListener("click", () => {
    void close();
  });
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) void close();
  });
  backdrop.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      void close();
    } else if (event.key === "Tab") {
      trapDialogFocus(backdrop, event);
    }
  });
  form.querySelectorAll('input[name="scope"]').forEach((input) => {
    input.addEventListener("change", () => syncSelectorAvailability(form));
  });
  syncSelectorAvailability(form);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const request = responseCacheInvalidationRequestFromFormData(new FormData(form));
      clearLocalValidation(form);
      void callbacks.onInvalidate?.(request);
    } catch {
      showLocalValidation(form, "请选择有效的失效范围和 selector");
    }
  });
}

/** scope=all 时禁用 selector，防止浏览器同时提交两种互斥意图。 */
function syncSelectorAvailability(form) {
  const tags = form.elements.namedItem("scope")?.value === "tags";
  const fieldset = form.querySelector("[data-response-cache-selector-list]");
  if (fieldset) fieldset.disabled = !tags;
}

/** aria-modal Dialog 将 Tab/Shift+Tab 约束在当前可操作控件，避免焦点落回被遮罩页面。 */
function trapDialogFocus(dialog, event) {
  const controls = [...dialog.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((element) => element.offsetParent !== null || typeof element.offsetParent === "undefined");
  if (!controls.length) return;
  const first = controls[0];
  const last = controls.at(-1);
  if (event.shiftKey && globalThis.document?.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && globalThis.document?.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function showLocalValidation(form, message) {
  const live = form.querySelector(".response-cache-editor-live, .response-cache-invalidate-live");
  if (live) {
    live.textContent = message;
    live.classList.add("is-error");
  }
}

/** 页面状态只接受后端授权的脱敏字段，并在身份变化时清理旧请求和私有状态。 */
function clearLocalValidation(form) {
  const live = form.querySelector(".response-cache-editor-live, .response-cache-invalidate-live");
  if (live) {
    live.textContent = "";
    live.classList.remove("is-error");
  }
}

/** Number() 会把空串变成 0；先要求 canonical 整数字符串再转换。 */
function strictInteger(value) {
  const text = String(value ?? "");
  if (!/^(?:0|[1-9][0-9]*)$/.test(text)) return Number.NaN;
  const number = Number(text);
  return Number.isSafeInteger(number) ? number : Number.NaN;
}
