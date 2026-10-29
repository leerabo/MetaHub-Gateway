/**
 * 模块职责：绑定 PAGE-077/078 的筛选、分页、详情和治理动作交互。
 * 安全边界：本模块只从固定控件读取白名单字段；tenant、actor、状态、到期时间、幂等键和
 * operation IDs 均由认证上下文、后端或页面闭包恢复，绝不从可篡改 DOM 接受。
 */

/**
 * 绑定成本异常页面事件。
 *
 * @param {object} handlers 主应用提供的状态转换和 API 调度回调。
 * @returns {void} 页面节点不存在时静默返回，兼容其他导航项和 loading 状态。
 */
export function bindCostAnomalyInteractions(handlers = {}, { isCurrent = () => true } = {}) {
  handlers = Object.fromEntries(Object.entries(handlers).map(([name, handler]) => [
    name, (...args) => isCurrent() ? handler(...args) : undefined,
  ]));
  bindFilters(handlers);
  bindPaging(handlers);
  bindSelection(handlers);
  bindActionCommands(handlers);
  bindActionDialog(handlers);
  bindRequestLinks(handlers);
}

/** 绑定筛选提交和清空；筛选只包含冻结 GET 参数。 */
function bindFilters(handlers) {
  const form = document.querySelector("#cost-anomaly-filter-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    await handlers.onFilter?.({
      status: text(data.get("status")),
      ruleType: text(data.get("ruleType")),
      severity: text(data.get("severity")),
      environmentId: text(data.get("environmentId")),
      virtualKeyId: text(data.get("virtualKeyId")),
      fromTime: toIsoTime(data.get("fromTime")),
      toTime: toIsoTime(data.get("toTime")),
      limit: 20,
    });
  });
  document.querySelector("#cost-anomaly-filter-reset")?.addEventListener("click", async () => {
    await handlers.onResetFilters?.();
  });
}

/** 绑定 opaque cursor 前后翻页；DOM 只表达命令，不读取或保存 cursor 原文。 */
function bindPaging(handlers) {
  document.querySelector("#cost-anomaly-prev")?.addEventListener("click", async () => {
    await handlers.onPreviousPage?.();
  });
  document.querySelector("#cost-anomaly-next")?.addEventListener("click", async () => {
    await handlers.onNextPage?.();
  });
}

/**
 * 绑定 PAGE-077 列表与 PAGE-078 卡片的详情命令。
 * ID 仍会由页面状态核对是否属于当前 tenant-qualified 响应页，data 属性本身不是权限依据。
 */
function bindSelection(handlers) {
  for (const button of document.querySelectorAll("[data-cost-anomaly-select]")) {
    button.addEventListener("click", async () => {
      await handlers.onSelectAnomaly?.(button.dataset.costAnomalySelect ?? "");
    });
  }
  document.querySelector("#cost-anomaly-close-detail")?.addEventListener("click", async () => {
    await handlers.onCloseDetail?.();
  });
}

/** 绑定固定动作枚举；异常 ID 和动作都会由页面状态及 API client 再次校验。 */
function bindActionCommands(handlers) {
  for (const button of document.querySelectorAll("[data-cost-anomaly-action]")) {
    button.addEventListener("click", async () => {
      await handlers.onOpenAction?.({
        anomalyId: button.dataset.costAnomalyId ?? "",
        action: button.dataset.costAnomalyAction ?? "",
      });
    });
  }
}

/**
 * 绑定动作表单。
 * number 控件显式转换为整数或 null，非法/小数值产生 NaN 并由 API client 在 fetch 前失败关闭。
 */
function bindActionDialog(handlers) {
  const form = document.querySelector("#cost-anomaly-action-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    await handlers.onSubmitAction?.({
      dismissReason: text(data.get("dismissReason")),
      durationMinutes: integerOrNull(data.get("durationMinutes")),
      rpmLimit: integerOrNull(data.get("rpmLimit")),
      tpmLimit: integerOrNull(data.get("tpmLimit")),
      concurrencyLimit: integerOrNull(data.get("concurrencyLimit")),
    });
  });
  for (const selector of ["#cost-anomaly-action-close", "#cost-anomaly-action-cancel"]) {
    document.querySelector(selector)?.addEventListener("click", async () => {
      await handlers.onCloseAction?.();
    });
  }
}

/** 绑定代表 request_id 跳转；目标 Request Logs 查询仍由后端按当前 Session tenant 校验。 */
function bindRequestLinks(handlers) {
  for (const button of document.querySelectorAll("[data-cost-anomaly-request-id]")) {
    button.addEventListener("click", async () => {
      await handlers.onOpenRequest?.(button.dataset.costAnomalyRequestId ?? "");
    });
  }
}

/** 把 datetime-local 转成明确 UTC ISO；空值保持未筛选，非法 DOM 注入收敛为空值。 */
function toIsoTime(value) {
  const normalized = text(value).trim();
  if (!normalized) {
    return "";
  }
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

/**
 * 把可空 number 控件转换为安全整数。
 * 非十进制整数不做宽松 parseInt 截断，而是返回 NaN 交给客户端统一校验拒绝。
 */
function integerOrNull(value) {
  const normalized = text(value).trim();
  if (!normalized) {
    return null;
  }
  if (!/^(?:0|[1-9][0-9]*)$/.test(normalized)) {
    return Number.NaN;
  }
  const parsed = Number(normalized);
  return Number.isSafeInteger(parsed) ? parsed : Number.NaN;
}

/** 把 FormData 可空值稳定转换为字符串，不展开 File 或任意对象字段。 */
function text(value) {
  return typeof value === "string" ? value : "";
}
