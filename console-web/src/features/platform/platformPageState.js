// 平台页面状态机负责取消过期请求、权限裁剪和幂等意图；敏感认证值只由 HttpOnly Cookie 承载。
import { PlatformApiError } from "../../lib/platform.js";
import { NOTIFICATION_EVENT_PARAMETERS } from "../../lib/notifications.js";
import { renderPlatformGate, renderPlatformWorkspace } from "./renderPlatformPage.js";

const PLATFORM_NAVIGATION_IDS = new Set([
  "infrastructure",
  "gateway",
  "job-runtime",
  "feature-rollouts",
  "config-distribution",
  "observability",
  "disaster-recovery",
  "status-incidents",
  "capacity",
  "abuse-risk",
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
  "notification-templates",
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
    notificationTemplates: null,
    notificationTemplateIntent: null,
    infrastructure: null,
    gatewayCells: null,
    gatewayPlacement: null,
    gatewayPlacementIntent: null,
    jobQueues: null,
    jobStatus: "",
    selectedJob: null,
    jobCommandIntent: null,
    jobTypeIntent: null,
    featureFlags: null,
    featureStatus: "",
    featureRiskLevel: "",
    featureCommandIntent: null,
    configDistribution: null,
    configRebuildIntent: null,
    observabilityServices: null,
    serviceLevelObjectives: null,
    errorBudgets: null,
    observabilityIntent: null,
    disasterRecoveryStatus: null,
    disasterRecoveryBackups: null,
    disasterRecoveryIntent: null,
    statusIncidents: null,
    statusIncidentIntent: null,
    capacity: null,
    unitEconomics: null,
    capacityIntent: null,
    riskCases: null,
    riskStatus: "",
    riskCursor: null,
    riskCursorHistory: [],
    riskDecisionIntent: null,
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
      if (state.loading && (state.disasterRecoveryIntent || state.statusIncidentIntent
        || state.capacityIntent)) return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
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
    selectView(view) {
      if (state.loading && (state.disasterRecoveryIntent || state.statusIncidentIntent
        || state.capacityIntent)) return false;
      const allowed = new Set((state.navigation ?? []).map((item) => item.id));
      const nextView = allowed.has(view) ? view : "overview";
      if (new Set([
        "job-runtime", "feature-rollouts", "config-distribution", "observability",
        "disaster-recovery", "status-incidents", "capacity", "abuse-risk",
      ]).has(state.view)
        && nextView !== state.view) {
        generation += 1;
        request?.abort();
        request = null;
        if (state.view === "job-runtime") {
          state.selectedJob = null;
          state.jobCommandIntent = null;
          state.jobTypeIntent = null;
        } else if (state.view === "feature-rollouts") {
          state.featureCommandIntent = null;
        } else if (state.view === "config-distribution") {
          state.configRebuildIntent = null;
        } else if (state.view === "observability") {
          state.observabilityIntent = null;
        } else if (state.view === "disaster-recovery") {
          state.disasterRecoveryIntent = null;
        } else if (state.view === "status-incidents") {
          state.statusIncidentIntent = null;
        } else if (state.view === "capacity") {
          state.capacityIntent = null;
        } else {
          state.riskDecisionIntent = null;
        }
        state.loading = false;
      }
      state.view = nextView;
      state.pendingAction = null;
      render();
      if (state.view === "job-runtime" && !state.jobQueues) void loadJobRuntime();
      if (state.view === "feature-rollouts" && !state.featureFlags) {
        void loadFeatureRollouts();
      }
      if (state.view === "config-distribution" && !state.configDistribution) {
        void loadConfigDistribution();
      }
      if (state.view === "observability" && !state.observabilityServices) {
        void loadObservability();
      }
      if (state.view === "disaster-recovery" && !state.disasterRecoveryStatus) {
        void loadDisasterRecovery();
      }
      if (state.view === "status-incidents" && !state.statusIncidents) {
        void loadStatusIncidents();
      }
      if (state.view === "capacity" && !state.capacity && !state.unitEconomics) {
        void loadCapacity();
      }
      if (state.view === "abuse-risk" && !state.riskCases) void loadRiskCases();
    },
    async filterJobs(status) {
      state.jobStatus = normalizeJobStatus(status);
      state.selectedJob = null;
      return loadJobRuntime();
    },
    async filterFeatureFlags(status, riskLevel) {
      state.featureStatus = normalizeFeatureStatus(status);
      state.featureRiskLevel = normalizeFeatureRisk(riskLevel);
      state.featureCommandIntent = null;
      return loadFeatureRollouts();
    },
    async openJob(jobId) {
      if (typeof api.job !== "function") return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.selectedJob = await api.job({ jobId, signal: ticket.signal });
        if (!current(ticket)) return false;
        state.loading = false;
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    closeJob() {
      state.selectedJob = null;
      state.jobCommandIntent = null;
      render();
    },
    async commandJob({ jobId, revision, action, reasonCode, confirmed }) {
      if (confirmed !== true || !new Set(["retry", "cancel"]).has(action)) return false;
      const operation = action === "retry" ? api.retryJob : api.cancelJob;
      if (typeof operation !== "function") return false;
      const fingerprint = JSON.stringify({ jobId, revision: Number(revision), action, reasonCode });
      const idempotencyKey = state.jobCommandIntent?.fingerprint === fingerprint
        ? state.jobCommandIntent.idempotencyKey
        : newIdempotencyKey();
      state.jobCommandIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        await operation({
          jobId,
          revision: Number(revision),
          reasonCode,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        const [queues, detail] = await Promise.all([
          api.jobQueues({ status: state.jobStatus, signal: ticket.signal }),
          api.job({ jobId, signal: ticket.signal }),
        ]);
        if (!current(ticket)) return false;
        state.jobQueues = queues;
        state.selectedJob = detail;
        state.jobCommandIntent = null;
        state.loading = false;
        state.feedback = action === "retry" ? "任务已重新入队。" : "取消请求已提交。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.jobCommandIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async commandJobType({ typeCode, revision, action, reasonCode, confirmed }) {
      if (confirmed !== true || typeof api.commandJobType !== "function") return false;
      const fingerprint = JSON.stringify({ typeCode, revision: Number(revision), action, reasonCode });
      const idempotencyKey = state.jobTypeIntent?.fingerprint === fingerprint
        ? state.jobTypeIntent.idempotencyKey
        : newIdempotencyKey();
      state.jobTypeIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        await api.commandJobType({
          typeCode,
          revision: Number(revision),
          action,
          reasonCode,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.jobQueues = await api.jobQueues({
          status: state.jobStatus,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.jobTypeIntent = null;
        state.loading = false;
        state.feedback = action === "activate" ? "任务类型已启用。" : "任务类型已停用。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.jobTypeIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async createFeatureFlag({ flag, revision = null } = {}) {
      if (typeof api.createFeatureFlag !== "function"
        || !currentPermissions().has("platform.feature.write")
        || flag?.confirmed !== true) return false;
      return featureWrite(
        { action: flag.action, flag, revision },
        (idempotencyKey, signal) => api.createFeatureFlag({
          flag,
          revision,
          idempotencyKey,
          signal,
        }),
        flag.action === "version" ? "候选版本已追加。" : "功能开关已创建。",
      );
    },
    async commandFeatureRollout(command) {
      const permission = command?.action === "approve"
        ? "platform.feature.approve"
        : "platform.feature.write";
      if (typeof api.commandFeatureRollout !== "function"
        || !currentPermissions().has(permission) || command?.confirmed !== true) return false;
      const input = {
        flagId: command.flagId,
        rolloutId: command.action === "start" ? null : command.rolloutId,
        action: command.action,
        versionNumber: command.action === "start" ? Number(command.versionNumber) : null,
        percentageBps: command.action === "start" ? Number(command.percentageBps) : null,
        environmentCode: command.action === "start" ? command.environmentCode : null,
        regionCode: command.action === "start" ? command.regionCode : null,
        revision: Number(command.revision),
      };
      return featureWrite(
        input,
        (idempotencyKey, signal) => api.commandFeatureRollout({
          ...input,
          idempotencyKey,
          signal,
        }),
        ({
          start: "发布阶梯已提交。",
          approve: "发布审批已记录。",
          pause: "发布已暂停。",
          resume: "发布恢复任务已提交。",
          rollback: "回滚任务已提交。",
        })[command.action] ?? "发布命令已提交。",
      );
    },
    async killFeatureFlag(command) {
      if (typeof api.killFeatureFlag !== "function"
        || !currentPermissions().has("platform.feature.kill")
        || command?.confirmed !== true) return false;
      const input = {
        flagId: command.flagId,
        revision: Number(command.revision),
        reasonCode: command.reasonCode,
      };
      return featureWrite(
        { action: "kill", ...input },
        (idempotencyKey, signal) => api.killFeatureFlag({
          ...input,
          idempotencyKey,
          signal,
        }),
        "Kill fence 已同步生效。",
      );
    },
    async rebuildWorkspaceConfig(command) {
      if (typeof api.rebuildWorkspaceConfig !== "function"
        || typeof api.configDistribution !== "function"
        || !currentPermissions().has("platform.config_distribution.rebuild")
        || command?.confirmed !== true) return false;
      const input = {
        workspaceId: command.workspaceId,
        reasonCode: command.reasonCode,
      };
      const fingerprint = JSON.stringify(input);
      const idempotencyKey = state.configRebuildIntent?.fingerprint === fingerprint
        ? state.configRebuildIntent.idempotencyKey
        : newIdempotencyKey();
      state.configRebuildIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        await api.rebuildWorkspaceConfig({
          ...input,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.configDistribution = await api.configDistribution({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.configRebuildIntent = null;
        state.loading = false;
        state.feedback = "配置重建任务已提交。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.configRebuildIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async createServiceLevelObjective({ slo } = {}) {
      if (typeof api.createServiceLevelObjective !== "function"
        || !currentPermissions().has("platform.observability.manage")
        || slo?.confirmed !== true) return false;
      return observabilityWrite(
        { action: "slo.version", slo },
        (idempotencyKey, signal) => api.createServiceLevelObjective({
          slo,
          idempotencyKey,
          signal,
        }),
        slo.previous_version === 0 ? "SLO 已创建。" : "SLO 新版本已追加。",
      );
    },
    async acknowledgePlatformAlert(command) {
      if (typeof api.acknowledgePlatformAlert !== "function"
        || !currentPermissions().has("platform.observability.alert_ack")
        || command?.confirmed !== true) return false;
      const input = {
        alertId: command.alertId,
        revision: Number(command.revision),
        reasonCode: command.reasonCode,
      };
      return observabilityWrite(
        { action: "alert.ack", ...input },
        (idempotencyKey, signal) => api.acknowledgePlatformAlert({
          ...input,
          idempotencyKey,
          signal,
        }),
        "告警确认已记录。",
      );
    },
    async refreshDisasterRecovery() {
      if (state.loading && state.disasterRecoveryIntent) return false;
      return loadDisasterRecovery();
    },
    async voteDisasterRecovery(command) {
      if (command?.confirmed !== true) return false;
      const permissions = currentPermissions();
      const review = command.voteKind === "security_review";
      if (review) {
        if (!permissions.has("platform.dr.approve")
          || !permissions.has("platform.dr.security_review")) return false;
      } else if (command.voteKind !== "execution"
        || !permissions.has("platform.dr.execute")) return false;
      const operation = command.runKind === "drill"
        ? api.startDisasterRecoveryDrill
        : command.runKind === "failover" ? api.startRegionFailover : null;
      if (typeof operation !== "function") return false;
      const input = {
        failoverPlanId: command.failoverPlanId,
        planVersion: Number(command.planVersion),
        reasonCode: command.reasonCode,
        expectedEpoch: Number(command.expectedEpoch),
      };
      if (command.runKind === "failover") input.regionId = command.regionId;
      return disasterRecoveryWrite(
        { runKind: command.runKind, voteKind: command.voteKind, ...input },
        (idempotencyKey, signal) => operation({ ...input, idempotencyKey, signal }),
        review
          ? "安全复核票已记录；满足双人控制后执行将进入队列。"
          : "执行票已记录，等待独立安全复核。",
      );
    },
    async refreshStatusIncidents() {
      if (state.loading && state.statusIncidentIntent) return false;
      return loadStatusIncidents();
    },
    async createStatusIncident(command) {
      if (command?.confirmed !== true
        || !currentPermissions().has("platform.status.write")
        || typeof api.createStatusIncident !== "function") return false;
      const input = {
        ownerWorkspaceId: command.ownerWorkspaceId,
        title: command.title,
        classification: command.classification,
        visibility: command.visibility,
        severity: command.severity,
        acknowledgedAt: command.acknowledgedAt,
        initialUpdate: command.classification === "security" ? null : command.initialUpdate,
        affectedComponents: command.affectedComponents,
        affectedRegions: command.affectedRegions,
      };
      return statusIncidentWrite(input, (idempotencyKey, signal) => api.createStatusIncident({
        ...input, idempotencyKey, signal,
      }), "Incident created.");
    },
    async mutateStatusIncident(command) {
      if (command?.confirmed !== true) return false;
      const approval = command.action === "security-approval";
      const operation = approval ? api.approveStatusIncident : api.updateStatusIncident;
      if (typeof operation !== "function") return false;
      if (approval) {
        if (!currentPermissions().has("platform.status.security_approve")) return false;
      } else if (command.action !== "update"
        || !currentPermissions().has("platform.status.write")) return false;
      const input = {
        incidentId: command.incidentId,
        revision: Number(command.revision),
        publicMessage: command.publicMessage,
        affectedComponents: command.affectedComponents,
        affectedRegions: command.affectedRegions,
        ...(approval ? {} : { status: command.status }),
      };
      return statusIncidentWrite(
        { action: command.action, ...input },
        (idempotencyKey, signal) => operation({ ...input, idempotencyKey, signal }),
        approval ? "Security disclosure approved." : "Incident update published.",
      );
    },
    async publishStatusPostmortem(command) {
      if (command?.confirmed !== true
        || !currentPermissions().has("platform.status.write")
        || typeof api.publishStatusPostmortem !== "function") return false;
      const input = {
        incidentId: command.incidentId,
        revision: Number(command.revision),
        summary: command.summary,
        timeline: command.timeline,
        causeCategory: command.causeCategory,
        correctiveActions: command.correctiveActions,
      };
      return statusIncidentWrite(input, (idempotencyKey, signal) => api.publishStatusPostmortem({
        ...input, idempotencyKey, signal,
      }), "Postmortem published.");
    },
    async createStatusMaintenance(command) {
      if (command?.confirmed !== true
        || !currentPermissions().has("platform.status.write")
        || typeof api.createStatusMaintenance !== "function") return false;
      const input = {
        ownerWorkspaceId: command.ownerWorkspaceId,
        title: command.title,
        publicMessage: command.publicMessage,
        visibility: command.visibility,
        affectedComponents: command.affectedComponents,
        affectedRegions: command.affectedRegions,
        startsAt: command.startsAt,
        endsAt: command.endsAt,
      };
      return statusIncidentWrite(input, (idempotencyKey, signal) => api.createStatusMaintenance({
        ...input, idempotencyKey, signal,
      }), "Maintenance window scheduled.");
    },
    async refreshCapacity() {
      if (state.loading && state.capacityIntent) return false;
      return loadCapacity();
    },
    async createCapacityForecast(command) {
      if (command?.confirmed !== true
        || !currentPermissions().has("platform.capacity.test")
        || typeof api.createCapacityForecast !== "function") return false;
      const input = {
        serviceCode: command.serviceCode,
        regionCode: command.regionCode,
        horizonDays: Number(command.horizonDays),
        expectedRevision: Number(command.expectedRevision),
        confirmed: true,
      };
      return capacityWrite(input, (idempotencyKey, signal) => api.createCapacityForecast({
        ...input, idempotencyKey, signal,
      }), "Capacity forecast queued.");
    },
    async createCapacityLoadTest(command) {
      if (command?.confirmed !== true
        || !currentPermissions().has("platform.capacity.test")
        || typeof api.createCapacityLoadTest !== "function") return false;
      const input = {
        scenarioCode: command.scenarioCode,
        environmentClass: command.environmentClass,
        regionCode: command.regionCode,
        syntheticTenantCount: Number(command.syntheticTenantCount),
        targetRps: Number(command.targetRps),
        durationSeconds: Number(command.durationSeconds),
        confirmed: true,
      };
      return capacityWrite(input, (idempotencyKey, signal) => api.createCapacityLoadTest({
        ...input, idempotencyKey, signal,
      }), "Load test queued in the isolated runner.");
    },
    async commandCapacityChaos(command) {
      if (command?.confirmed !== true
        || !currentPermissions().has("platform.capacity.chaos")
        || typeof api.commandCapacityChaos !== "function") return false;
      const input = command.action === "create" ? {
        action: "create",
        faultCode: command.faultCode,
        serviceCode: command.serviceCode,
        environmentClass: command.environmentClass,
        regionCode: command.regionCode,
        blastTenantCount: Number(command.blastTenantCount),
        durationSeconds: Number(command.durationSeconds),
        objectiveP95Ms: Number(command.objectiveP95Ms),
        objectiveErrorRateBps: Number(command.objectiveErrorRateBps),
        stopErrorRateBps: Number(command.stopErrorRateBps),
        reasonCode: command.reasonCode,
        confirmed: true,
      } : {
        action: command.action,
        experimentId: command.experimentId,
        revision: Number(command.revision),
        reasonCode: command.reasonCode,
        confirmed: true,
      };
      return capacityWrite(input, (idempotencyKey, signal) => api.commandCapacityChaos({
        ...input, idempotencyKey, signal,
      }), command.action === "kill"
        ? "Chaos kill fence advanced."
        : command.action === "approve" ? "Chaos approval recorded." : "Chaos experiment created.");
    },
    async filterRiskCases(status) {
      state.riskStatus = normalizeRiskStatus(status);
      state.riskCursor = null;
      state.riskCursorHistory = [];
      state.riskDecisionIntent = null;
      return loadRiskCases();
    },
    async nextRiskPage() {
      const next = state.riskCases?.next_cursor;
      if (!next) return false;
      state.riskCursorHistory.push(state.riskCursor);
      state.riskCursor = next;
      return loadRiskCases();
    },
    async previousRiskPage() {
      if (!state.riskCursorHistory.length) return false;
      state.riskCursor = state.riskCursorHistory.pop() ?? null;
      return loadRiskCases();
    },
    async decideRiskCase(command) {
      if (typeof api.decideRiskCase !== "function"
        || !currentPermissions().has("platform.risk.decide")
        || command?.confirmed !== true) return false;
      const input = {
        caseId: command.caseId,
        revision: Number(command.revision),
        decision: command.decision,
        reasonCode: command.reasonCode,
        restrictionKind: command.decision === "restricted"
          ? command.restrictionKind : null,
        restrictionMinutes: command.decision === "restricted"
          ? Number(command.restrictionMinutes) : null,
      };
      return riskWrite(input, (idempotencyKey, signal) => api.decideRiskCase({
        ...input,
        idempotencyKey,
        signal,
      }));
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
    async createNotificationTemplate(template) {
      if (
        typeof api.createNotificationTemplate !== "function"
        || !Number.isSafeInteger(state.notificationTemplates?.catalog_revision)
      ) return false;
      const fingerprint = JSON.stringify(template);
      const idempotencyKey = state.notificationTemplateIntent?.fingerprint === fingerprint
        ? state.notificationTemplateIntent.idempotencyKey
        : newIdempotencyKey();
      state.notificationTemplateIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        await api.createNotificationTemplate({
          revision: state.notificationTemplates.catalog_revision,
          idempotencyKey,
          template,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.notificationTemplateIntent = null;
        state.notificationTemplates = await api.notificationTemplates({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.loading = false;
        state.feedback = template.status === "published" ? "模板版本已发布。" : "模板草稿已创建。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.notificationTemplateIntent = null;
        }
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
        notificationTemplates: null,
        infrastructure: null,
        gatewayCells: null,
        jobQueues: null,
        featureFlags: null,
        configDistribution: null,
        disasterRecoveryStatus: null,
        disasterRecoveryBackups: null,
        capacity: null,
        unitEconomics: null,
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
      if (permissions.has("platform.notification_template.read")
        && typeof api.notificationTemplates === "function") {
        tasks.push(api.notificationTemplates({ signal: ticket.signal }).then((value) => {
          next.notificationTemplates = value;
        }));
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
      if (permissions.has("platform.job.read") && typeof api.jobQueues === "function") {
        tasks.push(api.jobQueues({
          status: state.jobStatus,
          signal: ticket.signal,
        }).then((value) => { next.jobQueues = value; }));
      }
      if (permissions.has("platform.feature.read") && typeof api.featureFlags === "function") {
        tasks.push(api.featureFlags({
          status: state.featureStatus,
          riskLevel: state.featureRiskLevel,
          signal: ticket.signal,
        }).then((value) => { next.featureFlags = value; }));
      }
      if (permissions.has("platform.config_distribution.read")
        && typeof api.configDistribution === "function") {
        tasks.push(api.configDistribution({ signal: ticket.signal }).then((value) => {
          next.configDistribution = value;
        }));
      }
      if ((permissions.has("platform.observability.read")
        || permissions.has("platform.observability.security_read"))
        && typeof api.observabilityServices === "function"
        && typeof api.serviceLevelObjectives === "function"
        && typeof api.errorBudgets === "function") {
        tasks.push(Promise.all([
          api.observabilityServices({ signal: ticket.signal }),
          api.serviceLevelObjectives({ signal: ticket.signal }),
          api.errorBudgets({ signal: ticket.signal }),
        ]).then(([services, slos, budgets]) => {
          next.observabilityServices = services;
          next.serviceLevelObjectives = slos;
          next.errorBudgets = budgets;
        }));
      }
      if (permissions.has("platform.dr.read")
        && typeof api.disasterRecoveryStatus === "function"
        && typeof api.disasterRecoveryBackups === "function") {
        tasks.push(Promise.all([
          api.disasterRecoveryStatus({ signal: ticket.signal }),
          api.disasterRecoveryBackups({ signal: ticket.signal }),
        ]).then(([status, backups]) => {
          next.disasterRecoveryStatus = status;
          next.disasterRecoveryBackups = backups;
        }));
      }
      if (permissions.has("platform.status.read")
        && typeof api.statusIncidents === "function") {
        tasks.push(api.statusIncidents({ signal: ticket.signal }).then((value) => {
          next.statusIncidents = value;
        }));
      }
      if (permissions.has("platform.capacity.read") && typeof api.capacity === "function") {
        tasks.push(api.capacity({ signal: ticket.signal }).then((value) => {
          next.capacity = value;
        }));
      }
      if (permissions.has("platform.unit_economics.read")
        && typeof api.unitEconomics === "function") {
        tasks.push(api.unitEconomics({ signal: ticket.signal }).then((value) => {
          next.unitEconomics = value;
        }));
      }
      if ((permissions.has("platform.risk.read")
        || permissions.has("platform.risk.support_read"))
        && typeof api.riskCases === "function") {
        tasks.push(api.riskCases({
          status: state.riskStatus,
          cursor: state.riskCursor,
          signal: ticket.signal,
        }).then((value) => { next.riskCases = value; }));
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
      if (!state.navigation.some((item) => item.id === "feature-rollouts")) {
        state.featureCommandIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "observability")) {
        state.observabilityIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "disaster-recovery")) {
        state.disasterRecoveryIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "status-incidents")) {
        state.statusIncidentIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "capacity")) {
        state.capacityIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "abuse-risk")) {
        state.riskDecisionIntent = null;
      }
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

  async function loadJobRuntime() {
    if (typeof api.jobQueues !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.jobQueues = await api.jobQueues({
        status: state.jobStatus,
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

  async function loadFeatureRollouts() {
    if (typeof api.featureFlags !== "function"
      || !currentPermissions().has("platform.feature.read")) return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.featureFlags = await api.featureFlags({
        status: state.featureStatus,
        riskLevel: state.featureRiskLevel,
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

  async function loadConfigDistribution() {
    if (typeof api.configDistribution !== "function"
      || !currentPermissions().has("platform.config_distribution.read")) return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.configDistribution = await api.configDistribution({ signal: ticket.signal });
      if (!current(ticket)) return false;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadObservability() {
    const permissions = currentPermissions();
    if ((!permissions.has("platform.observability.read")
      && !permissions.has("platform.observability.security_read"))
      || typeof api.observabilityServices !== "function"
      || typeof api.serviceLevelObjectives !== "function"
      || typeof api.errorBudgets !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const [services, slos, budgets] = await Promise.all([
        api.observabilityServices({ signal: ticket.signal }),
        api.serviceLevelObjectives({ signal: ticket.signal }),
        api.errorBudgets({ signal: ticket.signal }),
      ]);
      if (!current(ticket)) return false;
      state.observabilityServices = services;
      state.serviceLevelObjectives = slos;
      state.errorBudgets = budgets;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadDisasterRecovery() {
    if (!currentPermissions().has("platform.dr.read")
      || typeof api.disasterRecoveryStatus !== "function"
      || typeof api.disasterRecoveryBackups !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const [status, backups] = await Promise.all([
        api.disasterRecoveryStatus({ signal: ticket.signal }),
        api.disasterRecoveryBackups({ signal: ticket.signal }),
      ]);
      if (!current(ticket)) return false;
      state.disasterRecoveryStatus = status;
      state.disasterRecoveryBackups = backups;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadStatusIncidents() {
    if (!currentPermissions().has("platform.status.read")
      || typeof api.statusIncidents !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.statusIncidents = await api.statusIncidents({ signal: ticket.signal });
      if (!current(ticket)) return false;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadCapacity() {
    const permissions = currentPermissions();
    const canReadCapacity = permissions.has("platform.capacity.read")
      && typeof api.capacity === "function";
    const canReadEconomics = permissions.has("platform.unit_economics.read")
      && typeof api.unitEconomics === "function";
    if (!canReadCapacity && !canReadEconomics) return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const [capacity, economics] = await Promise.all([
        canReadCapacity ? api.capacity({ signal: ticket.signal }) : null,
        canReadEconomics ? api.unitEconomics({ signal: ticket.signal }) : null,
      ]);
      if (!current(ticket)) return false;
      state.capacity = capacity;
      state.unitEconomics = economics;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadRiskCases() {
    const permissions = currentPermissions();
    if ((!permissions.has("platform.risk.read")
      && !permissions.has("platform.risk.support_read"))
      || typeof api.riskCases !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.riskCases = await api.riskCases({
        status: state.riskStatus,
        cursor: state.riskCursor,
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

  async function featureWrite(intent, operation, message) {
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = state.featureCommandIntent?.fingerprint === fingerprint
      ? state.featureCommandIntent.idempotencyKey
      : newIdempotencyKey();
    state.featureCommandIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!current(ticket)) return false;
      state.featureFlags = await api.featureFlags({
        status: state.featureStatus,
        riskLevel: state.featureRiskLevel,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.featureCommandIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.featureCommandIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function observabilityWrite(intent, operation, message) {
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = state.observabilityIntent?.fingerprint === fingerprint
      ? state.observabilityIntent.idempotencyKey
      : newIdempotencyKey();
    state.observabilityIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!current(ticket)) return false;
      const [services, slos, budgets] = await Promise.all([
        api.observabilityServices({ signal: ticket.signal }),
        api.serviceLevelObjectives({ signal: ticket.signal }),
        api.errorBudgets({ signal: ticket.signal }),
      ]);
      if (!current(ticket)) return false;
      state.observabilityServices = services;
      state.serviceLevelObjectives = slos;
      state.errorBudgets = budgets;
      state.observabilityIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.observabilityIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function disasterRecoveryWrite(intent, operation, message) {
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = state.disasterRecoveryIntent?.fingerprint === fingerprint
      ? state.disasterRecoveryIntent.idempotencyKey
      : newIdempotencyKey();
    state.disasterRecoveryIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!current(ticket)) return false;
      const [status, backups] = await Promise.all([
        api.disasterRecoveryStatus({ signal: ticket.signal }),
        api.disasterRecoveryBackups({ signal: ticket.signal }),
      ]);
      if (!current(ticket)) return false;
      state.disasterRecoveryStatus = status;
      state.disasterRecoveryBackups = backups;
      state.disasterRecoveryIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.disasterRecoveryIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function statusIncidentWrite(intent, operation, message) {
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = state.statusIncidentIntent?.fingerprint === fingerprint
      ? state.statusIncidentIntent.idempotencyKey
      : newIdempotencyKey();
    state.statusIncidentIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!current(ticket)) return false;
      state.statusIncidents = await api.statusIncidents({ signal: ticket.signal });
      if (!current(ticket)) return false;
      state.statusIncidentIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.statusIncidentIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function capacityWrite(intent, operation, message) {
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = state.capacityIntent?.fingerprint === fingerprint
      ? state.capacityIntent.idempotencyKey
      : newIdempotencyKey();
    state.capacityIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!current(ticket)) return false;
      const permissions = currentPermissions();
      const [capacity, economics] = await Promise.all([
        permissions.has("platform.capacity.read") && typeof api.capacity === "function"
          ? api.capacity({ signal: ticket.signal }) : null,
        permissions.has("platform.unit_economics.read") && typeof api.unitEconomics === "function"
          ? api.unitEconomics({ signal: ticket.signal }) : null,
      ]);
      if (!current(ticket)) return false;
      state.capacity = capacity;
      state.unitEconomics = economics;
      state.capacityIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.capacityIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function riskWrite(intent, operation) {
    const fingerprint = JSON.stringify(intent);
    const idempotencyKey = state.riskDecisionIntent?.fingerprint === fingerprint
      ? state.riskDecisionIntent.idempotencyKey
      : newIdempotencyKey();
    state.riskDecisionIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!current(ticket)) return false;
      state.riskCases = await api.riskCases({
        status: state.riskStatus,
        cursor: state.riskCursor,
        signal: ticket.signal,
      });
      if (!current(ticket)) return false;
      state.riskDecisionIntent = null;
      state.loading = false;
      state.feedback = "Risk case decision recorded.";
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.riskDecisionIntent = null;
      }
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
      if (form.matches("[data-platform-notification-template-form]")) {
        const eventType = String(data.get("eventType") ?? "");
        controller.createNotificationTemplate({
          template_code: data.get("templateCode"),
          event_type: eventType,
          channel: data.get("channel"),
          locale: data.get("locale"),
          parameter_allowlist: [...(NOTIFICATION_EVENT_PARAMETERS[eventType] ?? [])],
          subject_template: data.get("subjectTemplate"),
          body_template: data.get("bodyTemplate"),
          status: data.get("status"),
        });
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
      if (form.matches("[data-platform-job-filter-form]")) {
        controller.filterJobs(data.get("status"));
      }
      if (form.matches("[data-platform-job-command-form]")) {
        controller.commandJob({
          jobId: data.get("jobId"),
          revision: data.get("revision"),
          action: data.get("action"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-job-type-form]")) {
        controller.commandJobType({
          typeCode: data.get("typeCode"),
          revision: data.get("revision"),
          action: data.get("action"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-feature-filter-form]")) {
        controller.filterFeatureFlags(data.get("status"), data.get("riskLevel"));
      }
      if (form.matches("[data-platform-feature-create-form]")) {
        controller.createFeatureFlag({
          revision: optionalNumber(data.get("revision")),
          flag: {
            action: data.get("action"),
            flag_id: optionalText(data.get("flagId")),
            flag_code: String(data.get("flagCode") ?? "").trim(),
            display_name: String(data.get("displayName") ?? "").trim(),
            risk_level: data.get("riskLevel"),
            rules: {
              environments: data.getAll("environments"),
              regions: commaList(data.get("regions")),
              cohort_code: optionalText(data.get("cohortCode")),
              percentage_bps: Number(data.get("percentageBps")),
              prerequisite_flag_code: optionalText(data.get("prerequisiteFlagCode")),
            },
            confirmed: data.get("confirmed") === "on",
          },
        });
      }
      if (form.matches("[data-platform-feature-command-form]")) {
        controller.commandFeatureRollout({
          flagId: data.get("flagId"),
          rolloutId: optionalText(data.get("rolloutId")),
          action: data.get("action"),
          versionNumber: optionalNumber(data.get("versionNumber")),
          percentageBps: optionalNumber(data.get("percentageBps")),
          environmentCode: optionalText(data.get("environmentCode")),
          regionCode: optionalText(data.get("regionCode")),
          revision: Number(data.get("revision")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-feature-kill-form]")) {
        controller.killFeatureFlag({
          flagId: data.get("flagId"),
          revision: Number(data.get("revision")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-config-rebuild-form]")) {
        controller.rebuildWorkspaceConfig({
          workspaceId: data.get("workspaceId"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-slo-form]")) {
        const sliKind = data.get("sliKind");
        const previousVersion = Number(data.get("previousVersion"));
        controller.createServiceLevelObjective({
          slo: {
            service_code: String(data.get("serviceCode") ?? "").trim(),
            slo_code: String(data.get("sloCode") ?? "").trim(),
            display_name: String(data.get("displayName") ?? "").trim(),
            signal_class: data.get("signalClass"),
            sli_kind: sliKind,
            operation_code: String(data.get("operationCode") ?? "").trim(),
            region_code: String(data.get("regionCode") ?? "").trim(),
            window_minutes: Number(data.get("windowMinutes")),
            target_bps: Number(data.get("targetBps")),
            latency_threshold_ms: sliKind === "latency"
              ? Number(data.get("latencyThresholdMs")) : null,
            burn_warning_bps: Number(data.get("burnWarningBps")),
            burn_critical_bps: Number(data.get("burnCriticalBps")),
            previous_version: previousVersion,
            reason_code: previousVersion === 0 ? "slo_create" : "slo_revision",
            confirmed: data.get("confirmed") === "on",
          },
        });
      }
      if (form.matches("[data-platform-alert-ack-form]")) {
        controller.acknowledgePlatformAlert({
          alertId: data.get("alertId"),
          revision: Number(data.get("revision")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-status-create-form]")) {
        controller.createStatusIncident({
          ownerWorkspaceId: data.get("ownerWorkspaceId"),
          title: String(data.get("title") ?? "").trim(),
          classification: data.get("classification"),
          visibility: data.get("visibility"),
          severity: data.get("severity"),
          acknowledgedAt: localTimestamp(data.get("acknowledgedAt")),
          initialUpdate: String(data.get("initialUpdate") ?? "").trim(),
          affectedComponents: commaList(data.get("affectedComponents")),
          affectedRegions: commaList(data.get("affectedRegions")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-status-incident-form]")) {
        controller.mutateStatusIncident({
          action: data.get("action"),
          incidentId: data.get("incidentId"),
          revision: Number(data.get("revision")),
          status: data.get("status"),
          publicMessage: String(data.get("publicMessage") ?? "").trim(),
          affectedComponents: commaList(data.get("affectedComponents")),
          affectedRegions: commaList(data.get("affectedRegions")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-status-postmortem-form]")) {
        controller.publishStatusPostmortem({
          incidentId: data.get("incidentId"),
          revision: Number(data.get("revision")),
          summary: String(data.get("summary") ?? "").trim(),
          timeline: lineList(data.get("timeline")),
          causeCategory: data.get("causeCategory"),
          correctiveActions: lineList(data.get("correctiveActions")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-maintenance-form]")) {
        controller.createStatusMaintenance({
          ownerWorkspaceId: data.get("ownerWorkspaceId"),
          title: String(data.get("title") ?? "").trim(),
          publicMessage: String(data.get("publicMessage") ?? "").trim(),
          visibility: data.get("visibility"),
          affectedComponents: commaList(data.get("affectedComponents")),
          affectedRegions: commaList(data.get("affectedRegions")),
          startsAt: localTimestamp(data.get("startsAt")),
          endsAt: localTimestamp(data.get("endsAt")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-capacity-forecast-form]")) {
        const [serviceCode, regionCode, revision] = String(data.get("capacityTarget") ?? "")
          .split("|");
        controller.createCapacityForecast({
          serviceCode,
          regionCode,
          horizonDays: Number(data.get("horizonDays")),
          expectedRevision: Number(revision),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-capacity-load-form]")) {
        controller.createCapacityLoadTest({
          scenarioCode: data.get("scenarioCode"),
          environmentClass: data.get("environmentClass"),
          regionCode: data.get("regionCode"),
          syntheticTenantCount: Number(data.get("syntheticTenantCount")),
          targetRps: Number(data.get("targetRps")),
          durationSeconds: Number(data.get("durationSeconds")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-capacity-chaos-form]")) {
        controller.commandCapacityChaos({
          action: data.get("action"),
          experimentId: data.get("experimentId"),
          revision: Number(data.get("revision")),
          faultCode: data.get("faultCode"),
          serviceCode: data.get("serviceCode"),
          environmentClass: data.get("environmentClass"),
          regionCode: data.get("regionCode"),
          blastTenantCount: Number(data.get("blastTenantCount")),
          durationSeconds: Number(data.get("durationSeconds")),
          objectiveP95Ms: Number(data.get("objectiveP95Ms")),
          objectiveErrorRateBps: Number(data.get("objectiveErrorRateBps")),
          stopErrorRateBps: Number(data.get("stopErrorRateBps")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-dr-command-form]")) {
        controller.voteDisasterRecovery({
          runKind: data.get("runKind"),
          voteKind: data.get("voteKind"),
          regionId: data.get("regionId"),
          failoverPlanId: data.get("failoverPlanId"),
          planVersion: Number(data.get("planVersion")),
          reasonCode: data.get("reasonCode"),
          expectedEpoch: Number(data.get("expectedEpoch")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-risk-filter-form]")) {
        controller.filterRiskCases(data.get("status"));
      }
      if (form.matches("[data-platform-risk-decision-form]")) {
        controller.decideRiskCase({
          caseId: data.get("caseId"),
          revision: Number(data.get("revision")),
          decision: data.get("decision"),
          reasonCode: data.get("reasonCode"),
          restrictionKind: data.get("restrictionKind"),
          restrictionMinutes: data.get("restrictionMinutes"),
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
      if (action === "dr-refresh") controller.refreshDisasterRecovery();
      if (action === "status-refresh") controller.refreshStatusIncidents();
      if (action === "capacity-refresh") controller.refreshCapacity();
      if (action === "risk-next") controller.nextRiskPage();
      if (action === "risk-previous") controller.previousRiskPage();
      if (action === "open-job") controller.openJob(target.dataset.jobId);
      if (action === "close-job") controller.closeJob();
      if (action === "prepare-workspace-lifecycle") controller.prepareWorkspaceLifecycle(target.dataset.lifecycleAction);
      if (action === "remove-workspace-risk") controller.removeWorkspaceRiskTag(target.dataset.tagCode, target.dataset.reasonCode);
    });
    root.addEventListener("change", (event) => {
      const select = event.target;
      if (!select?.matches?.('[data-platform-notification-template-form] select[name="eventType"]')) {
        return;
      }
      const output = select.form?.querySelector?.("[data-template-parameters]");
      if (output) {
        output.textContent = (NOTIFICATION_EVENT_PARAMETERS[select.value] ?? []).join(", ");
      }
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
  function currentPermissions() {
    return new Set(state.session?.context?.permissions ?? []);
  }
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
    state.notificationTemplates = null;
    state.notificationTemplateIntent = null;
    state.infrastructure = null;
    state.gatewayCells = null;
    state.gatewayPlacement = null;
    state.gatewayPlacementIntent = null;
    state.jobQueues = null;
    state.jobStatus = "";
    state.selectedJob = null;
    state.jobCommandIntent = null;
    state.jobTypeIntent = null;
    state.featureFlags = null;
    state.featureStatus = "";
    state.featureRiskLevel = "";
    state.featureCommandIntent = null;
    state.configDistribution = null;
    state.configRebuildIntent = null;
    state.observabilityServices = null;
    state.serviceLevelObjectives = null;
    state.errorBudgets = null;
    state.observabilityIntent = null;
    state.disasterRecoveryStatus = null;
    state.disasterRecoveryBackups = null;
    state.disasterRecoveryIntent = null;
    state.statusIncidents = null;
    state.statusIncidentIntent = null;
    state.capacity = null;
    state.unitEconomics = null;
    state.capacityIntent = null;
    state.riskCases = null;
    state.riskStatus = "";
    state.riskCursor = null;
    state.riskCursorHistory = [];
    state.riskDecisionIntent = null;
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

function normalizeJobStatus(value) {
  const text = String(value ?? "");
  return new Set([
    "", "queued", "leased", "running", "cancel_requested", "succeeded", "failed",
    "cancelled", "dead_lettered",
  ]).has(text) ? text : "";
}

function normalizeFeatureStatus(value) {
  const normalized = String(value ?? "");
  return new Set(["", "active", "killed", "disabled"]).has(normalized) ? normalized : "";
}

function normalizeFeatureRisk(value) {
  const normalized = String(value ?? "");
  return new Set(["", "standard", "p0"]).has(normalized) ? normalized : "";
}

function normalizeRiskStatus(value) {
  const normalized = String(value ?? "");
  return new Set(["", "open", "under_review", "restricted", "cleared", "closed"])
    .has(normalized) ? normalized : "";
}

function optionalText(value) {
  const normalized = String(value ?? "").trim();
  return normalized || null;
}

function optionalNumber(value) {
  const normalized = String(value ?? "").trim();
  return normalized ? Number(normalized) : null;
}

function commaList(value) {
  return [...new Set(String(value ?? "").split(",").map((item) => item.trim()).filter(Boolean))];
}

function lineList(value) {
  return [...new Set(String(value ?? "").split(/\r?\n/u).map((item) => item.trim()).filter(Boolean))];
}

function localTimestamp(value) {
  const parsed = new Date(String(value ?? ""));
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : "";
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
