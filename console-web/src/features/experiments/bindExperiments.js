/**
 * PAGE-105 DOM 交互绑定。
 *
 * 表单草稿、正文指纹和 UUIDv4 幂等键仅存在于当前 Dialog 事件闭包；离页、撤权或 Shell
 * 重建 DOM 后不可恢复，且任何值都不会写入 URL、Storage、日志或页面状态 snapshot。
 */

import {
  createExperimentIdempotencyKey,
  experimentCreateFingerprint,
  normalizeCreateExperimentRequest,
} from "../../lib/experiments.js";

// 绑定层只接受状态机已经投影的实验 ID 和固定筛选枚举，未知 DOM 值不会传给业务 handler。
const EXPERIMENT_ID = /^exp_[0-9a-f]{32}$/;
const STATUSES = new Set(["active", "collecting_feedback", "completed"]);

/** 在当前 PAGE-105 根节点绑定列表与创建交互；无授权 DOM 时不产生任何监听器。 */
export function bindExperimentInteractions(handlers = {}, root = globalThis.document) {
  const workspace = root?.querySelector?.("[data-experiments-root]");
  if (!workspace) return false;
  bindList(workspace, handlers);
  bindCreate(workspace, handlers);
  return true;
}

/** 身份或导航 owner 漂移时同步移除整个 PAGE-105 根节点，避免旧租户事实等待全页刷新才消失。 */
export function clearExperimentDom(root = globalThis.document) {
  root?.querySelector?.("[data-experiments-root]")?.remove?.();
}

/** 绑定筛选、显式刷新、私有 cursor 分页和当前列表内详情选择。 */
function bindList(workspace, handlers) {
  const current = () => workspace.isConnected !== false && (handlers.isCurrent?.("read") ?? true);
  workspace.querySelector("[data-experiment-status]")?.addEventListener("change", async (event) => {
    if (!current()) return;
    const value = String(event.currentTarget?.value ?? "");
    await handlers.onFilter?.(STATUSES.has(value) ? value : "");
  });
  workspace.querySelector("[data-experiment-refresh]")?.addEventListener("click", async () => {
    if (!current()) return;
    await handlers.onRefresh?.();
  });
  workspace.querySelector("[data-experiment-previous]")?.addEventListener("click", async () => {
    if (!current()) return;
    await handlers.onPreviousPage?.();
  });
  workspace.querySelector("[data-experiment-next]")?.addEventListener("click", async () => {
    if (!current()) return;
    await handlers.onNextPage?.();
  });
  for (const button of workspace.querySelectorAll("[data-experiment-select]")) {
    button.addEventListener("click", async () => {
      if (!current() || button.isConnected === false) return;
      const experimentId = String(button.dataset.experimentSelect ?? "");
      if (EXPERIMENT_ID.test(experimentId)) await handlers.onSelect?.(experimentId);
    });
  }
}

/** 绑定创建 Dialog、焦点边界、作用域联动和模糊网络结果下的幂等重试。 */
function bindCreate(workspace, handlers) {
  const dialog = workspace.querySelector("[data-experiment-dialog]");
  const form = dialog?.querySelector("[data-experiment-create-form]");
  const openButton = workspace.querySelector("[data-experiment-create]");
  if (!dialog || !form || !openButton) return;
  const current = () => workspace.isConnected !== false && form.isConnected !== false
    && (handlers.isCurrent?.("create") ?? true);
  const closeButtons = dialog.querySelectorAll(
    "[data-experiment-create-close], [data-experiment-create-cancel]",
  );
  const retryButton = dialog.querySelector("[data-experiment-create-retry]");
  let pendingFingerprint = "";
  let pendingIdempotencyKey = "";
  let opener = null;

  /** 打开当前 owner 的 Dialog，并从空草稿和空幂等意图开始。 */
  const open = async () => {
    if (!current()) return;
    if (await handlers.onOpenCreate?.() !== true) return;
    if (!current()) return;
    opener = openButton;
    pendingFingerprint = "";
    pendingIdempotencyKey = "";
    form.reset();
    dialog.hidden = false;
    syncFeedback(form);
    syncScopedOptions(form);
    form.elements.name?.focus();
  };
  /** 仅在状态机允许关闭时擦除草稿和幂等键，并把焦点还给 opener。 */
  const close = async () => {
    if (!current()) return;
    if (await handlers.onCloseCreate?.() !== true) return;
    if (!current()) return;
    pendingFingerprint = "";
    pendingIdempotencyKey = "";
    form.reset();
    dialog.hidden = true;
    opener?.focus?.();
  };
  openButton.addEventListener("click", open);
  for (const button of closeButtons) button.addEventListener("click", close);
  retryButton?.addEventListener("click", async () => {
    if (!current()) return;
    await handlers.onRefresh?.();
  });
  dialog.addEventListener("keydown", async (event) => {
    if (!current()) return;
    if (event.key === "Escape") {
      event.preventDefault();
      await close();
      return;
    }
    if (event.key === "Tab") containFocus(dialog, event);
  });
  form.elements.project_id?.addEventListener("change", () => { if (current()) syncScopedOptions(form); });
  form.elements.environment_id?.addEventListener("change", () => { if (current()) syncScopedOptions(form); });
  form.elements.feedback_collection_enabled?.addEventListener("change", () => { if (current()) syncFeedback(form); });
  form.elements.name?.addEventListener("input", () => { if (current()) validateName(form); });
  for (const name of ["model_a_id", "model_b_id"]) {
    form.elements[name]?.addEventListener("change", () => { if (current()) validateDistinctModels(form); });
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!current()) return;
    validateName(form);
    validateDistinctModels(form);
    if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
    let request;
    try {
      request = experimentCreateRequestFromFormData(new FormData(form));
    } catch {
      setLive(form, "请检查实验配置");
      return;
    }
    lockForm(form, true);
    try {
      const fingerprint = experimentCreateFingerprint(request);
      if (fingerprint !== pendingFingerprint) {
        // 先取得安全随机键再提交闭包 owner；随机源失败时不留下“有指纹、无键”的半状态。
        const nextIdempotencyKey = createExperimentIdempotencyKey();
        pendingFingerprint = fingerprint;
        pendingIdempotencyKey = nextIdempotencyKey;
      }
      const snapshot = await handlers.onCreate?.(request, pendingIdempotencyKey);
      if (!current()) return;
      if (snapshot?.createState === "success") {
        pendingFingerprint = "";
        pendingIdempotencyKey = "";
        form.reset();
      } else if (snapshot?.createError) {
        setLive(
          form,
          `${snapshot.createError.message} · request_id: ${snapshot.createError.requestId}`,
        );
      }
    } catch {
      if (current()) setLive(form, "实验创建暂不可用");
    } finally {
      if (current()) lockForm(form, false);
    }
  });
  syncFeedback(form);
  syncScopedOptions(form);
}

/** 把 FormData exact 投影为整数 POST DTO，不注入 tenant、actor 或实验 ID。 */
export function experimentCreateRequestFromFormData(data) {
  const feedbackEnabled = data?.get?.("feedback_collection_enabled") === "on";
  const request = {
    name: text(data?.get?.("name")),
    project_id: text(data?.get?.("project_id")),
    environment_id: text(data?.get?.("environment_id")),
    model_alias_id: text(data?.get?.("model_alias_id")),
    model_a_id: text(data?.get?.("model_a_id")),
    model_b_id: text(data?.get?.("model_b_id")),
    target_sample_count: strictInteger(data?.get?.("target_sample_count")),
    traffic_sample_bps: strictInteger(data?.get?.("traffic_sample_bps")),
    model_a_allocation_bps: strictInteger(data?.get?.("model_a_allocation_bps")),
    sampling_duration_seconds: strictInteger(data?.get?.("sampling_duration_seconds")),
    feedback_collection_enabled: feedbackEnabled,
    feedback_window_seconds: feedbackEnabled
      ? strictInteger(data?.get?.("feedback_window_seconds"))
      : 0,
  };
  return normalizeCreateExperimentRequest(request);
}

/** 保持反馈开关与窗口秒数成对，关闭时固定提交整数零。 */
function syncFeedback(form) {
  const enabled = Boolean(form.elements.feedback_collection_enabled?.checked);
  const input = form.elements.feedback_window_seconds;
  if (!input) return;
  input.disabled = !enabled;
  if (!enabled) input.value = "0";
  else if (input.value === "0" || !input.value) input.value = "86400";
}

/** 根据所选 Environment ID/code 隐藏不属于该作用域的 Alias 候选。 */
function syncScopedOptions(form) {
  const projectId = String(form.elements.project_id?.value ?? "");
  const environmentSelect = form.elements.environment_id;
  filterOptions(environmentSelect, (option) => (
    !option.dataset.projectId || !projectId || option.dataset.projectId === projectId
  ));
  const environmentId = String(environmentSelect?.value ?? "");
  const environmentCode = String(environmentSelect?.selectedOptions?.[0]?.dataset?.code ?? "");
  const aliasSelect = form.elements.model_alias_id;
  filterOptions(aliasSelect, (option) => (
    (!option.dataset.projectId || !projectId || option.dataset.projectId === projectId)
    && matchesEnvironmentOption(option, environmentId, environmentCode)
  ));
}

/** Alias 可按新 environment_id 或兼容 environment code 归属，任一路径命中即可展示。 */
function matchesEnvironmentOption(option, environmentId, environmentCode) {
  const ownerId = String(option.dataset.environmentId ?? "");
  const ownerCode = String(option.dataset.environment ?? "");
  if (!ownerId && !ownerCode) return true;
  return Boolean(
    (ownerId && environmentId && ownerId === environmentId)
    || (ownerCode && environmentCode && ownerCode === environmentCode)
  );
}

/** 更新 select option 的 hidden/disabled 状态，并清除已经越过作用域的选择。 */
function filterOptions(select, predicate) {
  if (!select) return;
  for (const option of select.options) {
    if (!option.value) continue;
    option.hidden = !predicate(option);
    option.disabled = option.hidden;
    if (option.hidden && option.selected) select.value = "";
  }
}

/** 在浏览器原生校验中拒绝同一个模型同时成为 A 与 B。 */
function validateDistinctModels(form) {
  const modelA = form.elements.model_a_id;
  const modelB = form.elements.model_b_id;
  const duplicate = modelA?.value && modelA.value === modelB?.value;
  modelB?.setCustomValidity?.(duplicate ? "模型 A 与模型 B 必须不同" : "");
}

/** 按后端 code-point 和 Cc 字符边界校验实验名称，避免 UTF-16 maxlength 漂移。 */
function validateName(form) {
  const input = form.elements.name;
  const value = String(input?.value ?? "");
  const invalid = !value
    || value !== value.trim()
    || [...value].length > 120
    || /[\u0000-\u001f\u007f-\u009f]/u.test(value);
  input?.setCustomValidity?.(invalid ? "名称需为 1 至 120 个可见字符，且首尾不能留空" : "");
}

/** 将 Tab/Shift+Tab 限制在可见可用的 Dialog 控件内。 */
function containFocus(dialog, event) {
  const focusable = [...dialog.querySelectorAll(
    "button:not([disabled]), input:not([disabled]), select:not([disabled])",
  )].filter((element) => !element.hidden);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const activeElement = dialog.ownerDocument?.activeElement ?? globalThis.document?.activeElement;
  if (event.shiftKey && activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/** POST 未决时锁定全部控件；结束后恢复反馈开关和作用域过滤。 */
function lockForm(form, locked) {
  form.setAttribute("aria-busy", locked ? "true" : "false");
  for (const control of form.querySelectorAll("button, input, select")) {
    control.disabled = locked;
  }
  if (!locked) {
    syncFeedback(form);
    syncScopedOptions(form);
  }
}

/** 仅通过 textContent 更新 Dialog live region，错误正文不能形成 HTML。 */
function setLive(form, value) {
  const target = form.querySelector("[data-experiment-create-live]");
  if (target) target.textContent = value;
}

/** 将无符号十进制表单值转换为安全整数，拒绝小数、指数、符号和超长数字。 */
function strictInteger(value) {
  const normalized = String(value ?? "");
  if (!/^(?:0|[1-9][0-9]{0,6})$/.test(normalized)) {
    // 表单数值异常必须在网络前固定失败，不能让浏览器或后端猜测 float 语义。
    throw new TypeError("experiment.input_invalid");
  }
  return Number(normalized);
}

/** 保留 FormData 原始字符串供 exact 请求校验，禁止隐式 trim 改写用户意图。 */
function text(value) {
  return typeof value === "string" ? value : "";
}
