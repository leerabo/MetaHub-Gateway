// Strict Platform configuration distribution client. It never accepts manifest material.
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const REBUILD_ID = /^crj_[0-9a-f]{32}$/u;
const JOB_ID = /^pjob_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const ERROR_MESSAGES = Object.freeze({
  "platform.permission_denied": "当前平台角色无权执行该操作。",
  "platform.csrf_invalid": "平台安全校验已失效。",
  "platform.origin_invalid": "请求来源无效。",
  "config_distribution.request_invalid": "配置分发请求无效。",
  "config_distribution.workspace_unavailable": "工作区配置 owner 当前不可用。",
  "config_distribution.idempotency_conflict": "幂等键已绑定到其他重建意图。",
  "config_distribution.unavailable": "配置分发服务暂时不可用。",
  "config_distribution.response_invalid": "配置分发响应格式无效。",
});

export function listConfigDistribution({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/config-distribution", { signal }, validList);
}

export function rebuildWorkspaceConfig({
  controlApiUrl,
  workspaceId,
  reasonCode = "operator_rebuild",
  idempotencyKey,
  signal,
} = {}) {
  const workspace = String(workspaceId ?? "");
  const key = String(idempotencyKey ?? "");
  if (!WORKSPACE_ID.test(workspace)
    || !new Set(["operator_rebuild", "consistency_repair"]).has(reasonCode)
    || !IDEMPOTENCY_KEY.test(key)) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    "/platform/config-rebuilds",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": readPlatformCsrfCookie(),
        "Idempotency-Key": key,
      },
      body: JSON.stringify({
        workspace_id: workspace,
        reason_code: reasonCode,
        confirmed: true,
      }),
      signal,
    },
    validRebuildResponse,
  );
}

async function request(controlApiUrl, path, options, validator) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options?.method ?? "GET",
      headers: { Accept: "application/json", ...(options?.headers ?? {}) },
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("config_distribution.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code
      : "config_distribution.unavailable";
    throw clientError(
      code,
      response.status,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) {
    throw clientError("config_distribution.response_invalid", 502);
  }
  return payload;
}

function validList(value) {
  return plainObject(value)
    && exactKeys(value, ["items", "total", "request_id", "trace_id"])
    && Array.isArray(value.items) && value.items.length <= 500
    && value.items.every(validItem) && integer(value.total, 0)
    && value.total === value.items.length && links(value);
}

function validItem(value) {
  return plainObject(value) && exactKeys(value, [
    "workspace_id", "cell_code", "generation", "revocation_watermark",
    "manifest_sha256", "gateway_count", "minimum_applied_generation",
    "minimum_lkg_generation", "stale_gateway_count", "latest_ack_at", "latest_rebuild",
  ]) && WORKSPACE_ID.test(String(value.workspace_id)) && code(value.cell_code)
    && integer(value.generation, 0) && integer(value.revocation_watermark, 0)
    && (value.manifest_sha256 === null || HASH.test(String(value.manifest_sha256)))
    && integer(value.gateway_count, 0) && integer(value.minimum_applied_generation, 0)
    && integer(value.minimum_lkg_generation, 0) && integer(value.stale_gateway_count, 0)
    && optionalTime(value.latest_ack_at)
    && (value.latest_rebuild === null || validRebuild(value.latest_rebuild));
}

function validRebuildResponse(value) {
  return plainObject(value)
    && exactKeys(value, ["rebuild", "idempotent_replay", "request_id", "trace_id"])
    && validRebuild(value.rebuild) && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function validRebuild(value) {
  return plainObject(value) && exactKeys(value, [
    "rebuild_job_id", "workspace_id", "target_generation", "platform_job_id", "status",
    "result_manifest_sha256", "revision", "created_at", "completed_at",
  ]) && REBUILD_ID.test(String(value.rebuild_job_id))
    && WORKSPACE_ID.test(String(value.workspace_id)) && integer(value.target_generation, 1)
    && JOB_ID.test(String(value.platform_job_id))
    && new Set(["queued", "running", "completed", "failed"]).has(value.status)
    && (value.result_manifest_sha256 === null || HASH.test(String(value.result_manifest_sha256)))
    && integer(value.revision, 1) && timestamp(value.created_at)
    && optionalTime(value.completed_at);
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function invalidRequest() {
  throw clientError("config_distribution.request_invalid", 0);
}

function clientError(codeValue, status, requestId = "-") {
  return new PlatformApiError({
    code: codeValue,
    status,
    requestId,
    message: ERROR_MESSAGES[codeValue] ?? "配置分发请求失败。",
  });
}

function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function integer(value, minimum) {
  return Number.isSafeInteger(value) && value >= minimum;
}

function code(value) {
  return /^[a-z0-9][a-z0-9.-]{1,95}$/u.test(String(value));
}

function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}

function optionalTime(value) { return value === null || timestamp(value); }
function links(value) {
  return LINK_ID.test(String(value.request_id ?? ""))
    && LINK_ID.test(String(value.trace_id ?? ""));
}

async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch {
    return {};
  }
}
