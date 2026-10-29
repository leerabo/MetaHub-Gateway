/**
 * 模块职责：绑定 PAGE-098 筛选、分页、详情、Request Logs 跳转与固定处置 Dialog。
 * 安全边界：只从固定控件读取枚举和筛选；tenant、actor、revision、target、cursor、幂等键与
 * operation IDs 均由认证上下文、当前安全 DTO 或页面闭包恢复，不能由可篡改 DOM 提交。
 */

/**
 * 绑定 PAGE-098 页面事件。
 *
 * @param {object} handlers 主应用提供的状态转换与 API 调度回调。
 * @returns {void} 非目标导航没有节点时静默结束。
 */
export function bindSessionRiskInteractions(handlers = {}) {
  bindFilters(handlers);
  bindPaging(handlers);
  bindSelection(handlers);
  bindActionCommands(handlers);
  bindActionDialog(handlers);
  bindRequestLinks(handlers);
}

/** 绑定查询筛选；非法 datetime DOM 值收敛为空并由后端保持默认窗口。 */
function bindFilters(handlers) {
  const form = document.querySelector("#session-risk-filter-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    await handlers.onFilter?.({
      status: text(data.get("status")),
      scopeType: text(data.get("scopeType")),
      virtualKeyId: text(data.get("virtualKeyId")),
      fromTime: toIsoTime(data.get("fromTime")),
      toTime: toIsoTime(data.get("toTime")),
      limit: 20,
    });
  });
  document.querySelector("#session-risk-filter-reset")?.addEventListener("click", async () => {
    await handlers.onResetFilters?.();
  });
}

/** 绑定 opaque cursor 前后翻页；DOM 只表达命令，不读取 cursor。 */
function bindPaging(handlers) {
  document.querySelector("#session-risk-prev")?.addEventListener("click", async () => {
    await handlers.onPreviousPage?.();
  });
  document.querySelector("#session-risk-next")?.addEventListener("click", async () => {
    await handlers.onNextPage?.();
  });
}

/** 绑定当前页详情选择与关闭；score ID 会由闭包当前页再次核对。 */
function bindSelection(handlers) {
  for (const button of document.querySelectorAll("[data-session-risk-select]")) {
    button.addEventListener("click", async () => {
      await handlers.onSelectRisk?.(button.dataset.sessionRiskSelect ?? "");
    });
  }
  document.querySelector("#session-risk-close-detail")?.addEventListener("click", async () => {
    await handlers.onCloseDetail?.();
  });
}

/** 绑定固定动作；动作与 ID 还会经过状态机、strict client 和后端三层校验。 */
function bindActionCommands(handlers) {
  for (const button of document.querySelectorAll("[data-session-risk-action]")) {
    button.addEventListener("click", async () => {
      await handlers.onOpenAction?.({
        sessionRiskScoreId: button.dataset.sessionRiskScoreId ?? "",
        action: button.dataset.sessionRiskAction ?? "",
      });
    });
  }
}

/** 绑定 Dialog 提交、取消、Escape 和焦点陷阱；表单只读取固定 reason_code。 */
function bindActionDialog(handlers) {
  const dialog = document.querySelector("#session-risk-action-dialog");
  const form = document.querySelector("#session-risk-action-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    await handlers.onSubmitAction?.({ reasonCode: text(data.get("reasonCode")) });
  });
  for (const selector of ["#session-risk-action-close", "#session-risk-action-cancel"]) {
    document.querySelector(selector)?.addEventListener("click", async () => {
      await handlers.onCloseAction?.();
    });
  }
  if (!dialog) return;
  queueMicrotask(() => dialog.querySelector("select, button:not([disabled])")?.focus());
  dialog.addEventListener("keydown", async (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      await handlers.onCloseAction?.();
      return;
    }
    if (event.key === "Tab") trapDialogFocus(event, dialog);
  });
}

/** 把 Tab 循环限制在当前 Dialog，避免焦点落到被遮罩的工作区。 */
function trapDialogFocus(event, dialog) {
  const focusable = Array.from(dialog.querySelectorAll(
    "button:not([disabled]), select:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])",
  ));
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/** 绑定 request_id 跳转；目标导航与 GET 仍由后端 Session 权限和 tenant 校验。 */
function bindRequestLinks(handlers) {
  for (const button of document.querySelectorAll("[data-session-risk-request-id]")) {
    button.addEventListener("click", async () => {
      await handlers.onOpenRequest?.(button.dataset.sessionRiskRequestId ?? "");
    });
  }
}

/** 把 datetime-local 明确转换为 UTC ISO；空值或非法注入不进入筛选。 */
function toIsoTime(value) {
  const normalized = text(value).trim();
  if (!normalized) return "";
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

/** 把 FormData 可空值稳定转换为字符串，不展开 File 或任意对象。 */
function text(value) {
  return typeof value === "string" ? value : "";
}
