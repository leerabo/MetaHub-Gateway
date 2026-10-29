/**
 * 模块职责：绑定 PAGE-083 的筛选、刷新、分页、重放与归档确认交互。
 * 安全边界：本模块只传递固定命令和当前表单白名单；cursor、tenant、actor、幂等键、状态迁移
 * 与归档时间均由页面闭包、认证上下文或后端恢复，不能从 data 属性或表单注入。
 */

/**
 * 绑定 Webhook 死信页面交互。
 *
 * @param {object} handlers 主应用提供的状态转换和网络调度回调。
 * @returns {void} 页面 DOM 不存在时静默返回，保证其他导航零副作用。
 */
export function bindWebhookDeadLetterInteractions(handlers = {}) {
  bindFilters(handlers);
  bindRefreshAndPaging(handlers);
  bindReplayCommands(handlers);
  bindArchiveDialog(handlers);
}

/**
 * 绑定状态分段控件和筛选表单。
 * 切换状态与提交菜单都会由状态机清空旧 cursor 和快照，不允许沿用旧分页位置。
 */
function bindFilters(handlers) {
  const form = document.querySelector("#webhook-dead-letter-filter-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await handlers.onFilter?.(filterValues(form));
  });
  for (const button of document.querySelectorAll("[data-webhook-dead-letter-status]")) {
    button.addEventListener("click", async () => {
      const values = filterValues(form);
      await handlers.onFilter?.({
        ...values,
        status: text(button.dataset.webhookDeadLetterStatus),
      });
    });
  }
}

/** 绑定图标刷新和 opaque cursor 分页；DOM 中从不保存或读取 cursor 原文。 */
function bindRefreshAndPaging(handlers) {
  document.querySelector("[data-webhook-dead-letter-refresh]")?.addEventListener("click", async () => {
    await handlers.onRefresh?.();
  });
  document.querySelector("#webhook-dead-letter-prev")?.addEventListener("click", async () => {
    await handlers.onPreviousPage?.();
  });
  document.querySelector("#webhook-dead-letter-next")?.addEventListener("click", async () => {
    await handlers.onNextPage?.();
  });
}

/**
 * 绑定重放命令。
 * data 属性仅携带当前行死信 ID；页面状态会再次核对该 ID 与后端 allowed_actions，再恢复来源 delivery。
 */
function bindReplayCommands(handlers) {
  for (const button of document.querySelectorAll("[data-webhook-dead-letter-replay]")) {
    button.addEventListener("click", async () => {
      await handlers.onReplay?.(text(button.dataset.webhookDeadLetterReplay));
    });
  }
}

/**
 * 绑定归档确认框的打开、取消、Escape 和提交。
 * 最终归档 ID 由状态机确认框恢复，表单不包含 hidden ID、tenant 或 actor 字段。
 */
function bindArchiveDialog(handlers) {
  for (const button of document.querySelectorAll("[data-webhook-dead-letter-archive]")) {
    button.addEventListener("click", async () => {
      await handlers.onOpenArchive?.(text(button.dataset.webhookDeadLetterArchive));
    });
  }
  for (const button of document.querySelectorAll("[data-webhook-dead-letter-archive-cancel]")) {
    button.addEventListener("click", async () => {
      await handlers.onCloseArchive?.();
    });
  }
  const dialog = document.querySelector("#webhook-dead-letter-archive-dialog");
  dialog?.addEventListener("cancel", async (event) => {
    event.preventDefault();
    await handlers.onCloseArchive?.();
  });
  document.querySelector("#webhook-dead-letter-archive-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await handlers.onConfirmArchive?.();
  });
  activateArchiveDialog(dialog);
}

/** 从筛选表单读取三个冻结字段；缺失表单返回默认值且不读取任意额外控件。 */
function filterValues(form) {
  if (!form) {
    return { status: "all", eventType: "", webhookEndpointId: "", limit: 50 };
  }
  const data = new FormData(form);
  return {
    status: text(form.querySelector('[data-webhook-dead-letter-status][aria-pressed="true"]')?.dataset.webhookDeadLetterStatus) || "all",
    eventType: text(data.get("eventType")),
    webhookEndpointId: text(data.get("webhookEndpointId")),
    limit: 50,
  };
}

/**
 * 使用原生 showModal 激活确认框；不支持 dialog API 的旧浏览器回退 open 属性，仍保留键盘按钮。
 * showModal 只作用于当前 PAGE-083 节点，不读取或传递任何业务字段。
 */
function activateArchiveDialog(dialog) {
  if (!dialog || dialog.open) {
    return;
  }
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
    return;
  }
  dialog.setAttribute("open", "");
}
/** 把 FormData 或 dataset 可空值稳定收敛为字符串，不展开 File 或对象字段。 */
function text(value) {
  return typeof value === "string" ? value : "";
}
