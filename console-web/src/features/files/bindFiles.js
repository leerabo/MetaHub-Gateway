/**
 * 模块职责：绑定文件选择、上传、状态筛选与私有 cursor 翻页交互。
 * 安全边界：原生 File 只从当前 input 传给单次上传回调，不进入页面状态、URL、Storage、日志或
 * dataset；tenant、权限、cursor 和后端状态均由主应用 owner 与 Control API 恢复。
 */

import {
  FILE_UPLOAD_MAX_BYTES,
  isSupportedUploadName,
} from "../../lib/files.js";

/**
 * 绑定当前文件工作台事件。
 *
 * @param {object} handlers 主应用提供的 owner 校验、API 调度和状态转换回调。
 * @returns {void} 非目标导航没有节点时静默结束。
 */
export function bindFileInteractions(handlers = {}) {
  bindUpload(handlers);
  bindStatusFilter(handlers);
  bindPaging(handlers);
}

/**
 * 返回当前工作台是否持有未提交的 File 选择。
 * 定时全页刷新据此保留浏览器不可重建的 file input；文件对象本身不会离开 DOM。
 */
export function fileUploadHasSelection(root = globalThis.document) {
  const input = root?.querySelector?.("#file-upload-input");
  return Boolean(input?.files?.length);
}

/** 绑定原生单文件选择和提交；所有提示通过 textContent 写入，文件名不进入 HTML 字符串。 */
function bindUpload(handlers) {
  const form = document.querySelector("#file-upload-form");
  const input = document.querySelector("#file-upload-input");
  const submit = form?.querySelector("[data-file-upload-submit]");
  const selection = form?.querySelector("[data-file-selection]");
  const live = document.querySelector("[data-file-upload-live]");
  if (!form || !input || !submit) return;

  const updateSelection = () => {
    const file = input.files?.[0] ?? null;
    const validation = validateSelection(file);
    // 文件名是必要交互字段，但必须只通过 textContent 放入当前 owner DOM。
    if (selection) selection.textContent = file?.name || "未选择文件";
    submit.disabled = !validation.valid || form.getAttribute("aria-busy") === "true";
    if (live) live.textContent = validation.message;
  };
  input.addEventListener("change", updateSelection);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = input.files?.[0] ?? null;
    const validation = validateSelection(file);
    if (!validation.valid) {
      if (live) live.textContent = validation.message;
      return;
    }
    lockUploadForm(form, "正在上传、解析并执行安全预扫描");
    try {
      await handlers.onUpload?.(file);
    } finally {
      // 全页 refresh 通常会替换 form；只有旧节点仍连接时才恢复，避免 finally 解锁新 owner DOM。
      if (form.isConnected) {
        form.removeAttribute("aria-busy");
        input.disabled = false;
        updateSelection();
      }
    }
  });
}

/** 绑定固定状态筛选；DOM 中未知值由页面状态机收敛为空筛选。 */
function bindStatusFilter(handlers) {
  const form = document.querySelector("#file-status-filter-form");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const status = typeof data.get("status") === "string" ? data.get("status") : "";
    await handlers.onFilter?.(status);
  });
  document.querySelector("#file-status-filter-reset")?.addEventListener("click", async () => {
    await handlers.onResetFilter?.();
  });
}

/** 绑定前后翻页命令；按钮不携带 opaque cursor。 */
function bindPaging(handlers) {
  document.querySelector("#file-page-prev")?.addEventListener("click", async () => {
    await handlers.onPreviousPage?.();
  });
  document.querySelector("#file-page-next")?.addEventListener("click", async () => {
    await handlers.onNextPage?.();
  });
}

/** 本地只执行低成本扩展名和 20 MiB 体验检查；MIME、魔数与结构仍由服务端决定。 */
function validateSelection(file) {
  if (!file) return { valid: false, message: "请选择一个文件" };
  if (!isSupportedUploadName(file.name)) {
    return { valid: false, message: "仅支持 PDF、DOCX 和 UTF-8 TXT" };
  }
  if (!Number.isSafeInteger(file.size) || file.size < 1) {
    return { valid: false, message: "文件为空或无法读取" };
  }
  if (file.size > FILE_UPLOAD_MAX_BYTES) {
    return { valid: false, message: "文件超过 20 MiB 上限" };
  }
  return { valid: true, message: "文件已就绪" };
}

/** POST 发出前立即锁定全部上传控件，关闭双击和选择变更窗口。 */
function lockUploadForm(form, message) {
  form.setAttribute("aria-busy", "true");
  for (const control of form.querySelectorAll("button, input")) control.disabled = true;
  const live = document.querySelector("[data-file-upload-live]");
  if (live) live.textContent = message;
}
