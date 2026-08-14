/** PAGE-108/109 的纯 UI 意图绑定；密码读取后立即清空且不进入 dataset 或状态机。 */
const countdownTimers = new WeakMap();
// 审批对话框只允许在可交互控件之间循环焦点，禁用或隐藏节点不得进入键盘路径。
const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "a[href]",
  "[tabindex]:not([tabindex='-1'])",
].join(", ");

export function bindApprovalInteractions(handlers, root = globalThis.document) {
  const scope = root?.querySelector ? root : null;
  if (!scope) return false;
  const page = scope.matches?.("[data-approvals-root]") ? scope : scope.querySelector("[data-approvals-root]");
  if (!page) return false;
  page.querySelector("[data-approval-refresh]")?.addEventListener("click", () => handlers.onRefresh?.());
  page.querySelector("[data-approval-back]")?.addEventListener("click", () => handlers.onBack?.());
  page.querySelector("[data-approval-status-filter]")?.addEventListener("change", (event) => {
    handlers.onFilter?.(
      String(event.currentTarget?.value ?? ""),
      String(page.querySelector("[data-approval-target-filter]")?.value ?? ""),
    );
  });
  page.querySelector("[data-approval-target-filter]")?.addEventListener("change", (event) => {
    handlers.onFilter?.(
      String(page.querySelector("[data-approval-status-filter]")?.value ?? "pending"),
      String(event.currentTarget?.value ?? ""),
    );
  });
  page.querySelector("[data-approval-previous]")?.addEventListener("click", () => handlers.onPrevious?.());
  page.querySelector("[data-approval-next]")?.addEventListener("click", () => handlers.onNext?.());
  for (const button of page.querySelectorAll("[data-approval-select]")) {
    button.addEventListener("click", () => {
      const id = canonicalApprovalId(button.dataset.approvalSelect);
      if (id) handlers.onSelect?.(id);
    });
  }
  for (const button of page.querySelectorAll("[data-approval-action]")) {
    button.addEventListener("click", () => {
      const action = String(button.dataset.approvalAction ?? "");
      const approvalId = canonicalApprovalId(button.dataset.approvalTask);
      if (["approve", "reject"].includes(action) && approvalId) {
        handlers.onOpenAction?.(Object.freeze({ approvalId, action }));
      }
    });
  }
  page.querySelector("[data-approval-reason]")?.addEventListener("change", (event) => {
    handlers.onReason?.(String(event.currentTarget?.value ?? ""));
  });
  page.querySelector("[data-approval-action-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const passwordInput = page.querySelector("[data-approval-password]");
    // password 只在该回调的局部变量中存在；读取后先擦除 DOM，再交给一次连续 step-up/decision closure。
    let password = passwordInput ? String(passwordInput.value ?? "") : undefined;
    if (passwordInput) passwordInput.value = "";
    try {
      await handlers.onSubmitAction?.(password);
    } finally {
      password = undefined;
      if (passwordInput) passwordInput.value = "";
    }
  });
  bindActionDialog(page, handlers);
  startCountdown(page);
  return true;
}

/** owner 漂移时先擦除密码与审批正文，再移除页面根。 */
export function clearApprovalDom(root = globalThis.document) {
  const scope = root?.querySelector ? root : null;
  if (!scope) return false;
  const page = scope.matches?.("[data-approvals-root]") ? scope : scope.querySelector("[data-approvals-root]");
  if (!page) return false;
  stopCountdown(page);
  clearPassword(page);
  closeDialog(page.querySelector("[data-approval-dialog]"));
  for (const element of page.querySelectorAll("[data-approval-sensitive]")) element.textContent = "";
  page.remove();
  return true;
}

/**
 * 根据任务、动作和身份 owner 的稳定描述恢复焦点，绝不复用旧页面保留的 DOM 节点。
 */
export function restoreApprovalActionFocus(
  description,
  currentOwnerKey,
  root = globalThis.document,
) {
  const approvalId = canonicalApprovalId(description?.approvalId);
  const action = String(description?.action ?? "");
  if (
    !approvalId
    || !["approve", "reject"].includes(action)
    || !description?.ownerKey
    || description.ownerKey !== currentOwnerKey
  ) return false;
  const scope = root?.querySelector ? root : null;
  if (!scope) return false;
  const page = scope.matches?.("[data-approvals-root]")
    ? scope
    : scope.querySelector("[data-approvals-root]");
  if (!page) return false;
  const button = [...page.querySelectorAll("[data-approval-action]")].find((candidate) => (
    canonicalApprovalId(candidate.dataset?.approvalTask) === approvalId
    && String(candidate.dataset?.approvalAction ?? "") === action
    && candidate.disabled !== true
  ));
  if (!button || typeof button.focus !== "function") return false;
  button.focus();
  return true;
}

/** 定时全壳刷新不得重建正在输入密码的 Dialog。 */
export function approvalHasPasswordInput(root = globalThis.document) {
  const input = root?.querySelector?.("[data-approval-password]");
  return Boolean(input && String(input.value ?? "").length > 0);
}

function clearPassword(root) {
  const input = root?.querySelector?.("[data-approval-password]");
  if (input) input.value = "";
}

/**
 * 激活原生模态框并绑定关闭与焦点边界；激活失败时擦除敏感输入并关闭前端动作状态。
 */
function bindActionDialog(page, handlers) {
  const dialog = page.querySelector("[data-approval-dialog]");
  if (!dialog) return;
  let closeRequested = false;
  const description = dialogDescription(dialog);
  /** 忙碌期间拒绝关闭；其余关闭路径先清除密码，再把稳定描述交还页面状态机。 */
  const requestClose = async (event) => {
    event?.preventDefault?.();
    if (dialogBusy(dialog, handlers) || closeRequested) return false;
    closeRequested = true;
    clearPassword(page);
    closeDialog(dialog);
    await handlers.onCloseAction?.(description);
    return true;
  };
  for (const button of page.querySelectorAll("[data-approval-dialog-close]")) {
    button.addEventListener("click", requestClose);
  }
  dialog.addEventListener("cancel", requestClose);
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      void requestClose(event);
      return;
    }
    if (event.key === "Tab") trapDialogFocus(event, dialog);
  });
  if (!showApprovalModal(dialog)) {
    clearPassword(page);
    void handlers.onCloseAction?.(description);
    return;
  }
  focusApprovalDialog(dialog);
}

/** 尝试把对话框放入浏览器顶层模态层，任何不支持或异常都按失败返回。 */
function showApprovalModal(dialog) {
  if (dialog.open) return true;
  if (typeof dialog.showModal !== "function") return false;
  try {
    dialog.showModal();
    return dialog.open !== false;
  } catch {
    return false;
  }
}

/** 幂等关闭仍处于顶层模态层的对话框，已卸载节点的异常不携带业务正文外传。 */
function closeDialog(dialog) {
  if (!dialog?.open || typeof dialog.close !== "function") return;
  try {
    dialog.close();
  } catch {
    // A detached dialog is already outside the modal top layer.
  }
}

/** 合并页面状态机与当前 DOM 的忙碌标记，防止处理中途通过旧控件关闭。 */
function dialogBusy(dialog, handlers = {}) {
  return handlers.isActionBusy?.() === true
    || String(dialog?.dataset?.approvalBusy ?? "") === "true";
}

/** 从受限 dataset 恢复稳定任务和动作描述，不保存密码、原因正文或 DOM 引用。 */
function dialogDescription(dialog) {
  const approvalId = canonicalApprovalId(dialog?.dataset?.approvalTask);
  const action = String(dialog?.dataset?.approvalDialogAction ?? "");
  return approvalId && ["approve", "reject"].includes(action)
    ? Object.freeze({ approvalId, action })
    : null;
}

/** 按密码、原因和提交顺序设置初始焦点；忙碌态只聚焦对话框容器。 */
function focusApprovalDialog(dialog) {
  if (dialogBusy(dialog)) {
    dialog.focus?.();
    return;
  }
  const target = dialog.querySelector("[data-approval-password]")
    ?? dialog.querySelector("[data-approval-reason]")
    ?? dialog.querySelector("button[type='submit']:not([disabled])");
  target?.focus?.();
}

/** 把 Tab 焦点限制在当前审批模态框，空控件集合时回落到对话框容器。 */
function trapDialogFocus(event, dialog) {
  const focusable = [...dialog.querySelectorAll(FOCUSABLE_SELECTOR)]
    .filter((element) => element.hidden !== true && element.disabled !== true);
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus?.();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = dialog.ownerDocument?.activeElement ?? globalThis.document?.activeElement;
  if (event.shiftKey && (active === first || !focusable.includes(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !focusable.includes(active))) {
    event.preventDefault();
    first.focus();
  }
}

/** 只接受后端审批标识的固定格式，拒绝把任意 dataset 文本用于请求路径。 */
function canonicalApprovalId(value) {
  const normalized = String(value ?? "");
  return /^apr_[0-9a-f]{32}$/.test(normalized) ? normalized : "";
}

/** 倒计时只改写固定文本节点；任务到期仍必须等待后端 scheduler/CAS 给出权威终态。 */
function startCountdown(page) {
  stopCountdown(page);
  updateCountdowns(page);
  if (typeof globalThis.setInterval !== "function") return;
  const timer = globalThis.setInterval(() => {
    if (page.isConnected === false) {
      stopCountdown(page);
      return;
    }
    updateCountdowns(page);
  }, 30_000);
  countdownTimers.set(page, timer);
}

/**
 * 处理 stopCountdown 对应审批交互；前端只控制体验，最终权限由后端重新校验。
 */
function stopCountdown(page) {
  const timer = countdownTimers.get(page);
  if (timer !== undefined && typeof globalThis.clearInterval === "function") {
    globalThis.clearInterval(timer);
  }
  countdownTimers.delete(page);
}

/**
 * 处理 updateCountdowns 对应审批交互；前端只控制体验，最终权限由后端重新校验。
 */
function updateCountdowns(page) {
  for (const node of page.querySelectorAll("[data-approval-countdown]")) {
    const status = String(node.dataset?.status ?? "");
    const expiresAt = String(node.dataset?.expiresAt ?? "");
    if (status !== "pending") {
      node.textContent = "已结束";
      continue;
    }
    const seconds = Math.max(0, Math.ceil((Date.parse(expiresAt) - Date.now()) / 1000));
    if (!Number.isFinite(seconds) || seconds <= 0) {
      node.textContent = "等待超时收敛";
      continue;
    }
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.ceil((seconds % 3600) / 60);
    node.textContent = hours > 0 ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`;
  }
}
