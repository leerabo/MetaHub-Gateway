/**
 * 绑定 PAGE-107 的纯 UI 意图。
 *
 * 本模块不保存 Token、tenant、问题或回答；dataset 只携带固定数组下标，真正 source/href 必须由主应用
 * 从当前 owner 状态重新读取并与最新后端导航求交。
 */
export function bindAdminAssistantInteractions(handlers, root = document) {
  const scope = root?.querySelector ? root : null;
  if (!scope) return false;
  const form = scope.querySelector("[data-admin-assistant-form]");
  const textarea = scope.querySelector("[data-admin-assistant-question]");

  textarea?.addEventListener("input", () => {
    handlers.onDraft?.(textarea.value);
  });
  for (const button of scope.querySelectorAll("[data-admin-assistant-suggestion]")) {
    button.addEventListener("click", () => {
      const index = canonicalIndex(button.dataset.adminAssistantSuggestion);
      if (index !== null) handlers.onSuggestion?.(index);
    });
  }
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (textarea) handlers.onDraft?.(textarea.value);
    await handlers.onSubmit?.();
  });
  scope.querySelector("[data-admin-assistant-cancel]")?.addEventListener("click", () => {
    handlers.onCancel?.();
  });
  scope.querySelector("[data-admin-assistant-clear]")?.addEventListener("click", () => {
    handlers.onClear?.();
  });
  for (const link of scope.querySelectorAll("a[data-admin-assistant-source-index]")) {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      const index = canonicalIndex(link.dataset.adminAssistantSourceIndex);
      await handlers.onOpenSource?.(index, link.getAttribute("href") ?? "");
    });
  }
  return true;
}

/** owner 漂移时同步擦除正文节点并移除页面根，避免 abort 回调观察旧租户可见事实。 */
export function clearAdminAssistantDom(root = globalThis.document) {
  const scope = root?.querySelector ? root : null;
  if (!scope) return false;
  const page = scope.matches?.("[data-admin-assistant-root]")
    ? scope
    : scope.querySelector("[data-admin-assistant-root]");
  if (!page) return false;
  const textarea = page.querySelector("[data-admin-assistant-question]");
  if (textarea) textarea.value = "";
  for (const element of page.querySelectorAll("[data-admin-assistant-sensitive]")) {
    element.textContent = "";
  }
  page.remove();
  return true;
}

/** 解析 dataset 中的规范数组下标，拒绝负数、前导零和非数字值。 */
function canonicalIndex(value) {
  const normalized = String(value ?? "");
  return /^(?:0|[1-9][0-9]*)$/.test(normalized) ? Number(normalized) : null;
}
