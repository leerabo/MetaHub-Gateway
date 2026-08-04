/**
 * 模块职责：渲染 PAGE-101/PAGE-102 文件上传和处理记录工作台。
 * 安全边界：只消费 strict client 的 FileInfo；抽取正文、DLP 规则、fingerprint、对象键和宿主路径
 * 不属于渲染输入。文件名虽为必要字段，仍必须逐处 HTML/属性转义并在 owner 失效时清空。
 */

import { filePageOwner } from "./filePageState.js";
import { FILE_UPLOAD_MAX_BYTES } from "../../lib/files.js";

// 文件状态同时冻结中文、图标和色调，避免只靠颜色表达终态。
const STATUS_LABELS = Object.freeze({
  processing: ["处理中", "…", "processing"],
  ready: ["可用", "✓", "ready"],
  blocked: ["已阻断", "!", "blocked"],
  failed: ["失败", "×", "failed"],
});
// DLP 状态独立于病毒占位；unavailable 与 blocked 都不得降级成绿色 clean。
const DLP_LABELS = Object.freeze({
  pending: ["等待预扫描", "…", "pending"],
  clean: ["未命中", "✓", "clean"],
  matched: ["已记录命中", "!", "matched"],
  blocked: ["已阻断", "!", "blocked"],
  unavailable: ["扫描不可用", "×", "unavailable"],
});

/**
 * 渲染当前 tenant 的文件工作台。
 *
 * @param {object|null} state 文件页面公开快照。
 * @param {object|null} authContext 当前 /auth/me 授权快照。
 * @param {object|null} consoleShellState 后端导航授权快照。
 * @param {object|null} i18n 当前展示语言上下文。
 * @returns {string} 安全 HTML；read owner 不成立时返回空字符串。
 */
export function renderFiles(state, authContext, consoleShellState, i18n = null) {
  const owner = filePageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const viewState = state ?? {
    state: "loading",
    files: [],
    status: "",
    canPrevious: false,
    canNext: false,
    uploadBusy: false,
    canWrite: owner.canWrite,
  };
  return `
    <div class="file-workspace" data-file-root>
      <header class="file-heading">
        <div>
          <h2 id="files-title">${escapeHtml(t(i18n, "files.title", "文件处理"))}</h2>
          <p>${escapeHtml(t(i18n, "files.description", "上传并查看当前租户的文本解析与安全预扫描结果。"))}</p>
        </div>
        <span>${escapeHtml(t(i18n, "files.tenant_scope", "当前租户"))}</span>
      </header>
      ${renderUpload(viewState, owner, i18n)}
      ${renderRecords(viewState, i18n)}
    </div>
  `;
}

/** 渲染顶部紧凑上传栏；file.write 缺失时只显示明确只读状态，不创建隐藏 input。 */
function renderUpload(state, owner, i18n) {
  if (!owner.canWrite || state.canWrite === false) {
    return `
      <section class="file-upload-strip is-read-only" aria-label="${escapeAttribute(t(i18n, "files.upload.title", "上传文件"))}">
        <div>
          <strong>${escapeHtml(t(i18n, "files.read_only", "只读文件视图"))}</strong>
          <span>${escapeHtml(t(i18n, "files.read_only.description", "当前权限可查看处理记录，但不能上传文件。"))}</span>
        </div>
      </section>
    `;
  }
  const busy = state.uploadBusy === true;
  return `
    <section class="file-upload-strip" aria-labelledby="file-upload-title">
      <div class="file-upload-copy">
        <strong id="file-upload-title">${escapeHtml(t(i18n, "files.upload.title", "上传文件"))}</strong>
        <span>PDF / DOCX / UTF-8 TXT · ${escapeHtml(formatBytes(FILE_UPLOAD_MAX_BYTES))}</span>
      </div>
      <form id="file-upload-form" class="file-upload-form"${busy ? ' aria-busy="true"' : ""}>
        <label class="file-picker">
          <input
            id="file-upload-input"
            name="file"
            type="file"
            accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
            ${busy ? "disabled" : ""}
          />
          <span>${escapeHtml(t(i18n, "files.upload.choose", "选择文件"))}</span>
        </label>
        <span class="file-selection" data-file-selection>${escapeHtml(t(i18n, "files.upload.none", "未选择文件"))}</span>
        <button
          class="file-upload-button"
          type="submit"
          data-file-upload-submit
          title="${escapeAttribute(t(i18n, "files.upload.submit", "上传并解析"))}"
          aria-label="${escapeAttribute(t(i18n, "files.upload.submit", "上传并解析"))}"
          disabled
        >
          <span aria-hidden="true">↑</span><span>${escapeHtml(t(i18n, "files.upload.submit", "上传并解析"))}</span>
        </button>
      </form>
      <p class="file-upload-live" data-file-upload-live aria-live="polite">${busy
        ? escapeHtml(t(i18n, "files.upload.busy", "正在上传、解析并执行安全预扫描"))
        : ""}</p>
      ${renderFeedback(state.feedback)}
    </section>
  `;
}

/** 上传反馈只显示固定 code/message/request_id；不回显文件名或底层异常。 */
function renderFeedback(feedback) {
  if (!feedback) return "";
  const tone = feedback.state === "success" ? "is-success" : "is-error";
  return `
    <div class="file-feedback ${tone}" role="status">
      <span>${escapeHtml(feedback.message ?? "文件操作已完成")}</span>
      <code>${escapeHtml(feedback.code ?? "-")}</code>
      <code>request_id: ${escapeHtml(feedback.requestId ?? "-")}</code>
    </div>
  `;
}

/** 渲染筛选、加载/错误态与紧凑记录表。 */
function renderRecords(state, i18n) {
  const files = Array.isArray(state.files) ? state.files : [];
  return `
    <section class="file-records" aria-labelledby="file-records-title">
      <div class="file-records-heading">
        <div>
          <h3 id="file-records-title">${escapeHtml(t(i18n, "files.records.title", "处理记录"))}</h3>
          <span>${escapeHtml(t(i18n, "files.records.description", "文件名与处理摘要仅对当前授权 tenant 展示。"))}</span>
        </div>
        <form id="file-status-filter-form" class="file-status-filter">
          <label>
            <span>${escapeHtml(t(i18n, "files.status.filter", "状态"))}</span>
            <select name="status">${renderStatusOptions(state.status, i18n)}</select>
          </label>
          <button type="submit">${escapeHtml(t(i18n, "files.filter.apply", "筛选"))}</button>
          <button id="file-status-filter-reset" type="button">${escapeHtml(t(i18n, "files.filter.reset", "重置"))}</button>
        </form>
      </div>
      ${state.state === "error" ? renderLoadError(state.error) : renderTable(files, state.state, i18n)}
      ${renderPager(state, i18n)}
    </section>
  `;
}

/** 固定状态枚举生成 select；未知值不会成为可提交选项。 */
function renderStatusOptions(selected, i18n) {
  const options = [
    ["", t(i18n, "files.status.all", "全部")],
    ["processing", t(i18n, "files.status.processing", "处理中")],
    ["ready", t(i18n, "files.status.ready", "可用")],
    ["blocked", t(i18n, "files.status.blocked", "已阻断")],
    ["failed", t(i18n, "files.status.failed", "失败")],
  ];
  return options.map(([value, label]) => (
    `<option value="${escapeAttribute(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`
  )).join("");
}

/** 渲染响应合同错误；旧列表不会与错误态混合展示。 */
function renderLoadError(error) {
  return `
    <div class="file-empty is-error" role="alert">
      <strong>${escapeHtml(error?.message ?? "文件处理记录读取失败")}</strong>
      <code>${escapeHtml(error?.code ?? "file.storage_unavailable")}</code>
      <code>request_id: ${escapeHtml(error?.requestId ?? "-")}</code>
    </div>
  `;
}

/** 渲染严格 FileInfo 列表；正文和对象键不存在于 item，未知字段也不会被展开。 */
function renderTable(files, state, i18n) {
  if (state === "loading") {
    return `<div class="file-empty">${escapeHtml(t(i18n, "files.loading", "正在读取文件处理记录"))}</div>`;
  }
  return `
    <div class="file-table" role="table" aria-label="${escapeAttribute(t(i18n, "files.records.title", "处理记录"))}">
      <div class="file-row file-row-head" role="row">
        <span>文件</span><span>格式 / 大小</span><span>处理状态</span><span>DLP</span><span>病毒扫描</span><span>Token 估算</span><span>完成时间 / request_id</span>
      </div>
      ${files.length
        ? files.map(renderFileRow).join("")
        : `<div class="file-empty">${escapeHtml(t(i18n, "files.empty", "当前条件下没有文件处理记录"))}</div>`}
    </div>
  `;
}

/** 渲染一条 FileInfo；文件名只经过转义写入文本位置，不进入 class、URL 或 data 属性。 */
function renderFileRow(file) {
  const status = STATUS_LABELS[file.status] ?? ["未知", "?", "unknown"];
  const dlp = DLP_LABELS[file.dlp_scan_status] ?? ["证据不可用", "?", "unknown"];
  const tokens = Number.isSafeInteger(file.extract?.estimated_tokens)
    ? file.extract.estimated_tokens.toLocaleString("zh-CN")
    : "-";
  const failure = file.failure_error_code
    ? `<code class="file-failure-code">${escapeHtml(file.failure_error_code)}</code>`
    : "";
  return `
    <div class="file-row" role="row">
      <strong class="file-name" data-label="文件">${escapeHtml(file.original_name)}</strong>
      <span data-label="格式 / 大小"><b>${escapeHtml(String(file.file_extension).toUpperCase())}</b><small>${escapeHtml(formatBytes(file.size_bytes))}</small></span>
      <span data-label="处理状态"><span class="file-state tone-${escapeAttribute(status[2])}"><i aria-hidden="true">${escapeHtml(status[1])}</i>${escapeHtml(status[0])}</span>${failure}</span>
      <span data-label="DLP"><span class="file-state tone-${escapeAttribute(dlp[2])}"><i aria-hidden="true">${escapeHtml(dlp[1])}</i>${escapeHtml(dlp[0])}</span><small>${escapeHtml(file.match_count ?? 0)} 条规则</small></span>
      <span data-label="病毒扫描"><span class="file-state tone-not-configured"><i aria-hidden="true">−</i>未配置病毒扫描</span></span>
      <span data-label="Token 估算"><strong>${escapeHtml(tokens)}</strong><small>${file.extract?.estimator_version ? "确定性估算" : "等待抽取"}</small></span>
      <span data-label="完成时间 / request_id"><time datetime="${escapeAttribute(file.completed_at ?? "")}">${escapeHtml(formatDateTime(file.completed_at))}</time><code>${escapeHtml(file.upload_request_id ?? "-")}</code></span>
    </div>
  `;
}

/** cursor 只通过状态机布尔投影控制按钮，完整签名永不进入 DOM。 */
function renderPager(state, i18n) {
  return `
    <div class="file-pager">
      <button id="file-page-prev" type="button"${state.canPrevious ? "" : " disabled"}>${escapeHtml(t(i18n, "files.page.previous", "上一页"))}</button>
      <code>request_id: ${escapeHtml(state.requestId ?? "-")}</code>
      <button id="file-page-next" type="button"${state.canNext ? "" : " disabled"}>${escapeHtml(t(i18n, "files.page.next", "下一页"))}</button>
    </div>
  `;
}

/** 使用二进制单位展示有界字节数；非法值固定为“-”。 */
function formatBytes(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

/** 时间只做展示，解析失败固定为“-”且不输出浏览器异常。 */
function formatDateTime(value) {
  const date = new Date(value ?? "");
  return Number.isNaN(date.getTime())
    ? "-"
    : date.toLocaleString("zh-CN", { hour12: false });
}

/** 读取 i18n 文案；当前语言缺失时由上下文回退中文，测试无上下文时使用固定 fallback。 */
function t(i18n, key, fallback) {
  return typeof i18n?.t === "function" ? i18n.t(key, {}, fallback) : fallback;
}

/** 统一转义文本节点，防止文件名和链路 ID 形成可执行 HTML。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值在文本转义基础上处理反引号，禁止退出 accept、title 或 datetime 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
