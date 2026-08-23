// 平台页面状态机负责取消过期请求、权限裁剪和幂等意图；敏感认证值只由 HttpOnly Cookie 承载。
import { PlatformApiError } from "../../lib/platform.js";
import { renderPlatformGate, renderPlatformWorkspace } from "./renderPlatformPage.js";

const PLATFORM_NAVIGATION_IDS = new Set([
  "infrastructure",
  "gateway",
  "operators",
  "workspaces",
  "support",
  "audit",
  "billing",
  "verification",
  "compliance",
  "privacy",
  "releases",
  "providers",
  "channels",
  "customer-success",
  "growth",
]);
const WORKSPACE_READ_PERMISSIONS = new Set([
  "platform.workspace.directory.read",
  "platform.billing.read",
  "platform.customer_success.read",
  "platform.workspace.risk.manage",
]);
const WORKSPACE_FILTER_KEYS = new Set([
  "search",
  "kind",
  "operational_status",
  "region",
  "risk_tag",
  "health_status",
  "health_risk",
]);

export function createPlatformPageController({
  root,
  api,
  confirmAction = defaultConfirm,
  newIdempotencyKey = defaultIdempotencyKey,
} = {}) {
  if (!root || !api) throw new TypeError("platform page requires root and api");
  let generation = 0;
  let request = null;
  const state = {
    loading: true,
    session: null,
    navigation: [],
    operators: null,
    plans: null,
    billing: null,
    billingStatus: "",
    billingCursor: null,
    billingCursorHistory: [],
    billingAdjustmentIntent: null,
    refunds: null,
    refundStatus: "requested",
    reconciliation: null,
    reconciliationCursor: null,
    reconciliationCursorHistory: [],
    reversal: null,
    reversalIntent: null,
    reversalApprovalIntent: null,
    verification: null,
    verificationStatus: "",
    verificationOffset: 0,
    infrastructure: null,
    gatewayCells: null,
    gatewayPlacement: null,
    gatewayPlacementIntent: null,
    backup: null,
    backupIntent: null,
    grants: null,
    audit: null,
    diagnostics: null,
    workspaces: null,
    workspaceFilters: {},
    workspaceCursor: null,
    workspaceCursorHistory: [],
    selectedWorkspaceId: null,
    workspaceDetail: null,
    workspaceHealth: null,
    workspaceEvents: null,
    view: "overview",
    error: null,
    feedback: null,
    pendingAction: null,
  };

  const controller = {
    async mount() {
      bind();
      const ticket = begin();
      try {
        state.session = await api.session({ signal: ticket.signal });
        if (!current(ticket)) return false;
        clearProtectedFacts();
        return reload(ticket);
      } catch (error) {
        if (!current(ticket)) return false;
        if (isPlatformSessionFailure(error)) clearPlatformSession();
        state.loading = false;
        state.error = error instanceof PlatformApiError && [401, 403].includes(error.status)
          ? null
          : safeError(error);
        renderPlatformGate(root, state);
        return false;
      }
    },
    async openSession(totpCode) {
      const ticket = begin();
      scrubSensitive();
      clearPlatformSession();
      state.loading = true;
      state.error = null;
      renderPlatformGate(root, { loading: true });
      try {
        state.session = await api.openSession({ totpCode, signal: ticket.signal });
        if (!current(ticket)) return false;
        state.feedback = "平台身份已验证。";
        return reload(ticket);
      } catch (error) {
        if (!current(ticket)) return false;
        state.loading = false;
        state.error = safeError(error);
        renderPlatformGate(root, state);
        return false;
      }
    },
    async refresh() {
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      return reload(ticket);
    },
    selectView(view) {
      const allowed = new Set((state.navigation ?? []).map((item) => item.id));
      state.view = allowed.has(view) ? view : "overview";
      state.pendingAction = null;
      render();
    },
    async createOperator(accountId, roles) {
      return write("操作员已创建。", () => api.createOperator({ accountId, roles }));
    },
    async createPlan(plan) {
      return write("套餐版本已创建。", (signal) => api.createPlan({ plan, signal }));
    },
    async createBackup({ backupType, retentionDays }) {
      if (typeof api.createBackup !== "function") return false;
      const intent = JSON.stringify({ backupType, retentionDays: Number(retentionDays) });
      const idempotencyKey = state.backupIntent?.intent === intent
        ? state.backupIntent.idempotencyKey
        : newIdempotencyKey();
      state.backupIntent = { intent, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.backup = await api.createBackup({
          backupType,
          retentionDays: Number(retentionDays),
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.backupIntent = null;
        state.loading = false;
        state.feedback = "备份请求已创建。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.backupIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async updateGatewayPlacement({
      workspaceId,
      revision,
      primaryCellCode,
      failoverCellCode,
      residencyPolicyVersion,
      reasonCode,
      confirmed,
    }) {
      // 发布属于显式运维写入，程序化 submit 也必须携带用户确认，不能只依赖浏览器 required 行为。
      if (typeof api.updateGatewayPlacement !== "function" || confirmed !== true) return false;
      const primary = state.gatewayCells?.cells?.find(
        (cell) => cell.cell_code === primaryCellCode,
      );
      if (!primary) return false;
      const placement = {
        primary_region_code: primary.region_code,
        primary_cell_code: primary.cell_code,
        failover_policy: failoverCellCode ? "explicit" : "deny",
        failover_cell_code: failoverCellCode || null,
        residency_policy_version: String(residencyPolicyVersion ?? ""),
        reason_code: String(reasonCode ?? ""),
        confirmed: true,
      };
      const fingerprint = JSON.stringify({ workspaceId, revision: Number(revision), placement });
      const idempotencyKey = state.gatewayPlacementIntent?.fingerprint === fingerprint
        ? state.gatewayPlacementIntent.idempotencyKey
        : newIdempotencyKey();
      state.gatewayPlacementIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.gatewayPlacement = await api.updateGatewayPlacement({
          workspaceId,
          revision: Number(revision),
          placement,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.gatewayPlacementIntent = null;
        state.gatewayCells = await api.gatewayCells({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.loading = false;
        state.feedback = "Gateway placement 已发布。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.gatewayPlacementIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async refreshBackup() {
      const backupId = state.backup?.backup?.backup_id;
      if (!backupId || typeof api.backup !== "function") return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.backup = await api.backup({ backupId, signal: ticket.signal });
        if (!current(ticket)) return false;
        state.loading = false;
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async filterBilling(status) {
      state.billingStatus = normalizeBillingStatus(status);
      state.billingCursor = null;
      state.billingCursorHistory = [];
      return loadBillingPage();
    },
    async nextBillingPage() {
      const cursor = state.billing?.next_cursor;
      if (!cursor) return false;
      state.billingCursorHistory.push(state.billingCursor);
      state.billingCursor = cursor;
      return loadBillingPage();
    },
    async previousBillingPage() {
      if (state.billingCursorHistory.length === 0) return false;
      state.billingCursor = state.billingCursorHistory.pop() ?? null;
      return loadBillingPage();
    },
    async filterRefunds(status) {
      state.refundStatus = normalizeRefundStatus(status);
      return loadRefundQueue();
    },
    async approveRefund(refundId, revision) {
      if (!confirmAction("确认批准这笔退款申请？")) return false;
      return reviewRefund({
        refundId,
        revision,
        decision: "approve",
        reasonCode: "policy_approved",
        confirmed: true,
      });
    },
    async rejectRefund({ refundId, revision, reasonCode, confirmed }) {
      if (confirmed !== true) return false;
      return reviewRefund({
        refundId,
        revision,
        decision: "reject",
        reasonCode,
        confirmed: true,
      });
    },
    async createReconciliation(confirmed) {
      if (confirmed !== true || typeof api.createReconciliation !== "function") return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const result = await api.createReconciliation({ confirmed: true, signal: ticket.signal });
        if (!current(ticket)) return false;
        state.reconciliation = { ...result, differences: [], next_cursor: null };
        state.reconciliationCursor = null;
        state.reconciliationCursorHistory = [];
        state.loading = false;
        state.feedback = "冻结水位对账已创建。";
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async refreshReconciliation() {
      if (!state.reconciliation?.reconciliation?.reconciliation_run_id) return false;
      return loadReconciliationPage();
    },
    async nextReconciliationPage() {
      const cursor = state.reconciliation?.next_cursor;
      if (!cursor) return false;
      state.reconciliationCursorHistory.push(state.reconciliationCursor);
      state.reconciliationCursor = cursor;
      return loadReconciliationPage();
    },
    async previousReconciliationPage() {
      if (state.reconciliationCursorHistory.length === 0) return false;
      state.reconciliationCursor = state.reconciliationCursorHistory.pop() ?? null;
      return loadReconciliationPage();
    },
    async createInvoiceReversal({ invoiceId, reasonCode, confirmed }) {
      if (confirmed !== true || typeof api.createInvoiceReversal !== "function") return false;
      const input = { invoiceId, reasonCode, confirmed: true };
      const fingerprint = JSON.stringify(input);
      const idempotencyKey = state.reversalIntent?.fingerprint === fingerprint
        ? state.reversalIntent.idempotencyKey
        : newIdempotencyKey();
      state.reversalIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.reversal = await api.createInvoiceReversal({
          ...input,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.reversalIntent = null;
        state.loading = false;
        state.feedback = "红字冲销申请已创建，需账务与合规分别审批。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.reversalIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async reviewInvoiceReversal({ reversalId, decision, reasonCode, confirmed }) {
      if (confirmed !== true || typeof api.reviewInvoiceReversal !== "function") return false;
      const input = { reversalId, decision, reasonCode, confirmed: true };
      const fingerprint = JSON.stringify(input);
      const idempotencyKey = state.reversalApprovalIntent?.fingerprint === fingerprint
        ? state.reversalApprovalIntent.idempotencyKey
        : newIdempotencyKey();
      state.reversalApprovalIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.reversal = await api.reviewInvoiceReversal({
          ...input,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.reversalApprovalIntent = null;
        state.loading = false;
        state.feedback = decision === "approve" ? "冲红审批已记录。" : "冲红申请已拒绝。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.reversalApprovalIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async createBillingAdjustment({ targetKey, reasonCode, quantity, amount }) {
      const target = billingAdjustmentTarget(state.billing, targetKey);
      if (!target || typeof api.createBillingAdjustment !== "function") return false;
      const magnitudeQuantity = Number(quantity);
      const magnitudeAmount = String(amount ?? "");
      if (!Number.isSafeInteger(magnitudeQuantity) || magnitudeQuantity < 0
        || !/^(?:0|[1-9][0-9]{0,10})\.[0-9]{2}$/u.test(magnitudeAmount)
        || (magnitudeQuantity === 0 && magnitudeAmount === "0.00")) return false;
      const sign = reasonCode === "manual_credit" ? -1 : reasonCode === "manual_debit" ? 1 : 0;
      if (sign === 0) return false;
      const adjustment = {
        workspace_id: target.cycle.workspace_id,
        billing_cycle_id: target.cycle.billing_cycle_id,
        meter_code: target.meter.meter_code,
        reason_code: reasonCode,
        quantity_delta: sign * magnitudeQuantity,
        amount_delta: sign < 0 && magnitudeAmount !== "0.00" ? `-${magnitudeAmount}` : magnitudeAmount,
        expected_summary_sha256: target.meter.summary_sha256,
      };
      const fingerprint = JSON.stringify(adjustment);
      const idempotencyKey = state.billingAdjustmentIntent?.fingerprint === fingerprint
        ? state.billingAdjustmentIntent.idempotencyKey
        : newIdempotencyKey();
      state.billingAdjustmentIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        await api.createBillingAdjustment({ adjustment, idempotencyKey, signal: ticket.signal });
        if (!current(ticket)) return false;
        state.billingAdjustmentIntent = null;
        state.billingCursor = null;
        state.billingCursorHistory = [];
        state.feedback = "账单调整已追加。";
        return reload(ticket);
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.billingAdjustmentIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async toggleOperator(input) {
      return write("操作员状态已更新。", () => api.updateOperator(input));
    },
    async createGrant(input) {
      return write("支持申请已提交。", () => api.createGrant(input));
    },
    async approveGrant(grantId) {
      return write("平台审批已记录。", () => api.approveGrant({ grantId }));
    },
    prepareRevoke(grantId) {
      state.pendingAction = { kind: "revoke", id: grantId };
      render();
    },
    cancelConfirmation() {
      state.pendingAction = null;
      render();
    },
    async confirmRevoke(grantId) {
      state.pendingAction = null;
      return write("支持授权已撤销。", () => api.revokeGrant({
        grantId,
        reasonCode: "operator_revoked",
      }));
    },
    async openSupport(grantId) {
      const ticket = begin();
      state.loading = true;
      render();
      try {
        await api.supportSession({ grantId, signal: ticket.signal });
        const diagnostics = await api.diagnostics({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.diagnostics = diagnostics;
        state.loading = false;
        state.feedback = "诊断会话已打开。";
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async breakGlass(input) {
      const ticket = begin();
      scrubSensitive();
      state.loading = true;
      render();
      try {
        await api.breakGlass({ ...input, signal: ticket.signal });
        const diagnostics = await api.diagnostics({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.diagnostics = diagnostics;
        state.loading = false;
        state.feedback = "Break-glass 已启动并进入复核队列。";
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async filterWorkspaces(input) {
      state.workspaceFilters = normalizeWorkspaceFilters(input);
      state.workspaceCursor = null;
      state.workspaceCursorHistory = [];
      clearWorkspaceDetail();
      return loadWorkspacePage();
    },
    async nextWorkspacePage() {
      const cursor = state.workspaces?.next_cursor;
      if (!cursor) return false;
      state.workspaceCursorHistory.push(state.workspaceCursor);
      state.workspaceCursor = cursor;
      return loadWorkspacePage();
    },
    async previousWorkspacePage() {
      if (state.workspaceCursorHistory.length === 0) return false;
      state.workspaceCursor = state.workspaceCursorHistory.pop() ?? null;
      return loadWorkspacePage();
    },
    async openWorkspace(workspaceId) {
      const ticket = begin();
      state.selectedWorkspaceId = String(workspaceId ?? "");
      state.workspaceDetail = null;
      state.workspaceHealth = null;
      state.workspaceEvents = null;
      state.pendingAction = null;
      state.loading = true;
      state.error = null;
      render();
      try {
        const [detail, health, events] = await Promise.all([
          api.workspace({ workspaceId, signal: ticket.signal }),
          api.workspaceHealth({ workspaceId, signal: ticket.signal }),
          api.workspaceEvents({ workspaceId, signal: ticket.signal }),
        ]);
        if (!current(ticket)) return false;
        state.workspaceDetail = detail;
        state.workspaceHealth = health;
        state.workspaceEvents = events;
        state.loading = false;
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    closeWorkspace() {
      clearWorkspaceDetail();
      state.error = null;
      state.pendingAction = null;
      render();
    },
    prepareWorkspaceLifecycle(kind) {
      const workspace = currentWorkspace();
      if (!workspace || !["suspend", "restore"].includes(kind)) return false;
      state.pendingAction = {
        kind: `workspace-${kind}`,
        id: workspace.workspace_id,
        revision: workspace.operational_revision,
      };
      render();
      return true;
    },
    async filterVerification(status) {
      state.verificationStatus = normalizeVerificationStatus(status);
      state.verificationOffset = 0;
      return loadVerificationPage();
    },
    async nextVerificationPage() {
      if ((state.verificationOffset ?? 0) + (state.verification?.cases?.length ?? 0)
        >= (state.verification?.total ?? 0)) return false;
      state.verificationOffset += 20;
      return loadVerificationPage();
    },
    async previousVerificationPage() {
      if ((state.verificationOffset ?? 0) === 0) return false;
      state.verificationOffset = Math.max(0, state.verificationOffset - 20);
      return loadVerificationPage();
    },
    async approveVerification(intentId, revision) {
      return reviewVerification({ intentId, revision, decision: "approve", reasonCode: "verified" });
    },
    async rejectVerification(intentId, revision, reasonCode) {
      return reviewVerification({ intentId, revision, decision: "reject", reasonCode });
    },
    async issueEvidenceToken(intentId, evidenceId) {
      if (typeof api.evidenceToken !== "function") return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      let result = null;
      try {
        result = await api.evidenceToken({ intentId, evidenceId, signal: ticket.signal });
        if (result && typeof result === "object") result.download_token = "";
        result = null;
        if (!current(ticket)) return false;
        state.feedback = "证据授权已签发。";
        return reload(ticket);
      } catch (error) {
        if (result && typeof result === "object") result.download_token = "";
        result = null;
        return fail(ticket, error);
      }
    },
    async confirmWorkspaceLifecycle(reasonCode) {
      const pending = state.pendingAction;
      if (!pending || !/^workspace-(?:suspend|restore)$/u.test(pending.kind)) return false;
      const action = pending.kind.slice("workspace-".length);
      state.pendingAction = null;
      return workspaceWrite(
        action === "suspend" ? "Workspace 已冻结。" : "Workspace 已恢复。",
        (signal) => api[action === "suspend" ? "suspendWorkspace" : "restoreWorkspace"]({
          workspaceId: pending.id,
          revision: pending.revision,
          reasonCode,
          signal,
        }),
      );
    },
    async addWorkspaceRiskTag(tagCode, reasonCode) {
      const workspace = currentWorkspace();
      if (!workspace) return false;
      return workspaceWrite("风险标签已添加。", (signal) => api.addWorkspaceRiskTag({
        workspaceId: workspace.workspace_id,
        revision: workspace.risk_revision,
        tagCode,
        reasonCode,
        signal,
      }));
    },
    async removeWorkspaceRiskTag(tagCode, reasonCode) {
      const workspace = currentWorkspace();
      if (!workspace) return false;
      return workspaceWrite("风险标签已移除。", (signal) => api.removeWorkspaceRiskTag({
        workspaceId: workspace.workspace_id,
        revision: workspace.risk_revision,
        tagCode,
        reasonCode,
        signal,
      }));
    },
    async notifyWorkspaceOwner(templateCode, reasonCode) {
      const workspace = currentWorkspace();
      if (!workspace) return false;
      return workspaceWrite("Owner 通知已进入投递队列。", (signal) => api.notifyWorkspaceOwner({
        workspaceId: workspace.workspace_id,
        revision: workspace.operational_revision,
        templateCode,
        reasonCode,
        signal,
      }));
    },
    destroy() {
      generation += 1;
      request?.abort();
      root.replaceChildren();
    },
  };

  async function reload(ticket) {
    try {
      const navigation = await api.navigation({ signal: ticket.signal });
      if (!current(ticket)) return false;
      const permissions = new Set(state.session?.context?.permissions ?? []);
      const next = {
        navigation: normalizeNavigation(navigation.items),
        operators: null,
        plans: null,
        billing: null,
        refunds: null,
        verification: null,
        infrastructure: null,
        gatewayCells: null,
        grants: null,
        audit: null,
        workspaces: null,
        workspaceDetail: null,
        workspaceHealth: null,
        workspaceEvents: null,
      };
      const tasks = [];
      if (permissions.has("platform.operator.manage")) {
        tasks.push(api.operators({ signal: ticket.signal }).then((value) => { next.operators = value; }));
      }
      if (permissions.has("platform.catalog.read") && typeof api.plans === "function") {
        tasks.push(api.plans({ signal: ticket.signal }).then((value) => { next.plans = value; }));
      }
      if (permissions.has("platform.billing.read") && typeof api.billingSummary === "function") {
        tasks.push(api.billingSummary({
          status: state.billingStatus,
          cursor: state.billingCursor,
          signal: ticket.signal,
        }).then((value) => { next.billing = value; }));
      }
      if (permissions.has("platform.billing.read") && typeof api.refunds === "function") {
        tasks.push(api.refunds({
          status: state.refundStatus,
          signal: ticket.signal,
        }).then((value) => { next.refunds = value; }));
      }
      if (permissions.has("platform.verification.read")
        && typeof api.businessVerifications === "function") {
        tasks.push(api.businessVerifications({
          status: state.verificationStatus,
          offset: state.verificationOffset,
          signal: ticket.signal,
        }).then((value) => { next.verification = value; }));
      }
      if (permissions.has("platform.infrastructure.read")
        && typeof api.infrastructureStatus === "function") {
        tasks.push(api.infrastructureStatus({ signal: ticket.signal }).then((value) => {
          next.infrastructure = value;
        }));
      }
      if (permissions.has("platform.gateway.read") && typeof api.gatewayCells === "function") {
        tasks.push(api.gatewayCells({ signal: ticket.signal }).then((value) => {
          next.gatewayCells = value;
        }));
      }
      if (permissions.has("platform.support.request") || permissions.has("platform.support.approve")) {
        tasks.push(api.grants({ signal: ticket.signal }).then((value) => { next.grants = value; }));
      }
      if (permissions.has("platform.audit.read")) {
        tasks.push(api.audit({ signal: ticket.signal }).then((value) => { next.audit = value; }));
      }
      if (hasWorkspaceRead(permissions)) {
        tasks.push(api.workspaces({
          filters: state.workspaceFilters,
          cursor: state.workspaceCursor,
          signal: ticket.signal,
        }).then((value) => { next.workspaces = value; }));
        if (state.selectedWorkspaceId) {
          const workspaceId = state.selectedWorkspaceId;
          tasks.push(api.workspace({ workspaceId, signal: ticket.signal }).then((value) => {
            next.workspaceDetail = value;
          }));
          tasks.push(api.workspaceHealth({ workspaceId, signal: ticket.signal }).then((value) => {
            next.workspaceHealth = value;
          }));
          tasks.push(api.workspaceEvents({ workspaceId, signal: ticket.signal }).then((value) => {
            next.workspaceEvents = value;
          }));
        }
      }
      await Promise.all(tasks);
      if (!current(ticket)) return false;
      Object.assign(state, next);
      if (!state.navigation.some((item) => item.id === state.view)) state.view = "overview";
      state.loading = false;
      state.error = null;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadWorkspacePage() {
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const workspaces = await api.workspaces({
        filters: state.workspaceFilters,
        cursor: state.workspaceCursor,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.workspaces = workspaces;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadBillingPage() {
    if (typeof api.billingSummary !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const billing = await api.billingSummary({
        status: state.billingStatus,
        cursor: state.billingCursor,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.billing = billing;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadRefundQueue() {
    if (typeof api.refunds !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.refunds = await api.refunds({
        status: state.refundStatus,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadReconciliationPage() {
    const reconciliationId = state.reconciliation?.reconciliation?.reconciliation_run_id;
    if (!reconciliationId || typeof api.reconciliationDifferences !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.reconciliation = await api.reconciliationDifferences({
        reconciliationId,
        cursor: state.reconciliationCursor,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function reviewRefund({ refundId, revision, decision, reasonCode, confirmed }) {
    if (typeof api.reviewRefund !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await api.reviewRefund({
        refundId,
        revision: Number(revision),
        decision,
        reasonCode,
        confirmed,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.feedback = decision === "approve" ? "退款申请已批准。" : "退款申请已拒绝。";
      return reload(ticket);
    } catch (error) {
      return fail(ticket, error);
    }
  }

  /** 读取有界 Platform 审核分页，权限仍由后端按当前 Session 动态裁决。 */
  async function loadVerificationPage() {
    if (typeof api.businessVerifications !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.verification = await api.businessVerifications({
        status: state.verificationStatus,
        offset: state.verificationOffset,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  /** 以新幂等键和案件 CAS 提交审核决定，成功后重新读取安全队列投影。 */
  async function reviewVerification({ intentId, revision, decision, reasonCode }) {
    if (typeof api.reviewBusinessVerification !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await api.reviewBusinessVerification({
        intentId,
        revision: Number(revision),
        decision,
        reasonCode,
        idempotencyKey: newIdempotencyKey(),
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.feedback = decision === "approve" ? "认证已批准。" : "认证已拒绝。";
      return reload(ticket);
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function write(message, operation) {
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(ticket.signal);
      if (!current(ticket)) return false;
      state.feedback = message;
      return reload(ticket);
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function workspaceWrite(message, operation) {
    const workspaceId = state.selectedWorkspaceId;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(ticket.signal);
      if (!current(ticket)) return false;
      state.feedback = message;
      state.workspaceCursor = null;
      state.workspaceCursorHistory = [];
      const refresh = begin();
      const [workspaces, detail, health, events] = await Promise.all([
        api.workspaces({ filters: state.workspaceFilters, cursor: null, signal: refresh.signal }),
        api.workspace({ workspaceId, signal: refresh.signal }),
        api.workspaceHealth({ workspaceId, signal: refresh.signal }),
        api.workspaceEvents({ workspaceId, signal: refresh.signal }),
      ]);
      if (!current(refresh)) return false;
      state.workspaces = workspaces;
      state.workspaceDetail = detail;
      state.workspaceHealth = health;
      state.workspaceEvents = events;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      const activeTicket = current(ticket) ? ticket : { generation, signal: request.signal };
      return fail(activeTicket, error);
    }
  }

  function fail(ticket, error) {
    if (!current(ticket) || error?.name === "AbortError") return false;
    if (isPlatformSessionFailure(error)) {
      clearPlatformSession();
      state.loading = false;
      state.error = null;
      renderPlatformGate(root, state);
      return false;
    }
    state.loading = false;
    state.error = safeError(error);
    render();
    return false;
  }

  function bind() {
    root.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof globalThis.HTMLFormElement)) return;
      event.preventDefault();
      const data = new FormData(form);
      if (form.matches("[data-platform-session-form]")) controller.openSession(data.get("totpCode"));
      if (form.matches("[data-operator-form]")) controller.createOperator(data.get("accountId"), data.getAll("roles"));
      if (form.matches("[data-platform-plan-form]")) {
        controller.createPlan({
          plan_code: data.get("planCode"),
          display_name: data.get("displayName"),
          display_order: Number(data.get("displayOrder")),
          billing_period: data.get("billingPeriod"),
          currency: data.get("currency"),
          unit_price: data.get("unitPrice"),
          trial_days: Number(data.get("trialDays")),
          entitlements: {
            gateway_requests_per_month: Number(data.get("gatewayRequests")),
            workspace_seats: Number(data.get("workspaceSeats")),
            model_aliases: Number(data.get("modelAliases")),
            retention_days: Number(data.get("retentionDays")),
            priority_support: data.get("prioritySupport") === "on",
          },
          publish: data.get("publish") === "on",
        });
      }
      if (form.matches("[data-platform-billing-filter-form]")) {
        controller.filterBilling(data.get("status"));
      }
      if (form.matches("[data-platform-refund-filter-form]")) {
        controller.filterRefunds(data.get("status"));
      }
      if (form.matches("[data-platform-refund-reject-form]")) {
        controller.rejectRefund({
          refundId: data.get("refundId"),
          revision: Number(data.get("revision")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-reconciliation-form]")) {
        controller.createReconciliation(data.get("confirmed") === "on");
      }
      if (form.matches("[data-platform-reversal-form]")) {
        controller.createInvoiceReversal({
          invoiceId: data.get("invoiceId"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-reversal-review-form]")) {
        controller.reviewInvoiceReversal({
          reversalId: data.get("reversalId"),
          decision: data.get("decision"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-verification-filter-form]")) {
        controller.filterVerification(data.get("status"));
      }
      if (form.matches("[data-platform-verification-reject-form]")) {
        controller.rejectVerification(
          data.get("intentId"),
          Number(data.get("revision")),
          data.get("reasonCode"),
        );
      }
      if (form.matches("[data-platform-billing-adjustment-form]")) {
        controller.createBillingAdjustment({
          targetKey: data.get("targetKey"),
          reasonCode: data.get("reasonCode"),
          quantity: data.get("quantity"),
          amount: data.get("amount"),
        });
      }
      if (form.matches("[data-platform-backup-form]")) {
        controller.createBackup({
          backupType: data.get("backupType"),
          retentionDays: data.get("retentionDays"),
        });
      }
      if (form.matches("[data-platform-gateway-placement-form]")) {
        controller.updateGatewayPlacement({
          workspaceId: data.get("workspaceId"),
          revision: data.get("revision"),
          primaryCellCode: data.get("primaryCellCode"),
          failoverCellCode: data.get("failoverCellCode"),
          residencyPolicyVersion: data.get("residencyPolicyVersion"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-support-grant-form]")) controller.createGrant({ workspaceId: data.get("workspaceId"), reasonCode: data.get("reasonCode"), durationMinutes: data.get("durationMinutes") });
      if (form.matches("[data-break-glass-form]")) {
        const input = { workspaceId: data.get("workspaceId"), reasonCode: data.get("reasonCode"), totpCode: data.get("totpCode") };
        form.querySelectorAll('input[type="password"], input[name="totpCode"]').forEach((node) => { node.value = ""; });
        controller.breakGlass(input);
      }
      if (form.matches("[data-workspace-filter-form]")) {
        controller.filterWorkspaces(Object.fromEntries(data.entries()));
      }
      if (form.matches("[data-workspace-lifecycle-form]")) {
        controller.confirmWorkspaceLifecycle(data.get("reasonCode"));
      }
      if (form.matches("[data-workspace-risk-form]")) {
        controller.addWorkspaceRiskTag(data.get("tagCode"), data.get("reasonCode"));
      }
      if (form.matches("[data-workspace-notification-form]")) {
        controller.notifyWorkspaceOwner(data.get("templateCode"), data.get("reasonCode"));
      }
    });
    root.addEventListener("click", (event) => {
      const target = event.target.closest("button, [data-platform-view]");
      if (!target) return;
      if (target.dataset.platformView) {
        const selectedView = target.dataset.platformView;
        controller.selectView(selectedView);
        [...root.querySelectorAll("[data-platform-view]")]
          .find((item) => item.dataset.platformView === selectedView)
          ?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      }
      const action = target.dataset.platformAction;
      if (action === "refresh") controller.refresh();
      if (action === "approve-grant") controller.approveGrant(target.dataset.grantId);
      if (action === "open-support") controller.openSupport(target.dataset.grantId);
      if (action === "prepare-revoke") controller.prepareRevoke(target.dataset.grantId);
      if (action === "cancel-confirm") controller.cancelConfirmation();
      if (action === "confirm-revoke") controller.confirmRevoke(target.dataset.grantId);
      if (action === "toggle-operator") controller.toggleOperator({ operatorId: target.dataset.operatorId, revision: Number(target.dataset.revision), status: target.dataset.status, roles: String(target.dataset.roles ?? "").split(",").filter(Boolean) });
      if (action === "open-workspace") controller.openWorkspace(target.dataset.workspaceId);
      if (action === "close-workspace") controller.closeWorkspace();
      if (action === "workspace-next") controller.nextWorkspacePage();
      if (action === "workspace-previous") controller.previousWorkspacePage();
      if (action === "billing-next") controller.nextBillingPage();
      if (action === "billing-previous") controller.previousBillingPage();
      if (action === "refund-approve") {
        controller.approveRefund(target.dataset.refundId, Number(target.dataset.revision));
      }
      if (action === "reconciliation-refresh") controller.refreshReconciliation();
      if (action === "reconciliation-next") controller.nextReconciliationPage();
      if (action === "reconciliation-previous") controller.previousReconciliationPage();
      if (action === "verification-next") controller.nextVerificationPage();
      if (action === "verification-previous") controller.previousVerificationPage();
      if (action === "verification-approve") {
        controller.approveVerification(target.dataset.intentId, Number(target.dataset.revision));
      }
      if (action === "verification-evidence-token") {
        controller.issueEvidenceToken(target.dataset.intentId, target.dataset.evidenceId);
      }
      if (action === "backup-refresh") controller.refreshBackup();
      if (action === "prepare-workspace-lifecycle") controller.prepareWorkspaceLifecycle(target.dataset.lifecycleAction);
      if (action === "remove-workspace-risk") controller.removeWorkspaceRiskTag(target.dataset.tagCode, target.dataset.reasonCode);
    });
  }

  function begin() {
    generation += 1;
    request?.abort();
    request = new AbortController();
    return { generation, signal: request.signal };
  }
  function current(ticket) { return ticket.generation === generation && !ticket.signal.aborted; }
  function render() { state.session ? renderPlatformWorkspace(root, state) : renderPlatformGate(root, state); }
  function scrubSensitive() { root.querySelectorAll('input[type="password"], input[name="totpCode"]').forEach((node) => { node.value = ""; }); }
  function currentWorkspace() { return state.workspaceDetail?.workspace ?? null; }
  function clearWorkspaceDetail() {
    state.selectedWorkspaceId = null;
    state.workspaceDetail = null;
    state.workspaceHealth = null;
    state.workspaceEvents = null;
  }
  function clearProtectedFacts() {
    state.navigation = [];
    state.operators = null;
    state.plans = null;
    state.billing = null;
    state.billingStatus = "";
    state.billingCursor = null;
    state.billingCursorHistory = [];
    state.billingAdjustmentIntent = null;
    state.refunds = null;
    state.refundStatus = "requested";
    state.reconciliation = null;
    state.reconciliationCursor = null;
    state.reconciliationCursorHistory = [];
    state.reversal = null;
    state.reversalIntent = null;
    state.reversalApprovalIntent = null;
    state.verification = null;
    state.verificationStatus = "";
    state.verificationOffset = 0;
    state.infrastructure = null;
    state.gatewayCells = null;
    state.gatewayPlacement = null;
    state.gatewayPlacementIntent = null;
    state.backup = null;
    state.backupIntent = null;
    state.grants = null;
    state.audit = null;
    state.diagnostics = null;
    state.workspaces = null;
    state.workspaceFilters = {};
    state.workspaceCursor = null;
    state.workspaceCursorHistory = [];
    clearWorkspaceDetail();
    state.pendingAction = null;
    state.view = "overview";
  }
  function clearPlatformSession() {
    state.session = null;
    clearProtectedFacts();
  }
  return controller;
}

function normalizeNavigation(items) {
  const values = Array.isArray(items) ? items.filter((item) => item && typeof item.id === "string") : [];
  const seen = new Set();
  const authorized = values.filter((item) => {
    if (!PLATFORM_NAVIGATION_IDS.has(item.id) || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
  return [{ id: "overview", label: "概览", href: "/platform.html#overview" }, ...authorized];
}

function normalizeWorkspaceFilters(input) {
  const filters = {};
  for (const [key, value] of Object.entries(input ?? {})) {
    const text = String(value ?? "").trim();
    if (WORKSPACE_FILTER_KEYS.has(key) && text) filters[key] = text;
  }
  return filters;
}

function hasWorkspaceRead(permissions) {
  return [...WORKSPACE_READ_PERMISSIONS].some((permission) => permissions.has(permission));
}

function normalizeBillingStatus(value) {
  const text = String(value ?? "");
  return new Set(["", "open", "closed", "reconciliation_required"]).has(text) ? text : "";
}

function normalizeRefundStatus(value) {
  const text = String(value ?? "");
  return new Set(["requested", "processing", "succeeded", "failed", "rejected"]).has(text)
    ? text
    : "requested";
}

/** 将审核筛选限制为固定状态枚举，未知输入回退为无筛选而不透传。 */
function normalizeVerificationStatus(value) {
  const text = String(value ?? "");
  return new Set(["", "submitted", "in_review", "approved", "rejected", "expired"])
    .has(text) ? text : "";
}

function billingAdjustmentTarget(billing, targetKey) {
  const [cycleId, meterCode, ...extra] = String(targetKey ?? "").split(":");
  if (extra.length > 0) return null;
  const cycle = billing?.cycles?.find((item) => item.billing_cycle_id === cycleId);
  const meter = cycle?.meters?.find((item) => item.meter_code === meterCode);
  if (!cycle || cycle.status === "open" || !meter?.summary_sha256) return null;
  return { cycle, meter };
}

function defaultIdempotencyKey() {
  const value = globalThis.crypto?.randomUUID?.();
  if (!value) throw new TypeError("secure idempotency key generation is unavailable");
  return `platform-billing-${value}`;
}

function defaultConfirm(message) {
  return typeof globalThis.confirm === "function" ? globalThis.confirm(message) : false;
}

function isPlatformSessionFailure(error) {
  return error instanceof PlatformApiError
    && error.status === 401
    && /^platform\.session_(?:required|invalid|expired|revoked)$/u.test(error.code);
}

function safeError(error) {
  if (error instanceof PlatformApiError) {
    return {
      message: error.message,
      requestId: error.requestId,
      code: error.code,
      status: error.status,
    };
  }
  return { message: "平台请求未完成，请稍后重试。", requestId: "-", code: "platform.request_failed", status: 0 };
}
