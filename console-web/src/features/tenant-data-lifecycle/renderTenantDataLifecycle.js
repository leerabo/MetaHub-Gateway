import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { tenantDataLifecycleOwner } from "./tenantDataLifecyclePageState.js";

const LABELS = Object.freeze({ active: "运行中", deletion_requested: "已申请注销", deleted: "已注销", pending: "等待生成", processing: "生成中", completed: "已完成", failed: "失败", expired: "已过期", requested: "已提交", quiescing: "收敛中", deleting: "删除中" });

export function renderTenantDataLifecycle(state, authContext, consoleShellState, i18n) {
  const owner = tenantDataLifecycleOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? { state: { lifecycle_status: "active", revision: 1, cache_generation: 0 }, exports: [], deletion_request: null };
  const canExport = hasPermission(authContext, PERMISSIONS.TENANT_DATA_EXPORT);
  const canDownload = hasPermission(authContext, PERMISSIONS.TENANT_DATA_DOWNLOAD);
  const canDelete = hasPermission(authContext, PERMISSIONS.TENANT_DATA_DELETE);
  const deletion = view.deletion_request;
  const label = (key, fallback) => i18n?.t?.(key, {}, fallback) ?? fallback;
  const statusLabel = (status) => label(`tenant_data.status.${status}`, LABELS[status] ?? status);
  return `<div class="tenant-data-lifecycle" data-tenant-data-root>
    <header class="tenant-data-toolbar"><div><h2 id="tenant-data-title">${escapeHtml(label("tenant_data.title", "数据管理"))}</h2><p>${escapeHtml(label("tenant_data.subtitle", "当前租户 · 生命周期状态与受保护导出"))}</p></div><button type="button" class="icon-command" data-tenant-data-refresh title="${escapeAttribute(label("tenant_data.refresh", "刷新"))}" aria-label="${escapeAttribute(label("tenant_data.refresh", "刷新"))}">↻</button></header>
    <div class="tenant-data-feedback" data-tenant-data-feedback role="status" aria-live="polite">${view.error ? `读取失败 · code: ${escapeHtml(view.error.code)} · request_id: ${escapeHtml(view.error.requestId)}` : ""}</div>
    <section class="tenant-data-state" aria-labelledby="tenant-data-state-title"><h3 id="tenant-data-state-title">${escapeHtml(label("tenant_data.state_title", "租户状态"))}</h3><dl><div><dt>${escapeHtml(label("tenant_data.lifecycle", "生命周期"))}</dt><dd>${escapeHtml(statusLabel(view.state?.lifecycle_status))}</dd></div><div><dt>${escapeHtml(label("tenant_data.revision", "Revision"))}</dt><dd>${escapeHtml(view.state?.revision ?? "-")}</dd></div><div><dt>${escapeHtml(label("tenant_data.cache_generation", "缓存代次"))}</dt><dd>${escapeHtml(view.state?.cache_generation ?? "-")}</dd></div></dl></section>
    <section class="tenant-data-export" aria-labelledby="tenant-data-export-title"><div class="tenant-data-section-heading"><h3 id="tenant-data-export-title">${escapeHtml(label("tenant_data.export_title", "导出任务"))}</h3><button type="button" data-tenant-data-export${canExport ? "" : " disabled"}>${escapeHtml(label("tenant_data.create_export", "创建安全导出"))}</button></div>${renderExports(view.exports, canDownload, label)}</section>
    <section class="tenant-data-deletion" aria-labelledby="tenant-data-deletion-title"><div class="tenant-data-section-heading"><div><h3 id="tenant-data-deletion-title">${escapeHtml(label("tenant_data.deletion_title", "注销租户"))}</h3><p>${escapeHtml(label("tenant_data.deletion_description", "注销会冻结租户并异步清理业务数据，保留必要的审计与留存证明。"))}</p></div><button type="button" class="danger" data-tenant-data-delete${canDelete && !deletion ? "" : " disabled"}>${escapeHtml(label("tenant_data.request_deletion", "申请注销"))}</button></div>${deletion ? `<div class="tenant-data-deletion-status"><strong>${escapeHtml(statusLabel(deletion.status))}</strong><span>${escapeHtml(label("tenant_data.request_id", "请求"))} ${escapeHtml(deletion.deletion_request_id)}</span><span>${escapeHtml(label("tenant_data.rows", "已归档 {archived} 行 · 已删除 {deleted} 行").replace("{archived}", deletion.archived_rows ?? 0).replace("{deleted}", deletion.deleted_rows ?? 0))}</span></div>` : ""}</section>
    <dialog class="tenant-data-dialog" data-tenant-data-dialog aria-busy="false" aria-labelledby="tenant-data-dialog-title"><form method="dialog" data-tenant-data-deletion-form><h3 id="tenant-data-dialog-title">${escapeHtml(label("tenant_data.dialog_title", "确认注销当前租户"))}</h3><p>${escapeHtml(label("tenant_data.dialog_description", "请输入当前租户 slug 以确认。此操作不可撤销。"))}</p><label>${escapeHtml(label("tenant_data.confirmation", "确认短语"))}<input name="confirmation" autocomplete="off" required maxlength="200" /></label><div class="tenant-data-dialog-feedback" data-tenant-data-dialog-feedback role="status" aria-live="polite"></div><div class="tenant-data-dialog-actions"><button type="button" data-tenant-data-cancel>${escapeHtml(label("common.cancel", "取消"))}</button><button type="submit" class="danger" data-tenant-data-dialog-submit>${escapeHtml(label("tenant_data.submit_deletion", "提交注销申请"))}</button></div></form></dialog>
  </div>`;
}

function renderExports(exports, canDownload, label) {
  if (!exports?.length) return `<p class="tenant-data-empty">${escapeHtml(label("tenant_data.no_exports", "尚无导出任务。"))}</p>`;
  return `<div class="tenant-data-export-list">${exports.map((item) => `<article class="tenant-data-export-row"><div><strong>${escapeHtml(label(`tenant_data.status.${item.status}`, LABELS[item.status] ?? item.status))}</strong><code>${escapeHtml(item.tenant_export_job_id)}</code></div><span>${escapeHtml(item.content_size ?? "-")} bytes</span>${item.content_sha256 ? `<code>${escapeHtml(item.content_sha256)}</code>` : ""}${item.status === "completed" && canDownload ? `<button type="button" data-tenant-data-download="${escapeAttribute(item.tenant_export_job_id)}">${escapeHtml(label("tenant_data.download", "下载"))}</button>` : ""}</article>`).join("")}</div>`;
}
function escapeHtml(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#096;"); }
