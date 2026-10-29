/**
 * 模块职责：保存 PAGE-081 对话框触发器的稳定身份，并在局部 DOM 重绘后解析当前可聚焦节点。
 * 安全边界：恢复状态只驻留内存且绑定完整 Session owner；规则公开 ID 不进入 URL 或 Storage。
 */

// 三类触发器使用固定种类，避免把任意选择器或未经转义的 DOM 属性拼进查询表达式。
const RESTORE_TARGET_KINDS = Object.freeze({
  create: Object.freeze({ selector: "button#error-map-create", attribute: "" }),
  edit: Object.freeze({ selector: "button[data-error-map-edit]", attribute: "data-error-map-edit" }),
  status: Object.freeze({ selector: "button[data-error-map-status]", attribute: "data-error-map-status" }),
});

/**
 * 提取触发器可跨局部重绘复用的公开身份。
 *
 * @param {Element | null | undefined} target 原始触发器节点。
 * @returns {{kind: string, resourceId: string} | null} 固定种类和公开资源 ID；非 PAGE-081 触发器返回 null。
 */
function readStableRestoreIdentity(target) {
  if (!target) {
    return null;
  }
  // 创建入口仍需通过后续 panel 归属、连接状态和可聚焦性校验，不能只信任公开 DOM id。
  if (target.id === "error-map-create") {
    return { kind: "create", resourceId: "" };
  }
  for (const kind of ["edit", "status"]) {
    const attribute = RESTORE_TARGET_KINDS[kind].attribute;
    const resourceId = target.getAttribute?.(attribute) ?? "";
    if (resourceId) {
      return { kind, resourceId };
    }
  }
  return null;
}

/**
 * 捕获对话框触发器的稳定身份和完整认证 owner。
 *
 * @param {Element | null | undefined} target 打开对话框的创建、编辑或状态按钮。
 * @param {object} options 冻结 owner、当前导航与 PAGE-081 面板。
 * @returns {Readonly<object> | null} 一次性内存 descriptor；节点或身份不可信时失败关闭。
 */
export function createErrorMapDialogRestoreState(
  target,
  { ownerIdentity, navigation, panel } = {},
) {
  const stableIdentity = readStableRestoreIdentity(target);
  if (
    !stableIdentity
    || typeof ownerIdentity !== "string"
    || !ownerIdentity
    || navigation !== "error-maps"
    || !validFocusTarget(target, panel)
  ) {
    return null;
  }
  // descriptor 不保留旧 DOM 节点；panel.innerHTML 重绘后只能按相同 owner 和公开稳定身份重新定位。
  return Object.freeze({
    ownerIdentity,
    navigation,
    ...stableIdentity,
  });
}

/**
 * 解析对话框关闭后可安全聚焦的当前触发器。
 *
 * @param {Readonly<object> | null} restoreState 打开对话框时捕获的一次性内存 descriptor。
 * @param {object} options 当前 owner、导航与 PAGE-081 面板；测试可传入最小 DOM 替身。
 * @returns {Element | null} 相同 owner 下精确匹配、可用且仍连接的重绘节点。
 */
export function resolveErrorMapDialogRestoreTarget(
  restoreState,
  { ownerIdentity, navigation, panel } = {},
) {
  // 完整 owner 或导航任一变化都禁止恢复焦点，避免跨租户、跨 Session 或跨页面操作旧资源。
  if (
    !restoreState
    || !ownerIdentity
    || restoreState.ownerIdentity !== ownerIdentity
    || restoreState.navigation !== navigation
    || navigation !== "error-maps"
    || !validErrorMapPanel(panel)
  ) {
    return null;
  }

  const targetKind = RESTORE_TARGET_KINDS[restoreState.kind];
  if (!targetKind) {
    return null;
  }
  if (restoreState.kind === "create") {
    return validFocusTarget(panel.querySelector?.(targetKind.selector), panel);
  }

  // 先按固定属性收窄集合，再用 JavaScript 精确比较公开 ID，避免把资源值插入 CSS 选择器。
  const candidates = panel.querySelectorAll?.(targetKind.selector) ?? [];
  for (const candidate of candidates) {
    if (
      candidate?.getAttribute?.(targetKind.attribute) === restoreState.resourceId
      && validFocusTarget(candidate, panel)
    ) {
      return candidate;
    }
  }
  return null;
}

/**
 * 校验 PAGE-081 面板是当前 document 中仍连接的真实面板。
 *
 * @param {Element | null | undefined} panel PAGE-081 面板。
 * @returns {boolean} 面板连接、类型和 ownerDocument 均可信时返回 true。
 */
function validErrorMapPanel(panel) {
  return Boolean(
    panel?.isConnected
    && panel.ownerDocument
    && panel.matches?.(".error-maps-panel")
    && typeof panel.contains === "function",
  );
}

/**
 * 校验候选是当前 PAGE-081 面板内仍可聚焦的真实按钮。
 *
 * @param {Element | null | undefined} candidate 重绘后的候选按钮。
 * @param {Element | object} panel 当前 PAGE-081 面板或测试替身。
 * @returns {Element | null} 可安全聚焦的候选；断开、禁用或伪造节点返回 null。
 */
function validFocusTarget(candidate, panel) {
  return validErrorMapPanel(panel)
    && candidate?.tagName === "BUTTON"
    && candidate.isConnected
    && candidate.ownerDocument === panel.ownerDocument
    && panel.contains(candidate)
    && candidate.disabled !== true
    && typeof candidate.focus === "function"
    ? candidate
    : null;
}
