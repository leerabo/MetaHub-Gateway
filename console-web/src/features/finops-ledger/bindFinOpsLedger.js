/**
 * 模块职责：绑定 PAGE-074/075 的 DOM 交互并把用户意图交给主应用状态机。
 * 安全边界：本模块不保存 Token、tenant_id、幂等键或服务端 cursor；表单只读取允许的
 * 类型、金额、关联请求和原因，所有权限及业务校验仍由后端执行。
 */

/**
 * 绑定当前已渲染 FinOps 工作区。
 *
 * @param {object} handlers 主应用提供的异步意图处理器。
 * @param {Document|Element} root 可查询当前页面节点的根对象，默认浏览器 document。
 */
export function bindFinOpsLedgerInteractions(handlers, root = document) {
  const isCurrent = () => handlers.isCurrent?.() !== false;
  const filterForm = root.querySelector("#finops-filter-form");
  filterForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!isCurrent()) return;
    await handlers.onFilter?.(readFilterValues(filterForm));
  });
  root.querySelector("#finops-filter-reset")?.addEventListener("click", async () => {
    if (!isCurrent()) return;
    await handlers.onResetFilters?.();
  });
  root.querySelector("#finops-prev")?.addEventListener("click", async () => {
    if (!isCurrent()) return;
    await handlers.onPreviousPage?.();
  });
  root.querySelector("#finops-next")?.addEventListener("click", async () => {
    if (!isCurrent()) return;
    await handlers.onNextPage?.();
  });

  for (const button of root.querySelectorAll("[data-finops-entry-id]")) {
    button.addEventListener("click", async () => {
      if (!isCurrent()) return;
      await handlers.onSelectEntry?.(button.dataset.finopsEntryId);
    });
  }
  for (const button of root.querySelectorAll("[data-finops-reverse-entry-id]")) {
    button.addEventListener("click", async () => {
      if (!isCurrent()) return;
      await handlers.onOpenAdjustment?.(button.dataset.finopsReverseEntryId);
    });
  }
  for (const button of root.querySelectorAll("[data-finops-request-id]")) {
    button.addEventListener("click", async () => {
      if (!isCurrent()) return;
      await handlers.onOpenRequest?.(button.dataset.finopsRequestId);
    });
  }
  root.querySelector("#finops-close-detail")?.addEventListener("click", async () => {
    if (!isCurrent()) return;
    await handlers.onCloseDetail?.();
  });
  const detailDialog = root.querySelector("#finops-detail-dialog");
  detailDialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (isCurrent()) void handlers.onCloseDetail?.();
  });
  if (detailDialog?.open && typeof detailDialog.showModal === "function") {
    detailDialog.close();
    detailDialog.showModal();
    detailDialog.querySelector("#finops-close-detail")?.focus();
  }
  root.querySelector("#finops-open-adjustment")?.addEventListener("click", async () => {
    if (!isCurrent()) return;
    await handlers.onOpenAdjustment?.(null);
  });

  const dialog = root.querySelector("#finops-adjustment-dialog");
  /**
   * 统一关闭入口并交由页面状态机销毁原因和幂等状态，禁止 DOM 自行保留财务草稿。
   */
  const closeDialog = async () => {
    if (isCurrent()) await handlers.onCloseAdjustment?.();
  };
  root.querySelector("#finops-close-adjustment")?.addEventListener("click", closeDialog);
  root.querySelector("#finops-cancel-adjustment")?.addEventListener("click", closeDialog);
  dialog?.addEventListener("cancel", async (event) => {
    // 静态 open dialog 的浏览器行为不完全一致，显式阻止默认关闭后由状态机统一销毁 reason。
    event.preventDefault();
    await closeDialog();
  });
  dialog?.addEventListener("keydown", async (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      await closeDialog();
    }
  });

  const adjustmentForm = root.querySelector("#finops-adjustment-form");
  adjustmentForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!isCurrent() || handlers.isWriteCurrent?.() === false) return;
    await handlers.onSubmitAdjustment?.(readAdjustmentValues(adjustmentForm));
  });

  // 模板先输出 open 供无脚本状态和纯渲染测试识别；浏览器绑定时重新以 modal 打开，阻止背景误操作。
  if (dialog?.open) {
    if (typeof dialog.showModal === "function") {
      dialog.close();
      dialog.showModal();
    }
    // 对话框重绘后把焦点移入第一个可编辑字段；关闭后的触发器焦点由主应用回调恢复。
    dialog.querySelector("select:not([disabled]), input:not([disabled]), textarea:not([disabled])")?.focus();
  }
}

/**
 * 读取筛选白名单；FormData 中即使被扩展注入 tenant_id 或任意字段也不会出现在返回对象。
 */
function readFilterValues(form) {
  const data = new FormData(form);
  return {
    startDate: optionalText(data.get("startDate")),
    endDate: optionalText(data.get("endDate")),
    entryType: optionalText(data.get("entryType")),
    direction: optionalText(data.get("direction")),
    quotaType: optionalText(data.get("quotaType")),
    requestId: optionalText(data.get("requestId")),
    priceSnapshotId: optionalText(data.get("priceSnapshotId")),
    relatedEntryId: optionalText(data.get("relatedEntryId")),
    limit: 20,
  };
}

/**
 * 读取调整表单的允许字段。
 * 冲正目标来自页面状态而非隐藏输入，因此 DOM 篡改不能把任意 entry_id 注入提交载荷。
 */
function readAdjustmentValues(form) {
  const data = new FormData(form);
  return {
    adjustmentType: optionalText(data.get("adjustmentType")),
    amount: String(data.get("amount") ?? ""),
    relatedRequestId: optionalText(data.get("relatedRequestId")),
    reason: String(data.get("reason") ?? ""),
  };
}

/**
 * 规范化可选文本，不改变金额或原因内部字符；金额精度和原因字节边界由 API client/后端校验。
 */
function optionalText(value) {
  return String(value ?? "").trim();
}
