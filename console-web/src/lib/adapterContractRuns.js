/**
 * PRD-064 Provider Adapter 契约测试报告只读客户端。
 *
 * 浏览器只读取当前 Session 租户的安全摘要，不提交 tenant_id，也不接收报告原文、Endpoint、
 * Header、Token、Prompt 或 Response。响应必须先通过严格白名单复制，才能进入页面状态。
 */

import { AuthApiError } from "./auth.js";

/** 列表接口唯一允许的查询字段；未知对象属性在 URL 构造前被忽略。 */
const FILTER_KEYS = Object.freeze(["status", "suite_kind", "mode", "limit", "cursor"]);
/** 后端不可变运行事实的固定枚举，前端不得根据自由文本扩展。 */
const RUN_STATUSES = new Set(["passed", "failed"]);
const SUITE_KINDS = new Set(["merge_gate", "probe"]);
const RUN_MODES = new Set(["mock", "gateway_probe"]);
const CASE_KINDS = new Set(["request", "non_stream", "stream", "error", "usage"]);
const AUTH_TYPES = new Set(["none", "bearer"]);
const PROVIDER_TYPES = new Set([
  "zhipu",
  "dashscope",
  "wenxin",
  "volcengine",
  "hunyuan",
  "deepseek",
  "openai_compatible",
  "custom",
  "migration_compatible",
]);
/** Adapter 与 failure code 白名单冻结可展示执行身份和脱敏失败分类。 */
const ADAPTER_NAMES = new Set(["openai_compatible", "generic_http"]);
/** failure code 只允许设计文档冻结的安全分类，不保留底层异常或断言正文。 */
const FAILURE_CODES = new Set([
  "adapter.contract_fixture_missing",
  "adapter.contract_fixture_unexpected",
  "adapter.contract_fixture_invalid",
  "adapter.contract_coverage_mismatch",
  "adapter.contract_request_failed",
  "adapter.contract_non_stream_failed",
  "adapter.contract_stream_failed",
  "adapter.contract_error_mapping_failed",
  "adapter.contract_usage_failed",
  "adapter.contract_probe_failed",
]);

/** 正则同时约束链路 ID、版本、hash、事实 ID 和 opaque cursor 的可接受语法。 */
const SAFE_ID = /^[A-Za-z0-9_.:-]{1,256}$/;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{0,159}$/;
const SAFE_VERSION = /^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}$/;
const HASH = /^[a-f0-9]{64}$/;
const RUN_ID = /^acr_[a-f0-9]{32}$/;
const CASE_ID = /^acc_[a-f0-9]{32}$/;
const CURSOR = /^[A-Za-z0-9_-]+={0,2}$/;

/**
 * 查询当前租户契约测试运行摘要。
 *
 * @param {object} input Control 地址、Session Token、当前租户校验值、筛选与 AbortSignal。
 * @returns {Promise<object>} 只含页面所需白名单字段的列表 DTO。
 */
export async function listAdapterContractRuns({
  controlApiUrl,
  token,
  expectedTenantId,
  filters = {},
  signal,
}) {
  const normalized = normalizeFilters(filters);
  const url = new URL(`${controlApiUrl}/adapter-contract-runs`);
  for (const key of FILTER_KEYS) {
    const value = normalized[key];
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url.toString(), {
    signal,
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Request-ID": createRequestId("list"),
    },
  });
  const payload = await readJson(response);
  // 非成功响应只进入统一错误转换；成功也必须经过 tenant owner 与 DTO 合同校验。
  if (!response.ok) throw responseError(response, payload, "契约测试运行读取失败");
  return sanitizeAdapterContractRunList(payload, response.headers, expectedTenantId);
}

/**
 * 查询一条当前租户契约运行及全部安全 case 摘要。
 *
 * run ID 在联网前严格校验并使用 encodeURIComponent；租户仍只由 Bearer Session 恢复，
 * expectedTenantId 只用于验证响应归属，绝不会进入 path 或 query。
 */
export async function getAdapterContractRun({
  controlApiUrl,
  token,
  expectedTenantId,
  adapterContractRunId,
  signal,
}) {
  const runId = strictText(adapterContractRunId, RUN_ID);
  // 非规范 ID 在联网前失败，防止 path traversal 或自由值进入请求日志。
  if (!runId) throw localContractError("adapter.contract_run_id_invalid", "契约测试运行标识无效", 422);
  const response = await fetch(
    `${controlApiUrl}/adapter-contract-runs/${encodeURIComponent(runId)}`,
    {
      signal,
      headers: {
        Authorization: `Bearer ${token}`,
        "X-Request-ID": createRequestId("detail"),
      },
    },
  );
  const payload = await readJson(response);
  // 详情同样先统一错误、再验证父子 owner 与 case 数量，不能宽松展示半份证据。
  if (!response.ok) throw responseError(response, payload, "契约测试运行详情读取失败");
  return sanitizeAdapterContractRunDetail(payload, response.headers, expectedTenantId, runId);
}

/**
 * 复制并验证列表成功响应。
 *
 * tenant_id 必须逐条等于当前认证快照，但不会被复制到返回对象；这既能发现代理或后端串租户，
 * 也避免租户标识进入 DOM、Storage 或诊断快照。
 */
export function sanitizeAdapterContractRunList(payload, headers, expectedTenantId) {
  const tenantId = requiredTenant(expectedTenantId);
  const runs = payload?.adapter_contract_runs;
  const limit = payload?.limit;
  const nextCursor = sanitizeCursor(payload?.next_cursor);
  const requestId = responseId(payload?.request_id, headers, "X-Request-ID");
  const traceId = responseId(payload?.trace_id, headers, "X-Trace-ID");
  if (
    !Array.isArray(runs)
    || runs.length > 100
    || !Number.isSafeInteger(limit)
    || limit < 1
    || limit > 100
    || nextCursor === undefined
  ) {
    // 顶层分页合同损坏时整批失败，不能展示可能错租户或截断的部分列表。
    throw invalidResponseError(requestId, traceId);
  }
  const sanitized = runs.map((run) => sanitizeRun(run, tenantId));
  if (
    sanitized.some((run) => run === null)
    || new Set(sanitized.map((run) => run.adapter_contract_run_id)).size !== sanitized.length
  ) {
    // 任一 run 无效或重复都会破坏不可变事实身份，整批响应必须失败关闭。
    throw invalidResponseError(requestId, traceId);
  }
  return Object.freeze({
    state: "ok",
    adapter_contract_runs: Object.freeze(sanitized),
    limit,
    next_cursor: nextCursor,
    request_id: requestId,
    trace_id: traceId,
  });
}

/** 验证详情父子 owner、数量和唯一键，并只复制页面实际展示的 case 字段。 */
export function sanitizeAdapterContractRunDetail(
  payload,
  headers,
  expectedTenantId,
  expectedRunId,
) {
  const tenantId = requiredTenant(expectedTenantId);
  const run = sanitizeRun(payload?.adapter_contract_run, tenantId);
  const cases = payload?.cases;
  const requestId = responseId(payload?.request_id, headers, "X-Request-ID");
  const traceId = responseId(payload?.trace_id, headers, "X-Trace-ID");
  if (
    !run
    || run.adapter_contract_run_id !== expectedRunId
    || !Array.isArray(cases)
    || cases.length > 2048
    || cases.length !== run.total_cases
  ) {
    // 详情父 run、case 数量或 path owner 不一致时不能降级为部分详情。
    throw invalidResponseError(requestId, traceId);
  }
  const sanitizedCases = cases.map((item) => sanitizeCase(item, tenantId, expectedRunId));
  if (
    sanitizedCases.some((item) => item === null)
    || new Set(sanitizedCases.map((item) => item.adapter_contract_case_id)).size !== sanitizedCases.length
    || new Set(sanitizedCases.map((item) => item.contract_case_key)).size !== sanitizedCases.length
    || sanitizedCases.filter((item) => item.status === "passed").length !== run.passed_cases
    || sanitizedCases.filter((item) => item.status === "failed").length !== run.failed_cases
  ) {
    // case owner、唯一键或通过/失败计数漂移会使报告不可验收，必须拒绝整份详情。
    throw invalidResponseError(requestId, traceId);
  }
  return Object.freeze({
    state: "ok",
    adapter_contract_run: run,
    cases: Object.freeze(sanitizedCases),
    request_id: requestId,
    trace_id: traceId,
  });
}

/** 规范化固定筛选；已知字段的非法值必须在 fetch 前失败，未知字段不会进入 URL。 */
function normalizeFilters(filters) {
  const status = optionalEnum(filters?.status, RUN_STATUSES);
  const suiteKind = optionalEnum(filters?.suite_kind, SUITE_KINDS);
  const mode = optionalEnum(filters?.mode, RUN_MODES);
  const limit = filters?.limit === undefined ? 50 : filters.limit;
  const cursor = filters?.cursor === undefined || filters?.cursor === null || filters?.cursor === ""
    ? null
    : strictText(filters.cursor, CURSOR, 4096);
  if (
    status === undefined
    || suiteKind === undefined
    || mode === undefined
    || !Number.isSafeInteger(limit)
    || limit < 1
    || limit > 100
    || cursor === ""
  ) {
    // 本地固定错误不回显篡改值，且必须在 fetch 前截断。
    throw localContractError("adapter.contract_filter_invalid", "契约测试筛选无效", 422);
  }
  return Object.freeze({ status, suite_kind: suiteKind, mode, limit, cursor });
}

/** 验证一条 run 的不可变计数、suite/mode 语义和安全摘要。 */
function sanitizeRun(value, expectedTenantId) {
  if (!value || typeof value !== "object" || value.tenant_id !== expectedTenantId) return null;
  const runId = strictText(value.adapter_contract_run_id, RUN_ID);
  const sourceRequestId = strictText(value.source_request_id, SAFE_ID);
  const sourceTraceId = strictText(value.source_trace_id, SAFE_ID);
  const catalogVersion = strictText(value.catalog_version, SAFE_VERSION);
  const catalogHash = strictText(value.catalog_hash, HASH);
  const runnerVersion = strictText(value.runner_version, SAFE_VERSION);
  const sourceRevisionHash = strictText(value.source_revision_hash, HASH);
  const reportHash = strictText(value.report_hash, HASH);
  const suiteKind = strictText(value.suite_kind, SAFE_CODE);
  const mode = strictText(value.mode, SAFE_CODE);
  const status = strictText(value.status, SAFE_CODE);
  const failureCodes = sanitizeFailureCodes(value.failure_codes);
  const startedAt = validTime(value.started_at);
  const completedAt = validTime(value.completed_at);
  const createdAt = validTime(value.created_at);
  const total = boundedCount(value.total_cases);
  const passed = boundedCount(value.passed_cases);
  const failed = boundedCount(value.failed_cases);
  if (
    !runId
    || !sourceRequestId
    || !sourceTraceId
    || value.schema_version !== "adapter-contract-report-v1"
    || !catalogVersion
    || !catalogHash
    || !runnerVersion
    || !sourceRevisionHash
    || !reportHash
    || !SUITE_KINDS.has(suiteKind)
    || !RUN_MODES.has(mode)
    || !RUN_STATUSES.has(status)
    || !failureCodes
    || !startedAt
    || !completedAt
    || !createdAt
    || completedAt < startedAt
    || createdAt < completedAt
    || total === null
    || passed === null
    || failed === null
    || total !== passed + failed
    || (status === "passed" && (failed !== 0 || failureCodes.length !== 0))
    || (status === "failed" && (failed < 1 || failureCodes.length < 1))
    || typeof value.coverage_complete !== "boolean"
    || (suiteKind === "merge_gate" && (mode !== "mock" || value.coverage_complete !== true))
    || (suiteKind === "probe" && (mode !== "gateway_probe" || value.coverage_complete !== false))
  ) return null;
  return Object.freeze({
    adapter_contract_run_id: runId,
    source_request_id: sourceRequestId,
    catalog_version: catalogVersion,
    catalog_hash: catalogHash,
    runner_version: runnerVersion,
    suite_kind: suiteKind,
    mode,
    status,
    coverage_complete: value.coverage_complete,
    total_cases: total,
    passed_cases: passed,
    failed_cases: failed,
    failure_codes: Object.freeze(failureCodes),
    started_at: String(value.started_at),
    completed_at: String(value.completed_at),
  });
}

/** 验证 case 的复合租户 owner、状态/failure code 配对和有界耗时。 */
function sanitizeCase(value, expectedTenantId, expectedRunId) {
  if (
    !value
    || typeof value !== "object"
    || value.tenant_id !== expectedTenantId
    || value.adapter_contract_run_id !== expectedRunId
  ) return null;
  const caseId = strictText(value.adapter_contract_case_id, CASE_ID);
  const caseKey = strictText(value.contract_case_key, SAFE_ID, 256);
  const specId = strictText(value.spec_id, SAFE_ID, 160);
  const providerType = strictText(value.provider_type, SAFE_CODE);
  const adapterName = strictText(value.adapter_name, SAFE_CODE);
  const adapterVersion = strictText(value.adapter_version, SAFE_VERSION);
  const caseKind = strictText(value.case_kind, SAFE_CODE);
  const variant = strictText(value.variant, SAFE_CODE);
  const authType = value.auth_type === null ? null : strictText(value.auth_type, SAFE_CODE);
  const status = strictText(value.status, SAFE_CODE);
  const failureCode = value.failure_code === null ? null : strictText(value.failure_code, SAFE_CODE);
  const duration = value.duration_ms;
  if (
    !caseId
    || !caseKey
    || !specId
    || !PROVIDER_TYPES.has(providerType)
    || !ADAPTER_NAMES.has(adapterName)
    || !adapterVersion
    || !CASE_KINDS.has(caseKind)
    || !variant
    || (authType !== null && !AUTH_TYPES.has(authType))
    || !RUN_STATUSES.has(status)
    || (status === "passed" && failureCode !== null)
    || (status === "failed" && !FAILURE_CODES.has(failureCode))
    || !Number.isSafeInteger(duration)
    || duration < 0
    || duration > 300000
    || !strictText(value.evidence_hash, HASH)
    || !validTime(value.created_at)
  ) return null;
  return Object.freeze({
    adapter_contract_case_id: caseId,
    contract_case_key: caseKey,
    spec_id: specId,
    provider_type: providerType,
    adapter_name: adapterName,
    adapter_version: adapterVersion,
    case_kind: caseKind,
    variant,
    auth_type: authType,
    status,
    failure_code: failureCode,
    duration_ms: duration,
  });
}

/** failure code 数组必须唯一、稳定排序且全部属于冻结集合。 */
function sanitizeFailureCodes(value) {
  if (!Array.isArray(value) || value.length > FAILURE_CODES.size) return null;
  const codes = value.map((item) => strictText(item, SAFE_CODE));
  if (
    codes.some((code) => !FAILURE_CODES.has(code))
    || new Set(codes).size !== codes.length
    || JSON.stringify(codes) !== JSON.stringify([...codes].sort())
  ) return null;
  return codes;
}

/** 把空筛选规范化为空串；非空但不在枚举中的值返回 undefined 触发本地失败关闭。 */
function optionalEnum(value, allowed) {
  if (value === undefined || value === null || value === "") return "";
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  return allowed.has(normalized) ? normalized : undefined;
}

/** 验证调用方提供的当前租户标识；缺失时不能验证响应 owner，因此失败关闭。 */
function requiredTenant(value) {
  const tenantId = strictText(value, SAFE_ID);
  if (!tenantId) throw localContractError("adapter.contract_tenant_invalid", "当前租户上下文无效", 401);
  // tenant 只用于响应等值校验，不会进入 URL 或返回 DTO。
  return tenantId;
}

/** 只接受 null 或有界 Base64URL cursor；opaque 值只用于下一次请求，不会进入渲染 DTO。 */
function sanitizeCursor(value) {
  if (value === null) return null;
  return strictText(value, CURSOR, 4096) || undefined;
}

/** 从 body 或响应头读取有界链路标识；缺失时保留短横线用于统一错误追踪。 */
function responseId(value, headers, headerName) {
  return strictText(value, SAFE_ID)
    || strictText(headers?.get?.(headerName), SAFE_ID)
    || "-";
}

/** 对字符串同时执行类型、长度、控制字符、首尾空白和模式约束。 */
function strictText(value, pattern, maximumLength = 256) {
  if (typeof value !== "string" || !value || value.length > maximumLength) return "";
  if (value !== value.trim() || /[\u0000-\u001f\u007f]/.test(value)) return "";
  return pattern.test(value) ? value : "";
}

/** 计数必须是 0..2048 的安全整数，禁止字符串隐式转换。 */
function boundedCount(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= 2048 ? value : null;
}

/** 返回可比较的有效时间戳；字符串原值仍由调用方保留以供中文格式化。 */
function validTime(value) {
  if (typeof value !== "string" || value.length > 64 || value !== value.trim()) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 统一读取 JSON；代理 HTML 和损坏正文不会进入异常消息或页面状态。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 把后端统一错误裁剪为现有 AuthApiError，不保留响应对象、Header 或 diagnosis 正文。 */
function responseError(response, payload, fallbackMessage) {
  const code = strictText(payload?.code, SAFE_CODE) || "adapter.contract_storage_unavailable";
  const requestId = responseId(payload?.request_id, response.headers, "X-Request-ID");
  const traceId = responseId(payload?.trace_id, response.headers, "X-Trace-ID");
  const message = safePublicMessage(payload?.message) || fallbackMessage;
  // 统一错误只携带公开 code/message/request/trace，禁止保留响应对象或 diagnosis 原文。
  return new AuthApiError({
    status: response.status,
    code,
    message,
    requestId,
    traceId,
    suggestion: safePublicMessage(payload?.suggestion),
    diagnosis: null,
  });
}

/** 仅接受有界无控制字符的后端公开消息，代理正文和复杂对象统一丢弃。 */
function safePublicMessage(value) {
  if (typeof value !== "string" || value.length > 500 || /[\u0000-\u001f\u007f]/.test(value)) return "";
  return value.trim();
}

/** 构造不回显篡改输入的本地合同错误。 */
function localContractError(code, message, status) {
  return new AuthApiError({ status, code, message, requestId: "-", traceId: "-", suggestion: "", diagnosis: null });
}

/** 成功响应损坏时固定返回本地 502；不把坏字段或原始 JSON 放入错误。 */
function invalidResponseError(requestId, traceId) {
  // 固定错误文本避免把损坏响应、租户 ID 或敏感扩展字段带入页面。
  return new AuthApiError({
    status: 502,
    code: "adapter.contract_response_invalid",
    message: "契约测试报告响应格式无效",
    requestId,
    traceId,
    suggestion: "请稍后重试或联系平台管理员。",
    diagnosis: null,
  });
}

/** 生成不含租户、筛选、Token 或业务字段的前端链路标识。 */
function createRequestId(kind) {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 12)
    ?? Math.random().toString(36).slice(2, 14);
  return `console_adapter_contract_${kind}_${Date.now()}_${random}`;
}
