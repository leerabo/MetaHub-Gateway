const GLOBAL_CSRF_COOKIE = "__Host-metahub-global-csrf";
const WORKSPACE_CSRF_COOKIE = "__Host-metahub-workspace-csrf";
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const HEX_64 = /^[0-9a-f]{64}$/u;
const SNAPSHOT_ID = /^ncs_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;

const ROUTE_ROWS = [
  ["workspace.home.overview", "overview", "primary", "home"],
  ["workspace.home.assistant", "admin-assistant", "primary", "home"],
  ["workspace.home.approvals", "approvals", "primary", "home"],
  ["workspace.home.getting-started", "quickstart", "task_home", "home"],
  ["workspace.deployment.branding", "branding", "detail", "workspace"],
  ["workspace.settings.profile", "tenant", "primary", "workspace"],
  ["workspace.settings.members", "users", "primary", "workspace"],
  ["workspace.settings.roles", "role-permissions", "primary", "workspace"],
  ["workspace.settings.deployments", "environments", "primary", "workspace"],
  ["workspace.settings.data-management", "tenant-data-lifecycle", "primary", "workspace"],
  ["workspace.gateway.providers", "providers", "primary", "gateway"],
  ["workspace.gateway.private-endpoints", "local-models", "primary", "gateway"],
  ["workspace.gateway.private-deployments", "private-gateways", "primary", "gateway"],
  ["workspace.gateway.provider-health", "provider-health", "primary", "gateway"],
  ["workspace.gateway.models", "models", "primary", "gateway"],
  ["workspace.gateway.aliases", "model-aliases", "primary", "gateway"],
  ["workspace.gateway.routing-policies", "routing-policies", "detail", "gateway"],
  ["workspace.gateway.quality", "model-quality-scores", "primary", "gateway"],
  ["workspace.gateway.evaluations", "experiments", "primary", "gateway"],
  ["workspace.gateway.projects", "projects", "primary", "gateway"],
  ["workspace.gateway.virtual-keys", "virtual-keys", "primary", "gateway"],
  ["workspace.finance.subscription", "workspace-subscription", "primary", "finance"],
  ["workspace.finance.usage", "usage", "primary", "finance"],
  ["workspace.finance.internal-finops", "internal-finops", "primary", "finance"],
  ["workspace.finance.provider-costs", "model-prices", "detail", "finance"],
  ["workspace.finance.quota", "quota-buckets", "primary", "finance"],
  ["workspace.finance.disputes", "billing-disputes", "primary", "finance"],
  ["workspace.finance.budgets", "budgets", "primary", "finance"],
  ["workspace.finance.anomalies", "cost-anomalies", "primary", "finance"],
  ["workspace.governance.policies", "policies", "primary", "governance"],
  ["workspace.governance.policy-releases", "policy-snapshots", "primary", "governance"],
  ["workspace.governance.policy-templates", "policy-templates", "primary", "governance"],
  ["workspace.governance.dlp", "dlp-rules", "primary", "governance"],
  ["workspace.governance.session-risks", "session-risks", "primary", "governance"],
  ["workspace.governance.output-safety", "output-safety", "primary", "governance"],
  ["workspace.governance.webhooks", "webhooks", "primary", "governance"],
  ["workspace.governance.audit", "audit-logs", "primary", "governance"],
  ["workspace.applications.chat", "enterprise-chat", "primary", "applications"],
  ["workspace.applications.prompts", "prompt-templates", "primary", "applications"],
  ["workspace.applications.files", "files", "primary", "applications"],
  ["workspace.applications.file-security", "file-security", "detail", "applications"],
  ["workspace.applications.batch", "batch-jobs", "primary", "applications"],
  ["workspace.observability.requests", "request-logs", "primary", "observability"],
  ["workspace.observability.request-explanation", "request-explanations", "detail", "observability"],
  ["workspace.observability.feedback", "feedback-summary", "primary", "observability"],
  ["workspace.observability.migrations", "migration-operations", "primary", "observability"],
  ["workspace.observability.support", "support-tickets", "primary", "observability"],
  ["workspace.observability.status", "system-status", "primary", "observability"],
  ["workspace.developer.sdk", "sdk-examples", "developer_center", "developer-center"],
  ["workspace.developer.migration", "migration-guide", "developer_center", "developer-center"],
  ["workspace.developer.api", "api-docs", "developer_center", "developer-center"],
  ["workspace.developer.mcp", "mcp-tools", "developer_center", "developer-center"],
  ["identity.preferences", "language-settings", "account", null],
  ["workspace.advanced.provider-errors", "error-maps", "advanced", null],
  ["workspace.advanced.response-cache", "response-cache", "advanced", null],
  ["workspace.advanced.policy-simulator", "policy-simulator", "advanced", null],
  ["workspace.advanced.intent-security", "intent-security", "advanced", null],
  ["workspace.advanced.webhook-dead-letters", "webhook-dead-letters", "advanced", null],
  ["workspace.advanced.debug-console", "debug-console", "advanced", null],
  ["workspace.advanced.deployment-summary", "config-summary", "advanced", null],
];

export const WORKSPACE_ROUTE_REGISTRY = Object.freeze(Object.fromEntries(ROUTE_ROWS.map(
  ([routeCode, sourceId, placement, groupCode]) => [routeCode, Object.freeze({
    routeCode, sourceId, placement, groupCode,
  })],
)));
export const WORKSPACE_SOURCE_ROUTE_REGISTRY = Object.freeze(Object.fromEntries(ROUTE_ROWS.map(
  ([routeCode, sourceId]) => [sourceId, routeCode],
)));

export class WorkspaceNavigationError extends Error {
  constructor({ status = 0, code = "workspace_navigation.request_failed", message, requestId = "-", traceId = "-" } = {}) {
    super(safeText(message) || "空间导航请求未完成。");
    this.name = "WorkspaceNavigationError";
    this.status = Number(status) || 0;
    this.code = safeCode(code);
    this.requestId = safeChain(requestId);
    this.traceId = safeChain(traceId);
  }
}

export function getWorkspaceNavigation({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/workspace/navigation", {
    signal,
    normalize: normalizeWorkspaceNavigation,
    etag: (value) => [value.contract_generation, value.projection_generation, value.navigation_epoch, value.permission_revision],
  });
}

export function getWorkspaceGettingStarted({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/workspace/getting-started", {
    signal,
    normalize: normalizeWorkspaceGettingStarted,
    etag: (value) => [value.contract_generation, value.task_generation, value.navigation_epoch, value.permission_revision],
  });
}

export function getWorkspaceAdvancedTools({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/workspace/advanced-tools", {
    signal,
    normalize: normalizeWorkspaceAdvancedTools,
    etag: (value) => [value.contract_generation, value.entitlement_generation, value.navigation_epoch, value.permission_revision],
  });
}

export function updateWorkspaceNavigationPreference({
  controlApiUrl,
  workspaceId,
  contractSnapshotId,
  revision,
  changes,
  idempotencyKey = createWorkspaceNavigationIdempotencyKey("preference"),
  signal,
} = {}) {
  if (!WORKSPACE_ID.test(String(workspaceId)) || !SNAPSHOT_ID.test(String(contractSnapshotId))
    || !Number.isSafeInteger(revision) || revision < 0 || !plain(changes)) invalidRequest();
  const allowed = new Set(["last_primary_route_code", "expanded_group_code", "sidebar_collapsed"]);
  const body = { workspace_id: workspaceId, contract_snapshot_id: contractSnapshotId };
  for (const [key, value] of Object.entries(changes)) {
    if (!allowed.has(key)) invalidRequest();
    if (key === "sidebar_collapsed") {
      if (typeof value !== "boolean") invalidRequest();
    } else if (key === "last_primary_route_code" && value !== null
      && !WORKSPACE_ROUTE_REGISTRY[value]) {
      invalidRequest();
    } else if (key === "expanded_group_code" && value !== null
      && !new Set(["home", "workspace", "gateway", "finance", "governance", "applications", "observability", "developer-center"]).has(value)) {
      invalidRequest();
    }
    body[key] = value;
  }
  if (Object.keys(body).length === 2 || !IDEMPOTENCY_KEY.test(String(idempotencyKey))) invalidRequest();
  return request(controlApiUrl, "/identity/preferences/navigation", {
    method: "PATCH",
    body,
    signal,
    headers: {
      "If-Match": "\"" + revision + "\"",
      "Idempotency-Key": String(idempotencyKey),
      "X-CSRF-Token": readExactCookie(GLOBAL_CSRF_COOKIE),
    },
    normalize: normalizeWorkspaceNavigationPreference,
    etag: (value) => [value.revision],
  });
}

export function openWorkspaceAdvancedTool({
  controlApiUrl,
  toolCode,
  entitlementRevision,
  idempotencyKey = createWorkspaceNavigationIdempotencyKey("advanced-open"),
  signal,
} = {}) {
  if (!/^[a-z][a-z0-9_-]{2,79}$/u.test(String(toolCode))
    || !Number.isSafeInteger(entitlementRevision) || entitlementRevision < 1
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey))) invalidRequest();
  return request(controlApiUrl, "/workspace/advanced-tools/" + encodeURIComponent(toolCode) + "/open", {
    method: "POST",
    signal,
    headers: {
      "If-Match": "\"" + entitlementRevision + "\"",
      "Idempotency-Key": String(idempotencyKey),
      "X-CSRF-Token": readExactCookie(WORKSPACE_CSRF_COOKIE),
    },
    normalize: normalizeWorkspaceAdvancedToolOpen,
    etag: (value) => [value.entitlement_revision],
  });
}

export function createWorkspaceNavigationIdempotencyKey(namespace = "navigation") {
  if (!globalThis.crypto?.getRandomValues) invalidRequest("workspace_navigation.configuration_invalid");
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return namespace + ":" + Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export function normalizeWorkspaceNavigation(value) {
  exact(value, [
    "account_action_codes", "advanced_tools_available", "contract_generation",
    "contract_snapshot_id", "contract_version", "detail_route_codes",
    "developer_route_codes", "groups", "item_set_digest", "membership_revision",
    "navigation_epoch", "observed_at", "owner_ref", "permission_fingerprint",
    "permission_revision", "preference", "projection_generation", "projection_status",
    "request_id", "trace_id",
  ]);
  if (value.contract_version !== "workspace-navigation-v1"
    || typeof value.advanced_tools_available !== "boolean") invalidResponse();
  const groups = array(value.groups, 8).map(normalizeGroup);
  unique(groups.map((group) => group.group_code));
  unique(groups.flatMap((group) => group.items.map((item) => item.route_code)));
  return deepFreeze({
    ...authorizationEnvelope(value),
    owner_ref: digest(value.owner_ref),
    observed_at: timestamp(value.observed_at),
    contract_version: value.contract_version,
    projection_generation: positive(value.projection_generation),
    projection_status: choice(value.projection_status, ["ready", "partial", "stale"]),
    groups,
    detail_route_codes: routeArray(value.detail_route_codes, 20, "detail"),
    developer_route_codes: routeArray(value.developer_route_codes, 10, "developer_center"),
    account_action_codes: routeArray(value.account_action_codes, 10, "account"),
    advanced_tools_available: value.advanced_tools_available,
    preference: normalizePreferenceProjection(value.preference),
    item_set_digest: digest(value.item_set_digest),
  });
}

export function normalizeWorkspaceGettingStarted(value) {
  exact(value, [
    "completed_count", "contract_generation", "contract_snapshot_id", "evidence_digest",
    "membership_revision", "navigation_epoch", "needs_attention_count", "next_task_code",
    "observed_at", "owner_ref", "permission_fingerprint", "permission_revision",
    "request_id", "role_code", "source_freshness", "task_generation", "tasks", "trace_id",
  ]);
  const tasks = array(value.tasks, 30).map((task) => {
    exact(task, [
      "action_enabled", "action_route_code", "description_key", "priority",
      "source_freshness", "status", "task_code", "title_key",
    ]);
    if (typeof task.action_enabled !== "boolean") invalidResponse();
    const route = task.action_route_code === null ? null : routeCode(task.action_route_code);
    const actionableStatus = new Set(["ready", "needs_attention"]).has(task.status);
    if (task.action_enabled !== Boolean(route && actionableStatus)) invalidResponse();
    return deepFreeze({
      task_code: code(task.task_code),
      title_key: key(task.title_key),
      description_key: key(task.description_key),
      status: choice(task.status, [
        "not_started", "ready", "blocked", "in_progress", "completed",
        "needs_attention", "not_applicable",
      ]),
      priority: count(task.priority),
      action_route_code: route,
      action_enabled: task.action_enabled,
      source_freshness: freshness(task.source_freshness),
    });
  });
  unique(tasks.map((task) => task.task_code));
  const enabledTasks = tasks.filter((task) => task.action_enabled);
  const nextTaskCode = value.next_task_code === null ? null : code(value.next_task_code);
  if (nextTaskCode !== (enabledTasks[0]?.task_code ?? null)
    || value.completed_count !== tasks.filter((task) => task.status === "completed").length
    || value.needs_attention_count
      !== tasks.filter((task) => task.status === "needs_attention").length) invalidResponse();
  return deepFreeze({
    ...authorizationEnvelope(value),
    owner_ref: digest(value.owner_ref),
    observed_at: timestamp(value.observed_at),
    role_code: code(value.role_code),
    task_generation: positive(value.task_generation),
    tasks,
    next_task_code: nextTaskCode,
    completed_count: count(value.completed_count),
    needs_attention_count: count(value.needs_attention_count),
    source_freshness: freshness(value.source_freshness),
    evidence_digest: digest(value.evidence_digest),
  });
}

export function normalizeWorkspaceAdvancedTools(value) {
  exact(value, [
    "contract_generation", "contract_snapshot_id", "entitlement_generation",
    "membership_revision", "navigation_epoch", "observed_at", "owner_ref",
    "permission_fingerprint", "permission_revision", "request_id", "tools", "trace_id",
  ]);
  const tools = array(value.tools, 20).map((tool) => {
    exact(tool, [
      "action_enabled", "availability", "entitlement_revision", "label_key",
      "observed_at", "reason_code", "route_code", "tool_code",
    ]);
    const registry = WORKSPACE_ROUTE_REGISTRY[routeCode(tool.route_code)];
    if (!registry || registry.placement !== "advanced"
      || typeof tool.action_enabled !== "boolean"
      || tool.action_enabled !== (tool.availability === "available")) invalidResponse();
    return deepFreeze({
      tool_code: code(tool.tool_code),
      route_code: tool.route_code,
      label_key: key(tool.label_key),
      availability: choice(tool.availability, ["available", "blocked", "revoked", "not_applicable"]),
      reason_code: tool.reason_code === null ? null : code(tool.reason_code),
      action_enabled: tool.action_enabled,
      entitlement_revision: positive(tool.entitlement_revision),
      observed_at: timestamp(tool.observed_at),
    });
  });
  unique(tools.map((tool) => tool.tool_code));
  return deepFreeze({
    ...authorizationEnvelope(value),
    owner_ref: digest(value.owner_ref),
    observed_at: timestamp(value.observed_at),
    entitlement_generation: positive(value.entitlement_generation),
    tools,
  });
}

export function normalizeWorkspaceNavigationPreference(value) {
  exact(value, [
    "changed", "contract_generation", "contract_snapshot_id", "expanded_group_code",
    "idempotent_replay", "last_primary_route_code", "membership_revision",
    "navigation_epoch", "permission_fingerprint", "permission_revision", "preference_id",
    "request_id", "revision", "sidebar_collapsed", "trace_id", "updated_at", "workspace_ref",
  ]);
  if (typeof value.changed !== "boolean" || typeof value.idempotent_replay !== "boolean"
    || typeof value.sidebar_collapsed !== "boolean") invalidResponse();
  return deepFreeze({
    ...authorizationEnvelope(value),
    preference_id: opaqueId(value.preference_id),
    workspace_ref: digest(value.workspace_ref),
    last_primary_route_code: nullableRoute(value.last_primary_route_code),
    expanded_group_code: nullableGroup(value.expanded_group_code),
    sidebar_collapsed: value.sidebar_collapsed,
    revision: positive(value.revision),
    changed: value.changed,
    idempotent_replay: value.idempotent_replay,
    updated_at: timestamp(value.updated_at),
  });
}

export function normalizeWorkspaceAdvancedToolOpen(value) {
  exact(value, [
    "contract_generation", "contract_snapshot_id", "entitlement_revision",
    "idempotent_replay", "membership_revision", "navigation_epoch", "owner_ref",
    "permission_fingerprint", "permission_revision", "request_id", "route_code",
    "tool_code", "trace_id",
  ]);
  const registry = WORKSPACE_ROUTE_REGISTRY[routeCode(value.route_code)];
  if (!registry || registry.placement !== "advanced"
    || typeof value.idempotent_replay !== "boolean") invalidResponse();
  return deepFreeze({
    ...authorizationEnvelope(value),
    owner_ref: digest(value.owner_ref),
    tool_code: code(value.tool_code),
    route_code: value.route_code,
    entitlement_revision: positive(value.entitlement_revision),
    idempotent_replay: value.idempotent_replay,
  });
}

export function resolveWorkspaceNavigationRoute(
  navigation,
  requestedRouteCode,
  { requestedRejected = false, advancedTools = null } = {},
) {
  const primary = navigation.groups.flatMap(
    (group) => group.items.map((item) => item.route_code),
  );
  const allowed = new Set([
    ...primary,
    ...navigation.detail_route_codes,
    ...navigation.developer_route_codes,
    ...navigation.account_action_codes,
  ]);
  for (const tool of advancedTools?.tools ?? []) {
    if (tool.action_enabled && tool.availability === "available") allowed.add(tool.route_code);
  }
  const preferred = navigation.preference.last_primary_route_code;
  const fallback = (
    preferred && primary.includes(preferred) ? preferred : null
  ) ?? (
    primary.includes("workspace.home.getting-started")
      ? "workspace.home.getting-started"
      : null
  ) ?? (
    primary.includes("workspace.home.overview") ? "workspace.home.overview" : primary[0]
  );
  if (!fallback) invalidResponse();
  const requested = String(requestedRouteCode ?? "").trim();
  if (requestedRejected || (requested && !allowed.has(requested))) {
    return { routeCode: fallback, rejected: true };
  }
  return { routeCode: requested || fallback, rejected: false };
}

export function projectWorkspaceNavigationForConsole(
  navigation,
  activeRouteCode,
  advancedTools = null,
) {
  const items = [];
  for (const group of navigation.groups) {
    for (const item of group.items) items.push(projectItem(item.route_code, item, true));
  }
  for (const route of navigation.developer_route_codes) {
    items.push(projectItem(route, null, true));
  }
  for (const route of navigation.detail_route_codes) {
    items.push(projectItem(route, null, false));
  }
  for (const route of navigation.account_action_codes) {
    items.push(projectItem(route, null, false));
  }
  for (const tool of advancedTools?.tools ?? []) {
    if (tool.action_enabled && tool.availability === "available") {
      items.push(projectItem(tool.route_code, tool, false));
    }
  }
  unique(items.map((item) => item.id));
  const active = WORKSPACE_ROUTE_REGISTRY[activeRouteCode];
  if (!active || !items.some((item) => item.route_code === activeRouteCode)) invalidResponse();
  return deepFreeze({
    ...navigation,
    navigation_items: items,
    active_navigation_item_id: active.sourceId,
    active_route_code: activeRouteCode,
  });
}

function projectItem(route, source, sidebarVisible) {
  const entry = WORKSPACE_ROUTE_REGISTRY[route];
  if (!entry) invalidResponse();
  return deepFreeze({
    id: entry.sourceId,
    route_code: route,
    placement: entry.placement,
    group_code: entry.groupCode,
    group: entry.groupCode ?? "advanced",
    target_section: entry.sourceId,
    label: entry.sourceId,
    label_key: source?.label_key ?? "navigation." + entry.sourceId + ".label",
    description: "",
    description_key: source?.description_key ?? "navigation." + entry.sourceId + ".description",
    visible: true,
    disabled: false,
    sidebar_visible: sidebarVisible,
    order: source?.order ?? 900,
  });
}

async function request(controlApiUrl, path, options) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(options.headers ?? {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new WorkspaceNavigationError({
      code: "workspace_navigation.network_unavailable",
      message: "暂时无法连接空间导航服务。",
    });
  }
  const payload = await json(response);
  if (!response.ok) {
    throw new WorkspaceNavigationError({
      status: response.status,
      code: payload?.code,
      message: payload?.message,
      requestId: payload?.request_id,
      traceId: payload?.trace_id,
    });
  }
  const normalized = options.normalize(payload);
  requirePrivateResponse(response, normalized, options.etag(normalized));
  return normalized;
}

function normalizeGroup(value) {
  exact(value, ["expanded", "group_code", "items", "label_key"]);
  if (typeof value.expanded !== "boolean") invalidResponse();
  const groupCode = nullableGroup(value.group_code);
  if (groupCode === null) invalidResponse();
  const items = array(value.items, 30).map((item) => {
    exact(item, ["description_key", "kind", "label_key", "order", "route_code"]);
    const route = routeCode(item.route_code);
    const registry = WORKSPACE_ROUTE_REGISTRY[route];
    if (!registry || registry.groupCode !== groupCode
      || !new Set(["primary", "task_home"]).has(item.kind)
      || registry.placement !== item.kind) invalidResponse();
    return deepFreeze({
      route_code: route,
      label_key: key(item.label_key),
      description_key: key(item.description_key),
      kind: item.kind,
      order: count(item.order),
    });
  });
  return deepFreeze({
    group_code: groupCode,
    label_key: key(value.label_key),
    expanded: value.expanded,
    items,
  });
}

function normalizePreferenceProjection(value) {
  exact(value, [
    "expanded_group_code", "last_primary_route_code", "revision", "sidebar_collapsed",
  ]);
  if (typeof value.sidebar_collapsed !== "boolean") invalidResponse();
  return deepFreeze({
    revision: count(value.revision),
    last_primary_route_code: nullableRoute(value.last_primary_route_code),
    expanded_group_code: nullableGroup(value.expanded_group_code),
    sidebar_collapsed: value.sidebar_collapsed,
  });
}

function authorizationEnvelope(value) {
  return {
    contract_snapshot_id: snapshotId(value.contract_snapshot_id),
    contract_generation: positive(value.contract_generation),
    navigation_epoch: positive(value.navigation_epoch),
    membership_revision: positive(value.membership_revision),
    permission_revision: positive(value.permission_revision),
    permission_fingerprint: digest(value.permission_fingerprint),
    request_id: chain(value.request_id),
    trace_id: chain(value.trace_id),
  };
}

function routeArray(value, maximum, placement) {
  const result = array(value, maximum).map(routeCode);
  unique(result);
  if (result.some((route) => WORKSPACE_ROUTE_REGISTRY[route]?.placement !== placement)) {
    invalidResponse();
  }
  return result;
}

function nullableRoute(value) {
  return value === null ? null : routeCode(value);
}

function routeCode(value) {
  const text = bounded(value, 3, 120);
  if (!WORKSPACE_ROUTE_REGISTRY[text]) invalidResponse();
  return text;
}

function nullableGroup(value) {
  if (value === null) return null;
  return choice(value, [
    "home", "workspace", "gateway", "finance", "governance", "applications",
    "observability", "developer-center",
  ]);
}

function requirePrivateResponse(response, value, parts) {
  const cache = response.headers?.get?.("cache-control")?.toLowerCase() ?? "";
  const pragma = response.headers?.get?.("pragma")?.toLowerCase() ?? "";
  const vary = (response.headers?.get?.("vary")?.toLowerCase() ?? "")
    .split(",").map((part) => part.trim());
  const fingerprint = response.headers?.get?.("x-permission-fingerprint") ?? "";
  const expectedEtag = "\"" + parts.join(".") + "\"";
  if (!cache.includes("no-store") || !cache.includes("private")
    || pragma !== "no-cache" || !vary.includes("cookie")
    || fingerprint !== value.permission_fingerprint
    || response.headers?.get?.("etag") !== expectedEtag) invalidResponse();
}

function apiUrl(controlApiUrl, path) {
  try {
    const base = new URL(String(controlApiUrl));
    if (!new Set(["http:", "https:"]).has(base.protocol)
      || base.username || base.password) throw new Error();
    return new URL(path, base).toString();
  } catch {
    throw new WorkspaceNavigationError({
      code: "workspace_navigation.configuration_invalid",
      message: "空间导航服务地址无效。",
    });
  }
}

function readExactCookie(name, cookieText = globalThis.document?.cookie ?? "") {
  const values = String(cookieText).split(";").map((item) => item.trim())
    .filter((item) => item.startsWith(name + "="))
    .map((item) => item.slice(name.length + 1));
  if (values.length !== 1) invalidRequest("workspace_navigation.csrf_invalid");
  let value;
  try {
    value = decodeURIComponent(values[0]);
  } catch {
    invalidRequest("workspace_navigation.csrf_invalid");
  }
  if (!/^[A-Za-z0-9_-]{43,128}$/u.test(value)) {
    invalidRequest("workspace_navigation.csrf_invalid");
  }
  return value;
}

function freshness(value) {
  return choice(value, ["fresh", "stale", "unavailable"]);
}

function choice(value, values) {
  if (!values.includes(value)) invalidResponse();
  return value;
}

function snapshotId(value) {
  const text = bounded(value, 36, 36);
  if (!SNAPSHOT_ID.test(text)) invalidResponse();
  return text;
}

function opaqueId(value) {
  return bounded(value, 36, 160);
}

function digest(value) {
  const text = bounded(value, 64, 64);
  if (!HEX_64.test(text)) invalidResponse();
  return text;
}

function code(value) {
  const text = bounded(value, 3, 140);
  if (!/^[A-Za-z0-9_.-]+$/u.test(text)) invalidResponse();
  return text;
}

function key(value) {
  return code(value);
}

function timestamp(value) {
  const text = bounded(value, 20, 40);
  if (!Number.isFinite(Date.parse(text))) invalidResponse();
  return text;
}

function chain(value) {
  const text = bounded(value, 1, 160);
  if (!/^[A-Za-z0-9_.:-]+$/u.test(text)) invalidResponse();
  return text;
}

function count(value) {
  if (!Number.isSafeInteger(value) || value < 0) invalidResponse();
  return value;
}

function positive(value) {
  if (!Number.isSafeInteger(value) || value < 1) invalidResponse();
  return value;
}

function bounded(value, minimum, maximum) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f-\u009f]/u.test(value)) invalidResponse();
  return value;
}

function array(value, maximum) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse();
  return value;
}

function unique(values) {
  if (new Set(values).size !== values.length) invalidResponse();
}

function exact(value, keys) {
  if (!plain(value)
    || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) {
    invalidResponse();
  }
  if (Object.keys(value).some(
    (name) => /^(?:authorization|cookie|csrf|password|secret|token)$/iu.test(name),
  )) invalidResponse();
}

function plain(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

function safeText(value) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f-\u009f]/gu, " ").slice(0, 240)
    : "";
}

function safeCode(value) {
  const text = String(value ?? "");
  return /^(?:workspace_session|workspace_navigation|identity)\.[a-z0-9_.]{1,120}$/u.test(text)
    ? text
    : "workspace_navigation.request_failed";
}

function safeChain(value) {
  const text = String(value ?? "");
  return /^[A-Za-z0-9_.:-]{1,160}$/u.test(text) ? text : "-";
}

function invalidRequest(codeValue = "workspace_navigation.request_invalid") {
  throw new WorkspaceNavigationError({
    code: codeValue,
    message: "空间导航请求字段无效。",
  });
}

function invalidResponse() {
  throw new WorkspaceNavigationError({
    status: 502,
    code: "workspace_navigation.response_invalid",
    message: "空间导航响应格式无效。",
  });
}

async function json(response) {
  try {
    const value = await response.json();
    return plain(value) ? value : {};
  } catch {
    return {};
  }
}
