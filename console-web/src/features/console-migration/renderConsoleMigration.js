import { CONSOLE_MIGRATION_DOMAINS } from "../../lib/consoleMigration.js";

export function renderConsoleMigration(state = {}, permissions = new Set()) {
  const response = state.response;
  const busy = state.busy === true;
  return `<div class="console-migration-workbench" data-console-migration-root>
    <header class="platform-page-heading">
      <div><p>Platform operations</p><h1>控制台迁移治理</h1></div>
      <button class="platform-icon-button" type="button" data-console-migration-refresh
        title="刷新" aria-label="刷新" ${busy ? "disabled" : ""}>↻</button>
    </header>
    ${feedback(state)}
    ${summary(response)}
    ${filters(state, busy)}
    ${migrationTable(state, busy)}
    ${permissions.has("platform.console_migration.backfill")
      ? prepareForm(busy) : ""}
  </div>`;
}

export function bindConsoleMigrationInteractions(root, handlers = {}) {
  const workbench = root?.querySelector?.("[data-console-migration-root]");
  if (!workbench) return () => {};
  const controller = new AbortController();
  const options = { signal: controller.signal };
  workbench.querySelector("[data-console-migration-refresh]")?.addEventListener(
    "click", () => void handlers.onRefresh?.(), options,
  );
  workbench.querySelector("[data-console-migration-previous]")?.addEventListener(
    "click", () => void handlers.onPage?.("previous"), options,
  );
  workbench.querySelector("[data-console-migration-next]")?.addEventListener(
    "click", () => void handlers.onPage?.("next"), options,
  );
  workbench.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.matches("[data-console-migration-filter]")) {
      event.preventDefault();
      const data = new FormData(form);
      void handlers.onFilter?.({
        domainCode: String(data.get("domainCode") ?? ""),
        state: String(data.get("state") ?? ""),
        workspaceRef: String(data.get("workspaceRef") ?? "").trim(),
      });
      return;
    }
    if (!form.matches("[data-console-migration-command]")) return;
    event.preventDefault();
    const data = new FormData(form);
    if (data.get("confirmed") !== "yes") return;
    const sensitive = form.querySelector("[name='verificationCode']");
    const verificationCode = String(data.get("verificationCode") ?? "");
    if (sensitive) sensitive.value = "";
    const input = commandInput(data);
    if (!input) return;
    void handlers.onCommand?.(input.kind, input.command, {
      method: String(data.get("method") ?? "totp"),
      verificationCode,
    });
  }, options);
  return () => controller.abort();
}

function summary(response) {
  if (!response) return `<section class="console-migration-empty" aria-live="polite">
    <span class="platform-loading">正在读取迁移状态</span></section>`;
  const blocked = response.domains.reduce((sum, item) => sum + item.blocked_count, 0);
  const active = response.domains.reduce((sum, item) => sum + item.active_count, 0);
  const total = response.domains.reduce((sum, item) => sum + item.migration_count, 0);
  return `<section class="console-migration-summary" aria-label="迁移摘要">
    ${metric("迁移", total)}${metric("阻断", blocked)}${metric("已切换", active)}
    ${metric("合同代", response.contract.contract_generation)}
    ${metric("Registry", response.scheduler.registry_generation)}
    <span class="console-migration-health ${response.scheduler.manifest_matches ? "is-ready" : "is-blocked"}">
      ${response.scheduler.manifest_matches ? "调度合同一致" : "调度合同漂移"}
    </span>
  </section>`;
}

function metric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${Number(value)}</strong></div>`;
}

function filters(state, busy) {
  return `<form class="console-migration-filters" data-console-migration-filter>
    <label><span>业务域</span><select name="domainCode">
      <option value="">全部</option>${CONSOLE_MIGRATION_DOMAINS.map((value) => (
        `<option value="${value}" ${state.filters?.domainCode === value ? "selected" : ""}>${domainLabel(value)}</option>`
      )).join("")}</select></label>
    <label><span>状态</span><select name="state">
      <option value="">全部</option>${STATES.map((value) => (
        `<option value="${value}" ${state.filters?.state === value ? "selected" : ""}>${stateLabel(value)}</option>`
      )).join("")}</select></label>
    <label class="console-migration-owner-filter"><span>Workspace 引用</span>
      <input name="workspaceRef" maxlength="64" pattern="[0-9a-f]{64}"
        value="${escapeHtml(state.filters?.workspaceRef ?? "")}" /></label>
    <button type="submit" ${busy ? "disabled" : ""}>筛选</button>
  </form>`;
}

function migrationTable(state, busy) {
  const response = state.response;
  if (!response) return "";
  const rows = response.items.map((item) => `<tr>
    <td><strong>${domainLabel(item.domain_code)}</strong><span>${shortId(item.migration_id)}</span></td>
    <td><span class="console-migration-state state-${item.state}">${stateLabel(item.state)}</span>
      <small>${item.progress_basis_points / 100}%</small></td>
    <td><strong>G${item.operation_generation}</strong><span>T${item.target_generation}</span></td>
    <td><span>${reconciliationLabel(item.reconciliation_status)}</span>
      <small>${escapeHtml(item.reason_code)}</small></td>
    <td>${readiness(item)}</td>
    <td>${actions(item, busy)}</td>
  </tr>`).join("");
  return `<section class="console-migration-list">
    <div class="console-migration-table-wrap"><table>
      <thead><tr><th>域与迁移</th><th>状态</th><th>代次</th><th>对账</th><th>合同</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="console-migration-empty-cell">当前筛选没有迁移</td></tr>`}</tbody>
    </table></div>
    <nav class="console-migration-pagination" aria-label="迁移分页">
      <button type="button" data-console-migration-previous ${!state.canPrevious || busy ? "disabled" : ""}>上一页</button>
      <button type="button" data-console-migration-next ${!state.canNext || busy ? "disabled" : ""}>下一页</button>
    </nav>
  </section>`;
}

function readiness(item) {
  return `<span class="console-migration-check ${item.scheduler_ready ? "is-ready" : "is-blocked"}">调度</span>
    <span class="console-migration-check ${item.route_ready ? "is-ready" : "is-pending"}">路由</span>
    <span class="console-migration-check ${item.approval_ready ? "is-ready" : "is-pending"}">Owner</span>
    <span class="console-migration-check ${item.security_review_ready ? "is-ready" : "is-pending"}">安全</span>`;
}

function actions(item, busy) {
  if (!item.allowed_actions.length) return `<span class="console-migration-muted">只读</span>`;
  return `<div class="console-migration-actions">${item.allowed_actions.map(
    (action) => commandForm(item, action, busy),
  ).join("")}</div>`;
}

function commandForm(item, action, busy) {
  const [kind, verb] = action.split(".");
  const security = verb === "security_review"
    ? `<label><span>对账 ID</span><input name="reconciliationId" required pattern="drc_[0-9a-f]{32}" /></label>
       <label><span>对账 revision</span><input name="reconciliationRevision" required type="number" min="1" /></label>`
    : "";
  const rollback = kind === "rollback"
    ? `<label><span>目标代</span><input name="targetGeneration" type="number" min="1"
         max="${Math.max(1, item.target_generation - 1)}" value="${Math.max(1, item.target_generation - 1)}" required /></label>
       <label><span>原因</span><select name="reasonCode"><option value="operator_recovery">操作恢复</option>
         <option value="validation_failed">验证失败</option><option value="dependency_failure">依赖故障</option>
         <option value="release_regression">发布回归</option></select></label>`
    : "";
  return `<details><summary>${actionLabel(action)}</summary>
    <form data-console-migration-command>
      <input type="hidden" name="kind" value="${kind}" />
      <input type="hidden" name="action" value="${verb}" />
      <input type="hidden" name="migrationId" value="${item.migration_id}" />
      <input type="hidden" name="revision" value="${item.revision}" />
      <input type="hidden" name="operationGeneration" value="${item.operation_generation}" />
      <input type="hidden" name="reconciliationStatus" value="${item.reconciliation_status}" />
      ${security}${rollback}${stepUpFields()}
      <label class="console-migration-confirm"><input type="checkbox" name="confirmed" value="yes" required /><span>确认执行</span></label>
      <button type="submit" ${busy ? "disabled" : ""}>${actionLabel(action)}</button>
    </form></details>`;
}

function prepareForm(busy) {
  return `<section class="console-migration-prepare">
    <header><p>Backfill inventory</p><h2>准备回填</h2></header>
    <form data-console-migration-command>
      <input type="hidden" name="kind" value="backfill" />
      <input type="hidden" name="action" value="prepare" />
      <input type="hidden" name="revision" value="0" />
      <label><span>业务域</span><select name="domainCode">${CONSOLE_MIGRATION_DOMAINS.map(
        (value) => `<option value="${value}">${domainLabel(value)}</option>`,
      ).join("")}</select></label>
      <label><span>范围</span><select name="scope"><option value="workspace">单 Workspace</option>
        <option value="all_active">全部 active</option></select></label>
      <label><span>Workspace 引用</span><input name="workspaceRef" maxlength="64" pattern="[0-9a-f]{64}" /></label>
      ${stepUpFields()}
      <label class="console-migration-confirm"><input type="checkbox" name="confirmed" value="yes" required /><span>确认冻结 inventory</span></label>
      <button class="platform-primary" type="submit" ${busy ? "disabled" : ""}>准备</button>
    </form>
  </section>`;
}

function stepUpFields() {
  return `<label><span>验证方式</span><select name="method"><option value="totp">TOTP</option>
    <option value="webauthn">Security key</option></select></label>
    <label><span>动态验证码</span><input name="verificationCode" type="password" inputmode="numeric"
      autocomplete="one-time-code" maxlength="6" data-platform-sensitive /></label>`;
}

function commandInput(data) {
  const kind = String(data.get("kind") ?? "");
  const action = String(data.get("action") ?? "");
  const revision = Number(data.get("revision"));
  if (kind === "backfill" && action === "prepare") {
    const scope = String(data.get("scope") ?? "");
    return { kind, command: {
      action,
      domain_code: String(data.get("domainCode") ?? ""),
      scope,
      workspace_ref: scope === "workspace" ? String(data.get("workspaceRef") ?? "").trim() : null,
      expected_revision: revision,
      resource_profile: "bounded-v1",
      confirmed: true,
    } };
  }
  const migrationId = String(data.get("migrationId") ?? "");
  if (kind === "backfill") return { kind, command: {
    action,
    migration_id: migrationId,
    expected_revision: revision,
    reconciliation_generation: data.get("reconciliationStatus") === "missing"
      ? 0 : Number(data.get("operationGeneration")),
    resource_profile: "bounded-v1",
    confirmed: true,
  } };
  const command = {
    action,
    migration_id: migrationId,
    expected_revision: revision,
    confirmed: true,
  };
  if (kind === "rollback") {
    command.reason_code = String(data.get("reasonCode") ?? "operator_recovery");
    command.target_generation = Number(data.get("targetGeneration"));
  }
  if (action === "security_review") {
    command.reconciliation_id = String(data.get("reconciliationId") ?? "");
    command.reconciliation_revision = Number(data.get("reconciliationRevision"));
  }
  return new Set(["cutover", "rollback"]).has(kind) ? { kind, command } : null;
}

function feedback(state) {
  if (state.error) return `<div class="platform-feedback is-error" role="alert">
    ${escapeHtml(state.error.code)} · ${escapeHtml(state.error.requestId)}</div>`;
  if (state.feedback) return `<div class="platform-feedback" role="status">
    ${state.feedback.code === "idempotent_replay" ? "原命令已安全重放" : "命令已接受"}
    · ${escapeHtml(state.feedback.operationId)}</div>`;
  return "";
}

function actionLabel(value) {
  return ({
    "backfill.start": "开始回填", "backfill.resume": "恢复回填", "backfill.cancel": "停止领取",
    "cutover.prepare": "准备切换", "cutover.approve": "Owner 批准",
    "cutover.security_review": "安全复核", "cutover.execute": "执行切换",
    "rollback.prepare": "准备回滚", "rollback.approve": "Owner 批准回滚",
    "rollback.security_review": "安全复核回滚", "rollback.execute": "执行回滚",
    "rollback.resume": "恢复回滚",
  })[value] ?? value;
}

function domainLabel(value) {
  return ({
    channel_attribution: "渠道归因", customer_success: "客户成功", growth_analytics: "增长分析",
    brand_distribution: "品牌分发", provider_engineering: "Provider 工程",
    commercial_accounting: "商业核算", console_experience: "控制台体验",
    workspace_navigation: "Workspace 导航",
  })[value] ?? value;
}

function stateLabel(value) {
  return ({
    inventoried: "已盘点", backfill_queued: "待回填", backfilling: "回填中", shadowing: "影子验证",
    ready: "可切换", quiescing: "静默中", cutting_over: "切换中", validating: "验证中",
    active: "已激活", rollback_queued: "待回滚", rolling_back: "回滚中",
    rolled_back: "已回滚", blocked: "已阻断",
  })[value] ?? value;
}

function reconciliationLabel(value) {
  return ({
    missing: "未生成", pending: "待核对", matched: "一致", mismatched: "不一致",
    unavailable: "不可用", superseded: "已替代",
  })[value] ?? value;
}

function shortId(value) { return `${String(value).slice(0, 8)}…${String(value).slice(-6)}`; }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[character]);
}

const STATES = [
  "inventoried", "backfill_queued", "backfilling", "shadowing", "ready", "quiescing",
  "cutting_over", "validating", "active", "rollback_queued", "rolling_back",
  "rolled_back", "blocked",
];
