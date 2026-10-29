import { IdentityApiError } from "./identity.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const RESOURCE_ID = /^[a-z]{3}_[0-9a-f]{32}$/u;
const STEP_UP_GRANT = /^[A-Za-z0-9_-]{32,128}$/u;

export async function getWorkspaceConditionalAccess({
  controlApiUrl,
  workspaceId,
  signal,
} = {}) {
  return conditionalAccessRequest(controlApiUrl, workspaceId, "", {
    responseType: "policy",
    signal,
  });
}

export async function simulateWorkspaceConditionalAccess({
  controlApiUrl,
  workspaceId,
  signal,
} = {}) {
  return conditionalAccessRequest(controlApiUrl, workspaceId, "/simulations", {
    method: "POST",
    body: {},
    responseType: "simulation",
    signal,
  });
}

export async function updateWorkspaceConditionalAccess({
  controlApiUrl,
  workspaceId,
  policy,
  stepUpGrant,
  signal,
} = {}) {
  return conditionalAccessRequest(controlApiUrl, workspaceId, "", {
    method: "PATCH",
    body: policyInput(policy),
    stepUpGrant: clientStepUpGrant(stepUpGrant),
    responseType: "policy",
    signal,
  });
}

async function conditionalAccessRequest(controlApiUrl, workspaceId, suffix, options) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  }
  if (options.stepUpGrant) headers["X-Step-Up-Grant"] = options.stepUpGrant;
  let response;
  try {
    response = await fetch(conditionalAccessUrl(controlApiUrl, workspaceId, suffix), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new IdentityApiError({
      code: "conditional_access.unavailable",
      message: "条件访问服务暂时不可用，请稍后重试。",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw new IdentityApiError({
      status: Number(response.status) || 0,
      code: text(payload.code, 120) || "conditional_access.unavailable",
      message: text(payload.message, 240) || "条件访问请求未完成，请稍后重试。",
      requestId: text(payload.request_id, 160) || response.headers?.get?.("X-Request-ID"),
      traceId: text(payload.trace_id, 160) || response.headers?.get?.("X-Trace-ID"),
    });
  }
  return validateResponse(payload, options.responseType);
}

function conditionalAccessUrl(controlApiUrl, workspaceId, suffix) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidClient("control_api_url");
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidClient("control_api_url");
  }
  const id = clientResourceId(workspaceId, "wsp_", "workspace_id");
  return new URL(
    `/workspaces/${encodeURIComponent(id)}/conditional-access${suffix}`,
    base,
  ).toString();
}

function policyInput(policy) {
  if (!policy || typeof policy !== "object" || Array.isArray(policy)) {
    invalidClient("policy");
  }
  const expectedRevision = Number(policy.expectedRevision);
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
    invalidClient("expected_revision");
  }
  return {
    expected_revision: expectedRevision,
    mode: clientChoice(policy.mode, ["audit", "enforced"], "mode"),
    require_mfa: clientBoolean(policy.requireMfa, "require_mfa"),
    require_step_up_for_high_risk: clientBoolean(
      policy.requireStepUpForHighRisk,
      "require_step_up_for_high_risk",
    ),
    allow_trusted_devices: clientBoolean(
      policy.allowTrustedDevices,
      "allow_trusted_devices",
    ),
    region_rule: clientChoice(policy.regionRule, ["any", "domestic_only"], "region_rule"),
    network_rule: clientChoice(policy.networkRule, ["any", "trusted_only"], "network_rule"),
    allowed_cidrs: clientStringArray(policy.allowedCidrs, 32, 64, "allowed_cidrs"),
    exception_membership_ids: clientStringArray(
      policy.exceptionMembershipIds,
      32,
      36,
      "exception_membership_ids",
    ).map((value) => clientResourceId(value, "wmb_", "exception_membership_id")),
  };
}

function validateResponse(payload, responseType) {
  plainObject(payload, "response");
  if (responseType === "policy") {
    exactKeys(payload, ["latest_simulation", "policy", "request_id", "trace_id"], "response");
    return {
      policy: payload.policy === null ? null : validatePolicy(payload.policy),
      latest_simulation: payload.latest_simulation === null
        ? null
        : validateSimulation(payload.latest_simulation),
      ...requestTrace(payload),
    };
  }
  if (responseType === "simulation") {
    exactKeys(payload, ["policy", "request_id", "simulation", "trace_id"], "response");
    return {
      policy: validatePolicy(payload.policy),
      simulation: validateSimulation(payload.simulation),
      ...requestTrace(payload),
    };
  }
  invalidResponse("response_type");
}

function validatePolicy(value) {
  plainObject(value, "policy");
  exactKeys(
    value,
    [
      "allow_trusted_devices",
      "allowed_cidrs",
      "exception_membership_ids",
      "mode",
      "network_rule",
      "policy_id",
      "region_rule",
      "require_mfa",
      "require_step_up_for_high_risk",
      "revision",
      "simulation_watermark_at",
      "updated_at",
      "workspace_id",
    ],
    "policy",
  );
  return {
    policy_id: responseResourceId(value.policy_id, "cap_", "policy_id"),
    workspace_id: responseResourceId(value.workspace_id, "wsp_", "workspace_id"),
    mode: responseChoice(value.mode, ["audit", "enforced"], "policy.mode"),
    require_mfa: responseBoolean(value.require_mfa, "policy.require_mfa"),
    require_step_up_for_high_risk: responseBoolean(
      value.require_step_up_for_high_risk,
      "policy.require_step_up_for_high_risk",
    ),
    allow_trusted_devices: responseBoolean(
      value.allow_trusted_devices,
      "policy.allow_trusted_devices",
    ),
    region_rule: responseChoice(value.region_rule, ["any", "domestic_only"], "policy.region_rule"),
    network_rule: responseChoice(value.network_rule, ["any", "trusted_only"], "policy.network_rule"),
    allowed_cidrs: responseStringArray(value.allowed_cidrs, 32, 64, "policy.allowed_cidrs"),
    exception_membership_ids: responseStringArray(
      value.exception_membership_ids,
      32,
      36,
      "policy.exception_membership_ids",
    ).map((item) => responseResourceId(item, "wmb_", "policy.exception_membership_id")),
    revision: positiveInteger(value.revision, "policy.revision"),
    simulation_watermark_at: value.simulation_watermark_at === null
      ? null
      : timestamp(value.simulation_watermark_at, "policy.simulation_watermark_at"),
    updated_at: timestamp(value.updated_at, "policy.updated_at"),
  };
}

function validateSimulation(value) {
  plainObject(value, "simulation");
  exactKeys(
    value,
    [
      "affected_session_count",
      "outcome_code",
      "owner_ready",
      "policy_revision",
      "simulated_at",
      "simulation_id",
    ],
    "simulation",
  );
  return {
    simulation_id: responseResourceId(value.simulation_id, "cas_", "simulation_id"),
    policy_revision: positiveInteger(value.policy_revision, "simulation.policy_revision"),
    outcome_code: responseChoice(
      value.outcome_code,
      ["simulation.ready", "simulation.owner_blocked"],
      "simulation.outcome_code",
    ),
    affected_session_count: nonnegativeInteger(
      value.affected_session_count,
      "simulation.affected_session_count",
    ),
    owner_ready: responseBoolean(value.owner_ready, "simulation.owner_ready"),
    simulated_at: timestamp(value.simulated_at, "simulation.simulated_at"),
  };
}

function clientStepUpGrant(value) {
  const grant = text(value, 128);
  if (!STEP_UP_GRANT.test(grant)) invalidClient("step_up_grant");
  return grant;
}

function clientResourceId(value, prefix, label) {
  const id = text(value, 36);
  if (!RESOURCE_ID.test(id) || !id.startsWith(prefix) || id.length !== 36) invalidClient(label);
  return id;
}

function responseResourceId(value, prefix, label) {
  const id = text(value, 36);
  if (!RESOURCE_ID.test(id) || !id.startsWith(prefix) || id.length !== 36) invalidResponse(label);
  return id;
}

function clientStringArray(value, maximum, itemMaximum, label) {
  if (!Array.isArray(value) || value.length > maximum) invalidClient(label);
  const result = value.map((item) => {
    const normalized = text(item, itemMaximum);
    if (!normalized) invalidClient(label);
    return normalized;
  });
  if (new Set(result).size !== result.length) invalidClient(label);
  return result;
}

function responseStringArray(value, maximum, itemMaximum, label) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(label);
  const result = value.map((item) => {
    const normalized = text(item, itemMaximum);
    if (!normalized) invalidResponse(label);
    return normalized;
  });
  if (new Set(result).size !== result.length) invalidResponse(label);
  return result;
}

function clientChoice(value, choices, label) {
  if (!choices.includes(value)) invalidClient(label);
  return value;
}

function responseChoice(value, choices, label) {
  if (!choices.includes(value)) invalidResponse(label);
  return value;
}

function clientBoolean(value, label) {
  if (typeof value !== "boolean") invalidClient(label);
  return value;
}

function responseBoolean(value, label) {
  if (typeof value !== "boolean") invalidResponse(label);
  return value;
}

function positiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) invalidResponse(label);
  return value;
}

function nonnegativeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) invalidResponse(label);
  return value;
}

function timestamp(value, label) {
  const result = text(value, 64);
  if (!result || !Number.isFinite(Date.parse(result))) invalidResponse(label);
  return result;
}

function requestTrace(payload) {
  return {
    request_id: responseText(payload.request_id, 160, "request_id"),
    trace_id: responseText(payload.trace_id, 160, "trace_id"),
  };
}

function responseText(value, maximum, label) {
  const result = text(value, maximum);
  if (!result) invalidResponse(label);
  return result;
}

function text(value, maximum) {
  if (typeof value !== "string" || !value || value.length > maximum) return "";
  return /[\u0000-\u001f\u007f]/u.test(value) ? "" : value;
}

function plainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse(label);
}

function exactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const allowed = [...expected].sort();
  if (actual.length !== allowed.length || actual.some((key, index) => key !== allowed[index])) {
    invalidResponse(label);
  }
}

function invalidClient(label) {
  throw new IdentityApiError({
    code: "conditional_access.request_invalid",
    message: `条件访问请求中的 ${label} 无效。`,
  });
}

function invalidResponse(label) {
  throw new IdentityApiError({
    code: "conditional_access.response_invalid",
    message: `条件访问服务返回了无效的 ${label}。`,
  });
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
