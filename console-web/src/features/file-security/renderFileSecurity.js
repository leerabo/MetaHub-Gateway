/**
 * 模块职责：渲染 PAGE-103 ready 文件选择、安全报告、findings 与扫描/CSV 命令。
 * 安全边界：只消费 strict FileInfo 和 FileSecurityReport；正文、OCR 文本、证据 HMAC、报告 hash、
 * 对象键、规则表达式和下载 Blob 均不属于渲染输入，文件名只在转义后的文本节点中展示。
 */

import { fileSecurityPageOwner } from "./fileSecurityPageState.js";

// 报告终态同时使用文字、符号和 tone，不能只靠颜色传达模型准入结论。
const STATUS_LABELS = Object.freeze({
  passed: ["扫描通过", "✓", "passed"],
  blocked: ["已阻断", "!", "blocked"],
  failed: ["扫描失败", "×", "failed"],
});
const RISK_LABELS = Object.freeze({
  none: "无已知风险",
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  critical: "严重风险",
  unknown: "风险未知",
});
// 覆盖状态必须明确区分不完整和不可用，不能显示成绿色布尔结果。
const COVERAGE_LABELS = Object.freeze({
  complete: "覆盖完整",
  partial: "覆盖不完整",
  unavailable: "覆盖不可用",
});
// OCR 状态与覆盖独立，未要求 OCR 不能被误写成已完成。
const OCR_LABELS = Object.freeze({
  not_required: "无需 OCR",
  completed: "OCR 已完成",
  partial: "OCR 覆盖不完整",
  unavailable: "OCR 不可用",
  failed: "OCR 失败",
});
// Finding 严重度使用固定中文短标签，适配窄屏表格轨道。
const SEVERITY_LABELS = Object.freeze({
  info: "提示",
  low: "低",
  medium: "中",
  high: "高",
  critical: "严重",
});
// category 只映射后端固定枚举，未知类别在 DTO 层已经拒绝。
const CATEGORY_LABELS = Object.freeze({
  prompt_injection: "指令注入",
  visual_hiding: "视觉隐藏",
  hidden_layer: "隐藏层",
  ocr: "图像文字",
  container: "嵌套容器",
  coverage: "检测覆盖",
});
// source 文案不包含文档 part 名、PDF 对象号或任何宿主路径。
const SOURCE_LABELS = Object.freeze({
  visible_text: "可见文本",
  docx_xml: "DOCX 结构",
  docx_image: "DOCX 图片",
  pdf_content: "PDF 内容流",
  pdf_image: "PDF 图片页",
  document_container: "文档容器",
  scan_coverage: "扫描覆盖",
});

/**
 * 渲染当前 tenant 的文件安全工作区。
 *
 * @param {object|null} state PAGE-103 安全快照。
 * @param {object|null} authContext 当前 /auth/me 权限快照。
 * @param {object|null} consoleShellState 后端导航快照。
 * @param {object|null} i18n 当前语言上下文。
 * @returns {string} 安全 HTML；read owner 不成立时返回空字符串。
 */
export function renderFileSecurity(state, authContext, consoleShellState, i18n = null) {
  const owner = fileSecurityPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const viewState = state ?? {
    state: "loading",
    files: [],
    selectedFileId: "",
    reportState: "idle",
    report: null,
    canScan: owner.canScan,
    scanBusy: false,
    exportBusy: false,
  };
  const tenantLabel = safeTenantLabel(authContext, owner.tenantId);
  return `
    <div class="file-security-workspace" data-file-security-root>
      <header class="file-security-heading">
        <div>
          <h2 id="file-security-title">${escapeHtml(t(i18n, "file_security.title", "文件安全报告"))}</h2>
          <p>${escapeHtml(t(i18n, "file_security.description", "核查隐藏指令风险与模型上下文准入状态。"))}</p>
        </div>
        <span>${escapeHtml(t(i18n, "file_security.tenant_scope", "当前租户"))}：${escapeHtml(tenantLabel)}</span>
      </header>
      ${renderFeedback(viewState.feedback)}
      ${viewState.state === "error"
        ? renderLoadError(viewState.error)
        : renderWorkspace(viewState, owner, i18n)}
    </div>
  `;
}

/** 渲染文件记录与报告双栏；窄屏由 CSS 转为单列且不创建嵌套卡片。 */
function renderWorkspace(state, owner, i18n) {
  return `
    <div class="file-security-layout">
      ${renderFileList(state, i18n)}
      ${renderReportRegion(state, owner, i18n)}
    </div>
  `;
}

/** 只渲染 strict client 已确认 ready 的文件，名称不进入 dataset、URL 或下载名。 */
function renderFileList(state, i18n) {
  const files = Array.isArray(state.files) ? state.files : [];
  return `
    <section class="file-security-files" aria-labelledby="file-security-files-title">
      <div class="file-security-section-heading">
        <div>
          <h3 id="file-security-files-title">${escapeHtml(t(i18n, "file_security.files.title", "可扫描文件"))}</h3>
          <span>${escapeHtml(t(i18n, "file_security.files.description", "仅显示已完成解析和 DLP 预扫描的文件"))}</span>
        </div>
        <strong>${escapeHtml(String(files.length))}</strong>
      </div>
      <div class="file-security-file-list" role="listbox" aria-label="${escapeAttribute(t(i18n, "file_security.files.title", "可扫描文件"))}">
        ${state.state === "loading"
          ? `<div class="file-security-empty">${escapeHtml(t(i18n, "file_security.loading", "正在读取可扫描文件"))}</div>`
          : files.length
            ? files.map((file) => renderFileChoice(file, state.selectedFileId)).join("")
            : `<div class="file-security-empty">${escapeHtml(t(i18n, "file_security.files.empty", "当前租户没有可扫描文件"))}</div>`}
      </div>
      ${renderFilePager(state, i18n)}
    </section>
  `;
}

/** cursor 只通过布尔投影控制前后按钮，签名值永不进入页面 HTML。 */
function renderFilePager(state, i18n) {
  return `
    <div class="file-security-pager">
      <button type="button" data-file-security-previous${state.canPrevious ? "" : " disabled"}>${escapeHtml(t(i18n, "file_security.previous", "上一页"))}</button>
      <button type="button" data-file-security-next${state.canNext ? "" : " disabled"}>${escapeHtml(t(i18n, "file_security.next", "下一页"))}</button>
    </div>
  `;
}

/** 一条文件选择仅把规范 file ID 放入命令属性，文件名始终位于安全文本节点。 */
function renderFileChoice(file, selectedFileId) {
  const selected = file.file_id === selectedFileId;
  return `
    <button
      class="file-security-file${selected ? " is-selected" : ""}"
      type="button"
      role="option"
      aria-selected="${selected ? "true" : "false"}"
      data-file-security-select="${escapeAttribute(file.file_id)}"
    >
      <strong>${escapeHtml(file.original_name)}</strong>
      <span><b>${escapeHtml(String(file.file_extension).toUpperCase())}</b>${escapeHtml(formatBytes(file.size_bytes))}</span>
      <small>${escapeHtml(formatDateTime(file.completed_at))} · DLP ${escapeHtml(file.dlp_scan_status)}</small>
    </button>
  `;
}

/** 报告区根据未选择、加载、未扫描、异常和可信终态分别渲染，旧报告不会残留在错误态。 */
function renderReportRegion(state, owner, i18n) {
  const selected = Array.isArray(state.files)
    ? state.files.find((file) => file.file_id === state.selectedFileId)
    : null;
  return `
    <section class="file-security-report" aria-labelledby="file-security-report-title">
      <div class="file-security-report-heading">
        <div>
          <h3 id="file-security-report-title">${escapeHtml(t(i18n, "file_security.report.title", "检测报告"))}</h3>
          <span>${selected ? escapeHtml(selected.original_name) : "-"}</span>
        </div>
        ${renderCommands(state, owner, Boolean(selected), i18n)}
      </div>
      ${renderReportBody(state, Boolean(selected), i18n)}
    </section>
  `;
}

/** 扫描权限缺失时不创建扫描按钮；CSV 仅在可信报告存在时创建。 */
function renderCommands(state, owner, hasSelection, i18n) {
  const scanBusy = state.scanBusy === true;
  const exportBusy = state.exportBusy === true;
  const canScan = owner.canScan && state.canScan !== false;
  const hasReport = state.reportState === "ready" && state.report?.file_id === state.selectedFileId;
  return `
    <div class="file-security-commands">
      ${canScan
        ? `
          <button
            type="button"
            data-file-security-scan
            title="${escapeAttribute(t(i18n, "file_security.scan", "执行安全扫描"))}"
            ${!hasSelection || scanBusy || exportBusy ? "disabled" : ""}
          ><span aria-hidden="true">⌕</span><span>${scanBusy ? "扫描中" : "扫描"}</span></button>
        `
        : `<span class="file-security-read-only">${escapeHtml(t(i18n, "file_security.read_only", "只读报告视图"))}</span>`}
      ${hasReport
        ? `
          <button
            type="button"
            data-file-security-export
            title="${escapeAttribute(t(i18n, "file_security.export", "下载 CSV 报告"))}"
            ${scanBusy || exportBusy ? "disabled" : ""}
          ><span aria-hidden="true">↓</span><span>${exportBusy ? "下载中" : "CSV"}</span></button>
        `
        : ""}
    </div>
  `;
}

/** 选择和报告状态的互斥渲染入口。 */
function renderReportBody(state, hasSelection, i18n) {
  if (!hasSelection) {
    return `<div class="file-security-empty">${escapeHtml(t(i18n, "file_security.report.no_file", "请选择一个可扫描文件"))}</div>`;
  }
  if (state.reportState === "loading") {
    return `<div class="file-security-empty">${escapeHtml(t(i18n, "file_security.report.loading", "正在读取当前安全报告"))}</div>`;
  }
  if (state.reportState === "empty") {
    return `
      <div class="file-security-empty">
        <strong>${escapeHtml(t(i18n, "file_security.report.empty", "尚未生成当前检测器版本的报告"))}</strong>
        <span>${state.canScan ? "可执行扫描生成报告" : "当前权限只能读取已有报告"}</span>
      </div>
    `;
  }
  // 报告依赖或完整性错误使用独立警示态，禁止回退空报告文案。
  if (state.reportState === "error") {
    // 报告错误必须与“尚未扫描”分开，避免依赖故障被用户误解为无风险。
    return renderReportError(state.reportError);
  }
  if (state.reportState !== "ready" || !state.report) {
    return `<div class="file-security-empty">${escapeHtml(t(i18n, "file_security.report.pending", "等待安全报告"))}</div>`;
  }
  return renderReport(state.report, i18n);
}

/** 渲染可信报告摘要与 findings，不输出原始命中证据。 */
function renderReport(report, i18n) {
  const status = STATUS_LABELS[report.status] ?? ["未知", "?", "failed"];
  const riskScore = Number.isSafeInteger(report.risk_score) ? `${report.risk_score} / 100` : "不可用";
  return `
    <div class="file-security-report-body">
      <div class="file-security-verdict tone-${escapeAttribute(status[2])}">
        <span aria-hidden="true">${escapeHtml(status[1])}</span>
        <div>
          <strong>${escapeHtml(status[0])}</strong>
          <small>${report.model_access_status === "allowed" ? "允许进入模型上下文" : "禁止进入模型上下文"}</small>
        </div>
        <code>${escapeHtml(report.scan_id)}</code>
      </div>
      <div class="file-security-metrics" aria-label="${escapeAttribute(t(i18n, "file_security.metrics", "报告指标"))}">
        ${renderMetric("风险等级", RISK_LABELS[report.risk_level] ?? "未知", riskScore)}
        ${renderMetric("检测覆盖", COVERAGE_LABELS[report.coverage_status] ?? "未知", `${report.scanned_page_count} 页 · ${report.scanned_image_count} 图`)}
        ${renderMetric("离线 OCR", OCR_LABELS[report.ocr_status] ?? "未知", report.ocr_engine_version ?? "未加载")}
        ${renderMetric("安全发现", `${report.finding_count} 条`, `${report.high_finding_count} 条高风险`)}
      </div>
      <dl class="file-security-contract">
        <div><dt>检测器</dt><dd><code>${escapeHtml(report.detector_version)}</code></dd></div>
        <div><dt>上下文边界</dt><dd><code>${escapeHtml(report.boundary_version)}</code></dd></div>
        <div><dt>文件 ID</dt><dd><code>${escapeHtml(report.file_id)}</code></dd></div>
        <div><dt>完成时间</dt><dd><time datetime="${escapeAttribute(report.completed_at)}">${escapeHtml(formatDateTime(report.completed_at))}</time></dd></div>
      </dl>
      ${renderFindings(report.findings, i18n)}
    </div>
  `;
}

/** 指标块使用固定紧凑尺寸，长版本和状态文本不会推动按钮或页面宽度。 */
function renderMetric(label, value, detail) {
  return `
    <div>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>
  `;
}

/** Finding 表仅展示固定枚举、置信度和有界位置，不展示任何 evidence 文本。 */
function renderFindings(findings, i18n) {
  if (!Array.isArray(findings) || findings.length === 0) {
    return `<div class="file-security-findings-empty">${escapeHtml(t(i18n, "file_security.findings.empty", "未发现已知隐藏指令风险"))}</div>`;
  }
  return `
    <section class="file-security-findings" aria-labelledby="file-security-findings-title">
      <div class="file-security-section-heading">
        <div>
          <h4 id="file-security-findings-title">${escapeHtml(t(i18n, "file_security.findings.title", "安全发现"))}</h4>
          <span>${escapeHtml(t(i18n, "file_security.findings.description", "报告不包含命中文本或 OCR 正文"))}</span>
        </div>
      </div>
      <div class="file-security-finding-table" role="table" aria-label="安全发现">
        <div class="file-security-finding-row is-head" role="row">
          <span>级别</span><span>类型 / 检测器</span><span>来源 / 位置</span><span>置信度 / 时间</span>
        </div>
        ${findings.map(renderFinding).join("")}
      </div>
    </section>
  `;
}

/** 渲染单条 fixed-enum finding。 */
function renderFinding(finding) {
  const location = finding.location_index === null
    ? "文档级"
    : `${locationLabel(finding.location_kind)} ${finding.location_index}`;
  return `
    <div class="file-security-finding-row" role="row">
      <span data-label="级别"><b class="finding-severity tone-${escapeAttribute(finding.severity)}">${escapeHtml(SEVERITY_LABELS[finding.severity] ?? "未知")}</b></span>
      <span data-label="类型 / 检测器"><strong>${escapeHtml(CATEGORY_LABELS[finding.category] ?? "未知")}</strong><code>${escapeHtml(finding.detector_code)}</code></span>
      <span data-label="来源 / 位置"><strong>${escapeHtml(SOURCE_LABELS[finding.source_kind] ?? "未知")}</strong><small>${escapeHtml(location)}</small></span>
      <span data-label="置信度 / 时间"><strong>${escapeHtml(`${(finding.confidence_milli / 10).toFixed(1)}%`)}</strong><time datetime="${escapeAttribute(finding.created_at)}">${escapeHtml(formatDateTime(finding.created_at))}</time></span>
    </div>
  `;
}

/** 把位置枚举转换为不含底层对象信息的展示文本。 */
function locationLabel(kind) {
  return { page: "页", part: "部件", image: "图片" }[kind] ?? "位置";
}

/** 顶部反馈只显示固定 code/message/request_id。 */
function renderFeedback(feedback) {
  if (!feedback) return "";
  return `
    <div class="file-security-feedback ${feedback.state === "success" ? "is-success" : "is-error"}" role="status">
      <span>${escapeHtml(feedback.message ?? "文件安全操作已完成")}</span>
      <code>${escapeHtml(feedback.code ?? "-")}</code>
      <code>request_id: ${escapeHtml(feedback.requestId ?? "-")}</code>
    </div>
  `;
}

/** ready 文件列表失败的独立页面错误。 */
function renderLoadError(error) {
  return `
    <div class="file-security-error" role="alert">
      <strong>${escapeHtml(error?.message ?? "文件安全记录读取失败")}</strong>
      <code>${escapeHtml(error?.code ?? "file.security_scan_unavailable")}</code>
      <code>request_id: ${escapeHtml(error?.requestId ?? "-")}</code>
    </div>
  `;
}

/** 报告读取失败不会伪装成尚未扫描，并保留已验证的 ready 文件选择区。 */
function renderReportError(error) {
  return `
    <div class="file-security-error" role="alert">
      <strong>${escapeHtml(error?.message ?? "文件安全报告读取失败")}</strong>
      <code>${escapeHtml(error?.code ?? "file.security_scan_unavailable")}</code>
      <code>request_id: ${escapeHtml(error?.requestId ?? "-")}</code>
    </div>
  `;
}

/** 当前租户标识优先使用有界展示名，缺失时回退规范 tenant ID。 */
function safeTenantLabel(authContext, tenantId) {
  const displayName = String(authContext?.tenant?.display_name ?? authContext?.tenant?.name ?? "").trim();
  if (displayName && displayName.length <= 128 && !/[\p{Cc}\p{Cf}]/u.test(displayName)) return displayName;
  // 展示名缺失或含控制字符时回退已由 owner 校验的规范 tenant ID。
  return tenantId;
}

/** 使用二进制单位展示文件大小。 */
function formatBytes(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

/** 时间只做展示；解析失败固定为“-”，不输出浏览器异常。 */
function formatDateTime(value) {
  const date = new Date(value ?? "");
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

/** 读取 i18n 文案，缺失时使用页面冻结中文。 */
function t(i18n, key, fallback) {
  return typeof i18n?.t === "function" ? i18n.t(key, {}, fallback) : fallback;
}

/** 统一转义文本节点，防止文件名、ID 或固定枚举形成 HTML。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值额外处理反引号，禁止退出 data、title 或 datetime 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
