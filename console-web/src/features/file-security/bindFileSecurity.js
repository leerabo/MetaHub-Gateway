/**
 * 模块职责：绑定 PAGE-103 文件选择、扫描和 CSV 下载命令。
 * 安全边界：DOM 只提供当前页规范 file ID；tenant、Token、权限 owner、报告和 Blob 均由主应用
 * 闭包恢复，按钮隐藏或 disabled 只改善体验，不替代 Control API 的后端权限校验。
 */

/**
 * 绑定文件安全页面交互。
 *
 * @param {object} handlers 主应用提供的 owner 校验、API 调度和状态转换回调。
 * @returns {void} 非目标页面没有节点时静默结束。
 */
export function bindFileSecurityInteractions(handlers = {}) {
  bindFileSelection(handlers);
  bindPaging(handlers);
  bindScan(handlers);
  bindExport(handlers);
}

/** 前后翻页按钮只触发状态命令，opaque cursor 由主应用私有闭包恢复。 */
function bindPaging(handlers) {
  document.querySelector("[data-file-security-previous]")?.addEventListener("click", async () => {
    lockCommandRegion();
    await handlers.onPreviousPage?.();
  });
  document.querySelector("[data-file-security-next]")?.addEventListener("click", async () => {
    lockCommandRegion();
    await handlers.onNextPage?.();
  });
}

/** 文件选择只读取规范 data ID；状态机仍会验证该 ID 必须来自当前 ready 列表。 */
function bindFileSelection(handlers) {
  for (const button of document.querySelectorAll("[data-file-security-select]")) {
    button.addEventListener("click", async () => {
      const fileId = String(button.dataset.fileSecuritySelect ?? "");
      if (!/^file_[0-9a-f]{32}$/.test(fileId)) return;
      lockCommandRegion();
      await handlers.onSelect?.(fileId);
    });
  }
}

/** 扫描点击后立即锁住所有命令，关闭全页重绘前的双击窗口。 */
function bindScan(handlers) {
  document.querySelector("[data-file-security-scan]")?.addEventListener("click", async () => {
    lockCommandRegion("正在执行文件安全扫描");
    await handlers.onScan?.();
  });
}

/** CSV 点击后立即锁住命令；Object URL 的创建与 revoke 由 lib 在同一调用栈完成。 */
function bindExport(handlers) {
  document.querySelector("[data-file-security-export]")?.addEventListener("click", async () => {
    lockCommandRegion("正在生成文件安全报告 CSV");
    await handlers.onExport?.();
  });
}

/** 锁定当前 PAGE-103 命令区并用 textContent 更新可访问状态，不重建文件名或报告 DOM。 */
function lockCommandRegion(message = "正在切换文件安全报告") {
  const root = document.querySelector("[data-file-security-root]");
  root?.setAttribute("aria-busy", "true");
  for (const control of root?.querySelectorAll("button") ?? []) control.disabled = true;
  const feedback = root?.querySelector(".file-security-feedback");
  if (feedback) feedback.textContent = message;
}
