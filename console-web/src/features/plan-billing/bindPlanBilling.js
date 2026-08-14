/** PAGE-110/111 DOM 事件与 exact 请求快照组装。 */

import {
  normalizePlanPatchRequest,
  normalizeQuotaAdjustmentRequest,
} from "../../lib/planBilling.js";

const guardedDialogDocuments = new WeakSet();

/** owner 漂移时同步擦除 PAGE-110 DOM。 */
export function clearPlanManagementDom(documentRef = globalThis.document) {
  documentRef?.querySelector?.(".plans-panel")?.replaceChildren?.();
}

/** owner 漂移时同步擦除 PAGE-111 DOM，包括 reason textarea。 */
export function clearQuotaManagementDom(documentRef = globalThis.document) {
  documentRef?.querySelector?.(".quota-buckets-panel")?.replaceChildren?.();
}

/** 绑定套餐草稿、CAS 确认和显式重读。 */
export function bindPlanManagementInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-plan-management-root]");
  if (!root) return;
  root.querySelector("[data-plan-refresh]")?.addEventListener("click", () => {
    void callbacks.onRefresh?.();
  });
  root.querySelector("[data-plan-rebase]")?.addEventListener("click", () => {
    void callbacks.onRebase?.();
  });
  root.querySelector("[data-plan-discard]")?.addEventListener("click", () => {
    void callbacks.onDiscard?.();
  });

  const form = root.querySelector("[data-plan-form]");
  /** 从当前表单发布已规范化草稿，非法输入只反馈本地校验错误。 */
  const publishDraft = () => {
    try {
      const request = planRequestFromFormData(
        new FormData(form),
        callbacks.currentRevision?.(),
      );
      clearLocalValidation(root);
      void callbacks.onDraft?.(request);
    } catch {
      showLocalValidation(root, "套餐字段不合法");
    }
  };
  form?.addEventListener("change", publishDraft);
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const request = planRequestFromFormData(
        new FormData(form),
        callbacks.currentRevision?.(),
      );
      clearLocalValidation(root);
      void callbacks.onOpenConfirmation?.(request);
    } catch {
      showLocalValidation(root, "套餐字段不合法");
    }
  });

  const dialog = root.querySelector("[data-plan-confirm-dialog]");
  if (dialog) {
    bindDialog(dialog, {
      onClose: callbacks.onCloseConfirmation,
      closeSelector: "[data-plan-confirm-close], [data-plan-confirm-cancel]",
      returnFocusSelector: '[data-plan-form] button[type="submit"]',
    });
    dialog.querySelector("[data-plan-confirm-submit]")?.addEventListener("click", () => {
      void callbacks.onConfirmSave?.();
    });
    showModal(dialog);
  }
}

/** 绑定额度刷新、Dialog、焦点约束和同一幂等意图重试。 */
export function bindQuotaManagementInteractions(callbacks = {}) {
  const root = globalThis.document?.querySelector?.("[data-quota-management-root]");
  if (!root) return;
  root.querySelector("[data-quota-refresh]")?.addEventListener("click", () => {
    void callbacks.onRefresh?.();
  });
  root.querySelectorAll("[data-quota-adjust]").forEach((button) => {
    button.addEventListener("click", () => {
      void callbacks.onOpenAdjustment?.(button.dataset.quotaAdjust ?? "");
    });
  });

  const dialog = root.querySelector("[data-quota-adjustment-dialog]");
  if (!dialog) return;
  bindDialog(dialog, {
    onClose: callbacks.onCloseAdjustment,
    closeSelector: "[data-quota-adjustment-close], [data-quota-adjustment-cancel]",
    returnFocusSelector: `[data-quota-adjust="${dialog.dataset.quotaType ?? ""}"]`,
  });
  dialog.querySelector("[data-quota-adjustment-submit]")?.addEventListener("click", () => {
    const form = dialog.querySelector("[data-quota-adjustment-form]");
    try {
      const request = quotaAdjustmentRequestFromFormData(
        new FormData(form),
        callbacks.currentBuckets?.(),
      );
      clearLocalValidation(dialog);
      // reason 只存在于当前 FormData 和后续 operation 闭包，不写入 dataset、URL 或存储。
      void callbacks.onSubmitAdjustment?.(request);
    } catch {
      showLocalValidation(dialog, "额度调整字段不合法");
    }
  });
  dialog.querySelector("[data-quota-adjustment-retry]")?.addEventListener("click", () => {
    void callbacks.onRetryAdjustment?.();
  });
  showModal(dialog);
}

/** 把套餐表单转换为完整 CAS body；datetime-local 在提交时转换为 canonical UTC。 */
export function planRequestFromFormData(formData, expectedRevision) {
  const trialValue = String(formData.get("trial_ends_at") ?? "");
  return normalizePlanPatchRequest({
    expected_revision: strictInteger(expectedRevision),
    display_name: String(formData.get("display_name") ?? ""),
    base_tier: String(formData.get("base_tier") ?? ""),
    status: String(formData.get("status") ?? ""),
    trial_ends_at: trialValue ? datetimeLocalToUtc(trialValue) : null,
  });
}

/** 把额度 Dialog 转换为 exact body；Bucket revision 只从当前已验证响应派生。 */
export function quotaAdjustmentRequestFromFormData(formData, buckets) {
  const quotaType = String(formData.get("quota_type") ?? "");
  const bucket = Array.isArray(buckets)
    ? buckets.find((item) => item?.quota_type === quotaType)
    : null;
  return normalizeQuotaAdjustmentRequest({
    quota_type: quotaType,
    direction: String(formData.get("direction") ?? ""),
    amount: canonicalCnyInput(formData.get("amount")),
    reason: String(formData.get("reason") ?? ""),
    expected_bucket_revision: strictInteger(bucket?.state_revision),
  });
}

/** 将本地无时区值按浏览器当地时区严格转换为 UTC；非法日期不允许自动归一。 */
export function datetimeLocalToUtc(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(String(value ?? ""));
  if (!match) return "";
  const [, year, month, day, hour, minute, second = "00"] = match;
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
    0,
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

/** 把用户十进制输入规范为六位 CNY 文本，禁止指数、符号和浮点转换。 */
export function canonicalCnyInput(value) {
  const match = /^(0|[1-9][0-9]*)(?:\.([0-9]{1,6}))?$/.exec(String(value ?? "").trim());
  if (!match) return "";
  return `${match[1]}.${String(match[2] ?? "").padEnd(6, "0")}`;
}

/** aria-modal Dialog 将 Tab/Shift+Tab 约束在当前可操作控件。 */
export function trapPlanBillingDialogFocus(dialog, event, documentRef = globalThis.document) {
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

/**
 * 在 document 捕获阶段阻止 busy 原生 Dialog 被 Escape 默认关闭。
 *
 * 保存重渲染会替换 Dialog 节点，而且所有按钮禁用后焦点可能回到 body；因此只绑定在
 * Dialog 节点上的 keydown 监听并不足够。每个 document 只安装一次守卫，实际是否拦截
 * 仍由当前 modal 的 aria-busy 状态决定，不影响空闲 Dialog 的原生 Escape 交互。
 */
export function installPlanBillingBusyEscapeGuard(documentRef = globalThis.document) {
  if (
    !documentRef
    || typeof documentRef.addEventListener !== "function"
    || guardedDialogDocuments.has(documentRef)
  ) return false;
  documentRef.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const dialog = documentRef.querySelector?.(
      '[data-plan-confirm-dialog][aria-busy="true"], [data-quota-adjustment-dialog][aria-busy="true"]',
    );
    if (dialog?.open === true) event.preventDefault();
  }, { capture: true });
  guardedDialogDocuments.add(documentRef);
  return true;
}

/** 绑定模态框关闭、焦点回落与键盘焦点约束，不持久化表单内容。 */
function bindDialog(dialog, { onClose, closeSelector, returnFocusSelector }) {
  installPlanBillingBusyEscapeGuard(dialog.ownerDocument ?? globalThis.document);
  /** 请求关闭当前模态框，并在状态层确认关闭后恢复触发控件焦点。 */
  const close = async () => {
    const closed = await onClose?.();
    if (closed !== false) {
      globalThis.document?.querySelector?.(returnFocusSelector)?.focus?.();
    }
  };
  dialog.querySelectorAll(closeSelector).forEach((button) => {
    button.addEventListener("click", () => void close());
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    void close();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Tab") trapPlanBillingDialogFocus(dialog, event);
  });
}

/** 以原生模态能力展示对话框，并把焦点移入首个可操作控件。 */
function showModal(dialog) {
  if (!dialog.open && typeof dialog.showModal === "function") dialog.showModal();
  dialog.querySelector?.("button:not([disabled]), input:not([disabled]), select:not([disabled])")?.focus?.();
}

/** 在当前套餐或额度区域展示本地校验错误，不回显敏感请求正文。 */
function showLocalValidation(root, message) {
  const live = root.querySelector?.(".plan-billing-live");
  if (live) {
    live.textContent = message;
    live.classList.add("is-error");
  }
}

/** 清除当前区域的本地校验提示，避免旧错误污染下一次编辑。 */
function clearLocalValidation(root) {
  const live = root.querySelector?.(".plan-billing-live");
  if (live) {
    live.textContent = "";
    live.classList.remove("is-error");
  }
}

/** 严格解析非负安全整数，拒绝隐式浮点转换和非规范数字文本。 */
function strictInteger(value) {
  if (Number.isSafeInteger(value) && value >= 0) return value;
  const text = String(value ?? "");
  if (!/^(?:0|[1-9][0-9]*)$/.test(text)) return Number.NaN;
  const number = Number(text);
  return Number.isSafeInteger(number) ? number : Number.NaN;
}
