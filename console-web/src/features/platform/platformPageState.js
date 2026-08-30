// 平台页面状态机负责取消过期请求、权限裁剪和幂等意图；敏感认证值只由 HttpOnly Cookie 承载。
import { PlatformApiError } from "../../lib/platform.js";
import { NOTIFICATION_EVENT_PARAMETERS } from "../../lib/notifications.js";
import { renderPlatformGate, renderPlatformWorkspace } from "./renderPlatformPage.js";

const PLATFORM_NAVIGATION_IDS = new Set([
  "infrastructure",
  "gateway",
  "edge-fleet",
  "edge-upgrades",
  "edge-licenses",
  "edge-backups",
  "offline-bundles",
  "job-runtime",
  "feature-rollouts",
  "config-distribution",
  "observability",
  "disaster-recovery",
  "status-incidents",
  "capacity",
  "data-residency",
  "security-assurance",
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
  "provider-catalog",
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
    providerCatalog: null,
    providerCatalogPublishIntent: null,
    developerContracts: null,
    developerContractIntent: null,
    securityAssurance: null,
    securityAssuranceLookup: "",
    securityAssuranceIntent: null,
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
    edgeFleet: null,
    edgeFleetFilters: { status: "", region: "", health: "" },
    edgeFleetCursor: null,
    edgeFleetCursorHistory: [],
    edgeDiagnosticIntent: null,
    edgeDiagnosticGrant: null,
    edgeDiagnosticCommand: null,
    gatewayReleases: null,
    fleetRollouts: null,
    fleetCompatibility: null,
    edgeUpgradeIntent: null,
    edgeLicenses: null,
    edgeLicenseIntent: null,
    edgeBackups: null,
    offlineBundles: null,
    offlineBundleIntent: null,
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
    residencyEvidence: null,
    residencyEvidenceFilters: { workspaceId: "", component: "", outcome: "" },
    residencyEvidenceCursor: null,
    residencyEvidenceCursorHistory: [],
    riskCases: null,
    riskStatus: "",
    riskCursor: null,
    riskCursorHistory: [],
    riskDecisionIntent: null,
    backup: null,
    backupIntent: null,
    grants: null,
    audit: null,
    privacyQueue: null,
    complianceControls: null,
    complianceWorkspaceId: "",
    diagnostics: null,
    supportTickets: null,
    supportTicketFilters: { status: "", severity: "", workspaceId: "" },
    selectedSupportTicketId: null,
    supportTicketIntent: null,
    workspaces: null,
    workspaceFilters: {},
    workspaceCursor: null,
    workspaceCursorHistory: [],
    selectedWorkspaceId: null,
    workspaceDetail: null,
    workspaceHealth: null,
    workspaceEvents: null,
    view: initialPlatformView(),
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
        || state.capacityIntent || state.providerCatalogPublishIntent
        || state.developerContractIntent
        || state.securityAssuranceIntent
        || state.edgeUpgradeIntent || state.edgeLicenseIntent
        || state.offlineBundleIntent || state.supportTicketIntent)) return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        state.session = await api.session({ signal: ticket.signal });
        if (!current(ticket)) return false;
        const activeView = state.view;
        clearProtectedFacts();
        state.view = activeView;
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
        || state.capacityIntent || state.providerCatalogPublishIntent
        || state.developerContractIntent
        || state.securityAssuranceIntent
        || state.edgeUpgradeIntent || state.edgeLicenseIntent
        || state.offlineBundleIntent || state.supportTicketIntent)) return false;
      const allowed = new Set((state.navigation ?? []).map((item) => item.id));
      const nextView = allowed.has(view) ? view : "overview";
      if (new Set([
        "job-runtime", "feature-rollouts", "config-distribution", "observability",
        "disaster-recovery", "status-incidents", "capacity", "data-residency", "abuse-risk",
        "security-assurance", "provider-catalog", "releases", "edge-fleet", "edge-upgrades",
        "edge-licenses", "edge-backups",
        "offline-bundles", "support", "privacy",
        "compliance",
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
        } else if (state.view === "abuse-risk") {
          state.riskDecisionIntent = null;
        } else if (state.view === "provider-catalog") {
          state.providerCatalogPublishIntent = null;
        } else if (state.view === "releases") {
          state.developerContracts = null;
          state.developerContractIntent = null;
        } else if (state.view === "security-assurance") {
          state.securityAssurance = null;
          state.securityAssuranceLookup = "";
          state.securityAssuranceIntent = null;
        } else if (state.view === "edge-fleet") {
          state.edgeFleet = null;
          state.edgeFleetFilters = { status: "", region: "", health: "" };
          state.edgeFleetCursor = null;
          state.edgeFleetCursorHistory = [];
          state.edgeDiagnosticIntent = null;
          state.edgeDiagnosticGrant = null;
          state.edgeDiagnosticCommand = null;
        } else if (state.view === "edge-upgrades") {
          state.gatewayReleases = null;
          state.fleetRollouts = null;
          state.fleetCompatibility = null;
          state.edgeUpgradeIntent = null;
        } else if (state.view === "edge-licenses") {
          state.edgeLicenses = null;
          state.edgeLicenseIntent = null;
        } else if (state.view === "edge-backups") {
          state.edgeBackups = null;
        } else if (state.view === "offline-bundles") {
          state.offlineBundles = null;
          state.offlineBundleIntent = null;
        } else if (state.view === "support") {
          state.supportTickets = null;
          state.selectedSupportTicketId = null;
          state.supportTicketIntent = null;
        } else if (state.view === "privacy") {
          state.privacyQueue = null;
        } else if (state.view === "compliance") {
          state.complianceControls = null;
        }
        state.loading = false;
      }
      state.view = nextView;
      replacePlatformHash(nextView);
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
      if (state.view === "data-residency" && !state.residencyEvidence) {
        void loadResidencyEvidence();
      }
      if (state.view === "abuse-risk" && !state.riskCases) void loadRiskCases();
      if (state.view === "provider-catalog" && !state.providerCatalog) {
        void loadProviderCatalog();
      }
      if (state.view === "releases" && !state.developerContracts) {
        void loadDeveloperContracts();
      }
      if (state.view === "edge-fleet" && !state.edgeFleet) void loadEdgeFleet();
      if (state.view === "edge-upgrades"
        && !state.gatewayReleases && !state.fleetRollouts) void loadEdgeUpgrades();
      if (state.view === "edge-licenses" && !state.edgeLicenses) void loadEdgeLicenses();
      if (state.view === "edge-backups" && !state.edgeBackups) void loadEdgeBackups();
      if (state.view === "offline-bundles" && !state.offlineBundles) {
        void loadOfflineBundles();
      }
      if (state.view === "support" && !state.supportTickets) void loadSupportTickets();
      if (state.view === "privacy" && !state.privacyQueue) void loadPrivacyQueue();
      if (state.view === "compliance" && !state.complianceControls) {
        void loadComplianceControls();
      }
    },
    async refreshPrivacyQueue() {
      if (state.view !== "privacy") return false;
      return loadPrivacyQueue();
    },
    async refreshComplianceControls() {
      if (state.view !== "compliance") return false;
      return loadComplianceControls("合规控制事实已刷新。");
    },
    async filterComplianceControls(workspaceId = "") {
      const normalized = String(workspaceId ?? "").trim();
      if (normalized && !/^wsp_[0-9a-f]{32}$/u.test(normalized)) return false;
      state.complianceWorkspaceId = normalized;
      state.complianceControls = null;
      return loadComplianceControls();
    },
    async buildDeveloperContract(input = {}) {
      if (!currentPermissions().has("platform.developer_contract.manage")
        || input.confirmed !== true) return false;
      return developerContractWrite(
        "contract.build",
        input,
        (idempotencyKey, signal) => api.buildDeveloperContract({
          ...input, idempotencyKey, signal,
        }),
        "Contract build is ready for independent approval.",
      );
    },
    async approveDeveloperContract(input = {}) {
      if (!currentPermissions().has("platform.developer_contract.approve")
        || input.confirmed !== true) return false;
      return developerContractWrite(
        "contract.approve",
        input,
        (idempotencyKey, signal) => api.approveDeveloperContract({
          ...input, idempotencyKey, signal,
        }),
        "Contract and signed SDK releases are published.",
      );
    },
    async createDeveloperDeprecation(input = {}) {
      if (!currentPermissions().has("platform.developer_contract.manage")
        || input.confirmed !== true) return false;
      return developerContractWrite(
        "deprecation.create",
        input,
        (idempotencyKey, signal) => api.createDeveloperDeprecation({
          ...input, idempotencyKey, signal,
        }),
        "Deprecation notice is ready for independent announcement.",
      );
    },
    async mutateDeveloperDeprecation(input = {}) {
      const permission = new Set(["announce", "sunset"]).has(input.action)
        ? "platform.developer_contract.approve" : "platform.developer_contract.manage";
      if (!currentPermissions().has(permission) || input.confirmed !== true) return false;
      return developerContractWrite(
        `deprecation.${input.action}`,
        input,
        (idempotencyKey, signal) => api.mutateDeveloperDeprecation({
          ...input, idempotencyKey, signal,
        }),
        `Deprecation ${input.action} recorded.`,
      );
    },
    async withdrawDeveloperSdk(input = {}) {
      if (!currentPermissions().has("platform.developer_contract.manage")
        || input.confirmed !== true) return false;
      return developerContractWrite(
        "sdk.withdraw",
        input,
        (idempotencyKey, signal) => api.withdrawDeveloperSdk({
          ...input, idempotencyKey, signal,
        }),
        "SDK release withdrawn; immutable history is retained.",
      );
    },
    async applySupplyChainException(input = {}) {
      const permission = input.operation === "approve"
        ? "platform.supply_chain.approve" : "platform.supply_chain.exception";
      if (!currentPermissions().has(permission) || input.confirmed !== true
        || typeof api.supplyChainException !== "function") return false;
      return developerContractWrite(
        `supply_chain.exception.${input.operation}`,
        input,
        (idempotencyKey, signal) => api.supplyChainException({
          ...input, idempotencyKey, signal,
        }),
        input.operation === "approve"
          ? "Security exception approved." : "Security exception requested.",
      );
    },
    async lookupSecurityAssessment(assessmentId) {
      const normalized = String(assessmentId ?? "").trim();
      if (state.view !== "security-assurance"
        || !/^sas_[0-9a-f]{32}$/u.test(normalized)) return false;
      state.securityAssuranceLookup = normalized;
      return loadSecurityAssurance(normalized);
    },
    async refreshSecurityAssessment() {
      const assessmentId = state.securityAssurance?.security_assessment_id
        ?? state.securityAssuranceLookup;
      return assessmentId ? loadSecurityAssurance(assessmentId) : false;
    },
    async startSecurityAssessment(input = {}) {
      const profileCode = String(input.profileCode ?? "");
      const method = String(input.method ?? "totp");
      if (state.view !== "security-assurance" || input.confirmed !== true
        || !currentPermissions().has("platform.assurance.run")
        || typeof api.startSecurityAssessment !== "function"
        || typeof api.createIdentityStepUpGrant !== "function"
        || !new Set(["incremental_release", "quarterly_full"]).has(profileCode)) return false;
      const command = {
        workspaceId: String(input.workspaceId ?? "").trim(),
        profileCode,
        profileVersion: 1,
        codeTreeSha256: String(input.codeTreeSha256 ?? "").trim(),
        migrationSetSha256: String(input.migrationSetSha256 ?? "").trim(),
        contractSetSha256: String(input.contractSetSha256 ?? "").trim(),
        artifactSha256: String(input.artifactSha256 ?? "").trim(),
        confirmed: true,
      };
      const fingerprint = JSON.stringify(["assessment.start", command, method]);
      const idempotencyKey = state.securityAssuranceIntent?.fingerprint === fingerprint
        ? state.securityAssuranceIntent.idempotencyKey : newIdempotencyKey();
      state.securityAssuranceIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      let verificationCode = String(input.verificationCode ?? "");
      let stepUpGrant = "";
      try {
        stepUpGrant = await createAssuranceStepUp({
          method,
          verificationCode,
          actionCode: "platform.assurance.run",
          resourceType: "security_assurance_profile",
          resourceId: profileCode,
          ticket,
        });
        verificationCode = "";
        if (!stepUpGrant || !currentAssuranceTicket(ticket)) return false;
        const response = await api.startSecurityAssessment({
          ...command, stepUpGrant, idempotencyKey, signal: ticket.signal,
        });
        stepUpGrant = "";
        if (!currentAssuranceTicket(ticket)) return false;
        state.securityAssurance = response.assessment;
        state.securityAssuranceLookup = response.assessment.security_assessment_id;
        state.securityAssuranceIntent = null;
        state.loading = false;
        state.feedback = response.assessment.status === "passed"
          ? "Security assessment passed." : "Security assessment completed with findings.";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.securityAssuranceIntent = null;
        }
        return fail(ticket, error);
      } finally {
        verificationCode = "";
        stepUpGrant = "";
      }
    },
    async remediateSecurityFinding(input = {}) {
      const method = String(input.method ?? "totp");
      const operation = String(input.operation ?? "");
      const findingId = String(input.findingId ?? "");
      if (state.view !== "security-assurance" || input.confirmed !== true
        || !currentPermissions().has("platform.assurance.remediate")
        || typeof api.securityRemediation !== "function"
        || typeof api.createIdentityStepUpGrant !== "function"
        || !new Set(["propose", "verify"]).has(operation)) return false;
      const command = {
        findingId,
        operation,
        expectedRevision: Number(input.expectedRevision),
        changeSha256: operation === "propose" ? String(input.changeSha256 ?? "").trim() : undefined,
        remediationId: operation === "verify" ? String(input.remediationId ?? "") : undefined,
        verificationAssessmentId: operation === "verify"
          ? String(input.verificationAssessmentId ?? "").trim() : undefined,
        confirmed: true,
      };
      const fingerprint = JSON.stringify(["finding.remediate", command, method]);
      const idempotencyKey = state.securityAssuranceIntent?.fingerprint === fingerprint
        ? state.securityAssuranceIntent.idempotencyKey : newIdempotencyKey();
      state.securityAssuranceIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      let verificationCode = String(input.verificationCode ?? "");
      let stepUpGrant = "";
      try {
        stepUpGrant = await createAssuranceStepUp({
          method,
          verificationCode,
          actionCode: "platform.assurance.remediate",
          resourceType: "pen_test_finding",
          resourceId: findingId,
          ticket,
        });
        verificationCode = "";
        if (!stepUpGrant || !currentAssuranceTicket(ticket)) return false;
        await api.securityRemediation({
          ...command, stepUpGrant, idempotencyKey, signal: ticket.signal,
        });
        stepUpGrant = "";
        if (!currentAssuranceTicket(ticket)) return false;
        const refreshed = await api.securityAssessment({
          assessmentId: state.securityAssurance.security_assessment_id,
          signal: ticket.signal,
        });
        if (!currentAssuranceTicket(ticket)) return false;
        state.securityAssurance = refreshed.assessment;
        state.securityAssuranceIntent = null;
        state.loading = false;
        state.feedback = operation === "propose"
          ? "Remediation is ready for independent verification."
          : "Verification result recorded.";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.securityAssuranceIntent = null;
        }
        return fail(ticket, error);
      } finally {
        verificationCode = "";
        stepUpGrant = "";
      }
    },
    async filterSupportTickets(filters = {}) {
      if (state.view !== "support") return false;
      state.supportTicketFilters = {
        status: String(filters.status ?? ""),
        severity: String(filters.severity ?? ""),
        workspaceId: String(filters.workspaceId ?? "").trim(),
      };
      return loadSupportTickets();
    },
    selectSupportTicket(ticketId) {
      if (state.view !== "support"
        || !state.supportTickets?.items?.some((item) => item.ticket_id === ticketId)) return false;
      state.selectedSupportTicketId = ticketId;
      state.feedback = null;
      render();
      return true;
    },
    async mutateSupportTicket(input = {}) {
      if (state.view !== "support" || state.loading
        || !currentPermissions().has("platform.support.ticket.manage")
        || typeof api.patchSupportTicket !== "function") return false;
      const ticketId = String(input.ticketId ?? "");
      const expectedRevision = Number(input.expectedRevision);
      const changes = input.changes;
      const fingerprint = JSON.stringify({ ticketId, expectedRevision, changes });
      const idempotencyKey = state.supportTicketIntent?.fingerprint === fingerprint
        ? state.supportTicketIntent.idempotencyKey
        : (api.supportIdempotencyKey?.("patch") ?? newIdempotencyKey());
      state.supportTicketIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const result = await api.patchSupportTicket({
          ticketId, expectedRevision, changes, idempotencyKey, signal: ticket.signal,
        });
        if (!currentSupportTicket(ticket)) return false;
        state.supportTickets = upsertSupportTicket(state.supportTickets, result.ticket);
        state.selectedSupportTicketId = result.ticket.ticket_id;
        state.supportTicketIntent = null;
        state.loading = false;
        state.feedback = input.feedback ?? "工单已更新。";
        render();
        return true;
      } catch (error) {
        if (![0, 503].includes(Number(error?.status))) state.supportTicketIntent = null;
        return fail(ticket, error);
      }
    },
    async escalateSupportTicket(input = {}) {
      if (state.view !== "support" || state.loading || input.confirmed !== true
        || !currentPermissions().has("platform.support.ticket.manage")
        || typeof api.escalateSupportTicket !== "function") return false;
      const intent = {
        ticketId: String(input.ticketId ?? ""),
        expectedRevision: Number(input.expectedRevision),
        reasonCode: String(input.reasonCode ?? ""),
      };
      const fingerprint = JSON.stringify(intent);
      const idempotencyKey = state.supportTicketIntent?.fingerprint === fingerprint
        ? state.supportTicketIntent.idempotencyKey
        : (api.supportIdempotencyKey?.("escalation") ?? newIdempotencyKey());
      state.supportTicketIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const result = await api.escalateSupportTicket({
          ...intent, idempotencyKey, signal: ticket.signal,
        });
        if (!currentSupportTicket(ticket)) return false;
        state.supportTickets = appendSupportEscalation(
          state.supportTickets, intent.ticketId, result,
        );
        state.supportTicketIntent = null;
        state.loading = false;
        state.feedback = "工单已升级。";
        render();
        return true;
      } catch (error) {
        if (![0, 503].includes(Number(error?.status))) state.supportTicketIntent = null;
        return fail(ticket, error);
      }
    },
    async filterEdgeFleet(filters) {
      if (state.view !== "edge-fleet") return false;
      state.edgeFleetFilters = normalizeEdgeFleetFilters(filters);
      state.edgeFleetCursor = null;
      state.edgeFleetCursorHistory = [];
      return loadEdgeFleet();
    },
    async nextEdgeFleetPage() {
      const cursor = state.edgeFleet?.next_cursor;
      if (state.view !== "edge-fleet" || !cursor) return false;
      state.edgeFleetCursorHistory.push(state.edgeFleetCursor);
      state.edgeFleetCursor = cursor;
      return loadEdgeFleet();
    },
    async previousEdgeFleetPage() {
      if (state.view !== "edge-fleet" || !state.edgeFleetCursorHistory.length) return false;
      state.edgeFleetCursor = state.edgeFleetCursorHistory.pop() ?? null;
      return loadEdgeFleet();
    },
    async refreshEdgeUpgrades() {
      return loadEdgeUpgrades();
    },
    async refreshEdgeLicenses() {
      return loadEdgeLicenses();
    },
    async refreshEdgeBackups() {
      return loadEdgeBackups();
    },
    async refreshOfflineBundles() {
      return loadOfflineBundles();
    },
    async decideOfflineBundle(input) {
      if (state.view !== "offline-bundles" || state.loading
        || input?.confirmed !== true
        || typeof api.decideOfflineBundle !== "function"
        || !currentPermissions().has("platform.edge_offline_bundle.manage")) return false;
      const requestId = String(input.requestId ?? "");
      const action = String(input.action ?? "");
      const expectedRevision = Number(input.expectedRevision);
      const reasonCode = String(input.reasonCode ?? "");
      const fingerprint = JSON.stringify({ requestId, action, expectedRevision, reasonCode });
      const idempotencyKey = state.offlineBundleIntent?.fingerprint === fingerprint
        ? state.offlineBundleIntent.idempotencyKey : newIdempotencyKey();
      state.offlineBundleIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const decision = await api.decideOfflineBundle({
          requestId,
          action,
          expectedRevision,
          reasonCode,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!currentOfflineBundleTicket(ticket)) return false;
        state.offlineBundles = applyOfflineDecision(state.offlineBundles, decision);
        state.offlineBundleIntent = null;
        state.loading = false;
        state.feedback = action === "approve" ? "离线交换请求已批准" : "离线交换请求已拒绝";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.offlineBundleIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async issueEdgeLicense(input) {
      if (state.view !== "edge-licenses" || input?.confirmed !== true
        || typeof api.issueEdgeLicense !== "function"
        || !currentPermissions().has("platform.edge_license.issue")) return false;
      const requestInput = {
        deploymentId: String(input.deploymentId ?? ""),
        nodeLimit: Number(input.nodeLimit),
        concurrentRequestLimit: Number(input.concurrentRequestLimit),
        features: Array.isArray(input.features) ? input.features.map(String) : [],
        expiresAt: String(input.expiresAt ?? ""),
        graceDays: Number(input.graceDays),
        sourceOrderId: String(input.sourceOrderId ?? "") || null,
        expectedRevision: input.expectedRevision === null || input.expectedRevision === ""
          ? null : Number(input.expectedRevision),
        reasonCode: String(input.reasonCode ?? ""),
      };
      return edgeLicenseWrite(
        JSON.stringify(requestInput),
        requestInput.expectedRevision === null ? "License 已签发。" : "License 容量已更新。",
        (idempotencyKey, signal) => api.issueEdgeLicense({
          ...requestInput,
          idempotencyKey,
          signal,
        }),
        (response) => { state.edgeLicenses = upsertLicense(state.edgeLicenses, response.license); },
      );
    },
    async revokeEdgeLicense(input) {
      if (state.view !== "edge-licenses" || input?.confirmed !== true
        || typeof api.revokeEdgeLicense !== "function"
        || !currentPermissions().has("platform.edge_license.revoke")) return false;
      const requestInput = {
        licenseId: String(input.licenseId ?? ""),
        expectedRevision: Number(input.expectedRevision),
        reasonCode: String(input.reasonCode ?? ""),
      };
      return edgeLicenseWrite(
        JSON.stringify(requestInput),
        "License 已明确吊销，离线宽限不再适用。",
        (idempotencyKey, signal) => api.revokeEdgeLicense({
          ...requestInput,
          idempotencyKey,
          signal,
        }),
        (response) => { state.edgeLicenses = upsertLicense(state.edgeLicenses, response.license); },
      );
    },
    async submitGatewayRelease(input) {
      if (state.view !== "edge-upgrades" || input?.confirmed !== true
        || typeof api.mutateGatewayRelease !== "function"
        || !currentPermissions().has("platform.edge_release.manage")) return false;
      const requestInput = {
        action: "submit",
        installerReleaseId: String(input.installerReleaseId ?? ""),
        releaseChannel: String(input.releaseChannel ?? ""),
        schemaFrom: Number(input.schemaFrom),
        schemaTo: Number(input.schemaTo),
        downMigrationSupported: input.downMigrationSupported === true,
        compatibleFromVersions: Array.isArray(input.compatibleFromVersions)
          ? input.compatibleFromVersions.map(String) : [],
        reasonCode: String(input.reasonCode ?? ""),
      };
      return edgeUpgradeWrite(
        JSON.stringify(requestInput),
        "Release 已提交，等待另一位发布负责人审批。",
        (idempotencyKey, signal) => api.mutateGatewayRelease({
          ...requestInput,
          idempotencyKey,
          signal,
        }),
        (response) => { state.gatewayReleases = upsertRelease(state.gatewayReleases, response.release); },
      );
    },
    async decideGatewayRelease(input) {
      const action = String(input?.action ?? "");
      const permission = action === "approve"
        ? "platform.edge_release.approve" : "platform.edge_release.manage";
      const webAuthn = input?.method === "webauthn";
      if (state.view !== "edge-upgrades" || !["approve", "withdraw"].includes(action)
        || state.loading || input?.confirmed !== true
        || typeof api.mutateGatewayRelease !== "function"
        || typeof api.createIdentityStepUpGrant !== "function"
        || (webAuthn && (typeof api.beginIdentityStepUp !== "function"
          || typeof api.getWebAuthnAssertion !== "function"))
        || !currentPermissions().has(permission)) return false;
      const releaseId = String(input.releaseId ?? "");
      const expectedRevision = Number(input.expectedRevision);
      const reasonCode = String(input.reasonCode ?? "");
      const fingerprint = JSON.stringify({ action, releaseId, expectedRevision, reasonCode });
      const idempotencyKey = state.edgeUpgradeIntent?.fingerprint === fingerprint
        ? state.edgeUpgradeIntent.idempotencyKey : newIdempotencyKey();
      state.edgeUpgradeIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      let verificationCode = String(input.verificationCode ?? "");
      let stepUpGrant = "";
      try {
        stepUpGrant = await createGlobalStepUp({
          method: input.method,
          verificationCode,
          actionCode: `fleet.release.${action}`,
          resourceType: "edge_gateway_release",
          resourceId: releaseId,
          ticket,
        });
        verificationCode = "";
        if (!stepUpGrant || !currentEdgeUpgradeTicket(ticket)) return false;
        const response = await api.mutateGatewayRelease({
          action,
          releaseId,
          expectedRevision,
          reasonCode,
          stepUpGrant,
          idempotencyKey,
          signal: ticket.signal,
        });
        stepUpGrant = "";
        if (!currentEdgeUpgradeTicket(ticket)) return false;
        state.gatewayReleases = upsertRelease(state.gatewayReleases, response.release);
        state.edgeUpgradeIntent = null;
        state.loading = false;
        state.feedback = action === "approve" ? "Release 已审批并可用于 rollout。" : "Release 已撤回。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.edgeUpgradeIntent = null;
        }
        return fail(ticket, error);
      } finally {
        verificationCode = "";
        stepUpGrant = "";
      }
    },
    async createFleetRollout(input) {
      if (state.view !== "edge-upgrades" || input?.confirmed !== true
        || typeof api.createFleetRollout !== "function"
        || !currentPermissions().has("platform.edge_rollout.manage")) return false;
      const requestInput = {
        releaseId: String(input.releaseId ?? ""),
        deploymentIds: Array.isArray(input.deploymentIds) ? input.deploymentIds.map(String) : [],
        canaryBasisPoints: Number(input.canaryBasisPoints),
        observationSeconds: Number(input.observationSeconds),
        batchSize: Number(input.batchSize),
        failureThresholdBasisPoints: Number(input.failureThresholdBasisPoints),
        reasonCode: String(input.reasonCode ?? ""),
      };
      return edgeUpgradeWrite(
        JSON.stringify({ action: "create_rollout", ...requestInput }),
        "Fleet rollout 已创建，Canary 门禁开始执行。",
        (idempotencyKey, signal) => api.createFleetRollout({
          ...requestInput,
          idempotencyKey,
          signal,
        }),
        (response) => { state.fleetRollouts = upsertRollout(state.fleetRollouts, response.rollout); },
      );
    },
    async mutateFleetRollout(input) {
      const action = String(input?.action ?? "");
      if (state.view !== "edge-upgrades" || !["pause", "resume"].includes(action)
        || input?.confirmed !== true || typeof api.mutateFleetRollout !== "function"
        || !currentPermissions().has("platform.edge_rollout.manage")) return false;
      const requestInput = {
        rolloutId: String(input.rolloutId ?? ""),
        action,
        expectedRevision: Number(input.expectedRevision),
        reasonCode: String(input.reasonCode ?? ""),
      };
      return edgeUpgradeWrite(
        JSON.stringify(requestInput),
        action === "pause" ? "Rollout 已暂停，未派发批次不会继续。" : "Rollout 已恢复。",
        (idempotencyKey, signal) => api.mutateFleetRollout({
          ...requestInput,
          idempotencyKey,
          signal,
        }),
        (response) => { state.fleetRollouts = upsertRollout(state.fleetRollouts, response.rollout); },
      );
    },
    async checkFleetCompatibility(input) {
      if (state.view !== "edge-upgrades" || state.loading
        || typeof api.fleetCompatibility !== "function"
        || !currentPermissions().has("platform.edge_rollout.read")) return false;
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const response = await api.fleetCompatibility({
          releaseId: String(input?.releaseId ?? ""),
          deploymentId: String(input?.deploymentId ?? ""),
          signal: ticket.signal,
        });
        if (!currentEdgeUpgradeTicket(ticket)) return false;
        state.fleetCompatibility = response.item;
        state.loading = false;
        state.feedback = response.item.compatible
          ? "该 Deployment 通过兼容性预检。" : "兼容性预检未通过。";
        render();
        return true;
      } catch (error) {
        return fail(ticket, error);
      }
    },
    async requestEdgeDiagnostic(input) {
      if (state.view !== "edge-fleet" || input?.confirmed !== true
        || typeof api.requestEdgeDiagnosticGrant !== "function"
        || !currentPermissions().has("platform.edge_fleet.manage")) return false;
      const requestInput = {
        deploymentId: String(input.deploymentId ?? ""),
        invocation: input.invocation,
        reasonCode: String(input.reasonCode ?? ""),
        maxRuns: Number(input.maxRuns),
      };
      const fingerprint = JSON.stringify({ action: "request", ...requestInput });
      const idempotencyKey = state.edgeDiagnosticIntent?.fingerprint === fingerprint
        ? state.edgeDiagnosticIntent.idempotencyKey
        : newIdempotencyKey();
      state.edgeDiagnosticIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const response = await api.requestEdgeDiagnosticGrant({
          ...requestInput,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!currentEdgeFleetTicket(ticket)) return false;
        state.edgeDiagnosticIntent = null;
        state.edgeDiagnosticGrant = response.grant;
        state.edgeDiagnosticCommand = null;
        state.loading = false;
        state.feedback = "客户诊断授权请求已提交。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.edgeDiagnosticIntent = null;
        }
        return fail(ticket, error);
      }
    },
    async runEdgeDiagnostic(input) {
      const webAuthn = input?.method === "webauthn";
      if (state.view !== "edge-fleet" || input?.confirmed !== true
        || typeof api.runEdgeDiagnostic !== "function"
        || typeof api.createIdentityStepUpGrant !== "function"
        || (webAuthn && (typeof api.beginIdentityStepUp !== "function"
          || typeof api.getWebAuthnAssertion !== "function"))
        || !currentPermissions().has("platform.edge_fleet.manage")) return false;
      const runInput = {
        deploymentId: String(input.deploymentId ?? ""),
        grantId: String(input.grantId ?? ""),
        deadlineSeconds: Number(input.deadlineSeconds),
        reasonCode: String(input.reasonCode ?? ""),
      };
      const fingerprint = JSON.stringify({ action: "run", ...runInput });
      const idempotencyKey = state.edgeDiagnosticIntent?.fingerprint === fingerprint
        ? state.edgeDiagnosticIntent.idempotencyKey
        : newIdempotencyKey();
      state.edgeDiagnosticIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      let verificationCode = String(input.verificationCode ?? "");
      let webAuthnCredential = null;
      let stepUpGrant = "";
      try {
        let challengeId;
        if (webAuthn) {
          const challenge = await api.beginIdentityStepUp({ signal: ticket.signal });
          if (!currentEdgeFleetTicket(ticket)) return false;
          challengeId = challenge.challenge_id;
          webAuthnCredential = await api.getWebAuthnAssertion(challenge.public_key_options);
          if (!currentEdgeFleetTicket(ticket)) return false;
        }
        const stepUp = await api.createIdentityStepUpGrant({
          method: input.method,
          actionCode: "edge.diagnostic.run",
          resourceType: "edge_deployment",
          resourceId: runInput.deploymentId,
          code: verificationCode,
          challengeId,
          credential: webAuthnCredential,
          signal: ticket.signal,
        });
        verificationCode = "";
        webAuthnCredential = null;
        stepUpGrant = stepUp.grant;
        if (!currentEdgeFleetTicket(ticket)) return false;
        const response = await api.runEdgeDiagnostic({
          ...runInput,
          stepUpGrant,
          idempotencyKey,
          signal: ticket.signal,
        });
        stepUpGrant = "";
        if (!currentEdgeFleetTicket(ticket)) return false;
        state.edgeDiagnosticIntent = null;
        state.edgeDiagnosticCommand = response.command;
        state.loading = false;
        state.feedback = "诊断命令已授权并等待 Agent 执行。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.edgeDiagnosticIntent = null;
        }
        return fail(ticket, error);
      } finally {
        verificationCode = "";
        webAuthnCredential = null;
        stepUpGrant = "";
      }
    },
    async cancelEdgeDiagnostic(input) {
      if (state.view !== "edge-fleet" || input?.confirmed !== true
        || typeof api.cancelEdgeDiagnostic !== "function"
        || !currentPermissions().has("platform.edge_fleet.manage")) return false;
      const cancelInput = {
        deploymentId: String(input.deploymentId ?? ""),
        commandId: String(input.commandId ?? ""),
        reasonCode: String(input.reasonCode ?? ""),
      };
      const fingerprint = JSON.stringify({ action: "cancel", ...cancelInput });
      const idempotencyKey = state.edgeDiagnosticIntent?.fingerprint === fingerprint
        ? state.edgeDiagnosticIntent.idempotencyKey
        : newIdempotencyKey();
      state.edgeDiagnosticIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const response = await api.cancelEdgeDiagnostic({
          ...cancelInput,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!currentEdgeFleetTicket(ticket)) return false;
        state.edgeDiagnosticIntent = null;
        state.edgeDiagnosticCommand = response.command;
        state.loading = false;
        state.feedback = "诊断取消请求已提交。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.edgeDiagnosticIntent = null;
        }
        return fail(ticket, error);
      }
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
    async filterResidencyEvidence({ workspaceId, component, outcome } = {}) {
      state.residencyEvidenceFilters = {
        workspaceId: String(workspaceId ?? "").trim(),
        component: String(component ?? ""),
        outcome: String(outcome ?? ""),
      };
      state.residencyEvidenceCursor = null;
      state.residencyEvidenceCursorHistory = [];
      return loadResidencyEvidence();
    },
    async nextResidencyEvidencePage() {
      const next = state.residencyEvidence?.next_cursor;
      if (!next) return false;
      state.residencyEvidenceCursorHistory.push(state.residencyEvidenceCursor);
      state.residencyEvidenceCursor = next;
      return loadResidencyEvidence();
    },
    async previousResidencyEvidencePage() {
      if (!state.residencyEvidenceCursorHistory.length) return false;
      state.residencyEvidenceCursor = state.residencyEvidenceCursorHistory.pop() ?? null;
      return loadResidencyEvidence();
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
    async publishProviderCatalog({ manifestText, keyId, signature, reasonCode, confirmed }) {
      if (confirmed !== true
        || typeof api.publishProviderCatalog !== "function"
        || typeof api.providerCatalog !== "function"
        || !currentPermissions().has("platform.provider_catalog.manage")) return false;
      let manifest;
      try {
        const source = String(manifestText ?? "");
        if (source.length < 2 || source.length > 250_000) throw new Error("invalid");
        manifest = JSON.parse(source);
        if (!manifest || Object.getPrototypeOf(manifest) !== Object.prototype) {
          throw new Error("invalid");
        }
      } catch {
        state.error = safeError(new PlatformApiError({
          status: 422,
          code: "provider_catalog.manifest_invalid",
          message: "Canonical manifest 必须是有效且有界的 JSON 对象。",
        }));
        render();
        return false;
      }
      const expectedRevision = Number(state.providerCatalog?.current?.generation);
      if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) return false;
      const fingerprint = JSON.stringify([
        String(manifest.version ?? ""), expectedRevision, String(keyId ?? ""), reasonCode,
      ]);
      const idempotencyKey = state.providerCatalogPublishIntent?.fingerprint === fingerprint
        ? state.providerCatalogPublishIntent.idempotencyKey
        : newIdempotencyKey();
      state.providerCatalogPublishIntent = { fingerprint, idempotencyKey };
      const ticket = begin();
      state.loading = true;
      state.error = null;
      render();
      try {
        const result = await api.publishProviderCatalog({
          manifest,
          keyId,
          signature,
          reasonCode,
          confirmed: true,
          expectedRevision,
          idempotencyKey,
          signal: ticket.signal,
        });
        if (!current(ticket)) return false;
        state.providerCatalog = await api.providerCatalog({ signal: ticket.signal });
        if (!current(ticket)) return false;
        state.providerCatalogPublishIntent = null;
        state.loading = false;
        state.feedback = result.idempotent_replay
          ? "已恢复同一目录发布结果。"
          : result.propagated ? "目录版本已发布并进入传播。" : "目录版本已发布。";
        render();
        return true;
      } catch (error) {
        if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
          state.providerCatalogPublishIntent = null;
        }
        return fail(ticket, error);
      }
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
        providerCatalog: null,
        developerContracts: null,
        securityAssurance: null,
        billing: null,
        refunds: null,
        verification: null,
        notificationTemplates: null,
        infrastructure: null,
        gatewayCells: null,
        edgeFleet: null,
        gatewayReleases: null,
        fleetRollouts: null,
        fleetCompatibility: null,
        edgeLicenses: null,
        edgeBackups: null,
        offlineBundles: null,
        jobQueues: null,
        featureFlags: null,
        configDistribution: null,
        disasterRecoveryStatus: null,
        disasterRecoveryBackups: null,
        capacity: null,
        unitEconomics: null,
        residencyEvidence: null,
        grants: null,
        supportTickets: null,
        audit: null,
        privacyQueue: null,
        complianceControls: null,
        workspaces: null,
        workspaceDetail: null,
        workspaceHealth: null,
        workspaceEvents: null,
      };
      const tasks = [];
      const fleetOnly = new Set([
        "edge-fleet", "edge-upgrades", "edge-licenses", "edge-backups", "offline-bundles",
      ]).has(state.view);
      if (!fleetOnly && permissions.has("platform.operator.manage")) {
        tasks.push(api.operators({ signal: ticket.signal }).then((value) => { next.operators = value; }));
      }
      if (!fleetOnly && permissions.has("platform.catalog.read") && typeof api.plans === "function") {
        tasks.push(api.plans({ signal: ticket.signal }).then((value) => { next.plans = value; }));
      }
      if (!fleetOnly && (permissions.has("platform.provider_catalog.manage")
        || permissions.has("platform.provider_catalog.security_read"))
        && typeof api.providerCatalog === "function") {
        tasks.push(api.providerCatalog({ signal: ticket.signal }).then((value) => {
          next.providerCatalog = value;
        }));
      }
      if (state.view === "releases"
        && permissions.has("platform.developer_contract.read")
        && typeof api.developerContracts === "function") {
        tasks.push(api.developerContracts({ signal: ticket.signal }).then((value) => {
          next.developerContracts = value;
        }));
      }
      if (state.view === "security-assurance" && state.securityAssuranceLookup
        && permissions.has("platform.assurance.read")
        && typeof api.securityAssessment === "function") {
        tasks.push(api.securityAssessment({
          assessmentId: state.securityAssuranceLookup,
          signal: ticket.signal,
        }).then((value) => { next.securityAssurance = value.assessment; }));
      }
      if (!fleetOnly && permissions.has("platform.billing.read")
        && typeof api.billingSummary === "function") {
        tasks.push(api.billingSummary({
          status: state.billingStatus,
          cursor: state.billingCursor,
          signal: ticket.signal,
        }).then((value) => { next.billing = value; }));
      }
      if (!fleetOnly && permissions.has("platform.billing.read")
        && typeof api.refunds === "function") {
        tasks.push(api.refunds({
          status: state.refundStatus,
          signal: ticket.signal,
        }).then((value) => { next.refunds = value; }));
      }
      if (!fleetOnly && permissions.has("platform.verification.read")
        && typeof api.businessVerifications === "function") {
        tasks.push(api.businessVerifications({
          status: state.verificationStatus,
          offset: state.verificationOffset,
          signal: ticket.signal,
        }).then((value) => { next.verification = value; }));
      }
      if (!fleetOnly && permissions.has("platform.notification_template.read")
        && typeof api.notificationTemplates === "function") {
        tasks.push(api.notificationTemplates({ signal: ticket.signal }).then((value) => {
          next.notificationTemplates = value;
        }));
      }
      if (!fleetOnly && permissions.has("platform.infrastructure.read")
        && typeof api.infrastructureStatus === "function") {
        tasks.push(api.infrastructureStatus({ signal: ticket.signal }).then((value) => {
          next.infrastructure = value;
        }));
      }
      if (!fleetOnly && permissions.has("platform.gateway.read")
        && typeof api.gatewayCells === "function") {
        tasks.push(api.gatewayCells({ signal: ticket.signal }).then((value) => {
          next.gatewayCells = value;
        }));
      }
      if (state.view === "edge-fleet"
        && permissions.has("platform.edge_fleet.read")
        && typeof api.edgeFleet === "function") {
        tasks.push(api.edgeFleet({
          filters: state.edgeFleetFilters,
          cursor: state.edgeFleetCursor,
          signal: ticket.signal,
        }).then((value) => {
          next.edgeFleet = value;
        }));
      }
      if (state.view === "edge-upgrades"
        && permissions.has("platform.edge_release.read")
        && typeof api.gatewayReleases === "function") {
        tasks.push(api.gatewayReleases({ signal: ticket.signal }).then((value) => {
          next.gatewayReleases = value;
        }));
      }
      if (state.view === "edge-upgrades"
        && permissions.has("platform.edge_rollout.read")
        && typeof api.fleetRollouts === "function") {
        tasks.push(api.fleetRollouts({ signal: ticket.signal }).then((value) => {
          next.fleetRollouts = value;
        }));
      }
      if (state.view === "edge-licenses"
        && permissions.has("platform.edge_license.read")
        && typeof api.edgeLicenses === "function") {
        tasks.push(api.edgeLicenses({ signal: ticket.signal }).then((value) => {
          next.edgeLicenses = value;
        }));
      }
      if (state.view === "edge-backups"
        && permissions.has("platform.edge_backup.read")
        && typeof api.edgeBackups === "function") {
        tasks.push(api.edgeBackups({ signal: ticket.signal }).then((value) => {
          next.edgeBackups = value;
        }));
      }
      if (state.view === "offline-bundles"
        && permissions.has("platform.edge_offline_bundle.read")
        && typeof api.offlineBundles === "function") {
        tasks.push(api.offlineBundles({ signal: ticket.signal }).then((value) => {
          next.offlineBundles = value;
        }));
      }
      if (!fleetOnly && permissions.has("platform.job.read")
        && typeof api.jobQueues === "function") {
        tasks.push(api.jobQueues({
          status: state.jobStatus,
          signal: ticket.signal,
        }).then((value) => { next.jobQueues = value; }));
      }
      if (!fleetOnly && permissions.has("platform.feature.read")
        && typeof api.featureFlags === "function") {
        tasks.push(api.featureFlags({
          status: state.featureStatus,
          riskLevel: state.featureRiskLevel,
          signal: ticket.signal,
        }).then((value) => { next.featureFlags = value; }));
      }
      if (!fleetOnly && permissions.has("platform.config_distribution.read")
        && typeof api.configDistribution === "function") {
        tasks.push(api.configDistribution({ signal: ticket.signal }).then((value) => {
          next.configDistribution = value;
        }));
      }
      if (!fleetOnly && (permissions.has("platform.observability.read")
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
      if (!fleetOnly && permissions.has("platform.dr.read")
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
      if (!fleetOnly && new Set(["overview", "status-incidents"]).has(state.view)
        && permissions.has("platform.status.read")
        && typeof api.statusIncidents === "function") {
        tasks.push(api.statusIncidents({ signal: ticket.signal }).then((value) => {
          next.statusIncidents = value;
        }));
      }
      if (!fleetOnly && permissions.has("platform.capacity.read")
        && typeof api.capacity === "function") {
        tasks.push(api.capacity({ signal: ticket.signal }).then((value) => {
          next.capacity = value;
        }));
      }
      if (!fleetOnly && permissions.has("platform.unit_economics.read")
        && typeof api.unitEconomics === "function") {
        tasks.push(api.unitEconomics({ signal: ticket.signal }).then((value) => {
          next.unitEconomics = value;
        }));
      }
      if (!fleetOnly && permissions.has("platform.data_residency.read")
        && typeof api.residencyEvidence === "function") {
        tasks.push(api.residencyEvidence({
          ...state.residencyEvidenceFilters,
          cursor: state.residencyEvidenceCursor,
          limit: 50,
          signal: ticket.signal,
        }).then((value) => { next.residencyEvidence = value; }));
      }
      if (!fleetOnly && (permissions.has("platform.risk.read")
        || permissions.has("platform.risk.support_read"))
        && typeof api.riskCases === "function") {
        tasks.push(api.riskCases({
          status: state.riskStatus,
          cursor: state.riskCursor,
          signal: ticket.signal,
        }).then((value) => { next.riskCases = value; }));
      }
      if (!fleetOnly
        && (permissions.has("platform.support.request")
          || permissions.has("platform.support.approve"))) {
        tasks.push(api.grants({ signal: ticket.signal }).then((value) => { next.grants = value; }));
      }
      if (state.view === "support" && permissions.has("platform.support.ticket.read")
        && typeof api.supportTickets === "function") {
        tasks.push(api.supportTickets({
          status: state.supportTicketFilters.status,
          severity: state.supportTicketFilters.severity,
          workspaceId: state.supportTicketFilters.workspaceId,
          signal: ticket.signal,
        }).then((value) => { next.supportTickets = value; }));
      }
      if (!fleetOnly && permissions.has("platform.audit.read")) {
        tasks.push(api.audit({ signal: ticket.signal }).then((value) => { next.audit = value; }));
      }
      if (state.view === "privacy" && permissions.has("platform.privacy.read")
        && typeof api.privacyQueue === "function") {
        tasks.push(api.privacyQueue({ limit: 100, signal: ticket.signal }).then((value) => {
          next.privacyQueue = value;
        }));
      }
      if (state.view === "compliance" && permissions.has("platform.compliance.read")
        && typeof api.complianceControls === "function") {
        tasks.push(api.complianceControls({
          workspaceId: state.complianceWorkspaceId,
          signal: ticket.signal,
        }).then((value) => { next.complianceControls = value; }));
      }
      if (!fleetOnly && hasWorkspaceRead(permissions)) {
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
      if (state.supportTickets) {
        state.selectedSupportTicketId = state.supportTickets.items.some(
          (item) => item.ticket_id === state.selectedSupportTicketId,
        ) ? state.selectedSupportTicketId : state.supportTickets.items[0]?.ticket_id ?? null;
      }
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
      if (!state.navigation.some((item) => item.id === "data-residency")) {
        state.residencyEvidence = null;
        state.residencyEvidenceCursor = null;
        state.residencyEvidenceCursorHistory = [];
      }
      if (!state.navigation.some((item) => item.id === "abuse-risk")) {
        state.riskDecisionIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "provider-catalog")) {
        state.providerCatalog = null;
        state.providerCatalogPublishIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "releases")) {
        state.developerContracts = null;
        state.developerContractIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "security-assurance")) {
        state.securityAssurance = null;
        state.securityAssuranceLookup = "";
        state.securityAssuranceIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "edge-fleet")) {
        state.edgeFleet = null;
        state.edgeFleetFilters = { status: "", region: "", health: "" };
        state.edgeFleetCursor = null;
        state.edgeFleetCursorHistory = [];
        state.edgeDiagnosticIntent = null;
        state.edgeDiagnosticGrant = null;
        state.edgeDiagnosticCommand = null;
      }
      if (!state.navigation.some((item) => item.id === "edge-upgrades")) {
        state.gatewayReleases = null;
        state.fleetRollouts = null;
        state.fleetCompatibility = null;
        state.edgeUpgradeIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "edge-licenses")) {
        state.edgeLicenses = null;
        state.edgeLicenseIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "edge-backups")) {
        state.edgeBackups = null;
      }
      if (!state.navigation.some((item) => item.id === "offline-bundles")) {
        state.offlineBundles = null;
        state.offlineBundleIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "support")) {
        state.supportTickets = null;
        state.selectedSupportTicketId = null;
        state.supportTicketIntent = null;
      }
      if (!state.navigation.some((item) => item.id === "privacy")) state.privacyQueue = null;
      if (!state.navigation.some((item) => item.id === "compliance")) {
        state.complianceControls = null;
        state.complianceWorkspaceId = "";
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

  async function loadSupportTickets() {
    if (state.view !== "support" || typeof api.supportTickets !== "function"
      || !currentPermissions().has("platform.support.ticket.read")) return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const response = await api.supportTickets({
        status: state.supportTicketFilters.status,
        severity: state.supportTicketFilters.severity,
        workspaceId: state.supportTicketFilters.workspaceId,
        signal: ticket.signal,
      });
      if (!currentSupportTicket(ticket)) return false;
      state.supportTickets = response;
      state.selectedSupportTicketId = response.items.some(
        (item) => item.ticket_id === state.selectedSupportTicketId,
      ) ? state.selectedSupportTicketId : response.items[0]?.ticket_id ?? null;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadEdgeFleet() {
    if (typeof api.edgeFleet !== "function"
      || !currentPermissions().has("platform.edge_fleet.read")
      || state.view !== "edge-fleet") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const fleet = await api.edgeFleet({
        filters: state.edgeFleetFilters,
        cursor: state.edgeFleetCursor,
        signal: ticket.signal,
      });
      if (!currentEdgeFleetTicket(ticket)) return false;
      state.edgeFleet = fleet;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadEdgeUpgrades() {
    const permissions = currentPermissions();
    if (state.view !== "edge-upgrades"
      || !permissions.has("platform.edge_release.read")) return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const [releases, rollouts] = await Promise.all([
        api.gatewayReleases({ signal: ticket.signal }),
        permissions.has("platform.edge_rollout.read") && typeof api.fleetRollouts === "function"
          ? api.fleetRollouts({ signal: ticket.signal }) : Promise.resolve(null),
      ]);
      if (!currentEdgeUpgradeTicket(ticket)) return false;
      state.gatewayReleases = releases;
      state.fleetRollouts = rollouts;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function edgeUpgradeWrite(fingerprint, message, operation, accept) {
    if (state.view !== "edge-upgrades" || state.loading) return false;
    const idempotencyKey = state.edgeUpgradeIntent?.fingerprint === fingerprint
      ? state.edgeUpgradeIntent.idempotencyKey : newIdempotencyKey();
    state.edgeUpgradeIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const response = await operation(idempotencyKey, ticket.signal);
      if (!currentEdgeUpgradeTicket(ticket)) return false;
      accept(response);
      state.edgeUpgradeIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.edgeUpgradeIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function loadEdgeLicenses() {
    if (state.view !== "edge-licenses"
      || !currentPermissions().has("platform.edge_license.read")
      || typeof api.edgeLicenses !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const licenses = await api.edgeLicenses({ signal: ticket.signal });
      if (!currentEdgeLicenseTicket(ticket)) return false;
      state.edgeLicenses = licenses;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadEdgeBackups() {
    if (state.view !== "edge-backups"
      || !currentPermissions().has("platform.edge_backup.read")
      || typeof api.edgeBackups !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const statuses = await api.edgeBackups({ signal: ticket.signal });
      if (!currentEdgeBackupTicket(ticket)) return false;
      state.edgeBackups = statuses;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadOfflineBundles() {
    if (state.view !== "offline-bundles"
      || !currentPermissions().has("platform.edge_offline_bundle.read")
      || typeof api.offlineBundles !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const exchanges = await api.offlineBundles({ signal: ticket.signal });
      if (!currentOfflineBundleTicket(ticket)) return false;
      state.offlineBundles = exchanges;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function edgeLicenseWrite(fingerprint, message, operation, accept) {
    if (state.view !== "edge-licenses" || state.loading) return false;
    const idempotencyKey = state.edgeLicenseIntent?.fingerprint === fingerprint
      ? state.edgeLicenseIntent.idempotencyKey : newIdempotencyKey();
    state.edgeLicenseIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const response = await operation(idempotencyKey, ticket.signal);
      if (!currentEdgeLicenseTicket(ticket)) return false;
      accept(response);
      state.edgeLicenseIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.edgeLicenseIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function createGlobalStepUp({
    method, verificationCode, actionCode, resourceType, resourceId, ticket,
  }) {
    let challengeId;
    let credential = null;
    if (method === "webauthn") {
      const challenge = await api.beginIdentityStepUp({ signal: ticket.signal });
      if (!currentEdgeUpgradeTicket(ticket)) return "";
      challengeId = challenge.challenge_id;
      credential = await api.getWebAuthnAssertion(challenge.public_key_options);
      if (!currentEdgeUpgradeTicket(ticket)) return "";
    }
    const response = await api.createIdentityStepUpGrant({
      method,
      actionCode,
      resourceType,
      resourceId,
      code: verificationCode,
      challengeId,
      credential,
      signal: ticket.signal,
    });
    credential = null;
    return currentEdgeUpgradeTicket(ticket) ? String(response.grant ?? "") : "";
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

  async function loadResidencyEvidence() {
    if (!currentPermissions().has("platform.data_residency.read")
      || typeof api.residencyEvidence !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.residencyEvidence = await api.residencyEvidence({
        ...state.residencyEvidenceFilters,
        cursor: state.residencyEvidenceCursor,
        limit: 50,
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

  async function loadDeveloperContracts() {
    if (state.view !== "releases"
      || !currentPermissions().has("platform.developer_contract.read")
      || typeof api.developerContracts !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const workspace = await api.developerContracts({ signal: ticket.signal });
      if (!currentDeveloperContractTicket(ticket)) return false;
      state.developerContracts = workspace;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function developerContractWrite(kind, input, operation, message) {
    if (state.view !== "releases" || state.loading
      || typeof operation !== "function" || typeof api.developerContracts !== "function") {
      return false;
    }
    const fingerprint = JSON.stringify([kind, input]);
    const idempotencyKey = state.developerContractIntent?.fingerprint === fingerprint
      ? state.developerContractIntent.idempotencyKey : newIdempotencyKey();
    state.developerContractIntent = { fingerprint, idempotencyKey };
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      await operation(idempotencyKey, ticket.signal);
      if (!currentDeveloperContractTicket(ticket)) return false;
      const workspace = await api.developerContracts({ signal: ticket.signal });
      if (!currentDeveloperContractTicket(ticket)) return false;
      state.developerContracts = workspace;
      state.developerContractIntent = null;
      state.loading = false;
      state.feedback = message;
      render();
      return true;
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 403 && current(ticket)) {
        state.developerContractIntent = null;
        await controller.refresh();
        return false;
      }
      if (!(error instanceof PlatformApiError) || ![0, 503].includes(error.status)) {
        state.developerContractIntent = null;
      }
      return fail(ticket, error);
    }
  }

  async function loadSecurityAssurance(assessmentId) {
    if (state.view !== "security-assurance"
      || !currentPermissions().has("platform.assurance.read")
      || typeof api.securityAssessment !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const response = await api.securityAssessment({ assessmentId, signal: ticket.signal });
      if (!currentAssuranceTicket(ticket)) return false;
      state.securityAssurance = response.assessment;
      state.securityAssuranceLookup = response.assessment.security_assessment_id;
      state.loading = false;
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function createAssuranceStepUp({
    method, verificationCode, actionCode, resourceType, resourceId, ticket,
  }) {
    let challengeId;
    let credential = null;
    if (method === "webauthn") {
      if (typeof api.beginIdentityStepUp !== "function"
        || typeof api.getWebAuthnAssertion !== "function") return "";
      const challenge = await api.beginIdentityStepUp({ signal: ticket.signal });
      if (!currentAssuranceTicket(ticket)) return "";
      challengeId = challenge.challenge_id;
      credential = await api.getWebAuthnAssertion(challenge.public_key_options);
      if (!currentAssuranceTicket(ticket)) return "";
    }
    const response = await api.createIdentityStepUpGrant({
      method,
      actionCode,
      resourceType,
      resourceId,
      code: verificationCode,
      challengeId,
      credential,
      signal: ticket.signal,
    });
    credential = null;
    return currentAssuranceTicket(ticket) ? String(response.grant ?? "") : "";
  }

  async function loadProviderCatalog() {
    const permissions = currentPermissions();
    if ((!permissions.has("platform.provider_catalog.manage")
      && !permissions.has("platform.provider_catalog.security_read"))
      || typeof api.providerCatalog !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      state.providerCatalog = await api.providerCatalog({ signal: ticket.signal });
      if (!current(ticket)) return false;
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

  async function loadPrivacyQueue() {
    if (state.view !== "privacy" || !currentPermissions().has("platform.privacy.read")
      || typeof api.privacyQueue !== "function") return false;
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const result = await api.privacyQueue({ limit: 100, signal: ticket.signal });
      if (!current(ticket) || state.view !== "privacy") return false;
      state.privacyQueue = result;
      state.loading = false;
      state.feedback = "隐私请求队列已刷新。";
      render();
      return true;
    } catch (error) {
      return fail(ticket, error);
    }
  }

  async function loadComplianceControls(feedback = "") {
    if (state.view !== "compliance"
      || !currentPermissions().has("platform.compliance.read")
      || typeof api.complianceControls !== "function") {
      state.complianceControls = null;
      return false;
    }
    const ticket = begin();
    state.loading = true;
    state.error = null;
    render();
    try {
      const result = await api.complianceControls({
        workspaceId: state.complianceWorkspaceId,
        signal: ticket.signal,
      });
      if (!current(ticket) || state.view !== "compliance"
        || !currentPermissions().has("platform.compliance.read")) return false;
      state.complianceControls = result;
      state.loading = false;
      state.feedback = feedback || null;
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
      if (form.matches("[data-platform-provider-catalog-form]")) {
        controller.publishProviderCatalog({
          manifestText: data.get("manifest"),
          keyId: data.get("keyId"),
          signature: data.get("signature"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-developer-contract-build-form]")) {
        controller.buildDeveloperContract({
          apiVersion: data.get("apiVersion"),
          pythonSdkVersion: data.get("pythonSdkVersion"),
          javascriptSdkVersion: data.get("javascriptSdkVersion"),
          producerRevision: Number(data.get("producerRevision")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-developer-contract-approve-form]")) {
        controller.approveDeveloperContract({
          apiVersionId: data.get("apiVersionId"),
          expectedRevision: Number(data.get("expectedRevision")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-developer-deprecation-create-form]")) {
        controller.createDeveloperDeprecation({
          apiVersionId: data.get("apiVersionId"),
          replacementApiVersionId: data.get("replacementApiVersionId"),
          affectedOperations: String(data.get("affectedOperations") ?? "")
            .split(/[,\n]/u).map((item) => item.trim()).filter(Boolean),
          migrationGuideCode: data.get("migrationGuideCode"),
          minimumNoticeDays: Number(data.get("minimumNoticeDays")),
          sunsetAt: data.get("sunsetAt"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-developer-deprecation-action-form]")) {
        const notification = data.get("notificationWatermark");
        const adoption = data.get("adoptionWatermark");
        controller.mutateDeveloperDeprecation({
          noticeId: data.get("noticeId"),
          action: data.get("action"),
          expectedRevision: Number(data.get("expectedRevision")),
          notificationWatermark: notification === null || notification === ""
            ? null : Number(notification),
          adoptionWatermark: adoption === null || adoption === "" ? null : Number(adoption),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-developer-sdk-withdraw-form]")) {
        controller.withdrawDeveloperSdk({
          sdkReleaseId: data.get("sdkReleaseId"),
          expectedRevision: Number(data.get("expectedRevision")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-supply-chain-exception-form]")) {
        const operation = data.get("operation");
        controller.applySupplyChainException({
          operation,
          findingId: data.get("findingId"),
          workspaceId: operation === "request" ? data.get("workspaceId") : undefined,
          reasonCode: operation === "request" ? data.get("reasonCode") : undefined,
          compensatingControls: operation === "request" ? data.getAll("controls") : undefined,
          startsAt: operation === "request" ? localTimestamp(data.get("startsAt")) : undefined,
          expiresAt: operation === "request" ? localTimestamp(data.get("expiresAt")) : undefined,
          securityExceptionId: operation === "approve"
            ? data.get("securityExceptionId") : undefined,
          expectedRevision: operation === "approve"
            ? Number(data.get("expectedRevision")) : undefined,
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-assurance-lookup-form]")) {
        controller.lookupSecurityAssessment(data.get("assessmentId"));
      }
      if (form.matches("[data-platform-assurance-start-form]")) {
        const verificationCode = String(data.get("verificationCode") ?? "");
        form.querySelectorAll("[data-platform-sensitive]").forEach((node) => { node.value = ""; });
        controller.startSecurityAssessment({
          workspaceId: data.get("workspaceId"),
          profileCode: data.get("profileCode"),
          codeTreeSha256: data.get("codeTreeSha256"),
          migrationSetSha256: data.get("migrationSetSha256"),
          contractSetSha256: data.get("contractSetSha256"),
          artifactSha256: data.get("artifactSha256"),
          method: data.get("method"),
          verificationCode,
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-assurance-remediation-form]")) {
        const operation = data.get("operation");
        const verificationCode = String(data.get("verificationCode") ?? "");
        form.querySelectorAll("[data-platform-sensitive]").forEach((node) => { node.value = ""; });
        controller.remediateSecurityFinding({
          operation,
          findingId: data.get("findingId"),
          expectedRevision: Number(data.get("expectedRevision")),
          changeSha256: operation === "propose" ? data.get("changeSha256") : undefined,
          remediationId: operation === "verify" ? data.get("remediationId") : undefined,
          verificationAssessmentId: operation === "verify"
            ? data.get("verificationAssessmentId") : undefined,
          method: data.get("method"),
          verificationCode,
          confirmed: data.get("confirmed") === "on",
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
      if (form.matches("[data-platform-edge-fleet-filter-form]")) {
        controller.filterEdgeFleet({
          status: data.get("status"),
          region: data.get("region"),
          health: data.get("health"),
        });
      }
      if (form.matches("[data-platform-edge-license-issue-form]")) {
        controller.issueEdgeLicense({
          deploymentId: data.get("deploymentId"),
          nodeLimit: Number(data.get("nodeLimit")),
          concurrentRequestLimit: Number(data.get("concurrentRequestLimit")),
          features: data.getAll("features"),
          expiresAt: localTimestamp(data.get("expiresAt")),
          graceDays: Number(data.get("graceDays")),
          sourceOrderId: data.get("sourceOrderId"),
          expectedRevision: data.get("expectedRevision"),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-edge-license-revoke-form]")) {
        controller.revokeEdgeLicense({
          licenseId: data.get("licenseId"),
          expectedRevision: Number(data.get("expectedRevision")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-offline-decision-form]")) {
        controller.decideOfflineBundle({
          requestId: data.get("requestId"),
          action: data.get("action"),
          expectedRevision: Number(data.get("expectedRevision")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-edge-release-submit-form]")) {
        controller.submitGatewayRelease({
          installerReleaseId: data.get("installerReleaseId"),
          releaseChannel: data.get("releaseChannel"),
          schemaFrom: Number(data.get("schemaFrom")),
          schemaTo: Number(data.get("schemaTo")),
          downMigrationSupported: data.get("downMigrationSupported") === "on",
          compatibleFromVersions: commaList(data.get("compatibleFromVersions")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-edge-release-decision-form]")) {
        let verificationCode = String(data.get("verificationCode") ?? "");
        form.querySelectorAll("[data-platform-sensitive]").forEach((node) => {
          node.value = "";
        });
        controller.decideGatewayRelease({
          action: data.get("action"),
          releaseId: data.get("releaseId"),
          expectedRevision: Number(data.get("expectedRevision")),
          reasonCode: data.get("reasonCode"),
          method: data.get("method"),
          verificationCode,
          confirmed: data.get("confirmed") === "on",
        });
        verificationCode = "";
      }
      if (form.matches("[data-platform-edge-rollout-create-form]")) {
        controller.createFleetRollout({
          releaseId: data.get("releaseId"),
          deploymentIds: commaList(data.get("deploymentIds")),
          canaryBasisPoints: Number(data.get("canaryBasisPoints")),
          observationSeconds: Number(data.get("observationSeconds")),
          batchSize: Number(data.get("batchSize")),
          failureThresholdBasisPoints: Number(data.get("failureThresholdBasisPoints")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-edge-rollout-command-form]")) {
        controller.mutateFleetRollout({
          rolloutId: data.get("rolloutId"),
          action: data.get("action"),
          expectedRevision: Number(data.get("expectedRevision")),
          reasonCode: data.get("reasonCode"),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-edge-compatibility-form]")) {
        controller.checkFleetCompatibility({
          releaseId: data.get("releaseId"),
          deploymentId: data.get("deploymentId"),
        });
      }
      if (form.matches("[data-platform-edge-diagnostic-request-form]")) {
        controller.requestEdgeDiagnostic({
          deploymentId: data.get("deploymentId"),
          invocation: edgeDiagnosticInvocation(data),
          reasonCode: data.get("reasonCode"),
          maxRuns: Number(data.get("maxRuns")),
          confirmed: data.get("confirmed") === "on",
        });
      }
      if (form.matches("[data-platform-edge-diagnostic-run-form]")) {
        let verificationCode = String(data.get("verificationCode") ?? "");
        form.querySelectorAll("[data-platform-sensitive]").forEach((node) => {
          node.value = "";
        });
        controller.runEdgeDiagnostic({
          deploymentId: data.get("deploymentId"),
          grantId: data.get("grantId"),
          deadlineSeconds: Number(data.get("deadlineSeconds")),
          reasonCode: data.get("reasonCode"),
          method: data.get("method"),
          verificationCode,
          confirmed: data.get("confirmed") === "on",
        });
        verificationCode = "";
      }
      if (form.matches("[data-platform-edge-diagnostic-cancel-form]")) {
        controller.cancelEdgeDiagnostic({
          deploymentId: data.get("deploymentId"),
          commandId: data.get("commandId"),
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
      if (form.matches("[data-platform-residency-filter-form]")) {
        controller.filterResidencyEvidence({
          workspaceId: data.get("workspaceId"),
          component: data.get("component"),
          outcome: data.get("outcome"),
        });
      }
      if (form.matches("[data-platform-compliance-filter-form]")) {
        controller.filterComplianceControls(data.get("workspaceId"));
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
      if (form.matches("[data-platform-support-ticket-filter-form]")) {
        controller.filterSupportTickets({
          status: data.get("status"),
          severity: data.get("severity"),
          workspaceId: data.get("workspaceId"),
        });
      }
      if (form.matches("[data-platform-support-message-form]")) {
        const message = String(data.get("message") ?? "").trim();
        form.querySelectorAll("textarea").forEach((node) => { node.value = ""; });
        controller.mutateSupportTicket({
          ticketId: data.get("ticketId"),
          expectedRevision: Number(data.get("revision")),
          changes: {
            message,
            message_visibility: data.get("visibility"),
          },
          feedback: data.get("visibility") === "internal" ? "内部备注已保存。" : "客户回复已发送。",
        });
      }
      if (form.matches("[data-platform-support-workflow-form]")) {
        const changes = { status: data.get("status") };
        const assignee = String(data.get("assigneeOperatorId") ?? "").trim();
        if (assignee) changes.assignee_operator_id = assignee;
        controller.mutateSupportTicket({
          ticketId: data.get("ticketId"),
          expectedRevision: Number(data.get("revision")),
          changes,
          feedback: "工单流程状态已更新。",
        });
      }
      if (form.matches("[data-platform-support-sla-form]")) {
        controller.mutateSupportTicket({
          ticketId: data.get("ticketId"),
          expectedRevision: Number(data.get("revision")),
          changes: { sla_action: data.get("slaAction"), reason_code: data.get("reasonCode") },
          feedback: "SLA 时钟已更新。",
        });
      }
      if (form.matches("[data-platform-support-escalation-form]")) {
        controller.escalateSupportTicket({
          ticketId: data.get("ticketId"),
          expectedRevision: Number(data.get("revision")),
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
      if (action === "privacy-refresh") controller.refreshPrivacyQueue();
      if (action === "compliance-refresh") controller.refreshComplianceControls();
      if (action === "assurance-refresh") controller.refreshSecurityAssessment();
      if (action === "approve-grant") controller.approveGrant(target.dataset.grantId);
      if (action === "open-support") controller.openSupport(target.dataset.grantId);
      if (action === "support-ticket-select") {
        controller.selectSupportTicket(target.dataset.ticketId);
      }
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
      if (action === "edge-fleet-next") controller.nextEdgeFleetPage();
      if (action === "edge-fleet-previous") controller.previousEdgeFleetPage();
      if (action === "edge-upgrades-refresh") controller.refreshEdgeUpgrades();
      if (action === "edge-licenses-refresh") controller.refreshEdgeLicenses();
      if (action === "edge-backups-refresh") controller.refreshEdgeBackups();
      if (action === "offline-bundles-refresh") controller.refreshOfflineBundles();
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
      if (action === "residency-evidence-next") controller.nextResidencyEvidencePage();
      if (action === "residency-evidence-previous") controller.previousResidencyEvidencePage();
      if (action === "risk-next") controller.nextRiskPage();
      if (action === "risk-previous") controller.previousRiskPage();
      if (action === "open-job") controller.openJob(target.dataset.jobId);
      if (action === "close-job") controller.closeJob();
      if (action === "prepare-workspace-lifecycle") controller.prepareWorkspaceLifecycle(target.dataset.lifecycleAction);
      if (action === "remove-workspace-risk") controller.removeWorkspaceRiskTag(target.dataset.tagCode, target.dataset.reasonCode);
    });
    root.addEventListener("change", (event) => {
      const select = event.target;
      if (select?.matches?.('[data-platform-notification-template-form] select[name="eventType"]')) {
        const output = select.form?.querySelector?.("[data-template-parameters]");
        if (output) {
          output.textContent = (NOTIFICATION_EVENT_PARAMETERS[select.value] ?? []).join(", ");
        }
      }
      if (select?.matches?.('[data-platform-edge-diagnostic-request-form] select[name="commandCode"]')) {
        updateEdgeDiagnosticParameterControls(select.form, select.value);
      }
      if (select?.matches?.('[data-platform-edge-diagnostic-run-form] select[name="method"]')) {
        const code = select.form?.querySelector?.("[data-platform-step-up-code]");
        const input = code?.querySelector?.("[data-platform-sensitive]");
        const webAuthn = select.value === "webauthn";
        if (input) {
          input.value = "";
          input.disabled = webAuthn;
          input.required = !webAuthn;
        }
        if (code) code.hidden = webAuthn;
      }
      if (select?.matches?.('[data-platform-edge-release-decision-form] select[name="method"]')) {
        const code = select.form?.querySelector?.("[data-platform-step-up-code]");
        const input = code?.querySelector?.("[data-platform-sensitive]");
        const webAuthn = select.value === "webauthn";
        if (input) {
          input.value = "";
          input.disabled = webAuthn;
          input.required = !webAuthn;
        }
        if (code) code.hidden = webAuthn;
      }
      if (select?.matches?.('[data-platform-assurance-start-form] select[name="method"], '
        + '[data-platform-assurance-remediation-form] select[name="method"]')) {
        const code = select.form?.querySelector?.("[data-platform-step-up-code]");
        const input = code?.querySelector?.("[data-platform-sensitive]");
        const webAuthn = select.value === "webauthn";
        if (input) {
          input.value = "";
          input.disabled = webAuthn;
          input.required = !webAuthn;
        }
        if (code) code.hidden = webAuthn;
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
  function currentSupportTicket(ticket) {
    return current(ticket) && state.view === "support"
      && currentPermissions().has("platform.support.ticket.read");
  }
  function currentEdgeFleetTicket(ticket) {
    return current(ticket) && state.view === "edge-fleet"
      && currentPermissions().has("platform.edge_fleet.read");
  }
  function currentEdgeUpgradeTicket(ticket) {
    return current(ticket) && state.view === "edge-upgrades"
      && currentPermissions().has("platform.edge_release.read");
  }
  function currentEdgeLicenseTicket(ticket) {
    return current(ticket) && state.view === "edge-licenses"
      && currentPermissions().has("platform.edge_license.read");
  }
  function currentEdgeBackupTicket(ticket) {
    return current(ticket) && state.view === "edge-backups"
      && currentPermissions().has("platform.edge_backup.read");
  }
  function currentOfflineBundleTicket(ticket) {
    return current(ticket) && state.view === "offline-bundles"
      && currentPermissions().has("platform.edge_offline_bundle.read");
  }
  function currentDeveloperContractTicket(ticket) {
    return current(ticket) && state.view === "releases"
      && currentPermissions().has("platform.developer_contract.read");
  }
  function currentAssuranceTicket(ticket) {
    return current(ticket) && state.view === "security-assurance"
      && currentPermissions().has("platform.assurance.read");
  }
  function render() { state.session ? renderPlatformWorkspace(root, state) : renderPlatformGate(root, state); }
  function currentPermissions() {
    return new Set(state.session?.context?.permissions ?? []);
  }
  function scrubSensitive() { root.querySelectorAll('input[type="password"], input[name="totpCode"], [data-platform-sensitive]').forEach((node) => { node.value = ""; }); }
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
    state.providerCatalog = null;
    state.providerCatalogPublishIntent = null;
    state.developerContracts = null;
    state.developerContractIntent = null;
    state.securityAssurance = null;
    state.securityAssuranceLookup = "";
    state.securityAssuranceIntent = null;
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
    state.edgeFleet = null;
    state.edgeFleetFilters = { status: "", region: "", health: "" };
    state.edgeFleetCursor = null;
    state.edgeFleetCursorHistory = [];
    state.edgeDiagnosticIntent = null;
    state.edgeDiagnosticGrant = null;
    state.edgeDiagnosticCommand = null;
    state.gatewayReleases = null;
    state.fleetRollouts = null;
    state.fleetCompatibility = null;
    state.edgeUpgradeIntent = null;
    state.edgeLicenses = null;
    state.edgeLicenseIntent = null;
    state.edgeBackups = null;
    state.offlineBundles = null;
    state.offlineBundleIntent = null;
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
    state.residencyEvidence = null;
    state.residencyEvidenceFilters = { workspaceId: "", component: "", outcome: "" };
    state.residencyEvidenceCursor = null;
    state.residencyEvidenceCursorHistory = [];
    state.riskCases = null;
    state.riskStatus = "";
    state.riskCursor = null;
    state.riskCursorHistory = [];
    state.riskDecisionIntent = null;
    state.backup = null;
    state.backupIntent = null;
    state.grants = null;
    state.supportTickets = null;
    state.supportTicketFilters = { status: "", severity: "", workspaceId: "" };
    state.selectedSupportTicketId = null;
    state.supportTicketIntent = null;
    state.audit = null;
    state.privacyQueue = null;
    state.complianceControls = null;
    state.complianceWorkspaceId = "";
    state.diagnostics = null;
    state.workspaces = null;
    state.workspaceFilters = {};
    state.workspaceCursor = null;
    state.workspaceCursorHistory = [];
    clearWorkspaceDetail();
    state.pendingAction = null;
  }
  function clearPlatformSession() {
    state.session = null;
    clearProtectedFacts();
  }
  return controller;
}

function upsertSupportTicket(response, ticket) {
  const items = [...(response?.items ?? [])];
  const index = items.findIndex((item) => item.ticket_id === ticket.ticket_id);
  if (index < 0) items.unshift(ticket); else items[index] = ticket;
  return Object.freeze({ ...(response ?? {}), items: Object.freeze(items) });
}

function appendSupportEscalation(response, ticketId, result) {
  const current = response?.items?.find((item) => item.ticket_id === ticketId);
  if (!current) return response;
  return upsertSupportTicket(response, Object.freeze({
    ...current,
    revision: result.ticket_revision,
    escalations: Object.freeze([
      ...current.escalations.filter((item) => item.escalation_id !== result.escalation.escalation_id),
      result.escalation,
    ]),
  }));
}

function upsertRelease(response, release) {
  const releases = response?.releases ?? [];
  return Object.freeze({
    ...(response ?? {}),
    releases: Object.freeze([
      release,
      ...releases.filter((item) => item.release_id !== release.release_id),
    ]),
  });
}

function upsertRollout(response, rollout) {
  const rollouts = response?.rollouts ?? [];
  return Object.freeze({
    ...(response ?? {}),
    rollouts: Object.freeze([
      rollout,
      ...rollouts.filter((item) => item.rollout_id !== rollout.rollout_id),
    ]),
  });
}

function upsertLicense(response, license) {
  const licenses = response?.licenses ?? [];
  return Object.freeze({
    ...(response ?? {}),
    licenses: Object.freeze([
      license,
      ...licenses.filter((item) => item.license_id !== license.license_id),
    ]),
  });
}

function applyOfflineDecision(response, decision) {
  const items = response?.items ?? [];
  let matched = false;
  const nextItems = items.map((item) => {
    const requests = item.state.requests.map((request) => {
      if (request.request_id !== decision.request.request_id) return request;
      matched = true;
      return decision.request;
    });
    return Object.freeze({
      ...item,
      state: Object.freeze({ ...item.state, requests: Object.freeze(requests) }),
    });
  });
  return matched
    ? Object.freeze({ ...response, items: Object.freeze(nextItems) })
    : response;
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

function normalizeEdgeFleetFilters(input) {
  const status = String(input?.status ?? "");
  const health = String(input?.health ?? "");
  const region = String(input?.region ?? "").trim().toLowerCase();
  return {
    status: new Set(["", "pending", "active", "revoked", "retired"]).has(status)
      ? status : "",
    region: /^[a-z0-9][a-z0-9-]{1,39}$/u.test(region) ? region : "",
    health: new Set([
      "", "healthy", "degraded", "critical", "offline", "revoked", "unknown",
    ]).has(health) ? health : "",
  };
}

function edgeDiagnosticInvocation(data) {
  const commandCode = String(data.get("commandCode") ?? "");
  if (commandCode === "runtime.watermarks") {
    return {
      command_code: commandCode,
      watermark_codes: data.getAll("watermarkCode").map(String),
    };
  }
  if (commandCode === "runtime.disk_budget") {
    return { command_code: commandCode, volume_code: String(data.get("volumeCode") ?? "") };
  }
  if (commandCode === "runtime.connectivity_summary") {
    return { command_code: commandCode, target_code: String(data.get("targetCode") ?? "") };
  }
  return { command_code: commandCode };
}

function updateEdgeDiagnosticParameterControls(form, commandCode) {
  for (const group of form?.querySelectorAll?.("[data-edge-command-parameter]") ?? []) {
    const active = group.dataset.edgeCommandParameter === commandCode;
    group.hidden = !active;
    group.querySelectorAll("input, select").forEach((control) => {
      control.disabled = !active;
    });
  }
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

function initialPlatformView() {
  const value = String(globalThis.window?.location?.hash ?? "").replace(/^#/u, "");
  return PLATFORM_NAVIGATION_IDS.has(value) ? value : "overview";
}

function replacePlatformHash(view) {
  const urlState = globalThis.window?.location;
  const navigationHistory = globalThis.window?.history;
  if (!urlState || typeof navigationHistory?.replaceState !== "function") return;
  const hash = `#${view}`;
  if (urlState.hash !== hash) navigationHistory.replaceState(null, "", hash);
}

function isPlatformSessionFailure(error) {
  return Number(error?.status) === 401
    && /^(?:platform\.session_(?:required|invalid|expired|revoked)|workspace_session\.(?:required|credential_conflict))$/u.test(String(error?.code ?? ""));
}

function safeError(error) {
  if (error instanceof PlatformApiError || (
    error && typeof error.code === "string" && Number.isFinite(Number(error.status))
  )) {
    return {
      message: error.message,
      requestId: error.requestId,
      code: error.code,
      status: error.status,
    };
  }
  return { message: "平台请求未完成，请稍后重试。", requestId: "-", code: "platform.request_failed", status: 0 };
}
